#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wsmock.py —— 假上游：冒充 chatgpt.com 的 Codex Responses WebSocket 端点（只用 Python 标准库）。

用途（升级前行为验收套件专用，见同目录 README.md）：
  - 监听 443/TLS（证书由套件临时生成的测试 CA 签发，SAN 含 chatgpt.com / auth.openai.com / api.openai.com /
    api.wsrec.test），网关容器通过 extra_hosts 把这些域名解析到本容器，OpenAI OAuth 账号（上游地址写死
    chatgpt.com）的官方出站链路因此真实地走到这里；
  - 接受 WebSocket 升级，逐帧记录 response.create 的“出站帧”：顶层键、input 各项的类型／角色、密文的
    sha256 前缀与长度、是否带 content 数组、配对 ID 等（日志里不记密文原文；原始帧另存 frames/ 供比对）；
  - 按 ctl.json（每帧重读）的场景指令应答。指令键（全部可选）：
      reject_ciphers  命中其中任意一条密文的业务帧，回 invalid_encrypted_content 错误（首个命中项的文案）
      hint            ellipsis（首 N 位...末 N 位，默认）| exact（完整值）| none（无片段）| short（片段太短）
                      | unicode（用 Unicode 省略号）
      hint_len        ellipsis 的首尾位数（默认 4）
      hint_override   错误文案里的片段取自这条密文，而不是真正被拒的那条（模拟定位证据与历史不一致）
      lifecycle_first 错误前先回 response.created
      output_first    错误前先回 response.created + 文字增量（模拟已有输出）
      toolitem_first  错误前先回 function_call 输出项的 added 事件（不发任何 delta）
      emit_failed     错误后再回 response.failed。默认 false：真实上游对失效密文只发 error，既不发终态也不断连，
                      这正是 v0.2.18-4 断流事故的触发形态，所以默认就是它
      close_after_error  错误事件之后直接断开 TCP
      drop_next       > 0 时下一个业务帧到达就直接断开 TCP 且不应答，并把计数减 1
      delay_ms        每个应答前的延迟
    其余业务帧与预热帧（generate=false）正常完成（created + 输出 + completed，带 usage）。

控制文件与日志都在 MOCK_DATA 目录（宿主机挂载）：ctl.json、requests.jsonl、frames/<conn>-<seq>.json。

结构说明：把“收到一帧后该回什么”抽成纯函数 plan_response()（不碰网络和文件），套件的单元测试直接测它；
网络与文件副作用只在 handle_response_create() / serve_ws() / main() 里。
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from datetime import datetime, timezone

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 以下路径由 configure() 设置；导入模块本身不做任何 I/O
DATA = ""
PORT = 443
CTL_PATH = ""
LOG_PATH = ""
FRAME_DIR = ""
CERT = ""
KEY = ""

# 单个原始帧超过这个体积就不落盘（防止测试把磁盘写满）；业务判据只需要密文指纹，不需要超大帧原文
FRAME_DUMP_LIMIT = 16 * 1024 * 1024

_lock = threading.Lock()
_conn_counter = 0
_resp_counter = 0

# 需要记录原值的请求头（小写）；其余只记名字
INTERESTING_HEADERS = (
    "user-agent", "originator", "authorization", "chatgpt-account-id", "openai-beta", "version",
    "session_id", "x-client-request-id", "x-codex-window-id", "x-codex-turn-state", "x-codex-turn-metadata",
    "x-codex-installation-id", "x-openai-subagent", "accept", "host", "sec-websocket-extensions",
)


def configure(data_dir, port=None):
    """设置数据目录与监听端口（只改全局变量，不创建目录）。"""
    global DATA, PORT, CTL_PATH, LOG_PATH, FRAME_DIR, CERT, KEY
    DATA = data_dir
    if port is not None:
        PORT = int(port)
    CTL_PATH = os.path.join(DATA, "ctl.json")
    LOG_PATH = os.path.join(DATA, "requests.jsonl")
    FRAME_DIR = os.path.join(DATA, "frames")
    CERT = os.path.join(DATA, "leaf.crt")
    KEY = os.path.join(DATA, "leaf.key")


