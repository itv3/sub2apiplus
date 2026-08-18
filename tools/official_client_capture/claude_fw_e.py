#!/usr/bin/env python3
"""执行 Claude Code FW-E 的 stable 冻结、静态取证、矩阵复核和证据封存。

本工具严格停在 EvidenceFact：它不会生成画像、Snapshot、ReleaseArtifact、
Persona 注册、production binding，也不会签发 EvidenceApprovalFact。所有真实抓包
仍由 ``capture.py`` 完成；本工具只冻结官方产物并验证控制组与目标组确实使用同一
份抓包执行源。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.extract_claude_bundle import (  # noqa: E402
    build_reachability_index,
    extract,
    write_modules,
)
from tools.official_client_capture.claude_target_inventory import (  # noqa: E402
    TargetInventoryError,
    build_target_inventory,
)
from tools.official_client_capture.claude_fw_e_crosswalk import (  # noqa: E402
    SCHEMA_CLOSURE,
    SCHEMA_MATRIX,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    canonical_json_sha256 as capture_canonical_json_sha256,
)
from tools.official_client_control.canonical import (  # noqa: E402
    canonical_sha256,
    canonical_json_bytes,
    load_json_file,
    sha256_bytes,
    sha256_file,
)
from tools.official_client_control.errors import ControlError  # noqa: E402
from tools.official_client_control.fw_e import seal_fw_e_plan  # noqa: E402
from tools.official_client_control.store import ControlStore  # noqa: E402


SCHEMA_FREEZE = "claude-code-fw-e-official-freeze/v1"
SCHEMA_STATIC = "claude-code-fw-e-static-diff/v1"
SCHEMA_CAPTURE_INDEX = "claude-code-fw-e-capture-index/v1"
SCHEMA_RULE_ASSESSMENTS = "claude-code-fw-e-rule-assessments/v1"
BASELINE_VERSION = "2.1.220"
MAIN_PACKAGE = "@anthropic-ai/claude-code"
PLATFORM_PACKAGES = {
    "darwin/arm64": "@anthropic-ai/claude-code-darwin-arm64",
    "linux/amd64": "@anthropic-ai/claude-code-linux-x64",
}
PACKAGE_BINARY_MEMBER = "package/claude"
REGISTRY_BASE = "https://registry.npmjs.org"
REGISTRY_KEYS_URL = f"{REGISTRY_BASE}/-/npm/v1/keys"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AST_TOOL_PATH = Path(__file__).with_name("claude_bundle_ast.mjs")
REQUIRED_PRIVACY_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}


class FWEEvidenceError(RuntimeError):
    """表示 FW-E 输入不闭合或官方身份不可验证。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_bytes(path: Path, content: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    mode = 0o700 if executable else 0o600
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fetch(url: str, *, accept: str) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "sub2apiplus-official-client-fw-e/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise FWEEvidenceError(f"官方 Registry 返回状态 {response.status}")
            content = response.read()
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "date", "etag", "last-modified"}
            }
            return content, headers
    except (OSError, urllib.error.URLError) as error:
        raise FWEEvidenceError(f"无法读取官方 Registry：{url}") from error


def _fetch_json(url: str) -> tuple[dict[str, Any], bytes, dict[str, str]]:
    raw, headers = _fetch(url, accept="application/json")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FWEEvidenceError(f"官方 Registry JSON 非法：{url}") from error
    if not isinstance(value, dict):
        raise FWEEvidenceError(f"官方 Registry 顶层不是对象：{url}")
    return value, raw, headers


def _package_url(package: str) -> str:
    return f"{REGISTRY_BASE}/{urllib.parse.quote(package, safe='')}"


def _selected_version_metadata(
    packument: dict[str, Any], package: str, version: str
) -> dict[str, Any]:
    if packument.get("name") != package:
        raise FWEEvidenceError(f"Registry package 身份不一致：{package}")
    versions = packument.get("versions")
    metadata = versions.get(version) if isinstance(versions, dict) else None
    if not isinstance(metadata, dict) or metadata.get("version") != version:
        raise FWEEvidenceError(f"Registry 缺少精确版本：{package}@{version}")
    dist = metadata.get("dist")
    if not isinstance(dist, dict):
        raise FWEEvidenceError(f"Registry 缺少 dist：{package}@{version}")
    required = ("tarball", "integrity", "shasum", "signatures")
    if any(not dist.get(key) for key in required):
        raise FWEEvidenceError(f"Registry dist 身份字段不完整：{package}@{version}")
    return metadata


def _verify_integrity(content: bytes, integrity: str, shasum: str) -> None:
    algorithm, separator, encoded = integrity.partition("-")
    if separator != "-" or algorithm != "sha512":
        raise FWEEvidenceError("当前只接受官方 sha512 SRI")
    try:
        expected = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise FWEEvidenceError("官方 SRI 不是合法 Base64") from error
    if hashlib.sha512(content).digest() != expected:
        raise FWEEvidenceError("tarball 与官方 sha512 integrity 不一致")
    if hashlib.sha1(content).hexdigest() != shasum:
        raise FWEEvidenceError("tarball 与官方 shasum 不一致")


def _verify_signatures(
    package: str,
    version: str,
    integrity: str,
    signatures: Any,
    keys: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(signatures, list) or not signatures:
        raise FWEEvidenceError(f"{package}@{version} 缺少 Registry 签名")
    key_rows = keys.get("keys")
    if not isinstance(key_rows, list) or not key_rows:
        raise FWEEvidenceError("Registry 公钥集合为空")
    by_id = {
        item.get("keyid"): item
        for item in key_rows
        if isinstance(item, dict) and isinstance(item.get("keyid"), str)
    }
    message = f"{package}@{version}:{integrity}".encode("utf-8")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="claude-fw-e-signature-") as directory:
        root = Path(directory)
        message_path = root / "message.txt"
        message_path.write_bytes(message)
        for index, signature in enumerate(signatures):
            if not isinstance(signature, dict):
                raise FWEEvidenceError("Registry 签名记录非法")
            key_id = signature.get("keyid")
            key = by_id.get(key_id)
            if not isinstance(key, dict):
                raise FWEEvidenceError(f"Registry 签名公钥不存在：{key_id}")
            if key.get("scheme") != "ecdsa-sha2-nistp256":
                raise FWEEvidenceError(f"Registry 签名算法不受支持：{key_id}")
            try:
                key_der = base64.b64decode(str(key["key"]), validate=True)
                signature_der = base64.b64decode(str(signature["sig"]), validate=True)
            except (KeyError, ValueError) as error:
                raise FWEEvidenceError("Registry 签名或公钥编码非法") from error
            key_der_path = root / f"key-{index}.der"
            key_pem_path = root / f"key-{index}.pem"
            signature_path = root / f"signature-{index}.der"
            key_der_path.write_bytes(key_der)
            signature_path.write_bytes(signature_der)
            converted = subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-pubin",
                    "-inform",
                    "DER",
                    "-in",
                    str(key_der_path),
                    "-out",
                    str(key_pem_path),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if converted.returncode != 0:
                raise FWEEvidenceError("无法解析 Registry ECDSA 公钥")
            verified = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(key_pem_path),
                    "-signature",
                    str(signature_path),
                    str(message_path),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if verified.returncode != 0:
                raise FWEEvidenceError(f"Registry 签名验证失败：{package}@{version}")
            results.append(
                {
                    "keyid": key_id,
                    "scheme": key["scheme"],
                    "key_expires": key.get("expires"),
                    "signature_sha256": sha256_bytes(signature_der),
                    "verified": True,
                }
            )
    return results


