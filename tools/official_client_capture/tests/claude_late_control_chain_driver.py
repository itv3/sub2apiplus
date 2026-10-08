"""在同一 VC-5 世界验证暂停和延期，然后续接 VC-6 故障；尚非完整 A 组认证。

沿用 claude_late_stage_chain_driver 明示的输入接缝。暂停只在真实 compile 返回后注入异常，
预算只把真实门禁的观察时刻推进到冻结截止之后，保留原门禁计算和原始账本字节。
两类故障修好后沿原 Campaign 顺序派发 compare、builder、accept 和四阶段交付。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests


class ClaudeLateControlChainDriver(late.ClaudeLateStageChainDriver):
    """故障点共享一个世界；方法返回六项审计，外层选择是否登记成完整认证场景。"""

    def repeat_root_cause_then_compare(self, world: dict[str, Any], sequence: int) -> dict[str, Any]:
        campaign = world["campaign"]
        self.campaign = campaign
        action = self.compare_action(campaign, candidate_id=world.get("candidate_id", late.CANDIDATE))
        audit = self.late_audit("后段同根因暂停并续派 compare", world)
        original = upgrade.compile_vc_batch

        def fail_after_prepare(*arguments: Any, **options: Any) -> Any:
            original(*arguments, **options)
            raise RuntimeError("后段连续链：compare 批次 prepare 之后中断")

        messages = []
        for _ in range(2):
            with mock.patch.object(upgrade, "compile_vc_batch", side_effect=fail_after_prepare):
                code, result, stderr = self.dispatch_vc5(world, sequence, action)
            self.assertEqual((code, result), (1, None), stderr)
            messages.append(stderr)
        counted = audit.accounting_state()
        old_counts = audit.totals_before["project"]["root_cause_counts"]
        changed = {key: value for key, value in ledger.replay_head(self.project_root)["root_cause_counts"].items()
                   if value != old_counts.get(key, 0)}
        self.assertEqual(len(changed), 1)
        cause, count = next(iter(changed.items()))
        self.assertEqual(count - old_counts.get(cause, 0), 2)
        staging = campaign / "control" / "vc" / "staging" / f"{sequence:04d}-vc-5"
        attempts = sorted(path.name for path in staging.iterdir())
        before_refusal = audit.accounting_state()
        code, _result, stderr = self.dispatch_vc5(world, sequence, action)
        self.assertEqual(code, 1, stderr)
        self.assertIn("根因达上限", stderr)
        self.assertEqual(sorted(path.name for path in staging.iterdir()), attempts)
        after_refusal = audit.accounting_state()

        regression = self.root / "late-control-regression.txt"
        regression.write_text("夹具故障开关已解除；该文件只作为隔离测试修复登记的输入\n", encoding="utf-8")
        deploy = ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261008t030000z")
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        repaired = subprocess.run([
            sys.executable, "-B", "-m", "tools.official_client_capture.codex_upgrade_project_ledger",
            "record-root-cause-repair", "--ledger-dir", str(self.project_root), "--root-cause-id", cause,
            "--kind", "code", "--binding", f"fix_commit_sha={'b' * 40}",
            "--binding", f"regression_receipt_sha256={sha(regression)}",
            "--binding", f"deployment_receipt_sha256={sha(deploy)}", "--note", "后段连续链：解除 prepare 故障后继续",
        ], text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        outcome = self.assert_batch_passed(self.dispatch_vc5(world, sequence, action), sequence, "VC-5")
        self.assertEqual(outcome["staging_attempt"], len(attempts) + 1)
        self.assertEqual(ledger.replay_head(self.project_root)["root_cause_counts"].get(cause), 0)
        batch = json.loads((campaign / "control" / "vc" / "batches" / f"{sequence:04d}-vc-5.json").read_text())
        audit.check_trusted()
        audit.check_closure(executed=batch["execute_item_ids"], expected=["compare"],
            reused=batch["reuse_item_ids"], expected_reused=[], what="同根因修复后的批次")
        audit.check_accounting(root_causes=2, root_cause_state=counted,
            repeats=[("暂停时拒绝派发", before_refusal, after_refusal)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(messages[-1], "record-root-cause-repair", "同序号重新派发")
        return audit.report(campaign_id=campaign.name, root_cause_id=cause, sequence=sequence)

    def expired_budget_then_assert(self, world: dict[str, Any], sequence: int) -> dict[str, Any]:
        """只注入门禁的观察时间；准入判断、延期批准和后续 builder 均执行产品实现。"""
        campaign = world["campaign"]
        comparison = upgrade._load_stage_result(campaign, "compare", world.get("candidate_id", late.CANDIDATE))
        action = self.assert_action(campaign, comparison)
        audit = self.late_audit("后段预算到期并延期续派 builder", world)
        frozen = campaign / "control" / "vc" / "campaign-plan.json"
        deadline = datetime.fromisoformat(json.loads(frozen.read_text())["original_deadline_at_utc"].replace("Z", "+00:00"))
        observed_at = deadline + timedelta(seconds=1)
        original = ledger._campaign_deadline_problems

        def observed_after_deadline(*arguments: Any, **options: Any) -> Any:
            options["now"] = observed_at
            return original(*arguments, **options)

        frozen_paths = (campaign / "campaign.json", frozen, self.ledger_dir / "ledger.json", self.project_root / "plan.json")
        originals = {path: path.read_bytes() for path in frozen_paths}
        before_refusal = audit.accounting_state()
        with mock.patch.object(ledger, "_campaign_deadline_problems", side_effect=observed_after_deadline):
            code, _result, refused = self.dispatch_vc5(world, sequence, action)
        self.assertEqual(code, 1, refused)
        self.assertIn("预算已暂停", refused)
        self.assertFalse((campaign / "control" / "vc" / "staging" / f"{sequence:04d}-vc-5").exists())
        after_refusal = audit.accounting_state()
        new_deadline = (deadline + timedelta(minutes=30)).isoformat()
        code, preview, stderr = batch_tests.run_main([
            "deadline-extend", "preview", "--campaign-dir", campaign, "--scope", "campaign",
            "--new-deadline-at-utc", new_deadline, "--reason", "后段连续链：到期准入拒绝后批准延期",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(preview, stderr)
        code, applied, stderr = batch_tests.run_main([
            "deadline-extend", "apply", "--campaign-dir", campaign, "--preview", preview["preview_path"],
            "--approve-sha256", preview["review_sha256"], "--approved-by", "后段连续链",
        ])
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(applied, stderr)
        self.assert_batch_passed(self.dispatch_vc5(world, sequence, action), sequence, "VC-5")
        self.assertEqual(originals, {path: path.read_bytes() for path in frozen_paths})
        batch = json.loads((campaign / "control" / "vc" / "batches" / f"{sequence:04d}-vc-5.json").read_text())
        self.assertIn("deadline_extension", batch)
        self.assertEqual(batch["original_deadline_at_utc"], json.loads(originals[frozen])["original_deadline_at_utc"])
        audit.check_trusted()
        audit.check_closure(executed=batch["execute_item_ids"], expected=["assert-rules"],
            reused=batch["reuse_item_ids"], expected_reused=[], what="延期后的 builder 批次")
        audit.check_accounting(repeats=[("到期时拒绝派发", before_refusal, after_refusal)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(refused, "批准延期")
        return audit.report(campaign_id=campaign.name, sequence=sequence, observation_at_utc=observed_at.isoformat(),
            observation_injection="真实门禁仅替换 now 输入", extended_to=new_deadline)

    def exercise_control_and_delivery_faults(self, world: dict[str, Any] | None = None, *, sequence_start: int = 9) -> list[dict[str, Any]]:
        """给定 world 时继续原 Campaign，不重建证据、Store 或两本账。"""

        world = self.vc5_world() if world is None else world
        self.campaign = world["campaign"]
        records = [self.repeat_root_cause_then_compare(world, sequence_start), self.expired_budget_then_assert(world, sequence_start + 1)]
        receipt = self.gate_receipt(world, "late-control-candidate-receipt.json")
        self.assert_batch_passed(self.dispatch_vc5(world, sequence_start + 2,
            self.accept_action(self.campaign, receipt, world["gate_root"], candidate_id=world.get("candidate_id", late.CANDIDATE))), sequence_start + 2, "VC-5")
        self.assertTrue(vc.vc_checkpoint_path(self.campaign, "VC-5", revision=world.get("revision", 1)).is_file())
        records.extend(self.exercise_delivery_faults(world, sequence_start=sequence_start + 3))
        self.assert_recorded_client_requests(world)
        self.assertEqual(len({row["campaign_id"] for row in records}), 1)
        return records
