"""录制来源合同的离线边界验收。

边界用例使用明确的合成来源；真实链只在显式设置来源、Linux ARM64 root 和 unshare 可用时执行。
未运行、缺条件或子进程失败均不能登记链认证。完整链的监督器、closeout 与 seal 不做通过替身。
"""

from __future__ import annotations

import copy
import dataclasses
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


PROJECTION_PROBE = "v4-advisor-enabled-positive"


def projection_inputs(root: Path, jobs: dict, candidate_jobs: dict | None = None) -> dict:
    """冻结最小测试步骤与版本事实；这些是显式合成输入，实际判定器不做通过替身。"""

    from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

    facts = source.read(Path(v5.__file__).with_name("claude_fw_f_v5_version_facts_2_1_285.json"))
    facts["claude_version"] = source.VERSION
    facts["provenance"]["basis"] = "仅供合成来源合同测试的冻结判定输入，不签发历史或正式版本认证。"
    facts_path = source.write(root / "version-facts.json", facts)
    digest = source.binding(facts_path)["sha256"]
    capture_jobs = []
    for phase, mapping in (("official", jobs), ("candidate", candidate_jobs or {})):
        for job_id, probes in mapping.items():
            capture_jobs.append({"id": job_id, "phase": phase, "steps": [{"probe_id": probe, "argv": [
                "python3", "claude_fw_e_relay.py", "capture", "--fw-f-v5-probe", probe,
                "--fw-f-v5-version-facts-sha256", digest,
            ]} for probe in probes]})
    scenarios = source.write(root / "scenarios.json", {
        "schema_version": "claude-upgrade-scenarios/v1", "claude_version": source.VERSION,
        "probe_catalog": {"path": "tools/official_client_capture/capturelib/claude_fw_f_v5.py", "schema_version": v5.SCHEMA_CATALOG},
        "version_facts": {"path": "fixture-only/version-facts.json", "sha256": digest}, "capture_jobs": capture_jobs,
    })
    return {"bindings": {"scenarios": source.binding(scenarios), "version_facts": source.binding(facts_path)},
            "jobs": jobs, "candidate_jobs": candidate_jobs or {}}


