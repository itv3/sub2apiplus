#!/usr/bin/env python3
"""驱动官方 app-server 以 file_id 图片作为会话输入，触发 imagegen 对会话图片的编辑（0.158.0 起）。

0.158.0 起 imagegen 用 ``num_last_images_to_include`` 从会话历史取图时保留 file-backed 图片，并以
``{"file_id": …}`` 发出 images/edits（``ext/image-generation/src/tool.rs`` 的 recent_images）；0.157.0 在同样
情形下直接报错、不发请求。CLI 默认把本地图片内联为 data URL，只有 app-server 的
``{"type":"image","fileId":…}`` 输入（``app-server-protocol`` v2 ``UserInput::Image`` + ``ImageReference::File``）
能把 file-backed 图片放进会话历史。

本驱动只走官方 RPC：initialize → thread/start → turn/start（文本 + file_id 图片）→ 等待 turn/completed。
模型侧的 imagegen 调用、images/edits 的结果与最后一条消息由字节中继按
``--synthesize-image-edit-file-id`` 受控应答（file_id 只在本链内有效，不转发生产）；请求字节全部来自官方二进制。
观测文件只记录事实（turn 状态、imageGeneration 条目事件计数），不判定成败；成败由请求断言与
seal 前的证据链决定。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time

from drive_codex_realtime import AppServer

FILE_ID_RE = re.compile(r"^file[-_][A-Za-z0-9_-]{8,64}$")
CONFIG_RE = re.compile(r"^[A-Za-z0-9_.-]+=[^\n\r]{1,256}$")


def thread_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("thread") or {}).get("id")


def turn_id_from(result: dict | None) -> str | None:
    return ((result or {}).get("turn") or {}).get("id")


def wait_turn(server: AppServer, thread_id: str, turn_id: str, timeout: float, start: int) -> dict:
    """等待指定 turn 完成，同时按条目类型统计 item/started 与 item/completed。"""

    end = time.monotonic() + timeout
    checked = start
    item_events: dict[str, int] = {}
    while time.monotonic() < end:
        with server.lock:
            notifications = list(server.notifications)
        for message in notifications[checked:]:
            method = message.get("method")
            params = message.get("params") or {}
            if method in ("item/started", "item/completed"):
                item_type = str((params.get("item") or {}).get("type") or "unknown")
                key = f"{method}:{item_type}"
                item_events[key] = item_events.get(key, 0) + 1
            if method != "turn/completed":
                continue
            turn = params.get("turn") or {}
            if params.get("threadId") != thread_id or turn.get("id") != turn_id:
                continue
            return {"completed": True, "status": turn.get("status"), "item_events": item_events}
        checked = len(notifications)
        time.sleep(0.25)
    return {"completed": False, "status": None, "item_events": item_events}


def write_observation(path: str, payload: dict) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="/root/.local/bin/codex")
    parser.add_argument("--codex-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cwd", default="/tmp/image-edit-file-id-probe")
    parser.add_argument("--file-id", required=True)
    parser.add_argument("--disable", action="append", default=[])
    parser.add_argument("--config", action="append", default=[],
                        help="透传给 app-server 的 -c 覆盖（例如只走 HTTP 的 provider），逐项校验")
    parser.add_argument("--observations", required=True)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        parser.error("--codex-version 必须是三段数字")
    if not FILE_ID_RE.fullmatch(args.file_id):
        parser.error("--file-id 必须形如 file-<8～64 位字母数字下划线连字符>")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.model):
        parser.error("--model 只能包含字母、数字、点、下划线和连字符")
    for item in args.config:
        if not CONFIG_RE.fullmatch(item):
            parser.error(f"--config 取值非法：{item!r}")

    pathlib.Path(args.cwd).mkdir(parents=True, exist_ok=True)
    argv = [args.codex_bin, "app-server"]
    for feature in args.disable:
        argv += ["--disable", feature]
    for item in args.config:
        argv += ["-c", item]

    observation: dict = {
        "schema_version": "codex-file-image-edit-observation/v1",
        "file_id": args.file_id,
        "model": args.model,
        "codex_version": args.codex_version,
        "rpc": [],
    }
    print(f"启动官方 app-server：file_id 图片编辑链（{args.model}）", flush=True)
    server = AppServer(argv)
    try:
        initialized = server.call(
            "initialize",
            {
                "clientInfo": {"name": "codex_exec", "version": args.codex_version, "title": "Codex"},
                "capabilities": {"experimentalApi": True},
            },
        )
        observation["rpc"].append({"method": "initialize", "ok": initialized is not None})
        if initialized is None:
            write_observation(args.observations, observation)
            return 2
        thread = server.call(
            "thread/start",
            {
                "cwd": args.cwd,
                "model": args.model,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        )
        thread_id = thread_id_from(thread)
        observation["rpc"].append({"method": "thread/start", "ok": bool(thread_id)})
        if not thread_id:
            write_observation(args.observations, observation)
            return 2
        with server.lock:
            start = len(server.notifications)
        turn = server.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": "请用图片工具编辑我附上的这张图片：改成蓝色调，保持构图不变。",
                        "textElements": [],
                    },
                    {"type": "image", "fileId": args.file_id},
                ],
            },
        )
        turn_id = turn_id_from(turn)
        observation["rpc"].append({"method": "turn/start", "ok": bool(turn_id)})
        if not turn_id:
            write_observation(args.observations, observation)
            return 2
        result = wait_turn(server, thread_id, turn_id, args.timeout, start)
        observation["turn"] = result
        write_observation(args.observations, observation)
        print(f"  turn 结束：{json.dumps(result, ensure_ascii=False)}", flush=True)
        # 给中继记录器留出写入最后一段字节的时间；完整性由独立门禁验收。
        time.sleep(3)
        return 0 if result["completed"] else 2
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
