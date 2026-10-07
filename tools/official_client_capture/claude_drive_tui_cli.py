#!/usr/bin/env python3
"""Claude Code 交互模式（TUI）驱动的命令行入口（Claude 升级控制面设计 B2-10 建立，B4-01 候选采集沿用）。

为什么要有
----------
``drive_claude_tui.py`` 是 Codex 策略归在 wire 层的共用驱动（只调用、不修改：改它就改了 Codex 的 wire 摘要，
``test_codex_identity_conservation`` 要求逐字等于基线），只提供 Python 函数 ``drive``／``drive_sequence``：官方取证由中继
在进程内调用它们。宿主侧工具（启动探测、候选采集脚本）要在采集容器里按同一状态机驱动一次 TUI，需要一个命令行入口——
本文件做参数解析、切换工作目录、驱动一次会话、把清理过 ANSI 的会话文本写进调用方给的文件，并在标准输出打印一行结果摘要。

信任框（2026-10-07 修复）
------------------------
共用状态机把信任框当普通引导菜单，回车接受默认高亮项。2.1.226 的信任框是「❯ 1. Yes, I trust this folder／2. No, exit」，
回车即确认；2.1.285 的 ``TrustDialog`` 给确认组件传了 ``cancelFirst``、``focus:"cancel"``、``hideIndexes``，变成
「❯ No, exit／Yes, I trust this folder」且数字键失效（``hideIndexes`` 时选择组件的 ``disableSelection`` 取 ``"numeric"``），
回车就选中「No, exit」退出。所以本文件自带 Claude 侧驱动循环 ``drive``／``drive_sequence``：与共用状态机逐行相同（复用它的
``clean``／``classify_screen``），只在识别出信任框时改走 ``TrustDialogHandler``——按选项文字找出确认项与当前焦点，焦点不在
确认项上就用幂等的 Home／End（必要时加 Down）把焦点移过去，等客户端重绘、核对指针确实落在确认项（或只重绘了指针）之后
才回车；信任框上任何拿不准的情况都不回车（宁可超时失败，也不按默认项选到退出）。焦点本来就在确认项上（2.1.226）时只发
一个回车，与共用状态机发出的按键相同。

中继（``claude_fw_e_relay.py``）在 TUI 临时 HOME 里预置工作目录信任、照旧调用共用状态机；启动探测的私有 HOME 同法预置，
正常路径不出现信任框，只有 ``untrust:`` 验收变更下才由这里确认它。

用法
----
``python3 claude_drive_tui_cli.py --claude-bin <路径> --model <模型> [--cwd <目录>]
(--prompt <文本> | --input <文本> [--input <文本> …]) [--total-timeout 秒] [--quiet-after 秒] [--transcript <文件>]
[--term <TERM>] -- <客户端额外参数…>``

* ``--prompt``：单次提交（``drive``）；``--input`` 可重复：同一会话逐项提交（``drive_sequence``）。二者恰选其一。
* ``--`` 之后的参数原样作为 ``extra_args`` 交给驱动（驱动按 ``[claude, --model, 模型, *extra_args]`` 启动客户端），
  与中继调用驱动的形状相同。
* 客户端环境取本进程环境；``--term`` 只在给出时设置 ``TERM``（官方取证的中继环境不带 ``TERM``，缺省与它一致）。
* ``--transcript``：会话文本（驱动已剥掉 ANSI）只写进这个文件（0600、不得已存在）；标准输出的摘要不含会话文本。

退出码：0 输入已全部提交且没有遇到登录门禁；3 遇到登录门禁（驱动立即停下，绝不把输入发进登录流程）；1 输入没有
全部提交（超时、客户端提前退出或卡在驱动认不出的界面）；2 参数错误。

本文件在 Claude 身份策略里归 wire 层（设计 B1-08 登记），被 Codex 身份策略忽略（``claude_`` 前缀）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import re
import select
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

# 受管工具树里不得出现 __pycache__：导入驱动之前关闭字节码写入（与启动探测运行器读取 Codex 驱动同一做法）。
sys.dont_write_bytecode = True
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import drive_claude_tui  # noqa: E402

RESULT_SCHEMA = "claude-drive-tui-cli/v1"
EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2
EXIT_AUTH_GATE = 3

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
# Claude 侧驱动循环：与 drive_claude_tui.drive／drive_sequence 逐行相同，只多信任框一支
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


def drive(
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
        screen_state = drive_claude_tui.classify_screen(view)

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


def drive_sequence(
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
        screen_state = drive_claude_tui.classify_screen(view)
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


class DriveCliError(ValueError):
    """参数不满足驱动前提：按用法错误退出，不启动客户端。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--claude-bin", required=True, help="目标客户端二进制的绝对路径")
    parser.add_argument("--model", required=True)
    parser.add_argument("--cwd", default=None, help="启动客户端之前切换到的工作目录（绝对路径，缺省不切换）")
    parser.add_argument("--prompt", default=None, help="单次提交的文本（drive）")
    parser.add_argument("--input", action="append", default=[], help="同一会话逐项提交的文本（drive_sequence，可重复）")
    parser.add_argument("--total-timeout", type=int, default=None, help="整个会话的上限秒数（缺省取驱动函数的缺省值）")
    parser.add_argument("--quiet-after", type=int, default=None, help="最后一次提交之后再等的秒数（缺省取驱动函数的缺省值）")
    parser.add_argument("--transcript", type=Path, default=None, help="会话文本写入的文件（0600，不得已存在）")
    parser.add_argument("--term", default=None, help="给客户端设置的 TERM（缺省不设，与官方取证的中继环境一致）")
    parser.add_argument("extra_args", nargs=argparse.REMAINDER, help="-- 之后原样交给驱动的客户端参数")
    return parser


