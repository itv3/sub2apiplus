#!/usr/bin/env python3
"""收口 Codex VC-0，并立即派发首个 VC-1 批次；中断或失败后用同一命令续作（E2-07）。

本工具是 Formal Campaign 的唯一创建入口。它从已冻结的 preflight Campaign
恢复全部 ``plan`` 参数，重放四份 VC-0 输入，完成计时事件、Formal Campaign 创建和
``campaign-run`` 派发。任一步失败都会留下不可覆盖诊断；工具不会删除半成品、延长原始
deadline 或自行重试。

可重入（E2-07）：同一个 Formal ID、同一本计时账本，重跑同一条命令即续作。每次执行先做只读
现场判定（Formal 未建／已建未派发／已派发），再按判定走对应的路：收据副本、建 Formal、控制产物
副本这一段包在账本的收口 attempt 里（失败记 attempt 失败与根因，VC-0 保持打开）；Formal 建成后
不重签、不重建，只补缺的事件后派发首批；首批已派发则不再派发、不重跑收口，交给 VC-1 已有的
对账与恢复链。已写的事件不改，已经成功的请求不重发。
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from tools.official_client_capture import certify_release
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_root_cause
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt


CLOSEOUT_RECEIPT_SCHEMA = "codex-upgrade-vc0-closeout-receipt/v1"
CLOSEOUT_DIAGNOSTIC_SCHEMA = "codex-upgrade-vc0-closeout-diagnostic/v1"
LIVE_REQUEST_AUDIT_SCHEMA = "codex-upgrade-live-request-audit/v1"
DEADLINE_ORPHAN_LIVE_REQUEST_AUDIT_SCHEMA = (
    "codex-upgrade-deadline-orphan-live-request-audit/v1"
)
CAMPAIGN_RUN_REHEARSAL_SCHEMA = "codex-p0-campaign-run-rehearsal/v1"
MANAGED_TOOL_DEPLOY_SCHEMA = "codex-arm64-supervisor-enable/v1"
MINIMUM_REMAINING_SECONDS = 300
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EXPECTED_MANAGED_DOCUMENTS = (
    "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
)
# C2：VC-0 只重放四份必需输入；Job rehearsal、campaign-run rehearsal 与 atomic-double
# 已合成进发布认证（tool-release-certification/v1），由发布认证绑定并在其签发时重放。
INPUT_ROLES = (
    "arm64_environment",
    "managed_tool_deploy",
    "p0_gate",
    "release_certification",
)
PRE_REQUEST_FAILURE_MARKERS = (
    b"Read-only file system",
    "宿主与容器 runs 根不同源".encode("utf-8"),
    b"CAPTURE_HOST_DATA_ROOT",
)
FAILED_CLOSEOUT_CLOSURE_REPAIR_MESSAGE = (
    "official-relay-oauth-refresh 已开始但没有可闭合的 live 请求计数来源"
)
# E2-07：收口续作的现场判定、上限后恢复与首批交接记录。
SITE_SCHEMA = "codex-upgrade-vc0-closeout-site/v1"
LEDGER_RESUME_PREVIEW_SCHEMA = "codex-upgrade-vc0-ledger-resume-preview/v1"
VC1_HANDOVER_SCHEMA = "codex-upgrade-vc0-vc1-handover/v1"
SITE_KINDS = ("fresh", "pre-formal", "formal-built", "dispatched", "inconsistent")
# 收口 attempt 被硬杀（进程不在了、attempt 仍是进行中）时记失败用的步骤名；与其它步骤同用
# vc0-closeout.step-failed 根因码（不新增根因码），同样计入同根因次数。
INTERRUPTED_STEP = "closeout-interrupted"
CLOSEOUT_ROOT_CAUSE_COMPONENT = "vc0-closeout"
CLOSEOUT_ROOT_CAUSE_CODE = "vc0-closeout.step-failed"
# 派发首批之后的失败归 VC-1：监督器的失败收口与对账链负责，收口不再追加计时事件。
VC1_OWNED_STEPS = frozenset({"dispatch-vc1", "write-closeout-receipt"})
# 收口续作需要人工批准或处置时的退出码（入口编排器据此标「被阻塞」，打印下一条命令）。
EXIT_NEEDS_OPERATOR = 3
FIX_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class VC0CloseoutError(RuntimeError):
    """VC-0 收口输入、写入或首批派发未闭合。"""


class CloseoutBlocked(VC0CloseoutError):
    """续作需要人工批准或处置（不是失败）：带种类、下一条命令与诊断明细。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        next_command: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.next_command = next_command
        self.details = dict(details or {})


