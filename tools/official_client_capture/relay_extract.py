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
import sys
import re
from pathlib import Path

SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "chatgpt-account-id",
    "session-id", "thread-id", "x-client-request-id", "x-codex-turn-metadata",
    "x-codex-window-id", "x-codex-installation-id", "x-codex-turn-state",
    "sec-websocket-key", "sec-websocket-accept",
}


def redact(name: str, value: str) -> str:
    low = name.strip().lower()
    if low in SENSITIVE_HEADERS:
        return f"<redacted len={len(value)}>"
    # h2 的 `:path` 伪头带完整 query，与 h1 请求行同样需要按白名单剥离；
    # 早前只在 h1 分支做了这件事，h2 分支的 pageToken 因此漏网。
    if low == ":path" and "?" in value:
        return redact_query(value)
    return value


# query 参数一律**白名单**：只有确认无凭据语义的才保留原值。
# 早前用的是黑名单 `(token|key|secret)=`，既漏掉驼峰写法（`pageToken` 的大写 T
# 匹配不上），也无法预知上游将来新增的参数名——`pageToken=eyJz…` 因此完整落盘。
# 白名单的失败方向是"多脱敏"，黑名单的失败方向是"漏脱敏"，只有前者可接受。
SAFE_QUERY_PARAMS = {
    "scope",           # GLOBAL / WORKSPACE
    "limit",           # 200
    "client_version",  # 0.145.0
    "platform",        # codex
    "include_metadata",
    "includeMetadata",
    # realtime call-create 的两个 query（SPEC-EP-012）——官方硬编码的常量
    # （endpoint/realtime_call.rs:213-224 的 append_query_pair），非凭据。
    # 不加白名单会被遮成 <redacted>，而它们正是要观测的形态。
    "intent",
    "architecture",
}

MAX_DECOMPRESSED_BODY = 64 * 1024 * 1024


def decompress_zstd(raw: bytes) -> bytes:
    """优先使用第三方 zstandard；Python 3.14 起可回退到标准库实现。"""
    try:
        import zstandard
    except ModuleNotFoundError:
        try:
            from compression import zstd
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "缺少 zstd 解压器：请使用 Python 3.14+，或安装 "
                "python3 -m pip install zstandard"
            ) from exc
        decompressor = zstd.ZstdDecompressor()
        output = decompressor.decompress(raw, max_length=MAX_DECOMPRESSED_BODY + 1)
        if len(output) > MAX_DECOMPRESSED_BODY or not decompressor.eof:
            raise ValueError("zstd 解压结果超过 64 MiB 或帧不完整")
        return output
    return zstandard.ZstdDecompressor().decompress(
        raw,
        max_output_size=MAX_DECOMPRESSED_BODY,
    )


def redact_query(target: str) -> str:
    """对 `path?query` 形态按白名单逐参数脱敏。h1 请求行与 h2 `:path` 共用。"""
    path, sep, query = target.partition("?")
    if not sep:
        return target
    kept = []
    for pair in query.split("&"):
        k, eq, v = pair.partition("=")
        kept.append(pair if (not eq or k in SAFE_QUERY_PARAMS)
                    else f"{k}=<redacted len={len(v)}>")
    return f"{path}?{'&'.join(kept)}"


def redact_request_line(line: str) -> str:
    """保留 method 与 path，query 按白名单逐参数处理。"""
    parts = line.split(" ")
    if len(parts) < 2:
        return line
    parts[1] = redact_query(parts[1])
    return " ".join(parts)


def summarize_body(raw: bytes, encoding: str, ctype: str) -> dict | None:
    """只提结构不留取值——body 里有对话内容。"""

    if "zstd" in encoding.lower() and raw:
        try:
            raw = decompress_zstd(raw)
        except Exception as exc:  # noqa: BLE001
            return {"note": f"zstd 解压失败：{type(exc).__name__}"}
    if not raw or "json" not in ctype.lower():
        return None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return {"top_level_type": type(parsed).__name__}
    return {"top_level_fields_in_order": list(parsed.keys()),
            "shape": {k: shape_value(v) for k, v in parsed.items()}}


