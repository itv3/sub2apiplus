"""改造 5 M2（attempt-recovery 恢复）：T5.17 transient-environment 准入与 T5.12 恢复段 ar<k>。

夹具沿用 M1 的 VC 链 + 合成候选 attempt（``_EvaluationChainMixin``）：b0 断言批次真实派发并失败 →
对账 → ``evaluation-recover apply --root-cause-class transient-environment`` 开出 attempt-recovery 基线 b1
（真实 apply：recovery.json／PREPARED／AUTHORIZATION／COMMIT／outbox 根因入账／账本 evaluation_baseline）→
``capture-candidate run --attempt-recovery ar1`` 只补跑 J*。

只 mock 外部环境依赖（候选容器身份、ARM64／容器探针、恢复 finalizer、候选凭据）与 Campaign 冻结 Job
定义的读取（``_campaign_jobs``，合成 Job 的步骤真实执行并把证据写到重定位后的新根）。本文件位于
tests/，不进受管摘要。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.codex_upgrade import Job
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import test_codex_upgrade
from tools.official_client_capture.tests.test_codex_upgrade_candidate_revision import R1, _read
from tools.official_client_capture.tests import test_codex_upgrade_evaluation_recovery as recovery_tests

# 不把测试类名引入模块全局，避免 unittest 重复收集 recovery 集成用例。
_RECOVERY_TESTS = recovery_tests.EvaluationRecoveryIntegrationTests


def _job_steps(_job_id: str, root: str) -> tuple[dict, ...]:
    """合成候选 Job 的唯一步骤：把证据写进 argv 末尾给出的证据根（段重定位会把该根名改写为新根）。"""

    return ({"argv": ["sh", "-c", 'mkdir -p "$0/relay" && printf \'{"recovered":true}\\n\' > "$0/relay/out.json"', root]},)


class AttemptRecoveryTests(recovery_tests._EvaluationChainMixin, unittest.TestCase):
    candidate_job_steps = staticmethod(_job_steps)

    def _candidate_evidence_parent(self, fixture: dict, campaign_dir: Path) -> Path:
        # 权限收口合同：外部 Job 证据根必须在逻辑 runs 根内且与宿主 <data>/runs 同 inode——夹具直接把候选
        # 证据放在宿主数据根的 runs 目录下，测试只把该目录注入为逻辑 runs 别名。
        runs = Path(str(fixture["data"])) / "runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        return runs

    def setUp(self) -> None:
        super().setUp()
        self.helper = test_codex_upgrade.CodexUpgradeTest("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    # 复用 recovery 集成测试的夹具链、只读替身与参数构造（同一夹具族）。
    _ready_vc4 = _RECOVERY_TESTS._ready_vc4
    _stage_patches = _RECOVERY_TESTS._stage_patches
    _recover_arguments = staticmethod(_RECOVERY_TESTS._recover_arguments)

    def _failed_b0_and_reconciled(self, root: Path) -> tuple[dict, dict]:
        fixture, context = self._ready_vc4(root)
        campaign_dir = context["campaign_dir"]
        plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
        result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
        self.assertEqual(returncode, 1, result)
        run_b0 = Path(str(result["campaign_run"]["run_dir"]))
        self.assertEqual(reconciler.reconcile_supervisor_run(run_b0, campaign_dir)["status"], "recoverable")
        for patcher in self._stage_patches(context, context["candidate"]):
            patcher.start()
            self.addCleanup(patcher.stop)
        context["data_root"] = Path(str(fixture["data"]))
        return fixture, context

    def _apply_transient(self, fixture: dict, context: dict) -> dict:
        with mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"):
            preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
            self.assertIn("transient-environment", preview["admissible_classes"])
            self.assertEqual(preview["failure_scope"]["jobs"], [context["job_ids"][0]])
            applied = codex_upgrade.evaluation_recover(
                self._recover_arguments(fixture, "apply", root_cause_class="transient-environment", approve_sha256=preview["review_sha256"])
            )
        return applied

    def _segment_run_patches(self, context: dict, jobs: list[Job]):
        """恢复段 run 只 mock 外部环境依赖；Job 步骤真实执行。"""

        campaign_dir = context["campaign_dir"]

        def arm64_receipt(target: Path, *, phase: str, subject_id: str, **_kwargs: object) -> tuple[Path, dict]:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / "receipt.json"
            receipt = {"schema_version": "codex-upgrade-arm64-environment-receipt/v1", "status": "passed", "phase": phase, "subject_id": subject_id, "continuity_identity_sha256": "c" * 64}
            path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            path.chmod(0o600)
            return path, receipt

        def probe(_manifest: dict, target: Path, phase: str, **_kwargs: object) -> dict:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = target / "probe-manifest.json"
            document = {"phase": phase, "status": "passed"}
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            path.chmod(0o600)
            return document

        def restoration(evidence_root: Path, *, phase: str, candidate_id: str | None) -> tuple[Path, dict]:
            path = evidence_root / "restoration-report.json"
            receipt = {"status": "passed", "phase": phase, "candidate_id": candidate_id}
            path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            path.chmod(0o600)
            return path, receipt

        # 夹具 Job 证据根在 Campaign 目录下（不在 /capture/runs 别名内）：权限收口／重放按同一逻辑执行，
        # 只把夹具的证据父目录注入为 runs 别名（不改合同常量；真机端到端用真实别名目录）。
        data_root = Path(str(context["data_root"]))
        alias_root = data_root / "runs"

        def closeout(attempt_root: Path, roots: list[Path]) -> dict:
            receipt_path, _receipt = permissions.close_evidence_permissions(
                attempt_root, roots, managed_data_root=data_root, logical_runs_roots=(alias_root,)
            )
            return permissions.receipt_binding(attempt_root, receipt_path)

        def replay(attempt_root: Path, roots: list[Path], binding: dict) -> dict:
            return permissions.replay_evidence_permission_closeout(
                attempt_root, list(roots), binding, managed_data_root=data_root, logical_runs_roots=(alias_root,)
            )

        return (
            mock.patch.object(codex_upgrade, "_campaign_jobs", return_value=list(jobs)),
            mock.patch.object(codex_upgrade, "_close_attempt_evidence_permissions", side_effect=closeout),
            mock.patch.object(codex_upgrade, "_replay_evidence_permission_closeout", side_effect=replay),
            mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"),
            mock.patch.object(codex_upgrade, "_validate_candidate_admin_credential"),
            mock.patch.object(codex_upgrade, "_capture_arm64_environment_receipt", side_effect=arm64_receipt),
            mock.patch.object(codex_upgrade, "_probe_capture_environment", side_effect=probe),
            mock.patch.object(codex_upgrade, "_finalize_attempt_restoration", side_effect=restoration),
        )

    @staticmethod
    def _run_arguments(campaign_dir: Path, recovery_revision: str | None) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=campaign_dir, candidate_id=R1, capture_action="run", attempt_recovery=recovery_revision,
            capture_root=None, rerun_failed=False, max_wall_seconds=600, heartbeat_seconds=1, candidate_purpose="validation_only",
        )

    def test_transient_apply_opens_attempt_recovery_baseline(self) -> None:
        """T5.17：断言失败 → transient-environment apply → b1（attempt-recovery，ar1，J*＝定位到的 Job）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            job_ids = context["job_ids"]
            applied = self._apply_transient(fixture, context)
            self.assertEqual((applied["status"], applied["kind"], applied["evaluation_baseline"]), ("applied", "attempt-recovery", 1), applied)
            self.assertEqual((applied["execute_jobs"], applied["reuse_jobs"]), ([job_ids[0]], sorted(job_ids[1:])))
            self.assertEqual((applied["attempt_id"], applied["recovery_revision"]), (context["attempt_root"].name, "ar1"))
            recovery = artifacts.validate_evaluation_recovery(_read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "recovery.json"))
            self.assertEqual((recovery["kind"], recovery["root_cause_class"], recovery["failed_step"]), ("attempt-recovery", "transient-environment", job_ids[0]))
            self.assertIsNone(recovery["fix_commit"])
            self.assertIsNone(recovery["evaluation_epoch"])
            # 根因按 attempt.job-transient-failure 编码并入总账；COMMIT 的 capture-candidate／compare 为 local。
            self.assertEqual(recovery["root_cause_id"], applied["root_cause_id"])
            commit = _read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "COMMIT")
            self.assertEqual(commit["stage_sources"]["capture-candidate"], {"source": "local", "target": f"candidates/{R1}/revisions/b1/result.json"})
            self.assertEqual(commit["stage_sources"]["compare"]["source"], "local")
            summary = timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))
            self.assertEqual(summary["current_evaluation_baseline"]["baseline_kind"], "attempt-recovery")
            self.assertEqual(summary["current_evaluation_baseline"]["recovery_revision"], "ar1")
            # 准入负例：evaluator-defect 的参数对 transient 无意义但不影响；假装原 attempt 恢复曾失败 → 拒绝。
            facts_attempt = dict(context["attempt"], restoration_error={"type": "X", "message": "y"})
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "环境事实不足"):
                with mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"):
                    codex_upgrade._transient_environment_admission(
                        campaign_dir, fixture["manifest"], {"failure_source": "assertion-failed", "failure_scope": {"jobs": job_ids[:1], "rules": []}},
                        attempt_root=context["attempt_root"], attempt=facts_attempt,
                    )
            # 候选五层身份不等于冻结值（夹具身份只有 candidate_purpose）→ 真实校验拒绝。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "源码树"):
                codex_upgrade._transient_environment_admission(
                    campaign_dir, fixture["manifest"], {"failure_source": "assertion-failed", "failure_scope": {"jobs": job_ids[:1], "rules": []}},
                    attempt_root=context["attempt_root"], attempt=context["attempt"],
                )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "J\\* 为空"):
                with mock.patch.object(codex_upgrade, "_verify_candidate_attempt_identity"):
                    codex_upgrade._transient_environment_admission(
                        campaign_dir, fixture["manifest"], {"failure_source": "assertion-failed", "failure_scope": {"jobs": [], "rules": []}},
                        attempt_root=context["attempt_root"], attempt=context["attempt"],
                    )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只能由断言失败触发"):
                codex_upgrade._transient_environment_admission(
                    campaign_dir, fixture["manifest"], {"failure_source": "offline-compare-failed", "failure_scope": None},
                    attempt_root=context["attempt_root"], attempt=context["attempt"],
                )

    def test_recovery_segment_runs_only_execute_jobs_in_relocated_roots(self) -> None:
        """T5.12：恢复段只补跑 J*，证据写进 <原根>-recovery-ar1，段目录闭包、账本追认与开段／完成事件。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_attempt_bytes = (attempt_root / "attempt.json").read_bytes()
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            source_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            # 恢复段之前：b0 基线不是 attempt-recovery → 拒绝开段。
            with mock.patch.object(codex_upgrade, "_campaign_jobs", return_value=list(source_jobs)):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有已 COMMIT 的 attempt-recovery 基线"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            # 段 Job 必须与原预约 planned_jobs 的执行摘要同源：Job 定义漂移（多一个环境变量）即拒绝。
            drifted_jobs = [
                Job(**{**job.__dict__, "steps": ({**job.steps[0], "environment": {"DRIFT": "1"}},)}) for job in source_jobs
            ]
            with self._segment_patches_started(context, drifted_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "原执行摘要与原预约不一致"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            with self._segment_patches_started(context, source_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "ar2"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar2"), "candidate")
                result = codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            self.assertEqual((result["status"], result["recovery_revision"], result["execute_jobs"]), ("awaiting_receipts", "ar1", [job_ids[0]]))
            segment = attempt_root / "recovery" / "ar1"
            self.assertTrue((segment / "recovery-reservation.json").is_file())
            self.assertTrue((segment / "attempt-recovery.json").is_file())
            self.assertEqual(sorted(p.name for p in segment.glob("job-*.json")), [f"job-{job_ids[0]}.json"])
            self.assertTrue((segment / "checkpoints").is_dir())
            self.assertTrue((segment / "logs").is_dir())
            self.assertTrue((segment / "evidence" / "environment" / "before" / "probe-manifest.json").is_file())
            self.assertTrue((segment / "evidence" / "environment" / "after" / "probe-manifest.json").is_file())
            self.assertTrue((segment / "evidence" / "restoration-report.json").is_file())
            reservation = _read(segment / "recovery-reservation.json")
            self.assertEqual([item["id"] for item in reservation["planned_jobs"]], [job_ids[0]])
            self.assertEqual(reservation["reuse_jobs"], sorted(job_ids[1:]))
            self.assertEqual(reservation["original_attempt"]["sha256"], codex_upgrade.file_sha256(attempt_root / "attempt.json"))
            summary_doc = _read(segment / "attempt-recovery.json")
            self.assertEqual((summary_doc["status"], summary_doc["schema_version"]), ("awaiting_receipts", codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA))
            # 证据根重定位：新根 = <原根>-recovery-ar1，原根未被触碰，旧 attempt.json 字节不变。
            recovered_root = Path(original_roots[job_ids[0]][0] + "-recovery-ar1")
            self.assertEqual(summary_doc["results"][0]["evidence_roots"], [str(recovered_root)])
            self.assertTrue((recovered_root / "relay" / "out.json").is_file())
            self.assertEqual((attempt_root / "attempt.json").read_bytes(), original_attempt_bytes)
            self.assertFalse(list(Path(original_roots[job_ids[0]][0]).glob("relay/out.json")))
            # 账本：原 attempt 追认（started/completed）+ 恢复段 started/completed。
            events = [(e["event_type"], e.get("attempt_id"), e.get("recovery_revision")) for e, _ in timing_ledger._load_events(Path(str(fixture["timing_ledger"])))]
            self.assertIn(("attempt_started", attempt_root.name, None), events)
            self.assertIn(("attempt_completed", attempt_root.name, None), events)
            self.assertIn(("attempt_recovery_started", attempt_root.name, "ar1"), events)
            self.assertIn(("attempt_recovery_completed", attempt_root.name, "ar1"), events)
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["attempt_recoveries"][f"{attempt_root.name}:ar1"]["status"], "completed")
            # 同段重开被拒；重验读取一致。
            with self._segment_patches_started(context, source_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已存在，禁止重开"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            with self._segment_patches_started(context, source_jobs):
                loaded_root, loaded_reservation, loaded_summary = codex_upgrade._load_attempt_recovery_segment(campaign_dir, R1, attempt_root.name, "ar1")
            self.assertEqual((loaded_root, loaded_reservation["run_nonce"], loaded_summary["attempt_recovery_digest"]), (segment, reservation["run_nonce"], summary_doc["attempt_recovery_digest"]))

    # ---- 夹具辅助 -------------------------------------------------------------------

    def _segment_patches_started(self, context: dict, jobs: list[Job]):
        patchers = self._segment_run_patches(context, jobs)

        class _Group:
            def __enter__(group_self):
                for patcher in patchers:
                    patcher.start()
                return group_self

            def __exit__(group_self, *_exc):
                for patcher in reversed(patchers):
                    patcher.stop()
                return False

        return _Group()


if __name__ == "__main__":
    unittest.main()
