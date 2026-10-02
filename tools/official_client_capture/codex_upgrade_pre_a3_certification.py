"""A2.5：pre-A3 路径认证（``pre-a3-path-certification/v1``）。

在隔离的 staging 树内、``fixture_only`` 项目总账下，以网络守卫把 A0～A3b 真实执行前用到的全部
路径同形跑一遍：deadline 中断、两个 reconciler 各两个分支、先入账后判定、恢复预览与批准、
batch 补齐、``unresolved`` 与 ``accounting_resolved``、wire transition intent 与 final、evaluation
epoch、策略 v2 的 seal 分支、``reuse-official-evidence`` 从 ``awaiting_receipts`` 导入并 seal、
两步式权限收口。每个场景复用受管测试模块里的离线夹具（它们与工具一起受管部署），全部临时目录
落在 staging 根下，项目总账一律 ``fixture_only``。

收据绑定当前 ARM64 部署收据（五摘要必须等于当前工具身份）、A2.6 策略激活认证，以及可选的
真实 v2 Formal 结构原子演练收据（29 个 Job 零网络合成动作）。整个过程 ``network_attempts=0``、
``live_request_count=0``；任何场景失败即认证失败关闭。

E3-02 起签发新形状 ``pre-a3-path-certification/v2``：场景作为入口门禁的可承接单元执行，认证从单元执行记录
组装（每个场景引用一条正式执行记录的摘要，并标明本次执行或承接），核验时逐条重验记录。v1 的全部字段原样保留，
消费端照读；v1 历史认证照旧只读重放。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as rehearsal
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_zero_request_smoke as smoke
from tools.official_client_capture.tests import project_ledger_fixture

SCHEMA_VERSION = "pre-a3-path-certification/v1"
# E3-02：从单元执行记录组装的新形状（v1 字段全部保留，另带逐场景的执行记录引用与运行清单引用）。
SCHEMA_VERSION_V2 = "pre-a3-path-certification/v2"
CERTIFICATION_SCHEMAS = (SCHEMA_VERSION, SCHEMA_VERSION_V2)
# 修好接着跑第 19 项：跨部署复用既有 pre-A3 认证时登记的复用收据（write-once、按绑定内容幂等）。
REUSE_SCHEMA_VERSION = "pre-a3-reuse-receipt/v1"
REUSE_RECEIPT_PREFIX = "pre-a3-reuse-"
FIXTURE_ONLY_ENV = project_ledger_fixture.FIXTURE_ONLY_ENV
# R13 分阶段登记：阶段 1 只要求 validation_only 连续链；R18 追加后段父失败注入链（VC-2／VC-4／VC-5）、
# 0.156.1 录制证据零请求回放的 VC-1 取证链与 VC-1 连续恢复链。
REAL_CHAIN_IDS = (
    "vc-chain.full-validation-only",
    "vc-chain.late-stage-faults",
    "vc-chain.vc1-capture",
    "vc-chain.vc1-recovery-chain",
)
# (场景名, 说明, 测试模块, 测试类, 测试方法)
SCENARIOS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "runtime-egress.kernel-faults",
        "隔离双端内核：指定路径、分别阻断、路由与 NAT 漂移、新旧连接及重建首包",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_fault_fixtures",
        "RuntimeEgressKernelTests",
        "test_r15_isolated_kernel_failure_and_recovery_chain",
    ),
    (
        "runtime-egress.guard-recovery",
        "两端真实守护逻辑驱动内核租期，共享修复后重新逐容器验证",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_fault_fixtures",
        "RuntimeEgressKernelTests",
        "test_r15_guard_drives_real_kernel_leases_and_requires_fresh_recovery",
    ),
    (
        "runtime-egress.capture-pause",
        "运行中出口故障停止采集并在原清理预算内结束",
        "tools.official_client_capture.tests.test_codex_runtime_egress",
        "EgressSupervisorTests",
        "test_active_capture_cleans_up_without_waiting_for_original_deadline",
    ),
    (
        "runtime-egress.immutable-pause",
        "修复网络不能自行恢复原 run，暂停事实不可覆盖",
        "tools.official_client_capture.tests.test_codex_runtime_egress",
        "EgressSupervisorTests",
        "test_pause_is_immutable_and_repair_does_not_resume_same_run",
    ),
    (
        "runtime-egress.history-equivalence",
        "同一工具包读取多个出口策略，旧 v7 收据只读回放并保持等价",
        "tools.official_client_capture.tests.test_codex_upgrade_arm64_environment_receipt",
        "Arm64EnvironmentReceiptTests",
        "test_r15_policy_switch_preserves_equivalence_and_history_without_runtime_reads",
    ),
    (
        "runtime-egress.docker-supervisor",
        "真实父监督器与 Docker 重启重建等待，故障清理后原 run 不可恢复",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_fault_fixtures",
        "EgressProcessTests",
        "test_real_parent_waits_for_docker_restart_rebuild_and_never_resumes_after_fault",
    ),
    (
        "runtime-egress.job-window",
        "按暂停窗口逐 Job 对账，保留窗口外结果并拒绝追认窗口内请求",
        "tools.official_client_capture.tests.test_codex_runtime_egress",
        "EgressSupervisorTests",
        "test_reconciliation_keeps_only_completed_jobs_before_uncertain_window",
    ),
    (
        "runtime-egress.reconcile-resume",
        "出口暂停后沿真实 checkpoint 与两本账对账，批准后仅派发窗口内任务",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_fault_fixtures",
        "RuntimeEgressRecoveryTests",
        "test_pause_reconciliation_approval_preserves_only_trusted_job",
    ),
    (
        "vc-chain.full-validation-only",
        "VC-0 复用导入、VC-2 三批、候选构建、真实评估与 VC-6 只读交付连续链",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_full_chain",
        "FullValidationOnlyChainTests",
        "test_full_validation_only_chain",
    ),
    (
        "vc-chain.late-stage-faults",
        "连续链在 VC-2 分类、VC-4 构建登记、VC-5 验收各注入一次父失败，逐次对账恢复直至 VC-6 交付",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_late_stage_faults",
        "LateStageFaultChainTests",
        "test_late_stage_parent_failures_recover_to_delivery",
    ),
    (
        "vc-chain.vc1-capture",
        "VC-0 收口真实建 Formal Campaign，0.156.1 录制官方证据零请求回放首批，断言包、seal 门禁（含延后项）、入账与 VC-2 分类草案",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_vc1_recorded_chain",
        "VC1RecordedCaptureChainTests",
        "test_vc1_capture_from_recorded_evidence",
    ),
    (
        "vc-chain.vc1-recovery-chain",
        "录制回放连续恢复：首批超时、预览失败重派、补跑失败、控制面与证据语义修复部署、epoch、封存与分类",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_vc1_recorded_chain",
        "VC1RecordedRecoveryChainTests",
        "test_vc1_recovery_chain_from_recorded_evidence",
    ),
    (
        "reconcile-attempt.recoverable-preview-approve-resume",
        "孤儿 attempt 先入账后判定为可恢复，零请求预览、批准与 resume 门禁",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_orphan_is_recoverable_and_gates_resume",
    ),
    (
        "reconcile-attempt.root-cause-limit-stop",
        "同根因第二次失败：stage_abandoned、stop_the_line 与 campaign_terminal",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_same_root_cause_limit_stops_the_line",
    ),
    (
        "accounting.unresolved-pauses-then-resolves",
        "请求数无法确定：总账 blocked 但只暂停；accounting-resolve 按批准上界带证据补账后同一对象重新对账可续跑",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_unresolved_accounting_pauses_and_accounting_resolve_continues",
    ),
    (
        "deadline-interruption.attempt-failed-before-pause",
        "deadline 到期：metadata-only attempt_failed 入账后只暂停，保留原阶段",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_deadline_expired_records_failure_and_pauses",
    ),
    (
        "deadline-extension.sigkill-resume",
        "真实到期、批准两账间 SIGKILL、幂等补齐后原 Campaign 实际派发",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_deadline_extension",
        "DeadlineExtensionChainTests",
        "test_cli_sigkill_extension_resumes_original_campaign",
    ),
    (
        "deadline-extension.runtime-stage-boundary",
        "阶段截止早于父 Campaign 截止时，真实动作仍按阶段边界停止",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_deadline_extension",
        "DeadlineExtensionChainTests",
        "test_running_command_honors_stage_before_campaign_deadline",
    ),
    (
        "deadline-extension.watchdog-stage-boundary",
        "独立 watchdog 持续采用三层最早截止，父时间锚保持不变",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_deadline_extension",
        "DeadlineExtensionChainTests",
        "test_watchdog_honors_stage_before_campaign_deadline",
    ),
    (
        "accounting.precise-keys-enter-once",
        "有权威来源的证据按身份键精确入账且只计一次",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_precise_requests_enter_ledger_once",
    ),
    (
        "accounting.estimated-sources-once",
        "估计按来源去重",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_estimated_sources_are_counted_once_in_project_ledger",
    ),
    (
        "batch.uncommitted-not-pushed",
        "写了 entry 未 COMMIT 的 batch 不推送并阻断新 batch",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_uncommitted_batch_is_not_pushed_and_blocks_new_batches",
    ),
    (
        "reconcile-supervisor-run.recoverable-and-stop",
        "父监督器 run 可恢复（receipt_passed，phase 保持 active）与同根因停线",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_supervisor_run_recoverable_then_limit_stops",
    ),
    (
        "wire-transition.intent-final",
        "wire producer 两阶段过渡：intent 冻结闭集、final 承接",
        "tools.official_client_capture.tests.test_codex_upgrade_wire_transition",
        "WireTransitionTests",
        "test_intent_final_chain_moves_effective_identity",
    ),
    (
        "wire-transition.all-jobs-affected-refused",
        "受影响等于全部或编排器闭包变化时拒签 intent",
        "tools.official_client_capture.tests.test_codex_upgrade_wire_transition",
        "WireTransitionTests",
        "test_all_jobs_affected_or_orchestrator_closure_change_refuses_intent",
    ),
    (
        "evaluation-epoch.chain",
        "evaluation epoch 单调链追加与断链检测",
        "tools.official_client_capture.tests.test_codex_upgrade_wire_transition",
        "WireTransitionTests",
        "test_evaluation_epoch_chain_appends_and_detects_breaks",
    ),
    (
        "policy-v2.seal-branches",
        "策略 v2 身份判定：control 只留痕、evidence 变化需 epoch、wire 变化需 intent/final、策略变化拒绝",
        "tools.official_client_capture.tests.test_codex_upgrade_policy_v2_identity",
        "PolicyV2IdentityTests",
        "test_wire_change_needs_intent_then_final",
    ),
    (
        "policy-v2.evidence-epoch-required",
        "evidence semantics 变化时评估类操作要求当前 attempt 已追加 epoch",
        "tools.official_client_capture.tests.test_codex_upgrade_policy_v2_identity",
        "PolicyV2IdentityTests",
        "test_evidence_semantics_change_requires_epoch_for_evaluation_operations",
    ),
    (
        "reuse-official-evidence.awaiting-receipts-import-and-seal",
        "从 awaiting_receipts 前序导入官方证据并由新 Campaign seal 完成 VC-1",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_0154_reuse_official_evidence_imports_awaiting_receipts_attempt_and_seals",
    ),
    (
        "reuse-official-evidence.broken-bindings-rejected",
        "任一收据缺失、未通过、未绑定或证据漂移即拒绝导入且不留半成品",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_0154_official_attempt_import_rejects_broken_bindings",
    ),
    (
        "harden-evidence-permissions.two-step",
        "两步式权限收口：preview 只读、apply 按批准摘要 metadata-only、replay",
        "tools.official_client_capture.tests.test_codex_upgrade_harden_evidence_permissions",
        "HardenEvidencePermissionsTests",
        "test_preview_then_apply_hardens_metadata_only",
    ),
    (
        "harden-evidence-permissions.content-drift-rejected",
        "预览后内容漂移或符号链接一律拒绝",
        "tools.official_client_capture.tests.test_codex_upgrade_harden_evidence_permissions",
        "HardenEvidencePermissionsTests",
        "test_apply_refuses_content_drift_and_symlinks",
    ),
    # ---- VC-2～VC-6 派发链（2026-09-16 增补）：零请求合成动作走真实原子入口 ----
    (
        "vc-chain.batches-through-vc6",
        "只读导入 Campaign 从 VC-2 到 VC-6 逐批经 compile-and-run-vc-batch 派发：no-op 首批引导、总账 admission、账本阶段事件、checkpoint 链",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_vc_chain_batches_advance_ledger_and_checkpoints_through_vc6",
    ),
    (
        "vc-chain.stopped-ledger-rejected-before-write",
        "账本已停线时原子入口在编译前拒绝，不写 batch／manifest／run／停线收据",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_vc_chain_rejects_stopped_ledger_before_any_artifact_is_written",
    ),
    (
        "vc-chain.failed-batch-abandons-stage",
        "动作失败：父 run 写 stage_abandoned＋stage_review_required，对账前后续批次被拒",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_vc_chain_failed_batch_abandons_stage_and_blocks_next_batch",
    ),
    (
        "stage-recovery.committed-classify",
        "classify COMMIT 后失败，原 Campaign 对账后 N+1 重派并复用草案，新增请求为零",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_stage_recovery",
        "StageRecoveryChainTests",
        "test_classify_commit_failure_reconciles_and_redispatches",
    ),
    (
        "segment-recovery.completed-job-reuse",
        "三段连续恢复只执行 2、1、0 个 Job，复用 0、1、2 个；真实 SIGKILL 后批准续作及增量封存",
        "tools.official_client_capture.tests.real_chains.test_codex_upgrade_segment_reuse",
        "SegmentReuseChainTests",
        "test_three_segments_reuse_completed_jobs_and_seal",
    ),
    (
        "stage-recovery.interrupted-closeout",
        "stage_abandoned 后 SIGKILL，重入只补一条 stage_review_required",
        "tools.official_client_capture.tests.test_codex_upgrade_stage_recovery",
        "StageRecoveryTests",
        "test_sigkill_between_abandon_and_review_appends_once",
    ),
    (
        "vc-chain.admission-before-any-write",
        "总账拒绝时不进入编译、锁与账本",
        "tools.official_client_capture.tests.test_codex_upgrade_atomic_dispatch",
        "AtomicDispatchTests",
        "test_governance_admission_rejection_happens_before_any_write",
    ),
    (
        "vc-chain.ledger-events-derivation",
        "阶段事件按账本阶段与 checkpoint 推导：同阶段不写、倒退与前序未封存拒绝",
        "tools.official_client_capture.tests.test_codex_upgrade_atomic_dispatch",
        "AtomicDispatchTests",
        "test_batch_events_are_derived_from_ledger_phase_and_checkpoints",
    ),
    (
        "vc-chain.ledger-budget-bound-to-project",
        "Campaign 账本绑定项目总账后预算由绝对截止裁剪，人工核对不再按 75 分钟硬切",
        "tools.official_client_capture.tests.test_codex_upgrade_timing_ledger",
        "TimingLedgerTests",
        "test_project_ledger_binding_lets_campaign_plan_set_budgets",
    ),
    (
        "vc-chain.draft-and-approval-preview-are-legal-stops",
        "classify 草案 draft 与批准预览 approval_required 在父批次内是合法停靠点，失败状态仍非零",
        "tools.official_client_capture.tests.test_codex_upgrade_0154_vc_contract",
        "CodexUpgrade0154VCContractTests",
        "test_intermediate_status_is_success_only_inside_campaign_run",
    ),
    (
        "vc-chain.candidate-seal-and-canonical-advance-consumers",
        "候选 seal 与 canonical-advance 各步骤同为总账消费者，未注册即拒绝",
        "tools.official_client_capture.tests.test_codex_upgrade_project_ledger_integration",
        "ProjectLedgerIntegrationTests",
        "test_consumers_reject_unregistered_0154_formal_and_pass_legacy",
    ),
)


class CertificationError(RuntimeError):
    """路径认证的前置绑定不满足或某个场景失败。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    return codex_upgrade._fingerprint(value)


