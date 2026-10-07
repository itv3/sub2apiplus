#!/usr/bin/env python3
"""编排 Claude Code 升级的 VC-0～VC-6 流程（Claude 升级控制面设计 B1-09：编排器骨架）。

与 ``codex_upgrade.py`` 平行的 Claude 编排器：共享控制流程只在 ``upgrade_control_plane`` 有一份，两个编排器各自
实现其下的读取、路径与校验函数（服务面 ``upgrade_client_adapter.ORCHESTRATOR_SURFACE``，Claude 以同名同签名提供
全部成员）。本模块不导入 ``codex_upgrade``；Claude 身份策略里它被忽略，v3 身份引擎在 Claude 闭包到达它时失败关闭。

服务面成员分四档：
* 迁入薄壳——共享流程的入口与迁入函数之间的互调，在 Claude 适配器作用域里转调 ``upgrade_control_plane``；
* 照搬——两客户端语义相同的通用函数（读写、摘要、路径、租约读取），逐字取 Codex 的实现作为 Claude 自有的一份
  （设计附录 E 第 6 条：以后与 Codex 合并）；
* 自实现——Claude 语义与 Codex 不同、此刻就能定的成员；
* 待实现——依赖后续工作项的成员，调用即报 ``ClaudeOrchestratorPending``，写明由哪个工作项落实，不伪造行为。

命令行与 Codex 同名：共享流程的子命令照搬 Codex 的参数定义并转调共享实现；Claude 专属的动作体由后续工作项落实。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# 作为主程序运行时让包名也指向这一份，延迟导入本模块的共享代码拿到的是同一个模块对象（与 Codex 编排器同一做法）。
if __name__ == "__main__":
    sys.modules.setdefault("tools.official_client_capture.claude_upgrade", sys.modules[__name__])

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256, secure_write_json
from tools.official_client_capture import claude_tool_identity_policy
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import claude_upgrade_predecessor
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import codex_upgrade_wire_transition
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane


# ---------------------------------------------------------------------------
# 服务面常量（共用布局与 Codex 同值；带 codex 前缀的 schema 换成 claude；Codex 历史专属数据置空）
# ---------------------------------------------------------------------------

APPROVAL_REVISION_FILENAME = "approval-revision.json"
ATTEMPT_RECOVERY_DIRNAME = "recovery"
ATTEMPT_RECOVERY_RESERVATION_FILENAME = "recovery-reservation.json"
ATTEMPT_RECOVERY_SUCCESS_STATUS = "awaiting_receipts"
ATTEMPT_RECOVERY_SUMMARY_FILENAME = "attempt-recovery.json"
CAMPAIGN_LEASE_FILENAME = ".campaign-lease.json"
CANDIDATE_INVALIDATION_FILENAME = "invalidation.json"
CANDIDATE_VC_PHASES = codex_upgrade_vc_artifacts.CANDIDATE_PHASES
CANONICAL_DIRECTORY = "canonical"
CANONICAL_IMPORT_RECEIPT_FILENAME = "import-receipt.json"
# Claude 探针的连续性比较种类（B2-06）：采集容器、配置（hosts／DNS／CA）、客户端状态；取证进程与监听端口只在 Job 前后比较。
CONTINUITY_PROBE_KINDS = ("containers", "configuration", "client")
ENVIRONMENT_ISOLATION_DIR = Path("control") / "environment"
ENVIRONMENT_ISOLATION_SCHEMA = "environment-isolation/v1"
EVALUATION_BASELINE_ABANDON_FILENAME = "ABANDON"
EVALUATION_BASELINE_AUTHORIZATION_FILENAME = "AUTHORIZATION"
EVALUATION_BASELINE_COMMIT_FILENAME = "COMMIT"
EVALUATION_BASELINE_PREPARED_FILENAME = "PREPARED"
EVALUATION_BASELINE_RECOVERY_FILENAME = "recovery.json"
EVALUATION_CHECKPOINTS_DIRNAME = "checkpoints"
EVALUATION_RUN_FILENAME = "evaluation-run.json"
EVIDENCE_CONFLICT_QUARANTINE_DIR = Path("control") / "evidence-conflict"
EVIDENCE_CONFLICT_QUARANTINE_SCHEMA = "evidence-conflict-quarantine/v1"
EVIDENCE_ROOT_SUPERSESSION_DIR = Path("control") / "evidence-roots"
EVIDENCE_ROOT_SUPERSESSION_SCHEMA = "evidence-root-supersession/v1"
FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS = (
    PurePosixPath("/capture/runs"),
    PurePosixPath("/root/oauth-capture/runs"),
)
FAILED_JOB_EVIDENCE_HOST_RUN_ROOT = Path(
    "/root/docker/capture-cli/data/runs"
)
FORMAL_FAILED_JOB_RECOVERY_COMMAND = "recover-candidate-failed-jobs"
FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND = "recover-candidate-post-run-seal"
JOB_RETRY_DELAY_SECONDS = 30
JOB_RETRY_LIMIT = 2
RECOVERY_CONTINUITY_DRIFT_STATUSES = ("drifted", "probe_failed")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
STAGING_PREPARED_FILENAME = "PREPARED"
TOOL_EVOLUTION_FIX_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VC_SEQUENCE_FILE_RE = re.compile(r"^([0-9]{4})-(vc-[0-6])\.json$")
ATTEMPT_RECOVERY_RESERVATION_SCHEMA = "claude-upgrade-attempt-recovery-reservation/v1"
ATTEMPT_RECOVERY_SUMMARY_SCHEMA = "claude-upgrade-attempt-recovery/v1"
RECOVERY_CONTINUITY_DRIFT_RECEIPT_SCHEMA = "claude-upgrade-recovery-continuity-drift/v1"
OFFICIAL_COMPACTION_LITE_RECOVERY_FILES = {}
OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS = frozenset()
RUNTIME_SUCCESSOR_CHANGED_TOOL_PATH_JOB_IDS = {}

# Claude Campaign 总计划（设计 B1-09）：schema、平台与必备顶层字段
CAMPAIGN_SCHEMA = "official-client-upgrade-campaign/v1"
CAMPAIGN_PLATFORM = "linux/arm64"
CAMPAIGN_REQUIRED_FIELDS = frozenset({
    "schema_version", "client", "campaign_id", "created_at_utc", "campaign_mode", "campaign_purpose",
    "baseline_version", "target_version", "target_sha256", "official_identity", "baseline_identity", "tool_identity",
    "control_receipts", "inputs", "configuration", "required_rules", "coverage_plan", "jobs", "vc_control", "predecessor",
})

# 照搬函数依赖的内部常量（与 Codex 同值）
SKIP_DIRECTORIES = {
    ".git",
    "target",
    "node_modules",
    "vendor",
    "fixtures",
    "snapshots",
}
REVISIONS_DIRNAME = "revisions"
RECOVERY_CONTINUITY_DRIFT_RECEIPT_NAME = "continuity-drift.json"
CONTINUITY_DRIFT_ERROR_TYPE = "EnvironmentContinuityDrift"
DEFAULT_ATTEMPT_WALL_SECONDS = 90 * 60
MAX_ATTEMPT_WALL_SECONDS = 6 * 60 * 60
DEFAULT_HEARTBEAT_SECONDS = codex_upgrade_supervisor.DEFAULT_HEARTBEAT_SECONDS
MAX_HEARTBEAT_SECONDS = 5 * 60
CAMPAIGN_LEASE_STALE_MULTIPLIER = 3
CAPTURE_CLEANUP_RESERVE_SECONDS = (
    codex_upgrade_supervisor.DEFAULT_BATCHED_ACTION_CLEANUP_GRACE_SECONDS
)
CAMPAIGN_LEASE_SCHEMA = "codex-upgrade-campaign-lease/v1"
CAMPAIGN_LEASE_STATES = frozenset({"active", "released", "stop_the_line"})
WATCHDOG_OPERATION_RE = re.compile(r"^[A-Za-z0-9._:/*?+={}\[\](), -]{1,256}$")
WATCHDOG_SENSITIVE_RE = re.compile(
    r"(authorization|api[-_]?key|cookie|token|secret|password|credential|bearer)",
    re.IGNORECASE,
)
CAMPAIGN_LEASE_LOCK_FILENAME = ".campaign-lease.lock"
CAMPAIGN_LEASE_STOP_DIRECTORY = "stop-the-line"
CAMPAIGN_LEASE_STOP_SCHEMA = "codex-upgrade-campaign-lease-stop/v1"
DEFAULT_WATCHDOG_TIMEOUT_SECONDS = (
    codex_upgrade_supervisor.DEFAULT_WATCHDOG_TIMEOUT_SECONDS
)
_ACTIVE_CAMPAIGN_LEASE: "CampaignLease | None" = None
CAMPAIGN_MODES = frozenset({"preflight_only", "formal"})
CANDIDATE_PURPOSES = frozenset({"validation_only", "production_replacement"})


# ---------------------------------------------------------------------------
# 待实现成员的统一报错
# ---------------------------------------------------------------------------


class ClaudeOrchestratorPending(ConfigurationError):
    """Claude 编排器尚未实现的服务面成员或子命令：报错写明由哪个工作项落实。"""


def _pending(name: str, work_item: str) -> ClaudeOrchestratorPending:
    return ClaudeOrchestratorPending(f"Claude 编排器尚未实现 {name}：由 {work_item} 落实（Claude 升级控制面设计）")


# ---------------------------------------------------------------------------
# 服务面类成员：异常类与 Job（形状与 Codex 相同；Claude 取证作业的字段语义由 B2-06 落实）
# ---------------------------------------------------------------------------


class TimingLedgerGateError(ConfigurationError):
    """UpgradeTimingLedger 拒绝为本批次推进阶段；作为预派发停线的 error_type 留痕。"""


class StagingStopTheLine(ConfigurationError):
    """孤儿对账或判定命中永久停线条件；入口以停线错误退出，该序号不得再派。"""


class StagingDeadlinePaused(ConfigurationError):
    """孤儿对账判定预算到期暂停（对账已登记暂停事实）：不是停线，批准延期后按同序号重新派发。"""


class ToolEvolutionRequired(ConfigurationError):
    """当前受管工具与 Campaign 有效身份不一致、尚未登记工具演进；报错之前不写任何文件。"""


@dataclass(frozen=True)
class Job:
    """一次可独立恢复的抓包任务。"""

    job_id: str
    phase: str
    suites: tuple[str, ...]
    description: str
    steps: tuple[dict[str, Any], ...]
    evidence_roots: tuple[str, ...]
    covers: tuple[str, ...]
    scenario_ids: tuple[str, ...] = ()
    required: bool = True
    # SCN-REALITY-01：本 job 必须为哪些场景产出真实性收据。不复用 scenario_ids——
    # 后者表达「该 job 覆盖哪些场景」，official-core 一个 job 就覆盖 9 个场景，
    # 不可能逐个产收据；真实性门禁只约束已证实失效的目标场景。
    required_scenario_receipts: tuple[str, ...] = ()
    track: str = "main"
    model_id: str = ""
    expected_use_responses_lite: bool = False
    required_model_receipt: bool = False
    # R16：逐作业显式工具依赖（相对工具根的路径，排序去重），由目标版本场景清单声明、
    # 随清单冻结。非空时依赖只按清单取摘要，不再递归扫描文件引用（旧算法把注释里的
    # 文件名也算依赖，0.156.1 的 relay 作业因此从 26 个文件涨到 176 个）；为空时沿用
    # 旧算法，历史 Campaign 的回放与复用判定口径不变。
    tool_dependencies: tuple[str, ...] = ()



# ---------------------------------------------------------------------------
# 照搬：通用函数逐字取 Codex 实现（Claude 自有一份；附录 E 第 6 条以后合并）
# ---------------------------------------------------------------------------


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_rfc3339_timestamp(value: Any) -> bool:
    """判断时间是否为带时区的 RFC 3339／ISO 8601 字符串。"""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _rfc3339_datetime(value: Any, label: str) -> datetime:
    """解析带时区时间，并统一为可比较的 datetime。"""

    if not _is_rfc3339_timestamp(value):
        raise ConfigurationError(f"{label} 不是带时区 RFC 3339 时间。")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _utc_now() -> str:
    """返回带微秒精度的 UTC RFC 3339 时间。"""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _attempt_continuity_drifted(attempt: Mapping[str, Any]) -> bool:
    """attempt 是否因环境连续性漂移失败（承接前失败、零请求）。"""

    error = attempt.get("execution_error")
    return (
        attempt.get("status") == "failed"
        and isinstance(error, Mapping)
        and error.get("type") == CONTINUITY_DRIFT_ERROR_TYPE
    )


def _recovery_continuity_receipt_path(campaign_dir: Path, attempt_id: str) -> Path:
    """零请求预览阶段发现环境连续性漂移的收据位置（第三批 B3-12，第 22 项）：对账目录，不写 attempt 证据。"""

    return campaign_dir / "control" / "reconciliation" / f"attempt-{attempt_id}" / RECOVERY_CONTINUITY_DRIFT_RECEIPT_NAME


def _normalize_watchdog_operation(value: Any) -> str:
    """只允许可审计的短操作标签，拒绝秘密、环境值和原始响应。"""

    if not isinstance(value, str):
        raise ConfigurationError("watchdog operation 必须是字符串。")
    operation = " ".join(value.strip().split())
    if (
        not operation
        or len(operation) > 256
        or not WATCHDOG_OPERATION_RE.fullmatch(operation)
        or WATCHDOG_SENSITIVE_RE.search(operation)
    ):
        raise ConfigurationError(
            "watchdog operation 含非法字符或敏感字段，拒绝写入 heartbeat。"
        )
    return operation


def _normalize_campaign_lease_note(value: Any, *, label: str) -> str:
    """规范化停线原因／下一动作；允许中文，但拒绝秘密和控制字符。"""

    if not isinstance(value, str):
        raise ConfigurationError(f"Campaign lease {label} 必须是字符串。")
    note = " ".join(value.strip().split())
    if (
        not note
        or len(note) > 512
        or any(ord(character) < 0x20 for character in note)
        or WATCHDOG_SENSITIVE_RE.search(note)
    ):
        raise ConfigurationError(f"Campaign lease {label} 非法。")
    return note


def _campaign_lease_pid_alive(pid: Any) -> bool:
    """以无副作用的 kill(pid, 0) 判断 owner 是否仍存在。"""

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


def _validate_campaign_lease_payload(
    payload: Mapping[str, Any],
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """校验持久租约的小型 JSON；绝不读取证据目录。"""

    required = {
        "schema_version",
        "campaign_id",
        "phase",
        "candidate_id",
        "owner_pid",
        "owner_nonce",
        "attempt_id",
        "started_at_utc",
        "deadline_at_utc",
        "last_heartbeat_at_utc",
        "heartbeat_seconds",
        "operation",
        "state",
        "recovered_owner_nonce",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ConfigurationError("Campaign lease 字段不闭合。")
    if (
        payload.get("schema_version") != CAMPAIGN_LEASE_SCHEMA
        or not SAFE_ID_RE.fullmatch(str(payload.get("campaign_id", "")))
        or (campaign_id is not None and payload.get("campaign_id") != campaign_id)
        or payload.get("phase") not in {"official", "candidate"}
        or not isinstance(payload.get("owner_pid"), int)
        or isinstance(payload.get("owner_pid"), bool)
        or int(payload.get("owner_pid", 0)) <= 0
        or not SHA256_RE.fullmatch(str(payload.get("owner_nonce", "")))
        or payload.get("state") not in CAMPAIGN_LEASE_STATES
        or not _is_rfc3339_timestamp(payload.get("started_at_utc"))
        or not _is_rfc3339_timestamp(payload.get("deadline_at_utc"))
        or not _is_rfc3339_timestamp(payload.get("last_heartbeat_at_utc"))
        or not isinstance(payload.get("heartbeat_seconds"), (int, float))
        or isinstance(payload.get("heartbeat_seconds"), bool)
        or not math.isfinite(float(payload.get("heartbeat_seconds")))
        or float(payload.get("heartbeat_seconds")) <= 0
        or float(payload.get("heartbeat_seconds")) > MAX_HEARTBEAT_SECONDS
        or not isinstance(payload.get("operation"), str)
        or _normalize_watchdog_operation(payload.get("operation"))
        != payload.get("operation")
    ):
        raise ConfigurationError("Campaign lease 身份或数值非法。")
    candidate_id = payload.get("candidate_id")
    attempt_id = payload.get("attempt_id")
    recovered = payload.get("recovered_owner_nonce")
    if payload.get("phase") == "official":
        if candidate_id is not None:
            raise ConfigurationError("official Campaign lease 不得携带 candidate-id。")
    elif not isinstance(candidate_id, str) or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("candidate Campaign lease 缺少合法 candidate-id。")
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not SAFE_ID_RE.fullmatch(attempt_id)
    ):
        raise ConfigurationError("Campaign lease attempt-id 非法。")
    if recovered is not None and not SHA256_RE.fullmatch(str(recovered)):
        raise ConfigurationError("Campaign lease recovered owner nonce 非法。")
    started = _rfc3339_datetime(payload["started_at_utc"], "lease.started_at_utc")
    deadline = _rfc3339_datetime(payload["deadline_at_utc"], "lease.deadline_at_utc")
    heartbeat = _rfc3339_datetime(
        payload["last_heartbeat_at_utc"], "lease.last_heartbeat_at_utc"
    )
    if deadline <= started or heartbeat < started:
        raise ConfigurationError("Campaign lease 时间窗口非法。")
    return dict(payload)


def _read_campaign_lease(campaign_dir: Path) -> dict[str, Any] | None:
    """只读读取当前租约；不存在时返回 ``None``。"""

    _validate_existing_campaign_path(campaign_dir)
    path = campaign_dir / CAMPAIGN_LEASE_FILENAME
    _reject_symlink_components(path, campaign_dir, "Campaign lease")
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("Campaign lease 必须是普通文件。")
    metadata = path.stat()
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 1024 * 1024
    ):
        raise ConfigurationError("Campaign lease 权限或大小非法。")
    payload = _read_json(path, "Campaign lease")
    return _validate_campaign_lease_payload(payload)


def _campaign_lease_staleness(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str, bool, bool]:
    """返回 ``(stale, reason, owner_alive, deadline_expired)``。"""

    if payload.get("state") != "active":
        return False, "", _campaign_lease_pid_alive(payload.get("owner_pid")), False
    observed = now or datetime.now(timezone.utc)
    deadline = _rfc3339_datetime(payload["deadline_at_utc"], "lease.deadline_at_utc")
    heartbeat = _rfc3339_datetime(
        payload["last_heartbeat_at_utc"], "lease.last_heartbeat_at_utc"
    )
    deadline_expired = observed >= deadline
    age = max(0.0, (observed - heartbeat).total_seconds())
    stale_after = max(
        float(payload["heartbeat_seconds"]) * CAMPAIGN_LEASE_STALE_MULTIPLIER,
        float(DEFAULT_HEARTBEAT_SECONDS),
    )
    owner_alive = _campaign_lease_pid_alive(payload.get("owner_pid"))
    if deadline_expired:
        return True, "deadline_expired", owner_alive, True
    if not owner_alive:
        return True, "owner_not_alive", False, False
    if age > stale_after:
        return True, "heartbeat_expired", True, False
    return False, "", True, False


def _campaign_lease_stop_path(campaign_dir: Path, owner_nonce: str) -> Path:
    if not SHA256_RE.fullmatch(owner_nonce):
        raise ConfigurationError("Campaign lease owner nonce 非法。")
    root = ensure_private_directory(
        campaign_dir / CAMPAIGN_LEASE_STOP_DIRECTORY,
        campaign_dir,
    )
    return root / f"lease-{owner_nonce}.json"


def _write_campaign_lease_stop_once(
    campaign_dir: Path,
    payload: Mapping[str, Any],
    *,
    reason: str,
    next_action: str,
) -> Path:
    """发布不可覆盖的租约停线收据；重复发现只重放原文件。"""

    lease = _validate_campaign_lease_payload(payload)
    reason = _normalize_campaign_lease_note(reason, label="reason")
    next_action = _normalize_campaign_lease_note(next_action, label="next_action")
    path = _campaign_lease_stop_path(campaign_dir, str(lease["owner_nonce"]))
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError("Campaign lease stop 收据路径不可信。")
        existing = _read_json(path, "Campaign lease stop 收据")
        _validate_campaign_lease_stop_receipt(existing, path)
        if (
            existing.get("campaign_id") != lease.get("campaign_id")
            or existing.get("phase") != lease.get("phase")
            or existing.get("candidate_id") != lease.get("candidate_id")
            or existing.get("owner_nonce") != lease.get("owner_nonce")
        ):
            raise ConfigurationError("Campaign lease stop 收据身份与租约不一致。")
        return path
    receipt: dict[str, Any] = {
        "schema_version": CAMPAIGN_LEASE_STOP_SCHEMA,
        "campaign_id": lease["campaign_id"],
        "phase": lease["phase"],
        "candidate_id": lease["candidate_id"],
        "owner_pid": lease["owner_pid"],
        "owner_nonce": lease["owner_nonce"],
        "attempt_id": lease["attempt_id"],
        "started_at_utc": lease["started_at_utc"],
        "deadline_at_utc": lease["deadline_at_utc"],
        "last_heartbeat_at_utc": lease["last_heartbeat_at_utc"],
        "operation": lease["operation"],
        "reason": reason,
        "detected_at_utc": _utc_now(),
        "next_action": next_action,
    }
    receipt["receipt_sha256"] = _fingerprint(receipt)
    _secure_write_json_once(path, receipt)
    return path


def _validate_campaign_lease_stop_receipt(
    payload: Mapping[str, Any],
    path: Path,
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """校验单份停线收据，并确保其身份与所属 Campaign 一致。"""

    required = {
        "schema_version",
        "campaign_id",
        "phase",
        "candidate_id",
        "owner_pid",
        "owner_nonce",
        "attempt_id",
        "started_at_utc",
        "deadline_at_utc",
        "last_heartbeat_at_utc",
        "operation",
        "reason",
        "detected_at_utc",
        "next_action",
        "receipt_sha256",
    }
    if set(payload) != required:
        raise ConfigurationError("Campaign lease stop 收据字段不闭合。")
    unsigned = dict(payload)
    digest = unsigned.pop("receipt_sha256")
    if (
        payload.get("schema_version") != CAMPAIGN_LEASE_STOP_SCHEMA
        or not SAFE_ID_RE.fullmatch(str(payload.get("campaign_id", "")))
        or (campaign_id is not None and payload.get("campaign_id") != campaign_id)
        or payload.get("phase") not in {"official", "candidate"}
        or not isinstance(payload.get("owner_pid"), int)
        or isinstance(payload.get("owner_pid"), bool)
        or payload.get("owner_pid", 0) <= 0
        or not SHA256_RE.fullmatch(str(payload.get("owner_nonce", "")))
        or path.name != f"lease-{payload.get('owner_nonce')}.json"
        or not _is_rfc3339_timestamp(payload.get("started_at_utc"))
        or not _is_rfc3339_timestamp(payload.get("deadline_at_utc"))
        or not _is_rfc3339_timestamp(payload.get("last_heartbeat_at_utc"))
        or not _is_rfc3339_timestamp(payload.get("detected_at_utc"))
        or not isinstance(payload.get("operation"), str)
        or _normalize_watchdog_operation(payload.get("operation"))
        != payload.get("operation")
        or not isinstance(payload.get("reason"), str)
        or not isinstance(payload.get("next_action"), str)
        or digest != _fingerprint(unsigned)
    ):
        raise ConfigurationError("Campaign lease stop 收据身份或摘要非法。")
    if payload.get("phase") == "official":
        if payload.get("candidate_id") is not None:
            raise ConfigurationError("official stop 收据不得携带 candidate-id。")
    elif not isinstance(payload.get("candidate_id"), str) or not SAFE_ID_RE.fullmatch(
        str(payload.get("candidate_id"))
    ):
        raise ConfigurationError("candidate stop 收据缺少合法 candidate-id。")
    attempt_id = payload.get("attempt_id")
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not SAFE_ID_RE.fullmatch(attempt_id)
    ):
        raise ConfigurationError("Campaign lease stop 收据 attempt-id 非法。")
    _normalize_campaign_lease_note(payload["reason"], label="reason")
    _normalize_campaign_lease_note(payload["next_action"], label="next_action")
    return dict(payload)


class CampaignLease:
    """Campaign 级持久租约。

    OS 文件锁只负责并发互斥；JSON 租约负责跨进程留下 owner、deadline 和
    最后 heartbeat。进程被强杀时文件不会消失，下一次命令会把 stale owner
    固化为不可覆盖停线收据，而不会静默重新跑全量任务。

    进程由 ``campaign-run`` 派发时，租约仅附加到父监督器的 run 目录，
    不再创建本地 lease 文件或第二个 monitor。
    """

    def __init__(
        self,
        campaign_dir: Path,
        *,
        phase: str,
        candidate_id: str | None,
        deadline: incremental_recovery.WallClockDeadline,
        attempt_id: str | None = None,
        command: str,
        allow_stale_recovery: bool = False,
        campaign_id: str | None = None,
    ) -> None:
        if phase not in {"official", "candidate"}:
            raise ConfigurationError("Campaign lease phase 非法。")
        if phase == "official" and candidate_id is not None:
            raise ConfigurationError("official Campaign lease 不得携带 candidate-id。")
        if phase == "candidate" and (
            not isinstance(candidate_id, str) or not SAFE_ID_RE.fullmatch(candidate_id)
        ):
            raise ConfigurationError("candidate Campaign lease 缺少合法 candidate-id。")
        if attempt_id is not None and not SAFE_ID_RE.fullmatch(str(attempt_id)):
            raise ConfigurationError("Campaign lease attempt-id 非法。")
        if not isinstance(deadline, incremental_recovery.WallClockDeadline):
            raise ConfigurationError("Campaign lease 必须绑定 WallClockDeadline。")
        self.campaign_dir = campaign_dir
        if campaign_id is not None and not SAFE_ID_RE.fullmatch(str(campaign_id)):
            raise ConfigurationError("Campaign lease campaign-id 非法。")
        self.campaign_id = str(campaign_id) if campaign_id is not None else None
        self.phase = phase
        self.candidate_id = candidate_id
        self.deadline = deadline
        self.attempt_id = attempt_id
        self.command = _normalize_watchdog_operation(command)
        self.allow_stale_recovery = bool(allow_stale_recovery)
        self.owner_pid = os.getpid()
        self.owner_nonce = secrets.token_hex(32)
        self._descriptor: int | None = None
        self._path = campaign_dir / CAMPAIGN_LEASE_FILENAME
        self._payload: dict[str, Any] | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutex = threading.RLock()
        self._last_heartbeat_monotonic = 0.0
        self._failure: BaseException | None = None
        self._stop_recorded = False
        self._attached_parent = False
        self._previous_global: CampaignLease | None = None
        # 进程内线程只作低延迟备用；真正的强停检测和分钟账本由这个独立进程负责。
        self._supervisor: codex_upgrade_supervisor.SupervisorClient | None = None

    @property
    def acquired(self) -> bool:
        return (
            self._attached_parent
            or (self._descriptor is not None and self._payload is not None)
        )

    @property
    def payload(self) -> dict[str, Any]:
        if self._payload is None:
            raise ConfigurationError("Campaign lease 尚未取得。")
        return dict(self._payload)

    def _open_lock(self) -> int:
        _validate_existing_campaign_path(self.campaign_dir)
        lock_path = self.campaign_dir / CAMPAIGN_LEASE_LOCK_FILENAME
        _reject_symlink_components(lock_path, self.campaign_dir, "Campaign lease 锁")
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            descriptor = os.open(
                lock_path,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            os.close(descriptor)
            raise ConfigurationError("Campaign lease 锁必须是当前用户拥有的 0600 文件。")
        os.fchmod(descriptor, 0o600)
        return descriptor

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        if self._path.is_symlink() or (self._path.exists() and not self._path.is_file()):
            raise ConfigurationError("Campaign lease 文件路径不可信。")
        secure_write_json(self._path, dict(payload))
        metadata = self._path.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ConfigurationError("Campaign lease 写入后权限或属主非法。")

    def acquire(self) -> "CampaignLease":
        global _ACTIVE_CAMPAIGN_LEASE
        if self.acquired:
            raise ConfigurationError("Campaign lease 不得重复取得。")
        # campaign-run 是正式流程的唯一父监督器。子命令收到父上下文时，
        # 只附加到已有 supervisor，不再创建 lease 文件、锁或第二个 monitor。
        try:
            attached = codex_upgrade_supervisor.SupervisorClient.attach_from_environment()
        except codex_upgrade_supervisor.SupervisorError as error:
            raise ConfigurationError(f"无法附加 campaign-run 父监督器：{error}") from error
        if attached is not None:
            try:
                self.deadline.check("campaign-lease:attach")
                campaign_manifest = self.campaign_dir / "campaign.json"
                if campaign_manifest.is_file():
                    manifest = _read_json(campaign_manifest, "Campaign 清单")
                    if manifest.get("campaign_id") != attached.campaign_id:
                        raise ConfigurationError(
                            "campaign-run 父监督器与 Campaign 清单身份不一致。"
                        )
                    if manifest.get("campaign_mode") != "formal":
                        raise ConfigurationError(
                            "campaign-run 只能承载 formal Campaign。"
                        )
                remaining = attached.deadline_at_epoch - time.time()
                if remaining <= 0:
                    raise ConfigurationError("campaign-run 父监督器全局 deadline 已到期。")
                self.deadline = incremental_recovery.WallClockDeadline(
                    remaining,
                    label=f"{self.phase}-campaign-run",
                )
                # 绑定父 run 的单调时钟边界，而不是以子进程启动时刻重新
                # 起算；WallClockDeadline 只保留同一绝对 deadline。
                self.deadline.started_monotonic = (
                    float(attached._started_monotonic_ns) / 1_000_000_000
                )
                self.deadline.deadline_monotonic = (
                    float(attached._deadline_monotonic_ns) / 1_000_000_000
                )
                self.deadline.heartbeat_seconds = float(attached.heartbeat_seconds)
                self._supervisor = attached
                self._attached_parent = True
                self.owner_pid = attached.owner_pid
                self.owner_nonce = attached.owner_nonce
                self._payload = {
                    "schema_version": CAMPAIGN_LEASE_SCHEMA,
                    "campaign_id": attached.campaign_id,
                    "phase": self.phase,
                    "candidate_id": self.candidate_id,
                    "owner_pid": attached.owner_pid,
                    "owner_nonce": attached.owner_nonce,
                    "attempt_id": self.attempt_id,
                    "state": "active",
                    "operation": self.command,
                    "deadline_at_utc": codex_upgrade_supervisor._epoch_to_utc(
                        attached.deadline_at_epoch
                    ),
                    "attached_to_run_dir": str(attached.run_dir),
                }
                self._last_heartbeat_monotonic = time.monotonic()
                self._previous_global = _ACTIVE_CAMPAIGN_LEASE
                _ACTIVE_CAMPAIGN_LEASE = self
                attached.heartbeat(f"attach:{self.command}", force=True)
                return self
            except BaseException:
                if attached is not None:
                    try:
                        attached._started = False
                    except BaseException:
                        pass
                raise
        self.deadline.check("campaign-lease:acquire")
        descriptor = self._open_lock()
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                current = _read_campaign_lease(self.campaign_dir)
                owner = (
                    f"owner_pid={current.get('owner_pid')} nonce={current.get('owner_nonce')}"
                    if current
                    else "owner=unknown"
                )
                raise ConfigurationError(f"Campaign lease 已被占用（{owner}）。")
            current = _read_campaign_lease(self.campaign_dir)
            recovered_nonce: str | None = None
            if current is not None and current.get("state") == "active":
                stale, reason, _alive, _expired = _campaign_lease_staleness(current)
                if not stale:
                    raise ConfigurationError(
                        "Campaign lease 仍在有效期内，禁止并发或重复运行："
                        f"owner_pid={current.get('owner_pid')} nonce={current.get('owner_nonce')}"
                    )
                _write_campaign_lease_stop_once(
                    self.campaign_dir,
                    current,
                    reason=reason,
                    next_action=(
                        "仅允许绑定同一失败源 attempt 的 resume --rerun-failed；"
                        "先校验 recovery_scope"
                    ),
                )
                recovered_nonce = str(current.get("owner_nonce"))
                if not self.allow_stale_recovery:
                    raise ConfigurationError(
                        "Campaign lease 已过期并已停线；普通 run 禁止接管，"
                        "请使用同一失败源的显式 resume --rerun-failed。"
                    )
            elif current is not None and current.get("state") not in {
                "released",
                "stop_the_line",
            }:
                raise ConfigurationError("Campaign lease 状态非法。")
            started = datetime.now(timezone.utc)
            remaining = self.deadline.remaining_seconds
            if remaining <= 0:
                raise incremental_recovery.WallClockTimeoutError(
                    "campaign-lease:acquire",
                    elapsed_seconds=self.deadline.elapsed_seconds,
                    budget_seconds=self.deadline.budget_seconds,
                )
            payload: dict[str, Any] = {
                "schema_version": CAMPAIGN_LEASE_SCHEMA,
                "campaign_id": self.campaign_id or str(
                    load_campaign_manifest(
                        self.campaign_dir,
                        _control_epoch_bootstrap=(self.command == "control-epoch"),
                    ).get("campaign_id", "")
                ),
                "phase": self.phase,
                "candidate_id": self.candidate_id,
                "owner_pid": self.owner_pid,
                "owner_nonce": self.owner_nonce,
                "attempt_id": self.attempt_id,
                "started_at_utc": started.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "deadline_at_utc": (started + timedelta(seconds=remaining)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "last_heartbeat_at_utc": started.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "heartbeat_seconds": float(getattr(self.deadline, "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)),
                "operation": self.command,
                "state": "active",
                "recovered_owner_nonce": recovered_nonce,
            }
            _validate_campaign_lease_payload(payload)
            self._descriptor = descriptor
            self._payload = payload
            self._write_payload(payload)
            try:
                self._supervisor = codex_upgrade_supervisor.SupervisorClient(
                    self.campaign_dir / ".supervisor",
                    campaign_id=str(payload["campaign_id"]),
                    phase=self.phase,
                    deadline_at_epoch=time.time() + remaining,
                    owner_pid=self.owner_pid,
                    heartbeat_seconds=float(
                        getattr(
                            self.deadline,
                            "heartbeat_seconds",
                            DEFAULT_HEARTBEAT_SECONDS,
                        )
                    ),
                    watchdog_timeout_seconds=DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
                    ledger_interval_seconds=60,
                    # Campaign lease 运行在 CLI 宿主进程内；监督器只负责封存
                    # watchdog 终态，不得为了一个阶段 deadline 误杀共享宿主
                    # （例如测试 runner 或上层服务）。外部调用者若使用独立
                    # owner 进程，可在 SupervisorClient 层显式选择终止。
                    terminate_owner=False,
                )
                self._supervisor.start()
            except (OSError, codex_upgrade_supervisor.SupervisorError) as error:
                # 独立监督器是正式可变命令的硬门禁；启动失败不能退化成只有
                # 进程内线程的不可审计模式。
                self._supervisor = None
                self._payload["state"] = "stop_the_line"
                self._payload["operation"] = "supervisor-start-failed"
                try:
                    self._write_payload(self._payload)
                finally:
                    raise ConfigurationError(
                        f"独立升级监督器启动失败：{error}"
                    ) from error
            self._last_heartbeat_monotonic = time.monotonic()
            self._previous_global = _ACTIVE_CAMPAIGN_LEASE
            _ACTIVE_CAMPAIGN_LEASE = self
            self._thread = threading.Thread(
                target=self._watchdog_loop,
                name=f"codex-campaign-lease-{self.owner_nonce[:8]}",
                daemon=True,
            )
            self._thread.start()
            return self
        except BaseException:
            if self._supervisor is not None:
                try:
                    self._supervisor.stop(
                        reason="supervisor-acquire-failed",
                        status="failed",
                    )
                except BaseException:
                    pass
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

    def __enter__(self) -> "CampaignLease":
        return self.acquire()

    def _mark_stop_line(self, reason: str, *, next_action: str | None = None) -> None:
        with self._mutex:
            if self._payload is None:
                return
            if self._stop_recorded:
                return
            if self._attached_parent:
                self._stop_recorded = True
                if self._supervisor is not None:
                    try:
                        self._supervisor.event_fail(
                            "campaign:child-stop",
                            reason=reason,
                        )
                    except BaseException as error:
                        self._failure = self._failure or error
                return
            action = next_action or (
                "人工审计租约停线收据；仅可绑定同一失败源的显式 resume --rerun-failed"
            )
            try:
                _write_campaign_lease_stop_once(
                    self.campaign_dir,
                    self._payload,
                    reason=reason,
                    next_action=action,
                )
                self._stop_recorded = True
                if self._supervisor is not None:
                    try:
                        self._supervisor.event_fail(
                            "campaign:stop-the-line",
                            reason=reason,
                        )
                    except BaseException:
                        # 主租约停线收据是权威事实；监督器附加事件失败时仍须
                        # 保留主收据，后续审计会标记附加账本缺口。
                        pass
            finally:
                self._payload["state"] = "stop_the_line"
                self._payload["operation"] = _normalize_watchdog_operation(reason)
                try:
                    self._write_payload(self._payload)
                except BaseException as error:
                    self._failure = self._failure or error

    def check(self, operation: str | None = None) -> float:
        """检查 lease 与 attempt deadline；到期先写停线再抛出。"""

        if self._failure is not None:
            raise self._failure
        if not self.acquired:
            raise ConfigurationError("Campaign lease 尚未取得。")
        if self._attached_parent and self._supervisor is not None:
            try:
                self._supervisor._ensure_monitor_alive()
            except codex_upgrade_supervisor.SupervisorError as error:
                self._failure = ConfigurationError(str(error))
                raise self._failure from error
        try:
            remaining = self.deadline.check(operation or self.command)
        except incremental_recovery.WallClockTimeoutError as error:
            self._mark_stop_line("deadline_expired")
            self._failure = error
            raise
        return remaining

    def heartbeat(
        self,
        operation: str,
        *,
        force: bool = False,
        allow_expired: bool = False,
    ) -> None:
        operation = _normalize_watchdog_operation(operation)
        if not self.acquired:
            return
        if not allow_expired:
            self.check(operation)
        if self._attached_parent:
            if self._supervisor is not None:
                self._supervisor.heartbeat(operation, force=force)
            self._last_heartbeat_monotonic = time.monotonic()
            return
        now_monotonic = time.monotonic()
        interval = float(getattr(self.deadline, "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS))
        if not force and now_monotonic - self._last_heartbeat_monotonic < interval:
            return
        with self._mutex:
            if self._payload is None:
                return
            if not allow_expired and self.deadline.expired:
                self._mark_stop_line("deadline_expired")
                raise incremental_recovery.WallClockTimeoutError(
                    operation,
                    elapsed_seconds=self.deadline.elapsed_seconds,
                    budget_seconds=self.deadline.budget_seconds,
                )
            self._payload["operation"] = operation
            self._payload["last_heartbeat_at_utc"] = _utc_now()
            self._write_payload(self._payload)
            self._last_heartbeat_monotonic = now_monotonic
            if self._supervisor is not None:
                self._supervisor.heartbeat(operation, force=force)

    def event_start(self, operation: str, *, job_id: str | None = None) -> None:
        """记录一个统一命令／Job 动作的开始。"""

        if self._supervisor is not None:
            self._supervisor.event_start(operation, job_id=job_id)

    def event_end(
        self,
        operation: str,
        *,
        job_id: str | None = None,
        status: str = "passed",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """记录一个动作的正常结束。"""

        if self._supervisor is not None:
            self._supervisor.event_end(
                operation,
                job_id=job_id,
                status=status,
                metadata=metadata,
            )

    def event_fail(
        self,
        operation: str,
        *,
        job_id: str | None = None,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """记录一个动作失败；失败原因只允许脱敏短文本。"""

        if self._supervisor is not None:
            self._supervisor.event_fail(
                operation,
                job_id=job_id,
                reason=reason,
                metadata=metadata,
            )

    def run_command(
        self,
        argv: Iterable[str],
        *,
        operation: str,
        timeout_seconds: float,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        output_stream: Any | None = None,
        heartbeat_callback: Any | None = None,
        job_id: str | None = None,
        merge_stderr: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        """通过独立监督器统一执行一个外部命令。"""

        if self._supervisor is None:
            raise ConfigurationError("Campaign lease 的独立监督器不可用。")
        return self._supervisor.run_command(
            list(argv),
            operation=operation,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            env=env,
            capture_output=output_stream is None,
            output_stream=output_stream,
            job_id=job_id,
            heartbeat_callback=heartbeat_callback,
            merge_stderr=merge_stderr,
        )

    def bind_attempt(self, attempt_id: str) -> None:
        """在原子 reservation 发布后把 attempt-id 绑定到当前租约。"""

        if not isinstance(attempt_id, str) or not SAFE_ID_RE.fullmatch(attempt_id):
            raise ConfigurationError("Campaign lease attempt-id 非法。")
        self.check("campaign-lease:bind-attempt")
        if self._attached_parent:
            self.attempt_id = attempt_id
            self._payload["attempt_id"] = attempt_id  # type: ignore[index]
            self.heartbeat("attempt:reserved", force=True)
            return
        with self._mutex:
            if self._payload is None:
                raise ConfigurationError("Campaign lease 尚未取得。")
            self._payload["attempt_id"] = attempt_id
            self._payload["operation"] = "attempt:reserved"
            self._payload["last_heartbeat_at_utc"] = _utc_now()
            self._write_payload(self._payload)
            self._last_heartbeat_monotonic = time.monotonic()
            if self._supervisor is not None:
                self._supervisor.heartbeat("attempt:reserved", force=True)

    def _watchdog_loop(self) -> None:
        interval = max(
            0.05,
            float(getattr(self.deadline, "heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS)),
        )
        while not self._stop_event.wait(min(interval, max(0.05, self.deadline.remaining_seconds))):
            try:
                if self.deadline.expired:
                    error = incremental_recovery.WallClockTimeoutError(
                        "campaign-lease:deadline",
                        elapsed_seconds=self.deadline.elapsed_seconds,
                        budget_seconds=self.deadline.budget_seconds,
                    )
                    self._failure = error
                    self._mark_stop_line("deadline_expired")
                    return
                self.heartbeat(self.command, force=True)
            except BaseException as error:
                self._failure = error
                try:
                    self._mark_stop_line("heartbeat_write_failed")
                except BaseException:
                    pass
                return

    def release(self, *, outcome: str = "released") -> None:
        """正常结束时释放 lease；停线状态本身不可被 released 覆盖。"""

        global _ACTIVE_CAMPAIGN_LEASE
        self._stop_event.set()
        if self._attached_parent:
            with self._mutex:
                if self._supervisor is not None:
                    self._supervisor.stop(reason="child-release", status="stopped")
                if _ACTIVE_CAMPAIGN_LEASE is self:
                    _ACTIVE_CAMPAIGN_LEASE = self._previous_global
                self._supervisor = None
                self._payload = None
                self._attached_parent = False
                self._descriptor = None
            return
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        with self._mutex:
            # 独立监督器可能在 owner 线程尚未醒来时先判定 heartbeat/deadline
            # 终态。先读取它的只读状态并同步 Campaign 停线事实，再决定是否
            # 将租约标成 released，避免 watchdog-aborted 被“正常释放”掩盖。
            if self._supervisor is not None and self._payload is not None:
                if (
                    self._supervisor.process is not None
                    and self._supervisor.process.poll() is not None
                ):
                    try:
                        self._supervisor._mark_monitor_exit()
                    except BaseException as error:
                        self._failure = self._failure or error
                try:
                    supervisor_state = self._supervisor.status().get("state")
                except BaseException as error:
                    supervisor_state = "audit-incomplete"
                    self._failure = self._failure or error
                if supervisor_state in {"watchdog-aborted", "audit-incomplete"}:
                    try:
                        self._mark_stop_line(
                            f"supervisor_{str(supervisor_state).replace('-', '_')}"
                        )
                    except BaseException as error:
                        self._failure = self._failure or error
            if self._payload is not None and self._payload.get("state") == "active":
                try:
                    self._payload["state"] = "released"
                    self._payload["operation"] = _normalize_watchdog_operation(
                        f"lease:{outcome}"
                    )
                    self._payload["last_heartbeat_at_utc"] = _utc_now()
                    self._write_payload(self._payload)
                except BaseException as error:
                    self._failure = self._failure or error
            if self._supervisor is not None:
                try:
                    supervisor_status = (
                        "stopped"
                        if self._payload is None
                        or self._payload.get("state") != "stop_the_line"
                        else "failed"
                    )
                    self._supervisor.stop(
                        reason=(
                            "normal-completion"
                            if supervisor_status == "stopped"
                            else "campaign-stop-the-line"
                        ),
                        status=supervisor_status,
                    )
                except BaseException as error:
                    self._failure = self._failure or error
                finally:
                    self._supervisor = None
            if _ACTIVE_CAMPAIGN_LEASE is self:
                _ACTIVE_CAMPAIGN_LEASE = self._previous_global
            descriptor = self._descriptor
            self._descriptor = None
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        # 任意异常都表示本次可变命令没有形成可验证的正常终态。不能把
        # ConfigurationError／I/O 错误当作“正常释放”，否则下一条命令会
        # 看见一个没有停线原因的旧租约并可能误接管。
        if exc is not None:
            try:
                reason = (
                    "owner_interrupted"
                    if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "owner_exception"
                )
                self._mark_stop_line(reason)
            except BaseException:
                pass
        self.release(outcome="exception" if exc is not None else "released")
        return False


def _lease_identity_matches(
    lease: CampaignLease,
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
) -> bool:
    """判断当前进程中的活动租约是否正好覆盖目标命令。"""

    try:
        same_root = lease.campaign_dir.resolve(strict=True) == campaign_dir.resolve(
            strict=True
        )
    except OSError:
        same_root = False
    return (
        same_root
        and lease.phase == phase
        and lease.candidate_id == candidate_id
    )


@contextmanager
def _campaign_operation_lease(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    command: str,
    deadline: incremental_recovery.WallClockDeadline,
    allow_stale_recovery: bool = False,
    campaign_id: str | None = None,
) -> Iterable[CampaignLease | None]:
    """为一个可变 CLI 操作取得 lease；嵌套调用复用同一 owner。"""

    active = _ACTIVE_CAMPAIGN_LEASE
    if active is not None:
        if not _lease_identity_matches(
            active,
            campaign_dir,
            phase=phase,
            candidate_id=candidate_id,
        ):
            raise ConfigurationError(
                "当前进程已经持有另一 Campaign／phase 的 lease，禁止嵌套可变操作。"
            )
        active.check(command)
        yield active
        active.check(f"{command}:complete")
        return
    with CampaignLease(
        campaign_dir,
        phase=phase,
        candidate_id=candidate_id,
        deadline=deadline,
        command=command,
        allow_stale_recovery=allow_stale_recovery,
        campaign_id=campaign_id,
    ) as lease:
        yield lease
        lease.check(f"{command}:complete")


def _campaign_cleanup_reserve_seconds() -> float:
    """读取父监督器冻结的清理窗口；普通直接调用没有额外保留量。"""

    raw = os.environ.get(
        codex_upgrade_supervisor.CAMPAIGN_RUN_CLEANUP_GRACE_ENV
    )
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigurationError("campaign-run 清理窗口不是数字。") from error
    if (
        not math.isfinite(value)
        or value < CAPTURE_CLEANUP_RESERVE_SECONDS
        or value > MAX_ATTEMPT_WALL_SECONDS
    ):
        raise ConfigurationError("campaign-run 清理窗口不满足受管下限。")
    return value


def _failed_job_evidence_host_route(value: Any) -> tuple[Path, PurePosixPath]:
    """把登记的容器 runs 路径转换为宿主物理路径。"""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ConfigurationError("失败任务证据根不是规范 POSIX 绝对路径。")
    logical = PurePosixPath(value)
    if (
        not logical.is_absolute()
        or str(logical) != value
        or any(part in {"", ".", ".."} for part in logical.parts[1:])
    ):
        raise ConfigurationError("失败任务证据根不是规范 POSIX 绝对路径。")
    for alias in FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS:
        try:
            relative = logical.relative_to(alias)
        except ValueError:
            continue
        if not relative.parts:
            raise ConfigurationError("失败任务证据根不能等于登记 runs 根。")
        return (
            FAILED_JOB_EVIDENCE_HOST_RUN_ROOT.joinpath(*relative.parts),
            relative,
        )
    raise ConfigurationError("失败任务证据根未落在登记的容器 runs 别名内。")


def _failed_job_ids(results: Any) -> list[str]:
    """返回已封存结果中的失败 Job；结构异常交由正式校验器处理。"""

    if not isinstance(results, list):
        return []
    return sorted(
        {
            str(result.get("id"))
            for result in results
            if isinstance(result, Mapping)
            and isinstance(result.get("id"), str)
            and result.get("status") == "failed"
        }
    )


def _validate_existing_campaign_path(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("--campaign-dir 必须是非符号链接的绝对路径。")
    if not path.is_dir():
        raise ConfigurationError(f"Campaign 目录不存在：{path}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取{label} {path}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象：{path}")
    return payload


def _reject_symlink_components(path: Path, root: Path, label: str) -> None:
    """拒绝从 Campaign 根到目标文件之间的任一符号链接。"""

    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ConfigurationError(f"{label}越过 Campaign 根目录。") from error
    current = root
    if current.is_symlink():
        raise ConfigurationError(f"{label} Campaign 根目录是符号链接。")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigurationError(f"{label}路径包含符号链接：{current}")


def _secure_write_json_once(path: Path, payload: dict[str, Any]) -> None:
    """以硬链接发布临时文件，实现跨进程原子且不可覆盖的 JSON 封存。"""

    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ConfigurationError(f"不可变文件已经存在，禁止覆盖：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_copy_file_once(source: Path, destination: Path) -> dict[str, Any]:
    """逐字节复制普通文件，并以不可覆盖方式发布目标。"""

    if source.is_symlink() or not source.is_file():
        raise ConfigurationError(f"后继 Campaign 复制源不是可信普通文件：{source}")
    ensure_private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with source.open("rb") as source_stream, os.fdopen(
            descriptor, "wb"
        ) as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
        descriptor = -1
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise ConfigurationError(
                f"不可变文件已经存在，禁止覆盖：{destination}"
            ) from error
        destination.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    source_digest = file_sha256(source)
    if file_sha256(destination) != source_digest:
        raise ConfigurationError(f"后继 Campaign 文件复制后摘要不一致：{destination}")
    return {
        "sha256": source_digest,
        "bytes": destination.stat().st_size,
    }


@contextmanager
def _campaign_lock(
    campaign_dir: Path,
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
) -> Iterable[None]:
    """以 Campaign 级文件锁串行化 attempt 预约与阶段发布。"""

    _validate_existing_campaign_path(campaign_dir)
    lock_path = campaign_dir / ".campaign.lock"
    _reject_symlink_components(lock_path, campaign_dir, "Campaign 锁")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(lock_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            raise ConfigurationError("Campaign 锁必须是当前用户拥有的 0600 普通文件。")
        if created:
            os.fchmod(descriptor, 0o600)
        if deadline is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            # 非阻塞轮询让 Campaign 锁等待也受 attempt 全局预算约束；
            # 不能因另一个进程持锁而无限挂起。
            while True:
                deadline.check("campaign-lock")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    deadline.sleep(
                        min(0.1, deadline.remaining_seconds),
                        operation="campaign-lock-wait",
                    )
                except InterruptedError:
                    continue
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _campaign_file(campaign_dir: Path, relative: str) -> Path:
    path = campaign_dir / relative
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(campaign_dir.resolve()):
        raise ConfigurationError(f"Campaign 文件越过根目录：{relative}")
    return path


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[a-f0-9]{40,64}", value) else None


def _candidate_revisions_root(campaign_dir: Path) -> Path:
    return campaign_dir / "control" / "vc" / REVISIONS_DIRNAME


def _candidate_revision_dir(campaign_dir: Path, revision: int) -> Path:
    return _candidate_revisions_root(campaign_dir) / f"r{int(revision)}"


def _ledger_last_stage_revision(ledger_dir: Path) -> dict[str, Any] | None:
    """账本最后一条 stage_revision 事件（显式激活的 revision）；没有则 None。"""

    last: dict[str, Any] | None = None
    try:
        for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir):
            if event.get("event_type") == "stage_revision":
                last = dict(event)
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise ConfigurationError(f"UpgradeTimingLedger 无法读取 stage_revision：{error}") from error
    return last


def _stage_replay_input_file(path: Path, label: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} 输入文件不可信")
    return file_sha256(path)


def _stage_replay_input_directory(path: Path, label: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ConfigurationError(f"{label} 输入目录不可信")
    return _directory_tree_digest(path)


def _staging_run_arguments(arguments: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        heartbeat_seconds=(
            arguments.heartbeat_seconds
            if arguments.heartbeat_seconds is not None
            else codex_upgrade_supervisor.DEFAULT_HEARTBEAT_SECONDS
        ),
        watchdog_timeout_seconds=arguments.watchdog_timeout_seconds,
        ledger_interval_seconds=arguments.ledger_interval_seconds,
    )


def _read_stop_reason(run_dir: Path) -> str | None:
    path = run_dir / "stop-receipt.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        reason = codex_upgrade_supervisor.read_stop_receipt(run_dir).get("reason")
    except codex_upgrade_supervisor.SupervisorError as error:
        raise ConfigurationError(f"父 run stop-receipt 无法校验：{error}") from error
    return reason if isinstance(reason, str) else None


def _staging_attempt_sha256s(attempt_dir: Path) -> tuple[str | None, str | None]:
    """从 PREPARED（或半写的产物）取 batch／manifest 摘要；缺失即 None。"""

    marker_path = attempt_dir / STAGING_PREPARED_FILENAME
    if marker_path.is_file() and not marker_path.is_symlink():
        try:
            marker = codex_upgrade_vc_artifacts.validate_staging_prepared_marker(
                _read_json(marker_path, "staging PREPARED 标记")
            )
            return str(marker["batch_sha256"]), str(marker["manifest_sha256"])
        except (ConfigurationError, codex_upgrade_vc_artifacts.VCArtifactError):
            return None, None
    return None, None


def _staging_error_summary(error: BaseException) -> str:
    """暂停／停线消息里的"类型：原文"（修好接着跑第 29 项）：原文经同一清洗并截到 600 字符。"""

    message = codex_upgrade_vc_artifacts.staging_abort_error_message(error)
    return f"{type(error).__name__}：{message[:600]}"


def _existing_candidate_revisions(campaign_dir: Path) -> list[int]:
    root = _candidate_revisions_root(campaign_dir)
    if root.is_symlink():
        raise ConfigurationError("候选 revisions 目录不得是符号链接。")
    if not root.is_dir():
        return []
    found: list[int] = []
    for child in root.iterdir():
        match = re.fullmatch(r"^r([1-9][0-9]*)$", child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise ConfigurationError(f"候选 revisions 目录含非法条目：{child.name}")
        found.append(int(match.group(1)))
    return sorted(found)


def _campaign_coordinates(manifest: Mapping[str, Any]) -> tuple[str, str]:
    """读取并严格校验 Campaign 创建时冻结的模式与用途。"""

    mode = manifest.get("campaign_mode")
    purpose = manifest.get("campaign_purpose")
    if mode not in CAMPAIGN_MODES:
        raise ConfigurationError("Campaign 缺少或携带非法 campaign_mode。")
    if purpose not in CANDIDATE_PURPOSES:
        raise ConfigurationError("Campaign 缺少或携带非法 campaign_purpose。")
    return str(mode), str(purpose)


def _require_formal_campaign(
    campaign_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """阻止 P0 计划通过任何直接或 continuation 入口进入正式阶段。"""

    current = manifest or load_campaign_manifest(campaign_dir)
    mode, _ = _campaign_coordinates(current)
    if mode != "formal":
        raise ConfigurationError(
            "preflight_only Campaign 只允许 plan/status；不得进入正式或 live 阶段，"
            "请以 formal 模式创建全新 Campaign。"
        )
    return current


def _evaluation_baseline_dir(campaign_dir: Path, candidate_id: str, baseline: int) -> Path:
    """b<K>（K≥1）的目录；b0 隐含为原路径，没有目录。"""

    if not isinstance(baseline, int) or isinstance(baseline, bool) or baseline < 1:
        raise ConfigurationError("评估基线编号必须是正整数。")
    if not candidate_id or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("评估基线必须提供合法 candidate-id。")
    return campaign_dir / "candidates" / candidate_id / "revisions" / f"b{baseline}"


def _require_file_binding(value: Any, label: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"path", "sha256"}
        or not isinstance(value.get("path"), str)
        or not value["path"]
        or not SHA256_RE.fullmatch(str(value.get("sha256")))
    ):
        raise ConfigurationError(f"{label}文件绑定非法。")


def _directory_identity(path: Path, *, ignore_device: bool = False) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError(f"取代证据根路径不是可信目录：{path}")
    # 第 66 项：隔离封存预演（OverlayFS 副本）里目录 st_dev 是 overlay 自己的设备号，调用方判定后只比 inode。
    return (0 if ignore_device else metadata.st_dev), metadata.st_ino


def _is_policy_v2_identity(identity: Mapping[str, Any]) -> bool:
    """plan 时记录了策略 v2 五摘要的 Campaign 才走四层判定；旧 Campaign 按 v1。"""

    return isinstance(identity.get("policy_sha256"), str) and isinstance(
        identity.get("wire_producer_sha256"), str
    )


def _directory_tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ConfigurationError(f"候选源码目录不存在或不可信：{root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return _fingerprint({"entries": entries})


def _capture_attempt_relative(phase: str, candidate_id: str | None) -> Path:
    """返回抓包 attempt 的阶段相对目录。"""

    if phase == "official":
        return Path("official")
    if not candidate_id or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("候选 attempt 必须绑定合法 candidate-id。")
    return Path("candidates") / candidate_id


def _attempt_recovery_segment_root(attempt_root: Path, recovery_revision: str) -> Path:
    return attempt_root / ATTEMPT_RECOVERY_DIRNAME / recovery_revision


def _normalized_json_sha256(payload: dict[str, Any]) -> str:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_build_receipt_path(campaign_dir: Path, candidate_id: str) -> Path:
    if not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("Candidate 构建收据 candidate-id 非法。")
    return campaign_dir / "candidates" / candidate_id / "build-receipt.json"


def _write_or_verify_json_ignoring(
    path: Path,
    payload: Mapping[str, Any],
    *,
    volatile: Iterable[str],
) -> None:
    """write-once + 内容核对：已存在时只豁免易变字段（时间戳、自摘要），其余逐字段一致。"""

    skip = set(volatile)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"派生封存文件路径不可信：{path}")
        existing = _read_json(path, "派生封存文件")
        left = {key: value for key, value in existing.items() if key not in skip}
        right = {key: value for key, value in payload.items() if key not in skip}
        if left != right:
            raise ConfigurationError(f"派生封存文件已经存在且内容不一致：{path}")
        return
    _secure_write_json_once(path, dict(payload))


def _assertions_root_for_baseline(campaign_dir: Path, candidate_id: str, baseline: int) -> Path:
    root = campaign_dir / "assertions" / candidate_id
    return root / "revisions" / f"b{baseline}" if baseline > 0 else root


def _campaign_tool_evolutions(
    campaign_dir: Path, manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """读取 Campaign 工具演进链；链损坏即失败关闭。"""

    try:
        return codex_upgrade_wire_transition.load_evolutions(campaign_dir, manifest)
    except codex_upgrade_wire_transition.WireTransitionError as error:
        raise ConfigurationError(f"工具演进链无法重放：{error}") from error


def _campaign_effective_tool_identity(
    campaign_dir: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Campaign 当前有效工具身份：最新演进的 to 完整身份，没有演进时为 plan 冻结身份。"""

    try:
        return codex_upgrade_wire_transition.effective_tool_identity(campaign_dir, manifest)
    except codex_upgrade_wire_transition.WireTransitionError as error:
        raise ConfigurationError(f"工具演进链无法重放：{error}") from error


