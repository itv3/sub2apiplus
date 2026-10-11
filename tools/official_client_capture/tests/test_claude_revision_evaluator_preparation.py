"""revision 八场景的来源接线与显式入口专项；不执行完整故障链。

官方原件和门禁环境使用实际 fixture 生成器与原严格消费者。入口报告
使用明示的内存指标测试失败传播和未认证边界，不能据此登记链通过。
"""

from __future__ import annotations

import copy
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import contextlib

from tools.official_client_capture import certify_release as release
from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_gate_receipt as gate
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_revision_evaluator_chain_driver as driver
from tools.official_client_capture.tests import claude_vc5_chain_fixture as sources
from tools.official_client_capture.tests import test_claude_vc5_gate_environment_fixture as gate_tests
from tools.official_client_capture.tests.real_chains import claude_revision_evaluator_scenarios as entry
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings
from tools.official_client_control.errors import ControlError


class DeliveryResumeEntryTests(unittest.TestCase):
    """连续链消费产品生成的监督入口；旧直接动作和不完整入口仍须拒绝。"""

    def test_three_recoverable_states_use_supervised_batch_entry(self):
        from tools.official_client_capture import claude_upgrade_delivery as delivery
        from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
        campaign = Path("/fixture/campaign")
        context = SimpleNamespace(campaign_dir=campaign, manifest={})
        for expected, closed, pending in (("delivery_stage_failed", True, None),
                ("delivery_in_progress", False, None), ("delivery_invalidation_pending", True, {"id": "fixture"})):
            state = {"receipt_refs": [], "complete": False,
                     "pending_invalidation_ref": pending, "latest_closed": closed}
            with self.subTest(status=expected), mock.patch.object(delivery, "_resolve", return_value=context), \
                    mock.patch.object(delivery, "load_stage_result", return_value=None), \
                    mock.patch.object(delivery, "_deliveries", return_value=[{"delivery_id": "d1"}]), \
                    mock.patch.object(delivery, "_chain", return_value=state):
                value = delivery.delivery_status(campaign, "candidate-r2")
                self.assertEqual(value["status"], expected)
                audit = outer.ScenarioAudit(self, expected)
                audit.check_entry(value, *late.RUN_ENTRY_FRAGMENTS)
                self.assertTrue(audit.checks["entry"]["passed"])

    def test_old_direct_or_incomplete_entry_cannot_pass_resume_check(self):
        from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
        for hint in ("deliver-candidate run", "compile-and-run-vc-batch --phase VC-5", "compile-and-run-vc-batch --phase VC-6"):
            with self.subTest(hint=hint), self.assertRaises(AssertionError):
                outer.ScenarioAudit(self, "恢复入口拒绝").check_entry({"next_command": hint}, *late.RUN_ENTRY_FRAGMENTS)


