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
from tools.official_client_capture import claude_build_rule_assertion_results as assertion_builder
from tools.official_client_capture import claude_tool_identity_policy as identity_policy
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import claude_upgrade_store as candidate_store
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
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
        if os.environ.get(world_fixture.METHOD_RESUME_ENV):
            if not world_fixture.restore_case(self):
                raise ValueError("revision 原方法缺少评估器与候选值字段，禁止重新预埋缺陷")
            self.assertEqual(self.checker_path, Path(assertion.__file__).resolve())
            original = world_fixture.load_state("revision-checker")
            if original is None:
                raise ValueError("revision 原方法缺少评估器原字节绑定")
            world_fixture.read_cut(original["binding"])
            super().setUp()
            saved = world_fixture.resume_checkpoint(self, "revision-setup")
            if saved is None or saved.get("phase") != "ready":
                raise ValueError("revision 原方法没有完整运行来源停点")
            return
        self.checker_path = Path(assertion.__file__).resolve()
        self.assertTrue(self.checker_path.is_relative_to(REPOSITORY_ROOT))
        self.fixed_checker = self.checker_path.read_bytes()
        source = self.fixed_checker.decode("utf-8")
        self.assertEqual(source.count(REQUEST_HEADER_LINE), 2, "请求和响应各有一处归一化；只改第一处请求解析")
        self.checker_path.write_text(source.replace(REQUEST_HEADER_LINE, DEFECTIVE_HEADER_LINE, 1), encoding="utf-8")
        # 原记录世界在失败后保留实际源码；普通单元测试仍按原行为清理。
        if world_fixture.record_root() is None:
            self.addCleanup(self.restore_checker)
        world_fixture.save_state("revision-checker", {"binding": world_fixture.file_cut(self.checker_path)})
        self.tool_identity = upgrade._tool_identity(include_git=False)
        self.defective_evaluator = identity_policy.evaluator_dependency_digests()
        self.active_candidate, self.active_revision = transient.CANDIDATE, 1
        super().setUp()
        transient._write(self.relay_control, {"bad_probe": transient.FAILED_PROBE, "header_name": "anthropic-version"})
        world_fixture.checkpoint(self, "revision-setup", phase="ready")
        world_fixture.checkpoint(self, "revision-chain", phase="unstarted", values={})
        world_fixture.checkpoint(self, "revision-delivery-chain", phase="unstarted")

    def restore_checker(self) -> None:
        """修复和清理都再验隔离边界；只能还原当前副本里预先保留的原始字节。"""

        self.require_isolated_copy()
        self.assertEqual(self.checker_path, Path(assertion.__file__).resolve())
        if self.checker_path.read_bytes() != self.fixed_checker:
            self.checker_path.write_bytes(self.fixed_checker)
        world_fixture.save_state("revision-checker", {"binding": world_fixture.file_cut(self.checker_path)})

    def process_step(self, cursor: str, key: str, argv: list[str], campaign: Path,
                     *, timeout: int = 120) -> tuple[int, Any, str]:
        """记录原子进程的完整退出事实；统一承接接缝复核原日志后才返回。"""

        def invoke() -> tuple[int, Any, str]:
            pending = world_fixture.begin_command(argv)
            try:
                completed = subprocess.run(argv, cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                    timeout=timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as error:
                stdout, stderr = error.stdout or "", error.stderr or ""
                world_fixture.record_command(argv, None,
                    stdout.decode() if isinstance(stdout, bytes) else stdout,
                    stderr.decode() if isinstance(stderr, bytes) else stderr, pending=pending)
                raise
            world_fixture.record_command(argv, completed.returncode, completed.stdout, completed.stderr, pending=pending)
            try:
                result = json.loads(completed.stdout) if completed.stdout.strip() else None
            except json.JSONDecodeError:
                result = None
            return completed.returncode, result, completed.stderr

        return world_fixture.command_step(self, cursor, key, argv, invoke, campaign=campaign)

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
        cursor = f"candidate-seal:{self.active_candidate}:{attempt_id}:b0"
        code, preview, stderr = self.source_step(cursor, "preview", argv, campaign)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        code, result, stderr = self.source_step(cursor, "apply",
            [*argv, "--approve-seal-sha256", preview["review_sha256"], "--approved-by", "连续链审核"], campaign)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (0, "complete"), stderr)
        return result

    def capture_and_seal(self, campaign: Path) -> tuple[Path, dict, dict]:
        """当前候选真实取证只执行一次；seal 和入账分别承接完整原命令输出。"""

        cursor = f"revision-capture:{self.active_candidate}"
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            self.prepare_runtime(campaign)
            saved = {"phase": "capture", "campaign": campaign}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved['phase'] == 'capture':
            code, captured, stderr = self.source_step(cursor, 'run', ["capture-candidate", "run", "--campaign-dir", campaign,
                "--candidate-id", self.active_candidate, "--acknowledge-live-requests"], campaign)
            self.assertIsNotNone(captured, stderr)
            self.assertEqual((code, captured["status"]), (2, "awaiting_receipts"), stderr)
            with self.source_context():
                attempt_root, attempt = upgrade._load_capture_attempt(campaign, "candidate", self.active_candidate, captured["attempt_id"])
            saved = {**saved, 'phase': 'seal', 'attempt_root': attempt_root, 'attempt': attempt}
            world_fixture.checkpoint(self, cursor, **saved)
        attempt_root, attempt = saved['attempt_root'], saved['attempt']
        if saved['phase'] == 'seal':
            self.seal(campaign, attempt_root.name, clients=self.client_inputs(campaign, attempt_root, attempt))
            saved = {**saved, 'phase': 'account'}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved['phase'] == 'account':
            code, accounted, stderr = self.source_step(cursor, 'account',
                ["account-sealed-candidate", "--campaign-dir", campaign, "--candidate-id", self.active_candidate], campaign)
            self.assertEqual(code, 0, stderr)
            saved = {**saved, 'phase': 'complete'}
            world_fixture.checkpoint(self, cursor, **saved)
        with self.source_context():
            current_root, current_attempt = upgrade._load_capture_attempt(campaign, "candidate", self.active_candidate, attempt_root.name)
            stage = upgrade._load_stage_result(campaign, "capture-candidate", self.active_candidate, _baseline=0)
        self.assertEqual((current_root, current_attempt), (attempt_root, attempt))
        self.assertEqual(stage["attempt"]["path"], (attempt_root / "attempt.json").relative_to(campaign).as_posix())
        return attempt_root, attempt, stage

    def revision_candidate_jobs(self, campaign: Path, manifest: dict[str, Any]) -> list[upgrade.Job]:
        """Job 不进停点；恢复时由原读侧重验冻结计划及当前 r2，再展开实际定义。"""

        with self.source_context():
            current_manifest = upgrade.load_campaign_manifest(campaign)
            self.assertEqual(current_manifest, manifest, "revision 原链的冻结 Campaign 不得被替换")
            current = compare.require_current_candidate(campaign, current_manifest, SECOND_CANDIDATE,
                                                        action="revision 原链候选 Job 闭合")
            self.assertEqual(current["revision"], 2, "候选 Job 必须属于当前 r2")
            return upgrade._campaign_jobs(campaign, current_manifest, "candidate", candidate_id=SECOND_CANDIDATE)

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

    def _interrupted_builder_source(self, context: dict[str, Any], sequence: int,
                                    action: dict[str, Any]) -> dict[str, Any]:
        """绑定已提交的原 builder 与完整 pass 前缀；不生成旧调用的退出码或成功输出。"""

        campaign = context["campaign"]
        commit_path = campaign / "control/vc/commits" / f"{sequence:04d}-vc-5.json"
        commit = supervisor._read_vc_commit(commit_path)
        run = Path(commit["parent_run_dir"])
        state = supervisor._read_state(run)
        monitor_pid = state.get("monitor_pid")
        if (state["state"] != "running" or isinstance(monitor_pid, bool) or not isinstance(monitor_pid, int)
                or monitor_pid <= 0 or supervisor._owner_alive(state["owner_pid"]) or supervisor._owner_alive(monitor_pid)):
            raise ValueError("原中断 builder 必须是 owner 与 monitor 均已死亡的 running 父 run")
        record = supervisor._read_json(run / "campaign-run-manifest.json")
        # 原编译器已为 builder 冻结输出声明；按同一规则规范化输入，再完整比较全部动作字段。
        expected_actions = upgrade._freeze_evaluation_output_bindings(campaign, SECOND_CANDIDATE, 1, [action])
        inner = record.get("manifest")
        if (not isinstance(inner, dict) or record.get("manifest_sha256") != supervisor._sha256(supervisor._canonical(inner))
                or inner.get("candidate_id") != SECOND_CANDIDATE or inner.get("candidate_revision") != 2
                or inner.get("evaluation_baseline") != 1 or inner.get("actions") != expected_actions
                or inner.get("batch_sequence") != sequence or inner.get("phase") != "VC-5"
                or inner.get("campaign_id") != campaign.name or state.get("campaign_id") != campaign.name
                or run.parent != self.state_dir or run.name != "run-" + state["owner_nonce"]
                or supervisor._staging_commit_for_run(campaign, state, inner, run) != commit):
            raise ValueError("原中断 builder 的 COMMIT／动作／候选／b1 绑定不闭合")
        root = upgrade._stage_write_target(campaign, SECOND_CANDIDATE, 1, "assertions")
        if (root / "evaluation-run.json").exists() or (root / "results.json").exists():
            raise ValueError("原中断 builder 已有最终输出，不能走无输出中断恢复")
        chain = assertion_builder.EvaluationRun._load_chain(root / assertion_builder.CHECKPOINTS_DIRNAME)
        if not chain:
            raise ValueError("原中断 builder 没有可保留的完整检查点前缀")
        bindings = [world_fixture.file_cut(commit_path), world_fixture.file_cut(run / "campaign-run-manifest.json")]
        for checkpoint in chain:
            if (checkpoint["status"] != "pass" or checkpoint["executed_by"]["run_dir"] != str(run)
                    or checkpoint["executed_by"]["owner_nonce"] != state["owner_nonce"]
                    or checkpoint["executed_by"]["run_manifest_sha256"] != record["manifest_sha256"]):
                raise ValueError("原中断 builder 检查点不属于本 run 的完整 pass 前缀")
            checkpoint_path = root / assertion_builder.CHECKPOINTS_DIRNAME / (
                f"{checkpoint['sequence']:04d}-{checkpoint['rule']}-{checkpoint['side']}.json")
            bindings.append(world_fixture.file_cut(checkpoint_path))
            for field in ("document", "input_projection"):
                path = campaign / checkpoint[field]["path"]
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("原中断 builder 的检查点引用超出 b1 目录")
                binding = world_fixture.file_cut(path)
                if binding["sha256"] != checkpoint[field]["sha256"]:
                    raise ValueError("原中断 builder 的检查点文档／投影摘要不一致")
                bindings.append(binding)
        return {"run_dir": run, "commit_sha256": commit["commit_sha256"],
                "checkpoint_count": len(chain), "checkpoint_head_sha256": chain[-1]["checkpoint_sha256"],
                "bindings": bindings}

    def vc_step(self, cursor: str, key: str, context: dict[str, Any], phase: str,
                sequence: int, action: dict[str, Any]) -> tuple[int, dict | None, str]:
        """revision 的 b1 builder 双死亡只走正规对账与 N+1；其他命令仍用原承接接缝。"""

        logical = "command-step:" + cursor + ":" + key
        pending = world_fixture.load_state(logical)
        recovery_name = "interrupted-builder:" + cursor
        recovery = world_fixture.load_state(recovery_name)
        if recovery is None and (pending is None or pending.get("status") != "pending"):
            return super().vc_step(cursor, key, context, phase, sequence, action)
        if (key != "builder" or phase != "VC-5" or context.get("candidate_id") != SECOND_CANDIDATE
                or context.get("revision") != 2 or context.get("evaluation_baseline") != 1
                or action.get("action_id") != "assert-rules"):
            return super().vc_step(cursor, key, context, phase, sequence, action)
        if not os.environ.get(world_fixture.METHOD_RESUME_ENV):
            raise ValueError("原中断 builder 只能从原方法恢复入口进入")
        campaign = context["campaign"]
        continuation = world_fixture.require_registered_control_continuation(campaign)
        if (set(continuation) != {"mode", "campaign", "binding"}
                or continuation["mode"] != "registered-control-evolution"
                or continuation["campaign"] != str(campaign.resolve())):
            raise ValueError("原中断 builder 的正式 control 演进来源凭证不属于本 Campaign")
        argv = [str(value) for value in ("compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", phase,
            "--sequence", sequence, "--predecessor-checkpoint", vc.vc_checkpoint_path(campaign, "VC-4", revision=2),
            "--action-plan", self.root / "action-plans" / f"vc-5-{sequence:04d}.json", "--state-dir", self.state_dir)]
        registry = (world_fixture.load_state("command-step-keys") or {}).get("keys", {})
        if (pending is None or pending.get("status") != "pending" or pending.get("argv") != argv
                or pending.get("campaign") != campaign or registry.get(logical) != {"argv": argv, "campaign": campaign}):
            raise ValueError("原中断 builder 的 pending／登记清单／参数不一致")
        prefix = pending.get("observation_prefix")
        begin = pending.get("observation_begin")
        observations = (world_fixture.load_state("command-observations") or {}).get("commands", [])
        if prefix is None or begin != len(prefix) or observations[:begin] != prefix:
            raise ValueError("原中断 builder 的命令观测前缀已改变")
        invocation = pending.get("invocation_binding")
        if invocation is None or json.loads(world_fixture.read_cut(invocation)) != {"argv": argv}:
            raise ValueError("原中断 builder 缺少准确的副作用前调用原件")
        invocation_dir = invocation["path"].parent
        if any((invocation_dir / name).exists() for name in ("stdout.log", "stderr.log", "command.json")):
            # 完整旧输出仍交原接缝核验；不将普通 pending 变成新批次。
            if recovery is None:
                return super().vc_step(cursor, key, context, phase, sequence, action)
            raise ValueError("已裁定无输出的原 builder 调用后来出现输出，禁止混用")
        if recovery is None:
            if observations[begin:]:
                raise ValueError("原中断 builder 之后出现未绑定的命令，禁止推测续作位置")
            world_fixture.verify_commands(prefix)
            source = self._interrupted_builder_source(context, sequence, action)
            recovery = {"original_sequence": sequence, "recovery_sequence": sequence + 1,
                "original_pending": world_fixture.file_cut(world_fixture._state_path(logical)),
                "invocation": invocation, "source": source, "product_continuation": continuation, "status": "pending"}
            world_fixture.save_state(recovery_name, recovery)
        if (recovery["original_sequence"] != sequence or recovery["recovery_sequence"] != sequence + 1
                or recovery["invocation"] != invocation or recovery.get("product_continuation") != continuation):
            raise ValueError("原中断 builder 的后继恢复序号／调用／演进来源绑定漂移")
        bindings = [recovery["original_pending"], invocation, continuation["binding"], *recovery["source"]["bindings"]]
        def verify_originals():
            for binding in bindings:
                raw = world_fixture.read_cut(binding)
                if binding["offset"] != 0 or len(raw) != binding["path"].stat().st_size:
                    raise ValueError("原中断 builder 的不可变原件后来被追加")
        verify_originals()
        code, reconciled, stderr = self.source_step(cursor, "reconcile-interrupted-builder",
            ["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", recovery["source"]["run_dir"]], campaign)
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        next_sequence = recovery["recovery_sequence"]
        outcome = super().vc_step(cursor, "builder-after-interruption", context, phase, next_sequence, action)
        verify_originals()
        context["next_sequence"] = max(context["next_sequence"], next_sequence + 1)
        context["last_builder_sequence"] = next_sequence
        world_fixture.save_state(recovery_name, {**recovery, "status": "complete"})
        return outcome

    def dispatch_builder(self, context: dict[str, Any], sequence: int, *, baseline: int) -> tuple[int, dict | None, str]:
        """先刷新当前 revision 的真实候选，再走父类的 compare 与正式 builder 配置。"""

        world_fixture.refresh_world(self, context, candidate_id=self.active_candidate, revision=self.active_revision)
        return super().dispatch_builder(context, sequence, baseline=baseline)

    def failed_builder(self, context: dict[str, Any], sequence: int) -> dict:
        cursor = f"failed-builder:{self.active_candidate}"
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            saved = {"phase": "builder", "campaign": context["campaign"], "sequence": sequence,
                     "counts_before": project_ledger.replay_head(self.project_root)["root_cause_counts"],
                     "previous_causes": set(getattr(self, 'assertion_failure_causes', set()))}
            world_fixture.checkpoint(self, cursor, **saved)
        counts_before = saved["counts_before"]
        code, failed, stderr = self.dispatch_builder(context, saved["sequence"], baseline=0)
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
        code, reconciled, stderr = self.source_step(cursor, "reconcile", ["reconcile-supervisor-run", "--campaign-dir", context["campaign"],
                                                      "--run-dir", self.failed_evaluation_run], context["campaign"])
        self.assertIsNotNone(reconciled, stderr)
        # r1、r2 的相同断言失败仍按原根因合同累计；r2 必须暂停，不能绕过上限。
        # 对账本身也会登记根因，因此在它完成后收集待修复集合，保留所有历史计数。
        head_after = project_ledger.replay_head(self.project_root)
        counts_after = head_after['root_cause_counts']
        causes = {row['root_cause_id'] for row in reconciled.get('root_causes', [reconciled['root_cause']])}
        previous_causes = saved['previous_causes']
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
        world_fixture.checkpoint(self, cursor, **{**saved, "phase": "complete"})
        return failed

    def invalidate_first_candidate(self, context: dict[str, Any]) -> dict:
        campaign = context['campaign']
        cursor = 'r1-invalidation'
        argv = ["--campaign-dir", campaign, "--candidate-id", self.active_candidate, "--reviewer", "连续链审核"]
        code, preview, stderr = self.source_step(cursor, 'recovery-preview', ['evaluation-recover', 'preview', *argv], campaign)
        self.assertEqual(code, 0, stderr)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual(preview["failure_scope"]["jobs"], [transient.FAILED_JOB])
        code, redirect, stderr = self.source_step(cursor, 'recovery-redirect', ['evaluation-recover', 'apply', *argv,
            "--approve-sha256", preview["review_sha256"], "--root-cause-class", "candidate-source"], campaign)
        self.assertIsNotNone(redirect, stderr)
        self.assertEqual((code, redirect["status"]), (0, "redirect"), stderr)
        self.assertIn("invalidate-candidate preview", redirect["next_command"])
        code, proposed, stderr = self.source_step(cursor, 'invalidate-preview', ["invalidate-candidate", "preview", *argv], campaign)
        self.assertIsNotNone(proposed, stderr)
        self.assertEqual((code, proposed["status"]), (0, "preview"), stderr)
        code, applied, stderr = self.source_step(cursor, 'invalidate-apply',
            ["invalidate-candidate", "apply", *argv, "--approve-sha256", proposed["review_sha256"]], campaign)
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"]), (0, "applied"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "revision_required")
        return applied

    def build_second_candidate(self, campaign: Path, context: dict[str, Any]) -> None:
        """r2 按原修订、构建和两批 VC-4 顺序续作；原序号在派发前冻结。"""

        cursor = 'r2-build'
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            saved = {'phase': 'open', 'campaign': campaign, 'fixture_values': {}, 'bindings': []}
            world_fixture.checkpoint(self, cursor, **saved)
        for binding in saved['bindings']:
            world_fixture.read_cut(binding)
        if world_fixture.load_state('world:' + cursor) is not None:
            restored = world_fixture.restore_world(self, cursor)
            context.clear()
            context.update(restored)
        if 'next_sequence' in saved:
            context['next_sequence'] = max(context['next_sequence'], saved['next_sequence'])
        if saved['phase'] == 'open':
            code, opened, stderr = self.source_step(cursor, 'revision-open', ["revision-open", "--campaign-dir", campaign,
                "--candidate-id", SECOND_CANDIDATE, "--supersedes", transient.CANDIDATE], campaign)
            self.assertIsNotNone(opened, stderr)
            self.assertEqual((code, opened["revision"], opened["store"]["payload"]["revision"]), (0, 2, 2), stderr)
            self.active_candidate, self.active_revision = SECOND_CANDIDATE, 2
            saved = {**saved, 'phase': 'preview'}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved['phase'] == 'preview':
            code, preview, stderr = self.source_step(cursor, 'gate-preview',
                ["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", SECOND_CANDIDATE], campaign)
            self.assertIsNotNone(preview, stderr)
            self.assertEqual((code, preview["candidate_revision"]), (2, 2), stderr)
            saved = {**saved, 'phase': 'source', 'preview': preview}
            world_fixture.checkpoint(self, cursor, **saved)
        preview = saved['preview']
        fixture = build_fixture.CandidateBuildFixture(self.root / "build-r2", campaign, SECOND_CANDIDATE,
            run_main=lambda argv: self.source_step(cursor, 'fixture-gate-plan', argv, campaign))
        for key, value in saved['fixture_values'].items():
            setattr(fixture, key, value)
        operations = [('source', lambda: fixture.build_source(preview=preview, marker="r2：修正版本请求头值")),
                      ('artifacts', fixture.build_artifacts), ('transition', fixture.build_transition),
                      ('implementation', lambda: fixture.build_implementation_receipt(preview['requirements'], f'{campaign.name}-ledger'))]
        phases = ['source', 'artifacts', 'transition', 'implementation', 'clone']
        for operation, invoke in operations:
            if saved['phase'] != operation:
                continue
            if saved.get('fixture_pending'):
                raise ValueError(f'r2 原构建来源停在 {operation} 的未决形成过程，禁止重建候选源码或覆盖原件')
            saved = {**saved, 'fixture_pending': True}
            world_fixture.checkpoint(self, cursor, **saved)
            invoke()
            fields = ('base_commit', 'commit', 'mapping_path', 'plan_path', 'gate_plan', 'parameters')
            fixture_values = {key: getattr(fixture, key) for key in fields if hasattr(fixture, key)}
            # 原构建来源每段实际写出的普通文件逐字节保存，不含 Git 内部锁和日志。
            files = [path for path in fixture.root.rglob('*') if path.is_file() and '.git' not in path.parts]
            saved = {**saved, 'phase': phases[phases.index(operation) + 1], 'fixture_pending': False,
                     'fixture_values': fixture_values, 'bindings': [world_fixture.file_cut(path) for path in files]}
            world_fixture.checkpoint(self, cursor, **saved)
        plan_source = self.root / "plan-source-r2"
        lifecycle = fixture.lifecycle_dir.relative_to(fixture.source)
        dispatched_plan = plan_source / lifecycle / "gate-plan-dispatch.json"
        if saved['phase'] == 'clone':
            if saved.get('clone_pending'):
                raise ValueError('r2 原计划源码克隆未形成完整停点，禁止覆盖原目录')
            code, _cloned, stderr = self.process_step(cursor, 'clone',
                ["git", "clone", "-q", str(fixture.source), str(plan_source)], campaign, timeout=60)
            self.assertEqual(code, 0, stderr)
            saved = {**saved, 'phase': 'plan', 'clone_pending': False, 'plan_sequence': world_fixture.take_sequence(context)}
            saved['next_sequence'] = context['next_sequence']
            world_fixture.checkpoint(self, cursor, **saved)
            world_fixture.save_world(cursor, context)
        if saved['phase'] == 'plan':
            action = batch_tests.batch.managed_action("vc4-plan-r2", "VC-4:plan-candidate-gates",
                ["plan-candidate-gates", "--campaign-dir", campaign, "--candidate-id", SECOND_CANDIDATE,
                 "--candidate-source", plan_source, "--mapping", plan_source / lifecycle / "gate-mapping.json", "--output", dispatched_plan],
                item_ids=["vc4-plan-r2"])
            sequence = saved['plan_sequence']
            self.assert_batch_passed(self.vc_step(cursor, 'vc4-plan', context, 'VC-4', sequence, action), sequence, 'VC-4')
            self.assertEqual(dispatched_plan.read_bytes(), fixture.plan_path.read_bytes())
            binary = fixture.install_fake_commands(self.root / "fake-bin-r2")
            saved = {**saved, 'phase': 'record', 'record_sequence': world_fixture.take_sequence(context), 'binary': binary,
                     'binary_bindings': [world_fixture.file_cut(path) for path in binary.iterdir() if path.is_file()]}
            saved['next_sequence'] = context['next_sequence']
            world_fixture.checkpoint(self, cursor, **saved)
            world_fixture.save_world(cursor, context)
        if saved['phase'] in {'record', 'complete'}:
            for binding in saved['binary_bindings']:
                world_fixture.read_cut(binding)
            action = batch_tests.batch.managed_action("vc4-record-r2", "VC-4:record-candidate-build", fixture.argv(), item_ids=["vc4-record-r2"])
            sequence = saved['record_sequence']
            with mock.patch.dict(os.environ, {"PATH": f"{saved['binary']}{os.pathsep}{os.environ.get('PATH', '')}"}):
                self.assert_batch_passed(self.vc_step(cursor, 'vc4-record', context, 'VC-4', sequence, action), sequence, 'VC-4')
        revision = json.loads((upgrade._candidate_revision_dir(campaign, 2) / "seal.json").read_bytes())
        self.assertEqual(revision["superseded"]["candidate_id"], transient.CANDIDATE)
        self.assertIn("source", revision["changed_layers"])
        previous = json.loads((campaign / "candidates" / transient.CANDIDATE / "build-receipt.json").read_bytes())
        current = json.loads((campaign / "candidates" / SECOND_CANDIDATE / "build-receipt.json").read_bytes())
        self.assertNotEqual(previous["source"]["tree_sha256"], current["source"]["tree_sha256"])
        self.assertTrue(vc.vc_checkpoint_path(campaign, "VC-4", revision=2).is_file())
        world_fixture.checkpoint(self, cursor, **{**saved, 'phase': 'complete'})

    def repair_evaluator(self, context: dict[str, Any], attempt_root: Path) -> dict:
        """实际修复、身份演进、根因放行和 b1 批准逐段承接；不替换真实判定。"""

        cursor = 'evaluator-repair'
        campaign = context['campaign']
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            saved = {'phase': 0, 'campaign': campaign, 'attempt_root': attempt_root, 'values': {}}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved['attempt_root'] != attempt_root:
            raise ValueError('评估器修复停点绑定的原 attempt 已改变')
        phase, values = saved['phase'], dict(saved['values'])
        repaired, digests = values.get('repaired'), values.get('digests')
        deployment, reconciled = values.get('deployment'), values.get('reconciled')
        preview, applied = values.get('preview'), values.get('applied')
        if deployment is not None:
            world_fixture.read_cut(values['deployment_binding'])
        if phase == 0:
            self.restore_checker()
            repaired = upgrade._tool_identity(include_git=False)
            digests = identity_policy.evaluator_dependency_digests()
            self.assertNotEqual(digests["checker_sha256"], self.defective_evaluator["checker_sha256"])
            self.assertEqual(repaired["policy_sha256"], self.tool_identity["policy_sha256"])
            self.assertEqual(repaired["wire_producer_sha256"], self.tool_identity["wire_producer_sha256"],
                             "实际策略若把本次解析修复归到 wire，不得绕过身份边界继续本链")
            deployment = ledger_fixture.write_claude_deploy_receipt(self.data, repaired, stamp="20261008t090000z")
            values.update(repaired=repaired, digests=digests, deployment=deployment)
            values["deployment_binding"] = world_fixture.file_cut(deployment)
            phase = 1
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 1:
            argv = ["tool-evolution", "--campaign-dir", context["campaign"], "--fix-commit", "e" * 40,
                    "--reason", "修复请求头大小写归一化，候选证据原字节复用"]
            code, preview, stderr = self.source_step(cursor, "tool-preview", argv, campaign)
            self.assertIsNotNone(preview, stderr)
            self.assertEqual((code, preview["status"]), (0, "approval_required"), stderr)
            self.assertEqual(preview["impact"]["official"]["affected_job_ids"], [])
            self.assertTrue(all(not value["affected_job_ids"] for value in preview["impact"]["candidates"].values()))
            approved = [*argv, "--approve-sha256", preview["review_sha256"], "--approved-by", "连续链审核"]
            code, evolved, stderr = self.source_step(cursor, "tool-apply", approved, campaign)
            self.assertIsNotNone(evolved, stderr)
            self.assertEqual((code, evolved["status"], evolved["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)
            phase = 2
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 2:
            if repaired["evidence_semantics_sha256"] != self.tool_identity["evidence_semantics_sha256"]:
                code, epoch, stderr = self.source_step(cursor, "epoch", ["evaluation-epoch", "--campaign-dir", context["campaign"],
                    "--candidate-id", self.active_candidate, "--attempt-id", attempt_root.name, "--reason", "修复头大小写解析后离线重评"], campaign)
                self.assertIsNotNone(epoch, stderr)
                self.assertEqual((code, epoch["status"]), (0, "epoch_appended"), stderr)
            phase = 3
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 3:
            # 暂停不会生成恢复放行收据。真实 checker 回归与修复登记完成后，
            # 必须重放原失败 run 的对账，使账本回到 active，再进入评估恢复批准。
            self.release_repaired_assertion_roots(context, deployment)
            phase = 4
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 4:
            code, reconciled, stderr = self.source_step(cursor, "repaired-reconcile", ["reconcile-supervisor-run", "--campaign-dir", context["campaign"],
                                                          "--run-dir", self.failed_evaluation_run], campaign)
            self.assertIsNotNone(reconciled, stderr)
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")
            repaired_counts = project_ledger.replay_head(self.project_root)["root_cause_counts"]
            self.assertTrue(all(repaired_counts.get(cause) == 0 for cause in self.assertion_failure_causes))
            passed = [event for event, _raw in timing._load_events(self.ledger_dir)
                      if event.get("event_id") == f"reconcile-run-passed-{reconciled['run_id']}"]
            self.assertEqual([event["event_type"] for event in passed], ["receipt_passed"])
            values.update(reconciled=reconciled)
            phase = 5
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 5:
            code, preview, stderr = self.source_step(cursor, "refusal-preview",
                ["evaluation-recover", "preview", "--campaign-dir", campaign, "--candidate-id", self.active_candidate, "--reviewer", "连续链审核"], campaign)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(preview, stderr)
            self.refuse_wrong_deployments(context, preview, deployment)
            values.update(preview=preview)
            phase = 6
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 6:
            # 错误部署收据必须零写入；批准草案仍绑定修复与重新对账后的当前 head。
            code, preview, stderr = self.source_step(cursor, "final-preview",
                ["evaluation-recover", "preview", "--campaign-dir", campaign, "--candidate-id", self.active_candidate, "--reviewer", "连续链审核"], campaign)
            self.assertEqual(code, 0, stderr)
            self.assertIsNotNone(preview, stderr)
            code, applied, stderr = self.source_step(cursor, "recovery-apply",
                ["evaluation-recover", "apply", "--campaign-dir", campaign, "--candidate-id", self.active_candidate, "--reviewer", "连续链审核",
                 "--approve-sha256", preview["review_sha256"], "--root-cause-class", "evaluator-defect", "--fix-commit", "e" * 40,
                 "--deployment-receipt", deployment], campaign)
            self.assertIsNotNone(applied, stderr)
            self.assertEqual((code, applied["kind"], applied["evaluation_baseline"]), (0, "evaluator-only", 1), stderr)
            values.update(preview=preview, applied=applied)
            phase = 7
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
        if phase == 7:
            self.tool_identity = repaired
            phase = 8
            world_fixture.checkpoint(self, cursor, phase=phase, campaign=campaign, attempt_root=attempt_root, values=values)
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
            cursor = 'wrong-deployment:' + label
            campaign = context["campaign"]
            scenario = f"评估器修复拒绝错误部署收据：{label}"
            saved = world_fixture.resume_checkpoint(self, cursor)
            if saved is None:
                path = transient._write(self.root / "wrong-deployment-inputs" / f"{label}.json", document)
                saved = {'phase': 'refusal', 'campaign': campaign, 'path': path,
                         'path_binding': world_fixture.file_cut(path), 'before_files': file_digests(
                             campaign, candidate_store.campaign_store_root(campaign), self.ledger_dir, self.project_root),
                         'before_head': project_ledger.replay_head(self.project_root),
                         'calls': len(self.runtime.candidate_calls())}
                world_fixture.checkpoint(self, cursor, **saved, audit_snapshots={scenario: 'uninitialized'})
            world_fixture.read_cut(saved['path_binding'])
            if saved['phase'] == 'complete':
                from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit
                report = ScenarioAudit.revalidate(self, scenario)
                if report not in context['records']:
                    context['records'].append(report)
                self.wrong_deployment_refusals.append({'case': label, 'stderr': saved['stderr']})
                continue
            path = saved['path']
            world_fixture.read_cut(saved['path_binding'])
            write_roots = [campaign, candidate_store.campaign_store_root(campaign), self.ledger_dir, self.project_root]
            audit = self.late_audit(f"评估器修复拒绝错误部署收据：{label}", {
                **context, 'trusted_roots': [*context.get('trusted_roots', []),
                    *write_roots, self.runtime.candidate_calls_file]})
            before_files, before_head, calls = saved['before_files'], saved['before_head'], saved['calls']
            code, _result, stderr = self.source_step(cursor, 'refuse',
                ["evaluation-recover", "apply", "--campaign-dir", campaign, "--candidate-id", self.active_candidate,
                 "--reviewer", "连续链审核", "--approve-sha256", preview["review_sha256"],
                 "--root-cause-class", "evaluator-defect", "--fix-commit", "e" * 40, "--deployment-receipt", path], campaign)
            self.assertEqual(code, 1, stderr)
            self.assertIn("不是 Claude 受管部署收据", stderr)
            self.assertEqual(file_digests(*write_roots), before_files, f"{label} 收据必须在任何恢复写入前拒绝")
            self.assertEqual(project_ledger.replay_head(self.project_root), before_head, f"{label} 误用不得修改原项目总账")
            self.assertEqual(len(self.runtime.candidate_calls()), calls)
            self.assertFalse((campaign / "candidates" / self.active_candidate / "revisions" / "b1" / upgrade.EVALUATION_BASELINE_COMMIT_FILENAME).exists())
            self.wrong_deployment_refusals.append({'case': label, 'stderr': stderr})
            audit.check_trusted()
            audit.check_closure(executed=[], expected=[], what='错误部署收据在恢复写入前拒绝',
                source=audit.refusal_closure_source(stderr))
            audit.check_accounting()
            audit.check_elapsed_untouched(self.ledger_dir, self.project_root)
            audit.check_history()
            # 使用真实状态入口输出的恢复命令，不由测试拼一条命令冒充产品提示。
            with self.source_context(), vc5_sources.seams(context['config_path']):
                recovery_status = self.candidate_status(context)
            audit.check_entry(recovery_status, 'evaluation-recover')
            report = audit.report(campaign_id=campaign.name, candidate_id=self.active_candidate,
                candidate_revision=self.active_revision, evaluation_baseline=0, refusal=label)
            if report not in context['records']:
                context['records'].append(report)
            world_fixture.checkpoint(self, cursor, **{**saved, 'phase': 'complete', 'stderr': stderr})

    def release_repaired_assertion_roots(self, context: dict[str, Any], deployment: Path) -> None:
        """修复后先用真实 checker 验证当前字节，再按正式根因修复入口放行，不能调高冻结上限。"""

        cursor = 'evaluator-root-release'
        campaign = context['campaign']
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            saved = {'phase': 'regression', 'campaign': campaign,
                     'previous_counts': project_ledger.replay_head(self.project_root)["root_cause_counts"]}
            world_fixture.checkpoint(self, cursor, **saved)
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
        code, _checked, stderr = self.process_step(cursor, 'checker-regression', [str(item) for item in command], campaign)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(regression.read_bytes())['status'], 'pass')
        previous_counts = saved["previous_counts"]
        if saved['phase'] == 'regression':
            saved = {**saved, 'phase': 'repairs', 'regression_binding': world_fixture.file_cut(regression),
                     'deployment_binding': world_fixture.file_cut(deployment)}
            world_fixture.checkpoint(self, cursor, **saved)
        world_fixture.read_cut(saved['regression_binding'])
        world_fixture.read_cut(saved['deployment_binding'])
        for cause in sorted(self.assertion_failure_causes):
            code, _repaired, stderr = self.process_step(cursor, 'root-repair:' + cause, [sys.executable, '-B', '-m', 'tools.official_client_capture.codex_upgrade_project_ledger',
                'record-root-cause-repair', '--ledger-dir', str(self.project_root), '--root-cause-id', cause, '--kind', 'code',
                '--binding', f"fix_commit_sha={'e' * 40}", '--binding', f'regression_receipt_sha256={upgrade.file_sha256(regression)}',
                '--binding', f'deployment_receipt_sha256={upgrade.file_sha256(deployment)}', '--note', '实际 checker 修复并验证 r2 原字节后放行'], campaign)
            self.assertEqual(code, 0, stderr)
        repaired_counts = project_ledger.replay_head(self.project_root)["root_cause_counts"]
        self.assertEqual({cause: count for cause, count in repaired_counts.items() if cause not in self.assertion_failure_causes},
                         {cause: count for cause, count in previous_counts.items() if cause not in self.assertion_failure_causes},
                         "本次评估修复不得清除无关根因的计数")

        world_fixture.checkpoint(self, cursor, **{**saved, "phase": "complete"})

    def run_revision_evaluator_chain(self) -> dict[str, Any]:
        """同一 r1→r2→b1 原链保存局部值与审计before，未完成命令才继续执行。"""

        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        cursor = 'revision-chain'
        saved = world_fixture.resume_checkpoint(self, cursor)
        if saved is None:
            if os.environ.get(world_fixture.METHOD_RESUME_ENV):
                raise ValueError('revision 原方法缺少阶段游标，禁止重新建立 Campaign')
            saved = {'phase': 'unstarted', 'values': {}}
            world_fixture.checkpoint(self, cursor, **saved)
        if saved['phase'] == 'unstarted':
            saved = {'phase': 'vc3-vc4', 'values': {}, 'initializations': {'vc3-vc4-parent': 'uninitialized'}}
            world_fixture.checkpoint(self, cursor, **saved)
        phase, values = (0 if saved['phase'] == 'vc3-vc4' else saved['phase']), dict(saved['values'])
        campaign = values.get('campaign')
        manifest = values.get('manifest')
        first_root = values.get('first_root')
        first_attempt = values.get('first_attempt')
        first_stage = values.get('first_stage')
        first_snapshot = values.get('first_snapshot')
        invalidated = values.get('invalidated')
        second_root = values.get('second_root')
        second_attempt = values.get('second_attempt')
        second_stage = values.get('second_stage')
        r2_provenance = values.get('r2_provenance')
        counted = values.get('counted')
        roots = values.get('roots')
        snapshot = values.get('snapshot')
        calls = values.get('calls')
        before = values.get('before')
        project_before = values.get('project_before')
        recovered = values.get('recovered')
        index_path = values.get('index_path')
        index = values.get('index')
        after = values.get('after')
        context = world_fixture.restore_world(self, cursor) if phase >= 2 else None
        source_name, evaluator_name = '候选源码失败与 r2 修订接续', '评估器真实修复与 b1 零采集重评'
        source_audit = self.late_audit(source_name, context) if 1 < phase <= 6 else None
        evaluator_audit = self.late_audit(evaluator_name, context) if 7 < phase < 11 else None
        if phase > 6:
            ScenarioAudit.revalidate(self, source_name)
        if phase == 11:
            ScenarioAudit.revalidate(self, evaluator_name)
            return context
        if phase == 0:
            campaign = self.drive_vc3_vc4_to_candidate_sealed()
            manifest = upgrade.load_campaign_manifest(campaign)
            world_fixture.freeze_control_profile(campaign)
            values.update(campaign=campaign, manifest=manifest)
            phase = 1
            world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
        with client_fixture.source_scope():
            if phase == 1:
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign,
                    audit_snapshots={source_name: (world_fixture.load_state("cursor:" + cursor) or {}).get("audit_snapshots", {}).get(source_name, "uninitialized")})
                first_root, first_attempt, first_stage = self.capture_and_seal(campaign)
                context = self.evaluation_context(campaign)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign,
                    audit_snapshots={source_name: (world_fixture.load_state("cursor:" + cursor) or {}).get(
                        "audit_snapshots", {}).get(source_name, "uninitialized")})
                source_audit = self.late_audit("候选源码失败与 r2 修订接续", context)
                values.update(first_root=first_root, first_attempt=first_attempt, first_stage=first_stage)
                phase = 2
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 2:
                self.failed_builder(context, context["next_sequence"])
                first_snapshot = self.boundary([first_root, campaign / "candidates" / transient.CANDIDATE / "seal",
                                               upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "capture-candidate"),
                                               upgrade._stage_write_target(campaign, transient.CANDIDATE, 0, "assertions"),
                                               *[Path(root) for row in first_attempt["results"] for root in row["evidence_roots"]]])
                values.update(first_snapshot=first_snapshot)
                phase = 3
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 3:
                invalidated = self.invalidate_first_candidate(context)
                values.update(invalidated=invalidated)
                phase = 4
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 4:
                self.build_second_candidate(campaign, context)
                transient._write(self.relay_control, {"bad_probe": None, "header_name": "Anthropic-Version"})
                phase = 5
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 5:
                second_root, second_attempt, second_stage = self.capture_and_seal(campaign)
                self.assertEqual(self.boundary([Path(path) for path in first_snapshot]), first_snapshot)
                self.assertEqual(second_attempt["identity"]["candidate_revision"], 2)
                self.assertTrue(all(row["disposition"] == "executed" for row in second_attempt["results"]))
                self.assertNotEqual(first_stage["store"]["validation_attempt_ref"], second_stage["store"]["validation_attempt_ref"])
                world_fixture.refresh_world(self, context, candidate_id=SECOND_CANDIDATE, revision=2)
                values.update(second_root=second_root, second_attempt=second_attempt, second_stage=second_stage)
                phase = 6
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 6:
                jobs = self.revision_candidate_jobs(campaign, manifest)
                self.assertCountEqual([row["id"] for row in second_attempt["results"]], [job.job_id for job in jobs])
                source_audit.check_trusted()
                source_audit.check_closure(executed=[row['id'] for row in second_attempt['results']], expected=[job.job_id for job in jobs],
                    what='源码改变后 r2 的完整候选 Job',
                    source=source_audit.file_closure_source(second_root / 'attempt.json', ('results',), row_key='id'))
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
                values.update(r2_provenance=r2_provenance)
                phase = 7
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 7:
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign,
                    audit_snapshots={evaluator_name: (world_fixture.load_state("cursor:" + cursor) or {}).get("audit_snapshots", {}).get(evaluator_name, "uninitialized")})
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
                values.update(counted=counted, roots=roots, snapshot=snapshot, calls=calls, before=before, project_before=project_before)
                phase = 8
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 8:
                recovered = self.repair_evaluator(context, second_root)
                values.update(recovered=recovered)
                phase = 9
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 9:
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
                values.update(index_path=index_path, index=index, after=after)
                phase = 10
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            if phase == 10:
                evaluator_audit.check_trusted()
                evaluator_audit.check_closure(executed=[row['rule'] for row in index['rules']],
                    expected=list(upgrade._approved_rules(campaign, manifest, require_approved=True)), what='修复后按全部批准规则离线重评',
                    source=evaluator_audit.file_closure_source(index_path, ('rules',), row_key='rule'))
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
                phase = 11
                if context is not None:
                    world_fixture.save_world(cursor, context)
                world_fixture.checkpoint(self, cursor, phase=phase, values=values, campaign=campaign)
            return context

    def run_revision_evaluator_delivery_chain(self) -> dict[str, Any]:
        """原第三条方法从 r2/b1 接严格验收与 VC-6，不新增认证方法。"""

        cursor = 'revision-delivery-chain'
        with client_fixture.source_scope():
            saved = world_fixture.resume_checkpoint(self, cursor)
            if saved is None:
                if os.environ.get(world_fixture.METHOD_RESUME_ENV):
                    raise ValueError('revision 后段原方法缺少交接游标，禁止推导续接位置')
                saved = {'phase': 'revision'}
                world_fixture.checkpoint(self, cursor, **saved)
            if saved['phase'] == 'unstarted':
                saved = {'phase': 'revision'}
                world_fixture.checkpoint(self, cursor, **saved)
            if saved['phase'] == 'revision':
                world = self.run_revision_evaluator_chain()
                world_fixture.save_world(cursor, world)
                saved = {'phase': 'accept', 'campaign': world['campaign']}
                world_fixture.checkpoint(self, cursor, **saved)
            else:
                world = world_fixture.restore_world(self, cursor)
            if saved['phase'] == 'accept':
                world_fixture.strict_accept(self, world)
                world_fixture.save_world(cursor, world)
                saved = {**saved, 'phase': 'delivery'}
                world_fixture.checkpoint(self, cursor, **saved)
            if saved['phase'] == 'delivery':
                world['records'].extend(self.exercise_delivery_faults(world, sequence_start=world['next_sequence']))
                self.assertEqual({row['campaign_id'] for row in world['records']}, {world['campaign'].name})
                world_fixture.save_world(cursor, world)
                world_fixture.checkpoint(self, cursor, phase='complete', campaign=world['campaign'])
            return world

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """r2/b1 与两次错误部署拒绝仅重验原件，后段活守卫交给原父类。"""

        from tools.official_client_capture.tests import claude_late_control_chain_driver as late_control
        from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains import ScenarioAudit

        own = {'候选源码失败与 r2 修订接续', '评估器真实修复与 b1 零采集重评',
               '评估器修复拒绝错误部署收据：codex', '评估器修复拒绝错误部署收据：missing-client'}
        records = []
        state = world_fixture.load_state('cursor:revision-chain')
        if state is None or state.get('phase') != 11 or not world_fixture.restore_case(self):
            raise ValueError('revision 原方法未形成完整终态游标')
        # 仅成功方法进入这个瞬时范围；未完成 revision 仍走原分阶段恢复入口。
        with client_fixture.source_scope(), world_fixture.verified_read_only_world(self, state['campaign']):
            if own & set(expected):
                saved = world_fixture.resume_checkpoint(self, 'revision-chain')
                if saved is None or saved['phase'] != 11:
                    raise ValueError('revision 原方法未形成完整终态游标')
                world_fixture.restore_world(self, 'revision-chain')
                with self.source_context():
                    records.extend(ScenarioAudit.revalidate(self, name) for name in expected if name in own)
            remaining = [name for name in expected if name not in own]
            if remaining:
                records.extend(late_control.ClaudeLateControlChainDriver.revalidate_completed_method(self, remaining))
        by_name = {record['scenario']: record for record in records}
        if set(by_name) != set(expected):
            raise ValueError('revision 原方法审计集合与原声明不同')
        return [by_name[name] for name in expected]
