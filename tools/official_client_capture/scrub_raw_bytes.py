#!/usr/bin/env python3
"""把中继原始字节里的活凭据做**等长**占位替换，产出可外发的原始字节副本。

为什么不是直接删或截短
----------------------
外发原始 `.bin` 的价值在于对方能自己解析——header 逐字节顺序、Content-Length
与 body 边界、h2 帧长度、WS 分帧。**任何改变长度的脱敏都会破坏这一点**：
Content-Length 与实际 body 对不上，解析器当场错乱，等于交了一份不能复现的样本。

所以这里只做等长替换：`eyJhbGci…`（512 字节）→ `<secret>XXX…`
（同样 512 字节）。所有偏移不变，所有长度字段仍然自洽，wire 层结论与真品
逐字节一致。

替换什么
--------
只替换**凭据值本身**，不动 header 名、不动结构：
  - Authorization: Bearer <token>      → 值替换
  - Cookie: …                           → 值替换
  - chatgpt-account-id: <uuid>          → 值替换（账号标识）
  - set-cookie（响应侧）                → 值替换
  - 敏感 URL query 参数值              → 值替换（尤其预签名上传 SAS）

**不替换** session-id / thread-id：它们是每次会话新生成的随机 UUID，不是凭据，
且 SPEC-HDR-007 要验的正是它们的形式与位置，替换掉反而毁了证据。

用法：python3 scrub_raw_bytes.py --src runs/ --dst runs-scrubbed/ [--verify]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

# (正则, 用哪个捕获组作为待替换的值)
# 每条都以 `(?i)` 大小写不敏感——h1 头大小写不定，正是我们要保留的形态特征。
RULES: list[tuple[re.Pattern[bytes], int]] = [
    (re.compile(rb"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9_.\-~+/=]+)"), 2),
    (re.compile(rb"(?i)(\bcookie:\s*)([^\r\n]+)"), 2),
    (re.compile(rb"(?i)(chatgpt-account-id:\s*)([^\r\n]+)"), 2),
    (re.compile(rb"(?i)(set-cookie:\s*)([^\r\n]+)"), 2),
    # WS/JSON 帧里以字段形式出现的 token
    (re.compile(rb'("(?:access_token|id_token|refresh_token)"\s*:\s*")([^"]+)'), 2),
    # 上游在 POST /oauth/token 响应里下发的 identity-signal 令牌，形如
    # `ois1.<段>.<段>`，同时出现在 x-oai-is-update 响应头与响应体的 oai_is 字段。
    # k52 的 A13 首次观测到它——k49／k50／k51 的同一 job 都没有，属上游新增下发；
    # 首版脱敏规则不认这两个名字，令牌以明文留在 relay 原始字节里，被 seal 的
    # jwt-shape 规则当场拦下。它是与账号绑定的短期凭据，必须与 access_token 同等对待。
    (re.compile(rb"(?i)(x-oai-is-update:\s*)([^\r\n]+)"), 2),
    (re.compile(rb'("oai_is"\s*:\s*")([^"]+)'), 2),
    # 分页游标：base64 解开是 {"scope":…,"creator_account_user_id":…}，含账号标识。
    # 出现在两处——响应体的 JSON 字段，与请求 URL 的 query。首版规则漏了这两个，
    # 复扫时残留 755 处（403 + 352）。
    (re.compile(rb'("(?:next_page_token|pageToken|page_token)"\s*:\s*")([^"]+)'), 2),
    (re.compile(rb"([?&](?:pageToken|page_token|next_page_token)=)([^&\s\r\n]+)"), 2),
    # 预签名上传 URL 的 query 本身就是短期凭据。它同时出现于第一跳响应 JSON 的
    # upload_url 与第二跳 PUT 请求行；只按 header/token 脱敏会把完整 SAS 签名带回
    # 本地。除已确认无凭据语义的固定参数白名单外，query 值一律等长替换；参数名、
    # 顺序、分隔符与字节偏移保持不变。终止字符包含双引号，以免 JSON URL 后面的
    # 语法被误吞。
    (re.compile(
        rb"([?&](?!(?:scope|limit|beta|client_version|platform|include_metadata|"
        rb"includeMetadata|intent|architecture)=)[A-Za-z0-9_.~-]+=)"
        rb"([^&\s\r\n\"]+)"
    ), 2),
]

# 通用 token 特征——仅用于复扫告警。加密的 TLS 记录与压缩流里会有随机字节碰巧
# 以 `eyJ` 开头，那不是凭据；因此复扫时只统计**前面有明确字段名**的命中。
#
# `ois1.` 也算明确前缀：identity-signal 令牌的形态是 `ois1.eyJ<载荷>.<签名>`，
# JWT 主体前面隔着这个前缀，原先只认 bearer／引号／等号的前瞻一律漏过——k52 的
# A13 因此复扫报 0 残留，却被 evidence guard 的 jwt-shape 拦下。复扫与 guard 判据
# 一旦不齐，漏网的是真凭据而不是告警。
GENERIC_TOKEN = re.compile(
    rb"(?:bearer\s+|[\"']|=|ois1\.)(eyJ[A-Za-z0-9_\-]{40,}|sk-[A-Za-z0-9]{20,})", re.I
)

# 占位前缀须同时满足两件事：一眼可知是脱敏值，并被最终 evidence guard 明确认作
# 安全占位。剩余长度仍用 X 填充，因此总长度、Content-Length 和后续字节偏移不变。
SECRET_MARKER = b"<secret>"
FILL = b"X"
HTTP_START_LINE = re.compile(
    rb"(?:[A-Z]+\s+\S+\s+HTTP/1\.[01]|HTTP/1\.[01]\s+\d{3}(?:\s+.*)?)"
)
CONTENT_LENGTH_HEADER = re.compile(rb"(?im)^content-length:\s*(\d+)\s*$")
ZSTD_CONTENT_ENCODING_HEADER = re.compile(
    rb"(?im)^content-encoding:\s*[^\r\n]*\bzstd\b[^\r\n]*\r?$"
)
WEBSOCKET_UPGRADE_HEADER = re.compile(rb"(?im)^upgrade:\s*websocket\s*\r?$")
PERMESSAGE_DEFLATE_HEADER = re.compile(
    rb"(?im)^sec-websocket-extensions:\s*[^\r\n]*\bpermessage-deflate\b[^\r\n]*\r?$"
)


def placeholder(length: int) -> bytes:
    """返回恰好 ``length`` 字节的安全等长占位。"""

    if length >= len(SECRET_MARKER):
        return SECRET_MARKER + FILL * (length - len(SECRET_MARKER))
    return FILL * length


def _zstd_body_spans(data: bytes) -> list[tuple[int, int]]:
    """定位 HTTP/1.x 消息里的 zstd body，避免把压缩字节误当明文凭据。

    relay 文件可能连续包含多个 HTTP 消息，因此按 Content-Length 逐条前进；
    无法闭合解析时停止保护，交给最终秘密扫描失败关闭。
    """

    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(data):
        header_end = data.find(b"\r\n\r\n", cursor)
        if header_end < 0:
            break
        first_line_end = data.find(b"\r\n", cursor, header_end)
        if first_line_end < 0 or not HTTP_START_LINE.fullmatch(
            data[cursor:first_line_end]
        ):
            break
        header = data[cursor:header_end]
        is_zstd = ZSTD_CONTENT_ENCODING_HEADER.search(header) is not None
        length_match = CONTENT_LENGTH_HEADER.search(header)
        if length_match is None:
            if is_zstd:
                raise ValueError("zstd HTTP 消息缺少可验证的 Content-Length")
            break
        body_start = header_end + 4
        body_end = body_start + int(length_match.group(1))
        if body_end > len(data):
            if is_zstd:
                raise ValueError("zstd HTTP 消息体短于 Content-Length")
            break
        if is_zstd:
            spans.append((body_start, body_end))
        cursor = body_end
    return spans


def _websocket_deflate_spans(data: bytes) -> list[tuple[int, int]]:
    """保护已协商 permessage-deflate 的 WS 帧区，避免把压缩字节当明文改写。

    OAuth 凭据位于握手 header，仍由既有规则等长脱敏。握手后的数据帧已经按
    permessage-deflate 压缩，直接对压缩字节跑 query／JSON 正则既不能可靠找到
    凭据，还可能把随机字节误判成 ``?name=value`` 并破坏 deflate 流。
    """

    header_end = data.find(b"\r\n\r\n")
    if header_end < 0:
        return []
    first_line_end = data.find(b"\r\n", 0, header_end)
    if first_line_end < 0 or not HTTP_START_LINE.fullmatch(data[:first_line_end]):
        return []
    header = data[:header_end]
    if (
        WEBSOCKET_UPGRADE_HEADER.search(header) is None
        or PERMESSAGE_DEFLATE_HEADER.search(header) is None
    ):
        return []
    frame_start = header_end + 4
    return [(frame_start, len(data))] if frame_start < len(data) else []


def _merge_protected_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并压缩区间，确保后续切片不重复或倒退。"""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _split_protected_chunks(data: bytes) -> list[tuple[bytes, bool]]:
    """把原始字节拆成可扫描区与压缩区，两阶段使用同一边界。"""

    protected = _merge_protected_spans(
        _zstd_body_spans(data) + _websocket_deflate_spans(data)
    )
    chunks: list[tuple[bytes, bool]] = []
    cursor = 0
    for start, end in protected:
        chunks.append((data[cursor:start], False))
        chunks.append((data[start:end], True))
        cursor = end
    chunks.append((data[cursor:], False))
    return chunks