def _load_test_case(module_name: str, class_name: str, method: str) -> unittest.TestCase:
    module = __import__(module_name, fromlist=[class_name])
    test_class = getattr(module, class_name)
    return test_class(method)


def run_scenario(scenario: tuple[str, str, str, str, str]) -> dict[str, Any]:
    """在当前进程内运行一个受管测试场景，返回结构化结果（不抛异常）。"""

    name, description, module_name, class_name, method = scenario
    started = time.monotonic()
    result = unittest.TestResult()
    try:
        case = _load_test_case(module_name, class_name, method)
        case.run(result)
    except Exception as error:  # noqa: BLE001 - 认证必须如实记录加载失败
        return {
            "name": name,
            "description": description,
            "test": f"{module_name}:{class_name}.{method}",
            "status": "failed",
            "error": f"{type(error).__name__}: {error}",
            "seconds": round(time.monotonic() - started, 3),
        }
    problems = [text for _case, text in [*result.errors, *result.failures]]
    status = "passed" if result.wasSuccessful() and result.testsRun == 1 and not result.skipped else "failed"
    record: dict[str, Any] = {
        "name": name,
        "description": description,
        "test": f"{module_name}:{class_name}.{method}",
        "status": status,
        "seconds": round(time.monotonic() - started, 3),
    }
    if problems:
        record["error"] = problems[0][-2000:]
    if result.skipped:
        record["error"] = "场景被跳过"
        if name in REAL_CHAIN_IDS:
            record["status"] = "uncertified"
    if hasattr(case, "real_chain_metrics"):
        record["metrics"] = case.real_chain_metrics
    return record


def real_chain_registration() -> list[dict[str, str]]:
    """冻结本发布包登记的链集合；后续增加链不得改变历史包的回放要求。"""

    return [{"id": name, "test": f"{scenario[2]}:{scenario[3]}.{scenario[4]}"}
            for name in REAL_CHAIN_IDS
            for scenario in SCENARIOS if scenario[0] == name]


def real_chain_coverage(payload: Mapping[str, Any], *, historical: bool = False) -> list[dict[str, Any]]:
    """新签发要求当前登记；历史回放只核验当时绑定的集合，缺失与 skip 均拒绝。"""

    rows = payload.get("scenarios")
    if not isinstance(rows, list):
        raise CertificationError("路径认证缺少逐场景结果")
    registration = payload.get("real_chain_registration")
    if (not isinstance(registration, list) or not registration
            or any(not isinstance(item, Mapping) or set(item) != {"id", "test"}
                   or not isinstance(item["id"], str) or not item["id"].startswith("vc-chain.")
                   or not isinstance(item["test"], str) or not item["test"] for item in registration)
            or len({item["id"] for item in registration}) != len(registration)):
        raise CertificationError("真实链登记集合缺失或非法")
    if not historical and registration != real_chain_registration():
        raise CertificationError("真实链登记集合与当前发布包不一致")
    coverage: list[dict[str, Any]] = []
    for entry in registration:
        name = entry["id"]
        matches = [row for row in rows if isinstance(row, Mapping) and row.get("name") == name]
        if len(matches) != 1 or matches[0].get("status") != "passed":
            raise CertificationError(f"已登记的真实链未认证：{name}")
        expected_test = entry["test"]
        if matches[0].get("test") != expected_test:
            raise CertificationError(f"真实链测试入口与登记不一致：{name}")
        coverage.append({"id": name, "test": expected_test, "status": "passed"})
    return coverage


