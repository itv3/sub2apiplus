"""Codex candidate 与 post-promotion 外部门禁收据测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_gate_receipt as receipt
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts


class CodexUpgradeGateReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.profile_digest = "1" * 64
        self.package_digest = "2" * 64
        self.source_tree = "3" * 64
        self.image_id = f"sha256:{'4' * 64}"
        self.image_reference = f"registry/sub2api@{self.image_id}"
        self.continuity_by_attempt: dict[str, str] = {}
        self.environment_patcher = mock.patch.object(
            receipt.codex_upgrade_arm64_environment_receipt,
            "replay",
            side_effect=self._replay_environment,
        )
        self.environment_patcher.start()

    def tearDown(self) -> None:
        self.environment_patcher.stop()
        self.temporary.cleanup()

    def _replay_environment(self, root: Path, relative: str) -> dict[str, object]:
        del root
        name = Path(relative).stem
        attempt_id, role = name.rsplit("-", 1)
        return {
            "status": "passed",
            "phase": "gate_before" if role == "before" else "gate_after",
            "subject_id": attempt_id,
            "continuity_identity_sha256": self.continuity_by_attempt.get(
                attempt_id, "8" * 64
            ),
        }

    def _write(self, relative: str, value: object) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _subject(self, phase: str) -> dict[str, object]:
        return {
            "campaign_id": "codex-0_999_0-campaign",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "candidate_id": "candidate-a",
            "candidate_purpose": "production_replacement",
            "target_version": "0.999.0",
            "target_architecture": "linux/amd64",
            "profile_id": "codex-0.999.0",
            "profile_digest": self.profile_digest,
            "candidate_package_digest": self.package_digest,
            "candidate_source_tree_sha256": self.source_tree,
            "candidate_image_id": self.image_id,
            "candidate_image_reference": self.image_reference,
            "production_tree_sha256": "5" * 64 if phase == receipt.POST_PROMOTION_PHASE else None,
            "acceptance_sha256": None,
            "promotion_receipt_sha256": None,
        }

    def _post_gate_plan(
        self,
        subject: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, str]]:
        requirements = vc_artifacts.build_gate_requirements(
            campaign_id=str(subject["campaign_id"]),
            target_version=str(subject["target_version"]),
            joint_manifest_sha256="a" * 64,
            affected_rule_ids=["SPEC-HDR-005"],
            inherited_rule_ids=["SPEC-BODY-001"],
            migration_manifest={"path": "inputs/migration.json", "sha256": "b" * 64},
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
        plan = vc_artifacts.build_gate_plan(requirements, mapping)
        path = self._write("inputs/gate-plan.json", plan)
        return plan, {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": self._digest(path),
        }

    def _gates(
        self,
        phase: str,
        gate_plan: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        if phase == receipt.CANDIDATE_PHASE:
            contracts = [
                {
                    "gate_id": gate_id,
                    "working_directory": cwd,
                    "command": list(command),
                    "test_id": None,
                }
                for gate_id, (cwd, command) in receipt.CANDIDATE_COMMANDS.items()
            ]
        else:
            assert gate_plan is not None
            contracts = list(gate_plan["gates"])
        gates: list[dict[str, object]] = []
        for index, contract in enumerate(
            sorted(contracts, key=lambda item: str(item["gate_id"]))
        ):
            gate_id = str(contract["gate_id"])
            evidence = self._write(
                f"evidence/{phase}-{gate_id}.json",
                {"gate_id": gate_id, "passed": True},
            )
            gate = {
                "gate_id": gate_id,
                "command": list(contract["command"]),
                "working_directory": contract["working_directory"],
                "host": "runner-1",
                "architecture": "linux/amd64",
                "started_at_utc": f"2026-08-23T00:{index:02d}:00Z",
                "completed_at_utc": f"2026-08-23T00:{index:02d}:30Z",
                "exit_code": 0,
                "status": "passed",
                "passed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "stdout_sha256": "6" * 64,
                "stderr_sha256": "7" * 64,
                "evidence": [
                    {
                        "path": evidence.relative_to(self.root).as_posix(),
                        "sha256": self._digest(evidence),
                    }
                ],
            }
            if phase == receipt.POST_PROMOTION_PHASE:
                gate["test_id"] = contract["test_id"]
            gates.append(gate)
        return gates

    def _environment(self, attempt_id: str) -> dict[str, dict[str, str]]:
        values: dict[str, dict[str, str]] = {}
        for role in ("before", "after"):
            path = self._write(
                f"environment/{attempt_id}-{role}.json",
                {"attempt_id": attempt_id, "role": role},
            )
            values[role] = {
                "path": path.relative_to(self.root).as_posix(),
                "sha256": self._digest(path),
            }
        return values

    def _facts(
        self,
        phase: str,
        *,
        attempt_id: str = "gate-attempt-001",
        root_cause_id: str | None = None,
        previous_receipt: str | None = None,
    ) -> dict[str, object]:
        subject = self._subject(phase)
        gate_plan: dict[str, object] | None = None
        gate_plan_reference: dict[str, str] | None = None
        inputs: list[dict[str, str]] = []
        if phase == receipt.POST_PROMOTION_PHASE:
            gate_plan, gate_plan_reference = self._post_gate_plan(subject)
            acceptance = self._write(
                "inputs/acceptance.json",
                {
                    "status": "complete",
                    "accepted": True,
                    "campaign_mode": subject["campaign_mode"],
                    "campaign_purpose": subject["campaign_purpose"],
                    "candidate_id": subject["candidate_id"],
                    "candidate_purpose": subject["candidate_purpose"],
                    "production_state": "accepted_not_activated",
                    "target_version": subject["target_version"],
                    "profile_id": subject["profile_id"],
                    "profile_digest": subject["profile_digest"],
                    "candidate_package_digest": subject["candidate_package_digest"],
                    "candidate_identity": {
                        "gate_plan": {
                            "sha256": gate_plan_reference["sha256"],
                            "plan_sha256": gate_plan["plan_sha256"],
                            "requirements_sha256": gate_plan["requirements_sha256"],
                        }
                    },
                },
            )
            subject["acceptance_sha256"] = self._digest(acceptance)
            promotion = self._write(
                "inputs/promotion.json",
                {
                    "schema_version": "official-egress-catalog-promotion/v1",
                    "campaign_id": subject["campaign_id"],
                    "acceptance_sha256": subject["acceptance_sha256"],
                    "target_version": subject["target_version"],
                    "target_profile_digest": subject["profile_digest"],
                    "production_selector_changed": True,
                },
            )
            subject["promotion_receipt_sha256"] = self._digest(promotion)
            inputs = [
                {
                    "role": "acceptance",
                    "path": acceptance.relative_to(self.root).as_posix(),
                    "sha256": self._digest(acceptance),
                },
                {
                    "role": "promotion",
                    "path": promotion.relative_to(self.root).as_posix(),
                    "sha256": self._digest(promotion),
                },
            ]
        return {
            "schema_version": receipt.FACTS_SCHEMA,
            "phase": phase,
            "attempt": {
                "attempt_id": attempt_id,
                "root_cause_id": root_cause_id,
                "previous_receipt": (
                    {
                        "path": previous_receipt,
                        "sha256": self._digest(self.root / previous_receipt),
                    }
                    if previous_receipt is not None
                    else None
                ),
            },
            "subject": subject,
            "inputs": inputs,
            "gate_plan": gate_plan_reference,
            "environment": self._environment(attempt_id),
            "gates": self._gates(phase, gate_plan),
        }

    def test_candidate_finalize_and_replay(self) -> None:
        self._write("candidate-facts.json", self._facts(receipt.CANDIDATE_PHASE))
        finalized = receipt.finalize(
            self.root, "candidate-facts.json", "candidate-receipt.json"
        )
        replayed = receipt.replay(self.root, "candidate-receipt.json")
        self.assertEqual(finalized, replayed)
        self.assertEqual(finalized["phase"], receipt.CANDIDATE_PHASE)
        self.assertEqual(finalized["status"], "passed")

    def test_post_promotion_binds_acceptance_and_promotion(self) -> None:
        self._write("post-facts.json", self._facts(receipt.POST_PROMOTION_PHASE))
        finalized = receipt.finalize(self.root, "post-facts.json", "post-receipt.json")
        self.assertEqual([item["role"] for item in finalized["inputs"]], ["acceptance", "promotion"])
        self.assertEqual(finalized, receipt.replay(self.root, "post-receipt.json"))

    def test_post_promotion_requires_gate_plan(self) -> None:
        facts = self._facts(receipt.POST_PROMOTION_PHASE)
        facts["gate_plan"] = None
        self._write("missing-plan.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "必须绑定 VC-4"):
            receipt.build_receipt(self.root, "missing-plan.json")

    def test_post_promotion_rejects_command_or_test_id_drift(self) -> None:
        for field, value in (("command", ["python3", "-m", "other"]), ("test_id", "other-test")):
            with self.subTest(field=field):
                facts = self._facts(receipt.POST_PROMOTION_PHASE)
                facts["gates"][0][field] = value
                path = f"drift-{field}.json"
                self._write(path, facts)
                with self.assertRaisesRegex(receipt.GateReceiptError, "冻结合同"):
                    receipt.build_receipt(self.root, path)

    def test_invalid_gate_id_fails_as_managed_error(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"][0]["gate_id"] = []
        self._write("bad-gate-id.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "gate_id"):
            receipt.build_receipt(self.root, "bad-gate-id.json")

    def test_v3_facts_cannot_finalize_but_historical_receipt_replays(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["schema_version"] = receipt.LEGACY_FACTS_SCHEMA
        facts.pop("gate_plan")
        self._write("legacy-facts.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "只允许历史收据重放"):
            receipt.finalize(self.root, "legacy-facts.json", "legacy-new.json")

        historical = receipt.build_receipt(
            self.root,
            "legacy-facts.json",
            _allow_legacy=True,
        )
        historical_path = self.root / "legacy-receipt.json"
        historical_path.write_bytes(receipt._canonical(historical))
        historical_path.chmod(0o600)
        self.assertEqual(
            receipt.replay(self.root, "legacy-receipt.json"),
            historical,
        )

    def test_missing_gate_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"].pop()
        self._write("missing.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "补跑集合非法"):
            receipt.build_receipt(self.root, "missing.json")

    def test_nonzero_or_skipped_gate_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"][0]["skipped_count"] = 1
        self._write("skipped.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "非预期跳过"):
            receipt.build_receipt(self.root, "skipped.json")

    def test_candidate_purpose_drift_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["subject"]["candidate_purpose"] = "validation_only"
        self._write("purpose-drift.json", facts)
        with self.assertRaisesRegex(
            receipt.GateReceiptError,
            "与 Campaign 用途不一致",
        ):
            receipt.build_receipt(self.root, "purpose-drift.json")

    def test_replay_rejects_tampered_gate_evidence(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        self._write("facts.json", facts)
        receipt.finalize(self.root, "facts.json", "receipt.json")
        evidence = self.root / facts["gates"][0]["evidence"][0]["path"]
        self._write(evidence.relative_to(self.root).as_posix(), {"tampered": True})
        with self.assertRaisesRegex(receipt.GateReceiptError, "摘要不一致"):
            receipt.replay(self.root, "receipt.json")

    def test_output_is_write_once(self) -> None:
        self._write("facts.json", self._facts(receipt.CANDIDATE_PHASE))
        receipt.finalize(self.root, "facts.json", "receipt.json")
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止覆盖"):
            receipt.finalize(self.root, "facts.json", "receipt.json")

    def test_retry_carries_passed_gates_and_only_executes_failed_gate(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][1].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        failed_id = first["gates"][1]["gate_id"]
        self._write("attempt-1-facts.json", first)
        failed = receipt.finalize(
            self.root, "attempt-1-facts.json", "attempt-1-receipt.json"
        )
        self.assertEqual(failed["status"], "failed")

        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="attempt-1-receipt.json",
        )
        second["gates"] = [
            item for item in second["gates"] if item["gate_id"] == failed_id
        ]
        self._write("attempt-2-facts.json", second)
        completed = receipt.finalize(
            self.root, "attempt-2-facts.json", "attempt-2-receipt.json"
        )
        self.assertEqual(completed["status"], "passed")
        self.assertEqual(completed["executed_gate_ids"], [failed_id])
        self.assertNotIn(failed_id, completed["carried_gate_ids"])
        self.assertEqual(
            sorted(completed["passed_gate_ids"]),
            sorted(receipt.CANDIDATE_COMMANDS),
        )

    def test_retry_rejects_rerunning_an_already_passed_gate(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][0].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        self._write("first-facts.json", first)
        receipt.finalize(self.root, "first-facts.json", "first-receipt.json")
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="first-receipt.json",
        )
        self._write("illegal-rerun.json", second)
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止重跑已通过项"):
            receipt.build_receipt(self.root, "illegal-rerun.json")

    def test_third_same_root_cause_attempt_is_rejected(self) -> None:
        previous: str | None = None
        failed_id = sorted(receipt.CANDIDATE_COMMANDS)[0]
        for index in (1, 2):
            facts = self._facts(
                receipt.CANDIDATE_PHASE,
                attempt_id=f"gate-attempt-00{index}",
                root_cause_id="root-cause-a",
                previous_receipt=previous,
            )
            facts["gates"] = [
                item for item in facts["gates"] if item["gate_id"] == failed_id
            ] if previous else facts["gates"]
            failed_gate = next(
                item for item in facts["gates"] if item["gate_id"] == failed_id
            )
            failed_gate.update(
                {"status": "failed", "exit_code": 1, "failed_count": 1}
            )
            facts_name = f"attempt-{index}-facts.json"
            receipt_name = f"attempt-{index}-receipt.json"
            self._write(facts_name, facts)
            receipt.finalize(self.root, facts_name, receipt_name)
            previous = receipt_name
        third = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-003",
            root_cause_id="root-cause-a",
            previous_receipt=previous,
        )
        third["gates"] = [
            item for item in third["gates"] if item["gate_id"] == failed_id
        ]
        self._write("attempt-3-facts.json", third)
        with self.assertRaisesRegex(receipt.GateReceiptError, "禁止第三次"):
            receipt.build_receipt(self.root, "attempt-3-facts.json")

    def test_retry_rejects_environment_continuity_drift(self) -> None:
        first = self._facts(
            receipt.CANDIDATE_PHASE,
            root_cause_id="root-cause-a",
        )
        first["gates"][0].update(
            {"status": "failed", "exit_code": 1, "failed_count": 1}
        )
        failed_id = first["gates"][0]["gate_id"]
        self._write("first-facts.json", first)
        receipt.finalize(self.root, "first-facts.json", "first-receipt.json")
        self.continuity_by_attempt["gate-attempt-002"] = "9" * 64
        second = self._facts(
            receipt.CANDIDATE_PHASE,
            attempt_id="gate-attempt-002",
            root_cause_id="root-cause-a",
            previous_receipt="first-receipt.json",
        )
        second["gates"] = [
            item for item in second["gates"] if item["gate_id"] == failed_id
        ]
        self._write("drift.json", second)
        with self.assertRaisesRegex(receipt.GateReceiptError, "连续性无法证明"):
            receipt.build_receipt(self.root, "drift.json")

    def test_schema_matches_runtime_version(self) -> None:
        schema_path = Path(receipt.__file__).with_name(
            "codex_upgrade_gate_receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )

    def test_direct_script_help_uses_repository_import_root(self) -> None:
        script = Path(receipt.__file__).resolve()
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[2],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("finalize", completed.stdout)


if __name__ == "__main__":
    unittest.main()
