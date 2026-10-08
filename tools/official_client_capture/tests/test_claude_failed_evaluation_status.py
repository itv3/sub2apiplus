"""Claude 当前评估失败的只读状态分流：真实轻量合同，不运行采集、builder 或完整恢复链。

批次经共享解析器校验，队列由 Claude 真实生成器编译；失败事实、正式 COMMIT、动作输出和两本账都落在
临时私有目录中。仅候选当前坐标与已进入 compare 的外层状态在集成入口测试里给定；关键事实验证器不替换。
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

CAMPAIGN = "claude-status-fixture"
CANDIDATE = "claude-candidate-r1"
ACTION = "assert-rules"
NONCE = "e" * 64


def _write(path: Path, payload: Any, *, compact: bool = False) -> Path:
    """测试造合同：所有目录私有，JSON 原件与副本可使用不同空白格式。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=None if compact else 2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resign(path: Path, digest_field: str, **updates: Any) -> None:
    """篡改事实后重签自摘要，证明拒绝来自跨文件绑定而非单文件摘要。"""

    payload = _read(path)
    payload.update(updates)
    payload.pop(digest_field, None)
    payload[digest_field] = artifacts.digest(payload)
    _write(path, payload)


class FailedEvaluationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "staging"
        self.root.mkdir(mode=0o700)
        self.started = datetime.now(timezone.utc) - timedelta(minutes=2)

    def at(self, seconds: int) -> str:
        return (self.started + timedelta(seconds=seconds)).isoformat()

    def make_world(
        self,
        name: str = "world",
        *,
        baseline: int = 0,
        reconciled: bool = True,
        project_event: bool = True,
        timing_event: bool = True,
        output_anchor: bool = True,
    ) -> dict[str, Any]:
        """只建立这次状态判定需要的真实元数据合同；本函数不派发任何动作。"""

        data = self.root / name
        data.mkdir(mode=0o700)
        campaign = data / "evidence" / "campaigns" / CAMPAIGN
        campaign.mkdir(parents=True, mode=0o700)
        project_root = data / project.ledger_dir_name("claude")
        project.create_project_ledger(
            project_root, project_id="fixture-status", absolute_deadline_utc=self.at(24 * 3600),
            deadline_approved_by="fixture", estimation_policy="none", estimation_policy_approved_by="fixture",
            fixture_only=True, client="claude", started_at_utc=self.at(0),
        )
        ledger = data / "timing"
        timing.create_ledger(
            ledger,
            upgrade_id=CAMPAIGN,
            baseline_version="2.1.226",
            target_version="2.1.245",
            campaign_purpose="validation_only",
            evidence_decision="recapture",
            client="claude",
            started_at_utc=self.at(0),
        )
        minute = 1
        timing.append_event(ledger, event_id="vc0-complete", phase="VC-0", event_type="stage_completed", recorded_at_utc=self.at(minute))
        for phase in ("VC-1", "VC-2", "VC-3"):
            minute += 1
            timing.append_event(ledger, event_id=f"{phase}-start", phase=phase, event_type="stage_started", recorded_at_utc=self.at(minute))
            minute += 1
            timing.append_event(ledger, event_id=f"{phase}-complete", phase=phase, event_type="stage_completed", recorded_at_utc=self.at(minute))
        timing.append_event(
            ledger, event_id="r1", phase="VC-4", event_type="stage_revision", revision=1, candidate_id=CANDIDATE,
            revision_commit_sha256="a" * 64, recorded_at_utc=self.at(8),
        )
        timing.append_event(ledger, event_id="vc4-start", phase="VC-4", event_type="stage_started", revision=1, recorded_at_utc=self.at(9))
        timing.append_event(ledger, event_id="vc4-complete", phase="VC-4", event_type="stage_completed", revision=1, recorded_at_utc=self.at(10))
        timing.append_event(ledger, event_id="vc5-start", phase="VC-5", event_type="stage_started", revision=1, recorded_at_utc=self.at(11))
        timing.append_event(
            ledger, event_id="vc5-recovery", phase="VC-5", event_type="recovery_required",
            root_cause_id="campaign-run.action-failed", next_action="reconcile-supervisor-run", recorded_at_utc=self.at(12),
        )
        manifest = {
            "client": "claude",
            "campaign_id": CAMPAIGN,
            "control_receipts": {"upgrade_timing": {"ledger_dir": str(ledger), "ledger_plan_sha256": claude_upgrade.file_sha256(ledger / "ledger.json")}},
        }
        _write(campaign / "campaign.json", manifest)
        index_path = claude_upgrade._assertions_root_for_baseline(campaign, CANDIDATE, baseline) / claude_upgrade.EVALUATION_RUN_FILENAME
        evaluator = {field: "d" * 64 for field in artifacts.EVALUATOR_DIGEST_FIELDS}
        index = {
            "schema_version": artifacts.EVALUATION_RUN_SCHEMA,
            "campaign_id": CAMPAIGN,
            "candidate_id": CANDIDATE,
            "candidate_revision": 1,
            "evaluation_baseline": baseline,
            "derived": False,
            "evaluator": evaluator,
            "rules": [{
                "rule": "SPEC-HDR-001", "validation_mode": "dual_wire", "status": "fail",
                "candidate_checkpoint": {"path": "assertions/checkpoint.json", "sha256": "b" * 64},
                "official_checkpoint": None, "dependency_projection_sha256": "c" * 64, "reused_from": None,
            }],
            "checkpoint_head_sha256": "b" * 64,
            "recorded_at_utc": self.at(12),
        }
        index["run_sha256"] = artifacts.digest(index)
        _write(index_path, artifacts.validate_evaluation_run(index))
        action = {
            "action_id": ACTION, "operation": "VC-5:assert-rules", "timeout_seconds": 60.0,
            "command": [sys.executable, "claude_build_rule_assertion_results.py"], "item_ids": [ACTION],
            "output_bindings": [index_path.relative_to(campaign).as_posix()],
        }
        batch = {
            "schema_version": artifacts.VC_BATCH_SCHEMA,
            "campaign_id": CAMPAIGN, "campaign_plan_sha256": "a" * 64,
            "batch_id": "vc-5-0001", "sequence": 1, "phase": "VC-5",
            "predecessor_checkpoint": {"path": "control/vc/checkpoints/r1/vc-4.json", "sha256": "c" * 64, "phase": "VC-4", "checkpoint_sha256": "b" * 64},
            "execute_item_ids": [ACTION], "reuse_item_ids": [], "actions": [action],
            "compiled_at_utc": self.at(11), "must_start_by_utc": self.at(60), "original_deadline_at_utc": self.at(3600),
            "candidate_revision": 1, "candidate_id": CANDIDATE,
            "evaluation_baseline": baseline or None, "baseline_commit_sha256": "f" * 64 if baseline else None,
            "evaluator_digests": evaluator,
        }
        batch["batch_sha256"] = artifacts.digest(batch)
        batch = artifacts.validate_vc_batch(batch)
        batch_path = _write(campaign / "control" / "vc" / "batches" / "0001-vc-5.json", batch)
        inner = claude_upgrade._vc_run_manifest_from_batch(batch, batch_model="staging")
        manifest_sha = artifacts.digest(inner)
        run = data / "state" / "run-status-failed"
        run.mkdir(parents=True, mode=0o700)
        _write(run / "campaign-run-manifest.json", {"schema_version": inner["schema_version"], "manifest_sha256": manifest_sha, "manifest": inner})
        commit_path = campaign / "control" / "vc" / "commits" / "0001-vc-5.json"
        marker = artifacts.build_staging_prepared_marker(
            campaign_id=CAMPAIGN, sequence=1, phase="VC-5", attempt=1, batch_sha256=batch["batch_sha256"],
            manifest_sha256=manifest_sha, owner_nonce=NONCE, prepared_at_utc=self.at(11),
        )
        state = {
            "schema_version": supervisor.STATE_SCHEMA, "campaign_id": CAMPAIGN, "phase": "VC-5", "state": "failed",
            "owner_pid": os.getpid(), "owner_nonce": NONCE, "started_at_epoch": self.started.timestamp(),
            "started_at_utc": self.at(0), "deadline_at_epoch": (self.started + timedelta(hours=1)).timestamp(),
            "started_monotonic_ns": 1, "deadline_monotonic_ns": 1000000000000,
            "staging_binding": {"campaign_dir": str(campaign), "sequence": 1, "phase": "VC-5", "staging_attempt": 1,
                                "commit_path": str(commit_path), "prepared_marker_sha256": marker["marker_sha256"]},
        }
        supervisor._write_state(run, state)
        _write(commit_path, artifacts.build_vc_commit(
            campaign_id=CAMPAIGN, sequence=1, phase="VC-5", staging_attempt=1, batch_sha256=batch["batch_sha256"],
            manifest_sha256=manifest_sha, parent_run_dir=str(run), owner_nonce=NONCE, ledger_event_ids=[], committed_at_utc=self.at(11),
        ))
        diagnostic = supervisor._action_diagnostic_path(run, ACTION, create_directory=True)
        supervisor._write_action_diagnostic(
            diagnostic, campaign_id=CAMPAIGN, phase="VC-5", action_id=ACTION, owner_pid=os.getpid(), owner_nonce=NONCE,
            failure_kind="handled-error", error_type="ConfigurationError", message="测试当前断言失败。",
        )
        for event_type, status in (("action-started", "running"), ("action-failed", "failed")):
            supervisor._append_event(
                run, event_type=event_type, operation="VC-5:assert-rules", campaign_id=CAMPAIGN, phase="VC-5",
                job_id=ACTION, status=status, owner_pid=os.getpid(), owner_nonce=NONCE,
            )
        outputs = None
        if output_anchor:
            outputs = supervisor.write_action_output_binding(
                run, campaign_dir=campaign, campaign_id=CAMPAIGN, phase="VC-5", action_id=ACTION,
                run_manifest_sha256=manifest_sha, owner_nonce=NONCE, output_bindings=action["output_bindings"],
            )
        supervisor._stop_receipt(
            run, event_type="failed", reason=f"action-failed:{ACTION}", detected_at_epoch=self.started.timestamp() + 12,
            owner_pid=os.getpid(), owner_nonce=NONCE, campaign_id=CAMPAIGN, phase="VC-5",
            action_outputs_sha256=outputs["binding_sha256"] if outputs else None,
        )
        receipt_path = campaign / "control" / "reconciliation" / f"run-{run.name}" / "supervisor-run-reconciliation.json"
        receipt_copy = ledger / "receipts" / "reconciliation.json"
        if reconciled:
            receipt = {
                "schema_version": supervisor.SUPERVISOR_RUN_RECONCILIATION_SCHEMA,
                "campaign_id": CAMPAIGN, "campaign_manifest_sha256": claude_upgrade.file_sha256(campaign / "campaign.json"),
                "failure_class": "execution-failure", "root_cause": {"root_cause_id": "rc1-" + "1" * 20},
                "run": {"run_id": run.name, "state": "failed", "phase": "VC-5", "batch_sequence": 1,
                        "batch_sha256": batch["batch_sha256"], "failure_class": "execution-failure"},
            }
            _write(receipt_path, receipt)
            if project_event:
                project.append_project_event(
                    project_root, operation_id=f"reconcile-supervisor-run:{run.name}", event_type="reconciliation_committed",
                    payload={"campaign_id": CAMPAIGN, "subject_kind": "supervisor_run", "subject_id": run.name,
                             "reconciliation_receipt_sha256": claude_upgrade.file_sha256(receipt_path),
                             "request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0}},
                    source_batch_sha256=batch["batch_sha256"],
                )
            if timing_event:
                _write(receipt_copy, receipt, compact=True)
                provenance = _write(ledger / "receipts" / "provenance.json", {"status": "complete"})
                timing.append_event(
                    ledger, event_id=f"reconcile-run-passed-{run.name}", phase="VC-5", event_type="receipt_passed",
                    receipts=[{"role": role, "path": path.relative_to(ledger).as_posix(), "sha256": claude_upgrade.file_sha256(path)}
                              for role, path in (("provenance", provenance), ("reconciliation", receipt_copy))],
                    next_action="evaluation-recover preview", recorded_at_utc=self.at(13),
                )
        return {"campaign": campaign, "manifest": manifest, "index": index_path, "batch": batch_path,
                "commit": commit_path, "run": run, "project": project_root, "timing": ledger,
                "receipt": receipt_path, "receipt_copy": receipt_copy, "baseline": baseline}

    def status(self, world: dict[str, Any], *, candidate: str = CANDIDATE, revision: int = 1, baseline: int | None = None):
        with mock.patch.object(supervisor, "_campaign_run_command", side_effect=AssertionError("状态查询不得派发动作")):
            return recovery._failed_evaluation_status(
                world["campaign"], world["manifest"], candidate, revision, world["baseline"] if baseline is None else baseline,
            )

    def assert_not_preview(self, result, expected: str) -> None:
        self.assertEqual(result[0], expected, result)
        self.assertNotIn("evaluation-recover preview", result[1])

    def test_current_failed_index_and_reconciled_binding_give_exact_preview(self) -> None:
        for baseline in (0, 1):
            with self.subTest(baseline=baseline):
                world = self.make_world(f"b{baseline}", baseline=baseline)
                self.assertNotEqual(claude_upgrade.file_sha256(world["receipt"]), claude_upgrade.file_sha256(world["receipt_copy"]))
                status, command, extra = self.status(world)
                self.assertEqual((status, extra), ("evaluation_failed", None))
                self.assertIn(f"evaluation-recover preview --campaign-dir {shlex.quote(str(world['campaign']))} --candidate-id {CANDIDATE}", command)
                self.assertIn("--reviewer <审核人>", command)

    def test_each_missing_reconciliation_layer_keeps_reconcile_entry(self) -> None:
        for label, options in (("收据", {"reconciled": False}), ("总账 operation", {"project_event": False}), ("计时通过事件", {"timing_event": False})):
            with self.subTest(label=label):
                world = self.make_world(label, **options)
                result = self.status(world)
                self.assert_not_preview(result, "evaluation_reconciliation_required")
                self.assertIn(f"reconcile-supervisor-run --campaign-dir {shlex.quote(str(world['campaign']))} --run-dir {shlex.quote(str(world['run']))}", result[1])

    def test_missing_output_anchor_requires_same_baseline_redispatch(self) -> None:
        world = self.make_world(output_anchor=False)
        result = self.status(world)
        self.assert_not_preview(result, "evaluation_binding_required")
        self.assertIn("同基线 b0", result[1])
        self.assertIn("逐字重派", result[1])

    def test_no_index_passed_index_and_old_coordinates_do_not_offer_recovery(self) -> None:
        world = self.make_world()
        world["index"].unlink()
        self.assertIsNone(self.status(world))
        self.assertIsNone(self.status(world, candidate="other-candidate"))
        self.assertIsNone(self.status(world, baseline=1))
        successful = self.make_world("success")
        rules = _read(successful["index"])["rules"]
        rules[0]["status"] = "pass"
        _resign(successful["index"], "run_sha256", rules=rules)
        self.assertIsNone(self.status(successful))

    def test_current_path_with_foreign_index_identity_fails_closed(self) -> None:
        for field, value in (("campaign_id", "other-campaign"), ("candidate_id", "other-candidate"), ("candidate_revision", 2), ("evaluation_baseline", 1)):
            with self.subTest(field=field):
                world = self.make_world(field)
                _resign(world["index"], "run_sha256", **{field: value})
                self.assert_not_preview(self.status(world), "evaluation_state_inconsistent")

    def test_valid_self_digests_cannot_replace_batch_or_commit(self) -> None:
        for label, path_key, field, value in (
            ("批次候选", "batch", "candidate_id", "other-candidate"),
            ("COMMIT 清单", "commit", "manifest_sha256", "1" * 64),
            ("COMMIT owner", "commit", "owner_nonce", "1" * 64),
        ):
            with self.subTest(label=label):
                world = self.make_world(label)
                _resign(world[path_key], "batch_sha256" if path_key == "batch" else "commit_sha256", **{field: value})
                self.assert_not_preview(self.status(world), "evaluation_state_inconsistent")

    def test_index_bytes_changed_after_output_seal_fail_closed(self) -> None:
        world = self.make_world()
        _resign(world["index"], "run_sha256", recorded_at_utc=self.at(14))
        self.assert_not_preview(self.status(world), "evaluation_state_inconsistent")

    def test_reconciliation_original_copy_and_event_chain_damage_keep_reconcile(self) -> None:
        for label in ("原件摘要", "副本摘要", "副本事实", "事件链"):
            with self.subTest(label=label):
                world = self.make_world(label)
                if label == "原件摘要":
                    receipt = _read(world["receipt"])
                    receipt["root_cause"]["root_cause_id"] = "rc1-" + "2" * 20
                    _write(world["receipt"], receipt)
                elif label in {"副本摘要", "副本事实"}:
                    receipt = _read(world["receipt_copy"])
                    receipt["run"]["run_id"] = "run-other"
                    _write(world["receipt_copy"], receipt)
                    if label == "副本事实":
                        last = world["timing"] / "events" / "000014.json"
                        event = _read(last)
                        next(row for row in event["receipts"] if row["role"] == "reconciliation")["sha256"] = claude_upgrade.file_sha256(world["receipt_copy"])
                        _write(last, event)
                else:
                    last = world["timing"] / "events" / "000014.json"
                    event = _read(last)
                    event["previous_event_sha256"] = "0" * 64
                    _write(last, event)
                self.assert_not_preview(self.status(world), "evaluation_reconciliation_required")

    def test_status_preserves_all_existing_history_bytes(self) -> None:
        world = self.make_world()
        data = world["project"].parent
        before = {p.relative_to(data): (p.read_bytes(), p.stat().st_mtime_ns) for p in data.rglob("*") if p.is_file()}
        self.assertEqual(self.status(world)[0], "evaluation_failed")
        after = {p.relative_to(data): (p.read_bytes(), p.stat().st_mtime_ns) for p in data.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_outer_status_keeps_segment_compare_and_successful_accept_precedence(self) -> None:
        from tools.official_client_capture import claude_upgrade_attempt_segment as segment
        from tools.official_client_capture import claude_upgrade_delivery as delivery

        world = self.make_world()
        campaign = world["campaign"]
        compared = _write(campaign / "comparisons" / CANDIDATE / "result.json", {"status": "complete"})
        accepted = campaign / "acceptance" / CANDIDATE / "result.json"
        def stage_path(_campaign: Path, stage: str, _candidate: str):
            return stage, compared if stage == "compare" else accepted
        with mock.patch.object(segment, "segment_status", return_value=None), mock.patch.object(claude_upgrade, "_stage_path", side_effect=stage_path), mock.patch.object(
            claude_upgrade, "_require_candidate_revision", return_value=(1, {})
        ), mock.patch.object(claude_upgrade, "_current_evaluation_baseline", return_value=(0, None)):
            self.assertEqual(recovery._evaluation_and_delivery_status(campaign, world["manifest"], CANDIDATE)[0], "evaluation_failed")
            with mock.patch.object(segment, "segment_status", return_value={"status": "attempt_recovery_pending", "next_command": "续作恢复段"}):
                self.assertEqual(recovery._evaluation_and_delivery_status(campaign, world["manifest"], CANDIDATE)[:2], ("attempt_recovery_pending", "续作恢复段"))
            world["index"].unlink()
            self.assertEqual(recovery._evaluation_and_delivery_status(campaign, world["manifest"], CANDIDATE)[0], "compared")
            compared.unlink()
            self.assertEqual(recovery._evaluation_and_delivery_status(campaign, world["manifest"], CANDIDATE)[0], "candidate_sealed")
            _write(compared, {"status": "complete"})
            _write(accepted, {"status": "complete"})
            checkpoint = recovery.claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1)
            _write(checkpoint, {"status": "complete"})
            with mock.patch.object(delivery, "delivery_status", return_value={"status": "ready", "next_command": "交付 run"}):
                result = recovery._evaluation_and_delivery_status(campaign, world["manifest"], CANDIDATE)
                self.assertEqual(result[:2], ("ready", "交付 run"))


if __name__ == "__main__":
    unittest.main()
