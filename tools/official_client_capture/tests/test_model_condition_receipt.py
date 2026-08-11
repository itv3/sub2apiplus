"""双轨模型条件收据的离线正反测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.model_condition_receipts import (
    ModelConditionReceiptError,
    build_receipt,
    validate_receipt,
)
from tools.official_client_capture.codex_upgrade import Job, run_job


def _h1_response(payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: application/json\r\n"
        + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


def _h1_request(model: str) -> bytes:
    body = json.dumps(
        {"model": model, "input": [], "parallel_tool_calls": True},
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        b"POST /backend-api/codex/responses HTTP/1.1\r\n"
        b"content-type: application/json\r\n"
        + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


class ModelConditionReceiptTest(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        request_model: str = "gpt-5.6-luna",
        lite: bool = True,
    ) -> Path:
        relay = root / "relay"
        relay.mkdir(parents=True)
        (relay / "relay.json").write_text(
            json.dumps(
                {
                    "schema_version": "byte-relay/v1",
                    "connections": [
                        {
                            "connection_id": 1,
                        },
                        {
                            "connection_id": 2,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (relay / "conn001.upstream_to_client.bin").write_bytes(
            _h1_response(
                {
                    "models": [
                        {"slug": "gpt-5.4", "use_responses_lite": False},
                        {"slug": "gpt-5.6-luna", "use_responses_lite": lite},
                    ]
                }
            )
        )
        (relay / "conn001.client_to_upstream.bin").write_bytes(
            b"GET /backend-api/codex/models?client_version=0.147.0 HTTP/1.1\r\n"
            b"host: chatgpt.com\r\n\r\n"
        )
        (relay / "conn002.client_to_upstream.bin").write_bytes(
            _h1_request(request_model)
        )
        return root

    def _ws_fixture(
        self,
        root: Path,
        *,
        request_model: str = "gpt-5.6-luna",
        lite: bool = True,
        compressed: bool = True,
        fragmented: bool = False,
    ) -> Path:
        """WS 传输下的 Lite 会话：Responses 不是 HTTP POST，模型在帧里。"""

        import struct
        import zlib

        self._fixture(root, request_model=request_model, lite=lite)
        relay = root / "relay"

        def frame(payload: bytes, *, opcode: int, fin: bool, rsv1: bool) -> bytes:
            mask = b"\xa1\xb2\xc3\xd4"
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            b0 = (0x80 if fin else 0) | (0x40 if rsv1 else 0) | opcode
            if len(masked) < 126:
                header = struct.pack("!BB", b0, 0x80 | len(masked))
            else:
                header = struct.pack("!BBH", b0, 0x80 | 126, len(masked))
            return header + mask + masked

        handshake = (
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"host: chatgpt.com\r\nupgrade: websocket\r\n"
            b"sec-websocket-extensions: permessage-deflate\r\n\r\n"
        )
        body = json.dumps({"model": request_model, "input": []}).encode()
        if compressed:
            # 上下文接管：整条连接共用一个压缩器，两条消息必须依次压。
            deflater = zlib.compressobj(-1, zlib.DEFLATED, -zlib.MAX_WBITS)
            first = deflater.compress(body) + deflater.flush(zlib.Z_SYNC_FLUSH)
            first = first[:-4] if first.endswith(b"\x00\x00\xff\xff") else first
            second = deflater.compress(body) + deflater.flush(zlib.Z_SYNC_FLUSH)
            second = second[:-4] if second.endswith(b"\x00\x00\xff\xff") else second
            payloads = [first, second]
        else:
            payloads = [body, body]
        frames = b""
        for payload in payloads:
            if fragmented:
                half = len(payload) // 2
                frames += frame(payload[:half], opcode=0x1, fin=False, rsv1=compressed)
                frames += frame(payload[half:], opcode=0x0, fin=True, rsv1=False)
            else:
                frames += frame(payload, opcode=0x1, fin=True, rsv1=compressed)
        (relay / "conn002.client_to_upstream.bin").write_bytes(handshake + frames)
        return root

    def test_ws_传输从压缩帧取出模型(self) -> None:
        """WS 下 Responses 走帧，且 permessage-deflate 让明文搜不到 model。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._ws_fixture(Path(directory) / "run")
            self.assertNotIn(
                b"gpt-5.6-luna",
                (root / "relay" / "conn002.client_to_upstream.bin").read_bytes(),
                "夹具必须真的压缩，否则测不到解压路径",
            )
            receipt = build_receipt(
                root=root,
                job_id="official-lite-ws-turnstate",
                run_id="campaign-lite-ws",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])
            self.assertFalse(receipt["model_fallback"])

    def test_ws_分片消息也能取出模型(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._ws_fixture(Path(directory) / "run", fragmented=True)
            receipt = build_receipt(
                root=root,
                job_id="official-lite-ws-turnstate",
                run_id="campaign-lite-ws",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])

    def test_ws_传输同样拦截模型_fallback(self) -> None:
        """WS 路径不得成为绕过 fallback 判定的后门。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._ws_fixture(Path(directory) / "run", request_model="gpt-5.4")
            with self.assertRaises(ModelConditionReceiptError):
                build_receipt(
                    root=root,
                    job_id="official-lite-ws-turnstate",
                    run_id="campaign-lite-ws",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )

    def test_builds_and_revalidates_lite_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(receipt["model_id"], "gpt-5.6-luna")
            self.assertTrue(receipt["use_responses_lite"])
            self.assertFalse(receipt["model_fallback"])
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])
            validate_receipt(
                receipt,
                root=root,
                job_id="official-lite-http-response",
                track="lite",
                model_id="gpt-5.6-luna",
                use_responses_lite=True,
            )

    def test_accepts_multiple_consistent_models_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append({"connection_id": 3})
            relay_path.write_text(json.dumps(relay), encoding="utf-8")
            (root / "relay" / "conn003.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models?client_version=0.147.0 HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            (root / "relay" / "conn003.upstream_to_client.bin").write_bytes(
                _h1_response(
                    {
                        "models": [
                            {"slug": "gpt-5.6-luna", "use_responses_lite": True},
                        ]
                    }
                )
            )
            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(len(receipt["evidence_bindings"]), 5)
            validate_receipt(
                receipt,
                root=root,
                job_id="official-lite-http-response",
                track="lite",
                model_id="gpt-5.6-luna",
                use_responses_lite=True,
            )

    def test_rejects_models_metadata_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run", lite=False)
            with self.assertRaisesRegex(ModelConditionReceiptError, "预期为 True"):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )

    def test_rejects_actual_request_model_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run", request_model="gpt-5.4")
            with self.assertRaisesRegex(ModelConditionReceiptError, "fallback"):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )

    def test_validation_detects_bound_evidence_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            (root / "relay" / "conn002.client_to_upstream.bin").write_bytes(
                _h1_request("gpt-5.6-luna") + b"tampered"
            )
            with self.assertRaisesRegex(ModelConditionReceiptError, "摘要不一致"):
                validate_receipt(
                    receipt,
                    root=root,
                    job_id="official-lite-http-response",
                    track="lite",
                    model_id="gpt-5.6-luna",
                    use_responses_lite=True,
                )

    def test_run_job_fails_closed_without_required_model_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = root / "run"
            run_root.mkdir()
            (run_root / "evidence.bin").write_bytes(b"evidence")
            job = Job(
                job_id="official-lite-http-response",
                phase="official",
                suites=("full",),
                description="Lite 模型收据门禁",
                steps=(
                    {
                        "argv": [sys.executable, "-c", "pass"],
                        "environment": {},
                        "timeout": 10,
                    },
                ),
                evidence_roots=(str(run_root),),
                covers=("SPEC-BODY-006",),
                scenario_ids=("A03",),
                track="lite",
                model_id="gpt-5.6-luna",
                expected_use_responses_lite=True,
                required_model_receipt=True,
            )
            result = run_job(job, root)
            self.assertEqual(result["status"], "failed")
            self.assertIn("未产出模型条件成功收据", result["model_condition_receipt_failure"])

    def test_run_job_accepts_bound_model_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_root = self._fixture(root / "run")
            receipt = build_receipt(
                root=run_root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            (run_root / "model-condition-receipt.json").write_text(
                json.dumps(receipt),
                encoding="utf-8",
            )
            job = Job(
                job_id="official-lite-http-response",
                phase="official",
                suites=("full",),
                description="Lite 模型收据门禁",
                steps=(
                    {
                        "argv": [sys.executable, "-c", "pass"],
                        "environment": {},
                        "timeout": 10,
                    },
                ),
                evidence_roots=(str(run_root),),
                covers=("SPEC-BODY-006",),
                scenario_ids=("A03",),
                track="lite",
                model_id="gpt-5.6-luna",
                expected_use_responses_lite=True,
                required_model_receipt=True,
            )
            result = run_job(job, root)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["model_condition_receipt"]["track"], "lite")


if __name__ == "__main__":
    unittest.main()
