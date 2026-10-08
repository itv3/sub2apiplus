"""Claude r2 后继：适配器顺序闭合、Codex 默认守恒及完整四证明的 history 分派正反例。"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_client_adapter as claude_adapter
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import upgrade_client_adapter as adapters
from tools.official_client_capture.tests import b4_reconciliation_fixtures as reconciliation_fixture
from tools.official_client_capture.tests import test_claude_upgrade_candidate as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_control_plane as control_tests
from tools.official_client_capture.tests import test_codex_upgrade_supervisor as supervisor_tests

REPOSITORY = Path(__file__).resolve().parents[3]
TOOL_ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "2654aeb90fbf10443428f338965c8be6ccfae72d"


class ProtocolOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = Path(temporary.name) / "campaign"
        self.campaign.mkdir(mode=0o700)
        self.campaign.joinpath("campaign.json").write_text(json.dumps({
            "schema_version": "official-client-upgrade-campaign/v1", "client": "claude",
        }), encoding="utf-8")
        self.campaign.joinpath("campaign.json").chmod(0o600)
        self.claude = control_tests._build(client="claude")
        self.codex = control_tests._build()

    def test_codex_preserves_original_tuple_object_names_and_function_order(self) -> None:
        source = subprocess.check_output([
            "git", "show", f"{BASE_COMMIT}:tools/official_client_capture/codex_upgrade_supervisor.py",
        ], cwd=REPOSITORY, text=True)
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                    and node.target.id == "_SUCCESSOR_PROTOCOLS")
        frozen = [(row.elts[0].value, row.elts[1].id) for row in node.value.elts]
        actual = supervisor._successor_protocols_for_run(self.codex, self.codex, campaign_dir=None)
        self.assertIs(actual, supervisor._SUCCESSOR_PROTOCOLS)
        self.assertEqual([(name, function.__name__) for name, function in actual], frozen)
        self.assertIsNone(adapters.adapter_for("codex").successor_protocol_order)
        recorder = (("recorder", object()),)
        with mock.patch.object(supervisor, "_SUCCESSOR_PROTOCOLS", recorder):
            self.assertIs(supervisor._successor_protocols_for_run(self.codex, self.codex, campaign_dir=None), recorder)

    def test_claude_only_moves_revision_before_environment_and_keeps_function_objects(self) -> None:
        actual = supervisor._successor_protocols_for_run(self.claude, self.claude, campaign_dir=self.campaign)
        default = dict(supervisor._SUCCESSOR_PROTOCOLS)
        names = tuple(name for name, _function in actual)
        self.assertEqual(names, adapters.adapter_for("claude").successor_protocol_order)
        self.assertLess(names.index("candidate_revision"), names.index("environment_redispatch"))
        self.assertEqual([name for name in names if name != "candidate_revision"],
                         [name for name in default if name != "candidate_revision"])
        for name, function in actual:
            self.assertIs(function, default[name])
        with self.assertRaises(AttributeError):
            adapters.adapter_for("claude").successor_protocol_order = names

    def test_bad_metadata_fails_before_any_protocol(self) -> None:
        names = claude_adapter.SUCCESSOR_PROTOCOL_ORDER
        bad_values = ([], "candidate_revision", (), names[:-1], names + (names[0],),
                      names[:-1] + (names[0],), names[:-1] + ("unknown",), names[:-1] + ([],))
        for value in bad_values:
            with self.subTest(metadata=value), mock.patch.object(claude_adapter, "SUCCESSOR_PROTOCOL_ORDER", value):
                with self.assertRaisesRegex(supervisor.SupervisorError, "完整无重复 tuple"):
                    supervisor._successor_protocols_for_run(self.claude, self.claude, campaign_dir=self.campaign)

    def test_owner_cross_client_and_unbound_explicit_order_are_rejected(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "同一客户端"):
            supervisor._successor_protocols_for_run(self.codex, self.claude, campaign_dir=self.campaign)
        with self.assertRaisesRegex(supervisor.SupervisorError, "拒绝派发"):
            supervisor._successor_protocols_for_run(self.codex, self.codex, campaign_dir=self.campaign)
        with self.assertRaisesRegex(supervisor.SupervisorError, "已核归属"):
            supervisor._successor_protocols_for_run(self.claude, self.claude, campaign_dir=None)


class RevisionHistoryTests(candidate_tests._CandidateCase):
    def _four_proof_history(self):
        """用真实 Claude 作废／双登记 API 和共享对账消费夹具闭合四证明，不执行采集或 builder。"""

        campaign = self.staged()
        old_candidate, new_candidate = "claude-cand-r1", "claude-cand-r2"
        opened = self.open_revision(campaign, old_candidate)
        manifest = json.loads((campaign / "campaign.json").read_text())
        ledger = self.root / "timing-ledger"
        for phase, event in (("VC-4", "stage_started"), ("VC-4", "stage_completed"), ("VC-5", "stage_started")):
            timing.append_event(ledger, event_id=f"protocol-fixture-{phase.lower()}-{event}", phase=phase,
                                event_type=event, next_action="协议边界夹具")
        checkpoint = {"path": "control/vc/vc-4-checkpoint.json", "sha256": "c" * 64,
                      "phase": "VC-4", "checkpoint_sha256": "d" * 64}
        common = {
            "campaign_id": manifest["campaign_id"],
            "campaign_plan_sha256": claude_upgrade.file_sha256(campaign / "campaign.json"),
            "original_deadline_at_utc": "2026-12-01T00:00:00Z", "reuse_items": [], "client": "claude",
        }
        prior = supervisor.build_batched_campaign_run_manifest(
            **common, batch_id="protocol-vc5-r1", batch_sequence=1, batch_sha256="1" * 64,
            phase="VC-5", predecessor_checkpoint=checkpoint, candidate_revision=1, candidate_id=old_candidate,
            execute_items=["assert-rules"], actions=[{
                "action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 60,
                "item_ids": ["assert-rules"], "command": [sys.executable, str(TOOL_ROOT / "claude_build_rule_assertion_results.py"),
                "--campaign-dir", str(campaign), "--candidate-id", old_candidate],
            }],
        )
        helper = supervisor_tests.SupervisorTests("test_b4_1_failed_parent_facts_classifies_terminal_kinds_and_locates_action")
        state, run_dir = helper._b4_failed_run(
            self.root, "protocol-failed-r1", campaign_id=manifest["campaign_id"], phase="VC-5",
            reason="action-failed:assert-rules",
            diagnostics=(("assert-rules", ("child-returncode", "ChildProcessError", "execution-failure")),),
        )
        state.update(started_at_epoch=1000.0, started_at_utc="1970-01-01T00:16:40.000Z")
        helper._write_json(run_dir / "state.json", state)
        timing.append_event(ledger, event_id="protocol-r1-recovery-required", phase="VC-5",
                            event_type="recovery_required", root_cause_id="campaign-run.action-failed",
                            next_action="reconcile-supervisor-run")
        receipt = reconciliation_fixture.bind_supervisor_run_reconciliation(campaign, run_dir, state, prior)
        self.assertEqual(supervisor.project_ledger.find_project_ledger(campaign), self.project_root)
        copy = ledger / "receipts" / "reconciliation" / run_dir.name / "reconciliation.json"
        copy.parent.mkdir(parents=True, mode=0o700)
        copy.write_bytes(receipt.read_bytes())
        copy.chmod(0o600)
        provenance = copy.with_name("provenance.json")
        helper._write_json(provenance, {"status": "complete"})
        timing.append_event(ledger, event_id=f"reconcile-run-passed-{run_dir.name}", phase="VC-5",
                            event_type="receipt_passed", next_action="redispatch-same-batch", receipts=[
                                {"role": "provenance", "path": provenance.relative_to(ledger).as_posix(), "sha256": timing._sha256_file(provenance)},
                                {"role": "reconciliation", "path": copy.relative_to(ledger).as_posix(), "sha256": timing._sha256_file(copy)},
                            ])
        invalidated = self.invalidate(campaign, old_candidate, candidate_source=self.source_tree())
        superseding = self.open_revision(campaign, new_candidate, supersedes=old_candidate)
        successor = supervisor.build_batched_campaign_run_manifest(
            **common, batch_id="protocol-vc4-r2", batch_sequence=2, batch_sha256="2" * 64,
            phase="VC-4", predecessor_checkpoint={**checkpoint, "phase": "VC-3", "path": "control/vc/vc-3-checkpoint.json"},
            candidate_revision=2, candidate_id=new_candidate, execute_items=["vc4-plan-r2"], actions=[{
                "action_id": "vc4-plan-r2", "operation": "VC-4:plan-candidate-gates", "timeout_seconds": 60,
                "item_ids": ["vc4-plan-r2"], "command": [sys.executable, str(TOOL_ROOT / "claude_upgrade.py"),
                "plan-candidate-gates", "--campaign-dir", str(campaign), "--candidate-id", new_candidate],
            }],
        )
        return campaign, state, prior, run_dir, successor, receipt, opened, invalidated, superseding

    def test_real_four_proofs_route_to_revision_and_invalid_proof_never_falls_through(self) -> None:
        campaign, state, prior, run_dir, successor, receipt, opened, invalidated, superseding = self._four_proof_history()
        history = [(state, prior, run_dir)]
        def validate_history():
            # 此层回归只隔离后继分派；合成批次不取得真实 staging 执行权，也不签完整升级链通过。
            return supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign, staging_model=False)

        self.assertTrue(supervisor._validate_candidate_revision_successor(state, prior, run_dir, successor, campaign_dir=campaign))
        original_order = tuple(name for name, _protocol in supervisor._SUCCESSOR_PROTOCOLS)
        with mock.patch.object(claude_adapter, "SUCCESSOR_PROTOCOL_ORDER", original_order):
            with self.assertRaisesRegex(supervisor.SupervisorError, "环境前提失败对账后只允许原批次内容重派"):
                validate_history()
        self.assertEqual(validate_history(), history)

        same_batch = {**prior, "batch_id": "protocol-vc5-r1-retry", "batch_sequence": 2, "batch_sha256": "3" * 64}
        self.assertFalse(supervisor._validate_candidate_revision_successor(state, prior, run_dir, same_batch, campaign_dir=campaign))
        self.assertEqual(supervisor._validate_batched_campaign_history(same_batch, history, campaign_dir=campaign, staging_model=False), history)
        malformed_successor = {**successor, "candidate_revision": 1, "candidate_id": prior["candidate_id"]}
        self.assertFalse(supervisor._validate_candidate_revision_successor(state, prior, run_dir, malformed_successor, campaign_dir=campaign))
        with self.assertRaisesRegex(supervisor.SupervisorError, "只允许原批次内容重派"):
            supervisor._validate_batched_campaign_history(malformed_successor, history, campaign_dir=campaign, staging_model=False)

        paths = [receipt, campaign / "candidates" / "claude-cand-r1" / "invalidation.json",
                 campaign / "control" / "vc" / "revisions" / "r2" / "revision.json",
                 campaign / "control" / "vc" / "revisions" / "r2" / "COMMIT"]
        originals = {path: path.read_bytes() for path in paths}
        environment = mock.Mock(wraps=supervisor._validate_batched_environment_redispatch_successor)
        observed = tuple((name, environment if name == "environment_redispatch" else function)
                         for name, function in supervisor._SUCCESSOR_PROTOCOLS)
        negative_results = []
        with mock.patch.object(supervisor, "_SUCCESSOR_PROTOCOLS", observed):
            for path in paths:
                with self.subTest(proof=path.name):
                    path.write_bytes(b"{}\n")
                    try:
                        with self.assertRaises(supervisor.SupervisorError) as failure:
                            validate_history()
                        negative_results.append({"proof": str(path.relative_to(campaign)), "error": str(failure.exception)})
                        environment.assert_not_called()
                    finally:
                        path.write_bytes(originals[path])
            invalidation_path = paths[1]
            payload = json.loads(originals[invalidation_path])
            resigned = artifacts.build_candidate_invalidation(
                campaign_id=payload["campaign_id"], candidate_id=payload["candidate_id"], revision=payload["revision"],
                diagnosis=payload["diagnosis"], recorded_at_utc="2099-01-01T00:00:00Z",
            )
            invalidation_path.write_text(json.dumps(resigned), encoding="utf-8")
            try:
                with self.assertRaisesRegex(supervisor.SupervisorError, "项目总账绑定不一致") as failure:
                    validate_history()
                negative_results.append({"proof": "合法重签作废收据但旧总账摘要", "error": str(failure.exception)})
                environment.assert_not_called()
            finally:
                invalidation_path.write_bytes(originals[invalidation_path])
        self.assertEqual(validate_history(), history)
        destination = os.environ.get("CLAUDE_REVISION_PROTOCOL_TEST_EVIDENCE")
        if destination:
            output = Path(destination).resolve()
            output.mkdir(parents=True, exist_ok=True)
            for path in [campaign / "campaign.json", *paths]:
                target = output / "campaign" / path.relative_to(campaign)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            shutil.copytree(run_dir, output / "failed-run", dirs_exist_ok=True)
            shutil.copytree(self.root / "timing-ledger", output / "timing-ledger", dirs_exist_ok=True)
            shutil.copytree(self.project_root, output / "project-ledger", dirs_exist_ok=True)
            shutil.copytree(campaign / "control" / "vc" / "revisions", output / "campaign" / "control" / "vc" / "revisions", dirs_exist_ok=True)
            shutil.copytree(claude_upgrade_store.campaign_store_root(campaign), output / "store", dirs_exist_ok=True)
            result = {"scope": "离线四证明 history 分派；非 staging 派发或完整升级链认证",
                      "prior_manifest": prior, "successor_manifest": successor, "r1": opened,
                      "invalidation": invalidated, "r2": superseding, "negative_results": negative_results,
                      "history_passed": True, "live_request_count": 0}
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