def _accounting_resolved_scenario(staging_root: Path) -> dict[str, Any]:
    """unresolved → 暂停 → 新证据可核清 → accounting-resolve 精确补账解除 blocked → 同一对象重新对账可恢复。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler
    from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests

    started = time.monotonic()
    record: dict[str, Any] = {
        "name": "accounting.resolved-unblocks",
        "description": "账务无法核清只暂停；补回证据后 accounting-resolve 按当前证据精确补账，解除 blocked 后同一对象续跑",
        "test": "inline:codex_upgrade_pre_a3_certification._accounting_resolved_scenario",
    }
    try:
        case = upgrade_tests.CodexUpgradeTest(
            "test_b0_reconcile_attempt_unresolved_accounting_pauses_and_accounting_resolve_continues"
        )
        case.setUp()
        root = Path(tempfile.mkdtemp(prefix="accounting-resolved-", dir=staging_root)).resolve()
        fixture = case._b0_fixture(root)
        campaign_dir = fixture["campaign_dir"]
        evidence_root = campaign_dir / "official-evidence"
        evidence_root.mkdir(mode=0o700)
        (evidence_root / "surface.json").write_text('{"records": []}\n', encoding="utf-8")
        (evidence_root / "surface.json").chmod(0o600)
        attempt_id = case._b0_orphan_attempt(
            fixture,
            evidence_roots=[evidence_root],
        )
        result = reconciler.reconcile_attempt(campaign_dir, attempt_id)
        if (
            result["status"] != "paused"
            or result["decision"].get("pause_kinds") != ["accounting"]
            or result["decision"]["terminal_reason"] is not None
        ):
            raise CertificationError(f"未决账务没有只暂停：{result['status']} {result['decision']}")
        operation_id = f"reconcile-attempt:{attempt_id}"
        head = project_ledger.replay_head(fixture["ledger"])
        if not head["blocked"] or operation_id not in head["unresolved_operation_ids"]:
            raise CertificationError("总账未登记未决 operation")
        if str(fixture["manifest"]["campaign_id"]) in head["terminal_campaigns"]:
            raise CertificationError("账务无法核清仍写了终态")
        # 夹具内把无法识别的证据根移除，模拟"补回证据后按当前证据已能核清"（夹具专用操作）。
        (evidence_root / "surface.json").unlink()
        evidence_root.rmdir()
        arguments = argparse.Namespace(
            campaign_dir=campaign_dir, operation_id=operation_id, estimated_count=None, evidence=None,
            reason="补回证据后按当前证据精确补账", approve_sha256=None, approved_by=None,
        )
        preview = codex_upgrade._accounting_resolve_command(arguments)
        if preview["status"] != "approval_required" or preview["resolution_mode"] != "precise_from_current_evidence":
            raise CertificationError(f"补账预览口径错误：{preview.get('resolution_mode')}")
        arguments.approve_sha256 = preview["review_sha256"]
        arguments.approved_by = "pre-A3 认证"
        resolved = codex_upgrade._accounting_resolve_command(arguments)
        if resolved["status"] != "resolved" or resolved["blocked"]:
            raise CertificationError("补账后总账仍 blocked")
        again = reconciler.reconcile_attempt(campaign_dir, attempt_id)
        if again["status"] != "recoverable":
            raise CertificationError(f"补账后同一对象重新对账不可恢复：{again['status']}")
        case.doCleanups()
        record["status"] = "passed"
    except Exception as error:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
    record["seconds"] = round(time.monotonic() - started, 3)
    return record


def _bind_rehearsal_receipt(path: Path | None, identity: Mapping[str, Any]) -> dict[str, Any] | None:
    """可选：绑定真实 v2 Formal 结构的原子演练收据，其工具身份必须来自当前树。"""

    if path is None:
        return None
    payload = rehearsal._load_json(Path(path).resolve(strict=True), "原子演练收据")
    if payload.get("schema_version") != rehearsal.ATOMIC_RECEIPT_SCHEMA:
        raise CertificationError("原子演练收据 schema 非法")
    if payload.get("tool_identity") != rehearsal._atomic_tool_identity():
        raise CertificationError("原子演练收据的工具身份不是当前工具树；先用当前树重做 campaign-run 演练")
    return {
        "path": str(Path(path).resolve(strict=True)),
        "sha256": codex_upgrade.file_sha256(Path(path)),
        "schema_version": payload.get("schema_version"),
        "campaign_id": payload.get("campaign_id"),
        "official_job_count": payload.get("official_job_count"),
        "tool_files_sha256": identity["tool_files_sha256"],
    }


def _fresh_staging_root(staging_root: Path) -> Path:
    """认证根每次用新目录（E2-03）：指定目录已存在且非空时改用带 UTC 时间后缀的新目录，同一秒再冲突加序号；
    同一 STAMP 重跑不必先手工归档上一次的认证根。"""

    root = Path(staging_root).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in root.parts:
        raise CertificationError("认证根必须位于 staging 目录树内")
    if not root.exists() or not any(root.iterdir()):
        return root
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    candidate = root.with_name(f"{root.name}-{stamp}")
    index = 1
    while candidate.exists():
        index += 1
        candidate = root.with_name(f"{root.name}-{stamp}-{index}")
    return candidate


def _certification_bindings(
    deployment_receipt: Path, policy_activation: Path, campaign_run_rehearsal_receipt: Path | None,
) -> dict[str, Any]:
    """签发前的绑定：当前工具身份、部署收据与激活认证（五摘要都等于当前身份），以及可选的原子演练收据。"""

    identity = policy_certification.current_identity()
    deployment_path = Path(deployment_receipt).resolve(strict=True)
    deployment = policy_certification.load_deployment_receipt(deployment_path, expected_identity=identity)
    activation_path = Path(policy_activation).resolve(strict=True)
    activation = policy_certification.verify_activation_certification(activation_path, expected_identity=identity)
    return {
        "identity": identity, "deployment_path": deployment_path, "deployment": deployment,
        "activation_path": activation_path, "activation": activation,
        "rehearsal": _bind_rehearsal_receipt(campaign_run_rehearsal_receipt, identity),
    }


def _build_receipt(
    staging_root: Path, bindings: Mapping[str, Any], report: list[dict[str, Any]], failed: list[str], network_attempts: int,
    observed_at_utc: str | None, *, records: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """串行（run）、按场景并行（issue）与从单元执行记录签发（E3-02）共用的收据形状。

    ``records`` 给出时签 v2：另带运行清单引用（``unit_manifest``）与记录库路径（``record_store``），逐场景的执行记录
    引用（``unit_record``）已由调用方放进 ``report``。v1 的字段一个不少，消费端照读。"""

    identity = bindings["identity"]
    receipt = {
        "schema_version": SCHEMA_VERSION_V2 if records is not None else SCHEMA_VERSION,
        "status": "passed" if not failed and not network_attempts else "failed",
        "certified_at_utc": observed_at_utc or _utc_now(),
        "staging_root": str(staging_root),
        "fixture_only": True,
        "identity": {name: identity[name] for name in policy_certification.IDENTITY_FIELDS},
        "policy_version": identity["policy_version"],
        "deployment_receipt": {
            "path": str(bindings["deployment_path"]),
            "sha256": codex_upgrade.file_sha256(bindings["deployment_path"]),
            "created_at_utc": bindings["deployment"].get("created_at_utc"),
        },
        "policy_activation": {
            "path": str(bindings["activation_path"]),
            "sha256": codex_upgrade.file_sha256(bindings["activation_path"]),
            "policy_sha256": bindings["activation"].get("policy_sha256"),
        },
        "campaign_run_rehearsal_receipt": bindings["rehearsal"],
        "real_chain_registration": real_chain_registration(),
        "scenarios": report,
        "scenario_count": len(report),
        "failed_scenarios": failed,
        "network_attempts": network_attempts,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    if records is not None:
        receipt["unit_manifest"] = dict(records["unit_manifest"])
        receipt["record_store"] = str(records["record_store"])
    receipt["receipt_sha256"] = _fingerprint(receipt)
    return receipt


def run_certification(
    staging_root: Path,
    *,
    deployment_receipt: Path,
    policy_activation: Path,
    campaign_run_rehearsal_receipt: Path | None = None,
    scenarios: tuple[tuple[str, str, str, str, str], ...] = SCENARIOS,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """串行认证：一个进程里依次跑全部场景（与按场景并行的 issue 同一收据形状，用于对照与回退）。"""

    staging_root = _fresh_staging_root(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root.chmod(0o700)
    bindings = _certification_bindings(deployment_receipt, policy_activation, campaign_run_rehearsal_receipt)
    temp_root = staging_root / "tmp"
    temp_root.mkdir(mode=0o700)
    previous_tempdir = tempfile.tempdir
    previous_env = os.environ.get(FIXTURE_ONLY_ENV)
    report: list[dict[str, Any]] = []
    tempfile.tempdir = str(temp_root)
    os.environ[FIXTURE_ONLY_ENV] = "1"
    try:
        with smoke._NetworkGuard() as guard:
            for scenario in scenarios:
                report.append(run_scenario(scenario))
            report.append(_accounting_resolved_scenario(temp_root))
        attempts = list(guard.attempts)
    finally:
        tempfile.tempdir = previous_tempdir
        if previous_env is None:
            os.environ.pop(FIXTURE_ONLY_ENV, None)
        else:
            os.environ[FIXTURE_ONLY_ENV] = previous_env
    failed = [item["name"] for item in report if item["status"] != "passed"]
    return _build_receipt(staging_root, bindings, report, failed, len(attempts), observed_at_utc)


# ---------------------------------------------------------------------------
# E2-03：按场景并行——单场景入口、执行器清单、汇总签发
# ---------------------------------------------------------------------------
# 场景作为单元交给统一调度执行器（tools/ci/unit_executor.py run-commands）：每个场景一个子进程，各自装网络守卫、
# 各自的临时目录，结果写成一份单场景结果；全部跑完后 issue 核对场景全集与网络计数，按串行认证的同一形状签发。
# 认证模块自己不带调度器，也不导入执行器（它不是受管代码），清单与汇总的 schema 字面量与执行器保持一致。
SCENARIO_RESULT_SCHEMA = "pre-a3-scenario-result/v1"
ACCOUNTING_SCENARIO_NAME = "accounting.resolved-unblocks"
SCENARIO_UNIT_PREFIX = "pre-a3:"
EXECUTOR_COMMANDS_SCHEMA = "unit-executor-commands/v1"
EXECUTOR_SUMMARY_SCHEMA = "unit-executor-commands-summary/v1"
RESULTS_DIR_NAME = "results"
SCENARIO_ROOTS_DIR_NAME = "scenarios"
# 执行器从长到短派发用的预计秒数：ARM64 实测（E2-02 部署后，七条重链 3 路并行）；没列的按 30 秒。
SCENARIO_SECONDS: dict[str, float] = {
    "vc-chain.vc1-recovery-chain": 456.0,
    "vc-chain.vc1-capture": 211.0,
    "deadline-extension.sigkill-resume": 183.0,
    "segment-recovery.completed-job-reuse": 168.0,
    "vc-chain.late-stage-faults": 63.0,
    "vc-chain.full-validation-only": 38.0,
    "stage-recovery.committed-classify": 19.0,
}
# 单元超时只作安全网（超时即判该场景崩溃、不签发）：真实链 40 分钟，其余 15 分钟。
REAL_CHAIN_TIMEOUT_SECONDS = 2400
SCENARIO_TIMEOUT_SECONDS = 900


def scenario_names() -> list[str]:
    """认证场景全集：登记的受管测试场景加上进程内的补账场景，顺序即认证里的顺序。"""

    return [scenario[0] for scenario in SCENARIOS] + [ACCOUNTING_SCENARIO_NAME]


def _scenario_file_name(name: str) -> str:
    return name.replace("/", "_")


def run_single_scenario(name: str, staging_root: Path) -> dict[str, Any]:
    """在当前进程（执行器派发的单元子进程）跑一个场景：自己的临时目录、只用夹具总账、装网络守卫，返回单场景结果
    （场景记录与本进程的网络连接尝试次数）。进程级设置只在这个子进程里改，跑完进程就退出。"""

    if name not in scenario_names():
        raise CertificationError(f"未登记的认证场景：{name}")
    root = Path(staging_root).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in root.parts:
        raise CertificationError("场景根必须位于 staging 目录树内")
    temp_root = root / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tempfile.tempdir = str(temp_root)
    os.environ[FIXTURE_ONLY_ENV] = "1"
    with smoke._NetworkGuard() as guard:
        if name == ACCOUNTING_SCENARIO_NAME:
            record = _accounting_resolved_scenario(temp_root)
        else:
            record = run_scenario(next(scenario for scenario in SCENARIOS if scenario[0] == name))
    return {
        "schema_version": SCENARIO_RESULT_SCHEMA,
        "name": name,
        "scenario": record,
        "network_attempts": len(guard.attempts),
        "finished_at_utc": _utc_now(),
    }


def plan_scenario_units(staging_root: Path, *, python: str | None = None, cwd: Path | None = None) -> dict[str, Any]:
    """生成执行器命令单元清单：每个场景一条 ``run-scenario`` 命令，结果写到 ``<认证根>/results/<场景>.json``，
    临时目录在 ``<认证根>/scenarios/<场景>/``。认证根每次用新目录，实际路径写在清单的 ``staging_root``。"""

    root = _fresh_staging_root(staging_root)
    for directory in (root, root / RESULTS_DIR_NAME, root / SCENARIO_ROOTS_DIR_NAME):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    real_chains = {scenario[0] for scenario in SCENARIOS if ".real_chains." in scenario[2]}
    workdir = str(Path(cwd or os.getcwd()).resolve())
    units = []
    for name in scenario_names():
        file_name = _scenario_file_name(name)
        units.append({
            "unit_id": f"{SCENARIO_UNIT_PREFIX}{name}",
            "argv": [
                python or sys.executable, "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification", "run-scenario",
                "--name", name, "--staging-root", str(root / SCENARIO_ROOTS_DIR_NAME / file_name),
                "--result", str(root / RESULTS_DIR_NAME / f"{file_name}.json"),
            ],
            "cwd": workdir,
            "cores": 1,
            "memory_mb": 1024,
            "timeout_seconds": REAL_CHAIN_TIMEOUT_SECONDS if name in real_chains else SCENARIO_TIMEOUT_SECONDS,
            "weight": SCENARIO_SECONDS.get(name, 30.0),
        })
    return {"schema_version": EXECUTOR_COMMANDS_SCHEMA, "staging_root": str(root), "units": units}


def _placeholder_record(name: str, problems: list[str], seconds: Any) -> dict[str, Any]:
    scenario = next((item for item in SCENARIOS if item[0] == name), None)
    return {
        "name": name,
        "description": scenario[1] if scenario else "账务无法核清只暂停；补回证据后 accounting-resolve 按当前证据精确补账，解除 blocked 后同一对象续跑",
        "test": f"{scenario[2]}:{scenario[3]}.{scenario[4]}" if scenario else "inline:codex_upgrade_pre_a3_certification._accounting_resolved_scenario",
        "status": "failed",
        "error": "；".join(problems),
        "seconds": seconds,
    }


def issue_certification(
    staging_root: Path,
    *,
    executor_summary: Path,
    deployment_receipt: Path,
    policy_activation: Path,
    campaign_run_rehearsal_receipt: Path | None = None,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """汇总签发：核对场景全集与各子进程上报的网络计数，按串行认证的同一形状出具认证（消费端照旧）。

    下面任何一种都记为该场景失败、认证不通过（调用方只写旁路文件，正式路径不写）：执行器没有正式执行该场景单元
    （缺报）、单元被信号终止、超时或退出码不是 0／1（崩溃）；没有结果文件或结果非法、场景名不符、结果里没有网络
    计数；同一场景出现两份结果（重复上报）。结果目录里全集之外的结果、执行器汇总里全集之外的单元也让认证不通过
    （记在 ``failed_scenarios``）。
    子进程退出码 0 必须对应「场景通过且本进程零网络」，1 对应其余情况；对不上同样判失败。诊断执行的结果不参与签发。
    """

    root = Path(staging_root).resolve(strict=True)
    if project_ledger.STAGING_DIR_NAME not in root.parts:
        raise CertificationError("认证根必须位于 staging 目录树内")
    bindings = _certification_bindings(deployment_receipt, policy_activation, campaign_run_rehearsal_receipt)
    summary = policy_certification._read_json(Path(executor_summary), "执行器汇总")
    if summary.get("schema_version") != EXECUTOR_SUMMARY_SCHEMA or not isinstance(summary.get("units"), list):
        raise CertificationError("执行器汇总 schema 非法")
    rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in summary["units"]:
        if not isinstance(row, Mapping) or row.get("kind") != "formal":
            raise CertificationError("执行器汇总的单元行非法（units 只应列正式执行）")
        rows.setdefault(str(row.get("unit_id")), []).append(row)
    names = scenario_names()
    # 汇总里全集之外的单元（例如同一场景换个编号又派发了一次）同样不签发：它可能抢先写了某个场景的结果。
    stray_units = sorted(set(rows) - {f"{SCENARIO_UNIT_PREFIX}{name}" for name in names})
    results: dict[str, list[dict[str, Any]]] = {}
    stray: list[str] = []
    for path in sorted((root / RESULTS_DIR_NAME).glob("*.json")):
        if path.name.endswith(".diagnostic.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        name = payload.get("name") if isinstance(payload, dict) and payload.get("schema_version") == SCENARIO_RESULT_SCHEMA else None
        if name not in names or path.name != f"{_scenario_file_name(str(name))}.json":
            stray.append(path.name)
            continue
        results.setdefault(str(name), []).append(payload)
    report: list[dict[str, Any]] = []
    attempts = 0
    for name in names:
        problems: list[str] = []
        unit_rows = rows.get(f"{SCENARIO_UNIT_PREFIX}{name}", [])
        row = unit_rows[0] if len(unit_rows) == 1 else None
        if not unit_rows:
            problems.append("执行器没有正式执行该场景（缺报）")
        elif len(unit_rows) > 1:
            problems.append("执行器汇总里该场景出现多次")
        elif row.get("timed_out"):
            problems.append(f"场景子进程超时（执行器以信号 {row.get('signal')} 终止）")
        elif row.get("signal"):
            problems.append(f"场景子进程被信号 {row['signal']} 终止")
        elif row.get("exit_code") not in (0, 1):
            problems.append(f"场景子进程异常退出（退出码 {row.get('exit_code')}）")
        found = results.get(name, [])
        if len(found) > 1:
            problems.append("同一场景出现多份结果（重复上报）")
        elif not found:
            problems.append("没有场景结果（缺报）")
        result = found[0] if len(found) == 1 else None
        if result is not None:
            count = result.get("network_attempts")
            record = result.get("scenario")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                problems.append("场景结果里没有网络计数")
            elif not isinstance(record, dict) or record.get("name") != name:
                problems.append("场景结果里的记录与场景名不符")
            else:
                attempts += count
                clean = record.get("status") == "passed" and count == 0
                if row is not None and row.get("exit_code") in (0, 1) and (row["exit_code"] == 0) != clean:
                    problems.append(f"子进程退出码 {row['exit_code']} 与场景结论不符")
        if problems or result is None:
            report.append(_placeholder_record(name, problems, row.get("seconds") if row else None))
        else:
            report.append(dict(result["scenario"]))
    failed = [item["name"] for item in report if item["status"] != "passed"]
    failed += [f"全集之外的结果：{name}" for name in stray]
    failed += [f"全集之外的执行单元：{unit_id}" for unit_id in stray_units]
    return _build_receipt(root, bindings, report, failed, attempts, observed_at_utc)


# ---------------------------------------------------------------------------
# E3-02：场景单元可承接——稳定命令、结果行、从单元执行记录签发
# ---------------------------------------------------------------------------
# 场景单元的命令只带稳定内容（场景名与场景父目录），单元规格不随运行变，入口门禁才能承接已经通过的场景；场景的临时根
# 每次在父目录下新建，通过后删除、失败保留供排查。结论不再写结果文件，而是单元标准输出里恰好一行
# ``PRE_A3_SCENARIO_RESULT <JSON>``：执行器把日志按内容摘要存进记录库，签发从记录库里的日志取这一行。
# 执行记录（unit-execution-record/v1）与运行清单（unit-execution-manifest/v1）的格式由 tools/ci/unit_records.py 定义。
# 认证在数据根运行，那里没有 tools/ci，所以核验算法（规范 JSON 的 sha256、记录库的路径规则、按原始字段判通过）在这里
# 照写一份，测试与 unit_records 交叉核对。
SCENARIO_RESULT_V2_SCHEMA = "pre-a3-scenario-result/v2"
SCENARIO_RESULT_PREFIX = "PRE_A3_SCENARIO_RESULT "
UNIT_RECORD_SCHEMA = "unit-execution-record/v1"
UNIT_MANIFEST_SCHEMA = "unit-execution-manifest/v1"
UNIT_MODES = ("full-set-pass", "re-execute")
UNIT_MAX_AGE_HOURS = 168.0
# 补账场景在本模块里实现，用的是 test_codex_upgrade 的 B0 夹具。
ACCOUNTING_TEST_MODULE = "tools.official_client_capture.tests.test_codex_upgrade"
_SHA256_TEXT = re.compile(r"[0-9a-f]{64}")
_RUN_ID_TEXT = re.compile(r"[0-9A-Za-z._-]+")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sealed(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    body = {name: value for name, value in payload.items() if name != key}
    return {**body, key: _sha256_json(body)}


def _seal_matches(payload: Mapping[str, Any], key: str) -> bool:
    body = {name: value for name, value in payload.items() if name != key}
    return payload.get(key) == _sha256_json(body)


def _parse_utc(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def scenario_test_module(name: str) -> str:
    """场景用到的测试模块（点分名）：登记场景取场景表，补账场景是 test_codex_upgrade。"""

    if name == ACCOUNTING_SCENARIO_NAME:
        return ACCOUNTING_TEST_MODULE
    for scenario in SCENARIOS:
        if scenario[0] == name:
            return scenario[2]
    raise CertificationError(f"未登记的认证场景：{name}")


def run_scenario_unit(name: str, staging_parent: Path, *, diagnostic: bool = False) -> dict[str, Any]:
    """E3-02 的单场景入口：在 ``staging_parent`` 下新建本次的临时根跑一个场景，返回带自摘要的单场景结果
    （``pre-a3-scenario-result/v2``）。通过且零网络时删掉临时根，否则保留供排查。诊断执行（执行器下发
    ``UNIT_EXECUTOR_KIND=diagnostic``）的临时根另带前缀、结果标 ``kind=diagnostic``，不参与签发。"""

    if name not in scenario_names():
        raise CertificationError(f"未登记的认证场景：{name}")
    parent = Path(staging_parent).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in parent.parts:
        raise CertificationError("场景父目录必须位于 staging 目录树内")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = ("diagnostic-" if diagnostic else "") + _scenario_file_name(name) + "-"
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    result = run_single_scenario(name, root)
    payload = _sealed({
        "schema_version": SCENARIO_RESULT_V2_SCHEMA,
        "name": name,
        "kind": "diagnostic" if diagnostic else "formal",
        "scenario": result["scenario"],
        "network_attempts": result["network_attempts"],
        "finished_at_utc": result["finished_at_utc"],
        "staging_root": str(root),
    }, "result_sha256")
    if result["scenario"].get("status") == "passed" and result["network_attempts"] == 0:
        shutil.rmtree(root, ignore_errors=True)
    return payload


def _emit_result_line(result: Mapping[str, Any]) -> None:
    """把场景结果作为单独一行写到标准输出。前面先换行，免得接在场景自己没换行的输出后面；拿得到文件描述符时绕过
    缓冲、直接写满整行，不和别的输出交错。"""

    line = ("\n" + SCENARIO_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        descriptor = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        sys.stdout.write(line.decode("utf-8"))
        sys.stdout.flush()
        return
    while line:
        line = line[os.write(descriptor, line):]


def plan_scenario_commands(staging_parent: Path, *, python: str | None = None, cwd: Path | None = None) -> dict[str, Any]:
    """E3-02 的场景命令单元清单：每个场景一条 ``run-scenario --name X --staging-parent <父目录>``，命令只带稳定内容。
    每个单元另给场景的测试模块文件（``scenario_test_file``，仓库相对路径），入口门禁据此按静态依赖闭包声明单元输入。"""

    parent = Path(staging_parent).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in parent.parts:
        raise CertificationError("场景父目录必须位于 staging 目录树内")
    real_chains = {scenario[0] for scenario in SCENARIOS if ".real_chains." in scenario[2]}
    workdir = str(Path(cwd or os.getcwd()).resolve())
    units = []
    for name in scenario_names():
        units.append({
            "unit_id": f"{SCENARIO_UNIT_PREFIX}{name}",
            "argv": [python or sys.executable, "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification", "run-scenario",
                     "--name", name, "--staging-parent", str(parent)],
            "cwd": workdir,
            "cores": 1,
            "memory_mb": 1024,
            "timeout_seconds": REAL_CHAIN_TIMEOUT_SECONDS if name in real_chains else SCENARIO_TIMEOUT_SECONDS,
            "weight": SCENARIO_SECONDS.get(name, 30.0),
            "scenario_test_file": scenario_test_module(name).replace(".", "/") + ".py",
        })
    return {"schema_version": EXECUTOR_COMMANDS_SCHEMA, "staging_parent": str(parent), "units": units}


class _RecordStore:
    """记录库的只读访问，路径规则与 tools/ci/unit_records.RecordStore 相同：记录按单元 ID 摘要的前 16 位分桶，日志与
    运行清单按名字存放。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=True)

    def record_path(self, unit_id: str, record_sha256: str) -> Path:
        return self.root / "records" / hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:16] / f"{record_sha256}.json"

    def log_path(self, digest: str) -> Path:
        return self.root / "logs" / f"{digest}.log"

    def manifest_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.json"

    @staticmethod
    def read(path: Path) -> dict[str, Any] | None:
        try:
            if Path(path).is_symlink():
                return None
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


