from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import production_activation_receipt as receipt


class ProductionActivationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.target_image = f"sha256:{'1' * 64}"
        self.rollback_image = f"sha256:{'2' * 64}"
        self.profile_digest = "3" * 64
        self.acceptance = self._write(
            "inputs/acceptance.json",
            {"status": "complete", "accepted": True, "candidate_id": "k83-dmit"},
        )
        self.facts = self._facts()
        self._write("facts.json", self.facts)

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def test_replay_rejects_tampered_evidence(self) -> None:
        receipt.finalize(self.root, "facts.json", "receipt.json")
        self._write("stages/canary.json", {"stage": "canary", "ok": False})
        with self.assertRaisesRegex(receipt.ProductionReceiptError, "摘要不一致"):
            receipt.replay(self.root, "receipt.json")


if __name__ == "__main__":
    unittest.main()
