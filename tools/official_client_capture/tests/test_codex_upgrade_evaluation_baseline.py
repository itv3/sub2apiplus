"""改造 5（评估失败局部恢复）M1 的函数级合同测试。

覆盖：batch v3／动作 output_bindings 闭集（T5.1）、账本四类事件（T5.2）、stop-receipt v2 取值合同与
v1 只读兼容、动作输出绑定 write-once、R2 四层判定与 monitor 封存前后复算一致（T5.3）、
当前基线判定与 stage_sources 读写分离（T5.4）、checker 投影构造与等价证明（T5.5）。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import candidate_rule_assertion as assertion
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

DIGESTS = {
    "checker_sha256": "a1" * 32,
    "builder_sha256": "b2" * 32,
    "compare_reader_sha256": "c3" * 32,
    "accept_reader_sha256": "d4" * 32,
}


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class BatchV3ContractTests(unittest.TestCase):
    """T5.1：batch v3 三字段与动作 output_bindings。"""

    def _plan(self) -> dict:
        return artifacts.build_campaign_plan(
            campaign_id="codex-0_154_0-campaign",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-14T00:00:00Z",
            original_deadline_at_utc="2099-09-14T12:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )

    def _common(self, plan: dict, phase: str, sequence: int) -> dict:
        predecessor_phase = artifacts.VC_PHASES[artifacts.VC_PHASES.index(phase) - 1]
        return dict(
            campaign_plan=plan,
            sequence=sequence,
            predecessor_checkpoint={
                "path": f"control/vc/{predecessor_phase.lower()}-checkpoint.json",
                "sha256": "5" * 64,
                "phase": predecessor_phase,
                "checkpoint_sha256": "6" * 64,
            },
            execute_item_ids=["item"],
            reuse_item_ids=[],
            actions=[{"action_id": "item", "operation": f"{phase}:item", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["item"]}],
            compiled_at_utc="2026-09-14T01:00:00Z",
            must_start_by_utc="2026-09-14T01:01:00Z",
        )

    def test_vc5_requires_evaluator_digests_and_pairs_baseline_fields(self) -> None:
        plan = self._plan()
        common = self._common(plan, "VC-5", 5)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "evaluator_digests"):
            artifacts.build_vc_batch(phase="VC-5", candidate_revision=1, candidate_id="cand", **common)
        b0 = artifacts.build_vc_batch(phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS, **common)
        self.assertEqual((b0["evaluation_baseline"], b0["baseline_commit_sha256"]), (None, None))
        self.assertEqual(b0["evaluator_digests"], DIGESTS)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "成对"):
            artifacts.build_vc_batch(phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS, evaluation_baseline=1, **common)
        b1 = artifacts.build_vc_batch(
            phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS,
            evaluation_baseline=1, baseline_commit_sha256="7" * 64, **common,
        )
        self.assertEqual(b1["evaluation_baseline"], 1)
        # 非 VC-5 阶段三字段必须 null。
        vc4 = self._common(plan, "VC-4", 4)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "非 VC-5"):
            artifacts.build_vc_batch(phase="VC-4", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS, **vc4)
        ok = artifacts.build_vc_batch(phase="VC-4", candidate_revision=1, candidate_id="cand", **vc4)
        self.assertIsNone(ok["evaluator_digests"])
        # 摘要闭集：缺一项即拒。
        with self.assertRaisesRegex(artifacts.VCArtifactError, "evaluator_digests 字段不闭合"):
            artifacts.build_vc_batch(
                phase="VC-5", candidate_revision=1, candidate_id="cand",
                evaluator_digests={k: v for k, v in DIGESTS.items() if k != "builder_sha256"}, **common,
            )

    def test_action_output_bindings_closed_set(self) -> None:
        plan = self._plan()
        common = self._common(plan, "VC-5", 5)
        action = dict(common["actions"][0], output_bindings=["assertions/cand/evaluation-run.json", "assertions/cand/checkpoints"])
        batch = artifacts.build_vc_batch(
            phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS,
            **{**common, "actions": [action]},
        )
        self.assertEqual(batch["actions"][0]["output_bindings"], ["assertions/cand/checkpoints", "assertions/cand/evaluation-run.json"])
        for bad in (["/abs/path"], ["a/../b"], [], ["x", "x"]):
            with self.assertRaises(artifacts.VCArtifactError):
                artifacts.build_vc_batch(
                    phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS,
                    **{**common, "actions": [dict(common["actions"][0], output_bindings=bad)]},
                )
        # v2 批次不得携带 output_bindings。
        v2 = {key: value for key, value in batch.items() if key not in {"batch_sha256", "evaluation_baseline", "baseline_commit_sha256", "evaluator_digests"}}
        v2["schema_version"] = artifacts.VC_BATCH_V2_SCHEMA
        v2["batch_sha256"] = artifacts.digest(v2)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "字段不闭合"):
            artifacts.validate_vc_batch(v2, plan)
        # action plan 允许 output_bindings。
        plan_doc = artifacts.validate_action_plan(
            {"schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": ["item"], "reuse_item_ids": [], "actions": [action]}
        )
        self.assertIn("output_bindings", plan_doc["actions"][0])

    def test_run_manifest_carries_evaluation_fields_and_output_bindings(self) -> None:
        plan = self._plan()
        common = self._common(plan, "VC-5", 5)
        action = dict(common["actions"][0], output_bindings=["assertions/cand/evaluation-run.json"])
        batch = artifacts.build_vc_batch(
            phase="VC-5", candidate_revision=1, candidate_id="cand", evaluator_digests=DIGESTS,
            **{**common, "actions": [action]},
        )
        manifest = codex_upgrade._vc_run_manifest_from_batch(batch, batch_model="staging")
        self.assertEqual(manifest["evaluator_digests"], DIGESTS)
        self.assertIsNone(manifest["evaluation_baseline"])
        self.assertEqual(manifest["actions"][0]["output_bindings"], ["assertions/cand/evaluation-run.json"])
        with self.assertRaisesRegex(supervisor.SupervisorError, "同时给出"):
            supervisor.build_batched_campaign_run_manifest(
                campaign_id=batch["campaign_id"], campaign_plan_sha256=batch["campaign_plan_sha256"],
                batch_id=batch["batch_id"], batch_sequence=5, batch_sha256=batch["batch_sha256"], phase="VC-5",
                predecessor_checkpoint=batch["predecessor_checkpoint"], original_deadline_at_utc=batch["original_deadline_at_utc"],
                actions=batch["actions"], execute_items=["item"], reuse_items=[],
                candidate_revision=1, candidate_id="cand", evaluation_baseline=None,
            )
        with self.assertRaisesRegex(supervisor.SupervisorError, "字段不闭合|output_bindings"):
            supervisor.build_batched_campaign_run_manifest(
                campaign_id=batch["campaign_id"], campaign_plan_sha256=batch["campaign_plan_sha256"],
                batch_id=batch["batch_id"], batch_sequence=5, batch_sha256=batch["batch_sha256"], phase="VC-5",
                predecessor_checkpoint=batch["predecessor_checkpoint"], original_deadline_at_utc=batch["original_deadline_at_utc"],
                actions=[dict(action, output_bindings=["../escape"])], execute_items=["item"], reuse_items=[],
                candidate_revision=1, candidate_id="cand", evaluation_baseline=None, baseline_commit_sha256=None, evaluator_digests=DIGESTS,
            )


class TimingLedgerEvaluationEventsTests(unittest.TestCase):
    """T5.2：evaluation_baseline 与 attempt_recovery_* 四类事件。"""

    START = "2026-08-30T00:00:00+00:00"

    def _create(self, root: Path) -> None:
        ledger.create_ledger(
            root, upgrade_id="codex-0154", baseline_version="0.151.0", target_version="0.154.0",
            campaign_purpose="validation_only", evidence_decision="reuse", started_at_utc=self.START,
        )

    @staticmethod
    def _at(minutes: int) -> str:
        return (datetime(2026, 8, 30, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()

    def _to_vc5(self, root: Path) -> int:
        # 账本创建即处于 VC-0 active；先完成 VC-0，再依次开关 VC-1～VC-3。
        minute = 1
        ledger.append_event(root, event_id="c0", phase="VC-0", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute))
        for phase in ("VC-1", "VC-2", "VC-3"):
            minute += 1
            ledger.append_event(root, event_id=f"s-{phase}", phase=phase, event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute))
            minute += 1
            ledger.append_event(root, event_id=f"c-{phase}", phase=phase, event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute))
        minute += 1
        ledger.append_event(root, event_id="r1", phase="VC-4", event_type="stage_revision", revision=1, candidate_id="cand-a", revision_commit_sha256="a" * 64, next_action="x", recorded_at_utc=self._at(minute))
        ledger.append_event(root, event_id="s4", phase="VC-4", event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute + 1))
        ledger.append_event(root, event_id="c4", phase="VC-4", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute + 2))
        ledger.append_event(root, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute + 3))
        return minute + 4

    def test_evaluation_baseline_event_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            minute = self._to_vc5(root)
            base = dict(phase="VC-5", event_type="evaluation_baseline", candidate_id="cand-a", next_action="x")
            # 缺字段／非 VC-5／attempt-recovery 缺 recovery_revision 各拒。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "evaluation_baseline 必须在 VC-5"):
                ledger.append_event(root, event_id="b1-bad", recorded_at_utc=self._at(minute), **{**base, "evaluation_baseline": 1, "baseline_commit_sha256": "1" * 64})
            with self.assertRaisesRegex(ledger.TimingLedgerError, "attempt-recovery 基线携带 recovery_revision"):
                ledger.append_event(root, event_id="b1-bad2", recorded_at_utc=self._at(minute), **{**base, "evaluation_baseline": 1, "baseline_commit_sha256": "1" * 64, "baseline_kind": "attempt-recovery"})
            with self.assertRaisesRegex(ledger.TimingLedgerError, "不接受评估基线"):
                ledger.append_event(root, event_id="rp-bad", phase="VC-5", event_type="receipt_passed", evaluation_baseline=1, next_action="x", recorded_at_utc=self._at(minute))
            summary = ledger.append_event(root, event_id="b1", recorded_at_utc=self._at(minute), **{**base, "evaluation_baseline": 1, "baseline_commit_sha256": "1" * 64, "baseline_kind": "evaluator-only"})
            self.assertEqual(summary["status"], "active")
            self.assertEqual(summary["active_phase"], "VC-5")
            self.assertEqual(summary["current_evaluation_baseline"]["evaluation_baseline"], 1)
            self.assertEqual(summary["current_evaluation_baseline"]["baseline_kind"], "evaluator-only")
            # 编号必须递增。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "必须大于当前基线"):
                ledger.append_event(root, event_id="b1-again", recorded_at_utc=self._at(minute + 1), **{**base, "evaluation_baseline": 1, "baseline_commit_sha256": "2" * 64, "baseline_kind": "evaluator-only"})
            summary = ledger.append_event(root, event_id="b3", recorded_at_utc=self._at(minute + 1), **{**base, "evaluation_baseline": 3, "baseline_commit_sha256": "3" * 64, "baseline_kind": "attempt-recovery", "recovery_revision": "ar1"})
            self.assertEqual(summary["current_evaluation_baseline"]["recovery_revision"], "ar1")
            # checkpoint 回放包含新字段；旧 checkpoint（无新字段、无基线）仍可回放。
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(minute + 2))
            ledger._write_once(root / "receipts" / "b.json", receipt)
            self.assertEqual(ledger.replay(root, "receipts/b.json"), receipt)

    def test_attempt_recovery_segment_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            minute = self._to_vc5(root)
            ledger.append_event(root, event_id="att-s", phase="VC-5", event_type="attempt_started", attempt_id="att-1", next_action="x", recorded_at_utc=self._at(minute))
            ledger.append_event(root, event_id="att-c", phase="VC-5", event_type="attempt_completed", attempt_id="att-1", next_action="x", recorded_at_utc=self._at(minute + 1))
            start = dict(phase="VC-5", event_type="attempt_recovery_started", attempt_id="att-1", candidate_id="cand-a", recovery_revision="ar1", next_action="x")
            # 没有 attempt-recovery 基线不得开段。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "attempt_recovery_started 必须承接"):
                ledger.append_event(root, event_id="ar1-early", recorded_at_utc=self._at(minute + 2), **start)
            ledger.append_event(root, event_id="b1", phase="VC-5", event_type="evaluation_baseline", candidate_id="cand-a", evaluation_baseline=1, baseline_commit_sha256="1" * 64, baseline_kind="attempt-recovery", recovery_revision="ar1", next_action="x", recorded_at_utc=self._at(minute + 2))
            # 段编号必须等于基线冻结的 ar<k>。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "attempt_recovery_started 必须承接"):
                ledger.append_event(root, event_id="ar2-bad", recorded_at_utc=self._at(minute + 3), **{**start, "recovery_revision": "ar2"})
            summary = ledger.append_event(root, event_id="ar1-s", recorded_at_utc=self._at(minute + 3), **start)
            self.assertEqual(summary["attempt_recoveries"]["att-1:ar1"]["status"], "active")
            # 段 active 时阶段不得关闭、同段不得重开、基线不得切换。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "stage_completed"):
                ledger.append_event(root, event_id="c5-early", phase="VC-5", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute + 4))
            with self.assertRaisesRegex(ledger.TimingLedgerError, "同段不得重开"):
                ledger.append_event(root, event_id="ar1-again", recorded_at_utc=self._at(minute + 4), **start)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "evaluation_baseline 只能在"):
                ledger.append_event(root, event_id="b2-early", phase="VC-5", event_type="evaluation_baseline", candidate_id="cand-a", evaluation_baseline=2, baseline_commit_sha256="2" * 64, baseline_kind="evaluator-only", next_action="x", recorded_at_utc=self._at(minute + 4))
            # 失败必须带根因并计入同根因计数。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "必须登记 root_cause_id"):
                ledger.append_event(root, event_id="ar1-f-bad", phase="VC-5", event_type="attempt_recovery_failed", attempt_id="att-1", candidate_id="cand-a", recovery_revision="ar1", next_action="x", recorded_at_utc=self._at(minute + 4))
            summary = ledger.append_event(root, event_id="ar1-f", phase="VC-5", event_type="attempt_recovery_failed", attempt_id="att-1", candidate_id="cand-a", recovery_revision="ar1", root_cause_id="rc1-t", next_action="x", recorded_at_utc=self._at(minute + 4))
            self.assertEqual(summary["attempt_recoveries"]["att-1:ar1"]["status"], "failed")
            self.assertEqual(summary["same_root_cause_failures"], {"rc1-t": 1})
            # 段已终态后 completed 被拒。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "没有对应的 active 恢复段"):
                ledger.append_event(root, event_id="ar1-c", phase="VC-5", event_type="attempt_recovery_completed", attempt_id="att-1", candidate_id="cand-a", recovery_revision="ar1", next_action="x", recorded_at_utc=self._at(minute + 5))

    def test_revision_switch_resets_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            minute = self._to_vc5(root)
            ledger.append_event(root, event_id="b1", phase="VC-5", event_type="evaluation_baseline", candidate_id="cand-a", evaluation_baseline=1, baseline_commit_sha256="1" * 64, baseline_kind="evaluator-only", next_action="x", recorded_at_utc=self._at(minute))
            ledger.append_event(root, event_id="a5", phase="VC-5", event_type="stage_abandoned", root_cause_id="rc1-a", next_action="x", recorded_at_utc=self._at(minute + 1))
            ledger.append_event(root, event_id="inv", phase="VC-5", event_type="candidate_invalidated", candidate_id="cand-a", root_cause_id="rc1-inv", next_action="x", recorded_at_utc=self._at(minute + 2))
            summary = ledger.append_event(root, event_id="r2", phase="VC-4", event_type="stage_revision", revision=2, candidate_id="cand-b", revision_commit_sha256="b" * 64, supersedes_revision=1, next_action="x", recorded_at_utc=self._at(minute + 3))
            self.assertIsNone(summary["current_evaluation_baseline"])
            self.assertEqual(summary["attempt_recoveries"], {})


class StopReceiptAndActionOutputsTests(unittest.TestCase):
    """T5.3：stop-receipt v2 取值合同、v1 只读兼容、动作输出绑定、R2 四层判定。"""

    def _run_dir(self, root: Path, *, manifest_actions: list[dict], owner_nonce: str = "8" * 64) -> tuple[Path, dict, dict]:
        run_dir = root / "run-1"
        run_dir.mkdir(mode=0o700)
        inner = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": "campaign-r2",
            "phase": "VC-5",
            "actions": manifest_actions,
        }
        manifest_sha256 = supervisor._sha256(supervisor._canonical(inner))
        supervisor._write_json(run_dir / "campaign-run-manifest.json", {"schema_version": inner["schema_version"], "manifest_sha256": manifest_sha256, "manifest": inner}, replace=False)
        state = {"state": "running", "campaign_id": "campaign-r2", "phase": "VC-5", "owner_pid": 1, "owner_nonce": owner_nonce}
        return run_dir, inner, state

    def _write_events(self, run_dir: Path, events: list[tuple[str, str | None]], *, campaign_id: str = "campaign-r2", owner_nonce: str = "8" * 64) -> None:
        for event_type, job_id in events:
            supervisor._append_event(
                run_dir, event_type=event_type, operation="VC-5:x", owner_pid=1, owner_nonce=owner_nonce,
                campaign_id=campaign_id, phase="VC-5", job_id=job_id,
                status="failed" if event_type == "action-failed" else "running", reason=None,
            )

    def _write_diagnostic(self, run_dir: Path, action_id: str, *, owner_nonce: str = "8" * 64) -> Path:
        path = run_dir / "action-diagnostics" / f"action-{action_id}-failure.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        supervisor._write_action_diagnostic(
            path, campaign_id="campaign-r2", phase="VC-5", action_id=action_id, owner_pid=1, owner_nonce=owner_nonce,
            failure_kind="child-returncode", error_type="ChildProcessError", message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
        )
        return path

    def test_stop_receipt_v2_value_contract_and_v1_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, inner, state = self._run_dir(root, manifest_actions=[{"action_id": "assert-1", "operation": "VC-5:assert", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["assert-1"], "output_bindings": ["assertions/cand/evaluation-run.json"]}])
            campaign_dir = root / "campaign"
            (campaign_dir / "assertions" / "cand").mkdir(parents=True)
            (campaign_dir / "assertions" / "cand" / "evaluation-run.json").write_text("{}", encoding="utf-8")
            binding = supervisor.write_action_output_binding(
                run_dir, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-1",
                run_manifest_sha256=supervisor._sha256(supervisor._canonical(inner)), owner_nonce="8" * 64,
                output_bindings=["assertions/cand/evaluation-run.json"],
            )
            self.assertEqual(binding["bindings"][0]["exists"], True)
            # write-once：同事实幂等，不同事实拒绝。
            again = supervisor.write_action_output_binding(
                run_dir, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-1",
                run_manifest_sha256=supervisor._sha256(supervisor._canonical(inner)), owner_nonce="8" * 64,
                output_bindings=["assertions/cand/evaluation-run.json"],
            )
            self.assertEqual(again["binding_sha256"], binding["binding_sha256"])
            with self.assertRaisesRegex(supervisor.SupervisorError, "禁止覆盖"):
                supervisor.write_action_output_binding(
                    run_dir, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-1",
                    run_manifest_sha256="9" * 64, owner_nonce="8" * 64, output_bindings=["assertions/cand/evaluation-run.json"],
                )
            # 取值合同：action-failed 绑定；文件缺失 null；其他终态固定 null；不一致抛错。
            self.assertEqual(supervisor.action_outputs_sha256_for_reason(run_dir, "action-failed:assert-1"), binding["binding_sha256"])
            self.assertIsNone(supervisor.action_outputs_sha256_for_reason(run_dir, "action-failed:other"))
            self.assertIsNone(supervisor.action_outputs_sha256_for_reason(run_dir, "queue-complete"))
            with self.assertRaisesRegex(supervisor.SupervisorError, "只有 action-failed"):
                supervisor._stop_receipt(run_dir, event_type="stopped", reason="queue-complete", detected_at_epoch=1.0, owner_pid=1, owner_nonce="8" * 64, campaign_id="campaign-r2", phase="VC-5", action_outputs_sha256="1" * 64)
            receipt = supervisor._stop_receipt(run_dir, event_type="failed", reason="action-failed:assert-1", detected_at_epoch=1.0, owner_pid=1, owner_nonce="8" * 64, campaign_id="campaign-r2", phase="VC-5", action_outputs_sha256=binding["binding_sha256"])
            self.assertEqual(receipt["schema_version"], supervisor.STOP_RECEIPT_SCHEMA)
            read = supervisor.read_stop_receipt(run_dir)
            self.assertEqual(read["action_outputs_sha256"], binding["binding_sha256"])
            # 篡改摘要 → 拒绝。
            tampered = dict(receipt, action_outputs_sha256=None)
            supervisor._write_json(run_dir / "stop-receipt.json", tampered, replace=True)
            with self.assertRaisesRegex(supervisor.SupervisorError, "自摘要不一致"):
                supervisor.read_stop_receipt(run_dir)
            # 篡改绑定文件 → 取值抛错（d 层）。
            path = run_dir / "action-outputs" / "assert-1.json"
            corrupted = json.loads(path.read_text(encoding="utf-8"))
            corrupted["action_id"] = "other"
            supervisor._write_json(path, corrupted, replace=True)
            with self.assertRaises(supervisor.SupervisorError):
                supervisor.action_outputs_sha256_for_reason(run_dir, "action-failed:assert-1")
            # v1 只读兼容：归一化 action_outputs_sha256=None，schema 原样。
            v1 = {
                "schema_version": supervisor.STOP_RECEIPT_LEGACY_SCHEMA, "event_type": "failed", "reason": "action-failed:assert-1",
                "detected_at_utc": "2026-09-19T00:00:00Z", "detected_at_epoch": 1.0, "owner_pid": 1, "owner_nonce": "8" * 64,
                "campaign_id": "campaign-r2", "phase": "VC-5",
            }
            v1["receipt_sha256"] = supervisor._sha256(supervisor._canonical(v1))
            legacy_dir = root / "run-legacy"
            legacy_dir.mkdir(mode=0o700)
            supervisor._write_json(legacy_dir / "stop-receipt.json", v1, replace=False)
            read_v1 = supervisor.read_stop_receipt(legacy_dir)
            self.assertIsNone(read_v1["action_outputs_sha256"])
            self.assertEqual(read_v1["schema_version"], supervisor.STOP_RECEIPT_LEGACY_SCHEMA)
            bad_v1 = dict(v1, action_outputs_sha256=None)
            supervisor._write_json(legacy_dir / "stop-receipt.json", bad_v1, replace=True)
            with self.assertRaisesRegex(supervisor.SupervisorError, "字段不闭合"):
                supervisor.read_stop_receipt(legacy_dir)

    def test_orphan_facts_four_layers_and_stable_across_monitor_sealing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            action = {"action_id": "assert-1", "operation": "VC-5:assert", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["assert-1"], "output_bindings": ["assertions/cand/evaluation-run.json"]}
            run_dir, inner, state = self._run_dir(root, manifest_actions=[action])
            manifest_sha256 = supervisor._sha256(supervisor._canonical(inner))
            campaign_dir = root / "campaign"
            (campaign_dir / "assertions" / "cand").mkdir(parents=True)
            (campaign_dir / "assertions" / "cand" / "evaluation-run.json").write_text("{}", encoding="utf-8")
            # a 层不成立：没有诊断。
            self._write_events(run_dir, [("action-started", "assert-1"), ("action-failed", "assert-1")])
            facts = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertFalse(facts["complete"])
            self.assertIn("no-action-diagnostic", facts["reasons"])
            self._write_diagnostic(run_dir, "assert-1")
            # b 层：诊断有效、末条生命周期事件为该动作 action-failed、动作在冻结清单；绑定文件不存在 → null。
            facts = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertTrue(facts["complete"])
            self.assertIsNone(facts["action_outputs_sha256"])
            self.assertFalse(facts["binding_mismatch"])
            # 动作不在冻结清单 → a 层不成立。
            other = supervisor.evaluation_orphan_facts(run_dir, state, {**inner, "actions": []})
            self.assertFalse(other["complete"])
            self.assertIn("action-not-in-frozen-manifest", other["reasons"])
            # c 层：绑定文件有效且声明路径与清单一致。
            binding = supervisor.write_action_output_binding(
                run_dir, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-1",
                run_manifest_sha256=manifest_sha256, owner_nonce="8" * 64, output_bindings=action["output_bindings"],
            )
            before = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertEqual(before["action_outputs_sha256"], binding["binding_sha256"])
            # monitor 封存后追加的非生命周期事件不改变判定（封存前后复算逐字相同）。
            supervisor._append_event(run_dir, event_type="failed", operation="supervisor:owner-check", owner_pid=1, owner_nonce="8" * 64, campaign_id="campaign-r2", phase="VC-5", status="failed", reason="action-failed:assert-1")
            supervisor._stop_receipt(run_dir, event_type="failed", reason="action-failed:assert-1", detected_at_epoch=1.0, owner_pid=1, owner_nonce="8" * 64, campaign_id="campaign-r2", phase="VC-5", action_outputs_sha256=binding["binding_sha256"])
            after = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertEqual(before, after)
            # 末条生命周期事件不是该动作的 action-failed（伪造追加 action-started）→ a 层不成立。
            self._write_events(run_dir, [("action-started", "assert-2")])
            forged = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertFalse(forged["complete"])
            self.assertIn("last-lifecycle-event-not-action-failed", forged["reasons"])
            # d 层：绑定文件声明路径与清单不一致 → binding_mismatch。
            (root / "two").mkdir(mode=0o700)
            run_dir2, inner2, state2 = self._run_dir(root / "two", manifest_actions=[action])
            self._write_events(run_dir2, [("action-started", "assert-1"), ("action-failed", "assert-1")])
            self._write_diagnostic(run_dir2, "assert-1")
            supervisor.write_action_output_binding(
                run_dir2, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-1",
                run_manifest_sha256=supervisor._sha256(supervisor._canonical(inner2)), owner_nonce="8" * 64,
                output_bindings=["assertions/cand/other.json"],
            )
            mismatch = supervisor.evaluation_orphan_facts(run_dir2, state2, inner2)
            self.assertTrue(mismatch["complete"])
            self.assertTrue(mismatch["binding_mismatch"])


class MonitorOrphanSealingTests(unittest.TestCase):
    """T5.3 R2：真实 monitor 子进程在 owner 被 SIGKILL 后按四层判定封存（进程级）。"""

    SCRIPT = Path(supervisor.__file__).resolve()

    def _campaign_start(self, root: Path) -> dict:
        result = subprocess.run(
            [
                sys.executable, str(self.SCRIPT), "campaign-start",
                "--state-dir", str(root / "campaign"), "--campaign-id", "campaign-r2", "--phase", "VC-5",
                "--deadline-seconds", "20", "--initial-operation", "test-planning", "--initial-timeout-seconds", "5",
                "--heartbeat-seconds", "0.05", "--watchdog-timeout-seconds", "0.5", "--ledger-interval-seconds", "0.05",
            ],
            check=False, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    @staticmethod
    def _wait_state(run_dir: Path, expected: set[str], timeout: float = 4.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            if state.get("state") in expected:
                return state
            time.sleep(0.05)
        raise AssertionError(f"monitor 未在预算内进入 {sorted(expected)}：{state}")

    def _prepare_orphan(self, root: Path, *, with_binding: bool, corrupt_binding: bool = False, with_diagnostic: bool = True) -> tuple[Path, dict, Path]:
        payload = self._campaign_start(root)
        run_dir = Path(str(payload["run_dir"]))
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        owner_pid = int(state["owner_pid"])
        owner_nonce = str(state["owner_nonce"])
        action = {"action_id": "assert-rules", "operation": "VC-5:assert", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["assert-rules"], "output_bindings": ["assertions/cand/evaluation-run.json"]}
        inner = {"schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA, "campaign_id": "campaign-r2", "phase": "VC-5", "actions": [action]}
        manifest_sha256 = supervisor._sha256(supervisor._canonical(inner))
        supervisor._write_json(run_dir / "campaign-run-manifest.json", {"schema_version": inner["schema_version"], "manifest_sha256": manifest_sha256, "manifest": inner}, replace=False)
        campaign_dir = root / "campaign-dir"
        (campaign_dir / "assertions" / "cand").mkdir(parents=True)
        (campaign_dir / "assertions" / "cand" / "evaluation-run.json").write_text("{}", encoding="utf-8")
        for event_type, status in (("action-started", "running"), ("action-failed", "failed")):
            supervisor._append_event(run_dir, event_type=event_type, operation="VC-5:assert", owner_pid=owner_pid, owner_nonce=owner_nonce, campaign_id="campaign-r2", phase="VC-5", job_id="assert-rules", status=status, reason=None)
        if with_diagnostic:
            diagnostic = run_dir / "action-diagnostics" / "action-assert-rules-failure.json"
            diagnostic.parent.mkdir(mode=0o700, exist_ok=True)
            supervisor._write_action_diagnostic(diagnostic, campaign_id="campaign-r2", phase="VC-5", action_id="assert-rules", owner_pid=owner_pid, owner_nonce=owner_nonce, failure_kind="child-returncode", error_type="ChildProcessError", message="子命令以非零状态退出，未提供进一步的脱敏诊断。")
        binding: dict = {}
        if with_binding:
            binding = supervisor.write_action_output_binding(
                run_dir, campaign_dir=campaign_dir, campaign_id="campaign-r2", phase="VC-5", action_id="assert-rules",
                run_manifest_sha256=manifest_sha256, owner_nonce=owner_nonce, output_bindings=["assertions/cand/evaluation-run.json"],
            )
            if corrupt_binding:
                path = run_dir / "action-outputs" / "assert-rules.json"
                corrupted = json.loads(path.read_text(encoding="utf-8"))
                corrupted["bindings"][0]["path"] = "assertions/cand/other.json"
                supervisor._write_json(path, corrupted, replace=True)
        os.kill(owner_pid, signal.SIGKILL)
        return run_dir, binding, campaign_dir

    def test_owner_loss_with_complete_identity_is_sealed_as_action_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, binding, _campaign = self._prepare_orphan(root, with_binding=True)
            state = self._wait_state(run_dir, {"failed", "watchdog-aborted"})
            self.assertEqual(state["state"], "failed")
            receipt = supervisor.read_stop_receipt(run_dir)
            self.assertEqual((receipt["event_type"], receipt["reason"]), ("failed", "action-failed:assert-rules"))
            self.assertEqual(receipt["action_outputs_sha256"], binding["binding_sha256"])
            events = supervisor.load_events(run_dir)
            sealing = [e for e in events if e["event_type"] == "failed" and e["operation"] == "supervisor:owner-check"]
            self.assertEqual(len(sealing), 1)
            # 封存后 reconciler 侧同一函数复算结果与封存时一致（stop-receipt 绑定摘要相等）。
            inner = json.loads((run_dir / "campaign-run-manifest.json").read_text(encoding="utf-8"))["manifest"]
            facts = supervisor.evaluation_orphan_facts(run_dir, state, inner)
            self.assertTrue(facts["complete"])
            self.assertEqual(facts["action_outputs_sha256"], receipt["action_outputs_sha256"])
            # 既有历史校验只接受 failed：该 run 可被后继协议当作普通 action-failed 前序。
            self.assertEqual(supervisor._audit_command(run_dir)["state"], "failed")

    def test_owner_loss_without_binding_is_sealed_with_null_and_reusable_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, _binding, _campaign = self._prepare_orphan(root, with_binding=False)
            state = self._wait_state(run_dir, {"failed", "watchdog-aborted"})
            self.assertEqual(state["state"], "failed")
            receipt = supervisor.read_stop_receipt(run_dir)
            self.assertEqual(receipt["reason"], "action-failed:assert-rules")
            self.assertIsNone(receipt["action_outputs_sha256"])

    def test_owner_loss_with_binding_mismatch_or_missing_diagnostic_is_watchdog_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, _binding, _campaign = self._prepare_orphan(root, with_binding=True, corrupt_binding=True)
            state = self._wait_state(run_dir, {"failed", "watchdog-aborted"})
            self.assertEqual(state["state"], "watchdog-aborted")
            self.assertIn("action-output-binding-mismatch", supervisor.read_stop_receipt(run_dir)["reason"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run_dir, _binding, _campaign = self._prepare_orphan(root, with_binding=False, with_diagnostic=False)
            state = self._wait_state(run_dir, {"failed", "watchdog-aborted"})
            self.assertEqual(state["state"], "watchdog-aborted")
            self.assertIn("owner-process-not-alive", supervisor.read_stop_receipt(run_dir)["reason"])


class ReconcilerOrphanBackfillTests(unittest.TestCase):
    """T5.3 R2 第 4 条：monitor 封存的 owner-loss run 由 reconcile-supervisor-run 在锁内补写 post-run-tooling 收据。"""

    def setUp(self) -> None:
        from tools.official_client_capture.tests import test_codex_upgrade

        self.helper = test_codex_upgrade.CodexUpgradeTest("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_backfills_receipt_then_recovers_and_is_idempotent(self) -> None:
        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self.helper._b0_fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            ledger_dir = Path(str(fixture["timing_ledger"]))
            self.helper._b0_advance_ledger_to_vc5(ledger_dir)
            attempt_root = self.helper._b0_completed_candidate_attempt(fixture)
            inner = self.helper._b0_seal_batch_manifest(fixture)
            action_id = str(inner["actions"][0]["action_id"])
            # 真实 owner＋monitor：owner 在诊断落盘后、post-run-tooling 收据前被 SIGKILL。
            script = Path(supervisor.__file__).resolve()
            started = subprocess.run(
                [
                    sys.executable, str(script), "campaign-start",
                    "--state-dir", str(root / "campaign-state"), "--campaign-id", str(fixture["manifest"]["campaign_id"]), "--phase", "VC-5",
                    "--deadline-seconds", "20", "--initial-operation", "test-planning", "--initial-timeout-seconds", "5",
                    "--heartbeat-seconds", "0.05", "--watchdog-timeout-seconds", "0.5", "--ledger-interval-seconds", "0.05",
                ],
                check=False, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(started.returncode, 0, started.stderr or started.stdout)
            payload = json.loads(started.stdout)
            run_dir = Path(str(payload["run_dir"]))
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            owner_pid, owner_nonce = int(state["owner_pid"]), str(state["owner_nonce"])
            supervisor._write_json(run_dir / "campaign-run-manifest.json", {"schema_version": inner["schema_version"], "manifest_sha256": supervisor._sha256(supervisor._canonical(inner)), "manifest": inner}, replace=False)
            for event_type, status in (("action-started", "running"), ("action-failed", "failed")):
                supervisor._append_event(run_dir, event_type=event_type, operation=str(inner["actions"][0]["operation"]), owner_pid=owner_pid, owner_nonce=owner_nonce, campaign_id=str(fixture["manifest"]["campaign_id"]), phase="VC-5", job_id=action_id, status=status, reason=None)
            diagnostic = run_dir / "action-diagnostics" / f"action-{action_id}-failure.json"
            diagnostic.parent.mkdir(mode=0o700, exist_ok=True)
            supervisor._write_action_diagnostic(diagnostic, campaign_id=str(fixture["manifest"]["campaign_id"]), phase="VC-5", action_id=action_id, owner_pid=owner_pid, owner_nonce=owner_nonce, failure_kind="child-returncode", error_type="ChildProcessError", message="子命令以非零状态退出，未提供进一步的脱敏诊断。")
            os.kill(owner_pid, signal.SIGKILL)
            state = MonitorOrphanSealingTests._wait_state(run_dir, {"failed", "watchdog-aborted"})
            self.assertEqual(state["state"], "failed")
            self.assertIsNone(supervisor.read_stop_receipt(run_dir)["action_outputs_sha256"])
            self.assertFalse((run_dir / "action-diagnostics" / f"action-{action_id}-post-run-tooling.json").exists())
            # 对账：锁内复算三项一致 → 补写收据（backfilled）→ post-run-tooling → receipt_passed。
            outcome = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(outcome["status"], "recoverable", outcome)
            receipt = json.loads((campaign_dir / outcome["reconciliation_receipt"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["failure_class"], "post-run-tooling")
            self.assertTrue(receipt["run"]["action_diagnostic"]["post_run_tooling"]["backfilled"])
            self.assertEqual(receipt["run"]["action_diagnostic"]["post_run_tooling"]["attempt_id"], attempt_root.name)
            self.assertTrue((run_dir / "action-diagnostics" / f"action-{action_id}-post-run-tooling.json").is_file())
            self.assertIn("逐字重派", outcome["next_command"])
            summary = ledger.inspect_ledger(ledger_dir)
            self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-5", "redispatch-same-batch"))
            # 幂等：收据已存在即不再补写（backfilled=false），总账不推进。
            again = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(again["status"], "recoverable")
            self.assertTrue(again["batch"]["reused"])
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            backfill = reconciler._backfill_orphaned_action_failure(run_dir, campaign_dir, manifest)
            self.assertIsNotNone(backfill)
            self.assertFalse(backfill["backfilled"])


class StagePathBaselineTests(unittest.TestCase):
    """T5.4：无账本绑定的 Campaign 恒为 b0；stage_sources 读写分离与 reused 规范性。"""

    def _commit(self, campaign_dir: Path, candidate_id: str, number: int, stage_sources: dict) -> dict:
        commit = artifacts.build_evaluation_baseline_commit(
            campaign_id="campaign-x", candidate_id=candidate_id, candidate_revision=1, evaluation_baseline=number,
            kind="evaluator-only", recovery_sha256="1" * 64, authorization_sha256="2" * 64,
            stage_sources=stage_sources, committed_at_utc="2026-09-19T00:00:00Z",
        )
        directory = campaign_dir / "candidates" / candidate_id / "revisions" / f"b{number}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "COMMIT").write_text(json.dumps(commit), encoding="utf-8")
        return commit

    def test_read_source_recurses_and_rejects_non_canonical_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory).resolve()
            candidate = "cand"
            b0_capture = campaign_dir / "candidates" / candidate / "result.json"
            b0_capture.parent.mkdir(parents=True)
            b0_capture.write_text('{"status":"complete"}', encoding="utf-8")
            b0_sha = hashlib.sha256(b0_capture.read_bytes()).hexdigest()
            # 无 campaign.json → 当前基线恒 b0。
            self.assertEqual(codex_upgrade._current_evaluation_baseline(campaign_dir, candidate), (0, None))
            self.assertEqual(codex_upgrade._stage_path(campaign_dir, "capture-candidate", candidate)[1], b0_capture)
            local_sources = {
                "capture-candidate": {"source": "reused", "baseline": 0, "path": "candidates/cand/result.json", "sha256": b0_sha},
                "compare": {"source": "local", "target": "comparisons/cand/revisions/b1/result.json"},
                "assertions": {"source": "local", "target": "assertions/cand/revisions/b1"},
                "accept": {"source": "local", "target": "acceptance/cand/revisions/b1/result.json"},
            }
            self._commit(campaign_dir, candidate, 1, local_sources)
            resolved = codex_upgrade._stage_read_source(campaign_dir, candidate, 1, "capture-candidate")
            self.assertEqual((resolved["baseline_of_record"], resolved["path"], resolved["status"]), (0, b0_capture, "complete"))
            pending = codex_upgrade._stage_read_source(campaign_dir, candidate, 1, "compare")
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(codex_upgrade._stage_write_target(campaign_dir, candidate, 1, "assertions"), campaign_dir / "assertions/cand/revisions/b1")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "禁止写入"):
                codex_upgrade._stage_write_target(campaign_dir, candidate, 1, "capture-candidate")
            # b2 引用 b1 的 compare（b1 local 已生成）→ 递归到 b1；路径不是规范解析结果即拒。
            compare_b1 = campaign_dir / "comparisons/cand/revisions/b1/result.json"
            compare_b1.parent.mkdir(parents=True)
            compare_b1.write_text('{"x":1}', encoding="utf-8")
            compare_sha = hashlib.sha256(compare_b1.read_bytes()).hexdigest()
            self._commit(campaign_dir, candidate, 2, {
                **local_sources,
                "compare": {"source": "reused", "baseline": 1, "path": "comparisons/cand/revisions/b1/result.json", "sha256": compare_sha},
                "assertions": {"source": "local", "target": "assertions/cand/revisions/b2"},
                "accept": {"source": "local", "target": "acceptance/cand/revisions/b2/result.json"},
            })
            resolved = codex_upgrade._stage_read_source(campaign_dir, candidate, 2, "compare")
            self.assertEqual((resolved["baseline_of_record"], resolved["path"]), (1, compare_b1))
            copied = campaign_dir / "comparisons/cand/copy.json"
            copied.write_bytes(compare_b1.read_bytes())
            self._commit(campaign_dir, candidate, 3, {
                **local_sources,
                "compare": {"source": "reused", "baseline": 1, "path": "comparisons/cand/copy.json", "sha256": compare_sha},
                "assertions": {"source": "local", "target": "assertions/cand/revisions/b3"},
                "accept": {"source": "local", "target": "acceptance/cand/revisions/b3/result.json"},
            })
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "规范解析结果"):
                codex_upgrade._stage_read_source(campaign_dir, candidate, 3, "compare")
            # COMMIT 合同：evaluator-only 的 capture 必须 reused；断言／accept 不得 reused；reused 只能指向更小编号。
            with self.assertRaisesRegex(artifacts.VCArtifactError, "capture-candidate 必须 reused"):
                self._commit(campaign_dir, candidate, 4, {**local_sources, "capture-candidate": {"source": "local", "target": "candidates/cand/revisions/b4/result.json"}})
            with self.assertRaisesRegex(artifacts.VCArtifactError, "不得 reused"):
                self._commit(campaign_dir, candidate, 4, {**local_sources, "assertions": {"source": "reused", "baseline": 0, "path": "assertions/cand", "sha256": b0_sha}})
            with self.assertRaisesRegex(artifacts.VCArtifactError, "更小编号"):
                self._commit(campaign_dir, candidate, 4, {**local_sources, "compare": {"source": "reused", "baseline": 4, "path": "comparisons/cand/x.json", "sha256": compare_sha}})


class CheckerProjectionTests(unittest.TestCase):
    """T5.5：投影构造、正文闭包、等价证明与整份模式 checks 一致。"""

    RECORD = {
        "schema_version": assertion.OBSERVATION_SCHEMA_VERSION,
        "record_id": "rec-1",
        "scenario_id": "A03",
        "record_type": "http_request",
        "data": {"method": "POST", "path": "/v1/responses"},
        "source_artifacts": ["raw/a03.bin"],
    }
    OTHER = {
        "schema_version": assertion.OBSERVATION_SCHEMA_VERSION,
        "record_id": "rec-2",
        "scenario_id": "A04",
        "record_type": "http_request",
        "data": {"method": "GET", "path": "/v1/models"},
    }
    PROFILE = {
        "schema_version": assertion.PROFILE_SCHEMA_VERSION,
        "codex_version": "0.154.0",
        "scenarios": [
            {"scenario_id": "A03", "required_artifact_kinds": ["process_trace"]},
            {"scenario_id": "A04", "required_artifact_kinds": ["process_trace"]},
        ],
        "rules": [
            {"rule_id": "SPEC-EP-001", "scenario_ids": ["A03"], "checks": []},
            {"rule_id": "SPEC-EP-002", "scenario_ids": ["A04"], "checks": []},
        ],
    }

    def _bundle(self, root: Path) -> tuple[Path, dict]:
        (root / "raw").mkdir(parents=True)
        (root / "derived").mkdir()
        (root / "raw" / "a03.bin").write_bytes(b"raw-bytes")
        (root / "derived" / "a03.jsonl").write_text(json.dumps(self.RECORD) + "\n", encoding="utf-8")
        (root / "derived" / "a04.jsonl").write_text(json.dumps(self.OTHER) + "\n", encoding="utf-8")

        def artifact(path: str, kind: str, parser: str, scenario: str) -> dict:
            return {"path": path, "sha256": _sha((root / path).read_bytes()), "kind": kind, "parser": parser, "scenario_ids": [scenario], "labels": {"transport": "http"}}

        manifest = {
            "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
            "codex_version": "0.154.0",
            "capture_id": "proj-test",
            "status": "complete",
            "artifacts": [
                artifact("raw/a03.bin", "relay_binary", "opaque_bound_source", "A03"),
                artifact("derived/a03.jsonl", "process_trace", "observation_jsonl", "A03"),
                artifact("derived/a04.jsonl", "process_trace", "observation_jsonl", "A04"),
            ],
        }
        manifest_path = root / "capture-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, manifest

    def test_projection_closure_and_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest_path, manifest = self._bundle(root)
            projection = assertion.project_capture_manifest(self.PROFILE, "SPEC-EP-001", manifest, root, "0.154.0")
            self.assertEqual([a["path"] for a in projection["artifacts"]], ["raw/a03.bin", "derived/a03.jsonl"])
            self.assertEqual(set(projection) , {"schema_version", "codex_version", "capture_id", "status", "artifacts"})
            # 投影模式与整份模式 load_observations 对本规则场景的观测一致。
            projection_path = root / "proj.json"
            projection_path.write_bytes(assertion.canonical_projection_bytes(projection))
            _, full_observations = assertion.load_observations(manifest_path, root, "0.154.0")
            _, projected_observations = assertion.load_observations(projection_path, root, "0.154.0")
            full_a03 = sorted(o.record_id for o in full_observations if o.scenario_id == "A03")
            self.assertEqual(sorted(o.record_id for o in projected_observations), full_a03)
            # 等价证明：伪造投影（漏掉同场景 opaque artifact）逐字不等 → projection-mismatch。
            verified, digest, manifest_digest = assertion.verify_capture_manifest_projection(self.PROFILE, "SPEC-EP-001", manifest_path, projection_path, root, "0.154.0")
            self.assertEqual(digest, _sha(projection_path.read_bytes()))
            self.assertEqual(manifest_digest, _sha(manifest_path.read_bytes()))
            forged = dict(projection, artifacts=[a for a in projection["artifacts"] if a["path"] != "raw/a03.bin"])
            forged_path = root / "forged.json"
            forged_path.write_bytes(assertion.canonical_projection_bytes(forged))
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "projection-mismatch"):
                assertion.verify_capture_manifest_projection(self.PROFILE, "SPEC-EP-001", manifest_path, forged_path, root, "0.154.0")
            # 跨场景引用：A04 记录引用 A03 的原始证据 → 失败关闭。
            cross = dict(self.OTHER, source_artifacts=["raw/a03.bin"])
            (root / "derived" / "a04.jsonl").write_text(json.dumps(cross) + "\n", encoding="utf-8")
            manifest["artifacts"][2]["sha256"] = _sha((root / "derived" / "a04.jsonl").read_bytes())
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "同一场景"):
                assertion.project_capture_manifest(self.PROFILE, "SPEC-EP-002", manifest, root, "0.154.0")
            # 命令合同：投影参数在 --output 之前追加，摘要按完整命令。
            command = assertion.build_assertion_command(rule_id="SPEC-EP-001", capture_manifest="m.json", evidence_root="/r", output="/o.json", capture_manifest_projection="/p.json")
            self.assertEqual(command[-4:], [assertion.PROJECTION_FLAG, "/p.json", "--output", "/o.json"])
            self.assertNotEqual(assertion.command_sha256(command), assertion.command_sha256(assertion.build_assertion_command(rule_id="SPEC-EP-001", capture_manifest="m.json", evidence_root="/r", output="/o.json")))
            # 单规则文档：投影模式两字段必须成对。
            with self.assertRaisesRegex(assertion.AssertionConfigurationError, "同时给出"):
                assertion.build_assertion_result(rule_id="SPEC-EP-001", checks=[], command=command, started_at="2026-09-19T00:00:00Z", finished_at="2026-09-19T00:00:01Z", projection_sha256="1" * 64)


if __name__ == "__main__":
    unittest.main()
