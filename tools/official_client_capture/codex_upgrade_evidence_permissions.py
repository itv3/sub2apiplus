#!/usr/bin/env python3
"""在 Attempt 发布前以纯元数据方式收口全部证据权限。

本模块不绑定 Campaign、版本或 Job 常量。外部证据继续以采集工具登记的逻辑
``runs`` 路径作为身份；实际修改只通过当前受管工具树反推出的宿主 ``data/runs``
别名完成。只有逐项证明两条路径的 inode 元数据完全一致后才允许 ``fchmod``。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "codex-upgrade-evidence-permission-closeout/v1"
RECEIPT_FILENAME = "evidence-permission-closeout.json"
LOGICAL_RUNS_ROOTS = (
    Path("/capture/runs"),
    Path("/root/oauth-capture/runs"),
)
TCPDUMP_UID = 100
TCPDUMP_GID = 102
TCPDUMP_FILENAMES = frozenset({"egress.pcap", "traffic.pcap"})
SHA256_CHARS = frozenset("0123456789abcdef")
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


class EvidencePermissionError(RuntimeError):
    """证据边界、别名身份或权限收口不可信时抛出。"""


@dataclass(frozen=True)
class EntrySnapshot:
    """一项逻辑路径和可写宿主路径的冻结元数据。"""

    read_path: Path
    write_path: Path
    kind: str
    mode: int
    target_mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    nlink: int
    uid: int
    gid: int
    external_alias: bool


@dataclass(frozen=True)
class BoundarySnapshot:
    """一次只含元数据的完整证据边界。"""

    entries: tuple[EntrySnapshot, ...]
    boundary_sha256: str
    gap_sha256: str
    changed_entry_count: int
    external_alias_entry_count: int


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _managed_data_root() -> Path:
    """从 ``data/tools/official_client_capture`` 或仓库同构位置反推数据根。"""

    return Path(__file__).resolve().parents[2]


def _reject_symlink_components(path: Path, label: str) -> None:
    """拒绝绝对路径中的任一符号链接或缺失组件。"""

    if not path.is_absolute() or ".." in path.parts:
        raise EvidencePermissionError(f"{label} 必须是规范绝对路径。")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise EvidencePermissionError(f"{label} 无法读取：{current}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise EvidencePermissionError(f"{label} 包含符号链接：{current}")


def _kind(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "special"


def _metadata_identity(metadata: os.stat_result) -> tuple[Any, ...]:
    """形成跨别名必须逐项相等的完整稳定身份。"""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        _kind(metadata),
    )


def _owner_allowed(path: Path, kind: str, metadata: Any) -> bool:
    """普通项必须归当前用户；只放行 tcpdump 的固定 pcap 数值身份。"""

    if metadata.st_uid == os.geteuid() and metadata.st_gid == os.getegid():
        return True
    return bool(
        kind == "file"
        and path.name in TCPDUMP_FILENAMES
        and metadata.st_uid == TCPDUMP_UID
        and metadata.st_gid == TCPDUMP_GID
    )


def _owned_directory(path: Path, label: str) -> os.stat_result:
    _reject_symlink_components(path, label)
    metadata = path.lstat()
    if _kind(metadata) != "directory" or not _owner_allowed(path, "directory", metadata):
        raise EvidencePermissionError(f"{label} 类型或属主漂移。")
    return metadata


def _entry_snapshot(
    read_path: Path,
    write_path: Path,
    *,
    external_alias: bool,
) -> EntrySnapshot:
    """验证双别名的类型、属主、链接数及稳定 inode 身份。"""

    _reject_symlink_components(read_path, "证据逻辑路径")
    _reject_symlink_components(write_path, "证据可写路径")
    read_metadata = read_path.lstat()
    write_metadata = write_path.lstat()
    kind = _kind(read_metadata)
    if kind not in {"directory", "file"}:
        raise EvidencePermissionError(f"证据边界包含特殊文件：{read_path}")
    if kind == "file" and (read_metadata.st_nlink != 1 or write_metadata.st_nlink != 1):
        raise EvidencePermissionError(f"证据普通文件存在边界外硬链接：{read_path}")
    if not _owner_allowed(read_path, kind, read_metadata):
        raise EvidencePermissionError(f"证据项属主漂移：{read_path}")
    if _metadata_identity(read_metadata) != _metadata_identity(write_metadata):
        raise EvidencePermissionError(f"证据双别名不是同一 inode：{read_path}")
    mask = 0o700 if kind == "directory" else 0o600
    return EntrySnapshot(
        read_path=read_path,
        write_path=write_path,
        kind=kind,
        mode=stat.S_IMODE(read_metadata.st_mode),
        target_mode=stat.S_IMODE(read_metadata.st_mode) & mask,
        device=read_metadata.st_dev,
        inode=read_metadata.st_ino,
        size=read_metadata.st_size,
        mtime_ns=read_metadata.st_mtime_ns,
        nlink=read_metadata.st_nlink,
        uid=read_metadata.st_uid,
        gid=read_metadata.st_gid,
        external_alias=external_alias,
    )


def _walk_paths(root: Path) -> list[Path]:
    """确定性枚举一棵证据树，不跟随符号链接。"""

    paths = [root]

    def onerror(error: OSError) -> None:
        raise EvidencePermissionError(f"证据树枚举失败：{root}") from error

    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=onerror,
    ):
        directory_names.sort()
        file_names.sort()
        paths.extend(Path(current) / name for name in directory_names)
        paths.extend(Path(current) / name for name in file_names)
    return paths


def _boundary_record(entry: EntrySnapshot) -> dict[str, Any]:
    """权限位不进入稳定边界，保证 fchmod 前后可比较。"""

    return {
        "read_path": str(entry.read_path),
        "write_path": str(entry.write_path),
        "kind": entry.kind,
        "device": entry.device,
        "inode": entry.inode,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
        "nlink": entry.nlink,
        "uid": entry.uid,
        "gid": entry.gid,
        "external_alias": entry.external_alias,
    }


def _gap_record(entry: EntrySnapshot) -> dict[str, str]:
    return {
        "path": str(entry.read_path),
        "kind": entry.kind,
        "mode": format(entry.mode, "04o"),
        "target_mode": format(entry.target_mode, "04o"),
    }


def _normalized_roots(values: Sequence[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw in values:
        root = Path(raw)
        if not root.is_absolute() or ".." in root.parts:
            raise EvidencePermissionError("证据根必须是规范绝对路径。")
        value = str(root)
        if value in seen:
            raise EvidencePermissionError(f"证据根重复：{root}")
        seen.add(value)
        roots.append(root)
    if not roots:
        raise EvidencePermissionError("证据根不能为空。")
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise EvidencePermissionError("证据根发生嵌套。")
    return tuple(roots)


def _root_aliases(
    attempt_root: Path,
    evidence_roots: Sequence[Path],
    *,
    managed_data_root: Path,
    logical_runs_roots: Sequence[Path],
) -> tuple[tuple[Path, Path, bool], ...]:
    """把逻辑证据根映射到唯一受管宿主路径，并验证根 inode。"""

    attempt_root = attempt_root.resolve(strict=True)
    _owned_directory(attempt_root, "Attempt 根")
    managed_data_root = managed_data_root.resolve(strict=True)
    _owned_directory(managed_data_root, "受管数据根")
    writable_runs_root = managed_data_root / "runs"
    writable_runs_metadata: os.stat_result | None = None
    current_internal_roots = {
        attempt_root / "evidence",
        attempt_root / "logs",
    }

    def is_managed_attempt_internal(root: Path) -> bool:
        """识别同一受管数据树内、可能被增量复用的其他 Attempt 根。"""

        try:
            relative = root.relative_to(managed_data_root / "evidence" / "campaigns")
        except ValueError:
            return False
        parts = relative.parts
        return bool(
            len(parts) >= 5
            and parts[-1] in {"evidence", "logs"}
            and parts[-3] == "attempts"
            and all(part not in {"", ".", ".."} for part in parts)
        )
    normalized_aliases = tuple(Path(value) for value in logical_runs_roots)
    if not normalized_aliases:
        raise EvidencePermissionError("逻辑 runs 别名不能为空。")

    aliases: list[tuple[Path, Path, bool]] = []
    validated_runs_roots: set[Path] = set()
    for root in _normalized_roots(evidence_roots):
        if root in current_internal_roots or is_managed_attempt_internal(root):
            aliases.append((root, root, False))
            continue
        match: tuple[Path, Path] | None = None
        for logical_runs_root in normalized_aliases:
            try:
                relative = root.relative_to(logical_runs_root)
            except ValueError:
                continue
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise EvidencePermissionError("外部证据根不能等于 runs 根。")
            match = logical_runs_root, relative
            break
        if match is None:
            raise EvidencePermissionError(f"外部证据根越出登记 runs 别名：{root}")
        logical_runs_root, relative = match
        if writable_runs_metadata is None:
            writable_runs_metadata = _owned_directory(
                writable_runs_root,
                "受管宿主 runs 根",
            )
        if logical_runs_root not in validated_runs_roots:
            logical_metadata = _owned_directory(logical_runs_root, "逻辑 runs 根")
            if _metadata_identity(logical_metadata) != _metadata_identity(
                writable_runs_metadata
            ):
                raise EvidencePermissionError("逻辑 runs 与受管宿主 runs 不是同一 inode。")
            validated_runs_roots.add(logical_runs_root)
        write_root = writable_runs_root.joinpath(*PurePosixPath(relative.as_posix()).parts)
        _entry_snapshot(root, write_root, external_alias=True)
        aliases.append((root, write_root, True))
    return tuple(aliases)


def inspect_evidence_boundary(
    attempt_root: Path,
    evidence_roots: Sequence[Path],
    *,
    managed_data_root: Path | None = None,
    logical_runs_roots: Sequence[Path] = LOGICAL_RUNS_ROOTS,
) -> BoundarySnapshot:
    """只读取 stat 元数据并形成全部证据根的稳定边界摘要。"""

    data_root = Path(managed_data_root) if managed_data_root is not None else _managed_data_root()
    aliases = _root_aliases(
        Path(attempt_root),
        evidence_roots,
        managed_data_root=data_root,
        logical_runs_roots=logical_runs_roots,
    )
    entries: list[EntrySnapshot] = []
    seen_read_paths: set[Path] = set()
    for read_root, write_root, external_alias in aliases:
        for read_path in _walk_paths(read_root):
            if read_path in seen_read_paths:
                raise EvidencePermissionError(f"证据项被重复枚举：{read_path}")
            seen_read_paths.add(read_path)
            relative = read_path.relative_to(read_root)
            entries.append(
                _entry_snapshot(
                    read_path,
                    write_root / relative,
                    external_alias=external_alias,
                )
            )
    entries.sort(key=lambda item: str(item.read_path))
    gaps = tuple(item for item in entries if item.mode != item.target_mode)
    return BoundarySnapshot(
        entries=tuple(entries),
        boundary_sha256=_sha256_bytes(
            _canonical([_boundary_record(item) for item in entries])
        ),
        gap_sha256=_sha256_bytes(_canonical([_gap_record(item) for item in gaps])),
        changed_entry_count=len(gaps),
        external_alias_entry_count=sum(item.external_alias for item in entries),
    )


def _harden_entry(entry: EntrySnapshot) -> None:
    """锁定可写 fd、二次验证元数据后执行最小权限相与。"""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if entry.kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(entry.write_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if _metadata_identity(metadata) != (
            entry.device,
            entry.inode,
            entry.size,
            entry.mtime_ns,
            entry.nlink,
            entry.uid,
            entry.gid,
            entry.kind,
        ):
            raise EvidencePermissionError(f"权限收口前 inode 边界漂移：{entry.read_path}")
        os.fchmod(descriptor, entry.target_mode)
    finally:
        os.close(descriptor)


def _write_receipt_once(path: Path, payload: Mapping[str, Any]) -> None:
    """以 O_EXCL 创建 0600 收据，并同步文件和父目录。"""

    parent_metadata = _owned_directory(path.parent, "权限收据父目录")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise EvidencePermissionError("权限收据父目录必须是 0700。")
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise EvidencePermissionError("权限收口收据已经存在，禁止覆盖。") from error
    try:
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def receipt_binding(attempt_root: Path, receipt_path: Path) -> dict[str, Any]:
    """为 Attempt 生成权限收口收据绑定。"""

    attempt_root = Path(attempt_root).resolve(strict=True)
    receipt_path = Path(receipt_path)
    _reject_symlink_components(receipt_path, "权限收口收据")
    metadata = receipt_path.lstat()
    if (
        receipt_path.parent.resolve(strict=True) != attempt_root
        or receipt_path.name != RECEIPT_FILENAME
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _owner_allowed(receipt_path, "file", metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise EvidencePermissionError("权限收口收据路径、类型、属主或权限非法。")
    return {
        "path": receipt_path.relative_to(attempt_root).as_posix(),
        "sha256": _sha256_file(receipt_path),
        "bytes": metadata.st_size,
    }


def close_evidence_permissions(
    attempt_root: Path,
    evidence_roots: Sequence[Path],
    *,
    managed_data_root: Path | None = None,
    logical_runs_roots: Sequence[Path] = LOGICAL_RUNS_ROOTS,
) -> tuple[Path, dict[str, Any]]:
    """收口全部证据权限，复核稳定边界并只写一次元数据收据。"""

    attempt_root = Path(attempt_root).resolve(strict=True)
    roots = _normalized_roots(evidence_roots)
    data_root = Path(managed_data_root) if managed_data_root is not None else _managed_data_root()
    receipt_path = attempt_root / RECEIPT_FILENAME
    if receipt_path.exists() or receipt_path.is_symlink():
        raise EvidencePermissionError("权限收口收据已经存在，禁止覆盖。")
    before = inspect_evidence_boundary(
        attempt_root,
        roots,
        managed_data_root=data_root,
        logical_runs_roots=logical_runs_roots,
    )
    for entry in before.entries:
        if entry.mode != entry.target_mode:
            _harden_entry(entry)
    after = inspect_evidence_boundary(
        attempt_root,
        roots,
        managed_data_root=data_root,
        logical_runs_roots=logical_runs_roots,
    )
    if (
        after.boundary_sha256 != before.boundary_sha256
        or len(after.entries) != len(before.entries)
        or after.changed_entry_count != 0
        or any(
            current.mode != previous.target_mode
            for previous, current in zip(before.entries, after.entries, strict=True)
        )
    ):
        raise EvidencePermissionError("权限收口后证据边界漂移或权限仍未闭合。")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "recorded_at_utc": _utc_now(),
        "attempt_root": str(attempt_root),
        "evidence_roots": [str(root) for root in roots],
        "managed_runs_root": str(data_root.resolve(strict=True) / "runs"),
        "logical_runs_roots": [str(Path(value)) for value in logical_runs_roots],
        "entry_count": len(after.entries),
        "changed_entry_count": before.changed_entry_count,
        "external_alias_entry_count": after.external_alias_entry_count,
        "boundary_sha256": after.boundary_sha256,
        "pre_closeout_gap_sha256": before.gap_sha256,
        "scanned_bytes": 0,
        "live_request_count": 0,
    }
    receipt["receipt_sha256"] = _sha256_bytes(_canonical(receipt))
    _write_receipt_once(receipt_path, receipt)
    return receipt_path, receipt


def _load_receipt(path: Path) -> dict[str, Any]:
    _reject_symlink_components(path, "权限收口收据")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _owner_allowed(path, "file", metadata)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > MAX_RECEIPT_BYTES
    ):
        raise EvidencePermissionError("权限收口收据类型、属主、权限或大小非法。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidencePermissionError("权限收口收据不是有效 JSON。") from error
    if not isinstance(payload, dict):
        raise EvidencePermissionError("权限收口收据必须是对象。")
    return payload


def replay_evidence_permission_closeout(
    attempt_root: Path,
    evidence_roots: Sequence[Path],
    binding: Mapping[str, Any],
    *,
    managed_data_root: Path | None = None,
    logical_runs_roots: Sequence[Path] = LOGICAL_RUNS_ROOTS,
) -> dict[str, Any]:
    """重放收据绑定，并从逻辑路径重新确认元数据边界和私有权限。"""

    attempt_root = Path(attempt_root).resolve(strict=True)
    expected_fields = {"path", "sha256", "bytes"}
    if not isinstance(binding, Mapping) or set(binding) != expected_fields:
        raise EvidencePermissionError("Attempt 的权限收口绑定字段不闭合。")
    if binding.get("path") != RECEIPT_FILENAME:
        raise EvidencePermissionError("Attempt 的权限收口收据路径非法。")
    digest = binding.get("sha256")
    size = binding.get("bytes")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or not set(digest).issubset(SHA256_CHARS)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size > MAX_RECEIPT_BYTES
    ):
        raise EvidencePermissionError("Attempt 的权限收口摘要或大小非法。")
    path = attempt_root / RECEIPT_FILENAME
    if path.stat().st_size != size or _sha256_file(path) != digest:
        raise EvidencePermissionError("Attempt 的权限收口收据摘要或大小漂移。")
    payload = _load_receipt(path)
    expected_payload_fields = {
        "schema_version",
        "status",
        "recorded_at_utc",
        "attempt_root",
        "evidence_roots",
        "managed_runs_root",
        "logical_runs_roots",
        "entry_count",
        "changed_entry_count",
        "external_alias_entry_count",
        "boundary_sha256",
        "pre_closeout_gap_sha256",
        "scanned_bytes",
        "live_request_count",
        "receipt_sha256",
    }
    unsigned = dict(payload)
    receipt_sha256 = unsigned.pop("receipt_sha256", None)
    roots = _normalized_roots(evidence_roots)
    data_root = Path(managed_data_root) if managed_data_root is not None else _managed_data_root()
    if (
        set(payload) != expected_payload_fields
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("status") != "passed"
        or payload.get("attempt_root") != str(attempt_root)
        or payload.get("evidence_roots") != [str(root) for root in roots]
        or payload.get("managed_runs_root")
        != str(data_root.resolve(strict=True) / "runs")
        or payload.get("logical_runs_roots")
        != [str(Path(value)) for value in logical_runs_roots]
        or payload.get("scanned_bytes") != 0
        or payload.get("live_request_count") != 0
        or not isinstance(receipt_sha256, str)
        or receipt_sha256 != _sha256_bytes(_canonical(unsigned))
    ):
        raise EvidencePermissionError("权限收口收据身份或零读取边界非法。")
    current = inspect_evidence_boundary(
        attempt_root,
        roots,
        managed_data_root=data_root,
        logical_runs_roots=logical_runs_roots,
    )
    if (
        payload.get("entry_count") != len(current.entries)
        or payload.get("external_alias_entry_count")
        != current.external_alias_entry_count
        or payload.get("boundary_sha256") != current.boundary_sha256
        or current.changed_entry_count != 0
    ):
        raise EvidencePermissionError("权限收口后的证据元数据边界漂移。")
    return payload
