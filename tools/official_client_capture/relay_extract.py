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
    for path in sorted(glob.glob(os.path.join(args.relay_dir, "*client_to_upstream.bin"))):
        data = Path(path).read_bytes()
        if not data:
            continue
        name = os.path.basename(path).split(".")[0]
        if data[:4] == b"PRI ":
            conns.append({"connection": name, "protocol": "h2",
                          "note": "h2 帧解析待实现", "bytes": len(data)})
            continue
        reqs = parse_h1_stream(data)
        if reqs:
            conns.append({"connection": name, "protocol": "h1",
                          "bytes": len(data), "requests": reqs})

    fd = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "relay-extract/v1", "connections": conns},
                  f, ensure_ascii=False, indent=2)
    total = sum(len(c.get("requests", [])) for c in conns)
    print(json.dumps({"connections": len(conns), "requests": total,
                      "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
