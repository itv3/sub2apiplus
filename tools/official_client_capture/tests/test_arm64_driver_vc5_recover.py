"""驱动 VC-5 候选采集续跑计划生成器：生成的两份动作计划必须逐字满足监督器续跑后继协议。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

SCRIPTS = driver_tests.SCRIPTS
TOOLS_ROOT = Path(__file__).resolve().parents[2]


class VC5RecoverPlanTests(unittest.TestCase):
    IDENTITY = {
        "--candidate-id": "cand-r5",
        "--build-receipt": "/data/evidence/campaigns/c/candidates/cand-r5/build-receipt.json",
        "--runtime-image": "repo@sha256:" + "1" * 64,
        "--candidate-image-id": "sha256:" + "1" * 64,
        "--candidate-source": "/data/candidates/cand-r5/source",
        "--build-id": "cand-r5-build",
        "--deployed-version": "0.157.0",
        "--profile-id": "codex-0.157.0-official-r1",
        "--profile-digest": "2" * 64,
        "--candidate-purpose": "production_replacement",
    }

    def _layout(self, root: Path) -> tuple[Path, Path, dict]:
        data = root / "data"
        (data / "tools").mkdir(parents=True)
        # 生成器按 Campaign 目录上推三级取数据根并导入其中的受管工具树。
        (data / "tools" / "official_client_capture").symlink_to(TOOLS_ROOT / "official_client_capture")
        (data / "tools" / "__init__.py").write_text("", encoding="utf-8")
        campaign = data / "evidence" / "campaigns" / "c"
        manifests = campaign / "control" / "vc" / "run-manifests"
        manifests.mkdir(parents=True)
        prefix = ["/usr/bin/python3", str(data / "tools" / "official_client_capture" / "codex_upgrade.py")]
        command = [*prefix, "capture-candidate", "run", "--campaign-dir", str(campaign)]
        for flag, value in self.IDENTITY.items():
            command.extend([flag, value])
        command.extend(["--max-wall-seconds", "21600", "--acknowledge-live-requests"])
        failed = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": "c", "campaign_plan_sha256": "1" * 64, "batch_id": "vc-5-0009", "batch_sequence": 9,
            "batch_sha256": "9" * 64, "phase": "VC-5", "predecessor_checkpoint": {"phase": "VC-4"},
            "original_deadline_at_utc": "2099-01-01T00:00:00Z", "no_op": False, "candidate_id": "cand-r5",
            "candidate_revision": 1, "evaluation_baseline": None, "baseline_commit_sha256": None,
            "actions": [{"action_id": "candidate-run", "operation": "VC-5:capture-candidate-run", "timeout_seconds": 21600.0,
                         "command": command, "item_ids": ["candidate-run"]}],
            "execute_items": ["candidate-run"], "reuse_items": [],
        }
        (manifests / "0008-vc-4.json").write_text(json.dumps({"phase": "VC-4"}), encoding="utf-8")
        (manifests / "0009-vc-5.json").write_text(json.dumps(failed), encoding="utf-8")
        preview = campaign / "control" / "reconciliation" / "attempt-a1" / "recovery-preview-01.json"
        preview.parent.mkdir(parents=True)
        preview.write_text(json.dumps({"phase": "candidate", "candidate_id": "cand-r5", "recovery_revision": None,
                                       "execute_job_ids": ["j1"], "reuse_job_ids": ["j2"]}), encoding="utf-8")
        return campaign, preview, failed

    def _generate(self, out: Path, campaign: Path, preview: Path) -> subprocess.CompletedProcess:
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "gen_vc5_recovery_plans.py"), str(out), str(campaign), str(preview)],
            capture_output=True, text=True, timeout=120, env=environment,
        )

    def test_generated_plans_satisfy_supervisor_recovery_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, preview, failed = self._layout(root)
            out = root / "plans"
            completed = self._generate(out, campaign, preview)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            preview_plan = json.loads((out / "action-plan-vc5-recovery-preview.json").read_text(encoding="utf-8"))
            run_plan = json.loads((out / "action-plan-vc5-recovery-run.json").read_text(encoding="utf-8"))
            for plan in (preview_plan, run_plan):
                self.assertEqual((plan["execute_item_ids"], plan["reuse_item_ids"]), (["candidate-run"], []))
            # 以计划动作组装 N+1 清单：监督器续跑预览协议逐字接受；补跑命令解析出同一组候选身份参数。
            successor = copy.deepcopy(failed)
            successor.update(batch_id="vc-5-0010", batch_sequence=10, batch_sha256="a" * 64, actions=preview_plan["actions"])
            prefix, campaign_text, identity = supervisor.candidate_recovery_parent_identity(failed)
            self.assertEqual((campaign_text, identity), (str(campaign), self.IDENTITY))
            action = successor["actions"][0]
            self.assertEqual(action["command"], supervisor.candidate_recovery_preview_command(prefix, campaign_text, identity))
            parsed = supervisor._candidate_recovery_command_identity(run_plan["actions"][0]["command"], preview=False)
            self.assertIsNotNone(parsed)
            self.assertEqual((parsed[2], parsed[3]), (self.IDENTITY, str(preview)))
            self.assertEqual(
                supervisor.candidate_recovery_parent_identity({**failed, "actions": run_plan["actions"]})[2], self.IDENTITY
            )

    def test_generator_refuses_mismatched_preview_or_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, preview, _failed = self._layout(root)
            other = json.loads(preview.read_text(encoding="utf-8"))
            other["candidate_id"] = "other"
            preview.write_text(json.dumps(other), encoding="utf-8")
            completed = self._generate(root / "plans", campaign, preview)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("候选不一致", completed.stderr)
            segment = dict(other, candidate_id="cand-r5", recovery_revision="ar1")
            preview.write_text(json.dumps(segment), encoding="utf-8")
            completed = self._generate(root / "plans2", campaign, preview)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("非段模式", completed.stderr)


if __name__ == "__main__":
    unittest.main()
