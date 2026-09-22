"""VC-6 canonical 步骤的批次派发闭包（冻结映射、分组、次序与恢复分类）。

0.154.0 起 ``canonical-advance`` 必须由 campaign-run 派发，而 staging 模型的后继
批次只能经 ``compile-and-run-vc-batch`` 编译，因此 VC-6 的三个生产步骤必须和
VC-5 四步一样落在冻结映射内。本模块只用小型纯函数夹具，不建 Campaign、不发请求。
"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

CAMPAIGN_DIR = "/data/evidence/campaigns/c0154-formal"
CANDIDATE_ID = "c0154-candidate-v14r5"
ATTEMPT_ID = "20260922T081201Z-6b19d215a302dbd4"
RECEIPT = "/data/evidence/campaigns/c0154-formal/control/vc6/activation-receipt.json"
CLI = "/data/tools/official_client_capture/codex_upgrade.py"


def advance_action(
    action_id: str,
    item_id: str,
    step: str,
    *,
    step_receipt: str | None = RECEIPT,
    retire_version: str | None = None,
    extra: tuple[str, ...] = (),
    phase: str | None = None,
) -> dict[str, object]:
    command = [
        "/usr/bin/python3",
        CLI,
        "canonical-advance",
        "--campaign-dir",
        CAMPAIGN_DIR,
        "--candidate-id",
        CANDIDATE_ID,
        "--attempt-id",
        ATTEMPT_ID,
        "--canonical-step",
        step,
    ]
    if step_receipt is not None:
        command += ["--step-receipt", step_receipt]
    if retire_version is not None:
        command += ["--retire-version", retire_version]
    if phase is not None:
        command += ["--phase", phase]
    command += list(extra)
    return {
        "action_id": action_id,
        "operation": f"VC-6:canonical-advance-{step}",
        "timeout_seconds": 1800,
        "command": command,
        "item_ids": [item_id],
    }


def vc5_seal_action() -> dict[str, object]:
    return {
        "action_id": "canonical-2-seal",
        "operation": "VC-5:canonical-advance-seal",
        "timeout_seconds": 1800,
        "command": [
            "/usr/bin/python3",
            CLI,
            "canonical-advance",
            "--campaign-dir",
            CAMPAIGN_DIR,
            "--candidate-id",
            CANDIDATE_ID,
            "--attempt-id",
            ATTEMPT_ID,
            "--canonical-step",
            "seal",
        ],
        "item_ids": ["canonical-seal"],
    }


PRODUCTION = advance_action(
    "canonical-5-production-activation",
    "production-activation",
    "production-activation",
)
ROLLBACK = advance_action(
    "canonical-6-rollback-verification",
    "rollback-verification",
    "rollback-verification",
)
RETIRE = advance_action(
    "canonical-7-retire",
    "retire-0.149.1",
    "retire",
    retire_version="0.149.1",
)


class CanonicalItemClosureTest(unittest.TestCase):
    """冻结闭集识别 VC-5 四项、VC-6 两个静态项和动态退休项。"""

    def test_phase_of_each_frozen_item(self) -> None:
        for item in ("canonical-import", "canonical-seal", "canonical-compare", "canonical-accept"):
            self.assertEqual(artifacts.canonical_item_phase(item), "VC-5", item)
        for item in ("production-activation", "rollback-verification", "retire-0.149.1", "retire-0.147.0"):
            self.assertEqual(artifacts.canonical_item_phase(item), "VC-6", item)

    def test_non_canonical_and_malformed_retire_items_are_not_canonical(self) -> None:
        for item in (
            "acceptance",
            "assert-SPEC-EP-019",
            "candidate-core-direct",
            "retire",
            "retire-",
            "retire-0.149",
            "retire-0.149.1-extra",
            "retire-v0.149.1",
            "",
            None,
            123,
        ):
            self.assertFalse(artifacts.is_canonical_item(item), item)

    def test_retire_item_carries_its_version(self) -> None:
        self.assertEqual(
            artifacts.canonical_item_command("retire-0.149.1"),
            ("canonical-advance", "retire", "0.149.1"),
        )
        self.assertEqual(
            artifacts.canonical_item_command("production-activation"),
            ("canonical-advance", "production-activation", None),
        )


class CanonicalActionBindingTest(unittest.TestCase):
    """单动作层：子命令、步骤、退休版本、收据与 --phase 必须逐字对上。"""

    def test_vc6_actions_bind(self) -> None:
        for action, item, step in (
            (PRODUCTION, "production-activation", "production-activation"),
            (ROLLBACK, "rollback-verification", "rollback-verification"),
            (RETIRE, "retire-0.149.1", "retire"),
        ):
            binding = artifacts.canonical_action_binding(action)
            assert binding is not None
            self.assertEqual(binding["item_id"], item)
            self.assertEqual(binding["canonical_step"], step)
            self.assertEqual(binding["group"], "VC-6")
            self.assertEqual(binding["campaign_dir"], CAMPAIGN_DIR)
            self.assertEqual(binding["step_receipt"], RECEIPT)
        self.assertEqual(
            artifacts.canonical_action_binding(RETIRE)["retire_version"], "0.149.1"
        )
        self.assertIsNone(
            artifacts.canonical_action_binding(PRODUCTION)["retire_version"]
        )

    def test_retire_version_must_equal_item_version(self) -> None:
        action = advance_action(
            "canonical-7-retire", "retire-0.149.1", "retire", retire_version="0.151.0"
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--retire-version"):
            artifacts.canonical_action_binding(action)

    def test_retire_version_is_required_for_retire_item(self) -> None:
        action = advance_action("canonical-7-retire", "retire-0.149.1", "retire")
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--retire-version"):
            artifacts.canonical_action_binding(action)

    def test_non_retire_item_rejects_retire_version(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            retire_version="0.149.1",
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不接受 --retire-version"):
            artifacts.canonical_action_binding(action)

    def test_duplicate_retire_version_option_is_rejected(self) -> None:
        action = advance_action(
            "canonical-7-retire",
            "retire-0.149.1",
            "retire",
            retire_version="0.149.1",
            extra=("--retire-version", "0.149.1"),
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "重复出现"):
            artifacts.canonical_action_binding(action)

    def test_duplicate_step_receipt_option_is_rejected(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            extra=("--step-receipt", RECEIPT),
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "重复出现"):
            artifacts.canonical_action_binding(action)

    def test_step_receipt_required_and_absolute_for_vc6(self) -> None:
        missing = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            step_receipt=None,
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--step-receipt"):
            artifacts.canonical_action_binding(missing)
        relative = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            step_receipt="control/vc6/activation-receipt.json",
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--step-receipt"):
            artifacts.canonical_action_binding(relative)

    def test_vc5_item_rejects_step_receipt(self) -> None:
        action = vc5_seal_action()
        action["command"] = [*action["command"], "--step-receipt", RECEIPT]
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不接受 --step-receipt"):
            artifacts.canonical_action_binding(action)

    def test_step_must_match_item(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "rollback-verification",
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--canonical-step"):
            artifacts.canonical_action_binding(action)

    def test_action_phase_option_must_match_group(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            phase="VC-5",
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--phase 必须是 VC-6"):
            artifacts.canonical_action_binding(action)

    def test_vc6_action_still_rejects_supervisor_run_dir(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            extra=("--supervisor-run-dir", "/data/control/run"),
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--supervisor-run-dir"):
            artifacts.canonical_action_binding(action)

    def test_import_keeps_its_retire_version_semantics(self) -> None:
        """canonical-import 用 --retire-version 冻结退休项，是唯一的例外形态。"""

        def import_action(retire: str | None) -> dict[str, object]:
            command = [
                "/usr/bin/python3",
                CLI,
                "canonical-import",
                "--campaign-dir",
                CAMPAIGN_DIR,
                "--candidate-id",
                CANDIDATE_ID,
                "--attempt-id",
                ATTEMPT_ID,
                "--phase",
                "VC-5",
                "--approve-import-sha256",
                "a" * 64,
            ]
            if retire is not None:
                command += ["--retire-version", retire]
            return {
                "action_id": "canonical-1-import",
                "operation": "VC-5:canonical-import",
                "timeout_seconds": 1800,
                "command": command,
                "item_ids": ["canonical-import"],
            }

        binding = artifacts.canonical_action_binding(import_action("0.149.1"))
        assert binding is not None
        self.assertEqual(binding["group"], "VC-5")
        self.assertIsNone(binding["retire_version"])
        for bad in (None, "0.149", "v0.149.1"):
            with self.assertRaisesRegex(artifacts.VCArtifactError, "--retire-version 非法"):
                artifacts.canonical_action_binding(import_action(bad))

    def test_vc6_action_rejects_import_approval(self) -> None:
        action = advance_action(
            "canonical-5-production-activation",
            "production-activation",
            "production-activation",
            extra=("--approve-import-sha256", "a" * 64),
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "--approve-import-sha256"):
            artifacts.canonical_action_binding(action)


class CanonicalBatchBindingTest(unittest.TestCase):
    """批次层：纯 canonical、单组、冻结次序、同一身份。"""

    def test_full_vc6_batch_binds(self) -> None:
        binding = artifacts.canonical_batch_binding(
            [PRODUCTION, ROLLBACK, RETIRE],
            execute_item_ids=["production-activation", "retire-0.149.1", "rollback-verification"],
            phase="VC-6",
        )
        assert binding is not None
        self.assertEqual(binding["group"], "VC-6")
        self.assertEqual(
            binding["item_ids"],
            ["production-activation", "retire-0.149.1", "rollback-verification"],
        )
        self.assertEqual(binding["attempt_id"], ATTEMPT_ID)

    def test_partial_vc6_batch_binds(self) -> None:
        binding = artifacts.canonical_batch_binding(
            [PRODUCTION, ROLLBACK],
            execute_item_ids=["production-activation", "rollback-verification"],
            phase="VC-6",
        )
        assert binding is not None
        self.assertEqual(binding["group"], "VC-6")

    def test_vc6_items_rejected_in_vc5_batch(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不得编入 VC-5 批次"):
            artifacts.canonical_batch_binding(
                [PRODUCTION],
                execute_item_ids=["production-activation"],
                phase="VC-5",
            )

    def test_vc5_items_rejected_in_vc6_batch(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不得编入 VC-6 批次"):
            artifacts.canonical_batch_binding(
                [vc5_seal_action()],
                execute_item_ids=["canonical-seal"],
                phase="VC-6",
            )

    def test_mixed_groups_are_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不得混合 VC-5 与 VC-6"):
            artifacts.canonical_batch_binding(
                [vc5_seal_action(), PRODUCTION],
                execute_item_ids=["canonical-seal", "production-activation"],
            )

    def test_retire_must_come_last(self) -> None:
        retire_first = copy.deepcopy(RETIRE)
        retire_first["action_id"] = "canonical-1-retire"
        with self.assertRaisesRegex(artifacts.VCArtifactError, "冻结次序"):
            artifacts.canonical_batch_binding(
                [retire_first, PRODUCTION, ROLLBACK],
                execute_item_ids=[
                    "production-activation",
                    "retire-0.149.1",
                    "rollback-verification",
                ],
                phase="VC-6",
            )

    def test_rollback_before_activation_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "冻结次序"):
            artifacts.canonical_batch_binding(
                [ROLLBACK, PRODUCTION],
                execute_item_ids=["production-activation", "rollback-verification"],
                phase="VC-6",
            )

    def test_non_canonical_execute_item_cannot_join_vc6_batch(self) -> None:
        other = {
            "action_id": "zz-other",
            "operation": "VC-6:other",
            "timeout_seconds": 60,
            "command": ["/bin/true"],
            "item_ids": ["acceptance"],
        }
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不得混入其它 execute 项"):
            artifacts.canonical_batch_binding(
                [PRODUCTION, other],
                execute_item_ids=["acceptance", "production-activation"],
                phase="VC-6",
            )

    def test_actions_must_share_one_attempt(self) -> None:
        other_attempt = copy.deepcopy(ROLLBACK)
        command = list(other_attempt["command"])
        command[command.index(ATTEMPT_ID)] = "20260922T000000Z-ffffffffffffffff"
        other_attempt["command"] = command
        with self.assertRaisesRegex(artifacts.VCArtifactError, "同一 Campaign"):
            artifacts.canonical_batch_binding(
                [PRODUCTION, other_attempt],
                execute_item_ids=["production-activation", "rollback-verification"],
                phase="VC-6",
            )


class ActionPlanAndBatchTest(unittest.TestCase):
    """操作员 action plan 与编译出的 v3 批次都按同一闭包校验。"""

    def _plan(self, actions: list[dict[str, object]], execute: list[str]) -> dict[str, object]:
        return {
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": sorted(execute),
            "reuse_item_ids": ["candidate-core-direct", "candidate-core-mitm"],
            "actions": actions,
        }

    def test_vc6_action_plan_validates(self) -> None:
        plan = artifacts.validate_action_plan(
            self._plan([PRODUCTION, ROLLBACK], ["production-activation", "rollback-verification"]),
            phase="VC-6",
        )
        self.assertEqual(len(plan["actions"]), 2)

    def test_vc6_action_plan_rejected_for_vc5_phase(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不得编入 VC-5 批次"):
            artifacts.validate_action_plan(
                self._plan([PRODUCTION], ["production-activation"]),
                phase="VC-5",
            )

    def test_compiled_batch_round_trips(self) -> None:
        plan = {
            "schema_version": artifacts.CAMPAIGN_PLAN_SCHEMA,
            "campaign_id": "c0154-formal",
            "plan_sha256": "b" * 64,
            "original_deadline_at_utc": "2026-12-01T00:00:00+00:00",
        }
        with mock.patch.object(
            artifacts, "validate_campaign_plan", return_value=plan
        ):
            batch = artifacts.build_vc_batch(
                campaign_plan=plan,
                phase="VC-6",
                sequence=18,
                predecessor_checkpoint={
                    "path": "control/vc/vc-5-checkpoint.json",
                    "sha256": "c" * 64,
                    "phase": "VC-5",
                    "checkpoint_sha256": "d" * 64,
                },
                execute_item_ids=["production-activation", "rollback-verification"],
                reuse_item_ids=["candidate-core-direct"],
                actions=[PRODUCTION, ROLLBACK],
                compiled_at_utc="2026-09-22T13:00:00+00:00",
                must_start_by_utc="2026-09-22T13:01:00+00:00",
                candidate_revision=1,
                candidate_id=CANDIDATE_ID,
            )
        self.assertEqual(batch["phase"], "VC-6")
        self.assertEqual(batch["batch_id"], "vc-6-0018")
        binding = artifacts.canonical_batch_binding(
            batch["actions"], execute_item_ids=batch["execute_item_ids"], phase="VC-6"
        )
        assert binding is not None
        self.assertEqual(binding["group"], "VC-6")


class PostRunToolingClassificationTest(unittest.TestCase):
    """VC-6 三步失败必须仍归入可恢复的 post-run-tooling 闭集。"""

    def test_vc6_items_are_post_run_tooling_allowed(self) -> None:
        for item in ("production-activation", "rollback-verification", "retire-0.149.1"):
            self.assertTrue(supervisor._post_run_tooling_item_allowed(item), item)

    def test_vc5_items_remain_allowed(self) -> None:
        for item in ("candidate-seal", "compare", "acceptance", "canonical-accept", "assert-SPEC-EP-019"):
            self.assertTrue(supervisor._post_run_tooling_item_allowed(item), item)

    def test_capture_items_remain_rejected(self) -> None:
        for item in ("candidate-core-direct", "retire", "retire-0.149", "official-core"):
            self.assertFalse(supervisor._post_run_tooling_item_allowed(item), item)


if __name__ == "__main__":
    unittest.main()
