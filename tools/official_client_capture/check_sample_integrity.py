#!/usr/bin/env python3
"""检查中继样本的完整性——采集后立即跑，别等到审核阶段才发现丢数据。

判据
----
`response_only` 或 `idle` 不为 0 就是丢数据。HTTP/1.1 的响应必然与请求在同一条
TCP 连接上，所以「有完整响应却没有请求字节」只可能是上行丢了，不是官方行为。
只有 `relay.json` 同时标为 `valid=true` 与 `expected_upstream_only=true` 的受控断连
可从异常数中扣除；它仍须在任何连接计数结论里单列。

成因是记录器不 flush 而采集脚本用 pkill 强杀（已于 2026-07-28 修复）。
这个 bug 曾让 13 个样本里 10 个带伤，并据此写出过 SPEC-CONN-001 的错误结论。

退出码：0 洁净，1 有丢失。
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: check_sample_integrity.py <relay 目录>", file=sys.stderr)
        return 2
    relay_dir = sys.argv[1]
    expected_upstream_only: set[str] = set()
    manifest_path = os.path.join(relay_dir, "relay.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            for connection in manifest.get("connections", []):
                connection_id = connection.get("connection_id")
                if (isinstance(connection_id, int)
                        and connection.get("expected_upstream_only") is True
                        and connection.get("valid") is True):
                    expected_upstream_only.add(f"conn{connection_id:03d}")
        except (OSError, ValueError):
            # 清单损坏时不能放宽判据，仍按普通异常处理。
            expected_upstream_only.clear()
    conns: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for f in glob.glob(os.path.join(relay_dir, "*.bin")):
        m = re.match(r"(conn\d+)\.(client_to_upstream|upstream_to_client)\.bin",
                     os.path.basename(f))
        if m:
            conns[m.group(1)][m.group(2)] = os.path.getsize(f)

    if not conns:
        print("  ⚠ 没找到 .bin 文件", file=sys.stderr)
        return 2

    both = down_only = up_only = zero = 0
    expected_up_only = 0
    for name, c in conns.items():
        u, d = c.get("client_to_upstream", 0), c.get("upstream_to_client", 0)
        if u and d:
            both += 1
        elif d:
            down_only += 1
        elif u:
            up_only += 1
            if name in expected_upstream_only:
                expected_up_only += 1
        else:
            zero += 1

    print(f"  连接 {len(conns)}｜双向 {both}｜只有下行 {down_only}｜"
          f"只有上行 {up_only}（声明干预 {expected_up_only}）｜两向皆 0 {zero}")
    # up_only 也是异常：请求发出去了却零响应，说明**中继没连上上游**
    # （曾因 hosts 劫持导致中继连回自身，346 条连接全是这个形态，
    # 而首版判据只看 down_only+zero，把它误判成"洁净"）。
    unexpected_up_only = up_only - expected_up_only
    bad = down_only + zero + unexpected_up_only
    if bad:
        detail = []
        if down_only: detail.append(f"{down_only} 条只有下行（上行丢字节）")
        if unexpected_up_only:
            detail.append(f"{unexpected_up_only} 条非预期只有上行（**上游未连通**）")
        if zero: detail.append(f"{zero} 条两向皆 0")
        print(f"  ❌ {bad} 条异常：{'；'.join(detail)}")
        print(f"     本样本**只能支持正例**，不能支持「全程没有 X」「共 N 次」这类命题")
        return 1
    if expected_up_only:
        print("  ✅ 扣除 relay.json 明确声明的受控断连后无非预期缺口；"
              "计数命题必须保留该干预口径")
    else:
        print("  ✅ 洁净，可用于全称与计数命题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
