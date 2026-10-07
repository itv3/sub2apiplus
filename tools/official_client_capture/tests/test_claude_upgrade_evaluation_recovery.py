"""Claude 评估失败局部恢复（Claude 升级控制面设计 B4-05a）：失败定位、批准断言画像冻结与三类根因收敛。

真实链部分沿用 B3-01 的真实工作树批次夹具：VC-3／VC-4 批次走到 Store ``candidate_sealed``，冻结批准断言画像，再以
``compile-and-run-vc-batch`` 派发一个会失败的 VC-5 断言批次（真实 Claude builder，证据与画像取 B4-03 的评估器夹具），对账
之后执行真实的 ``evaluation-recover``。候选阶段结果与候选 attempt 文档由合成对象提供（候选采集与封存 B4-01／B4-02 落地前的
接缝，只影响 evaluation-recover 的只读读取，派发、对账、恢复与 Store 写入全部真实）；Store 侧 VC-5 入口三类事实（selector 前态、
ValidationExecutionPlan、validation_attempt_created）按路线三 B4-02 的口径由夹具在封存批准的位置代写，执行计划的外部门禁
由 ``claude_upgrade_accept.candidate_external_gate_plan_entries`` 生成——开评估基线的 Store 门禁要求候选已进入 VC-5。覆盖：

* preview：失败来源、复用授权、按评估用 manifest 条目的 ``job_id`` 回溯的 failure-scope（伴随文件随锚点归同一 Job）与
  四类裁定集合；
* candidate-source：apply 返回 redirect，下一步是 invalidate-candidate；
* evaluator-defect：评估器摘要变化（以替身模拟修复后的 checker 摘要）与部署收据 → apply 写 b1 COMMIT，Store 同步登记评估基线；
* approval-inputs：apply 返回 redirect；approval-revision 按冻结画像的修订开 b1，生效画像与官方权威随之切换，b1 断言批次
  真实派发并通过；
* 冻结画像：绑定收据的输入与审计摘要、重跑复用、篡改即失败关闭；六个分类读侧成员。

单元部分：失败定位的输入合同与每一步失败关闭、b0 派生索引拒绝、修订画像差异判据。
"""

from __future__ import annotations

