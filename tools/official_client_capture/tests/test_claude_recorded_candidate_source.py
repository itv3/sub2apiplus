"""候选录制控制来源的轻量合同拒绝例；全部来源为显式合成，不登记重链或正式版本认证。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
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


def write_relay(root: Path, request: bytes, response: bytes | None, *, connection_id: int = 1, **metadata) -> None:
    """写明确的合成 H1 录制；真实完整性校验仍检查原字节、方向和身份。"""

    streams = {"client_to_upstream": request, **({"upstream_to_client": response} if response is not None else {})}
    connection = {
        "connection_id": connection_id, "client_alpn_offer": ["http/1.1"], "alpn_source": "assumed",
        "client_alpn": "http/1.1", "upstream_alpn_offer": ["http/1.1"], "upstream_alpn": "http/1.1",
        "sni": "api.anthropic.com", "valid": True, "opened_at_unix_ms": 10, "closed_at_unix_ms": 20,
        "bytes": {name: len(raw) for name, raw in streams.items()},
        "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in streams.items()},
        "segments": [{"direction": name, "offset": 0, "length": len(raw), "t_ms": number}
                     for number, (name, raw) in enumerate(streams.items()) if raw], **metadata,
    }
    for name, raw in streams.items():
        path = root / f"relay/conn{connection_id:03d}.{name}.bin"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(raw)
    (root / "relay/relay.json").write_text(json.dumps({
        "schema_version": "byte-relay/v1", "mode": "direct", "upstream_host": "api.anthropic.com",
        "mirror_selected_alpn": True, "synthetic_profile": None, "production_forwarding_enabled": True,
        "credential_scrubbing": {"byte_offsets_preserved": True}, "connections": [connection],
    }), encoding="utf-8")


def freeze_labels(state: dict) -> None:
    """从正式标签截取本测试 Job 的规则与场景；不替换规则口径或标签加载器。"""

    root = full.vc1.REPOSITORY / "tools/official_client_capture"
    labels = source.read(root / "claude_upgrade_evidence_labels_2_1_285.json")
    original_scenarios = source.read(root / "claude_upgrade_scenarios_2_1_285.json")
    binding = state["inputs"]["bindings"]["scenarios"]
    path = source.bound(binding)
    scenarios = source.read(path)
    scenarios["evidence_scenarios"] = original_scenarios["evidence_scenarios"]
    entries = []
    for job in scenarios["capture_jobs"]:
        rules = []
        for step in job["steps"]:
            found = [item for entry in labels["entries"] if entry["side"] == job["phase"]
                     for item in entry["rules"] if item["glob"].startswith(step["probe_id"] + "/")]
            rules.extend(found)
        entries.append({"job_id": job["id"], "side": job["phase"], "rules": rules})
    labels.update(claude_version=source.VERSION, entries=entries)
    labels_path = source.write(path.with_name("labels.json"), labels)
    path.write_text(json.dumps(scenarios, ensure_ascii=False), encoding="utf-8")
    state["inputs"]["bindings"].update(scenarios=source.binding(path), labels=source.binding(labels_path))


class RecordedCandidateSourceTests(unittest.TestCase):
    """候选预约与历史来源必须分别闭合；不继承旧用例类，避免默认发现重复运行。"""

    def setUp(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture import claude_upgrade_plan as plan

        fixture = source_fixture.RecordedSourceContractTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.data, self.state = fixture.data, copy.deepcopy(fixture.state)
        self.fixture = fixture.fixture
        original_path = fixture.fixture.supplement / f"attempts/{PROBE}/attempt-001/relay-manifest.json"
        original_root = original_path.parent
        write_relay(original_root, (original_root / "relay/conn001.client_to_upstream.bin").read_bytes(),
                    (original_root / "relay/conn001.upstream_to_client.bin").read_bytes())
        (original_root / "relay/intervention.jsonl").write_bytes(b"")
        manifest = source.read(original_path)
        manifest.update(secret_scan={"passed": True}, cleanup={"relay_stopped": True, "hosts_restored": True},
                        m_binding={"complete": True}, upstream={"host": "api.anthropic.com", "live_requests": True})
        original_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.state["snapshot"] = replay.build_snapshot(self.data / "control/candidate-snapshot", recorded=fixture.fixture.recorded)
        self.state["inputs"] = source_fixture.projection_inputs(
            self.data / "control/candidate-v5-inputs", self.state["jobs"], {"candidate-fixture": [PROBE]})
        freeze_labels(self.state)
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

    def _add_shared_connection(self, *, mixed: bool = False, unknown: bool = False) -> None:
        """实际补写测试来源字节，再由真实 v4 判定器与快照构造器重冻；不替换分域或 checker。"""

        from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4

        root = self.fixture.supplement / f"attempts/{PROBE}/attempt-001"
        index = source.read(root / "relay/relay.json")
        request = b"HEAD /api/hello HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n"
        if unknown:
            request = request.replace(b"/api/hello", b"/api/unregistered")
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        if mixed:
            request += (root / "relay/conn001.client_to_upstream.bin").read_bytes()
            response += (root / "relay/conn001.upstream_to_client.bin").read_bytes()
            write_relay(root, request, response)
        else:
            write_relay(root, request, response, connection_id=2, opened_at_unix_ms=5, closed_at_unix_ms=6)
            updated = source.read(root / "relay/relay.json")
            updated["connections"].extend(index["connections"])
            (root / "relay/relay.json").write_text(json.dumps(updated), encoding="utf-8")
        manifest = source.read(root / "relay-manifest.json")
        dimension = v4.validate_complete_probe_evidence(
            probe_id=PROBE, relay_root=root / "relay", scenario_summary=manifest["scenario_result"],
            relay_integrity=manifest["relay_integrity"], pcap_receipt=manifest["tls_p_channel"])
        manifest["dimension_evidence"] = dimension
        (root / "relay-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.state["snapshot"] = replay.build_snapshot(self.data / "control/domain-snapshot", recorded=self.fixture.recorded)

    def _enable_recorded_policy(self, *, formal: bool = False) -> None:
        """短验只使用现有真实合同产生的夹具总账与冻结画像；不宣称执行了 VC-3 或现场认证。"""

        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded
        from tools.official_client_capture import codex_upgrade_project_ledger as ledger

        self.context = self.runtime_context()
        ledger.create_project_ledger(
            self.data / "upgrade-project-ledger-claude", project_id="recorded-domain-fixture",
            absolute_deadline_utc="2027-01-31T00:00:00Z", deadline_approved_by="fixture",
            estimation_policy="none", estimation_policy_approved_by="fixture", fixture_only=not formal,
            formal_open_limit=64, client="claude")
        profile_path = self.context.campaign_dir / "assertions/approved-assertion-profile.json"
        profile_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(full.vc1.REPOSITORY / "tools/official_client_capture/claude_rule_expectations_2_1_226.json", profile_path)
        self.original_state_path = source.write(self.data / "control/recorded-original-state.json", self.state)
        policy = recorded.build_policy(self.original_state_path, campaign_path=self.context.campaign_dir / "campaign.json",
                                       project_plan_path=self.data / "upgrade-project-ledger-claude/plan.json", assertion_profile_path=profile_path)
        self.policy_path = source.write(self.data / "control/recorded-evaluation-policy.json", policy)
        self.state["inputs"]["bindings"][recorded.POLICY_KEY] = source.binding(self.policy_path)
        self.state_path = source.write(self.data / "control/recorded-domain-state.json", self.state)

    def _copy_and_bind_recorded(self) -> dict:
        """运行真实来源复制与 seal 的两步 manifest 形成函数；不派发阶段或启动客户端。"""

        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded
        from tools.official_client_capture import claude_rule_assertion as assertion

        self.projection_path = source.write(self.data / "control/recorded-domain-projection.json", self.projection)
        original_loader = source.load_state
        with mock.patch.object(source, "load_state", side_effect=lambda path, digest: original_loader(path, digest, isolation=False)):
            candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"],
                                      self.projection_path, source.binding(self.projection_path)["sha256"])
        destination = Path(self.projection["output_root"])
        directory = destination.relative_to(self.data).as_posix()
        profile = source.read(self.context.campaign_dir / "assertions/approved-assertion-profile.json")
        scenarios = [item["scenario_id"] for item in profile["scenarios"]]
        common = {"probe_id": PROBE, "job_id": "candidate-fixture", "scenario_ids": scenarios, "labels": {"probe": PROBE}}
        artifacts = [{**common, "path": directory + "/relay-manifest.json", "kind": "relay_manifest", "parser": "claude_relay_manifest"}]
        for ordinal, connection in enumerate(source.read(destination / "relay/relay.json")["connections"], 1):
            artifacts.append({**common, "path": directory + f"/relay/conn{connection['connection_id']:03d}.client_to_upstream.bin",
                              "kind": "relay_binary", "parser": "opaque_bound_source",
                              "connection": {"ordinal": ordinal, "connection_id": connection["connection_id"]},
                              "derive": {"parser": "h1_request_stream", "kind": "claude_http_request"}})
        capture = {"schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION, "target_version": source.VERSION,
                   "label_mismatches": [], "artifacts": artifacts}
        inventory = {path.relative_to(self.data).as_posix(): replay._digest(path) for path in destination.rglob("*") if path.is_file()}
        self.v1_manifest = assertion.bind_capture_manifest(capture, inventory, capture_id="fixture-domain")
        return recorded.bind_recorded_manifest(self.v1_manifest, self.data, inventory)

    def test_recorded_domains_consume_one_logical_probe_and_preserve_projection(self) -> None:
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded
        from tools.official_client_capture import claude_rule_assertion as assertion
        from tools.official_client_capture import claude_upgrade_compare as compare
        from tools.official_client_capture import claude_upgrade_evaluation_recovery as recovery

        self._add_shared_connection()
        self._enable_recorded_policy()
        manifest = self._copy_and_bind_recorded()
        self.assertEqual(manifest["schema_version"], recorded.EVALUATION_SCHEMA)
        assertion.validate_evaluation_manifest(manifest, expected_claude_version=source.VERSION)
        observations = assertion.observations_from_manifest(manifest, self.data)
        runs = [item for item in observations if item.record_type == "probe_run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].data["request_count"], 2)
        self.assertEqual(runs[0].data["message_count"], 1)
        self.assertEqual(runs[0].data["relay_mode"], "connect")
        self.assertFalse(runs[0].data["current_client_executed"])
        self.assertEqual(runs[0].data["endpoints"][0], "HEAD /api/hello")
        requests = [item for item in observations if item.record_type == "claude_http_request"]
        self.assertEqual({item.data["connection_id"] for item in requests}, {1, 2})
        shared = next(item for item in requests if item.data["connection_id"] == 2)
        self.assertEqual(shared.labels["origin_platform"], "linux/amd64")
        self.assertTrue(any("recorded-official-originals/relay/relay.json" in path for path in shared.evidence_paths))
        self.assertEqual(len([item for item in observations if item.record_type == "runtime_receipt"]), 1)
        profile = source.read(self.context.campaign_dir / "assertions/approved-assertion-profile.json")
        projection = assertion.project_capture_manifest(profile, profile["rules"][0]["rule_id"], manifest, self.data, source.VERSION)
        self.assertEqual(projection["recorded_evaluation"], manifest["recorded_evaluation"])
        self.assertEqual(assertion.observations_from_manifest(projection, self.data), observations)
        evaluation_path = source.write(self.context.campaign_dir / "assertions/fixture-recorded-evaluation.json", manifest)
        fields = {key: {} for key in compare.SOURCE_FIELDS}
        fields.update(evaluation_manifest=manifest, evidence_root=str(self.data))
        self.assertEqual(compare._require_source(fields, "候选", source.VERSION)["evaluation_manifest"], manifest)
        candidate_result = {"assertion_context": {"evaluation_manifest": {
            "path": evaluation_path.relative_to(self.context.campaign_dir).as_posix(), "sha256": source.binding(evaluation_path)["sha256"]}}}
        self.assertEqual(set(recovery.candidate_failure_scope_inputs(self.context.campaign_dir, candidate_result,
                                                                    {"results": [{"id": "candidate-fixture"}]} )["artifact_jobs"].values()), {"candidate-fixture"})
        with self.assertRaisesRegex(assertion.AssertionConfigurationError, "降级"):
            assertion.observations_from_manifest(self.v1_manifest, self.data)
        tampered = Path(self.projection["output_root"]) / recorded.ORIGINAL_DIRECTORY / "relay/conn002.client_to_upstream.bin"
        tampered.write_bytes(tampered.read_bytes() + b" ")
        for consumer in (lambda: assertion.observations_from_manifest(manifest, self.data),
                         lambda: compare._require_source(fields, "候选", source.VERSION),
                         lambda: recovery.candidate_failure_scope_inputs(self.context.campaign_dir, candidate_result, {"results": [{"id": "candidate-fixture"}]})):
            with self.subTest(consumer=consumer), self.assertRaisesRegex(Exception, "漂移|不一致"):
                consumer()

    def test_recorded_policy_rejects_formal_mixed_unknown_and_extra_state_changes(self) -> None:
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

        self._enable_recorded_policy()
        self.assertIsNotNone(recorded.optional_policy(self.state))
        altered = copy.deepcopy(self.state)
        altered["inputs"]["candidate_configuration"]["claude_account_id"] = "8"
        with self.assertRaisesRegex(recorded.RecordedEvaluationError, "除唯一 policy"):
            recorded.optional_policy(altered)
        altered_policy = source.read(self.policy_path)
        altered_policy["client"] = "codex"
        self.policy_path.write_text(json.dumps(altered_policy), encoding="utf-8")
        self.state["inputs"]["bindings"][recorded.POLICY_KEY] = source.binding(self.policy_path)
        with self.assertRaisesRegex(recorded.RecordedEvaluationError, "资格|自摘要"):
            recorded.optional_policy(self.state)

    def test_recorded_mixed_connection_is_rejected_before_destination_creation(self) -> None:
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

        self._add_shared_connection(mixed=True)
        self._enable_recorded_policy()
        projection_path = source.write(self.data / "control/mixed-projection.json", self.projection)
        original_loader = source.load_state
        with mock.patch.object(source, "load_state", side_effect=lambda path, digest: original_loader(path, digest, isolation=False)), \
                self.assertRaisesRegex(recorded.RecordedEvaluationError, "混合域"):
            candidate.copy_registered(self.state_path, source.binding(self.state_path)["sha256"], projection_path, source.binding(projection_path)["sha256"])
        self.assertFalse(Path(self.projection["output_root"]).exists())

    def test_recorded_unknown_connection_is_rejected_before_destination_creation(self) -> None:
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

        self._add_shared_connection(unknown=True)
        self._enable_recorded_policy()
        origin = candidate.verify_candidate_projection(self.state, self.projection)
        with self.assertRaisesRegex(recorded.RecordedEvaluationError, "未知端点"):
            recorded.connection_domains(self.state, origin)
        self.assertFalse(Path(self.projection["output_root"]).exists())

    def test_recorded_policy_cannot_enable_formal_project(self) -> None:
        from tools.official_client_capture import claude_recorded_evaluation_contract as recorded

        with self.assertRaisesRegex(recorded.RecordedEvaluationError, "正式非 fixture"):
            self._enable_recorded_policy(formal=True)

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
        from tools.official_client_capture import claude_upgrade_plan as plan

        candidate_view = evidence.candidate_probe_view(current_root, base=self.data)
        job = {"candidate_steps": {PROBE: self.projection["candidate"]}}
        self.assertEqual(evidence._candidate_probe_checks(
            {"gateway_base_url": self.projection["candidate"]["base_url"]}, candidate_view, job, PROBE), [])
        labels = plan.load_evidence_labels(source.bound(self.state["inputs"]["bindings"]["labels"]),
                                          expected_version=source.VERSION,
                                          scenario_payload=source.read(source.bound(self.state["inputs"]["bindings"]["scenarios"])))
        self.assertTrue(plan.match_relay_connections(labels["plans"]["candidate-fixture"][PROBE], candidate_view["connections"]))
        payload = source.read(current_root / "relay-manifest.json")
        self.assertTrue(payload["dimension_evidence"]["observation_only"])
        self.assertEqual(payload["fixture_projection"]["original_v5_dimension_evidence"]["result"], "passed")
        self.assertEqual(payload["relay_integrity"]["hello_request_count"], 0)
        self.assertTrue(all(item["path"].startswith(candidate.ORIGINAL_DIRECTORY + "/")
                            or item["path"] in {row["path"] for row in source.read(current_root / "gateway-byte-projection.json")["projected_files"]}
                            or item["path"] == "gateway-byte-projection.json" for item in payload["artifacts"]))
        original_view = evidence.relay_probe_view(Path(origin["manifest_path"]).parent, base=Path(origin["index"]["root"]))
        self.assertIn("probe_identity", evidence._probe_checks(original_view, probe=PROBE, context=context, synthetic=False))
        replay.assert_source_untouched(self.state["snapshot"])


class GatewayByteProjectionTests(unittest.TestCase):
    """只验证隔离夹具的字节形成与正式连接消费，不运行客户端、阶段链或版本认证。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-candidate-byte-projection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "staging/data"
        self.data.mkdir(parents=True, mode=0o700)

    def material(self, request: bytes, response: bytes | None, *, probe: str = PROBE, pcap: bytes | None = b"fixture-only-pcap", **metadata):
        self.projection = {"job_id": "candidate-fixture", "probe_id": probe,
                           "candidate": {"trigger": "client"}}
        from tools.official_client_capture import claude_upgrade_plan as plan

        self.projection["candidate"]["trigger"] = plan.candidate_trigger(probe)
        self.state = {"data_root": str(self.data), "inputs": source_fixture.projection_inputs(
            self.data / "inputs", {}, {"candidate-fixture": [probe]})}
        freeze_labels(self.state)
        attempt = self.root / "originals/attempt-001"
        write_relay(attempt, request, response, **metadata)
        source.write(attempt / "relay-manifest.json", {"fixture_only": True, "purpose": "synthetic-byte-contract"})
        if pcap is not None:
            (attempt / "tls-clienthello.pcap").write_bytes(pcap)
        self.origin = {"manifest_path": attempt / "relay-manifest.json", "row": {"attempt": "attempt-001"},
                       "index": {"root": str(attempt.parent), "files": {
                           path.relative_to(attempt.parent).as_posix(): replay._digest(path)
                           for path in attempt.rglob("*") if path.is_file()}}}
        return candidate.gateway_material(self.state, self.projection, self.origin)

    def materialize(self, material: dict) -> Path:
        destination = self.data / "candidate"
        destination.mkdir(mode=0o700)
        for relative, binding in self.origin["index"]["files"].items():
            path = destination / candidate.ORIGINAL_DIRECTORY / Path(relative).relative_to("attempt-001")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(source.bound({"path": str(Path(self.origin["index"]["root"]) / relative), **binding}).read_bytes())
        for relative, raw in material["files"].items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(raw)
        source.write(destination / "gateway-byte-projection.json", material["receipt"])
        return destination

    @staticmethod
    def request(target: str = "/v1/messages", method: str = "POST") -> bytes:
        body = b'{"model":"claude-sonnet-5","messages":[],"fixture_only":true}' if method == "POST" else b""
        return (f"{method} {target}?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)

    def test_mixed_connection_keeps_exact_request_response_and_chunk_framing(self) -> None:
        requests = [self.request("/api/hello", "HEAD"), self.request(), self.request("/api/claude_code/settings", "GET")]
        responses = [b"HTTP/1.1 200 OK\r\nContent-Length: 777\r\n\r\n",
                     b"HTTP/1.1 103 Early Hints\r\nLink: </fixture>\r\n\r\nHTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
                     b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"]
        material = self.material(b"".join(requests), b"".join(responses))
        self.assertEqual(material["files"]["relay/conn001.client_to_upstream.bin"], requests[1])
        self.assertEqual(material["files"]["relay/conn001.upstream_to_client.bin"], responses[1])
        mapping = material["receipt"]["connections"][0]
        self.assertEqual(mapping["kept_request_indices"], [1])
        self.assertEqual(mapping["requests"][0]["source_range"], [len(requests[0]), len(requests[0] + requests[1])])
        self.assertEqual(mapping["requests"][0]["response_range"], {
            "start": len(responses[0]), "end": len(responses[0] + responses[1]), "complete": True})
        destination = self.materialize(material)
        self.assertEqual(candidate.verify_gateway_material(self.state, self.projection, self.origin, destination), material["receipt"])
        from tools.official_client_capture import claude_fw_e_relay as relay_capture

        integrity = relay_capture._validate_relay_integrity(destination / "relay", relay_mode="connect", candidate=True)
        self.assertEqual(integrity["messages_request_count"], 1)
        self.assertEqual(integrity["hello_request_count"], 0)
        self.assertEqual((destination / "tls-clienthello.pcap").read_bytes(), b"fixture-only-pcap")
        self.assertIn("不是本轮网关", material["receipt"]["pcap_scope"])

    def test_zero_egress_has_no_fabricated_request_response_or_packet(self) -> None:
        material = self.material(self.request("/api/hello", "HEAD"), b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
                                 probe="v4-replay-custom-header-invalid-name", pcap=None)
        self.assertEqual(set(material["files"]), {"relay/relay.json"})
        self.assertEqual(json.loads(material["files"]["relay/relay.json"])["connections"], [])
        self.assertIsNone(material["receipt"]["pcap_source"])
        destination = self.materialize(material)
        from tools.official_client_capture import claude_fw_e_relay as relay_capture

        integrity = relay_capture._validate_relay_integrity(destination / "relay", relay_mode="connect", candidate=True,
                                                            message_request_expectation="zero")
        self.assertEqual(integrity["connection_count"], 0)

    def test_count_tokens_preserves_compressed_body_and_headers(self) -> None:
        compressed = gzip.compress(b'{"model":"claude-sonnet-5","messages":[],"fixture_only":true}', mtime=0)
        compressed_request = (b"POST /v1/messages/count_tokens?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                    b"Content-Encoding: gzip\r\nX-Fixture-Source: original\r\n"
                    + f"Content-Length: {len(compressed)}\r\n\r\n".encode() + compressed)
        count = self.request("/v1/messages/count_tokens")
        responses = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n" * 2
        material = self.material(compressed_request + count, responses, probe="v4-tui-count-tokens")
        self.assertEqual(material["files"]["relay/conn001.client_to_upstream.bin"], compressed_request + count)
        self.assertEqual(material["files"]["relay/conn001.upstream_to_client.bin"], responses)
        self.assertEqual(material["receipt"]["connections"][0]["kept_request_indices"], [0, 1])

    def test_overlapping_source_segments_cannot_forge_complete_coverage(self) -> None:
        segments = [{"direction": "client_to_upstream", "offset": 0, "length": 2, "t_ms": 1},
                    {"direction": "client_to_upstream", "offset": 0, "length": 2, "t_ms": 2}]
        with self.assertRaisesRegex(replay.ReplayError, "原偏移不连续"):
            candidate._project_segments(segments, {"client_to_upstream": [(0, 4)]})

    def test_probe_without_pcap_keeps_absence_and_original_bytes(self) -> None:
        material = self.material(self.request(), b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", pcap=None)
        self.assertNotIn("tls-clienthello.pcap", material["files"])
        self.assertIsNone(material["receipt"]["pcap_source"])

    def test_one_sided_shutdown_uses_real_constant_and_never_drops_response(self) -> None:
        from tools.official_client_capture import claude_fw_e_relay as relay_capture

        marker = {"termination_reason": relay_capture.ONE_SIDED_SHUTDOWN_TERMINATION_REASON, "relay_stop_requested": True}
        material = self.material(self.request(), None, **marker)
        self.assertEqual(material["receipt"]["connections"][0]["requests"][0]["response_range"]["reason"], "recorded_one_sided_shutdown")
        destination = self.materialize(material)
        integrity = relay_capture._validate_relay_integrity(destination / "relay", relay_mode="connect", candidate=True)
        self.assertEqual(integrity["one_sided_shutdown_connection_count"], 1)
        source_root = self.origin["manifest_path"].parent
        response_path = source_root / "relay/conn001.upstream_to_client.bin"
        response_path.write_bytes(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        self.origin["index"]["files"]["attempt-001/relay/conn001.upstream_to_client.bin"] = replay._digest(response_path)
        with self.assertRaisesRegex(replay.ReplayError, "不能丢弃其字节"):
            candidate.gateway_material(self.state, self.projection, self.origin)

    def test_tampered_source_derived_mapping_and_added_files_are_rejected(self) -> None:
        material = self.material(self.request(), b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        destination = self.materialize(material)
        derived = destination / "relay/conn001.client_to_upstream.bin"
        original_derived = derived.read_bytes()
        derived.write_bytes(original_derived + b"x")
        with self.assertRaises(replay.ReplayError):
            candidate.verify_gateway_material(self.state, self.projection, self.origin, destination)
        derived.write_bytes(original_derived)
        receipt_path = destination / "gateway-byte-projection.json"
        receipt = source.read(receipt_path)
        receipt["connections"][0]["requests"][0]["response_range"]["start"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(replay.ReplayError, "原件绑定漂移"):
            candidate.verify_gateway_material(self.state, self.projection, self.origin, destination)
        receipt_path.write_text(json.dumps(material["receipt"]), encoding="utf-8")
        extra = destination / "relay/conn999.client_to_upstream.bin"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(replay.ReplayError, "输出闭集漂移"):
            candidate.verify_gateway_material(self.state, self.projection, self.origin, destination)
        extra.unlink()
        copied = destination / candidate.ORIGINAL_DIRECTORY / "relay/conn001.client_to_upstream.bin"
        copied.write_bytes(b"altered-copy")
        with self.assertRaises(replay.ReplayError):
            candidate.verify_gateway_material(self.state, self.projection, self.origin, destination)
        original = self.origin["manifest_path"].parent / "relay/conn001.client_to_upstream.bin"
        original.write_bytes(b"altered-source")
        with self.assertRaises(replay.ReplayError):
            candidate.gateway_material(self.state, self.projection, self.origin)

    def test_unpaired_and_ambiguous_nonfinal_response_fails_closed(self) -> None:
        for raw in (b"HTTP/1.1 200 OK\r\n\r\nambiguous", b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"):
            with self.subTest(raw=raw), self.assertRaises(replay.ReplayError):
                candidate._response_spans(raw, ["POST", "POST"], intervention=None)
        with self.assertRaises(replay.ReplayError):
            candidate._response_spans(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\nextra", ["POST"], intervention=None)

    def test_recorded_final_disconnect_and_truncation_keep_actual_bytes(self) -> None:
        self.assertEqual(candidate._response_spans(b"", ["POST"], intervention="disconnect_without_response")[0]["end"], 0)
        self.assertEqual(candidate._project_segments(
            [{"direction": "upstream_to_client", "offset": 0, "length": 0, "t_ms": 1}],
            {"upstream_to_client": [(0, 0)]}), [])
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"
        spans = candidate._response_spans(raw, ["POST"], intervention=None)
        self.assertEqual(spans, [{"start": 0, "end": len(raw), "complete": False, "reason": "recorded_final_truncation"}])

    def test_unknown_or_unapproved_forwarded_endpoint_is_not_silently_removed(self) -> None:
        for target in ("/v1/unknown", "/v1/messages/count_tokens"):
            with self.subTest(target=target), self.assertRaisesRegex(replay.ReplayError, "未获标签允许"):
                self.material(self.request(target), b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            shutil.rmtree(self.data / "inputs")
            shutil.rmtree(self.root / "originals")


if __name__ == "__main__":
    unittest.main()
