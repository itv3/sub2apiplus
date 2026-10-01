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
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.codex_upgrade import Job
from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import test_codex_upgrade
from tools.official_client_capture.tests.test_codex_upgrade_candidate_revision import R1, _read
from tools.official_client_capture.tests import test_codex_upgrade_evaluation_recovery as recovery_tests


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

    # 复用 recovery 集成测试的夹具链、只读替身与参数构造（同一夹具族）。一律经模块属性取用，本模块全局里不放
    # 那个测试类：unittest 收集时不看名字前缀，模块级别名（原先的 _RECOVERY_TESTS）会让它的 8 个集成用例在这里
    # 再执行一遍，ARM64 上每次全量多花约 2 分钟（E2-02）。
    _ready_vc4 = recovery_tests.EvaluationRecoveryIntegrationTests._ready_vc4
    _stage_patches = recovery_tests.EvaluationRecoveryIntegrationTests._stage_patches
    _recover_arguments = staticmethod(recovery_tests.EvaluationRecoveryIntegrationTests._recover_arguments)

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
            path = evidence_root / "receipts/restoration-report.json"
            path.parent.mkdir(mode=0o700, exist_ok=True)
            receipt = {"status": "restored", "phase": phase, "candidate_id": candidate_id}
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
            # 零请求外部环境替身只提供 continuity 字段，不能交给真实 v8 收据 reader。
            mock.patch.object(codex_upgrade.codex_upgrade_arm64_environment_receipt, "receipts_equivalent",
                              side_effect=lambda _a, before, _b, after: before["continuity_identity_sha256"] == after["continuity_identity_sha256"]),
            mock.patch.object(codex_upgrade, "_probe_capture_environment", side_effect=probe),
            mock.patch.object(codex_upgrade, "_finalize_attempt_restoration", side_effect=restoration),
        )

    @staticmethod
    def _run_arguments(campaign_dir: Path, recovery_revision: str | None) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=campaign_dir, candidate_id=R1, capture_action="run", attempt_recovery=recovery_revision,
            capture_root=None, rerun_failed=False, max_wall_seconds=600, heartbeat_seconds=1, candidate_purpose="validation_only",
            acknowledge_live_requests=True,
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
            self.assertTrue((segment / "evidence" / "receipts" / "restoration-report.json").is_file())
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
            # 成功段的同段重派按幂等返回（崩溃矩阵 R2 的 attempt-recovery 变体：零请求、不重跑 Job、摘要不变、
            # 账本无新事件）；重验读取一致。
            with self._segment_patches_started(context, source_jobs):
                with mock.patch.object(codex_upgrade, "run_job", side_effect=AssertionError("幂等重派不得重跑 Job")):
                    replayed = codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            self.assertEqual((replayed["status"], replayed["idempotent_replay"], replayed["live_request_count"]), ("awaiting_receipts", True, 0))
            self.assertEqual(replayed["attempt_recovery_digest"], summary_doc["attempt_recovery_digest"])
            self.assertEqual(replayed["ledger_events"], [])
            self.assertEqual(_read(segment / "attempt-recovery.json"), summary_doc)
            with self._segment_patches_started(context, source_jobs):
                loaded_root, loaded_reservation, loaded_summary = codex_upgrade._load_attempt_recovery_segment(campaign_dir, R1, attempt_root.name, "ar1")
            self.assertEqual((loaded_root, loaded_reservation["run_nonce"], loaded_summary["attempt_recovery_digest"]), (segment, reservation["run_nonce"], summary_doc["attempt_recovery_digest"]))

    def test_recovery_segment_failure_and_interruption_are_reconciled_per_segment(self) -> None:
        """T5.13／T5.14：段内 Job 失败 → reconcile-attempt --recovery-revision（收据带段号、operation 带段号、
        账本 attempt_recovery_failed、总账入账、判定）；段内中断（无 run-summary）同样按段对账；
        provenance 枚举段目录；同段续跑被拒；父 run 窗口扫描识别段预约。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            # 段 Job 真实执行但失败（步骤退出 3，且不写证据）。
            failing_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            with self._segment_patches_started(context, failing_jobs):
                with mock.patch.object(codex_upgrade, "run_job", side_effect=lambda job, *a, **k: {
                    "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                    "status": "failed", "description": job.description, "duration_seconds": 0.0, "steps": [{"argv": ["sh"], "return_code": 3, "log": ""}],
                    "evidence_roots": [], "missing_evidence_patterns": list(job.evidence_roots), "empty_evidence_patterns": [], "covers": [],
                    "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [], "track": "main", "model_id": "",
                    "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
                    "model_condition_receipt_failure": None, "disposition": "executed",
                }):
                    failed_run = codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            self.assertEqual(failed_run["status"], "failed")
            self.assertTrue(failed_run["next_command"].startswith("reconcile-attempt"))
            segment = attempt_root / "recovery" / "ar1"
            summary_doc = _read(segment / "attempt-recovery.json")
            self.assertEqual(summary_doc["status"], "failed")
            # provenance 枚举到段目录（段预约已发布）。
            from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance

            self.assertIn(segment.resolve(), provenance._attempt_directories(campaign_dir, "candidate"))
            # 父 run 窗口扫描识别段预约（键 <attempt>:<ar>）。
            from tools.official_client_capture import codex_upgrade_supervisor as supervisor

            found = supervisor.candidate_reservations_in_run_window(campaign_dir, candidate_id=R1, started_at_epoch=0.0)
            self.assertIn((f"{attempt_root.name}:ar1", segment), found)
            # 失败段禁止同段续跑／重开：不带 --rerun-failed 重派同段被拒，带 --rerun-failed 指向首段亦被拒。
            with self._segment_patches_started(context, failing_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "状态为 failed，禁止重开"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只用于开失败段的后继段"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar1")), "rerun_failed": True}), "candidate"
                    )
                # 后继段必须等失败段对账入账后才能开。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是已对账的失败终态"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True}), "candidate"
                    )
            # 段对账：收据、operation、账本事件、判定（重放段 run-summary 的权限收口需同一 runs 别名注入）。
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
            self.assertEqual(reconciled["recovery_revision"], "ar1")
            self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
            receipt = _read(campaign_dir / reconciled["reconciliation_receipt"]["path"])
            self.assertEqual((receipt["recovery_revision"], receipt["attempt_id"]), ("ar1", attempt_root.name))
            self.assertTrue(receipt["reservation"]["path"].endswith("recovery/ar1/recovery-reservation.json"))
            self.assertEqual(reconciled["jobs"]["failed"], [job_ids[0]])
            self.assertEqual(reconciled["batch"]["batch_dir"].split("/")[-1].startswith("batch-"), True)
            operations = _read(Path(reconciled["batch"]["batch_dir"]) / "COMMIT") if (Path(reconciled["batch"]["batch_dir"]) / "COMMIT").is_file() else {}
            self.assertIn("ar1", json.dumps(operations))
            ledger = timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))
            self.assertEqual(ledger["attempt_recoveries"][f"{attempt_root.name}:ar1"]["status"], "failed")
            self.assertEqual(ledger["attempt_recoveries"][f"{attempt_root.name}:ar1"]["root_cause_id"], reconciled["root_cause"]["root_cause_id"])
            # 幂等：再次对账返回同一收据；预览目录按段命名。
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                again = reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
            self.assertEqual(again["reconciliation_receipt"], reconciled["reconciliation_receipt"])
            self.assertTrue(again["recovery_preview_path"].split("/")[-2].endswith(f"{attempt_root.name}-ar1"))
            # 段级对账绑定校验（作废前核对用）。
            bound = supervisor.verify_attempt_reconciliation_binding(
                campaign_dir, campaign_id=str(fixture["manifest"]["campaign_id"]), candidate_id=R1, attempt_root=segment, label="核对"
            )
            self.assertEqual((bound["attempt_id"], bound["recovery_revision"], bound["operation_id"]), (attempt_root.name, "ar1", f"reconcile-attempt:{attempt_root.name}:ar1"))
            # 崩溃矩阵 A1：失败段对账入账后，以 --rerun-failed 开后继段 ar2（同一基线冻结的 J* 全量补跑，不重复
            # 裁定根因）；ar2 成功收口，账本 ar1 failed／ar2 completed；跳号（ar3）与不带 --rerun-failed 都被拒。
            with self._segment_patches_started(context, failing_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "后继恢复段编号必须紧接已有段"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar3")), "rerun_failed": True}), "candidate"
                    )
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "冻结的恢复段是 ar1，不是 ar2"):
                    codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar2"), "candidate")
            successor_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            preview_path = Path(reconciled["recovery_preview_path"])
            with self._segment_patches_started(context, successor_jobs):
                # B0：后继段必须持有前序失败段已批准的零请求恢复预览。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须提供前序失败段 ar1 已批准的恢复预览"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True}), "candidate"
                    )
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未批准"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True, "recovery_preview": preview_path}),
                        "candidate",
                    )
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                approval = reconciler.approve_recovery_preview(
                    campaign_dir, attempt_root.name, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
            self.assertEqual(approval["approved_sha256"], reconciled["recovery_preview"]["review_sha256"])
            with self._segment_patches_started(context, successor_jobs):
                successor_run = codex_upgrade._run_capture_attempt(
                    argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True, "recovery_preview": preview_path}),
                    "candidate",
                )
            self.assertEqual((successor_run["status"], successor_run["recovery_revision"], successor_run["execute_jobs"]), ("awaiting_receipts", "ar2", [job_ids[0]]))
            successor_segment = attempt_root / "recovery" / "ar2"
            successor_summary = _read(successor_segment / "attempt-recovery.json")
            self.assertEqual(successor_summary["status"], "awaiting_receipts")
            self.assertTrue(all(root.endswith("-recovery-ar2") for row in successor_summary["results"] for root in row["evidence_roots"]))
            self.assertEqual(_read(successor_segment / "recovery-reservation.json")["recovery_revision"], "ar2")
            ledger = timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))
            self.assertEqual(ledger["attempt_recoveries"][f"{attempt_root.name}:ar1"]["status"], "failed")
            self.assertEqual(ledger["attempt_recoveries"][f"{attempt_root.name}:ar2"]["status"], "completed")
            self.assertEqual(ledger["current_evaluation_baseline"]["recovery_revision"], "ar1")

    # ------------------------------------------------------------------
    # P1（授权闭包，2026-09-21 审核）：后继段预览批准范围 == 权威链 J*，reuse 恒空；J* 只从段预约三元组沿
    # COMMIT → recovery.json 取；"一个 complete、一个 failed"的混合结果与篡改预览、recovery.json 与 COMMIT
    # 不一致（合法自摘要）都被 CLI／reconciler／监督器三处拒绝。
    # ------------------------------------------------------------------

    @mock.patch.object(reconciler, 'segment_reuse_proofs', return_value={})
    def test_segment_preview_scope_requires_proof_even_with_mixed_results(self, _proofs) -> None:
        """段内一个 complete、一个 failed，但缺少四项证明时仍执行完整 J*；
        段预约 planned 与 J* 不一致、缺 J* 都失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            receipt_dir = root / "reconciliation"
            receipt_dir.mkdir(mode=0o700)
            jobs = {
                "planned_job_ids": ["job-a", "job-b"],
                "states": {"job-a": "complete", "job-b": "failed"},
                "groups": {"complete": ["job-a"], "failed": ["job-b"], "indeterminate": [], "pending": []},
                "details": {},
            }
            common = dict(
                manifest={"campaign_id": "campaign-p1"}, attempt_id="att-1", phase="candidate", candidate_id=R1,
                attempt_exists=True, environment_status="restored",
                provenance_copy={"jobs": [
                    {"job_id": "job-a", "precise_count": 2, "estimated_count": 0},
                    {"job_id": "job-b", "precise_count": 0, "estimated_count": 3},
                ]},
                current={"policy_sha256": "1" * 64, "wire_producer_sha256": "2" * 64, "files_sha256": "3" * 64},
                reconciliation_receipt_sha256="4" * 64, campaign_ledger_head={}, project_ledger_scope={},
                now="2026-09-21T00:00:00Z", recovery_revision="ar1",
            )
            preview = reconciler._recovery_preview(root, receipt_dir, jobs=jobs, recovery_execute_jobs=["job-b", "job-a"], **common)
            self.assertEqual((preview["planned_job_ids"], preview["execute_job_ids"], preview["reuse_job_ids"]), (["job-a", "job-b"], ["job-a", "job-b"], []))
            self.assertEqual(preview["complete_job_ids"], ["job-a"])
            self.assertIn("四项", preview["reuse_basis"])
            self.assertEqual((preview["expected_new_requests"]["known_total"], preview["expected_new_requests"]["known_by_job"]), (5, {"job-a": 2, "job-b": 3}))
            self.assertEqual(preview["expected_new_requests"]["unknown_job_ids"], [])
            # 普通 attempt 路径不变：complete Job 复用。
            plain = reconciler._recovery_preview(root, receipt_dir / "plain", jobs=jobs, **{**common, "recovery_revision": None})
            self.assertEqual((plain["reuse_job_ids"], plain["execute_job_ids"]), (["job-a"], ["job-b"]))
            with self.assertRaisesRegex(reconciler.ReconcilerError, r"与基线冻结的 J\*"):
                reconciler._recovery_preview(root, receipt_dir / "x", jobs=jobs, recovery_execute_jobs=["job-a"], **common)
            with self.assertRaisesRegex(reconciler.ReconcilerError, "必须提供权威链"):
                reconciler._recovery_preview(root, receipt_dir / "y", jobs=jobs, **common)

    def test_successor_segment_rejects_preview_scope_drift_and_recovery_unbound_from_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            failing_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            failed_result = lambda job, *a, **k: {
                "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "failed", "description": job.description, "duration_seconds": 0.0, "steps": [{"argv": ["sh"], "return_code": 3, "log": ""}],
                "evidence_roots": [], "missing_evidence_patterns": list(job.evidence_roots), "empty_evidence_patterns": [], "covers": [],
                "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [], "track": "main", "model_id": "",
                "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
                "model_condition_receipt_failure": None, "disposition": "executed",
            }
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(codex_upgrade, "run_job", side_effect=failed_result):
                self.assertEqual(codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")["status"], "failed")
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
            self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
            preview = reconciled["recovery_preview"]
            frozen = sorted(applied["execute_jobs"])
            # 预览范围 == 权威链 J*，reuse 恒空，估算覆盖完整 J*。
            self.assertEqual((sorted(preview["planned_job_ids"]), sorted(preview["execute_job_ids"]), preview["reuse_job_ids"]), (frozen, frozen, []))
            self.assertEqual(sorted(set(preview["expected_new_requests"]["known_by_job"]) | set(preview["expected_new_requests"]["unknown_job_ids"])), frozen)
            from tools.official_client_capture import codex_upgrade_supervisor as supervisor

            b1_dir = campaign_dir / "candidates" / R1 / "revisions" / "b1"
            commit = artifacts.validate_evaluation_baseline_commit(_read(b1_dir / "COMMIT"))
            prior_manifest = {"candidate_id": R1, "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"]}
            self.assertEqual(
                supervisor._require_segment_preview_scope_matches_frozen_jobs(campaign_dir, prior_manifest, attempt_id=attempt_root.name, prior_revision="ar1", preview=preview),
                frozen,
            )
            codex_upgrade._require_recovery_preview_scope_equals_frozen_jobs(preview, frozen, label="核对")
            # 估算合同：known_by_job ∪ unknown_job_ids == J*、不相交、known_total == Σknown。
            estimate = preview["expected_new_requests"]
            self.assertEqual(estimate["known_total"], sum(estimate["known_by_job"].values()))
            self.assertEqual(set(estimate["known_by_job"]) & set(estimate["unknown_job_ids"]), set())
            preview_dir = Path(reconciled["recovery_preview_path"]).parent

            def write_tampered(index: int, **changes) -> tuple[dict, Path]:
                document = {k: v for k, v in preview.items() if k not in {"created_at_utc", "review_sha256", "index"}}
                document.update(changes)
                document["index"] = index
                document["created_at_utc"] = preview["created_at_utc"]
                document["review_sha256"] = reconciler._fingerprint({k: v for k, v in document.items() if k not in {"created_at_utc", "review_sha256"}})
                path = preview_dir / f"recovery-preview-{index:02d}.json"
                path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                path.chmod(0o600)
                return document, path

            # 篡改 1：合法自摘要但只批准 failed Job、复用 complete Job（"批准 1 个、执行 2 个"）→ 函数级双拒绝。
            drifted, _drifted_path = write_tampered(int(preview["index"]) + 1, reuse_job_ids=list(frozen), execute_job_ids=[])
            with self.assertRaisesRegex(supervisor.SupervisorError, r"不等于基线冻结的 J\*"):
                supervisor._require_segment_preview_scope_matches_frozen_jobs(campaign_dir, prior_manifest, attempt_id=attempt_root.name, prior_revision="ar1", preview=drifted)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, r"不等于基线冻结的 J\*"):
                codex_upgrade._require_recovery_preview_scope_equals_frozen_jobs(drifted, frozen, label="核对")
            # 篡改 2：known_total 与 Σknown 不等 → 函数级双拒绝。
            wrong_total, _ = write_tampered(int(preview["index"]) + 2, expected_new_requests={**estimate, "known_total": int(estimate["known_total"]) + 1})
            with self.assertRaisesRegex(supervisor.SupervisorError, r"请求估算未覆盖完整执行集合"):
                supervisor._require_segment_preview_scope_matches_frozen_jobs(campaign_dir, prior_manifest, attempt_id=attempt_root.name, prior_revision="ar1", preview=wrong_total)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, r"请求估算未覆盖完整执行集合"):
                codex_upgrade._require_recovery_preview_scope_equals_frozen_jobs(wrong_total, frozen, label="核对")
            # 篡改 3（批准并走 CLI 端到端）：Job 范围正确但估算少一项（known 与 unknown 都不含该 Job）。
            missing_estimate, tampered_path = write_tampered(
                int(preview["index"]) + 3,
                expected_new_requests={**estimate, "known_by_job": {}, "unknown_job_ids": [], "known_total": 0},
            )
            self.assertEqual((sorted(missing_estimate["planned_job_ids"]), sorted(missing_estimate["execute_job_ids"]), missing_estimate["reuse_job_ids"]), (frozen, frozen, []))
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                approval = reconciler.approve_recovery_preview(campaign_dir, attempt_root.name, approve_sha256=missing_estimate["review_sha256"], recovery_revision="ar1")
            self.assertEqual(approval["approved_sha256"], missing_estimate["review_sha256"])
            with self.assertRaisesRegex(supervisor.SupervisorError, r"请求估算未覆盖完整执行集合"):
                supervisor._require_segment_preview_scope_matches_frozen_jobs(campaign_dir, prior_manifest, attempt_id=attempt_root.name, prior_revision="ar1", preview=missing_estimate)
            with self._segment_patches_started(context, failing_jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, r"请求估算未覆盖完整执行集合"):
                    codex_upgrade._run_capture_attempt(
                        argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True, "recovery_preview": tampered_path}),
                        "candidate",
                    )
            self.assertFalse((attempt_root / "recovery" / "ar2").exists())
            # recovery.json 合法自摘要但不是 COMMIT 绑定的那一份 → 三处拒绝；恢复后 CLI 基线读取重新可用。
            recovery_path = b1_dir / "recovery.json"
            original_recovery = recovery_path.read_bytes()
            forged = json.loads(original_recovery)
            forged.pop("recovery_sha256")
            forged["reviewer"] = f"{forged['reviewer']}-forged"
            forged["recovery_sha256"] = artifacts.digest(forged)
            artifacts.validate_evaluation_recovery(forged)
            recovery_path.write_bytes(json.dumps(forged, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
            try:
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_sha256 不一致"):
                    codex_upgrade._current_attempt_recovery_baseline(campaign_dir, R1, "ar1")
                with self.assertRaisesRegex(supervisor.SupervisorError, "recovery_sha256 或 attempt 身份不一致"):
                    supervisor._require_segment_preview_scope_matches_frozen_jobs(campaign_dir, prior_manifest, attempt_id=attempt_root.name, prior_revision="ar1", preview=preview)
                with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                    with self.assertRaisesRegex(reconciler.ReconcilerError, "权威链不成立"):
                        reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
                with self._segment_patches_started(context, failing_jobs):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_sha256 不一致"):
                        codex_upgrade._run_capture_attempt(
                            argparse.Namespace(**{**vars(self._run_arguments(campaign_dir, "ar2")), "rerun_failed": True, "recovery_preview": tampered_path}),
                            "candidate",
                        )
            finally:
                recovery_path.write_bytes(original_recovery)
            self.assertEqual(codex_upgrade._current_attempt_recovery_baseline(campaign_dir, R1, "ar1")[0], 1)

    def test_successor_segment_batch_is_accepted_by_identity_and_rejects_input_drift(self) -> None:
        """修好接着跑第 9 项（第三批 B3-11）：失败段 ar1 的后继段批次按重派身份承接——同阶段、同动作
        （action_id／operation／item_ids）、同候选（含同基线）、同输入即放行，失败动作的命令 argv（段号、预览、
        心跳、新增参数）、timeout 与 output_bindings 可变；改 item_ids／operation／执行分区拒绝并点名字段；
        跨基线拒绝并指向 evaluation-recover；未换段号、缺恢复参数拒绝。账本许可与 R11 预览范围核对走真实链。"""

        import os
        import sys

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            failing_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            failed_result = lambda job, *a, **k: {
                "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "failed", "description": job.description, "duration_seconds": 0.0, "steps": [{"argv": ["sh"], "return_code": 3, "log": ""}],
                "evidence_roots": [], "missing_evidence_patterns": list(job.evidence_roots), "empty_evidence_patterns": [], "covers": [],
                "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [], "track": "main", "model_id": "",
                "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
                "model_condition_receipt_failure": None, "disposition": "executed",
            }
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(codex_upgrade, "run_job", side_effect=failed_result):
                self.assertEqual(codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")["status"], "failed")
            commit = artifacts.validate_evaluation_baseline_commit(_read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "COMMIT"))

            # 失败段 ar1 的父批次（段 run 动作失败终态）：state／stop-receipt 与 v2 清单；父监督器按 execution-failure
            # 收口让账本进入 recovery_required（真实链：父 run 失败 → 段对账 → 批准预览 → recovery_authorized → active）。
            campaign_id = str(fixture["manifest"]["campaign_id"])
            prior_dir = root / "run-ar1"
            prior_dir.mkdir(mode=0o700)
            owner_nonce = "8" * 64
            prior_state = {
                "state": "failed", "campaign_id": campaign_id, "phase": "VC-5", "owner_pid": os.getpid(),
                "owner_nonce": owner_nonce, "terminal_at_utc": "2026-09-27T01:00:00Z",
            }
            stop = {
                "schema_version": supervisor.STOP_SCHEMA, "campaign_id": campaign_id, "detected_at_epoch": 1005.0,
                "detected_at_utc": "2026-09-27T01:00:00Z", "event_type": "failed", "owner_nonce": owner_nonce,
                "owner_pid": os.getpid(), "phase": "VC-5", "reason": "action-failed:ar-run",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            for name, document in (("state.json", prior_state), ("stop-receipt.json", stop)):
                path = prior_dir / name
                path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                path.chmod(0o600)
            segment_command = [
                sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                "--candidate-id", R1, "--attempt-recovery", "ar1", "--heartbeat-seconds", "1",
            ]
            prior_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": str(codex_upgrade._vc_campaign_plan(campaign_dir, fixture["manifest"])["plan_sha256"]),
                "batch_id": "vc-5-0007", "batch_sequence": 7, "batch_sha256": "7" * 64, "phase": "VC-5",
                "predecessor_checkpoint": {"path": "control/vc/vc-4-checkpoint.json", "sha256": "3" * 64, "phase": "VC-4", "checkpoint_sha256": "4" * 64},
                "original_deadline_at_utc": "2099-09-15T08:12:43Z", "no_op": False,
                "actions": [{"action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 600.0, "command": segment_command, "item_ids": [job_ids[0]]}],
                "execute_items": [job_ids[0]], "reuse_items": [],
                "candidate_revision": 1, "candidate_id": R1, "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"],
            }
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, prior_manifest, failed_action_id="ar-run", failure_class="execution-failure"
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
            self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                approval = reconciler.approve_recovery_preview(
                    campaign_dir, attempt_root.name, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
            self.assertEqual(approval["approved_sha256"], reconciled["recovery_preview"]["review_sha256"])
            preview_path = Path(reconciled["recovery_preview_path"])
            # 消费批准（与真实链驱动同一步骤）：recovery_required → recovery_authorized → active，后继段批次才能派发。
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_root.name, preview_path, recovery_revision="ar1")
            self.assertEqual(authorized["status"], "authorized", authorized)
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "active")

            def successor_manifest(command: list[str], **changes: object) -> dict:
                action = {
                    "action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 900.0, "command": command,
                    "item_ids": [job_ids[0]],
                    "output_bindings": [f"candidates/{R1}/attempts/{attempt_root.name}/recovery/ar2/attempt-recovery.json"],
                }
                action_changes = changes.pop("action", None)
                if isinstance(action_changes, dict):
                    action.update(action_changes)
                manifest = dict(prior_manifest, batch_id="vc-5-0008", batch_sequence=8, batch_sha256="8" * 64, actions=[action])
                manifest.update(changes)
                return manifest

            successor_command = [
                sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                "--candidate-id", R1, "--attempt-recovery", "ar2", "--rerun-failed", "--recovery-preview", str(preview_path),
                "--heartbeat-seconds", "5", "--max-wall-seconds", "900",
            ]

            def check(manifest: dict) -> bool:
                return supervisor._validate_attempt_recovery_segment_successor(
                    prior_state, prior_manifest, prior_dir, manifest, campaign_dir=campaign_dir
                )

            # 按身份放行：段号 ar2、已批准预览、心跳与新增参数、timeout、output_bindings 都与前序不同。
            self.assertTrue(check(successor_manifest(successor_command)))
            # 改输入（item_ids）、operation、执行分区：身份漂移，拒绝并点名字段。
            with self.assertRaisesRegex(supervisor.SupervisorError, "只允许失败动作换段号并追加恢复预览，漂移字段：actions$"):
                check(successor_manifest(successor_command, action={"item_ids": ["other-job"]}))
            with self.assertRaisesRegex(supervisor.SupervisorError, "漂移字段：actions$"):
                check(successor_manifest(successor_command, action={"operation": "VC-5:attempt-recovery-run-renamed"}))
            with self.assertRaisesRegex(supervisor.SupervisorError, "漂移字段：execute_items$"):
                check(successor_manifest(successor_command, execute_items=[job_ids[0], "other-job"]))
            # 跨基线：不是同一批次的重派，指向 evaluation-recover。
            with self.assertRaisesRegex(supervisor.SupervisorError, "漂移字段：evaluation_baseline；跨评估基线.*evaluation-recover"):
                check(successor_manifest(successor_command, evaluation_baseline=2))
            # 失败动作未换段号、缺恢复参数：拒绝。
            with self.assertRaisesRegex(supervisor.SupervisorError, "必须把 --attempt-recovery ar1 改为 ar2"):
                check(successor_manifest(["ar1" if token == "ar2" else token for token in successor_command]))
            with self.assertRaisesRegex(supervisor.SupervisorError, "必须带 --rerun-failed 与 --recovery-preview"):
                check(successor_manifest([token for token in successor_command if token != "--rerun-failed"]))

    def test_d07_watchdog_segment_parent_with_diagnostic_is_claimed_through_0w(self) -> None:
        """草表 D-07（第 1 项补充）：经 0-W 继承的代表协议——恢复段 run（ar1）批次的父 campaign-run 在段 run 子进程写出
        失败诊断后被看门狗中止，run 期间发布的段预约按 attempt 对账收据分流。处理型 execution-failure 时，段对账、批准
        并消费恢复预览后，后继段协议（15）按 failed 同一判据承接 ar2，且 15 条协议里只有这一条承接；诊断换成永久失败类
        即在 0-W 失败关闭——修复前 0-W 在有预约时只核对 attempt 收据，永久失败类的看门狗中止也会被后继段协议放行。

        父 run 是合成的：账本收口与既有 failed 段用例一样显式调用父监督器收账函数。看门狗中止的父进程自己来不及收账，
        有预约时账本不会自动进入 recovery_required，恢复预览也就无从消费——这是另一处缺口，不在本用例范围内。"""

        import os
        import sys
        import time

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            failing_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            failed_result = lambda job, *a, **k: {
                "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "failed", "description": job.description, "duration_seconds": 0.0, "steps": [{"argv": ["sh"], "return_code": 3, "log": ""}],
                "evidence_roots": [], "missing_evidence_patterns": list(job.evidence_roots), "empty_evidence_patterns": [], "covers": [],
                "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [], "track": "main", "model_id": "",
                "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
                "model_condition_receipt_failure": None, "disposition": "executed",
            }
            # 父批次 vc-5-0007 取得执行权的时刻：原 attempt 的预约早于它，段 ar1 的预约在它之后发布（run 期间的预约）。
            run_started = time.time()
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(codex_upgrade, "run_job", side_effect=failed_result):
                self.assertEqual(codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")["status"], "failed")
            commit = artifacts.validate_evaluation_baseline_commit(_read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "COMMIT"))

            # 看门狗中止的父 run：state／stop receipt 是看门狗中止本身；段 run 子进程写出的失败诊断留在 run 目录。
            campaign_id = str(fixture["manifest"]["campaign_id"])
            prior_dir = root / "run-ar1"
            prior_dir.mkdir(mode=0o700)
            owner_nonce = "8" * 64
            prior_state = {
                "state": "watchdog-aborted", "campaign_id": campaign_id, "phase": "VC-5", "owner_pid": os.getpid(),
                "owner_nonce": owner_nonce, "terminal_at_utc": "2026-09-28T01:00:00Z", "started_at_epoch": run_started,
            }
            stop = {
                "schema_version": supervisor.STOP_SCHEMA, "campaign_id": campaign_id, "detected_at_epoch": 1005.0,
                "detected_at_utc": "2026-09-28T01:00:00Z", "event_type": "watchdog-aborted", "owner_nonce": owner_nonce,
                "owner_pid": os.getpid(), "phase": "VC-5", "reason": "owner-process-not-alive",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            for name, document in (("state.json", prior_state), ("stop-receipt.json", stop)):
                path = prior_dir / name
                path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                path.chmod(0o600)
            segment_command = [
                sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                "--candidate-id", R1, "--attempt-recovery", "ar1", "--heartbeat-seconds", "1",
            ]
            prior_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": str(codex_upgrade._vc_campaign_plan(campaign_dir, fixture["manifest"])["plan_sha256"]),
                "batch_id": "vc-5-0007", "batch_sequence": 7, "batch_sha256": "7" * 64, "phase": "VC-5",
                "predecessor_checkpoint": {"path": "control/vc/vc-4-checkpoint.json", "sha256": "3" * 64, "phase": "VC-4", "checkpoint_sha256": "4" * 64},
                "original_deadline_at_utc": "2099-09-15T08:12:43Z", "no_op": False,
                "actions": [{"action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 600.0, "command": segment_command, "item_ids": [job_ids[0]]}],
                "execute_items": [job_ids[0]], "reuse_items": [],
                "candidate_revision": 1, "candidate_id": R1, "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"],
            }
            diagnostic_path = supervisor._action_diagnostic_path(prior_dir, "ar-run", create_directory=True)

            def write_diagnostic(failure_kind: str, error_type: str, failure_class: str) -> None:
                supervisor._write_action_diagnostic(
                    diagnostic_path, campaign_id=campaign_id, phase="VC-5", action_id="ar-run", owner_pid=os.getpid(),
                    owner_nonce=owner_nonce, failure_kind=failure_kind, failure_class=failure_class, error_type=error_type,
                    message="D-07 段 run 夹具。",
                )

            write_diagnostic("child-returncode", "ChildProcessError", "execution-failure")
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, prior_manifest, failed_action_id="ar-run", failure_class="execution-failure"
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_root.name, recovery_revision="ar1")
            self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                approval = reconciler.approve_recovery_preview(
                    campaign_dir, attempt_root.name, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
            self.assertEqual(approval["approved_sha256"], reconciled["recovery_preview"]["review_sha256"])
            preview_path = Path(reconciled["recovery_preview_path"])
            with self._segment_patches_started(context, failing_jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_root.name, preview_path, recovery_revision="ar1")
            self.assertEqual(authorized["status"], "authorized", authorized)
            successor = dict(
                prior_manifest, batch_id="vc-5-0008", batch_sequence=8, batch_sha256="8" * 64,
                actions=[{
                    "action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 900.0,
                    "command": [
                        sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                        "--candidate-id", R1, "--attempt-recovery", "ar2", "--rerun-failed", "--recovery-preview", str(preview_path),
                    ],
                    "item_ids": [job_ids[0]],
                    "output_bindings": [f"candidates/{R1}/attempts/{attempt_root.name}/recovery/ar2/attempt-recovery.json"],
                }],
            )

            def accepting_protocols() -> list[str]:
                accepted: list[str] = []
                for name, protocol in supervisor._SUCCESSOR_PROTOCOLS:
                    try:
                        if protocol(prior_state, prior_manifest, prior_dir, successor, campaign_dir=campaign_dir):
                            accepted.append(name)
                    except supervisor.SupervisorError:
                        pass
                return accepted

            self.assertTrue(
                supervisor._validate_attempt_recovery_segment_successor(
                    prior_state, prior_manifest, prior_dir, successor, campaign_dir=campaign_dir
                )
            )
            self.assertEqual(accepting_protocols(), ["attempt_recovery_segment"])
            # 诊断换成永久失败类：0-W 失败关闭（段预约的 attempt 对账收据不绑定父 run 的诊断，改写只改变后继判据的输入）。
            diagnostic_path.chmod(0o600)
            diagnostic_path.unlink()
            write_diagnostic("handled-error", "PolicyDrift", "identity-drift")
            with self.assertRaisesRegex(supervisor.SupervisorError, "后继恢复段：看门狗中止父 run run-ar1 的动作诊断是永久失败类 identity-drift"):
                supervisor._validate_attempt_recovery_segment_successor(
                    prior_state, prior_manifest, prior_dir, successor, campaign_dir=campaign_dir
                )
            self.assertEqual(accepting_protocols(), [])

    def _item45_owner_lost_segment(
        self,
        root: Path,
        *,
        diagnostic: tuple[str, str, str] = ("child-returncode", "ChildProcessError", "execution-failure"),
        shape: str = "watchdog",
    ) -> dict:
        """第 45 项夹具：真实的失败恢复段 ar1（段预约、账本 attempt_recovery_started 均已落盘），发布它的父 campaign-run
        （批次 vc-5-0007，已登记 COMMIT）在段 run 子进程写出 execution-failure 诊断后丢失 owner、被看门狗中止——父监督器
        没来得及收账，账本停在 active。父 run 用完整的监督器 state（owner 进程已退出），窗口包住段预约时刻。

        第 48 项：``diagnostic`` 可换成段 run 的其它失败诊断；``shape`` 取 ``watchdog``（看门狗中止＋诊断）、``r2``（R2 封存）
        或 ``alive``（父进程自己封存：stop 原因 action-failed:ar-run、没有 owner-check 事件，不是 R2；账本收口由调用方以父
        监督器收账函数完成）。"""

        import sys

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        fixture, context = self._failed_b0_and_reconciled(root)
        campaign_dir = context["campaign_dir"]
        attempt_root = context["attempt_root"]
        job_ids = context["job_ids"]
        original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
        self.assertEqual(self._apply_transient(fixture, context)["status"], "applied")
        failing_jobs = [
            Job(
                job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
            )
            for job_id in job_ids
        ]
        failed_result = lambda job, *a, **k: {
            "id": job.job_id, "phase": "candidate", "required": True, "execution_sha256": codex_upgrade._job_execution_sha256(job),
            "status": "failed", "description": job.description, "duration_seconds": 0.0, "steps": [{"argv": ["sh"], "return_code": 3, "log": ""}],
            "evidence_roots": [], "missing_evidence_patterns": list(job.evidence_roots), "empty_evidence_patterns": [], "covers": [],
            "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [], "track": "main", "model_id": "",
            "expected_use_responses_lite": False, "required_model_receipt": False, "model_condition_receipt": None,
            "model_condition_receipt_failure": None, "disposition": "executed",
        }
        with self._segment_patches_started(context, failing_jobs), mock.patch.object(codex_upgrade, "run_job", side_effect=failed_result):
            self.assertEqual(codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")["status"], "failed")
        commit = artifacts.validate_evaluation_baseline_commit(_read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "COMMIT"))
        prior_manifest = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": str(fixture["manifest"]["campaign_id"]),
            "campaign_plan_sha256": str(codex_upgrade._vc_campaign_plan(campaign_dir, fixture["manifest"])["plan_sha256"]),
            "batch_id": "vc-5-0007", "batch_sequence": 7, "batch_sha256": "7" * 64, "phase": "VC-5",
            "predecessor_checkpoint": {"path": "control/vc/vc-4-checkpoint.json", "sha256": "3" * 64, "phase": "VC-4", "checkpoint_sha256": "4" * 64},
            "original_deadline_at_utc": "2099-09-15T08:12:43Z", "no_op": False,
            "actions": [{
                "action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 600.0,
                "command": [
                    sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                    "--candidate-id", R1, "--attempt-recovery", "ar1", "--heartbeat-seconds", "1",
                ],
                "item_ids": [job_ids[0]],
            }],
            "execute_items": [job_ids[0]], "reuse_items": [],
            "candidate_revision": 1, "candidate_id": R1, "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"],
        }
        state_root = root / "supervisor-state"
        state_root.mkdir(mode=0o700)
        pseudo = {"control": state_root, "manifest": fixture["manifest"], "campaign_dir": campaign_dir}
        segment_reservation = attempt_root / codex_upgrade.ATTEMPT_RECOVERY_DIRNAME / "ar1" / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME
        if shape == "alive":
            reserved = json.loads(segment_reservation.read_text(encoding="utf-8"))["started_at_utc"]
            offset = datetime.fromisoformat(str(reserved).replace("Z", "+00:00")).timestamp() - 0.5 - time.time()
            prior_dir = self.helper._b0_run_dir(
                pseudo, "a" * 64, phase="VC-5", state="failed", batched_manifest=prior_manifest, failure_class=diagnostic[2],
                action_id="ar-run", failure_kind=diagnostic[0], error_type=diagnostic[1], started_offset_seconds=offset,
            )
            self.helper._item45_bind_commit(campaign_dir, prior_dir, prior_manifest)
            prior_state = json.loads((prior_dir / "state.json").read_text(encoding="utf-8"))
        else:
            prior_state, prior_dir = self.helper._item45_owner_lost_run(
                pseudo, "a" * 64, inner=prior_manifest, action_id="ar-run", phase="VC-5", reservation_path=segment_reservation,
                diagnostic=diagnostic, shape=shape,
            )
        self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "active")
        return {
            "fixture": fixture, "context": context, "campaign_dir": campaign_dir, "attempt_root": attempt_root, "job_ids": job_ids,
            "failing_jobs": failing_jobs, "prior_manifest": prior_manifest, "prior_state": prior_state, "prior_dir": prior_dir,
        }

    def _item45_segment_successor(self, case: dict, preview_path: Path) -> dict:
        import sys

        campaign_dir, attempt_root = case["campaign_dir"], case["attempt_root"]
        return dict(
            case["prior_manifest"], batch_id="vc-5-0008", batch_sequence=8, batch_sha256="8" * 64,
            actions=[{
                "action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 900.0,
                "command": [
                    sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
                    "--candidate-id", R1, "--attempt-recovery", "ar2", "--rerun-failed", "--recovery-preview", str(preview_path),
                ],
                "item_ids": [case["job_ids"][0]],
                "output_bindings": [f"candidates/{R1}/attempts/{attempt_root.name}/recovery/ar2/attempt-recovery.json"],
            }],
        )

    def _item45_accepting(self, case: dict, successor: dict) -> list[str]:
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        accepted: list[str] = []
        for name, protocol in supervisor._SUCCESSOR_PROTOCOLS:
            try:
                if protocol(case["prior_state"], case["prior_manifest"], case["prior_dir"], successor, campaign_dir=case["campaign_dir"]):
                    accepted.append(name)
            except supervisor.SupervisorError:
                pass
        return accepted

    def test_item45_owner_lost_segment_reconciliation_backfills_recovery_then_protocol_15_accepts(self) -> None:
        """第 45 项（有预约路径的死路）：VC-5 恢复段 ar1 执行失败，父 campaign-run 来不及收账（看门狗中止＋诊断），账本停在
        active。修复前 reconcile-attempt 只登记 attempt_recovery_failed、判 recoverable 并给出恢复预览，但账本不在
        recovery_required，批准并消费预览不写 recovery_authorized，协议 15 拒绝后继段 ar2（"缺少账本 recovery_authorized"），
        无路可走。修复后 reconcile-attempt 沿 COMMIT 找到发布段预约的父 run，以父监督器同一收账函数补 recovery_required，
        恢复预览冻结在可授权的账本 head 上；批准并消费后写 recovery_authorized、阶段回到 active，后继段 ar2 有且只有协议 15
        承接；重复对账不再补账。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            case = self._item45_owner_lost_segment(root)
            campaign_dir, attempt_id = case["campaign_dir"], case["attempt_root"].name
            ledger_dir = Path(str(case["fixture"]["timing_ledger"]))
            with self._segment_patches_started(case["context"], case["failing_jobs"]), \
                    mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
                backfill = reconciled.get("ledger_closeout_backfill")
                self.assertIsNotNone(backfill, reconciled)
                self.assertEqual(
                    (backfill["run_id"], backfill["action_id"], backfill["failure_class"], backfill["ledger_status"]),
                    (case["prior_dir"].name, "ar-run", "execution-failure", "recovery_required"),
                )
                events = [event for event, _raw in timing_ledger._load_events(ledger_dir)]
                self.assertEqual([event["event_type"] for event in events[-2:]], ["attempt_recovery_failed", "recovery_required"])
                again = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertNotIn("ledger_closeout_backfill", again)
                self.assertEqual(len(timing_ledger._load_events(ledger_dir)), len(events))
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
                preview_path = Path(reconciled["recovery_preview_path"])
                authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_id, preview_path, recovery_revision="ar1")
            self.assertIsNotNone(authorized["timing_recovery_event"], authorized)
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "active")
            self.assertEqual(self._item45_accepting(case, self._item45_segment_successor(case, preview_path)), ["attempt_recovery_segment"])

    def test_item45_stuck_segment_reconciled_by_old_tooling_continues_after_fix(self) -> None:
        """第 45 项"修好后能接着跑"：旧工具已对账过的死路现场（段已登记失败、预览已批准，但消费预览不写 recovery_authorized，
        协议 15 拒绝 ar2）。部署修复后重新执行 reconcile-attempt：账本自这次失败之后没有推进（只多了本段的失败登记），
        于是补 recovery_required，给出冻结在可授权 head 上的新恢复预览；批准并消费新预览后协议 15 唯一承接 ar2。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            case = self._item45_owner_lost_segment(root)
            campaign_dir, attempt_id = case["campaign_dir"], case["attempt_root"].name
            ledger_dir = Path(str(case["fixture"]["timing_ledger"]))
            with self._segment_patches_started(case["context"], case["failing_jobs"]), \
                    mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                with mock.patch.object(reconciler, "_backfill_attempt_owner_closeout", return_value=None):
                    old = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=old["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
                old_preview = Path(old["recovery_preview_path"])
                stuck = reconciler.authorize_recovery_preview(campaign_dir, attempt_id, old_preview, recovery_revision="ar1")
                self.assertIsNone(stuck["timing_recovery_event"], stuck)
                self.assertEqual(self._item45_accepting(case, self._item45_segment_successor(case, old_preview)), [])
                fixed = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertEqual(fixed["ledger_closeout_backfill"]["ledger_status"], "recovery_required", fixed)
                new_preview = Path(fixed["recovery_preview_path"])
                self.assertNotEqual(new_preview, old_preview)
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=fixed["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
                authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_id, new_preview, recovery_revision="ar1")
            self.assertIsNotNone(authorized["timing_recovery_event"], authorized)
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "active")
            self.assertEqual(self._item45_accepting(case, self._item45_segment_successor(case, new_preview)), ["attempt_recovery_segment"])

    def test_item48_segment_deadline_failure_closes_out_as_segment_recovery_then_protocol_15_accepts(self) -> None:
        """第 48 项：恢复段 ar1 在段预约之后以截止类失败（WallClockTimeoutError，deadline-expired）收口。修复前它不算段失败，
        收账落到候选审核分支——owner 在线时段仍 active，stage_abandoned 被账本拒绝、收账失败，账本停在 active，授权不写
        recovery_authorized，协议 15 拒绝 ar2（死路）；owner 丢失（看门狗中止＋诊断、R2 封存）时第 45 项补账进候选审核，对账
        提示"批准预览后开 ar2"，授权却拒绝恢复段。修复后三种形态都按段失败进入 recovery_required（截止类文案：先延期、
        不需要部署），段对账可恢复 → 批准 → 授权写 recovery_authorized、阶段回到 active → 后继段 ar2 有且只有协议 15 承接。"""

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        deadline = ("handled-error", "WallClockTimeoutError", "deadline-expired")
        for shape in ("alive", "watchdog", "r2"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                root.chmod(0o700)
                case = self._item45_owner_lost_segment(root, diagnostic=deadline, shape=shape)
                campaign_dir, attempt_id = case["campaign_dir"], case["attempt_root"].name
                ledger_dir = Path(str(case["fixture"]["timing_ledger"]))
                if shape == "alive":
                    # 父监督器自己收账（段仍 active）：按段失败进入 recovery_required。
                    closeout = supervisor._close_failed_campaign_timing_ledger(
                        campaign_dir, case["prior_manifest"], failed_action_id="ar-run", failure_class="deadline-expired"
                    )
                    self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
                    self.assertIn("deadline-extend", closeout["next_action"])
                    self.assertIn("--attempt-recovery ar2", closeout["next_action"])
                with self._segment_patches_started(case["context"], case["failing_jobs"]), \
                        mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                    reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                    self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
                    backfill = reconciled.get("ledger_closeout_backfill")
                    if shape == "alive":
                        self.assertIsNone(backfill, reconciled)
                    else:
                        self.assertIsNotNone(backfill, reconciled)
                        self.assertEqual(
                            (backfill["failure_class"], backfill["ledger_status"]), ("deadline-expired", "recovery_required")
                        )
                    events = [event for event, _raw in timing_ledger._load_events(ledger_dir)]
                    self.assertIn("attempt_recovery_failed", [event["event_type"] for event in events])
                    self.assertNotIn("candidate_review_required", [event["event_type"] for event in events])
                    self.assertIn("--attempt-recovery ar2", reconciled["next_command"])
                    reconciler.approve_recovery_preview(
                        campaign_dir, attempt_id, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                    )
                    preview_path = Path(reconciled["recovery_preview_path"])
                    authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_id, preview_path, recovery_revision="ar1")
                self.assertTrue(authorized["timing_recovery_event"]["appended"], authorized)
                self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "active")
                self.assertEqual(
                    self._item45_accepting(case, self._item45_segment_successor(case, preview_path)), ["attempt_recovery_segment"]
                )

    def test_item48_segment_failure_routing_depends_on_class_and_segment_reservation(self) -> None:
        """第 48 项的收账判定边界（纯函数），第 53 项起按段是否已预约分两路：段已发布预约时，执行失败与截止类失败按段失败
        收口（段对账后开 ar<k+1>）；段预约之前的执行失败与截止类失败改按同一段号重派收口（第 53 项，此前执行失败也按段失败
        收口、截止类进候选审核，都没有可走的续跑）；其余审核类与永久失败类两路都不走；不是恢复段 run 的动作不算。段是否已预约
        由账本推断的一路：该段号有 active 恢复段。"""

        import sys

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        manifest = {"actions": [
            {"action_id": "ar-run", "command": [sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run",
                                                "--candidate-id", R1, "--attempt-recovery", "ar3"]},
            {"action_id": "capture", "command": [sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run",
                                                 "--candidate-id", R1]},
        ]}
        route = supervisor._attempt_recovery_segment_failure
        redispatch = supervisor._attempt_recovery_segment_redispatch
        for failure_class, reserved, expected_segment, expected_redispatch in (
            ("execution-failure", False, None, "ar3"),
            ("execution-failure", True, "ar3", None),
            ("deadline-expired", True, "ar3", None),
            ("deadline-expired", False, None, "ar3"),
            ("request-accounting-uncertain", True, None, None),
            ("request-accounting-uncertain", False, None, None),
            ("restoration-failed", True, None, None),
            ("restoration-failed", False, None, None),
        ):
            with self.subTest(failure_class=failure_class, reserved=reserved):
                self.assertEqual(route(manifest, "ar-run", failure_class, segment_reserved=reserved), expected_segment)
                self.assertEqual(redispatch(manifest, "ar-run", failure_class, segment_reserved=reserved), expected_redispatch)
        self.assertIsNone(route(manifest, "capture", "execution-failure", segment_reserved=True))
        self.assertIsNone(redispatch(manifest, "capture", "execution-failure", segment_reserved=False))
        active = supervisor._attempt_recovery_segment_active
        summary = {"attempt_recoveries": {
            "a1:ar2": {"attempt_id": "a1", "recovery_revision": "ar2", "status": "failed"},
            "a1:ar3": {"attempt_id": "a1", "recovery_revision": "ar3", "status": "active"},
        }}
        self.assertTrue(active(summary, "ar3"))
        self.assertFalse(active(summary, "ar2"))
        self.assertFalse(active(summary, "ar4"))
        self.assertFalse(active({}, "ar3"))

    def test_item48_segment_under_candidate_review_is_not_prompted_to_open_successor(self) -> None:
        """第 48 项（提示与放行一致）：恢复段失败经补账进入候选审核——这里用仍按审核类路由的 request-accounting-uncertain
        诊断构造（段 run 实际不做就绪 probe、不会产生它；第 48 项之前按旧口径收口的截止类段失败现场同理）。授权对候选审核下
        的恢复段一律拒绝（候选审核只允许 VC-5 采集失败 attempt 的续跑授权）；修复前对账仍提示"批准预览后开 ar2"并接受
        批准，修复后提示候选审核的处置（invalidate-candidate 或 close-campaign-ledger），批准被拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            case = self._item45_owner_lost_segment(
                root, diagnostic=("handled-error", "ProbeAccountingUncertainError", "request-accounting-uncertain"), shape="watchdog"
            )
            campaign_dir, attempt_id = case["campaign_dir"], case["attempt_root"].name
            ledger_dir = Path(str(case["fixture"]["timing_ledger"]))
            with self._segment_patches_started(case["context"], case["failing_jobs"]), \
                    mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertEqual(reconciled["ledger_closeout_backfill"]["ledger_status"], "candidate_review_required", reconciled)
                self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "candidate_review_required")
                self.assertIn("candidate_review_required", reconciled["next_command"])
                self.assertIn("invalidate-candidate", reconciled["next_command"])
                self.assertNotIn("--attempt-recovery ar2", reconciled["next_command"])
                with self.assertRaisesRegex(reconciler.ReconcilerError, "候选审核下不接受恢复段续跑批准"):
                    reconciler.reconcile_attempt(
                        campaign_dir, attempt_id, recovery_revision="ar1",
                        approve_recovery_sha256=reconciled["recovery_preview"]["review_sha256"],
                    )
                # 与之一致：即使绕过对账直接批准预览，授权同样拒绝恢复段，协议 15 也不承接后继段。
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
                preview_path = Path(reconciled["recovery_preview_path"])
                with self.assertRaises(reconciler.ReconcilerError):
                    reconciler.authorize_recovery_preview(campaign_dir, attempt_id, preview_path, recovery_revision="ar1")
            self.assertEqual(self._item45_accepting(case, self._item45_segment_successor(case, preview_path)), [])

    def test_item50_owner_alive_segment_permanent_failure_is_closed_out_after_segment_reconciliation(self) -> None:
        """第 50 项（第 48 项遗留）：恢复段 ar1 在段预约之后以永久失败类（restoration-failed）收口，owner 在线。父监督器收账走永久
        分支先写 stage_abandoned，此时段仍 active，被账本拒绝、收账失败，账本停在 active。修复前段对账登记段失败后不补收口，
        判 recoverable 并提示"批准预览后开 ar2"——永久类却被提示续跑，而授权不写 recovery_authorized、协议 15 拒绝。修复后段对账
        把段登记为失败之后，以父监督器同一收账函数补做收口：停线，对账永久停线。"""

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        permanent = ("handled-error", "RestorationError", "restoration-failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            case = self._item45_owner_lost_segment(root, diagnostic=permanent, shape="alive")
            campaign_dir, attempt_id = case["campaign_dir"], case["attempt_root"].name
            ledger_dir = Path(str(case["fixture"]["timing_ledger"]))
            with self.assertRaisesRegex(supervisor.SupervisorError, "stage_abandoned"):
                supervisor._close_failed_campaign_timing_ledger(
                    campaign_dir, case["prior_manifest"], failed_action_id="ar-run", failure_class="restoration-failed"
                )
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "active")
            with self._segment_patches_started(case["context"], case["failing_jobs"]), \
                    mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
            backfill = reconciled.get("ledger_closeout_backfill")
            self.assertIsNotNone(backfill, reconciled)
            self.assertEqual((backfill["failure_class"], backfill["ledger_status"]), ("restoration-failed", "stopped"))
            self.assertEqual(reconciled["status"], reconciler.DECISION_STOP, reconciled.get("decision"))
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "stopped")

    # ---- 第 53 项：恢复段在段预约之前失败 ------------------------------------------------------------

    @staticmethod
    def _item53_source_jobs(context: dict) -> list[Job]:
        """候选 Job 全集（步骤真实执行、证据写到段重定位后的新根），与既有段用例同形。"""

        original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
        return [
            Job(
                job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(),
                scenario_ids=("A03",),
            )
            for job_id in context["job_ids"]
        ]

    @staticmethod
    def _item53_segment_manifest(
        fixture: dict, campaign_dir: Path, job_ids: list[str], *, sequence: int, revision: str, preview: Path | None = None,
    ) -> dict:
        """某个恢复段 run 的单动作批次清单（与既有段用例同形）；后继段带 ``--rerun-failed --recovery-preview``。"""

        import sys

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        commit = artifacts.validate_evaluation_baseline_commit(_read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "COMMIT"))
        command = [
            sys.executable, "/managed/codex_upgrade.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir),
            "--candidate-id", R1, "--attempt-recovery", revision,
        ]
        if preview is not None:
            command += ["--rerun-failed", "--recovery-preview", str(preview)]
        command += ["--heartbeat-seconds", "1"]
        return {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": str(fixture["manifest"]["campaign_id"]),
            "campaign_plan_sha256": str(codex_upgrade._vc_campaign_plan(campaign_dir, fixture["manifest"])["plan_sha256"]),
            "batch_id": f"vc-5-{sequence:04d}", "batch_sequence": sequence, "batch_sha256": str(sequence) * 64, "phase": "VC-5",
            "predecessor_checkpoint": {"path": "control/vc/vc-4-checkpoint.json", "sha256": "3" * 64, "phase": "VC-4", "checkpoint_sha256": "4" * 64},
            "original_deadline_at_utc": "2099-09-15T08:12:43Z", "no_op": False,
            "actions": [{
                "action_id": "ar-run", "operation": "VC-5:attempt-recovery-run", "timeout_seconds": 600.0, "command": command,
                "item_ids": [job_ids[0]],
            }],
            "execute_items": [job_ids[0]], "reuse_items": [],
            "candidate_revision": 1, "candidate_id": R1, "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"],
        }

    def _item53_prereservation_run(
        self, pseudo: dict, campaign_dir: Path, name: str, manifest: dict, *, diagnostic: tuple[str, str, str], shape: str,
    ) -> tuple[dict, Path]:
        """发布某个恢复段批次的父 campaign-run：段 run 在段预约之前失败、写出 ``diagnostic``——``alive`` 父进程自己封存（stop
        原因 action-failed:ar-run），``r2`` 父进程在收账前丢失、monitor 按 R2 封存；已登记 COMMIT。父 run 窗口从半秒前开始，晚于
        原 attempt 与既有段的预约（本 run 期间没有任何预约）。"""

        if shape == "alive":
            run_dir = self.helper._b0_run_dir(
                pseudo, name, phase="VC-5", state="failed", batched_manifest=manifest, failure_class=diagnostic[2],
                action_id="ar-run", failure_kind=diagnostic[0], error_type=diagnostic[1], started_offset_seconds=-0.5,
            )
        else:
            _state, run_dir = self.helper._r2_sealed_run(
                pseudo, name, inner=manifest, action_id="ar-run", phase="VC-5", diagnostic=diagnostic,
                action_failed_reason="returncode=1", started_offset_seconds=-0.5,
            )
        self.helper._item45_bind_commit(campaign_dir, run_dir, manifest)
        return json.loads((run_dir / "state.json").read_text(encoding="utf-8")), run_dir

    def test_item53_segment_failure_before_reservation_redispatches_same_segment(self) -> None:
        """第 53 项：恢复段 ar1（当前基线冻结的首段）的 run 在段预约之前失败——预约准入处抛错，段目录与账本段事件都不存在：
        ① owner 在线、执行失败（ConfigurationError）；② owner 在线、截止类失败（WallClockTimeoutError）；③ 截止类失败、父进程在
        收账前丢失被 R2 封存（对账补账）。

        修复前：① 收账按段失败收口，提示"reconcile-attempt --recovery-revision ar1 后开 ar2"（该段没有预约可对账），
        reconcile-supervisor-run 又以"账本处于 recovery_required，但父动作分类不可恢复"拒绝入账——死路；②③ 落到候选审核，只能
        作废候选或停线。修复后三种形态都收为 recovery_required（同段重派文案），reconcile-supervisor-run 入账、账本回到 active 并
        提示按 N+1 重派同一恢复段 ar1；N+1 同段重派只有协议 15 承接，后继段 ar2（没有可授权的段预览）不承接；真实重跑 ar1 发布段
        预约、登记账本并正常收口。"""

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        cases = (
            ("execution-failure", codex_upgrade.ConfigurationError("合成：段预约准入前的工具缺陷"),
             ("handled-error", "ConfigurationError", "execution-failure"), "alive"),
            ("deadline-expired", incremental_recovery.WallClockTimeoutError(
                "attempt-recovery:reservation-admission", elapsed_seconds=600.0, budget_seconds=600.0),
             ("handled-error", "WallClockTimeoutError", "deadline-expired"), "alive"),
            ("deadline-expired-r2", incremental_recovery.WallClockTimeoutError(
                "attempt-recovery:reservation-admission", elapsed_seconds=600.0, budget_seconds=600.0),
             ("handled-error", "WallClockTimeoutError", "deadline-expired"), "r2"),
        )
        for label, error, diagnostic, shape in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                root.chmod(0o700)
                fixture, context = self._failed_b0_and_reconciled(root)
                campaign_dir, attempt_root, job_ids = context["campaign_dir"], context["attempt_root"], context["job_ids"]
                ledger_dir = Path(str(fixture["timing_ledger"]))
                self.assertEqual(self._apply_transient(fixture, context)["status"], "applied")
                jobs = self._item53_source_jobs(context)
                with self._segment_patches_started(context, jobs), mock.patch.object(
                    codex_upgrade, "_require_capture_budget_before_data_action", side_effect=error
                ):
                    with self.assertRaises(type(error)):
                        codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
                segment_root = attempt_root / codex_upgrade.ATTEMPT_RECOVERY_DIRNAME / "ar1"
                self.assertFalse(segment_root.exists())
                self.assertNotIn(f"{attempt_root.name}:ar1", timing_ledger.inspect_ledger(ledger_dir)["attempt_recoveries"])
                manifest = self._item53_segment_manifest(fixture, campaign_dir, job_ids, sequence=7, revision="ar1")
                state_root = root / "supervisor-state"
                state_root.mkdir(mode=0o700)
                pseudo = {"control": state_root, "manifest": fixture["manifest"], "campaign_dir": campaign_dir}
                prior_state, prior_dir = self._item53_prereservation_run(
                    pseudo, campaign_dir, "a" * 64, manifest, diagnostic=diagnostic, shape=shape
                )
                case = {"prior_state": prior_state, "prior_manifest": manifest, "prior_dir": prior_dir, "campaign_dir": campaign_dir}
                if shape == "alive":
                    closeout = supervisor._close_failed_campaign_timing_ledger(
                        campaign_dir, manifest, failed_action_id="ar-run", failure_class=diagnostic[2]
                    )
                    self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
                    self.assertIn("重派同一恢复段 ar1", closeout["next_action"])
                    self.assertNotIn("ar2", closeout["next_action"])
                with self._segment_patches_started(context, jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                    reconciled = reconciler.reconcile_supervisor_run(prior_dir, campaign_dir)
                self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
                if shape == "r2":
                    self.assertEqual(reconciled["ledger_closeout_backfill"]["ledger_status"], "recovery_required", reconciled)
                self.assertIn("重派同一恢复段 ar1", reconciled["next_command"])
                self.assertNotIn("invalidate-candidate", reconciled["next_command"])
                summary = timing_ledger.inspect_ledger(ledger_dir)
                self.assertEqual(
                    (summary["status"], summary["active_phase"], summary["next_action"]), ("active", "VC-5", "redispatch-same-batch")
                )
                same = dict(manifest, batch_id="vc-5-0008", batch_sequence=8, batch_sha256="8" * 64)
                self.assertEqual(self._item45_accepting(case, same), ["attempt_recovery_segment"])
                successor = self._item53_segment_manifest(
                    fixture, campaign_dir, job_ids, sequence=8, revision="ar2", preview=root / "no-such-preview.json"
                )
                self.assertEqual(self._item45_accepting(case, successor), [])
                # 修好接着跑：同一段号真实重跑，段预约发布并登记账本。
                with self._segment_patches_started(context, jobs):
                    rerun = codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
                self.assertEqual(rerun["status"], "awaiting_receipts", rerun)
                self.assertTrue((segment_root / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME).is_file())
                self.assertIn(f"{attempt_root.name}:ar1", timing_ledger.inspect_ledger(ledger_dir)["attempt_recoveries"])

    def test_item53_successor_segment_failure_before_reservation_redispatches_after_rereconciling_previous(self) -> None:
        """第 53 项（后继段）：ar1 执行失败（段已预约）→ 父监督器收账 → reconcile-attempt --recovery-revision ar1 → 批准并授权恢复
        预览 → 派发 ar2 的批次，ar2 的段 run 在消费已授权预览之后、段预约之前以截止类失败收口（段目录与账本段事件都不存在）。

        修复前收账把它当作未预约的截止失败送进候选审核（只能作废候选或停线）。修复后收为 recovery_required，reconcile-supervisor-run
        入账、账本回到 active，提示先对前序段 ar1 重新对账、批准新预览——本次入账在项目总账追加了本 Campaign 的事件，原预览按冻结
        的总账事件核对已不能消费（真实重跑用原预览被拒）；重新对账 ar1 得到新预览、批准后，N+1 同段重派 ar2 只有协议 15 承接，真实
        重跑 ar2 消费新预览并发布段预约。"""

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            case = self._item45_owner_lost_segment(root, shape="alive")
            campaign_dir, attempt_root, job_ids = case["campaign_dir"], case["attempt_root"], case["job_ids"]
            attempt_id = attempt_root.name
            fixture, context = case["fixture"], case["context"]
            ledger_dir = Path(str(fixture["timing_ledger"]))
            jobs = self._item53_source_jobs(context)
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, case["prior_manifest"], failed_action_id="ar-run", failure_class="execution-failure"
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
            with self._segment_patches_started(context, jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertEqual(reconciled["status"], "recoverable", reconciled.get("decision"))
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=reconciled["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
                old_preview = Path(reconciled["recovery_preview_path"])
                authorized = reconciler.authorize_recovery_preview(campaign_dir, attempt_id, old_preview, recovery_revision="ar1")
            self.assertTrue(authorized["timing_recovery_event"]["appended"], authorized)
            deadline = incremental_recovery.WallClockTimeoutError(
                "attempt-recovery:reservation-admission", elapsed_seconds=600.0, budget_seconds=600.0
            )

            def ar2_arguments(preview: Path) -> argparse.Namespace:
                arguments = self._run_arguments(campaign_dir, "ar2")
                arguments.rerun_failed = True
                arguments.recovery_preview = preview
                return arguments

            with self._segment_patches_started(context, jobs), mock.patch.object(
                codex_upgrade, "_require_capture_budget_before_data_action", side_effect=deadline
            ):
                with self.assertRaises(incremental_recovery.WallClockTimeoutError):
                    codex_upgrade._run_capture_attempt(ar2_arguments(old_preview), "candidate")
            ar2_root = attempt_root / codex_upgrade.ATTEMPT_RECOVERY_DIRNAME / "ar2"
            self.assertFalse(ar2_root.exists())
            ar2_manifest = self._item53_segment_manifest(fixture, campaign_dir, job_ids, sequence=8, revision="ar2", preview=old_preview)
            pseudo = {"control": Path(case["prior_dir"]).parent, "manifest": fixture["manifest"], "campaign_dir": campaign_dir}
            prior_state, prior_dir = self._item53_prereservation_run(
                pseudo, campaign_dir, "b" * 64, ar2_manifest,
                diagnostic=("handled-error", "WallClockTimeoutError", "deadline-expired"), shape="alive",
            )
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir, ar2_manifest, failed_action_id="ar-run", failure_class="deadline-expired"
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required", closeout)
            self.assertIn("重派同一恢复段 ar2", closeout["next_action"])
            self.assertIn("reconcile-attempt --recovery-revision ar1", closeout["next_action"])
            with self._segment_patches_started(context, jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                reconciled_run = reconciler.reconcile_supervisor_run(prior_dir, campaign_dir)
            self.assertEqual(reconciled_run["status"], "recoverable", reconciled_run.get("decision"))
            self.assertIn("reconcile-attempt --recovery-revision ar1", reconciled_run["next_command"])
            self.assertIn("重派同一恢复段 ar2", reconciled_run["next_command"])
            self.assertEqual(timing_ledger.inspect_ledger(ledger_dir)["status"], "active")
            # 原预览已不能消费：本次对账在项目总账追加了本 Campaign 的事件。
            with self._segment_patches_started(context, jobs):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须重新对账"):
                    codex_upgrade._run_capture_attempt(ar2_arguments(old_preview), "candidate")
            self.assertFalse(ar2_root.exists())
            with self._segment_patches_started(context, jobs), mock.patch.object(reconciler, "_deployment_receipt", return_value=None):
                again = reconciler.reconcile_attempt(campaign_dir, attempt_id, recovery_revision="ar1")
                self.assertEqual(again["status"], "recoverable", again.get("decision"))
                new_preview = Path(again["recovery_preview_path"])
                self.assertNotEqual(new_preview, old_preview)
                reconciler.approve_recovery_preview(
                    campaign_dir, attempt_id, approve_sha256=again["recovery_preview"]["review_sha256"], recovery_revision="ar1"
                )
            case2 = {"prior_state": prior_state, "prior_manifest": ar2_manifest, "prior_dir": prior_dir, "campaign_dir": campaign_dir}
            same = self._item53_segment_manifest(fixture, campaign_dir, job_ids, sequence=9, revision="ar2", preview=new_preview)
            self.assertEqual(self._item45_accepting(case2, same), ["attempt_recovery_segment"])
            with self._segment_patches_started(context, jobs):
                rerun = codex_upgrade._run_capture_attempt(ar2_arguments(new_preview), "candidate")
            self.assertEqual(rerun["status"], "awaiting_receipts", rerun)
            self.assertTrue((ar2_root / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME).is_file())
            self.assertIn(f"{attempt_id}:ar2", timing_ledger.inspect_ledger(ledger_dir)["attempt_recoveries"])

    def test_parent_finalize_lost_summary_is_verified_with_segment_load_strength_and_frozen_jobs(self) -> None:
        """R2 attempt-recovery 变体（2026-09-21 三审 P1）：对账／后继协议对绑定的段摘要用与幂等重派相同强度的
        段加载校验（自摘要、身份、预约绑定、权限收口重放）并要求结果 Job 集合恰等于权威链 J*；
        伪摘要（自摘要重签但预约绑定伪造）、自摘要损坏、缺 Job／多 Job 都拒绝。"""

        import hashlib
        import sys

        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._failed_b0_and_reconciled(root)
            campaign_dir = context["campaign_dir"]
            attempt_root = context["attempt_root"]
            job_ids = context["job_ids"]
            original_roots = {str(item["id"]): list(item["evidence_roots"]) for item in context["attempt"]["results"]}
            source_jobs = [
                Job(
                    job_id=job_id, phase="candidate", suites=("full",), description=f"合成候选 Job {job_id}",
                    steps=_job_steps(job_id, original_roots[job_id][0]), evidence_roots=(original_roots[job_id][0],), covers=(), scenario_ids=("A03",),
                )
                for job_id in job_ids
            ]
            applied = self._apply_transient(fixture, context)
            self.assertEqual(applied["status"], "applied", applied)
            with self._segment_patches_started(context, source_jobs):
                result = codex_upgrade._run_capture_attempt(self._run_arguments(campaign_dir, "ar1"), "candidate")
            self.assertEqual((result["status"], result["execute_jobs"]), ("awaiting_receipts", [job_ids[0]]))
            segment = attempt_root / "recovery" / "ar1"
            summary_path = segment / "attempt-recovery.json"
            original = summary_path.read_bytes()
            relative = summary_path.relative_to(campaign_dir).as_posix()

            def facts_for(raw: bytes) -> dict:
                return {
                    "complete": True, "binding_mismatch": False, "action_id": "vc5-1-ar-run", "operation": "VC-5:capture-candidate",
                    "recovery_revision": "ar1", "binding_path": relative, "binding_sha256": "1" * 64,
                    "output_sha256": hashlib.sha256(raw).hexdigest(), "reasons": [],
                }

            with self._segment_patches_started(context, source_jobs):
                verified = supervisor.verify_attempt_recovery_orphan_output(campaign_dir, facts_for(original))
            self.assertEqual((verified["status"], verified["job_count"], verified["execute_jobs"]), ("awaiting_receipts", 1, sorted(applied["execute_jobs"])))
            self.assertEqual(verified["attempt_recovery_digest"], _read(summary_path)["attempt_recovery_digest"])

            def resign(document: dict) -> bytes:
                unsigned = {k: v for k, v in document.items() if k != "attempt_recovery_digest"}
                signed = dict(unsigned)
                signed["attempt_recovery_digest"] = codex_upgrade._fingerprint(unsigned)
                return (json.dumps(signed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

            base = _read(summary_path)
            forged_cases = {
                # ① 伪摘要：自摘要重签，但预约绑定摘要被换掉（不再指向本段预约）。
                "伪摘要": resign({**base, "reservation": {**base["reservation"], "sha256": "2" * 64}}),
                # ② 自摘要损坏：改结果状态但不重签。
                "自摘要损坏": (json.dumps({**base, "results": [{**base["results"][0], "status": "complete", "duration_seconds": 1.0}]}, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
                # ③ 缺 Job：结果集合为空（自摘要重签）。
                "缺 Job": resign({**base, "results": []}),
                # ④ 多 Job：结果集合多出冻结 J* 之外的 Job（自摘要重签）。
                "多 Job": resign({**base, "results": [*base["results"], {**base["results"][0], "id": f"{job_ids[0]}-extra"}]}),
            }
            try:
                for label, forged in forged_cases.items():
                    summary_path.write_bytes(forged)
                    with self._segment_patches_started(context, source_jobs):
                        with self.assertRaises(supervisor.SupervisorError, msg=label) as raised:
                            supervisor.verify_attempt_recovery_orphan_output(campaign_dir, facts_for(forged))
                    message = str(raised.exception)
                    if label in {"伪摘要", "自摘要损坏"}:
                        self.assertIn("无法按段加载校验重验", message, label)
                    elif label == "缺 Job":
                        self.assertIn("不是无失败 Job 的成功终态", message, label)
                    else:
                        self.assertIn("不等于基线冻结的 J*", message, label)
                    # 浅层 JSON 校验会把这些都判为成功（status／results 看起来合法）——这正是三审指出的缺口。
                    document = json.loads(forged)
                    self.assertEqual(document["status"], "awaiting_receipts")
            finally:
                summary_path.write_bytes(original)
            # 封存时绑定的摘要与当前文件不一致（文件被改写但绑定未变）→ 先于段加载拒绝。
            with self.assertRaisesRegex(supervisor.SupervisorError, "当前字节与封存时不一致"):
                supervisor.verify_attempt_recovery_orphan_output(campaign_dir, {**facts_for(original), "output_sha256": "3" * 64})
            # reconciler 的 parent-finalize-lost 分类走同一函数：合成父 run 目录（单动作 ar-run、绑定指向本段摘要）。
            run_dir = root / "run-finalize-lost"
            run_dir.mkdir(mode=0o700)
            owner_nonce = "8" * 64
            action = {
                "action_id": "vc5-1-ar-run", "operation": "VC-5:capture-candidate", "timeout_seconds": 5.0,
                "command": [sys.executable, "cli.py", "capture-candidate", "run", "--campaign-dir", str(campaign_dir), "--candidate-id", R1, "--attempt-recovery", "ar1", "--acknowledge-live-requests"],
                "item_ids": [job_ids[0]], "output_bindings": [relative],
            }
            inner = {"schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA, "campaign_id": str(fixture["manifest"]["campaign_id"]), "phase": "VC-5", "candidate_id": R1, "actions": [action]}
            manifest_sha256 = supervisor._sha256(supervisor._canonical(inner))
            supervisor._write_json(run_dir / "campaign-run-manifest.json", {"schema_version": inner["schema_version"], "manifest_sha256": manifest_sha256, "manifest": inner}, replace=False)
            state = {"state": "failed", "campaign_id": inner["campaign_id"], "phase": "VC-5", "owner_pid": 1, "owner_nonce": owner_nonce}
            for event_type, status in (("action-started", "running"), ("action-finished", "passed")):
                supervisor._append_event(run_dir, event_type=event_type, operation="VC-5:capture-candidate", owner_pid=1, owner_nonce=owner_nonce, campaign_id=inner["campaign_id"], phase="VC-5", job_id="vc5-1-ar-run", status=status, reason=None)
            supervisor.write_action_output_binding(
                run_dir, campaign_dir=campaign_dir, campaign_id=inner["campaign_id"], phase="VC-5", action_id="vc5-1-ar-run",
                run_manifest_sha256=manifest_sha256, owner_nonce=owner_nonce, output_bindings=[relative],
            )
            facts = supervisor.attempt_recovery_orphan_facts(run_dir, state, inner)
            self.assertTrue(facts["complete"], facts)
            with self._segment_patches_started(context, source_jobs):
                self.assertEqual(supervisor.verify_attempt_recovery_orphan_output(campaign_dir, facts)["execute_jobs"], sorted(applied["execute_jobs"]))

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
