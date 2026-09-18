#!/usr/bin/env python3
"""为不可变 Codex 升级证据生成可续作、只扫描一次的内容清单。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tools.official_client_capture.candidate_evidence_guard import (
    HEURISTIC_PATTERNS,
    SCAN_CHUNK_SIZE,
    SCAN_OVERLAP,
)
from tools.official_client_capture.capturelib.security import (
    canonical_json_sha256,
    secure_write_json,
)


MANIFEST_SCHEMA = "codex-upgrade-evidence-manifest/v1"
CHECKPOINT_SCHEMA = "codex-upgrade-evidence-manifest-checkpoint/v2"


class EvidenceManifestError(ValueError):
    """证据边界、权限、检查点或内容摘要不可信。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _root_map(roots: Iterable[Path]) -> list[tuple[Path, str]]:
    resolved = sorted({path.resolve(strict=False) for path in roots})
    counts: dict[str, int] = {}
    for root in resolved:
        counts[root.name] = counts.get(root.name, 0) + 1
    return [
        (
            root,
            root.name if counts[root.name] == 1 else f"{index:03d}-{root.name}",
        )
        for index, root in enumerate(resolved, 1)
    ]


def _stat_boundary(path: Path) -> dict[str, int]:
    value = path.stat(follow_symlinks=False)
    return {
        "mode": stat.S_IMODE(value.st_mode),
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "nlink": value.st_nlink,
    }


def _require_private_mode(path: Path, value: os.stat_result, *, directory: bool) -> None:
    if value.st_mode & 0o077:
        kind = "目录" if directory else "文件"
        raise EvidenceManifestError(
            f"证据{kind}向 group/other 开放，必须先修正权限：{path}"
        )


def preflight_evidence_roots(roots: Iterable[Path]) -> dict[str, Any]:
    """只读取目录项和 stat，先拒绝不可信路径与权限，不读取文件内容。"""

    raw_roots = list(roots)
    if not raw_roots:
        raise EvidenceManifestError("证据根不能为空。")
    for root in raw_roots:
        if not root.is_absolute():
            raise EvidenceManifestError(f"证据根必须是绝对路径：{root}")
        if root.is_symlink() or not root.exists():
            raise EvidenceManifestError(
                f"证据根必须是存在的非符号链接绝对路径：{root}"
            )
    root_entries: list[dict[str, Any]] = []
    file_entries: list[dict[str, Any]] = []
    seen_resolved: set[Path] = set()
    for root, prefix in _root_map(raw_roots):
        if root.is_symlink() or not root.exists():
            raise EvidenceManifestError(f"证据根必须是存在的非符号链接绝对路径：{root}")
        root_stat = root.stat(follow_symlinks=False)
        if not (stat.S_ISDIR(root_stat.st_mode) or stat.S_ISREG(root_stat.st_mode)):
            raise EvidenceManifestError(f"证据根不是普通文件或目录：{root}")
        _require_private_mode(
            root,
            root_stat,
            directory=stat.S_ISDIR(root_stat.st_mode),
        )
        root_entries.append(
            {
                "path": str(root),
                "prefix": prefix,
                **_stat_boundary(root),
            }
        )
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            value = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(value.st_mode):
                raise EvidenceManifestError(f"证据边界包含符号链接：{path}")
            if stat.S_ISDIR(value.st_mode):
                _require_private_mode(path, value, directory=True)
                continue
            if not stat.S_ISREG(value.st_mode):
                raise EvidenceManifestError(f"证据边界包含非普通文件：{path}")
            _require_private_mode(path, value, directory=False)
            resolved = path.resolve(strict=True)
            if resolved in seen_resolved:
                continue
            seen_resolved.add(resolved)
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            file_entries.append(
                {
                    "path": f"{prefix}/{relative}",
                    "absolute_path": str(resolved),
                    **_stat_boundary(path),
                }
            )
    file_entries.sort(key=lambda item: item["path"])
    metadata_view = {
        "roots": root_entries,
        "entries": [
            {key: value for key, value in entry.items() if key != "absolute_path"}
            for entry in file_entries
        ],
    }
    return {
        "roots": root_entries,
        "entries": file_entries,
        "entry_count": len(file_entries),
        "total_bytes": sum(int(item["size"]) for item in file_entries),
        "metadata_sha256": canonical_json_sha256(metadata_view),
        "scanned_bytes": 0,
    }


