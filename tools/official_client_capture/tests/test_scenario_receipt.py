"""SCN-REALITY-01：job 退出成功不等于目标协议分支已触发。

k36 的形态是 19 个 job 全部被编排器记为 complete，而 A11／A13／A14 一跳都没走到
各自的目标分支——A11 call-create 返回 400、A13 只发生业务请求、A14 的工具根本没被
调用。本测试的核心回归用例就是这个形态：退出码 0、证据目录非空、收据缺失，必须判
failed。

夹具一律用真实字节流构造（明文 H1 报文 + 手工封装的 pcap），不用 mock 替代解析——
被测的正是解析本身。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.official_client_capture import build_scenario_facts as facts_builder
from tools.official_client_capture import codex_upgrade as cu
from tools.official_client_capture import codex_upgrade_receipt_finalizer as finalizer
from tools.official_client_capture import scenario_receipts

TOOL_ROOT = Path(__file__).parents[1]
CAMPAIGN_ID = "codex-0-147-0-20260810T000000Z"
ATTEMPT_ID = "20260810T010203Z-0123456789abcdef"
RUN_NONCE = "b" * 64
RUN_ID = "campaign-official-oauth-refresh"
JOB_ID = "official-relay-oauth-refresh"
# A14 区域连接的捕获时刻，夹具据此构造 create → PUT → uploaded 的真实先后。
REGIONAL_TS = 1_786_000_000.0


def _pcap(records: list[tuple[float, str]]) -> bytes:
    """构造含 TLS ClientHello 的 Ethernet pcap；records 是 (捕获时刻, SNI)。"""

    out = bytearray()
    # 全局头：小端、微秒精度、LINKTYPE_ETHERNET
    out += struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for captured_at, sni in records:
        packet = _ethernet_client_hello(sni)
        seconds = int(captured_at)
        micros = int(round((captured_at - seconds) * 1_000_000))
        out += struct.pack("<IIII", seconds, micros, len(packet), len(packet))
        out += packet
    return bytes(out)


def _ethernet_client_hello(sni: str) -> bytes:
    hello = _client_hello_record(sni)
    tcp = struct.pack(">HHIIBBHHH", 12345, 443, 0, 0, 5 << 4, 0x18, 65535, 0, 0) + hello
    total = 20 + len(tcp)
    ip = (
        struct.pack(">BBHHHBBH", 0x45, 0, total, 0, 0, 64, 6, 0)
        + bytes([127, 0, 0, 1])
        + bytes([127, 0, 0, 1])
        + tcp
    )
    return b"\x00" * 12 + b"\x08\x00" + ip


def _client_hello_record(sni: str) -> bytes:
    encoded = sni.encode("ascii")
    server_name = b"\x00" + struct.pack(">H", len(encoded)) + encoded
    name_list = struct.pack(">H", len(server_name)) + server_name
    sni_ext = struct.pack(">HH", 0, len(name_list)) + name_list
    alpn_body = b"\x02h2"
    alpn_list = struct.pack(">H", len(alpn_body)) + alpn_body
    alpn_ext = struct.pack(">HH", 16, len(alpn_list)) + alpn_list
    extensions = sni_ext + alpn_ext
    body = (
        b"\x03\x03"
        + b"\x11" * 32
        + b"\x00"
        + struct.pack(">H", 2)
        + b"\x13\x01"
        + b"\x01\x00"
        + struct.pack(">H", len(extensions))
        + extensions
    )
    handshake = b"\x01" + struct.pack(">I", len(body))[1:] + body
    return b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake


def _request(method: str, target: str, body: bytes = b"") -> bytes:
    return (
        f"{method} {target} HTTP/1.1\r\n"
        f"Host: auth.openai.com\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("latin-1") + body


def _response(status: int, body: bytes) -> bytes:
    return (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("latin-1") + body


SDP_ANSWER = (
    b"v=0\r\no=- 3356858930863409685 1786344426 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
    b"a=ice-lite\r\na=group:BUNDLE 0 1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
)


def _call_create_response(call_id: str) -> bytes:
    """WebRTC call-create 的真实形态：201 + Location 头 + text/plain 的 SDP answer。"""

    return (
        f"HTTP/1.1 201 Created\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"location: /v1/realtime/calls/{call_id}\r\n"
        f"access-control-expose-headers: Location, X-Request-Id\r\n"
        f"Content-Length: {len(SDP_ANSWER)}\r\n\r\n"
    ).encode("latin-1") + SDP_ANSWER


def _ws_upgrade(target: str) -> bytes:
    """V3 sideband 的 WS 升级请求：call_id 拼在路径末段，不是 query。"""

    return (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: api.openai.com\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Content-Length: 0\r\n\r\n"
    ).encode("latin-1")


class ScenarioFixtureBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # 生产代码写的是 0600／0700，清理前放开权限。
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                path.chmod(0o755 if path.is_dir() else 0o644)
            except OSError:
                pass
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _run_root(self, name: str = "run") -> Path:
        root = self.root / name
        (root / "relay").mkdir(parents=True, exist_ok=True)
        (root / "direct").mkdir(parents=True, exist_ok=True)
        (root / "scenario-observations").mkdir(parents=True, exist_ok=True)
        return root

    def _write_relay(self, root: Path, index: int, up: bytes, down: bytes) -> None:
        (root / "relay" / f"conn{index:03d}.client_to_upstream.bin").write_bytes(up)
        (root / "relay" / f"conn{index:03d}.upstream_to_client.bin").write_bytes(down)

    def _write_relay_manifest(self, root: Path, connections: list[dict]) -> None:
        (root / "relay" / "relay.json").write_text(
            json.dumps(
                {"schema_version": "byte-relay/v1", "connections": connections},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_observation(self, root: Path, name: str, payload: dict) -> None:
        (root / "scenario-observations" / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _a13_run(self, name: str = "run") -> Path:
        """A13 成功形态：真实 POST /oauth/token + auth SNI + 凭据被刷新改写。"""

        root = self._run_root(name)
        self._write_relay(
            root,
            1,
            _request("POST", "/oauth/token", b'{"grant_type":"refresh_token"}'),
            _response(200, b'{"access_token":"x"}'),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "auth.openai.com")])
        )
        self._write_observation(
            root,
            "A13-jwt-exp.json",
            {
                "exp_at_utc": "2026-08-10T00:04:00Z",
                "observed_at_utc": "2026-08-10T00:00:00Z",
                "trigger": "app_server_refresh_request",
                "token_sha256": "c" * 64,
            },
        )
        # 刷新成功后 CLI 用轮换后的 refresh_token 改写 auth.json，前后摘要必然不同。
        self._write_observation(
            root,
            "A13-credential-restore.json",
            {
                "before_sha256": "d" * 64,
                "after_sha256": "d" * 64,
                "capture_side_wrote_auth": False,
            },
        )
        return root


class ScenarioReceiptSchemaTest(unittest.TestCase):
    """schema 只允许成功态，且顶层与嵌套都闭合。"""

    def setUp(self) -> None:
        self.schema = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenario_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def test_成功态被常量钉死(self) -> None:
        properties = self.schema["properties"]
        self.assertEqual(
            properties["schema_version"]["const"], "codex-egress-scenario-receipt/v1"
        )
        self.assertEqual(properties["status"]["const"], "success")
        self.assertFalse(self.schema["additionalProperties"])

    def test_逐场景_facts_闭合且钉死成功值(self) -> None:
        defs = self.schema["$defs"]
        for name in ("factsA11", "factsA13", "factsA14"):
            self.assertFalse(defs[name]["additionalProperties"], name)
        self.assertEqual(defs["factsA11"]["properties"]["async_error_count"]["const"], 0)
        self.assertEqual(
            defs["factsA11"]["properties"]["sideband_sni"]["const"], "api.openai.com"
        )
        self.assertEqual(
            defs["factsA13"]["properties"]["oauth_sni"]["const"], "auth.openai.com"
        )
        self.assertEqual(
            defs["factsA13"]["properties"]["jwt_exp_observation"]["properties"][
                "trigger"
            ]["enum"],
            ["app_server_refresh_request", "natural_expiry_window"],
        )
        self.assertEqual(
            defs["factsA14"]["properties"]["regional_host_from_response"]["const"], True
        )
        # R3：采集侧不得写 auth.json，刷新必须真的改写凭据。
        restore = defs["factsA13"]["properties"]["credential_restore"]["properties"]
        self.assertEqual(restore["capture_side_wrote_auth"]["const"], False)
        # 不再要求 rotated_by_refresh：规格只约束发往哪个域名，不约束上游回什么。
        self.assertNotIn("rotated_by_refresh", restore)
        self.assertNotIn("uploaded_event", defs["factsA14"]["properties"])

    def test_producer_复用既有收据体系(self) -> None:
        producer = self.schema["$defs"]["producer"]
        self.assertEqual(
            producer["properties"]["schema_version"]["const"],
            "codex-egress-receipt-producer/v1",
        )
        self.assertEqual(producer["properties"]["subcommand"]["const"], "scenario")


class ScenarioFactsPassTest(ScenarioFixtureBase):
    """真实字节夹具能提取出预期字段。"""

    def test_A13_成功形态产出事实(self) -> None:
        root = self._a13_run()
        document = facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self.assertEqual(document["final_state"], "token_refreshed")
        self.assertEqual(document["facts"]["token_request_method"], "POST")
        self.assertEqual(document["facts"]["token_request_path"], "/oauth/token")
        self.assertEqual(document["facts"]["oauth_sni"], "auth.openai.com")
        self.assertTrue(document["evidence_bindings"])
        for binding in document["evidence_bindings"]:
            target = root / binding["path"]
            self.assertTrue(target.is_file())
            self.assertEqual(target.stat().st_size, binding["bytes"])

    def _a11_run(self, sideband_target: str | None = None) -> Path:
        """A11 成功形态：call-create 2xx + V3 sideband 同 call_id + api SNI。"""

        root = self._run_root()
        # 真实形态（k37 观测）：201 Created + Location 头带 call_id + text/plain 的
        # SDP answer 响应体——call_id 不在 JSON 体里。
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/realtime/calls?intent=quicksilver", b"{}"),
            _call_create_response("rtc_u0_EBE4oHU6FYPaFejVfBpPW"),
        )
        # V3 的 sideband 把 call_id 拼在路径末段（methods.rs:985-993）。
        self._write_relay(
            root,
            2,
            _ws_upgrade(sideband_target or "/v1/live/rtc_u0_EBE4oHU6FYPaFejVfBpPW"),
            _response(101, b""),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "api.openai.com")])
        )
        self._write_observation(
            root,
            "A11-realtime-events.json",
            {
                "notifications": [
                    {
                        "method": "thread/realtime/started",
                        "params": {"realtimeSessionId": "sess-1", "version": "v3"},
                    }
                ],
                "requested_version": "v3",
                "negotiated_version": "v3",
            },
        )
        return root

    def test_A11_成功形态产出事实(self) -> None:
        root = self._a11_run()
        document = facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self.assertEqual(document["final_state"], "sideband_established")
        self.assertEqual(document["facts"]["call_create_status"], 201)
        self.assertEqual(
            document["facts"]["call_id_sha256"],
            hashlib.sha256(b"rtc_u0_EBE4oHU6FYPaFejVfBpPW").hexdigest(),
        )
        self.assertEqual(document["facts"]["async_error_count"], 0)
        self.assertTrue(document["facts"]["sideband_call_id_linked"])

    def test_A11_接受_V1_形态的_query_call_id(self) -> None:
        """V1／V2 用 query 传 call_id；两种形态都算真实关联。"""

        root = self._a11_run(sideband_target="/v1/realtime?call_id=rtc_u0_EBE4oHU6FYPaFejVfBpPW")
        document = facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self.assertTrue(document["facts"]["sideband_call_id_linked"])

    def test_A14_成功形态区域主机来自响应(self) -> None:
        root = self._a14_run("sdmntprwestus3.oaiusercontent.com")
        document = facts_builder.build("A14", "official-relay-file-upload", RUN_ID, root)
        self.assertEqual(document["final_state"], "upload_chain_complete")
        self.assertEqual(
            document["facts"]["regional_sni"], "sdmntprwestus3.oaiusercontent.com"
        )
        self.assertTrue(document["facts"]["regional_host_from_response"])
        self.assertEqual(
            document["facts"]["put_destination"]["host"],
            document["facts"]["upload_url_source_event"]["host"],
        )

    def _a14_run(self, response_host: str, pcap_host: str | None = None) -> Path:
        root = self._run_root()
        upload_url = f"https://{response_host}/blob/abc"
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/files", b"{}"),
            _response(200, json.dumps({"upload_url": upload_url}).encode("utf-8")),
        )
        self._write_relay(
            root,
            2,
            _request("POST", "/backend-api/files/f-1/uploaded", b"{}"),
            _response(200, b"{}"),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(REGIONAL_TS, pcap_host or response_host)])
        )
        self._write_observation(
            root,
            "A14-tool-call.json",
            {"tool_name": "save_site_version", "tool_call_id": "call-1"},
        )
        # 三跳顺序从 relay.json 的连接墙钟时刻推导，不读脚本写的顺序声明。
        # 区域 PUT 直连不经中继，只在 pcap 里可见，两侧必须有共同的 Unix 基准。
        self._write_relay_manifest(
            root,
            [
                {
                    "connection_id": 1,
                    "opened_at_unix_ms": int((REGIONAL_TS - 60) * 1000),
                    "closed_at_unix_ms": int((REGIONAL_TS - 50) * 1000),
                },
                {
                    "connection_id": 2,
                    "opened_at_unix_ms": int((REGIONAL_TS + 50) * 1000),
                    "closed_at_unix_ms": int((REGIONAL_TS + 60) * 1000),
                },
            ],
        )
        return root


class ScenarioFactsNegativeTest(ScenarioFixtureBase):
    """目标分支未成立时必须失败关闭，且不写任何成功事实。"""

    def _assert_no_facts(self, root: Path, scenario: str) -> None:
        self.assertFalse((root / "scenario-facts" / f"{scenario}-facts.json").exists())

    def test_A11_call_create_400_拒绝产出(self) -> None:
        root = self._run_root()
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/realtime/calls", b"{}"),
            _response(400, b'{"error":"invalid_quicksilver_alpha_header"}'),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "api.openai.com")])
        )
        self._write_observation(
            root,
            "A11-realtime-events.json",
            {"notifications": [{"method": "thread/realtime/error"}], "sideband_call_id": ""},
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self._assert_no_facts(root, "A11")

    def test_A11_sideband_关联到别的_call_id_拒绝产出(self) -> None:
        """sideband 存在但 join 的不是本轮 call_id，不算关联成立。"""

        passer = ScenarioFactsPassTest()
        passer.root = self.root
        root = passer._a11_run(sideband_target="/v1/live/rtc_other")
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self._assert_no_facts(root, "A11")

    def test_A11_无_sideband_连接拒绝产出(self) -> None:
        """只有 call-create 成功、sideband 从未建立，场景不成立。"""

        root = self._run_root()
        # 真实形态（k37 观测）：201 Created + Location 头带 call_id + text/plain 的
        # SDP answer 响应体——call_id 不在 JSON 体里。
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/realtime/calls?intent=quicksilver", b"{}"),
            _call_create_response("rtc_u0_EBE4oHU6FYPaFejVfBpPW"),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "api.openai.com")])
        )
        self._write_observation(
            root,
            "A11-realtime-events.json",
            {"notifications": [{"method": "thread/realtime/started", "params": {}}]},
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self._assert_no_facts(root, "A11")

    def test_A13_只有业务请求没有_refresh_拒绝产出(self) -> None:
        """k36 的 A13 实际形态：只发生 models／Responses，没有 /oauth/token。"""

        root = self._run_root()
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/responses", b"{}"),
            _response(200, b"{}"),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "chatgpt.com")])
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self._assert_no_facts(root, "A13")

    def test_A14_工具未被调用_拒绝产出(self) -> None:
        root = self._run_root()
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/responses", b"{}"),
            _response(200, b"{}"),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "chatgpt.com")])
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A14", "official-relay-file-upload", RUN_ID, root)
        self._assert_no_facts(root, "A14")

    def test_pcap_缺目标_ClientHello_拒绝产出(self) -> None:
        root = self._a13_run()
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "chatgpt.com")])
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self._assert_no_facts(root, "A13")

    def test_pcap_只有全局头_拒绝产出(self) -> None:
        root = self._a13_run()
        (root / "direct" / "traffic.pcap").write_bytes(
            struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self._assert_no_facts(root, "A13")

    def test_A13_采集侧写过_auth_拒绝产出(self) -> None:
        """R3 不接受任何受控篡改——k36 改 last_refresh 正是被否掉的手法。"""

        root = self._a13_run()
        self._write_observation(
            root,
            "A13-credential-restore.json",
            {
                "before_sha256": "d" * 64,
                "after_sha256": "e" * 64,
                "capture_side_wrote_auth": True,
            },
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self._assert_no_facts(root, "A13")

    def test_A14_create_晚于区域连接_拒绝产出(self) -> None:
        """create 必须早于区域连接，否则无法证明 URL 来自响应而非预知。"""

        passer = ScenarioFactsPassTest()
        passer.root = self.root
        root = passer._a14_run("sdmntprwestus3.oaiusercontent.com")
        # 把 create 连接挪到区域连接之后。
        passer._write_relay_manifest(
            root,
            [
                {
                    "connection_id": 1,
                    "opened_at_unix_ms": int((REGIONAL_TS + 60) * 1000),
                    "closed_at_unix_ms": int((REGIONAL_TS + 65) * 1000),
                },
                {
                    "connection_id": 2,
                    "opened_at_unix_ms": int((REGIONAL_TS + 70) * 1000),
                    "closed_at_unix_ms": int((REGIONAL_TS + 80) * 1000),
                },
            ],
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A14", "official-relay-file-upload", RUN_ID, root)
        self._assert_no_facts(root, "A14")

    def test_A14_relay_缺墙钟时刻_拒绝产出(self) -> None:
        """没有共同时间基准就无法证明三跳顺序。"""

        passer = ScenarioFactsPassTest()
        passer.root = self.root
        root = passer._a14_run("sdmntprwestus3.oaiusercontent.com")
        passer._write_relay_manifest(
            root, [{"connection_id": 1}, {"connection_id": 2}]
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A14", "official-relay-file-upload", RUN_ID, root)
        self._assert_no_facts(root, "A14")

    def test_A14_区域_SNI_与响应主机不一致_拒绝产出(self) -> None:
        """模拟 RELAY_HOSTS 预列域名凑出的 SNI：响应返回另一个分片。"""

        passer = ScenarioFactsPassTest()
        passer.root = self.root
        root = passer._a14_run(
            "sdmntprwestus2.oaiusercontent.com",
            pcap_host="sdmntprwestus3.oaiusercontent.com",
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A14", "official-relay-file-upload", RUN_ID, root)
        self._assert_no_facts(root, "A14")


class ScenarioReceiptRuntimeValidationTest(ScenarioFixtureBase):
    """不依赖 jsonschema 的运行时等价校验必须逐字段失败关闭。"""

    def _receipt(self) -> dict:
        root = self._a13_run()
        document = facts_builder.build("A13", JOB_ID, RUN_ID, root)
        evidence_root = self.root / "evidence"
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        facts_copy = evidence_root / "A13-facts.json"
        facts_copy.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        facts_copy.chmod(0o600)
        import argparse

        return finalizer.finalize_scenario(
            argparse.Namespace(
                evidence_root=evidence_root,
                output=evidence_root / "A13-scenario-receipt.json",
                scenario_id="A13",
                job_id=JOB_ID,
                campaign_id=CAMPAIGN_ID,
                attempt_id=ATTEMPT_ID,
                run_nonce=RUN_NONCE,
                run_id=RUN_ID,
                facts=facts_copy,
            )
        )

    def _validate(self, receipt: dict) -> dict:
        return scenario_receipts.validate_receipt(
            receipt,
            scenario_id="A13",
            job_id=JOB_ID,
            campaign_id=CAMPAIGN_ID,
            attempt_id=ATTEMPT_ID,
            run_nonce=RUN_NONCE,
            run_id=RUN_ID,
        )

    def test_完整收据通过校验(self) -> None:
        receipt = self._receipt()
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["final_state"], "token_refreshed")
        self.assertEqual(receipt["campaign_id"], CAMPAIGN_ID)
        self._validate(receipt)

    def test_逐字段_mutation_均失败关闭(self) -> None:
        base = self._receipt()
        mutations = {
            "schema_version": "codex-egress-scenario-receipt/v2",
            "status": "failed",
            "final_state": "sideband_established",
            "scenario_id": "A14",
            "job_id": "official-core",
            "campaign_id": "other-campaign",
            "attempt_id": "other-attempt",
            "run_nonce": "a" * 64,
            "run_id": "other-run",
            "observed_at_utc": "not-a-time",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                mutated = {**base, field: value}
                with self.assertRaises(scenario_receipts.ScenarioReceiptError):
                    self._validate(mutated)

    def test_facts_缺字段或多字段均失败(self) -> None:
        base = self._receipt()
        without = {**base, "facts": {k: v for k, v in base["facts"].items() if k != "oauth_sni"}}
        with self.assertRaises(scenario_receipts.ScenarioReceiptError):
            self._validate(without)
        extra = {**base, "facts": {**base["facts"], "unexpected": 1}}
        with self.assertRaises(scenario_receipts.ScenarioReceiptError):
            self._validate(extra)

    def test_篡改_producer_摘要失败(self) -> None:
        base = self._receipt()
        tampered = {
            **base,
            "producer": {**base["producer"], "command_sha256": "f" * 64},
        }
        with self.assertRaises(scenario_receipts.ScenarioReceiptError):
            self._validate(tampered)

    def test_evidence_bindings_字节不符失败(self) -> None:
        root = self._a13_run("run-b")
        document = facts_builder.build("A13", JOB_ID, RUN_ID, root)
        bad = {
            **document,
            "evidence_bindings": [
                {**document["evidence_bindings"][0], "sha256": "a" * 64}
            ],
        }
        with self.assertRaises(scenario_receipts.ScenarioReceiptError):
            scenario_receipts.validate_facts_document(
                bad,
                scenario_id="A13",
                job_id=JOB_ID,
                run_id=RUN_ID,
                approved_roots={"job_evidence": root},
            )


class ScenarioReceiptReplayTest(ScenarioFixtureBase):
    """seal 阶段的独立复算必须与 run 阶段收据逐字相等。"""

    def setUp(self) -> None:
        super().setUp()
        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir(mode=0o700)
        run_root = self._a13_run()
        document = facts_builder.build("A13", JOB_ID, RUN_ID, run_root)
        self.facts_copy = self.evidence_root / "A13-facts.json"
        self.facts_copy.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.facts_copy.chmod(0o600)
        self.output = self.evidence_root / "A13-scenario-receipt.json"
        import argparse

        self.receipt = finalizer.finalize_scenario(
            argparse.Namespace(
                evidence_root=self.evidence_root,
                output=self.output,
                scenario_id="A13",
                job_id=JOB_ID,
                campaign_id=CAMPAIGN_ID,
                attempt_id=ATTEMPT_ID,
                run_nonce=RUN_NONCE,
                run_id=RUN_ID,
                facts=self.facts_copy,
            )
        )

    def test_五处登记齐备(self) -> None:
        self.assertEqual(
            finalizer.RECEIPT_SUBCOMMAND_BY_SCHEMA[
                "codex-egress-scenario-receipt/v1"
            ],
            "scenario",
        )
        self.assertEqual(finalizer.REPLAY_INPUT_NAMES["scenario"], ("facts",))
        self.assertIn("run_nonce", finalizer.REPLAY_CANONICAL_FIELDS["scenario"])

    def test_重放结果逐字相等(self) -> None:
        replayed = finalizer.replay_receipt(
            self.output, self.evidence_root, expected_subcommand="scenario"
        )
        self.assertEqual(replayed, self.receipt)

    def test_手改收据任一字段后重放报不一致(self) -> None:
        payload = json.loads(self.output.read_text(encoding="utf-8"))
        payload["facts"]["oauth_sni"] = "auth.example.com"
        self.output.chmod(0o600)
        self.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(finalizer.ReceiptFinalizerError):
            finalizer.replay_receipt(
                self.output, self.evidence_root, expected_subcommand="scenario"
            )

    def test_收据不被误路由到_kilo_binding(self) -> None:
        """replay_receipt 的 else 兜底会落到 kilo-binding，必须是显式 elif。"""

        with self.assertRaises(finalizer.ReceiptFinalizerError) as caught:
            finalizer.replay_receipt(
                self.output, self.evidence_root, expected_subcommand="kilo-binding"
            )
        self.assertIn("expected_subcommand", str(caught.exception))


class RunJobScenarioGateTest(ScenarioFixtureBase):
    """run_job 的第四条件。"""

    def _job(self, run_root: Path, receipts: tuple[str, ...]) -> cu.Job:
        return cu.Job(
            job_id=JOB_ID,
            phase="official",
            suites=("full",),
            description="A13 官方 OAuth 刷新",
            steps=(
                {
                    "argv": ["true"],
                    "environment": {"RUN_ID": RUN_ID},
                    "timeout": 60,
                },
            ),
            evidence_roots=(str(run_root),),
            covers=("SPEC-EP-002",),
            scenario_ids=("A13",),
            required=True,
            required_scenario_receipts=receipts,
        )

    def _context(self) -> cu.ScenarioReceiptContext:
        evidence_root = self.root / "attempt" / "evidence"
        evidence_root.mkdir(mode=0o700, parents=True)
        return cu.ScenarioReceiptContext(
            campaign_id=CAMPAIGN_ID,
            attempt_id=ATTEMPT_ID,
            run_nonce=RUN_NONCE,
            evidence_root=evidence_root,
            campaign_dir=self.root / "attempt",
        )

    def _log_root(self) -> Path:
        log_root = self.root / "logs"
        log_root.mkdir(mode=0o700, exist_ok=True)
        return log_root

    def test_收据齐备时_complete(self) -> None:
        run_root = self._a13_run()
        facts_builder.build("A13", JOB_ID, RUN_ID, run_root)
        result = cu.run_job(
            self._job(run_root, ("A13",)), self._log_root(), 1, self._context()
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["scenario_receipts"]), 1)
        self.assertEqual(result["scenario_receipts"][0]["scenario_id"], "A13")
        self.assertEqual(
            result["scenario_receipts"][0]["final_state"], "token_refreshed"
        )
        self.assertEqual(result["scenario_receipt_failures"], [])

    def test_k36_形态_退出码_0_证据非空但收据缺失判_failed(self) -> None:
        """本门禁的核心回归用例。"""

        run_root = self._a13_run()
        result = cu.run_job(
            self._job(run_root, ("A13",)), self._log_root(), 1, self._context()
        )
        self.assertEqual(result["steps"][0]["return_code"], 0)
        self.assertEqual(result["missing_evidence_patterns"], [])
        self.assertEqual(result["empty_evidence_patterns"], [])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            [item["scenario_id"] for item in result["scenario_receipt_failures"]],
            ["A13"],
        )

    def test_未声明收据的任务不受影响(self) -> None:
        run_root = self._a13_run()
        result = cu.run_job(self._job(run_root, ()), self._log_root(), 1, self._context())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scenario_receipts"], [])

    def test_收据身份不匹配判_failed(self) -> None:
        run_root = self._a13_run()
        facts_builder.build("A13", "official-core", RUN_ID, run_root)
        result = cu.run_job(
            self._job(run_root, ("A13",)), self._log_root(), 1, self._context()
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["scenario_receipt_failures"])

    def test_缺少_attempt_上下文判_failed(self) -> None:
        run_root = self._a13_run()
        facts_builder.build("A13", JOB_ID, RUN_ID, run_root)
        result = cu.run_job(self._job(run_root, ("A13",)), self._log_root(), 1, None)
        self.assertEqual(result["status"], "failed")

    def test_收据是软链判_failed(self) -> None:
        run_root = self._a13_run()
        facts_builder.build("A13", JOB_ID, RUN_ID, run_root)
        target = run_root / "scenario-facts" / "A13-facts.json"
        moved = run_root / "scenario-facts" / "real-facts.json"
        target.rename(moved)
        os.symlink(moved, target)
        result = cu.run_job(
            self._job(run_root, ("A13",)), self._log_root(), 1, self._context()
        )
        self.assertEqual(result["status"], "failed")

    def test_补跑写入独立目录不复用旧收据(self) -> None:
        run_root = self._a13_run()
        facts_builder.build("A13", JOB_ID, RUN_ID, run_root)
        context = self._context()
        log_root = self._log_root()
        first = cu.run_job(self._job(run_root, ("A13",)), log_root, 1, context)
        second = cu.run_job(self._job(run_root, ("A13",)), log_root, 2, context)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertIn("retry-1", first["scenario_receipts"][0]["path"])
        self.assertIn("retry-2", second["scenario_receipts"][0]["path"])


class ScenarioExecutionFingerprintTest(unittest.TestCase):
    """门禁要求必须进执行指纹，否则可在同一指纹下被悄改。"""

    def _job(self, receipts: tuple[str, ...]) -> cu.Job:
        return cu.Job(
            job_id=JOB_ID,
            phase="official",
            suites=("full",),
            description="A13",
            steps=({"argv": ["true"], "environment": {}, "timeout": 60},),
            evidence_roots=("/tmp/run",),
            covers=("SPEC-EP-002",),
            scenario_ids=("A13",),
            required=True,
            required_scenario_receipts=receipts,
        )

    def test_声明变化时指纹必须变化(self) -> None:
        self.assertNotEqual(
            cu._job_execution_sha256(self._job(())),
            cu._job_execution_sha256(self._job(("A13",))),
        )


class ScenarioManifestContractTest(unittest.TestCase):
    """场景清单的门禁声明与两份清单的 kinds 一致性。"""

    def setUp(self) -> None:
        self.scenarios = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )
        self.expectations = json.loads(
            (TOOL_ROOT / "candidate_rule_expectations_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )

    def test_三个目标_job_声明真实性收据(self) -> None:
        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        self.assertEqual(
            by_id["official-relay-realtime-webrtc"]["required_scenario_receipts"],
            ["A11"],
        )
        self.assertEqual(
            by_id["official-relay-oauth-refresh"]["required_scenario_receipts"], ["A13"]
        )
        self.assertEqual(
            by_id["official-relay-file-upload"]["required_scenario_receipts"], ["A14"]
        )

    def test_声明必须是自身_scenario_ids_子集(self) -> None:
        for job in self.scenarios["capture_jobs"]:
            self.assertTrue(
                set(job["required_scenario_receipts"]).issubset(set(job["scenario_ids"])),
                job["id"],
            )

    def test_R8_双轨模型_job_与收据契约独立(self) -> None:
        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        pairs = (
            ("official-relay-http-response", "official-lite-http-response"),
            (
                "official-relay-legacy-compact-default",
                "official-lite-legacy-compact-default",
            ),
        )
        for main_id, lite_id in pairs:
            main = by_id[main_id]
            lite = by_id[lite_id]
            self.assertEqual(main["track"], "main")
            self.assertEqual(main["model_id"], "{model}")
            self.assertFalse(main["expected_use_responses_lite"])
            self.assertTrue(main["required_model_receipt"])
            self.assertEqual(lite["track"], "lite")
            self.assertEqual(lite["model_id"], "gpt-5.6-luna")
            self.assertTrue(lite["expected_use_responses_lite"])
            self.assertTrue(lite["required_model_receipt"])
            self.assertNotEqual(main["evidence_roots"], lite["evidence_roots"])
            self.assertEqual(
                main["steps"][0]["environment"]["MODEL_TRACK"], "main"
            )
            self.assertEqual(
                lite["steps"][0]["environment"]["MODEL_TRACK"], "lite"
            )

    def test_R8_turn_state_从_WS_metadata_进入状态仓库(self) -> None:
        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        environment = by_id["official-relay-turnstate-compact"]["steps"][0][
            "environment"
        ]
        self.assertEqual(
            environment["RELAY_INJECT_WS_TURN_STATE"], "upgrade-turn-state"
        )
        self.assertNotIn("RELAY_INJECT_TURN_STATE", environment)

    def test_R8_WS_默认样本不再复用_runtime_metrics(self) -> None:
        labels = json.loads(
            (TOOL_ROOT / "codex_upgrade_evidence_labels_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )
        by_id = {entry["job_id"]: entry for entry in labels["entries"]}
        metrics = [
            rule
            for rule in by_id["official-relay-runtime-metrics"]["rules"]
            if "A06" in rule["scenario_ids"]
        ]
        self.assertEqual(metrics[0]["labels"]["variant"], "runtime_metrics")
        default = by_id["official-relay-ws-default"]["rules"][0]
        self.assertEqual(default["labels"]["variant"], "default")
        self.assertEqual(default["labels"]["track"], "main")

    def test_两份清单的_required_artifact_kinds_逐场景相等(self) -> None:
        """§3.1：分叉会让 A01／A15 的定案在 accept 阶段失效。"""

        left = {
            item["scenario_id"]: item["required_artifact_kinds"]
            for item in self.scenarios["evidence_scenarios"]
        }
        right = {
            item["scenario_id"]: item["required_artifact_kinds"]
            for item in self.expectations["scenarios"]
        }
        self.assertEqual(set(left), set(right))
        for scenario_id in sorted(left):
            self.assertEqual(
                left[scenario_id], right[scenario_id], f"{scenario_id} kinds 分叉"
            )

    def test_file_upload_不再预列区域上传主机(self) -> None:
        """§4.4：硬编码单一区域分片违反规格，且响应返回其他分片时抓不到。"""

        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        hosts = by_id["official-relay-file-upload"]["steps"][0]["environment"][
            "RELAY_HOSTS"
        ]
        self.assertNotIn("oaiusercontent.com", hosts)

    def test_三个目标场景声明分侧触发契约(self) -> None:
        """§3.2：共用一个 trigger 会让 official 沿用候选侧受控手法。"""

        by_id = {
            item["scenario_id"]: item for item in self.scenarios["evidence_scenarios"]
        }
        for scenario_id in ("A11", "A13", "A14"):
            sides = by_id[scenario_id].get("side_triggers")
            self.assertIsNotNone(sides, scenario_id)
            self.assertEqual(set(sides), {"official", "candidate"}, scenario_id)
            for side in ("official", "candidate"):
                self.assertTrue(sides[side]["trigger"].strip())
                self.assertTrue(sides[side]["preconditions"])
        # A13 官方侧必须靠 JWT 自然过期，不能再用 k36 的改 last_refresh 手法。
        official = by_id["A13"]["side_triggers"]["official"]
        self.assertIn("exp", official["trigger"])
        self.assertTrue(
            any("last_refresh" in item for item in official["preconditions"])
        )

    def test_分侧触发缺一侧即失败关闭(self) -> None:
        with self.assertRaises(cu.ConfigurationError):
            cu._validate_side_triggers(
                {
                    "scenario_id": "A13",
                    "side_triggers": {
                        "official": {"trigger": "x", "preconditions": ["y"]}
                    },
                }
            )

    def test_R8_主线模型变量冻结为非_Lite_模型(self) -> None:
        """§10.11.1：主线固定 gpt-5.4，变量默认值不得再回落到 Lite 模型。

        变量默认值是没有显式传参时的权威坐标；留着 gpt-5.6-luna 会让主线在
        default 路径上悄悄变成 Lite 轨道，而标签仍按 non_lite 声明。
        """

        variables = {item["name"]: item for item in self.scenarios["variable_contract"]}
        self.assertEqual(variables["model"]["default"], "gpt-5.4")

    def test_R8_官方标签的_Lite_声明必须由_Lite_轨道_job_支撑(self) -> None:
        """标签 authority 原则：mode=lite 只能出现在固定 Lite 模型的 job 上。

        A03 的 precondition 写 use_responses_lite=true，历史上却由主线模型的 job
        贴 mode=lite——条件从未成立。这条不变量把「声明」与「采集坐标」绑死：
        任何官方 job 想声明 Lite，就必须同时是 lite 轨道、固定 gpt-5.6-luna 且
        产出模型条件收据。
        """

        labels = json.loads(
            (TOOL_ROOT / "codex_upgrade_evidence_labels_0_145_0.json").read_text(
                encoding="utf-8"
            )
        )
        by_id = {job["id"]: job for job in self.scenarios["capture_jobs"]}
        for entry in labels["entries"]:
            if entry["side"] != "official":
                continue
            job = by_id[entry["job_id"]]
            lite_job = (
                job.get("track") == "lite"
                and job.get("model_id") == "gpt-5.6-luna"
                and job.get("expected_use_responses_lite") is True
                and job.get("required_model_receipt") is True
            )
            for rule in entry["rules"]:
                mode = rule["labels"].get("mode")
                track = rule["labels"].get("track")
                if mode == "lite" or track == "lite":
                    self.assertTrue(
                        lite_job,
                        f"{entry['job_id']} 未固定 Lite 模型坐标却声明 Lite",
                    )
                if lite_job:
                    self.assertNotEqual(
                        mode, "non_lite", f"{entry['job_id']} Lite job 声明 non_lite"
                    )
                    self.assertNotEqual(
                        track, "main", f"{entry['job_id']} Lite job 声明主线轨道"
                    )

    def test_R8_两条轨道的证据根与运行标识不相交(self) -> None:
        """§11.1：混用 evidence root 会让主线未触发 Lite 被当成 Lite 失败。"""

        roots: dict[str, str] = {}
        run_ids: dict[str, str] = {}
        for job in self.scenarios["capture_jobs"]:
            if job.get("phase") != "official":
                continue
            for root in job["evidence_roots"]:
                self.assertNotIn(root, roots, f"{job['id']} 与 {roots.get(root)} 共用证据根")
                roots[root] = job["id"]
            run_id = job["steps"][0]["environment"].get("RUN_ID")
            if run_id is not None:
                self.assertNotIn(
                    run_id, run_ids, f"{job['id']} 与 {run_ids.get(run_id)} 共用 RUN_ID"
                )
                run_ids[run_id] = job["id"]

    def test_R8_非_Lite_body_判据只选_Responses_的_POST(self) -> None:
        """§10.11.2：A04＋mode=non_lite 会把启动 models GET 一并选中。

        residency-us 与 runtime-metrics 用整目录 glob 绑 A04，其 relay 字节里必然
        含启动期的 GET /models；不约束 method 与 path，body 断言必然失败，R8 补出
        的非 Lite POST 样本也就白采。
        """

        rules = {rule["rule_id"]: rule for rule in self.expectations["rules"]}
        checks = {
            check["id"]: check
            for check in rules["SPEC-BODY-006"]["checks"]
        }
        for check_id in ("nonlite-fields-present", "nonlite-tools-present"):
            where = {
                (item["path"], item["operator"]): item.get("value")
                for item in checks[check_id]["select"]["where"]
            }
            self.assertEqual(where[("data.method", "equal")], "POST", check_id)
            self.assertEqual(
                where[("data.path", "equal")],
                "/backend-api/codex/responses",
                check_id,
            )


class OfficialCaptureScriptTest(unittest.TestCase):
    """采集脚本的错误传播、抓包范围与恢复证明。"""

    def setUp(self) -> None:
        self.source = (TOOL_ROOT / "run_official_relay_scenario.sh").read_text(
            encoding="utf-8"
        )

    def test_抓包覆盖全部网卡并有预检与校验(self) -> None:
        self.assertIn("tcpdump -i any", self.source)
        self.assertNotIn("tcpdump -i lo", self.source)
        self.assertIn("command -v tcpdump", self.source)
        self.assertIn("verify_pcap", self.source)
        # 24 字节是 pcap 全局头长度，只有头没有包即失败。
        self.assertIn("size <= 24", self.source)
        self.assertIn("tcpdump -nn -r", self.source)

    def test_目标驱动路径不再吞掉退出码(self) -> None:
        self.assertNotIn("--hold \"${REALTIME_HOLD:-20}\" 2>&1 | tail -10 || true", self.source)
        self.assertIn("realtime_status=$?", self.source)
        self.assertIn("exec_status=$?", self.source)
        self.assertIn("--events-output", self.source)

    def test_realtime_显式声明_v3(self) -> None:
        """R2：WebRTC 不传版本会默认 v1，header quicksilver=v1 已被上游拒绝。"""

        self.assertIn('--realtime-version "${REALTIME_VERSION:-v3}"', self.source)

    def test_R8_中继脚本默认模型是主线非_Lite(self) -> None:
        """§10.11.1：默认值是没传 MODEL 时的权威坐标，不能落回 Lite 模型。"""

        self.assertIn("model=${MODEL:-gpt-5.4}", self.source)

    def test_R8_h1_探针清单覆盖两条轨道且_Lite_标志与上游一致(self) -> None:
        """探针是受控上游，模型元数据由它权威给出，写错即等于伪造 Lite 条件。

        清单缺主线模型时 CLI 查不到元数据会落到默认值，采集条件与标签声明脱节；
        k41 的 /models 原文已核实 gpt-5.4=false、gpt-5.6-*=true。
        """

        source = (TOOL_ROOT / "h1_wire_probe.py").read_text(encoding="utf-8")
        start = source.index("MODELS_BODY = (")
        end = source.index(")", source.index("]}'", start))
        literal = source[start:end]
        payload = json.loads(
            "".join(
                line.strip().removeprefix("b'").removesuffix("'")
                for line in literal.splitlines()[1:]
            )
        )
        lite_by_slug = {
            item["slug"]: item["use_responses_lite"] for item in payload["models"]
        }
        self.assertIs(lite_by_slug["gpt-5.4"], False)
        self.assertIs(lite_by_slug["gpt-5.6-luna"], True)

    def test_A13_不再改写_last_refresh(self) -> None:
        """R3：正常 JWT 的 exp 优先于 last_refresh，改后者一次刷新都触发不了。"""

        self.assertNotIn('doc["last_refresh"] = stale', self.source)
        self.assertNotIn("datetime.datetime(2020, 1, 1", self.source)

    def test_A13_走官方显式刷新而不是干等到期(self) -> None:
        """access token 有效期 10 天，等自然到期不现实；官方 app-server 有显式入口。"""

        self.assertIn("__AUTH_REFRESH__", self.source)
        self.assertIn("drive_codex_auth_refresh.py", self.source)
        self.assertIn("auth_refresh_status=$?", self.source)
        self.assertIn("a13_derive_observations", self.source)
        self.assertIn("A13-jwt-exp.json", self.source)
        self.assertIn("A13-credential-restore.json", self.source)
        # 采集前仍要确认凭据可读，探针无输出即失败关闭。
        self.assertIn("a13_probe_jwt", self.source)
        self.assertIn("JWT 探针无输出", self.source)

    def test_A14_用_json_事件流提取工具调用(self) -> None:
        """R4：人读输出取不到 tool/status，必须走 --json 的事件流。"""

        self.assertIn("extract_a14_tool_call", self.source)
        self.assertIn("exec_json_args", self.source)
        # 字段平铺在 item 下，没有 details 这一层——照 Rust 嵌套结构取会一无所获。
        self.assertIn('item.get("type") != "mcp_tool_call"', self.source)
        self.assertIn('item.get("tool")', self.source)
        self.assertIn("A14-tool-call.json", self.source)
        # 模型侧称其为 sites.save_site_version，裸名／带命名空间都要认。
        self.assertIn("qualified", self.source)
        # 业务失败是设计使然（不存在的 project_id），只排除仍在进行中的。
        self.assertIn('{"completed", "failed"}', self.source)

    def test_A14_提示词放行工具检索(self) -> None:
        """k37 实测：禁止调用其他工具会让模型无法先检索出该工具，一个请求都发不出。"""

        self.assertNotIn("不要调用任何其他工具。参数必须是", self.source)
        self.assertIn("这些检索调用是允许且必要的", self.source)
        # 安全约束仍在。
        self.assertIn("不要创建站点、不要发布或部署", self.source)

    def test_A13_探针必须分配_stdin(self) -> None:
        """docker exec 不带 -i 时 heredoc 传不进容器，探针静默输出空。"""

        self.assertIn('docker exec -i "$capture_container" python3 -', self.source)
        self.assertIn("JWT 探针无输出", self.source)

    def test_A13_观测不落_token_本体(self) -> None:
        """只记 exp 与 token 摘要；解析在容器内完成，token 不离开容器。"""

        self.assertIn("token_sha256", self.source)
        self.assertNotIn("access_token\\\":", self.source)
        observation = self.source[self.source.index("a13_observation() {") :]
        observation = observation[: observation.index("\n}\n")]
        self.assertNotIn("access_token", observation)


    def test_cleanup_必须在_set_加e_下跑(self) -> None:
        """set -Eeuo pipefail 下，EXIT trap 里一条非 0 就让整个 cleanup 中止。

        k37 实证：cleanup 执行到第一个 stop_tcpdump 的 return 就停，hosts 从未被
        还原，「环境已恢复」这句从未出现在任何 job 日志里。
        """

        body = self.source[self.source.index("cleanup() {") :]
        body = body[: body.index("\n}\n")]
        self.assertIn("set +e", body)
        # hosts 还原必须排在停进程之前——它是污染后续采集的唯一途径。
        hosts = body.index("for h in ${RELAY_HOSTS:-chatgpt.com}")
        # 找实际调用行（行首缩进），不是注释里提到的名字。
        stop = body.index("\n  stop_tcpdump\n")
        self.assertLess(hosts, stop)

    def test_trap_覆盖信号且还原留下证明(self) -> None:
        self.assertIn("trap cleanup EXIT INT TERM", self.source)
        self.assertIn("restore_auth_json", self.source)
        self.assertIn("auth_before_sha256", self.source)
        self.assertIn("A13-credential-restore.json", self.source)

    def test_三个目标场景接线到事实构建器(self) -> None:
        self.assertIn("build_scenario_facts.py", self.source)
        for scenario_id in ("A11", "A13", "A14"):
            self.assertIn(scenario_id, self.source)
        # 事实构建必须在脱敏与还原之后，否则 evidence_bindings 绑的是旧字节，
        # 且 A13 的还原前后摘要还没产生。这里比的是实际调用点，不是注释。
        scrub = self.source.index('mv -- "$scrubbed_relay"')
        restore = self.source.index("restore_auth_json\n\n# SCN-REALITY-01")
        build = self.source.index('"$capture_tool_root/build_scenario_facts.py"')
        self.assertLess(scrub, restore)
        self.assertLess(restore, build)


class AuthRefreshDriverTest(unittest.TestCase):
    """A13 的显式刷新驱动：走 v2 account/read，不看 exp 窗口。"""

    def setUp(self) -> None:
        self.source = (TOOL_ROOT / "drive_codex_auth_refresh.py").read_text(encoding="utf-8")

    def test_走_v2_的_account_read(self) -> None:
        self.assertIn('srv.call("account/read", {"refreshToken": True}', self.source)
        # v1 的 getAuthStatus 在 v2 模式下不生效，实测调用成功却不发刷新。
        self.assertNotIn('srv.call("getAuthStatus"', self.source)

    def test_触发值与收据枚举一致(self) -> None:
        self.assertIn('"trigger": "app_server_refresh_request"', self.source)
        self.assertIn("app_server_refresh_request", scenario_receipts.A13_TRIGGERS)

    def test_刷新未落盘必须失败(self) -> None:
        self.assertIn("auth.json 未发生变化，刷新没有真正落盘", self.source)
        self.assertIn("return 3", self.source)

    def test_不落_token_本体(self) -> None:
        """只记 exp、token 摘要与文件摘要。"""

        observe = self.source[self.source.index("def _observe_auth") :]
        observe = observe[: observe.index("\ndef ")]
        self.assertIn("token_sha256", observe)
        self.assertNotIn('"access_token": token', observe)


class RelayWallClockTest(unittest.TestCase):
    """R4：区域 PUT 直连不经中继，跨 relay／pcap 排序需要共同的墙钟基准。"""

    def setUp(self) -> None:
        self.source = (TOOL_ROOT / "upstream_byte_relay.py").read_text(encoding="utf-8")

    def test_连接记录带绝对时刻(self) -> None:
        self.assertIn("opened_at_unix_ms", self.source)
        self.assertIn("closed_at_unix_ms", self.source)

    def test_相对_t_ms_仍然保留(self) -> None:
        """新增是纯增量，既有 segments 的相对时间不动。"""

        self.assertIn('"t_ms"', self.source)


class RealtimeDriverV3Test(unittest.TestCase):
    """R2：驱动必须显式走 V3 并等待最终事件。"""

    def setUp(self) -> None:
        self.source = (TOOL_ROOT / "drive_codex_realtime.py").read_text(encoding="utf-8")

    def test_默认版本为_v3_且进入请求参数(self) -> None:
        self.assertIn('choices=["v1", "v2", "v3"], default="v3"', self.source)
        self.assertIn('"version": args.realtime_version', self.source)

    def test_等待最终事件而不是无条件_sleep(self) -> None:
        self.assertIn("wait_for_notification", self.source)
        for method in (
            "thread/realtime/started",
            "thread/realtime/sdp",
            "thread/realtime/error",
            "thread/realtime/closed",
        ):
            self.assertIn(method, self.source)

    def test_不再从空的_start_响应取_call_id(self) -> None:
        """ThreadRealtimeStartResponse 是空对象，call_id 只在 relay 字节里。"""

        self.assertNotIn('started.get("callId")', self.source)
        self.assertIn("realtime_session_id", self.source)
        self.assertIn("negotiated_version", self.source)


if __name__ == "__main__":
    unittest.main()
