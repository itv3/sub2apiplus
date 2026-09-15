#!/usr/bin/env python3
"""Codex 升级的独立监督器与分钟级审计账本。

监督器必须是升级编排器的独立进程。编排器被 ``SIGKILL``、崩溃或机器会话
中止时，监督器仍可依据最后一次心跳写入 ``audit-incomplete`` 和
``watchdog-aborted``，从而把不可核对区间明确标出来，而不是把它误认为空闲。

本模块只记录脱敏的操作标签、Job 身份和时间元数据，不记录命令参数、环境变量、
凭据或原始网络响应。所有记录均实时 ``fsync``；文件只允许当前用户读写。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

if __package__ in {None, ""}:
    import codex_upgrade_evidence_permissions as evidence_permissions
    import codex_upgrade_root_cause as root_cause
    import codex_upgrade_timing_ledger as timing_ledger
    import codex_upgrade_vc1_permission_alias_closeout as permission_alias_closeout
    import codex_upgrade_vc_artifacts as vc_artifacts
else:
    from . import codex_upgrade_evidence_permissions as evidence_permissions
    from . import codex_upgrade_root_cause as root_cause
    from . import codex_upgrade_timing_ledger as timing_ledger
    from . import codex_upgrade_vc1_permission_alias_closeout as permission_alias_closeout
    from . import codex_upgrade_vc_artifacts as vc_artifacts


SCHEMA_VERSION = "codex-upgrade-supervisor/v1"
EVENT_SCHEMA = "codex-upgrade-supervisor-event/v1"
HEARTBEAT_SCHEMA = "codex-upgrade-supervisor-heartbeat/v1"
WATCHDOG_HEARTBEAT_SCHEMA = "codex-upgrade-supervisor-watchdog-heartbeat/v1"
MINUTE_SCHEMA = "codex-upgrade-supervisor-minute/v1"
STOP_SCHEMA = "codex-upgrade-supervisor-stop/v1"
STATE_SCHEMA = "codex-upgrade-supervisor-state/v1"
CAMPAIGN_ACTIVITY_SCHEMA = "codex-upgrade-campaign-activity/v1"
CAMPAIGN_FINISH_SCHEMA = "codex-upgrade-campaign-finish/v1"
CAMPAIGN_CONTINUITY_SCHEMA = "codex-upgrade-campaign-continuity/v1"
CAMPAIGN_RUN_SCHEMA = "codex-upgrade-campaign-run/v1"
CAMPAIGN_RUN_BATCHED_SCHEMA = "codex-upgrade-campaign-run/v2"
CAMPAIGN_RUN_RECOVERY_SCHEMA = "codex-upgrade-campaign-run/v3"
CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA = "codex-upgrade-campaign-run/v4"
CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA = "codex-upgrade-campaign-run/v5"
# 2026-09-14：0.154 首轮正式 VC-1 的 sequence 2 已完成 assertion bundle，
# 但 seal 在读取证据正文前因历史目录权限为 0755/0644 而失败。sequence 3 已在
# 失败后、工具修复前不可覆盖地编译，因此不能追加恢复字段。以下身份只为这一条
# 已冻结 v2→v2 权限补偿边提供 fail-close 锚点，不得复用为普通失败重试机制。
VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID = (
    "c0154-formal-vc1-bwg-new-window-20260914t100818z"
)
VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256 = (
    "ac13ad0e159050a872a42b26b7f7b8966e85488a9a86ab7af0b93219441f797c"
)
VC1_PERMISSION_COMPENSATION_FAILED_BATCH_SHA256 = (
    "8dc9756311f232a330216c0562f05ec7301eb9370106e8e63e6d6d3a2f53d92b"
)
VC1_PERMISSION_COMPENSATION_SUCCESSOR_BATCH_SHA256 = (
    "6a3aa65e451a76d0608125e594f3d2164c76e8212fabd051a379c3c975eb64df"
)
VC1_PERMISSION_COMPENSATION_ATTEMPT_ID = (
    "20260914T102852Z-04996800fbbe4e94"
)
VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256 = (
    "2fe5677bdc05dda3b8089ca07fa176aa76f07499867f2ca64785dc73769dd0c7"
)
VC1_PERMISSION_COMPENSATION_ROOTS_SHA256 = (
    "ae45b6f54c7d2333a5cdf80c5a42aa8510bc2f0083df381c2a3ed53b1116fe2d"
)
VC1_PERMISSION_COMPENSATION_HELPER_SHA256 = (
    "0d2f432beb6684d7bb9a427134b93b28b99179c349ef1f447b7b7a8339b72d0a"
)
VC1_PERMISSION_COMPENSATION_ACTION_PLAN_SHA256 = (
    "6fd7f67d5f003520ccab7ceea23a78e6f91a6301a253a750455865860a779ec1"
)
VC1_PERMISSION_COMPENSATION_DIAGNOSTIC_SHA256 = (
    "7fc4cf19ad4fb586d83242e9ae89baea96880545d80b5a559822cfdb12b1ceb6"
)
# sequence 3 的首次权限动作经只读 bind alias 执行 fchmod，已在证据正文读取和
# seal 启动前失败。以下锚点只允许同一 Campaign 的 sequence 4 使用新 helper
# 经宿主可写 alias 收口；未来批次或其他失败不能借此退化为普通 v2 重试。
VC1_PERMISSION_ALIAS_FAILURE_DIAGNOSTIC_SHA256 = (
    "dfe840655b4772c3bb4c3a0f5d706687bcc9ba744af749424be15f057f06b325"
)
VC1_PERMISSION_ALIAS_FAILED_RUN_NAME = (
    "run-d932716c0dd0fbb60789232cbffad83910a271f58262b67679db75629341769f"
)
VC1_PERMISSION_ALIAS_STATE_FILE_SHA256 = (
    "00091772350c6bdafc383b8f6c06156def25f4dfa01c7d1cae4c0e137c3e5f86"
)
VC1_PERMISSION_ALIAS_MANIFEST_FILE_SHA256 = (
    "41e1f332139f32188d815b003541fb9bc1d207589131603f48f2288fe7dec727"
)
VC1_PERMISSION_ALIAS_STOP_FILE_SHA256 = (
    "d2f1a4d68d2859d8a8f571d1a20b2eb7cf7935f8ea0780a5d3aea722fae101dc"
)
VC1_PERMISSION_ALIAS_EVENTS_FILE_SHA256 = (
    "b5a22aa0a361ce457d89d4429400221e1714445e282bcc868abfa12d85763f84"
)
VC1_PERMISSION_ALIAS_DIAGNOSTIC_FILE_SHA256 = (
    "757051899fef3f73fbc228416b9e4b8dde6813a2d936d16393f56cda2d4b04d5"
)
VC1_PERMISSION_ALIAS_HELPER_SHA256 = (
    "408e9d733b8997e569fbea36ab648f1bd70e45f9669accf3cc9353eb8b2566b1"
)
VC1_PERMISSION_ALIAS_ENTRY_COUNT = 1882
VC1_PERMISSION_ALIAS_GAP_COUNT = 15
VC1_PERMISSION_ALIAS_GAP_SHA256 = (
    "2b69ee039891bf6c58b6c787af105b9a896956b562cd0801c10ca7d2b36f2842"
)
VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT = Path("/root/oauth-capture/runs")
# sequence 4 已在 60 秒窗口内编译，但旧 helper 遗漏真实 OAuth 嵌套根，
# 因而在父 run 创建前被只读前检拒绝。以下摘要只允许该未派发批次经零请求
# 封口收据承接到唯一 sequence 5；不得覆盖或重编 sequence 4。
VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_FILE_SHA256 = (
    "02445883a35a1298821a76cfb8eed841b596de2169c22fc34d6d660981ed2cc8"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_SHA256 = (
    "b2455bb609270d25ac257d08405c6eebfd6cfa2d3d207f946e227929030f72fb"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_MANIFEST_FILE_SHA256 = (
    "6d574a8c785f5ccd0a26033ff43631b62789b27db289590053c210aaedf55e23"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_ACTION_PLAN_SHA256 = (
    "0daf24b9e369b59735520cfa4ed51d874baa083b1fa396089c36fe8e0b5d8cf1"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_DEPLOYMENT_SHA256 = (
    "7d81a306ec1d71e0eb795ed45d1b2ad71449e60d39f5f2f969e921c0968ca5a6"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_TOOL_FILES_SHA256 = (
    "3bfcc618a6fb94a8c145e747a044803e6b0f148ef5f7004e15f990a91d7d6da9"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_SUPERVISOR_SHA256 = (
    "625a6d86ea28e4025b051ded84a829e8bfd5787db7b5450a41ef2460317d71b9"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_COMPILED_AT_UTC = (
    "2026-09-14T13:43:34+00:00"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_MUST_START_BY_UTC = (
    "2026-09-14T13:44:34+00:00"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_ERROR = (
    "外部证据根不属于冻结 Campaign："
    "/root/oauth-capture/runs/official-client/oauth/"
    "oauth-c0154-formal-vc1-bwg-new-window-20260914t100818z"
)
VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_PATH = Path(
    "/root/docker/capture-cli/data/control/"
    "codex-0154-supervisor-enable-20260914t145040z.json"
)
VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_SHA256 = (
    "1f3c903139c9035a2e9f5a3fe176801811478d812449df8a5b0d076192b5f395"
)
VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_ROLLBACK_PATH = Path(
    "/root/docker/capture-cli/data/control/"
    "managed-tools-backup-before-3ce4cb1b6b17-20260914t145040z-ae7426f3"
)
VC1_PERMISSION_ALIAS_V2_HELPER_SHA256 = (
    "67957d6ac413b5c87fcfb24bb003e49f13e27212e90fa564cd7652b5ab411a4c"
)
VC1_PERMISSION_ALIAS_PREDISPATCH_CLOSEOUT_TOOL_SHA256 = (
    "cc7d2409938ca1215c3388a9a9194e32491c8ff9f88410d32b50e7289a120b00"
)
CAMPAIGN_RUN_LOCK_FILENAME = ".campaign-run.lock"
# campaign-run 启动的子命令通过这些只读环境变量复用同一个父监督器。
# 子进程不得自行创建第二个监督器；身份仍以父 run_dir/state.json 为准。
CAMPAIGN_RUN_CONTEXT_ENV = "CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE"
CAMPAIGN_RUN_DIR_ENV = "CODEX_UPGRADE_CAMPAIGN_RUN_DIR"
CAMPAIGN_RUN_ID_ENV = "CODEX_UPGRADE_CAMPAIGN_ID"
CAMPAIGN_RUN_PHASE_ENV = "CODEX_UPGRADE_CAMPAIGN_PHASE"
CAMPAIGN_RUN_OWNER_PID_ENV = "CODEX_UPGRADE_CAMPAIGN_OWNER_PID"
CAMPAIGN_RUN_OWNER_NONCE_ENV = "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE"
CAMPAIGN_RUN_DEADLINE_ENV = "CODEX_UPGRADE_CAMPAIGN_DEADLINE_AT_EPOCH"
CAMPAIGN_RUN_ACTION_ID_ENV = "CODEX_UPGRADE_CAMPAIGN_ACTION_ID"
CAMPAIGN_RUN_ACTION_DIAGNOSTIC_ENV = (
    "CODEX_UPGRADE_CAMPAIGN_ACTION_DIAGNOSTIC"
)
CAMPAIGN_RUN_CLEANUP_SIGNAL_ENV = "CODEX_UPGRADE_CAMPAIGN_CLEANUP_SIGNAL"
CAMPAIGN_RUN_EXECUTION_DEADLINE_ENV = (
    "CODEX_UPGRADE_CAMPAIGN_EXECUTION_DEADLINE_AT_EPOCH"
)
CAMPAIGN_RUN_CLEANUP_GRACE_ENV = "CODEX_UPGRADE_CAMPAIGN_CLEANUP_GRACE_SECONDS"
ACTION_DIAGNOSTIC_SCHEMA = "codex-upgrade-campaign-action-diagnostic/v1"
ACTION_DIAGNOSTIC_FAILURE_KINDS = frozenset(
    {"handled-error", "interrupted", "unexpected-error", "child-returncode"}
)
ACTION_DIAGNOSTIC_FIELDS = frozenset(
    {
        "schema_version",
        "campaign_id",
        "phase",
        "action_id",
        "owner_pid",
        "owner_nonce",
        "failure_kind",
        "error_type",
        "message",
        "recorded_at_utc",
        "diagnostic_sha256",
    }
)
DEFAULT_HEARTBEAT_SECONDS = 5
DEFAULT_WATCHDOG_TIMEOUT_SECONDS = 20
DEFAULT_LEDGER_INTERVAL_SECONDS = 60
# 动作完成后，编排器必须在这个窗口内派发下一动作或请求终态。
# 该计时器独立于 worker watchdog，防止“没有命令但持续心跳”的空转。
DEFAULT_ORCHESTRATOR_DISPATCH_TIMEOUT_SECONDS = 15
DEFAULT_POLL_SECONDS = 0.2
DEFAULT_STOP_WAIT_SECONDS = 2
# v2 及后续批次必须在原始总截止前主动结束数据面动作，为 attempt after、
# restoration、ARM64 after、timeout checkpoint 和父监督器终态留下固定窗口。
# 这不是延长 deadline：数据面可用时间会相应缩短，原始绝对截止保持不变。
DEFAULT_BATCHED_ACTION_CLEANUP_GRACE_SECONDS = 120
DEFAULT_TERMINAL_DRAIN_SECONDS = 5
MAX_OPERATION_LENGTH = 256
MAX_JOB_ID_LENGTH = 128
MAX_NOTE_LENGTH = 512
SAFE_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
SENSITIVE_MARKERS = (
    "authorization",
    "api-key",
    "apikey",
    "cookie",
    "token",
    "secret",
    "password",
    "bearer",
    "credential",
)
# 失败详情只能保留经过短文本规则校验的控制面说明。出现这些来源标签时，
# 整段内容改写为固定文案，避免 argv、环境或原始输出进入持久审计文件。
ACTION_DIAGNOSTIC_REDACTION_MARKERS = SENSITIVE_MARKERS + (
    "argv",
    "environment",
    "stdout",
    "stderr",
    "raw output",
    "command line",
)
TERMINAL_STATES = frozenset(
    {"stopped", "failed", "audit-incomplete", "watchdog-aborted"}
)
STOP_STATUSES = frozenset(TERMINAL_STATES)
CAMPAIGN_CLASSIFICATIONS = frozenset(
    {"active", "planning", "orchestrator-idle", "waiting"}
)
# VC-6 是唯一需要触碰生产选择器的正式阶段。为避免把历史兼容编排器
# 误当成正式流程，以下入口在 VC-6 阶段即使没有父上下文也必须拒绝。
FORMAL_VC6_PHASE = "VC-6"
LEGACY_SUPERVISOR_WRITE_COMMANDS = frozenset(
    {
        "campaign-start",
        "campaign-mark",
        "campaign-exec",
        "campaign-stop",
        "campaign-owner",
        "run",
    }
)
CAMPAIGN_ACTION_FIELDS = frozenset(
    {
        "action_id",
        "worker_pid",
        "worker_started_at_epoch",
        "worker_started_monotonic_ns",
        "worker_heartbeat_at_epoch",
        "worker_heartbeat_monotonic_ns",
        "command_pid",
        "action_status",
        "returncode",
        "reason",
        "ended_at_epoch",
        "ended_at_utc",
    }
)
CAMPAIGN_ACTION_OPTIONAL_FIELDS = frozenset({"accepted_returncodes"})


class SupervisorError(RuntimeError):
    """监督器状态、账本或外部命令不可信。"""


class SupervisorTimeout(SupervisorError):
    """统一命令入口达到单步或全局墙钟预算。"""


def _canonical(value: Mapping[str, Any]) -> bytes:
    """委托 VC 控制制品模块生成跨工具统一的 canonical JSON。"""

    return vc_artifacts.canonical_bytes(value)


def _legacy_compact_canonical(value: Mapping[str, Any]) -> bytes:
    """重放唯一历史 compact 收据；新制品不得使用此编码。"""

    canonical = vc_artifacts.canonical_bytes(value)
    if not canonical.endswith(b"\n"):
        raise SupervisorError("共享 canonical JSON 合同缺少唯一末尾换行。")
    return canonical[:-1]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _epoch_to_utc(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _parse_epoch(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupervisorError(f"{label} 必须是数字。")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SupervisorError(f"{label} 非法。")
    return number


def _parse_monotonic_ns(value: Any, label: str) -> int:
    """校验跨进程共享的 CLOCK_MONOTONIC 纳秒值。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SupervisorError(f"{label} 必须是非负整数。")
    return value


def _reject_symlink_components(path: Path) -> None:
    """在任何 resolve/open 前拒绝路径上的符号链接组件。"""

    if not path.is_absolute():
        raise SupervisorError("监督器路径必须是绝对路径。")
    current = Path(path.anchor)
    for part in path.parts:
        if part in {path.anchor, ""}:
            continue
        current /= part
        if current.is_symlink():
            raise SupervisorError(f"监督器路径不得包含符号链接：{current}")


def _safe_id(value: Any, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SupervisorError(f"{label} 非法。")
    if any(char not in SAFE_ID_CHARS for char in value):
        raise SupervisorError(f"{label} 含非法字符。")
    return value


def _operation(value: Any) -> str:
    if not isinstance(value, str):
        raise SupervisorError("操作标签必须是字符串。")
    normalized = " ".join(value.strip().split())
    lowered = normalized.lower()
    if (
        not normalized
        or len(normalized) > MAX_OPERATION_LENGTH
        or any(marker in lowered for marker in SENSITIVE_MARKERS)
        or any(ord(char) < 0x20 for char in normalized)
    ):
        raise SupervisorError("操作标签为空、过长或含敏感信息。")
    return normalized


def _note(value: Any, label: str = "说明") -> str:
    if not isinstance(value, str):
        raise SupervisorError(f"{label} 必须是字符串。")
    normalized = " ".join(value.strip().split())
    lowered = normalized.lower()
    if (
        not normalized
        or len(normalized) > MAX_NOTE_LENGTH
        or any(ord(char) < 0x20 for char in normalized)
        or any(marker in lowered for marker in SENSITIVE_MARKERS)
    ):
        raise SupervisorError(f"{label} 非法。")
    return normalized


def _positive_seconds(value: Any, label: str) -> float:
    """校验可用于内部测试和正式运行的有限正数秒数。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupervisorError(f"{label} 必须是数字。")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SupervisorError(f"{label} 必须是有限正数。")
    return number


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """限制事件附加字段，防止命令参数或凭据进入审计日志。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SupervisorError("事件 metadata 必须是对象。")

    def clean(item: Any, *, depth: int = 0) -> Any:
        if depth > 3:
            raise SupervisorError("事件 metadata 嵌套过深。")
        if item is None or isinstance(item, (bool, int, float, str)):
            if isinstance(item, float) and not math.isfinite(item):
                raise SupervisorError("事件 metadata 数字必须有限。")
            if isinstance(item, str):
                lowered = item.lower()
                if len(item) > MAX_NOTE_LENGTH or any(
                    marker in lowered for marker in SENSITIVE_MARKERS
                ):
                    raise SupervisorError("事件 metadata 含敏感或过长文本。")
                if any(ord(char) < 0x20 for char in item):
                    raise SupervisorError("事件 metadata 含控制字符。")
            return item
        if isinstance(item, list):
            if len(item) > 32:
                raise SupervisorError("事件 metadata 数组过长。")
            return [clean(child, depth=depth + 1) for child in item]
        if isinstance(item, Mapping):
            if len(item) > 32:
                raise SupervisorError("事件 metadata 字段过多。")
            output: dict[str, Any] = {}
            for key, child in item.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 64
                    or any(marker in key.lower() for marker in SENSITIVE_MARKERS)
                    or any(ord(char) < 0x20 for char in key)
                ):
                    raise SupervisorError("事件 metadata 键非法。")
                output[key] = clean(child, depth=depth + 1)
            return output
        raise SupervisorError("事件 metadata 含不支持的值。")

    result = clean(dict(value))
    if not isinstance(result, dict):
        raise SupervisorError("事件 metadata 必须是对象。")
    # 以规范 JSON 的字节数作为最终上限，避免恶意写入撑爆账本。
    if len(_canonical(result)) > 16 * 1024:
        raise SupervisorError("事件 metadata 过大。")
    return result


