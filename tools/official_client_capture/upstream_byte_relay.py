#!/usr/bin/env python3
"""真实上游应用字节中继（验证方案 §4.3 的 R 类观测通道）。

存在的理由
----------
此前两种通道各有硬伤：

  - **MITM 代理**必然协商 h2，HPACK 会把 header 强制小写并重排，因此 h1 的大小写
    与顺序**完全不可见**；mitmproxy 还会用自己的 h2 栈重建连接，客户端原始的
    SETTINGS 集合与帧内顺序在转发后丢失。
  - **终结型探针**（h1_wire_probe / h2_wire_probe）自己应答、不转发上游，客户端
    拿不到真实响应就不会有后续动作，凡是依赖模型自主决策的场景（工具调用、生图、
    上下文压缩）请求根本发不出来。

本中继两条 TLS 腿之间**只复制明文应用字节**——不解析、不修改、不重建 HTTP。
因此既有真实交互（能触发完整状态链），又完整保留 h1 的字面大小写与顺序、h2 的
原始帧与 HPACK 动态表演进、WS 的握手与分帧。

它能证明什么、不能证明什么
--------------------------
**能**：h1 请求行/header 字面量/顺序/重复项/body 原始字节；h2 preface、帧序、
SETTINGS、WINDOW_UPDATE、HPACK 原始块；WS 握手与帧；真实上游响应与多轮交互。

**不能**：客户端直连真实上游时的 ServerHello、证书、record 分片与 TCP 时序，
以及 TLS session resumption——这些仍须由被动 pcap（N0）负责。

ALPN 镜像
---------
中继**不得**固定向上游 offer 一个协议列表。必须先窥探客户端实际 offer，再用
**同一列表**与上游握手；客户端没 offer 就不 offer。任一侧协商结果不一致即终止
连接并把该次运行标记为无效——否则会把客户端逼上它本来不走的协议，这本身就是污染。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import ssl
import struct
import time
from pathlib import Path

# ClientHello 的 ALPN 扩展编号（RFC 7301）。
_EXT_ALPN = 0x0010


def parse_client_hello_alpn(data: bytes) -> list[str] | None:
    """从 ClientHello 原始字节里取出 ALPN offer 列表。

    返回 None 表示客户端未携带 ALPN 扩展——此时上游腿也必须不发 ALPN。
    解析失败同样返回 None：宁可不 offer，也不能臆造一个客户端没给的列表。
    """

    try:
        # TLS record: type(1) version(2) length(2)，随后是 handshake
        if len(data) < 43 or data[0] != 0x16:
            return None
        pos = 5
        if data[pos] != 0x01:  # handshake type: client_hello
            return None
        pos += 4  # handshake header
        pos += 2 + 32  # client_version + random
        pos += 1 + data[pos]  # session_id
        pos += 2 + struct.unpack(">H", data[pos:pos + 2])[0]  # cipher_suites
        pos += 1 + data[pos]  # compression_methods
        if pos + 2 > len(data):
            return None
        ext_end = pos + 2 + struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        while pos + 4 <= min(ext_end, len(data)):
            ext_type, ext_len = struct.unpack(">HH", data[pos:pos + 4])
            pos += 4
            if ext_type == _EXT_ALPN:
                body = data[pos:pos + ext_len]
                inner = struct.unpack(">H", body[:2])[0]
                out, cur = [], 2
                while cur < 2 + inner and cur < len(body):
                    n = body[cur]
                    out.append(body[cur + 1:cur + 1 + n].decode("ascii", "replace"))
                    cur += 1 + n
                return out or None
            pos += ext_len
    except (struct.error, IndexError, ValueError):
        return None
    return None


class ByteRecorder:
    """按方向分别落盘原始字节，并记录分片边界与哈希。

    分片边界要单独记：应用字节流本身不含"这是第几次 write"的信息，而分帧行为
    （例如 header 是否与 body 同一次写出）本身就是要观测的形态之一。
    """

    def __init__(self, out_dir: Path, conn_id: int):
        self.dir = out_dir
        self.conn_id = conn_id
        self.files: dict[str, object] = {}
        self.digests: dict[str, "hashlib._Hash"] = {}
        self.offsets: dict[str, int] = {}
        self.segments: list[dict] = []
        self.t0 = time.monotonic()

    def _stream(self, direction: str):
        if direction not in self.files:
            path = self.dir / f"conn{self.conn_id:03d}.{direction}.bin"
            fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            self.files[direction] = os.fdopen(fd, "wb")
            self.digests[direction] = hashlib.sha256()
            self.offsets[direction] = 0
        return self.files[direction]

    def write(self, direction: str, chunk: bytes) -> None:
        f = self._stream(direction)
        f.write(chunk)
        self.digests[direction].update(chunk)
        self.segments.append({
            "direction": direction,
            "t_ms": round((time.monotonic() - self.t0) * 1000, 3),
            "offset": self.offsets[direction],
            "length": len(chunk),
        })
        self.offsets[direction] += len(chunk)

    def close(self) -> dict:
        for f in self.files.values():
            f.flush()
            f.close()
        return {
            "connection_id": self.conn_id,
            "bytes": {d: self.offsets[d] for d in self.offsets},
            "sha256": {d: h.hexdigest() for d, h in self.digests.items()},
            "segments": self.segments,
        }


async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
               rec: ByteRecorder, direction: str) -> None:
    """单向复制。逐块转发并落盘，不做任何解析或缓冲重组。

    背压由 drain() 提供——不能无限缓存，否则慢消费端会把中继撑爆。
    """

    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            rec.write(direction, chunk)
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
        pass
    finally:
        # 半关闭：只关写端，让对向继续把剩余数据送完。
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, ConnectionError):
            pass


class Relay:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output)
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn_seq = 0
        self.records: list[dict] = []

        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.conn_seq += 1
        conn_id = self.conn_seq
        meta: dict = {"connection_id": conn_id}
        rec = ByteRecorder(self.out, conn_id)
        up_r = up_w = None
        try:
            target_host, target_port = self.args.upstream_host, 443

            # ── CONNECT 模式：先接下隧道 ──
            if self.args.mode == "connect":
                head = await reader.readuntil(b"\r\n\r\n")
                line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                meta["connect_request_line"] = line
                if not line.upper().startswith("CONNECT"):
                    meta["error"] = "非 CONNECT 请求"
                    return
                hostport = line.split()[1]
                target_host = hostport.rsplit(":", 1)[0]
                target_port = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 443
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

            # ── 取客户端 ALPN offer ──
            # asyncio 的 TransportSocket 不暴露 recv()，无法在握手前 MSG_PEEK 窥探
            # ClientHello。改由调用方按被测客户端的已知 ALPN 画像显式传入——该值
            # 必须与 N0 被动 pcap 的实测一致，给错等于把客户端逼上它本不走的协议。
            offered = self.args.assume_alpn.split(",") if self.args.assume_alpn else None
            meta["client_alpn_offer"] = offered
            meta["alpn_source"] = "assumed" if offered else "none"

            # ── 上游腿：用**客户端同一份** ALPN 列表握手 ──
            up_ctx = ssl.create_default_context()
            if offered:
                up_ctx.set_alpn_protocols(offered)
            up_r, up_w = await asyncio.open_connection(
                host=self.args.upstream_ip or target_host, port=target_port,
                ssl=up_ctx, server_hostname=target_host)
            up_alpn = up_w.get_extra_info("ssl_object").selected_alpn_protocol()
            meta["upstream_alpn"] = up_alpn

            # ── 客户端腿：只允许协商到上游已选定的同一协议 ──
            if up_alpn:
                self.ctx.set_alpn_protocols([up_alpn])
            # start_tls 会换掉底层 transport，必须从它的返回值取 ssl_object；
            # 继续读原 writer 的 extra_info 会拿到升级前的（None）。
            new_transport = await writer.start_tls(self.ctx)
            if new_transport is not None:
                writer._transport = new_transport  # type: ignore[attr-defined]
            ssl_obj = (new_transport or writer.transport).get_extra_info("ssl_object")
            cli_alpn = ssl_obj.selected_alpn_protocol() if ssl_obj else None
            meta["client_alpn"] = cli_alpn

            if cli_alpn != up_alpn:
                # 两侧不一致即污染：中继会把客户端逼上它本不走的协议。
                meta["error"] = f"ALPN 不一致 client={cli_alpn} upstream={up_alpn}"
                meta["valid"] = False
                return

            meta["valid"] = True
            await asyncio.gather(
                pump(reader, up_w, rec, "client_to_upstream"),
                pump(up_r, writer, rec, "upstream_to_client"),
            )
        except Exception as exc:  # noqa: BLE001 - 单连接失败不应终止整轮采集
            meta["error"] = f"{type(exc).__name__}: {exc}"
            meta.setdefault("valid", False)
        finally:
            meta.update(rec.close())
            self.records.append(meta)
            for w in (writer, up_w):
                try:
                    if w:
                        w.close()
                except OSError:
                    pass

    async def serve(self) -> None:
        # 始终以明文接受：TLS 握手必须发生在窥探 ClientHello 之后，
        # 否则 asyncio 会在回调前就完成握手，拿不到客户端的 ALPN offer。
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.args.port)
        async with server:
            try:
                await asyncio.wait_for(server.serve_forever(), timeout=self.args.timeout)
            except asyncio.TimeoutError:
                pass

    def dump(self) -> None:
        path = self.out / "relay.json"
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema_version": "byte-relay/v1",
                       "mode": self.args.mode,
                       "upstream_host": self.args.upstream_host,
                       "connections": self.records},
                      f, ensure_ascii=False, indent=2)
        print(json.dumps({"connections": len(self.records),
                          "valid": sum(1 for r in self.records if r.get("valid")),
                          "output": str(path)}, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cert", required=True, help="面向客户端的证书链 PEM")
    ap.add_argument("--key", required=True)
    ap.add_argument("--mode", choices=["direct", "connect"], default="direct",
                    help="direct=hosts 劫持；connect=显式 HTTP 代理")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--upstream-host", default="chatgpt.com")
    ap.add_argument("--upstream-ip", default="",
                    help="direct 模式必填：上游真实 IP，绕开被劫持的 hosts")
    ap.add_argument("--output", required=True)
    ap.add_argument("--assume-alpn", default="",
                    help="客户端 ALPN offer（逗号分隔）。asyncio 无法窥探 ClientHello，"
                         "故由调用方按被测客户端的已知画像显式给出；留空表示不 offer。"
                         "给错会把客户端逼上它本不走的协议——须与 pcap 实测一致。")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    relay = Relay(args)
    try:
        asyncio.run(relay.serve())
    finally:
        relay.dump()


if __name__ == "__main__":
    main()
