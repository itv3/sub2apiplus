"""候选 B 组的轻量来源专项：明示合成合同输入，不启动恢复重链或认证入口。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture.tests import claude_recorded_candidate_source as candidate
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_recorded_candidate_source as fixtures
from tools.official_client_capture.tests import claude_chain_driver as driver
from tools.official_client_capture.capturelib.model import ConfigurationError


class CandidateRecordedRecoverySourceTests(unittest.TestCase):
    """故障只发生在完整复制之后；原件、运行预约与白名单仍执行既有校验。"""

    def setUp(self) -> None:
        fixture = fixtures.RecordedCandidateSourceTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data, self.state, self.projection = fixture.data, copy.deepcopy(fixture.state), copy.deepcopy(fixture.projection)
        self.control_root = self.data / "control/candidate-recovery"
        self.control_root.mkdir(mode=0o700)
        self.failures = source.write(self.control_root / "failures.json", {fixtures.PROBE: 1})
        control = source.write(self.data / "control/recovery-control.json", {
            "schema_version": source.RECOVERY_CONTROL_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
            "control_root": str(self.control_root), "allowed_failure_probe_ids": [fixtures.PROBE],
        })
        self.state["inputs"]["candidate_recovery_control"] = source.binding(control)
        self.state_path = source.write(self.data / "control/recovery-state.json", self.state)

    def copy_step(self, projection: dict, name: str) -> tuple[int, dict]:
        path = source.write(self.data / "control" / (name + ".json"), projection)
        original = source.load_state
        # 这里只验证合成世界的动作合同，不代表 Linux ARM64 命名空间条件通过。
        with mock.patch.object(source, "load_state", side_effect=lambda p, d: original(p, d, isolation=False)):
            code = candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"],
                                             path, source.binding(path)["sha256"])
        return code, source.read(path.with_suffix(".copied.json"))

    def resumed_projection(self) -> dict:
        projection = copy.deepcopy(self.projection)
        reservation = source.read(source.bound(projection["runtime"]["reservation"]))
        runtime = source.read(source.bound(projection["runtime"]["receipt"]))
        nonce = capture._step_run_nonce(reservation["run_nonce"], projection["job_id"], 2, 1)
        runtime["run_nonce"] = nonce
        runtime_path = source.write(source.bound(projection["runtime"]["receipt"]).with_name("resumed-runtime.json"), runtime)
        projection["operation"] = "job:candidate-fixture:step-1:attempt-2"
        projection["runtime"].update(receipt=source.binding(runtime_path), run_nonce=nonce)
        projection["output_root"] = str(self.data / "runs/candidate-fixture-resumed" / fixtures.PROBE)
        argv = projection["argv"]
        argv[argv.index("--output-root") + 1] = projection["output_root"]
        return projection

    def test_failure_after_copy_then_repair_keeps_bytes_and_logs_exact_operations(self) -> None:
        code, copied = self.copy_step(self.projection, "failed-step")
        self.assertEqual(code, 2)
        restoration = source.read(source.bound(copied["restoration"]))
        self.assertEqual((restoration["status"], restoration["capture_exit_code"]), ("capture_failed", 2))
        decision = source.read(source.bound(copied["recovery_action"]))
        self.assertTrue(decision["copy_completed"])
        self.assertEqual((decision["return_code"], decision["network_requests_this_run"]), (2, 0))
        origin = source.origin_probe(self.state, fixtures.PROBE)
        for item in copied["files"]:
            original = Path(origin["index"]["root"]) / item["original_relative"]
            self.assertEqual(source.bound(item["copied"]).read_bytes(), original.read_bytes())
        # 修复只改变本世界故障表；旧决策与已复制的请求字节不被覆盖。
        frozen_decision = source.binding(source.bound(copied["recovery_action"]))
        self.failures.write_text(json.dumps({fixtures.PROBE: 0}), encoding="utf-8")
        resumed = self.resumed_projection()
        code, second = self.copy_step(resumed, "resumed-step")
        self.assertEqual(code, 0)
        self.assertEqual(source.binding(source.bound(copied["recovery_action"])), frozen_decision)
        self.assertEqual(source.read(source.bound(second["restoration"]))["status"], "complete")
        rows = [json.loads(line) for line in (self.control_root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([row["probe_id"] for row in rows], [fixtures.PROBE, fixtures.PROBE])
        self.assertEqual([row["operation"] for row in rows], [self.projection["operation"], resumed["operation"]])
        self.assertEqual([source.read(source.bound(row["decision"]))["return_code"] for row in rows], [2, 0])
        replay.assert_source_untouched(self.state["snapshot"])

    def test_missing_drifted_cross_root_or_wrong_control_identity_fails_before_copy(self) -> None:
        original_path = source.bound(self.state["inputs"]["candidate_recovery_control"])
        original = source.read(original_path)
        variants = ({"fixture_only": False}, {"purpose": "formal"}, {"schema_version": "unknown"},
                    {"control_root": str(self.data / "runs")}, {"allowed_failure_probe_ids": ["unknown"]},
                    {"unexpected": True})
        for index, fields in enumerate(variants):
            with self.subTest(fields=fields):
                path = source.write(self.data / "control" / f"invalid-control-{index}.json", {**original, **fields})
                state = copy.deepcopy(self.state)
                state["inputs"]["candidate_recovery_control"] = source.binding(path)
                with self.assertRaises(replay.ReplayError):
                    source.recovery_control(state)
        before = original_path.read_bytes()
        original_path.write_bytes(before + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
            self.copy_step(self.projection, "drifted-control-step")
        original_path.write_bytes(before)
        original_path.unlink()
        with self.assertRaises(replay.ReplayError):
            source.recovery_control(self.state)
        self.assertFalse(Path(self.projection["output_root"]).exists())
        self.assertFalse((self.control_root / "calls.jsonl").exists())

    def test_unknown_fault_probe_or_invalid_count_is_rejected_without_execution(self) -> None:
        for values in ({"unknown": 1}, {fixtures.PROBE: -1}, {fixtures.PROBE: True}, {fixtures.PROBE: "1"}):
            with self.subTest(values=values):
                self.failures.write_text(json.dumps(values), encoding="utf-8")
                with self.assertRaisesRegex(replay.ReplayError, "故障表"):
                    source.recovery_control(self.state)
        self.assertFalse((self.control_root / "calls.jsonl").exists())

    def test_recovery_logging_cannot_bypass_runtime_or_origin_identity(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["runtime"]["run_nonce"] = "f" * 64
        with self.assertRaises(replay.ReplayError):
            self.copy_step(projection, "invalid-runtime-step")
        self.assertFalse(Path(projection["output_root"]).exists())
        self.assertFalse((self.control_root / "calls.jsonl").exists())
        replay.assert_source_untouched(self.state["snapshot"])

    def test_duplicate_operation_cannot_increment_counter_or_append_log(self) -> None:
        self.copy_step(self.projection, "first-step")
        calls = (self.control_root / "calls.jsonl").read_bytes()
        counts = (self.control_root / "counts.json").read_bytes()
        with self.assertRaisesRegex(replay.ReplayError, "禁止覆盖"):
            self.copy_step(self.projection, "duplicate-step")
        self.assertEqual((self.control_root / "calls.jsonl").read_bytes(), calls)
        self.assertEqual((self.control_root / "counts.json").read_bytes(), counts)


class CandidateJobAggregationTests(unittest.TestCase):
    """只构造并重验冻结输入，不调用 VC-0 收口、批次或任何恢复重链。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="claude-candidate-aggregation-")
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve() / "staging/data"
        driver.full.vc1._create_world_roots(self.data)
        self.inputs = driver.inputs(self.data)
        self.state = {"data_root": str(self.data), "inputs": self.inputs, "jobs": self.inputs["jobs"]}
        self.aggregation = driver.validate_aggregation(self.state)

    def test_full_rules_original_probes_steps_and_official_jobs_are_preserved(self) -> None:
        original = source.read(source.bound(self.aggregation["original_scenarios"]))
        grouped = source.read(source.bound(self.aggregation["grouped_scenarios"]))
        self.assertEqual([job for job in grouped["capture_jobs"] if job["phase"] == "official"],
                         [job for job in original["capture_jobs"] if job["phase"] == "official"])
        candidates = [job for job in original["capture_jobs"] if job["phase"] == "candidate"]
        self.assertEqual(set(self.inputs["candidate_jobs"]), {driver.LIVE_JOB, driver.REFRESH_JOB})
        self.assertEqual(self.inputs["candidate_jobs"][driver.REFRESH_JOB], [driver.REFRESH_PROBE])
        original_probes = [step["probe_id"] for job in candidates for step in job["steps"]]
        grouped_probes = [probe for probes in self.inputs["candidate_jobs"].values() for probe in probes]
        self.assertEqual(sorted(original_probes), sorted(grouped_probes))
        self.assertEqual(len(grouped_probes), len(set(grouped_probes)))
        self.assertEqual(len(self.aggregation["step_mapping"]), len(original_probes))
        self.assertEqual(len(source.read(source.bound(self.aggregation["rules"]))["required_rules"]), 40)
        live = next(job for job in grouped["capture_jobs"] if job["id"] == driver.LIVE_JOB)
        for key in ("scenario_ids", "covers", "tool_dependencies", "required_scenario_receipts"):
            self.assertEqual(set(live[key]), {value for job in candidates if job["id"] != driver.REFRESH_JOB for value in job[key]})
        for row in self.aggregation["step_mapping"]:
            old = next(job for job in candidates if job["id"] == row["original_job_id"])["steps"][row["original_step_index"] - 1]
            new = next(job for job in grouped["capture_jobs"] if job["id"] == row["target_job_id"])["steps"][row["target_step_index"] - 1]
            self.assertEqual(new, driver._coordinate_projection(old, row["original_evidence_root"], row["target_evidence_root"]))

    def test_missing_probe_or_duplicate_step_mapping_is_rejected_even_with_updated_local_binding(self) -> None:
        for index, mapping in enumerate((self.aggregation["step_mapping"][:-1],
                                         [*self.aggregation["step_mapping"], self.aggregation["step_mapping"][0]])):
            with self.subTest(index=index):
                value = {**self.aggregation, "step_mapping": mapping}
                path = source.write(self.data / "control" / f"bad-map-{index}.json", value)
                state = copy.deepcopy(self.state)
                state["inputs"]["bindings"]["candidate_aggregation"] = source.binding(path)
                with self.assertRaisesRegex(replay.ReplayError, "遗漏、重复或漂移"):
                    driver.validate_aggregation(state)

    def test_rule_coverage_omission_and_duplicate_probe_are_rejected_by_original_contracts(self) -> None:
        original = source.read(source.bound(self.aggregation["original_scenarios"]))
        missing = copy.deepcopy(original)
        rule = "SPEC-BODY-001"
        for job in missing["capture_jobs"]:
            if job["phase"] == "candidate":
                job["covers"] = [value for value in job["covers"] if value != rule]
        with self.assertRaisesRegex(ConfigurationError, "未映射规则"):
            driver.aggregate_scenarios(missing)
        duplicate = copy.deepcopy(original)
        job = next(job for job in duplicate["capture_jobs"] if job["id"] == driver.LIVE_JOB)
        job["steps"].append(copy.deepcopy(job["steps"][0]))
        with self.assertRaises(ConfigurationError):
            driver.aggregate_scenarios(duplicate)

    def test_frozen_map_scenario_or_original_source_bytes_cannot_change(self) -> None:
        for name in ("original_scenarios", "grouped_scenarios", "grouped_labels", "baseline_scenarios"):
            with self.subTest(name=name):
                path = source.bound(self.aggregation[name])
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
                    driver.validate_aggregation(self.state)
                path.write_bytes(before)
        path = source.bound(self.inputs["bindings"]["candidate_aggregation"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
            driver.validate_aggregation(self.state)

    def test_wrong_job_whitelist_or_cross_root_frozen_binding_fails_closed(self) -> None:
        state = copy.deepcopy(self.state)
        state["inputs"]["candidate_jobs"][driver.LIVE_JOB].pop()
        with self.assertRaisesRegex(replay.ReplayError, "白名单"):
            driver.validate_aggregation(state)
        outside = source.write(self.data.parent / "outside-scenarios.json",
                               source.read(source.bound(self.aggregation["grouped_scenarios"])))
        value = {**self.aggregation, "grouped_scenarios": source.binding(outside)}
        path = source.write(self.data / "control/cross-root-map.json", value)
        state = copy.deepcopy(self.state)
        state["inputs"]["bindings"]["candidate_aggregation"] = source.binding(path)
        with self.assertRaisesRegex(replay.ReplayError, "越过录制根"):
            driver.validate_aggregation(state)


if __name__ == "__main__":
    unittest.main()
