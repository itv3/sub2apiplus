"""Claude Code 2.1.226 FW-E/F v4 完整场景目录测试。"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.capturelib.claude_fw_f_v3 import PROBES as V3_PROBES
from tools.official_client_capture.capturelib.claude_fw_f_v4 import (
    PROBES,
    PROBE_IDS,
    REQUIRED_MATRIX_DIMENSIONS,
    ClaudeFWFCompleteProbeError,
    catalog_document,
    get_probe,
    run_claude_fw_f_complete_probe,
    validate_complete_probe_evidence,
    validate_probe_catalog,
)


class ClaudeFWFV4CatalogTests(unittest.TestCase):
    def test_catalog_covers_every_required_real_dimension(self) -> None:
        validate_probe_catalog()
        catalog = catalog_document()
        self.assertEqual(PROBE_IDS, tuple(sorted(PROBES)))
        self.assertEqual(catalog["probe_count"], 79)
        self.assertEqual(
            catalog["required_matrix_dimension_count"],
            len(REQUIRED_MATRIX_DIMENSIONS),
        )
        self.assertTrue(
            all(catalog["required_matrix_dimensions"][key] for key in REQUIRED_MATRIX_DIMENSIONS)
        )

    def test_every_v3_probe_is_replayed_without_overwriting_v3(self) -> None:
        inherited = {
            probe.source_v3_probe
            for probe in PROBES.values()
            if probe.driver == "v3"
        }
        self.assertEqual(inherited, set(V3_PROBES))
        self.assertTrue(all(probe_id.startswith("v4-") for probe_id in PROBES))

    def test_privacy_absence_is_not_a_rule(self) -> None:
        privacy = catalog_document()["privacy_configuration"]
        self.assertTrue(privacy["telemetry_disabled"])
        self.assertTrue(privacy["nonessential_traffic_disabled"])
        self.assertFalse(privacy["absence_generates_rule"])

    def test_tls_and_refresh_channels_are_explicit(self) -> None:
        tls = get_probe("v4-native-tls-baseline")
        refresh = get_probe("v4-oauth-refresh")
        self.assertTrue(tls.require_pcap)
        self.assertEqual(refresh.target_host, "platform.claude.com")
        self.assertEqual(refresh.response_plan, "oauth-refresh-reject")
        self.assertEqual(refresh.message_request_expectation, "zero")

    def test_thinking_display_probes_freeze_supported_cli_values(self) -> None:
        summarized = get_probe("v4-thinking-display-summarized")
        omitted = get_probe("v4-thinking-display-omitted")
        self.assertEqual(
            summarized.cli_args,
            ("--thinking-display", "summarized"),
        )
        self.assertEqual(
            omitted.cli_args,
            ("--thinking-display", "omitted"),
        )
        self.assertEqual(summarized.dimensions, ())
        self.assertEqual(omitted.dimensions, ())

    def test_usage_advisor_and_models_are_not_name_only_scenarios(self) -> None:
        usage = get_probe("v4-tui-usage")
        advisor_positive = get_probe("v4-advisor-enabled-positive")
        advisor_negative = get_probe("v4-advisor-default-negative")
        models = get_probe("v4-models-privacy-state")
        self.assertEqual(usage.required_requests, ())
        self.assertEqual(usage.forbidden_requests, ("GET /api/oauth/usage",))
        self.assertEqual(
            usage.runtime_conclusion,
            "usage_blocked_by_essential_traffic_only",
        )
        self.assertTrue(usage.require_debug_log)
        self.assertEqual(advisor_positive.required_message_tools, ("advisor",))
        self.assertEqual(advisor_negative.forbidden_message_tools, ("advisor",))
        self.assertIn("GET /v1/models?beta=true", models.forbidden_requests)
        self.assertEqual(
            models.runtime_conclusion,
            "model_capabilities_hard_disabled_in_2_1_226",
        )

    def test_unknown_environment_fails_closed(self) -> None:
        invalid = dataclasses.replace(
            get_probe("v4-models-privacy-state"),
            probe_id="v4-invalid",
            injected_env=(("LD_PRELOAD", "/tmp/not-allowed"),),
        )
        with mock.patch.dict(PROBES, {"v4-invalid": invalid}, clear=False):
            with self.assertRaisesRegex(ClaudeFWFCompleteProbeError, "未批准环境变量"):
                validate_probe_catalog()


class ClaudeFWFV4RunnerTests(unittest.TestCase):
    @staticmethod
    def _write_request(path: Path, request_line: str, body: dict | None = None) -> None:
        raw_body = json.dumps(body, separators=(",", ":")).encode() if body else b""
        headers = ["Host: api.anthropic.com"]
        if raw_body:
            headers.extend(
                [
                    "Content-Type: application/json",
                    f"Content-Length: {len(raw_body)}",
                ]
            )
        path.write_bytes(
            (request_line + "\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode()
            + raw_body
        )

    def test_usage_dimension_proves_essential_traffic_negative_without_making_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory)
            self._write_request(
                relay / "conn001.client_to_upstream.bin",
                "GET /api/oauth/profile HTTP/1.1",
            )
            summary = {
                "inner_result": {
                    "input_complete": True,
                    "invocation": {
                        "environment": {
                            "values": {
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
                            }
                        }
                    },
                }
            }
            result = validate_complete_probe_evidence(
                probe_id="v4-tui-usage",
                relay_root=relay,
                scenario_summary=summary,
                relay_integrity={},
                pcap_receipt={},
            )
            self.assertEqual(result["result"], "passed")

            self._write_request(
                relay / "conn002.client_to_upstream.bin",
                "GET /api/oauth/usage HTTP/1.1",
            )
            unexpected = validate_complete_probe_evidence(
                probe_id="v4-tui-usage",
                relay_root=relay,
                scenario_summary=summary,
                relay_integrity={},
                pcap_receipt={},
            )
            self.assertEqual(unexpected["result"], "failed")
            self.assertIn("aux.usage", unexpected["failed_dimensions"])

    def test_tui_lifecycle_does_not_require_profile_cache_miss(self) -> None:
        """TUI 身份由官方生命周期请求证明，不依赖账号缓存是否触发 profile。"""

        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory)
            self._write_request(
                relay / "conn001.client_to_upstream.bin",
                "HEAD /api/hello HTTP/1.1",
            )
            summary = {
                "inner_result": {
                    "input_complete": True,
                    "invocation": {
                        "environment": {
                            "values": {
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
                            }
                        }
                    },
                }
            }
            result = validate_complete_probe_evidence(
                probe_id="v4-tui-usage",
                relay_root=relay,
                scenario_summary=summary,
                relay_integrity={},
                pcap_receipt={},
            )
            self.assertEqual(result["result"], "passed")

    def test_advisor_positive_requires_real_wire_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = Path(directory)
            body = {
                "model": "claude-sonnet-5",
                "tools": [
                    {
                        "type": "advisor_20260301",
                        "name": "advisor",
                        "model": "claude-fable-5",
                    }
                ],
            }
            self._write_request(
                relay / "conn001.client_to_upstream.bin",
                "POST /v1/messages?beta=true HTTP/1.1",
                body,
            )
            result = validate_complete_probe_evidence(
                probe_id="v4-advisor-enabled-positive",
                relay_root=relay,
                scenario_summary={},
                relay_integrity={},
                pcap_receipt={},
            )
            self.assertEqual(result["result"], "passed")

    def test_print_probe_binds_invocation_and_result(self) -> None:
        record = {
            "type": "result",
            "subtype": "success",
            "result": "FW_F_V4_OK",
            "is_error": False,
        }
        completed = subprocess.CompletedProcess(
            ["claude"], 0, stdout=json.dumps(record) + "\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v4.subprocess.run",
                return_value=completed,
            ):
                result = run_claude_fw_f_complete_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v4-models-privacy-state",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            saved = json.loads((output / "v4-summary.json").read_text())
            invocation = saved["inner_result"]["invocation"]
            self.assertTrue(invocation["argv_sha256"])
            self.assertTrue(invocation["environment"]["sha256"])

    def test_tui_compact_requires_both_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v4.drive_tui_sequence",
                return_value={
                    "all_inputs_sent": True,
                    "sent_inputs": list(get_probe("v4-tui-compact").tui_inputs),
                    "steps": [],
                    "transcript": "compact complete",
                },
            ):
                result = run_claude_fw_f_complete_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v4-tui-compact",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            self.assertTrue(result["inner_result"]["input_complete"])

    def test_background_probe_uses_native_background_entrypoint(self) -> None:
        launched = subprocess.CompletedProcess(
            ["claude"], 0, stdout="backgrounded · 6d60a481\n", stderr=""
        )
        listed = subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout=json.dumps(
                [{"id": "6d60a481", "state": "done"}]
            ),
            stderr="",
        )
        stopped = subprocess.CompletedProcess(
            ["claude"], 0, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            config = Path(directory) / "config"
            state = config / "jobs" / "6d60a481" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    {
                        "state": "done",
                        "detail": "replied as instructed",
                        "tempo": "idle",
                        "output": "FW_F_V4_OK",
                    }
                )
            )
            with (
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4.subprocess.run",
                    side_effect=(launched, listed, stopped),
                ) as run,
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4.time.sleep"
                ),
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4._background_process_count",
                    return_value=0,
                ),
            ):
                result = run_claude_fw_f_complete_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v4-background",
                    environment={
                        "PATH": "/usr/bin",
                        "CLAUDE_CONFIG_DIR": str(config),
                    },
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )

            command = run.call_args_list[0].args[0]
            self.assertNotIn("-p", command)
            self.assertNotIn("--no-session-persistence", command)
            self.assertNotIn("--prompt-suggestions", command)
            self.assertNotIn("--tools", command)
            self.assertIn("--bg", command)
            self.assertEqual(command[-2:], ["--bg", get_probe("v4-background").prompt])
            self.assertTrue(result["valid"])
            self.assertTrue(result["inner_result"]["background_control"]["valid"])
            self.assertEqual(
                result["inner_result"]["background_control"]["final_state"], "done"
            )
            self.assertTrue(
                result["inner_result"]["background_control"]["daemon_settle_waited"]
            )
            self.assertTrue(
                result["inner_result"]["background_control"]["stopped_by_driver"]
            )
            self.assertEqual(
                result["inner_result"]["background_control"][
                    "remaining_background_process_count"
                ],
                0,
            )

    def test_background_probe_stops_idle_session_after_observed_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            config = Path(directory) / "config"
            state = config / "jobs" / "6d60a481" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"state": "running", "output": "FW_F_V4_OK"}))

            def run_command(command, **_kwargs):
                if "--bg" in command:
                    return subprocess.CompletedProcess(
                        command, 0, stdout="backgrounded · 6d60a481\n", stderr=""
                    )
                if "agents" in command:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps([{"id": "6d60a481", "state": "running"}]),
                        stderr="",
                    )
                if "stop" in command:
                    state.write_text(json.dumps({"state": "stopped", "output": "FW_F_V4_OK"}))
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                raise AssertionError(command)

            with (
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4.subprocess.run",
                    side_effect=run_command,
                ),
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4.time.sleep"
                ),
                mock.patch(
                    "tools.official_client_capture.capturelib.claude_fw_f_v4._background_process_count",
                    return_value=0,
                ),
            ):
                result = run_claude_fw_f_complete_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v4-background",
                    environment={
                        "PATH": "/usr/bin",
                        "CLAUDE_CONFIG_DIR": str(config),
                    },
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )

            control = result["inner_result"]["background_control"]
            self.assertTrue(result["valid"])
            self.assertTrue(control["response_observed"])
            self.assertTrue(control["stopped_by_driver"])
            self.assertEqual(control["final_state"], "stopped")


if __name__ == "__main__":
    unittest.main()