import contextlib
import copy
import json
import site
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_accept as accept
from tools.official_client_capture import claude_upgrade_assertion_profile as assertion_profile
from tools.official_client_capture import claude_upgrade_evaluation_recovery as evaluation_recovery
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.canonical import canonical_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILDER = REPOSITORY_ROOT / "tools" / "official_client_capture" / "claude_build_rule_assertion_results.py"
CANDIDATE = "claude-cand-r1"
ATTEMPT_ID = "attempt-0001"
JOB = "job-main"
run_main = batch_tests.run_main


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@unittest.skipUnless(site.ENABLE_USER_SITE, "批次夹具经 usercustomize 进入动作子进程，要求启用用户 site")
class EvaluationRecoveryChainTests(batch_tests._RealTreeBatchCase):
    """真实 VC-5 断言批次失败之后的 evaluation-recover（见模块说明）。"""

    # -- 世界：VC-4 完成 → 冻结画像 → 失败的 b0 断言批次 → 对账 -----------------------------------------------

    def failed_b0(self) -> dict[str, Any]:
        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        binding = assertion_profile.freeze_approved_assertion_profile(campaign, manifest)
        evaluation = campaign / "candidates" / CANDIDATE / "evaluation"
        context: dict[str, Any] = {
            "campaign": campaign,
            "manifest": manifest,
            "binding": binding,
            "evaluation": evaluation,
            "profile_path": evaluation / "assertion-profile.json",
            "rules_path": evaluator_fixture.write_rule_manifest(evaluation / "target-rules.json"),
            "official": evaluator_fixture.write_side(evaluation / "official", capture_id="eval-official"),
            # 候选侧 anthropic-version 头不符：SPEC-HDR-001 失败（B4-03 评估器夹具同一做法）。
            "candidate": evaluator_fixture.write_side(evaluation / "candidate", capture_id="eval-candidate", anthropic_version="2023-01-01"),
        }
        context["profile_sha256"] = evaluator_fixture.write_profile(context["profile_path"])
        # 合成的候选阶段结果（B4-02 接缝）：assertion_context 按失败定位的输入合同绑定候选评估用 manifest。
        manifest_path = context["candidate"].manifest_path
        candidate_stage = {
            "status": "complete",
            "assertion_context": {
                "evaluation_manifest": {"path": manifest_path.relative_to(campaign).as_posix(), "sha256": claude_upgrade.file_sha256(manifest_path)}
            },
        }
        context["candidate_stage_path"] = _write_json(campaign / "candidates" / CANDIDATE / "result.json", candidate_stage)
        self.open_validation_attempt(context)
        context["attempt_root"] = campaign / "candidates" / CANDIDATE / "attempts" / ATTEMPT_ID
        context["attempt"] = {"attempt_id": ATTEMPT_ID, "candidate_id": CANDIDATE, "results": [{"id": JOB, "status": "passed"}]}
        code, result, stderr = self.dispatch_builder(context, 9, baseline=0)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
        self.assertTrue((campaign / "assertions" / CANDIDATE / "evaluation-run.json").is_file())
        code, reconciled, stderr = run_main(
            ["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", Path(result["campaign_run"]["run_dir"])]
        )
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
        return context

    def open_validation_attempt(self, context: dict[str, Any]) -> None:
        """B4-02 接缝：在 Store 里写 selector 前态、ValidationExecutionPlan 与 validation_attempt_created（零请求）。"""

        store, campaign = claude_upgrade_store.open_campaign_store(context["campaign"], context["manifest"])
        campaign_id = str(campaign["campaign_id"])
        candidate = [fact for fact in store.list_facts(campaign_id, "validation") if fact["fact_kind"] == "candidate_frozen"][-1]
        candidate_payload = candidate["payload"]
        approval = store.load_fact(candidate_payload["profile_approval_ref"])["payload"]
        evidence_approval = store.load_fact(approval["evidence_approval_ref"])["payload"]
        classification = store.load_fact(evidence_approval["classification_fact_ref"])["payload"]
        build = store.load_receipt(candidate_payload["candidate_build_receipt_ref"])
        issued = claude_upgrade._utc_now()
        snapshot = store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": campaign["persona"],
                "catalog_digest": "d" * 64,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": issued,
                "source_ref": {"path": "runtime/selector.json", "sha256": "e" * 64, "bytes": 1},
            },
        )
        selector = store.append_fact(campaign_id, "selector_observed", {"catalog_snapshot_ref": snapshot, "observation_kind": "read_only"}, issued)
        gates = accept.candidate_external_gate_plan_entries(host_id="arm64-runner", architecture=str(candidate_payload["target_architecture"]))
        conditions = {
            "run_nonce": "nonce-accept-0001",
            "environment_sha256": "4" * 64,
            "tool_sha256": store_receipts.effective_tool_identity(store, campaign)["tool_files_sha256"],
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "account_model_sha256": "a" * 64,
        }
        vircs = {"ownership": "operator_managed", "verification": "unverified", "action": "not_touched"}
        plan = {
            "schema_version": "official-client-validation-execution-plan/v1",
            "campaign_id": campaign_id,
            "persona": campaign["persona"],
            "plan_id": "validation-plan-0001",
            "candidate_id": CANDIDATE,
            "candidate_ref": store.fact_ref(candidate),
            "candidate_build_receipt_ref": candidate_payload["candidate_build_receipt_ref"],
            "profile_approval_ref": candidate_payload["profile_approval_ref"],
            "scenario_plan_ref": approval["scenario_plan_ref"],
            "atomic_assertion_ledger_ref": classification["atomic_assertion_ledger_ref"],
            "validation_scope_sha256": build["validation_scope_sha256"],
            "execute_item_ids": sorted(["baseline", *(gate["gate_id"] for gate in gates)]),
            "reuse_item_ids": [],
            "external_gates": gates,
            "run_conditions": conditions,
            "production_selector_before_ref": selector,
            "vircs_state": vircs,
        }
        plan["identity_sha256"] = canonical_sha256(plan)
        plan_ref = store.seal_object("validation_execution_plan", plan)
        attempt = {
            "schema_version": "official-client-validation-attempt/v1",
            "attempt_id": ATTEMPT_ID,
            "candidate_id": CANDIDATE,
            "candidate_ref": store.fact_ref(candidate),
            "candidate_build_receipt_ref": candidate_payload["candidate_build_receipt_ref"],
            "validation_execution_plan_ref": plan_ref,
            "run_conditions": conditions,
            "production_selector_before_ref": selector,
            "vircs_state": vircs,
            "result": "created",
        }
        attempt["identity_sha256"] = canonical_sha256(attempt)
        store.append_fact(campaign_id, "validation_attempt_created", attempt, issued)

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """派发一个 VC-5 断言批次：真实 Claude builder，结果写进当前基线的 assertions 目标（b0 原路径，b≥1 COMMIT 的 local 目标）。"""

        campaign = context["campaign"]
        target = claude_upgrade._stage_write_target(campaign, CANDIDATE, baseline, "assertions")
        config = {
            "assertion_profile": str(context["profile_path"]),
            "rule_manifest": str(context["rules_path"]),
            "expected_profile_sha256": context["profile_sha256"],
            "official_evidence_root": str(context["official"].evidence_root),
            "candidate_evidence_root": str(context["candidate"].evidence_root),
            "official_capture_manifest": str(context["official"].manifest_path),
            "candidate_capture_manifest": str(context["candidate"].manifest_path),
            "official_evidence_prefix": "official-run",
            "candidate_evidence_prefix": "candidate-run",
            "target_version": evaluator_fixture.TARGET_VERSION,
            "candidate_id": CANDIDATE,
            "profile_id": "claude-eval-v1",
            "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64,
            "candidate_package_digest": "2" * 64,
            "comparison_package_digest": "3" * 64,
            "official_authority": dict(evaluator_fixture.AUTHORITY),
            "rules": list(evaluator_fixture.RULES),
            "campaign_dir": str(campaign),
        }
        config_path = _write_json(context["evaluation"] / f"builder-config-b{baseline}.json", config)
        action = {
            "action_id": "assert-rules",
            "operation": "VC-5:assert-rules",
            "timeout_seconds": 600.0,
            "command": [
                sys.executable, str(BUILDER), "--config", str(config_path), "--output", str(target / "results.json"),
                "--results-dir", str(target / "machine"), "--evaluation-baseline", str(baseline),
            ],
            "item_ids": ["assert-rules"],
        }
        plan = _write_json(self.root / "action-plans" / f"vc-5-assert-b{baseline}.json", batch_tests.batch.action_plan([action], phase="VC-5"))
        return run_main(
            [
                "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-5", "--sequence", sequence,
                "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-4", revision=1),
                "--action-plan", plan, "--state-dir", self.state_dir,
            ]
        )

    @contextlib.contextmanager
    def candidate_seams(self, context: dict[str, Any]) -> Iterator[None]:
        """B4-01／B4-02 落地前的接缝：候选阶段结果读合成文件，候选 attempt 上下文给合成文档。"""

        real_load = claude_upgrade._load_stage_result

        def load(campaign_dir: Path, stage: str, candidate_id: str | None = None, **options: Any) -> dict[str, Any]:
            if stage in {"capture-candidate", "candidate"}:
                return json.loads(context["candidate_stage_path"].read_text(encoding="utf-8"))
            return real_load(campaign_dir, stage, candidate_id, **options)

        with mock.patch.object(claude_upgrade, "_load_stage_result", side_effect=load), mock.patch.object(
            claude_upgrade, "_capture_stage_attempt_context", return_value=(context["attempt_root"], dict(context["attempt"]))
        ):
            yield

    def recover(self, context: dict[str, Any], action: str, *extra: Any) -> tuple[int, dict | None, str]:
        return run_main(
            ["evaluation-recover", action, "--campaign-dir", context["campaign"], "--candidate-id", CANDIDATE, "--reviewer", "reviewer-a", *extra]
        )

    def preview(self, context: dict[str, Any]) -> dict[str, Any]:
        code, preview, stderr = self.recover(context, "preview")
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (0, "preview"), stderr)
        return preview

    # -- 用例 -------------------------------------------------------------------------------------------------

    def test_preview_scope_frozen_profile_and_candidate_source_redirect(self) -> None:
        context = self.failed_b0()
        campaign, manifest = context["campaign"], context["manifest"]
        # 冻结画像：绑定收据写明输入、批准与审计摘要；重跑冻结复用；分类视图附上两项评估字段；六个读侧成员可用。
        binding = context["binding"]
        self.assertEqual(set(binding["inputs"]), {"atomic_assertion_ledger", "scenarios", "rule_manifest", "release_profile", "release_wire"})
        self.assertEqual(binding["audit"]["generator"]["checker_sha256"], claude_identity.evaluator_dependency_digests()["checker_sha256"])
        self.assertEqual(binding["audit"]["templates"]["sha256"], claude_upgrade.file_sha256(assertion_profile.TEMPLATES_PATH))
        self.assertEqual(assertion_profile.freeze_approved_assertion_profile(campaign, manifest), binding)
        classification = claude_upgrade._load_stage_result(campaign, "classify")
        self.assertEqual(classification["assertion_profile_manifest"], {"path": binding["profile"]["path"], "sha256": binding["profile"]["sha256"]})
        self.assertEqual(classification["joint_manifest_sha256"], binding["approvals"]["profile_joint_sha256"])
        authority = claude_upgrade._classification_official_authority(classification)
        self.assertEqual(
            authority,
            {
                "assertion_profile_sha256": binding["profile"]["sha256"],
                "classification_package_digest": binding["approvals"]["classification_review_sha256"],
                "review_sha256": binding["approvals"]["profile_joint_sha256"],
            },
        )
        self.assertEqual(list(claude_upgrade._approved_rules(campaign, manifest, require_approved=True)), list(classification["target_rules"]))
        self.assertEqual(claude_upgrade._effective_classification_view(campaign, CANDIDATE, classification), classification)
        self.assertIsNone(claude_upgrade._effective_assertion_profile(campaign, CANDIDATE, 0))
        self.assertEqual(claude_upgrade._approval_revision_baselines(campaign, CANDIDATE), [])

        with self.candidate_seams(context):
            preview = self.preview(context)
            self.assertEqual((preview["failure_source"], preview["reuse_authority"], preview["failed_step"]), ("assertion-failed", "anchored", "SPEC-HDR-001"))
            scope = preview["failure_scope"]
            self.assertEqual((scope["failed_rules"], scope["jobs"]), (["SPEC-HDR-001"], [JOB]))
            (failed_check,) = scope["failed_checks"]
            # 伴随文件（上游响应字节、relay.json）沿用锚点的 job_id，一起定位到同一 Job。
            self.assertIn(f"{JOB}/print/relay/conn001.upstream_to_client.bin", failed_check["evidence_paths"])
            self.assertEqual(scope["rules"], sorted(evaluator_fixture.RULES))
            self.assertEqual(preview["admissible_classes"], ["approval-inputs", "candidate-source", "evaluator-defect", "transient-environment"])
            code, redirect, stderr = self.recover(
                context, "apply", "--approve-sha256", preview["review_sha256"], "--root-cause-class", "candidate-source"
            )
        self.assertEqual((code, redirect["status"]), (0, "redirect"), stderr)
        self.assertIn("invalidate-candidate preview", redirect["next_command"])
        self.assertEqual(claude_upgrade._current_evaluation_baseline(campaign, CANDIDATE), (0, None))

        # 模板与生成器摘要只用于审计：冻结之后模板变化不影响读取，重跑冻结仍返回原绑定。
        changed_templates = self.root / "changed-templates.json"
        changed_templates.write_text(assertion_profile.TEMPLATES_PATH.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with mock.patch.object(assertion_profile, "TEMPLATES_PATH", changed_templates):
            self.assertEqual(assertion_profile.load_approved_assertion_profile(campaign, manifest)["sha256"], binding["profile"]["sha256"])
            self.assertEqual(assertion_profile.freeze_approved_assertion_profile(campaign, manifest), binding)
        # 绑定收据的输入与 Campaign 当前状态不符：失败关闭。
        binding_file = campaign / assertion_profile.BINDING_RELATIVE
        original = binding_file.read_text(encoding="utf-8")
        forged = json.loads(original)
        forged["approvals"]["profile_joint_sha256"] = "0" * 64
        binding_file.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, r"冻结批准断言画像之后输入已变化（approvals）"):
            assertion_profile.load_approved_assertion_profile(campaign, manifest)
        binding_file.write_text(original, encoding="utf-8")
        # 篡改冻结画像：读取即失败关闭（分类视图也拒绝）。
        profile_file = campaign / binding["profile"]["path"]
        profile_file.write_text(profile_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "冻结的批准断言画像缺失或摘要漂移"):
            assertion_profile.load_approved_assertion_profile(campaign, manifest)

    def test_evaluator_defect_apply_commits_b1_with_store_fact(self) -> None:
        context = self.failed_b0()
        campaign = context["campaign"]
        fixed = dict(claude_identity.evaluator_dependency_digests(), checker_sha256="f6" * 32)
        receipt = self.data / "control" / "claude-managed-deploy-20261007t000000z.json"
        with self.candidate_seams(context), mock.patch.object(claude_identity, "evaluator_dependency_digests", return_value=fixed):
            preview = self.preview(context)
            code, applied, stderr = self.recover(
                context, "apply", "--approve-sha256", preview["review_sha256"], "--root-cause-class", "evaluator-defect",
                "--fix-commit", "a" * 40, "--deployment-receipt", receipt,
            )
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"], applied["kind"], applied["evaluation_baseline"]), (0, "applied", "evaluator-only", 1), stderr)
        # checker 变化：全部规则依赖摘要变化，全量重跑。
        self.assertEqual(applied["execute_rules"], sorted(claude_upgrade._approved_rules(campaign, context["manifest"], require_approved=True)))
        self.assertEqual(applied["store"]["payload"]["baseline"], 1)
        self.assertEqual(applied["store"]["payload"]["kind"], "evaluator-only")
        baseline, commit = claude_upgrade._current_evaluation_baseline(campaign, CANDIDATE)
        self.assertEqual((baseline, commit["commit_sha256"]), (1, applied["store"]["payload"]["baseline_commit_sha256"]))
        recovery = claude_upgrade._load_evaluation_baseline_recovery(campaign, CANDIDATE, 1)
        self.assertEqual(recovery["current_evaluator_digests"], fixed)

    def test_approval_inputs_redirect_then_approval_revision_and_b1_batch_passes(self) -> None:
        context = self.failed_b0()
        campaign, manifest = context["campaign"], context["manifest"]
        frozen = json.loads((campaign / context["binding"]["profile"]["path"]).read_text(encoding="utf-8"))
        revised = copy.deepcopy(frozen)
        revised["rules"][0]["checks"][0]["description"] = "修订：批准断言的说明更正"
        revised_path = _write_json(self.root / "revised-assertion-profile.json", revised)
        with self.candidate_seams(context):
            preview = self.preview(context)
            code, redirect, stderr = self.recover(
                context, "apply", "--approve-sha256", preview["review_sha256"], "--root-cause-class", "approval-inputs"
            )
            self.assertEqual((code, redirect["status"]), (0, "redirect"), stderr)
            self.assertIn("approval-revision", redirect["next_command"])
            code, revision_preview, stderr = self.recover(context, "approval-revision", "--assertion-profile", revised_path)
            self.assertIsNotNone(revision_preview, stderr)
            self.assertEqual((code, revision_preview["status"]), (0, "preview"), stderr)
            self.assertEqual(revision_preview["changed_rule_ids"], [revised["rules"][0]["rule_id"]])
            code, applied, stderr = self.recover(
                context, "approval-revision", "--assertion-profile", revised_path, "--approve-sha256", revision_preview["review_sha256"]
            )
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"], applied["evaluation_baseline"]), (0, "applied", 1), stderr)
        self.assertEqual(applied["store"]["payload"]["kind"], "approval-revision")
        # 生效画像与官方权威切到修订画像；修订基线登记进候选的 approval-revision 列表。
        effective = claude_upgrade._effective_assertion_profile(campaign, CANDIDATE, 1)
        self.assertEqual(effective["sha256"], claude_upgrade.file_sha256(revised_path))
        classification = claude_upgrade._effective_classification_view(campaign, CANDIDATE, claude_upgrade._load_stage_result(campaign, "classify"))
        self.assertEqual(classification["assertion_profile_manifest"]["sha256"], effective["sha256"])
        self.assertEqual(claude_upgrade._classification_official_authority(classification)["assertion_profile_sha256"], effective["sha256"])
        self.assertEqual(claude_upgrade._approval_revision_baselines(campaign, CANDIDATE), [1])
        # b1 断言批次：COMMIT 冻结的 local 目标、授权摘要与父 run 绑定全部真实；换成正确的候选证据后通过。
        context["candidate"] = evaluator_fixture.write_side(context["evaluation"] / "candidate-fixed", capture_id="eval-candidate")
        code, result, stderr = self.dispatch_builder(context, 10, baseline=1)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
        index = json.loads((claude_upgrade._stage_write_target(campaign, CANDIDATE, 1, "assertions") / "evaluation-run.json").read_text(encoding="utf-8"))
        self.assertEqual({row["status"] for row in index["rules"]}, {"pass"})
        self.assertEqual(index["evaluation_baseline"], 1)
        del manifest


