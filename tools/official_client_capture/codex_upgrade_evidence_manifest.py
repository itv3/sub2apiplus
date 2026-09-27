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
# 改造 5 M2：按整根投影旧清单的来源收据（attempt-recovery 增量封存用）。
PROJECTION_SCHEMA = "codex-upgrade-evidence-manifest-projection/v1"


class EvidenceManifestError(ValueError):
    """证据边界、权限、检查点或内容摘要不可信。"""


# 2026-09-22（v14r4 事故）：已封存 EvidenceManifest 绑定的证据在 seal 之后被再次 chmod，
# 16 个条目 ctime_ns 漂移，accept 读侧只抛通用 EvidenceManifestError，父监督器按默认
# execution-failure 又把它升级成"可修工具后逐字重派"的 post-run-tooling。ctime 不可合法
# 回写、manifest 是 write-once 产物，这类漂移对当前 attempt 永远不可恢复，必须由生产者
# 明确给出机器失败分类，监督器与 reconciler 只校验记录、不从 message 反推。
BOUNDARY_DRIFT_CHECK_ID = "evidence-manifest.boundary"
BOUNDARY_DRIFT_FAILURE_CODE = "stat-boundary-drift"


class EvidenceManifestBoundaryDriftError(EvidenceManifestError):
    """既有 EvidenceManifest 的不可变 stat 边界与当前证据根不一致。

    ``failure_class``／``failure_observations`` 与 ``CandidateReadinessError`` 同构，
    供 ``write_campaign_run_action_diagnostic`` 直接写入动作诊断（v3）：分类固定为
    永久失败类 ``evidence-integrity``，观测二元组固定为
    ``evidence-manifest.boundary／stat-boundary-drift``，reconciler 据此生成稳定根因。
    """

    failure_class = "evidence-integrity"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.failure_observations = [
            {
                "check_id": BOUNDARY_DRIFT_CHECK_ID,
                "failure_code": BOUNDARY_DRIFT_FAILURE_CODE,
            }
        ]


# 第三批 R3（ctime 部分）：只有 mtime_ns／ctime_ns／inode 漂移（路径集合、size、mode、nlink、device 都不变）的边界
# 差异是"元数据漂移"，不是完整性异常——seal 后再 chmod、复制回原路径都只改这三项而内容未变。它可经
# ``harden-evidence-permissions rebind-boundary`` 逐文件复算内容一致后写一份 rebind 收据（write-once、成链、绑定清单
# 摘要与新旧边界摘要），读侧核验时接受 rebind 链末绑定的边界。清单文件本身仍是 write-once、不改。
METADATA_DRIFT_FAILURE_CODE = "metadata-only-drift"
METADATA_DRIFT_FAILURE_CLASS = "evidence-metadata-drift"
METADATA_DRIFT_FIELDS = frozenset({"mtime_ns", "ctime_ns", "inode"})
REBIND_SCHEMA = "evidence-manifest-boundary-rebind/v1"
REBIND_RE = re.compile(r"^evidence-manifest-rebind-(\d{2})\.json$")
REBIND_MAX_INDEX = 99
MAX_REBIND_BYTES = 16 * 1024 * 1024


