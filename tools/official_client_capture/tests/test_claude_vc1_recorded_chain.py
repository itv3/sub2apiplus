"""录制来源合同的离线边界验收。

边界用例使用明确的合成来源；真实链只在显式设置来源、Linux ARM64 root 和 unshare 可用时执行。
未运行、缺条件或子进程失败均不能登记链认证。完整链的监督器、closeout 与 seal 不做通过替身。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_recorded_replay as snapshot_tests


class RecordedBinaryObservationTests(unittest.TestCase):
    """来源标记与正式二进制事实分开；直接交真实 Job 重放器做正反例，不登记任何链通过。"""

    def inputs(self):
        from tools.official_client_capture.tests import test_claude_upgrade_job_rehearsal as rehearsal_fixture

        manifest = {"target_version": source.VERSION, "target_sha256": "a" * 64,
                    "configuration": {"claude_bin": "/opt/claude-2.1.226/claude",
                                      "synthetic_capture_container": "capture-cli-synthetic",
                                      "runtime_image": "capture-cli@sha256:" + "b" * 64}}
        state = {"fixture_only": True, "freeze": {"origin_platform": "linux/amd64",
                  "files": {"freeze.json": {"path": "/fixture-only/freeze.json"}}}}
        observer = rehearsal_fixture.ClaudeRehearsalFixture(methodName="runTest")
        observer.binary_calls = 0
        return state, manifest, observer

    def test_source_metadata_does_not_pollute_closed_job_binary_contract(self) -> None:
        from tools.official_client_capture import claude_upgrade_job_rehearsal as contract
        from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as receipt
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        state, manifest, observer = self.inputs()
        # 此用例只提供外部 freeze 观察源；binary 形状与时效仍由正式 job_contract_replay 判断。
        with mock.patch.object(source, "verify_origin_freeze") as verify:
            binary = driver._binary_observation(state, manifest, observer)
        verify.assert_called_once_with(state, Path("/fixture-only/freeze.json"),
                                       expected_version=source.VERSION, expected_binary_sha256=manifest["target_sha256"])
        self.assertEqual(contract.job_contract_replay(manifest, {"binary_verification": binary}),
                         {"binary_facts": contract.binary_facts(manifest)})
        self.assertEqual(observer.binary_calls, 1)
        for fields in ({"fixture_only": True, "origin_platform": "linux/amd64"}, {"target_sha256": "c" * 64}):
            with self.subTest(fields=fields), self.assertRaisesRegex(receipt.JobRehearsalReceiptError, "目标客户端或运行镜像核验事实非法"):
                contract.job_contract_replay(manifest, {"binary_verification": {**binary, **fields}})

    def test_failed_freeze_source_prevents_binary_observation(self) -> None:
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        state, manifest, observer = self.inputs()
        with mock.patch.object(source, "verify_origin_freeze", side_effect=replay.ReplayError("来源身份漂移")), \
                self.assertRaisesRegex(replay.ReplayError, "来源身份漂移"):
            driver._binary_observation(state, manifest, observer)
        self.assertEqual(observer.binary_calls, 0)


class RecordedSourceContractTests(unittest.TestCase):
    """证明 origin/runtime 双绑定和精确复制白名单，合成材料不计为真实链通过。"""

    def setUp(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture

        fixture = snapshot_tests.ClaudeRecordedSnapshotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.data = (fixture.root / "staging" / "data").resolve()
        self.data.mkdir(parents=True, mode=0o700)
        self.binary_sha = "4" * 64
        for root, probes in ((fixture.recorded, ["v4-fixture-probe"]), (fixture.supplement, list(source.replay.evaluator_fixture.SUPPLEMENT_PROBES))):
            for probe in probes:
                receipt_path = root / "runtime-receipts" / f"{probe}-attempt-001.json"
                receipt = source.read(receipt_path)
                receipt.update(container={"name": "historical-capture", "id": "6" * 64}, issued_at_utc="2026-08-19T00:00:00Z",
                               runtime_image_reference="historical@sha256:" + "7" * 64, runtime_image_id="sha256:" + "7" * 64,
                               docker_server={"os": "linux", "arch": "amd64"},
                               capture_source_bundle={"sha256": "3" * 64, "files": []}, repo_digest_verified=True)
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                path = root / "attempts" / probe / "attempt-001" / "relay-manifest.json"
                manifest = source.read(path)
                manifest["client"]["sha256"] = self.binary_sha
                manifest["runtime"]["host_runtime_receipt"]["sha256"] = source.binding(receipt_path)["sha256"]
                manifest["runtime"]["host_runtime_receipt"].update({key: receipt[key] for key in (
                    "schema_version", "container", "docker_server", "runtime_image_reference", "runtime_image_id", "repo_digest_verified")})
                manifest["runtime"]["host_runtime_receipt"].update(
                    capture_source_bundle_sha256=receipt["capture_source_bundle"]["sha256"],
                    container_runtime_binding={"verified": True, "container_id": receipt["container"]["id"]})
                manifest["runtime"].update(runtime_image_reference=receipt["runtime_image_reference"],
                                           capture_tools={"execution_sources": receipt["capture_source_bundle"]})
                path.write_text(json.dumps(manifest), encoding="utf-8")
        self.snapshot = replay.build_snapshot(self.data / "control", recorded=fixture.recorded)
        self.state = {"schema_version": source.SCHEMA, "fixture_only": True, "purpose": "regression_fixture", "target_version": source.VERSION,
                      "data_root": str(self.data), "snapshot": self.snapshot, "freeze": {"binary_sha256": self.binary_sha},
                      "jobs": {"official-fixture": ["v4-fixture-probe"]}}
        self.state_path = source.write(self.data / "control/source.json", self.state)
        origin = source.origin_probe(self.state, "v4-fixture-probe")
        origin_path = self.data / "control/origin.json"
        origin_path.write_bytes(origin["receipt_path"].read_bytes())
        attempt = self.data / "control/fixture-attempt"
        reservation = source.write(attempt / "reservation.json", {
            "schema_version": capture.CAPTURE_RESERVATION_SCHEMA, "mode": capture.MODE_FORMAL,
            "attempt_id": attempt.name, "run_nonce": "a" * 64, "planned_jobs": [{"id": "official-fixture"}],
        })
        current_nonce = capture._step_run_nonce("a" * 64, "official-fixture", 1, 1)
        runtime_path = source.write(attempt / "runtime.json", {
            "schema_version": "official-client-runtime-host-receipt/v1", "run_nonce": current_nonce,
            "runtime_image_reference": "current@sha256:" + "8" * 64,
            "container": {"name": "capture-cli", "id": "9" * 64}, "repo_digest_verified": True,
        })
        self.projection = {"schema_version": source.PROJECTION_SCHEMA, "fixture_only": True,
                           "operation": "job:official-fixture:step-1:attempt-1", "job_id": "official-fixture", "probe_id": "v4-fixture-probe",
                           "argv": ["docker", "exec", "capture-cli", "registered-command"],
                           "output_root": str(self.data / "runs/official-fixture/v4-fixture-probe"),
                           "runtime": {"receipt": source.binding(runtime_path), "run_nonce": current_nonce,
                                       "reservation": source.binding(reservation), "attempt_id": attempt.name,
                                       "runtime_image": "current@sha256:" + "8" * 64, "container_name": "capture-cli", "container_id": "9" * 64},
                           "origin": {"receipt": source.binding(origin_path), "run_nonce": origin["receipt"]["run_nonce"],
                                      "source_id": origin["row"]["source_id"], "attempt": origin["row"]["attempt"],
                                      "platform": "linux/amd64", "binary_sha256": self.binary_sha}}

    def test_dual_receipts_are_revalidated_independently(self) -> None:
        source.verify_projection(self.state, self.projection)
        for name in ("runtime", "origin"):
            with self.subTest(name=name):
                path = Path(self.projection[name]["receipt"]["path"])
                before = path.read_bytes()
                path.write_bytes(before + b" ")
                with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
                    source.verify_projection(self.state, self.projection)
                path.write_bytes(before)
        mixed = copy.deepcopy(self.projection)
        mixed["origin"]["receipt"] = mixed["runtime"]["receipt"]
        with self.assertRaises(replay.ReplayError):
            source.verify_projection(self.state, mixed)
        mixed = copy.deepcopy(self.projection)
        mixed["operation"] = "job:official-fixture:step-1:attempt-2"
        with self.assertRaisesRegex(replay.ReplayError, "runtime 身份"):
            source.verify_projection(self.state, mixed)

    def test_snapshot_does_not_include_unselected_host_receipts(self) -> None:
        index = replay.load_snapshot(self.snapshot)
        receipts = [name for name in index["files"] if "/runtime-receipts/" in name]
        self.assertEqual(len(receipts), 2)
        self.assertFalse(any("summarized" in name for name in receipts))

    def test_unknown_operation_job_probe_or_command_is_rejected(self) -> None:
        for field, value in (("operation", "job:unknown:step-1:attempt-1"), ("job_id", "unknown"),
                             ("probe_id", "v4-unknown"), ("operation", "job:official-fixture:step-2:attempt-1")):
            with self.subTest(field=field, value=value), self.assertRaises(replay.ReplayError):
                source.verify_projection(self.state, {**self.projection, field: value})
        adapter = object.__new__(source.SourceAdapter)
        adapter.state, adapter.path, adapter.digest = self.state, self.state_path, source.binding(self.state_path)["sha256"]
        projection_path = source.write(self.data / "control/step.projection.json", self.projection)
        entry = (self.projection["argv"], projection_path, source.binding(projection_path)["sha256"])
        original = mock.Mock()
        adapter.pending = {self.projection["operation"]: entry}
        with self.assertRaisesRegex(replay.ReplayError, "白名单"):
            adapter.command(original, object(), ["unknown"], operation=self.projection["operation"], job_id="official-fixture")
        adapter.pending = {}
        with self.assertRaisesRegex(replay.ReplayError, "未知复制"):
            adapter.command(original, object(), self.projection["argv"], operation="job:unknown:step-1:attempt-1", job_id="unknown")
        original.assert_not_called()

    def test_copy_keeps_original_bytes_and_refuses_duplicate_destination(self) -> None:
        path = source.write(self.data / "control/step.projection.json", self.projection)
        args = (self.state_path, source.binding(self.state_path)["sha256"], path, source.binding(path)["sha256"])
        # 这里只测动作体边界；真实链用例不能替换 require_isolation。
        with mock.patch.object(source, "require_isolation"):
            source.copy_registered(*args)
            with self.assertRaisesRegex(replay.ReplayError, "禁止重复派发"):
                source.copy_registered(*args)
        copied = source.read(path.with_suffix(".copied.json"))
        original = source.origin_probe(self.state, self.projection["probe_id"])
        for item in copied["files"]:
            current = source.bound(item, root=self.data)
            relative = current.relative_to(self.projection["output_root"])
            self.assertEqual(current.read_bytes(), (Path(original["index"]["root"]) / original["row"]["attempt"] / relative).read_bytes())
        self.assertNotEqual(copied["runtime"]["run_nonce"], copied["origin"]["run_nonce"])
        replay.assert_source_untouched(self.snapshot)

    def test_state_is_fixture_only_and_outside_staging_is_rejected(self) -> None:
        source.load_state(self.state_path, source.binding(self.state_path)["sha256"], isolation=False)
        for name, changes in (("purpose", {"purpose": "formal"}), ("flag", {"fixture_only": False}),
                              ("version", {"target_version": "2.1.285"}), ("root", {"data_root": str(self.fixture.root)})):
            with self.subTest(name=name):
                path = source.write(self.data / "control" / f"{name}.json", {**self.state, **changes})
                with self.assertRaises(replay.ReplayError):
                    source.load_state(path, source.binding(path)["sha256"], isolation=False)

    def test_unregistered_v1_freeze_is_rejected_before_staging(self) -> None:
        original = self.fixture.root / "unregistered-freeze"
        source.write(original / "freeze.json", {"schema_version": "claude-code-fw-e-official-freeze/v1", "target_version": source.VERSION})
        destination = self.data / "unregistered-freeze-copy"
        with self.assertRaisesRegex(replay.ReplayError, "已登记"):
            source.stage_freeze(destination, original)
        self.assertFalse(destination.exists())


class ProductionV2VerifierIsolationTests(unittest.TestCase):
    """真实 v2 冻结校验在测试来源作用域内外必须同样拒绝非法版本，不能被旧来源接缝放行。"""

    def test_real_v2_verifier_is_not_replaced_for_unregistered_inputs(self) -> None:
        from tools.official_client_capture import claude_upgrade_freeze as freeze
        from tools.official_client_capture.tests import test_claude_upgrade_freeze as fixture

        with tempfile.TemporaryDirectory(prefix="claude-v2-source-isolation-") as directory:
            root = Path(directory)
            signer = fixture._Signer(root)
            frozen = fixture.freeze_fixture(root / "v2", signer=signer)
            original = freeze.verify_freeze_receipt
            adapter = object.__new__(source.SourceAdapter)
            adapter.state = {"freeze": {"files": {"freeze.json": {"path": str(root / "unrelated-v1/freeze.json")}}}}
            with adapter.patched():
                result = freeze.verify_freeze_receipt(frozen)
                self.assertEqual(result["binary_equivalence"], "npm=gcs")
                with self.assertRaises(freeze.FreezeError):
                    freeze.verify_freeze_receipt(frozen, expected_version="2.1.226")
            self.assertIs(freeze.verify_freeze_receipt, original)
            with self.assertRaises(freeze.FreezeError):
                original(frozen, expected_version="2.1.226")


class RecordedFullInputTests(unittest.TestCase):
    """VC-1 录制输入在预检冻结前就保留完整候选映射，不能把 full 校验降成官方单侧。"""

    def test_projected_inputs_keep_both_sides_and_their_labels(self) -> None:
        from tools.official_client_capture import claude_upgrade_plan as plan
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve() / "staging/data"
            inputs = driver._inputs(data)
            scenarios = source.read(source.bound(inputs["bindings"]["scenarios"]))
            plan.validate_scenario_manifest(scenarios)
            rules = plan.load_baseline_rules(source.bound(inputs["bindings"]["baseline"]), driver.BASELINE)
            jobs = [SimpleNamespace(phase=job["phase"], required=job["required"], covers=job["covers"]) for job in scenarios["capture_jobs"]]
            plan.validate_full_coverage(jobs, rules)
            self.assertTrue(inputs["candidate_jobs"])
            self.assertFalse(set(inputs["jobs"]) & set(inputs["candidate_jobs"]))
            official = {probe for probes in inputs["jobs"].values() for probe in probes}
            mirrored = {probe for probes in inputs["candidate_jobs"].values() for probe in probes}
            self.assertEqual(mirrored, official)
            labels = source.read(source.bound(inputs["bindings"]["labels"]))
            self.assertEqual({entry["job_id"] for entry in labels["entries"]}, set(inputs["jobs"]) | set(inputs["candidate_jobs"]))
            self.assertEqual(sum(map(len, inputs["jobs"].values())), 42)


class RecordedContinuationContractTests(unittest.TestCase):
    """后段凭证不能用 passed 字样、篡改字节或错误阶段绕过实际封存验证。"""

    def test_changed_receipt_fails_before_loading_a_campaign(self) -> None:
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        with tempfile.TemporaryDirectory() as directory:
            path = source.write(Path(directory) / "continuation.json", {"fixture_only": True})
            digest = source.binding(path)["sha256"]
            path.write_bytes(path.read_bytes() + b" ")
            with mock.patch.object(source, "load_state") as load_state:
                with self.assertRaisesRegex(replay.ReplayError, "外持摘要漂移"):
                    driver.load_continuation(path, digest)
                load_state.assert_not_called()

    def test_full_chain_claim_cannot_be_carried_by_vc1_receipt(self) -> None:
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        with tempfile.TemporaryDirectory() as directory:
            path = source.write(Path(directory) / "continuation.json", {
                "schema_version": driver.CONTINUATION_SCHEMA, "fixture_only": True, "purpose": "regression_fixture",
                "scope": "VC-0→VC-1", "target_version": source.VERSION, "full_vc0_vc6_certified": True,
            })
            with mock.patch.object(source, "load_state") as load_state:
                with self.assertRaisesRegex(replay.ReplayError, "合同非法"):
                    driver.load_continuation(path, source.binding(path)["sha256"])
                load_state.assert_not_called()

    def test_vc0_and_unknown_phases_never_create_an_action(self) -> None:
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            for phase in ("VC-0", "VC-7", "passed"):
                with self.subTest(phase=phase), self.assertRaisesRegex(replay.ReplayError, "动作阶段"):
                    driver._batch(data, data / "campaign", "bad", ["status"], phase=phase)
            self.assertEqual(list(data.iterdir()), [])

    def test_explicit_record_directory_survives_the_shell_scope(self) -> None:
        from tools.official_client_capture.tests.real_chains import test_claude_vc1_recorded_chain as shell

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            records = root / "records.jsonl"
            with mock.patch.dict(os.environ, {shell.RECORD_DIR_ENV: str(root / "artifacts"), shell.RECORDS_ENV: str(records)}):
                with shell.staging_directory("vc1-") as staging:
                    report = source.write(staging / "staging/data/control/result.json", {"status": "unverified"})
                    record = {"scenario": "vc-chain.claude-vc1-capture", "status": "unverified", "report": source.binding(report)}
                    shell.save_record(staging, record, append=False)
                    shell.save_record(staging, record)
                self.assertTrue(staging.is_dir())
                binding = source.read(staging / "record-binding.json")
                self.assertEqual(source.bound(binding), staging / "record.json")
                self.assertEqual(len(records.read_text().splitlines()), 1)
                self.assertEqual(source.bound(source.read(staging / "record.json")["report"]), report)


if __name__ == "__main__":
    unittest.main()
