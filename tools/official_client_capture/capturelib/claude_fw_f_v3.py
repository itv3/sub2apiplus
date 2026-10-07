"""Claude Code 2.1.226 FW-F v3 的固定场景目录与执行驱动。

该模块只接受源码内冻结的场景身份，不接受任意 argv、任意 prompt 或任意环境变量。
真实上游场景用于观察 Header／Body／状态关系；合成故障场景仍由同一个官方二进制
发起请求，但响应来自隔离中继的白名单计划，绝不连接生产上游。

Claude 侧 TUI 驱动（2026-10-07，B2-10 启动探测在 ARM64 上用 2.1.285 实测发现）
------------------------------------------------------------------------------
v3、v4、v5 的 TUI 场景与启动探测的驱动入口（``claude_drive_tui_cli.py``）都用本模块的 ``claude_tui_drive``／
``claude_tui_drive_sequence``。共用驱动 ``drive_claude_tui.py`` 被 Codex 身份策略按 ``drive_`` 前缀计入 Codex wire 层，
改它就改了 Codex 的 wire 摘要；驱动循环放在这里（Claude wire 层，Codex 策略忽略 ``capturelib/claude_fw_``），并且本文件
已在执行源清单与各场景清单逐作业依赖的闭包里，不新增任何依赖文件。循环与共用驱动逐行相同（复用它的 ``clean``／
``flatten``／``classify_screen``），只有两处按 2.1.285 改：

* 信任框：2.1.285 的 ``TrustDialog`` 把取消项排在前面并默认聚焦（「❯ No, exit」），回车接受默认项就退出。改由
  ``TrustDialogHandler`` 按选项文字把焦点移到「Yes, I trust this folder」、核对重绘后再回车，拿不准就不回车。
* 主输入框是否可以输入：共用状态机只认页脚「? for shortcuts」。2.1.285 以 auto 为缺省启动权限模式（模型支持、设置没有
  禁用时；GrowthBook 不可用时 ``tengu_auto_mode_config`` 取缺省 enabled），页脚在非缺省模式下不再显示「? for shortcuts」，
  空闲时是「⏵⏵ auto mode on (shift+tab to cycle) · ← for agents」，忙碌时是「… · esc to interrupt · ← for agents」。
  ``classify_screen`` 把这些空闲提示也算作可以输入，紧跟在「esc to interrupt」后面的同一段忙碌页脚除外。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pty
import re
import select
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import tools.official_client_capture.drive_claude_tui as drive_claude_tui

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


# ---------------------------------------------------------------------------
# 信任框：按选项文字定位确认项（取值见 2.1.226／2.1.285 冻结二进制里 TrustDialog 与选择组件的源码）
# ---------------------------------------------------------------------------

# 信任框的识别文案（去空白后匹配）：2.1.226／2.1.285 的确认项文字，与更早版本的标题。
TRUST_SCREEN_MARKERS = ("Itrustthisfolder", "Doyoutrustthefilesinthisfolder")
# 一次完整渲染的起点（去空白后匹配）：只解析最后一次渲染，避免把旧帧的指针当成当前焦点。
TRUST_HEADER_MARKERS = ("Accessingworkspace:", "Quicksafetycheck", "Doyoutrustthefilesinthisfolder")
# 确认项与取消项的选项文字。取消项在 2.1.285 有两种（项目带预批准权限时是「继续但不带这些权限」）。
TRUST_CONFIRM_LABELS = ("Yes, I trust this folder", "Yes, proceed")
TRUST_CANCEL_LABELS = ("No, exit", "No, continue without these permissions")
# 选择组件的按键（两个版本的默认键位里 Select 上下文都有 home→select:first、end→select:last、down→select:next，
# 终端序列 ESC[H／ESC[F／ESC[B 由客户端的按键解析表识别）。Home／End 是幂等的：重发不会像 Down 那样绕回取消项。
KEY_HOME = b"\x1b[H"
KEY_END = b"\x1b[F"
KEY_DOWN = b"\x1b[B"
KEY_ENTER = b"\r"
# 信任框首次出现后等它画完、选择组件的按键处理器注册完再发键；移动焦点后等客户端重绘再核对。
TRUST_SETTLE_SECONDS = 1.0
TRUST_VERIFY_SECONDS = 1.0
# 移动焦点最多重试的次数；同一会话里信任框最多确认的次数（再出现说明确认没有生效，不再回车）。
TRUST_MAX_MOVES = 3
TRUST_MAX_CONFIRMS = 2
POINTER = "❯"
_TRUST_LABELS = tuple((label, drive_claude_tui.flatten(label)) for label in TRUST_CONFIRM_LABELS + TRUST_CANCEL_LABELS)
_OPTION_INDEX_RE = re.compile(r"[0-9]+\.")


def trust_dialog_options(flat: str) -> tuple[list[str], str | None]:
    """在去空白的渲染文本里按出现顺序列出已知选项，并找出紧跟指针 ``❯``（可带「1.」一类编号）的焦点项。

    指针后没有紧跟已知选项（例如客户端只重绘了指针所在的格子）时焦点记为 None。
    """

    found = sorted((flat.find(needle), label) for label, needle in _TRUST_LABELS if needle in flat)
    options = [label for _, label in found]
    focused: str | None = None
    for match in re.finditer(POINTER, flat):
        rest = flat[match.end():]
        index = _OPTION_INDEX_RE.match(rest)
        if index is not None:
            rest = rest[index.end():]
        for label, needle in _TRUST_LABELS:
            if rest.startswith(needle):
                focused = label
                break
    return options, focused


def trust_selection(view: str) -> dict[str, Any] | None:
    """把一轮渲染解析成信任框的处理计划；不是信任框时返回 None。

    计划的 ``action``：``wait`` 渲染还不完整（确认项与取消项没有同时出现），不发任何键；``confirm`` 焦点已在确认项上，
    直接回车；``move`` 先发 ``keys`` 把焦点移到确认项（确认项在末位用 End、在首位用 Home、在中间用 Home 加若干 Down，
    都与当前焦点无关），核对后再回车。
    """

    flat = drive_claude_tui.flatten(view)
    if not any(marker in flat for marker in TRUST_SCREEN_MARKERS):
        return None
    start = max(flat.rfind(marker) for marker in TRUST_HEADER_MARKERS)
    options, focused = trust_dialog_options(flat[start:] if start >= 0 else flat)
    target = next((label for label in options if label in TRUST_CONFIRM_LABELS), None)
    if target is None or not any(label in TRUST_CANCEL_LABELS for label in options):
        return {"action": "wait"}
    if focused == target:
        return {"action": "confirm", "target": target, "keys": b"", "options": options, "focused": focused}
    index = options.index(target)
    if index == len(options) - 1:
        keys = KEY_END
    elif index == 0:
        keys = KEY_HOME
    else:
        keys = KEY_HOME + KEY_DOWN * index
    return {"action": "move", "target": target, "keys": keys, "options": options, "focused": focused}


class TrustDialogHandler:
    """信任框状态机：等渲染稳定 → 按文字把焦点移到确认项 → 等重绘核对 → 回车；拿不准就不回车。

    ``step`` 每轮由驱动循环在屏幕归为菜单时调用：不是信任框返回 None（驱动照共用状态机处理）；是信任框返回 ``pending``
    （等待中，本轮不发别的键）、``confirmed``（刚回车确认）或 ``unresolved``（放弃：此后信任框上一个键都不发）。
    """

    def __init__(self, write: Callable[[int, bytes], Any] = os.write, clock: Callable[[], float] = time.time) -> None:
        self._write = write
        self._clock = clock
        self.seen_at: float | None = None
        self.moved_at: float | None = None
        self.mark = 0
        self.target: str | None = None
        self.keys = b""
        self.moves = 0
        self.confirms = 0
        self.unresolved = False

    def step(self, fd: int, view: str, transcript: bytearray, push: Callable[[str], None]) -> str | None:
        plan = trust_selection(view)
        if plan is None:
            return None
        if self.unresolved:
            return "unresolved"
        if self.confirms >= TRUST_MAX_CONFIRMS:
            return self._give_up(push)
        now = self._clock()
        if self.seen_at is None:
            self.seen_at = now
        if self.moved_at is None:
            if plan["action"] == "wait" or now - self.seen_at < TRUST_SETTLE_SECONDS:
                return "pending"
            if plan["action"] == "confirm":
                return self._confirm(fd, push)
            self.target, self.keys = plan["target"], plan["keys"]
            return self._move(fd, transcript, push)
        if now - self.moved_at < TRUST_VERIFY_SECONDS:
            return "pending"
        # 只看移动之后的新输出：客户端按格子差分重绘，旧帧里的指针仍停在原焦点上。
        redrawn = drive_claude_tui.clean(bytes(transcript[self.mark:]))
        _, focused = trust_dialog_options(drive_claude_tui.flatten(redrawn))
        if POINTER in redrawn and focused in {None, self.target}:
            return self._confirm(fd, push)
        if self.moves >= TRUST_MAX_MOVES:
            return self._give_up(push)
        push("trust-focus-unverified")
        return self._move(fd, transcript, push)

    def _move(self, fd: int, transcript: bytearray, push: Callable[[str], None]) -> str:
        self.mark = len(transcript)
        self._write(fd, self.keys)
        self.moved_at = self._clock()
        self.moves += 1
        push("trust-focus-confirm-option")
        return "pending"

    def _confirm(self, fd: int, push: Callable[[str], None]) -> str:
        self._write(fd, KEY_ENTER)
        push("enter-menu")
        self.confirms += 1
        self.seen_at = self.moved_at = self.target = None
        self.keys, self.mark, self.moves = b"", 0, 0
        return "confirmed"

    def _give_up(self, push: Callable[[str], None]) -> str:
        self.unresolved = True
        push("trust-dialog-unresolved")
        return "unresolved"


# ---------------------------------------------------------------------------
# 主输入框是否可以输入（取值见 2.1.226／2.1.285 冻结二进制里主输入框页脚组件的源码）
# ---------------------------------------------------------------------------

# 主输入框页脚里表示「空闲、可以输入」的提示（去空白后匹配）：
# * 「? for shortcuts」：2.1.226 的空闲页脚，2.1.285 在缺省权限模式下仍显示；
# * 「← for agents」与「(shift+tab to cycle)」：2.1.285 在 auto 等非缺省权限模式下省略「? for shortcuts」（页脚组件里
#   ``!(Lr&&js&&ys)`` 才推入它），空闲页脚是「⏵⏵ auto mode on (shift+tab to cycle) · ← for agents」。
IDLE_FOOTER_MARKERS = ("?forshortcuts", "←foragents", "(shift+tabtocycle)")
# 忙碌提示：两个版本都是「esc to interrupt」。
BUSY_FOOTER_MARKER = "esctointerrupt"
# 忙碌页脚是「… · esc to interrupt · ← for agents」（页脚组件先推入忙碌提示与有后台任务时的「ctrl+t to show/hide tasks」，
# 再推入「← for agents」）：紧跟在忙碌提示后面的这两项属于同一次忙碌渲染，其中的「← for agents」不算空闲。只认这两项，
# 不按「·」往后延伸（扁平化后「● high · /effort」等别处的「·」会把无关文字连进来）。
_BUSY_FOOTER_RUN_RE = re.compile(r"esctointerrupt(?:·ctrl\+tto(?:show|hide)tasks)?(?:·←foragents)?")


def classify_screen(text: str) -> str:
    """Claude 侧屏幕状态：登录门禁与引导菜单照共用状态机判；主输入框的空闲／忙碌按两个版本的页脚判。

    与共用状态机同一口径：以最后一次页脚提示为当前状态（客户端按格子差分重绘，旧提示仍留在累积字节里），并要求本轮
    流里出现过主输入指针 ``❯``。区别只在空闲提示多认 2.1.285 的两种，且忙碌页脚自带的「← for agents」不算空闲。
    """

    state = drive_claude_tui.classify_screen(text)
    if state in {"auth_gate", "menu"}:
        return state
    view = drive_claude_tui.flatten(text)
    busy_runs = [match.span() for match in _BUSY_FOOTER_RUN_RE.finditer(view)]
    idle_index = -1
    for marker in IDLE_FOOTER_MARKERS:
        start = view.find(marker)
        while start >= 0:
            if not any(begin <= start < end for begin, end in busy_runs):
                idle_index = max(idle_index, start)
            start = view.find(marker, start + 1)
    busy_index = view.rfind(BUSY_FOOTER_MARKER)
    if POINTER in view and idle_index >= 0 and idle_index > busy_index:
        return "idle_prompt"
    if busy_index >= 0 and busy_index > idle_index:
        return "busy"
    return "waiting"


# ---------------------------------------------------------------------------
# Claude 侧驱动循环：与 drive_claude_tui.drive／drive_sequence 逐行相同，只多信任框一支、主输入框按本模块的
# classify_screen 判定
# ---------------------------------------------------------------------------


def _stop(fd: int, pid: int) -> None:
    """会话收尾：与共用状态机相同（Ctrl-C、SIGINT、SIGKILL、非阻塞回收）。"""

    os.write(fd, b"\x03")
    time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGINT)
        time.sleep(0.8)
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except Exception:
        pass


def claude_tui_drive(
    claude: str,
    model: str,
    prompt: str,
    env: dict,
    total_timeout: int = 75,
    quiet_after_prompt: int = 20,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """单次提交：``drive_claude_tui.drive`` 的同一状态机，信任框改由 ``TrustDialogHandler`` 按选项文字确认。"""

    pid, fd = pty.fork()
    if pid == 0:
        os.execve(claude, [claude, "--model", model, *extra_args], env)
    os.set_blocking(fd, False)

    transcript = bytearray()
    start = time.time()
    deadline = start + total_timeout
    steps: list[tuple[float, str]] = []
    sent_prompt = False
    prompt_at = None
    last_menu = 0.0
    scan_from = 0
    auth_gate_detected = False
    trust = TrustDialogHandler()

    def push(tag):
        steps.append((round(time.time() - start, 1), tag))

    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
        scan_start = max(scan_from, len(transcript) - 8000)
        view = drive_claude_tui.clean(bytes(transcript[scan_start:]))
        screen_state = classify_screen(view)

        if screen_state == "auth_gate":
            auth_gate_detected = True
            push("auth-gate-detected")
            break

        if sent_prompt:
            if prompt_at and time.time() - prompt_at > quiet_after_prompt:
                push("response-window-done")
                break
            continue

        if screen_state == "menu":
            # 信任框只走这一支，绝不落到下面「回车接受默认高亮项」。
            outcome = trust.step(fd, view, transcript, push)
            if outcome == "confirmed":
                last_menu = time.time()
                scan_from = len(transcript)
                time.sleep(1.5)
                continue
            if outcome is not None:
                continue

        # 引导菜单：回车接受默认高亮项，限速避免连续误发
        if screen_state == "menu" and time.time() - last_menu > 2.0:
            os.write(fd, b"\r")
            last_menu = time.time()
            # 后续只扫描本次操作之后的新渲染，避免旧菜单仍留在 transcript
            # 时每两秒重复发送回车。
            scan_from = len(transcript)
            push("enter-menu")
            time.sleep(1.5)
            continue

        if screen_state == "idle_prompt" and time.time() - start > 3:
            os.write(fd, prompt.encode())
            time.sleep(0.4)
            os.write(fd, b"\r")
            sent_prompt = True
            prompt_at = time.time()
            push("sent-prompt")
            continue

    _stop(fd, pid)
    return {
        "steps": steps,
        "sent_prompt": sent_prompt,
        "auth_gate_detected": auth_gate_detected,
        "transcript": drive_claude_tui.clean(bytes(transcript)),
    }


def claude_tui_drive_sequence(
    claude: str,
    model: str,
    inputs: tuple[str, ...],
    env: dict,
    total_timeout: int = 150,
    quiet_after_last_input: int = 25,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """同一会话逐项提交：``drive_claude_tui.drive_sequence`` 的同一状态机，信任框改由 ``TrustDialogHandler`` 确认。"""

    if not inputs or any(not isinstance(value, str) or not value for value in inputs):
        raise ValueError("TUI 固定输入序列不能为空。")
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(claude, [claude, "--model", model, *extra_args], env)
    os.set_blocking(fd, False)
    transcript = bytearray()
    start = time.time()
    deadline = start + total_timeout
    steps: list[tuple[float, str]] = []
    sent: list[str] = []
    last_input_at: float | None = None
    last_menu = 0.0
    scan_from = 0
    auth_gate_detected = False
    trust = TrustDialogHandler()

    def push(tag):
        steps.append((round(time.time() - start, 1), tag))

    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.5)
        if readable:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
        scan_start = max(scan_from, len(transcript) - 12000)
        view = drive_claude_tui.clean(bytes(transcript[scan_start:]))
        screen_state = classify_screen(view)
        if screen_state == "auth_gate":
            auth_gate_detected = True
            push("auth-gate-detected")
            break
        if len(sent) == len(inputs):
            if last_input_at and time.time() - last_input_at > quiet_after_last_input:
                push("response-window-done")
                break
            continue
        if screen_state == "menu":
            outcome = trust.step(fd, view, transcript, push)
            if outcome == "confirmed":
                last_menu = time.time()
                scan_from = len(transcript)
                time.sleep(1.5)
                continue
            if outcome is not None:
                continue
        if screen_state == "menu" and time.time() - last_menu > 2.0:
            os.write(fd, b"\r")
            last_menu = time.time()
            scan_from = len(transcript)
            push("enter-menu")
            time.sleep(1.5)
            continue
        if screen_state == "idle_prompt" and time.time() - start > 3:
            value = inputs[len(sent)]
            os.write(fd, value.encode())
            time.sleep(0.4)
            os.write(fd, b"\r")
            sent.append(value)
            last_input_at = time.time()
            scan_from = len(transcript)
            push(f"sent-input-{len(sent)}")
            # 下一项必须等待本次提交之后重新渲染输入框，不能复用旧光标。
            time.sleep(1.0)

    _stop(fd, pid)
    return {
        "steps": steps,
        "sent_inputs": sent,
        "all_inputs_sent": tuple(sent) == inputs,
        "auth_gate_detected": auth_gate_detected,
        "transcript": drive_claude_tui.clean(bytes(transcript)),
    }


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
        result = claude_tui_drive(
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
