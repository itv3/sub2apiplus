"""Codex VC-0～VC-6 小型控制制品的闭集与防篡改测试。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts


class CodexUpgradeVCArtifactsTests(unittest.TestCase):
    @staticmethod
    def _campaign_plan() -> dict[str, object]:
        return artifacts.build_campaign_plan(
            campaign_id="codex-0_154_0-campaign",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-14T00:00:00Z",
            original_deadline_at_utc="2026-09-14T12:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )

    def _interrupted_recovery_contract(self) -> dict[str, object]:
        plan = self._campaign_plan()
        return artifacts.build_interrupted_recovery_contract(
            campaign_plan=plan,
            batch_sequence=2,
            source_attempt={
                "attempt_id": "attempt-a",
                "reservation": {
                    "path": "official/attempts/attempt-a/reservation.json",
                    "sha256": "5" * 64,
                    "reservation_digest": "6" * 64,
                },
                "run_nonce": "7" * 64,
                "identity_sha256": "8" * 64,
                "checkpoint": {
                    "path": "official/attempts/attempt-a/checkpoints",
                    "record_count": 2,
                    "last_sequence": 2,
                    "last_sha256": "9" * 64,
                },
                "planned_job_ids": ["job-a", "job-b", "job-c"],
                "completed_job_ids": ["job-a"],
                "failed_job_ids": ["job-b"],
                "pending_job_ids": ["job-c"],
                "execute_job_ids": ["job-b", "job-c"],
                "reuse_job_ids": ["job-a"],
            },
            failed_supervisor={
                "run_dir": "/srv/control/run-a",
                "state_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "stop_receipt_sha256": "c" * 64,
                "owner_nonce": "d" * 64,
                "terminal_at_utc": "2026-09-14T01:00:00Z",
                "state": "failed",
                "reason": "KeyboardInterrupt",
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "e" * 64,
            },
            timing_ledger={
                "ledger_dir": "/srv/control/ledger",
                "ledger_plan_sha256": "f" * 64,
                "event_head_sequence": 6,
                "event_head_sha256": "0" * 64,
                "status": "active",
                "total_live_request_count": 26,
                "total_deadline_at_utc": plan["original_deadline_at_utc"],
            },
            deployment_receipt={
                "path": "/srv/control/deploy.json",
                "sha256": "1" * 64,
                "tool_files_sha256": "2" * 64,
            },
            tool_transition={
                "from_tool_files_sha256": "3" * 64,
                "to_tool_files_sha256": "2" * 64,
                "changed_files": [
                    {
                        "path": "run_official_relay_scenario.sh",
                        "from_sha256": "4" * 64,
                        "to_sha256": "5" * 64,
                        "classification": "failed_job_production",
                        "affected_job_ids": ["job-b", "job-c"],
                    }
                ],
                "allowed_production_paths": ["run_official_relay_scenario.sh"],
                "affected_job_ids": ["job-b", "job-c"],
            },
            compiled_at_utc="2026-09-14T01:01:00Z",
            must_start_by_utc="2026-09-14T01:03:00Z",
        )

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
            build_inventory={
                "path": "candidates/candidate-a/build-evidence/build-inventory.json",
                "sha256": "2" * 64,
                "bytes": 10,
                "receipt_digest": "3" * 64,
            },
            frontend_provenance={
                "path": "candidates/candidate-a/build-evidence/frontend-provenance.json",
                "sha256": "4" * 64,
                "bytes": 10,
                "receipt_digest": "5" * 64,
            },
            image_inspection={
                "path": "candidates/candidate-a/build-evidence/image-inspection.json",
                "sha256": "6" * 64,
                "bytes": 10,
                "receipt_digest": "7" * 64,
            },
            capability_probe={
                "path": "candidates/candidate-a/build-evidence/capability-probe.json",
                "sha256": "8" * 64,
                "bytes": 10,
                "receipt_digest": "9" * 64,
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

    def test_legacy_candidate_build_receipt_remains_replayable(self) -> None:
        """v7 已封存的 v1 收据没有四份 v2 机器收据，仍须只读重放。"""

        candidate = self._candidate_build_receipt()
        candidate["schema_version"] = artifacts.LEGACY_CANDIDATE_BUILD_SCHEMA
        for field in (
            "build_inventory",
            "frontend_provenance",
            "image_inspection",
            "capability_probe",
        ):
            candidate.pop(field)
        unsigned = dict(candidate)
        unsigned.pop("receipt_digest")
        candidate["receipt_digest"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "受管历史投影"):
            artifacts.validate_candidate_build_receipt(candidate)
        self.assertEqual(
            artifacts.validate_candidate_build_receipt(
                candidate,
                allow_legacy=True,
            ),
            candidate,
        )

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

    def test_interrupted_recovery_contract_closes_execute_reuse_and_zero_boundary(self) -> None:
        contract = self._interrupted_recovery_contract()
        self.assertEqual(
            artifacts.validate_interrupted_recovery_contract(
                contract,
                self._campaign_plan(),
            ),
            contract,
        )

        wrong_partition = copy.deepcopy(contract)
        wrong_partition["source_attempt"]["completed_job_ids"] = ["job-a", "job-b"]
        wrong_partition["contract_sha256"] = artifacts.digest(
            {key: value for key, value in wrong_partition.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "执行／复用闭集"):
            artifacts.validate_interrupted_recovery_contract(wrong_partition)

        wrong_boundary = copy.deepcopy(contract)
        wrong_boundary["zero_request_boundary"]["reservation_exists"] = True
        wrong_boundary["contract_sha256"] = artifacts.digest(
            {key: value for key, value in wrong_boundary.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "零预约"):
            artifacts.validate_interrupted_recovery_contract(wrong_boundary)

    def test_interrupted_recovery_rejects_production_change_on_completed_job(self) -> None:
        contract = self._interrupted_recovery_contract()
        tampered = copy.deepcopy(contract)
        changed = tampered["tool_transition"]["changed_files"][0]
        changed["affected_job_ids"] = ["job-a"]
        tampered["tool_transition"]["affected_job_ids"] = ["job-a"]
        tampered["contract_sha256"] = artifacts.digest(
            {key: value for key, value in tampered.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "越过 failed/pending"):
            artifacts.validate_interrupted_recovery_contract(tampered)

    def test_new_schema_files_match_runtime_versions(self) -> None:
        root = Path(artifacts.__file__).resolve().parent
        expected = {
            "codex_upgrade_campaign_plan.schema.json": artifacts.CAMPAIGN_PLAN_SCHEMA,
            "codex_upgrade_vc_checkpoint.schema.json": artifacts.VC_CHECKPOINT_SCHEMA,
            "codex_upgrade_vc_batch.schema.json": artifacts.VC_BATCH_SCHEMA,
            "codex_upgrade_vc_action_plan.schema.json": artifacts.VC_ACTION_PLAN_SCHEMA,
            "codex_upgrade_interrupted_recovery_contract.schema.json": artifacts.INTERRUPTED_RECOVERY_CONTRACT_SCHEMA,
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
