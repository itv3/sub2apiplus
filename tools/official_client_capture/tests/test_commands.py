"""客户端与抓包命令测试。"""

from __future__ import annotations

import unittest
import tomllib
from pathlib import Path

from tools.official_client_capture.capturelib.lifecycle import process_command
from tools.official_client_capture.capturelib.model import build_campaign_plan
from tools.official_client_capture.capturelib.scenarios import (
    build_claude_command,
    build_codex_command,
    build_codex_config_preflight_command,
    codex_provider_values,
)


class CommandTest(unittest.TestCase):
    def _case(self, task: str, transport: str, evidence: str = "direct"):
        plan = build_campaign_plan(
            task=task,
            batch_id="command-test",
            scenarios=("s1",),
            evidence_modes=(evidence,),
            sub2api_base_url=(
                "https://gateway.example.com" if task == "api" else None
            ),
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        return next(
            case
            for case in plan.cases
            if case.product == "codex" and case.transport == transport
        )

    def test_claude_command_has_no_auth_value(self) -> None:
        command = build_claude_command(
            claude_bin="/bin/claude", model="claude-test", scenario="s4"
        )
        text = "\n".join(command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("Bash", command)
        allowed_index = command.index("--allowedTools")
        self.assertEqual(
            command[allowed_index + 1],
            "Bash(printf CLAUDE_CAPTURE_TOOL_OK)",
        )
        self.assertEqual(command.count("Bash"), 1)
        self.assertNotIn("CANARY-SECRET", text)

    def test_claude_nested_agent_command_uses_cli_alias_without_budget_cap(self) -> None:
        command = build_claude_command(
            claude_bin="/bin/claude", model="claude-test", scenario="a3"
        )
        self.assertEqual(command[command.index("--tools") + 1], "Task")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Task")
        self.assertNotIn("--max-budget-usd", command)
        self.assertIn("dontAsk", command)

    def test_codex_api_http_and_ws_are_separate(self) -> None:
        http_case = self._case("api", "http")
        ws_case = self._case("api", "ws")
        http_values = codex_provider_values(
            case=http_case, api_key_env="SUB2API_CAPTURE_API_KEY"
        )
        ws_values = codex_provider_values(
            case=ws_case, api_key_env="SUB2API_CAPTURE_API_KEY"
        )
        self.assertIn(
            "model_providers.sub2api_capture_http.supports_websockets=false",
            http_values,
        )
        self.assertIn(
            "model_providers.sub2api_capture_ws.supports_websockets=true", ws_values
        )
        self.assertTrue(
            any("https://gateway.example.com/v1" in value for value in http_values)
        )

    def test_codex_oauth_ws_uses_builtin_provider(self) -> None:
        case = self._case("oauth", "ws")
        self.assertEqual(
            codex_provider_values(case=case, api_key_env="IGNORED"), ()
        )

    def test_codex_oauth_http_provider_matches_builtin_identity(self) -> None:
        case = self._case("oauth", "http")
        provider_values = codex_provider_values(
            case=case,
            api_key_env="IGNORED",
            codex_version="0.145.0",
        )
        self.assertIn('model_provider="official_openai_http"', provider_values)
        self.assertIn(
            'model_providers.official_openai_http.name="OpenAI"', provider_values
        )
        self.assertIn(
            "model_providers.official_openai_http.supports_websockets=false",
            provider_values,
        )
        self.assertIn(
            'model_providers.official_openai_http.http_headers.version="0.145.0"',
            provider_values,
        )
        command = build_codex_command(
            codex_bin="/bin/codex",
            model="gpt-test",
            case=case,
            api_key_env="IGNORED",
            resume=False,
            scenario="s1",
            hook_audit_path=Path("/capture/result/hook-audit.jsonl"),
            codex_version="0.145.0",
        )
        command_text = "\n".join(command)
        self.assertIn("official_openai_http", command_text)
        self.assertNotIn("model_catalog_json", command_text)

    def test_codex_command_keeps_private_profile_and_workdir(self) -> None:
        case = self._case("api", "http")
        command = build_codex_command(
            codex_bin="/bin/codex",
            model="gpt-test",
            case=case,
            api_key_env="SUB2API_CAPTURE_API_KEY",
            resume=False,
            scenario="s4",
            hook_audit_path=Path("/capture/result/hook-audit.jsonl"),
            hook_path=Path(
                "/capture/tools/official_client_capture/hooks/exact_bash_hook.py"
            ),
        )
        self.assertIn("/work", command)
        self.assertIn('permissions.capture-tool.extends=":read-only"', command)
        self.assertIn(
            'permissions.capture-tool.filesystem={"/root"="deny","/capture"="deny"}',
            command,
        )
        self.assertNotIn("-s", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--dangerously-bypass-hook-trust", command)
        for value in (
            "check_for_update_on_startup=false",
            "analytics.enabled=false",
            "feedback.enabled=false",
            'otel.exporter="none"',
            "otel.log_user_prompt=false",
        ):
            self.assertIn(value, command)
        command_text = "\n".join(command)
        self.assertIn("features.hooks=true", command)
        self.assertIn('shell_environment_policy.inherit="none"', command)
        self.assertIn("--expected-command", command_text)
        self.assertIn("printf CODEX_CAPTURE_TOOL_OK", command_text)
        self.assertNotIn("CANARY-SECRET", "\n".join(command))
        for index, value in enumerate(command[:-1]):
            if value == "-c":
                _key, raw_toml = command[index + 1].split("=", 1)
                tomllib.loads(f"value={raw_toml}")

    def test_codex_resume_omits_unsupported_workdir_and_sandbox(self) -> None:
        case = self._case("api", "http")
        command = build_codex_command(
            codex_bin="/bin/codex",
            model="gpt-test",
            case=case,
            api_key_env="SUB2API_CAPTURE_API_KEY",
            resume=True,
            scenario="s2",
            hook_audit_path=Path("/capture/result/hook-audit.jsonl"),
            hook_path=Path(
                "/capture/tools/official_client_capture/hooks/exact_bash_hook.py"
            ),
        )
        self.assertEqual(command[:3], ["/bin/codex", "exec", "resume"])
        self.assertNotIn("-C", command)
        self.assertNotIn("/work", command)
        self.assertNotIn("-s", command)
        self.assertIn('permissions.capture-tool.extends=":read-only"', command)
        self.assertIn(
            'permissions.capture-tool.filesystem={"/root"="deny","/capture"="deny"}',
            command,
        )
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--dangerously-bypass-hook-trust", command)
        self.assertIn("analytics.enabled=false", command)
        self.assertIn('otel.exporter="none"', command)
        self.assertIn("--deny-all", "\n".join(command))

    def test_codex_config_preflight_uses_no_model_subcommand(self) -> None:
        case = self._case("api", "ws")
        command = build_codex_config_preflight_command(
            codex_bin="/bin/codex",
            case=case,
            api_key_env="SUB2API_CAPTURE_API_KEY",
            scenario="s4",
            hook_audit_path=Path("/capture/preflight/hook-audit.jsonl"),
            hook_path=Path(
                "/capture/tools/official_client_capture/hooks/exact_bash_hook.py"
            ),
        )
        self.assertEqual(command[-2:], ["features", "list"])
        self.assertNotIn("exec", command)
        self.assertNotIn("--strict-config", command)
        self.assertIn(
            'permissions.capture-tool.filesystem={"/root"="deny","/capture"="deny"}',
            command,
        )
        self.assertNotIn("CANARY-SECRET", "\n".join(command))

    def test_capture_commands_have_no_shell_or_secret(self) -> None:
        for evidence in ("direct", "mitm"):
            case = self._case("api", "http", evidence)
            command = process_command(
                case=case,
                output_dir=Path("/capture/run") / evidence,
                tcpdump_bin="/usr/bin/tcpdump",
                mitmdump_bin="/usr/bin/mitmdump",
                mitm_addon=Path("/capture/addon.py"),
                mitm_confdir=Path("/opt/mitm"),
                mitm_port=18080,
                interface="any",
                target_addresses=("203.0.113.10", "2001:db8::10"),
            )
            self.assertIsInstance(command, list)
            self.assertNotIn("CANARY-SECRET", "\n".join(command))
            if evidence == "direct":
                self.assertEqual(
                    command[-1],
                    "tcp port 443 and (host 203.0.113.10 or host 2001:db8::10)",
                )
            else:
                self.assertIn("127.0.0.1", command)

    def test_direct_bpf_rejects_unvalidated_address(self) -> None:
        case = self._case("api", "http", "direct")
        with self.assertRaises(ValueError):
            process_command(
                case=case,
                output_dir=Path("/capture/run/direct"),
                tcpdump_bin="/usr/bin/tcpdump",
                mitmdump_bin="/usr/bin/mitmdump",
                mitm_addon=Path("/capture/addon.py"),
                mitm_confdir=Path("/opt/mitm"),
                mitm_port=18080,
                interface="any",
                target_addresses=("203.0.113.10 or port 22",),
            )


if __name__ == "__main__":
    unittest.main()
