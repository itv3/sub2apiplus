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
# (场景名, 说明, 测试模块, 测试类, 测试方法)
SCENARIOS: tuple[tuple[str, str, str, str, str], ...] = (
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
    return record


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
        attempt_id = case._b0_orphan_attempt(fixture)
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
