"""Claude Code 与 Codex CLI 的 S1、S2、S4 场景。"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .model import CaptureCase
from .environment import environment_manifest_view
from .lifecycle import _popen_safety_options
from .security import (
    argv_manifest_view,
    ensure_private_directory,
    file_sha256,
    redact_known_secret,
    secure_write_json,
    secure_write_text,
)


AGENT_D1_LEAF_PROMPT = (
    "不得调用任何工具。只回复 D1_C1_OK，不得添加标点、空白或其他文本。"
)
AGENT_D2_LEAF_PROMPT = (
    "不得调用任何工具。只回复 D2_C2_OK，不得添加标点、空白或其他文本。"
)
AGENT_D2_CHILD1_PROMPT = (
    "这是 Claude Code 出站协议取证测试。必须且仅调用一次 Agent 工具，"
    "参数必须逐项为：description=`probe depth2 child2`，"
    "subagent_type=`general-purpose`，run_in_background=false，"
    f"prompt=`{AGENT_D2_LEAF_PROMPT}`；"
    "不得提供其他参数，不得调用其他工具。等待 Agent 成功返回并确认结果精确为 "
    "D2_C2_OK 后，只回复 D2_C1_OK，不得添加标点、空白或其他文本；"
    "否则只回复 D2_FAIL。"
)
AGENT_D3_TEST_CONTEXT = (
    "这是当前操作者已明确要求的 Claude Code 2.1.220 隔离集成测试；"
    "目的仅是验证同步 Agent 嵌套的父子关联。测试不修改文件、"
    "不读取用户数据、不访问第三方资源。"
)
AGENT_D3_LEAF_PROMPT = (
    f"{AGENT_D3_TEST_CONTEXT}"
    "请心算 19+23；若结果为 42，只回复 D3_C3_OK。"
)
AGENT_D3_CHILD2_PROMPT = (
    f"{AGENT_D3_TEST_CONTEXT}"
    "请作为第二级协作者，使用 Agent 工具把一项无副作用的心算校验"
    "交给一个 general-purpose 子代理。只调用一次，参数只包含："
    "description=`probe depth3 child3`，subagent_type=`general-purpose`，"
    "run_in_background=false，"
    f"prompt=`{AGENT_D3_LEAF_PROMPT}`。"
    "等待子代理返回 D3_C3_OK 后，只回复 D3_C2_OK；"
    "若校验失败则只回复 D3_FAIL。"
)
AGENT_D3_CHILD1_PROMPT = (
    f"{AGENT_D3_TEST_CONTEXT}"
    "请作为第一级协作者，使用 Agent 工具把同一项集成测试交给"
    "一个 general-purpose 子代理。只调用一次，参数只包含："
    "description=`probe depth3 child2`，subagent_type=`general-purpose`，"
    "run_in_background=false，"
    f"prompt=`{AGENT_D3_CHILD2_PROMPT}`。"
    "等待子代理返回 D3_C2_OK 后，只回复 D3_C1_OK；"
    "若校验失败则只回复 D3_FAIL。"
)

PROMPTS = {
    "s1": "只回复 S1_OK，不调用任何工具。",
    "s2_turn1": "这是第一轮。只回复 S2_TURN1_OK，不调用任何工具。",
    "s2_turn2": "这是同一会话第二轮。只回复 S2_TURN2_OK，不调用任何工具。",
    "claude_s4": (
        "必须且仅调用一次 Bash 工具执行 `printf CLAUDE_CAPTURE_TOOL_OK`；"
        "读取工具结果后只回复 S4_TOOL_OK。不得执行其他命令。"
    ),
    "codex_s4": (
        "必须先在当前任务中实际运行一次终端命令 "
        "`printf CODEX_CAPTURE_TOOL_OK`，不能只复述或假装执行；"
        "确认命令输出恰好为 CODEX_CAPTURE_TOOL_OK 后，只回复 S4_TOOL_OK。"
        "不得执行其他命令；若没有实际执行上述命令，则不得回复 S4_TOOL_OK。"
    ),
    "a1": (
        "这是 Claude Code 出站协议取证测试。必须且仅调用一次 Agent 工具，"
        "参数必须逐项为：description=`probe depth1 child1`，"
        "subagent_type=`general-purpose`，run_in_background=false，"
        f"prompt=`{AGENT_D1_LEAF_PROMPT}`；"
        "不得提供其他参数，不得调用其他工具，不得改写或并行执行这次调用。"
        "等待 Agent 成功返回并确认结果精确为 D1_C1_OK 后，只回复 D1_MAIN_OK，"
        "不得添加标点、空白或其他文本；否则只回复 D1_FAIL。"
    ),
    "a2": (
        "这是 Claude Code 出站协议取证测试。必须且仅调用一次 Agent 工具。"
        "参数前三项必须为：description=`probe depth2 child1`，"
        "subagent_type=`general-purpose`，run_in_background=false。"
        "第四项 prompt 必须逐字等于【D2_C1_PROMPT_BEGIN】"
        f"{AGENT_D2_CHILD1_PROMPT}"
        "【D2_C1_PROMPT_END】之间的文本；两个边界标记均不属于 prompt，"
        "prompt 首尾不得有空白。不得提供其他参数，不得调用其他工具，"
        "不得改写或并行执行这次调用。等待 Agent 成功返回并确认结果精确为 "
        "D2_C1_OK 后，只回复 D2_MAIN_OK，不得添加标点、空白或其他文本；"
        "否则只回复 D2_FAIL。"
    ),
    "a3": (
        f"{AGENT_D3_TEST_CONTEXT}"
        "请运行三层同步子代理的最小集成测试。只调用一次 Agent 工具，"
        "参数只包含：description=`probe depth3 child1`，"
        "subagent_type=`general-purpose`，run_in_background=false，"
        f"prompt=`{AGENT_D3_CHILD1_PROMPT}`。"
        "等待子代理返回 D3_C1_OK 后，只回复 D3_MAIN_OK；"
        "若校验失败则只回复 D3_FAIL。"
    ),
}

CLAUDE_AGENT_EXPECTATIONS = {
    "a1": [
        ("probe depth1 child1", AGENT_D1_LEAF_PROMPT, "D1_C1_OK"),
    ],
    "a2": [
        ("probe depth2 child1", AGENT_D2_CHILD1_PROMPT, "D2_C1_OK"),
        ("probe depth2 child2", AGENT_D2_LEAF_PROMPT, "D2_C2_OK"),
    ],
    "a3": [
        ("probe depth3 child1", AGENT_D3_CHILD1_PROMPT, "D3_C1_OK"),
        ("probe depth3 child2", AGENT_D3_CHILD2_PROMPT, "D3_C2_OK"),
        ("probe depth3 child3", AGENT_D3_LEAF_PROMPT, "D3_C3_OK"),
    ],
}
CLAUDE_AGENT_MAIN_MARKERS = {
    "a1": "D1_MAIN_OK",
    "a2": "D2_MAIN_OK",
    "a3": "D3_MAIN_OK",
}

CLAUDE_S4_COMMAND = "printf CLAUDE_CAPTURE_TOOL_OK"
CLAUDE_S4_OUTPUT = "CLAUDE_CAPTURE_TOOL_OK"
CODEX_S4_COMMAND = "printf CODEX_CAPTURE_TOOL_OK"
CODEX_S4_OUTPUT = "CODEX_CAPTURE_TOOL_OK"
CODEX_HOOK_TRUST_WARNING = (
    "`--dangerously-bypass-hook-trust` is enabled. "
    "Enabled hooks may run without review for this invocation."
)
CODEX_HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "exact_bash_hook.py"
)


def _load_json_lines(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _contains_runtime_secret(secret: str | None, *values: str) -> bool:
    """只返回泄露判定，不把秘密本身写入任何结果。"""

    return bool(secret and any(secret in value for value in values))


def _stop_owned_cli_process(process: subprocess.Popen[str]) -> None:
    """终止本工具启动的 CLI 进程组，避免中断后遗留模型请求。"""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


# 上游对高需求模型会间歇性返回「无容量」，与画像、账号额度和客户端行为都无关：
# 同一模型同一传输在几分钟内可以一次失败一次成功。一次 official-core 要跑 6 个场景，
# 任一场景命中就让整个 attempt 作废、需要归档证据后全量重跑 17 项，因此对这一类上游
# 资源错误做有限重试。重试只认这一种错误，其余失败一律原样上报。
UPSTREAM_CAPACITY_MESSAGE = "Selected model is at capacity"
UPSTREAM_CAPACITY_RETRY_LIMIT = 4
UPSTREAM_CAPACITY_RETRY_DELAY_SECONDS = 20


def _upstream_capacity_error(events: list[dict[str, Any]]) -> bool:
    """仅识别上游临时无容量，避免把真实缺陷重试掉。"""

    return any(
        event.get("type") == "error"
        and UPSTREAM_CAPACITY_MESSAGE in str(event.get("message", ""))
        for event in events
    )


def _run_owned_cli_command(
    command: list[str], environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """运行单轮 CLI，并在超时、信号或异常时回收整个进程组。"""

    process = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **_popen_safety_options(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _stop_owned_cli_process(process)
        raise
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _nested_items_by_type(value: Any, expected: str) -> list[dict[str, Any]]:
    """递归提取指定类型的结构，供工具调用做严格校验。"""

    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("type") == expected:
            result.append(value)
        for child in value.values():
            result.extend(_nested_items_by_type(child, expected))
    elif isinstance(value, list):
        for child in value:
            result.extend(_nested_items_by_type(child, expected))
    return result


def _direct_claude_blocks(
    records: list[dict[str, Any]], expected: str
) -> list[dict[str, Any]]:
    """只读取 stream-json 顶层消息的直接 content block。"""

    items: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") not in {"assistant", "user"}:
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == expected:
                items.append(
                    {
                        "owner": record.get("parent_tool_use_id"),
                        "subagent_type": record.get("subagent_type"),
                        "task_description": record.get("task_description"),
                        "block": block,
                    }
                )
    return items


def _deduplicate_claude_blocks(
    items: list[dict[str, Any]], key_name: str
) -> tuple[list[dict[str, Any]], int, bool]:
    """按 tool ID 语义去重；同 ID 内容或 owner 冲突即标记无效。"""

    unique: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    conflict = False
    for item in items:
        block = item["block"]
        key = block.get(key_name)
        if not isinstance(key, str) or not key:
            conflict = True
            continue
        normalized = json.dumps(
            {
                "owner": item.get("owner"),
                "block": block,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if key in unique:
            duplicate_count += 1
            if unique[key]["normalized"] != normalized:
                conflict = True
            continue
        unique[key] = {**item, "normalized": normalized}
    return list(unique.values()), duplicate_count, conflict


def _last_owner_text(
    records: list[dict[str, Any]], owner: str
) -> tuple[str | None, bool]:
    """返回一个子 agent 最后的直接文本，并校验转发身份元数据。"""

    texts: list[str] = []
    metadata_valid = False
    for record in records:
        if record.get("type") != "assistant" or record.get("parent_tool_use_id") != owner:
            continue
        if record.get("subagent_type") == "general-purpose" and isinstance(
            record.get("task_description"), str
        ):
            metadata_valid = True
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()
            ):
                texts.append(block["text"].strip())
    return (texts[-1] if texts else None), metadata_valid


def _contains_standalone_success_marker(text: str | None, marker: str) -> bool:
    """允许子代理给出解释，但成功标记必须且只能独占一行。"""

    if text is None:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    failure_markers = {"D1_FAIL", "D2_FAIL", "D3_FAIL"}
    return lines.count(marker) == 1 and not failure_markers.intersection(lines)


def _validate_claude_agent_chain(
    scenario: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """按 parent_tool_use_id 严格验证同步嵌套 Agent 链。"""

    raw_uses = _direct_claude_blocks(records, "tool_use")
    raw_results = _direct_claude_blocks(records, "tool_result")
    uses, use_duplicates, use_conflict = _deduplicate_claude_blocks(
        raw_uses, "id"
    )
    results, result_duplicates, result_conflict = _deduplicate_claude_blocks(
        raw_results, "tool_use_id"
    )
    expected = CLAUDE_AGENT_EXPECTATIONS[scenario]
    by_owner: dict[Any, list[dict[str, Any]]] = {}
    for item in uses:
        by_owner.setdefault(item.get("owner"), []).append(item)
    result_by_id = {
        item["block"]["tool_use_id"]: item
        for item in results
        if isinstance(item["block"].get("tool_use_id"), str)
    }
    ordered: list[dict[str, Any]] = []
    owner: str | None = None
    chain_valid = True
    child_markers: list[dict[str, Any]] = []
    for description, prompt, child_marker in expected:
        candidates = by_owner.get(owner, [])
        if len(candidates) != 1:
            chain_valid = False
            break
        item = candidates[0]
        block = item["block"]
        tool_id = block.get("id")
        tool_input = block.get("input")
        exact_input = (
            block.get("name") == "Agent"
            and isinstance(tool_id, str)
            and isinstance(tool_input, dict)
            and set(tool_input) == {
                "description",
                "prompt",
                "run_in_background",
                "subagent_type",
            }
            and tool_input.get("description") == description
            and tool_input.get("prompt") == prompt
            and tool_input.get("run_in_background") is False
            and tool_input.get("subagent_type") == "general-purpose"
        )
        result = result_by_id.get(str(tool_id))
        result_valid = (
            result is not None
            and result.get("owner") == owner
            and result["block"].get("is_error") is not True
        )
        child_text, child_metadata_present = _last_owner_text(records, str(tool_id))
        child_records = [
            record
            for record in records
            if record.get("parent_tool_use_id") == tool_id
            and record.get("type") in {"assistant", "user"}
        ]
        child_metadata_valid = bool(child_records) and all(
            record.get("subagent_type") == "general-purpose"
            and record.get("task_description") == description
            for record in child_records
        )
        child_marker_exact = child_text == child_marker
        child_marker_valid = _contains_standalone_success_marker(
            child_text, child_marker
        )
        chain_valid = chain_valid and exact_input and result_valid
        chain_valid = (
            chain_valid
            and child_metadata_present
            and child_metadata_valid
            and child_marker_valid
        )
        ordered.append(
            {
                "tool_use_id": tool_id,
                "owner": owner,
                "description": description,
                "exact_input": exact_input,
                "paired_result": result_valid,
                "child_metadata_valid": child_metadata_valid,
                "child_marker_present": child_marker_valid,
                "child_marker_exact": child_marker_exact,
            }
        )
        child_markers.append(
            {"description": description, "marker_present": child_marker_valid}
        )
        owner = str(tool_id)
    expected_ids = {str(item["tool_use_id"]) for item in ordered}
    paired_ids = set(result_by_id)
    shape_valid = (
        len(uses) == len(expected)
        and len(results) == len(expected)
        and paired_ids == expected_ids
        and all(item["block"].get("name") == "Agent" for item in uses)
    )
    return {
        "tool_use_count": len(uses),
        "tool_result_count": len(results),
        "tool_use_raw_count": len(raw_uses),
        "tool_result_raw_count": len(raw_results),
        "tool_use_duplicate_count": use_duplicates,
        "tool_result_duplicate_count": result_duplicates,
        "tool_block_conflict": use_conflict or result_conflict,
        "agent_chain": ordered,
        "child_markers": child_markers,
        "agent_chain_valid": (
            chain_valid
            and shape_valid
            and not use_conflict
            and not result_conflict
        ),
    }


def build_claude_command(
    *, claude_bin: str, model: str, scenario: str
) -> list[str]:
    """构造 Claude 命令；认证和代理只通过子进程环境提供。"""

    command = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--prompt-suggestions",
        "false",
        "--no-session-persistence",
        "--max-budget-usd",
        (
            "2.00"
            if scenario == "a3"
            else "1.00" if scenario in CLAUDE_AGENT_EXPECTATIONS else "0.25"
        ),
    ]
    if scenario == "s4":
        command.extend(
            [
                "--tools",
                "Bash",
                "--allowedTools",
                f"Bash({CLAUDE_S4_COMMAND})",
                "--permission-mode",
                "dontAsk",
            ]
        )
    elif scenario in CLAUDE_AGENT_EXPECTATIONS:
        # Task 是 CLI 的兼容名；发往 Anthropic 的实际工具 schema 名为 Agent。
        command.extend(
            [
                "--tools",
                "Task",
                "--allowedTools",
                "Task",
                "--permission-mode",
                "dontAsk",
            ]
        )
    else:
        command.extend(["--tools", ""])
    return command


def _pump_claude_stdout(
    stream: Any, line_queue: queue.Queue[str | None]
) -> None:
    """由唯一线程持续读取 TextIO，避免 select 看不到其内部预取数据。"""

    try:
        for line in stream:
            line_queue.put(line)
    except (OSError, ValueError):
        pass
    finally:
        line_queue.put(None)


def _pump_claude_stderr(stream: Any, records: list[str]) -> None:
    """并行排空 stderr，避免大量诊断输出填满管道阻塞 CLI。"""

    try:
        records.extend(iter(stream.readline, ""))
    except (OSError, ValueError):
        pass


def _read_claude_until_result(
    line_queue: queue.Queue[str | None], records: list[str], deadline: float
) -> None:
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            line = line_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:
            raise RuntimeError("Claude stdout 在当前轮次结果前关闭。")
        records.append(line)
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("type") == "result":
            return
    raise TimeoutError("等待 Claude 当前轮次结果超时。")


def _run_claude_two_turns(
    command: list[str], environment: dict[str, str], timeout: int
) -> tuple[int, str, str]:
    process = subprocess.Popen(
        command,
        env=environment,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        **_popen_safety_options(),
    )
    output_lines: list[str] = []
    stderr_lines: list[str] = []
    stdout_queue: queue.Queue[str | None] = queue.Queue()
    if process.stdout is None or process.stderr is None:
        _stop_owned_cli_process(process)
        raise RuntimeError("Claude stdout/stderr 不可用。")
    stdout_thread = threading.Thread(
        target=_pump_claude_stdout,
        args=(process.stdout, stdout_queue),
        name="claude-capture-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_claude_stderr,
        args=(process.stderr, stderr_lines),
        name="claude-capture-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + timeout
    try:
        if process.stdin is None:
            raise RuntimeError("Claude stdin 不可用。")
        for prompt_key in ("s2_turn1", "s2_turn2"):
            message = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": PROMPTS[prompt_key]}],
                },
            }
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
            _read_claude_until_result(stdout_queue, output_lines, deadline)
        process.stdin.close()
        return_code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise RuntimeError("Claude 输出读取线程未能结束。")
        while not stdout_queue.empty():
            line = stdout_queue.get_nowait()
            if line is not None:
                output_lines.append(line)
        stdout = "".join(output_lines)
        stderr = "".join(stderr_lines)
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        return return_code, stdout, stderr
    except BaseException:
        _stop_owned_cli_process(process)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        raise


def _tool_result_text(tool_result: dict[str, Any]) -> str | None:
    """提取 Claude Bash 工具的纯文本结果，拒绝混合或结构异常的结果。"""

    content = tool_result.get("content")
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        return content[0]["text"]
    return None


def _validate_claude(
    scenario: str,
    return_code: int,
    stdout: str,
    *,
    runtime_secret_exposed: bool = False,
) -> dict[str, Any]:
    records = _load_json_lines(stdout)
    results = [record for record in records if record.get("type") == "result"]
    expected_results = 2 if scenario == "s2" else 1
    if scenario == "s2":
        markers = ["S2_TURN1_OK", "S2_TURN2_OK"]
    elif scenario == "s4":
        markers = ["S4_TOOL_OK"]
    elif scenario in CLAUDE_AGENT_MAIN_MARKERS:
        markers = [CLAUDE_AGENT_MAIN_MARKERS[scenario]]
    else:
        markers = ["S1_OK"]
    result_values = [str(record.get("result", "")).strip() for record in results]
    raw_tool_uses = _direct_claude_blocks(records, "tool_use")
    raw_tool_results = _direct_claude_blocks(records, "tool_result")
    tool_uses, tool_use_duplicates, tool_use_conflict = _deduplicate_claude_blocks(
        raw_tool_uses, "id"
    )
    tool_results, tool_result_duplicates, tool_result_conflict = (
        _deduplicate_claude_blocks(raw_tool_results, "tool_use_id")
    )
    tool_use_blocks = [item["block"] for item in tool_uses]
    tool_result_blocks = [item["block"] for item in tool_results]
    exact_tool_command = (
        len(tool_use_blocks) == 1
        and tool_use_blocks[0].get("name") == "Bash"
        and isinstance(tool_use_blocks[0].get("input"), dict)
        and tool_use_blocks[0]["input"].get("command") == CLAUDE_S4_COMMAND
    )
    exact_tool_output = (
        len(tool_result_blocks) == 1
        and _tool_result_text(tool_result_blocks[0]) == CLAUDE_S4_OUTPUT
        and tool_result_blocks[0].get("is_error") is False
    )
    agent_summary = (
        _validate_claude_agent_chain(scenario, records)
        if scenario in CLAUDE_AGENT_EXPECTATIONS
        else None
    )
    summary: dict[str, Any] = {
        "return_code": return_code,
        "result_count": len(results),
        "success_result_count": sum(
            record.get("subtype") == "success" for record in results
        ),
        "tool_use_count": len(tool_use_blocks),
        "tool_result_count": len(tool_result_blocks),
        "tool_use_raw_count": len(raw_tool_uses),
        "tool_result_raw_count": len(raw_tool_results),
        "tool_use_duplicate_count": tool_use_duplicates,
        "tool_result_duplicate_count": tool_result_duplicates,
        "tool_block_conflict": tool_use_conflict or tool_result_conflict,
        "exact_tool_command": exact_tool_command,
        "exact_tool_output": exact_tool_output,
        "markers_present": result_values == markers,
        "runtime_secret_exposed": runtime_secret_exposed,
    }
    if agent_summary is not None:
        summary.update(agent_summary)
    summary["valid"] = (
        return_code == 0
        and summary["result_count"] == expected_results
        and summary["success_result_count"] == expected_results
        and summary["markers_present"]
        and not runtime_secret_exposed
        and not summary["tool_block_conflict"]
        and (
            summary["agent_chain_valid"]
            if scenario in CLAUDE_AGENT_EXPECTATIONS
            else (
                summary["tool_use_count"] == 0
                and summary["tool_result_count"] == 0
            )
            if scenario != "s4"
            else exact_tool_command and exact_tool_output
        )
    )
    return summary


def run_claude_scenario(
    *,
    claude_bin: str,
    model: str,
    scenario: str,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    runtime_secret: str | None,
    known_secrets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行并校验一个 Claude 场景。"""

    ensure_private_directory(output_dir)
    command = build_claude_command(
        claude_bin=claude_bin, model=model, scenario=scenario
    )
    if scenario == "s2":
        command.extend(
            [
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
            ]
        )
    else:
        if scenario in CLAUDE_AGENT_EXPECTATIONS:
            command.append("--forward-subagent-text")
        prompt = PROMPTS["claude_s4"] if scenario == "s4" else PROMPTS[scenario]
        command.extend(["--output-format", "stream-json", "--verbose", prompt])
    secret_values = dict(known_secrets or {})
    if runtime_secret and not secret_values:
        secret_values["runtime_secret"] = runtime_secret
    invocation = argv_manifest_view(command, secret_values)
    invocation.update(
        {
            "cwd": os.getcwd(),
            "environment": environment_manifest_view(environment, secret_values),
            "stdin_mode": "stream-json-two-turns" if scenario == "s2" else "devnull",
        }
    )
    invocation_path = output_dir / "invocation.json"
    secure_write_json(invocation_path, invocation)
    invocation["file_sha256"] = file_sha256(invocation_path)

    if scenario == "s2":
        return_code, stdout, stderr = _run_claude_two_turns(
            command, environment, timeout
        )
    else:
        completed = _run_owned_cli_command(command, environment, timeout)
        return_code, stdout, stderr = (
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    runtime_secret_exposed = _contains_runtime_secret(
        runtime_secret, stdout, stderr
    )
    safe_stdout = redact_known_secret(stdout, runtime_secret)
    safe_stderr = redact_known_secret(stderr, runtime_secret)
    secure_write_text(output_dir / "stdout.jsonl", safe_stdout)
    secure_write_text(output_dir / "stderr.log", safe_stderr)
    summary = _validate_claude(
        scenario,
        return_code,
        safe_stdout,
        runtime_secret_exposed=runtime_secret_exposed,
    )
    summary.update(
        {
            "product": "claude",
            "scenario": scenario,
            "model": model,
            "invocation": invocation,
        }
    )
    secure_write_json(output_dir / "summary.json", summary)
    return summary


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def codex_provider_values(
    *, case: CaptureCase, api_key_env: str, codex_version: str = "0.145.0"
) -> tuple[str, ...]:
    """返回 OAuth/API 与 HTTP/WS 对应的 Codex provider 覆盖。"""

    if case.product != "codex":
        raise ValueError("Codex provider 只能用于 Codex case。")
    if case.task == "oauth" and case.transport == "ws":
        # WS 基准直接使用内置 OpenAI provider。
        return ()
    if case.task == "oauth":
        # 内置 openai provider 是保留 ID，不能覆盖 supports_websockets。HTTP
        # 基准使用独立 ID，但 name、base_url、认证和 version Header 必须逐项
        # 对齐 Codex 0.145.0 的 create_openai_provider；name="OpenAI" 还决定
        # 官方 zstd 压缩路径。唯一差异是关闭 WebSocket 支持。
        provider = "official_openai_http"
        return (
            f"model_provider={_toml_string(provider)}",
            f'model_providers.{provider}.name="OpenAI"',
            (
                f"model_providers.{provider}.base_url="
                '"https://chatgpt.com/backend-api/codex"'
            ),
            f'model_providers.{provider}.wire_api="responses"',
            f"model_providers.{provider}.requires_openai_auth=true",
            f"model_providers.{provider}.supports_websockets=false",
            (
                f"model_providers.{provider}.http_headers.version="
                f"{_toml_string(codex_version)}"
            ),
        )

    provider = f"sub2api_capture_{case.transport}"
    websocket_enabled = case.transport == "ws"
    return (
        f"model_provider={_toml_string(provider)}",
        f"model_providers.{provider}.name=\"Sub2API Capture\"",
        f"model_providers.{provider}.base_url={_toml_string(str(case.base_url))}",
        f"model_providers.{provider}.env_key={_toml_string(api_key_env)}",
        f"model_providers.{provider}.wire_api=\"responses\"",
        f"model_providers.{provider}.requires_openai_auth=false",
        (
            f"model_providers.{provider}.supports_websockets="
            f"{str(websocket_enabled).lower()}"
        ),
    )


def _codex_hook_config_value(
    *, scenario: str, hook_path: Path, audit_path: Path
) -> str:
    """构造只信任仓库静态脚本的 Codex PreToolUse 配置。"""

    hook_arguments = [
        "/usr/bin/python3",
        str(hook_path),
        "--audit-file",
        str(audit_path),
    ]
    if scenario == "s4":
        hook_arguments.extend(["--expected-command", CODEX_S4_COMMAND])
    else:
        hook_arguments.append("--deny-all")
    hook_command = shlex.join(hook_arguments)
    return (
        "hooks.PreToolUse=[{matcher=\"^Bash$\",hooks=[{type=\"command\","
        f"command={_toml_string(hook_command)},timeout=5"
        "}]}]"
    )


def _codex_config_args(
    case: CaptureCase,
    api_key_env: str,
    *,
    scenario: str,
    hook_path: Path,
    hook_audit_path: Path,
    codex_version: str = "0.145.0",
) -> list[str]:
    # --ignore-user-config 会同时忽略 API 专用状态目录中的 config.toml，
    # 因此抓包所需的隐私配置必须在每次首轮和续轮命令中显式覆盖。
    privacy_values = (
        "check_for_update_on_startup=false",
        "analytics.enabled=false",
        "feedback.enabled=false",
        'otel.exporter="none"',
        "otel.log_user_prompt=false",
        # 插件/MCP 是官方原生支持关闭的功能流量（Feature::Plugins 为 Stable，
        # 默认开启但可配置关闭）。基准若保留它，官方样本里会多出
        # plugins/featured、ps/plugins/*、ps/mcp 等请求，并因此提前拿到
        # Cloudflare Cookie，与只发模型请求的候选出站不可比。关闭后两侧
        # 才落在同一条最小必要流量基线上。
        "features.plugins=false",
        "features.hooks=true",
        'approval_policy="never"',
        "allow_login_shell=false",
        'shell_environment_policy.inherit="none"',
        (
            'shell_environment_policy.set={PATH="/usr/bin:/bin",'
            'LANG="C.UTF-8",LC_ALL="C.UTF-8"}'
        ),
        'default_permissions="capture-tool"',
        'permissions.capture-tool.extends=":read-only"',
        'permissions.capture-tool.filesystem={"/root"="deny","/capture"="deny"}',
        _codex_hook_config_value(
            scenario=scenario,
            hook_path=hook_path,
            audit_path=hook_audit_path,
        ),
    )
    arguments: list[str] = []
    for value in (
        *privacy_values,
        *codex_provider_values(
            case=case,
            api_key_env=api_key_env,
            codex_version=codex_version,
        ),
    ):
        arguments.extend(["-c", value])
    return arguments


def build_codex_command(
    *,
    codex_bin: str,
    model: str,
    case: CaptureCase,
    api_key_env: str,
    resume: bool,
    scenario: str,
    hook_audit_path: Path,
    hook_path: Path = CODEX_HOOK_PATH,
    codex_version: str = "0.145.0",
) -> list[str]:
    """构造 Codex 首轮或续轮命令。"""

    action = ["exec", "resume"] if resume else ["exec"]
    command = [
        codex_bin,
        *action,
        "--strict-config",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--ignore-user-config",
        "--dangerously-bypass-hook-trust",
    ]
    # 续轮不支持 -C；权限使用首轮与续轮都可复现的最小权限 Profile。
    if not resume:
        command.extend(["-C", "/work"])
    command.extend(
        [
            "--json",
            "-m",
            model,
            *_codex_config_args(
                case,
                api_key_env,
                scenario=scenario,
                hook_path=hook_path,
                hook_audit_path=hook_audit_path,
                codex_version=codex_version,
            ),
        ]
    )
    return command


def build_codex_config_preflight_command(
    *,
    codex_bin: str,
    case: CaptureCase,
    api_key_env: str,
    scenario: str,
    hook_audit_path: Path,
    hook_path: Path = CODEX_HOOK_PATH,
) -> list[str]:
    """用不发模型请求的 features list 解析真实 case 配置覆盖项。"""

    return [
        codex_bin,
        *(
            _codex_config_args(
                case,
                api_key_env,
                scenario=scenario,
                hook_path=hook_path,
                hook_audit_path=hook_audit_path,
            )
        ),
        "features",
        "list",
    ]


def _extract_thread_id(events: list[dict[str, Any]]) -> str:
    for event in events:
        thread_id = event.get("thread_id")
        if event.get("type") == "thread.started" and isinstance(thread_id, str):
            return thread_id
    raise RuntimeError("Codex 输出中缺少 thread.started/thread_id。")


def _completed_command_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event["item"]
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "command_execution"
    ]


