"""Canonical checkpoint 的历史导入与原生初始化测试。"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import incremental_recovery


ROOT = Path(__file__).resolve().parents[3]


class CanonicalImportTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _fixture(self, root: Path) -> argparse.Namespace:
        campaign = root / "campaign"
        campaign.mkdir(mode=0o700)
        self._write(
            campaign / "campaign.json",
            {
                "campaign_mode": "formal",
                "campaign_id": "campaign-151",
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
            },
        )
        migration_path = campaign / "classification" / "approved" / "rule-migration.json"
        self._write(
            migration_path,
            {
                "status": "approved",
                "entries": [
                    {
                        "classification": "inherit",
                        "baseline_rule": "SPEC-A-001",
                        "target_rule": "SPEC-A-001",
                    },
                    {
                        "classification": "change",
                        "baseline_rule": "SPEC-A-002",
                        "target_rule": "SPEC-A-002",
                    },
                    {
                        "classification": "condition_change",
                        "baseline_rule": "SPEC-A-003",
                        "target_rule": "SPEC-A-003",
                    },
                ],
            },
        )
        self._write(
            campaign / "classification" / "result.json",
            {
                "migration_manifest": {
                    "path": migration_path.relative_to(campaign).as_posix(),
                    "sha256": codex_upgrade.file_sha256(migration_path),
                },
            },
        )
        active_profile = root / "active-profile.json"
        active_payload = {
            "Version": "0.149.1",
            "Digest": "c" * 64,
            "Surfaces": [{"PlatformPrefix": "x86_64"}],
            "Endpoints": [
                {"Headers": [{"Value": "0.149.1"}]},
                {"Body": {"Fields": None}},
            ],
        }
        self._write(active_profile, active_payload)
        target_profile = campaign / "classification" / "approved" / "profile.json"
        target_payload = {
            "Version": "0.151.0",
            "Digest": "d" * 64,
            "Surfaces": [{"PlatformPrefix": "aarch64"}],
            "Endpoints": [
                {"Headers": [{"Value": "0.151.0"}]},
                {"Body": {"Fields": [{"Name": "pdf_c2pa_create_request"}]}},
            ],
        }
        self._write(
            target_profile,
            {
                "codex_version": "0.151.0",
                "profile_payload": target_payload,
            },
        )
        classification = json.loads(
            (campaign / "classification" / "result.json").read_text(
                encoding="utf-8"
            )
        )
        classification["profile_manifest"] = {
            "path": target_profile.relative_to(campaign).as_posix(),
            "sha256": codex_upgrade.file_sha256(target_profile),
        }
        self._write(campaign / "classification" / "result.json", classification)
        patch_manifest = root / "profile-patches.json"
        self._write(
            patch_manifest,
            {
                "schema_version": "codex-upgrade-profile-rule-patches/v1",
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
                "active_profile_sha256": codex_upgrade.file_sha256(active_profile),
                "rule_patches": [
                    {
                        "rule_id": "SPEC-A-002",
                        "path": "/Surfaces/0/PlatformPrefix",
                        "before": "x86_64",
                        "after": "aarch64",
                    },
                    {
                        "rule_id": "SPEC-A-003",
                        "path": "/Endpoints/1/Body/Fields",
                        "before": None,
                        "after": [{"Name": "pdf_c2pa_create_request"}],
                    },
                ],
            },
        )
        transition = campaign / "classification-candidate-reuse-transition.json"
        self._write(transition, {"status": "approved"})
        transition_binding = {
            "path": transition.relative_to(campaign).as_posix(),
            "sha256": codex_upgrade.file_sha256(transition),
            "bytes": transition.stat().st_size,
        }
        candidate_id = "candidate-151"
        attempt_id = "attempt-151"
        attempt_root = campaign / "candidates" / candidate_id / "attempts" / attempt_id
        results = [
            {
                "id": job_id,
                "status": "complete",
                "disposition": "reused",
                "incremental_result_key": character * 64,
                "source_receipt": transition_binding,
            }
            for job_id, character in (("candidate-a", "a"), ("candidate-b", "b"))
        ]
        self._write(
            attempt_root / "attempt.json",
            {
                "campaign_id": "campaign-151",
                "phase": "candidate",
                "candidate_id": candidate_id,
                "attempt_id": attempt_id,
                "status": "awaiting_receipts",
                "incremental_plan": {
                    "planned_job_ids": ["candidate-a", "candidate-b"],
                    "reused_job_ids": ["candidate-a", "candidate-b"],
                    "affected_job_ids": [],
                    "failed_job_ids": [],
                    "pending_job_ids": [],
                    "executed_job_ids": [],
                },
                "results": results,
            },
        )
        kilo_path = attempt_root / "evidence" / "client" / "raw" / "kilo-facts.json"
        self._write(
            kilo_path,
            {
                "identity": {
                    "campaign_id": "campaign-151",
                    "candidate_id": candidate_id,
                    "attempt_id": attempt_id,
                    "target_version": "0.151.0",
                },
                "observations": {
                    "kilo-compatible": {"http_status": 200, "usage_id": "1"},
                    "kilo-responses": {"http_status": 101, "usage_id": "2"},
                },
            },
        )
        profile_path = attempt_root / "evidence" / "client" / "raw" / "profile-activation-fact.json"
        self._write(profile_path, {"status": "active", "candidate_id": candidate_id})
        supervisor = root / "supervisor"
        supervisor.mkdir(mode=0o700)
        started = time.time() - 60
        self._write(
            supervisor / "state.json",
            {
                "state": "running",
                "campaign_started_at_epoch": started,
                "started_at_epoch": started,
                "deadline_at_epoch": started + 3600,
            },
        )
        return argparse.Namespace(
            campaign_dir=campaign,
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            kilo_facts=kilo_path,
            active_profile=active_profile,
            profile_patch_manifest=patch_manifest,
            profile_activation_fact=profile_path,
            supervisor_run_dir=supervisor,
            phase="VC-5",
            retire_version="0.147.0",
            approve_import_sha256=None,
        )

    def _make_native_attempt(self, arguments: argparse.Namespace) -> None:
        """把历史全复用夹具改为 fresh Campaign 的全执行成功 attempt。"""

        campaign_path = arguments.campaign_dir / "campaign.json"
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign.update({"baseline_version": "0.151.0", "target_version": "0.154.0"})
        self._write(campaign_path, campaign)

        active = json.loads(arguments.active_profile.read_text(encoding="utf-8"))
        active["Version"] = "0.151.0"
        active["Endpoints"][0]["Headers"][0]["Value"] = "0.151.0"
        self._write(arguments.active_profile, active)

        classification_path = (
            arguments.campaign_dir / "classification" / "result.json"
        )
        classification = json.loads(classification_path.read_text(encoding="utf-8"))
        target_path = (
            arguments.campaign_dir / classification["profile_manifest"]["path"]
        )
        target = json.loads(target_path.read_text(encoding="utf-8"))
        target["codex_version"] = "0.154.0"
        target["profile_payload"]["Version"] = "0.154.0"
        target["profile_payload"]["Endpoints"][0]["Headers"][0]["Value"] = "0.154.0"
        self._write(target_path, target)
        classification["profile_manifest"]["sha256"] = codex_upgrade.file_sha256(
            target_path
        )
        self._write(classification_path, classification)

        patches = json.loads(
            arguments.profile_patch_manifest.read_text(encoding="utf-8")
        )
        patches.update(
            {
                "baseline_version": "0.151.0",
                "target_version": "0.154.0",
                "active_profile_sha256": codex_upgrade.file_sha256(
                    arguments.active_profile
                ),
            }
        )
        self._write(arguments.profile_patch_manifest, patches)

        attempt_path = (
            arguments.campaign_dir
            / "candidates"
            / arguments.candidate_id
            / "attempts"
            / arguments.attempt_id
            / "attempt.json"
        )
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        attempt["incremental_plan"].update(
            {
                "reused_job_ids": [],
                "affected_job_ids": ["candidate-a", "candidate-b"],
                "executed_job_ids": ["candidate-a", "candidate-b"],
            }
        )
        for result in attempt["results"]:
            result["disposition"] = "executed"
            result.pop("source_receipt", None)
        self._write(attempt_path, attempt)

        kilo = json.loads(arguments.kilo_facts.read_text(encoding="utf-8"))
        kilo["identity"]["target_version"] = "0.154.0"
        self._write(arguments.kilo_facts, kilo)

    def test_import_is_previewed_then_written_once_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._fixture(Path(directory))
            preview = codex_upgrade.import_canonical_checkpoint(arguments)
            self.assertEqual(preview["status"], "approval_required")
            self.assertEqual(
                preview["affected_rule_ids"], ["SPEC-A-002", "SPEC-A-003"]
            )
            self.assertEqual(preview["inherited_rule_ids"], ["SPEC-A-001"])
            self.assertEqual(preview["scanned_bytes"], 0)
            self.assertEqual(preview["live_request_count"], 0)
            self.assertEqual(preview["source_kind"], "historical-import")
            self.assertNotIn("candidate-a", preview["execute_item_ids"])

            arguments.approve_import_sha256 = preview["review_sha256"]
            completed = codex_upgrade.import_canonical_checkpoint(arguments)
            self.assertEqual(completed["status"], "complete")
            store = incremental_recovery.CanonicalCheckpointStore(
                arguments.campaign_dir / "canonical" / "checkpoints",
                create=False,
            )
            checkpoint = store.latest()
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            self.assertEqual(checkpoint["metrics"]["scanned_bytes"], 0)
            self.assertEqual(checkpoint["metrics"]["live_request_count"], 0)

            self.assertEqual(checkpoint["plan"]["execute_item_ids"], preview["execute_item_ids"])
            self.assertEqual(
                len(checkpoint["items"]),
                7,
            )
            inherited = {
                item["item_id"]: item
                for item in checkpoint["items"]
                if item["details"].get("kind") == "inherited-rule"
            }
            self.assertEqual(set(inherited), {"inherit-SPEC-A-001"})
            self.assertEqual(
                inherited["inherit-SPEC-A-001"]["source"]["sha256"],
                checkpoint["migration"]["manifest_sha256"],
            )
            self.assertEqual(
                codex_upgrade.import_canonical_checkpoint(arguments)["checkpoint"],
                completed["checkpoint"],
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "只允许历史只读解析"
            ):
                codex_upgrade._reject_canonical_legacy_write(arguments, "control-epoch")

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_reject_unparented_formal_write",
                    return_value=None,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    side_effect=AssertionError("canonical lease 不得重放历史 Campaign"),
                ),
                mock.patch.object(
                    codex_upgrade, "_main_without_campaign_lease", return_value=0
                ),
            ):
                code = codex_upgrade.main(
                    [
                        "canonical-advance",
                        "--campaign-dir",
                        str(arguments.campaign_dir),
                        "--candidate-id",
                        arguments.candidate_id,
                        "--attempt-id",
                        arguments.attempt_id,
                        "--canonical-step",
                        "seal",
                    ]
                )
            self.assertEqual(code, 0)
            lease = json.loads(
                (
                    arguments.campaign_dir
                    / codex_upgrade.CAMPAIGN_LEASE_FILENAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(lease["campaign_id"], "campaign-151")
            self.assertEqual(lease["state"], "released")

    def test_incomplete_0154_fixture_cannot_initialize_native_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._fixture(Path(directory))
            arguments.retire_version = "0.149.1"
            self._make_native_attempt(arguments)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "campaign.json 或 campaign.sha256",
            ):
                codex_upgrade.import_canonical_checkpoint(arguments)

    def test_0154_patch_manifest_binds_real_active_profile(self) -> None:
        """0.154 补丁必须绑定活动画像文件，并通过正式派生校验。"""

        active_profile = (
            ROOT
            / "backend/internal/officialegress/catalogdata/runtime/profiles/0.151.0"
            / "dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json"
        )
        patch_manifest = (
            ROOT
            / "tools/official_client_capture/profile_rule_patches_0_154_0.json"
        )
        patch_payload = json.loads(patch_manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            patch_payload["active_profile_sha256"],
            codex_upgrade.file_sha256(active_profile),
        )

        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            active_payload = json.loads(active_profile.read_text(encoding="utf-8"))
            target_payload, _ = codex_upgrade._replace_json_string_literal(
                active_payload,
                "0.151.0",
                "0.154.0",
            )
            target_payload["Digest"] = "e" * 64
            target_profile = fixture_root / "target-profile.json"
            migration = fixture_root / "rule-migration.json"
            self._write(
                target_profile,
                {
                    "codex_version": "0.154.0",
                    "profile_payload": target_payload,
                },
            )
            self._write(
                migration,
                {
                    "status": "approved",
                    "entries": [
                        {
                            "classification": "inherit",
                            "baseline_rule": "SPEC-CODEX-IDENTITY",
                            "target_rule": "SPEC-CODEX-IDENTITY",
                        }
                    ],
                },
            )

            result = codex_upgrade.validate_profile_derivation(
                active_profile_path=active_profile,
                target_profile_path=target_profile,
                migration_path=migration,
                patch_manifest_path=patch_manifest,
            )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["affected_rule_ids"], [])
            self.assertEqual(result["live_request_count"], 0)

    def test_advance_seals_compares_and_accepts_only_affected_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._fixture(Path(directory))
            preview = codex_upgrade.import_canonical_checkpoint(arguments)
            arguments.approve_import_sha256 = preview["review_sha256"]
            codex_upgrade.import_canonical_checkpoint(arguments)
            advance = argparse.Namespace(
                campaign_dir=arguments.campaign_dir,
                candidate_id=arguments.candidate_id,
                attempt_id=arguments.attempt_id,
                canonical_step="seal",
            )

            seal = codex_upgrade.advance_canonical_checkpoint(advance)
            self.assertEqual(seal["status"], "complete")
            self.assertEqual(seal["scanned_bytes"], 0)
            self.assertEqual(seal["live_request_count"], 0)

            advance.canonical_step = "compare"
            comparison = codex_upgrade.advance_canonical_checkpoint(advance)
            self.assertTrue(comparison["equal"])
            self.assertEqual(comparison["affected_assertion_count"], 2)
            self.assertEqual(comparison["inherited_receipt_count"], 1)

            advance.canonical_step = "accept"
            acceptance = codex_upgrade.advance_canonical_checkpoint(advance)
            self.assertTrue(acceptance["accepted"])
            self.assertEqual(
                acceptance["executed_affected_assertion_ids"],
                ["assert-SPEC-A-002", "assert-SPEC-A-003"],
            )
            self.assertEqual(
                acceptance["replayed_inherited_receipt_ids"],
                ["inherit-SPEC-A-001"],
            )
            checkpoint = incremental_recovery.CanonicalCheckpointStore(
                arguments.campaign_dir / "canonical" / "checkpoints",
                create=False,
            ).latest()
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            self.assertEqual(
                checkpoint["plan"]["execute_item_ids"],
                ["production-activation", "retire-0.147.0", "rollback-verification"],
            )
            self.assertEqual(
                checkpoint["metrics"],
                {"scanned_bytes": 0, "live_request_count": 0},
            )
            self.assertEqual(
                codex_upgrade.advance_canonical_checkpoint(advance)["checkpoint_sha256"],
                checkpoint["checkpoint_sha256"],
            )

    def test_vc6_only_records_replayed_activation_rollback_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._fixture(Path(directory))
            preview = codex_upgrade.import_canonical_checkpoint(arguments)
            arguments.approve_import_sha256 = preview["review_sha256"]
            codex_upgrade.import_canonical_checkpoint(arguments)
            advance = argparse.Namespace(
                campaign_dir=arguments.campaign_dir,
                candidate_id=arguments.candidate_id,
                attempt_id=arguments.attempt_id,
                canonical_step="seal",
                step_receipt=None,
            )
            codex_upgrade.advance_canonical_checkpoint(advance)
            advance.canonical_step = "compare"
            codex_upgrade.advance_canonical_checkpoint(advance)
            advance.canonical_step = "accept"
            codex_upgrade.advance_canonical_checkpoint(advance)
            accepted = incremental_recovery.CanonicalCheckpointStore(
                arguments.campaign_dir / "canonical" / "checkpoints",
                create=False,
            ).latest()
            self.assertIsNotNone(accepted)
            assert accepted is not None
            acceptance = next(
                item for item in accepted["items"] if item["item_id"] == "acceptance"
            )

            activation_path = (
                arguments.campaign_dir / "canonical" / "activation" / "receipt.json"
            )
            self._write(activation_path, {"immutable": "activation"})
            target_image = f"sha256:{'1' * 64}"
            rollback_image = f"sha256:{'2' * 64}"
            activation = {
                "campaign": {
                    "id": "campaign-151",
                    "candidate_id": "candidate-151",
                    "acceptance": acceptance["source"],
                },
                "target": {"version": "0.151.0", "image_id": target_image},
                "rollback": {"version": "0.149.1", "image_id": rollback_image},
                "stages": [
                    {"name": "canary", "status": "pass", "image_id": target_image},
                    {
                        "name": "production_switch",
                        "status": "pass",
                        "image_id": target_image,
                    },
                    {"name": "rollback", "status": "pass", "image_id": rollback_image},
                    {
                        "name": "target_restore",
                        "status": "pass",
                        "image_id": target_image,
                    },
                ],
                "final_state": {
                    "active_version": "0.151.0",
                    "image_id": target_image,
                },
            }
            advance.step_receipt = activation_path
            with mock.patch.object(
                codex_upgrade.production_activation_receipt,
                "replay",
                return_value=activation,
            ):
                advance.canonical_step = "production-activation"
                self.assertEqual(
                    codex_upgrade.advance_canonical_checkpoint(advance)["status"],
                    "complete",
                )
                advance.canonical_step = "rollback-verification"
                self.assertEqual(
                    codex_upgrade.advance_canonical_checkpoint(advance)["status"],
                    "complete",
                )

            activation_sha256 = codex_upgrade.file_sha256(activation_path)
            removal_path = (
                arguments.campaign_dir / "canonical" / "activation" / "removal.json"
            )
            self._write(
                removal_path,
                {
                    "schema_version": "codex-runtime-profile-removal/v1",
                    "status": "complete",
                    "campaign_id": "campaign-151",
                    "active_version": "0.151.0",
                    "rollback_version": "0.149.1",
                    "removed_version": "0.147.0",
                    "production_activation_sha256": activation_sha256,
                    "consumer_scan": {
                        "catalog_references": 0,
                        "selector_references": 0,
                        "unknown_references": 0,
                    },
                    "runtime_catalog_removed": True,
                    "historical_evidence_preserved": True,
                },
            )
            advance.canonical_step = "retire"
            advance.retire_version = "0.147.0"
            advance.step_receipt = removal_path
            result = codex_upgrade.advance_canonical_checkpoint(advance)
            self.assertEqual(result["status"], "complete")
            completed = incremental_recovery.CanonicalCheckpointStore(
                arguments.campaign_dir / "canonical" / "checkpoints",
                create=False,
            ).latest()
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed["phase"], "VC-6")
            self.assertEqual(completed["plan"]["execute_item_ids"], [])


if __name__ == "__main__":
    unittest.main()