class RevisionCheckpointTests(unittest.TestCase):
    """只检验阶段接续；冻结 Job 用原消费者展开，运行与封存事实为明示短验输入。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool_identity = upgrade._tool_identity(include_git=False)

    def setUp(self) -> None:
        self.fixture = driver.candidate_tests.CandidateFixture("runTest")
        self.fixture.tool_identity = self.tool_identity
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.campaign = self.fixture.write_candidate_campaign(mode="preflight_only")
        self.manifest = upgrade.load_campaign_manifest(self.campaign)
        self.case = driver.ClaudeRevisionEvaluatorChainDriver("runTest")
        self.case.source_context = self.fixture.patched
        self.case.active_candidate, self.case.active_revision = driver.SECOND_CANDIDATE, 2
        self.attempt_root = self.campaign / "candidates" / driver.SECOND_CANDIDATE / "attempts/short-fixture"
        self.jobs = upgrade._campaign_jobs(self.campaign, self.manifest, "candidate", candidate_id=driver.SECOND_CANDIDATE)
        self.attempt = {"identity": {"candidate_revision": 2},
                        "results": [{"id": job.job_id, "disposition": "executed", "evidence_roots": []} for job in self.jobs]}
        self.stage = {"attempt": {"path": (self.attempt_root / "attempt.json").relative_to(self.campaign).as_posix()},
                      "store": {"validation_attempt_ref": {"fact_id": "short-r2"}}}

    def test_phase5_checkpoint_only_saves_facts_and_keeps_job_serialization_rejected(self) -> None:
        first_snapshot = {str(self.fixture.root / "old-original"): "原边界摘要"}
        saved = {"phase": 5, "values": {"campaign": self.campaign, "manifest": self.manifest,
            "first_snapshot": first_snapshot, "first_stage": {"store": {"validation_attempt_ref": {"fact_id": "short-r1"}}}}}
        world = {"campaign": self.campaign, "candidate_id": driver.SECOND_CANDIDATE, "revision": 2}
        self.case.capture_and_seal = mock.Mock(return_value=(self.attempt_root, self.attempt, self.stage))
        self.case.boundary = mock.Mock(return_value=first_snapshot)
        self.case.late_audit = mock.Mock()
        captured = []

        class CheckpointReached(Exception):
            pass

        def checkpoint(_case, _cursor, **values):
            # 原序列化器是本短验的真实拒绝边界；不打桩成允许任意对象。
            driver.world_fixture._encoded(values)
            captured.append(values)
            if values.get("phase") == 6:
                raise CheckpointReached()

        with mock.patch.object(driver.world_fixture, "resume_checkpoint", return_value=saved), \
                mock.patch.object(driver.world_fixture, "restore_world", return_value=world), \
                mock.patch.object(driver.world_fixture, "refresh_world"), \
                mock.patch.object(driver.world_fixture, "save_world"), \
                mock.patch.object(driver.world_fixture, "checkpoint", side_effect=checkpoint), \
                self.assertRaises(CheckpointReached):
            self.case.run_revision_evaluator_chain()
        self.assertEqual(captured[-1]["phase"], 6)
        self.assertNotIn("jobs", captured[-1]["values"])
        self.assertEqual(captured[-1]["values"]["second_attempt"], self.attempt)
        with self.assertRaisesRegex(TypeError, "不支持运行对象：Job"):
            driver.world_fixture._encoded(self.jobs)

    def test_phase6_reloads_original_frozen_jobs_for_current_r2(self) -> None:
        with mock.patch.object(driver.compare, "require_current_candidate", return_value={"revision": 2}) as current, \
                mock.patch.object(upgrade, "_campaign_jobs", wraps=upgrade._campaign_jobs) as expand:
            jobs = self.case.revision_candidate_jobs(self.campaign, self.manifest)
        self.assertEqual(jobs, self.jobs)
        current.assert_called_once_with(self.campaign, self.manifest, driver.SECOND_CANDIDATE,
                                        action="revision 原链候选 Job 闭合")
        expand.assert_called_once_with(self.campaign, self.manifest, "candidate", candidate_id=driver.SECOND_CANDIDATE)
        # 从 phase6 单独进入原驱动，证明实际 closure 消费重新展开的定义，而非仅测试帮助函数。
        saved = {"phase": 6, "values": {"campaign": self.campaign, "manifest": self.manifest,
            "second_root": self.attempt_root, "second_attempt": self.attempt, "second_stage": self.stage,
            "invalidated": {"next_command": "revision-open"}}}
        world = {"campaign": self.campaign, "candidate_id": driver.SECOND_CANDIDATE, "revision": 2, "records": []}
        audit = mock.Mock()
        audit.report.return_value = {"fixture_only": True}
        self.case.late_audit = mock.Mock(return_value=audit)

        class CheckpointReached(Exception):
            pass

        def checkpoint(_case, _cursor, **values):
            driver.world_fixture._encoded(values)
            if values.get("phase") == 7:
                raise CheckpointReached()

        with mock.patch.object(driver.world_fixture, "resume_checkpoint", return_value=saved), \
                mock.patch.object(driver.world_fixture, "restore_world", return_value=world), \
                mock.patch.object(driver.world_fixture, "save_world"), \
                mock.patch.object(driver.world_fixture, "checkpoint", side_effect=checkpoint), \
                mock.patch.object(driver.compare, "require_current_candidate", return_value={"revision": 2}), \
                mock.patch.object(driver.provenance, "collect_root_provenance", return_value={"unresolved_roots": [], "precise_total": 0}), \
                self.assertRaises(CheckpointReached):
            self.case.run_revision_evaluator_chain()
        self.assertEqual(audit.check_closure.call_args.kwargs["expected"], [job.job_id for job in self.jobs])
        self.assertEqual(audit.check_closure.call_args.kwargs["executed"], [row["id"] for row in self.attempt["results"]])

    def test_phase6_rejects_frozen_input_drift_or_noncurrent_revision(self) -> None:
        with mock.patch.object(driver.compare, "require_current_candidate", return_value={"revision": 1}), \
                self.assertRaisesRegex(AssertionError, "当前 r2"):
            self.case.revision_candidate_jobs(self.campaign, self.manifest)
        with mock.patch.object(driver.compare, "require_current_candidate", side_effect=upgrade.ConfigurationError("当前候选已替代")), \
                self.assertRaisesRegex(upgrade.ConfigurationError, "当前候选已替代"):
            self.case.revision_candidate_jobs(self.campaign, self.manifest)
        saved = copy.deepcopy(self.manifest)
        saved["jobs"] = [{"id": "被替换的停点任务"}]
        with mock.patch.object(driver.compare, "require_current_candidate") as current, \
                self.assertRaisesRegex(AssertionError, "冻结 Campaign"):
            self.case.revision_candidate_jobs(self.campaign, saved)
        current.assert_not_called()
        # 原冻结文件被改动时，原 manifest/Job 读侧失败；不得仅从停点的 Job ID 反推定义。
        scenario = self.campaign / "inputs/target-scenarios.json"
        scenario.write_bytes(scenario.read_bytes() + b" ")
        with mock.patch.object(driver.compare, "require_current_candidate", return_value={"revision": 2}), \
                self.assertRaises(upgrade.ConfigurationError):
            self.case.revision_candidate_jobs(self.campaign, self.manifest)

    def test_completed_capture_checkpoint_is_read_only_across_repeated_calls(self) -> None:
        saved = {"phase": "complete", "campaign": self.campaign,
                 "attempt_root": self.attempt_root, "attempt": self.attempt}
        # 产品读取返回明确的短验事实；此处只证明 completed 分支不重派或重写，不冒充整链验收。
        with mock.patch.object(driver.world_fixture, "resume_checkpoint", return_value=saved), \
                mock.patch.object(upgrade, "_load_capture_attempt", return_value=(self.attempt_root, self.attempt)) as read_attempt, \
                mock.patch.object(upgrade, "_load_stage_result", return_value=self.stage) as read_stage, \
                mock.patch.object(self.case, "prepare_runtime") as prepare, \
                mock.patch.object(self.case, "source_step") as command, \
                mock.patch.object(self.case, "seal") as seal, \
                mock.patch.object(self.case, "client_inputs") as clients, \
                mock.patch.object(driver.world_fixture, "checkpoint") as checkpoint:
            for _ in range(2):
                self.assertEqual(self.case.capture_and_seal(self.campaign), (self.attempt_root, self.attempt, self.stage))
        self.assertEqual((read_attempt.call_count, read_stage.call_count), (2, 2))
        for action in (prepare, command, seal, clients, checkpoint):
            action.assert_not_called()


class RevisionOfficialSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="revision-official-input-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = driver.ClaudeRevisionEvaluatorChainDriver("runTest")
        self.case.data = self.root / "data"
        self.campaign = self.case.data / "evidence/campaigns/revision-fixture"
        self.campaign.mkdir(parents=True)
        self.evaluation = self.campaign / "continuous-sources"
        self.side = evaluator.write_side(self.root / "original-evaluator-side", capture_id="revision-official")
        self.package = {"object_kind": "evidence_package", "sha256": "c" * 64}
        self.old_config = sources._write_json(self.evaluation / "source-config.json", {
            "official_source": str(self.evaluation / "original-source.json"),
            "capture_layout": {"host_runs_root": str(self.case.data / "runs"), "container_run_roots": [str(self.case.data / "runs")]},
        })

    def context(self) -> dict:
        world = {"campaign": self.campaign, "official": self.side, "evaluation": self.evaluation,
                 "config_path": self.old_config}
        self.case.runtime = SimpleNamespace(patched=lambda: contextlib.nullcontext())
        with mock.patch.object(driver.transient.world_fixture, "real_candidate_world", return_value=world), \
                mock.patch.object(driver.upgrade, "_load_stage_result", return_value={"store": {"evidence_package_ref": self.package}}):
            return self.case.evaluation_context(self.campaign)

    def closure(self, source: dict) -> list[dict]:
        roots = [{**source[name], "path": (self.campaign.relative_to(self.case.data) / source[name]["path"]).as_posix()}
                 for name in ("capture_manifest", "capture_index", "inventory")]
        channel = execution.VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64, "regression-fixture", self.case.data)
        return project_delivery_raw_bindings(channel, roots, gate_projection=None)

    def test_materialized_source_reaches_original_recursive_consumer_and_preserves_inputs(self) -> None:
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        world = self.context()
        config = json.loads(world["config_path"].read_bytes())
        source = json.loads(Path(config["official_source"]).read_bytes())
        closed = self.closure(source)
        self.assertEqual(len(closed), 17)
        self.assertEqual(config["gate_environment_mode"], sources.REAL_GATE_ENVIRONMENT)
        self.assertEqual(config["capture_layout"], json.loads(before[self.old_config])["capture_layout"])
        self.assertEqual(source["package_ref"], self.package)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        self.assertNotEqual(world["config_path"], self.old_config)
        original_reader = upgrade._capture_stage_attempt_context
        original_environment = arm.replay
        with sources.seams(world["config_path"]):
            self.assertIs(upgrade._capture_stage_attempt_context, original_reader)
            self.assertIs(arm.replay, original_environment)
        origin = json.loads((self.campaign / "seam/official/fixture-origin.json").read_bytes())
        self.assertTrue(origin["fixture_only"])
        self.assertFalse(origin["formal_version_semantics_certified"])

        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_delivery as delivery
        from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor
        from tools.official_client_capture.tests import claude_late_stage_chain_driver as late
        from tools.official_client_capture.tests import claude_typed_vc6_fixture as typed
        from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world

        # 两条连续链都消费冻结的两映射；真实布局读侧必须仍拒绝默认宿主根与未登记别名。
        manifest = {"configuration": {"capture_root": str(self.case.data), "capture_tool_root": "/capture/tools/official_client_capture",
            "capture_container": "fixture-capture", "synthetic_capture_container": "fixture-synthetic",
            "runtime_image": "fixture/runtime@sha256:" + "a" * 64}}
        original_layout_reader = capture._capture_layout
        original_mappings = (upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT, upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS)
        expected = (str(self.case.data.resolve()), str(self.case.data))

        def read_layout() -> tuple[str, str]:
            layout = capture._capture_layout(self.campaign, manifest)
            self.assertIs(capture._capture_layout, original_layout_reader)
            return str(layout.host_data_root), str(layout.capture_root)

        with self.assertRaises(upgrade.ConfigurationError):
            read_layout()
        with sources.seams(world["config_path"]):
            self.assertEqual(read_layout(), expected)
            bad = {"configuration": {**manifest["configuration"], "capture_root": "/unregistered-capture"}}
            with self.assertRaisesRegex(upgrade.ConfigurationError, "不是登记的容器 runs 别名"):
                capture._capture_layout(self.campaign, bad)

        class LayoutProbeComplete(Exception):
            pass

        observed = []

        class LayoutProbeFixture:
            def __init__(_fixture, campaign, candidate):
                observed.append(read_layout())

            def __enter__(_fixture):
                # 与真实 typed 守卫使用同一 fork 入口，核子进程继承的登记，不启动交付长链。
                context = multiprocessing.get_context("fork")
                receiver, sender = context.Pipe(duplex=False)

                def guard_read():
                    try:
                        sender.send(read_layout())
                    finally:
                        sender.close()

                process = context.Process(target=guard_read)
                process.start()
                sender.close()
                try:
                    self.assertTrue(receiver.poll(10), "守卫布局探针没有返回")
                    observed.append(receiver.recv())
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                    receiver.close()
                raise LayoutProbeComplete()

        with mock.patch.object(late.environment, "TypedDeliveryFixture", LayoutProbeFixture), \
                self.assertRaises(LayoutProbeComplete):
            self.case.exercise_delivery_faults(world)
        self.assertEqual(observed, [expected, expected])
        self.assertEqual((upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT, upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS), original_mappings)

        # 原 run/finalize 启动代码在真实新解释器里先登记来源配置，再安装 typed 来源。
        # 只隔离父批次执行；子进程调用真实来源安装与布局读侧，结果不作为整链认证。
        self.case.data.chmod(0o700)
        configuration, execution_context, _environment = fixture_world(self.case.data)
        configuration_path = self.campaign / "control/layout-probe-configuration.json"
        guard.write_json_once(configuration_path, configuration)
        execution_context["configuration_ref"] = guard.binding(self.case.data, configuration_path)
        self.case.control = self.case.data / "control/layout-probe-control.json"
        guard.write_json_once(self.case.control, {"schema_version": typed.CONTROL_SCHEMA, "fixture_only": True,
            "formal_version_semantics_certified": False, "context": execution_context})
        self.case.root, self.case.state_dir = self.root, self.case.data / "control/layout-probe-state"
        script = '''
import json, sys
from pathlib import Path
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_control import delivery_execution as execution
campaign, manifest, selected = sys.argv[1:]
try:
    layout = capture._capture_layout(Path(campaign), json.loads(manifest))
    observed = [str(layout.host_data_root), str(layout.capture_root)]
except capture.ConfigurationError:
    if selected == "1":
        raise
    observed = None
print(json.dumps({"layout": observed, "typed": "arm64-isolated" in delivery.ENVIRONMENT_EXECUTORS
    and executor.AUTHORITY in execution.SOURCE_VERIFIERS}))
'''
        for index, action in enumerate((delivery.run_action(self.campaign, "candidate-current",
                environment_executor=typed.EXECUTOR_NAME, observation_window_seconds=1,
                executor_configuration=configuration_path,
                executor_configuration_sha256=execution_context["configuration_ref"]["sha256"]),
                delivery.finalize_action(self.campaign, "candidate-current", "a" * 64))):
            def inspect_action(_argv):
                for selected in (True, False):
                    child_environment = {**os.environ, supervisor.CAMPAIGN_RUN_ACTION_ID_ENV:
                        action["action_id"] if selected else "vc3-unselected"}
                    result = subprocess.run([sys.executable, "-B", "-c", script, str(self.campaign),
                        json.dumps(manifest), "1" if selected else "0"], cwd=sources.REPOSITORY_ROOT,
                        env=child_environment, capture_output=True, text=True, timeout=15, check=True)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout), {"layout": list(expected) if selected else None,
                        "typed": selected})
                return 0, {}, ""

            with sources.seams(world["config_path"]), mock.patch.object(late.batch_tests, "run_main", side_effect=inspect_action), \
                    mock.patch.dict(os.environ, {typed.DIAGNOSTIC_ROOT_ENV: ""}):
                self.case.dispatch_vc6(world, 12 + index, action)
        # 错误来源配置另建原件，不篡改已经冻结的配置；新动作仍由原布局校验器拒绝。
        wrong_configuration = {**config, "capture_layout": {**config["capture_layout"],
            "container_run_roots": ["/unregistered-capture/runs"]}}
        wrong_path = sources._write_json(self.evaluation / "wrong-layout-source-config.json", wrong_configuration)
        wrong_world = {**world, "config_path": wrong_path}
        with sources.seams(wrong_path), mock.patch.object(late.batch_tests, "run_main", side_effect=inspect_action), \
                mock.patch.dict(os.environ, {typed.DIAGNOSTIC_ROOT_ENV: ""}), \
                self.assertRaises(subprocess.CalledProcessError) as rejected:
            self.case.dispatch_vc6(wrong_world, 14, action)
        self.assertIn("不是登记的容器 runs 别名", rejected.exception.stderr)
        self.assertEqual((upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT, upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS), original_mappings)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))

    def test_missing_materialized_parent_still_fails_in_original_consumer(self) -> None:
        world = self.context()
        config = json.loads(world["config_path"].read_bytes())
        source = json.loads(Path(config["official_source"]).read_bytes())
        (self.campaign / source["capture_manifest"]["path"]).unlink()
        with self.assertRaisesRegex(ControlError, "原件缺失"):
            self.closure(source)

    def test_refreshed_world_audit_still_rejects_materialized_source_overwrite(self) -> None:
        world = self.context()
        world.update(trusted_roots=[self.side.evidence_root, self.side.manifest_path],
                     store=SimpleNamespace(root=self.root / "store"))
        self.case.ledger_dir, self.case.project_root = self.root / "timing", self.root / "project"
        # 此轻量用例只隔离账本统计输入；可信／历史快照与漂移拒绝全部调用原六项审计。
        with mock.patch.object(outer, "ledger_totals", return_value={}):
            audit = self.case.late_audit("物化来源不被改写", world)
        self.assertIn(world["config_path"], audit.trusted_paths)
        self.assertIn(self.campaign / "seam/official", audit.trusted_paths)
        path = self.campaign / "seam/official/capture-index.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(AssertionError, "可信产物被改写"):
            audit.check_trusted()
        self.assertEqual(world["trusted_roots"], [self.side.evidence_root, self.side.manifest_path])

    def test_attempt_replacement_input_is_rejected_before_materialization(self) -> None:
        for field in ("attempt", "attempt_root"):
            with self.subTest(field=field):
                config = json.loads(self.old_config.read_bytes())
                config[field] = "不能消费的候选替身"
                sources._write_json(self.old_config, config)
                with self.assertRaises(AssertionError):
                    self.context()
                self.assertFalse((self.campaign / "seam").exists())
                config.pop(field)
                sources._write_json(self.old_config, config)


class TransientOfficialSourceTests(RevisionOfficialSourceTests):
    """七场景与八场景共用来源准备，原件闭包和损坏拒绝逐条复验。"""

    def setUp(self):
        super().setUp()
        data = self.case.data
        self.case = driver.transient.ClaudeTransientChainDriver('runTest')
        self.case.data = data


class RevisionGateSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = gate_tests.GateEnvironmentFixtureTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = driver.ClaudeRevisionEvaluatorChainDriver("runTest")
        self.case.active_candidate = self.fixture.subject["candidate_id"]
        self.world = {"campaign": self.fixture.root / "campaign", "manifest": {},
                      "candidate_id": self.case.active_candidate, "build": {}, "candidate_payload": {},
                      "config_path": self.fixture.config, "gate_root": self.fixture.gate_root,
                      "real_candidate": True, "stage": {"store": {"validation_execution_plan_ref": {"sha256": "1" * 64}}},
                      "store": SimpleNamespace(load_object=lambda _reference: {"payload": {
                          "external_gates": [{"host_id": "ARM64", "architecture": "linux/arm64"}]}})}

    def create(self) -> str:
        # 这里只提供当前候选身份输入；原环境 reader、门禁 finalizer 和 replay 都不替换。
        with mock.patch.object(driver.compare, "candidate_gate_identity", return_value={}), \
                mock.patch.object(sources, "expected_subject", return_value=self.fixture.subject):
            return self.case.gate_receipt(self.world, "revision-receipt.json")

    def test_gate_consumes_actual_environment_contract_with_original_readers(self) -> None:
        original_replay, original_equivalence = arm.replay, arm.receipts_equivalent
        receipt = self.create()
        result = gate.replay(self.world["gate_root"], receipt)
        self.assertEqual(result["status"], "passed")
        self.assertIs(arm.replay, original_replay)
        self.assertIs(arm.receipts_equivalent, original_equivalence)
        before = self.fixture.environment_receipt(receipt, "before")
        environment = arm.replay(self.fixture.gate_root, before.relative_to(self.fixture.gate_root).as_posix())
        self.assertEqual(environment["phase"], "gate_before")
        (self.fixture.gate_root / environment["facts"]["path"]).unlink()
        with self.assertRaises(gate.GateReceiptError):
            gate.replay(self.fixture.gate_root, receipt)

    def test_legacy_mode_or_wrong_candidate_is_rejected_before_new_gate_files(self) -> None:
        sources._write_json(self.fixture.config, {})
        with self.assertRaisesRegex(AssertionError, "实读门禁环境"):
            self.create()
        self.assertFalse(self.fixture.gate_root.exists())
        sources._write_json(self.fixture.config, {"gate_environment_mode": sources.REAL_GATE_ENVIRONMENT})
        self.world["candidate_id"] = "已取代的旧候选"
        with self.assertRaises(AssertionError):
            self.create()
        self.assertFalse(self.fixture.gate_root.exists())


class PrivateRevisionEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="private-revision-entry-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clean = {"git": {"commit": "e" * 40, "status_porcelain": ""}, "files": {},
                      "formal_registry_keys": {"client_sources": [], "delivery_executors": [], "delivery_sources": []}}

    def synthetic_metrics(self) -> dict:
        """只用于报告消费回归；这些内存记录没有运行身份，不能提交 pre-A3。"""

        return {"returncode": 0, "upstream_requests": 0, "isolation": "degraded", "scenarios": [
            {"scenario": name, "campaign_id": "同一内存夹具Campaign",
             "checks": {key: {"passed": True} for key in outer.SIX_CHECKS}}
            for name in outer.REVISION_CONTINUOUS_SCENARIOS]}

    def execute(self, exercise) -> tuple[int, dict]:
        with mock.patch.object(entry, "source_snapshot", return_value=self.clean), \
                mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous", new=exercise):
            return entry.execute_private_chain(self.root / "new-run")

    def test_describe_never_executes_or_registers_private_chain(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as stream, mock.patch.object(entry, "execute_private_chain") as execute:
            self.assertEqual(entry.main(["--describe"]), 0)
        description = json.loads(stream.getvalue())
        self.assertEqual(description["scenarios"], list(outer.REVISION_CONTINUOUS_SCENARIOS))
        self.assertEqual(len(description["scenarios"]), 8)
        self.assertFalse(description["formal_certification"])
        execute.assert_not_called()
        self.assertNotIn("_run_pending_revision_continuous", unittest.defaultTestLoader.getTestCaseNames(outer.ClaudeFaultChainTests))
        if description["pending_formal_registration"]:
            self.assertNotIn(description["chain_id"], pre_a3.CLAUDE_REAL_CHAIN_IDS)
            self.assertNotIn(description["chain_id"], release.RELEASE_REQUIRED_CHAINS["claude"])
        else:
            self.assertIn(description["chain_id"], pre_a3.CLAUDE_REAL_CHAIN_IDS)
            self.assertIn(description["chain_id"], release.RELEASE_REQUIRED_CHAINS["claude"])

    def test_original_failure_is_preserved_and_propagated(self) -> None:
        def fail(case):
            case.real_chain_metrics = {"returncode": 1, "scenarios": []}
            raise AssertionError("轻量注入：vc4-plan-r2 拒绝")
        code, report = self.execute(fail)
        self.assertEqual((code, report["result"], report["failures"], report["skips"]), (1, "failed_or_incomplete", 1, 0))
        self.assertFalse(report["formal_certification"])
        self.assertIn("vc4-plan-r2 拒绝", (self.root / "new-run/首败诊断.txt").read_text())
        self.assertIn("FAILED", (self.root / "new-run/tests.log").read_text())

    def test_complete_local_report_remains_uncertified(self) -> None:
        metrics = self.synthetic_metrics()
        def exercise(case):
            case.real_chain_metrics = metrics
        code, report = self.execute(exercise)
        self.assertEqual((code, report["result"]), (0, "passed_local_fixture_chain"))
        self.assertTrue(report["eight_scenarios_six_checks_complete"])
        self.assertTrue(report["same_campaign"])
        self.assertFalse(report["formal_certification"])
        self.assertFalse(report["formal_registry_or_certification_written"])

    def test_incomplete_or_mixed_campaign_report_is_not_promoted(self) -> None:
        for fault in ("missing-check", "mixed-campaign", "malformed"):
            with self.subTest(fault=fault):
                metrics = self.synthetic_metrics()
                if fault == "missing-check":
                    del metrics["scenarios"][-1]["checks"]["history"]
                elif fault == "mixed-campaign":
                    metrics["scenarios"][-1]["campaign_id"] = "另一个Campaign"
                else:
                    metrics["scenarios"][-1] = None
                def exercise(case):
                    case.real_chain_metrics = metrics
                with mock.patch.object(entry, "source_snapshot", return_value=self.clean), \
                        mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous", new=exercise):
                    code, report = entry.execute_private_chain(self.root / fault)
                self.assertEqual((code, report["result"]), (1, "failed_or_incomplete"))

    def test_dirty_source_and_existing_evidence_reject_before_execution(self) -> None:
        dirty = {**self.clean, "git": {"commit": "e" * 40, "status_porcelain": " M checker.py\n"}}
        with mock.patch.object(entry, "source_snapshot", return_value=dirty), \
                mock.patch.object(outer.ClaudeFaultChainTests, "_run_pending_revision_continuous") as exercise:
            code, report = entry.execute_private_chain(self.root / "dirty-run")
        self.assertEqual((code, report["result"], report["tests_run"]), (1, "refused_dirty_source", 0))
        exercise.assert_not_called()
        existing = self.root / "old-evidence"
        existing.mkdir()
        sentinel = existing / "旧失败原件.txt"
        sentinel.write_bytes(b"immutable failure\n")
        with self.assertRaises(FileExistsError):
            entry.execute_private_chain(existing)
        self.assertEqual(sentinel.read_bytes(), b"immutable failure\n")


if __name__ == "__main__":
    unittest.main()