def _known_secrets(secret_env_names: Sequence[str]) -> list[tuple[str, bytes]]:
    output: list[tuple[str, bytes]] = []
    for name in secret_env_names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EvidenceManifestError(f"秘密环境变量名非法：{name!r}")
        value = os.environ.get(name)
        if value is None:
            raise EvidenceManifestError(f"秘密环境变量未设置：{name}")
        encoded = value.encode("utf-8")
        if len(encoded) < 8:
            raise EvidenceManifestError(f"秘密环境变量 {name} 少于 8 字节。")
        output.append((name, encoded))
    return output


def _hash_and_scan(
    path: Path,
    logical_path: str,
    known_secrets: Sequence[tuple[str, bytes]],
) -> tuple[str, list[dict[str, Any]], int]:
    digest = hashlib.sha256()
    overlap_size = max(
        SCAN_OVERLAP,
        max((len(secret) - 1 for _, secret in known_secrets), default=0),
    )
    overlap = b""
    offset = 0
    scanned_bytes = 0
    findings: dict[tuple[str, str, int], dict[str, Any]] = {}
    with path.open("rb") as stream:
        while chunk := stream.read(SCAN_CHUNK_SIZE):
            digest.update(chunk)
            scanned_bytes += len(chunk)
            payload = overlap + chunk
            payload_base = offset - len(overlap)
            for name, secret in known_secrets:
                start = 0
                while True:
                    found = payload.find(secret, start)
                    if found < 0:
                        break
                    finding = {
                        "path": logical_path,
                        "rule": f"known-secret-env:{name}",
                        "offset": payload_base + found,
                    }
                    findings[(logical_path, finding["rule"], finding["offset"])] = finding
                    start = found + len(secret)
            for rule, pattern in HEURISTIC_PATTERNS:
                for match in pattern.finditer(payload):
                    finding = {
                        "path": logical_path,
                        "rule": rule,
                        "offset": payload_base + match.start(),
                    }
                    findings[(logical_path, rule, finding["offset"])] = finding
            overlap = payload[-overlap_size:]
            offset += len(chunk)
    return digest.hexdigest(), [findings[key] for key in sorted(findings)], scanned_bytes


def _checkpoint_digest(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("checkpoint_digest", None)
    return canonical_json_sha256(unsigned)


def _load_checkpoint(
    path: Path,
    metadata_sha256: str,
    secret_env_names: Sequence[str],
) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": CHECKPOINT_SCHEMA,
            "metadata_sha256": metadata_sha256,
            "secret_env_names": sorted(set(secret_env_names)),
            "completed": [],
        }
    if path.is_symlink() or not path.is_file():
        raise EvidenceManifestError(f"证据扫描 checkpoint 路径不可信：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceManifestError(f"证据扫描 checkpoint 不是合法 JSON：{error}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != CHECKPOINT_SCHEMA
        or payload.get("metadata_sha256") != metadata_sha256
        or payload.get("secret_env_names") != sorted(set(secret_env_names))
        or not isinstance(payload.get("completed"), list)
        or payload.get("checkpoint_digest") != _checkpoint_digest(payload)
    ):
        raise EvidenceManifestError("证据扫描 checkpoint 身份或摘要不一致。")
    return payload


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    value = dict(payload)
    value["checkpoint_digest"] = _checkpoint_digest(value)
    secure_write_json(path, value)


