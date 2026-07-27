#!/usr/bin/env python3
"""HTTP/2 线形探针：记录候选出站经代理时的 h2 帧层形态。

存在的理由
----------
h2 的 SETTINGS、WINDOW_UPDATE 与伪头顺序都在 TLS 内部，MITM 代理虽然能解密，
但 mitmproxy 会用**自己的** h2 栈重建连接，客户端原始的 SETTINGS 参数集合、取值与
帧内顺序在转发后已经丢失，因此从 mitmproxy 的流量记录里读不到这些。

同时，官方 OpenAI 直连画像是空 ALPN，恒为 HTTP/1.1，**根本不产生 h2 流量**；只有
经代理时 reqwest 才换用含 h2 的 ClientHello。所以要观测 h2 帧层，探针必须同时扮演
两个角色：先作为 HTTP CONNECT 代理接下隧道，再在隧道内作为 h2 服务端完成握手。

它不转发到真实上游，因此不消耗配额、不产生真实业务请求。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import struct
import threading
from pathlib import Path

try:
    from hpack import Decoder as HPACKDecoder
except ImportError:  # 没有 hpack 时仍可采集 SETTINGS，只是伪头顺序缺失
    HPACKDecoder = None

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
HEADER_TERMINATOR = b"\r\n\r\n"
READ_CHUNK = 65536

FRAME_TYPES = {
    0x0: "DATA", 0x1: "HEADERS", 0x2: "PRIORITY", 0x3: "RST_STREAM",
    0x4: "SETTINGS", 0x5: "PUSH_PROMISE", 0x6: "PING", 0x7: "GOAWAY",
    0x8: "WINDOW_UPDATE", 0x9: "CONTINUATION",
}

# 名称取自 RFC 9113 §6.5.2 与 RFC 8441（0x8）。
SETTINGS_NAMES = {
    0x1: "HEADER_TABLE_SIZE", 0x2: "ENABLE_PUSH", 0x3: "MAX_CONCURRENT_STREAMS",
    0x4: "INITIAL_WINDOW_SIZE", 0x5: "MAX_FRAME_SIZE", 0x6: "MAX_HEADER_LIST_SIZE",
    0x8: "ENABLE_CONNECT_PROTOCOL",
}

# 与 h1 探针同口径：产物会被拉回本地归档，绝不能落明文凭据。
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "chatgpt-account-id",
    "session-id", "thread-id", "x-client-request-id", "x-codex-turn-metadata",
    "x-codex-window-id", "x-codex-installation-id", "x-codex-turn-state",
}


def redact(name: str, value: str) -> str:
    if name.strip().lower() in SENSITIVE_HEADERS:
        return f"<redacted len={len(value)}>"
    return value


def secure_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)


def recv_exactly(conn, count: int) -> bytes:
    buffer = b""
    while len(buffer) < count:
        chunk = conn.recv(count - len(buffer))
        if not chunk:
            break
        buffer += chunk
    return buffer


def parse_settings_payload(payload: bytes) -> list[dict]:
    """手工解析以**保留帧内顺序**——h2 库解析成 dict 会丢掉顺序，而顺序正是要观测的。"""

    items = []
    for offset in range(0, len(payload) - 5, 6):
        identifier, value = struct.unpack(">HI", payload[offset:offset + 6])
        items.append({
            "id": identifier,
            "name": SETTINGS_NAMES.get(identifier, f"UNKNOWN_0x{identifier:x}"),
            "value": value,
        })
    return items


# 空 SETTINGS（length=0）与 SETTINGS ACK（flags=0x1）。
# 时序必须正确：ACK 只能在**收到**对端 SETTINGS 之后发。抢先发 ACK 时官方 h2 栈容忍，
# 但 Go 的 x/net/http2 判定为协议错误直接断开，导致采不到 HEADERS 帧的伪头顺序。
SERVER_SETTINGS = struct.pack(">BHBBI", 0, 0, 0x4, 0x0, 0)
SETTINGS_ACK = struct.pack(">BHBBI", 0, 0, 0x4, 0x1, 0)


def handle(connection, records: list, lock: threading.Lock, context: ssl.SSLContext,
           max_frames: int) -> None:
    record: dict = {"frames": []}
    try:
        # ---- 阶段一：HTTP CONNECT 隧道 ----
        buffer = b""
        while HEADER_TERMINATOR not in buffer and len(buffer) < 65536:
            chunk = connection.recv(READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
        request_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        record["connect_request_line"] = request_line
        if not request_line.upper().startswith("CONNECT"):
            record["note"] = "非 CONNECT 请求，探针只处理代理隧道"
            with lock:
                records.append(record)
            return
        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # ---- 阶段二：隧道内 TLS，ALPN 由客户端 offer 决定 ----
        tls_conn = context.wrap_socket(connection, server_side=True)
        record["negotiated_alpn"] = tls_conn.selected_alpn_protocol()
        record["tls_version"] = tls_conn.version()
        if record["negotiated_alpn"] != "h2":
            # 客户端没 offer h2 本身就是结论：说明该路径不产生 h2 流量。
            record["note"] = "未协商 h2，该路径无 h2 帧层可观测"
            with lock:
                records.append(record)
            return

        # ---- 阶段三：读 h2 前言与帧 ----
        preface = recv_exactly(tls_conn, len(PREFACE))
        record["preface_ok"] = preface == PREFACE
        tls_conn.sendall(SERVER_SETTINGS)
        tls_conn.settimeout(15)

        decoder = HPACKDecoder() if HPACKDecoder else None
        while len(record["frames"]) < max_frames:
            head = recv_exactly(tls_conn, 9)
            if len(head) < 9:
                break
            length = int.from_bytes(head[0:3], "big")
            frame_type = head[3]
            flags = head[4]
            stream_id = int.from_bytes(head[5:9], "big") & 0x7FFFFFFF
            payload = recv_exactly(tls_conn, length) if length else b""

            entry = {
                "type": FRAME_TYPES.get(frame_type, f"UNKNOWN_0x{frame_type:x}"),
                "stream_id": stream_id,
                "flags": flags,
                "length": length,
            }
            if frame_type == 0x4 and not (flags & 0x1):
                entry["settings_in_order"] = parse_settings_payload(payload)
                tls_conn.sendall(SETTINGS_ACK)
            elif frame_type == 0x8:
                entry["window_size_increment"] = int.from_bytes(payload, "big") & 0x7FFFFFFF
            elif frame_type == 0x1 and decoder is not None:
                block = payload
                # 跳过 PADDED / PRIORITY 前缀，否则 HPACK 解码会失败。
                if flags & 0x8:
                    block = block[1:-block[0]] if block else block
                if flags & 0x20:
                    block = block[5:]
                try:
                    decoded = decoder.decode(block)
                    entry["header_names_in_order"] = [name for name, _ in decoded]
                    entry["headers"] = [
                        {"name": name, "value": redact(name, value)} for name, value in decoded
                    ]
                except Exception as exc:  # noqa: BLE001 - 解码失败不应中断采集
                    entry["hpack_error"] = str(exc)
            record["frames"].append(entry)
            # 拿到首个 HEADERS 即已覆盖 SETTINGS/WINDOW_UPDATE/伪头三项目标。
            if frame_type == 0x1:
                break

        with lock:
            records.append(record)
    except (OSError, ssl.SSLError) as exc:
        record.setdefault("note", f"连接异常: {exc}")
        with lock:
            records.append(record)
    finally:
        try:
            connection.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True, help="chatgpt.com 证书链 PEM")
    parser.add_argument("--key", required=True, help="对应私钥 PEM")
    parser.add_argument("--port", type=int, default=8888, help="CONNECT 代理监听端口")
    parser.add_argument("--output", required=True, help="记录写入路径（JSON）")
    parser.add_argument("--expect", type=int, default=1, help="收满多少条连接后退出")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--idle-timeout", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=32)
    args = parser.parse_args()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)
    # 同时 offer h2 与 http/1.1：协商结果本身就是要观测的量，不能预设。
    context.set_alpn_protocols(["h2", "http/1.1"])

    records: list = []
    lock = threading.Lock()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", args.port))
    listener.listen(16)
    listener.settimeout(args.timeout)

    try:
        while len(records) < args.expect:
            try:
                raw_conn, _ = listener.accept()
            except socket.timeout:
                break
            thread = threading.Thread(
                target=handle,
                args=(raw_conn, records, lock, context, args.max_frames),
                daemon=True,
            )
            thread.start()
            thread.join(timeout=args.timeout)
            listener.settimeout(args.idle_timeout)
    finally:
        listener.close()

    secure_write(
        Path(args.output),
        json.dumps({"schema_version": "h2-wire-probe/v1", "connections": records},
                   ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps({"captured": len(records), "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
