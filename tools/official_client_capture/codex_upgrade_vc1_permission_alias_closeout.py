#!/usr/bin/env python3
"""通过已冻结的可写宿主别名收口本次 VC-1 官方证据权限。

本工具只服务于 0.154.0 首轮正式 Campaign 的 sequence 4 预派发失败后继。证据仍以
``/root/oauth-capture/runs`` 下的只读逻辑路径作为身份；只有逐项证明宿主
``runs`` 别名指向同一 inode 后，才允许经可写别名执行 ``fchmod``。工具不读取
证据正文、不发送网络请求，也不创建或改写 seal 制品。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


CAMPAIGN_ID = "c0154-formal-vc1-bwg-new-window-20260914t100818z"
ATTEMPT_ID = "20260914T102852Z-04996800fbbe4e94"
ATTEMPT_SHA256 = "2fe5677bdc05dda3b8089ca07fa176aa76f07499867f2ca64785dc73769dd0c7"
ROOTS_SHA256 = "ae45b6f54c7d2333a5cdf80c5a42aa8510bc2f0083df381c2a3ed53b1116fe2d"
EXPECTED_ENTRY_COUNT = 1882
EXPECTED_GAP_COUNT = 15
EXPECTED_GAP_SHA256 = "2b69ee039891bf6c58b6c787af105b9a896956b562cd0801c10ca7d2b36f2842"
HOST_DATA_ROOT = Path("/root/docker/capture-cli/data")
READONLY_RUNS_ROOT = Path("/root/oauth-capture/runs")
WRITABLE_RUNS_ROOT = HOST_DATA_ROOT / "runs"
TOOL_PATH = (
    HOST_DATA_ROOT
    / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_closeout.py"
)
SUPERVISOR_PATH = (
    HOST_DATA_ROOT / "tools/official_client_capture/codex_upgrade_supervisor.py"
)
ACTION_INPUT_DIR = HOST_DATA_ROOT / "control" / f"{CAMPAIGN_ID}-action-inputs"
RECEIPT_PATH = ACTION_INPUT_DIR / "sequence5-permission-alias-closeout-receipt.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PermissionAliasCloseoutError(RuntimeError):
    """权限别名边界或不可变输入不满足唯一恢复合同时抛出。"""


@dataclass(frozen=True)
class EntrySnapshot:
    """一个只读身份路径及其可写等价路径的冻结 inode 元数据。"""

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
    external_alias: bool


@dataclass(frozen=True)
class BoundarySnapshot:
    """一次完整元数据枚举结果；不包含任何证据正文。"""

    entries: tuple[EntrySnapshot, ...]
    gaps: tuple[EntrySnapshot, ...]
    boundary_sha256: str
    gap_sha256: str
    external_alias_entry_count: int


def canonical_bytes(value: Any) -> bytes:
    """生成不带结尾换行的规范 JSON 字节。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reject_symlink_components(path: Path, label: str) -> None:
    """拒绝绝对路径任一已存在组件为符号链接。"""

    if not path.is_absolute():
        raise PermissionAliasCloseoutError(f"{label} 必须是绝对路径。")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise PermissionAliasCloseoutError(f"{label} 无法读取：{current}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionAliasCloseoutError(f"{label} 包含符号链接：{current}")


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _validate_owned_directory(path: Path, label: str) -> os.stat_result:
    reject_symlink_components(path, label)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
    ):
        raise PermissionAliasCloseoutError(f"{label} 类型或属主漂移。")
    return metadata


def _is_readonly_mount(path: Path) -> bool:
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    return bool(os.statvfs(path).f_flag & readonly_flag)


def validate_alias_roots(
    readonly_runs_root: Path,
    writable_runs_root: Path,
    *,
    require_mount_modes: bool,
) -> None:
    """证明逻辑 runs 根与宿主 runs 根是同一 inode 且写属性符合合同。"""

    readonly_metadata = _validate_owned_directory(
        readonly_runs_root,
        "只读逻辑 runs 根",
    )
    writable_metadata = _validate_owned_directory(
        writable_runs_root,
        "可写宿主 runs 根",
    )
    if _metadata_identity(readonly_metadata) != _metadata_identity(writable_metadata):
        raise PermissionAliasCloseoutError("只读与可写 runs 根不是同一 inode。")
    if require_mount_modes and (
        not _is_readonly_mount(readonly_runs_root)
        or _is_readonly_mount(writable_runs_root)
    ):
        raise PermissionAliasCloseoutError("runs 双别名的只读／可写挂载属性漂移。")


