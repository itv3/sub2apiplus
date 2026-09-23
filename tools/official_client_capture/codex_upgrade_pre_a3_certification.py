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
FIXTURE_ONLY_ENV = project_ledger_fixture.FIXTURE_ONLY_ENV
# R13 分阶段登记：阶段 1 只要求 validation_only 连续链，后续再追加取证与生产替换链。
REAL_CHAIN_IDS = ("vc-chain.full-validation-only",)
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
        "accounting.unresolved-blocks-terminal-allowed",
        "请求数无法确定：unresolved 使总账 blocked，campaign_terminal 仍可写",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_unresolved_accounting_blocks_and_terminates",
    ),
    (
        "deadline-interruption.attempt-failed-before-abandon",
        "deadline 到期：metadata-only attempt_failed 先于 stage_abandoned 与 stop_the_line",
        "tools.official_client_capture.tests.test_codex_upgrade",
        "CodexUpgradeTest",
        "test_b0_reconcile_attempt_deadline_expired_fails_attempt_before_abandoning_stage",
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
    """unresolved → accounting_resolved：以新的已解析 provenance 逐个补账并解除 blocked。"""

    from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler
    from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests

    started = time.monotonic()
    record: dict[str, Any] = {
        "name": "accounting.resolved-unblocks",
        "description": "accounting_resolved 绑定新 provenance 逐个补账，未决集合清空后解除 blocked",
        "test": "inline:codex_upgrade_pre_a3_certification._accounting_resolved_scenario",
    }
    try:
        case = upgrade_tests.CodexUpgradeTest("test_b0_reconcile_attempt_unresolved_accounting_blocks_and_terminates")
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
        if result["status"] != "permanent_stop" or result["decision"]["terminal_reason"] != "accounting_unresolved":
            raise CertificationError("未决账务未使总账 blocked")
        operation_id = f"reconcile-attempt:{attempt_id}"
        head = project_ledger.replay_head(fixture["ledger"])
        if not head["blocked"] or operation_id not in head["unresolved_operation_ids"]:
            raise CertificationError("总账未登记未决 operation")
        # 夹具内把无法识别的证据根移除后重新核算：得到已解析的 provenance（夹具专用操作）。
        (evidence_root / "surface.json").unlink()
        evidence_root.rmdir()
        resolved = provenance.collect_campaign_provenance(
            campaign_dir, formal_campaign_id=str(fixture["manifest"]["campaign_id"]), estimation_policy="none"
        )
        if resolved["status"] != "complete":
            raise CertificationError("重新核算后的 provenance 仍未解析")
        with project_ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
            project_ledger.write_batch(
                ledger_dir,
                operation_id=f"accounting-resolved:{attempt_id}",
                event_type="accounting_resolved",
                payload={
                    "campaign_id": fixture["manifest"]["campaign_id"],
                    "resolved_operation_id": operation_id,
                    "request": {
                        "status": "resolved",
                        "identity_keys": [item["identity_key"] for item in resolved["requests"]],
                        "estimated_delta": 0,
                        "estimated_sources": [],
                        "provenance_identity_keys_sha256": resolved["identity_keys_sha256"],
                    },
                },
                source={"kind": "provenance", "sha256": _fingerprint(resolved)},
            )
        project_ledger.reconcile_project_ledger(fixture["ledger"], campaign_dir=campaign_dir)
        head = project_ledger.replay_head(fixture["ledger"])
        if head["blocked"] or head["unresolved_operation_ids"]:
            raise CertificationError("补账后总账仍 blocked")
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
        payload = verify_certification(arguments.certification)
        print(json.dumps({"status": "valid", "scenario_count": payload["scenario_count"]}, ensure_ascii=False))
        return 0
    except (CertificationError, policy_certification.PolicyCertificationError, codex_upgrade.ConfigurationError, OSError, ValueError) as error:
        print(f"pre-A3 路径认证失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