def _safe_package_name(package: str) -> str:
    return package.removeprefix("@").replace("/", "-")


def _validate_tar_members(archive: tarfile.TarFile) -> None:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != member.name:
            raise FWEEvidenceError(f"npm tarball 含不安全路径：{member.name}")
        if not (member.isfile() or member.isdir()):
            raise FWEEvidenceError(f"npm tarball 含非普通成员：{member.name}")


def _package_identity_and_binary(
    content: bytes,
    package: str,
    version: str,
    *,
    require_binary: bool,
) -> tuple[dict[str, Any], bytes | None]:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            _validate_tar_members(archive)
            package_json = archive.extractfile("package/package.json")
            if package_json is None:
                raise FWEEvidenceError("npm tarball 缺少 package/package.json")
            metadata = json.loads(package_json.read())
            if metadata.get("name") != package or metadata.get("version") != version:
                raise FWEEvidenceError(f"tarball package 身份不一致：{package}@{version}")
            binary: bytes | None = None
            if require_binary:
                binary_stream = archive.extractfile(PACKAGE_BINARY_MEMBER)
                if binary_stream is None:
                    raise FWEEvidenceError(f"平台包缺少 {PACKAGE_BINARY_MEMBER}")
                binary = binary_stream.read()
                if not binary:
                    raise FWEEvidenceError("平台二进制为空")
            return metadata, binary
    except (tarfile.TarError, json.JSONDecodeError, KeyError) as error:
        raise FWEEvidenceError(f"无法验证 npm tarball：{package}@{version}") from error


def _minimal_registry_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    dist = metadata["dist"]
    return {
        "name": metadata["name"],
        "version": metadata["version"],
        "bin": metadata.get("bin"),
        "optionalDependencies": metadata.get("optionalDependencies", {}),
        "dist": {
            key: dist.get(key)
            for key in (
                "tarball",
                "integrity",
                "shasum",
                "fileCount",
                "unpackedSize",
                "signatures",
            )
        },
    }


