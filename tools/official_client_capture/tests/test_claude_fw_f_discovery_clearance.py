"""Claude FW-F 发现项语义清零工具测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_f_discovery_clearance import (
    DiscoveryClearanceError,
    build_clearance,
)
from tools.official_client_control.canonical import canonical_json_bytes


MEASURED_SPEC_IDS = sorted(
    ["SPEC-NEW-001"] + [f"SPEC-TEST-{index:03d}" for index in range(1, 88)]
)
PRIOR_PROPOSAL_IDS = [f"SPEC-PROP-{index:03d}" for index in range(1, 98)]
STRICT_EGRESS_IDS = [
    "egress-claude-lifecycle-hello",
    "egress-claude-messages-inference",
    "egress-claude-oauth-profile",
    "egress-claude-policy-limits",
    "egress-claude-settings",
]


def write_json(path: Path, value: object) -> None:
    """写入测试用规范 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def measured_entry(spec_id: str) -> dict[str, object]:
    """构造一条同时具备 R/M 证据的实测规则。"""

    return {
        "spec_id": spec_id,
        "assertion_id": f"PAIR-{spec_id}",
        "assertion_result": "passed",
        "evidence_level": "observed",
        "compatibility_class": "request_egress",
        "egress_ids": (
            STRICT_EGRESS_IDS
            if spec_id == "SPEC-NEW-001"
            else ["egress-claude-messages-inference"]
        ),
        "evidence_channels": ["M", "R"],
        "evidence_refs": [
            {"path": "evidence/identity.json", "channel": "M"},
            {"path": f"evidence/{spec_id}.bin", "channel": "R"},
        ],
    }


