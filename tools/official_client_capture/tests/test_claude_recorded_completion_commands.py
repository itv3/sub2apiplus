"""录制后段命令的必要短检：真实离线导入、原入账消费者与误派零写入拒绝。

来源夹具只提供离线观察和受管输入；CLI、客户端收据、provenance 和项目账本使用原产品实现。
本模块不运行 ARM64 长链，也不签发完整链或版本语义认证。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_upgrade_batch as batch
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_client_receipt_fixture as clients
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as driver
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as vc1
from tools.official_client_capture.tests import test_claude_upgrade_candidate_seal_evidence as candidate_fixture
from tools.official_client_capture.tests import test_claude_upgrade_classify as cli_fixture
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_fixture
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_fixture


class BatchCommandBoundaryTests(unittest.TestCase):
    """无需 Campaign 即重现产品拒绝；助手必须先拒绝，再考虑读 Campaign 和写计划。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve()
        self.campaign = self.data / "missing-campaign"

    def test_product_control_guard_and_helper_reject_before_any_write(self) -> None:
        for command in sorted(batch.CONTROL_PLANE_COMMANDS):
            with self.subTest(command=command):
                with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
                    with self.assertRaisesRegex(batch.ConfigurationError, "禁止由 campaign-run 动作派发"):
                        batch.reject_control_plane_in_campaign_run(command)
                for explicit in (False, True):
                    action = batch.managed_action("control", "VC-5:control", [command], item_ids=["control"]) if explicit else None
                    with self.assertRaisesRegex(replay.ReplayError, "禁止由 campaign-run 动作派发"):
                        vc1._batch(self.data, self.campaign, "control", [] if explicit else [command], phase="VC-5", action=action)
                self.assertEqual(list(self.data.iterdir()), [])

    def test_real_cli_and_helper_reject_nested_mac_import_without_writes(self) -> None:
        for client in contract.CLIENTS:
            argv = ["capture-candidate", "client-evidence", "import", "--campaign-dir", str(self.campaign),
                    "--candidate-id", "candidate-fixture", "--attempt-id", "attempt-fixture", "--client", client,
                    "--client-task", str(self.data / "task.json"), "--bundle", str(self.data / "bundle")]
            with self.subTest(client=client), mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
                code, result, stderr = cli_fixture.run_main(argv)
                self.assertEqual((code, result), (1, None))
                self.assertIn("client-evidence 是批次之间的离线控制入口", stderr)
                with self.assertRaisesRegex(replay.ReplayError, "批次之间的离线控制入口"):
                    vc1._batch(self.data, self.campaign, "client", argv, phase="VC-5")
                action = batch.managed_action("client", "VC-5:client", argv, item_ids=["client"])
                with self.assertRaisesRegex(replay.ReplayError, "批次之间的离线控制入口"):
                    vc1._batch(self.data, self.campaign, "client", [], phase="VC-5", action=action)
                self.assertEqual(os.environ[supervisor.CAMPAIGN_RUN_CONTEXT_ENV], "1")
                self.assertEqual(list(self.data.iterdir()), [])

    def test_legal_managed_and_external_builder_commands_are_not_rejected(self) -> None:
        from tools.official_client_capture import claude_upgrade_delivery as delivery

        for command in ("classify", "compare", "accept", "capture-candidate"):
            argv = [command, "run"] if command == "capture-candidate" else [command]
            vc1._validate_batch_command(batch.managed_action("legal", "VC-5:legal", argv, item_ids=["legal"]))
        external = {"command": [sys.executable, str(Path(__file__).resolve().parents[1] / "claude_rule_assertion.py"), "assert-rules"]}
        vc1._validate_batch_command(external)
        vc1._validate_batch_command(delivery.run_action(self.campaign, "candidate-fixture", environment_executor="fixture-executor"))
        vc1._validate_batch_command(delivery.finalize_action(self.campaign, "candidate-fixture", "a" * 64))
        self.assertEqual(list(self.data.iterdir()), [])

        # 在原短验 UID 内覆盖承接声明，不增加公共发现集或启动父监督进程。
        for check in (self._check_sealed_actions_reuse, self._check_nonsealed_reuse,
                      self._check_capture_item_mismatch, self._check_invalid_sealed_attempt):
            with self.subTest(boundary=check.__name__):
                check()

    def _check_sealed_actions_reuse(self) -> None:
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_candidate_seal as seal
        from tools.official_client_capture.tests import test_codex_upgrade_supervisor as supervisor_fixture

        fixture = supervisor_fixture.SupervisorTests(methodName="runTest")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, _ledger, inner, attempt_root = fixture._post_run_tooling_fixture(root)
            data = campaign.parents[2]
            attempt = source.read(attempt_root / "attempt.json")
            original = source.binding(attempt_root / "attempt.json")
            expected_jobs = sorted(item["id"] for item in attempt["results"])
            self.assertNotEqual(len(expected_jobs), 12, "本例必须能证明没有硬编码完整链的十二个 Job")
            builder = {"script": "tools/official_client_capture/claude_build_rule_assertion_results.py",
                       "config": {"path": "comparisons/cand-1/builder-config.json"},
                       "output": "assertions/cand-1/results.json", "results_dir": "assertions/cand-1/rules",
                       "evaluation_baseline": 0}
            actions = {
                "compare": batch.managed_action("compare", "VC-5:compare",
                    ["compare", "--campaign-dir", str(campaign), "--candidate-id", "cand-1"], item_ids=["compare"]),
                "acceptance": batch.managed_action("acceptance", "VC-5:acceptance",
                    ["accept", "--campaign-dir", str(campaign), "--candidate-id", "cand-1",
                     "--external-gate-root", str(data / "gate"), "--external-gate-receipt", "receipt.json"], item_ids=["acceptance"]),
                "assert-rules": {"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 900.0,
                    "command": [sys.executable, str(data / builder["script"]), "--config", str(campaign / builder["config"]["path"]),
                        "--output", str(campaign / builder["output"]), "--results-dir", str(campaign / builder["results_dir"]),
                        "--evaluation-baseline", "0", "--reuse-authority", "none"], "item_ids": ["assert-rules"]},
            }
            def consume(arguments):
                plan = source.read(arguments.action_plan)
                self.assertEqual(plan["reuse_item_ids"], expected_jobs)
                checked = supervisor.post_run_tooling_facts(campaign, {
                    **inner, "actions": plan["actions"], "execute_items": plan["execute_item_ids"],
                    "reuse_items": plan["reuse_item_ids"],
                }, run_started_at_utc="2099-01-01T00:00:00Z")
                self.assertTrue(checked["qualifies"], checked["reasons"])
                return {"fixture_only": True}, 0
            with mock.patch.object(upgrade, "load_campaign_manifest", return_value={}), \
                    mock.patch.object(upgrade, "_current_candidate_revision", return_value=1), \
                    mock.patch.object(upgrade, "_current_candidate_revision_record", return_value=(1, {"candidate_id": "cand-1"})), \
                    mock.patch.object(upgrade, "_vc_checkpoint_path", return_value=campaign / "vc4.json"), \
                    mock.patch.object(upgrade, "_vc_campaign_plan", return_value={}), \
                    mock.patch.object(upgrade, "_replay_vc_checkpoint"), \
                    mock.patch.object(upgrade, "_committed_vc_sequences", return_value=[]), \
                    mock.patch.object(upgrade, "_load_stage_result", return_value={"builder": builder}), \
                    mock.patch.object(seal, "load_round_one_result", return_value=(campaign / "result.json", {"fixture_only": True})), \
                    mock.patch.object(seal, "sealed_attempt_context", return_value=(attempt_root, attempt)), \
                    mock.patch.object(upgrade, "compile_and_run_vc_batch", side_effect=consume) as compiled:
                for name, action in actions.items():
                    with self.subTest(name=name):
                        vc1._batch(data, campaign, name, [], phase="VC-5", action=action)
                self.assertEqual(compiled.call_count, 3)
            self.assertEqual(source.binding(attempt_root / "attempt.json"), original)

    def _check_nonsealed_reuse(self) -> None:
        from tools.official_client_capture import claude_upgrade as upgrade

        with mock.patch.object(upgrade, "_current_candidate_revision_record") as current:
            for phase, name in (("VC-1", "compare"), ("VC-6", "acceptance"),
                                ("VC-5", "vc5-01-candidate-run"), ("VC-5", "candidate-seal"),
                                ("VC-5", "vc5-02-candidate-seal-preview")):
                with self.subTest(phase=phase, name=name):
                    self.assertEqual(vc1._sealed_candidate_reuse(Path("/unused"), Path("/unused"), phase, name, {}, {}), [])
            current.assert_not_called()

    def _check_capture_item_mismatch(self) -> None:
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_candidate_seal as seal

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory).resolve()
            with mock.patch.object(upgrade, "_current_candidate_revision_record", return_value=(1, {"candidate_id": "cand-1"})), \
                    mock.patch.object(seal, "load_round_one_result") as sealed:
                for name in ("compare", "acceptance"):
                    action = batch.managed_action(name, "VC-5:" + name,
                        ["capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", "cand-1"], item_ids=[name])
                    with self.subTest(name=name), self.assertRaises((replay.ReplayError, batch.ConfigurationError)):
                        vc1._sealed_candidate_reuse(campaign, campaign, "VC-5", name, action, {})
                sealed.assert_not_called()
            self.assertEqual(list(campaign.iterdir()), [])

    def _check_invalid_sealed_attempt(self) -> None:
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_candidate_seal as seal

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory).resolve()
            action = batch.managed_action("compare", "VC-5:compare",
                ["compare", "--campaign-dir", str(campaign), "--candidate-id", "cand-1"], item_ids=["compare"])
            with mock.patch.object(upgrade, "_current_candidate_revision_record", return_value=(1, {"candidate_id": "cand-1"})), \
                    mock.patch.object(seal, "load_round_one_result", return_value=(campaign / "result.json", {})) as sealed, \
                    mock.patch.object(seal, "sealed_attempt_context") as context:
                for results in ([], [{"id": "job-a", "status": "failed"}],
                                [{"id": "job-a", "status": "complete"}] * 2):
                    context.return_value = (campaign, {"status": "awaiting_receipts", "results": results})
                    with self.subTest(results=results), self.assertRaises(replay.ReplayError):
                        vc1._sealed_candidate_reuse(campaign, campaign, "VC-5", "compare", action, {})
                sealed.side_effect = batch.ConfigurationError("阶段尚未封存")
                with self.assertRaisesRegex(batch.ConfigurationError, "阶段尚未封存"):
                    vc1._sealed_candidate_reuse(campaign, campaign, "VC-5", "compare", action, {})
            self.assertEqual(list(campaign.iterdir()), [])