def shape_value(v, depth: int = 2):
    """把值降维成脱敏形态摘要。

    depth 控制嵌套对象展开几层——展开是为了拿到 `reasoning.context` 这类
    **枚举值**（SPEC-BODY-003 要逐字段核对 Lite 变换）。字符串一律沿用
    ≤24 字符才保留原文的策略，token 是长串，会落到 `<len=N>` 分支。
    """
    if isinstance(v, bool):
        return f"bool:{str(v).lower()}"
    if isinstance(v, str):
        return f"str:{v}" if len(v) <= 24 else f"str:<len={len(v)}>"
    if isinstance(v, (int, float)):
        return f"num:{v}"
    if isinstance(v, list):
        # 数组只展开首元素的形态，长度单独记，避免逐条泄露对话内容。
        # 但额外记下**每个元素的 type 枚举**——判断压缩链路全靠它：
        # V2 的发起帧在 input 里追加一个 {"type":"compaction_trigger"} 项，
        # 只看首元素会漏掉（它排在数组后段）。type 是固定枚举，不含用户内容。
        if depth > 0 and v:
            out = {"_array_len": len(v), "_first": shape_value(v[0], depth - 1)}
            types = [x.get("type") for x in v if isinstance(x, dict) and isinstance(x.get("type"), str)]
            if types:
                # `_types` 保留**前 64 项的顺序**——判断 compaction_trigger 落在
                # 数组哪个位置要靠它。
                out["_types"] = types[:64]
                # ⚠ 但只有前 64 项时**无法复算总数**：SPEC-BODY-007 的项类型分布
                # （最大 78 项）因此不可审计，外部审核重算得出的数与正文对不上。
                # `type` 是固定枚举、不含任何用户内容，没有隐私理由限制长度，
                # 故额外记完整计数与总数。
                out["_types_total"] = len(types)
                counts: dict = {}
                for x in types:
                    counts[x] = counts.get(x, 0) + 1
                out["_types_count"] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
                if len(types) > 64:
                    out["_types_truncated"] = f"顺序仅前 64 项，完整分布见 _types_count"
            return out
        return f"array:<len={len(v)}>"
    if isinstance(v, dict):
        if depth > 0:
            return {k: shape_value(sub, depth - 1) for k, sub in list(v.items())[:12]}
        return "object:{" + ",".join(sorted(v.keys())[:10]) + "}"
    return "null"


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
    客户端原始的 SETTINGS 集合、取值与帧内顺序在转发后已丢失（§2.6）。中继只
    复制字节，故这些全都保留。

    HPACK 解码需按连接维护**单一** Decoder 实例——动态表是跨帧累积的，
    每帧新建解码器会解出错误结果。
    """

    import struct

    try:
        from hpack import Decoder as HPACKDecoder

        decoder = HPACKDecoder()
    except ImportError:
        # ⚠ 缺 hpack 时 HEADERS 帧解不出伪头，H2-007 的伪头顺序**无法复现**。
        # 首版在这里静默置 None 并照常退出 0，产出的 json 里没有伪头字段，
        # 看起来像"该样本没有 HEADERS 帧"——是**假成功**。现在显式警告。
        decoder = None
        print("⚠ 未安装 hpack，H2 HEADERS 帧不会解码——SPEC-H2-007 的伪头顺序"
              "在本次输出中缺失，不是'样本里没有'。装上再跑：pip install hpack",
              file=sys.stderr)

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


WS_OPCODES = {0x0: "CONT", 0x1: "TEXT", 0x2: "BINARY",
              0x8: "CLOSE", 0x9: "PING", 0xA: "PONG"}


def parse_ws_frames(data: bytes) -> list[dict]:
    """解析 WS 帧序列。

    官方 responses 默认走 WebSocket，业务往返（含工具调用的发起与结果回传）
    全在 WS 帧里——只解析 h1/h2 会漏掉整条业务链。

    客户端→服务端的帧带 mask，须解掩码才能读出 payload。这里只提取**结构**：
    TEXT 帧解析为 JSON 后记录顶层字段与事件类型，不留取值。
    """

    # permessage-deflate 默认启用**上下文接管**：滑动窗口跨帧共享，因此必须按
    # 连接维护单一解压器。逐帧新建会让第 2 帧起全部失败——首版即如此。
    try:
        import zlib

        inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    except ImportError:
        inflater = None

    frames, pos = [], 0
    message_index: int | None = None
    message_opcode: int | None = None
    message_compressed = False
    message_payload = bytearray()
    message_fragment_count = 0

    def finish_text_message() -> None:
        """把完整分片消息的结构回填到首个 TEXT 帧。"""

        nonlocal message_index, message_opcode, message_compressed
        nonlocal message_payload, message_fragment_count
        if message_index is None or message_opcode != 0x1:
            message_index = None
            message_opcode = None
            message_compressed = False
            message_payload.clear()
            message_fragment_count = 0
            return
        entry = frames[message_index]
        raw = bytes(message_payload)
        if message_compressed:
            try:
                raw = inflater.decompress(raw + b"\x00\x00\xff\xff")
                entry["compressed"] = "permessage-deflate"
            except Exception as exc:  # noqa: BLE001
                entry["note"] = f"deflate 解压失败: {exc}"
        if "note" not in entry:
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(obj, dict):
                    entry["top_level_fields_in_order"] = list(obj.keys())
                    if "type" in obj:
                        entry["event_type"] = obj["type"]
                    # body 字段只记结构，payload 里含对话内容
                    entry["shape"] = {k: shape_value(v) for k, v in obj.items()}
            except ValueError:
                entry["note"] = "非 JSON TEXT 消息"
        entry["message_complete"] = True
        entry["message_fragment_count"] = message_fragment_count
        entry["message_payload_length"] = len(message_payload)
        message_index = None
        message_opcode = None
        message_compressed = False
        message_payload.clear()
        message_fragment_count = 0

    while pos + 2 <= len(data):
        b0, b1 = data[pos], data[pos + 1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        # RSV1 置位表示 permessage-deflate 压缩（RFC 7692），官方 WS 握手协商了
        # 该扩展，业务帧 payload 因此是 raw deflate——不解压读不出 JSON。
        rsv1 = bool(b0 & 0x40)
        masked, ln = bool(b1 & 0x80), b1 & 0x7F
        cur = pos + 2
        if ln == 126:
            if cur + 2 > len(data):
                break
            ln = int.from_bytes(data[cur:cur + 2], "big"); cur += 2
        elif ln == 127:
            if cur + 8 > len(data):
                break
            ln = int.from_bytes(data[cur:cur + 8], "big"); cur += 8
        mask = b""
        if masked:
            if cur + 4 > len(data):
                break
            mask = data[cur:cur + 4]; cur += 4
        if cur + ln > len(data):
            break
        payload = data[cur:cur + ln]
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        entry = {"opcode": WS_OPCODES.get(opcode, f"0x{opcode:x}"),
                 "fin": fin, "masked": masked, "length": ln, "rsv1_deflate": rsv1}
        frames.append(entry)
        current_index = len(frames) - 1
        if opcode in {0x1, 0x2}:
            if message_index is not None:
                frames[message_index]["note"] = "WS 分片消息未结束即出现新数据帧"
            message_index = current_index
            message_opcode = opcode
            message_compressed = rsv1
            message_payload = bytearray(payload)
            message_fragment_count = 1
            if fin:
                finish_text_message()
        elif opcode == 0x0:
            if message_index is None or rsv1:
                entry["note"] = "WS continuation 缺少起始帧或错误设置 RSV1"
            else:
                message_payload.extend(payload)
                message_fragment_count += 1
                entry["message_start_frame_index"] = message_index
                if fin:
                    finish_text_message()
        pos = cur + ln
    if message_index is not None:
        frames[message_index]["note"] = "WS 分片消息在证据边界前未结束"
    return frames


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
            "request_line": redact_request_line(lines[0]),
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
    # 连接形态统计——**同时是样本完整性的检查项**。
    #
    # ⚠ `response_only` 或 `idle` 不为 0，**就是丢数据的信号**，不是官方行为。
    # HTTP/1.1 的响应必然与请求在同一条 TCP 连接上，所以「有完整响应却没有请求
    # 字节」只可能是上行丢了。成因是记录器不 flush 而采集脚本用 pkill 强杀
    # （已于 2026-07-28 修复，见 upstream_byte_relay.py 的 write()）。
    #
    # 旧注释曾把这批解释成"官方预建 TLS 连接占位"，并据此写出 SPEC-CONN-001 的
    # 错误结论。判据是反的：响应完整恰恰证明上行丢了。
    #
    # **用样本前先看这三个数**：response_only/idle 非 0 的样本只能支持正例
    #（"看到了 X"），不能支持全称或否定命题（"全程没有 Y"、"总共 N 次"）。
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
        if not reqs:
            continue
        rec = {"connection": name, "protocol": "h1", "bytes": len(data), "requests": reqs}
        # WS 握手之后的剩余字节是 WS 帧——业务往返全在这里
        if any("upgrade" in n.lower() for r in reqs for n in r["header_names_in_order"]):
            idx = data.find(b"\r\n\r\n")
            if idx > 0:
                ws = parse_ws_frames(data[idx + 4:])
                if ws:
                    rec["protocol"] = "ws"
                    rec["ws_frames"] = ws
                    rec["ws_frame_count"] = len(ws)
        conns.append(rec)

    # ⚠ 空结果必须显式失败。`connection_stats` 全 0 时
    # （total=0, response_only=0, idle=0）**看起来正好符合"洁净样本"的判据**——
    # 而它其实是"什么都没采到"。外部审核指出："提取器也不执行规格断言，
    # 空目录仍会成功输出零记录"。误把空结果当洁净证据引用，比直接报错危险得多。
    if stats["total"] == 0:
        print(f"🔴 {args.relay_dir} 里没有任何连接记录——目录为空、路径写错，"
              f"或采集根本没跑起来。**这不是洁净样本**，不要引用。", file=sys.stderr)
        sys.exit(2)

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
