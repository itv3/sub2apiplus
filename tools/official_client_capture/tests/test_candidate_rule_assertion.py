"""候选侧冻结画像与独立逐规则断言器测试。"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.candidate_rule_assertion import (
    AssertionConfigurationError,
    Observation,
    _evaluate_assertion,
    build_assertion_command,
    build_assertion_result,
    command_sha256,
    evaluate_rule,
    file_sha256,
    load_observations,
    load_profile,
    main as assertion_main,
    source_spec_section_sha256,
)


TOOL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = TOOL_ROOT / "candidate_rule_expectations_0_145_0.json"
RULE_MANIFEST_PATH = TOOL_ROOT / "codex_upgrade_rules_0_145_0.json"


def write_manifest(
    path: Path,
    artifacts: list[dict[str, object]],
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "codex-candidate-capture-manifest/v1",
                "codex_version": "0.145.0",
                "capture_id": "unit-test-capture",
                "status": "complete",
                "artifacts": artifacts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class CandidateRuleExpectationTest(unittest.TestCase):
    def test_source_spec_fragment_digest_ignores_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "spec.md"
            source.write_text(
                "# 第一部分 背景\n旧内容\n# 第二部分 规则\n冻结规则\n# 第三部分 方案\n旧方案\n",
                encoding="utf-8",
            )
            before = source_spec_section_sha256(source, "第二章")
            source.write_text(
                "# 第一部分 背景\n新内容\n# 第二部分 规则\n冻结规则\n# 第三部分 方案\n新方案\n# 第四部分 升级\n新增流程\n",
                encoding="utf-8",
            )
            self.assertEqual(
                source_spec_section_sha256(source, "第二章"), before
            )

    def test_profile_is_exact_closed_42_rule_universe(self) -> None:
        profile = load_profile(PROFILE_PATH, RULE_MANIFEST_PATH)
        manifest = json.loads(RULE_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(profile["scenarios"]), 15)
        self.assertEqual(len(profile["rules"]), 42)
        self.assertEqual(
            [rule["rule_id"] for rule in profile["rules"]],
            manifest["required_rules"],
        )
        self.assertGreaterEqual(
            sum(len(rule["checks"]) for rule in profile["rules"]),
            42,
        )
        spec_relative, _, fragment = profile["source_spec"].partition("#")
        spec_path = TOOL_ROOT.parents[1] / spec_relative
        self.assertEqual(
            source_spec_section_sha256(spec_path, fragment),
            profile["source_spec_sha256"],
        )

    def test_profile_is_independent_from_candidate_go_profile(self) -> None:
        checker_source = (TOOL_ROOT / "candidate_rule_assertion.py").read_text(
            encoding="utf-8"
        )
        profile_source = PROFILE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("official_egress_codex_0145_profile", checker_source)
        self.assertNotIn("backend/internal/service", checker_source)
        self.assertNotIn("backend/internal/service", profile_source)

    def test_capture_and_observation_schemas_are_pinned(self) -> None:
        capture_schema = json.loads(
            (TOOL_ROOT / "candidate_capture_manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        observation_schema = json.loads(
            (TOOL_ROOT / "candidate_observation.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            capture_schema["properties"]["schema_version"]["const"],
            "codex-candidate-capture-manifest/v1",
        )
        self.assertEqual(
            capture_schema["properties"]["codex_version"]["pattern"],
            "^[0-9]+\\.[0-9]+\\.[0-9]+$",
        )
        self.assertEqual(
            observation_schema["properties"]["schema_version"]["const"],
            "codex-candidate-observation/v1",
        )
        artifact_schema = capture_schema["properties"]["artifacts"]["items"]
        frame_labels_schema = artifact_schema["properties"]["frame_labels"]
        self.assertEqual(frame_labels_schema["type"], "object")
        self.assertEqual(
            frame_labels_schema["propertyNames"]["pattern"],
            "^(0|[1-9][0-9]*)$",
        )
        # 帧级标签的两条合法路径：直接解析原始字节（h1_request_stream），或候选侧
        # 走 opaque+derive 后由派生 jsonl 承载帧观测（observation_jsonl）。
        self.assertEqual(
            artifact_schema["allOf"][0]["then"]["properties"]["parser"]["enum"],
            ["h1_request_stream", "observation_jsonl"],
        )

    def test_profile_missing_rule_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            profile["rules"].pop()
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(
            AssertionConfigurationError, "精确覆盖目标版本规则全集"
            ):
                load_profile(
                    path,
                    RULE_MANIFEST_PATH,
                    verify_frozen_digest=False,
                )

    def test_profile_digest_is_pinned_by_checker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            profile["rules"][0]["description"] += "被未审核修改"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(
                AssertionConfigurationError, "SHA-256 不匹配"
            ):
                load_profile(path, RULE_MANIFEST_PATH)

    def test_approved_profile_status_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            profile["status"] = "approved"
            path.write_text(json.dumps(profile), encoding="utf-8")
            loaded = load_profile(
                path,
                RULE_MANIFEST_PATH,
                verify_frozen_digest=False,
                expected_profile_sha256=file_sha256(path),
            )
            self.assertEqual(loaded["status"], "approved")

    def test_nonapproved_profile_status_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            profile["status"] = "draft"
            path.write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(
                AssertionConfigurationError,
                "status 存在时必须为 approved",
            ):
                load_profile(
                    path,
                    RULE_MANIFEST_PATH,
                    verify_frozen_digest=False,
                )


class CandidateRuleAssertionTest(unittest.TestCase):
    @staticmethod
    def _tls_observation(
        index: int,
        sequence: list[int],
        *,
        artifact: str | None = None,
    ) -> Observation:
        return Observation(
            record_id=f"tls-{index}",
            scenario_id="A02",
            record_type="tls_client_hello",
            artifact_path=artifact or f"direct/ws-{index}.pcap",
            evidence_paths=(artifact or f"direct/ws-{index}.pcap",),
            labels={"transport": "websocket"},
            data={"extension_types": sequence},
        )

    def test_tls_order_operator_accepts_one_qualifying_subset(self) -> None:
        observations = [
            self._tls_observation(1, [0, 11, 10]),
            self._tls_observation(2, [11, 0, 10]),
            self._tls_observation(3, [0, 10, 11]),
            self._tls_observation(4, [10, 0, 11]),
            # 同一 selector 里的另一种 TLS 实现拥有不同扩展集合；它不应把
            # 上面已经成立的四份官方 WS 子集整体否掉。
            self._tls_observation(5, [0, 11, 10, 35]),
        ]
        passed, actual = _evaluate_assertion(
            observations,
            {
                "operator": "same_set_distinct_order",
                "path": "data.extension_types",
                "minimum_records": 4,
                "minimum_artifacts": 4,
                "minimum_distinct_orders": 2,
            },
        )
        self.assertTrue(passed)
        self.assertEqual(actual["matching_group_count"], 1)
        self.assertEqual(actual["selected_group"]["artifact_count"], 4)

    def test_tls_order_operator_requires_independent_artifacts(self) -> None:
        observations = [
            self._tls_observation(
                index,
                [0, 11, 10] if index % 2 else [11, 0, 10],
                artifact="direct/shared.pcap",
            )
            for index in range(1, 5)
        ]
        passed, actual = _evaluate_assertion(
            observations,
            {
                "operator": "same_set_distinct_order",
                "path": "data.extension_types",
                "minimum_records": 4,
                "minimum_artifacts": 4,
                "minimum_distinct_orders": 2,
            },
        )
        self.assertFalse(passed)
        self.assertEqual(actual["groups"][0]["artifact_count"], 1)

    def test_tls_order_operator_rejects_non_list_observation(self) -> None:
        observations = [
            self._tls_observation(1, [0, 11, 10]),
            self._tls_observation(2, [11, 0, 10]),
            self._tls_observation(3, [0, 10, 11]),
            self._tls_observation(4, [10, 0, 11]),
            Observation(
                record_id="tls-invalid",
                scenario_id="A02",
                record_type="tls_client_hello",
                artifact_path="direct/ws-invalid.pcap",
                evidence_paths=("direct/ws-invalid.pcap",),
                labels={"transport": "websocket"},
                data={"extension_types": "not-a-list"},
            ),
        ]
        passed, actual = _evaluate_assertion(
            observations,
            {
                "operator": "same_set_distinct_order",
                "path": "data.extension_types",
                "minimum_records": 4,
                "minimum_artifacts": 4,
                "minimum_distinct_orders": 2,
            },
        )
        self.assertFalse(passed)
        self.assertEqual(actual["invalid_record_ids"], ["tls-invalid"])

    @staticmethod
    def _masked_text_frame(value: dict[str, object]) -> bytes:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        if len(payload) <= 125:
            length = bytes((0x80 | len(payload),))
        else:
            length = bytes((0x80 | 126,)) + struct.pack(">H", len(payload))
        mask = b"\x11\x22\x33\x44"
        masked = bytes(
            byte ^ mask[index % len(mask)]
            for index, byte in enumerate(payload)
        )
        return b"\x81" + length + mask + masked

    def _websocket_fixture(
        self,
        root: Path,
        *,
        frame_labels: object,
        labels: dict[str, object] | None = None,
        parser: str = "h1_request_stream",
        kind: str = "relay_binary",
    ) -> tuple[Path, Path, Path]:
        evidence_root = root / "evidence"
        evidence = evidence_root / "candidate/A06/conn001.client_to_upstream.bin"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"host: chatgpt.com\r\n"
            b"upgrade: websocket\r\n\r\n"
            + self._masked_text_frame(
                {"type": "response.create", "marker": "warmup"}
            )
            + self._masked_text_frame(
                {"type": "response.create", "marker": "business"}
            )
        )
        manifest = root / "capture-manifest.json"
        write_manifest(
            manifest,
            [
                {
                    "path": "candidate/A06/conn001.client_to_upstream.bin",
                    "sha256": file_sha256(evidence),
                    "kind": kind,
                    "parser": parser,
                    "scenario_ids": ["A06"],
                    "labels": labels
                    if labels is not None
                    else {"connection_id": "conn001", "variant": "default"},
                    "frame_labels": frame_labels,
                }
            ],
        )
        return evidence_root, manifest, evidence

    def _models_fixture(
        self, root: Path, *, version: str = "0.145.0"
    ) -> tuple[Path, Path]:
        evidence_root = root / "evidence"
        evidence = evidence_root / "candidate/A09/models.bin"
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(
            (
                "GET /backend-api/codex/models?client_version="
                f"{version} HTTP/1.1\r\n"
                "version: 0.145.0\r\n"
                "accept: */*\r\n"
                "originator: codex_exec\r\n"
                "user-agent: codex_exec/0.145.0\r\n"
                "host: chatgpt.com\r\n\r\n"
            ).encode("ascii")
        )
        trace = evidence_root / "candidate/A09/capture-integrity.jsonl"
        trace.write_text(
            json.dumps(
                {
                    "schema_version": "codex-candidate-observation/v1",
                    "record_id": "A09-capture-integrity",
                    "scenario_id": "A09",
                    "record_type": "capture_integrity",
                    "data": {"status": "complete"},
                    "source_artifacts": ["candidate/A09/models.bin"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        manifest = root / "capture-manifest.json"
        write_manifest(
            manifest,
            [
                {
                    "path": "candidate/A09/models.bin",
                    "sha256": file_sha256(evidence),
                    "kind": "relay_binary",
                    "parser": "h1_request_stream",
                    "scenario_ids": ["A09"],
                    "labels": {
                        "protocol": "http",
                        "provider": "openai_oauth",
                        "endpoint_class": "auxiliary",
                    },
                },
                {
                    "path": "candidate/A09/capture-integrity.jsonl",
                    "sha256": file_sha256(trace),
                    "kind": "process_trace",
                    "parser": "observation_jsonl",
                    "scenario_ids": ["A09"],
                    "labels": {},
                }
            ],
        )
        return evidence_root, manifest

    def test_raw_h1_evidence_passes_ep006(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest = self._models_fixture(root)
            profile = load_profile(PROFILE_PATH, RULE_MANIFEST_PATH)
            capture_manifest, observations = load_observations(
                manifest, evidence_root
            )
            checks = evaluate_rule(
                profile, "SPEC-EP-006", observations, capture_manifest
            )
            self.assertTrue(all(check["passed"] for check in checks), checks)
            self.assertEqual(
                checks[1]["evidence_paths"], ["candidate/A09/models.bin"]
            )
            self.assertEqual(
                checks[1]["actual"]["values"][0]["query_pairs"],
                [["client_version", "0.145.0"]],
            )

    def test_wrong_version_fails_semantically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest = self._models_fixture(
                root, version="0.144.0"
            )
            profile = load_profile(PROFILE_PATH, RULE_MANIFEST_PATH)
            capture_manifest, observations = load_observations(
                manifest, evidence_root
            )
            checks = evaluate_rule(
                profile, "SPEC-EP-006", observations, capture_manifest
            )
            self.assertFalse(checks[1]["passed"])

    def test_manifest_hash_mismatch_fails_before_assertion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest = self._models_fixture(root)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["artifacts"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                AssertionConfigurationError, "SHA-256 不匹配"
            ):
                load_observations(manifest, evidence_root)

    def test_frame_labels_merge_only_into_selected_websocket_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest, evidence = self._websocket_fixture(
                root,
                frame_labels={"0": {"frame_role": "warmup"}},
            )
            original_bytes = evidence.read_bytes()

            _, observations = load_observations(manifest, evidence_root)

            request = next(
                item for item in observations if item.record_type == "http_request"
            )
            frames = [
                item
                for item in observations
                if item.record_type == "websocket_frame"
            ]
            self.assertEqual([item.data["frame_index"] for item in frames], [0, 1])
            self.assertEqual(frames[0].labels["frame_role"], "warmup")
            self.assertEqual(frames[0].labels["connection_id"], "conn001")
            self.assertNotIn("frame_role", frames[1].labels)
            self.assertNotIn("frame_role", request.labels)
            self.assertEqual(evidence.read_bytes(), original_bytes)

    def test_frame_labels_reject_malformed_shapes_and_values(self) -> None:
        cases = (
            ("必须是非空对象", {}),
            ("必须是非空对象", []),
            ("索引非法", {"00": {"frame_role": "warmup"}}),
            ("索引非法", {"-1": {"frame_role": "warmup"}}),
            ("必须是非空对象", {"0": {}}),
            ("必须是非空对象", {"0": []}),
            ("标签名非法", {"0": {"FrameRole": "warmup"}}),
            ("必须是非空字符串", {"0": {"frame_role": True}}),
            ("必须是非空字符串", {"0": {"frame_role": "   "}}),
        )
        for expected_error, frame_labels in cases:
            with self.subTest(frame_labels=frame_labels):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    evidence_root, manifest, _ = self._websocket_fixture(
                        root,
                        frame_labels=frame_labels,
                    )
                    with self.assertRaisesRegex(
                        AssertionConfigurationError, expected_error
                    ):
                        load_observations(manifest, evidence_root)

    def test_frame_labels_reject_incompatible_parser_and_label_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest, _ = self._websocket_fixture(
                root,
                frame_labels={"0": {"frame_role": "warmup"}},
                parser="opaque_bound_source",
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError,
                "仅支持 h1_request_stream 或 observation_jsonl",
            ):
                load_observations(manifest, evidence_root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest, _ = self._websocket_fixture(
                root,
                frame_labels={"0": {"variant": "warmup"}},
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError, "不能覆盖 artifact labels"
            ):
                load_observations(manifest, evidence_root)

    def test_frame_labels_merge_into_derived_observation_frames(self) -> None:
        """候选侧 relay 走 opaque+derive，帧观测在派生 jsonl 里——标签必须同样落到帧上。

        官方侧 relay 原件可直接以 h1_request_stream 解析，候选侧则先收口为
        opaque_bound_source 再由 ACC-02b 派生器投影，两条路径的帧级标签语义必须一致，
        否则 A06 的 warmup 帧在候选侧永远选不中。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            trace = evidence_root / "derived/A06/websocket_trace/conn001.jsonl"
            trace.parent.mkdir(parents=True)
            records = [
                {
                    "schema_version": "codex-candidate-observation/v1",
                    "record_id": "conn001#frame-0",
                    "scenario_id": "A06",
                    "record_type": "websocket_frame",
                    "data": {
                        "opcode": "TEXT",
                        "event_type": "response.create",
                        "frame_index": 0,
                    },
                },
                {
                    "schema_version": "codex-candidate-observation/v1",
                    "record_id": "conn001#frame-1",
                    "scenario_id": "A06",
                    "record_type": "websocket_frame",
                    "data": {
                        "opcode": "TEXT",
                        "event_type": "response.create",
                        "frame_index": 1,
                    },
                },
            ]
            trace.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
                + "\n",
                encoding="utf-8",
            )
            manifest = root / "capture-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-capture-manifest/v1",
                        "codex_version": "0.145.0",
                        "capture_id": "frame-label-derived",
                        "status": "complete",
                        "artifacts": [
                            {
                                "path": "derived/A06/websocket_trace/conn001.jsonl",
                                "sha256": file_sha256(trace),
                                "kind": "websocket_trace",
                                "parser": "observation_jsonl",
                                "scenario_ids": ["A06"],
                                "labels": {"variant": "default"},
                                "frame_labels": {"0": {"frame_role": "warmup"}},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            _, observations = load_observations(manifest, evidence_root)

            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0].labels["frame_role"], "warmup")
            self.assertEqual(observations[0].labels["variant"], "default")
            self.assertNotIn("frame_role", observations[1].labels)

    def test_derived_frame_labels_reject_missing_frame_index(self) -> None:
        """派生路径同样要求帧索引真实存在，防止标签指向不存在的帧而静默失效。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            trace = evidence_root / "derived/A06/websocket_trace/conn001.jsonl"
            trace.parent.mkdir(parents=True)
            trace.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-observation/v1",
                        "record_id": "conn001#frame-0",
                        "scenario_id": "A06",
                        "record_type": "websocket_frame",
                        "data": {"opcode": "TEXT", "frame_index": 0},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = root / "capture-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-capture-manifest/v1",
                        "codex_version": "0.145.0",
                        "capture_id": "frame-label-derived-missing",
                        "status": "complete",
                        "artifacts": [
                            {
                                "path": "derived/A06/websocket_trace/conn001.jsonl",
                                "sha256": file_sha256(trace),
                                "kind": "websocket_trace",
                                "parser": "observation_jsonl",
                                "scenario_ids": ["A06"],
                                "labels": {"variant": "default"},
                                "frame_labels": {"3": {"frame_role": "warmup"}},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError, "不存在的 WebSocket 帧"
            ):
                load_observations(manifest, evidence_root)

    def test_frame_labels_reject_missing_websocket_frame_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest, _ = self._websocket_fixture(
                root,
                frame_labels={"2": {"frame_role": "warmup"}},
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError, "不存在的 WebSocket 帧"
            ):
                load_observations(manifest, evidence_root)

    def test_structured_trace_must_bind_declared_raw_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            trace = evidence_root / "candidate/A07/fallback.jsonl"
            trace.parent.mkdir(parents=True)
            trace.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-observation/v1",
                        "record_id": "fallback-1",
                        "scenario_id": "A07",
                        "record_type": "transport_fallback",
                        "data": {
                            "retry_budget_state": "exhausted",
                            "transports": ["websocket", "http"],
                            "invocation_ids": ["hash-1", "hash-1"],
                        },
                        "source_artifacts": ["candidate/A07/not-declared.bin"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = root / "capture-manifest.json"
            write_manifest(
                manifest,
                [
                    {
                        "path": "candidate/A07/fallback.jsonl",
                        "sha256": file_sha256(trace),
                        "kind": "process_trace",
                        "parser": "observation_jsonl",
                        "scenario_ids": ["A07"],
                        "labels": {},
                    }
                ],
            )
            with self.assertRaisesRegex(
                AssertionConfigurationError, "manifest 外证据"
            ):
                load_observations(manifest, evidence_root)

    def test_opaque_raw_response_must_be_bound_by_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            raw = evidence_root / "candidate/A07/upstream-to-client.bin"
            trace = evidence_root / "candidate/A07/fallback.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"HTTP/1.1 426 Upgrade Required\r\ncontent-length: 0\r\n\r\n")
            trace.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-observation/v1",
                        "record_id": "fallback-bound",
                        "scenario_id": "A07",
                        "record_type": "transport_fallback",
                        "data": {
                            "retry_budget_state": "exhausted",
                            "transports": ["websocket", "http"],
                            "invocation_ids": ["hash-1", "hash-1"],
                        },
                        "source_artifacts": [
                            "candidate/A07/upstream-to-client.bin"
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = root / "capture-manifest.json"
            write_manifest(
                manifest,
                [
                    {
                        "path": "candidate/A07/upstream-to-client.bin",
                        "sha256": file_sha256(raw),
                        "kind": "relay_binary",
                        "parser": "opaque_bound_source",
                        "scenario_ids": ["A07"],
                        "labels": {},
                    },
                    {
                        "path": "candidate/A07/fallback.jsonl",
                        "sha256": file_sha256(trace),
                        "kind": "process_trace",
                        "parser": "observation_jsonl",
                        "scenario_ids": ["A07"],
                        "labels": {},
                    },
                ],
            )
            _, observations = load_observations(manifest, evidence_root)
            fallback = next(
                item for item in observations if item.record_type == "transport_fallback"
            )
            self.assertEqual(
                fallback.evidence_paths,
                (
                    "candidate/A07/fallback.jsonl",
                    "candidate/A07/upstream-to-client.bin",
                ),
            )

            trace_value = json.loads(trace.read_text(encoding="utf-8"))
            trace_value.pop("source_artifacts")
            trace.write_text(json.dumps(trace_value) + "\n", encoding="utf-8")
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_value["artifacts"][1]["sha256"] = file_sha256(trace)
            manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
            with self.assertRaisesRegex(
                AssertionConfigurationError, "opaque 原始证据"
            ):
                load_observations(manifest, evidence_root)

    def test_assertion_result_hashes_canonical_command(self) -> None:
        command = build_assertion_command(
            rule_id="SPEC-EP-006",
            capture_manifest="evidence/capture-manifest.json",
            evidence_root="evidence",
            output="evidence/assertions/SPEC-EP-006.result.json",
        )
        checks = [
            {
                "id": "models-method-path-query",
                "description": "models 方法、路径与 query 匹配",
                "passed": True,
                "expected": {"operator": "all_subset"},
                "actual": {"matched_count": 1},
                "evidence_paths": ["candidate/A09/models.bin"],
            }
        ]
        result = build_assertion_result(
            rule_id="SPEC-EP-006",
            checks=checks,
            command=command,
            started_at="2026-07-30T10:00:00+08:00",
            finished_at="2026-07-30T10:00:01+08:00",
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["command_sha256"], command_sha256(command))
        self.assertRegex(result["checker_sha256"], r"^[0-9a-f]{64}$")

    def test_cli_writes_candidate_acceptance_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root, manifest = self._models_fixture(root)
            output = evidence_root / "assertions/SPEC-EP-006.result.json"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = assertion_main(
                    [
                        "--rule-id",
                        "SPEC-EP-006",
                        "--capture-manifest",
                        str(manifest),
                        "--evidence-root",
                        str(evidence_root),
                        "--profile",
                        str(PROFILE_PATH),
                        "--rule-manifest",
                        str(RULE_MANIFEST_PATH),
                        "--side",
                        "candidate",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exit_code, 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(result),
                {
                    "schema_version",
                    "rule_id",
                    "status",
                    "started_at",
                    "finished_at",
                    "exit_code",
                    "checker_sha256",
                    "command_sha256",
                    "checks",
                },
            )
            self.assertEqual(result["schema_version"], "codex-candidate-rule-assertion/v1")
            self.assertEqual(result["status"], "pass")
            self.assertTrue(all(check["passed"] for check in result["checks"]))
            expected_command = build_assertion_command(
                rule_id="SPEC-EP-006",
                capture_manifest=str(manifest),
                evidence_root=str(evidence_root),
                profile=str(PROFILE_PATH),
                rule_manifest=str(RULE_MANIFEST_PATH),
                side="candidate",
                output=str(output),
            )
            self.assertEqual(
                result["command_sha256"], command_sha256(expected_command)
            )


if __name__ == "__main__":
    unittest.main()
