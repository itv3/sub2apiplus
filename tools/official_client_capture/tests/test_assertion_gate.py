"""ACC-03 seal 门禁必须把 accept 的证据前提全部前移并失败关闭。

k34 的教训：manifest 只登记 6 个 pcap、标签语义错位、17 条内部规则被强制
双侧，全部拖到 accept 才暴露。门禁测试按同样的失败面构造负例。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import acceptance_contract as contract_module  # noqa: E402
import assertion_gate as gate  # noqa: E402
import build_assertion_bundle as bundle  # noqa: E402
import candidate_rule_assertion as assertion  # noqa: E402
import derive_official_observations as derive  # noqa: E402

TARGET_VERSION = "0.147.0"

H1_HTTP_STREAM = (
    b"POST /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 19\r\n\r\n"
    b'{"model":"gpt-5.6"}'
)


def _masked_text_frame(payload: bytes) -> bytes:
    key = b"\x01\x02\x03\x04"
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes([0x81, 0x80 | len(payload)]) + key + masked


H1_WS_STREAM = (
    b"GET /backend-api/codex/responses HTTP/1.1\r\n"
    b"Host: chatgpt.com\r\n"
    b"Connection: Upgrade\r\n"
    b"Upgrade: websocket\r\n\r\n"
) + _masked_text_frame(b'{"type":"response.created"}')


def _profile() -> dict:
    return {
        "schema_version": "codex-candidate-rule-expectations/v1",
        "codex_version": TARGET_VERSION,
        "scenarios": [
            {
                "scenario_id": "A03",
                "description": "http 相关场景",
                "trigger": "test",
                "preconditions": ["test"],
                "required_artifact_kinds": ["relay_binary", "process_trace"],
            },
            {
                "scenario_id": "A05",
                "description": "ws 场景",
                "trigger": "test",
                "preconditions": ["test"],
                "required_artifact_kinds": ["relay_binary", "websocket_trace"],
            },
        ],
        "rules": [
            {
                "rule_id": "SPEC-H1-001",
                "scenario_ids": ["A03"],
                "description": "http 方法",
                "checks": [
                    {
                        "id": "method-check",
                        "description": "POST",
                        "select": {
                            "record_type": "http_request",
                            "where": [
                                {
                                    "path": "labels.transport",
                                    "operator": "equal",
                                    "value": "http",
                                }
                            ],
                        },
                        "assertion": {
                            "operator": "all_equal",
                            "path": "data.method",
                            "value": "POST",
                        },
                    }
                ],
            },
            {
                "rule_id": "SPEC-WS-001",
                "scenario_ids": ["A05"],
                "description": "ws 帧",
                "checks": [
                    {
                        "id": "frame-check",
                        "description": "首帧",
                        "select": {"record_type": "websocket_frame"},
                        "assertion": {
                            "operator": "all_equal",
                            "path": "data.frame_index",
                            "value": 0,
                        },
                    }
                ],
            },
            {
                "rule_id": "SPEC-INT-001",
                "scenario_ids": ["A03"],
                "description": "内部事实",
                "checks": [
                    {
                        "id": "surface-check",
                        "description": "surface",
                        "select": {"record_type": "surface_identity"},
                        "assertion": {
                            "operator": "all_equal",
                            "path": "data.surface",
                            "value": "codex",
                        },
                    }
                ],
            },
        ],
    }


CANDIDATE_TRACE_RECORD = {
    "schema_version": assertion.OBSERVATION_SCHEMA_VERSION,
    "record_id": "candidate-surface-1",
    "scenario_id": "A03",
    "record_type": "surface_identity",
    "data": {"surface": "codex"},
    "source_artifacts": ["official-run/relay/conn001.client_to_upstream.bin"],
}


class GateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="assertion-gate-test-"))
        self.addCleanup(self._cleanup)
        self.profile = _profile()
        self.contract = contract_module.build_contract_payload(self.profile)
        self.source_root = self.workdir / "official-run"
        (self.source_root / "relay").mkdir(parents=True)
        (self.source_root / "traces").mkdir()
        (self.source_root / "relay" / "conn001.client_to_upstream.bin").write_bytes(
            H1_HTTP_STREAM
        )
        (self.source_root / "relay" / "conn002.client_to_upstream.bin").write_bytes(
            H1_WS_STREAM
        )
        (self.source_root / "traces" / "candidate-a03.observation.jsonl").write_text(
            json.dumps(CANDIDATE_TRACE_RECORD, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        self.roots = {"official-run": self.source_root}
        self.bundle_dir = self.workdir / gate.BUNDLE_DIR_NAME

    def _cleanup(self) -> None:
        for path in sorted(self.workdir.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o644)
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.workdir.rmdir()

    def _build_bundle(self, *, include_candidate_trace: bool) -> None:
        entries = [
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
        ]
        if include_candidate_trace:
            entries.append(
                {
                    "root": "official-run",
                    "path": "traces/candidate-a03.observation.jsonl",
                    "target": "traces/candidate-a03.observation.jsonl",
                }
            )
        plan_path = self.workdir / "bundle-plan.json"
        plan_path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        bundle.build_bundle(
            self.roots, bundle.load_plan(plan_path), self.bundle_dir
        )
        derivation_plan = [
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
        ]
        plan_path = self.workdir / "derive-plan.json"
        plan_path.write_text(
            json.dumps({"entries": derivation_plan}, ensure_ascii=False),
            encoding="utf-8",
        )
        derive.derive_observations(
            self.bundle_dir, derive.load_derivation_plan(plan_path)
        )

    def _write_manifest(
        self,
        *,
        include_candidate_trace: bool,
        transport_label: str = "http",
        conn001_parser: str = "opaque_bound_source",
        drop_a03_derived: bool = False,
    ) -> None:
        def _artifact(path: str, kind: str, parser: str, scenarios: list[str]) -> dict:
            return {
                "path": path,
                "sha256": hashlib.sha256(
                    (self.bundle_dir / path).read_bytes()
                ).hexdigest(),
                "kind": kind,
                "parser": parser,
                "scenario_ids": scenarios,
                "labels": {"transport": transport_label},
            }

        artifacts = [
            _artifact(
                "official-run/relay/conn001.client_to_upstream.bin",
                "relay_binary",
                conn001_parser,
                ["A03"],
            ),
            _artifact(
                "official-run/relay/conn002.client_to_upstream.bin",
                "relay_binary",
                "opaque_bound_source",
                ["A05"],
            ),
            _artifact(
                "derived/A05/conn002.observation.jsonl",
                "websocket_trace",
                "observation_jsonl",
                ["A05"],
            ),
        ]
        if not drop_a03_derived:
            artifacts.insert(
                2,
                _artifact(
                    "derived/A03/conn001.observation.jsonl",
                    "process_trace",
                    "observation_jsonl",
                    ["A03"],
                ),
            )
        if include_candidate_trace:
            artifacts.append(
                _artifact(
                    "traces/candidate-a03.observation.jsonl",
                    "process_trace",
                    "observation_jsonl",
                    ["A03"],
                )
            )
        manifest = {
            "schema_version": assertion.CAPTURE_MANIFEST_SCHEMA_VERSION,
            "codex_version": TARGET_VERSION,
            "capture_id": "gate-test",
            "status": "complete",
            "artifacts": artifacts,
        }
        (self.bundle_dir / gate.MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _run(self, side: str) -> dict:
        return gate.run_assertion_gate(
            bundle_dir=self.bundle_dir,
            source_roots=self.roots,
            side=side,
            profile=self.profile,
            contract=self.contract,
            target_version=TARGET_VERSION,
        )


class AssertionGatePassTest(GateFixture):
    def test_official_side_passes_and_skips_internal_rules(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(include_candidate_trace=False)
        receipt = self._run("official")
        self.assertEqual(receipt["side"], "official")
        self.assertEqual(receipt["checked_rule_count"], 2)
        self.assertEqual(receipt["checked_check_count"], 2)
        self.assertIsNotNone(receipt["derived_provenance_sha256"])
        gate.validate_gate_receipt(receipt, side="official")

    def test_candidate_side_requires_internal_observations(self) -> None:
        self._build_bundle(include_candidate_trace=True)
        self._write_manifest(include_candidate_trace=True)
        receipt = self._run("candidate")
        self.assertEqual(receipt["checked_rule_count"], 3)
        self.assertEqual(receipt["checked_check_count"], 3)

    def test_candidate_side_without_internal_trace_fails(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(include_candidate_trace=False)
        with self.assertRaises(gate.AssertionGateError) as raised:
            self._run("candidate")
        self.assertIn("SPEC-INT-001", str(raised.exception))


class AssertionGateNegativeTest(GateFixture):
    def test_label_semantic_mismatch_fails_at_seal(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(
            include_candidate_trace=False, transport_label="direct"
        )
        with self.assertRaises(gate.AssertionGateError) as raised:
            self._run("official")
        self.assertIn("method-check", str(raised.exception))
        self.assertIn("标签语义错位", str(raised.exception))

    def test_missing_artifact_kind_fails_at_seal(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(include_candidate_trace=False)
        profile = _profile()
        profile["scenarios"][0]["required_artifact_kinds"] = [
            "relay_binary",
            "process_trace",
            "pcap",
        ]
        with self.assertRaises(gate.AssertionGateError) as raised:
            gate.run_assertion_gate(
                bundle_dir=self.bundle_dir,
                source_roots=self.roots,
                side="official",
                profile=profile,
                contract=contract_module.build_contract_payload(profile),
                target_version=TARGET_VERSION,
            )
        self.assertIn("A03:pcap", str(raised.exception))

    def test_double_parse_of_same_bytes_fails(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(
            include_candidate_trace=False, conn001_parser="h1_request_stream"
        )
        with self.assertRaises(gate.AssertionGateError) as raised:
            self._run("official")
        self.assertIn("双重解析计数", str(raised.exception))

    def test_bundle_tampering_fails(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(include_candidate_trace=False)
        target = (
            self.bundle_dir / "official-run/relay/conn001.client_to_upstream.bin"
        )
        target.chmod(0o644)
        target.write_bytes(H1_HTTP_STREAM + b"x")
        with self.assertRaises(gate.AssertionGateError) as raised:
            self._run("official")
        self.assertIn("bundle provenance 重放失败", str(raised.exception))

    def test_manifest_missing_from_bundle_root_fails(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        with self.assertRaises(gate.AssertionGateError) as raised:
            self._run("official")
        self.assertIn("capture manifest 必须位于 bundle 根", str(raised.exception))

    def test_unknown_side_rejected(self) -> None:
        self._build_bundle(include_candidate_trace=False)
        self._write_manifest(include_candidate_trace=False)
        with self.assertRaises(gate.AssertionGateError):
            self._run("upstream")


class GateReceiptContractTest(unittest.TestCase):
    def test_receipt_field_closure_enforced(self) -> None:
        with self.assertRaises(gate.AssertionGateError):
            gate.validate_gate_receipt({"side": "official"}, side="official")

    def test_receipt_side_mismatch_rejected(self) -> None:
        receipt = {
            "side": "official",
            "bundle_dir_name": gate.BUNDLE_DIR_NAME,
            "bundle_provenance_sha256": "0" * 64,
            "bundle_entry_count": 1,
            "derived_provenance_sha256": None,
            "capture_manifest": {"path": gate.MANIFEST_FILENAME, "sha256": "0" * 64},
            "acceptance_contract_sha256": "0" * 64,
            "artifact_count": 1,
            "observation_count": 1,
            "checked_rule_count": 1,
            "checked_check_count": 1,
        }
        gate.validate_gate_receipt(receipt, side="official")
        with self.assertRaises(gate.AssertionGateError):
            gate.validate_gate_receipt(receipt, side="candidate")


if __name__ == "__main__":
    unittest.main()