def count_unscrubbed_credentials(data: bytes) -> tuple[int, int]:
    """只复扫可解释明文字节，返回规则残留数和通用 token 命中数。"""

    leftover = 0
    generic = 0
    for chunk, is_protected in _split_protected_chunks(data):
        if is_protected:
            continue
        for pattern, group in RULES:
            for match in pattern.finditer(chunk):
                value = match.group(group)
                safe = value.startswith(SECRET_MARKER) and not (
                    set(value[len(SECRET_MARKER):]) - set(FILL)
                )
                if not safe and set(value) - set(FILL):
                    leftover += 1
        generic += len(GENERIC_TOKEN.findall(chunk))
    return leftover, generic


def scrub(data: bytes) -> tuple[bytes, int]:
    """等长替换，返回 (新字节, 替换处数)。"""
    count = 0

    def repl(m: re.Match[bytes], group: int) -> bytes:
        nonlocal count
        count += 1
        whole = m.group(0)
        value = m.group(group)
        # 用同样长度的占位替换值本身，前缀（header 名与冒号空格）原样保留
        return whole[: len(whole) - len(value)] + placeholder(len(value))

    rewritten: list[bytes] = []
    for chunk, is_protected in _split_protected_chunks(data):
        if is_protected:
            rewritten.append(chunk)
            continue
        out = chunk
        for pattern, group in RULES:
            out = pattern.sub(lambda m, g=group: repl(m, g), out)
        rewritten.append(out)
    out = b"".join(rewritten)
    return out, count


