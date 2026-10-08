"""同 Campaign 第二次 transient 的离线连续链；不单独登记认证通过。

沿用首段驱动明示的官方、宿主、容器、网关与中继来源，只在首次 ar1 执行前把故障移到同 Job 的另一有效 probe。
真实 b1 compare／builder 失败后按项目总账环境修复收据解除根因暂停，冻结 b2／ar2，只补冻结的 J*。
预约、原始字节、段 seal、入账、当前基线 compare／builder、Store 与两本账均执行产品代码。
本驱动保留控制面画像接缝，不能据此声称全规则真实语义认证或 ARM64 真机验收通过。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
from tools.official_client_capture.tests import claude_fault_chain_world as world_fixture
from tools.official_client_capture.tests import claude_transient_chain_driver as first_chain

SECOND_FAILED_PROBE = "v4-native-tls-baseline"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class ClaudeSecondTransientChainDriver(first_chain.ClaudeTransientChainDriver):
    """首段已成功封存并入账后接续第二段；不改旧段正文，不放宽根因上限。"""

    def repair_environment_root(self, world: dict[str, Any], reconciled: dict[str, Any]) -> dict[str, Any]:
        """解除外部故障后用真实 CLI 登记环境修复；收据明确只属于本次离线来源。"""

        cause = str(reconciled["root_cause"]["root_cause_id"])
        self.assertIn(cause, ledger.replay_head(self.project_root)["root_cause_counts"])
        if reconciled["status"] == "paused":
            self.assertEqual(set(reconciled["decision"]["pause_kinds"]), {"root_cause_repair"})
        else:
            self.assertEqual(reconciled["status"], "recoverable")
        before_control = upgrade.file_sha256(self.relay_control)
        first_chain._write(self.relay_control, {"bad_probe": None})
        self.assertIsNone(json.loads(self.relay_control.read_bytes())["bad_probe"])
        binding = {
            "campaign_id": world["campaign"].name, "candidate_id": world["candidate_id"],
            "attempt_id": world["attempt_id"], "root_cause_id": cause,
            "source_kind": "offline-fixture-environment", "production_certification": False,
            "control_path": str(self.relay_control), "control_before_sha256": before_control,
            "control_after_sha256": upgrade.file_sha256(self.relay_control),
        }
        repaired = first_chain._write(self.root / "second-transient-environment-repair.json", {
            "schema_version": "claude-offline-environment-repair/v1", **binding,
            "repaired_probe": SECOND_FAILED_PROBE,
        })
        clean = first_chain._write(self.root / "second-transient-clean-environment.json", {
            "schema_version": "claude-offline-clean-environment/v1", **binding,
            "bad_probe": None, "environment_repair_receipt_sha256": upgrade.file_sha256(repaired),
        })
        result = subprocess.run([
            sys.executable, "-B", "-m", "tools.official_client_capture.codex_upgrade_project_ledger",
            "record-root-cause-repair", "--ledger-dir", str(self.project_root), "--root-cause-id", cause,
            "--kind", "environment", "--binding", f"environment_repair_receipt_sha256={upgrade.file_sha256(repaired)}",
            "--binding", f"clean_environment_receipt_sha256={upgrade.file_sha256(clean)}",
            "--note", "同 Campaign 第二次 transient：解除离线中继故障后续跑，不代表生产环境认证",
        ], cwd=REPOSITORY_ROOT, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["root_cause_ids"], [cause])
        self.assertEqual(ledger.replay_head(self.project_root)["root_cause_counts"][cause], 0)
        return receipt

    def run_second_transient_chain(self, *, finish_acceptance: bool = False) -> dict[str, Any]:
        """同一候选 attempt 沿 b0／ar1／b1／ar2／b2 运行，并核六项恢复边界。"""

        with client_fixture.source_scope():
            world = self.run_transient_chain(evaluate_recovery=False, first_recovery_fault=SECOND_FAILED_PROBE)
            campaign, attempt_id = world["campaign"], world["attempt_id"]
            attempt_root, original = world["original_attempt_root"], world["original_attempt"]
            ar1_root, ar1_summary = world["recovery_root"], world["recovery_summary"]
            self.assertEqual((world["evaluation_baseline"], world["recovery_revision"]), (1, "ar1"))
            ar1_jobs = {row["id"]: row for row in ar1_summary["results"]}
            self.assertIn(SECOND_FAILED_PROBE, [step["probe_id"] for step in ar1_jobs[first_chain.FAILED_JOB]["steps"] if step.get("argv")])
            self.assertNotEqual(SECOND_FAILED_PROBE, first_chain.FAILED_PROBE)
            old_boundary = self.boundary([Path(path) for path in world["old_boundary"]] + [ar1_root,
                campaign / "candidates" / first_chain.CANDIDATE / "revisions" / "b1",
                *[Path(root) for row in ar1_summary["results"] for root in row["evidence_roots"]]])
            audit = self.late_audit("同 Campaign 第二次 transient 与 ar2 续跑", world)
            code, failed, stderr = self.dispatch_builder(world, world["next_sequence"], baseline=1)
            self.assertIsNotNone(failed, stderr)
            self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
            reconcile_argv = ["reconcile-supervisor-run", "--campaign-dir", campaign,
                "--run-dir", Path(failed["campaign_run"]["run_dir"])]
            code, reconciled, stderr = self.source_command(reconcile_argv)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(reconciled, stderr)
            counted = audit.accounting_state()
            repair = self.repair_environment_root(world, reconciled)
            code, reconciled_after, stderr = self.source_command(reconcile_argv)
            self.assertIsNotNone(reconciled_after, stderr)
            self.assertEqual((code, reconciled_after["status"]), (0, "recoverable"), stderr)
            self.assertEqual(ledger.replay_head(self.project_root)["root_cause_counts"][reconciled["root_cause"]["root_cause_id"]], 0)
            with self.source_context():
                preview = self.preview(world)
                self.assertEqual(preview["failure_scope"]["jobs"], [first_chain.FAILED_JOB])
                code, recovered, stderr = self.recover(world, "apply", "--approve-sha256", preview["review_sha256"],
                    "--root-cause-class", "transient-environment")
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(recovered, stderr)
            baseline_root = campaign / "candidates" / first_chain.CANDIDATE / "revisions" / "b2"
            recovery = json.loads((baseline_root / "recovery.json").read_bytes())
            self.assertEqual((recovery["attempt_id"], recovery["recovery_revision"], recovery["previous_baseline"]), (attempt_id, "ar2", 1))
            self.assertEqual(recovery["execute_jobs"], [first_chain.FAILED_JOB])
            jobs = {row["id"]: row for row in original["results"]}
            self.assertEqual(set(recovery["reuse_jobs"]), set(jobs) - {first_chain.FAILED_JOB})
            before_count = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)["precise_total"]
            before_calls = len(self.runtime.candidate_calls())
            run_argv = ["capture-candidate", "run", "--campaign-dir", campaign, "--candidate-id", first_chain.CANDIDATE,
                "--attempt-recovery", "ar2", "--acknowledge-live-requests"]
            code, result, stderr = self.source_command(run_argv)
            self.assertIsNotNone(result, stderr)
            self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
            segment = attempt_root / "recovery" / "ar2"
            summary = json.loads((segment / "attempt-recovery.json").read_bytes())
            ar1_reservation = json.loads((ar1_root / "reservation.json").read_bytes())
            reservation = json.loads((segment / "reservation.json").read_bytes())
            self.assertEqual(len({original["run_nonce"], ar1_reservation["run_nonce"], reservation["run_nonce"]}), 3)
            self.assertNotEqual(ar1_reservation["identity_sha256"], reservation["identity_sha256"])
            self.assertEqual([row["id"] for row in summary["results"]], [first_chain.FAILED_JOB])
            self.assertTrue(all("-recovery-ar2" in root for row in summary["results"] for root in row["evidence_roots"]))
            new_calls = self.runtime.candidate_calls()[before_calls:]
            executed = [row["probe"] for row in new_calls if not row["dry_run"]]
            expected = [row["probe_id"] for row in jobs[first_chain.FAILED_JOB]["steps"] if row.get("argv")]
            self.assertCountEqual(executed, expected)
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            self.seal(campaign, attempt_id, recovery="ar2")
            with self.source_context():
                stage = upgrade._load_stage_result(campaign, "capture-candidate", first_chain.CANDIDATE, _baseline=2)
            self.assertEqual(stage["client_evidence"], world["stage"]["client_evidence"])
            self.assertEqual(set(stage["root_sets"]["dropped_roots"]), set(ar1_jobs[first_chain.FAILED_JOB]["evidence_roots"]))
            effective_path = campaign / stage["effective_results"]["path"]
            self.assertEqual(upgrade.file_sha256(effective_path), stage["effective_results"]["sha256"])
            effective = json.loads(effective_path.read_bytes())
            self.assertEqual((effective["evaluation_baseline"], effective["recovery_revision"]), (2, "ar2"))
            self.assertEqual({row["id"]: row for row in effective["results"]}, {**jobs, first_chain.FAILED_JOB: summary["results"][0]})
            self.assertEqual(stage["results"], effective["results"])
            self.assertNotEqual(stage["store"]["validation_attempt_ref"], world["stage"]["store"]["validation_attempt_ref"])
            execution = world["store"].load_object(stage["store"]["validation_execution_plan_ref"])["payload"]
            self.assertEqual(set(execution["execute_item_ids"]) & set(jobs), {first_chain.FAILED_JOB})
            self.assertEqual(set(execution["reuse_item_ids"]) & set(jobs), set(jobs) - {first_chain.FAILED_JOB})
            account_argv = ["account-sealed-candidate", "--campaign-dir", campaign,
                "--candidate-id", first_chain.CANDIDATE, "--attempt-recovery", "ar2"]
            code, account, stderr = self.source_command(account_argv)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(account, stderr)
            keys = [key for key in ledger.replay_head(self.project_root)["operations"] if key.startswith("account-sealed-candidate:")]
            self.assertTrue(any("ar1" in key for key in keys))
            self.assertTrue(any("ar2" in key for key in keys))
            receipt = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
            self.assertEqual(receipt["status"], "complete")
            probes = [probe for row in summary["results"] for root in row["evidence_roots"]
                for probe in Path(root).iterdir() if (probe / "relay-manifest.json").is_file()]
            delta_receipt = provenance.collect_root_provenance(probes)
            self.assertEqual(delta_receipt["unresolved_roots"], [])
            delta = delta_receipt["precise_total"]
            self.assertGreater(delta, 0)
            self.assertEqual(receipt["precise_total"] - before_count, delta)
            code, passed, stderr = self.dispatch_builder(world, world["next_sequence"], baseline=2)
            self.assertIsNotNone(passed, stderr)
            self.assertEqual((code, passed["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
            index = json.loads((upgrade._stage_write_target(campaign, first_chain.CANDIDATE, 2, "assertions") / "evaluation-run.json").read_bytes())
            self.assertEqual(index["evaluation_baseline"], 2)
            self.assertTrue(all(row["status"] == "pass" for row in index["rules"]))
            calls = len(self.runtime.candidate_calls())
            before_replay = audit.accounting_state()
            replay_boundary = self.boundary([segment, baseline_root, world["store"].root])
            code, reused, stderr = self.source_command(run_argv)
            self.assertEqual((code, reused["reused"], reused["live_request_count"]), (2, True, 0), stderr)
            code, resealed, stderr = self.source_command(["capture-candidate", "seal", "--campaign-dir", campaign,
                "--candidate-id", first_chain.CANDIDATE, "--attempt-id", attempt_id, "--attempt-recovery", "ar2",
                "--approve-seal-sha256", stage["seal"]["review_sha256"], "--approved-by", "连续链审核"])
            self.assertIsNotNone(resealed, stderr)
            self.assertEqual((code, resealed["status"], resealed["reused"]), (0, "complete", True), stderr)
            code, _reaccounted, stderr = self.source_command(account_argv)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            self.assertEqual(self.boundary([segment, baseline_root, world["store"].root]), replay_boundary)
            self.assertEqual(self.boundary([Path(path) for path in old_boundary]), old_boundary)
            after_replay = audit.accounting_state()
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
            self.assert_campaign_request_accounting(campaign, expected_client_requests=world["expected_client_requests"])
            audit.check_trusted()
            audit.check_closure(executed=[row["id"] for row in summary["results"]], expected=[first_chain.FAILED_JOB],
                reused=recovery["reuse_jobs"], expected_reused=sorted(set(jobs) - {first_chain.FAILED_JOB}), what="第二次恢复段冻结 J*")
            audit.check_accounting(precise_delta=delta, root_causes=1, root_cause_state=counted,
                repeats=[("ar2 run／seal／account 幂等重读", before_replay, after_replay)])
            audit.check_elapsed()
            audit.check_history()
            audit.check_entry(recovered, "compile-and-run-vc-batch", "VC-5")
            world["records"].append(audit.report(campaign_id=campaign.name, candidate_id=first_chain.CANDIDATE,
                candidate_revision=world["revision"], evaluation_baseline=2, recovery_revision="ar2",
                predecessor_recovery="ar1", executed_probes=executed, live_request_delta=delta,
                environment_repair=repair["receipt_sha256"]))
            world_fixture.refresh_world(self, world)
            world.update(recovery_revision="ar2", recovery_root=segment, recovery_summary=summary,
                baseline_recovery=recovery, second_transient_old_boundary=old_boundary)
            if finish_acceptance:
                world_fixture.strict_accept(self, world)
            return world