def _entry_snapshot(
    read_path: Path,
    write_path: Path,
    *,
    external_alias: bool,
) -> EntrySnapshot:
    """逐项验证双别名类型、属主和完整 inode 边界。"""

    reject_symlink_components(read_path, "证据只读身份路径")
    reject_symlink_components(write_path, "证据可写别名路径")
    read_metadata = read_path.lstat()
    write_metadata = write_path.lstat()
    if (
        read_metadata.st_uid != os.geteuid()
        or read_metadata.st_gid != os.getegid()
        or write_metadata.st_uid != os.geteuid()
        or write_metadata.st_gid != os.getegid()
    ):
        raise PermissionAliasCloseoutError(f"证据项属主漂移：{read_path}")
    if stat.S_ISDIR(read_metadata.st_mode):
        kind = "directory"
        target_mode = stat.S_IMODE(read_metadata.st_mode) & 0o700
        same_type = stat.S_ISDIR(write_metadata.st_mode)
    elif stat.S_ISREG(read_metadata.st_mode):
        kind = "file"
        target_mode = stat.S_IMODE(read_metadata.st_mode) & 0o600
        same_type = stat.S_ISREG(write_metadata.st_mode)
        if read_metadata.st_nlink != 1 or write_metadata.st_nlink != 1:
            raise PermissionAliasCloseoutError(
                f"证据文件存在边界外硬链接风险：{read_path}"
            )
    else:
        raise PermissionAliasCloseoutError(f"证据边界包含特殊文件：{read_path}")
    if not same_type or _metadata_identity(read_metadata) != _metadata_identity(
        write_metadata
    ):
        raise PermissionAliasCloseoutError(f"证据双别名 inode 边界不一致：{read_path}")
    return EntrySnapshot(
        read_path=read_path,
        write_path=write_path,
        kind=kind,
        mode=stat.S_IMODE(read_metadata.st_mode),
        target_mode=target_mode,
        device=read_metadata.st_dev,
        inode=read_metadata.st_ino,
        size=read_metadata.st_size,
        mtime_ns=read_metadata.st_mtime_ns,
        nlink=read_metadata.st_nlink,
        external_alias=external_alias,
    )


def _walk_paths(root: Path) -> list[Path]:
    """确定性枚举目录树；任何遍历错误都必须失败关闭。"""

    paths = [root]

    def onerror(error: OSError) -> None:
        raise PermissionAliasCloseoutError(f"证据树枚举失败：{root}") from error

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


def _gap_record(entry: EntrySnapshot) -> dict[str, str]:
    return {
        "kind": entry.kind,
        "mode": format(entry.mode, "04o"),
        "path": str(entry.read_path),
        "target_mode": format(entry.target_mode, "04o"),
    }


def _boundary_record(entry: EntrySnapshot) -> dict[str, Any]:
    """生成不含权限位的稳定边界，允许 fchmod 后逐项复核。"""

    return {
        "read_path": str(entry.read_path),
        "write_path": str(entry.write_path),
        "kind": entry.kind,
        "device": entry.device,
        "inode": entry.inode,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
        "nlink": entry.nlink,
        "external_alias": entry.external_alias,
    }


def _external_root_belongs_to_campaign(relative: Path, campaign_id: str) -> bool:
    """只接受本次冻结清单实际使用的两种外部根命名。"""

    parts = relative.parts
    direct_root = len(parts) == 1 and parts[0].startswith(f"{campaign_id}-")
    oauth_roots = {
        f"oauth-{campaign_id}",
        f"oauth-{campaign_id}-ws-repeat",
    }
    nested_oauth_root = (
        len(parts) == 3
        and parts[:2] == ("official-client", "oauth")
        and parts[2] in oauth_roots
    )
    return direct_root or nested_oauth_root