@dataclass(frozen=True)
class ReceiptSource:
    """一份已重放收据的不可变源坐标。"""

    role: str
    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ValidatedInputs:
    """创建 Formal Campaign 前完成联合校验的全部输入。"""

    preflight_dir: Path
    preflight_manifest: dict[str, Any]
    timing_ledger_dir: Path
    arm64_root: Path
    arm64_receipt: Path
    job_rehearsal_root: Path
    job_rehearsal_receipt: Path
    p0_gate_root: Path
    p0_gate_receipt: Path
    release_certification: Path
    receipts: tuple[ReceiptSource, ...]
    timing_summary: dict[str, Any]


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    """从不可跟随符号链接的单一文件描述符计算稳定摘要。"""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise VC0CloseoutError(f"无法安全打开摘要输入：{path}") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise VC0CloseoutError(f"摘要输入不是普通文件：{path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
        if _file_stat_identity(before) != _file_stat_identity(after):
            raise VC0CloseoutError(f"摘要输入在读取期间发生变化：{path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _file_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """返回足以检测一次读取期间替换或改写的文件身份。"""

    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_file(
    path: Path,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> bytes:
    """通过 O_NOFOLLOW 描述符读取一次有大小上限的稳定普通文件。"""

    source = _trusted_file(path, label, maximum=maximum, allow_empty=allow_empty)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise VC0CloseoutError(f"{label}无法安全打开") from error
    minimum = 0 if allow_empty else 1
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not minimum <= before.st_size <= maximum
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise VC0CloseoutError(f"{label}文件身份或权限不可信")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or len(raw) > maximum
            or _file_stat_identity(before) != _file_stat_identity(after)
        ):
            raise VC0CloseoutError(f"{label}在读取期间发生变化")
        return raw
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise VC0CloseoutError(f"{label}不是 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VC0CloseoutError(f"{label}不是 RFC3339 时间") from error
    if parsed.tzinfo is None:
        raise VC0CloseoutError(f"{label}缺少时区")
    return parsed


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise VC0CloseoutError(f"{label}不是安全标识")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise VC0CloseoutError(f"{label}不是小写 SHA-256")
    return value


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise VC0CloseoutError(
            f"{label}字段不闭合：缺少={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return dict(value)


def _private_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise VC0CloseoutError(f"{label}必须是可信绝对目录")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise VC0CloseoutError(f"{label}权限不得允许 group/other 访问")
    return resolved


def _trusted_directory(path: Path, label: str) -> Path:
    """接受私有目录或只读共享目录，但拒绝任何 group/other 写权限。"""

    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise VC0CloseoutError(f"{label}必须是可信绝对目录")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise VC0CloseoutError(f"{label}不得允许 group/other 写入")
    return resolved


def _new_private_directory(path: Path, label: str) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not path.name
        or path.is_symlink()
        or path.exists()
    ):
        raise VC0CloseoutError(f"{label}必须是尚不存在的绝对路径")
    parent = _private_directory(path.parent, f"{label}父目录")
    resolved = parent / path.name
    resolved.mkdir(mode=0o700)
    return resolved


def _trusted_file(
    path: Path,
    label: str,
    *,
    maximum: int = MAX_JSON_BYTES,
    allow_empty: bool = False,
) -> Path:
    """校验可信普通文件；默认拒绝零字节，只有日志类输入显式放行。

    ``allow_empty`` 只给 Formal Job 日志使用：deadline 到期或进程被杀时
    wrapper 可能合法地留下零字节日志，这不是伪造证据，不能因此让收口失败。
    JSON 收据仍然必须至少 1 字节。
    """

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise VC0CloseoutError(f"{label}必须是可信绝对普通文件")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    minimum = 0 if allow_empty else 1
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not minimum <= metadata.st_size <= maximum
    ):
        raise VC0CloseoutError(f"{label}大小、属主或权限非法")
    return resolved


def _inside(root: Path, value: Path | str, label: str) -> Path:
    root = root.resolve(strict=True)
    candidate = Path(value)
    if not candidate.is_absolute():
        pure = PurePosixPath(str(value))
        if (
            not pure.parts
            or "\\" in str(value)
            or str(pure) != str(value)
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise VC0CloseoutError(f"{label}不是规范相对路径")
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise VC0CloseoutError(f"{label}包含符号链接")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise VC0CloseoutError(f"{label}越过受管根") from error
    return _trusted_file(resolved, label, maximum=MAX_RECEIPT_BYTES)


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_stable_file(path, label, maximum=MAX_JSON_BYTES)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise VC0CloseoutError(f"{label}不是有效 JSON") from error
    if not isinstance(value, dict):
        raise VC0CloseoutError(f"{label}必须是 JSON 对象")
    return value, raw


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    """通过临时文件加硬链接发布不可覆盖 JSON。"""

    if path.exists() or path.is_symlink():
        raise VC0CloseoutError(f"不可变输出已经存在：{path}")
    parent = _private_directory(path.parent, "输出父目录")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise VC0CloseoutError(f"不可变输出已经存在：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_once(source: Path, destination: Path) -> ReceiptSource:
    """逐字节复制收据并以不可覆盖方式发布。"""

    source = _trusted_file(source, "收据复制源", maximum=MAX_RECEIPT_BYTES)
    if destination.exists() or destination.is_symlink():
        raise VC0CloseoutError(f"收据复制目标已经存在：{destination}")
    parent = _private_directory(destination.parent, "收据复制目标父目录")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    source_descriptor = -1
    try:
        os.fchmod(descriptor, 0o600)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        source_descriptor = os.open(source, flags)
        before = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= MAX_RECEIPT_BYTES
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise VC0CloseoutError("收据复制源文件身份或权限不可信")
        with os.fdopen(source_descriptor, "rb", closefd=False) as reader, os.fdopen(
            descriptor, "wb", closefd=False
        ) as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(source_descriptor)
        if size != before.st_size or _file_stat_identity(before) != _file_stat_identity(
            after
        ):
            raise VC0CloseoutError("收据复制源在读取期间发生变化")
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise VC0CloseoutError(
                f"收据复制目标已经存在：{destination}"
            ) from error
        destination.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        temporary.unlink(missing_ok=True)
    expected = digest.hexdigest()
    if _sha256_file(destination) != expected:
        raise VC0CloseoutError("收据复制后的摘要不一致")
    return ReceiptSource("", destination, expected, size)


@contextmanager
def _ledger_lock(root: Path) -> Iterator[None]:
    """串行化同一 UpgradeTimingLedger 的 VC-0 收口。"""

    lock_path = root / ".vc0-closeout.lock"
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise VC0CloseoutError("VC-0 收口锁文件不可信或无法创建") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise VC0CloseoutError("VC-0 收口锁文件身份不可信")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise VC0CloseoutError("同一时间账本已有 VC-0 收口在运行") from error
        yield
    finally:
        os.close(descriptor)


def _binding_source(role: str, path: Path) -> ReceiptSource:
    source = _trusted_file(path, f"{role} 收据", maximum=MAX_RECEIPT_BYTES)
    raw = _read_stable_file(
        source,
        f"{role} 收据",
        maximum=MAX_RECEIPT_BYTES,
    )
    return ReceiptSource(
        role=role,
        path=source,
        sha256=_sha256_bytes(raw),
        bytes=len(raw),
    )


def _bound_control_file(
    root: Path,
    binding: Any,
    label: str,
) -> Path:
    reference = _expect(binding, {"path", "sha256", "bytes"}, label)
    path = _inside(root, str(reference["path"]), label)
    if (
        _sha256_file(path) != _sha256(reference["sha256"], f"{label}.sha256")
        or path.stat().st_size != reference["bytes"]
    ):
        raise VC0CloseoutError(f"{label}摘要或大小漂移")
    return path


def _tool_entry_sha256(manifest: Mapping[str, Any], relative: str) -> str:
    identity = manifest.get("tool_identity")
    entries = identity.get("entries") if isinstance(identity, Mapping) else None
    if not isinstance(entries, list):
        raise VC0CloseoutError("preflight 缺少工具文件清单")
    matches = [
        item
        for item in entries
        if isinstance(item, Mapping) and item.get("path") == relative
    ]
    if len(matches) != 1:
        raise VC0CloseoutError(f"preflight 工具清单缺少唯一 {relative}")
    return _sha256(matches[0].get("sha256"), f"preflight {relative}")


def _load_preflight(path: Path) -> tuple[Path, dict[str, Any]]:
    root = _private_directory(path, "preflight Campaign")
    try:
        manifest = codex_upgrade.load_campaign_manifest(
            root,
            _skip_control_validation=True,
        )
    except (OSError, ValueError, codex_upgrade.ConfigurationError) as error:
        raise VC0CloseoutError(f"preflight Campaign 重放失败：{error}") from error
    if manifest.get("campaign_mode") != "preflight_only":
        raise VC0CloseoutError("VC-0 收口只能消费 preflight_only Campaign")
    target_version = str(manifest.get("target_version", ""))
    try:
        target_parts = tuple(int(part) for part in target_version.split("."))
    except ValueError as error:
        raise VC0CloseoutError("preflight target_version 非法") from error
    if len(target_parts) != 3 or target_parts < (0, 154, 0):
        raise VC0CloseoutError("原子 VC-0 收口只服务 0.154.0 及后续流程")
    return root, manifest


def _remaining_seconds(summary: Mapping[str, Any], now: str) -> int:
    observed = _timestamp(now, "当前时间")
    deadlines = [
        _timestamp(summary.get("total_deadline_at_utc"), "总 deadline")
    ]
    stage_deadline = summary.get("stage_deadline_at_utc")
    if stage_deadline is not None:
        deadlines.append(_timestamp(stage_deadline, "阶段 deadline"))
    return int((min(deadlines) - observed).total_seconds())


def _assert_active_vc0(
    summary: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    upgrade_id: str,
    evidence_decision: str,
    expected_total_live_request_count: int,
    now: str,
) -> None:
    expected_identity = {
        "upgrade_id": upgrade_id,
        "baseline_version": manifest.get("baseline_version"),
        "target_version": manifest.get("target_version"),
        "campaign_purpose": manifest.get("campaign_purpose"),
        "evidence_decision": evidence_decision,
    }
    if (
        any(summary.get(key) != value for key, value in expected_identity.items())
        or summary.get("status") != "active"
        or summary.get("active_phase") != "VC-0"
        or summary.get("total_live_request_count")
        != expected_total_live_request_count
    ):
        raise VC0CloseoutError("UpgradeTimingLedger 不是当前升级的 active VC-0")
    remaining = _remaining_seconds(summary, now)
    if remaining < MINIMUM_REMAINING_SECONDS:
        raise VC0CloseoutError(
            f"VC-0 剩余时间仅 {remaining} 秒，少于 {MINIMUM_REMAINING_SECONDS} 秒"
        )


def _validate_timing_and_arm64(
    preflight_dir: Path,
    manifest: Mapping[str, Any],
    *,
    now: str,
) -> tuple[Path, Path, Path, dict[str, Any], ReceiptSource]:
    controls = _expect(
        manifest.get("control_receipts"),
        {"upgrade_timing", "arm64_environment"},
        "preflight control_receipts",
    )
    timing = _expect(
        controls["upgrade_timing"],
        {
            "ledger_dir",
            "ledger_plan_sha256",
            "receipt",
            "upgrade_id",
            "evidence_decision",
            "checkpoint_head_sha256",
        },
        "preflight upgrade_timing",
    )
    timing_root = _private_directory(
        Path(str(timing["ledger_dir"])), "UpgradeTimingLedger"
    )
    timing_path = _bound_control_file(
        timing_root,
        timing["receipt"],
        "preflight timing checkpoint",
    )
    if _sha256_file(timing_root / "ledger.json") != _sha256(
        timing["ledger_plan_sha256"], "ledger_plan_sha256"
    ):
        raise VC0CloseoutError("UpgradeTimingLedger 计划摘要漂移")
    try:
        frozen = codex_upgrade_timing_ledger.replay(
            timing_root,
            timing_path.relative_to(timing_root).as_posix(),
        )
        summary = codex_upgrade_timing_ledger.inspect_ledger(
            timing_root,
            now=now,
        )
    except (
        OSError,
        ValueError,
        codex_upgrade_timing_ledger.TimingLedgerError,
    ) as error:
        raise VC0CloseoutError(f"UpgradeTimingLedger 重放失败：{error}") from error
    if (
        frozen.get("summary", {}).get("head_sha256")
        != timing["checkpoint_head_sha256"]
    ):
        raise VC0CloseoutError("preflight timing checkpoint head 漂移")
    frozen_summary = frozen.get("summary")
    if not isinstance(frozen_summary, Mapping):
        raise VC0CloseoutError("preflight timing checkpoint 缺少冻结摘要")
    frozen_live_requests = frozen_summary.get("total_live_request_count")
    if (
        not isinstance(frozen_live_requests, int)
        or isinstance(frozen_live_requests, bool)
        or frozen_live_requests < 0
    ):
        raise VC0CloseoutError("preflight timing checkpoint 请求累计值非法")
    _assert_active_vc0(
        summary,
        manifest,
        upgrade_id=str(timing["upgrade_id"]),
        evidence_decision=str(timing["evidence_decision"]),
        expected_total_live_request_count=frozen_live_requests,
        now=now,
    )

    arm = _expect(
        controls["arm64_environment"],
        {
            "evidence_root",
            "receipt",
            "subject_id",
            "contract_sha256",
            "continuity_identity_sha256",
        },
        "preflight arm64_environment",
    )
    arm_root = _private_directory(
        Path(str(arm["evidence_root"])), "ARM64 环境证据根"
    )
    arm_path = _bound_control_file(
        arm_root,
        arm["receipt"],
        "ARM64 环境收据",
    )
    try:
        arm_receipt = codex_upgrade_arm64_environment_receipt.replay(
            arm_root,
            arm_path.relative_to(arm_root).as_posix(),
        )
    except (
        OSError,
        codex_upgrade_arm64_environment_receipt.Arm64EnvironmentReceiptError,
    ) as error:
        raise VC0CloseoutError(f"ARM64 环境收据重放失败：{error}") from error
    if (
        arm_receipt.get("status") != "passed"
        or arm_receipt.get("phase") != "p0"
        or arm_receipt.get("subject_id") != summary["upgrade_id"]
        or arm_receipt.get("contract_sha256") != arm["contract_sha256"]
        or arm_receipt.get("continuity_identity_sha256")
        != arm["continuity_identity_sha256"]
    ):
        raise VC0CloseoutError("ARM64 环境收据与 preflight 或时间账本不一致")
    return (
        timing_root,
        arm_root,
        arm_path,
        summary,
        _binding_source("arm64_environment", arm_path),
    )


def _validate_job_rehearsal(
    preflight_dir: Path,
    manifest: Mapping[str, Any],
    root: Path,
    receipt_path: Path,
) -> tuple[Path, Path, dict[str, Any], ReceiptSource]:
    evidence_root = _private_directory(root, "Job rehearsal 证据根")
    path = _inside(evidence_root, receipt_path, "Job rehearsal 收据")
    try:
        receipt = codex_upgrade_job_rehearsal_receipt.replay(
            evidence_root,
            path.relative_to(evidence_root).as_posix(),
        )
        expected_contract = codex_upgrade._job_rehearsal_contract_from_manifest(
            preflight_dir,
            manifest,
        )
        codex_upgrade_job_rehearsal_receipt.assert_formal_compatible(
            receipt,
            expected_contract,
        )
    except (
        OSError,
        ValueError,
        codex_upgrade.ConfigurationError,
        codex_upgrade_job_rehearsal_receipt.JobRehearsalReceiptError,
    ) as error:
        raise VC0CloseoutError(f"Job rehearsal 收据重放失败：{error}") from error
    preflight = receipt.get("preflight_campaign")
    if (
        not isinstance(preflight, Mapping)
        or Path(str(preflight.get("path", ""))).resolve(strict=False)
        != preflight_dir
        or preflight.get("campaign_id") != manifest.get("campaign_id")
        or preflight.get("manifest_sha256")
        != _sha256_file(preflight_dir / "campaign.json")
        or preflight.get("campaign_mode") != "preflight_only"
        or not SHA256_RE.fullmatch(
            str(receipt.get("failure_lifecycle_probe_sha256", ""))
        )
    ):
        raise VC0CloseoutError("Job rehearsal 未绑定当前 preflight 或失败生命周期")
    return (
        evidence_root,
        path,
        receipt,
        _binding_source("job_rehearsal", path),
    )


def _validate_campaign_run_rehearsal(
    path: Path,
    preflight_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    receipt, _raw = _load_json(path, "campaign-run rehearsal 收据")
    value = _expect(
        receipt,
        {
            "schema_version",
            "status",
            "campaign_id",
            "inputs",
            "multi_batch_passed",
            "original_deadline_inherited",
            "deadline_drift_rejected",
            "live_request_count",
            "runs",
            "negative_fixture",
            "temporary_asset_inventory",
        },
        "campaign-run rehearsal 收据",
    )
    plan_binding = manifest.get("vc_control", {}).get("campaign_plan")
    checkpoint_binding = manifest.get("vc_control", {}).get("vc0_checkpoint")
    if not isinstance(plan_binding, Mapping) or not isinstance(
        checkpoint_binding, Mapping
    ):
        raise VC0CloseoutError("preflight 缺少 VC-0 计划或 checkpoint")
    plan_path = _inside(
        preflight_dir,
        str(plan_binding.get("path", "")),
        "preflight Campaign plan",
    )
    checkpoint_path = _inside(
        preflight_dir,
        str(checkpoint_binding.get("path", "")),
        "preflight VC-0 checkpoint",
    )
    plan_raw, _ = _load_json(plan_path, "preflight Campaign plan")
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(plan_raw)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise VC0CloseoutError(f"preflight Campaign plan 非法：{error}") from error
    inputs = _expect(
        value.get("inputs"),
        {
            "campaign_plan_sha256",
            "preflight_campaign",
            "tool_sha256",
            "vc0_checkpoint_file_sha256",
        },
        "campaign-run rehearsal inputs",
    )
    if (
        value.get("schema_version") != CAMPAIGN_RUN_REHEARSAL_SCHEMA
        or value.get("status") != "passed"
        or value.get("campaign_id") != manifest.get("campaign_id")
        or value.get("multi_batch_passed") is not True
        or value.get("original_deadline_inherited") is not True
        or value.get("deadline_drift_rejected") is not True
        or value.get("live_request_count") != 0
        or Path(str(inputs.get("preflight_campaign", ""))).resolve(strict=False)
        != preflight_dir
        or inputs.get("campaign_plan_sha256") != plan["plan_sha256"]
        or inputs.get("vc0_checkpoint_file_sha256")
        != _sha256_file(checkpoint_path)
        or inputs.get("tool_sha256")
        != _tool_entry_sha256(manifest, "codex_upgrade_supervisor.py")
    ):
        raise VC0CloseoutError("campaign-run rehearsal 身份或零请求事实非法")

    negative = _expect(
        value.get("negative_fixture"),
        {"kind", "rejected_before_action", "returncode", "stderr"},
        "campaign-run rehearsal negative_fixture",
    )
    if (
        negative.get("kind") != "original_deadline_drift"
        or negative.get("rejected_before_action") is not True
        or negative.get("returncode") == 0
        or "原始 deadline" not in str(negative.get("stderr", ""))
    ):
        raise VC0CloseoutError("campaign-run rehearsal deadline 负例未闭合")

    runs = value.get("runs")
    if not isinstance(runs, list) or len(runs) != 2:
        raise VC0CloseoutError("campaign-run rehearsal 必须有两个正式批次")
    normalized_runs = sorted(runs, key=lambda item: item.get("batch_sequence", 0))
    if [item.get("batch_sequence") for item in normalized_runs] != [1, 2]:
        raise VC0CloseoutError("campaign-run rehearsal 批次序号不连续")
    run_names: set[str] = set()
    batch_ids: set[str] = set()
    for index, raw_run in enumerate(normalized_runs, 1):
        run = _expect(
            raw_run,
            {
                "schema_version",
                "batch_id",
                "batch_sequence",
                "original_deadline_at_utc",
                "state",
                "audit_incomplete",
                "event_count",
                "execute_items",
                "reuse_items",
                "run_dir",
            },
            f"campaign-run rehearsal run {index}",
        )
        run_dir = _private_directory(
            Path(str(run["run_dir"])),
            f"campaign-run rehearsal run {index}",
        )
        try:
            audit = codex_upgrade_supervisor._audit_command(run_dir)
        except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
            raise VC0CloseoutError(
                f"campaign-run rehearsal run {index} 审计失败：{error}"
            ) from error
        execute_items = run.get("execute_items")
        reuse_items = run.get("reuse_items")
        event_count = run.get("event_count")
        if (
            run.get("schema_version") != "codex-upgrade-campaign-run/v2"
            or run.get("batch_sequence") != index
            or run.get("original_deadline_at_utc")
            != plan["original_deadline_at_utc"]
            or run.get("state") != "stopped"
            or run.get("audit_incomplete") is not False
            or not isinstance(event_count, int)
            or isinstance(event_count, bool)
            or event_count < 1
            or not isinstance(execute_items, list)
            or not execute_items
            or not isinstance(reuse_items, list)
            or not reuse_items
            or set(execute_items) & set(reuse_items)
            or audit.get("state") != "stopped"
            or audit.get("audit_incomplete") is not False
            or audit.get("event_count") != event_count
        ):
            raise VC0CloseoutError(
                f"campaign-run rehearsal run {index} 未形成完整终态"
            )
        run_names.add(run_dir.name)
        batch_ids.add(_safe_id(run.get("batch_id"), f"run {index}.batch_id"))
    if len(run_names) != 2 or len(batch_ids) != 2:
        raise VC0CloseoutError("campaign-run rehearsal 重复使用 run 或 batch 身份")
    inventory = value.get("temporary_asset_inventory")
    if (
        not isinstance(inventory, list)
        or inventory != sorted(set(inventory))
        or not run_names.issubset(set(inventory))
    ):
        raise VC0CloseoutError("campaign-run rehearsal 临时资产 inventory 非法")
    return value


def _validate_p0_gate(
    root: Path,
    receipt_path: Path,
    manifest: Mapping[str, Any],
    timing_summary: Mapping[str, Any],
    release_certification: Path,
) -> tuple[Path, Path, ReceiptSource]:
    evidence_root = _private_directory(root, "P0 gate 证据根")
    path = _inside(evidence_root, receipt_path, "P0 gate 收据")
    try:
        receipt = codex_upgrade_vc_receipt.replay(
            evidence_root,
            path.relative_to(evidence_root).as_posix(),
        )
    except (OSError, codex_upgrade_vc_receipt.VCReceiptError) as error:
        raise VC0CloseoutError(f"P0 gate 收据重放失败：{error}") from error
    subject = receipt.get("subject")
    assertions = receipt.get("assertions")
    if (
        receipt.get("kind") != "p0_gate"
        or receipt.get("status") != "passed"
        or not isinstance(subject, Mapping)
        or subject.get("upgrade_id") != timing_summary.get("upgrade_id")
        or subject.get("campaign_id") is not None
        or subject.get("campaign_purpose") != manifest.get("campaign_purpose")
        or subject.get("baseline_version") != manifest.get("baseline_version")
        or subject.get("target_version") != manifest.get("target_version")
        or not isinstance(assertions, Mapping)
        or assertions.get("release_certification_sha256")
        != _sha256_file(release_certification)
    ):
        raise VC0CloseoutError("P0 gate 未绑定当前升级或发布认证")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list):
        raise VC0CloseoutError("P0 gate 缺少 evidence")
    by_role = {
        str(item.get("role")): item
        for item in evidence
        if isinstance(item, Mapping)
    }
    if set(by_role) != {
        "check_egress_spec",
        "release_certification",
        "rollback",
        "test_capture_tools",
    }:
        raise VC0CloseoutError("P0 gate evidence 角色不闭合")
    if by_role["release_certification"].get("sha256") != _sha256_file(release_certification):
        raise VC0CloseoutError("P0 gate evidence 未绑定发布认证字节")
    return (
        evidence_root,
        path,
        _binding_source("p0_gate", path),
    )


MANAGED_TOOL_DEPLOY_V2_FIELDS = frozenset(
    {"policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"}
)


def _deploy_receipt_identity_matches(
    receipt: Mapping[str, Any],
    current_identity: Mapping[str, Any],
    manifest_identity: Mapping[str, Any],
) -> bool:
    """A2-2 三向比：收据、当前受管树、Campaign 冻结身份。

    收据带 v2 字段时比当前有效 wire 身份与策略摘要（control／evidence 变化不阻断
    VC-0 收口）；历史收据只有整树摘要时退回整树三向相等。
    """

    if MANAGED_TOOL_DEPLOY_V2_FIELDS & set(receipt):
        for field in ("wire_producer_sha256", "policy_sha256"):
            if not (
                receipt.get(field)
                == current_identity.get(field)
                == manifest_identity.get(field)
            ):
                return False
        return True
    return (
        receipt.get("tool_files_sha256") == current_identity.get("files_sha256")
        and receipt.get("tool_files_sha256") == manifest_identity.get("files_sha256")
    )


def _validate_managed_tool_deploy(
    path: Path,
    manifest: Mapping[str, Any],
) -> tuple[ReceiptSource, dict[str, Any]]:
    receipt_path = _trusted_file(path, "受管工具部署收据")
    payload, raw = _load_json(receipt_path, "受管工具部署收据")
    # A2-2：新部署收据带策略 v2 五摘要；历史收据没有，两种集合都接受。
    v2_fields = (
        MANAGED_TOOL_DEPLOY_V2_FIELDS
        if isinstance(payload, Mapping) and MANAGED_TOOL_DEPLOY_V2_FIELDS & set(payload)
        else frozenset()
    )
    # B9：新部署收据只读记录项目总账 head 摘要（可为 null）；历史收据没有该字段。
    optional_fields = {"project_ledger"} if isinstance(payload, Mapping) and "project_ledger" in payload else set()
    receipt = _expect(
        payload,
        {
            "schema_version",
            "status",
            "campaign_id",
            "created_at_utc",
            "architecture",
            "production_tool_root",
            "production_doc_root",
            "tool_files_sha256",
            *v2_fields,
            *optional_fields,
            "supervisor_sha256",
            "assertion_preparer_sha256",
            "rollback_backup",
            "assertion_preparer_rollback_backup",
            "document_rollback_backup",
            "switched_archived_documents",
            "installed_runtime_documents",
            "supervisor_run_dir",
        },
        "受管工具部署收据",
    )
    _safe_id(receipt.get("campaign_id"), "部署 campaign_id")
    _timestamp(receipt.get("created_at_utc"), "部署时间")
    if raw != _canonical(payload):
        raise VC0CloseoutError("受管工具部署收据不是规范不可变 JSON")
    production_root = _trusted_directory(
        Path(str(receipt.get("production_tool_root", ""))),
        "生产工具根",
    )
    current_root = Path(__file__).resolve().parent
    current_identity = codex_upgrade._tool_identity()
    manifest_identity = manifest.get("tool_identity")
    if not isinstance(manifest_identity, Mapping):
        raise VC0CloseoutError("preflight 缺少受管工具身份")
    supervisor_path = production_root / "codex_upgrade_supervisor.py"
    assertion_preparer = production_root.parent / "prepare_assertion_bundle.sh"
    for label, raw_path, expected_type in (
        ("生产文档根", receipt.get("production_doc_root"), "directory"),
        ("工具回滚点", receipt.get("rollback_backup"), "directory"),
        (
            "assertion preparer 回滚点",
            receipt.get("assertion_preparer_rollback_backup"),
            "file",
        ),
        ("文档回滚点", receipt.get("document_rollback_backup"), "directory"),
        ("部署监督器 run", receipt.get("supervisor_run_dir"), "directory"),
    ):
        candidate = Path(str(raw_path or ""))
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or (expected_type == "directory" and not candidate.is_dir())
            or (expected_type == "file" and not candidate.is_file())
            or candidate.stat().st_mode & 0o022
        ):
            raise VC0CloseoutError(f"{label}不存在或不可信")
    switched = receipt.get("switched_archived_documents")
    installed = receipt.get("installed_runtime_documents")
    if (
        receipt.get("schema_version") != MANAGED_TOOL_DEPLOY_SCHEMA
        or receipt.get("status") != "passed"
        or receipt.get("architecture") != "aarch64"
        or platform.machine() != "aarch64"
        or production_root != current_root
        or not _deploy_receipt_identity_matches(receipt, current_identity, manifest_identity)
        or receipt.get("supervisor_sha256") != _sha256_file(supervisor_path)
        or not assertion_preparer.is_file()
        or assertion_preparer.is_symlink()
        or receipt.get("assertion_preparer_sha256")
        != _sha256_file(assertion_preparer)
        or switched != list(EXPECTED_MANAGED_DOCUMENTS)
        or not isinstance(installed, list)
        or installed != sorted(set(installed))
    ):
        raise VC0CloseoutError("受管工具部署收据未绑定当前生产工具树")
    run_dir = Path(str(receipt["supervisor_run_dir"]))
    try:
        run_state = codex_upgrade_supervisor._read_state(run_dir)
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise VC0CloseoutError(f"受管工具部署监督器审计失败：{error}") from error
    if (
        run_state.get("campaign_id") != receipt.get("campaign_id")
        or run_state.get("phase") != "bootstrap"
        or run_state.get("state") != "stopped"
        or audit.get("run_dir") != str(run_dir.resolve(strict=True))
        or audit.get("state") != "stopped"
        or audit.get("audit_incomplete") is not False
    ):
        raise VC0CloseoutError("受管工具部署监督器未形成完整终态")
    return _binding_source("managed_tool_deploy", receipt_path), dict(receipt)


def _validate_release_certification(
    path: Path,
    deploy_receipt: Mapping[str, Any],
    preflight_dir: Path,
    manifest: Mapping[str, Any],
) -> tuple[Path, Path, dict[str, Any], ReceiptSource, ReceiptSource]:
    """C2：重放发布认证，核对其五摘要与部署收据一致，并从中恢复 Job rehearsal 绑定。"""

    certification_path = _trusted_file(path, "发布认证")
    try:
        certification = certify_release.verify(certification_path)
    except (OSError, ValueError, certify_release.ReleaseCertificationError) as error:
        raise VC0CloseoutError(f"发布认证校验失败：{error}") from error
    identity = certification.get("identity") or {}
    if any(
        str(deploy_receipt.get(field)) != str(identity.get(field))
        for field in ("tool_files_sha256", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256")
    ) or deploy_receipt.get("policy_version") != certification.get("policy_version"):
        raise VC0CloseoutError("发布认证五摘要或策略版本与受管工具部署收据不一致")
    bound_deploy = certification.get("deployment_receipt") or {}
    if Path(str(bound_deploy.get("path", ""))).resolve(strict=False) != Path(str(deploy_receipt.get("_path", ""))).resolve(strict=False) and bound_deploy.get("sha256") != deploy_receipt.get("_sha256"):
        raise VC0CloseoutError("发布认证绑定的部署收据不是本次收口提供的部署收据")
    job_binding = certification.get("job_rehearsal") or {}
    job_root, job_path, job_receipt, job_source = _validate_job_rehearsal(
        preflight_dir,
        manifest,
        Path(str(job_binding.get("evidence_root", ""))),
        Path(str(job_binding.get("receipt", ""))),
    )
    if job_source.sha256 != job_binding.get("sha256"):
        raise VC0CloseoutError("发布认证绑定的 Job rehearsal 收据摘要漂移")
    return (
        job_root,
        job_path,
        job_receipt,
        job_source,
        _binding_source("release_certification", certification_path),
    )


def validate_inputs(
    *,
    preflight_campaign_dir: Path,
    p0_gate_root: Path,
    p0_gate_receipt: Path,
    managed_tool_deploy_receipt: Path,
    release_certification: Path,
    now: str | None = None,
) -> ValidatedInputs:
    """重放四份输入并返回创建 Formal 所需的规范坐标（C2）。"""

    observed = now or _utc_now()
    preflight_dir, manifest = _load_preflight(preflight_campaign_dir)
    (
        timing_root,
        arm_root,
        arm_path,
        timing_summary,
        arm_source,
    ) = _validate_timing_and_arm64(preflight_dir, manifest, now=observed)
    deploy_source, deploy_receipt = _validate_managed_tool_deploy(
        managed_tool_deploy_receipt,
        manifest,
    )
    deploy_receipt = {
        **deploy_receipt,
        "_path": str(deploy_source.path),
        "_sha256": deploy_source.sha256,
    }
    (
        job_root,
        job_path,
        _job_receipt,
        _job_source,
        release_source,
    ) = _validate_release_certification(
        release_certification,
        deploy_receipt,
        preflight_dir,
        manifest,
    )
    p0_root, p0_path, p0_source = _validate_p0_gate(
        p0_gate_root,
        p0_gate_receipt,
        manifest,
        timing_summary,
        release_source.path,
    )
    receipts = tuple(
        sorted(
            (arm_source, deploy_source, p0_source, release_source),
            key=lambda item: item.role,
        )
    )
    if tuple(item.role for item in receipts) != INPUT_ROLES:
        raise VC0CloseoutError("VC-0 四份输入角色不闭合")
    return ValidatedInputs(
        preflight_dir=preflight_dir,
        preflight_manifest=manifest,
        timing_ledger_dir=timing_root,
        arm64_root=arm_root,
        arm64_receipt=arm_path,
        job_rehearsal_root=job_root,
        job_rehearsal_receipt=job_path,
        p0_gate_root=p0_root,
        p0_gate_receipt=p0_path,
        release_certification=release_source.path,
        receipts=receipts,
        timing_summary=timing_summary,
    )


def recover_formal_plan_arguments(
    validated: ValidatedInputs,
    *,
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    timing_receipt: Path,
) -> argparse.Namespace:
    """只从 preflight 清单恢复完整 Formal ``plan`` 参数。"""

    manifest = validated.preflight_manifest
    configuration = _expect(
        manifest.get("configuration"),
        {
            "baseline_source",
            "target_source",
            "target_package",
            "baseline_evidence",
            "runtime_image",
            "model",
            "lite_model",
            "capture_root",
            "capture_container",
            "service_container",
            "keeper_container",
            "postgres_container",
            "redis_container",
            "capture_codex_bin",
            "relay_codex_bin",
            "capture_code_mode_host_bin",
            "relay_code_mode_host_bin",
            "codex_account_id",
            "api_key_id",
            "live_attestation_compose_dir",
            "live_attestation_compose_files",
        },
        "preflight configuration",
    )
    inputs = _expect(
        manifest.get("inputs"),
        {
            "baseline_rules",
            "discovery_scenarios",
            "target_discovery_scenarios",
            "extra_jobs",
        },
        "preflight inputs",
    )
    official = manifest.get("official_identity")
    package = official.get("package") if isinstance(official, Mapping) else None
    if not isinstance(package, Mapping):
        raise VC0CloseoutError("preflight 缺少官方 package 身份")

    def input_path(field: str) -> Path:
        reference = inputs[field]
        if not isinstance(reference, Mapping):
            raise VC0CloseoutError(f"preflight inputs.{field} 非法")
        return _inside(
            validated.preflight_dir,
            str(reference.get("path", "")),
            f"preflight inputs.{field}",
        )

    command = [
        "plan",
        "--campaign-dir",
        str(formal_campaign_dir),
        "--campaign-id",
        _safe_id(formal_campaign_id, "formal_campaign_id"),
        "--baseline-version",
        str(manifest.get("baseline_version", "")),
        "--target-version",
        str(manifest.get("target_version", "")),
        "--campaign-mode",
        "formal",
        "--campaign-purpose",
        str(manifest.get("campaign_purpose", "")),
        "--timing-ledger-dir",
        str(validated.timing_ledger_dir),
        "--timing-receipt",
        str(timing_receipt),
        "--arm64-environment-root",
        str(validated.arm64_root),
        "--arm64-environment-receipt",
        str(validated.arm64_receipt),
        "--job-rehearsal-root",
        str(validated.job_rehearsal_root),
        "--job-rehearsal-receipt",
        str(validated.job_rehearsal_receipt),
        "--p0-gate-root",
        str(validated.p0_gate_root),
        "--release-certification",
        str(validated.release_certification),
        "--p0-gate-receipt",
        str(validated.p0_gate_receipt),
        "--baseline-source",
        str(configuration["baseline_source"]),
        "--target-source",
        str(configuration["target_source"]),
        "--baseline-evidence",
        str(configuration["baseline_evidence"]),
        "--target-sha256",
        str(manifest.get("target_sha256", "")),
        "--target-package",
        str(configuration["target_package"]),
        "--target-package-sha256",
        str(package.get("asset_sha256", "")),
        "--target-code-mode-host-sha256",
        str(package.get("code_mode_host_sha256", "")),
        "--runtime-image",
        str(configuration["runtime_image"]),
        "--rule-manifest",
        str(input_path("baseline_rules")),
        "--scenario-manifest",
        str(input_path("discovery_scenarios")),
        "--target-scenario-manifest",
        str(input_path("target_discovery_scenarios")),
        "--suite",
        str(manifest.get("suite", "")),
        "--model",
        str(configuration["model"]),
        "--lite-model",
        str(configuration["lite_model"]),
        "--capture-root",
        str(configuration["capture_root"]),
        "--capture-container",
        str(configuration["capture_container"]),
        "--service-container",
        str(configuration["service_container"]),
        "--keeper-container",
        str(configuration["keeper_container"]),
        "--postgres-container",
        str(configuration["postgres_container"]),
        "--redis-container",
        str(configuration["redis_container"]),
        "--capture-codex-bin",
        str(configuration["capture_codex_bin"]),
        "--relay-codex-bin",
        str(configuration["relay_codex_bin"]),
        "--capture-code-mode-host-bin",
        str(configuration["capture_code_mode_host_bin"]),
        "--relay-code-mode-host-bin",
        str(configuration["relay_code_mode_host_bin"]),
        "--codex-account-id",
        str(configuration["codex_account_id"]),
        "--api-key-id",
        str(configuration["api_key_id"]),
        "--live-attestation-compose-dir",
        str(configuration["live_attestation_compose_dir"]),
        "--live-attestation-compose-files",
        str(configuration["live_attestation_compose_files"]),
    ]
    extra = inputs.get("extra_jobs")
    if extra is not None:
        command.extend(["--extra-jobs", str(input_path("extra_jobs"))])
    try:
        return codex_upgrade._build_parser().parse_args(command)
    except SystemExit as error:
        raise VC0CloseoutError("无法从 preflight 恢复完整 Formal plan 参数") from error


def _event_ids(root: Path) -> set[str]:
    try:
        events = codex_upgrade_timing_ledger._load_events(root)
    except (OSError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise VC0CloseoutError(f"无法预检时间账本 event：{error}") from error
    return {str(event.get("event_id")) for event, _raw in events}


def _precheck_outputs(
    validated: ValidatedInputs,
    *,
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    supervisor_state_dir: Path,
) -> Path:
    if (
        not formal_campaign_dir.is_absolute()
        or ".." in formal_campaign_dir.parts
        or formal_campaign_dir.is_symlink()
        or formal_campaign_dir.exists()
    ):
        raise VC0CloseoutError("Formal Campaign 路径必须尚不存在")
    _private_directory(formal_campaign_dir.parent, "Formal Campaign 父目录")
    if (
        not supervisor_state_dir.is_absolute()
        or ".." in supervisor_state_dir.parts
        or supervisor_state_dir.is_symlink()
        or supervisor_state_dir.exists()
    ):
        raise VC0CloseoutError("VC-1 supervisor state-dir 必须尚不存在")
    _private_directory(supervisor_state_dir.parent, "supervisor state-dir 父目录")
    receipts_root = _private_directory(
        validated.timing_ledger_dir / "receipts",
        "UpgradeTimingLedger receipts 根",
    )
    namespace_root = receipts_root / "vc0-closeout"
    receipt_root = namespace_root / formal_campaign_id
    if namespace_root.exists() or namespace_root.is_symlink():
        _private_directory(namespace_root, "VC-0 收口收据命名空间")
    if receipt_root.exists() or receipt_root.is_symlink():
        raise VC0CloseoutError("VC-0 收口收据目录已经存在，禁止重复写入")
    ids = _event_ids(validated.timing_ledger_dir)
    expected_ids = {
        f"{formal_campaign_id}-p0-receipts-passed",
        f"{formal_campaign_id}-vc0-completed",
        f"{formal_campaign_id}-vc1-started",
    }
    duplicate = sorted(ids & expected_ids)
    if duplicate:
        raise VC0CloseoutError(f"VC-0 收口 event_id 已存在：{duplicate}")
    return receipt_root


def _copy_inputs_to_ledger(
    validated: ValidatedInputs,
    receipt_root: Path,
) -> list[dict[str, str]]:
    ledger_root = _private_directory(
        validated.timing_ledger_dir,
        "UpgradeTimingLedger",
    )
    receipts_root = _private_directory(
        ledger_root / "receipts",
        "UpgradeTimingLedger receipts 根",
    )
    namespace_root = receipts_root / "vc0-closeout"
    if namespace_root.exists() or namespace_root.is_symlink():
        namespace_root = _private_directory(
            namespace_root,
            "VC-0 收口收据命名空间",
        )
    else:
        namespace_root = _new_private_directory(
            namespace_root,
            "VC-0 收口收据命名空间",
        )
    if receipt_root.parent.resolve(strict=True) != namespace_root:
        raise VC0CloseoutError("VC-0 收口收据目录越过受管命名空间")
    receipt_root = namespace_root / receipt_root.name
    _new_private_directory(receipt_root, "VC-0 收口收据目录")
    bindings: list[dict[str, str]] = []
    for source in validated.receipts:
        destination = receipt_root / f"{source.role}.json"
        copied = _copy_once(source.path, destination)
        if copied.sha256 != source.sha256 or copied.bytes != source.bytes:
            raise VC0CloseoutError(f"{source.role} 收据复制结果漂移")
        bindings.append(
            {
                "role": source.role,
                "path": destination.relative_to(ledger_root).as_posix(),
                "sha256": copied.sha256,
            }
        )
    return bindings


def _copy_formal_control_artifacts(
    validated: ValidatedInputs,
    formal_campaign_dir: Path,
    formal_manifest: Mapping[str, Any],
    receipt_root: Path,
) -> dict[str, dict[str, Any]]:
    ledger_root = _private_directory(
        validated.timing_ledger_dir,
        "UpgradeTimingLedger",
    )
    control = formal_manifest.get("vc_control")
    if not isinstance(control, Mapping):
        raise VC0CloseoutError("Formal Campaign 缺少 VC 控制制品")
    result: dict[str, dict[str, Any]] = {}
    for field, output_name in (
        ("campaign_plan", "formal-campaign-plan.json"),
        ("vc0_checkpoint", "formal-vc0-checkpoint.json"),
    ):
        reference = control.get(field)
        if not isinstance(reference, Mapping):
            raise VC0CloseoutError(f"Formal Campaign 缺少 {field}")
        source = _inside(
            formal_campaign_dir,
            str(reference.get("path", "")),
            f"Formal {field}",
        )
        if _sha256_file(source) != reference.get("sha256"):
            raise VC0CloseoutError(f"Formal {field} 摘要漂移")
        destination = receipt_root / output_name
        copied = _copy_once(source, destination)
        result[field] = {
            "path": destination.relative_to(ledger_root).as_posix(),
            "sha256": copied.sha256,
            "bytes": copied.bytes,
        }
    return result


def _formal_host_data_root(formal_campaign_dir: Path) -> Path:
    """从规范 ``<data>/evidence/campaigns/<id>`` 坐标恢复宿主数据根。"""

    campaign = formal_campaign_dir.resolve(strict=True)
    if (
        campaign.parent.name != "campaigns"
        or campaign.parent.parent.name != "evidence"
    ):
        raise VC0CloseoutError("Formal Campaign 不在规范宿主 evidence/campaigns 根")
    return _trusted_directory(
        campaign.parent.parent.parent,
        "Formal Campaign 宿主数据根",
    )


def _map_container_evidence_root(
    value: Any,
    *,
    capture_root: Path,
    host_data_root: Path,
) -> Path:
    """把冻结的容器证据坐标映射到同源宿主数据根，不要求目标已经存在。"""

    candidate = Path(str(value))
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise VC0CloseoutError("Job evidence root 不是规范绝对路径")
    try:
        relative = candidate.relative_to(capture_root)
    except ValueError as error:
        raise VC0CloseoutError("Job evidence root 越过冻结 CAPTURE_ROOT") from error
    mapped = host_data_root / relative
    try:
        mapped.relative_to(host_data_root)
    except ValueError as error:
        raise VC0CloseoutError("Job evidence root 越过宿主数据根") from error
    return mapped


def _failed_attempt_roots(base: Path) -> list[Path]:
    """返回一个冻结 root 及其不可变 ``.failed-attemptN`` 归档。"""

    candidates: list[tuple[int, Path]] = []
    if base.exists() or base.is_symlink():
        candidates.append((0, base))
    if base.parent.is_dir() and not base.parent.is_symlink():
        prefix = re.escape(base.name) + r"\.failed-attempt([1-9][0-9]*)"
        for path in base.parent.iterdir():
            match = re.fullmatch(prefix, path.name)
            if match is not None:
                candidates.append((int(match.group(1)), path))
    roots: list[Path] = []
    for _index, path in sorted(candidates):
        if path.is_symlink() or not path.is_dir():
            raise VC0CloseoutError(f"live 请求审计遇到不可信 evidence root：{path}")
        roots.append(path.resolve(strict=True))
    return roots


def _capture_manifest_live_requests(path: Path) -> int:
    """按既有 UpgradeTimingLedger 口径累计官方 case 的真实 turn。"""

    payload, _raw = _load_json(path, "官方 capture manifest")
    if payload.get("schema_version") != "official-client-capture/v1":
        raise VC0CloseoutError("官方 capture manifest schema 非预期")
    cases = payload.get("case_results")
    if not isinstance(cases, list):
        raise VC0CloseoutError("官方 capture manifest 缺少 case_results")
    count = 0
    for index, case in enumerate(cases):
        scenario = case.get("scenario_result") if isinstance(case, Mapping) else None
        turns = scenario.get("turn_count") if isinstance(scenario, Mapping) else None
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
            raise VC0CloseoutError(
                f"官方 capture manifest case_results[{index}].turn_count 非法"
            )
        count += turns
    return count


def _compact_summary_live_requests(path: Path) -> int:
    """累计 app-server compact 驱动已经完成并落盘的 turn 数。"""

    payload, _raw = _load_json(path, "官方 compact summary")
    turns = payload.get("turn_completed_count")
    if (
        payload.get("schema_version") != "codex-compact-capture/v1"
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns < 0
    ):
        raise VC0CloseoutError("官方 compact summary schema 或 turn 数非法")
    return turns


def _relay_live_request_sources(root: Path) -> list[dict[str, Any]]:
    """从 relay 客户端原始字节累计 HTTP／WS Responses 模型请求。"""

    relay_root = root / "relay"
    if not relay_root.exists():
        return []
    if relay_root.is_symlink() or not relay_root.is_dir():
        raise VC0CloseoutError("relay evidence root 不可信")
    # 复用模型条件收据的逐帧解析器；它同时覆盖 HTTP body 与 Upgrade 后 WS 帧，
    # 不把 /models 预热或 TCP 连接数误算成模型请求。
    from tools.official_client_capture import model_condition_receipts

    sources: list[dict[str, Any]] = []
    request_paths = sorted(relay_root.glob("conn*.client_to_upstream.bin"))
    if not request_paths:
        # relay 已成功启动但客户端尚未连接时不会产生 conn*.bin。绑定零连接
        # manifest，才能区分“请求数确定为 0”和“证据根损坏／未形成”。
        manifest_path = relay_root / "relay.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return []
        manifest, _raw = _load_json(manifest_path, "relay 零连接 manifest")
        if (
            manifest.get("schema_version") != "byte-relay/v1"
            or manifest.get("connections") != []
        ):
            raise VC0CloseoutError("relay 没有请求字节且零连接 manifest 非法")
        resolved = _trusted_file(
            manifest_path,
            "relay 零连接 manifest",
            maximum=MAX_JSON_BYTES,
        )
        return [
            {
                "kind": "relay_zero_connections",
                "path": str(resolved),
                "sha256": _sha256_file(resolved),
                "live_request_count": 0,
            }
        ]
    for path in request_paths:
        path = _trusted_file(
            path,
            "relay 客户端原始字节",
            maximum=512 * 1024 * 1024,
        )
        count = len(
            model_condition_receipts._responses_request_models(path.read_bytes())
        )
        # OAuth refresh 等正式场景会形成真实 relay 字节，但按本账本的
        # “模型 turn／Responses 请求”口径计数应为 0。零值来源仍须绑定文件
        # 摘要，否则失败收口会把“已观察且为零”误判成“没有计数来源”。
        sources.append(
            {
                "kind": "relay_responses_requests",
                "path": str(path),
                "sha256": _sha256_file(path),
                "live_request_count": count,
            }
        )
    return sources


def _job_logs(formal_campaign_dir: Path, job_id: str) -> list[Path]:
    """只读取当前 Campaign attempts 下与 Job ID 精确匹配的脱敏日志。"""

    attempts = formal_campaign_dir / "official" / "attempts"
    if not attempts.exists():
        return []
    if attempts.is_symlink() or not attempts.is_dir():
        raise VC0CloseoutError("Formal official attempts 根不可信")
    result: list[Path] = []
    pattern = re.compile(re.escape(job_id) + r"(?:-retry[0-9]+)?-[0-9]+\.log")
    for attempt in attempts.iterdir():
        logs = attempt / "logs"
        if attempt.is_symlink() or not attempt.is_dir() or not logs.exists():
            continue
        if logs.is_symlink() or not logs.is_dir():
            raise VC0CloseoutError("Formal attempt logs 根不可信")
        for path in logs.iterdir():
            if pattern.fullmatch(path.name):
                # deadline 到期或进程被杀时 wrapper 可能只留下零字节日志，
                # 这是合法现场，不能在收集阶段就把整次收口判死。
                result.append(_trusted_file(path, "Formal Job 日志", allow_empty=True))
    return sorted(result)


def _logs_prove_pre_request_failure(paths: list[Path]) -> bool:
    """仅接受 wrapper 在创建运行根之前写出的固定失败标记。"""

    if not paths:
        return False
    for path in paths:
        raw = _read_stable_file(
            path, "Formal Job 日志", maximum=MAX_JSON_BYTES, allow_empty=True
        )
        # 零字节日志读出空串，自然不含任何失败标记，只能得出“无法证明
        # 请求前失败”的保守结论，而不是抛错让收口中止。
        if not any(marker in raw for marker in PRE_REQUEST_FAILURE_MARKERS):
            return False
    return True


def build_live_request_audit(
    formal_campaign_dir: Path,
    *,
    formal_campaign_id: str,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """从 Formal 不可变证据核算已发生的模型请求，未知来源一律失败关闭。"""

    campaign_dir = _trusted_directory(formal_campaign_dir, "Formal Campaign")
    manifest, raw = _load_json(campaign_dir / "campaign.json", "Formal Campaign")
    if (
        manifest.get("campaign_id") != formal_campaign_id
        or manifest.get("campaign_mode") != "formal"
    ):
        raise VC0CloseoutError("live 请求审计的 Formal Campaign 身份不一致")
    digest_path = _trusted_file(campaign_dir / "campaign.sha256", "Campaign 摘要")
    expected_digest = digest_path.read_text(encoding="ascii").strip()
    if expected_digest != _sha256_bytes(raw):
        raise VC0CloseoutError("live 请求审计发现 Campaign 摘要漂移")
    jobs = manifest.get("jobs", [])
    if not isinstance(jobs, list):
        raise VC0CloseoutError("Formal Campaign jobs 非数组")
    configuration = manifest.get("configuration")
    if jobs and not isinstance(configuration, Mapping):
        raise VC0CloseoutError("Formal Campaign 缺少 configuration")
    if not jobs:
        return {
            "schema_version": LIVE_REQUEST_AUDIT_SCHEMA,
            "status": "complete",
            "campaign_id": formal_campaign_id,
            "observed_at_utc": observed_at_utc or _utc_now(),
            "counting_rule": "codex_model_turns_and_responses_requests/v1",
            "live_request_count": 0,
            "observed_job_ids": [],
            "pending_job_ids": [],
            "pre_request_zero_job_ids": [],
            "sources": [],
        }

    host_data_root = _formal_host_data_root(campaign_dir)
    capture_root = Path(str(configuration.get("capture_root", "")))
    if not capture_root.is_absolute() or capture_root == Path("/"):
        raise VC0CloseoutError("Formal Campaign CAPTURE_ROOT 非法")
    sources: list[dict[str, Any]] = []
    observed_jobs: set[str] = set()
    pending_jobs: set[str] = set()
    zero_jobs: set[str] = set()
    seen_sources: set[Path] = set()
    for item in jobs:
        if not isinstance(item, Mapping) or item.get("phase") != "official":
            continue
        job_id = str(item.get("id", ""))
        if not SAFE_ID_RE.fullmatch(job_id):
            raise VC0CloseoutError("Formal official Job ID 非法")
        evidence_roots = item.get("evidence_roots")
        if not isinstance(evidence_roots, list):
            raise VC0CloseoutError(f"{job_id} evidence_roots 非数组")
        roots: list[Path] = []
        for value in evidence_roots:
            base = _map_container_evidence_root(
                value,
                capture_root=capture_root,
                host_data_root=host_data_root,
            )
            roots.extend(_failed_attempt_roots(base))
        logs = _job_logs(campaign_dir, job_id)
        if not roots and not logs:
            pending_jobs.add(job_id)
            continue
        observed_jobs.add(job_id)
        supported = False
        for root in roots:
            manifest_path = root / "manifest.json"
            if manifest_path.is_file() and not manifest_path.is_symlink():
                resolved = manifest_path.resolve(strict=True)
                if resolved not in seen_sources:
                    count = _capture_manifest_live_requests(resolved)
                    sources.append(
                        {
                            "kind": "official_capture_turns",
                            "path": str(resolved),
                            "sha256": _sha256_file(resolved),
                            "live_request_count": count,
                        }
                    )
                    seen_sources.add(resolved)
                supported = True
            for summary_path in (
                root / "result" / "direct" / "summary.json",
                root / "result" / "mitm" / "summary.json",
            ):
                if summary_path.is_file() and not summary_path.is_symlink():
                    resolved = summary_path.resolve(strict=True)
                    if resolved not in seen_sources:
                        count = _compact_summary_live_requests(resolved)
                        sources.append(
                            {
                                "kind": "compact_completed_turns",
                                "path": str(resolved),
                                "sha256": _sha256_file(resolved),
                                "live_request_count": count,
                            }
                        )
                        seen_sources.add(resolved)
                    supported = True
            relay_sources = _relay_live_request_sources(root)
            if relay_sources:
                supported = True
            for source in relay_sources:
                resolved = Path(str(source["path"])).resolve(strict=True)
                if resolved not in seen_sources:
                    sources.append(source)
                    seen_sources.add(resolved)
        # HTTP fallback 的探针只在容器 localhost 返回受控响应，不转发上游；
        # 无 run root 且固定前置失败标记也证明脚本尚未启动任何客户端请求。
        if not supported:
            if job_id == "official-http-fallback" or _logs_prove_pre_request_failure(logs):
                zero_jobs.add(job_id)
            else:
                raise VC0CloseoutError(
                    f"{job_id} 已开始但没有可闭合的 live 请求计数来源"
                )
    sources.sort(key=lambda source: (str(source["path"]), str(source["kind"])))
    return {
        "schema_version": LIVE_REQUEST_AUDIT_SCHEMA,
        "status": "complete",
        "campaign_id": formal_campaign_id,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "counting_rule": "codex_model_turns_and_responses_requests/v1",
        "live_request_count": sum(
            int(source["live_request_count"]) for source in sources
        ),
        "observed_job_ids": sorted(observed_jobs),
        "pending_job_ids": sorted(pending_jobs),
        "pre_request_zero_job_ids": sorted(zero_jobs),
        "sources": sources,
    }


def _publish_failure_live_request_audit(
    timing_ledger_dir: Path,
    *,
    formal_campaign_id: str,
    audit: Mapping[str, Any],
    filename: str,
) -> dict[str, Any]:
    """把计数收据一次性写入既有 VC-0 收口命名空间。"""

    receipt_root = _private_directory(
        timing_ledger_dir / "receipts" / "vc0-closeout" / formal_campaign_id,
        "VC-0 收口收据目录",
    )
    path = receipt_root / filename
    _write_once(path, audit)
    return {
        "role": "live_request_accounting",
        "path": path.relative_to(timing_ledger_dir).as_posix(),
        "sha256": _sha256_file(path),
    }


def _close_failed_timing_stage(
    timing_ledger_dir: Path,
    *,
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    failed_step: str,
) -> dict[str, Any]:
    """在收口已写入账本后，以不可变事件关闭残留的 active 阶段。"""

    closeout_event_ids = {
        f"{formal_campaign_id}-p0-receipts-passed",
        f"{formal_campaign_id}-vc0-completed",
        f"{formal_campaign_id}-vc1-started",
    }
    written_ids = _event_ids(timing_ledger_dir) & closeout_event_ids
    if not written_ids:
        return {"status": "not-required", "event_id": None}
    summary = codex_upgrade_timing_ledger.inspect_ledger(timing_ledger_dir)
    if summary.get("status") == "stopped":
        return {"status": "already-stopped", "event_id": None}

    digest = _sha256_bytes(
        f"{formal_campaign_id}\0{failed_step}".encode("utf-8")
    )[:20]
    # 旧根因 ID 把 campaign_id 混进了摘要，同一步骤在每个新 Campaign 里都算
    # 新根因，同根因重试上限永远不触发。现在根因只由步骤名生成；含
    # campaign_id 的 digest 只用于事件 ID，保证本账本内唯一。
    root_cause_id = codex_upgrade_root_cause.structured_root_cause(
        component="vc0-closeout",
        stable_error_code="vc0-closeout.step-failed",
        failed_step=failed_step,
    )
    event_id = f"vc0-closeout-failure-{digest}"
    active_phase = summary.get("active_phase")
    next_action = (
        "保留全部不可变现场，按 Framework §5.3.4 从最后合法 checkpoint "
        "审计并恢复；不得重置原 deadline 或重发已通过请求"
    )
    audit = build_live_request_audit(
        formal_campaign_dir,
        formal_campaign_id=formal_campaign_id,
    )
    live_request_count = int(audit["live_request_count"])
    audit_binding = _publish_failure_live_request_audit(
        timing_ledger_dir,
        formal_campaign_id=formal_campaign_id,
        audit=audit,
        filename=f"failure-live-request-audit-{digest}.json",
    )
    if summary.get("status") == "stop_required":
        phase = (
            str(active_phase)
            if active_phase in codex_upgrade_timing_ledger.PHASE_ORDER
            else "VC-1"
            if f"{formal_campaign_id}-vc1-started" in written_ids
            else "VC-0"
        )
        closed = codex_upgrade_timing_ledger.append_event(
            timing_ledger_dir,
            event_id=event_id,
            phase=phase,
            event_type="stop_the_line",
            root_cause_id=root_cause_id,
            live_request_count=live_request_count,
            receipts=[audit_binding],
            next_action=next_action,
        )
        return {
            "status": "stop-the-line-recorded",
            "event_id": event_id,
            "head_sha256": closed.get("head_sha256"),
            "live_request_count": live_request_count,
            "live_request_audit": audit_binding,
        }
    accounting_event_id = f"vc0-closeout-live-audit-{digest}"
    accounting_phase = (
        str(active_phase)
        if active_phase in codex_upgrade_timing_ledger.PHASE_ORDER
        else "VC-1"
        if f"{formal_campaign_id}-vc1-started" in written_ids
        else "VC-0"
    )
    accounted = codex_upgrade_timing_ledger.append_event(
        timing_ledger_dir,
        event_id=accounting_event_id,
        phase=accounting_phase,
        event_type="receipt_passed",
        receipts=[audit_binding],
        live_request_count=live_request_count,
        next_action=next_action,
    )
    if active_phase not in codex_upgrade_timing_ledger.PHASE_ORDER:
        return {
            "status": "between-stages",
            "event_id": None,
            "head_sha256": accounted.get("head_sha256"),
            "live_request_count": live_request_count,
            "live_request_audit": audit_binding,
        }
    closed = codex_upgrade_timing_ledger.append_event(
        timing_ledger_dir,
        event_id=event_id,
        phase=str(active_phase),
        event_type="stage_abandoned",
        root_cause_id=root_cause_id,
        live_request_count=0,
        next_action=next_action,
    )
    return {
        "status": "stage-abandoned-recorded",
        "event_id": event_id,
        "phase": active_phase,
        "head_sha256": closed.get("head_sha256"),
        "live_request_count": live_request_count,
        "live_request_audit": audit_binding,
    }


def _write_failure(
    audit_dir: Path,
    *,
    step: str,
    error: BaseException,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
    timing_ledger_dir: Path | None,
    timing_failure_closure: Mapping[str, Any] | None,
    next_command: str | None = None,
) -> None:
    summary: dict[str, Any] | None = None
    formal_exists = formal_campaign_dir.exists() or formal_campaign_dir.is_symlink()
    supervisor_exists = (
        supervisor_state_dir.exists() or supervisor_state_dir.is_symlink()
    )
    if timing_ledger_dir is not None:
        try:
            summary = codex_upgrade_timing_ledger.inspect_ledger(
                timing_ledger_dir
            )
        except BaseException:
            summary = None
    payload = {
        "schema_version": CLOSEOUT_DIAGNOSTIC_SCHEMA,
        "status": "failed",
        "failed_at_utc": _utc_now(),
        "failed_step": step,
        "error_type": type(error).__name__,
        "message": str(error),
        "formal_campaign_path_exists": formal_exists,
        "supervisor_state_path_exists": supervisor_exists,
        "timing_summary": summary,
        "timing_failure_closure": (
            dict(timing_failure_closure)
            if timing_failure_closure is not None
            else None
        ),
        "deadline_extended": False,
        "cleanup_performed": False,
        # E2-07：收口可重入，修复后用同一 Formal ID、同一账本、新的审计目录重跑同一条命令续作。
        "next_action": (
            next_command
            if next_command
            else "保留 Formal Campaign 和 supervisor 现场；修复后用同一 Formal ID 重跑本命令续作"
            "（首批已派发时不再派发，按 VC-1 对账恢复链接着跑）"
            if formal_exists
            else "修复失败的输入后，沿用原时间账本与 deadline，用同一 Formal ID、新的审计目录重跑本命令续作"
        ),
    }
    try:
        _write_once(audit_dir / "failure.json", payload)
    except BaseException:
        # 原始异常优先；audit 目录和已有不可变文件仍保留现场。
        pass


def repair_failure_live_request_accounting(
    *,
    formal_campaign_dir: Path,
    timing_ledger_dir: Path,
    audit_dir: Path,
) -> dict[str, Any]:
    """只追加修复旧失败事件漏记的 live 请求量，不改写任何历史文件。"""

    os.umask(0o077)
    audit_root = _new_private_directory(audit_dir, "live 请求修复审计目录")
    request = {
        "schema_version": "codex-upgrade-live-request-accounting-repair-request/v1",
        "formal_campaign_dir": str(formal_campaign_dir),
        "timing_ledger_dir": str(timing_ledger_dir),
        "requested_at_utc": _utc_now(),
        "history_rewrite_allowed": False,
    }
    _write_once(audit_root / "request.json", request)
    try:
        campaign_dir = _trusted_directory(formal_campaign_dir, "Formal Campaign")
        campaign, _raw = _load_json(
            campaign_dir / "campaign.json",
            "Formal Campaign",
        )
        campaign_id = str(campaign.get("campaign_id", ""))
        if not SAFE_ID_RE.fullmatch(campaign_id):
            raise VC0CloseoutError("Formal Campaign ID 非法")
        ledger_root = _private_directory(timing_ledger_dir, "UpgradeTimingLedger")
        with _ledger_lock(ledger_root):
            before = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)
            if before.get("total_live_request_count") != 0:
                raise VC0CloseoutError("账本已有 live 请求计量，拒绝重复追加修复")
            events = codex_upgrade_timing_ledger._load_events(ledger_root)
            if not any(
                event.get("event_type") == "stage_abandoned"
                and str(event.get("event_id", "")).startswith(
                    "vc0-closeout-failure-"
                )
                for event, _event_raw in events
            ):
                raise VC0CloseoutError("账本没有可修复的 VC-0 closeout 失败事件")
            audit = build_live_request_audit(
                campaign_dir,
                formal_campaign_id=campaign_id,
            )
            live_request_count = int(audit["live_request_count"])
            if live_request_count <= 0:
                raise VC0CloseoutError("审计未取得任何可追加的 live 请求")
            _write_once(audit_root / "evidence.json", audit)
            digest = _sha256_bytes(
                f"{campaign_id}\0historical-live-accounting".encode("utf-8")
            )[:20]
            binding = _publish_failure_live_request_audit(
                ledger_root,
                formal_campaign_id=campaign_id,
                audit=audit,
                filename=f"historical-live-request-audit-{digest}.json",
            )
            event_id = f"live-request-accounting-repair-{digest}"
            appended = codex_upgrade_timing_ledger.append_event(
                ledger_root,
                event_id=event_id,
                phase="VC-1",
                event_type="receipt_passed",
                receipts=[binding],
                live_request_count=live_request_count,
                next_action=(
                    "保留原 Campaign 与 deadline，完成工具修复离线闭环后从最近合法 "
                    "VC-1 checkpoint 恢复 failed/pending 项"
                ),
            )
        receipt = {
            "schema_version": "codex-upgrade-live-request-accounting-repair/v1",
            "status": "complete",
            "completed_at_utc": _utc_now(),
            "campaign_id": campaign_id,
            "history_rewritten": False,
            "live_request_count": live_request_count,
            "ledger_event_id": event_id,
            "ledger_head_sequence": appended["head_sequence"],
            "ledger_head_sha256": appended["head_sha256"],
            "ledger_receipt": binding,
        }
        _write_once(audit_root / "receipt.json", receipt)
        return receipt
    except BaseException as error:
        try:
            _write_once(
                audit_root / "failure.json",
                {
                    "schema_version": "codex-upgrade-live-request-accounting-repair-failure/v1",
                    "status": "failed",
                    "failed_at_utc": _utc_now(),
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "history_rewritten": False,
                },
            )
        except BaseException:
            pass
        raise


def repair_failed_closeout_closure(
    *,
    formal_campaign_dir: Path,
    timing_ledger_dir: Path,
    source_audit_dir: Path,
    audit_dir: Path,
) -> dict[str, Any]:
    """只追加补齐一次因计数器缺陷未写入的失败阶段闭合。"""

    os.umask(0o077)
    audit_root = _new_private_directory(audit_dir, "失败闭合修复审计目录")
    request = {
        "schema_version": "codex-upgrade-failed-closeout-closure-repair-request/v1",
        "formal_campaign_dir": str(formal_campaign_dir),
        "timing_ledger_dir": str(timing_ledger_dir),
        "source_audit_dir": str(source_audit_dir),
        "requested_at_utc": _utc_now(),
        "history_rewrite_allowed": False,
        "live_requests_allowed": False,
    }
    _write_once(audit_root / "request.json", request)
    try:
        campaign_dir = _trusted_directory(formal_campaign_dir, "Formal Campaign")
        campaign, _campaign_raw = _load_json(
            campaign_dir / "campaign.json",
            "Formal Campaign",
        )
        campaign_id = str(campaign.get("campaign_id", ""))
        if (
            campaign.get("campaign_mode") != "formal"
            or not SAFE_ID_RE.fullmatch(campaign_id)
        ):
            raise VC0CloseoutError("Formal Campaign 身份非法")

        source_root = _trusted_directory(source_audit_dir, "原失败审计目录")
        source_request_path = _trusted_file(
            source_root / "request.json",
            "原 VC-0 closeout 请求",
        )
        source_failure_path = _trusted_file(
            source_root / "failure.json",
            "原 VC-0 closeout 失败诊断",
        )
        source_request, _source_request_raw = _load_json(
            source_request_path,
            "原 VC-0 closeout 请求",
        )
        source_failure, _source_failure_raw = _load_json(
            source_failure_path,
            "原 VC-0 closeout 失败诊断",
        )
        closure = source_failure.get("timing_failure_closure")
        timing_summary = source_failure.get("timing_summary")
        failed_step = source_failure.get("failed_step")
        try:
            source_campaign_dir = Path(
                str(source_request.get("formal_campaign_dir", ""))
            ).resolve(strict=True)
        except OSError as error:
            raise VC0CloseoutError("原失败请求的 Formal Campaign 路径不可重放") from error
        if (
            source_failure.get("schema_version") != CLOSEOUT_DIAGNOSTIC_SCHEMA
            or source_failure.get("status") != "failed"
            or not isinstance(closure, Mapping)
            or closure.get("status") != "closure-failed"
            or closure.get("error_type") != "VC0CloseoutError"
            or closure.get("message") != FAILED_CLOSEOUT_CLOSURE_REPAIR_MESSAGE
            or not isinstance(timing_summary, Mapping)
            or failed_step != "dispatch-vc1"
            or source_request.get("formal_campaign_id") != campaign_id
            or source_campaign_dir != campaign_dir.resolve(strict=True)
            or source_failure.get("formal_campaign_path_exists") is not True
            or source_failure.get("deadline_extended") is not False
            or source_failure.get("cleanup_performed") is not False
        ):
            raise VC0CloseoutError("原失败诊断不属于可追加修复的 closeout 闭合缺陷")

        ledger_root = _private_directory(timing_ledger_dir, "UpgradeTimingLedger")
        with _ledger_lock(ledger_root):
            before = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)
            for key in (
                "upgrade_id",
                "head_sequence",
                "head_sha256",
                "active_phase",
                "total_live_request_count",
                "total_deadline_at_utc",
            ):
                if before.get(key) != timing_summary.get(key):
                    raise VC0CloseoutError(
                        f"失败闭合修复发现原时间账本字段漂移：{key}"
                    )
            # R8：预算到期后账本显示 deadline_paused，暂停前仍是 active／stop_required；修复只追加
            # stage_abandoned 与 stop_the_line，暂停期间允许写入，因此按暂停前状态判断。
            status = (
                before.get("status_before_pause")
                if before.get("status") == "deadline_paused"
                else before.get("status")
            )
            if (
                status not in {"active", "stop_required"}
                or before.get("active_phase") not in codex_upgrade_timing_ledger.PHASE_ORDER
            ):
                raise VC0CloseoutError("原时间账本已不是待闭合的 active／stop_required 阶段")
            closure_result = _close_failed_timing_stage(
                ledger_root,
                formal_campaign_dir=campaign_dir,
                formal_campaign_id=campaign_id,
                failed_step=failed_step,
            )
            if closure_result.get("status") not in {
                "stage-abandoned-recorded",
                "stop-the-line-recorded",
            }:
                raise VC0CloseoutError("失败闭合修复未形成唯一阶段终态")
            after = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)

        receipt = {
            "schema_version": "codex-upgrade-failed-closeout-closure-repair/v1",
            "status": "complete",
            "completed_at_utc": _utc_now(),
            "campaign_id": campaign_id,
            "source_request": {
                "path": str(source_request_path),
                "sha256": _sha256_file(source_request_path),
            },
            "source_failure": {
                "path": str(source_failure_path),
                "sha256": _sha256_file(source_failure_path),
            },
            "closure": closure_result,
            "ledger_head_sequence": after["head_sequence"],
            "ledger_head_sha256": after["head_sha256"],
            "total_live_request_count": after["total_live_request_count"],
            "history_rewritten": False,
            "live_request_count": 0,
        }
        _write_once(audit_root / "receipt.json", receipt)
        return receipt
    except BaseException as error:
        try:
            _write_once(
                audit_root / "failure.json",
                {
                    "schema_version": "codex-upgrade-failed-closeout-closure-repair-failure/v1",
                    "status": "failed",
                    "failed_at_utc": _utc_now(),
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "history_rewritten": False,
                    "live_request_count": 0,
                },
            )
        except BaseException:
            pass
        raise


# ---------------------------------------------------------------------------
# E2-07：只读现场判定
# ---------------------------------------------------------------------------
#
# 续作的第一步永远是只读判定，判定结果决定走哪条路；对不上的一律拒绝并出诊断，不动现场。
#
# | 判定         | 依据                                                                     |
# |--------------|--------------------------------------------------------------------------|
# | fresh        | 账本里没有本 Formal ID 的任何收口痕迹，Formal 路径与监督器状态目录都不存在     |
# | pre-formal   | 有收口 attempt，Formal 不可重放或注册批次没有 COMMIT（视同未创建），没有派发痕迹 |
# | formal-built | Formal 可重放且注册批次已 COMMIT，没有派发痕迹                               |
# | dispatched   | 监督器状态目录里有本 Formal 的父 run，或 Campaign 里有官方 attempt／租约        |
# | inconsistent | 事件被别的内容占用、Formal 不是本收口建的、阶段事件与 Formal 状态对不上等       |


def _closeout_root_cause(step: str) -> str:
    """收口步骤失败的稳定根因 ID：只由步骤名决定（与 ``_close_failed_timing_stage`` 同一构造）。"""

    return codex_upgrade_root_cause.structured_root_cause(
        component=CLOSEOUT_ROOT_CAUSE_COMPONENT,
        stable_error_code=CLOSEOUT_ROOT_CAUSE_CODE,
        failed_step=step,
    )


def _attempt_id(formal_campaign_id: str, number: int) -> str:
    return f"{formal_campaign_id}-closeout-a{number}"


def _receipt_event_id(formal_campaign_id: str, set_index: int) -> str:
    """第 1 组收据副本沿用原事件 ID；修复期间重签过输入、另起的新组带 attempt 序号。"""

    base = f"{formal_campaign_id}-p0-receipts-passed"
    return base if set_index == 1 else f"{base}-a{set_index}"


def _assert_event_id_room(formal_campaign_id: str) -> None:
    """派生出的最长事件 ID 也必须是账本接受的安全标识（不超过 128 字符）。"""

    if not SAFE_ID_RE.fullmatch(f"{_attempt_id(formal_campaign_id, 999)}-completed"):
        raise VC0CloseoutError("formal_campaign_id 太长：派生的收口 attempt 事件 ID 会超过 128 字符")


def _ledger_dir_from_preflight(preflight_campaign_dir: Path) -> Path:
    """只读取 preflight 清单里冻结的计时账本目录（完整校验由 ``validate_inputs`` 做）。"""

    manifest, _raw = _load_json(
        _trusted_file(Path(preflight_campaign_dir) / "campaign.json", "preflight Campaign 清单"),
        "preflight Campaign 清单",
    )
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    ledger_dir = timing.get("ledger_dir") if isinstance(timing, Mapping) else None
    if not isinstance(ledger_dir, str) or not ledger_dir:
        raise VC0CloseoutError("preflight Campaign 没有冻结计时账本目录")
    return _private_directory(Path(ledger_dir), "UpgradeTimingLedger")


def _ledger_summary_view(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: summary.get(key)
        for key in (
            "status",
            "status_before_pause",
            "active_phase",
            "head_sequence",
            "head_sha256",
            "same_root_cause_failures",
            "total_live_request_count",
            "upgrade_id",
        )
    }


def _closeout_ledger_facts(timing_root: Path, formal_campaign_id: str) -> dict[str, Any]:
    """只读：本 Formal ID 的收口 attempt、收据通过事件、两条阶段事件，以及被别的内容占用的事件 ID。"""

    try:
        events = codex_upgrade_timing_ledger._load_events(timing_root)
        summary = codex_upgrade_timing_ledger.inspect_ledger(timing_root)
    except (OSError, ValueError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise VC0CloseoutError(f"UpgradeTimingLedger 重放失败：{error}") from error
    attempt_pattern = re.compile(re.escape(formal_campaign_id) + r"-closeout-a([1-9][0-9]*)")
    receipt_pattern = re.compile(
        re.escape(formal_campaign_id) + r"-p0-receipts-passed(?:-a([1-9][0-9]*))?"
    )
    stage_ids = {
        f"{formal_campaign_id}-vc0-completed": ("vc0_completed", "stage_completed", "VC-0"),
        f"{formal_campaign_id}-vc1-started": ("vc1_started", "stage_started", "VC-1"),
    }
    attempts: dict[int, dict[str, Any]] = {}
    receipt_events: list[dict[str, Any]] = []
    stages: dict[str, int] = {}
    problems: list[str] = []
    event_ids: set[str] = set()
    for event, _raw in events:
        event_id = str(event.get("event_id"))
        event_ids.add(event_id)
        event_type = event.get("event_type")
        attempt = event.get("attempt_id")
        match = attempt_pattern.fullmatch(attempt) if isinstance(attempt, str) else None
        if match is not None:
            number = int(match.group(1))
            row = attempts.setdefault(
                number,
                {
                    "attempt_id": attempt,
                    "number": number,
                    "status": None,
                    "retry_root_cause_id": None,
                    "failure_root_cause_id": None,
                    "started_sequence": None,
                },
            )
            if event.get("phase") != "VC-0":
                problems.append(f"收口 attempt {attempt} 的事件不在 VC-0：{event_id}")
            elif event_type == "attempt_started" and row["status"] is None:
                row.update(
                    status="active",
                    retry_root_cause_id=event.get("root_cause_id"),
                    started_sequence=int(event["sequence"]),
                )
            elif event_type == "attempt_failed" and row["status"] == "active":
                row.update(status="failed", failure_root_cause_id=event.get("root_cause_id"))
            elif event_type == "attempt_completed" and row["status"] == "active":
                row["status"] = "completed"
            else:
                problems.append(f"收口 attempt {attempt} 的事件序列非法：{event_id}（{event_type}）")
            continue
        receipt_match = receipt_pattern.fullmatch(event_id)
        if receipt_match is not None:
            if event_type != "receipt_passed" or event.get("phase") != "VC-0":
                problems.append(f"事件 {event_id} 已被其它内容占用")
            else:
                receipt_events.append(
                    {
                        "event_id": event_id,
                        "set_index": int(receipt_match.group(1) or 1),
                        "sequence": int(event["sequence"]),
                        "receipts": [dict(item) for item in event.get("receipts") or []],
                    }
                )
            continue
        if event_id in stage_ids:
            name, expected_type, expected_phase = stage_ids[event_id]
            if event_type != expected_type or event.get("phase") != expected_phase:
                problems.append(f"事件 {event_id} 已被其它内容占用")
            else:
                stages[name] = int(event["sequence"])
    numbers = sorted(attempts)
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"收口 attempt 序号不连续：{numbers}")
    active = [attempts[number] for number in numbers if attempts[number]["status"] == "active"]
    if len(active) > 1:
        problems.append("同时有多个进行中的收口 attempt")
    return {
        "summary": _ledger_summary_view(summary),
        "attempts": [attempts[number] for number in numbers],
        "active_attempt": active[-1] if active else None,
        "next_attempt": len(numbers) + 1,
        "receipt_events": sorted(receipt_events, key=lambda item: item["sequence"]),
        "vc0_completed": stages.get("vc0_completed"),
        "vc1_started": stages.get("vc1_started"),
        "problems": problems,
        "_event_ids": event_ids,
        "_summary": summary,
    }


def _receipt_namespace(timing_root: Path, formal_campaign_id: str) -> Path:
    return timing_root / "receipts" / "vc0-closeout" / formal_campaign_id


def _receipt_sets(
    timing_root: Path,
    formal_campaign_id: str,
    receipt_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只读：本 Formal ID 的各组收据副本（第 1 组在命名空间根，第 n 组在 ``a<n>/``）与绑定它的事件。"""

    namespace_root = timing_root / "receipts" / "vc0-closeout"
    if namespace_root.is_symlink():
        raise VC0CloseoutError("VC-0 收口收据命名空间不得是符号链接")
    namespace = _receipt_namespace(timing_root, formal_campaign_id)
    if not (namespace.exists() or namespace.is_symlink()):
        return []
    root = _private_directory(namespace, "VC-0 收口收据目录")
    candidates: list[tuple[int, Path]] = [(1, root)]
    for child in sorted(root.iterdir()):
        match = re.fullmatch(r"a([1-9][0-9]*)", child.name)
        if match is not None:
            candidates.append((int(match.group(1)), _private_directory(child, f"收据副本组 {child.name}")))
    events = {item["set_index"]: item for item in receipt_events}
    sets: list[dict[str, Any]] = []
    for index, directory in sorted(candidates):
        files: dict[str, str] = {}
        for role in INPUT_ROLES:
            path = directory / f"{role}.json"
            if path.exists() or path.is_symlink():
                files[role] = _sha256_file(
                    _trusted_file(path, f"{role} 收据副本", maximum=MAX_RECEIPT_BYTES)
                )
        checkpoint = directory / "timing-vc0-active.json"
        sets.append(
            {
                "index": index,
                "directory": directory,
                "files": files,
                "event": events.get(index),
                "checkpoint": checkpoint.relative_to(timing_root.resolve(strict=True)).as_posix()
                if checkpoint.is_file() and not checkpoint.is_symlink()
                else None,
            }
        )
    for index in events:
        if index not in {item["index"] for item in sets}:
            raise VC0CloseoutError(f"收据通过事件 {events[index]['event_id']} 对应的收据副本组不在了")
    return sets


def _set_bindings(timing_root: Path, receipt_set: Mapping[str, Any]) -> list[dict[str, str]]:
    ledger_root = timing_root.resolve(strict=True)
    directory = Path(receipt_set["directory"])
    return [
        {
            "role": role,
            "path": (directory / f"{role}.json").relative_to(ledger_root).as_posix(),
            "sha256": receipt_set["files"][role],
        }
        for role in sorted(receipt_set["files"])
    ]


def _set_event_problems(timing_root: Path, receipt_set: Mapping[str, Any]) -> list[str]:
    """绑定事件必须逐字对上这一组的四份副本（角色、相对路径、摘要）。"""

    event = receipt_set.get("event")
    if event is None:
        return []
    if set(receipt_set["files"]) != set(INPUT_ROLES):
        return [f"收据通过事件 {event['event_id']} 绑定的副本组不完整"]
    expected = _set_bindings(timing_root, receipt_set)
    recorded = sorted(
        ({key: str(item.get(key)) for key in ("role", "path", "sha256")} for item in event["receipts"]),
        key=lambda item: item["role"],
    )
    if recorded != expected:
        return [f"收据通过事件 {event['event_id']} 与账本里的收据副本不一致"]
    return []


def _registration_state(campaign_dir: Path, formal_campaign_id: str) -> str:
    """Formal 的总账注册批次：none／uncommitted（写了一半，视同未创建）／committed／invalid。"""

    outbox = campaign_dir / codex_upgrade_project_ledger.CAMPAIGN_LEDGER_DIR_NAME / "outbox"
    try:
        batches = codex_upgrade_project_ledger._batch_dirs(outbox)
        if not batches:
            return "none"
        batch = codex_upgrade_project_ledger._read_batch(batches[0][1])
    except (OSError, ValueError, codex_upgrade_project_ledger.ProjectLedgerError):
        return "invalid"
    if not batch["committed"]:
        return "uncommitted"
    commit = batch["commit"]
    if (
        commit.get("event_type") != "campaign_registered"
        or commit.get("operation_id") != f"register:{formal_campaign_id}"
    ):
        return "invalid"
    return "committed"


def _formal_facts(
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    timing_root: Path,
) -> dict[str, Any]:
    """只读：Formal 目录是否存在、能否重放、注册批次状态，以及有没有派发后才会出现的痕迹。"""

    facts: dict[str, Any] = {
        "path": str(formal_campaign_dir),
        "exists": False,
        "replayable": False,
        "registration": None,
        "activity": [],
        "problems": [],
        "replay_error": None,
        "_manifest": None,
    }
    if not formal_campaign_dir.is_absolute() or ".." in formal_campaign_dir.parts:
        raise VC0CloseoutError("Formal Campaign 路径必须是规范绝对路径")
    if not (formal_campaign_dir.exists() or formal_campaign_dir.is_symlink()):
        return facts
    facts["exists"] = True
    if formal_campaign_dir.is_symlink() or not formal_campaign_dir.is_dir():
        facts["problems"].append("Formal Campaign 路径不是普通目录")
        return facts
    facts["registration"] = _registration_state(formal_campaign_dir, formal_campaign_id)
    try:
        manifest = codex_upgrade.load_campaign_manifest(formal_campaign_dir)
    except (OSError, ValueError, codex_upgrade.ConfigurationError) as error:
        facts["replay_error"] = str(error)[:500]
    else:
        facts["replayable"] = True
        facts["_manifest"] = manifest
        controls = manifest.get("control_receipts")
        timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
        bound_ledger = timing.get("ledger_dir") if isinstance(timing, Mapping) else None
        if manifest.get("campaign_id") != formal_campaign_id or manifest.get("campaign_mode") != "formal":
            facts["problems"].append("Formal Campaign 清单的身份或模式与本次收口不一致")
        elif not isinstance(bound_ledger, str) or Path(bound_ledger).resolve(strict=False) != timing_root.resolve(strict=True):
            facts["problems"].append("Formal Campaign 绑定的计时账本不是本账本")
    attempts_root = formal_campaign_dir / "official" / "attempts"
    if attempts_root.is_dir() and not attempts_root.is_symlink():
        facts["activity"].extend(f"official/attempts/{child.name}" for child in sorted(attempts_root.iterdir()))
    lease = formal_campaign_dir / codex_upgrade.CAMPAIGN_LEASE_FILENAME
    if lease.exists() or lease.is_symlink():
        facts["activity"].append(codex_upgrade.CAMPAIGN_LEASE_FILENAME)
    return facts


def _first_run_manifest_path(formal_campaign_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Formal 绑定的首个 VC-1 campaign-run 清单路径（路径在 Formal 内、摘要与绑定一致）。"""

    control = manifest.get("vc_control")
    reference = control.get("first_campaign_run_manifest") if isinstance(control, Mapping) else None
    if not isinstance(reference, Mapping):
        raise VC0CloseoutError("Formal Campaign 缺少首个 campaign-run 清单")
    path = _inside(formal_campaign_dir, str(reference.get("path", "")), "首个 VC-1 campaign-run 清单")
    if _sha256_file(path) != reference.get("sha256"):
        raise VC0CloseoutError("首个 VC-1 campaign-run 清单摘要漂移")
    return path


def _first_run_manifest(formal_campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Formal 绑定的首个 VC-1 campaign-run 清单（路径＋摘要核对后解析）。"""

    path = _first_run_manifest_path(formal_campaign_dir, manifest)
    try:
        parsed = codex_upgrade_supervisor._campaign_run_manifest(path)
    except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise VC0CloseoutError(f"首个 VC-1 campaign-run 清单无法解析：{error}") from error
    return path, parsed


def _flock_busy(path: Path) -> bool:
    """锁文件当前是否被别的进程持有（只探测，不创建文件）。"""

    if not path.exists() or path.is_symlink():
        return False
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return False


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _supervisor_facts(
    state_dir: Path,
    formal_campaign_id: str,
    first_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """只读：监督器状态目录里本 Formal 的父 run（是否首批、状态、终态原因）与动作队列锁。"""

    facts: dict[str, Any] = {"path": str(state_dir), "exists": False, "runs": [], "lock_busy": False, "problems": []}
    if not state_dir.is_absolute() or ".." in state_dir.parts:
        raise VC0CloseoutError("VC-1 supervisor state-dir 必须是规范绝对路径")
    if not (state_dir.exists() or state_dir.is_symlink()):
        return facts
    facts["exists"] = True
    if state_dir.is_symlink() or not state_dir.is_dir():
        facts["problems"].append("VC-1 supervisor state-dir 不是普通目录")
        return facts
    for state_path in sorted(state_dir.glob("run-*/state.json")):
        run_dir = state_path.parent
        if run_dir.is_symlink() or state_path.is_symlink():
            facts["problems"].append(f"监督器 run 目录不可信：{run_dir.name}")
            continue
        try:
            state = codex_upgrade_supervisor._read_state(run_dir)
        except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
            facts["problems"].append(f"监督器 run {run_dir.name} 的状态读不了：{error}")
            continue
        if state.get("campaign_id") != formal_campaign_id:
            facts["problems"].append(f"监督器状态目录里有别的 Campaign 的 run：{run_dir.name}")
            continue
        stop_reason = None
        if (run_dir / "stop-receipt.json").exists():
            try:
                stop_reason = codex_upgrade_supervisor.read_stop_receipt(run_dir).get("reason")
            except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
                facts["problems"].append(f"监督器 run {run_dir.name} 的终态收据读不了：{error}")
                continue
        first_batch = False
        record_path = run_dir / "campaign-run-manifest.json"
        if first_manifest is not None and record_path.is_file() and not record_path.is_symlink():
            try:
                record, _raw = _load_json(_trusted_file(record_path, "campaign-run 清单"), "campaign-run 清单")
            except VC0CloseoutError:
                record = {}
            first_batch = record.get("manifest") == first_manifest
        facts["runs"].append(
            {
                "run_dir": str(run_dir),
                "state": state.get("state"),
                "owner_pid": state.get("owner_pid"),
                "stop_reason": stop_reason,
                "first_batch": first_batch,
            }
        )
    facts["lock_busy"] = _flock_busy(state_dir / codex_upgrade_supervisor.CAMPAIGN_RUN_LOCK_FILENAME)
    return facts


def _formal_binding_problems(
    manifest: Mapping[str, Any],
    sets: list[dict[str, Any]],
    timing_root: Path,
) -> list[str]:
    """Formal 冻结的控制收据必须等于账本里最后一组带事件的收据副本（含账本 checkpoint）。"""

    bound = [item for item in sets if item["event"] is not None]
    if not bound:
        return ["Formal 已建，但账本里没有带收据通过事件的收据副本"]
    latest = max(bound, key=lambda item: item["event"]["sequence"])
    controls = manifest.get("control_receipts")
    if not isinstance(controls, Mapping):
        return ["Formal Campaign 缺少控制收据绑定"]

    def receipt_sha(field: str) -> Any:
        value = controls.get(field)
        receipt = value.get("receipt") if isinstance(value, Mapping) else None
        return receipt.get("sha256") if isinstance(receipt, Mapping) else None

    release = controls.get("release_certification")
    timing = controls.get("upgrade_timing")
    timing_receipt = timing.get("receipt") if isinstance(timing, Mapping) else None
    problems: list[str] = []
    if receipt_sha("p0_gate") != latest["files"].get("p0_gate"):
        problems.append("Formal 冻结的 P0 收据与账本里的副本不一致")
    if (release.get("sha256") if isinstance(release, Mapping) else None) != latest["files"].get("release_certification"):
        problems.append("Formal 冻结的发布认证与账本里的副本不一致")
    if receipt_sha("arm64_environment") != latest["files"].get("arm64_environment"):
        problems.append("Formal 冻结的 ARM64 环境收据与账本里的副本不一致")
    if not isinstance(timing_receipt, Mapping) or timing_receipt.get("path") != latest["checkpoint"]:
        problems.append("Formal 冻结的账本 checkpoint 不是最后一组收据副本的 checkpoint")
    elif _sha256_file(timing_root / str(latest["checkpoint"])) != timing_receipt.get("sha256"):
        problems.append("Formal 冻结的账本 checkpoint 摘要与账本里的文件不一致")
    return problems


def inspect_site(
    *,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
) -> dict[str, Any]:
    """只读现场判定（E2-07 第 1 点）。返回可序列化的判定；以 ``_`` 开头的键是内部对象，不落盘。"""

    ledger = _closeout_ledger_facts(timing_root, formal_campaign_id)
    sets = _receipt_sets(timing_root, formal_campaign_id, ledger["receipt_events"])
    formal = _formal_facts(formal_campaign_dir, formal_campaign_id, timing_root)
    first_manifest: dict[str, Any] | None = None
    problems = [*ledger["problems"], *formal["problems"]]
    for receipt_set in sets:
        problems.extend(_set_event_problems(timing_root, receipt_set))
    built = formal["replayable"] and formal["registration"] == "committed"
    has_runs = supervisor_state_dir.is_dir() and not supervisor_state_dir.is_symlink() and any(
        supervisor_state_dir.glob("run-*/state.json")
    )
    if formal["replayable"] and not formal["problems"] and has_runs:
        # 只有监督器状态目录里已经有 run 时才需要解析首批清单（用来认出哪个 run 是首批）。
        try:
            _first_path, first_manifest = _first_run_manifest(formal_campaign_dir, formal["_manifest"])
        except VC0CloseoutError as error:
            problems.append(str(error))
    supervisor = _supervisor_facts(supervisor_state_dir, formal_campaign_id, first_manifest)
    problems.extend(supervisor["problems"])
    has_attempts = bool(ledger["attempts"])
    dispatched = bool(supervisor["runs"] or formal["activity"])
    if formal["exists"] and not has_attempts:
        problems.append("Formal Campaign 路径已存在，但账本里没有本 Formal ID 的收口 attempt（不是本收口建的）")
    if (sets or ledger["receipt_events"] or ledger["vc0_completed"] or ledger["vc1_started"]) and not has_attempts:
        problems.append("账本里有本 Formal ID 的收口副本或事件，但没有收口 attempt（旧版收口现场，需人工处置）")
    if formal["registration"] == "committed" and not formal["replayable"]:
        problems.append("Formal 注册批次已提交，但 Campaign 清单不可重放")
    if (ledger["vc0_completed"] or ledger["vc1_started"]) and not built:
        problems.append("「VC-0 完成」或「VC-1 开始」已写，但 Formal 未建成")
    if ledger["vc1_started"] and not ledger["vc0_completed"]:
        problems.append("「VC-1 开始」已写，但没有「VC-0 完成」")
    if supervisor["exists"] and not built:
        problems.append("监督器状态目录已存在，但 Formal 未建成")
    if dispatched and not ledger["vc1_started"]:
        problems.append("已有首批运行痕迹，但账本里没有「VC-1 开始」")
    if dispatched and not built:
        problems.append("已有首批运行痕迹，但 Formal 未建成")
    if built and formal["_manifest"] is not None:
        problems.extend(_formal_binding_problems(formal["_manifest"], sets, timing_root))
    if problems:
        kind = "inconsistent"
    elif dispatched:
        kind = "dispatched"
    elif built:
        kind = "formal-built"
    elif has_attempts:
        kind = "pre-formal"
    else:
        kind = "fresh"
    return {
        "schema_version": SITE_SCHEMA,
        "observed_at_utc": _utc_now(),
        "formal_campaign_id": formal_campaign_id,
        "site": kind,
        "problems": problems,
        "ledger": {key: value for key, value in ledger.items() if not key.startswith("_") and key != "problems"},
        "receipt_sets": [
            {
                "index": item["index"],
                "directory": str(item["directory"]),
                "roles": sorted(item["files"]),
                "event_id": (item["event"] or {}).get("event_id"),
                "checkpoint": item["checkpoint"],
            }
            for item in sets
        ],
        "formal": {key: value for key, value in formal.items() if not key.startswith("_") and key != "problems"},
        "supervisor": {key: value for key, value in supervisor.items() if key != "problems"},
        "_ledger": ledger,
        "_sets": sets,
        "_formal_manifest": formal["_manifest"],
        "_first_manifest": first_manifest,
    }


def _public_site(site: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in site.items() if not key.startswith("_")}


# ---------------------------------------------------------------------------
# E2-07：收口 attempt 与续作
# ---------------------------------------------------------------------------


@dataclass
class _Progress:
    """一次执行的进度：当前步骤、进行中的收口 attempt、失败收口的结果。"""

    step: str = "initialize-audit"
    attempt_id: str | None = None
    closure: dict[str, Any] | None = None
    created_formal: bool = False
    archived: list[str] | None = None
    closed_interrupted: list[str] | None = None
    resumed_at_limit: dict[str, Any] | None = None


class _AtLimit(Exception):
    """账本同根因到上限（stop_required）：在锁外走上限后的恢复，再重新判定现场。"""


@contextmanager
def _closeout_locks(timing_root: Path, formal_campaign_dir: Path) -> Iterator[None]:
    """锁序固定：项目锁（有总账时，进程内可重入，建 Formal 的准入复用它）→ 账本锁。

    延期、campaign-resume、预算暂停都是先项目锁后账本锁，收口同序才不会互相等死。派发首批之前必须把两把锁都放掉：
    监督器失败收口要非阻塞地取账本锁，预算暂停要取项目锁。
    """

    project_root = codex_upgrade_project_ledger.find_project_ledger(formal_campaign_dir.parent)
    if project_root is None:
        with _ledger_lock(timing_root):
            yield
        return
    with codex_upgrade_project_ledger.project_lock(project_root), _ledger_lock(timing_root):
        yield


def _append_once(timing_root: Path, event_ids: set[str], *, event_id: str, **fields: Any) -> bool:
    """事件 ID 已在账本里就跳过（续作时已写的事件不重写），否则追加；返回是否新写。"""

    if event_id in event_ids:
        return False
    codex_upgrade_timing_ledger.append_event(timing_root, event_id=event_id, **fields)
    event_ids.add(event_id)
    return True


def _close_interrupted_attempt(timing_root: Path, ledger: Mapping[str, Any], progress: _Progress) -> None:
    """持账本锁时仍是进行中的收口 attempt，原进程已经不在了：以「收口进程中断」为根因记失败关掉。"""

    active = ledger.get("active_attempt")
    if active is None:
        return
    attempt_id = str(active["attempt_id"])
    codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=f"{attempt_id}-failed",
        phase="VC-0",
        event_type="attempt_failed",
        attempt_id=attempt_id,
        root_cause_id=_closeout_root_cause(INTERRUPTED_STEP),
        live_request_count=0,
        next_action="收口进程中断：现场与已写内容核对一致后，同一 Formal ID 开新的收口 attempt 续作",
    )
    progress.closed_interrupted = [*(progress.closed_interrupted or []), attempt_id]


def _start_attempt(timing_root: Path, formal_campaign_id: str, ledger: Mapping[str, Any], progress: _Progress) -> str:
    number = int(ledger["next_attempt"])
    attempt_id = _attempt_id(formal_campaign_id, number)
    failed = [row for row in ledger["attempts"] if row["status"] == "failed"]
    retry_cause = failed[-1]["failure_root_cause_id"] if failed else None
    codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=f"{attempt_id}-started",
        phase="VC-0",
        event_type="attempt_started",
        attempt_id=attempt_id,
        root_cause_id=retry_cause,
        live_request_count=0,
        next_action="收据副本 → 建 Formal Campaign → 控制产物副本；完成后关闭本 attempt 再写「VC-0 完成」",
    )
    progress.attempt_id = attempt_id
    return attempt_id


def _fail_attempt(timing_root: Path, progress: _Progress) -> None:
    """attempt 内的步骤失败：记 attempt 失败与根因（步骤名），VC-0 保持打开、继续计时。持锁时调用。"""

    attempt_id = progress.attempt_id
    if attempt_id is None:
        return
    root_cause_id = _closeout_root_cause(progress.step)
    try:
        codex_upgrade_timing_ledger.append_event(
            timing_root,
            event_id=f"{attempt_id}-failed",
            phase="VC-0",
            event_type="attempt_failed",
            attempt_id=attempt_id,
            root_cause_id=root_cause_id,
            live_request_count=0,
            next_action="修复后用同一 Formal ID、同一账本重跑收口续作；同一根因连续两次后先走上限后的恢复",
        )
    except BaseException as error:
        progress.closure = {
            "status": "closure-failed",
            "attempt_id": attempt_id,
            "error_type": type(error).__name__,
            "message": str(error)[:1000],
        }
        return
    progress.closure = {
        "status": "attempt-failed-recorded",
        "attempt_id": attempt_id,
        "event_id": f"{attempt_id}-failed",
        "root_cause_id": root_cause_id,
        "failed_step": progress.step,
    }
    progress.attempt_id = None


def _complete_attempt(timing_root: Path, progress: _Progress) -> None:
    attempt_id = progress.attempt_id
    assert attempt_id is not None
    codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=f"{attempt_id}-completed",
        phase="VC-0",
        event_type="attempt_completed",
        attempt_id=attempt_id,
        live_request_count=0,
        next_action="写「VC-0 完成」「VC-1 开始」并派发首批",
    )
    progress.attempt_id = None


def _archive_half_built_formal(
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    progress: _Progress,
) -> None:
    """半成品 Formal（不可重放或注册批次没 COMMIT，总账视同未创建）改名归档到账本的收口命名空间里，不删。

    不能留在 ``evidence/campaigns`` 下：请求来源扫描与处置模块会把带 campaign.json 的子目录当成 Campaign。
    """

    if not (formal_campaign_dir.exists() or formal_campaign_dir.is_symlink()):
        return
    if formal_campaign_dir.is_symlink() or not formal_campaign_dir.is_dir():
        raise VC0CloseoutError("Formal Campaign 路径不是普通目录，不能归档")
    archive_root = _receipt_namespace(timing_root, formal_campaign_id) / "archive"
    _ensure_private_tree(archive_root, timing_root)
    target = archive_root / f"formal-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%S%fz')}"
    if target.exists() or target.is_symlink():
        raise VC0CloseoutError(f"归档目标已存在：{target}")
    try:
        os.rename(formal_campaign_dir, target)
    except OSError as error:
        raise VC0CloseoutError(f"半成品 Formal 目录归档失败（须与账本在同一文件系统）：{error}") from error
    progress.archived = [*(progress.archived or []), str(target)]


def _ensure_private_tree(path: Path, root: Path) -> Path:
    """逐级创建 ``root`` 之下的私有目录（已存在的必须是私有目录、不是符号链接）。"""

    root = _private_directory(root, "UpgradeTimingLedger")
    relative = path.resolve(strict=False).relative_to(root) if path.is_absolute() else path
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            _private_directory(cursor, f"收口目录 {cursor.name}")
        else:
            cursor.mkdir(mode=0o700)
    return cursor


def _bind_receipt_set(
    validated: ValidatedInputs,
    timing_root: Path,
    formal_campaign_id: str,
    sets: list[dict[str, Any]],
    attempt_number: int,
    progress: _Progress,
) -> dict[str, Any]:
    """选定（或新起）一组收据副本并补齐缺的文件：内容相同的副本沿用；重签过的输入另起 ``a<n>/`` 新组。

    已有绑定事件的组只在四份副本与本次输入逐字节相同时沿用；没有事件的组（上次写了一半）只要已写的
    副本都相同就补齐。旧组与旧事件原样留作历史。
    """

    progress.step = "copy-p0-receipts"
    sources = {item.role: item for item in validated.receipts}
    if tuple(sorted(sources)) != INPUT_ROLES:
        raise VC0CloseoutError("VC-0 四份输入角色不闭合")

    def consistent(receipt_set: Mapping[str, Any]) -> bool:
        return all(receipt_set["files"].get(role) in (None, sources[role].sha256) for role in INPUT_ROLES)

    latest = sets[-1] if sets else None
    if latest is not None and latest["event"] is not None:
        complete = set(latest["files"]) == set(INPUT_ROLES) and consistent(latest)
        target = latest if complete else None
    elif latest is not None and consistent(latest):
        target = latest
    else:
        target = None
    namespace = _ensure_private_tree(_receipt_namespace(timing_root, formal_campaign_id), timing_root)
    if target is None:
        index = 1 if latest is None else attempt_number
        directory = namespace if index == 1 else namespace / f"a{index}"
        if index != 1:
            if directory.exists() or directory.is_symlink():
                raise VC0CloseoutError(f"收据副本组 a{index} 已存在却不在判定结果里")
            directory.mkdir(mode=0o700)
        target = {"index": index, "directory": directory, "files": {}, "event": None, "checkpoint": None}
    directory = Path(target["directory"])
    for role in INPUT_ROLES:
        if role in target["files"]:
            continue
        copied = _copy_once(sources[role].path, directory / f"{role}.json")
        if copied.sha256 != sources[role].sha256 or copied.bytes != sources[role].bytes:
            raise VC0CloseoutError(f"{role} 收据复制结果漂移")
        target["files"][role] = copied.sha256
    return target


def _ensure_receipt_event(
    timing_root: Path,
    formal_campaign_id: str,
    target: dict[str, Any],
    event_ids: set[str],
    progress: _Progress,
) -> int:
    """这一组副本的「P0 收据通过」事件：已有就核对，缺了就补；返回事件序号。"""

    progress.step = "append-receipt-passed"
    if target["event"] is not None:
        problems = _set_event_problems(timing_root, target)
        if problems:
            raise VC0CloseoutError("；".join(problems))
        return int(target["event"]["sequence"])
    event_id = _receipt_event_id(formal_campaign_id, int(target["index"]))
    if event_id in event_ids:
        raise VC0CloseoutError(f"VC-0 收口 event_id 已存在：{event_id}")
    summary = codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=event_id,
        phase="VC-0",
        event_type="receipt_passed",
        receipts=_set_bindings(timing_root, target),
        live_request_count=0,
        next_action="创建 Formal Campaign 并立即启动 VC-1 首批",
    )
    event_ids.add(event_id)
    target["event"] = {"event_id": event_id, "sequence": int(summary["head_sequence"])}
    return int(summary["head_sequence"])