def _is_exact_codex_tool_command(value: Any) -> bool:
    """只接受固定 printf 的裸命令或等价单层 shell 包装。"""

    if not isinstance(value, str):
        return False
    expected_tokens = ["printf", CODEX_S4_OUTPUT]
    try:
        outer_tokens = shlex.split(value, posix=True)
    except ValueError:
        return False
    if outer_tokens == expected_tokens:
        return True
    if (
        len(outer_tokens) != 3
        or outer_tokens[0]
        not in {"/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh"}
        or outer_tokens[1] not in {"-c", "-lc"}
    ):
        return False
    try:
        return shlex.split(outer_tokens[2], posix=True) == expected_tokens
    except ValueError:
        return False


def _load_codex_hook_audit(path: Path) -> tuple[list[dict[str, bool]], bool]:
    """读取不含命令正文的 hook 审计记录；格式异常时失败关闭。"""

    if not path.exists():
        return [], True
    records: list[dict[str, bool]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("allowed"), bool):
                return records, False
            records.append({"allowed": value["allowed"]})
    except (OSError, ValueError):
        return records, False
    return records, True


def _validate_codex(
    *,
    scenario: str,
    turns: list[dict[str, Any]],
    thread_id: str,
    hook_audit: list[dict[str, bool]],
    hook_audit_valid: bool,
    runtime_secret_exposed: bool,
) -> dict[str, Any]:
    """严格验证 Codex 最终答复、工具命令、输出和本地门禁审计。"""

    expected_markers = (
        ["S2_TURN1_OK", "S2_TURN2_OK"]
        if scenario == "s2"
        else ["S4_TOOL_OK" if scenario == "s4" else "S1_OK"]
    )
    all_events = [event for turn in turns for event in turn["events"]]
    command_items = _completed_command_items(all_events)
    command_count = len(command_items)
    exact_tool_command = len(command_items) == 1 and _is_exact_codex_tool_command(
        command_items[0].get("command")
    )
    exact_tool_output = (
        len(command_items) == 1
        and command_items[0].get("aggregated_output") == CODEX_S4_OUTPUT
        and command_items[0].get("exit_code") == 0
        and command_items[0].get("status") == "completed"
    )
    markers_present = len(turns) == len(expected_markers) and all(
        turns[index]["last_message"].strip() == marker
        for index, marker in enumerate(expected_markers)
    )
    hook_allowed_count = sum(record["allowed"] for record in hook_audit)
    hook_denied_count = len(hook_audit) - hook_allowed_count
    hook_shape_valid = (
        hook_audit_valid
        and (
            hook_allowed_count == 1 and hook_denied_count == 0
            if scenario == "s4"
            else hook_allowed_count == 0 and hook_denied_count == 0
        )
    )
    tool_item_types = {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "collab_tool_call",
        "web_search",
    }
    unexpected_tool_item_count = sum(
        event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") in tool_item_types - {"command_execution"}
        for event in all_events
    )

    def is_error_event(event: dict[str, Any]) -> bool:
        return event.get("type") == "error" or (
            event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "error"
        )

    def is_hook_trust_warning(event: dict[str, Any]) -> bool:
        item = event.get("item")
        return (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "error"
            and item.get("message") == CODEX_HOOK_TRUST_WARNING
        )

    hook_trust_warning_count = sum(
        is_hook_trust_warning(event) for event in all_events
    )
    error_event_count = sum(
        is_error_event(event) and not is_hook_trust_warning(event)
        for event in all_events
    )
    summary: dict[str, Any] = {
        "turn_count": len(turns),
        "return_codes": [turn["return_code"] for turn in turns],
        "markers_present": markers_present,
        "command_execution_count": command_count,
        "exact_tool_command": exact_tool_command,
        "exact_tool_output": exact_tool_output,
        "hook_audit_valid": hook_audit_valid,
        "hook_allowed_count": hook_allowed_count,
        "hook_denied_count": hook_denied_count,
        "unexpected_tool_item_count": unexpected_tool_item_count,
        "hook_trust_warning_count": hook_trust_warning_count,
        "error_event_count": error_event_count,
        "thread_id_present": bool(thread_id),
        "runtime_secret_exposed": runtime_secret_exposed,
    }
    summary["valid"] = (
        len(turns) == len(expected_markers)
        and all(turn["return_code"] == 0 for turn in turns)
        and markers_present
        and hook_shape_valid
        and unexpected_tool_item_count == 0
        and error_event_count == 0
        and not runtime_secret_exposed
        and (
            command_count == 0
            if scenario != "s4"
            else exact_tool_command and exact_tool_output
        )
    )
    return summary


