#!/usr/bin/env python3
"""Codex PreToolUse 门禁：只允许场景声明的唯一 Bash 命令。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验 Codex Bash 工具命令。")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--expected-command")
    mode.add_argument("--deny-all", action="store_true")
    parser.add_argument("--audit-file", type=Path, required=True)
    return parser


def _write_audit(path: Path, *, allowed: bool) -> None:
    """只记录判定结果，不保存可能包含秘密的命令正文。"""

    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("审计输出必须是当前用户拥有的独立普通文件。")
        os.fchmod(descriptor, 0o600)
        payload = json.dumps(
            {"allowed": allowed}, ensure_ascii=False, separators=(",", ":")
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _deny(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _is_allowed(payload: Any, expected_command: str | None) -> bool:
    if expected_command is None or not isinstance(payload, dict):
        return False
    tool_input = payload.get("tool_input")
    return (
        payload.get("hook_event_name") == "PreToolUse"
        and payload.get("tool_name") == "Bash"
        and isinstance(tool_input, dict)
        and tool_input.get("command") == expected_command
    )


def main() -> int:
    arguments = _build_parser().parse_args()
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = None

    allowed = _is_allowed(payload, arguments.expected_command)
    try:
        _write_audit(arguments.audit_file, allowed=allowed)
    except OSError:
        _deny("抓包工具门禁无法写入审计记录，已失败关闭。")
        return 0

    if not allowed:
        _deny("抓包场景仅允许预先声明的固定命令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