def _timing_event_phase(ledger_dir: Path, sequence: int) -> str:
    for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir):
        if int(event["sequence"]) == sequence:
            return str(event["phase"])
    raise ConfigurationError(f"计时账本缺少序号 {sequence} 的事件。")



# ---------------------------------------------------------------------------
# 自实现：Claude 语义与 Codex 不同、此刻就能定的成员
# ---------------------------------------------------------------------------


def _verify_campaign_file_bindings(campaign_dir: Path, group: Any, label: str) -> None:
    """总计划里一组文件绑定（名字 → ``{path, sha256}`` 或空值）：路径在 Campaign 目录内、内容摘要未漂移。"""

    if group is None:
        return
    if not isinstance(group, Mapping):
        raise ConfigurationError(f"Campaign {label} 必须是对象。")
    for name, reference in group.items():
        if reference is None:
            continue
        if not isinstance(reference, Mapping) or set(reference) - {"path", "sha256", "bytes", "schema_version"}:
            raise ConfigurationError(f"Campaign {label}.{name} 绑定形态非法。")
        relative = reference.get("path")
        expected_sha = reference.get("sha256")
        if not isinstance(relative, str) or not SHA256_RE.fullmatch(str(expected_sha)):
            raise ConfigurationError(f"Campaign {label}.{name} 引用非法。")
        target = _campaign_file(campaign_dir, relative)
        if not target.is_file() or file_sha256(target) != expected_sha:
            raise ConfigurationError(f"Campaign {label} 摘要漂移：{relative}")


