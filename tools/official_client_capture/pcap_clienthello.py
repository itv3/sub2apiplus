#!/usr/bin/env python3
"""从 pcap 解析 TLS ClientHello：SNI、扩展类型序列、TCP 目标。

为什么需要这个工具
------------------
N0 通道（被动 pcap）是两条规则的**唯一**证据来源，而它们都是全称命题，
只能靠被动观测覆盖全部出站：

  - SPEC-EP-002 官方 OAuth 的域名分布（⚠ 不是「只访问 chatgpt.com」）
    ——不能用中继样本证明：中继靠 hosts 劫持 chatgpt.com 建立，
      打其他域名的流量根本不进中继，存在采样偏差
  - SPEC-TLS-003 WS 握手 ClientHello 的扩展顺序每次随机

此前这两条的结论是用一次性脚本跑出来的，外部审核者无法复现。工具化于此。

只依赖标准库，不需要 scapy/tshark。

用法：
  python3 pcap_clienthello.py --dir local-analysis/captures/.../direct
  python3 pcap_clienthello.py --dir <目录> --by-subdir   # 按子目录分组统计
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import socket
import struct
import sys

# pcap 链路层类型
LINKTYPE_ETHERNET = 1
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276  # 容器抓包常见，头长 20 字节

# Codex CLI 0.145.0 在 Linux/OpenSSL 默认 HTTP 分支中的已验证扩展顺序。
# 不能拿“当前样本里出现次数最多的顺序”当固定序：当样本全部来自 rustls 且每次
# 随机顺序都只出现一次时，Counter.most_common(1) 仍会随便选出一条，进而把它
# 错标成 native-tls。固定序必须和已知画像逐项相等。
NATIVE_TLS_EXTENSION_ORDER = (
    65281, 0, 11, 10, 35, 22, 23, 13, 43, 45, 51,
)


def iter_timed_packets(path: pathlib.Path):
    """逐包产出 (linktype, 捕获时刻 Unix 秒, 原始字节)。

    时间戳是 SCN-REALITY-01 判定 A14 上传三跳先后顺序的唯一被动来源，因此不能
    像 iter_packets 那样丢弃。纳秒精度 pcap（magic 0xA1B23C4D／0x4D3CB2A1）的
    第二个字段是纳秒而非微秒，除数不同。
    """
    with path.open("rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        if magic == 0x0A0D0D0A:
            print(f"  ⚠ {path.name} 是 pcapng 格式，跳过", file=sys.stderr)
            return
        endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
        divisor = 1_000_000_000 if magic in (0xA1B23C4D, 0x4D3CB2A1) else 1_000_000
        link = struct.unpack(endian + "I", gh[20:24])[0]
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                break
            seconds, fraction, incl, _ = struct.unpack(endian + "IIII", ph)
            data = f.read(incl)
            if len(data) < incl:
                break
            yield link, seconds + fraction / divisor, data


def iter_packets(path: pathlib.Path):
    """逐包产出 (linktype, 原始字节)。只支持经典 pcap，不支持 pcapng。"""
    for link, _, data in iter_timed_packets(path):
        yield link, data


def tcp_payload(link: int, data: bytes):
    """剥链路层与 IP/TCP 头，返回 (目标IP, 目标端口, TCP 载荷)。"""
    if link == LINKTYPE_LINUX_SLL2:
        if len(data) < 20:
            return None
        ethertype, offset = struct.unpack(">H", data[0:2])[0], 20
    elif link == LINKTYPE_LINUX_SLL:
        if len(data) < 16:
            return None
        ethertype, offset = struct.unpack(">H", data[14:16])[0], 16
    elif link == LINKTYPE_ETHERNET:
        if len(data) < 14:
            return None
        ethertype, offset = struct.unpack(">H", data[12:14])[0], 14
    else:
        return None
    if ethertype != 0x0800 or len(data) < offset + 20:  # 只看 IPv4
        return None
    ip = data[offset:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 6:  # 只看 TCP
        return None
    dst = socket.inet_ntoa(ip[16:20])
    tcp = ip[ihl:]
    if len(tcp) < 20:
        return None
    dport = struct.unpack(">H", tcp[2:4])[0]
    return dst, dport, tcp[((tcp[12] >> 4) * 4):]


def parse_client_hello(payload: bytes):
    """返回 (SNI, [扩展类型…])；不是 ClientHello 则返回 None。"""
    if len(payload) < 45 or payload[0] != 0x16:  # TLS handshake record
        return None
    try:
        p = payload[5:]
        if p[0] != 0x01:  # ClientHello
            return None
        i = 4 + 2 + 32                      # 跳过 length + version + random
        i += 1 + p[i]                       # session id
        # cipher suites：原本只是跳过。SPEC-TLS-001 的"30 cipher"与 TLS-002 的
        # "10 cipher"是规则正文里的数字，却只能从 analysis/*.json 侧面核对
        # （外部审核指出："该命令不能完整验证 TLS-001 的 30 cipher + ALPN 空"）。
        # 现在直接从 ClientHello 解出来，让 pcap 自己就能验这两条。
        cs_len = struct.unpack(">H", p[i:i + 2])[0]
        ciphers = [struct.unpack(">H", p[i + 2 + k:i + 4 + k])[0]
                   for k in range(0, cs_len, 2)]
        i += 2 + cs_len
        i += 1 + p[i]                       # compression methods
        ext_len = struct.unpack(">H", p[i:i + 2])[0]
        i += 2
        end = i + ext_len
        exts, sni, alpn = [], None, []
        while i + 4 <= end:
            etype, elen = struct.unpack(">HH", p[i:i + 4])
            i += 4
            exts.append(etype)
            if etype == 0 and elen >= 5:    # server_name
                n = struct.unpack(">H", p[i + 3:i + 5])[0]
                sni = p[i + 5:i + 5 + n].decode("utf-8", "replace")
            elif etype == 16 and elen >= 2:  # ALPN
                j, stop = i + 2, i + elen
                while j < stop:
                    ln = p[j]
                    alpn.append(p[j + 1:j + 1 + ln].decode("ascii", "replace"))
                    j += 1 + ln
            i += elen
        return sni, exts, ciphers, alpn
    except (IndexError, struct.error):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="含 *.pcap 的目录，递归查找")
    ap.add_argument("--by-subdir", action="store_true",
                    help="按一级子目录分组——验 SPEC-TLS-003 时用，"
                         "可看出 codex-ws 与 codex-http 的差异")
    args = ap.parse_args()

    root = pathlib.Path(args.dir)
    files = sorted(root.rglob("*.pcap"))
    if not files:
        print(f"{root} 下没有 .pcap", file=sys.stderr)
        return 1

    snis = collections.Counter()
    dests = collections.Counter()
    ext_seqs = collections.Counter()
    cipher_counts = collections.Counter()
    cipher_by_seq: list[tuple[int, tuple[int, ...], tuple[str, ...]]] = []
    alpn_offers = collections.Counter()
    by_sub: dict[str, list[tuple]] = collections.defaultdict(list)
    total = 0

    for f in files:
        try:
            rel = f.relative_to(root).parts[0]
        except ValueError:
            rel = f.parent.name
        for link, data in iter_packets(f):
            parsed = tcp_payload(link, data)
            if not parsed:
                continue
            dst, dport, payload = parsed
            dests[f"{dst}:{dport}"] += 1
            ch = parse_client_hello(payload)
            if not ch or not ch[1]:
                continue
            sni, exts, ciphers, alpn = ch
            total += 1
            if sni:
                snis[sni] += 1
            ext_seqs[tuple(exts)] += 1
            by_sub[rel].append(tuple(exts))
            cipher_counts[len(ciphers)] += 1
            cipher_by_seq.append((len(ciphers), tuple(exts), tuple(alpn)))
            alpn_offers[tuple(alpn) if alpn else ("（不 offer）",)] += 1

    print(f"解析 {len(files)} 个 pcap，共 {total} 个 ClientHello\n")
    print("=== SNI（验 SPEC-EP-002：域名分布。⚠ 不是「只访问 chatgpt.com」——"
          "token 刷新打 auth.openai.com，realtime 打 api.openai.com）===")
    for s, n in snis.most_common():
        print(f"  {n:>5}×  {s}")
    print("\n=== 对外 TCP 目标（前 5）===")
    for d, n in dests.most_common(5):
        print(f"  {n:>6}×  {d}")

    # ⚠ cipher 数必须与扩展序**关联**输出，分开统计会被误读：
    # `codex-ws` 目录里也有 HTTP 请求（该采集主体的进程同样打 models 等），
    # 所以"30 cipher 有 37 个"里有 16 个来自 codex-ws 目录——
    # **目录名标的是采集主体，不是每条连接的传输类型**。
    # 真正的判据是 TLS 栈与分支条件的组合，不能只看“本样本的众数顺序”：
    #   native-tls HTTP       → 30 cipher + 已知固定序 + 不 offer ALPN
    #   rustls WS             → 10 cipher + 非固定序 + 不 offer ALPN
    #   rustls 自定义 CA HTTP → 10 cipher + 非固定序 + h2,http/1.1
    # 第三种画像在 2026-07-30 的 TLS-002 N0 补采前没有直接 pcap；正因为新样本
    # 的三条随机序均只出现一次，旧的“取众数为固定序”算法会稳定制造一条误报。
    def stack_profile(cipher_count: int, seq: tuple[int, ...], alpn: tuple[str, ...]) -> str:
        if (
            cipher_count == 30
            and seq == NATIVE_TLS_EXTENSION_ORDER
            and not alpn
        ):
            return "native-tls HTTP"
        if cipher_count == 10 and alpn == ("h2", "http/1.1"):
            return "rustls 自定义 CA HTTP"
        if cipher_count == 10 and not alpn:
            return "rustls WS／realtime"
        return "未分类画像"

    combo = collections.Counter(
        (
            stack_profile(n, seq, alpn),
            n,
            "已知固定序" if seq == NATIVE_TLS_EXTENSION_ORDER else "非固定序",
            alpn,
        )
        for n, seq, alpn in cipher_by_seq
    )
    print("\n=== TLS 栈形态（cipher × 扩展序，验 SPEC-TLS-001/002）===")
    for (profile, n, kind, alpn), count in sorted(combo.items()):
        offered = list(alpn) if alpn else ["（不 offer）"]
        print(
            f"  {count:>5} 个：{n} cipher + {kind} + ALPN {offered}"
            f"   → {profile}"
        )
    print("\n=== ALPN offer（验 SPEC-PROTO-001：未配自定义 CA 时不 offer）===")
    for a, c in alpn_offers.most_common():
        print(f"  {c:>5}×  {list(a)}")

    print(f"\n=== 扩展顺序（验 SPEC-TLS-003：WS 握手每次随机）===")
    print(f"共 {len(ext_seqs)} 种不同顺序")
    for i, (seq, n) in enumerate(ext_seqs.most_common(), 1):
        tag = "  ← 已知 native-tls 固定序" if seq == NATIVE_TLS_EXTENSION_ORDER else ""
        print(f"  顺序{i} 出现 {n} 次: {list(seq)}{tag}")

    if args.by_subdir and by_sub:
        print("\n=== 按子目录分组 ===")
        print(f"{'子目录':<16}{'ClientHello':>12}{'已知固定序':>12}{'非固定序':>10}")
        for sub in sorted(by_sub):
            lst = by_sub[sub]
            nf = sum(1 for x in lst if x == NATIVE_TLS_EXTENSION_ORDER)
            print(f"{sub:<16}{len(lst):>12}{nf:>12}{len(lst) - nf:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
