"""Claude Code 2.1.226 完整 FW-E/F v4 场景目录与官方客户端驱动。

v4 是对历史 v2/v3 的追加式补充。它重放 v3 已有场景，并补齐完整真实场景矩阵、
必要辅助出站和 TLS P 通道。目录只接受源码内冻结的场景，不接受调用方注入任意
prompt、argv、环境变量或响应计划。
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.drive_claude_tui import (
    drive as drive_tui,
    drive_sequence as drive_tui_sequence,
)

from .claude_fw_f_v3 import (
    PROBES as V3_PROBES,
    run_claude_fw_f_probe as run_v3_probe,
)
from .environment import INJECTABLE_PROBE_KEYS, environment_manifest_view
from .lifecycle import _popen_safety_options
from .scenarios import run_claude_scenario
from .security import (
    argv_manifest_view,
    ensure_private_directory,
    file_sha256,
    redact_known_secret,
    secure_write_json,
    secure_write_text,
)


SCHEMA_CATALOG = "claude-code-fw-f-v4-scenario-catalog/v1"
SCHEMA_RESULT = "claude-code-fw-f-v4-probe-result/v1"
SCHEMA_DIMENSION_EVIDENCE = "claude-code-fw-f-v4-dimension-evidence/v1"
DEFAULT_MARKER = "FW_F_V4_OK"
ALLOWED_TARGET_HOSTS = {"api.anthropic.com", "platform.claude.com"}
BACKGROUND_ID_RE = re.compile(r"backgrounded\s*[·:]\s*(?P<id>[0-9a-f]{8,16})", re.I)
BACKGROUND_TERMINAL_STATES = frozenset(
    {"completed", "done", "failed", "stopped", "killed"}
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
MCP_SERVER = FIXTURE_ROOT / "claude_fw_f_v4_mcp_server.py"
HOOK_SCRIPT = FIXTURE_ROOT / "claude_fw_f_v4_hook.py"


REQUIRED_MATRIX_DIMENSIONS = frozenset(
    {
        "transport.native_tls",
        "transport.alpn",
        "transport.http",
        "transport.connection_reuse",
        "entrypoint.tui",
        "entrypoint.sdk_cli",
        "entrypoint.agent_sdk",
        "entrypoint.workload",
        "agency.foreground",
        "agency.background",
        "agency.subagent",
        "agency.nested_subagent",
        "agency.fork",
        "agency.hook",
        "agency.compact",
        "agency.remote",
        "header.custom",
        "header.beta",
        "header.metadata",
        "header.usage_limit",
        "header.dispatch_id",
        "body.system",
        "body.attachment",
        "body.context",
        "body.cache_breakpoint",
        "tool.bash",
        "tool.agent",
        "tool.mcp",
        "tool.web_search",
        "tool.advisor",
        "tool.deferred",
        "tool.extended",
        "failure.401",
        "failure.403",
        "failure.408",
        "failure.409",
        "failure.429",
        "failure.5xx",
        "failure.529",
        "failure.retry_after",
        "failure.disconnect",
        "failure.fallback",
        "failure.non_stream",
        "aux.count_tokens",
        "aux.oauth_refresh",
        "aux.usage",
        "aux.models",
        "privacy.telemetry_disabled",
        "privacy.nonessential_disabled",
    }
)


@dataclasses.dataclass(frozen=True)
class ClaudeFWFCompleteProbe:
    """一个参数、环境、入口和取证通道均冻结的 v4 官方客户端场景。"""

    probe_id: str
    driver: str
    dimensions: tuple[str, ...]
    prompt: str = f"只回复 {DEFAULT_MARKER}，不得调用任何工具。"
    tui_inputs: tuple[str, ...] = ()
    marker: str | None = DEFAULT_MARKER
    source_v3_probe: str | None = None
    legacy_scenario: str | None = None
    cli_args: tuple[str, ...] = ()
    injected_env: tuple[tuple[str, str], ...] = ()
    tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    fixture: str | None = None
    response_plan: str | None = None
    expected_outcome: str = "success"
    expected_tool_name: str | None = None
    message_request_expectation: str = "at-least-one"
    target_host: str = "api.anthropic.com"
    require_pcap: bool = False
    require_debug_log: bool = False
    required_requests: tuple[str, ...] = ()
    forbidden_requests: tuple[str, ...] = ()
    required_message_tools: tuple[str, ...] = ()
    forbidden_message_tools: tuple[str, ...] = ()
    runtime_conclusion: str | None = None
    requires_account_state: bool = False
    safe_mode: bool = True
    wait_seconds: int = 20

    def env_dict(self) -> dict[str, str]:
        return dict(self.injected_env)


def _probe(
    probe_id: str,
    *,
    driver: str = "print",
    dimensions: tuple[str, ...],
    prompt: str | None = None,
    tui_inputs: tuple[str, ...] = (),
    marker: str | None = DEFAULT_MARKER,
    source_v3_probe: str | None = None,
    legacy_scenario: str | None = None,
    cli_args: tuple[str, ...] = (),
    injected_env: Mapping[str, str] | None = None,
    tools: tuple[str, ...] = (),
    allowed_tools: tuple[str, ...] = (),
    fixture: str | None = None,
    response_plan: str | None = None,
    expected_outcome: str = "success",
    expected_tool_name: str | None = None,
    message_request_expectation: str = "at-least-one",
    target_host: str = "api.anthropic.com",
    require_pcap: bool = False,
    require_debug_log: bool = False,
    required_requests: tuple[str, ...] = (),
    forbidden_requests: tuple[str, ...] = (),
    required_message_tools: tuple[str, ...] = (),
    forbidden_message_tools: tuple[str, ...] = (),
    runtime_conclusion: str | None = None,
    requires_account_state: bool = False,
    safe_mode: bool = True,
    wait_seconds: int = 20,
) -> ClaudeFWFCompleteProbe:
    return ClaudeFWFCompleteProbe(
        probe_id=probe_id,
        driver=driver,
        dimensions=tuple(sorted(set(dimensions))),
        prompt=prompt or f"只回复 {marker or DEFAULT_MARKER}，不得调用任何工具。",
        tui_inputs=tui_inputs,
        marker=marker,
        source_v3_probe=source_v3_probe,
        legacy_scenario=legacy_scenario,
        cli_args=cli_args,
        injected_env=tuple(sorted((injected_env or {}).items())),
        tools=tools,
        allowed_tools=allowed_tools,
        fixture=fixture,
        response_plan=response_plan,
        expected_outcome=expected_outcome,
        expected_tool_name=expected_tool_name,
        message_request_expectation=message_request_expectation,
        target_host=target_host,
        require_pcap=require_pcap,
        require_debug_log=require_debug_log,
        required_requests=required_requests,
        forbidden_requests=forbidden_requests,
        required_message_tools=required_message_tools,
        forbidden_message_tools=forbidden_message_tools,
        runtime_conclusion=runtime_conclusion,
        requires_account_state=requires_account_state,
        safe_mode=safe_mode,
        wait_seconds=wait_seconds,
    )


def _inherited_v3_dimensions(probe_id: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "v3-baseline": ("entrypoint.sdk_cli", "agency.foreground", "transport.http"),
        "v3-tui": ("entrypoint.tui",),
        "v3-agent-sdk": ("entrypoint.agent_sdk",),
        "v3-workload": ("entrypoint.workload",),
        "v3-remote-container": ("agency.remote",),
        "v3-remote-session": ("agency.remote",),
        "v3-header-combination": ("entrypoint.agent_sdk", "entrypoint.workload", "agency.remote"),
        "v3-custom-header-grammar": ("header.custom",),
        "v3-custom-header-invalid-name": ("header.custom",),
        "v3-extra-metadata": ("header.metadata",),
        "v3-beta-deduplicate": ("header.beta",),
        "v3-custom-system": ("body.system",),
        "v3-append-system": ("body.system",),
        "v3-exclude-dynamic-system": ("body.system", "body.context"),
        "v3-cache-disabled": ("body.cache_breakpoint",),
        "v3-cache-sonnet-disabled": ("body.cache_breakpoint",),
        "v3-cache-one-hour": ("body.cache_breakpoint",),
        "v3-session-fork": ("agency.fork",),
        "v3-session-resume": (),
        "v3-retry-401": ("failure.401",),
        "v3-nonretry-403": ("failure.403",),
        "v3-retry-408": ("failure.408",),
        "v3-retry-409": ("failure.409",),
        "v3-retry-429": ("failure.429",),
        "v3-retry-500": ("failure.5xx",),
        "v3-retry-502": ("failure.5xx",),
        "v3-retry-503": ("failure.5xx",),
        "v3-retry-529": ("failure.529",),
        "v3-retry-after-seconds": ("failure.retry_after",),
        "v3-retry-after-date": ("failure.retry_after",),
        "v3-disconnect-retry": ("failure.disconnect",),
        "v3-fallback-model": ("failure.fallback",),
        "v3-stream-404-fallback": ("failure.non_stream",),
        "v3-stream-404-disable-flag": ("failure.non_stream",),
        "v3-stream-interrupt": ("failure.non_stream", "failure.disconnect"),
        "v3-stream-interrupt-no-fallback": ("failure.non_stream", "failure.disconnect"),
    }
    return mapping.get(probe_id, ())


PROBES: dict[str, ClaudeFWFCompleteProbe] = {}

# v3 所有场景均在全新 v4 目录重新运行；旧目录只作为历史证据，不原位续写。
for _v3_id, _v3_probe in sorted(V3_PROBES.items()):
    _v4_id = "v4-replay-" + _v3_id.removeprefix("v3-")
    PROBES[_v4_id] = _probe(
        _v4_id,
        driver="v3",
        source_v3_probe=_v3_id,
        dimensions=_inherited_v3_dimensions(_v3_id),
        response_plan=_v3_probe.response_plan,
        expected_outcome=_v3_probe.expected_outcome,
        message_request_expectation=_v3_probe.message_request_expectation,
        injected_env=_v3_probe.env_dict(),
        safe_mode=_v3_probe.safe_mode,
    )

PROBES.update(
    {
        "v4-native-tls-baseline": _probe(
            "v4-native-tls-baseline",
            driver="legacy",
            legacy_scenario="s1",
            dimensions=(
                "transport.native_tls",
                "transport.alpn",
                "transport.http",
                "entrypoint.sdk_cli",
                "privacy.telemetry_disabled",
                "privacy.nonessential_disabled",
            ),
            require_pcap=True,
        ),
        "v4-connection-reuse": _probe(
            "v4-connection-reuse",
            driver="legacy",
            legacy_scenario="s2",
            dimensions=("transport.connection_reuse", "transport.http"),
            require_pcap=True,
        ),
        "v4-bash": _probe(
            "v4-bash",
            driver="legacy",
            legacy_scenario="s4",
            dimensions=("tool.bash",),
        ),
        "v4-agent-depth1": _probe(
            "v4-agent-depth1",
            driver="legacy",
            legacy_scenario="a1",
            dimensions=("agency.subagent", "tool.agent"),
        ),
        "v4-agent-depth2": _probe(
            "v4-agent-depth2",
            driver="legacy",
            legacy_scenario="a2",
            dimensions=("agency.nested_subagent", "tool.agent"),
        ),
        "v4-agent-depth3": _probe(
            "v4-agent-depth3",
            driver="legacy",
            legacy_scenario="a3",
            dimensions=("agency.nested_subagent", "tool.agent"),
        ),
        "v4-background": _probe(
            "v4-background",
            driver="background",
            dimensions=("agency.background",),
            cli_args=("--bg",),
            marker=DEFAULT_MARKER,
            wait_seconds=90,
        ),
        "v4-background-subagent": _probe(
            "v4-background-subagent",
            dimensions=("agency.background", "agency.subagent", "tool.agent"),
            prompt=(
                "必须且仅调用一次 Agent 工具，description 为 fw-f-v4-bg，"
                "subagent_type 为 general-purpose，run_in_background 为 true，"
                "prompt 为‘只回复 FW_F_V4_BG_CHILD_OK’；之后只回复 FW_F_V4_OK。"
            ),
            tools=("Task",),
            allowed_tools=("Task",),
            expected_tool_name="Agent",
            safe_mode=True,
        ),
        "v4-agent-usage-limit": _probe(
            "v4-agent-usage-limit",
            dimensions=("agency.subagent", "tool.agent", "header.usage_limit"),
            prompt=(
                "必须且仅调用一次 Agent 工具，description 为 fw-f-v4-usage-limit，"
                "subagent_type 为 general-purpose，prompt 为‘只回复 FW_F_V4_CHILD_OK’；"
                "读取结果后只回复 FW_F_V4_OK。"
            ),
            tools=("Task",),
            allowed_tools=("Task",),
            expected_tool_name="Agent",
            requires_account_state=True,
            runtime_conclusion="usage_limit_rollout_not_observed_for_measured_account",
        ),
        "v4-hook": _probe(
            "v4-hook",
            dimensions=("agency.hook", "tool.bash", "body.context"),
            prompt=(
                "必须且仅调用一次 Bash 工具执行 `printf FW_F_V4_HOOK_TOOL_OK`；"
                "读取结果后只回复 FW_F_V4_OK。"
            ),
            tools=("Bash",),
            allowed_tools=("Bash(printf FW_F_V4_HOOK_TOOL_OK)",),
            fixture="hook",
            expected_tool_name="Bash",
            safe_mode=False,
        ),
        "v4-context-claude-md": _probe(
            "v4-context-claude-md",
            dimensions=("body.context", "body.system"),
            prompt="根据当前项目 CLAUDE.md，只回复其中要求的固定标记。",
            fixture="context",
            safe_mode=False,
        ),
        "v4-tui-attachment": _probe(
            "v4-tui-attachment",
            driver="tui",
            dimensions=("entrypoint.tui", "body.attachment", "body.context"),
            prompt="@fw_f_v4_attachment.txt 只回复文件中的固定标记。",
            marker=None,
            fixture="attachment",
            safe_mode=False,
        ),
        "v4-tui-compact": _probe(
            "v4-tui-compact",
            driver="tui",
            dimensions=("entrypoint.tui", "agency.compact", "body.cache_breakpoint"),
            prompt="/compact",
            tui_inputs=(
                "只回复 FW_F_V4_COMPACT_SEED_OK，不得调用任何工具。",
                "/compact",
            ),
            marker=None,
            safe_mode=False,
            wait_seconds=30,
        ),
        "v4-tui-count-tokens": _probe(
            "v4-tui-count-tokens",
            driver="tui",
            dimensions=("entrypoint.tui", "aux.count_tokens"),
            prompt="/context",
            marker=None,
            safe_mode=False,
            wait_seconds=30,
            message_request_expectation="zero",
            required_requests=("POST /v1/messages/count_tokens?beta=true",),
        ),
        "v4-tui-usage": _probe(
            "v4-tui-usage",
            driver="tui",
            dimensions=("entrypoint.tui", "aux.usage"),
            prompt="/usage",
            marker=None,
            safe_mode=False,
            wait_seconds=30,
            message_request_expectation="zero",
            require_debug_log=True,
            forbidden_requests=("GET /api/oauth/usage",),
            runtime_conclusion="usage_blocked_by_essential_traffic_only",
        ),
        "v4-mcp-tool": _probe(
            "v4-mcp-tool",
            dimensions=("tool.mcp",),
            prompt=(
                "必须且仅调用一次 claude-fw-f-v4 MCP 的 probe_echo 工具，"
                "value 为 exact；读取结果后只回复 FW_F_V4_OK。"
            ),
            fixture="mcp",
            expected_tool_name="mcp__claude-fw-f-v4__probe_echo",
            safe_mode=False,
        ),
        "v4-mcp-deferred": _probe(
            "v4-mcp-deferred",
            dimensions=("tool.mcp", "tool.deferred", "tool.extended"),
            prompt=(
                "在 claude-fw-f-v4 MCP 目录中查找并调用 deferred_probe_32，"
                "query 为 exact；读取结果后只回复 FW_F_V4_OK。"
            ),
            fixture="mcp",
            marker=DEFAULT_MARKER,
            safe_mode=False,
        ),
        "v4-web-search": _probe(
            "v4-web-search",
            dimensions=("tool.web_search", "tool.extended"),
            prompt=(
                "必须调用一次 WebSearch 搜索 Anthropic 官方站点中的 Claude Code，"
                "完成后只回复 FW_F_V4_OK。"
            ),
            tools=("WebSearch",),
            allowed_tools=("WebSearch",),
            expected_tool_name="WebSearch",
        ),
        "v4-advisor-default-negative": _probe(
            "v4-advisor-default-negative",
            dimensions=("tool.advisor", "tool.extended"),
            prompt=(
                "检查当前可用工具；不得调用任何工具，只回复 FW_F_V4_OK。"
            ),
            tools=("default",),
            marker=DEFAULT_MARKER,
            forbidden_message_tools=("advisor",),
            runtime_conclusion="advisor_default_disabled_negative",
        ),
        "v4-advisor-enabled-positive": _probe(
            "v4-advisor-enabled-positive",
            dimensions=("tool.advisor", "tool.extended"),
            prompt=(
                "必须调用一次 advisor 工具取得最小建议；收到建议后只回复 FW_F_V4_OK。"
            ),
            injected_env={"CLAUDE_CODE_ENABLE_EXPERIMENTAL_ADVISOR_TOOL": "1"},
            tools=("default",),
            fixture="advisor",
            marker=DEFAULT_MARKER,
            require_debug_log=True,
            required_message_tools=("advisor",),
            runtime_conclusion="advisor_explicitly_enabled_positive",
        ),
        "v4-dispatch-account-negative": _probe(
            "v4-dispatch-account-negative",
            dimensions=("header.dispatch_id",),
            requires_account_state=True,
            runtime_conclusion="dispatch_rollout_not_observed_for_measured_account",
        ),
        "v4-models-privacy-state": _probe(
            "v4-models-privacy-state",
            dimensions=(
                "aux.models",
                "privacy.telemetry_disabled",
                "privacy.nonessential_disabled",
            ),
            wait_seconds=30,
            forbidden_requests=("GET /v1/models", "GET /v1/models?beta=true"),
            runtime_conclusion="model_capabilities_hard_disabled_in_2_1_226",
        ),
        "v4-oauth-refresh": _probe(
            "v4-oauth-refresh",
            driver="oauth-refresh",
            dimensions=("aux.oauth_refresh",),
            marker=None,
            target_host="platform.claude.com",
            response_plan="oauth-refresh-reject",
            expected_outcome="failure",
            message_request_expectation="zero",
            safe_mode=False,
        ),
    }
)

PROBE_IDS = tuple(sorted(PROBES))
SYNTHETIC_RESPONSE_PLANS = tuple(
    sorted({probe.response_plan for probe in PROBES.values() if probe.response_plan})
)


class ClaudeFWFCompleteProbeError(RuntimeError):
    """表示 v4 场景定义、执行或结果没有满足冻结合同。"""


def get_probe(probe_id: str) -> ClaudeFWFCompleteProbe:
    try:
        return PROBES[probe_id]
    except KeyError as error:
        raise ClaudeFWFCompleteProbeError(f"未知 FW-F v4 probe：{probe_id}") from error


def validate_probe_catalog() -> None:
    """验证完整矩阵、驱动闭集和每个场景的安全边界。"""

    allowed_drivers = {"v3", "legacy", "print", "tui", "background", "oauth-refresh"}
    allowed_fixtures = {None, "hook", "context", "attachment", "mcp", "advisor"}
    covered: set[str] = set()
    for probe_id, probe in PROBES.items():
        if probe.probe_id != probe_id:
            raise ClaudeFWFCompleteProbeError(f"probe 字典键与身份不一致：{probe_id}")
        if probe.driver not in allowed_drivers:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} driver 非法。")
        if probe.fixture not in allowed_fixtures:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} fixture 非法。")
        if probe.target_host not in ALLOWED_TARGET_HOSTS:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 目标 host 非法。")
        if probe.expected_outcome not in {"success", "failure"}:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 预期终态非法。")
        if probe.message_request_expectation not in {"at-least-one", "zero"}:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} messages 分母非法。")
        if set(probe.required_requests) & set(probe.forbidden_requests):
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 请求正负断言冲突。")
        if set(probe.required_message_tools) & set(probe.forbidden_message_tools):
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 工具正负断言冲突。")
        if len(probe.required_requests) != len(set(probe.required_requests)):
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 必需请求断言重复。")
        if len(probe.forbidden_requests) != len(set(probe.forbidden_requests)):
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 禁止请求断言重复。")
        unknown = set(probe.env_dict()) - INJECTABLE_PROBE_KEYS
        if unknown:
            raise ClaudeFWFCompleteProbeError(
                f"{probe_id} 使用未批准环境变量：{sorted(unknown)}"
            )
        if probe.driver == "v3" and probe.source_v3_probe not in V3_PROBES:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} 未绑定有效 v3 场景。")
        if probe.driver == "legacy" and probe.legacy_scenario not in {"s1", "s2", "s4", "a1", "a2", "a3"}:
            raise ClaudeFWFCompleteProbeError(f"{probe_id} legacy 场景非法。")
        if probe.driver == "oauth-refresh" and probe.target_host != "platform.claude.com":
            raise ClaudeFWFCompleteProbeError("OAuth refresh 必须隔离到 platform.claude.com。")
        if probe.requires_account_state and probe.driver == "oauth-refresh":
            raise ClaudeFWFCompleteProbeError("OAuth refresh 使用独立过期凭据状态。")
        covered.update(probe.dimensions)
    missing = REQUIRED_MATRIX_DIMENSIONS - covered
    if missing:
        raise ClaudeFWFCompleteProbeError(f"完整真实场景矩阵缺失：{sorted(missing)}")


def catalog_document() -> dict[str, Any]:
    validate_probe_catalog()
    dimensions = {
        dimension: sorted(
            probe_id for probe_id, probe in PROBES.items() if dimension in probe.dimensions
        )
        for dimension in sorted(REQUIRED_MATRIX_DIMENSIONS)
    }
    return {
        "schema_version": SCHEMA_CATALOG,
        "target_version": "2.1.226",
        "probe_count": len(PROBES),
        "required_matrix_dimension_count": len(REQUIRED_MATRIX_DIMENSIONS),
        "required_matrix_dimensions": dimensions,
        "privacy_configuration": {
            "telemetry_disabled": True,
            "nonessential_traffic_disabled": True,
            "absence_generates_rule": False,
        },
        "probes": [dataclasses.asdict(PROBES[probe_id]) for probe_id in PROBE_IDS],
    }


def _fixture_paths(output_dir: Path, probe: ClaudeFWFCompleteProbe) -> tuple[Path, list[str]]:
    workspace = output_dir / "workspace"
    workspace.mkdir(mode=0o700)
    args: list[str] = []
    if probe.fixture == "context":
        secure_write_text(
            workspace / "CLAUDE.md",
            "所有回答只允许是 FW_F_V4_OK，不得添加其他文本。\n",
        )
    elif probe.fixture == "attachment":
        secure_write_text(
            workspace / "fw_f_v4_attachment.txt",
            "附件固定标记：FW_F_V4_OK\n",
        )
    elif probe.fixture == "hook":
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{sys.executable} {HOOK_SCRIPT}",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        settings_path = output_dir / "settings.json"
        secure_write_json(settings_path, settings)
        args.extend(("--settings", str(settings_path), "--include-hook-events"))
    elif probe.fixture == "mcp":
        config = {
            "mcpServers": {
                "claude-fw-f-v4": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(MCP_SERVER)],
                }
            }
        }
        config_path = output_dir / "mcp.json"
        secure_write_json(config_path, config)
        args.extend(("--mcp-config", str(config_path), "--strict-mcp-config"))
    elif probe.fixture == "advisor":
        settings_path = output_dir / "advisor-settings.json"
        secure_write_json(settings_path, {"advisorModel": "fable"})
        args.extend(("--settings", str(settings_path)))
    return workspace, args


def _result_records(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _nested_tool_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
            names.append(value["name"])
        for child in value.values():
            names.extend(_nested_tool_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_nested_tool_names(child))
    return names


def _new_debug_path(probe: ClaudeFWFCompleteProbe) -> Path | None:
    if not probe.require_debug_log:
        return None
    memory_root = Path("/dev/shm")
    if memory_root.is_symlink() or not memory_root.is_dir():
        raise ClaudeFWFCompleteProbeError("受管 debug 原始目录不是可信 tmpfs。")
    descriptor, value = tempfile.mkstemp(
        prefix=f"claude-fw-f-{probe.probe_id}-",
        suffix=".log",
        dir=memory_root,
    )
    os.close(descriptor)
    path = Path(value)
    path.chmod(0o600)
    return path


def _archive_debug_log(
    path: Path | None,
    output_dir: Path,
    known_secrets: Mapping[str, str],
) -> tuple[bool, bool]:
    if path is None:
        return True, False
    exposed = False
    present = False
    try:
        if path.is_symlink() or not path.is_file():
            return False, False
        raw = path.read_bytes()
        present = bool(raw)
        text = raw.decode("utf-8", errors="replace")
        exposed = any(secret and secret in text for secret in known_secrets.values())
        for secret in known_secrets.values():
            text = redact_known_secret(text, secret)
        secure_write_text(output_dir / "debug.log", text)
        return present, exposed
    finally:
        path.unlink(missing_ok=True)


def _read_background_job_state(
    environment: Mapping[str, str], session_id: str
) -> dict[str, Any] | None:
    """读取官方后台调度器的单任务状态，不猜测 daemon 私有 socket。"""

    config_value = environment.get("CLAUDE_CONFIG_DIR")
    if not config_value:
        return None
    config_root = Path(config_value)
    if not config_root.is_absolute() or config_root.is_symlink():
        return None
    state_path = config_root / "jobs" / session_id / "state.json"
    if state_path.is_symlink() or not state_path.is_file():
        return None
    if not state_path.resolve().is_relative_to(config_root.resolve()):
        return None
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _background_process_count(session_id: str) -> int:
    """核验后台 daemon、PTY host 与目标 session 均已离开当前容器。"""

    if not session_id:
        return 0
    count = 0
    session_marker = session_id.encode("ascii")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if (
            b"daemon run --origin transient" in command
            or b"bg-pty-host" in command
            or session_marker in command
        ):
            count += 1
    return count


def _background_management_command(
    *,
    command: list[str],
    environment: dict[str, str],
    workspace: Path,
    known_secrets: Mapping[str, str],
) -> tuple[dict[str, Any], str, str, int, bool]:
    """执行一个冻结的后台管理命令，并返回可归档的脱敏结果。"""

    invocation = argv_manifest_view(command, known_secrets)
    invocation.update(
        {
            "cwd": str(workspace),
            "environment": environment_manifest_view(environment, known_secrets),
        }
    )
    completed = subprocess.run(
        command,
        env=environment,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        **_popen_safety_options(),
    )
    exposed = any(
        secret and (secret in completed.stdout or secret in completed.stderr)
        for secret in known_secrets.values()
    )
    stdout, stderr = completed.stdout, completed.stderr
    for secret in known_secrets.values():
        stdout = redact_known_secret(stdout, secret)
        stderr = redact_known_secret(stderr, secret)
    return invocation, stdout, stderr, completed.returncode, exposed


def _manage_background_session(
    *,
    claude_bin: str,
    probe: ClaudeFWFCompleteProbe,
    launcher_stdout: str,
    environment: dict[str, str],
    workspace: Path,
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
) -> dict[str, Any]:
    """等待 `--bg` 真实出站完成，并在退出前收口后台任务。"""

    matched = BACKGROUND_ID_RE.search(launcher_stdout)
    session_id = matched.group("id") if matched else ""
    polls: list[dict[str, Any]] = []
    invocations: dict[str, dict[str, Any]] = {}
    management_stderr: dict[str, str] = {}
    response_observed = False
    runtime_secret_exposed = False
    final_state = "missing"
    stopped_by_driver = False
    daemon_settle_waited = False
    deadline = time.monotonic() + max(1, min(probe.wait_seconds, timeout - 20))

    while session_id and time.monotonic() < deadline:
        command = [claude_bin, "agents", "--json", "--all"]
        invocation, stdout, stderr, return_code, exposed = _background_management_command(
            command=command,
            environment=environment,
            workspace=workspace,
            known_secrets=known_secrets,
        )
        invocations.setdefault("agents", invocation)
        management_stderr["agents"] = stderr
        runtime_secret_exposed = runtime_secret_exposed or exposed
        agents: list[dict[str, Any]] = []
        try:
            decoded = json.loads(stdout)
            if isinstance(decoded, list):
                agents = [item for item in decoded if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
        agent = next(
            (item for item in agents if str(item.get("id", "")) == session_id),
            None,
        )
        job = _read_background_job_state(environment, session_id)
        final_state = str(
            (job or {}).get("state") or (agent or {}).get("state") or "missing"
        )
        output_value = (job or {}).get("output")
        output_text = (
            output_value
            if isinstance(output_value, str)
            else json.dumps(output_value, ensure_ascii=False, sort_keys=True)
            if output_value is not None
            else ""
        )
        marker_present = bool(probe.marker and probe.marker in output_text)
        response_observed = response_observed or marker_present
        polls.append(
            {
                "agents_return_code": return_code,
                "agent_present": agent is not None,
                "job_state_present": job is not None,
                "state": final_state,
                "output_present": output_value is not None,
                "marker_present": marker_present,
            }
        )
        if response_observed or final_state in BACKGROUND_TERMINAL_STATES:
            break
        time.sleep(2)

    if session_id and final_state not in {"stopped", "killed"}:
        command = [claude_bin, "stop", session_id]
        invocation, _stdout, stderr, return_code, exposed = (
            _background_management_command(
                command=command,
                environment=environment,
                workspace=workspace,
                known_secrets=known_secrets,
            )
        )
        invocations["stop"] = invocation
        management_stderr["stop"] = stderr
        runtime_secret_exposed = runtime_secret_exposed or exposed
        stopped_by_driver = return_code == 0
        time.sleep(1)
        job = _read_background_job_state(environment, session_id)
        final_state = str((job or {}).get("state") or final_state)

    if session_id and (
        final_state in BACKGROUND_TERMINAL_STATES or stopped_by_driver
    ):
        # 后台 daemon 无客户端后会在五秒内自行退出。无论任务自然 done 还是由
        # 驱动 stop，都等待它完成最后的 hello／连接回收，避免 relay 先停导致
        # 单向截断连接，也避免下一场景误复用前一 attempt 的本地控制进程。
        time.sleep(10)
        daemon_settle_waited = True

    remaining_background_process_count = _background_process_count(session_id)
    cleanup_confirmed = bool(
        final_state in BACKGROUND_TERMINAL_STATES
        and remaining_background_process_count == 0
        and (final_state in {"stopped", "killed"} or stopped_by_driver)
    )
    result = {
        "schema_version": "claude-code-fw-f-v4-background-control/v1",
        "session_id": session_id,
        "session_id_observed": bool(session_id),
        "response_marker": probe.marker,
        "response_observed": response_observed,
        "final_state": final_state,
        "cleanup_confirmed": cleanup_confirmed,
        "stopped_by_driver": stopped_by_driver,
        "daemon_settle_waited": daemon_settle_waited,
        "remaining_background_process_count": remaining_background_process_count,
        "runtime_secret_exposed": runtime_secret_exposed,
        "polls": polls,
        "management_invocations": invocations,
        "management_stderr": management_stderr,
        "valid": bool(
            session_id
            and response_observed
            and cleanup_confirmed
            and not runtime_secret_exposed
        ),
    }
    secure_write_json(output_dir / "background-control.json", result)
    return result


def _run_print_probe(
    *,
    claude_bin: str,
    model: str,
    probe: ClaudeFWFCompleteProbe,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
) -> dict[str, Any]:
    workspace, fixture_args = _fixture_paths(output_dir, probe)
    debug_path = _new_debug_path(probe)
    # `--bg` 是真实交互会话入口；官方 2.1.226 明确拒绝把它与
    # `--print`／`--no-session-persistence` 组合。后台场景因此必须从
    # 无 `-p` 的命令骨架启动，其余普通探针仍使用可结构化取证的 print 模式。
    command = [claude_bin]
    if probe.driver != "background":
        command.append("-p")
    command.extend(["--model", model, "--no-chrome"])
    if probe.driver != "background":
        command.extend(("--prompt-suggestions", "false"))
    command.extend(("--max-budget-usd", "3.00"))
    if probe.driver != "background":
        command.append("--no-session-persistence")
    if probe.safe_mode:
        command.append("--safe-mode")
    command.extend(fixture_args)
    if debug_path is not None:
        command.extend(("--debug-file", str(debug_path)))
    if probe.driver != "background":
        command.extend(probe.cli_args)
    if probe.tools:
        command.extend(("--tools", ",".join(probe.tools)))
    elif probe.driver != "background":
        command.extend(("--tools", ""))
    if probe.allowed_tools:
        command.extend(("--allowedTools", ",".join(probe.allowed_tools)))
        command.extend(("--permission-mode", "dontAsk"))
    if probe.driver == "background":
        # `--bg` 接收紧随其后的可选 task。它必须位于所有普通选项之后；
        # 同时不能在它前面使用可变长的空 `--tools`，否则 2.1.226 会把 task
        # 吞进 tools 参数并建立一个“idle — send a prompt to start”的空任务。
        command.extend(probe.cli_args)
        command.append(probe.prompt)
    else:
        command.extend(("--output-format", "stream-json", "--verbose", probe.prompt))
    invocation = argv_manifest_view(command, known_secrets)
    invocation.update(
        {
            "cwd": str(workspace),
            "environment": environment_manifest_view(environment, known_secrets),
        }
    )
    secure_write_json(output_dir / "invocation.json", invocation)
    try:
        completed = subprocess.run(
            command,
            env=environment,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            **_popen_safety_options(),
        )
    except BaseException:
        if debug_path is not None:
            debug_path.unlink(missing_ok=True)
        raise
    debug_present, debug_secret_exposed = _archive_debug_log(
        debug_path, output_dir, known_secrets
    )
    background_control = None
    if probe.driver == "background" and completed.returncode == 0:
        background_control = _manage_background_session(
            claude_bin=claude_bin,
            probe=probe,
            launcher_stdout=completed.stdout,
            environment=environment,
            workspace=workspace,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    exposed = any(
        secret and (secret in completed.stdout or secret in completed.stderr)
        for secret in known_secrets.values()
    )
    stdout, stderr = completed.stdout, completed.stderr
    for secret in known_secrets.values():
        stdout = redact_known_secret(stdout, secret)
        stderr = redact_known_secret(stderr, secret)
    secure_write_text(output_dir / "stdout.jsonl", stdout)
    secure_write_text(output_dir / "stderr.log", stderr)
    records = _result_records(stdout)
    results = [record for record in records if record.get("type") == "result"]
    tool_names = sorted(set(_nested_tool_names(records)))
    marker_present = probe.marker is None or any(
        probe.marker in str(record.get("result", "")) for record in results
    )
    if probe.driver == "background":
        outcome_valid = bool(
            completed.returncode == 0
            and background_control
            and background_control.get("valid") is True
        )
    elif probe.expected_outcome == "success":
        outcome_valid = (
            completed.returncode == 0
            and bool(results)
            and all(
                record.get("subtype") == "success"
                and record.get("is_error") is False
                and isinstance(record.get("result"), str)
                for record in results
            )
        )
    else:
        outcome_valid = completed.returncode != 0 or any(
            record.get("is_error") is True for record in results
        )
    tool_valid = probe.expected_tool_name is None or probe.expected_tool_name in tool_names
    return {
        "valid": (
            outcome_valid
            and tool_valid
            and debug_present
            and not exposed
            and not debug_secret_exposed
        ),
        "return_code": completed.returncode,
        "result_count": len(results),
        "marker_present": marker_present,
        "model_text_matches_marker": marker_present,
        "tool_names": tool_names,
        "expected_tool_name": probe.expected_tool_name,
        "runtime_secret_exposed": exposed
        or debug_secret_exposed
        or bool(background_control and background_control.get("runtime_secret_exposed")),
        "debug_log_required": probe.require_debug_log,
        "debug_log_present": debug_present,
        "background_control": background_control,
        "invocation": invocation,
    }


def _run_tui_probe(
    *,
    claude_bin: str,
    model: str,
    probe: ClaudeFWFCompleteProbe,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
) -> dict[str, Any]:
    workspace, fixture_args = _fixture_paths(output_dir, probe)
    debug_path = _new_debug_path(probe)
    args = [
        "--no-chrome",
        "--prompt-suggestions",
        "false",
        "--max-budget-usd",
        "3.00",
        *fixture_args,
        *probe.cli_args,
    ]
    if debug_path is not None:
        args.extend(("--debug-file", str(debug_path)))
    if probe.safe_mode:
        args.insert(0, "--safe-mode")
    command = [claude_bin, "--model", model, *args]
    invocation = argv_manifest_view(command, known_secrets)
    invocation.update(
        {
            "cwd": str(workspace),
            "environment": environment_manifest_view(environment, known_secrets),
            "tui_inputs": list(probe.tui_inputs or (probe.prompt,)),
        }
    )
    secure_write_json(output_dir / "invocation.json", invocation)
    previous = Path.cwd()
    try:
        os.chdir(workspace)
        if probe.tui_inputs:
            result = drive_tui_sequence(
                claude_bin,
                model,
                probe.tui_inputs,
                environment,
                total_timeout=timeout,
                quiet_after_last_input=probe.wait_seconds,
                extra_args=tuple(args),
            )
        else:
            result = drive_tui(
                claude_bin,
                model,
                probe.prompt,
                environment,
                total_timeout=timeout,
                quiet_after_prompt=probe.wait_seconds,
                extra_args=tuple(args),
            )
    except BaseException:
        if debug_path is not None:
            debug_path.unlink(missing_ok=True)
        raise
    finally:
        os.chdir(previous)
    debug_present, debug_secret_exposed = _archive_debug_log(
        debug_path, output_dir, known_secrets
    )
    transcript = str(result.get("transcript", ""))
    exposed = any(secret and secret in transcript for secret in known_secrets.values())
    for secret in known_secrets.values():
        transcript = redact_known_secret(transcript, secret)
    secure_write_text(output_dir / "tui-transcript.log", transcript)
    marker_present = probe.marker is None or probe.marker in transcript
    input_complete = (
        result.get("all_inputs_sent") is True
        if probe.tui_inputs
        else result.get("sent_prompt") is True
    )
    return {
        "valid": (
            input_complete
            and debug_present
            and not exposed
            and not debug_secret_exposed
        ),
        "input_complete": input_complete,
        "sent_prompt": result.get("sent_prompt") is True,
        "sent_inputs": result.get("sent_inputs", []),
        "marker_present": marker_present,
        "steps": result.get("steps", []),
        "runtime_secret_exposed": exposed or debug_secret_exposed,
        "debug_log_required": probe.require_debug_log,
        "debug_log_present": debug_present,
        "invocation": invocation,
    }


def _parse_wire_requests(relay_root: Path) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        matched = re.fullmatch(r"conn(?P<id>[0-9]{3})\.client_to_upstream\.bin", path.name)
        if not matched or path.is_symlink() or not path.is_file():
            raise ClaudeFWFCompleteProbeError(f"R 证据文件身份非法：{path}")
        content = path.read_bytes()
        offset = 0
        while offset < len(content):
            head_end = content.find(b"\r\n\r\n", offset)
            if head_end < 0:
                raise ClaudeFWFCompleteProbeError(f"R 请求缺少 Header 终止符：{path}")
            raw_head = content[offset:head_end]
            lines = raw_head.decode("latin-1").split("\r\n")
            request_line = re.fullmatch(
                r"(?P<method>[A-Z]+) (?P<target>\S+) (?P<version>HTTP/[0-9.]+)",
                lines[0],
            )
            if request_line is None:
                raise ClaudeFWFCompleteProbeError(f"R 请求行非法：{lines[0]}")
            headers: list[dict[str, str]] = []
            content_length = 0
            for line in lines[1:]:
                name, separator, value = line.partition(":")
                if not separator or not name:
                    raise ClaudeFWFCompleteProbeError(f"R Header 非法：{line}")
                item = {"name": name, "value": value.lstrip(" ")}
                headers.append(item)
                if name.lower() == "content-length":
                    try:
                        content_length = int(item["value"])
                    except ValueError as error:
                        raise ClaudeFWFCompleteProbeError("R Content-Length 非整数。") from error
            body_start = head_end + 4
            request_end = body_start + content_length
            if request_end > len(content):
                raise ClaudeFWFCompleteProbeError("R Content-Length 越过连接字节结尾。")
            wire_body = content[body_start:request_end]
            header_map = {item["name"].lower(): item["value"] for item in headers}
            decoded_body = wire_body
            if header_map.get("content-encoding") == "gzip":
                try:
                    decoded_body = gzip.decompress(wire_body)
                except (OSError, EOFError) as error:
                    raise ClaudeFWFCompleteProbeError("R gzip Body 无法解压。") from error
            body: Any = None
            if decoded_body:
                try:
                    body = json.loads(decoded_body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
            raw = content[offset:request_end]
            requests.append(
                {
                    "connection_id": int(matched.group("id")),
                    "evidence_file": path.name,
                    "stream_offset": offset,
                    "method": request_line.group("method"),
                    "request_target": request_line.group("target"),
                    "http_version": request_line.group("version"),
                    "headers": headers,
                    "header_map": header_map,
                    "body": body,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
                }
            )
            offset = request_end
    return requests


def _request_identity(request: Mapping[str, Any]) -> str:
    return f"{request.get('method')} {request.get('request_target')}"


def _message_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        request
        for request in requests
        if _request_identity(request) == "POST /v1/messages?beta=true"
    ]


def _message_tool_names(messages: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for request in messages:
        body = request.get("body")
        if not isinstance(body, dict):
            continue
        tools = body.get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _message_tool_descriptors(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for request in messages:
        body = request.get("body")
        tools = body.get("tools") if isinstance(body, dict) else None
        if isinstance(tools, list):
            descriptors.extend(tool for tool in tools if isinstance(tool, dict))
    return descriptors


def _body_text(messages: list[dict[str, Any]]) -> str:
    return json.dumps(
        [request.get("body") for request in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _header_values(messages: list[dict[str, Any]], name: str) -> list[str]:
    lowered = name.lower()
    return [
        str(request.get("header_map", {}).get(lowered))
        for request in messages
        if lowered in request.get("header_map", {})
    ]


def _scenario_tool_names(scenario_summary: Mapping[str, Any]) -> set[str]:
    inner = scenario_summary.get("inner_result")
    if not isinstance(inner, dict):
        return set()
    values = inner.get("tool_names")
    return {str(value) for value in values} if isinstance(values, list) else set()


def _privacy_environment(scenario_summary: Mapping[str, Any]) -> dict[str, str]:
    inner = scenario_summary.get("inner_result")
    invocation = inner.get("invocation") if isinstance(inner, dict) else None
    environment = invocation.get("environment") if isinstance(invocation, dict) else None
    values = environment.get("values") if isinstance(environment, dict) else None
    return {str(key): str(value) for key, value in values.items()} if isinstance(values, dict) else {}


def _dimension_passed(
    dimension: str,
    probe: ClaudeFWFCompleteProbe,
    requests: list[dict[str, Any]],
    scenario_summary: Mapping[str, Any],
    relay_integrity: Mapping[str, Any],
    pcap_receipt: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    messages = _message_requests(requests)
    request_ids = [_request_identity(request) for request in requests]
    message_tools = _message_tool_names(messages)
    tool_descriptors = _message_tool_descriptors(messages)
    body_text = _body_text(messages)
    scenario_tools = _scenario_tool_names(scenario_summary)
    source_probe = probe.source_v3_probe or probe.probe_id
    synthetic = relay_integrity.get("synthetic_plan")
    synthetic = synthetic if isinstance(synthetic, dict) else {}
    plan = str(synthetic.get("plan") or probe.response_plan or "")
    actions = [str(value) for value in synthetic.get("actions", [])]
    agent_ids = set(_header_values(messages, "x-claude-code-agent-id"))
    session_ids = set(_header_values(messages, "x-claude-code-session-id"))
    user_agents = _header_values(messages, "user-agent")

    if dimension == "transport.native_tls":
        passed = pcap_receipt.get("target_client_hello_count", 0) > 0
    elif dimension == "transport.alpn":
        observations = pcap_receipt.get("observations", [])
        offers = [
            item.get("alpn_offer", [])
            for item in observations
            if isinstance(item, dict)
        ]
        passed = (
            bool(offers)
            and any("http/1.1" in offer for offer in offers)
            and all(set(offer).issubset({"http/1.1"}) for offer in offers)
        )
    elif dimension == "transport.http":
        passed = bool(requests) and all(
            request.get("http_version") == "HTTP/1.1" for request in requests
        )
    elif dimension == "transport.connection_reuse":
        counts: dict[int, int] = {}
        for request in messages:
            connection_id = int(request["connection_id"])
            counts[connection_id] = counts.get(connection_id, 0) + 1
        passed = any(count >= 2 for count in counts.values())
    elif dimension == "entrypoint.tui":
        inner_result = scenario_summary.get("inner_result")
        input_complete = bool(
            isinstance(inner_result, Mapping)
            and inner_result.get("input_complete") is True
        )
        passed = (
            any("(external, cli)" in value for value in user_agents)
            or (
                probe.driver == "tui"
                and input_complete
                and (
                    "HEAD /api/hello" in request_ids
                    or "GET /api/oauth/profile" in request_ids
                )
            )
        )
    elif dimension == "entrypoint.sdk_cli":
        passed = any("(external, sdk-cli)" in value for value in user_agents)
    elif dimension == "entrypoint.agent_sdk":
        passed = any("agent-sdk/" in value for value in user_agents)
    elif dimension == "entrypoint.workload":
        passed = any("workload/" in value for value in user_agents) and "cc_workload=" in body_text
    elif dimension == "agency.foreground":
        passed = bool(messages) and any(
            "x-claude-code-agent-id" not in request.get("header_map", {})
            for request in messages
        )
    elif dimension == "agency.background":
        inner = scenario_summary.get("inner_result")
        control = inner.get("background_control") if isinstance(inner, dict) else None
        passed = bool(
            isinstance(control, dict) and control.get("valid") is True
        ) or ('"run_in_background":true' in body_text and "Agent" in scenario_tools)
    elif dimension == "agency.subagent":
        passed = bool(agent_ids) and "cc_is_subagent=true" in body_text
    elif dimension == "agency.nested_subagent":
        minimum = 3 if source_probe == "a3" else 2
        passed = len(agent_ids) >= minimum and "x-claude-code-parent-agent-id" in {
            name.lower()
            for request in messages
            for name in request.get("header_map", {})
        }
    elif dimension == "agency.fork":
        passed = source_probe == "v3-session-fork" and len(session_ids) >= 2
    elif dimension == "agency.hook":
        passed = "Bash" in scenario_tools and "FW_F_V4_HOOK" in body_text
    elif dimension == "agency.compact":
        passed = probe.driver == "tui" and len(messages) >= 2 and "compact" in body_text.lower()
    elif dimension == "agency.remote":
        passed = any(
            name in request.get("header_map", {})
            for request in messages
            for name in ("x-claude-remote-container-id", "x-claude-remote-session-id")
        )
    elif dimension == "header.custom":
        if source_probe == "v3-custom-header-invalid-name":
            passed = not messages and scenario_summary.get("valid") is True
        else:
            passed = any(
                "x-fw-f-probe" in request.get("header_map", {})
                and "x-fw-f-trim" in request.get("header_map", {})
                for request in messages
            )
    elif dimension == "header.beta":
        values = _header_values(messages, "anthropic-beta")
        passed = bool(values) and any(value.count("oauth-2025-04-20") >= 3 for value in values)
    elif dimension == "header.metadata":
        passed = "fw_f_probe" in body_text and "nested" in body_text
    elif dimension == "header.usage_limit":
        values = _header_values(messages, "anthropic-usage-limit")
        if (
            probe.runtime_conclusion
            == "usage_limit_rollout_not_observed_for_measured_account"
        ):
            passed = bool(messages) and not values and bool(agent_ids)
        else:
            passed = "extended" in values and any(
                "anthropic-usage-limit" not in request.get("header_map", {})
                for request in messages
            )
    elif dimension == "header.dispatch_id":
        values = _header_values(messages, "anthropic-dispatch-id")
        if (
            probe.runtime_conclusion
            == "dispatch_rollout_not_observed_for_measured_account"
        ):
            passed = bool(messages) and not values
        else:
            passed = "v2s" in values and any(
                "anthropic-dispatch-id" not in request.get("header_map", {})
                for request in messages
            )
    elif dimension == "body.system":
        passed = bool(messages) and all(
            isinstance(request.get("body"), dict)
            and isinstance(request["body"].get("system"), list)
            and bool(request["body"]["system"])
            for request in messages
        )
    elif dimension == "body.attachment":
        passed = "fw_f_v4_attachment.txt" in body_text and "附件固定标记" in body_text
    elif dimension == "body.context":
        passed = any(
            marker in body_text
            for marker in ("CLAUDE.md", "FW_F_V4_HOOK", "currentDate", "exclude")
        )
    elif dimension == "body.cache_breakpoint":
        if source_probe in {"v3-cache-disabled", "v3-cache-sonnet-disabled"}:
            passed = '"cache_control"' not in body_text
        else:
            passed = '"cache_control"' in body_text
    elif dimension == "tool.bash":
        passed = "Bash" in message_tools and (
            "Bash" in scenario_tools or '"tool_result"' in body_text
        )
    elif dimension == "tool.agent":
        passed = "Agent" in message_tools and bool(agent_ids)
    elif dimension == "tool.mcp":
        passed = any(name.startswith("mcp__claude-fw-f-v4__") for name in message_tools) and any(
            name.startswith("mcp__claude-fw-f-v4__") for name in scenario_tools
        )
    elif dimension == "tool.web_search":
        passed = "WebSearch" in message_tools and "WebSearch" in scenario_tools
    elif dimension == "tool.advisor":
        advisor = [tool for tool in tool_descriptors if tool.get("name") == "advisor"]
        if probe.runtime_conclusion == "advisor_default_disabled_negative":
            passed = not advisor
        else:
            passed = any(
                tool.get("type") == "advisor_20260301"
                and tool.get("model") == "claude-fable-5"
                for tool in advisor
            )
    elif dimension == "tool.deferred":
        passed = any("deferred_probe_32" in name for name in message_tools) and any(
            "deferred_probe_32" in name for name in scenario_tools
        )
    elif dimension == "tool.extended":
        if probe.runtime_conclusion == "advisor_default_disabled_negative":
            passed = "advisor" not in message_tools
        else:
            passed = bool(
                message_tools
                & {
                    "WebSearch",
                    "advisor",
                    "mcp__claude-fw-f-v4__deferred_probe_32",
                }
            )
    elif dimension.startswith("failure."):
        expected_fragments = {
            "failure.401": ("401",),
            "failure.403": ("403",),
            "failure.408": ("408",),
            "failure.409": ("409",),
            "failure.429": ("429",),
            "failure.5xx": ("500", "502", "503"),
            "failure.529": ("529",),
            "failure.retry_after": ("after",),
            "failure.disconnect": ("disconnect", "interrupt"),
            "failure.fallback": ("fallback-model",),
            "failure.non_stream": ("stream-404", "stream-interrupt"),
        }[dimension]
        passed = any(fragment in plan for fragment in expected_fragments) and bool(actions)
    elif dimension == "aux.count_tokens":
        passed = "POST /v1/messages/count_tokens?beta=true" in request_ids
    elif dimension == "aux.oauth_refresh":
        passed = request_ids == ["POST /v1/oauth/token"] and plan == "oauth-refresh-reject"
    elif dimension == "aux.usage":
        environment = _privacy_environment(scenario_summary)
        passed = (
            probe.runtime_conclusion == "usage_blocked_by_essential_traffic_only"
            and environment.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
            and "GET /api/oauth/usage" not in request_ids
        )
    elif dimension == "aux.models":
        passed = (
            probe.runtime_conclusion == "model_capabilities_hard_disabled_in_2_1_226"
            and "GET /v1/models" not in request_ids
            and "GET /v1/models?beta=true" not in request_ids
        )
    elif dimension in {"privacy.telemetry_disabled", "privacy.nonessential_disabled"}:
        environment = _privacy_environment(scenario_summary)
        expected_key = (
            "DISABLE_TELEMETRY"
            if dimension == "privacy.telemetry_disabled"
            else "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
        )
        passed = environment.get(expected_key) == "1"
    else:
        raise ClaudeFWFCompleteProbeError(f"缺少维度断言实现：{dimension}")
    observation = {
        "request_count": len(requests),
        "messages_request_count": len(messages),
        "request_identities": sorted(set(request_ids)),
        "message_tool_names": sorted(message_tools),
        "scenario_tool_names": sorted(scenario_tools),
        "response_plan": plan or None,
    }
    if dimension.startswith("header."):
        header_name = {
            "header.usage_limit": "anthropic-usage-limit",
            "header.dispatch_id": "anthropic-dispatch-id",
        }.get(dimension)
        if header_name is not None:
            observation["observed_header_values"] = _header_values(
                messages, header_name
            )
    if dimension == "entrypoint.tui":
        inner_result = scenario_summary.get("inner_result")
        observation["tui_input_complete"] = bool(
            isinstance(inner_result, Mapping)
            and inner_result.get("input_complete") is True
        )
    if dimension == "aux.usage":
        observation["nonessential_traffic_disabled"] = (
            _privacy_environment(scenario_summary).get(
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
            )
            == "1"
        )
    return passed, observation


def validate_complete_probe_evidence(
    *,
    probe_id: str,
    relay_root: Path,
    scenario_summary: Mapping[str, Any],
    relay_integrity: Mapping[str, Any],
    pcap_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    probe = get_probe(probe_id)
    requests = _parse_wire_requests(relay_root)
    identities = [_request_identity(request) for request in requests]
    messages = _message_requests(requests)
    message_tools = _message_tool_names(messages)
    contract_failures: list[str] = []
    if probe.message_request_expectation == "at-least-one" and not messages:
        contract_failures.append("messages_request_missing")
    if probe.message_request_expectation == "zero" and messages:
        contract_failures.append("unexpected_messages_request")
    for expected in probe.required_requests:
        if expected not in identities:
            contract_failures.append(f"required_request_missing:{expected}")
    for forbidden in probe.forbidden_requests:
        if forbidden in identities:
            contract_failures.append(f"forbidden_request_observed:{forbidden}")
    for expected in probe.required_message_tools:
        if expected not in message_tools:
            contract_failures.append(f"required_message_tool_missing:{expected}")
    for forbidden in probe.forbidden_message_tools:
        if forbidden in message_tools:
            contract_failures.append(f"forbidden_message_tool_observed:{forbidden}")
    assertions: list[dict[str, Any]] = []
    for dimension in probe.dimensions:
        passed, observation = _dimension_passed(
            dimension,
            probe,
            requests,
            scenario_summary,
            relay_integrity,
            pcap_receipt,
        )
        assertions.append(
            {
                "assertion_id": f"DIM-{probe_id}-{dimension}",
                "dimension": dimension,
                "result": "passed" if passed else "failed",
                "observation": observation,
            }
        )
    failed_dimensions = [
        assertion["dimension"]
        for assertion in assertions
        if assertion["result"] != "passed"
    ]
    inventory = [
        {
            "connection_id": request["connection_id"],
            "evidence_file": request["evidence_file"],
            "stream_offset": request["stream_offset"],
            "method": request["method"],
            "request_target": request["request_target"],
            "http_version": request["http_version"],
            "header_names": [item["name"] for item in request["headers"]],
            "body_keys": (
                list(request["body"].keys()) if isinstance(request["body"], dict) else []
            ),
            "message_tool_names": (
                sorted(
                    str(tool.get("name"))
                    for tool in request["body"].get("tools", [])
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                )
                if isinstance(request["body"], dict)
                and isinstance(request["body"].get("tools"), list)
                else []
            ),
            "raw_sha256": request["raw_sha256"],
            "wire_body_sha256": request["wire_body_sha256"],
        }
        for request in requests
    ]
    result = {
        "schema_version": SCHEMA_DIMENSION_EVIDENCE,
        "probe_id": probe_id,
        "runtime_conclusion": probe.runtime_conclusion,
        "actual_wire_inventory": inventory,
        "wire_contract": {
            "required_requests": list(probe.required_requests),
            "forbidden_requests": list(probe.forbidden_requests),
            "required_message_tools": list(probe.required_message_tools),
            "forbidden_message_tools": list(probe.forbidden_message_tools),
            "failures": contract_failures,
        },
        "dimension_assertions": assertions,
        "failed_dimensions": failed_dimensions,
        "result": (
            "passed" if not contract_failures and not failed_dimensions else "failed"
        ),
    }
    return result


def run_claude_fw_f_complete_probe(
    *,
    claude_bin: str,
    model: str,
    probe_id: str,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
) -> dict[str, Any]:
    """执行一个冻结 v4 场景，并写入追加式 M 结果。"""

    validate_probe_catalog()
    probe = get_probe(probe_id)
    ensure_private_directory(output_dir)
    for key, value in probe.env_dict().items():
        if environment.get(key) != value:
            raise ClaudeFWFCompleteProbeError(
                f"{probe_id} 运行环境与冻结目录不一致：{key}"
            )
    if probe.driver == "v3":
        inner = run_v3_probe(
            claude_bin=claude_bin,
            model=model,
            probe_id=str(probe.source_v3_probe),
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    elif probe.driver == "legacy":
        inner = run_claude_scenario(
            claude_bin=claude_bin,
            model=model,
            scenario=str(probe.legacy_scenario),
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            runtime_secret=(
                known_secrets.get("access_token")
                or known_secrets.get("claude_oauth_access_token")
            ),
            known_secrets=dict(known_secrets),
        )
    elif probe.driver == "tui":
        inner = _run_tui_probe(
            claude_bin=claude_bin,
            model=model,
            probe=probe,
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    else:
        inner = _run_print_probe(
            claude_bin=claude_bin,
            model=model,
            probe=probe,
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    result = {
        "schema_version": SCHEMA_RESULT,
        "probe_id": probe_id,
        "driver": probe.driver,
        "source_v3_probe": probe.source_v3_probe,
        "legacy_scenario": probe.legacy_scenario,
        "dimensions": list(probe.dimensions),
        "target_host": probe.target_host,
        "response_plan": probe.response_plan,
        "expected_outcome": probe.expected_outcome,
        "message_request_expectation": probe.message_request_expectation,
        "require_pcap": probe.require_pcap,
        "inner_result": inner,
        "valid": inner.get("valid") is True,
        "catalog_producer": {
            "path": "tools/official_client_capture/capturelib/claude_fw_f_v4.py",
            "sha256": file_sha256(Path(__file__)),
        },
    }
    secure_write_json(output_dir / "v4-summary.json", result)
    return result
