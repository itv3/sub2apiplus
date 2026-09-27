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
"""

from __future__ import annotations

import argparse
import json
import os
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


def run_certification(
    staging_root: Path,
    *,
    deployment_receipt: Path,
    policy_activation: Path,
    campaign_run_rehearsal_receipt: Path | None = None,
    scenarios: tuple[tuple[str, str, str, str, str], ...] = SCENARIOS,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    staging_root = Path(staging_root).resolve(strict=False)
    if project_ledger.STAGING_DIR_NAME not in staging_root.parts:
        raise CertificationError("认证根必须位于 staging 目录树内")
    if staging_root.exists() and any(staging_root.iterdir()):
        raise CertificationError("认证根必须是空目录")
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_root.chmod(0o700)
    identity = policy_certification.current_identity()
    deployment_path = Path(deployment_receipt).resolve(strict=True)
    deployment = policy_certification.load_deployment_receipt(deployment_path, expected_identity=identity)
    activation_path = Path(policy_activation).resolve(strict=True)
    activation = policy_certification.verify_activation_certification(activation_path, expected_identity=identity)
    rehearsal_binding = _bind_rehearsal_receipt(campaign_run_rehearsal_receipt, identity)
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
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if not failed and not attempts else "failed",
        "certified_at_utc": observed_at_utc or _utc_now(),
        "staging_root": str(staging_root),
        "fixture_only": True,
        "identity": {name: identity[name] for name in policy_certification.IDENTITY_FIELDS},
        "policy_version": identity["policy_version"],
        "deployment_receipt": {
            "path": str(deployment_path),
            "sha256": codex_upgrade.file_sha256(deployment_path),
            "created_at_utc": deployment.get("created_at_utc"),
        },
        "policy_activation": {
            "path": str(activation_path),
            "sha256": codex_upgrade.file_sha256(activation_path),
            "policy_sha256": activation.get("policy_sha256"),
        },
        "campaign_run_rehearsal_receipt": rehearsal_binding,
        "real_chain_registration": real_chain_registration(),
        "scenarios": report,
        "scenario_count": len(report),
        "failed_scenarios": failed,
        "network_attempts": len(attempts),
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    receipt["receipt_sha256"] = _fingerprint(receipt)
    return receipt


def verify_certification(path: Path, *, expected_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = policy_certification._read_json(Path(path), "pre-A3 路径认证收据")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("status") != "passed":
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
    run = subparsers.add_parser("run", help="在 staging 内跑通全部路径场景并出具收据")
    run.add_argument("--staging-root", type=Path, required=True, help="staging 目录树内的空目录")
    run.add_argument("--deployment-receipt", type=Path, required=True)
    run.add_argument("--policy-activation", type=Path, required=True)
    run.add_argument("--campaign-run-rehearsal-receipt", type=Path)
    run.add_argument("--output", type=Path, required=True)
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
        if arguments.action == "run":
            receipt = run_certification(
                arguments.staging_root,
                deployment_receipt=arguments.deployment_receipt,
                policy_activation=arguments.policy_activation,
                campaign_run_rehearsal_receipt=arguments.campaign_run_rehearsal_receipt,
            )
            policy_certification._write_once(arguments.output.resolve(strict=False), receipt)
            summary = {
                "status": receipt["status"],
                "scenario_count": receipt["scenario_count"],
                "failed_scenarios": receipt["failed_scenarios"],
                "network_attempts": receipt["network_attempts"],
                "output": str(arguments.output),
            }
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            return 0 if receipt["status"] == "passed" else 2
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