def load_campaign_manifest(
    path: Path,
    *,
    _control_epoch_bootstrap: bool = False,
    _skip_control_validation: bool = False,
) -> dict[str, Any]:
    """加载 Claude Campaign 总计划（``official-client-upgrade-campaign/v1``）：核对自摘要、归属客户端、必备字段与计划期
    输入绑定。

    与 Codex 同签名。Claude 没有控制纪元，``_control_epoch_bootstrap`` 只能为假；控制收据的语义核验由 P0 收口
    （B2-11）落实，VC 控制制品的语义重放由原子批次入口（B3-01）落实，这里只核两者的文件绑定；前序绑定（B2-09）
    只核形状——``null`` 或冻结前序 Campaign 身份的只读复用声明，与前序 Store 的实际身份在导入时核对。"""

    if _control_epoch_bootstrap:
        raise ConfigurationError("Claude Campaign 没有 control epoch。")
    campaign_dir = path.parent if path.name == "campaign.json" else path
    _validate_existing_campaign_path(campaign_dir)
    manifest_path = campaign_dir / "campaign.json"
    digest_path = campaign_dir / "campaign.sha256"
    if not manifest_path.is_file() or not digest_path.is_file():
        raise ConfigurationError("Campaign 缺少 campaign.json 或 campaign.sha256。")
    expected = digest_path.read_text(encoding="utf-8").strip()
    if not SHA256_RE.fullmatch(expected) or file_sha256(manifest_path) != expected:
        raise ConfigurationError("Campaign 核心清单摘要不一致，拒绝继续。")
    manifest = _read_json(manifest_path, "Campaign 核心清单")
    if manifest.get("schema_version") != CAMPAIGN_SCHEMA or manifest.get("client") != "claude":
        raise ConfigurationError("不是 Claude Campaign 总计划（schema 或 client 不符）。")
    missing = sorted(CAMPAIGN_REQUIRED_FIELDS - set(manifest))
    if missing:
        raise ConfigurationError(f"Claude Campaign 总计划缺少字段：{missing}")
    _campaign_coordinates(manifest)
    if not SAFE_ID_RE.fullmatch(str(manifest.get("campaign_id", ""))):
        raise ConfigurationError("Claude Campaign ID 非法。")
    claude_upgrade_predecessor.validate_predecessor_binding(
        manifest.get("predecessor"), campaign_id=str(manifest["campaign_id"])
    )
    identity = manifest.get("tool_identity")
    if not isinstance(identity, Mapping) or identity.get("client") != "claude":
        raise ConfigurationError("Claude Campaign 工具身份缺少 client 或归属不符。")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping) or configuration.get("platform") != CAMPAIGN_PLATFORM:
        raise ConfigurationError(f"Claude Campaign 只支持 {CAMPAIGN_PLATFORM} 平台。")
    groups = ("inputs", "vc_control") if _skip_control_validation else ("inputs", "vc_control", "control_receipts")
    for group_name in groups:
        _verify_campaign_file_bindings(campaign_dir, manifest.get(group_name), group_name)
    return manifest


