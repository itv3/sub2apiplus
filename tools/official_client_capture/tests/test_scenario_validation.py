"""官方客户端场景结果的严格离线校验测试。"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.official_client_capture.capturelib.scenarios import (
    CLAUDE_AGENT_EXPECTATIONS,
    CLAUDE_AGENT_MAIN_MARKERS,
    CODEX_HOOK_TRUST_WARNING,
    _contains_runtime_secret,
    _run_owned_cli_command,
    _run_claude_two_turns,
    _validate_claude,
    _validate_codex,
    run_claude_scenario,
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
                        "id": "tool-1",
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


def _claude_agent_records(scenario: str) -> list[dict[str, object]]:
    """构造与 2.1.226 扁平转发格式一致的同步 Agent 链。"""

    expectations = CLAUDE_AGENT_EXPECTATIONS[scenario]
    records: list[dict[str, object]] = []
    owners: list[str | None] = []
    tool_ids: list[str] = []
    owner: str | None = None
    for index, (description, prompt, _marker) in enumerate(expectations, start=1):
        tool_id = f"agent-tool-{index}"
        record: dict[str, object] = {
            "type": "assistant",
            "parent_tool_use_id": owner,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Agent",
                        "input": {
                            "description": description,
                            "prompt": prompt,
                            "run_in_background": False,
                            "subagent_type": "general-purpose",
                        },
                    }
                ]
            },
        }
        if owner is not None:
            parent_description = expectations[index - 2][0]
            record.update(
                {
                    "subagent_type": "general-purpose",
                    "task_description": parent_description,
                }
            )
        records.append(record)
        owners.append(owner)
        tool_ids.append(tool_id)
        owner = tool_id

    for index in reversed(range(len(expectations))):
        description, _prompt, marker = expectations[index]
        tool_id = tool_ids[index]
        records.append(
            {
                "type": "assistant",
                "parent_tool_use_id": tool_id,
                "subagent_type": "general-purpose",
                "task_description": description,
                "message": {"content": [{"type": "text", "text": marker}]},
            }
        )
        result_record: dict[str, object] = {
            "type": "user",
            "parent_tool_use_id": owners[index],
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": marker,
                    }
                ]
            },
        }
        if owners[index] is not None:
            result_record.update(
                {
                    "subagent_type": "general-purpose",
                    "task_description": expectations[index - 1][0],
                }
            )
        records.append(result_record)
    records.append(
        {
            "type": "result",
            "subtype": "success",
            "result": CLAUDE_AGENT_MAIN_MARKERS[scenario],
        }
    )
    return records


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
        extra = json.loads(json.dumps(records[0]))
        extra["message"]["content"][0]["id"] = "tool-2"
        records.insert(1, extra)
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

    def test_nested_agent_scenarios_require_exact_parent_chain(self) -> None:
        for scenario, expected_depth in (("a1", 1), ("a2", 2), ("a3", 3)):
            with self.subTest(scenario=scenario):
                summary = _validate_claude(
                    scenario,
                    0,
                    _jsonl(_claude_agent_records(scenario)),
                )
                self.assertTrue(summary["valid"], summary)
                self.assertEqual(summary["tool_use_count"], expected_depth)
                self.assertTrue(summary["agent_chain_valid"])

    def test_nested_agent_main_result_allows_explanation_and_standalone_marker(self) -> None:
        records = _claude_agent_records("a2")
        records[-1]["result"] = "并发和余额边界审查完成。\n\nD2_MAIN_OK"
        summary = _validate_claude("a2", 0, _jsonl(records))
        self.assertTrue(summary["valid"], summary)

    def test_nested_agent_wrong_parent_is_rejected(self) -> None:
        records = _claude_agent_records("a2")
        second_use = next(
            record
            for record in records
            if record.get("type") == "assistant"
            and record.get("parent_tool_use_id") == "agent-tool-1"
            and record.get("message", {}).get("content", [{}])[0].get("type")
            == "tool_use"
        )
        second_use["parent_tool_use_id"] = None
        summary = _validate_claude("a2", 0, _jsonl(records))
        self.assertFalse(summary["valid"])

    def test_nested_agent_accepts_one_standalone_marker_with_explanation(self) -> None:
        records = _claude_agent_records("a3")
        leaf = next(
            record
            for record in records
            if record.get("type") == "assistant"
            and record.get("parent_tool_use_id") == "agent-tool-3"
            and record.get("message", {}).get("content", [{}])[0].get("type")
            == "text"
        )
        leaf["message"]["content"][0]["text"] = (
            "19+23=42。\n\nD3_C3_OK\n\n这是无副作用校验。"
        )
        summary = _validate_claude("a3", 0, _jsonl(records))
        self.assertTrue(summary["valid"], summary)
        self.assertFalse(summary["agent_chain"][2]["child_marker_exact"])
        self.assertTrue(summary["agent_chain"][2]["child_marker_present"])

        leaf["message"]["content"][0]["text"] += "\nD3_FAIL"
        self.assertFalse(_validate_claude("a3", 0, _jsonl(records))["valid"])

    def test_invocation_archives_exact_final_argv_and_redacted_environment(self) -> None:
        stdout = _jsonl(
            [{"type": "result", "subtype": "success", "result": "S1_OK"}]
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "tools.official_client_capture.capturelib.scenarios._run_owned_cli_command",
            return_value=subprocess.CompletedProcess(
                ["/bin/claude"], 0, stdout=stdout, stderr=""
            ),
        ) as runner:
            summary = run_claude_scenario(
                claude_bin="/bin/claude",
                model="claude-test",
                scenario="s1",
                environment={
                    "PATH": "/usr/bin",
                    "CLAUDE_CODE_OAUTH_TOKEN": "CANARY-SECRET",
                },
                output_dir=Path(directory),
                timeout=5,
                runtime_secret="CANARY-SECRET",
                known_secrets={"oauth_access": "CANARY-SECRET"},
            )
            executed_argv = runner.call_args.args[0]
            invocation = json.loads(
                (Path(directory) / "invocation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(invocation["argv_redacted"], executed_argv)
            self.assertEqual(summary["invocation"]["argv_redacted"], executed_argv)
            self.assertTrue(
                invocation["environment"]["values"]["CLAUDE_CODE_OAUTH_TOKEN"][
                    "redacted"
                ]
            )
            self.assertNotIn(
                "CANARY-SECRET",
                (Path(directory) / "invocation.json").read_text(encoding="utf-8"),
            )


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
