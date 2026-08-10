#!/usr/bin/env python3
"""驱动 codex app-server 发起 realtime 会话，采官方 live 端点基线（SPEC-EP-012）。

为什么不是 TUI 也不是 exec
--------------------------
live/realtime 既不在斜杠命令列表里（`tui/src/slash_command.rs` 全表无 realtime），
也没有 CLI 参数。它由 **app-server 的 JSON-RPC** 发起
（`app-server/src/request_processors/turn_processor.rs:1059`
`thread_realtime_start_inner`），TUI 只是接收 `ThreadRealtime*` 通知事件。

因此路径是：`codex app-server`（stdio JSON-RPC）→ initialize → thread/start →
thread/realtime/start。

前置条件
--------
`Feature::RealtimeConversation` 默认关闭（`features/src/lib.rs:1319-1323`，
`Stage::UnderDevelopment`），须用 `--enable realtime_conversation` 打开；
未开时 app-server 直接返回 `thread {id} does not support realtime conversation`
（同文件 :1050）。

**这是官方 CLI 自带的开关，不需要任何外部权限或账号侧配置。**
早前把它记成"需管理员开 AllowLive"是把两侧搞混了——`AllowLive` 是 Sub2API 自己的
分组开关（`group.AllowLive`），管的是我们的用户能否调 live 接口，与官方形态无关。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import threading
import time


# 结构合法的最小 SDP offer——只为把 call-create 请求发出去。
# 真实客户端的 offer 由 RTCPeerConnection 生成，含完整 ICE/DTLS 参数；
# 这里不需要它能真的协商成功：即便后续 SDP 交换失败，
# **HTTP call-create 那一跳已经上线**，形态即可观测。
MINIMAL_SDP_OFFER = (
    # 上游会校验 offer 内容——只给 data channel 会被拒：
    #   {"message": "Offer did not have an audio media section.",
    #    "code": "invalid_offer"}
    # 所以必须带 m=audio 段。opus/48000/2 是 realtime 的标准编解码。
    # 这仍不是真实 RTCPeerConnection 生成的 offer（ICE candidate 都没有），
    # 但目标只是让 call-create 被接受、从而观测**后续的 sideband 出站**。
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0 1\r\n"
    "a=msid-semantic: WMS probe\r\n"
    # ── audio 段（上游必需）──
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:probe\r\n"
    "a=ice-pwd:probeprobeprobeprobeprobe\r\n"
    "a=ice-options:trickle\r\n"
    "a=fingerprint:sha-256 "
    "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:"
    "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=sendrecv\r\n"
    "a=rtcp-mux\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
    "a=ssrc:1001 cname:probe\r\n"
    # ── data channel 段（realtime 事件通道）──
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:probe\r\n"
    "a=ice-pwd:probeprobeprobeprobeprobe\r\n"
    "a=fingerprint:sha-256 "
    "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:"
    "00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
    "a=setup:actpass\r\n"
    "a=mid:1\r\n"
    "a=sctp-port:5000\r\n"
)


class AppServer:
    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.responses: dict[int, dict] = {}
        self.notifications: list[dict] = []
        # 收据构建器要按原始事件判定 started/SDP 与异步 error，因此调用层的失败
        # 也必须留痕——此前 realtime/start 的返回值被整个丢弃，没有任何消费者。
        self.call_errors: list[dict] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._next_id = 0

    def _pump_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                print(f"    [非 JSON] {line[:120]}", flush=True)
                continue
            with self.lock:
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
                    m = msg.get("method", "")
                    if m:
                        print(f"    [通知] {m}", flush=True)

    def _pump_stderr(self):
        for line in self.proc.stderr:
            s = line.strip()
            # app-server 的日志噪声大，只回显可能有用的
            if any(k in s.lower() for k in ("error", "realtime", "warn", "panic")):
                # 上游的错误体是多行 JSON，截断会丢掉关键的 message/param 字段
                print(f"    [stderr] {s[:600]}", flush=True)

    def wait_for_notification(self, methods: set[str], timeout: float):
        """等待任一目标通知出现，返回 (method, 消息) 或 (None, None)。

        `thread/realtime/start` 的响应是空对象（`ThreadRealtimeStartResponse {}`），
        会话是否真的建立只能由随后的异步通知回答。此前这里是无条件 sleep，
        started／sdp／error 全都没有消费者。
        """

        end = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < end:
            with self.lock:
                pending = self.notifications[seen:]
                seen = len(self.notifications)
            for message in pending:
                method = message.get("method", "")
                if method in methods:
                    return method, message
            time.sleep(0.1)
        return None, None

    def call(self, method: str, params: dict, timeout: float = 30.0):
        self._next_id += 1
        rid = self._next_id
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        print(f"  → {method}", flush=True)
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if rid in self.responses:
                    msg = self.responses.pop(rid)
                    if "error" in msg:
                        print(f"    ✗ {json.dumps(msg['error'], ensure_ascii=False)[:200]}",
                              flush=True)
                        self.call_errors.append({"method": method, "error": msg["error"]})
                        return None
                    print("    ✓", flush=True)
                    return msg.get("result")
            time.sleep(0.1)
        print(f"    ✗ 超时 {timeout}s", flush=True)
        self.call_errors.append({"method": method, "error": {"message": f"timeout {timeout}s"}})
        return None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codex-bin", default="/root/.local/bin/codex")
    ap.add_argument("--codex-version", default="0.145.0")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--cwd", default="/tmp/realtime-probe")
    ap.add_argument("--hold", type=float, default=25.0,
                    help="realtime/start 之后保持多久，让握手与首帧走完")
    ap.add_argument("--transport", choices=["websocket", "webrtc"], default="websocket",
                    help="**webrtc 才是 OAuth 可达的那条**（websocket 分支要 API key）")
    ap.add_argument("--output-modality", choices=["text", "audio"], default="text")
    ap.add_argument("--disable", action="append", default=[],
                    help="传给 codex 的 --disable <FEATURE>，可重复")
    ap.add_argument("--events-output", default=None,
                    help="落原始 JSON-RPC 通知流，供 SCN-REALITY-01 收据构建器解析")
    # WebRTC 传输不显式声明版本时，0.147 默认落到 V1
    # （`core/src/realtime_conversation.rs:1170-1172`），发出的是
    # `openai-alpha: quicksilver=v1`，上游已用 400 invalid_quicksilver_alpha_header 拒绝。
    # V3 走 FramelessBidi 解析器，header 值是 quicksilver=v2（命名错位，但这是官方实现，
    # 见 `:1647-1661`）。WebRTC 只接受 v1／v3，v2 会被 validate_avas_webrtc_start 拒绝。
    ap.add_argument("--realtime-version", choices=["v1", "v2", "v3"], default="v3",
                    help="显式声明 realtime 协议版本；WebRTC 不传会默认 v1")
    ap.add_argument("--final-event-timeout", type=float, default=90.0,
                    help="等待 started/SDP 最终事件的上限")
    args = ap.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        ap.error("--codex-version 必须是三段数字")

    import os
    os.makedirs(args.cwd, exist_ok=True)

    argv = [args.codex_bin, "app-server", "--enable", "realtime_conversation"]
    for f in (args.disable or []):
        argv += ["--disable", f]
    print(f"启动: {' '.join(argv)}", flush=True)
    srv = AppServer(argv)
    # 提前 return 的分支也要走 finally 落事件日志，因此先初始化。
    started: dict | None = None
    try:
        # `thread/realtime/start` 标了 `#[experimental(...)]`，未声明该能力时
        # app-server 直接拒绝："… requires experimentalApi capability"
        # （`app-server-protocol/src/experimental_api.rs:31`）。
        init = srv.call("initialize", {
            "clientInfo": {"name": "codex_exec", "version": args.codex_version,
                           "title": "Codex"},
            "capabilities": {"experimentalApi": True},
        })
        if init is None:
            print("initialize 失败，无法继续", flush=True)
            return 2
        # 注意：app-server **没有** `initialized` 这个通知（不同于 LSP/MCP 的惯例），
        # 发了会收到 -32600 unknown variant。initialize 返回即可直接下一步。

        thread = srv.call("thread/start", {"cwd": args.cwd, "model": args.model})
        if not thread:
            print("thread/start 失败", flush=True)
            return 2
        # 返回结构是 {"thread": {"id": …, "sessionId": …}}，threadId 不在顶层
        tid = (thread.get("thread") or {}).get("id")
        print(f"  threadId = {tid}", flush=True)
        if not tid:
            print(f"  未拿到 threadId，result = {json.dumps(thread)[:300]}", flush=True)
            return 2

        # 真正要测的调用：它会打 {base}/realtime/calls
        #
        # outputModality 是必填（`ThreadRealtimeStartParams.output_modality` 无
        # `Option`）。注意 v1／v3 只接受 audio——`realtime_conversation.rs:1336-1342`
        # 明确「text realtime output modality requires realtime v2」。
        params = {
            "threadId": tid,
            "outputModality": args.output_modality,
            # 显式声明版本。`RealtimeConversationVersion` 按 snake_case 序列化为
            # "v1"／"v2"／"v3"（`protocol/src/protocol.rs:1625-1632`）。
            "version": args.realtime_version,
        }
        if args.transport == "webrtc":
            # WebRTC 分支才是 OAuth 可达的那条（`realtime_conversation.rs:1175`
            # 传 api_key: None），默认的 Websocket 分支会因缺 API key 直接失败。
            #
            # `ThreadRealtimeStartTransport::Webrtc { sdp }` 要求一个 SDP offer
            # （`app-server-protocol/src/protocol/v2/realtime.rs:138`）。
            # 我们的目标是**让它把 HTTP call-create 请求发出去**并观测其形态，
            # 不是真的建成 WebRTC 会话——所以给一个结构合法的最小 offer 即可。
            params["transport"] = {"type": "webrtc", "sdp": MINIMAL_SDP_OFFER}
        # 这一跳的返回值此前被整个丢弃：上游拒绝 SDP、feature 未开、60s 超时，
        # 全都只打一行 ✗ 而函数照样 return 0。现在判空并向外传播。
        started = srv.call("thread/realtime/start", params, timeout=60)
        if started is None:
            print("thread/realtime/start 失败，目标协议分支未成立", flush=True)
        else:
            # 响应体是空对象，会话是否建立只能看异步通知。等到最终事件为止，
            # 不再无条件 sleep 到 hold 结束。
            print(f"--- 等待 started/SDP 最终事件（上限 {args.final_event_timeout}s）---",
                  flush=True)
            final_method, _ = srv.wait_for_notification(
                {
                    "thread/realtime/started",
                    "thread/realtime/sdp",
                    "thread/realtime/error",
                    "thread/realtime/closed",
                },
                args.final_event_timeout,
            )
            if final_method is None:
                print("  未等到任何最终事件", flush=True)
            else:
                print(f"  最终事件：{final_method}", flush=True)
            # 会话建立后仍保持一段时间，让 sideband 与首帧走完并进入抓包。
            if final_method in {"thread/realtime/started", "thread/realtime/sdp"}:
                print(f"--- 保持 {args.hold}s，让 sideband 与首帧走完 ---", flush=True)
                time.sleep(args.hold)
    finally:
        _write_events(args.events_output, srv, started, args.realtime_version)
        srv.close()
    if started is None:
        return 2
    return 0


def _write_events(
    output: str | None,
    srv: "AppServer",
    started: dict | None,
    requested_version: str,
) -> None:
    """落一份原始事件流。收据构建器只解析，不推断——驱动不判定场景成败。

    这里刻意不产出 call_id：`ThreadRealtimeStartResponse` 是空对象
    （`app-server-protocol/src/protocol/v2/realtime.rs:161-165`），call_id 只存在于
    call-create 的 HTTP 响应与 sideband URL 里，两者都在 relay 字节中，由收据构建器
    从原始字节提取并互相印证。驱动侧能证明的是会话终态与协商出的版本。
    """

    if not output:
        return
    import os

    with srv.lock:
        notifications = list(srv.notifications)
        errors = list(srv.call_errors)
    realtime_session_id = ""
    negotiated_version = ""
    for message in notifications:
        if message.get("method") != "thread/realtime/started":
            continue
        params = message.get("params") or {}
        if isinstance(params.get("realtimeSessionId"), str):
            realtime_session_id = params["realtimeSessionId"]
        if isinstance(params.get("version"), str):
            negotiated_version = params["version"]
    payload = {
        "schema_version": "codex-egress-realtime-events/v2",
        "notifications": notifications,
        "call_errors": errors,
        "requested_version": requested_version,
        "negotiated_version": negotiated_version,
        "realtime_session_id": realtime_session_id,
        "realtime_start_returned": started is not None,
    }
    path = pathlib.Path(output)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"  事件日志: {path}（{len(notifications)} 条通知，{len(errors)} 次调用失败）",
          flush=True)


if __name__ == "__main__":
    sys.exit(main())