def build_evidence_manifest(
    roots: Iterable[Path],
    *,
    checkpoint_path: Path,
    secret_env_names: Sequence[str] = (),
) -> dict[str, Any]:
    """先做零内容前检，再按文件续作一次 hash 与秘密扫描。"""

    root_list = list(roots)
    preflight = preflight_evidence_roots(root_list)
    resolved_checkpoint = checkpoint_path.resolve(strict=False)
    if any(
        resolved_checkpoint == root.resolve(strict=False)
        or resolved_checkpoint.is_relative_to(root.resolve(strict=False))
        for root in root_list
    ):
        raise EvidenceManifestError("证据扫描 checkpoint 不得位于原始证据边界内。")
    known_secrets = _known_secrets(secret_env_names)
    checkpoint = _load_checkpoint(
        checkpoint_path,
        preflight["metadata_sha256"],
        secret_env_names,
    )
    completed = {
        str(item.get("path")): item
        for item in checkpoint["completed"]
        if isinstance(item, dict)
    }
    started_at = _utc_now()
    started = time.monotonic()
    scanned_bytes = 0
    reused_bytes = 0
    final_entries: list[dict[str, Any]] = []
    all_findings: list[dict[str, Any]] = []
    for metadata in preflight["entries"]:
        logical_path = str(metadata["path"])
        boundary = {
            key: value
            for key, value in metadata.items()
            if key != "absolute_path"
        }
        cached = completed.get(logical_path)
        if (
            isinstance(cached, dict)
            and cached.get("boundary") == boundary
            and re.fullmatch(r"[0-9a-f]{64}", str(cached.get("sha256", "")))
            and isinstance(cached.get("findings"), list)
        ):
            sha256 = str(cached["sha256"])
            findings = list(cached["findings"])
            reused_bytes += int(metadata["size"])
        else:
            sha256, findings, read_bytes = _hash_and_scan(
                Path(str(metadata["absolute_path"])),
                logical_path,
                known_secrets,
            )
            if read_bytes != int(metadata["size"]):
                raise EvidenceManifestError(f"扫描期间文件大小漂移：{logical_path}")
            scanned_bytes += read_bytes
            completed[logical_path] = {
                "path": logical_path,
                "boundary": boundary,
                "sha256": sha256,
                "findings": findings,
            }
            checkpoint["completed"] = [completed[key] for key in sorted(completed)]
            _write_checkpoint(checkpoint_path, checkpoint)
        final_entries.append({**boundary, "sha256": sha256})
        all_findings.extend(findings)

    # 内容读取完成后重新枚举并比较完整 stat 边界。这样即使文件在扫描期间被
    # 替换、改写或增删，也不会把不同时间点的内容与元数据拼成一个 manifest。
    # 该复核只读取目录项和 stat，不再次读取任何文件内容。
    final_preflight = preflight_evidence_roots(root_list)
    if (
        final_preflight["roots"] != preflight["roots"]
        or final_preflight["metadata_sha256"] != preflight["metadata_sha256"]
        or [
            {key: value for key, value in item.items() if key != "absolute_path"}
            for item in final_preflight["entries"]
        ]
        != [
            {key: value for key, value in item.items() if key != "absolute_path"}
            for item in preflight["entries"]
        ]
    ):
        raise EvidenceManifestError("证据 stat 边界在扫描期间发生漂移。")
    ordered_findings = [
        value
        for _, value in sorted(
            {
                (item["path"], item["rule"], int(item["offset"])): item
                for item in all_findings
            }.items()
        )
    ]
    inventory_entries = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in final_entries
    ]
    inventory = {
        "entry_count": len(inventory_entries),
        "entries": inventory_entries,
        "digest": canonical_json_sha256({"entries": inventory_entries}),
    }
    security = {
        "known_secret_scan_passed": not ordered_findings,
        "known_secret_env_names": sorted(set(secret_env_names)),
        "file_count": len(final_entries),
        "scanned_bytes": preflight["total_bytes"],
        "findings": ordered_findings,
        "limitation": (
            None
            if secret_env_names
            else "未读取容器内 OAuth 凭据值；仍执行令牌形态启发式扫描。"
        ),
    }
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at_utc": started_at,
        "completed_at_utc": _utc_now(),
        "roots": preflight["roots"],
        "metadata_sha256": preflight["metadata_sha256"],
        "entry_count": len(final_entries),
        "total_bytes": preflight["total_bytes"],
        "entries": final_entries,
        "inventory": inventory,
        "security": security,
        "scan": {
            "full_scan_count": 1,
            "scanned_bytes": scanned_bytes,
            "reused_bytes": reused_bytes,
            "total_bytes": preflight["total_bytes"],
            "elapsed_seconds": round(time.monotonic() - started, 6),
        },
    }
    manifest["manifest_digest"] = canonical_json_sha256(manifest)
    return manifest


