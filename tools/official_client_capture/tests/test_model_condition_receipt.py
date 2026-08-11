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
