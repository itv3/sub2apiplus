"""OAuth 与 API 运行环境隔离。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .model import CaptureCase, ConfigurationError
from .security import (
    SENSITIVE_HEADER_RE,
    canonical_json_sha256,
    ensure_private_directory,
    secure_write_text,
)


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


# 允许在差分场景中显式注入的探针变量。这些键都是官方客户端自身读取的条件开关，
# 用于给 docs/Claude_code_21220_EGRESS_SPEC.md 第 2.2 节的条件候选取正负例。
#
# 这里刻意维持一份独立白名单，而不是放开 clean_environment：默认拒绝未知变量是为了
# 防止环境污染改变客户端或 TLS 画像，一旦允许任意注入，任何一次采集都无法再自证
# 「出站形态只由画像决定」。凡是能改变凭据、上游地址、代理或 CA 的键都不在此列，
# 它们会改变的是被测对象本身而不是被测条件。
INJECTABLE_PROBE_KEYS = {
    "ANTHROPIC_BETAS",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_ADDITIONAL_PROTECTION",
    "CLAUDE_CODE_DISPATCH_V2S",
    # 以下三个由同一个 header 构造对象读取，各自条件展开一个 header：
    #   s = process.env.CLAUDE_CODE_CONTAINER_ID        -> x-claude-remote-container-id
    #   a = process.env.CLAUDE_CODE_REMOTE_SESSION_ID   -> x-claude-remote-session-id
    #   l = process.env.CLAUDE_AGENT_SDK_CLIENT_APP     -> x-client-app
    # 它们只在出站 header 上追加标识，不改变凭据、上游地址、代理或 CA。
    "CLAUDE_AGENT_SDK_CLIENT_APP",
    "CLAUDE_CODE_CONTAINER_ID",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
}


def _safe_custom_headers(value: str) -> str:
    """保留非敏感探针 Header，并遮蔽认证类 Header 的值。"""

    lines: list[str] = []
    for line in value.splitlines():
        name, separator, item_value = line.partition(":")
        if separator and SENSITIVE_HEADER_RE.search(name):
            lines.append(f"{name}: <redacted-sensitive-header>")
        else:
            lines.append(line)
    return "\n".join(lines)


def environment_manifest_view(
    environment: Mapping[str, str],
    known_secrets: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """归档实际子进程环境的完整脱敏视图。"""

    secrets = {
        source: value
        for source, value in (known_secrets or {}).items()
        if value
    }
    sensitive_name = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "CREDENTIAL",
        "AUTHORIZATION",
        "COOKIE",
    )
    values: dict[str, Any] = {}
    redacted_keys: list[str] = []
    for key in sorted(environment):
        value = str(environment[key])
        secret_sources = sorted(
            source for source, secret in secrets.items() if secret in value
        )
        if secret_sources or any(marker in key.upper() for marker in sensitive_name):
            values[key] = {
                "present": True,
                "redacted": True,
                "reason": "credential",
                "secret_sources": secret_sources,
            }
            redacted_keys.append(key)
        elif key == "ANTHROPIC_CUSTOM_HEADERS":
            values[key] = _safe_custom_headers(value)
        else:
            values[key] = value
    return {
        "schema_version": "official-client-environment/v1",
        "values": values,
        "keys": sorted(values),
        "redacted_keys": redacted_keys,
        "sha256": canonical_json_sha256(values),
    }


def parse_injected_env(pairs: list[str] | None) -> dict[str, str]:
    """解析 `KEY=VALUE` 形式的探针变量，仅接受白名单内的键。"""

    injected: dict[str, str] = {}
    for item in pairs or ():
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigurationError(f"--inject-env 需要 KEY=VALUE 形式：{item!r}")
        if key not in INJECTABLE_PROBE_KEYS:
            allowed = "、".join(sorted(INJECTABLE_PROBE_KEYS))
            raise ConfigurationError(
                f"{key} 不在允许注入的探针变量内。允许的键：{allowed}")
        if key in injected:
            raise ConfigurationError(f"--inject-env 重复声明了 {key}。")
        injected[key] = value
    return injected


def clean_environment(
    source: Mapping[str, str] | None = None,
    injected: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """从最小白名单重建环境，拒绝未知变量改变客户端或 TLS 画像。

    `injected` 只接受 `INJECTABLE_PROBE_KEYS` 内的探针变量，由调用方先经
    `parse_injected_env` 校验；这里再兜一次底，避免绕过参数解析直接传入。
    """

    original = source if source is not None else os.environ
    environment = {
        key: value
        for key, value in original.items()
        if key in BASE_ENVIRONMENT_KEYS
    }
    for key, value in (injected or {}).items():
        if key not in INJECTABLE_PROBE_KEYS:
            raise ConfigurationError(f"{key} 不是允许注入的探针变量。")
        environment[key] = value
    return environment


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


def prepare_claude_oauth_state(run_dir: Path) -> Path:
    """为运行时 OAuth token 建立不含长期凭据的逐轮 Claude 私有 HOME。"""

    state_root = ensure_private_directory(run_dir / "state", run_dir)
    claude_home = ensure_private_directory(state_root / "claude-oauth", run_dir)
    settings = {
        "skipWebFetchPreflight": True,
        "env": PRIVACY_ENV,
    }
    secure_write_text(
        claude_home / "settings.json",
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
    )
    return claude_home


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
    oauth_claude_secret: str | None = None,
    injected_env: Mapping[str, str] | None = None,
    claude_oauth_home: Path | None = None,
) -> dict[str, str]:
    """构造单一 case 的子进程环境，不修改当前进程环境。"""

    environment = clean_environment(source, injected_env)
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
        if case.product == "claude" and oauth_claude_secret:
            # 显式覆盖只进入当前 Claude 子进程；编排器会对完整产物执行精确值扫描。
            environment["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_claude_secret
            if not claude_oauth_home:
                raise ConfigurationError("运行时 Claude OAuth token 必须使用逐轮私有 HOME。")
            environment["CLAUDE_CONFIG_DIR"] = str(claude_oauth_home)
            environment["HOME"] = str(claude_oauth_home)

    if case.evidence == "mitm":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment[key] = proxy_url
        if case.product == "claude":
            environment["NODE_EXTRA_CA_CERTS"] = str(ca_bundle)
        else:
            environment["SSL_CERT_FILE"] = str(ca_bundle)
    return environment