class RealOfflineImportTests(candidate_fixture._CandidateSealCase):
    """真实冻结 Campaign 输入源；只替换驱动的候选常量，不替换 CLI 或成功判定。"""

    def setUp(self) -> None:
        super().setUp()
        self.attempt = self.campaign / "candidates" / candidate_fixture.CANDIDATE / "attempts" / candidate_fixture.ATTEMPT
        self.values = {"campaign_id": self.manifest["campaign_id"], "candidate_id": candidate_fixture.CANDIDATE,
                       "attempt_id": candidate_fixture.ATTEMPT, "run_nonce": candidate_fixture.RUN_NONCE}
        clients.write(self.attempt / "attempt.json", {**self.values, "status": "awaiting_receipts", "evidence_roots": [],
                                                       "completed_at_utc": clients.NOW})
        # 在构造新输入世界时补齐最小夹具；正式驱动调用之后禁止再改变 attempt 原字节。
        for client in contract.CLIENTS:
            clients.candidate_inputs(self.attempt, self.values, client)
        self.closeout_snapshot = {}
        self.project = clients.fixture_ledger(self.attempt)

    def test_both_clients_use_real_offline_cli_and_reimport_is_accounting_idempotent(self) -> None:
        original = source.binding(self.attempt / "attempt.json")
        with self.seal_patched(), mock.patch.object(driver, "CANDIDATE", candidate_fixture.CANDIDATE):
            result = driver._client_inputs(self.data, self.campaign, self.attempt, source.read(self.attempt / "attempt.json"))
            head = project.replay_head(self.project)
            self.assertNotIn("batches", result)
            self.assertEqual({item["client"] for item in result["offline_imports"]}, set(contract.CLIENTS))
            self.assertEqual(head["precise_total"], 2)
            for imported in result["offline_imports"]:
                contract.verify_client_receipt(self.attempt, imported["client"], Path(imported["receipt"]["path"]))
                code, again, _stderr = driver._direct(imported["argv"])
                self.assertEqual(code, 0)
                self.assertEqual(again, imported["result"])
                source.bound(imported["receipt"], root=self.attempt)
            self.assertEqual(result["seal_arguments"], [value for item in result["offline_imports"]
                              for value in ("--client-evidence", item["client"] + "=" + item["result"]["receipt"])])
            self.assertEqual(project.replay_head(self.project), head)
            self.assertEqual(source.binding(self.attempt / "attempt.json"), original)
            self.assertFalse(source.read(Path(result["origin"]["path"]))["formal_version_semantics_certified"])


