"""VC-4～VC-6 统一收据的闭集、重放与防篡改测试。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt as receipts


class CodexUpgradeVCReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        path.chmod(0o600)

    @staticmethod
    def _subject(
        purpose: str = "validation_only",
        *,
        attempt_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "upgrade_id": "codex-0154-upgrade",
            "campaign_id": "codex-0154-campaign",
            "campaign_purpose": purpose,
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "candidate_id": "candidate-a",
            "attempt_id": attempt_id,
        }

    def _implementation_facts(self) -> dict[str, object]:
        return {
            "schema_version": receipts.FACTS_SCHEMA,
            "kind": "implementation_tests",
            "subject": self._subject(),
            "assertions": {
                "git_commit": "1" * 40,
                "source_tree_sha256": "2" * 64,
                "target_architecture": "linux/arm64",
                "gates": [
                    {
                        "gate_id": "affected-spec-hdr-005",
                        "kind": "affected",
                        "command": ["python3", "-m", "test_affected_header"],
                        "exit_code": 0,
                        "passed": 1,
                        "failed": 0,
                        "approved_skip": 0,
                        "unexpected_skip": 0,
                    },
                    {
                        "gate_id": "check-egress-spec",
                        "kind": "public",
                        "command": ["make", "check-egress-spec"],
                        "exit_code": 0,
                        "passed": 1,
                        "failed": 0,
                        "approved_skip": 0,
                        "unexpected_skip": 0,
                    },
                ],
            },
            "evidence": [
                {"role": "check_egress_spec", "path": "logs/check-egress-spec.log"},
                {"role": "implementation_tests", "path": "logs/implementation.log"},
            ],
        }

    def _finalize_implementation(self) -> Path:
        self._write(self.root / "logs" / "check-egress-spec.log", b"passed\n")
        self._write(self.root / "logs" / "implementation.log", b"passed\n")
        self._write(self.root / "facts.json", self._implementation_facts())
        receipts.finalize(self.root, "facts.json", "receipt.json")
        return self.root / "receipt.json"

    def test_implementation_receipt_and_evidence_tampering_are_rejected(self) -> None:
        receipt_path = self._finalize_implementation()
        replayed = receipts.replay(self.root, "receipt.json")
        self.assertEqual(replayed["kind"], "implementation_tests")
        self.assertEqual(replayed["status"], "passed")

        tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered["assertions"]["target_architecture"] = "linux/amd64"
        self._write(receipt_path, tampered)
        with self.assertRaisesRegex(receipts.VCReceiptError, "自摘要"):
            receipts.replay(self.root, "receipt.json")

        receipt_path.unlink()
        self._finalize_implementation()
        (self.root / "logs" / "implementation.log").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(receipts.VCReceiptError, "摘要或大小漂移"):
            receipts.replay(self.root, "receipt.json")

    def test_completion_receipt_roles_follow_purpose(self) -> None:
        binding = {
            "path": "facts/acceptance.json",
            "sha256": "3" * 64,
            "bytes": 12,
        }
        validation_vc5 = receipts.build_receipt(
            {
                "schema_version": receipts.FACTS_SCHEMA,
                "kind": "vc5_completion",
                "subject": self._subject(attempt_id="attempt-a"),
                "assertions": {
                    "acceptance_passed": True,
                    "vc5_pending_count": 0,
                    "canonical_handoff": "not_required",
                },
                "evidence": [
                    {"role": "acceptance_fact", "path": binding["path"]}
                ],
            },
            [{"role": "acceptance_fact", **binding}],
            issued_at_utc="2026-09-13T00:00:00Z",
        )
        self.assertEqual(validation_vc5["status"], "complete")

        production_subject = self._subject(
            "production_replacement",
            attempt_id="attempt-a",
        )
        with self.assertRaisesRegex(receipts.VCReceiptError, "evidence 角色"):
            receipts.build_receipt(
                {
                    "schema_version": receipts.FACTS_SCHEMA,
                    "kind": "vc5_completion",
                    "subject": production_subject,
                    "assertions": {
                        "acceptance_passed": True,
                        "vc5_pending_count": 0,
                        "canonical_handoff": "complete",
                    },
                    "evidence": [
                        {"role": "acceptance_fact", "path": binding["path"]}
                    ],
                },
                [{"role": "acceptance_fact", **binding}],
                issued_at_utc="2026-09-13T00:00:00Z",
            )

    def test_vc_checkpoint_replay_recomputes_stage_receipt_digest(self) -> None:
        campaign = self.root / "campaign"
        campaign.mkdir(mode=0o700)
        plan = artifacts.build_campaign_plan(
            campaign_id="codex-0154-campaign",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-13T00:00:00Z",
            original_deadline_at_utc="2026-09-13T06:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )
        stage0 = campaign / "control" / "vc" / "receipts" / "vc0.json"
        self._write(stage0, {"stage": "VC-0"})
        checkpoint0 = artifacts.build_vc_checkpoint(
            campaign_plan=plan,
            phase="VC-0",
            status="complete",
            predecessor_checkpoint=None,
            stage_receipt={
                "path": stage0.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(stage0),
            },
            completed_at_utc="2026-09-13T00:01:00Z",
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        checkpoint0_path = campaign / "control" / "vc" / "vc-0-checkpoint.json"
        self._write(checkpoint0_path, checkpoint0)

        stage1 = campaign / "control" / "vc" / "receipts" / "vc1.json"
        self._write(stage1, {"stage": "VC-1"})
        checkpoint1 = artifacts.build_vc_checkpoint(
            campaign_plan=plan,
            phase="VC-1",
            status="complete",
            predecessor_checkpoint={
                "path": checkpoint0_path.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(checkpoint0_path),
                "phase": "VC-0",
                "checkpoint_sha256": checkpoint0["checkpoint_sha256"],
            },
            stage_receipt={
                "path": stage1.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(stage1),
            },
            completed_at_utc="2026-09-13T00:02:00Z",
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        self._write(campaign / "control" / "vc" / "vc-1-checkpoint.json", checkpoint1)
        codex_upgrade._replay_vc_checkpoint(campaign, plan, "VC-1")

        self._write(stage1, {"stage": "VC-1", "tampered": True})
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "阶段收据摘要漂移",
        ):
            codex_upgrade._replay_vc_checkpoint(campaign, plan, "VC-1")

    def test_action_plan_rejects_string_reuse_item_ids(self) -> None:
        with self.assertRaisesRegex(artifacts.VCArtifactError, "reuse_item_ids"):
            artifacts.validate_action_plan(
                {
                    "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
                    "execute_item_ids": [],
                    "reuse_item_ids": "reuse-a",
                    "actions": [],
                }
            )

    def test_schema_file_matches_runtime_version(self) -> None:
        schema = json.loads(
            Path(receipts.__file__)
            .with_name("codex_upgrade_vc_receipt.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            receipts.RECEIPT_SCHEMA,
        )


if __name__ == "__main__":
    unittest.main()
