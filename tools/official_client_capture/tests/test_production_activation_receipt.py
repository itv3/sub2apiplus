from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import production_activation_receipt as receipt


class ProductionActivationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.target_image = f"sha256:{'1' * 64}"
        self.rollback_image = f"sha256:{'2' * 64}"
        self.profile_digest = "3" * 64
        self.candidate_package_digest = "7" * 64
        self.candidate_source_tree = "8" * 64
        self.candidate_image = f"sha256:{'9' * 64}"
        self.candidate_image_reference = f"registry/candidate@{self.candidate_image}"
        self.environment_patcher = mock.patch.object(
            gate_receipt.codex_upgrade_arm64_environment_receipt,
            "replay",
            side_effect=self._replay_environment,
        )
        self.environment_patcher.start()
        self.acceptance = self._write(
            "inputs/acceptance.json",
            {
                "status": "complete",
                "accepted": True,
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "candidate_id": "k83-dmit",
                "candidate_purpose": "production_replacement",
                "production_state": "accepted_not_activated",
                "target_version": "0.147.0",
                "profile_id": "codex-0.147.0",
                "profile_digest": self.profile_digest,
                "candidate_package_digest": self.candidate_package_digest,
                "candidate_identity": {
                    "source_tree_sha256": self.candidate_source_tree,
                    "image_id": self.candidate_image,
                    "image_reference": self.candidate_image_reference,
                    "build_id": "candidate-build",
                    "deployed_version": "candidate-version",
                    "candidate_purpose": "production_replacement",
                },
            },
        )
        self.facts = self._facts()
        self._write("facts.json", self.facts)

    def tearDown(self) -> None:
        self.environment_patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def _replay_environment(root: Path, relative: str) -> dict[str, object]:
        del root
        return {
            "status": "passed",
            "phase": "gate_before" if "before" in relative else "gate_after",
            "subject_id": "post-gate-attempt",
            "continuity_identity_sha256": "c" * 64,
        }

    def _write(self, relative: str, payload: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _identity(self, *, target: bool) -> dict[str, object]:
        return {
            "version": "0.147.0" if target else "0.145.0",
            "profile_id": "codex-0.147.0" if target else "codex-0.145.0",
            "profile_digest": self.profile_digest if target else "4" * 64,
            "source_tree_sha256": "5" * 64 if target else "6" * 64,
            "build_id": "k83-build" if target else "rollback-build",
            "deployed_version": "0.1.171-17" if target else "0.1.170-1",
            "image_id": self.target_image if target else self.rollback_image,
            "image_reference": (
                f"registry/sub2api@{self.target_image}"
                if target
                else f"registry/sub2api@{self.rollback_image}"
            ),
        }

    def _facts(self) -> dict[str, object]:
        target = self._identity(target=True)
        rollback = self._identity(target=False)
        acceptance_sha256 = self._digest(self.acceptance)
        promotion = self._write(
            "inputs/promotion.json",
            {
                "schema_version": "official-egress-catalog-promotion/v1",
                "campaign_id": "codex-0_147_0-campaign",
                "acceptance_sha256": acceptance_sha256,
                "target_version": target["version"],
                "target_profile_digest": target["profile_digest"],
                "rollback_version": rollback["version"],
                "rollback_profile_digest": rollback["profile_digest"],
                "production_selector_changed": True,
            },
        )
        requirements = vc_artifacts.build_gate_requirements(
            campaign_id="codex-0_147_0-campaign",
            target_version=str(target["version"]),
            joint_manifest_sha256="d" * 64,
            affected_rule_ids=["SPEC-HDR-005"],
            inherited_rule_ids=["SPEC-BODY-001"],
            migration_manifest={
                "path": "inputs/migration.json",
                "sha256": "e" * 64,
            },
        )
        mapping = {
            "schema_version": vc_artifacts.GATE_MAPPING_SCHEMA,
            "requirements_sha256": requirements["requirements_sha256"],
            "gates": [
                {
                    "gate_id": row["gate_id"],
                    "test_id": f"post-test-{index:03d}",
                    "working_directory": "backend" if index % 2 else ".",
                    "command": ["python3", "-m", f"post_gate_{index:03d}"],
                    "requirement_sha256": vc_artifacts.digest(row),
                }
                for index, row in enumerate(requirements["requirements"], 1)
            ],
        }
        gate_plan = vc_artifacts.build_gate_plan(requirements, mapping)
        gate_plan_path = self._write("inputs/gate-plan.json", gate_plan)
        gate_facts = {
            "schema_version": gate_receipt.FACTS_SCHEMA,
            "phase": gate_receipt.POST_PROMOTION_PHASE,
            "attempt": {
                "attempt_id": "post-gate-attempt",
                "root_cause_id": None,
                "previous_receipt": None,
            },
            "subject": {
                "campaign_id": "codex-0_147_0-campaign",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "candidate_id": "k83-dmit",
                "candidate_purpose": "production_replacement",
                "target_version": target["version"],
                "target_architecture": "linux/amd64",
                "profile_id": target["profile_id"],
                "profile_digest": target["profile_digest"],
                "candidate_package_digest": self.candidate_package_digest,
                "candidate_source_tree_sha256": self.candidate_source_tree,
                "candidate_image_id": self.candidate_image,
                "candidate_image_reference": self.candidate_image_reference,
                "production_tree_sha256": target["source_tree_sha256"],
                "acceptance_sha256": acceptance_sha256,
                "promotion_receipt_sha256": self._digest(promotion),
            },
            "inputs": [
                {
                    "role": "acceptance",
                    "path": self.acceptance.relative_to(self.root).as_posix(),
                    "sha256": acceptance_sha256,
                },
                {
                    "role": "promotion",
                    "path": promotion.relative_to(self.root).as_posix(),
                    "sha256": self._digest(promotion),
                },
            ],
            "gate_plan": {
                "path": gate_plan_path.relative_to(self.root).as_posix(),
                "sha256": self._digest(gate_plan_path),
            },
            "environment": {},
            "gates": [],
        }
        for role in ("before", "after"):
            environment = self._write(
                f"inputs/post-gate-{role}.json",
                {"role": role, "attempt_id": "post-gate-attempt"},
            )
            gate_facts["environment"][role] = {
                "path": environment.relative_to(self.root).as_posix(),
                "sha256": self._digest(environment),
            }
        for index, contract in enumerate(gate_plan["gates"]):
            gate_id = contract["gate_id"]
            evidence = self._write(
                f"post-gates/{gate_id}.json",
                {"gate_id": gate_id, "passed": True},
            )
            gate_facts["gates"].append(
                {
                    "gate_id": gate_id,
                    "test_id": contract["test_id"],
                    "command": list(contract["command"]),
                    "working_directory": contract["working_directory"],
                    "host": "runner-1",
                    "architecture": "linux/amd64",
                    "started_at_utc": f"2026-08-15T23:{index:02d}:00Z",
                    "completed_at_utc": f"2026-08-15T23:{index:02d}:30Z",
                    "exit_code": 0,
                    "status": "passed",
                    "passed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "stdout_sha256": "a" * 64,
                    "stderr_sha256": "b" * 64,
                    "evidence": [
                        {
                            "path": evidence.relative_to(self.root).as_posix(),
                            "sha256": self._digest(evidence),
                        }
                    ],
                }
            )
        self._write("inputs/post-gate-facts.json", gate_facts)
        gate_receipt.finalize(
            self.root,
            "inputs/post-gate-facts.json",
            "inputs/post-gate-receipt.json",
        )
        post_gate = self.root / "inputs/post-gate-receipt.json"
        stages = []
        for index, name in enumerate(receipt.STAGE_ORDER):
            evidence = self._write(f"stages/{name}.json", {"stage": name, "ok": True})
            identity = target if name in receipt.TARGET_STAGES else rollback
            stages.append(
                {
                    "name": name,
                    "started_at_utc": f"2026-08-16T00:0{index}:00Z",
                    "completed_at_utc": f"2026-08-16T00:0{index}:30Z",
                    "host": "Vircs",
                    "architecture": "linux/amd64",
                    "status": "pass",
                    "image_id": identity["image_id"],
                    "checks": {
                        "container_status": "running",
                        "health": "pass",
                        "active_version": identity["version"],
                        "profile_digest": identity["profile_digest"],
                        "fatal_log_count": 0,
                        "guard_failure_count": 0,
                    },
                    "evidence": [
                        {
                            "path": str(evidence.relative_to(self.root)),
                            "sha256": self._digest(evidence),
                        }
                    ],
                }
            )
        return {
            "schema_version": receipt.FACTS_SCHEMA,
            "campaign": {
                "id": "codex-0_147_0-campaign",
                "candidate_id": "k83-dmit",
                "acceptance_path": str(self.acceptance.relative_to(self.root)),
                "acceptance_sha256": self._digest(self.acceptance),
            },
            "promotion": {
                "path": promotion.relative_to(self.root).as_posix(),
                "sha256": self._digest(promotion),
            },
            "post_promotion_gate": {
                "path": post_gate.relative_to(self.root).as_posix(),
                "sha256": self._digest(post_gate),
            },
            "target": target,
            "rollback": rollback,
            "stages": stages,
            "final_state": {
                "candidate_id": "k83-dmit",
                "image_id": self.target_image,
                "active_version": "0.147.0",
                "profile_id": "codex-0.147.0",
                "profile_digest": self.profile_digest,
                "container_status": "running",
                "health": "pass",
            },
        }

    def test_finalize_and_replay(self) -> None:
        finalized = receipt.finalize(self.root, "facts.json", "receipt.json")
        replayed = receipt.replay(self.root, "receipt.json")
        self.assertEqual(finalized, replayed)
        self.assertEqual(finalized["campaign"]["candidate_id"], "k83-dmit")

    def test_canonical_acceptance_binds_explicit_candidate_identity(self) -> None:
        self._write(
            "inputs/acceptance.json",
            {
                "schema_version": "codex-upgrade-canonical-step/v1",
                "item_id": "acceptance",
                "status": "complete",
                "accepted": True,
                "production_state": "accepted_not_activated",
                "candidate_id": "k83-dmit",
                "attempt_id": "attempt-151",
            },
        )
        acceptance_sha256 = self._digest(self.acceptance)
        promotion_path = self.root / "inputs/promotion.json"
        promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
        promotion["acceptance_sha256"] = acceptance_sha256
        self._write("inputs/promotion.json", promotion)
        promotion_sha256 = self._digest(promotion_path)

        gate_facts_path = self.root / "inputs/post-gate-facts.json"
        gate_facts = json.loads(gate_facts_path.read_text(encoding="utf-8"))
        gate_facts["subject"]["acceptance_sha256"] = acceptance_sha256
        gate_facts["subject"]["promotion_receipt_sha256"] = promotion_sha256
        gate_facts["inputs"][0]["sha256"] = acceptance_sha256
        gate_facts["inputs"][1]["sha256"] = promotion_sha256
        self._write("inputs/post-gate-facts.json", gate_facts)
        gate_path = self.root / "inputs/post-gate-receipt.json"
        gate_path.unlink()
        gate_receipt.finalize(
            self.root,
            "inputs/post-gate-facts.json",
            "inputs/post-gate-receipt.json",
        )

        canonical_facts = dict(self.facts)
        canonical_facts["campaign"] = {
            **self.facts["campaign"],
            "acceptance_sha256": acceptance_sha256,
        }
        canonical_facts["promotion"] = {
            "path": "inputs/promotion.json",
            "sha256": promotion_sha256,
        }
        canonical_facts["post_promotion_gate"] = {
            "path": "inputs/post-gate-receipt.json",
            "sha256": self._digest(gate_path),
        }
        canonical_facts["candidate"] = {
            "package_digest": self.candidate_package_digest,
            "source_tree_sha256": self.candidate_source_tree,
            "build_id": "candidate-build",
            "deployed_version": "candidate-version",
            "image_id": self.candidate_image,
            "image_reference": self.candidate_image_reference,
        }
        self._write("canonical-facts.json", canonical_facts)
        built = receipt.build_receipt(self.root, "canonical-facts.json")
        self.assertEqual(built["campaign"]["acceptance"]["sha256"], acceptance_sha256)
        self.assertEqual(built["candidate"], canonical_facts["candidate"])

    def test_schema_file_is_valid_json_and_matches_version(self) -> None:
        schema_path = Path(receipt.__file__).with_name(
            "production_activation_receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )

    def test_output_is_write_once(self) -> None:
        receipt.finalize(self.root, "facts.json", "receipt.json")
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "禁止覆盖"):
            receipt.finalize(self.root, "facts.json", "receipt.json")

    def test_rejects_wrong_final_image(self) -> None:
        self.facts["final_state"]["image_id"] = self.rollback_image
        self._write("wrong-facts.json", self.facts)
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "final_state"):
            receipt.build_receipt(self.root, "wrong-facts.json")

    def test_rejects_failed_stage(self) -> None:
        self.facts["stages"][1]["checks"]["health"] = "fail"
        self._write("failed-facts.json", self.facts)
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "health"):
            receipt.build_receipt(self.root, "failed-facts.json")

    def test_rejects_missing_post_promotion_gate(self) -> None:
        self.facts.pop("post_promotion_gate")
        self._write("missing-gate-facts.json", self.facts)
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "字段不闭合"):
            receipt.build_receipt(self.root, "missing-gate-facts.json")

    def test_rejects_post_promotion_production_tree_mismatch(self) -> None:
        self.facts["target"]["source_tree_sha256"] = "f" * 64
        self._write("wrong-tree-facts.json", self.facts)
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "post-promotion"):
            receipt.build_receipt(self.root, "wrong-tree-facts.json")

    def test_replay_rejects_tampered_evidence(self) -> None:
        receipt.finalize(self.root, "facts.json", "receipt.json")
        self._write("stages/canary.json", {"stage": "canary", "ok": False})
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "摘要不一致"):
            receipt.replay(self.root, "receipt.json")


if __name__ == "__main__":
    unittest.main()
