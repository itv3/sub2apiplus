"""OAuth 与 API 运行环境隔离。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

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
    "API_TIMEOUT_MS",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ADDITIONAL_PROTECTION",
    "CLAUDE_CODE_ATTRIBUTION_HEADER",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK",
    "CLAUDE_CODE_DISABLE_THINKING",
    "CLAUDE_CODE_DISPATCH_V2S",
    "CLAUDE_CODE_EXTRA_BODY",
    "CLAUDE_CODE_EXTRA_METADATA",
    "CLAUDE_CODE_GZIP_REQUEST_BODIES",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_MAX_RETRIES",
    # 以下三个由同一个 header 构造对象读取，各自条件展开一个 header：
    #   s = process.env.CLAUDE_CODE_CONTAINER_ID        -> x-claude-remote-container-id
    #   a = process.env.CLAUDE_CODE_REMOTE_SESSION_ID   -> x-claude-remote-session-id
    #   l = process.env.CLAUDE_AGENT_SDK_CLIENT_APP     -> x-client-app
    # 它们只在出站 header 上追加标识，不改变凭据、上游地址、代理或 CA。
    "CLAUDE_AGENT_SDK_CLIENT_APP",
    "CLAUDE_CODE_CONTAINER_ID",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "DISABLE_PROMPT_CACHING",
    "DISABLE_PROMPT_CACHING_SONNET",
    "ENABLE_PROMPT_CACHING_1H",
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


def _read_private_state_file(path: Path, label: str) -> bytes:
    """读取 TUI 所需状态，但不把内容或内容摘要写入证据。"""

    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} 必须是可信绝对普通文件。")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise ConfigurationError(f"{label} 所有者或写权限不安全。")
    value = path.read_bytes()
    if not value:
        raise ConfigurationError(f"{label} 不能为空。")
    return value


def _write_private_state_file(path: Path, value: bytes) -> None:
    """在新建私有目录内以排他方式写入短期状态。"""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _normalized_tui_global_state(
    value: bytes,
    expected_version: str,
) -> tuple[bytes, list[str]]:
    """只在短期副本中补齐官方已完成引导状态。

    TUI 取证复用的是已经由官方 OAuth 流程建立的登录态，而不是重新执行登录。
    因此全局状态必须同时证明存在官方 ``oauthAccount``，并固定为目标版本已经完成
    onboarding。这里不读取、复制或派生账号字段，只返回可公开的字段名清单。
    """

    if not isinstance(expected_version, str) or not expected_version.strip():
        raise ConfigurationError("Claude TUI 目标版本不能为空。")
    try:
        state = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Claude TUI 全局状态必须是合法 JSON。") from error
    if not isinstance(state, dict):
        raise ConfigurationError("Claude TUI 全局状态顶层必须是对象。")
    if not isinstance(state.get("oauthAccount"), dict) or not state["oauthAccount"]:
        raise ConfigurationError("Claude TUI 全局状态缺少官方 oauthAccount。")

    expected = {
        "theme": "dark",
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": expected_version,
    }
    normalized_fields: list[str] = []
    for field, field_value in expected.items():
        if state.get(field) != field_value:
            state[field] = field_value
            normalized_fields.append(field)
    normalized = (
        json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return normalized, normalized_fields


@contextmanager
def temporary_claude_tui_state(
    credentials_file: Path,
    global_state_file: Path,
    *,
    expected_version: str,
    memory_root: Path = Path("/dev/shm"),
) -> Iterator[tuple[Path, Path, dict[str, Any]]]:
    """在内存文件系统中建立一次性官方 TUI 登录态并保证终态删除。

    Claude TUI 不把 ``CLAUDE_CODE_OAUTH_TOKEN`` 当作已有交互登录态。取证时必须
    同时提供官方 ``.credentials.json`` 与 ``.claude.json``，但二者都含私有状态，
    因而只能短暂复制到 ``/dev/shm``，不得进入证据目录。返回的收据只记录布尔事实，
    不记录路径、字节数或任何凭据派生摘要。
    """

    if (
        not memory_root.is_absolute()
        or memory_root.is_symlink()
        or not memory_root.is_dir()
    ):
        raise ConfigurationError("TUI 短期状态根必须是可信绝对目录。")
    credentials = _read_private_state_file(
        credentials_file, "Claude TUI credentials"
    )
    global_state = _read_private_state_file(
        global_state_file, "Claude TUI 全局状态"
    )
    normalized_global_state, normalized_fields = _normalized_tui_global_state(
        global_state,
        expected_version,
    )
    temporary_home = Path(
        tempfile.mkdtemp(prefix="claude-fw-f-tui-", dir=memory_root)
    )
    temporary_home.chmod(0o700)
    resolved_memory_root = memory_root.resolve()
    if temporary_home.is_symlink() or temporary_home.resolve().parent != resolved_memory_root:
        shutil.rmtree(temporary_home, ignore_errors=True)
        raise ConfigurationError("TUI 短期 HOME 没有落在冻结内存目录内。")

    receipt: dict[str, Any] = {
        "required": True,
        "storage_scope": "memory-backed-temporary-home",
        "credentials_copied": False,
        "global_state_copied": False,
        "privacy_settings_written": False,
        "onboarding_state_normalized": False,
        "onboarding_normalized_fields": [],
        "source_global_state_modified": False,
        "archived_in_evidence": False,
        "removed": False,
    }
    try:
        config_dir = temporary_home / ".claude"
        config_dir.mkdir(mode=0o700)
        _write_private_state_file(config_dir / ".credentials.json", credentials)
        receipt["credentials_copied"] = True
        _write_private_state_file(
            temporary_home / ".claude.json", normalized_global_state
        )
        receipt["global_state_copied"] = True
        receipt["onboarding_state_normalized"] = True
        receipt["onboarding_normalized_fields"] = normalized_fields
        settings = {
            "skipWebFetchPreflight": True,
            "env": PRIVACY_ENV,
        }
        secure_write_text(
            config_dir / "settings.json",
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        )
        receipt["privacy_settings_written"] = True
        yield temporary_home, config_dir, receipt
    finally:
        shutil.rmtree(temporary_home)
        receipt["removed"] = not temporary_home.exists()
        if not receipt["removed"]:
            raise ConfigurationError("Claude TUI 短期登录态未能彻底删除。")
        current_global_state = _read_private_state_file(
            global_state_file, "Claude TUI 全局状态"
        )
        receipt["source_global_state_modified"] = current_global_state != global_state
        if receipt["source_global_state_modified"]:
            raise ConfigurationError("Claude TUI 长期全局状态在取证期间被修改。")


@contextmanager
def temporary_claude_refresh_state(
    credentials_file: Path,
    global_state_file: Path,
    *,
    memory_root: Path = Path("/dev/shm"),
) -> Iterator[tuple[Path, Path, dict[str, Any]]]:
    """在 tmpfs 复制凭据并仅把副本标成过期，以触发官方 refresh 请求。

    该状态只用于隔离合成中继：refresh 请求不会转发到真实 OAuth 端点，长期
    credentials 也不会被客户端写入。收据不记录令牌、字节数或秘密派生摘要。
    """

    if (
        not memory_root.is_absolute()
        or memory_root.is_symlink()
        or not memory_root.is_dir()
    ):
        raise ConfigurationError("OAuth refresh 短期状态根必须是可信绝对目录。")
    credentials_bytes = _read_private_state_file(
        credentials_file, "Claude OAuth refresh credentials"
    )
    global_state = _read_private_state_file(
        global_state_file, "Claude OAuth refresh 全局状态"
    )
    try:
        document = json.loads(credentials_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Claude OAuth refresh credentials 不是合法 JSON。") from error
    oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        raise ConfigurationError("Claude OAuth refresh credentials 缺少 refreshToken。")
    oauth["expiresAt"] = 1
    expired_credentials = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary_home = Path(
        tempfile.mkdtemp(prefix="claude-fw-f-refresh-", dir=memory_root)
    )
    temporary_home.chmod(0o700)
    if temporary_home.is_symlink() or temporary_home.resolve().parent != memory_root.resolve():
        shutil.rmtree(temporary_home, ignore_errors=True)
        raise ConfigurationError("OAuth refresh 短期 HOME 没有落在冻结内存目录内。")
    receipt: dict[str, Any] = {
        "required": True,
        "storage_scope": "memory-backed-expired-credential-copy",
        "credentials_copied": False,
        "global_state_copied": False,
        "privacy_settings_written": False,
        "expiry_forced_on_copy": False,
        "production_oauth_forwarding_enabled": False,
        "archived_in_evidence": False,
        "removed": False,
    }
    try:
        config_dir = temporary_home / ".claude"
        config_dir.mkdir(mode=0o700)
        _write_private_state_file(config_dir / ".credentials.json", expired_credentials)
        receipt["credentials_copied"] = True
        receipt["expiry_forced_on_copy"] = True
        _write_private_state_file(temporary_home / ".claude.json", global_state)
        receipt["global_state_copied"] = True
        settings = {
            "skipWebFetchPreflight": True,
            "env": PRIVACY_ENV,
        }
        secure_write_text(
            config_dir / "settings.json",
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        )
        receipt["privacy_settings_written"] = True
        yield temporary_home, config_dir, receipt
    finally:
        shutil.rmtree(temporary_home)
        receipt["removed"] = not temporary_home.exists()
        if not receipt["removed"]:
            raise ConfigurationError("OAuth refresh 短期登录态未能彻底删除。")


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