def _record_passes(record: Mapping[str, Any]) -> tuple[bool, str]:
    """按执行记录的原始字段重判结论，与 unit_records.derived_pass 的命令单元分支同一规则（不只看 passed 字段）。"""

    if record.get("exit_code") != 0 or record.get("signal") is not None or record.get("timed_out") is not False:
        return False, "退出状态不是成功"
    if record.get("unit_type") != "command":
        return False, "不是命令单元"
    if record.get("passed") is not True:
        return False, "记录结论不是通过"
    return True, ""


def _published_manifest(store: _RecordStore, path: Path) -> dict[str, Any]:
    """本次运行清单，并核对它就是记录库里已发布的那一份（执行器只在清单自检通过后发布）。"""

    manifest = store.read(Path(path))
    if manifest is None or manifest.get("schema_version") != UNIT_MANIFEST_SCHEMA or not _seal_matches(manifest, "manifest_sha256"):
        raise CertificationError("运行清单缺失、格式不对或自摘要不符")
    if manifest.get("mode") not in UNIT_MODES:
        raise CertificationError(f"运行清单的模式不认识：{manifest.get('mode')!r}")
    run_id = str(manifest.get("run_id") or "")
    published = store.read(store.manifest_path(run_id)) if _RUN_ID_TEXT.fullmatch(run_id) else None
    if published is None or not _seal_matches(published, "manifest_sha256") or published.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise CertificationError("运行清单没有发布到记录库（清单自检没通过的运行不能签发）")
    return published


