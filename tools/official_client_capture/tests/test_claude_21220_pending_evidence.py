from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools.official_client_capture import analyze_claude_21220_pending_evidence as analyzer


REPO_ROOT = Path(__file__).resolve().parents[3]
ARCHIVE_ROOT = (
    REPO_ROOT
    / "local-analysis"
    / "captures"
    / "claude-code-2.1.220-pending-evidence-20260801"
)


def request_record(
    run_id: str,
    scenario: str,
    headers: list[list[str]],
    status: int = 200,
    flow_id: str | None = None,
) -> dict[str, object]:
    return {
        "_run_id": run_id,
        "_task": "oauth",
        "_subject": "claude-http",
        "_scenario": scenario,
        "_category": "claude",
        "_boundary": "official_cli_to_official_platform",
        "_flow_id": flow_id or str(uuid.uuid4()),
        "request": {
            "method": "POST",
            "host": "api.anthropic.com",
            "path": "/v1/messages?beta=true",
            "http_version": "HTTP/1.1",
            "headers": headers,
        },
        "response": {
            "status": status,
            "headers": [["Content-Type", "text/event-stream; charset=utf-8"]],
        },
    }


def common_headers(user_agent: str | None = None, session_id: str | None = None) -> list[list[str]]:
    return [
        ["Authorization", "<redacted len=100>"],
        ["User-Agent", user_agent or analyzer.BASE_USER_AGENT],
        ["X-Claude-Code-Session-Id", session_id or str(uuid.uuid4())],
        ["X-Stainless-Retry-Count", "0"],
        ["x-app", "cli"],
        ["x-client-request-id", str(uuid.uuid4())],
    ]


