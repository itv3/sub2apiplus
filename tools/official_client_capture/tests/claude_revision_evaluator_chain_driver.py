"""Claude 后段链的候选 r2 与评估器修复驱动，尚未登记完整 A 组认证。

同一 Campaign 真实执行：r1 候选 run/seal、失败 builder、candidate-source 裁定与作废、r2 新源码构建和
run/seal、再次失败 builder、实际 checker 源码修复、工具演进、evaluation epoch、b1 恢复和 builder。
候选来源沿用 transient 驱动的离线宿主／Docker／网关及中继字节来源；不替换阶段结果、Store 或断言结论。

checker 缺陷只允许写在通过 shell._assert_isolation 的副本树：请求解析器误把版本头保留原大小写。
r1 的小写版本头带错误值，定位候选源码问题；r2 的值正确但使用混合大小写，从而暴露 checker 缺陷。
修复还原解析器源码，所有身份由实际策略重算，不替换摘要，也不直接写入 passed。演进合同拒绝时直接失败。

修复后先在 evaluator-defect apply 的强制部署收据门拒绝 Codex 收据和缺 client 收据，原 Campaign 与两本账不得写入。
本链仍采用一条请求头规则画像，证明候选与评估器恢复的连续性；不能据此声明完整规则 accept 或 VC-6 通过。
调用方可取得返回的同一 Campaign／r2／b1 世界继续扩展。部署收据与构建实物明确来自外部来源夹具。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_rule_assertion as assertion
from tools.official_client_capture import claude_tool_identity_policy as identity_policy
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_assertion_profile as assertion_profile
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_store as candidate_store
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_boundary_receipt_fixture as boundary_fixture
from tools.official_client_capture.tests import claude_candidate_build_fixture as build_fixture
from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import claude_transient_chain_driver as transient
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_control_chain as shell

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SECOND_CANDIDATE = "claude-cand-r2"
REQUEST_HEADER_LINE = "            lowered.setdefault(name.lower(), []).append(value)\n"
DEFECTIVE_HEADER_LINE = (
    "            # 连续链副本预埋：版本头被误当作大小写敏感字段。\n"
    '            lowered.setdefault(name if name.lower() == "anthropic-version" else name.lower(), []).append(value)\n'
)


class ClaudeRevisionEvaluatorChainDriver(transient.ClaudeTransientChainDriver):
    """同一世界的候选修订与评估器恢复；普通工作树调用在任何修改之前拒绝。"""

    def require_isolated_copy(self) -> None:
        shell._assert_isolation(self, shell._ChainRecorder("候选修订与评估器恢复的隔离来源"))
        self.assertEqual(Path(os.environ[shell.TREE_ENV]).resolve(), REPOSITORY_ROOT)
        self.assertFalse((REPOSITORY_ROOT / ".git").exists(), "预埋／修复不能作用于 Git 工作树")

    def setUp(self) -> None:
        self.require_isolated_copy()
        self.checker_path = Path(assertion.__file__).resolve()
        self.assertTrue(self.checker_path.is_relative_to(REPOSITORY_ROOT))
        self.fixed_checker = self.checker_path.read_bytes()
        source = self.fixed_checker.decode("utf-8")
        self.assertEqual(source.count(REQUEST_HEADER_LINE), 2, "请求和响应各有一处归一化；只改第一处请求解析")
        self.checker_path.write_text(source.replace(REQUEST_HEADER_LINE, DEFECTIVE_HEADER_LINE, 1), encoding="utf-8")
        self.addCleanup(self.restore_checker)
        self.tool_identity = upgrade._tool_identity(include_git=False)
        self.defective_evaluator = identity_policy.evaluator_dependency_digests()
        self.active_candidate, self.active_revision = transient.CANDIDATE, 1
        super().setUp()
        transient._write(self.relay_control, {"bad_probe": transient.FAILED_PROBE, "header_name": "anthropic-version"})

    def restore_checker(self) -> None:
        """修复和清理都再验隔离边界；只能还原当前副本里预先保留的原始字节。"""

        self.require_isolated_copy()
        self.assertEqual(self.checker_path, Path(assertion.__file__).resolve())
        self.checker_path.write_bytes(self.fixed_checker)

    def install_relay_source(self) -> None:
        super().install_relay_source()
        path = self.runtime.tool_copy / "claude_fw_e_relay.py"
        source = path.read_text(encoding="utf-8")
        anchor = '        stream += f"{method} {target} HTTP/1.1'
        self.assertEqual(source.count(anchor), 1)
        source = source.replace(anchor, '        header_name = json.load(open(os.environ["CLAUDE_TRANSIENT_RELAY_CONTROL"], encoding="utf-8"))["header_name"]\n' + anchor)
        self.assertEqual(source.count("Anthropic-Version: {version}"), 1)
        path.write_text(source.replace("Anthropic-Version: {version}", "{header_name}: {version}"), encoding="utf-8")

    def prepare_runtime(self, campaign: Path) -> None:
        self.build = json.loads((campaign / "candidates" / self.active_candidate / "build-receipt.json").read_bytes())
        gateway = copy.deepcopy(candidate_tests.BASE_GATEWAY)
        gateway["service"]["image_id"] = self.build["image"]["image_id"]
        transient._write(self.runtime.gateway_file, gateway)
        self.runtime.activation_fact = {
            "schema_version": candidate_capture.ACTIVATION_FACT_SCHEMA, "source": "sub2api-runtime",
            "event_type": "claude_profile_observed", "event_id": "a" * 64, "profile_mode": "validation_candidate",
            "claude_version": capture_tests.TARGET, "profile_digest": self.build["profile"]["profile_digest"],
            "image_id": self.build["image"]["image_id"],
        }

    def write_clients(self, campaign: Path, attempt_root: Path, attempt: dict) -> list[str]:
        values = {"campaign_id": campaign.name, "candidate_id": self.active_candidate,
                  "attempt_id": attempt_root.name, "run_nonce": attempt["run_nonce"]}
        original = (attempt_root / "attempt.json").read_bytes()
        argv = []
        for client in ("claude-desktop", "claude-vscode"):
            path = client_fixture.write_receipt(attempt_root, client, values)
            argv += ["--client-evidence", f"{client}={path}"]
        for check in ("codex-final-wire-isolation", "third-party-ingress-rejection"):
            boundary_fixture.write_boundary_receipt(attempt_root, check)
        self.assertEqual((attempt_root / "attempt.json").read_bytes(), original)
        return argv

    def seal(self, campaign: Path, attempt_id: str, *, clients: list[str] | None = None, recovery: str | None = None) -> dict:
        self.assertIsNone(recovery, "本驱动只开新候选；恢复段由独立 transient 连续链覆盖")
        argv = ["capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", self.active_candidate,
                "--attempt-id", attempt_id, *list(clients or [])]
        code, preview, stderr = self.source_command(argv)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        code, result, stderr = self.source_command([*argv, "--approve-seal-sha256", preview["review_sha256"], "--approved-by", "连续链审核"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (0, "complete"), stderr)
        return result

    def capture_and_seal(self, campaign: Path) -> tuple[Path, dict, dict]:
        """当前候选必须真实产生 attempt 和证据；写好客户端来源后真实封存并对账。"""

        self.prepare_runtime(campaign)
        code, captured, stderr = self.source_command(["capture-candidate", "run", "--campaign-dir", campaign,
            "--candidate-id", self.active_candidate, "--acknowledge-live-requests"])
        self.assertIsNotNone(captured, stderr)
        self.assertEqual((code, captured["status"]), (2, "awaiting_receipts"), stderr)
        with self.source_context():
            attempt_root, attempt = upgrade._load_capture_attempt(campaign, "candidate", self.active_candidate, captured["attempt_id"])
        self.seal(campaign, attempt_root.name, clients=self.write_clients(campaign, attempt_root, attempt))
        code, accounted, stderr = self.source_command(["account-sealed-candidate", "--campaign-dir", campaign, "--candidate-id", self.active_candidate])
        self.assertEqual(code, 0, stderr)
        with self.source_context():
            stage = upgrade._load_stage_result(campaign, "capture-candidate", self.active_candidate, _baseline=0)
        self.assertEqual(stage["attempt"]["path"], (attempt_root / "attempt.json").relative_to(campaign).as_posix())
        return attempt_root, attempt, stage

    def recover(self, context: dict[str, Any], action: str, *extra: Any) -> tuple[int, dict | None, str]:
        return self.source_command(["evaluation-recover", action, "--campaign-dir", context["campaign"],
            "--candidate-id", self.active_candidate, "--reviewer", "连续链审核", *extra])

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """新 revision 与评估基线分别取自己的 checkpoint 和输出位置；候选 manifest 必须来自真实 seal。"""

        campaign = context["campaign"]
        with self.source_context():
            stage = upgrade._load_stage_result(campaign, "capture-candidate", self.active_candidate, _baseline=baseline)
        candidate = compare.candidate_evaluation_source(campaign, upgrade.load_campaign_manifest(campaign), stage)
        target = upgrade._stage_write_target(campaign, self.active_candidate, baseline, "assertions")
        config = {
            "assertion_profile": str(context["profile_path"]), "rule_manifest": str(context["rules_path"]),
            "expected_profile_sha256": context["profile_sha256"], "official_evidence_root": str(context["official"].evidence_root),
            "candidate_evidence_root": candidate["evidence_root"], "official_capture_manifest": str(context["official"].manifest_path),
            "candidate_capture_manifest": str(campaign / stage["assertion_context"]["evaluation_manifest"]["path"]),
            "official_evidence_prefix": "official-run", "candidate_evidence_prefix": "candidate-run", "target_version": capture_tests.TARGET,
            "candidate_id": self.active_candidate, "profile_id": "revision-header", "profile_digest": "d" * 64,
            "official_package_digest": "1" * 64, "candidate_package_digest": "2" * 64, "comparison_package_digest": "3" * 64,
            "official_authority": dict(evaluator_fixture.AUTHORITY), "rules": [transient.RULE], "campaign_dir": str(campaign),
        }
        path = transient._write(context["evaluation"] / f"builder-r{self.active_revision}-b{baseline}.json", config)
        action = {"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 600.0,
            "command": [sys.executable, str(transient.BUILDER), "--config", str(path), "--output", str(target / "results.json"),
                        "--results-dir", str(target / "machine"), "--evaluation-baseline", str(baseline)], "item_ids": ["assert-rules"]}
        action_plan = transient._write(self.root / "action-plans" / f"revision-r{self.active_revision}-b{baseline}.json",
                                      batch_tests.batch.action_plan([action], phase="VC-5"))
        return self.source_command(["compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-5", "--sequence", sequence,
            "--predecessor-checkpoint", vc.vc_checkpoint_path(campaign, "VC-4", revision=self.active_revision),
            "--action-plan", action_plan, "--state-dir", self.state_dir])

    def failed_builder(self, context: dict[str, Any], sequence: int) -> dict:
        code, failed, stderr = self.dispatch_builder(context, sequence, baseline=0)
        self.assertIsNotNone(failed, stderr)
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
        target = upgrade._stage_write_target(context["campaign"], self.active_candidate, 0, "assertions")
        index = json.loads((target / "evaluation-run.json").read_bytes())
        self.assertEqual((index["candidate_id"], index["candidate_revision"]), (self.active_candidate, self.active_revision))
        self.assertEqual(index["evaluator"]["checker_sha256"], self.defective_evaluator["checker_sha256"])
        self.assertEqual({row["status"] for row in index["rules"]}, {"fail"})
        official = json.loads((target / "machine" / "official" / f"{transient.RULE}.json").read_bytes())
        self.assertEqual(official["status"], "pass", "每次失败必须来自候选侧，官方小写头应由真实 checker 通过")
        code, reconciled, stderr = self.source_command(["reconcile-supervisor-run", "--campaign-dir", context["campaign"],
                                                      "--run-dir", Path(failed["campaign_run"]["run_dir"])])
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        return failed

    def invalidate_first_candidate(self, context: dict[str, Any]) -> dict:
        preview = self.preview(context)
        self.assertEqual(preview["failure_scope"]["jobs"], [transient.FAILED_JOB])
        code, redirect, stderr = self.recover(context, "apply", "--approve-sha256", preview["review_sha256"],
                                            "--root-cause-class", "candidate-source")
        self.assertIsNotNone(redirect, stderr)
        self.assertEqual((code, redirect["status"]), (0, "redirect"), stderr)
        self.assertIn("invalidate-candidate preview", redirect["next_command"])
        argv = ["--campaign-dir", context["campaign"], "--candidate-id", self.active_candidate, "--reviewer", "连续链审核"]
        code, proposed, stderr = self.source_command(["invalidate-candidate", "preview", *argv])
        self.assertIsNotNone(proposed, stderr)
        self.assertEqual((code, proposed["status"]), (0, "preview"), stderr)
        code, applied, stderr = self.source_command(["invalidate-candidate", "apply", *argv, "--approve-sha256", proposed["review_sha256"]])
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"]), (0, "applied"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "revision_required")
        return applied

    def build_second_candidate(self, campaign: Path) -> None:
        code, opened, stderr = self.source_command(["revision-open", "--campaign-dir", campaign, "--candidate-id", SECOND_CANDIDATE,
                                                  "--supersedes", transient.CANDIDATE])
        self.assertIsNotNone(opened, stderr)
        self.assertEqual((code, opened["revision"], opened["store"]["payload"]["revision"]), (0, 2, 2), stderr)
        self.active_candidate, self.active_revision = SECOND_CANDIDATE, 2
        code, preview, stderr = self.source_command(["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", self.active_candidate])
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["candidate_revision"]), (2, 2), stderr)
        fixture = build_fixture.CandidateBuildFixture(self.root / "build-r2", campaign, self.active_candidate, run_main=batch_tests.run_main)
        fixture.build_source(preview=preview, marker="r2：修正版本请求头值")
        fixture.build_artifacts()
        fixture.build_transition()
        fixture.build_implementation_receipt(preview["requirements"], f"{campaign.name}-ledger")
        plan_source = self.root / "plan-source-r2"
        subprocess.run(["git", "clone", "-q", str(fixture.source), str(plan_source)], check=True, capture_output=True, timeout=60)
        lifecycle = fixture.lifecycle_dir.relative_to(fixture.source)
        dispatched_plan = plan_source / lifecycle / "gate-plan-dispatch.json"
        action = batch_tests.batch.managed_action("vc4-plan-r2", "VC-4:plan-candidate-gates",
            ["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", self.active_candidate, "--candidate-source", plan_source,
             "--mapping", plan_source / lifecycle / "gate-mapping.json", "--output", dispatched_plan], item_ids=["vc4-plan-r2"])
        self.assert_batch_passed(self.dispatch(campaign, "VC-4", 10, [action]), 10, "VC-4")
        self.assertEqual(dispatched_plan.read_bytes(), fixture.plan_path.read_bytes())
        action = batch_tests.batch.managed_action("vc4-record-r2", "VC-4:record-candidate-build", fixture.argv(), item_ids=["vc4-record-r2"])
        binary = fixture.install_fake_commands(self.root / "fake-bin-r2")
        with mock.patch.dict(os.environ, {"PATH": f"{binary}{os.pathsep}{os.environ.get('PATH', '')}"}):
            self.assert_batch_passed(self.dispatch(campaign, "VC-4", 11, [action]), 11, "VC-4")
        seal_path = upgrade._candidate_revision_dir(campaign, 2) / "seal.json"
        revision = json.loads(seal_path.read_bytes())
        self.assertEqual(revision["superseded"]["candidate_id"], transient.CANDIDATE)
        self.assertIn("source", revision["changed_layers"])
        previous = json.loads((campaign / "candidates" / transient.CANDIDATE / "build-receipt.json").read_bytes())
        current = json.loads((campaign / "candidates" / SECOND_CANDIDATE / "build-receipt.json").read_bytes())
        self.assertNotEqual(previous["source"]["tree_sha256"], current["source"]["tree_sha256"])
        self.assertTrue(vc.vc_checkpoint_path(campaign, "VC-4", revision=2).is_file())

    def repair_evaluator(self, context: dict[str, Any], attempt_root: Path) -> dict:
        """实际修复副本源码，再按真实变化层办理演进；不替换摘要、身份、批准判定或结果。"""

        self.restore_checker()
        repaired = upgrade._tool_identity(include_git=False)
        digests = identity_policy.evaluator_dependency_digests()
        self.assertNotEqual(digests["checker_sha256"], self.defective_evaluator["checker_sha256"])
        self.assertEqual(repaired["policy_sha256"], self.tool_identity["policy_sha256"])
        self.assertEqual(repaired["wire_producer_sha256"], self.tool_identity["wire_producer_sha256"],
                         "实际策略若把本次解析修复归到 wire，不得绕过身份边界继续本链")
        deployment = ledger_fixture.write_claude_deploy_receipt(self.data, repaired, stamp="20261008t090000z")
        argv = ["tool-evolution", "--campaign-dir", context["campaign"], "--fix-commit", "e" * 40,
                "--reason", "修复请求头大小写归一化，候选证据原字节复用"]
        code, preview, stderr = self.source_command(argv)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (0, "approval_required"), stderr)
        self.assertEqual(preview["impact"]["official"]["affected_job_ids"], [])
        self.assertTrue(all(not value["affected_job_ids"] for value in preview["impact"]["candidates"].values()))
        approved = [*argv, "--approve-sha256", preview["review_sha256"], "--approved-by", "连续链审核"]
        code, evolved, stderr = self.source_command(approved)
        self.assertIsNotNone(evolved, stderr)
        self.assertEqual((code, evolved["status"], evolved["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)
        if repaired["evidence_semantics_sha256"] != self.tool_identity["evidence_semantics_sha256"]:
            code, epoch, stderr = self.source_command(["evaluation-epoch", "--campaign-dir", context["campaign"],
                "--candidate-id", self.active_candidate, "--attempt-id", attempt_root.name, "--reason", "修复头大小写解析后离线重评"])
            self.assertIsNotNone(epoch, stderr)
            self.assertEqual((code, epoch["status"]), (0, "epoch_appended"), stderr)
        preview = self.preview(context)
        self.refuse_wrong_deployments(context, preview, deployment)
        code, applied, stderr = self.recover(context, "apply", "--approve-sha256", preview["review_sha256"],
            "--root-cause-class", "evaluator-defect", "--fix-commit", "e" * 40, "--deployment-receipt", deployment)
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["kind"], applied["evaluation_baseline"]), (0, "evaluator-only", 1), stderr)
        return applied

    def refuse_wrong_deployments(self, context: dict[str, Any], preview: dict, deployment: Path) -> None:
        """复用当前 r2 的真实恢复入口核对两种误用，不重建 Campaign 或用旁账绕过 fixture_only 语义。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import file_digests

        original = json.loads(deployment.read_bytes())
        codex = {"schema_version": "codex-arm64-supervisor-enable/v1", "status": "passed",
                 "created_at_utc": original["created_at_utc"], **original["identity"]}
        missing_client = {key: value for key, value in original.items() if key != "client"}
        self.wrong_deployment_refusals = []
        for label, document in (("codex", codex), ("missing-client", missing_client)):
            path = transient._write(self.root / "wrong-deployment-inputs" / f"{label}.json", document)
            campaign = context["campaign"]
            write_roots = [campaign, candidate_store.campaign_store_root(campaign), self.ledger_dir, self.project_root]
            before_files = file_digests(*write_roots)
            before_head = project_ledger.replay_head(self.project_root)
            calls = len(self.runtime.candidate_calls())
            code, _result, stderr = self.recover(context, "apply", "--approve-sha256", preview["review_sha256"],
                "--root-cause-class", "evaluator-defect", "--fix-commit", "e" * 40, "--deployment-receipt", path)
            self.assertEqual(code, 1, stderr)
            self.assertIn("不是 Claude 受管部署收据", stderr)
            self.assertEqual(file_digests(*write_roots), before_files, f"{label} 收据必须在任何恢复写入前拒绝")
            self.assertEqual(project_ledger.replay_head(self.project_root), before_head, f"{label} 误用不得修改原项目总账")
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            self.assertFalse((campaign / "candidates" / self.active_candidate / "revisions" / "b1" / upgrade.EVALUATION_BASELINE_COMMIT_FILENAME).exists())
            self.wrong_deployment_refusals.append({"case": label, "stderr": stderr})

    def run_revision_evaluator_chain(self) -> dict[str, Any]:
        """从真实 r1 失败一直进入真实 r2／b1；返回同一世界，供后续完整 A 组继续接续。"""

        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        manifest = upgrade.load_campaign_manifest(campaign)
        assertion_profile.freeze_approved_assertion_profile(campaign, manifest)
        with client_fixture.source_scope():
            first_root, first_attempt, first_stage = self.capture_and_seal(campaign)
            context = self.evaluation_context(campaign)
            self.failed_builder(context, 9)
            first_snapshot = self.boundary([first_root, campaign / "candidates" / transient.CANDIDATE / "seal",
                                           upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "capture-candidate"),
                                           upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "assertions"),
                                           *[Path(root) for row in first_attempt["results"] for root in row["evidence_roots"]]])
            invalidated = self.invalidate_first_candidate(context)
            self.build_second_candidate(campaign)
            transient._write(self.relay_control, {"bad_probe": None, "header_name": "Anthropic-Version"})
            second_root, second_attempt, second_stage = self.capture_and_seal(campaign)
            self.assertEqual(self.boundary([Path(path) for path in first_snapshot]), first_snapshot)
            self.assertEqual(second_attempt["identity"]["candidate_revision"], 2)
            jobs = upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=SECOND_CANDIDATE)
            self.assertCountEqual([row["id"] for row in second_attempt["results"]], [job.job_id for job in jobs])
            self.assertTrue(all(row["disposition"] == "executed" for row in second_attempt["results"]))
            self.assertNotEqual(first_stage["store"]["validation_attempt_ref"], second_stage["store"]["validation_attempt_ref"])
            self.failed_builder(context, 12)
            # r2 的值已正确；缺陷必须来自实际 checker，而非改写证据或伪造结果。
            roots = [Path(root) for row in second_attempt["results"] for root in row["evidence_roots"]]
            snapshot = self.boundary(roots)
            correct_headers = 0
            for root in roots:
                for binary in root.glob("*/relay/*.client_to_upstream.bin"):
                    raw = binary.read_bytes()
                    self.assertNotIn(b"Anthropic-Version: 2023-01-01", raw)
                    correct_headers += raw.count(b"Anthropic-Version: 2023-06-01")
            self.assertGreater(correct_headers, 0, "r2 必须实际产生正确值、混合大小写的请求头字节")
            calls = len(self.runtime.candidate_calls())
            before = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
            project_before = project_ledger.replay_head(self.project_root)["precise_total"]
            recovered = self.repair_evaluator(context, second_root)
            code, passed, stderr = self.dispatch_builder(context, 13, baseline=1)
            self.assertIsNotNone(passed, stderr)
            self.assertEqual((code, passed["campaign_run"]["reason"]), (0, "queue-complete"), stderr)
            index_path = upgrade._stage_write_target(campaign, SECOND_CANDIDATE, 1, "assertions") / "evaluation-run.json"
            index = json.loads(index_path.read_bytes())
            self.assertEqual(index["evaluation_baseline"], 1)
            self.assertEqual(index["evaluator"]["checker_sha256"], identity_policy.evaluator_dependency_digests()["checker_sha256"])
            self.assertEqual({row["status"] for row in index["rules"]}, {"pass"})
            self.assertEqual(self.boundary(roots), snapshot)
            self.assertEqual(self.boundary([Path(path) for path in first_snapshot]), first_snapshot)
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            after = provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)
            self.assertEqual((before["status"], after["status"]), ("complete", "complete"))
            self.assertEqual(after["precise_total"], before["precise_total"])
            self.assertEqual(project_ledger.replay_head(self.project_root)["precise_total"], project_before)
            self.assert_campaign_request_accounting(campaign, expected_client_requests=4)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["current_revision"], 2)
            store, store_campaign = candidate_store.open_campaign_store(campaign, manifest)
            return {**context, "manifest": manifest, "candidate_id": SECOND_CANDIDATE, "candidate_revision": 2,
                    "evaluation_baseline": 1, "attempt_root": second_root, "attempt": second_attempt, "stage": second_stage,
                    "store": store, "store_campaign": store_campaign, "r1_invalidation": invalidated, "evaluation_recovery": recovered,
                    "wrong_deployment_refusals": list(self.wrong_deployment_refusals),
                    "evaluation_index": index_path, "capture_calls": calls, "precise_total": after["precise_total"]}
