"""R13-3：六类历史故障的独立复现夹具。

初始断言固定改造前的拒绝、停线或无法结束现象；对应 R 项完成时改为断言合法恢复，
并补充执行／复用／新增请求计数。这里不提供绕过正式门禁的入口，也不接触真实凭据。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import evaluation_chain_driver as driver
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests
from tools.official_client_capture.tests import test_codex_upgrade_evidence_integrity as integrity_tests
from tools.official_client_capture.tests import test_codex_upgrade_timing_ledger as timing_tests


class UpgradeFaultFixtureTests(unittest.TestCase):
    def test_r2_image_only_revision_is_currently_rejected(self):
        with self.assertRaisesRegex(artifacts.VCArtifactError, "全部相同"):
            artifacts.build_candidate_revision_seal(
                campaign_id="fixture-upgrade", revision=2, candidate_id="candidate-r2",
                candidate_commit="a" * 40, source_tree_sha256="1" * 64,
                image_id="sha256:" + "3" * 64, build_receipt_sha256="c" * 64,
                vc3_stage_receipt_sha256="d" * 64, sealed_at_utc="2026-09-23T00:00:00Z",
                superseded={"revision": 1, "candidate_id": "candidate-r1", "git_commit": "a" * 40,
                            "source_tree_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64},
            )

    def test_r3_metadata_only_drift_is_currently_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root, manifest_path = integrity_tests._sealed_evidence_root(root)
            manifest = json.loads(manifest_path.read_text())
            originals = {path: path.read_bytes() for path in evidence_root.rglob("*") if path.is_file()}
            integrity_tests._drift_ctime_only(evidence_root)
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})
            with self.assertRaises(evidence.EvidenceManifestBoundaryDriftError) as caught:
                evidence.verify_manifest_boundary(manifest, [evidence_root])
            self.assertEqual(caught.exception.failure_class, "evidence-integrity")

    def test_r4_classify_parent_failure_currently_stops_campaign(self):
        case = upgrade_tests.CodexUpgradeTest("test_vc_chain_failed_batch_abandons_stage_and_blocks_next_batch")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertFalse(result.skipped)
        self.assertTrue(result.wasSuccessful(), str(result.errors + result.failures))

    def test_r6_import_currently_leaves_vc0_active(self):
        case = driver.new_real_chain_case()
        self.addCleanup(case.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            fixture = case._b0_fixture(Path(directory).resolve(), campaign_id="fault-source")
            source, manifest = fixture["campaign_dir"], fixture["manifest"]
            case._write_capture_stage(source, source / "official-evidence", phase="official",
                                      identity=manifest["official_identity"],
                                      prepare_evidence=driver._prepare_side_evidence(None),
                                      extra_artifacts=driver._extra_artifacts(False))
            target = fixture["data"] / "evidence" / "campaigns" / "fault-successor"
            code, stdout, stderr = case._run_main([
                "reuse-official-evidence", "--predecessor-campaign-dir", str(source),
                "--campaign-dir", str(target), "--campaign-id", "fault-successor",
                "--codex-account-id", str(manifest["configuration"]["codex_account_id"]),
            ])
            self.assertEqual(code, 0, stderr)
            imported = json.loads(stdout)
            self.assertEqual((imported["executed_job_count"], imported["live_request_count"]), (0, 0))
            summary = timing.phase_ledger_state(fixture["timing_ledger"])
            self.assertEqual(summary["active_phase"], "VC-0")
            self.assertNotIn("VC-1", summary["completed_phases"])

    def test_r8_expired_budget_currently_requires_stop(self):
        helper = timing_tests.TimingLedgerTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ledger"
            helper._create(root)
            summary = timing.inspect_ledger(root, now=helper._at(45))
            self.assertEqual(summary["status"], "stop_required")

    def test_r12_dead_child_currently_leaves_driver_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            scripts = root / "driver"
            scripts.mkdir()
            for name in ("vc4-all.sh", "lib.sh", "parse_env.py"):
                shutil.copy2(driver_tests.SCRIPTS / name, scripts / name)
            for name, source in {"trees.sh": "exit 0\n", "frontend.sh": "echo FRONTEND_DONE\n", "vc4-gates.sh": "exit 7\n"}.items():
                (scripts / name).write_text(source)
            process = subprocess.Popen(["bash", str(scripts / "vc4-all.sh")],
                                       env={**os.environ, **fixture.env}, cwd=root,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            self.assertEqual((fixture.runroot / "gates.out").read_text(), "")


if __name__ == "__main__":
    unittest.main()