class ClaudePendingEvidenceTests(unittest.TestCase):
    def test_archived_campaign_passes_when_available(self) -> None:
        if not ARCHIVE_ROOT.is_dir():
            self.skipTest("本地忽略区没有待补证归档")
        report = analyzer.analyze_campaign(ARCHIVE_ROOT)
        self.assertTrue(report["passed"])
        self.assertEqual(report["run_count"], 22)
        self.assertEqual(report["integrity"]["complete_m_runs"], 22)
        self.assertEqual(report["integrity"]["post_run_secret_scan_verified_runs"], 22)
        self.assertEqual(report["matrix_counts"]["retry-9"], 2)
        self.assertTrue(report["agents"]["two_independent_runs_per_depth"])
        self.assertEqual(
            report["integrity"]["source_compatibility"]["agent_a3_variant_paths"],
            ["capturelib/scenarios.py"],
        )

        catalog = report["evidence_catalog"]
        self.assertEqual(set(catalog), {
            "HEADER-negative",
            "HEADER-client",
            "HEADER-container",
            "HEADER-remote",
            "HEADER-combo",
            "AGENT-depths",
            "RETRY-count3",
            "RETRY-count5",
            "RETRY-count9",
        })
        grouped_entries = {
            "HEADER-negative": (catalog["HEADER-negative"], 6),
            "HEADER-client": (catalog["HEADER-client"], 6),
            "HEADER-container": (catalog["HEADER-container"], 6),
            "HEADER-remote": (catalog["HEADER-remote"], 6),
            "HEADER-combo": (catalog["HEADER-combo"], 7),
            "AGENT-a1": (catalog["AGENT-depths"]["a1"], 7),
            "AGENT-a2": (catalog["AGENT-depths"]["a2"], 7),
            "AGENT-a3": (catalog["AGENT-depths"]["a3"], 7),
            "RETRY-count3": (catalog["RETRY-count3"], 8),
            "RETRY-count5": (catalog["RETRY-count5"], 8),
            "RETRY-count9": (catalog["RETRY-count9"], 8),
        }
        for group, (entries, expected_path_count) in grouped_entries.items():
            with self.subTest(catalog_group=group):
                self.assertEqual(len(entries), 2)
                for entry in entries:
                    self.assertEqual(len(entry["paths"]), expected_path_count)
                    self.assertEqual(entry["paths"], sorted(entry["paths"]))
                    self.assertEqual(set(entry["paths"]), set(entry["sha256_by_path"]))
                    for relative_path in entry["paths"]:
                        self.assertFalse(Path(relative_path).is_absolute())
                        self.assertTrue(relative_path.startswith("local-analysis/captures/"))
                        self.assertEqual(
                            analyzer._sha256_file(REPO_ROOT / relative_path),
                            entry["sha256_by_path"][relative_path],
                        )
        analyzer_identity = report["analyzer_identity"]
        self.assertEqual(
            analyzer_identity["path"],
            "tools/official_client_capture/analyze_claude_21220_pending_evidence.py",
        )
        self.assertEqual(
            analyzer_identity["sha256"],
            analyzer._sha256_file(REPO_ROOT / analyzer_identity["path"]),
        )

    def test_archived_campaign_rejects_m_secret_and_receipt_tampering(self) -> None:
        if not ARCHIVE_ROOT.is_dir():
            self.skipTest("本地忽略区没有待补证归档")
        for target in (
            "m_binding",
            "m_requirements",
            "secret_scan",
            "secret_snapshot",
            "receipt",
            "post_secret",
            "post_secret_tool",
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary_dir:
                copied_root = Path(temporary_dir) / ARCHIVE_ROOT.name
                shutil.copytree(ARCHIVE_ROOT, copied_root)
                first_run = sorted((copied_root / "runs").iterdir())[0]
                manifest_path = first_run / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if target == "m_binding":
                    manifest["m_binding"]["complete"] = False
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    expected_error = "M 绑定"
                elif target == "m_requirements":
                    manifest["m_binding"]["requirements"]["invented_requirement"] = True
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    expected_error = "M 子要求字段集"
                elif target == "secret_scan":
                    manifest["secret_scan"]["passed"] = False
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    expected_error = "secret scan"
                elif target == "secret_snapshot":
                    manifest["secret_scan"]["byte_count"] += 1
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    expected_error = "扫描时字节快照"
                elif target == "receipt":
                    receipt_path = copied_root / "runtime-receipts" / f"{manifest['batch_id']}.json"
                    receipt_path.write_text(receipt_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
                    expected_error = "receipt SHA-256"
                else:
                    receipt_path = copied_root / "post-run-secret-scan-receipts" / f"{manifest['run_id']}.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if target == "post_secret":
                        receipt["inventory_sha256"] = "0" * 64
                        expected_error = "inventory_sha256"
                    else:
                        receipt["tool_sha256"] = "0" * 64
                        expected_error = "tool_sha256"
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                with self.assertRaisesRegex(analyzer.EvidenceValidationError, expected_error):
                    analyzer.analyze_campaign(copied_root)

    def test_archived_invocation_rejects_argv_and_environment_splicing(self) -> None:
        if not ARCHIVE_ROOT.is_dir():
            self.skipTest("本地忽略区没有待补证归档")
        selected: tuple[dict[str, object], dict[str, object], str] | None = None
        for run_dir in sorted((ARCHIVE_ROOT / "runs").iterdir()):
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            scenario = manifest["cases"][0]["scenarios"][0]
            runtime = manifest["runtime"]
            if scenario == "s1" and runtime["injected_probe_env"] == {} and runtime["injected_fault_spec"] is None:
                invocation_path = run_dir / "results" / "mitm" / "claude-http" / scenario / "invocation.json"
                selected = (
                    json.loads(invocation_path.read_text(encoding="utf-8")),
                    runtime["injected_probe_env"],
                    manifest["run_id"],
                )
                break
        self.assertIsNotNone(selected)
        invocation, injected_env, run_id = selected
        analyzer._validate_invocation(invocation, scenario="s1", injected_env=injected_env, run_id=run_id)

        wrong_argv = json.loads(json.dumps(invocation))
        wrong_argv["argv_redacted"].append("--invented")
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "argv_sha256 重算"):
            analyzer._validate_invocation(wrong_argv, scenario="s1", injected_env=injected_env, run_id=run_id)

        wrong_environment = json.loads(json.dumps(invocation))
        values = wrong_environment["environment"]["values"]
        values["INVENTED_ENV"] = "1"
        wrong_environment["environment"]["keys"] = sorted(values)
        wrong_environment["environment"]["sha256"] = analyzer._canonical_sha256(values)
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "完整环境不符"):
            analyzer._validate_invocation(wrong_environment, scenario="s1", injected_env=injected_env, run_id=run_id)

    def test_retry_interval_has_exponential_base_and_cap(self) -> None:
        self.assertEqual(analyzer._retry_delay_interval(1), (500, 625))
        self.assertEqual(analyzer._retry_delay_interval(6), (16000, 20000))
        self.assertEqual(analyzer._retry_delay_interval(7), (32000, 40000))
        self.assertEqual(analyzer._retry_delay_interval(9), (32000, 40000))

    def test_header_order_and_user_agent_link_are_fail_closed(self) -> None:
        session_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        headers = [
            ["Authorization", "<redacted len=100>"],
            ["User-Agent", f"claude-cli/2.1.220 (external, sdk-cli, client-app/{analyzer.CLIENT_APP})"],
            ["X-Claude-Code-Session-Id", session_id],
            ["X-Stainless-Retry-Count", "0"],
            ["x-app", "cli"],
            ["x-claude-code-agent-id", "dynamic-child"],
            ["x-claude-code-parent-agent-id", "dynamic-parent"],
            ["x-claude-remote-container-id", analyzer.CONTAINER_ID],
            ["x-claude-remote-session-id", analyzer.REMOTE_SESSION_ID],
            ["x-client-app", analyzer.CLIENT_APP],
            ["x-client-request-id", request_id],
        ]
        run = {
            "run_id": "synthetic-header",
            "scenario": "a2",
            "injected_env": dict(analyzer.HEADER_ENVIRONMENTS["header-combination"]),
            "raw_records": [request_record("synthetic-header", "a2", headers)],
        }
        result = analyzer._validate_http_records(run)
        self.assertTrue(result["conditional_header_projection_order_verified"])

        swapped = json.loads(json.dumps(run))
        swapped_headers = swapped["raw_records"][0]["request"]["headers"]
        swapped_headers[8], swapped_headers[9] = swapped_headers[9], swapped_headers[8]
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "投影顺序"):
            analyzer._validate_http_records(swapped)

        wrong_ua = json.loads(json.dumps(run))
        wrong_ua["raw_records"][0]["request"]["headers"][1][1] = analyzer.BASE_USER_AGENT
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "UA"):
            analyzer._validate_http_records(wrong_ua)

    def test_agent_chain_discovers_dynamic_ids_and_rejects_wrong_parent(self) -> None:
        tool_ids = ["tool-dynamic-one", "tool-dynamic-two"]
        agent_ids = ["agent-dynamic-one", "agent-dynamic-two"]
        tool_inputs = [
            {
                "description": "probe depth2 child1",
                "subagent_type": "general-purpose",
                "run_in_background": False,
                "prompt": "D2_C1_OK；probe depth2 child2；D2_C2_OK",
            },
            {
                "description": "probe depth2 child2",
                "subagent_type": "general-purpose",
                "run_in_background": False,
                "prompt": "D2_C2_OK",
            },
        ]
        events = [
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "tool_use", "id": tool_ids[0], "name": "Agent", "input": tool_inputs[0]}]},
            },
            {
                "type": "assistant",
                "parent_tool_use_id": tool_ids[0],
                "message": {"content": [{"type": "tool_use", "id": tool_ids[1], "name": "Agent", "input": tool_inputs[1]}]},
            },
            {
                "type": "assistant",
                "parent_tool_use_id": tool_ids[0],
                "message": {"content": [{"type": "text", "text": "D2_C1_OK"}]},
            },
            {
                "type": "assistant",
                "parent_tool_use_id": tool_ids[1],
                "message": {"content": [{"type": "text", "text": "D2_C2_OK"}]},
            },
            {
                "type": "user",
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "tool_result", "tool_use_id": tool_ids[0], "content": [{"type": "text", "text": f"agentId: {agent_ids[0]}\n"}]}]},
            },
            {
                "type": "user",
                "parent_tool_use_id": tool_ids[0],
                "message": {"content": [{"type": "tool_result", "tool_use_id": tool_ids[1], "content": [{"type": "text", "text": f"agentId: {agent_ids[1]}\n"}]}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "parent_tool_use_id": None,
                "result": "D2_MAIN_OK",
            },
        ]
        pairs = [
            (None, None),
            (agent_ids[0], None),
            (agent_ids[1], agent_ids[0]),
            (agent_ids[0], None),
            (None, None),
        ]
        raw_records = []
        for agent_id, parent_id in pairs:
            headers = [["x-app", "cli"]]
            if agent_id:
                headers.append(["x-claude-code-agent-id", agent_id])
            if parent_id:
                headers.append(["x-claude-code-parent-agent-id", parent_id])
            headers.append(["x-client-request-id", str(uuid.uuid4())])
            raw_records.append(request_record("synthetic-agent", "a2", headers))
        run = {
            "run_id": "synthetic-agent",
            "scenario": "a2",
            "stdout_records": events,
            "raw_records": raw_records,
            "summary": {
                "tool_use_count": 2,
                "tool_use_raw_count": 2,
                "tool_result_count": 2,
                "tool_result_raw_count": 2,
                "tool_use_duplicate_count": 0,
                "tool_result_duplicate_count": 0,
                "markers_present": True,
                "tool_block_conflict": False,
            },
            "invocation": {"argv_redacted": ["claude", "--scenario", "a2"]},
        }
        expected_hashes = tuple(analyzer._canonical_sha256(value) for value in tool_inputs)
        with mock.patch.dict(analyzer.EXPECTED_AGENT_INPUT_SHA256, {"a2": expected_hashes}):
            result = analyzer._analyze_agent_run(run)
            self.assertTrue(result["parent_tool_use_id_chain_verified"])
            self.assertEqual(result["request_depth_sequence"], [0, 1, 2, 1, 0])

            broken = json.loads(json.dumps(run))
            broken["stdout_records"][1]["parent_tool_use_id"] = "unrelated-tool"
            with self.assertRaisesRegex(analyzer.EvidenceValidationError, "parent_tool_use_id"):
                analyzer._analyze_agent_run(broken)

            wrong_header_id = json.loads(json.dumps(run))
            wrong_header_id["raw_records"][2]["request"]["headers"][1][1] = "unrelated-agent"
            with self.assertRaisesRegex(analyzer.EvidenceValidationError, "HTTP 父子链"):
                analyzer._analyze_agent_run(wrong_header_id)

            wrong_prompt = json.loads(json.dumps(run))
            wrong_prompt["stdout_records"][0]["message"]["content"][0]["input"]["prompt"] += "被篡改"
            with self.assertRaisesRegex(analyzer.EvidenceValidationError, "完整调用参数"):
                analyzer._analyze_agent_run(wrong_prompt)

            pinned_id = json.loads(json.dumps(run))
            pinned_id["invocation"]["argv_redacted"].append(tool_ids[0])
            with self.assertRaisesRegex(analyzer.EvidenceValidationError, "被预置"):
                analyzer._analyze_agent_run(pinned_id)

    def test_retry_run_binds_api_events_gaps_and_final_success(self) -> None:
        fault_count = 3
        flow_ids = [f"flow-{index}" for index in range(fault_count + 1)]
        delays = [600, 1200, 2400]
        timestamps = [1_000_000_000]
        for delay in delays:
            timestamps.append(timestamps[-1] + (delay + 30) * 1_000_000)
        raw_records = []
        lifecycle = []
        retry_session_id = str(uuid.uuid4())
        scope = {
            "_run_id": "synthetic-retry",
            "_task": "oauth",
            "_subject": "claude-http",
            "_scenario": "s1",
            "_category": "claude",
            "_boundary": "official_cli_to_official_platform",
            "method": "POST",
            "path": "/v1/messages?beta=true",
            "http_version": "HTTP/1.1",
        }
        for index in range(fault_count + 1):
            headers = common_headers(session_id=retry_session_id)
            raw_records.append(
                request_record(
                    "synthetic-retry",
                    "s1",
                    headers,
                    500 if index < fault_count else 200,
                    flow_ids[index],
                )
            )
            lifecycle.append({
                **scope,
                "_event": "request",
                "_flow_id": flow_ids[index],
                "_captured_monotonic_ns": timestamps[index],
                "retry_count": "0",
            })
            if index < fault_count:
                lifecycle.append({
                    **scope,
                    "_event": "fault_status",
                    "_flow_id": flow_ids[index],
                    "_captured_monotonic_ns": timestamps[index] + 1_000_000,
                    "injected_status": 500,
                    "remaining_budget": fault_count - index - 1,
                })
        stdout = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": retry_session_id,
            },
            *[
            {
                "type": "system",
                "subtype": "api_retry",
                "attempt": attempt,
                "max_retries": 10,
                "retry_delay_ms": delays[attempt - 1],
                "error_status": 500,
                "error": "server_error",
                "session_id": retry_session_id,
            }
            for attempt in range(1, fault_count + 1)
            ],
        ]
        stdout.append({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "api_error_status": None,
            "result": "S1_OK",
            "session_id": retry_session_id,
        })
        run = {
            "run_id": "synthetic-retry",
            "raw_records": raw_records,
            "lifecycle_records": lifecycle,
            "stdout_records": stdout,
        }
        result = analyzer._analyze_retry_run(run, fault_count)
        self.assertEqual(result["capture_overhead_ms"], [30.0, 30.0, 30.0])
        self.assertTrue(result["all_gaps_cover_declared_delay"])

        out_of_range = json.loads(json.dumps(run))
        out_of_range["stdout_records"][1]["retry_delay_ms"] = 626
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "理论区间"):
            analyzer._analyze_retry_run(out_of_range, fault_count)

        impossible_gap = json.loads(json.dumps(run))
        impossible_gap["lifecycle_records"][2]["_captured_monotonic_ns"] = timestamps[0] + 599 * 1_000_000
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "实际 gap"):
            analyzer._analyze_retry_run(impossible_gap, fault_count)

        spliced_flow = json.loads(json.dumps(run))
        spliced_flow["lifecycle_records"][1]["_flow_id"] = "unrelated-flow"
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "flow 绑定"):
            analyzer._analyze_retry_run(spliced_flow, fault_count)

        wrong_scope = json.loads(json.dumps(run))
        wrong_scope["lifecycle_records"][0]["_run_id"] = "unrelated-run"
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "范围元数据"):
            analyzer._analyze_retry_run(wrong_scope, fault_count)

        spliced_session = json.loads(json.dumps(run))
        spliced_session["stdout_records"][1]["session_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(analyzer.EvidenceValidationError, "session_id"):
            analyzer._analyze_retry_run(spliced_session, fault_count)

    def test_cli_writes_failed_report_and_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "failure.json"
            exit_code = analyzer.main([str(Path(temporary_dir) / "missing"), "--output", str(output)])
            self.assertEqual(exit_code, 1)
            self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["passed"])


if __name__ == "__main__":
    unittest.main()
