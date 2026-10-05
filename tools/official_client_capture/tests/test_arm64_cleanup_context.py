"""清理前动态参数的原生路径、完整阶段、只读及漂移边界。"""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tools.arm64_capture_driver.driver import cleanup_context
from tools.official_client_capture.tests import test_arm64_phase_context as phases


class CleanupContextTests(unittest.TestCase):
    def setUp(self):
        fixture = phases.PhaseContextTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.config, self.cu = fixture.config, fixture.cu
        context = fixture.vc6_context()
        plan = fixture.vc6_plan(context)
        checkpoint = self.cu._canonical_latest_checkpoint.return_value
        for action in plan["actions"]:
            item = phases.native.codex_upgrade_vc_artifacts.canonical_action_binding(action)
            source = phases.native._canonical_file_binding(fixture.campaign, Path(item["step_receipt"]), "完成步骤")
            checkpoint["items"].append({"item_id": item["item_id"], "source": source})
        fixture.write(self.cu._canonical_checkpoint_file.return_value, checkpoint)
        fixture.capture_result()
        self.cu.RESULTS_SCHEMA_V2 = phases.native.RESULTS_SCHEMA_V2
        for stage in ("compare", "assertions", "accept"):
            path = fixture.read_source(fixture.campaign, self.config["CAND"], 0, stage)["path"]
            value = {"status": "complete", "package_digest": "a" * 64}
            if stage == "assertions":
                value = {"schema_version": phases.native.RESULTS_SCHEMA_V2, "candidate_id": self.config["CAND"],
                         "target_version": self.config["TARGET_VERSION"], "comparison_package_digest": "a" * 64}
                fixture.write(path.parent / "evaluation-run.json", {"index": True})
            if stage == "accept":
                assertion = fixture.read_source(fixture.campaign, self.config["CAND"], 0, "assertions")["path"]
                value["assertion_result"] = {"path": str(assertion.relative_to(fixture.campaign)),
                                             "sha256": hashlib.sha256(assertion.read_bytes()).hexdigest()}
            fixture.write(path, value)
        for phase in ("VC-5", "VC-6"):
            fixture.write(fixture.receipt_path(fixture.campaign, self.config["CAND"], phase.replace("-", "").lower() + "_completion"), {"phase": phase})
            fixture.write(fixture.checkpoint_path(fixture.campaign, phase, revision=1), {"phase": phase})

    def resolve(self):
        return cleanup_context.resolve(self.config, upgrade=self.cu)

    def test_current_identity_receipts_and_repeat_are_read_only(self):
        before = self.fixture.base.snapshot()
        result = self.resolve()
        self.assertEqual(result, self.resolve())
        self.assertEqual(result["parameters"]["ATT"], self.fixture.attempt_id)
        self.assertEqual(result["parameters"]["CAND"], self.config["CAND"])
        self.assertTrue(result["parameters"]["RETIRE_RECEIPT"].endswith("/control/current/removal.json"))
        self.assertEqual(before, self.fixture.base.snapshot())
        self.cu._canonical_production_step.assert_not_called()

    def test_missing_or_failed_stage_refuses_cleanup(self):
        for stage in ("compare", "accept", "assertions"):
            path = self.fixture.read_source(self.fixture.campaign, self.config["CAND"], 0, stage)["path"]
            old = path.read_bytes()
            path.unlink()
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                self.resolve()
            path.write_bytes(old)

    def test_completion_must_replay_current_vc5_and_vc6(self):
        self.cu._replay_vc_completion.side_effect = ValueError("完成收据重放失败")
        with self.assertRaisesRegex(ValueError, "完成收据"):
            self.resolve()

    def test_missing_canonical_step_or_changed_bytes_refuses_cleanup(self):
        checkpoint = self.cu._canonical_latest_checkpoint.return_value
        item = checkpoint["items"].pop()
        with self.assertRaisesRegex(ValueError, "步骤未完成"):
            self.resolve()
        checkpoint["items"].append(item)
        item["source"]["bytes"] += 1
        with self.assertRaisesRegex(ValueError, "已完成步骤"):
            self.resolve()

    def test_old_round_and_attempt_refuse_cleanup(self):
        self.config["CAND"] = "old-candidate"
        with self.assertRaises(ValueError):
            self.resolve()

    def test_canonical_change_during_read_is_rejected(self):
        original = deepcopy(self.cu._canonical_latest_checkpoint.return_value)
        changed = deepcopy(original)
        changed["changed"] = True
        self.cu._canonical_latest_checkpoint.side_effect = [original, original, changed]
        with self.assertRaisesRegex(ValueError, "canonical 发生变化"):
            self.resolve()

    def test_disabled_identity_cache_is_restored_without_writes(self):
        before = self.fixture.base.snapshot()
        with mock.patch.dict(os.environ, {"CODEX_UPGRADE_IDENTITY_MEMO": "/unused/cleanup-memo"}):
            self.resolve()
            self.assertEqual(os.environ["CODEX_UPGRADE_IDENTITY_MEMO"], "/unused/cleanup-memo")
        self.assertEqual(before, self.fixture.base.snapshot())

    def test_standalone_missing_env_has_no_bytecode_or_output_files(self):
        driver = self.fixture.root / "standalone"
        driver.mkdir()
        for name in ("cleanup_context.py", "phase_context.py", "round_context.py", "parse_env.py"):
            (driver / name).write_bytes((phases.SCRIPTS / name).read_bytes())
        before = self.fixture.base.snapshot()
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH"}}
        run = subprocess.run([sys.executable, str(driver / "cleanup_context.py"), "--env", str(driver / "absent.env")],
                             env=environment, capture_output=True, text=True)
        self.assertEqual(run.returncode, 3)
        self.assertEqual(before, self.fixture.base.snapshot())


if __name__ == "__main__":
    unittest.main()
