"""Codex 精确 Bash 门禁的离线测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "exact_bash_hook.py"
)


class ExactBashHookTest(unittest.TestCase):
    def _run_hook(
        self, payload: object, *, expected: str | None
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, bool]]]:
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            command = [
                sys.executable,
                str(HOOK_PATH),
                "--audit-file",
                str(audit_path),
            ]
            if expected is None:
                command.append("--deny-all")
            else:
                command.extend(["--expected-command", expected])
            completed = subprocess.run(
                command,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            records = (
                [json.loads(line) for line in audit_path.read_text().splitlines()]
                if audit_path.exists()
                else []
            )
            if audit_path.exists():
                self.assertEqual(audit_path.stat().st_mode & 0o777, 0o600)
            return completed, records

    @staticmethod
    def _payload(command: str) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def test_exact_command_is_the_only_allowed_value(self) -> None:
        expected = "printf CODEX_CAPTURE_TOOL_OK"
        completed, records = self._run_hook(
            self._payload(expected), expected=expected
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(records, [{"allowed": True}])

    def test_command_variants_are_denied_without_auditing_command_text(self) -> None:
        expected = "printf CODEX_CAPTURE_TOOL_OK"
        variants = (
            "env",
            "cat /root/.codex/auth.json",
            f"{expected}; env",
            f"{expected}\n",
            f"{expected} ",
            "/usr/bin/printf CODEX_CAPTURE_TOOL_OK",
            f"/bin/bash -lc '{expected}'",
        )
        for command in variants:
            with self.subTest(command=command):
                completed, records = self._run_hook(
                    self._payload(command), expected=expected
                )
                self.assertEqual(completed.returncode, 0)
                decision = json.loads(completed.stdout)
                output = decision["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertEqual(records, [{"allowed": False}])
                self.assertNotIn(command, json.dumps(records, ensure_ascii=False))

    def test_non_tool_scenario_denies_even_the_marker_command(self) -> None:
        completed, records = self._run_hook(
            self._payload("printf CODEX_CAPTURE_TOOL_OK"), expected=None
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertEqual(records, [{"allowed": False}])

    def test_malformed_payload_fails_closed(self) -> None:
        completed, records = self._run_hook(
            {"tool_name": "Bash"}, expected="printf CODEX_CAPTURE_TOOL_OK"
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            json.loads(completed.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertEqual(records, [{"allowed": False}])


if __name__ == "__main__":
    unittest.main()
