"""revision 八场景的来源接线与显式入口专项；不执行完整故障链。

官方原件和门禁环境使用实际 fixture 生成器与原严格消费者。入口报告
使用明示的内存指标测试失败传播和未认证边界，不能据此登记链通过。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import contextlib

from tools.official_client_capture import certify_release as release
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_gate_receipt as gate
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_revision_evaluator_chain_driver as driver
from tools.official_client_capture.tests import claude_vc5_chain_fixture as sources
from tools.official_client_capture.tests import test_claude_vc5_gate_environment_fixture as gate_tests
from tools.official_client_capture.tests.real_chains import claude_revision_evaluator_scenarios as entry
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
from tools.official_client_control.errors import ControlError


class DeliveryResumeEntryTests(unittest.TestCase):
    """连续链消费产品生成的监督入口；旧直接动作和不完整入口仍须拒绝。"""

    def test_three_recoverable_states_use_supervised_batch_entry(self):
        from tools.official_client_capture import claude_upgrade_delivery as delivery
        from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
        campaign = Path("/fixture/campaign")
        context = SimpleNamespace(campaign_dir=campaign, manifest={})
        for expected, closed, pending in (("delivery_stage_failed", True, None),
                ("delivery_in_progress", False, None), ("delivery_invalidation_pending", True, {"id": "fixture"})):
            state = {"receipt_refs": [], "complete": False,
                     "pending_invalidation_ref": pending, "latest_closed": closed}
            with self.subTest(status=expected), mock.patch.object(delivery, "_resolve", return_value=context), \
                    mock.patch.object(delivery, "load_stage_result", return_value=None), \
                    mock.patch.object(delivery, "_deliveries", return_value=[{"delivery_id": "d1"}]), \
                    mock.patch.object(delivery, "_chain", return_value=state):
                value = delivery.delivery_status(campaign, "candidate-r2")
                self.assertEqual(value["status"], expected)
                audit = outer.ScenarioAudit(self, expected)
                audit.check_entry(value, *late.RUN_ENTRY_FRAGMENTS)
                self.assertTrue(audit.checks["entry"]["passed"])

    def test_old_direct_or_incomplete_entry_cannot_pass_resume_check(self):
        from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
        for hint in ("deliver-candidate run", "compile-and-run-vc-batch --phase VC-5", "compile-and-run-vc-batch --phase VC-6"):
            with self.subTest(hint=hint), self.assertRaises(AssertionError):
                outer.ScenarioAudit(self, "恢复入口拒绝").check_entry({"next_command": hint}, *late.RUN_ENTRY_FRAGMENTS)


class RevisionOfficialSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="revision-official-input-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = driver.ClaudeRevisionEvaluatorChainDriver("runTest")
        self.case.data = self.root / "data"
        self.campaign = self.case.data / "evidence/campaigns/revision-fixture"
        self.campaign.mkdir(parents=True)
        self.evaluation = self.campaign / "continuous-sources"
        self.side = evaluator.write_side(self.root / "original-evaluator-side", capture_id="revision-official")
        self.package = {"object_kind": "evidence_package", "sha256": "c" * 64}
        self.old_config = sources._write_json(self.evaluation / "source-config.json", {
            "official_source": str(self.evaluation / "original-source.json"),
            "capture_layout": {"host_runs_root": str(self.case.data / "runs"), "container_run_roots": ["/capture/runs"]},
        })

    def context(self) -> dict:
        world = {"campaign": self.campaign, "official": self.side, "evaluation": self.evaluation,
                 "config_path": self.old_config}
        self.case.runtime = SimpleNamespace(patched=lambda: contextlib.nullcontext())
        with mock.patch.object(driver.transient.world_fixture, "real_candidate_world", return_value=world), \
                mock.patch.object(driver.upgrade, "_load_stage_result", return_value={"store": {"evidence_package_ref": self.package}}):
            return self.case.evaluation_context(self.campaign)

    def closure(self, source: dict) -> list[dict]:
        roots = [{**source[name], "path": (self.campaign.relative_to(self.case.data) / source[name]["path"]).as_posix()}
                 for name in ("capture_manifest", "capture_index", "inventory")]
        channel = execution.VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64, "regression-fixture", self.case.data)
        return project_delivery_raw_bindings(channel, roots, gate_projection=None)

    def test_materialized_source_reaches_original_recursive_consumer_and_preserves_inputs(self) -> None:
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        world = self.context()
        config = json.loads(world["config_path"].read_bytes())
        source = json.loads(Path(config["official_source"]).read_bytes())
        closed = self.closure(source)
        self.assertEqual(len(closed), 17)
        self.assertEqual(config["gate_environment_mode"], sources.REAL_GATE_ENVIRONMENT)
        self.assertEqual(config["capture_layout"], json.loads(before[self.old_config])["capture_layout"])
        self.assertEqual(source["package_ref"], self.package)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        self.assertNotEqual(world["config_path"], self.old_config)
        original_reader = upgrade._capture_stage_attempt_context
        original_environment = arm.replay
        with sources.seams(world["config_path"]):
            self.assertIs(upgrade._capture_stage_attempt_context, original_reader)
            self.assertIs(arm.replay, original_environment)
        origin = json.loads((self.campaign / "seam/official/fixture-origin.json").read_bytes())
        self.assertTrue(origin["fixture_only"])
        self.assertFalse(origin["formal_version_semantics_certified"])

    def test_missing_materialized_parent_still_fails_in_original_consumer(self) -> None:
        world = self.context()
        config = json.loads(world["config_path"].read_bytes())
        source = json.loads(Path(config["official_source"]).read_bytes())
        (self.campaign / source["capture_manifest"]["path"]).unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.closure(source)

    def test_refreshed_world_audit_still_rejects_materialized_source_overwrite(self) -> None:
        world = self.context()
        world.update(trusted_roots=[self.side.evidence_root, self.side.manifest_path],
                     store=SimpleNamespace(root=self.root / "store"))
        self.case.ledger_dir, self.case.project_root = self.root / "timing", self.root / "project"
        # 此轻量用例只隔离账本统计输入；可信／历史快照与漂移拒绝全部调用原六项审计。
        with mock.patch.object(outer, "ledger_totals", return_value={}):
            audit = self.case.late_audit("物化来源不被改写", world)
        self.assertIn(world["config_path"], audit.trusted_paths)
        self.assertIn(self.campaign / "seam/official", audit.trusted_paths)
        path = self.campaign / "seam/official/capture-index.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(AssertionError, "可信产物被改写"):
            audit.check_trusted()
        self.assertEqual(world["trusted_roots"], [self.side.evidence_root, self.side.manifest_path])

    def test_attempt_replacement_input_is_rejected_before_materialization(self) -> None:
        for field in ("attempt", "attempt_root"):
            with self.subTest(field=field):
                config = json.loads(self.old_config.read_bytes())
                config[field] = "不能消费的候选替身"
                sources._write_json(self.old_config, config)
                with self.assertRaises(AssertionError):
                    self.context()
                self.assertFalse((self.campaign / "seam").exists())
                config.pop(field)
                sources._write_json(self.old_config, config)


class TransientOfficialSourceTests(RevisionOfficialSourceTests):
    """七场景与八场景共用来源准备，原件闭包和损坏拒绝逐条复验。"""

    def setUp(self):
        super().setUp()
        data = self.case.data
        self.case = driver.transient.ClaudeTransientChainDriver('runTest')
        self.case.data = data


class RevisionGateSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = gate_tests.GateEnvironmentFixtureTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = driver.ClaudeRevisionEvaluatorChainDriver("runTest")
        self.case.active_candidate = self.fixture.subject["candidate_id"]
        self.world = {"campaign": self.fixture.root / "campaign", "manifest": {},
                      "candidate_id": self.case.active_candidate, "build": {}, "candidate_payload": {},
                      "config_path": self.fixture.config, "gate_root": self.fixture.gate_root,
                      "real_candidate": True, "stage": {"store": {"validation_execution_plan_ref": {"sha256": "1" * 64}}},
                      "store": SimpleNamespace(load_object=lambda _reference: {"payload": {
                          "external_gates": [{"host_id": "ARM64", "architecture": "linux/arm64"}]}})}

    def create(self) -> str:
        # 这里只提供当前候选身份输入；原环境 reader、门禁 finalizer 和 replay 都不替换。
        with mock.patch.object(driver.compare, "candidate_gate_identity", return_value={}), \
                mock.patch.object(sources, "expected_subject", return_value=self.fixture.subject):
            return self.case.gate_receipt(self.world, "revision-receipt.json")

    def test_gate_consumes_actual_environment_contract_with_original_readers(self) -> None:
        original_replay, original_equivalence = arm.replay, arm.receipts_equivalent
        receipt = self.create()
        result = gate.replay(self.world["gate_root"], receipt)
        self.assertEqual(result["status"], "passed")
        self.assertIs(arm.replay, original_replay)
        self.assertIs(arm.receipts_equivalent, original_equivalence)
        before = self.fixture.environment_receipt(receipt, "before")
        environment = arm.replay(self.fixture.gate_root, before.relative_to(self.fixture.gate_root).as_posix())
        self.assertEqual(environment["phase"], "gate_before")
        (self.fixture.gate_root / environment["facts"]["path"]).unlink()
        with self.assertRaises(gate.GateReceiptError):
            gate.replay(self.fixture.gate_root, receipt)

    def test_legacy_mode_or_wrong_candidate_is_rejected_before_new_gate_files(self) -> None:
        sources._write_json(self.fixture.config, {})
        with self.assertRaisesRegex(AssertionError, "实读门禁环境"):
            self.create()
        self.assertFalse(self.fixture.gate_root.exists())
        sources._write_json(self.fixture.config, {"gate_environment_mode": sources.REAL_GATE_ENVIRONMENT})
        self.world["candidate_id"] = "已取代的旧候选"
        with self.assertRaises(AssertionError):
            self.create()
        self.assertFalse(self.fixture.gate_root.exists())


class PrivateRevisionEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="private-revision-entry-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clean = {"git": {"commit": "e" * 40, "status_porcelain": ""}, "files": {},
                      "formal_registry_keys": {"client_sources": [], "delivery_executors": [], "delivery_sources": []}}

    def synthetic_metrics(self) -> dict:
        """只用于报告消费回归；这些内存记录没有运行身份，不能提交 pre-A3。"""

        return {"returncode": 0, "upstream_requests": 0, "isolation": "degraded", "scenarios": [
            {"scenario": name, "campaign_id": "同一内存夹具Campaign",
             "checks": {key: {"passed": True} for key in outer.SIX_CHECKS}}
            for name in outer.REVISION_CONTINUOUS_SCENARIOS]}

    def execute(self, exercise) -> tuple[int, dict]:
        with mock.patch.object(entry, "source_snapshot", return_value=self.clean), \
                mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous", new=exercise):
            return entry.execute_private_chain(self.root / "new-run")

    def test_describe_never_executes_or_registers_private_chain(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as stream, mock.patch.object(entry, "execute_private_chain") as execute:
            self.assertEqual(entry.main(["--describe"]), 0)
        description = json.loads(stream.getvalue())
        self.assertEqual(description["scenarios"], list(outer.REVISION_CONTINUOUS_SCENARIOS))
        self.assertEqual(len(description["scenarios"]), 8)
        self.assertFalse(description["formal_certification"])
        execute.assert_not_called()
        self.assertNotIn("_run_pending_revision_continuous", unittest.defaultTestLoader.getTestCaseNames(outer.ClaudeFaultChainTests))
        if description["pending_formal_registration"]:
            self.assertNotIn(description["chain_id"], pre_a3.CLAUDE_REAL_CHAIN_IDS)
            self.assertNotIn(description["chain_id"], release.RELEASE_REQUIRED_CHAINS["claude"])
        else:
            self.assertIn(description["chain_id"], pre_a3.CLAUDE_REAL_CHAIN_IDS)
            self.assertIn(description["chain_id"], release.RELEASE_REQUIRED_CHAINS["claude"])

    def test_original_failure_is_preserved_and_propagated(self) -> None:
        def fail(case):
            case.real_chain_metrics = {"returncode": 1, "scenarios": []}
            raise AssertionError("轻量注入：vc4-plan-r2 拒绝")
        code, report = self.execute(fail)
        self.assertEqual((code, report["result"], report["failures"], report["skips"]), (1, "failed_or_incomplete", 1, 0))
        self.assertFalse(report["formal_certification"])
        self.assertIn("vc4-plan-r2 拒绝", (self.root / "new-run/首败诊断.txt").read_text())
        self.assertIn("FAILED", (self.root / "new-run/tests.log").read_text())

    def test_complete_local_report_remains_uncertified(self) -> None:
        metrics = self.synthetic_metrics()
        def exercise(case):
            case.real_chain_metrics = metrics
        code, report = self.execute(exercise)
        self.assertEqual((code, report["result"]), (0, "passed_local_fixture_chain"))
        self.assertTrue(report["eight_scenarios_six_checks_complete"])
        self.assertTrue(report["same_campaign"])
        self.assertFalse(report["formal_certification"])
        self.assertFalse(report["formal_registry_or_certification_written"])

    def test_incomplete_or_mixed_campaign_report_is_not_promoted(self) -> None:
        for fault in ("missing-check", "mixed-campaign", "malformed"):
            with self.subTest(fault=fault):
                metrics = self.synthetic_metrics()
                if fault == "missing-check":
                    del metrics["scenarios"][-1]["checks"]["history"]
                elif fault == "mixed-campaign":
                    metrics["scenarios"][-1]["campaign_id"] = "另一个Campaign"
                else:
                    metrics["scenarios"][-1] = None
                def exercise(case):
                    case.real_chain_metrics = metrics
                with mock.patch.object(entry, "source_snapshot", return_value=self.clean), \
                        mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous", new=exercise):
                    code, report = entry.execute_private_chain(self.root / fault)
                self.assertEqual((code, report["result"]), (1, "failed_or_incomplete"))

    def test_dirty_source_and_existing_evidence_reject_before_execution(self) -> None:
        dirty = {**self.clean, "git": {"commit": "e" * 40, "status_porcelain": " M checker.py\n"}}
        with mock.patch.object(entry, "source_snapshot", return_value=dirty), \
                mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous") as exercise:
            code, report = entry.execute_private_chain(self.root / "dirty-run")
        self.assertEqual((code, report["result"], report["tests_run"]), (1, "refused_dirty_source", 0))
        exercise.assert_not_called()
        existing = self.root / "old-evidence"
        existing.mkdir()
        sentinel = existing / "旧失败原件.txt"
        sentinel.write_bytes(b"immutable failure\n")
        with self.assertRaises(FileExistsError):
            entry.execute_private_chain(existing)
        self.assertEqual(sentinel.read_bytes(), b"immutable failure\n")


if __name__ == "__main__":
    unittest.main()