class FailureScopeTests(unittest.TestCase):
    """失败定位的输入合同与每一步失败关闭（合成目录，不建 Campaign）。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-failure-scope-")
        self.addCleanup(temporary.cleanup)
        self.campaign = Path(temporary.name).resolve() / "campaign"
        self.side = evaluator_fixture.write_side(self.campaign / "candidate", capture_id="eval-candidate")
        manifest = self.side.manifest_path
        self.candidate = {"assertion_context": {"evaluation_manifest": {"path": manifest.relative_to(self.campaign).as_posix(), "sha256": claude_upgrade.file_sha256(manifest)}}}
        self.attempt = {"results": [{"id": JOB}]}
        self.assertions = self.campaign / "assertions" / CANDIDATE
        self.paths = [artifact["path"] for artifact in json.loads(manifest.read_text(encoding="utf-8"))["artifacts"]]
        self.run = {"rules": [{"rule": "SPEC-A-001", "status": "fail"}, {"rule": "SPEC-B-001", "status": "pass"}, {"rule": "SPEC-C-001", "status": "pass"}]}
        self.document("candidate", "SPEC-A-001", [{"id": "a.fail", "passed": False, "evidence_paths": [self.paths[0]]}])
        self.document("candidate", "SPEC-B-001", [{"id": "b.ok", "passed": True, "evidence_paths": [self.paths[0]]}])
        self.document("candidate", "SPEC-C-001", [{"id": "c.ok", "passed": True, "evidence_paths": []}])

    def document(self, side: str, rule: str, checks: list[dict[str, Any]]) -> None:
        _write_json(self.assertions / "machine" / side / f"{rule}.json", {"rule": rule, "checks": checks})

    def scope(self) -> tuple[dict[str, Any], str]:
        return evaluation_recovery.evaluation_failure_scope(self.campaign, self.candidate, self.attempt, self.run, self.assertions)

    def test_scope_follows_manifest_job_ids(self) -> None:
        inputs = evaluation_recovery.candidate_failure_scope_inputs(self.campaign, self.candidate, self.attempt)
        self.assertEqual(set(inputs["artifact_jobs"].values()), {JOB})
        self.assertEqual(inputs["attempt_jobs"], [JOB])
        scope, failed_step = self.scope()
        self.assertEqual(failed_step, "SPEC-A-001")
        self.assertEqual((scope["jobs"], scope["failed_rules"]), ([JOB], ["SPEC-A-001"]))
        # R*：引用了 J* 任一 artifact 的规则（SPEC-B-001 引用同一证据，SPEC-C-001 不引用）。
        self.assertEqual(scope["rules"], ["SPEC-A-001", "SPEC-B-001"])
        self.assertEqual(set(scope), {"failed_rules", "failed_checks", "jobs", "rules", "official_refs"})
        # 候选侧文档缺失（官方侧失败）：只列官方引用，不定位候选 Job。
        (self.assertions / "machine" / "candidate" / "SPEC-A-001.json").unlink()
        self.document("official", "SPEC-A-001", [{"id": "a.official", "passed": False, "evidence_paths": ["official/x.bin"]}])
        scope, _step = self.scope()
        self.assertEqual((scope["jobs"], scope["official_refs"], scope["rules"]), ([], ["official/x.bin"], ["SPEC-A-001"]))

    def test_each_step_fails_closed(self) -> None:
        # 失败 check 引用的证据不在评估用 manifest 里。
        self.document("candidate", "SPEC-A-001", [{"id": "a.fail", "passed": False, "evidence_paths": ["job-main/unknown.bin"]}])
        with self.assertRaisesRegex(ConfigurationError, "不在候选评估用 manifest 里"):
            self.scope()
        self.document("candidate", "SPEC-A-001", [{"id": "a.fail", "passed": False, "evidence_paths": [self.paths[0]]}])
        with self.assertRaisesRegex(ConfigurationError, "不在原 attempt 内"):
            evaluation_recovery.evaluation_failure_scope(self.campaign, self.candidate, {"results": [{"id": "job-other"}]}, self.run, self.assertions)
        with self.assertRaisesRegex(ConfigurationError, "没有登记 Job"):
            evaluation_recovery.evaluation_failure_scope(self.campaign, self.candidate, {"results": []}, self.run, self.assertions)
        with self.assertRaisesRegex(ConfigurationError, "缺少 assertion_context.evaluation_manifest"):
            evaluation_recovery.evaluation_failure_scope(self.campaign, {}, self.attempt, self.run, self.assertions)
        with self.assertRaisesRegex(ConfigurationError, "没有 status=fail"):
            evaluation_recovery.evaluation_failure_scope(self.campaign, self.candidate, self.attempt, {"rules": [{"rule": "SPEC-A-001", "status": "pass"}]}, self.assertions)
        with self.assertRaisesRegex(ConfigurationError, "需要已校验的 evaluation-run.json"):
            evaluation_recovery.evaluation_failure_scope(self.campaign, self.candidate, self.attempt, object(), self.assertions)  # type: ignore[arg-type]
        # manifest 条目缺 job_id：定位链不完整。
        manifest = json.loads(self.side.manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][0].pop("job_id")
        self.side.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "摘要漂移"):
            self.scope()
        self.candidate["assertion_context"]["evaluation_manifest"]["sha256"] = claude_upgrade.file_sha256(self.side.manifest_path)
        with self.assertRaisesRegex(ConfigurationError, "缺少 job_id"):
            self.scope()

    def test_derive_b0_evaluation_run_refuses(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "同基线逐字重派该断言批次"):
            claude_upgrade._derive_b0_evaluation_run(self.campaign, {}, CANDIDATE, 1, {}, ())


class DeploymentReceiptTests(unittest.TestCase):
    """修复部署收据核对：Claude 受管部署收据的五摘要在 identity 子键，判据与共享实现逐条相同。"""

    IDENTITY = {
        "policy_version": "v3",
        "policy_sha256": "1" * 64,
        "wire_producer_sha256": "2" * 64,
        "evidence_semantics_sha256": "3" * 64,
        "control_sha256": "4" * 64,
        "tool_files_sha256": "5" * 64,
    }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-deploy-receipt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        current = {**{key: value for key, value in self.IDENTITY.items() if key != "tool_files_sha256"}, "files_sha256": self.IDENTITY["tool_files_sha256"]}
        for target, value in (("_is_policy_v2_identity", False), ("_tool_identity", current)):
            patcher = mock.patch.object(claude_upgrade, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manifest = {"tool_identity": {"wire_producer_sha256": "2" * 64, "policy_sha256": "1" * 64}}

    def receipt(self, **overrides: Any) -> Path:
        payload = {"schema_version": "official-client-managed-deploy/v1", "status": "passed", "client": "claude", "identity": dict(self.IDENTITY)}
        payload.update(overrides)
        return _write_json(self.root / f"receipt-{canonical_sha256(payload)[:12]}.json", payload)

    def test_claude_receipt_shape(self) -> None:
        self.assertEqual(claude_upgrade._verify_current_deployment_receipt(self.root, self.manifest, self.receipt())["client"], "claude")
        cases = {
            "status 不是 passed": self.receipt(status="failed"),
            "不是 Claude 受管部署收据": self.receipt(client="codex"),
            "wire_producer_sha256 与 Campaign 有效工具身份不一致": self.receipt(identity={**self.IDENTITY, "wire_producer_sha256": "9" * 64}),
            "control_sha256 不等于当前受管工具树身份": self.receipt(identity={**self.IDENTITY, "control_sha256": "9" * 64}),
        }
        for pattern, path in cases.items():
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ConfigurationError, pattern):
                claude_upgrade._verify_current_deployment_receipt(self.root, self.manifest, path)
        # Codex 形状（顶层五摘要、没有 identity）不被当作 Claude 收据。
        codex_shaped = _write_json(self.root / "codex.json", {"status": "passed", **self.IDENTITY})
        with self.assertRaisesRegex(ConfigurationError, "不是 Claude 受管部署收据"):
            claude_upgrade._verify_current_deployment_receipt(self.root, self.manifest, codex_shaped)


class ApprovalRevisionDeltaTests(unittest.TestCase):
    """修订画像差异判据：只允许 selector／assertion／description 变化。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-approval-delta-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current = evaluator_fixture.build_profile()
        self.current_path = _write_json(self.root / "current.json", self.current)

    def delta(self, revised: dict[str, Any]) -> dict[str, Any]:
        return claude_upgrade._approval_revision_delta(self.current_path, _write_json(self.root / "revised.json", revised))

    def test_selector_revision_is_accepted_and_others_refused(self) -> None:
        revised = copy.deepcopy(self.current)
        check = revised["rules"][0]["checks"][0]
        check["description"] = "修订说明"
        result = self.delta(revised)
        self.assertEqual(result["changed_rule_ids"], [revised["rules"][0]["rule_id"]])
        self.assertEqual(result["changed_check_ids"], [f"{revised['rules'][0]['rule_id']}:{check['id']}"])
        cases = {
            "claude_version": lambda doc: doc.__setitem__("claude_version", "2.1.286"),
            "generated_from": lambda doc: doc["generated_from"].__setitem__("ledger", {"schema_version": "x", "sha256": "0" * 64}),
            "rules（规则 id 集合）": lambda doc: doc["rules"].pop(),
            "atomic_assertion_id": lambda doc: doc["rules"][0]["checks"][0].__setitem__("atomic_assertion_id", "ASSERT-OTHER"),
            r"\.atomic_assertion_ids": lambda doc: doc["rules"][0].__setitem__("atomic_assertion_ids", ["ASSERT-X-999"]),
        }
        for pattern, mutate in cases.items():
            broken = copy.deepcopy(self.current)
            mutate(broken)
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ConfigurationError, pattern):
                self.delta(broken)
        with self.assertRaisesRegex(ConfigurationError, "没有差异"):
            self.delta(copy.deepcopy(self.current))


if __name__ == "__main__":
    unittest.main()
