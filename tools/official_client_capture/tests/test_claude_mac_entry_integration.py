"""Mac 入口与批次的请求边界；全部材料仅存在私有夹具，禁止运行真实产品。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch as batch
from tools.official_client_capture import claude_upgrade_client_receipts as importer
from tools.official_client_capture import claude_upgrade_mac_client_capture as control
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_client_receipt_fixture as fixture

REAL_SOURCE_WRAPPER = claude_upgrade.verify_mac_source_v2


class MacEntryIntegrationTests(unittest.TestCase):
    def arguments(self, *extra: str):
        return claude_upgrade._build_parser().parse_args([
            "capture-candidate", "client-run", "--campaign-dir", "/unused/campaign",
            "--candidate-id", "candidate-1", "--attempt-id", "attempt-1",
            "--client", "claude-desktop", "--client-task", "/unused/task.json", *extra,
        ])

    def test_live_action_requires_parent_and_confirmation_before_context(self) -> None:
        with mock.patch.object(importer, "_context") as context:
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "父批次"):
                control.command(self.arguments("--acknowledge-live-requests"))
            with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}), self.assertRaisesRegex(ValueError, "确认真实请求"):
                control.command(self.arguments())
        context.assert_not_called()

    def test_live_action_rejects_offline_bundle_parameters_before_context(self) -> None:
        with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}), mock.patch.object(importer, "_context") as context:
            with self.assertRaisesRegex(ValueError, "离线"):
                control.command(self.arguments("--acknowledge-live-requests", "--bundle", "/unused/source"))
        context.assert_not_called()

    def test_static_source_wrapper_and_frozen_batch_use_real_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, fixture.source_scope():
            campaign = Path(temporary).resolve() / "staging/data/evidence/campaigns/campaign-1"
            attempt = campaign / "candidates/candidate-1/attempts/attempt-1"
            values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
            fixture.write(campaign / "campaign.json", {"campaign_id": "campaign-1", "client": "claude"})
            fixture.write(attempt / "attempt.json", values)
            task_path, bundle, capture = fixture.material(attempt, "claude-desktop", values)
            task = contract.read_json(task_path)
            self.assertEqual(REAL_SOURCE_WRAPPER(task, bundle, capture), source.verify_mac_source_v2(task, bundle, capture))
            with mock.patch.object(claude_upgrade, "_require_formal_campaign", return_value={"campaign_id": "campaign-1", "client": "claude"}):
                action = batch.mac_client_run_action(campaign, task_path)
            self.assertEqual(action["item_ids"], ["claude-desktop"])
            self.assertEqual(action["timeout_seconds"], task["execution"]["timeout_seconds"])
            parsed = claude_upgrade._build_parser().parse_args(batch._orchestrator_argv(action["command"]))
            self.assertEqual((parsed.capture_action, parsed.client_task), ("client-run", task_path))
            self.assertTrue(parsed.acknowledge_live_requests)
            self.assertEqual(batch.action_plan([action], phase="VC-5")["execute_item_ids"], ["claude-desktop"])
            parsed.campaign_dir = campaign
            with mock.patch.dict(os.environ, {supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}), \
                    mock.patch.dict(contract.SOURCE_ADAPTERS, {}, clear=True), \
                    mock.patch.object(importer, "_context", return_value=({}, attempt, values)), \
                    mock.patch.object(ledger, "runtime_admission_scope") as runtime:
                with self.assertRaisesRegex(ValueError, "尚未登记"):
                    control.command(parsed)
            runtime.assert_not_called()
            self.assertFalse((bundle.parent / "dispatch-intent.json").exists())


if __name__ == "__main__":
    unittest.main()
