#!/usr/bin/env python3
"""驱动同一个 Codex app-server 进程触发记忆整合请求。

记忆整合没有直接 JSON-RPC。生产路径要求先有一个可提取的已完成线程，再由另一个
根线程的非空 ``turn/start`` 启动 memories pipeline。本驱动按该顺序构造线程 A/B，
最终只以中继原始上行出现两个条件头为成功判据：

``x-openai-memgen-request: true`` 与
``x-openai-subagent: memory_consolidation``。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

from drive_codex_realtime import AppServer


def wait_turn_completed(
    server: AppServer,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> bool:
    """等待指定 turn 的完成通知，其他通知保留给后续诊断。"""
    end = time.monotonic() + timeout
    checked = 0
    while time.monotonic() < end:
        with server.lock:
            notifications = list(server.notifications)
        for message in notifications[checked:]:
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params") or {}
            turn = params.get("turn") or {}
            if params.get("threadId") == thread_id and turn.get("id") == turn_id:
                status = turn.get("status")
                print(f"  turn {turn_id} 完成，状态={status}", flush=True)
                return status == "completed"
        checked = len(notifications)
        time.sleep(0.25)
    print(f"  等待 turn/completed 超时：{turn_id}", flush=True)
    return False


def target_headers_seen(relay_dir: pathlib.Path) -> tuple[bool, str | None]:
    """扫描上行原始字节，要求两个 header 出现在同一个连接文件里。"""
    for path in sorted(relay_dir.glob("conn*.client_to_upstream.bin")):
        data = path.read_bytes().lower()
        if (b"x-openai-memgen-request: true" in data
                and b"x-openai-subagent: memory_consolidation" in data):
            return True, path.name
    return False, None


def thread_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("thread") or {}).get("id")


def turn_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("turn") or {}).get("id")


def text_input(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "textElements": []}]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="/root/.local/bin/codex")
    parser.add_argument("--codex-version", default="0.145.0")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--cwd", default="/tmp/memgen-probe")
    parser.add_argument("--relay-dir", required=True)
    parser.add_argument("--hold", type=float, default=300.0,
                        help="线程 B 启动后等待内部记忆整合的最长秒数")
    parser.add_argument("--disable", action="append", default=[])
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        parser.error("--codex-version 必须是三段数字")

    pathlib.Path(args.cwd).mkdir(parents=True, exist_ok=True)
    relay_dir = pathlib.Path(args.relay_dir)

    argv = [args.codex_bin, "app-server", "--enable", "memories"]
    for feature in args.disable:
        argv += ["--disable", feature]
    print(f"启动: {' '.join(argv)}", flush=True)
    server = AppServer(argv)
    try:
        initialized = server.call("initialize", {
            "clientInfo": {
                "name": "codex_exec",
                "version": args.codex_version,
                "title": "Codex",
            },
            "capabilities": {"experimentalApi": True},
        })
        if initialized is None:
            return 2

        # 线程 A 要持久化为候选，但禁止 A 自己启动 memories pipeline。
        thread_a = server.call("thread/start", {
            "cwd": args.cwd,
            "model": args.model,
            "ephemeral": False,
            "config": {
                "features.memories": False,
                "memories.generate_memories": True,
            },
        })
        thread_a_id = thread_id_from(thread_a)
        if not thread_a_id:
            print("线程 A 创建失败", flush=True)
            return 2
        print(f"  线程 A = {thread_a_id}", flush=True)

        turn_a = server.call("turn/start", {
            "threadId": thread_a_id,
            "input": text_input(
                "这是长期工作约定：发布 memgen-probe 前必须运行 cargo test，"
                "约定代号 MEMGEN-0145。请只回复 A-OK。"
            ),
        })
        turn_a_id = turn_id_from(turn_a)
        if not turn_a_id or not wait_turn_completed(
            server, thread_a_id, turn_a_id, timeout=180.0
        ):
            print("线程 A 未正常完成，无法作为记忆候选", flush=True)
            return 2

        memory_mode = server.call("thread/memoryMode/set", {
            "threadId": thread_a_id,
            "mode": "enabled",
        })
        if memory_mode is None:
            print("设置线程 A 的 memory mode 失败", flush=True)
            return 2

        # 线程 B 的非空 turn/start 启动真实 memories pipeline。把时间与配额阈值降到
        # 最小只是在触发条件上做 I 类干预，Phase 1/2 与内部请求仍走生产代码。
        thread_b = server.call("thread/start", {
            "cwd": args.cwd,
            "model": args.model,
            "ephemeral": False,
            "config": {
                "features.memories": True,
                "memories.generate_memories": True,
                "memories.use_memories": True,
                "memories.disable_on_external_context": False,
                "memories.min_rollout_idle_hours": 0,
                "memories.min_rate_limit_remaining_percent": 0,
                "memories.max_rollouts_per_startup": 1,
                "memories.max_raw_memories_for_consolidation": 1,
            },
        })
        thread_b_id = thread_id_from(thread_b)
        if not thread_b_id:
            print("线程 B 创建失败", flush=True)
            return 2
        print(f"  线程 B = {thread_b_id}", flush=True)

        turn_b = server.call("turn/start", {
            "threadId": thread_b_id,
            "input": text_input("请只回复 B-OK。"),
        })
        if not turn_id_from(turn_b):
            print("线程 B 的 turn/start 失败", flush=True)
            return 2

        print(f"--- 最长等待 {args.hold:.0f}s，扫描 memgen 条件头 ---", flush=True)
        end = time.monotonic() + args.hold
        while time.monotonic() < end:
            found, file_name = target_headers_seen(relay_dir)
            if found:
                print(f"  ✅ 在 {file_name} 同时命中两个 memgen header", flush=True)
                time.sleep(5)
                return 0
            time.sleep(1)
        print("  ❌ 等待结束，未命中两个 memgen header", flush=True)
        return 1
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
