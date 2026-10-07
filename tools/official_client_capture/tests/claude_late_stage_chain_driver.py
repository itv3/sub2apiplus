"""B5b-03 A 组后段连续链的测试驱动，不单独登记认证入口。

同一 Campaign 从已有 VC-3／VC-4 批次夹具经真实 compare、builder、accept 进入 VC-6。
本文件沿用 claude_vc5_chain_fixture 明示的官方来源、候选来源及画像接缝，以及
claude_vc6_delivery_fixture 的外部环境执行器；不将这些输入接缝算作真机取证。
批次提交、恢复对账、交付阶段失效、Store 收据和 checkpoint 都执行产品代码。

这里先覆盖交付半链，完整 A 组及 ARM64 时长验收完成前不登记 late-stage-faults 通过。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests import claude_vc6_delivery_fixture as environment
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_vc6_batch as delivery_tests

CANDIDATE = delivery_tests.CANDIDATE
CRASH_MARKER_ENV = "CLAUDE_LATE_CHAIN_FINALIZE_CRASH_MARKER"
# 只在指定 finalize 动作进程、且 Store 已签出收据后的一次性故障；不改变成功判据。
FINALIZE_CRASH = f'''
if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") == "{delivery.FINALIZE_ACTION_ID}" and os.environ.get("{CRASH_MARKER_ENV}"):
    import signal
    from pathlib import Path
    from tools.official_client_capture import claude_upgrade_vc
    _original_complete = claude_upgrade_vc.complete_candidate_phase

    def _complete_after_one_crash(*args, **kwargs):
        marker = Path(os.environ["{CRASH_MARKER_ENV}"])
        if not marker.exists():
            marker.write_text("交付收据已签出，checkpoint 发布前强杀\\n", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        return _original_complete(*args, **kwargs)

    claude_upgrade_vc.complete_candidate_phase = _complete_after_one_crash
'''


class ClaudeLateStageChainDriver(batch_tests._RealTreeBatchCase):
    """借用输入构造和命令构造，不继承原测试用例，避免每个外层场景重复运行旧用例。"""

    vc5_world = delivery_tests.Vc6BatchChainTests.vc5_world
    dispatch_vc5 = delivery_tests.Vc6BatchChainTests.dispatch_vc5
    compare_action = delivery_tests.Vc6BatchChainTests.compare_action
    assert_action = delivery_tests.Vc6BatchChainTests.assert_action
    accept_action = delivery_tests.Vc6BatchChainTests.accept_action
    gate_receipt = delivery_tests.Vc6BatchChainTests.gate_receipt
    run_compare_and_assert = delivery_tests.Vc6BatchChainTests.run_compare_and_assert
    vc5_complete_world = delivery_tests.Vc6BatchChainTests.vc5_complete_world
    set_control = delivery_tests.Vc6BatchChainTests.set_control
    assert_status = delivery_tests.Vc6BatchChainTests.assert_status
    delivery_calls = delivery_tests.Vc6BatchChainTests.delivery_calls
    assert_batch_failed_and_reconcile = delivery_tests.Vc6BatchChainTests.assert_batch_failed_and_reconcile

    def run_action(self, world: dict[str, Any]) -> dict[str, Any]:
        return delivery.run_action(world["campaign"], world.get("candidate_id", CANDIDATE),
            environment_executor=environment.EXECUTOR_NAME, observation_window_seconds=60)

    def candidate_status(self, world: dict[str, Any]) -> dict[str, Any]:
        with chain.seams(world["config_path"]):
            return recovery.campaign_status(world["campaign"], world.get("candidate_id", CANDIDATE))

    def dispatch_vc6(self, world: dict[str, Any], sequence: int, action: dict[str, Any], *, crash_after_receipt: bool = False) -> tuple[int, dict | None, str]:
        """同一父批次入口；只给 finalize 动作的 usercustomize 增加一次性强杀点。"""

        campaign = world["campaign"]
        plan = chain._write_json(self.root / "action-plans" / f"vc-6-{sequence:04d}.json", delivery.vc6_action_plan(action))
        injection = self.root / "inject-vc6"
        variables = environment.subprocess_environment(injection, self.control, [str(action["action_id"])])
        if crash_after_receipt:
            self.assertEqual(action["action_id"], delivery.FINALIZE_ACTION_ID)
            with (injection / "usercustomize.py").open("a", encoding="utf-8") as stream:
                stream.write(FINALIZE_CRASH)
            variables[CRASH_MARKER_ENV] = str(self.root / "finalize-crashed-once")
        with mock.patch.dict(os.environ, variables):
            return batch_tests.run_main([
                "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-6", "--sequence", sequence,
                "--predecessor-checkpoint", vc.vc_checkpoint_path(campaign, "VC-5", revision=world.get("revision", 1)),
                "--action-plan", plan, "--state-dir", self.state_dir,
            ])

    def late_audit(self, name: str, world: dict[str, Any]) -> Any:
        """共用六项审计；每个故障在同一 Campaign 的当前可信状态上取快照。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        campaign = world["campaign"]
        control = campaign / "control" / "vc"
        audit = ScenarioAudit(self, name)
        audit.snapshot(
            trusted=[campaign / "campaign.json", control / "commits", control / "batches",
                     *sorted(control.rglob("*-checkpoint.json"))],
            history=[world["store"].root, self.ledger_dir / "events", self.ledger_dir / "ledger.json",
                     self.project_root / "events", self.project_root / "plan.json", control / "commits", control / "batches"],
            timing_dir=self.ledger_dir, project_root=self.project_root,
        )
        return audit

    def finish_audit(self, audit: Any, *, executed: list[str], expected: list[str], entry: Any,
                     fragments: tuple[str, ...], repeats: list[tuple[str, Any, Any]] | None = None, **facts: Any) -> dict[str, Any]:
        audit.check_trusted()
        audit.check_closure(executed=executed, expected=expected, what="当前 Campaign 的交付动作")
        audit.check_accounting(repeats=repeats or [])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(entry, *fragments)
        return audit.report(**facts)

    def exercise_delivery_faults(self, world: dict[str, Any] | None = None, *, sequence_start: int = 12) -> list[dict[str, Any]]:
        """同一世界连续执行批次提交故障、阶段失败、动作强杀、阶段失效及收据后强杀。"""

        world = self.vc5_complete_world() if world is None else world
        campaign = world["campaign"]
        self.campaign = campaign
        candidate = world.get("candidate_id", CANDIDATE)
        revision = world.get("revision", 1)
        # 调用方可交入同一个已完成 VC-5 的世界；不重新创建 Campaign 或清空前段账本。
        if not hasattr(self, "control"):
            self.control = environment.write_control(self.root / "vc6" / "control.json",
                environment={**environment.ENVIRONMENT, "architecture": world["architecture"]})
        run = self.run_action(world)
        records = []

        # 第一层提交故障：阶段事件已写，但 COMMIT 发布失败；重派保持同一逻辑序号。
        audit = self.late_audit("后段批次 COMMIT 发布中断", world)
        original_publish = upgrade._publish_staging_file
        calls = 0

        def fail_commit(source: Path, target: Path, root: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise upgrade.ConfigurationError("后段连续链注入：COMMIT 发布失败")
            original_publish(source, target, root)

        with mock.patch.object(upgrade, "_publish_staging_file", side_effect=fail_commit):
            code, result, stderr = self.dispatch_vc6(world, sequence_start, run)
        self.assertEqual((code, result), (1, None), stderr)
        self.assertIn("COMMIT 发布失败", stderr)
        self.assertEqual(self.delivery_calls(), [])
        self.set_control(world, failures=[["dr0", "candidate_restored", None]])
        retried = self.assert_batch_passed(self.dispatch_vc6(world, sequence_start, run), sequence_start, "VC-6")
        self.assertEqual(retried["staging_attempt"], 2)
        failed = self.assert_status(world, "delivery_stage_failed")
        records.append(self.finish_audit(audit, executed=[f"{a}:{b}" for a, b in self.delivery_calls()],
            expected=[f"dr0:{stage}" for stage in delivery.STAGES[:3]], entry=failed, fragments=("deliver-candidate",),
            campaign_id=campaign.name, staging_attempt=2))

        # 修复阶段三后，阶段四动作被强杀；真实父 run 对账后只重派剩余阶段四。
        audit = self.late_audit("后段交付修复后动作中断", world)
        before_calls = len(self.delivery_calls())
        self.set_control(world, kill_at=["dr1", "stable_observed"])
        self.assert_batch_failed_and_reconcile(self.dispatch_vc6(world, sequence_start + 1, run), delivery.RUN_ACTION_ID)
        interrupted = self.assert_status(world, "delivery_in_progress")
        self.set_control(world)
        self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 2, run), sequence_start + 2, "VC-6")
        self.assert_status(world, "delivery_awaiting_finalize")
        records.append(self.finish_audit(audit, executed=[f"{a}:{b}" for a, b in self.delivery_calls()[before_calls:]],
            expected=["dr1:candidate_restored", "dr1:stable_observed", "dr1:stable_observed"],
            entry=interrupted, fragments=("deliver-candidate",), campaign_id=campaign.name))

        # 已通过阶段只有经批准失效才可再执行，从批准阶段起重做。
        audit = self.late_audit("后段交付阶段批准失效", world)
        before_calls = len(self.delivery_calls())
        arguments = ["--campaign-dir", campaign, "--candidate-id", candidate, "--stage", "rollback_verified",
                     "--reason", "回退检查在相同冻结环境中重新核验"]
        code, preview, stderr = batch_tests.run_main(["delivery-stage-invalidate", "preview", *arguments])
        self.assertEqual((code, preview["status"]), (2, "preview"), stderr)
        code, applied, stderr = batch_tests.run_main(["delivery-stage-invalidate", "apply", *arguments,
            "--approve-sha256", preview["review_sha256"], "--approved-by", "fixture-reviewer"])
        self.assertEqual((code, applied["status"]), (0, "applied"), stderr)
        pending = self.assert_status(world, "delivery_invalidation_pending")
        self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 3, run), sequence_start + 3, "VC-6")
        ready = self.assert_status(world, "delivery_awaiting_finalize")
        records.append(self.finish_audit(audit, executed=[f"{a}:{b}" for a, b in self.delivery_calls()[before_calls:]],
            expected=[f"dr2:{stage}" for stage in delivery.STAGES[1:]], entry=pending,
            fragments=("deliver-candidate",), campaign_id=campaign.name))

        # 第二层提交故障：Store 已签交付收据，VC-6 checkpoint 尚未写；下批只补收尾。
        audit = self.late_audit("后段交付收据后中断与重复收尾", world)
        before_calls = len(self.delivery_calls())
        finalize = delivery.finalize_action(campaign, candidate, ready["delivery"]["review_sha256"])
        self.assert_batch_failed_and_reconcile(self.dispatch_vc6(world, sequence_start + 4, finalize, crash_after_receipt=True), delivery.FINALIZE_ACTION_ID)
        self.assertTrue((self.root / "finalize-crashed-once").is_file())
        receipt_refs = world["store"].list_receipt_refs("candidate_delivery")
        self.assertEqual(len(receipt_refs), 1)
        self.assertFalse(vc.vc_checkpoint_path(campaign, "VC-6", revision=revision).exists())
        interrupted = self.assert_status(world, "delivery_completion_pending")
        self.assert_batch_passed(self.dispatch_vc6(world, sequence_start + 5, finalize), sequence_start + 5, "VC-6")
        checkpoint = vc.vc_checkpoint_path(campaign, "VC-6", revision=revision)
        checkpoint_bytes = checkpoint.read_bytes()
        self.assertEqual(world["store"].list_receipt_refs("candidate_delivery"), receipt_refs)
        before_repeat = audit.accounting_state()
        for _ in range(2):
            code, repeated, stderr = batch_tests.run_main(["deliver-candidate", "finalize", "--campaign-dir", campaign, "--candidate-id", candidate])
            self.assertEqual((code, repeated["status"]), (0, "finalized"), stderr)
        self.assertEqual(checkpoint.read_bytes(), checkpoint_bytes)
        records.append(self.finish_audit(audit, executed=[f"{a}:{b}" for a, b in self.delivery_calls()[before_calls:]], expected=[],
            entry=interrupted, fragments=("逐字重派", "finalize"),
            repeats=[("重复读取已完成 finalize", before_repeat, audit.accounting_state())], campaign_id=campaign.name,
            receipt_sha256=receipt_refs[0]["sha256"]))
        self.assert_status(world, "ready_for_operator_release")
        self.assert_zero_requests()
        self.assertEqual(len({row["campaign_id"] for row in records}), 1)
        return records