def _validate_state_dir(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise SupervisorError("监督器目录必须是非符号链接绝对路径。")
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise SupervisorError("监督器目录不是可信目录。")
        resolved = path.resolve(strict=True)
        if (resolved.stat().st_mode & 0o777) != 0o700:
            raise SupervisorError("监督器目录权限必须是 0700。")
        if resolved.stat().st_uid != os.geteuid():
            raise SupervisorError("监督器目录必须由当前用户拥有。")
        return resolved
    if not create:
        raise SupervisorError("监督器目录不存在。")
    parent = path.parent.resolve(strict=True)
    if path.parent.is_symlink() or not parent.is_dir():
        raise SupervisorError("监督器目录父级不可信。")
    path.mkdir(mode=0o700)
    return path.resolve(strict=True)


def _validate_file(path: Path, *, allow_missing: bool = False) -> None:
    if path.is_symlink():
        raise SupervisorError(f"监督器文件不得是符号链接：{path.name}")
    if not path.exists():
        if allow_missing:
            return
        raise SupervisorError(f"监督器文件不存在：{path.name}")
    metadata = path.stat()
    if not path.is_file() or metadata.st_uid != os.geteuid():
        raise SupervisorError(f"监督器文件不是当前用户普通文件：{path.name}")
    if (metadata.st_mode & 0o777) != 0o600:
        raise SupervisorError(f"监督器文件权限必须是 0600：{path.name}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    """处理 os.write 的部分写，确保一条 JSON 记录完整落盘。"""

    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise SupervisorError("监督器日志写入未前进。")
        view = view[written:]


def _write_json(path: Path, payload: Mapping[str, Any], *, replace: bool) -> None:
    """原子写入小型 JSON，并确保内容和目录项都落盘。"""

    # 不先 resolve 最终路径：否则最终文件是符号链接时会被解析成目标，绕过
    # 下面的 symlink 检查并把审计记录写到不受管的位置。
    if not path.is_absolute():
        raise SupervisorError("监督器文件路径必须是绝对路径。")
    path = Path(os.path.abspath(path))
    _reject_symlink_components(path)
    if path.parent.is_symlink():
        raise SupervisorError("监督器文件父级不得是符号链接。")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise SupervisorError(f"监督器文件路径不可信：{path.name}")
    if not replace and path.exists():
        raise SupervisorError(f"监督器文件禁止覆盖：{path.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
            temporary.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    _validate_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SupervisorError(f"监督器 JSON 无法读取：{path.name}") from error
    if not isinstance(payload, dict):
        raise SupervisorError(f"监督器 JSON 顶层必须是对象：{path.name}")
    return payload


def _action_diagnostic_message(value: Any) -> str:
    """生成有界脱敏说明，禁止把命令、环境或原始输出带入收据。"""

    fallback = "错误详情已按脱敏规则省略。"
    if not isinstance(value, str):
        return _note(fallback, "动作失败诊断")
    normalized = " ".join(value.strip().split())
    lowered = normalized.lower()
    if (
        not normalized
        or len(normalized) > MAX_NOTE_LENGTH
        or any(marker in lowered for marker in ACTION_DIAGNOSTIC_REDACTION_MARKERS)
    ):
        return _note(fallback, "动作失败诊断")
    try:
        return _note(normalized, "动作失败诊断")
    except SupervisorError:
        return _note(fallback, "动作失败诊断")


def _validate_action_diagnostic_timestamp(value: Any) -> str:
    """只接受带 ``Z`` 的 RFC3339 UTC 时间。"""

    if not isinstance(value, str) or not value.endswith("Z"):
        raise SupervisorError("动作失败诊断时间必须是 RFC3339 UTC。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SupervisorError("动作失败诊断时间非法。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SupervisorError("动作失败诊断时间缺少 UTC 时区。")
    return value


def _action_diagnostic_path(
    run_dir: Path,
    action_id: str,
    *,
    create_directory: bool,
    supplied_path: Path | None = None,
) -> Path:
    """解析动作专属诊断路径，并把它约束在父 run 目录内。"""

    run_dir = _validate_state_dir(Path(run_dir), create=False)
    action_id = _safe_id(action_id, "action_id")
    diagnostic_dir = _validate_state_dir(
        run_dir / "action-diagnostics",
        create=create_directory,
    )
    expected = diagnostic_dir / f"action-{action_id}-failure.json"
    if supplied_path is not None:
        if not supplied_path.is_absolute():
            raise SupervisorError("动作失败诊断路径必须是绝对路径。")
        supplied = Path(os.path.abspath(supplied_path))
        if supplied != expected:
            raise SupervisorError("动作失败诊断路径与 action 身份不一致。")
    return expected


def _validate_action_diagnostic(
    path: Path,
    *,
    run_dir: Path,
    campaign_id: str,
    phase: str,
    action_id: str,
    owner_pid: int,
    owner_nonce: str,
) -> dict[str, Any]:
    """校验动作失败诊断的闭合字段、父身份、权限与自摘要。"""

    expected = _action_diagnostic_path(
        run_dir,
        action_id,
        create_directory=False,
        supplied_path=path,
    )
    payload = _read_json(expected)
    if set(payload) != ACTION_DIAGNOSTIC_FIELDS:
        raise SupervisorError("动作失败诊断字段不闭合。")
    unsigned = dict(payload)
    digest = unsigned.pop("diagnostic_sha256", None)
    if (
        payload.get("schema_version") != ACTION_DIAGNOSTIC_SCHEMA
        or payload.get("campaign_id") != campaign_id
        or payload.get("phase") != phase
        or payload.get("action_id") != action_id
        or payload.get("owner_pid") != owner_pid
        or payload.get("owner_nonce") != owner_nonce
        or payload.get("failure_kind") not in ACTION_DIAGNOSTIC_FAILURE_KINDS
        or not isinstance(payload.get("error_type"), str)
        or _safe_id(payload.get("error_type"), "error_type")
        != payload.get("error_type")
        or not isinstance(payload.get("message"), str)
        or _action_diagnostic_message(payload.get("message"))
        != payload.get("message")
        or digest != _sha256(_canonical(unsigned))
    ):
        raise SupervisorError("动作失败诊断身份、内容或摘要非法。")
    _validate_action_diagnostic_timestamp(payload.get("recorded_at_utc"))
    return payload


def _write_action_diagnostic(
    path: Path,
    *,
    campaign_id: str,
    phase: str,
    action_id: str,
    owner_pid: int,
    owner_nonce: str,
    failure_kind: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    """以不可覆盖方式写一份动作失败诊断。"""

    campaign_id = _safe_id(campaign_id, "campaign_id")
    phase = _safe_id(phase, "phase", maximum=32)
    action_id = _safe_id(action_id, "action_id")
    owner_nonce = _safe_id(owner_nonce, "owner_nonce", maximum=128)
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise SupervisorError("动作失败诊断 owner_pid 非法。")
    if failure_kind not in ACTION_DIAGNOSTIC_FAILURE_KINDS:
        raise SupervisorError("动作失败诊断 failure_kind 非法。")
    error_type = _safe_id(error_type, "error_type")
    payload: dict[str, Any] = {
        "schema_version": ACTION_DIAGNOSTIC_SCHEMA,
        "campaign_id": campaign_id,
        "phase": phase,
        "action_id": action_id,
        "owner_pid": owner_pid,
        "owner_nonce": owner_nonce,
        "failure_kind": failure_kind,
        "error_type": error_type,
        "message": _action_diagnostic_message(message),
        "recorded_at_utc": _utc_now(),
    }
    payload["diagnostic_sha256"] = _sha256(_canonical(payload))
    _write_json(path, payload, replace=False)
    return payload


def write_campaign_run_action_diagnostic(
    *,
    failure_kind: str,
    error: BaseException,
) -> Path | None:
    """由 campaign-run 子进程写入动作专属的脱敏失败诊断。

    非 campaign-run 上下文不创建文件。调用方必须忽略这里的写入异常并保留
    原始失败；父进程会在子命令退出后独立校验或生成固定的兜底诊断。
    """

    if os.environ.get(CAMPAIGN_RUN_CONTEXT_ENV) != "1":
        return None
    client = SupervisorClient.attach_from_environment()
    if client is None or client.run_dir is None:
        raise SupervisorError("动作失败诊断无法附加父监督器。")
    action_id = os.environ.get(CAMPAIGN_RUN_ACTION_ID_ENV)
    diagnostic_value = os.environ.get(CAMPAIGN_RUN_ACTION_DIAGNOSTIC_ENV)
    if not action_id or not diagnostic_value:
        raise SupervisorError("campaign-run 上下文缺少动作失败诊断坐标。")
    path = _action_diagnostic_path(
        client.run_dir,
        action_id,
        create_directory=False,
        supplied_path=Path(diagnostic_value),
    )
    _write_action_diagnostic(
        path,
        campaign_id=client.campaign_id,
        phase=client.phase,
        action_id=action_id,
        owner_pid=client.owner_pid,
        owner_nonce=client.owner_nonce,
        failure_kind=failure_kind,
        error_type=type(error).__name__,
        message=str(error),
    )
    return path


def _read_state(run_dir: Path) -> dict[str, Any]:
    """读取并校验监督器状态，供 owner 与 monitor 共同使用。"""

    state = _read_json(run_dir / "state.json")
    if state.get("schema_version") != STATE_SCHEMA:
        raise SupervisorError("监督器 state schema 不匹配。")
    _safe_id(state.get("campaign_id"), "campaign_id")
    _safe_id(state.get("owner_nonce"), "owner_nonce", maximum=128)
    _safe_id(state.get("phase"), "phase", maximum=32)
    owner_pid = state.get("owner_pid")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise SupervisorError("监督器 owner_pid 非法。")
    _parse_epoch(state.get("started_at_epoch"), "started_at_epoch")
    _parse_epoch(state.get("deadline_at_epoch"), "deadline_at_epoch")
    started_monotonic_ns = _parse_monotonic_ns(
        state.get("started_monotonic_ns"), "started_monotonic_ns"
    )
    deadline_monotonic_ns = _parse_monotonic_ns(
        state.get("deadline_monotonic_ns"), "deadline_monotonic_ns"
    )
    if deadline_monotonic_ns <= started_monotonic_ns:
        raise SupervisorError("监督器单调 deadline 必须晚于启动时刻。")
    if state.get("state") not in {"running", *TERMINAL_STATES}:
        raise SupervisorError("监督器 state 状态非法。")
    return state


@contextmanager
def _state_lock(state_dir: Path) -> Iterator[None]:
    """串行化事件序号、摘要链和状态文件更新。"""

    lock_path = state_dir / ".supervisor.lock"
    if lock_path.exists() and lock_path.is_symlink():
        raise SupervisorError("监督器锁不得是符号链接。")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    # 必须先检查原始路径，再进行任何 open；先 resolve 会把恶意的最终
    # 符号链接解析到受管目录之外，导致审计记录写入不可控位置。
    if not path.is_absolute():
        raise SupervisorError("监督器日志路径必须是绝对路径。")
    path = Path(os.path.abspath(path))
    _reject_symlink_components(path)
    _validate_state_dir(path.parent, create=False)
    with _state_lock(path.parent):
        if path.exists():
            _validate_file(path)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise SupervisorError("监督器日志不是当前用户普通文件。")
            os.fchmod(descriptor, 0o600)
            raw = _canonical(payload)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _last_event(path: Path) -> tuple[int, str | None]:
    if not path.exists():
        return 0, None
    _validate_file(path)
    sequence = 0
    digest: str | None = None
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise SupervisorError("事件账本含损坏 JSON。") from error
            if not isinstance(record, dict):
                raise SupervisorError("事件账本记录必须是对象。")
            value = record.get("sequence")
            if isinstance(value, int) and not isinstance(value, bool):
                sequence = max(sequence, value)
            event_digest = record.get("event_sha256")
            if isinstance(event_digest, str):
                digest = event_digest
    return sequence, digest


def _append_event(
    run_dir: Path,
    *,
    event_type: str,
    operation: str,
    owner_pid: int,
    owner_nonce: str,
    campaign_id: str,
    phase: str,
    job_id: str | None = None,
    status: str = "running",
    reason: str | None = None,
    started_at_epoch: float | None = None,
    ended_at_epoch: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    event_type = _safe_id(event_type, "event_type", maximum=64)
    operation = _operation(operation)
    campaign_id = _safe_id(campaign_id, "campaign_id")
    phase = _safe_id(phase, "phase", maximum=32)
    owner_nonce = _safe_id(owner_nonce, "owner_nonce", maximum=128)
    if job_id is not None:
        job_id = _safe_id(job_id, "job_id")
    if not isinstance(status, str) or not status or len(status) > 32:
        raise SupervisorError("event status 非法。")
    if reason is not None:
        reason = _note(reason, "reason")
    if started_at_epoch is not None:
        started_at_epoch = _parse_epoch(started_at_epoch, "started_at_epoch")
    if ended_at_epoch is not None:
        ended_at_epoch = _parse_epoch(ended_at_epoch, "ended_at_epoch")
        if started_at_epoch is not None and ended_at_epoch < started_at_epoch:
            raise SupervisorError("event 时间倒退。")
    event_metadata = _metadata(metadata)
    path = run_dir / "events.ndjson"
    _validate_state_dir(run_dir, create=False)
    with _state_lock(run_dir):
        sequence, previous_digest = _last_event(path)
        unsigned: dict[str, Any] = {
            "schema_version": EVENT_SCHEMA,
            "sequence": sequence + 1,
            "recorded_at_utc": _utc_now(),
            "recorded_at_epoch": time.time(),
            "event_type": event_type,
            "operation": operation,
            "campaign_id": campaign_id,
            "phase": phase,
            "owner_pid": owner_pid,
            "owner_nonce": owner_nonce,
            "job_id": job_id,
            "status": status,
            "reason": reason,
            "started_at_epoch": started_at_epoch,
            "ended_at_epoch": ended_at_epoch,
            "metadata": event_metadata,
            "previous_event_sha256": previous_digest,
        }
        event = dict(unsigned)
        event["event_sha256"] = _sha256(_canonical(unsigned))
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, _canonical(event))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(run_dir)
    return event


def _read_latest_heartbeat(
    run_dir: Path,
    *,
    owner_pid: int | None = None,
    owner_nonce: str | None = None,
) -> dict[str, Any]:
    path = run_dir / "heartbeat.json"
    heartbeat = _read_json(path)
    expected = {
        "schema_version",
        "owner_pid",
        "owner_nonce",
        "updated_at_utc",
        "updated_at_epoch",
        "monotonic_ns",
        "operation",
        "job_id",
        "state",
        "event_sequence",
    }
    if set(heartbeat) != expected:
        raise SupervisorError("owner heartbeat 字段不闭合。")
    if heartbeat.get("schema_version") != HEARTBEAT_SCHEMA:
        raise SupervisorError("owner heartbeat schema 不匹配。")
    if owner_pid is not None and heartbeat.get("owner_pid") != owner_pid:
        raise SupervisorError("owner heartbeat PID 不一致。")
    if owner_nonce is not None and heartbeat.get("owner_nonce") != owner_nonce:
        raise SupervisorError("owner heartbeat nonce 不一致。")
    if isinstance(heartbeat.get("owner_pid"), bool) or not isinstance(
        heartbeat.get("owner_pid"), int
    ):
        raise SupervisorError("owner heartbeat PID 非法。")
    _safe_id(heartbeat.get("owner_nonce"), "heartbeat.owner_nonce", maximum=128)
    _operation(heartbeat.get("operation"))
    _parse_epoch(heartbeat.get("updated_at_epoch"), "heartbeat.updated_at_epoch")
    _parse_monotonic_ns(heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns")
    if heartbeat.get("state") not in {"running", "failed", *TERMINAL_STATES}:
        raise SupervisorError("owner heartbeat state 非法。")
    job_id = heartbeat.get("job_id")
    if job_id is not None:
        _safe_id(job_id, "heartbeat.job_id")
    sequence = heartbeat.get("event_sequence")
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0
    ):
        raise SupervisorError("owner heartbeat event_sequence 非法。")
    return heartbeat


def _write_state(run_dir: Path, updates: Mapping[str, Any]) -> dict[str, Any]:
    path = run_dir / "state.json"
    with _state_lock(run_dir):
        state = _read_json(path) if path.exists() else {}
        state.update(dict(updates))
        _write_json(path, state, replace=True)
    return state


def _set_terminal_state(
    run_dir: Path,
    *,
    state: str,
    terminal_at_epoch: float | None = None,
) -> dict[str, Any]:
    """设置终态且绝不覆盖已判定的 watchdog／审计终态。"""

    if state not in STOP_STATUSES:
        raise SupervisorError(f"非法监督器终态：{state}")
    with _state_lock(run_dir):
        current = _read_state(run_dir)
        existing = current.get("state")
        if existing in TERMINAL_STATES:
            return current
        updates: dict[str, Any] = {
            "state": state,
            "terminal_at_utc": _utc_now(),
            "terminal_at_epoch": (
                float(terminal_at_epoch)
                if terminal_at_epoch is not None
                else time.time()
            ),
        }
        current.update(updates)
        _write_json(run_dir / "state.json", current, replace=True)
        return current


def _owner_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def _terminate_owner(pid: int) -> None:
    """有界请求 owner 退出；不向未知进程发送信号。"""

    if pid <= 0 or pid == os.getpid() or not _owner_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _owner_alive(pid):
        time.sleep(0.05)
    if _owner_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _request_process_cleanup(process: subprocess.Popen[Any]) -> None:
    """请求受管 worker 展开清理；不立即杀死其独立子进程组。"""

    if process.poll() is not None:
        return
    try:
        os.kill(process.pid, signal.SIGUSR1)
    except (AttributeError, OSError, ProcessLookupError):
        # 不支持 SIGUSR1 的平台仍以 SIGTERM 失败关闭；正式 ARM64 路径固定
        # 支持 SIGUSR1，Codex worker 会把它转换为可展开的清理异常。
        try:
            process.terminate()
        except (AttributeError, OSError, ProcessLookupError):
            pass


def _bucket_start(epoch: float, interval: float) -> float:
    """返回稳定的账本桶起点；支持短时离线测试的非整数间隔。"""

    return math.floor(epoch / interval) * interval


def _write_minute_record(
    run_dir: Path,
    *,
    bucket_start: float,
    bucket_end: float,
    heartbeat: Mapping[str, Any] | None,
    owner_alive: bool,
    heartbeat_age: float | None,
    classification: str,
    reason: str | None = None,
) -> None:
    record = {
        "schema_version": MINUTE_SCHEMA,
        "bucket_start_utc": _epoch_to_utc(bucket_start),
        "bucket_end_utc": _epoch_to_utc(bucket_end),
        "bucket_start_epoch": round(float(bucket_start), 6),
        "bucket_end_epoch": round(float(bucket_end), 6),
        "duration_seconds": round(max(0.0, bucket_end - bucket_start), 3),
        "recorded_at_utc": _utc_now(),
        "classification": classification,
        "operation": heartbeat.get("operation") if heartbeat else None,
        "job_id": heartbeat.get("job_id") if heartbeat else None,
        "state": heartbeat.get("state") if heartbeat else "unknown",
        "owner_alive": owner_alive,
        "heartbeat_age_seconds": (
            round(max(0.0, float(heartbeat_age)), 3)
            if heartbeat_age is not None
            else None
        ),
        "source_event_sequence": (
            heartbeat.get("event_sequence") if heartbeat else None
        ),
        "reason": reason,
    }
    _append_jsonl(run_dir / "minute-ledger.ndjson", record)


def _minute_classification(
    heartbeat: Mapping[str, Any] | None,
    state_name: str,
) -> str:
    """把父监督器操作前缀转换为分钟账本分类。"""

    if state_name in {"watchdog-aborted", "audit-incomplete"}:
        return "audit-incomplete"
    if state_name == "failed":
        return "failed"
    if heartbeat is None:
        return "waiting"
    operation = str(heartbeat.get("operation", ""))
    prefix = operation.partition(":")[0]
    if prefix in CAMPAIGN_CLASSIFICATIONS:
        return prefix
    return "active" if heartbeat.get("state") == "running" else "waiting"


def _write_watchdog_heartbeat(
    run_dir: Path,
    *,
    owner_alive: bool,
    heartbeat: Mapping[str, Any] | None,
    heartbeat_age: float | None,
    status: str,
) -> None:
    record = {
        "schema_version": WATCHDOG_HEARTBEAT_SCHEMA,
        "recorded_at_utc": _utc_now(),
        "recorded_at_epoch": time.time(),
        "recorded_monotonic_ns": time.monotonic_ns(),
        "status": status,
        "owner_alive": owner_alive,
        "heartbeat_age_seconds": (
            round(max(0.0, float(heartbeat_age)), 3)
            if heartbeat_age is not None
            else None
        ),
        "operation": heartbeat.get("operation") if heartbeat else None,
        "job_id": heartbeat.get("job_id") if heartbeat else None,
        "owner_heartbeat_at_utc": (
            heartbeat.get("updated_at_utc") if heartbeat else None
        ),
        "owner_heartbeat_monotonic_ns": (
            heartbeat.get("monotonic_ns") if heartbeat else None
        ),
        "owner_event_sequence": (
            heartbeat.get("event_sequence") if heartbeat else None
        ),
    }
    _append_jsonl(run_dir / "watchdog-heartbeats.ndjson", record)


def _stop_receipt(
    run_dir: Path,
    *,
    event_type: str,
    reason: str,
    detected_at_epoch: float,
    owner_pid: int,
    owner_nonce: str,
    campaign_id: str,
    phase: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": STOP_SCHEMA,
        "event_type": event_type,
        "reason": _note(reason, "reason"),
        "detected_at_utc": _epoch_to_utc(detected_at_epoch),
        "detected_at_epoch": detected_at_epoch,
        "owner_pid": owner_pid,
        "owner_nonce": owner_nonce,
        "campaign_id": campaign_id,
        "phase": phase,
    }
    payload["receipt_sha256"] = _sha256(_canonical(payload))
    path = run_dir / "stop-receipt.json"
    if path.exists():
        existing = _read_json(path)
        if existing != payload:
            # 停线事实不可覆盖；返回第一次观察到的事实。
            return existing
        return existing
    _write_json(path, payload, replace=False)
    return payload


def _read_stop_request(
    run_dir: Path,
    *,
    owner_pid: int,
    owner_nonce: str,
) -> dict[str, Any]:
    """读取并绑定 owner 写入的停线请求，拒绝伪造或覆盖请求。"""

    request = _read_json(run_dir / "stop-request.json")
    expected = {
        "schema_version",
        "requested_at_utc",
        "requested_at_epoch",
        "reason",
        "status",
        "owner_pid",
        "owner_nonce",
    }
    if set(request) != expected:
        raise SupervisorError("监督器 stop-request 字段不闭合。")
    if (
        request.get("schema_version") != STOP_SCHEMA
        or request.get("owner_pid") != owner_pid
        or request.get("owner_nonce") != owner_nonce
        or request.get("status") not in STOP_STATUSES
    ):
        raise SupervisorError("监督器 stop-request 身份或状态非法。")
    _parse_epoch(request.get("requested_at_epoch"), "requested_at_epoch")
    if not isinstance(request.get("requested_at_utc"), str):
        raise SupervisorError("监督器 stop-request 时间非法。")
    request["reason"] = _note(request.get("reason"), "stop reason")
    return request


def _monitor_impl(args: argparse.Namespace) -> int:
    run_dir = _validate_state_dir(Path(args.state_dir), create=False)
    state = _read_state(run_dir)
    owner_pid = int(state["owner_pid"])
    owner_nonce = _safe_id(state["owner_nonce"], "owner_nonce", maximum=128)
    campaign_id = _safe_id(state["campaign_id"], "campaign_id")
    phase = _safe_id(state["phase"], "phase", maximum=32)
    heartbeat_seconds = _positive_seconds(args.heartbeat_seconds, "heartbeat_seconds")
    timeout_seconds = _positive_seconds(
        args.watchdog_timeout_seconds, "watchdog_timeout_seconds"
    )
    ledger_interval = _positive_seconds(
        args.ledger_interval_seconds, "ledger_interval_seconds"
    )
    _parse_epoch(state["deadline_at_epoch"], "deadline_at_epoch")
    started_epoch = _parse_epoch(state["started_at_epoch"], "started_at_epoch")
    started_monotonic_ns = _parse_monotonic_ns(
        state["started_monotonic_ns"], "started_monotonic_ns"
    )
    deadline_monotonic_ns = _parse_monotonic_ns(
        state["deadline_monotonic_ns"], "deadline_monotonic_ns"
    )
    if deadline_monotonic_ns <= started_monotonic_ns:
        raise SupervisorError("监督器单调 deadline 必须晚于启动时刻。")
    last_watchdog_monotonic_ns = 0
    next_bucket = _bucket_start(started_epoch, ledger_interval)
    poll_seconds = max(0.05, min(DEFAULT_POLL_SECONDS, heartbeat_seconds / 2.0))
    terminal_state: str | None = None
    terminal_ledger_finalized = False

    def flush_ledger(
        now: float,
        heartbeat: Mapping[str, Any] | None,
        owner_alive: bool,
        heartbeat_age: float | None,
        *,
        final: bool = False,
        state_name: str = "running",
    ) -> None:
        nonlocal next_bucket
        # 先写完所有完整桶，再在终态写当前未满桶。每次写入的起点严格
        # 接续上一次的 next_bucket，因而不会出现重叠或无记录空洞。
        while next_bucket + ledger_interval <= now + 1e-6:
            bucket_start = max(next_bucket, started_epoch)
            bucket_end = next_bucket + ledger_interval
            if bucket_end > bucket_start:
                _write_minute_record(
                    run_dir,
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    heartbeat=heartbeat,
                    owner_alive=owner_alive,
                    heartbeat_age=heartbeat_age,
                    classification=_minute_classification(
                        heartbeat,
                        state_name,
                    ),
                    reason=("terminal-full-bucket" if final else None),
                )
            next_bucket += ledger_interval
        if final and now > next_bucket + 1e-6:
            bucket_start = max(next_bucket, started_epoch)
            if now > bucket_start:
                _write_minute_record(
                    run_dir,
                    bucket_start=bucket_start,
                    bucket_end=now,
                    heartbeat=heartbeat,
                    owner_alive=owner_alive,
                    heartbeat_age=heartbeat_age,
                    classification=_minute_classification(
                        heartbeat,
                        state_name,
                    ),
                    reason="partial-final-bucket",
                )
                next_bucket = now

    def abort(reason: str, *, operation: str, now: float) -> None:
        """一次性封存不可恢复的 watchdog 终态。"""

        nonlocal terminal_state, terminal_ledger_finalized
        current = _read_state(run_dir)
        if current.get("state") in TERMINAL_STATES:
            terminal_state = str(current["state"])
            return
        try:
            _append_event(
                run_dir,
                event_type="audit-incomplete",
                operation=operation,
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                status="audit-incomplete",
                reason=reason,
            )
            _append_event(
                run_dir,
                event_type="watchdog-aborted",
                operation=operation,
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                status="watchdog-aborted",
                reason=reason,
            )
            _stop_receipt(
                run_dir,
                event_type="watchdog-aborted",
                reason=reason,
                detected_at_epoch=now,
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
            )
        finally:
            # 终态一旦可见，审计必须已经能覆盖到同一终态时间；不能把
            # 最后一个部分分钟桶留给后续调度再补写。
            flush_ledger(
                now,
                None,
                _owner_alive(owner_pid),
                None,
                final=True,
                state_name="watchdog-aborted",
            )
            terminal_ledger_finalized = True
            _set_terminal_state(
                run_dir, state="watchdog-aborted", terminal_at_epoch=now
            )
        terminal_state = "watchdog-aborted"
        if args.terminate_owner:
            _terminate_owner(owner_pid)

    # 启动即写一条监督器心跳，避免极短命令在第一次轮询前正常停止时
    # 产生“心跳缺失”的假审计缺口；后续记录仍按固定间隔追加。
    try:
        initial_now = time.time()
        initial_monotonic_ns = time.monotonic_ns()
        initial_heartbeat = _read_latest_heartbeat(
            run_dir, owner_pid=owner_pid, owner_nonce=owner_nonce
        )
        initial_owner_monotonic_ns = _parse_monotonic_ns(
            initial_heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns"
        )
        initial_age = max(
            0.0,
            (initial_monotonic_ns - initial_owner_monotonic_ns)
            / 1_000_000_000,
        )
        _write_watchdog_heartbeat(
            run_dir,
            owner_alive=_owner_alive(owner_pid),
            heartbeat=initial_heartbeat,
            heartbeat_age=initial_age,
            status="active",
        )
        last_watchdog_monotonic_ns = initial_monotonic_ns
    except SupervisorError as error:
        abort(
            f"heartbeat-invalid-{type(error).__name__}",
            operation="supervisor:startup-heartbeat",
            now=initial_now,
        )

    while terminal_state is None:
        state = _read_state(run_dir)
        if state.get("state") in TERMINAL_STATES:
            terminal_state = str(state["state"])
            break
        now = time.time()
        now_monotonic_ns = time.monotonic_ns()
        heartbeat: dict[str, Any] | None = None
        try:
            heartbeat = _read_latest_heartbeat(
                run_dir, owner_pid=owner_pid, owner_nonce=owner_nonce
            )
        except SupervisorError as error:
            abort(
                f"heartbeat-invalid-{type(error).__name__}",
                operation="supervisor:read-heartbeat",
                now=now,
            )
            continue
        _parse_epoch(heartbeat.get("updated_at_epoch"), "heartbeat.updated_at_epoch")
        heartbeat_monotonic_ns = _parse_monotonic_ns(
            heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns"
        )
        if heartbeat_monotonic_ns < started_monotonic_ns:
            abort(
                "owner-heartbeat-before-supervisor-start",
                operation="supervisor:heartbeat-check",
                now=now,
            )
            continue
        heartbeat_age = max(
            0.0,
            (now_monotonic_ns - heartbeat_monotonic_ns) / 1_000_000_000,
        )
        owner_is_alive = _owner_alive(owner_pid)

        if now_monotonic_ns >= deadline_monotonic_ns:
            abort(
                "global-wall-clock-deadline-expired",
                operation="supervisor:deadline",
                now=now,
            )
            continue

        stop_path = run_dir / "stop-request.json"
        if stop_path.exists():
            try:
                request = _read_stop_request(
                    run_dir, owner_pid=owner_pid, owner_nonce=owner_nonce
                )
            except SupervisorError as error:
                abort(
                    f"stop-request-invalid-{type(error).__name__}",
                    operation="supervisor:stop-request",
                    now=now,
                )
                continue
            requested_status = str(request["status"])
            reason = str(request["reason"])
            _append_event(
                run_dir,
                event_type="stop-requested",
                operation="supervisor:stop-request",
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                status="stopping",
                reason=reason,
            )
            _append_event(
                run_dir,
                event_type=("stopped" if requested_status == "stopped" else requested_status),
                operation="supervisor:stop",
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                status=requested_status,
                reason=reason,
            )
            _stop_receipt(
                run_dir,
                event_type=("stopped" if requested_status == "stopped" else requested_status),
                reason=reason,
                detected_at_epoch=now,
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
            )
            # 先把终态覆盖区间持久化，再发布 state 终态。这样调用方读到
            # failed/stopped 后立即执行 audit 也不会观察到瞬时缺口。
            flush_ledger(
                now,
                heartbeat,
                owner_is_alive,
                heartbeat_age,
                final=True,
                state_name=requested_status,
            )
            terminal_ledger_finalized = True
            resulting = _set_terminal_state(
                run_dir, state=requested_status, terminal_at_epoch=now
            )
            terminal_state = str(resulting.get("state", requested_status))
            continue

        if not owner_is_alive:
            abort(
                "owner-process-not-alive"
                if heartbeat_age <= timeout_seconds
                else "owner-process-not-alive-after-heartbeat-gap",
                operation="supervisor:owner-check",
                now=now,
            )
            continue

        if heartbeat_age > timeout_seconds:
            abort(
                f"owner-heartbeat-timeout-{heartbeat_age:.3f}s",
                operation="supervisor:heartbeat-check",
                now=now,
            )
            continue

        if (
            last_watchdog_monotonic_ns == 0
            or now_monotonic_ns - last_watchdog_monotonic_ns
            >= int(heartbeat_seconds * 1_000_000_000)
        ):
            _write_watchdog_heartbeat(
                run_dir,
                owner_alive=owner_is_alive,
                heartbeat=heartbeat,
                heartbeat_age=heartbeat_age,
                status="active",
            )
            last_watchdog_monotonic_ns = now_monotonic_ns
        flush_ledger(now, heartbeat, owner_is_alive, heartbeat_age)
        remaining_monotonic = (
            deadline_monotonic_ns - time.monotonic_ns()
        ) / 1_000_000_000
        time.sleep(min(poll_seconds, max(0.05, remaining_monotonic)))

    # 终态封存所有完整桶和最后的部分桶；即使强停发生在桶边界之前，
    # 也会留下明确的 audit-incomplete/stopped 分类。
    now = time.time()
    try:
        heartbeat = _read_latest_heartbeat(
            run_dir, owner_pid=owner_pid, owner_nonce=owner_nonce
        )
    except SupervisorError:
        heartbeat = None
    heartbeat_monotonic_ns = (
        _parse_monotonic_ns(heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns")
        if heartbeat
        else None
    )
    heartbeat_age = (
        max(
            0.0,
            (time.monotonic_ns() - heartbeat_monotonic_ns) / 1_000_000_000,
        )
        if heartbeat_monotonic_ns is not None
        else None
    )
    if not terminal_ledger_finalized:
        flush_ledger(
            now,
            heartbeat,
            _owner_alive(owner_pid),
            heartbeat_age,
            final=True,
            state_name=terminal_state or "audit-incomplete",
        )
    return 0


def _monitor(args: argparse.Namespace) -> int:
    """把监督器自身异常转换成不可覆盖的 audit-incomplete 终态。"""

    try:
        return _monitor_impl(args)
    except BaseException as error:
        # 监控循环若因未知异常退出，不能把仍为 running 的状态留给 owner
        # 误判成正常停止。尽可能使用已经落盘的身份封存停线事实；若状态本身
        # 已损坏，则保留非零退出码，由上层 Campaign lease 继续 fail-close。
        try:
            run_dir = _validate_state_dir(Path(args.state_dir), create=False)
            state = _read_state(run_dir)
            if state.get("state") not in TERMINAL_STATES:
                now = time.time()
                owner_pid = int(state["owner_pid"])
                owner_nonce = str(state["owner_nonce"])
                campaign_id = str(state["campaign_id"])
                phase = str(state["phase"])
                reason = f"monitor-internal-{type(error).__name__}"
                _append_event(
                    run_dir,
                    event_type="audit-incomplete",
                    operation="supervisor:internal-error",
                    owner_pid=owner_pid,
                    owner_nonce=owner_nonce,
                    campaign_id=campaign_id,
                    phase=phase,
                    status="audit-incomplete",
                    reason=reason,
                )
                _stop_receipt(
                    run_dir,
                    event_type="audit-incomplete",
                    reason=reason,
                    detected_at_epoch=now,
                    owner_pid=owner_pid,
                    owner_nonce=owner_nonce,
                    campaign_id=campaign_id,
                    phase=phase,
                )
                _set_terminal_state(
                    run_dir,
                    state="audit-incomplete",
                    terminal_at_epoch=now,
                )
        except BaseException:
            pass
        return 1


class SupervisorClient:
    """升级进程使用的监督器客户端。

    ``start`` 会另起一个 Python 进程；客户端自身只负责实时写入 owner 心跳和
    事件。客户端被强杀后，独立进程仍会继续写分钟账本并判定 watchdog 状态。
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        campaign_id: str,
        phase: str,
        deadline_at_epoch: float,
        owner_pid: int | None = None,
        owner_nonce: str | None = None,
        heartbeat_seconds: int | float = DEFAULT_HEARTBEAT_SECONDS,
        watchdog_timeout_seconds: int | float = DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
        ledger_interval_seconds: int | float = DEFAULT_LEDGER_INTERVAL_SECONDS,
        terminate_owner: bool = True,
    ) -> None:
        self.base_dir = Path(state_dir)
        self.campaign_id = _safe_id(campaign_id, "campaign_id")
        self.phase = _safe_id(phase, "phase", maximum=32)
        self.owner_pid = int(owner_pid or os.getpid())
        if self.owner_pid <= 0:
            raise SupervisorError("owner_pid 非法。")
        self.deadline_at_epoch = _parse_epoch(deadline_at_epoch, "deadline_at_epoch")
        self.heartbeat_seconds = _positive_seconds(
            heartbeat_seconds, "heartbeat_seconds"
        )
        self.watchdog_timeout_seconds = _positive_seconds(
            watchdog_timeout_seconds, "watchdog_timeout_seconds"
        )
        self.ledger_interval_seconds = _positive_seconds(
            ledger_interval_seconds, "ledger_interval_seconds"
        )
        # 正式运行由调用方固定为 5/20/60 秒；这里不强制 heartbeat 小于
        # timeout，以便用几十毫秒的短预算做确定性的离线回归测试。
        self.terminate_owner = bool(terminate_owner)
        self.owner_nonce = (
            _safe_id(owner_nonce, "owner_nonce", maximum=128)
            if owner_nonce is not None
            else secrets.token_hex(32)
        )
        self.run_dir: Path | None = None
        self.process: subprocess.Popen[Any] | None = None
        self._started_monotonic_ns: int | None = None
        self._deadline_monotonic_ns: int | None = None
        self._last_heartbeat_monotonic = 0.0
        self._action_started: dict[str, float] = {}
        self._last_event_sequence: int | None = None
        self._started = False
        # attach 模式只引用 campaign-run 已经启动的父监督器，不创建 monitor
        # 子进程，也不拥有结束父监督器的权限。
        self._attached = False
        self._stop_completed = False
        self._command_failed = False

    @property
    def started(self) -> bool:
        return self._started and self.run_dir is not None

    @property
    def attached(self) -> bool:
        """当前客户端是否复用了 campaign-run 的父监督器。"""

        return self._attached and self.started

    @classmethod
    def attach(cls, run_dir: Path) -> "SupervisorClient":
        """从受管父进程上下文附加到已有监督器。

        该方法只允许 campaign-run 直接子进程调用：父 PID、Campaign 身份、
        owner nonce 和 deadline 必须同时来自环境变量并与 state.json 完全一致。
        附加客户端不会启动或停止 monitor，避免形成“父监督器套子监督器”。
        """

        run_dir = _validate_state_dir(Path(run_dir), create=False)
        state = _read_state(run_dir)
        if state.get("state") != "running":
            raise SupervisorError("campaign-run 父监督器不在运行态。")
        owner_pid = int(state["owner_pid"])
        if owner_pid != os.getppid():
            raise SupervisorError("campaign-run 父监督器 owner PID 与当前父进程不一致。")
        if not _owner_alive(owner_pid) or not _owner_alive(int(state.get("monitor_pid", -1))):
            raise SupervisorError("campaign-run 父监督器或 monitor 不在线。")
        expected_id = os.environ.get(CAMPAIGN_RUN_ID_ENV)
        expected_phase = os.environ.get(CAMPAIGN_RUN_PHASE_ENV)
        expected_pid = os.environ.get(CAMPAIGN_RUN_OWNER_PID_ENV)
        expected_nonce = os.environ.get(CAMPAIGN_RUN_OWNER_NONCE_ENV)
        expected_deadline = os.environ.get(CAMPAIGN_RUN_DEADLINE_ENV)
        if (
            expected_id != str(state["campaign_id"])
            or expected_phase != str(state["phase"])
            or expected_pid != str(owner_pid)
            or expected_nonce != str(state["owner_nonce"])
            or expected_deadline != str(state["deadline_at_epoch"])
        ):
            raise SupervisorError("campaign-run 父监督器上下文身份不一致。")
        client = cls(
            run_dir,
            campaign_id=str(state["campaign_id"]),
            phase=str(state["phase"]),
            deadline_at_epoch=float(state["deadline_at_epoch"]),
            owner_pid=owner_pid,
            owner_nonce=str(state["owner_nonce"]),
            heartbeat_seconds=float(state["heartbeat_seconds"]),
            watchdog_timeout_seconds=float(state["watchdog_timeout_seconds"]),
            ledger_interval_seconds=float(state["ledger_interval_seconds"]),
            terminate_owner=False,
        )
        client.run_dir = run_dir
        client._started = True
        client._attached = True
        client._started_monotonic_ns = int(state["started_monotonic_ns"])
        client._deadline_monotonic_ns = int(state["deadline_monotonic_ns"])
        client._ensure_monitor_alive()
        return client

    @classmethod
    def attach_from_environment(cls) -> "SupervisorClient | None":
        """按 campaign-run 标记附加；无标记时返回 ``None``。"""

        if os.environ.get(CAMPAIGN_RUN_CONTEXT_ENV) != "1":
            return None
        raw_run_dir = os.environ.get(CAMPAIGN_RUN_DIR_ENV)
        if not raw_run_dir:
            raise SupervisorError("campaign-run 上下文缺少父 run_dir。")
        return cls.attach(Path(raw_run_dir))

    def start(self) -> "SupervisorClient":
        if self.started:
            raise SupervisorError("监督器不得重复启动。")
        if self._stop_completed:
            raise SupervisorError("监督器实例已经收口，不能重新启动。")
        if os.environ.get(CAMPAIGN_RUN_CONTEXT_ENV) == "1":
            raise SupervisorError(
                "campaign-run 子进程不得启动新的监督器；请附加父 run。"
            )
        now = time.time()
        now_monotonic_ns = time.monotonic_ns()
        remaining = self.deadline_at_epoch - now
        if remaining <= 0:
            raise SupervisorTimeout("监督器启动时全局墙钟预算已到期。")
        deadline_monotonic_ns = now_monotonic_ns + max(
            1, int(math.ceil(remaining * 1_000_000_000))
        )
        self._started_monotonic_ns = now_monotonic_ns
        self._deadline_monotonic_ns = deadline_monotonic_ns
        base = _validate_state_dir(self.base_dir, create=True)
        run_dir = base / f"run-{self.owner_nonce}"
        run_dir.mkdir(mode=0o700)
        run_dir = _validate_state_dir(run_dir, create=False)
        state = {
            "schema_version": STATE_SCHEMA,
            "supervisor_schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "phase": self.phase,
            "owner_pid": self.owner_pid,
            "owner_nonce": self.owner_nonce,
            "started_at_utc": _epoch_to_utc(now),
            "started_at_epoch": now,
            "started_monotonic_ns": now_monotonic_ns,
            "deadline_at_epoch": self.deadline_at_epoch,
            "deadline_monotonic_ns": deadline_monotonic_ns,
            "heartbeat_seconds": self.heartbeat_seconds,
            "watchdog_timeout_seconds": self.watchdog_timeout_seconds,
            "ledger_interval_seconds": self.ledger_interval_seconds,
            "state": "running",
            "terminate_owner": self.terminate_owner,
            "campaign_started_at_epoch": now,
            "predecessor_run_dir": None,
            "predecessor_state_sha256": None,
        }
        _write_json(run_dir / "state.json", state, replace=False)
        self.run_dir = run_dir
        self._write_owner_heartbeat(operation="supervisor:start", state="running", force=True)
        event = self._event(
            "command-started",
            "supervisor:start",
            status="running",
        )
        self._last_event_sequence = int(event["sequence"])
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "monitor",
            "--state-dir",
            str(run_dir),
            "--heartbeat-seconds",
            str(self.heartbeat_seconds),
            "--watchdog-timeout-seconds",
            str(self.watchdog_timeout_seconds),
            "--ledger-interval-seconds",
            str(self.ledger_interval_seconds),
        ]
        if self.terminate_owner:
            command.append("--terminate-owner")
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["CODEX_UPGRADE_SUPERVISOR_RUN_DIR"] = str(run_dir)
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as error:
            self._event(
                "supervisor-failed",
                "supervisor:start",
                status="failed",
                reason=f"无法启动独立监督器：{error}",
            )
            raise SupervisorError("无法启动独立监督器。") from error
        _write_state(run_dir, {"monitor_pid": self.process.pid})
        self._started = True
        return self

    def _require_started(self) -> Path:
        if not self.started:
            raise SupervisorError("监督器尚未启动。")
        assert self.run_dir is not None
        return self.run_dir

    def _mark_monitor_exit(self) -> bool:
        """监控进程异常退出且仍为 running 时立即封存审计缺口。"""

        if self.process is None or self.process.poll() is None or self.run_dir is None:
            return False
        run_dir = self.run_dir
        try:
            state = _read_state(run_dir)
        except BaseException:
            # 状态本身损坏时由调用方继续 fail-close；这里不尝试猜测身份。
            return False
        if state.get("state") in TERMINAL_STATES:
            return False
        now = time.time()
        reason = f"monitor-exited-returncode-{self.process.returncode}"
        owner_pid = int(state["owner_pid"])
        owner_nonce = str(state["owner_nonce"])
        campaign_id = str(state["campaign_id"])
        phase = str(state["phase"])
        try:
            _append_event(
                run_dir,
                event_type="audit-incomplete",
                operation="supervisor:monitor-exit",
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                status="audit-incomplete",
                reason=reason,
            )
            _stop_receipt(
                run_dir,
                event_type="audit-incomplete",
                reason=reason,
                detected_at_epoch=now,
                owner_pid=owner_pid,
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
            )
        finally:
            _set_terminal_state(
                run_dir,
                state="audit-incomplete",
                terminal_at_epoch=now,
            )
        return True

    def _ensure_monitor_alive(self) -> None:
        """任何新动作前确认独立监督器仍在运行。"""

        if self._stop_completed:
            raise SupervisorError("监督器已经收口。")
        if self._attached:
            run_dir = self._require_started()
            state = _read_state(run_dir)
            if state.get("state") != "running":
                raise SupervisorError("campaign-run 父监督器已经停线。")
            monitor_pid = state.get("monitor_pid")
            if (
                isinstance(monitor_pid, bool)
                or not isinstance(monitor_pid, int)
                or monitor_pid <= 0
                or not _owner_alive(monitor_pid)
            ):
                raise SupervisorError("campaign-run 父监督器 monitor 已退出。")
            return
        if self.process is not None and self.process.poll() is not None:
            self._mark_monitor_exit()
            raise SupervisorError("独立监督器已退出。")

    def _event(
        self,
        event_type: str,
        operation: str,
        *,
        job_id: str | None = None,
        status: str = "running",
        reason: str | None = None,
        started_at_epoch: float | None = None,
        ended_at_epoch: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_dir = self._require_started() if self.started else self.run_dir
        if run_dir is None:
            raise SupervisorError("监督器运行目录尚未建立。")
        return _append_event(
            run_dir,
            event_type=event_type,
            operation=operation,
            owner_pid=self.owner_pid,
            owner_nonce=self.owner_nonce,
            campaign_id=self.campaign_id,
            phase=self.phase,
            job_id=job_id,
            status=status,
            reason=reason,
            started_at_epoch=started_at_epoch,
            ended_at_epoch=ended_at_epoch,
            metadata=metadata,
        )

    def _write_owner_heartbeat(
        self,
        *,
        operation: str,
        job_id: str | None = None,
        state: str = "running",
        force: bool = False,
    ) -> None:
        run_dir = self._require_started() if self.started else self.run_dir
        if run_dir is None:
            raise SupervisorError("监督器运行目录尚未建立。")
        self._ensure_monitor_alive()
        operation = _operation(operation)
        if (
            self._started
            and self._deadline_monotonic_ns is not None
            and time.monotonic_ns() >= self._deadline_monotonic_ns
            and state == "running"
        ):
            raise SupervisorTimeout("监督器全局墙钟预算已到期。")
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self._last_heartbeat_monotonic
            < self.heartbeat_seconds
        ):
            return
        payload = {
            "schema_version": HEARTBEAT_SCHEMA,
            "owner_pid": self.owner_pid,
            "owner_nonce": self.owner_nonce,
            "updated_at_utc": _utc_now(),
            "updated_at_epoch": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "operation": operation,
            "job_id": _safe_id(job_id, "job_id") if job_id is not None else None,
            "state": state,
            "event_sequence": self._last_event_sequence,
        }
        _write_json(run_dir / "heartbeat.json", payload, replace=True)
        self._last_heartbeat_monotonic = now_monotonic

    def heartbeat(
        self,
        operation: str,
        *,
        job_id: str | None = None,
        state: str = "running",
        force: bool = False,
    ) -> None:
        self._write_owner_heartbeat(
            operation=operation,
            job_id=job_id,
            state=state,
            force=force,
        )

    def event_start(self, operation: str, *, job_id: str | None = None) -> int:
        self._ensure_monitor_alive()
        operation = _operation(operation)
        started = time.time()
        self._action_started[operation] = started
        event = self._event(
            "action-started",
            operation,
            job_id=job_id,
            status="running",
            started_at_epoch=started,
        )
        self._last_event_sequence = int(event["sequence"])
        self._write_owner_heartbeat(operation=operation, job_id=job_id, force=True)
        return int(event["sequence"])

    def event_end(
        self,
        operation: str,
        *,
        job_id: str | None = None,
        status: str = "passed",
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        self._ensure_monitor_alive()
        operation = _operation(operation)
        started = self._action_started.pop(operation, None)
        ended = time.time()
        event = self._event(
            "action-finished",
            operation,
            job_id=job_id,
            status=status,
            started_at_epoch=started,
            ended_at_epoch=ended,
            metadata={
                **dict(metadata or {}),
                "duration_seconds": round(ended - started, 3) if started else None,
            },
        )
        self._last_event_sequence = int(event["sequence"])
        self._write_owner_heartbeat(operation=f"{operation}:complete", job_id=job_id, force=True)
        return int(event["sequence"])

    def event_fail(
        self,
        operation: str,
        *,
        job_id: str | None = None,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        self._ensure_monitor_alive()
        operation = _operation(operation)
        started = self._action_started.pop(operation, None)
        ended = time.time()
        event = self._event(
            "action-failed",
            operation,
            job_id=job_id,
            status="failed",
            reason=reason,
            started_at_epoch=started,
            ended_at_epoch=ended,
            metadata={
                **dict(metadata or {}),
                "duration_seconds": round(ended - started, 3) if started else None,
            },
        )
        self._last_event_sequence = int(event["sequence"])
        self._write_owner_heartbeat(operation=f"{operation}:failed", job_id=job_id, state="failed", force=True)
        return int(event["sequence"])

    def run_command(
        self,
        argv: Sequence[str],
        *,
        operation: str,
        timeout_seconds: float,
        cleanup_grace_seconds: float = 0,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = True,
        text: bool = True,
        output_stream: Any | None = None,
        job_id: str | None = None,
        heartbeat_callback: Any | None = None,
        merge_stderr: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        """统一执行入口；命令开始、结束、失败和超时均写入事件账本。"""

        run_dir = self._require_started()
        command = [str(item) for item in argv]
        if not command or any(not item for item in command):
            raise SupervisorError("统一命令入口收到空参数。")
        timeout_seconds = _positive_seconds(timeout_seconds, "统一命令入口 timeout")
        if (
            isinstance(cleanup_grace_seconds, bool)
            or not isinstance(cleanup_grace_seconds, (int, float))
            or not math.isfinite(float(cleanup_grace_seconds))
            or float(cleanup_grace_seconds) < 0
        ):
            raise SupervisorError("统一命令入口 cleanup grace 非法。")
        cleanup_grace = float(cleanup_grace_seconds)
        self._ensure_monitor_alive()
        if self._deadline_monotonic_ns is None:
            raise SupervisorError("监督器单调 deadline 尚未初始化。")
        remaining_wall = (
            self._deadline_monotonic_ns - time.monotonic_ns()
        ) / 1_000_000_000
        terminal_drain = (
            min(DEFAULT_TERMINAL_DRAIN_SECONDS, cleanup_grace / 4.0)
            if cleanup_grace > 0
            else 0.0
        )
        if remaining_wall <= cleanup_grace + terminal_drain:
            raise SupervisorTimeout(
                f"统一命令剩余预算不足以容纳清理与终态排空：{operation}"
            )
        self.event_start(operation, job_id=job_id)
        if output_stream is not None and capture_output:
            raise SupervisorError("统一命令入口不能同时指定 output_stream 和 capture_output。")
        output_target: Any = (
            output_stream
            if output_stream is not None
            else (subprocess.PIPE if capture_output else subprocess.DEVNULL)
        )
        stderr_target: Any = (
            output_stream
            if output_stream is not None and merge_stderr
            else (
                subprocess.STDOUT
                if capture_output and merge_stderr
                else (subprocess.PIPE if capture_output else subprocess.DEVNULL)
            )
        )
        process_environment = dict(env) if env is not None else None
        execution_deadline_epoch = self.deadline_at_epoch - cleanup_grace
        if cleanup_grace > 0:
            if process_environment is None:
                process_environment = os.environ.copy()
            process_environment.update(
                {
                    CAMPAIGN_RUN_CLEANUP_SIGNAL_ENV: "SIGUSR1",
                    CAMPAIGN_RUN_EXECUTION_DEADLINE_ENV: str(
                        execution_deadline_epoch
                    ),
                    CAMPAIGN_RUN_CLEANUP_GRACE_ENV: str(cleanup_grace),
                }
            )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output_target,
                stderr=stderr_target,
                cwd=cwd,
                env=process_environment,
                text=text,
                start_new_session=True,
                shell=False,
            )
        except BaseException as error:
            self._command_failed = True
            try:
                self.event_fail(operation, reason=type(error).__name__)
            except BaseException:
                pass
            raise
        started = time.monotonic()
        # 同时受单步 timeout 和 Campaign 的绝对墙钟截止约束；绝不因为
        # 子命令重试而重新起算全局预算。batched Campaign 会把执行截止提前，
        # 到点先用 SIGUSR1 请求 Python worker 展开 finally；只有清理窗口耗尽
        # 才强杀进程组。原始 Campaign deadline 从未改变。
        global_deadline = self._deadline_monotonic_ns / 1_000_000_000
        execution_deadline = min(
            started + timeout_seconds,
            global_deadline - cleanup_grace,
        )
        cleanup_deadline = min(
            global_deadline - terminal_drain,
            execution_deadline + max(0.0, cleanup_grace - terminal_drain),
        )
        interval = min(float(self.heartbeat_seconds), 1.0)
        try:
            while True:
                remaining = execution_deadline - time.monotonic()
                if remaining <= 0:
                    raise SupervisorTimeout(f"统一命令超时：{operation}")
                try:
                    stdout, stderr = process.communicate(timeout=max(0.05, min(interval, remaining)))
                    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                    if result.returncode:
                        self._command_failed = True
                        self.event_fail(
                            operation,
                            job_id=job_id,
                            reason=f"returncode={result.returncode}",
                        )
                    else:
                        self.event_end(
                            operation,
                            job_id=job_id,
                            metadata={"returncode": result.returncode},
                        )
                    return result
                except subprocess.TimeoutExpired:
                    self.heartbeat(operation, force=True)
                    if heartbeat_callback is not None:
                        heartbeat_callback(operation)
        except BaseException as error:
            if (
                isinstance(error, SupervisorTimeout)
                and cleanup_grace > 0
                and process.poll() is None
            ):
                _request_process_cleanup(process)
                cleanup_operation = f"{operation}:cleanup"
                while process.poll() is None and time.monotonic() < cleanup_deadline:
                    remaining_cleanup = cleanup_deadline - time.monotonic()
                    try:
                        process.wait(
                            timeout=max(
                                0.01,
                                min(interval, remaining_cleanup),
                            )
                        )
                    except subprocess.TimeoutExpired:
                        self.heartbeat(cleanup_operation, job_id=job_id, force=True)
                        if heartbeat_callback is not None:
                            heartbeat_callback(cleanup_operation)
            _terminate_process_group(process)
            try:
                self.event_fail(
                    operation,
                    job_id=job_id,
                    reason=(
                        "cleanup-window-expired"
                        if isinstance(error, SupervisorTimeout)
                        and cleanup_grace > 0
                        and process.poll() is None
                        else "cleanup-requested-timeout"
                        if isinstance(error, SupervisorTimeout)
                        and cleanup_grace > 0
                        else type(error).__name__
                    ),
                )
            except BaseException:
                pass
            raise

    def stop(self, *, reason: str = "normal-completion", status: str = "stopped") -> None:
        if self._stop_completed:
            return
        if self._attached:
            # 子进程只负责结束自己的 lease；父 run 的终态必须由 campaign-run
            # 在全部动作完成后统一请求，避免子进程提前封存父账本。
            self._stop_completed = True
            self._started = False
            return
        run_dir = self._require_started()
        reason = _note(reason, "stop reason")
        if status not in STOP_STATUSES:
            raise SupervisorError("监督器 stop status 非法。")
        # monitor 已经退出但没有写终态时，先封存审计缺口；不能把它当成
        # 正常停止再覆盖为 stopped。
        if self.process is not None and self.process.poll() is not None:
            if self._mark_monitor_exit():
                self._stop_completed = True
                return
        request_path = run_dir / "stop-request.json"
        request = {
            "schema_version": STOP_SCHEMA,
            "requested_at_utc": _utc_now(),
            "requested_at_epoch": time.time(),
            "reason": reason,
            "status": status,
            "owner_pid": self.owner_pid,
            "owner_nonce": self.owner_nonce,
        }
        if request_path.exists():
            existing = _read_json(request_path)
            if (
                existing.get("owner_pid") != self.owner_pid
                or existing.get("owner_nonce") != self.owner_nonce
            ):
                raise SupervisorError("已有 stop-request 属于其他 owner。")
        else:
            _write_json(request_path, request, replace=False)

        # 轮询只等待一个很小的上限；monitor 自身会先把 stop 事实 fsync，
        # 再退出。若它卡住，立即留下 audit-incomplete 并终止 monitor，
        # 不能让 release 在这里阻塞数分钟。
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=DEFAULT_STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    self._event(
                        "audit-incomplete",
                        "supervisor:stop",
                        status="audit-incomplete",
                        reason="supervisor-stop-timeout",
                    )
                except BaseException:
                    pass
                _set_terminal_state(
                    run_dir, state="audit-incomplete", terminal_at_epoch=time.time()
                )
                _terminate_process_group(self.process)

        if self.process is not None and self.process.poll() is not None:
            self._mark_monitor_exit()

        # 只有 state 仍是 running 时才写 requested status；若 monitor 已经
        # 判定 watchdog-aborted/audit-incomplete，普通 release 不得覆盖它。
        try:
            _set_terminal_state(
                run_dir,
                state=status,
                terminal_at_epoch=time.time(),
            )
        finally:
            self._stop_completed = True

    def mark_audit_incomplete(self, reason: str) -> None:
        self._event(
            "audit-incomplete",
            "supervisor:audit",
            status="audit-incomplete",
            reason=reason,
        )

    def status(self) -> dict[str, Any]:
        run_dir = self._require_started()
        state = _read_state(run_dir)
        heartbeat = _read_json(run_dir / "heartbeat.json")
        now_monotonic_ns = time.monotonic_ns()
        heartbeat_monotonic_ns = _parse_monotonic_ns(
            heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns"
        )
        heartbeat_age = (
            now_monotonic_ns - heartbeat_monotonic_ns
        ) / 1_000_000_000
        state.update(
            {
                "run_dir": str(run_dir),
                "heartbeat_age_seconds": round(max(0.0, heartbeat_age), 3),
                "owner_alive": _owner_alive(self.owner_pid),
                "monitor_alive": (
                    (
                        self.process is not None and self.process.poll() is None
                    )
                    if not self._attached
                    else _owner_alive(int(state.get("monitor_pid", -1)))
                ),
                "events_path": str(run_dir / "events.ndjson"),
                "minute_ledger_path": str(run_dir / "minute-ledger.ndjson"),
            }
        )
        return state

    def __enter__(self) -> "SupervisorClient":
        return self.start()

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if not self.started:
            return False
        if self._attached:
            self._started = False
            self._stop_completed = True
            return False
        try:
            if exc is not None:
                try:
                    self._event(
                        "owner-failed",
                        "supervisor:owner",
                        status="failed",
                        reason=type(exc).__name__,
                    )
                except BaseException:
                    pass
                # owner 异常已经由上面的事件完整记录，属于业务失败；只有
                # 监督器失联、链损坏或覆盖缺口才是 audit-incomplete。
                self.stop(reason="owner-exception", status="failed")
            else:
                self.stop(
                    reason=(
                        "command-failed" if self._command_failed else "normal-completion"
                    ),
                    status="failed" if self._command_failed else "stopped",
                )
        finally:
            self._started = False
        return False


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=1.0)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _status_command(state_dir: Path) -> dict[str, Any]:
    run_dir = _validate_state_dir(state_dir, create=False)
    state = _read_state(run_dir)
    heartbeat = _read_json(run_dir / "heartbeat.json")
    now_monotonic_ns = time.monotonic_ns()
    heartbeat_monotonic_ns = _parse_monotonic_ns(
        heartbeat.get("monotonic_ns"), "heartbeat.monotonic_ns"
    )
    state.update(
        {
            "run_dir": str(run_dir),
            "heartbeat_age_seconds": round(
                max(
                    0.0,
                    (now_monotonic_ns - heartbeat_monotonic_ns)
                    / 1_000_000_000,
                ),
                3,
            ),
            "owner_alive": _owner_alive(int(state.get("owner_pid", -1))),
            "monitor_alive": _owner_alive(int(state.get("monitor_pid", -1))),
            "events_path": str(run_dir / "events.ndjson"),
            "minute_ledger_path": str(run_dir / "minute-ledger.ndjson"),
        }
    )
    return state


def _audit_command(
    state_dir: Path,
    *,
    _seen: set[Path] | None = None,
) -> dict[str, Any]:
    run_dir = _validate_state_dir(state_dir, create=False)
    seen = set() if _seen is None else set(_seen)
    if run_dir in seen:
        raise SupervisorError("父监督器续接链形成循环。")
    seen.add(run_dir)
    state = _read_state(run_dir)
    errors: list[str] = []

    def load_lines(
        path: Path, label: str, *, required: bool = True
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        if not path.exists():
            if required:
                errors.append(f"{label}缺失")
            return values
        try:
            _validate_file(path)
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise SupervisorError(f"{label}第 {line_number} 行不是对象")
                values.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError, SupervisorError) as error:
            errors.append(str(error))
        return values

    events = load_lines(run_dir / "events.ndjson", "事件账本")
    previous_digest: str | None = None
    previous_recorded = 0.0
    for expected_sequence, event in enumerate(events, 1):
        if event.get("sequence") != expected_sequence:
            errors.append("事件序号不连续")
            break
        unsigned = dict(event)
        digest = unsigned.pop("event_sha256", None)
        if not isinstance(digest, str) or _sha256(_canonical(unsigned)) != digest:
            errors.append(f"事件 {expected_sequence} 摘要不一致")
        if event.get("previous_event_sha256") != previous_digest:
            errors.append(f"事件 {expected_sequence} 摘要链断裂")
        recorded = event.get("recorded_at_epoch")
        if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
            errors.append(f"事件 {expected_sequence} 时间非法")
        elif float(recorded) < previous_recorded:
            errors.append("事件时间倒退")
        else:
            previous_recorded = float(recorded)
        previous_digest = digest if isinstance(digest, str) else None

    watchdog_duration = max(
        0.0,
        float(state.get("terminal_at_epoch", time.time()))
        - float(state["started_at_epoch"]),
    )
    watchdog_records = load_lines(
        run_dir / "watchdog-heartbeats.ndjson",
        "监督器心跳",
        required=watchdog_duration
        >= float(state.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)) * 1.25,
    )
    watchdog_required_fields = {
        "schema_version",
        "recorded_at_utc",
        "recorded_at_epoch",
        "recorded_monotonic_ns",
        "status",
        "owner_alive",
        "heartbeat_age_seconds",
        "operation",
        "job_id",
        "owner_heartbeat_at_utc",
        "owner_heartbeat_monotonic_ns",
        "owner_event_sequence",
    }
    previous_watchdog_monotonic = -1
    for index, record in enumerate(watchdog_records, 1):
        if set(record) != watchdog_required_fields:
            errors.append(f"监督器心跳第 {index} 行字段不闭合")
            continue
        if record.get("schema_version") != WATCHDOG_HEARTBEAT_SCHEMA:
            errors.append(f"监督器心跳第 {index} 行 Schema 不匹配")
        try:
            _parse_epoch(record.get("recorded_at_epoch"), "监督器心跳.recorded_at_epoch")
            monotonic = _parse_monotonic_ns(
                record.get("recorded_monotonic_ns"),
                "监督器心跳.recorded_monotonic_ns",
            )
            if monotonic < previous_watchdog_monotonic:
                errors.append("监督器心跳单调时间倒退")
            previous_watchdog_monotonic = monotonic
        except SupervisorError as error:
            errors.append(str(error))
        if record.get("status") not in {
            "active",
            "stopped",
            "failed",
            "audit-incomplete",
            "watchdog-aborted",
        }:
            errors.append(f"监督器心跳第 {index} 行状态非法")
        if not isinstance(record.get("owner_alive"), bool):
            errors.append(f"监督器心跳第 {index} 行 owner_alive 非法")
        age = record.get("heartbeat_age_seconds")
        if age is not None and (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(float(age))
            or float(age) < 0
        ):
            errors.append(f"监督器心跳第 {index} 行 heartbeat_age 非法")
        operation = record.get("operation")
        if operation is not None:
            try:
                _operation(operation)
            except SupervisorError as error:
                errors.append(str(error))
        job_id = record.get("job_id")
        if job_id is not None:
            try:
                _safe_id(job_id, "监督器心跳.job_id")
            except SupervisorError as error:
                errors.append(str(error))
        owner_mono = record.get("owner_heartbeat_monotonic_ns")
        if owner_mono is not None:
            try:
                _parse_monotonic_ns(owner_mono, "监督器心跳.owner_heartbeat_monotonic_ns")
            except SupervisorError as error:
                errors.append(str(error))
        sequence = record.get("owner_event_sequence")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0
        ):
            errors.append(f"监督器心跳第 {index} 行 event_sequence 非法")

    records = load_lines(run_dir / "minute-ledger.ndjson", "分钟账本")
    counts: dict[str, int] = {}
    for record in records:
        classification = str(record.get("classification", "unknown"))
        counts[classification] = counts.get(classification, 0) + 1
        if classification not in {
            "active",
            "planning",
            "orchestrator-idle",
            "waiting",
            "stopped",
            "failed",
            "audit-incomplete",
        }:
            errors.append(f"分钟账本分类非法：{classification}")

    # 逐条核对分钟账本的连续覆盖范围。终态时必须覆盖到 terminal_at_epoch；
    # 运行中允许尚未封存的当前部分桶，但已写区间之间不得出现空洞或重叠。
    coverage_start: float | None = None
    coverage_end: float | None = None
    last_end: float | None = None
    for index, record in enumerate(records, 1):
        start = record.get("bucket_start_epoch")
        end = record.get("bucket_end_epoch")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or not isinstance(end, (int, float)) or isinstance(end, bool) or float(end) <= float(start):
            errors.append(f"分钟账本第 {index} 行区间非法")
            continue
        start_f, end_f = float(start), float(end)
        if coverage_start is None:
            coverage_start = start_f
        if last_end is not None and abs(start_f - last_end) > 0.01:
            errors.append(f"分钟账本第 {index} 行存在时间缺口或重叠")
        last_end = end_f
        coverage_end = end_f
    started_epoch = float(state["started_at_epoch"])
    if coverage_start is not None and abs(coverage_start - started_epoch) > 0.01:
        errors.append("分钟账本未从监督器启动时刻开始")
    state_name = str(state.get("state"))
    if state_name in TERMINAL_STATES:
        terminal_epoch = state.get("terminal_at_epoch")
        if not isinstance(terminal_epoch, (int, float)) or isinstance(terminal_epoch, bool):
            errors.append("终态缺少 terminal_at_epoch")
        elif coverage_end is None or coverage_end + 0.01 < float(terminal_epoch):
            errors.append("终态分钟账本存在未分类时间段")

    continuity_report: dict[str, Any] | None = None
    continuity_path = run_dir / "continuity.json"
    predecessor_value = state.get("predecessor_run_dir")
    if continuity_path.exists() or predecessor_value is not None:
        try:
            continuity = _read_json(continuity_path)
            expected_fields = {
                "schema_version",
                "campaign_id",
                "predecessor_run_dir",
                "predecessor_state",
                "predecessor_state_sha256",
                "predecessor_terminal_at_epoch",
                "resumed_at_epoch",
                "gap_seconds",
                "campaign_started_at_epoch",
                "deadline_at_epoch",
            }
            if set(continuity) != expected_fields:
                raise SupervisorError("父监督器续接收据字段不闭合。")
            predecessor_dir = _validate_state_dir(
                Path(str(continuity.get("predecessor_run_dir"))),
                create=False,
            )
            predecessor_state_path = predecessor_dir / "state.json"
            predecessor_digest = _sha256(predecessor_state_path.read_bytes())
            predecessor_state = _read_state(predecessor_dir)
            gap_seconds = float(continuity.get("gap_seconds", -1))
            if (
                continuity.get("schema_version") != CAMPAIGN_CONTINUITY_SCHEMA
                or continuity.get("campaign_id") != state.get("campaign_id")
                or continuity.get("predecessor_run_dir") != predecessor_value
                or continuity.get("predecessor_state_sha256")
                != predecessor_digest
                or state.get("predecessor_state_sha256") != predecessor_digest
                or predecessor_state.get("campaign_id") != state.get("campaign_id")
                or predecessor_state.get("state") not in TERMINAL_STATES
                or continuity.get("predecessor_state")
                != predecessor_state.get("state")
                or float(continuity.get("predecessor_terminal_at_epoch", -1))
                != float(predecessor_state.get("terminal_at_epoch", -2))
                or float(continuity.get("deadline_at_epoch", -1))
                != float(state.get("deadline_at_epoch", -2))
                or float(predecessor_state.get("deadline_at_epoch", -1))
                != float(state.get("deadline_at_epoch", -2))
                or float(continuity.get("campaign_started_at_epoch", -1))
                != float(state.get("campaign_started_at_epoch", -2))
                or gap_seconds < 0
            ):
                raise SupervisorError("父监督器续接身份或 deadline 漂移。")
            predecessor_audit = _audit_command(predecessor_dir, _seen=seen)
            continuity_report = {
                "predecessor_run_dir": str(predecessor_dir),
                "gap_seconds": gap_seconds,
                "gap_classification": (
                    "continuous-handoff"
                    if gap_seconds <= 0.01
                    else "audit-incomplete"
                ),
                "predecessor_audit_incomplete": bool(
                    predecessor_audit.get("audit_incomplete")
                ),
            }
            if gap_seconds > 0.01:
                errors.append(
                    f"父监督器续接存在 {gap_seconds:.3f} 秒未受监管区间"
                )
            if predecessor_audit.get("audit_incomplete"):
                errors.append("前序父监督器审计不完整")
        except (OSError, TypeError, ValueError, SupervisorError) as error:
            errors.append(f"父监督器续接收据非法：{error}")

    incomplete = bool(errors) or counts.get("audit-incomplete", 0) > 0
    return {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "event_count": len(events),
        "watchdog_heartbeat_count": len(watchdog_records),
        "minute_record_count": len(records),
        "classification_counts": dict(sorted(counts.items())),
        "coverage_start_epoch": coverage_start,
        "coverage_end_epoch": coverage_end,
        "audit_incomplete": incomplete,
        "integrity_errors": errors,
        "state": state_name,
        "continuity": continuity_report,
    }