class EvidenceManifestMetadataDriftError(EvidenceManifestError):
    """既有 EvidenceManifest 只有 mtime／ctime／inode 漂移：可恢复，先 rebind-boundary 再继续。

    ``failure_class``／``failure_observations`` 形态与 ``EvidenceManifestBoundaryDriftError`` 相同，分类为
    可恢复类 ``evidence-metadata-drift``，观测 ``evidence-manifest.boundary／metadata-only-drift``。
    """

    failure_class = METADATA_DRIFT_FAILURE_CLASS

    def __init__(self, message: str, *, drifted_entries: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.failure_observations = [
            {
                "check_id": BOUNDARY_DRIFT_CHECK_ID,
                "failure_code": METADATA_DRIFT_FAILURE_CODE,
            }
        ]
        self.drifted_entries = [dict(item) for item in drifted_entries]


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


def project_evidence_manifest(
    manifest: Mapping[str, Any],
    *,
    keep_roots: Iterable[str | Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按**整根**投影已验证的旧清单：只保留 ``keep_roots``，不读任何文件正文。

    改造 5 M2（attempt-recovery 增量封存）：被重采 Job 的根与旧 attempt 的派生根整根丢弃，
    保留根的 ``prefix`` 与全部条目逐字沿用，``entries／inventory／security`` 按根过滤并重算计数，
    ``scan`` 记为零扫描（全部字节视为复用）。返回投影清单（通过 ``validate_manifest_document``）
    与投影来源收据（``PROJECTION_SCHEMA``：来源摘要、保留根、丢弃根、投影摘要）。
    保留根必须逐一存在于来源 ``roots``，不允许子树裁剪；``keep_roots`` 可为空（全部 Job 重采）。
    """

    source = validate_manifest_document(manifest)
    requested = [str(Path(str(value))) for value in keep_roots]
    if len(set(requested)) != len(requested):
        raise EvidenceManifestError("投影保留根重复。")
    keep = set(requested)
    source_paths = [str(row["path"]) for row in source["roots"]]
    missing = sorted(keep - set(source_paths))
    if missing:
        raise EvidenceManifestError(
            "投影保留根不在来源 EvidenceManifest 的 roots 内：" + ", ".join(missing)
        )
    kept_rows = [dict(row) for row in source["roots"] if str(row["path"]) in keep]
    kept_prefixes = {str(row["prefix"]) for row in kept_rows}
    entries = [
        dict(row)
        for row in source["entries"]
        if str(row["path"]).partition("/")[0] in kept_prefixes
    ]
    dropped = sorted(path for path in source_paths if path not in keep)
    inventory_entries = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in entries
    ]
    total_bytes = sum(int(item["size"]) for item in entries)
    source_security = source["security"]
    metadata_view = {
        "roots": kept_rows,
        "entries": [
            {key: value for key, value in entry.items() if key != "sha256"}
            for entry in entries
        ],
    }
    projected: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "created_at_utc": source["created_at_utc"],
        "completed_at_utc": _utc_now(),
        "roots": kept_rows,
        "metadata_sha256": canonical_json_sha256(metadata_view),
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "entries": entries,
        "inventory": {
            "entry_count": len(inventory_entries),
            "entries": inventory_entries,
            "digest": canonical_json_sha256({"entries": inventory_entries}),
        },
        "security": {
            "known_secret_scan_passed": True,
            "known_secret_env_names": list(source_security["known_secret_env_names"]),
            "file_count": len(entries),
            "scanned_bytes": total_bytes,
            "findings": [],
            "limitation": source_security.get("limitation"),
        },
        "scan": {
            "full_scan_count": 1,
            "scanned_bytes": 0,
            "reused_bytes": total_bytes,
            "total_bytes": total_bytes,
            "elapsed_seconds": 0.0,
        },
    }
    projected["manifest_digest"] = canonical_json_sha256(projected)
    projected = validate_manifest_document(projected)
    receipt = {
        "schema_version": PROJECTION_SCHEMA,
        "source_manifest_digest": str(source["manifest_digest"]),
        "kept_roots": sorted(keep),
        "dropped_roots": dropped,
        "kept_entry_count": len(entries),
        "dropped_entry_count": int(source["entry_count"]) - len(entries),
        "projected_manifest_digest": str(projected["manifest_digest"]),
    }
    return projected, receipt


def validate_projection_receipt(
    receipt: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any],
    projected_manifest: Mapping[str, Any],
    expected_dropped_roots: Iterable[str | Path],
) -> dict[str, Any]:
    """重放投影收据：字段闭集、来源／投影摘要绑定、``dropped_roots`` 与期望集合精确相等。"""

    expected_keys = {
        "schema_version",
        "source_manifest_digest",
        "kept_roots",
        "dropped_roots",
        "kept_entry_count",
        "dropped_entry_count",
        "projected_manifest_digest",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != expected_keys:
        raise EvidenceManifestError("投影收据字段不闭合。")
    if receipt.get("schema_version") != PROJECTION_SCHEMA:
        raise EvidenceManifestError("投影收据 schema 不受支持。")
    source = validate_manifest_document(source_manifest)
    projected = validate_manifest_document(projected_manifest)
    if receipt.get("source_manifest_digest") != source["manifest_digest"]:
        raise EvidenceManifestError("投影收据未绑定来源 EvidenceManifest 摘要。")
    if receipt.get("projected_manifest_digest") != projected["manifest_digest"]:
        raise EvidenceManifestError("投影收据未绑定投影 EvidenceManifest 摘要。")
    kept = receipt.get("kept_roots")
    dropped = receipt.get("dropped_roots")
    if (
        not isinstance(kept, list)
        or not isinstance(dropped, list)
        or kept != sorted(str(row["path"]) for row in projected["roots"])
        or sorted(kept) != kept
        or sorted(dropped) != dropped
        or set(kept) | set(dropped) != {str(row["path"]) for row in source["roots"]}
        or set(kept) & set(dropped)
    ):
        raise EvidenceManifestError("投影收据的保留根／丢弃根与两份清单不一致。")
    expected_dropped = sorted({str(Path(str(value))) for value in expected_dropped_roots})
    if dropped != expected_dropped:
        raise EvidenceManifestError(
            f"投影丢弃根与恢复基线冻结的集合不精确相等：实际={dropped}，期望={expected_dropped}"
        )
    if (
        receipt.get("kept_entry_count") != projected["entry_count"]
        or receipt.get("dropped_entry_count") != source["entry_count"] - projected["entry_count"]
    ):
        raise EvidenceManifestError("投影收据条目计数与清单不一致。")
    return dict(receipt)


def merge_evidence_manifests(
    reused_manifest: Mapping[str, Any],
    delta_manifest: Mapping[str, Any],
    *,
    preserve_prefixes: bool = False,
) -> dict[str, Any]:
    """合并已验证的来源清单与本轮小型增量清单，不重读来源文件内容。

    ``preserve_prefixes=True``（改造 5 M2 增量封存）：来源清单（投影）的根前缀逐字沿用、
    条目路径不重写，增量清单的根沿用自身前缀且不得与来源前缀冲突；默认分支仍对全部根
    按既有规则重算前缀。
    """

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

    if preserve_prefixes:
        reused_prefixes = set(old_prefix_to_root[0])
        conflicts = sorted(reused_prefixes & set(old_prefix_to_root[1]))
        if conflicts:
            raise EvidenceManifestError(
                "增量 EvidenceManifest 的根前缀与保留根前缀冲突：" + ", ".join(conflicts)
            )
        prefix_by_root = {
            path: prefix
            for prefix_map in old_prefix_to_root
            for prefix, path in prefix_map.items()
        }
    else:
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
            # 同一挂载点后挂载的覆盖先挂载的（namespace 内把别名重新 bind 到 overlay），取最后一条。
            if best is None or len(mount_point) >= best[0]:
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


def _boundary_view(
    roots: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    *,
    drop: frozenset[str],
) -> dict[str, Any]:
    """stat 边界视图：去掉内容摘要与绝对路径（隔离预演时再去 device），供比较与 rebind 摘要。"""

    return {
        "roots": [{key: value for key, value in item.items() if key not in drop} for item in roots],
        "entries": [{key: value for key, value in item.items() if key not in drop} for item in entries],
    }


# 正式口径的视图只去内容摘要与绝对路径；rebind 收据里的边界摘要按这一口径计算。
_BOUNDARY_VIEW_DROP = frozenset({"sha256", "absolute_path"})


def _view_drop(rehearsal: bool) -> frozenset[str]:
    return _BOUNDARY_VIEW_DROP | (frozenset({"device"}) if rehearsal else frozenset())


def boundary_view_sha256(view: Mapping[str, Any]) -> str:
    return canonical_json_sha256({"roots": list(view["roots"]), "entries": list(view["entries"])})


def classify_boundary_drift(
    expected_view: Mapping[str, Any], current_view: Mapping[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """比较两份 stat 视图：路径集合一致、字段集合一致且每条只有 mtime_ns／ctime_ns／inode 不同 → ``metadata-only``
    并给出逐条漂移清单；其余任何差异（增删条目、size、mode、nlink、device）→ ``boundary``。"""

    def diff(expected_items: Sequence[Mapping[str, Any]], current_items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
        expected_map = {str(item["path"]): item for item in expected_items}
        current_map = {str(item["path"]): item for item in current_items}
        if set(expected_map) != set(current_map) or len(expected_map) != len(expected_items) or len(current_map) != len(current_items):
            return None
        drifted: list[dict[str, Any]] = []
        for path in sorted(expected_map):
            before, after = expected_map[path], current_map[path]
            if set(before) != set(after):
                return None
            changed = {field for field in before if before[field] != after[field]}
            if not changed:
                continue
            if not changed <= METADATA_DRIFT_FIELDS:
                return None
            drifted.append(
                {
                    "path": path,
                    "fields": {field: {"expected": before[field], "current": after[field]} for field in sorted(changed)},
                }
            )
        return drifted

    root_drift = diff(expected_view["roots"], current_view["roots"])
    entry_drift = diff(expected_view["entries"], current_view["entries"])
    if root_drift is None or entry_drift is None:
        return "boundary", []
    return "metadata-only", [*root_drift, *entry_drift]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_boundary_rebinds(manifest_path: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """读取并校验清单同目录的 rebind 链（evidence-manifest-rebind-NN.json）；没有即空链。

    校验：编号连续、schema、自摘要、绑定本清单 manifest_digest、previous 成链、起点等于上一有效边界
    （首条等于清单自身的 stat 视图摘要）、new_boundary 摘要由视图重算、内容已复算。任何不一致失败关闭。
    """

    manifest_path = Path(manifest_path)
    directory = manifest_path.parent
    if directory.is_symlink() or not directory.is_dir():
        raise EvidenceManifestError(f"EvidenceManifest 所在目录不可信：{directory}")
    found: dict[int, dict[str, Any]] = {}
    for child in sorted(directory.iterdir()):
        match = REBIND_RE.fullmatch(child.name)
        if not match:
            continue
        if child.is_symlink() or not child.is_file():
            raise EvidenceManifestError(f"rebind 收据不是普通文件：{child}")
        if child.stat().st_size > MAX_REBIND_BYTES:
            raise EvidenceManifestError(f"rebind 收据超过大小上限：{child}")
        try:
            payload = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EvidenceManifestError(f"rebind 收据无法读取：{child}") from error
        index = int(match.group(1))
        if not isinstance(payload, dict) or payload.get("schema_version") != REBIND_SCHEMA or payload.get("index") != index:
            raise EvidenceManifestError(f"rebind 收据 {child.name} schema 或编号非法")
        unsigned = {key: value for key, value in payload.items() if key != "receipt_sha256"}
        if payload.get("receipt_sha256") != canonical_json_sha256(unsigned):
            raise EvidenceManifestError(f"rebind 收据 {child.name} 自摘要不一致")
        found[index] = payload
    if not found:
        return []
    expected = validate_manifest_document(manifest)
    previous_sha: str | None = None
    previous_boundary = boundary_view_sha256(
        _boundary_view(expected["roots"], expected["entries"], drop=_BOUNDARY_VIEW_DROP)
    )
    chain: list[dict[str, Any]] = []
    for index in range(1, max(found) + 1):
        payload = found.get(index)
        if payload is None:
            raise EvidenceManifestError(f"rebind 链缺少 evidence-manifest-rebind-{index:02d}.json")
        label = f"rebind-{index:02d}"
        if payload.get("manifest_digest") != expected["manifest_digest"]:
            raise EvidenceManifestError(f"{label} 绑定的清单摘要不是本 EvidenceManifest")
        if payload.get("previous_rebind_sha256") != previous_sha:
            raise EvidenceManifestError(f"{label} 未衔接上一 rebind")
        if payload.get("previous_boundary_sha256") != previous_boundary:
            raise EvidenceManifestError(f"{label} 的起点不是上一有效边界")
        new_boundary = payload.get("new_boundary")
        if (
            not isinstance(new_boundary, dict)
            or set(new_boundary) != {"roots", "entries"}
            or not isinstance(new_boundary.get("roots"), list)
            or not isinstance(new_boundary.get("entries"), list)
        ):
            raise EvidenceManifestError(f"{label} 的 new_boundary 形态非法")
        if payload.get("new_boundary_sha256") != boundary_view_sha256(new_boundary):
            raise EvidenceManifestError(f"{label} 的 new_boundary 摘要与视图不一致")
        if payload.get("content_recomputed") is not True:
            raise EvidenceManifestError(f"{label} 没有逐文件复算内容")
        if not isinstance(payload.get("drifted_entries"), list) or not payload["drifted_entries"]:
            raise EvidenceManifestError(f"{label} 缺少漂移条目清单")
        previous_sha = str(payload["receipt_sha256"])
        previous_boundary = str(payload["new_boundary_sha256"])
        chain.append(payload)
    return chain


def build_boundary_rebind(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    roots: Iterable[Path],
    *,
    existing_rebinds: Sequence[Mapping[str, Any]],
    campaign_id: str,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
    operator: str,
) -> dict[str, Any]:
    """生成下一份 rebind 收据（不落盘）：当前边界相对上一有效边界必须只有元数据漂移，且逐文件内容等于清单。"""

    expected = validate_manifest_document(manifest)
    root_list = list(roots)
    current = preflight_evidence_roots(root_list)
    if existing_rebinds:
        base_view: Mapping[str, Any] = existing_rebinds[-1]["new_boundary"]
    else:
        base_view = _boundary_view(expected["roots"], expected["entries"], drop=_BOUNDARY_VIEW_DROP)
    current_view = _boundary_view(current["roots"], current["entries"], drop=_BOUNDARY_VIEW_DROP)
    if current_view == base_view:
        raise EvidenceManifestError("证据边界与上一有效边界一致，无需 rebind。")
    kind, drifted = classify_boundary_drift(base_view, current_view)
    if kind != "metadata-only":
        raise EvidenceManifestBoundaryDriftError(
            "证据边界不是只有 mtime／ctime／inode 的元数据漂移（路径集合、size、mode、nlink 或 device 已变化），不能 rebind。"
        )
    expected_sha256 = {str(item["path"]): str(item["sha256"]) for item in expected["entries"]}
    for entry in current["entries"]:
        actual = _file_sha256(Path(str(entry["absolute_path"])))
        if actual != expected_sha256.get(str(entry["path"])):
            raise EvidenceManifestBoundaryDriftError(
                f"证据文件内容与 EvidenceManifest 不一致，不能 rebind：{entry['path']}"
            )
    index = len(existing_rebinds) + 1
    if index > REBIND_MAX_INDEX:
        raise EvidenceManifestError("rebind 次数已达上限。")
    manifest_path = Path(manifest_path)
    payload: dict[str, Any] = {
        "schema_version": REBIND_SCHEMA,
        "index": index,
        "campaign_id": campaign_id,
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "manifest": {"path": manifest_path.name, "sha256": _file_sha256(manifest_path)},
        "manifest_digest": expected["manifest_digest"],
        "previous_rebind_sha256": str(existing_rebinds[-1]["receipt_sha256"]) if existing_rebinds else None,
        "previous_boundary_sha256": boundary_view_sha256(base_view),
        "drifted_entries": drifted,
        "content_recomputed": True,
        "new_boundary": current_view,
        "new_boundary_sha256": boundary_view_sha256(current_view),
        "operator": operator,
        "recorded_at_utc": _utc_now(),
    }
    payload["receipt_sha256"] = canonical_json_sha256(payload)
    return payload


def verify_manifest_boundary(
    manifest: Mapping[str, Any],
    roots: Iterable[Path],
    *,
    rebinds: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """只比较目录项和 stat 边界，绝不读取文件内容。

    ``rebinds`` 是已校验的 rebind 链（``load_boundary_rebinds``）：当前边界等于链末绑定的边界即通过；
    否则相对上一有效边界分类——只有 mtime／ctime／inode 漂移抛可恢复的
    ``EvidenceManifestMetadataDriftError``，其余抛 ``EvidenceManifestBoundaryDriftError``。
    """

    expected = validate_manifest_document(manifest)
    current = preflight_evidence_roots(roots)
    # rehearse-candidate-seal 在 OverlayFS 副本上执行时，所有条目的 st_dev 必然与
    # 正式目录不同；隔离预演只忽略 device 字段（及由其派生的 metadata_sha256），
    # 目录项、大小、mtime、inode 等其余 stat 边界仍逐项比较。正式目录上判据不变。
    rehearsal = _isolated_rehearsal_context(roots)
    drop = _view_drop(rehearsal)
    expected_view = _boundary_view(expected["roots"], expected["entries"], drop=drop)
    current_view = _boundary_view(current["roots"], current["entries"], drop=drop)
    if current_view == expected_view and (rehearsal or current["metadata_sha256"] == expected["metadata_sha256"]):
        return {
            "status": "passed",
            "entry_count": current["entry_count"],
            "total_bytes": current["total_bytes"],
            "manifest_digest": expected["manifest_digest"],
            "scanned_bytes": 0,
            "rebind_index": None,
        }
    if rebinds:
        latest = rebinds[-1]
        latest_view = _boundary_view(latest["new_boundary"]["roots"], latest["new_boundary"]["entries"], drop=drop)
        if current_view == latest_view:
            return {
                "status": "passed",
                "entry_count": current["entry_count"],
                "total_bytes": current["total_bytes"],
                "manifest_digest": expected["manifest_digest"],
                "scanned_bytes": 0,
                "rebind_index": int(latest["index"]),
            }
        # 链末之后再次漂移：以链末边界为基准分类。
        expected_view = latest_view
    kind, drifted = classify_boundary_drift(expected_view, current_view)
    if rehearsal:
        # 隔离预演下把首个差异条目写到 stderr，便于区分 overlay 固有差异与真实漂移。
        import sys

        detail: dict[str, Any] = {
            "kind": kind,
            "roots_equal": current_view["roots"] == expected_view["roots"],
            "expected_count": len(expected_view["entries"]),
            "current_count": len(current_view["entries"]),
        }
        if current_view["roots"] != expected_view["roots"]:
            detail["roots"] = {"expected": expected_view["roots"], "current": current_view["roots"]}
        for index, (left, right) in enumerate(zip(expected_view["entries"], current_view["entries"])):
            if left != right:
                detail["first_diff_index"] = index
                detail["expected"] = left
                detail["current"] = right
                break
        sys.stderr.write("EvidenceManifest 隔离预演差异：" + json.dumps(detail, ensure_ascii=False, default=str) + "\n")
    if kind == "metadata-only":
        raise EvidenceManifestMetadataDriftError(
            "EvidenceManifest 只有 mtime／ctime／inode 元数据漂移；执行 harden-evidence-permissions rebind-boundary "
            "逐文件复算内容并绑定新边界后可继续。",
            drifted_entries=drifted,
        )
    raise EvidenceManifestBoundaryDriftError(
        "EvidenceManifest 的不可变 stat 边界发生漂移。"
    )


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
