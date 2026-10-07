#!/usr/bin/env python3
"""Claude Code 交互模式（TUI）驱动的命令行入口（Claude 升级控制面设计 B2-10 建立，B4-01 候选采集沿用）。

为什么要有
----------
官方取证由中继在进程内调用 TUI 驱动函数。宿主侧工具（启动探测、候选采集脚本）要在采集容器里按同一状态机驱动一次 TUI，
需要一个命令行入口——本文件只做参数解析、切换工作目录、调用驱动函数、把清理过 ANSI 的会话文本写进调用方给的文件，并在
标准输出打印一行结果摘要。

驱动函数（2026-10-07 起）
------------------------
``drive``／``drive_sequence`` 绑定到 ``capturelib/claude_fw_f_v3.py`` 的 Claude 侧驱动 ``claude_tui_drive``／
``claude_tui_drive_sequence``，中继 v3／v4／v5 的 TUI 场景用的是同一份。它与共用驱动 ``drive_claude_tui.py`` 逐行相同，只按
2.1.285 改了两处（详见该模块说明）：信任框按选项文字把焦点移到「Yes, I trust this folder」再确认（2.1.285 默认聚焦
「No, exit」）；主输入框在 auto 权限模式下页脚不再显示「? for shortcuts」，改认「⏵⏵ auto mode on (shift+tab to cycle) ·
← for agents」一类空闲提示。共用驱动被 Codex 身份策略按 ``drive_`` 前缀计入 Codex wire 层，只调用、不修改。

中继（``claude_fw_e_relay.py``）在 TUI 临时 HOME 里预置工作目录信任；启动探测的私有 HOME 同法预置，正常路径不出现信任框，
只有 ``untrust:`` 验收变更下才由驱动确认它。

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
import sys
from pathlib import Path
from typing import Any, Sequence

# 受管工具树里不得出现 __pycache__：导入驱动之前关闭字节码写入（与启动探测运行器读取 Codex 驱动同一做法）。
sys.dont_write_bytecode = True
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib import claude_fw_f_v3  # noqa: E402

RESULT_SCHEMA = "claude-drive-tui-cli/v1"
EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_USAGE = 2
EXIT_AUTH_GATE = 3

# 驱动循环（含信任框与 2.1.285 主输入框判定）在 capturelib/claude_fw_f_v3.py，中继的 TUI 场景用的是同一份；
# 这里只绑定名字，run() 照常调用 drive／drive_sequence。
drive = claude_fw_f_v3.claude_tui_drive
drive_sequence = claude_fw_f_v3.claude_tui_drive_sequence


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