def inspect_permission_boundary(
    *,
    attempt_path: Path,
    campaign_id: str,
    attempt_id: str,
    attempt_sha256: str,
    roots_sha256: str,
    readonly_runs_root: Path,
    writable_runs_root: Path,
    require_mount_modes: bool,
    expected_entry_count: int | None = None,
    expected_gap_count: int | None = None,
    expected_gap_sha256: str | None = None,
) -> BoundarySnapshot:
    """校验 attempt、32 根和每个双别名 inode，只读取元数据。"""

    reject_symlink_components(attempt_path, "attempt")
    if not attempt_path.is_file() or file_sha256(attempt_path) != attempt_sha256:
        raise PermissionAliasCloseoutError("attempt 路径、类型或摘要漂移。")
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PermissionAliasCloseoutError("attempt 不是有效 UTF-8 JSON。") from error
    results = attempt.get("results")
    root_values = attempt.get("evidence_roots")
    if (
        attempt.get("campaign_id") != campaign_id
        or attempt.get("attempt_id") != attempt_id
        or attempt.get("status") != "awaiting_receipts"
        or not isinstance(results, list)
        or len(results) != 29
        or any(
            not isinstance(item, Mapping) or item.get("status") != "complete"
            for item in results
        )
        or not isinstance(root_values, list)
        or len(root_values) != 32
        or any(not isinstance(value, str) for value in root_values)
        or len(set(root_values)) != 32
        or sha256_bytes(canonical_bytes(root_values)) != roots_sha256
    ):
        raise PermissionAliasCloseoutError("attempt 身份、Job 终态或 32 根漂移。")

    validate_alias_roots(
        readonly_runs_root,
        writable_runs_root,
        require_mount_modes=require_mount_modes,
    )
    attempt_root = attempt_path.parent
    internal_roots = {attempt_root / "evidence", attempt_root / "logs"}
    result_roots: list[Path] = []
    for result in results:
        values = result.get("evidence_roots")
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) for value in values)
        ):
            raise PermissionAliasCloseoutError("Job evidence_roots 字段不闭合。")
        result_roots.extend(Path(value) for value in values)
    roots = [Path(value) for value in root_values]
    if set(result_roots) | internal_roots != set(roots):
        raise PermissionAliasCloseoutError("Job 根与 attempt 顶层证据根不闭合。")

    root_aliases: list[tuple[Path, Path, bool]] = []
    for index, root in enumerate(roots):
        if not root.is_absolute():
            raise PermissionAliasCloseoutError("证据根不是绝对路径。")
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise PermissionAliasCloseoutError("冻结证据根发生嵌套。")
        if root in internal_roots:
            write_root = root
            external_alias = False
        else:
            try:
                relative = root.relative_to(readonly_runs_root)
            except ValueError as error:
                raise PermissionAliasCloseoutError(
                    f"外部证据根越出只读 runs 根：{root}"
                ) from error
            if not _external_root_belongs_to_campaign(relative, campaign_id):
                raise PermissionAliasCloseoutError(
                    f"外部证据根不属于冻结 Campaign：{root}"
                )
            write_root = writable_runs_root / relative
            external_alias = True
        _entry_snapshot(root, write_root, external_alias=external_alias)
        root_aliases.append((root, write_root, external_alias))

    entries: list[EntrySnapshot] = []
    seen_read_paths: set[Path] = set()
    for read_root, write_root, external_alias in root_aliases:
        for read_path in _walk_paths(read_root):
            if read_path in seen_read_paths:
                raise PermissionAliasCloseoutError(f"证据项被重复枚举：{read_path}")
            seen_read_paths.add(read_path)
            relative = read_path.relative_to(read_root)
            write_path = write_root / relative
            entries.append(
                _entry_snapshot(
                    read_path,
                    write_path,
                    external_alias=external_alias,
                )
            )
    entries.sort(key=lambda item: str(item.read_path))
    gaps = tuple(item for item in entries if item.mode != item.target_mode)
    boundary_sha256 = sha256_bytes(
        canonical_bytes([_boundary_record(item) for item in entries])
    )
    gap_sha256 = sha256_bytes(canonical_bytes([_gap_record(item) for item in gaps]))
    snapshot = BoundarySnapshot(
        entries=tuple(entries),
        gaps=gaps,
        boundary_sha256=boundary_sha256,
        gap_sha256=gap_sha256,
        external_alias_entry_count=sum(item.external_alias for item in entries),
    )
    if expected_entry_count is not None and len(snapshot.entries) != expected_entry_count:
        raise PermissionAliasCloseoutError("证据元数据项数量漂移。")
    if expected_gap_count is not None and len(snapshot.gaps) != expected_gap_count:
        raise PermissionAliasCloseoutError("待收口权限项数量漂移。")
    if expected_gap_sha256 is not None and snapshot.gap_sha256 != expected_gap_sha256:
        raise PermissionAliasCloseoutError("待收口权限项身份或模式漂移。")
    return snapshot