class SealedProjectClosureTests(recovery_fixture.RecoveryChainCase):
    """真实 run→seal 之后，缺账、部分账与入账操作缺失都不能误报完整闭合。"""

    def test_real_seal_partial_accounting_and_original_account_command_close_all_keys(self) -> None:
        campaign = self.build_recovery_campaign()
        campaign_id = source.read(campaign / "campaign.json")["campaign_id"]
        first = self.capture_run(campaign)
        with self.chain_patched():
            code, preview, stderr = seal_fixture.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
            code, sealed, stderr = seal_fixture.run_main(self.seal_argv(campaign, first["attempt_id"],
                                                       "--approve-seal-sha256", preview["review_sha256"]))
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)
            operation = "account-sealed-official:" + first["attempt_id"]
            empty = project.replay_head(self.project_root)
            with self.assertRaisesRegex(replay.ReplayError, "项目总账未全部闭合"):
                driver._verify_project_accounting(campaign, self.project_root, [operation])
            self.assertEqual(project.replay_head(self.project_root), empty)
            receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign_id)
            keys = sorted(item["identity_key"] for item in receipt["requests"])
            self.assertGreater(len(keys), 1)
            # 用原账本消费者写一条真实来源键的部分账，不伪造 replay_head 或闭合结果。
            project.append_project_event(self.project_root, operation_id="fixture-partial-sealed-accounting",
                event_type="reconciliation_committed", source_batch_sha256=None,
                payload={"campaign_id": campaign_id, "request": {"status": "resolved", "identity_keys": keys[:1], "estimated_delta": 0}})
            partial = project.replay_head(self.project_root)
            self.assertEqual(partial["precise_total"], 1)
            with self.assertRaisesRegex(replay.ReplayError, "项目总账未全部闭合"):
                driver._verify_project_accounting(campaign, self.project_root, [operation])
            self.assertEqual(project.replay_head(self.project_root), partial)
            accounted = driver._account_sealed(self.data, campaign, "official")
            closure = driver._verify_project_accounting(campaign, self.project_root, [accounted["operation_id"]])
            self.assertEqual(closure["precise_identity_keys"], keys)
            self.assertEqual(closure["project_head"]["precise_total"], len(keys))
            with self.assertRaisesRegex(replay.ReplayError, "缺少成功官方或候选封存的原入账操作"):
                driver._verify_project_accounting(campaign, self.project_root, [operation, "account-sealed-candidate:missing:missing"])
            code, again, _stderr = driver._direct(["account-sealed-official", "--campaign-dir", str(campaign)])
            self.assertEqual((code, again["status"]), (0, "accounted"))
            self.assertEqual(project.replay_head(self.project_root), closure["project_head"])
            source.bound(accounted["receipt"], root=self.data)
            for original in accounted["originals"].values():
                source.bound(original, root=campaign)


