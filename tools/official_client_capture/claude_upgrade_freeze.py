#!/usr/bin/env python3
"""冻结 Claude Code 官方制品：npm 渠道 stable 精确版本 + 原生分发（GCS）同版本双来源（Claude 升级控制面设计 B2-01）。

离线工具，不依赖采集容器里的二进制，不发送任何模型请求，只读取公开发布元数据与制品：

1. **版本选择**：只取 npm 主包 ``dist-tags.<dist_tag>``（默认 ``stable``）的精确三段版本；原生分发的 ``latest``／
   ``stable`` 端点只记录用于核对，不参与选版本。冻结结束时复查 npm 标签，期间漂移即作废本轮。
2. **npm 渠道**：沿用 FW-E 冻结的下载与校验（``claude_fw_e`` 的 sha512 SRI、sha1、``ecdsa-sha2-nistp256`` Registry
   签名、主包 ``optionalDependencies`` 精确绑定平台包），平台参数化（``linux/arm64`` → ``@anthropic-ai/claude-code-linux-arm64``），
   从平台包解出 ``package/claude``。
3. **原生分发渠道**：``<base>/<同一精确版本>/manifest.json`` 取 ``platforms["<平台键>"].{checksum, size}`` 与
   ``commit``、``modsCommit``、``buildDate``、``manifestSignatureEnforcement``，下载 ``<base>/<版本>/<平台键>/claude``，
   核对 sha256 与字节数。
4. **ELF 判定**：两份二进制都必须是目标架构的 ELF64，按 ``PT_INTERP`` 区分 glibc 与 musl；``linux/arm64`` 要求 glibc。
5. **等价性**：两渠道二进制逐字节比较。相同则登记一个 ``binary_sha256``（只存一份）并绑定两渠道证明；不同则两份都存、
   ``binary_equivalence`` 写 ``divergent``、``binary_sha256`` 为 null——VC-0 不得据此建 Formal Campaign。

产物写入一个此前不存在的目录（先写同级暂存目录，完成后整体改名，失败即删除暂存），``freeze.json`` 合同为
``claude-code-fw-e-official-freeze/v2``。``claude_fw_e.py freeze``（v1，x86_64／darwin 两平台）只读保留，不修改。
Campaign 层面的条件（entrypoint、隐私模式、账号与模型）由 VC-0 总计划冻结，不写进本收据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_fw_e

FREEZE_SCHEMA = "claude-code-fw-e-official-freeze/v2"
REGISTRY_SNAPSHOT_SCHEMA = "claude-code-fw-e-registry-snapshot/v2"
MAIN_PACKAGE = claude_fw_e.MAIN_PACKAGE
PACKAGE_BINARY_MEMBER = claude_fw_e.PACKAGE_BINARY_MEMBER
NATIVE_BASE_URL = "https://downloads.claude.ai/claude-code-releases"
NATIVE_CHANNEL_ENDPOINTS = ("latest", "stable")
USER_AGENT = "sub2apiplus-claude-upgrade-freeze/1"
# 每个可冻结平台：npm 平台包、原生分发平台键、ELF 机器号与 glibc 解释器。只登记已验证的组合，未登记平台即拒绝。
PLATFORMS: Mapping[str, Mapping[str, Any]] = {
    "linux/arm64": {
        "npm_package": "@anthropic-ai/claude-code-linux-arm64",
        "native_key": "linux-arm64",
        "elf_machine": 183,  # EM_AARCH64
        "elf_machine_name": "aarch64",
        "glibc_interpreter": "/lib/ld-linux-aarch64.so.1",
        "musl_interpreter": "/lib/ld-musl-aarch64.so.1",
    },
}
# 单次下载的大小上限：当前 linux-arm64 二进制约 240 MB，按十倍留余量；超过即视为异常响应。
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
ELF_MAGIC = b"\x7fELF"
PT_INTERP = 3


class FreezeError(RuntimeError):
    """冻结输入不闭合、官方身份不可验证或两渠道不一致到无法登记。"""


# --------------------------------------------------------------------------- 网络读取


def _http_get(url: str, *, accept: str) -> tuple[bytes, dict[str, str]]:
    """GET 一个公开发布地址，返回正文与少量可审计的响应头；非 200、超限或网络错误即失败。

    测试以替身替换本函数（npm 与原生分发两渠道都经它读取），不触网。
    """

    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise FreezeError(f"官方发布地址返回状态 {response.status}：{url}")
            content = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise FreezeError(f"官方发布地址响应超过大小上限：{url}")
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "date", "etag", "last-modified"}
            }
            return content, headers
    except (OSError, urllib.error.URLError) as error:
        raise FreezeError(f"无法读取官方发布地址：{url}") from error


def _get_json(url: str) -> tuple[dict[str, Any], bytes, dict[str, str]]:
    raw, headers = _http_get(url, accept="application/json")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FreezeError(f"官方发布地址返回的 JSON 非法：{url}") from error
    if not isinstance(value, dict):
        raise FreezeError(f"官方发布地址返回的 JSON 顶层不是对象：{url}")
    return value, raw, headers


def _response_receipt(raw: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
    return {"response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw), "headers": dict(headers)}


# --------------------------------------------------------------------------- ELF 判定


def elf_identity(binary: bytes, platform: str) -> dict[str, Any]:
    """读取 ELF 头与 ``PT_INTERP``，证明二进制属于目标平台并区分 glibc／musl。

    只解析判定所需的固定字段：ELF64、小端、机器号等于平台登记值；动态链接器路径决定 libc（等于 glibc 解释器为
    ``glibc``，等于 musl 解释器为 ``musl``，没有 ``PT_INTERP`` 为 ``static``，其它值为 ``unknown``）。
    """

    spec = PLATFORMS[platform]
    if len(binary) < 64 or binary[:4] != ELF_MAGIC:
        raise FreezeError(f"{platform} 二进制不是 ELF 文件")
    if binary[4] != 2 or binary[5] != 1:
        raise FreezeError(f"{platform} 二进制不是 64 位小端 ELF")
    machine = struct.unpack_from("<H", binary, 18)[0]
    if machine != spec["elf_machine"]:
        raise FreezeError(f"{platform} 二进制的 ELF 机器号 {machine} 不是 {spec['elf_machine_name']}")
    phoff = struct.unpack_from("<Q", binary, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", binary, 54)
    if phentsize < 56 or phoff + phentsize * phnum > len(binary):
        raise FreezeError(f"{platform} 二进制的 ELF 程序头越界")
    interpreter: str | None = None
    for index in range(phnum):
        base = phoff + index * phentsize
        if struct.unpack_from("<I", binary, base)[0] != PT_INTERP:
            continue
        offset = struct.unpack_from("<Q", binary, base + 8)[0]
        size = struct.unpack_from("<Q", binary, base + 32)[0]
        if size == 0 or offset + size > len(binary):
            raise FreezeError(f"{platform} 二进制的 PT_INTERP 越界")
        raw = binary[offset : offset + size].split(b"\0", 1)[0]
        try:
            interpreter = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise FreezeError(f"{platform} 二进制的动态链接器路径不是 ASCII") from error
        break
    if interpreter is None:
        libc = "static"
    elif interpreter == spec["glibc_interpreter"]:
        libc = "glibc"
    elif interpreter == spec["musl_interpreter"]:
        libc = "musl"
    else:
        libc = "unknown"
    return {"class": "ELF64", "machine": spec["elf_machine_name"], "interpreter": interpreter, "libc": libc}


def _require_glibc(identity: Mapping[str, Any], platform: str, channel: str) -> None:
    if identity["libc"] != "glibc":
        raise FreezeError(
            f"{channel} 渠道的 {platform} 二进制不是 glibc 构建（libc={identity['libc']}，"
            f"interpreter={identity['interpreter']}）：可能取到了 musl 或静态变体"
        )


# --------------------------------------------------------------------------- npm 渠道


def _npm_channel(platform: str, dist_tag: str, staging: Path) -> dict[str, Any]:
    """npm 渠道：选版本、核对主包与平台包、下载校验两个 tarball 并解出平台二进制。"""

    spec = PLATFORMS[platform]
    platform_package = spec["npm_package"]
    main_packument, main_raw, main_headers = _get_json(claude_fw_e._package_url(MAIN_PACKAGE))
    tags = main_packument.get("dist-tags")
    version = tags.get(dist_tag) if isinstance(tags, dict) else None
    if not isinstance(version, str) or not claude_fw_e.VERSION_RE.fullmatch(version):
        raise FreezeError(f"npm dist-tags.{dist_tag} 不是精确三段版本：{version!r}")
    keys, keys_raw, keys_headers = _get_json(claude_fw_e.REGISTRY_KEYS_URL)
    platform_packument, platform_raw, platform_headers = _get_json(claude_fw_e._package_url(platform_package))
    try:
        selected = {
            MAIN_PACKAGE: claude_fw_e._selected_version_metadata(main_packument, MAIN_PACKAGE, version),
            platform_package: claude_fw_e._selected_version_metadata(platform_packument, platform_package, version),
        }
    except claude_fw_e.FWEEvidenceError as error:
        raise FreezeError(str(error)) from error
    optional = selected[MAIN_PACKAGE].get("optionalDependencies")
    if not isinstance(optional, dict) or optional.get(platform_package) != version:
        raise FreezeError(f"npm 主包没有把 {platform_package} 精确绑定到 {version}")

    artifacts: list[dict[str, Any]] = []
    binary: bytes | None = None
    for package in (MAIN_PACKAGE, platform_package):
        dist = selected[package]["dist"]
        tarball, download_headers = _http_get(str(dist["tarball"]), accept="application/octet-stream")
        try:
            claude_fw_e._verify_integrity(tarball, str(dist["integrity"]), str(dist["shasum"]))
            signatures = claude_fw_e._verify_signatures(
                package, version, str(dist["integrity"]), dist["signatures"], keys
            )
            package_json, extracted = claude_fw_e._package_identity_and_binary(
                tarball, package, version, require_binary=package == platform_package
            )
        except claude_fw_e.FWEEvidenceError as error:
            raise FreezeError(str(error)) from error
        relative = f"official/{claude_fw_e._safe_package_name(package)}-{version}.tgz"
        claude_fw_e._write_private_bytes(staging / relative, tarball)
        artifacts.append(
            {
                "package": package,
                "version": version,
                "tarball_url": dist["tarball"],
                "integrity": dist["integrity"],
                "shasum": dist["shasum"],
                "tarball_path": relative,
                "tarball_sha256": hashlib.sha256(tarball).hexdigest(),
                "tarball_bytes": len(tarball),
                "download_headers": download_headers,
                "registry_signatures": signatures,
                "package_json_sha256": hashlib.sha256(claude_fw_e.canonical_json_bytes(package_json)).hexdigest(),
            }
        )
        if extracted is not None:
            binary = extracted
    assert binary is not None  # 平台包必有二进制，由 _package_identity_and_binary 保证
    return {
        "version": version,
        "dist_tags_at_start": {key: tags[key] for key in sorted(tags) if isinstance(tags.get(key), str)},
        "platform_package": platform_package,
        "artifacts": sorted(artifacts, key=lambda item: item["tarball_path"]),
        "binary": binary,
        "packages": [claude_fw_e._minimal_registry_metadata(selected[name]) for name in (MAIN_PACKAGE, platform_package)],
        "packument_receipts": {
            MAIN_PACKAGE: _response_receipt(main_raw, main_headers),
            platform_package: _response_receipt(platform_raw, platform_headers),
        },
        "registry_keys_receipt": {**_response_receipt(keys_raw, keys_headers), "keys": keys.get("keys")},
    }


def _npm_dist_tag_at_end(dist_tag: str) -> tuple[str | None, dict[str, Any]]:
    packument, raw, headers = _get_json(claude_fw_e._package_url(MAIN_PACKAGE))
    tags = packument.get("dist-tags")
    value = tags.get(dist_tag) if isinstance(tags, dict) else None
    return (value if isinstance(value, str) else None), _response_receipt(raw, headers)


# --------------------------------------------------------------------------- 原生分发渠道


def _native_channel(platform: str, version: str, staging: Path, base_url: str) -> dict[str, Any]:
    """原生分发渠道：读同一精确版本的 manifest，下载并核对该平台二进制；记录 latest／stable 端点仅供核对。"""

    spec = PLATFORMS[platform]
    native_key = spec["native_key"]
    manifest_url = f"{base_url}/{urllib.parse.quote(version)}/manifest.json"
    manifest, manifest_raw, manifest_headers = _get_json(manifest_url)
    if manifest.get("version") != version:
        raise FreezeError(f"原生分发 manifest 的 version 为 {manifest.get('version')!r}，不是 {version}")
    platforms = manifest.get("platforms")
    entry = platforms.get(native_key) if isinstance(platforms, dict) else None
    if not isinstance(entry, dict):
        raise FreezeError(f"原生分发 manifest 缺少平台 {native_key}")
    checksum = entry.get("checksum")
    size = entry.get("size")
    if not isinstance(checksum, str) or not claude_fw_e.SHA256_RE.fullmatch(checksum):
        raise FreezeError(f"原生分发 manifest 的 {native_key}.checksum 不是 sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise FreezeError(f"原生分发 manifest 的 {native_key}.size 非法")
    member = entry.get("binary", "claude")
    if member != "claude":
        raise FreezeError(f"原生分发 manifest 的 {native_key}.binary 不是 claude：{member!r}")
    binary_url = f"{base_url}/{urllib.parse.quote(version)}/{native_key}/claude"
    binary, binary_headers = _http_get(binary_url, accept="application/octet-stream")
    if len(binary) != size:
        raise FreezeError(f"原生分发二进制字节数 {len(binary)} 与 manifest 登记的 {size} 不一致")
    digest = hashlib.sha256(binary).hexdigest()
    if digest != checksum:
        raise FreezeError("原生分发二进制的 sha256 与 manifest 登记的 checksum 不一致")
    manifest_relative = "native/manifest.json"
    claude_fw_e._write_private_bytes(staging / manifest_relative, manifest_raw)
    channels: dict[str, Any] = {}
    for endpoint in NATIVE_CHANNEL_ENDPOINTS:
        raw, _headers = _http_get(f"{base_url}/{endpoint}", accept="text/plain")
        text = raw.decode("utf-8", errors="replace").strip()
        channels[endpoint] = text if claude_fw_e.VERSION_RE.fullmatch(text) else None
    return {
        "base_url": base_url,
        "manifest_url": manifest_url,
        "manifest_path": manifest_relative,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "manifest_headers": manifest_headers,
        "version": version,
        "commit": manifest.get("commit"),
        "mods_commit": manifest.get("modsCommit"),
        "build_date": manifest.get("buildDate"),
        "manifest_signature_enforcement": manifest.get("manifestSignatureEnforcement"),
        "platform_key": native_key,
        "checksum": checksum,
        "size": size,
        "binary_url": binary_url,
        "binary_headers": binary_headers,
        "channel_versions": channels,
        "binary": binary,
    }


# --------------------------------------------------------------------------- 冻结主流程


def _binary_row(staging: Path, relative: str, binary: bytes, identity: Mapping[str, Any]) -> dict[str, Any]:
    claude_fw_e._write_private_bytes(staging / relative, binary, executable=True)
    return {"path": relative, "sha256": hashlib.sha256(binary).hexdigest(), "bytes": len(binary), "elf": dict(identity)}


def freeze_official_release(
    output_root: Path,
    *,
    dist_tag: str = "stable",
    platform: str = "linux/arm64",
    native_base_url: str = NATIVE_BASE_URL,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """冻结目标平台的官方制品，返回写入的 ``freeze.json`` 内容。

    ``output_root`` 必须是不存在的绝对路径；其父目录按 0700 创建。全部核对通过后才把暂存目录改名为
    ``output_root``，中途任何失败都删除暂存、不留半成品。
    """

    now = clock or claude_fw_e._utc_now
    if platform not in PLATFORMS:
        raise FreezeError(f"不支持的冻结平台：{platform}（已登记：{sorted(PLATFORMS)}）")
    if not dist_tag or not all(character.isalnum() or character in "-._" for character in dist_tag):
        raise FreezeError(f"npm dist-tag 非法：{dist_tag!r}")
    if not output_root.is_absolute():
        raise FreezeError("冻结输出目录必须是绝对路径")
    if output_root.exists() or output_root.is_symlink():
        raise FreezeError(f"冻结输出目录已存在，禁止覆盖或续写：{output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.staging-", dir=output_root.parent))
    staging.chmod(0o700)
    try:
        started_at = now()
        npm = _npm_channel(platform, dist_tag, staging)
        version = npm["version"]
        native = _native_channel(platform, version, staging, native_base_url.rstrip("/"))
        npm_identity = elf_identity(npm["binary"], platform)
        native_identity = elf_identity(native["binary"], platform)
        _require_glibc(npm_identity, platform, "npm")
        _require_glibc(native_identity, platform, "原生分发")
        dashed = platform.replace("/", "-")
        if npm["binary"] == native["binary"]:
            equivalence = "npm=gcs"
            binary_row = _binary_row(staging, f"binaries/{dashed}/claude", npm["binary"], npm_identity)
            binaries = {"npm": binary_row, "gcs": binary_row}
            binary_sha256: str | None = binary_row["sha256"]
        else:
            equivalence = "divergent"
            binaries = {
                "npm": _binary_row(staging, f"binaries/{dashed}/npm/claude", npm["binary"], npm_identity),
                "gcs": _binary_row(staging, f"binaries/{dashed}/gcs/claude", native["binary"], native_identity),
            }
            binary_sha256 = None
        tag_at_end, end_receipt = _npm_dist_tag_at_end(dist_tag)
        if tag_at_end != version:
            raise FreezeError(f"冻结期间 npm dist-tags.{dist_tag} 从 {version} 变为 {tag_at_end}，本轮必须作废")
        snapshot = {
            "schema_version": REGISTRY_SNAPSHOT_SCHEMA,
            "registry": claude_fw_e.REGISTRY_BASE,
            "queried_at_start_utc": started_at,
            "queried_at_end_utc": now(),
            "dist_tag": dist_tag,
            "dist_tags_at_start": npm["dist_tags_at_start"],
            "dist_tag_at_end": tag_at_end,
            "packages": npm["packages"],
            "packument_receipts": npm["packument_receipts"],
            "end_packument_receipt": end_receipt,
            "registry_keys_receipt": npm["registry_keys_receipt"],
        }
        claude_fw_e._write_private_json(staging / "registry-snapshot.json", snapshot)
        gcs = {key: value for key, value in native.items() if key != "binary"}
        gcs["binary_path"] = binaries["gcs"]["path"]
        freeze = {
            "schema_version": FREEZE_SCHEMA,
            "target_version": version,
            "dist_tag": dist_tag,
            "dist_tag_at_start": version,
            "dist_tag_at_end": tag_at_end,
            "platforms": [platform],
            "registry_snapshot_path": "registry-snapshot.json",
            "registry_snapshot_sha256": claude_fw_e.sha256_file(staging / "registry-snapshot.json"),
            "npm": {
                "platform_package": npm["platform_package"],
                "artifacts": npm["artifacts"],
                "binary_member": PACKAGE_BINARY_MEMBER,
                "binary_path": binaries["npm"]["path"],
            },
            "gcs": gcs,
            "binaries": {platform: binaries},
            "binary_equivalence": equivalence,
            "binary_sha256": binary_sha256,
            "producer": {
                "path": "tools/official_client_capture/claude_upgrade_freeze.py",
                "sha256": claude_fw_e.sha256_file(Path(__file__)),
            },
            "freeze_completed_at_utc": now(),
        }
        claude_fw_e._write_private_json(staging / "freeze.json", freeze)
        os.rename(staging, output_root)
        return freeze
    except BaseException:
        if staging.exists() and staging.is_dir() and staging.parent == output_root.parent:
            shutil.rmtree(staging)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-root", type=Path, required=True, help="冻结输出目录（绝对路径，必须不存在）")
    parser.add_argument("--dist-tag", default="stable", help="npm 主包的 dist-tag，默认 stable")
    parser.add_argument("--platform", default="linux/arm64", choices=sorted(PLATFORMS), help="冻结平台")
    return parser


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    """命令行与 ``claude_upgrade.py freeze`` 共用：冻结并返回摘要（不含大字段）。"""

    freeze = freeze_official_release(
        arguments.output_root, dist_tag=arguments.dist_tag, platform=arguments.platform
    )
    return {
        "output_root": str(arguments.output_root),
        "target_version": freeze["target_version"],
        "dist_tag": freeze["dist_tag"],
        "platforms": freeze["platforms"],
        "binary_equivalence": freeze["binary_equivalence"],
        "binary_sha256": freeze["binary_sha256"],
        "freeze_sha256": claude_fw_e.sha256_file(arguments.output_root / "freeze.json"),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = run(arguments)
    except FreezeError as error:
        print(f"官方制品冻结失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["binary_equivalence"] == "npm=gcs" else 2


if __name__ == "__main__":
    raise SystemExit(main())
