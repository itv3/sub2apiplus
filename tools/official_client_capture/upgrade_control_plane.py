"""Codex 与 Claude 升级编排器共用的控制流程（Claude 升级控制面设计 B1-09）。

从 ``codex_upgrade.py`` 迁入的共享流程：admission 与写入守卫、批次编译与 staging、恢复预览与恢复段、评估基线、
候选 revision、tool-evolution 与隔离、对账与入账命令壳。只迁不在任何 Codex 身份闭包内的函数；闭包内的函数、
含 Codex 专属字面量或被源码钉住的函数留在编排器原位（附录 E）。

调用约定：本模块函数只能在 ``adapter_scope(adapter)`` 作用域内执行，由各编排器原位的同名薄壳设定（Codex 薄壳传
``adapter_for("codex")``）。函数对编排器函数、类、共用常量的引用一律写成字面形式 ``adapter.orchestrator.<原名>``
（``adapter = _adapter()``），运行时按客户端绑定现取，Codex 测试对编排器符号的替身照常生效；客户端专属的取值经
适配器成员（``evaluation_builder_basename``、``orchestrator_basenames``）取得。本模块不按客户端分支。
"""

from __future__ import annotations

import argparse
import contextvars
import glob
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable, Iterator, Sequence
from tools.official_client_capture.capturelib.security import ensure_private_directory
from tools.official_client_capture import codex_upgrade_evidence_permissions
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_root_cause
from tools.official_client_capture import codex_upgrade_wire_transition
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import incremental_recovery

# 当前客户端适配器：由编排器薄壳经 adapter_scope 设定；同一调用链只能属于一个客户端。
_ADAPTER: contextvars.ContextVar[Any] = contextvars.ContextVar("upgrade_control_plane_adapter", default=None)


def _adapter() -> Any:
    """当前作用域的客户端适配器；不在任何编排器薄壳之内调用即拒绝。"""

    adapter = _ADAPTER.get()
    if adapter is None:
        raise RuntimeError("共享控制流程必须经编排器薄壳调用：当前没有客户端适配器作用域")
    return adapter


@contextmanager
def adapter_scope(adapter: Any) -> Iterator[Any]:
    """设定当前客户端适配器；嵌套作用域只能是同一客户端，防止一个客户端的流程误入另一个客户端的编排器。

    同一作用域同时设定对账器的客户端（Claude 升级 B3-06）：本模块直接调用对账器的内部函数（staging 中止根因、总账推送、
    判定、身份事实等），它们按对账器自己的作用域取适配器，不经对账器公开入口时缺省是 Codex——Claude 的 staging 中止收据会
    按 Codex 码表算根因，对账时又按 Claude 重算，两边对不上而失败关闭。对账器作用域已由公开入口设为另一客户端时同样拒绝。
    Codex 设定的就是对账器的缺省值，行为不变。
    """

    outer = _ADAPTER.get()
    if outer is not None and outer.client != adapter.client:
        raise RuntimeError(f"共享控制流程的客户端作用域冲突：{outer.client} 内进入 {adapter.client}")
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    reconciler_outer = reconciler._ADAPTER.get()
    if reconciler_outer is not None and reconciler_outer.client != adapter.client:
        raise RuntimeError(f"对账器的客户端作用域冲突：{reconciler_outer.client} 内进入 {adapter.client}")
    token = _ADAPTER.set(adapter)
    reconciler_token = reconciler._ADAPTER.set(adapter)
    try:
        yield adapter
    finally:
        reconciler._ADAPTER.reset(reconciler_token)
        _ADAPTER.reset(token)


CAPTURE_RESERVATION_ADMISSION_SECONDS = 30
STAGING_BATCH_FILENAME = "batch.json"
STAGING_MANIFEST_FILENAME = "run-manifest.json"
STAGING_ABORT_FILENAME = "ABORT"
_STAGING_ATTEMPT_RE = re.compile(r"^attempt-([1-9][0-9]*)$")
# R18：VC-4 两个零请求动作的重派参数闭集（必需参数，可选的 watchdog 参数）。
CANDIDATE_STAGE_REPLAY_FLAGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "plan-candidate-gates": (
        frozenset({"--campaign-dir", "--candidate-id", "--candidate-source", "--mapping", "--output"}),
        frozenset({"--max-wall-seconds", "--heartbeat-seconds"}),
    ),
    "record-candidate-build": (
        frozenset({
            "--campaign-dir", "--candidate-id", "--candidate-purpose", "--candidate-source", "--candidate-binary",
            "--runtime-image", "--candidate-image-id", "--build-id", "--deployed-version", "--target-architecture",
            "--build-parameters", "--build-tree", "--docker-context", "--frontend-dist-source", "--catalog-stage-dir",
            "--source-transition", "--gate-plan", "--implementation-test-root", "--implementation-test-receipt",
        }),
        frozenset({"--max-wall-seconds", "--heartbeat-seconds"}),
    ),
}
REVISION_OPEN_COMMAND = "revision-open"
INVALIDATE_CANDIDATE_COMMAND = "invalidate-candidate"
EVALUATION_RECOVER_COMMAND = "evaluation-recover"
EVALUATION_BASELINE_DIAGNOSIS_FILENAME = "diagnosis.json"
# 第三批 B3-3：tool-evolution 基线的重评触发事实（作为 recovery.diagnosis 绑定，与 diagnosis.json 同目录）。
EVALUATION_REEVALUATION_FILENAME = "reevaluation.json"
EVALUATION_ACTION_KINDS_BY_SOURCE = {
    "assertion": "assertion-failed",
    "compare": "offline-compare-failed",
    "accept": "offline-accept-failed",
}
# 各失败来源"缺陷所在项"：evaluator-defect 准入要求变化项覆盖之。
EVALUATION_DEFECT_ITEMS = {
    "assertion-failed": ("checker_sha256", "builder_sha256"),
    "offline-compare-failed": ("compare_reader_sha256",),
    "offline-accept-failed": ("accept_reader_sha256",),
}
APPROVAL_REVISION_PROFILE_FILENAME = "assertion-profile.json"
ENVIRONMENT_ISOLATION_PREVIEW_SCHEMA = "environment-isolation-preview/v1"
# 干净环境复核收据的时间字段（环境探针清单、ARM64 环境收据等），取第一个存在的字段。
CLEAN_ENVIRONMENT_TIME_FIELDS = (
    "observed_at_utc",
    "generated_at_utc",
    "created_at_utc",
    "recorded_at_utc",
    "certified_at_utc",
)
EVIDENCE_CONFLICT_QUARANTINE_PREVIEW_SCHEMA = "evidence-conflict-quarantine-preview/v1"
# 登记前的静默判据：未写 attempt.json 的 attempt 在该时长内仍有看门狗心跳，视为可能仍在运行。
TOOL_EVOLUTION_QUIESCENCE_SECONDS = 900
# ---------------------------------------------------------------------------
# campaign-resume：凭修复证据恢复停线的 Campaign（修好接着跑第 10 项后半、第 11 项）
# ---------------------------------------------------------------------------
#
# 停线（计时账本 stopped／同根因达上限 stop_required）与总账可恢复终态此前基本不可恢复：recovery_verified
# 只覆盖"同根因两次且无编排入口"，总账 campaign_terminal 不可撤销，只能新建 Campaign。现在：绑定修复提交、
# 离线回归收据与晚于停线的部署收据，经预览→批准后，先写计时账本的恢复事件（清零达上限根因、恢复停线前
# 状态或以原起点重开被放弃的阶段为 recovery_required），再写总账 campaign_resumed 撤销终态；之后统一走
# "对账 → 恢复预览批准 → 续跑"。不可恢复：完整性异常、人工放弃／取代、已完成、显式关账本。
CAMPAIGN_RESUME_PREVIEW_SCHEMA = "campaign-resume-preview/v1"
CAMPAIGN_RESUME_DIR = Path("control") / "resume"
# 显式关账本（人工决定）写的停线不可恢复。
_NON_RESUMABLE_STOP_EVENT_PREFIXES = ("close-stop-the-line",)
# ---------------------------------------------------------------------------
# accounting-resolve：为账务无法核清的 operation 带证据补账（修好接着跑第 12 项）
# ---------------------------------------------------------------------------
#
# 此前请求来源无法核清（例如被打断的作业没有 Job 收据，只剩日志）即判 accounting_unresolved 终态，总账 blocked
# 让所有 Campaign 的对账都终止，也没有生产上的补账入口。现在对账把它判为暂停，由本命令按当前证据复算后补账：
#   · 原未决作业按当前证据仍无法核清：登记它们的特征摘要，按批准的请求数上界（--estimated-count，附 --evidence
#     来源审计）入账；之后对账遇到同一批未决事实不再重复判为未决（新证据出现时特征变化，照常重新核算）；
#   · 原未决作业按当前证据已能核清（例如补回了中继日志）：按与对账同一口径（reconciler.request_accounting）
#     把未入账的精确身份键与估计来源一并入账，不接受 --estimated-count。
# 两种情况都写 accounting_resolved 解除该 operation 的 blocked；同一批准重跑按收据冻结的预览幂等续接。
ACCOUNTING_RESOLUTION_PREVIEW_SCHEMA = "accounting-resolution-preview/v1"
ACCOUNTING_RESOLUTION_SCHEMA = "accounting-resolution/v1"
ACCOUNTING_RESOLUTION_DIR = Path("control") / "accounting"


def _mutable_command_coordinates(
    arguments: argparse.Namespace,
) -> tuple[str, str, str | None, bool] | None:
    """返回 ``(command, phase, candidate_id, allow_stale_recovery)``。"""

    adapter = _adapter()
    command = str(getattr(arguments, "command", ""))
    if command in {
        "status",
        "plan",
        adapter.orchestrator.FORMAL_FAILED_JOB_RECOVERY_COMMAND,
        adapter.orchestrator.FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND,
        "terminal-transition-preflight",
        # 只读裁定；两步式权限收口自持 Campaign 排他锁（.campaign.lock），
        # 不能再建 CampaignLease，否则同一进程内会与自身的锁互相等待。
        "verdict-official-attempt-identity",
        "harden-evidence-permissions",
        # B0 两个 reconciler 自持 Campaign 排他锁与账本目录锁，不建 CampaignLease。
        "reconcile-supervisor-run",
        "reconcile-attempt",
        # 改造 2：revision 登记与候选作废同样自持 Campaign 排他锁与账本锁。
        "revision-open",
        "invalidate-candidate",
        # 改造 5：评估失败的分类与评估基线状态机同样自持 Campaign 排他锁与账本锁。
        "evaluation-recover",
        "deadline-extend",
        # 修好接着跑第 14 项：请求预算延长与延期同类，自持预算控制锁与项目锁。
        "request-budget-extend",
        "campaign-abandon",
        # 工具演进登记在 Campaign 排他锁内核对静默并写一次演进收据；状态查询只读。
        "tool-evolution",
        "tool-evolution-status",
        # campaign-resume 与延期同类：自持预算控制锁、项目锁与 Campaign 锁，核对静默后写两本账。
        "campaign-resume",
        # 补账自持 Campaign 锁与总账锁，写一次批准收据与 outbox batch 后推送。
        "accounting-resolve",
        # 环境隔离自持 Campaign 锁，只写 control/environment 下的收据副本与一次性隔离收据。
        "environment-isolate",
        # 证据根冲突隔离（第 24 项）自持 Campaign 锁，只写 control/evidence-conflict 下的一次性隔离收据。
        "evidence-conflict-quarantine",
        # R6：零请求导入／续作自持项目 admission 锁及 Campaign 锁，不启动执行监督器。
        "reuse-official-evidence",
        # seal 预演在私有 mount namespace 的 OverlayFS 副本上执行，正式目录只读；
        # 收据写入 control/seal-rehearsal，不触碰 attempt 与账本。
        "rehearse-candidate-seal",
        "",
    }:
        return None
    if command in {"wire-transition-intent", "wire-transition-final", "evaluation-epoch"}:
        # A2 控制面写入：借 official 侧 CampaignLease 防止与其他 Campaign 写操作并发。
        return command, "official", None, False
    if command in {"capture-official"}:
        return command, "official", None, False
    if command == "compile-and-run-vc-batch":
        # 原子入口从编译开始到父 run 终态始终持有同一个 state-dir
        # ``.campaign-run.lock``，父 campaign-run 继承 Campaign 原始绝对
        # deadline。这里不得再创建 CampaignLease，否则会多出第二个监督器，
        # 并由默认 ``--max-wall-seconds`` 产生一条与原 deadline 竞争的边界。
        return None
    if command in {
        "compile-vc-batch",
        "compile-vc-interrupted-recovery-batch",
    }:
        # batch 编译是两个 campaign-run 之间的控制面写入；它必须直接运行，
        # 但仍借 official 侧 CampaignLease 防止与其他 Campaign 写操作并发。
        return command, "official", None, False
    if command == "recover-vc1-interruption":
        return command, "official", None, True
    if command in {
        "plan-candidate-gates",
        "record-candidate-build",
        "capture-candidate",
        "deliver-candidate",
        "account-sealed-candidate",
        "all",
    }:
        # 改造 5 M2：恢复段后继段（capture-candidate run --attempt-recovery ar<k+1> --rerun-failed）接管
        # 中断段留下的过期 lease；其它候选级命令保持不接管。
        allow_stale = (
            command == "capture-candidate"
            and bool(getattr(arguments, "rerun_failed", False))
            and getattr(arguments, "capture_action", None) == "run"
            and bool(getattr(arguments, "attempt_recovery", None))
        )
        return command, "candidate", str(arguments.candidate_id), allow_stale
    if command in {"evaluation-transition", "control-epoch"}:
        phase = str(arguments.phase)
        return command, phase, (
            str(arguments.candidate_id) if phase == "candidate" else None
        ), command == "control-epoch"
    if command in {"compare", "accept", "candidate-runtime-override"}:
        return command, "candidate", str(arguments.candidate_id), False
    if command in {"canonical-import", "canonical-advance"}:
        return command, "candidate", str(arguments.candidate_id), True
    if command == "deep-verify":
        candidate_id = getattr(arguments, "candidate_id", None)
        return command, "candidate" if candidate_id else "official", candidate_id, False
    if command == "resume":
        candidate_id = getattr(arguments, "candidate_id", None)
        # resume 自身先由 status 解析当前阶段；显式 candidate-id 优先。
        if candidate_id:
            phase = "candidate"
        else:
            state = adapter.orchestrator.campaign_status(arguments.campaign_dir).get("status")
            phase = (
                "candidate"
                if state in {
                    "candidate_selection_required",
                    "candidate_capture_failed",
                    "candidate_capture_interrupted",
                    "candidate_awaiting_client_checkpoint",
                    "candidate_client_checkpoint_created",
                    "candidate_awaiting_seal_approval",
                    "candidate_sealed",
                    "compared",
                    "ready",
                    "profile_approved",
                }
                else "official"
            )
        return command, phase, candidate_id, bool(getattr(arguments, "rerun_failed", False))
    if command in {
        "classify",
        "prepare-profile",
        "stage-profile",
        "successor",
        adapter.orchestrator.FORMAL_FAILED_JOB_RECOVERY_COMMAND,
        adapter.orchestrator.FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND,
        "account-sealed-official",
    }:
        return command, "official", None, False
    raise adapter.orchestrator.ConfigurationError(f"不受支持的命令：{command}")


@contextmanager
def _main_command_lease(
    arguments: argparse.Namespace,
) -> Iterable[CampaignLease | None]:
    """主 CLI 的统一 lease 入口；只读、引导和原子派发不另建 lease。"""

    adapter = _adapter()
    coordinates = adapter.orchestrator._mutable_command_coordinates(arguments)
    if coordinates is None:
        yield None
        return
    command, phase, candidate_id, allow_stale_recovery = coordinates
    campaign_dir = arguments.campaign_dir
    # 非法输入（例如单元测试中被 mock 的不存在 Campaign）交给原函数做
    # 正式路径校验；合法的 Formal 目录必须经过 lease，不能用此分支绕过。
    if not isinstance(campaign_dir, Path) or not campaign_dir.is_dir():
        yield None
        return
    campaign_id: str | None = None
    if command in {"canonical-import", "canonical-advance"}:
        manifest = adapter.orchestrator._read_json(campaign_dir / "campaign.json", "Campaign 清单")
        raw_campaign_id = manifest.get("campaign_id")
        if (
            manifest.get("campaign_mode") != "formal"
            or not isinstance(raw_campaign_id, str)
            or not adapter.orchestrator.SAFE_ID_RE.fullmatch(raw_campaign_id)
        ):
            raise adapter.orchestrator.ConfigurationError("canonical 命令的 Campaign 身份非法。")
        campaign_id = raw_campaign_id
    deadline = adapter.orchestrator._attempt_deadline(arguments, phase)
    with adapter.orchestrator._campaign_operation_lease(
        campaign_dir,
        phase=phase,
        candidate_id=candidate_id,
        command=command,
        deadline=deadline,
        allow_stale_recovery=allow_stale_recovery,
        campaign_id=campaign_id,
    ) as lease:
        yield lease


def _require_capture_budget_before_data_action(
    deadline: incremental_recovery.WallClockDeadline,
    *,
    operation: str,
    reservation: bool = False,
) -> None:
    """在 reservation／Job 前保留完整 after 与终态清理预算。"""

    adapter = _adapter()
    reserve = adapter.orchestrator._campaign_cleanup_reserve_seconds()
    if reserve <= 0:
        deadline.check(operation)
        return
    required = reserve + (
        CAPTURE_RESERVATION_ADMISSION_SECONDS if reservation else 0
    )
    if deadline.remaining_seconds <= required:
        raise incremental_recovery.WallClockTimeoutError(
            operation,
            elapsed_seconds=deadline.elapsed_seconds,
            budget_seconds=deadline.budget_seconds,
        )
    execution_deadline = os.environ.get(
        codex_upgrade_supervisor.CAMPAIGN_RUN_EXECUTION_DEADLINE_ENV
    )
    if execution_deadline is None:
        raise adapter.orchestrator.ConfigurationError("campaign-run 缺少冻结的数据面执行截止。")
    try:
        remaining_execution = float(execution_deadline) - time.time()
    except ValueError as error:
        raise adapter.orchestrator.ConfigurationError("campaign-run 数据面执行截止非法。") from error
    minimum = CAPTURE_RESERVATION_ADMISSION_SECONDS if reservation else 0
    if not math.isfinite(remaining_execution) or remaining_execution <= minimum:
        raise incremental_recovery.WallClockTimeoutError(
            operation,
            elapsed_seconds=deadline.elapsed_seconds,
            budget_seconds=deadline.budget_seconds,
        )


def _wire_closure_sha256(identity: Mapping[str, Any]) -> str | None:
    """取工具身份里编排器 wire 闭包摘要；v1 身份没有该字段返回 None。"""

    closures = identity.get("orchestrator_closures")
    if not isinstance(closures, Mapping):
        return None
    wire = closures.get("wire_producer")
    value = wire.get("closure_sha256") if isinstance(wire, Mapping) else None
    return value if isinstance(value, str) else None


def _tool_path_job_map(
    jobs: Iterable[Job],
    expected_tool: Mapping[str, Any],
    current_tool: Mapping[str, Any],
) -> dict[str, set[str]]:
    """建立工具路径到计划 Job 的精确反向依赖表。"""

    adapter = _adapter()
    planned = list(jobs)
    planned_ids = {job.job_id for job in planned}
    impact_map: dict[str, set[str]] = {}
    for job in planned:
        current_files = set(adapter.orchestrator._job_tool_dependency_files(job, current_tool))
        expected_files = set(adapter.orchestrator._job_tool_dependency_files(job, expected_tool))
        for path in current_files | expected_files:
            impact_map.setdefault(path, set()).add(job.job_id)
    for path, job_ids in adapter.orchestrator.RUNTIME_SUCCESSOR_CHANGED_TOOL_PATH_JOB_IDS.items():
        impact_map.setdefault(path, set()).update(
            job_id
            for job_id in job_ids
            if job_id in planned_ids
        )
    for path in adapter.orchestrator.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES:
        impact_map.setdefault(path, set()).update(
            job_id
            for job_id in adapter.orchestrator.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS
            if job_id in planned_ids
        )
    return impact_map


def _implicit_r1_candidate_id(campaign_dir: Path, manifest: Mapping[str, Any]) -> str:
    """历史隐含 r1 的候选：由原路径 VC-4 checkpoint 绑定的构建收据给出。"""

    adapter = _adapter()
    plan = adapter.orchestrator._vc_campaign_plan(campaign_dir, manifest)
    _path, checkpoint = adapter.orchestrator._replay_vc_checkpoint(campaign_dir, plan, "VC-4", revision=1)
    binding = checkpoint.get("stage_receipt")
    if not isinstance(binding, Mapping):
        raise adapter.orchestrator.ConfigurationError("VC-4 checkpoint 缺少构建收据绑定。")
    raw = Path(str(binding.get("path", "")))
    receipt_path = raw if raw.is_absolute() else adapter.orchestrator._campaign_file(campaign_dir, str(binding.get("path", "")))
    receipt = adapter.orchestrator._read_json(receipt_path, "VC-4 构建收据")
    candidate_id = receipt.get("candidate_id")
    if not isinstance(candidate_id, str) or not adapter.orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise adapter.orchestrator.ConfigurationError("VC-4 构建收据缺少合法 candidate_id。")
    return candidate_id


def _campaign_batch_model(campaign_dir: Path) -> str:
    """派发入口的模型判定：委托监督器同一函数，无 Campaign／无总计划即 legacy。"""

    adapter = _adapter()
    try:
        return codex_upgrade_supervisor.campaign_batch_model(campaign_dir)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error


def _campaign_uses_staging_model(campaign_dir: Path, plan: Mapping[str, Any]) -> bool:
    """以总计划 ``batch_model`` 为主判据；legacy Campaign 出现 staging 制品即失败关闭。"""

    adapter = _adapter()
    try:
        model = codex_upgrade_vc_artifacts.campaign_plan_batch_model(plan)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    vc_root = campaign_dir / "control" / "vc"
    if model == "legacy":
        for name in ("staging", "commits", "staging-aborts"):
            path = vc_root / name
            if path.exists() or path.is_symlink():
                raise adapter.orchestrator.ConfigurationError(
                    f"legacy 批次模型的 Campaign 不得混用 staging 制品：control/vc/{name}"
                )
        return False
    return True


def _staging_sequence_dir(campaign_dir: Path, sequence: int, phase: str) -> Path:
    return campaign_dir / "control" / "vc" / "staging" / f"{sequence:04d}-{phase.lower()}"


def _staging_commit_path(campaign_dir: Path, sequence: int, phase: str) -> Path:
    return codex_upgrade_supervisor._staging_commit_path(campaign_dir, sequence, phase)


def _staging_attempt_dirs(sequence_dir: Path) -> list[tuple[int, Path]]:
    """按 attempt 序号升序枚举一个序号目录下的 staging attempt。"""

    adapter = _adapter()
    if sequence_dir.is_symlink():
        raise adapter.orchestrator.ConfigurationError(f"staging 序号目录不得是符号链接：{sequence_dir}")
    if not sequence_dir.is_dir():
        return []
    attempts: list[tuple[int, Path]] = []
    for child in sequence_dir.iterdir():
        match = _STAGING_ATTEMPT_RE.fullmatch(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            raise adapter.orchestrator.ConfigurationError(f"staging 序号目录含非法条目：{child}")
        attempts.append((int(match.group(1)), child))
    return sorted(attempts)


def _next_staging_attempt(sequence_dir: Path) -> int:
    adapter = _adapter()
    attempts = adapter.orchestrator._staging_attempt_dirs(sequence_dir)
    return (attempts[-1][0] + 1) if attempts else 1


def _read_staging_commit(commit_path: Path) -> dict[str, Any]:
    adapter = _adapter()
    try:
        return codex_upgrade_supervisor._read_vc_commit(commit_path)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(f"COMMIT 无法校验：{commit_path.name}：{error}") from error


def _committed_vc_sequences(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[int]:
    """staging 模型的序号占用：序号 1 由 VC-0 在 campaign.json 内绑定，其余只认 COMMIT。"""

    adapter = _adapter()
    committed: list[int] = []
    control = manifest.get("vc_control")
    first_binding = control.get("first_formal_batch") if isinstance(control, Mapping) else None
    if isinstance(first_binding, Mapping) and isinstance(first_binding.get("path"), str):
        first_path = campaign_dir / str(first_binding["path"])
        if first_path.is_symlink() or not first_path.is_file():
            raise adapter.orchestrator.ConfigurationError("VC-0 绑定的首批 batch 文件不存在或不可信。")
        if adapter.orchestrator.file_sha256(first_path) != first_binding.get("sha256"):
            raise adapter.orchestrator.ConfigurationError("VC-0 绑定的首批 batch 摘要漂移。")
        committed.append(1)
    commits_root = campaign_dir / "control" / "vc" / "commits"
    if commits_root.is_symlink():
        raise adapter.orchestrator.ConfigurationError("COMMIT 目录不得是符号链接。")
    if commits_root.is_dir():
        for path in sorted(commits_root.iterdir()):
            match = adapter.orchestrator._VC_SEQUENCE_FILE_RE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise adapter.orchestrator.ConfigurationError(f"COMMIT 目录含非法条目：{path.name}")
            commit = adapter.orchestrator._read_staging_commit(path)
            sequence = int(match.group(1))
            if (
                commit["sequence"] != sequence
                or commit["phase"].lower() != match.group(2)
                or commit["campaign_id"] != manifest.get("campaign_id")
            ):
                raise adapter.orchestrator.ConfigurationError(f"COMMIT 内容与文件名或 Campaign 不一致：{path.name}")
            committed.append(sequence)
    if len(committed) != len(set(committed)):
        raise adapter.orchestrator.ConfigurationError("COMMIT 序号重复。")
    return sorted(committed)


def _evaluation_action_kind(command: Sequence[str]) -> str | None:
    """识别 VC-5 评估动作：``assertion``（builder）、``compare``、``accept``；其余返回 None。"""

    adapter = _adapter()
    basenames = {Path(token).name for token in command[:4]}
    if adapter.evaluation_builder_basename in {Path(token).name for token in command}:
        return "assertion"
    if basenames & adapter.orchestrator_basenames:
        for token in command:
            if token in {"compare", "accept"}:
                return token
    return None


def _freeze_evaluation_output_bindings(
    campaign_dir: Path,
    candidate_id: str,
    baseline: int,
    actions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """编译侧为评估动作写死 output_bindings（Campaign 相对）；操作员自带声明必须逐字相等。

    断言 builder：当前基线断言目录的 ``evaluation-run.json`` 与 ``checkpoints``；compare：
    compare 阶段结果的规范读来源（reused 基线下即前序路径）；accept：accept 阶段结果写目标
    与其输入 ``evaluation-run.json``。
    """

    adapter = _adapter()
    assertions_root = adapter.orchestrator._stage_write_target(campaign_dir, candidate_id, baseline, "assertions")
    frozen: list[dict[str, Any]] = []
    for action in actions:
        normalized = dict(action)
        kind = adapter.orchestrator._evaluation_action_kind(list(action["command"]))
        expected: list[str] | None = None
        if kind == "assertion":
            expected = [
                (assertions_root / "checkpoints").relative_to(campaign_dir).as_posix(),
                (assertions_root / "evaluation-run.json").relative_to(campaign_dir).as_posix(),
            ]
        elif kind == "compare":
            source = adapter.orchestrator._stage_read_source(campaign_dir, candidate_id, baseline, "compare")
            expected = [Path(source["path"]).relative_to(campaign_dir).as_posix()]
        elif kind == "accept":
            target = adapter.orchestrator._stage_write_target(campaign_dir, candidate_id, baseline, "accept")
            expected = [
                target.relative_to(campaign_dir).as_posix(),
                (assertions_root / "evaluation-run.json").relative_to(campaign_dir).as_posix(),
            ]
        if expected is not None:
            expected = sorted(set(expected))
            declared = action.get("output_bindings")
            if declared is not None and list(declared) != expected:
                raise adapter.orchestrator.ConfigurationError(
                    f"动作 {action['action_id']} 自带的 output_bindings 与编译侧冻结声明不一致。"
                )
            normalized["output_bindings"] = expected
        frozen.append(normalized)
    return frozen


def _require_stage_replay_proof_for_checkpoint(ledger_dir: Path, phase: str, checkpoint_path: Path) -> dict[str, Any]:
    """已有 checkpoint 的 VC-2／VC-3 逐字重派前，核对放行它的账本事件确实绑定了阶段幂等重派证明。

    放行事件取最后一条非预算控制事件：必须是本阶段、下一动作为逐字重派的 receipt_passed，且恰好绑定
    一份 stage_replay 收据；该证明判定可重派，并且每个动作记录的 checkpoint 摘要都等于当前 checkpoint。
    """

    adapter = _adapter()
    event = codex_upgrade_timing_ledger.last_substantive_event(ledger_dir)
    if (
        event is None
        or event.get("event_type") != "receipt_passed"
        or event.get("phase") != phase
        or event.get("next_action") != "redispatch-same-batch"
    ):
        raise adapter.orchestrator.ConfigurationError(f"{phase} 已有 checkpoint，只允许阶段审核对账证明后的逐字重派。")
    bindings = [item for item in event.get("receipts", []) if item.get("role") == "stage_replay"]
    if len(bindings) != 1:
        raise adapter.orchestrator.ConfigurationError(f"{phase} 已有 checkpoint，但放行事件没有绑定阶段幂等重派证明。")
    proof_path = ledger_dir / str(bindings[0].get("path", ""))
    if proof_path.is_symlink() or not proof_path.is_file() or adapter.orchestrator.file_sha256(proof_path) != bindings[0].get("sha256"):
        raise adapter.orchestrator.ConfigurationError("阶段幂等重派证明的账本副本漂移。")
    proof = adapter.orchestrator._read_json(proof_path, "阶段幂等重派证明")
    checkpoint_sha256 = adapter.orchestrator.file_sha256(checkpoint_path)
    actions = proof.get("actions")
    if (
        proof.get("schema_version") != codex_upgrade_vc_artifacts.STAGE_REPLAY_SCHEMA
        or proof.get("decision") != "recoverable"
        or proof.get("allowed") is not True
        or proof.get("phase") != phase
        or proof.get("next_action") != "redispatch-same-batch"
        or not isinstance(actions, list)
        or not actions
        or any(not isinstance(action, Mapping) or not isinstance(action.get("outputs"), Mapping)
               or action["outputs"].get("checkpoint") != checkpoint_sha256 for action in actions)
    ):
        raise adapter.orchestrator.ConfigurationError(f"{phase} 的阶段幂等重派证明与当前 checkpoint 不一致，禁止再编译。")
    return proof


def _candidate_stage_replay_action_facts(
    campaign_dir: Path,
    run_manifest: Mapping[str, Any],
    argv: Sequence[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """R18：VC-4 零请求动作的幂等重派合同，只读核验，不执行命令、不补写产物。

    plan-candidate-gates 的输出 write-once 且命令拒绝覆盖，只有输出尚不存在时逐字重派才会成功；
    record-candidate-build 的 build-evidence、revision seal、构建收据与 VC-4 checkpoint 都是写入或逐字核对，
    已写半成品按摘要绑定并核对候选与构建标识，逐字重派在其上续作。候选须属当前 revision、未作废、
    未被取代，且尚无候选 attempt；否则留在候选审核（作废开新 revision 或停线）。
    """

    adapter = _adapter()
    action = argv[0] if argv else ""
    if action not in CANDIDATE_STAGE_REPLAY_FLAGS or len(argv[1:]) % 2:
        raise adapter.orchestrator.ConfigurationError("阶段动作或参数形态没有幂等合同")
    required, optional = CANDIDATE_STAGE_REPLAY_FLAGS[action]
    flags = dict(zip(argv[1::2], argv[2::2]))
    if (
        len(flags) * 2 != len(argv) - 1
        or not required <= set(flags)
        or set(flags) - required - optional
        or flags["--campaign-dir"] != str(campaign_dir)
    ):
        raise adapter.orchestrator.ConfigurationError(f"{action} 参数没有精确闭合或 Campaign 参数漂移")
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    revision, record = adapter.orchestrator._require_candidate_revision(campaign_dir, manifest, action=f"{action} 重派证明")
    candidate_id = flags["--candidate-id"]
    expected_candidate = (
        str(record["candidate_id"]) if record is not None else adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
    )
    if (
        candidate_id != expected_candidate
        or run_manifest.get("candidate_id", candidate_id) != candidate_id
        or run_manifest.get("candidate_revision", revision) != revision
    ):
        raise adapter.orchestrator.ConfigurationError("重派动作的候选不属于当前 revision 或与失败批次绑定不一致")
    candidate_root = campaign_dir / "candidates" / candidate_id
    for marker in (adapter.orchestrator.CANDIDATE_INVALIDATION_FILENAME, "superseded-by.json"):
        if (candidate_root / marker).exists() or (candidate_root / marker).is_symlink():
            raise adapter.orchestrator.ConfigurationError(f"候选 {candidate_id} 已作废或被取代，只能开新 revision")
    if adapter.orchestrator._active_unsealed_attempts(campaign_dir, "candidate", _manifest=manifest):
        raise adapter.orchestrator.ConfigurationError("VC-4 已出现 Candidate attempt，构建动作不能重派")
    source = Path(flags["--candidate-source"])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise adapter.orchestrator.ConfigurationError("候选源码树不可信")
    source = source.resolve(strict=True)
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}
    if action == "plan-candidate-gates":
        output = Path(flags["--output"])
        if not output.is_absolute():
            raise adapter.orchestrator.ConfigurationError("门禁执行计划输出路径不可信")
        if output.exists() or output.is_symlink():
            raise adapter.orchestrator.ConfigurationError("门禁执行计划输出已存在，逐字重派会被命令拒绝覆盖")
        inputs["--mapping"] = adapter.orchestrator.file_sha256(adapter.orchestrator._path_in_candidate_source(Path(flags["--mapping"]), source, "门禁映射"))
    else:
        for flag in ("--candidate-binary", "--build-parameters", "--source-transition"):
            inputs[flag] = adapter.orchestrator._stage_replay_input_file(Path(flags[flag]), flag)
        implementation_root = Path(flags["--implementation-test-root"])
        implementation_receipt = Path(flags["--implementation-test-receipt"])
        if not implementation_receipt.is_absolute():
            implementation_receipt = implementation_root / implementation_receipt
        inputs["--implementation-test-receipt"] = adapter.orchestrator._stage_replay_input_file(
            implementation_receipt, "--implementation-test-receipt"
        )
        inputs["--gate-plan"] = adapter.orchestrator.file_sha256(adapter.orchestrator._path_in_candidate_source(Path(flags["--gate-plan"]), source, "门禁执行计划"))
        for flag in (
            "--candidate-source", "--build-tree", "--docker-context", "--frontend-dist-source",
            "--catalog-stage-dir", "--implementation-test-root",
        ):
            inputs[flag] = adapter.orchestrator._stage_replay_input_directory(Path(flags[flag]), flag)
        receipt_path = adapter.orchestrator._candidate_build_receipt_path(campaign_dir, candidate_id)
        written = [receipt_path, *sorted((receipt_path.parent / "build-evidence").glob("*"))]
        if record is not None:
            written.append(adapter.orchestrator._candidate_revision_dir(campaign_dir, revision) / "seal.json")
        for path in written:
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_file():
                raise adapter.orchestrator.ConfigurationError("VC-4 已写半成品包含符号链接或非普通文件")
            outputs[path.relative_to(campaign_dir).as_posix()] = adapter.orchestrator.file_sha256(path)
        if receipt_path.exists():
            receipt = adapter.orchestrator._read_json(receipt_path, "Candidate 构建收据")
            build = receipt.get("build") if isinstance(receipt.get("build"), Mapping) else {}
            image = receipt.get("image") if isinstance(receipt.get("image"), Mapping) else {}
            if (
                receipt.get("candidate_id") != candidate_id
                or build.get("build_id") != flags["--build-id"]
                or image.get("image_id") != flags["--candidate-image-id"]
            ):
                raise adapter.orchestrator.ConfigurationError("已写构建收据与重派动作的候选或构建标识不一致")
    checkpoint = adapter.orchestrator._vc_checkpoint_path(campaign_dir, "VC-4", revision=revision)
    if checkpoint.exists() or checkpoint.is_symlink():
        adapter.orchestrator._replay_vc_checkpoint(campaign_dir, adapter.orchestrator._campaign_vc_plan(campaign_dir), "VC-4", revision=revision)
        outputs["checkpoint"] = adapter.orchestrator.file_sha256(checkpoint)
    return inputs, outputs


def _campaign_vc_plan(campaign_dir: Path) -> dict[str, Any]:
    """读取已由 Campaign 绑定的总计划，供阶段恢复重放 checkpoint。"""

    adapter = _adapter()
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    binding = manifest["vc_control"]["campaign_plan"]
    path = adapter.orchestrator._campaign_file(campaign_dir, binding["path"])
    if adapter.orchestrator.file_sha256(path) != binding["sha256"]:
        raise adapter.orchestrator.ConfigurationError("Campaign 总计划绑定漂移")
    return codex_upgrade_vc_artifacts.validate_campaign_plan(adapter.orchestrator._read_json(path, "Campaign 总计划"))


def compile_vc_batch(
    arguments: argparse.Namespace,
    *,
    staging_attempt_dir: Path | None = None,
    owner_nonce: str | None = None,
) -> dict[str, Any]:
    """从直接前序 checkpoint 一次生成 batch 和可执行 v2 队列。

    改造 4：staging 模型的 Campaign 只能由 ``compile-and-run-vc-batch`` 在锁内预分配
    ``owner_nonce`` 与 staging attempt 目录后调用，产物写到
    ``control/vc/staging/NNNN-vc-x/attempt-K/{batch.json, run-manifest.json}`` 并以
    ``PREPARED`` 标记闭合；正式序号只在 COMMIT 写入时占用。legacy 模型保持原行为。
    """

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest):
        raise adapter.orchestrator.ConfigurationError("compile-vc-batch 只用于 0.154.0 起的完整 VC 链。")
    phase = str(arguments.phase)
    sequence = arguments.sequence
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 2:
        raise adapter.orchestrator.ConfigurationError("后继 VC batch --sequence 必须是大于等于 2 的连续整数。")
    control = manifest.get("vc_control")
    plan_reference = control.get("campaign_plan") if isinstance(control, Mapping) else None
    adapter.orchestrator._require_file_binding(plan_reference, "Campaign 总计划")
    assert isinstance(plan_reference, dict)
    plan_path = adapter.orchestrator._campaign_file(campaign_dir, plan_reference["path"])
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(
            adapter.orchestrator._read_json(plan_path, "Campaign 总计划")
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    staging_model = adapter.orchestrator._campaign_uses_staging_model(campaign_dir, plan)
    if staging_model:
        if staging_attempt_dir is None or owner_nonce is None:
            raise adapter.orchestrator.ConfigurationError(
                "staging 模型 Campaign 的批次只能由 compile-and-run-vc-batch 在锁内"
                "预分配 owner_nonce 与 staging attempt 后编译。"
            )
        try:
            codex_upgrade_vc_artifacts._sha256(owner_nonce, "owner_nonce")
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise adapter.orchestrator.ConfigurationError(str(error)) from error
        expected_parent = adapter.orchestrator._staging_sequence_dir(campaign_dir, sequence, phase)
        if (
            staging_attempt_dir.parent != expected_parent
            or _STAGING_ATTEMPT_RE.fullmatch(staging_attempt_dir.name) is None
        ):
            raise adapter.orchestrator.ConfigurationError("staging attempt 目录不是本序号的规范路径。")
    elif staging_attempt_dir is not None or owner_nonce is not None:
        raise adapter.orchestrator.ConfigurationError("legacy 批次模型的 Campaign 不接受 staging 参数。")
    predecessor_phase = codex_upgrade_vc_artifacts.VC_PHASES[
        codex_upgrade_vc_artifacts.VC_PHASES.index(phase) - 1
    ]
    # 改造 2：候选级阶段的批次绑定当前 active revision 与其候选；VC-4 的前序是
    # Campaign 级 VC-3，VC-5／VC-6 的前序是同 revision 的上一阶段。
    candidate_revision: int | None = None
    candidate_id: str | None = None
    if phase in adapter.orchestrator.CANDIDATE_VC_PHASES:
        candidate_revision, revision_record = adapter.orchestrator._require_candidate_revision(
            campaign_dir, manifest, action=f"{phase} 批次编译"
        )
        if revision_record is not None:
            candidate_id = str(revision_record["candidate_id"])
        else:
            candidate_id = adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
    predecessor_path, predecessor = adapter.orchestrator._replay_vc_checkpoint(
        campaign_dir,
        plan,
        predecessor_phase,
        revision=candidate_revision if predecessor_phase in adapter.orchestrator.CANDIDATE_VC_PHASES else None,
    )
    try:
        supplied_predecessor = arguments.predecessor_checkpoint.resolve(strict=True)
    except OSError as error:
        raise adapter.orchestrator.ConfigurationError("--predecessor-checkpoint 不存在。") from error
    if supplied_predecessor != predecessor_path.resolve(strict=True):
        raise adapter.orchestrator.ConfigurationError(
            f"{phase} batch 必须引用规范 {predecessor_phase} checkpoint。"
        )
    completed_path = adapter.orchestrator._vc_checkpoint_path(campaign_dir, phase, revision=candidate_revision)
    if completed_path.exists() or completed_path.is_symlink():
        ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
        ledger = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        # 只允许已对账且尚未完成父批次的同阶段逐字重派；正式后继协议再绑定原 run。
        # R18：候选审核下 VC-4 凭同一种阶段幂等重派证明重开，checkpoint 按当前 revision 定位。
        if (phase not in {"VC-2", "VC-3", *codex_upgrade_timing_ledger.CANDIDATE_STAGE_REPLAY_PHASES}
                or ledger.get("active_phase") != phase
                or ledger.get("next_action") != "redispatch-same-batch"):
            raise adapter.orchestrator.ConfigurationError(f"{phase} 已有 checkpoint，禁止再编译执行批次。")
        adapter.orchestrator._replay_vc_checkpoint(campaign_dir, plan, phase, revision=candidate_revision)
        # R4：同一 next_action 也可能来自环境恢复等其他对账；已有 checkpoint 的阶段只允许凭阶段审核对账
        # 写入账本的幂等重派证明逐字重派，且证明记录的 checkpoint 必须就是当前这份。
        adapter.orchestrator._require_stage_replay_proof_for_checkpoint(ledger_dir, phase, completed_path)
    if (
        not arguments.action_plan.is_absolute()
        or arguments.action_plan.is_symlink()
        or not arguments.action_plan.is_file()
    ):
        raise adapter.orchestrator.ConfigurationError("--action-plan 必须是可信绝对普通文件。")
    try:
        action_plan = codex_upgrade_vc_artifacts.validate_action_plan(
            adapter.orchestrator._read_json(arguments.action_plan, "VC action plan"),
            phase=phase,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    # 改造 5：候选级 VC-5 批次冻结当前评估基线（b0 为 null）与 evaluator 四项直接依赖摘要，
    # 并为评估动作（断言 builder／compare／accept）固定 output_bindings。
    evaluation_baseline: int | None = None
    baseline_commit_sha256: str | None = None
    evaluator_digests: dict[str, str] | None = None
    actions = list(action_plan["actions"])
    if phase == "VC-5":
        assert candidate_id is not None
        current_baseline, baseline_commit = adapter.orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
        if current_baseline:
            evaluation_baseline = current_baseline
            baseline_commit_sha256 = str(baseline_commit["commit_sha256"])  # type: ignore[index]
        try:
            evaluator_digests = adapter.identity_policy.evaluator_dependency_digests()
        except adapter.identity_policy.ToolIdentityPolicyError as error:
            raise adapter.orchestrator.ConfigurationError(f"evaluator 依赖摘要无法计算：{error}") from error
        # 起点防伪：b0 的 checker／builder 冻结值必须等于 plan 工具身份的规范 entry；
        # b≥1 四项必须等于该基线 recovery.json 授权值。不等即拒绝编译，不做归一化。
        adapter.orchestrator._verify_evaluator_digests_authorized(
            campaign_dir, manifest, candidate_id, current_baseline, evaluator_digests, label="编译冻结 evaluator 摘要"
        )
        actions = adapter.orchestrator._freeze_evaluation_output_bindings(campaign_dir, candidate_id, current_baseline, actions)

    batches_root = campaign_dir / "control" / "vc" / "batches"
    manifests_root = campaign_dir / "control" / "vc" / "run-manifests"
    existing_sequences: list[int] = []
    if staging_model:
        # 序号占用 = 存在 COMMIT（序号 1 由 VC-0 绑定）；正式 batches/ 里没有 COMMIT 的
        # 半产物不计入，由派发入口归档。
        existing_sequences = adapter.orchestrator._committed_vc_sequences(campaign_dir, manifest)
    else:
        for path in sorted(batches_root.glob("[0-9][0-9][0-9][0-9]-vc-*.json")):
            try:
                existing_sequences.append(int(path.name.split("-", 1)[0]))
            except ValueError as error:
                raise adapter.orchestrator.ConfigurationError("既有 VC batch 文件名非法。") from error
    if existing_sequences != list(range(1, sequence)):
        raise adapter.orchestrator.ConfigurationError(
            "VC batch 全局序号必须连续；当前既有序号="
            f"{existing_sequences}，请求序号={sequence}。"
        )
    now = datetime.now(timezone.utc)
    deadlines = codex_upgrade_vc_artifacts.effective_deadlines(campaign_dir)
    if deadlines["paused_scopes"]:
        raise adapter.orchestrator.ConfigurationError("预算暂停期间不得编译新批次，必须先批准延期")
    deadline = datetime.fromisoformat(str(deadlines["total_deadline_at_utc"]).replace("Z", "+00:00"))
    if deadline <= now:
        raise adapter.orchestrator.ConfigurationError(
            "Campaign 有效截止（含已批准延期）已到期，禁止生成新批次；先 deadline-extend 或 campaign-abandon。"
        )
    must_start = min(now + timedelta(seconds=60), deadline)
    compiled_at = now.isoformat(timespec="seconds")
    try:
        batch = codex_upgrade_vc_artifacts.build_vc_batch(
            campaign_plan=plan,
            phase=phase,
            sequence=sequence,
            predecessor_checkpoint=adapter.orchestrator._vc_checkpoint_reference(
                campaign_dir,
                predecessor_path,
                predecessor,
            ),
            execute_item_ids=action_plan["execute_item_ids"],
            reuse_item_ids=action_plan["reuse_item_ids"],
            actions=actions,
            compiled_at_utc=compiled_at,
            must_start_by_utc=must_start.isoformat(timespec="seconds"),
            candidate_revision=candidate_revision,
            candidate_id=candidate_id,
            evaluation_baseline=evaluation_baseline,
            baseline_commit_sha256=baseline_commit_sha256,
            evaluator_digests=evaluator_digests,
            deadline_extension=deadlines["campaign_extension"],
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    run_manifest = adapter.orchestrator._vc_run_manifest_from_batch(batch, batch_model="staging" if staging_model else "legacy")
    if staging_model:
        assert staging_attempt_dir is not None and owner_nonce is not None
        return adapter.orchestrator._write_staging_batch_artifacts(
            campaign_dir,
            staging_attempt_dir,
            plan=plan,
            batch=batch,
            run_manifest=run_manifest,
            owner_nonce=owner_nonce,
            prepared_at_utc=compiled_at,
        )
    batch_path = batches_root / f"{sequence:04d}-{phase.lower()}.json"
    run_path = manifests_root / f"{sequence:04d}-{phase.lower()}.json"
    ensure_private_directory(batches_root, campaign_dir)
    ensure_private_directory(manifests_root, campaign_dir)
    adapter.orchestrator._secure_write_json_once(batch_path, batch)
    try:
        adapter.orchestrator._secure_write_json_once(run_path, run_manifest)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as error:
        # batch 已是不可变审计事实；若第二个文件写入失败，不删除或覆盖，明确停线。
        raise adapter.orchestrator.ConfigurationError(
            "VC batch 已封存但 campaign-run 清单写入失败；禁止覆盖，需按恢复策略处理。"
        ) from error
    return {
        "status": "complete",
        "campaign_id": plan["campaign_id"],
        "phase": phase,
        "batch_sequence": sequence,
        "batch": str(batch_path),
        "batch_sha256": batch["batch_sha256"],
        "campaign_run_manifest": str(run_path),
        "original_deadline_at_utc": batch["original_deadline_at_utc"],
        "execute_item_ids": batch["execute_item_ids"],
        "reuse_item_ids": batch["reuse_item_ids"],
    }


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
    """把编译产物写进 staging attempt 目录并以 PREPARED 闭合；不触碰正式路径。"""

    adapter = _adapter()
    sequence = int(batch["sequence"])
    phase = str(batch["phase"])
    staging_root = campaign_dir / "control" / "vc" / "staging"
    ensure_private_directory(staging_root, campaign_dir)
    ensure_private_directory(attempt_dir.parent, campaign_dir)
    if attempt_dir.exists() or attempt_dir.is_symlink():
        raise adapter.orchestrator.ConfigurationError(f"staging attempt 目录已存在，禁止复用：{attempt_dir.name}")
    ensure_private_directory(attempt_dir, campaign_dir)
    batch_path = attempt_dir / STAGING_BATCH_FILENAME
    manifest_path = attempt_dir / STAGING_MANIFEST_FILENAME
    adapter.orchestrator._secure_write_json_once(batch_path, dict(batch))
    adapter.orchestrator._secure_write_json_once(manifest_path, dict(run_manifest))
    attempt = int(_STAGING_ATTEMPT_RE.fullmatch(attempt_dir.name).group(1))  # type: ignore[union-attr]
    try:
        marker = codex_upgrade_vc_artifacts.build_staging_prepared_marker(
            campaign_id=str(plan["campaign_id"]),
            sequence=sequence,
            phase=phase,
            attempt=attempt,
            batch_sha256=str(batch["batch_sha256"]),
            manifest_sha256=codex_upgrade_supervisor._sha256(
                codex_upgrade_supervisor._canonical(run_manifest)
            ),
            owner_nonce=owner_nonce,
            prepared_at_utc=prepared_at_utc,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    adapter.orchestrator._secure_write_json_once(attempt_dir / adapter.orchestrator.STAGING_PREPARED_FILENAME, marker)
    return {
        "status": "prepared",
        "batch_model": "staging",
        "campaign_id": plan["campaign_id"],
        "phase": phase,
        "batch_sequence": sequence,
        "staging_attempt": attempt,
        "staging_attempt_dir": str(attempt_dir),
        "batch": str(batch_path),
        "batch_sha256": batch["batch_sha256"],
        "campaign_run_manifest": str(manifest_path),
        "prepared_marker": str(attempt_dir / adapter.orchestrator.STAGING_PREPARED_FILENAME),
        "prepared_marker_sha256": marker["marker_sha256"],
        "owner_nonce": owner_nonce,
        "original_deadline_at_utc": batch["original_deadline_at_utc"],
        "execute_item_ids": batch["execute_item_ids"],
        "reuse_item_ids": batch["reuse_item_ids"],
    }


def _timing_ledger_batch_events(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    phase: str,
    ledger_dir: Path,
) -> tuple[int, list[tuple[str, str]]]:
    """只读推导派发 ``phase`` 批次前要补写的账本事件序列。

    返回 ``(当前 head_sequence, [(event_type, phase), ...])``。规则：
    账本必须 active 且版本、用途与 Campaign 一致；目标阶段已是 active 阶段时
    不写事件（同阶段后续批次）；否则把尚未登记完成、且 checkpoint 已封存的
    前序阶段依次补成 ``stage_completed``（只读导入的 Campaign 会一次补齐
    VC-0／VC-1），再为目标阶段写 ``stage_started``。阶段倒退或前序未封存即拒绝。
    """

    adapter = _adapter()
    state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
    for field in ("baseline_version", "target_version", "campaign_purpose"):
        if state.get(field) != manifest.get(field):
            raise adapter.orchestrator.TimingLedgerGateError("UpgradeTimingLedger 与 Campaign 版本或用途不一致。")
    if state.get("status") != "active":
        raise adapter.orchestrator.TimingLedgerGateError(
            f"UpgradeTimingLedger 当前状态为 {state.get('status')}，禁止编译 {phase} 批次；"
            "阶段或总预算先到即停线，不得新建账本重置计时。"
        )
    order = codex_upgrade_vc_artifacts.VC_PHASES
    target_index = order.index(phase)
    active = state.get("active_phase")
    # 改造 2：候选级阶段必须有 active revision；账本 completed_phases 已按当前 revision
    # 计算，被作废 revision 完成的 VC-4～VC-6 不算数。目标阶段序低于已完成最大阶段时，
    # 只有当前 revision 尚未完成该阶段、且最后一条 stage_revision 的阶段不超过目标阶段
    # （stage_revision 固定在 VC-4）才放行。
    current_revision: int | None = None
    if phase in adapter.orchestrator.CANDIDATE_VC_PHASES:
        current_revision, _record = adapter.orchestrator._require_candidate_revision(
            campaign_dir, manifest, action=f"{phase} 批次派发"
        )
        if state.get("current_revision") not in {None, current_revision}:
            raise adapter.orchestrator.TimingLedgerGateError(
                f"UpgradeTimingLedger 当前 revision {state.get('current_revision')} 与 Campaign 当前 revision r{current_revision} 不一致。"
            )
    if active == phase:
        return int(state["head_sequence"]), []
    if isinstance(active, str) and active in order and order.index(active) > target_index:
        raise adapter.orchestrator.TimingLedgerGateError(
            f"UpgradeTimingLedger 当前阶段 {active} 已越过 {phase}，禁止阶段倒退。"
        )
    completed = set(state.get("completed_phases", []))
    if phase in completed:
        raise adapter.orchestrator.TimingLedgerGateError(
            f"UpgradeTimingLedger 已登记 {phase} 在当前 revision 完成，禁止重开。"
        )
    highest_completed = max((order.index(item) for item in completed), default=-1)
    if highest_completed > target_index:
        last_revision_event = adapter.orchestrator._ledger_last_stage_revision(ledger_dir)
        if (
            current_revision is None
            or last_revision_event is None
            or order.index(str(last_revision_event.get("phase"))) > target_index
        ):
            raise adapter.orchestrator.TimingLedgerGateError(
                f"UpgradeTimingLedger 已完成的阶段越过 {phase}，且没有 stage_revision 授权回到该阶段。"
            )
    events: list[tuple[str, str]] = []
    for earlier in order[:target_index]:
        if earlier in completed:
            continue
        # 补登记的前序阶段必须已有成功封存的 checkpoint；缺失即拒绝，不得跳过阶段。
        adapter.orchestrator._replay_vc_checkpoint(
            campaign_dir,
            plan,
            earlier,
            revision=current_revision if earlier in adapter.orchestrator.CANDIDATE_VC_PHASES else None,
        )
        if active == earlier:
            events.append(("stage_completed", earlier))
        else:
            events.append(("stage_started", earlier))
            events.append(("stage_completed", earlier))
    events.append(("stage_started", phase))
    return int(state["head_sequence"]), events


def _append_timing_ledger_batch_events(
    ledger_dir: Path,
    events: list[tuple[str, str]],
    *,
    phase: str,
    sequence: int,
    expected_head: int,
) -> list[dict[str, Any]]:
    """在账本收口锁内按序追加事件；head 在编译期间被改写即拒绝。"""

    adapter = _adapter()
    if not events:
        return []
    written: list[dict[str, Any]] = []
    try:
        with codex_upgrade_supervisor._timing_closeout_lock(ledger_dir):
            state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
            if int(state["head_sequence"]) != expected_head:
                raise adapter.orchestrator.TimingLedgerGateError("UpgradeTimingLedger 在编译期间被其他流程改写。")
            for event_type, event_phase in events:
                kind = "started" if event_type == "stage_started" else "completed"
                event_id = f"vc-batch-{sequence:04d}-{phase.lower()}-{event_phase.lower()}-{kind}"
                if event_type == "stage_started" and event_phase == phase:
                    next_action = f"派发 {phase} 批次 {sequence:04d}"
                else:
                    next_action = f"{event_phase} 已封存，继续补齐账本至 {phase}"
                summary = codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=event_id,
                    phase=event_phase,
                    event_type=event_type,
                    next_action=next_action,
                )
                written.append(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "phase": event_phase,
                        "head_sequence": summary["head_sequence"],
                        "head_sha256": summary["head_sha256"],
                    }
                )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.TimingLedgerGateError(str(error)) from error
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise adapter.orchestrator.TimingLedgerGateError(f"UpgradeTimingLedger 拒绝推进阶段：{error}") from error
    return written


def _complete_timing_ledger_phase_after_batch(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    *,
    phase: str,
    sequence: int,
    ledger_dir: Path,
) -> dict[str, Any] | None:
    """父 run 成功后，本阶段 checkpoint 已封存则写 ``stage_completed``。

    阶段之间的人工核对因此不再占阶段墙钟；没有 checkpoint（同阶段还有后续
    批次）时不写事件。同阶段重复调用幂等。
    """

    adapter = _adapter()
    revision: int | None = None
    if phase in adapter.orchestrator.CANDIDATE_VC_PHASES:
        campaign = adapter.orchestrator._read_json(campaign_dir / "campaign.json", "Campaign 清单")
        revision = adapter.orchestrator._current_candidate_revision(campaign_dir, campaign)
        if revision is None:
            return None
    path = adapter.orchestrator._vc_checkpoint_path(campaign_dir, phase, revision=revision)
    if path.is_symlink() or not path.is_file():
        return None
    adapter.orchestrator._replay_vc_checkpoint(campaign_dir, plan, phase, revision=revision)
    try:
        with codex_upgrade_supervisor._timing_closeout_lock(ledger_dir):
            state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
            if phase in state.get("completed_phases", []):
                return {
                    "idempotent": True,
                    "head_sequence": state["head_sequence"],
                    "head_sha256": state["head_sha256"],
                }
            if state.get("active_phase") != phase:
                raise adapter.orchestrator.TimingLedgerGateError(
                    f"UpgradeTimingLedger 当前阶段为 {state.get('active_phase')}，"
                    f"无法登记 {phase} 完成。"
                )
            event_id = f"vc-batch-{sequence:04d}-{phase.lower()}-completed"
            summary = codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=event_id,
                phase=phase,
                event_type="stage_completed",
                next_action=f"{phase} checkpoint 已封存；人工核对后编译下一阶段批次",
            )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.TimingLedgerGateError(str(error)) from error
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise adapter.orchestrator.TimingLedgerGateError(
            f"父 run 已成功且 {phase} checkpoint 已封存，但 UpgradeTimingLedger 拒绝登记完成：{error}"
        ) from error
    return {
        "idempotent": False,
        "event_id": event_id,
        "head_sequence": summary["head_sequence"],
        "head_sha256": summary["head_sha256"],
    }


def _bootstrap_noop_first_batch(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    sequence: int,
    state_dir: Path,
    run_arguments: argparse.Namespace,
) -> dict[str, Any] | None:
    """只读导入的 Campaign 在派发第 2 批前，先把零请求 no-op 首批跑成父 run 历史。

    official-only 后继只生成“全部 official Job 为 reuse”的 VC-1 no-op 批次
    与 checkpoint，不派发父 run；而监督器要求同一 state-dir 的 batch_sequence 从 1
    连续递增。没有这一步，恢复 Campaign 的 VC-2 首批会被“禁止跳批”拒绝。
    首批含真实动作（普通 Formal 的 capture-official）时不代跑，沿用原拒绝语义。
    """

    adapter = _adapter()
    if sequence != 2:
        return None
    campaign_id = str(manifest.get("campaign_id", ""))
    try:
        history = codex_upgrade_supervisor._campaign_run_history(state_dir, campaign_id)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    if history:
        return None
    first_manifest_path = campaign_dir / "control" / "vc" / "run-manifests" / "0001-vc-1.json"
    if first_manifest_path.is_symlink() or not first_manifest_path.is_file():
        return None
    try:
        first_manifest = codex_upgrade_supervisor._campaign_run_manifest(first_manifest_path)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(f"首批 VC-1 清单不可信：{error}") from error
    if first_manifest.get("no_op") is not True or first_manifest.get("campaign_id") != campaign_id:
        return None
    try:
        returncode, run = codex_upgrade_supervisor._campaign_run_locked(
            run_arguments,
            manifest=first_manifest,
            state_dir=state_dir,
            campaign_dir=campaign_dir,
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(f"no-op 首批引导派发失败：{error}") from error
    # 以落盘终态为准：首批父 run 要作为后续批次的可信前序，state.json 必须真正封存为 stopped。不是（例如 stop 等不到
    # monitor 被封成 audit-incomplete）就当场失败并点名落盘终态，不把不可信前序留给下一批的批次历史校验。
    try:
        sealed_state = codex_upgrade_supervisor._read_state(Path(str(run["run_dir"]))).get("state")
    except (KeyError, codex_upgrade_supervisor.SupervisorError) as error:
        raise adapter.orchestrator.ConfigurationError(f"no-op 首批引导派发的父 run 终态无法读取：{error}") from error
    if returncode != 0 or run.get("reason") != "incremental-noop" or sealed_state != "stopped":
        raise adapter.orchestrator.ConfigurationError(
            f"no-op 首批引导派发未闭合：status={run.get('status')} reason={run.get('reason')} 落盘终态={sealed_state}；"
            "该首批父 run 不能作为可信前序，后续批次会被批次历史校验拒绝，须先人工审计该 run"
        )
    return {
        "run_dir": run.get("run_dir"),
        "status": run.get("status"),
        "reason": run.get("reason"),
        "batch_sequence": first_manifest.get("batch_sequence"),
    }


def _prepare_atomic_batch_governance(arguments: argparse.Namespace) -> dict[str, Any]:
    """原子派发前的三层治理：工具演进登记预检、项目总账 admission 与时间账本只读预检。

    三者都在取 state-dir 锁与任何落盘之前完成：工具演进预检先行——修好工具部署后若尚未
    登记工具演进，后面的孤儿对账会按当前身份把中断批次误判为身份变化并写永久终态，这里
    零写入拒绝；admission 与 ``campaign-run`` CLI 共用同一门禁（此前原子入口绕过了它）；
    账本预检只读，拒绝时不产生 batch、manifest 或停线收据。
    """

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    phase = str(arguments.phase)
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest):
        raise adapter.orchestrator.ConfigurationError("compile-and-run-vc-batch 只用于 0.154.0 起的完整 VC 链。")
    adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="compile-and-run-vc-batch")
    adapter.orchestrator._refuse_seal_chain_on_evolution_invalidated_attempt(
        campaign_dir, manifest, phase=phase, action_plan=getattr(arguments, "action_plan", None)
    )
    adapter.orchestrator._refuse_vc1_seal_batch_against_bundle_state(
        manifest, phase=phase, action_plan=getattr(arguments, "action_plan", None)
    )
    # 与 campaign-run CLI（codex_upgrade_supervisor._assert_campaign_run_admitted）同一底层门禁：
    # 补齐器先行、锁内重放，0.154 formal 必须已注册且未 blocked／终态／超预算。
    try:
        admission = codex_upgrade_project_ledger.assert_campaign_admitted(
            campaign_dir,
            command="campaign-run",
            require=adapter.orchestrator._project_ledger_required(manifest.get("campaign_mode"), manifest.get("target_version")),
        )
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"项目总账拒绝派发：{error}") from error
    plan = adapter.orchestrator._vc_campaign_plan(campaign_dir, manifest)
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    head_sequence, events = adapter.orchestrator._timing_ledger_batch_events(
        campaign_dir,
        manifest,
        plan,
        phase=phase,
        ledger_dir=ledger_dir,
    )
    return {
        "manifest": manifest,
        "plan": plan,
        "ledger_dir": ledger_dir,
        "ledger_head_sequence": head_sequence,
        "ledger_events": events,
        "admission": admission,
    }


def _empty_batch_governance() -> dict[str, Any]:
    """零网络生产同形演练（campaign_run_rehearsal_receipt）的治理占位。

    演练作用于合成 Campaign：没有项目总账注册，账本也只是夹具，治理层不适用；
    正式 CLI 永远走 ``_prepare_atomic_batch_governance``。
    """

    return {
        "manifest": {},
        "plan": {},
        "ledger_dir": None,
        "ledger_head_sequence": 0,
        "ledger_events": [],
        "admission": None,
    }


def compile_and_run_vc_batch(
    arguments: argparse.Namespace,
    *,
    _compiler: Any | None = None,
) -> tuple[dict[str, Any], int]:
    """在同一 state-dir 锁内编译后继批次并立即创建父 run。

    ``_compiler`` 只供零网络生产同形演练注入同一制品构建器；正式 CLI 永远
    使用 ``compile_vc_batch``，参数面不能选择或替换编译实现。

    0.154 起本入口是 VC-2～VC-6 每一批的唯一强制入口，因此项目总账 admission、
    时间账本的阶段推进（``stage_started``／``stage_completed``）都在这里闭合，
    不再依赖操作员人工 ``append``。

    改造 4：按 Campaign 总计划的 ``batch_model`` 分流——``staging`` 走
    孤儿对账 → prepare → prepared 父 run → commit 四步的新流程；``legacy``
    （历史 Campaign 与无总计划的夹具）保持原流程，只读重放不变。
    """

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    if _compiler is None:
        governance = adapter.orchestrator._prepare_atomic_batch_governance(arguments)
    else:
        governance = adapter.orchestrator._empty_batch_governance()
    if adapter.orchestrator._campaign_batch_model(campaign_dir) == "staging":
        return adapter.orchestrator._compile_and_run_vc_batch_staging(
            arguments, governance=governance, _compiler=_compiler
        )
    return adapter.orchestrator._compile_and_run_vc_batch_legacy(
        arguments, governance=governance, _compiler=_compiler
    )


def _load_prepared_staging_attempt(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    owner_nonce: str | None,
    sequence: int,
    phase: str,
) -> dict[str, Any]:
    """读取并交叉校验 staging attempt 的 batch／run-manifest／PREPARED 三件套。"""

    adapter = _adapter()
    batch_path = attempt_dir / STAGING_BATCH_FILENAME
    manifest_path = attempt_dir / STAGING_MANIFEST_FILENAME
    marker_path = attempt_dir / adapter.orchestrator.STAGING_PREPARED_FILENAME
    for path in (batch_path, manifest_path, marker_path):
        if path.is_symlink() or not path.is_file():
            raise adapter.orchestrator.ConfigurationError(f"staging attempt 缺少可信产物：{path.name}")
    try:
        batch = codex_upgrade_vc_artifacts.validate_vc_batch(
            adapter.orchestrator._read_json(batch_path, "staging VC batch")
        )
        marker = codex_upgrade_vc_artifacts.validate_staging_prepared_marker(
            adapter.orchestrator._read_json(marker_path, "staging PREPARED 标记")
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    raw_manifest = adapter.orchestrator._read_json(manifest_path, "staging campaign-run 清单")
    try:
        run_manifest = codex_upgrade_supervisor._campaign_run_manifest(manifest_path)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    manifest_sha256 = codex_upgrade_supervisor._sha256(
        codex_upgrade_supervisor._canonical(raw_manifest)
    )
    attempt = int(_STAGING_ATTEMPT_RE.fullmatch(attempt_dir.name).group(1))  # type: ignore[union-attr]
    if (
        marker["sequence"] != sequence
        or marker["phase"] != phase
        or marker["attempt"] != attempt
        or marker["batch_sha256"] != batch["batch_sha256"]
        or marker["manifest_sha256"] != manifest_sha256
        or batch["sequence"] != sequence
        or batch["phase"] != phase
        or run_manifest.get("batch_sha256") != batch["batch_sha256"]
        or run_manifest.get("batch_sequence") != sequence
        or run_manifest.get("phase") != phase
    ):
        raise adapter.orchestrator.ConfigurationError("staging attempt 的 PREPARED、batch 与清单不一致。")
    # 改造 2：staging 清单必须携带候选级绑定，且与正式 batch 逐字一致。
    if (
        not codex_upgrade_supervisor.manifest_has_candidate_binding(run_manifest)
        or run_manifest.get("candidate_revision") != batch.get("candidate_revision")
        or run_manifest.get("candidate_id") != batch.get("candidate_id")
    ):
        raise adapter.orchestrator.ConfigurationError("staging 清单的 candidate_revision／candidate_id 缺失或与 batch 不一致。")
    # 改造 5：v3 批次的评估基线三字段与动作 output_bindings 必须与清单逐字一致；
    # v2 批次的清单不得携带评估基线字段。
    if batch.get("schema_version") == codex_upgrade_vc_artifacts.VC_BATCH_SCHEMA:
        if (
            "evaluation_baseline" not in run_manifest
            or run_manifest.get("evaluation_baseline") != batch.get("evaluation_baseline")
            or run_manifest.get("baseline_commit_sha256") != batch.get("baseline_commit_sha256")
            or run_manifest.get("evaluator_digests") != batch.get("evaluator_digests")
        ):
            raise adapter.orchestrator.ConfigurationError("staging 清单的评估基线字段缺失或与 batch 不一致。")
    elif "evaluation_baseline" in run_manifest:
        raise adapter.orchestrator.ConfigurationError("v2 批次的 staging 清单不得携带评估基线字段。")
    manifest_actions = {str(action["action_id"]): action for action in run_manifest.get("actions", [])}
    for action in batch.get("actions", []):
        recorded = manifest_actions.get(str(action["action_id"]))
        if recorded is None or recorded.get("output_bindings") != action.get("output_bindings"):
            raise adapter.orchestrator.ConfigurationError("staging 清单的动作 output_bindings 与 batch 不一致。")
    if owner_nonce is not None and marker["owner_nonce"] != owner_nonce:
        raise adapter.orchestrator.ConfigurationError("staging PREPARED 的 owner_nonce 与入口预分配值不一致。")
    return {
        "attempt": attempt,
        "batch": batch,
        "run_manifest": run_manifest,
        "raw_manifest": raw_manifest,
        "manifest_sha256": manifest_sha256,
        "marker": marker,
    }


def _publish_staging_file(source: Path, target: Path, campaign_dir: Path) -> None:
    """把 staging 产物逐字节发布到正式路径（write-once）；已存在且相同即幂等跳过。"""

    adapter = _adapter()
    payload = adapter.orchestrator._read_json(source, "staging 产物")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise adapter.orchestrator.ConfigurationError(f"正式产物路径不可信：{target}")
        if adapter.orchestrator.file_sha256(target) != adapter.orchestrator.file_sha256(source):
            raise adapter.orchestrator.ConfigurationError(f"正式产物已存在且内容不同，禁止覆盖：{target.name}")
        return
    ensure_private_directory(target.parent, campaign_dir)
    adapter.orchestrator._secure_write_json_once(target, payload)
    if adapter.orchestrator.file_sha256(target) != adapter.orchestrator.file_sha256(source):
        raise adapter.orchestrator.ConfigurationError(f"正式产物发布后与 staging 内容不一致：{target.name}")


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
    """写 staging ABORT（write-once + 内容核对）：已存在时逐字段核对当前事实，不同即失败关闭。

    只有 ``recorded_at_utc`` 与自摘要 ``receipt_sha256`` 是易变字段；其余字段（Campaign、
    总计划摘要、阶段、序号、attempt、stage、failure_kind、error_type、根因、产物摘要、父 run
    绑定、对账收据绑定、零请求断言）必须与既有收据完全一致，避免孤儿对账把错误的根因或
    收据绑定到既有 ABORT 上。``error_signature``（第 32 项）决定根因编码，是事实字段、参与核对。
    """

    adapter = _adapter()
    abort_path = attempt_dir / STAGING_ABORT_FILENAME
    try:
        abort = codex_upgrade_vc_artifacts.build_staging_abort(
            campaign_id=campaign_id,
            campaign_plan_sha256=campaign_plan_sha256,
            phase=phase,
            sequence=sequence,
            staging_attempt=attempt,
            stage=stage,
            failure_kind=failure_kind,
            error_type=error_type[:128],
            root_cause_id=root_cause_id,
            batch_sha256=batch_sha256,
            manifest_sha256=manifest_sha256,
            parent_run_dir=parent_run_dir,
            parent_run_state=parent_run_state,
            reconciliation_receipt=reconciliation_receipt,
            recorded_at_utc=adapter.orchestrator._utc_now(),
            error_message=error_message,
            error_signature=error_signature,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    if abort_path.exists() or abort_path.is_symlink():
        if abort_path.is_symlink() or not abort_path.is_file():
            raise adapter.orchestrator.ConfigurationError(f"staging ABORT 路径不可信：{abort_path}")
        try:
            existing = codex_upgrade_vc_artifacts.validate_staging_abort(
                adapter.orchestrator._read_json(abort_path, "staging ABORT")
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise adapter.orchestrator.ConfigurationError(f"既有 staging ABORT 无法校验：{error}") from error
        # 第 29 项：error_message 是诊断附注，不参与事实核对（既有无消息的 ABORT 重新对账不漂移）。
        volatile = {"recorded_at_utc", "receipt_sha256", "error_message"}
        drifted = sorted(
            field
            for field in abort
            if field not in volatile and existing.get(field) != abort[field]
        )
        if drifted:
            raise adapter.orchestrator.ConfigurationError(
                "既有 staging ABORT 与当前事实不一致，拒绝覆盖或复用；漂移字段："
                + "、".join(drifted)
            )
        return existing
    adapter.orchestrator._secure_write_json_once(abort_path, abort)
    return abort


def _staging_abort_accounted(campaign_dir: Path, sequence: int, attempt: int) -> bool:
    """态 C 判据：无父 run 的 staging 中止已推入项目总账（operation 在 head.operations 内）。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
    if root is None:
        return False
    head = codex_upgrade_project_ledger.replay_head(root)
    return reconciler.staging_abort_operation_id(sequence, attempt) in head.get("operations", {})


def _reconcile_staging_abort_receipt(
    campaign_dir: Path,
    attempt_dir: Path,
    *,
    sequence: int,
    phase: str,
    attempt: int,
) -> dict[str, Any]:
    """态 B→C→判定：对已写 ABORT 的无父 run attempt 完成 outbox → 总账 → 判定（各步幂等）。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    abort_path = attempt_dir / STAGING_ABORT_FILENAME
    try:
        abort = codex_upgrade_vc_artifacts.validate_staging_abort(
            adapter.orchestrator._read_json(abort_path, "staging ABORT")
        )
        result = reconciler.reconcile_staging_abort(campaign_dir, abort_path)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(f"staging ABORT 无法校验：{error}") from error
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(f"staging 中止对账失败：{error}") from error
    outcome = {
        "kind": "staging-attempt",
        "sequence": sequence,
        "phase": phase,
        "staging_attempt": attempt,
        "stage": abort["stage"],
        "abort_sha256": abort["receipt_sha256"],
        "decision": result["decision"]["decision"],
        "terminal_reason": result["decision"].get("terminal_reason"),
        "root_cause_id": abort["root_cause_id"],
        "root_cause_count": result["project_head"]["root_cause_count"],
    }
    if result["status"] == reconciler.DECISION_PAUSED:
        # 预算到期、账务未决、环境污染或同根因达上限（第三批 B3-9）只是暂停（对账已登记暂停事实），不是停线：
        # 按暂停种类处理后按同序号重新派发即从原 checkpoint 续接。
        raise adapter.orchestrator.StagingDeadlinePaused(
            f"staging attempt {sequence:04d}-{phase.lower()}/attempt-{attempt} 对账判定暂停："
            + reconciler._paused_next_command(result["decision"], "按同序号重新派发")
        )
    if result["status"] != reconciler.DECISION_RECOVERABLE:
        raise adapter.orchestrator.StagingStopTheLine(
            f"staging attempt {sequence:04d}-{phase.lower()}/attempt-{attempt} 对账命中永久停线："
            f"{result['decision']['terminal_reason']}；{'；'.join(result['decision']['reasons'])}"
        )
    return outcome


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
    """P1：没有父 run 的 staging attempt → ABORT → outbox → 总账 → 判定（态 A→B→C）。

    attempt 目录不存在（编译在落盘前失败）时没有事实可登记，返回 ``None``。
    判定命中永久停线时抛 ``StagingStopTheLine``。

    修好接着跑第 32 项：带原始异常的失败同时给出 ``error_message`` 与 ``error_signature``，根因按
    ``staging.attempt-failed``（异常类型＋归一化拒因签名）编码，同一步骤的不同拒因互不累计；孤儿扫描发现的
    遗弃 attempt 没有异常原文，两者都不给，仍记 ``staging.abandoned``。
    """

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    if not attempt_dir.exists():
        return None
    campaign_id = str(plan["campaign_id"])
    # ABORT 只记前 128 个字符的类型名；根因用同一截断值，对账器从收据复算才能得到同一 ID。
    recorded_error_type = error_type[:128]
    try:
        cause = reconciler.staging_abort_root_cause(
            phase,
            stage,
            error_type=recorded_error_type if error_signature is not None else None,
            error_signature=error_signature,
        )
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    batch_sha256, manifest_sha256 = adapter.orchestrator._staging_attempt_sha256s(attempt_dir)
    adapter.orchestrator._write_staging_abort(
        attempt_dir,
        campaign_id=campaign_id,
        campaign_plan_sha256=str(plan["plan_sha256"]),
        phase=phase,
        sequence=sequence,
        attempt=attempt,
        stage=stage,
        failure_kind=failure_kind,
        error_type=recorded_error_type,
        root_cause_id=str(cause["root_cause_id"]),
        batch_sha256=batch_sha256,
        manifest_sha256=manifest_sha256,
        parent_run_dir=None,
        parent_run_state=None,
        reconciliation_receipt=None,
        error_message=error_message,
        error_signature=error_signature,
    )
    return adapter.orchestrator._reconcile_staging_abort_receipt(
        campaign_dir, attempt_dir, sequence=sequence, phase=phase, attempt=attempt
    )


def _supervisor_run_reconciled(campaign_dir: Path, run_dir: Path) -> bool:
    """态 E 判据：Campaign 侧收据、outbox COMMIT、总账 operation 与账本事件四者齐全。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    run_id = run_dir.name
    receipt_path = (
        campaign_dir
        / "control"
        / reconciler.RECONCILIATION_DIR
        / f"run-{run_id}"
        / reconciler.SUPERVISOR_RUN_RECEIPT_NAME
    )
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    operation_id = f"reconcile-supervisor-run:{run_id}"
    ledger_dir = campaign_dir / codex_upgrade_project_ledger.CAMPAIGN_LEDGER_DIR_NAME
    outbox = ledger_dir / "outbox"
    committed = False
    if outbox.is_dir():
        for _index, batch_dir in codex_upgrade_project_ledger._batch_dirs(outbox):
            batch = codex_upgrade_project_ledger._read_batch(batch_dir)
            if batch["committed"] and batch["commit"]["operation_id"] == operation_id:
                committed = True
                break
    if not committed:
        return False
    root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
    if root is None:
        return False
    head = codex_upgrade_project_ledger.replay_head(root)
    if operation_id not in head.get("operations", {}):
        return False
    receipt = adapter.orchestrator._read_json(receipt_path, "父 run 对账收据")
    ledger_facts = receipt.get("campaign_ledger")
    active_phase = ledger_facts.get("active_phase") if isinstance(ledger_facts, Mapping) else None
    if active_phase is None:
        # 对账时阶段未开始（P2 在账本事件前崩溃）：reconciler 不写 receipt_passed。
        return True
    campaign = adapter.orchestrator._read_json(campaign_dir / "campaign.json", "Campaign 清单")
    timing_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, campaign)
    for event, _raw in codex_upgrade_timing_ledger._load_events(timing_dir):
        if event.get("event_id") == f"reconcile-run-passed-{run_id}":
            return True
    # 永久停线分支不写 receipt_passed，而是 stop_the_line；账本已 stopped 也视为闭合。
    return codex_upgrade_timing_ledger.inspect_ledger(timing_dir).get("status") == "stopped"


def _archive_uncommitted_formal_artifacts(
    campaign_dir: Path,
    *,
    sequence: int,
    phase: str,
    attempt: int,
) -> list[str]:
    """态 G：把无 COMMIT 的正式半产物移到 staging-aborts/NNNN-vc-x/attempt-K/（只移动）。"""

    adapter = _adapter()
    name = f"{sequence:04d}-{phase.lower()}.json"
    vc_root = campaign_dir / "control" / "vc"
    commit_path = adapter.orchestrator._staging_commit_path(campaign_dir, sequence, phase)
    if commit_path.exists():
        return []
    archive_dir = vc_root / "staging-aborts" / f"{sequence:04d}-{phase.lower()}" / f"attempt-{attempt}"
    moved: list[str] = []
    for source_dir, target_name in (
        (vc_root / "batches", STAGING_BATCH_FILENAME),
        (vc_root / "run-manifests", STAGING_MANIFEST_FILENAME),
    ):
        source = source_dir / name
        if source.is_symlink():
            raise adapter.orchestrator.ConfigurationError(f"正式半产物不得是符号链接：{source}")
        if not source.is_file():
            continue
        ensure_private_directory(vc_root / "staging-aborts", campaign_dir)
        ensure_private_directory(archive_dir.parent, campaign_dir)
        ensure_private_directory(archive_dir, campaign_dir)
        target = archive_dir / target_name
        if target.exists():
            if adapter.orchestrator.file_sha256(target) == adapter.orchestrator.file_sha256(source):
                source.unlink()
                moved.append(target_name)
                continue
            raise adapter.orchestrator.ConfigurationError(f"归档目标已存在且内容不同：{target}")
        os.rename(source, target)
        moved.append(target_name)
    return moved


def _formal_artifact_attempt(
    campaign_dir: Path,
    *,
    sequence: int,
    phase: str,
) -> int | None:
    """按 batch_sha256 找到无 COMMIT 正式半产物所属的 staging attempt。"""

    adapter = _adapter()
    name = f"{sequence:04d}-{phase.lower()}.json"
    batch_path = campaign_dir / "control" / "vc" / "batches" / name
    manifest_path = campaign_dir / "control" / "vc" / "run-manifests" / name
    digests: set[str] = set()
    if batch_path.is_file() and not batch_path.is_symlink():
        payload = adapter.orchestrator._read_json(batch_path, "正式半产物 batch")
        if isinstance(payload.get("batch_sha256"), str):
            digests.add(str(payload["batch_sha256"]))
    if manifest_path.is_file() and not manifest_path.is_symlink():
        payload = adapter.orchestrator._read_json(manifest_path, "正式半产物清单")
        if isinstance(payload.get("batch_sha256"), str):
            digests.add(str(payload["batch_sha256"]))
    if not digests:
        return None
    if len(digests) != 1:
        raise adapter.orchestrator.ConfigurationError(f"序号 {sequence:04d} 的正式半产物 batch 摘要互相矛盾，需人工审计。")
    digest = next(iter(digests))
    for attempt, attempt_dir in adapter.orchestrator._staging_attempt_dirs(adapter.orchestrator._staging_sequence_dir(campaign_dir, sequence, phase)):
        batch_sha256, _manifest_sha256 = adapter.orchestrator._staging_attempt_sha256s(attempt_dir)
        if batch_sha256 == digest:
            return attempt
    raise adapter.orchestrator.ConfigurationError(
        f"序号 {sequence:04d} 的正式半产物找不到对应的 staging attempt，需人工审计。"
    )


def _reconcile_prepared_parent_run(
    campaign_dir: Path,
    run_dir: Path,
    state: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """态 E→F：对一个无 COMMIT 的终态父 run 完成正式对账并写 staging ABORT（绑定收据）。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    binding = state["staging_binding"]
    sequence = int(binding["sequence"])
    phase = str(binding["phase"])
    attempt = int(binding["staging_attempt"])
    attempt_dir = adapter.orchestrator._staging_sequence_dir(campaign_dir, sequence, phase) / f"attempt-{attempt}"
    already = adapter.orchestrator._supervisor_run_reconciled(campaign_dir, run_dir)
    if already:
        # 态 E 已闭合：只读既有收据取根因与绑定，不重复对账（判定结果由总账终态与 admission 体现）。
        receipt_path = (
            campaign_dir
            / "control"
            / reconciler.RECONCILIATION_DIR
            / f"run-{run_dir.name}"
            / reconciler.SUPERVISOR_RUN_RECEIPT_NAME
        )
        receipt = adapter.orchestrator._read_json(receipt_path, "父 run 对账收据")
        receipt_binding = {
            "path": receipt_path.resolve(strict=True)
            .relative_to(campaign_dir.resolve(strict=True))
            .as_posix(),
            "sha256": adapter.orchestrator.file_sha256(receipt_path),
        }
        decision = "closed"
        terminal_reason = None
        pause_kinds = None
        root_cause_id = str(receipt["root_cause"]["root_cause_id"])
    else:
        try:
            result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
        except reconciler.ReconcilerError as error:
            raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 对账失败：{error}") from error
        receipt_binding = result["reconciliation_receipt"]
        decision = str(result["decision"]["decision"])
        terminal_reason = result["decision"].get("terminal_reason")
        # 第三批 B3-9：暂停种类随判定带出，入口按种类给下一步（同根因达上限不再是"预算到期"）。
        pause_kinds = result["decision"].get("pause_kinds")
        root_cause_id = str(result["root_cause"]["root_cause_id"])
    stop_reason = adapter.orchestrator._read_stop_reason(run_dir) or ""
    if stop_reason == codex_upgrade_supervisor.PREPARED_ABANDONED_REASON:
        stage, failure_kind = "parent-run", "abandoned"
    elif stop_reason.startswith(codex_upgrade_supervisor.STAGING_COMMIT_FAILED_PREFIX):
        stage = stop_reason[len(codex_upgrade_supervisor.STAGING_COMMIT_FAILED_PREFIX) :]
        failure_kind = "commit-failed"
    else:
        stage, failure_kind = "parent-run", "interrupted"
    batch_sha256, manifest_sha256 = adapter.orchestrator._staging_attempt_sha256s(attempt_dir)
    abort_written = False
    if attempt_dir.is_dir():
        abort_path = attempt_dir / STAGING_ABORT_FILENAME
        abort_written = not abort_path.exists()
        adapter.orchestrator._write_staging_abort(
            attempt_dir,
            campaign_id=str(plan["campaign_id"]),
            campaign_plan_sha256=str(plan["plan_sha256"]),
            phase=phase,
            sequence=sequence,
            attempt=attempt,
            stage=stage,
            failure_kind=failure_kind,
            error_type=stop_reason or "unknown",
            root_cause_id=root_cause_id,
            batch_sha256=batch_sha256,
            manifest_sha256=manifest_sha256,
            parent_run_dir=str(run_dir),
            parent_run_state=str(state["state"]),
            reconciliation_receipt={
                "path": str(receipt_binding["path"]),
                "sha256": str(receipt_binding["sha256"]),
            },
        )
    return {
        "kind": "parent-run",
        "run_dir": str(run_dir),
        "run_state": state["state"],
        "stop_reason": stop_reason,
        "sequence": sequence,
        "phase": phase,
        "staging_attempt": attempt,
        "already_reconciled": already,
        "abort_written": abort_written,
        "decision": decision,
        "terminal_reason": terminal_reason,
        "pause_kinds": pause_kinds,
        "root_cause_id": root_cause_id,
        "reconciliation_receipt": dict(receipt_binding),
    }


def _reconcile_staging_orphans(
    campaign_dir: Path,
    state_dir: Path,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """派发入口锁内、prepare 前的孤儿处理：按可观测状态逐项幂等续接到闭合。

    对象与顺序：① 同 Campaign 的 staging 父 run（prepared／committed 未启动的先终态化；
    无 COMMIT 的终态 run 走对账 → ABORT；COMMIT 完整性异常走永久停线；
    ``failed(parent-start-failed)`` 是 P4，留给 reconcile-supervisor-run，入口不处理）；
    ② 没有父 run 的 staging attempt（ABORT → outbox → 总账 → 判定）；③ 无 COMMIT 的
    正式半产物归档。任一对象命中永久停线即在处理完本对象后抛 ``StagingStopTheLine``。
    """

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    campaign_id = str(plan["campaign_id"])
    handled: list[dict[str, Any]] = []
    stops: list[str] = []
    # 预算到期暂停的对象：不是停线，全部处理完后提示先批准延期（对账已登记暂停事实）。
    paused: list[str] = []
    changed = False
    referenced_attempts: set[tuple[int, str, int]] = set()
    supervisor = codex_upgrade_supervisor
    # ① 父 run
    for state_path in sorted(state_dir.glob("run-*/state.json")):
        run_dir = state_path.parent
        try:
            state = supervisor._read_state(run_dir)
        except supervisor.SupervisorError:
            continue
        if state.get("campaign_id") != campaign_id:
            continue
        binding = state.get("staging_binding")
        if not isinstance(binding, Mapping):
            continue
        referenced_attempts.add(
            (int(binding["sequence"]), str(binding["phase"]), int(binding["staging_attempt"]))
        )
        if state.get("state") in supervisor.ACTIVE_STATES:
            if supervisor._owner_alive(int(state["owner_pid"])):
                raise adapter.orchestrator.ConfigurationError(
                    f"父 run {run_dir.name} 的 owner 仍在线，拒绝并发处理。"
                )
            if state.get("state") == "running" and not supervisor.committed_run_never_started(
                run_dir, state
            ):
                raise adapter.orchestrator.ConfigurationError(
                    f"父 run {run_dir.name} 已开始动作但 owner 丢失，先执行 reconcile-supervisor-run。"
                )
            state = adapter.orchestrator._wait_for_monitor_finalization(run_dir, state)
            if state.get("state") in supervisor.ACTIVE_STATES:
                try:
                    supervisor.finalize_prepared_run(run_dir, operation="orchestrator:orphan-scan")
                except supervisor.SupervisorError as error:
                    raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 终态化失败：{error}") from error
                state = supervisor._read_state(run_dir)
            changed = True
        run_state = str(state.get("state"))
        stop_reason = adapter.orchestrator._read_stop_reason(run_dir)
        classification = supervisor.classify_prepared_run(run_dir, state)
        if classification == "integrity_mismatch" or (
            run_state == "audit-incomplete"
            and stop_reason == supervisor.COMMIT_INTEGRITY_MISMATCH_REASON
        ):
            # 态 I：固定永久停线（reconciler 内部 stage_abandoned + stop_the_line + campaign_terminal）。
            if run_state in supervisor.ACTIVE_STATES:
                raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} COMMIT 完整性异常且未终态化。")
            try:
                result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 完整性异常对账失败：{error}") from error
            handled.append(
                {
                    "kind": "parent-run",
                    "run_dir": str(run_dir),
                    "run_state": run_state,
                    "classification": "integrity_mismatch",
                    "decision": result["decision"]["decision"],
                    "terminal_reason": result["decision"].get("terminal_reason"),
                }
            )
            changed = True
            stops.append(
                f"父 run {run_dir.name} 的 COMMIT 完整性异常，序号 {int(binding['sequence']):04d} 已占且不可重派"
            )
            continue
        if classification == "committed":
            # 正常历史或 P4（failed + parent-start-failed）：P4 交 reconcile-supervisor-run，入口不处理。
            continue
        if run_state == "stopped":
            raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 无 COMMIT 却已 stopped，历史不可信。")
        if run_state not in supervisor.TERMINAL_STATES:
            raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 未终态化。")
        outcome = adapter.orchestrator._reconcile_prepared_parent_run(campaign_dir, run_dir, state, plan=plan)
        if outcome["already_reconciled"] and not outcome["abort_written"]:
            # 完全闭合的历史孤儿：不重复登记。
            continue
        handled.append(outcome)
        changed = True
        if outcome["decision"] == reconciler.DECISION_PAUSED:
            paused.append(f"父 run {run_dir.name} 对账判定暂停（预算到期、账务未决、环境污染或同根因达上限）")
        elif outcome["decision"] not in {reconciler.DECISION_RECOVERABLE, "closed"}:
            stops.append(
                f"父 run {run_dir.name} 对账命中永久停线：{outcome['terminal_reason']}"
            )
    # ② 没有父 run 的 staging attempt
    staging_root = campaign_dir / "control" / "vc" / "staging"
    if staging_root.is_symlink():
        raise adapter.orchestrator.ConfigurationError("staging 根目录不得是符号链接。")
    if staging_root.is_dir():
        for sequence_dir in sorted(staging_root.iterdir()):
            match = re.fullmatch(r"^([0-9]{4})-(vc-[0-6])$", sequence_dir.name)
            if match is None or sequence_dir.is_symlink() or not sequence_dir.is_dir():
                raise adapter.orchestrator.ConfigurationError(f"staging 根目录含非法条目：{sequence_dir.name}")
            sequence = int(match.group(1))
            phase = match.group(2).upper()
            commit_path = adapter.orchestrator._staging_commit_path(campaign_dir, sequence, phase)
            committed_attempt: int | None = None
            if commit_path.exists():
                committed_attempt = int(adapter.orchestrator._read_staging_commit(commit_path)["staging_attempt"])
            for attempt, attempt_dir in adapter.orchestrator._staging_attempt_dirs(sequence_dir):
                if committed_attempt == attempt:
                    continue
                if (sequence, phase, attempt) in referenced_attempts:
                    # 有父 run 引用：已在 ① 处理（或属于 P4，不写 ABORT）。
                    continue
                if (attempt_dir / STAGING_ABORT_FILENAME).exists():
                    # 态 B／C：ABORT 已写但 outbox／总账尚未闭合（写 ABORT 后崩溃）。
                    if adapter.orchestrator._staging_abort_accounted(campaign_dir, sequence, attempt):
                        continue
                    outcome = adapter.orchestrator._reconcile_staging_abort_receipt(
                        campaign_dir, attempt_dir, sequence=sequence, phase=phase, attempt=attempt
                    )
                    changed = True
                    handled.append(outcome)
                    continue
                outcome = adapter.orchestrator._abort_staging_attempt_without_parent(
                    campaign_dir,
                    attempt_dir,
                    plan=plan,
                    sequence=sequence,
                    phase=phase,
                    attempt=attempt,
                    stage="prepare",
                    failure_kind="abandoned",
                    error_type="StagingAttemptAbandoned",
                )
                changed = True
                if outcome is not None:
                    handled.append(outcome)
    # ③ 无 COMMIT 的正式半产物归档
    vc_root = campaign_dir / "control" / "vc"
    for source_dir in (vc_root / "batches", vc_root / "run-manifests"):
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.iterdir()):
            match = adapter.orchestrator._VC_SEQUENCE_FILE_RE.fullmatch(path.name)
            if match is None:
                continue
            sequence = int(match.group(1))
            if sequence == 1:
                continue
            phase = match.group(2).upper()
            if adapter.orchestrator._staging_commit_path(campaign_dir, sequence, phase).exists():
                continue
            attempt = adapter.orchestrator._formal_artifact_attempt(campaign_dir, sequence=sequence, phase=phase)
            if attempt is None:
                continue
            moved = adapter.orchestrator._archive_uncommitted_formal_artifacts(
                campaign_dir, sequence=sequence, phase=phase, attempt=attempt
            )
            if moved:
                changed = True
                handled.append(
                    {
                        "kind": "formal-artifacts-archived",
                        "sequence": sequence,
                        "phase": phase,
                        "staging_attempt": attempt,
                        "moved": moved,
                    }
                )
    if stops:
        raise adapter.orchestrator.StagingStopTheLine("；".join(stops))
    if paused:
        raise adapter.orchestrator.StagingDeadlinePaused(
            "；".join(paused) + "：deadline-extend preview/apply 批准延期后，按同序号重新派发。"
        )
    return {"changed": changed, "handled": handled}


def _compile_and_run_vc_batch_staging(
    arguments: argparse.Namespace,
    *,
    governance: dict[str, Any],
    _compiler: Any | None,
) -> tuple[dict[str, Any], int]:
    """staging 批次模型的原子派发：孤儿对账 → prepare → prepared 父 run → commit 四步。"""

    adapter = _adapter()
    supervisor = codex_upgrade_supervisor
    campaign_dir = Path(arguments.campaign_dir)
    phase = str(arguments.phase)
    sequence = int(arguments.sequence)
    formal = _compiler is None
    campaign = adapter.orchestrator._read_json(campaign_dir / "campaign.json", "Campaign 清单")
    plan = adapter.orchestrator._vc_campaign_plan(campaign_dir, campaign)
    if not adapter.orchestrator._campaign_uses_staging_model(campaign_dir, plan):
        raise adapter.orchestrator.ConfigurationError("Campaign 总计划不是 staging 批次模型。")
    campaign_id = str(plan["campaign_id"])
    if campaign.get("campaign_id") not in {None, campaign_id}:
        raise adapter.orchestrator.ConfigurationError("Campaign 清单与总计划的 campaign_id 不一致。")
    name = f"{sequence:04d}-{phase.lower()}.json"
    batch_path = campaign_dir / "control" / "vc" / "batches" / name
    manifest_path = campaign_dir / "control" / "vc" / "run-manifests" / name
    commit_path = adapter.orchestrator._staging_commit_path(campaign_dir, sequence, phase)
    try:
        lock_descriptor, state_dir = supervisor._campaign_run_lock(arguments.state_dir)
    except supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    run_arguments = adapter.orchestrator._staging_run_arguments(arguments)
    bootstrap_run: dict[str, Any] | None = None
    try:
        if formal:
            bootstrap_run = adapter.orchestrator._bootstrap_noop_first_batch(
                campaign_dir,
                governance["manifest"],
                sequence=sequence,
                state_dir=state_dir,
                run_arguments=run_arguments,
            )
        orphans = adapter.orchestrator._reconcile_staging_orphans(campaign_dir, state_dir, plan=plan)
        if formal and orphans["changed"]:
            # 孤儿处理推进了总账／账本：重算只读预检与 admission，头序号以此为准。
            governance = adapter.orchestrator._prepare_atomic_batch_governance(arguments)
        if commit_path.exists():
            raise adapter.orchestrator.ConfigurationError(
                f"序号 {sequence:04d} 已有 COMMIT，禁止重复派发；后继请用序号 {sequence + 1:04d}。"
            )
        owner_nonce = secrets.token_hex(32)
        sequence_dir = adapter.orchestrator._staging_sequence_dir(campaign_dir, sequence, phase)
        attempt = adapter.orchestrator._next_staging_attempt(sequence_dir)
        attempt_dir = sequence_dir / f"attempt-{attempt}"
        compiler = adapter.orchestrator.compile_vc_batch if _compiler is None else _compiler
        try:
            compiled = compiler(
                arguments, staging_attempt_dir=attempt_dir, owner_nonce=owner_nonce
            )
            prepared = adapter.orchestrator._load_prepared_staging_attempt(
                campaign_dir,
                attempt_dir,
                owner_nonce=owner_nonce,
                sequence=sequence,
                phase=phase,
            )
            start_by = datetime.fromisoformat(
                str(prepared["batch"]["must_start_by_utc"]).replace("Z", "+00:00")
            )
            if datetime.now(timezone.utc) > start_by:
                raise adapter.orchestrator.ConfigurationError("VC batch 的 60 秒启动窗口已过期，拒绝创建父 run。")
        except BaseException as error:
            failure_kind = (
                "interrupted"
                if isinstance(error, (KeyboardInterrupt, SystemExit))
                else "prepare-failed"
            )
            try:
                adapter.orchestrator._abort_staging_attempt_without_parent(
                    campaign_dir,
                    attempt_dir,
                    plan=plan,
                    sequence=sequence,
                    phase=phase,
                    attempt=attempt,
                    stage="prepare",
                    failure_kind=failure_kind,
                    error_type=type(error).__name__,
                    error_message=codex_upgrade_vc_artifacts.staging_abort_error_message(error),
                    error_signature=codex_upgrade_vc_artifacts.staging_abort_error_signature(error),
                )
            except adapter.orchestrator.StagingStopTheLine as stop_error:
                raise adapter.orchestrator.StagingStopTheLine(
                    f"prepare 失败（{adapter.orchestrator._staging_error_summary(error)}）且对账命中永久停线：{stop_error}"
                ) from error
            except adapter.orchestrator.StagingDeadlinePaused as paused_error:
                raise adapter.orchestrator.StagingDeadlinePaused(
                    f"prepare 失败（{adapter.orchestrator._staging_error_summary(error)}）；{paused_error}"
                ) from error
            raise
        staging_binding = {
            "campaign_dir": str(campaign_dir.resolve(strict=True)),
            "sequence": sequence,
            "phase": phase,
            "staging_attempt": attempt,
            "commit_path": str(commit_path.resolve(strict=False)),
            "prepared_marker_sha256": str(prepared["marker"]["marker_sha256"]),
        }
        commit_facts: dict[str, Any] = {"ledger_events": []}

        def _commit_staging(client: Any) -> None:
            client.begin_commit_step("nonce-mismatch")
            if not (prepared["marker"]["owner_nonce"] == client.owner_nonce == owner_nonce):
                raise supervisor.StagingCommitError(
                    "nonce-mismatch", "PREPARED、父 run 与入口预分配的 owner_nonce 不一致。"
                )
            # 改造 5：正式 COMMIT 前核对当前 evaluator 四项摘要等于批次冻结值（编译器与这里
            # 调用同一纯函数）；不等即 aborted_prepared、序号不占、同序号以当前摘要重新编译。
            client.begin_commit_step("evaluator-digests")
            frozen_digests = prepared["run_manifest"].get("evaluator_digests")
            if frozen_digests is not None:
                try:
                    current_digests = adapter.identity_policy.evaluator_dependency_digests()
                except adapter.identity_policy.ToolIdentityPolicyError as error:
                    raise supervisor.StagingCommitError("evaluator-digests", str(error)) from error
                if current_digests != dict(frozen_digests):
                    drifted = sorted(
                        field for field in current_digests if current_digests[field] != frozen_digests.get(field)
                    )
                    raise supervisor.StagingCommitError(
                        "evaluator-digests",
                        "当前 evaluator 依赖摘要与批次冻结值不一致（编译后工具已变化），"
                        f"漂移项：{drifted}；请同序号重新编译派发。",
                    )
            client.begin_commit_step("commit-ledger")
            try:
                if governance["ledger_dir"] is not None:
                    commit_facts["ledger_events"] = adapter.orchestrator._append_timing_ledger_batch_events(
                        governance["ledger_dir"],
                        governance["ledger_events"],
                        phase=phase,
                        sequence=sequence,
                        expected_head=governance["ledger_head_sequence"],
                    )
            except adapter.orchestrator.ConfigurationError as error:
                raise supervisor.StagingCommitError("commit-ledger", str(error)) from error
            client.begin_commit_step("commit-publish")
            try:
                adapter.orchestrator._publish_staging_file(attempt_dir / STAGING_BATCH_FILENAME, batch_path, campaign_dir)
                adapter.orchestrator._publish_staging_file(
                    attempt_dir / STAGING_MANIFEST_FILENAME, manifest_path, campaign_dir
                )
            except adapter.orchestrator.ConfigurationError as error:
                raise supervisor.StagingCommitError("commit-publish", str(error)) from error
            client.begin_commit_step("commit-mark")
            try:
                commit = codex_upgrade_vc_artifacts.build_vc_commit(
                    campaign_id=campaign_id,
                    sequence=sequence,
                    phase=phase,
                    staging_attempt=attempt,
                    batch_sha256=str(prepared["batch"]["batch_sha256"]),
                    manifest_sha256=str(prepared["manifest_sha256"]),
                    parent_run_dir=str(client.run_dir),
                    owner_nonce=client.owner_nonce,
                    ledger_event_ids=[
                        str(item["event_id"]) for item in commit_facts["ledger_events"]
                    ],
                    committed_at_utc=adapter.orchestrator._utc_now(),
                )
                adapter.orchestrator._secure_write_json_once(commit_path, commit)
            except (adapter.orchestrator.ConfigurationError, codex_upgrade_vc_artifacts.VCArtifactError) as error:
                raise supervisor.StagingCommitError("commit-mark", str(error)) from error
            commit_facts["commit"] = commit
            client.begin_commit_step("commit-activate")
            client.activate_committed(commit)

        try:
            returncode, run = supervisor._campaign_run_locked(
                run_arguments,
                manifest=prepared["run_manifest"],
                state_dir=state_dir,
                campaign_dir=campaign_dir,
                commit=_commit_staging,
                owner_nonce=owner_nonce,
                staging_binding=staging_binding,
            )
        except BaseException as error:
            run_dir = state_dir / f"run-{owner_nonce}"
            if not run_dir.exists():
                failure_kind = (
                    "interrupted"
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else "prepare-failed"
                )
                try:
                    adapter.orchestrator._abort_staging_attempt_without_parent(
                        campaign_dir,
                        attempt_dir,
                        plan=plan,
                        sequence=sequence,
                        phase=phase,
                        attempt=attempt,
                        stage="parent-run-create",
                        failure_kind=failure_kind,
                        error_type=type(error).__name__,
                        error_message=codex_upgrade_vc_artifacts.staging_abort_error_message(error),
                        error_signature=codex_upgrade_vc_artifacts.staging_abort_error_signature(error),
                    )
                except adapter.orchestrator.StagingStopTheLine as stop_error:
                    raise adapter.orchestrator.StagingStopTheLine(
                        f"父 run 创建失败（{adapter.orchestrator._staging_error_summary(error)}）且对账命中永久停线：{stop_error}"
                    ) from error
                except adapter.orchestrator.StagingDeadlinePaused as paused_error:
                    raise adapter.orchestrator.StagingDeadlinePaused(
                        f"父 run 创建失败（{adapter.orchestrator._staging_error_summary(error)}）；{paused_error}"
                    ) from error
            # 父 run 已建立（含 commit 中被中断后已封存 aborted_prepared）：由下次入口的
            # 孤儿处理幂等续接对账、ABORT 与归档。
            if isinstance(error, supervisor.SupervisorError):
                raise adapter.orchestrator.ConfigurationError(str(error)) from error
            raise
        run_dir = Path(str(run["run_dir"]))
        run_status = str(run["status"])
        run_reason = str(run.get("reason", ""))
        if run_status == "aborted_prepared":
            # 固定顺序：① 正式对账（收据、outbox、总账、账本）→ ② ABORT（绑定收据）→ ③ 归档半产物。
            state = supervisor._read_state(run_dir)
            outcome = adapter.orchestrator._reconcile_prepared_parent_run(campaign_dir, run_dir, state, plan=plan)
            moved = adapter.orchestrator._archive_uncommitted_formal_artifacts(
                campaign_dir, sequence=sequence, phase=phase, attempt=attempt
            )
            commit_failure = run["commit_failure"]
            # 修好接着跑第 51 项：失败消息带监督器返回的异常原文（与第 29 项父 run 创建失败同口径，截 600 字）。
            original = str(commit_failure.get("error_message") or "")[:600]
            summary = (
                f"staging commit 在 {commit_failure['commit_step']} 步失败"
                f"（{commit_failure['error_type']}{'：' + original if original else ''}），父 run {run_dir.name} 已封存 aborted_prepared，"
                f"序号 {sequence:04d} 未占；对账根因 {outcome['root_cause_id']}"
            )
            if outcome["decision"] == "paused":
                # 第三批 B3-9：暂停种类可能是预算到期、账务未决、环境污染或同根因达上限，下一步按种类给出。
                from tools.official_client_capture import codex_upgrade_reconciler as paused_reconciler

                raise adapter.orchestrator.StagingDeadlinePaused(
                    f"{summary}；已归档 {moved or '无'}；对账判定暂停："
                    + paused_reconciler._paused_next_command(
                        {"pause_kinds": outcome.get("pause_kinds")}, "按同序号重新派发"
                    )
                )
            if outcome["decision"] not in {"recoverable", "closed"}:
                raise adapter.orchestrator.StagingStopTheLine(
                    f"{summary}；对账命中永久停线：{outcome['terminal_reason']}"
                )
            raise adapter.orchestrator.ConfigurationError(
                f"{summary}；已归档 {moved or '无'}，可按同序号重新派发。"
            )
        if run_status == "audit-incomplete" and run_reason == supervisor.COMMIT_INTEGRITY_MISMATCH_REASON:
            from tools.official_client_capture import codex_upgrade_reconciler as reconciler

            try:
                reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(
                    f"父 run {run_dir.name} COMMIT 完整性异常，且永久停线对账失败：{error}"
                ) from error
            raise adapter.orchestrator.StagingStopTheLine(
                f"父 run {run_dir.name} 的 COMMIT 完整性异常，序号 {sequence:04d} 已占且不可重派，已永久停线。"
            )
        completion = None
        if returncode == 0 and governance["ledger_dir"] is not None:
            completion = adapter.orchestrator._complete_timing_ledger_phase_after_batch(
                campaign_dir,
                governance["plan"],
                phase=phase,
                sequence=sequence,
                ledger_dir=governance["ledger_dir"],
            )
        return (
            {
                "status": run_status,
                "campaign_id": compiled["campaign_id"],
                "phase": compiled["phase"],
                "batch_sequence": compiled["batch_sequence"],
                "batch_model": "staging",
                "staging_attempt": attempt,
                "commit": commit_facts.get("commit"),
                "compile": compiled,
                "campaign_run": run,
                "predispatch_stop": None,
                "orphans": orphans["handled"],
                "bootstrap_noop_run": bootstrap_run,
                "project_ledger": governance["admission"],
                "timing_ledger": {
                    "ledger_dir": (
                        str(governance["ledger_dir"]) if governance["ledger_dir"] is not None else None
                    ),
                    "events": commit_facts["ledger_events"],
                    "completion": completion,
                },
            },
            returncode,
        )
    finally:
        os.close(lock_descriptor)


def _assert_revision_admission(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    command: str,
) -> dict[str, Any] | None:
    """与 plan／reuse-official-evidence 同集合的 admission，外加总计划 deadline。"""

    adapter = _adapter()
    try:
        admission = codex_upgrade_project_ledger.assert_campaign_admitted(
            campaign_dir,
            command=command,
            require=adapter.orchestrator._project_ledger_required(manifest.get("campaign_mode"), manifest.get("target_version")),
        )
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"项目总账拒绝 {command}：{error}") from error
    deadline = adapter.orchestrator._campaign_plan_deadline(campaign_dir)
    if deadline is not None:
        expiry = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= expiry:
            raise adapter.orchestrator.ConfigurationError(f"{command} 拒绝：Campaign 总计划 deadline 已到。")
    return admission


def _ledger_last_event_of_type(ledger_dir: Path, event_type: str) -> tuple[dict[str, Any], str] | None:
    """账本最后一条指定类型事件及其原始字节摘要。"""

    adapter = _adapter()
    last: tuple[dict[str, Any], str] | None = None
    try:
        for event, raw in codex_upgrade_timing_ledger._load_events(ledger_dir):
            if event.get("event_type") == event_type:
                last = (dict(event), codex_upgrade_timing_ledger._sha256_bytes(raw))
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"UpgradeTimingLedger 无法读取：{error}") from error
    return last


def _write_candidate_revision_pending(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    revision: int,
    candidate_id: str,
    supersedes: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """写 revision 目录的 revision.json 与 COMMIT（pending）；已存在时内容核对。"""

    adapter = _adapter()
    plan = adapter.orchestrator._vc_campaign_plan(campaign_dir, manifest)
    vc3_path, vc3 = adapter.orchestrator._replay_vc_checkpoint(campaign_dir, plan, "VC-3")
    vc3_binding = vc3.get("stage_receipt")
    if not isinstance(vc3_binding, Mapping):
        raise adapter.orchestrator.ConfigurationError("VC-3 checkpoint 阶段收据绑定非法。")
    previous_sha256: str | None = None
    if revision >= 2:
        previous_record, previous_commit = adapter.orchestrator._read_candidate_revision_record(campaign_dir, revision - 1)
        if previous_record is None or previous_commit is None:
            raise adapter.orchestrator.ConfigurationError(f"候选 revision r{revision - 1} 记录或 COMMIT 缺失，无法登记 r{revision}。")
        previous_sha256 = str(previous_record["record_sha256"])
    try:
        record = codex_upgrade_vc_artifacts.build_candidate_revision(
            campaign_id=str(plan["campaign_id"]),
            revision=revision,
            candidate_id=candidate_id,
            opened_at_utc=adapter.orchestrator._utc_now(),
            previous_revision_sha256=previous_sha256,
            vc3_checkpoint=adapter.orchestrator._vc_checkpoint_reference(campaign_dir, vc3_path, vc3),
            vc3_stage_receipt={"path": str(vc3_binding["path"]), "sha256": str(vc3_binding["sha256"])},
            supersedes=supersedes,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(f"候选 revision 记录无法生成：{error}") from error
    directory = adapter.orchestrator._candidate_revision_dir(campaign_dir, revision)
    ensure_private_directory(adapter.orchestrator._candidate_revisions_root(campaign_dir), campaign_dir)
    ensure_private_directory(directory, campaign_dir)
    record_path = directory / "revision.json"
    try:
        adapter.orchestrator._write_or_verify_json_ignoring(record_path, record, volatile=("opened_at_utc", "record_sha256"))
    except adapter.orchestrator.ConfigurationError as error:
        raise adapter.orchestrator.ConfigurationError(
            f"候选 revision r{revision} 已有登记且内容不一致（候选、VC-3 绑定或取代关系漂移），拒绝覆盖：{error}"
        ) from error
    try:
        record = codex_upgrade_vc_artifacts.validate_candidate_revision(
            adapter.orchestrator._read_json(record_path, "候选 revision 记录")
        )
        commit = codex_upgrade_vc_artifacts.build_candidate_revision_commit(
            campaign_id=str(plan["campaign_id"]),
            revision=revision,
            candidate_id=candidate_id,
            record_sha256=str(record["record_sha256"]),
            committed_at_utc=adapter.orchestrator._utc_now(),
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    adapter.orchestrator._write_or_verify_json_ignoring(directory / "COMMIT", commit, volatile=("committed_at_utc", "commit_sha256"))
    commit = codex_upgrade_vc_artifacts.validate_candidate_revision_commit(
        adapter.orchestrator._read_json(directory / "COMMIT", "候选 revision COMMIT")
    )
    return record, commit


def open_candidate_revision(arguments: argparse.Namespace) -> dict[str, Any]:
    """revision-open：登记并激活一个候选级 revision（零请求，幂等）。

    ``--initial``：新 Campaign 在 VC-4 首批前建立 r1（导入型后继 Campaign 的 r1 目录已由
    successor 在暂存区写成 pending，这里只补账本 stage_revision）；``--supersedes <old>``：
    账本处于 revision_required 且旧候选已 candidate_invalidated 时开 r(N+1)。
    admission → 绑定 VC-3 → revision.json + COMMIT（pending）→ 账本 stage_revision（active）。
    """

    adapter = _adapter()
    campaign_dir = Path(arguments.campaign_dir)
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest):
        raise adapter.orchestrator.ConfigurationError("revision-open 只用于 0.154.0 起的完整 VC 链 Campaign。")
    candidate_id = str(arguments.candidate_id)
    if not adapter.orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise adapter.orchestrator.ConfigurationError("--candidate-id 格式非法。")
    initial = bool(getattr(arguments, "initial", False))
    supersedes_id = getattr(arguments, "supersedes", None)
    if initial == bool(supersedes_id):
        raise adapter.orchestrator.ConfigurationError("revision-open 必须且只能给出 --initial 或 --supersedes 之一。")
    admission = adapter.orchestrator._assert_revision_admission(campaign_dir, manifest, command=REVISION_OPEN_COMMAND)
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    with adapter.orchestrator._campaign_lock(campaign_dir):
        summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        last_stage_revision = adapter.orchestrator._ledger_last_stage_revision(ledger_dir)
        supersedes: dict[str, Any] | None = None
        if initial:
            if last_stage_revision is not None:
                if int(last_stage_revision["revision"]) != 1 or last_stage_revision.get("candidate_id") != candidate_id:
                    raise adapter.orchestrator.ConfigurationError(
                        f"Campaign 已激活 revision r{last_stage_revision['revision']}"
                        f"（候选 {last_stage_revision.get('candidate_id')}），--initial 只能幂等重放 r1。"
                    )
            elif summary.get("current_revision") is not None:
                raise adapter.orchestrator.ConfigurationError(
                    "历史 Campaign 的候选级事件已隐含 r1，只读重放，不得再登记 revision。"
                )
            elif summary.get("status") != "active":
                raise adapter.orchestrator.ConfigurationError(
                    f"revision-open --initial 要求账本 active（当前 {summary.get('status')}）。"
                )
            legacy_vc4 = campaign_dir / "control" / "vc" / "vc-4-checkpoint.json"
            if legacy_vc4.exists() and not adapter.orchestrator._candidate_revisions_root(campaign_dir).exists():
                raise adapter.orchestrator.ConfigurationError("历史 Campaign 已有隐含 r1 的 VC-4 checkpoint，只读重放。")
            for other in adapter.orchestrator._existing_candidate_revisions(campaign_dir):
                if other != 1:
                    raise adapter.orchestrator.ConfigurationError(f"Campaign 已存在 r{other} 目录，--initial 只能登记 r1。")
            revision = 1
        else:
            supersedes_id = str(supersedes_id)
            if not adapter.orchestrator.SAFE_ID_RE.fullmatch(supersedes_id):
                raise adapter.orchestrator.ConfigurationError("--supersedes 格式非法。")
            if supersedes_id == candidate_id:
                raise adapter.orchestrator.ConfigurationError("新候选不得与被取代候选同名。")
            current = summary.get("current_revision")
            if current is None or last_stage_revision is None:
                raise adapter.orchestrator.ConfigurationError("没有可取代的 active revision；新 Campaign 请用 --initial。")
            already_active = (
                last_stage_revision.get("candidate_id") == candidate_id
                and last_stage_revision.get("supersedes_revision") is not None
            )
            if already_active:
                # 幂等重放：最后一条 stage_revision 已由本命令以同一候选激活，被取代的
                # revision 由事件自身给出（此时账本 current_revision 已是新值）。
                revision = int(last_stage_revision["revision"])
                current = int(last_stage_revision["supersedes_revision"])
            else:
                if summary.get("status") != "revision_required":
                    raise adapter.orchestrator.ConfigurationError(
                        f"revision-open --supersedes 要求账本 revision_required（当前 {summary.get('status')}）；"
                        "先完成对账并执行 invalidate-candidate。"
                    )
                revision = int(current) + 1
            next_revision = revision
            existing_next = adapter.orchestrator._read_candidate_revision_record(campaign_dir, next_revision)[0]
            invalidated = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, "candidate_invalidated")
            if (
                invalidated is None
                or invalidated[0].get("candidate_id") != supersedes_id
                or int(invalidated[0].get("revision") or 0) != int(current)
            ):
                raise adapter.orchestrator.ConfigurationError(
                    f"账本没有对候选 {supersedes_id}（r{current}）的 candidate_invalidated 事件。"
                )
            invalidation_path = campaign_dir / "candidates" / supersedes_id / adapter.orchestrator.CANDIDATE_INVALIDATION_FILENAME
            if invalidation_path.is_symlink() or not invalidation_path.is_file():
                raise adapter.orchestrator.ConfigurationError(f"被取代候选 {supersedes_id} 缺少 invalidation.json。")
            try:
                invalidation = codex_upgrade_vc_artifacts.validate_candidate_invalidation(
                    adapter.orchestrator._read_json(invalidation_path, "候选作废记录")
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(str(error)) from error
            if invalidation["candidate_id"] != supersedes_id or invalidation["revision"] != int(current):
                raise adapter.orchestrator.ConfigurationError("invalidation.json 与被取代候选／revision 不一致。")
            supersedes = {
                "revision": int(current),
                "candidate_id": supersedes_id,
                "invalidation_receipt": {
                    "path": invalidation_path.resolve(strict=True)
                    .relative_to(campaign_dir.resolve(strict=True))
                    .as_posix(),
                    "sha256": adapter.orchestrator.file_sha256(invalidation_path),
                },
                "candidate_invalidated_event_sha256": invalidated[1],
            }
            if existing_next is not None and existing_next.get("candidate_id") != candidate_id:
                raise adapter.orchestrator.ConfigurationError(
                    f"r{next_revision} 已登记候选 {existing_next.get('candidate_id')}，与 --candidate-id 不一致。"
                )
        record, commit = adapter.orchestrator._write_candidate_revision_pending(
            campaign_dir,
            manifest,
            revision=revision,
            candidate_id=candidate_id,
            supersedes=supersedes,
        )
        ledger_event: dict[str, Any]
        idempotent = False
        if (
            last_stage_revision is not None
            and int(last_stage_revision["revision"]) == revision
            and last_stage_revision.get("candidate_id") == candidate_id
        ):
            if last_stage_revision.get("revision_commit_sha256") != commit["commit_sha256"]:
                raise adapter.orchestrator.ConfigurationError(
                    f"账本已激活的 r{revision} 引用的 COMMIT 与目录 COMMIT 不一致。"
                )
            ledger_event = {"event_id": last_stage_revision["event_id"], "appended": False}
            idempotent = True
        else:
            try:
                result_summary = codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=f"stage-revision-r{revision}",
                    phase="VC-4",
                    event_type="stage_revision",
                    revision=revision,
                    candidate_id=candidate_id,
                    revision_commit_sha256=str(commit["commit_sha256"]),
                    supersedes_revision=(int(supersedes["revision"]) if supersedes is not None else None),
                    next_action=(
                        f"r{revision} 已激活：以 compile-and-run-vc-batch 派发 VC-4 首批"
                        "（plan-candidate-gates），然后 record-candidate-build（含 revision-seal）"
                    ),
                )
            except codex_upgrade_timing_ledger.TimingLedgerError as error:
                raise adapter.orchestrator.ConfigurationError(f"UpgradeTimingLedger 拒绝 stage_revision：{error}") from error
            ledger_event = {
                "event_id": f"stage-revision-r{revision}",
                "appended": True,
                "head_sequence": result_summary["head_sequence"],
                "head_sha256": result_summary["head_sha256"],
            }
    return {
        "status": "complete",
        "idempotent": idempotent,
        "campaign_id": manifest["campaign_id"],
        "revision": revision,
        "candidate_id": candidate_id,
        "supersedes": supersedes,
        "revision_record": str(adapter.orchestrator._candidate_revision_dir(campaign_dir, revision) / "revision.json"),
        "record_sha256": record["record_sha256"],
        "commit_sha256": commit["commit_sha256"],
        "ledger_event": ledger_event,
        "project_ledger": admission,
        "live_request_count": 0,
    }


def _candidate_accounting_checks(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    *,
    head: Mapping[str, Any],
    ledger_dir: Path,
) -> dict[str, Any]:
    """作废前提：候选已有的请求全部入账；失败父 run 与未 seal attempt 均已对账。

    attempt 对账与父 run 对账都完整重放收据 schema／身份／项目总账摘要绑定；引起
    ``candidate_review_required`` 的失败按 reservation 分流：父 run 期间发布过 reservation 的
    只认 ``reconcile-attempt``，否则只认 ``reconcile-supervisor-run``（分解稿 T2.6）。
    """

    adapter = _adapter()
    operations = head.get("operations", {})
    campaign_id = str(manifest.get("campaign_id", ""))
    facts: dict[str, Any] = {"sealed": None, "attempts": [], "failed_runs": [], "zero_request": True}
    result_path = campaign_dir / "candidates" / candidate_id / "result.json"
    sealed_attempt: str | None = None
    if result_path.is_file() and not result_path.is_symlink():
        sealed = adapter.orchestrator._read_json(result_path, "候选阶段结果")
        attempt_binding = sealed.get("attempt")
        if sealed.get("status") == "complete" and isinstance(attempt_binding, Mapping):
            attempt_path = adapter.orchestrator._campaign_file(campaign_dir, str(attempt_binding.get("path", "")))
            sealed_attempt = attempt_path.parent.name
            operation_id = f"account-sealed-candidate:{candidate_id}:{sealed_attempt}"
            if operation_id not in operations:
                raise adapter.orchestrator.ConfigurationError(
                    f"候选 {candidate_id} 已 seal 但尚未入账（缺 {operation_id}）；先执行 account-sealed-candidate。"
                )
            facts["sealed"] = {"attempt_id": sealed_attempt, "operation_id": operation_id}
            facts["zero_request"] = False
    reconciled_attempts: dict[str, dict[str, Any]] = {}
    # 改造 5 M2：恢复段（attempts/<id>/recovery/ar<k>）——已增量封存的段要求段级入账
    # account-sealed-candidate:<cid>:<id>:ar<k>，未封存的段要求段级对账 reconcile-attempt:<id>:ar<k>。
    sealed_segments: set[str] = set()
    revisions_root = campaign_dir / "candidates" / candidate_id / "revisions"
    if revisions_root.is_dir() and not revisions_root.is_symlink():
        for baseline_dir in sorted(revisions_root.iterdir()):
            baseline_result = baseline_dir / "result.json"
            if not baseline_result.is_file() or baseline_result.is_symlink():
                continue
            baseline_sealed = adapter.orchestrator._read_json(baseline_result, "候选基线阶段结果")
            recovery_binding = baseline_sealed.get("recovery")
            attempt_binding = baseline_sealed.get("attempt")
            if baseline_sealed.get("status") != "complete" or not isinstance(recovery_binding, Mapping) or not isinstance(attempt_binding, Mapping):
                continue
            segment_attempt = adapter.orchestrator._campaign_file(campaign_dir, str(attempt_binding.get("path", ""))).parent.name
            segment_revision = str(recovery_binding.get("recovery_revision"))
            operation_id = f"account-sealed-candidate:{candidate_id}:{segment_attempt}:{segment_revision}"
            if operation_id not in operations:
                raise adapter.orchestrator.ConfigurationError(
                    f"候选 {candidate_id} 的恢复段 {segment_attempt}:{segment_revision} 已增量封存但尚未入账（缺 {operation_id}）；"
                    "先执行 account-sealed-candidate --attempt-recovery。"
                )
            sealed_segments.add(f"{segment_attempt}:{segment_revision}")
            facts["zero_request"] = False
    for phase, current_candidate, attempt_root in adapter.orchestrator._campaign_attempt_roots(campaign_dir):
        if phase != "candidate" or current_candidate != candidate_id:
            continue
        attempt_id = attempt_root.name
        recovery_root = attempt_root / adapter.orchestrator.ATTEMPT_RECOVERY_DIRNAME
        if recovery_root.is_dir() and not recovery_root.is_symlink():
            for segment_root in sorted(recovery_root.iterdir()):
                if segment_root.is_symlink() or not segment_root.is_dir():
                    continue
                segment_key = f"{attempt_id}:{segment_root.name}"
                if segment_key in sealed_segments:
                    continue
                facts["zero_request"] = False
                try:
                    reconciled_segment = codex_upgrade_supervisor.verify_attempt_reconciliation_binding(
                        campaign_dir,
                        campaign_id=campaign_id,
                        candidate_id=candidate_id,
                        attempt_root=segment_root,
                        label="作废前对账核对",
                    )
                except codex_upgrade_supervisor.SupervisorError as error:
                    raise adapter.orchestrator.ConfigurationError(str(error)) from error
                if reconciled_segment["operation_id"] not in operations:
                    raise adapter.orchestrator.ConfigurationError(
                        f"候选 {candidate_id} 的恢复段 {segment_key} 未封存且尚未对账入账；先执行 reconcile-attempt --recovery-revision。"
                    )
                reconciled_attempts[segment_key] = reconciled_segment
                facts["attempts"].append(reconciled_segment)
        if attempt_id == sealed_attempt:
            continue
        facts["zero_request"] = False
        try:
            reconciled = codex_upgrade_supervisor.verify_attempt_reconciliation_binding(
                campaign_dir,
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                attempt_root=attempt_root,
                label="作废前对账核对",
            )
        except codex_upgrade_supervisor.SupervisorError as error:
            raise adapter.orchestrator.ConfigurationError(str(error)) from error
        if reconciled["operation_id"] not in operations:
            raise adapter.orchestrator.ConfigurationError(
                f"候选 {candidate_id} 的 attempt {attempt_id} 未 seal 且尚未对账入账；先执行 reconcile-attempt。"
            )
        reconciled_attempts[attempt_id] = reconciled
        facts["attempts"].append(reconciled)
    review = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, "candidate_review_required")
    if review is not None and review[0].get("candidate_id") == candidate_id:
        failed_run = adapter.orchestrator._candidate_failed_run_for_review(campaign_dir, manifest, candidate_id, review[0])
        reservations = codex_upgrade_supervisor.candidate_reservations_in_run_window(
            campaign_dir,
            candidate_id=candidate_id,
            started_at_epoch=float(failed_run["started_at_epoch"]),
        )
        if reservations:
            # 有 reservation：attempt 中断，只认 reconcile-attempt（上面的循环已完整重放）。
            missing = [attempt_id for attempt_id, _root in reservations if attempt_id not in reconciled_attempts]
            if missing:
                raise adapter.orchestrator.ConfigurationError(
                    f"候选 {candidate_id} 处于 candidate_review_required，失败父 run {failed_run['run_id']} 期间的 "
                    f"attempt {missing} 尚未对账入账；先执行 reconcile-attempt。"
                )
            facts["failed_runs"] = [
                {
                    "run_id": failed_run["run_id"],
                    "batch_sequence": failed_run["batch_sequence"],
                    "reconciliation": "attempt",
                    "attempts": [attempt_id for attempt_id, _root in reservations],
                }
            ]
        else:
            try:
                reconciled_run = codex_upgrade_supervisor.verify_supervisor_run_reconciliation_binding(
                    campaign_dir,
                    campaign_id=campaign_id,
                    run_id=failed_run["run_id"],
                    phase=failed_run["phase"],
                    batch_sequence=failed_run["batch_sequence"],
                    batch_sha256=failed_run["batch_sha256"],
                    label="作废前对账核对",
                )
            except codex_upgrade_supervisor.SupervisorError as error:
                raise adapter.orchestrator.ConfigurationError(str(error)) from error
            if reconciled_run["operation_id"] not in operations:
                raise adapter.orchestrator.ConfigurationError(
                    f"候选 {candidate_id} 处于 candidate_review_required，但失败父 run {failed_run['run_id']} 尚未对账入账；"
                    "先执行 reconcile-supervisor-run。"
                )
            facts["failed_runs"] = [
                {
                    "run_id": failed_run["run_id"],
                    "batch_sequence": failed_run["batch_sequence"],
                    "reconciliation": "supervisor-run",
                    "operation_id": reconciled_run["operation_id"],
                }
            ]
        facts["zero_request"] = False
    return facts


def _invalidation_phase(summary: Mapping[str, Any], ledger_dir: Path) -> str:
    """根因维度 phase：当前阶段，或最后一条已关闭候选级阶段，或 VC-4。"""

    adapter = _adapter()
    active = summary.get("active_phase")
    if isinstance(active, str) and active in adapter.orchestrator.CANDIDATE_VC_PHASES:
        return active
    for event_type in ("stage_abandoned", "stage_completed"):
        last = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, event_type)
        if last is not None and str(last[0].get("phase")) in adapter.orchestrator.CANDIDATE_VC_PHASES:
            return str(last[0]["phase"])
    return "VC-4"


def invalidate_candidate(arguments: argparse.Namespace) -> dict[str, Any]:
    """invalidate-candidate preview／apply：本批唯一的候选作废入口（零请求，两步式）。

    preview：admission → 候选属于当前 revision 且未 accepted → 入账核对 → 身份快照 →
    诊断草案（绑定总账 head）→ review_sha256；不落盘。
    apply：重跑同集合 admission、复验 head 与 --candidate-source → invalidation.json（write-once）
    → 若候选级阶段 active 则 stage_abandoned → outbox reconciliation_committed（请求 0，根因
    candidate.source-change-required）→ 推总账 → 二次判定 → 命中即现有停线合同（不写
    candidate_invalidated）；通过 → candidate_invalidated → revision_required；幂等。
    """

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    action = str(getattr(arguments, "invalidate_action", ""))
    if action not in {"preview", "apply"}:
        raise adapter.orchestrator.ConfigurationError("invalidate-candidate 只接受 preview 或 apply。")
    campaign_dir = Path(arguments.campaign_dir)
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest):
        raise adapter.orchestrator.ConfigurationError("invalidate-candidate 只用于 0.154.0 起的完整 VC 链 Campaign。")
    candidate_id = str(arguments.candidate_id)
    if not adapter.orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise adapter.orchestrator.ConfigurationError("--candidate-id 格式非法。")
    reviewer = str(getattr(arguments, "reviewer", "") or "").strip()
    if not reviewer:
        raise adapter.orchestrator.ConfigurationError("--reviewer 不能为空。")
    evidence_paths = [Path(item) for item in (getattr(arguments, "evidence", None) or [])]
    candidate_source = getattr(arguments, "candidate_source", None)
    approve_sha256 = getattr(arguments, "approve_sha256", None)
    if action == "apply" and not (isinstance(approve_sha256, str) and adapter.orchestrator.SHA256_RE.fullmatch(approve_sha256)):
        raise adapter.orchestrator.ConfigurationError("apply 必须给出 preview 输出的 --approve-sha256。")

    admission = adapter.orchestrator._assert_revision_admission(campaign_dir, manifest, command=INVALIDATE_CANDIDATE_COMMAND)
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    campaign_root = campaign_dir.resolve(strict=True)
    with adapter.orchestrator._campaign_lock(campaign_dir):
        summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        status = str(summary.get("status"))
        # recovery_required：VC-5 候选采集失败对账后判为候选源码问题时同样可作废（入账核对照常要求
        # 失败 attempt 已对账）；作废会先 stage_abandoned 关闭仍 active 的候选级阶段。
        recovery_phase = summary.get("recovery_phase") or summary.get("active_phase")
        if status == "recovery_required" and recovery_phase not in adapter.orchestrator.CANDIDATE_VC_PHASES:
            raise adapter.orchestrator.ConfigurationError(
                f"invalidate-candidate 拒绝：recovery_required 暂停的是 {recovery_phase}，不是候选级阶段。"
            )
        if status not in {"active", "candidate_review_required", "revision_required", "recovery_required"}:
            hint = {
                "stop_required": "账本已要求停线，按停线合同收口",
                "stopped": "Campaign 已停线，只读",
                "complete": "升级已完成，只读",
            }.get(status, "")
            raise adapter.orchestrator.ConfigurationError(f"invalidate-candidate 拒绝：账本状态 {status}；{hint}。")
        revision, record = adapter.orchestrator._current_candidate_revision_record(campaign_dir, manifest)
        if revision is None:
            raise adapter.orchestrator.ConfigurationError("没有 active 候选 revision，无候选可作废。")
        current_candidate = record["candidate_id"] if record is not None else adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
        if current_candidate != candidate_id:
            raise adapter.orchestrator.ConfigurationError(
                f"候选 {candidate_id} 不是当前 revision r{revision} 的候选（当前 {current_candidate}）。"
            )
        acceptance_path = campaign_dir / "acceptance" / candidate_id / "result.json"
        if acceptance_path.is_file() and not acceptance_path.is_symlink():
            accepted = adapter.orchestrator._read_json(acceptance_path, "验收结果")
            if accepted.get("accepted") is True or accepted.get("status") == "accepted":
                raise adapter.orchestrator.ConfigurationError(f"候选 {candidate_id} 已 accepted，不得作废。")
        project_root = reconciler._project_root(campaign_root)
        plan, head = reconciler._project_facts(project_root)
        accounting = adapter.orchestrator._candidate_accounting_checks(
            campaign_dir, manifest, candidate_id, head=head, ledger_dir=ledger_dir
        )
        snapshot = adapter.orchestrator._candidate_identity_snapshot(campaign_dir, candidate_id, candidate_source)
        evidence_refs: list[dict[str, Any]] = []
        for path in evidence_paths:
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise adapter.orchestrator.ConfigurationError(f"--evidence 必须是可信绝对普通文件：{path}")
            resolved = path.resolve(strict=True)
            relative = (
                resolved.relative_to(campaign_root).as_posix()
                if resolved.is_relative_to(campaign_root)
                else str(resolved)
            )
            evidence_refs.append({"path": relative, "sha256": adapter.orchestrator.file_sha256(resolved)})
        evidence_refs.sort(key=lambda item: item["path"])
        phase = adapter.orchestrator._invalidation_phase(summary, ledger_dir)
        try:
            root_cause_id = codex_upgrade_root_cause.structured_root_cause(
                component="candidate",
                stable_error_code="candidate.source-change-required",
                failed_step=f"candidate-source-change-{phase.lower()}",
                stable_dimensions={"phase": phase},
            )
        except codex_upgrade_root_cause.RootCauseError as error:
            raise adapter.orchestrator.ConfigurationError(f"根因编码失败：{error}") from error
        invalidation_path = campaign_dir / "candidates" / candidate_id / adapter.orchestrator.CANDIDATE_INVALIDATION_FILENAME
        existing_invalidation: dict[str, Any] | None = None
        if invalidation_path.exists() or invalidation_path.is_symlink():
            if invalidation_path.is_symlink() or not invalidation_path.is_file():
                raise adapter.orchestrator.ConfigurationError("invalidation.json 路径不可信。")
            try:
                existing_invalidation = codex_upgrade_vc_artifacts.validate_candidate_invalidation(
                    adapter.orchestrator._read_json(invalidation_path, "候选作废记录")
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(str(error)) from error
        if existing_invalidation is not None:
            # 幂等重放以已落盘的诊断为准（head 绑定的是当时的总账）。
            diagnosis = dict(existing_invalidation["diagnosis"])
        else:
            try:
                diagnosis = codex_upgrade_vc_artifacts.build_candidate_invalidation_diagnosis(
                    campaign_id=str(manifest["campaign_id"]),
                    campaign_manifest_sha256=adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
                    candidate_id=candidate_id,
                    revision=revision,
                    reviewer=reviewer,
                    reviewed_at_utc=adapter.orchestrator._utc_now(),
                    evidence_refs=evidence_refs,
                    project_ledger_head_sha256=str(head["head_sha256"]),
                    project_ledger_head_sequence=int(head["sequence"]),
                    root_cause_id=root_cause_id,
                    identity_snapshot=snapshot,
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(f"诊断草案无法生成：{error}") from error
        review_sha256 = str(diagnosis["review_sha256"])
        preview_payload = {
            "status": "preview",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "ledger_status": status,
            "accounting": accounting,
            "diagnosis": diagnosis,
            "review_sha256": review_sha256,
            "project_ledger": admission,
            "live_request_count": 0,
        }
        if action == "preview":
            return preview_payload
        if approve_sha256 != review_sha256:
            raise adapter.orchestrator.ConfigurationError(
                "apply 的批准摘要与当前草案不一致（总账 head、身份快照或证据已变化），请重新 preview。"
            )
        # 第三批 B3-5（第 7 项⑥）：任何写入之前先做零写入预检——未登记的 wire／策略／evidence 变化只提示先登记
        # 工具演进，不能在二次判定里被写成 identity_changed 永久终态。
        adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="invalidate-candidate apply")
        if existing_invalidation is None:
            # apply 首次落盘前：head 与 --candidate-source 已在本次重算的草案里复验（不一致
            # 即 review_sha256 不同）；这里写 invalidation.json（write-once）。
            try:
                invalidation = codex_upgrade_vc_artifacts.build_candidate_invalidation(
                    campaign_id=str(manifest["campaign_id"]),
                    candidate_id=candidate_id,
                    revision=revision,
                    diagnosis=diagnosis,
                    recorded_at_utc=adapter.orchestrator._utc_now(),
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(str(error)) from error
            ensure_private_directory(invalidation_path.parent, campaign_dir)
            adapter.orchestrator._secure_write_json_once(invalidation_path, invalidation)
        else:
            invalidation = existing_invalidation
        # 若候选级阶段 active，先关闭阶段（根因 = 作废根因）。
        ledger_events: list[dict[str, Any]] = []
        current = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        active = current.get("active_phase")
        if isinstance(active, str) and active in adapter.orchestrator.CANDIDATE_VC_PHASES:
            try:
                codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=f"candidate-invalidation-{candidate_id}-r{revision}-stage-abandoned",
                    phase=active,
                    event_type="stage_abandoned",
                    root_cause_id=root_cause_id,
                    next_action="invalidate-candidate apply：推送根因后二次判定",
                )
            except codex_upgrade_timing_ledger.TimingLedgerError as error:
                raise adapter.orchestrator.ConfigurationError(f"UpgradeTimingLedger 拒绝 stage_abandoned：{error}") from error
            ledger_events.append({"event_type": "stage_abandoned", "phase": active})
        # outbox 根因事件（请求 0）→ 推总账 → 重放 → 二次判定。
        receipt_binding = reconciler._binding(campaign_root, invalidation_path, "reconciliation")
        cause = codex_upgrade_root_cause.describe_root_cause(
            component="candidate",
            stable_error_code="candidate.source-change-required",
            failed_step=f"candidate-source-change-{phase.lower()}",
            stable_dimensions={"phase": phase},
        )
        operation_id = f"invalidate-candidate:{candidate_id}:r{revision}"
        try:
            batch = reconciler._commit_batch(
                campaign_root,
                operation_id=operation_id,
                event_type="reconciliation_committed",
                payload={
                    "campaign_id": str(manifest["campaign_id"]),
                    "subject_kind": "candidate_invalidation",
                    "subject_id": f"{candidate_id}:r{revision}",
                    "phase": phase,
                    "request": dict(reconciler.ZERO_REQUEST_PART),
                    "root_cause": {
                        "root_cause_id": cause["root_cause_id"],
                        "stable_error_code": cause["stable_error_code"],
                        "failed_step": cause["failed_step"],
                        "stable_dimensions": cause["stable_dimensions"],
                        "component": cause["component"],
                    },
                    "reconciliation_receipt_sha256": receipt_binding["sha256"],
                    "attempt_failed_event_sha256": None,
                },
                source={"kind": "candidate_invalidation", "sha256": receipt_binding["sha256"]},
                receipt_bindings=[receipt_binding],
            )
            observed = adapter.orchestrator._utc_now()
            pushed, head_after = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            identity = reconciler._identity_facts(campaign_root, manifest, reconciler._current_identity())
            ledger_facts = reconciler._ledger_facts(ledger_dir, now=observed)
            # 修好接着跑第 13 项：未隔离污染暂停待 environment-isolate，官方已封存受污染才终态。
            environment_decision = adapter.orchestrator._campaign_environment_decision(campaign_dir, _manifest=manifest)
            decision = reconciler._decide(
                head=head_after,
                plan=plan,
                ledger=ledger_facts,
                identity=identity,
                environment_status=reconciler._environment_decision_status(environment_decision),
                campaign_deadline_at_utc=adapter.orchestrator._campaign_plan_deadline(campaign_dir),
                root_cause_id=cause["root_cause_id"],
                request_status="resolved",
                now=observed,
                campaign_id=str(manifest["campaign_id"]),
            )
        except reconciler.ReconcilerError as error:
            raise adapter.orchestrator.ConfigurationError(f"候选作废入账失败：{error}") from error
        result: dict[str, Any] = {
            "status": "applied",
            "idempotent": False,
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "invalidation_receipt": receipt_binding,
            "root_cause_id": cause["root_cause_id"],
            "batch": batch,
            "project_push": pushed,
            "decision": decision,
            "ledger_events": ledger_events,
            "live_request_count": 0,
        }
        if decision["decision"] == reconciler.DECISION_PAUSED:
            # 预算到期只是暂停，不是停线（此前误把暂停也送进永久停线，写出 terminal_reason="None" 的终态 batch，
            # 总账重放拒收、batch 永远推不进）。作废收据、根因 batch 与账本事件都已幂等落盘，批准延期后以同一批准
            # 摘要重跑 apply 即从二次判定续接。本函数持有 Campaign 锁，不在锁内登记暂停事实（登记需要同一把锁），
            # 对账或派发入口遇到到期时会登记。
            result["status"] = "paused"
            result["next_command"] = "预算已暂停：" + reconciler._paused_next_command(
                decision, "以同一批准摘要重新执行 invalidate-candidate --action apply（幂等续接）"
            )
            return result
        if decision["decision"] != reconciler.DECISION_RECOVERABLE:
            try:
                stop = reconciler._permanent_stop(
                    campaign_root,
                    manifest,
                    ledger_dir,
                    subject_id=f"candidate-invalidation-{candidate_id}-r{revision}",
                    root_cause_id=cause["root_cause_id"],
                    terminal_reason=str(decision["terminal_reason"]),
                    receipt_bindings=[receipt_binding],
                    ledger_receipts=[],
                    live_request_count=0,
                    reconciliation_receipt_sha256=receipt_binding["sha256"],
                    ledger_facts=ledger_facts,
                )
                pushed_terminal, head_terminal = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(f"候选作废后永久停线收口失败：{error}") from error
            result["status"] = "permanent_stop"
            result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
            result["next_command"] = stop["next_action"]
            return result
        # 通过：candidate_invalidated（绑定 invalidation.json 与 outbox batch COMMIT 的账本副本）→ revision_required。
        last_invalidated = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, "candidate_invalidated")
        if (
            last_invalidated is not None
            and last_invalidated[0].get("candidate_id") == candidate_id
            and int(last_invalidated[0].get("revision") or 0) == revision
        ):
            result["idempotent"] = True
            result["ledger_events"].append({"event_type": "candidate_invalidated", "appended": False})
            result["next_command"] = f"revision-open --candidate-id <new> --supersedes {candidate_id}"
            return result
        receipts = adapter.orchestrator._publish_ledger_receipt_copies(
            ledger_dir,
            f"candidate-{candidate_id}-r{revision}",
            {
                "invalidation": invalidation_path,
                "reconciliation": Path(str(batch["batch_dir"])) / "COMMIT",
            },
        )
        try:
            appended = codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"candidate-invalidated-{candidate_id}-r{revision}",
                phase=phase,
                event_type="candidate_invalidated",
                revision=revision,
                candidate_id=candidate_id,
                root_cause_id=cause["root_cause_id"],
                receipts=receipts,
                next_action=f"revision-open --candidate-id <new> --supersedes {candidate_id}",
            )
        except codex_upgrade_timing_ledger.TimingLedgerError as error:
            raise adapter.orchestrator.ConfigurationError(f"UpgradeTimingLedger 拒绝 candidate_invalidated：{error}") from error
        result["ledger_events"].append(
            {
                "event_type": "candidate_invalidated",
                "appended": True,
                "head_sequence": appended["head_sequence"],
                "head_sha256": appended["head_sha256"],
                "ledger_status": appended["status"],
            }
        )
        result["next_command"] = f"revision-open --candidate-id <new> --supersedes {candidate_id}"
        return result


def _locate_evaluation_failed_run(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    revision: int,
    baseline: int,
) -> dict[str, Any]:
    """定位当前基线的评估失败父 run：候选级 VC-5 COMMIT → parent_run_dir → action-failed 事实。

    只接受失败动作属评估动作（builder／compare／accept）且清单冻结基线等于当前基线的 run；
    命中多个取序号最大者（同基线重派重现失败）。返回运行事实、内层清单与 stop-receipt。
    """

    adapter = _adapter()
    campaign_id = str(manifest.get("campaign_id", ""))
    commits_root = campaign_dir / "control" / "vc" / "commits"
    if commits_root.is_symlink():
        raise adapter.orchestrator.ConfigurationError("COMMIT 目录不得是符号链接。")
    matched: list[dict[str, Any]] = []
    if commits_root.is_dir():
        for path in sorted(commits_root.iterdir()):
            match = adapter.orchestrator._VC_SEQUENCE_FILE_RE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise adapter.orchestrator.ConfigurationError(f"COMMIT 目录含非法条目：{path.name}")
            commit = adapter.orchestrator._read_staging_commit(path)
            if commit["campaign_id"] != campaign_id or str(commit["phase"]) != "VC-5":
                continue
            run_dir = Path(str(commit.get("parent_run_dir", "")))
            if not run_dir.is_absolute():
                continue
            try:
                facts = codex_upgrade_supervisor.campaign_run_failure_facts(run_dir, campaign_dir=campaign_dir)
            except codex_upgrade_supervisor.SupervisorError as error:
                raise adapter.orchestrator.ConfigurationError(f"父 run {run_dir.name} 的失败事实不可信：{error}") from error
            if facts is None or facts["candidate_id"] != candidate_id or facts["candidate_revision"] != revision:
                continue
            record = adapter.orchestrator._read_json(run_dir / "campaign-run-manifest.json", "campaign-run 清单")
            inner = record.get("manifest")
            if not isinstance(inner, Mapping):
                continue
            if int(inner.get("evaluation_baseline") or 0) != baseline:
                continue
            action = next(
                (item for item in inner.get("actions", []) if isinstance(item, Mapping) and item.get("action_id") == facts["action_id"]),
                None,
            )
            if action is None:
                continue
            kind = adapter.orchestrator._evaluation_action_kind(list(action["command"]))
            if kind is None:
                continue
            if facts["failure_class"] in codex_upgrade_supervisor.PERMANENT_ACTION_FAILURE_CLASSES:
                # identity-drift 等永久失败类不是 evaluator 缺陷（动作根本没有执行或环境不可信），
                # 只能走既有停线／人工审计，不进入评估基线状态机。
                raise adapter.orchestrator.ConfigurationError(
                    f"父 run {run_dir.name} 的失败分类 {facts['failure_class']} 属永久失败类，evaluation-recover 不受理。"
                )
            matched.append({**facts, "inner_manifest": dict(inner), "action": dict(action), "action_kind": kind})
    if not matched:
        raise adapter.orchestrator.ConfigurationError(
            f"候选 {candidate_id} 在评估基线 b{baseline} 没有评估动作失败的父 run；evaluation-recover 只处理封存后的离线评估失败。"
        )
    return max(matched, key=lambda item: int(item["batch_sequence"]))


def _evaluation_failure_scope(
    campaign_dir: Path,
    candidate: Mapping[str, Any],
    attempt: Mapping[str, Any],
    evaluation_run: Mapping[str, Any],
    assertions_root: Path,
) -> tuple[dict[str, Any], str]:
    """failure-scope 定位链：失败规则 → 失败 check → evidence_paths → provenance → source_root → 唯一 Job。

    返回 ``(failure_scope, failed_step)``；每步失败关闭，不得退化为全量。J* 取定位到的 Job
    （候选 Job 间无声明依赖时下游闭集等于本身）；R* 为引用了 J* 任一 artifact 的规则。
    """

    adapter = _adapter()
    context = candidate.get("assertion_context")
    if not isinstance(context, Mapping):
        raise adapter.orchestrator.ConfigurationError("候选阶段结果缺少 assertion_context，无法定位失败证据。")
    bundle_dir = Path(str(context.get("evidence_root", "")))
    provenance_path = bundle_dir / "provenance.json"
    if not bundle_dir.is_absolute() or provenance_path.is_symlink() or not provenance_path.is_file():
        raise adapter.orchestrator.ConfigurationError("候选 assertion bundle 缺少 provenance 收据，无法定位失败证据。")
    provenance = adapter.orchestrator._read_json(provenance_path, "assertion bundle provenance")
    by_target: dict[str, str] = {}
    for entry in provenance.get("entries", []):
        if not isinstance(entry, Mapping):
            raise adapter.orchestrator.ConfigurationError("provenance 条目非法。")
        by_target[str(entry.get("target_path"))] = str(entry.get("source_root"))
    # 派生观测（derived/…，由 relay 流等原始证据按派生收据生成）沿派生收据的 source 回到原始条目，
    # 再由原始条目的 source_root 定位 Job；派生收据缺失或 source 未登记即失败关闭。
    derived_receipt_path = bundle_dir / "derived" / "derived-provenance.json"
    if derived_receipt_path.exists() or derived_receipt_path.is_symlink():
        if derived_receipt_path.is_symlink() or not derived_receipt_path.is_file():
            raise adapter.orchestrator.ConfigurationError("assertion bundle 的派生收据不可信。")
        derived_receipt = adapter.orchestrator._read_json(derived_receipt_path, "assertion bundle 派生收据")
        for entry in derived_receipt.get("entries", []):
            if not isinstance(entry, Mapping):
                raise adapter.orchestrator.ConfigurationError("派生收据条目非法。")
            source = str(entry.get("source"))
            target = str(entry.get("target"))
            if source not in by_target:
                raise adapter.orchestrator.ConfigurationError(f"派生观测 {target} 的来源 {source} 没有 provenance 条目。")
            if target in by_target and by_target[target] != by_target[source]:
                raise adapter.orchestrator.ConfigurationError(f"派生观测 {target} 同时被原始条目登记且来源根不一致。")
            by_target[target] = by_target[source]
    roots_by_name: dict[str, str] = {}
    for result in attempt.get("results", []):
        if not isinstance(result, Mapping):
            continue
        for root in result.get("evidence_roots", []) or []:
            name = Path(str(root)).name
            if name in roots_by_name and roots_by_name[name] != result.get("id"):
                raise adapter.orchestrator.ConfigurationError(f"证据根名称 {name} 被多个 Job 声明，定位链不唯一。")
            roots_by_name[name] = str(result.get("id"))
    manifest_path = Path(str(context.get("capture_manifest_path", "")))
    capture_manifest = adapter.orchestrator._read_json(manifest_path, "候选 capture manifest") if manifest_path.is_file() else {}
    artifact_jobs: dict[str, str] = {}
    for artifact in capture_manifest.get("artifacts", []) or []:
        if isinstance(artifact, Mapping):
            source_root = by_target.get(str(artifact.get("path")))
            if source_root is not None and source_root in roots_by_name:
                artifact_jobs[str(artifact.get("path"))] = roots_by_name[source_root]
    failed_rules = sorted(row["rule"] for row in evaluation_run["rules"] if row["status"] == "fail")
    if not failed_rules:
        raise adapter.orchestrator.ConfigurationError("evaluation-run.json 没有 status=fail 的规则。")
    failed_checks: list[dict[str, Any]] = []
    jobs: set[str] = set()
    official_refs: set[str] = set()
    for rule in failed_rules:
        document_path = assertions_root / "machine" / "candidate" / f"{rule}.json"
        if not document_path.is_file():
            # 候选侧文档缺失（官方侧失败）：只列官方引用，不能定位候选 Job。
            official_document = assertions_root / "machine" / "official" / f"{rule}.json"
            if official_document.is_file():
                for check in adapter.orchestrator._read_json(official_document, f"{rule} 官方文档").get("checks", []):
                    if isinstance(check, Mapping) and check.get("passed") is False:
                        official_refs.update(str(path) for path in check.get("evidence_paths", []))
            continue
        document = adapter.orchestrator._read_json(document_path, f"{rule} 候选文档")
        for check in document.get("checks", []):
            if not isinstance(check, Mapping) or check.get("passed") is not False:
                continue
            paths = [str(path) for path in check.get("evidence_paths", [])]
            check_jobs: set[str] = set()
            for path in paths:
                source_root = by_target.get(path)
                if source_root is None:
                    raise adapter.orchestrator.ConfigurationError(f"失败 check {rule}/{check.get('id')} 引用的证据 {path} 没有 provenance 条目。")
                job_id = roots_by_name.get(source_root)
                if job_id is None:
                    raise adapter.orchestrator.ConfigurationError(f"失败 check {rule}/{check.get('id')} 的证据根 {source_root} 无法映射到唯一 Job。")
                check_jobs.add(job_id)
            failed_checks.append(
                {"rule": rule, "check_id": str(check.get("id")), "evidence_paths": sorted(paths), "jobs": sorted(check_jobs)}
            )
            jobs.update(check_jobs)
    affected_rules: set[str] = set(failed_rules)
    if jobs:
        for rule in (row["rule"] for row in evaluation_run["rules"]):
            document_path = assertions_root / "machine" / "candidate" / f"{rule}.json"
            if not document_path.is_file():
                continue
            document = adapter.orchestrator._read_json(document_path, f"{rule} 候选文档")
            for check in document.get("checks", []):
                if isinstance(check, Mapping) and any(
                    artifact_jobs.get(str(path)) in jobs for path in check.get("evidence_paths", [])
                ):
                    affected_rules.add(rule)
    scope = {
        "failed_rules": failed_rules,
        "failed_checks": failed_checks,
        "jobs": sorted(jobs),
        "rules": sorted(affected_rules),
        "official_refs": sorted(official_refs),
    }
    return scope, failed_rules[0]


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
    """从失败父 run 推出诊断稳定事实：失败来源（tagged union）、复用授权、绑定、failure-scope。"""

    adapter = _adapter()
    failed = adapter.orchestrator._locate_evaluation_failed_run(campaign_dir, manifest, candidate_id, revision, baseline)
    run_dir = Path(str(failed["run_dir"]))
    failure_source = EVALUATION_ACTION_KINDS_BY_SOURCE[str(failed["action_kind"])]
    try:
        stop = codex_upgrade_supervisor.read_stop_receipt(run_dir)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(f"失败父 run 的 stop-receipt 无法校验：{error}") from error
    diagnostic_path = run_dir / "action-diagnostics" / f"action-{failed['action_id']}-failure.json"
    if not diagnostic_path.is_file():
        raise adapter.orchestrator.ConfigurationError("失败父 run 缺少动作诊断。")
    outputs_sha256 = stop.get("action_outputs_sha256")
    action_outputs: dict[str, Any] | None = None
    assertions_root = adapter.orchestrator._assertions_root_for_baseline(campaign_dir, candidate_id, baseline)
    index_path = assertions_root / adapter.orchestrator.EVALUATION_RUN_FILENAME
    if outputs_sha256 is not None:
        try:
            binding = codex_upgrade_supervisor.read_action_output_binding(run_dir, str(failed["action_id"]))
        except codex_upgrade_supervisor.SupervisorError as error:
            raise adapter.orchestrator.ConfigurationError(f"失败父 run 的动作输出绑定无法读取：{error}") from error
        if binding["binding_sha256"] != outputs_sha256:
            raise adapter.orchestrator.ConfigurationError("stop-receipt 的 action_outputs_sha256 与动作输出绑定文件不一致。")
        index_relative = index_path.relative_to(campaign_dir).as_posix()
        checkpoints_relative = (assertions_root / adapter.orchestrator.EVALUATION_CHECKPOINTS_DIRNAME).relative_to(campaign_dir).as_posix()
        items = {item["path"]: item for item in binding["bindings"]}
        run_item = items.get(index_relative)
        head_item = items.get(checkpoints_relative)
        action_outputs = {
            "path": str(run_dir / "action-outputs" / f"{failed['action_id']}.json"),
            "sha256": adapter.orchestrator.file_sha256(run_dir / "action-outputs" / f"{failed['action_id']}.json"),
            "evaluation_run": (
                {"path": index_relative, "sha256": str(run_item["sha256"])}
                if run_item is not None and run_item["exists"] and run_item["sha256"] is not None
                else None
            ),
            "checkpoint_head_sha256": (
                str(head_item["sha256"]) if head_item is not None and head_item["sha256"] is not None else None
            ),
        }
    if failure_source == "offline-compare-failed":
        reuse_authority = "none"
    elif action_outputs is not None and action_outputs["evaluation_run"] is not None:
        reuse_authority = "anchored"
    else:
        reuse_authority = "none"
    evaluation_run_binding: dict[str, Any] | None = None
    evaluation_run: dict[str, Any] | None = None
    failed_step: str
    scope: dict[str, Any] = {"failed_rules": [], "failed_checks": [], "jobs": [], "rules": [], "official_refs": []}
    admissible = ["approval-inputs", "candidate-source", "evaluator-defect"]
    if failure_source == "assertion-failed":
        if index_path.is_file() and not index_path.is_symlink():
            try:
                evaluation_run = codex_upgrade_vc_artifacts.validate_evaluation_run(adapter.orchestrator._read_json(index_path, "evaluation-run.json"))
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(f"evaluation-run.json 无法校验：{error}") from error
        elif baseline == 0:
            evaluation_run = adapter.orchestrator._derive_b0_evaluation_run(campaign_dir, manifest, candidate_id, revision, classification, rules)
            ensure_private_directory(assertions_root, campaign_dir)
            adapter.orchestrator._secure_write_json_once(index_path, evaluation_run)
            reuse_authority = "none"
        else:
            raise adapter.orchestrator.ConfigurationError(
                f"评估基线 b{baseline} 的断言批次未落 evaluation-run.json（builder 未到批次结束），"
                "请先同基线逐字重派该批次。"
            )
        if action_outputs is not None and action_outputs["evaluation_run"] is not None and (
            action_outputs["evaluation_run"]["sha256"] != adapter.orchestrator.file_sha256(index_path)
        ):
            raise adapter.orchestrator.ConfigurationError("动作输出绑定记录的 evaluation-run 摘要与当前文件不一致。")
        if evaluation_run["derived"]:
            reuse_authority = "none"
        evaluation_run_binding = {
            "path": index_path.relative_to(campaign_dir).as_posix(),
            "sha256": adapter.orchestrator.file_sha256(index_path),
            "derived": bool(evaluation_run["derived"]),
        }
        scope, failed_step = adapter.orchestrator._evaluation_failure_scope(campaign_dir, candidate, attempt, evaluation_run, assertions_root)
        admissible.append("transient-environment")
    elif failure_source == "offline-compare-failed":
        failed_step = "compare"
    else:
        failed_step = "acceptance"
        gates_root = adapter.orchestrator._stage_write_target(campaign_dir, candidate_id, baseline, "accept").parent / "attempts"
        if gates_root.is_dir():
            for attempt_dir in sorted(gates_root.iterdir()):
                result_path = attempt_dir / "result.json"
                if result_path.is_file() and adapter.orchestrator._read_json(result_path, "accept attempt 结果").get("failed_gates"):
                    raise adapter.orchestrator.ConfigurationError(
                        "accept 因 failed_gates 非零退出，不是本合同的事实；请按指南 4.5.6 补跑外部门禁。"
                    )
        if action_outputs is not None and action_outputs["evaluation_run"] is not None and index_path.is_file():
            # accept 失败时断言批次已全部 pass：索引就是 b<K+1> 复用的依据（anchored），必须与
            # 动作输出绑定记录的摘要一致并可校验，否则复用集合退化为全部重跑。
            if action_outputs["evaluation_run"]["sha256"] != adapter.orchestrator.file_sha256(index_path):
                raise adapter.orchestrator.ConfigurationError("动作输出绑定记录的 evaluation-run 摘要与当前文件不一致。")
            try:
                evaluation_run = codex_upgrade_vc_artifacts.validate_evaluation_run(adapter.orchestrator._read_json(index_path, "evaluation-run.json"))
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(f"evaluation-run.json 无法校验：{error}") from error
            if evaluation_run["candidate_id"] != candidate_id or evaluation_run["evaluation_baseline"] != baseline:
                raise adapter.orchestrator.ConfigurationError("evaluation-run.json 的候选或基线身份与失败批次不一致。")
            if evaluation_run["derived"]:
                reuse_authority = "none"
            evaluation_run_binding = {
                "path": index_path.relative_to(campaign_dir).as_posix(),
                "sha256": adapter.orchestrator.file_sha256(index_path),
                "derived": bool(evaluation_run["derived"]),
            }
    frozen_digests = failed["inner_manifest"].get("evaluator_digests")
    if not isinstance(frozen_digests, Mapping):
        raise adapter.orchestrator.ConfigurationError("失败父 run 的清单没有冻结 evaluator_digests（非 v3 批次），不能用 evaluation-recover。")
    try:
        current_digests = adapter.identity_policy.evaluator_dependency_digests()
    except adapter.identity_policy.ToolIdentityPolicyError as error:
        raise adapter.orchestrator.ConfigurationError(f"evaluator 依赖摘要无法计算：{error}") from error
    return {
        "failed": failed,
        "failure_source": failure_source,
        "reuse_authority": reuse_authority,
        "failed_step": failed_step,
        "failed_run": {
            "run_id": str(failed["run_id"]),
            "run_dir": str(run_dir),
            "manifest_sha256": adapter.orchestrator.file_sha256(run_dir / "campaign-run-manifest.json"),
            "state_sha256": adapter.orchestrator.file_sha256(run_dir / "state.json"),
            "stop_receipt_sha256": adapter.orchestrator.file_sha256(run_dir / "stop-receipt.json"),
            "action_id": str(failed["action_id"]),
            "action_diagnostic_sha256": adapter.orchestrator.file_sha256(diagnostic_path),
            "action_outputs_sha256": outputs_sha256,
        },
        "action_outputs": action_outputs,
        "evaluation_run": evaluation_run_binding,
        "evaluation_run_document": evaluation_run,
        "failure_scope": scope,
        "failed_evaluator_digests": {field: str(frozen_digests[field]) for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS},
        "current_evaluator_digests": current_digests,
        "admissible_classes": sorted(admissible),
    }


def _verify_current_deployment_receipt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    receipt_path: Path,
) -> dict[str, Any]:
    """修复已受监督部署且正在被使用：部署收据 passed、wire／policy 等于 Campaign 有效身份、五摘要等于当前受管树。

    evaluator-defect 与第三批 B3-3 的 reevaluate 共用同一口径。
    """

    adapter = _adapter()
    receipt = adapter.orchestrator._read_json(Path(receipt_path), "部署收据")
    if receipt.get("status") != "passed":
        raise adapter.orchestrator.ConfigurationError("部署收据 status 不是 passed。")
    frozen = manifest.get("tool_identity") if isinstance(manifest.get("tool_identity"), Mapping) else {}
    # 基准是 Campaign 有效工具身份：登记过工具演进时为最新演进的 to 身份（wire 修复已逐作业裁定）。
    if adapter.orchestrator._is_policy_v2_identity(frozen):
        frozen = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)["identity"]
    for field in ("wire_producer_sha256", "policy_sha256"):
        frozen_value = frozen.get(field)
        if frozen_value is None or receipt.get(field) != frozen_value:
            raise adapter.orchestrator.ConfigurationError(f"部署收据的 {field} 与 Campaign 有效工具身份不一致或缺失。")
    # 部署收据必须就是当前运行工具树的那一份：五摘要等于当前受管身份（修复已部署且正在被使用）。
    current = adapter.orchestrator._tool_identity(include_git=False)
    for field, current_field in (
        ("tool_files_sha256", "files_sha256"),
        ("policy_sha256", "policy_sha256"),
        ("wire_producer_sha256", "wire_producer_sha256"),
        ("evidence_semantics_sha256", "evidence_semantics_sha256"),
        ("control_sha256", "control_sha256"),
    ):
        if receipt.get(field) != current.get(current_field):
            raise adapter.orchestrator.ConfigurationError(f"部署收据的 {field} 不等于当前受管工具树身份；修复尚未部署到当前树。")
    return receipt


def _evaluation_defect_admission(
    arguments: argparse.Namespace,
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    attempt_root: Path,
) -> dict[str, Any]:
    """evaluator-defect 准入：修复提交＋部署收据（passed、wire／policy 等于冻结值、五摘要等于当前树）、
    四项摘要至少一项变化且覆盖缺陷项、候选 attempt 的 evaluation-epoch 链已覆盖当前 evidence 摘要。

    既有 A2 合同：evidence semantics 变化后评估类操作要求先对当前 attempt 追加 ``evaluation-epoch``。
    apply 不代写 epoch（仍由 ``evaluation-epoch --candidate-id`` 生成），只重放完整 append-only 链并把
    采用的 epoch（path／sha256／index／to_evidence_semantics_sha256）冻结进 recovery.json，经
    AUTHORIZATION／COMMIT 的 ``recovery_sha256`` 传递绑定，供 accept 读侧回读复核。
    """

    adapter = _adapter()
    fix_commit = getattr(arguments, "fix_commit", None)
    receipt_path = getattr(arguments, "deployment_receipt", None)
    if not isinstance(fix_commit, str) or not re.fullmatch(r"^[0-9a-f]{40}$", fix_commit):
        raise adapter.orchestrator.ConfigurationError("evaluator-defect 必须以 --fix-commit 给出完整 40 位修复提交。")
    if receipt_path is None or not Path(receipt_path).is_absolute() or Path(receipt_path).is_symlink() or not Path(receipt_path).is_file():
        raise adapter.orchestrator.ConfigurationError("evaluator-defect 必须以 --deployment-receipt 给出可信绝对路径的部署收据。")
    adapter.orchestrator._verify_current_deployment_receipt(campaign_dir, manifest, Path(receipt_path))
    current = adapter.orchestrator._tool_identity(include_git=False)
    # 修好接着跑第 8 项后半：失败批次冻结于旧读侧口径时，reader 两项会因口径切换显示变化——这里有意不按旧
    # 口径复算去掉它：否则 b≥1 下重派判漂移、evaluation-recover 又判"没变化"，两头拒绝即卡死。多放行的后果
    # 只是开新基线按当前工具重评一次（真实缺陷未修则再次失败，届时同口径比较即被拒），不会得出错误结论。
    changed = sorted(
        field
        for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS
        if facts["current_evaluator_digests"][field] != facts["failed_evaluator_digests"][field]
    )
    if not changed:
        raise adapter.orchestrator.ConfigurationError("evaluator 四项摘要相对失败批次没有任何变化，不能裁定 evaluator-defect。")
    defect_items = EVALUATION_DEFECT_ITEMS[str(facts["failure_source"])]
    if not any(item in changed for item in defect_items):
        raise adapter.orchestrator.ConfigurationError(
            f"变化项 {changed} 未覆盖失败来源 {facts['failure_source']} 声明的缺陷项 {list(defect_items)}。"
        )
    evaluation_epoch = adapter.orchestrator._evaluation_epoch_for_recovery(
        campaign_dir, manifest, attempt_root, current_evidence=str(current["evidence_semantics_sha256"])
    )
    return {
        "fix_commit": fix_commit,
        "deployment_receipt": {"path": str(Path(receipt_path).resolve(strict=True)), "sha256": adapter.orchestrator.file_sha256(Path(receipt_path))},
        "changed_items": changed,
        "evaluation_epoch": evaluation_epoch,
    }


def _next_recovery_revision(attempt_root: Path) -> str:
    """原 attempt 下一个恢复段编号 ar<k>：已存在的段目录（含未收口）编号最大值 + 1。"""

    adapter = _adapter()
    recovery_root = attempt_root / adapter.orchestrator.ATTEMPT_RECOVERY_DIRNAME
    highest = 0
    if recovery_root.exists():
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            raise adapter.orchestrator.ConfigurationError("attempt 恢复段目录不可信。")
        for entry in recovery_root.iterdir():
            if entry.is_symlink() or not entry.is_dir() or not codex_upgrade_vc_artifacts.RECOVERY_REVISION_RE.fullmatch(entry.name):
                raise adapter.orchestrator.ConfigurationError(f"attempt 恢复段目录含非法条目：{entry.name}")
            highest = max(highest, int(entry.name[2:]))
    return f"ar{highest + 1}"


def _transient_environment_admission(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    attempt_root: Path,
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    """transient-environment 准入（三稿 3.4／分解稿 T5.17）：

    候选五层身份等于冻结值；原 attempt 的 after 探针与恢复收据存在且 ``restoration_error=null``；
    Campaign 无污染记录；``J*`` 非空且每个 Job 已沿 failure-scope 定位链证明（定位链本身失败关闭，
    全部 Job 亦允许）；``J*`` 与其余 Job 恰好划分原 attempt 的 Job 全集。根因 ``attempt.job-transient-failure``
    的上限由 apply 的 outbox 入账后二次判定处理。返回冻结进 recovery.json 的段计划。
    """

    adapter = _adapter()
    if facts["failure_source"] != "assertion-failed":
        raise adapter.orchestrator.ConfigurationError("transient-environment 只能由断言失败触发（离线 compare／accept 工具异常不是环境故障）。")
    identity = attempt.get("identity")
    if not isinstance(identity, Mapping):
        raise adapter.orchestrator.ConfigurationError("原 attempt 缺少候选身份，不能裁定 transient-environment。")
    adapter.orchestrator._verify_candidate_attempt_identity(manifest, identity)
    if attempt.get("status") != adapter.orchestrator.ATTEMPT_RECOVERY_SUCCESS_STATUS:
        raise adapter.orchestrator.ConfigurationError(f"原 attempt 状态为 {attempt.get('status')}，不是 awaiting_receipts。")
    environment = attempt.get("environment")
    if (
        not isinstance(environment, Mapping)
        or not isinstance(environment.get("after_probe"), Mapping)
        or not isinstance(environment.get("restoration_report"), Mapping)
        or attempt.get("restoration_error") is not None
    ):
        raise adapter.orchestrator.ConfigurationError("原 attempt 缺少 after 探针或恢复收据，或恢复曾失败；环境事实不足以裁定 transient-environment。")
    contamination = adapter.orchestrator._campaign_contamination_records(campaign_dir, _manifest=manifest)
    if contamination:
        raise adapter.orchestrator.ConfigurationError("Campaign 存在环境污染记录，不能裁定 transient-environment。")
    if ("candidate", attempt.get("candidate_id"), str(attempt.get("attempt_id"))) in adapter.orchestrator._rerun_all_invalidated_attempts(campaign_dir):
        # 修好接着跑第 13 项：被环境隔离作废的 attempt 结果永不复用，只能全部重跑，不能做段恢复（第 24 项证据根
        # 冲突隔离同理）。
        raise adapter.orchestrator.ConfigurationError(
            "原 attempt 已被环境隔离作废或证据根冲突隔离（结果永不复用），不能裁定 transient-environment；"
            "reconcile-attempt 入账并批准恢复预览后 resume --rerun-failed 全部重跑。"
        )
    scope = facts["failure_scope"]
    if not isinstance(scope, Mapping):
        raise adapter.orchestrator.ConfigurationError("失败事实缺少 failure-scope，不能裁定 transient-environment。")
    execute_jobs = sorted(str(job_id) for job_id in scope.get("jobs", []))
    if not execute_jobs:
        raise adapter.orchestrator.ConfigurationError("failure-scope 没有定位到任何候选 Job（J* 为空），不能裁定 transient-environment。")
    all_jobs = sorted(
        str(result.get("id"))
        for result in attempt.get("results", [])
        if isinstance(result, Mapping) and isinstance(result.get("id"), str)
    )
    unknown = sorted(set(execute_jobs) - set(all_jobs))
    if unknown:
        raise adapter.orchestrator.ConfigurationError(f"failure-scope 定位到的 Job 不在原 attempt 内：{unknown}")
    reuse_jobs = sorted(set(all_jobs) - set(execute_jobs))
    return {
        "attempt_id": attempt_root.name,
        "recovery_revision": adapter.orchestrator._next_recovery_revision(attempt_root),
        "execute_jobs": execute_jobs,
        "reuse_jobs": reuse_jobs,
        "affected_rules": sorted(str(rule) for rule in scope.get("rules", [])),
        "failed_step": execute_jobs[0],
    }


def _evaluation_epoch_for_recovery(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    *,
    current_evidence: str,
) -> dict[str, Any] | None:
    """重放候选 attempt 的完整 evaluation-epoch 链并给出 recovery 采用的 epoch 绑定。

    链末 ``to_evidence_semantics_sha256``（无链即 Campaign 冻结值）必须等于当前受管树的 evidence
    摘要；否则 evidence 已变化而 epoch 未追加（或追加后又变化），失败关闭。链为空返回 None。
    """

    adapter = _adapter()
    try:
        chain = codex_upgrade_wire_transition.load_epochs(attempt_root)
    except codex_upgrade_wire_transition.WireTransitionError as error:
        raise adapter.orchestrator.ConfigurationError(f"候选 attempt 的 evaluation-epoch 链无法重放：{error}") from error
    frozen = manifest.get("tool_identity", {}).get("evidence_semantics_sha256") if isinstance(manifest.get("tool_identity"), Mapping) else None
    effective = str(chain[-1]["to_evidence_semantics_sha256"]) if chain else (str(frozen) if frozen is not None else None)
    if effective != current_evidence:
        raise adapter.orchestrator.ConfigurationError(
            "当前受管树的 evidence semantics 摘要与候选 attempt 的 evaluation-epoch 链末不一致；"
            "先执行 evaluation-epoch --candidate-id 追加覆盖当前 evidence 摘要的 epoch，再 apply。"
        )
    if not chain:
        return None
    last = chain[-1]
    path = attempt_root / f"evaluation-epoch-{int(last['index']):02d}.json"
    if path.is_symlink() or not path.is_file():
        raise adapter.orchestrator.ConfigurationError("evaluation-epoch 链末文件不存在。")
    return {
        "path": path.relative_to(campaign_dir).as_posix(),
        "sha256": adapter.orchestrator.file_sha256(path),
        "index": int(last["index"]),
        "to_evidence_semantics_sha256": str(last["to_evidence_semantics_sha256"]),
    }


def _verify_failed_run_reconciled(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    ledger_dir: Path,
    failed: Mapping[str, Any],
    *,
    head: Mapping[str, Any],
) -> dict[str, Any]:
    """apply 的账本前提：失败父 run 已由 reconcile-supervisor-run 对账入账且 receipt_passed 存在。"""

    adapter = _adapter()
    operations = head.get("operations", {})
    try:
        reconciled = codex_upgrade_supervisor.verify_supervisor_run_reconciliation_binding(
            campaign_dir,
            campaign_id=str(manifest["campaign_id"]),
            run_id=str(failed["run_id"]),
            phase=str(failed["phase"]),
            batch_sequence=int(failed["batch_sequence"]),
            batch_sha256=str(failed["batch_sha256"]),
            label="evaluation-recover",
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    if reconciled["operation_id"] not in operations:
        raise adapter.orchestrator.ConfigurationError(
            f"失败父 run {failed['run_id']} 尚未对账入账；先执行 reconcile-supervisor-run。"
        )
    events = codex_upgrade_timing_ledger._load_events(ledger_dir)
    expected_event_id = f"reconcile-run-passed-{failed['run_id']}"
    matches = [event for event, _raw in events if event.get("event_id") == expected_event_id]
    if len(matches) != 1 or matches[0].get("event_type") != "receipt_passed":
        raise adapter.orchestrator.ConfigurationError(
            f"失败父 run {failed['run_id']} 的 receipt_passed 事件不存在或不唯一；对账尚未恢复账本。"
        )
    return reconciled


def evaluation_recover(arguments: argparse.Namespace) -> dict[str, Any]:
    """evaluation-recover preview／apply／abandon：候选证据封存后离线评估失败的分类与局部恢复（零请求）。

    preview：admission → 候选属当前 revision → 当前基线与失败父 run 定位 → 失败来源／复用授权／
    failure-scope → 诊断草案（绑定总账 head）→ review_sha256；不落盘。
    apply：复验 review_sha256 → 账本 active 且失败 run 已对账（receipt_passed）→ 类别准入 →
    ① diagnosis.json＋recovery.json＋PREPARED → ② outbox 根因事件（请求 0）→ 推总账 → 二次判定
    （停线即现有停线合同，基线停在 prepared）→ ③ AUTHORIZATION → ④ COMMIT（stage_sources）→
    ⑤ 账本 evaluation_baseline。每步 write-once 幂等，续作收敛。
    abandon：未 COMMIT 的 PREPARED 基线显式作废（编号不复用）。
    """

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    action = str(getattr(arguments, "recover_action", ""))
    if action not in {"preview", "apply", "abandon", "reevaluate", "approval-revision"}:
        raise adapter.orchestrator.ConfigurationError("evaluation-recover 只接受 preview、apply、abandon、reevaluate 或 approval-revision。")
    campaign_dir = Path(arguments.campaign_dir)
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest):
        raise adapter.orchestrator.ConfigurationError("evaluation-recover 只用于 0.154.0 起的完整 VC 链 Campaign。")
    candidate_id = str(arguments.candidate_id)
    if not adapter.orchestrator.SAFE_ID_RE.fullmatch(candidate_id):
        raise adapter.orchestrator.ConfigurationError("--candidate-id 格式非法。")
    reviewer = str(getattr(arguments, "reviewer", "") or "").strip()
    if not reviewer:
        raise adapter.orchestrator.ConfigurationError("--reviewer 不能为空。")
    approve_sha256 = getattr(arguments, "approve_sha256", None)
    root_cause_class = getattr(arguments, "root_cause_class", None)
    if action == "apply":
        if not (isinstance(approve_sha256, str) and adapter.orchestrator.SHA256_RE.fullmatch(approve_sha256)):
            raise adapter.orchestrator.ConfigurationError("apply 必须给出 preview 输出的 --approve-sha256。")
        if root_cause_class not in codex_upgrade_vc_artifacts.ROOT_CAUSE_CLASSES:
            raise adapter.orchestrator.ConfigurationError("apply 必须以 --root-cause-class 给出人工裁定的类别。")
    admission = adapter.orchestrator._assert_revision_admission(campaign_dir, manifest, command=EVALUATION_RECOVER_COMMAND)
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    campaign_root = campaign_dir.resolve(strict=True)
    if action == "approval-revision":
        # 第三批 R5：批准断言画像的 selector 修正在原 Campaign 内以 approval-revision 基线承接；--approve-sha256 缺省即预览。
        if approve_sha256 is not None and not (isinstance(approve_sha256, str) and adapter.orchestrator.SHA256_RE.fullmatch(approve_sha256)):
            raise adapter.orchestrator.ConfigurationError("approval-revision 的 --approve-sha256 必须是预览输出的 review_sha256。")
        try:
            return adapter.orchestrator._evaluation_approval_revision_locked(
                arguments, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, admission, ledger_dir, reconciler
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise adapter.orchestrator.ConfigurationError(f"评估基线制品非法：{error}") from error
    if action == "reevaluate":
        # 第三批 B3-3：不依赖失败父 run 的重评；--approve-sha256 缺省即预览，带上才落盘。
        if approve_sha256 is not None and not (isinstance(approve_sha256, str) and adapter.orchestrator.SHA256_RE.fullmatch(approve_sha256)):
            raise adapter.orchestrator.ConfigurationError("reevaluate 的 --approve-sha256 必须是预览输出的 review_sha256。")
        try:
            return adapter.orchestrator._evaluation_reevaluate_locked(
                arguments, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, admission, ledger_dir, reconciler
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise adapter.orchestrator.ConfigurationError(f"评估基线制品非法：{error}") from error
    try:
        return adapter.orchestrator._evaluation_recover_locked(
            arguments, action, campaign_dir, campaign_root, manifest, candidate_id, reviewer, approve_sha256, root_cause_class, admission, ledger_dir, reconciler
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise adapter.orchestrator.ConfigurationError(f"评估基线制品非法：{error}") from error


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
    """非失败类基线（approval-revision）二次判定通过后的 ③ AUTHORIZATION → ④ COMMIT → ⑤ 账本事件。

    与 reevaluate（tool-evolution）同一 write-once 形态：每步已存在即核对绑定、续作收敛；stage_sources 固定为候选
    证据 reused、compare／断言／accept 本基线 local（全量重评）。
    """

    adapter = _adapter()
    baseline_dir = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, number)
    authorization_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_AUTHORIZATION_FILENAME
    batch_commit_path = Path(str(batch["batch_dir"])) / "COMMIT"
    if authorization_path.is_file():
        authorization = codex_upgrade_vc_artifacts.validate_evaluation_baseline_authorization(
            adapter.orchestrator._read_json(authorization_path, "评估基线 AUTHORIZATION")
        )
        if authorization["recovery_sha256"] != recovery["recovery_sha256"]:
            raise adapter.orchestrator.ConfigurationError("AUTHORIZATION 绑定的 recovery 摘要与 recovery.json 不一致。")
    else:
        authorization = codex_upgrade_vc_artifacts.build_evaluation_baseline_authorization(
            campaign_id=str(manifest["campaign_id"]),
            candidate_id=candidate_id,
            candidate_revision=revision,
            evaluation_baseline=number,
            recovery_sha256=str(recovery["recovery_sha256"]),
            ledger_operation_id=operation_id,
            ledger_event_sha256=adapter.orchestrator.file_sha256(batch_commit_path),
            project_ledger_head_sequence=int(head_after["sequence"]),
            project_ledger_head_sha256=str(head_after["head_sha256"]),
            root_cause_id=root_cause_id,
            root_cause_count=int(decision.get("root_cause_count", 0)),
            authorized_at_utc=adapter.orchestrator._utc_now(),
        )
        adapter.orchestrator._secure_write_json_once(authorization_path, authorization)
    commit_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_COMMIT_FILENAME
    if commit_path.is_file():
        commit = codex_upgrade_vc_artifacts.validate_evaluation_baseline_commit(adapter.orchestrator._read_json(commit_path, "评估基线 COMMIT"))
    else:
        capture_source = adapter.orchestrator._stage_read_source(campaign_dir, candidate_id, current_baseline, "capture-candidate")
        stage_sources: dict[str, Any] = {
            "capture-candidate": {
                "source": "reused",
                "baseline": int(capture_source["baseline_of_record"]),
                "path": Path(capture_source["path"]).relative_to(campaign_dir).as_posix(),
                "sha256": str(capture_source["sha256"]),
            },
            "compare": {"source": "local", "target": f"comparisons/{candidate_id}/revisions/b{number}/result.json"},
            "assertions": {"source": "local", "target": f"assertions/{candidate_id}/revisions/b{number}"},
            "accept": {"source": "local", "target": f"acceptance/{candidate_id}/revisions/b{number}/result.json"},
        }
        commit = codex_upgrade_vc_artifacts.build_evaluation_baseline_commit(
            campaign_id=str(manifest["campaign_id"]),
            candidate_id=candidate_id,
            candidate_revision=revision,
            evaluation_baseline=number,
            kind=kind,
            recovery_sha256=str(recovery["recovery_sha256"]),
            authorization_sha256=str(authorization["authorization_sha256"]),
            stage_sources=stage_sources,
            committed_at_utc=adapter.orchestrator._utc_now(),
        )
        adapter.orchestrator._secure_write_json_once(commit_path, commit)
    last = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, ledger_event_type)
    if (
        last is not None
        and last[0].get("candidate_id") == candidate_id
        and last[0].get("evaluation_baseline") == number
        and last[0].get("baseline_commit_sha256") == commit["commit_sha256"]
    ):
        result["ledger_event"] = {"event_type": ledger_event_type, "appended": False}
    else:
        try:
            appended = codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"{ledger_event_type.replace('_', '-')}-{candidate_id}-r{revision}-b{number}",
                phase="VC-5",
                event_type=ledger_event_type,
                revision=revision,
                candidate_id=candidate_id,
                evaluation_baseline=number,
                baseline_commit_sha256=str(commit["commit_sha256"]),
                baseline_kind=kind,
                recovery_revision=None,
                next_action=next_action,
            )
        except codex_upgrade_timing_ledger.TimingLedgerError as error:
            raise adapter.orchestrator.ConfigurationError(f"账本登记 {ledger_event_type} 失败：{error}") from error
        result["ledger_event"] = {
            "event_type": ledger_event_type,
            "appended": True,
            "head_sequence": appended.get("head_sequence"),
            "status": appended.get("status"),
            "active_phase": appended.get("active_phase"),
        }
    result["next_command"] = f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}）"
    return result


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
    """第三批 R5：``evaluation-recover approval-revision``——批准断言画像的 selector 修正在原 Campaign 内开
    approval-revision 基线，按修订画像全量重评。候选 revision 不变、不重采；不是失败、不计根因；VC-5 已在本
    revision 完成时经 ``evaluation_reopened`` 重开。五步 write-once 与 reevaluate 同构，续作收敛。
    """

    adapter = _adapter()
    revised_source = getattr(arguments, "assertion_profile", None)
    if (
        revised_source is None
        or not Path(revised_source).is_absolute()
        or Path(revised_source).is_symlink()
        or not Path(revised_source).is_file()
    ):
        raise adapter.orchestrator.ConfigurationError("approval-revision 必须以 --assertion-profile 给出可信绝对路径的修订画像。")
    revised_source = Path(revised_source)
    reason = str(getattr(arguments, "reason", "") or "").strip() or "assertion selector revision"
    with adapter.orchestrator._campaign_lock(campaign_dir):
        summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        status = str(summary.get("status"))
        if status != "active":
            raise adapter.orchestrator.ConfigurationError(f"evaluation-recover approval-revision 拒绝：账本状态 {status}（须 active）。")
        revision, record = adapter.orchestrator._current_candidate_revision_record(campaign_dir, manifest)
        if revision is None:
            raise adapter.orchestrator.ConfigurationError("没有 active 候选 revision。")
        current_candidate = record["candidate_id"] if record is not None else adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
        if current_candidate != candidate_id:
            raise adapter.orchestrator.ConfigurationError(
                f"候选 {candidate_id} 不是当前 revision r{revision} 的候选（当前 {current_candidate}）。"
            )
        # 账本位置：VC-5 进行中直接切换基线；VC-5 已在本 revision 完成且 VC-6 未开始则重开；其余拒绝。
        active_phase = summary.get("active_phase")
        phase_states = (summary.get("revision_phase_state") or {}).get(str(revision), {})
        if active_phase == "VC-5":
            ledger_event_type = "evaluation_baseline"
        elif active_phase is None and phase_states.get("VC-5") == "completed" and "VC-6" not in phase_states:
            ledger_event_type = "evaluation_reopened"
        else:
            raise adapter.orchestrator.ConfigurationError(
                f"evaluation-recover approval-revision 拒绝：账本 active_phase={active_phase}、VC-5 状态 {phase_states.get('VC-5')}、"
                f"VC-6 状态 {phase_states.get('VC-6')}；只能在 VC-5 进行中，或 VC-5 已完成且 VC-6 未开始时修订。"
            )
        current_baseline, current_commit = adapter.orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
        states = adapter.orchestrator._evaluation_baseline_states(campaign_dir, candidate_id)
        pending = sorted(
            number
            for number, flags in states.items()
            if flags["prepared"] and not flags["abandon"] and (not flags["commit"] or number > current_baseline)
        )
        if len(pending) > 1:
            raise adapter.orchestrator.ConfigurationError(f"存在多个未 COMMIT 的 PREPARED 评估基线：{pending}，状态不可信。")
        resume_number = pending[-1] if pending else None
        if resume_number is not None:
            pending_recovery = adapter.orchestrator._load_evaluation_baseline_recovery(campaign_dir, candidate_id, resume_number)
            if pending_recovery["kind"] != "approval-revision":
                raise adapter.orchestrator.ConfigurationError(
                    f"存在未 COMMIT 的 PREPARED 评估基线 b{resume_number}（{pending_recovery['kind']}），先 apply 或 abandon 它。"
                )
        candidate = adapter.orchestrator._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
        if candidate.get("status") != "complete":
            raise adapter.orchestrator.ConfigurationError("候选阶段结果不是 complete；未封存的候选没有可修订的评估。")
        attempt_root, attempt = adapter.orchestrator._capture_stage_attempt_context(
            campaign_dir, candidate, phase="candidate", candidate_id=candidate_id
        )
        classification = adapter.orchestrator._load_stage_result(campaign_dir, "classify")
        if classification.get("status") != "complete":
            raise adapter.orchestrator.ConfigurationError("分类批准尚未完成，没有可修订的批准画像。")
        rules = adapter.orchestrator._approved_rules(campaign_dir, manifest, require_approved=True)
        official_authority = adapter.orchestrator._classification_official_authority(classification)
        # 生效画像：最近一次修订的画像，或分类收据的批准画像（封存后漂移即拒）。
        effective = adapter.orchestrator._effective_assertion_profile(campaign_dir, candidate_id, current_baseline)
        if effective is None:
            reference = classification.get("assertion_profile_manifest")
            if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
                raise adapter.orchestrator.ConfigurationError("分类收据缺少批准断言画像。")
            previous_profile = {"path": str(reference["path"]), "sha256": str(reference.get("sha256", ""))}
        else:
            previous_profile = {"path": effective["path"], "sha256": effective["sha256"]}
        previous_path = adapter.orchestrator._campaign_file(campaign_dir, previous_profile["path"])
        if previous_path.is_symlink() or not previous_path.is_file() or adapter.orchestrator.file_sha256(previous_path) != previous_profile["sha256"]:
            raise adapter.orchestrator.ConfigurationError("生效的批准断言画像在封存后漂移或丢失。")
        revised_sha256 = adapter.orchestrator.file_sha256(revised_source)
        if revised_sha256 == previous_profile["sha256"]:
            raise adapter.orchestrator.ConfigurationError("approval-revision 拒绝：修订画像与生效画像逐字节相同，没有修订。")
        delta = adapter.orchestrator._approval_revision_delta(previous_path, revised_source)
        # 工具口径不混入修订：评估器四项与证据语义必须等于当前基线口径，变化了先 reevaluate。
        try:
            current_digests = adapter.identity_policy.evaluator_dependency_digests()
        except adapter.identity_policy.ToolIdentityPolicyError as error:
            raise adapter.orchestrator.ConfigurationError(f"evaluator 依赖摘要无法计算：{error}") from error
        authorized_partial = adapter.orchestrator._authorized_evaluator_digests(campaign_dir, manifest, candidate_id, current_baseline)
        authorized = {
            field: str(authorized_partial.get(field, current_digests[field]))
            for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS
        }
        changed_fields = sorted(
            field for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS if authorized[field] != current_digests[field]
        )
        current_tool = adapter.orchestrator._tool_identity(include_git=False)
        current_evidence = str(current_tool["evidence_semantics_sha256"])
        frozen_identity = manifest.get("tool_identity") if isinstance(manifest.get("tool_identity"), Mapping) else {}
        frozen_evidence = str(frozen_identity.get("evidence_semantics_sha256", ""))
        if current_baseline == 0:
            baseline_evidence = frozen_evidence
        else:
            bound = adapter.orchestrator._load_evaluation_baseline_recovery(campaign_dir, candidate_id, current_baseline).get("evaluation_epoch")
            baseline_evidence = str(bound["to_evidence_semantics_sha256"]) if isinstance(bound, Mapping) else frozen_evidence
        if changed_fields or current_evidence != baseline_evidence:
            raise adapter.orchestrator.ConfigurationError(
                "approval-revision 拒绝：评估器摘要或证据语义相对当前评估基线已变化"
                f"（{'、'.join(changed_fields) or 'evidence_semantics'}）；先以 evaluation-recover reevaluate 承接工具变化，再修订画像。"
            )
        evaluation_epoch = adapter.orchestrator._evaluation_epoch_for_recovery(
            campaign_dir, manifest, attempt_root, current_evidence=current_evidence
        )
        effective_identity = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
        project_root = reconciler._project_root(campaign_root)
        plan, head = reconciler._project_facts(project_root)
        number = resume_number if resume_number is not None else (max(states) + 1 if states else 1)
        if number <= current_baseline:
            raise adapter.orchestrator.ConfigurationError("新评估基线编号必须大于当前基线。")
        baseline_dir = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, number)
        approval_index = len(adapter.orchestrator._approval_revision_baselines(campaign_dir, candidate_id)) + 1
        revised_profile = {
            "path": f"candidates/{candidate_id}/revisions/b{number}/{APPROVAL_REVISION_PROFILE_FILENAME}",
            "sha256": revised_sha256,
        }
        record_path = baseline_dir / adapter.orchestrator.APPROVAL_REVISION_FILENAME
        existing: dict[str, Any] | None = None
        if resume_number is not None:
            existing = codex_upgrade_vc_artifacts.validate_approval_revision(adapter.orchestrator._read_json(record_path, "批准修订记录"))
            if existing["revised_profile"] != revised_profile or existing["previous_profile"] != previous_profile:
                raise adapter.orchestrator.ConfigurationError(f"未 COMMIT 的 PREPARED 基线 b{resume_number} 绑定的画像与本次 --assertion-profile 不一致。")
            approval_record = existing
        else:
            approval_record = codex_upgrade_vc_artifacts.build_approval_revision(
                campaign_id=str(manifest["campaign_id"]),
                campaign_manifest_sha256=adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
                candidate_id=candidate_id,
                candidate_revision=revision,
                from_baseline=current_baseline,
                from_baseline_commit_sha256=(str(current_commit["commit_sha256"]) if current_commit is not None else None),
                approval_revision=approval_index,
                previous_profile=previous_profile,
                revised_profile=revised_profile,
                classification_package_digest=str(official_authority["classification_package_digest"]),
                joint_manifest_sha256=str(official_authority["review_sha256"]),
                contract_sha256=str(delta["contract_sha256"]),
                changed_rule_ids=delta["changed_rule_ids"],
                changed_check_ids=delta["changed_check_ids"],
                evaluator_digests=current_digests,
                tool_evolution_index=int(effective_identity["index"]),
                reason=reason,
                reviewer=reviewer,
                reviewed_at_utc=adapter.orchestrator._utc_now(),
            )
        review_sha256 = str(approval_record["review_sha256"])
        preview_payload: dict[str, Any] = {
            "status": "preview",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "current_evaluation_baseline": current_baseline,
            "pending_baseline": resume_number,
            "ledger_status": status,
            "ledger_event_type": ledger_event_type,
            "kind": "approval-revision",
            "approval_revision": approval_index,
            "previous_profile": previous_profile,
            "revised_profile": revised_profile,
            "changed_rule_ids": list(delta["changed_rule_ids"]),
            "changed_check_ids": list(delta["changed_check_ids"]),
            "record": approval_record,
            "review_sha256": review_sha256,
            "project_ledger": admission,
            "live_request_count": 0,
        }
        if approve_sha256 is None:
            return preview_payload
        if approve_sha256 != review_sha256:
            raise adapter.orchestrator.ConfigurationError(
                "approval-revision 的批准摘要与当前修订事实不一致（画像、基线或分类收据已变化），请重新预览。"
            )
        # 第三批 B3-5（第 7 项⑥）同一预检：写入前未登记的工具变化只提示先登记演进。
        adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="evaluation-recover approval-revision")
        # ① 修订画像逐字节封存＋批准修订记录＋recovery.json＋PREPARED（write-once；续作即校验一致）。
        ensure_private_directory(baseline_dir.parent, campaign_dir)
        ensure_private_directory(baseline_dir, campaign_dir)
        profile_destination = baseline_dir / APPROVAL_REVISION_PROFILE_FILENAME
        if profile_destination.exists() or profile_destination.is_symlink():
            if profile_destination.is_symlink() or not profile_destination.is_file() or adapter.orchestrator.file_sha256(profile_destination) != revised_sha256:
                raise adapter.orchestrator.ConfigurationError(f"评估基线 b{number} 已封存的修订画像与本次 --assertion-profile 不一致。")
        else:
            adapter.orchestrator._secure_copy_file_once(revised_source, profile_destination)
        if existing is None:
            adapter.orchestrator._secure_write_json_once(record_path, approval_record)
        cause_step = f"approval-revision-{approval_index:02d}"
        # 根因 ID 只用于 AUTHORIZATION 记录与二次判定的上限交集，不写进总账（修订不是失败、不计数）；复用评估器
        # 组件既有的错误码，failed_step 用修订序号区分——新增错误码要配总账迁移收据，本批不做。
        try:
            root_cause_id = codex_upgrade_root_cause.structured_root_cause(
                component="evaluator",
                stable_error_code="evaluation.rule-failed",
                failed_step=cause_step,
                stable_dimensions={"phase": "VC-5"},
            )
        except codex_upgrade_root_cause.RootCauseError as error:
            raise adapter.orchestrator.ConfigurationError(f"根因编码失败：{error}") from error
        recovery_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_RECOVERY_FILENAME
        if recovery_path.is_file():
            recovery = codex_upgrade_vc_artifacts.validate_evaluation_recovery(adapter.orchestrator._read_json(recovery_path, "评估基线 recovery"))
        else:
            recovery = codex_upgrade_vc_artifacts.build_evaluation_recovery(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                kind="approval-revision",
                diagnosis={"path": record_path.relative_to(campaign_dir).as_posix(), "sha256": adapter.orchestrator.file_sha256(record_path)},
                failure_source="approval-revision",
                reuse_authority="none",
                root_cause_class="approval-revision",
                root_cause_id=root_cause_id,
                failed_step=cause_step,
                previous_baseline=current_baseline,
                previous_baseline_commit_sha256=(str(current_commit["commit_sha256"]) if current_commit is not None else None),
                execute_rules=sorted(rules),
                reuse_rules=[],
                execute_jobs=[],
                reuse_jobs=sorted(
                    str(result.get("id"))
                    for result in attempt.get("results", [])
                    if isinstance(result, Mapping) and isinstance(result.get("id"), str)
                ),
                attempt_id=None,
                recovery_revision=None,
                fix_commit=None,
                deployment_receipt=None,
                evaluation_epoch=evaluation_epoch,
                failed_evaluator_digests=authorized,
                current_evaluator_digests=current_digests,
                reviewer=reviewer,
                approved_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(recovery_path, recovery)
        prepared_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_PREPARED_FILENAME
        if prepared_path.is_file():
            prepared = codex_upgrade_vc_artifacts.validate_evaluation_baseline_prepared(adapter.orchestrator._read_json(prepared_path, "评估基线 PREPARED"))
            if prepared["recovery_sha256"] != recovery["recovery_sha256"]:
                raise adapter.orchestrator.ConfigurationError("PREPARED 绑定的 recovery 摘要与 recovery.json 不一致。")
        else:
            prepared = codex_upgrade_vc_artifacts.build_evaluation_baseline_prepared(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                project_ledger_head_sequence=int(head["sequence"]),
                project_ledger_head_sha256=str(head["head_sha256"]),
                prepared_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(prepared_path, prepared)
        # ② outbox 事件（请求 0；修订不是失败，payload 不带 root_cause、不计根因）→ 推总账 → 重放 → 二次判定。
        receipt_binding = reconciler._binding(campaign_root, recovery_path, "reconciliation")
        operation_id = f"evaluation-approval-revision:{candidate_id}:r{revision}:b{number}"
        try:
            batch = reconciler._commit_batch(
                campaign_root,
                operation_id=operation_id,
                event_type="reconciliation_committed",
                payload={
                    "campaign_id": str(manifest["campaign_id"]),
                    "subject_kind": "evaluation_baseline",
                    "subject_id": f"{candidate_id}:r{revision}:b{number}",
                    "phase": "VC-5",
                    "request": dict(reconciler.ZERO_REQUEST_PART),
                    "reconciliation_receipt_sha256": receipt_binding["sha256"],
                    "attempt_failed_event_sha256": None,
                    "approval_revision": {
                        "evaluation_baseline": number,
                        "approval_revision": approval_index,
                        "previous_profile_sha256": previous_profile["sha256"],
                        "revised_profile_sha256": revised_sha256,
                        "changed_rule_ids": list(delta["changed_rule_ids"]),
                    },
                },
                source={"kind": "evaluation_baseline", "sha256": receipt_binding["sha256"]},
                receipt_bindings=[receipt_binding],
            )
            observed = adapter.orchestrator._utc_now()
            pushed, head_after = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            identity = reconciler._identity_facts(campaign_root, manifest, reconciler._current_identity())
            ledger_facts = reconciler._ledger_facts(ledger_dir, now=observed)
            environment_decision = adapter.orchestrator._campaign_environment_decision(campaign_dir, _manifest=manifest)
            decision = reconciler._decide(
                head=head_after,
                plan=plan,
                ledger=ledger_facts,
                identity=identity,
                environment_status=reconciler._environment_decision_status(environment_decision),
                campaign_deadline_at_utc=adapter.orchestrator._campaign_plan_deadline(campaign_dir),
                root_cause_id=root_cause_id,
                request_status="resolved",
                now=observed,
                campaign_id=str(manifest["campaign_id"]),
            )
        except reconciler.ReconcilerError as error:
            raise adapter.orchestrator.ConfigurationError(f"批准修订入账失败：{error}") from error
        result: dict[str, Any] = {
            "status": "applied",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "evaluation_baseline": number,
            "kind": "approval-revision",
            "approval_revision": approval_index,
            "ledger_event_type": ledger_event_type,
            "recovery_sha256": recovery["recovery_sha256"],
            "execute_rules": list(recovery["execute_rules"]),
            "reuse_rules": [],
            "execute_jobs": [],
            "reuse_jobs": list(recovery["reuse_jobs"]),
            "attempt_id": None,
            "recovery_revision": None,
            "root_cause_id": root_cause_id,
            "previous_profile": previous_profile,
            "revised_profile": revised_profile,
            "changed_rule_ids": list(delta["changed_rule_ids"]),
            "changed_check_ids": list(delta["changed_check_ids"]),
            "batch": batch,
            "project_push": pushed,
            "decision": decision,
            "live_request_count": 0,
        }
        if decision["decision"] == reconciler.DECISION_PAUSED:
            result["status"] = "paused"
            result["next_command"] = "预算已暂停：" + reconciler._paused_next_command(
                decision, "以同一批准摘要重新执行 evaluation-recover approval-revision（幂等续接）"
            )
            return result
        if decision["decision"] != reconciler.DECISION_RECOVERABLE:
            try:
                stop = reconciler._permanent_stop(
                    campaign_root,
                    manifest,
                    ledger_dir,
                    subject_id=f"evaluation-baseline-{candidate_id}-r{revision}-b{number}",
                    root_cause_id=root_cause_id,
                    terminal_reason=str(decision["terminal_reason"]),
                    receipt_bindings=[receipt_binding],
                    ledger_receipts=[],
                    live_request_count=0,
                    reconciliation_receipt_sha256=receipt_binding["sha256"],
                    ledger_facts=ledger_facts,
                )
                pushed_terminal, head_terminal = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(f"批准修订二次判定停线收口失败：{error}") from error
            result["status"] = "permanent_stop"
            result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
            result["next_command"] = stop["next_action"]
            return result
        # ③ AUTHORIZATION → ④ COMMIT → ⑤ 账本（与 reevaluate 同构）。
        return adapter.orchestrator._authorize_and_commit_non_failure_baseline(
            campaign_dir,
            manifest,
            candidate_id=candidate_id,
            revision=revision,
            number=number,
            current_baseline=current_baseline,
            kind="approval-revision",
            recovery=recovery,
            batch=batch,
            head_after=head_after,
            decision=decision,
            root_cause_id=root_cause_id,
            operation_id=operation_id,
            ledger_dir=ledger_dir,
            ledger_event_type=ledger_event_type,
            next_action=f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}，按修订画像 a{approval_index} 全量重评）",
            result=result,
        )


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
    """第三批 B3-3：``evaluation-recover reevaluate``——评估器／证据层修好并登记演进后，对已有评估产出的候选开
    tool-evolution 基线按新工具全量重评。不依赖失败父 run，不计根因；VC-5 已在本 revision 完成时经
    ``evaluation_reopened`` 重开。五步 write-once 与 evaluator-defect 同构，续作收敛。
    """

    adapter = _adapter()
    with adapter.orchestrator._campaign_lock(campaign_dir):
        summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        status = str(summary.get("status"))
        if status != "active":
            raise adapter.orchestrator.ConfigurationError(f"evaluation-recover reevaluate 拒绝：账本状态 {status}（须 active）。")
        revision, record = adapter.orchestrator._current_candidate_revision_record(campaign_dir, manifest)
        if revision is None:
            raise adapter.orchestrator.ConfigurationError("没有 active 候选 revision。")
        current_candidate = record["candidate_id"] if record is not None else adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
        if current_candidate != candidate_id:
            raise adapter.orchestrator.ConfigurationError(
                f"候选 {candidate_id} 不是当前 revision r{revision} 的候选（当前 {current_candidate}）。"
            )
        # 账本位置：VC-5 进行中直接切换基线；VC-5 已在本 revision 完成且 VC-6 未开始则重开；其余拒绝。
        active_phase = summary.get("active_phase")
        phase_states = (summary.get("revision_phase_state") or {}).get(str(revision), {})
        if active_phase == "VC-5":
            ledger_event_type = "evaluation_baseline"
        elif active_phase is None and phase_states.get("VC-5") == "completed" and "VC-6" not in phase_states:
            ledger_event_type = "evaluation_reopened"
        else:
            raise adapter.orchestrator.ConfigurationError(
                f"evaluation-recover reevaluate 拒绝：账本 active_phase={active_phase}、VC-5 状态 {phase_states.get('VC-5')}、"
                f"VC-6 状态 {phase_states.get('VC-6')}；只能在 VC-5 进行中，或 VC-5 已完成且 VC-6 未开始时重评。"
            )
        current_baseline, current_commit = adapter.orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
        states = adapter.orchestrator._evaluation_baseline_states(campaign_dir, candidate_id)
        pending = sorted(
            number
            for number, flags in states.items()
            if flags["prepared"] and not flags["abandon"] and (not flags["commit"] or number > current_baseline)
        )
        if len(pending) > 1:
            raise adapter.orchestrator.ConfigurationError(f"存在多个未 COMMIT 的 PREPARED 评估基线：{pending}，状态不可信。")
        resume_number = pending[-1] if pending else None
        if resume_number is not None:
            pending_recovery = adapter.orchestrator._load_evaluation_baseline_recovery(campaign_dir, candidate_id, resume_number)
            if pending_recovery["kind"] != "tool-evolution":
                raise adapter.orchestrator.ConfigurationError(
                    f"存在未 COMMIT 的 PREPARED 评估基线 b{resume_number}（{pending_recovery['kind']}），先 apply 或 abandon 它。"
                )
        candidate = adapter.orchestrator._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
        if candidate.get("status") != "complete":
            raise adapter.orchestrator.ConfigurationError("候选阶段结果不是 complete；未封存的候选没有需要重评的评估产出。")
        attempt_root, attempt = adapter.orchestrator._capture_stage_attempt_context(
            campaign_dir, candidate, phase="candidate", candidate_id=candidate_id
        )
        rules = adapter.orchestrator._approved_rules(campaign_dir, manifest, require_approved=True)
        outputs = adapter.orchestrator._evaluation_baseline_has_outputs(campaign_dir, candidate_id, current_baseline)
        if not outputs:
            raise adapter.orchestrator.ConfigurationError(
                f"当前评估基线 b{current_baseline} 还没有任何评估产出，不需要重评；直接派发 VC-5 评估批次即可。"
            )
        # 触发事实：基线授权的评估器口径 vs 当前工具；基线绑定的证据语义 vs 当前工具。
        try:
            current_digests = adapter.identity_policy.evaluator_dependency_digests()
        except adapter.identity_policy.ToolIdentityPolicyError as error:
            raise adapter.orchestrator.ConfigurationError(f"evaluator 依赖摘要无法计算：{error}") from error
        authorized_partial = adapter.orchestrator._authorized_evaluator_digests(campaign_dir, manifest, candidate_id, current_baseline)
        # b0 只对 checker／builder 设口径：reader 两项按当前值补齐，不构成差异。
        authorized = {
            field: str(authorized_partial.get(field, current_digests[field]))
            for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS
        }
        changed_fields = sorted(
            field for field in codex_upgrade_vc_artifacts.EVALUATOR_DIGEST_FIELDS if authorized[field] != current_digests[field]
        )
        current_tool = adapter.orchestrator._tool_identity(include_git=False)
        current_evidence = str(current_tool["evidence_semantics_sha256"])
        frozen_identity = manifest.get("tool_identity") if isinstance(manifest.get("tool_identity"), Mapping) else {}
        frozen_evidence = str(frozen_identity.get("evidence_semantics_sha256", ""))
        if current_baseline == 0:
            baseline_evidence = frozen_evidence
        else:
            bound = adapter.orchestrator._load_evaluation_baseline_recovery(campaign_dir, candidate_id, current_baseline).get("evaluation_epoch")
            baseline_evidence = str(bound["to_evidence_semantics_sha256"]) if isinstance(bound, Mapping) else frozen_evidence
        evidence_changed = current_evidence != baseline_evidence
        if not changed_fields and not evidence_changed:
            raise adapter.orchestrator.ConfigurationError("评估器四项摘要与证据语义相对当前评估基线都没有变化，没有需要重评的理由。")
        # 与 evaluator-defect 同一 A2 合同：证据语义变化后须先对候选 attempt 追加 evaluation-epoch。
        evaluation_epoch = adapter.orchestrator._evaluation_epoch_for_recovery(
            campaign_dir, manifest, attempt_root, current_evidence=current_evidence
        )
        effective = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
        project_root = reconciler._project_root(campaign_root)
        plan, head = reconciler._project_facts(project_root)
        existing: dict[str, Any] | None = None
        if resume_number is not None:
            existing = codex_upgrade_vc_artifacts.validate_evaluation_reevaluation(
                adapter.orchestrator._read_json(
                    adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, resume_number) / EVALUATION_REEVALUATION_FILENAME,
                    "评估重评触发事实",
                )
            )
            reevaluation = existing
        else:
            reevaluation = codex_upgrade_vc_artifacts.build_evaluation_reevaluation(
                campaign_id=str(manifest["campaign_id"]),
                campaign_manifest_sha256=adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
                candidate_id=candidate_id,
                candidate_revision=revision,
                from_baseline=current_baseline,
                from_baseline_commit_sha256=(str(current_commit["commit_sha256"]) if current_commit is not None else None),
                tool_evolution_index=int(effective["index"]),
                evaluator_changed_fields=changed_fields,
                evidence_from_sha256=baseline_evidence,
                evidence_to_sha256=current_evidence,
                authorized_evaluator_digests=authorized,
                current_evaluator_digests=current_digests,
                evaluation_outputs=outputs,
                reviewer=reviewer,
                reviewed_at_utc=adapter.orchestrator._utc_now(),
            )
        review_sha256 = str(reevaluation["review_sha256"])
        preview_payload: dict[str, Any] = {
            "status": "preview",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "current_evaluation_baseline": current_baseline,
            "pending_baseline": resume_number,
            "ledger_status": status,
            "ledger_event_type": ledger_event_type,
            "kind": "tool-evolution",
            "trigger": {
                "evaluator_changed_fields": changed_fields,
                "evidence_changed": evidence_changed,
                "evidence_from_sha256": baseline_evidence,
                "evidence_to_sha256": current_evidence,
                "tool_evolution_index": int(effective["index"]),
                "evaluation_outputs": outputs,
            },
            "reevaluation": reevaluation,
            "review_sha256": review_sha256,
            "project_ledger": admission,
            "live_request_count": 0,
        }
        if approve_sha256 is None:
            return preview_payload
        if approve_sha256 != review_sha256:
            raise adapter.orchestrator.ConfigurationError(
                "reevaluate 的批准摘要与当前触发事实不一致（评估器摘要、证据语义或当前基线已变化），请重新预览。"
            )
        # 第三批 B3-5（第 7 项⑥）：写入前零写入预检，未登记的工具变化只提示先登记演进。
        adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="evaluation-recover reevaluate")
        fix_commit = getattr(arguments, "fix_commit", None)
        receipt_path = getattr(arguments, "deployment_receipt", None)
        if not isinstance(fix_commit, str) or not re.fullmatch(r"^[0-9a-f]{40}$", fix_commit):
            raise adapter.orchestrator.ConfigurationError("reevaluate 落盘必须以 --fix-commit 给出完整 40 位修复提交。")
        if (
            receipt_path is None
            or not Path(receipt_path).is_absolute()
            or Path(receipt_path).is_symlink()
            or not Path(receipt_path).is_file()
        ):
            raise adapter.orchestrator.ConfigurationError("reevaluate 落盘必须以 --deployment-receipt 给出可信绝对路径的部署收据。")
        adapter.orchestrator._verify_current_deployment_receipt(campaign_dir, manifest, Path(receipt_path))
        deployment_binding = {
            "path": str(Path(receipt_path).resolve(strict=True)),
            "sha256": adapter.orchestrator.file_sha256(Path(receipt_path)),
        }
        # ① 编号与触发事实／recovery.json／PREPARED（write-once；续作即校验一致）。
        number = resume_number if resume_number is not None else (max(states) + 1 if states else 1)
        if number <= current_baseline:
            raise adapter.orchestrator.ConfigurationError("新评估基线编号必须大于当前基线。")
        baseline_dir = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, number)
        ensure_private_directory(baseline_dir.parent, campaign_dir)
        ensure_private_directory(baseline_dir, campaign_dir)
        reevaluation_path = baseline_dir / EVALUATION_REEVALUATION_FILENAME
        if existing is None:
            adapter.orchestrator._secure_write_json_once(reevaluation_path, reevaluation)
        cause_step = f"tool-evolution-{int(effective['index']):02d}"
        # 根因 ID 只用于 AUTHORIZATION 记录与二次判定的上限交集，不写进总账（重评不是失败、不计数）；复用评估器
        # 组件既有的错误码，failed_step 用演进序号与真实失败规则（SPEC-*）区分——新增错误码要配总账迁移收据，本批不做。
        try:
            root_cause_id = codex_upgrade_root_cause.structured_root_cause(
                component="evaluator",
                stable_error_code="evaluation.rule-failed",
                failed_step=cause_step,
                stable_dimensions={"phase": "VC-5"},
            )
        except codex_upgrade_root_cause.RootCauseError as error:
            raise adapter.orchestrator.ConfigurationError(f"根因编码失败：{error}") from error
        recovery_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_RECOVERY_FILENAME
        if recovery_path.is_file():
            recovery = codex_upgrade_vc_artifacts.validate_evaluation_recovery(adapter.orchestrator._read_json(recovery_path, "评估基线 recovery"))
        else:
            recovery = codex_upgrade_vc_artifacts.build_evaluation_recovery(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                kind="tool-evolution",
                diagnosis={"path": reevaluation_path.relative_to(campaign_dir).as_posix(), "sha256": adapter.orchestrator.file_sha256(reevaluation_path)},
                failure_source="tool-evolution",
                reuse_authority="none",
                root_cause_class="tool-evolution",
                root_cause_id=root_cause_id,
                failed_step=cause_step,
                previous_baseline=current_baseline,
                previous_baseline_commit_sha256=(str(current_commit["commit_sha256"]) if current_commit is not None else None),
                execute_rules=sorted(rules),
                reuse_rules=[],
                execute_jobs=[],
                reuse_jobs=sorted(
                    str(result.get("id"))
                    for result in attempt.get("results", [])
                    if isinstance(result, Mapping) and isinstance(result.get("id"), str)
                ),
                attempt_id=None,
                recovery_revision=None,
                fix_commit=fix_commit,
                deployment_receipt=deployment_binding,
                evaluation_epoch=evaluation_epoch,
                failed_evaluator_digests=authorized,
                current_evaluator_digests=current_digests,
                reviewer=reviewer,
                approved_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(recovery_path, recovery)
        prepared_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_PREPARED_FILENAME
        if prepared_path.is_file():
            prepared = codex_upgrade_vc_artifacts.validate_evaluation_baseline_prepared(adapter.orchestrator._read_json(prepared_path, "评估基线 PREPARED"))
            if prepared["recovery_sha256"] != recovery["recovery_sha256"]:
                raise adapter.orchestrator.ConfigurationError("PREPARED 绑定的 recovery 摘要与 recovery.json 不一致。")
        else:
            prepared = codex_upgrade_vc_artifacts.build_evaluation_baseline_prepared(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                project_ledger_head_sequence=int(head["sequence"]),
                project_ledger_head_sha256=str(head["head_sha256"]),
                prepared_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(prepared_path, prepared)
        # ② outbox 事件（请求 0；重评不是失败，payload 不带 root_cause、不计根因）→ 推总账 → 重放 → 二次判定。
        receipt_binding = reconciler._binding(campaign_root, recovery_path, "reconciliation")
        operation_id = f"evaluation-reevaluate:{candidate_id}:r{revision}:b{number}"
        try:
            batch = reconciler._commit_batch(
                campaign_root,
                operation_id=operation_id,
                event_type="reconciliation_committed",
                payload={
                    "campaign_id": str(manifest["campaign_id"]),
                    "subject_kind": "evaluation_baseline",
                    "subject_id": f"{candidate_id}:r{revision}:b{number}",
                    "phase": "VC-5",
                    "request": dict(reconciler.ZERO_REQUEST_PART),
                    "reconciliation_receipt_sha256": receipt_binding["sha256"],
                    "attempt_failed_event_sha256": None,
                    "tool_evolution_reevaluation": {
                        "evaluation_baseline": number,
                        "tool_evolution_index": int(effective["index"]),
                        "evaluator_changed_fields": changed_fields,
                        "evidence_changed": evidence_changed,
                    },
                },
                source={"kind": "evaluation_baseline", "sha256": receipt_binding["sha256"]},
                receipt_bindings=[receipt_binding],
            )
            observed = adapter.orchestrator._utc_now()
            pushed, head_after = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            identity = reconciler._identity_facts(campaign_root, manifest, reconciler._current_identity())
            ledger_facts = reconciler._ledger_facts(ledger_dir, now=observed)
            environment_decision = adapter.orchestrator._campaign_environment_decision(campaign_dir, _manifest=manifest)
            decision = reconciler._decide(
                head=head_after,
                plan=plan,
                ledger=ledger_facts,
                identity=identity,
                environment_status=reconciler._environment_decision_status(environment_decision),
                campaign_deadline_at_utc=adapter.orchestrator._campaign_plan_deadline(campaign_dir),
                root_cause_id=root_cause_id,
                request_status="resolved",
                now=observed,
                campaign_id=str(manifest["campaign_id"]),
            )
        except reconciler.ReconcilerError as error:
            raise adapter.orchestrator.ConfigurationError(f"评估重评入账失败：{error}") from error
        result: dict[str, Any] = {
            "status": "applied",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "evaluation_baseline": number,
            "kind": "tool-evolution",
            "ledger_event_type": ledger_event_type,
            "recovery_sha256": recovery["recovery_sha256"],
            "execute_rules": list(recovery["execute_rules"]),
            "reuse_rules": [],
            "execute_jobs": [],
            "reuse_jobs": list(recovery["reuse_jobs"]),
            "attempt_id": None,
            "recovery_revision": None,
            "root_cause_id": root_cause_id,
            "trigger": preview_payload["trigger"],
            "batch": batch,
            "project_push": pushed,
            "decision": decision,
            "live_request_count": 0,
        }
        if decision["decision"] == reconciler.DECISION_PAUSED:
            result["status"] = "paused"
            result["next_command"] = "预算已暂停：" + reconciler._paused_next_command(
                decision, "以同一批准摘要重新执行 evaluation-recover reevaluate（幂等续接）"
            )
            return result
        if decision["decision"] != reconciler.DECISION_RECOVERABLE:
            try:
                stop = reconciler._permanent_stop(
                    campaign_root,
                    manifest,
                    ledger_dir,
                    subject_id=f"evaluation-baseline-{candidate_id}-r{revision}-b{number}",
                    root_cause_id=root_cause_id,
                    terminal_reason=str(decision["terminal_reason"]),
                    receipt_bindings=[receipt_binding],
                    ledger_receipts=[],
                    live_request_count=0,
                    reconciliation_receipt_sha256=receipt_binding["sha256"],
                    ledger_facts=ledger_facts,
                )
                pushed_terminal, head_terminal = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(f"评估重评二次判定停线收口失败：{error}") from error
            result["status"] = "permanent_stop"
            result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
            result["next_command"] = stop["next_action"]
            return result
        # ③ AUTHORIZATION（绑定 outbox 事件摘要、head 与判定；根因计数为 0）。
        authorization_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_AUTHORIZATION_FILENAME
        batch_commit_path = Path(str(batch["batch_dir"])) / "COMMIT"
        if authorization_path.is_file():
            authorization = codex_upgrade_vc_artifacts.validate_evaluation_baseline_authorization(
                adapter.orchestrator._read_json(authorization_path, "评估基线 AUTHORIZATION")
            )
            if authorization["recovery_sha256"] != recovery["recovery_sha256"]:
                raise adapter.orchestrator.ConfigurationError("AUTHORIZATION 绑定的 recovery 摘要与 recovery.json 不一致。")
        else:
            authorization = codex_upgrade_vc_artifacts.build_evaluation_baseline_authorization(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                ledger_operation_id=operation_id,
                ledger_event_sha256=adapter.orchestrator.file_sha256(batch_commit_path),
                project_ledger_head_sequence=int(head_after["sequence"]),
                project_ledger_head_sha256=str(head_after["head_sha256"]),
                root_cause_id=root_cause_id,
                root_cause_count=int(decision.get("root_cause_count", 0)),
                authorized_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(authorization_path, authorization)
        # ④ COMMIT（stage_sources：候选证据 reused；compare／断言／accept 全部本基线 local——全量重评）。
        commit_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_COMMIT_FILENAME
        if commit_path.is_file():
            commit = codex_upgrade_vc_artifacts.validate_evaluation_baseline_commit(adapter.orchestrator._read_json(commit_path, "评估基线 COMMIT"))
        else:
            capture_source = adapter.orchestrator._stage_read_source(campaign_dir, candidate_id, current_baseline, "capture-candidate")
            stage_sources: dict[str, Any] = {
                "capture-candidate": {
                    "source": "reused",
                    "baseline": int(capture_source["baseline_of_record"]),
                    "path": Path(capture_source["path"]).relative_to(campaign_dir).as_posix(),
                    "sha256": str(capture_source["sha256"]),
                },
                "compare": {"source": "local", "target": f"comparisons/{candidate_id}/revisions/b{number}/result.json"},
                "assertions": {"source": "local", "target": f"assertions/{candidate_id}/revisions/b{number}"},
                "accept": {"source": "local", "target": f"acceptance/{candidate_id}/revisions/b{number}/result.json"},
            }
            commit = codex_upgrade_vc_artifacts.build_evaluation_baseline_commit(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                kind="tool-evolution",
                recovery_sha256=str(recovery["recovery_sha256"]),
                authorization_sha256=str(authorization["authorization_sha256"]),
                stage_sources=stage_sources,
                committed_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(commit_path, commit)
        # ⑤ 账本：VC-5 进行中登记 evaluation_baseline；VC-5 已完成登记 evaluation_reopened（重开）。幂等：最后一条已引用本 COMMIT。
        last = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, ledger_event_type)
        if (
            last is not None
            and last[0].get("candidate_id") == candidate_id
            and last[0].get("evaluation_baseline") == number
            and last[0].get("baseline_commit_sha256") == commit["commit_sha256"]
        ):
            result["ledger_event"] = {"event_type": ledger_event_type, "appended": False}
        else:
            try:
                appended = codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=f"{ledger_event_type.replace('_', '-')}-{candidate_id}-r{revision}-b{number}",
                    phase="VC-5",
                    event_type=ledger_event_type,
                    revision=revision,
                    candidate_id=candidate_id,
                    evaluation_baseline=number,
                    baseline_commit_sha256=str(commit["commit_sha256"]),
                    baseline_kind="tool-evolution",
                    recovery_revision=None,
                    next_action=f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}，按新工具全量重评）",
                )
            except codex_upgrade_timing_ledger.TimingLedgerError as error:
                raise adapter.orchestrator.ConfigurationError(f"账本登记 {ledger_event_type} 失败：{error}") from error
            result["ledger_event"] = {
                "event_type": ledger_event_type,
                "appended": True,
                "head_sequence": appended.get("head_sequence"),
                "status": appended.get("status"),
                "active_phase": appended.get("active_phase"),
            }
        result["next_command"] = f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}）"
        return result


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
    adapter = _adapter()
    with adapter.orchestrator._campaign_lock(campaign_dir):
        summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
        status = str(summary.get("status"))
        allowed = {"active", "recovery_required"} if action == "preview" else {"active"}
        if status not in allowed:
            raise adapter.orchestrator.ConfigurationError(
                f"evaluation-recover {action} 拒绝：账本状态 {status}"
                + ("；先执行 reconcile-supervisor-run 使账本回到 active" if status == "recovery_required" else "")
                + "。"
            )
        revision, record = adapter.orchestrator._current_candidate_revision_record(campaign_dir, manifest)
        if revision is None:
            raise adapter.orchestrator.ConfigurationError("没有 active 候选 revision。")
        current_candidate = record["candidate_id"] if record is not None else adapter.orchestrator._implicit_r1_candidate_id(campaign_dir, manifest)
        if current_candidate != candidate_id:
            raise adapter.orchestrator.ConfigurationError(
                f"候选 {candidate_id} 不是当前 revision r{revision} 的候选（当前 {current_candidate}）。"
            )
        current_baseline, current_commit = adapter.orchestrator._current_evaluation_baseline(campaign_dir, candidate_id)
        states = adapter.orchestrator._evaluation_baseline_states(campaign_dir, candidate_id)
        # 续作对象：PREPARED 且未 ABANDON，且尚未成为当前基线（无 COMMIT，或 COMMIT 已写但账本未引用＝E5）。
        pending = sorted(
            number
            for number, flags in states.items()
            if flags["prepared"] and not flags["abandon"] and (not flags["commit"] or number > current_baseline)
        )
        if action == "abandon":
            abandonable = [number for number in pending if not states[number]["commit"]]
            if not abandonable:
                raise adapter.orchestrator.ConfigurationError("没有未 COMMIT 的 PREPARED 评估基线可作废。")
            number = abandonable[-1]
            baseline_dir = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, number)
            recovery = codex_upgrade_vc_artifacts.validate_evaluation_recovery(
                adapter.orchestrator._read_json(baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_RECOVERY_FILENAME, "评估基线 recovery")
            )
            reason = str(getattr(arguments, "reason", "") or "").strip() or "operator-abandoned"
            abandon = codex_upgrade_vc_artifacts.build_evaluation_baseline_abandon(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                reason=reason,
                abandoned_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_ABANDON_FILENAME, abandon)
            return {
                "status": "abandoned",
                "campaign_id": manifest["campaign_id"],
                "candidate_id": candidate_id,
                "evaluation_baseline": number,
                "abandon_sha256": abandon["abandon_sha256"],
                "current_evaluation_baseline": current_baseline,
                "live_request_count": 0,
            }
        candidate = adapter.orchestrator._load_stage_result(campaign_dir, "capture-candidate", candidate_id)
        if candidate.get("status") != "complete":
            raise adapter.orchestrator.ConfigurationError("当前基线的候选阶段结果不是 complete；封存前的失败走 resume --rerun-failed。")
        attempt_root, attempt = adapter.orchestrator._capture_stage_attempt_context(
            campaign_dir, candidate, phase="candidate", candidate_id=candidate_id
        )
        classification = adapter.orchestrator._load_stage_result(campaign_dir, "classify")
        # 第三批 R5：失败诊断的机器命令重建与规则集按当前基线生效的批准画像走（approval-revision 基线下取修订画像）。
        classification = adapter.orchestrator._effective_classification_view(campaign_dir, candidate_id, classification)
        rules = adapter.orchestrator._approved_rules(campaign_dir, manifest, require_approved=True)
        project_root = reconciler._project_root(campaign_root)
        plan, head = reconciler._project_facts(project_root)
        # 续作：已有 PREPARED 未 COMMIT 的基线以其落盘诊断为准（head 绑定的是当时的总账）。
        resume_number = pending[-1] if pending else None
        if len(pending) > 1:
            raise adapter.orchestrator.ConfigurationError(f"存在多个未 COMMIT 的 PREPARED 评估基线：{pending}，状态不可信。")
        facts = adapter.orchestrator._evaluation_failure_diagnosis_facts(
            campaign_dir, manifest, candidate_id, revision, current_baseline,
            candidate=candidate, attempt=attempt, classification=classification, rules=rules,
        )
        existing_diagnosis: dict[str, Any] | None = None
        if resume_number is not None:
            diagnosis_path = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, resume_number) / EVALUATION_BASELINE_DIAGNOSIS_FILENAME
            existing_diagnosis = codex_upgrade_vc_artifacts.validate_evaluation_failure_diagnosis(
                adapter.orchestrator._read_json(diagnosis_path, "评估失败诊断")
            )
            diagnosis = existing_diagnosis
        else:
            try:
                diagnosis = codex_upgrade_vc_artifacts.build_evaluation_failure_diagnosis(
                    campaign_id=str(manifest["campaign_id"]),
                    campaign_manifest_sha256=adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
                    candidate_id=candidate_id,
                    candidate_revision=revision,
                    evaluation_baseline=current_baseline,
                    failure_source=facts["failure_source"],
                    reuse_authority=facts["reuse_authority"],
                    failed_step=facts["failed_step"],
                    failed_run=facts["failed_run"],
                    action_outputs=facts["action_outputs"],
                    evaluation_run=facts["evaluation_run"],
                    failure_scope=facts["failure_scope"],
                    failed_evaluator_digests=facts["failed_evaluator_digests"],
                    current_evaluator_digests=facts["current_evaluator_digests"],
                    admissible_classes=facts["admissible_classes"],
                    project_ledger_head_sequence=int(head["sequence"]),
                    project_ledger_head_sha256=str(head["head_sha256"]),
                    reviewer=reviewer,
                    reviewed_at_utc=adapter.orchestrator._utc_now(),
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(f"诊断草案无法生成：{error}") from error
        review_sha256 = str(diagnosis["review_sha256"])
        preview_payload = {
            "status": "preview",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "current_evaluation_baseline": current_baseline,
            "pending_baseline": resume_number,
            "ledger_status": status,
            "failed_run": facts["failed_run"],
            "failure_source": facts["failure_source"],
            "reuse_authority": facts["reuse_authority"],
            "failed_step": facts["failed_step"],
            "failure_scope": facts["failure_scope"],
            "admissible_classes": facts["admissible_classes"],
            "diagnosis": diagnosis,
            "review_sha256": review_sha256,
            "project_ledger": admission,
            "live_request_count": 0,
        }
        if action == "preview":
            return preview_payload
        if approve_sha256 != review_sha256:
            raise adapter.orchestrator.ConfigurationError(
                "apply 的批准摘要与当前草案不一致（总账 head、失败事实或评估摘要已变化），请重新 preview。"
            )
        if root_cause_class not in facts["admissible_classes"]:
            raise adapter.orchestrator.ConfigurationError(f"类别 {root_cause_class} 不在本次失败允许的裁定集合 {facts['admissible_classes']} 内。")
        if root_cause_class == "candidate-source":
            return {**preview_payload, "status": "redirect", "next_command": f"invalidate-candidate preview --candidate-id {candidate_id}"}
        if root_cause_class == "approval-inputs":
            # 第三批 R5：批准断言画像的 selector 修正在原 Campaign 内承接；规则集／validation mode／场景／版本变化仍要建后继。
            return {
                **preview_payload,
                "status": "redirect",
                "next_command": (
                    "断言画像 selector 修正：evaluation-recover approval-revision --assertion-profile <修订画像>（原 Campaign 内开"
                    " approval-revision 基线）；规则集／validation mode／场景／版本变化：close-campaign-ledger（显式停线）后从 VC-2 建后继 Campaign"
                ),
            }
        # 第三批 B3-5（第 7 项⑥）：写入前零写入预检，未登记的工具变化只提示先登记演进，不写 identity_changed 终态。
        adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="evaluation-recover apply")
        adapter.orchestrator._verify_failed_run_reconciled(campaign_dir, manifest, ledger_dir, facts["failed"], head=head)
        transient: dict[str, Any] | None = None
        defect: dict[str, Any] | None = None
        if root_cause_class == "transient-environment":
            # 改造 5 M2（T5.17）：临时环境／Job 故障 → attempt-recovery 基线，零请求裁定，段本身才发请求。
            transient = adapter.orchestrator._transient_environment_admission(campaign_dir, manifest, facts, attempt_root=attempt_root, attempt=attempt)
            baseline_kind = "attempt-recovery"
            cause_component, cause_code, cause_step = "reconciler", "attempt.job-transient-failure", str(transient["failed_step"])
        else:
            defect = adapter.orchestrator._evaluation_defect_admission(arguments, campaign_dir, manifest, facts, attempt_root=attempt_root)
            baseline_kind = "evaluator-only"
            cause_component, cause_code, cause_step = "evaluator", "evaluation.rule-failed", str(facts["failed_step"])
        # ① 编号与 recovery.json／PREPARED（write-once；续作即校验一致）。
        number = resume_number if resume_number is not None else (max(states) + 1 if states else 1)
        if number <= current_baseline:
            raise adapter.orchestrator.ConfigurationError("新评估基线编号必须大于当前基线。")
        baseline_dir = adapter.orchestrator._evaluation_baseline_dir(campaign_dir, candidate_id, number)
        ensure_private_directory(baseline_dir.parent, campaign_dir)
        ensure_private_directory(baseline_dir, campaign_dir)
        diagnosis_path = baseline_dir / EVALUATION_BASELINE_DIAGNOSIS_FILENAME
        if existing_diagnosis is None:
            adapter.orchestrator._secure_write_json_once(diagnosis_path, diagnosis)
        # 执行集合（三稿 3.2.5）：前序 fail／pending ∪ 逐规则依赖变化。evaluator-only 下投影不变，
        # checker／builder 任一变化即全部规则的依赖摘要变化 → 全部重跑；只有 reader 变化时只重跑
        # 非 pass 行，其余引用复用（须 anchored）。builder 执行时仍按判据逐条复核，这里是冻结的预计。
        evaluation_run_document = facts["evaluation_run_document"]
        all_rules = sorted(rules)
        if transient is not None:
            # attempt-recovery：重采 J* 后其证据变化，引用 J* 的规则（含失败规则）进入 execute；其余 pass 行
            # 在 anchored 下复用。builder 执行时仍按逐规则依赖判据逐条复核。
            if facts["reuse_authority"] != "anchored" or evaluation_run_document is None:
                execute_rules = all_rules
                reuse_rules: list[str] = []
            else:
                execute_rules = sorted(
                    set(transient["affected_rules"])
                    | {str(row["rule"]) for row in evaluation_run_document["rules"] if row["status"] != "pass"}
                    | (set(all_rules) - {str(row["rule"]) for row in evaluation_run_document["rules"]})
                )
                reuse_rules = sorted(set(all_rules) - set(execute_rules))
        elif (
            facts["reuse_authority"] != "anchored"
            or evaluation_run_document is None
            or {"checker_sha256", "builder_sha256"} & set(defect["changed_items"])
        ):
            execute_rules = all_rules
            reuse_rules = []
        else:
            execute_rules = sorted(
                {str(row["rule"]) for row in evaluation_run_document["rules"] if row["status"] != "pass"}
                | (set(all_rules) - {str(row["rule"]) for row in evaluation_run_document["rules"]})
            )
            reuse_rules = sorted(set(all_rules) - set(execute_rules))
        try:
            root_cause_id = codex_upgrade_root_cause.structured_root_cause(
                component=cause_component,
                stable_error_code=cause_code,
                failed_step=cause_step,
                stable_dimensions={"phase": "VC-5"},
            )
        except codex_upgrade_root_cause.RootCauseError as error:
            raise adapter.orchestrator.ConfigurationError(f"根因编码失败：{error}") from error
        recovery_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_RECOVERY_FILENAME
        if recovery_path.is_file():
            recovery = codex_upgrade_vc_artifacts.validate_evaluation_recovery(adapter.orchestrator._read_json(recovery_path, "评估基线 recovery"))
        else:
            try:
                recovery = codex_upgrade_vc_artifacts.build_evaluation_recovery(
                    campaign_id=str(manifest["campaign_id"]),
                    candidate_id=candidate_id,
                    candidate_revision=revision,
                    evaluation_baseline=number,
                    kind=baseline_kind,
                    diagnosis={"path": diagnosis_path.relative_to(campaign_dir).as_posix(), "sha256": adapter.orchestrator.file_sha256(diagnosis_path)},
                    failure_source=facts["failure_source"],
                    reuse_authority=facts["reuse_authority"],
                    root_cause_class=root_cause_class,
                    root_cause_id=root_cause_id,
                    failed_step=cause_step,
                    previous_baseline=current_baseline,
                    previous_baseline_commit_sha256=(str(current_commit["commit_sha256"]) if current_commit is not None else None),
                    execute_rules=execute_rules,
                    reuse_rules=reuse_rules,
                    execute_jobs=list(transient["execute_jobs"]) if transient is not None else [],
                    reuse_jobs=(
                        list(transient["reuse_jobs"])
                        if transient is not None
                        else sorted(str(result.get("id")) for result in attempt.get("results", []) if isinstance(result, Mapping) and isinstance(result.get("id"), str))
                    ),
                    attempt_id=str(transient["attempt_id"]) if transient is not None else None,
                    recovery_revision=str(transient["recovery_revision"]) if transient is not None else None,
                    fix_commit=defect["fix_commit"] if defect is not None else None,
                    deployment_receipt=defect["deployment_receipt"] if defect is not None else None,
                    evaluation_epoch=defect["evaluation_epoch"] if defect is not None else None,
                    failed_evaluator_digests=facts["failed_evaluator_digests"],
                    current_evaluator_digests=facts["current_evaluator_digests"],
                    reviewer=reviewer,
                    approved_at_utc=adapter.orchestrator._utc_now(),
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise adapter.orchestrator.ConfigurationError(f"recovery.json 无法生成：{error}") from error
            adapter.orchestrator._secure_write_json_once(recovery_path, recovery)
        prepared_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_PREPARED_FILENAME
        if prepared_path.is_file():
            prepared = codex_upgrade_vc_artifacts.validate_evaluation_baseline_prepared(adapter.orchestrator._read_json(prepared_path, "评估基线 PREPARED"))
            if prepared["recovery_sha256"] != recovery["recovery_sha256"]:
                raise adapter.orchestrator.ConfigurationError("PREPARED 绑定的 recovery 摘要与 recovery.json 不一致。")
        else:
            prepared = codex_upgrade_vc_artifacts.build_evaluation_baseline_prepared(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                project_ledger_head_sequence=int(head["sequence"]),
                project_ledger_head_sha256=str(head["head_sha256"]),
                prepared_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(prepared_path, prepared)
        # ② outbox 根因事件（请求 0）→ 推总账 → 重放 → 二次判定。
        receipt_binding = reconciler._binding(campaign_root, recovery_path, "reconciliation")
        cause = codex_upgrade_root_cause.describe_root_cause(
            component=cause_component,
            stable_error_code=cause_code,
            failed_step=cause_step,
            stable_dimensions={"phase": "VC-5"},
        )
        operation_id = f"evaluation-recover:{candidate_id}:r{revision}:b{number}"
        try:
            batch = reconciler._commit_batch(
                campaign_root,
                operation_id=operation_id,
                event_type="reconciliation_committed",
                payload={
                    "campaign_id": str(manifest["campaign_id"]),
                    "subject_kind": "evaluation_baseline",
                    "subject_id": f"{candidate_id}:r{revision}:b{number}",
                    "phase": "VC-5",
                    "request": dict(reconciler.ZERO_REQUEST_PART),
                    "root_cause": {
                        "root_cause_id": cause["root_cause_id"],
                        "stable_error_code": cause["stable_error_code"],
                        "failed_step": cause["failed_step"],
                        "stable_dimensions": cause["stable_dimensions"],
                        "component": cause["component"],
                    },
                    "reconciliation_receipt_sha256": receipt_binding["sha256"],
                    "attempt_failed_event_sha256": None,
                },
                source={"kind": "evaluation_baseline", "sha256": receipt_binding["sha256"]},
                receipt_bindings=[receipt_binding],
            )
            observed = adapter.orchestrator._utc_now()
            pushed, head_after = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            identity = reconciler._identity_facts(campaign_root, manifest, reconciler._current_identity())
            ledger_facts = reconciler._ledger_facts(ledger_dir, now=observed)
            # 修好接着跑第 13 项：未隔离污染暂停待 environment-isolate，官方已封存受污染才终态。
            environment_decision = adapter.orchestrator._campaign_environment_decision(campaign_dir, _manifest=manifest)
            decision = reconciler._decide(
                head=head_after,
                plan=plan,
                ledger=ledger_facts,
                identity=identity,
                environment_status=reconciler._environment_decision_status(environment_decision),
                campaign_deadline_at_utc=adapter.orchestrator._campaign_plan_deadline(campaign_dir),
                root_cause_id=cause["root_cause_id"],
                request_status="resolved",
                now=observed,
                campaign_id=str(manifest["campaign_id"]),
            )
        except reconciler.ReconcilerError as error:
            raise adapter.orchestrator.ConfigurationError(f"评估基线根因入账失败：{error}") from error
        result: dict[str, Any] = {
            "status": "applied",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            "revision": revision,
            "evaluation_baseline": number,
            "kind": baseline_kind,
            "recovery_sha256": recovery["recovery_sha256"],
            "execute_rules": execute_rules,
            "reuse_rules": reuse_rules,
            "execute_jobs": list(recovery["execute_jobs"]),
            "reuse_jobs": list(recovery["reuse_jobs"]),
            "attempt_id": recovery["attempt_id"],
            "recovery_revision": recovery["recovery_revision"],
            "root_cause_id": cause["root_cause_id"],
            "batch": batch,
            "project_push": pushed,
            "decision": decision,
            "live_request_count": 0,
        }
        if decision["decision"] == reconciler.DECISION_PAUSED:
            # 预算到期只是暂停，不是停线（同 invalidate-candidate）：诊断、recovery、prepared 与根因 batch 都已幂等落盘，
            # 批准延期后重跑本命令即从二次判定续接；持有 Campaign 锁，不在锁内登记暂停事实。
            result["status"] = "paused"
            result["next_command"] = "预算已暂停：" + reconciler._paused_next_command(
                decision, "以同一批准摘要重新执行 evaluation-recover（幂等续接）"
            )
            return result
        if decision["decision"] != reconciler.DECISION_RECOVERABLE:
            try:
                stop = reconciler._permanent_stop(
                    campaign_root,
                    manifest,
                    ledger_dir,
                    subject_id=f"evaluation-baseline-{candidate_id}-r{revision}-b{number}",
                    root_cause_id=cause["root_cause_id"],
                    terminal_reason=str(decision["terminal_reason"]),
                    receipt_bindings=[receipt_binding],
                    ledger_receipts=[],
                    live_request_count=0,
                    reconciliation_receipt_sha256=receipt_binding["sha256"],
                    ledger_facts=ledger_facts,
                )
                pushed_terminal, head_terminal = reconciler._push_and_replay(project_root, campaign_root, now=observed)
            except reconciler.ReconcilerError as error:
                raise adapter.orchestrator.ConfigurationError(f"评估基线二次判定停线收口失败：{error}") from error
            result["status"] = "permanent_stop"
            result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
            result["next_command"] = stop["next_action"]
            return result
        # ③ AUTHORIZATION（绑定 outbox 事件摘要、head 与判定）。
        authorization_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_AUTHORIZATION_FILENAME
        batch_commit_path = Path(str(batch["batch_dir"])) / "COMMIT"
        if authorization_path.is_file():
            authorization = codex_upgrade_vc_artifacts.validate_evaluation_baseline_authorization(
                adapter.orchestrator._read_json(authorization_path, "评估基线 AUTHORIZATION")
            )
            if authorization["recovery_sha256"] != recovery["recovery_sha256"]:
                raise adapter.orchestrator.ConfigurationError("AUTHORIZATION 绑定的 recovery 摘要与 recovery.json 不一致。")
        else:
            authorization = codex_upgrade_vc_artifacts.build_evaluation_baseline_authorization(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                recovery_sha256=str(recovery["recovery_sha256"]),
                ledger_operation_id=operation_id,
                ledger_event_sha256=adapter.orchestrator.file_sha256(batch_commit_path),
                project_ledger_head_sequence=int(head_after["sequence"]),
                project_ledger_head_sha256=str(head_after["head_sha256"]),
                root_cause_id=cause["root_cause_id"],
                root_cause_count=int(decision.get("root_cause_count", 0)),
                authorized_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(authorization_path, authorization)
        # ④ COMMIT（stage_sources：capture 恒 reused；compare 按 compare-reader 是否变化；断言／accept local）。
        commit_path = baseline_dir / adapter.orchestrator.EVALUATION_BASELINE_COMMIT_FILENAME
        if commit_path.is_file():
            commit = codex_upgrade_vc_artifacts.validate_evaluation_baseline_commit(adapter.orchestrator._read_json(commit_path, "评估基线 COMMIT"))
        else:
            capture_source = adapter.orchestrator._stage_read_source(campaign_dir, candidate_id, current_baseline, "capture-candidate")
            compare_source = adapter.orchestrator._stage_read_source(campaign_dir, candidate_id, current_baseline, "compare")
            stage_sources: dict[str, Any] = {
                "capture-candidate": (
                    # attempt-recovery：本基线增量封存出新的候选阶段结果（local）。
                    {"source": "local", "target": f"candidates/{candidate_id}/revisions/b{number}/result.json"}
                    if transient is not None
                    else {
                        "source": "reused",
                        "baseline": int(capture_source["baseline_of_record"]),
                        "path": Path(capture_source["path"]).relative_to(campaign_dir).as_posix(),
                        "sha256": str(capture_source["sha256"]),
                    }
                ),
                "assertions": {"source": "local", "target": f"assertions/{candidate_id}/revisions/b{number}"},
                "accept": {"source": "local", "target": f"acceptance/{candidate_id}/revisions/b{number}/result.json"},
            }
            if (
                transient is not None
                or "compare_reader_sha256" in defect["changed_items"]
                or compare_source["status"] != "complete"
            ):
                stage_sources["compare"] = {"source": "local", "target": f"comparisons/{candidate_id}/revisions/b{number}/result.json"}
            else:
                stage_sources["compare"] = {
                    "source": "reused",
                    "baseline": int(compare_source["baseline_of_record"]),
                    "path": Path(compare_source["path"]).relative_to(campaign_dir).as_posix(),
                    "sha256": str(compare_source["sha256"]),
                }
            commit = codex_upgrade_vc_artifacts.build_evaluation_baseline_commit(
                campaign_id=str(manifest["campaign_id"]),
                candidate_id=candidate_id,
                candidate_revision=revision,
                evaluation_baseline=number,
                kind=baseline_kind,
                recovery_sha256=str(recovery["recovery_sha256"]),
                authorization_sha256=str(authorization["authorization_sha256"]),
                stage_sources=stage_sources,
                committed_at_utc=adapter.orchestrator._utc_now(),
            )
            adapter.orchestrator._secure_write_json_once(commit_path, commit)
        # ⑤ 账本 evaluation_baseline（幂等：最后一条已引用本 COMMIT 即 appended=false）。
        last = adapter.orchestrator._ledger_last_event_of_type(ledger_dir, "evaluation_baseline")
        if (
            last is not None
            and last[0].get("candidate_id") == candidate_id
            and last[0].get("evaluation_baseline") == number
            and last[0].get("baseline_commit_sha256") == commit["commit_sha256"]
        ):
            result["ledger_event"] = {"event_type": "evaluation_baseline", "appended": False}
        else:
            try:
                appended = codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=f"evaluation-baseline-{candidate_id}-r{revision}-b{number}",
                    phase="VC-5",
                    event_type="evaluation_baseline",
                    revision=revision,
                    candidate_id=candidate_id,
                    evaluation_baseline=number,
                    baseline_commit_sha256=str(commit["commit_sha256"]),
                    baseline_kind=baseline_kind,
                    recovery_revision=recovery["recovery_revision"],
                    next_action=(
                        f"capture-candidate run --attempt-recovery {recovery['recovery_revision']}（评估基线 b{number}）"
                        if transient is not None
                        else f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}）"
                    ),
                )
            except codex_upgrade_timing_ledger.TimingLedgerError as error:
                raise adapter.orchestrator.ConfigurationError(f"UpgradeTimingLedger 拒绝 evaluation_baseline：{error}") from error
            result["ledger_event"] = {
                "event_type": "evaluation_baseline",
                "appended": True,
                "head_sequence": appended["head_sequence"],
                "head_sha256": appended["head_sha256"],
            }
        result["commit_sha256"] = commit["commit_sha256"]
        result["stage_sources"] = commit["stage_sources"]
        if transient is not None:
            result["next_command"] = (
                f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}：capture-candidate run --attempt-recovery "
                f"{recovery['recovery_revision']} 只补跑 {'、'.join(recovery['execute_jobs'])} → account-sealed-candidate "
                f"--attempt-recovery → capture-candidate seal --attempt-recovery → 断言 builder --evaluation-baseline {number}"
                + (f" --reuse-from …/evaluation-run.json --reuse-authority anchored" if reuse_rules else "")
                + "）"
            )
            return result
        result["next_command"] = f"compile-and-run-vc-batch --phase VC-5（评估基线 b{number}：断言 builder --evaluation-baseline {number}"
        if reuse_rules:
            result["next_command"] += f" --reuse-from assertions/{candidate_id}[/revisions/b{current_baseline}]/evaluation-run.json --reuse-authority anchored"
        result["next_command"] += "）"
        return result


def _publish_ledger_receipt_copies(
    ledger_dir: Path,
    subject: str,
    sources: Mapping[str, Path],
) -> list[dict[str, str]]:
    """把收据逐字节发布到账本目录内（write-once），供账本事件 receipts 绑定。"""

    adapter = _adapter()
    target_dir = ledger_dir / "receipts" / "reconciliation" / subject
    if target_dir.is_symlink():
        raise adapter.orchestrator.ConfigurationError("账本收据目录不可信。")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (ledger_dir / "receipts", ledger_dir / "receipts" / "reconciliation", target_dir):
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            directory.chmod(0o700)
    bindings: list[dict[str, str]] = []
    for role, source in sorted(sources.items()):
        payload = adapter.orchestrator._read_json(source, f"{role} 收据")
        target = target_dir / f"{role}.json"
        try:
            codex_upgrade_timing_ledger._publish_once(target, payload, f"{role} 账本副本")
        except codex_upgrade_timing_ledger.TimingLedgerError as error:
            raise adapter.orchestrator.ConfigurationError(str(error)) from error
        bindings.append(
            {"role": role, "path": target.relative_to(ledger_dir).as_posix(), "sha256": adapter.orchestrator.file_sha256(target)}
        )
    return bindings


def _project_ledger_required(campaign_mode: Any, target_version: Any) -> bool:
    """0.154 起的 formal Campaign 必须在项目总账内创建与执行；其余按总账是否存在决定。"""

    adapter = _adapter()
    return campaign_mode == "formal" and adapter.orchestrator._requires_complete_vc_artifacts(str(target_version or ""))


def _campaign_plan_deadline(campaign_dir: Path) -> str | None:
    """读取批准后的 Campaign 有效截止；原始总计划及各批次中的冻结字段保持不变。"""

    path = campaign_dir / "control" / "vc" / "campaign-plan.json"
    if path.is_symlink() or not path.is_file():
        return None
    return codex_upgrade_vc_artifacts.effective_deadlines(campaign_dir)["total_deadline_at_utc"]


def _deadline_extend_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """预算控制不申请执行租约；批准仅解除对应一层预算，不替代恢复准入。"""
    adapter = _adapter()
    if arguments.deadline_action == "preview":
        if not arguments.scope or not arguments.new_deadline_at_utc or not arguments.reason:
            raise adapter.orchestrator.ConfigurationError("延期预览必须提供 scope、new-deadline-at-utc 和 reason")
        return codex_upgrade_project_ledger.preview_deadline_extension(arguments.campaign_dir,
            scope=arguments.scope, phase=arguments.phase, new_deadline_at_utc=arguments.new_deadline_at_utc,
            reason=arguments.reason)
    if not arguments.preview or not arguments.approve_sha256 or not arguments.approved_by:
        raise adapter.orchestrator.ConfigurationError("延期 apply 必须绑定预览、批准摘要与批准人")
    return codex_upgrade_project_ledger.apply_deadline_extension(arguments.campaign_dir,
        preview_path=arguments.preview, approve_sha256=arguments.approve_sha256, approved_by=arguments.approved_by)


def _request_budget_extend_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """请求预算延长（修好接着跑第 14 项）：不申请执行租约；批准只放宽请求预算，不替代恢复准入。"""

    adapter = _adapter()
    if arguments.budget_action == "preview":
        if arguments.new_budget is None or not arguments.reason:
            raise adapter.orchestrator.ConfigurationError("请求预算延长预览必须提供 --new-budget 与 --reason")
        return codex_upgrade_project_ledger.preview_live_request_budget_extension(
            arguments.campaign_dir, new_budget=int(arguments.new_budget), reason=str(arguments.reason)
        )
    if not arguments.preview or not arguments.approve_sha256 or not arguments.approved_by:
        raise adapter.orchestrator.ConfigurationError("请求预算延长 apply 必须绑定预览、批准摘要与批准人")
    return codex_upgrade_project_ledger.apply_live_request_budget_extension(
        arguments.campaign_dir, preview_path=arguments.preview, approve_sha256=arguments.approve_sha256,
        approved_by=arguments.approved_by,
    )


def _recovery_preview_retry_root_cause_ids(preview_path: Any) -> list[str] | None:
    """已批准恢复预览对应对账收据登记的根因集合（第三批 B3-9：续跑只挡本次要重试的根因）。

    预览与对账收据（``attempt-reconciliation.json``）同目录；预览缺省、路径不可信或收据读不到时返回 None，
    门禁退回整版本保守挡。收据本身由 ``load_approved_recovery_preview`` 按摘要核对，这里只取根因 ID。
    """

    if not isinstance(preview_path, Path):
        return None
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    if preview_path.is_symlink() or not preview_path.is_file():
        return None
    receipt_path = preview_path.resolve().parent / reconciler.ATTEMPT_RECEIPT_NAME
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return None
    try:
        # 修好接着跑第 38 项：取有效根因（同目录重归属收据优先），账务占位根因不能替真实根因绕过上限门禁。
        ids = reconciler.receipt_root_cause_ids(receipt_path)
    except reconciler.ReconcilerError:
        return None
    return sorted(set(ids)) if ids else None


def _assert_project_ledger_consumer(command: str, arguments: argparse.Namespace) -> None:
    """A0a-12 消费者门禁：resume 与 capture-official seal 开始前先补齐再锁内重放总账。"""

    adapter = _adapter()
    consumer = None
    if command == "resume":
        consumer = "resume"
    elif (
        command in {"capture-official", "capture-candidate"}
        and getattr(arguments, "capture_action", None) == "seal"
    ):
        # 官方与候选两侧的证据封存都是总账消费者；候选 seal 此前漏在门禁之外。
        consumer = "seal"
    elif command in {"compare", "accept"}:
        # B9：compare／accept 写比较与验收收据，同样先经项目总账准入门禁。
        consumer = command
    elif command == "canonical-advance":
        # canonical-advance 的 seal／compare／accept 与同名命令是同一语义终点，
        # 不得成为绕开总账门禁的旁路；VC-6 的生产步骤按 canonical-advance 本身准入。
        step = str(getattr(arguments, "canonical_step", "") or "")
        consumer = step if step in {"seal", "compare", "accept"} else "canonical-advance"
    if consumer is None:
        return
    campaign_dir = getattr(arguments, "campaign_dir", None)
    if not isinstance(campaign_dir, Path):
        return
    manifest_path = campaign_dir / "campaign.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return
    manifest = adapter.orchestrator._read_json(manifest_path, "Campaign 清单")
    # 第三批 B3-9（第 10 项③）：resume 按已批准恢复预览对应的根因集合只挡交集；其余消费者命令整版本保守挡。
    retry_root_cause_ids = (
        adapter.orchestrator._recovery_preview_retry_root_cause_ids(getattr(arguments, "recovery_preview", None)) if consumer == "resume" else None
    )
    codex_upgrade_project_ledger.assert_campaign_admitted(
        campaign_dir,
        command=consumer,
        require=adapter.orchestrator._project_ledger_required(manifest.get("campaign_mode"), manifest.get("target_version")),
        retry_root_cause_ids=retry_root_cause_ids,
    )


def _b0_evaluator_authorized_digest_history(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
) -> list[dict[str, str]]:
    """该候选 b0 的 checker／builder 授权口径历史：plan 冻结值，再按演进链顺序加上每次迁移到的 to 值。

    供逐字重派核对（修好接着跑第 9 项）：后继批次冻结的两项必须是历史中的某一值。历史只增不减，
    因此历史链重放时早先放行的后继依然成立；迁移条件与 ``_b0_evaluator_authorized_digests`` 相同。
    """

    adapter = _adapter()
    history = [adapter.orchestrator._plan_evaluator_entry_digests(manifest)]
    frozen = manifest.get("tool_identity")
    if isinstance(frozen, Mapping) and adapter.orchestrator._is_policy_v2_identity(frozen):
        for evolution in adapter.orchestrator._campaign_tool_evolutions(campaign_dir, manifest):
            evaluator = evolution.get("evaluator")
            if not isinstance(evaluator, Mapping) or not isinstance(evaluator.get("candidates"), Mapping):
                raise adapter.orchestrator.ConfigurationError(f"evolution-{int(evolution['index']):02d} 的评估器记录形态非法。")
            record = evaluator["candidates"].get(candidate_id)
            if record is None or (isinstance(record, Mapping) and record.get("b0_authorization_moved") is True):
                target = evaluator.get("to")
                if (
                    not isinstance(target, Mapping)
                    or set(target) != {"checker_sha256", "builder_sha256"}
                    or not all(isinstance(value, str) and adapter.orchestrator.SHA256_RE.fullmatch(value) for value in target.values())
                ):
                    raise adapter.orchestrator.ConfigurationError(f"evolution-{int(evolution['index']):02d} 的评估器 to 摘要非法。")
                entry = {field: str(target[field]) for field in ("checker_sha256", "builder_sha256")}
                if entry not in history:
                    history.append(entry)
    return history


def _evidence_root_registrants(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    exclude_attempt_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """同阶段（同候选）已有 attempt 的结果当前登记的外部证据根 → 登记者；已被取代映射走的不算。

    只读 attempt.json（其余完整性照常核对），不重放证据边界：被破坏的来源正是要处理的对象。
    """

    adapter = _adapter()
    owners: dict[str, list[dict[str, Any]]] = {}
    for attempt_root, _reservation in adapter.orchestrator._ordered_capture_attempts(
        campaign_dir, phase, candidate_id, _manifest=manifest
    ):
        if attempt_root.name == exclude_attempt_id or not (attempt_root / "attempt.json").is_file():
            continue
        _root, payload = adapter.orchestrator._load_capture_attempt(
            campaign_dir,
            phase,
            candidate_id,
            attempt_root.name,
            _verified_campaign_manifest=manifest,
            _replay_evidence_permissions=False,
        )
        relocated = adapter.orchestrator._evidence_root_relocations(
            campaign_dir, phase=phase, candidate_id=candidate_id, attempt_id=attempt_root.name
        )
        reference = {
            "phase": phase,
            "candidate_id": candidate_id,
            "attempt_id": attempt_root.name,
            "attempt_sha256": adapter.orchestrator.file_sha256(attempt_root / "attempt.json"),
        }
        roots = {
            str(root)
            for result in payload.get("results", [])
            if isinstance(result, Mapping)
            for root in result.get("evidence_roots", []) or []
            if isinstance(root, str)
        }
        for root in sorted(roots - set(relocated)):
            owners.setdefault(root, []).append(dict(reference))
    return owners


def _supersede_occupied_evidence_roots(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    attempt_root: Path,
    jobs: Sequence[Job],
) -> dict[str, Any] | None:
    """派发前清出执行集合作业的固定证据根：被其它 attempt 登记占据的目录改名为同级 ``.superseded-<本 attempt>``。

    先写取代收据（绑定原目录 inode），再逐个 rename 并核对 inode；未被任何 attempt 登记的孤儿目录不动，保持原有
    拒绝覆盖／增量语义。返回收据（没有需要取代的目录时返回 None）。
    """

    adapter = _adapter()
    registrants = adapter.orchestrator._evidence_root_registrants(
        campaign_dir, manifest, phase=phase, candidate_id=candidate_id, exclude_attempt_id=attempt_root.name
    )
    if not registrants:
        return None
    rows: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for job in jobs:
        for pattern in job.evidence_roots:
            for match in sorted(glob.glob(str(pattern))):
                logical = Path(match)
                owners = registrants.get(str(logical))
                if not owners:
                    continue
                if str(logical) in claimed:
                    raise adapter.orchestrator.ConfigurationError(f"同一证据根被执行集合中多个作业声明：{logical}")
                claimed.add(str(logical))
                host_root, _relative = adapter.orchestrator._failed_job_evidence_host_route(str(logical))
                identity = adapter.orchestrator._directory_identity(host_root)
                if identity is None or adapter.orchestrator._directory_identity(logical) != identity:
                    raise adapter.orchestrator.ConfigurationError(f"被占据的证据根宿主路径与逻辑路径不是同一目录：{logical}")
                infix = codex_upgrade_evidence_permissions.SUPERSEDED_ROOT_INFIX + attempt_root.name
                archived_name = f"{host_root.name}{infix}"
                suffix = 1
                while (host_root.with_name(archived_name)).exists() or (host_root.with_name(archived_name)).is_symlink():
                    suffix += 1
                    archived_name = f"{host_root.name}{infix}-{suffix}"
                rows.append(
                    {
                        "job_id": job.job_id,
                        "logical_root": str(logical),
                        "host_root": str(host_root),
                        "archived_logical_root": str(logical.with_name(archived_name)),
                        "archived_host_root": str(host_root.with_name(archived_name)),
                        "device": identity[0],
                        "inode": identity[1],
                        "registered_by": owners,
                    }
                )
    if not rows:
        return None
    receipt: dict[str, Any] = {
        "schema_version": adapter.orchestrator.EVIDENCE_ROOT_SUPERSESSION_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "phase": phase,
        "candidate_id": candidate_id,
        "superseding_attempt_id": attempt_root.name,
        "recorded_at_utc": adapter.orchestrator._utc_now(),
        "roots": rows,
        "live_request_count": 0,
    }
    receipt["receipt_sha256"] = adapter.orchestrator._fingerprint(receipt)
    directory = campaign_dir / adapter.orchestrator.EVIDENCE_ROOT_SUPERSESSION_DIR
    ensure_private_directory(directory, campaign_dir)
    adapter.orchestrator._secure_write_json_once(directory / f"supersession-{attempt_root.name}.json", receipt)
    for row in rows:
        Path(row["host_root"]).rename(row["archived_host_root"])
        if adapter.orchestrator._directory_identity(Path(row["archived_host_root"])) != (row["device"], row["inode"]):
            raise adapter.orchestrator.ConfigurationError(f"证据根取代后归档目录 inode 不一致：{row['logical_root']}")
    return receipt


def _evidence_root_conflicts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> list[dict[str, Any]]:
    """同阶段（同候选）非复用结果的证据根在取代映射后仍被两个以上 attempt 登记即为冲突（机器核验）。

    复用结果与来源共享证据根是合法承接，不计入；正常续跑在派发前已把来源占据的根取代归档，映射后不再相同。
    """

    adapter = _adapter()
    owners: dict[str, list[dict[str, Any]]] = {}
    for attempt_root, _reservation in adapter.orchestrator._ordered_capture_attempts(
        campaign_dir, phase, candidate_id, _manifest=manifest
    ):
        if not (attempt_root / "attempt.json").is_file():
            continue
        _root, payload = adapter.orchestrator._load_capture_attempt(
            campaign_dir,
            phase,
            candidate_id,
            attempt_root.name,
            _verified_campaign_manifest=manifest,
            _replay_evidence_permissions=False,
        )
        relocated = adapter.orchestrator._evidence_root_relocations(
            campaign_dir, phase=phase, candidate_id=candidate_id, attempt_id=attempt_root.name
        )
        per_root: dict[str, set[str]] = {}
        for result in payload.get("results", []) or []:
            if not isinstance(result, Mapping) or result.get("disposition") == "reused":
                continue
            for root in result.get("evidence_roots", []) or []:
                if isinstance(root, str):
                    per_root.setdefault(relocated.get(root, root), set()).add(str(result.get("id")))
        for root, job_ids in per_root.items():
            owners.setdefault(root, []).append({"attempt_id": attempt_root.name, "job_ids": sorted(job_ids)})
    return [
        {"evidence_root": root, "attempts": sorted(items, key=lambda item: item["attempt_id"])}
        for root, items in sorted(owners.items())
        if len(items) > 1
    ]


def _incremental_noop_job_ids(attempt_root: Path, payload: Mapping[str, Any]) -> list[str]:
    """附加事实：非复用结果的作业日志里出现"增量空执行"（mitm 矩阵按坐标 checkpoint 全部复用、零执行）。"""

    log_root = attempt_root / "logs"
    if log_root.is_symlink() or not log_root.is_dir():
        return []
    executed = {
        str(result.get("id"))
        for result in payload.get("results", []) or []
        if isinstance(result, Mapping) and result.get("disposition") != "reused"
    }
    found: set[str] = set()
    for path in sorted(log_root.glob("*.log")):
        if path.is_symlink() or not path.is_file():
            continue
        job_id = max((job for job in executed if path.name.startswith(job + "-")), key=len, default=None)
        if job_id is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"(?m)^incremental_noop=true executed=0\b", text):
            found.add(job_id)
    return sorted(found)


def _evidence_conflict_quarantine_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    reason: str,
) -> dict[str, Any]:
    """冻结本次要隔离的全部未隔离冲突 attempt 与逐项事实；预览零写入。

    卷入冲突的 attempt 一律隔离（不可挑选）：收口重放失败的判 compromised（边界不可复原，只读留档），其余判
    invalidated；两者都永不 seal、永不复用、续跑全部重跑。阶段已封存时拒绝（封存证据异常另有终态处理）。
    """

    adapter = _adapter()
    if not reason.strip():
        raise adapter.orchestrator.ConfigurationError("--reason 不得为空。")
    canonical = "capture-official" if phase == "official" else "capture-candidate"
    if adapter.orchestrator._stage_path(campaign_dir, canonical, candidate_id)[1].is_file():
        raise adapter.orchestrator.ConfigurationError(f"{canonical} 已封存，封存后的证据不能按冲突隔离续跑。")
    chain = adapter.orchestrator._evidence_conflict_quarantines(campaign_dir)
    quarantined = {
        str(item["attempt_id"])
        for receipt in chain
        for item in receipt["attempts"]
        if item.get("phase") == phase and item.get("candidate_id") == candidate_id
    }
    conflicts = adapter.orchestrator._evidence_root_conflicts(campaign_dir, manifest, phase=phase, candidate_id=candidate_id)
    involved = sorted(
        {entry["attempt_id"] for conflict in conflicts for entry in conflict["attempts"]} - quarantined
    )
    if not involved:
        raise adapter.orchestrator.ConfigurationError("没有未隔离的证据根冲突，无需 evidence-conflict-quarantine。")
    effective = [
        conflict
        for conflict in conflicts
        if any(entry["attempt_id"] in involved for entry in conflict["attempts"])
    ]
    attempts: list[dict[str, Any]] = []
    for attempt_id in involved:
        attempt_root, payload = adapter.orchestrator._load_capture_attempt(
            campaign_dir,
            phase,
            candidate_id,
            attempt_id,
            _verified_campaign_manifest=manifest,
            _replay_evidence_permissions=False,
        )
        try:
            adapter.orchestrator._replay_attempt_evidence_permissions(
                attempt_root,
                payload,
                root_relocations=adapter.orchestrator._evidence_root_relocations(
                    campaign_dir, phase=phase, candidate_id=candidate_id, attempt_id=attempt_id
                ),
            )
            boundary = {"status": "passed", "error": None}
        except adapter.orchestrator.ConfigurationError as error:
            boundary = {"status": "failed", "error": str(error)}
        attempts.append(
            {
                "phase": phase,
                "candidate_id": candidate_id,
                "attempt_id": attempt_id,
                "attempt_sha256": adapter.orchestrator.file_sha256(attempt_root / "attempt.json"),
                "status": str(payload.get("status")),
                "disposition": "compromised" if boundary["status"] == "failed" else "invalidated",
                "boundary_replay": boundary,
                "conflict_roots": sorted(
                    conflict["evidence_root"]
                    for conflict in effective
                    if any(entry["attempt_id"] == attempt_id for entry in conflict["attempts"])
                ),
                "incremental_noop_job_ids": adapter.orchestrator._incremental_noop_job_ids(attempt_root, payload),
            }
        )
    preview: dict[str, Any] = {
        "schema_version": EVIDENCE_CONFLICT_QUARANTINE_PREVIEW_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "index": len(chain) + 1,
        "previous_quarantine_sha256": str(chain[-1]["quarantine_sha256"]) if chain else None,
        "phase": phase,
        "candidate_id": candidate_id,
        "conflicts": effective,
        "attempts": attempts,
        "reason": reason,
        "live_request_count": 0,
    }
    preview["review_sha256"] = adapter.orchestrator._fingerprint(preview)
    return preview


def _evidence_conflict_quarantine_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """evidence-conflict-quarantine：不带 --approve-sha256 只预览；批准后写一次冲突隔离收据。

    要求已登记当前工具的演进、Campaign 静默；锁内重算预览，批准摘要不一致即作废。
    """

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    candidate_id = getattr(arguments, "candidate_id", None) or None
    phase = "candidate" if candidate_id else "official"
    if candidate_id is not None and not adapter.orchestrator.SAFE_ID_RE.fullmatch(str(candidate_id)):
        raise adapter.orchestrator.ConfigurationError("--candidate-id 非法。")
    approval = getattr(arguments, "approve_sha256", None)
    next_steps = (
        "隔离已生效：被隔离的等待封存 attempt 以 reconcile-attempt 按作废对账（失败的 seal 链父批次先 "
        "reconcile-supervisor-run），批准恢复预览后 resume --rerun-failed 全部重跑；被隔离的 attempt 永不复用、永不 seal"
    )
    if approval is not None:
        # 同一批准重跑：隔离收据已写入则幂等返回（写一次的收据是唯一落盘点）。
        for receipt in adapter.orchestrator._evidence_conflict_quarantines(campaign_dir):
            if receipt.get("review_sha256") == str(approval):
                return {
                    "status": "quarantined",
                    "reused": True,
                    "quarantine_index": receipt["index"],
                    "quarantine_sha256": receipt["quarantine_sha256"],
                    "attempts": receipt["attempts"],
                    "next_command": next_steps,
                }
    adapter.orchestrator._require_tool_evolution_registered(campaign_dir, manifest, action="evidence-conflict-quarantine")
    quiet = adapter.orchestrator._tool_evolution_quiescence_problems(campaign_dir)
    if quiet:
        raise adapter.orchestrator.ConfigurationError("证据根冲突隔离要求 Campaign 静默：" + "；".join(quiet))
    options = {"phase": phase, "candidate_id": candidate_id, "reason": str(arguments.reason)}
    preview = adapter.orchestrator._evidence_conflict_quarantine_preview(campaign_dir, manifest, **options)
    if approval is None:
        return {
            "status": "approval_required",
            **preview,
            "next_command": "evidence-conflict-quarantine（同样参数）--approve-sha256 <review_sha256> --approved-by <批准人>",
        }
    approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
    if not approved_by:
        raise adapter.orchestrator.ConfigurationError("批准 evidence-conflict-quarantine 必须提供 --approved-by。")
    with adapter.orchestrator._campaign_lock(campaign_dir):
        preview = adapter.orchestrator._evidence_conflict_quarantine_preview(campaign_dir, manifest, **options)
        if str(approval) != preview["review_sha256"]:
            raise adapter.orchestrator.ConfigurationError("批准摘要与重算的冲突隔离预览不一致（冲突事实或隔离链已变化）；重新预览后再批准。")
        directory = campaign_dir / adapter.orchestrator.EVIDENCE_CONFLICT_QUARANTINE_DIR
        ensure_private_directory(directory, campaign_dir)
        receipt: dict[str, Any] = {
            "schema_version": adapter.orchestrator.EVIDENCE_CONFLICT_QUARANTINE_SCHEMA,
            "campaign_id": preview["campaign_id"],
            "index": preview["index"],
            "previous_quarantine_sha256": preview["previous_quarantine_sha256"],
            "phase": phase,
            "candidate_id": candidate_id,
            "conflicts": preview["conflicts"],
            "attempts": preview["attempts"],
            "reason": preview["reason"],
            "review_sha256": preview["review_sha256"],
            "approved_by": approved_by,
            "approved_at_utc": adapter.orchestrator._utc_now(),
        }
        receipt["quarantine_sha256"] = adapter.orchestrator._fingerprint(receipt)
        adapter.orchestrator._secure_write_json_once(directory / f"quarantine-{int(preview['index']):02d}.json", receipt)
    return {
        "status": "quarantined",
        "reused": False,
        "quarantine_index": receipt["index"],
        "quarantine_sha256": receipt["quarantine_sha256"],
        "attempts": receipt["attempts"],
        "next_command": next_steps,
    }


def recovery_preview_continuity(
    campaign_dir: Path,
    *,
    manifest: Mapping[str, Any],
    phase: str,
    candidate_id: str | None,
    source_attempt_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    """零请求预览阶段的环境连续性检查（第三批 B3-12，第 22 项）。

    与 resume 真实执行前的 ``_verify_environment_continuity`` 同一比较口径：新采一次只读探针（写到对账目录下的
    ``output_dir``，不写 attempt 证据），与被承接 attempt 的 after 探针比 ``CONTINUITY_PROBE_KINDS``。返回
    ``status``：consistent（可复用）、drifted（漂移，附 drifted_kinds）、probe_failed（探针采不到，失败关闭按漂移
    处理）、unverifiable（源 attempt 没有 after 探针，无从比较；真实执行按既有规则拒绝承接）。
    """

    adapter = _adapter()
    relative = adapter.orchestrator._capture_attempt_relative(phase, candidate_id)
    after_path = (
        campaign_dir.resolve(strict=True)
        / relative
        / "attempts"
        / source_attempt_id
        / "evidence"
        / "environment"
        / "after"
        / "probe-manifest.json"
    )
    if after_path.is_symlink() or not after_path.is_file():
        return {
            "status": "unverifiable",
            "reason": "被承接 attempt 缺少 after 环境探针，无法比较连续性",
            "drifted_kinds": [],
            "compared_kinds": list(adapter.orchestrator.CONTINUITY_PROBE_KINDS),
        }
    after_manifest = adapter.orchestrator._read_json(after_path, "被承接 attempt 的 after 探针")
    previous = adapter.orchestrator._probe_snapshot_digests(after_manifest)
    probe_manifest = dict(manifest)
    if phase == "candidate":
        probe_manifest = adapter.orchestrator._apply_candidate_runtime_override(campaign_dir, probe_manifest, candidate_id)
    try:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        current_manifest = adapter.orchestrator._probe_capture_environment(probe_manifest, output_dir, "before")
    except Exception as error:  # noqa: BLE001 —— 探针采不到一律失败关闭：按漂移处理并记录原因
        return {
            "status": "probe_failed",
            "reason": f"{type(error).__name__}: {error}",
            "drifted_kinds": list(adapter.orchestrator.CONTINUITY_PROBE_KINDS),
            "compared_kinds": list(adapter.orchestrator.CONTINUITY_PROBE_KINDS),
            "source_after_probe_sha256": adapter.orchestrator.file_sha256(after_path),
        }
    current = adapter.orchestrator._probe_snapshot_digests(current_manifest)
    drifted = [kind for kind in adapter.orchestrator.CONTINUITY_PROBE_KINDS if previous.get(kind) != current.get(kind)]
    probe_path = output_dir / "probe-manifest.json"
    return {
        "status": "drifted" if drifted else "consistent",
        "drifted_kinds": drifted,
        "compared_kinds": list(adapter.orchestrator.CONTINUITY_PROBE_KINDS),
        "source_after_probe_sha256": adapter.orchestrator.file_sha256(after_path),
        "probe_manifest_sha256": adapter.orchestrator.file_sha256(probe_path) if probe_path.is_file() else None,
        "probe_dir": str(output_dir),
    }


def _recovery_root_suffix(recovery_revision: str) -> str:
    return f"-recovery-{recovery_revision}"


def _job_recovery_root_keys(job: Job) -> list[str]:
    """Job 证据根模式的稳定目录名（glob 之前的最后一段），作为段重定位的替换键。"""

    adapter = _adapter()
    keys: list[str] = []
    for pattern in job.evidence_roots:
        stem = str(pattern).split("*", 1)[0].rstrip("-")
        name = Path(stem).name
        if not name or "/" in name:
            raise adapter.orchestrator.ConfigurationError(f"Job {job.job_id} 的证据根模式无法推导稳定目录名：{pattern}")
        if name not in keys:
            keys.append(name)
    if not keys:
        raise adapter.orchestrator.ConfigurationError(f"Job {job.job_id} 没有证据根模式，无法进入恢复段。")
    return keys


def _relocate_job_for_recovery(job: Job, recovery_revision: str) -> Job:
    """把 Job 的证据根与命令里出现的根名改写为 ``<根名>-recovery-ar<k>``（同级新目录）。"""

    adapter = _adapter()
    keys = adapter.orchestrator._job_recovery_root_keys(job)
    suffix = adapter.orchestrator._recovery_root_suffix(recovery_revision)
    for key in keys:
        if key.endswith(suffix):
            raise adapter.orchestrator.ConfigurationError(f"Job {job.job_id} 的证据根已带恢复段后缀，禁止嵌套：{key}")

    def relocate_component(part: str) -> str:
        # 只改写整段等于根名、或以根名开头且紧跟 glob 尾巴（如 <名>-*-run）的路径分量／取值；
        # 子串命中（runs 含 run）一律不动。
        for key in keys:
            if part == key:
                return f"{key}{suffix}"
            remainder = part[len(key):]
            if part.startswith(key) and remainder.startswith("-*"):
                return f"{key}{suffix}{remainder}"
        return part

    def relocate(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return "/".join(relocate_component(part) for part in value.split("/"))

    steps: list[dict[str, Any]] = []
    for raw_step in job.steps:
        step = dict(raw_step)
        argv = raw_step.get("argv")
        if isinstance(argv, (list, tuple)):
            step["argv"] = [relocate(item) for item in argv]
        environment = raw_step.get("environment")
        if isinstance(environment, Mapping):
            step["environment"] = {str(k): relocate(v) for k, v in environment.items()}
        steps.append(step)
    relocated = replace(
        job,
        steps=tuple(steps),
        evidence_roots=tuple(relocate(pattern) for pattern in job.evidence_roots),
    )
    if relocated.evidence_roots == job.evidence_roots:
        raise adapter.orchestrator.ConfigurationError(f"Job {job.job_id} 的证据根未被重定位到恢复段。")
    return relocated


def _reserve_attempt_recovery(
    campaign_dir: Path,
    *,
    manifest: Mapping[str, Any],
    candidate_id: str,
    attempt_root: Path,
    original_attempt: Mapping[str, Any],
    original_reservation: Mapping[str, Any],
    recovery: Mapping[str, Any],
    recovery_revision: str,
    baseline: int,
    baseline_commit_sha256: str,
    jobs: Sequence[Job],
    source_jobs: Mapping[str, Job],
    identity: Mapping[str, Any],
    lease: CampaignLease | None,
    deadline: incremental_recovery.WallClockDeadline,
    reuse_preview: Mapping[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """在 Campaign 锁内原子发布恢复段预约：段目录不存在、同 attempt 无其它 active 恢复段。
    ``recovery_revision`` 是本段实际编号（基线冻结的首段或其失败段的后继段）。"""

    adapter = _adapter()
    deadline.check("attempt-recovery:reserve:start")
    with adapter.orchestrator._campaign_lock(campaign_dir, deadline=deadline):
        adapter.orchestrator._reject_contaminated_campaign(campaign_dir)
        recovery_root = attempt_root / adapter.orchestrator.ATTEMPT_RECOVERY_DIRNAME
        segment_root = adapter.orchestrator._attempt_recovery_segment_root(attempt_root, recovery_revision)
        if segment_root.exists() or segment_root.is_symlink():
            raise adapter.orchestrator.ConfigurationError(
                f"恢复段 {recovery_revision} 已存在，禁止重开；成功段由逐字重派幂等返回，失败段先 "
                f"reconcile-attempt --recovery-revision {recovery_revision} 对账，再以后继段 ar<k+1> "
                "--rerun-failed --recovery-preview 承接。"
            )
        if recovery_root.exists():
            if recovery_root.is_symlink() or not recovery_root.is_dir():
                raise adapter.orchestrator.ConfigurationError("attempt 恢复段目录不可信。")
            for existing in sorted(recovery_root.iterdir()):
                if existing.is_symlink() or not existing.is_dir():
                    raise adapter.orchestrator.ConfigurationError("attempt 恢复段目录含不可信条目。")
                # 既有段只能是已对账的失败终态（中断段先 reconcile-attempt --recovery-revision）；成功段
                # 意味着本基线已有可封存结果，禁止再开段。
                if not adapter.orchestrator._attempt_recovery_segment_terminal_failed(
                    campaign_dir, candidate_id=candidate_id, attempt_id=attempt_root.name, segment_root=existing
                ):
                    raise adapter.orchestrator.ConfigurationError(
                        f"attempt 存在未收口或未对账的恢复段 {existing.name}，禁止并行开段"
                        f"（中断／失败段先 reconcile-attempt --recovery-revision {existing.name}）。"
                    )
        # 当前基线 b<K> 的候选阶段目标要等段 seal 才写出：已封存 attempt 按前序基线的读来源解析。
        active = adapter.orchestrator._active_unsealed_attempts(
            campaign_dir, "candidate", _manifest=manifest, _baseline=int(recovery["previous_baseline"])
        )
        if active:
            raise adapter.orchestrator.ConfigurationError(f"Campaign 存在未封存预约或 attempt，禁止开恢复段：{active}")
        expected_execution = {
            str(item.get("id")): str(item.get("execution_sha256"))
            for item in original_reservation.get("planned_jobs", [])
            if isinstance(item, Mapping)
        }
        reuse_proofs = dict(reuse_preview.get("reuse_proofs", {})) if reuse_preview else {}
        if reuse_preview is not None:
            from tools.official_client_capture import codex_upgrade_reconciler as reconciler

            adapter.orchestrator._require_recovery_preview_scope_equals_frozen_jobs(reuse_preview, recovery["execute_jobs"], label="恢复段预约")
            # R11：复验读取的证据字节记入预约，复用前的成本可审计。
            reuse_validation_scanned_bytes = reconciler.validate_segment_reuse_preview(campaign_dir, reuse_preview)
        if set(job.job_id for job in jobs) != set(recovery["execute_jobs"]):
            raise adapter.orchestrator.ConfigurationError("恢复段预约必须覆盖基线 J*。")
        planned_jobs: list[dict[str, Any]] = []
        for job in jobs:
            source = source_jobs.get(job.job_id)
            if source is None or expected_execution.get(job.job_id) != adapter.orchestrator._job_execution_sha256(source):
                raise adapter.orchestrator.ConfigurationError(f"恢复段 Job {job.job_id} 的原执行摘要与原预约不一致。")
            planned_jobs.append(
                {
                    "id": job.job_id,
                    "required": job.required,
                    "execution_sha256": (
                        adapter.orchestrator._read_json(campaign_dir / reuse_proofs[job.job_id]["result"]["path"], "复用 Job")["execution_sha256"]
                        if job.job_id in reuse_proofs else adapter.orchestrator._job_execution_sha256(job)
                    ),
                    "source_execution_sha256": expected_execution[job.job_id],
                }
            )
        run_nonce = secrets.token_hex(32)
        reservation: dict[str, Any] = {
            "schema_version": adapter.orchestrator.ATTEMPT_RECOVERY_RESERVATION_SCHEMA,
            "campaign_id": manifest["campaign_id"],
            "campaign_mode": manifest["campaign_mode"],
            "campaign_purpose": manifest["campaign_purpose"],
            "campaign_manifest_sha256": adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
            "phase": "candidate",
            "candidate_id": candidate_id,
            "candidate_purpose": manifest["campaign_purpose"],
            "attempt_id": attempt_root.name,
            "recovery_revision": recovery_revision,
            "evaluation_baseline": baseline,
            "baseline_commit_sha256": baseline_commit_sha256,
            "recovery_sha256": str(recovery["recovery_sha256"]),
            "run_nonce": run_nonce,
            "started_at_utc": adapter.orchestrator._utc_now(),
            "identity_sha256": adapter.orchestrator._fingerprint(dict(identity)),
            "original_reservation": {
                "path": str((attempt_root / "reservation.json").relative_to(campaign_dir)),
                "sha256": adapter.orchestrator.file_sha256(attempt_root / "reservation.json"),
                "run_nonce": str(original_reservation["run_nonce"]),
            },
            "original_attempt": {
                "path": str((attempt_root / "attempt.json").relative_to(campaign_dir)),
                "sha256": adapter.orchestrator.file_sha256(attempt_root / "attempt.json"),
                "attempt_digest": str(original_attempt["attempt_digest"]),
            },
            "planned_jobs": planned_jobs,
            "reuse_jobs": list(recovery["reuse_jobs"]),
        }
        if reuse_preview is not None:
            reservation.update(
                execute_job_ids=list(reuse_preview["execute_job_ids"]),
                reuse_job_ids=list(reuse_preview["reuse_job_ids"]), reuse_proofs=reuse_proofs,
                reuse_proofs_sha256=adapter.orchestrator._fingerprint(reuse_proofs),
                recovery_review_sha256=reuse_preview["review_sha256"],
                reuse_validation_scanned_bytes=reuse_validation_scanned_bytes,
            )
        if lease is not None:
            if not lease.acquired:
                raise adapter.orchestrator.ConfigurationError("恢复段预约缺少 active Campaign lease。")
            owner_nonce = lease.payload.get("owner_nonce")
            if lease.payload.get("campaign_id") != manifest.get("campaign_id") or not isinstance(owner_nonce, str):
                raise adapter.orchestrator.ConfigurationError("恢复段预约与 Campaign lease 身份不一致。")
            reservation["campaign_lease"] = {
                "path": adapter.orchestrator.CAMPAIGN_LEASE_FILENAME,
                "owner_nonce": owner_nonce,
                "deadline_at_utc": str(lease.payload.get("deadline_at_utc")),
            }
        reservation["reservation_digest"] = adapter.orchestrator._fingerprint(reservation)
        ensure_private_directory(recovery_root, campaign_dir)
        temporary_root = Path(tempfile.mkdtemp(prefix=f".{recovery_revision}-", dir=recovery_root))
        temporary_root.chmod(0o700)
        try:
            adapter.orchestrator._secure_write_json_once(temporary_root / adapter.orchestrator.ATTEMPT_RECOVERY_RESERVATION_FILENAME, reservation)
            os.rename(temporary_root, segment_root)
            descriptor = os.open(recovery_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            if temporary_root.exists() and not temporary_root.is_symlink():
                (temporary_root / adapter.orchestrator.ATTEMPT_RECOVERY_RESERVATION_FILENAME).unlink(missing_ok=True)
                try:
                    temporary_root.rmdir()
                except OSError:
                    pass
            raise
    deadline.check("attempt-recovery:reserve:complete")
    return segment_root, reservation


def _backfill_original_attempt_ledger_events(
    ledger_dir: Path,
    *,
    attempt_id: str,
    live_request_count: int,
) -> list[dict[str, Any]]:
    """账本追认原 attempt：正常 run 不写 attempt 事件，而 attempt_recovery_started 要求原 attempt 已 completed。"""

    adapter = _adapter()
    state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
    recorded: str | None = None
    for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir):
        if event.get("attempt_id") != attempt_id:
            continue
        event_type = event.get("event_type")
        if event_type == "attempt_started":
            recorded = "active"
        elif event_type == "attempt_completed":
            recorded = "completed"
        elif event_type == "attempt_failed":
            recorded = "failed"
    written: list[dict[str, Any]] = []
    if recorded is not None:
        if recorded != "completed":
            raise adapter.orchestrator.ConfigurationError(f"账本中原 attempt {attempt_id} 的状态是 {recorded}，不能开恢复段。")
        return written
    phase = str(state.get("active_phase"))
    for event_type, extra in (
        ("attempt_started", {}),
        ("attempt_completed", {"live_request_count": int(live_request_count)}),
    ):
        summary = codex_upgrade_timing_ledger.append_event(
            ledger_dir,
            event_id=f"attempt-{attempt_id}-{event_type.split('_', 1)[1]}-backfill",
            phase=phase,
            event_type=event_type,
            attempt_id=attempt_id,
            next_action="恢复段开段前追认已封存的原 attempt",
            **extra,
        )
        written.append({"event_type": event_type, "head_sequence": summary["head_sequence"], "head_sha256": summary["head_sha256"]})
    return written


def _append_attempt_recovery_ledger_event(
    ledger_dir: Path,
    *,
    event_type: str,
    attempt_id: str,
    recovery_revision: str,
    candidate_id: str,
    revision: int | None,
    root_cause_id: str | None = None,
    live_request_count: int | None = None,
    next_action: str,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if root_cause_id is not None:
        extra["root_cause_id"] = root_cause_id
    if live_request_count is not None:
        extra["live_request_count"] = int(live_request_count)
    if revision is not None:
        extra["revision"] = int(revision)
    summary = codex_upgrade_timing_ledger.append_event(
        ledger_dir,
        event_id=f"attempt-recovery-{attempt_id}-{recovery_revision}-{event_type.rsplit('_', 1)[1]}",
        phase="VC-5",
        event_type=event_type,
        attempt_id=attempt_id,
        recovery_revision=recovery_revision,
        candidate_id=candidate_id,
        next_action=next_action,
        **extra,
    )
    return {"event_type": event_type, "head_sequence": summary["head_sequence"], "head_sha256": summary["head_sha256"]}


def _write_attempt_recovery_summary(
    campaign_dir: Path,
    segment_root: Path,
    reservation: Mapping[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """只写一次恢复段 run-summary（形同 attempt.json，绑定段预约）。"""

    adapter = _adapter()
    planned = {item["id"]: item["execution_sha256"] for item in reservation["planned_jobs"]}
    for result in payload.get("results", []):
        if (
            not isinstance(result, dict)
            or result.get("id") not in planned
            or result.get("execution_sha256") != planned[result["id"]]
        ):
            raise adapter.orchestrator.ConfigurationError("恢复段任务不在段预约内或执行摘要漂移。")
    if "execute_job_ids" in reservation:
        expected_execute = set(reservation["execute_job_ids"])
        expected_reuse = set(reservation["reuse_job_ids"])
        results = payload.get("results", [])
        actual_reuse = {row["id"] for row in results if row.get("recovered_from") is not None}
        actual_execute = {row["id"] for row in results if row.get("recovered_from") is None}
        if (expected_execute & expected_reuse or expected_execute | expected_reuse != set(planned)
                or actual_reuse != expected_reuse or not actual_execute <= expected_execute
                or payload.get("execute_jobs") != reservation["execute_job_ids"]
                or (payload.get("status") == adapter.orchestrator.ATTEMPT_RECOVERY_SUCCESS_STATUS and actual_execute != expected_execute)):
            raise adapter.orchestrator.ConfigurationError("恢复段实际启动／复用集合与预约批准不一致。")
    document = dict(payload)
    document["schema_version"] = adapter.orchestrator.ATTEMPT_RECOVERY_SUMMARY_SCHEMA
    document["campaign_mode"] = reservation["campaign_mode"]
    document["campaign_purpose"] = reservation["campaign_purpose"]
    document["candidate_purpose"] = reservation["candidate_purpose"]
    document["campaign_manifest_sha256"] = adapter.orchestrator.file_sha256(campaign_dir / "campaign.json")
    document["attempt_id"] = reservation["attempt_id"]
    document["recovery_revision"] = reservation["recovery_revision"]
    document["evaluation_baseline"] = reservation["evaluation_baseline"]
    document["baseline_commit_sha256"] = reservation["baseline_commit_sha256"]
    document["run_nonce"] = reservation["run_nonce"]
    document["started_at_utc"] = reservation["started_at_utc"]
    document["completed_at_utc"] = adapter.orchestrator._utc_now()
    document["reservation"] = {
        "path": str((segment_root / adapter.orchestrator.ATTEMPT_RECOVERY_RESERVATION_FILENAME).relative_to(campaign_dir)),
        "sha256": adapter.orchestrator.file_sha256(segment_root / adapter.orchestrator.ATTEMPT_RECOVERY_RESERVATION_FILENAME),
    }
    document["original_attempt"] = dict(reservation["original_attempt"])
    failure_observations, root_causes = adapter.orchestrator._attempt_failure_facts(document)
    document["failure_observations"] = failure_observations
    document["root_causes"] = root_causes
    adapter.orchestrator._replay_attempt_recovery_evidence_permissions(segment_root, document)
    document["attempt_recovery_digest"] = adapter.orchestrator._fingerprint(document)
    adapter.orchestrator._secure_write_json_once(segment_root / adapter.orchestrator.ATTEMPT_RECOVERY_SUMMARY_FILENAME, document)
    return document


def _reject_canonical_legacy_write(
    arguments: argparse.Namespace,
    command: str,
) -> None:
    """canonical checkpoint 建立后，旧恢复对象只能读取，禁止继续追加。"""

    adapter = _adapter()
    if command not in {
        "successor",
        adapter.orchestrator.FORMAL_FAILED_JOB_RECOVERY_COMMAND,
        adapter.orchestrator.FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND,
        "reuse-official-evidence",
        "control-epoch",
        "evaluation-transition",
        "terminal-transition-preflight",
    }:
        return
    roots: list[Path] = []
    campaign_dir = getattr(arguments, "campaign_dir", None)
    predecessor_dir = getattr(arguments, "predecessor_campaign_dir", None)
    if isinstance(campaign_dir, Path):
        roots.append(campaign_dir)
    if isinstance(predecessor_dir, Path):
        roots.append(predecessor_dir)
    if any((root / adapter.orchestrator.CANONICAL_DIRECTORY / adapter.orchestrator.CANONICAL_IMPORT_RECEIPT_FILENAME).is_file() for root in roots):
        raise adapter.orchestrator.ConfigurationError(
            "当前 Campaign 已切换到 canonical checkpoint；"
            "successor/control-epoch/runtime-repair/evaluation-transition "
            "只允许历史只读解析，禁止继续写入。"
        )


def _tool_evolution_unregistered_drift(
    effective_identity: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """当前受管树相对有效身份的差异项；非空即必须先登记工具演进才能继续派发。

    不变式：Campaign 继续执行时，当前受管树必须恰好是有效身份（最新演进）的树。这样每个
    attempt 的全部结果都能用其生产序号对应的完整身份逐文件验真，续跑与 seal 的判定才是精确的。
    control 层单独变化同样要登记（影响为空，只是一次预览＋批准）。
    """

    drift: list[str] = []
    for field, label in (
        ("policy_sha256", "工具身份策略"),
        ("wire_producer_sha256", "wire producer"),
        ("evidence_semantics_sha256", "evidence semantics"),
        ("control_sha256", "control"),
    ):
        if str(current.get(field)) != str(effective_identity.get(field)):
            drift.append(label)
    if not drift and str(current.get("files_sha256")) != str(effective_identity.get("files_sha256")):
        drift.append("受管树（被忽略前缀的其它客户端文件）")
    return drift


def _refuse_seal_chain_on_evolution_invalidated_attempt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    action_plan: Any,
) -> None:
    """编译 VC-1／VC-5 seal 链批次前：目标 attempt 有作业被工具演进作废就零写入拒绝（修好接着跑第 21 项）。

    seal 本身也会拒绝，但派发后才失败会让父批次落入恢复或审核、多绕一轮；这里在任何落盘之前拦下，
    指向真实可走的续跑路径。动作计划本身的合法性仍由编译器校验，读不到就交给编译器报错。
    """

    adapter = _adapter()
    if phase not in {"VC-1", "VC-5"} or not isinstance(action_plan, Path) or not action_plan.is_file():
        return
    try:
        plan = adapter.orchestrator._read_json(action_plan, "VC action plan")
    except adapter.orchestrator.ConfigurationError:
        return
    actions = plan.get("actions") if isinstance(plan, Mapping) else None
    for action in actions if isinstance(actions, list) else []:
        target = codex_upgrade_supervisor._seal_chain_attempt_target(action)
        if target is None:
            continue
        _campaign, side, candidate_id, attempt_id = target
        if side == "candidate" and not candidate_id:
            continue
        attempt_root = campaign_dir / adapter.orchestrator._capture_attempt_relative(side, candidate_id) / "attempts" / attempt_id
        if not (attempt_root / "attempt.json").is_file():
            continue
        _root, attempt = adapter.orchestrator._load_capture_attempt(
            campaign_dir, side, candidate_id, attempt_id, _verified_campaign_manifest=manifest
        )
        invalidated = adapter.orchestrator._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, attempt_root, attempt, phase=side, candidate_id=candidate_id
        )
        if invalidated:
            raise adapter.orchestrator.ToolEvolutionRequired(
                f"compile-and-run-vc-batch 拒绝：{action.get('action_id')} 要封存的 attempt {attempt_id} "
                f"有作业被工具演进作废（{'、'.join(invalidated)}）。先 reconcile-attempt 入账并批准恢复预览，"
                "resume --rerun-failed 只重跑失效作业，再对新 attempt 走 seal；本次未写入任何文件。"
            )
        if (side, candidate_id, attempt_id) in adapter.orchestrator._isolation_invalidated_attempts(campaign_dir):
            # 修好接着跑第 13 项：被环境隔离作废的 attempt 永不 seal，同样在任何落盘之前拦下。
            raise adapter.orchestrator.ConfigurationError(
                f"compile-and-run-vc-batch 拒绝：{action.get('action_id')} 要封存的 attempt {attempt_id} "
                "已被环境隔离作废（结果永不复用、永不 seal）。先 reconcile-attempt 入账并批准恢复预览，"
                "resume --rerun-failed 全部重跑，再对新 attempt 走 seal；本次未写入任何文件。"
            )
        if (side, candidate_id, attempt_id) in adapter.orchestrator._conflict_quarantined_attempts(campaign_dir):
            # 修好接着跑第 24 项：被证据根冲突隔离的 attempt 永不 seal，同样在任何落盘之前拦下。
            raise adapter.orchestrator.ConfigurationError(
                f"compile-and-run-vc-batch 拒绝：{action.get('action_id')} 要封存的 attempt {attempt_id} "
                "已被证据根冲突隔离（证据不可信，永不复用、永不 seal）。先 reconcile-attempt 入账并批准恢复预览，"
                "resume --rerun-failed 全部重跑，再对新 attempt 走 seal；本次未写入任何文件。"
            )


def _refuse_vc1_seal_batch_against_bundle_state(
    manifest: Mapping[str, Any],
    *,
    phase: str,
    action_plan: Any,
) -> None:
    """第 43 项：编译 VC-1 seal 链批次前，按断言证据包的实际状态零写入核对动作组合。

    与监督器 VC-1 断言包门禁（``_validate_vc1_assertion_seal_gate``，编译与派发时的队列清单校验）同一实现：断言包
    已发布后再派发断言包动作必然被脚本 write-once 拒绝覆盖；只含 seal 预览的续派要求断言包已发布且能核对为本
    attempt 同一冻结输入的产物；官方证据已封存则两者都拒绝。门禁在编译内拒绝会被记成 staging prepare 失败并计入
    根因上限，这里在任何落盘之前拦下并给出原因。动作计划本身的合法性仍由编译器校验，读不到就交给编译器报错。
    """

    adapter = _adapter()
    if phase != "VC-1" or not isinstance(action_plan, Path) or not action_plan.is_file():
        return
    try:
        plan = adapter.orchestrator._read_json(action_plan, "VC action plan")
    except adapter.orchestrator.ConfigurationError:
        return
    actions = plan.get("actions") if isinstance(plan, Mapping) else None
    if not isinstance(actions, list) or not all(isinstance(action, Mapping) for action in actions):
        return
    try:
        codex_upgrade_supervisor._validate_vc1_assertion_seal_gate(
            schema_version=codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            campaign_id=str(manifest.get("campaign_id", "")),
            phase=phase,
            actions=[dict(action) for action in actions],
            require_bound_files=True,
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise adapter.orchestrator.ConfigurationError(f"compile-and-run-vc-batch 拒绝：{error}本次未写入任何文件。") from error


def _require_tool_evolution_registered(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None = None,
    action: str,
) -> dict[str, Any] | None:
    """v2 Campaign 的零写入预检：未登记的 wire／evidence 变化立即报 ToolEvolutionRequired。

    返回有效身份（v1 Campaign 返回 None，不做预检）。调用方必须在任何写入（staging、孤儿
    对账、预约）之前调用，保证「先部署修复、后派发」不会被自动对账写成永久终态。
    """

    adapter = _adapter()
    frozen = manifest.get("tool_identity")
    if not isinstance(frozen, Mapping) or not adapter.orchestrator._is_policy_v2_identity(frozen):
        return None
    effective = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
    current_tool = current if current is not None else adapter.orchestrator._tool_identity(include_git=False)
    drift = adapter.orchestrator._tool_evolution_unregistered_drift(effective["identity"], current_tool)
    if drift:
        raise adapter.orchestrator.ToolEvolutionRequired(
            f"{action} 拒绝：当前受管工具的 {'、'.join(drift)} 与 Campaign 有效工具身份"
            f"（{'plan' if not effective['index'] else 'evolution-%02d' % effective['index']}）不一致，"
            "尚未登记工具演进。先执行 tool-evolution（预览→批准）登记本次修复，再继续；本次未写入任何文件。"
        )
    return effective


def _tool_evolution_candidate_jobs(
    campaign_dir: Path,
    manifest: dict[str, Any],
    candidate_id: str,
) -> tuple[list[Job] | None, list[str]]:
    """候选的计划作业：按最近一个带 attempt.json 的 attempt 身份展开。

    只有孤儿预约（没有任何 attempt.json）时返回 ``(None, 预约计划作业)``，调用方按保守口径
    把全部计划作业记为受影响。
    """

    adapter = _adapter()
    planned_ids: list[str] = []
    for attempt_root, reservation in adapter.orchestrator._ordered_capture_attempts(
        campaign_dir, "candidate", candidate_id, _manifest=manifest
    ):
        ids = sorted(str(row["id"]) for row in reservation["planned_jobs"])
        if not planned_ids:
            planned_ids = ids
        attempt_path = attempt_root / "attempt.json"
        if not attempt_path.is_file() or attempt_path.is_symlink():
            continue
        _root, payload = adapter.orchestrator._load_capture_attempt(
            campaign_dir,
            "candidate",
            candidate_id,
            attempt_root.name,
            _verified_campaign_manifest=manifest,
        )
        identity = payload.get("identity")
        if not isinstance(identity, Mapping):
            continue
        candidate_manifest = adapter.orchestrator._apply_candidate_runtime_override(
            campaign_dir, manifest, candidate_id
        )
        jobs = adapter.orchestrator._campaign_jobs(
            campaign_dir,
            candidate_manifest,
            "candidate",
            candidate_id=candidate_id,
            runtime_image=identity.get("image_reference"),
            profile_id=identity.get("profile_id"),
            profile_digest=identity.get("profile_digest"),
            build_id=identity.get("build_id"),
            deployed_version=identity.get("deployed_version"),
            candidate_image_id=identity.get("image_id"),
            source_tree_sha256=identity.get("source_tree_sha256"),
            candidate_purpose=identity.get("candidate_purpose"),
        )
        return jobs, sorted(job.job_id for job in jobs)
    return None, planned_ids


def _candidate_b0_evaluation_outputs(campaign_dir: Path, candidate_id: str) -> list[str]:
    """候选 b0 已有的评估产出（断言目录内任何文件、compare／accept 结果）。"""

    adapter = _adapter()
    found: list[str] = []
    assertions = adapter.orchestrator._assertions_root(campaign_dir, candidate_id)
    if assertions.is_symlink():
        found.append(assertions.relative_to(campaign_dir).as_posix())
    elif assertions.is_dir():
        for path in sorted(assertions.rglob("*")):
            if path.is_file() or path.is_symlink():
                found.append(path.relative_to(campaign_dir).as_posix())
                break
    for stage in ("compare", "accept"):
        _canonical, path = adapter.orchestrator._legacy_stage_path(campaign_dir, stage, candidate_id)
        if path.exists() or path.is_symlink():
            found.append(path.relative_to(campaign_dir).as_posix())
    return found


def _tool_evolution_quiescence_problems(campaign_dir: Path) -> list[str]:
    """登记演进前的静默核对：没有存活的 Campaign 租约，也没有仍在心跳的未收口 attempt。"""

    adapter = _adapter()
    problems: list[str] = []
    lease = adapter.orchestrator._read_campaign_lease(campaign_dir)
    if isinstance(lease, Mapping) and lease.get("state") == "active":
        stale, _reason, owner_alive, _expired = adapter.orchestrator._campaign_lease_staleness(lease)
        if owner_alive and not stale:
            problems.append(f"Campaign 租约仍由存活进程持有（{lease.get('command')}）")
    now = time.time()
    for _phase, _candidate, attempt_root in adapter.orchestrator._campaign_attempt_roots(campaign_dir):
        if (attempt_root / "attempt.json").exists():
            continue
        heartbeat = attempt_root / "watchdog-heartbeat.json"
        if heartbeat.is_file() and not heartbeat.is_symlink():
            age = now - heartbeat.stat().st_mtime
            if 0 <= age < TOOL_EVOLUTION_QUIESCENCE_SECONDS:
                problems.append(
                    f"attempt {attempt_root.name} 尚未收口且 {int(age)} 秒前仍有看门狗心跳"
                )
    return problems


def _tool_evolution_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """登记工具演进：不带 --approve-sha256 只输出预览；带上且与重算预览一致才落盘。"""

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    with adapter.orchestrator._campaign_lock(campaign_dir):
        preview = adapter.orchestrator._tool_evolution_preview(
            campaign_dir,
            manifest,
            fix_commit=str(arguments.fix_commit),
            reason=str(arguments.reason),
            control_root=getattr(arguments, "control_root", None),
            compatibility_receipt=getattr(arguments, "policy_compatibility_receipt", None),
            activation_certification=getattr(arguments, "policy_activation_certification", None),
        )
        summary = {
            "index": preview["index"],
            "from_summary": preview["from_summary"],
            "to_summary": preview["to_summary"],
            "changes": preview["changes"],
            "impact": preview["impact"],
            "evaluator": preview["evaluator"],
            "bindings": preview["bindings"],
            "policy_transition": preview.get("policy_transition"),
            "review_sha256": preview["review_sha256"],
            "live_request_count": 0,
        }
        approval = getattr(arguments, "approve_sha256", None)
        if approval is None:
            return {"status": "approval_required", **summary}
        if str(approval) != preview["review_sha256"]:
            raise adapter.orchestrator.ConfigurationError("批准摘要与重算的工具演进预览不一致；重新预览后再批准。")
        approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
        if not approved_by:
            raise adapter.orchestrator.ConfigurationError("批准工具演进必须提供 --approved-by。")
        payload = {key: value for key, value in preview.items() if key != "review_sha256"}
        payload["approved_sha256"] = preview["review_sha256"]
        payload["approved_by"] = approved_by
        payload["approved_at_utc"] = adapter.orchestrator._utc_now()
        payload["receipt_sha256"] = adapter.orchestrator._fingerprint(payload)
        try:
            path = codex_upgrade_wire_transition.write_evolution(campaign_dir, manifest, payload)
        except codex_upgrade_wire_transition.WireTransitionError as error:
            raise adapter.orchestrator.ConfigurationError(str(error)) from error
    return {
        "status": "evolution_applied",
        "path": str(path),
        "receipt_sha256": payload["receipt_sha256"],
        **summary,
    }


def _tool_evolution_status_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """只读：有效工具身份、演进链与当前受管工具是否需要登记。"""

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    chain = adapter.orchestrator._campaign_tool_evolutions(campaign_dir, manifest)
    effective = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
    current = adapter.orchestrator._tool_identity(include_git=False)
    frozen = manifest.get("tool_identity")
    drift = (
        adapter.orchestrator._tool_evolution_unregistered_drift(effective["identity"], current)
        if isinstance(frozen, Mapping) and adapter.orchestrator._is_policy_v2_identity(frozen)
        else []
    )
    return {
        "status": "evolution_required" if drift else "registered",
        "effective_index": effective["index"],
        "effective_summary": effective["summary"],
        "current_summary": codex_upgrade_wire_transition.identity_summary(current),
        "unregistered_drift": drift,
        "evolutions": [
            {
                "index": item["index"],
                "receipt_sha256": item["receipt_sha256"],
                "fix_commit": item["bindings"]["fix_commit"],
                "approved_at_utc": item.get("approved_at_utc"),
                "policy_transition": (
                    {
                        "from_policy_sha256": item["policy_transition"]["from_policy_sha256"],
                        "to_policy_sha256": item["policy_transition"]["to_policy_sha256"],
                        "to_policy_version": item["policy_transition"]["to_policy_version"],
                    }
                    if isinstance(item.get("policy_transition"), Mapping)
                    else None
                ),
                "official_affected": item["impact"]["official"].get("affected_job_ids"),
                "candidates_affected": {
                    cid: part.get("affected_job_ids")
                    for cid, part in item["impact"]["candidates"].items()
                },
            }
            for item in chain
        ],
        "live_request_count": 0,
    }


def _campaign_resume_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    fix_commit: str,
    regression_receipt: Path,
    reason: str,
    control_root: Path | None,
) -> dict[str, Any]:
    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    frozen = manifest.get("tool_identity")
    if not adapter.orchestrator._requires_complete_vc_artifacts(manifest) or not isinstance(frozen, Mapping) or not adapter.orchestrator._is_policy_v2_identity(frozen):
        raise adapter.orchestrator.ConfigurationError("campaign-resume 只用于 0.154.0 起、v2 工具身份的完整 VC 链 Campaign。")
    if not adapter.orchestrator.TOOL_EVOLUTION_FIX_COMMIT_RE.fullmatch(fix_commit):
        raise adapter.orchestrator.ConfigurationError("--fix-commit 必须是 40 位小写十六进制提交号。")
    if not reason.strip():
        raise adapter.orchestrator.ConfigurationError("--reason 不得为空。")
    problems = adapter.orchestrator._tool_evolution_quiescence_problems(campaign_dir)
    if problems:
        raise adapter.orchestrator.ConfigurationError("campaign-resume 要求 Campaign 静默：" + "；".join(problems))
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    try:
        facts = codex_upgrade_timing_ledger.resume_facts(ledger_dir)
    except codex_upgrade_timing_ledger.TimingLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"Campaign 计时账本无法重放：{error}") from error
    if facts["status"] not in {"stopped", "stop_required"}:
        raise adapter.orchestrator.ConfigurationError(f"Campaign 计时账本状态为 {facts['status']}，不是停线，无需 campaign-resume。")
    stop_event = facts["stop_event"]
    if facts["stopped"] and (
        stop_event is None or str(stop_event["event_id"]).startswith(_NON_RESUMABLE_STOP_EVENT_PREFIXES)
    ):
        raise adapter.orchestrator.ConfigurationError("该停线是显式关账本（人工决定），不可由 campaign-resume 恢复。")
    project_root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
    if project_root is None:
        raise adapter.orchestrator.ConfigurationError("campaign-resume 必须在项目总账内进行。")
    try:
        with codex_upgrade_project_ledger.project_lock(project_root):
            plan, _raw = codex_upgrade_project_ledger._load_plan(project_root)
            head = codex_upgrade_project_ledger.replay_head(project_root)
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"项目总账重放失败：{error}") from error
    campaign_id = str(manifest["campaign_id"])
    terminal = head["terminal_campaigns"].get(campaign_id)
    if terminal is not None and terminal["terminal_reason"] not in codex_upgrade_project_ledger.RESUMABLE_TERMINAL_REASONS:
        raise adapter.orchestrator.ConfigurationError(f"总账终态原因 {terminal['terminal_reason']} 不可恢复（完整性、人工放弃／取代或已完成）。")
    target_version = codex_upgrade_project_ledger.campaign_target_version(head, campaign_id)
    project_at_limit = sorted(codex_upgrade_project_ledger.root_causes_at_limit_for(head, target_version))
    # 以下三项先各自处理再恢复：未决账务（accounting-resolve）、请求预算（request-budget-extend）与环境污染。
    blocking = codex_upgrade_project_ledger.campaign_blocked(head, campaign_id)
    if blocking:
        # 第三批 B3-10：只看本 Campaign 或无归属的未决账务。
        raise adapter.orchestrator.ConfigurationError(
            f"项目总账 blocked（未决账务 {blocking}），先以 accounting-resolve 补账再恢复。"
        )
    remaining = head.get("remaining_live_requests")
    if remaining is not None and int(remaining) <= 0:
        raise adapter.orchestrator.ConfigurationError("项目请求预算已耗尽，先批准请求预算延长再恢复。")
    if adapter.orchestrator._campaign_contamination_records(campaign_dir, _manifest=manifest):
        raise adapter.orchestrator.ConfigurationError("Campaign 存在未隔离的环境污染记录，先以 environment-isolate 隔离污染再恢复。")
    if terminal is not None:
        open_formal = [
            cid
            for cid, item in head["registered_campaigns"].items()
            if item.get("campaign_mode") == "formal"
            and item.get("target_version") == target_version
            and cid not in head["terminal_campaigns"]
            and cid != campaign_id
        ]
        if len(open_formal) >= int(plan["formal_open_limit"]):
            raise adapter.orchestrator.ConfigurationError(f"同版本无终态 formal Campaign 已达上限：{open_formal}，恢复会超额。")
    current = adapter.orchestrator._tool_identity(include_git=False)
    # R1：对照 Campaign 有效策略（策略演进登记后即最新演进的策略），而不是 plan 冻结策略。
    effective_policy = (
        adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)["identity"].get("policy_sha256")
        if adapter.orchestrator._is_policy_v2_identity(frozen)
        else frozen.get("policy_sha256")
    )
    if current.get("policy_sha256") != effective_policy:
        raise adapter.orchestrator.ConfigurationError(
            "当前工具策略与 Campaign 有效策略不同；先以 tool-evolution 登记策略演进（绑定 A2.6 兼容收据与激活认证）再恢复。"
        )
    try:
        deployment = reconciler._deployment_receipt(
            reconciler._control_root(campaign_dir, control_root), current, required=True
        )
    except (reconciler.ReconcilerError, reconciler.closeout.VC0CloseoutError) as error:
        raise adapter.orchestrator.ConfigurationError(f"campaign-resume 必须绑定当前工具的通过部署收据：{error}") from error
    assert deployment is not None
    # 第三批 B3-9：同根因达上限只暂停、没有停线事件时，以导致达上限的最后一次失败事件为修复部署的时间基准。
    reference_event = stop_event if stop_event is not None else facts.get("limit_event")
    if reference_event is not None and adapter.orchestrator._rfc3339_datetime(deployment["created_at_utc"], "部署收据时间") <= adapter.orchestrator._rfc3339_datetime(
        reference_event["recorded_at_utc"], "停线事件时间"
    ):
        raise adapter.orchestrator.ConfigurationError("部署收据早于停线；修复必须在停线之后受监督部署。")
    regression = Path(regression_receipt)
    if not regression.is_absolute() or regression.is_symlink() or not regression.is_file():
        raise adapter.orchestrator.ConfigurationError("--regression-receipt 必须是可信的绝对路径普通文件。")
    effective = adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)
    drift = adapter.orchestrator._tool_evolution_unregistered_drift(effective["identity"], current)
    primary = (
        (stop_event or {}).get("root_cause_id")
        or (facts["at_limit_root_cause_ids"] or [None])[0]
        or (project_at_limit or [None])[0]
    )
    if not isinstance(primary, str) or not primary:
        raise adapter.orchestrator.ConfigurationError("停线没有登记根因，无法绑定恢复。")
    preview = {
        "schema_version": CAMPAIGN_RESUME_PREVIEW_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": adapter.orchestrator.file_sha256(campaign_dir / "campaign.json"),
        "root_cause_id": primary,
        "timing": {
            "status": facts["status"],
            "stop_event": (
                None if stop_event is None else {"sequence": stop_event["sequence"], "sha256": stop_event["sha256"]}
            ),
            "stop_event_id": None if stop_event is None else stop_event["event_id"],
            "cleared_root_cause_ids": list(facts["at_limit_root_cause_ids"]),
            "resume_epoch": int(facts["resume_epoch"]),
            "campaign_ledger_head": {"sequence": facts["head_sequence"], "sha256": facts["head_sha256"]},
        },
        "project": {
            "terminal": None if terminal is None else {
                "operation_id": terminal["operation_id"], "terminal_reason": terminal["terminal_reason"],
            },
            "cleared_root_cause_ids": project_at_limit,
            "head": {"sequence": head["sequence"], "sha256": head["head_sha256"]},
        },
        "bindings": {
            "fix_commit": fix_commit,
            "regression_receipt": {"path": str(regression), "sha256": adapter.orchestrator.file_sha256(regression)},
            "deployment_receipt": {
                "path": str(deployment["path"]),
                "sha256": str(deployment["sha256"]),
                "created_at_utc": deployment["created_at_utc"],
            },
        },
        "unregistered_tool_drift": drift,
        "reason": reason,
        "live_request_count": 0,
    }
    preview["review_sha256"] = adapter.orchestrator._fingerprint(preview)
    return preview


def _campaign_resume_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """campaign-resume：不带 --approve-sha256 只预览；批准后按"批准收据 → 总账根因修复 → 计时账本恢复 →
    总账 campaign_resumed"顺序幂等写入，重跑同一批准即补齐半完成状态。"""

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    next_steps = (
        "恢复后先按 tool-evolution-status 登记未登记的工具演进，再对账停线对象（reconcile-attempt／"
        "reconcile-supervisor-run），批准恢复预览后续跑"
    )
    approval = getattr(arguments, "approve_sha256", None)
    if approval is not None:
        # 同一批准重跑：批准收据已落盘时按收据冻结的预览续接（计时账本可能已恢复、不再是停线，
        # 不能重算预览），补齐半完成的两本账；已完成则幂等返回。
        stored_path = campaign_dir / CAMPAIGN_RESUME_DIR / f"resume-{approval}.json"
        if stored_path.is_file() and not stored_path.is_symlink():
            stored = adapter.orchestrator._read_json(stored_path, "campaign-resume 批准收据")
            frozen_preview = stored.get("preview")
            if (
                stored.get("approved_sha256") != str(approval)
                or not isinstance(frozen_preview, Mapping)
                or frozen_preview.get("review_sha256") != str(approval)
                or adapter.orchestrator._fingerprint({k: v for k, v in frozen_preview.items() if k != "review_sha256"}) != str(approval)
            ):
                raise adapter.orchestrator.ConfigurationError("既有 campaign-resume 批准收据与批准摘要不一致。")
            return adapter.orchestrator._apply_campaign_resume(
                campaign_dir, manifest, frozen_preview, approved_by=str(stored["approved_by"]), next_steps=next_steps
            )
    preview = adapter.orchestrator._campaign_resume_preview(
        campaign_dir,
        manifest,
        fix_commit=str(arguments.fix_commit),
        regression_receipt=Path(arguments.regression_receipt),
        reason=str(arguments.reason),
        control_root=getattr(arguments, "control_root", None),
    )
    if approval is None:
        return {"status": "approval_required", **preview, "next_command": "campaign-resume --approve-sha256 <review_sha256> --approved-by <批准人>"}
    if str(approval) != preview["review_sha256"]:
        raise adapter.orchestrator.ConfigurationError("批准摘要与重算的 campaign-resume 预览不一致；重新预览后再批准。")
    approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
    if not approved_by:
        raise adapter.orchestrator.ConfigurationError("批准 campaign-resume 必须提供 --approved-by。")
    return adapter.orchestrator._apply_campaign_resume(campaign_dir, manifest, preview, approved_by=approved_by, next_steps=next_steps)


def _apply_campaign_resume(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    preview: Mapping[str, Any],
    *,
    approved_by: str,
    next_steps: str,
) -> dict[str, Any]:
    adapter = _adapter()
    campaign_id = str(manifest["campaign_id"])
    project_root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
    assert project_root is not None
    ledger_dir = adapter.orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    review = str(preview["review_sha256"])
    timing = preview["timing"]
    resume_dir = campaign_dir / CAMPAIGN_RESUME_DIR
    try:
        with codex_upgrade_project_ledger.deadline_control_scope(campaign_dir), \
                codex_upgrade_project_ledger.project_lock(project_root), \
                adapter.orchestrator._campaign_lock(campaign_dir), \
                codex_upgrade_project_ledger._flock(ledger_dir / ".vc0-closeout.lock", "campaign-resume"):
            ensure_private_directory(resume_dir, campaign_dir)
            receipt_path = resume_dir / f"resume-{review}.json"
            if receipt_path.exists():
                receipt = adapter.orchestrator._read_json(receipt_path, "campaign-resume 批准收据")
                if receipt.get("approved_sha256") != review:
                    raise adapter.orchestrator.ConfigurationError("既有 campaign-resume 批准收据与本次批准不一致。")
            else:
                receipt = {
                    "schema_version": codex_upgrade_timing_ledger.CAMPAIGN_RESUME_SCHEMA,
                    "campaign_id": campaign_id,
                    "root_cause_id": preview["root_cause_id"],
                    "campaign_ledger_head": dict(timing["campaign_ledger_head"]),
                    "timing_stop_event": timing["stop_event"],
                    "timing_cleared_root_cause_ids": list(timing["cleared_root_cause_ids"]),
                    "project_terminal": preview["project"]["terminal"],
                    "project_cleared_root_cause_ids": list(preview["project"]["cleared_root_cause_ids"]),
                    "bindings": preview["bindings"],
                    "reason": preview["reason"],
                    "preview": dict(preview),
                    "approved_sha256": review,
                    "approved_by": approved_by,
                    "approved_at_utc": adapter.orchestrator._utc_now(),
                }
                receipt["receipt_sha256"] = adapter.orchestrator._fingerprint(receipt)
                adapter.orchestrator._secure_write_json_once(receipt_path, receipt)
            # 总账根因修复：只清零批准时本版本已达上限的根因（修复收据按绑定幂等）。
            bindings = preview["bindings"]
            if preview["project"]["cleared_root_cause_ids"]:
                codex_upgrade_project_ledger.record_root_cause_repair(
                    project_root,
                    root_cause_ids=list(preview["project"]["cleared_root_cause_ids"]),
                    kind="code",
                    bindings={
                        "fix_commit_sha": bindings["fix_commit"],
                        "regression_receipt_sha256": bindings["regression_receipt"]["sha256"],
                        "deployment_receipt_sha256": bindings["deployment_receipt"]["sha256"],
                    },
                    note=f"campaign-resume {review[:16]}",
                    # 修复收据时间取批准时间：收据摘要与 operation 确定，同一批准重跑不会重复入账。
                    now=str(receipt["approved_at_utc"]),
                )
            # 计时账本恢复事件：三份收据复制进账本目录（账本只接受目录内文件）。
            event_id = f"campaign-resume-{review[:16]}"
            existing = [event for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir) if event["event_id"] == event_id]
            if not existing:
                receipts_dir = ledger_dir / "receipts" / "campaign-resume" / review[:16]
                receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                copies = {
                    "campaign_resume": (receipts_dir / "campaign-resume.json", receipt_path.read_bytes()),
                    "offline_regression": (receipts_dir / "offline-regression.receipt", Path(bindings["regression_receipt"]["path"]).read_bytes()),
                    "tool_fix": (receipts_dir / "tool-fix-deployment.json", Path(bindings["deployment_receipt"]["path"]).read_bytes()),
                }
                ledger_receipts = []
                for role, (path, content) in sorted(copies.items()):
                    if path.exists():
                        if path.read_bytes() != content:
                            raise adapter.orchestrator.ConfigurationError(f"账本内既有 {role} 收据副本与本次不一致。")
                    else:
                        path.write_bytes(content)
                        path.chmod(0o600)
                    ledger_receipts.append({"role": role, "path": path.relative_to(ledger_dir).as_posix(), "sha256": adapter.orchestrator.file_sha256(path)})
                if bindings["regression_receipt"]["sha256"] != adapter.orchestrator.file_sha256(copies["offline_regression"][0]):
                    raise adapter.orchestrator.ConfigurationError("回归收据在预览后被修改。")
                codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id=event_id,
                    phase=str((timing.get("stop_event") and adapter.orchestrator._timing_event_phase(ledger_dir, int(timing["stop_event"]["sequence"]))) or codex_upgrade_timing_ledger.inspect_ledger(ledger_dir).get("active_phase") or "VC-0"),
                    event_type="recovery_verified",
                    root_cause_id=str(preview["root_cause_id"]),
                    receipts=ledger_receipts,
                    next_action="campaign-resume：按恢复后的状态对账停线对象，批准恢复预览后续跑",
                )
            campaign_event = next(
                (event, raw) for event, raw in codex_upgrade_timing_ledger._load_events(ledger_dir) if event["event_id"] == event_id
            )
            epoch = int(timing["resume_epoch"]) + 1
            _after, project_event = codex_upgrade_project_ledger.append_project_event(
                project_root,
                operation_id=f"campaign-resumed:{campaign_id}:e{epoch}",
                event_type="campaign_resumed",
                payload={
                    "campaign_id": campaign_id,
                    "terminal_operation_id": (preview["project"]["terminal"] or {}).get("operation_id"),
                    "terminal_reason": (preview["project"]["terminal"] or {}).get("terminal_reason"),
                    "resume_receipt_sha256": str(receipt["receipt_sha256"]),
                    "campaign_event": {
                        "sequence": int(campaign_event[0]["sequence"]),
                        "sha256": hashlib.sha256(campaign_event[1]).hexdigest(),
                    },
                },
                source_batch_sha256=None,
            )
    except (codex_upgrade_project_ledger.ProjectLedgerError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise adapter.orchestrator.ConfigurationError(f"campaign-resume 写入失败（以同一批准重跑即续接）：{error}") from error
    summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
    return {
        "status": "resumed",
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt["receipt_sha256"],
        "timing_status": summary["status"],
        "active_phase": summary["active_phase"],
        "project_event": project_event,
        "resume_epoch": int(timing["resume_epoch"]) + 1,
        "next_command": next_steps,
    }


def _accounting_resolve_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    operation_id: str,
    estimated_count: int | None,
    evidence: Path | None,
    reason: str,
) -> dict[str, Any]:
    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    if estimated_count is not None and (
        isinstance(estimated_count, bool) or not isinstance(estimated_count, int) or estimated_count < 0
    ):
        raise adapter.orchestrator.ConfigurationError("--estimated-count 必须是非负整数（未决作业请求数的批准上界）。")
    if not reason.strip():
        raise adapter.orchestrator.ConfigurationError("--reason 不得为空。")
    if evidence is not None:
        evidence = Path(evidence)
        if not evidence.is_absolute() or evidence.is_symlink() or not evidence.is_file():
            raise adapter.orchestrator.ConfigurationError("--evidence 必须是可信的绝对路径普通文件（补账依据的来源审计）。")
    project_root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
    if project_root is None:
        raise adapter.orchestrator.ConfigurationError("accounting-resolve 必须在项目总账内进行。")
    campaign_id = str(manifest["campaign_id"])
    try:
        with codex_upgrade_project_ledger.project_lock(project_root):
            plan, _raw = codex_upgrade_project_ledger._load_plan(project_root)
            head = codex_upgrade_project_ledger.replay_head(project_root)
            events = codex_upgrade_project_ledger._load_events(project_root)
            initial_keys = codex_upgrade_project_ledger._initial_keys(project_root, plan)
    except codex_upgrade_project_ledger.ProjectLedgerError as error:
        raise adapter.orchestrator.ConfigurationError(f"项目总账重放失败：{error}") from error
    if operation_id not in head["unresolved_operation_ids"]:
        raise adapter.orchestrator.ConfigurationError(f"operation {operation_id} 不在总账未决集合里。")
    original = next((event for event in events if event["operation_id"] == operation_id), None)
    # 与总账重放同一口径：原 operation 若有追加式历史更正，按更正后的 payload 判断归属与未决作业。
    overlays, _audit = codex_upgrade_project_ledger._correction_overlays(events)
    original_payload = overlays.get(operation_id, original["payload"]) if original is not None else {}
    if original is None or original_payload.get("campaign_id") != campaign_id:
        raise adapter.orchestrator.ConfigurationError("未决 operation 不属于本 Campaign；只能由所属 Campaign 补账。")
    original_unresolved = {
        str(item) for item in (original_payload.get("request") or {}).get("unresolved_job_ids", [])
    }
    try:
        receipt = reconciler.provenance.collect_campaign_provenance(
            campaign_dir,
            formal_campaign_id=campaign_id,
            estimation_policy=str(plan["estimation_policy"]),
            observed_at_utc=adapter.orchestrator._utc_now(),
        )
    except Exception as error:  # 来源核算的各类失败都只意味着本次不能补账
        raise adapter.orchestrator.ConfigurationError(f"来源核算失败：{error}") from error
    accounting = reconciler.request_accounting(receipt, head=head, initial_keys=initial_keys, campaign_id=campaign_id)
    entries = {
        str(item.get("job_id")): item
        for item in receipt.get("jobs", [])
        if isinstance(item, Mapping) and item.get("status") == "unresolved"
    }
    still_unresolved = sorted(original_unresolved & set(accounting["unresolved_job_ids"]))
    if any(job_id not in entries for job_id in still_unresolved):
        raise adapter.orchestrator.ConfigurationError("来源核算的未决作业清单与作业条目不一致；先排查来源核算。")
    covered = [
        {"job_id": job_id, "signature_sha256": reconciler.unresolved_job_signature(entries[job_id])}
        for job_id in still_unresolved
    ]
    if covered and (estimated_count is None or evidence is None):
        raise adapter.orchestrator.ConfigurationError(
            f"原 operation 仍有按当前证据无法核清的作业 {still_unresolved}：必须以 --estimated-count 给出请求数批准上界，"
            "并以 --evidence 附来源审计。"
        )
    if not covered and estimated_count is not None:
        raise adapter.orchestrator.ConfigurationError(
            "原 operation 的未决作业按当前证据已全部核清：按精确入账补账，不接受 --estimated-count。"
        )
    sources = [dict(item) for item in accounting["estimated_sources"]]
    if covered and int(estimated_count or 0) > 0:
        sources.append(
            {
                "source_id": f"{campaign_id}:accounting-resolve:{operation_id}",
                "job_id": covered[0]["job_id"],
                "estimated_count": int(estimated_count or 0),
            }
        )
    request = {
        # 批准上界为 0 且没有其他估计来源，表示未决作业确认未发出请求，按已核清入账。
        "status": "estimated" if sources else "resolved",
        "identity_keys": list(accounting["new_keys"]),
        "estimated_delta": sum(int(item["estimated_count"]) for item in sources),
        "estimated_sources": sources,
        # 去掉观测时间的来源核算稳定摘要：任何人在同一证据上复算都得到同一值。
        "provenance_receipt_sha256": adapter.orchestrator._fingerprint({k: v for k, v in receipt.items() if k != "observed_at_utc"}),
    }
    preview = {
        "schema_version": ACCOUNTING_RESOLUTION_PREVIEW_SCHEMA,
        "campaign_id": campaign_id,
        "operation_id": operation_id,
        "original_payload_sha256": str(original["payload_sha256"]),
        "resolution_mode": "estimated_upper_bound" if covered else "precise_from_current_evidence",
        "covered_unresolved": covered,
        "estimated_count": int(estimated_count) if covered and estimated_count is not None else None,
        "request": request,
        "evidence": (
            {"path": str(evidence), "sha256": adapter.orchestrator.file_sha256(evidence)} if evidence is not None else None
        ),
        "reason": reason,
        "project_ledger_head": {"sequence": head["sequence"], "sha256": head["head_sha256"]},
        "live_request_count": 0,
    }
    preview["review_sha256"] = adapter.orchestrator._fingerprint(preview)
    return preview


def _accounting_resolve_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """accounting-resolve：不带 --approve-sha256 只预览；批准后写一次批准收据与 accounting_resolved batch 并推送。"""

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    approval = getattr(arguments, "approve_sha256", None)
    if approval is not None:
        # 同一批准重跑：批准收据已落盘时按收据冻结的预览续接（operation 可能已被本批准核清、不再未决，
        # 不能重算预览），补齐未推送的 batch；已完成则幂等返回。
        stored_path = campaign_dir / ACCOUNTING_RESOLUTION_DIR / f"resolve-{approval}.json"
        if stored_path.is_file() and not stored_path.is_symlink():
            stored = adapter.orchestrator._read_json(stored_path, "补账批准收据")
            frozen_preview = stored.get("preview")
            if (
                stored.get("approved_sha256") != str(approval)
                or not isinstance(frozen_preview, Mapping)
                or frozen_preview.get("review_sha256") != str(approval)
                or adapter.orchestrator._fingerprint({k: v for k, v in frozen_preview.items() if k != "review_sha256"}) != str(approval)
            ):
                raise adapter.orchestrator.ConfigurationError("既有补账批准收据与批准摘要不一致。")
            return adapter.orchestrator._apply_accounting_resolution(
                campaign_dir, manifest, frozen_preview, approved_by=str(stored["approved_by"])
            )
    preview = adapter.orchestrator._accounting_resolve_preview(
        campaign_dir,
        manifest,
        operation_id=str(arguments.operation_id),
        estimated_count=None if arguments.estimated_count is None else int(arguments.estimated_count),
        evidence=None if arguments.evidence is None else Path(arguments.evidence),
        reason=str(arguments.reason),
    )
    if approval is None:
        return {
            "status": "approval_required",
            **preview,
            "next_command": "accounting-resolve（同样参数）--approve-sha256 <review_sha256> --approved-by <批准人>",
        }
    if str(approval) != preview["review_sha256"]:
        raise adapter.orchestrator.ConfigurationError("批准摘要与重算的补账预览不一致（总账或证据已变化）；重新预览后再批准。")
    approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
    if not approved_by:
        raise adapter.orchestrator.ConfigurationError("批准补账必须提供 --approved-by。")
    return adapter.orchestrator._apply_accounting_resolution(campaign_dir, manifest, preview, approved_by=approved_by)


def _apply_accounting_resolution(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    preview: Mapping[str, Any],
    *,
    approved_by: str,
) -> dict[str, Any]:
    """按"证据副本 → 批准收据 → outbox batch → 推送"顺序幂等写入；任一步中断后以同一批准重跑即续接。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    campaign_id = str(manifest["campaign_id"])
    operation_id = str(preview["operation_id"])
    review = str(preview["review_sha256"])
    evidence = preview.get("evidence")
    with adapter.orchestrator._campaign_lock(campaign_dir):
        directory = campaign_dir / ACCOUNTING_RESOLUTION_DIR
        ensure_private_directory(directory, campaign_dir)
        evidence_copy: Path | None = None
        if isinstance(evidence, Mapping):
            # 证据逐字节复制进 Campaign（按摘要命名、只写一次）：批准依据随 Campaign 保存，原文件之后被清理也可审计。
            evidence_sha256 = str(evidence["sha256"])
            evidence_copy = directory / f"evidence-{evidence_sha256}"
            if not evidence_copy.exists():
                source = Path(str(evidence["path"]))
                if source.is_symlink() or not source.is_file() or adapter.orchestrator.file_sha256(source) != evidence_sha256:
                    raise adapter.orchestrator.ConfigurationError("补账证据在预览后被移走或改动；重新预览后再批准。")
                copied = adapter.orchestrator._secure_copy_file_once(source, evidence_copy)
                if copied["sha256"] != evidence_sha256:
                    # 复制途中被改动：删掉按预期摘要命名的错误副本，避免之后的重跑被它卡住。
                    evidence_copy.unlink(missing_ok=True)
                    raise adapter.orchestrator.ConfigurationError("补账证据在复制途中被改动；重新预览后再批准。")
            elif evidence_copy.is_symlink() or adapter.orchestrator.file_sha256(evidence_copy) != evidence_sha256:
                raise adapter.orchestrator.ConfigurationError(f"补账证据副本与预览摘要不一致：{evidence_copy}")
        receipt_path = directory / f"resolve-{review}.json"
        if receipt_path.exists():
            receipt = adapter.orchestrator._read_json(receipt_path, "补账批准收据")
            if receipt.get("approved_sha256") != review:
                raise adapter.orchestrator.ConfigurationError("既有补账批准收据与本次批准不一致。")
        else:
            receipt = {
                "schema_version": ACCOUNTING_RESOLUTION_SCHEMA,
                "preview": dict(preview),
                "approved_sha256": review,
                "approved_by": approved_by,
                "approved_at_utc": adapter.orchestrator._utc_now(),
            }
            receipt["receipt_sha256"] = adapter.orchestrator._fingerprint(receipt)
            adapter.orchestrator._secure_write_json_once(receipt_path, receipt)
    bindings = [reconciler._binding(campaign_dir, receipt_path, "accounting_resolution")]
    if evidence_copy is not None:
        bindings.append(reconciler._binding(campaign_dir, evidence_copy, "accounting_resolution_evidence"))
    try:
        batch = reconciler._commit_batch(
            campaign_dir,
            operation_id=f"accounting-resolved:{operation_id}",
            event_type="accounting_resolved",
            payload={
                "campaign_id": campaign_id,
                "resolved_operation_id": operation_id,
                # 预览按与对账同一口径算好的请求部分（精确身份键、未入账估计来源、批准上界），批准后逐字入账。
                "request": dict(preview["request"]),
                "covered_unresolved": list(preview["covered_unresolved"]),
                "resolution_receipt_sha256": str(receipt["receipt_sha256"]),
            },
            source={"kind": "accounting_resolution", "sha256": str(receipt["receipt_sha256"])},
            receipt_bindings=bindings,
        )
        project_root = codex_upgrade_project_ledger.find_project_ledger(campaign_dir)
        assert project_root is not None
        pushed, head_after = reconciler._push_and_replay(project_root, campaign_dir, now=adapter.orchestrator._utc_now())
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(f"补账写入失败（以同一批准重跑即续接）：{error}") from error
    remaining = [str(item) for item in head_after.get("unresolved_operation_ids", [])]
    return {
        "status": "resolved",
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt["receipt_sha256"],
        "batch": batch,
        "project_push": pushed,
        "blocked": bool(head_after.get("blocked")),
        "next_command": (
            f"总账仍有未决 operation {remaining}：由各自所属 Campaign 逐个 accounting-resolve 后再对账"
            if remaining
            else "账务已补齐：重新对账暂停的对象（reconcile-attempt／reconcile-supervisor-run）后续跑"
        ),
    }


def _optional_json_object(path: Path) -> dict[str, Any] | None:
    """文件是 JSON 对象时返回它；不是 JSON 或不是对象时返回 None（修复收据允许是任意文本）。"""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _environment_isolate_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    repair_receipt: Path,
    clean_receipt: Path,
    reason: str,
) -> dict[str, Any]:
    """冻结本次要覆盖的未隔离污染事实、作废的 attempt 与两份环境收据；预览零写入。"""

    adapter = _adapter()
    if not reason.strip():
        raise adapter.orchestrator.ConfigurationError("--reason 不得为空。")
    for label, path in (
        ("--environment-repair-receipt", repair_receipt),
        ("--clean-environment-receipt", clean_receipt),
    ):
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise adapter.orchestrator.ConfigurationError(f"{label} 必须是可信的绝对路径普通文件。")
    clean_document = adapter.orchestrator._read_json(clean_receipt, "干净环境复核收据")
    field = next((name for name in CLEAN_ENVIRONMENT_TIME_FIELDS if name in clean_document), None)
    if field is None:
        raise adapter.orchestrator.ConfigurationError(
            f"干净环境复核收据缺少观测时间（{'／'.join(CLEAN_ENVIRONMENT_TIME_FIELDS)} 之一）；"
            "应使用修复后新取的环境探针清单或 ARM64 环境收据。"
        )
    clean_at = adapter.orchestrator._rfc3339_datetime(clean_document[field], f"干净环境复核收据 {field}")
    if clean_at > datetime.now(timezone.utc):
        raise adapter.orchestrator.ConfigurationError("干净环境复核收据的观测时间晚于当前时间。")
    facts = adapter.orchestrator._campaign_contamination_facts(campaign_dir, _manifest=manifest)
    chain = adapter.orchestrator._environment_isolations(campaign_dir)
    covered_keys = {
        (str(item.get("record")), (item.get("source") or {}).get("sha256"))
        for receipt in chain
        for item in receipt["covered"]
        if isinstance(item, Mapping)
    }
    effective = [
        fact for fact in facts if (fact["record"], fact["source"]["sha256"]) not in covered_keys
    ]
    if not effective:
        raise adapter.orchestrator.ConfigurationError("没有未隔离的环境污染事实，无需 environment-isolate。")
    if any(fact["source"]["sha256"] is None for fact in effective):
        raise adapter.orchestrator.ConfigurationError("污染事实的来源文件不可信（不是普通文件），不能按摘要覆盖；先人工审计。")
    if adapter.orchestrator._stage_path(campaign_dir, "capture-official", None)[1].is_file() and any(
        fact["phase"] == "official" for fact in effective
    ):
        raise adapter.orchestrator.ConfigurationError(
            "官方阶段已封存且官方侧存在未隔离的环境污染：Campaign 内无法重采官方证据，不能隔离（只能终态）。"
        )
    timed = [
        adapter.orchestrator._rfc3339_datetime(fact["at_utc"], f"污染事实 {fact['record']} 时间")
        for fact in effective
        if fact["at_utc"] is not None
    ]
    if timed and clean_at <= max(timed):
        raise adapter.orchestrator.ConfigurationError(
            "干净环境复核必须晚于所覆盖的全部污染事实（修复后重新取证）；"
            f"复核时间 {clean_document[field]}，最近污染 {max(timed).isoformat()}。"
        )
    invalidated = sorted(
        {
            (str(fact["phase"]), fact["candidate_id"] or "", str(fact["attempt_id"]), fact["recovery_revision"] or "")
            for fact in effective
            if fact["kind"] in {"attempt", "seal", "recovery"}
        }
    )
    # 第三批 B3-14（R3 污染部分）：repair 收据可选给出污染发生时刻（RFC3339，操作者依据修复取证给出）。给出即校验：
    # 不晚于所覆盖事实的最早发现时刻、早于干净环境复核、晚于每个被作废 attempt 的预约时刻；缺省或收据不是 JSON 对象即
    # 无窗口（隔离作废 attempt 的已完成作业全部重跑，原口径）。
    contamination_started_raw: str | None = None
    repair_document = adapter.orchestrator._optional_json_object(repair_receipt)
    if repair_document is not None and repair_document.get("contamination_started_at_utc") is not None:
        contamination_started_raw = str(repair_document["contamination_started_at_utc"])
        contamination_started_at = adapter.orchestrator._rfc3339_datetime(contamination_started_raw, "修复收据 contamination_started_at_utc")
        if timed and contamination_started_at > min(timed):
            raise adapter.orchestrator.ConfigurationError(
                "修复收据的污染发生时刻晚于所覆盖事实的最早发现时刻；窗口起点只能在发现之前"
                f"（发生 {contamination_started_raw}，最早发现 {min(timed).isoformat()}）。"
            )
        if contamination_started_at >= clean_at:
            raise adapter.orchestrator.ConfigurationError("修复收据的污染发生时刻不早于干净环境复核时间。")
        for phase, candidate_id, attempt_id, recovery_revision in invalidated:
            if recovery_revision:
                continue
            attempt_root = campaign_dir / adapter.orchestrator._capture_attempt_relative(phase, candidate_id or None) / "attempts" / attempt_id
            reservation = adapter.orchestrator._load_capture_reservation(
                campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id or None, _manifest=manifest
            )
            begun = adapter.orchestrator._rfc3339_datetime(str(reservation.get("started_at_utc")), f"attempt {attempt_id} 预约时间")
            if contamination_started_at <= begun:
                raise adapter.orchestrator.ConfigurationError(
                    f"修复收据的污染发生时刻不晚于被作废 attempt {attempt_id} 的预约时刻；该 attempt 没有污染前的作业可复用，"
                    "删去该字段按全部重跑隔离。"
                )
    preview = {
        "schema_version": ENVIRONMENT_ISOLATION_PREVIEW_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "index": len(chain) + 1,
        "previous_isolation_sha256": str(chain[-1]["isolation_sha256"]) if chain else None,
        "covered": [dict(fact) for fact in effective],
        "invalidated_attempts": [
            {
                "phase": phase,
                "candidate_id": candidate_id or None,
                "attempt_id": attempt_id,
                "recovery_revision": recovery_revision or None,
            }
            for phase, candidate_id, attempt_id, recovery_revision in invalidated
        ],
        "environment_repair": {"source_path": str(repair_receipt), "sha256": adapter.orchestrator.file_sha256(repair_receipt)},
        "clean_environment": {
            "source_path": str(clean_receipt),
            "sha256": adapter.orchestrator.file_sha256(clean_receipt),
            "observed_at_utc": str(clean_document[field]),
        },
        "contamination_started_at_utc": contamination_started_raw,
        "reason": reason,
        "live_request_count": 0,
    }
    preview["review_sha256"] = adapter.orchestrator._fingerprint(preview)
    return preview


def _environment_isolate_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """environment-isolate：不带 --approve-sha256 只预览；批准后复制两份环境收据并写一次隔离收据。"""

    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    approval = getattr(arguments, "approve_sha256", None)
    next_steps = (
        "隔离已生效：对账暂停的对象（reconcile-attempt／reconcile-supervisor-run）后按恢复预览续跑；"
        "被作废的 attempt 永不复用、永不 seal，按失败 attempt 全部重跑"
    )
    if approval is not None:
        # 同一批准重跑：隔离收据已写入则幂等返回（写一次的收据是唯一落盘点）。
        for receipt in adapter.orchestrator._environment_isolations(campaign_dir):
            if receipt.get("review_sha256") == str(approval):
                return {
                    "status": "isolated",
                    "reused": True,
                    "isolation_index": receipt["index"],
                    "isolation_sha256": receipt["isolation_sha256"],
                    "invalidated_attempts": receipt["invalidated_attempts"],
                    "next_command": next_steps,
                }
    options = {
        "repair_receipt": Path(arguments.environment_repair_receipt),
        "clean_receipt": Path(arguments.clean_environment_receipt),
        "reason": str(arguments.reason),
    }
    preview = adapter.orchestrator._environment_isolate_preview(campaign_dir, manifest, **options)
    if approval is None:
        return {
            "status": "approval_required",
            **preview,
            "next_command": "environment-isolate（同样参数）--approve-sha256 <review_sha256> --approved-by <批准人>",
        }
    approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
    if not approved_by:
        raise adapter.orchestrator.ConfigurationError("批准 environment-isolate 必须提供 --approved-by。")
    with adapter.orchestrator._campaign_lock(campaign_dir):
        # 锁内重算：预览之后若出现新的污染事实或别的隔离，批准即作废，必须重新预览。
        preview = adapter.orchestrator._environment_isolate_preview(campaign_dir, manifest, **options)
        if str(approval) != preview["review_sha256"]:
            raise adapter.orchestrator.ConfigurationError("批准摘要与重算的隔离预览不一致（污染事实、隔离链或环境收据已变化）；重新预览后再批准。")
        directory = campaign_dir / adapter.orchestrator.ENVIRONMENT_ISOLATION_DIR
        ensure_private_directory(directory, campaign_dir)
        bindings: dict[str, dict[str, Any]] = {}
        for role, prefix in (("environment_repair", "repair"), ("clean_environment", "clean")):
            frozen = preview[role]
            copy = directory / f"{prefix}-{frozen['sha256']}"
            if not copy.exists():
                copied = adapter.orchestrator._secure_copy_file_once(Path(frozen["source_path"]), copy)
                if copied["sha256"] != frozen["sha256"]:
                    copy.unlink(missing_ok=True)
                    raise adapter.orchestrator.ConfigurationError(f"{role} 收据在复制途中被改动；重新预览后再批准。")
            elif copy.is_symlink() or adapter.orchestrator.file_sha256(copy) != frozen["sha256"]:
                raise adapter.orchestrator.ConfigurationError(f"{role} 收据副本与预览摘要不一致：{copy}")
            bindings[role] = {
                "path": copy.relative_to(campaign_dir).as_posix(),
                "sha256": frozen["sha256"],
                **({"observed_at_utc": frozen["observed_at_utc"]} if "observed_at_utc" in frozen else {}),
            }
        receipt = {
            "schema_version": adapter.orchestrator.ENVIRONMENT_ISOLATION_SCHEMA,
            "campaign_id": preview["campaign_id"],
            "index": preview["index"],
            "previous_isolation_sha256": preview["previous_isolation_sha256"],
            "covered": preview["covered"],
            "invalidated_attempts": preview["invalidated_attempts"],
            "environment_repair": bindings["environment_repair"],
            "clean_environment": bindings["clean_environment"],
            # 第三批 B3-14：污染发生时刻（无窗口为 null）；恢复范围按它区分污染前可复用与污染后重跑的作业。
            "contamination_started_at_utc": preview["contamination_started_at_utc"],
            "reason": preview["reason"],
            "review_sha256": preview["review_sha256"],
            "approved_by": approved_by,
            "approved_at_utc": adapter.orchestrator._utc_now(),
        }
        receipt["isolation_sha256"] = adapter.orchestrator._fingerprint(receipt)
        receipt_path = directory / f"isolation-{int(preview['index']):02d}.json"
        adapter.orchestrator._secure_write_json_once(receipt_path, receipt)
    return {
        "status": "isolated",
        "reused": False,
        "isolation_index": receipt["index"],
        "isolation_sha256": receipt["isolation_sha256"],
        "receipt_path": str(receipt_path),
        "invalidated_attempts": receipt["invalidated_attempts"],
        "next_command": next_steps,
    }


def _evaluation_epoch_command(arguments: argparse.Namespace) -> dict[str, Any]:
    adapter = _adapter()
    campaign_dir = arguments.campaign_dir
    manifest = adapter.orchestrator._require_formal_campaign(campaign_dir)
    candidate_id = getattr(arguments, "candidate_id", None)
    # 2026-09-18：seal／compare／accept 对 Candidate attempt 同样要求 evidence semantics
    # 变化后先追加 evaluation epoch（同一 current_evidence_semantics 判据），此前该命令
    # 只能定位 official attempt，候选侧无路可走。
    if candidate_id is not None:
        if not adapter.orchestrator.SAFE_ID_RE.fullmatch(str(candidate_id)):
            raise adapter.orchestrator.ConfigurationError("--candidate-id 格式非法。")
        attempt_root, _attempt = adapter.orchestrator._load_capture_attempt(
            campaign_dir, "candidate", str(candidate_id), arguments.attempt_id
        )
    else:
        attempt_root, _attempt = adapter.orchestrator._load_capture_attempt(campaign_dir, "official", None, arguments.attempt_id)
    try:
        # R1：策略经策略演进承接后，epoch 对照 Campaign 有效策略而不是 plan 冻结策略。
        effective_policy = (
            str(adapter.orchestrator._campaign_effective_tool_identity(campaign_dir, manifest)["identity"].get("policy_sha256"))
            if adapter.orchestrator._is_policy_v2_identity(manifest.get("tool_identity") or {})
            else None
        )
        path = codex_upgrade_wire_transition.append_epoch(
            attempt_root,
            manifest,
            current_identity=adapter.orchestrator._tool_identity(include_git=False),
            reason=str(arguments.reason),
            effective_policy_sha256=effective_policy,
        )
        chain = codex_upgrade_wire_transition.load_epochs(attempt_root)
    except codex_upgrade_wire_transition.WireTransitionError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    return {"status": "epoch_appended", "path": str(path), "epoch_index": chain[-1]["index"], "evidence_semantics_sha256": chain[-1]["to_evidence_semantics_sha256"]}


def _reconcile_supervisor_run_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """B0：父监督器 run 对账（自身零请求）。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    try:
        return reconciler.reconcile_supervisor_run(
            Path(arguments.run_dir),
            arguments.campaign_dir,
            control_root=getattr(arguments, "control_root", None),
        )
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error


def _rehearse_candidate_seal_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """在 OverlayFS 隔离副本上跑通 Candidate seal 零请求后处理链并写收据。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_seal_rehearsal as rehearsal

    campaign_dir = Path(arguments.campaign_dir).resolve(strict=True)
    status_command = [
        sys.executable,
        str(Path(adapter.orchestrator.__file__).resolve()),
        "status",
        "--campaign-dir",
        str(campaign_dir),
    ]
    skip = arguments.skip_action
    try:
        actions = rehearsal._load_action_plan(Path(arguments.action_plan))
        result = rehearsal.rehearse(
            campaign_dir=campaign_dir,
            candidate_id=str(arguments.candidate_id),
            attempt_id=str(arguments.attempt_id),
            actions=actions,
            data_root=Path(arguments.data_root).resolve(),
            alias_roots=[Path(item).resolve() for item in arguments.alias_root],
            upper_root=Path(arguments.upper_root).resolve(),
            status_command=status_command,
            skip_action_ids=(
                list(skip) if skip else list(rehearsal.DEFAULT_SKIP_ACTION_IDS)
            ),
        )
    except rehearsal.SealRehearsalError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
    return {
        key: value
        for key, value in result.items()
        if key not in {"lower_snapshot_before", "lower_snapshot_after"}
    }


def _reconcile_attempt_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """B0：attempt 对账（自身零请求）；可恢复时产出或批准恢复预览。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    try:
        authorize = getattr(arguments, "authorize_recovery_preview", None)
        if authorize is not None:
            if getattr(arguments, "approve_recovery_sha256", None) is not None:
                raise adapter.orchestrator.ConfigurationError("--authorize-recovery-preview 与 --approve-recovery-sha256 不能同时使用。")
            return reconciler.authorize_recovery_preview(
                arguments.campaign_dir,
                str(arguments.attempt_id),
                Path(authorize),
                recovery_revision=getattr(arguments, "recovery_revision", None),
            )
        return reconciler.reconcile_attempt(
            arguments.campaign_dir,
            str(arguments.attempt_id),
            control_root=getattr(arguments, "control_root", None),
            approve_recovery_sha256=getattr(arguments, "approve_recovery_sha256", None),
            recovery_revision=getattr(arguments, "recovery_revision", None),
        )
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error


def _account_sealed_official_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """已封存 official 阶段的模型请求入总账（零请求、幂等）。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    try:
        return reconciler.account_sealed_official(arguments.campaign_dir)
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error


def _account_sealed_candidate_command(arguments: argparse.Namespace) -> dict[str, Any]:
    """已封存 Candidate 阶段的模型请求入总账（零请求、幂等）。"""

    adapter = _adapter()
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    try:
        return reconciler.account_sealed_candidate(
            arguments.campaign_dir,
            str(arguments.candidate_id),
            recovery_revision=getattr(arguments, "attempt_recovery", None),
        )
    except reconciler.ReconcilerError as error:
        raise adapter.orchestrator.ConfigurationError(str(error)) from error
