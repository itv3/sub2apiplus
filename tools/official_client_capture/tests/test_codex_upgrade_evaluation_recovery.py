"""改造 5（评估失败局部恢复）M1：evaluator-only 派发入口的附属用例（合成候选证据、父进程调用入口）。

正式的端到端链（真实 checker 修复、真实 CLI compare／accept 动作、R2／E1～E5 崩溃续作、后继协议篡改）
在 ``test_codex_upgrade_evaluation_real_chain.py``（副本受管树 + 子进程）。本文件只保留三类不需要
"真实 evaluator 修复"的入口级用例：

* COMMIT 前 evaluator 摘要漂移 → ``aborted_prepared`` 且同序号重编（以 patch 摘要序列模拟"编译后工具变化"，
  这是对入口核对逻辑的模拟，不是 evaluator 修复正例）；
* 评估基线后继协议的七类伪造拒绝（b1 由 patch 摘要制造，只用于产生被篡改对象；真实 b1 与篡改 COMMIT 的
  负例见真实链用例）；
* 同基线幂等重入（零 checker、重现同一失败）与第二次对账即 ``permanent_stop``。

候选阶段结果与分类收据以合成对象提供（只影响 evaluation-recover 的只读读取，派发链全部真实）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from typing import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import assertion_gate as gate
from tools.official_client_capture import build_assertion_bundle as bundle
from tools.official_client_capture import candidate_rule_assertion as assertion
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.codex_upgrade import Job
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy_module
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import derive_official_observations as derive
from tools.official_client_capture.tests import test_acceptance_end_to_end as e2e
from tools.official_client_capture.tests import test_codex_upgrade
from tools.official_client_capture.tests.test_codex_upgrade_candidate_revision import R1, _ChainMixin, _read

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDER = REPO_ROOT / "tools" / "official_client_capture" / "build_rule_assertion_results.py"
TARGET_VERSION = "0.154.0"


def _candidate_job_ids(fixture: dict) -> list[str]:
    """Campaign 冻结的候选 Job 闭集（合成场景通常只有一个）。"""

    return sorted(str(item["id"]) for item in fixture["manifest"]["jobs"] if item.get("phase") == "candidate")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _EvaluationChainMixin(_ChainMixin):
    """在 VC 链夹具上合成候选 attempt、两侧 bundle、批准画像与断言批次动作计划。"""

    # M2：子类可指定合成候选 Job 的步骤工厂 (job_id, evidence_root) -> steps，让恢复段真实执行并写证据；
    # 默认保持 M1 的占位步骤。
    candidate_job_steps: Callable[[str, str], tuple[dict, ...]] | None = None

    def _prepare_candidate(self, fixture: dict, root: Path) -> dict:
        campaign_dir = Path(str(fixture["campaign_dir"]))
        profile = json.loads(json.dumps(e2e.PROFILE))
        profile["codex_version"] = TARGET_VERSION
        rule_manifest = dict(e2e.RULE_MANIFEST, codex_version=TARGET_VERSION)
        control = campaign_dir / "control" / "eval"
        control.mkdir(mode=0o700, parents=True)
        profile_path = control / "assertion-profile.json"
        profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        rules_path = control / "target-rules.json"
        rules_path.write_text(json.dumps(rule_manifest, ensure_ascii=False), encoding="utf-8")
        for path in (profile_path, rules_path):
            path.chmod(0o600)
        # 证据根必须落在宿主数据根内（provenance 按冻结 CAPTURE_ROOT／宿主数据根映射）：bundle 建在 Campaign 目录下
        # （M2 恢复段用例改放到宿主 runs 根下，见 _candidate_evidence_parent）。
        official = self._bundle(campaign_dir / "official-evidence-eval", surface="codex", candidate_side=False)
        candidate_parent = self._candidate_evidence_parent(fixture, campaign_dir)
        candidate = self._bundle(candidate_parent, surface="other", candidate_side=True)
        job_ids = _candidate_job_ids(fixture)
        self.assertTrue(job_ids)  # type: ignore[attr-defined]
        # 首个候选 Job 的证据根就是候选 bundle 的来源根（目录名 run，与 provenance source_root 对应）。
        evidence_roots = {job_ids[0]: str(candidate_parent / "run")}
        for extra in job_ids[1:]:
            (candidate_parent / f"run-{extra}").mkdir(exist_ok=True)
            evidence_roots[extra] = str(candidate_parent / f"run-{extra}")
        attempt_root, attempt = self._completed_candidate_attempt(fixture, evidence_roots=evidence_roots)
        # b0 候选阶段结果文件真实落盘（stage_sources.capture-candidate 的 reused 引用按其规范路径与摘要绑定）；
        # 其字段级语义由 _load_stage_result 的合成对象提供。
        stage_result = campaign_dir / "candidates" / R1 / "result.json"
        self._write(stage_result, {"status": "complete", "candidate_id": R1, "attempt": {"path": attempt_root.relative_to(campaign_dir).as_posix() + "/attempt.json"}})
        return {
            "campaign_dir": campaign_dir,
            "job_ids": job_ids,
            "profile_path": profile_path,
            "rules_path": rules_path,
            "official": official,
            "candidate": candidate,
            "attempt_root": attempt_root,
            "attempt": attempt,
        }

    def _candidate_evidence_parent(self, fixture: dict, campaign_dir: Path) -> Path:
        """候选 bundle／Job 证据根的父目录；默认在 Campaign 目录下，子类可改到宿主 runs 根。"""

        return campaign_dir / "candidate-evidence"

    def _completed_candidate_attempt(self, fixture: dict, *, evidence_roots: dict[str, str]) -> tuple[Path, dict]:
        """按正式预约与封存合同发布 Job 全部 complete、等待 seal 收据的候选 attempt（同 B0 夹具，候选为 R1）。"""

        campaign_dir = Path(str(fixture["campaign_dir"]))
        manifest = fixture["manifest"]
        identity = {"candidate_purpose": manifest["campaign_purpose"]}
        steps_factory = type(self).candidate_job_steps
        jobs = [
            Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=f"合成候选 Job {job_id}",
                steps=(
                    steps_factory(job_id, evidence_roots[job_id])
                    if steps_factory is not None
                    else ({"argv": ["bash", f"{job_id}.sh"]},)
                ),
                evidence_roots=(evidence_roots[job_id],),
                covers=(),
                scenario_ids=("A03",),
            )
            for job_id in sorted(evidence_roots)
        ]
        attempt_root, reservation = codex_upgrade._reserve_capture_attempt(
            campaign_dir, phase="candidate", candidate_id=R1, identity=identity, jobs=jobs, allow_failed_rerun=True
        )
        results: list[dict] = []
        store = codex_upgrade.incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
        previous: str | None = None
        for job in jobs:
            result = {
                "id": job.job_id, "phase": "candidate", "required": True,
                "execution_sha256": codex_upgrade._job_execution_sha256(job), "status": "complete",
                "description": "合成候选 Job", "duration_seconds": 0.0, "steps": [],
                "evidence_roots": list(job.evidence_roots), "missing_evidence_patterns": [], "empty_evidence_patterns": [],
                "covers": [], "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [],
                "track": "main", "model_id": "gpt-5.5", "expected_use_responses_lite": False, "required_model_receipt": False,
                "model_condition_receipt": None, "model_condition_receipt_failure": None, "disposition": "executed",
            }
            codex_upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
            appended = store.append(
                {
                    "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA, "campaign_id": manifest["campaign_id"],
                    "phase": "candidate", "attempt_id": attempt_root.name, "run_nonce": reservation["run_nonce"],
                    "item_id": job.job_id, "status": "complete", "disposition": "executed",
                    "result_sha256": codex_upgrade.incremental_recovery.digest(result), "result_key": None, "result": result,
                    "source_receipt": None, "previous_checkpoint_sha256": previous,
                }
            )
            previous = str(appended["checkpoint_sha256"])
            results.append(result)
        evidence_root = attempt_root / "evidence"
        logs_root = attempt_root / "logs"
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        logs_root.mkdir(mode=0o700, exist_ok=True)
        closeout = codex_upgrade._close_attempt_evidence_permissions(attempt_root, [evidence_root, logs_root])
        attempt = {
            "campaign_id": manifest["campaign_id"], "phase": "candidate", "candidate_id": R1, "status": "awaiting_receipts",
            "identity": identity, "results": results, "failure_observations": [],
            "evidence_roots": [str(evidence_root), str(logs_root)], "evidence_permission_closeout": closeout,
            "evidence_permission_error": None,
            "environment": {"evidence_root": str(evidence_root), "before_probe": None, "after_probe": {"status": "passed"}, "restoration_report": {"status": "passed"}, "arm64_before_receipt": None, "arm64_after_receipt": None},
            "binary_verification": None, "execution_error": None, "restoration_error": None, "next_gate": "运行 capture manifest finalizer。",
        }
        codex_upgrade._write_capture_attempt(campaign_dir, attempt_root, attempt)
        return attempt_root, json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _bundle(side_dir: Path, *, surface: str, candidate_side: bool) -> Path:
        source_root = side_dir / "run"
        (source_root / "relay").mkdir(parents=True)
        (source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(e2e.H1_STREAM)
        entries = [{"root": "run", "path": "relay/conn001.client_to_upstream.bin", "target": "run/relay/conn001.client_to_upstream.bin"}]
        if candidate_side:
            (source_root / "traces").mkdir()
            record = dict(e2e.INTERNAL_RECORD, data={"surface": surface})
            (source_root / "traces" / "surface.observation.jsonl").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            entries.append({"root": "run", "path": "traces/surface.observation.jsonl", "target": "run/traces/surface.observation.jsonl"})
        plan_path = side_dir / "bundle-plan.json"
        plan_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        bundle_dir = side_dir / gate.BUNDLE_DIR_NAME
        bundle.build_bundle({"run": source_root}, bundle.load_plan(plan_path), bundle_dir)
        derive_plan = side_dir / "derive-plan.json"
        derive_plan.write_text(json.dumps({"entries": [{"source": "run/relay/conn001.client_to_upstream.bin", "parser": "h1_request_stream", "scenario_id": "A03", "kind": "process_trace", "target": "derived/A03/conn001.observation.jsonl", "connection_id": "conn001"}]}), encoding="utf-8")
        derive.derive_observations(bundle_dir, derive.load_derivation_plan(derive_plan))

        def artifact(path: str, kind: str, parser: str) -> dict:
            return {"path": path, "sha256": _sha(bundle_dir / path), "kind": kind, "parser": parser, "scenario_ids": ["A03"], "labels": {"transport": "http"}}

        artifacts_list = [artifact("run/relay/conn001.client_to_upstream.bin", "relay_binary", "opaque_bound_source"), artifact("derived/A03/conn001.observation.jsonl", "process_trace", "observation_jsonl")]
        if candidate_side:
            artifacts_list.append(artifact("run/traces/surface.observation.jsonl", "process_trace", "observation_jsonl"))
        manifest = {"schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION, "codex_version": TARGET_VERSION, "capture_id": f"eval-{side_dir.name}", "status": "complete", "artifacts": artifacts_list}
        (bundle_dir / gate.MANIFEST_FILENAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return bundle_dir

    def _builder_config(self, context: dict, root: Path, *, baseline: int, candidate_bundle: Path, tag: str) -> Path:
        campaign_dir = context["campaign_dir"]
        assertions_root = campaign_dir / "assertions" / R1
        if baseline:
            assertions_root = assertions_root / "revisions" / f"b{baseline}"
        config = {
            "campaign_dir": str(campaign_dir),
            "assertion_profile": str(context["profile_path"]),
            "rule_manifest": str(context["rules_path"]),
            "expected_profile_sha256": _sha(context["profile_path"]),
            "official_evidence_root": str(context["official"]),
            "candidate_evidence_root": str(candidate_bundle),
            "official_capture_manifest": str(context["official"] / gate.MANIFEST_FILENAME),
            "candidate_capture_manifest": str(candidate_bundle / gate.MANIFEST_FILENAME),
            "official_evidence_prefix": "official-run",
            "candidate_evidence_prefix": "candidate-run",
            "target_version": TARGET_VERSION,
            "candidate_id": R1,
            "profile_id": "codex-eval-v1",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": e2e.AUTHORITY,
            "rules": ["SPEC-H1-001", "SPEC-EP-006"],
        }
        path = root / f"builder-config-{tag}.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return path

    def _assertion_plan(self, context: dict, root: Path, *, baseline: int, candidate_bundle: Path, tag: str, reuse_from: Path | None = None, authority: str = "none") -> Path:
        campaign_dir = context["campaign_dir"]
        assertions_root = campaign_dir / "assertions" / R1
        if baseline:
            assertions_root = assertions_root / "revisions" / f"b{baseline}"
        command = [
            sys.executable, str(BUILDER),
            "--config", str(self._builder_config(context, root, baseline=baseline, candidate_bundle=candidate_bundle, tag=tag)),
            "--output", str(assertions_root / "results.json"),
            "--results-dir", str(assertions_root / "machine"),
            "--evaluation-baseline", str(baseline),
            "--reuse-authority", authority,
        ]
        if reuse_from is not None:
            command.extend(["--reuse-from", str(reuse_from)])
        plan = {
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": ["assert-rules"],
            "reuse_item_ids": list(context["job_ids"]),
            "actions": [{"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 300, "command": command, "item_ids": ["assert-rules"]}],
        }
        path = root / f"plans-{tag}" / "vc-5-assert.json"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path.resolve(strict=True)

    def _dispatch_plan(self, fixture: dict, sequence: int, plan: Path):
        return codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", sequence, plan))

    def _stage_patches(self, context: dict, candidate_bundle: Path):
        """evaluation-recover 只读读取候选阶段结果／分类收据／attempt 上下文：以合成对象提供。"""

        campaign_dir = context["campaign_dir"]
        original = codex_upgrade._load_stage_result

        def load_stage_result(campaign_dir_arg: Path, stage: str, candidate_id: str | None = None, **kwargs: object) -> dict:
            canonical = codex_upgrade._STAGE_ALIASES.get(stage, stage)
            if canonical == "capture-candidate":
                return {
                    "status": "complete",
                    "candidate_purpose": "validation_only",
                    "assertion_context": {
                        "evidence_root": str(candidate_bundle),
                        "capture_manifest_path": str(candidate_bundle / gate.MANIFEST_FILENAME),
                        "evidence_prefix": "candidate-run",
                    },
                    "attempt": {"path": context["attempt_root"].relative_to(campaign_dir).as_posix() + "/attempt.json"},
                }
            if canonical == "classify":
                return {
                    "status": "complete",
                    "assertion_profile_manifest": {"path": context["profile_path"].relative_to(campaign_dir).as_posix(), "sha256": _sha(context["profile_path"])},
                    "target_rule_manifest": {"path": context["rules_path"].relative_to(campaign_dir).as_posix(), "sha256": _sha(context["rules_path"])},
                    # 第三批 R5：批准包身份（approval-revision 记录绑定 package digest 与联合摘要）。
                    "package_digest": e2e.AUTHORITY["classification_package_digest"],
                    "joint_manifest_sha256": e2e.AUTHORITY["review_sha256"],
                }
            return original(campaign_dir_arg, stage, candidate_id, **kwargs)

        return (
            mock.patch.object(codex_upgrade, "_load_stage_result", side_effect=load_stage_result),
            mock.patch.object(codex_upgrade, "_capture_stage_attempt_context", return_value=(context["attempt_root"], context["attempt"])),
        )

    @staticmethod
    def _recover_arguments(fixture: dict, action: str, **extra: object) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=Path(str(fixture["campaign_dir"])),
            candidate_id=R1,
            recover_action=action,
            reviewer="boss",
            root_cause_class=extra.get("root_cause_class"),
            fix_commit=extra.get("fix_commit"),
            deployment_receipt=extra.get("deployment_receipt"),
            approve_sha256=extra.get("approve_sha256"),
            reason=extra.get("reason"),
            assertion_profile=extra.get("assertion_profile"),
        )


class EvaluationRecoveryIntegrationTests(_EvaluationChainMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.helper = test_codex_upgrade.CodexUpgradeTest("test_bound_evidence_path_accepts_legacy_attempt_relative_binding")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def _ready_vc4(self, root: Path) -> tuple[dict, dict]:
        fixture = self._fixture(root)
        self._advance_to_vc3(fixture, root)
        self._open(fixture, R1, initial=True)
        result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4")
        self.assertEqual(returncode, 0, result)
        context = self._prepare_candidate(fixture, root)
        return fixture, context

    def test_evaluator_digest_drift_after_compile_aborts_before_commit_and_same_sequence_recompiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            original = policy_module.evaluator_dependency_digests()
            drifted = dict(original, checker_sha256="f6" * 32)
            # 编译时冻结原值，提交前核对时工具已变化（部署新 evaluator）：正式 COMMIT 前中止、序号不占。
            with mock.patch.object(policy_module, "evaluator_dependency_digests", side_effect=[dict(original), drifted, drifted]):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "evaluator-digests 步失败.*同序号") as raised:
                    self._dispatch_plan(fixture, 5, plan_b0)
            self.assertNotIsInstance(raised.exception, codex_upgrade.StagingStopTheLine)
            self.assertFalse((campaign_dir / "control" / "vc" / "commits" / "0005-vc-5.json").exists())
            aborted = [run for run in Path(str(fixture["state_dir"])).glob("run-*") if supervisor._read_state(run)["state"] == "aborted_prepared"]
            self.assertEqual(len(aborted), 1)
            self.assertEqual(supervisor.read_stop_receipt(aborted[0])["reason"], "staging-commit-failed:evaluator-digests")
            abort = _read(next((campaign_dir / "control" / "vc" / "staging" / "0005-vc-5").glob("attempt-*/ABORT")))
            self.assertEqual((abort["stage"], abort["failure_kind"]), ("evaluator-digests", "commit-failed"))
            self.assertNotIn(("stage_started", "VC-5"), {(e[0], e[1][-4:]) for e in self._events(fixture)})
            # 同序号以当前摘要重新编译派发成功（断言批次真实执行到 fail 终态）。
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            self.assertTrue((campaign_dir / "control" / "vc" / "commits" / "0005-vc-5.json").is_file())
            self.assertEqual(_read(campaign_dir / "control" / "vc" / "batches" / "0005-vc-5.json")["evaluator_digests"], original)

    def test_evaluation_recover_paused_decision_does_not_stop_and_resumes(self) -> None:
        """二次判定为预算暂停时不写停线与终态；批准延期后（以取消暂停判定模拟）同一批准摘要重跑即完成授权。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            run_b0 = Path(str(result["campaign_run"]["run_dir"]))
            self.assertEqual(reconciler.reconcile_supervisor_run(run_b0, campaign_dir)["status"], "recoverable")
            patches = self._stage_patches(context, context["candidate"])
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            real_decide = reconciler._decide

            def paused_decide(**kwargs: object) -> dict:
                decision = real_decide(**kwargs)
                return {**decision, "decision": reconciler.DECISION_PAUSED, "terminal_reason": None}

            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f6" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))

                def apply() -> dict:
                    return codex_upgrade.evaluation_recover(self._recover_arguments(
                        fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview["review_sha256"],
                        fix_commit="a" * 40, deployment_receipt=Path(str(fixture["deployment"])),
                    ))

                with mock.patch.object(reconciler, "_decide", side_effect=paused_decide):
                    paused = apply()
                self.assertEqual(paused["status"], "paused")
                self.assertIn("deadline-extend", paused["next_command"])
                head = project_ledger.replay_head(Path(str(fixture["ledger"])))
                self.assertEqual(head["terminal_campaigns"], {})
                self.assertNotIn(
                    timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], {"stopped", "stop_required"}
                )
                applied = apply()
            self.assertEqual(applied["status"], "applied")

    def test_successor_protocol_rejects_forged_baseline_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            run_b0 = Path(str(result["campaign_run"]["run_dir"]))
            self.assertEqual(reconciler.reconcile_supervisor_run(run_b0, campaign_dir)["status"], "recoverable")
            patches = self._stage_patches(context, context["candidate"])
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f6" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
                applied = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview["review_sha256"], fix_commit="a" * 40, deployment_receipt=Path(str(fixture["deployment"]))))
            self.assertEqual(applied["status"], "applied")
            prior_state = supervisor._read_state(run_b0)
            prior_manifest = _read(run_b0 / "campaign-run-manifest.json")["manifest"]
            successor = dict(prior_manifest, batch_sequence=6, batch_id="vc-5-0006", evaluation_baseline=1, baseline_commit_sha256=applied["commit_sha256"])

            def check(manifest: dict) -> bool:
                return supervisor._validate_evaluation_baseline_successor(prior_state, prior_manifest, run_b0, manifest, campaign_dir=campaign_dir)

            self.assertTrue(check(successor))
            # 不带基线字段：入口不成立（交给既有逐字重派协议判定）。
            self.assertFalse(check(dict(successor, evaluation_baseline=None, baseline_commit_sha256=None)))
            # 修好接着跑第 9 项（B3-11）：b1 失败批次的重派按该基线 recovery.json 授权的四项核对评估器摘要
            # （真实 apply 冻结的 current_evaluator_digests），不与上一次批次逐字比较；不等于授权四项或跨基线即漂移。
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            authorized = codex_upgrade._authorized_evaluator_digests(campaign_dir, manifest, R1, 1)
            self.assertEqual(authorized["checker_sha256"], "f6" * 32)
            prior_b1 = dict(successor, evaluator_digests=dict(authorized, compare_reader_sha256="0" * 64))
            successor_b1 = dict(successor, evaluator_digests=dict(authorized))
            self.assertFalse(supervisor._redispatch_evaluator_digests_drifted(prior_b1, successor_b1, campaign_dir=campaign_dir))
            self.assertTrue(supervisor._redispatch_evaluator_digests_drifted(
                prior_b1, dict(successor_b1, evaluator_digests=dict(authorized, checker_sha256="0" * 64)), campaign_dir=campaign_dir
            ))
            self.assertTrue(supervisor._redispatch_evaluator_digests_drifted(
                prior_b1, dict(successor_b1, evaluation_baseline=2), campaign_dir=campaign_dir
            ))
            # 修好接着跑第 9 项：同候选同 revision 同基线（b≥1 评估批次失败后的重派）不是开新基线，入口不成立，
            # 交给逐字重派等协议；此前在这里失败关闭，b≥1 评估批次连环境失败都无法重派。
            self.assertFalse(
                supervisor._validate_evaluation_baseline_successor(
                    prior_state, dict(prior_manifest, evaluation_baseline=1), run_b0, successor, campaign_dir=campaign_dir
                )
            )
            # 七类伪造各拒。
            with self.assertRaisesRegex(supervisor.SupervisorError, "未由账本以同一 COMMIT 摘要激活"):
                check(dict(successor, baseline_commit_sha256="0" * 64))
            with self.assertRaisesRegex(supervisor.SupervisorError, "未由账本以同一 COMMIT 摘要激活"):
                check(dict(successor, evaluation_baseline=2))
            with self.assertRaisesRegex(supervisor.SupervisorError, "同候选同 revision"):
                check(dict(successor, candidate_revision=2))
            baseline_dir = campaign_dir / "candidates" / R1 / "revisions" / "b1"
            for name, pattern in (("AUTHORIZATION", "无法重放|摘要链"), ("recovery.json", "无法重放|摘要链"), ("diagnosis.json", "无法重放|摘要链")):
                path = baseline_dir / name
                original_bytes = path.read_bytes()
                payload = json.loads(original_bytes)
                key = next(k for k in payload if k.endswith("_sha256") and k not in {"recovery_sha256", "authorization_sha256", "receipt_sha256"})
                payload[key] = "0" * 64
                path.chmod(0o600)
                path.write_text(json.dumps(payload), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(supervisor.SupervisorError, pattern):
                        check(successor)
                finally:
                    path.write_bytes(original_bytes)
            # 前序 run 三摘要篡改（诊断绑定的失败 run 不是本前序）：改写 stop-receipt 字节。
            stop_path = run_b0 / "stop-receipt.json"
            original_stop = stop_path.read_bytes()
            stop_payload = json.loads(original_stop)
            stop_payload["detected_at_utc"] = "2000-01-01T00:00:00Z"
            stop_payload["receipt_sha256"] = supervisor._sha256(supervisor._canonical({k: v for k, v in stop_payload.items() if k != "receipt_sha256"}))
            supervisor._write_json(stop_path, stop_payload, replace=True)
            try:
                with self.assertRaisesRegex(supervisor.SupervisorError, "不是本前序批次"):
                    check(successor)
            finally:
                supervisor._write_json(stop_path, json.loads(original_stop), replace=True)
            self.assertTrue(check(successor))
            # 跳号：b2 未经完整 PREPARED＋ABANDON 链即拒；补链后仍要求后继等于账本当前基线。
            with self.assertRaisesRegex(supervisor.SupervisorError, "未由账本以同一 COMMIT 摘要激活"):
                check(dict(successor, evaluation_baseline=3, baseline_commit_sha256=applied["commit_sha256"]))
            self.assertTrue(check(successor))

    def test_failed_evaluation_batch_successor_bridged_by_non_failure_baselines(self) -> None:
        """修好接着跑（E5 VC-5 批次 26 实测）：b0 评估批次失败并对账后，按指南 §4.5.8 先 reevaluate 开 tool-evolution
        基线 b1、再 approval-revision 开 b2；评估基线后继协议沿 previous_baseline 链放行 b2（或只开到 b1 时的 b1）
        批次——此前 b1 已 COMMIT 被当成跳号、b2 没有失败诊断，两处都失败关闭。负例：链上制品或触发记录被篡改、
        承接基线在失败对账之前已激活、账本缺失败对账事件各拒。失败类基线的原路径见
        test_successor_protocol_rejects_forged_baseline_bindings（本修复不改它）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
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
            # 修订画像只改一条 check 的 description（第一类判据）。
            document = _read(context["profile_path"])
            document["rules"][0]["checks"][0]["description"] = "方法为 POST（批准修订描述）"
            revised = root / "profile-revised.json"
            revised.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            revised.chmod(0o600)
            # 工具演进：评估器摘要变化（以 patch 摘要模拟）→ reevaluate 开 b1；修订基线要求口径等于 b1 授权，同一 patch 下开 b2。
            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f6" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
                b1 = codex_upgrade.evaluation_recover(self._recover_arguments(
                    fixture, "reevaluate", approve_sha256=preview["review_sha256"], fix_commit="a" * 40,
                    deployment_receipt=Path(str(fixture["deployment"])),
                ))
                preview2 = codex_upgrade.evaluation_recover(
                    self._recover_arguments(fixture, "approval-revision", assertion_profile=revised)
                )
                b2 = codex_upgrade.evaluation_recover(self._recover_arguments(
                    fixture, "approval-revision", assertion_profile=revised, approve_sha256=preview2["review_sha256"]
                ))
            self.assertEqual(
                (b1["kind"], b1["evaluation_baseline"], b2["kind"], b2["evaluation_baseline"]),
                ("tool-evolution", 1, "approval-revision", 2),
            )
            commit_b1 = codex_upgrade._read_evaluation_baseline_commit(campaign_dir, R1, 1)["commit_sha256"]
            commit_b2 = codex_upgrade._read_evaluation_baseline_commit(campaign_dir, R1, 2)["commit_sha256"]
            prior_state = supervisor._read_state(run_b0)
            prior_manifest = _read(run_b0 / "campaign-run-manifest.json")["manifest"]
            successor = dict(
                prior_manifest, batch_sequence=6, batch_id="vc-5-0006", evaluation_baseline=2, baseline_commit_sha256=commit_b2,
            )

            def check(manifest: dict) -> bool:
                return supervisor._validate_evaluation_baseline_successor(
                    prior_state, prior_manifest, run_b0, manifest, campaign_dir=campaign_dir
                )

            self.assertTrue(check(successor))
            # 只开到 b1 就派发（直接接 tool-evolution 基线）同样放行。
            self.assertTrue(check(dict(successor, evaluation_baseline=1, baseline_commit_sha256=commit_b1)))
            # 链上制品与触发记录篡改各拒：后继 b2 自身与回溯经过的 b1 各取几份，改一个非自摘要的 *_sha256 字段后复原。
            self_digests = {
                "COMMIT": {"commit_sha256"},
                "AUTHORIZATION": {"authorization_sha256"},
                "recovery.json": {"recovery_sha256"},
                "approval-revision.json": {"receipt_sha256", "review_sha256"},
                "reevaluation.json": {"receipt_sha256", "review_sha256"},
            }
            for number, name in (
                (2, "COMMIT"), (2, "AUTHORIZATION"), (2, "approval-revision.json"),
                (1, "COMMIT"), (1, "recovery.json"), (1, "reevaluation.json"),
            ):
                path = campaign_dir / "candidates" / R1 / "revisions" / f"b{number}" / name
                original_bytes = path.read_bytes()
                payload = json.loads(original_bytes)
                key = next(k for k in sorted(payload) if k.endswith("_sha256") and k not in self_digests[name])
                payload[key] = "0" * 64
                path.chmod(0o600)
                path.write_text(json.dumps(payload), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(supervisor.SupervisorError, "无法重放|摘要链|不一致", msg=f"b{number}/{name}:{key}"):
                        check(successor)
                finally:
                    path.write_bytes(original_bytes)
            self.assertTrue(check(successor))
            # 时间顺序与对账事件：直接以重排／删减后的账本事件重放承接链。
            ledger_dir = Path(str(fixture["timing_ledger"])).resolve()
            raw = timing_ledger._load_events(ledger_dir)
            passed_id = f"reconcile-run-passed-{run_b0.name}"
            passed = [item for item in raw if item[0].get("event_id") == passed_id]
            self.assertEqual(len(passed), 1)
            others = [item for item in raw if item[0].get("event_id") != passed_id]

            def bridge(events: list) -> bool:
                return supervisor._validate_non_failure_baseline_bridge(
                    run_b0, prior_manifest, campaign_dir=campaign_dir.resolve(), campaign_id=str(prior_manifest["campaign_id"]),
                    candidate_id=R1, revision=prior_manifest["candidate_revision"], prior_baseline=0, successor_baseline=2,
                    raw_events=events, label="评估基线后继",
                )

            self.assertTrue(bridge(raw))
            # 承接基线在失败父 run 对账之前已激活（对账事件挪到最后）：不是对本次失败的承接。
            with self.assertRaisesRegex(supervisor.SupervisorError, "对账之前已激活"):
                bridge(others + passed)
            # 账本没有失败父 run 的 receipt_passed 事件：拒绝。
            with self.assertRaisesRegex(supervisor.SupervisorError, "receipt_passed 事件不存在或不唯一"):
                bridge(others)

    def test_same_baseline_reentry_reproduces_failure_with_zero_checker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            run_b0 = Path(str(result["campaign_run"]["run_dir"]))
            reconciled = reconciler.reconcile_supervisor_run(run_b0, campaign_dir)
            self.assertEqual(reconciled["status"], "recoverable")
            # M2-G0 修正：无枚举观测的动作失败，根因 failed_step 取失败动作的 operation（VC-5:assert-rules），
            # 不再取父 run 最后事件（supervisor-stop）——否则同批次内另一动作（accept）失败会被编成同一
            # 根因、逐字重派后被误判为同根因第二次而停线。
            self.assertEqual(reconciled["root_cause"]["stable_error_code"], "supervisor-run.interrupted")
            self.assertEqual(reconciled["root_cause"]["failed_step"], "VC-5-assert-rules")
            receipt_run = _read(campaign_dir / reconciled["reconciliation_receipt"]["path"])["run"]
            self.assertEqual(receipt_run["action_diagnostic"]["operation"], "VC-5:assert-rules")
            machine = campaign_dir / "assertions" / R1 / "machine"
            stamps = {p: p.stat().st_mtime_ns for p in machine.rglob("*.json")}
            checkpoints_before = sorted(p.name for p in (campaign_dir / "assertions" / R1 / "checkpoints").iterdir())
            # 同基线逐字重派：既有 checkpoint／index 视为完成，零 checker，重现同一 failed_step。
            plan_again = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result2, returncode2 = self._dispatch_plan(fixture, 6, plan_again)
            self.assertEqual(returncode2, 1, result2)
            run_b0_again = Path(str(result2["campaign_run"]["run_dir"]))
            self.assertEqual(supervisor.read_stop_receipt(run_b0_again)["reason"], "action-failed:assert-rules")
            self.assertEqual({p: p.stat().st_mtime_ns for p in machine.rglob("*.json")}, stamps)
            self.assertEqual(sorted(p.name for p in (campaign_dir / "assertions" / R1 / "checkpoints").iterdir()), checkpoints_before)
            index = artifacts.validate_evaluation_run(_read(campaign_dir / "assertions" / R1 / "evaluation-run.json"))
            self.assertEqual({row["rule"]: row["status"] for row in index["rules"]}, {"SPEC-EP-006": "fail", "SPEC-H1-001": "pass"})
            # 再对账即同根因第二次（同一动作 VC-5:assert-rules 再次失败）→ 第三批 B3-9：同根因达上限只暂停
            # （暂停种类 root_cause_repair，登记修复证据后重新对账继续），不再写终态。
            second = reconciler.reconcile_supervisor_run(run_b0_again, campaign_dir)
            self.assertEqual(second["status"], "paused")
            self.assertEqual(second["root_cause"]["root_cause_id"], reconciled["root_cause"]["root_cause_id"])
            self.assertEqual(second["decision"]["pause_kinds"], ["root_cause_repair"])
            self.assertNotIn("permanent_stop", second)

    def test_reevaluate_opens_tool_evolution_baseline_without_failed_run(self) -> None:
        """第三批 B3-3：b0 已有评估产出后修评估器（checker 摘要变化）——reevaluate 不依赖失败父 run：预览给出触发事实，
        落盘后 b1 为 tool-evolution 基线（候选证据 reused、compare／断言／accept 全部 local 全量重评），账本切换当前基线、
        总账根因计数不变、b1 授权口径＝当前工具；无产出、无变化、缺修复提交各拒。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            for patcher in self._stage_patches(context, context["candidate"]):
                patcher.start()
                self.addCleanup(patcher.stop)
            # VC-5 尚未开始：账本位置不允许重评。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只能在 VC-5 进行中"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
            timing_ledger.append_event(
                Path(str(fixture["timing_ledger"])), event_id="s5-reevaluate", phase="VC-5", event_type="stage_started",
                next_action="x",
            )
            # VC-5 进行中但还没有评估产出：不需要重评。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "还没有任何评估产出"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            run_b0 = Path(str(result["campaign_run"]["run_dir"]))
            self.assertEqual(reconciler.reconcile_supervisor_run(run_b0, campaign_dir)["status"], "recoverable")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有需要重评的理由"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
            head_before = project_ledger.replay_head(Path(str(fixture["ledger"])))
            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f6" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
                self.assertEqual(
                    (preview["status"], preview["kind"], preview["ledger_event_type"]),
                    ("preview", "tool-evolution", "evaluation_baseline"),
                )
                self.assertEqual(preview["trigger"]["evaluator_changed_fields"], ["checker_sha256"])
                self.assertFalse(preview["trigger"]["evidence_changed"])
                self.assertTrue(preview["trigger"]["evaluation_outputs"])
                self.assertEqual(preview["reevaluation"]["from_baseline"], 0)
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须以 --fix-commit"):
                    codex_upgrade.evaluation_recover(
                        self._recover_arguments(fixture, "reevaluate", approve_sha256=preview["review_sha256"])
                    )
                applied = codex_upgrade.evaluation_recover(self._recover_arguments(
                    fixture, "reevaluate", approve_sha256=preview["review_sha256"], fix_commit="a" * 40,
                    deployment_receipt=Path(str(fixture["deployment"])),
                ))
                self.assertEqual(
                    (applied["status"], applied["kind"], applied["evaluation_baseline"], applied["reuse_rules"]),
                    ("applied", "tool-evolution", 1, []),
                )
                self.assertEqual(applied["ledger_event"], {**applied["ledger_event"], "event_type": "evaluation_baseline", "appended": True})
                manifest = codex_upgrade._require_formal_campaign(campaign_dir)
                self.assertEqual(codex_upgrade._authorized_evaluator_digests(campaign_dir, manifest, R1, 1)["checker_sha256"], "f6" * 32)
                # b1 生效后再 reevaluate：b1 还没有评估产出，拒绝（不会无限开基线）。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "还没有任何评估产出"):
                    codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
            summary = timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))
            self.assertEqual(summary["current_evaluation_baseline"]["baseline_kind"], "tool-evolution")
            self.assertEqual((summary["status"], summary["active_phase"]), ("active", "VC-5"))
            head = project_ledger.replay_head(Path(str(fixture["ledger"])))
            self.assertEqual(head["root_cause_counts"], head_before["root_cause_counts"])
            self.assertFalse(head["blocked"])
            commit = codex_upgrade._read_evaluation_baseline_commit(campaign_dir, R1, 1)
            self.assertEqual(
                {stage: source["source"] for stage, source in commit["stage_sources"].items()},
                {"capture-candidate": "reused", "compare": "local", "assertions": "local", "accept": "local"},
            )
            recovery = codex_upgrade._load_evaluation_baseline_recovery(campaign_dir, R1, 1)
            self.assertEqual(
                (recovery["kind"], recovery["failure_source"], recovery["root_cause_class"], recovery["reuse_authority"]),
                ("tool-evolution", "tool-evolution", "tool-evolution", "none"),
            )
            self.assertEqual(recovery["diagnosis"]["path"], f"candidates/{R1}/revisions/b1/reevaluation.json")
            fact = artifacts.validate_evaluation_reevaluation(
                _read(campaign_dir / "candidates" / R1 / "revisions" / "b1" / "reevaluation.json")
            )
            self.assertEqual(fact["evaluator_changed_fields"], ["checker_sha256"])

    def test_reopened_vc5_lands_checkpoint_and_completion_in_reopen_directory(self) -> None:
        """第三批 R5 前置：VC-5 在同一 revision 完成后经 reevaluate 重开——再次派发 VC-5 不被"已有 checkpoint"拒绝，
        checkpoint／完成收据落到 reopen-b<K>/、首次完成的制品原样保留，VC-6 的前序绑定指向重开后的 checkpoint。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            ledger_dir = Path(str(fixture["timing_ledger"]))
            for patcher in self._stage_patches(context, context["candidate"]):
                patcher.start()
                self.addCleanup(patcher.stop)
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            attempt_id = context["attempt_root"].name
            completion = dict(
                kind="vc5_completion", candidate_id=R1, attempt_id=attempt_id,
                assertions={"acceptance_passed": True, "vc5_pending_count": 0, "canonical_handoff": "not_required"},
            )
            # b0 评估产出（reevaluate 的前提）：断言目录内任何文件即可，不经失败批次（失败后账本只允许逐字重派原批次）。
            b0_outputs = campaign_dir / "assertions" / R1 / "machine"
            b0_outputs.mkdir(parents=True, mode=0o700)
            (b0_outputs / "SPEC-H1-001.json").write_text("{}\n", encoding="utf-8")
            (b0_outputs / "SPEC-H1-001.json").chmod(0o600)
            # 首次完成 VC-5：合成批次封存原路径 checkpoint、账本登记完成；完成收据写在原路径。
            result, returncode = self._dispatch(fixture, root, "VC-5", 5, tag="vc5-first")
            self.assertEqual(returncode, 0, result)
            first_checkpoint = campaign_dir / "control" / "vc" / "vc-5-checkpoint.json"
            self.assertTrue(first_checkpoint.is_file())
            first_checkpoint_sha = _sha(first_checkpoint)
            self.assertIn("VC-5", timing_ledger.phase_ledger_state(ledger_dir)["completed_phases"])
            stage_receipt = campaign_dir / "control" / "vc-chain" / "vc-5-stage-result.json"
            first_receipt_path, _ = codex_upgrade._write_vc_completion_receipt(
                campaign_dir, manifest, evidence_paths={"acceptance_fact": stage_receipt}, **completion
            )
            self.assertEqual(first_receipt_path, campaign_dir / "control" / "vc" / "receipts" / R1 / "vc5-completion.json")
            # 重开：评估器摘要变化 → reevaluate 落盘 b1（evaluation_reopened），账本记下重开基线。
            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f7" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
                self.assertEqual(preview["ledger_event_type"], "evaluation_reopened")
                applied = codex_upgrade.evaluation_recover(self._recover_arguments(
                    fixture, "reevaluate", approve_sha256=preview["review_sha256"], fix_commit="a" * 40,
                    deployment_receipt=Path(str(fixture["deployment"])),
                ))
                self.assertEqual(
                    (applied["status"], applied["evaluation_baseline"], applied["ledger_event"]["event_type"]),
                    ("applied", 1, "evaluation_reopened"),
                )
                summary = timing_ledger.inspect_ledger(ledger_dir)
                self.assertEqual((summary["active_phase"], summary["vc5_reopened_baseline"]), ("VC-5", 1))
                reopen_dir = campaign_dir / "control" / "vc" / "reopen-b1"
                self.assertEqual(codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-5", revision=1), reopen_dir / "vc-5-checkpoint.json")
                self.assertEqual(
                    codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-4", revision=1),
                    campaign_dir / "control" / "vc" / "vc-4-checkpoint.json",
                )
                # 重开后再次派发 VC-5：不再被"已有 checkpoint，禁止再编译"拒绝，checkpoint 落到 reopen-b1/，原 checkpoint 不动。
                result, returncode = self._dispatch(fixture, root, "VC-5", 6, tag="vc5-reopened")
                self.assertEqual(returncode, 0, result)
                self.assertTrue((reopen_dir / "vc-5-checkpoint.json").is_file())
                self.assertEqual(_sha(first_checkpoint), first_checkpoint_sha)
                self.assertIn("VC-5", timing_ledger.phase_ledger_state(ledger_dir)["completed_phases"])
                # 完成收据同样落到 reopen-b1/，与首次收据并存。
                second_receipt_path, _ = codex_upgrade._write_vc_completion_receipt(
                    campaign_dir, manifest, evidence_paths={"acceptance_fact": stage_receipt}, **completion
                )
                self.assertEqual(
                    second_receipt_path,
                    campaign_dir / "control" / "vc" / "receipts" / R1 / "reopen-b1" / "vc5-completion.json",
                )
                self.assertTrue(first_receipt_path.is_file())
                # VC-6 的前序绑定指向重开后的 VC-5 checkpoint。
                result, returncode = self._dispatch(fixture, root, "VC-6", 7, tag="vc6")
                self.assertEqual(returncode, 0, result)
                vc6 = _read(campaign_dir / "control" / "vc" / "vc-6-checkpoint.json")
                self.assertEqual(vc6["predecessor_checkpoint"]["path"], "control/vc/reopen-b1/vc-5-checkpoint.json")

    def test_approval_revision_opens_baseline_with_revised_profile_and_read_side_projection(self) -> None:
        """第三批 R5：批准画像 selector 修正在原 Campaign 内以 approval-revision 基线承接——账本位置、第一类判据（规则增删／
        check 集合／场景／版本／规则其它字段／无差异各拒，工具已变先 reevaluate）、预览批准摘要；apply 落盘修订画像／批准记录／
        五步制品，账本切换基线（kind approval-revision）、总账根因计数不变；读侧投影与 accept 机器命令用修订画像；
        b1 再修订 → b2 沿链取最近。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            ledger_dir = Path(str(fixture["timing_ledger"]))
            for patcher in self._stage_patches(context, context["candidate"]):
                patcher.start()
                self.addCleanup(patcher.stop)
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            profile = _read(context["profile_path"])

            def revised(name: str, mutate) -> Path:
                document = json.loads(json.dumps(profile))
                mutate(document)
                path = root / f"profile-{name}.json"
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                path.chmod(0o600)
                return path

            def selector_fix(document: dict) -> None:
                check = document["rules"][0]["checks"][0]
                check["select"]["where"] = {"data.method": {"operator": "present"}}
                check["description"] = "方法为 POST（修正 selector）"

            def approval(profile_path: Path, **extra: object) -> argparse.Namespace:
                return self._recover_arguments(fixture, "approval-revision", assertion_profile=profile_path, **extra)

            good = revised("good", selector_fix)
            # VC-5 尚未开始：账本位置不允许修订。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只能在 VC-5 进行中"):
                codex_upgrade.evaluation_recover(approval(good))
            timing_ledger.append_event(ledger_dir, event_id="s5-approval", phase="VC-5", event_type="stage_started", next_action="x")
            # 第二／三类与规则其它字段变化各拒；与生效画像无差异也拒。
            for name, mutate, message in (
                ("drop-rule", lambda d: d["rules"].pop(), "规则 id 集"),
                ("rename-check", lambda d: d["rules"][0]["checks"][0].__setitem__("id", "method-post-2"), "acceptance_contract"),
                ("scenario", lambda d: d["scenarios"][0].__setitem__("description", "改了场景"), "scenarios"),
                ("version", lambda d: d.__setitem__("codex_version", "0.999.0"), "codex_version"),
                ("rule-field", lambda d: d["rules"][0].__setitem__("note", "x"), r"SPEC-H1-001\.note"),
            ):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, message, msg=name):
                    codex_upgrade.evaluation_recover(approval(revised(name, mutate)))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有差异|逐字节相同"):
                codex_upgrade.evaluation_recover(approval(revised("same", lambda d: None)))
            # 工具已变（评估器摘要≠当前基线授权口径）：先 reevaluate，不混在修订基线里。
            drifted = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f8" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=drifted):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "先以 evaluation-recover reevaluate"):
                    codex_upgrade.evaluation_recover(approval(good))
            head_before = project_ledger.replay_head(Path(str(fixture["ledger"])))
            preview = codex_upgrade.evaluation_recover(approval(good, reason="修正 where"))
            self.assertEqual(
                (preview["status"], preview["kind"], preview["ledger_event_type"], preview["approval_revision"]),
                ("preview", "approval-revision", "evaluation_baseline", 1),
            )
            self.assertEqual(preview["changed_rule_ids"], ["SPEC-H1-001"])
            self.assertEqual(preview["changed_check_ids"], ["SPEC-H1-001:method-post"])
            self.assertEqual(preview["previous_profile"]["sha256"], _sha(context["profile_path"]))
            revised_binding = {"path": f"candidates/{R1}/revisions/b1/assertion-profile.json", "sha256": _sha(good)}
            self.assertEqual(preview["revised_profile"], revised_binding)
            self.assertFalse((campaign_dir / "candidates" / R1 / "revisions" / "b1").exists())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "请重新预览"):
                codex_upgrade.evaluation_recover(approval(good, approve_sha256="0" * 64))
            applied = codex_upgrade.evaluation_recover(approval(good, reason="修正 where", approve_sha256=preview["review_sha256"]))
            self.assertEqual(
                (applied["status"], applied["kind"], applied["evaluation_baseline"], applied["approval_revision"]),
                ("applied", "approval-revision", 1, 1),
            )
            self.assertEqual(applied["ledger_event"], {**applied["ledger_event"], "event_type": "evaluation_baseline", "appended": True})
            baseline_dir = campaign_dir / "candidates" / R1 / "revisions" / "b1"
            self.assertEqual(_sha(baseline_dir / "assertion-profile.json"), _sha(good))
            record = artifacts.validate_approval_revision(_read(baseline_dir / "approval-revision.json"))
            self.assertEqual(
                (record["approval_revision"], record["from_baseline"], record["changed_rule_ids"], record["revised_profile"]),
                (1, 0, ["SPEC-H1-001"], revised_binding),
            )
            recovery = codex_upgrade._load_evaluation_baseline_recovery(campaign_dir, R1, 1)
            self.assertEqual(
                (recovery["kind"], recovery["failure_source"], recovery["root_cause_class"], recovery["fix_commit"], recovery["deployment_receipt"]),
                ("approval-revision", "approval-revision", "approval-revision", None, None),
            )
            self.assertEqual(recovery["diagnosis"]["path"], f"candidates/{R1}/revisions/b1/approval-revision.json")
            commit = codex_upgrade._read_evaluation_baseline_commit(campaign_dir, R1, 1)
            self.assertEqual(
                {stage: source["source"] for stage, source in commit["stage_sources"].items()},
                {"capture-candidate": "reused", "compare": "local", "assertions": "local", "accept": "local"},
            )
            summary = timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(
                (summary["status"], summary["active_phase"], summary["current_evaluation_baseline"]["baseline_kind"],
                 summary["current_evaluation_baseline"]["evaluation_baseline"]),
                ("active", "VC-5", "approval-revision", 1),
            )
            head = project_ledger.replay_head(Path(str(fixture["ledger"])))
            self.assertEqual(head["root_cause_counts"], head_before["root_cause_counts"])
            self.assertFalse(head["blocked"])
            # 读侧投影：生效画像 = b1 修订画像；accept 重建的 checker 命令引用修订画像路径与摘要，不再引用批准画像。
            effective = codex_upgrade._effective_assertion_profile(campaign_dir, R1, 1)
            self.assertEqual((effective["path"], effective["sha256"], effective["approval_revision"]), (revised_binding["path"], _sha(good), 1))
            classification = codex_upgrade._load_stage_result(campaign_dir, "classify")
            view = codex_upgrade._effective_classification_view(campaign_dir, R1, classification)
            self.assertEqual(view["assertion_profile_manifest"], revised_binding)
            self.assertEqual(view["target_rule_manifest"], classification["target_rule_manifest"])
            command = codex_upgrade._campaign_machine_command(
                campaign_dir, manifest, view, codex_upgrade._load_stage_result(campaign_dir, "capture-candidate", R1),
                rule="SPEC-H1-001", output=root / "out.json", side="candidate",
            )
            self.assertIn(str((baseline_dir / "assertion-profile.json").resolve()), command)
            self.assertIn(_sha(good), command)
            self.assertNotIn(str(context["profile_path"].resolve()), command)
            self.assertNotIn(_sha(context["profile_path"]), command)
            # b1 生效后再提交同一画像：与生效画像相同，拒绝（不会无限开基线）。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有差异|逐字节相同"):
                codex_upgrade.evaluation_recover(approval(good))
            # b1 再修订另一条规则的 assertion → b2（第 2 次修订）：沿链取最近的修订画像，历史基线各取各的。

            def second_fix(document: dict) -> None:
                selector_fix(document)
                document["rules"][1]["checks"][0]["assertion"]["value"] = "codex-cli"

            good2 = revised("good2", second_fix)
            preview2 = codex_upgrade.evaluation_recover(approval(good2))
            self.assertEqual(
                (preview2["approval_revision"], preview2["previous_profile"]["sha256"], preview2["changed_rule_ids"]),
                (2, _sha(good), ["SPEC-EP-006"]),
            )
            applied2 = codex_upgrade.evaluation_recover(approval(good2, approve_sha256=preview2["review_sha256"]))
            self.assertEqual((applied2["evaluation_baseline"], applied2["approval_revision"]), (2, 2))
            self.assertEqual(codex_upgrade._effective_assertion_profile(campaign_dir, R1, 2)["sha256"], _sha(good2))
            self.assertEqual(codex_upgrade._effective_assertion_profile(campaign_dir, R1, 1)["sha256"], _sha(good))
            self.assertIsNone(codex_upgrade._effective_assertion_profile(campaign_dir, R1, 0))

    def test_apply_paths_refuse_unregistered_tool_evolution_before_writing(self) -> None:
        """第三批 B3-5（第 7 项⑥）：evaluation-recover apply 与 reevaluate 落盘前做零写入预检——未登记的工具变化只提示
        先登记演进，不写任何基线文件、不写终态、账本不变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]
            for patcher in self._stage_patches(context, context["candidate"]):
                patcher.start()
                self.addCleanup(patcher.stop)
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            self.assertEqual(
                reconciler.reconcile_supervisor_run(Path(str(result["campaign_run"]["run_dir"])), campaign_dir)["status"],
                "recoverable",
            )
            fixed = dict(policy_module.evaluator_dependency_digests(), checker_sha256="f6" * 32)
            refusal = mock.Mock(side_effect=codex_upgrade.ToolEvolutionRequired("未登记的工具演进：先执行 tool-evolution"))
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed):
                preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
                reevaluate = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "reevaluate"))
                with mock.patch.object(codex_upgrade, "_require_tool_evolution_registered", refusal):
                    with self.assertRaisesRegex(codex_upgrade.ToolEvolutionRequired, "未登记的工具演进"):
                        codex_upgrade.evaluation_recover(self._recover_arguments(
                            fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview["review_sha256"],
                            fix_commit="a" * 40, deployment_receipt=Path(str(fixture["deployment"])),
                        ))
                    with self.assertRaisesRegex(codex_upgrade.ToolEvolutionRequired, "未登记的工具演进"):
                        codex_upgrade.evaluation_recover(self._recover_arguments(
                            fixture, "reevaluate", approve_sha256=reevaluate["review_sha256"],
                            fix_commit="a" * 40, deployment_receipt=Path(str(fixture["deployment"])),
                        ))
            self.assertEqual(refusal.call_count, 2)
            self.assertFalse((campaign_dir / "candidates" / R1 / "revisions" / "b1").exists())
            self.assertEqual(project_ledger.replay_head(Path(str(fixture["ledger"])))["terminal_campaigns"], {})
            self.assertEqual(timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))["status"], "active")


if __name__ == "__main__":
    unittest.main()