def _ensure_checkpoint(timing_root: Path, target: dict[str, Any], event_sequence: int, progress: _Progress) -> str:
    """这一组的账本 checkpoint（在它的收据通过事件之后、active VC-0）：已有就核对，缺了就写。"""

    progress.step = "create-active-timing-checkpoint"
    relative = (Path(target["directory"]) / "timing-vc0-active.json").relative_to(
        timing_root.resolve(strict=True)
    ).as_posix()
    if target.get("checkpoint") is None:
        checkpoint = codex_upgrade_timing_ledger.checkpoint(timing_root, relative)
    else:
        try:
            checkpoint = codex_upgrade_timing_ledger.replay(timing_root, relative)
        except (OSError, ValueError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
            raise VC0CloseoutError(f"已有的账本 checkpoint 重放失败：{error}") from error
    summary = checkpoint.get("summary", {})
    if summary.get("status") != "active" or summary.get("active_phase") != "VC-0":
        raise VC0CloseoutError("Formal plan 前的 timing checkpoint 非 active VC-0")
    if int(summary.get("head_sequence") or 0) < event_sequence:
        raise VC0CloseoutError("已有的账本 checkpoint 早于这一组收据的通过事件")
    target["checkpoint"] = relative
    return relative


def _ensure_formal_control_artifacts(
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    formal_manifest: Mapping[str, Any],
    progress: _Progress,
) -> dict[str, dict[str, Any]]:
    """Formal 控制产物（campaign-plan、vc-0 checkpoint）复制进账本：已有且逐字节相同就沿用。"""

    progress.step = "copy-formal-control-artifacts"
    ledger_root = timing_root.resolve(strict=True)
    namespace = _ensure_private_tree(_receipt_namespace(timing_root, formal_campaign_id), timing_root)
    control = formal_manifest.get("vc_control")
    if not isinstance(control, Mapping):
        raise VC0CloseoutError("Formal Campaign 缺少 VC 控制制品")
    result: dict[str, dict[str, Any]] = {}
    for field, output_name in (
        ("campaign_plan", "formal-campaign-plan.json"),
        ("vc0_checkpoint", "formal-vc0-checkpoint.json"),
    ):
        reference = control.get(field)
        if not isinstance(reference, Mapping):
            raise VC0CloseoutError(f"Formal Campaign 缺少 {field}")
        source = _inside(formal_campaign_dir, str(reference.get("path", "")), f"Formal {field}")
        digest = _sha256_file(source)
        if digest != reference.get("sha256"):
            raise VC0CloseoutError(f"Formal {field} 摘要漂移")
        destination = namespace / output_name
        if destination.exists() or destination.is_symlink():
            existing = _trusted_file(destination, f"Formal {field} 副本", maximum=MAX_RECEIPT_BYTES)
            if _sha256_file(existing) != digest:
                raise VC0CloseoutError(f"账本里已有的 Formal {field} 副本与 Formal 不一致")
            size = existing.stat().st_size
        else:
            copied = _copy_once(source, destination)
            if copied.sha256 != digest:
                raise VC0CloseoutError(f"Formal {field} 复制结果漂移")
            size = copied.bytes
        result[field] = {
            "path": destination.relative_to(ledger_root).as_posix(),
            "sha256": digest,
            "bytes": size,
        }
    return result


IDENTITY_COMPARE_FIELDS = (
    "files_sha256",
    "policy_version",
    "policy_sha256",
    "wire_producer_sha256",
    "evidence_semantics_sha256",
    "control_sha256",
)


def _identity_drift(frozen: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    return [field for field in IDENTITY_COMPARE_FIELDS if field in frozen and frozen.get(field) != current.get(field)]


def _assert_identity_matches_preflight(preflight_manifest: Mapping[str, Any]) -> None:
    """Formal 建成之前：当前工具身份必须与预检 Campaign 冻结身份整体一致（E2-07 第 5 点）。

    不一致说明预检之后改过受管工具（哪怕只是 control 层）：Job 演练合同按预检冻结身份签，建 Formal 必然对不上。
    在写任何账本事件之前拒绝，由入口按失效规则重建预检 Campaign 与其后各步。
    """

    frozen = preflight_manifest.get("tool_identity")
    if not isinstance(frozen, Mapping):
        raise VC0CloseoutError("preflight 缺少受管工具身份")
    drift = _identity_drift(frozen, codex_upgrade._tool_identity(include_git=False))
    if drift:
        raise VC0CloseoutError(
            "收口前预检：当前受管工具身份与预检 Campaign 冻结身份不一致（"
            + "、".join(drift)
            + "）；先按入口失效规则重建预检 Campaign、Job 演练、发布认证与 P0 收据，再收口"
        )


def _assert_identity_matches_formal(formal_campaign_dir: Path, formal_manifest: Mapping[str, Any]) -> None:
    """Formal 建成之后：当前工具身份必须等于 Formal 的有效身份（创建时冻结的，或最近一次登记的工具演进）。"""

    try:
        effective = codex_upgrade._campaign_effective_tool_identity(formal_campaign_dir, formal_manifest)["identity"]
    except (OSError, ValueError, codex_upgrade.ConfigurationError) as error:
        raise VC0CloseoutError(f"Formal 有效工具身份无法重放：{error}") from error
    drift = _identity_drift(effective, codex_upgrade._tool_identity(include_git=False))
    if drift:
        raise CloseoutBlocked(
            "当前受管工具身份不等于 Formal 的有效身份（" + "、".join(drift) + "）：Formal 建成之后修工具不重签、不重建，"
            "先在这个 Formal 上登记工具演进，再续作",
            kind="tool-evolution-required",
            next_command=(
                f"python3 -m tools.official_client_capture.codex_upgrade tool-evolution --campaign-dir {formal_campaign_dir} "
                "（先预览，再带 --approve-sha256 <review_sha256> --approved-by <批准人> 登记）"
            ),
        )


def _push_registration(formal_campaign_dir: Path, formal_campaign_id: str, progress: _Progress) -> None:
    """注册批次已 COMMIT 但还没推进总账时，用总账现有的补推入口补上；被拒绝则需人工处置。"""

    progress.step = "push-project-registration"
    root = codex_upgrade_project_ledger.find_project_ledger(formal_campaign_dir.parent)
    if root is None:
        return
    try:
        codex_upgrade_project_ledger.reconcile_project_ledger(root, campaign_dir=formal_campaign_dir)
        head = codex_upgrade_project_ledger.replay_head(root)
    except (OSError, ValueError, codex_upgrade_project_ledger.ProjectLedgerError) as error:
        raise VC0CloseoutError(f"项目总账补推注册失败：{error}") from error
    if formal_campaign_id in head.get("rejected_campaigns", {}):
        raise CloseoutBlocked(
            "项目总账拒绝了这个 Formal 的注册（campaign_registration_rejected）",
            kind="registration-rejected",
            next_command="按总账拒绝原因人工处置；同一 Formal ID 无法再注册",
        )
    if formal_campaign_id not in head.get("registered_campaigns", {}):
        raise VC0CloseoutError("项目总账里没有这个 Formal 的注册事件")


def _assert_can_continue(summary: Mapping[str, Any], *, phase: str, now: str) -> None:
    """续作写阶段事件或派发之前：账本仍 active、处在预期阶段、剩余时间不少于 300 秒。"""

    if summary.get("status") != "active" or summary.get("active_phase") != phase:
        raise VC0CloseoutError(
            f"UpgradeTimingLedger 当前是 {summary.get('status')}／{summary.get('active_phase')}，不是 active {phase}"
        )
    remaining = _remaining_seconds(summary, now)
    if remaining < MINIMUM_REMAINING_SECONDS:
        raise VC0CloseoutError(f"{phase} 剩余时间仅 {remaining} 秒，少于 {MINIMUM_REMAINING_SECONDS} 秒")


def _now_micro() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ledger_now(timing_root: Path) -> dict[str, Any]:
    try:
        return codex_upgrade_timing_ledger.inspect_ledger(timing_root, now=_now_micro())
    except (OSError, ValueError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise VC0CloseoutError(f"取得账本锁后的状态重放失败：{error}") from error


def _same_head(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (left.get("head_sequence"), left.get("head_sha256")) == (right.get("head_sequence"), right.get("head_sha256"))


def _closeout_once(
    arguments: argparse.Namespace,
    *,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
    site: dict[str, Any],
    progress: _Progress,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], ValidatedInputs | None]:
    """持锁完成 VC-0：Formal 未建时走收口 attempt 建 Formal；之后补控制产物副本、注册推送与两条阶段事件。

    返回 Formal 清单、控制产物副本与（Formal 未建时的）已校验输入。派发首批不在这里（要先放锁）。
    """

    validated: ValidatedInputs | None = None
    formal_manifest: dict[str, Any] | None = None
    if site["site"] in {"fresh", "pre-formal"}:
        progress.step = "validate-inputs"
        validated = validate_inputs(
            preflight_campaign_dir=Path(arguments.preflight_campaign_dir),
            p0_gate_root=Path(arguments.p0_gate_root),
            p0_gate_receipt=Path(arguments.p0_gate_receipt),
            managed_tool_deploy_receipt=Path(arguments.managed_tool_deploy_receipt),
            release_certification=Path(arguments.release_certification),
        )
        if validated.timing_ledger_dir.resolve(strict=True) != timing_root.resolve(strict=True):
            raise VC0CloseoutError("preflight 冻结的计时账本与收口输入的计时账本不一致")
        progress.step = "precheck-tool-identity"
        _assert_identity_matches_preflight(validated.preflight_manifest)
    else:
        formal_manifest = dict(site["_formal_manifest"])
        progress.step = "precheck-tool-identity"
        _assert_identity_matches_formal(formal_campaign_dir, formal_manifest)
    unlocked_head = dict(site["ledger"]["summary"])
    with _closeout_locks(timing_root, formal_campaign_dir):
        progress.step = "recheck-site"
        current = inspect_site(
            timing_root=timing_root,
            formal_campaign_id=formal_campaign_id,
            formal_campaign_dir=formal_campaign_dir,
            supervisor_state_dir=supervisor_state_dir,
        )
        if current["site"] != site["site"] or not _same_head(current["ledger"]["summary"], unlocked_head):
            raise VC0CloseoutError("取得账本锁前 UpgradeTimingLedger 已发生并发漂移（现场在加锁前变化），重跑续作")
        ledger = current["_ledger"]
        event_ids: set[str] = ledger["_event_ids"]
        progress.step = "close-interrupted-attempt"
        _close_interrupted_attempt(timing_root, ledger, progress)
        if progress.closed_interrupted:
            ledger = _closeout_ledger_facts(timing_root, formal_campaign_id)
            event_ids = ledger["_event_ids"]
        if ledger["_summary"].get("status") == "stop_required":
            raise _AtLimit()
        if validated is not None:
            progress.step = "recheck-active-vc0"
            _assert_active_vc0(
                _ledger_now(timing_root),
                validated.preflight_manifest,
                upgrade_id=str(validated.timing_summary["upgrade_id"]),
                evidence_decision=str(validated.timing_summary["evidence_decision"]),
                expected_total_live_request_count=int(validated.timing_summary["total_live_request_count"]),
                now=_now_micro(),
            )
            if current["site"] == "fresh":
                progress.step = "precheck-outputs"
                _precheck_outputs(
                    validated,
                    formal_campaign_dir=formal_campaign_dir,
                    formal_campaign_id=formal_campaign_id,
                    supervisor_state_dir=supervisor_state_dir,
                )
            else:
                _private_directory(formal_campaign_dir.parent, "Formal Campaign 父目录")
                _private_directory(supervisor_state_dir.parent, "supervisor state-dir 父目录")
            progress.step = "start-closeout-attempt"
            attempt_number = int(ledger["next_attempt"])
            _start_attempt(timing_root, formal_campaign_id, ledger, progress)
            try:
                progress.step = "archive-half-built-formal"
                _archive_half_built_formal(timing_root, formal_campaign_id, formal_campaign_dir, progress)
                target = _bind_receipt_set(
                    validated, timing_root, formal_campaign_id, current["_sets"], attempt_number, progress
                )
                event_sequence = _ensure_receipt_event(timing_root, formal_campaign_id, target, event_ids, progress)
                timing_relative = _ensure_checkpoint(timing_root, target, event_sequence, progress)
                progress.step = "recover-formal-plan"
                plan_arguments = recover_formal_plan_arguments(
                    validated,
                    formal_campaign_dir=formal_campaign_dir,
                    formal_campaign_id=formal_campaign_id,
                    timing_receipt=timing_root / timing_relative,
                )
                progress.step = "create-formal-campaign"
                codex_upgrade.create_campaign(plan_arguments)
                progress.created_formal = True
                progress.step = "replay-formal-campaign"
                formal_manifest = codex_upgrade.load_campaign_manifest(formal_campaign_dir)
                if (
                    formal_manifest.get("campaign_id") != formal_campaign_id
                    or formal_manifest.get("campaign_mode") != "formal"
                ):
                    raise VC0CloseoutError("Formal Campaign 创建结果身份漂移")
                artifacts = _ensure_formal_control_artifacts(
                    timing_root, formal_campaign_id, formal_campaign_dir, formal_manifest, progress
                )
                progress.step = "complete-closeout-attempt"
                _complete_attempt(timing_root, progress)
            except BaseException:
                _fail_attempt(timing_root, progress)
                raise
        else:
            assert formal_manifest is not None
            latest = ledger["attempts"][-1] if ledger["attempts"] else None
            copies_present = all(
                (_receipt_namespace(timing_root, formal_campaign_id) / name).is_file()
                for name in ("formal-campaign-plan.json", "formal-vc0-checkpoint.json")
            )
            if ledger["vc0_completed"] is None and (latest is None or latest["status"] != "completed" or not copies_present):
                # Formal 已建但收口 attempt 没有正常结束（被杀或复制控制产物时失败）：开新 attempt 补齐后关闭。
                _assert_can_continue(_ledger_now(timing_root), phase="VC-0", now=_now_micro())
                progress.step = "start-closeout-attempt"
                _start_attempt(timing_root, formal_campaign_id, ledger, progress)
                try:
                    artifacts = _ensure_formal_control_artifacts(
                        timing_root, formal_campaign_id, formal_campaign_dir, formal_manifest, progress
                    )
                    progress.step = "complete-closeout-attempt"
                    _complete_attempt(timing_root, progress)
                except BaseException:
                    _fail_attempt(timing_root, progress)
                    raise
            else:
                artifacts = _ensure_formal_control_artifacts(
                    timing_root, formal_campaign_id, formal_campaign_dir, formal_manifest, progress
                )
        _push_registration(formal_campaign_dir, formal_campaign_id, progress)
        progress.step = "complete-vc0"
        if f"{formal_campaign_id}-vc0-completed" not in event_ids:
            _assert_can_continue(_ledger_now(timing_root), phase="VC-0", now=_now_micro())
        _append_once(
            timing_root,
            event_ids,
            event_id=f"{formal_campaign_id}-vc0-completed",
            phase="VC-0",
            event_type="stage_completed",
            live_request_count=0,
            next_action="立即启动 VC-1 首批",
        )
        progress.step = "start-vc1"
        _append_once(
            timing_root,
            event_ids,
            event_id=f"{formal_campaign_id}-vc1-started",
            phase="VC-1",
            event_type="stage_started",
            live_request_count=0,
            next_action="由 campaign-run 执行首个 VC-1 冻结批次",
        )
        progress.step = "precheck-dispatch"
        _assert_can_continue(_ledger_now(timing_root), phase="VC-1", now=_now_micro())
    assert formal_manifest is not None
    return formal_manifest, artifacts, validated


def _dispatch_first_batch(
    arguments: argparse.Namespace,
    *,
    formal_campaign_dir: Path,
    formal_manifest: Mapping[str, Any],
    supervisor_state_dir: Path,
    progress: _Progress,
) -> dict[str, Any]:
    """放锁之后派发首批（监督器的失败收口要能非阻塞地取到账本锁）。"""

    progress.step = "dispatch-vc1"
    run_manifest_path = _first_run_manifest_path(formal_campaign_dir, formal_manifest)
    returncode, run_result = codex_upgrade_supervisor._campaign_run_command(
        argparse.Namespace(
            state_dir=supervisor_state_dir,
            manifest=run_manifest_path,
            heartbeat_seconds=float(arguments.heartbeat_seconds),
            watchdog_timeout_seconds=float(arguments.watchdog_timeout_seconds),
            ledger_interval_seconds=float(arguments.ledger_interval_seconds),
        )
    )
    if returncode != 0 or run_result.get("status") != "stopped" or run_result.get("reason") != "queue-complete":
        raise VC0CloseoutError("首个 VC-1 campaign-run 未形成 queue-complete 终态")
    return dict(run_result)


def _closeout_receipt(
    *,
    timing_root: Path,
    preflight_dir: Path,
    formal_campaign_dir: Path,
    formal_campaign_id: str,
    validated: ValidatedInputs | None,
    artifacts: Mapping[str, Any],
    run_result: Mapping[str, Any],
    site: Mapping[str, Any],
    progress: _Progress,
) -> dict[str, Any]:
    """收口收据：在账本锁内写，要求首批结束时账本没有停线。"""

    final_timing = codex_upgrade_timing_ledger.inspect_ledger(timing_root)
    if final_timing.get("status") != "active":
        raise VC0CloseoutError("VC-1 首批结束时原始时间预算已要求停线")
    sets = _receipt_sets(
        timing_root,
        formal_campaign_id,
        _closeout_ledger_facts(timing_root, formal_campaign_id)["receipt_events"],
    )
    bound = [item for item in sets if item["event"] is not None]
    latest = max(bound, key=lambda item: item["event"]["sequence"]) if bound else None
    preflight_manifest, _raw = _load_json(_trusted_file(preflight_dir / "campaign.json", "preflight Campaign 清单"), "preflight")
    input_receipts = (
        [
            {"role": item.role, "source_path": str(item.path), "sha256": item.sha256, "bytes": item.bytes}
            for item in validated.receipts
        ]
        if validated is not None
        else [
            {"role": binding["role"], "ledger_copy": binding["path"], "sha256": binding["sha256"]}
            for binding in (_set_bindings(timing_root, latest) if latest is not None else [])
        ]
    )
    return {
        "schema_version": CLOSEOUT_RECEIPT_SCHEMA,
        "status": "passed",
        "completed_at_utc": _utc_now(),
        "preflight_campaign": {
            "path": str(preflight_dir),
            "campaign_id": preflight_manifest.get("campaign_id"),
            "sha256": _sha256_file(preflight_dir / "campaign.json"),
        },
        "formal_campaign": {
            "path": str(formal_campaign_dir),
            "campaign_id": formal_campaign_id,
            "sha256": _sha256_file(formal_campaign_dir / "campaign.json"),
        },
        "input_receipts": input_receipts,
        "formal_control_artifacts": dict(artifacts),
        "timing_checkpoint": {
            "path": latest["checkpoint"] if latest is not None else None,
            "sha256": _sha256_file(timing_root / str(latest["checkpoint"])) if latest is not None and latest["checkpoint"] else None,
        },
        "timing_head_sequence": final_timing["head_sequence"],
        "timing_head_sha256": final_timing["head_sha256"],
        "vc1_campaign_run": dict(run_result),
        "deadline_extended": False,
        "create_campaign_call_count": 1 if progress.created_formal else 0,
        "campaign_run_call_count": 1,
        "continuation": {
            "site": site["site"],
            "closed_interrupted_attempts": list(progress.closed_interrupted or []),
            "archived_half_built_formal": list(progress.archived or []),
            "resumed_at_limit": progress.resumed_at_limit,
        },
    }


# ---------------------------------------------------------------------------
# E2-07：同根因到上限之后的恢复（第 3 点）
# ---------------------------------------------------------------------------


def _default_control_root(formal_campaign_dir: Path) -> Path:
    """部署收据所在的控制根：缺省是 Formal 所在数据根下的 control（演练根要显式给生产数据根的 control）。"""

    return formal_campaign_dir.parent.parent.parent / "control"


def _ledger_resume_preview(
    *,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
    fix_commit: str,
    regression_receipt: Path,
    reason: str,
    control_root: Path,
) -> dict[str, Any]:
    """Formal 未建、收口同根因到上限：恢复预览。证据与 campaign-resume 相同（修复提交、离线回归收据、晚于最后
    一次失败的通过部署收据），批准后写账本现有的 recovery_verified（campaign-resume 形态三份收据）。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    if not FIX_COMMIT_RE.fullmatch(fix_commit):
        raise VC0CloseoutError("--fix-commit 必须是 40 位小写十六进制提交号")
    if not reason.strip():
        raise VC0CloseoutError("--reason 不得为空")
    site = inspect_site(
        timing_root=timing_root,
        formal_campaign_id=formal_campaign_id,
        formal_campaign_dir=formal_campaign_dir,
        supervisor_state_dir=supervisor_state_dir,
    )
    if site["site"] != "pre-formal":
        raise VC0CloseoutError(
            f"上限后的账本恢复只用于 Formal 未建的收口（现场是 {site['site']}）；Formal 已建用 campaign-resume"
        )
    if site["ledger"]["active_attempt"] is not None:
        raise VC0CloseoutError("还有进行中的收口 attempt：先重跑收口命令把中断的 attempt 记失败，再预览")
    try:
        facts = codex_upgrade_timing_ledger.resume_facts(timing_root)
    except (OSError, ValueError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise VC0CloseoutError(f"计时账本无法重放：{error}") from error
    if facts["status"] != "stop_required":
        raise VC0CloseoutError(f"计时账本状态为 {facts['status']}，不是同根因到上限（stop_required）")
    closeout_causes = {
        row["failure_root_cause_id"] for row in site["ledger"]["attempts"] if row["status"] == "failed"
    }
    at_limit = list(facts["at_limit_root_cause_ids"])
    if not at_limit or not set(at_limit) <= closeout_causes:
        raise VC0CloseoutError("达到上限的根因不全是本 Formal 收口 attempt 的失败，不能由收口恢复")
    current = codex_upgrade._tool_identity(include_git=False)
    try:
        deployment = reconciler._deployment_receipt(control_root, current, required=True)
    except (reconciler.ReconcilerError, VC0CloseoutError, OSError) as error:
        raise VC0CloseoutError(f"上限后的恢复必须绑定当前工具的通过部署收据：{error}") from error
    assert deployment is not None
    limit_event = facts.get("limit_event")
    if limit_event is not None and _timestamp(deployment["created_at_utc"], "部署收据时间") <= _timestamp(
        limit_event["recorded_at_utc"], "最后一次失败时间"
    ):
        raise VC0CloseoutError("部署收据早于最后一次失败；修复必须在失败之后受监督部署")
    regression = _trusted_file(Path(regression_receipt), "离线回归收据", maximum=MAX_RECEIPT_BYTES)
    preview: dict[str, Any] = {
        "schema_version": LEDGER_RESUME_PREVIEW_SCHEMA,
        "formal_campaign_id": formal_campaign_id,
        "root_cause_id": at_limit[0],
        "timing": {
            "status": facts["status"],
            "stop_event": None,
            "cleared_root_cause_ids": at_limit,
            "limit_event": None
            if limit_event is None
            else {key: limit_event[key] for key in ("sequence", "sha256", "event_id", "recorded_at_utc")},
            "resume_epoch": int(facts["resume_epoch"]),
            "campaign_ledger_head": {"sequence": facts["head_sequence"], "sha256": facts["head_sha256"]},
        },
        "bindings": {
            "fix_commit": fix_commit,
            "regression_receipt": {"path": str(regression), "sha256": _sha256_file(regression)},
            "deployment_receipt": {
                "path": str(deployment["path"]),
                "sha256": str(deployment["sha256"]),
                "created_at_utc": deployment["created_at_utc"],
            },
        },
        "reason": reason,
        "live_request_count": 0,
    }
    preview["review_sha256"] = codex_upgrade._fingerprint(preview)
    return preview


def ledger_resume(
    *,
    preflight_campaign_dir: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
    fix_commit: str,
    regression_receipt: Path,
    reason: str,
    approve_sha256: str | None = None,
    approved_by: str | None = None,
    control_root: Path | None = None,
) -> dict[str, Any]:
    """上限后的账本恢复：不带批准只预览；批准后写批准收据、三份收据副本与账本恢复事件（幂等，可同一批准重跑）。"""

    formal_campaign_id = _safe_id(formal_campaign_id, "formal_campaign_id")
    formal_campaign_dir = Path(formal_campaign_dir)
    timing_root = _ledger_dir_from_preflight(Path(preflight_campaign_dir))
    control = Path(control_root) if control_root is not None else _default_control_root(formal_campaign_dir)
    with _ledger_lock(timing_root):
        resume_root = _receipt_namespace(timing_root, formal_campaign_id) / "resume"
        if approve_sha256 is not None:
            stored_path = resume_root / f"resume-{approve_sha256}.json"
            if stored_path.is_file() and not stored_path.is_symlink():
                stored, _raw = _load_json(_trusted_file(stored_path, "上限后恢复批准收据"), "上限后恢复批准收据")
                if stored.get("approved_sha256") != approve_sha256:
                    raise VC0CloseoutError("既有上限后恢复批准收据与批准摘要不一致")
                return _apply_ledger_resume(timing_root, stored_path, stored)
        preview = _ledger_resume_preview(
            timing_root=timing_root,
            formal_campaign_id=formal_campaign_id,
            formal_campaign_dir=formal_campaign_dir,
            supervisor_state_dir=Path(supervisor_state_dir),
            fix_commit=fix_commit,
            regression_receipt=Path(regression_receipt),
            reason=reason,
            control_root=control,
        )
        if approve_sha256 is None:
            return {
                "status": "approval_required",
                **preview,
                "next_command": "同一命令加 --approve-sha256 <review_sha256> --approved-by <批准人>",
            }
        if approve_sha256 != preview["review_sha256"]:
            raise VC0CloseoutError("批准摘要与重算的预览不一致；重新预览后再批准")
        approver = str(approved_by or "").strip()
        if not approver:
            raise VC0CloseoutError("批准上限后的恢复必须提供 --approved-by")
        timing = preview["timing"]
        receipt: dict[str, Any] = {
            "schema_version": codex_upgrade_timing_ledger.CAMPAIGN_RESUME_SCHEMA,
            "campaign_id": formal_campaign_id,
            "root_cause_id": preview["root_cause_id"],
            "campaign_ledger_head": dict(timing["campaign_ledger_head"]),
            "timing_stop_event": None,
            "timing_cleared_root_cause_ids": list(timing["cleared_root_cause_ids"]),
            "project_terminal": None,
            "project_cleared_root_cause_ids": [],
            "bindings": preview["bindings"],
            "reason": preview["reason"],
            "preview": dict(preview),
            "approved_sha256": preview["review_sha256"],
            "approved_by": approver,
            "approved_at_utc": _utc_now(),
        }
        receipt["receipt_sha256"] = codex_upgrade._fingerprint(receipt)
        _ensure_private_tree(resume_root, timing_root)
        receipt_path = resume_root / f"resume-{preview['review_sha256']}.json"
        _write_once(receipt_path, receipt)
        return _apply_ledger_resume(timing_root, receipt_path, receipt)


def _apply_ledger_resume(timing_root: Path, receipt_path: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """批准之后：三份收据复制进账本（campaign-resume 同一命名），追加 recovery_verified；已写过就幂等返回。持账本锁调用。"""

    review = str(receipt["approved_sha256"])
    event_id = f"campaign-resume-{review[:16]}"
    existing = [event for event, _raw in codex_upgrade_timing_ledger._load_events(timing_root) if event["event_id"] == event_id]
    if not existing:
        bindings = receipt["bindings"]
        copies_dir = _ensure_private_tree(timing_root / "receipts" / "campaign-resume" / review[:16], timing_root)
        ledger_root = timing_root.resolve(strict=True)
        ledger_receipts: list[dict[str, str]] = []
        for role, destination_name, source in sorted(
            (
                ("campaign_resume", "campaign-resume.json", receipt_path),
                ("offline_regression", "offline-regression.receipt", Path(bindings["regression_receipt"]["path"])),
                ("tool_fix", "tool-fix-deployment.json", Path(bindings["deployment_receipt"]["path"])),
            )
        ):
            destination = copies_dir / destination_name
            if destination.exists() or destination.is_symlink():
                digest = _sha256_file(_trusted_file(destination, f"{role} 收据副本", maximum=MAX_RECEIPT_BYTES))
                if digest != _sha256_file(_trusted_file(source, f"{role} 收据", maximum=MAX_RECEIPT_BYTES)):
                    raise VC0CloseoutError(f"账本内既有 {role} 收据副本与本次不一致")
            else:
                digest = _copy_once(source, destination).sha256
            ledger_receipts.append(
                {"role": role, "path": destination.relative_to(ledger_root).as_posix(), "sha256": digest}
            )
        expected = {
            "offline_regression": bindings["regression_receipt"]["sha256"],
            "tool_fix": bindings["deployment_receipt"]["sha256"],
        }
        if any(item["sha256"] != expected[item["role"]] for item in ledger_receipts if item["role"] in expected):
            raise VC0CloseoutError("回归收据或部署收据在预览后被修改")
        codex_upgrade_timing_ledger.append_event(
            timing_root,
            event_id=event_id,
            phase="VC-0",
            event_type="recovery_verified",
            root_cause_id=str(receipt["root_cause_id"]),
            receipts=ledger_receipts,
            next_action="上限后的恢复已登记：同一 Formal ID、同一账本重跑收口续作",
        )
    summary = codex_upgrade_timing_ledger.inspect_ledger(timing_root)
    return {
        "status": "resumed",
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt.get("receipt_sha256"),
        "event_id": event_id,
        "timing_status": summary.get("status"),
        "active_phase": summary.get("active_phase"),
    }


def _resolve_at_limit(
    arguments: argparse.Namespace,
    *,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    site: Mapping[str, Any],
    progress: _Progress,
) -> None:
    """同根因到上限（账本拒绝第三次 attempt）：Formal 未建走账本恢复，已建走 campaign-resume；都要修复证据与批准。

    恢复命令自己取锁（campaign-resume 先项目锁后账本锁），所以必须在收口的锁外调用。成功后返回，由调用方重新判定现场。
    """

    progress.step = "resume-at-limit"
    fix_commit = getattr(arguments, "fix_commit", None)
    regression = getattr(arguments, "regression_receipt", None)
    reason = str(getattr(arguments, "reason", None) or "").strip()
    approve = getattr(arguments, "approve_sha256", None)
    approved_by = getattr(arguments, "approved_by", None)
    control_root = getattr(arguments, "control_root", None)
    evidence_hint = (
        "修复并受监督部署后，带 --fix-commit <修复提交> --regression-receipt <离线回归收据> --reason <理由> 重跑本命令"
        "得到预览摘要，再加 --approve-sha256 <review_sha256> --approved-by <批准人> 批准"
    )
    if site["site"] == "formal-built":
        if not fix_commit or regression is None or not reason:
            raise CloseoutBlocked(
                "同一根因已连续失败两次（账本拒绝第三次）：Formal 已建，用 campaign-resume 恢复",
                kind="campaign-resume-required",
                next_command=evidence_hint,
            )
        argv = [
            "campaign-resume",
            "--campaign-dir",
            str(formal_campaign_dir),
            "--fix-commit",
            str(fix_commit),
            "--regression-receipt",
            str(regression),
            "--reason",
            reason,
        ]
        if control_root is not None:
            argv += ["--control-root", str(control_root)]
        if approve:
            argv += ["--approve-sha256", str(approve), "--approved-by", str(approved_by or "")]
        try:
            result = codex_upgrade._campaign_resume_command(codex_upgrade._build_parser().parse_args(argv))
        except (OSError, ValueError, codex_upgrade.ConfigurationError) as error:
            raise VC0CloseoutError(f"campaign-resume 失败：{error}") from error
        if result.get("status") == "approval_required":
            raise CloseoutBlocked(
                "同一根因已连续失败两次：campaign-resume 预览已生成，等待批准",
                kind="approval-required",
                next_command=f"同一命令加 --approve-sha256 {result.get('review_sha256')} --approved-by <批准人>",
                details={"review_sha256": result.get("review_sha256"), "preview": result},
            )
        progress.resumed_at_limit = {"kind": "campaign-resume", "receipt_sha256": result.get("receipt_sha256")}
        return
    if site["ledger"]["active_attempt"] is not None:
        # 账本 stop_required 时仍允许只登记失败的 attempt_failed：先把被杀留下的 attempt 关掉，再做恢复预览。
        with _closeout_locks(timing_root, formal_campaign_dir):
            _close_interrupted_attempt(timing_root, _closeout_ledger_facts(timing_root, formal_campaign_id), progress)
    if not fix_commit or regression is None or not reason:
        raise CloseoutBlocked(
            "同一根因已连续失败两次（账本拒绝第三次）：Formal 未建，走收口的上限后恢复",
            kind="ledger-resume-required",
            next_command=evidence_hint,
        )
    result = ledger_resume(
        preflight_campaign_dir=Path(arguments.preflight_campaign_dir),
        formal_campaign_id=formal_campaign_id,
        formal_campaign_dir=formal_campaign_dir,
        supervisor_state_dir=Path(arguments.supervisor_state_dir),
        fix_commit=str(fix_commit),
        regression_receipt=Path(regression),
        reason=reason,
        approve_sha256=approve,
        approved_by=approved_by,
        control_root=Path(control_root) if control_root is not None else None,
    )
    if result.get("status") == "approval_required":
        raise CloseoutBlocked(
            "同一根因已连续失败两次：上限后恢复的预览已生成，等待批准",
            kind="approval-required",
            next_command=f"同一命令加 --approve-sha256 {result['review_sha256']} --approved-by <批准人>",
            details={"review_sha256": result["review_sha256"], "preview": result},
        )
    progress.resumed_at_limit = {"kind": "ledger-resume", "receipt_sha256": result.get("receipt_sha256")}


# ---------------------------------------------------------------------------
# E2-07：首批已派发之后的续作（第 4 点第三行）
# ---------------------------------------------------------------------------


def _official_attempt_rows(formal_campaign_dir: Path) -> list[dict[str, Any]]:
    """Formal 里的官方 attempt：是否已发布预约、attempt.json 的状态；按预约发布时间排序（没有预约的排最前）。"""

    root = formal_campaign_dir / "official" / "attempts"
    rows: list[dict[str, Any]] = []
    if not root.is_dir() or root.is_symlink():
        return rows
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            raise VC0CloseoutError(f"官方 attempt 目录不可信：{child.name}")
        reservation = child / "reservation.json"
        status = None
        attempt_path = child / "attempt.json"
        if attempt_path.is_file() and not attempt_path.is_symlink():
            payload, _raw = _load_json(_trusted_file(attempt_path, "官方 attempt"), "官方 attempt")
            status = payload.get("status")
        reserved = reservation.is_file() and not reservation.is_symlink()
        rows.append(
            {
                "attempt_id": child.name,
                "reservation": reserved,
                "status": status,
                "_order": reservation.stat().st_mtime_ns if reserved else 0,
            }
        )
    rows.sort(key=lambda row: (row["_order"], row["attempt_id"]))
    for row in rows:
        row.pop("_order")
    return rows


def _quiescence_problems(formal_campaign_dir: Path, supervisor: Mapping[str, Any]) -> list[str]:
    """续作已派发现场之前：首批父 run 与采集进程都必须已经结束（硬杀收口进程不会杀掉动作进程组）。"""

    problems = list(codex_upgrade._tool_evolution_quiescence_problems(formal_campaign_dir))
    if supervisor.get("lock_busy"):
        problems.append("监督器状态目录的动作队列锁仍被占用（首批父 run 还在跑）")
    for run in supervisor.get("runs", []):
        if run.get("state") in codex_upgrade_supervisor.ACTIVE_STATES and _pid_alive(run.get("owner_pid")):
            problems.append(f"父 run {Path(str(run['run_dir'])).name} 的 owner 进程仍在")
    return problems


def _write_handover(timing_root: Path, formal_campaign_id: str, payload: Mapping[str, Any]) -> Path:
    """恢复记录（只写一次、逐次编号）：把现场判定、对账结论与恢复结果绑在一起，供处置与复盘。"""

    root = _ensure_private_tree(_receipt_namespace(timing_root, formal_campaign_id) / "vc1-handover", timing_root)
    existing = sorted(int(path.stem) for path in root.glob("[0-9][0-9][0-9][0-9].json"))
    path = root / f"{(existing[-1] if existing else 0) + 1:04d}.json"
    _write_once(path, {"schema_version": VC1_HANDOVER_SCHEMA, "recorded_at_utc": _utc_now(), **payload})
    return path


def _recovery_action_plan(
    formal_campaign_dir: Path,
    first_manifest: Mapping[str, Any],
    *,
    preview_path: Path | None,
    execute: list[str],
    reuse: list[str],
) -> dict[str, Any]:
    """与录制回放链同形的 VC-1 恢复批次：零请求恢复预览，或按已批准预览补跑。命令前缀取首批 capture-official 动作的，
    监督器的恢复链协议要求两者前缀一致。"""

    actions = first_manifest.get("actions")
    command = actions[0].get("command") if isinstance(actions, list) and actions and isinstance(actions[0], Mapping) else None
    if not isinstance(command, list) or command.count("capture-official") != 1:
        raise VC0CloseoutError("首批清单缺少唯一 capture-official 动作，无法生成恢复批次")
    prefix = [str(item) for item in command[: command.index("capture-official")]]
    base = [*prefix, "resume", "--campaign-dir", str(formal_campaign_dir), "--rerun-failed"]
    if preview_path is None:
        action_id, arguments, timeout = "preview-official-recovery", ["--preview-recovery"], 1800.0
    else:
        first_timeout = float(actions[0].get("timeout_seconds") or 0)
        action_id = "run-official-recovery"
        arguments = ["--recovery-preview", str(preview_path), "--acknowledge-live-requests"]
        timeout = max(first_timeout, 600.0)
    return {
        "schema_version": codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": list(execute),
        "reuse_item_ids": list(reuse),
        "actions": [
            {
                "action_id": action_id,
                "operation": "VC-1:official-recovery",
                "timeout_seconds": timeout,
                "command": [*base, *arguments],
                "item_ids": list(execute),
            }
        ],
    }


def _dispatch_recovery_batch(
    arguments: argparse.Namespace,
    *,
    audit_dir: Path,
    formal_campaign_dir: Path,
    formal_manifest: Mapping[str, Any],
    supervisor_state_dir: Path,
    plan: Mapping[str, Any],
    name: str,
) -> dict[str, Any]:
    """以 compile-and-run-vc-batch 派发一个 VC-1 恢复批次（序号接在已提交批次之后，前序 checkpoint 是 VC-0 的）。"""

    sequence = max(codex_upgrade._committed_vc_sequences(formal_campaign_dir, formal_manifest), default=0) + 1
    plans_root = audit_dir / "action-plans"
    if not plans_root.exists():
        plans_root.mkdir(mode=0o700)
    plan_path = plans_root / f"{sequence:04d}-{name}.json"
    _write_once(plan_path, plan)
    result, returncode = codex_upgrade.compile_and_run_vc_batch(
        argparse.Namespace(
            campaign_dir=formal_campaign_dir,
            state_dir=supervisor_state_dir,
            phase="VC-1",
            sequence=sequence,
            predecessor_checkpoint=formal_campaign_dir / "control" / "vc" / "vc-0-checkpoint.json",
            action_plan=plan_path,
            heartbeat_seconds=float(arguments.heartbeat_seconds),
            watchdog_timeout_seconds=float(arguments.watchdog_timeout_seconds),
            ledger_interval_seconds=float(arguments.ledger_interval_seconds),
        )
    )
    run = result.get("campaign_run") if isinstance(result, Mapping) else None
    run = run if isinstance(run, Mapping) else {}
    return {
        "sequence": sequence,
        "action_plan": str(plan_path),
        "returncode": returncode,
        "status": run.get("status"),
        "reason": run.get("reason"),
        "run_dir": run.get("run_dir"),
    }


def _continue_dispatched(
    arguments: argparse.Namespace,
    *,
    audit_dir: Path,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    supervisor_state_dir: Path,
    site: dict[str, Any],
    progress: _Progress,
) -> dict[str, Any]:
    """首批已派发：不再派发首批、不重跑收口。首批（或其恢复）已跑完就补写收口收据；否则按指南对账
    （有预约 reconcile-attempt，否则 reconcile-supervisor-run），可恢复且批准过恢复预览时只补跑没完成的作业。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    progress.step = "vc1-handover"
    formal_manifest = site["_formal_manifest"]
    problems = _quiescence_problems(formal_campaign_dir, site["supervisor"])
    if problems:
        raise CloseoutBlocked(
            "首批父 run 或采集进程还没结束：" + "；".join(problems),
            kind="not-quiescent",
            next_command="等首批父 run 与采集进程结束（或超时被监督器封口）后重跑本命令",
        )
    first_runs = [run for run in site["supervisor"]["runs"] if run["first_batch"]]
    attempts = _official_attempt_rows(formal_campaign_dir)
    public = _public_site(site)
    finished_run = next((run for run in first_runs if run["stop_reason"] == "queue-complete"), None)
    finished_attempt = next(
        (row for row in reversed(attempts) if row["status"] in {"awaiting_receipts", "complete"}), None
    )
    if finished_run is not None or finished_attempt is not None:
        return _write_continuation_receipt(
            arguments,
            audit_dir=audit_dir,
            timing_root=timing_root,
            formal_campaign_id=formal_campaign_id,
            formal_campaign_dir=formal_campaign_dir,
            site=site,
            run_result={
                "status": "stopped" if finished_run is not None else None,
                "reason": finished_run["stop_reason"] if finished_run is not None else None,
                "run_dir": finished_run["run_dir"] if finished_run is not None else None,
                "completed_attempt": finished_attempt,
            },
            recovery=None,
            progress=progress,
        )
    control_root = getattr(arguments, "control_root", None)
    control_root = Path(control_root) if control_root is not None else None
    reserved = [row for row in attempts if row["reservation"]]
    progress.step = "vc1-reconcile"
    try:
        if reserved:
            target_attempt = str(reserved[-1]["attempt_id"])
            reconcile_kind = "reconcile-attempt"
            result = reconciler.reconcile_attempt(formal_campaign_dir, target_attempt, control_root=control_root)
        elif first_runs:
            target_attempt = None
            reconcile_kind = "reconcile-supervisor-run"
            result = reconciler.reconcile_supervisor_run(
                Path(str(first_runs[-1]["run_dir"])), formal_campaign_dir, control_root=control_root
            )
        else:
            raise CloseoutBlocked(
                "有派发痕迹，但找不到首批父 run，也没有已发布预约的官方 attempt",
                kind="inconsistent",
            )
    except (reconciler.ReconcilerError, codex_upgrade.ConfigurationError, OSError) as error:
        raise VC0CloseoutError(f"首批对账失败：{error}") from error
    preview_path = result.get("recovery_preview_path")
    review = (result.get("recovery_preview") or {}).get("review_sha256") if isinstance(result.get("recovery_preview"), Mapping) else None
    reconcile_view = {
        "kind": reconcile_kind,
        "attempt_id": target_attempt,
        "status": result.get("status"),
        "decision": result.get("decision"),
        "next_command": result.get("next_command"),
        "recovery_preview_path": preview_path,
        "review_sha256": review,
    }
    approve = getattr(arguments, "approve_sha256", None)
    if reconcile_kind != "reconcile-attempt" or result.get("status") != "recoverable" or not preview_path or not review:
        _write_handover(timing_root, formal_campaign_id, {"site": public, "reconcile": reconcile_view, "recovery": None})
        raise CloseoutBlocked(
            f"首批已派发，对账结论是 {result.get('status')}：按对账给出的下一步处置",
            kind="vc1-reconcile",
            next_command=str(result.get("next_command") or ""),
            details={"reconcile": reconcile_view},
        )
    if approve != review:
        _write_handover(timing_root, formal_campaign_id, {"site": public, "reconcile": reconcile_view, "recovery": None})
        raise CloseoutBlocked(
            "首批已派发、对账可恢复：批准恢复预览后只补跑没完成的作业",
            kind="approval-required",
            next_command=f"同一命令加 --approve-sha256 {review} --approved-by <批准人>",
            # 待批准的摘要与其它「需要批准」一样放在明细顶层（编排器与验收按同一个键取）。
            details={"review_sha256": review, "reconcile": reconcile_view},
        )
    progress.step = "vc1-recovery-approve"
    preview_payload, _raw = _load_json(_trusted_file(Path(str(preview_path)), "恢复预览"), "恢复预览")
    execute = [str(item) for item in preview_payload.get("execute_job_ids") or []]
    reuse = [str(item) for item in (preview_payload.get("reuse_job_ids") or preview_payload.get("reused_job_ids") or [])]
    try:
        reconciler.reconcile_attempt(
            formal_campaign_dir, str(target_attempt), control_root=control_root, approve_recovery_sha256=str(review)
        )
        authorized = reconciler.authorize_recovery_preview(formal_campaign_dir, str(target_attempt), Path(str(preview_path)))
    except (reconciler.ReconcilerError, codex_upgrade.ConfigurationError, OSError) as error:
        raise VC0CloseoutError(f"批准或授权恢复预览失败：{error}") from error
    _first_path, first_manifest = _first_run_manifest(formal_campaign_dir, formal_manifest)
    progress.step = "vc1-recovery-preview"
    preview_batch = _dispatch_recovery_batch(
        arguments,
        audit_dir=audit_dir,
        formal_campaign_dir=formal_campaign_dir,
        formal_manifest=formal_manifest,
        supervisor_state_dir=supervisor_state_dir,
        plan=_recovery_action_plan(formal_campaign_dir, first_manifest, preview_path=None, execute=execute, reuse=reuse),
        name="recovery-preview",
    )
    recovery: dict[str, Any] = {
        "approved_sha256": review,
        "authorized": authorized.get("status"),
        "execute_job_ids": execute,
        "reuse_job_ids": reuse,
        "batches": [preview_batch],
    }
    if preview_batch["returncode"] != 0:
        _write_handover(timing_root, formal_campaign_id, {"site": public, "reconcile": reconcile_view, "recovery": recovery})
        raise VC0CloseoutError("VC-1 零请求恢复预览批次失败；按 VC-1 对账恢复链处置后重跑本命令")
    progress.step = "vc1-recovery-run"
    run_batch = _dispatch_recovery_batch(
        arguments,
        audit_dir=audit_dir,
        formal_campaign_dir=formal_campaign_dir,
        formal_manifest=formal_manifest,
        supervisor_state_dir=supervisor_state_dir,
        plan=_recovery_action_plan(
            formal_campaign_dir, first_manifest, preview_path=Path(str(preview_path)), execute=execute, reuse=reuse
        ),
        name="recovery-run",
    )
    recovery["batches"].append(run_batch)
    latest = (_official_attempt_rows(formal_campaign_dir) or [None])[-1]
    recovery["final_attempt"] = latest
    _write_handover(timing_root, formal_campaign_id, {"site": public, "reconcile": reconcile_view, "recovery": recovery})
    if run_batch["returncode"] != 0 or latest is None or latest.get("status") not in {"awaiting_receipts", "complete"}:
        raise VC0CloseoutError("VC-1 按预览补跑批次没有跑完；按 VC-1 对账恢复链处置后重跑本命令")
    return _write_continuation_receipt(
        arguments,
        audit_dir=audit_dir,
        timing_root=timing_root,
        formal_campaign_id=formal_campaign_id,
        formal_campaign_dir=formal_campaign_dir,
        site=site,
        run_result={"status": run_batch["status"], "reason": run_batch["reason"], "run_dir": run_batch["run_dir"],
                    "completed_attempt": latest},
        recovery=recovery,
        progress=progress,
    )


def _write_continuation_receipt(
    arguments: argparse.Namespace,
    *,
    audit_dir: Path,
    timing_root: Path,
    formal_campaign_id: str,
    formal_campaign_dir: Path,
    site: Mapping[str, Any],
    run_result: Mapping[str, Any],
    recovery: Mapping[str, Any] | None,
    progress: _Progress,
) -> dict[str, Any]:
    """首批（或其恢复）已跑完：补写收口收据（续作形态，带首批运行事实与恢复记录）。"""

    progress.step = "write-closeout-receipt"
    with _ledger_lock(timing_root):
        artifacts = {}
        namespace = _receipt_namespace(timing_root, formal_campaign_id)
        ledger_root = timing_root.resolve(strict=True)
        for field, name in (("campaign_plan", "formal-campaign-plan.json"), ("vc0_checkpoint", "formal-vc0-checkpoint.json")):
            path = namespace / name
            if path.is_file() and not path.is_symlink():
                artifacts[field] = {"path": path.relative_to(ledger_root).as_posix(), "sha256": _sha256_file(path),
                                    "bytes": path.stat().st_size}
        receipt = _closeout_receipt(
            timing_root=timing_root,
            preflight_dir=Path(arguments.preflight_campaign_dir),
            formal_campaign_dir=formal_campaign_dir,
            formal_campaign_id=formal_campaign_id,
            validated=None,
            artifacts=artifacts,
            run_result=run_result,
            site=site,
            progress=progress,
        )
        receipt["campaign_run_call_count"] = 0
        receipt["vc1_recovery"] = dict(recovery) if recovery is not None else None
        _write_once(audit_dir / "receipt.json", receipt)
    return receipt


def closeout(arguments: argparse.Namespace) -> dict[str, Any]:
    """执行（或续作）VC-0 收口与 VC-1 首批派发；同一 Formal ID 重跑即续作（E2-07）。"""

    os.umask(0o077)
    formal_campaign_dir = Path(arguments.formal_campaign_dir)
    supervisor_state_dir = Path(arguments.supervisor_state_dir)
    audit_dir: Path | None = None
    formal_campaign_id: str | None = None
    timing_root: Path | None = None
    progress = _Progress()
    try:
        audit_dir = _new_private_directory(Path(arguments.audit_dir), "closeout audit-dir")
        progress.step = "validate-request"
        formal_campaign_id = _safe_id(arguments.formal_campaign_id, "formal_campaign_id")
        _assert_event_id_room(formal_campaign_id)
        for field in ("heartbeat_seconds", "watchdog_timeout_seconds", "ledger_interval_seconds"):
            codex_upgrade_supervisor._positive_seconds(getattr(arguments, field), field)
        request = {
            "preflight_campaign_dir": str(arguments.preflight_campaign_dir),
            "formal_campaign_dir": str(formal_campaign_dir),
            "formal_campaign_id": formal_campaign_id,
            "p0_gate_root": str(arguments.p0_gate_root),
            "p0_gate_receipt": str(arguments.p0_gate_receipt),
            "managed_tool_deploy_receipt": str(arguments.managed_tool_deploy_receipt),
            "release_certification": str(arguments.release_certification),
            "supervisor_state_dir": str(supervisor_state_dir),
            "approve_sha256": getattr(arguments, "approve_sha256", None),
            "requested_at_utc": _utc_now(),
        }
        _write_once(audit_dir / "request.json", request)
        progress.step = "inspect-site"
        timing_root = _ledger_dir_from_preflight(Path(arguments.preflight_campaign_dir))
        for round_index in range(3):
            site = inspect_site(
                timing_root=timing_root,
                formal_campaign_id=formal_campaign_id,
                formal_campaign_dir=formal_campaign_dir,
                supervisor_state_dir=supervisor_state_dir,
            )
            if round_index == 0:
                _write_once(audit_dir / "site.json", _public_site(site))
            if site["site"] == "inconsistent":
                raise CloseoutBlocked(
                    "现场判定不了，拒绝续作、不动现场：" + "；".join(site["problems"]),
                    kind="inconsistent",
                    details={"problems": site["problems"]},
                )
            if site["site"] == "dispatched":
                return _continue_dispatched(
                    arguments,
                    audit_dir=audit_dir,
                    timing_root=timing_root,
                    formal_campaign_id=formal_campaign_id,
                    formal_campaign_dir=formal_campaign_dir,
                    supervisor_state_dir=supervisor_state_dir,
                    site=site,
                    progress=progress,
                )
            if site["ledger"]["summary"].get("status") == "stop_required":
                _resolve_at_limit(arguments, timing_root=timing_root, formal_campaign_id=formal_campaign_id,
                                  formal_campaign_dir=formal_campaign_dir, site=site, progress=progress)
                continue
            try:
                formal_manifest, artifacts, validated = _closeout_once(
                    arguments,
                    timing_root=timing_root,
                    formal_campaign_id=formal_campaign_id,
                    formal_campaign_dir=formal_campaign_dir,
                    supervisor_state_dir=supervisor_state_dir,
                    site=site,
                    progress=progress,
                )
            except _AtLimit:
                continue
            break
        else:
            raise VC0CloseoutError("上限后的恢复之后现场仍未就绪，停止续作")
        run_result = _dispatch_first_batch(
            arguments,
            formal_campaign_dir=formal_campaign_dir,
            formal_manifest=formal_manifest,
            supervisor_state_dir=supervisor_state_dir,
            progress=progress,
        )
        progress.step = "write-closeout-receipt"
        with _ledger_lock(timing_root):
            receipt = _closeout_receipt(
                timing_root=timing_root,
                preflight_dir=Path(arguments.preflight_campaign_dir),
                formal_campaign_dir=formal_campaign_dir,
                formal_campaign_id=formal_campaign_id,
                validated=validated,
                artifacts=artifacts,
                run_result=run_result,
                site=site,
                progress=progress,
            )
            _write_once(audit_dir / "receipt.json", receipt)
        return receipt
    except BaseException as error:
        closure = progress.closure
        if closure is None and timing_root is not None:
            if progress.step in VC1_OWNED_STEPS:
                closure = {
                    "status": "vc1-owned",
                    "event_id": None,
                    "next_action": "首批已派发：监督器失败收口与 VC-1 对账恢复链负责；重跑本命令按「已派发」续作",
                }
            else:
                closure = {"status": "not-required", "event_id": None}
        if audit_dir is not None:
            _write_failure(
                audit_dir,
                step=(f"blocked:{error.kind}" if isinstance(error, CloseoutBlocked) else progress.step),
                error=error,
                formal_campaign_dir=formal_campaign_dir,
                supervisor_state_dir=supervisor_state_dir,
                timing_ledger_dir=timing_root,
                timing_failure_closure=closure,
                next_command=error.next_command if isinstance(error, CloseoutBlocked) else None,
            )
        if isinstance(error, VC0CloseoutError):
            raise
        raise VC0CloseoutError(f"{progress.step} 失败：{error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-id", required=True)
    parser.add_argument("--p0-gate-root", type=Path, required=True)
    parser.add_argument("--p0-gate-receipt", type=Path, required=True)
    parser.add_argument("--managed-tool-deploy-receipt", type=Path, required=True)
    parser.add_argument("--release-certification", type=Path, required=True)
    parser.add_argument("--supervisor-state-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=codex_upgrade_supervisor.DEFAULT_HEARTBEAT_SECONDS,
    )
    parser.add_argument(
        "--watchdog-timeout-seconds",
        type=float,
        default=codex_upgrade_supervisor.DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--ledger-interval-seconds",
        type=float,
        default=codex_upgrade_supervisor.DEFAULT_LEDGER_INTERVAL_SECONDS,
    )
    _add_continuation_arguments(parser)
    return parser


def _add_continuation_arguments(parser: argparse.ArgumentParser) -> None:
    """续作用的可选参数（E2-07）：上限后恢复的修复证据，以及当前现场待批准事项的批准。"""

    parser.add_argument("--fix-commit", help="上限后的恢复：修复所在提交（40 位）")
    parser.add_argument("--regression-receipt", type=Path, help="上限后的恢复：修复后的离线回归收据（绝对路径）")
    parser.add_argument("--reason", help="上限后的恢复：失败原因已如何消除")
    parser.add_argument("--approve-sha256", help="批准当前现场待批准事项（上限后恢复的预览或首批恢复预览）的 review_sha256")
    parser.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")
    parser.add_argument("--control-root", type=Path, help="部署收据所在控制根；缺省为 Formal 所在数据根下的 control")


def build_inspect_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读现场判定：Formal 未建／已建未派发／已派发／判定不了")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--preflight-campaign-dir", type=Path, help="从 preflight 清单取计时账本目录")
    source.add_argument("--timing-ledger-dir", type=Path, help="直接给计时账本目录")
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-id", required=True)
    parser.add_argument("--supervisor-state-dir", type=Path, required=True)
    return parser


def build_ledger_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Formal 未建、收口同根因到上限：凭修复证据恢复计时账本（不带 --approve-sha256 只预览）"
    )
    parser.add_argument("--preflight-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-id", required=True)
    parser.add_argument("--supervisor-state-dir", type=Path, required=True)
    parser.add_argument("--fix-commit", required=True)
    parser.add_argument("--regression-receipt", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--approve-sha256")
    parser.add_argument("--approved-by")
    parser.add_argument("--control-root", type=Path)
    return parser


def build_accounting_repair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只追加修复 VC-0 closeout 失败账本漏记的 live 请求量"
    )
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--timing-ledger-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    return parser


def build_failure_closure_repair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只追加补齐 VC-0 closeout 失败后未写入的时间阶段终态"
    )
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--timing-ledger-dir", type=Path, required=True)
    parser.add_argument("--source-audit-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "repair-failure-closure":
        arguments = build_failure_closure_repair_parser().parse_args(argv[1:])
        try:
            receipt = repair_failed_closeout_closure(
                formal_campaign_dir=arguments.formal_campaign_dir,
                timing_ledger_dir=arguments.timing_ledger_dir,
                source_audit_dir=arguments.source_audit_dir,
                audit_dir=arguments.audit_dir,
            )
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Codex closeout 失败闭合修复失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if argv and argv[0] == "repair-failure-accounting":
        arguments = build_accounting_repair_parser().parse_args(argv[1:])
        try:
            receipt = repair_failure_live_request_accounting(
                formal_campaign_dir=arguments.formal_campaign_dir,
                timing_ledger_dir=arguments.timing_ledger_dir,
                audit_dir=arguments.audit_dir,
            )
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Codex live 请求账本修复失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    if argv and argv[0] == "inspect":
        arguments = build_inspect_parser().parse_args(argv[1:])
        try:
            site = inspect_site(
                timing_root=(
                    _private_directory(arguments.timing_ledger_dir, "UpgradeTimingLedger")
                    if arguments.timing_ledger_dir is not None
                    else _ledger_dir_from_preflight(arguments.preflight_campaign_dir)
                ),
                formal_campaign_id=_safe_id(arguments.formal_campaign_id, "formal_campaign_id"),
                formal_campaign_dir=arguments.formal_campaign_dir,
                supervisor_state_dir=arguments.supervisor_state_dir,
            )
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Codex VC-0 收口现场判定失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(_public_site(site), ensure_ascii=False, sort_keys=True))
        return 0
    if argv and argv[0] == "ledger-resume":
        arguments = build_ledger_resume_parser().parse_args(argv[1:])
        try:
            result = ledger_resume(
                preflight_campaign_dir=arguments.preflight_campaign_dir,
                formal_campaign_id=arguments.formal_campaign_id,
                formal_campaign_dir=arguments.formal_campaign_dir,
                supervisor_state_dir=arguments.supervisor_state_dir,
                fix_commit=arguments.fix_commit,
                regression_receipt=arguments.regression_receipt,
                reason=arguments.reason,
                approve_sha256=arguments.approve_sha256,
                approved_by=arguments.approved_by,
                control_root=arguments.control_root,
            )
        except (OSError, ValueError, VC0CloseoutError) as error:
            print(f"Codex VC-0 上限后恢复失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return EXIT_NEEDS_OPERATOR if result.get("status") == "approval_required" else 0
    arguments = build_parser().parse_args(argv)
    try:
        receipt = closeout(arguments)
    except CloseoutBlocked as blocked:
        # 需要人工批准或处置：不是失败。标准输出给一行 JSON（入口编排器据此打印下一条命令）。
        print(f"Codex VC-0 收口续作需要处理：{blocked}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "kind": blocked.kind,
                    "message": str(blocked),
                    "next_command": blocked.next_command,
                    "details": blocked.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_NEEDS_OPERATOR
    except (OSError, ValueError, VC0CloseoutError) as error:
        print(f"Codex VC-0 收口失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
