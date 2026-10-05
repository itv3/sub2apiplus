"""本轮根解析：旧参数必须在写入前拒绝，初始化不能伪装为已激活候选。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.arm64_capture_driver.driver import parse_env, round_context
from tools.official_client_capture.tests.test_arm64_capture_driver import SCRIPTS, _DriverFixture


class RoundContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = _DriverFixture(self.root)
        self.values = parse_env.parse(self.fixture.env_file.read_text())
        self.config = {**self.values, **parse_env.derive(self.values)}
        self.campaign = self.fixture.newdir
        self.manifest = {"campaign_id": self.config["NEW"], "baseline_version": self.config["BASELINE_VERSION"],
                         "target_version": self.config["TARGET_VERSION"], "campaign_mode": "formal",
                         "vc_control": {"campaign_plan": {"path": "control/vc/campaign-plan.json"}}}
        self.write(self.campaign / "campaign.json", self.manifest)
        (self.campaign / "campaign.sha256").write_text(hashlib.sha256((self.campaign / "campaign.json").read_bytes()).hexdigest())
        self.write(self.campaign / "control/vc/campaign-plan.json", {"plan": "本轮"})
        self.write(Path(self.config["L"]) / "ledger.json", {"ledger": "本轮"})
        self.upgrade = mock.Mock()
        self.upgrade._require_formal_campaign.return_value = self.manifest
        self.upgrade._vc_campaign_plan.return_value = {"plan": "本轮"}
        self.upgrade._campaign_timing_ledger_dir.return_value = Path(self.config["L"])
        self.upgrade._candidate_revision_dir.side_effect = lambda c, r: c / "control/vc/revisions" / f"r{r}"
        self.activate(1)

    @staticmethod
    def write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False))

    def activate(self, revision):
        record = {"campaign_id": self.config["NEW"], "candidate_id": self.config["CAND"], "revision": revision}
        directory = self.campaign / "control/vc/revisions" / f"r{revision}"
        self.write(directory / "revision.json", record)
        self.write(directory / "COMMIT", {"revision": revision})
        self.upgrade._current_candidate_revision_record.return_value = revision, record

    def resolve(self, **kwargs):
        return round_context.resolve(self.config, upgrade=self.upgrade, **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_current_context_is_read_only_and_repeatable(self):
        before = self.snapshot()
        with mock.patch.dict(os.environ, {"CODEX_UPGRADE_IDENTITY_MEMO": str(self.root / "memo")}):
            first = self.resolve()
            self.assertEqual(first, self.resolve())
            self.assertEqual(os.environ["CODEX_UPGRADE_IDENTITY_MEMO"], str(self.root / "memo"))
        self.assertEqual(first["state"], "candidate_verified")
        self.assertEqual(first["parameters"]["B"], str(self.fixture.candidate_dir))
        self.assertEqual(self.snapshot(), before)

    def test_candidate_directory_larger_number_is_not_authority(self):
        self.activate(2)
        self.write(self.campaign / "control/vc/revisions/r99/revision.json", {"candidate_id": "旧候选"})
        report = self.resolve()
        self.assertEqual(report["candidate_revision"], 2)
        self.assertTrue(report["parameters"]["CANDIDATE_REVISION_ROOT"].endswith("/r2"))

    def test_old_candidate_assertion_is_rejected(self):
        record = dict(self.upgrade._current_candidate_revision_record.return_value[1], candidate_id="other-candidate")
        self.upgrade._current_candidate_revision_record.return_value = 1, record
        with self.assertRaisesRegex(ValueError, "CAND.*不一致"):
            self.resolve()

    def test_candidate_from_other_campaign_is_rejected(self):
        record = dict(self.upgrade._current_candidate_revision_record.return_value[1], campaign_id="other-campaign")
        self.upgrade._current_candidate_revision_record.return_value = 1, record
        with self.assertRaisesRegex(ValueError, "revision.*当前 Campaign"):
            self.resolve()

    def test_pending_revision_does_not_activate_candidate(self):
        self.upgrade._current_candidate_revision_record.return_value = None, None
        with self.assertRaisesRegex(ValueError, "pending"):
            self.resolve()
        self.assertEqual(self.resolve(mode="campaign")["state"], "campaign_verified")

    def test_historical_r1_uses_replayed_checkpoint_candidate(self):
        path = self.campaign / "control/vc/vc-4-checkpoint.json"
        self.write(path, {"historical": 1})
        self.upgrade._current_candidate_revision_record.return_value = 1, None
        self.upgrade._implicit_r1_candidate_id.return_value = self.config["CAND"]
        self.upgrade._replay_vc_checkpoint.return_value = path, {}
        report = self.resolve()
        self.assertEqual(report["candidate_revision"], 1)
        self.assertEqual(report["parameters"]["CANDIDATE_REVISION_ROOT"], str(self.campaign / "control/vc"))
        self.assertIn("legacy_vc4_checkpoint", report["bindings"])
        self.upgrade._implicit_r1_candidate_id.return_value = "previous-candidate"
        with self.assertRaisesRegex(ValueError, "CAND"):
            self.resolve()

    def test_manifest_identity_and_versions_must_match(self):
        for key in ("campaign_id", "baseline_version", "target_version"):
            with self.subTest(key=key):
                self.upgrade._require_formal_campaign.return_value = dict(self.manifest, **{key: "旧值"})
                with self.assertRaisesRegex(ValueError, key):
                    self.resolve()

    def test_control_epoch_ledger_cannot_fall_back_to_old_up(self):
        self.upgrade._campaign_timing_ledger_dir.return_value = self.root / "new-epoch-ledger"
        with self.assertRaisesRegex(ValueError, "有效账本"):
            self.resolve()

    def test_missing_or_corrupt_authority_fails_closed(self):
        (self.campaign / "campaign.sha256").unlink()
        with self.assertRaisesRegex(ValueError, "权威输入缺失"):
            self.resolve()
        (self.campaign / "campaign.sha256").write_text("不合法摘要")
        self.upgrade._require_formal_campaign.side_effect = ValueError("原收据重放拒绝")
        with self.assertRaisesRegex(ValueError, "原收据重放拒绝"):
            self.resolve()

    def test_missing_committed_record_is_rejected(self):
        (self.campaign / "control/vc/revisions/r1/COMMIT").unlink()
        with self.assertRaisesRegex(ValueError, "权威输入缺失"):
            self.resolve()

    def test_authority_change_during_resolution_is_rejected(self):
        def change(campaign, manifest):
            self.write(self.campaign / "campaign.json", {"drift": True})
            return Path(self.config["L"])
        self.upgrade._campaign_timing_ledger_dir.side_effect = change
        with self.assertRaisesRegex(ValueError, "权威输入发生变化"):
            self.resolve()

    def test_revision_change_during_resolution_is_rejected(self):
        current = self.upgrade._current_candidate_revision_record.return_value
        self.upgrade._current_candidate_revision_record.side_effect = [current, (2, {"candidate_id": "next"})]
        with self.assertRaisesRegex(ValueError, "Candidate 发生变化"):
            self.resolve()

    def test_init_has_separate_state_and_never_creates_directory(self):
        config = dict(self.config, NEW="new-campaign")
        report = round_context.resolve(config, mode="init")
        self.assertEqual(report["state"], "initialization_coordinates")
        self.assertFalse(Path(report["parameters"]["NEWDIR"]).exists())
        with self.assertRaisesRegex(ValueError, "尚未创建"):
            round_context.resolve(config)
        with self.assertRaisesRegex(ValueError, "不能降级初始化"):
            self.resolve(mode="init")

    def test_optional_b_is_derived_but_old_assertion_is_rejected(self):
        text = self.fixture.env_file.read_text()
        without = "\n".join(line for line in text.splitlines() if not line.startswith("B="))
        self.assertEqual(parse_env.parse(without)["B"], str(self.fixture.candidate_dir))
        with self.assertRaisesRegex(ValueError, "旧候选路径"):
            parse_env.parse(text.replace(str(self.fixture.candidate_dir), str(self.root / "old-candidate")))

    def test_invalid_roots_are_rejected_before_runroot_creation(self):
        env_text = self.fixture.env_file.read_text().replace(str(self.fixture.runroot), str(self.root / "uncreated"))
        path = self.root / "invalid.env"
        probe = self.root / "probe.sh"
        probe.write_text('set -e; source "$1/lib.sh"; echo UNEXPECTED\n')
        for bad in ("/previous-candidate", str(self.fixture.candidate_dir) + "/../old", "relative/path"):
            with self.subTest(path=bad):
                path.write_text(env_text.replace(str(self.fixture.candidate_dir), bad))
                result = subprocess.run(["bash", str(probe), str(SCRIPTS)],
                                        env={**os.environ, "ARM64_VC_ENV": str(path)}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("旧候选路径", result.stderr + result.stdout)
                self.assertFalse((self.root / "uncreated").exists())
                self.assertNotIn("UNEXPECTED", result.stdout)

    def test_symlink_parent_cannot_redirect_receipt_root(self):
        receipts = self.campaign / "control/vc/receipts"
        target = self.root / "old-receipts"
        target.mkdir()
        receipts.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.resolve()

    def test_shell_exports_overwrite_stale_root_values(self):
        command = 'set -e; source "$1/lib.sh"; printf "%s\\n" "$B" "$NEWDIR" "$CANDIDATE_RECEIPT_ROOT"'
        probe = self.root / "probe.sh"
        probe.write_text(command + "\n")
        env = {**os.environ, **self.fixture.env, "B": "/old", "NEWDIR": "/old", "CANDIDATE_RECEIPT_ROOT": "/old"}
        result = subprocess.run(["bash", str(probe), str(SCRIPTS)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [self.config[key] for key in ("B", "NEWDIR", "CANDIDATE_RECEIPT_ROOT")])


if __name__ == "__main__":
    unittest.main()