def _campaign_activity(
    run_dir: Path,
    *,
    classification: str,
    operation: str,
    timeout_seconds: float,
    job_id: str | None,
    revision: int,
) -> dict[str, Any]:
    """生成父监督器消费的单调活动分类。"""

    if classification not in CAMPAIGN_CLASSIFICATIONS:
        raise SupervisorError("Campaign 活动分类非法。")
    timeout = _positive_seconds(timeout_seconds, "Campaign 活动 timeout")
    if classification == "orchestrator-idle" and timeout > DEFAULT_ORCHESTRATOR_DISPATCH_TIMEOUT_SECONDS:
        raise SupervisorError(
            "orchestrator-idle 最长只能登记 "
            f"{DEFAULT_ORCHESTRATOR_DISPATCH_TIMEOUT_SECONDS} 秒；"
            "没有待执行动作时必须请求终态。"
        )
    operation = _operation(operation)
    encoded_operation = _operation(f"{classification}:{operation}")
    if job_id is not None:
        job_id = _safe_id(job_id, "job_id")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise SupervisorError("Campaign 活动 revision 非法。")
    now = time.time()
    now_monotonic_ns = time.monotonic_ns()
    return {
        "schema_version": CAMPAIGN_ACTIVITY_SCHEMA,
        "revision": revision,
        "classification": classification,
        "operation": operation,
        "encoded_operation": encoded_operation,
        "job_id": job_id,
        "started_at_utc": _epoch_to_utc(now),
        "started_at_epoch": now,
        "started_monotonic_ns": now_monotonic_ns,
        "deadline_at_utc": _epoch_to_utc(now + timeout),
        "deadline_at_epoch": now + timeout,
        "deadline_monotonic_ns": now_monotonic_ns
        + max(1, int(math.ceil(timeout * 1_000_000_000))),
        "requested_by_pid": os.getpid(),
    }


def _campaign_action_fields(
    *,
    action_id: str,
    worker_pid: int,
    worker_started_at_epoch: float,
    worker_started_monotonic_ns: int,
    accepted_returncodes: Iterable[int] = (0,),
) -> dict[str, Any]:
    """建立不含命令参数和环境值的真实工作进程身份。"""

    if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
        raise SupervisorError("Campaign worker_pid 非法。")
    started_epoch = _parse_epoch(worker_started_at_epoch, "worker_started_at_epoch")
    started_monotonic = _parse_monotonic_ns(
        worker_started_monotonic_ns,
        "worker_started_monotonic_ns",
    )
    accepted = sorted(set(accepted_returncodes) | {0})
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 255
        for value in accepted
    ):
        raise SupervisorError("Campaign 允许退出码必须是 0～255 的整数。")
    return {
        "action_id": _safe_id(action_id, "action_id", maximum=128),
        "worker_pid": worker_pid,
        "worker_started_at_epoch": started_epoch,
        "worker_started_monotonic_ns": started_monotonic,
        "worker_heartbeat_at_epoch": started_epoch,
        "worker_heartbeat_monotonic_ns": started_monotonic,
        "command_pid": None,
        "action_status": "running",
        "returncode": None,
        "reason": None,
        "ended_at_epoch": None,
        "ended_at_utc": None,
        "accepted_returncodes": accepted,
    }


def _read_campaign_activity(run_dir: Path) -> dict[str, Any]:
    """读取并闭合校验父监督器活动文件。"""

    activity = _read_json(run_dir / "campaign-activity.json")
    expected = {
        "schema_version",
        "revision",
        "classification",
        "operation",
        "encoded_operation",
        "job_id",
        "started_at_utc",
        "started_at_epoch",
        "started_monotonic_ns",
        "deadline_at_utc",
        "deadline_at_epoch",
        "deadline_monotonic_ns",
        "requested_by_pid",
    }
    actual_fields = set(activity)
    if (
        not expected.issubset(actual_fields)
        or actual_fields
        - expected
        - CAMPAIGN_ACTION_FIELDS
        - CAMPAIGN_ACTION_OPTIONAL_FIELDS
        or activity.get("schema_version") != CAMPAIGN_ACTIVITY_SCHEMA
    ):
        raise SupervisorError("Campaign 活动文件字段不闭合或 Schema 不匹配。")
    classification = activity.get("classification")
    if classification not in CAMPAIGN_CLASSIFICATIONS:
        raise SupervisorError("Campaign 活动分类非法。")
    operation = _operation(activity.get("operation"))
    if activity.get("encoded_operation") != f"{classification}:{operation}":
        raise SupervisorError("Campaign 活动操作标签不一致。")
    revision = activity.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise SupervisorError("Campaign 活动 revision 非法。")
    job_id = activity.get("job_id")
    if job_id is not None:
        _safe_id(job_id, "Campaign 活动 job_id")
    for field in ("started_at_epoch", "deadline_at_epoch"):
        _parse_epoch(activity.get(field), f"Campaign 活动 {field}")
    started_monotonic = _parse_monotonic_ns(
        activity.get("started_monotonic_ns"),
        "Campaign 活动 started_monotonic_ns",
    )
    deadline_monotonic = _parse_monotonic_ns(
        activity.get("deadline_monotonic_ns"),
        "Campaign 活动 deadline_monotonic_ns",
    )
    if deadline_monotonic <= started_monotonic:
        raise SupervisorError("Campaign 活动 deadline 非法。")
    requested_by = activity.get("requested_by_pid")
    if (
        isinstance(requested_by, bool)
        or not isinstance(requested_by, int)
        or requested_by <= 0
    ):
        raise SupervisorError("Campaign 活动 requested_by_pid 非法。")
    action_fields = actual_fields & CAMPAIGN_ACTION_FIELDS
    if action_fields:
        if action_fields != CAMPAIGN_ACTION_FIELDS or classification != "active":
            raise SupervisorError("Campaign worker 字段不闭合或出现在非 active 活动中。")
        _safe_id(activity.get("action_id"), "Campaign action_id", maximum=128)
        worker_pid = activity.get("worker_pid")
        if (
            isinstance(worker_pid, bool)
            or not isinstance(worker_pid, int)
            or worker_pid <= 0
        ):
            raise SupervisorError("Campaign worker_pid 非法。")
        _parse_epoch(
            activity.get("worker_started_at_epoch"),
            "Campaign worker_started_at_epoch",
        )
        worker_started = _parse_monotonic_ns(
            activity.get("worker_started_monotonic_ns"),
            "Campaign worker_started_monotonic_ns",
        )
        _parse_epoch(
            activity.get("worker_heartbeat_at_epoch"),
            "Campaign worker_heartbeat_at_epoch",
        )
        worker_heartbeat = _parse_monotonic_ns(
            activity.get("worker_heartbeat_monotonic_ns"),
            "Campaign worker_heartbeat_monotonic_ns",
        )
        if worker_heartbeat < worker_started:
            raise SupervisorError("Campaign worker 心跳早于启动时间。")
        command_pid = activity.get("command_pid")
        if command_pid is not None and (
            isinstance(command_pid, bool)
            or not isinstance(command_pid, int)
            or command_pid <= 0
        ):
            raise SupervisorError("Campaign command_pid 非法。")
        action_status = activity.get("action_status")
        if action_status not in {"running", "passed", "failed"}:
            raise SupervisorError("Campaign action_status 非法。")
        returncode = activity.get("returncode")
        if returncode is not None and (
            isinstance(returncode, bool) or not isinstance(returncode, int)
        ):
            raise SupervisorError("Campaign returncode 非法。")
        accepted_returncodes = activity.get("accepted_returncodes", [0])
        if (
            not isinstance(accepted_returncodes, list)
            or not accepted_returncodes
            or accepted_returncodes != sorted(set(accepted_returncodes))
            or 0 not in accepted_returncodes
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 255
                for value in accepted_returncodes
            )
        ):
            raise SupervisorError("Campaign 允许退出码非法。")
        reason = activity.get("reason")
        if reason is not None:
            _note(reason, "Campaign action reason")
        ended_epoch = activity.get("ended_at_epoch")
        ended_utc = activity.get("ended_at_utc")
        if action_status == "running":
            if returncode is not None or reason is not None or ended_epoch is not None or ended_utc is not None:
                raise SupervisorError("运行中的 Campaign action 含终态字段。")
        else:
            _parse_epoch(ended_epoch, "Campaign ended_at_epoch")
            if not isinstance(ended_utc, str) or not ended_utc:
                raise SupervisorError("Campaign ended_at_utc 非法。")
            if action_status == "passed" and returncode not in accepted_returncodes:
                raise SupervisorError("通过的 Campaign action 退出码不在允许集合中。")
            if (
                action_status == "passed"
                and returncode != 0
                and reason != f"accepted-returncode={returncode}"
            ):
                raise SupervisorError("预期负结果缺少明确的允许退出码原因。")
            if action_status == "failed" and reason is None:
                raise SupervisorError("失败的 Campaign action 缺少原因。")
    return activity


def _campaign_mark_command(
    run_dir: Path,
    *,
    classification: str,
    operation: str,
    timeout_seconds: float,
    job_id: str | None,
) -> dict[str, Any]:
    """原子切换父监督器分类，并等待 owner 确认心跳。"""

    run_dir = _validate_state_dir(run_dir, create=False)
    if classification == "active":
        raise SupervisorError("active 只能由 campaign-exec 绑定真实命令。")
    if classification == "orchestrator-idle":
        raise SupervisorError(
            "新流程禁止登记 orchestrator-idle；请登记 planning:dispatch-next-action "
            "或直接请求 campaign-stop。"
        )
    state = _read_state(run_dir)
    if state.get("state") != "running":
        raise SupervisorError("Campaign 父监督器已经停线或结束。")
    if not _owner_alive(int(state.get("owner_pid", -1))) or not _owner_alive(
        int(state.get("monitor_pid", -1))
    ):
        raise SupervisorError("Campaign 父监督器 owner 或 monitor 不在线。")
    path = run_dir / "campaign-activity.json"
    current = _read_campaign_activity(run_dir)
    payload = _campaign_activity(
        run_dir,
        classification=classification,
        operation=operation,
        timeout_seconds=timeout_seconds,
        job_id=job_id,
        revision=int(current["revision"]) + 1,
    )
    _write_json(path, payload, replace=True)
    deadline = time.monotonic() + min(2.0, float(state["watchdog_timeout_seconds"]))
    while time.monotonic() < deadline:
        heartbeat = _read_latest_heartbeat(
            run_dir,
            owner_pid=int(state["owner_pid"]),
            owner_nonce=str(state["owner_nonce"]),
        )
        if heartbeat.get("operation") == payload["encoded_operation"]:
            return payload
        if _campaign_mark_consumed_by_stop(run_dir, payload):
            return payload
        time.sleep(0.05)
    if _campaign_mark_consumed_by_stop(run_dir, payload):
        return payload
    raise SupervisorError("Campaign 父监督器未及时确认活动切换。")


def _campaign_mark_consumed_by_stop(run_dir: Path, payload: Mapping[str, Any]) -> bool:
    """父监督器已把本次登记的活动按到期停线时，视为切换已被确认。

    ``campaign-mark`` 允许登记很短的派发超时。父编排器可能在心跳回显新
    operation 之前就检测到该活动到期并停线；这时活动切换事实上已被消费，
    不能再报「未及时确认」，否则同一事实会留下两种互相矛盾的终态。
    只有停线原因确属活动到期、且当前活动 revision 就是本次登记时才成立。
    """

    request_path = run_dir / "stop-request.json"
    if request_path.is_symlink() or not request_path.is_file():
        return False
    try:
        request = _read_json(request_path)
        latest = _read_campaign_activity(run_dir)
    except SupervisorError:
        return False
    try:
        if int(latest.get("revision", -1)) != int(payload["revision"]):
            return False
    except (TypeError, ValueError):
        return False
    reason = str(request.get("reason") or "")
    return (
        reason.startswith("orchestrator-dispatch-timeout")
        or reason == "campaign-activity-deadline-expired"
    )


