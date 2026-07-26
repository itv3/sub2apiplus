"""OAuth 与 API 运行环境隔离。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .model import CaptureCase, ConfigurationError
from .security import ensure_private_directory, secure_write_text


PROXY_KEYS = {
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
CA_KEYS = {
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
}
AUTH_AND_TARGET_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "AWS_ACCESS_KEY_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_API_TYPE",
    "OPENAI_ACCESS_TOKEN",
    "OPENAI_BASE_URL",
    "CHATGPT_ACCESS_TOKEN",
    "SUB2API_API_KEY",
    "SUB2API_CAPTURE_API_KEY",
}
PRIVACY_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    "DISABLE_TELEMETRY": "1",
}
BASE_ENVIRONMENT_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TZ",
    "USER",
}


def clean_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """从最小白名单重建环境，拒绝未知变量改变客户端或 TLS 画像。"""

    original = source if source is not None else os.environ
    return {
        key: value
        for key, value in original.items()
        if key in BASE_ENVIRONMENT_KEYS
    }


def prepare_api_state(run_dir: Path) -> tuple[Path, Path]:
    """创建不含 OAuth 凭据的 Claude/Codex API 专用状态。"""

    state_root = ensure_private_directory(run_dir / "state", run_dir)
    claude_home = ensure_private_directory(state_root / "claude", run_dir)
    codex_home = ensure_private_directory(state_root / "codex", run_dir)

    claude_settings = {
        "skipWebFetchPreflight": True,
        "env": PRIVACY_ENV,
    }
    secure_write_text(
        claude_home / "settings.json",
        json.dumps(claude_settings, ensure_ascii=False, indent=2) + "\n",
    )
    secure_write_text(
        codex_home / "config.toml",
        "\n".join(
            (
                "check_for_update_on_startup = false",
                "",
                "[analytics]",
                "enabled = false",
                "",
                "[feedback]",
                "enabled = false",
                "",
                "[otel]",
                'exporter = "none"',
                "log_user_prompt = false",
                "",
            )
        ),
    )
    return claude_home, codex_home


def build_case_environment(
    *,
    case: CaptureCase,
    source: Mapping[str, str],
    api_secret: str | None,
    api_key_env: str,
    claude_api_home: Path | None,
    codex_api_home: Path | None,
    proxy_url: str,
    ca_bundle: Path,
) -> dict[str, str]:
    """构造单一 case 的子进程环境，不修改当前进程环境。"""

    environment = clean_environment(source)
    # 自定义 Key 变量名也必须先删除，避免 all 模式把 API 凭据带入 OAuth 子进程。
    environment.pop(api_key_env, None)
    environment.update(PRIVACY_ENV)

    if case.task == "api":
        if not api_secret:
            raise ConfigurationError(f"API 任务缺少环境变量 {api_key_env}。")
        if case.product == "claude":
            if not claude_api_home:
                raise ConfigurationError("Claude API 状态目录尚未初始化。")
            environment["ANTHROPIC_API_KEY"] = api_secret
            environment["ANTHROPIC_BASE_URL"] = str(case.base_url)
            environment["CLAUDE_CONFIG_DIR"] = str(claude_api_home)
            environment["HOME"] = str(claude_api_home)
        else:
            if not codex_api_home:
                raise ConfigurationError("Codex API 状态目录尚未初始化。")
            environment[api_key_env] = api_secret
            environment["CODEX_HOME"] = str(codex_api_home)
            environment["HOME"] = str(codex_api_home)
    else:
        # OAuth 必须使用容器内已隔离的默认授权状态，禁止继承 API 专用目录。
        environment.pop("CLAUDE_CONFIG_DIR", None)
        environment.pop("CODEX_HOME", None)

    if case.evidence == "mitm":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment[key] = proxy_url
        if case.product == "claude":
            environment["NODE_EXTRA_CA_CERTS"] = str(ca_bundle)
        else:
            environment["SSL_CERT_FILE"] = str(ca_bundle)
    return environment