def _scenario_from_records(store: _RecordStore, manifest: Mapping[str, Any], entry: Mapping[str, Any],
                           name: str) -> tuple[dict[str, Any] | None, int, list[str]]:
    """核验运行清单里一个场景的那一项，返回（带执行记录引用的场景记录、网络计数、问题）。问题非空即该场景失败。

    核对：处置是本次执行或承接（重新执行全集不得承接）；记录在库、文件名＝自摘要、是该场景的正式执行、规格与输入
    摘要等于清单、按原始字段判通过；本次执行的记录来自本次运行，承接的记录来自别的运行、原运行清单把它列为正式执行、
    没超过清单的承接期限；日志在库且摘要相符，日志里恰好一行场景结果、自摘要相符、场景名对、是正式执行、网络计数为 0、
    场景通过。"""

    unit_id = f"{SCENARIO_UNIT_PREFIX}{name}"
    disposition = entry.get("disposition")
    if disposition not in ("executed", "inherited"):
        return None, 0, [f"运行清单里的处置不是本次执行或承接：{disposition!r}"]
    if disposition == "inherited" and manifest.get("mode") != "full-set-pass":
        return None, 0, ["重新执行全集的运行清单不得有承接项"]
    digest = str(entry.get("record_sha256") or "")
    record = store.read(store.record_path(unit_id, digest)) if _SHA256_TEXT.fullmatch(digest) else None
    if record is None:
        return None, 0, ["执行记录不在记录库里"]
    if record.get("schema_version") != UNIT_RECORD_SCHEMA or not _seal_matches(record, "record_sha256") or record.get("record_sha256") != digest:
        return None, 0, ["执行记录自摘要不符（被改过）"]
    if record.get("unit_id") != unit_id or record.get("kind") != "formal":
        return None, 0, ["执行记录不是该场景的正式执行"]
    problems: list[str] = []
    if record.get("spec_sha256") != entry.get("spec_sha256") or record.get("inputs_sha256") != entry.get("inputs_sha256"):
        problems.append("执行记录的单元规格或输入摘要与运行清单不符")
    passed, why = _record_passes(record)
    if not passed:
        problems.append(f"执行记录不是通过（{why}）")
    run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
    run_id = str(run.get("run_id") or "")
    if disposition == "executed":
        if run_id != manifest.get("run_id"):
            problems.append("登记为本次执行，记录却来自别的运行")
    else:
        if run_id == manifest.get("run_id"):
            problems.append("承接项的记录来自本次运行")
        origin = store.read(store.manifest_path(run_id)) if _RUN_ID_TEXT.fullmatch(run_id) else None
        if origin is None or origin.get("schema_version") != UNIT_MANIFEST_SCHEMA or not _seal_matches(origin, "manifest_sha256"):
            problems.append("原运行的清单缺失或自摘要不符")
        elif not any(isinstance(item, Mapping) and item.get("unit_id") == unit_id and item.get("disposition") == "executed"
                     and item.get("record_sha256") == digest for item in origin.get("units") or []):
            problems.append("原运行的清单没有把这条记录列为该场景的正式执行")
        completed, decided = _parse_utc(record.get("completed_at_utc")), _parse_utc(manifest.get("decided_at_utc"))
        try:
            limit = min(float(manifest.get("inheritance_max_age_hours") or 0), UNIT_MAX_AGE_HOURS)
        except (TypeError, ValueError):
            limit = 0.0
        if completed is None or decided is None or not -360 <= decided - completed <= limit * 3600:
            problems.append("承接的记录超过运行清单的承接期限，或时间不可读")
    log = record.get("log") if isinstance(record.get("log"), Mapping) else {}
    log_digest = str(log.get("sha256") or "")
    log_path = store.log_path(log_digest) if _SHA256_TEXT.fullmatch(log_digest) else None
    data = log_path.read_bytes() if log_path is not None and log_path.is_file() and not log_path.is_symlink() else None
    if data is None or hashlib.sha256(data).hexdigest() != log_digest:
        return None, 0, problems + ["日志不在记录库里或摘要不符"]
    lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.startswith(SCENARIO_RESULT_PREFIX)]
    if len(lines) != 1:
        return None, 0, problems + [f"日志里的场景结果行不是恰好一行（{len(lines)} 行）"]
    try:
        result = json.loads(lines[0][len(SCENARIO_RESULT_PREFIX):])
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("schema_version") != SCENARIO_RESULT_V2_SCHEMA or not _seal_matches(result, "result_sha256"):
        return None, 0, problems + ["场景结果行非法或自摘要不符"]
    scenario, count = result.get("scenario"), result.get("network_attempts")
    if result.get("name") != name or result.get("kind") != "formal" or not isinstance(scenario, dict) or scenario.get("name") != name:
        return None, 0, problems + ["场景结果与场景名不符，或不是正式执行的结果"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None, 0, problems + ["场景结果里没有网络计数"]
    if scenario.get("status") != "passed" or count != 0:
        problems.append(f"场景没通过（{scenario.get('status')}，网络连接尝试 {count} 次）")
    reference = {
        "record_sha256": digest, "run_id": run_id, "disposition": disposition, "log_sha256": log_digest,
        "result_sha256": result["result_sha256"], "completed_at_utc": record.get("completed_at_utc"),
    }
    return {**scenario, "unit_record": reference}, count, problems


def _scenarios_from_records(store: _RecordStore, manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str], int]:
    """按场景全集逐个核验运行清单，返回（逐场景报告、失败项、网络计数合计）。清单里全集之外的 pre-A3 单元同样让认证
    不通过（记在失败项）。"""

    names = scenario_names()
    entries: dict[str, list[Mapping[str, Any]]] = {}
    for item in manifest.get("units") or []:
        if isinstance(item, Mapping) and str(item.get("unit_id", "")).startswith(SCENARIO_UNIT_PREFIX):
            entries.setdefault(str(item["unit_id"]), []).append(item)
    stray = sorted(set(entries) - {f"{SCENARIO_UNIT_PREFIX}{name}" for name in names})
    report: list[dict[str, Any]] = []
    attempts = 0
    for name in names:
        found = entries.get(f"{SCENARIO_UNIT_PREFIX}{name}", [])
        if len(found) != 1:
            report.append(_placeholder_record(name, ["运行清单里没有该场景（缺报）" if not found else "运行清单里该场景出现多次"], None))
            continue
        row, count, problems = _scenario_from_records(store, manifest, found[0], name)
        attempts += count
        if problems or row is None:
            report.append(_placeholder_record(name, problems or ["场景执行记录核验不通过"], (row or {}).get("seconds")))
        else:
            report.append(row)
    failed = [item["name"] for item in report if item["status"] != "passed"]
    failed += [f"全集之外的执行单元：{unit_id}" for unit_id in stray]
    return report, failed, attempts