class DiscoveryClearanceTests(unittest.TestCase):
    """验证逐项覆盖、实测规则、提案撤回和负向门禁。"""

    def _fixture(self, root: Path) -> dict[str, Path]:
        target_version = "9.9.9"
        target_binary_sha256 = "a" * 64
        document_path = "local-analysis/sources/hitcc/docs/context.md"
        paths = {
            "discovery": root / "discovery.json",
            "candidates": root / "candidates.json",
            "rules": root / "rules.json",
            "atoms": root / "atoms.json",
            "egress": root / "egress.json",
            "measured": root / "measured.json",
            "prior": root / "prior-rule-additions.json",
            "policy": root / "policy.json",
        }

        write_json(
            paths["discovery"],
            {
                "schema_version": "claude-code-fw-e-discovery-inventory/v1",
                "target_version": target_version,
                "item_count": 5,
                "items": [
                    {
                        "discovery_id": "D-RULE",
                        "source_kind": "historical_source",
                        "disposition": "mapped_existing_rule",
                        "proposition": "既有规则证据。",
                        "evidence_paths": ["rules.json"],
                        "spec_ids": ["SPEC-NEW-001"],
                        "semantic_candidate_ids": [],
                    },
                    {
                        "discovery_id": "D-CAND",
                        "source_kind": "target_ast_call",
                        "disposition": "mapped_semantic_candidate",
                        "proposition": "目标候选证据。",
                        "evidence_paths": ["target.json"],
                        "spec_ids": [],
                        "semantic_candidate_ids": ["CAND-RULE"],
                    },
                    {
                        "discovery_id": "D-MANAGED",
                        "source_kind": "target_ast_call",
                        "disposition": "mapped_semantic_candidate",
                        "proposition": "受管出站证据。",
                        "evidence_paths": ["target.json"],
                        "spec_ids": [],
                        "semantic_candidate_ids": ["CAND-MANAGED"],
                    },
                    {
                        "discovery_id": "HDOC-LINK",
                        "source_kind": "hitcc_document_atom",
                        "disposition": "catalogued_context",
                        "proposition": "[相关页](other.md)",
                        "evidence_paths": ["atoms.json", document_path],
                        "spec_ids": [],
                        "semantic_candidate_ids": [],
                    },
                    {
                        "discovery_id": "HDOC-TEXT",
                        "source_kind": "hitcc_document_atom",
                        "disposition": "catalogued_context",
                        "proposition": "上下文会影响消息构造。",
                        "evidence_paths": ["atoms.json", document_path],
                        "spec_ids": [],
                        "semantic_candidate_ids": [],
                    },
                ],
            },
        )
        write_json(
            paths["candidates"],
            {
                "schema_version": "claude-code-fw-e-semantic-candidates/v1",
                "target_version": target_version,
                "candidate_count": 2,
                "candidates": [
                    {
                        "id": "CAND-RULE",
                        "candidate_kind": "wire_semantic",
                        "domain": "header",
                        "retained_claim": "候选形成实测规则。",
                        "source_ids": ["D-CAND"],
                        "evidence_level": "observed",
                        "required_channels": ["R", "M"],
                    },
                    {
                        "id": "CAND-MANAGED",
                        "candidate_kind": "managed_semantic",
                        "domain": "endpoint",
                        "retained_claim": "候选进入受管出站。",
                        "source_ids": ["D-MANAGED"],
                        "evidence_level": "observed",
                        "required_channels": ["J", "R", "M"],
                    },
                ],
            },
        )
        write_json(
            paths["rules"],
            {
                "schema_version": "claude-code-fw-e-rule-assessments/v2",
                "target_version": target_version,
                "rule_count": 1,
                "rules": [{"spec_id": "SPEC-NEW-001"}],
            },
        )
        write_json(
            paths["atoms"],
            {
                "schema_version": "claude-code-fw-e-hitcc-document-atoms/v2",
                "target_version": target_version,
                "documents": [
                    {
                        "path": document_path,
                        "atoms": [
                            {
                                "atom_id": "HDOC-LINK",
                                "path": document_path,
                                "heading": "相关文件",
                                "text": "[相关页](other.md)",
                            },
                            {
                                "atom_id": "HDOC-TEXT",
                                "path": document_path,
                                "heading": "消息语义",
                                "text": "上下文会影响消息构造。",
                            },
                        ],
                    }
                ],
            },
        )
        write_json(
            paths["egress"],
            {
                "schema_version": "official-client-control-object/v1",
                "object_kind": "egress_disposition_inventory",
                "payload": {
                    "schema_version": "official-client-egress-disposition-inventory/v1",
                    "entries": [{"egress_id": "egress-current"}],
                },
            },
        )
        write_json(
            paths["measured"],
            {
                "schema_version": "claude-code-fw-f-measured-rule-ledger/v2",
                "target_version": target_version,
                "target_binary_sha256": target_binary_sha256,
                "rule_count": len(MEASURED_SPEC_IDS),
                "entries": [measured_entry(spec_id) for spec_id in MEASURED_SPEC_IDS],
                "result": "passed",
            },
        )
        write_json(
            paths["prior"],
            {
                "schema_version": "claude-code-fw-f-rule-ledger-additions/v1",
                "target_version": target_version,
                "rule_count": len(PRIOR_PROPOSAL_IDS),
                "entries": [
                    {
                        "spec_id": spec_id,
                        "evidence_level": "blocked",
                        "source_candidate_ids": ["CAND-RULE"],
                        "source_ids": ["D-CAND"],
                    }
                    for spec_id in PRIOR_PROPOSAL_IDS
                ],
            },
        )
        write_json(
            paths["policy"],
            {
                "schema_version": "claude-code-fw-f-discovery-clearance-policy/v2",
                "target_version": target_version,
                "target_binary_sha256": target_binary_sha256,
                "strict_egress_ids": STRICT_EGRESS_IDS,
                "measured_rule_ids": MEASURED_SPEC_IDS,
                "expected_counts": {
                    "discovery_count": 5,
                    "candidate_count": 2,
                    "catalogued_context_count": 2,
                },
                "supporting_facts": [
                    {
                        "fact_id": "FACT-2_1_226-UNMEASURED-FEATURE-BOUNDARY",
                        "domain": "evidence_boundary",
                        "rationale": "未被当前运行触发的命题只保留为边界事实。",
                    }
                ],
                "managed_egress_facts": [],
                "candidate_resolutions": [
                    {
                        "candidate_id": "CAND-RULE",
                        "bindings": [
                            {
                                "binding_type": "rule_bound",
                                "spec_ids": ["SPEC-NEW-001"],
                            }
                        ],
                        "rationale": "候选已绑定真实 R/M 规则。",
                    },
                    {
                        "candidate_id": "CAND-MANAGED",
                        "bindings": [
                            {
                                "binding_type": "managed_egress_bound",
                                "managed_egress_ids": ["egress-current"],
                            }
                        ],
                        "rationale": "候选已经绑定受管出站。",
                    },
                ],
                "invalid_v1_rule_proposals": [
                    {"spec_id": spec_id, "retained_claim": "v1 机械拆分提案。"}
                    for spec_id in PRIOR_PROPOSAL_IDS
                ],
                "document_policies": [
                    {
                        "path": document_path,
                        "fact_id": "FACT-DOCUMENT-CONTEXT",
                        "domain": "message_body",
                        "egress_role": "profile_support",
                        "managed_egress_ids": [],
                        "rationale": "文档上下文支撑消息构造。",
                    }
                ],
            },
        )
        return paths

    def _build(self, paths: dict[str, Path]) -> dict[str, dict[str, object]]:
        return build_clearance(
            discovery_path=paths["discovery"],
            candidates_path=paths["candidates"],
            rule_assessments_path=paths["rules"],
            document_atoms_path=paths["atoms"],
            egress_inventory_path=paths["egress"],
            measured_rules_path=paths["measured"],
            prior_rule_additions_path=paths["prior"],
            policy_path=paths["policy"],
        )

    def _replace_measured_id(self, paths: dict[str, Path], replacement: str) -> None:
        """把一个无关测试规则替换为待验证的非法活动规则。"""

        measured = json.loads(paths["measured"].read_text(encoding="utf-8"))
        old = "SPEC-TEST-087"
        entry = next(value for value in measured["entries"] if value["spec_id"] == old)
        entry.update(measured_entry(replacement))
        measured["entries"] = sorted(measured["entries"], key=lambda value: value["spec_id"])
        write_json(paths["measured"], measured)

        policy = json.loads(paths["policy"].read_text(encoding="utf-8"))
        policy["measured_rule_ids"] = sorted(
            replacement if value == old else value for value in policy["measured_rule_ids"]
        )
        write_json(paths["policy"], policy)

    def test_all_discoveries_candidates_and_v1_proposals_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            outputs = self._build(paths)

        closure = outputs["closure.json"]
        self.assertEqual(closure["result"], "passed")
        self.assertEqual(closure["source_discovery_count"], 5)
        self.assertEqual(closure["resolved_record_count"], 5)
        self.assertEqual(closure["measured_rule_count"], len(MEASURED_SPEC_IDS))
        self.assertTrue(all(value == 0 for value in closure["gate_counts"].values()))
        self.assertEqual(outputs["candidate-resolution-ledger.json"]["resolved_count"], 2)
        self.assertEqual(outputs["withdrawn-rule-proposals.json"]["withdrawn_count"], 97)

        entries = {
            value["discovery_id"]: value
            for value in outputs["discovery-disposition-ledger.json"]["entries"]
        }
        self.assertEqual(entries["HDOC-LINK"]["bindings"][0]["binding_type"], "non_egress_proven")
        self.assertEqual(entries["HDOC-TEXT"]["bindings"][0]["binding_type"], "supporting_fact_bound")

    def test_missing_document_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            policy = json.loads(paths["policy"].read_text(encoding="utf-8"))
            policy["document_policies"] = []
            write_json(paths["policy"], policy)
            with self.assertRaisesRegex(DiscoveryClearanceError, "document policy"):
                self._build(paths)

    def test_unknown_supporting_fact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            policy = json.loads(paths["policy"].read_text(encoding="utf-8"))
            policy["candidate_resolutions"][0]["bindings"] = [
                {"binding_type": "supporting_fact_bound", "fact_ids": ["FACT-UNKNOWN"]}
            ]
            write_json(paths["policy"], policy)
            with self.assertRaisesRegex(DiscoveryClearanceError, "unknown supporting fact|未知 supporting fact"):
                self._build(paths)

    def test_candidate_reverse_link_gap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            candidates = json.loads(paths["candidates"].read_text(encoding="utf-8"))
            candidates["candidates"][0]["source_ids"].append("D-NOT-IN-INVENTORY")
            write_json(paths["candidates"], candidates)
            with self.assertRaisesRegex(DiscoveryClearanceError, "候选|门禁失败"):
                self._build(paths)

    def test_duplicate_discovery_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            discovery = json.loads(paths["discovery"].read_text(encoding="utf-8"))
            discovery["items"].append(discovery["items"][0])
            discovery["item_count"] = 6
            write_json(paths["discovery"], discovery)
            with self.assertRaisesRegex(DiscoveryClearanceError, "重复身份"):
                self._build(paths)

    def test_rule_without_r_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            measured = json.loads(paths["measured"].read_text(encoding="utf-8"))
            measured["entries"][0]["evidence_channels"] = ["M"]
            write_json(paths["measured"], measured)
            with self.assertRaisesRegex(DiscoveryClearanceError, "缺少 R/M"):
                self._build(paths)

    def test_non_request_or_unmeasured_wire_rules_are_rejected(self) -> None:
        forbidden_ids = [
            "SPEC-HDR-011",
            "SPEC-HDR-034",
            "SPEC-RESP-001",
            "SPEC-STATE-002",
            "SPEC-TLS-001",
            "SPEC-TLS-002",
        ]
        for forbidden_id in forbidden_ids:
            with self.subTest(spec_id=forbidden_id), tempfile.TemporaryDirectory() as temporary:
                paths = self._fixture(Path(temporary))
                self._replace_measured_id(paths, forbidden_id)
                with self.assertRaisesRegex(DiscoveryClearanceError, "禁用|门禁失败"):
                    self._build(paths)

    def test_v1_proposal_set_must_be_fully_withdrawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._fixture(Path(temporary))
            prior = json.loads(paths["prior"].read_text(encoding="utf-8"))
            prior["entries"].pop()
            prior["rule_count"] = len(prior["entries"])
            write_json(paths["prior"], prior)
            with self.assertRaisesRegex(DiscoveryClearanceError, "集合不一致|数量不是 97"):
                self._build(paths)


if __name__ == "__main__":
    unittest.main()