def _campaign_archive_action(run_dir: Path, activity: Mapping[str, Any]) -> Path:
    """保留每个动作的不可覆盖终态，供强停后审计。"""

    action_id = _safe_id(activity.get("action_id"), "action_id", maximum=128)
    archive_dir = run_dir / "campaign-actions"
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise SupervisorError("Campaign action 归档目录不可信。")
    path = archive_dir / f"{action_id}.json"
    if not path.exists():
        _write_json(path, dict(activity), replace=False)
    return path


def _campaign_failed_action_count(run_dir: Path, encoded_operation: str) -> int:
    """统计同一操作已封存的失败次数；损坏记录直接失败关闭。"""

    archive_dir = run_dir / "campaign-actions"
    if not archive_dir.exists():
        return 0
    if archive_dir.is_symlink() or not archive_dir.is_dir():
        raise SupervisorError("Campaign action 归档目录不可信。")
    count = 0
    for path in sorted(archive_dir.glob("*.json"), key=lambda value: value.name):
        action = _read_json(path)
        if (
            action.get("encoded_operation") == encoded_operation
            and action.get("action_status") == "failed"
        ):
            count += 1
    return count


def _campaign_update_action(
    run_dir: Path,
    *,
    action_id: str,
    updates: Mapping[str, Any],
    archive: bool = False,
) -> dict[str, Any]:
    """只更新当前动作；分类已切换时拒绝追写。"""

    action_id = _safe_id(action_id, "action_id", maximum=128)
    with _state_lock(run_dir):
        current = _read_campaign_activity(run_dir)
        if current.get("classification") != "active" or current.get("action_id") != action_id:
            raise SupervisorError("Campaign action 已切换或身份漂移。")
        current.update(dict(updates))
        _write_json(run_dir / "campaign-activity.json", current, replace=True)
        # 在锁内完成运行时闭合校验；返回后父监督器可以立即切换为 idle，
        # 不能再次读取 current 并把这次正常并发误报为身份漂移。
        validated = _read_campaign_activity(run_dir)
    if archive:
        _campaign_archive_action(run_dir, validated)
    return validated


def _campaign_begin_action(
    run_dir: Path,
    *,
    operation: str,
    timeout_seconds: float,
    job_id: str | None,
    accepted_returncodes: Iterable[int] = (0,),
) -> dict[str, Any]:
    """在真实命令启动前原子绑定执行包装器。"""

    run_dir = _validate_state_dir(run_dir, create=False)
    state = _read_state(run_dir)
    if state.get("state") != "running":
        raise SupervisorError("Campaign 父监督器已经停线或结束。")
    requested_timeout = _positive_seconds(timeout_seconds, "Campaign action timeout")
    drain_seconds = max(
        float(state["watchdog_timeout_seconds"]),
        float(state["heartbeat_seconds"]) * 2,
    )
    remaining_seconds = float(state["deadline_at_epoch"]) - time.time()
    if requested_timeout + drain_seconds > remaining_seconds:
        raise SupervisorError(
            "Campaign 父监督器剩余预算不足以容纳动作和终态排空窗口。"
        )
    worker_started_epoch = time.time()
    worker_started_monotonic_ns = time.monotonic_ns()
    action_id = secrets.token_hex(16)
    with _state_lock(run_dir):
        current = _read_campaign_activity(run_dir)
        if current.get("classification") == "active":
            raise SupervisorError("Campaign 已有 active 动作。")
        payload = _campaign_activity(
            run_dir,
            classification="active",
            operation=operation,
            timeout_seconds=requested_timeout,
            job_id=job_id,
            revision=int(current["revision"]) + 1,
        )
        payload.update(
            _campaign_action_fields(
                action_id=action_id,
                worker_pid=os.getpid(),
                worker_started_at_epoch=worker_started_epoch,
                worker_started_monotonic_ns=worker_started_monotonic_ns,
                accepted_returncodes=accepted_returncodes,
            )
        )
        _write_json(run_dir / "campaign-activity.json", payload, replace=True)
    deadline = time.monotonic() + min(2.0, float(state["watchdog_timeout_seconds"]))
    while time.monotonic() < deadline:
        heartbeat = _read_latest_heartbeat(
            run_dir,
            owner_pid=int(state["owner_pid"]),
            owner_nonce=str(state["owner_nonce"]),
        )
        if heartbeat.get("operation") == payload["encoded_operation"]:
            return payload
        time.sleep(0.05)
    raise SupervisorError("Campaign 父监督器未确认真实命令。")


def _orchestrator_dispatch_timeout_seconds(client: "SupervisorClient") -> float:
    """为测试和生产统一计算短派发窗口，不得退化为长时间 idle。"""

    # 测试可使用更短的 worker watchdog；生产默认固定为 15 秒上限。
    return min(
        DEFAULT_ORCHESTRATOR_DISPATCH_TIMEOUT_SECONDS,
        max(1.0, float(client.watchdog_timeout_seconds) * 2),
    )


def _terminate_process_group_pid(pid: int | None) -> None:
    """只终止 campaign-exec 已登记的独立命令进程组。"""

    if pid is None or pid <= 0 or pid == os.getpid() or not _owner_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and _owner_alive(pid):
        time.sleep(0.05)
    if _owner_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _campaign_exec_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """在父监督器下执行一条真实命令并自动封存终态。"""

    command = list(args.command_argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or any(not item for item in command):
        raise SupervisorError("campaign-exec 缺少待执行命令。")
    accepted_returncodes = sorted(
        {0, *(getattr(args, "accept_returncode", None) or [])}
    )
    if any(value < 0 or value > 255 for value in accepted_returncodes):
        raise SupervisorError("--accept-returncode 必须位于 0～255。")
    run_dir = _validate_state_dir(args.state_dir, create=False)
    activity = _campaign_begin_action(
        run_dir,
        operation=args.operation,
        timeout_seconds=args.timeout_seconds,
        job_id=args.job_id,
        accepted_returncodes=accepted_returncodes,
    )
    action_id = str(activity["action_id"])
    output_path: Path | None = None
    output_stream: Any | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        if args.persist_output:
            archive_dir = run_dir / "campaign-actions"
            archive_dir.mkdir(mode=0o700, exist_ok=True)
            output_path = archive_dir / f"{action_id}.log"
            descriptor = os.open(
                output_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            output_stream = os.fdopen(descriptor, "wb")
        process = subprocess.Popen(
            [str(item) for item in command],
            stdin=subprocess.DEVNULL,
            stdout=output_stream if output_stream is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            shell=False,
        )
        activity = _campaign_update_action(
            run_dir,
            action_id=action_id,
            updates={"command_pid": process.pid},
        )
        deadline_monotonic_ns = int(activity["deadline_monotonic_ns"])
        heartbeat_interval = min(1.0, float(_read_state(run_dir)["heartbeat_seconds"]))
        while True:
            remaining = (deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                _terminate_process_group(process)
                ended = time.time()
                _campaign_update_action(
                    run_dir,
                    action_id=action_id,
                    updates={
                        "action_status": "failed",
                        "returncode": 124,
                        "reason": "command-timeout",
                        "ended_at_epoch": ended,
                        "ended_at_utc": _epoch_to_utc(ended),
                    },
                    archive=True,
                )
                return 124, {
                    "status": "failed",
                    "action_id": action_id,
                    "returncode": 124,
                    "output_log": str(output_path) if output_path is not None else None,
                }
            try:
                returncode = process.wait(timeout=max(0.05, min(heartbeat_interval, remaining)))
            except subprocess.TimeoutExpired:
                state = _read_state(run_dir)
                if state.get("state") != "running":
                    _terminate_process_group(process)
                    raise SupervisorError("Campaign 父监督器已停线。")
                now = time.time()
                _campaign_update_action(
                    run_dir,
                    action_id=action_id,
                    updates={
                        "worker_heartbeat_at_epoch": now,
                        "worker_heartbeat_monotonic_ns": time.monotonic_ns(),
                    },
                )
                continue
            ended = time.time()
            passed = returncode in accepted_returncodes
            _campaign_update_action(
                run_dir,
                action_id=action_id,
                updates={
                    "worker_heartbeat_at_epoch": ended,
                    "worker_heartbeat_monotonic_ns": time.monotonic_ns(),
                    "action_status": "passed" if passed else "failed",
                    "returncode": returncode,
                    "reason": (
                        None
                        if returncode == 0
                        else (
                            f"accepted-returncode={returncode}"
                            if passed
                            else f"returncode={returncode}"
                        )
                    ),
                    "ended_at_epoch": ended,
                    "ended_at_utc": _epoch_to_utc(ended),
                },
                archive=True,
            )
            return (0 if passed else returncode), {
                "status": "passed" if passed else "failed",
                "action_id": action_id,
                "returncode": returncode,
                "accepted_returncodes": accepted_returncodes,
                "output_log": str(output_path) if output_path is not None else None,
            }
    except BaseException as error:
        if process is not None:
            _terminate_process_group(process)
        try:
            ended = time.time()
            _campaign_update_action(
                run_dir,
                action_id=action_id,
                updates={
                    "worker_heartbeat_at_epoch": ended,
                    "worker_heartbeat_monotonic_ns": time.monotonic_ns(),
                    "action_status": "failed",
                    "returncode": 1,
                    "reason": f"worker-{type(error).__name__}",
                    "ended_at_epoch": ended,
                    "ended_at_utc": _epoch_to_utc(ended),
                },
                archive=True,
            )
        except BaseException:
            pass
        raise
    finally:
        if output_stream is not None:
            output_stream.flush()
            os.fsync(output_stream.fileno())
            output_stream.close()


def _campaign_owner(args: argparse.Namespace) -> int:
    """独立驻留的 Campaign owner；命令间继续心跳和分钟分类。"""

    deadline_at_epoch = _parse_epoch(args.deadline_at_epoch, "deadline_at_epoch")
    client = SupervisorClient(
        Path(args.state_dir),
        campaign_id=args.campaign_id,
        phase=args.phase,
        deadline_at_epoch=deadline_at_epoch,
        owner_pid=os.getpid(),
        owner_nonce=args.owner_nonce,
        heartbeat_seconds=args.heartbeat_seconds,
        watchdog_timeout_seconds=args.watchdog_timeout_seconds,
        ledger_interval_seconds=args.ledger_interval_seconds,
        terminate_owner=False,
    )
    current_operation: str | None = None
    current_job_id: str | None = None
    current_revision = 0
    try:
        client.start()
        run_dir = client._require_started()
        if args.predecessor_run_dir is not None:
            predecessor_dir = _validate_state_dir(
                Path(args.predecessor_run_dir), create=False
            )
            predecessor_state = _read_state(predecessor_dir)
            if (
                predecessor_state.get("campaign_id") != args.campaign_id
                or predecessor_state.get("state") not in TERMINAL_STATES
                or float(predecessor_state["deadline_at_epoch"])
                != deadline_at_epoch
            ):
                raise SupervisorError("前序父监督器身份、终态或 deadline 漂移。")
            terminal_at = _parse_epoch(
                predecessor_state.get("terminal_at_epoch"),
                "predecessor.terminal_at_epoch",
            )
            resumed_at = time.time()
            predecessor_state_path = predecessor_dir / "state.json"
            predecessor_digest = _sha256(predecessor_state_path.read_bytes())
            campaign_started_at = float(
                predecessor_state.get(
                    "campaign_started_at_epoch",
                    predecessor_state["started_at_epoch"],
                )
            )
            continuity = {
                "schema_version": CAMPAIGN_CONTINUITY_SCHEMA,
                "campaign_id": args.campaign_id,
                "predecessor_run_dir": str(predecessor_dir),
                "predecessor_state": str(predecessor_state["state"]),
                "predecessor_state_sha256": predecessor_digest,
                "predecessor_terminal_at_epoch": terminal_at,
                "resumed_at_epoch": resumed_at,
                "gap_seconds": round(max(0.0, resumed_at - terminal_at), 6),
                "campaign_started_at_epoch": campaign_started_at,
                "deadline_at_epoch": deadline_at_epoch,
            }
            _write_json(run_dir / "continuity.json", continuity, replace=False)
            _write_state(
                run_dir,
                {
                    "campaign_started_at_epoch": campaign_started_at,
                    "predecessor_run_dir": str(predecessor_dir),
                    "predecessor_state_sha256": predecessor_digest,
                },
            )
        initial = _campaign_activity(
            run_dir,
            classification="planning",
            operation=args.initial_operation,
            timeout_seconds=args.initial_timeout_seconds,
            job_id=None,
            revision=1,
        )
        _write_json(run_dir / "campaign-activity.json", initial, replace=False)
        while True:
            finish_path = run_dir / "campaign-finish-request.json"
            if finish_path.exists():
                finish = _read_json(finish_path)
                if (
                    set(finish)
                    != {
                        "schema_version",
                        "requested_at_utc",
                        "requested_at_epoch",
                        "reason",
                        "status",
                    }
                    or finish.get("schema_version") != CAMPAIGN_FINISH_SCHEMA
                    or finish.get("status") not in {"stopped", "failed"}
                ):
                    raise SupervisorError("Campaign 结束请求非法。")
                if current_operation is not None:
                    client.event_end(
                        current_operation,
                        job_id=current_job_id,
                        status=(
                            "passed"
                            if finish["status"] == "stopped"
                            else "failed"
                        ),
                    )
                client.stop(
                    reason=_note(finish.get("reason"), "Campaign 结束原因"),
                    status=str(finish["status"]),
                )
                return 0 if finish["status"] == "stopped" else 1

            activity = _read_campaign_activity(run_dir)
            if int(activity["revision"]) != current_revision:
                if current_operation is not None:
                    client.event_end(current_operation, job_id=current_job_id)
                current_operation = str(activity["encoded_operation"])
                current_job_id = (
                    str(activity["job_id"])
                    if activity.get("job_id") is not None
                    else None
                )
                client.event_start(current_operation, job_id=current_job_id)
                current_revision = int(activity["revision"])

            # 新 active 只能来自 campaign-exec，并且同时绑定执行包装器、真实命令
            # 进程组和独立心跳。包装器结束或失联后，不能继续等分类 deadline。
            if activity["classification"] == "active" and activity.get("action_id"):
                action_status = str(activity["action_status"])
                if action_status == "running":
                    worker_pid = int(activity["worker_pid"])
                    worker_age = (
                        time.monotonic_ns()
                        - int(activity["worker_heartbeat_monotonic_ns"])
                    ) / 1_000_000_000
                    worker_lost_reason: str | None = None
                    if not _owner_alive(worker_pid):
                        worker_lost_reason = "worker-lost"
                    elif worker_age >= float(client.watchdog_timeout_seconds):
                        worker_lost_reason = "worker-heartbeat-timeout"
                    if worker_lost_reason is not None:
                        _terminate_process_group_pid(activity.get("command_pid"))
                        _terminate_owner(worker_pid)
                        ended = time.time()
                        activity = _campaign_update_action(
                            run_dir,
                            action_id=str(activity["action_id"]),
                            updates={
                                "action_status": "failed",
                                "returncode": 1,
                                "reason": worker_lost_reason,
                                "ended_at_epoch": ended,
                                "ended_at_utc": _epoch_to_utc(ended),
                            },
                            archive=True,
                        )
                        client.event_fail(
                            current_operation,
                            job_id=current_job_id,
                            reason=worker_lost_reason,
                            metadata={"action_id": activity["action_id"]},
                        )
                        client.stop(reason=worker_lost_reason, status="failed")
                        return 2
                elif action_status == "passed":
                    client.event_end(
                        current_operation,
                        job_id=current_job_id,
                        metadata={
                            "action_id": activity["action_id"],
                            "returncode": activity["returncode"],
                        },
                    )
                    with _state_lock(run_dir):
                        latest = _read_campaign_activity(run_dir)
                        if (
                            latest.get("action_id") != activity["action_id"]
                            or latest.get("action_status") != "passed"
                        ):
                            raise SupervisorError("Campaign action 终态在切换时漂移。")
                        next_activity = _campaign_activity(
                            run_dir,
                            classification="planning",
                            operation="dispatch-next-action",
                            timeout_seconds=_orchestrator_dispatch_timeout_seconds(client),
                            job_id=None,
                            revision=int(latest["revision"]) + 1,
                        )
                        _write_json(
                            run_dir / "campaign-activity.json",
                            next_activity,
                            replace=True,
                        )
                    current_operation = None
                    current_job_id = None
                    continue
                else:
                    reason = str(activity.get("reason") or "command-failed")
                    client.event_fail(
                        current_operation,
                        job_id=current_job_id,
                        reason=reason,
                        metadata={
                            "action_id": activity["action_id"],
                            "returncode": activity["returncode"],
                        },
                    )
                    failure_count = _campaign_failed_action_count(
                        run_dir, str(activity["encoded_operation"])
                    )
                    if failure_count >= 2:
                        client.stop(
                            reason=f"repeated-{reason}", status="failed"
                        )
                        return 2
                    with _state_lock(run_dir):
                        latest = _read_campaign_activity(run_dir)
                        if (
                            latest.get("action_id") != activity["action_id"]
                            or latest.get("action_status") != "failed"
                        ):
                            raise SupervisorError(
                                "Campaign 失败 action 终态在切换时漂移。"
                            )
                        next_activity = _campaign_activity(
                            run_dir,
                            classification="planning",
                            operation="failure-diagnosis",
                            timeout_seconds=300,
                            job_id=None,
                            revision=int(latest["revision"]) + 1,
                        )
                        _write_json(
                            run_dir / "campaign-activity.json",
                            next_activity,
                            replace=True,
                        )
                    current_operation = None
                    current_job_id = None
                    continue

            if time.monotonic_ns() >= int(activity["deadline_monotonic_ns"]):
                if activity.get("action_id"):
                    _terminate_process_group_pid(activity.get("command_pid"))
                    _terminate_owner(int(activity["worker_pid"]))
                if activity.get("operation") == "dispatch-next-action":
                    reason = (
                        "orchestrator-dispatch-timeout-"
                        f"{int(round(float(activity['deadline_at_epoch']) - float(activity['started_at_epoch'])))}s"
                    )
                elif activity["classification"] == "orchestrator-idle":
                    reason = "orchestrator-idle-timeout"
                else:
                    reason = "campaign-activity-deadline-expired"
                client.event_fail(
                    str(activity["encoded_operation"]),
                    job_id=current_job_id,
                    reason=reason,
                )
                client.stop(reason=reason, status="failed")
                return 2
            client.heartbeat(
                str(activity["encoded_operation"]),
                job_id=current_job_id,
            )
            time.sleep(min(0.2, float(args.heartbeat_seconds) / 2.0))
    except BaseException as error:
        try:
            if client.started and not client._stop_completed:
                if current_operation is not None:
                    client.event_fail(
                        current_operation,
                        job_id=current_job_id,
                        reason=f"campaign-owner-{type(error).__name__}",
                    )
                client.stop(reason="campaign-owner-failed", status="failed")
        except BaseException:
            pass
        return 1


def _campaign_start_command(args: argparse.Namespace) -> dict[str, Any]:
    """启动脱离当前会话的 Campaign owner，并返回固定 run 目录。"""

    base = _validate_state_dir(Path(args.state_dir), create=True)
    campaign_id = _safe_id(args.campaign_id, "campaign_id")
    states: list[tuple[Path, dict[str, Any]]] = []
    for candidate in base.glob("run-*/state.json"):
        try:
            state = _read_state(candidate.parent)
        except SupervisorError:
            continue
        states.append((candidate.parent, state))
        if state.get("campaign_id") == campaign_id and state.get("state") == "running":
            raise SupervisorError("同一 Campaign 已有活动父监督器。")
    predecessor_dir: Path | None = None
    if args.resume_from_run_dir is not None:
        predecessor_dir = _validate_state_dir(
            Path(args.resume_from_run_dir), create=False
        )
        predecessor_state = _read_state(predecessor_dir)
        if predecessor_state.get("campaign_id") != campaign_id:
            raise SupervisorError("前序父监督器不属于同一 Campaign。")
        if predecessor_state.get("state") not in TERMINAL_STATES:
            raise SupervisorError("前序父监督器尚未进入终态。")
        if any(
            state.get("predecessor_run_dir") == str(predecessor_dir)
            for _path, state in states
        ):
            raise SupervisorError("前序父监督器已经被续接，禁止分叉。")
        deadline_at_epoch = float(predecessor_state["deadline_at_epoch"])
        if deadline_at_epoch <= time.time():
            raise SupervisorError("前序父监督器的原始全局 deadline 已到期。")
    else:
        deadline_seconds = _positive_seconds(
            args.deadline_seconds, "deadline_seconds"
        )
        deadline_at_epoch = time.time() + deadline_seconds
    nonce = secrets.token_hex(32)
    run_dir = base / f"run-{nonce}"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "campaign-owner",
        "--state-dir",
        str(base),
        "--campaign-id",
        campaign_id,
        "--phase",
        _safe_id(args.phase, "phase", maximum=32),
        "--owner-nonce",
        nonce,
        "--deadline-at-epoch",
        str(deadline_at_epoch),
        "--initial-operation",
        _operation(args.initial_operation),
        "--initial-timeout-seconds",
        str(_positive_seconds(args.initial_timeout_seconds, "initial_timeout_seconds")),
        "--heartbeat-seconds",
        str(_positive_seconds(args.heartbeat_seconds, "heartbeat_seconds")),
        "--watchdog-timeout-seconds",
        str(
            _positive_seconds(
                args.watchdog_timeout_seconds,
                "watchdog_timeout_seconds",
            )
        ),
        "--ledger-interval-seconds",
        str(
            _positive_seconds(
                args.ledger_interval_seconds,
                "ledger_interval_seconds",
            )
        ),
    ]
    if predecessor_dir is not None:
        command.extend(["--predecessor-run-dir", str(predecessor_dir)])
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
        close_fds=True,
    )
    ready_deadline = time.monotonic() + 5.0
    while time.monotonic() < ready_deadline:
        if process.poll() is not None:
            raise SupervisorError("Campaign 父监督器启动失败。")
        if (run_dir / "state.json").is_file() and (
            run_dir / "campaign-activity.json"
        ).is_file() and (
            predecessor_dir is None or (run_dir / "continuity.json").is_file()
        ):
            state = _status_command(run_dir)
            if state.get("state") == "running" and state.get("monitor_alive"):
                return {
                    "status": "running",
                    "campaign_id": campaign_id,
                    "run_dir": str(run_dir),
                    "owner_pid": state["owner_pid"],
                    "monitor_pid": state["monitor_pid"],
                    "deadline_at_epoch": state["deadline_at_epoch"],
                    "predecessor_run_dir": state.get("predecessor_run_dir"),
                }
        time.sleep(0.05)
    raise SupervisorError("Campaign 父监督器未在 5 秒内就绪。")


def _campaign_stop_command(
    run_dir: Path,
    *,
    reason: str,
    status: str,
) -> dict[str, Any]:
    """请求父监督器封存终态，并等待有界确认。"""

    run_dir = _validate_state_dir(run_dir, create=False)
    state = _read_state(run_dir)
    if state.get("state") != "running":
        raise SupervisorError("Campaign 父监督器已经结束。")
    if status not in {"stopped", "failed"}:
        raise SupervisorError("Campaign 结束状态非法。")
    now = time.time()
    request = {
        "schema_version": CAMPAIGN_FINISH_SCHEMA,
        "requested_at_utc": _epoch_to_utc(now),
        "requested_at_epoch": now,
        "reason": _note(reason, "Campaign 结束原因"),
        "status": status,
    }
    _write_json(
        run_dir / "campaign-finish-request.json",
        request,
        replace=False,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = _status_command(run_dir)
        if (
            result.get("state") in TERMINAL_STATES
            and result.get("owner_alive") is False
            and result.get("monitor_alive") is False
        ):
            return result
        time.sleep(0.05)
    raise SupervisorError("Campaign 父监督器未在 5 秒内封存终态并排空进程。")


def _campaign_run_sha256(value: Any, label: str) -> str:
    """校验 campaign-run 控制制品中的 SHA-256 字面量。"""

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SupervisorError(f"Campaign run {label} 非法。")
    return value


def _campaign_run_file_binding(
    value: Any,
    *,
    label: str,
    require_bound_files: bool,
    include_tool_identity: bool = False,
) -> dict[str, str]:
    """校验恢复清单绑定的绝对普通文件及其摘要。"""

    fields = {"path", "sha256"}
    if include_tool_identity:
        fields.add("tool_files_sha256")
    if not isinstance(value, dict) or set(value) != fields:
        raise SupervisorError(f"Campaign {label} 字段不闭合。")
    bound_path = Path(str(value.get("path", "")))
    digest = _campaign_run_sha256(value.get("sha256"), f"{label}.sha256")
    if (
        not bound_path.is_absolute()
        or bound_path.is_symlink()
        or (
            require_bound_files
            and (
                not bound_path.is_file()
                or _sha256(bound_path.read_bytes()) != digest
            )
        )
    ):
        raise SupervisorError(f"Campaign {label} 路径或摘要漂移。")
    normalized = {"path": str(bound_path), "sha256": digest}
    if include_tool_identity:
        normalized["tool_files_sha256"] = _campaign_run_sha256(
            value.get("tool_files_sha256"),
            f"{label}.tool_files_sha256",
        )
    return normalized


def _campaign_run_tool_transition(
    value: Any,
    *,
    label: str,
    maintenance_only: bool,
) -> dict[str, Any]:
    """校验 v4 中维护变化和最终有效工具变化的逐文件闭集。"""

    fields = {
        "from_tool_files_sha256",
        "to_tool_files_sha256",
        "changed_files",
        "allowed_production_paths",
        "affected_job_ids",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SupervisorError(f"Campaign {label} 字段不闭合。")
    normalized: dict[str, Any] = {
        "from_tool_files_sha256": _campaign_run_sha256(
            value.get("from_tool_files_sha256"),
            f"{label}.from_tool_files_sha256",
        ),
        "to_tool_files_sha256": _campaign_run_sha256(
            value.get("to_tool_files_sha256"),
            f"{label}.to_tool_files_sha256",
        ),
    }
    raw_changed = value.get("changed_files")
    changed: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    if not isinstance(raw_changed, list) or not raw_changed:
        raise SupervisorError(f"Campaign {label}.changed_files 必须为非空数组。")
    for index, raw in enumerate(raw_changed, 1):
        if not isinstance(raw, dict) or set(raw) != {
            "path",
            "from_sha256",
            "to_sha256",
            "classification",
            "affected_job_ids",
        }:
            raise SupervisorError(
                f"Campaign {label}.changed_files 第 {index} 项字段不闭合。"
            )
        raw_path = raw.get("path")
        parsed = Path(str(raw_path))
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or parsed.is_absolute()
            or "\\" in raw_path
            or str(parsed) != raw_path
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or raw_path in seen_paths
        ):
            raise SupervisorError(f"Campaign {label}.changed_files 路径非法。")
        seen_paths.add(raw_path)
        for side in ("from_sha256", "to_sha256"):
            digest = raw.get(side)
            if digest is not None:
                _campaign_run_sha256(digest, f"{label}.{side}")
        if raw.get("from_sha256") is None and raw.get("to_sha256") is None:
            raise SupervisorError(f"Campaign {label}.changed_files 双侧摘要均为空。")
        classification = raw.get("classification")
        allowed_classifications = {
            "evaluation",
            "phase_scoped_hybrid",
        }
        if not maintenance_only:
            allowed_classifications.add("failed_job_production")
        if classification not in allowed_classifications:
            raise SupervisorError(f"Campaign {label}.changed_files 风险分类非法。")
        affected = raw.get("affected_job_ids")
        if (
            not isinstance(affected, list)
            or affected != sorted(set(affected))
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 128
                or not SAFE_ID_CHARS.issuperset(item)
                for item in affected
            )
            or (classification != "failed_job_production" and affected)
        ):
            raise SupervisorError(f"Campaign {label}.changed_files affected Job 非法。")
        changed.append(dict(raw))
    if [item["path"] for item in changed] != sorted(seen_paths):
        raise SupervisorError(f"Campaign {label}.changed_files 必须按路径排序。")
    for field in ("allowed_production_paths", "affected_job_ids"):
        values = value.get(field)
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or any(not isinstance(item, str) or not item for item in values)
        ):
            raise SupervisorError(f"Campaign {label}.{field} 非法。")
        normalized[field] = list(values)
    production_paths = sorted(
        item["path"]
        for item in changed
        if item["classification"] == "failed_job_production"
    )
    affected_union = sorted(
        {
            job_id
            for item in changed
            for job_id in item["affected_job_ids"]
        }
    )
    if (
        normalized["allowed_production_paths"] != production_paths
        or normalized["affected_job_ids"] != affected_union
        or (
            maintenance_only
            and (
                normalized["allowed_production_paths"]
                or normalized["affected_job_ids"]
            )
        )
    ):
        raise SupervisorError(f"Campaign {label} 产出路径或 affected Job 闭集漂移。")
    normalized["changed_files"] = changed
    return normalized