def run_codex_scenario(
    *,
    codex_bin: str,
    model: str,
    case: CaptureCase,
    scenario: str,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    runtime_secret: str | None,
    api_key_env: str,
    codex_version: str = "0.145.0",
) -> dict[str, Any]:
    """执行并校验一个 Codex 场景。"""

    ensure_private_directory(output_dir)
    prompts = (
        [PROMPTS["s2_turn1"], PROMPTS["s2_turn2"]]
        if scenario == "s2"
        else [PROMPTS["codex_s4"] if scenario == "s4" else PROMPTS[scenario]]
    )
    turns: list[dict[str, Any]] = []
    thread_id = ""
    hook_audit_path = output_dir / "codex-hook-audit.jsonl"
    for index, prompt in enumerate(prompts, start=1):
        last_message_path = output_dir / f"turn{index}-last-message.txt"
        command = build_codex_command(
            codex_bin=codex_bin,
            model=model,
            case=case,
            api_key_env=api_key_env,
            resume=index > 1,
            scenario=scenario,
            hook_audit_path=hook_audit_path,
            codex_version=codex_version,
        )
        if index == 1 and scenario != "s2":
            command.append("--ephemeral")
        command.extend(["-o", str(last_message_path)])
        if index > 1:
            command.append(thread_id)
        command.append(prompt)
        remaining_capacity_retries = UPSTREAM_CAPACITY_RETRY_LIMIT
        while True:
            completed = _run_owned_cli_command(command, environment, timeout)
            if completed.returncode == 0 or remaining_capacity_retries <= 0:
                break
            if not _upstream_capacity_error(_load_json_lines(completed.stdout)):
                break
            remaining_capacity_retries -= 1
            time.sleep(UPSTREAM_CAPACITY_RETRY_DELAY_SECONDS)
        raw_last_message = (
            last_message_path.read_text(encoding="utf-8")
            if last_message_path.exists()
            else ""
        )
        turn_secret_exposed = _contains_runtime_secret(
            runtime_secret,
            completed.stdout,
            completed.stderr,
            raw_last_message,
        )
        safe_stdout = redact_known_secret(completed.stdout, runtime_secret)
        safe_stderr = redact_known_secret(completed.stderr, runtime_secret)
        secure_write_text(output_dir / f"turn{index}-events.jsonl", safe_stdout)
        secure_write_text(output_dir / f"turn{index}-stderr.log", safe_stderr)
        events = _load_json_lines(safe_stdout)
        if index == 1 and scenario == "s2":
            thread_id = _extract_thread_id(events)
        last_message = redact_known_secret(raw_last_message, runtime_secret).strip()
        secure_write_text(last_message_path, last_message + ("\n" if last_message else ""))
        turns.append(
            {
                "return_code": completed.returncode,
                "events": events,
                "last_message": last_message,
                "runtime_secret_exposed": turn_secret_exposed,
            }
        )
        if completed.returncode != 0:
            break

    hook_audit, hook_audit_valid = _load_codex_hook_audit(hook_audit_path)
    summary = _validate_codex(
        scenario=scenario,
        turns=turns,
        thread_id=thread_id,
        hook_audit=hook_audit,
        hook_audit_valid=hook_audit_valid,
        runtime_secret_exposed=any(
            turn["runtime_secret_exposed"] for turn in turns
        ),
    )
    summary.update({"product": "codex", "scenario": scenario, "model": model})
    secure_write_json(output_dir / "summary.json", summary)
    return summary
