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

    def _write_observation(self, root: Path, name: str, payload: dict) -> None:
        (root / "scenario-observations" / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def _a13_run(self, name: str = "run") -> Path:
        """A13 成功形态：真实 POST /oauth/token + auth SNI + 逐字还原。"""

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
                "within_refresh_window": True,
                "token_sha256": "c" * 64,
            },
        )
        self._write_observation(
            root,
            "A13-credential-restore.json",
            {"before_sha256": "d" * 64, "after_sha256": "d" * 64, "restored": True},
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
                "within_refresh_window"
            ]["const"],
            True,
        )
        self.assertEqual(
            defs["factsA14"]["properties"]["regional_host_from_response"]["const"], True
        )

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

    def test_A11_成功形态产出事实(self) -> None:
        root = self._run_root()
        self._write_relay(
            root,
            1,
            _request("POST", "/backend-api/codex/realtime/calls", b"{}"),
            _response(201, b'{"call_id":"call-abc"}'),
        )
        (root / "direct" / "traffic.pcap").write_bytes(
            _pcap([(1_780_000_000.0, "api.openai.com")])
        )
        self._write_observation(
            root,
            "A11-realtime-events.json",
            {
                "notifications": [{"method": "thread/realtime/started"}],
                "sideband_call_id": "call-abc",
            },
        )
        document = facts_builder.build("A11", "official-relay-realtime-webrtc", RUN_ID, root)
        self.assertEqual(document["final_state"], "sideband_established")
        self.assertEqual(document["facts"]["call_create_status"], 201)
        self.assertEqual(
            document["facts"]["call_id_sha256"],
            hashlib.sha256(b"call-abc").hexdigest(),
        )
        self.assertEqual(document["facts"]["async_error_count"], 0)

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
        # 三跳时间线必须真实自洽：create → 区域 PUT → uploaded。
        self._write_observation(
            root,
            "A14-upload-sequence.json",
            {
                "create_event": "file_create_response",
                "create_at_utc": facts_builder._utc(REGIONAL_TS - 60),
                "uploaded_at_utc": facts_builder._utc(REGIONAL_TS + 60),
            },
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

    def test_A13_还原摘要不等_拒绝产出(self) -> None:
        root = self._a13_run()
        self._write_observation(
            root,
            "A13-credential-restore.json",
            {"before_sha256": "d" * 64, "after_sha256": "e" * 64, "restored": True},
        )
        with self.assertRaises(facts_builder.ScenarioFactsError):
            facts_builder.build("A13", JOB_ID, RUN_ID, root)
        self._assert_no_facts(root, "A13")

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


if __name__ == "__main__":
    unittest.main()