configure(os.environ.get("MOCK_DATA", "/data"), os.environ.get("MOCK_PORT", "443"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(rec: dict) -> None:
    rec = dict(ts=now(), **rec)
    with _lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_ctl() -> dict:
    try:
        with open(CTL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ctl(ctl: dict) -> None:
    tmp = CTL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctl, f, ensure_ascii=False)
    os.replace(tmp, CTL_PATH)


# ---------------------------------------------------------------------------------------------
# WebSocket 帧编解码
# ---------------------------------------------------------------------------------------------
def recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf.extend(chunk)
    return bytes(buf)


def read_http_head(sock) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before request head")
        data.extend(chunk)
        if len(data) > 262144:
            raise ValueError("request head too large")
    return bytes(data)


def unmask(payload: bytes, mask: bytes) -> bytes:
    """客户端帧必须带掩码；大帧（几 MiB）用整数异或，避免逐字节 Python 循环。"""
    n = len(payload)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def ws_read_frame(sock):
    """读一个 WebSocket 帧，返回 (fin, opcode, payload)。"""
    b1, b2 = recv_exact(sock, 2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked:
        payload = unmask(payload, mask)
    return fin, opcode, payload


def ws_encode(opcode: int, payload: bytes) -> bytes:
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def ws_send(sock, opcode: int, payload: bytes) -> None:
    sock.sendall(ws_encode(opcode, payload))


def ws_send_json(sock, obj: dict) -> None:
    ws_send(sock, 0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------------------------
# 帧内容分析（纯函数）
# ---------------------------------------------------------------------------------------------
def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def summarize_input(items) -> list:
    """把 input 各项概括成不含正文的结构摘要：类型、角色、密文指纹、content 形态、配对 ID。"""
    out = []
    if not isinstance(items, list):
        return out
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            out.append({"i": index, "type": type(item).__name__})
            continue
        entry = {"i": index, "type": item.get("type"), "role": item.get("role")}
        if "encrypted_content" in item:
            enc = item.get("encrypted_content")
            if isinstance(enc, str):
                entry["enc"] = digest(enc)
                entry["enc_len"] = len(enc)
            else:
                entry["enc"] = "<%s>" % type(enc).__name__
        if "content" in item:
            content = item.get("content")
            entry["content"] = ("list%d" % len(content)) if isinstance(content, list) else type(content).__name__
        if "summary" in item:
            summary = item.get("summary")
            entry["summary"] = ("list%d" % len(summary)) if isinstance(summary, list) else type(summary).__name__
        for key in ("call_id", "id"):
            if key in item:
                entry[key] = item.get(key)
        out.append(entry)
    return out


def iter_encrypted(items):
    """按 input 顺序产出 (index, item, encrypted_content)。"""
    if isinstance(items, list):
        for index, item in enumerate(items):
            if isinstance(item, dict) and isinstance(item.get("encrypted_content"), str):
                yield index, item, item["encrypted_content"]


def hint_text(value: str, ctl: dict) -> str:
    """按场景构造上游错误文案；格式与线上一致：The encrypted content <片段> could not be verified. Reason: ..."""
    mode = ctl.get("hint", "ellipsis")
    n = int(ctl.get("hint_len", 4))
    tail = " could not be verified. Reason: Encrypted content could not be decrypted or parsed."
    if mode == "exact":
        fragment = value
    elif mode == "none":
        return "Encrypted content could not be verified. Reason: Encrypted content could not be decrypted or parsed."
    elif mode == "short":
        fragment = value[:2] + "..." + value[-2:]
    elif mode == "unicode":
        fragment = value[:n] + "…" + value[-n:]
    else:
        fragment = value[:n] + "..." + value[-n:]
    return "The encrypted content " + fragment + tail


def completed_events(resp_id: str, model: str, generate: bool) -> list:
    created = {"type": "response.created", "response": {"id": resp_id, "object": "response", "status": "in_progress", "model": model}}
    usage = {
        "input_tokens": 120 if generate else 0, "output_tokens": 12 if generate else 0, "total_tokens": 132 if generate else 0,
        "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0},
    }
    if not generate:
        return [created, {"type": "response.completed", "response": {"id": resp_id, "object": "response", "status": "completed", "model": model, "output": [], "usage": usage}}]
    item_id = "msg_" + resp_id
    message = {"id": item_id, "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "好的，已收到（假上游）。", "annotations": []}]}
    return [
        created,
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": "好的，已收到（假上游）。"},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": {"id": resp_id, "object": "response", "status": "completed", "model": model, "output": [message], "usage": usage}},
    ]


def plan_response(frame: dict, ctl: dict, resp_id: str) -> dict:
    """纯函数：根据一个已解析的 response.create 帧与控制指令，决定该回什么。

    返回字典：
      action          "drop_connection" | "reject_invalid_encrypted_content" | "complete"
      events          按顺序要发给网关的事件（dict 列表）
      disconnect      发完 events 后是否直接断开 TCP
      consume_drop    True 表示本次消耗了 drop_next 计数（调用方要把 ctl 里的计数减 1 再保存）
      rejected        被拒密文的指纹（仅 reject 时有）
      error_form      reject 时的文案形态
    """
    items = frame.get("input")
    generate = frame.get("generate") is not False
    model = frame.get("model") or "gpt-mock"
    # 断线场景：到达即断开，不应答（预热帧和空历史不算）
    drop = int(ctl.get("drop_next", 0) or 0)
    if drop > 0 and generate and isinstance(items, list) and len(items) > 0:
        return {"action": "drop_connection", "events": [], "disconnect": True, "consume_drop": True}
    reject = [c for c in (ctl.get("reject_ciphers") or []) if isinstance(c, str)]
    hit = None
    for _, _, cipher in iter_encrypted(items):
        if cipher in reject:
            hit = cipher
            break
    if hit is None:
        return {"action": "complete", "events": completed_events(resp_id, model, generate), "disconnect": False, "consume_drop": False}
    message = hint_text(ctl.get("hint_override") or hit, ctl)
    created = {"type": "response.created", "response": {"id": resp_id, "object": "response", "status": "in_progress", "model": model}}
    events = []
    if ctl.get("lifecycle_first") or ctl.get("output_first"):
        events.append(created)
    if ctl.get("toolitem_first"):
        # 只发 function_call 输出项的 added（不发任何 delta），模拟“已开始产出工具调用但未经 delta 事件”
        events.append(created)
        events.append({"type": "response.output_item.added", "output_index": 0, "item": {
            "id": "fc_" + resp_id, "type": "function_call", "call_id": "call_" + resp_id, "name": "lookup", "arguments": "", "status": "in_progress"}})
    if ctl.get("output_first"):
        events.append({"type": "response.output_text.delta", "item_id": "msg_" + resp_id, "output_index": 0, "content_index": 0, "delta": "已经输出的文字"})
    events.append({"type": "error", "status": 400, "error": {
        "type": "invalid_request_error", "code": "invalid_encrypted_content", "message": message, "param": None}})
    if ctl.get("emit_failed"):
        events.append({"type": "response.failed", "response": {"id": resp_id, "status": "failed", "error": {
            "code": "invalid_encrypted_content", "message": message}}})
    return {"action": "reject_invalid_encrypted_content", "events": events, "disconnect": bool(ctl.get("close_after_error")),
            "consume_drop": False, "rejected": digest(hit), "error_form": ctl.get("hint", "ellipsis")}


# ---------------------------------------------------------------------------------------------
# 网络与文件副作用
# ---------------------------------------------------------------------------------------------
def next_resp_id(conn_id: int) -> str:
    global _resp_counter
    with _lock:
        _resp_counter += 1
        return "resp_wsrec_%d_%d" % (conn_id, _resp_counter)


def handle_response_create(sock, conn_id: int, seq: int, text: str, head: dict) -> bool:
    """处理一个 response.create 帧。返回 False 表示应断开连接。"""
    try:
        frame = json.loads(text)
    except Exception as exc:
        log({"kind": "bad_json", "conn": conn_id, "seq": seq, "err": repr(exc)})
        return True
    if not isinstance(frame, dict) or frame.get("type") != "response.create":
        log({"kind": "other_frame", "conn": conn_id, "seq": seq, "type": frame.get("type") if isinstance(frame, dict) else None})
        return True
    ctl = load_ctl()
    items = frame.get("input")
    generate = frame.get("generate") is not False
    os.makedirs(FRAME_DIR, exist_ok=True)
    frame_path = os.path.join(FRAME_DIR, "%d-%d.json" % (conn_id, seq))
    dumped = len(text) <= FRAME_DUMP_LIMIT
    if dumped:
        with open(frame_path, "w", encoding="utf-8") as f:
            f.write(text)
    record = {
        "kind": "frame", "conn": conn_id, "seq": seq, "generate": generate, "model": frame.get("model") or "gpt-mock",
        "keys": sorted(frame.keys()), "prev": frame.get("previous_response_id"),
        "prompt_cache_key": frame.get("prompt_cache_key"), "n_input": len(items) if isinstance(items, list) else None,
        "frame_bytes": len(text.encode("utf-8")),
        "input": summarize_input(items), "frame_file": frame_path if dumped else None,
        "account_hdr": head.get("chatgpt-account-id"), "window": head.get("x-codex-window-id"),
        "session_hdr": head.get("session_id"),
    }
    plan = plan_response(frame, ctl, next_resp_id(conn_id))
    record["action"] = plan["action"]
    if plan.get("rejected"):
        record["rejected_enc"] = plan["rejected"]
        record["error_message_form"] = plan.get("error_form")
    if plan.get("consume_drop"):
        ctl["drop_next"] = int(ctl.get("drop_next", 0) or 0) - 1
        save_ctl(ctl)
    log(record)
    if plan["action"] == "drop_connection":
        return False
    delay = float(ctl.get("delay_ms", 0) or 0) / 1000.0
    if delay > 0:
        time.sleep(delay)
    for event in plan["events"]:
        ws_send_json(sock, event)
    return not plan["disconnect"]


def serve_ws(sock, conn_id: int, head: dict) -> None:
    seq = 0
    fragments = bytearray()
    while True:
        fin, opcode, payload = ws_read_frame(sock)
        if opcode == 0x8:
            ws_send(sock, 0x8, payload[:2])
            log({"kind": "ws_close_from_client", "conn": conn_id, "code": struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None})
            return
        if opcode == 0x9:
            ws_send(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x2):
            fragments = bytearray(payload)
        elif opcode == 0x0:
            fragments.extend(payload)
        if not fin:
            continue
        seq += 1
        text = bytes(fragments).decode("utf-8", errors="replace")
        if not handle_response_create(sock, conn_id, seq, text, head):
            sock.close()
            return


def parse_http_head(head_bytes: bytes):
    """解析 HTTP 请求头，返回 (方法, 路径, 小写头字典)。"""
    head_text = head_bytes.partition(b"\r\n\r\n")[0]
    lines = head_text.decode("latin-1").split("\r\n")
    method, path, _version = (lines[0].split(" ", 2) + ["", ""])[:3]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return method, path, headers


def handle_conn(raw, addr, ctx) -> None:
    global _conn_counter
    with _lock:
        _conn_counter += 1
        conn_id = _conn_counter
    sock = None
    try:
        sock = ctx.wrap_socket(raw, server_side=True)
        sock.settimeout(300)
        method, path, headers = parse_http_head(read_http_head(sock))
        shown = {}
        for name in INTERESTING_HEADERS:
            if name in headers:
                value = headers[name]
                if name == "authorization":
                    value = value[:12] + "..."  # 只留前缀，不记完整令牌
                shown[name] = value
        upgrade = headers.get("upgrade", "").lower() == "websocket"
        log({"kind": "http_request", "conn": conn_id, "method": method, "path": path, "upgrade": upgrade,
             "sni_host": headers.get("host"), "headers": shown, "header_names": sorted(headers.keys())})
        if upgrade:
            accept = base64.b64encode(hashlib.sha1((headers.get("sec-websocket-key", "") + GUID).encode()).digest()).decode()
            response = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        "Sec-WebSocket-Accept: %s\r\n\r\n" % accept)
            sock.sendall(response.encode())
            serve_ws(sock, conn_id, headers)
            return
        # 非 WS 请求：模型清单给最小可用 JSON，其余 404；一律记日志
        body = b"{}"
        status = "404 Not Found"
        if path.startswith("/backend-api/codex/models") or path.startswith("/v1/models"):
            status = "200 OK"
            body = json.dumps({"models": [], "data": [], "object": "list"}).encode()
        sock.sendall(("HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (status, len(body))).encode() + body)
    except (ConnectionError, ssl.SSLError, socket.timeout, OSError) as exc:
        log({"kind": "conn_end", "conn": conn_id, "err": repr(exc)[:200]})
    except Exception as exc:  # 兜底，避免单连接异常拖垮服务
        log({"kind": "conn_error", "conn": conn_id, "err": repr(exc)[:300]})
    finally:
        try:
            (sock or raw).close()
        except Exception:
            pass


def main() -> None:
    os.makedirs(FRAME_DIR, exist_ok=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    ctx.set_alpn_protocols(["http/1.1"])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(128)
    log({"kind": "start", "port": PORT})
    print("wsmock listening on %d" % PORT, flush=True)
    while True:
        raw, addr = srv.accept()
        threading.Thread(target=handle_conn, args=(raw, addr, ctx), daemon=True).start()


if __name__ == "__main__":
    main()