def _absolute(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DriveCliError(f"{label} 必须是不含父目录跳转的绝对路径：{value}")
    return path


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    arguments = build_parser().parse_args(list(argv))
    extra = list(arguments.extra_args)
    if extra and extra[0] == "--":
        extra = extra[1:]
    arguments.extra_args = extra
    _absolute(arguments.claude_bin, "--claude-bin")
    if arguments.cwd is not None:
        _absolute(arguments.cwd, "--cwd")
    if (arguments.prompt is None) == (not arguments.input):
        raise DriveCliError("--prompt 与 --input 必须恰好给出一种")
    texts = [arguments.prompt] if arguments.prompt is not None else list(arguments.input)
    if any(not isinstance(text, str) or not text for text in texts):
        raise DriveCliError("提交的文本不能为空")
    for label, value in (("--total-timeout", arguments.total_timeout), ("--quiet-after", arguments.quiet_after)):
        if value is not None and value <= 0:
            raise DriveCliError(f"{label} 必须是正整数")
    if arguments.transcript is not None:
        transcript = _absolute(str(arguments.transcript), "--transcript")
        if transcript.exists() or transcript.is_symlink():
            raise DriveCliError(f"--transcript 已存在，禁止覆盖：{transcript}")
    if arguments.term is not None and (not arguments.term or any(char.isspace() for char in arguments.term)):
        raise DriveCliError("--term 不能为空或含空白")
    return arguments


def _write_transcript(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def run(arguments: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """切换工作目录后调用本文件的驱动循环，返回（退出码，摘要）。参数形状与中继调用共用驱动函数时相同。"""

    environment = dict(os.environ)
    if arguments.term is not None:
        environment["TERM"] = arguments.term
    options: dict[str, Any] = {"extra_args": tuple(arguments.extra_args)}
    if arguments.total_timeout is not None:
        options["total_timeout"] = arguments.total_timeout
    previous = Path.cwd()
    if arguments.cwd is not None:
        os.chdir(arguments.cwd)
    try:
        if arguments.prompt is not None:
            if arguments.quiet_after is not None:
                options["quiet_after_prompt"] = arguments.quiet_after
            result = drive(arguments.claude_bin, arguments.model, arguments.prompt, environment, **options)
            complete = result.get("sent_prompt") is True
        else:
            if arguments.quiet_after is not None:
                options["quiet_after_last_input"] = arguments.quiet_after
            result = drive_sequence(
                arguments.claude_bin, arguments.model, tuple(arguments.input), environment, **options
            )
            complete = result.get("all_inputs_sent") is True
    finally:
        os.chdir(previous)
    transcript = str(result.get("transcript", ""))
    if arguments.transcript is not None:
        _write_transcript(arguments.transcript, transcript)
    auth_gate = result.get("auth_gate_detected") is True
    summary: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "mode": "prompt" if arguments.prompt is not None else "sequence",
        "steps": [[float(at), str(tag)] for at, tag in result.get("steps", [])],
        "input_complete": complete,
        "inputs_sent": 1 if arguments.prompt is not None and complete else len(result.get("sent_inputs", [])),
        "auth_gate_detected": auth_gate,
        "transcript_bytes": len(transcript.encode("utf-8")),
        "transcript_sha256": hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        "transcript_written": arguments.transcript is not None,
    }
    if auth_gate:
        return EXIT_AUTH_GATE, summary
    return (EXIT_COMPLETE if complete else EXIT_INCOMPLETE), summary


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    except DriveCliError as error:
        print(json.dumps({"schema_version": RESULT_SCHEMA, "status": "usage_error", "error": str(error)}, ensure_ascii=False))
        return EXIT_USAGE
    code, summary = run(arguments)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