def freeze_official_stable(output_root: Path) -> dict[str, Any]:
    """先查 stable，再冻结主包和两种目标平台包，最后复查 stable。"""

    if not output_root.is_absolute():
        raise FWEEvidenceError("freeze output_root 必须是绝对路径")
    if output_root.exists():
        raise FWEEvidenceError("freeze output_root 必须不存在，禁止覆盖或续写")
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_root.parent.chmod(0o700)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.staging-", dir=output_root.parent)
    )
    staging.chmod(0o700)
    try:
        start_at = _utc_now()
        main_packument, main_raw, main_headers = _fetch_json(_package_url(MAIN_PACKAGE))
        tags = main_packument.get("dist-tags")
        stable = tags.get("stable") if isinstance(tags, dict) else None
        if not isinstance(stable, str) or not VERSION_RE.fullmatch(stable):
            raise FWEEvidenceError("官方 dist-tags.stable 不是精确三段版本")
        package_names = [MAIN_PACKAGE, *PLATFORM_PACKAGES.values()]
        keys, keys_raw, key_headers = _fetch_json(REGISTRY_KEYS_URL)
        selected: dict[str, dict[str, Any]] = {
            MAIN_PACKAGE: _selected_version_metadata(main_packument, MAIN_PACKAGE, stable)
        }
        raw_receipts: dict[str, dict[str, Any]] = {
            MAIN_PACKAGE: {
                "response_sha256": sha256_bytes(main_raw),
                "response_bytes": len(main_raw),
                "headers": main_headers,
            }
        }
        for package in package_names[1:]:
            packument, raw, headers = _fetch_json(_package_url(package))
            selected[package] = _selected_version_metadata(packument, package, stable)
            raw_receipts[package] = {
                "response_sha256": sha256_bytes(raw),
                "response_bytes": len(raw),
                "headers": headers,
            }
        optional = selected[MAIN_PACKAGE].get("optionalDependencies")
        if not isinstance(optional, dict) or any(
            optional.get(package) != stable for package in PLATFORM_PACKAGES.values()
        ):
            raise FWEEvidenceError("主包没有把目标平台包精确绑定到 stable")

        artifact_rows: list[dict[str, Any]] = []
        binary_rows: dict[str, dict[str, Any]] = {}
        package_to_platform = {value: key for key, value in PLATFORM_PACKAGES.items()}
        for package in package_names:
            metadata = selected[package]
            dist = metadata["dist"]
            tarball, download_headers = _fetch(
                str(dist["tarball"]), accept="application/octet-stream"
            )
            _verify_integrity(tarball, str(dist["integrity"]), str(dist["shasum"]))
            signature_results = _verify_signatures(
                package,
                stable,
                str(dist["integrity"]),
                dist["signatures"],
                keys,
            )
            package_json, binary = _package_identity_and_binary(
                tarball,
                package,
                stable,
                require_binary=package in package_to_platform,
            )
            tarball_relative = f"official/{_safe_package_name(package)}-{stable}.tgz"
            _write_private_bytes(staging / tarball_relative, tarball)
            row = {
                "package": package,
                "version": stable,
                "tarball_url": dist["tarball"],
                "integrity": dist["integrity"],
                "shasum": dist["shasum"],
                "tarball_path": tarball_relative,
                "tarball_sha256": sha256_bytes(tarball),
                "tarball_bytes": len(tarball),
                "download_headers": download_headers,
                "registry_signatures": signature_results,
                "package_json_sha256": sha256_bytes(canonical_json_bytes(package_json)),
            }
            artifact_rows.append(row)
            if binary is not None:
                platform = package_to_platform[package]
                binary_relative = (
                    f"binaries/{platform.replace('/', '-')}/claude"
                )
                _write_private_bytes(staging / binary_relative, binary, executable=True)
                binary_rows[platform] = {
                    "package": package,
                    "path": binary_relative,
                    "sha256": sha256_bytes(binary),
                    "bytes": len(binary),
                }

        end_packument, end_raw, end_headers = _fetch_json(_package_url(MAIN_PACKAGE))
        end_tags = end_packument.get("dist-tags")
        stable_end = end_tags.get("stable") if isinstance(end_tags, dict) else None
        if stable_end != stable:
            raise FWEEvidenceError(
                f"冻结期间 stable 从 {stable} 变化为 {stable_end}，本轮必须作废"
            )
        registry_snapshot = {
            "schema_version": "claude-code-fw-e-registry-snapshot/v1",
            "registry": REGISTRY_BASE,
            "queried_at_start_utc": start_at,
            "queried_at_end_utc": _utc_now(),
            "dist_tags_at_start": {
                key: tags[key]
                for key in sorted(tags)
                if isinstance(tags.get(key), str)
            },
            "stable_at_end": stable_end,
            "packages": [
                _minimal_registry_metadata(selected[package]) for package in package_names
            ],
            "packument_receipts": raw_receipts,
            "end_packument_receipt": {
                "response_sha256": sha256_bytes(end_raw),
                "response_bytes": len(end_raw),
                "headers": end_headers,
            },
            "registry_keys_receipt": {
                "response_sha256": sha256_bytes(keys_raw),
                "response_bytes": len(keys_raw),
                "headers": key_headers,
                "keys": keys.get("keys"),
            },
        }
        _write_private_json(staging / "registry-snapshot.json", registry_snapshot)
        artifact_rows.sort(key=lambda item: item["tarball_path"])
        freeze = {
            "schema_version": SCHEMA_FREEZE,
            "target_version": stable,
            "baseline_version": BASELINE_VERSION,
            "stable_at_start": stable,
            "stable_at_end": stable_end,
            "registry_snapshot_path": "registry-snapshot.json",
            "registry_snapshot_sha256": sha256_file(staging / "registry-snapshot.json"),
            "platforms": sorted(binary_rows),
            "entrypoint": "sdk-cli",
            "privacy_mode": "essential-traffic",
            "default_conditions": [
                "authentication=claude.ai-oauth",
                "entrypoint=sdk-cli",
                "model=claude-sonnet-5",
                "privacy=essential-traffic",
                "provider=firstParty",
            ],
            "artifacts": artifact_rows,
            "binaries": {key: binary_rows[key] for key in sorted(binary_rows)},
            "producer": {
                "path": "tools/official_client_capture/claude_fw_e.py",
                "sha256": sha256_file(Path(__file__)),
            },
            "freeze_completed_at_utc": _utc_now(),
        }
        _write_private_json(staging / "freeze.json", freeze)
        os.rename(staging, output_root)
        return freeze
    except BaseException:
        if staging.exists() and staging.is_dir() and staging.parent == output_root.parent:
            shutil.rmtree(staging)
        raise


