"""官方客户端场景结果的严格离线校验测试。"""

from __future__ import annotations

import json
import signal
import sys
import unittest
from unittest.mock import Mock, patch

from tools.official_client_capture.capturelib.scenarios import (
    CODEX_HOOK_TRUST_WARNING,
    _contains_runtime_secret,
    _run_owned_cli_command,
    _run_claude_two_turns,
    _validate_claude,
    _validate_codex,
)


def _jsonl(records: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _claude_s4_records(
    *, command: str = "printf CLAUDE_CAPTURE_TOOL_OK", output: str = "CLAUDE_CAPTURE_TOOL_OK"
) -> list[dict[str, object]]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": output,
                        "is_error": False,
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success", "result": "S4_TOOL_OK"},
    ]


def _codex_command_event(
    *,
    command: str = "/bin/bash -lc 'printf CODEX_CAPTURE_TOOL_OK'",
    output: str = "CODEX_CAPTURE_TOOL_OK",
    exit_code: int = 0,
    status: str = "completed",
) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": command,
            "aggregated_output": output,
            "exit_code": exit_code,
            "status": status,
        },
    }


class ClaudeValidationTest(unittest.TestCase):
    def test_two_turn_reader_handles_multiple_lines_from_one_os_write(self) -> None:
        fake_client = r'''
import json
import os
import sys

for index in range(2):
    if not sys.stdin.readline():
        raise SystemExit(2)
    payload = (
        json.dumps({"type": "assistant", "turn": index})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "result": "OK"})
        + "\n"
    )
    os.write(sys.stdout.fileno(), payload.encode("utf-8"))
'''
        return_code, stdout, stderr = _run_claude_two_turns(
            [sys.executable, "-c", fake_client], {}, 5
        )
        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(
            sum(
                json.loads(line).get("type") == "result"
                for line in stdout.splitlines()
            ),
            2,
        )

    def test_cli_process_group_is_terminated_on_interruption(self) -> None:
        process = Mock(pid=4321, stdin=None, stdout=None, stderr=None)
        process.communicate.side_effect = KeyboardInterrupt()
        process.poll.return_value = None
        process.wait.return_value = 0
        with patch(
            "tools.official_client_capture.capturelib.scenarios.subprocess.Popen",
            return_value=process,
        ), patch(
            "tools.official_client_capture.capturelib.scenarios.os.killpg"
        ) as kill_group, self.assertRaises(KeyboardInterrupt):
            _run_owned_cli_command(["/bin/client"], {}, 1)
        kill_group.assert_called_once_with(4321, signal.SIGTERM)

    def test_raw_output_secret_detection_does_not_depend_on_saved_redaction(self) -> None:
        self.assertTrue(
            _contains_runtime_secret(
                "CANARY-SECRET", "safe stdout", "leak=CANARY-SECRET"
            )
        )
        self.assertFalse(
            _contains_runtime_secret("CANARY-SECRET", "safe stdout", "safe stderr")
        )

    def test_s4_requires_one_exact_command_and_exact_output(self) -> None:
        summary = _validate_claude("s4", 0, _jsonl(_claude_s4_records()))
        self.assertTrue(summary["valid"])

        wrong_command = _validate_claude(
            "s4",
            0,
            _jsonl(_claude_s4_records(command="printf CLAUDE_CAPTURE_TOOL_OK; env")),
        )
        self.assertFalse(wrong_command["valid"])

        wrong_output = _validate_claude(
            "s4",
            0,
            _jsonl(_claude_s4_records(output="CLAUDE_CAPTURE_TOOL_OK\nextra")),
        )
        self.assertFalse(wrong_output["valid"])

        tool_error_records = _claude_s4_records()
        tool_error_records[1]["message"]["content"][0]["is_error"] = True
        self.assertFalse(
            _validate_claude("s4", 0, _jsonl(tool_error_records))["valid"]
        )

    def test_marker_without_tool_and_extra_tool_are_rejected(self) -> None:
        marker_only = _validate_claude(
            "s4",
            0,
            _jsonl(
                [{"type": "result", "subtype": "success", "result": "S4_TOOL_OK"}]
            ),
        )
        self.assertFalse(marker_only["valid"])

        records = _claude_s4_records()
        records.insert(1, records[0])
        self.assertFalse(_validate_claude("s4", 0, _jsonl(records))["valid"])

    def test_non_tool_scenario_requires_zero_tool_items(self) -> None:
        valid = _validate_claude(
            "s1",
            0,
            _jsonl([{"type": "result", "subtype": "success", "result": "S1_OK"}]),
        )
        self.assertTrue(valid["valid"])

        records = _claude_s4_records()
        records[-1] = {"type": "result", "subtype": "success", "result": "S1_OK"}
        self.assertFalse(_validate_claude("s1", 0, _jsonl(records))["valid"])

    def test_runtime_secret_exposure_forces_failure(self) -> None:
        summary = _validate_claude(
            "s4",
            0,
            _jsonl(_claude_s4_records()),
            runtime_secret_exposed=True,
        )
        self.assertFalse(summary["valid"])
        self.assertTrue(summary["runtime_secret_exposed"])