def rewrite_relay_manifest(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """复制 relay.json，并把 bytes／SHA-256 改成脱敏副本的真实值。"""
    source = src / "relay.json"
    if not source.is_file():
        return False
    # 失败场景可能在收到完整请求前没有生成任何 *.bin；此时上面的逐文件循环
    # 不会顺带创建 dst，但 relay.json 仍应能被安全复制，恢复钩子不能再抛异常。
    dst.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(source.read_text(encoding="utf-8"))
    for connection in manifest.get("connections", []):
        connection_id = connection.get("connection_id")
        if not isinstance(connection_id, int):
            continue
        sizes: dict[str, int] = {}
        digests: dict[str, str] = {}
        for direction in ("client_to_upstream", "upstream_to_client"):
            path = dst / f"conn{connection_id:03d}.{direction}.bin"
            if not path.is_file():
                continue
            sizes[direction] = path.stat().st_size
            digests[direction] = hashlib.sha256(path.read_bytes()).hexdigest()
        connection["bytes"] = sizes
        connection["sha256"] = digests
    manifest["credential_scrubbing"] = {
        "method": "equal_length_replacement",
        "byte_offsets_preserved": True,
        "hashes_recomputed": True,
    }
    output = dst / "relay.json"
    descriptor = output.open("w", encoding="utf-8")
    try:
        json.dump(manifest, descriptor, ensure_ascii=False, indent=2)
    finally:
        descriptor.close()
    output.chmod(0o600)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--verify", action="store_true",
                    help="替换后复扫，确认无残留且长度未变")
    args = ap.parse_args()

    src = pathlib.Path(args.src)
    dst = pathlib.Path(args.dst)
    files = sorted(src.rglob("*.bin"))
    total_hits = 0
    length_mismatch = []

    for f in files:
        raw = f.read_bytes()
        new, hits = scrub(raw)
        total_hits += hits
        if len(new) != len(raw):
            length_mismatch.append(str(f))
        rel = f.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new)

    manifest_rewritten = rewrite_relay_manifest(src, dst)

    print(f"处理 {len(files)} 个文件，等长替换 {total_hits} 处凭据")
    if length_mismatch:
        print(f"❌ {len(length_mismatch)} 个文件长度发生变化——脱敏破坏了字节对齐：",
              file=sys.stderr)
        for f in length_mismatch[:5]:
            print(f"   {f}", file=sys.stderr)
        return 1
    print("✅ 全部文件长度未变，字节对齐保持")
    if manifest_rewritten:
        print("✅ relay.json 已复制，并按脱敏字节重算 bytes／SHA-256")

    if args.verify:
        leftover = 0
        generic = 0
        for f in sorted(dst.rglob("*.bin")):
            file_leftover, file_generic = count_unscrubbed_credentials(
                f.read_bytes()
            )
            leftover += file_leftover
            generic += file_generic
        print(f"复扫残留凭据：{leftover} 处")
        print(f"通用 token 特征残留：{generic} 处（仅计前面有字段名的，"
              f"排除加密流里的偶然匹配）")
        if leftover or generic:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
