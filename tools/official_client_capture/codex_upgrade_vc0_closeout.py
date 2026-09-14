#!/usr/bin/env python3
"""原子收口 Codex VC-0，并立即派发首个 VC-1 批次。

本工具是 Formal Campaign 的唯一创建入口。它从已冻结的 preflight Campaign
恢复全部 ``plan`` 参数，重放五类 P0 收据，在同一个 Python 进程内完成计时
事件、Formal Campaign 创建和 ``campaign-run`` 派发。任一步失败都会留下
不可覆盖诊断；工具不会删除半成品、延长原始 deadline 或自行重试。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt


CLOSEOUT_RECEIPT_SCHEMA = "codex-upgrade-vc0-closeout-receipt/v1"
CLOSEOUT_DIAGNOSTIC_SCHEMA = "codex-upgrade-vc0-closeout-diagnostic/v1"
LIVE_REQUEST_AUDIT_SCHEMA = "codex-upgrade-live-request-audit/v1"
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
INPUT_ROLES = (
    "arm64_environment",
    "campaign_run_rehearsal",
    "job_rehearsal",
    "managed_tool_deploy",
    "p0_gate",
)
PRE_REQUEST_FAILURE_MARKERS = (
    b"Read-only file system",
    "宿主与容器 runs 根不同源".encode("utf-8"),
    b"CAPTURE_HOST_DATA_ROOT",
)


class VC0CloseoutError(RuntimeError):
    """VC-0 收口输入、写入或首批派发未闭合。"""


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


def _read_stable_file(path: Path, label: str, *, maximum: int) -> bytes:
    """通过 O_NOFOLLOW 描述符读取一次有大小上限的稳定普通文件。"""

    source = _trusted_file(path, label, maximum=maximum)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise VC0CloseoutError(f"{label}无法安全打开") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 1 <= before.st_size <= maximum
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


def _trusted_file(path: Path, label: str, *, maximum: int = MAX_JSON_BYTES) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise VC0CloseoutError(f"{label}必须是可信绝对普通文件")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not 1 <= metadata.st_size <= maximum
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
        or summary.get("total_live_request_count") != 0
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
    _assert_active_vc0(
        summary,
        manifest,
        upgrade_id=str(timing["upgrade_id"]),
        evidence_decision=str(timing["evidence_decision"]),
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
    job_source: ReceiptSource,
    preflight_dir: Path,
) -> tuple[Path, Path, ReceiptSource, ReceiptSource]:
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
        or assertions.get("job_rehearsal_sha256") != job_source.sha256
    ):
        raise VC0CloseoutError("P0 gate 未绑定当前升级或 Job rehearsal")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, list):
        raise VC0CloseoutError("P0 gate 缺少 evidence")
    by_role = {
        str(item.get("role")): item
        for item in evidence
        if isinstance(item, Mapping)
    }
    if set(by_role) != {
        "campaign_run_rehearsal",
        "check_egress_spec",
        "job_rehearsal",
        "rollback",
        "test_capture_tools",
    }:
        raise VC0CloseoutError("P0 gate evidence 角色不闭合")
    if by_role["job_rehearsal"].get("sha256") != job_source.sha256:
        raise VC0CloseoutError("P0 gate evidence 未绑定 Job rehearsal 字节")
    campaign_binding = by_role["campaign_run_rehearsal"]
    campaign_path = _inside(
        evidence_root,
        str(campaign_binding.get("path", "")),
        "campaign-run rehearsal 收据",
    )
    if (
        _sha256_file(campaign_path)
        != _sha256(campaign_binding.get("sha256"), "campaign-run rehearsal sha256")
        or campaign_path.stat().st_size != campaign_binding.get("bytes")
    ):
        raise VC0CloseoutError("campaign-run rehearsal P0 绑定漂移")
    _validate_campaign_run_rehearsal(campaign_path, preflight_dir, manifest)
    return (
        evidence_root,
        path,
        _binding_source("p0_gate", path),
        _binding_source("campaign_run_rehearsal", campaign_path),
    )


def _validate_managed_tool_deploy(
    path: Path,
    manifest: Mapping[str, Any],
) -> ReceiptSource:
    receipt_path = _trusted_file(path, "受管工具部署收据")
    payload, raw = _load_json(receipt_path, "受管工具部署收据")
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
        or receipt.get("tool_files_sha256") != current_identity.get("files_sha256")
        or receipt.get("tool_files_sha256")
        != manifest_identity.get("files_sha256")
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
    return _binding_source("managed_tool_deploy", receipt_path)


def validate_inputs(
    *,
    preflight_campaign_dir: Path,
    job_rehearsal_root: Path,
    job_rehearsal_receipt: Path,
    p0_gate_root: Path,
    p0_gate_receipt: Path,
    managed_tool_deploy_receipt: Path,
    now: str | None = None,
) -> ValidatedInputs:
    """重放五份输入并返回创建 Formal 所需的规范坐标。"""

    observed = now or _utc_now()
    preflight_dir, manifest = _load_preflight(preflight_campaign_dir)
    (
        timing_root,
        arm_root,
        arm_path,
        timing_summary,
        arm_source,
    ) = _validate_timing_and_arm64(preflight_dir, manifest, now=observed)
    (
        job_root,
        job_path,
        _job_receipt,
        job_source,
    ) = _validate_job_rehearsal(
        preflight_dir,
        manifest,
        job_rehearsal_root,
        job_rehearsal_receipt,
    )
    p0_root, p0_path, p0_source, campaign_source = _validate_p0_gate(
        p0_gate_root,
        p0_gate_receipt,
        manifest,
        timing_summary,
        job_source,
        preflight_dir,
    )
    deploy_source = _validate_managed_tool_deploy(
        managed_tool_deploy_receipt,
        manifest,
    )
    receipts = tuple(
        sorted(
            (
                arm_source,
                campaign_source,
                job_source,
                deploy_source,
                p0_source,
            ),
            key=lambda item: item.role,
        )
    )
    if tuple(item.role for item in receipts) != INPUT_ROLES:
        raise VC0CloseoutError("VC-0 五份输入角色不闭合")
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
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        path = _trusted_file(
            path,
            "relay 客户端原始字节",
            maximum=512 * 1024 * 1024,
        )
        count = len(
            model_condition_receipts._responses_request_models(path.read_bytes())
        )
        if count:
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
                result.append(_trusted_file(path, "Formal Job 日志"))
    return sorted(result)


def _logs_prove_pre_request_failure(paths: list[Path]) -> bool:
    """仅接受 wrapper 在创建运行根之前写出的固定失败标记。"""

    if not paths:
        return False
    for path in paths:
        raw = _read_stable_file(path, "Formal Job 日志", maximum=MAX_JSON_BYTES)
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
    root_cause_id = f"vc0-closeout-{digest}"
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
        "next_action": (
            "保留 Formal Campaign 和 supervisor 审计现场，按最近合法 checkpoint "
            "恢复 pending/failed 项，不得重新运行本收口"
            if formal_exists
            else "修复失败的控制面输入，沿用原时间账本与 deadline，并使用新的不可变输出坐标重做 VC-0 收口"
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


def closeout(arguments: argparse.Namespace) -> dict[str, Any]:
    """执行一次不可重入的 VC-0 收口与 VC-1 首批派发。"""

    os.umask(0o077)
    formal_campaign_dir = Path(arguments.formal_campaign_dir)
    supervisor_state_dir = Path(arguments.supervisor_state_dir)
    audit_dir: Path | None = None
    formal_campaign_id: str | None = None
    step = "initialize-audit"
    timing_root: Path | None = None
    try:
        audit_dir = _new_private_directory(
            Path(arguments.audit_dir),
            "closeout audit-dir",
        )
        step = "validate-request"
        formal_campaign_id = _safe_id(
            arguments.formal_campaign_id,
            "formal_campaign_id",
        )
        for field in (
            "heartbeat_seconds",
            "watchdog_timeout_seconds",
            "ledger_interval_seconds",
        ):
            codex_upgrade_supervisor._positive_seconds(
                getattr(arguments, field),
                field,
            )
        request = {
            "preflight_campaign_dir": str(arguments.preflight_campaign_dir),
            "formal_campaign_dir": str(formal_campaign_dir),
            "formal_campaign_id": formal_campaign_id,
            "job_rehearsal_root": str(arguments.job_rehearsal_root),
            "job_rehearsal_receipt": str(arguments.job_rehearsal_receipt),
            "p0_gate_root": str(arguments.p0_gate_root),
            "p0_gate_receipt": str(arguments.p0_gate_receipt),
            "managed_tool_deploy_receipt": str(
                arguments.managed_tool_deploy_receipt
            ),
            "supervisor_state_dir": str(supervisor_state_dir),
            "requested_at_utc": _utc_now(),
        }
        _write_once(audit_dir / "request.json", request)
        step = "validate-inputs"
        validated = validate_inputs(
            preflight_campaign_dir=Path(arguments.preflight_campaign_dir),
            job_rehearsal_root=Path(arguments.job_rehearsal_root),
            job_rehearsal_receipt=Path(arguments.job_rehearsal_receipt),
            p0_gate_root=Path(arguments.p0_gate_root),
            p0_gate_receipt=Path(arguments.p0_gate_receipt),
            managed_tool_deploy_receipt=Path(
                arguments.managed_tool_deploy_receipt
            ),
        )
        timing_root = _private_directory(
            validated.timing_ledger_dir,
            "UpgradeTimingLedger",
        )
        with _ledger_lock(timing_root):
            step = "recheck-active-vc0"
            recheck_at = _utc_now()
            try:
                current_timing = codex_upgrade_timing_ledger.inspect_ledger(
                    timing_root,
                    now=recheck_at,
                )
            except (
                OSError,
                ValueError,
                codex_upgrade_timing_ledger.TimingLedgerError,
            ) as error:
                raise VC0CloseoutError(
                    f"取得账本锁后的状态重放失败：{error}"
                ) from error
            _assert_active_vc0(
                current_timing,
                validated.preflight_manifest,
                upgrade_id=str(validated.timing_summary["upgrade_id"]),
                evidence_decision=str(
                    validated.timing_summary["evidence_decision"]
                ),
                now=recheck_at,
            )
            if (
                current_timing.get("head_sequence")
                != validated.timing_summary.get("head_sequence")
                or current_timing.get("head_sha256")
                != validated.timing_summary.get("head_sha256")
            ):
                raise VC0CloseoutError(
                    "取得账本锁前 UpgradeTimingLedger 已发生并发漂移"
                )
            step = "precheck-outputs"
            receipt_root = _precheck_outputs(
                validated,
                formal_campaign_dir=formal_campaign_dir,
                formal_campaign_id=formal_campaign_id,
                supervisor_state_dir=supervisor_state_dir,
            )
            step = "copy-p0-receipts"
            input_bindings = _copy_inputs_to_ledger(validated, receipt_root)
            step = "append-receipt-passed"
            codex_upgrade_timing_ledger.append_event(
                timing_root,
                event_id=f"{formal_campaign_id}-p0-receipts-passed",
                phase="VC-0",
                event_type="receipt_passed",
                receipts=input_bindings,
                live_request_count=0,
                next_action="创建 Formal Campaign 并立即启动 VC-1 首批",
            )
            step = "create-active-timing-checkpoint"
            timing_relative = (
                receipt_root / "timing-vc0-active.json"
            ).relative_to(timing_root).as_posix()
            timing_checkpoint = codex_upgrade_timing_ledger.checkpoint(
                timing_root,
                timing_relative,
            )
            if (
                timing_checkpoint.get("summary", {}).get("status") != "active"
                or timing_checkpoint.get("summary", {}).get("active_phase")
                != "VC-0"
            ):
                raise VC0CloseoutError("Formal plan 前的 timing checkpoint 非 active VC-0")

            step = "recover-formal-plan"
            plan_arguments = recover_formal_plan_arguments(
                validated,
                formal_campaign_dir=formal_campaign_dir,
                formal_campaign_id=formal_campaign_id,
                timing_receipt=timing_root / timing_relative,
            )
            step = "create-formal-campaign"
            formal_manifest = codex_upgrade.create_campaign(plan_arguments)
            step = "replay-formal-campaign"
            formal_manifest = codex_upgrade.load_campaign_manifest(
                formal_campaign_dir
            )
            if (
                formal_manifest.get("campaign_id") != formal_campaign_id
                or formal_manifest.get("campaign_mode") != "formal"
            ):
                raise VC0CloseoutError("Formal Campaign 创建结果身份漂移")
            step = "copy-formal-control-artifacts"
            formal_artifacts = _copy_formal_control_artifacts(
                validated,
                formal_campaign_dir,
                formal_manifest,
                receipt_root,
            )
            step = "complete-vc0"
            codex_upgrade_timing_ledger.append_event(
                timing_root,
                event_id=f"{formal_campaign_id}-vc0-completed",
                phase="VC-0",
                event_type="stage_completed",
                live_request_count=0,
                next_action="立即启动 VC-1 首批",
            )
            step = "start-vc1"
            codex_upgrade_timing_ledger.append_event(
                timing_root,
                event_id=f"{formal_campaign_id}-vc1-started",
                phase="VC-1",
                event_type="stage_started",
                live_request_count=0,
                next_action="由 campaign-run 执行首个 VC-1 冻结批次",
            )
            control = formal_manifest.get("vc_control")
            run_reference = (
                control.get("first_campaign_run_manifest")
                if isinstance(control, Mapping)
                else None
            )
            if not isinstance(run_reference, Mapping):
                raise VC0CloseoutError("Formal Campaign 缺少首个 campaign-run 清单")
            run_manifest_path = _inside(
                formal_campaign_dir,
                str(run_reference.get("path", "")),
                "首个 VC-1 campaign-run 清单",
            )
            if _sha256_file(run_manifest_path) != run_reference.get("sha256"):
                raise VC0CloseoutError("首个 VC-1 campaign-run 清单摘要漂移")
            step = "dispatch-vc1"
            returncode, run_result = codex_upgrade_supervisor._campaign_run_command(
                argparse.Namespace(
                    state_dir=supervisor_state_dir,
                    manifest=run_manifest_path,
                    heartbeat_seconds=float(arguments.heartbeat_seconds),
                    watchdog_timeout_seconds=float(
                        arguments.watchdog_timeout_seconds
                    ),
                    ledger_interval_seconds=float(arguments.ledger_interval_seconds),
                )
            )
            if (
                returncode != 0
                or run_result.get("status") != "stopped"
                or run_result.get("reason") != "queue-complete"
            ):
                raise VC0CloseoutError(
                    "首个 VC-1 campaign-run 未形成 queue-complete 终态"
                )

            step = "write-closeout-receipt"
            final_timing = codex_upgrade_timing_ledger.inspect_ledger(timing_root)
            if final_timing.get("status") != "active":
                raise VC0CloseoutError("VC-1 首批结束时原始时间预算已要求停线")
            receipt = {
                "schema_version": CLOSEOUT_RECEIPT_SCHEMA,
                "status": "passed",
                "completed_at_utc": _utc_now(),
                "preflight_campaign": {
                    "path": str(validated.preflight_dir),
                    "campaign_id": validated.preflight_manifest["campaign_id"],
                    "sha256": _sha256_file(
                        validated.preflight_dir / "campaign.json"
                    ),
                },
                "formal_campaign": {
                    "path": str(formal_campaign_dir),
                    "campaign_id": formal_campaign_id,
                    "sha256": _sha256_file(formal_campaign_dir / "campaign.json"),
                },
                "input_receipts": [
                    {
                        "role": item.role,
                        "source_path": str(item.path),
                        "sha256": item.sha256,
                        "bytes": item.bytes,
                    }
                    for item in validated.receipts
                ],
                "formal_control_artifacts": formal_artifacts,
                "timing_checkpoint": {
                    "path": timing_relative,
                    "sha256": _sha256_file(timing_root / timing_relative),
                },
                "timing_head_sequence": final_timing["head_sequence"],
                "timing_head_sha256": final_timing["head_sha256"],
                "vc1_campaign_run": run_result,
                "deadline_extended": False,
                "create_campaign_call_count": 1,
                "campaign_run_call_count": 1,
            }
            _write_once(audit_dir / "receipt.json", receipt)
            return receipt
    except BaseException as error:
        timing_failure_closure: dict[str, Any] | None = None
        if timing_root is not None and formal_campaign_id is not None:
            try:
                with _ledger_lock(timing_root):
                    timing_failure_closure = _close_failed_timing_stage(
                        timing_root,
                        formal_campaign_dir=formal_campaign_dir,
                        formal_campaign_id=formal_campaign_id,
                        failed_step=step,
                    )
            except BaseException as closure_error:
                timing_failure_closure = {
                    "status": "closure-failed",
                    "error_type": type(closure_error).__name__,
                    "message": str(closure_error),
                }
        if audit_dir is not None:
            _write_failure(
                audit_dir,
                step=step,
                error=error,
                formal_campaign_dir=formal_campaign_dir,
                supervisor_state_dir=supervisor_state_dir,
                timing_ledger_dir=timing_root,
                timing_failure_closure=timing_failure_closure,
            )
        if isinstance(error, VC0CloseoutError):
            raise
        raise VC0CloseoutError(f"{step} 失败：{error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--formal-campaign-id", required=True)
    parser.add_argument("--job-rehearsal-root", type=Path, required=True)
    parser.add_argument("--job-rehearsal-receipt", type=Path, required=True)
    parser.add_argument("--p0-gate-root", type=Path, required=True)
    parser.add_argument("--p0-gate-receipt", type=Path, required=True)
    parser.add_argument("--managed-tool-deploy-receipt", type=Path, required=True)
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
    return parser


def build_accounting_repair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只追加修复 VC-0 closeout 失败账本漏记的 live 请求量"
    )
    parser.add_argument("--formal-campaign-dir", type=Path, required=True)
    parser.add_argument("--timing-ledger-dir", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
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
    arguments = build_parser().parse_args(argv)
    try:
        receipt = closeout(arguments)
    except (OSError, ValueError, VC0CloseoutError) as error:
        print(f"Codex VC-0 原子收口失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