def issue_certification_from_records(
    staging_root: Path,
    *,
    unit_manifest: Path,
    record_store: Path,
    deployment_receipt: Path,
    policy_activation: Path,
    campaign_run_rehearsal_receipt: Path | None = None,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """E3-02 签发 v2：从入口门禁 ``run-gates`` 的运行清单与记录库组装认证。

    签发条件：全部场景在清单里各恰好一项、每项都是一条通过的正式执行记录（本次执行或承接）、输入摘要等于清单（清单
    由执行器按当前测试树算出，入口门禁跑之前已核对数据根部署的就是这棵树）、网络计数为 0；清单本身已经发布到记录库。
    ``staging_root`` 是场景父目录（写进收据的 ``staging_root``，v1 字段含义不变：认证用的 staging 位置）。"""

    root = Path(staging_root).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in root.parts:
        raise CertificationError("场景父目录必须位于 staging 目录树内")
    bindings = _certification_bindings(deployment_receipt, policy_activation, campaign_run_rehearsal_receipt)
    try:
        store = _RecordStore(Path(record_store))
    except OSError as error:
        raise CertificationError(f"记录库不可读：{error}") from error
    manifest = _published_manifest(store, Path(unit_manifest))
    report, failed, attempts = _scenarios_from_records(store, manifest)
    records = {
        "unit_manifest": {"path": str(store.manifest_path(str(manifest["run_id"]))), "manifest_sha256": manifest["manifest_sha256"],
                          "run_id": manifest["run_id"], "mode": manifest["mode"]},
        "record_store": str(store.root),
    }
    return _build_receipt(root, bindings, report, failed, attempts, observed_at_utc, records=records)


def _verify_records_v2(payload: Mapping[str, Any]) -> None:
    """v2 认证的逐条重验：按收据登记的记录库与运行清单重新核验每个场景，逐场景报告必须与收据完全一致。"""

    reference = payload.get("unit_manifest") if isinstance(payload.get("unit_manifest"), Mapping) else {}
    try:
        store = _RecordStore(Path(str(payload.get("record_store") or "")))
    except OSError as error:
        raise CertificationError(f"v2 认证登记的记录库不可读：{error}") from error
    run_id = str(reference.get("run_id") or "")
    manifest = store.read(store.manifest_path(run_id)) if _RUN_ID_TEXT.fullmatch(run_id) else None
    if (manifest is None or manifest.get("schema_version") != UNIT_MANIFEST_SCHEMA or not _seal_matches(manifest, "manifest_sha256")
            or manifest.get("manifest_sha256") != reference.get("manifest_sha256") or manifest.get("mode") != reference.get("mode")):
        raise CertificationError("v2 认证引用的运行清单在记录库里缺失，或与收据登记的不符")
    report, failed, attempts = _scenarios_from_records(store, manifest)
    if failed or attempts:
        raise CertificationError(f"v2 认证的执行记录重验不通过：{failed[:3]}")
    if payload.get("scenarios") != report:
        raise CertificationError("v2 认证的逐场景结果与记录库重验结果不一致")


def write_certification(output: Path, receipt: Mapping[str, Any]) -> Path:
    """通过的认证写正式路径（write-once）；没通过的写带 UTC 时间后缀的旁路文件，正式路径保持不存在，修好后同一坐标
    直接重跑（原来失败的认证也写正式路径，重跑被「文件已存在」挡住，驱动还会把它当成已有认证）。"""

    output = Path(output).resolve(strict=False)
    if receipt.get("status") == "passed":
        target = output
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        target = output.with_name(f"{output.stem}.failed-{stamp}{output.suffix}")
        index = 1
        while target.exists():
            index += 1
            target = output.with_name(f"{output.stem}.failed-{stamp}-{index}{output.suffix}")
    policy_certification._write_once(target, dict(receipt))
    return target


def verify_certification(path: Path, *, expected_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """只读校验认证对当前工具是否有效：v1、v2 都认；v2 另按登记的记录库与运行清单逐条重验执行记录（E3-02）。"""

    payload = policy_certification._read_json(Path(path), "pre-A3 路径认证收据")
    if payload.get("schema_version") not in CERTIFICATION_SCHEMAS or payload.get("status") != "passed":
        raise CertificationError("路径认证收据 schema 非法或未通过")
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    if _fingerprint(unsigned) != payload.get("receipt_sha256"):
        raise CertificationError("路径认证收据自摘要不一致")
    identity = expected_identity or policy_certification.current_identity()
    if {k: str(v) for k, v in (payload.get("identity") or {}).items()} != {
        name: str(identity[name]) for name in policy_certification.IDENTITY_FIELDS
    }:
        raise CertificationError("路径认证收据五摘要与当前工具身份不一致")
    if payload.get("network_attempts") != 0 or payload.get("live_request_count") != 0:
        raise CertificationError("路径认证收据零请求边界不满足")
    if payload["schema_version"] == SCHEMA_VERSION_V2:
        _verify_records_v2(payload)
    return payload


def _current_bindings(deployment_receipt: Path, policy_activation: Path) -> dict[str, Any]:
    """本次部署的绑定：当前工具身份、通过校验的部署收据与激活认证（五摘要都必须等于当前身份、策略等于当前策略）。

    任一项不合法即抛错：这不是"没有可复用的认证"，而是本次部署本身不能认证（新跑 pre-A3 同样会失败）。
    """

    identity = policy_certification.current_identity()
    deployment_path = Path(deployment_receipt).resolve(strict=True)
    deployment = policy_certification.load_deployment_receipt(deployment_path, expected_identity=identity)
    activation_path = Path(policy_activation).resolve(strict=True)
    activation = policy_certification.verify_activation_certification(activation_path, expected_identity=identity)
    return {
        "identity": identity,
        "deployment_path": deployment_path,
        "deployment": deployment,
        "activation_path": activation_path,
        "activation": activation,
    }


def verify_reusable_certification(path: Path, *, bindings: Mapping[str, Any]) -> dict[str, Any]:
    """按工具身份判定一份 pre-A3 认证对本次部署是否可复用（修好接着跑第 19 项）。

    可复用当且仅当：收据通过 ``verify_certification``（自摘要、五摘要等于当前工具身份、零请求）、策略版本
    等于当前策略、收据绑定的激活策略摘要等于本次激活认证的策略摘要、真实链登记集合等于当前发布包。
    不再要求收据绑定的部署收据／激活认证与本次逐字相同：重新部署一次但工具五摘要与策略都没变时仍复用；
    策略变化、任一摘要不同、收据自身校验失败都不复用。
    """

    identity = bindings["identity"]
    payload = verify_certification(path, expected_identity=identity)
    if payload.get("policy_version") != identity["policy_version"]:
        raise CertificationError("路径认证收据 policy_version 与当前策略不一致")
    bound_activation = payload.get("policy_activation") or {}
    if bound_activation.get("policy_sha256") != bindings["activation"].get("policy_sha256"):
        raise CertificationError("路径认证收据绑定的激活策略与本次激活认证不一致")
    real_chain_coverage(payload)
    return payload


def find_reusable_certification(
    *,
    deployment_receipt: Path,
    policy_activation: Path,
    search_root: Path | None = None,
    certification: Path | None = None,
) -> Path | None:
    """可复用的 pre-A3 认证（修好接着跑第 19 项）。

    每次重建 Campaign 都重跑约 55 分钟的 pre-A3，而工具身份没变——只是重新部署一次（部署收据 sha 变了）
    也一样。判定只看工具身份：本次部署收据与激活认证先各自通过校验（``_current_bindings``），候选认证再按
    ``verify_reusable_certification`` 核验，不要求部署收据／激活认证的 sha 逐字相等。``certification`` 给出时
    只核验该份（stage1 建账本前的门禁，修好接着跑第 18 项）；否则在 ``search_root`` 下取认证时间最近的一份。
    """

    bindings = _current_bindings(deployment_receipt, policy_activation)
    candidates = [Path(certification)] if certification is not None else sorted(Path(search_root).glob("*.json"))
    best: tuple[str, Path] | None = None
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = verify_reusable_certification(path, bindings=bindings)
        except (CertificationError, policy_certification.PolicyCertificationError, OSError, ValueError):
            continue
        key = str(payload.get("certified_at_utc"))
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best is not None else None


def _reuse_binding_key(
    *,
    certification_receipt_sha256: Any,
    deployment_receipt_sha256: Any,
    policy_activation_sha256: Any,
    identity: Any,
    policy_version: Any,
) -> dict[str, Any]:
    """复用收据的判重键：只由内容摘要构成（不含路径与时刻），同一组绑定重复登记时据此判定"已登记"。"""

    return {
        "reused_certification_receipt_sha256": certification_receipt_sha256,
        "deployment_receipt_sha256": deployment_receipt_sha256,
        "policy_activation_sha256": policy_activation_sha256,
        "identity": identity,
        "policy_version": policy_version,
    }


def build_reuse_receipt(
    certification: Path,
    *,
    deployment_receipt: Path,
    policy_activation: Path,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """复用收据（不落盘）：把被复用的 pre-A3 认证与本次部署收据、激活认证、五摘要绑定在一起，供审计与发布认证。

    被复用的认证文件本身不改动、不重签；收据同时记下认证原先绑定的部署收据／激活认证摘要，审计时可以
    看出"这份认证是在哪次部署下签发、在哪次部署下被复用"。
    """

    bindings = _current_bindings(deployment_receipt, policy_activation)
    certification_path = Path(certification).resolve(strict=True)
    payload = verify_reusable_certification(certification_path, bindings=bindings)
    identity = bindings["identity"]
    five = {name: identity[name] for name in policy_certification.IDENTITY_FIELDS}
    deployment_sha256 = codex_upgrade.file_sha256(bindings["deployment_path"])
    activation_sha256 = codex_upgrade.file_sha256(bindings["activation_path"])
    key = _reuse_binding_key(
        certification_receipt_sha256=str(payload.get("receipt_sha256")),
        deployment_receipt_sha256=deployment_sha256,
        policy_activation_sha256=activation_sha256,
        identity=five,
        policy_version=identity["policy_version"],
    )
    receipt = {
        "schema_version": REUSE_SCHEMA_VERSION,
        "reused_at_utc": observed_at_utc or _utc_now(),
        "identity": five,
        "policy_version": identity["policy_version"],
        "reused_certification": {
            "path": str(certification_path),
            "sha256": codex_upgrade.file_sha256(certification_path),
            "receipt_sha256": str(payload.get("receipt_sha256")),
            "certified_at_utc": payload.get("certified_at_utc"),
            "bound_deployment_receipt_sha256": (payload.get("deployment_receipt") or {}).get("sha256"),
            "bound_policy_activation_sha256": (payload.get("policy_activation") or {}).get("sha256"),
        },
        "deployment_receipt": {
            "path": str(bindings["deployment_path"]),
            "sha256": deployment_sha256,
            "created_at_utc": bindings["deployment"].get("created_at_utc"),
            "supervisor_sha256": bindings["deployment"].get("supervisor_sha256"),
        },
        "policy_activation": {
            "path": str(bindings["activation_path"]),
            "sha256": activation_sha256,
            "policy_sha256": bindings["activation"].get("policy_sha256"),
            "activated_at_utc": bindings["activation"].get("activated_at_utc"),
        },
        "binding_sha256": _fingerprint(key),
        "rule": "工具五摘要与策略未变时复用既有 pre-A3 认证；本收据只登记复用事实，不改变被复用的认证文件。",
    }
    receipt["receipt_sha256"] = _fingerprint(receipt)
    return receipt


def load_reuse_receipt(path: Path, *, expected_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """只读校验复用收据：schema、自摘要、判重键与内容一致、五摘要与策略版本等于期望身份。"""

    payload = policy_certification._read_json(Path(path), "pre-A3 复用收据")
    if payload.get("schema_version") != REUSE_SCHEMA_VERSION:
        raise CertificationError("pre-A3 复用收据 schema 非法")
    unsigned = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    if _fingerprint(unsigned) != payload.get("receipt_sha256"):
        raise CertificationError("pre-A3 复用收据自摘要不一致")
    identity = expected_identity or policy_certification.current_identity()
    if {k: str(v) for k, v in (payload.get("identity") or {}).items()} != {
        name: str(identity[name]) for name in policy_certification.IDENTITY_FIELDS
    } or payload.get("policy_version") != identity["policy_version"]:
        raise CertificationError("pre-A3 复用收据五摘要或策略版本与当前工具身份不一致")
    key = _reuse_binding_key(
        certification_receipt_sha256=(payload.get("reused_certification") or {}).get("receipt_sha256"),
        deployment_receipt_sha256=(payload.get("deployment_receipt") or {}).get("sha256"),
        policy_activation_sha256=(payload.get("policy_activation") or {}).get("sha256"),
        identity=payload.get("identity"),
        policy_version=payload.get("policy_version"),
    )
    if _fingerprint(key) != payload.get("binding_sha256"):
        raise CertificationError("pre-A3 复用收据判重键与内容不一致")
    return payload


def verify_reuse_receipt(
    path: Path,
    *,
    certification: Mapping[str, Any],
    deployment_receipt_sha256: str,
    policy_activation_sha256: str | None = None,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """复用收据必须把给定的 pre-A3 认证（按 ``receipt_sha256``）绑定到给定的部署收据（及激活认证）。"""

    payload = load_reuse_receipt(path, expected_identity=expected_identity)
    if (payload.get("reused_certification") or {}).get("receipt_sha256") != certification.get("receipt_sha256"):
        raise CertificationError("pre-A3 复用收据绑定的不是这份路径认证")
    if (payload.get("deployment_receipt") or {}).get("sha256") != deployment_receipt_sha256:
        raise CertificationError("pre-A3 复用收据绑定的不是本次部署收据")
    if policy_activation_sha256 is not None and (payload.get("policy_activation") or {}).get("sha256") != policy_activation_sha256:
        raise CertificationError("pre-A3 复用收据绑定的不是本次激活认证")
    return payload


def find_reuse_receipt(receipt_root: Path, binding_sha256: str) -> Path | None:
    """目录下已登记同一组绑定（判重键相同）的复用收据；没有则返回 None。"""

    for path in sorted(Path(receipt_root).glob(f"{REUSE_RECEIPT_PREFIX}*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = load_reuse_receipt(path)
        except (CertificationError, policy_certification.PolicyCertificationError, OSError, ValueError):
            continue
        if payload.get("binding_sha256") == binding_sha256:
            return path
    return None


def record_reuse(
    certification: Path,
    *,
    deployment_receipt: Path,
    policy_activation: Path,
    receipt_root: Path,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    """登记复用事实（write-once、幂等），返回摘要供驱动脚本打印：

    * 认证就是本次部署、本次激活认证下签发的（两项绑定逐字相同）：不需要复用收据，``status=not_needed``；
    * 同一组绑定（认证内容、部署收据、激活认证、五摘要、策略版本）已登记：返回既有收据，不重复写；
    * 否则写 ``<receipt_root>/pre-a3-reuse-<UTC 时刻>.json``（0600、只写一次）。
    """

    receipt = build_reuse_receipt(
        certification, deployment_receipt=deployment_receipt, policy_activation=policy_activation, observed_at_utc=observed_at_utc
    )
    reused = receipt["reused_certification"]
    summary = {
        "reused_certification": reused["path"],
        "reused_certification_receipt_sha256": reused["receipt_sha256"],
        "binding_sha256": receipt["binding_sha256"],
    }
    if (
        reused["bound_deployment_receipt_sha256"] == receipt["deployment_receipt"]["sha256"]
        and reused["bound_policy_activation_sha256"] == receipt["policy_activation"]["sha256"]
    ):
        return {**summary, "status": "not_needed", "reuse_receipt": None}
    root = Path(receipt_root).resolve(strict=False)
    existing = find_reuse_receipt(root, receipt["binding_sha256"])
    if existing is not None:
        return {**summary, "status": "existing", "reuse_receipt": str(existing)}
    stamp = "".join(ch for ch in str(receipt["reused_at_utc"]) if ch.isalnum())
    path = root / f"{REUSE_RECEIPT_PREFIX}{stamp}.json"
    policy_certification._write_once(path, receipt)
    return {**summary, "status": "recorded", "reuse_receipt": str(path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2.5：pre-A3 路径认证。")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run", help="在 staging 内串行跑通全部路径场景并出具收据（没通过只写旁路文件）")
    run.add_argument("--staging-root", type=Path, required=True, help="staging 目录树内的认证根；已存在且非空时自动改用带时间后缀的新目录")
    run.add_argument("--deployment-receipt", type=Path, required=True)
    run.add_argument("--policy-activation", type=Path, required=True)
    run.add_argument("--campaign-run-rehearsal-receipt", type=Path)
    run.add_argument("--output", type=Path, required=True)
    plan = subparsers.add_parser("plan", help="生成统一调度执行器的场景命令单元清单（每个场景一条 run-scenario）")
    plan_root = plan.add_mutually_exclusive_group(required=True)
    plan_root.add_argument("--staging-root", type=Path,
                           help="E2-03：staging 目录树内的认证根；已存在且非空时自动改用带时间后缀的新目录")
    plan_root.add_argument("--staging-parent", type=Path,
                           help="E3-02：staging 目录树内的场景父目录（固定位置）；命令只带稳定内容，入口门禁可承接")
    plan.add_argument("--output", type=Path, required=True, help="清单写到这里（执行器 run-commands --manifest，或入口门禁 --pre-a3-units）")
    single = subparsers.add_parser("run-scenario", help="在本进程跑一个场景（执行器派发）")
    single.add_argument("--name", required=True)
    single.add_argument("--staging-parent", type=Path,
                        help="E3-02：场景父目录；本次临时根在其下新建，结果作为标准输出里的一行 PRE_A3_SCENARIO_RESULT")
    single.add_argument("--staging-root", type=Path, help="E2-03：场景临时根（与 --result 同用）")
    single.add_argument("--result", type=Path, help="E2-03：单场景结果文件")
    issue = subparsers.add_parser("issue", help="核对场景全集与网络计数后签发（没通过只写旁路文件）")
    issue.add_argument("--staging-root", type=Path, required=True, help="E2-03：plan 打印的实际认证根；E3-02：场景父目录")
    issue_source = issue.add_mutually_exclusive_group(required=True)
    issue_source.add_argument("--executor-summary", type=Path, help="E2-03：执行器 run-commands 的 summary.json，签 v1")
    issue_source.add_argument("--unit-manifest", type=Path,
                              help="E3-02：执行器 run-gates 的 unit-manifest.json，签 v2（另给 --record-store）")
    issue.add_argument("--record-store", type=Path, help="E3-02：记录库目录（与 --unit-manifest 同用）")
    issue.add_argument("--deployment-receipt", type=Path, required=True)
    issue.add_argument("--policy-activation", type=Path, required=True)
    issue.add_argument("--campaign-run-rehearsal-receipt", type=Path)
    issue.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="只读校验收据对当前工具是否有效")
    verify.add_argument("--certification", type=Path, required=True)
    reusable = subparsers.add_parser(
        "find-reusable",
        help="工具身份（五摘要）与策略未变时可复用的认证（重新部署也复用）：找到打印路径并退出 0，否则退出 1",
    )
    scope = reusable.add_mutually_exclusive_group(required=True)
    scope.add_argument("--search-root", type=Path, help="在该目录下找认证时间最近的可复用认证")
    scope.add_argument("--certification", type=Path, help="只核验这一份认证对本次部署是否有效")
    reusable.add_argument("--deployment-receipt", type=Path, required=True)
    reusable.add_argument("--policy-activation", type=Path, required=True)
    record = subparsers.add_parser(
        "record-reuse",
        help="登记本轮认证对本次部署的复用事实（write-once、按绑定内容幂等；认证就是本次部署下签发的则不写）",
    )
    record.add_argument("--certification", type=Path, required=True)
    record.add_argument("--deployment-receipt", type=Path, required=True)
    record.add_argument("--policy-activation", type=Path, required=True)
    record.add_argument("--receipt-root", type=Path, required=True, help="复用收据目录（通常就是认证所在目录）")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action in ("run", "issue"):
            if arguments.action == "run":
                receipt = run_certification(
                    arguments.staging_root,
                    deployment_receipt=arguments.deployment_receipt,
                    policy_activation=arguments.policy_activation,
                    campaign_run_rehearsal_receipt=arguments.campaign_run_rehearsal_receipt,
                )
            elif arguments.unit_manifest is not None:
                if arguments.record_store is None:
                    raise CertificationError("--unit-manifest 需要同时给 --record-store")
                receipt = issue_certification_from_records(
                    arguments.staging_root,
                    unit_manifest=arguments.unit_manifest,
                    record_store=arguments.record_store,
                    deployment_receipt=arguments.deployment_receipt,
                    policy_activation=arguments.policy_activation,
                    campaign_run_rehearsal_receipt=arguments.campaign_run_rehearsal_receipt,
                )
            else:
                if arguments.record_store is not None:
                    raise CertificationError("--record-store 只与 --unit-manifest 同用")
                receipt = issue_certification(
                    arguments.staging_root,
                    executor_summary=arguments.executor_summary,
                    deployment_receipt=arguments.deployment_receipt,
                    policy_activation=arguments.policy_activation,
                    campaign_run_rehearsal_receipt=arguments.campaign_run_rehearsal_receipt,
                )
            target = write_certification(arguments.output, receipt)
            summary = {
                "schema_version": receipt["schema_version"],
                "status": receipt["status"],
                "scenario_count": receipt["scenario_count"],
                "failed_scenarios": receipt["failed_scenarios"],
                "network_attempts": receipt["network_attempts"],
                "staging_root": receipt["staging_root"],
                "output": str(target),
            }
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0 if receipt["status"] == "passed" else 2
        if arguments.action == "plan":
            if arguments.staging_parent is not None:
                manifest = plan_scenario_commands(arguments.staging_parent)
                where = {"staging_parent": manifest["staging_parent"]}
            else:
                manifest = plan_scenario_units(arguments.staging_root)
                where = {"staging_root": manifest["staging_root"]}
            policy_certification._write_once(arguments.output.resolve(strict=False), manifest)
            print(json.dumps({**where, "units": len(manifest["units"]), "manifest": str(arguments.output)}, ensure_ascii=False, sort_keys=True))
            return 0
        if arguments.action == "run-scenario":
            # 执行器的诊断重跑（UNIT_EXECUTOR_KIND=diagnostic）用同一条命令：临时目录另放、结果标诊断，不顶替正式执行。
            diagnostic = os.environ.get("UNIT_EXECUTOR_KIND") == "diagnostic"
            if arguments.staging_parent is not None:
                if arguments.staging_root is not None or arguments.result is not None:
                    raise CertificationError("--staging-parent 不能与 --staging-root、--result 同用")
                result = run_scenario_unit(arguments.name, arguments.staging_parent, diagnostic=diagnostic)
                record = result["scenario"]
                print(json.dumps({"name": arguments.name, "status": record.get("status"), "network_attempts": result["network_attempts"],
                                  "seconds": record.get("seconds"), "staging_root": result["staging_root"]}, ensure_ascii=False, sort_keys=True),
                      flush=True)
                _emit_result_line(result)
                return 0 if record.get("status") == "passed" and result["network_attempts"] == 0 else 1
            if arguments.staging_root is None or arguments.result is None:
                raise CertificationError("run-scenario 需要 --staging-parent，或者同时给 --staging-root 与 --result")
            staging = Path(f"{arguments.staging_root}.diagnostic") if diagnostic else arguments.staging_root
            result_path = arguments.result.with_name(arguments.result.stem + ".diagnostic.json") if diagnostic else arguments.result
            result = run_single_scenario(arguments.name, staging)
            result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = result_path.with_name(f".{result_path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            temporary.chmod(0o600)
            try:
                # 硬链接发布：读者看不到半写文件，且同一场景第二次上报直接失败（FileExistsError → 退出码 2，issue
                # 判该场景异常退出），不会悄悄覆盖第一次的结果。
                os.link(temporary, result_path)
            finally:
                temporary.unlink(missing_ok=True)
            record = result["scenario"]
            print(json.dumps({"name": arguments.name, "status": record.get("status"), "network_attempts": result["network_attempts"],
                              "seconds": record.get("seconds")}, ensure_ascii=False, sort_keys=True))
            return 0 if record.get("status") == "passed" and result["network_attempts"] == 0 else 1
        if arguments.action == "find-reusable":
            found = find_reusable_certification(
                deployment_receipt=arguments.deployment_receipt,
                policy_activation=arguments.policy_activation,
                search_root=arguments.search_root,
                certification=arguments.certification,
            )
            if found is None:
                print("没有工具身份与策略未变、对本次部署有效的 pre-A3 认证。", file=sys.stderr)
                return 1
            print(found)
            return 0
        if arguments.action == "record-reuse":
            result = record_reuse(
                arguments.certification,
                deployment_receipt=arguments.deployment_receipt,
                policy_activation=arguments.policy_activation,
                receipt_root=arguments.receipt_root,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        payload = verify_certification(arguments.certification)
        print(json.dumps({"status": "valid", "scenario_count": payload["scenario_count"]}, ensure_ascii=False))
        return 0
    except (CertificationError, policy_certification.PolicyCertificationError, codex_upgrade.ConfigurationError, OSError, ValueError) as error:
        print(f"pre-A3 路径认证失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
