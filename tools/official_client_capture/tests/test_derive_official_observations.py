"""ACC-02b 派生器必须与断言器解析同构、确定性可重放，且只产 wire 观测。

派生记录是官方侧 21 条 wire 规则的 coverage 与领域判据来源；若与
``h1_request_stream`` 直接解析不同构，官方证据的语义就被派生器改写了。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_assertion_bundle as bundle  # noqa: E402
import candidate_rule_assertion as assertion  # noqa: E402
import derive_official_observations as derive  # noqa: E402

H1_HTTP_BODY = b'{"model":"gpt-5.6"}'
H1_HTTP_STREAM = (
    b"POST /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Authorization: Bearer secret-token\r\n"
    b"Content-Type: application/json\r\n"
    + f"Content-Length: {len(H1_HTTP_BODY)}\r\n\r\n".encode("ascii")
    + H1_HTTP_BODY
)


def _masked_text_frame(payload: bytes) -> bytes:
    key = b"\x01\x02\x03\x04"
    masked = bytes(
        byte ^ key[index % 4] for index, byte in enumerate(payload)
    )
    return bytes([0x81, 0x80 | len(payload)]) + key + masked


H1_WS_STREAM = (
    b"GET /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Connection: Upgrade\r\n"
    b"Upgrade: websocket\r\n"
    b"Sec-WebSocket-Version: 13\r\n\r\n"
) + _masked_text_frame(b'{"type":"response.created"}')

MITM_LINE = {
    "_captured_at": "2026-08-09T00:00:00+00:00",
    "_category": "models",
    "request": {
        "method": "GET",
        "scheme": "https",
        "host": "chatgpt.com",
        "port": 443,
        "path": "/backend-api/models?client_version=0.147.0&pageToken=eyJzecret",
        "http_version": "HTTP/2.0",
        "headers": [
            ["Host", "chatgpt.com"],
            ["Authorization", "Bearer raw-token"],
            ["Accept", "application/json"],
        ],
        "body": {"length": 0, "json": None},
    },
    "response": {"status": 200},
}
MITM_BODY_LINE = {
    "request": {
        "method": "POST",
        "scheme": "https",
        "host": "chatgpt.com",
        "port": 443,
        "path": "/backend-api/codex/responses",
        "http_version": "HTTP/2.0",
        "headers": [["Host", "chatgpt.com"], ["Content-Type", "application/json"]],
        "body": {
            "length": 34,
            "json": {"model": "gpt-5.6-luna", "stream": True},
        },
    },
    "response": None,
}


class DerivationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="derive-obs-test-"))
        self.addCleanup(self._cleanup)
        self.source_root = self.workdir / "official-run"
        (self.source_root / "relay").mkdir(parents=True)
        (self.source_root / "mitm").mkdir()
        (self.source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(
            H1_HTTP_STREAM
        )
        (self.source_root / "relay" / "conn002.client_to_upstream.bin").write_bytes(
            H1_WS_STREAM
        )
        (self.source_root / "mitm" / "models-http.jsonl").write_text(
            json.dumps(MITM_LINE, ensure_ascii=False)
            + "\n"
            + json.dumps(MITM_BODY_LINE, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        self.roots = {"official-run": self.source_root}
        plan = [
            {
                "root": "official-run",
                "path": "relay/conn001.client_to_upstream.bin",
                "target": "official-run/relay/conn001.client_to_upstream.bin",
            },
            {
                "root": "official-run",
                "path": "relay/conn002.client_to_upstream.bin",
                "target": "official-run/relay/conn002.client_to_upstream.bin",
            },
            {
                "root": "official-run",
                "path": "mitm/models-http.jsonl",
                "target": "official-run/mitm/models-http.jsonl",
            },
        ]
        plan_path = self.workdir / "bundle-plan.json"
        plan_path.write_text(
            json.dumps({"entries": plan}, ensure_ascii=False), encoding="utf-8"
        )
        self.bundle_dir = self.workdir / "assertion-bundle"
        bundle.build_bundle(
            self.roots, bundle.load_plan(plan_path), self.bundle_dir
        )
        self.derivation_plan = [
            {
                "source": "official-run/relay/conn001.client_to_upstream.bin",
                "parser": "h1_request_stream",
                "scenario_id": "A03",
                "kind": "process_trace",
                "target": "derived/A03/conn001.observation.jsonl",
                "connection_id": "conn001",
            },
            {
                "source": "official-run/relay/conn002.client_to_upstream.bin",
                "parser": "h1_request_stream",
                "scenario_id": "A05",
                "kind": "websocket_trace",
                "target": "derived/A05/conn002.observation.jsonl",
                "connection_id": "conn002",
            },
            {
                "source": "official-run/mitm/models-http.jsonl",
                "parser": "mitm_http_jsonl",
                "scenario_id": "A09",
                "kind": "process_trace",
                "target": "derived/A09/models.observation.jsonl",
                "connection_id": "mitm-models",
            },
        ]

    def _cleanup(self) -> None:
        for path in sorted(self.workdir.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o644)
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.workdir.rmdir()

    def _write_derivation_plan(self) -> Path:
        path = self.workdir / "derive-plan.json"
        path.write_text(
            json.dumps({"entries": self.derivation_plan}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _derive(self) -> dict:
        return derive.derive_observations(
            self.bundle_dir,
            derive.load_derivation_plan(self._write_derivation_plan()),
        )

    def _records(self, relative: str) -> list[dict]:
        path = self.bundle_dir / relative
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class DerivationTest(DerivationTestBase):
    def test_h1_derivation_matches_direct_parser(self) -> None:
        self._derive()
        records = self._records("derived/A03/conn001.observation.jsonl")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["schema_version"], derive.OBSERVATION_SCHEMA_VERSION)
        self.assertEqual(record["record_type"], "http_request")
        self.assertEqual(
            record["source_artifacts"],
            ["official-run/relay/conn001.client_to_upstream.bin"],
        )
        direct = assertion._h1_observations(
            self.bundle_dir / "official-run/relay/conn001.client_to_upstream.bin",
            "official-run/relay/conn001.client_to_upstream.bin",
            ["A03"],
            {"connection_id": "conn001"},
            {},
        )
        self.assertEqual(len(direct), 1)
        self.assertEqual(record["data"], direct[0].data)
        self.assertEqual(
            record["data"]["header_values"]["authorization"],
            [f"<redacted len={len('Bearer secret-token')}>"],
        )
        self.assertEqual(
            record["data"]["body"],
            {
                "top_level_fields_in_order": ["model"],
                "shape": {"model": "str:gpt-5.6"},
            },
        )

    def test_ws_derivation_produces_frames(self) -> None:
        self._derive()
        records = self._records("derived/A05/conn002.observation.jsonl")
        types = [record["record_type"] for record in records]
        self.assertEqual(types, ["http_request", "websocket_frame"])
        self.assertEqual(records[0]["data"]["wire_protocol"], "websocket")
        frame = records[1]["data"]
        self.assertEqual(frame["frame_index"], 0)
        self.assertEqual(frame["event_type"], "response.created")
        direct = assertion._h1_observations(
            self.bundle_dir / "official-run/relay/conn002.client_to_upstream.bin",
            "official-run/relay/conn002.client_to_upstream.bin",
            ["A05"],
            {"connection_id": "conn002"},
            {},
        )
        self.assertEqual(
            [observation.data for observation in direct],
            [record["data"] for record in records],
        )

    def test_mitm_derivation_redacts_and_summarizes(self) -> None:
        self._derive()
        records = self._records("derived/A09/models.observation.jsonl")
        self.assertEqual(len(records), 2)
        first = records[0]["data"]
        self.assertEqual(first["method"], "GET")
        self.assertEqual(first["path"], "/backend-api/models")
        self.assertIn(["client_version", "0.147.0"], first["query_pairs"])
        # redact_query 的替换值含空格，_parse_request_line 按空格切分请求行，
        # 非白名单 query 因此呈现为截断形态——这与 h1_request_stream 直接解析
        # relay 字节的既有行为逐字节同构，派生器不得单方面"修复"。
        self.assertIn(["pageToken", "<redacted"], first["query_pairs"])
        self.assertEqual(
            first["header_values"]["authorization"],
            [f"<redacted len={len('Bearer raw-token')}>"],
        )
        self.assertIsNone(first["body"])
        second = records[1]["data"]
        self.assertEqual(
            second["body"]["top_level_fields_in_order"], ["model", "stream"]
        )
        self.assertEqual(second["body"]["shape"]["stream"], "bool:true")
        self.assertNotIn("text", json.dumps(records, ensure_ascii=False))

    def test_derivation_is_deterministic_and_replayable(self) -> None:
        receipt = self._derive()
        replay = derive.verify_derivation(self.bundle_dir)
        self.assertEqual(
            replay["provenance_sha256"], receipt["provenance_sha256"]
        )
        second_dir = self.workdir / "assertion-bundle-2"
        bundle.build_bundle(
            self.roots,
            bundle.load_plan(self.workdir / "bundle-plan.json"),
            second_dir,
        )
        second = derive.derive_observations(
            second_dir,
            derive.load_derivation_plan(self._write_derivation_plan()),
        )
        self.assertEqual(
            second["provenance_sha256"], receipt["provenance_sha256"]
        )

    def test_bundle_verify_accepts_derived_prefix(self) -> None:
        self._derive()
        bundle.verify_bundle(
            self.roots,
            self.bundle_dir,
            allowed_extra_prefixes=(derive.DERIVED_PREFIX,),
        )


class DerivationNegativeTest(DerivationTestBase):
    def test_unregistered_source_rejected(self) -> None:
        (self.bundle_dir / "stray.bin").write_bytes(H1_HTTP_STREAM)
        self.derivation_plan = [
            dict(self.derivation_plan[0], source="stray.bin")
        ]
        with self.assertRaises(derive.ObservationDerivationError) as raised:
            self._derive()
        self.assertIn("未在 bundle provenance 登记", str(raised.exception))

    def test_source_drift_rejected(self) -> None:
        target = (
            self.bundle_dir / "official-run/relay/conn001.client_to_upstream.bin"
        )
        target.chmod(0o644)
        target.write_bytes(H1_HTTP_STREAM + b"drift")
        with self.assertRaises(derive.ObservationDerivationError) as raised:
            self._derive()
        self.assertIn("漂移", str(raised.exception))

    def test_observation_reinput_rejected(self) -> None:
        observation_line = {
            "schema_version": derive.OBSERVATION_SCHEMA_VERSION,
            "record_id": "x",
            "scenario_id": "A09",
            "record_type": "surface_identity",
            "data": {"key": "value"},
        }
        path = self.source_root / "mitm" / "models-http.jsonl"
        path.write_text(
            json.dumps(observation_line, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rebuilt = self.workdir / "assertion-bundle-reinput"
        bundle.build_bundle(
            self.roots,
            bundle.load_plan(self.workdir / "bundle-plan.json"),
            rebuilt,
        )
        self.derivation_plan = [self.derivation_plan[2]]
        with self.assertRaises(derive.ObservationDerivationError) as raised:
            derive.derive_observations(
                rebuilt,
                derive.load_derivation_plan(self._write_derivation_plan()),
            )
        self.assertIn("禁止以派生观测再派生", str(raised.exception))

    def test_internal_kind_rejected(self) -> None:
        self.derivation_plan = [
            dict(self.derivation_plan[0], kind="surface_identity")
        ]
        with self.assertRaises(derive.ObservationDerivationError):
            derive.load_derivation_plan(self._write_derivation_plan())

    def test_target_outside_derived_rejected(self) -> None:
        self.derivation_plan = [
            dict(self.derivation_plan[0], target="official-run/injected.jsonl")
        ]
        with self.assertRaises(derive.ObservationDerivationError):
            derive.load_derivation_plan(self._write_derivation_plan())

    def test_unregistered_derived_file_rejected(self) -> None:
        self._derive()
        stray = self.bundle_dir / "derived" / "stray.jsonl"
        stray.write_text("{}", encoding="utf-8")
        with self.assertRaises(derive.ObservationDerivationError) as raised:
            derive.verify_derivation(self.bundle_dir)
        self.assertIn("未登记文件", str(raised.exception))

    def test_derived_output_drift_rejected(self) -> None:
        self._derive()
        target = self.bundle_dir / "derived/A09/models.observation.jsonl"
        target.chmod(0o644)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(derive.ObservationDerivationError):
            derive.verify_derivation(self.bundle_dir)

    def test_tampered_receipt_rejected(self) -> None:
        self._derive()
        path = self.bundle_dir / derive.DERIVED_PROVENANCE_RELATIVE_PATH
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entries"][0]["record_count"] = 99
        path.chmod(0o644)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(derive.ObservationDerivationError):
            derive.verify_derivation(self.bundle_dir)


if __name__ == "__main__":
    unittest.main()
