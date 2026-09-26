"""R4：阶段审核不能绕过半成品、永久条件、预约分流或 write-once 收口。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests
from tools.official_client_capture.tests import test_codex_upgrade_supervisor as supervisor_tests
from tools.official_client_capture.tests import test_codex_upgrade_timing_ledger as timing_tests


class StageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = upgrade_tests.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_unknown_partial_action_remains_review_after_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self.case._vc_chain_fixture(root)
            campaign = fixture["campaign_dir"]
            plan = self.case._vc_chain_action_plan(root, campaign, "VC-2", fail=True)
            failed, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-2", 2, plan))
            self.assertEqual(code, 1)
            result = reconciler.reconcile_supervisor_run(Path(failed["campaign_run"]["run_dir"]), campaign)
            self.assertEqual(result["status"], "stage_review_required")
            self.assertFalse(result["stage_replay"]["allowed"])
            self.assertEqual(timing.inspect_ledger(fixture["timing_ledger"])["status"], "stage_review_required")
            with self.assertRaisesRegex(upgrade.ConfigurationError, "stage_review_required"):
                upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-2", 3, plan))

    def test_catalog_partial_directory_has_no_replay_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign = root / "campaign"
            campaign.mkdir()
            output = root / "catalog"
            output.mkdir()
            (output / "partial.json").write_text("{}")
            manifest = {"phase": "VC-3", "campaign_id": "campaign", "actions": [{
                "action_id": "stage", "command": [sys.executable, str(Path(upgrade.__file__).resolve()),
                    "stage-profile", "--campaign-dir", str(campaign), "--output", str(output)]}]}
            result = upgrade._campaign_stage_replay_facts(campaign, manifest)
            self.assertFalse(result["allowed"])
            self.assertTrue(result["reasons"])

    def test_vc1_reservation_uses_attempt_preview_and_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.case._b0_fixture(Path(directory).resolve())
            campaign, ledger = fixture["campaign_dir"], fixture["timing_ledger"]
            timing.append_event(ledger, event_id="vc0-done", phase="VC-0", event_type="stage_completed")
            timing.append_event(ledger, event_id="vc1-start", phase="VC-1", event_type="stage_started")
            run = self.case._b0_run_dir(fixture, "r4-reserved")
            attempt = self.case._b0_orphan_attempt(fixture)
            for event_type in ("stage_abandoned", "stage_review_required"):
                timing.append_event(ledger, event_id=event_type, phase="VC-1", event_type=event_type,
                                    root_cause_id="reservation-interrupted", next_action="reconcile-attempt")
            with self.assertRaisesRegex(reconciler.ReconcilerError, "改用 reconcile-attempt"):
                reconciler.reconcile_supervisor_run(run, campaign)
            result = reconciler.reconcile_attempt(campaign, attempt)
            self.assertEqual(result["status"], "recoverable", result)
            preview = Path(result["recovery_preview_path"])
            with self.assertRaisesRegex(reconciler.ReconcilerError, "尚未批准"):
                reconciler.load_approved_recovery_preview(campaign, preview, phase="official", candidate_id=None)
            reconciler.approve_recovery_preview(campaign, attempt, approve_sha256=result["recovery_preview"]["review_sha256"])
            approved = reconciler.load_approved_recovery_preview(campaign, preview, phase="official", candidate_id=None)
            self.assertEqual(approved["execute_job_ids"], ["official-test"])
            state = timing.inspect_ledger(ledger)
            self.assertEqual((state["status"], state["active_phase"]), ("active", "VC-1"))
            self.assertEqual(state["total_live_request_count"], 0)

    def test_sigkill_between_abandon_and_review_appends_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign, ledger, manifest = supervisor_tests.SupervisorTests()._timing_closeout_fixture(root)
            script = '''import json,os,signal,sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade_supervisor as s
original=s.timing_ledger.append_event
def append(*args,**kwargs):
    if kwargs.get("event_type")=="stage_review_required": os.kill(os.getpid(),signal.SIGKILL)
    return original(*args,**kwargs)
s.timing_ledger.append_event=append
s._close_failed_campaign_timing_ledger(Path(sys.argv[1]),json.loads(sys.argv[2]),failed_action_id="failing-action")
'''
            completed = subprocess.run([sys.executable, "-c", script, str(campaign), json.dumps(manifest)],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, -signal.SIGKILL, completed.stderr)
            originals = {p: p.read_bytes() for p in (ledger / "events").glob("*.json")}
            for _ in range(2):
                supervisor._close_failed_campaign_timing_ledger(campaign, manifest, failed_action_id="failing-action")
            events = [event for event, _ in timing._load_events(ledger)]
            self.assertEqual(sum(e["event_type"] == "stage_review_required" for e in events), 1)
            self.assertEqual(sum(e["event_type"] == "stage_abandoned" for e in events), 1)
            self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_integrity_and_existing_stop_required_still_stop(self):
        for failure in ("evidence-integrity", "stop_required"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                root.chmod(0o700)
                campaign, ledger, manifest = supervisor_tests.SupervisorTests()._timing_closeout_fixture(root)
                if failure == "stop_required":
                    for index in range(2):
                        timing.append_event(ledger, event_id=f"start-{index}", phase="VC-1", event_type="attempt_started", attempt_id=f"attempt-{index}")
                        timing.append_event(ledger, event_id=f"failed-{index}", phase="VC-1", event_type="attempt_failed", attempt_id=f"attempt-{index}", root_cause_id="same-cause")
                result = supervisor._close_failed_campaign_timing_ledger(campaign, manifest, failed_action_id="failing-action",
                    failure_class="evidence-integrity" if failure == "evidence-integrity" else "execution-failure")
                self.assertEqual(result["ledger_status"], "stopped")

    def test_legacy_checkpoint_without_review_fields_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ledger"
            helper = timing_tests.TimingLedgerTests()
            helper._create(root)
            receipt = timing.build_checkpoint(root, observed_at_utc=helper._at(1))
            receipt["summary"].pop("review_phase")
            receipt["summary"].pop("review_root_cause_id")
            timing._write_once(root / "receipts" / "legacy.json", receipt)
            self.assertEqual(timing.replay(root, "receipts/legacy.json"), receipt)


if __name__ == "__main__":
    unittest.main()
