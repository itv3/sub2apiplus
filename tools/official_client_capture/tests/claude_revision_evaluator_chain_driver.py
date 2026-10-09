"""Claude 后段链的候选 r2 与评估器修复驱动，尚未登记完整 A 组认证。

同一 Campaign 真实执行：r1 候选 run/seal、失败 builder、candidate-source 裁定与作废、r2 新源码构建和
run/seal、再次失败 builder、实际 checker 源码修复、工具演进、evaluation epoch、b1 恢复和 builder。
候选来源沿用 transient 驱动的离线宿主／Docker／网关及中继字节来源；不替换阶段结果、Store 或断言结论。

checker 缺陷只允许写在通过 shell._assert_isolation 的副本树：请求解析器误把版本头保留原大小写。
r1 的小写版本头带错误值，定位候选源码问题；r2 的值正确但使用混合大小写，从而暴露 checker 缺陷。
修复还原解析器源码，所有身份由实际策略重算，不替换摘要，也不直接写入 passed。演进合同拒绝时直接失败。

修复后先在 evaluator-defect apply 的强制部署收据门拒绝 Codex 收据和缺 client 收据，原 Campaign 与两本账不得写入。
本链使用首次冻结的全部批准规则 ID 和控制面画像接缝，真实 compare 生成 builder 配置，后继接严格 accept 与 VC-6。
部署收据、官方来源与构建实物来自明示夹具；覆盖控制面连续性，不等于全部规则的真实客户端语义认证。
是否登记第三条链必须在完整隔离实测后按设计的 380 秒判据决定。
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
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_store as candidate_store
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_boundary_receipt_fixture as boundary_fixture
from tools.official_client_capture.tests import claude_candidate_build_fixture as build_fixture
from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import claude_transient_chain_driver as transient
from tools.official_client_capture.tests import claude_fault_chain_world as world_fixture
from tools.official_client_capture.tests import claude_vc5_chain_fixture as vc5_sources
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
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
        self.runtime.activation_fact = vc5_sources.activation_fact(self.build, capture_tests.TARGET)

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

    def evaluation_context(self, campaign: Path) -> dict[str, Any]:
        """首个评估前补齐官方夹具原件，供同一世界的严格验收和 typed 交付实读。

        父类来源仍只表示离线控制面夹具。这里沿用其请求、响应和包引用，
        物化三份父清单及完整下层字节；不修改旧来源配置、官方 Store 包或
        候选阶段结果。新配置在第一次 compare 前写出，后续 r2／b1 共用。
        """

        context = super().evaluation_context(campaign)
        original_config = json.loads(context["config_path"].read_bytes())
        self.assertNotIn("attempt", original_config, "候选上下文必须由正式封存读侧读取")
        self.assertNotIn("attempt_root", original_config, "不能用来源配置替换候选 attempt")
        official = upgrade._load_stage_result(campaign, "capture-official")
        source = vc5_sources.materialize_official_source(
            campaign=campaign, data_root=self.data, side=context["official"],
            package_ref=official["store"]["evidence_package_ref"],
        )
        source_path = transient._write(context["evaluation"] / "materialized-official-source.json", source)
        config = {**original_config, "official_source": str(source_path),
                  "gate_environment_mode": vc5_sources.REAL_GATE_ENVIRONMENT}
        context["config_path"] = transient._write(context["evaluation"] / "materialized-source-config.json", config)
        return context

    def late_audit(self, name: str, world: dict[str, Any]) -> Any:
        """每次六项审计都保留新物化来源；刷新当前候选不能漏掉这些可信原件。"""

        config = json.loads(world["config_path"].read_bytes())
        roots = [*world.get("trusted_roots", []), world["config_path"],
                 Path(config["official_source"]), world["campaign"] / "seam" / "official"]
        audit_world = {**world, "trusted_roots": list(dict.fromkeys(roots))}
        return super().late_audit(name, audit_world)

    def gate_receipt(self, world: dict[str, Any], name: str, *, failed_gate: str | None = None) -> str:
        """当前 revision 的门禁输入采用可独立重放的环境合同；原 reader 不打桩。

        门禁命令的结果仍是明示测试输入。宿主与架构取真实封存执行计划，
        环境原件经原 finalizer 形成，不能把旧最小替身送入 typed 交付。
        """

        config = json.loads(world["config_path"].read_bytes())
        self.assertEqual(vc5_sources.gate_environment_mode(config), vc5_sources.REAL_GATE_ENVIRONMENT,
                         "revision 连续链必须实读门禁环境原件")
        self.assertTrue(world.get("real_candidate"), "门禁必须绑定真实 run／seal 的候选世界")
        self.assertEqual(world["candidate_id"], self.active_candidate)
        with vc5_sources.seams(world["config_path"]):
            identity = compare.candidate_gate_identity(world["campaign"], world["manifest"],
                                                     world["candidate_id"], world["build"])
            subject = vc5_sources.expected_subject(world["manifest"], world["candidate_payload"], identity)
            reference = world["stage"]["store"]["validation_execution_plan_ref"]
            execution = world["store"].load_object(reference)["payload"]
            coordinates = {(gate["host_id"], gate["architecture"]) for gate in execution["external_gates"]}
            self.assertEqual(len(coordinates), 1, "本夹具只支持同一执行宿主与架构的门禁集合")
            host, architecture = next(iter(coordinates))
            return vc5_sources.gate_receipt_file(world["gate_root"], name=name, subject=subject,
                host=host, architecture=architecture, failed_gate=failed_gate,
                environment_mode=vc5_sources.REAL_GATE_ENVIRONMENT)

    def recover(self, context: dict[str, Any], action: str, *extra: Any) -> tuple[int, dict | None, str]:
        return self.source_command(["evaluation-recover", action, "--campaign-dir", context["campaign"],
            "--candidate-id", self.active_candidate, "--reviewer", "连续链审核", *extra])

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """先刷新当前 revision 的真实候选，再走父类的 compare 与正式 builder 配置。"""

        world_fixture.refresh_world(self, context, candidate_id=self.active_candidate, revision=self.active_revision)
        return super().dispatch_builder(context, sequence, baseline=baseline)

    def failed_builder(self, context: dict[str, Any], sequence: int) -> dict:
        counts_before = project_ledger.replay_head(self.project_root)["root_cause_counts"]
        code, failed, stderr = self.dispatch_builder(context, sequence, baseline=0)
        self.assertIsNotNone(failed, stderr)
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
        target = upgrade._stage_write_target(context["campaign"], self.active_candidate, 0, "assertions")
        index = json.loads((target / "evaluation-run.json").read_bytes())
        self.assertEqual((index["candidate_id"], index["candidate_revision"]), (self.active_candidate, self.active_revision))
        self.assertEqual(index["evaluator"]["checker_sha256"], self.defective_evaluator["checker_sha256"])
        self.assertEqual({row["status"] for row in index["rules"]}, {"fail"})
        # 全规则接缝仍核对每条双侧规则的官方文档；不能只挑原来的 HDR001 忽略其余批准规则。
        for rule in index['rules']:
            if rule['validation_mode'] == 'dual_wire':
                official = json.loads((target / 'machine' / 'official' / f"{rule['rule']}.json").read_bytes())
                self.assertEqual(official['status'], 'pass', '失败必须来自候选侧，官方小写头由真实 checker 通过')
        self.failed_evaluation_run = Path(failed["campaign_run"]["run_dir"])
        code, reconciled, stderr = self.source_command(["reconcile-supervisor-run", "--campaign-dir", context["campaign"],
                                                      "--run-dir", self.failed_evaluation_run])
        self.assertIsNotNone(reconciled, stderr)
        # r1、r2 的相同断言失败仍按原根因合同累计；r2 必须暂停，不能绕过上限。
        # 对账本身也会登记根因，因此在它完成后收集待修复集合，保留所有历史计数。
        head_after = project_ledger.replay_head(self.project_root)
        counts_after = head_after['root_cause_counts']
        causes = {row['root_cause_id'] for row in reconciled.get('root_causes', [reconciled['root_cause']])}
        previous_causes = getattr(self, 'assertion_failure_causes', set())
        self.assertTrue(causes)
        self.assertTrue(all(counts_after[cause] > counts_before.get(cause, 0) for cause in causes))
        self.assertion_failure_causes = previous_causes | causes
        self.assertIn(self.active_revision, (1, 2))
        expected = (0, "recoverable") if self.active_revision == 1 else (3, "paused")
        self.assertEqual((code, reconciled["status"]), expected, stderr)
        if self.active_revision == 2:
            self.assertEqual(reconciled["decision"]["pause_kinds"], ["root_cause_repair"])
            self.assertIsNone(reconciled["decision"]["terminal_reason"])
            self.assertTrue(any(reconciled["decision"]["root_cause_counts"][cause] >= project_ledger.DEFAULT_RETRY_LIMIT
                                for cause in causes & previous_causes))
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "recovery_required")
            self.assertNotIn(context["campaign"].name, head_after["terminal_campaigns"])
            self.assertFalse(any(event.get("event_id") == f"reconcile-run-passed-{reconciled['run_id']}"
                                 for event, _raw in timing._load_events(self.ledger_dir)))
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

    def build_second_candidate(self, campaign: Path, context: dict[str, Any]) -> None:
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
        plan_sequence = world_fixture.take_sequence(context)
        self.assert_batch_passed(self.dispatch(campaign, "VC-4", plan_sequence, [action]), plan_sequence, "VC-4")
        self.assertEqual(dispatched_plan.read_bytes(), fixture.plan_path.read_bytes())
        action = batch_tests.batch.managed_action("vc4-record-r2", "VC-4:record-candidate-build", fixture.argv(), item_ids=["vc4-record-r2"])
        record_sequence = world_fixture.take_sequence(context)
        binary = fixture.install_fake_commands(self.root / "fake-bin-r2")
        with mock.patch.dict(os.environ, {"PATH": f"{binary}{os.pathsep}{os.environ.get('PATH', '')}"}):
            self.assert_batch_passed(self.dispatch(campaign, "VC-4", record_sequence, [action]), record_sequence, "VC-4")
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
        # 暂停不会生成恢复放行收据。真实 checker 回归与修复登记完成后，
        # 必须重放原失败 run 的对账，使账本回到 active，再进入评估恢复批准。
        self.release_repaired_assertion_roots(context, deployment)
        code, reconciled, stderr = self.source_command(["reconcile-supervisor-run", "--campaign-dir", context["campaign"],
                                                      "--run-dir", self.failed_evaluation_run])
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
        repaired_counts = project_ledger.replay_head(self.project_root)["root_cause_counts"]
        self.assertTrue(all(repaired_counts.get(cause) == 0 for cause in self.assertion_failure_causes))
        passed = [event for event, _raw in timing._load_events(self.ledger_dir)
                  if event.get("event_id") == f"reconcile-run-passed-{reconciled['run_id']}"]
        self.assertEqual([event["event_type"] for event in passed], ["receipt_passed"])
        preview = self.preview(context)
        self.refuse_wrong_deployments(context, preview, deployment)
        # 错误部署收据必须零写入；批准草案仍绑定修复与重新对账后的当前 head。
        preview = self.preview(context)
        code, applied, stderr = self.recover(context, "apply", "--approve-sha256", preview["review_sha256"],
            "--root-cause-class", "evaluator-defect", "--fix-commit", "e" * 40, "--deployment-receipt", deployment)
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["kind"], applied["evaluation_baseline"]), (0, "evaluator-only", 1), stderr)
        self.tool_identity = repaired
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
            audit = self.late_audit(f"评估器修复拒绝错误部署收据：{label}", context)
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
            self.wrong_deployment_refusals.append({'case': label, 'stderr': stderr})
            audit.check_trusted()
            audit.check_closure(executed=[], expected=[], what='错误部署收据在恢复写入前拒绝')
            audit.check_accounting()
            audit.check_elapsed_untouched(self.ledger_dir, self.project_root)
            audit.check_history()
            # 使用真实状态入口输出的恢复命令，不由测试拼一条命令冒充产品提示。
            with self.source_context(), vc5_sources.seams(context['config_path']):
                recovery_status = self.candidate_status(context)
            audit.check_entry(recovery_status, 'evaluation-recover')
            context['records'].append(audit.report(campaign_id=campaign.name, candidate_id=self.active_candidate,
                candidate_revision=self.active_revision, evaluation_baseline=0, refusal=label))

    def release_repaired_assertion_roots(self, context: dict[str, Any], deployment: Path) -> None:
        """修复后先用真实 checker 验证当前字节，再按正式根因修复入口放行，不能调高冻结上限。"""

        with self.source_context():
            stage = upgrade._load_stage_result(context['campaign'], 'capture-candidate', self.active_candidate, _baseline=0)
        candidate = compare.candidate_evaluation_source(context['campaign'], context['manifest'], stage)
        profile_document = json.loads(context['profile_path'].read_bytes())
        regression = self.root / 'checker-restoration-regression.json'
        command = assertion.build_assertion_command(rule_id=profile_document['rules'][0]['rule_id'],
            capture_manifest=str(context['campaign'] / stage['assertion_context']['evaluation_manifest']['path']),
            evidence_root=candidate['evidence_root'], output=str(regression), profile=str(context['profile_path']),
            rule_manifest=str(context['rules_path']), expected_claude_version=capture_tests.TARGET,
            expected_profile_sha256=context['profile_sha256'], side='candidate')
        checked = subprocess.run(command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(json.loads(regression.read_bytes())['status'], 'pass')
        previous_counts = project_ledger.replay_head(self.project_root)["root_cause_counts"]
        for cause in sorted(self.assertion_failure_causes):
            repaired = subprocess.run([sys.executable, '-B', '-m', 'tools.official_client_capture.codex_upgrade_project_ledger',
                'record-root-cause-repair', '--ledger-dir', str(self.project_root), '--root-cause-id', cause, '--kind', 'code',
                '--binding', f"fix_commit_sha={'e' * 40}", '--binding', f'regression_receipt_sha256={upgrade.file_sha256(regression)}',
                '--binding', f'deployment_receipt_sha256={upgrade.file_sha256(deployment)}', '--note', '实际 checker 修复并验证 r2 原字节后放行'],
                cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        repaired_counts = project_ledger.replay_head(self.project_root)["root_cause_counts"]
        self.assertEqual({cause: count for cause, count in repaired_counts.items() if cause not in self.assertion_failure_causes},
                         {cause: count for cause, count in previous_counts.items() if cause not in self.assertion_failure_causes},
                         "本次评估修复不得清除无关根因的计数")

    def run_revision_evaluator_chain(self) -> dict[str, Any]:
        """从真实 r1 失败一直进入真实 r2／b1；返回同一世界，供后续完整 A 组继续接续。"""

        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        manifest = upgrade.load_campaign_manifest(campaign)
        world_fixture.freeze_control_profile(campaign)
        with client_fixture.source_scope():
            first_root, first_attempt, first_stage = self.capture_and_seal(campaign)
            context = self.evaluation_context(campaign)
            source_audit = self.late_audit("候选源码失败与 r2 修订接续", context)
            self.failed_builder(context, context["next_sequence"])
            first_snapshot = self.boundary([first_root, campaign / "candidates" / transient.CANDIDATE / "seal",
                                           upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "capture-candidate"),
                                           upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "assertions"),
                                           *[Path(root) for row in first_attempt["results"] for root in row["evidence_roots"]]])
            invalidated = self.invalidate_first_candidate(context)
            self.build_second_candidate(campaign, context)
            transient._write(self.relay_control, {"bad_probe": None, "header_name": "Anthropic-Version"})
            second_root, second_attempt, second_stage = self.capture_and_seal(campaign)
            self.assertEqual(self.boundary([Path(path) for path in first_snapshot]), first_snapshot)
            self.assertEqual(second_attempt["identity"]["candidate_revision"], 2)
            jobs = upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=SECOND_CANDIDATE)
            self.assertCountEqual([row["id"] for row in second_attempt["results"]], [job.job_id for job in jobs])
            self.assertTrue(all(row["disposition"] == "executed" for row in second_attempt["results"]))
            self.assertNotEqual(first_stage["store"]["validation_attempt_ref"], second_stage["store"]["validation_attempt_ref"])
            world_fixture.refresh_world(self, context, candidate_id=SECOND_CANDIDATE, revision=2)
            source_audit.check_trusted()
            source_audit.check_closure(executed=[row['id'] for row in second_attempt['results']], expected=[job.job_id for job in jobs],
                what='源码改变后 r2 的完整候选 Job')
            r2_probes = [probe for row in second_attempt['results'] for root in row['evidence_roots']
                         for probe in Path(root).iterdir() if (probe / 'relay-manifest.json').is_file()]
            r2_provenance = provenance.collect_root_provenance(r2_probes)
            self.assertEqual(r2_provenance['unresolved_roots'], [])
            # 原件逐请求重放给出期望：r2 CLI 请求加两个新增 Mac 请求，不能由总账增量反推期望。
            source_audit.check_accounting(precise_delta=r2_provenance['precise_total'] + 2)
            source_audit.check_elapsed(reopened_phases=['VC-4', 'VC-5'])
            source_audit.check_history()
            source_audit.check_entry(invalidated, 'revision-open')
            context['records'].append(source_audit.report(campaign_id=campaign.name, candidate_id=SECOND_CANDIDATE,
                candidate_revision=2, evaluation_baseline=0))
            evaluator_audit = self.late_audit('评估器真实修复与 b1 零采集重评', context)
            self.failed_builder(context, context['next_sequence'])
            counted = evaluator_audit.accounting_state()
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
            code, passed, stderr = self.dispatch_builder(context, context["next_sequence"], baseline=1)
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
            evaluator_audit.check_trusted()
            evaluator_audit.check_closure(executed=[row['rule'] for row in index['rules']],
                expected=list(upgrade._approved_rules(campaign, manifest, require_approved=True)), what='修复后按全部批准规则离线重评')
            evaluator_audit.check_accounting()
            # b1 的批准合法重开 VC-5 一次；仍核累计时间、原截止和历史事件不变。
            self.assertEqual(recovered['ledger_event']['event_type'], 'evaluation_baseline')
            self.assertEqual(evaluator_audit.totals()['timing']['stage_started_counts']['VC-5'],
                             evaluator_audit.totals_before['timing']['stage_started_counts']['VC-5'] + 1)
            evaluator_audit.check_elapsed(reopened_phases=['VC-5'])
            evaluator_audit.check_history()
            evaluator_audit.check_entry(recovered, 'compile-and-run-vc-batch', 'VC-5')
            context['records'].append(evaluator_audit.report(campaign_id=campaign.name, candidate_id=SECOND_CANDIDATE,
                candidate_revision=2, evaluation_baseline=1, checker_sha256=index['evaluator']['checker_sha256'],
                root_cause_state_before_repair=counted['project']['root_cause_counts']))
            world_fixture.refresh_world(self, context)
            context.update(expected_client_requests=4, r1_invalidation=invalidated, evaluation_recovery=recovered,
                wrong_deployment_refusals=list(self.wrong_deployment_refusals), evaluation_index=index_path,
                capture_calls=calls, precise_total=after['precise_total'])
            return context

    def run_revision_evaluator_delivery_chain(self) -> dict[str, Any]:
        """备选第三条连续链：同 Campaign 的 r2／b1 严格验收与 VC-6；实测前不注册认证。"""

        with client_fixture.source_scope():
            world = self.run_revision_evaluator_chain()
            world_fixture.strict_accept(self, world)
            world['records'].extend(self.exercise_delivery_faults(world, sequence_start=world['next_sequence']))
            self.assertEqual({row['campaign_id'] for row in world['records']}, {world['campaign'].name})
            return world
