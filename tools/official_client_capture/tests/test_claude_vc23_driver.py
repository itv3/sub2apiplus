"""Claude 的 VC-2／VC-3 驱动（Claude 升级 B5b-01，``vc23-claude.sh``／``claude_vc23.py``）：客户端表、计划合同与真实批次链。

文档路 10-07 查出 Claude 的 VC-2、VC-3 没有 ``vc23.sh`` 那样的驱动脚本。这里核对：

* 客户端表：两客户端的 VC-2／VC-3 派发脚本都在驱动里（Codex ``vc23.sh`` 流程不变、Claude ``vc23-claude.sh``）；两个 Claude
  入口对 Codex 参数失败关闭，Codex 的 ``vc23.sh`` 对 Claude 参数失败关闭；
* 计划生成：八个步骤的参数一个不少、一个不多，修订输入必须是绝对路径目录；计划通过共享动作计划合同与 Claude 的阶段幂等
  重派合同（形态精确闭合）；
* 真实批次链（``test_claude_upgrade_batch._RealTreeBatchCase``）：用驱动生成的计划在父监督器里依次派发 VC-2 草案、修订、
  批准与 VC-3 准备草案、修订、批准、暂存预览、暂存批准，Store 走到 ``profile_approved``、VC-2／VC-3 checkpoint 写出、计时
  账本两阶段完成；收尾摘要列出 revision、批准结果、联合摘要与 checkpoint。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import site
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import client_table as CT
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_batch as batch
from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_profile as profile
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_profile as profile_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"


def load_vc23() -> Any:
    return driver_tests.load_script("claude_vc23")


class Vc23TableAndPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.module = load_vc23()

    def env(self, client: str = "claude") -> Any:
        return mock.patch.dict(os.environ, {"CLIENT": client, "NEWDIR": str(self.campaign), "W": str(self.inputs)})

    def test_table_entries_point_at_real_scripts(self) -> None:
        self.assertEqual((CT.spec("codex")["vc23_dispatch_script"], CT.spec("claude")["vc23_dispatch_script"]), ("vc23.sh", "vc23-claude.sh"))
        for client in CT.CLIENTS:
            self.assertTrue((DRIVER / CT.spec(client)["vc23_dispatch_script"]).is_file())
        self.assertEqual(os.stat(DRIVER / "vc23-claude.sh").st_mode & 0o111, 0o111)

    def test_codex_is_refused_by_the_claude_entries_and_claude_by_vc23(self) -> None:
        with self.env("codex"), self.assertRaisesRegex(ValueError, "VC-2／VC-3 派发脚本是 vc23.sh"):
            self.module.plan("classify-draft", self.inputs / "plan.json", {})
        self.assertFalse((self.inputs / "plan.json").exists())
        for script, client, expected in (("vc23-claude.sh", "codex", "vc23.sh"), ("vc23.sh", "claude", "vc23-claude.sh")):
            with self.subTest(script=script):
                fixture = driver_tests._DriverFixture(self.root / f"fixture-{script}")
                if client == "claude":
                    from tools.official_client_capture.tests.test_driver_client_table import claude_env_text

                    fixture.env_file.write_text(claude_env_text(fixture), encoding="utf-8")
                completed = subprocess.run(["bash", str(DRIVER / script), "classify-draft"], env={**os.environ, **fixture.env},
                                           capture_output=True, text=True, cwd=str(self.root))
                self.assertEqual(completed.returncode, 3, completed.stdout + completed.stderr)
                self.assertIn(f"派发脚本是 {expected}，拒绝", completed.stdout)

    def test_step_arguments_are_exact(self) -> None:
        revision_input = self.root / "resolved"
        revision_input.mkdir()
        cases = {
            "classify-revise": ({"from_revision": "rev-0001"}, "缺 \\['revision_input'\\]"),
            "classify-draft": ({"approved_by": "reviewer-a"}, "多余 \\['approved_by'\\]"),
            "prepare-approve": ({"approve_revision": "rev-0002", "approve_review_sha256": "a" * 64}, "缺 \\['approved_by'\\]"),
            "stage-approve": ({"approve_joint_sha256": "b" * 64, "approved_by": "r", "from_revision": "rev-0001"}, "多余 \\['from_revision'\\]"),
        }
        with self.env():
            for name, (values, pattern) in cases.items():
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, pattern):
                    self.module.step_argv(name, values)
            with self.assertRaisesRegex(ValueError, "绝对路径目录"):
                self.module.step_argv("prepare-revise", {"from_revision": "rev-0001", "revision_input": "relative"})
            with self.assertRaisesRegex(ValueError, "未知步骤"):
                self.module.step_argv("classify-preview", {})
            argv = self.module.step_argv("classify-revise", {"from_revision": "rev-0001", "revision_input": str(revision_input)})
        self.assertEqual(argv, ["classify", "--campaign-dir", str(self.campaign), "--from-revision", "rev-0001",
                                "--revision-input", str(revision_input)])
        phases = {name: (step.phase, step.predecessor) for name, step in self.module.STEPS.items()}
        self.assertEqual({name for name, value in phases.items() if value == ("VC-2", "VC-1")}, {"classify-draft", "classify-revise", "classify-approve"})
        self.assertEqual({name for name, value in phases.items() if value == ("VC-3", "VC-2")},
                         {"prepare-draft", "prepare-revise", "prepare-approve", "stage-preview", "stage-approve"})
        # 每一步的命令形态都落在 Claude 阶段幂等重派合同里登记的那一种。
        for name, step in self.module.STEPS.items():
            self.assertIn(step.mode, batch.STAGE_REPLAY_CONTRACTS[step.phase][step.subcommand], name)
            self.assertEqual({flag for _option, flag in step.options} | {"--campaign-dir"},
                             set(batch.STAGE_REPLAY_CONTRACTS[step.phase][step.subcommand][step.mode][0]), name)


@unittest.skipUnless(site.ENABLE_USER_SITE, "批次夹具经 usercustomize 进入动作子进程，要求启用用户 site")
class Vc23ClaudeChainTests(batch_tests._RealTreeBatchCase):
    """驱动生成的计划在真实父监督器里派发 VC-2 与 VC-3 全部八个步骤。"""

    def generated(self, module: Any, campaign: Path, name: str, **values: str) -> dict[str, Any]:
        inputs = self.root / "driver-inputs"
        inputs.mkdir(exist_ok=True)
        output = inputs / f"action-plan-claude-{name}.json"
        with mock.patch.dict(os.environ, {"CLIENT": "claude", "NEWDIR": str(campaign), "W": str(inputs)}), \
                contextlib.redirect_stdout(io.StringIO()):
            module.plan(name, output, {option: values.get(option) for option in module.OPTION_NAMES})
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(oct(output.stat().st_mode & 0o777), "0o600")
        return payload

    def run_step(self, module: Any, campaign: Path, sequence: int, name: str, **values: str) -> dict[str, Any]:
        plan = self.generated(module, campaign, name, **values)
        step = module.STEPS[name]
        [action] = plan["actions"]
        self.assertEqual((action["action_id"], action["operation"], action["item_ids"]), (step.action_id, f"{step.phase}:{step.action_id}", [step.action_id]))
        return self.assert_batch_passed(self.dispatch(campaign, step.phase, sequence, plan["actions"]), sequence, step.phase)

    def test_vc2_and_vc3_dispatch_through_driver_plans(self) -> None:
        module = load_vc23()
        campaign = self.campaign = self.build_batch_campaign()
        # VC-2：草案 → 审核人处置 → 修订 → 批准。
        self.run_step(module, campaign, 2, "classify-draft")
        docs = self.resolved_documents(campaign / classify.DRAFT_ROOT / "rev-0001", change=(profile_tests.CHANGE,), delete=(profile_tests.DELETE,))
        # 与 approve_vc2_directly 同一处置：受影响规则登记画像补丁落点（VC-3 准备的补丁只能落在这里）。
        for row in docs["profile_patches"]["rule_patches"]:
            row["profile_paths"] = list(profile_tests.PLANNED[row["spec_id"]])
        resolved = self.write_documents(docs, self.root / "vc2-resolved")
        self.run_step(module, campaign, 3, "classify-revise", from_revision="rev-0001", revision_input=str(resolved))
        code, revised, stderr = self.revise(campaign, "rev-0001", resolved)
        self.assertEqual((code, revised["revision"], revised["gate"]["reasons"]), (2, "rev-0002", []), stderr)
        self.run_step(module, campaign, 4, "classify-approve", approve_revision="rev-0002", approve_review_sha256=revised["review_sha256"])
        self.assertTrue(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2").is_file())
        # VC-3：准备草案 → 修订 → 批准 → 暂存预览 → 暂存批准。
        self.run_step(module, campaign, 5, "prepare-draft")
        prepare_resolved = self.write_prepare(self.resolved_prepare(campaign, campaign / profile.PREPARE_DRAFT_ROOT / "rev-0001"),
                                              self.root / "prepare-resolved")
        self.run_step(module, campaign, 6, "prepare-revise", from_revision="rev-0001", revision_input=str(prepare_resolved))
        code, prepared, stderr = self.revise_prepare(campaign, "rev-0001", prepare_resolved)
        self.assertEqual((code, prepared["revision"], prepared["gate"]["reasons"]), (2, "rev-0002", []), stderr)
        self.run_step(module, campaign, 7, "prepare-approve", approve_revision="rev-0002", approve_review_sha256=prepared["review_sha256"],
                      approved_by="reviewer-a")
        self.run_step(module, campaign, 8, "stage-preview")
        (joint_manifest,) = sorted((campaign / profile.STAGE_ROOT).glob(f"*/{profile.JOINT_MANIFEST_FILENAME}"))
        joint = json.loads(joint_manifest.read_text(encoding="utf-8"))["joint_sha256"]
        result = self.run_step(module, campaign, 9, "stage-approve", approve_joint_sha256=joint, approved_by="reviewer-a")
        self.assertEqual(result["timing_ledger"]["completion"]["idempotent"], False)
        self.assertEqual(seal_tests.store_status(self.store_root, batch_tests.CAMPAIGN_ID), "profile_approved")
        completed = timing.phase_ledger_state(self.ledger_dir)["completed_phases"]
        self.assertTrue({"VC-2", "VC-3"} <= set(completed), completed)
        self.assertEqual(claude_upgrade._load_stage_result(campaign, "stage-profile")["joint_sha256"], joint)
        # 收尾摘要：revision、批准结果、联合摘要与两个 checkpoint。
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"CLIENT": "claude", "NEWDIR": str(campaign), "W": str(self.root / "driver-inputs")}), \
                contextlib.redirect_stdout(output):
            module.summary()
        text = output.getvalue()
        self.assertIn("VC-2 分类草案 revision：['rev-0001', 'rev-0002']；批准：已写", text)
        self.assertIn(joint, text)
        self.assertIn("VC-2 checkpoint：已写", text)
        self.assertIn("VC-3 checkpoint：已写", text)
        self.assert_zero_requests()


if __name__ == "__main__":
    unittest.main()