def _harden_entry(entry: EntrySnapshot) -> None:
    """锁定可写别名 fd 后执行最小权限相与。"""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if entry.kind == "directory":
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(entry.write_path, flags)
    try:
        metadata = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if entry.kind == "directory" else stat.S_ISREG
        if (
            not expected_type(metadata.st_mode)
            or _metadata_identity(metadata)
            != (entry.device, entry.inode, entry.size, entry.mtime_ns, entry.nlink)
        ):
            raise PermissionAliasCloseoutError(
                f"权限收口前可写别名 inode 漂移：{entry.read_path}"
            )
        os.fchmod(descriptor, entry.target_mode)
    finally:
        os.close(descriptor)


def secure_write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    """以 O_EXCL 首次创建 0600 收据，并同步文件与父目录。"""

    parent = path.parent
    parent_metadata = _validate_owned_directory(parent, "权限收据父目录")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise PermissionAliasCloseoutError("权限收据父目录必须是 0700。")
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def validate_deployment_receipt(
    path: Path,
    *,
    expected_sha256: str,
    expected_tool_files_sha256: str,
    expected_helper_sha256: str,
) -> dict[str, Any]:
    """绑定当前 helper、监督器与同一 ARM64 受管部署收据。"""

    if (
        path.parent != HOST_DATA_ROOT / "control"
        or not path.name.startswith("codex-0154-supervisor-enable-")
        or not path.name.endswith(".json")
    ):
        raise PermissionAliasCloseoutError("部署收据不在唯一受管坐标。")
    reject_symlink_components(path, "部署收据")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or file_sha256(path) != expected_sha256
    ):
        raise PermissionAliasCloseoutError("部署收据类型、权限或摘要漂移。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PermissionAliasCloseoutError("部署收据不是有效 UTF-8 JSON。") from error
    if (
        payload.get("schema_version") != "codex-arm64-supervisor-enable/v1"
        or payload.get("status") != "passed"
        or payload.get("architecture") != "aarch64"
        or payload.get("production_tool_root")
        != str(HOST_DATA_ROOT / "tools/official_client_capture")
        or payload.get("tool_files_sha256") != expected_tool_files_sha256
        or payload.get("supervisor_sha256") != file_sha256(SUPERVISOR_PATH)
        or file_sha256(TOOL_PATH) != expected_helper_sha256
    ):
        raise PermissionAliasCloseoutError("部署收据没有绑定当前受管工具。")
    return payload


def apply_closeout(
    *,
    attempt_path: Path,
    campaign_id: str,
    attempt_id: str,
    attempt_sha256: str,
    roots_sha256: str,
    readonly_runs_root: Path,
    writable_runs_root: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    """完成 15 项权限收口，从只读身份路径复核并发布唯一收据。"""

    before = inspect_permission_boundary(
        attempt_path=attempt_path,
        campaign_id=campaign_id,
        attempt_id=attempt_id,
        attempt_sha256=attempt_sha256,
        roots_sha256=roots_sha256,
        readonly_runs_root=readonly_runs_root,
        writable_runs_root=writable_runs_root,
        require_mount_modes=True,
        expected_entry_count=EXPECTED_ENTRY_COUNT,
        expected_gap_count=EXPECTED_GAP_COUNT,
        expected_gap_sha256=EXPECTED_GAP_SHA256,
    )
    for entry in before.gaps:
        _harden_entry(entry)
    after = inspect_permission_boundary(
        attempt_path=attempt_path,
        campaign_id=campaign_id,
        attempt_id=attempt_id,
        attempt_sha256=attempt_sha256,
        roots_sha256=roots_sha256,
        readonly_runs_root=readonly_runs_root,
        writable_runs_root=writable_runs_root,
        require_mount_modes=True,
        expected_entry_count=EXPECTED_ENTRY_COUNT,
        expected_gap_count=0,
        expected_gap_sha256=sha256_bytes(canonical_bytes([])),
    )
    if before.boundary_sha256 != after.boundary_sha256:
        raise PermissionAliasCloseoutError("权限收口期间证据名称或 inode 边界漂移。")
    core = {
        "schema_version": "codex-upgrade-evidence-permission-alias-closeout/v1",
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "attempt_sha256": attempt_sha256,
        "roots_sha256": roots_sha256,
        "readonly_runs_root": str(readonly_runs_root),
        "writable_runs_root": str(writable_runs_root),
        "entry_count": len(after.entries),
        "external_alias_entry_count": after.external_alias_entry_count,
        "changed_count": len(before.gaps),
        "before_gap_sha256": before.gap_sha256,
        "stable_boundary_sha256": before.boundary_sha256,
        "changes": [_gap_record(entry) for entry in before.gaps],
        "scanned_bytes": 0,
        "live_request_count": 0,
        "completed_at_utc": utc_now(),
        "status": "passed",
    }
    receipt = {**core, "receipt_sha256": sha256_bytes(canonical_bytes(core))}
    secure_write_receipt(receipt_path, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="收口 0.154 VC-1 唯一失败现场的证据权限",
    )
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--attempt-sha256", required=True)
    parser.add_argument("--roots-sha256", required=True)
    parser.add_argument("--self-sha256", required=True)
    parser.add_argument("--readonly-runs-root", required=True, type=Path)
    parser.add_argument("--writable-runs-root", required=True, type=Path)
    parser.add_argument("--deployment-receipt", required=True, type=Path)
    parser.add_argument("--deployment-receipt-sha256", required=True)
    parser.add_argument("--tool-files-sha256", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    self_path = Path(__file__).absolute()
    self_metadata = self_path.lstat()
    if (
        self_path != TOOL_PATH
        or stat.S_IMODE(self_metadata.st_mode) & 0o022
        or self_metadata.st_uid != os.geteuid()
        or self_metadata.st_gid != os.getegid()
        or self_metadata.st_nlink != 1
        or not SHA256_RE.fullmatch(arguments.self_sha256)
        or file_sha256(self_path) != arguments.self_sha256
    ):
        raise PermissionAliasCloseoutError("权限收口 helper 身份、属主或摘要漂移。")
    expected_attempt_path = (
        HOST_DATA_ROOT
        / "evidence/campaigns"
        / CAMPAIGN_ID
        / "official/attempts"
        / ATTEMPT_ID
        / "attempt.json"
    )
    if (
        arguments.campaign_id != CAMPAIGN_ID
        or arguments.attempt_id != ATTEMPT_ID
        or arguments.attempt != expected_attempt_path
        or arguments.attempt_sha256 != ATTEMPT_SHA256
        or arguments.roots_sha256 != ROOTS_SHA256
        or arguments.readonly_runs_root != READONLY_RUNS_ROOT
        or arguments.writable_runs_root != WRITABLE_RUNS_ROOT
        or arguments.receipt != RECEIPT_PATH
        or not SHA256_RE.fullmatch(arguments.deployment_receipt_sha256)
        or not SHA256_RE.fullmatch(arguments.tool_files_sha256)
    ):
        raise PermissionAliasCloseoutError("权限收口命令坐标或冻结身份漂移。")
    validate_deployment_receipt(
        arguments.deployment_receipt,
        expected_sha256=arguments.deployment_receipt_sha256,
        expected_tool_files_sha256=arguments.tool_files_sha256,
        expected_helper_sha256=arguments.self_sha256,
    )
    receipt = apply_closeout(
        attempt_path=arguments.attempt,
        campaign_id=arguments.campaign_id,
        attempt_id=arguments.attempt_id,
        attempt_sha256=arguments.attempt_sha256,
        roots_sha256=arguments.roots_sha256,
        readonly_runs_root=arguments.readonly_runs_root,
        writable_runs_root=arguments.writable_runs_root,
        receipt_path=arguments.receipt,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
