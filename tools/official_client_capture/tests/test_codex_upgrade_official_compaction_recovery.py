"""0.154 Main→Astra Lite 两项 official 恢复的离线控制面门禁。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor


class OfficialCompactionRecoveryTest(unittest.TestCase):
    @staticmethod
    def _tool_identity(entries: list[dict[str, str]]) -> dict[str, object]:
        components = codex_upgrade._tool_component_identities(entries)
        return {
            "entry_count": len(entries),
            "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
            "entries": entries,
            "components": components["components"],
            "component_identity_sha256": codex_upgrade._fingerprint(components),
            **codex_upgrade._tool_identity_sides(entries),
        }

    @classmethod
    def _fixture(cls) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        recovery_ids = sorted(
            codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS
        )
        pass_ids = [f"official-pass-{index:02d}" for index in range(27)]
        planned = sorted([*recovery_ids, *pass_ids])
        expected_entries = [
            {
                "path": "codex_upgrade.py",
                "sha256": "1" * 64,
            },
            {
                "path": "codex_upgrade_scenarios_0_154_0.json",
                "sha256": codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES[
                    "codex_upgrade_scenarios_0_154_0.json"
                ]["from_sha256"],
            },
            {
                "path": "run_official_relay_scenario.sh",
                "sha256": codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES[
                    "run_official_relay_scenario.sh"
                ]["from_sha256"],
            },
        ]
        current_entries = [
            {
                "path": "build_compaction_model_catalog.py",
                "sha256": codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES[
                    "build_compaction_model_catalog.py"
                ]["to_sha256"],
            },
            {"path": "codex_upgrade.py", "sha256": "2" * 64},
            {
                "path": "codex_upgrade_scenarios_0_154_0.json",
                "sha256": codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES[
                    "codex_upgrade_scenarios_0_154_0.json"
                ]["to_sha256"],
            },
            {
                "path": "run_official_relay_scenario.sh",
                "sha256": codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES[
                    "run_official_relay_scenario.sh"
                ]["to_sha256"],
            },
        ]
        manifest: dict[str, object] = {
            "campaign_id": "formal-0154",
            "campaign_mode": "formal",
            "target_version": "0.154.0",
            "configuration": {
                "model": "gpt-5.5",
                "lite_model": "gpt-6-astra",
            },
            "tool_identity": cls._tool_identity(expected_entries),
            "jobs": [
                {"id": job_id, "phase": "official"} for job_id in planned
            ],
        }
        current = cls._tool_identity(current_entries)
        source_attempt: dict[str, object] = {
            "campaign_id": "formal-0154",
            "phase": "official",
            "candidate_id": None,
            "status": "failed",
        }
        scope: dict[str, object] = {
            "planned_job_ids": planned,
            "completed_job_ids": sorted(pass_ids),
            "failed_job_ids": recovery_ids,
            "pending_job_ids": [],
            "execute_job_ids": recovery_ids,
        }
        return manifest, current, source_attempt, scope

    def test_exact_three_file_recovery_produces_two_by_twenty_seven_scope(self) -> None:
        manifest, current, source_attempt, scope = self._fixture()
        impact = codex_upgrade._official_compaction_lite_recovery_tool_impact(
            manifest,
            current,
            source_attempt=source_attempt,
            recovery_scope=scope,
        )
        self.assertIsNotNone(impact)
        assert impact is not None
        self.assertEqual(
            impact["kind"], codex_upgrade.BATCHED_FAILED_CAPTURE_RECOVERY_KIND
        )
        self.assertEqual(
            impact["affected_job_ids"],
            sorted(codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS),
        )
        self.assertEqual(
            impact["allowed_production_paths"],
            sorted(codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES),
        )
        self.assertEqual(impact["models"]["lite_use_responses_lite"], True)

    def test_each_reviewed_production_digest_drift_is_rejected(self) -> None:
        for path in sorted(codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_FILES):
            with self.subTest(path=path):
                manifest, current, source_attempt, scope = self._fixture()
                entries = current["entries"]
                assert isinstance(entries, list)
                for item in entries:
                    if item["path"] == path:
                        item["sha256"] = "f" * 64
                current = self._tool_identity(entries)
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "摘要漂移",
                ):
                    codex_upgrade._official_compaction_lite_recovery_tool_impact(
                        manifest,
                        current,
                        source_attempt=source_attempt,
                        recovery_scope=scope,
                    )

    def test_third_affected_job_is_rejected(self) -> None:
        manifest, current, source_attempt, scope = self._fixture()
        third = scope["completed_job_ids"][0]
        scope["completed_job_ids"] = scope["completed_job_ids"][1:]
        scope["failed_job_ids"] = sorted([*scope["failed_job_ids"], third])
        scope["execute_job_ids"] = sorted([*scope["execute_job_ids"], third])
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "failed=2"):
            codex_upgrade._official_compaction_lite_recovery_tool_impact(
                manifest,
                current,
                source_attempt=source_attempt,
                recovery_scope=scope,
            )

    def test_execute_reuse_overlap_or_gap_is_rejected(self) -> None:
        for mode in ("overlap", "gap"):
            with self.subTest(mode=mode):
                manifest, current, source_attempt, scope = self._fixture()
                if mode == "overlap":
                    scope["completed_job_ids"] = sorted(
                        [*scope["completed_job_ids"], scope["execute_job_ids"][0]]
                    )
                else:
                    scope["completed_job_ids"] = scope["completed_job_ids"][1:]
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "failed=2",
                ):
                    codex_upgrade._official_compaction_lite_recovery_tool_impact(
                        manifest,
                        current,
                        source_attempt=source_attempt,
                        recovery_scope=scope,
                    )

    def test_new_builder_is_not_an_unmapped_production_file(self) -> None:
        manifest, current, _source_attempt, _scope = self._fixture()
        jobs = [
            codex_upgrade.Job(
                job_id=job_id,
                phase="official",
                suites=("full",),
                description=job_id,
                steps=(),
                evidence_roots=(f"/tmp/{job_id}",),
                covers=(),
            )
            for job_id in sorted(
                codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS
            )
        ]
        impact = codex_upgrade._cheap_capture_tool_impact(manifest, jobs, current)
        self.assertEqual(impact["unmapped_production_paths"], [])
        self.assertEqual(
            impact["affected_job_ids"],
            sorted(codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS),
        )

    def test_successor_attempt_replays_bound_handoff_for_later_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            source_root = campaign_dir / "official" / "attempts" / "source-attempt"
            successor_root = (
                campaign_dir / "official" / "attempts" / "successor-attempt"
            )
            handoff_root = source_root / "recovery-execution-handoffs"
            handoff_root.mkdir(parents=True)
            successor_root.mkdir(parents=True)
            source_path = source_root / "attempt.json"
            source_path.write_text("{}\n", encoding="utf-8")

            manifest, current, source_attempt, scope = self._fixture()
            manifest["official_identity"] = {"version": "0.154.0"}
            source_attempt.update(
                {
                    "attempt_id": "source-attempt",
                    "attempt_digest": "a" * 64,
                }
            )
            impact = codex_upgrade._official_compaction_lite_recovery_tool_impact(
                manifest,
                current,
                source_attempt=source_attempt,
                recovery_scope=scope,
            )
            assert impact is not None
            execute = list(scope["execute_job_ids"])
            reuse = list(scope["completed_job_ids"])
            reused_results = [
                {
                    "id": job_id,
                    "status": "complete",
                    "disposition": "reused",
                    "source_receipt": {
                        "path": "official/attempts/source-attempt/attempt.json",
                        "sha256": "b" * 64,
                        "bytes": 2,
                    },
                }
                for job_id in reuse
            ]
            executed_results = [
                {"id": job_id, "status": "complete", "disposition": "executed"}
                for job_id in execute
            ]
            parent = {"batch_sequence": 2, "run_dir": "/trusted/preview"}
            core = {
                "schema_version": codex_upgrade.RECOVERY_EXECUTION_HANDOFF_SCHEMA,
                "issued_at_utc": "2026-09-15T03:00:00+00:00",
                "campaign_id": manifest["campaign_id"],
                "source_attempt": {
                    "path": str(source_path.relative_to(campaign_dir)),
                    "sha256": codex_upgrade.file_sha256(source_path),
                    "attempt_id": "source-attempt",
                    "attempt_digest": "a" * 64,
                },
                "parent_preview": parent,
                "official_identity_sha256": codex_upgrade._fingerprint(
                    manifest["official_identity"]
                ),
                "tool_files_sha256": current["files_sha256"],
                "tool_impact": impact,
                "recovery_scope": scope,
                "planned_job_ids": list(scope["planned_job_ids"]),
                "execute_job_ids": execute,
                "reuse_job_ids": reuse,
                "reused_results": reused_results,
                "zero_request_boundary": {
                    "reservation_exists": False,
                    "live_request_count": 0,
                    "scanned_bytes": 0,
                },
            }
            handoff = {**core, "handoff_sha256": codex_upgrade._fingerprint(core)}
            handoff_path = handoff_root / "preview.json"
            handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
            binding = {
                "path": str(handoff_path.relative_to(campaign_dir)),
                "sha256": codex_upgrade.file_sha256(handoff_path),
            }
            successor_attempt = {
                "campaign_id": manifest["campaign_id"],
                "phase": "official",
                "candidate_id": None,
                "status": "awaiting_receipts",
                "recovery_execution_handoff": binding,
                "incremental_plan": {
                    "planned_job_ids": list(scope["planned_job_ids"]),
                    "affected_job_ids": execute,
                    "reused_job_ids": reuse,
                    "executed_job_ids": execute,
                    "failed_job_ids": [],
                    "pending_job_ids": [],
                },
                "results": [*reused_results, *executed_results],
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=scope,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_recovery_execution_handoff_parent_terminal",
                ),
            ):
                replayed = (
                    codex_upgrade._replay_bound_official_compaction_recovery_handoff(
                        campaign_dir,
                        manifest,
                        current,
                        successor_root=successor_root,
                        successor_attempt=successor_attempt,
                    )
                )
            self.assertEqual(
                replayed["kind"],
                codex_upgrade.BATCHED_FAILED_CAPTURE_RECOVERY_KIND,
            )
            self.assertEqual(replayed["recovery_execution_handoff"], binding)

            drifted = dict(current)
            drifted["files_sha256"] = "f" * 64
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=scope,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "字段或身份非法",
                ),
            ):
                codex_upgrade._replay_bound_official_compaction_recovery_handoff(
                    campaign_dir,
                    manifest,
                    drifted,
                    successor_root=successor_root,
                    successor_attempt=successor_attempt,
                )

            drifted_scope = dict(scope)
            drifted_scope["failed_job_ids"] = []
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=drifted_scope,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "源 attempt 闭集漂移",
                ),
            ):
                codex_upgrade._replay_bound_official_compaction_recovery_handoff(
                    campaign_dir,
                    manifest,
                    current,
                    successor_root=successor_root,
                    successor_attempt=successor_attempt,
                )

    @staticmethod
    def _parent_manifest(
        campaign_dir: Path,
        *,
        preview: bool = True,
    ) -> tuple[dict[str, object], str, list[str], list[str]]:
        action_id = "preview-two-official-jobs" if preview else "run-two-official-jobs"
        execute = sorted(codex_upgrade.OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS)
        reuse = [f"official-pass-{index:02d}" for index in range(27)]
        command = [
            "python3",
            str(Path(codex_upgrade.__file__).resolve()),
            "resume",
            "--campaign-dir",
            str(campaign_dir.resolve()),
            "--rerun-failed",
        ]
        command.append(
            "--preview-recovery" if preview else "--acknowledge-live-requests"
        )
        return (
            {
                "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": "formal-0154",
                "phase": "VC-1",
                "batch_sequence": 2 if preview else 3,
                "no_op": False,
                "execute_items": execute,
                "reuse_items": reuse,
                "actions": [
                    {
                        "action_id": action_id,
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 60,
                        "command": command,
                        "item_ids": execute,
                    }
                ],
            },
            action_id,
            execute,
            reuse,
        )

    def test_parent_requires_v2_preview_without_live_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            parent, action_id, execute, reuse = self._parent_manifest(campaign_dir)
            codex_upgrade._validate_batched_recovery_execution_parent(
                parent,
                campaign_dir=campaign_dir,
                campaign_id="formal-0154",
                action_id=action_id,
                execute_job_ids=execute,
                reuse_job_ids=reuse,
                preview=True,
                expected_sequence=2,
            )
            mutations = (
                lambda value: value.update({"schema_version": "legacy"}),
                lambda value: value["actions"][0]["command"].remove(
                    "--preview-recovery"
                ),
                lambda value: value["actions"][0]["command"].append(
                    "--acknowledge-live-requests"
                ),
            )
            for index, mutate in enumerate(mutations):
                with self.subTest(index=index):
                    candidate = json.loads(json.dumps(parent))
                    mutate(candidate)
                    with self.assertRaises(codex_upgrade.ConfigurationError):
                        codex_upgrade._validate_batched_recovery_execution_parent(
                            candidate,
                            campaign_dir=campaign_dir,
                            campaign_id="formal-0154",
                            action_id=action_id,
                            execute_job_ids=execute,
                            reuse_job_ids=reuse,
                            preview=True,
                            expected_sequence=2,
                        )

    def test_handoff_parent_must_finish_queue_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve() / "run"
            run_dir.mkdir(mode=0o700)
            manifest = {
                "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "batch_sequence": 2,
            }
            manifest_sha256 = codex_upgrade_supervisor._sha256(
                codex_upgrade_supervisor._canonical(manifest)
            )
            record = {"manifest_sha256": manifest_sha256, "manifest": manifest}
            record_path = run_dir / "campaign-run-manifest.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            # 改造 5：读点统一走 read_stop_receipt，夹具须是自摘要闭合的 v2 收据（终态不是 queue-complete）。
            codex_upgrade_supervisor._stop_receipt(
                run_dir,
                event_type="stopped",
                reason="action-failed",
                detected_at_epoch=1_700_000_000.0,
                owner_pid=1,
                owner_nonce="a" * 64,
                campaign_id="campaign-handoff",
                phase="VC-1",
            )
            parent = {
                "run_dir": str(run_dir),
                "owner_nonce": "a" * 64,
                "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "batch_sequence": 2,
                "manifest_record_sha256": codex_upgrade.file_sha256(record_path),
                "manifest_sha256": manifest_sha256,
            }
            with mock.patch.object(
                codex_upgrade_supervisor,
                "_read_state",
                return_value={"state": "stopped", "owner_nonce": "a" * 64},
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "queue-complete",
                ):
                    codex_upgrade._validate_recovery_execution_handoff_parent_terminal(
                        parent
                    )


if __name__ == "__main__":
    unittest.main()
