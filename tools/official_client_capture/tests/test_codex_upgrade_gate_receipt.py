"""Codex candidate 与 post-promotion 外部门禁收据测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_gate_receipt as receipt


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

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def _gates(self, phase: str) -> list[dict[str, object]]:
        contracts = (
            receipt.CANDIDATE_COMMANDS
            if phase == receipt.CANDIDATE_PHASE
            else receipt.POST_PROMOTION_COMMANDS
        )
        gates: list[dict[str, object]] = []
        for index, gate_id in enumerate(sorted(contracts)):
            cwd, command = contracts[gate_id]
            evidence = self._write(
                f"evidence/{phase}-{gate_id}.json",
                {"gate_id": gate_id, "passed": True},
            )
            gates.append(
                {
                    "gate_id": gate_id,
                    "command": list(command),
                    "working_directory": cwd,
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
            )
        return gates

    def _facts(self, phase: str) -> dict[str, object]:
        subject = self._subject(phase)
        inputs: list[dict[str, str]] = []
        if phase == receipt.POST_PROMOTION_PHASE:
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
            "subject": subject,
            "inputs": inputs,
            "gates": self._gates(phase),
        }

    def test_candidate_finalize_and_replay(self) -> None:
        self._write("candidate-facts.json", self._facts(receipt.CANDIDATE_PHASE))
        finalized = receipt.finalize(
            self.root, "candidate-facts.json", "candidate-receipt.json"
        )
        replayed = receipt.replay(self.root, "candidate-receipt.json")
        self.assertEqual(finalized, replayed)
        self.assertEqual(finalized["phase"], receipt.CANDIDATE_PHASE)

    def test_post_promotion_binds_acceptance_and_promotion(self) -> None:
        self._write("post-facts.json", self._facts(receipt.POST_PROMOTION_PHASE))
        finalized = receipt.finalize(self.root, "post-facts.json", "post-receipt.json")
        self.assertEqual([item["role"] for item in finalized["inputs"]], ["acceptance", "promotion"])
        self.assertEqual(finalized, receipt.replay(self.root, "post-receipt.json"))

    def test_missing_gate_fails_closed(self) -> None:
        facts = self._facts(receipt.CANDIDATE_PHASE)
        facts["gates"].pop()
        self._write("missing.json", facts)
        with self.assertRaisesRegex(receipt.GateReceiptError, "唯一且完整覆盖"):
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

    def test_schema_matches_runtime_version(self) -> None:
        schema_path = Path(receipt.__file__).with_name(
            "codex_upgrade_gate_receipt.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipt.RECEIPT_SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