def _probe_map(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    probes = index.get("probes")
    if not isinstance(probes, list):
        raise FWEEvidenceError("reachability index 缺少 probes")
    result: dict[str, dict[str, Any]] = {}
    for probe in probes:
        if not isinstance(probe, dict) or not isinstance(probe.get("candidate"), str):
            raise FWEEvidenceError("reachability probe 非法")
        if probe["candidate"] in result:
            raise FWEEvidenceError(f"reachability probe 重复：{probe['candidate']}")
        result[probe["candidate"]] = probe
    return result


def _probe_digest_set(probe: dict[str, Any] | None) -> list[str]:
    if not probe:
        return []
    hits = probe.get("hits")
    if not isinstance(hits, list):
        return []
    return sorted(
        {
            str(hit["alpha_sha256"])
            for hit in hits
            if isinstance(hit, dict) and SHA256_RE.fullmatch(str(hit.get("alpha_sha256", "")))
        }
    )


def analyze_bundles(
    freeze_root: Path,
    output_root: Path,
    baseline_anchors_path: Path,
    baseline_reachability_path: Path,
    node_binary: str = "node",
    typescript_module_path: Path | None = None,
) -> dict[str, Any]:
    if output_root.exists():
        raise FWEEvidenceError("static output_root 必须不存在，禁止覆盖")
    freeze = load_json_file(freeze_root / "freeze.json", "FW-E freeze")
    if freeze.get("schema_version") != SCHEMA_FREEZE:
        raise FWEEvidenceError("FW-E freeze schema 不匹配")
    output_root.mkdir(parents=True, mode=0o700)
    output_root.chmod(0o700)
    platform_results: dict[str, dict[str, Any]] = {}
    for platform in sorted(PLATFORM_PACKAGES):
        binary_row = freeze["binaries"].get(platform)
        if not isinstance(binary_row, dict):
            raise FWEEvidenceError(f"freeze 缺少平台二进制：{platform}")
        binary = freeze_root / str(binary_row["path"])
        if sha256_file(binary) != binary_row["sha256"]:
            raise FWEEvidenceError(f"冻结二进制摘要漂移：{platform}")
        platform_dir = output_root / platform.replace("/", "-")
        modules_dir = platform_dir / "modules"
        platform_dir.mkdir(parents=True, mode=0o700)
        result = extract(binary, str(binary_row["sha256"]))
        write_modules(binary, result, modules_dir)
        anchors_path = platform_dir / "bundle-anchors.json"
        _write_private_json(anchors_path, result)
        reachability = build_reachability_index(modules_dir / "cli.js")
        reachability_path = platform_dir / "reachability-index.json"
        _write_private_json(reachability_path, reachability)
        ast_path = platform_dir / "target-native-ast.json"
        command = [
            node_binary,
            str(AST_TOOL_PATH),
            "--bundle",
            str(modules_dir / "cli.js"),
            "--output",
            str(ast_path),
            "--expected-sha256",
            str(reachability["bundle_sha256"]),
        ]
        if typescript_module_path is not None:
            command.extend(["--typescript-module", str(typescript_module_path)])
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if completed.returncode != 0:
            raise FWEEvidenceError(
                f"目标 AST inventory 失败：{platform}：{completed.stderr.strip()}"
            )
        ast_inventory = load_json_file(ast_path, f"{platform} AST inventory")
        sink_inventory = build_target_inventory(
            ast_inventory,
            reachability,
            target_version=str(freeze["target_version"]),
            platform=platform,
            ast_binding={
                "path": ast_path.relative_to(output_root).as_posix(),
                "sha256": sha256_file(ast_path),
            },
            lexical_binding={
                "path": reachability_path.relative_to(output_root).as_posix(),
                "sha256": sha256_file(reachability_path),
            },
        )
        sink_inventory_path = platform_dir / "target-sink-inventory.json"
        _write_private_json(sink_inventory_path, sink_inventory)
        platform_results[platform] = {
            "binary_sha256": binary_row["sha256"],
            "anchors_path": anchors_path.relative_to(output_root).as_posix(),
            "anchors_sha256": sha256_file(anchors_path),
            "reachability_path": reachability_path.relative_to(output_root).as_posix(),
            "reachability_sha256": sha256_file(reachability_path),
            "target_native_ast_path": ast_path.relative_to(output_root).as_posix(),
            "target_native_ast_sha256": sha256_file(ast_path),
            "target_sink_inventory_path": sink_inventory_path.relative_to(
                output_root
            ).as_posix(),
            "target_sink_inventory_sha256": sha256_file(sink_inventory_path),
            "target_sink_count": sink_inventory["sink_total"],
            "bundle_sha256": reachability["bundle_sha256"],
        }
    baseline_anchors = load_json_file(baseline_anchors_path, "2.1.220 anchors")
    baseline_index = load_json_file(baseline_reachability_path, "2.1.220 reachability")
    baseline_probes = _probe_map(baseline_index)
    target_indexes = {
        platform: load_json_file(
            output_root / row["reachability_path"], f"{platform} reachability"
        )
        for platform, row in platform_results.items()
    }
    target_probe_maps = {
        platform: _probe_map(index) for platform, index in target_indexes.items()
    }
    candidates = sorted(
        set(baseline_probes).union(
            *(set(value) for value in target_probe_maps.values())
        )
    )
    probe_diffs = []
    for candidate in candidates:
        baseline_digests = _probe_digest_set(baseline_probes.get(candidate))
        platform_digests = {
            platform: _probe_digest_set(probes.get(candidate))
            for platform, probes in target_probe_maps.items()
        }
        target_sets = list(platform_digests.values())
        probe_diffs.append(
            {
                "candidate": candidate,
                "baseline_alpha_sha256": baseline_digests,
                "target_alpha_sha256": platform_digests,
                "target_cross_platform_equal": len({tuple(item) for item in target_sets}) == 1,
                "baseline_target_equal": all(
                    item == baseline_digests for item in target_sets
                ),
            }
        )
    static_diff = {
        "schema_version": SCHEMA_STATIC,
        "baseline_version": BASELINE_VERSION,
        "target_version": freeze["target_version"],
        "baseline": {
            "binary_sha256": baseline_anchors.get("binary_sha256"),
            "bundle_sha256": baseline_index.get("bundle_sha256"),
            "anchors_path": str(baseline_anchors_path),
            "anchors_sha256": sha256_file(baseline_anchors_path),
            "reachability_path": str(baseline_reachability_path),
            "reachability_sha256": sha256_file(baseline_reachability_path),
        },
        "target_platforms": platform_results,
        "probe_diffs": probe_diffs,
        "semantic_inheritance_policy": (
            "只有对应规则的结构锚点、依赖和 sink 关系均被工具明确证明等价时，"
            "才能使用 inherit；字符串相同或单个 alpha 摘要相同不足以证明。"
        ),
        "target_discovery_policy": (
            "目标规则分母是 AST 调用点与无截断词法候选的并集；lexical_only 必须显式"
            "处置，不能因 AST 未识别而删除。"
        ),
        "ast_tool": {
            "path": AST_TOOL_PATH.relative_to(Path(__file__).resolve().parents[2]).as_posix(),
            "sha256": sha256_file(AST_TOOL_PATH),
        },
        "producer": {
            "path": "tools/official_client_capture/claude_fw_e.py",
            "sha256": sha256_file(Path(__file__)),
        },
        "generated_at_utc": _utc_now(),
    }
    _write_private_json(output_root / "static-diff.json", static_diff)
    return static_diff


def _manifest_binding(path: Path, group_root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(group_root).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _case_analysis(
    manifest_path: Path, case: dict[str, Any], label: str
) -> dict[str, Any]:
    relative = case.get("analysis_path")
    if not isinstance(relative, str):
        raise FWEEvidenceError(f"{label} case 缺少 analysis_path：{manifest_path}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        raise FWEEvidenceError(f"{label} analysis_path 非法：{relative}")
    run_root = manifest_path.parent.resolve()
    path = (manifest_path.parent / pure).resolve()
    try:
        path.relative_to(run_root)
    except ValueError as error:
        raise FWEEvidenceError(f"{label} analysis_path 越界：{relative}") from error
    if path.is_symlink() or not path.is_file():
        raise FWEEvidenceError(f"{label} analysis 不是普通文件：{relative}")
    return load_json_file(path, f"{label} analysis")


def _network_key(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化一个不含秘密的运行网络坐标。"""

    return {
        "transport": str(raw.get("transport", "")),
        "method": str(raw.get("method", "")),
        "scheme": str(raw.get("scheme", "")),
        "host": str(raw.get("host", "")).lower(),
        "port": str(raw.get("port", "")),
        "path": str(raw.get("path", "")),
    }


def _network_observation_rows(
    manifest_path: Path,
    case: dict[str, Any],
    label: str,
) -> tuple[str, list[dict[str, Any]]]:
    """从单个 case 的规范化证据提取全 host／path 坐标。"""

    capture = case.get("capture")
    scope = capture.get("host_scope") if isinstance(capture, dict) else None
    if scope not in {"all", "targets"}:
        raise FWEEvidenceError(f"{label} case 缺少受管 host_scope：{manifest_path}")
    analysis = _case_analysis(manifest_path, case, label)
    evidence = str(case.get("evidence"))
    rows: list[dict[str, Any]] = []
    if evidence == "mitm":
        if analysis.get("schema_version") != "official-client-capture-normalized/v1":
            raise FWEEvidenceError(f"{label} MITM analysis schema 非法")
        lifecycle = analysis.get("network_lifecycle")
        if not isinstance(lifecycle, list):
            raise FWEEvidenceError(f"{label} MITM analysis 缺少请求生命周期 inventory")
        for raw in lifecycle:
            if not isinstance(raw, dict) or raw.get("event") != "request":
                continue
            if raw.get("capture_host_scope") != scope:
                raise FWEEvidenceError(f"{label} MITM host_scope 与 manifest 不一致")
            rows.append(_network_key({**raw, "transport": "http"}))
        records = analysis.get("records")
        if not isinstance(records, list):
            raise FWEEvidenceError(f"{label} MITM analysis 缺少 records")
        for raw in records:
            if not isinstance(raw, dict) or raw.get("kind") != "websocket_frame":
                continue
            rows.append(
                _network_key(
                    {
                        **raw,
                        "transport": "websocket",
                        "method": "GET",
                    }
                )
            )
    elif evidence == "direct":
        if analysis.get("schema_version") != "official-client-capture-tls/v1":
            raise FWEEvidenceError(f"{label} direct analysis schema 非法")
        if analysis.get("capture_host_scope") != scope:
            raise FWEEvidenceError(f"{label} direct host_scope 与 manifest 不一致")
        hellos = analysis.get("client_hellos")
        if not isinstance(hellos, list):
            raise FWEEvidenceError(f"{label} direct analysis 缺少 ClientHello")
        for hello in hellos:
            if not isinstance(hello, dict):
                continue
            rows.append(
                _network_key(
                    {
                        "transport": "tls",
                        "method": "CONNECT",
                        "scheme": "tls",
                        "host": hello.get("sni"),
                        "port": hello.get("port"),
                        "path": "",
                    }
                )
            )
    else:
        raise FWEEvidenceError(f"{label} 未知 evidence：{evidence}")
    for row in rows:
        if not row["host"] or (scope == "all" and row["host"] == "<target-host>"):
            raise FWEEvidenceError(f"{label} 全量网络坐标缺少实际 host")
    return scope, rows


def _merge_network_observation(
    inventory: dict[str, dict[str, Any]],
    row: dict[str, Any],
    *,
    scenario: str,
    evidence: str,
) -> None:
    digest = canonical_sha256(row)
    observation_id = f"RUN-NET-{digest[:20]}"
    current = inventory.setdefault(
        observation_id,
        {
            "observation_id": observation_id,
            **row,
            "occurrence_count": 0,
            "scenarios": set(),
            "evidence_modes": set(),
        },
    )
    if any(current.get(key) != value for key, value in row.items()):
        raise FWEEvidenceError(f"运行网络 observation ID 冲突：{observation_id}")
    current["occurrence_count"] += 1
    current["scenarios"].add(scenario)
    current["evidence_modes"].add(evidence)


def _validate_case_privacy_environment(
    manifest_path: Path,
    case: dict[str, Any],
    label: str,
) -> str:
    """核验实际 Claude 子进程确实关闭遥测与非必要流量。"""

    scenario_result = case.get("scenario_result")
    invocation = (
        scenario_result.get("invocation")
        if isinstance(scenario_result, dict)
        else None
    )
    environment = (
        invocation.get("environment") if isinstance(invocation, dict) else None
    )
    if (
        not isinstance(environment, dict)
        or environment.get("schema_version") != "official-client-environment/v1"
    ):
        raise FWEEvidenceError(
            f"{label} case 缺少受管环境清单：{manifest_path}"
        )
    values = environment.get("values")
    if not isinstance(values, dict):
        raise FWEEvidenceError(f"{label} 环境 values 非法：{manifest_path}")
    if environment.get("keys") != sorted(values):
        raise FWEEvidenceError(f"{label} 环境 keys 与 values 不一致：{manifest_path}")
    # invocation 环境由 capturelib.security.environment_manifest_view 生成，
    # 它的摘要规范不带末尾换行；不能误用控制 Store 带换行的 canonical_sha256。
    environment_sha256 = capture_canonical_json_sha256(values)
    if environment.get("sha256") != environment_sha256:
        raise FWEEvidenceError(f"{label} 环境摘要复算失败：{manifest_path}")
    for key, expected in REQUIRED_PRIVACY_ENV.items():
        if values.get(key) != expected:
            raise FWEEvidenceError(
                f"{label} 隐私开关实际值非法：{key}={values.get(key)!r}，"
                f"要求 {expected!r}：{manifest_path}"
            )
    return environment_sha256


def _scan_capture_group(
    group_root: Path,
    label: str,
    expected_version: str,
    expected_binary_sha256: str,
) -> dict[str, Any]:
    manifests = sorted(group_root.rglob("manifest.json"))
    if not manifests:
        raise FWEEvidenceError(f"{label} 没有 manifest.json")
    rows: list[dict[str, Any]] = []
    source_digests: set[str] = set()
    evidence_modes: set[str] = set()
    scenarios: set[str] = set()
    capture_host_scopes: set[str] = set()
    network_inventory: dict[str, dict[str, Any]] = {}
    environment_sha256s: set[str] = set()
    case_count = 0
    for path in manifests:
        manifest = load_json_file(path, f"{label} manifest")
        if manifest.get("schema_version") != "official-client-capture/v1":
            raise FWEEvidenceError(f"{label} manifest schema 非法：{path}")
        if manifest.get("status") != "complete":
            raise FWEEvidenceError(f"{label} 抓包未完成：{path}")
        cleanup = manifest.get("cleanup")
        m_binding = manifest.get("m_binding")
        secret_scan = manifest.get("secret_scan")
        if not isinstance(cleanup, dict) or cleanup.get("successful") is not True:
            raise FWEEvidenceError(f"{label} 清理证明失败：{path}")
        if not isinstance(m_binding, dict) or m_binding.get("complete") is not True:
            raise FWEEvidenceError(f"{label} M 不完整：{path}")
        if (
            not isinstance(secret_scan, dict)
            or secret_scan.get("passed") is not True
            or secret_scan.get("matches") != []
        ):
            raise FWEEvidenceError(f"{label} 终态秘密扫描失败：{path}")
        clients = manifest.get("clients")
        client = clients.get("claude") if isinstance(clients, dict) else None
        if not isinstance(client, dict):
            raise FWEEvidenceError(f"{label} 缺少 Claude 客户端身份：{path}")
        actual_version = str(client.get("version", "")).split(" ", 1)[0]
        if actual_version != expected_version or client.get("sha256") != expected_binary_sha256:
            raise FWEEvidenceError(f"{label} 客户端版本或摘要漂移：{path}")
        runtime = manifest.get("runtime")
        tools = runtime.get("capture_tools") if isinstance(runtime, dict) else None
        sources = tools.get("execution_sources") if isinstance(tools, dict) else None
        source_sha = sources.get("sha256") if isinstance(sources, dict) else None
        if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
            raise FWEEvidenceError(f"{label} 缺少抓包执行源摘要：{path}")
        source_digests.add(source_sha)
        runtime_receipt = runtime.get("host_runtime_receipt") if isinstance(runtime, dict) else None
        if (
            not isinstance(runtime_receipt, dict)
            or runtime_receipt.get("repo_digest_verified") is not True
            or runtime_receipt.get("container_runtime_binding", {}).get("verified") is not True
        ):
            raise FWEEvidenceError(f"{label} 宿主运行身份未验证：{path}")
        case_results = manifest.get("case_results")
        if not isinstance(case_results, list) or not case_results:
            raise FWEEvidenceError(f"{label} 没有 case result：{path}")
        for case in case_results:
            if not isinstance(case, dict) or case.get("status") != "complete":
                raise FWEEvidenceError(f"{label} case 未完成：{path}")
            environment_sha256s.add(
                _validate_case_privacy_environment(path, case, label)
            )
            case_count += 1
            evidence_modes.add(str(case.get("evidence")))
            scenarios.add(str(case.get("scenario")))
            scope, observations = _network_observation_rows(path, case, label)
            capture_host_scopes.add(scope)
            for observation in observations:
                _merge_network_observation(
                    network_inventory,
                    observation,
                    scenario=str(case.get("scenario")),
                    evidence=str(case.get("evidence")),
                )
        rows.append(
            {
                "batch_id": manifest.get("batch_id"),
                "manifest": _manifest_binding(path, group_root),
                "case_count": len(case_results),
                "evidence_modes": sorted(
                    {str(item.get("evidence")) for item in case_results}
                ),
                "scenarios": sorted({str(item.get("scenario")) for item in case_results}),
                "capture_source_sha256": source_sha,
                "runtime_image_reference": runtime_receipt.get("runtime_image_reference"),
            }
        )
    if len(source_digests) != 1:
        raise FWEEvidenceError(f"{label} 内部使用了多份抓包执行源：{sorted(source_digests)}")
    normalized_network_inventory = []
    for observation_id in sorted(network_inventory):
        row = network_inventory[observation_id]
        normalized_network_inventory.append(
            {
                **row,
                "scenarios": sorted(row["scenarios"]),
                "evidence_modes": sorted(row["evidence_modes"]),
            }
        )
    return {
        "label": label,
        "version": expected_version,
        "binary_sha256": expected_binary_sha256,
        "capture_source_sha256": next(iter(source_digests)),
        "manifest_count": len(rows),
        "evidence_modes": sorted(evidence_modes),
        "scenarios": sorted(scenarios),
        "capture_host_scopes": sorted(capture_host_scopes),
        "network_observations": normalized_network_inventory,
        "privacy_controls": {
            "required_values": dict(sorted(REQUIRED_PRIVACY_ENV.items())),
            "case_count": case_count,
            "environment_manifest_sha256s": sorted(environment_sha256s),
            "result": "passed",
        },
        "runs": rows,
    }


def build_capture_index(
    control_root: Path,
    target_root: Path,
    output_path: Path,
    target_version: str,
    control_binary_sha256: str,
    target_binary_sha256: str,
    relay_index_path: Path | None,
) -> dict[str, Any]:
    control = _scan_capture_group(
        control_root, "control", BASELINE_VERSION, control_binary_sha256
    )
    target = _scan_capture_group(
        target_root, "target", target_version, target_binary_sha256
    )
    for group in (control, target):
        if group["capture_host_scopes"] != ["all"]:
            raise FWEEvidenceError(
                f"{group['label']} 没有关闭 host 预筛：{group['capture_host_scopes']}"
            )
        if not group["network_observations"]:
            raise FWEEvidenceError(f"{group['label']} 全 host/path inventory 为空")
    if control["capture_source_sha256"] != target["capture_source_sha256"]:
        raise FWEEvidenceError("控制组与目标组没有使用同一冻结抓包执行源")
    relay: dict[str, Any] | None = None
    channels = {"A1", "J", "L", "M", "P"}
    if relay_index_path is not None:
        relay = load_json_file(relay_index_path, "relay index")
        if relay.get("schema_version") != "claude-code-fw-e-relay-index/v1":
            raise FWEEvidenceError("relay index schema 不匹配")
        if relay.get("result") != "passed":
            raise FWEEvidenceError("relay R 证据未通过")
        if relay.get("capture_source_sha256") != control["capture_source_sha256"]:
            raise FWEEvidenceError("relay 没有绑定同一抓包执行源")
        channels.add("R")
    result = {
        "schema_version": SCHEMA_CAPTURE_INDEX,
        "baseline_version": BASELINE_VERSION,
        "target_version": target_version,
        "capture_source_sha256": control["capture_source_sha256"],
        "channels": sorted(channels),
        "control": control,
        "target": target,
        "relay": relay,
        "network_inventory": {
            "host_prefilter_disabled": True,
            "control_observation_count": len(control["network_observations"]),
            "target_observation_count": len(target["network_observations"]),
            "result": "passed",
        },
        "result": "passed",
        "producer": {
            "path": "tools/official_client_capture/claude_fw_e.py",
            "sha256": sha256_file(Path(__file__)),
        },
        "generated_at_utc": _utc_now(),
    }
    _write_private_json(output_path, result)
    return result


def _matrix_target_rules(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    """读取四方矩阵派生的目标规则，不把旧版本条数当作目标上限。"""

    rows = matrix.get("target_rules")
    if not isinstance(rows, list) or not rows:
        raise FWEEvidenceError("四方矩阵缺少 target_rules")
    identities = [str(row.get("id")) for row in rows if isinstance(row, dict)]
    if len(identities) != len(rows) or len(set(identities)) != len(identities):
        raise FWEEvidenceError("四方矩阵 target rule 身份缺失或重复")
    for row in rows:
        if row.get("origin") not in {"historical_rule", "target_native_add"}:
            raise FWEEvidenceError(f"目标规则来源非法：{row.get('id')}")
        if not isinstance(row.get("required_channels"), list):
            raise FWEEvidenceError(f"目标规则 required_channels 非法：{row.get('id')}")
    return sorted(rows, key=lambda item: str(item["id"]))


def _required_channels(rule: dict[str, Any]) -> set[str]:
    channels = rule.get("required_channels")
    if not isinstance(channels, list):
        return set()
    return {str(item) for item in channels}


def _baseline_disposition(rule: dict[str, Any]) -> str | None:
    value = rule.get("baseline_disposition")
    if rule.get("origin") == "target_native_add" and value is None:
        return None
    if not isinstance(value, str):
        raise FWEEvidenceError(f"规则缺少 disposition：{rule.get('id')}")
    return value


def _load_overrides(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    value = load_json_file(path, "FW-E rule overrides")
    if not isinstance(value, dict) or value.get("schema_version") != "claude-code-fw-e-rule-overrides/v1":
        raise FWEEvidenceError("rule overrides schema 不匹配")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise FWEEvidenceError("rule overrides entries 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("spec_id"), str):
            raise FWEEvidenceError("rule override 非法")
        if entry["spec_id"] in result:
            raise FWEEvidenceError(f"rule override 重复：{entry['spec_id']}")
        result[entry["spec_id"]] = entry
    return result


def build_rule_assessments(
    workspace_root: Path,
    cross_source_matrix_path: Path,
    completeness_closure_path: Path,
    static_diff_path: Path,
    capture_index_path: Path,
    output_root: Path,
    overrides_path: Path | None,
) -> dict[str, Any]:
    if output_root.exists():
        raise FWEEvidenceError("rule assessment output_root 必须不存在")
    output_root.mkdir(parents=True, mode=0o700)
    output_root.chmod(0o700)
    matrix = load_json_file(cross_source_matrix_path, "FW-E cross-source matrix")
    closure = load_json_file(completeness_closure_path, "FW-E completeness closure")
    static_diff = load_json_file(static_diff_path, "FW-E static diff")
    capture_index = load_json_file(capture_index_path, "FW-E capture index")
    if matrix.get("schema_version") != SCHEMA_MATRIX:
        raise FWEEvidenceError("cross-source matrix schema 不匹配")
    if closure.get("schema_version") != SCHEMA_CLOSURE:
        raise FWEEvidenceError("completeness closure schema 不匹配")
    if (
        closure.get("result") != "passed"
        or closure.get("unresolved_total") != 0
        or closure.get("matrix_sha256") != canonical_sha256(matrix)
    ):
        raise FWEEvidenceError("目标 sink／历史候选四方闭集未通过")
    if static_diff.get("schema_version") != SCHEMA_STATIC:
        raise FWEEvidenceError("static diff schema 不匹配")
    if capture_index.get("schema_version") != SCHEMA_CAPTURE_INDEX or capture_index.get("result") != "passed":
        raise FWEEvidenceError("capture index 未通过")
    channels = {str(item) for item in capture_index.get("channels", [])}
    overrides = _load_overrides(overrides_path)
    target_rows = _matrix_target_rules(matrix)
    versions = {
        str(matrix.get("target_version")),
        str(closure.get("target_version")),
        str(static_diff.get("target_version")),
        str(capture_index.get("target_version")),
    }
    if len(versions) != 1:
        raise FWEEvidenceError(f"FW-E 目标版本绑定不一致：{sorted(versions)}")
    known_ids = {str(row["id"]) for row in target_rows}
    unknown_overrides = sorted(set(overrides) - known_ids)
    if unknown_overrides:
        raise FWEEvidenceError(f"rule overrides 含未知规则：{unknown_overrides}")
    common_bindings = [
        {
            "role": "cross_source_matrix",
            "path": cross_source_matrix_path.relative_to(workspace_root).as_posix(),
            "sha256": sha256_file(cross_source_matrix_path),
        },
        {
            "role": "completeness_closure",
            "path": completeness_closure_path.relative_to(workspace_root).as_posix(),
            "sha256": sha256_file(completeness_closure_path),
        },
        {
            "role": "target_static",
            "path": static_diff_path.relative_to(workspace_root).as_posix(),
            "sha256": sha256_file(static_diff_path),
        },
        {
            "role": "target_runtime",
            "path": capture_index_path.relative_to(workspace_root).as_posix(),
            "sha256": sha256_file(capture_index_path),
        },
    ]
    assessment_rows: list[dict[str, Any]] = []
    for rule in target_rows:
        spec_id = str(rule["id"])
        baseline_disposition = _baseline_disposition(rule)
        required = _required_channels(rule)
        channels_complete = required.issubset(channels)
        if rule["origin"] == "target_native_add":
            decision = "add"
            basis = "new_target_rule"
            lifecycle = "candidate"
            evidence_level = "observed" if channels_complete else "blocked"
        elif baseline_disposition == "superseded":
            decision = "delete"
            basis = "removed_target_rule"
            lifecycle = "superseded"
            evidence_level = "observed" if channels_complete else "blocked"
        else:
            decision = "change"
            basis = "inheritance_not_proven"
            lifecycle = "candidate"
            if not channels_complete:
                evidence_level = "blocked"
            elif baseline_disposition == "verified":
                evidence_level = "regressed_evidence"
            else:
                evidence_level = "observed"
        compatibility = (
            "response_compat"
            if baseline_disposition == "response_compat"
            else "request_egress"
        )
        override = overrides.get(spec_id)
        if override is not None:
            allowed_keys = {
                "spec_id",
                "migration_decision",
                "decision_basis",
                "semantic_equivalence_proven",
                "evidence_level",
                "rationale",
            }
            if set(override) != allowed_keys:
                raise FWEEvidenceError(f"{spec_id} override 字段不闭合")
            decision = str(override["migration_decision"])
            basis = str(override["decision_basis"])
            evidence_level = str(override["evidence_level"])
            equivalence = override["semantic_equivalence_proven"]
            rationale = str(override["rationale"])
        else:
            equivalence = False
            if decision == "change":
                rationale = (
                    "目标 stable 已完成 P/R/J/M 与静态差分，但尚未形成足以安全继承"
                    "该规则全部语义、条件和 sink 依赖的证明，因此按 change 重新派生。"
                )
            elif decision == "add":
                rationale = "目标原生 sink 没有历史规则承接，按四方矩阵新增原子规则。"
            else:
                rationale = "历史规则已被后继原子规则取代，目标台账保持删除结论。"
        if decision == "inherit" and not equivalence:
            raise FWEEvidenceError(f"{spec_id} 未证明语义等价，禁止 override 为 inherit")
        if rule["origin"] == "target_native_add" and decision != "add":
            raise FWEEvidenceError(f"{spec_id} 是目标原生新增规则，迁移决策必须为 add")
        evidence_document = {
            "schema_version": "claude-code-fw-e-rule-evidence/v1",
            "spec_id": spec_id,
            "baseline_version": BASELINE_VERSION,
            "target_version": static_diff["target_version"],
            "baseline_disposition": baseline_disposition,
            "rule_origin": rule["origin"],
            "required_channels": sorted(required),
            "available_channels": sorted(channels),
            "required_channels_present": channels_complete,
            "migration_decision": decision,
            "decision_basis": basis,
            "semantic_equivalence_proven": equivalence,
            "evidence_level": evidence_level,
            "rationale": rationale,
            "source_bindings": common_bindings,
            "retained_claim": rule.get("retained_claim"),
            "scope": rule.get("scope"),
        }
        evidence_path = output_root / "rules" / f"{spec_id}.json"
        _write_private_json(evidence_path, evidence_document)
        evidence_relative = evidence_path.relative_to(workspace_root).as_posix()
        assessment_rows.append(
            {
                "spec_id": spec_id,
                "evidence_level": evidence_level,
                "rule_lifecycle": lifecycle,
                "compatibility_class": compatibility,
                "migration_decision": decision,
                "decision_basis": basis,
                "semantic_equivalence_proven": equivalence,
                "evidence_paths": [evidence_relative],
                "applicability": [
                    "authentication=claude.ai-oauth",
                    "entrypoint=sdk-cli",
                    "model=claude-sonnet-5",
                    "platform=linux/amd64",
                    "privacy=essential-traffic",
                    "provider=firstParty",
                ],
            }
        )
    result = {
        "schema_version": SCHEMA_RULE_ASSESSMENTS,
        "baseline_version": BASELINE_VERSION,
        "target_version": matrix["target_version"],
        "rule_count": len(assessment_rows),
        "inherit_count": sum(
            row["migration_decision"] == "inherit" for row in assessment_rows
        ),
        "regressed_evidence_count": sum(
            row["evidence_level"] == "regressed_evidence" for row in assessment_rows
        ),
        "blocked_count": sum(row["evidence_level"] == "blocked" for row in assessment_rows),
        "rules": assessment_rows,
        "generated_at_utc": _utc_now(),
    }
    _write_private_json(output_root / "rule-assessments.json", result)
    return result


def seal_plan(store_root: Path, workspace_root: Path, plan_path: Path) -> dict[str, Any]:
    plan = load_json_file(plan_path, "FW-E seal plan")
    store = ControlStore(store_root)
    return seal_fw_e_plan(store, workspace_root, plan)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze", help="查询并冻结当前官方 stable")
    freeze.add_argument("--output-root", type=Path, required=True)

    static = commands.add_parser("analyze-bundles", help="提取目标 bundle 并与 2.1.220 比较")
    static.add_argument("--freeze-root", type=Path, required=True)
    static.add_argument("--output-root", type=Path, required=True)
    static.add_argument("--baseline-anchors", type=Path, required=True)
    static.add_argument("--baseline-reachability", type=Path, required=True)
    static.add_argument("--node-binary", default="node")
    static.add_argument("--typescript-module", type=Path)

    capture = commands.add_parser("capture-index", help="复核控制组和目标组完整 M")
    capture.add_argument("--control-root", type=Path, required=True)
    capture.add_argument("--target-root", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--target-version", required=True)
    capture.add_argument("--control-binary-sha256", required=True)
    capture.add_argument("--target-binary-sha256", required=True)
    capture.add_argument("--relay-index", type=Path)

    rules = commands.add_parser("rule-assessments", help="由闭合四方矩阵生成目标规则台账")
    rules.add_argument("--workspace-root", type=Path, required=True)
    rules.add_argument("--cross-source-matrix", type=Path, required=True)
    rules.add_argument("--completeness-closure", type=Path, required=True)
    rules.add_argument("--static-diff", type=Path, required=True)
    rules.add_argument("--capture-index", type=Path, required=True)
    rules.add_argument("--output-root", type=Path, required=True)
    rules.add_argument("--overrides", type=Path)

    seal = commands.add_parser("seal", help="把 FW-E 当前事实封存到 FW-D Store")
    seal.add_argument("--store", type=Path, required=True)
    seal.add_argument("--workspace-root", type=Path, required=True)
    seal.add_argument("--plan", type=Path, required=True)
    return parser


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.command == "freeze":
        return freeze_official_stable(arguments.output_root)
    if arguments.command == "analyze-bundles":
        return analyze_bundles(
            arguments.freeze_root,
            arguments.output_root,
            arguments.baseline_anchors,
            arguments.baseline_reachability,
            arguments.node_binary,
            arguments.typescript_module,
        )
    if arguments.command == "capture-index":
        return build_capture_index(
            arguments.control_root,
            arguments.target_root,
            arguments.output,
            arguments.target_version,
            arguments.control_binary_sha256,
            arguments.target_binary_sha256,
            arguments.relay_index,
        )
    if arguments.command == "rule-assessments":
        return build_rule_assessments(
            arguments.workspace_root,
            arguments.cross_source_matrix,
            arguments.completeness_closure,
            arguments.static_diff,
            arguments.capture_index,
            arguments.output_root,
            arguments.overrides,
        )
    if arguments.command == "seal":
        return seal_plan(arguments.store, arguments.workspace_root, arguments.plan)
    raise FWEEvidenceError(f"未处理命令：{arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        result = execute(_build_parser().parse_args(argv))
    except (
        FWEEvidenceError,
        TargetInventoryError,
        ControlError,
        OSError,
        ValueError,
    ) as error:
        print(f"Claude FW-E 拒绝：{error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
