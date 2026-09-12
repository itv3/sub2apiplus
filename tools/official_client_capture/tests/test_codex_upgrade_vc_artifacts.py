"""Codex VC-0～VC-6 小型控制制品的闭集与防篡改测试。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts


class CodexUpgradeVCArtifactsTests(unittest.TestCase):
    def _requirements(
        self,
        affected: list[str] | None = None,
    ) -> dict[str, object]:
        return artifacts.build_gate_requirements(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            joint_manifest_sha256="1" * 64,
            affected_rule_ids=affected or ["SPEC-HDR-005"],
            inherited_rule_ids=["SPEC-BODY-001"],
            migration_manifest={"path": "rules/rule-migration.json", "sha256": "2" * 64},
        )

    @staticmethod
    def _mapping(requirements: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": artifacts.GATE_MAPPING_SCHEMA,
            "requirements_sha256": requirements["requirements_sha256"],
            "gates": [
                {
                    "gate_id": row["gate_id"],
                    "test_id": f"gate-test-{index:03d}",
                    "working_directory": "backend" if index % 2 else ".",
                    "command": ["python3", "-m", f"gate_test_{index:03d}"],
                    "requirement_sha256": artifacts.digest(row),
                }
                for index, row in enumerate(requirements["requirements"], 1)
            ],
        }

    def _gate_plan(self) -> tuple[dict[str, object], dict[str, object]]:
        requirements = self._requirements()
        return requirements, artifacts.build_gate_plan(
            requirements,
            self._mapping(requirements),
        )

    def _candidate_build_receipt(self) -> dict[str, object]:
        requirements, gate_plan = self._gate_plan()
        return artifacts.build_candidate_build_receipt(
            campaign_id="codex-0_154_0-campaign",
            campaign_manifest_sha256="3" * 64,
            candidate_id="candidate-a",
            candidate_purpose="validation_only",
            target_version="0.154.0",
            deployed_version="0.154.0",
            target_architecture="linux/arm64",
            source={
                "root": "/srv/candidate-source",
                "tree_sha256": "4" * 64,
                "git_commit": "5" * 40,
            },
            binary={"path": "/srv/build/codex", "sha256": "6" * 64, "bytes": 42},
            image={
                "reference": f"registry/sub2api@sha256:{'7' * 64}",
                "manifest_digest": f"sha256:{'7' * 64}",
                "image_id": f"sha256:{'8' * 64}",
            },
            build_id="build-a",
            build_parameters={"offline": True, "target": "linux/arm64"},
            profile={
                "profile_id": "codex-0.154.0",
                "profile_digest": "9" * 64,
                "derivation_receipt_sha256": "a" * 64,
            },
            catalog_stage={
                "path": "/srv/candidate-source/catalog-stage-receipt.json",
                "sha256": "b" * 64,
                "catalog_tree_sha256": "c" * 64,
            },
            source_transition={
                "path": "/srv/control/source-transition.json",
                "sha256": "d" * 64,
            },
            gate_requirements={
                "path": "controls/post-promotion-gate-requirements.json",
                "sha256": "e" * 64,
                "requirements_sha256": requirements["requirements_sha256"],
            },
            gate_plan={
                "path": "/srv/candidate-source/post-promotion-gate-plan.json",
                "sha256": "f" * 64,
                "plan_sha256": gate_plan["plan_sha256"],
                "requirements_sha256": gate_plan["requirements_sha256"],
            },
            implementation_tests={
                "evidence_root": "/srv/control/implementation-tests",
                "receipt": {
                    "path": "receipt.json",
                    "sha256": "0" * 64,
                    "bytes": 10,
                },
                "receipt_digest": "1" * 64,
            },
            built_at_utc="2026-09-12T08:00:00Z",
        )

    def test_discovery_inventory_is_complete_and_not_truncated(self) -> None:
        inventory = artifacts.build_discovery_inventory(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            source_diff={
                "added_count": 2,
                "removed_count": 0,
                "added": [
                    {"fingerprint": "1" * 64, "surface": "header"},
                    {"fingerprint": "2" * 64, "surface": "model"},
                ],
                "removed": [],
            },
            official_diff={
                "added_count": 1,
                "removed_count": 1,
                "added": [{"fingerprint": "3" * 64, "surface": "request"}],
                "removed": [{"fingerprint": "4" * 64, "surface": "legacy"}],
            },
            source_diff_binding={"path": "source-diff.json", "sha256": "5" * 64},
            official_diff_binding={"path": "official-diff.json", "sha256": "6" * 64},
            evidence_manifest_binding={"path": "evidence.json", "sha256": "7" * 64},
        )
        self.assertFalse(inventory["truncated"])
        self.assertEqual(inventory["item_count"], 4)
        self.assertEqual(inventory["expected_item_count"], 4)
        self.assertEqual(artifacts.validate_discovery_inventory(inventory), inventory)

    def test_discovery_inventory_rejects_item_or_count_tampering(self) -> None:
        inventory = artifacts.build_discovery_inventory(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            source_diff={
                "added_count": 1,
                "removed_count": 0,
                "added": [{"fingerprint": "1" * 64, "surface": "header"}],
                "removed": [],
            },
            official_diff={"added_count": 0, "removed_count": 0, "added": [], "removed": []},
            source_diff_binding={"path": "source.json", "sha256": "2" * 64},
            official_diff_binding={"path": "official.json", "sha256": "3" * 64},
            evidence_manifest_binding={"path": "evidence.json", "sha256": "4" * 64},
        )
        tampered = copy.deepcopy(inventory)
        tampered["items"][0]["fact"]["fingerprint"] = "5" * 64
        unsigned = dict(tampered)
        unsigned.pop("inventory_sha256")
        tampered["inventory_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "事实摘要"):
            artifacts.validate_discovery_inventory(tampered)

        wrong_count = copy.deepcopy(inventory)
        wrong_count["item_count"] = 0
        unsigned = dict(wrong_count)
        unsigned.pop("inventory_sha256")
        wrong_count["inventory_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "计数"):
            artifacts.validate_discovery_inventory(wrong_count)

    def test_affected_rule_change_changes_gate_requirements_digest(self) -> None:
        first = self._requirements(["SPEC-HDR-005"])
        second = self._requirements(["SPEC-HDR-005", "SPEC-MODEL-001"])
        self.assertNotEqual(first["requirements_sha256"], second["requirements_sha256"])

    def test_mapping_must_exactly_cover_requirements(self) -> None:
        requirements = self._requirements()
        mapping = self._mapping(requirements)
        mapping["gates"].pop()
        with self.assertRaisesRegex(artifacts.VCArtifactError, "精确覆盖"):
            artifacts.build_gate_plan(requirements, mapping)

    def test_mapping_rejects_wrong_requirement_digest(self) -> None:
        requirements = self._requirements()
        mapping = self._mapping(requirements)
        mapping["gates"][0]["requirement_sha256"] = "0" * 64
        with self.assertRaisesRegex(artifacts.VCArtifactError, "需求摘要"):
            artifacts.build_gate_plan(requirements, mapping)

    def test_gate_plan_requires_unique_test_and_literal_command(self) -> None:
        requirements, plan = self._gate_plan()
        self.assertEqual(plan["gate_count"], len(requirements["requirements"]))
        tampered = copy.deepcopy(plan)
        tampered["gates"][1]["test_id"] = tampered["gates"][0]["test_id"]
        unsigned = dict(tampered)
        unsigned.pop("plan_sha256")
        tampered["plan_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "重复"):
            artifacts.validate_gate_plan(tampered, requirements)

    def test_candidate_build_receipt_is_self_bound(self) -> None:
        candidate = self._candidate_build_receipt()
        self.assertEqual(artifacts.validate_candidate_build_receipt(candidate), candidate)
        tampered = copy.deepcopy(candidate)
        tampered["build"]["parameters"]["offline"] = False
        with self.assertRaisesRegex(artifacts.VCArtifactError, "parameters 摘要"):
            artifacts.validate_candidate_build_receipt(tampered)

    def test_candidate_delivery_has_distinct_purpose_endpoints(self) -> None:
        build_receipt = self._candidate_build_receipt()
        common = {
            "campaign_id": "codex-0_154_0-campaign",
            "campaign_manifest_sha256": "1" * 64,
            "candidate_id": "candidate-a",
            "attempt_id": "attempt-a",
            "target_version": "0.154.0",
            "candidate_identity_sha256": "2" * 64,
            "issued_at_utc": "2026-09-12T09:00:00Z",
        }
        build_binding = {
            "path": "candidate/build.json",
            "sha256": "3" * 64,
            "bytes": 10,
        }
        acceptance_binding = {
            "path": "candidate/acceptance.json",
            "sha256": "4" * 64,
            "bytes": 10,
        }
        validation = artifacts.build_candidate_delivery_receipt(
            **common,
            campaign_purpose="validation_only",
            build_receipt=build_binding,
            acceptance_fact=acceptance_binding,
            canonical_checkpoint=None,
            production_state="accepted_not_activated",
        )
        self.assertEqual(validation["release_state"], "ready_for_operator_release")

        production = artifacts.build_candidate_delivery_receipt(
            **common,
            campaign_purpose="production_replacement",
            build_receipt=build_binding,
            acceptance_fact=acceptance_binding,
            canonical_checkpoint={"path": "canonical/latest.json", "sha256": "5" * 64, "bytes": 10},
            production_state="restored_active",
        )
        self.assertEqual(production["release_state"], "production_active_restored")
        self.assertEqual(build_receipt["candidate_purpose"], "validation_only")

    def test_new_schema_files_match_runtime_versions(self) -> None:
        root = Path(artifacts.__file__).resolve().parent
        expected = {
            "codex_upgrade_campaign_plan.schema.json": artifacts.CAMPAIGN_PLAN_SCHEMA,
            "codex_upgrade_vc_checkpoint.schema.json": artifacts.VC_CHECKPOINT_SCHEMA,
            "codex_upgrade_vc_batch.schema.json": artifacts.VC_BATCH_SCHEMA,
            "codex_upgrade_vc_action_plan.schema.json": artifacts.VC_ACTION_PLAN_SCHEMA,
            "codex_upgrade_gate_requirements.schema.json": artifacts.GATE_REQUIREMENTS_SCHEMA,
            "codex_upgrade_gate_mapping.schema.json": artifacts.GATE_MAPPING_SCHEMA,
            "codex_upgrade_gate_plan.schema.json": artifacts.GATE_PLAN_SCHEMA,
            "codex_upgrade_candidate_build_receipt.schema.json": artifacts.CANDIDATE_BUILD_SCHEMA,
            "codex_upgrade_candidate_delivery_receipt.schema.json": artifacts.CANDIDATE_DELIVERY_SCHEMA,
        }
        for name, schema_version in expected.items():
            with self.subTest(name=name):
                schema = json.loads((root / name).read_text(encoding="utf-8"))
                self.assertEqual(schema["properties"]["schema_version"]["const"], schema_version)


if __name__ == "__main__":
    unittest.main()
