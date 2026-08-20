"""Claude Code 2.1.226 FW-F v3 的固定场景目录与执行驱动。

该模块只接受源码内冻结的场景身份，不接受任意 argv、任意 prompt 或任意环境变量。
真实上游场景用于观察 Header／Body／状态关系；合成故障场景仍由同一个官方二进制
发起请求，但响应来自隔离中继的白名单计划，绝不连接生产上游。
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.drive_claude_tui import drive as drive_tui

from .environment import INJECTABLE_PROBE_KEYS, environment_manifest_view
from .lifecycle import _popen_safety_options
from .security import (
    argv_manifest_view,
    ensure_private_directory,
    file_sha256,
    redact_known_secret,
    secure_write_json,
    secure_write_text,
)


DEFAULT_MARKER = "FW_F_V3_OK"
JSON_MARKER = "fw-f-v3-ok"
SESSION_FIRST_MARKER = "FW_F_V3_SESSION_FIRST_OK"
SESSION_SECOND_MARKER = "FW_F_V3_SESSION_SECOND_OK"
TUI_MARKER = "FW_F_V3_TUI_OK"


@dataclasses.dataclass(frozen=True)
class ClaudeFWFProbe:
    """一个参数、条件和预期结果均冻结的官方客户端探针。"""

    probe_id: str
    driver: str = "print"
    prompt: str = f"只回复 {DEFAULT_MARKER}，不得调用任何工具。"
    marker: str = DEFAULT_MARKER
    cli_args: tuple[str, ...] = ()
    injected_env: tuple[tuple[str, str], ...] = ()
    response_plan: str | None = None
    expected_outcome: str = "success"
    local_error_marker: str | None = None
    message_request_expectation: str = "at-least-one"
    safe_mode: bool = True
    preserve_session: bool = False
    long_prompt_bytes: int = 0

    def env_dict(self) -> dict[str, str]:
        return dict(self.injected_env)


def _probe(
    probe_id: str,
    *,
    driver: str = "print",
    prompt: str | None = None,
    marker: str = DEFAULT_MARKER,
    cli_args: tuple[str, ...] = (),
    injected_env: Mapping[str, str] | None = None,
    response_plan: str | None = None,
    expected_outcome: str = "success",
    local_error_marker: str | None = None,
    message_request_expectation: str = "at-least-one",
    safe_mode: bool = True,
    preserve_session: bool = False,
    long_prompt_bytes: int = 0,
) -> ClaudeFWFProbe:
    return ClaudeFWFProbe(
        probe_id=probe_id,
        driver=driver,
        prompt=prompt or f"只回复 {marker}，不得调用任何工具。",
        marker=marker,
        cli_args=cli_args,
        injected_env=tuple(sorted((injected_env or {}).items())),
        response_plan=response_plan,
        expected_outcome=expected_outcome,
        local_error_marker=local_error_marker,
        message_request_expectation=message_request_expectation,
        safe_mode=safe_mode,
        preserve_session=preserve_session,
        long_prompt_bytes=long_prompt_bytes,
    )


# 这些场景只覆盖会改变 Claude Persona `/v1/messages` 最终 wire 或跨请求状态的
# 条件。遥测、反馈和非必要流量不在目录中，也不得被“零流量”计作画像差异。
PROBES: dict[str, ClaudeFWFProbe] = {
    "v3-baseline": _probe("v3-baseline"),
    "v3-tui": _probe(
        "v3-tui",
        driver="tui",
        prompt="只回复把 FW_F_V3_、TUI_、OK 三段直接拼接后的字符串，不得调用任何工具。",
        marker=TUI_MARKER,
    ),
    "v3-agent-sdk": _probe(
        "v3-agent-sdk",
        injected_env={"CLAUDE_AGENT_SDK_VERSION": "9.9.9-fw-f-v3"},
    ),
    "v3-client-app": _probe(
        "v3-client-app",
        injected_env={"CLAUDE_AGENT_SDK_CLIENT_APP": "fw-f-v3-client"},
    ),
    "v3-workload": _probe(
        "v3-workload",
        cli_args=("--workload", "fw-f-v3-workload"),
    ),
    "v3-remote-container": _probe(
        "v3-remote-container",
        injected_env={"CLAUDE_CODE_CONTAINER_ID": "fw-f-v3-container"},
    ),
    "v3-remote-session": _probe(
        "v3-remote-session",
        injected_env={"CLAUDE_CODE_REMOTE_SESSION_ID": "fw-f-v3-remote-session"},
    ),
    "v3-header-combination": _probe(
        "v3-header-combination",
        injected_env={
            "CLAUDE_AGENT_SDK_CLIENT_APP": "fw-f-v3-combo-app",
            "CLAUDE_AGENT_SDK_VERSION": "9.9.9-fw-f-v3-combo",
            "CLAUDE_CODE_ADDITIONAL_PROTECTION": "1",
            "CLAUDE_CODE_CONTAINER_ID": "fw-f-v3-combo-container",
            "CLAUDE_CODE_REMOTE_SESSION_ID": "fw-f-v3-combo-session",
        },
        cli_args=("--workload", "fw-f-v3-combo-workload"),
    ),
    "v3-custom-header-grammar": _probe(
        "v3-custom-header-grammar",
        injected_env={
            "ANTHROPIC_CUSTOM_HEADERS": (
                "X-FW-F-Probe: value:with:colon\n"
                "  X-FW-F-Trim  :  trimmed-value  \n"
                "badline\n\n"
                "x-client-request-id: 11111111-2222-4333-8444-555555555555"
            )
        },
    ),
    "v3-custom-header-invalid-name": _probe(
        "v3-custom-header-invalid-name",
        injected_env={"ANTHROPIC_CUSTOM_HEADERS": ":value-without-name"},
        expected_outcome="failure",
        local_error_marker="API Error: Invalid header name: ''",
        message_request_expectation="zero",
    ),
    "v3-additional-protection": _probe(
        "v3-additional-protection",
        injected_env={"CLAUDE_CODE_ADDITIONAL_PROTECTION": "true"},
    ),
    "v3-extra-body": _probe(
        "v3-extra-body",
        injected_env={"CLAUDE_CODE_EXTRA_BODY": '{"max_tokens":2048}'},
    ),
    "v3-extra-metadata": _probe(
        "v3-extra-metadata",
        injected_env={
            "CLAUDE_CODE_EXTRA_METADATA": (
                '{"fw_f_probe":"v3","nested":{"value":"measured"}}'
            )
        },
    ),
    "v3-attribution-disabled": _probe(
        "v3-attribution-disabled",
        injected_env={"CLAUDE_CODE_ATTRIBUTION_HEADER": "false"},
    ),
    "v3-thinking-disabled": _probe(
        "v3-thinking-disabled",
        injected_env={"CLAUDE_CODE_DISABLE_THINKING": "1"},
    ),
    "v3-adaptive-thinking-disabled": _probe(
        "v3-adaptive-thinking-disabled",
        injected_env={"CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1"},
    ),
    "v3-cache-disabled": _probe(
        "v3-cache-disabled",
        injected_env={"DISABLE_PROMPT_CACHING": "1"},
    ),
    "v3-cache-sonnet-disabled": _probe(
        "v3-cache-sonnet-disabled",
        injected_env={"DISABLE_PROMPT_CACHING_SONNET": "1"},
    ),
    "v3-cache-one-hour": _probe(
        "v3-cache-one-hour",
        injected_env={"ENABLE_PROMPT_CACHING_1H": "1"},
    ),
    "v3-max-output-tokens": _probe(
        "v3-max-output-tokens",
        injected_env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2048"},
    ),
    "v3-gzip-request": _probe(
        "v3-gzip-request",
        injected_env={"CLAUDE_CODE_GZIP_REQUEST_BODIES": "1"},
        long_prompt_bytes=24576,
    ),
    "v3-custom-system": _probe(
        "v3-custom-system",
        cli_args=("--system-prompt", "FW-F v3 自定义系统提示词。"),
    ),
    "v3-append-system": _probe(
        "v3-append-system",
        cli_args=("--append-system-prompt", "FW-F v3 追加系统提示词。"),
    ),
    "v3-exclude-dynamic-system": _probe(
        "v3-exclude-dynamic-system",
        cli_args=("--exclude-dynamic-system-prompt-sections",),
    ),
    "v3-effort-low": _probe("v3-effort-low", cli_args=("--effort", "low")),
    "v3-effort-medium": _probe(
        "v3-effort-medium", cli_args=("--effort", "medium")
    ),
    "v3-effort-xhigh": _probe(
        "v3-effort-xhigh", cli_args=("--effort", "xhigh")
    ),
    "v3-effort-max": _probe("v3-effort-max", cli_args=("--effort", "max")),
    "v3-json-schema": _probe(
        "v3-json-schema",
        prompt=(
            "只返回满足给定 JSON Schema 的对象，其中 value 必须精确为 fw-f-v3-ok。"
        ),
        marker=JSON_MARKER,
        cli_args=(
            "--json-schema",
            '{"type":"object","properties":{"value":{"type":"string",'
            '"const":"fw-f-v3-ok"}},"required":["value"],'
            '"additionalProperties":false}',
        ),
    ),
    "v3-custom-agent": _probe(
        "v3-custom-agent",
        safe_mode=False,
        cli_args=(
            "--agents",
            '{"fw-f-reviewer":{"description":"FW-F v3 固定取证代理",'
            '"prompt":"你只执行固定出站取证任务。"}}',
            "--agent",
            "fw-f-reviewer",
        ),
    ),
    "v3-custom-agent-safe-mode": _probe(
        "v3-custom-agent-safe-mode",
        cli_args=(
            "--agents",
            '{"fw-f-reviewer":{"description":"FW-F v3 固定取证代理",'
            '"prompt":"你只执行固定出站取证任务。"}}',
            "--agent",
            "fw-f-reviewer",
        ),
        expected_outcome="failure",
        local_error_marker="--agent 'fw-f-reviewer' not found.",
        message_request_expectation="zero",
    ),
    "v3-session-resume": _probe(
        "v3-session-resume",
        driver="resume",
        preserve_session=True,
        marker=SESSION_SECOND_MARKER,
    ),
    "v3-session-fork": _probe(
        "v3-session-fork",
        driver="fork",
        preserve_session=True,
        marker=SESSION_SECOND_MARKER,
    ),
    "v3-beta-deduplicate": _probe(
        "v3-beta-deduplicate",
        injected_env={
            "ANTHROPIC_BETAS": (
                " oauth-2025-04-20, claude-code-20250219,"
                "oauth-2025-04-20,, "
            )
        },
    ),
    "v3-retry-408": _probe("v3-retry-408", response_plan="retry-408"),
    "v3-retry-409": _probe("v3-retry-409", response_plan="retry-409"),
    "v3-retry-401": _probe("v3-retry-401", response_plan="retry-401"),
    "v3-retry-429": _probe("v3-retry-429", response_plan="retry-429"),
    "v3-retry-after-seconds": _probe(
        "v3-retry-after-seconds", response_plan="retry-429-after-seconds"
    ),
    "v3-retry-after-date": _probe(
        "v3-retry-after-date", response_plan="retry-429-after-date"
    ),
    "v3-retry-500": _probe("v3-retry-500", response_plan="retry-500"),
    "v3-retry-502": _probe("v3-retry-502", response_plan="retry-502"),
    "v3-retry-503": _probe("v3-retry-503", response_plan="retry-503"),
    "v3-retry-529": _probe("v3-retry-529", response_plan="retry-529"),
    "v3-nonretry-400": _probe(
        "v3-nonretry-400",
        response_plan="nonretry-400",
        expected_outcome="failure",
    ),
    "v3-nonretry-403": _probe(
        "v3-nonretry-403",
        response_plan="nonretry-403",
        expected_outcome="failure",
    ),
    "v3-retry-limit": _probe(
        "v3-retry-limit",
        response_plan="always-529",
        expected_outcome="failure",
        injected_env={"CLAUDE_CODE_MAX_RETRIES": "2"},
    ),
    "v3-fallback-model": _probe(
        "v3-fallback-model",
        response_plan="fallback-model",
        cli_args=("--fallback-model", "claude-haiku-4-5"),
        injected_env={"CLAUDE_CODE_MAX_RETRIES": "2"},
    ),
    "v3-stream-404-fallback": _probe(
        "v3-stream-404-fallback", response_plan="stream-404-fallback"
    ),
    "v3-stream-404-disable-flag": _probe(
        "v3-stream-404-disable-flag",
        response_plan="stream-404-fallback",
        injected_env={"CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1"},
    ),
    "v3-stream-interrupt": _probe(
        "v3-stream-interrupt", response_plan="stream-interrupt-fallback"
    ),
    "v3-stream-interrupt-no-fallback": _probe(
        "v3-stream-interrupt-no-fallback",
        response_plan="stream-interrupt-no-fallback",
        expected_outcome="failure",
        injected_env={"CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1"},
    ),
    "v3-disconnect-retry": _probe(
        "v3-disconnect-retry", response_plan="disconnect-retry"
    ),
    "v3-timeout": _probe(
        "v3-timeout",
        response_plan="stall",
        expected_outcome="failure",
        injected_env={"API_TIMEOUT_MS": "1000", "CLAUDE_CODE_MAX_RETRIES": "0"},
    ),
}

PROBE_IDS = tuple(sorted(PROBES))
SYNTHETIC_RESPONSE_PLANS = tuple(
    sorted({probe.response_plan for probe in PROBES.values() if probe.response_plan})
)


class ClaudeFWFProbeError(RuntimeError):
    """表示 v3 场景定义、调用或结果不满足冻结合同。"""


def get_probe(probe_id: str) -> ClaudeFWFProbe:
    try:
        return PROBES[probe_id]
    except KeyError as exc:
        raise ClaudeFWFProbeError(f"未知 FW-F v3 probe：{probe_id}") from exc


def validate_probe_catalog() -> None:
    """启动前复核场景目录没有越过受控参数和环境边界。"""

    allowed_drivers = {"print", "tui", "resume", "fork"}
    allowed_outcomes = {"success", "failure"}
    allowed_message_expectations = {"at-least-one", "zero"}
    for probe_id, probe in PROBES.items():
        if probe.probe_id != probe_id:
            raise ClaudeFWFProbeError(f"probe 字典键与身份不一致：{probe_id}")
        if probe.driver not in allowed_drivers:
            raise ClaudeFWFProbeError(f"{probe_id} driver 非法：{probe.driver}")
        if probe.expected_outcome not in allowed_outcomes:
            raise ClaudeFWFProbeError(
                f"{probe_id} expected_outcome 非法：{probe.expected_outcome}"
            )
        if probe.message_request_expectation not in allowed_message_expectations:
            raise ClaudeFWFProbeError(
                f"{probe_id} message_request_expectation 非法："
                f"{probe.message_request_expectation}"
            )
        if probe.local_error_marker and probe.expected_outcome != "failure":
            raise ClaudeFWFProbeError(
                f"{probe_id} 本地错误标记只能用于预期失败场景"
            )
        if probe.response_plan and probe.message_request_expectation == "zero":
            raise ClaudeFWFProbeError(
                f"{probe_id} 合成故障场景不能声明零 messages"
            )
        unknown_env = set(probe.env_dict()) - INJECTABLE_PROBE_KEYS
        if unknown_env:
            raise ClaudeFWFProbeError(
                f"{probe_id} 使用未批准环境变量：{sorted(unknown_env)}"
            )
        if probe.response_plan and probe.response_plan not in SYNTHETIC_RESPONSE_PLANS:
            raise ClaudeFWFProbeError(f"{probe_id} 响应计划没有进入闭集")
        if probe.driver in {"resume", "fork"} and not probe.preserve_session:
            raise ClaudeFWFProbeError(f"{probe_id} 会话场景必须启用持久化")


def _base_command(
    claude_bin: str,
    model: str,
    probe: ClaudeFWFProbe,
    *,
    session_id: str | None = None,
    resume: bool = False,
    fork: bool = False,
) -> list[str]:
    command = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--no-chrome",
        "--disable-slash-commands",
        "--prompt-suggestions",
        "false",
        "--max-budget-usd",
        "0.50",
        "--tools",
        "",
    ]
    if probe.safe_mode:
        command.append("--safe-mode")
    if not probe.preserve_session:
        command.append("--no-session-persistence")
    if session_id and not resume:
        command.extend(("--session-id", session_id))
    if resume:
        if not session_id:
            raise ClaudeFWFProbeError("resume 场景缺少 session_id")
        command.extend(("--resume", session_id))
    if fork:
        command.append("--fork-session")
    command.extend(probe.cli_args)
    command.extend(("--output-format", "stream-json", "--verbose"))
    return command


def _run(
    command: list[str], environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        **_popen_safety_options(),
    )


def _result_records(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "result":
            records.append(value)
    return records


def _result_matches_marker(record: dict[str, Any], marker: str) -> bool:
    result = record.get("result")
    if isinstance(result, str):
        stripped = result.strip()
        if stripped == marker:
            return True
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return marker in stripped
        return isinstance(parsed, dict) and parsed.get("value") == marker
    return False


def _result_is_success(record: dict[str, Any]) -> bool:
    """只校验官方客户端的成功终态，不把模型回答文本当作 wire 规则。"""

    return (
        record.get("subtype") == "success"
        and record.get("is_error") is False
        and isinstance(record.get("result"), str)
    )


def _result_matches_expected_failure(
    record: dict[str, Any], probe: ClaudeFWFProbe
) -> bool:
    """按冻结故障计划校验 Claude stream-json 的受控失败终态。"""

    if record.get("is_error") is not True:
        return False
    expected_status = {
        "nonretry-400": 400,
        "nonretry-403": 403,
        "always-529": 529,
        "stream-404-fallback": 404,
    }.get(probe.response_plan or "")
    if expected_status is not None:
        return record.get("api_error_status") == expected_status
    if probe.response_plan == "stall":
        return record.get("terminal_reason") in {"api_error", "timeout"}
    if probe.response_plan == "stream-interrupt-no-fallback":
        return record.get("terminal_reason") in {"api_error", "timeout"}
    return False


def _safe_outputs(
    stdout: str,
    stderr: str,
    known_secrets: Mapping[str, str],
) -> tuple[str, str, bool]:
    exposed = any(
        secret and (secret in stdout or secret in stderr)
        for secret in known_secrets.values()
    )
    safe_stdout = stdout
    safe_stderr = stderr
    for secret in known_secrets.values():
        safe_stdout = redact_known_secret(safe_stdout, secret)
        safe_stderr = redact_known_secret(safe_stderr, secret)
    return safe_stdout, safe_stderr, exposed


def _manifest_command(
    command: list[str], environment: Mapping[str, str], known_secrets: Mapping[str, str]
) -> dict[str, Any]:
    value = argv_manifest_view(command, known_secrets)
    value.update(
        {
            "cwd": os.getcwd(),
            "environment": environment_manifest_view(environment, known_secrets),
        }
    )
    return value


def run_claude_fw_f_probe(
    *,
    claude_bin: str,
    model: str,
    probe_id: str,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
) -> dict[str, Any]:
    """执行一个冻结的 v3 场景并生成不含凭据的 M 结果。"""

    validate_probe_catalog()
    probe = get_probe(probe_id)
    ensure_private_directory(output_dir)
    expected_env = probe.env_dict()
    for key, value in expected_env.items():
        if environment.get(key) != value:
            raise ClaudeFWFProbeError(
                f"{probe_id} 运行环境与目录不一致：{key}"
            )

    invocations: list[dict[str, Any]] = []
    outputs: list[tuple[str, str, int]] = []
    runtime_secret_exposed = False

    if probe.driver == "tui":
        extra_args = (
            "--no-chrome",
            "--disable-slash-commands",
            "--prompt-suggestions",
            "false",
            "--max-budget-usd",
            "0.50",
            "--tools",
            "",
            *probe.cli_args,
        )
        if probe.safe_mode:
            extra_args = ("--safe-mode", *extra_args)
        command = [claude_bin, "--model", model, *extra_args]
        invocations.append(_manifest_command(command, environment, known_secrets))
        result = drive_tui(
            claude_bin,
            model,
            probe.prompt,
            environment,
            total_timeout=timeout,
            quiet_after_prompt=min(30, max(10, timeout // 3)),
            extra_args=extra_args,
        )
        transcript = str(result.get("transcript", ""))
        safe_transcript, _, exposed = _safe_outputs(
            transcript, "", known_secrets
        )
        runtime_secret_exposed = exposed
        secure_write_text(output_dir / "tui-transcript.log", safe_transcript)
        valid = result.get("sent_prompt") is True and not exposed
        summary: dict[str, Any] = {
            "driver": probe.driver,
            "valid": valid,
            "sent_prompt": result.get("sent_prompt") is True,
            "marker_present": probe.marker in safe_transcript,
            "steps": result.get("steps", []),
            "runtime_secret_exposed": exposed,
        }
    else:
        session_id = str(uuid.uuid4()) if probe.driver in {"resume", "fork"} else None
        commands: list[tuple[list[str], str]] = []
        if probe.driver in {"resume", "fork"}:
            first = _base_command(
                claude_bin, model, probe, session_id=session_id
            )
            first.append(
                f"只回复 {SESSION_FIRST_MARKER}，不得调用任何工具。"
            )
            second = _base_command(
                claude_bin,
                model,
                probe,
                session_id=session_id,
                resume=True,
                fork=probe.driver == "fork",
            )
            second.append(
                f"只回复 {SESSION_SECOND_MARKER}，不得调用任何工具。"
            )
            commands.extend(((first, SESSION_FIRST_MARKER), (second, SESSION_SECOND_MARKER)))
        else:
            command = _base_command(claude_bin, model, probe)
            prompt = probe.prompt
            if probe.long_prompt_bytes:
                padding = "测" * (probe.long_prompt_bytes // len("测".encode("utf-8")))
                prompt = f"{padding}\n{prompt}"
            command.append(prompt)
            commands.append((command, probe.marker))

        marker_results: list[bool] = []
        success_results: list[bool] = []
        error_results: list[bool] = []
        local_error_results: list[bool] = []
        return_codes: list[int] = []
        for index, (command, marker) in enumerate(commands, start=1):
            invocations.append(_manifest_command(command, environment, known_secrets))
            completed = _run(command, environment, timeout)
            safe_stdout, safe_stderr, exposed = _safe_outputs(
                completed.stdout, completed.stderr, known_secrets
            )
            runtime_secret_exposed = runtime_secret_exposed or exposed
            outputs.append((safe_stdout, safe_stderr, completed.returncode))
            secure_write_text(output_dir / f"stdout-{index:02d}.jsonl", safe_stdout)
            secure_write_text(output_dir / f"stderr-{index:02d}.log", safe_stderr)
            records = _result_records(safe_stdout)
            marker_results.append(
                len(records) == 1 and _result_matches_marker(records[0], marker)
            )
            success_results.append(
                len(records) == 1 and _result_is_success(records[0])
            )
            error_results.append(
                len(records) == 1
                and _result_matches_expected_failure(records[0], probe)
            )
            local_error_results.append(
                bool(
                    probe.local_error_marker
                    and probe.local_error_marker in f"{safe_stdout}\n{safe_stderr}"
                )
            )
            return_codes.append(completed.returncode)

        if probe.expected_outcome == "success":
            valid = (
                all(code == 0 for code in return_codes)
                and all(success_results)
                and not runtime_secret_exposed
            )
        else:
            expected_failure_observed = (
                local_error_results[0]
                if probe.local_error_marker
                else error_results[0]
            )
            valid = (
                len(commands) == 1
                and return_codes[0] != 0
                and expected_failure_observed
                and not runtime_secret_exposed
            )
        summary = {
            "driver": probe.driver,
            "valid": valid,
            "expected_outcome": probe.expected_outcome,
            "return_codes": return_codes,
            "marker_results": marker_results,
            "success_results": success_results,
            "error_results": error_results,
            "local_error_results": local_error_results,
            "runtime_secret_exposed": runtime_secret_exposed,
            "session_id": session_id,
        }

    invocation_value = {
        "schema_version": "claude-code-fw-f-v3-invocations/v1",
        "probe_id": probe_id,
        "response_plan": probe.response_plan,
        "invocations": invocations,
    }
    invocation_path = output_dir / "invocation.json"
    secure_write_json(invocation_path, invocation_value)
    summary.update(
        {
            "schema_version": "claude-code-fw-f-v3-probe-result/v1",
            "product": "claude",
            "probe_id": probe_id,
            "model": model,
            "response_plan": probe.response_plan,
            "message_request_expectation": probe.message_request_expectation,
            "injected_probe_env": expected_env,
            "invocation": {
                "path": invocation_path.name,
                "sha256": file_sha256(invocation_path),
                "bytes": invocation_path.stat().st_size,
            },
        }
    )
    secure_write_json(output_dir / "summary.json", summary)
    return summary


validate_probe_catalog()
