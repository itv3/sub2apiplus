"""双轨模型条件收据的离线正反测试。"""

from __future__ import annotations

import hashlib
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

    def _write_prewarm(
        self,
        root: Path,
        *,
        connection_id: int = 1,
        model: str = "gpt-5.6-luna",
        lite: bool = True,
    ) -> Path:
        """绑定夹具里一份已完整落盘的模型目录响应。"""

        request = root / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
        response = root / "relay" / f"conn{connection_id:03d}.upstream_to_client.bin"
        path = root / "model-catalog-prewarm.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "codex-model-catalog-prewarm/v1",
                    "status": "success",
                    "codex_version": "0.149.1",
                    "model_id": model,
                    "use_responses_lite": lite,
                    "protocol_record_count": 3,
                    "model_count": 2,
                    "capture": {
                        "connection_id": connection_id,
                        "request_path": (
                            f"relay/conn{connection_id:03d}.client_to_upstream.bin"
                        ),
                        "request_sha256": hashlib.sha256(
                            request.read_bytes()
                        ).hexdigest(),
                        "response_path": (
                            f"relay/conn{connection_id:03d}.upstream_to_client.bin"
                        ),
                        "response_sha256": hashlib.sha256(
                            response.read_bytes()
                        ).hexdigest(),
                        "use_responses_lite": lite,
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

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
            roles_by_path = {
                item["path"]: item["roles"]
                for item in receipt["evidence_bindings"]
            }
            self.assertEqual(
                roles_by_path["relay/conn001.client_to_upstream.bin"],
                ["models_request"],
            )
            self.assertEqual(
                roles_by_path["relay/conn001.upstream_to_client.bin"],
                ["models_response"],
            )
            self.assertEqual(
                roles_by_path["relay/conn002.client_to_upstream.bin"],
                ["responses_request"],
            )
            validate_receipt(
                receipt,
                root=root,
                job_id="official-lite-http-response",
                track="lite",
                model_id="gpt-5.6-luna",
                use_responses_lite=True,
            )

    def test_角色被改绑到错误请求时失败关闭(self) -> None:
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
            by_path = {
                item["path"]: item for item in receipt["evidence_bindings"]
            }
            by_path["relay/conn001.client_to_upstream.bin"]["roles"] = [
                "responses_request"
            ]
            by_path["relay/conn002.client_to_upstream.bin"]["roles"] = [
                "models_request"
            ]
            with self.assertRaisesRegex(ModelConditionReceiptError, "models_request"):
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
            self.assertEqual(len(receipt["evidence_bindings"]), 6)
            validate_receipt(
                receipt,
                root=root,
                job_id="official-lite-http-response",
                track="lite",
                model_id="gpt-5.6-luna",
                use_responses_lite=True,
            )

    def test_预热摘要只绑定完整响应并排除并发半响应(self) -> None:
        """隔离 app-server 主动退出留下的其他半响应不得作废已绑定的完整 200。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append({"connection_id": 3})
            relay_path.write_text(json.dumps(relay), encoding="utf-8")
            (root / "relay" / "conn003.client_to_upstream.bin").write_bytes(
                b"GET /backend-api/codex/models HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            (root / "relay" / "conn003.upstream_to_client.bin").write_bytes(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                b"content-length: 100\r\n\r\n{"
            )
            prewarm = self._write_prewarm(root)

            with self.assertRaises(ModelConditionReceiptError):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )
            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
                model_catalog_prewarm=prewarm,
            )
            bound_paths = {item["path"] for item in receipt["evidence_bindings"]}
            self.assertIn("model-catalog-prewarm.json", bound_paths)
            self.assertIn("relay/conn001.upstream_to_client.bin", bound_paths)
            self.assertNotIn("relay/conn003.upstream_to_client.bin", bound_paths)

    def test_预热摘要原始字节摘要漂移时失败关闭(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            prewarm = self._write_prewarm(root)
            payload = json.loads(prewarm.read_text(encoding="utf-8"))
            payload["capture"]["response_sha256"] = "0" * 64
            prewarm.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ModelConditionReceiptError, "摘要不一致"):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                    model_catalog_prewarm=Path("model-catalog-prewarm.json"),
                )

    def test_预热摘要接受_manifest_证明的等长脱敏(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            request_path = root / "relay" / "conn001.client_to_upstream.bin"
            request_path.write_bytes(
                request_path.read_bytes()
                .replace(b"\r\n\r\n", b"\r\nauthorization: Bearer secret-one\r\n\r\n")
            )
            prewarm = self._write_prewarm(root)
            request_path.write_bytes(
                request_path.read_bytes().replace(
                    b"Bearer secret-one",
                    b"<secret><secret>x",
                )
            )
            response_path = root / "relay" / "conn001.upstream_to_client.bin"
            request = request_path.read_bytes()
            response = response_path.read_bytes()
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["credential_scrubbing"] = {
                "method": "equal_length_replacement",
                "byte_offsets_preserved": True,
                "hashes_recomputed": True,
            }
            relay["connections"][0] = {
                "connection_id": 1,
                "valid": True,
                "error": None,
                "bytes": {
                    "client_to_upstream": len(request),
                    "upstream_to_client": len(response),
                },
                "sha256": {
                    "client_to_upstream": hashlib.sha256(request).hexdigest(),
                    "upstream_to_client": hashlib.sha256(response).hexdigest(),
                },
                "segments": [
                    {
                        "direction": "client_to_upstream",
                        "offset": 0,
                        "length": len(request),
                    },
                    {
                        "direction": "upstream_to_client",
                        "offset": 0,
                        "length": len(response),
                    },
                ],
            }
            relay_path.write_text(json.dumps(relay), encoding="utf-8")

            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
                model_catalog_prewarm=prewarm,
            )
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])

    def test_ignores_manifest_declared_zero_byte_connection(self) -> None:
        """中继登记但未传输任何字节的竞速连接不属于证据丢失。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append(
                {
                    "connection_id": 3,
                    "bytes": {},
                    "sha256": {},
                    "segments": [],
                    "opened_at_unix_ms": 1,
                    "closed_at_unix_ms": 2,
                }
            )
            relay_path.write_text(json.dumps(relay), encoding="utf-8")

            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])
            bound_paths = {item["path"] for item in receipt["evidence_bindings"]}
            self.assertIn("relay/relay.json", bound_paths)
            self.assertNotIn("relay/conn003.client_to_upstream.bin", bound_paths)

    def test_允许受_manifest_约束的单向_models_失败尝试(self) -> None:
        """已有完整 200 时，额外单向请求是上游零响应事实，不是文件丢失。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            request = (
                b"GET /backend-api/codex/models?client_version=0.149.1 HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            request_path = root / "relay" / "conn003.client_to_upstream.bin"
            request_path.write_bytes(request)
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append(
                {
                    "connection_id": 3,
                    "valid": True,
                    "bytes": {"client_to_upstream": len(request)},
                    "sha256": {
                        "client_to_upstream": hashlib.sha256(request).hexdigest()
                    },
                    "segments": [
                        {
                            "direction": "client_to_upstream",
                            "offset": 0,
                            "length": len(request),
                        }
                    ],
                }
            )
            relay_path.write_text(json.dumps(relay), encoding="utf-8")

            receipt = build_receipt(
                root=root,
                job_id="official-lite-http-response",
                run_id="campaign-lite-http",
                track="lite",
                expected_model="gpt-5.6-luna",
                expected_lite=True,
            )
            self.assertEqual(receipt["observed_request_models"], ["gpt-5.6-luna"])
            bound_paths = {item["path"] for item in receipt["evidence_bindings"]}
            self.assertIn("relay/relay.json", bound_paths)
            self.assertIn("relay/conn003.client_to_upstream.bin", bound_paths)

    def test_单向_models_的_manifest_摘要漂移仍失败关闭(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            request = (
                b"GET /backend-api/codex/models?client_version=0.149.1 HTTP/1.1\r\n"
                b"host: chatgpt.com\r\n\r\n"
            )
            (root / "relay" / "conn003.client_to_upstream.bin").write_bytes(request)
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append(
                {
                    "connection_id": 3,
                    "valid": True,
                    "bytes": {"client_to_upstream": len(request)},
                    "sha256": {"client_to_upstream": "0" * 64},
                    "segments": [
                        {
                            "direction": "client_to_upstream",
                            "offset": 0,
                            "length": len(request),
                        }
                    ],
                }
            )
            relay_path.write_text(json.dumps(relay), encoding="utf-8")

            with self.assertRaisesRegex(ModelConditionReceiptError, "缺少连接 3"):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
                )

    def test_rejects_missing_bytes_for_nonempty_connection(self) -> None:
        """只豁免严格空连接；声称有 segment 的连接仍须存在原始字节。"""

        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory) / "run")
            relay_path = root / "relay" / "relay.json"
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            relay["connections"].append(
                {
                    "connection_id": 3,
                    "bytes": {"client_to_upstream": 10},
                    "sha256": {"client_to_upstream": "0" * 64},
                    "segments": [
                        {
                            "direction": "client_to_upstream",
                            "offset": 0,
                            "length": 10,
                        }
                    ],
                }
            )
            relay_path.write_text(json.dumps(relay), encoding="utf-8")

            with self.assertRaisesRegex(ModelConditionReceiptError, "缺少连接 3"):
                build_receipt(
                    root=root,
                    job_id="official-lite-http-response",
                    run_id="campaign-lite-http",
                    track="lite",
                    expected_model="gpt-5.6-luna",
                    expected_lite=True,
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