class CodexValidationTest(unittest.TestCase):
    @staticmethod
    def _validate_s4(
        event: dict[str, object] | None,
        *,
        hook_audit: list[dict[str, bool]] | None = None,
        runtime_secret_exposed: bool = False,
        last_message: str = "S4_TOOL_OK",
    ) -> dict[str, object]:
        events = [event] if event else []
        return _validate_codex(
            scenario="s4",
            turns=[
                {
                    "return_code": 0,
                    "events": events,
                    "last_message": last_message,
                }
            ],
            thread_id="",
            hook_audit=(hook_audit if hook_audit is not None else [{"allowed": True}]),
            hook_audit_valid=True,
            runtime_secret_exposed=runtime_secret_exposed,
        )

    def test_s4_requires_exact_completed_command_and_output(self) -> None:
        self.assertTrue(self._validate_s4(_codex_command_event())["valid"])
        self.assertTrue(
            self._validate_s4(
                _codex_command_event(command="printf CODEX_CAPTURE_TOOL_OK")
            )["valid"]
        )
        for event in (
            _codex_command_event(command="printf CODEX_CAPTURE_TOOL_OK; env"),
            _codex_command_event(
                command="/bin/bash -lc 'printf CODEX_CAPTURE_TOOL_OK; env'"
            ),
            _codex_command_event(output="CODEX_CAPTURE_TOOL_OK\n"),
            _codex_command_event(exit_code=1),
            _codex_command_event(status="failed"),
        ):
            with self.subTest(event=event):
                self.assertFalse(self._validate_s4(event)["valid"])

    def test_marker_without_command_or_with_extra_command_is_rejected(self) -> None:
        self.assertFalse(self._validate_s4(None)["valid"])
        extra = _codex_command_event()
        summary = _validate_codex(
            scenario="s4",
            turns=[
                {
                    "return_code": 0,
                    "events": [extra, extra],
                    "last_message": "S4_TOOL_OK",
                }
            ],
            thread_id="",
            hook_audit=[{"allowed": True}, {"allowed": True}],
            hook_audit_valid=True,
            runtime_secret_exposed=False,
        )
        self.assertFalse(summary["valid"])

    def test_hook_denial_and_runtime_secret_force_failure(self) -> None:
        self.assertFalse(
            self._validate_s4(
                _codex_command_event(), hook_audit=[{"allowed": False}]
            )["valid"]
        )
        self.assertFalse(
            self._validate_s4(
                _codex_command_event(), runtime_secret_exposed=True
            )["valid"]
        )

    def test_non_tool_scenario_requires_zero_tool_and_hook_records(self) -> None:
        summary = _validate_codex(
            scenario="s1",
            turns=[{"return_code": 0, "events": [], "last_message": "S1_OK"}],
            thread_id="",
            hook_audit=[],
            hook_audit_valid=True,
            runtime_secret_exposed=False,
        )
        self.assertTrue(summary["valid"])

        summary = _validate_codex(
            scenario="s1",
            turns=[
                {
                    "return_code": 0,
                    "events": [_codex_command_event()],
                    "last_message": "S1_OK",
                }
            ],
            thread_id="",
            hook_audit=[{"allowed": True}],
            hook_audit_valid=True,
            runtime_secret_exposed=False,
        )
        self.assertFalse(summary["valid"])

    def test_exact_hook_trust_warning_is_allowed(self) -> None:
        warning = {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": CODEX_HOOK_TRUST_WARNING,
            },
        }
        summary = _validate_codex(
            scenario="s1",
            turns=[
                {
                    "return_code": 0,
                    "events": [warning, warning],
                    "last_message": "S1_OK",
                }
            ],
            thread_id="thread-1",
            hook_audit=[],
            hook_audit_valid=True,
            runtime_secret_exposed=False,
        )
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["hook_trust_warning_count"], 2)
        self.assertEqual(summary["error_event_count"], 0)

    def test_other_error_event_is_rejected(self) -> None:
        error = {
            "type": "item.completed",
            "item": {"type": "error", "message": "模型请求失败"},
        }
        summary = _validate_codex(
            scenario="s1",
            turns=[
                {
                    "return_code": 0,
                    "events": [error],
                    "last_message": "S1_OK",
                }
            ],
            thread_id="thread-1",
            hook_audit=[],
            hook_audit_valid=True,
            runtime_secret_exposed=False,
        )
        self.assertFalse(summary["valid"])
        self.assertEqual(summary["hook_trust_warning_count"], 0)
        self.assertEqual(summary["error_event_count"], 1)

    def test_final_marker_must_be_the_only_reply(self) -> None:
        summary = self._validate_s4(
            _codex_command_event(), last_message="done: S4_TOOL_OK"
        )
        self.assertFalse(summary["valid"])


if __name__ == "__main__":
    unittest.main()
