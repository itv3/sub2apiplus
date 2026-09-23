#!/usr/bin/env python3
"""驱动的有界等待：完成标记证明完成，PID／心跳只证明仍在运行。"""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class WaitError(RuntimeError):
    """等待条件不再成立；调用方不得改变 Campaign 状态。"""


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("等待时限必须是有限正数")
    return result


def alive(pid: int) -> bool:
    """kill -0 之外拒绝僵尸进程，避免父 bash 尚未 wait 时误判存活。"""

    if pid < 2:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                            text=True, timeout=5, check=False)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def marker(path: Path, pattern: str) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open(errors="replace") as stream:
        return any(re.search(pattern, line) for line in stream)


def supervisor_fresh(root: Path, launched: float, stale: float) -> bool:
    """只认本次派发之后的最新父 run，不能借前一批次的新鲜心跳续命。"""

    runs = []
    for path in root.glob("run-*/state.json"):
        state = json.loads(path.read_text())
        if state.get("started_at_epoch", 0) >= launched - 1:
            runs.append((state["started_at_epoch"], path.parent, state))
    if not runs:
        return False
    _, run, state = max(runs, key=lambda row: row[0])
    path = run / "heartbeat.json"
    if not path.is_file():
        return False
    heartbeat = json.loads(path.read_text())
    if (heartbeat.get("schema_version") != "codex-upgrade-supervisor-heartbeat/v1"
            or heartbeat.get("owner_pid") != state.get("owner_pid")
            or heartbeat.get("owner_nonce") != state.get("owner_nonce")
            or heartbeat.get("state") not in {"running", "stopped"}):
        raise WaitError("监督器心跳身份或状态不匹配")
    return 0 <= time.time() - heartbeat["updated_at_epoch"] <= min(stale, state.get("watchdog_timeout_seconds", stale))


def wait(args: argparse.Namespace) -> None:
    started = time.monotonic()
    pid = args.pid
    launched = time.time()
    heartbeat_seen = False
    while True:
        elapsed = time.monotonic() - started
        if args.mode == "marker":
            done = marker(args.file, args.regex)
            peer_done = args.peer_log is None or marker(args.peer_log, args.peer_regex)
            if done and peer_done:
                return
            if args.pid_file and pid is None:
                if args.pid_file.is_file():
                    pid = int(args.pid_file.read_text().strip())
                    launched = args.pid_file.stat().st_mtime
                elif elapsed >= min(args.startup_seconds, args.max_seconds):
                    raise WaitError("实际 run 子进程未写出 PID")
            if not done and pid is not None and not alive(pid):
                raise WaitError(f"子进程已退出：PID={pid}")
            if not peer_done and args.peer_pid is not None and not alive(args.peer_pid):
                raise WaitError(f"并行子进程已退出：PID={args.peer_pid}")
            if args.supervisor_root and not done and pid is not None:
                fresh = supervisor_fresh(args.supervisor_root, launched, args.stale_seconds)
                if not fresh and (heartbeat_seen or time.time() - launched >= args.startup_seconds):
                    raise WaitError("监督器心跳缺失或过期")
                heartbeat_seen = heartbeat_seen or fresh
        else:
            # READY 只结束等待，调用方还必须核验上传清单及完整实现测试收据。
            if (args.file.parent / "READY").is_file():
                return
            if args.file.is_file() and not args.file.is_symlink():
                age = time.time() - args.file.stat().st_mtime
                if age < -1 or age > args.stale_seconds:
                    raise WaitError("上传心跳过期或时间异常")
            elif elapsed >= args.stale_seconds:
                raise WaitError("上传心跳一直缺失")
        if elapsed >= args.max_seconds:
            raise WaitError("等待超过总时限")
        time.sleep(min(0.5, args.max_seconds - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("marker", "heartbeat"))
    parser.add_argument("file", type=Path)
    parser.add_argument("--regex", default="")
    parser.add_argument("--max-seconds", type=positive, required=True)
    parser.add_argument("--stale-seconds", type=positive, default=300)
    parser.add_argument("--startup-seconds", type=positive, default=60)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--supervisor-root", type=Path)
    parser.add_argument("--peer-log", type=Path)
    parser.add_argument("--peer-regex", default="")
    parser.add_argument("--peer-pid", type=int)
    parser.add_argument("--log", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        wait(args)
        return 0
    except (WaitError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"等待失败：{error}", file=sys.stderr)
        for path in dict.fromkeys([args.file, args.peer_log, *args.log]):
            if path is not None and path.is_file():
                print(f"日志尾 200 行：{path}", file=sys.stderr)
                with path.open(errors="replace") as stream:
                    sys.stderr.writelines(deque(stream, maxlen=200))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