def _attempt_fingerprint(payload: Mapping[str, Any]) -> str:
    """复算与主编排器一致、无结尾换行的 Attempt 摘要。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _command_assignment(command: Sequence[str], name: str) -> str:
    """从 ``/usr/bin/env KEY=value`` 动作中提取唯一冻结坐标。"""

    prefix = f"{name}="
    values = [value[len(prefix) :] for value in command if value.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise SupervisorError(f"VC-1 assertion 动作缺少唯一 {name} 坐标。")
    return values[0]


def _command_flag(command: Sequence[str], name: str) -> str:
    """从 CLI 动作中提取一个唯一的参数值。"""

    if command.count(name) != 1:
        raise SupervisorError(f"VC-1 seal 动作缺少唯一 {name} 坐标。")
    index = command.index(name)
    if index + 1 >= len(command) or not command[index + 1]:
        raise SupervisorError(f"VC-1 seal 动作的 {name} 值为空。")
    return command[index + 1]


def _assertion_coordinates(action: Mapping[str, Any]) -> tuple[Path, str]:
    """解析正式 assertion bundle 动作绑定的 Campaign 与 Attempt。"""

    command = action.get("command")
    if not isinstance(command, list) or not all(isinstance(value, str) for value in command):
        raise SupervisorError("VC-1 assertion command 非法。")
    campaign_dir = Path(_command_assignment(command, "CAMPAIGN_DIR"))
    attempt_id = _safe_id(_command_assignment(command, "ATTEMPT_ID"), "ATTEMPT_ID")
    if (
        _command_assignment(command, "SIDE") != "official"
        or not campaign_dir.is_absolute()
        or campaign_dir.is_symlink()
    ):
        raise SupervisorError("VC-1 assertion 只能绑定官方侧绝对 Campaign。")
    return campaign_dir, attempt_id


def _seal_coordinates(action: Mapping[str, Any]) -> tuple[Path, str]:
    """解析正式 seal preview 动作绑定的 Campaign 与 Attempt。"""

    command = action.get("command")
    if not isinstance(command, list) or not all(isinstance(value, str) for value in command):
        raise SupervisorError("VC-1 seal command 非法。")
    campaign_dir = Path(_command_flag(command, "--campaign-dir"))
    attempt_id = _safe_id(_command_flag(command, "--attempt-id"), "--attempt-id")
    if not campaign_dir.is_absolute() or campaign_dir.is_symlink():
        raise SupervisorError("VC-1 seal 只能绑定绝对 Campaign。")
    return campaign_dir, attempt_id


def _replay_vc1_evidence_permission_closeout(
    campaign_dir: Path,
    attempt_id: str,
    *,
    expected_campaign_id: str | None = None,
) -> str:
    """重放新 Attempt 的权限收口；v2 仅作为不可重跑的历史身份返回。"""

    try:
        campaign_dir = campaign_dir.resolve(strict=True)
    except OSError as error:
        raise SupervisorError("VC-1 权限门禁的 Campaign 不存在。") from error
    try:
        relative = campaign_dir.relative_to(campaign_dir.parents[2])
    except (IndexError, ValueError) as error:
        raise SupervisorError("VC-1 Campaign 路径层级非法。") from error
    if relative.parts[:2] != ("evidence", "campaigns") or len(relative.parts) != 3:
        raise SupervisorError("VC-1 Campaign 不在受管 data/evidence/campaigns 下。")
    campaign = _read_json(campaign_dir / "campaign.json")
    attempt_root = campaign_dir / "official" / "attempts" / attempt_id
    attempt = _read_json(attempt_root / "attempt.json")
    unsigned = dict(attempt)
    digest = unsigned.pop("attempt_digest", None)
    schema_version = attempt.get("schema_version")
    if (
        (
            expected_campaign_id is not None
            and campaign.get("campaign_id") != expected_campaign_id
        )
        or
        attempt.get("campaign_id") != campaign.get("campaign_id")
        or attempt.get("phase") != "official"
        or attempt.get("candidate_id") is not None
        or attempt.get("attempt_id") != attempt_id
        or not isinstance(digest, str)
        or digest != _attempt_fingerprint(unsigned)
    ):
        raise SupervisorError("VC-1 权限门禁的 Attempt 身份或摘要漂移。")
    if schema_version == "codex-upgrade-capture-attempt/v2":
        return schema_version
    if (
        schema_version != "codex-upgrade-capture-attempt/v3"
        or attempt.get("status") != "awaiting_receipts"
        or attempt.get("evidence_permission_error") is not None
        or not isinstance(attempt.get("evidence_permission_closeout"), Mapping)
    ):
        raise SupervisorError("VC-1 assertion 前 Attempt 没有通过的权限收口。")
    roots = attempt.get("evidence_roots")
    if (
        not isinstance(roots, list)
        or not roots
        or any(not isinstance(value, str) for value in roots)
    ):
        raise SupervisorError("VC-1 权限门禁的 evidence_roots 非法。")
    try:
        evidence_permissions.replay_evidence_permission_closeout(
            attempt_root,
            [Path(value) for value in roots],
            attempt["evidence_permission_closeout"],
            managed_data_root=campaign_dir.parents[2],
        )
    except (OSError, evidence_permissions.EvidencePermissionError) as error:
        raise SupervisorError(f"VC-1 assertion 前权限收口未通过：{error}") from error
    return schema_version


def _validate_vc1_assertion_seal_gate(
    *,
    schema_version: str,
    campaign_id: str,
    phase: str,
    actions: Sequence[Mapping[str, Any]],
    require_bound_files: bool,
) -> None:
    """强制新 VC-1 形成权限收口→assertion→seal 的单向顺序。"""

    if schema_version != CAMPAIGN_RUN_BATCHED_SCHEMA or phase != "VC-1":
        return
    assertion_indices = [
        index
        for index, action in enumerate(actions)
        if action.get("action_id") == "prepare-official-assertion-bundle"
    ]
    seal_indices = [
        index
        for index, action in enumerate(actions)
        if action.get("action_id") == "seal-official-preview"
    ]
    if not assertion_indices and not seal_indices:
        return
    if len(assertion_indices) > 1 or len(seal_indices) > 1:
        raise SupervisorError("VC-1 assertion 或 seal 动作重复。")
    if assertion_indices and seal_indices and assertion_indices[0] >= seal_indices[0]:
        raise SupervisorError("VC-1 必须先生成 assertion bundle，再执行 seal preview。")
    coordinates: tuple[Path, str]
    if assertion_indices:
        coordinates = _assertion_coordinates(actions[assertion_indices[0]])
    else:
        coordinates = _seal_coordinates(actions[seal_indices[0]])
    if assertion_indices and seal_indices:
        if _seal_coordinates(actions[seal_indices[0]]) != coordinates:
            raise SupervisorError("VC-1 assertion 与 seal 没有绑定同一 Attempt。")
    if not require_bound_files:
        return
    attempt_schema = _replay_vc1_evidence_permission_closeout(
        *coordinates,
        expected_campaign_id=campaign_id,
    )
    if attempt_schema == "codex-upgrade-capture-attempt/v3" and (
        not assertion_indices or not seal_indices
    ):
        raise SupervisorError("新 VC-1 批次必须连续声明 assertion bundle 与 seal preview。")


def _campaign_run_manifest(
    path: Path,
    *,
    require_bound_files: bool = True,
) -> dict[str, Any]:
    """读取并严格校验新流程使用的预声明动作队列。

    生成器可在最终合同原子落盘前只做结构校验；真正执行清单时保持默认值，
    必须重放合同普通文件及其摘要，不能把生成期豁免带到运行边界。
    """

    path = Path(path)
    _validate_file(path)
    payload = _read_json(path)
    schema_version = payload.get("schema_version")
    if schema_version == CAMPAIGN_RUN_SCHEMA:
        required = {
            "schema_version",
            "campaign_id",
            "phase",
            "deadline_seconds",
            "actions",
            "no_op",
        }
        optional = {"execute_items", "reuse_items"}
    elif schema_version in {
        CAMPAIGN_RUN_BATCHED_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
    }:
        required = {
            "schema_version",
            "campaign_id",
            "campaign_plan_sha256",
            "batch_id",
            "batch_sequence",
            "batch_sha256",
            "phase",
            "predecessor_checkpoint",
            "original_deadline_at_utc",
            "actions",
            "execute_items",
            "reuse_items",
            "no_op",
        }
        if schema_version == CAMPAIGN_RUN_RECOVERY_SCHEMA:
            required.update(
                {
                    "recovery_mode",
                    "recovery_contract",
                    "recovery_predecessor",
                }
            )
        elif schema_version in {
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            required.update(
                {
                    "recovery_mode",
                    "recovery_contract",
                    "recovery_predecessor",
                    "continuation_predecessor",
                    "deployment_receipt",
                    "maintenance_tool_transition",
                    "effective_tool_transition",
                }
            )
            if schema_version == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA:
                required.update(
                    {
                        "continuation_manifest",
                        "finalization_predecessor",
                    }
                )
        optional = set()
    else:
        raise SupervisorError("Campaign run manifest schema_version 不受支持。")
    if (
        not required.issubset(payload)
        or set(payload) - required - optional
    ):
        raise SupervisorError("Campaign run manifest 字段或 schema 不闭合。")
    campaign_id = _safe_id(payload.get("campaign_id"), "campaign_id")
    phase = _safe_id(payload.get("phase"), "phase", maximum=32)
    deadline_seconds: float | None = None
    original_deadline_at_utc: str | None = None
    original_deadline_at_epoch: float | None = None
    if schema_version == CAMPAIGN_RUN_SCHEMA:
        deadline_seconds = _positive_seconds(
            payload.get("deadline_seconds"),
            "deadline_seconds",
        )
    else:
        original_deadline_at_utc = str(payload.get("original_deadline_at_utc", ""))
        try:
            deadline_value = datetime.fromisoformat(
                original_deadline_at_utc.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise SupervisorError(
                "Campaign run original_deadline_at_utc 不是有效 RFC3339 时间。"
            ) from error
        if deadline_value.tzinfo is None:
            raise SupervisorError("Campaign run 原始 deadline 缺少时区。")
        original_deadline_at_epoch = deadline_value.timestamp()
        for field in ("campaign_plan_sha256", "batch_sha256"):
            _campaign_run_sha256(payload.get(field), field)
        _safe_id(payload.get("batch_id"), "batch_id")
        sequence = payload.get("batch_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise SupervisorError("Campaign run batch_sequence 非法。")
        predecessor = payload.get("predecessor_checkpoint")
        if not isinstance(predecessor, dict) or set(predecessor) != {
            "path",
            "sha256",
            "phase",
            "checkpoint_sha256",
        }:
            raise SupervisorError("Campaign run predecessor_checkpoint 字段不闭合。")
        _safe_id(predecessor.get("phase"), "predecessor_checkpoint.phase", maximum=32)
        if not isinstance(predecessor.get("path"), str) or not predecessor["path"]:
            raise SupervisorError("Campaign run predecessor checkpoint 路径为空。")
        for field in ("sha256", "checkpoint_sha256"):
            value = predecessor.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise SupervisorError(
                    f"Campaign run predecessor_checkpoint.{field} 非法。"
                )
        if schema_version in {
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            expected_mode = {
                CAMPAIGN_RUN_RECOVERY_SCHEMA: "interrupted-vc1-preview",
                CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA: (
                    "interrupted-vc1-preview-continuation"
                ),
                CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA: (
                    "interrupted-vc1-preview-finalization"
                ),
            }[schema_version]
            if payload.get("recovery_mode") != expected_mode:
                raise SupervisorError("Campaign recovery mode 非法。")
            _campaign_run_file_binding(
                payload.get("recovery_contract"),
                label="recovery_contract",
                require_bound_files=require_bound_files,
            )
            recovery_predecessor = payload.get("recovery_predecessor")
            recovery_fields = {
                "run_dir",
                "state_sha256",
                "manifest_sha256",
                "stop_receipt_sha256",
                "owner_nonce",
                "terminal_at_utc",
                "state",
                "reason",
                "batch_id",
                "batch_sequence",
                "batch_sha256",
            }
            if (
                not isinstance(recovery_predecessor, dict)
                or set(recovery_predecessor) != recovery_fields
                or recovery_predecessor.get("state") != "failed"
                or recovery_predecessor.get("reason") != "KeyboardInterrupt"
            ):
                raise SupervisorError("Campaign recovery_predecessor 字段或直接前序关系非法。")
            run_dir = Path(str(recovery_predecessor.get("run_dir", "")))
            if not run_dir.is_absolute():
                raise SupervisorError("Campaign recovery predecessor run_dir 必须是绝对路径。")
            _safe_id(recovery_predecessor.get("batch_id"), "recovery batch_id")
            terminal_at = recovery_predecessor.get("terminal_at_utc")
            try:
                terminal_value = datetime.fromisoformat(
                    str(terminal_at).replace("Z", "+00:00")
                )
            except ValueError as error:
                raise SupervisorError(
                    "Campaign recovery predecessor terminal_at_utc 非法。"
                ) from error
            if terminal_value.tzinfo is None:
                raise SupervisorError(
                    "Campaign recovery predecessor terminal_at_utc 缺少时区。"
                )
            for field in (
                "state_sha256",
                "manifest_sha256",
                "stop_receipt_sha256",
                "owner_nonce",
                "batch_sha256",
            ):
                _campaign_run_sha256(
                    recovery_predecessor.get(field),
                    f"recovery_predecessor.{field}",
                )
            if (
                schema_version == CAMPAIGN_RUN_RECOVERY_SCHEMA
                and recovery_predecessor.get("batch_sequence") != sequence - 1
            ):
                raise SupervisorError("Campaign v3 recovery_predecessor 不是直接前序。")
            if schema_version in {
                CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
                CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
            }:
                continuation = payload.get("continuation_predecessor")
                continuation_fields = {
                    "run_dir",
                    "state_sha256",
                    "manifest_sha256",
                    "stop_receipt_sha256",
                    "action_diagnostic_sha256",
                    "owner_nonce",
                    "terminal_at_utc",
                    "state",
                    "reason",
                    "error_type",
                    "message",
                    "batch_id",
                    "batch_sequence",
                    "batch_sha256",
                }
                if (
                    not isinstance(continuation, dict)
                    or set(continuation) != continuation_fields
                    or continuation.get("state") != "failed"
                    or continuation.get("reason")
                    != "action-failed:recover-vc1-interruption-preview"
                    or continuation.get("error_type") != "ConfigurationError"
                    or continuation.get("message")
                    != "watchdog heartbeat 越出当前 attempt。"
                    or recovery_predecessor.get("batch_sequence") != 1
                    or continuation.get("batch_sequence") != 2
                    or (
                        schema_version == CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA
                        and sequence != 3
                    )
                    or (
                        schema_version == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA
                        and sequence != 4
                    )
                ):
                    raise SupervisorError(
                        "Campaign v4/v5 continuation_predecessor 非法。"
                    )
                continuation_dir = Path(str(continuation.get("run_dir", "")))
                if not continuation_dir.is_absolute():
                    raise SupervisorError(
                        "Campaign v4/v5 continuation run_dir 必须是绝对路径。"
                    )
                _safe_id(continuation.get("batch_id"), "continuation batch_id")
                try:
                    continuation_time = datetime.fromisoformat(
                        str(continuation.get("terminal_at_utc", "")).replace(
                            "Z", "+00:00"
                        )
                    )
                except ValueError as error:
                    raise SupervisorError(
                        "Campaign v4/v5 continuation terminal_at_utc 非法。"
                    ) from error
                if continuation_time.tzinfo is None:
                    raise SupervisorError(
                        "Campaign v4/v5 continuation terminal_at_utc 缺少时区。"
                    )
                for field in (
                    "state_sha256",
                    "manifest_sha256",
                    "stop_receipt_sha256",
                    "action_diagnostic_sha256",
                    "owner_nonce",
                    "batch_sha256",
                ):
                    _campaign_run_sha256(
                        continuation.get(field),
                        f"continuation_predecessor.{field}",
                    )
                if schema_version == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA:
                    _campaign_run_file_binding(
                        payload.get("continuation_manifest"),
                        label="continuation_manifest",
                        require_bound_files=require_bound_files,
                    )
                    finalization = payload.get("finalization_predecessor")
                    finalization_fields = {
                        "run_dir",
                        "state_sha256",
                        "manifest_sha256",
                        "stop_receipt_sha256",
                        "action_diagnostic_sha256",
                        "owner_nonce",
                        "terminal_at_utc",
                        "state",
                        "reason",
                        "error_type",
                        "message",
                        "batch_id",
                        "batch_sequence",
                        "batch_sha256",
                    }
                    if (
                        not isinstance(finalization, dict)
                        or set(finalization) != finalization_fields
                        or finalization.get("state") != "failed"
                        or finalization.get("reason")
                        != "action-failed:continue-vc1-interruption-preview"
                        or finalization.get("error_type") != "ConfigurationError"
                        or finalization.get("message")
                        != "中断恢复续接父 v4 清单、自绑定或动作漂移。"
                        or finalization.get("batch_sequence") != 3
                    ):
                        raise SupervisorError(
                            "Campaign v5 finalization_predecessor 非法。"
                        )
                    finalization_dir = Path(
                        str(finalization.get("run_dir", ""))
                    )
                    if not finalization_dir.is_absolute():
                        raise SupervisorError(
                            "Campaign v5 finalization run_dir 必须是绝对路径。"
                        )
                    _safe_id(
                        finalization.get("batch_id"),
                        "finalization batch_id",
                    )
                    try:
                        finalization_time = datetime.fromisoformat(
                            str(finalization.get("terminal_at_utc", "")).replace(
                                "Z", "+00:00"
                            )
                        )
                    except ValueError as error:
                        raise SupervisorError(
                            "Campaign v5 finalization terminal_at_utc 非法。"
                        ) from error
                    if finalization_time.tzinfo is None:
                        raise SupervisorError(
                            "Campaign v5 finalization terminal_at_utc 缺少时区。"
                        )
                    for field in (
                        "state_sha256",
                        "manifest_sha256",
                        "stop_receipt_sha256",
                        "action_diagnostic_sha256",
                        "owner_nonce",
                        "batch_sha256",
                    ):
                        _campaign_run_sha256(
                            finalization.get(field),
                            f"finalization_predecessor.{field}",
                        )
                _campaign_run_file_binding(
                    payload.get("deployment_receipt"),
                    label="deployment_receipt",
                    require_bound_files=require_bound_files,
                    include_tool_identity=True,
                )
                maintenance = _campaign_run_tool_transition(
                    payload.get("maintenance_tool_transition"),
                    label="maintenance_tool_transition",
                    maintenance_only=True,
                )
                effective = _campaign_run_tool_transition(
                    payload.get("effective_tool_transition"),
                    label="effective_tool_transition",
                    maintenance_only=False,
                )
                if (
                    maintenance["to_tool_files_sha256"]
                    != effective["to_tool_files_sha256"]
                ):
                    raise SupervisorError(
                        "Campaign v4/v5 两段工具 transition 目标不一致。"
                    )
    no_op = payload.get("no_op")
    if not isinstance(no_op, bool):
        raise SupervisorError("Campaign run manifest no_op 必须是布尔值。")
    actions = payload.get("actions")
    if not isinstance(actions, list) or len(actions) > 256:
        raise SupervisorError("Campaign run manifest actions 必须是最多 256 项的数组。")
    normalized_actions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(actions, 1):
        expected_action_fields = {
            "action_id",
            "operation",
            "timeout_seconds",
            "command",
        }
        if schema_version in {
            CAMPAIGN_RUN_BATCHED_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            expected_action_fields.add("item_ids")
        if not isinstance(raw, dict) or set(raw) != expected_action_fields:
            raise SupervisorError(f"Campaign run action 第 {index} 项字段不闭合。")
        action_id = _safe_id(raw.get("action_id"), f"action[{index}].action_id")
        if action_id in seen_ids:
            raise SupervisorError(f"Campaign run action 重复：{action_id}")
        seen_ids.add(action_id)
        operation = _operation(raw.get("operation"))
        if (
            phase.casefold() == FORMAL_VC6_PHASE.casefold()
            and (
                operation == "dispatch-next-action"
                or operation == "failure-diagnosis"
                or operation.startswith("planning:")
            )
        ):
            raise SupervisorError(
                "VC-6 campaign-run 不得声明动态 planning/dispatch 动作；"
                "请一次性声明完整动作队列。"
            )
        timeout_seconds = _positive_seconds(
            raw.get("timeout_seconds"), f"action[{index}].timeout_seconds"
        )
        command = raw.get("command")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise SupervisorError(f"Campaign run action 第 {index} 项 command 非法。")
        if any(len(item) > 4096 for item in command):
            raise SupervisorError(f"Campaign run action 第 {index} 项 command 参数过长。")
        command_basenames = {Path(item).name for item in command[:4]}
        is_upgrade_cli = bool(
            command_basenames & {"codex_upgrade.py", "codex-upgrade"}
        )
        is_supervisor_cli = bool(
            command_basenames
            & {"codex_upgrade_supervisor.py", "codex-upgrade-supervisor"}
        )
        forbidden = {
            "successor",
            "control-epoch",
            "evaluation-transition",
            "terminal-transition-preflight",
            "plan",
            "reuse-official-evidence",
            "compile-vc-batch",
            "compile-vc-interrupted-recovery-batch",
            "compile-vc-interrupted-recovery-continuation",
        }
        if schema_version != CAMPAIGN_RUN_RECOVERY_SCHEMA:
            forbidden.add("recover-vc1-interruption")
        if schema_version not in {
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            forbidden.add("continue-vc1-interruption")
        if is_supervisor_cli:
            forbidden |= {
                "campaign-start",
                "campaign-mark",
                "campaign-exec",
                "campaign-stop",
                "campaign-owner",
                "campaign-run",
                "run",
            }
        if is_upgrade_cli and any(item in forbidden for item in command):
            raise SupervisorError(
                f"Campaign run action 第 {index} 项包含控制面或旧写入入口，拒绝预声明。"
            )
        normalized_action = {
            "action_id": action_id,
            "operation": operation,
            "timeout_seconds": timeout_seconds,
            "command": list(command),
        }
        if schema_version in {
            CAMPAIGN_RUN_BATCHED_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            item_ids = raw.get("item_ids")
            if (
                not isinstance(item_ids, list)
                or not item_ids
                or item_ids != sorted(set(item_ids))
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > 128
                    or not SAFE_ID_CHARS.issuperset(value)
                    for value in item_ids
                )
            ):
                raise SupervisorError(
                    f"Campaign run action 第 {index} 项 item_ids 非法。"
                )
            normalized_action["item_ids"] = list(item_ids)
        normalized_actions.append(normalized_action)
    _validate_vc1_assertion_seal_gate(
        schema_version=str(schema_version),
        campaign_id=campaign_id,
        phase=phase,
        actions=normalized_actions,
        require_bound_files=require_bound_files,
    )
    execute_items = payload.get(
        "execute_items", [item["action_id"] for item in normalized_actions]
    )
    reuse_items = payload.get("reuse_items", [])
    for label, values in (("execute_items", execute_items), ("reuse_items", reuse_items)):
        if not isinstance(values, list) or len(values) > 256:
            raise SupervisorError(f"Campaign run manifest {label} 必须是最多 256 项的数组。")
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 128
            or not SAFE_ID_CHARS.issuperset(value)
            for value in values
        ) or len(set(values)) != len(values):
            raise SupervisorError(f"Campaign run manifest {label} 含非法或重复 ID。")
    action_ids = {item["action_id"] for item in normalized_actions}
    if schema_version == CAMPAIGN_RUN_SCHEMA:
        if set(execute_items) != action_ids:
            raise SupervisorError("execute_items 必须与预声明动作集合完全一致。")
    else:
        covered = [
            item_id
            for action in normalized_actions
            for item_id in action["item_ids"]
        ]
        if len(covered) != len(set(covered)) or sorted(covered) != sorted(execute_items):
            raise SupervisorError(
                "batched campaign-run 动作未无重叠地精确覆盖 execute_items。"
            )
    if set(execute_items) & set(reuse_items):
        raise SupervisorError("execute_items 与 reuse_items 不得交叉。")
    if no_op and normalized_actions:
        raise SupervisorError("no_op Campaign 不得包含动作。")
    if not no_op and not normalized_actions:
        raise SupervisorError("非 no_op Campaign 必须至少声明一个动作。")
    if no_op and execute_items:
        raise SupervisorError("no_op Campaign 的 execute_items 必须为空。")
    if schema_version == CAMPAIGN_RUN_RECOVERY_SCHEMA:
        if no_op or len(normalized_actions) != 1:
            raise SupervisorError("v3 中断恢复必须恰好声明一个非 no-op 动作。")
        recovery_action = normalized_actions[0]
        recovery_command = recovery_action["command"]
        contract_path = str(payload["recovery_contract"]["path"])
        contract_flag_index = (
            recovery_command.index("--recovery-contract")
            if recovery_command.count("--recovery-contract") == 1
            else -1
        )
        recovery_basenames = {
            Path(item).name for item in recovery_command[:4]
        }
        if (
            phase != "VC-1"
            or not recovery_basenames
            & {"codex_upgrade.py", "codex-upgrade"}
            or recovery_action["action_id"]
            != "recover-vc1-interruption-preview"
            or recovery_action["operation"]
            != "VC-1:recover-interruption-preview"
            or recovery_command.count("recover-vc1-interruption") != 1
            or contract_flag_index < 0
            or contract_flag_index + 1 >= len(recovery_command)
            or recovery_command[contract_flag_index + 1] != contract_path
            or "--acknowledge-live-requests" in recovery_command
        ):
            raise SupervisorError(
                "v3 中断恢复只能执行绑定合同的唯一零请求预览动作。"
            )
    if schema_version in {
        CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
    }:
        if no_op or len(normalized_actions) != 1:
            raise SupervisorError(
                "v4/v5 中断恢复续接必须恰好声明一个非 no-op 动作。"
            )
        continuation_action = normalized_actions[0]
        continuation_command = continuation_action["command"]
        contract_path = str(payload["recovery_contract"]["path"])
        contract_flag_index = (
            continuation_command.index("--recovery-contract")
            if continuation_command.count("--recovery-contract") == 1
            else -1
        )
        manifest_flag_index = (
            continuation_command.index("--continuation-manifest")
            if continuation_command.count("--continuation-manifest") == 1
            else -1
        )
        continuation_basenames = {
            Path(item).name for item in continuation_command[:4]
        }
        if (
            phase != "VC-1"
            or not continuation_basenames & {"codex_upgrade.py", "codex-upgrade"}
            or continuation_action["action_id"]
            != "continue-vc1-interruption-preview"
            or continuation_action["operation"]
            != "VC-1:continue-interruption-preview"
            or continuation_command.count("continue-vc1-interruption") != 1
            or contract_flag_index < 0
            or contract_flag_index + 1 >= len(continuation_command)
            or continuation_command[contract_flag_index + 1] != contract_path
            or manifest_flag_index < 0
            or manifest_flag_index + 1 >= len(continuation_command)
            or not Path(continuation_command[manifest_flag_index + 1]).is_absolute()
            or "--acknowledge-live-requests" in continuation_command
        ):
            raise SupervisorError(
                "v4/v5 中断恢复续接只能执行绑定原合同和自身清单的唯一零请求预览动作。"
            )
    normalized = {
        "schema_version": schema_version,
        "campaign_id": campaign_id,
        "phase": phase,
        "no_op": no_op,
        "actions": normalized_actions,
        "execute_items": list(execute_items),
        "reuse_items": list(reuse_items),
    }
    if schema_version == CAMPAIGN_RUN_SCHEMA:
        normalized["deadline_seconds"] = deadline_seconds
    else:
        normalized.update(
            {
                "campaign_plan_sha256": payload["campaign_plan_sha256"],
                "batch_id": payload["batch_id"],
                "batch_sequence": payload["batch_sequence"],
                "batch_sha256": payload["batch_sha256"],
                "predecessor_checkpoint": dict(payload["predecessor_checkpoint"]),
                "original_deadline_at_utc": original_deadline_at_utc,
            }
        )
        if schema_version in {
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            normalized.update(
                {
                    "recovery_mode": payload["recovery_mode"],
                    "recovery_contract": dict(payload["recovery_contract"]),
                    "recovery_predecessor": dict(payload["recovery_predecessor"]),
                }
            )
        if schema_version in {
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            normalized.update(
                {
                    "continuation_predecessor": dict(
                        payload["continuation_predecessor"]
                    ),
                    "deployment_receipt": dict(payload["deployment_receipt"]),
                    "maintenance_tool_transition": dict(
                        payload["maintenance_tool_transition"]
                    ),
                    "effective_tool_transition": dict(
                        payload["effective_tool_transition"]
                    ),
                }
            )
            if schema_version == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA:
                normalized.update(
                    {
                        "continuation_manifest": dict(
                            payload["continuation_manifest"]
                        ),
                        "finalization_predecessor": dict(
                            payload["finalization_predecessor"]
                        ),
                    }
                )
    return normalized


def build_campaign_run_manifest(
    campaign_id: str,
    phase: str,
    deadline_seconds: float,
    *,
    actions: Sequence[Mapping[str, Any]],
    reuse_items: Iterable[str] = (),
) -> dict[str, Any]:
    """生成正式流程使用的不可变动作清单。

    ``actions`` 只接受本轮 execute 集合；``reuse_items`` 只记录从 checkpoint
    读取的项目，不会被 campaign-run 执行。execute 为空时自动生成 no-op 清单，
    调用方无需另写“空队列”分支。
    """

    normalized_actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(actions, 1):
        if not isinstance(raw, Mapping):
            raise SupervisorError(f"正式动作第 {index} 项必须是对象。")
        try:
            action_id = _safe_id(raw["action_id"], f"action[{index}].action_id")
            operation = _operation(raw["operation"])
            timeout_seconds = _positive_seconds(
                raw["timeout_seconds"], f"action[{index}].timeout_seconds"
            )
            command = raw["command"]
        except KeyError as error:
            raise SupervisorError(f"正式动作第 {index} 项缺少字段：{error.args[0]}") from error
        if action_id in seen:
            raise SupervisorError(f"正式动作重复：{action_id}")
        seen.add(action_id)
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
            or len(command) > 64
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise SupervisorError(f"正式动作 {action_id} command 非法。")
        normalized_actions.append(
            {
                "action_id": action_id,
                "operation": operation,
                "timeout_seconds": timeout_seconds,
                "command": list(command),
            }
        )
    campaign_id = _safe_id(campaign_id, "campaign_id")
    phase = _safe_id(phase, "phase", maximum=32)
    deadline_seconds = _positive_seconds(deadline_seconds, "deadline_seconds")
    normalized_reuse = [_safe_id(value, "reuse_item") for value in reuse_items]
    if len(set(normalized_reuse)) != len(normalized_reuse):
        raise SupervisorError("reuse_items 含重复 ID。")
    if set(normalized_reuse) & seen:
        raise SupervisorError("reuse_items 与 execute actions 不得交叉。")
    payload = {
        "schema_version": CAMPAIGN_RUN_SCHEMA,
        "campaign_id": campaign_id,
        "phase": phase,
        "deadline_seconds": deadline_seconds,
        "no_op": not normalized_actions,
        "actions": normalized_actions,
        "execute_items": [item["action_id"] for item in normalized_actions],
        "reuse_items": normalized_reuse,
    }
    if not normalized_actions:
        payload["no_op"] = True
    return payload


def build_batched_campaign_run_manifest(
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    batch_id: str,
    batch_sequence: int,
    batch_sha256: str,
    phase: str,
    predecessor_checkpoint: Mapping[str, Any],
    original_deadline_at_utc: str,
    actions: Sequence[Mapping[str, Any]],
    execute_items: Sequence[str],
    reuse_items: Sequence[str],
) -> dict[str, Any]:
    """把已冻结 VC batch 编译成使用绝对总截止时间的 v2 队列。"""

    payload = {
        "schema_version": CAMPAIGN_RUN_BATCHED_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_plan_sha256": campaign_plan_sha256,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "batch_sha256": batch_sha256,
        "phase": phase,
        "predecessor_checkpoint": dict(predecessor_checkpoint),
        "original_deadline_at_utc": original_deadline_at_utc,
        "no_op": not actions,
        "actions": [dict(action) for action in actions],
        "execute_items": list(execute_items),
        "reuse_items": list(reuse_items),
    }
    # 使用与 CLI 完全相同的校验器，避免生成器与执行器对字段或闭集理解不同。
    descriptor, temporary_name = tempfile.mkstemp(prefix=".campaign-run-v2-", suffix=".json")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
        normalized = _campaign_run_manifest(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    # ``_campaign_run_manifest`` 只增加运行期解析值；不可变文件保持声明字段。
    normalized.pop("original_deadline_at_epoch", None)
    return normalized


def build_recovery_campaign_run_manifest(
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    batch_id: str,
    batch_sequence: int,
    batch_sha256: str,
    phase: str,
    predecessor_checkpoint: Mapping[str, Any],
    original_deadline_at_utc: str,
    recovery_contract: Mapping[str, Any],
    recovery_predecessor: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    execute_items: Sequence[str],
    reuse_items: Sequence[str],
) -> dict[str, Any]:
    """生成只承接一次 VC-1 中断的 v3 恢复队列。"""

    payload = {
        "schema_version": CAMPAIGN_RUN_RECOVERY_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_plan_sha256": campaign_plan_sha256,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "batch_sha256": batch_sha256,
        "phase": phase,
        "predecessor_checkpoint": dict(predecessor_checkpoint),
        "original_deadline_at_utc": original_deadline_at_utc,
        "recovery_mode": "interrupted-vc1-preview",
        "recovery_contract": dict(recovery_contract),
        "recovery_predecessor": dict(recovery_predecessor),
        "no_op": not actions,
        "actions": [dict(action) for action in actions],
        "execute_items": list(execute_items),
        "reuse_items": list(reuse_items),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".campaign-run-v3-", suffix=".json"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
        normalized = _campaign_run_manifest(
            temporary,
            require_bound_files=False,
        )
    finally:
        temporary.unlink(missing_ok=True)
    normalized.pop("original_deadline_at_epoch", None)
    return normalized


def build_recovery_continuation_campaign_run_manifest(
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    batch_id: str,
    batch_sequence: int,
    batch_sha256: str,
    phase: str,
    predecessor_checkpoint: Mapping[str, Any],
    original_deadline_at_utc: str,
    recovery_contract: Mapping[str, Any],
    recovery_predecessor: Mapping[str, Any],
    continuation_predecessor: Mapping[str, Any],
    deployment_receipt: Mapping[str, Any],
    maintenance_tool_transition: Mapping[str, Any],
    effective_tool_transition: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    execute_items: Sequence[str],
    reuse_items: Sequence[str],
) -> dict[str, Any]:
    """生成只承接指定 v3 路径缺陷的一次性 v4 零请求续接队列。"""

    payload = {
        "schema_version": CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_plan_sha256": campaign_plan_sha256,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "batch_sha256": batch_sha256,
        "phase": phase,
        "predecessor_checkpoint": dict(predecessor_checkpoint),
        "original_deadline_at_utc": original_deadline_at_utc,
        "recovery_mode": "interrupted-vc1-preview-continuation",
        "recovery_contract": dict(recovery_contract),
        "recovery_predecessor": dict(recovery_predecessor),
        "continuation_predecessor": dict(continuation_predecessor),
        "deployment_receipt": dict(deployment_receipt),
        "maintenance_tool_transition": dict(maintenance_tool_transition),
        "effective_tool_transition": dict(effective_tool_transition),
        "no_op": not actions,
        "actions": [dict(action) for action in actions],
        "execute_items": list(execute_items),
        "reuse_items": list(reuse_items),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".campaign-run-v4-", suffix=".json"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
        normalized = _campaign_run_manifest(
            temporary,
            require_bound_files=False,
        )
    finally:
        temporary.unlink(missing_ok=True)
    normalized.pop("original_deadline_at_epoch", None)
    return normalized


def build_recovery_finalization_campaign_run_manifest(
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    batch_id: str,
    batch_sequence: int,
    batch_sha256: str,
    phase: str,
    predecessor_checkpoint: Mapping[str, Any],
    original_deadline_at_utc: str,
    recovery_contract: Mapping[str, Any],
    recovery_predecessor: Mapping[str, Any],
    continuation_predecessor: Mapping[str, Any],
    continuation_manifest: Mapping[str, Any],
    finalization_predecessor: Mapping[str, Any],
    deployment_receipt: Mapping[str, Any],
    maintenance_tool_transition: Mapping[str, Any],
    effective_tool_transition: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    execute_items: Sequence[str],
    reuse_items: Sequence[str],
) -> dict[str, Any]:
    """生成只承接指定 v4 摘要算法缺陷的一次性 v5 零请求收尾队列。"""

    payload = {
        "schema_version": CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_plan_sha256": campaign_plan_sha256,
        "batch_id": batch_id,
        "batch_sequence": batch_sequence,
        "batch_sha256": batch_sha256,
        "phase": phase,
        "predecessor_checkpoint": dict(predecessor_checkpoint),
        "original_deadline_at_utc": original_deadline_at_utc,
        "recovery_mode": "interrupted-vc1-preview-finalization",
        "recovery_contract": dict(recovery_contract),
        "recovery_predecessor": dict(recovery_predecessor),
        "continuation_predecessor": dict(continuation_predecessor),
        "continuation_manifest": dict(continuation_manifest),
        "finalization_predecessor": dict(finalization_predecessor),
        "deployment_receipt": dict(deployment_receipt),
        "maintenance_tool_transition": dict(maintenance_tool_transition),
        "effective_tool_transition": dict(effective_tool_transition),
        "no_op": not actions,
        "actions": [dict(action) for action in actions],
        "execute_items": list(execute_items),
        "reuse_items": list(reuse_items),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".campaign-run-v5-", suffix=".json"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
        normalized = _campaign_run_manifest(
            temporary,
            require_bound_files=False,
        )
    finally:
        temporary.unlink(missing_ok=True)
    normalized.pop("original_deadline_at_epoch", None)
    return normalized


def _campaign_run_lock(state_dir: Path) -> tuple[int, Path]:
    """为一个动作队列占用唯一锁，防止同一目录并发启动多个父监督器。"""

    state_dir = _validate_state_dir(state_dir, create=True)
    lock_path = state_dir / CAMPAIGN_RUN_LOCK_FILENAME
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise SupervisorError("同一 state-dir 已有动作队列运行，拒绝并发 Campaign。")
    return descriptor, state_dir


def _campaign_run_history(
    state_dir: Path,
    campaign_id: str,
) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    """读取同一 Campaign 已封存队列，拒绝缺清单或摘要不一致的历史。"""

    history: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    for state_path in sorted(state_dir.glob("run-*/state.json")):
        try:
            previous = _read_state(state_path.parent)
        except SupervisorError:
            continue
        if previous.get("campaign_id") != campaign_id:
            continue
        record_path = state_path.parent / "campaign-run-manifest.json"
        if not record_path.is_file() or record_path.is_symlink():
            raise SupervisorError("同一 Campaign 的历史 run 缺少不可变队列清单。")
        record = _read_json(record_path)
        recorded_manifest = record.get("manifest")
        if (
            not isinstance(recorded_manifest, dict)
            or record.get("manifest_sha256")
            != _sha256(_canonical(recorded_manifest))
        ):
            raise SupervisorError("同一 Campaign 的历史队列清单摘要不一致。")
        history.append((previous, recorded_manifest, state_path.parent))
    return history


def _recovery_predecessor_from_run(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """从已封存失败批次重算 v3 必须逐字绑定的直接前序。"""

    state_path = run_dir / "state.json"
    manifest_path = run_dir / "campaign-run-manifest.json"
    stop_path = run_dir / "stop-receipt.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (state_path, manifest_path, stop_path)
    ):
        raise SupervisorError("中断恢复前序缺少可信 state／manifest／stop 收据。")
    stop = _read_json(stop_path)
    if (
        state.get("state") != "failed"
        or stop.get("event_type") != "failed"
        or stop.get("reason") != "KeyboardInterrupt"
        or stop.get("owner_nonce") != state.get("owner_nonce")
        or stop.get("campaign_id") != state.get("campaign_id")
        or manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
    ):
        raise SupervisorError("中断恢复只允许承接 KeyboardInterrupt 的 v2 失败批次。")
    return {
        "run_dir": str(run_dir.resolve(strict=True)),
        "state_sha256": _sha256(state_path.read_bytes()),
        "manifest_sha256": _sha256(manifest_path.read_bytes()),
        "stop_receipt_sha256": _sha256(stop_path.read_bytes()),
        "owner_nonce": str(state["owner_nonce"]),
        "terminal_at_utc": str(state["terminal_at_utc"]),
        "state": "failed",
        "reason": "KeyboardInterrupt",
        "batch_id": str(manifest["batch_id"]),
        "batch_sequence": int(manifest["batch_sequence"]),
        "batch_sha256": str(manifest["batch_sha256"]),
    }


def _recovery_continuation_predecessor_from_run(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """重放本次路径基准缺陷导致的失败 v3，并生成 v4 唯一前序绑定。"""

    state_path = run_dir / "state.json"
    manifest_path = run_dir / "campaign-run-manifest.json"
    stop_path = run_dir / "stop-receipt.json"
    diagnostic_path = _action_diagnostic_path(
        run_dir,
        "recover-vc1-interruption-preview",
        create_directory=False,
    )
    if any(
        path.is_symlink() or not path.is_file()
        for path in (state_path, manifest_path, stop_path, diagnostic_path)
    ):
        raise SupervisorError("v4 续接前序缺少可信 state／manifest／stop／诊断收据。")
    stop = _read_json(stop_path)
    if (
        state.get("state") != "failed"
        or stop.get("event_type") != "failed"
        or stop.get("reason")
        != "action-failed:recover-vc1-interruption-preview"
        or stop.get("owner_nonce") != state.get("owner_nonce")
        or stop.get("campaign_id") != state.get("campaign_id")
        or manifest.get("schema_version") != CAMPAIGN_RUN_RECOVERY_SCHEMA
        or manifest.get("batch_sequence") != 2
    ):
        raise SupervisorError("v4 只允许承接固定路径缺陷导致的唯一失败 v3。")
    diagnostic = _validate_action_diagnostic(
        diagnostic_path,
        run_dir=run_dir,
        campaign_id=str(state["campaign_id"]),
        phase=str(state["phase"]),
        action_id="recover-vc1-interruption-preview",
        owner_pid=int(state["owner_pid"]),
        owner_nonce=str(state["owner_nonce"]),
    )
    if (
        diagnostic.get("failure_kind") != "handled-error"
        or diagnostic.get("error_type") != "ConfigurationError"
        or diagnostic.get("message") != "watchdog heartbeat 越出当前 attempt。"
    ):
        raise SupervisorError("v4 续接前序诊断不是获批的 watchdog 路径缺陷。")
    return {
        "run_dir": str(run_dir.resolve(strict=True)),
        "state_sha256": _sha256(state_path.read_bytes()),
        "manifest_sha256": _sha256(manifest_path.read_bytes()),
        "stop_receipt_sha256": _sha256(stop_path.read_bytes()),
        "action_diagnostic_sha256": _sha256(diagnostic_path.read_bytes()),
        "owner_nonce": str(state["owner_nonce"]),
        "terminal_at_utc": str(state["terminal_at_utc"]),
        "state": "failed",
        "reason": "action-failed:recover-vc1-interruption-preview",
        "error_type": "ConfigurationError",
        "message": "watchdog heartbeat 越出当前 attempt。",
        "batch_id": str(manifest["batch_id"]),
        "batch_sequence": int(manifest["batch_sequence"]),
        "batch_sha256": str(manifest["batch_sha256"]),
    }


def _recovery_finalization_predecessor_from_run(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """重放固定父清单摘要算法缺陷导致的失败 v4。"""

    state_path = run_dir / "state.json"
    manifest_path = run_dir / "campaign-run-manifest.json"
    stop_path = run_dir / "stop-receipt.json"
    diagnostic_path = _action_diagnostic_path(
        run_dir,
        "continue-vc1-interruption-preview",
        create_directory=False,
    )
    if any(
        path.is_symlink() or not path.is_file()
        for path in (state_path, manifest_path, stop_path, diagnostic_path)
    ):
        raise SupervisorError(
            "v5 收尾前序缺少可信 state／manifest／stop／诊断收据。"
        )
    stop = _read_json(stop_path)
    if (
        state.get("state") != "failed"
        or stop.get("event_type") != "failed"
        or stop.get("reason")
        != "action-failed:continue-vc1-interruption-preview"
        or stop.get("owner_nonce") != state.get("owner_nonce")
        or stop.get("campaign_id") != state.get("campaign_id")
        or manifest.get("schema_version")
        != CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA
        or manifest.get("batch_sequence") != 3
    ):
        raise SupervisorError(
            "v5 只允许承接固定父清单摘要算法缺陷导致的唯一失败 v4。"
        )
    diagnostic = _validate_action_diagnostic(
        diagnostic_path,
        run_dir=run_dir,
        campaign_id=str(state["campaign_id"]),
        phase=str(state["phase"]),
        action_id="continue-vc1-interruption-preview",
        owner_pid=int(state["owner_pid"]),
        owner_nonce=str(state["owner_nonce"]),
    )
    if (
        diagnostic.get("failure_kind") != "handled-error"
        or diagnostic.get("error_type") != "ConfigurationError"
        or diagnostic.get("message")
        != "中断恢复续接父 v4 清单、自绑定或动作漂移。"
    ):
        raise SupervisorError(
            "v5 收尾前序诊断不是获批的父清单摘要算法缺陷。"
        )
    return {
        "run_dir": str(run_dir.resolve(strict=True)),
        "state_sha256": _sha256(state_path.read_bytes()),
        "manifest_sha256": _sha256(manifest_path.read_bytes()),
        "stop_receipt_sha256": _sha256(stop_path.read_bytes()),
        "action_diagnostic_sha256": _sha256(diagnostic_path.read_bytes()),
        "owner_nonce": str(state["owner_nonce"]),
        "terminal_at_utc": str(state["terminal_at_utc"]),
        "state": "failed",
        "reason": "action-failed:continue-vc1-interruption-preview",
        "error_type": "ConfigurationError",
        "message": "中断恢复续接父 v4 清单、自绑定或动作漂移。",
        "batch_id": str(manifest["batch_id"]),
        "batch_sequence": int(manifest["batch_sequence"]),
        "batch_sha256": str(manifest["batch_sha256"]),
    }


def _permission_compensation_private_directory(path: Path, label: str) -> None:
    """校验一次性权限补偿控制目录的属主、类型和最小权限。"""

    if not path.is_absolute():
        raise SupervisorError(f"{label} 必须是绝对路径。")
    _reject_symlink_components(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SupervisorError(f"{label} 无法读取。") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SupervisorError(f"{label} 必须是当前执行用户和组拥有的 0700 目录。")


def _permission_compensation_private_file(path: Path, label: str) -> bytes:
    """以 no-follow fd 读取一次性权限补偿文件并锁定 inode 边界。"""

    if not path.is_absolute():
        raise SupervisorError(f"{label} 必须是绝对路径。")
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SupervisorError(f"{label} 无法安全打开。") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            raise SupervisorError(
                f"{label} 必须是当前执行用户和组拥有、无额外硬链接的 0600 普通文件。"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ):
            raise SupervisorError(f"{label} 在读取期间发生漂移。")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _permission_compensation_managed_file(path: Path, label: str) -> bytes:
    """读取 root 管理且不可由 group/other 写入的部署工具文件。"""

    if not path.is_absolute():
        raise SupervisorError(f"{label} 必须是绝对路径。")
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SupervisorError(f"{label} 无法安全打开。") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_gid != os.getegid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
        ):
            raise SupervisorError(
                f"{label} 必须是 root 管理、无额外硬链接且不可由 group/other 写入的普通文件。"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ):
            raise SupervisorError(f"{label} 在读取期间发生漂移。")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _permission_compensation_json(raw: bytes, label: str) -> dict[str, Any]:
    """解析一次性权限补偿绑定的 UTF-8 JSON 对象。"""

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SupervisorError(f"{label} 不是有效 UTF-8 JSON。") from error
    if not isinstance(value, dict):
        raise SupervisorError(f"{label} 顶层必须是对象。")
    return value


def _permission_compensation_events(
    run_dir: Path,
    *,
    campaign_id: str,
    owner_pid: int,
    owner_nonce: str,
) -> list[dict[str, Any]]:
    """只读重放失败批次事件摘要链，并返回绑定同一父身份的事件。"""

    raw = _permission_compensation_private_file(
        run_dir / "events.ndjson",
        "权限补偿前序事件账本",
    )
    records: list[dict[str, Any]] = []
    previous_digest: str | None = None
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise SupervisorError(
                f"权限补偿前序事件账本第 {line_number} 行损坏。"
            ) from error
        if not isinstance(event, dict):
            raise SupervisorError(
                f"权限补偿前序事件账本第 {line_number} 行不是对象。"
            )
        unsigned = dict(event)
        digest = unsigned.pop("event_sha256", None)
        if (
            event.get("schema_version") != EVENT_SCHEMA
            or event.get("sequence") != len(records) + 1
            or event.get("previous_event_sha256") != previous_digest
            or not isinstance(digest, str)
            or digest != _sha256(_canonical(unsigned))
            or event.get("campaign_id") != campaign_id
            or event.get("phase") != "VC-1"
            or event.get("owner_pid") != owner_pid
            or event.get("owner_nonce") != owner_nonce
        ):
            raise SupervisorError("权限补偿前序事件身份或摘要链漂移。")
        records.append(event)
        previous_digest = digest
    if not records:
        raise SupervisorError("权限补偿前序事件账本为空。")
    return records


def _permission_compensation_command_value(
    command: Sequence[str],
    flag: str,
) -> str:
    """从冻结命令中取得唯一旗标值，拒绝缺失、重复或无值。"""

    if command.count(flag) != 1:
        raise SupervisorError(f"权限补偿命令 {flag} 必须且只能出现一次。")
    index = command.index(flag)
    if index + 1 >= len(command):
        raise SupervisorError(f"权限补偿命令 {flag} 缺少值。")
    return command[index + 1]


def _validate_permission_preflight_compensation_successor(
    prior_state: Mapping[str, Any],
    prior_manifest: Mapping[str, Any],
    prior_dir: Path,
    successor_manifest: Mapping[str, Any],
) -> bool:
    """验证 0.154 VC-1 唯一 seal 权限前检补偿 v2 后继。

    返回 ``False`` 表示当前清单根本不是这条一次性边，调用方继续使用原有
    v2→v3 通用失败关闭规则；一旦命中本 Campaign 的 sequence 3，则任一
    身份、文件、动作或收据漂移都会抛错，不能降级为普通 v2 重试。
    """

    if (
        successor_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or successor_manifest.get("campaign_id")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
        or successor_manifest.get("batch_sequence") != 3
    ):
        return False

    campaign_id = VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
    if (
        prior_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or prior_manifest.get("campaign_id") != campaign_id
        or prior_manifest.get("campaign_plan_sha256")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256
        or successor_manifest.get("campaign_plan_sha256")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256
        or prior_manifest.get("batch_sequence") != 2
        or prior_manifest.get("batch_sha256")
        != VC1_PERMISSION_COMPENSATION_FAILED_BATCH_SHA256
        or successor_manifest.get("batch_sha256")
        != VC1_PERMISSION_COMPENSATION_SUCCESSOR_BATCH_SHA256
        or prior_manifest.get("phase") != "VC-1"
        or successor_manifest.get("phase") != "VC-1"
        or prior_manifest.get("predecessor_checkpoint")
        != successor_manifest.get("predecessor_checkpoint")
    ):
        raise SupervisorError("VC-1 权限前检补偿的 Campaign 或批次身份漂移。")

    _permission_compensation_private_directory(
        prior_dir,
        "权限补偿前序 run 目录",
    )
    state_path = prior_dir / "state.json"
    recorded_state = _permission_compensation_json(
        _permission_compensation_private_file(
            state_path,
            "权限补偿前序 state",
        ),
        "权限补偿前序 state",
    )
    if recorded_state != dict(prior_state):
        raise SupervisorError("VC-1 权限前检补偿的前序 state 读取值漂移。")
    owner_pid = prior_state.get("owner_pid")
    owner_nonce = prior_state.get("owner_nonce")
    if (
        prior_state.get("state") != "failed"
        or prior_state.get("campaign_id") != campaign_id
        or prior_state.get("phase") != "VC-1"
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not isinstance(owner_nonce, str)
        or not owner_nonce
    ):
        raise SupervisorError("VC-1 权限前检补偿的前序 state 终态或父身份非法。")

    manifest_record = _permission_compensation_json(
        _permission_compensation_private_file(
            prior_dir / "campaign-run-manifest.json",
            "权限补偿前序 run manifest",
        ),
        "权限补偿前序 run manifest",
    )
    if (
        set(manifest_record) != {"schema_version", "manifest", "manifest_sha256"}
        or manifest_record.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or manifest_record.get("manifest") != dict(prior_manifest)
        or manifest_record.get("manifest_sha256")
        != _sha256(_canonical(dict(prior_manifest)))
    ):
        raise SupervisorError("VC-1 权限前检补偿的前序 run manifest 漂移。")

    stop = _permission_compensation_json(
        _permission_compensation_private_file(
            prior_dir / "stop-receipt.json",
            "权限补偿前序 stop receipt",
        ),
        "权限补偿前序 stop receipt",
    )
    unsigned_stop = dict(stop)
    stop_digest = unsigned_stop.pop("receipt_sha256", None)
    if (
        stop.get("schema_version") != STOP_SCHEMA
        or stop.get("event_type") != "failed"
        or stop.get("reason") != "action-failed:seal-official-preview"
        or stop.get("campaign_id") != campaign_id
        or stop.get("phase") != "VC-1"
        or stop.get("owner_pid") != owner_pid
        or stop.get("owner_nonce") != owner_nonce
        or stop_digest != _sha256(_canonical(unsigned_stop))
    ):
        raise SupervisorError("VC-1 权限前检补偿的前序 stop receipt 漂移。")

    diagnostic_path = _action_diagnostic_path(
        prior_dir,
        "seal-official-preview",
        create_directory=False,
    )
    diagnostic = _validate_action_diagnostic(
        diagnostic_path,
        run_dir=prior_dir,
        campaign_id=campaign_id,
        phase="VC-1",
        action_id="seal-official-preview",
        owner_pid=owner_pid,
        owner_nonce=owner_nonce,
    )
    diagnostic_raw = _permission_compensation_private_file(
        diagnostic_path,
        "权限补偿前序 seal 诊断",
    )
    message_prefix = (
        "seal 廉价前检失败（scanned_bytes=0）：证据目录向 group/other 开放，"
        "必须先修正权限："
    )
    message = diagnostic.get("message")
    if (
        diagnostic.get("diagnostic_sha256")
        != VC1_PERMISSION_COMPENSATION_DIAGNOSTIC_SHA256
        or diagnostic.get("failure_kind") != "handled-error"
        or diagnostic.get("error_type") != "ConfigurationError"
        or not isinstance(message, str)
        or not message.startswith(message_prefix)
        or not message.removeprefix(message_prefix)
    ):
        raise SupervisorError(
            "VC-1 权限前检补偿的失败诊断不是 scanned_bytes=0 权限前检。"
        )

    prior_actions = prior_manifest.get("actions")
    successor_actions = successor_manifest.get("actions")
    if (
        not isinstance(prior_actions, list)
        or len(prior_actions) != 2
        or not isinstance(successor_actions, list)
        or len(successor_actions) != 2
    ):
        raise SupervisorError("VC-1 权限前检补偿的动作数量漂移。")
    prior_prepare, prior_seal = prior_actions
    harden_action, successor_seal = successor_actions
    if not isinstance(prior_seal, dict) or not isinstance(successor_seal, dict):
        raise SupervisorError("VC-1 权限前检补偿的 seal 动作非法。")
    seal_command = prior_seal.get("command")
    if (
        not isinstance(seal_command, list)
        or not all(isinstance(value, str) for value in seal_command)
    ):
        raise SupervisorError("VC-1 权限前检补偿的 seal 命令非法。")
    campaign_dir = Path(
        _permission_compensation_command_value(seal_command, "--campaign-dir")
    )
    attempt_id = _permission_compensation_command_value(
        seal_command,
        "--attempt-id",
    )
    if (
        not campaign_dir.is_absolute()
        or campaign_dir.name != campaign_id
        or campaign_dir.parent.name != "campaigns"
        or campaign_dir.parent.parent.name != "evidence"
        or attempt_id != VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
    ):
        raise SupervisorError("VC-1 权限前检补偿的 Campaign 或 attempt 路径漂移。")
    data_root = campaign_dir.parents[2]
    attempt_root = (
        campaign_dir / "official" / "attempts" / attempt_id
    )
    assertion_root = attempt_root / "evidence" / "assertion-bundle"
    capture_manifest = assertion_root / "capture-manifest.json"
    expected_seal_command = [
        "/usr/bin/python3",
        str(data_root / "tools/official_client_capture/codex_upgrade.py"),
        "capture-official",
        "seal",
        "--campaign-dir",
        str(campaign_dir),
        "--attempt-id",
        attempt_id,
        "--capture-manifest",
        str(capture_manifest),
        "--assertion-evidence-root",
        str(assertion_root),
        "--max-wall-seconds",
        "1440",
        "--heartbeat-seconds",
        "5",
    ]
    expected_seal_action = {
        "action_id": "seal-official-preview",
        "operation": "VC-1:capture-official-seal-preview",
        "timeout_seconds": 1500.0,
        "command": expected_seal_command,
        "item_ids": ["seal-official-preview"],
    }
    expected_prepare_action = {
        "action_id": "prepare-official-assertion-bundle",
        "operation": "VC-1:prepare-official-assertion-bundle",
        "timeout_seconds": 300.0,
        "command": [
            "/usr/bin/env",
            f"CAMPAIGN_DIR={campaign_dir}",
            f"ATTEMPT_ID={attempt_id}",
            "SIDE=official",
            f"REPO_ROOT={data_root}",
            f"TOOL_ROOT={data_root / 'tools/official_client_capture'}",
            "/usr/bin/bash",
            str(data_root / "tools/prepare_assertion_bundle.sh"),
        ],
        "item_ids": ["prepare-official-assertion-bundle"],
    }
    if (
        prior_prepare != expected_prepare_action
        or prior_seal != expected_seal_action
        or successor_seal != prior_seal
        or prior_manifest.get("execute_items")
        != ["prepare-official-assertion-bundle", "seal-official-preview"]
        or prior_manifest.get("reuse_items") != []
        or prior_manifest.get("no_op") is not False
    ):
        raise SupervisorError(
            "VC-1 权限前检补偿未逐字复用原 assertion/seal 动作。"
        )

    events = _permission_compensation_events(
        prior_dir,
        campaign_id=campaign_id,
        owner_pid=owner_pid,
        owner_nonce=owner_nonce,
    )
    prepare_started = [
        event
        for event in events
        if event.get("event_type") == "action-started"
        and event.get("job_id") == "prepare-official-assertion-bundle"
    ]
    prepare_finished = [
        event
        for event in events
        if event.get("event_type") == "action-finished"
        and event.get("job_id") == "prepare-official-assertion-bundle"
    ]
    seal_started = [
        event
        for event in events
        if event.get("event_type") == "action-started"
        and event.get("job_id") == "seal-official-preview"
    ]
    seal_failed = [
        event
        for event in events
        if event.get("event_type") == "action-failed"
        and event.get("job_id") == "seal-official-preview"
    ]
    if (
        len(prepare_started) != 1
        or len(prepare_finished) != 1
        or len(seal_started) != 1
        or len(seal_failed) != 1
        or not (
            prepare_started[0]["sequence"]
            < prepare_finished[0]["sequence"]
            < seal_started[0]["sequence"]
            < seal_failed[0]["sequence"]
        )
        or prepare_finished[0].get("operation")
        != "VC-1:prepare-official-assertion-bundle"
        or prepare_finished[0].get("status") != "passed"
        or not isinstance(prepare_finished[0].get("metadata"), dict)
        or prepare_finished[0]["metadata"].get("returncode") != 0
        or seal_failed[0].get("operation")
        != "VC-1:capture-official-seal-preview"
        or seal_failed[0].get("status") != "failed"
        or seal_failed[0].get("reason") != "returncode=1"
        or any(
            event.get("event_type") == "action-failed"
            and event.get("job_id") == "prepare-official-assertion-bundle"
            for event in events
        )
        or any(
            event.get("event_type") == "action-finished"
            and event.get("job_id") == "seal-official-preview"
            for event in events
        )
    ):
        raise SupervisorError(
            "VC-1 权限前检补偿未证明 assertion bundle 成功且 seal 仅前检失败。"
        )

    _permission_compensation_private_directory(
        assertion_root,
        "权限补偿 assertion bundle 目录",
    )
    _permission_compensation_private_file(
        capture_manifest,
        "权限补偿 capture manifest",
    )
    attempt_path = attempt_root / "attempt.json"
    attempt_raw = _permission_compensation_private_file(
        attempt_path,
        "权限补偿 attempt",
    )
    attempt = _permission_compensation_json(attempt_raw, "权限补偿 attempt")
    evidence_roots = attempt.get("evidence_roots")
    results = attempt.get("results")
    if (
        _sha256(attempt_raw) != VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256
        or attempt.get("campaign_id") != campaign_id
        or attempt.get("attempt_id") != attempt_id
        or attempt.get("status") != "awaiting_receipts"
        or not isinstance(results, list)
        or len(results) != 29
        or any(
            not isinstance(item, dict) or item.get("status") != "complete"
            for item in results
        )
        or not isinstance(evidence_roots, list)
        or len(evidence_roots) != 32
        or any(not isinstance(value, str) for value in evidence_roots)
        or len(set(evidence_roots)) != 32
        or _sha256(
            json.dumps(
                evidence_roots,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        != VC1_PERMISSION_COMPENSATION_ROOTS_SHA256
    ):
        raise SupervisorError("VC-1 权限前检补偿的 attempt、Job 或 32 根摘要漂移。")
    internal_roots = {attempt_root / "evidence", attempt_root / "logs"}
    result_roots: list[str] = []
    for result in results:
        values = result.get("evidence_roots")
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) for value in values)
        ):
            raise SupervisorError("VC-1 权限前检补偿的 Job evidence_roots 不闭合。")
        result_roots.extend(values)
    roots = [Path(value) for value in evidence_roots]
    if set(map(Path, result_roots)) | internal_roots != set(roots):
        raise SupervisorError("VC-1 权限前检补偿的 Job 根与 attempt 根不闭合。")
    for index, root in enumerate(roots):
        if not root.is_absolute():
            raise SupervisorError("VC-1 权限前检补偿包含非绝对证据根。")
        _reject_symlink_components(root)
        try:
            metadata = root.lstat()
        except OSError as error:
            raise SupervisorError("VC-1 权限前检补偿证据根无法读取。") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
        ):
            raise SupervisorError("VC-1 权限前检补偿证据根类型或属主漂移。")
        for other in roots[index + 1 :]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise SupervisorError("VC-1 权限前检补偿证据根发生嵌套。")
    failed_root = Path(message.removeprefix(message_prefix))
    if not failed_root.is_absolute():
        raise SupervisorError("VC-1 权限前检补偿诊断目录不是绝对路径。")
    _reject_symlink_components(failed_root)
    matching_roots = [
        root
        for root in roots
        if failed_root == root or failed_root.is_relative_to(root)
    ]
    try:
        failed_metadata = failed_root.lstat()
    except OSError as error:
        raise SupervisorError("VC-1 权限前检补偿诊断目录无法读取。") from error
    if (
        len(matching_roots) != 1
        or not stat.S_ISDIR(failed_metadata.st_mode)
        or failed_metadata.st_uid != os.geteuid()
        or failed_metadata.st_gid != os.getegid()
        or stat.S_IMODE(failed_metadata.st_mode) & 0o077 == 0
    ):
        raise SupervisorError(
            "VC-1 权限前检补偿诊断目录不在唯一冻结根内或已不再开放。"
        )

    for output in (
        attempt_root / "evidence-manifest.json",
        attempt_root / "seal-draft.json",
        attempt_root / "seal-preview.json",
    ):
        if output.exists() or output.is_symlink():
            raise SupervisorError("VC-1 权限前检补偿前已存在 seal 制品。")

    action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"
    helper_path = action_input_dir / "vc1_evidence_permission_closeout.py"
    action_plan_path = action_input_dir / "vc1-sequence3-action-plan.json"
    receipt_path = action_input_dir / "sequence3-permission-closeout-receipt.json"
    expected_harden_command = [
        "/usr/bin/python3",
        str(helper_path),
        "--campaign-id",
        campaign_id,
        "--attempt-id",
        attempt_id,
        "--attempt",
        str(attempt_path),
        "--attempt-sha256",
        VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
        "--roots-sha256",
        VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
        "--self-sha256",
        VC1_PERMISSION_COMPENSATION_HELPER_SHA256,
        "--receipt",
        str(receipt_path),
    ]
    expected_harden_action = {
        "action_id": "harden-official-evidence-permissions",
        "operation": "VC-1:harden-official-evidence-permissions",
        "timeout_seconds": 120.0,
        "command": expected_harden_command,
        "item_ids": ["harden-official-evidence-permissions"],
    }
    if (
        harden_action != expected_harden_action
        or successor_manifest.get("execute_items")
        != ["harden-official-evidence-permissions", "seal-official-preview"]
        or successor_manifest.get("reuse_items")
        != ["prepare-official-assertion-bundle"]
        or successor_manifest.get("no_op") is not False
    ):
        raise SupervisorError("VC-1 权限前检补偿的 action/reuse 闭集漂移。")

    _permission_compensation_private_directory(
        action_input_dir,
        "权限补偿 action-inputs 目录",
    )
    helper_raw = _permission_compensation_private_file(
        helper_path,
        "权限补偿 helper",
    )
    action_plan_raw = _permission_compensation_private_file(
        action_plan_path,
        "权限补偿 action plan",
    )
    action_plan = _permission_compensation_json(
        action_plan_raw,
        "权限补偿 action plan",
    )
    expected_action_plan = {
        "schema_version": "codex-upgrade-vc-action-plan/v1",
        "execute_item_ids": list(successor_manifest["execute_items"]),
        "reuse_item_ids": list(successor_manifest["reuse_items"]),
        "actions": list(successor_manifest["actions"]),
    }
    if (
        _sha256(helper_raw) != VC1_PERMISSION_COMPENSATION_HELPER_SHA256
        or _sha256(action_plan_raw)
        != VC1_PERMISSION_COMPENSATION_ACTION_PLAN_SHA256
        or action_plan != expected_action_plan
        or receipt_path.exists()
        or receipt_path.is_symlink()
    ):
        raise SupervisorError(
            "VC-1 权限前检补偿的 helper、action plan 或目标 receipt 漂移。"
        )
    return True


def _validate_permission_alias_closeout_successor(
    prior_state: Mapping[str, Any],
    prior_manifest: Mapping[str, Any],
    prior_dir: Path,
    successor_manifest: Mapping[str, Any],
    *,
    historical_frozen: bool = False,
) -> bool:
    """验证只承接本次 sequence 3 EROFS 的唯一 sequence 4。

    sequence 4 仍使用 v2，是因为既有 sequence 3 已不可覆盖且没有恢复扩展字段。
    本函数以现场文件摘要、失败动作链、零 seal 制品、15 项权限缺口和双别名
    inode 边界共同补足直接前序关系；任一漂移都必须停线。
    """

    if (
        successor_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or successor_manifest.get("campaign_id")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
        or successor_manifest.get("batch_sequence") != 4
    ):
        return False

    campaign_id = VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
    if (
        prior_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or prior_manifest.get("campaign_id") != campaign_id
        or successor_manifest.get("campaign_plan_sha256")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256
        or prior_manifest.get("campaign_plan_sha256")
        != VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256
        or prior_manifest.get("batch_id") != "vc-1-0003"
        or prior_manifest.get("batch_sequence") != 3
        or prior_manifest.get("batch_sha256")
        != VC1_PERMISSION_COMPENSATION_SUCCESSOR_BATCH_SHA256
        or successor_manifest.get("batch_id") != "vc-1-0004"
        or prior_manifest.get("phase") != "VC-1"
        or successor_manifest.get("phase") != "VC-1"
        or prior_manifest.get("predecessor_checkpoint")
        != successor_manifest.get("predecessor_checkpoint")
    ):
        raise SupervisorError("VC-1 权限别名收口的 Campaign 或批次身份漂移。")

    if (
        prior_dir.name != VC1_PERMISSION_ALIAS_FAILED_RUN_NAME
        or prior_dir.parent.name != f"{campaign_id}-supervisor"
    ):
        raise SupervisorError("VC-1 权限别名收口没有绑定唯一失败 run 目录。")
    _permission_compensation_private_directory(prior_dir, "权限别名失败 run 目录")
    state_raw = _permission_compensation_private_file(
        prior_dir / "state.json",
        "权限别名失败 state",
    )
    if _sha256(state_raw) != VC1_PERMISSION_ALIAS_STATE_FILE_SHA256:
        raise SupervisorError("VC-1 权限别名失败 state 文件摘要漂移。")
    recorded_state = _permission_compensation_json(
        state_raw,
        "权限别名失败 state",
    )
    owner_pid = prior_state.get("owner_pid")
    owner_nonce = prior_state.get("owner_nonce")
    if (
        recorded_state != dict(prior_state)
        or prior_state.get("state") != "failed"
        or prior_state.get("campaign_id") != campaign_id
        or prior_state.get("phase") != "VC-1"
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not isinstance(owner_nonce, str)
        or owner_nonce != VC1_PERMISSION_ALIAS_FAILED_RUN_NAME.removeprefix("run-")
    ):
        raise SupervisorError("VC-1 权限别名失败 state 终态或父身份漂移。")

    manifest_raw = _permission_compensation_private_file(
        prior_dir / "campaign-run-manifest.json",
        "权限别名失败 run manifest",
    )
    if _sha256(manifest_raw) != VC1_PERMISSION_ALIAS_MANIFEST_FILE_SHA256:
        raise SupervisorError("VC-1 权限别名失败 run manifest 文件摘要漂移。")
    manifest_record = _permission_compensation_json(
        manifest_raw,
        "权限别名失败 run manifest",
    )
    if (
        set(manifest_record) != {"schema_version", "manifest", "manifest_sha256"}
        or manifest_record.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or manifest_record.get("manifest") != dict(prior_manifest)
        or manifest_record.get("manifest_sha256")
        != _sha256(_canonical(dict(prior_manifest)))
    ):
        raise SupervisorError("VC-1 权限别名失败 run manifest 内容漂移。")

    stop_raw = _permission_compensation_private_file(
        prior_dir / "stop-receipt.json",
        "权限别名失败 stop receipt",
    )
    if _sha256(stop_raw) != VC1_PERMISSION_ALIAS_STOP_FILE_SHA256:
        raise SupervisorError("VC-1 权限别名失败 stop receipt 文件摘要漂移。")
    stop = _permission_compensation_json(stop_raw, "权限别名失败 stop receipt")
    unsigned_stop = dict(stop)
    stop_digest = unsigned_stop.pop("receipt_sha256", None)
    if (
        stop.get("schema_version") != STOP_SCHEMA
        or stop.get("event_type") != "failed"
        or stop.get("reason")
        != "action-failed:harden-official-evidence-permissions"
        or stop.get("campaign_id") != campaign_id
        or stop.get("phase") != "VC-1"
        or stop.get("owner_pid") != owner_pid
        or stop.get("owner_nonce") != owner_nonce
        or stop_digest != _sha256(_canonical(unsigned_stop))
    ):
        raise SupervisorError("VC-1 权限别名失败 stop receipt 内容漂移。")

    diagnostic_path = _action_diagnostic_path(
        prior_dir,
        "harden-official-evidence-permissions",
        create_directory=False,
    )
    diagnostic_raw = _permission_compensation_private_file(
        diagnostic_path,
        "权限别名失败动作诊断",
    )
    if _sha256(diagnostic_raw) != VC1_PERMISSION_ALIAS_DIAGNOSTIC_FILE_SHA256:
        raise SupervisorError("VC-1 权限别名失败动作诊断文件摘要漂移。")
    diagnostic = _validate_action_diagnostic(
        diagnostic_path,
        run_dir=prior_dir,
        campaign_id=campaign_id,
        phase="VC-1",
        action_id="harden-official-evidence-permissions",
        owner_pid=owner_pid,
        owner_nonce=owner_nonce,
    )
    if (
        diagnostic.get("diagnostic_sha256")
        != VC1_PERMISSION_ALIAS_FAILURE_DIAGNOSTIC_SHA256
        or diagnostic.get("failure_kind") != "child-returncode"
        or diagnostic.get("error_type") != "ChildProcessError"
        or diagnostic.get("message")
        != "子命令以非零状态退出，未提供进一步的脱敏诊断。"
    ):
        raise SupervisorError("VC-1 权限别名失败动作诊断语义漂移。")

    events_raw = _permission_compensation_private_file(
        prior_dir / "events.ndjson",
        "权限别名失败事件账本",
    )
    if _sha256(events_raw) != VC1_PERMISSION_ALIAS_EVENTS_FILE_SHA256:
        raise SupervisorError("VC-1 权限别名失败事件账本文件摘要漂移。")
    events = _permission_compensation_events(
        prior_dir,
        campaign_id=campaign_id,
        owner_pid=owner_pid,
        owner_nonce=owner_nonce,
    )
    event_summary = [
        (
            event.get("event_type"),
            event.get("job_id"),
            event.get("operation"),
            event.get("status"),
            event.get("reason"),
        )
        for event in events
    ]
    if event_summary != [
        ("command-started", None, "supervisor:start", "running", None),
        (
            "action-started",
            "harden-official-evidence-permissions",
            "VC-1:harden-official-evidence-permissions",
            "running",
            None,
        ),
        (
            "action-failed",
            "harden-official-evidence-permissions",
            "VC-1:harden-official-evidence-permissions",
            "failed",
            "returncode=1",
        ),
        ("stop-requested", None, "supervisor:stop-request", "stopping", "action-failed:harden-official-evidence-permissions"),
        ("failed", None, "supervisor:stop", "failed", "action-failed:harden-official-evidence-permissions"),
    ]:
        raise SupervisorError("VC-1 权限别名失败事件没有证明 seal 从未启动。")

    prior_actions = prior_manifest.get("actions")
    successor_actions = successor_manifest.get("actions")
    if (
        not isinstance(prior_actions, list)
        or len(prior_actions) != 2
        or not isinstance(successor_actions, list)
        or len(successor_actions) != 2
    ):
        raise SupervisorError("VC-1 权限别名收口动作数量漂移。")
    prior_harden, prior_seal = prior_actions
    alias_harden, successor_seal = successor_actions
    if (
        not isinstance(prior_harden, dict)
        or not isinstance(prior_seal, dict)
        or not isinstance(alias_harden, dict)
        or not isinstance(successor_seal, dict)
        or successor_seal != prior_seal
    ):
        raise SupervisorError("VC-1 权限别名收口没有逐字复用原 seal 动作。")
    seal_command = prior_seal.get("command")
    if (
        not isinstance(seal_command, list)
        or not all(isinstance(value, str) for value in seal_command)
    ):
        raise SupervisorError("VC-1 权限别名收口的 seal 命令非法。")
    campaign_dir = Path(
        _permission_compensation_command_value(seal_command, "--campaign-dir")
    )
    attempt_id = _permission_compensation_command_value(seal_command, "--attempt-id")
    if (
        not campaign_dir.is_absolute()
        or campaign_dir.name != campaign_id
        or campaign_dir.parent.name != "campaigns"
        or campaign_dir.parent.parent.name != "evidence"
        or attempt_id != VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
    ):
        raise SupervisorError("VC-1 权限别名收口的 Campaign 或 attempt 路径漂移。")
    data_root = campaign_dir.parents[2]
    attempt_root = campaign_dir / "official" / "attempts" / attempt_id
    attempt_path = attempt_root / "attempt.json"
    assertion_root = attempt_root / "evidence" / "assertion-bundle"
    capture_manifest = assertion_root / "capture-manifest.json"
    expected_seal_action = {
        "action_id": "seal-official-preview",
        "operation": "VC-1:capture-official-seal-preview",
        "timeout_seconds": 1500.0,
        "command": [
            "/usr/bin/python3",
            str(data_root / "tools/official_client_capture/codex_upgrade.py"),
            "capture-official",
            "seal",
            "--campaign-dir",
            str(campaign_dir),
            "--attempt-id",
            attempt_id,
            "--capture-manifest",
            str(capture_manifest),
            "--assertion-evidence-root",
            str(assertion_root),
            "--max-wall-seconds",
            "1440",
            "--heartbeat-seconds",
            "5",
        ],
        "item_ids": ["seal-official-preview"],
    }
    action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"
    old_helper_path = action_input_dir / "vc1_evidence_permission_closeout.py"
    old_receipt_path = action_input_dir / "sequence3-permission-closeout-receipt.json"
    expected_old_harden = {
        "action_id": "harden-official-evidence-permissions",
        "operation": "VC-1:harden-official-evidence-permissions",
        "timeout_seconds": 120.0,
        "command": [
            "/usr/bin/python3",
            str(old_helper_path),
            "--campaign-id",
            campaign_id,
            "--attempt-id",
            attempt_id,
            "--attempt",
            str(attempt_path),
            "--attempt-sha256",
            VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
            "--roots-sha256",
            VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
            "--self-sha256",
            VC1_PERMISSION_COMPENSATION_HELPER_SHA256,
            "--receipt",
            str(old_receipt_path),
        ],
        "item_ids": ["harden-official-evidence-permissions"],
    }
    if (
        prior_harden != expected_old_harden
        or prior_seal != expected_seal_action
        or prior_manifest.get("execute_items")
        != ["harden-official-evidence-permissions", "seal-official-preview"]
        or prior_manifest.get("reuse_items")
        != ["prepare-official-assertion-bundle"]
        or prior_manifest.get("no_op") is not False
    ):
        raise SupervisorError("VC-1 权限别名收口的 sequence 3 动作闭集漂移。")

    _permission_compensation_private_directory(
        action_input_dir,
        "权限别名 action-inputs 目录",
    )
    if (
        _sha256(
            _permission_compensation_private_file(
                old_helper_path,
                "权限别名旧 helper",
            )
        )
        != VC1_PERMISSION_COMPENSATION_HELPER_SHA256
        or _sha256(
            _permission_compensation_private_file(
                action_input_dir / "vc1-sequence3-action-plan.json",
                "权限别名旧 action plan",
            )
        )
        != VC1_PERMISSION_COMPENSATION_ACTION_PLAN_SHA256
        or old_receipt_path.exists()
        or old_receipt_path.is_symlink()
    ):
        raise SupervisorError("VC-1 权限别名收口的 sequence 3 输入或收据漂移。")
    _permission_compensation_private_directory(
        assertion_root,
        "权限别名 assertion bundle 目录",
    )
    _permission_compensation_private_file(
        capture_manifest,
        "权限别名 capture manifest",
    )
    for output in (
        attempt_root / "evidence-manifest.json",
        attempt_root / "seal-draft.json",
        attempt_root / "seal-preview.json",
    ):
        if output.exists() or output.is_symlink():
            raise SupervisorError("VC-1 权限别名收口前已存在 seal 制品。")

    try:
        permission_alias_closeout.inspect_permission_boundary(
            attempt_path=attempt_path,
            campaign_id=campaign_id,
            attempt_id=attempt_id,
            attempt_sha256=VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
            roots_sha256=VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
            readonly_runs_root=VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT,
            writable_runs_root=data_root / "runs",
            require_mount_modes=True,
            expected_entry_count=VC1_PERMISSION_ALIAS_ENTRY_COUNT,
            expected_gap_count=VC1_PERMISSION_ALIAS_GAP_COUNT,
            expected_gap_sha256=VC1_PERMISSION_ALIAS_GAP_SHA256,
        )
    except permission_alias_closeout.PermissionAliasCloseoutError as error:
        raise SupervisorError(f"VC-1 权限别名边界前检失败：{error}") from error

    alias_command = alias_harden.get("command")
    if (
        not isinstance(alias_command, list)
        or not all(isinstance(value, str) for value in alias_command)
    ):
        raise SupervisorError("VC-1 权限别名 helper 命令非法。")
    deployment_path = Path(
        _permission_compensation_command_value(alias_command, "--deployment-receipt")
    )
    deployment_sha256 = _permission_compensation_command_value(
        alias_command,
        "--deployment-receipt-sha256",
    )
    tool_files_sha256 = _permission_compensation_command_value(
        alias_command,
        "--tool-files-sha256",
    )
    if (
        deployment_path.parent != data_root / "control"
        or not deployment_path.name.startswith("codex-0154-supervisor-enable-")
        or not deployment_path.name.endswith(".json")
    ):
        raise SupervisorError("VC-1 权限别名部署收据坐标漂移。")
    deployment_raw = _permission_compensation_private_file(
        deployment_path,
        "权限别名部署收据",
    )
    deployment = _permission_compensation_json(
        deployment_raw,
        "权限别名部署收据",
    )
    helper_path = (
        data_root
        / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_closeout.py"
    )
    deployment_common_valid = (
        _sha256(deployment_raw) == deployment_sha256
        and deployment.get("schema_version") == "codex-arm64-supervisor-enable/v1"
        and deployment.get("status") == "passed"
        and deployment.get("architecture") == "aarch64"
        and deployment.get("production_tool_root")
        == str(data_root / "tools/official_client_capture")
        and deployment.get("tool_files_sha256") == tool_files_sha256
    )
    if historical_frozen:
        deployment_valid = (
            deployment_common_valid
            and deployment_sha256
            == VC1_PERMISSION_ALIAS_PREDISPATCH_DEPLOYMENT_SHA256
            and tool_files_sha256
            == VC1_PERMISSION_ALIAS_PREDISPATCH_TOOL_FILES_SHA256
            and deployment.get("supervisor_sha256")
            == VC1_PERMISSION_ALIAS_PREDISPATCH_SUPERVISOR_SHA256
        )
    else:
        helper_raw = _permission_compensation_managed_file(
            helper_path,
            "权限别名新 helper",
        )
        deployment_valid = (
            deployment_common_valid
            and _sha256(helper_raw) == VC1_PERMISSION_ALIAS_HELPER_SHA256
            and deployment.get("supervisor_sha256")
            == _sha256(Path(__file__).read_bytes())
        )
    if not deployment_valid:
        raise SupervisorError("VC-1 权限别名部署收据没有绑定当前工具。")

    receipt_path = action_input_dir / "sequence4-permission-alias-closeout-receipt.json"
    expected_alias_action = {
        "action_id": "harden-official-evidence-permissions-via-alias",
        "operation": "VC-1:harden-official-evidence-permissions-via-alias",
        "timeout_seconds": 180.0,
        "command": [
            "/usr/bin/python3",
            str(helper_path),
            "--campaign-id",
            campaign_id,
            "--attempt-id",
            attempt_id,
            "--attempt",
            str(attempt_path),
            "--attempt-sha256",
            VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
            "--roots-sha256",
            VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
            "--self-sha256",
            VC1_PERMISSION_ALIAS_HELPER_SHA256,
            "--readonly-runs-root",
            str(VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT),
            "--writable-runs-root",
            str(data_root / "runs"),
            "--deployment-receipt",
            str(deployment_path),
            "--deployment-receipt-sha256",
            deployment_sha256,
            "--tool-files-sha256",
            tool_files_sha256,
            "--receipt",
            str(receipt_path),
        ],
        "item_ids": ["harden-official-evidence-permissions-via-alias"],
    }
    if (
        alias_harden != expected_alias_action
        or successor_manifest.get("execute_items")
        != [
            "harden-official-evidence-permissions-via-alias",
            "seal-official-preview",
        ]
        or successor_manifest.get("reuse_items")
        != ["prepare-official-assertion-bundle"]
        or successor_manifest.get("no_op") is not False
        or receipt_path.exists()
        or receipt_path.is_symlink()
    ):
        raise SupervisorError("VC-1 权限别名 sequence 4 动作或收据漂移。")
    action_plan_raw = _permission_compensation_private_file(
        action_input_dir / "vc1-sequence4-action-plan.json",
        "权限别名 sequence 4 action plan",
    )
    action_plan = _permission_compensation_json(
        action_plan_raw,
        "权限别名 sequence 4 action plan",
    )
    if action_plan != {
        "schema_version": "codex-upgrade-vc-action-plan/v1",
        "execute_item_ids": list(successor_manifest["execute_items"]),
        "reuse_item_ids": list(successor_manifest["reuse_items"]),
        "actions": list(successor_manifest["actions"]),
    }:
        raise SupervisorError("VC-1 权限别名 action plan 与批次漂移。")
    return True


def _validate_permission_alias_predispatch_successor(
    prior_state: Mapping[str, Any],
    prior_manifest: Mapping[str, Any],
    prior_dir: Path,
    successor_manifest: Mapping[str, Any],
    ordered_history: Sequence[tuple[dict[str, Any], dict[str, Any], Path]],
) -> bool:
    """验证唯一未派发 sequence 4 经零请求收据承接到 sequence 5。"""

    campaign_id = VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
    if (
        successor_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or successor_manifest.get("campaign_id") != campaign_id
        or successor_manifest.get("batch_id") != "vc-1-0005"
        or successor_manifest.get("batch_sequence") != 5
        or successor_manifest.get("phase") != "VC-1"
        or len(ordered_history) != 3
        or [item[1].get("batch_sequence") for item in ordered_history]
        != [1, 2, 3]
    ):
        return False

    prior_actions = prior_manifest.get("actions")
    successor_actions = successor_manifest.get("actions")
    if (
        not isinstance(prior_actions, list)
        or len(prior_actions) != 2
        or not isinstance(successor_actions, list)
        or len(successor_actions) != 2
    ):
        raise SupervisorError("VC-1 权限别名预派发恢复动作数量漂移。")
    seal_action = prior_actions[1]
    if not isinstance(seal_action, dict) or successor_actions[1] != seal_action:
        raise SupervisorError("VC-1 权限别名预派发恢复没有逐字复用 seal 动作。")
    seal_command = seal_action.get("command")
    if not isinstance(seal_command, list) or not all(
        isinstance(value, str) for value in seal_command
    ):
        raise SupervisorError("VC-1 权限别名预派发恢复的 seal 命令非法。")
    campaign_dir = Path(
        _permission_compensation_command_value(seal_command, "--campaign-dir")
    )
    data_root = campaign_dir.parents[2]
    action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"
    batch_path = campaign_dir / "control/vc/batches/0004-vc-1.json"
    manifest_path = campaign_dir / "control/vc/run-manifests/0004-vc-1.json"
    batch_raw = _permission_compensation_private_file(
        batch_path,
        "权限别名 sequence 4 batch",
    )
    frozen_manifest_raw = _permission_compensation_private_file(
        manifest_path,
        "权限别名 sequence 4 run manifest",
    )
    if (
        _sha256(batch_raw)
        != VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_FILE_SHA256
        or _sha256(frozen_manifest_raw)
        != VC1_PERMISSION_ALIAS_PREDISPATCH_MANIFEST_FILE_SHA256
        or _sha256(
            _permission_compensation_private_file(
                action_input_dir / "vc1-sequence4-action-plan.json",
                "权限别名 sequence 4 action plan",
            )
        )
        != VC1_PERMISSION_ALIAS_PREDISPATCH_ACTION_PLAN_SHA256
    ):
        raise SupervisorError("VC-1 权限别名未派发 sequence 4 制品摘要漂移。")
    batch = _permission_compensation_json(batch_raw, "权限别名 sequence 4 batch")
    frozen_manifest = _permission_compensation_json(
        frozen_manifest_raw,
        "权限别名 sequence 4 run manifest",
    )
    if (
        batch.get("sequence") != 4
        or batch.get("batch_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_SHA256
        or batch.get("compiled_at_utc")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_COMPILED_AT_UTC
        or batch.get("must_start_by_utc")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_MUST_START_BY_UTC
        or frozen_manifest.get("batch_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_SHA256
        or successor_manifest.get("predecessor_checkpoint")
        != frozen_manifest.get("predecessor_checkpoint")
    ):
        raise SupervisorError("VC-1 权限别名未派发 sequence 4 身份漂移。")
    try:
        must_start_by = datetime.fromisoformat(
            VC1_PERMISSION_ALIAS_PREDISPATCH_MUST_START_BY_UTC
        ).timestamp()
    except ValueError as error:
        raise SupervisorError("VC-1 权限别名 sequence 4 启动窗口非法。") from error
    if time.time() <= must_start_by:
        raise SupervisorError("VC-1 权限别名 sequence 4 尚未过启动窗口。")

    # 历史模式仍重放 sequence 3 的全部一次性锚点、旧 action plan、旧部署
    # 收据和当前 1,882 项元数据边界，但不要求生产树继续保留旧 helper。
    if not _validate_permission_alias_closeout_successor(
        prior_state,
        prior_manifest,
        prior_dir,
        frozen_manifest,
        historical_frozen=True,
    ):
        raise SupervisorError("VC-1 权限别名未派发 sequence 4 无法重放。")

    receipt_path = action_input_dir / "sequence4-predispatch-closeout-receipt.json"
    receipt_raw = _permission_compensation_private_file(
        receipt_path,
        "权限别名 sequence 4 预派发封口收据",
    )
    receipt = _permission_compensation_json(
        receipt_raw,
        "权限别名 sequence 4 预派发封口收据",
    )
    unsigned_receipt = dict(receipt)
    receipt_sha256 = unsigned_receipt.pop("receipt_sha256", None)
    try:
        receipt_created_at = datetime.fromisoformat(
            str(receipt.get("created_at_utc")).replace("Z", "+00:00")
        ).timestamp()
    except ValueError as error:
        raise SupervisorError("VC-1 权限别名预派发封口时间非法。") from error
    if receipt_created_at <= must_start_by or receipt_created_at > time.time() + 5:
        raise SupervisorError("VC-1 权限别名预派发封口时间越出允许窗口。")
    expected_run_history = []
    for state, manifest, run_dir in ordered_history:
        del state
        run_raw = _permission_compensation_private_file(
            run_dir / "campaign-run-manifest.json",
            "权限别名历史 run manifest",
        )
        expected_run_history.append(
            {
                "batch_sequence": manifest.get("batch_sequence"),
                "run_name": run_dir.name,
                "manifest_file_sha256": _sha256(run_raw),
            }
        )
    try:
        boundary = permission_alias_closeout.inspect_permission_boundary(
            attempt_path=(
                campaign_dir
                / "official/attempts"
                / VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
                / "attempt.json"
            ),
            campaign_id=campaign_id,
            attempt_id=VC1_PERMISSION_COMPENSATION_ATTEMPT_ID,
            attempt_sha256=VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
            roots_sha256=VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
            readonly_runs_root=VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT,
            writable_runs_root=data_root / "runs",
            require_mount_modes=True,
            expected_entry_count=VC1_PERMISSION_ALIAS_ENTRY_COUNT,
            expected_gap_count=VC1_PERMISSION_ALIAS_GAP_COUNT,
            expected_gap_sha256=VC1_PERMISSION_ALIAS_GAP_SHA256,
        )
    except permission_alias_closeout.PermissionAliasCloseoutError as error:
        raise SupervisorError(
            f"VC-1 权限别名 sequence 5 边界前检失败：{error}"
        ) from error
    deployment_path_text = receipt.get("current_deployment_receipt")
    if not isinstance(deployment_path_text, str):
        raise SupervisorError("VC-1 权限别名预派发收据缺少当前部署坐标。")
    deployment_path = Path(deployment_path_text)
    deployment_raw = _permission_compensation_private_file(
        deployment_path,
        "权限别名 sequence 5 部署收据",
    )
    deployment = _permission_compensation_json(
        deployment_raw,
        "权限别名 sequence 5 部署收据",
    )
    helper_path = (
        data_root
        / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_closeout.py"
    )
    closeout_tool_path = (
        data_root
        / "tools/official_client_capture/"
        "codex_upgrade_vc1_permission_alias_predispatch_closeout.py"
    )
    helper_raw = _permission_compensation_managed_file(
        helper_path,
        "权限别名 sequence 5 helper",
    )
    closeout_tool_raw = _permission_compensation_managed_file(
        closeout_tool_path,
        "权限别名预派发封口工具",
    )
    if (
        receipt_sha256 != _sha256(_legacy_compact_canonical(unsigned_receipt))
        or receipt.get("schema_version")
        != "codex-vc1-permission-alias-predispatch-closeout/v1"
        or receipt.get("status") != "passed"
        or receipt.get("campaign_id") != campaign_id
        or receipt.get("attempt_id") != VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
        or receipt.get("source_sequence") != 4
        or receipt.get("source_batch_file_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_FILE_SHA256
        or receipt.get("source_batch_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_SHA256
        or receipt.get("source_manifest_file_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_MANIFEST_FILE_SHA256
        or receipt.get("source_action_plan_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_ACTION_PLAN_SHA256
        or receipt.get("source_failed_deployment_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_DEPLOYMENT_SHA256
        or receipt.get("source_compiled_at_utc")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_COMPILED_AT_UTC
        or receipt.get("source_must_start_by_utc")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_MUST_START_BY_UTC
        or receipt.get("source_actions")
        != ["harden-official-evidence-permissions-via-alias", "seal-official-preview"]
        or receipt.get("source_run_created") is not False
        or receipt.get("source_run_history") != expected_run_history
        or receipt.get("deterministic_error_type")
        != "PermissionAliasCloseoutError"
        or receipt.get("deterministic_error")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_ERROR
        or receipt.get("historical_helper_deployment_receipt")
        != str(VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_PATH)
        or receipt.get("historical_helper_deployment_receipt_sha256")
        != VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_SHA256
        or receipt.get("historical_helper_rollback")
        != str(VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_ROLLBACK_PATH)
        or receipt.get("historical_helper_sha256")
        != VC1_PERMISSION_ALIAS_HELPER_SHA256
        or receipt.get("current_deployment_receipt_sha256")
        != _sha256(deployment_raw)
        or receipt.get("current_tool_files_sha256")
        != deployment.get("tool_files_sha256")
        or receipt.get("closeout_tool_sha256")
        != VC1_PERMISSION_ALIAS_PREDISPATCH_CLOSEOUT_TOOL_SHA256
        or _sha256(closeout_tool_raw)
        != VC1_PERMISSION_ALIAS_PREDISPATCH_CLOSEOUT_TOOL_SHA256
        or receipt.get("current_helper_sha256")
        != VC1_PERMISSION_ALIAS_V2_HELPER_SHA256
        or _sha256(helper_raw) != VC1_PERMISSION_ALIAS_V2_HELPER_SHA256
        or receipt.get("boundary")
        != {
            "entry_count": len(boundary.entries),
            "gap_count": len(boundary.gaps),
            "gap_sha256": boundary.gap_sha256,
            "stable_boundary_sha256": boundary.boundary_sha256,
        }
        or receipt.get("scanned_bytes") != 0
        or receipt.get("live_request_count") != 0
        or deployment.get("schema_version")
        != "codex-arm64-supervisor-enable/v1"
        or deployment.get("status") != "passed"
        or deployment.get("architecture") != "aarch64"
        or deployment.get("production_tool_root")
        != str(data_root / "tools/official_client_capture")
        or deployment.get("supervisor_sha256")
        != _sha256(Path(__file__).read_bytes())
    ):
        raise SupervisorError("VC-1 权限别名预派发封口收据或当前工具漂移。")

    alias_action = successor_actions[0]
    if not isinstance(alias_action, dict):
        raise SupervisorError("VC-1 权限别名 sequence 5 动作非法。")
    deployment_sha256 = receipt["current_deployment_receipt_sha256"]
    tool_files_sha256 = receipt["current_tool_files_sha256"]
    permission_receipt_path = (
        action_input_dir / "sequence5-permission-alias-closeout-receipt.json"
    )
    expected_alias_action = {
        "action_id": "harden-official-evidence-permissions-via-alias-v2",
        "operation": "VC-1:harden-official-evidence-permissions-via-alias-v2",
        "timeout_seconds": 180.0,
        "command": [
            "/usr/bin/python3",
            str(helper_path),
            "--campaign-id",
            campaign_id,
            "--attempt-id",
            VC1_PERMISSION_COMPENSATION_ATTEMPT_ID,
            "--attempt",
            str(
                campaign_dir
                / "official/attempts"
                / VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
                / "attempt.json"
            ),
            "--attempt-sha256",
            VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256,
            "--roots-sha256",
            VC1_PERMISSION_COMPENSATION_ROOTS_SHA256,
            "--self-sha256",
            VC1_PERMISSION_ALIAS_V2_HELPER_SHA256,
            "--readonly-runs-root",
            str(VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT),
            "--writable-runs-root",
            str(data_root / "runs"),
            "--deployment-receipt",
            str(deployment_path),
            "--deployment-receipt-sha256",
            deployment_sha256,
            "--tool-files-sha256",
            tool_files_sha256,
            "--receipt",
            str(permission_receipt_path),
        ],
        "item_ids": ["harden-official-evidence-permissions-via-alias-v2"],
    }
    if (
        alias_action != expected_alias_action
        or successor_manifest.get("execute_items")
        != [
            "harden-official-evidence-permissions-via-alias-v2",
            "seal-official-preview",
        ]
        or successor_manifest.get("reuse_items")
        != ["prepare-official-assertion-bundle"]
        or successor_manifest.get("no_op") is not False
        or permission_receipt_path.exists()
        or permission_receipt_path.is_symlink()
    ):
        raise SupervisorError("VC-1 权限别名 sequence 5 动作或输出漂移。")
    if (
        deployment_path.parent != data_root / "control"
        or not deployment_path.name.startswith("codex-0154-supervisor-enable-")
        or not deployment_path.name.endswith(".json")
        or deployment.get("tool_files_sha256") != tool_files_sha256
    ):
        raise SupervisorError("VC-1 权限别名 sequence 5 部署坐标漂移。")
    action_plan = _permission_compensation_json(
        _permission_compensation_private_file(
            action_input_dir / "vc1-sequence5-action-plan.json",
            "权限别名 sequence 5 action plan",
        ),
        "权限别名 sequence 5 action plan",
    )
    if action_plan != {
        "schema_version": "codex-upgrade-vc-action-plan/v1",
        "execute_item_ids": list(successor_manifest["execute_items"]),
        "reuse_item_ids": list(successor_manifest["reuse_items"]),
        "actions": list(successor_manifest["actions"]),
    }:
        raise SupervisorError("VC-1 权限别名 sequence 5 action plan 漂移。")
    return True


def _validate_batched_official_recovery_preview_successor(
    prior_state: Mapping[str, Any],
    prior_manifest: Mapping[str, Any],
    prior_dir: Path,
    successor_manifest: Mapping[str, Any],
) -> bool:
    """允许完整失败 attempt 进入普通 v2 的零请求恢复预览。

    这里只开放父监督器的结构边界：sequence 1 必须是唯一失败的
    ``capture-official``，sequence 2 必须把原 execute 集合无遗漏地划分为
    execute／reuse，并且唯一动作只能执行 ``resume --rerun-failed
    --preview-recovery``。源 attempt、自摘要、工具影响和实际恢复闭集仍由动作
    内的恢复器逐字复算；本层不得读取正文或写死某一次事故的数量。
    """

    successor_actions = successor_manifest.get("actions")
    if (
        successor_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or successor_manifest.get("phase") != "VC-1"
        or successor_manifest.get("batch_sequence") != 2
        or not isinstance(successor_actions, list)
        or len(successor_actions) != 1
        or not isinstance(successor_actions[0], Mapping)
    ):
        return False
    successor_action = successor_actions[0]
    successor_command = successor_action.get("command")
    if (
        not isinstance(successor_command, list)
        or successor_command.count("resume") != 1
        or successor_command.count("--preview-recovery") != 1
    ):
        return False

    resume_index = successor_command.index("resume")
    successor_prefix = successor_command[:resume_index]
    successor_tail = successor_command[resume_index:]
    if (
        resume_index not in {1, 2}
        or not successor_prefix
        or not {
            Path(value).name for value in successor_prefix
        }
        & {"codex_upgrade.py", "codex-upgrade"}
        or len(successor_tail) != 5
        or successor_tail[0] != "resume"
        or successor_tail[1] != "--campaign-dir"
        or not Path(successor_tail[2]).is_absolute()
        or successor_tail[3:] != ["--rerun-failed", "--preview-recovery"]
        or "--acknowledge-live-requests" in successor_command
    ):
        raise SupervisorError("VC-1 普通恢复预览动作或零请求边界漂移。")
    campaign_dir = Path(successor_tail[2])

    prior_actions = prior_manifest.get("actions")
    prior_execute = prior_manifest.get("execute_items")
    successor_execute = successor_manifest.get("execute_items")
    successor_reuse = successor_manifest.get("reuse_items")
    if (
        prior_manifest.get("schema_version") != CAMPAIGN_RUN_BATCHED_SCHEMA
        or prior_manifest.get("campaign_id") != successor_manifest.get("campaign_id")
        or prior_manifest.get("phase") != "VC-1"
        or prior_manifest.get("batch_sequence") != 1
        or prior_manifest.get("campaign_plan_sha256")
        != successor_manifest.get("campaign_plan_sha256")
        or prior_manifest.get("original_deadline_at_utc")
        != successor_manifest.get("original_deadline_at_utc")
        or prior_manifest.get("predecessor_checkpoint")
        != successor_manifest.get("predecessor_checkpoint")
        or prior_manifest.get("no_op") is not False
        or successor_manifest.get("no_op") is not False
        or prior_manifest.get("reuse_items") != []
        or not isinstance(prior_actions, list)
        or len(prior_actions) != 1
        or not isinstance(prior_actions[0], Mapping)
        or not isinstance(prior_execute, list)
        or not prior_execute
        or not isinstance(successor_execute, list)
        or not successor_execute
        or not isinstance(successor_reuse, list)
        or set(successor_execute) & set(successor_reuse)
        or sorted(successor_execute + successor_reuse) != sorted(prior_execute)
        or successor_action.get("item_ids") != successor_execute
        or successor_action.get("operation") != "VC-1:official-recovery"
    ):
        raise SupervisorError("VC-1 普通恢复预览的父批次身份或 execute/reuse 分区漂移。")

    prior_action = prior_actions[0]
    prior_command = prior_action.get("command")
    if not isinstance(prior_command, list) or prior_command.count("capture-official") != 1:
        raise SupervisorError("VC-1 普通恢复预览缺少唯一 capture-official 父动作。")
    capture_index = prior_command.index("capture-official")
    prior_prefix = prior_command[:capture_index]
    prior_tail = prior_command[capture_index:]
    if (
        prior_prefix != successor_prefix
        or prior_tail
        != [
            "capture-official",
            "run",
            "--campaign-dir",
            str(campaign_dir),
            "--acknowledge-live-requests",
        ]
        or prior_action.get("action_id") != "capture-official"
        or prior_action.get("operation") != "VC-1:capture-official"
        or prior_action.get("item_ids") != prior_execute
    ):
        raise SupervisorError("VC-1 普通恢复预览的 capture-official 父动作漂移。")

    _permission_compensation_private_directory(
        prior_dir,
        "VC-1 普通恢复预览前序 run 目录",
    )
    recorded_state = _permission_compensation_json(
        _permission_compensation_private_file(
            prior_dir / "state.json",
            "VC-1 普通恢复预览前序 state",
        ),
        "VC-1 普通恢复预览前序 state",
    )
    owner_pid = prior_state.get("owner_pid")
    owner_nonce = prior_state.get("owner_nonce")
    if (
        recorded_state != dict(prior_state)
        or prior_state.get("state") != "failed"
        or prior_state.get("campaign_id") != successor_manifest.get("campaign_id")
        or prior_state.get("phase") != "VC-1"
        or isinstance(owner_pid, bool)
        or not isinstance(owner_pid, int)
        or owner_pid <= 0
        or not isinstance(owner_nonce, str)
        or not owner_nonce
    ):
        raise SupervisorError("VC-1 普通恢复预览的父终态或 owner 身份漂移。")

    stop = _permission_compensation_json(
        _permission_compensation_private_file(
            prior_dir / "stop-receipt.json",
            "VC-1 普通恢复预览前序 stop receipt",
        ),
        "VC-1 普通恢复预览前序 stop receipt",
    )
    unsigned_stop = dict(stop)
    stop_digest = unsigned_stop.pop("receipt_sha256", None)
    if (
        stop.get("schema_version") != STOP_SCHEMA
        or stop.get("event_type") != "failed"
        or stop.get("reason") != "action-failed:capture-official"
        or stop.get("campaign_id") != prior_state.get("campaign_id")
        or stop.get("phase") != "VC-1"
        or stop.get("owner_pid") != owner_pid
        or stop.get("owner_nonce") != owner_nonce
        or stop_digest != _sha256(_canonical(unsigned_stop))
    ):
        raise SupervisorError("VC-1 普通恢复预览的父 stop receipt 漂移。")

    diagnostic = _validate_action_diagnostic(
        _action_diagnostic_path(
            prior_dir,
            "capture-official",
            create_directory=False,
        ),
        run_dir=prior_dir,
        campaign_id=str(prior_state["campaign_id"]),
        phase="VC-1",
        action_id="capture-official",
        owner_pid=owner_pid,
        owner_nonce=owner_nonce,
    )
    if (
        diagnostic.get("failure_kind") != "child-returncode"
        or diagnostic.get("error_type") != "ChildProcessError"
        or diagnostic.get("message")
        != "子命令以非零状态退出，未提供进一步的脱敏诊断。"
    ):
        raise SupervisorError("VC-1 普通恢复预览的父动作诊断漂移。")
    return True


def _validate_batched_campaign_history(
    manifest: Mapping[str, Any],
    history: Sequence[tuple[dict[str, Any], dict[str, Any], Path]],
) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    """校验 v2/v3/v4/v5 连续链和获批的唯一失败后继。"""

    if any(
        prior_manifest.get("schema_version")
        not in {
            CAMPAIGN_RUN_BATCHED_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }
        for _state, prior_manifest, _run_dir in history
    ):
        raise SupervisorError("batched Campaign 不得与历史 v1 run 混用。")
    ordered = sorted(
        history,
        key=lambda item: int(item[1].get("batch_sequence", 0)),
    )
    sequences = [
        int(prior_manifest.get("batch_sequence", 0))
        for _state, prior_manifest, _run_dir in ordered
    ]
    predispatch_successor = False
    if (
        int(manifest["batch_sequence"]) == 5
        and sequences == [1, 2, 3]
        and ordered
    ):
        last_state, last_manifest, last_dir = ordered[-1]
        predispatch_successor = _validate_permission_alias_predispatch_successor(
            last_state,
            last_manifest,
            last_dir,
            manifest,
            ordered,
        )
    expected_prior = (
        [1, 2, 3]
        if predispatch_successor
        else list(range(1, int(manifest["batch_sequence"])))
    )
    if sequences != expected_prior:
        raise SupervisorError(
            "Campaign batch_sequence 必须从 1 连续递增，禁止跳批、重复或回退。"
        )
    if any(
        prior_manifest.get("original_deadline_at_utc")
        != manifest["original_deadline_at_utc"]
        or prior_manifest.get("campaign_plan_sha256")
        != manifest["campaign_plan_sha256"]
        for _state, prior_manifest, _run_dir in ordered
    ):
        raise SupervisorError("Campaign 后继批次改变了总计划或原始 deadline。")

    # 把待执行清单临时拼到链尾，只校验身份，不把它当作已有终态。这样既能
    # 约束当前 v3/v4/v5 的直接前序，也能验证历史特殊清单没有被普通 v2 绕过。
    combined: list[tuple[dict[str, Any] | None, Mapping[str, Any], Path | None]] = [
        (state, prior_manifest, run_dir)
        for state, prior_manifest, run_dir in ordered
    ]
    combined.append((None, manifest, None))
    for index, (state, current_manifest, run_dir) in enumerate(combined):
        schema = current_manifest.get("schema_version")
        if schema == CAMPAIGN_RUN_RECOVERY_SCHEMA:
            if index == 0:
                raise SupervisorError("v3 恢复批次缺少直接失败 v2 前序。")
            previous_state, previous_manifest, previous_dir = combined[index - 1]
            if (
                previous_state is None
                or previous_dir is None
                or previous_manifest.get("schema_version")
                != CAMPAIGN_RUN_BATCHED_SCHEMA
                or previous_state.get("state") != "failed"
                or current_manifest.get("recovery_predecessor")
                != _recovery_predecessor_from_run(
                    previous_state,
                    previous_manifest,
                    previous_dir,
                )
            ):
                raise SupervisorError("v3 恢复清单未逐字绑定唯一直接失败 v2 前序。")
        elif schema == CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA:
            # v4 不是可复用的通用恢复版本，只承接本 Campaign 的 sequence 1/2
            # 特定失败链；历史 v4 成功后，sequence 4 才能回到普通 v2。
            if index != 2:
                raise SupervisorError("v4 续接只允许位于唯一 sequence 3。")
            original_state, original_manifest, original_dir = combined[0]
            failed_state, failed_manifest, failed_dir = combined[1]
            if (
                original_state is None
                or original_dir is None
                or failed_state is None
                or failed_dir is None
                or original_manifest.get("schema_version")
                != CAMPAIGN_RUN_BATCHED_SCHEMA
                or failed_manifest.get("schema_version")
                != CAMPAIGN_RUN_RECOVERY_SCHEMA
                or original_state.get("state") != "failed"
                or failed_state.get("state") != "failed"
                or current_manifest.get("recovery_predecessor")
                != _recovery_predecessor_from_run(
                    original_state,
                    original_manifest,
                    original_dir,
                )
                or current_manifest.get("continuation_predecessor")
                != _recovery_continuation_predecessor_from_run(
                    failed_state,
                    failed_manifest,
                    failed_dir,
                )
            ):
                raise SupervisorError("v4 没有逐字绑定获批的 v2/v3 双前序。")
        elif schema == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA:
            # v5 只承接本次已封存的 sequence 3 父清单摘要算法缺陷；它不是
            # 通用重试通道，成功后 sequence 5 才能回到普通 v2。
            if index != 3:
                raise SupervisorError("v5 收尾只允许位于唯一 sequence 4。")
            original_state, original_manifest, original_dir = combined[0]
            recovery_state, recovery_manifest, recovery_dir = combined[1]
            continuation_state, continuation_manifest, continuation_dir = combined[2]
            if (
                original_state is None
                or original_dir is None
                or recovery_state is None
                or recovery_dir is None
                or continuation_state is None
                or continuation_dir is None
                or original_manifest.get("schema_version")
                != CAMPAIGN_RUN_BATCHED_SCHEMA
                or recovery_manifest.get("schema_version")
                != CAMPAIGN_RUN_RECOVERY_SCHEMA
                or continuation_manifest.get("schema_version")
                != CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA
                or original_state.get("state") != "failed"
                or recovery_state.get("state") != "failed"
                or continuation_state.get("state") != "failed"
                or current_manifest.get("recovery_predecessor")
                != _recovery_predecessor_from_run(
                    original_state,
                    original_manifest,
                    original_dir,
                )
                or current_manifest.get("continuation_predecessor")
                != _recovery_continuation_predecessor_from_run(
                    recovery_state,
                    recovery_manifest,
                    recovery_dir,
                )
                or current_manifest.get("finalization_predecessor")
                != _recovery_finalization_predecessor_from_run(
                    continuation_state,
                    continuation_manifest,
                    continuation_dir,
                )
            ):
                raise SupervisorError(
                    "v5 没有逐字绑定获批的 v2/v3/v4 三前序。"
                )

    # 已封存失败只能由链中的下一项消费。完整失败 attempt 可由普通 v2
    # 零请求预览承接；KeyboardInterrupt 孤儿仍由 v3 承接。只有上述精确
    # 诊断的 v3 可交给 v4，只有固定摘要算法诊断的 v4 可交给 v5。
    # v5 失败或其他终态一律停线，不再开放第六种恢复清单。
    for index, (state, prior_manifest, _run_dir) in enumerate(ordered):
        terminal_state = state.get("state")
        if terminal_state == "stopped":
            continue
        if terminal_state != "failed":
            raise SupervisorError("前序 Campaign 批次没有可信终态。")
        successor_manifest = combined[index + 1][1]
        prior_schema = prior_manifest.get("schema_version")
        successor_schema = successor_manifest.get("schema_version")
        if (
            prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and successor_schema == CAMPAIGN_RUN_RECOVERY_SCHEMA
        ):
            continue
        if (
            prior_schema == CAMPAIGN_RUN_RECOVERY_SCHEMA
            and successor_schema == CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA
        ):
            continue
        if (
            prior_schema == CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA
            and successor_schema == CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA
        ):
            continue
        if (
            prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and successor_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and _validate_batched_official_recovery_preview_successor(
                state,
                prior_manifest,
                _run_dir,
                successor_manifest,
            )
        ):
            continue
        if (
            prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and successor_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and _validate_permission_preflight_compensation_successor(
                state,
                prior_manifest,
                _run_dir,
                successor_manifest,
            )
        ):
            continue
        if (
            prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and successor_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and _validate_permission_alias_closeout_successor(
                state,
                prior_manifest,
                _run_dir,
                successor_manifest,
            )
        ):
            continue
        if (
            predispatch_successor
            and index == len(ordered) - 1
            and prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
            and successor_schema == CAMPAIGN_RUN_BATCHED_SCHEMA
        ):
            continue
        if prior_schema == CAMPAIGN_RUN_BATCHED_SCHEMA:
            raise SupervisorError("失败批次只能由唯一直接 v3 恢复后继承接。")
        raise SupervisorError("失败批次没有被唯一允许的直接恢复后继承接。")

    seen_batch_ids = {
        str(prior_manifest.get("batch_id"))
        for _state, prior_manifest, _run_dir in ordered
    }
    seen_batch_digests = {
        str(prior_manifest.get("batch_sha256"))
        for _state, prior_manifest, _run_dir in ordered
    }
    if (
        manifest["batch_id"] in seen_batch_ids
        or manifest["batch_sha256"] in seen_batch_digests
    ):
        raise SupervisorError("Campaign 后继批次重复使用既有批次身份。")
    return ordered


def _campaign_dir_from_run_manifest(path: Path) -> Path | None:
    """从规范 ``control/vc/run-manifests`` 路径识别所属 Campaign。"""

    try:
        resolved = Path(path).resolve(strict=True)
    except OSError:
        return None
    if (
        resolved.parent.name != "run-manifests"
        or resolved.parent.parent.name != "vc"
        or resolved.parent.parent.parent.name != "control"
    ):
        return None
    campaign_dir = resolved.parents[3]
    campaign_path = campaign_dir / "campaign.json"
    if campaign_path.is_symlink() or not campaign_path.is_file():
        return None
    return campaign_dir


@contextmanager
def _timing_closeout_lock(root: Path) -> Iterator[None]:
    """与 VC-0 收口共用同一把账本锁，串行化失败终态追加。"""

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
        raise SupervisorError("Campaign 失败时间账本锁无法创建或不可信。") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
        ):
            raise SupervisorError("Campaign 失败时间账本锁身份不可信。")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise SupervisorError("UpgradeTimingLedger 正由其他收口流程使用。") from error
        yield
    finally:
        os.close(descriptor)


def _close_failed_campaign_timing_ledger(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    failed_action_id: str,
) -> dict[str, Any]:
    """把父动作失败确定性映射为 stage_abandoned 与 stop_the_line。"""

    campaign_dir = Path(campaign_dir)
    campaign = _read_json(campaign_dir / "campaign.json")
    controls = campaign.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping):
        raise SupervisorError("Campaign 缺少 UpgradeTimingLedger 控制绑定。")
    ledger_dir = Path(str(timing.get("ledger_dir", "")))
    if not ledger_dir.is_absolute() or ledger_dir.is_symlink():
        raise SupervisorError("Campaign UpgradeTimingLedger 路径不可信。")
    try:
        ledger_dir = ledger_dir.resolve(strict=True)
    except OSError as error:
        raise SupervisorError("Campaign UpgradeTimingLedger 不存在。") from error
    phase = str(manifest.get("phase", ""))
    campaign_id = str(manifest.get("campaign_id", ""))
    if (
        campaign.get("campaign_id") != campaign_id
        or phase not in vc_artifacts.VC_PHASES
        or campaign.get("campaign_mode") != "formal"
        or timing.get("ledger_plan_sha256")
        != _sha256((ledger_dir / "ledger.json").read_bytes())
    ):
        raise SupervisorError("Campaign、阶段或 UpgradeTimingLedger 计划绑定漂移。")
    if manifest.get("schema_version") in {
        CAMPAIGN_RUN_BATCHED_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
        CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
    }:
        vc_control = campaign.get("vc_control")
        plan_binding = (
            vc_control.get("campaign_plan")
            if isinstance(vc_control, Mapping)
            else None
        )
        if not isinstance(plan_binding, Mapping):
            raise SupervisorError("父批次与 Campaign 总计划摘要不一致。")
        plan_path = campaign_dir / str(plan_binding.get("path", ""))
        if (
            plan_path.is_symlink()
            or not plan_path.is_file()
            or _sha256(plan_path.read_bytes()) != plan_binding.get("sha256")
        ):
            raise SupervisorError("Campaign 总计划文件或摘要漂移。")
        # 两个口径不能互比：vc_control.campaign_plan.sha256 是计划文件的字节摘要，
        # 而批次 manifest 的 campaign_plan_sha256 记录的是计划内嵌的自摘要
        # plan_sha256（见 vc_artifacts.build_campaign_plan）。以前拿字节摘要
        # 去比自摘要，合法父批次必然被拒，失败账本永远关不掉。
        plan_payload = _read_json(plan_path)
        if plan_payload.get("plan_sha256") != manifest.get("campaign_plan_sha256"):
            raise SupervisorError("父批次与 Campaign 总计划摘要不一致。")

    sequence = manifest.get("batch_sequence", 1)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise SupervisorError("父失败批次序号非法。")
    failed_action_id = _safe_id(failed_action_id, "failed_action_id")
    failure_digest = _sha256(
        _canonical(
            {
                "campaign_id": campaign_id,
                "phase": phase,
                "batch_sequence": sequence,
                "failed_action_id": failed_action_id,
            }
        )
    )
    # 根因只由稳定输入生成：同一动作在不同 Campaign 失败得到同一个 ID，
    # 项目总账的同根因上限才能跨 Campaign 累计。failure_digest 仍含
    # campaign_id，只用于让事件 ID 在本账本内唯一。
    root_cause_id = root_cause.structured_root_cause(
        component="supervisor",
        stable_error_code="campaign-run.action-failed",
        failed_step=failed_action_id,
        stable_dimensions={"phase": phase},
    )
    event_prefix = f"campaign-run-failure-{failure_digest[:20]}"
    abandon_event_id = f"{event_prefix}-stage-abandoned"
    stop_event_id = f"{event_prefix}-stop-the-line"
    next_action = "完成工具闭合后建立全新 VC-0；禁止继续当前 Campaign。"

    with _timing_closeout_lock(ledger_dir):
        try:
            before = timing_ledger.inspect_ledger(ledger_dir)
        except (OSError, timing_ledger.TimingLedgerError) as error:
            raise SupervisorError(f"UpgradeTimingLedger 无法重放：{error}") from error
        if (
            before.get("upgrade_id") != timing.get("upgrade_id")
            or before.get("campaign_purpose") != campaign.get("campaign_purpose")
            or before.get("baseline_version") != campaign.get("baseline_version")
            or before.get("target_version") != campaign.get("target_version")
        ):
            raise SupervisorError("UpgradeTimingLedger 与 Campaign 版本或用途漂移。")
        if before.get("status") == "stopped":
            if (
                before.get("last_event_id") != stop_event_id
                or before.get("next_action") != next_action
            ):
                raise SupervisorError("UpgradeTimingLedger 已由其他根因停线。")
            return {
                "status": "passed",
                "idempotent": True,
                "ledger_dir": str(ledger_dir),
                "head_sequence": before["head_sequence"],
                "head_sha256": before["head_sha256"],
                "root_cause_id": root_cause_id,
                "next_action": next_action,
            }

        if before.get("last_event_id") == abandon_event_id:
            if before.get("active_phase") is not None:
                raise SupervisorError("stage_abandoned 部分终态仍残留 active 阶段。")
        elif before.get("active_phase") == phase:
            try:
                timing_ledger.append_event(
                    ledger_dir,
                    event_id=abandon_event_id,
                    phase=phase,
                    event_type="stage_abandoned",
                    root_cause_id=root_cause_id,
                    live_request_count=0,
                    next_action=next_action,
                )
            except (OSError, timing_ledger.TimingLedgerError) as error:
                raise SupervisorError(
                    f"UpgradeTimingLedger stage_abandoned 写入失败：{error}"
                ) from error
        else:
            raise SupervisorError("UpgradeTimingLedger 当前阶段与父失败阶段不一致。")

        try:
            middle = timing_ledger.inspect_ledger(ledger_dir)
            if (
                middle.get("last_event_id") != abandon_event_id
                or middle.get("active_phase") is not None
            ):
                raise SupervisorError("UpgradeTimingLedger stage_abandoned 未稳定落盘。")
            timing_ledger.append_event(
                ledger_dir,
                event_id=stop_event_id,
                phase=phase,
                event_type="stop_the_line",
                root_cause_id=root_cause_id,
                live_request_count=0,
                next_action=next_action,
            )
            final = timing_ledger.inspect_ledger(ledger_dir)
        except (OSError, timing_ledger.TimingLedgerError) as error:
            raise SupervisorError(
                f"UpgradeTimingLedger stop_the_line 写入失败：{error}"
            ) from error
        if (
            final.get("status") != "stopped"
            or final.get("active_phase") is not None
            or final.get("last_event_id") != stop_event_id
            or final.get("next_action") != next_action
        ):
            raise SupervisorError("UpgradeTimingLedger 父失败终态未闭合。")
        return {
            "status": "passed",
            "idempotent": False,
            "ledger_dir": str(ledger_dir),
            "head_sequence": final["head_sequence"],
            "head_sha256": final["head_sha256"],
            "root_cause_id": root_cause_id,
            "next_action": next_action,
        }


def _campaign_run_locked(
    args: argparse.Namespace,
    *,
    manifest: Mapping[str, Any],
    state_dir: Path,
    campaign_dir: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """在调用方已持有 ``.campaign-run.lock`` 时执行一个父动作队列。"""

    client: SupervisorClient | None = None
    results: list[dict[str, Any]] = []
    status = "failed"
    reason = "campaign-run-failed"
    timing_closeout: dict[str, Any] | None = None
    active_action_id: str | None = None
    try:
        history = _campaign_run_history(state_dir, str(manifest["campaign_id"]))
        if manifest["schema_version"] == CAMPAIGN_RUN_SCHEMA:
            # v1 只有相对预算，不能安全承接后续批次；保留历史行为且禁止复用 ID。
            if history:
                raise SupervisorError(
                    "同一 campaign-id 已有历史运行实例，拒绝重新起算 deadline。"
                )
            deadline = time.time() + float(manifest["deadline_seconds"])
        else:
            _validate_batched_campaign_history(manifest, history)
            deadline = datetime.fromisoformat(
                str(manifest["original_deadline_at_utc"]).replace("Z", "+00:00")
            ).timestamp()
            if deadline <= time.time():
                raise SupervisorError(
                    "Campaign 原始绝对 deadline 已经过期，禁止重新计时。"
                )
        client = SupervisorClient(
            state_dir,
            campaign_id=str(manifest["campaign_id"]),
            phase=str(manifest["phase"]),
            deadline_at_epoch=deadline,
            heartbeat_seconds=args.heartbeat_seconds,
            watchdog_timeout_seconds=args.watchdog_timeout_seconds,
            ledger_interval_seconds=args.ledger_interval_seconds,
        )
        client.start()
        if client.run_dir is None:
            raise SupervisorError("Campaign 父监督器运行目录尚未建立。")
        # 把归一化后的不可变清单和摘要写入本次 run，便于强停后确认实际
        # 执行的是哪一条队列；该写入发生在任何动作启动之前。
        _write_json(
            client.run_dir / "campaign-run-manifest.json",
            {
                "schema_version": manifest["schema_version"],
                "manifest_sha256": _sha256(_canonical(manifest)),
                "manifest": manifest,
            },
            replace=False,
        )
        if bool(manifest["no_op"]):
            operation = "campaign:incremental-noop"
            client.event_start(operation)
            client.event_end(operation, metadata={"execute_count": 0})
            status = "stopped"
            reason = "incremental-noop"
        else:
            if client.run_dir is None:
                raise SupervisorError("Campaign 父监督器运行目录尚未建立。")
            # 所有动作都继承同一父监督器身份。子命令只能 attach，不能再起
            # CampaignLease／SupervisorClient；身份字段由父 state.json 交叉校验。
            child_environment = os.environ.copy()
            child_environment.update(
                {
                    CAMPAIGN_RUN_CONTEXT_ENV: "1",
                    CAMPAIGN_RUN_DIR_ENV: str(client.run_dir),
                    CAMPAIGN_RUN_ID_ENV: str(manifest["campaign_id"]),
                    CAMPAIGN_RUN_PHASE_ENV: str(manifest["phase"]),
                    CAMPAIGN_RUN_OWNER_PID_ENV: str(client.owner_pid),
                    CAMPAIGN_RUN_OWNER_NONCE_ENV: str(client.owner_nonce),
                    CAMPAIGN_RUN_DEADLINE_ENV: str(client.deadline_at_epoch),
                }
            )
            diagnostic_dir = _validate_state_dir(
                client.run_dir / "action-diagnostics",
                create=True,
            )
            for action in manifest["actions"]:
                action_id = str(action["action_id"])
                active_action_id = action_id
                if (
                    manifest["schema_version"] == CAMPAIGN_RUN_BATCHED_SCHEMA
                    and manifest["phase"] == "VC-1"
                    and action_id == "prepare-official-assertion-bundle"
                ):
                    coordinates = _assertion_coordinates(action)
                    attempt_schema = _replay_vc1_evidence_permission_closeout(
                        *coordinates,
                        expected_campaign_id=str(manifest["campaign_id"]),
                    )
                    if attempt_schema != "codex-upgrade-capture-attempt/v3":
                        raise SupervisorError(
                            "历史 v2 Attempt 不得重新派发 assertion 动作。"
                        )
                diagnostic_path = diagnostic_dir / (
                    f"action-{action_id}-failure.json"
                )
                action_environment = dict(child_environment)
                action_environment.update(
                    {
                        CAMPAIGN_RUN_ACTION_ID_ENV: action_id,
                        CAMPAIGN_RUN_ACTION_DIAGNOSTIC_ENV: str(diagnostic_path),
                    }
                )
                try:
                    result = client.run_command(
                        list(action["command"]),
                        operation=str(action["operation"]),
                        timeout_seconds=float(action["timeout_seconds"]),
                        cleanup_grace_seconds=(
                            DEFAULT_BATCHED_ACTION_CLEANUP_GRACE_SECONDS
                            if manifest["schema_version"]
                            in {
                                CAMPAIGN_RUN_BATCHED_SCHEMA,
                                CAMPAIGN_RUN_RECOVERY_SCHEMA,
                                CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
                                CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
                            }
                            else 0
                        ),
                        job_id=action_id,
                        env=action_environment,
                        capture_output=False,
                    )
                except BaseException as error:
                    if not diagnostic_path.exists():
                        _write_action_diagnostic(
                            diagnostic_path,
                            campaign_id=str(manifest["campaign_id"]),
                            phase=str(manifest["phase"]),
                            action_id=action_id,
                            owner_pid=client.owner_pid,
                            owner_nonce=client.owner_nonce,
                            failure_kind="unexpected-error",
                            error_type=type(error).__name__,
                            message="子命令未正常返回。",
                        )
                    _validate_action_diagnostic(
                        diagnostic_path,
                        run_dir=client.run_dir,
                        campaign_id=str(manifest["campaign_id"]),
                        phase=str(manifest["phase"]),
                        action_id=action_id,
                        owner_pid=client.owner_pid,
                        owner_nonce=client.owner_nonce,
                    )
                    raise
                action_result = {
                    "action_id": action_id,
                    "returncode": int(result.returncode),
                    "status": "passed" if result.returncode == 0 else "failed",
                }
                if result.returncode != 0:
                    if not diagnostic_path.exists():
                        _write_action_diagnostic(
                            diagnostic_path,
                            campaign_id=str(manifest["campaign_id"]),
                            phase=str(manifest["phase"]),
                            action_id=action_id,
                            owner_pid=client.owner_pid,
                            owner_nonce=client.owner_nonce,
                            failure_kind="child-returncode",
                            error_type="ChildProcessError",
                            message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
                        )
                    diagnostic = _validate_action_diagnostic(
                        diagnostic_path,
                        run_dir=client.run_dir,
                        campaign_id=str(manifest["campaign_id"]),
                        phase=str(manifest["phase"]),
                        action_id=action_id,
                        owner_pid=client.owner_pid,
                        owner_nonce=client.owner_nonce,
                    )
                    action_result["diagnostic"] = {
                        "schema_version": ACTION_DIAGNOSTIC_SCHEMA,
                        "path": str(diagnostic_path.relative_to(client.run_dir)),
                        "sha256": diagnostic["diagnostic_sha256"],
                    }
                results.append(action_result)
                if result.returncode != 0:
                    reason = f"action-failed:{action['action_id']}"
                    break
                active_action_id = None
            else:
                status = "stopped"
                reason = "queue-complete"
        if status == "failed" and reason.startswith("action-failed:") and campaign_dir is not None:
            try:
                timing_closeout = _close_failed_campaign_timing_ledger(
                    campaign_dir,
                    manifest,
                    failed_action_id=reason.split(":", 1)[1],
                )
            except BaseException as error:
                timing_closeout = {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error)[:1000],
                }
        client.stop(reason=reason, status=status)
        payload = {
            "campaign_id": manifest["campaign_id"],
            "run_dir": str(client.run_dir),
            "status": status,
            "reason": reason,
            "actions": results,
            "execute_items": list(manifest.get("execute_items", [])),
            "reuse_items": list(manifest.get("reuse_items", [])),
        }
        if campaign_dir is not None:
            payload["timing_closeout"] = timing_closeout
        if manifest["schema_version"] in {
            CAMPAIGN_RUN_BATCHED_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
        }:
            payload.update(
                {
                    "batch_id": manifest["batch_id"],
                    "batch_sequence": manifest["batch_sequence"],
                    "batch_sha256": manifest["batch_sha256"],
                    "original_deadline_at_utc": manifest[
                        "original_deadline_at_utc"
                    ],
                }
            )
            if manifest["schema_version"] in {
                CAMPAIGN_RUN_RECOVERY_SCHEMA,
                CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
                CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
            }:
                payload["recovery_mode"] = manifest["recovery_mode"]
        return (0 if status == "stopped" else 1), payload
    except BaseException as error:
        reason = f"{type(error).__name__}"
        closeout_error: BaseException | None = None
        if campaign_dir is not None and active_action_id is not None:
            try:
                timing_closeout = _close_failed_campaign_timing_ledger(
                    campaign_dir,
                    manifest,
                    failed_action_id=active_action_id,
                )
            except BaseException as timing_error:
                closeout_error = timing_error
        if client is not None and client.started and not client._stop_completed:
            try:
                client.stop(reason=reason, status="failed")
            except BaseException:
                pass
        if closeout_error is not None:
            raise SupervisorError(
                "父 campaign-run 异常且 UpgradeTimingLedger 收口失败："
                f"{type(closeout_error).__name__}: {str(closeout_error)[:800]}"
            ) from error
        raise
    finally:
        # 无论动作返回失败还是父编排器抛出异常，都必须把本轮父监督器收口；
        # 监控进程只负责 watchdog，不能让 running 状态悬挂到下一次任务。
        if client is not None and client.started and not client._stop_completed:
            try:
                client.stop(reason=reason, status="failed")
            except BaseException:
                pass


def _campaign_run_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """取唯一锁并按预声明队列完成全部动作。"""

    manifest_path = Path(args.manifest)
    manifest = _campaign_run_manifest(manifest_path)
    campaign_dir = _campaign_dir_from_run_manifest(manifest_path)
    lock_descriptor, state_dir = _campaign_run_lock(Path(args.state_dir))
    try:
        return _campaign_run_locked(
            args,
            manifest=manifest,
            state_dir=state_dir,
            campaign_dir=campaign_dir,
        )
    finally:
        os.close(lock_descriptor)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    monitor = commands.add_parser("monitor", help="内部：运行独立监督器")
    monitor.add_argument("--state-dir", required=True, type=Path)
    monitor.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    monitor.add_argument("--watchdog-timeout-seconds", type=float, default=DEFAULT_WATCHDOG_TIMEOUT_SECONDS)
    monitor.add_argument("--ledger-interval-seconds", type=float, default=DEFAULT_LEDGER_INTERVAL_SECONDS)
    monitor.add_argument("--terminate-owner", action="store_true")
    status = commands.add_parser("status", help="只读显示监督器状态")
    status.add_argument("--state-dir", required=True, type=Path)
    audit = commands.add_parser("audit", help="只读汇总分钟账本")
    audit.add_argument("--state-dir", required=True, type=Path)
    campaign_owner = commands.add_parser(
        "campaign-owner",
        help=argparse.SUPPRESS,
    )
    campaign_owner.add_argument("--state-dir", required=True, type=Path)
    campaign_owner.add_argument("--campaign-id", required=True)
    campaign_owner.add_argument("--phase", required=True)
    campaign_owner.add_argument("--owner-nonce", required=True)
    campaign_owner.add_argument("--deadline-at-epoch", required=True, type=float)
    campaign_owner.add_argument("--predecessor-run-dir", type=Path)
    campaign_owner.add_argument("--initial-operation", required=True)
    campaign_owner.add_argument("--initial-timeout-seconds", required=True, type=float)
    campaign_owner.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    campaign_owner.add_argument("--watchdog-timeout-seconds", type=float, default=DEFAULT_WATCHDOG_TIMEOUT_SECONDS)
    campaign_owner.add_argument("--ledger-interval-seconds", type=float, default=DEFAULT_LEDGER_INTERVAL_SECONDS)
    campaign_start = commands.add_parser(
        "campaign-start",
        help="启动跨命令持续运行的 Campaign 父监督器",
    )
    campaign_start.add_argument("--state-dir", required=True, type=Path)
    campaign_start.add_argument("--campaign-id", required=True)
    campaign_start.add_argument("--phase", required=True)
    deadline_group = campaign_start.add_mutually_exclusive_group(required=True)
    deadline_group.add_argument("--deadline-seconds", type=float)
    deadline_group.add_argument(
        "--resume-from-run-dir",
        type=Path,
        help="从同一 Campaign 前序终态续接并继承原始全局 deadline。",
    )
    campaign_start.add_argument("--initial-operation", default="campaign-planning")
    campaign_start.add_argument("--initial-timeout-seconds", type=float, default=300)
    campaign_start.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    campaign_start.add_argument("--watchdog-timeout-seconds", type=float, default=DEFAULT_WATCHDOG_TIMEOUT_SECONDS)
    campaign_start.add_argument("--ledger-interval-seconds", type=float, default=DEFAULT_LEDGER_INTERVAL_SECONDS)
    campaign_mark = commands.add_parser(
        "campaign-mark",
        help="切换 Campaign 父监督器的分钟分类和当前动作",
    )
    campaign_mark.add_argument("--state-dir", required=True, type=Path)
    campaign_mark.add_argument(
        "--classification",
        required=True,
        choices=sorted(CAMPAIGN_CLASSIFICATIONS - {"active"}),
    )
    campaign_mark.add_argument("--operation", required=True)
    campaign_mark.add_argument("--timeout-seconds", required=True, type=float)
    campaign_mark.add_argument("--job-id")
    campaign_exec = commands.add_parser(
        "campaign-exec",
        help="在 Campaign 父监督器下绑定并执行一条真实命令",
    )
    campaign_exec.add_argument("--state-dir", required=True, type=Path)
    campaign_exec.add_argument("--operation", required=True)
    campaign_exec.add_argument("--timeout-seconds", required=True, type=float)
    campaign_exec.add_argument("--job-id")
    campaign_exec.add_argument(
        "--accept-returncode",
        action="append",
        type=int,
        help="显式允许一个非零诊断退出码；可重复，0 始终允许",
    )
    campaign_exec.add_argument(
        "--persist-output",
        action="store_true",
        help="仅用于不会输出秘密的离线命令；将 stdout/stderr 写入私有日志",
    )
    campaign_exec.add_argument("command_argv", nargs=argparse.REMAINDER)
    campaign_stop = commands.add_parser(
        "campaign-stop",
        help="仅在整轮升级完成或明确失败时结束 Campaign 父监督器",
    )
    campaign_stop.add_argument("--state-dir", required=True, type=Path)
    campaign_stop.add_argument("--reason", required=True)
    campaign_stop.add_argument(
        "--status",
        choices=("stopped", "failed"),
        default="stopped",
    )
    campaign_run = commands.add_parser(
        "campaign-run",
        help="新流程唯一入口：在一个父监督器下自动执行预声明动作队列",
    )
    campaign_run.add_argument("--state-dir", required=True, type=Path)
    campaign_run.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help=(
            "0600 的 campaign-run 动作队列清单；历史兼容使用 v1，"
            "VC-0～VC-6 正式分批流程使用 v2"
        ),
    )
    campaign_run.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=DEFAULT_HEARTBEAT_SECONDS,
    )
    campaign_run.add_argument(
        "--watchdog-timeout-seconds",
        type=float,
        default=DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    )
    campaign_run.add_argument(
        "--ledger-interval-seconds",
        type=float,
        default=DEFAULT_LEDGER_INTERVAL_SECONDS,
    )
    run = commands.add_parser("run", help="通过统一监督入口执行一个命令")
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--campaign-id", required=True)
    run.add_argument("--phase", required=True)
    run.add_argument("--deadline-seconds", required=True, type=float)
    run.add_argument("--operation", required=True)
    run.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    run.add_argument("--watchdog-timeout-seconds", type=float, default=DEFAULT_WATCHDOG_TIMEOUT_SECONDS)
    run.add_argument("--ledger-interval-seconds", type=float, default=DEFAULT_LEDGER_INTERVAL_SECONDS)
    run.add_argument(
        "--persist-output",
        action="store_true",
        help="仅用于不会输出秘密的离线门禁；将 stdout/stderr 写入私有日志",
    )
    run.add_argument("command_argv", nargs=argparse.REMAINDER)
    return parser


def _command_phase(arguments: argparse.Namespace) -> str | None:
    """读取命令绑定的阶段；读取失败时返回 None，不替代后续状态校验。"""

    phase = getattr(arguments, "phase", None)
    if isinstance(phase, str) and phase:
        return phase
    state_dir = getattr(arguments, "state_dir", None)
    if not isinstance(state_dir, Path):
        return None
    state_path = state_dir / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = _read_state(state_dir)
    except (OSError, SupervisorError, json.JSONDecodeError):
        return None
    value = state.get("phase")
    return value if isinstance(value, str) else None


def _reject_unparented_vc6_supervisor_write(
    arguments: argparse.Namespace,
) -> None:
    """VC-6 正式流程禁止从旧监督器入口启动或改写状态。

    旧入口继续保留给历史离线夹具，但不能只依赖 campaign-run 注入的环境变量
    才拒绝。之前两次 VC-6 正是从无父上下文的 campaign-start／campaign-mark
    路径启动，导致动作队列没有被一次性声明。
    """

    command = getattr(arguments, "command", None)
    if command not in LEGACY_SUPERVISOR_WRITE_COMMANDS:
        return
    # 父 campaign-run 的子命令由下面的统一上下文检查处理；这里不重复报错。
    if os.environ.get(CAMPAIGN_RUN_CONTEXT_ENV) == "1":
        return
    phase = _command_phase(arguments)
    if isinstance(phase, str) and phase.casefold() == FORMAL_VC6_PHASE.casefold():
        raise SupervisorError(
            f"VC-6 正式流程拒绝旧监督器入口：{command}；"
            "唯一入口是带完整不可变 manifest 的 campaign-run。"
        )


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args(argv)
    try:
        _reject_unparented_vc6_supervisor_write(arguments)
        if (
            os.environ.get(CAMPAIGN_RUN_CONTEXT_ENV) == "1"
            and arguments.command
            in {
                "campaign-start",
                "campaign-mark",
                "campaign-exec",
                "campaign-stop",
                "campaign-run",
                "run",
                "campaign-owner",
                "monitor",
            }
        ):
            raise SupervisorError(
                f"正式 campaign-run 禁止旧监督器写入入口：{arguments.command}；"
                "动作必须由 campaign-run 父队列统一执行。"
            )
        if arguments.command == "monitor":
            return _monitor(arguments)
        if arguments.command == "status":
            print(json.dumps(_status_command(arguments.state_dir), ensure_ascii=False, sort_keys=True))
            return 0
        if arguments.command == "audit":
            report = _audit_command(arguments.state_dir)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            # 审计缺口是硬门禁；调用方不能只看 JSON 中的摘要继续部署。
            return 2 if report.get("audit_incomplete") else 0
        if arguments.command == "campaign-owner":
            return _campaign_owner(arguments)
        if arguments.command == "campaign-start":
            print(
                json.dumps(
                    _campaign_start_command(arguments),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "campaign-mark":
            print(
                json.dumps(
                    _campaign_mark_command(
                        arguments.state_dir,
                        classification=arguments.classification,
                        operation=arguments.operation,
                        timeout_seconds=arguments.timeout_seconds,
                        job_id=arguments.job_id,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "campaign-exec":
            returncode, payload = _campaign_exec_command(arguments)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return returncode
        if arguments.command == "campaign-stop":
            print(
                json.dumps(
                    _campaign_stop_command(
                        arguments.state_dir,
                        reason=arguments.reason,
                        status=arguments.status,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "campaign-run":
            returncode, payload = _campaign_run_command(arguments)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return returncode
        command_argv = list(arguments.command_argv)
        if command_argv[:1] == ["--"]:
            command_argv = command_argv[1:]
        if not command_argv:
            raise SupervisorError("run 缺少待执行命令。")
        state_dir = _validate_state_dir(arguments.state_dir, create=True)
        deadline = time.time() + float(arguments.deadline_seconds)
        with SupervisorClient(
            state_dir,
            campaign_id=arguments.campaign_id,
            phase=arguments.phase,
            deadline_at_epoch=deadline,
            heartbeat_seconds=arguments.heartbeat_seconds,
            watchdog_timeout_seconds=arguments.watchdog_timeout_seconds,
            ledger_interval_seconds=arguments.ledger_interval_seconds,
        ) as client:
            output_path: Path | None = None
            if arguments.persist_output:
                output_path = client.run_dir / "command-output.log"
                descriptor = os.open(
                    output_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        result = client.run_command(
                            command_argv,
                            operation=arguments.operation,
                            timeout_seconds=float(arguments.deadline_seconds),
                            capture_output=False,
                            output_stream=stream,
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    # fdopen 接管 descriptor；异常时 with 会关闭，不能重复 close。
                    raise
            else:
                result = client.run_command(
                    command_argv,
                    operation=arguments.operation,
                    timeout_seconds=float(arguments.deadline_seconds),
                )
            print(
                json.dumps(
                    {
                        "returncode": result.returncode,
                        "run_dir": str(client.run_dir),
                        "status": "passed" if result.returncode == 0 else "failed",
                        "output_log": (
                            str(output_path) if output_path is not None else None
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return int(result.returncode)
    except (OSError, SupervisorError, subprocess.SubprocessError) as error:
        print(f"Codex 升级监督器失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
