"""campaign-resume（修好接着跑第 10 项后半、第 11 项）：凭修复证据恢复停线的 Campaign。

背景：同根因达上限、身份变化、账务、污染、预算等原因停线后，计时账本 stopped 与总账 campaign_terminal
基本不可恢复（recovery_verified 只覆盖同根因两次且无编排入口，总账终态不可撤销），只能新建 Campaign。
另外对账器写终态的幂等键写死，撤销终态后同一对象再次停线会被静默吞掉。本文件覆盖：恢复前置与拒绝、
两本账的写入顺序与幂等、半完成时对账零写入拒绝、恢复后同一对象可再次停线（恢复纪元后缀）。
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture.tests import test_codex_upgrade


class CampaignResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = test_codex_upgrade.CodexUpgradeTest("test_b0_reconcile_attempt_same_root_cause_limit_stops_the_line")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _stopped(self, root: Path) -> tuple[dict, str, str]:
        """同根因第二次失败：第三批 B3-9 起对账只暂停（root_cause_repair），计时账本 stop_required、总账无终态。"""

        fixture = self.helper._b0_fixture(root)
        campaign_dir = fixture["campaign_dir"]
        first = self.helper._b0_orphan_attempt(fixture)
        self.assertEqual(reconciler.reconcile_attempt(campaign_dir, first)["status"], "recoverable")
        second = self.helper._b0_orphan_attempt(fixture)
        stopped = reconciler.reconcile_attempt(campaign_dir, second)
        self.assertEqual((stopped["status"], stopped["decision"]["pause_kinds"]), ("paused", ["root_cause_repair"]))
        self.assertEqual(timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], "stop_required")
        return fixture, second, str(stopped["root_cause"]["root_cause_id"])

    def _fresh_deployment(self, fixture: dict, *, seconds: int = 2) -> Path:
        """修复后受监督部署的收据：五摘要等于当前工具，创建时间晚于停线。"""

        tool = codex_upgrade._tool_identity(include_git=False)
        created = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return self.helper._write_import_receipt(
            fixture["control"] / f"codex-0154-supervisor-enable-{created.strftime('%Y%m%dt%H%M%Sz')}.json",
            {
                "schema_version": codex_upgrade.ARM64_SUPERVISED_DEPLOY_RECEIPT_SCHEMA,
                "status": "passed",
                "campaign_id": "codex-0154-b0-deploy",
                "created_at_utc": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "architecture": "aarch64",
                "production_tool_root": "/root/docker/capture-cli/data/tools/official_client_capture",
                "production_doc_root": "/root/docker/capture-cli/data/docs",
                "tool_files_sha256": tool["files_sha256"],
                "policy_version": tool["policy_version"],
                "policy_sha256": tool["policy_sha256"],
                "wire_producer_sha256": tool["wire_producer_sha256"],
                "evidence_semantics_sha256": tool["evidence_semantics_sha256"],
                "control_sha256": tool["control_sha256"],
                "supervisor_sha256": "1" * 64,
                "assertion_preparer_sha256": "2" * 64,
                "rollback_backup": None,
            },
        )

    @staticmethod
    def _arguments(fixture: dict, regression: Path, *, approve: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=fixture["campaign_dir"],
            fix_commit="a" * 40,
            regression_receipt=regression,
            reason="修复了导致同根因连续失败的工具缺陷",
            control_root=fixture["control"],
            approve_sha256=approve,
            approved_by="老板" if approve else None,
        )

    @staticmethod
    def _regression(root: Path) -> Path:
        path = root / "regression-summary.txt"
        path.write_text("make test-capture-tools exit=0\n", encoding="utf-8")
        return path

    def test_root_cause_limit_stop_is_resumed_then_recoverable_and_can_stop_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, second, cause = self._stopped(root)
            campaign_dir = fixture["campaign_dir"]
            campaign_id = str(fixture["manifest"]["campaign_id"])
            regression = self._regression(root)
            # 部署收据早于停线：修复必须在停线之后受监督部署。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "部署收据早于停线"):
                codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            self._fresh_deployment(fixture)
            preview = codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            self.assertEqual(preview["status"], "approval_required")
            self.assertEqual(preview["timing"]["cleared_root_cause_ids"], [cause])
            # 第三批 B3-9：总账没有终态（上限只暂停），campaign-resume 仍按项目 at_limit 清零该根因。
            self.assertIsNone(preview["project"]["terminal"])
            self.assertEqual(preview["project"]["cleared_root_cause_ids"], [cause])
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "批准摘要与重算"):
                codex_upgrade._campaign_resume_command(self._arguments(fixture, regression, approve="0" * 64))
            applied = codex_upgrade._campaign_resume_command(
                self._arguments(fixture, regression, approve=preview["review_sha256"])
            )
            self.assertEqual((applied["status"], applied["resume_epoch"]), ("resumed", 1))
            # 计时账本：清零根因，以原起点重开被放弃的阶段为 recovery_required；总账：撤销终态、清零本版本根因。
            summary = timing_ledger.inspect_ledger(fixture["timing_ledger"])
            # 第三批 B3-9：上限只暂停、没有放弃阶段，清零后账本直接回到 active（VC-0 仍在进行中），由重新对账再登记恢复。
            self.assertEqual((summary["status"], summary["active_phase"], summary["recovery_root_cause_id"]), ("active", "VC-0", None))
            self.assertEqual(summary["same_root_cause_failures"][cause], 0)
            head = project_ledger.replay_head(fixture["ledger"])
            self.assertNotIn(campaign_id, head["terminal_campaigns"])
            self.assertEqual(project_ledger.campaign_resume_epoch(head, campaign_id), 1)
            self.assertEqual(head["root_cause_counts"][cause], 0)
            project_ledger.assert_campaign_admitted(campaign_dir, command="resume", require=True)
            # 同一批准重跑：幂等，不重复两本账。
            events_before = timing_ledger._load_events(fixture["timing_ledger"])
            again = codex_upgrade._campaign_resume_command(
                self._arguments(fixture, regression, approve=preview["review_sha256"])
            )
            self.assertEqual(again["status"], "resumed")
            self.assertEqual(len(timing_ledger._load_events(fixture["timing_ledger"])), len(events_before))
            self.assertEqual(project_ledger.replay_head(fixture["ledger"])["sequence"], head["sequence"])
            # 恢复后重新对账停线对象：可恢复，生成恢复预览。
            replay = reconciler.reconcile_attempt(campaign_dir, second)
            self.assertEqual(replay["status"], "recoverable", replay.get("decision"))
            self.assertIn("recovery_preview", replay)
            # 恢复后同根因再失败两次：第三批 B3-9 起同样只暂停（root_cause_repair），账本回到 stop_required、总账仍无终态，
            # 再次 campaign-resume 即可（恢复纪元 e1 的幂等键不吞掉新一轮）。
            third = self.helper._b0_orphan_attempt(fixture)
            self.assertEqual(reconciler.reconcile_attempt(campaign_dir, third)["status"], "recoverable")
            fourth = self.helper._b0_orphan_attempt(fixture)
            stopped_again = reconciler.reconcile_attempt(campaign_dir, fourth)
            self.assertEqual((stopped_again["status"], stopped_again["decision"]["pause_kinds"]), ("paused", ["root_cause_repair"]))
            self.assertEqual(timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], "stop_required")
            event_ids = [event["event_id"] for event, _raw in timing_ledger._load_events(fixture["timing_ledger"])]
            self.assertNotIn(f"reconcile-stop-the-line-{fourth}-e1", event_ids)
            head = project_ledger.replay_head(fixture["ledger"])
            self.assertNotIn(campaign_id, head["terminal_campaigns"])
            self.assertEqual(head["root_cause_counts"][cause], 2)

    def test_policy_evolution_moves_the_resume_policy_baseline(self) -> None:
        """第三批 R1：策略变化不再让 campaign-resume 无路可走——未登记策略演进时零写入拒绝并指向 tool-evolution，
        登记后以 Campaign 有效策略为基准，恢复预览照常生成。"""

        from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip
        from tools.official_client_capture import codex_upgrade_wire_transition as wt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, _second, _cause = self._stopped(root)
            campaign_dir = fixture["campaign_dir"]
            manifest = fixture["manifest"]
            frozen = manifest["tool_identity"]
            policy_file = tip.POLICY_FILENAME
            entries = [dict(e, sha256="9" * 64) if e["path"] == policy_file else dict(e) for e in frozen["entries"]]
            v2 = tip.compute_identity_v2(tip.load_policy(), Path(codex_upgrade.__file__).resolve().parent, entries)
            evolved = {
                **frozen,
                "entries": entries,
                "control_sha256": v2["control_sha256"],
                "policy_sha256": "9" * 64,
                "policy_version": int(frozen["policy_version"]) + 1,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
            }
            regression = self._regression(root)
            with mock.patch.object(codex_upgrade, "_tool_identity", mock.Mock(return_value=evolved)):
                self._fresh_deployment(fixture)
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "先以 tool-evolution 登记策略演进"):
                    codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
                payload = {
                    "schema_version": wt.EVOLUTION_SCHEMA,
                    "index": 1,
                    "campaign_id": manifest["campaign_id"],
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(campaign_dir / "campaign.json"),
                    "previous_evolution_sha256": None,
                    "from_summary": wt.identity_summary(frozen),
                    "to_summary": wt.identity_summary(evolved),
                    "to_identity": evolved,
                    "to_evaluator_digests": {},
                    "changes": {"paths_by_layer": {"control": [policy_file]}},
                    "impact": {
                        "official": {"planned_job_ids": [], "affected_job_ids": [], "sealed": False},
                        "candidates": {},
                        "inactive_candidates": [],
                    },
                    "evaluator": {"from": {}, "to": {}, "changed_fields": [], "candidates": {}},
                    "bindings": {"fix_commit": "f" * 40, "deployment_receipt": {"created_at_utc": "2026-09-27T00:00:00Z"}},
                    "policy_transition": {
                        "from_policy_sha256": frozen["policy_sha256"],
                        "from_policy_version": int(frozen["policy_version"]),
                        "to_policy_sha256": "9" * 64,
                        "to_policy_version": int(frozen["policy_version"]) + 1,
                        "compatibility_receipt": {"path": "/control/policy-compatibility.json", "sha256": "c" * 64},
                        "activation_certification": {"path": "/control/policy-activation.json", "sha256": "a" * 64},
                    },
                    "reason": "测试：策略演进",
                    "approved_sha256": "a" * 64,
                    "approved_by": "tester",
                    "approved_at_utc": "2026-09-27T00:00:01Z",
                }
                payload["receipt_sha256"] = wt._fingerprint(payload)
                wt.write_evolution(campaign_dir, manifest, payload)
                preview = codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            self.assertEqual(preview["status"], "approval_required")

    def test_half_applied_resume_blocks_reconciliation_until_reapplied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, second, _cause = self._stopped(root)
            regression = self._regression(root)
            self._fresh_deployment(fixture)
            preview = codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            approve = self._arguments(fixture, regression, approve=preview["review_sha256"])
            real_append = project_ledger.append_project_event

            def crash_on_resumed(root_path, **kwargs):
                if kwargs.get("event_type") == "campaign_resumed":
                    raise project_ledger.ProjectLedgerError("模拟在两本账之间崩溃")
                return real_append(root_path, **kwargs)

            with mock.patch.object(project_ledger, "append_project_event", side_effect=crash_on_resumed):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "以同一批准重跑即续接"):
                    codex_upgrade._campaign_resume_command(approve)
            # 半完成：计时账本已恢复、总账仍终态——对账零写入拒绝。
            self.assertEqual(timing_ledger.resume_epoch(fixture["timing_ledger"]), 1)
            events = timing_ledger._load_events(fixture["timing_ledger"])
            with self.assertRaisesRegex(reconciler.ReconcilerError, "campaign-resume 未完成"):
                reconciler.reconcile_attempt(fixture["campaign_dir"], second)
            self.assertEqual(len(timing_ledger._load_events(fixture["timing_ledger"])), len(events))
            # 以同一批准重跑补齐：计时账本已不在停线状态，不能重算预览，按批准收据冻结的预览续接。
            resumed = codex_upgrade._campaign_resume_command(approve)
            self.assertEqual((resumed["status"], resumed["resume_epoch"]), ("resumed", 1))
            self.assertEqual(timing_ledger.resume_epoch(fixture["timing_ledger"]), 1)
            self.assertEqual(reconciler.reconcile_attempt(fixture["campaign_dir"], second)["status"], "recoverable")

    def test_manual_close_and_non_stopped_campaigns_are_not_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self.helper._b0_fixture(root)
            regression = self._regression(root)
            self._fresh_deployment(fixture)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是停线"):
                codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            # 显式关账本（close-campaign-ledger 写的 close-stop-the-line）是人工决定，不可恢复。
            timing_ledger.append_event(
                fixture["timing_ledger"], event_id="close-stop-the-line", phase="VC-0", event_type="stop_the_line",
                root_cause_id="operator-close", next_action="人工决定关账本",
            )
            self.assertEqual(timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], "stopped")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "显式关账本"):
                codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))

    def test_tampered_resume_receipt_is_rejected_by_timing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, _second, _cause = self._stopped(root)
            regression = self._regression(root)
            self._fresh_deployment(fixture)
            preview = codex_upgrade._campaign_resume_command(self._arguments(fixture, regression))
            forged = dict(preview)
            forged["timing"] = dict(preview["timing"], cleared_root_cause_ids=[])
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有绑定当前账本 head、所恢复的停线或达上限根因"):
                codex_upgrade._apply_campaign_resume(
                    fixture["campaign_dir"], fixture["manifest"], forged, approved_by="老板", next_steps=""
                )
            self.assertEqual(timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], "stop_required")



class RequestBudgetExtensionTests(unittest.TestCase):
    """修好接着跑第 14 项：请求预算耗尽只暂停（不再写 deadline_live_requests 终态），批准延长后原对象续跑。"""

    def setUp(self) -> None:
        self.helper = test_codex_upgrade.CodexUpgradeTest("test_b0_reconcile_attempt_same_root_cause_limit_stops_the_line")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _fixture_with_budget(self, root: Path, budget: int) -> dict:
        # 先建带请求预算的项目总账，b0 夹具的安装器见到已有总账即原样复用。
        data = root / "data"
        data.mkdir(parents=True)
        data.chmod(0o700)
        project_ledger.create_project_ledger(
            data / project_ledger.LEDGER_DIR_NAME,
            project_id="fixture-project",
            absolute_deadline_utc=(datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            deadline_approved_by="fixture",
            estimation_policy="upper_bound_from_sibling_or_turn_ratio",
            estimation_policy_approved_by="fixture",
            fixture_only=False,
            live_request_budget=budget,
            formal_open_limit=64,
        )
        return self.helper._b0_fixture(root)

    def test_exhausted_budget_pauses_and_extension_resumes_same_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture_with_budget(root, 1)
            campaign_dir = fixture["campaign_dir"]
            campaign_id = str(fixture["manifest"]["campaign_id"])
            project_ledger.append_project_event(
                fixture["ledger"], operation_id="consume-budget", event_type="reconciliation_committed",
                payload={"campaign_id": campaign_id, "request": {"status": "resolved", "identity_keys": ["k-used"], "estimated_delta": 0, "estimated_sources": []}},
                source_batch_sha256=None,
            )
            self.assertEqual(project_ledger.replay_head(fixture["ledger"])["remaining_live_requests"], 0)
            attempt = self.helper._b0_orphan_attempt(fixture)
            paused = reconciler.reconcile_attempt(campaign_dir, attempt)
            self.assertEqual(paused["status"], "paused", paused.get("decision"))
            self.assertEqual(paused["decision"]["pause_kinds"], ["request_budget"])
            self.assertIn("request-budget-extend", paused["next_command"])
            head = project_ledger.replay_head(fixture["ledger"])
            self.assertNotIn(campaign_id, head["terminal_campaigns"])
            self.assertNotIn(timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], {"stopped", "stop_required"})
            # 新请求预算必须大于原有效预算与已消耗数。
            with self.assertRaisesRegex(project_ledger.ProjectLedgerError, "新请求预算必须大于"):
                project_ledger.preview_live_request_budget_extension(campaign_dir, new_budget=1, reason="补预算")
            preview = project_ledger.preview_live_request_budget_extension(campaign_dir, new_budget=5, reason="VC-5 续跑需要")
            self.assertEqual((preview["original_budget"], preview["consumed_at_preview"]), (1, 1))
            applied = project_ledger.apply_live_request_budget_extension(
                campaign_dir, preview_path=Path(preview["preview_path"]), approve_sha256=preview["review_sha256"], approved_by="老板"
            )
            self.assertEqual((applied["effective_live_request_budget"], applied["remaining_live_requests"]), (5, 4))
            again = project_ledger.apply_live_request_budget_extension(
                campaign_dir, preview_path=Path(preview["preview_path"]), approve_sha256=preview["review_sha256"], approved_by="老板"
            )
            self.assertEqual(again["project_event"], "duplicate")
            # 延长后同一对象重新对账：可恢复。
            self.assertEqual(reconciler.reconcile_attempt(campaign_dir, attempt)["status"], "recoverable")
            # 历史原因只供回放：新写 deadline_live_requests 终态被拒。
            with self.assertRaisesRegex(project_ledger.ProjectLedgerError, "仅供历史回放"):
                project_ledger.append_project_event(
                    fixture["ledger"], operation_id="terminal-budget", event_type="campaign_terminal",
                    payload={"campaign_id": campaign_id, "terminal_reason": "deadline_live_requests"}, source_batch_sha256=None,
                )

    def test_extension_rejects_unbudgeted_project_and_stale_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self.helper._b0_fixture(root)
            with self.assertRaisesRegex(project_ledger.ProjectLedgerError, "没有设请求预算"):
                project_ledger.preview_live_request_budget_extension(fixture["campaign_dir"], new_budget=5, reason="无预算")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture_with_budget(root, 3)
            preview = project_ledger.preview_live_request_budget_extension(fixture["campaign_dir"], new_budget=9, reason="补预算")
            project_ledger.append_project_event(
                fixture["ledger"], operation_id="concurrent", event_type="reconciliation_committed",
                payload={"campaign_id": "other", "request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0, "estimated_sources": []}},
                source_batch_sha256=None,
            )
            with self.assertRaisesRegex(project_ledger.ProjectLedgerError, "head 已过期"):
                project_ledger.apply_live_request_budget_extension(
                    fixture["campaign_dir"], preview_path=Path(preview["preview_path"]),
                    approve_sha256=preview["review_sha256"], approved_by="老板",
                )

if __name__ == "__main__":
    unittest.main()
