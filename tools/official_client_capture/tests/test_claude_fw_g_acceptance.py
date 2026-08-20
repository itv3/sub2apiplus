from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_fw_g_acceptance as acceptance
from tools.official_client_control.contracts import validate_object_document


class ClaudeFWGAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persona = {
            "provider": "anthropic",
            "official_product": "claude-code",
            "auth_family": "oauth",
            "upstream_route_family": "anthropic-api",
        }
        self.evidence_ref = {"object_kind": "operational_evidence", "sha256": "a" * 64}

    def test_candidate_git_source_test_and_dependency_trees_are_frozen(self) -> None:
        self.assertEqual(
            acceptance.candidate_git_material_digests(
                acceptance.REPOSITORY_ROOT,
                acceptance.CANDIDATE_COMMIT,
            ),
            {
                "source": acceptance.CANDIDATE_SOURCE_TREE,
                "test": acceptance.CANDIDATE_TEST_TREE,
                "dependency": acceptance.CANDIDATE_DEPENDENCY_LOCK,
            },
        )

    def test_ingress_inventory_splits_official_and_third_party_strict_branches(self) -> None:
        observation = acceptance.build_ingress_observation(self.persona, self.evidence_ref)
        validate_object_document(
            {
                "schema_version": "official-client-control-object/v1",
                "object_kind": "ingress_observation",
                "payload": observation,
            }
        )
        observation_ref = {"object_kind": "ingress_observation", "sha256": "b" * 64}
        inventory = acceptance.build_ingress_inventory(
            self.persona,
            observation_ref,
            self.evidence_ref,
        )
        validate_object_document(
            {
                "schema_version": "official-client-control-object/v1",
                "object_kind": "production_ingress_inventory",
                "payload": inventory,
            }
        )
        entries = {item["logical_ingress_id"]: item for item in inventory["entries"]}
        strict_ids = {
            "official-count-tokens-oauth",
            "official-messages-oauth",
            "third-party-count-tokens-oauth",
            "third-party-messages-oauth",
        }
        self.assertTrue(strict_ids.issubset(entries))
        self.assertEqual(
            {entries[item]["protocol_class"] for item in strict_ids},
            {"anthropic-messages"},
        )
        self.assertEqual(entries["official-messages-oauth"]["ingress_kind"], "official")
        self.assertEqual(entries["third-party-messages-oauth"]["ingress_kind"], "third_party")
        self.assertTrue(all(entries[item]["current_disposition"] == "retained_legacy" for item in strict_ids))

    def test_support_capabilities_cover_exactly_four_strict_logical_ingresses(self) -> None:
        capabilities = acceptance.build_capabilities()
        self.assertEqual(
            {item["logical_ingress_id"] for item in capabilities},
            {
                "official-count-tokens-oauth",
                "official-messages-oauth",
                "third-party-count-tokens-oauth",
                "third-party-messages-oauth",
            },
        )
        self.assertEqual(
            [acceptance.capability_key(item) for item in capabilities],
            sorted(acceptance.capability_key(item) for item in capabilities),
        )

    def test_verified_evidence_keeps_old_proof_and_adds_fw_g_proof(self) -> None:
        old = {
            "schema_version": "official-client-evidence-package/v2",
            "producer_tool_sha256": "1" * 64,
            "completeness_ref": self.evidence_ref,
            "rules": [
                {
                    "spec_id": "SPEC-BODY-001",
                    "evidence_level": "observed",
                    "evidence_refs": [{"path": "old.json", "sha256": "2" * 64, "bytes": 1}],
                },
                {
                    "spec_id": "SPEC-TLS-001",
                    "evidence_level": "observed",
                    "evidence_refs": [{"path": "tls-old.json", "sha256": "3" * 64, "bytes": 1}],
                },
            ],
        }
        common = [
            {"path": "candidate.json", "sha256": "4" * 64, "bytes": 1},
            {"path": "official.json", "sha256": "5" * 64, "bytes": 1},
        ]
        tls = {"path": "tls-capture.json", "sha256": "6" * 64, "bytes": 1}
        upgraded = acceptance.build_verified_evidence(
            old,
            "7" * 64,
            self.evidence_ref,
            common,
            tls,
        )
        by_spec = {item["spec_id"]: item for item in upgraded["rules"]}
        self.assertTrue(all(item["evidence_level"] == "verified" for item in by_spec.values()))
        self.assertEqual(
            [item["path"] for item in by_spec["SPEC-BODY-001"]["evidence_refs"]],
            ["candidate.json", "official.json", "old.json"],
        )
        self.assertIn("tls-capture.json", [item["path"] for item in by_spec["SPEC-TLS-001"]["evidence_refs"]])

    def test_scenario_plan_replaces_atomic_assertions_with_required_rule_pairs(self) -> None:
        plan = acceptance.build_scenario_plan(
            {
                "scenarios": [
                    {
                        "id": "domain-tls",
                        "spec_ids": ["SPEC-PROTO-001", "SPEC-TLS-001"],
                        "ingress_protocol_classes": ["legacy"],
                        "conditions": ["approval=validation-only"],
                        "assertion_ids": ["PAIR-SPEC-TLS-002"],
                    }
                ]
            },
            self.persona,
        )
        scenario = plan["scenarios"][0]
        self.assertEqual(
            scenario["assertion_ids"],
            ["PAIR-SPEC-PROTO-001", "PAIR-SPEC-TLS-001"],
        )
        self.assertEqual(scenario["ingress_protocol_classes"], ["anthropic-messages"])
        self.assertIn("approval=production-replacement", scenario["conditions"])
        self.assertNotIn("approval=validation-only", scenario["conditions"])

    def test_secret_scan_rejects_oauth_callback_material(self) -> None:
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.scan_secret_bytes(b"A" * 24 + b"#" + b"B" * 24, "synthetic")

    def test_frozen_external_view_does_not_hardlink_workspace_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            source = repository / "source.txt"
            source.write_bytes(b"frozen-source")
            store = repository / "store"
            store.mkdir()
            binding = {
                "path": "source.txt",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "bytes": source.stat().st_size,
            }
            (store / "bindings.json").write_text(
                json.dumps({"binding": binding}),
                encoding="utf-8",
            )
            view = repository / "view"
            summary = acceptance.build_frozen_external_view(
                repository,
                store,
                view,
                repository / "stage",
                repository / "final",
            )
            frozen = view / "source.txt"
            self.assertEqual(summary["current_sources"], 1)
            self.assertNotEqual(source.stat().st_ino, frozen.stat().st_ino)
            source.write_bytes(b"changed-source")
            self.assertEqual(frozen.read_bytes(), b"frozen-source")


if __name__ == "__main__":
    unittest.main()
