"""OAuth/API 认证与代理隔离测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.environment import (
    build_case_environment,
    environment_manifest_view,
    parse_injected_env,
    prepare_api_state,
    prepare_claude_oauth_state,
)
from tools.official_client_capture.capturelib.model import (
    ConfigurationError,
    build_campaign_plan,
)


class EnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "old-anthropic",
            "ANTHROPIC_BASE_URL": "https://wrong.example",
            "ANTHROPIC_CUSTOM_HEADERS": "x-wrong: true",
            "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example",
            "ANTHROPIC_VERTEX_BASE_URL": "https://vertex.example",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.example",
            "ANTHROPIC_FOUNDRY_API_KEY": "old-foundry",
            "ANTHROPIC_FOUNDRY_RESOURCE": "old-resource",
            "ANTHROPIC_VERTEX_PROJECT_ID": "old-project",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "AWS_ACCESS_KEY_ID": "old-aws-key",
            "AWS_SECRET_ACCESS_KEY": "old-aws-secret",
            "AWS_SESSION_TOKEN": "old-aws-session",
            "AWS_BEARER_TOKEN_BEDROCK": "old-bedrock-token",
            "AWS_PROFILE": "old-profile",
            "AWS_REGION": "old-region",
            "AWS_DEFAULT_REGION": "old-default-region",
            "GOOGLE_APPLICATION_CREDENTIALS": "/wrong/google.json",
            "GOOGLE_CLOUD_PROJECT": "old-google-project",
            "CLOUD_ML_REGION": "old-cloud-region",
            "OPENAI_API_KEY": "old-openai",
            "OPENAI_BASE_URL": "https://wrong-openai.example",
            "OPENAI_API_BASE": "https://wrong-openai-api.example",
            "OPENAI_API_TYPE": "azure",
            "AZURE_OPENAI_API_KEY": "old-azure",
            "AZURE_OPENAI_ENDPOINT": "https://wrong-azure.example",
            "SUB2API_CAPTURE_API_KEY": "should-not-leak",
            "CUSTOM_CAPTURE_KEY": "custom-should-not-leak",
            "HTTPS_PROXY": "http://wrong-proxy",
            "SSL_CERT_FILE": "/wrong/ca.pem",
        }

    def _case(self, task: str, product: str, evidence: str = "direct"):
        plan = build_campaign_plan(
            task=task,
            batch_id="test-run",
            scenarios=("s1",),
            evidence_modes=(evidence,),
            sub2api_base_url=(
                "https://gateway.example.com" if task == "api" else None
            ),
            api_key_env="CUSTOM_CAPTURE_KEY",
        )
        return next(case for case in plan.cases if case.product == product)

    def test_oauth_removes_every_api_and_proxy_override(self) -> None:
        environment = build_case_environment(
            case=self._case("oauth", "claude"),
            source=self.source,
            api_secret=None,
            api_key_env="CUSTOM_CAPTURE_KEY",
            claude_api_home=None,
            codex_api_home=None,
            proxy_url="http://127.0.0.1:18080",
            ca_bundle=Path("/opt/mitm/ca.pem"),
        )
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_FOUNDRY_RESOURCE",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "CLOUD_ML_REGION",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_API_TYPE",
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "SUB2API_CAPTURE_API_KEY",
            "CUSTOM_CAPTURE_KEY",
            "HTTPS_PROXY",
            "SSL_CERT_FILE",
        ):
            self.assertNotIn(key, environment)

    def test_api_uses_isolated_state_and_only_runtime_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            claude_home, codex_home = prepare_api_state(run_dir)
            case = self._case("api", "codex", "mitm")
            environment = build_case_environment(
                case=case,
                source=self.source,
                api_secret="runtime-canary",
                api_key_env="CUSTOM_CAPTURE_KEY",
                claude_api_home=claude_home,
                codex_api_home=codex_home,
                proxy_url="http://127.0.0.1:18080",
                ca_bundle=Path("/opt/mitm/ca.pem"),
            )
            self.assertEqual(environment["CUSTOM_CAPTURE_KEY"], "runtime-canary")
            self.assertEqual(environment["CODEX_HOME"], str(codex_home))
            self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:18080")
            self.assertEqual(environment["SSL_CERT_FILE"], "/opt/mitm/ca.pem")
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
            self.assertFalse((codex_home / "auth.json").exists())

    def test_explicit_oauth_token_only_enters_claude_oauth_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            oauth_home = prepare_claude_oauth_state(Path(directory))
            claude_environment = build_case_environment(
                case=self._case("oauth", "claude"),
                source=self.source,
                api_secret=None,
                api_key_env="CUSTOM_CAPTURE_KEY",
                claude_api_home=None,
                codex_api_home=None,
                proxy_url="http://127.0.0.1:18080",
                ca_bundle=Path("/opt/mitm/ca.pem"),
                oauth_claude_secret="fresh-oauth-token",
                claude_oauth_home=oauth_home,
            )
            codex_environment = build_case_environment(
                case=self._case("oauth", "codex"),
                source=self.source,
                api_secret=None,
                api_key_env="CUSTOM_CAPTURE_KEY",
                claude_api_home=None,
                codex_api_home=None,
                proxy_url="http://127.0.0.1:18080",
                ca_bundle=Path("/opt/mitm/ca.pem"),
                oauth_claude_secret="fresh-oauth-token",
                claude_oauth_home=oauth_home,
            )
            self.assertEqual(
                claude_environment["CLAUDE_CODE_OAUTH_TOKEN"],
                "fresh-oauth-token",
            )
            self.assertEqual(claude_environment["HOME"], str(oauth_home))
            self.assertEqual(
                claude_environment["CLAUDE_CONFIG_DIR"], str(oauth_home)
            )
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", codex_environment)

    def test_runtime_oauth_token_requires_private_home(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "逐轮私有 HOME"):
            build_case_environment(
                case=self._case("oauth", "claude"),
                source=self.source,
                api_secret=None,
                api_key_env="CUSTOM_CAPTURE_KEY",
                claude_api_home=None,
                codex_api_home=None,
                proxy_url="http://127.0.0.1:18080",
                ca_bundle=Path("/opt/mitm/ca.pem"),
                oauth_claude_secret="fresh-oauth-token",
            )

    def test_environment_manifest_view_covers_all_keys_without_secret_hash(self) -> None:
        view = environment_manifest_view(
            {
                "PATH": "/usr/bin",
                "CLAUDE_CODE_OAUTH_TOKEN": "CANARY-SECRET",
                "CLAUDE_CODE_REMOTE_SESSION_ID": "probe-session",
            },
            {"oauth_access": "CANARY-SECRET"},
        )
        self.assertEqual(
            view["keys"],
            ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_REMOTE_SESSION_ID", "PATH"],
        )
        self.assertTrue(view["values"]["CLAUDE_CODE_OAUTH_TOKEN"]["redacted"])
        self.assertEqual(
            view["values"]["CLAUDE_CODE_REMOTE_SESSION_ID"], "probe-session"
        )
        self.assertNotIn("CANARY-SECRET", str(view))

    def test_subagent_depth_probe_is_an_explicit_whitelisted_variable(self) -> None:
        self.assertEqual(
            parse_injected_env(["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=3"]),
            {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "3"},
        )


if __name__ == "__main__":
    unittest.main()
