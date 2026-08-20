"""Claude Code 2.1.226 FW-F v3 固定场景目录与驱动测试。"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.capturelib.claude_fw_f_v3 import (
    PROBES,
    PROBE_IDS,
    SYNTHETIC_RESPONSE_PLANS,
    ClaudeFWFProbeError,
    _base_command,
    get_probe,
    run_claude_fw_f_probe,
    validate_probe_catalog,
)


class ClaudeFWFV3CatalogTests(unittest.TestCase):
    def test_catalog_is_closed_and_every_fault_plan_has_probe(self) -> None:
        validate_probe_catalog()
        self.assertEqual(PROBE_IDS, tuple(sorted(PROBES)))
        self.assertGreaterEqual(len(PROBE_IDS), 50)
        self.assertEqual(
            set(SYNTHETIC_RESPONSE_PLANS),
            {probe.response_plan for probe in PROBES.values() if probe.response_plan},
        )

    def test_unknown_probe_and_unapproved_environment_fail_closed(self) -> None:
        with self.assertRaisesRegex(ClaudeFWFProbeError, "未知"):
            get_probe("v3-arbitrary-argv")
        invalid = dataclasses.replace(
            get_probe("v3-baseline"),
            probe_id="v3-invalid-env",
            injected_env=(("LD_PRELOAD", "/tmp/not-allowed.so"),),
        )
        with mock.patch.dict(PROBES, {"v3-invalid-env": invalid}, clear=False):
            with self.assertRaisesRegex(ClaudeFWFProbeError, "未批准环境变量"):
                validate_probe_catalog()

    def test_resume_and_fork_commands_are_frozen(self) -> None:
        session_id = "11111111-2222-4333-8444-555555555555"
        resume = _base_command(
            "/opt/claude",
            "claude-sonnet-5",
            get_probe("v3-session-resume"),
            session_id=session_id,
            resume=True,
        )
        fork = _base_command(
            "/opt/claude",
            "claude-sonnet-5",
            get_probe("v3-session-fork"),
            session_id=session_id,
            resume=True,
            fork=True,
        )
        self.assertIn("--resume", resume)
        self.assertIn(session_id, resume)
        self.assertNotIn("--fork-session", resume)
        self.assertIn("--fork-session", fork)
        self.assertNotIn("--no-session-persistence", resume)

    def test_custom_agent_safe_mode_is_an_explicit_matrix(self) -> None:
        enabled = _base_command(
            "/opt/claude",
            "claude-sonnet-5",
            get_probe("v3-custom-agent"),
        )
        rejected = _base_command(
            "/opt/claude",
            "claude-sonnet-5",
            get_probe("v3-custom-agent-safe-mode"),
        )
        self.assertNotIn("--safe-mode", enabled)
        self.assertIn("--safe-mode", rejected)
        self.assertEqual(
            get_probe("v3-custom-agent-safe-mode").message_request_expectation,
            "zero",
        )

    def test_custom_header_valid_and_invalid_cases_are_separate(self) -> None:
        legal = get_probe("v3-custom-header-grammar").env_dict()[
            "ANTHROPIC_CUSTOM_HEADERS"
        ]
        invalid = get_probe("v3-custom-header-invalid-name")
        self.assertNotIn(":value-without-name", legal)
        self.assertEqual(invalid.expected_outcome, "failure")
        self.assertEqual(invalid.message_request_expectation, "zero")
        self.assertTrue(invalid.local_error_marker)

    def test_tui_prompt_does_not_echo_the_success_marker(self) -> None:
        probe = get_probe("v3-tui")
        self.assertNotIn(probe.marker, probe.prompt)


class ClaudeFWFV3RunnerTests(unittest.TestCase):
    @staticmethod
    def _completed(
        *,
        marker: str,
        returncode: int,
        subtype: str,
        is_error: bool = False,
        api_error_status: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        record = {
            "type": "result",
            "subtype": subtype,
            "result": marker,
            "is_error": is_error,
        }
        if api_error_status is not None:
            record["api_error_status"] = api_error_status
        return subprocess.CompletedProcess(
            args=["claude"],
            returncode=returncode,
            stdout=json.dumps(record) + "\n",
            stderr="",
        )

    def test_success_probe_binds_invocation_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            completed = self._completed(
                marker="FW_F_V3_OK", returncode=0, subtype="success"
            )
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v3._run",
                return_value=completed,
            ):
                result = run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-baseline",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            invocation = json.loads((output / "invocation.json").read_text())
            self.assertEqual(invocation["probe_id"], "v3-baseline")
            self.assertTrue(invocation["invocations"][0]["argv_sha256"])
            self.assertTrue(
                invocation["invocations"][0]["environment"]["sha256"]
            )

    def test_success_probe_does_not_compare_model_answer_text(self) -> None:
        """模型回答内容属于行为层，不能成为官方出站一致性判据。"""

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            completed = self._completed(
                marker="与冻结标记不同的正常回答",
                returncode=0,
                subtype="success",
            )
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v3._run",
                return_value=completed,
            ):
                result = run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-baseline",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            self.assertEqual(result["marker_results"], [False])
            self.assertEqual(result["success_results"], [True])

    def test_expected_failure_is_valid_only_for_nonzero_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            completed = self._completed(
                marker="fw-f expected failure",
                returncode=1,
                subtype="success",
                is_error=True,
                api_error_status=400,
            )
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v3._run",
                return_value=completed,
            ):
                result = run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-nonretry-400",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            self.assertEqual(result["return_codes"], [1])
            self.assertEqual(result["error_results"], [True])

    def test_local_failure_accepts_frozen_error_without_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            completed = self._completed(
                marker="API Error: Invalid header name: ''",
                returncode=1,
                subtype="success",
                is_error=True,
            )
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v3._run",
                return_value=completed,
            ):
                result = run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-custom-header-invalid-name",
                    environment={
                        "PATH": "/usr/bin",
                        "ANTHROPIC_CUSTOM_HEADERS": ":value-without-name",
                    },
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            self.assertEqual(result["local_error_results"], [True])
            self.assertEqual(result["message_request_expectation"], "zero")

    def test_local_failure_can_be_stderr_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=1,
                stdout="",
                stderr="--agent 'fw-f-reviewer' not found. Available agents: claude\n",
            )
            with mock.patch(
                "tools.official_client_capture.capturelib.claude_fw_f_v3._run",
                return_value=completed,
            ):
                result = run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-custom-agent-safe-mode",
                    environment={"PATH": "/usr/bin"},
                    output_dir=output,
                    timeout=30,
                    known_secrets={},
                )
            self.assertTrue(result["valid"])
            self.assertEqual(result["local_error_results"], [True])

    def test_catalog_environment_must_be_present_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ClaudeFWFProbeError, "运行环境与目录不一致"):
                run_claude_fw_f_probe(
                    claude_bin="/opt/claude",
                    model="claude-sonnet-5",
                    probe_id="v3-max-output-tokens",
                    environment={"PATH": "/usr/bin"},
                    output_dir=Path(directory) / "result",
                    timeout=30,
                    known_secrets={},
                )


if __name__ == "__main__":
    unittest.main()
