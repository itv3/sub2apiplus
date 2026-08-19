"""Claude FW-F target-first 画像、纵向样例和批准门禁测试。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_f_profile import (
    COMPILED_ENVELOPE_FIELDS,
    CLEARANCE_CLOSURE_SCHEMA,
    MEASURED_RULES_SCHEMA,
    WITHDRAWN_PROPOSALS_SCHEMA,
    ProfileBuildError,
    aggregate_profile,
    attach_lifecycle_profile,
    classify_header,
    compare_profiles,
    compile_vertical_sample,
    guide_rule_ids,
    parse_http_stream,
    rule_spec_ids_by_egress,
    validate_clearance_inputs,
    validate_compiled_envelope,
    validate_policy,
)


ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = ROOT / "tools/official_client_capture/claude_fw_f_profile_policy_2_1_226.json"
MEASURED_SPEC_IDS = sorted(
    ["SPEC-ACTIVE-001"] + [f"SPEC-TEST-{index:03d}" for index in range(1, 88)]
)


def encoded_request(body: dict[str, object], request_id: str) -> bytes:
    """生成包含显式 Content-Length 的脱敏 HTTP/1.1 请求。"""

    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        "POST /v1/messages?beta=true HTTP/1.1",
        "host: api.anthropic.com",
        "authorization: Bearer <secret>",
        "user-agent: claude-cli/9.9.9 (external, sdk-cli)",
        f"x-client-request-id: {request_id}",
        "content-type: application/json",
        f"content-length: {len(raw_body)}",
    ]
    return "\r\n".join(headers).encode("latin-1") + b"\r\n\r\n" + raw_body


def sample(
    scenario: str,
    version: str,
    request_id: str,
    secret_text: str = "用户私密提示词",
) -> dict[str, object]:
    """构造一条与真实 R 结构相同的最小样例。"""

    parsed = parse_http_stream(
        encoded_request(
            {
                "model": "claude-sonnet-5",
                "stream": True,
                "system": [{"type": "text", "text": "Persona 私密系统原文"}],
                "messages": [{"role": "user", "content": secret_text}],
            },
            request_id,
        ),
        scenario,
    )[0]
    parsed["scenario"] = scenario
    parsed["evidence"] = []
    parsed["headers"] = [
        {
            **header,
            "value": (
                f"claude-cli/{version} (external, sdk-cli)"
                if header["name"].lower() == "user-agent"
                else header["value"]
            ),
        }
        for header in parsed["headers"]
    ]
    return parsed


def lifecycle_sample(scenario: str, version: str) -> dict[str, object]:
    """构造不含 Body 的官方客户端生命周期探测。"""

    raw = (
        "HEAD /api/hello HTTP/1.1\r\n"
        "host: api.anthropic.com\r\n"
        f"user-agent: claude-cli/{version} (external, sdk-cli)\r\n"
        "accept: application/json\r\n"
        "content-length: 0\r\n\r\n"
    ).encode("latin-1")
    parsed = parse_http_stream(raw, scenario)[0]
    parsed["scenario"] = scenario
    parsed["evidence"] = []
    return parsed


def full_profile(role: str, version: str, request_id: str) -> dict[str, object]:
    """生成同时覆盖推理和生命周期端点的测试画像。"""

    return attach_lifecycle_profile(
        aggregate_profile(role, version, [sample("s1", version, request_id)], fake_rules()),
        aggregate_profile(
            f"{role}_lifecycle",
            version,
            [lifecycle_sample("s1", version)],
            [],
        ),
    )


def fake_rules() -> list[dict[str, str]]:
    """返回画像聚合所需的最小正交规则。"""

    return [
        {
            "spec_id": "SPEC-HDR-001",
            "domain": "header",
            "retained_claim": "User-Agent 来自 Release。",
            "evidence_level": "observed",
            "rule_lifecycle": "candidate",
            "compatibility_class": "request_egress",
            "migration_decision": "change",
            "production_eligibility": "denied_until_verified_and_approved",
        }
    ]


def fake_release(marker: str) -> dict[str, dict[str, str]]:
    """生成只用于纵向样例闭集校验的内容寻址引用。"""

    return {
        "release_artifact": {
            "object_kind": "release_artifact",
            "sha256": marker * 64,
        },
        "release_bundle": {
            "object_kind": "release_bundle",
            "sha256": marker.upper() * 64,
        },
    }


def clearance_fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """构造已清零发现项、动态 R/M 规则和 97 条撤回提案。"""

    clearance = {
        "schema_version": CLEARANCE_CLOSURE_SCHEMA,
        "result": "passed",
        "source_discovery_count": 7368,
        "resolved_record_count": 7368,
        "legacy_semantic_candidate_count": 32,
        "orthogonal_candidate_count": 593,
        "candidate_resolution_count": 593,
        "gate_counts": {"unresolved_record_count": 0, "orphan_reference_count": 0},
    }
    measured_rules = {
        "schema_version": MEASURED_RULES_SCHEMA,
        "result": "passed",
        "rule_count": len(MEASURED_SPEC_IDS),
        "entries": [
            {
                "spec_id": spec_id,
                "domain": "header",
                "assertion_id": f"PAIR-{spec_id}",
                "assertion_result": "passed",
                "compatibility_class": "request_egress",
                "egress_ids": ["egress-claude-messages-inference"],
                "evidence_channels": ["M", "R"],
                "evidence_refs": [
                    {"path": "evidence/identity.json", "channel": "M"},
                    {"path": f"evidence/{spec_id}.bin", "channel": "R"},
                ],
                "official_positive": {
                    "assertion_id": f"PAIR-{spec_id}-POSITIVE",
                    "result": "passed",
                    "sample_count": 1,
                },
                "official_negative": {
                    "assertion_id": f"PAIR-{spec_id}-NEGATIVE",
                    "result": "passed",
                    "sample_count": 1,
                },
                "applicability": ["version=2.1.226"],
                "applicability_scope": "1 条请求",
                "sample_scope": {
                    "eligible_count": 1,
                    "matched_count": 1,
                    "unit": "request",
                },
            }
            for spec_id in MEASURED_SPEC_IDS
        ],
    }
    withdrawn = {
        "schema_version": WITHDRAWN_PROPOSALS_SCHEMA,
        "proposal_count": 97,
        "withdrawn_count": 97,
        "active_rule_count": 0,
    }
    return clearance, measured_rules, withdrawn


class ClaudeFWFProfileTests(unittest.TestCase):
    """验证 target-first、最小共享合同和 validation-only 边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        cls.persona = cls.policy["persona"]
        cls.persona_contracts = cls.policy["persona_contracts"]

    def test_http_stream_splits_three_reused_requests(self) -> None:
        raw = b"".join(
            encoded_request(
                {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": index}]},
                f"00000000-0000-4000-8000-00000000000{index}",
            )
            for index in range(1, 4)
        )
        parsed = parse_http_stream(raw, "a1")
        self.assertEqual(len(parsed), 3)
        self.assertEqual([item["stream_offset"] for item in parsed], sorted(item["stream_offset"] for item in parsed))
        self.assertTrue(all(item["request_target"] == "/v1/messages?beta=true" for item in parsed))

    def test_header_classification_is_static_dynamic_and_conditional(self) -> None:
        self.assertEqual(
            classify_header("user-agent", ["same", "same"], 2, 2),
            {"classification": "static", "value": "same"},
        )
        self.assertEqual(
            classify_header(
                "x-client-request-id",
                ["00000000-0000-4000-8000-000000000001"],
                1,
                1,
            )["classification"],
            "dynamic",
        )
        self.assertEqual(
            classify_header("x-app", ["cli"], 1, 2)["classification"],
            "conditional",
        )
        self.assertEqual(
            classify_header("content-length", ["10"], 1, 1)["classification"],
            "derived",
        )

    def test_profile_does_not_persist_user_or_persona_text(self) -> None:
        profile = aggregate_profile(
            "target_first",
            "2.1.226",
            [sample("s1", "2.1.226", "00000000-0000-4000-8000-000000000001")],
            fake_rules(),
        )
        serialized = json.dumps(profile, ensure_ascii=False)
        self.assertNotIn("用户私密提示词", serialized)
        self.assertNotIn("Persona 私密系统原文", serialized)
        self.assertIn("shape_by_scenario", profile["body"])

    def test_target_is_built_before_baseline_and_diff_is_explicit(self) -> None:
        target = full_profile(
            "target_first",
            "2.1.226",
            "00000000-0000-4000-8000-000000000001",
        )
        baseline = full_profile(
            "baseline_fixture",
            "2.1.220",
            "00000000-0000-4000-8000-000000000002",
        )
        result = compare_profiles(target, baseline)
        self.assertEqual(result["generation_order"], ["target", "baseline"])
        self.assertEqual(
            result["changed_static_headers"]["messages-inference/user-agent"],
            {
                "baseline": "claude-cli/2.1.220 (external, sdk-cli)",
                "target": "claude-cli/2.1.226 (external, sdk-cli)",
            },
        )
        self.assertIn("lifecycle-hello/user-agent", result["changed_static_headers"])

    def test_compiled_envelope_has_only_vendor_neutral_control_fields(self) -> None:
        profile = full_profile(
            "target_first",
            "2.1.226",
            "00000000-0000-4000-8000-000000000001",
        )
        release = fake_release("a")
        vertical = compile_vertical_sample(
            profile,
            self.persona,
            {"object_kind": "persona_descriptor", "sha256": "c" * 64},
            release,
            "d" * 64,
            ["SPEC-HDR-001"],
            self.persona_contracts,
        )
        envelope = vertical["compiled_envelope"]
        self.assertEqual(set(envelope), COMPILED_ENVELOPE_FIELDS)
        self.assertFalse({"headers", "body", "version", "fallback", "beta_policy"} & set(envelope))
        self.assertEqual(vertical["guard"]["mode"], "validation_only")

        lifecycle = compile_vertical_sample(
            profile,
            self.persona,
            {"object_kind": "persona_descriptor", "sha256": "c" * 64},
            release,
            "d" * 64,
            ["SPEC-EP-002"],
            self.persona_contracts,
            "lifecycle-hello",
        )
        self.assertEqual(lifecycle["compiled_envelope"]["method"], "HEAD")
        self.assertEqual(
            lifecycle["compiled_envelope"]["endpoint"]["request_target"],
            "/api/hello",
        )

        expanded = {**envelope, "headers": {"x-vendor": "forbidden"}}
        with self.assertRaisesRegex(ProfileBuildError, "字段闭集"):
            validate_compiled_envelope(
                expanded,
                profile,
                self.persona,
                release,
                "d" * 64,
                self.persona_contracts,
            )

    def test_cross_release_and_cross_persona_envelopes_are_rejected(self) -> None:
        profile = full_profile(
            "target_first",
            "2.1.226",
            "00000000-0000-4000-8000-000000000001",
        )
        release = fake_release("a")
        vertical = compile_vertical_sample(
            profile,
            self.persona,
            {"object_kind": "persona_descriptor", "sha256": "c" * 64},
            release,
            "d" * 64,
            ["SPEC-HDR-001"],
            self.persona_contracts,
        )
        cross_release = copy.deepcopy(vertical["compiled_envelope"])
        cross_release["release_artifact_ref"] = fake_release("b")["release_artifact"]
        with self.assertRaisesRegex(ProfileBuildError, "合同不一致"):
            validate_compiled_envelope(
                cross_release,
                profile,
                self.persona,
                release,
                "d" * 64,
                self.persona_contracts,
            )

        cross_persona = copy.deepcopy(vertical["compiled_envelope"])
        cross_persona["persona_sha256"] = "e" * 64
        with self.assertRaisesRegex(ProfileBuildError, "合同不一致"):
            validate_compiled_envelope(
                cross_persona,
                profile,
                self.persona,
                release,
                "d" * 64,
                self.persona_contracts,
            )

    def test_only_validation_only_policy_is_accepted(self) -> None:
        validate_policy(copy.deepcopy(self.policy))
        invalid = copy.deepcopy(self.policy)
        invalid["approval_purpose"] = "production_replacement"
        with self.assertRaisesRegex(ProfileBuildError, "validation_only"):
            validate_policy(invalid)

    def test_guide_rule_ids_require_one_bounded_unique_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            guide = Path(temporary_directory) / "guide.md"
            guide.write_text(
                "<!-- FW-F-ACTIVE-RULES-BEGIN -->\n"
                "| `SPEC-B-001` | B |\n"
                "| `SPEC-A-001` | A |\n"
                "<!-- FW-F-ACTIVE-RULES-END -->\n",
                encoding="utf-8",
            )
            self.assertEqual(guide_rule_ids(guide), ["SPEC-A-001", "SPEC-B-001"])

            guide.write_text(
                "<!-- FW-F-ACTIVE-RULES-BEGIN -->\n"
                "| `SPEC-A-001` | A |\n"
                "| `SPEC-A-001` | A |\n"
                "<!-- FW-F-ACTIVE-RULES-END -->\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProfileBuildError, "为空或重复"):
                guide_rule_ids(guide)

    def test_rules_are_grouped_by_declared_strict_egress(self) -> None:
        rules = [
            {
                "spec_id": "SPEC-EP-001",
                "egress_ids": ["egress-claude-messages-inference"],
            },
            {
                "spec_id": "SPEC-EP-008",
                "egress_ids": [
                    "egress-claude-lifecycle-hello",
                    "egress-claude-messages-inference",
                ],
            },
        ]
        result = rule_spec_ids_by_egress(
            rules,
            ["egress-claude-lifecycle-hello", "egress-claude-messages-inference"],
        )
        self.assertEqual(result["egress-claude-lifecycle-hello"], ["SPEC-EP-008"])
        self.assertEqual(
            result["egress-claude-messages-inference"],
            ["SPEC-EP-001", "SPEC-EP-008"],
        )

    def test_nonzero_discovery_clearance_gate_is_rejected(self) -> None:
        clearance, measured_rules, withdrawn = clearance_fixture()
        validate_clearance_inputs(clearance, measured_rules, withdrawn)
        clearance["gate_counts"]["unresolved_record_count"] = 1
        with self.assertRaisesRegex(ProfileBuildError, "非零门禁"):
            validate_clearance_inputs(clearance, measured_rules, withdrawn)

    def test_rule_without_raw_r_evidence_is_rejected(self) -> None:
        clearance, measured_rules, withdrawn = clearance_fixture()
        measured_rules["entries"][0]["evidence_refs"] = [
            {"path": "evidence/identity.json", "channel": "M"}
        ]
        with self.assertRaisesRegex(ProfileBuildError, "原始 R 证据"):
            validate_clearance_inputs(clearance, measured_rules, withdrawn)

    def test_blocked_rule_classes_are_rejected(self) -> None:
        forbidden_ids = [
            "SPEC-HDR-011",
            "SPEC-HDR-034",
            "SPEC-RESP-001",
            "SPEC-STATE-002",
        ]
        for forbidden_id in forbidden_ids:
            with self.subTest(spec_id=forbidden_id):
                clearance, measured_rules, withdrawn = clearance_fixture()
                measured_rules["entries"][-1]["spec_id"] = forbidden_id
                measured_rules["entries"] = sorted(
                    measured_rules["entries"], key=lambda value: value["spec_id"]
                )
                with self.assertRaisesRegex(ProfileBuildError, "非活动规则"):
                    validate_clearance_inputs(clearance, measured_rules, withdrawn)

    def test_tls_rule_uses_native_p_and_m(self) -> None:
        clearance, measured_rules, withdrawn = clearance_fixture()
        tls = measured_rules["entries"][-1]
        tls.update({
            "spec_id": "SPEC-TLS-001",
            "domain": "tls",
            "assertion_id": "PAIR-SPEC-TLS-001",
            "official_positive": {
                "assertion_id": "PAIR-SPEC-TLS-001-POSITIVE",
                "result": "passed",
                "sample_count": 1,
            },
            "official_negative": {
                "assertion_id": "PAIR-SPEC-TLS-001-NEGATIVE",
                "result": "passed",
                "sample_count": 1,
            },
            "evidence_channels": ["M", "P"],
            "evidence_refs": [
                {"path": "evidence/identity.json", "channel": "M"},
                {"path": "evidence/clienthello.json", "channel": "P"},
            ],
        })
        measured_rules["entries"] = sorted(measured_rules["entries"], key=lambda value: value["spec_id"])
        validate_clearance_inputs(clearance, measured_rules, withdrawn)

        tls["evidence_channels"] = ["M", "R"]
        with self.assertRaisesRegex(ProfileBuildError, "缺少 P/M"):
            validate_clearance_inputs(clearance, measured_rules, withdrawn)

    def test_all_v1_proposals_must_be_withdrawn(self) -> None:
        clearance, measured_rules, withdrawn = clearance_fixture()
        withdrawn["withdrawn_count"] = 96
        with self.assertRaisesRegex(ProfileBuildError, "没有全部撤回"):
            validate_clearance_inputs(clearance, measured_rules, withdrawn)


if __name__ == "__main__":
    unittest.main()
