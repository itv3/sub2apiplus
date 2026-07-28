#!/usr/bin/env python3
"""从字节中继的原始产物中提取**脱敏后的形态摘要**。

存在的理由
----------
中继保留的是完整明文应用字节，其中含 OAuth access token、账号 ID、cookie 与对话
内容。原始字节**不得离开采集服务器**；而验证规格表只需要形态——header 的字面
大小写与出现顺序、body 的字段结构、帧序列。本工具就地做这一步降维。

脱敏口径与既有探针一致（h1_wire_probe 的 SENSITIVE_HEADERS），并额外：

  - body 只保留顶层字段名与类型，长字符串只记长度
  - 请求行保留 method 与 path，但剥掉 query 中的敏感参数值
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "chatgpt-account-id",
    "session-id", "thread-id", "x-client-request-id", "x-codex-turn-metadata",
    "x-codex-window-id", "x-codex-installation-id", "x-codex-turn-state",
    "sec-websocket-key", "sec-websocket-accept",
}


def redact(name: str, value: str) -> str:
    return f"<redacted len={len(value)}>" if name.strip().lower() in SENSITIVE_HEADERS else value


def summarize_body(raw: bytes, encoding: str, ctype: str) -> dict | None:
    """只提结构不留取值——body 里有对话内容。"""

    if "zstd" in encoding.lower() and raw:
        try:
            import zstandard

            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=64 * 1024 * 1024)
        except Exception:  # noqa: BLE001
            return {"note": "zstd 解压失败"}
    if not raw or "json" not in ctype.lower():
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return {"top_level_type": type(parsed).__name__}
    shape = {}
    for k, v in parsed.items():
        if isinstance(v, bool):
            shape[k] = f"bool:{str(v).lower()}"
        elif isinstance(v, str):
            shape[k] = f"str:{v}" if len(v) <= 24 else f"str:<len={len(v)}>"
        elif isinstance(v, (int, float)):
            shape[k] = f"num:{v}"
        elif isinstance(v, list):
            shape[k] = f"array:<len={len(v)}>"
        elif isinstance(v, dict):
            shape[k] = "object:{" + ",".join(sorted(v.keys())[:10]) + "}"
        else:
            shape[k] = "null"
    return {"top_level_fields_in_order": list(parsed.keys()), "shape": shape}


H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

H2_FRAME_TYPES = {
    0x0: "DATA", 0x1: "HEADERS", 0x2: "PRIORITY", 0x3: "RST_STREAM",
    0x4: "SETTINGS", 0x5: "PUSH_PROMISE", 0x6: "PING", 0x7: "GOAWAY",
    0x8: "WINDOW_UPDATE", 0x9: "CONTINUATION",
}

H2_SETTINGS_NAMES = {
    0x1: "HEADER_TABLE_SIZE", 0x2: "ENABLE_PUSH", 0x3: "MAX_CONCURRENT_STREAMS",
    0x4: "INITIAL_WINDOW_SIZE", 0x5: "MAX_FRAME_SIZE", 0x6: "MAX_HEADER_LIST_SIZE",
    0x8: "ENABLE_CONNECT_PROTOCOL",
}


def parse_h2_stream(data: bytes) -> dict:
    """解析 h2 帧序列，保留 SETTINGS 帧内顺序与 HPACK 动态表演进。

    这是中继相对 mitmproxy 的核心优势所在：mitmproxy 会用自己的 h2 栈重建连接，
    客户端原始的 SETTINGS 集合、取值与帧内顺序在转发后已丢失（§2.4）。中继只
    复制字节，故这些全都保留。

    HPACK 解码需按连接维护**单一** Decoder 实例——动态表是跨帧累积的，
    每帧新建解码器会解出错误结果。
    """

    import struct

    try:
        from hpack import Decoder as HPACKDecoder

        decoder = HPACKDecoder()
    except ImportError:
        decoder = None

    frames, pos = [], 0
    if data.startswith(H2_PREFACE):
        pos = len(H2_PREFACE)
    while pos + 9 <= len(data):
        length = int.from_bytes(data[pos:pos + 3], "big")
        ftype, flags = data[pos + 3], data[pos + 4]
        sid = int.from_bytes(data[pos + 5:pos + 9], "big") & 0x7FFFFFFF
        payload = data[pos + 9:pos + 9 + length]
        if len(payload) < length:
            break
        entry = {"type": H2_FRAME_TYPES.get(ftype, f"UNKNOWN_0x{ftype:x}"),
                 "stream_id": sid, "flags": flags, "length": length}
        if ftype == 0x4 and not (flags & 0x1):
            # 手工解析以保留帧内顺序——h2 库解析成 dict 会丢掉顺序，
            # 而顺序本身就是要观测的形态。
            items = []
            for off in range(0, len(payload) - 5, 6):
                ident, val = struct.unpack(">HI", payload[off:off + 6])
                items.append({"name": H2_SETTINGS_NAMES.get(ident, f"UNKNOWN_0x{ident:x}"),
                              "value": val})
            entry["settings_in_order"] = items
        elif ftype == 0x8:
            entry["window_size_increment"] = int.from_bytes(payload, "big") & 0x7FFFFFFF
        elif ftype == 0x7 and length >= 8:
            entry["last_stream_id"] = int.from_bytes(payload[:4], "big") & 0x7FFFFFFF
            entry["error_code"] = int.from_bytes(payload[4:8], "big")
        elif ftype == 0x3 and length >= 4:
            entry["error_code"] = int.from_bytes(payload[:4], "big")
        elif ftype == 0x1 and decoder is not None:
            block = payload
            if flags & 0x8 and block:  # PADDED
                block = block[1:len(block) - block[0]]
            if flags & 0x20:  # PRIORITY
                block = block[5:]
            try:
                decoded = decoder.decode(block)
                entry["header_names_in_order"] = [n for n, _ in decoded]
                entry["headers"] = [{"name": n, "value": redact(n, v)} for n, v in decoded]
            except Exception as exc:  # noqa: BLE001
                entry["hpack_error"] = str(exc)
        frames.append(entry)
        pos += 9 + length
    return {"frames": frames, "frame_count": len(frames)}


def parse_h1_stream(data: bytes) -> list[dict]:
    """从连续字节流里逐个切出 h1 请求，保留字面大小写与出现顺序。"""

    out, pos = [], 0
    while True:
        idx = data.find(b"\r\n\r\n", pos)
        if idx < 0:
            break
        head = data[pos:idx].decode("latin-1", "replace")
        lines = head.split("\r\n")
        if not lines or " HTTP/1." not in lines[0]:
            break
        headers, clen, ctype, cenc = [], 0, "", ""
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            value = value.strip()
            low = name.strip().lower()
            if low == "content-length":
                try:
                    clen = int(value)
                except ValueError:
                    clen = 0
            elif low == "content-type":
                ctype = value
            elif low == "content-encoding":
                cenc = value
            headers.append({"name": name, "value": redact(name, value)})
        body_start = idx + 4
        rec = {
            "request_line": re.sub(r"(token|key|secret)=[^&\s]+", r"\1=<redacted>", lines[0]),
            "header_names_in_order": [h["name"] for h in headers],
            "headers": headers,
        }
        if clen:
            if summary := summarize_body(data[body_start:body_start + clen], cenc, ctype):
                rec["body"] = summary
        out.append(rec)
        pos = body_start + clen
        if clen == 0 and pos <= idx + 4:
            pos = idx + 4
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--relay-dir", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    conns = []
    # 先统计连接形态：官方会预建 TLS 连接占位（上行 0 字节），响应则落在实际
    # 承载请求的那条连接上。这不是管道漏字节——已核验「只有下行」的 19 条其
    # 下行全部以完整 HTTP 响应行开头、残缺续传为 0。
    stats = {"total": 0, "with_request": 0, "response_only": 0, "idle": 0}
    for path in sorted(glob.glob(os.path.join(args.relay_dir, "*client_to_upstream.bin"))):
        down = path.replace("client_to_upstream", "upstream_to_client")
        up_n = os.path.getsize(path)
        down_n = os.path.getsize(down) if os.path.exists(down) else 0
        stats["total"] += 1
        if up_n > 0:
            stats["with_request"] += 1
        elif down_n > 0:
            stats["response_only"] += 1
        else:
            stats["idle"] += 1

    for path in sorted(glob.glob(os.path.join(args.relay_dir, "*client_to_upstream.bin"))):
        data = Path(path).read_bytes()
        if not data:
            continue
        name = os.path.basename(path).split(".")[0]
        if data.startswith(H2_PREFACE):
            conns.append({"connection": name, "protocol": "h2",
                          "bytes": len(data), **parse_h2_stream(data)})
            continue
        reqs = parse_h1_stream(data)
        if reqs:
            conns.append({"connection": name, "protocol": "h1",
                          "bytes": len(data), "requests": reqs})

    fd = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "relay-extract/v1",
                   "connection_stats": stats, "connections": conns},
                  f, ensure_ascii=False, indent=2)
    total = sum(len(c.get("requests", [])) for c in conns)
    print(json.dumps({"connections": len(conns), "requests": total,
                      "connection_stats": stats,
                      "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
