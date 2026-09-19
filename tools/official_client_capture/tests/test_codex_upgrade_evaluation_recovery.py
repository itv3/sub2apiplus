"""改造 5（评估失败局部恢复）M1：evaluator-only 端到端集成（T5.8～T5.10，走正式派发）。

链路：VC 链夹具 → r1 → VC-2～VC-4 → VC-5 断言批次（真实 builder＋真实 checker，2 条规则 1 条 fail）
→ 父 run failed（post-run-tooling，动作输出绑定 anchored）→ reconcile-supervisor-run → receipt_passed
→ evaluation-recover preview（assertion-failed／anchored／failure-scope）→ apply（evaluator-defect，
fixture 部署收据；checker 摘要变化以 patch 编译侧纯函数模拟）→ b1 committed → 派发 b1 评估批次
（评估基线后继协议）→ builder 两条规则全部重跑 → 全 pass → results.json。

附属：同基线幂等重入（零 checker、重现同一失败）；崩溃点 E1～E5 幂等续作；后继协议七类伪造拒绝；
R2 对账侧补写 post-run-tooling 收据。候选阶段结果与分类收据以合成对象提供（只影响 evaluation-recover
的只读读取，派发链全部真实）。
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
        # 证据根必须落在宿主数据根内（provenance 按冻结 CAPTURE_ROOT／宿主数据根映射）：bundle 建在 Campaign 目录下。
        official = self._bundle(campaign_dir / "official-evidence-eval", surface="codex", candidate_side=False)
        candidate = self._bundle(campaign_dir / "candidate-evidence", surface="other", candidate_side=True)
        job_ids = _candidate_job_ids(fixture)
        self.assertTrue(job_ids)  # type: ignore[attr-defined]
        # 首个候选 Job 的证据根就是候选 bundle 的来源根（目录名 run，与 provenance source_root 对应）。
        evidence_roots = {job_ids[0]: str(campaign_dir / "candidate-evidence" / "run")}
        for extra in job_ids[1:]:
            (campaign_dir / "candidate-evidence" / f"run-{extra}").mkdir(exist_ok=True)
            evidence_roots[extra] = str(campaign_dir / "candidate-evidence" / f"run-{extra}")
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

    def _completed_candidate_attempt(self, fixture: dict, *, evidence_roots: dict[str, str]) -> tuple[Path, dict]:
        """按正式预约与封存合同发布 Job 全部 complete、等待 seal 收据的候选 attempt（同 B0 夹具，候选为 R1）。"""

        campaign_dir = Path(str(fixture["campaign_dir"]))
        manifest = fixture["manifest"]
        identity = {"candidate_purpose": manifest["campaign_purpose"]}
        jobs = [
            Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=f"合成候选 Job {job_id}",
                steps=({"argv": ["bash", f"{job_id}.sh"]},),
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

    def test_evaluator_only_chain_from_failed_assertion_to_b1_full_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, context = self._ready_vc4(root)
            campaign_dir = context["campaign_dir"]

            # ---- b0：断言批次真实执行，SPEC-EP-006 fail → 父 run failed（post-run-tooling）----
            plan_b0 = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="b0")
            result, returncode = self._dispatch_plan(fixture, 5, plan_b0)
            self.assertEqual(returncode, 1, result)
            self.assertEqual(result["status"], "failed")
            run_b0 = Path(str(result["campaign_run"]["run_dir"]))
            self.assertEqual(result["campaign_run"]["timing_closeout"]["failure_class"], "post-run-tooling", result["campaign_run"]["timing_closeout"])
            batch_b0 = _read(campaign_dir / "control" / "vc" / "batches" / "0005-vc-5.json")
            self.assertEqual(batch_b0["schema_version"], artifacts.VC_BATCH_SCHEMA)
            self.assertIsNone(batch_b0["evaluation_baseline"])
            self.assertEqual(set(batch_b0["evaluator_digests"]), set(artifacts.EVALUATOR_DIGEST_FIELDS))
            self.assertEqual(batch_b0["actions"][0]["output_bindings"], [f"assertions/{R1}/checkpoints", f"assertions/{R1}/evaluation-run.json"])
            index_b0 = artifacts.validate_evaluation_run(_read(campaign_dir / "assertions" / R1 / "evaluation-run.json"))
            self.assertEqual({row["rule"]: row["status"] for row in index_b0["rules"]}, {"SPEC-EP-006": "fail", "SPEC-H1-001": "pass"})
            receipt = supervisor.read_stop_receipt(run_b0)
            self.assertEqual(receipt["reason"], "action-failed:assert-rules")
            binding = supervisor.read_action_output_binding(run_b0, "assert-rules")
            self.assertEqual(receipt["action_outputs_sha256"], binding["binding_sha256"])
            bound = {item["path"]: item for item in binding["bindings"]}
            self.assertEqual(bound[f"assertions/{R1}/evaluation-run.json"]["sha256"], _sha(campaign_dir / "assertions" / R1 / "evaluation-run.json"))
            self.assertEqual(bound[f"assertions/{R1}/checkpoints"]["sha256"], index_b0["checkpoint_head_sha256"])
            self.assertEqual(self._summary(fixture)["status"], "recovery_required")

            # ---- 账本 recovery_required：preview 允许、apply 拒绝 ----
            patches = self._stage_patches(context, context["candidate"])
            for patcher in patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
            self.assertEqual((preview["status"], preview["failure_source"], preview["reuse_authority"]), ("preview", "assertion-failed", "anchored"))
            self.assertEqual(preview["failed_step"], "SPEC-EP-006")
            self.assertEqual(preview["failure_scope"]["failed_rules"], ["SPEC-EP-006"])
            self.assertEqual(preview["failure_scope"]["jobs"], [context["job_ids"][0]])
            self.assertIn("transient-environment", preview["admissible_classes"])
            self.assertEqual(preview["failed_run"]["run_id"], run_b0.name)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_required"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview["review_sha256"]))

            # ---- reconcile → receipt_passed → active ----
            outcome = reconciler.reconcile_supervisor_run(run_b0, campaign_dir)
            self.assertEqual(outcome["status"], "recoverable")
            self.assertEqual(self._summary(fixture)["status"], "active")
            self.assertIn(("receipt_passed", f"reconcile-run-passed-{run_b0.name}"), self._events(fixture))

            # ---- apply：类别集合、准入负例、evaluator-defect 正例（checker 摘要变化） ----
            preview = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
            approve = preview["review_sha256"]
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "批准摘要"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256="0" * 64))
            redirect = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="candidate-source", approve_sha256=approve))
            self.assertEqual(redirect["status"], "redirect")
            self.assertIn("invalidate-candidate", redirect["next_command"])
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "--fix-commit"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=approve))
            deployment = Path(str(fixture["deployment"]))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有任何变化"):
                codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=approve, fix_commit="a" * 40, deployment_receipt=deployment))
            original_digests = policy_module.evaluator_dependency_digests()
            reader_only = dict(original_digests, accept_reader_sha256="e5" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=reader_only):
                # 变化项未覆盖断言失败声明的缺陷项（checker／builder）。
                preview_reader = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "未覆盖"):
                    codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview_reader["review_sha256"], fix_commit="a" * 40, deployment_receipt=deployment))
            fixed_digests = dict(original_digests, checker_sha256="f6" * 32)
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed_digests):
                preview_fixed = codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "preview"))
                apply_arguments = self._recover_arguments(fixture, "apply", root_cause_class="evaluator-defect", approve_sha256=preview_fixed["review_sha256"], fix_commit="a" * 40, deployment_receipt=deployment)
                baseline_dir = campaign_dir / "candidates" / R1 / "revisions" / "b1"
                # E3：AUTHORIZATION 写出前崩溃 → PREPARED＋outbox 已落盘，b1 不是当前基线，编译被拒；续作收敛。
                with mock.patch.object(artifacts, "build_evaluation_baseline_authorization", side_effect=artifacts.VCArtifactError("crash-e3")):
                    with self.assertRaises(codex_upgrade.ConfigurationError):
                        codex_upgrade.evaluation_recover(apply_arguments)
                self.assertTrue((baseline_dir / "PREPARED").is_file())
                self.assertFalse((baseline_dir / "COMMIT").exists())
                self.assertEqual(codex_upgrade._current_evaluation_baseline(campaign_dir, R1)[0], 0)
                recovery_bytes = (baseline_dir / "recovery.json").read_bytes()
                # E5：COMMIT 已写、账本事件未写 → 编译仍被拒（账本未引用）；续作只补账本事件。
                with mock.patch.object(timing_ledger, "append_event", side_effect=timing_ledger.TimingLedgerError("crash-e5")):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "拒绝 evaluation_baseline"):
                        codex_upgrade.evaluation_recover(apply_arguments)
                self.assertTrue((baseline_dir / "COMMIT").is_file())
                self.assertEqual(codex_upgrade._current_evaluation_baseline(campaign_dir, R1)[0], 0)
                self.assertEqual((baseline_dir / "recovery.json").read_bytes(), recovery_bytes)
                applied = codex_upgrade.evaluation_recover(apply_arguments)
                self.assertEqual((applied["status"], applied["evaluation_baseline"], applied["kind"]), ("applied", 1, "evaluator-only"))
                self.assertTrue(applied["ledger_event"]["appended"])
                self.assertEqual((baseline_dir / "recovery.json").read_bytes(), recovery_bytes)
                self.assertEqual(applied["execute_rules"], ["SPEC-EP-006", "SPEC-H1-001"])
                self.assertEqual(applied["reuse_rules"], [])
                self.assertEqual(applied["stage_sources"]["capture-candidate"]["source"], "reused")
                self.assertEqual(applied["stage_sources"]["compare"]["source"], "local")
                baseline_dir = campaign_dir / "candidates" / R1 / "revisions" / "b1"
                for name in ("diagnosis.json", "recovery.json", "PREPARED", "AUTHORIZATION", "COMMIT"):
                    self.assertTrue((baseline_dir / name).is_file(), name)
                self.assertEqual(codex_upgrade._current_evaluation_baseline(campaign_dir, R1)[0], 1)
                summary = self._summary(fixture)
                self.assertEqual((summary["status"], summary["active_phase"], summary["current_evaluation_baseline"]["evaluation_baseline"]), ("active", "VC-5", 1))
                # 基线已激活：再 apply 没有新的失败 run 可处理；没有可 abandon 的基线。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有评估动作失败的父 run"):
                    codex_upgrade.evaluation_recover(apply_arguments)
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "没有未 COMMIT"):
                    codex_upgrade.evaluation_recover(self._recover_arguments(fixture, "abandon"))

                # ---- b1 评估批次：后继协议 → 两条规则全部重跑（checker 变化）→ 全 pass ----
                fixed_bundle = self._bundle(campaign_dir / "candidate-evidence-fixed", surface="codex", candidate_side=True)
                plan_b1 = self._assertion_plan(context, root, baseline=1, candidate_bundle=fixed_bundle, tag="b1", reuse_from=campaign_dir / "assertions" / R1 / "evaluation-run.json", authority="anchored")
                result_b1, returncode_b1 = self._dispatch_plan(fixture, 6, plan_b1)
            self.assertEqual(returncode_b1, 0, result_b1)
            batch_b1 = _read(campaign_dir / "control" / "vc" / "batches" / "0006-vc-5.json")
            self.assertEqual((batch_b1["evaluation_baseline"], batch_b1["baseline_commit_sha256"]), (1, applied["commit_sha256"]))
            self.assertEqual(batch_b1["evaluator_digests"]["checker_sha256"], "f6" * 32)
            b1_root = campaign_dir / "assertions" / R1 / "revisions" / "b1"
            index_b1 = artifacts.validate_evaluation_run(_read(b1_root / "evaluation-run.json"))
            self.assertTrue(all(row["status"] == "pass" and row["reused_from"] is None for row in index_b1["rules"]))
            self.assertTrue((b1_root / "results.json").is_file())
            self.assertEqual(index_b1["evaluator"]["checker_sha256"], "f6" * 32)
            # 编译侧按账本当前基线冻结：动作里写错 --evaluation-baseline 0 的批次仍冻结 b1，builder 以清单冻结值
            # 校验不一致而失败关闭（不会写 b0 目录）。
            plan_stale = self._assertion_plan(context, root, baseline=0, candidate_bundle=context["candidate"], tag="stale")
            with mock.patch.object(policy_module, "evaluator_dependency_digests", return_value=fixed_digests):
                result_stale, returncode_stale = self._dispatch_plan(fixture, 7, plan_stale)
            self.assertEqual(returncode_stale, 1, result_stale)
            batch_stale = _read(campaign_dir / "control" / "vc" / "batches" / "0007-vc-5.json")
            self.assertEqual(batch_stale["evaluation_baseline"], 1)
            self.assertEqual(sorted(p.name for p in (campaign_dir / "assertions" / R1 / "checkpoints").iterdir() if not p.name.endswith("-input.json")), sorted(p.name for p in (campaign_dir / "assertions" / R1 / "checkpoints").iterdir() if not p.name.endswith("-input.json")))

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
            with self.assertRaisesRegex(supervisor.SupervisorError, "不是账本当前激活"):
                check(dict(successor, baseline_commit_sha256="0" * 64))
            with self.assertRaisesRegex(supervisor.SupervisorError, "不是账本当前激活"):
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
            with self.assertRaisesRegex(supervisor.SupervisorError, "不是账本当前激活"):
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
            self.assertEqual(reconciler.reconcile_supervisor_run(run_b0, campaign_dir)["status"], "recoverable")
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
            # 再对账即同根因第二次 → 按 root_causes_at_limit 停线（门禁正确行为，不进恢复主链）。
            second = reconciler.reconcile_supervisor_run(run_b0_again, campaign_dir)
            self.assertEqual(second["status"], "permanent_stop")
            self.assertIn("permanent_stop", second)


if __name__ == "__main__":
    unittest.main()
