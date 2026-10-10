"""录制完整链准备的轻量拒绝例；不进入重链、不登记 VC-0→VC-6 认证。"""

from __future__ import annotations

import os
import copy
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
            observed["world"] = data.parent.parent
            self.addCleanup(shutil.rmtree, observed["world"])
            return subprocess.CompletedProcess(argv, 23, "真实外部边界 stdout 标记\n", "真实外部边界 stderr 标记\n")

        environment = {"CLAUDE_2_1_226_RELAY_FIXTURE": "/fixture/relay", "CLAUDE_2_1_226_FREEZE_FIXTURE": "/fixture/freeze",
                       shell.RECORD_DIR_ENV: original_temp, shell.RECORDS_ENV: ""}
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


if __name__ == "__main__":
    unittest.main()