def merge_evidence_manifests(
    reused_manifest: Mapping[str, Any],
    delta_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """合并已验证的来源清单与本轮小型增量清单，不重读来源文件内容。"""

    reused = validate_manifest_document(reused_manifest)
    delta = validate_manifest_document(delta_manifest)
    manifests = (reused, delta)
    root_rows: dict[str, dict[str, Any]] = {}
    old_prefix_to_root: list[dict[str, str]] = []
    for manifest in manifests:
        prefix_map: dict[str, str] = {}
        for row in manifest["roots"]:
            path = str(row["path"])
            prefix = str(row["prefix"])
            if path in root_rows:
                raise EvidenceManifestError("来源与增量 EvidenceManifest 的证据根重叠。")
            root_rows[path] = dict(row)
            prefix_map[prefix] = path
        old_prefix_to_root.append(prefix_map)

    prefix_by_root = {
        str(root): prefix
        for root, prefix in _root_map(Path(path) for path in root_rows)
    }
    roots = [
        {**root_rows[path], "prefix": prefix_by_root[path]}
        for path in sorted(root_rows)
    ]
    entries: list[dict[str, Any]] = []
    for manifest, prefix_map in zip(manifests, old_prefix_to_root, strict=True):
        for row in manifest["entries"]:
            old_prefix, separator, relative = str(row["path"]).partition("/")
            root_path = prefix_map.get(old_prefix)
            if not separator or not relative or root_path is None:
                raise EvidenceManifestError("EvidenceManifest 合并条目的根前缀非法。")
            entries.append(
                {
                    **row,
                    "path": f"{prefix_by_root[root_path]}/{relative}",
                }
            )
    entries.sort(key=lambda item: str(item["path"]))
    if len({str(item["path"]) for item in entries}) != len(entries):
        raise EvidenceManifestError("EvidenceManifest 合并后出现重复逻辑路径。")

    inventory_entries = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in entries
    ]
    total_bytes = sum(int(item["size"]) for item in entries)
    reused_security = reused["security"]
    delta_security = delta["security"]
    for field in ("known_secret_env_names", "limitation"):
        if reused_security.get(field) != delta_security.get(field):
            raise EvidenceManifestError(
                f"来源与增量 EvidenceManifest 的安全扫描合同不一致：{field}"
            )
    metadata_view = {
        "roots": roots,
        "entries": [
            {key: value for key, value in entry.items() if key != "sha256"}
            for entry in entries
        ],
    }
    inventory = {
        "entry_count": len(inventory_entries),
        "entries": inventory_entries,
        "digest": canonical_json_sha256({"entries": inventory_entries}),
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at_utc": delta["created_at_utc"],
        "completed_at_utc": _utc_now(),
        "roots": roots,
        "metadata_sha256": canonical_json_sha256(metadata_view),
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "entries": entries,
        "inventory": inventory,
        "security": {
            "known_secret_scan_passed": True,
            "known_secret_env_names": list(reused_security["known_secret_env_names"]),
            "file_count": len(entries),
            "scanned_bytes": total_bytes,
            "findings": [],
            "limitation": reused_security.get("limitation"),
        },
        "scan": {
            "full_scan_count": 1,
            "scanned_bytes": int(delta["scan"]["scanned_bytes"]),
            "reused_bytes": int(reused["total_bytes"])
            + int(delta["scan"]["reused_bytes"]),
            "total_bytes": total_bytes,
            "elapsed_seconds": float(delta["scan"]["elapsed_seconds"]),
        },
    }
    manifest["manifest_digest"] = canonical_json_sha256(manifest)
    return validate_manifest_document(manifest)


