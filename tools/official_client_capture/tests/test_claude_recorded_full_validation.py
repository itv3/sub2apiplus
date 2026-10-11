"""录制完整链准备的轻量拒绝例；不进入重链、不登记 VC-0→VC-6 认证。"""

from __future__ import annotations

import os
import copy
import contextlib
import io
import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as driver
from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as vc1
from tools.official_client_capture.tests import claude_recorded_replay as replay


class RecordedFullPreparationTests(unittest.TestCase):
    """完整来源和门禁收据用正式只读合同重验，轻量测试不能制造完整链登记。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / "staging/data"
        vc1._create_world_roots(self.data)

    def test_mac_source_requires_actual_first_level_staging(self) -> None:
        driver._require_client_staging(self.data)
        with self.assertRaisesRegex(source.replay.ReplayError, "首层 <world>/staging"):
            driver._require_client_staging(self.data.parent.parent / "other-staging/data")

    def test_full_inputs_replay_real_policy_and_preserve_rule_denominator(self) -> None:
        from tools.official_client_capture import claude_upgrade_wire_projection as wire
        from tools.official_client_capture import claude_upgrade_release as release
        from tools.official_client_capture.tests import test_claude_upgrade_profile as fixture

        originals = {path: source.binding(path) for path in (fixture.ACTIVE_PROFILE, fixture.ACTIVE_WIRE, fixture.ACTIVE_CATALOG)}
        inputs = driver._inputs(self.data)
        policy = wire.load_projection_policy(source.bound(inputs["bindings"]["active_projection_policy"], root=self.data))
        self.assertEqual(policy["version"], vc1.BASELINE)
        self.assertEqual(policy["models"]["ordered"], ["claude-sonnet-5"])
        review = driver.validate_frozen_inputs(self.data, inputs)
        self.assertEqual((review["version"], len(wire.required_probes(review))), (source.VERSION, 23))
        self.assertEqual((len(inputs["jobs"]), len(inputs["candidate_jobs"])), (9, 9))
        self.assertEqual({side: sum(len(probes) for probes in inputs[side].values()) for side in ("jobs", "candidate_jobs")},
                         {"jobs": 57, "candidate_jobs": 57})
        self.assertEqual(len(source.read(source.bound(inputs["bindings"]["rules"], root=self.data))["required_rules"]), 40)
        self.assertEqual(len(source.read(source.bound(inputs["bindings"]["active_profile"], root=self.data))["rules"]), 40)
        origin = source.read(source.bound(inputs["bindings"]["active_projection_origin"], root=self.data))
        self.assertFalse(origin["formal_version_semantics_certified"])
        original_policy, _binding = release.active_projection_policy(source.VERSION)
        self.assertEqual((origin["model_scope"]["original_required_probe_count"], origin["model_scope"]["fixture_required_probe_count"]),
                         (len(wire.required_probes(original_policy)), len(wire.required_probes(review))))
        self.assertEqual(origin["model_scope"]["original_models"], ["claude-sonnet-5", "claude-opus-5", "claude-fable-5"])
        self.assertFalse(origin["model_scope"]["formal_model_scope_certified"])
        for path, expected in originals.items():
            self.assertEqual(source.binding(path), expected)
        # 同一判据补核后继的完整主模型选择；原九 Job 输入仍保持原字节语义。
        complete_data = self.data / "main-complete"
        vc1._create_world_roots(complete_data)
        complete = driver._inputs(complete_data, complete_main_model=True)
        complete_review = driver.validate_frozen_inputs(complete_data, complete)
        self.assertEqual(complete["recorded_control_scope"], driver.COMPLETE_MAIN_MODEL_SCOPE)
        self.assertEqual((len(complete["jobs"]), len(complete["candidate_jobs"])), (12, 12))
        self.assertEqual({side: sum(map(len, complete[side].values())) for side in ("jobs", "candidate_jobs")},
                         {"jobs": 79, "candidate_jobs": 79})
        self.assertEqual(complete_review["models"]["ordered"], ["claude-sonnet-5"])
        self.assertEqual(len(source.read(source.bound(complete["bindings"]["rules"], root=complete_data))["required_rules"]), 40)
        self.assertNotIn("official-model-capability-claude-opus-5", complete["jobs"])
        self.assertNotIn("official-model-capability-claude-fable-5", complete["jobs"])

        # 完整模板只做集合预检的通过对照；此报告不运行断言、不授予版本认证。
        from tools.official_client_capture import claude_rule_assertion
        from tools.official_client_capture import claude_upgrade_plan as plan
        from tools.official_client_capture import claude_upgrade_probe_preflight as preflight

        profile = claude_rule_assertion.load_profile(
            vc1.REPOSITORY / "tools/official_client_capture/claude_rule_expectations_2_1_226.json",
            expected_claude_version=source.VERSION, verify_frozen_digest=True,
        )
        complete = source.read(plan.scenario_manifest_path("2.1.285"))
        complete_report = preflight.probe_coverage_preflight(profile, complete)
        self.assertEqual(complete_report["status"], "passed")
        self.assertEqual(complete_report["missing_probe_ids"], {"official": [], "candidate": []})

        # 原CLI完整路径必须接上冻结前校验；局部VC-3不冒充完整40规则资格。
        for through in ("VC-3", "VC-6"):
            with self.subTest(through=through), mock.patch.object(vc1, "namespace_main", return_value={"status": "仅核接线"}) as namespace, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(driver.main(["--data", str(self.data), "--recorded", "/fixture/recorded", "--freeze", "/fixture/freeze",
                                              "--parent-namespaces", "{}", "--through", through]), 0)
                self.assertIs(namespace.call_args.kwargs["input_verifier"],
                              driver._full_vc5_input_verifier if through == "VC-6" else None)
        # 57个来源的局部控制资格保留，但完整40规则必须在快照与freeze之前一次报告缺失实验。
        bindings_before = copy.deepcopy(inputs["bindings"])
        with self.assertRaises(preflight.ProbeCoverageError) as failure:
            driver._full_vc5_input_verifier(self.data, inputs)
        report = failure.exception.report
        self.assertEqual((report["rule_count"], report["files_written"], report["live_request_count"]), (40, 0, 0))
        for side in ("official", "candidate"):
            self.assertEqual((len(report["required_probe_ids"][side]), len(report["planned_probe_ids"][side]),
                              len(report["missing_probe_ids"][side])), (71, 57, 22))
        conn021 = next(row for row in report["rules"] if row["rule_id"] == "SPEC-CONN-021")
        self.assertEqual({side: len(conn021["missing_probe_ids"][side]) for side in ("official", "candidate")},
                         {"official": 11, "candidate": 11})
        self.assertEqual(inputs["bindings"], bindings_before)
        self.assertFalse((self.data / "control/snapshot").exists())
        self.assertFalse((self.data / "official/claude-code-2.1.226").exists())
        for path, expected in originals.items():
            self.assertEqual(source.binding(path), expected)


    def test_default_vc1_still_uses_original_seven_complete_jobs(self) -> None:
        inputs = vc1._inputs(self.data)
        self.assertEqual((len(inputs["jobs"]), len(inputs["candidate_jobs"])), (7, 7))
        self.assertEqual(sum(map(len, inputs["jobs"].values())), 42)
        self.assertNotIn("source_selections", inputs)

    def test_incomplete_full_job_and_missing_job_are_rejected_before_stage(self) -> None:
        for omission in ("job", "step"):
            with self.subTest(omission=omission):
                data = self.data / omission
                inputs = driver._inputs(data)
                scenarios = source.bound(inputs["bindings"]["scenarios"], root=data)
                document = source.read(scenarios)
                if omission == "job":
                    document["capture_jobs"] = [job for job in document["capture_jobs"] if job["id"] != "official-generation-params"]
                else:
                    next(job for job in document["capture_jobs"] if job["id"] == "candidate-generation-params")["steps"].pop()
                scenarios.write_text(json.dumps(document), encoding="utf-8")
                inputs["bindings"]["scenarios"] = inputs["bindings"]["full_scenarios"] = source.binding(scenarios)
                with self.assertRaisesRegex(replay.ReplayError, "Job 未闭合"):
                    driver.validate_frozen_inputs(data, inputs)

    def test_review_bytes_and_actual_mapping_drift_are_rejected(self) -> None:
        inputs = driver._inputs(self.data)
        changed_scope = copy.deepcopy(inputs)
        changed_scope["recorded_control_scope"] = "unknown"
        with self.assertRaisesRegex(replay.ReplayError, "来源选择合同未知"):
            driver.validate_frozen_inputs(self.data, changed_scope)
        changed = copy.deepcopy(inputs)
        changed["candidate_jobs"]["candidate-generation-params"].pop()
        with self.assertRaisesRegex(replay.ReplayError, "Job 映射漂移"):
            driver.validate_frozen_inputs(self.data, changed)
        path = source.bound(inputs["bindings"]["review_projection_policy"], root=self.data)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "绑定摘要或字节数漂移"):
            driver.validate_frozen_inputs(self.data, inputs)

    def test_missing_registered_source_is_rejected_before_phase(self) -> None:
        from tools.official_client_capture.tests import test_claude_recorded_replay as fixtures

        fixture = fixtures.ClaudeRecordedSnapshotTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        snapshot = replay.build_snapshot(fixture.staging, recorded=fixture.recorded)
        with self.assertRaisesRegex(replay.ReplayError, "candidate 冻结 Job 缺少已登记来源"):
            source.require_registered_jobs(snapshot, {"jobs": {"official-test": ["v4-fixture-probe"]},
                                                       "candidate_jobs": {"candidate-test": ["v4-thinking-display-summarized"]}})

    def test_recovery_aggregation_keeps_full_jobs_and_same_review_scope(self) -> None:
        from tools.official_client_capture.tests import claude_chain_driver as recovery

        inputs = recovery.inputs(self.data)
        review = driver.validate_frozen_inputs(self.data, inputs)
        self.assertEqual(review["models"]["ordered"], ["claude-sonnet-5"])
        self.assertNotEqual(inputs["bindings"]["scenarios"], inputs["bindings"]["full_scenarios"])

    def test_actual_grouped_side_must_keep_formal_required_probes(self) -> None:
        from tools.official_client_capture.tests import claude_chain_driver as recovery

        inputs = recovery.inputs(self.data)
        scenarios = source.bound(inputs["bindings"]["scenarios"], root=self.data)
        document = source.read(scenarios)
        for job in document["capture_jobs"]:
            if job["phase"] == "candidate":
                job["steps"] = [step for step in job["steps"] if step["probe_id"] != "v4-thinking-display-summarized"]
        scenarios.write_text(json.dumps(document), encoding="utf-8")
        inputs["bindings"]["scenarios"] = source.binding(scenarios)
        inputs["candidate_jobs"] = {job["id"]: [step["probe_id"] for step in job["steps"]]
                                    for job in document["capture_jobs"] if job["phase"] == "candidate"}
        with self.assertRaisesRegex(replay.ReplayError, "candidate 冻结审核策略缺少正式 required probes"):
            driver.validate_frozen_inputs(self.data, inputs)
        from tools.official_client_capture import claude_rule_assertion
        from tools.official_client_capture import claude_upgrade_probe_preflight as preflight

        profile = claude_rule_assertion.load_profile(
            vc1.REPOSITORY / "tools/official_client_capture/claude_rule_expectations_2_1_226.json",
            expected_claude_version=source.VERSION, verify_frozen_digest=True,
        )
        # 上述thinking探测仍由Wire required合同检查；另选画像确实引用的baseline验证分侧及reference。
        comparison_document = copy.deepcopy(document)
        for job in comparison_document["capture_jobs"]:
            if job["phase"] == "candidate":
                job["steps"] = [step for step in job["steps"] if step["probe_id"] != "v4-replay-baseline"]
        report = preflight.probe_coverage_preflight(profile, comparison_document)
        self.assertIn("v4-replay-baseline", report["missing_probe_ids"]["candidate"])
        self.assertNotIn("v4-replay-baseline", report["missing_probe_ids"]["official"])
        self.assertTrue(any(row["selector"] == "reference" for row in report["problems"]))

    def test_real_all_57_registered_sources_pass_v5_and_exact_origin_bindings(self) -> None:
        from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

        configured = os.environ.get("CLAUDE_2_1_226_RELAY_FIXTURE")
        if not configured:
            self.skipTest("需显式登记完整只读录制根；不以本用例替代当前 ARM64 原生取证")
        inputs = driver._inputs(self.data)
        snapshot = replay.build_snapshot(self.data / "control/real-snapshot", recorded=Path(configured),
                                         source_selections=tuple(inputs["source_selections"]))
        source.require_registered_jobs(snapshot, inputs)
        state = {"snapshot": snapshot, "freeze": {"binary_sha256": "4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555"}}
        facts = source.read(source.bound(inputs["bindings"]["version_facts"], root=self.data))
        probes = {probe for items in inputs["jobs"].values() for probe in items}
        self.assertEqual(len(probes), 57)
        index = replay.load_snapshot(snapshot)
        self.assertEqual(len(index["probes"]), 79)
        for probe in sorted(probes):
            with self.subTest(probe=probe):
                origin = source.origin_probe(state, probe)
                manifest = origin["manifest"]
                self.assertEqual(manifest["status"], "complete")
                self.assertTrue(manifest["m_binding"]["complete"])
                self.assertTrue(manifest["secret_scan"]["passed"])
                self.assertEqual(manifest["cleanup"], {"relay_stopped": True, "hosts_restored": True})
                attempt = origin["manifest_path"].parent
                catalog = source.read(attempt.parents[2] / "scenario-catalog.json")
                definition = next(row for row in catalog["probes"] if row["probe_id"] == probe)
                self.assertEqual(definition, json.loads(json.dumps(dataclasses.asdict(v5.v4.get_probe(probe)))))
                summary = copy.deepcopy(manifest["scenario_result"])
                self.assertTrue(summary["valid"])
                current = v5.get_probe(probe)
                if "tool.advisor" in current.dimensions and current.require_debug_log:
                    summary["inner_result"]["debug_declarations"] = v5._debug_declarations(attempt / "results", facts)
                dimension = v5.validate_v5_probe_evidence(probe_id=probe, relay_root=attempt / "relay", scenario_summary=summary,
                                                        relay_integrity=manifest["relay_integrity"], pcap_receipt=manifest["tls_p_channel"],
                                                        version_facts=facts)
                self.assertEqual(dimension["result"], "passed")
                self.assertEqual(dimension["actual_wire_inventory"], manifest["dimension_evidence"]["actual_wire_inventory"])
        replay.assert_source_untouched(snapshot)

    def test_gate_environment_real_replay_and_tamper_rejection(self) -> None:
        from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
        from tools.official_client_capture import claude_upgrade_plan as plan
        from types import SimpleNamespace

        root = self.data / "control/gate"
        root.mkdir(mode=0o700)
        # 单测仅给真实 collector 提供 ARM64 宿主身份；完整链的 require_isolation 不允许这种替身。
        with mock.patch.object(arm.platform, "machine", return_value="aarch64"):
            bindings = driver._gate_environment(root, self.data, "fixture-gate")
        before = arm.replay(root, bindings["before"]["path"])
        after = arm.replay(root, bindings["after"]["path"])
        self.assertTrue(arm.receipts_equivalent(root, before, root, after))
        self.assertTrue(source.read(self.data / "control/fixture-gate-observation/origin.json")["fixture_only"])
        facts = root / "environment/after-facts.json"
        facts.write_bytes(facts.read_bytes() + b" ")
        with self.assertRaises(arm.Arm64EnvironmentReceiptError):
            arm.replay(root, bindings["after"]["path"])
        with self.assertRaises(arm.Arm64EnvironmentReceiptError):
            arm.receipts_equivalent(root, before, root, after)

        # 原 token 前检保持真实执行；合成值只在已校验的录制运行来源内生效，退出和异常都恢复环境。
        state = {"data_root": str(self.data), "inputs": {"candidate_configuration": {"service_container": "fixture-service"}}}
        configuration = {**state["inputs"]["candidate_configuration"], "capture_container": "capture-cli",
                         "synthetic_capture_container": "capture-cli-synthetic", "runtime_image": "runtime@sha256:" + "d" * 64}
        manifest = {"campaign_id": "fixture-runtime", "configuration": configuration}
        jobs = [SimpleNamespace(steps=[{"environment": {"GATEWAY_TRIGGER": plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH}}])]
        with mock.patch.dict(os.environ, {"ADMIN_BEARER_TOKEN": "", "ADMIN_BEARER_TOKEN_FILE": ""}):
            with driver.runtime_sources(state):
                with self.assertRaisesRegex(candidate.ConfigurationError, "管理 token"):
                    candidate.require_admin_token_for(jobs)
            build = source.write(self.data / "evidence/campaigns/fixture-runtime/candidates" / driver.CANDIDATE / "build-receipt.json", {
                "candidate_id": driver.CANDIDATE, "fixture_only": True,
                "image": {"image_id": "sha256:" + "b" * 64, "reference": "candidate@sha256:" + "a" * 64},
                "profile": {"profile_digest": "1" * 64, "wire_sha256": "2" * 64,
                            "release_sha256": "3" * 64, "bundle_sha256": "4" * 64},
                "source": {"tree_sha256": "5" * 64}, "build": {"build_id": "fixture-build"}, "deployed_version": "fixture-only",
            })
            profile = source.write(self.data / "control/fixture-profile-stage.json", {"fixture_only": True})
            runtime = source.write(self.data / "control/full-runtime-source.json", {
                "fixture_only": True, "purpose": "recorded_control_runtime_source", "build_receipt": source.binding(build),
                "profile_stage": source.binding(profile), "candidate_configuration": configuration,
                "formal_version_semantics_certified": False,
            })
            with mock.patch.object(upgrade, "load_campaign_manifest", return_value=manifest):
                with self.assertRaisesRegex(RuntimeError, "作用域退出"):
                    with driver.runtime_sources(state):
                        candidate.require_admin_token_for(jobs)
                        self.assertEqual(os.environ["ADMIN_BEARER_TOKEN"], "recorded-fixture-only-not-a-real-admin-token")
                        raise RuntimeError("作用域退出")
                self.assertEqual((os.environ["ADMIN_BEARER_TOKEN"], os.environ["ADMIN_BEARER_TOKEN_FILE"]), ("", ""))
                with self.assertRaisesRegex(candidate.ConfigurationError, "管理 token"):
                    candidate.require_admin_token_for(jobs)
                original = runtime.read_bytes()
                runtime.write_text(json.dumps({**source.read(runtime), "fixture_only": False}), encoding="utf-8")
                with self.assertRaisesRegex(replay.ReplayError, "明确的控制夹具"):
                    with driver.runtime_sources(state):
                        self.fail("未登记来源不能启用合成凭据")
                runtime.write_bytes(original)

    def test_pending_private_shell_delegates_cli_and_preserves_failure(self) -> None:
        from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as shell

        case = shell.ClaudeRecordedVC1ChainTests(methodName="_run_pending_full_validation")
        observed = {}
        original_temp = tempfile.gettempdir()
        original_readlink = os.readlink

        def external_command(argv, **options):
            if argv == ["unshare", "-m", "-n", "true"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            observed["argv"], observed["options"] = argv, options
            data = Path(argv[argv.index("--data") + 1])
            data.mkdir(parents=True, exist_ok=True)
            observed["world"] = data.parent.parent
            self.addCleanup(shutil.rmtree, observed["world"])
            return subprocess.CompletedProcess(argv, 23, "真实外部边界 stdout 标记\n", "真实外部边界 stderr 标记\n")

        environment = {"CLAUDE_2_1_226_RELAY_FIXTURE": "/fixture/relay", "CLAUDE_2_1_226_FREEZE_FIXTURE": "/fixture/freeze",
                       shell.RECORD_DIR_ENV: str(self.data / "external-records"), shell.RECORDS_ENV: ""}
        with mock.patch.dict(os.environ, environment), mock.patch.object(shell.platform, "system", return_value="Linux"), \
                mock.patch.object(shell.platform, "machine", return_value="aarch64"), \
                mock.patch.object(shell.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(shell.shutil, "which", return_value="/fixture/tool"), \
                mock.patch.object(shell.os, "readlink", side_effect=lambda path: "parent-" + path.rsplit("/", 1)[-1]
                                  if str(path).startswith("/proc/self/ns/") else original_readlink(path)), \
                mock.patch.object(shell.subprocess, "run", side_effect=external_command):
            with self.assertRaises(AssertionError):
                case._run_pending_full_validation()
        command = observed["argv"]
        self.assertIn(driver.ACTIVATION_MODULE, command)
        self.assertEqual(command[command.index("--through") + 1], "VC-6")
        self.assertIn("--complete-main-model", command)
        self.assertEqual(Path(command[command.index("--data") + 1]), observed["world"] / "staging/data")
        record = source.read(observed["world"] / "record.json")
        self.assertEqual(record["status"], "unverified")
        self.assertEqual(record["returncode"], 23)
        self.assertFalse(record["formal_version_semantics_certified"])
        self.assertIsNone(record["report_payload"])
        self.assertEqual(source.bound(record["stdout"]).read_text(encoding="utf-8"), "真实外部边界 stdout 标记\n")
        self.assertEqual(source.bound(record["stderr"]).read_text(encoding="utf-8"), "真实外部边界 stderr 标记\n")
        self.assertEqual(tempfile.gettempdir(), original_temp)
        self.assertNotIn("_run_pending_full_validation", unittest.defaultTestLoader.getTestCaseNames(shell.ClaudeRecordedVC1ChainTests))
        # 首跑已过去两小时也不扣本轮挂死预算；原失败与位置凭证仍逐字保留。这里只核外壳，不授予完整链认证。
        world = observed["world"]
        original_record = (world / "record.json").read_bytes()
        location_path = self.data / "external-records/complete-main-model-location.json"
        original_location = location_path.read_bytes()
        first_started = source.read(location_path)["started_at_epoch"]
        second = {}
        def resumed_command(argv, **options):
            if argv == ["unshare", "-m", "-n", "true"]:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            second["argv"], second["options"] = argv, options
            data = Path(argv[argv.index("--data") + 1])
            state = source.write(data / "control/shell-test-source.json", {"fixture_only": True})
            continuation = source.write(data / "control/shell-test-continuation.json", {"fixture_only": True})
            source.write(data / "control/recorded-full-chain-result.json", {
                "schema_version": driver.FULL_SCHEMA, "registered_chains": ["vc-chain.claude-full-validation-only"],
                "status": "passed", "scope": "VC-0→VC-6", "fixture_only": True, "full_vc0_vc6_certified": True,
                "formal_version_semantics_certified": False, "network_requests_this_run": 0,
                "checkpoints": {f"VC-{index}": {} for index in range(7)},
                "source_state": source.binding(state), "continuation": source.binding(continuation),
            })
            return subprocess.CompletedProcess(argv, 0, "外壳短验来源\n", "")
        with mock.patch.dict(os.environ, environment), mock.patch.object(shell.platform, "system", return_value="Linux"), \
                mock.patch.object(shell.platform, "machine", return_value="aarch64"), \
                mock.patch.object(shell.os, "geteuid", return_value=0, create=True), \
                mock.patch.object(shell.shutil, "which", return_value="/fixture/tool"), \
                mock.patch.object(shell.os, "readlink", side_effect=lambda path: "parent-" + path.rsplit("/", 1)[-1]
                                  if str(path).startswith("/proc/self/ns/") else original_readlink(path)), \
                mock.patch.object(shell.subprocess, "run", side_effect=resumed_command), \
                mock.patch.object(shell.tempfile, "mkdtemp", side_effect=AssertionError("续作不能重建原世界")), \
                mock.patch.object(shell.time, "time", return_value=first_started + 7200), \
                mock.patch.object(shell.time, "monotonic", side_effect=[1000.0, 1000.25, 1001.0]):
            case._run_pending_full_validation()
        budget = shell.certification.claude_recorded_time_budget("vc-chain.claude-full-validation-only")
        self.assertEqual(second["options"]["timeout"], budget["chain_timeout_seconds"])
        self.assertEqual(second["argv"], command)
        self.assertEqual((world / "record.json").read_bytes(), original_record)
        self.assertEqual(location_path.read_bytes(), original_location)
        resumed = source.read(world / "invocations/0001/record.json")
        self.assertEqual((resumed["seconds"], resumed["invocation_seconds"], resumed["first_started_at_epoch"]), (7200, 1, first_started))
        self.assertEqual((resumed["metrics"]["seconds"], resumed["metrics"]["cumulative_wall_seconds"]), (1, 7200))

    def test_successor_completed_action_reads_original_files_without_repeating_producer(self) -> None:
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        origin = source.write(self.data / "control/origin.json", {"fixture_only": True})
        record_root = self.data / "private-stops"
        record_root.mkdir()
        artifact = source.write(self.data / "control/input.json", {"value": "原字节"})
        action = mock.Mock(return_value={"source": source.binding(artifact)})
        verified = mock.Mock()
        token = driver._SUCCESSOR.set({"data": self.data, "source_state": source.binding(origin)})
        self.addCleanup(driver._SUCCESSOR.reset, token)
        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(record_root)}):
            first = driver._control_step("input", action, verify=verified, intent={"choice": "原选择"})
            self.assertEqual(driver._control_step("input", action, verify=verified, intent={"choice": "原选择"}), first)
            self.assertEqual((action.call_count, verified.call_count), (1, 2))
            with self.assertRaisesRegex(replay.ReplayError, "停点意图漂移"):
                driver._control_step("input", action, intent={"choice": "另一次选择"})
            artifact.write_text('{"value":"改变"}\n', encoding="utf-8")
            with self.assertRaisesRegex(replay.ReplayError, "绑定摘要或字节数漂移"):
                driver._control_step("input", action, intent={"choice": "原选择"})
        self.assertEqual(action.call_count, 1)
        self._assert_recorded_assertion_timeout_selection()
        self._assert_recorded_compiled_action_readers()

    def _assert_recorded_compiled_action_readers(self) -> None:
        """沿既有 UID 核 failed／ABORT 的读侧接线；真实编译规范化不替换，不冒称现场对账通过。

        外围已验真的父 run、PREPARED 和基线读结果使用显式夹具；原件绑定、动作合同、输出冻结和
        ABORT 自摘要校验均实跑。实际监督器与正式对账仍由既有真实批次测试覆盖。
        """
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_baseline as baselines
        from tools.official_client_capture import codex_upgrade_reconciler as reconciler
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor
        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

        for index, baseline in enumerate((None, 1)):
            with self.subTest(frozen_baseline=baseline):
                campaign = self.data / "evidence/campaigns" / f"compiled-reader-{index}"
                candidate, sequence, phase = "claude-recorded-frozen-candidate", 701 + index, "VC-5"
                action_path = self.data / "control/actions" / f"{sequence}-assert-rules.json"
                action = {"action_id": "assert-rules", "operation": "VC-5:assert-rules", "timeout_seconds": 900.0,
                          "command": ["/usr/bin/python3", "/fixture/claude_build_rule_assertion_results.py",
                                      "--evaluation-baseline", str(0 if baseline is None else baseline)],
                          "item_ids": ["assert-rules"]}
                action_plan = {"schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": ["assert-rules"],
                               "reuse_item_ids": [], "actions": [action]}
                source.write(action_path, action_plan)
                assertions = (campaign / "assertions" / candidate if baseline is None else
                              campaign / "candidates" / candidate / "revisions/b1/assertions")
                baseline_commit = {"stage_sources": {"assertions": {"source": "local",
                                   "target": assertions.relative_to(campaign).as_posix()}}}
                # b1 的读侧接缝只提供冻结路径；输出声明仍由原编译器计算，不能随最新基线改变。
                with mock.patch.object(baselines, "read_evaluation_baseline_commit", return_value=baseline_commit) as baseline_reader, \
                        mock.patch.object(upgrade, "_current_evaluation_baseline", side_effect=AssertionError("不得读最新基线")):
                    compiled = upgrade._freeze_evaluation_output_bindings(
                        campaign, candidate, 0 if baseline is None else baseline, [action])
                    self.assertNotIn("output_bindings", action)
                    self.assertEqual(compiled[0]["output_bindings"], [
                        (assertions / "checkpoints").relative_to(campaign).as_posix(),
                        (assertions / "evaluation-run.json").relative_to(campaign).as_posix()])
                    inner = {"campaign_id": campaign.name, "candidate_id": candidate, "evaluation_baseline": baseline,
                             "phase": phase, "actions": compiled, "execute_items": ["assert-rules"], "reuse_items": []}
                    run_dir = self.data / "supervisor" / f"run-compiled-reader-{index}"
                    commit = {"fixture_only": True, "parent_run_dir": str(run_dir)}
                    source.write(upgrade._staging_commit_path(campaign, sequence, phase), commit)
                    for name, value in (("state.json", {"fixture_only": True, "state": "failed"}),
                                        ("campaign-run-manifest.json", inner), ("stop-receipt.json", {"fixture_only": True})):
                        source.write(run_dir / name, value)
                    receipt = campaign / "control" / reconciler.RECONCILIATION_DIR / f"run-{run_dir.name}" / reconciler.SUPERVISOR_RUN_RECEIPT_NAME
                    source.write(receipt, {"fixture_only": True, "campaign_id": campaign.name, "reservation_exists": False,
                                          "live_request_count": 0, "attempt_events_fabricated": False,
                                          "run": {"run_id": run_dir.name}})
                    attempt = upgrade._staging_sequence_dir(campaign, sequence, phase) / "attempt-1"
                    prepared = {"attempt": 1, "batch": {"batch_sha256": "b" * 64},
                                "manifest_sha256": "c" * 64, "run_manifest": inner}
                    abort = artifacts.build_staging_abort(
                        campaign_id=campaign.name, campaign_plan_sha256="a" * 64, phase=phase, sequence=sequence,
                        staging_attempt=1, stage="parent-run-create", failure_kind="prepare-failed",
                        error_type="FixtureError", root_cause_id="rc1-" + "d" * 20, batch_sha256="b" * 64,
                        manifest_sha256="c" * 64, parent_run_dir=None, parent_run_state=None,
                        reconciliation_receipt=None, recorded_at_utc="2026-10-12T00:00:00Z")
                    for name, value in (("PREPARED", {"fixture_only": True}), ("ABORT", abort),
                                        ("batch.json", prepared["batch"]), ("run-manifest.json", inner)):
                        source.write(attempt / name, value)
                    originals = {path: path.read_bytes() for root in (campaign, run_dir) for path in root.rglob("*") if path.is_file()}
                    originals[action_path] = action_path.read_bytes()
                    original_inner = copy.deepcopy(inner)
                    with contextlib.ExitStack() as patches:
                        for owner, name, value in (
                                (upgrade, "_read_staging_commit", commit),
                                (supervisor, "_read_state", {"state": "failed"}),
                                (supervisor, "_run_inner_manifest", inner),
                                (supervisor, "_staging_commit_for_run", commit),
                                (supervisor, "_audit_command", {"state": "failed", "audit_incomplete": False}),
                                (upgrade, "_load_prepared_staging_attempt", prepared),
                                (upgrade, "load_campaign_manifest", {"campaign_id": campaign.name}),
                                (upgrade, "_vc_campaign_plan", {"plan_sha256": "a" * 64})):
                            patches.enter_context(mock.patch.object(owner, name, return_value=value))
                        reconciled = patches.enter_context(mock.patch.object(upgrade, "_supervisor_run_reconciled", return_value=True))
                        freeze = patches.enter_context(mock.patch.object(upgrade, "_freeze_evaluation_output_bindings",
                                                                         wraps=upgrade._freeze_evaluation_output_bindings))
                        readers = {"failed": lambda: driver._recorded_failed_batch(campaign, action_path),
                                   "abort": lambda: driver._recorded_staging_abort(campaign, action_path, attempt)}
                        for reader_name, read in readers.items():
                            with self.subTest(reader=reader_name):
                                outcome = read()
                                self.assertEqual(outcome["action_plan"], source.binding(action_path))
                                freeze.assert_called_with(campaign, candidate, 0 if baseline is None else baseline, [action])
                                declared = copy.deepcopy(action_plan)
                                declared["actions"][0]["output_bindings"] = compiled[0]["output_bindings"]
                                action_path.write_text(json.dumps(declared), encoding="utf-8")
                                self.assertEqual(read()["action_plan"], source.binding(action_path))
                                declared["actions"][0]["output_bindings"] = ["wrong/output.json"]
                                action_path.write_text(json.dumps(declared), encoding="utf-8")
                                with self.assertRaisesRegex(upgrade.ConfigurationError, "output_bindings.*不一致"):
                                    read()
                                action_path.write_bytes(originals[action_path])
                                for field, value in (("command", ["changed"]), ("timeout_seconds", 1800.0),
                                                     ("operation", "VC-5:changed"), ("action_id", "changed"),
                                                     ("item_ids", ["changed"]), ("output_bindings", ["wrong/output.json"]),
                                                     ("output_bindings", None)):
                                    with self.subTest(compiled_drift=field, value=value):
                                        inner["actions"] = copy.deepcopy(compiled)
                                        if value is None:
                                            del inner["actions"][0][field]
                                        else:
                                            inner["actions"][0][field] = value
                                        with self.assertRaisesRegex(replay.ReplayError, "不闭合"):
                                            read()
                                        inner["actions"] = compiled
                                for field in ("execute_items", "reuse_items"):
                                    original = inner[field]
                                    inner[field] = ["changed"]
                                    with self.assertRaisesRegex(replay.ReplayError, "不闭合"):
                                        read()
                                    inner[field] = original
                                for invalid in (False, "", -1):
                                    inner["evaluation_baseline"] = invalid
                                    with self.assertRaisesRegex(upgrade.ConfigurationError, "评估基线号必须是非负整数"):
                                        read()
                                inner["evaluation_baseline"] = baseline
                        reconciled.return_value = False
                        with self.assertRaisesRegex(replay.ReplayError, "尚未正式对账闭合"):
                            readers["failed"]()
                        self.assertEqual(inner, original_inner)
                        # 非 VC-5 动作保持原字段；即使命令名相似，也不能擅自补评估输出。
                        ordinary = copy.deepcopy(action_plan)
                        ordinary["actions"][0]["operation"] = "VC-4:fixture"
                        freeze.reset_mock()
                        self.assertEqual(driver._recorded_compiled_actions(campaign, ordinary, "VC-4", {}), ordinary["actions"])
                        freeze.assert_not_called()
                        malformed = {**ordinary, "unregistered": True}
                        with self.assertRaisesRegex(artifacts.VCArtifactError, "字段不闭合"):
                            driver._recorded_compiled_actions(campaign, malformed, "VC-4", {})
                    if baseline is None:
                        baseline_reader.assert_not_called()
                    else:
                        self.assertTrue(baseline_reader.call_args_list)
                        self.assertTrue(all(call == mock.call(campaign, candidate, 1) for call in baseline_reader.call_args_list))
                    self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def _assert_recorded_assertion_timeout_selection(self) -> None:
        """沿既有 UID 核默认时限接线；这里只核驱动选择，真实对账与批次验真仍由原控制链测试覆盖。"""
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture.tests import claude_fault_chain_world as stops

        campaign = self.data / "campaigns/claude-fixture"
        comparison = {"builder": {"script": "fixture.py", "config": {"path": "config.json"},
                                   "output": "results.json", "results_dir": "machine", "evaluation_baseline": 0}}
        with mock.patch.object(upgrade, "_load_stage_result", return_value=comparison), \
                mock.patch.object(vc1, "_batch", side_effect=[{}, RuntimeError("只检查默认动作参数")]) as batch:
            with self.assertRaisesRegex(RuntimeError, "只检查默认动作参数"):
                driver.compare_and_accept(self.data, campaign)
            current_action = batch.call_args.kwargs["action"]
            self.assertEqual(current_action["timeout_seconds"], 1800.0)
        old_options = {"phase": "VC-5", "action": {**current_action, "timeout_seconds": 900.0}}
        new_options = {"phase": "VC-5", "action": current_action}
        for initial_status in ("fresh", "complete", "started"):
            with self.subTest(assertion_stop=initial_status):
                record_root = self.data / ("assertion-stops-" + initial_status)
                record_root.mkdir()
                origin = source.write(self.data / "control" / (initial_status + "-origin.json"), {"fixture_only": True})
                active = {"data": self.data, "source_state": source.binding(origin)}
                token = driver._SUCCESSOR.set(active)
                try:
                    result = {"scope": "仅检查停点参数接线"}
                    producer = mock.Mock(return_value=result)
                    key = "recorded-full-step:batch:assert-rules"
                    with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(record_root)}), \
                            mock.patch.object(driver, "_verify_batch_result") as verify, \
                            mock.patch.object(driver, "_resume_undispatched_batch", return_value=result) as resume:
                        def run(**changes):
                            arguments = {"data": self.data, "campaign": campaign, "name": "assert-rules", "argv": [],
                                         **copy.deepcopy(new_options), **changes}
                            return driver._recorded_batch(producer, **arguments)
                        if initial_status == "started":
                            producer.side_effect = RuntimeError("原动作超时，尚待正式对账")
                            with self.assertRaisesRegex(RuntimeError, "原动作超时"):
                                run(**old_options)
                            producer.side_effect = None
                        elif initial_status == "complete":
                            self.assertEqual(run(**old_options), result)
                        originals = {path: path.read_bytes() for path in record_root.iterdir()}
                        if initial_status == "started":
                            # 原恢复入口拒绝时不得改停点；此模拟只证明接线，不能充当真实对账凭证。
                            resume.side_effect = replay.ReplayError("尚未正式对账闭合")
                            with self.assertRaisesRegex(replay.ReplayError, "尚未正式对账闭合"):
                                run()
                            self.assertEqual({path: path.read_bytes() for path in record_root.iterdir()}, originals)
                            resume.side_effect = None
                            resume.reset_mock()
                        self.assertEqual(run(), result)
                        expected_timeout = 1800.0 if initial_status == "fresh" else 900.0
                        saved = stops.load_state(key)
                        self.assertEqual(saved["intent"]["options"]["action"]["timeout_seconds"], expected_timeout)
                        self.assertEqual(saved["status"], "complete")
                        self.assertEqual(producer.call_count, 1)
                        if initial_status == "started":
                            resume.assert_called_once_with(producer, self.data, campaign, "assert-rules", [], **old_options)
                        else:
                            resume.assert_not_called()
                        if initial_status == "complete":
                            self.assertEqual({path: path.read_bytes() for path in record_root.iterdir()}, originals)
                        self.assertGreaterEqual(verify.call_count, 1)
                        if initial_status != "fresh":
                            frozen = {path: path.read_bytes() for path in record_root.iterdir()}
                            variants = [
                                {"argv": ["changed"]}, {"campaign": campaign / "other"}, {"data": self.data / "other"},
                                {"phase": "VC-4"}, {"reuse_items": ["other"]},
                                *({"action": {**current_action, field: value}} for field, value in (
                                    ("command", ["changed"]), ("operation", "VC-5:other"), ("action_id", "other"),
                                    ("item_ids", ["other"]), ("timeout_seconds", 450.0), ("timeout_seconds", 3600.0))),
                            ]
                            for change in variants:
                                with self.subTest(refused_change=change), self.assertRaisesRegex(replay.ReplayError, "停点意图漂移"):
                                    run(**change)
                            previous = active["source_state"]
                            active["source_state"] = {**previous, "sha256": "0" * 64}
                            with self.assertRaisesRegex(replay.ReplayError, "停点意图漂移"):
                                run()
                            active["source_state"] = previous
                            self.assertEqual({path: path.read_bytes() for path in record_root.iterdir()}, frozen)
                finally:
                    driver._SUCCESSOR.reset(token)

    def test_successor_partial_input_is_preserved_and_never_rebuilt(self) -> None:
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        origin = source.write(self.data / "control/origin.json", {"fixture_only": True})
        record_root = self.data / "private-stops"
        record_root.mkdir()
        token = driver._SUCCESSOR.set({"data": self.data, "source_state": source.binding(origin)})
        self.addCleanup(driver._SUCCESSOR.reset, token)
        partial = self.data / "control/partial.json"
        def fail_after_real_input():
            source.write(partial, {"fixture_only": True, "incomplete": True})
            raise RuntimeError("真实边界失败")
        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(record_root)}):
            with self.assertRaisesRegex(RuntimeError, "真实边界失败"):
                driver._control_step("inputs", fail_after_real_input)
            original = source.binding(partial)
            with self.assertRaisesRegex(replay.ReplayError, "保留半产物且禁止猜测重建"):
                driver._control_step("inputs", fail_after_real_input)
            self.assertEqual(source.binding(partial), original)

        # 评估视图是确定性冻结：真实相对画像绑定、policy 合同和原停点都参与短验；不宣称运行过 VC-3。
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_assertion_profile as profiles
        from tools.official_client_capture import codex_upgrade_project_ledger as ledger
        from tools.official_client_capture.tests import test_claude_recorded_candidate_source as fixture

        case = fixture.RecordedCandidateSourceTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        context = case.runtime_context()
        campaign = context.campaign_dir
        ledger.create_project_ledger(
            case.data / "upgrade-project-ledger-claude", project_id="recorded-domain-fixture",
            absolute_deadline_utc="2027-01-31T00:00:00Z", deadline_approved_by="fixture",
            estimation_policy="none", estimation_policy_approved_by="fixture", fixture_only=True,
            formal_open_limit=64, client="claude")
        profile_path = campaign / "assertions/approved-assertion-profile.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(vc1.REPOSITORY / "tools/official_client_capture/claude_rule_expectations_2_1_226.json", profile_path)
        case.state["inputs"]["recorded_control_scope"] = driver.COMPLETE_MAIN_MODEL_SCOPE
        state_path = source.write(case.data / "control/original-evaluation-state.json", case.state)
        state_binding = source.binding(state_path)
        original_state = state_path.read_bytes()
        frozen = {"path": profile_path.relative_to(campaign).as_posix(), "sha256": source.binding(profile_path)["sha256"]}
        evaluation_stops = case.data / "evaluation-stops"
        evaluation_stops.mkdir(mode=0o700)
        successor = {"data": case.data, "source_state": state_binding}
        token = driver._SUCCESSOR.set(successor)
        self.addCleanup(driver._SUCCESSOR.reset, token)
        original_loader, original_write = source.load_state, source.write
        policy_path = case.data / "control/recorded-evaluation-policy.json"
        result_path = case.data / "control/recorded-evaluation-source.json"
        key = "recorded-full-step:evaluation-state"
        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(evaluation_stops)}), \
                mock.patch.object(source, "load_state", side_effect=lambda path, digest: original_loader(path, digest, isolation=False)), \
                mock.patch.object(upgrade, "load_campaign_manifest", return_value=context.manifest), \
                mock.patch.object(profiles, "freeze_approved_assertion_profile"), \
                mock.patch.object(profiles, "load_approved_assertion_profile", return_value=frozen) as profile_reader:
            # 原失败尚未产生 policy，保存 started 后同一入口可以继续。
            profile_reader.side_effect = FileNotFoundError("原画像相对路径失败")
            with self.assertRaisesRegex(FileNotFoundError, "原画像相对路径失败"):
                driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign)
            started = stops.load_state(key)
            self.assertEqual(started["status"], "started")
            self.assertFalse(policy_path.exists())
            profile_reader.side_effect = None
            def interrupt_after_policy(path, document):
                if path == result_path:
                    raise RuntimeError("policy 已冻结后中断")
                return original_write(path, document)
            with mock.patch.object(source, "write", side_effect=interrupt_after_policy):
                with self.assertRaisesRegex(RuntimeError, "policy 已冻结后中断"):
                    driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign)
            self.assertEqual(stops.load_state(key), started)
            self.assertFalse(result_path.exists())
            policy_bytes = policy_path.read_bytes()
            self.assertEqual(source.read(policy_path)["assertion_profile"]["path"], str(profile_path))
            bad_policy = source.read(policy_path)
            bad_policy["target_version"] = "冲突画像"
            policy_path.write_text(json.dumps(bad_policy), encoding="utf-8")
            with self.assertRaisesRegex(replay.ReplayError, "不能改画像或来源意图"):
                driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign)
            policy_path.write_bytes(policy_bytes)
            profile_reader.return_value = {**frozen, "path": "../approved-assertion-profile.json"}
            with self.assertRaises(upgrade.ConfigurationError):
                driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign)
            profile_reader.return_value = frozen
            with self.assertRaises(replay.ReplayError):
                driver._recorded_evaluation_state(state_path, "f" * 64, campaign)
            self.assertEqual(stops.load_state(key), started)
            result = driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign)
            self.assertEqual((stops.load_state(key)["status"], policy_path.read_bytes(), state_path.read_bytes()),
                             ("complete", policy_bytes, original_state))
            result_bytes = result_path.read_bytes()
            with mock.patch.object(driver, "_freeze_recorded_evaluation_state", side_effect=AssertionError("完成项不能重做")) as producer:
                self.assertEqual(driver._recorded_evaluation_state(state_path, state_binding["sha256"], campaign), result)
                producer.assert_not_called()
            self.assertEqual(result_path.read_bytes(), result_bytes)

    def test_prepare_views_resume_original_started_with_real_reader_and_values_only(self) -> None:
        """实际投影只保存明示值；续作拒绝追加修订、原件损坏及意图漂移，不重派批次。"""
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import claude_upgrade_profile as profile
        from tools.official_client_capture import claude_upgrade_release as release
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        from tools.official_client_capture.tests import test_claude_upgrade_profile as fixture

        case = fixture._ProfileCase(methodName="runTest")
        case.setUpClass()
        self.addCleanup(case.tearDownClass)
        case.setUp()
        self.addCleanup(case.doCleanups)
        campaign = case.approved_vc2()
        draft = case.prepare_draft(campaign)
        origin = source.write(self.data / "control/prepare-origin.json", {"fixture_only": True})
        record_root = self.data / "prepare-stops"
        record_root.mkdir()
        context = {"data": self.data, "source_state": source.binding(origin)}
        token = driver._SUCCESSOR.set(context)
        self.addCleanup(driver._SUCCESSOR.reset, token)
        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(record_root)}):
            # 草案读到一半退出时，只允许重读原唯一首次草案。
            with self.assertRaisesRegex(RuntimeError, "纯读中断"):
                driver._control_step("vc3-draft-view", lambda: (_ for _ in ()).throw(RuntimeError("纯读中断")))
            draft_view = driver._prepare_view("vc3-draft-view", campaign)
            self.assertEqual(draft_view["gate"], draft["gate"])
            self.assertEqual(draft_view["review_sha256"], draft["review_sha256"])
            inputs_dir = case.write_prepare(case.resolved_prepare(campaign, Path(draft["draft_dir"])), case.root / "prepare-resolved")
            code, revised, stderr = case.revise_prepare(campaign, draft["revision"], inputs_dir)
            self.assertEqual((code, revised["gate"]["status"]), (2, "closed"), stderr)
            manifest = upgrade.load_campaign_manifest(campaign)
            receipt, files = profile.load_revision(campaign, manifest["campaign_id"], revised["revision"])
            catalog, active = profile._bound_active_paths(campaign, receipt)
            inputs = profile.collect_inputs(campaign, manifest, catalog, active, allowed_status=profile._PREPARE_STATUSES)
            gate = profile.evaluate_prepare(inputs, files)
            self.assertIsInstance(gate["derived"]["projection"], release.CatalogProjection)
            original_result = {"revision": revised["revision"], "draft_dir": Path(revised["draft_dir"]), "gate": gate,
                               "review_sha256": revised["review_sha256"]}
            original_files = {path.relative_to(campaign): path.read_bytes() for path in campaign.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(TypeError, "不支持运行对象：CatalogProjection"):
                driver._control_step("vc3-revision-view", lambda: original_result)
            key = "recorded-full-step:vc3-revision-view"
            started = stops.load_state(key)
            self.assertEqual(started["status"], "started")
            self.assertFalse(list(record_root.glob(".stop-*")))
            arguments = {"parent_revision": draft["revision"], "review_input": inputs_dir}
            revision_file = Path(revised["draft_dir"]) / profile.PREPARE_FILES["joint_inputs"]
            original_raw = revision_file.read_bytes()
            revision_file.write_bytes(original_raw + b" ")
            with self.assertRaises(profile.ConfigurationError):
                driver._prepare_view("vc3-revision-view", campaign, **arguments)
            self.assertEqual(stops.load_state(key), started)
            revision_file.write_bytes(original_raw)
            context["source_state"] = {**context["source_state"], "sha256": "f" * 64}
            with self.assertRaisesRegex(replay.ReplayError, "停点意图漂移"):
                driver._prepare_view("vc3-revision-view", campaign, **arguments)
            context["source_state"] = source.binding(origin)
            # 由正式CLI生成有效第三revision，证明不能把later/latest误当成原失败读操作。
            later_docs = {name: json.loads((inputs_dir / filename).read_bytes()) for name, filename in profile.PREPARE_FILES.items()}
            later_docs["joint_inputs"]["boundary_assertions"][0]["facts"]["later_revision_short_test"] = True
            later_input = case.write_prepare(later_docs, case.root / "later-review-input")
            code, added, stderr = case.revise_prepare(campaign, revised["revision"], later_input)
            self.assertEqual((code, added["gate"]["status"]), (2, "closed"), stderr)
            self.assertNotEqual(added["revision"], revised["revision"])
            with self.assertRaisesRegex(replay.ReplayError, "禁止选择后来追加的 revision"):
                driver._prepare_view("vc3-revision-view", campaign, **arguments)
            self.assertEqual(stops.load_state(key), started)
            shutil.rmtree(added["draft_dir"])
            with mock.patch.object(vc1, "_batch") as batch, mock.patch.object(case, "write_prepare") as write_inputs, \
                    mock.patch.object(driver, "_latest_prepare", wraps=driver._latest_prepare) as reader:
                value = driver._prepare_view("vc3-revision-view", campaign, **arguments)
                self.assertEqual(value["gate"], revised["gate"])
                self.assertEqual(value["review_sha256"], revised["review_sha256"])
                self.assertEqual(driver._prepare_view("vc3-revision-view", campaign, **arguments), value)
                self.assertEqual(driver._prepare_view("vc3-draft-view", campaign), draft_view)
                self.assertEqual(reader.call_count, 1)
                reader.assert_called_once_with(campaign, revision=revised["revision"])
                batch.assert_not_called()
                write_inputs.assert_not_called()
            self.assertEqual(stops.load_state(key)["status"], "complete")
            self.assertEqual({path.relative_to(campaign): path.read_bytes() for path in campaign.rglob("*") if path.is_file()}, original_files)
            revision_file.write_bytes(original_raw + b" ")
            with self.assertRaisesRegex(replay.ReplayError, "绑定摘要或字节数漂移"):
                driver._prepare_view("vc3-revision-view", campaign, **arguments)

    def test_real_batch_stop_reuses_original_commit_parent_and_results(self) -> None:
        """VC-5 同形控制链：预约前失败→正式对账→旧准入 ABORT→同序号新 attempt→只读 complete。

        真实执行 VC-3／VC-4 封存、编译、父监督器、诊断、两本账和 ABORT。动作内部只调用真实预约前
        管理 token guard；通过后写明示的控制探针结果，不执行采集、不生成候选 attempt 或版本认证。
        """
        from tools.official_client_capture import claude_upgrade as upgrade
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor
        from tools.official_client_capture.tests import claude_fault_chain_world as stops
        from tools.official_client_capture.tests import test_claude_upgrade_batch as batches

        case = batches.AtomicBatchChainTests(methodName="runTest")
        case.setUpClass()
        self.addCleanup(case.tearDownClass)
        case.setUp()
        self.addCleanup(case.doCleanups)
        build = case.build_batch_campaign
        def build_with_driver_state(**options):
            campaign = build(**options)
            case.state_dir = case.data / "supervisor"
            case.state_dir.mkdir(mode=0o700)
            return campaign
        with mock.patch.object(case, "build_batch_campaign", side_effect=build_with_driver_state):
            campaign = case.drive_vc3_vc4_to_candidate_sealed()
        sequence = max(upgrade._committed_vc_sequences(campaign, upgrade.load_campaign_manifest(campaign))) + 1
        name, phase = "vc5-01-candidate-run", "VC-5"
        argv = ["capture-candidate", "run", "--campaign-dir", str(campaign),
                "--candidate-id", "claude-cand-r1", "--acknowledge-live-requests"]
        origin = source.write(self.data / "control/batch-origin.json", {"fixture_only": True})
        record_root = self.data / "batch-stops"
        record_root.mkdir()
        token = driver._SUCCESSOR.set({"data": case.data, "source_state": source.binding(origin)})
        self.addCleanup(driver._SUCCESSOR.reset, token)
        inject = case.root / "short-inject"
        inject.mkdir(mode=0o700)
        result_path = inject / "preflight-probe.json"
        token_path = inject / "invalid-offline-token"
        # 子动作仍走正式 CLI admission、诊断和监督协议；成功标志只属于下列显式控制探针。
        (inject / "usercustomize.py").write_text(
            "import json, os, sys\n"
            f"if os.environ.get({supervisor.CAMPAIGN_RUN_ACTION_ID_ENV!r}) == {name!r}:\n"
            f"    sys.path.insert(0, {str(vc1.REPOSITORY)!r})\n"
            "    from types import SimpleNamespace\n"
            "    from pathlib import Path\n"
            "    from tools.official_client_capture import claude_upgrade_capture as capture\n"
            "    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate\n"
            "    from tools.official_client_capture import claude_upgrade_plan as plan\n"
            "    def control_probe(arguments):\n"
            "        job = SimpleNamespace(steps=[{'environment': {'GATEWAY_TRIGGER': plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH}}])\n"
            "        candidate.require_admin_token_for([job])\n"
            "        result = {'fixture_only': True, 'scope': 'vc5-prereservation-control-probe', 'live_request_count': 0}\n"
            f"        with Path({str(result_path)!r}).open('x') as handle:\n"
            "            json.dump(result, handle)\n"
            "        return result, 0\n"
            "    capture.capture_candidate_run_command = control_probe\n", encoding="utf-8")
        def resume():
            return driver._recorded_batch(vc1._batch, case.data, campaign, name, argv, phase=phase)

        with mock.patch.dict(os.environ, {stops.METHOD_RECORD_ENV: str(record_root),
                "PYTHONPATH": str(inject), "ADMIN_BEARER_TOKEN": "", "ADMIN_BEARER_TOKEN_FILE": str(token_path)}):
            # 初次普通 capture 的真正管理 token guard 失败；没有任何候选预约。
            with self.assertRaises(replay.ReplayError):
                resume()
            key = "recorded-full-step:batch:" + name
            self.assertEqual(stops.load_state(key)["status"], "started")
            failed_commit = upgrade._staging_commit_path(campaign, sequence, phase)
            failed_run = Path(source.read(failed_commit)["parent_run_dir"])
            prior_state = supervisor._read_state(failed_run)
            prior = supervisor._run_inner_manifest(failed_run)
            self.assertEqual(prior_state["state"], "failed")
            self.assertEqual((prior["phase"], prior["execute_items"], prior["actions"][0]["item_ids"]),
                             (phase, [name], [name]))
            self.assertEqual(supervisor.candidate_capture_recovery_action(prior, name), "capture")
            diagnostic = failed_run / "action-diagnostics" / f"action-{name}-failure.json"
            self.assertEqual(source.read(diagnostic)["failure_class"], "execution-failure")
            from types import SimpleNamespace
            from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
            from tools.official_client_capture import claude_upgrade_plan as plan
            with self.assertRaises(upgrade.ConfigurationError) as guard:
                candidate.require_admin_token_for([SimpleNamespace(steps=[{
                    "environment": {"GATEWAY_TRIGGER": plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH}}])])
            self.assertEqual(source.read(diagnostic)["error_signature"],
                             supervisor._action_error_signature("handled-error", guard.exception))
            action_path = case.data / "control/actions" / f"{sequence}-{name}.json"
            preserved = {path: path.read_bytes() for path in (action_path, failed_commit, failed_run / "state.json",
                         failed_run / "campaign-run-manifest.json", failed_run / "stop-receipt.json", diagnostic)}
            self.assertFalse(list(campaign.glob("candidates/*/attempts/*/reservation.json")))
            with self.assertRaisesRegex(replay.ReplayError, "尚未正式对账闭合"):
                resume()
            self.assertFalse((case.data / "control/actions" / f"{sequence + 1}-{name}.json").exists())
            code, reconciled, stderr = batches.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            receipt = campaign / "control/reconciliation" / f"run-{failed_run.name}" / "supervisor-run-reconciliation.json"
            receipt_bytes = receipt.read_bytes()
            self.assertEqual((source.read(receipt)["reservation_exists"], source.read(receipt)["live_request_count"]), (False, 0))
            receipt.unlink()
            with self.assertRaisesRegex(replay.ReplayError, "尚未正式对账闭合"):
                resume()
            receipt.write_bytes(receipt_bytes)
            receipt.chmod(0o600)

            # 复现旧 environment_redispatch 的窄拒绝；ABORT 由真实 compiler 形成并保留原件。
            protocols = tuple((label, (lambda *a, **k: supervisor._protocol_reject("有效失败类 execution-failure 不可逐字重派"))
                               if label == "environment_redispatch" else protocol)
                              for label, protocol in supervisor._SUCCESSOR_PROTOCOLS)
            with mock.patch.object(supervisor, "_SUCCESSOR_PROTOCOLS", protocols):
                with self.assertRaisesRegex((upgrade.ConfigurationError, supervisor.SupervisorError), "不可逐字重派"):
                    resume()
            staged = campaign / "control/vc/staging" / f"{sequence + 1:04d}-vc-5" / "attempt-1"
            abort = staged / "ABORT"
            self.assertTrue(abort.is_file())
            self.assertFalse(upgrade._staging_commit_path(campaign, sequence + 1, phase).exists())
            originals = {path: path.read_bytes() for path in staged.iterdir() if path.is_file()}
            next_action = case.data / "control/actions" / f"{sequence + 1}-{name}.json"
            abort_binding = driver._recorded_staging_abort(campaign, next_action, staged)
            successor = source.read(staged / "run-manifest.json")
            # 无对账、多动作、已有预约与动作身份漂移均不能借新入口重派。
            validate = supervisor._validate_batched_environment_redispatch_successor
            self.assertTrue(validate(prior_state, prior, failed_run, successor, campaign_dir=campaign))
            multiple = copy.deepcopy(prior)
            multiple["actions"].append({**multiple["actions"][0], "action_id": "other"})
            self.assertFalse(validate(prior_state, multiple, failed_run, successor, campaign_dir=campaign))
            reserved = campaign / "candidates/claude-cand-r1/attempts/fixture-reservation"
            from datetime import datetime, timezone
            begun = datetime.fromtimestamp(prior_state["started_at_epoch"] + 0.1, tz=timezone.utc).isoformat()
            source.write(reserved / "reservation.json", {"started_at_utc": begun})
            self.assertFalse(validate(prior_state, prior, failed_run, successor, campaign_dir=campaign))
            shutil.rmtree(reserved)
            drift = copy.deepcopy(successor)
            drift["actions"][0]["item_ids"] = ["other"]
            with self.assertRaisesRegex(supervisor.SupervisorError, "漂移字段"):
                validate(prior_state, prior, failed_run, drift, campaign_dir=campaign)
            # 只改本次缺失的离线输入；真正 guard 通过后只执行控制探针，不生成采集成功证据。
            token_path.write_text("deliberately-invalid-offline-fixture-token\n", encoding="utf-8")
            token_path.chmod(0o600)
            with mock.patch.object(upgrade, "compile_and_run_vc_batch", wraps=upgrade.compile_and_run_vc_batch) as dispatch:
                outcome = resume()
                self.assertEqual(resume(), outcome)
                self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(outcome["batch_sequence"], sequence + 1)
            self.assertEqual(outcome["commit"]["staging_attempt"], 2)
            self.assertEqual(outcome["recorded_recoveries"][0]["sequence"], sequence)
            self.assertEqual(outcome["recorded_aborts"], [abort_binding])
            self.assertTrue(upgrade._staging_abort_accounted(campaign, sequence + 1, 1))
            self.assertEqual(next_action.read_bytes(), preserved[action_path])
            self.assertEqual(source.read(result_path)["scope"], "vc5-prereservation-control-probe")
            self.assertFalse(list(campaign.glob("candidates/*/attempts/*/reservation.json")))
            self.assertEqual(stops.load_state(key)["status"], "complete")
            for path, original in {**preserved, **originals}.items():
                self.assertEqual(path.read_bytes(), original)
            # complete 仍必须重读旧失败对账和未提交 ABORT，不再派发新父 run。
            receipt.write_bytes(receipt_bytes + b" ")
            with self.assertRaises((replay.ReplayError, upgrade.ConfigurationError)):
                resume()
            receipt.write_bytes(receipt_bytes)
            abort.write_bytes(originals[abort] + b" ")
            with self.assertRaises(replay.ReplayError):
                resume()
            abort.write_bytes(originals[abort])
            self.assertEqual(resume(), outcome)
            case.assert_zero_requests()

    def test_complete_main_model_cli_binds_intent_roots_and_original_stop(self) -> None:
        target = self.data.with_name("successor")
        predecessor = self.data.with_name("predecessor")
        intent = {"schema_version": driver.BUDGET_CARRY_INTENT_SCHEMA, "data_root": str(target),
                  "predecessor_data": str(predecessor), "predecessor_campaign_id": "original-campaign",
                  "expected_timing_plan_sha256": "a" * 64, "expected_timing_head": {"sequence": 11, "sha256": "b" * 64},
                  "expected_project_plan_sha256": "c" * 64, "expected_project_head": {"sequence": 19, "sha256": "d" * 64},
                  "preserved_bindings": {"store_campaign": {}, "batch19_commit": {}}}
        path = source.write(self.data / "external-intent.json", intent)
        binding = source.binding(path)
        argv = ["--data", str(target), "--recorded", "/fixture/recorded", "--freeze", "/fixture/freeze",
                "--parent-namespaces", "{}", "--through", "VC-6", "--complete-main-model",
                "--budget-carry-intent", str(path), "--budget-carry-intent-sha256", binding["sha256"]]
        with mock.patch.object(source, "require_isolation"), \
                mock.patch.object(vc1, "namespace_main", return_value={"status": "仅核参数接线"}) as namespace, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(driver.main(argv), 0)
            self.assertEqual(namespace.call_args.kwargs["extra_readonly_roots"], (predecessor,))
            self.assertEqual(namespace.call_args.kwargs["initialization_name"], "recorded-full-initialization")
            self.assertIs(namespace.call_args.kwargs["input_verifier"], driver._full_vc5_input_verifier)
            self.assertEqual(driver.main(argv), 0)
            target.mkdir()
            self.assertEqual(driver.main([*argv[:-1], "f" * 64]), 1)
        self.assertEqual(source.binding(path), binding)
        changed = source.write(self.data / "extra-intent.json", {**intent, "new_contract": True})
        with self.assertRaisesRegex(replay.ReplayError, "字段或新数据根不闭合"):
            driver._carry_intent(str(changed), source.binding(changed)["sha256"], target)


if __name__ == "__main__":
    unittest.main()