def _campaign_timing_ledger_dir(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
) -> Path:
    """解析 Campaign 绑定的计时账本：Claude 没有控制纪元，只读总计划 ``control_receipts.upgrade_timing``。"""

    del campaign_dir
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping):
        raise ConfigurationError("Campaign 缺少 UpgradeTimingLedger 控制绑定。")
    ledger_dir = Path(str(timing.get("ledger_dir", "")))
    if not ledger_dir.is_absolute() or ledger_dir.is_symlink() or not ledger_dir.is_dir():
        raise ConfigurationError("Campaign UpgradeTimingLedger 路径不可信。")
    ledger_dir = ledger_dir.resolve(strict=True)
    plan_path = ledger_dir / "ledger.json"
    if plan_path.is_symlink() or not plan_path.is_file() or file_sha256(plan_path) != timing.get("ledger_plan_sha256"):
        raise ConfigurationError("Campaign 绑定的 UpgradeTimingLedger 计划摘要漂移。")
    if codex_upgrade_timing_ledger.plan_client(codex_upgrade_timing_ledger._load_json(plan_path, "计时账本计划")[0]) != "claude":
        raise ConfigurationError("Campaign 绑定的计时账本不属于 Claude。")
    return ledger_dir


def _tool_tree_entries(tool_root: Path) -> list[dict[str, Any]]:
    """按 Claude 身份策略（v3）列出两个受管根的文件摘要，条目键为 ``<根目录名>/<相对路径>``。

    ``tool_root`` 是 ``official_client_capture`` 目录（或其执行副本），同级的 ``official_client_control`` 一并计入。"""

    policy = claude_tool_identity_policy.load_policy()
    digests = claude_tool_identity_policy.managed_tree_digests(Path(tool_root).resolve().parents[1], policy)
    return [{"path": key, "sha256": digests[key]} for key in sorted(digests)]


def _tool_identity(*, include_git: bool = True) -> dict[str, Any]:
    """Claude 受管工具身份（Campaign 总计划 ``tool_identity``）：v3 引擎的五摘要与两个编排器闭包，加整树条目与摘要。"""

    tool_root = Path(__file__).resolve().parent
    repository = tool_root.parents[1]
    try:
        policy = claude_tool_identity_policy.load_policy()
        identity = claude_tool_identity_policy.compute_identity(policy, repository)
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"Claude 工具身份策略无法计算：{error}") from error
    entries = _tool_tree_entries(tool_root)
    files_sha256 = _fingerprint({"entries": entries})
    if files_sha256 != identity["tool_files_sha256"]:
        raise ConfigurationError("Claude 工具整树摘要与身份引擎不一致。")
    return {
        "client": "claude",
        "git_commit": _git_commit(repository) if include_git else None,
        "entry_count": len(entries),
        "files_sha256": files_sha256,
        "entries": entries,
        "policy_version": identity["policy_version"],
        "policy_sha256": identity["policy_sha256"],
        "wire_producer_sha256": identity["wire_producer_sha256"],
        "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
        "control_sha256": identity["control_sha256"],
        "orchestrator_closures": identity["orchestrator_closures"],
    }


def _requires_complete_vc_artifacts(value: Mapping[str, Any] | str) -> bool:
    """Claude 从第一个受管版本起就走完整 VC 制品流程，没有 Codex 0.154 之前的旧口径。"""

    del value
    return True


def _compile_and_run_vc_batch_legacy(
    arguments: argparse.Namespace,
    *,
    governance: dict[str, Any],
    _compiler: Any | None,
) -> tuple[dict[str, Any], int]:
    """Claude 只有原子批次模型（staging），没有改造 4 之前的 legacy 批次 Campaign。"""

    del arguments, governance, _compiler
    raise ConfigurationError("Claude Campaign 只使用原子批次模型，不存在 legacy 批次")



# ---------------------------------------------------------------------------
# 迁入薄壳：共享流程在 upgrade_control_plane，以 Claude 适配器转调
# ---------------------------------------------------------------------------


