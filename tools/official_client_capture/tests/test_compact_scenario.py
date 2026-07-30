"""Codex app-server compact 场景的纯函数门禁。"""

from __future__ import annotations

import unittest

from tools.official_client_capture.run_codex_compact_scenario import (
    AppServerClient,
    build_app_server_command,
    extract_thread_id,
    protocol_requests,
    provider_values,
)


class CompactScenarioTest(unittest.TestCase):
    def test_official_provider_triggers_codex_zstd_identity(self) -> None:
        values = provider_values("official-http")
        self.assertIn('model_providers.official_openai_http.name="OpenAI"', values)
        self.assertIn(
            'model_providers.official_openai_http.http_headers.version="0.145.0"',
            values,
        )
        self.assertTrue(any("supports_websockets=false" in value for value in values))

    def test_official_provider_accepts_target_codex_version(self) -> None:
        values = provider_values("official-http", "0.146.0")
        self.assertIn(
            'model_providers.official_openai_http.http_headers.version="0.146.0"',
            values,
        )
        with self.assertRaises(ValueError):
            provider_values("official-http", '0.146.0"')

    def test_command_contains_no_auth_value(self) -> None:
        command = build_app_server_command("/bin/codex", "sub2api-http")
        self.assertEqual(command[:4], ["/bin/codex", "app-server", "--strict-config", "--stdio"])
        self.assertNotIn("CANARY-SECRET", "\n".join(command))

    def test_protocol_has_turn_then_compact_lifecycle(self) -> None:
        initial = protocol_requests("gpt-5.4")
        self.assertEqual(initial["initialize"]["method"], "initialize")
        self.assertEqual(initial["thread_start"]["method"], "thread/start")
        active = protocol_requests("gpt-5.4", "thread-123")
        self.assertEqual(active["turn_start"]["params"]["threadId"], "thread-123")
        self.assertEqual(active["compact_start"]["method"], "thread/compact/start")

    def test_extract_thread_id_fails_closed(self) -> None:
        self.assertEqual(
            extract_thread_id({"result": {"thread": {"id": "thread-123"}}}),
            "thread-123",
        )
        with self.assertRaises(RuntimeError):
            extract_thread_id({"result": {}})

    def test_compact_completion_uses_second_turn_completed(self) -> None:
        client = object.__new__(AppServerClient)
        client.records = [
            {"method": "turn/completed"},
            {"method": "turn/completed"},
        ]
        self.assertEqual(client.notification_count("turn/completed"), 2)
        self.assertEqual(client.notification_count("thread/compacted"), 0)


if __name__ == "__main__":
    unittest.main()
