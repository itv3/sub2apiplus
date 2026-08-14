"""部署源码 ``go test -json`` 到候选结构化 trace 的失败关闭测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import candidate_rule_assertion
from tools.official_client_capture import candidate_test_trace
from tools.official_client_capture.candidate_rule_assertion import (
    load_observations,
)
from tools.official_client_capture.candidate_test_trace import (
    CandidateTestTraceError,
    FACT_PREFIX,
    file_sha256,
    generate_test_traces,
    load_mapping,
)


PACKAGE = "github.com/Wei-Shaw/sub2api/internal/service"
TEST_NAME = "TestCandidateTraceFallback"
FACT_ID = "a07.transport-fallback"


class CandidateTestTraceTest(unittest.TestCase):
    def test_frozen_input_digests_match_checked_in_assets(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        # 两侧冻结的是不同东西，不能再共用一份摘要：
        # - candidate_rule_assertion 冻结 0.145.0 基线画像——它是 classify 的迁移基线
        #   （codex_upgrade.py 的 base_path），升级期间保持不动；
        # - candidate_test_trace 冻结的映射与画像都要与 Campaign 目标同版本，否则
        #   load_mapping／load_profile 的 codex_version 校验直接拒绝。
        baseline_profile = tool_root / "candidate_rule_expectations_0_145_0.json"
        target_profile = tool_root / "candidate_rule_expectations_0_147_0.json"
        target_mapping = tool_root / "candidate_test_fact_map_0_147_0.json"

        self.assertEqual(
            candidate_rule_assertion.FROZEN_PROFILE_SHA256,
            file_sha256(baseline_profile),
        )
        self.assertEqual(
            candidate_test_trace.FROZEN_PROFILE_SHA256,
            file_sha256(target_profile),
        )
        self.assertEqual(
            candidate_test_trace.FROZEN_MAPPING_SHA256,
            file_sha256(target_mapping),
        )

    def _fixture(
        self,
        root: Path,
        *,
        action: str = "pass",
        fact_id: str = FACT_ID,
        record_type: str = "transport_fallback",
        include_fact: bool = True,
        include_raw: bool = True,
        cached: bool = False,
        codex_version: str = "0.145.0",
    ) -> dict[str, Path | str]:
        source_root = root / "source"
        evidence_root = root / "evidence"
        test_file = source_root / "backend/internal/service/candidate_acceptance_test.go"
        source_file = source_root / "backend/internal/service/official_egress_codex_engine.go"
        profile = source_root / "tools/official_client_capture/candidate_rule_expectations_0_145_0.json"
        mapping = source_root / "tools/official_client_capture/candidate_test_fact_map_0_145_0.json"
        test_file.parent.mkdir(parents=True)
        source_file.parent.mkdir(parents=True, exist_ok=True)
        profile.parent.mkdir(parents=True)
        test_file.write_text("package service\n// 测试快照\n", encoding="utf-8")
        source_file.write_text("package service\n// 生产快照\n", encoding="utf-8")
        profile.write_text(
            json.dumps({"codex_version": codex_version, "profile": "unit-test"})
            + "\n",
            encoding="utf-8",
        )
        mapping_value = {
            "schema_version": "codex-candidate-test-fact-map/v1",
            "codex_version": codex_version,
            "fact_prefix": FACT_PREFIX,
            "forbidden_record_types": [
                "http_request",
                "tls_client_hello",
                "websocket_frame",
            ],
            "tests": [
                {
                    "package": PACKAGE,
                    "name": TEST_NAME,
                    "test_file": "backend/internal/service/candidate_acceptance_test.go",
                    "test_file_sha256": file_sha256(test_file),
                    "source_files": [
                        {
                            "path": "backend/internal/service/official_egress_codex_engine.go",
                            "sha256": file_sha256(source_file),
                        }
                    ],
                    "facts": [
                        {
                            "fact_id": fact_id,
                            "scenario_id": "A07",
                            "record_type": record_type,
                            "trace_kind": "process_trace",
                            "data_keys": [
                                "retry_budget_state",
                                "transports",
                                "invocation_ids",
                            ],
                            "required_source_kinds": ["relay_binary"],
                        }
                    ],
                }
            ],
        }
        mapping.write_text(
            json.dumps(mapping_value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        relay = evidence_root / "candidate/A07/raw.bin"
        go_log = evidence_root / "candidate/go-test.jsonl"
        relay.parent.mkdir(parents=True)
        go_log.parent.mkdir(parents=True, exist_ok=True)
        relay.write_bytes(b"opaque-redacted-relay-bytes")
        fact = {
            "schema_version": "codex-candidate-test-fact/v1",
            "fact_id": fact_id,
            "scenario_id": "A07",
            "record_type": record_type,
            "data": {
                "retry_budget_state": "exhausted",
                "transports": ["websocket", "websocket", "http"],
                "invocation_ids": ["f" * 64, "f" * 64, "f" * 64],
            },
        }
        events: list[dict[str, object]] = [
            {"Action": "start", "Package": PACKAGE},
            {"Action": "run", "Package": PACKAGE, "Test": TEST_NAME},
        ]
        if include_fact:
            events.append(
                {
                    "Action": "output",
                    "Package": PACKAGE,
                    "Test": TEST_NAME,
                    "Output": (
                        "candidate_acceptance_test.go:42: "
                        + FACT_PREFIX
                        + json.dumps(fact, separators=(",", ":"))
                        + "\n"
                    ),
                }
            )
        events.append(
            {
                "Action": action,
                "Package": PACKAGE,
                "Test": TEST_NAME,
                "Elapsed": 0.01,
            }
        )
        if cached:
            events.append(
                {
                    "Action": "output",
                    "Package": PACKAGE,
                    "Output": f"ok  \t{PACKAGE}\t(cached)\n",
                }
            )
        events.append({"Action": action, "Package": PACKAGE, "Elapsed": 0.01})
        go_log.write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )

        artifacts: list[dict[str, object]] = [
            {
                "path": "candidate/go-test.jsonl",
                "sha256": file_sha256(go_log),
                "kind": "stdout_log",
                "parser": "opaque_bound_source",
                "scenario_ids": ["A07"],
                "labels": {"command": "go test -json -count=1"},
            }
        ]
        if include_raw:
            artifacts.append(
                {
                    "path": "candidate/A07/raw.bin",
                    "sha256": file_sha256(relay),
                    "kind": "relay_binary",
                    "parser": "opaque_bound_source",
                    "scenario_ids": ["A07"],
                    "labels": {"direction": "client_to_upstream"},
                }
            )
        manifest = evidence_root / "capture-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "codex-candidate-capture-manifest/v1",
                    "codex_version": codex_version,
                    "capture_id": "unit-test",
                    "status": "complete",
                    "artifacts": artifacts,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return {
            "source_root": source_root,
            "evidence_root": evidence_root,
            "mapping": mapping,
            "profile": profile,
            "manifest": manifest,
            "test_file": test_file,
            "codex_version": codex_version,
        }

    def _generate(self, fixture: dict[str, Path | str]) -> dict[str, object]:
        return generate_test_traces(
            source_root=fixture["source_root"],  # type: ignore[arg-type]
            evidence_root=fixture["evidence_root"],  # type: ignore[arg-type]
            capture_manifest_path=fixture["manifest"],  # type: ignore[arg-type]
            go_test_artifact="candidate/go-test.jsonl",
            mapping_path=fixture["mapping"],  # type: ignore[arg-type]
            profile_path=fixture["profile"],  # type: ignore[arg-type]
            trace_dir="generated/test-traces",
            output_manifest="generated/capture-with-test-traces.json",
            output_receipt="generated/test-trace-receipt.json",
            expected_codex_version=fixture["codex_version"],  # type: ignore[arg-type]
            expected_mapping_sha256=file_sha256(
                fixture["mapping"]  # type: ignore[arg-type]
            ),
            expected_profile_sha256=file_sha256(
                fixture["profile"]  # type: ignore[arg-type]
            ),
        )

    def test_target_version_is_bound_across_inputs_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), codex_version="0.147.0")
            receipt = self._generate(fixture)
            self.assertEqual(receipt["codex_version"], "0.147.0")
            output_manifest = (
                fixture["evidence_root"]  # type: ignore[operator]
                / "generated/capture-with-test-traces.json"
            )
            self.assertEqual(
                json.loads(output_manifest.read_text(encoding="utf-8"))[
                    "codex_version"
                ],
                "0.147.0",
            )

    def test_empty_or_wrong_target_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), codex_version="0.147.0")
            common = {
                "source_root": fixture["source_root"],
                "evidence_root": fixture["evidence_root"],
                "capture_manifest_path": fixture["manifest"],
                "go_test_artifact": "candidate/go-test.jsonl",
                "mapping_path": fixture["mapping"],
                "profile_path": fixture["profile"],
                "trace_dir": "generated/test-traces",
                "output_manifest": "generated/capture.json",
                "output_receipt": "generated/receipt.json",
                "expected_mapping_sha256": file_sha256(
                    fixture["mapping"]  # type: ignore[arg-type]
                ),
                "expected_profile_sha256": file_sha256(
                    fixture["profile"]  # type: ignore[arg-type]
                ),
            }
            with self.assertRaisesRegex(CandidateTestTraceError, "期望 Codex 版本"):
                generate_test_traces(expected_codex_version="", **common)  # type: ignore[arg-type]
            with self.assertRaisesRegex(CandidateTestTraceError, "Campaign 目标"):
                generate_test_traces(
                    expected_codex_version="0.146.0",
                    **common,  # type: ignore[arg-type]
                )

    def test_passed_exact_test_generates_bound_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            receipt = self._generate(fixture)
            self.assertEqual(receipt["status"], "pass")
            generated_manifest = (
                fixture["evidence_root"]  # type: ignore[operator]
                / "generated/capture-with-test-traces.json"
            )
            _, observations = load_observations(
                generated_manifest,
                fixture["evidence_root"],  # type: ignore[arg-type]
            )
            fallback = [
                item for item in observations if item.record_type == "transport_fallback"
            ]
            self.assertEqual(len(fallback), 1)
            self.assertEqual(
                set(fallback[0].evidence_paths),
                {
                    "candidate/go-test.jsonl",
                    "candidate/A07/raw.bin",
                    "generated/test-traces/A07/go-test-facts.jsonl",
                },
            )
            proof = fallback[0].data["_test_proof"]
            self.assertEqual(proof["test_name"], TEST_NAME)
            self.assertEqual(proof["test_file_sha256"], file_sha256(fixture["test_file"]))  # type: ignore[arg-type]

    def test_failed_test_rejects_marker_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), action="fail")
            with self.assertRaisesRegex(CandidateTestTraceError, "失败事件"):
                self._generate(fixture)
            self.assertFalse(
                (fixture["evidence_root"] / "generated").exists()  # type: ignore[operator]
            )

    def test_missing_fact_rejects_passed_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), include_fact=False)
            with self.assertRaisesRegex(CandidateTestTraceError, "缺少冻结事实"):
                self._generate(fixture)

    def test_cached_test_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), cached=True)
            with self.assertRaisesRegex(CandidateTestTraceError, "缓存结果"):
                self._generate(fixture)

    def test_missing_same_scenario_raw_relay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), include_raw=False)
            with self.assertRaisesRegex(CandidateTestTraceError, "缺少场景 A07"):
                self._generate(fixture)

    def test_changed_test_source_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            fixture["test_file"].write_text(  # type: ignore[union-attr]
                "package service\n// 未经冻结的修改\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CandidateTestTraceError, "源码快照 SHA-256"):
                self._generate(fixture)

    def test_mapping_cannot_authorize_raw_wire_record_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(
                Path(directory),
                record_type="http_request",
            )
            with self.assertRaisesRegex(CandidateTestTraceError, "不是允许的抽象事实"):
                load_mapping(
                    fixture["mapping"],  # type: ignore[arg-type]
                    expected_codex_version="0.145.0",
                    expected_sha256=file_sha256(
                        fixture["mapping"]  # type: ignore[arg-type]
                    ),
                )

    def test_output_paths_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            first = self._generate(fixture)
            with self.assertRaisesRegex(CandidateTestTraceError, "拒绝覆盖"):
                self._generate(fixture)
            receipt_path = fixture["evidence_root"] / "generated/test-trace-receipt.json"  # type: ignore[operator]
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8"))["generated"],
                first["generated"],
            )

    def test_mapping_and_receipt_schemas_pin_closed_versions(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        mapping_schema = json.loads(
            (tool_root / "candidate_test_fact_map.schema.json").read_text(
                encoding="utf-8"
            )
        )
        receipt_schema = json.loads(
            (tool_root / "candidate_test_trace_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            mapping_schema["properties"]["schema_version"]["const"],
            "codex-candidate-test-fact-map/v1",
        )
        self.assertEqual(
            receipt_schema["properties"]["schema_version"]["const"],
            "codex-candidate-test-trace-receipt/v1",
        )
        version_pattern = "^[0-9]+\\.[0-9]+\\.[0-9]+$"
        self.assertEqual(
            mapping_schema["properties"]["codex_version"]["pattern"],
            version_pattern,
        )
        self.assertEqual(
            receipt_schema["properties"]["codex_version"]["pattern"],
            version_pattern,
        )

    def test_default_mapping_digest_and_fact_universe_are_frozen(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        _, tests = load_mapping(
            tool_root / "candidate_test_fact_map_0_147_0.json",
            expected_codex_version="0.147.0",
            expected_sha256=candidate_test_trace.FROZEN_MAPPING_SHA256,
        )
        fact_ids = {
            fact.fact_id
            for test in tests
            for fact in test.facts
        }
        self.assertEqual(len(tests), 11)
        self.assertEqual(len(fact_ids), 31)
        self.assertIn("a07.oauth-fallback", fact_ids)
        self.assertIn("a08.connection-lifecycle", fact_ids)
        self.assertIn("a14.file-upload-url-chain", fact_ids)
        record_types = {
            fact.record_type
            for test in tests
            for fact in test.facts
        }
        self.assertNotIn("http_request", record_types)


if __name__ == "__main__":
    unittest.main()
