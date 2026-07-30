#!/usr/bin/env python3
"""驱动同一 Codex app-server 线程触发一次换模前压缩。

本驱动只负责走官方 RPC 路径并等待两轮完成。触发原因不能从 app-server 的
``contextCompaction`` 事件中读出，必须在采集结束后由
``extract_compaction_reason.py`` 从官方出站字节的
``x-codex-turn-metadata`` 中核验。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

from drive_codex_realtime import AppServer


def text_input(text: str) -> list[dict]:
    """构造 app-server v2 的最小文本输入。"""
    return [{"type": "text", "text": text, "textElements": []}]


def thread_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("thread") or {}).get("id")


def turn_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("turn") or {}).get("id")


def wait_turn_completed(
    server: AppServer,
    thread_id: str,
    turn_id: str,
    timeout: float,
    notification_start: int,
) -> bool:
    """等待指定 turn 完成；同时记录是否出现过压缩生命周期事件。"""
    end = time.monotonic() + timeout
    checked = notification_start
    compaction_items = 0
    while time.monotonic() < end:
        with server.lock:
            notifications = list(server.notifications)
        for message in notifications[checked:]:
            method = message.get("method")
            params = message.get("params") or {}
            item = params.get("item") or {}
            if method in ("item/started", "item/completed"):
                if item.get("type") == "contextCompaction":
                    compaction_items += 1
                    print(f"  观察到 {method}: contextCompaction", flush=True)
            if method != "turn/completed":
                continue
            turn = params.get("turn") or {}
            if params.get("threadId") != thread_id or turn.get("id") != turn_id:
                continue
            status = turn.get("status")
            print(
                f"  turn 完成，状态={status}，压缩生命周期事件={compaction_items}",
                flush=True,
            )
            return status == "completed"
        checked = len(notifications)
        time.sleep(0.25)
    print("  等待 turn/completed 超时", flush=True)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="/root/.local/bin/codex")
    parser.add_argument("--codex-version", default="0.145.0")
    parser.add_argument("--first-model", required=True)
    parser.add_argument("--second-model", required=True)
    parser.add_argument("--cwd", default="/tmp/compaction-reason-probe")
    parser.add_argument("--model-catalog-json")
    parser.add_argument("--disable", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        parser.error("--codex-version 必须是三段数字")

    pathlib.Path(args.cwd).mkdir(parents=True, exist_ok=True)
    argv = [args.codex_bin, "app-server"]
    for feature in args.disable:
        argv += ["--disable", feature]
    if args.model_catalog_json:
        argv += ["-c", f'model_catalog_json="{args.model_catalog_json}"']

    print(
        "启动官方 app-server："
        f"{args.first_model} -> {args.second_model}"
        + ("（受控模型目录）" if args.model_catalog_json else "（生产模型目录）"),
        flush=True,
    )
    server = AppServer(argv)
    try:
        initialized = server.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_exec",
                    "version": args.codex_version,
                    "title": "Codex",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if initialized is None:
            return 2

        thread = server.call(
            "thread/start",
            {
                "cwd": args.cwd,
                "model": args.first_model,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )
        thread_id = thread_id_from(thread)
        if not thread_id:
            print("thread/start 未返回线程 ID", flush=True)
            return 2

        with server.lock:
            first_notification_start = len(server.notifications)
        first = server.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": text_input("请只回复 FIRST-OK，不要调用任何工具。"),
            },
        )
        first_id = turn_id_from(first)
        if not first_id or not wait_turn_completed(
            server, thread_id, first_id, args.timeout, first_notification_start
        ):
            print("第一轮未正常完成，不能建立 previous_turn_settings", flush=True)
            return 2

        with server.lock:
            second_notification_start = len(server.notifications)
        second = server.call(
            "turn/start",
            {
                "threadId": thread_id,
                "model": args.second_model,
                "input": text_input("请只回复 SECOND-OK，不要调用任何工具。"),
            },
        )
        second_id = turn_id_from(second)
        if not second_id or not wait_turn_completed(
            server, thread_id, second_id, args.timeout, second_notification_start
        ):
            print("第二轮未正常完成", flush=True)
            return 2

        # 给中继记录器留出写入最后一帧的时间；最终完整性仍由独立脚本验收。
        time.sleep(3)
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