def recorded_probe(root: Path, probe_id: str, binary_sha: str) -> None:
    """写明确的合成历史 probe；HTTP 字节由真实 v4/v5 解析与判定，拒绝例也使用同一判定器。"""

    from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4

    probe = v4.get_probe(probe_id)
    attempt = root / "attempts" / probe_id / "attempt-001"
    tools = ([{"name": "advisor", "type": "advisor_20260301", "model": "claude-fable-5"}]
             if probe_id == PROJECTION_PROBE else [])
    body = json.dumps({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "fixture_only"}],
                       "tools": tools}).encode()
    request = ("POST /v1/messages?beta=true HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode() + body
    (attempt / "relay/conn001.client_to_upstream.bin").write_bytes(request)
    response = b'{"fixture_only":true}'
    (attempt / "relay/conn001.upstream_to_client.bin").write_bytes(
        f"HTTP/1.1 200 OK\r\nContent-Length: {len(response)}\r\n\r\n".encode() + response)
    (attempt / "relay/relay.json").write_text(json.dumps({"connections": [{"connection_id": 1, "valid": True}]}))
    (attempt / "results/debug.log").write_text(
        "[AdvisorTool] Server-side tool enabled with claude-fable-5 as the advisor model\n", encoding="utf-8")
    receipt_path = root / "runtime-receipts" / f"{probe_id}-attempt-001.json"
    receipt = source.read(receipt_path)
    bundle = {"sha256": "3" * 64, "files": [{"path": "capturelib/claude_fw_f_v4.py", "sha256": "d" * 64, "size": 1}]}
    receipt.update(container={"name": "historical-capture", "id": "6" * 64}, issued_at_utc="2026-08-19T00:00:00Z",
                   runtime_image_reference="historical@sha256:" + "7" * 64, runtime_image_id="sha256:" + "7" * 64,
                   docker_server={"os": "linux", "arch": "amd64"}, capture_source_bundle=bundle, repo_digest_verified=True)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    host = {"sha256": source.binding(receipt_path)["sha256"], "run_nonce": receipt["run_nonce"],
            **{key: receipt[key] for key in ("schema_version", "container", "docker_server", "runtime_image_reference",
                                           "runtime_image_id", "repo_digest_verified")},
            "capture_source_bundle_sha256": bundle["sha256"],
            "container_runtime_binding": {"verified": True, "container_id": receipt["container"]["id"]}}
    inner = {"valid": True, "tool_names": [tool["name"] for tool in tools], "debug_log_present": True,
             "invocation": {"environment": {"values": {"DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}}}}
    summary = {"schema_version": v4.SCHEMA_RESULT, "probe_id": probe_id, "driver": probe.driver,
               "source_v3_probe": probe.source_v3_probe, "legacy_scenario": probe.legacy_scenario,
               "dimensions": list(probe.dimensions), "response_plan": probe.response_plan,
               "inner_result": inner, "valid": True,
               "catalog_producer": {"path": "tools/official_client_capture/capturelib/claude_fw_f_v4.py", "sha256": "d" * 64}}
    integrity = {"result": "passed", "excluded_connections": []}
    pcap = {"stopped": True, "parsed": True}
    dimension = v4.validate_complete_probe_evidence(probe_id=probe_id, relay_root=attempt / "relay",
                                                   scenario_summary=summary, relay_integrity=integrity, pcap_receipt=pcap)
    if dimension["result"] != "passed":
        raise AssertionError("合成来源必须先经真实 v4 判定通过")
    manifest = {"schema_version": "claude-code-fw-e-relay-run/v1", "status": "complete", "probe_id": probe_id,
                "run_id": "historical-" + probe_id, "model": "claude-sonnet-5", "capture_mode": "fw-f-v4",
                "fw_f_v4_probe": probe_id, "fw_f_v5_probe": None, "response_plan": probe.response_plan,
                "client": {"version": "2.1.226 (Claude Code)", "sha256": binary_sha},
                "runtime": {"runtime_image_reference": receipt["runtime_image_reference"], "host_runtime_receipt": host,
                            "capture_tools": {"execution_sources": bundle}},
                "secret_scan": {"passed": True}, "cleanup": {"relay_stopped": True, "hosts_restored": True},
                "m_binding": {"complete": True}, "upstream": {"host": "api.anthropic.com", "live_requests": True},
                "scenario_result": summary, "relay_integrity": integrity, "tls_p_channel": pcap, "dimension_evidence": dimension}
    (attempt / "relay-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class RecordedWorldDirectoryTests(unittest.TestCase):
    """在通用 022 umask 下由正式审计目录前置判断新世界；错误权限必须在写收据前拒绝。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve() / "world/staging/data"

    def test_private_world_passes_real_closeout_directory_precondition(self) -> None:
        from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        previous = os.umask(0o022)
        try:
            driver._create_world_roots(self.data)
        finally:
            os.umask(previous)
        audit = self.data / "control/closeout-audit"
        self.assertFalse(audit.exists())
        self.assertEqual(closeout._new_private_directory(audit, "closeout audit-dir"), audit)
        for relative in (".", "runs", "evidence", "evidence/campaigns", "control", "control/closeout-audit"):
            self.assertEqual((self.data / relative).stat().st_mode & 0o777, 0o700)

    def test_open_parent_is_rejected_without_audit_or_receipt(self) -> None:
        from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        driver._create_world_roots(self.data)
        (self.data / "control").chmod(0o755)
        audit = self.data / "control/closeout-audit"
        with self.assertRaisesRegex(closeout.VC0CloseoutError, "父目录权限不得允许 group/other"):
            closeout._new_private_directory(audit, "closeout audit-dir")
        self.assertFalse(audit.exists())
        self.assertEqual((self.data / "control").stat().st_mode & 0o777, 0o755)


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
        fixture._source(fixture.recorded, [PROJECTION_PROBE])
        from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
        from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5

        for root, probes in ((fixture.recorded, [PROJECTION_PROBE]), (fixture.supplement, list(source.replay.evaluator_fixture.SUPPLEMENT_PROBES))):
            catalog = v4.catalog_document()
            catalog["probes"] = [json.loads(json.dumps(dataclasses.asdict(v4.get_probe(probe)))) for probe in probes]
            catalog["probe_count"] = len(probes)
            (root / "scenario-catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            for probe in probes:
                recorded_probe(root, probe, self.binary_sha)
        self.snapshot = replay.build_snapshot(self.data / "control", recorded=fixture.recorded)
        self.state = {"schema_version": source.SCHEMA, "fixture_only": True, "purpose": "regression_fixture", "target_version": source.VERSION,
                      "data_root": str(self.data), "snapshot": self.snapshot, "freeze": {"binary_sha256": self.binary_sha},
                      "jobs": {"official-fixture": [PROJECTION_PROBE]}}
        self.state["inputs"] = projection_inputs(self.data / "control/v5-inputs", self.state["jobs"])
        self.state_path = source.write(self.data / "control/source.json", self.state)
        origin = source.origin_probe(self.state, PROJECTION_PROBE)
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
            "runtime_image_id": "sha256:" + "8" * 64, "docker_server": {"os": "linux", "arch": "arm64"},
            "capture_source_bundle": {"sha256": "e" * 64, "files": []},
            "container": {"name": "capture-cli", "id": "9" * 64}, "repo_digest_verified": True,
        })
        self.projection = {"schema_version": source.PROJECTION_SCHEMA, "fixture_only": True,
                           "operation": "job:official-fixture:step-1:attempt-1", "job_id": "official-fixture", "probe_id": PROJECTION_PROBE,
                           "output_root": str(self.data / "runs/official-fixture" / PROJECTION_PROBE),
                           "runtime": {"receipt": source.binding(runtime_path), "run_nonce": current_nonce,
                                       "reservation": source.binding(reservation), "attempt_id": attempt.name,
                                       "runtime_image": "current@sha256:" + "8" * 64, "container_name": "capture-cli", "container_id": "9" * 64},
                           "origin": {"receipt": source.binding(origin_path), "run_nonce": origin["receipt"]["run_nonce"],
                                      "source_id": origin["row"]["source_id"], "attempt": origin["row"]["attempt"],
                                      "platform": "linux/amd64", "binary_sha256": self.binary_sha}}
        catalog_path = self.data / "tools/official_client_capture/capturelib/claude_fw_f_v5.py"
        catalog_path.parent.mkdir(parents=True, mode=0o700)
        catalog_path.write_bytes(Path(v5.__file__).read_bytes())
        self.projection["v5_catalog"] = {"file": source.binding(catalog_path), "argument": str(catalog_path)}
        facts = self.state["inputs"]["bindings"]["version_facts"]
        self.projection["argv"] = [
            "python3", str(catalog_path.parent.parent / "claude_fw_e_relay.py"), "capture",
            "--probe-id", PROJECTION_PROBE, "--fw-f-v5-probe", PROJECTION_PROBE,
            "--fw-f-v5-catalog", str(catalog_path), "--fw-f-v5-version-facts", facts["path"],
            "--fw-f-v5-version-facts-sha256", facts["sha256"], "--run-id", "fixture-r1-" + PROJECTION_PROBE,
            "--model", "claude-sonnet-5", "--expected-version", source.VERSION, "--expected-sha256", self.binary_sha,
            "--output-root", self.projection["output_root"], "--upstream-ip", "160.79.104.10",
        ]

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
            if relative.as_posix() == source.ORIGIN_MANIFEST_FILENAME:
                relative = Path("relay-manifest.json")
            self.assertEqual(current.read_bytes(), (Path(original["index"]["root"]) / original["row"]["attempt"] / relative).read_bytes())
        manifest = source.read(source.bound(copied["control_manifest"]))
        self.assertEqual(manifest["fw_f_v5_probe"], PROJECTION_PROBE)
        self.assertIsNone(manifest["fw_f_v4_probe"])
        self.assertFalse(manifest["formal_version_semantics_certified"])
        self.assertFalse(manifest["fixture_projection"]["current_client_executed"])
        self.assertEqual(manifest["fixture_projection"]["original_execution_sources"], original["receipt"]["capture_source_bundle"])
        self.assertNotEqual(copied["runtime"]["run_nonce"], copied["origin"]["run_nonce"])
        replay.assert_source_untouched(self.snapshot)

    def revalidate(self) -> dict:
        """重验入口沿用真实双身份和 v5 判定，测试不补通过结果。"""

        return source.control_manifest(self.state, self.projection, source.verify_projection(self.state, self.projection))

    def new_snapshot(self) -> None:
        """仅为拒绝例重新冻结已改动的合成来源，不能用重签摘要掩盖目录或判定合同差异。"""

        self.state["snapshot"] = replay.build_snapshot(self.data / "control/negative-snapshot", recorded=self.fixture.recorded)

    def test_real_v5_projection_passes_unchanged_production_probe_checker(self) -> None:
        from tools.official_client_capture import claude_upgrade_evidence as evidence

        manifest = self.revalidate()
        origin = source.verify_projection(self.state, self.projection)
        view = evidence.relay_probe_view(origin["manifest_path"].parent, base=Path(origin["index"]["root"]))
        context = {"target_version": source.VERSION, "target_sha256": self.binary_sha,
                   "runtime_image": self.projection["runtime"]["runtime_image"]}
        self.assertEqual(evidence._probe_checks(view, probe=PROJECTION_PROBE, context=context, synthetic=False),
                         ["probe_identity", "runtime_image"])
        path = source.write(self.data / "control/checker-step.projection.json", self.projection)
        # 只替换本机缺少的命名空间条件；动作体、目录判定与生产 probe 校验保持原实现。
        with mock.patch.object(source, "require_isolation"):
            source.copy_registered(self.state_path, source.binding(self.state_path)["sha256"], path, source.binding(path)["sha256"])
        current = evidence.relay_probe_view(Path(self.projection["output_root"]), base=self.data)
        self.assertEqual(evidence._probe_checks(current, probe=PROJECTION_PROBE, context=context, synthetic=False), [])
        self.assertEqual(manifest["scenario_result"], origin["manifest"]["scenario_result"])
        self.assertEqual(manifest["dimension_evidence"]["result"], "passed")
        self.assertEqual(manifest["fixture_projection"]["original_execution_sources"]["files"][0]["sha256"], "d" * 64)

    def test_wire_debug_facts_and_validator_drift_are_rejected(self) -> None:
        origin = source.verify_projection(self.state, self.projection)
        attempt = Path(origin["manifest_path"]).parent
        paths = [attempt / "relay/conn001.client_to_upstream.bin", attempt / "results/debug.log",
                 Path(self.state["inputs"]["bindings"]["version_facts"]["path"]),
                 Path(self.projection["v5_catalog"]["file"]["path"])]
        for path in paths:
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b"\n")
                try:
                    with self.assertRaisesRegex(replay.ReplayError, "摘要或字节数漂移"):
                        self.revalidate()
                finally:
                    path.write_bytes(before)

    def test_catalog_changes_outside_registered_override_are_rejected(self) -> None:
        path = self.fixture.recorded / "scenario-catalog.json"
        value = source.read(path)
        value["probes"][0]["wait_seconds"] += 1
        path.write_text(json.dumps(value), encoding="utf-8")
        self.new_snapshot()
        with self.assertRaisesRegex(replay.ReplayError, "目录差异超出登记覆盖"):
            self.revalidate()

    def test_missing_advisor_declaration_fails_real_v5_dimension(self) -> None:
        path = self.fixture.recorded / f"attempts/{PROJECTION_PROBE}/attempt-001/results/debug.log"
        path.write_text("合成拒绝例：日志存在，但没有 advisor 模型宣告。", encoding="utf-8")
        self.new_snapshot()
        with self.assertRaisesRegex(replay.ReplayError, "未通过真实 v5 重验.*tool.advisor"):
            self.revalidate()

    def test_bound_facts_conflicting_with_wire_fail_real_v5_dimension(self) -> None:
        facts = source.read(Path(self.state["inputs"]["bindings"]["version_facts"]["path"]))
        facts["advisor"]["tool_type"] = "advisor_20990101"
        facts_path = source.write(self.data / "control/negative-facts.json", facts)
        digest = source.binding(facts_path)["sha256"]
        scenarios = source.read(Path(self.state["inputs"]["bindings"]["scenarios"]["path"]))
        scenarios["version_facts"]["sha256"] = digest
        for job in scenarios["capture_jobs"]:
            for step in job["steps"]:
                argv = step["argv"]
                argv[argv.index("--fw-f-v5-version-facts-sha256") + 1] = digest
        path = source.write(self.data / "control/negative-scenarios.json", scenarios)
        self.state["inputs"]["bindings"] = {"scenarios": source.binding(path), "version_facts": source.binding(facts_path)}
        argv = self.projection["argv"]
        argv[argv.index("--fw-f-v5-version-facts-sha256") + 1] = digest
        argv[argv.index("--fw-f-v5-version-facts") + 1] = str(facts_path)
        with self.assertRaisesRegex(replay.ReplayError, "未通过真实 v5 重验.*tool.advisor"):
            self.revalidate()

    def test_unregistered_relay_read_and_outside_destination_are_rejected(self) -> None:
        origin = source.verify_projection(self.state, self.projection)
        path = Path(origin["manifest_path"]).parent / "relay/conn999.client_to_upstream.bin"
        path.write_bytes("未登记的连接不能成为校验输入".encode())
        with self.assertRaisesRegex(replay.ReplayError, "读集含未登记文件"):
            self.revalidate()
        path.unlink()
        outside = {**self.projection, "output_root": str(self.data / "outside" / PROJECTION_PROBE)}
        with self.assertRaisesRegex(replay.ReplayError, "复制目的不在本轮 runs"):
            source.verify_projection(self.state, outside)
        mixed = copy.deepcopy(self.projection)
        mixed["origin"]["run_nonce"] = mixed["runtime"]["run_nonce"]
        with self.assertRaisesRegex(replay.ReplayError, "origin 原字节漂移或与本轮 runtime 混同"):
            source.verify_projection(self.state, mixed)

    def test_official_adapter_returns_current_runtime_and_retains_origin(self) -> None:
        from tools.official_client_capture import claude_upgrade_capture as capture
        from tools.official_client_capture.capturelib.identity import capture_source_bundle_identity
        from tools.official_client_capture.tests import claude_vc1_recorded_chain_driver as driver

        tools = self.data / "tools/official_client_capture"
        campaign = self.data / "evidence/campaigns/fixture"
        attempt = campaign / "official/attempts/fixture-attempt"
        reservation = source.read(source.bound(self.projection["runtime"]["reservation"]))
        source.write(attempt / "reservation.json", reservation)
        manifest = {"configuration": {"capture_root": str(self.data), "capture_tool_root": str(tools),
                    "capture_container": "capture-cli", "synthetic_capture_container": "capture-cli-synthetic",
                    "runtime_image": driver.IMAGE}}
        # 使用真实布局，宿主签发源码仍在仓库中，容器执行副本位于独立 staging 数据根。
        with mock.patch.object(capture.claude_upgrade, "FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS", (str(self.data / "runs"),)), \
                mock.patch.object(capture.claude_upgrade, "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT", str(self.data / "runs")):
            layout = capture._capture_layout(campaign, manifest)
        self.assertFalse(layout.host_tool_root.is_relative_to(self.data))
        context = capture.ScenarioReceiptContext(mode=capture.MODE_FORMAL, attempt_root=attempt, campaign_dir=campaign,
                                                 campaign_id=campaign.name, attempt_id=attempt.name, manifest=manifest,
                                                 layout=layout, run_nonce=reservation["run_nonce"], evidence_root=attempt / "evidence")
        raw_step = {"probe_id": PROJECTION_PROBE, "argv": ["docker", "exec", "capture-cli", *self.projection["argv"],
                    "--host-runtime-receipt", "{host_runtime_receipt}",
                    "--host-runtime-receipt-sha256", "{host_runtime_receipt_sha256}", "--run-nonce", "{run_nonce}"],
                    "environment": {}, "timeout": 1,
                    "runtime_variables": {"host_runtime_receipt": "absolute_path", "host_runtime_receipt_sha256": "sha256", "run_nonce": "sha256"}}
        adapter = object.__new__(source.SourceAdapter)
        adapter.state, adapter.pending = self.state, {}
        # 只给步骤查询与 Docker 提供外部输入，初始官方阶段经过真实 runtime 签发和完整 v5 判定。
        job = SimpleNamespace(job_id="official-fixture", steps=[raw_step])
        observer = driver.official_host_observation(self.data)
        with mock.patch.object(capture.claude_upgrade, "_campaign_jobs", return_value=[job]), \
                mock.patch.object(capture, "_run_probe_command", observer.run):
            actual_values, actual_binding = adapter.issue(capture._issue_host_runtime_receipt, context,
                                                         job_id="official-fixture", probe_id=PROJECTION_PROBE,
                                                         step_index=1, run_index=1, container="capture-cli", deadline=None, heartbeat=None)
        runtime_path = Path(actual_values["host_runtime_receipt"])
        receipt = source.read(runtime_path)
        self.assertEqual(receipt["capture_source_bundle"], capture_source_bundle_identity(layout.host_tool_root))
        self.assertEqual(receipt["docker_server"], {"version": "fixture-recorded-daemon", "os": "linux", "arch": "arm64"})
        self.assertEqual(receipt["run_nonce"], actual_binding["run_nonce"])
        _argv, path, digest = adapter.pending[self.projection["operation"]]
        projected = source.read(source.bound({"path": str(path), **replay._digest(path)}))
        self.assertEqual(digest, source.binding(path)["sha256"])
        self.assertNotEqual(projected["origin"]["run_nonce"], actual_binding["run_nonce"])
        self.assertEqual(projected["runtime"]["receipt"], source.binding(runtime_path))
        catalog = tools / "capturelib/claude_fw_f_v5.py"
        self.assertEqual(projected["v5_catalog"], {"file": source.binding(catalog),
                                               "argument": str(layout.capture_tool_root / "capturelib/claude_fw_f_v5.py")})
        origin = source.verify_projection(self.state, projected)
        self.assertEqual(source.control_manifest(self.state, projected, origin)["fixture_projection"]["v5_dimension_result"], "passed")
        for index, server in enumerate((None, {"os": "linux", "arch": "amd64"})):
            with self.subTest(docker_server=server):
                rejected = copy.deepcopy(projected)
                rejected_path = source.write(attempt / f"rejected-runtime-{index}.json", {**receipt, "docker_server": server})
                rejected["runtime"]["receipt"] = source.binding(rejected_path)
                with self.assertRaisesRegex(replay.ReplayError, "缺少完整 ARM64 镜像与执行源事实"):
                    source.control_manifest(self.state, rejected, source.verify_projection(self.state, rejected))

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