def _abort_staging_attempt_without_parent(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    plan: Mapping[str, Any],
    sequence: int,
    phase: str,
    attempt: int,
    stage: str,
    failure_kind: str,
    error_type: str,
    error_message: str | None = None,
    error_signature: str | None = None,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._abort_staging_attempt_without_parent``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._abort_staging_attempt_without_parent(campaign_dir, attempt_dir, plan=plan, sequence=sequence, phase=phase, attempt=attempt, stage=stage, failure_kind=failure_kind, error_type=error_type, error_message=error_message, error_signature=error_signature)


def _accounting_resolve_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._accounting_resolve_command``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._accounting_resolve_command(arguments)


def _accounting_resolve_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    operation_id: str,
    estimated_count: int | None,
    evidence: Path | None,
    reason: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._accounting_resolve_preview``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._accounting_resolve_preview(campaign_dir, manifest, operation_id=operation_id, estimated_count=estimated_count, evidence=evidence, reason=reason)


def _append_timing_ledger_batch_events(
    ledger_dir: Path,
    events: list[tuple[str, str]],
    *,
    phase: str,
    sequence: int,
    expected_head: int,
) -> list[dict[str, Any]]:
    """共享流程在 ``upgrade_control_plane._append_timing_ledger_batch_events``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._append_timing_ledger_batch_events(ledger_dir, events, phase=phase, sequence=sequence, expected_head=expected_head)


def _apply_accounting_resolution(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    preview: Mapping[str, Any],
    *,
    approved_by: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._apply_accounting_resolution``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._apply_accounting_resolution(campaign_dir, manifest, preview, approved_by=approved_by)


def _apply_campaign_resume(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    preview: Mapping[str, Any],
    *,
    approved_by: str,
    next_steps: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._apply_campaign_resume``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._apply_campaign_resume(campaign_dir, manifest, preview, approved_by=approved_by, next_steps=next_steps)


def _archive_uncommitted_formal_artifacts(
    campaign_dir: Path,
    *,
    sequence: int,
    phase: str,
    attempt: int,
) -> list[str]:
    """共享流程在 ``upgrade_control_plane._archive_uncommitted_formal_artifacts``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._archive_uncommitted_formal_artifacts(campaign_dir, sequence=sequence, phase=phase, attempt=attempt)


def _assert_revision_admission(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    command: str,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._assert_revision_admission``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._assert_revision_admission(campaign_dir, manifest, command=command)


def _authorize_and_commit_non_failure_baseline(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    candidate_id: str,
    revision: int,
    number: int,
    current_baseline: int,
    kind: str,
    recovery: Mapping[str, Any],
    batch: Mapping[str, Any],
    head_after: Mapping[str, Any],
    decision: Mapping[str, Any],
    root_cause_id: str,
    operation_id: str,
    ledger_dir: Path,
    ledger_event_type: str,
    next_action: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._authorize_and_commit_non_failure_baseline``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._authorize_and_commit_non_failure_baseline(campaign_dir, manifest, candidate_id=candidate_id, revision=revision, number=number, current_baseline=current_baseline, kind=kind, recovery=recovery, batch=batch, head_after=head_after, decision=decision, root_cause_id=root_cause_id, operation_id=operation_id, ledger_dir=ledger_dir, ledger_event_type=ledger_event_type, next_action=next_action, result=result)


def _b0_evaluator_authorized_digest_history(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
) -> list[dict[str, str]]:
    """共享流程在 ``upgrade_control_plane._b0_evaluator_authorized_digest_history``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._b0_evaluator_authorized_digest_history(campaign_dir, manifest, candidate_id)


def _bootstrap_noop_first_batch(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    sequence: int,
    state_dir: Path,
    run_arguments: argparse.Namespace,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._bootstrap_noop_first_batch``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._bootstrap_noop_first_batch(campaign_dir, manifest, sequence=sequence, state_dir=state_dir, run_arguments=run_arguments)


def _campaign_batch_model(campaign_dir: Path) -> str:
    """共享流程在 ``upgrade_control_plane._campaign_batch_model``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_batch_model(campaign_dir)


def _campaign_plan_deadline(campaign_dir: Path) -> str | None:
    """共享流程在 ``upgrade_control_plane._campaign_plan_deadline``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_plan_deadline(campaign_dir)


def _campaign_resume_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._campaign_resume_command``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_resume_command(arguments)


def _campaign_resume_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    fix_commit: str,
    regression_receipt: Path,
    reason: str,
    control_root: Path | None,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._campaign_resume_preview``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_resume_preview(campaign_dir, manifest, fix_commit=fix_commit, regression_receipt=regression_receipt, reason=reason, control_root=control_root)


def _campaign_uses_staging_model(campaign_dir: Path, plan: Mapping[str, Any]) -> bool:
    """共享流程在 ``upgrade_control_plane._campaign_uses_staging_model``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_uses_staging_model(campaign_dir, plan)


def _campaign_vc_plan(campaign_dir: Path) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._campaign_vc_plan``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._campaign_vc_plan(campaign_dir)


def _candidate_accounting_checks(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    *,
    head: Mapping[str, Any],
    ledger_dir: Path,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._candidate_accounting_checks``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._candidate_accounting_checks(campaign_dir, manifest, candidate_id, head=head, ledger_dir=ledger_dir)


def _committed_vc_sequences(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[int]:
    """共享流程在 ``upgrade_control_plane._committed_vc_sequences``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._committed_vc_sequences(campaign_dir, manifest)


def _compile_and_run_vc_batch_staging(
    arguments: argparse.Namespace,
    *,
    governance: dict[str, Any],
    _compiler: Any | None,
) -> tuple[dict[str, Any], int]:
    """共享流程在 ``upgrade_control_plane._compile_and_run_vc_batch_staging``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._compile_and_run_vc_batch_staging(arguments, governance=governance, _compiler=_compiler)


def _complete_timing_ledger_phase_after_batch(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    *,
    phase: str,
    sequence: int,
    ledger_dir: Path,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._complete_timing_ledger_phase_after_batch``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._complete_timing_ledger_phase_after_batch(campaign_dir, plan, phase=phase, sequence=sequence, ledger_dir=ledger_dir)


def _empty_batch_governance() -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._empty_batch_governance``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._empty_batch_governance()


def _environment_isolate_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    repair_receipt: Path,
    clean_receipt: Path,
    reason: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._environment_isolate_preview``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._environment_isolate_preview(campaign_dir, manifest, repair_receipt=repair_receipt, clean_receipt=clean_receipt, reason=reason)


def _evaluation_action_kind(command: Sequence[str]) -> str | None:
    """共享流程在 ``upgrade_control_plane._evaluation_action_kind``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_action_kind(command)


def _evaluation_approval_revision_locked(
    arguments: argparse.Namespace,
    campaign_dir: Path,
    campaign_root: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    reviewer: str,
    approve_sha256: Any,
    admission: dict[str, Any] | None,
    ledger_dir: Path,
    reconciler: Any,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evaluation_approval_revision_locked``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_approval_revision_locked(arguments, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, admission, ledger_dir, reconciler)


def _evaluation_defect_admission(
    arguments: argparse.Namespace,
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    attempt_root: Path,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evaluation_defect_admission``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_defect_admission(arguments, campaign_dir, manifest, facts, attempt_root=attempt_root)


def _evaluation_epoch_for_recovery(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    *,
    current_evidence: str,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._evaluation_epoch_for_recovery``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_epoch_for_recovery(campaign_dir, manifest, attempt_root, current_evidence=current_evidence)


def _evaluation_failure_diagnosis_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    revision: int,
    baseline: int,
    *,
    candidate: Mapping[str, Any],
    attempt: Mapping[str, Any],
    classification: Mapping[str, Any],
    rules: Sequence[str],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evaluation_failure_diagnosis_facts``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_failure_diagnosis_facts(campaign_dir, manifest, candidate_id, revision, baseline, candidate=candidate, attempt=attempt, classification=classification, rules=rules)


def _evaluation_failure_scope(
    campaign_dir: Path,
    candidate: Mapping[str, Any],
    attempt: Mapping[str, Any],
    evaluation_run: Mapping[str, Any],
    assertions_root: Path,
) -> tuple[dict[str, Any], str]:
    """共享流程在 ``upgrade_control_plane._evaluation_failure_scope``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_failure_scope(campaign_dir, candidate, attempt, evaluation_run, assertions_root)


def _evaluation_recover_locked(
    arguments: argparse.Namespace,
    action: str,
    campaign_dir: Path,
    campaign_root: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    reviewer: str,
    approve_sha256: Any,
    root_cause_class: Any,
    admission: dict[str, Any] | None,
    ledger_dir: Path,
    reconciler: Any,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evaluation_recover_locked``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_recover_locked(arguments, action, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, root_cause_class, admission, ledger_dir, reconciler)


def _evaluation_reevaluate_locked(
    arguments: argparse.Namespace,
    campaign_dir: Path,
    campaign_root: Path,
    manifest: dict[str, Any],
    candidate_id: str,
    reviewer: str,
    approve_sha256: Any,
    admission: dict[str, Any] | None,
    ledger_dir: Path,
    reconciler: Any,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evaluation_reevaluate_locked``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evaluation_reevaluate_locked(arguments, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, admission, ledger_dir, reconciler)


def _evidence_conflict_quarantine_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    reason: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._evidence_conflict_quarantine_preview``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evidence_conflict_quarantine_preview(campaign_dir, manifest, phase=phase, candidate_id=candidate_id, reason=reason)


def _evidence_root_conflicts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> list[dict[str, Any]]:
    """共享流程在 ``upgrade_control_plane._evidence_root_conflicts``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evidence_root_conflicts(campaign_dir, manifest, phase=phase, candidate_id=candidate_id)


def _evidence_root_registrants(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    exclude_attempt_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """共享流程在 ``upgrade_control_plane._evidence_root_registrants``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._evidence_root_registrants(campaign_dir, manifest, phase=phase, candidate_id=candidate_id, exclude_attempt_id=exclude_attempt_id)


def _formal_artifact_attempt(
    campaign_dir: Path,
    *,
    sequence: int,
    phase: str,
) -> int | None:
    """共享流程在 ``upgrade_control_plane._formal_artifact_attempt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._formal_artifact_attempt(campaign_dir, sequence=sequence, phase=phase)


def _freeze_evaluation_output_bindings(
    campaign_dir: Path,
    candidate_id: str,
    baseline: int,
    actions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """共享流程在 ``upgrade_control_plane._freeze_evaluation_output_bindings``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._freeze_evaluation_output_bindings(campaign_dir, candidate_id, baseline, actions)


def _implicit_r1_candidate_id(campaign_dir: Path, manifest: Mapping[str, Any]) -> str:
    """共享流程在 ``upgrade_control_plane._implicit_r1_candidate_id``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._implicit_r1_candidate_id(campaign_dir, manifest)


def _incremental_noop_job_ids(attempt_root: Path, payload: Mapping[str, Any]) -> list[str]:
    """共享流程在 ``upgrade_control_plane._incremental_noop_job_ids``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._incremental_noop_job_ids(attempt_root, payload)


def _invalidation_phase(summary: Mapping[str, Any], ledger_dir: Path) -> str:
    """共享流程在 ``upgrade_control_plane._invalidation_phase``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._invalidation_phase(summary, ledger_dir)


def _job_recovery_root_keys(job: Job) -> list[str]:
    """共享流程在 ``upgrade_control_plane._job_recovery_root_keys``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._job_recovery_root_keys(job)


def _ledger_last_event_of_type(ledger_dir: Path, event_type: str) -> tuple[dict[str, Any], str] | None:
    """共享流程在 ``upgrade_control_plane._ledger_last_event_of_type``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._ledger_last_event_of_type(ledger_dir, event_type)


def _load_prepared_staging_attempt(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    owner_nonce: str | None,
    sequence: int,
    phase: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._load_prepared_staging_attempt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._load_prepared_staging_attempt(campaign_dir, attempt_dir, owner_nonce=owner_nonce, sequence=sequence, phase=phase)


def _locate_evaluation_failed_run(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    revision: int,
    baseline: int,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._locate_evaluation_failed_run``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._locate_evaluation_failed_run(campaign_dir, manifest, candidate_id, revision, baseline)


def _mutable_command_coordinates(
    arguments: argparse.Namespace,
) -> tuple[str, str, str | None, bool] | None:
    """共享流程在 ``upgrade_control_plane._mutable_command_coordinates``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._mutable_command_coordinates(arguments)


def _next_recovery_revision(attempt_root: Path) -> str:
    """共享流程在 ``upgrade_control_plane._next_recovery_revision``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._next_recovery_revision(attempt_root)


def _next_staging_attempt(sequence_dir: Path) -> int:
    """共享流程在 ``upgrade_control_plane._next_staging_attempt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._next_staging_attempt(sequence_dir)


def _optional_json_object(path: Path) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._optional_json_object``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._optional_json_object(path)


def _prepare_atomic_batch_governance(arguments: argparse.Namespace) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._prepare_atomic_batch_governance``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._prepare_atomic_batch_governance(arguments)


def _project_ledger_required(campaign_mode: Any, target_version: Any) -> bool:
    """共享流程在 ``upgrade_control_plane._project_ledger_required``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._project_ledger_required(campaign_mode, target_version)


def _publish_ledger_receipt_copies(
    ledger_dir: Path,
    subject: str,
    sources: Mapping[str, Path],
) -> list[dict[str, str]]:
    """共享流程在 ``upgrade_control_plane._publish_ledger_receipt_copies``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._publish_ledger_receipt_copies(ledger_dir, subject, sources)


def _publish_staging_file(source: Path, target: Path, campaign_dir: Path) -> None:
    """共享流程在 ``upgrade_control_plane._publish_staging_file``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._publish_staging_file(source, target, campaign_dir)


def _read_staging_commit(commit_path: Path) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._read_staging_commit``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._read_staging_commit(commit_path)


def _reconcile_prepared_parent_run(
    campaign_dir: Path,
    run_dir: Path,
    state: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._reconcile_prepared_parent_run``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._reconcile_prepared_parent_run(campaign_dir, run_dir, state, plan=plan)


def _reconcile_staging_abort_receipt(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    sequence: int,
    phase: str,
    attempt: int,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._reconcile_staging_abort_receipt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._reconcile_staging_abort_receipt(campaign_dir, attempt_dir, sequence=sequence, phase=phase, attempt=attempt)


def _reconcile_staging_orphans(
    campaign_dir: Path,
    state_dir: Path,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._reconcile_staging_orphans``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._reconcile_staging_orphans(campaign_dir, state_dir, plan=plan)


def _recovery_preview_retry_root_cause_ids(preview_path: Any) -> list[str] | None:
    """共享流程在 ``upgrade_control_plane._recovery_preview_retry_root_cause_ids``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._recovery_preview_retry_root_cause_ids(preview_path)


def _recovery_root_suffix(recovery_revision: str) -> str:
    """共享流程在 ``upgrade_control_plane._recovery_root_suffix``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._recovery_root_suffix(recovery_revision)


def _refuse_seal_chain_on_evolution_invalidated_attempt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    action_plan: Any,
) -> None:
    """共享流程在 ``upgrade_control_plane._refuse_seal_chain_on_evolution_invalidated_attempt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._refuse_seal_chain_on_evolution_invalidated_attempt(campaign_dir, manifest, phase=phase, action_plan=action_plan)


def _refuse_vc1_seal_batch_against_bundle_state(
    manifest: Mapping[str, Any],
    *,
    phase: str,
    action_plan: Any,
) -> None:
    """共享流程在 ``upgrade_control_plane._refuse_vc1_seal_batch_against_bundle_state``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._refuse_vc1_seal_batch_against_bundle_state(manifest, phase=phase, action_plan=action_plan)


def _require_stage_replay_proof_for_checkpoint(ledger_dir: Path, phase: str, checkpoint_path: Path) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._require_stage_replay_proof_for_checkpoint``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._require_stage_replay_proof_for_checkpoint(ledger_dir, phase, checkpoint_path)


def _require_tool_evolution_registered(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None = None,
    action: str,
) -> dict[str, Any] | None:
    """共享流程在 ``upgrade_control_plane._require_tool_evolution_registered``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._require_tool_evolution_registered(campaign_dir, manifest, current=current, action=action)


def _staging_abort_accounted(campaign_dir: Path, sequence: int, attempt: int) -> bool:
    """共享流程在 ``upgrade_control_plane._staging_abort_accounted``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._staging_abort_accounted(campaign_dir, sequence, attempt)


def _staging_attempt_dirs(sequence_dir: Path) -> list[tuple[int, Path]]:
    """共享流程在 ``upgrade_control_plane._staging_attempt_dirs``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._staging_attempt_dirs(sequence_dir)


def _staging_commit_path(campaign_dir: Path, sequence: int, phase: str) -> Path:
    """共享流程在 ``upgrade_control_plane._staging_commit_path``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._staging_commit_path(campaign_dir, sequence, phase)


def _staging_sequence_dir(campaign_dir: Path, sequence: int, phase: str) -> Path:
    """共享流程在 ``upgrade_control_plane._staging_sequence_dir``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._staging_sequence_dir(campaign_dir, sequence, phase)


def _supervisor_run_reconciled(campaign_dir: Path, run_dir: Path) -> bool:
    """共享流程在 ``upgrade_control_plane._supervisor_run_reconciled``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._supervisor_run_reconciled(campaign_dir, run_dir)


def _timing_ledger_batch_events(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    phase: str,
    ledger_dir: Path,
) -> tuple[int, list[tuple[str, str]]]:
    """共享流程在 ``upgrade_control_plane._timing_ledger_batch_events``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._timing_ledger_batch_events(campaign_dir, manifest, plan, phase=phase, ledger_dir=ledger_dir)


def _tool_evolution_quiescence_problems(campaign_dir: Path) -> list[str]:
    """共享流程在 ``upgrade_control_plane._tool_evolution_quiescence_problems``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._tool_evolution_quiescence_problems(campaign_dir)


def _tool_evolution_unregistered_drift(
    effective_identity: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """共享流程在 ``upgrade_control_plane._tool_evolution_unregistered_drift``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._tool_evolution_unregistered_drift(effective_identity, current)


def _transient_environment_admission(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    attempt_root: Path,
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._transient_environment_admission``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._transient_environment_admission(campaign_dir, manifest, facts, attempt_root=attempt_root, attempt=attempt)


def _verify_current_deployment_receipt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    receipt_path: Path,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._verify_current_deployment_receipt``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._verify_current_deployment_receipt(campaign_dir, manifest, receipt_path)


def _verify_failed_run_reconciled(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    ledger_dir: Path,
    failed: Mapping[str, Any],
    *,
    head: Mapping[str, Any],
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._verify_failed_run_reconciled``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._verify_failed_run_reconciled(campaign_dir, manifest, ledger_dir, failed, head=head)


def _write_candidate_revision_pending(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    revision: int,
    candidate_id: str,
    supersedes: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """共享流程在 ``upgrade_control_plane._write_candidate_revision_pending``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._write_candidate_revision_pending(campaign_dir, manifest, revision=revision, candidate_id=candidate_id, supersedes=supersedes)


def _write_staging_abort(
    attempt_dir: Path,
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    phase: str,
    sequence: int,
    attempt: int,
    stage: str,
    failure_kind: str,
    error_type: str,
    root_cause_id: str,
    batch_sha256: str | None,
    manifest_sha256: str | None,
    parent_run_dir: str | None,
    parent_run_state: str | None,
    reconciliation_receipt: Mapping[str, Any] | None,
    error_message: str | None = None,
    error_signature: str | None = None,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._write_staging_abort``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._write_staging_abort(attempt_dir, campaign_id=campaign_id, campaign_plan_sha256=campaign_plan_sha256, phase=phase, sequence=sequence, attempt=attempt, stage=stage, failure_kind=failure_kind, error_type=error_type, root_cause_id=root_cause_id, batch_sha256=batch_sha256, manifest_sha256=manifest_sha256, parent_run_dir=parent_run_dir, parent_run_state=parent_run_state, reconciliation_receipt=reconciliation_receipt, error_message=error_message, error_signature=error_signature)


def _write_staging_batch_artifacts(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    plan: Mapping[str, Any],
    batch: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
    owner_nonce: str,
    prepared_at_utc: str,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane._write_staging_batch_artifacts``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane._write_staging_batch_artifacts(campaign_dir, attempt_dir, plan=plan, batch=batch, run_manifest=run_manifest, owner_nonce=owner_nonce, prepared_at_utc=prepared_at_utc)


def compile_and_run_vc_batch(
    arguments: argparse.Namespace,
    *,
    _compiler: Any | None = None,
) -> tuple[dict[str, Any], int]:
    """共享流程在 ``upgrade_control_plane.compile_and_run_vc_batch``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane.compile_and_run_vc_batch(arguments, _compiler=_compiler)


def compile_vc_batch(
    arguments: argparse.Namespace,
    *,
    staging_attempt_dir: Path | None = None,
    owner_nonce: str | None = None,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane.compile_vc_batch``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane.compile_vc_batch(arguments, staging_attempt_dir=staging_attempt_dir, owner_nonce=owner_nonce)


def recovery_preview_continuity(
    campaign_dir: Path,
    *,
    manifest: Mapping[str, Any],
    phase: str,
    candidate_id: str | None,
    source_attempt_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    """共享流程在 ``upgrade_control_plane.recovery_preview_continuity``；以 Claude 适配器转调。"""

    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        return upgrade_control_plane.recovery_preview_continuity(campaign_dir, manifest=manifest, phase=phase, candidate_id=candidate_id, source_attempt_id=source_attempt_id, output_dir=output_dir)



# ---------------------------------------------------------------------------
# 待实现：依赖后续工作项的服务面成员
# ---------------------------------------------------------------------------


# B2-11（VC-0 收口续作与 P0 收据）


def create_campaign(arguments: argparse.Namespace) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-11（VC-0 收口续作与 P0 收据） 落实）。"""

    del arguments
    raise _pending("create_campaign", "B2-11（VC-0 收口续作与 P0 收据）")


def campaign_status(
    campaign_dir: Path,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-11（VC-0 收口续作与 P0 收据） 落实）。"""

    del campaign_dir, candidate_id
    raise _pending("campaign_status", "B2-11（VC-0 收口续作与 P0 收据）")


# B2-08（工具演进预览）


def _tool_evolution_preview(
    campaign_dir: Path,
    manifest: dict[str, Any],
    *,
    fix_commit: str,
    reason: str,
    control_root: Path | None,
    compatibility_receipt: Path | None = None,
    activation_certification: Path | None = None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（工具演进预览） 落实）。"""

    del campaign_dir, manifest, fix_commit, reason, control_root, compatibility_receipt, activation_certification
    raise _pending("_tool_evolution_preview", "B2-08（工具演进预览）")


# B2-06（取证 Job 动作体与环境闭环）：实现在 ``claude_upgrade_capture``，这里是同名同签名的薄壳（延迟导入，避免与
# 子模块循环导入；子模块在调用时经本模块属性取常量与互调成员，测试与 Job 演练对本模块的改写照样生效）。


def _campaign_jobs(
    campaign_dir: Path,
    manifest: dict[str, Any],
    phase: str,
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
    candidate_purpose: str | None = None,
    use_approved_scenario: bool | None = None,
) -> list[Job]:
    """服务面成员（B2-06）：从 Campaign 冻结的场景清单副本展开某阶段的 Job；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._campaign_jobs(
        campaign_dir,
        manifest,
        phase,
        candidate_id=candidate_id,
        runtime_image=runtime_image,
        profile_id=profile_id,
        profile_digest=profile_digest,
        build_id=build_id,
        deployed_version=deployed_version,
        candidate_image_id=candidate_image_id,
        source_tree_sha256=source_tree_sha256,
        candidate_purpose=candidate_purpose,
        use_approved_scenario=use_approved_scenario,
    )


def _job_execution_sha256(job: Job) -> str:
    """服务面成员（B2-06）：Job 执行定义的摘要；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._job_execution_sha256(job)


def _job_tool_dependency_files(
    job: Job,
    tool_identity: Mapping[str, Any],
) -> dict[str, str]:
    """服务面成员（B2-06）：Job 显式声明的工具依赖 → 文件摘要；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._job_tool_dependency_files(job, tool_identity)


def _run_job_with_retry(
    job: Job,
    log_root: Path,
    scenario_context: ScenarioReceiptContext | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员（B2-06）：同一 attempt 内对失败 Job 有限补跑并做环境闭环；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._run_job_with_retry(
        job, log_root, scenario_context, identity=identity, tool_identity=tool_identity, deadline=deadline, heartbeat=heartbeat
    )


def _probe_capture_environment(
    manifest: dict[str, Any],
    output_dir: Path,
    phase: str,
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员（B2-06）：Claude 只读环境探针；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._probe_capture_environment(manifest, output_dir, phase, deadline=deadline, heartbeat=heartbeat)


def _probe_snapshot_digests(manifest: Mapping[str, Any]) -> dict[str, str]:
    """服务面成员（B2-06）：探针清单的快照种类 → 摘要；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._probe_snapshot_digests(manifest)


def _verify_official_binaries(
    manifest: dict[str, Any],
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员（B2-06）：目标客户端两处安装与运行镜像核验；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._verify_official_binaries(manifest, deadline=deadline, heartbeat=heartbeat)


def _verify_execution_tree(capture_root: Path | None) -> None:
    """服务面成员（B2-06）：采集执行副本与受管树逐字一致；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._verify_execution_tree(capture_root)


def _attempt_deadline(
    arguments: argparse.Namespace,
    phase: str,
) -> incremental_recovery.WallClockDeadline:
    """服务面成员（B2-06）：一次 attempt 的全阶段 deadline；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._attempt_deadline(arguments, phase)


def _attempt_failure_facts(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """服务面成员（B2-06）：attempt 失败观测与根因（Claude 根因码表）；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._attempt_failure_facts(payload)


def _load_capture_attempt(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
    *,
    _historical_manifest_controls: bool = False,
    _verified_campaign_manifest: Mapping[str, Any] | None = None,
    _replay_evidence_permissions: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """服务面成员（B2-06）：读取并重验正式 attempt；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._load_capture_attempt(
        campaign_dir,
        phase,
        candidate_id,
        attempt_id,
        _historical_manifest_controls=_historical_manifest_controls,
        _verified_campaign_manifest=_verified_campaign_manifest,
        _replay_evidence_permissions=_replay_evidence_permissions,
    )


def _load_capture_reservation(
    campaign_dir: Path,
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
    _manifest: Mapping[str, Any] | None = None,
    _historical_manifest_controls: bool = False,
) -> dict[str, Any]:
    """服务面成员（B2-06）：读取正式 attempt 的原子预约收据；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._load_capture_reservation(
        campaign_dir,
        attempt_root,
        phase=phase,
        candidate_id=candidate_id,
        _manifest=_manifest,
        _historical_manifest_controls=_historical_manifest_controls,
    )


def _ordered_capture_attempts(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    *,
    _historical_manifest_controls: bool = False,
    _manifest: Mapping[str, Any] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """服务面成员（B2-06）：按预约时间倒序列出正式 attempt；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._ordered_capture_attempts(
        campaign_dir, phase, candidate_id, _historical_manifest_controls=_historical_manifest_controls, _manifest=_manifest
    )


def _active_unsealed_attempts(
    campaign_dir: Path,
    phase: str,
    *,
    _manifest: Mapping[str, Any] | None = None,
    _baseline: int | None = None,
) -> list[str]:
    """服务面成员（B2-06）：未完成预约或等待封存的正式 attempt；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._active_unsealed_attempts(campaign_dir, phase, _manifest=_manifest, _baseline=_baseline)


def _campaign_attempt_roots(
    campaign_dir: Path,
) -> list[tuple[str, str | None, Path]]:
    """服务面成员（B2-06）：枚举 Campaign 内全部正式 attempt 目录；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._campaign_attempt_roots(campaign_dir)


def _capture_stage_attempt_context(
    campaign_dir: Path,
    stage: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    """服务面成员（B2-06）：阶段结果绑定的 attempt；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._capture_stage_attempt_context(campaign_dir, stage, phase=phase, candidate_id=candidate_id)


def _wait_for_monitor_finalization(run_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """服务面成员（B2-06）：monitor 在线时有界等待其封存终态；实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._wait_for_monitor_finalization(run_dir, state)


# B2-07（seal 链幂等与四阶段化）


def _replay_attempt_evidence_permissions(
    attempt_root: Path,
    payload: Mapping[str, Any],
    *,
    root_relocations: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del attempt_root, payload, root_relocations
    raise _pending("_replay_attempt_evidence_permissions", "B2-07（seal 链幂等与四阶段化）")


def _replay_attempt_recovery_evidence_permissions(
    segment_root: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del segment_root, payload
    raise _pending("_replay_attempt_recovery_evidence_permissions", "B2-07（seal 链幂等与四阶段化）")


def _evidence_root_relocations(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> dict[str, str]:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del campaign_dir, phase, candidate_id, attempt_id
    raise _pending("_evidence_root_relocations", "B2-07（seal 链幂等与四阶段化）")


def _evidence_conflict_quarantines(campaign_dir: Path) -> list[dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del campaign_dir
    raise _pending("_evidence_conflict_quarantines", "B2-07（seal 链幂等与四阶段化）")


def _conflict_quarantined_attempts(campaign_dir: Path) -> set[tuple[str, str | None, str]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del campaign_dir
    raise _pending("_conflict_quarantined_attempts", "B2-07（seal 链幂等与四阶段化）")


def _attempt_conflict_quarantine_receipt(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """服务面成员（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del campaign_dir, phase, candidate_id, attempt_id
    raise _pending("_attempt_conflict_quarantine_receipt", "B2-07（seal 链幂等与四阶段化）")


# B2-08（VC-1 恢复接入共享对账）


def _attempt_continuity_drift_receipt(campaign_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, attempt_id
    raise _pending("_attempt_continuity_drift_receipt", "B2-08（VC-1 恢复接入共享对账）")


def _attempt_evolution_impact(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, manifest, attempt_root, phase, candidate_id
    raise _pending("_attempt_evolution_impact", "B2-08（VC-1 恢复接入共享对账）")


def _attempt_evolution_invalidated_job_ids(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    attempt: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> list[str]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, manifest, attempt_root, attempt, phase, candidate_id
    raise _pending("_attempt_evolution_invalidated_job_ids", "B2-08（VC-1 恢复接入共享对账）")


def _attempt_recovery_segment_terminal_failed(
    campaign_dir: Path,
    *,
    candidate_id: str,
    attempt_id: str,
    segment_root: Path,
) -> bool:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, candidate_id, attempt_id, segment_root
    raise _pending("_attempt_recovery_segment_terminal_failed", "B2-08（VC-1 恢复接入共享对账）")


def _authoritative_recovery_execute_jobs(
    campaign_dir: Path,
    candidate_id: str,
    reservation: Mapping[str, Any],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, candidate_id, reservation
    raise _pending("_authoritative_recovery_execute_jobs", "B2-08（VC-1 恢复接入共享对账）")


def _load_attempt_recovery_reservation(
    campaign_dir: Path,
    segment_root: Path,
    *,
    candidate_id: str,
    attempt_id: str,
    recovery_revision: str,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, segment_root, candidate_id, attempt_id, recovery_revision
    raise _pending("_load_attempt_recovery_reservation", "B2-08（VC-1 恢复接入共享对账）")


def _load_attempt_recovery_segment(
    campaign_dir: Path,
    candidate_id: str,
    attempt_id: str,
    recovery_revision: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, candidate_id, attempt_id, recovery_revision
    raise _pending("_load_attempt_recovery_segment", "B2-08（VC-1 恢复接入共享对账）")


def _require_recovery_preview_scope_equals_frozen_jobs(
    preview: Mapping[str, Any],
    execute_jobs: Sequence[str],
    *,
    label: str,
) -> None:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del preview, execute_jobs, label
    raise _pending("_require_recovery_preview_scope_equals_frozen_jobs", "B2-08（VC-1 恢复接入共享对账）")


def recovery_reuse_check(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    source_attempt_id: str,
    reuse_job_ids: Iterable[str],
    execute_job_ids: Iterable[str],
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, phase, candidate_id, source_attempt_id, reuse_job_ids, execute_job_ids
    raise _pending("recovery_reuse_check", "B2-08（VC-1 恢复接入共享对账）")


def _rerun_all_invalidated_attempts(campaign_dir: Path) -> set[tuple[str, str | None, str]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir
    raise _pending("_rerun_all_invalidated_attempts", "B2-08（VC-1 恢复接入共享对账）")


def _isolation_invalidated_attempts(campaign_dir: Path) -> set[tuple[str, str | None, str]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir
    raise _pending("_isolation_invalidated_attempts", "B2-08（VC-1 恢复接入共享对账）")


def _environment_isolations(campaign_dir: Path) -> list[dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir
    raise _pending("_environment_isolations", "B2-08（VC-1 恢复接入共享对账）")


def _campaign_contamination_facts(
    campaign_dir: Path,
    *,
    _manifest: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, _manifest
    raise _pending("_campaign_contamination_facts", "B2-08（VC-1 恢复接入共享对账）")


def _campaign_contamination_records(
    campaign_dir: Path,
    *,
    _manifest: Mapping[str, Any] | None = None,
) -> list[str]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, _manifest
    raise _pending("_campaign_contamination_records", "B2-08（VC-1 恢复接入共享对账）")


def _campaign_environment_decision(
    campaign_dir: Path,
    *,
    _manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, _manifest
    raise _pending("_campaign_environment_decision", "B2-08（VC-1 恢复接入共享对账）")


def _reject_contaminated_campaign(
    campaign_dir: Path,
    *,
    allow_metadata_only_repair: bool = False,
    attempt_root: Path | None = None,
    attempt: Mapping[str, Any] | None = None,
) -> None:
    """服务面成员（Claude 编排器尚未实现，由 B2-08（VC-1 恢复接入共享对账） 落实）。"""

    del campaign_dir, allow_metadata_only_repair, attempt_root, attempt
    raise _pending("_reject_contaminated_campaign", "B2-08（VC-1 恢复接入共享对账）")


# B2-10（Job 演练与客户端启动探测）


def _job_rehearsal_contract_from_manifest(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    target_scenario_override: Mapping[str, Any] | None = None,
    tool_files_sha256_override: str | None = None,
    recovery_rehearsal_receipt: Mapping[str, Any] | None = None,
    control_receipts_override: Mapping[str, Any] | None = None,
    _allow_historical_tool_identity: bool = False,
    _allow_bound_evidence_label_digest: bool = False,
    _require_incremental_noop_preflight_active: bool = True,
    _allow_stopped_metadata_only: bool = False,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B2-10（Job 演练与客户端启动探测） 落实）。"""

    del campaign_dir, manifest, target_scenario_override, tool_files_sha256_override, recovery_rehearsal_receipt, control_receipts_override, _allow_historical_tool_identity, _allow_bound_evidence_label_digest, _require_incremental_noop_preflight_active, _allow_stopped_metadata_only
    raise _pending("_job_rehearsal_contract_from_manifest", "B2-10（Job 演练与客户端启动探测）")


# B3-01（VC-2～VC-6 原子批次入口接入）


def _vc_campaign_plan(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, manifest
    raise _pending("_vc_campaign_plan", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _vc_checkpoint_path(campaign_dir: Path, phase: str, *, revision: int | None = None) -> Path:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, phase, revision
    raise _pending("_vc_checkpoint_path", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _vc_checkpoint_reference(
    campaign_dir: Path,
    path: Path,
    checkpoint: Mapping[str, Any],
) -> dict[str, str]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, path, checkpoint
    raise _pending("_vc_checkpoint_reference", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _replay_vc_checkpoint(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    phase: str,
    *,
    revision: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, plan, phase, revision
    raise _pending("_replay_vc_checkpoint", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _vc_run_manifest_from_batch(
    batch: Mapping[str, Any],
    *,
    batch_model: str = "legacy",
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del batch, batch_model
    raise _pending("_vc_run_manifest_from_batch", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _stage_path(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
    *,
    mode: str = "read",
) -> tuple[str, Path]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, stage, candidate_id, mode
    raise _pending("_stage_path", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _legacy_stage_path(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
) -> tuple[str, Path]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, stage, candidate_id
    raise _pending("_legacy_stage_path", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _load_stage_result(
    campaign_dir: Path,
    stage: str,
    candidate_id: str | None = None,
    *,
    _import_chain: frozenset[Path] | None = None,
    _replay_machine_receipts: bool = True,
    _shallow: bool = False,
    _ignore_checkpoint: bool = False,
    _import_cache: dict[tuple[str, str, str | None], dict[str, Any]] | None = None,
    _skip_evidence_scan: bool = False,
    _historical_manifest_controls: bool = False,
    _verified_campaign_manifest: Mapping[str, Any] | None = None,
    _baseline: int | None = None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, stage, candidate_id, _import_chain, _replay_machine_receipts, _shallow, _ignore_checkpoint, _import_cache, _skip_evidence_scan, _historical_manifest_controls, _verified_campaign_manifest, _baseline
    raise _pending("_load_stage_result", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _stage_read_source(
    campaign_dir: Path,
    candidate_id: str,
    baseline: int,
    stage: str,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, candidate_id, baseline, stage
    raise _pending("_stage_read_source", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _stage_write_target(
    campaign_dir: Path,
    candidate_id: str,
    baseline: int,
    stage: str,
) -> Path:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, candidate_id, baseline, stage
    raise _pending("_stage_write_target", "B3-01（VC-2～VC-6 原子批次入口接入）")


def _campaign_stage_replay_facts(campaign_dir: Path, run_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-01（VC-2～VC-6 原子批次入口接入） 落实）。"""

    del campaign_dir, run_manifest
    raise _pending("_campaign_stage_replay_facts", "B3-01（VC-2～VC-6 原子批次入口接入）")


# B3-02／B3-03（分类与画像 producer）


def _classification_official_authority(
    classification: dict[str, Any],
) -> dict[str, str]:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del classification
    raise _pending("_classification_official_authority", "B3-02／B3-03（分类与画像 producer）")


def _effective_classification_view(campaign_dir: Path, candidate_id: str, classification: dict[str, Any]) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del campaign_dir, candidate_id, classification
    raise _pending("_effective_classification_view", "B3-02／B3-03（分类与画像 producer）")


def _approved_rules(
    campaign_dir: Path,
    manifest: dict[str, Any],
    *,
    require_approved: bool,
) -> tuple[str, ...]:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del campaign_dir, manifest, require_approved
    raise _pending("_approved_rules", "B3-02／B3-03（分类与画像 producer）")


def _effective_assertion_profile(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any] | None:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del campaign_dir, candidate_id, baseline
    raise _pending("_effective_assertion_profile", "B3-02／B3-03（分类与画像 producer）")


def _approval_revision_baselines(campaign_dir: Path, candidate_id: str) -> list[int]:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del campaign_dir, candidate_id
    raise _pending("_approval_revision_baselines", "B3-02／B3-03（分类与画像 producer）")


def _approval_revision_delta(current_path: Path, revised_path: Path) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-02／B3-03（分类与画像 producer） 落实）。"""

    del current_path, revised_path
    raise _pending("_approval_revision_delta", "B3-02／B3-03（分类与画像 producer）")


# B3-04／B3-05（候选构建与 revision）


def _current_candidate_revision(campaign_dir: Path, manifest: Mapping[str, Any]) -> int | None:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, manifest
    raise _pending("_current_candidate_revision", "B3-04／B3-05（候选构建与 revision）")


def _current_candidate_revision_record(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
) -> tuple[int | None, dict[str, Any] | None]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, manifest
    raise _pending("_current_candidate_revision_record", "B3-04／B3-05（候选构建与 revision）")


def _read_candidate_revision_record(
    campaign_dir: Path,
    revision: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, revision
    raise _pending("_read_candidate_revision_record", "B3-04／B3-05（候选构建与 revision）")


def _require_candidate_revision(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    action: str,
) -> tuple[int, dict[str, Any] | None]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, manifest, action
    raise _pending("_require_candidate_revision", "B3-04／B3-05（候选构建与 revision）")


def _candidate_identity_snapshot(
    campaign_dir: Path,
    candidate_id: str,
    candidate_source: Path | None,
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, candidate_id, candidate_source
    raise _pending("_candidate_identity_snapshot", "B3-04／B3-05（候选构建与 revision）")


def _path_in_candidate_source(path: Path, source_root: Path, label: str) -> Path:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del path, source_root, label
    raise _pending("_path_in_candidate_source", "B3-04／B3-05（候选构建与 revision）")


def _candidate_failed_run_for_review(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    review_event: Mapping[str, Any],
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, manifest, candidate_id, review_event
    raise _pending("_candidate_failed_run_for_review", "B3-04／B3-05（候选构建与 revision）")


def _apply_candidate_runtime_override(
    campaign_dir: Path, manifest: dict[str, Any], candidate_id: str | None
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del campaign_dir, manifest, candidate_id
    raise _pending("_apply_candidate_runtime_override", "B3-04／B3-05（候选构建与 revision）")


def _verify_candidate_attempt_identity(
    manifest: dict[str, Any], identity: dict[str, Any]
) -> None:
    """服务面成员（Claude 编排器尚未实现，由 B3-04／B3-05（候选构建与 revision） 落实）。"""

    del manifest, identity
    raise _pending("_verify_candidate_attempt_identity", "B3-04／B3-05（候选构建与 revision）")


# B4-04／B4-05（评估基线与评估失败恢复）


def _assertions_root(campaign_dir: Path, candidate_id: str) -> Path:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, candidate_id
    raise _pending("_assertions_root", "B4-04／B4-05（评估基线与评估失败恢复）")


def _current_evaluation_baseline(
    campaign_dir: Path,
    candidate_id: str,
) -> tuple[int, dict[str, Any] | None]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, candidate_id
    raise _pending("_current_evaluation_baseline", "B4-04／B4-05（评估基线与评估失败恢复）")


def _evaluation_baseline_has_outputs(campaign_dir: Path, candidate_id: str, baseline: int) -> list[str]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, candidate_id, baseline
    raise _pending("_evaluation_baseline_has_outputs", "B4-04／B4-05（评估基线与评估失败恢复）")


def _evaluation_baseline_states(campaign_dir: Path, candidate_id: str) -> dict[int, dict[str, bool]]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, candidate_id
    raise _pending("_evaluation_baseline_states", "B4-04／B4-05（评估基线与评估失败恢复）")


def _load_evaluation_baseline_recovery(
    campaign_dir: Path, candidate_id: str, baseline: int
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, candidate_id, baseline
    raise _pending("_load_evaluation_baseline_recovery", "B4-04／B4-05（评估基线与评估失败恢复）")


def _authorized_evaluator_digests(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    baseline: int,
) -> dict[str, str]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, manifest, candidate_id, baseline
    raise _pending("_authorized_evaluator_digests", "B4-04／B4-05（评估基线与评估失败恢复）")


def _verify_evaluator_digests_authorized(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    baseline: int,
    digests: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, manifest, candidate_id, baseline, digests, label
    raise _pending("_verify_evaluator_digests_authorized", "B4-04／B4-05（评估基线与评估失败恢复）")


def _plan_evaluator_entry_digests(manifest: Mapping[str, Any]) -> dict[str, str]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del manifest
    raise _pending("_plan_evaluator_entry_digests", "B4-04／B4-05（评估基线与评估失败恢复）")


def _derive_b0_evaluation_run(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    revision: int,
    classification: Mapping[str, Any],
    rules: Sequence[str],
) -> dict[str, Any]:
    """服务面成员（Claude 编排器尚未实现，由 B4-04／B4-05（评估基线与评估失败恢复） 落实）。"""

    del campaign_dir, manifest, candidate_id, revision, classification, rules
    raise _pending("_derive_b0_evaluation_run", "B4-04／B4-05（评估基线与评估失败恢复）")


# ---------------------------------------------------------------------------
# Claude 身份闭包根（不在服务面）：Claude 身份策略登记的 wire／evidence 根与评估读侧根，签名由落实它们的工作项定
# ---------------------------------------------------------------------------


def _load_plan_jobs(
    arguments: argparse.Namespace,
    rules: tuple[str, ...],
) -> tuple[list[Job], dict[str, dict[str, str]]]:
    """wire 根（B2-05）：按目标版本场景清单展开 plan 的全部取证 Job，返回 (Job 列表, 规则与场景三份输入的路径与摘要)。

    Claude 没有基线场景清单（2.1.226 的场景目录是 FW-F v4 probe 目录，已并入目标场景清单）；场景清单的绑定核对、
    Job 闭合与 official 覆盖校验在 ``claude_upgrade_plan.plan_jobs``。证据标签属于 evidence 语义，由 ``_plan_command``
    在本根之外校验，不进 wire 闭包。
    """

    return claude_upgrade_plan.plan_jobs(arguments, rules, context=_job_context(arguments), job_factory=Job)


def _job_context(arguments: argparse.Namespace) -> dict[str, str]:
    """wire 根（B2-05）：场景模板的 Campaign 作用域变量取值；运行期变量（宿主运行凭据与 nonce）由取证动作体逐步绑定。"""

    return claude_upgrade_plan.plan_context(arguments)


def _campaign_arguments(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
    candidate_purpose: str | None = None,
) -> argparse.Namespace:
    """wire 根（B2-06）：把已校验的总计划映射成 Job 展开参数；只读传入的字典，不调用 control 函数。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._campaign_arguments(
        campaign_dir,
        manifest,
        candidate_id=candidate_id,
        runtime_image=runtime_image,
        profile_id=profile_id,
        profile_digest=profile_digest,
        build_id=build_id,
        deployed_version=deployed_version,
        candidate_image_id=candidate_image_id,
        source_tree_sha256=source_tree_sha256,
        candidate_purpose=candidate_purpose,
    )


def _run_job_impl(
    job: Job,
    log_root: Path,
    attempt_index: int = 1,
    scenario_context: Any | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """wire 根（B2-06）：按已展开的 Job 执行一次；只接收 Job 与执行上下文，实现在 ``claude_upgrade_capture``。"""

    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._run_job_impl(
        job,
        log_root,
        attempt_index,
        scenario_context,
        identity=identity,
        tool_identity=tool_identity,
        deadline=deadline,
        heartbeat=heartbeat,
    )


def _scenario_job_execution_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """wire 根（B2-05）：提取场景清单全部 Job 中会改变实际执行或封存判定的字段。"""

    return claude_upgrade_plan.scenario_job_execution_contract(payload)


def scan_evidence(*arguments: Any, **options: Any) -> Any:
    """evidence 根：扫描取证证据（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del arguments, options
    raise _pending("scan_evidence", "B2-07（seal 链幂等与四阶段化）")


def _evidence_inventory(*arguments: Any, **options: Any) -> Any:
    """evidence 根：证据清单（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del arguments, options
    raise _pending("_evidence_inventory", "B2-07（seal 链幂等与四阶段化）")


def _finalize_scenario_receipt(*arguments: Any, **options: Any) -> Any:
    """evidence 根：场景收据定稿（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del arguments, options
    raise _pending("_finalize_scenario_receipt", "B2-07（seal 链幂等与四阶段化）")


def _seal_capture_attempt(*arguments: Any, **options: Any) -> Any:
    """evidence 根：封存取证 attempt（Claude 编排器尚未实现，由 B2-07（seal 链幂等与四阶段化） 落实）。"""

    del arguments, options
    raise _pending("_seal_capture_attempt", "B2-07（seal 链幂等与四阶段化）")


def compare_campaign(*arguments: Any, **options: Any) -> Any:
    """评估读侧根：离线比较（Claude 编排器尚未实现，由 B4-03（Claude 评估器） 落实）。"""

    del arguments, options
    raise _pending("compare_campaign", "B4-03（Claude 评估器）")


def accept_campaign(*arguments: Any, **options: Any) -> Any:
    """评估读侧根：正式验收（Claude 编排器尚未实现，由 B4-06（accept 门禁 Claude 版） 落实）。"""

    del arguments, options
    raise _pending("accept_campaign", "B4-06（accept 门禁 Claude 版）")


# ---------------------------------------------------------------------------
# 命令行（子命令与 Codex 同名）
# ---------------------------------------------------------------------------

# Claude 专属子命令：子命令 → (说明, 落实它的工作项)。动作体落实之前，执行即报 ClaudeOrchestratorPending。
# ``plan`` 已由 B2-05 落实展开与校验（见 ``_plan_command``），``import-predecessor-evidence`` 已由 B2-09 落实（见
# ``_import_predecessor_command``），不在此表。
CLAUDE_OWN_SUBCOMMANDS: Mapping[str, tuple[str, str]] = {
    "capture-official": ("官方取证 attempt：run 或 seal", "B2-06／B2-07（取证 Job 动作体与 seal 链）"),
    "classify": ("VC-2 规则分类", "B3-02（VC-2 分类 producer）"),
    "prepare-profile": ("VC-3 准备画像草案", "B3-03（VC-3 画像 producer）"),
    "stage-profile": ("VC-3 暂存画像", "B3-03（VC-3 画像 producer）"),
    "plan-candidate-gates": ("VC-4 规划候选门禁", "B3-04（VC-4 候选构建与冻结）"),
    "record-candidate-build": ("VC-4 登记候选构建收据", "B3-04（VC-4 候选构建与冻结）"),
    "capture-candidate": ("候选采集 attempt：run 或 seal", "B4-01／B4-02（候选采集与四阶段封存）"),
    "compare": ("只用封存证据离线比较", "B4-03（Claude 评估器）"),
    "accept": ("逐规则正式验收门禁", "B4-06（accept 门禁 Claude 版）"),
    "harden-evidence-permissions": ("证据权限收口", "B2-07（seal 链幂等与四阶段化）"),
    "wire-transition-intent": ("wire 层变化的两阶段过渡：意图", "B2-08（VC-1 恢复接入共享对账）"),
    "wire-transition-final": ("wire 层变化的两阶段过渡：终态", "B2-08（VC-1 恢复接入共享对账）"),
    "reconcile-supervisor-run": ("对账父监督器运行", "B2-08（VC-1 恢复接入共享对账）"),
    "reconcile-attempt": ("对账失败 attempt", "B2-08（VC-1 恢复接入共享对账）"),
    "account-sealed-official": ("官方封存后的请求入账", "B2-08（VC-1 恢复接入共享对账）"),
    "account-sealed-candidate": ("候选封存后的请求入账", "B4-02（候选证据四阶段封存）"),
    "resume": ("按最近稳定状态续跑失败阶段", "B2-08（VC-1 恢复接入共享对账）"),
    "deliver-candidate": ("候选交付四阶段", "B4-07（交付四阶段）"),
    "delivery-stage-invalidate": ("经批准的交付阶段失效", "B4-07（交付四阶段）"),
    "status": ("显示 Campaign 状态和下一命令", "B1-09 续（Campaign 总计划读取与工具身份）"),
}
# 已落实的 Claude 离线工具子命令（不进入阶段动作队列，监督器预声明动作里禁止）：子命令 → 说明。
CLAUDE_OFFLINE_SUBCOMMANDS: Mapping[str, str] = {
    "freeze": "冻结官方制品：npm dist-tag 精确版本、linux/arm64 的 npm 与原生分发双来源（claude_upgrade_freeze）",
}
# 共享流程的子命令：参数定义照搬 Codex，执行在 Claude 适配器作用域里转调 upgrade_control_plane。
SHARED_SUBCOMMANDS = (
    "compile-and-run-vc-batch", "revision-open", "invalidate-candidate", "evaluation-recover", "evaluation-epoch",
    "tool-evolution", "tool-evolution-status", "deadline-extend", "request-budget-extend", "campaign-resume",
    "accounting-resolve", "environment-isolate", "evidence-conflict-quarantine", "campaign-abandon",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_upgrade.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=argparse.ArgumentParser)

    def add_campaign_reference(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--campaign-dir",
            "--campaign",
            dest="campaign_dir",
            type=Path,
            required=True,
        )

    def add_heartbeat_option(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--heartbeat-seconds",
            type=int,
            default=None,
            help=(
                f"watchdog heartbeat 间隔（默认 {DEFAULT_HEARTBEAT_SECONDS} 秒，"
                f"上限 {MAX_HEARTBEAT_SECONDS} 秒）。"
            ),
        )

    def add_vc_batch_compile_arguments(target: argparse.ArgumentParser) -> None:
        add_campaign_reference(target)
        target.add_argument(
            "--phase",
            choices=codex_upgrade_vc_artifacts.VC_PHASES[1:],
            required=True,
        )
        target.add_argument("--sequence", type=int, required=True)
        target.add_argument(
            "--predecessor-checkpoint",
            type=Path,
            required=True,
        )
        target.add_argument(
            "--action-plan",
            type=Path,
            required=True,
            help="codex-upgrade-vc-action-plan/v1 的 execute／reuse 与动作映射。",
        )

    compile_and_run_batch = subparsers.add_parser(
        "compile-and-run-vc-batch",
        help="0.154.0 起 Formal 后继批次的原子编译与父监督器派发入口",
    )
    add_vc_batch_compile_arguments(compile_and_run_batch)
    compile_and_run_batch.add_argument("--state-dir", type=Path, required=True)
    compile_and_run_batch.add_argument(
        "--watchdog-timeout-seconds",
        type=float,
        default=codex_upgrade_supervisor.DEFAULT_WATCHDOG_TIMEOUT_SECONDS,
    )
    compile_and_run_batch.add_argument(
        "--ledger-interval-seconds",
        type=float,
        default=codex_upgrade_supervisor.DEFAULT_LEDGER_INTERVAL_SECONDS,
    )
    # 本入口的唯一墙钟边界来自 Campaign plan 的原始绝对 deadline；只允许
    # 调整父监督器心跳，禁止再暴露第二个相对 ``--max-wall-seconds``。
    add_heartbeat_option(compile_and_run_batch)

    revision_open = subparsers.add_parser(
        "revision-open",
        help=(
            "改造 2：登记并激活一个候选级 revision（零请求、幂等）。--initial 在 VC-4 首批前建立 r1；"
            "--supersedes 在 invalidate-candidate 之后开 r(N+1)"
        ),
    )
    add_campaign_reference(revision_open)
    revision_open.add_argument("--candidate-id", required=True)
    revision_mode = revision_open.add_mutually_exclusive_group(required=True)
    revision_mode.add_argument("--initial", action="store_true", help="新 Campaign 的 r1")
    revision_mode.add_argument("--supersedes", metavar="OLD_CANDIDATE_ID", help="被作废候选的 ID")
    invalidate = subparsers.add_parser(
        "invalidate-candidate",
        help=(
            "改造 2：显式作废当前候选（两步式，零请求）。preview 输出诊断草案与 review_sha256；"
            "apply 以 --approve-sha256 落盘、推送根因并二次判定，通过后账本进入 revision_required"
        ),
    )
    invalidate.add_argument("invalidate_action", choices=("preview", "apply"))
    add_campaign_reference(invalidate)
    invalidate.add_argument("--candidate-id", required=True)
    invalidate.add_argument("--reviewer", required=True, help="人工审核人标识（非空即可）")
    invalidate.add_argument("--evidence", type=Path, action="append", default=[], help="证据文件（可重复），只作 evidence_refs")
    invalidate.add_argument(
        "--candidate-source",
        type=Path,
        help="旧候选源码树：旧候选既无 build receipt 又无 attempt 身份投影时必填",
    )
    invalidate.add_argument("--approve-sha256", help="apply 必需：preview 输出的 review_sha256")
    evaluation_recover_parser = subparsers.add_parser(
        "evaluation-recover",
        help=(
            "改造 5：候选证据封存后离线评估失败的分类与局部恢复（零请求，两步式）。preview 输出"
            "失败来源、复用授权、failure-scope 与诊断草案；apply 以 --approve-sha256 与 --root-cause-class"
            "开评估基线 b<K>（prepared→authorized→committed）；abandon 作废未 COMMIT 的 PREPARED 基线；"
            "reevaluate（第三批 B3-3）：评估器／证据层修好并登记演进后，不依赖失败父 run，对已有评估产出"
            "开 tool-evolution 基线全量重评（不带 --approve-sha256 只预览；VC-5 已完成时重开 VC-5）；"
            "approval-revision（第三批 R5）：批准断言画像的 selector 修正（--assertion-profile 给出修订画像）在原 Campaign 内"
            "开 approval-revision 基线按修订画像全量重评（规则集／validation mode／场景／版本变化仍须从 VC-2 建后继 Campaign）"
        ),
    )
    evaluation_recover_parser.add_argument(
        "recover_action", choices=("preview", "apply", "abandon", "reevaluate", "approval-revision")
    )
    evaluation_recover_parser.add_argument(
        "--assertion-profile", type=Path, help="approval-revision 必需：修订后的断言画像（绝对路径，逐字节封存为新基线的批准画像）"
    )
    add_campaign_reference(evaluation_recover_parser)
    evaluation_recover_parser.add_argument("--candidate-id", required=True)
    evaluation_recover_parser.add_argument("--reviewer", required=True, help="人工审核人标识（非空即可）")
    evaluation_recover_parser.add_argument(
        "--root-cause-class",
        choices=("evaluator-defect", "transient-environment", "candidate-source", "approval-inputs"),
        help="apply 必需：人工裁定的类别（须在 preview 给出的 admissible_classes 内）",
    )
    evaluation_recover_parser.add_argument("--fix-commit", help="evaluator-defect 必需：完整 40 位修复提交")
    evaluation_recover_parser.add_argument(
        "--deployment-receipt", type=Path, help="evaluator-defect 必需：修复已受监督部署的 ARM64 部署收据（绝对路径）"
    )
    evaluation_recover_parser.add_argument("--approve-sha256", help="apply 必需：preview 输出的 review_sha256")
    evaluation_recover_parser.add_argument("--reason", help="abandon 可选：作废原因；approval-revision 可选：修订原因")
    epoch = subparsers.add_parser(
        "evaluation-epoch", help="evidence semantics 变化后为 attempt 追加 evaluation epoch"
    )
    add_campaign_reference(epoch)
    epoch.add_argument("--attempt-id", required=True)
    epoch.add_argument("--reason", required=True)
    epoch.add_argument(
        "--candidate-id",
        help="为 Candidate attempt 追加 epoch（seal／compare／accept 前的 evidence semantics 变化）；缺省为 official attempt。",
    )
    tool_evolution = subparsers.add_parser(
        "tool-evolution",
        help="修好工具并受监督部署后，在原 Campaign 登记工具演进（不带 --approve-sha256 只预览）",
    )
    add_campaign_reference(tool_evolution)
    tool_evolution.add_argument("--fix-commit", required=True, help="本次修复所在提交（40 位）")
    tool_evolution.add_argument("--reason", required=True, help="修复了什么、为什么需要登记")
    tool_evolution.add_argument(
        "--control-root",
        type=Path,
        help="部署收据所在控制根；缺省为 Campaign 数据根下的 control",
    )
    tool_evolution.add_argument("--approve-sha256", help="批准预览的 review_sha256")
    tool_evolution.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")
    tool_evolution.add_argument(
        "--policy-compatibility-receipt",
        type=Path,
        help="策略演进必需：A2.6 policy-compatibility-receipt/v1（旧策略＝Campaign 有效策略、新策略＝当前策略，绝对路径）",
    )
    tool_evolution.add_argument(
        "--policy-activation-certification",
        type=Path,
        help="策略演进必需：当前策略的 policy-activation-certification/v1（绑定同一兼容收据与本次部署收据，绝对路径）",
    )
    tool_evolution_status = subparsers.add_parser(
        "tool-evolution-status", help="只读：Campaign 有效工具身份、演进链与是否需要登记"
    )
    add_campaign_reference(tool_evolution_status)
    deadline_extend = subparsers.add_parser("deadline-extend", help="预算延期：先冻结预览，再按批准摘要写入两本账")
    deadline_extend.add_argument("deadline_action", choices=("preview", "apply"))
    add_campaign_reference(deadline_extend)
    deadline_extend.add_argument("--scope", choices=("project", "campaign", "stage"))
    deadline_extend.add_argument("--phase", choices=codex_upgrade_vc_artifacts.VC_PHASES)
    deadline_extend.add_argument("--new-deadline-at-utc")
    deadline_extend.add_argument("--reason")
    deadline_extend.add_argument("--preview", type=Path)
    deadline_extend.add_argument("--approve-sha256")
    deadline_extend.add_argument("--approved-by")
    budget_extend = subparsers.add_parser(
        "request-budget-extend", help="请求预算延长：先冻结预览，再按批准摘要写入项目总账（修好接着跑第 14 项）"
    )
    budget_extend.add_argument("budget_action", choices=("preview", "apply"))
    add_campaign_reference(budget_extend)
    budget_extend.add_argument("--new-budget", type=int, help="新的有效请求预算（必须大于原有效预算与已消耗数）")
    budget_extend.add_argument("--reason")
    budget_extend.add_argument("--preview", type=Path)
    budget_extend.add_argument("--approve-sha256")
    budget_extend.add_argument("--approved-by")
    campaign_resume = subparsers.add_parser(
        "campaign-resume",
        help="凭修复证据恢复停线的 Campaign（不带 --approve-sha256 只预览）",
    )
    add_campaign_reference(campaign_resume)
    campaign_resume.add_argument("--fix-commit", required=True, help="修复所在提交（40 位）")
    campaign_resume.add_argument("--regression-receipt", required=True, type=Path, help="修复后的离线回归收据（绝对路径）")
    campaign_resume.add_argument("--reason", required=True, help="停线原因已如何消除")
    campaign_resume.add_argument(
        "--control-root",
        type=Path,
        help="部署收据所在控制根；缺省为 Campaign 数据根下的 control",
    )
    campaign_resume.add_argument("--approve-sha256", help="批准预览的 review_sha256")
    campaign_resume.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")
    accounting_resolve = subparsers.add_parser(
        "accounting-resolve",
        help="为账务无法核清的 operation 带证据补账（不带 --approve-sha256 只预览）",
    )
    add_campaign_reference(accounting_resolve)
    accounting_resolve.add_argument("--operation-id", required=True, help="总账未决集合里本 Campaign 的 operation")
    accounting_resolve.add_argument(
        "--estimated-count", type=int, help="仍无法核清的未决作业请求数批准上界（按当前证据已能核清时不提供）"
    )
    accounting_resolve.add_argument(
        "--evidence", type=Path, help="补账依据的来源审计文件（绝对路径；给出上界时必须提供）"
    )
    accounting_resolve.add_argument("--reason", required=True)
    accounting_resolve.add_argument("--approve-sha256", help="批准预览的 review_sha256")
    accounting_resolve.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")
    environment_isolate = subparsers.add_parser(
        "environment-isolate",
        help="修复环境后隔离污染 attempt，Campaign 继续（不带 --approve-sha256 只预览）",
    )
    add_campaign_reference(environment_isolate)
    environment_isolate.add_argument(
        "--environment-repair-receipt", required=True, type=Path, help="环境修复记录（绝对路径）"
    )
    environment_isolate.add_argument(
        "--clean-environment-receipt",
        required=True,
        type=Path,
        help="修复后新取、晚于污染的干净环境复核收据（环境探针清单或 ARM64 环境收据，绝对路径）",
    )
    environment_isolate.add_argument("--reason", required=True)
    environment_isolate.add_argument("--approve-sha256", help="批准预览的 review_sha256")
    environment_isolate.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")
    evidence_conflict = subparsers.add_parser(
        "evidence-conflict-quarantine",
        help="续跑重跑来源已完成作业后证据根冲突：隔离卷入冲突的 attempt，Campaign 继续（不带 --approve-sha256 只预览）",
    )
    add_campaign_reference(evidence_conflict)
    evidence_conflict.add_argument("--candidate-id", help="候选阶段的 candidate-id；缺省为 official 阶段")
    evidence_conflict.add_argument("--reason", required=True)
    evidence_conflict.add_argument("--approve-sha256", help="批准预览的 review_sha256")
    evidence_conflict.add_argument("--approved-by", help="批准人（与 --approve-sha256 一起提供）")

    campaign_abandon = subparsers.add_parser("campaign-abandon", help="按明确批准显式放弃 Campaign，保留全部历史证据")
    add_campaign_reference(campaign_abandon)
    campaign_abandon.add_argument("--approved-by", required=True)
    campaign_abandon.add_argument("--reason", required=True)

    plan = subparsers.add_parser(
        "plan",
        help=(
            "B2-05：按目标场景清单展开并校验全部取证 Job（preflight_only 零写入、输出展开结果）；"
            "建 Campaign 与 formal 由 B2-11（VC-0 收口）落实"
        ),
    )
    claude_upgrade_plan.add_plan_arguments(plan)

    import_predecessor = subparsers.add_parser(
        "import-predecessor-evidence",
        help=(
            "B2-09：把前序 Store 的 VC-1 官方证据导入本 Campaign 的受管 Store（read_only_reuse 授权 VC-1 零请求复用，"
            "只用于总计划冻结的前序；regression_fixture 只登记回归夹具，不授予正式引用权限）"
        ),
    )
    claude_upgrade_predecessor.add_import_arguments(import_predecessor)

    freeze = subparsers.add_parser("freeze", help=CLAUDE_OFFLINE_SUBCOMMANDS["freeze"])
    freeze.add_argument("--output-root", type=Path, required=True, help="冻结输出目录（绝对路径，必须不存在）")
    freeze.add_argument("--dist-tag", default="stable", help="npm 主包的 dist-tag，默认 stable")
    freeze.add_argument("--platform", default="linux/arm64", help="冻结平台（目前只登记 linux/arm64）")

    for name, (summary, work_item) in CLAUDE_OWN_SUBCOMMANDS.items():
        target = subparsers.add_parser(name, help=f"{summary}（由 {work_item} 落实）")
        add_campaign_reference(target)
        if name in {"capture-official", "capture-candidate"}:
            target.add_argument("capture_action", choices=("run", "seal"))
        if name == "capture-official":
            # run 的参数（B2-06）：真实请求确认、零请求演练与 deadline；seal 的参数由 B2-07 增加。
            from tools.official_client_capture import claude_upgrade_capture

            claude_upgrade_capture.add_capture_official_run_arguments(target)
        if name in {"capture-candidate", "compare", "accept", "plan-candidate-gates", "record-candidate-build"}:
            target.add_argument("--candidate-id")
    return parser


def _plan_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """``plan``（B2-05）：校验参数，按目标场景清单展开全部取证 Job 并核对规则、标签与覆盖，输出零写入报告。

    建 Campaign（计时账本、环境收据、控制收据与总计划落盘）归 B2-11；Formal Campaign 只由 VC-0 收口创建，
    这里直接拒绝 ``--campaign-mode formal``。
    """

    if arguments.campaign_mode == "formal":
        raise _pending("plan --campaign-mode formal（Formal Campaign 只由 VC-0 收口创建）", "B2-11（VC-0 收口续作与 P0 收据）")
    claude_upgrade_plan.prepare_plan_arguments(arguments)
    rules = claude_upgrade_plan.load_baseline_rules(arguments.rule_manifest, arguments.baseline_version)
    claude_upgrade_plan.verify_target_rules(arguments, rules)
    jobs, inputs = _load_plan_jobs(arguments, rules)
    # 证据标签属于 evidence 语义：在 wire 根之外校验并绑定，标签校验代码的变化不改变 wire 身份。
    labels = claude_upgrade_plan.evidence_labels_binding(arguments, rules)
    return claude_upgrade_plan.plan_report(arguments, jobs, rules, {**inputs, "evidence_labels": labels})


def _import_predecessor_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """``import-predecessor-evidence``（B2-09）：Campaign 锁内、没有未释放的执行租约时导入；实现在 ``claude_upgrade_predecessor``。"""

    campaign_dir = Path(arguments.campaign_dir)
    load_campaign_manifest(campaign_dir)
    with _campaign_lock(campaign_dir):
        lease = _read_campaign_lease(campaign_dir)
        if lease is not None and lease.get("state") != "released":
            raise ConfigurationError("Campaign 有未释放的执行租约，前序证据导入要求 Campaign 静止：先按恢复协议对账。")
        manifest = load_campaign_manifest(campaign_dir)
        return claude_upgrade_predecessor.import_predecessor_evidence(
            campaign_dir,
            manifest,
            predecessor_store=Path(arguments.predecessor_store),
            predecessor_campaign=str(arguments.predecessor_campaign),
            mode=str(arguments.mode),
            approved_by=str(arguments.approved_by),
        )


def _run_shared_subcommand(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """共享流程的子命令：在 Claude 适配器作用域里转调共享实现；退出码口径与 Codex 相同。"""

    command = str(arguments.command)
    with upgrade_control_plane.adapter_scope(upgrade_client_adapter.adapter_for("claude")):
        if command == "compile-and-run-vc-batch":
            return upgrade_control_plane.compile_and_run_vc_batch(arguments)
        if command == "revision-open":
            return upgrade_control_plane.open_candidate_revision(arguments), 0
        if command == "invalidate-candidate":
            result = upgrade_control_plane.invalidate_candidate(arguments)
            return result, 0 if result.get("status") in {"preview", "applied"} else 3
        if command == "evaluation-recover":
            result = upgrade_control_plane.evaluation_recover(arguments)
            return result, 0 if result.get("status") in {"preview", "applied", "abandoned", "redirect"} else 3
        if command == "evaluation-epoch":
            return upgrade_control_plane._evaluation_epoch_command(arguments), 0
        if command == "tool-evolution":
            return upgrade_control_plane._tool_evolution_command(arguments), 0
        if command == "tool-evolution-status":
            return upgrade_control_plane._tool_evolution_status_command(arguments), 0
        if command == "deadline-extend":
            return upgrade_control_plane._deadline_extend_command(arguments), 0
        if command == "request-budget-extend":
            return upgrade_control_plane._request_budget_extend_command(arguments), 0
        if command == "campaign-resume":
            return upgrade_control_plane._campaign_resume_command(arguments), 0
        if command == "accounting-resolve":
            return upgrade_control_plane._accounting_resolve_command(arguments), 0
        if command == "environment-isolate":
            return upgrade_control_plane._environment_isolate_command(arguments), 0
        if command == "evidence-conflict-quarantine":
            return upgrade_control_plane._evidence_conflict_quarantine_command(arguments), 0
        if command == "campaign-abandon":
            result = codex_upgrade_project_ledger.abandon_campaign(
                arguments.campaign_dir, approved_by=arguments.approved_by, reason=arguments.reason
            )
            return result, 0
    raise ConfigurationError(f"不是共享流程的子命令：{command}")


def main(argv: list[str] | None = None) -> int:
    """Claude 编排器入口：共享子命令转调共享实现，freeze 转调官方制品冻结，plan 展开取证 Job（B2-05），
    import-predecessor-evidence 导入前序证据（B2-09），capture-official run 执行官方取证 attempt（B2-06），其余 Claude
    专属子命令在动作体落实前报未实现。

    租约、遗留入口拒绝与停线收据等 Codex 主入口的外层流程，随租约与锁（B1-09 续）一起落实。
    """

    arguments = _build_parser().parse_args(argv)
    if arguments.command == "freeze":
        from tools.official_client_capture import claude_upgrade_freeze

        try:
            summary = claude_upgrade_freeze.run(arguments)
        except claude_upgrade_freeze.FreezeError as error:
            print(f"官方制品冻结失败：{error}", file=sys.stderr)
            return 1
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        # 两渠道二进制不一致时冻结照常落盘，但退出码 2 提示 VC-0 不得据此建 Formal Campaign。
        return 0 if summary["binary_equivalence"] == "npm=gcs" else 2
    try:
        if arguments.command in SHARED_SUBCOMMANDS:
            result, return_code = _run_shared_subcommand(arguments)
        elif arguments.command == "plan":
            result, return_code = _plan_command(arguments), 0
        elif arguments.command == "import-predecessor-evidence":
            result, return_code = _import_predecessor_command(arguments), 0
        elif arguments.command == "capture-official" and arguments.capture_action == "run":
            from tools.official_client_capture import claude_upgrade_capture

            result, return_code = claude_upgrade_capture.capture_official_run_command(arguments)
        else:
            summary, work_item = CLAUDE_OWN_SUBCOMMANDS[str(arguments.command)]
            raise _pending(f"子命令 {arguments.command}（{summary}）", work_item)
    except ConfigurationError as error:
        print(f"Claude 升级失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
