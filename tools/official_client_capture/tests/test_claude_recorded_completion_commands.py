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
