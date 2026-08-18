"""FW-E 只能封存当前事实并停在 EvidenceFact。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.fw_e import seal_fw_e_plan
from tools.official_client_control.store import ControlStore


class FWESealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.external = self.root / "workspace"
        self.external.mkdir(mode=0o700)
        self.store = ControlStore.initialize(
            self.root / "store", "2026-08-18T00:00:00Z"
        )
        for relative, value in (
            ("contract.json", {"contract": "fw-d"}),
            ("fw-c.json", {"result": "passed"}),
            ("runtime.json", {"active": "codex"}),
            ("official.tgz", {"artifact": "official"}),
            ("inventory.json", {"scope": "source-to-sink"}),
            ("rules/SPEC-001.json", {"result": "observed"}),
        ):
            path = self.external / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical_json_bytes(value))
        target_inventory = {
            "schema_version": "claude-code-target-sink-inventory/v1",
            "target_version": "2.1.226",
            "platform": "linux/amd64",
            "bundle_sha256": "a" * 64,
            "completeness": {
                "truncated": False,
                "ast_parse_diagnostic_count": 0,
                "ambiguous_lexical_match_count": 0,
                "duplicate_sink_id_count": 0,
            },
            "sink_total": 1,
            "sinks": [{"sink_id": "TN-SINK-001"}],
        }
        (self.external / "target-inventory.json").write_bytes(
            canonical_json_bytes(target_inventory)
        )
        discovery = {
            "schema_version": "claude-code-fw-e-discovery-inventory/v1",
            "target_version": "2.1.226",
            "item_count": 1,
            "counts_by_source_kind": {"hitcc_document_atom_2_1_197": 1},
            "counts_by_disposition": {"catalogued_context": 1},
            "items": [
                {
                    "discovery_id": "HDOC-CONTEXT-001",
                    "semantic_candidate_ids": [],
                }
            ],
            "rule_generation": "forbidden",
        }
        (self.external / "discovery.json").write_bytes(
            canonical_json_bytes(discovery)
        )
        matrix = {
            "schema_version": "claude-code-fw-e-cross-source-matrix/v2",
            "target_version": "2.1.226",
            "target_sinks": [{"sink_id": "TN-SINK-001"}],
            "runtime_observations": [
                {"observation_id": "RUN-NET-001", "disposition": "mapped_sink"}
            ],
            "semantic_candidates": [],
            "discovery_inventory": {
                "path": "discovery.json",
                "sha256": sha256_file(self.external / "discovery.json"),
                "item_count": 1,
                "counts_by_source_kind": {
                    "hitcc_document_atom_2_1_197": 1
                },
                "counts_by_disposition": {"catalogued_context": 1},
            },
            "target_rules": [{"id": "SPEC-001"}],
        }
        (self.external / "matrix.json").write_bytes(canonical_json_bytes(matrix))
        closure = {
            "schema_version": "claude-code-fw-e-completeness/v2",
            "target_version": "2.1.226",
            "matrix_sha256": canonical_sha256(matrix),
            "target_inventory_sha256": sha256_file(
                self.external / "target-inventory.json"
            ),
            "target_sink_total": 1,
            "target_sink_disposition_counts": {"mapped_strict": 1},
            "runtime_observation_disposition_counts": {"mapped_sink": 1},
            "target_rule_count": 1,
            "semantic_candidate_count": 0,
            "discovery_item_count": 1,
            "unresolved": {
                "source_candidate_ids": [],
                "hitcc_clue_ids": [],
                "hitcc_document_paths": [],
                "target_sink_ids": [],
                "runtime_observation_ids": [],
                "runtime_capture_scope": [],
            },
            "unresolved_total": 0,
            "result": "passed",
        }
        (self.external / "closure.json").write_bytes(canonical_json_bytes(closure))
        capture = {
            "schema_version": "claude-code-fw-e-capture-index/v1",
            "target_version": "2.1.226",
            "result": "passed",
            "network_inventory": {
                "result": "passed",
                "host_prefilter_disabled": True,
            },
            "control": {
                "privacy_controls": {
                    "required_values": {
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "DISABLE_TELEMETRY": "1",
                    },
                    "case_count": 1,
                    "environment_manifest_sha256s": ["c" * 64],
                    "result": "passed",
                }
            },
            "target": {
                "capture_host_scopes": ["all"],
                "privacy_controls": {
                    "required_values": {
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "DISABLE_TELEMETRY": "1",
                    },
                    "case_count": 1,
                    "environment_manifest_sha256s": ["d" * 64],
                    "result": "passed",
                },
                "network_observations": [
                    {"observation_id": "RUN-NET-001"}
                ],
            },
        }
        (self.external / "capture.json").write_bytes(canonical_json_bytes(capture))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _managed_policy() -> dict[str, str]:
        return {
            "authentication": "claude-oauth",
            "endpoint": "https://api.anthropic.com/v1/messages",
            "client": "legacy-http-upstream",
            "timeout_policy": "legacy-bounded",
            "retry_policy": "legacy-bounded",
            "secret_policy": "redacted",
            "audit_policy": "metadata-only",
        }

    def plan(self) -> dict:
        persona = {
            "provider": "anthropic",
            "official_product": "claude-code",
            "auth_family": "oauth",
            "upstream_route_family": "anthropic-api",
        }
        return {
            "schema_version": "official-client-fw-e-seal-plan/v3",
            "campaign_id": "claude-fw-e-test",
            "persona": persona,
            "target_version": "2.1.226",
            "platforms": ["linux/amd64"],
            "entrypoints": ["sdk-cli"],
            "default_conditions": ["privacy=essential-traffic"],
            "traffic_observation_policy": {
                "traffic_presence_comparison": "disabled",
                "strict_wire_traffic_classes": ["essential"],
                "record_only_traffic_classes": ["nonessential", "telemetry"],
                "absence_of_record_only_traffic": "conformant_not_a_difference",
            },
            "created_at_utc": "2026-08-18T00:00:01Z",
            "discovered_at_utc": "2026-08-18T00:00:02Z",
            "discovery_source": "official-npm-stable",
            "source_commit": "1" * 40,
            "contract_source_paths": ["contract.json"],
            "fw_c_receipt_paths": ["fw-c.json"],
            "runtime_catalog_paths": ["runtime.json"],
            "official_artifact_paths": ["official.tgz"],
            "target_sink_inventory_paths": ["target-inventory.json"],
            "cross_source_matrix_path": "matrix.json",
            "completeness_closure_path": "closure.json",
            "capture_index_path": "capture.json",
            "rules": [
                {
                    "spec_id": "SPEC-001",
                    "evidence_level": "observed",
                    "rule_lifecycle": "candidate",
                    "compatibility_class": "request_egress",
                    "migration_decision": "change",
                    "decision_basis": "inheritance_not_proven",
                    "semantic_equivalence_proven": False,
                    "evidence_paths": ["rules/SPEC-001.json"],
                    "applicability": ["platform=linux/amd64"],
                }
            ],
            "inventory_observed_at_utc": "2026-08-18T00:00:03Z",
            "inventory_evidence_paths": ["inventory.json"],
            "ingress_aliases": [
                {
                    "alias_id": "alias-messages",
                    "logical_ingress_id": "messages-oauth",
                    "physical_route": "POST /v1/messages -> claude-oauth",
                    "caller_ids": ["gateway-handler"],
                }
            ],
            "ingress_entries": [
                {
                    "logical_ingress_id": "messages-oauth",
                    "physical_alias_ids": ["alias-messages"],
                    "caller_ids": ["gateway-handler"],
                    "adapter_id": "anthropic-messages-legacy",
                    "route_id": "claude-oauth-legacy",
                    "ingress_kind": "official",
                    "protocol_class": "anthropic-messages",
                    "current_disposition": "retained_legacy",
                }
            ],
            "egress_observed": [
                {
                    "egress_id": "messages-inference",
                    "route_id": "claude-oauth-legacy",
                    "sink_id": "anthropic-messages",
                    "oauth_related": True,
                    "kind": "inference",
                }
            ],
            "egress_entries": [
                {
                    "egress_id": "messages-inference",
                    "current_disposition": "non_persona_managed",
                    "current_guard_state": "legacy_observe",
                    "spec_ids": [],
                    "managed_policy": self._managed_policy(),
                }
            ],
            "target_proposals": [
                {
                    "id": "messages-inference",
                    "kind": "egress",
                    "target_disposition": "persona_strict",
                    "rationale": "只作为 FW-F 待批准目标。",
                }
            ],
        }

    def test_seals_current_facts_without_approval_or_profile(self) -> None:
        result = seal_fw_e_plan(self.store, self.external, self.plan())
        self.assertEqual(result["checkpoint"], "evidence_recorded")
        self.assertEqual(result["approval_state"], "awaiting_explicit_evidence_approval")
        self.assertEqual(result["rule_count"], 1)
        self.assertEqual(result["semantic_candidate_count"], 0)
        self.assertEqual(result["discovery_item_count"], 1)
        self.assertEqual(result["target_sink_count"], 1)
        self.assertEqual(result["runtime_observation_count"], 1)
        self.assertEqual(
            result["traffic_observation_policy_ref"]["object_kind"],
            "operational_evidence",
        )
        facts = self.store.list_facts("claude-fw-e-test")
        self.assertEqual(
            {fact["fact_kind"] for fact in facts},
            {"discovery_recorded", "evidence_recorded"},
        )
        object_kinds = {path.parent.name for path in (self.store.root / "objects").rglob("*.json")}
        self.assertNotIn("profile_schema", object_kinds)
        self.assertNotIn("snapshot", object_kinds)
        self.assertNotIn("release_artifact", object_kinds)

    def test_blocks_inherit_without_semantic_equivalence(self) -> None:
        plan = self.plan()
        plan["rules"][0]["migration_decision"] = "inherit"
        with self.assertRaisesRegex(ControlError, "禁止使用 inherit"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_legacy_observe_from_claiming_strict(self) -> None:
        plan = self.plan()
        entry = plan["egress_entries"][0]
        entry["current_disposition"] = "persona_strict"
        entry["spec_ids"] = ["SPEC-001"]
        entry["managed_policy"] = None
        with self.assertRaisesRegex(ControlError, "不得冒充 persona_strict"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_known_oauth_egress_left_out_of_scope(self) -> None:
        plan = self.plan()
        plan["egress_entries"][0]["current_guard_state"] = "out_of_scope_passthrough"
        with self.assertRaisesRegex(ControlError, "不得停留在 out_of_scope_passthrough"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_source_absent_requires_denied(self) -> None:
        plan = self.plan()
        plan["egress_entries"][0]["current_guard_state"] = "source_absent"
        with self.assertRaisesRegex(ControlError, "source_absent 只能对应 denied"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_traffic_presence_as_consistency_dimension(self) -> None:
        plan = self.plan()
        plan["traffic_observation_policy"]["traffic_presence_comparison"] = "enabled"
        with self.assertRaisesRegex(ControlError, "禁止把流量类别是否出现"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_unclassified_target_sink_closure(self) -> None:
        plan = self.plan()
        closure_path = self.external / "closure.json"
        closure = json.loads(closure_path.read_text())
        closure["target_sink_disposition_counts"] = {"unclassified": 1}
        closure_path.write_bytes(canonical_json_bytes(closure))
        with self.assertRaisesRegex(ControlError, "unclassified target sink"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_unbounded_rule_with_blocked_evidence(self) -> None:
        plan = self.plan()
        plan["rules"][0]["evidence_level"] = "blocked"
        with self.assertRaisesRegex(ControlError, "只有在 validation-only"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_semantic_candidate_mixed_into_rule_ledger(self) -> None:
        plan = self.plan()
        matrix_path = self.external / "matrix.json"
        matrix = json.loads(matrix_path.read_text())
        matrix["target_rules"] = [{"id": "CAND-NOT-A-RULE"}]
        matrix_path.write_bytes(canonical_json_bytes(matrix))
        closure_path = self.external / "closure.json"
        closure = json.loads(closure_path.read_text())
        closure["matrix_sha256"] = canonical_sha256(matrix)
        closure_path.write_bytes(canonical_json_bytes(closure))
        with self.assertRaisesRegex(ControlError, "只能包含身份唯一的 SPEC"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_seals_bounded_validation_only_blocked_rule(self) -> None:
        plan = self.plan()
        rule = plan["rules"][0]
        rule.update(
            {
                "evidence_level": "blocked",
                "migration_decision": "add",
                "decision_basis": "new_target_rule",
                "applicability": [
                    "approval_scope=validation_only",
                    "platform=linux/amd64",
                    "production_eligibility=denied",
                    "validation_scope=historical-source",
                ],
            }
        )
        result = seal_fw_e_plan(self.store, self.external, plan)
        self.assertEqual(result["checkpoint"], "evidence_recorded")
        self.assertEqual(result["approval_state"], "awaiting_explicit_evidence_approval")

    def test_blocks_validation_only_rule_without_scope(self) -> None:
        plan = self.plan()
        rule = plan["rules"][0]
        rule.update(
            {
                "evidence_level": "blocked",
                "migration_decision": "add",
                "decision_basis": "new_target_rule",
                "applicability": [
                    "approval_scope=validation_only",
                    "platform=linux/amd64",
                    "production_eligibility=denied",
                ],
            }
        )
        with self.assertRaisesRegex(ControlError, "边界明确"):
            seal_fw_e_plan(self.store, self.external, plan)

    def test_blocks_capture_without_privacy_control_proof(self) -> None:
        plan = self.plan()
        capture_path = self.external / "capture.json"
        capture = json.loads(capture_path.read_text())
        capture["target"]["privacy_controls"]["required_values"][
            "DISABLE_TELEMETRY"
        ] = "0"
        capture_path.write_bytes(canonical_json_bytes(capture))
        with self.assertRaisesRegex(ControlError, "隐私开关的实际值"):
            seal_fw_e_plan(self.store, self.external, plan)


if __name__ == "__main__":
    unittest.main()
