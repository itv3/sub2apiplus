"""候选录制控制来源的轻量合同拒绝例；全部来源为显式合成，不登记重链或正式版本认证。"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import unittest

from tools.official_client_capture.tests import claude_recorded_candidate_source as candidate
from tools.official_client_capture.tests import claude_recorded_chain_source as source
from tools.official_client_capture.tests import claude_recorded_replay as replay
from tools.official_client_capture.tests import test_claude_vc1_recorded_chain as source_fixture
from tools.official_client_capture.tests import claude_recorded_full_validation_driver as full

PROBE = "v4-thinking-display-omitted"


class RecordedCandidateSourceTests(unittest.TestCase):
    """候选预约与历史来源必须分别闭合；不继承旧用例类，避免默认发现重复运行。"""

    def setUp(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan

        fixture = source_fixture.RecordedSourceContractTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data, self.state = fixture.data, copy.deepcopy(fixture.state)
        original_path = fixture.fixture.supplement / f"attempts/{PROBE}/attempt-001/relay-manifest.json"
        manifest = source.read(original_path)
        manifest.update(secret_scan={"passed": True}, cleanup={"relay_stopped": True, "hosts_restored": True},
                        m_binding={"complete": True}, upstream={"host": "api.anthropic.com", "live_requests": True})
        original_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.state["snapshot"] = replay.build_snapshot(self.data / "control/candidate-snapshot", recorded=fixture.fixture.recorded)
        self.state["inputs"] = source_fixture.projection_inputs(
            self.data / "control/candidate-v5-inputs", self.state["jobs"], {"candidate-fixture": [PROBE]})
        self.state["inputs"]["candidate_configuration"] = {
            "service_container": "sub2apiplus", "keeper_container": "sub2apiplus-keeper",
            "postgres_container": "sub2apiplus-postgres", "claude_account_id": "7", "api_key_id": "9"}
        attempt = self.data / f"evidence/campaigns/fixture/candidates/{full.CANDIDATE}/attempts/attempt-fixture"
        reservation = source.write(attempt / "reservation.json", {
            "schema_version": capture.CAPTURE_RESERVATION_SCHEMA, "mode": capture.MODE_FORMAL,
            "attempt_id": attempt.name, "phase": "candidate", "candidate_id": full.CANDIDATE,
            "run_nonce": "a" * 64, "planned_jobs": [{"id": "candidate-fixture"}],
        })
        nonce = capture._step_run_nonce("a" * 64, "candidate-fixture", 1, 1)
        runtime = source.write(attempt / "runtime.json", {
            "schema_version": capture.HOST_RUNTIME_RECEIPT_SCHEMA, "run_nonce": nonce,
            "runtime_image_reference": "current@sha256:" + "8" * 64, "runtime_image_id": "sha256:" + "8" * 64,
            "container": {"name": "capture-cli", "id": "9" * 64}, "docker_server": {"os": "linux", "arch": "arm64"},
            "repo_digest_verified": True, "capture_source_bundle": {"sha256": "e" * 64, "files": []},
        })
        self.projection = copy.deepcopy(fixture.projection)
        self.projection.update(operation="job:candidate-fixture:step-1:attempt-1", job_id="candidate-fixture",
                               side="candidate", candidate_id=full.CANDIDATE, probe_id=PROBE,
                               output_root=str(self.data / "runs/candidate-fixture" / PROBE))
        self.projection["runtime"].update(receipt=source.binding(runtime), run_nonce=nonce,
                                          reservation=source.binding(reservation), attempt_id=attempt.name)
        origin = source.origin_probe(self.state, PROBE)
        old_path = attempt / "origin-runtime.json"
        old_path.write_bytes(origin["receipt_path"].read_bytes())
        self.projection["origin"].update(receipt=source.binding(old_path), run_nonce=origin["receipt"]["run_nonce"],
                                         source_id=origin["row"]["source_id"], attempt=origin["row"]["attempt"])
        self.projection["argv"] = [
            "python3", "/fixture/tools/claude_fw_e_relay.py", "capture", "--probe-id", PROBE,
            "--run-id", "fixture-cand-r1-" + PROBE, "--model", "claude-sonnet-5",
            "--expected-version", source.VERSION, "--expected-sha256", self.state["freeze"]["binary_sha256"],
            "--output-root", self.projection["output_root"], "--upstream-ip", "160.79.104.10",
        ]
        facts = self.state["inputs"]["bindings"]["version_facts"]
        self.projection["argv"].extend([
            "--fw-f-v5-probe", PROBE, "--fw-f-v5-catalog", self.projection["v5_catalog"]["argument"],
            "--fw-f-v5-version-facts", facts["path"], "--fw-f-v5-version-facts-sha256", facts["sha256"],
        ])
        self.projection["candidate"] = {
            "run_id": "fixture-cand-r1-" + PROBE, "probe_id": PROBE, "model": "claude-sonnet-5",
            "account_id": 7, "api_key_id": 9, "base_url": "http://sub2apiplus:8080",
            "trigger": plan.candidate_trigger(PROBE), "relay_port": int(plan.CANDIDATE_RELAY_PORT), "live": True,
        }

    def runtime_context(self):
        """使用真正的签发上下文和实际工具文件；只构造显式的 daemon 外部观察输入。"""

        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture.capturelib.identity import CAPTURE_SOURCE_RELATIVE_PATHS

        current = self.projection["runtime"]
        attempt = source.bound(current["reservation"]).parent
        campaign = attempt.parents[3]
        tools = self.data / "tools/official_client_capture"
        for relative in CAPTURE_SOURCE_RELATIVE_PATHS:
            destination = tools / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(full.vc1.REPOSITORY / "tools/official_client_capture" / relative, destination)
        configuration = {**self.state["inputs"]["candidate_configuration"], "platform": "linux/arm64",
                         "runtime_image": current["runtime_image"], "capture_container": "capture-cli",
                         "synthetic_capture_container": "capture-cli-synthetic", "capture_root": str(self.data),
                         "capture_tool_root": str(tools)}
        manifest = {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": "fixture",
                    "created_at_utc": "2026-10-08T00:00:00Z", "campaign_mode": "formal", "campaign_purpose": "validation_only",
                    "baseline_version": "2.1.220", "target_version": source.VERSION, "target_sha256": self.state["freeze"]["binary_sha256"],
                    "official_identity": {}, "baseline_identity": {}, "tool_identity": {"client": "claude"},
                    "control_receipts": {}, "inputs": {}, "vc_control": {}, "configuration": configuration,
                    "required_rules": [], "coverage_plan": {}, "jobs": [], "predecessor": None}
        path = source.write(campaign / "campaign.json", manifest)
        (campaign / "campaign.sha256").write_text(source.binding(path)["sha256"] + "\n", encoding="utf-8")
        # 只登记测试数据根，布局与宿主签发源码位置交给真实产品函数计算。
        with mock.patch.object(capture.claude_upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS", (str(self.data / "runs"),)), \
                mock.patch.object(capture.claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", str(self.data / "runs")):
            layout = capture._capture_layout(campaign, manifest)
        self.assertFalse(layout.host_tool_root.is_relative_to(self.data))
        context = capture.ScenarioReceiptContext(campaign_id="fixture", attempt_id=attempt.name,
                                                run_nonce=source.read(attempt / "reservation.json")["run_nonce"],
                                                evidence_root=attempt / "evidence", campaign_dir=campaign, attempt_root=attempt,
                                                mode=capture.MODE_FORMAL, layout=layout, manifest=manifest)
        return context

    def issue_runtime(self, context):
        from tools.official_client_capture import claude_upgrade_capture as capture

        values, binding = capture._issue_host_runtime_receipt(
            context, job_id="candidate-fixture", probe_id=PROBE, run_index=1, step_index=1,
            container="capture-cli", deadline=None, heartbeat=None,
        )
        path = context.attempt_root / binding["path"]
        self.projection["runtime"].update(receipt=source.binding(path), run_nonce=binding["run_nonce"],
                                          container_name=binding["container_name"], container_id=binding["container_id"])
        return values, binding, path

    def register_runtime_source(self, context):
        """记录实际 runtime_sources 消费的最小外部输入；不调用构建、封存、Store 或重链。"""

        build = source.write(context.campaign_dir / "candidates" / full.CANDIDATE / "build-receipt.json", {
            "candidate_id": full.CANDIDATE, "fixture_only": True,
            "image": {"image_id": "sha256:" + "b" * 64, "reference": "candidate@sha256:" + "a" * 64},
            "profile": {"profile_digest": "1" * 64, "wire_sha256": "2" * 64,
                        "release_sha256": "3" * 64, "bundle_sha256": "4" * 64},
            "source": {"tree_sha256": "5" * 64}, "build": {"build_id": "fixture-build"}, "deployed_version": "fixture-only",
        })
        profile = source.write(self.data / "control/fixture-profile-stage.json", {"fixture_only": True})
        source.write(self.data / "control/full-runtime-source.json", {
            "fixture_only": True, "purpose": "recorded_control_runtime_source", "build_receipt": source.binding(build),
            "profile_stage": source.binding(profile), "candidate_configuration": context.manifest["configuration"],
            "formal_version_semantics_certified": False,
        })

    def test_real_legacy_fakehost_output_is_rejected_before_manifest(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture.tests import test_claude_upgrade_capture as fixture

        context = self.runtime_context()
        with mock.patch.object(capture, "_run_probe_command", fixture.FakeHost().run):
            _values, _binding, path = self.issue_runtime(context)
        self.assertNotIn("docker_server", source.read(path))
        with self.assertRaisesRegex(replay.ReplayError, "缺少完整 Docker"):
            candidate.verify_candidate_projection(self.state, self.projection)

    def test_actual_full_observer_producer_runtime_and_manifest_are_closed(self) -> None:
        from tools.official_client_capture import claude_upgrade_candidate_capture as gateway
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan
        from tools.official_client_capture.capturelib.identity import capture_source_bundle_identity

        context = self.runtime_context()
        frozen = copy.deepcopy(self.state["inputs"]["candidate_configuration"])
        self.register_runtime_source(context)
        original_image_observer = gateway.observe_service_image
        with full.runtime_sources(self.state):
            self.assertIs(gateway.observe_service_image, original_image_observer)
            values, binding, path = self.issue_runtime(context)
            observed = gateway._collect_gateway_facts(context.manifest, self.data, deadline=None, heartbeat=None)
            self.assertEqual(observed["account"]["id"], 7)
            self.assertEqual(observed["api_key"]["id"], 9)
            self.assertEqual(gateway.observe_service_image(frozen["service_container"]), "sha256:" + "b" * 64)
            self.assertEqual(gateway.host_facts(), {"host_id": full.HOST, "architecture": "linux/arm64"})
            raw, content = gateway._read_activation_fact(context.manifest)
            self.assertTrue(raw.startswith("/app/data/"))
            self.assertEqual(json.loads(content)["profile_mode"], "validation_candidate")
            drifted = copy.deepcopy(context.manifest)
            drifted["configuration"]["claude_account_id"] = "8"
            with self.assertRaisesRegex(replay.ReplayError, "冻结 Campaign"):
                gateway._collect_gateway_facts(drifted, self.data, deadline=None, heartbeat=None)
            with self.assertRaisesRegex(replay.ReplayError, "冻结坐标"):
                gateway.observe_service_image("different-service")
        self.assertEqual(set(values), {"host_runtime_receipt", "host_runtime_receipt_sha256", "run_nonce"})
        self.assertEqual(values["host_runtime_receipt"], str(path))
        self.assertEqual(values["host_runtime_receipt_sha256"], source.binding(path)["sha256"])
        self.assertEqual(values["run_nonce"], capture._step_run_nonce(context.run_nonce, "candidate-fixture", 1, 1))
        raw = {"probe_id": PROBE, "argv": ["echo", "{host_runtime_receipt}", "{host_runtime_receipt_sha256}", "{run_nonce}"],
               "environment": {}, "timeout": 1,
               "runtime_variables": {"host_runtime_receipt": "absolute_path", "host_runtime_receipt_sha256": "sha256", "run_nonce": "sha256"}}
        bound = plan.bind_step_runtime(raw, values)
        self.assertEqual(bound["argv"][1:], [values[name] for name in ("host_runtime_receipt", "host_runtime_receipt_sha256", "run_nonce")])
        runtime = source.read(path)
        self.assertEqual(runtime["docker_server"], {"version": "fixture-recorded-daemon", "os": "linux", "arch": "arm64"})
        self.assertEqual(runtime["container"]["id"], binding["container_id"])
        self.assertEqual(runtime["capture_source_bundle"], capture_source_bundle_identity(context.layout.host_tool_root))
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        manifest = candidate._candidate_manifest(self.state, self.projection, origin)
        self.assertEqual(manifest["runtime"]["host_runtime_receipt"]["docker_server"], runtime["docker_server"])
        self.assertEqual(self.state["inputs"]["candidate_configuration"], frozen)
        observed_origin = source.read(path.with_suffix(".observation-origin.json"))
        self.assertTrue(observed_origin["not_production_observation"])
        self.assertFalse(observed_origin["formal_version_semantics_certified"])
        self.assertEqual(observed_origin["receipt"], source.binding(path))
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(replay.ReplayError):
            candidate.verify_candidate_projection(self.state, self.projection)

    def test_missing_or_drifted_runtime_facts_are_rejected(self) -> None:
        context = self.runtime_context()
        self.register_runtime_source(context)
        with full.runtime_sources(self.state):
            _values, _binding, path = self.issue_runtime(context)
        runtime = source.read(path)
        variants = (("docker_server", None), ("docker_server", {"os": "linux", "arch": "amd64"}),
                    ("docker_server", {"os": "darwin", "arch": "arm64"}), ("runtime_image_id", "missing"),
                    ("capture_source_bundle", {"sha256": "missing"}))
        for number, (field, value) in enumerate(variants):
            with self.subTest(field=field, value=value):
                variant = source.write(path.with_name(f"rejected-{number}-{field}.json"), {**runtime, field: value})
                projection = copy.deepcopy(self.projection)
                projection["runtime"]["receipt"] = source.binding(variant)
                with self.assertRaisesRegex(replay.ReplayError, "缺少完整 Docker"):
                    candidate.verify_candidate_projection(self.state, projection)

    def test_candidate_adapter_uses_frozen_catalog_with_real_runtime_issuer(self) -> None:
        """真实布局和签发器贯通候选适配器，不能把仓库路径误当成容器执行副本。"""

        from tools.official_client_capture import claude_upgrade_capture as capture

        context = self.runtime_context()
        self.register_runtime_source(context)
        raw_step = {"probe_id": PROBE, "argv": ["docker", "exec", "capture-cli", *self.projection["argv"],
                    "--host-runtime-receipt", "{host_runtime_receipt}",
                    "--host-runtime-receipt-sha256", "{host_runtime_receipt_sha256}", "--run-nonce", "{run_nonce}"],
                    "environment": {}, "timeout": 1,
                    "runtime_variables": {"host_runtime_receipt": "absolute_path", "host_runtime_receipt_sha256": "sha256", "run_nonce": "sha256"}}
        adapter = object.__new__(candidate.SourceAdapter)
        adapter.state, adapter.pending, adapter.candidate_pending = self.state, {}, {}
        job = SimpleNamespace(job_id="candidate-fixture", steps=[raw_step])
        # 只提供冻结步骤与外部 daemon 观察，保留真实 runtime 签发、投影和 v5 判定。
        with full.runtime_sources(self.state), mock.patch.object(capture.claude_upgrade, "_campaign_jobs", return_value=[job]):
            values, current = adapter.issue(capture._issue_host_runtime_receipt, context,
                                            job_id="candidate-fixture", probe_id=PROBE, step_index=1, run_index=1,
                                            container="capture-cli", deadline=None, heartbeat=None)
        _argv, path, digest = adapter.candidate_pending[self.projection["operation"]]
        projected = source.read(source.bound({"path": str(path), **replay._digest(path)}))
        catalog = self.data / "tools/official_client_capture/capturelib/claude_fw_f_v5.py"
        self.assertEqual(digest, source.binding(path)["sha256"])
        self.assertEqual(projected["v5_catalog"], {"file": source.binding(catalog),
                                               "argument": str(context.layout.capture_tool_root / "capturelib/claude_fw_f_v5.py")})
        self.assertEqual(projected["runtime"]["receipt"], source.binding(Path(values["host_runtime_receipt"])))
        self.assertEqual(projected["runtime"]["run_nonce"], current["run_nonce"])
        self.assertNotEqual(projected["origin"]["run_nonce"], current["run_nonce"])
        origin = candidate.verify_candidate_projection(self.state, projected)
        manifest = candidate._candidate_manifest(self.state, projected, origin)
        self.assertEqual(manifest["fixture_projection"]["v5_dimension_result"], "passed")
        self.assertFalse(manifest["formal_version_semantics_certified"])

    def test_host_observer_rejects_substituted_issuer_or_source_root(self) -> None:
        """绑定真实签发源码后仍拒绝换脚本、换根或把两者一起换成 staging 副本。"""

        from tools.official_client_capture import claude_upgrade_capture as capture

        context = self.runtime_context()
        self.register_runtime_source(context)
        host_tools = context.layout.host_tool_root
        staging_tools = self.data / "tools/official_client_capture"
        variants = ((staging_tools, host_tools), (host_tools, staging_tools), (staging_tools, staging_tools))
        with full.runtime_sources(self.state):
            for index, (script_root, source_root) in enumerate(variants):
                output = context.attempt_root / f"rejected-source-{index}.json"
                command = [sys.executable, str(script_root / "runtime_host_receipt.py"),
                           "--container", context.layout.capture_container, "--runtime-image", context.layout.runtime_image,
                           "--capture-tool-root", str(source_root), "--output", str(output), "--run-nonce", "a" * 64]
                with self.subTest(script_root=str(script_root), source_root=str(source_root)), \
                        self.assertRaisesRegex(replay.ReplayError, "宿主收据观察命令与冻结工具"):
                    capture._run_probe_command(command, operation="fixture-source-rejection")
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix(".observation-origin.json").exists())

    def test_valid_candidate_keeps_independent_origin_and_runtime(self) -> None:
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        manifest = candidate._candidate_manifest(self.state, self.projection, origin)
        self.assertEqual(manifest["runtime"]["host_runtime_receipt"]["run_nonce"], self.projection["runtime"]["run_nonce"])
        self.assertNotEqual(manifest["runtime"]["host_runtime_receipt"]["run_nonce"], origin["receipt"]["run_nonce"])
        self.assertEqual(manifest["candidate_gateway"]["account_id"], 7)
        self.assertEqual(manifest["fw_f_v5_probe"], PROBE)
        self.assertIsNone(manifest["fw_f_v4_probe"])
        self.assertFalse(manifest["formal_version_semantics_certified"])
        self.assertEqual(manifest["fixture_projection"]["v5_dimension_result"], "passed")
        self.assertEqual(source.origin_probe(self.state, PROBE)["manifest"], origin["manifest"])

    def test_wrong_job_nonce_gateway_or_side_fails_closed(self) -> None:
        variants = []
        for field, value in (("side", "official"), ("job_id", "unknown"), ("candidate_id", "another")):
            variants.append({**copy.deepcopy(self.projection), field: value})
        gateway = copy.deepcopy(self.projection)
        gateway["candidate"]["account_id"] = 8
        variants.append(gateway)
        runtime = copy.deepcopy(self.projection)
        runtime["runtime"]["run_nonce"] = "f" * 64
        variants.append(runtime)
        for variant in variants:
            with self.subTest(variant=variant["operation"]), self.assertRaises(replay.ReplayError):
                candidate.verify_candidate_projection(self.state, variant)

    def test_incomplete_origin_cannot_be_projected_to_complete_candidate(self) -> None:
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        origin["manifest"]["m_binding"]["complete"] = False
        with self.assertRaisesRegex(replay.ReplayError, "历史 relay 来源未具备"):
            candidate._candidate_manifest(self.state, self.projection, origin)

    def test_copy_preserves_all_original_attachment_bytes(self) -> None:
        self.state_path = source.write(self.data / "control/candidate-state.json", self.state)
        projection_path = source.write(self.data / "control/candidate-projection.json", self.projection)
        original_loader = source.load_state
        with mock.patch.object(source, "load_state", side_effect=lambda path, digest: original_loader(path, digest, isolation=False)):
            candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"],
                                      projection_path, source.binding(projection_path)["sha256"])
        copied = source.read(projection_path.with_suffix(".copied.json"))
        self.assertFalse(copied["formal_version_semantics_certified"])
        origin = source.origin_probe(self.state, PROBE)
        for item in copied["files"]:
            original = Path(origin["index"]["root"]) / item["original_relative"]
            self.assertEqual(source.bound(item["copied"]).read_bytes(), original.read_bytes())
        self.assertNotEqual(source.read(source.bound(copied["candidate_manifest"])), origin["manifest"])
        # 正式 probe 校验不放宽：原 v4 清单仍失败，受控投影必须由同一生产校验函数判通过。
        from tools.official_client_capture import claude_upgrade_evidence as evidence

        current_root = source.bound(copied["candidate_manifest"]).parent
        projected_view = evidence.relay_probe_view(current_root, base=self.data)
        context = {"target_version": source.VERSION, "target_sha256": self.state["freeze"]["binary_sha256"],
                   "runtime_image": self.projection["runtime"]["runtime_image"]}
        self.assertEqual(evidence._probe_checks(projected_view, probe=PROBE, context=context, synthetic=False), [])
        original_view = evidence.relay_probe_view(Path(origin["manifest_path"]).parent, base=Path(origin["index"]["root"]))
        self.assertIn("probe_identity", evidence._probe_checks(original_view, probe=PROBE, context=context, synthetic=False))
        replay.assert_source_untouched(self.state["snapshot"])


if __name__ == "__main__":
    unittest.main()