class RealCandidateAccountingTests(candidate_fixture._CandidateSealCase):
    """候选成功 seal 的原 CLI 入账分支也实际执行，不拿官方分支通过代替它。"""

    def test_real_candidate_seal_and_account_command_preserve_originals(self) -> None:
        attempt = self.write_evidence_and_attempt()
        values = {"campaign_id": self.manifest["campaign_id"], "candidate_id": candidate_fixture.CANDIDATE,
                  "attempt_id": candidate_fixture.ATTEMPT, "run_nonce": candidate_fixture.RUN_NONCE}
        for client in contract.CLIENTS:
            clients.candidate_inputs(attempt, values, client)
        with self.seal_patched(), mock.patch.object(driver, "CANDIDATE", candidate_fixture.CANDIDATE):
            imported = driver._client_inputs(self.data, self.campaign, attempt, source.read(attempt / "attempt.json"))
            code, preview, stderr = self.run_seal(*imported["seal_arguments"])
            self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), (preview, stderr))
            code, sealed, stderr = self.run_seal("--approve-seal-sha256", preview["review_sha256"], "--approved-by", "reviewer-a", *imported["seal_arguments"])
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)
            accounted = driver._account_sealed(self.data, self.campaign, "candidate")
            self.assertEqual((accounted["result"]["phase"], accounted["result"]["candidate_id"], accounted["result"]["attempt_id"]),
                             ("candidate", candidate_fixture.CANDIDATE, candidate_fixture.ATTEMPT))
            root = project.find_project_ledger(self.campaign, client="claude")
            before = project.replay_head(root)
            self.assertEqual(before["operations"][accounted["operation_id"]]["event_type"], "reconciliation_committed")
            receipt = provenance.collect_campaign_provenance(self.campaign, formal_campaign_id=self.manifest["campaign_id"])
            keys = {row["identity_key"] for row in receipt["requests"]}
            self.assertEqual(set(before["accounted_identity_index"]), keys)
            self.assertEqual((before["precise_total"], before["estimated_total"]), (len(keys), 0))
            code, again, _stderr = driver._direct(["account-sealed-candidate", "--campaign-dir", str(self.campaign),
                                                  "--candidate-id", candidate_fixture.CANDIDATE])
            self.assertEqual((code, again["status"]), (0, "accounted"))
            self.assertEqual(project.replay_head(root), before)
            for original in accounted["originals"].values():
                source.bound(original, root=self.campaign)


if __name__ == "__main__":
    unittest.main()