def validate_manifest_document(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    digest = payload.pop("manifest_digest", None)
    entries = payload.get("entries")
    inventory = payload.get("inventory")
    security = payload.get("security")
    scan = payload.get("scan")
    expected_inventory_entries = (
        [
            {"path": item.get("path"), "size": item.get("size"), "sha256": item.get("sha256")}
            for item in entries
        ]
        if isinstance(entries, list) and all(isinstance(item, dict) for item in entries)
        else None
    )
    if (
        set(manifest)
        != {
            "schema_version",
            "created_at_utc",
            "completed_at_utc",
            "roots",
            "metadata_sha256",
            "entry_count",
            "total_bytes",
            "entries",
            "inventory",
            "security",
            "scan",
            "manifest_digest",
        }
        or payload.get("schema_version") != MANIFEST_SCHEMA
        or not isinstance(payload.get("roots"), list)
        or not isinstance(entries, list)
        or payload.get("entry_count") != len(entries)
        or not isinstance(payload.get("total_bytes"), int)
        or payload["total_bytes"] < 0
        or not all(
            isinstance(item, dict)
            and set(item)
            == {
                "path",
                "mode",
                "device",
                "inode",
                "size",
                "mtime_ns",
                "ctime_ns",
                "nlink",
                "sha256",
            }
            and isinstance(item.get("path"), str)
            and isinstance(item.get("size"), int)
            and item["size"] >= 0
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
            for item in entries
        )
        or sum(item["size"] for item in entries) != payload["total_bytes"]
        or not isinstance(inventory, dict)
        or inventory.get("entry_count") != len(entries)
        or inventory.get("entries") != expected_inventory_entries
        or inventory.get("digest")
        != canonical_json_sha256({"entries": expected_inventory_entries})
        or not isinstance(security, dict)
        or security.get("file_count") != len(entries)
        or security.get("known_secret_scan_passed") is not True
        or security.get("findings") != []
        or security.get("scanned_bytes") != payload["total_bytes"]
        or not isinstance(scan, dict)
        or set(scan)
        != {
            "full_scan_count",
            "scanned_bytes",
            "reused_bytes",
            "total_bytes",
            "elapsed_seconds",
        }
        or scan.get("full_scan_count") != 1
        or not all(
            isinstance(scan.get(field), int) and scan[field] >= 0
            for field in ("scanned_bytes", "reused_bytes", "total_bytes")
        )
        or scan["scanned_bytes"] + scan["reused_bytes"] != scan["total_bytes"]
        or scan["total_bytes"] != payload["total_bytes"]
        or not isinstance(digest, str)
        or digest != canonical_json_sha256(payload)
    ):
        raise EvidenceManifestError("EvidenceManifest 结构或自摘要非法。")
    return dict(manifest)


REHEARSAL_CONTEXT_ENV = "CODEX_UPGRADE_SEAL_REHEARSAL_ACTIVE"


def _mount_fstype_of(path: Path) -> str | None:
    """返回覆盖 ``path`` 的最长挂载点的文件系统类型；读不到 mountinfo 时为 None。"""

    try:
        rows = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    resolved = str(Path(path).resolve(strict=False))
    best: tuple[int, str] | None = None
    for row in rows:
        left, _, right = row.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mount_point = fields[4].replace("\\040", " ")
        if resolved == mount_point or resolved.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > best[0]:
                best = (len(mount_point), right.split()[0])
    return None if best is None else best[1]


def _isolated_rehearsal_context(roots: Iterable[Path]) -> bool:
    """只有带预演标记且全部证据根都在 overlay 挂载点上才视为隔离预演。"""

    if os.environ.get(REHEARSAL_CONTEXT_ENV) != "1":
        return False
    root_list = [Path(root) for root in roots]
    verdicts = [(str(root), _mount_fstype_of(root)) for root in root_list]
    isolated = bool(verdicts) and all(fstype == "overlay" for _root, fstype in verdicts)
    if not isolated:
        # 带预演标记却不在 overlay 上：把逐根判定写到 stderr，便于定位是哪一个证据根
        # 没有被 namespace 覆盖（正式目录上执行时也据此失败关闭）。
        import sys

        sys.stderr.write(
            "EvidenceManifest 隔离预演判定未成立：" + json.dumps(verdicts, ensure_ascii=False) + "\n"
        )
    return isolated


def verify_manifest_boundary(
    manifest: Mapping[str, Any], roots: Iterable[Path]
) -> dict[str, Any]:
    """只比较目录项和 stat 边界，绝不读取文件内容。"""

    expected = validate_manifest_document(manifest)
    current = preflight_evidence_roots(roots)
    # rehearse-candidate-seal 在 OverlayFS 副本上执行时，所有条目的 st_dev 必然与
    # 正式目录不同；隔离预演只忽略 device 字段（及由其派生的 metadata_sha256），
    # 目录项、大小、mtime、inode 等其余 stat 边界仍逐项比较。正式目录上判据不变。
    rehearsal = _isolated_rehearsal_context(roots)
    expected_drop = {"sha256"} | ({"device"} if rehearsal else set())
    current_drop = {"absolute_path"} | ({"device"} if rehearsal else set())
    expected_entries = [
        {key: value for key, value in item.items() if key not in expected_drop}
        for item in expected["entries"]
    ]
    current_entries = [
        {key: value for key, value in item.items() if key not in current_drop}
        for item in current["entries"]
    ]
    if (
        current["roots"] != expected["roots"]
        or current_entries != expected_entries
        or (
            not rehearsal
            and current["metadata_sha256"] != expected["metadata_sha256"]
        )
    ):
        raise EvidenceManifestError("EvidenceManifest 的不可变 stat 边界发生漂移。")
    return {
        "status": "passed",
        "entry_count": current["entry_count"],
        "total_bytes": current["total_bytes"],
        "manifest_digest": expected["manifest_digest"],
        "scanned_bytes": 0,
    }


def deep_verify_manifest(
    manifest: Mapping[str, Any],
    roots: Iterable[Path],
    *,
    checkpoint_path: Path,
    secret_env_names: Sequence[str] = (),
) -> dict[str, Any]:
    """显式重哈希并比较既有 manifest，不覆盖既有 manifest。"""

    expected = validate_manifest_document(manifest)
    current = build_evidence_manifest(
        roots,
        checkpoint_path=checkpoint_path,
        secret_env_names=secret_env_names,
    )
    stable_fields = (
        "roots",
        "metadata_sha256",
        "entry_count",
        "total_bytes",
        "entries",
        "inventory",
        "security",
    )
    if any(current[field] != expected[field] for field in stable_fields):
        raise EvidenceManifestError("显式 deep-verify 与既有 EvidenceManifest 不一致。")
    return {
        "status": "passed",
        "manifest_digest": expected["manifest_digest"],
        "full_scan_count": current["scan"]["full_scan_count"],
        "scanned_bytes": current["scan"]["scanned_bytes"],
        "reused_bytes": current["scan"]["reused_bytes"],
        "total_bytes": current["scan"]["total_bytes"],
        "elapsed_seconds": current["scan"]["elapsed_seconds"],
    }
