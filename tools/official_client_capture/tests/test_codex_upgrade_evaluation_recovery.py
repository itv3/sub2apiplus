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
            # 再对账即同根因第二次（同一动作 VC-5:assert-rules 再次失败）→ 按 root_causes_at_limit 停线
            # （门禁正确行为，不进恢复主链）。
            second = reconciler.reconcile_supervisor_run(run_b0_again, campaign_dir)
            self.assertEqual(second["status"], "permanent_stop")
            self.assertEqual(second["root_cause"]["root_cause_id"], reconciled["root_cause"]["root_cause_id"])
            self.assertIn("permanent_stop", second)


if __name__ == "__main__":
    unittest.main()
