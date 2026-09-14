"""Codex VC-1 deadline 孤儿直接封口测试。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import incremental_recovery


class DeadlineOrphanFinalizerTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _attempt_validation_payload(self) -> tuple[dict[str, object], set[str]]:
        planned = [f"job-{index:02d}" for index in range(29)]
        reused = planned[:2]
        executed = planned[2:17]
        pending = planned[17:]
        affected = sorted([*executed, *pending])
        plan_core = {
            "schema_version": incremental_recovery.SCHEMA_VERSION,
            "planned_job_ids": planned,
            "changed_components": ["producer"],
            "affected_job_ids": affected,
            "reused_job_ids": reused,
            "executed_job_ids": executed,
            "failed_job_ids": [],
            "pending_job_ids": pending,
        }
        binding = {
            "path": "official/source/interrupted-recovery-transition.json",
            "sha256": "1" * 64,
        }
        file_binding = {**binding, "bytes": 1}
        payload: dict[str, object] = {
            "phase": "official",
            "candidate_id": None,
            "status": "failed",
            "continuity": {"source_attempt_id": "source-attempt"},
            "incremental_tool_transition": None,
            "evaluation_transition": None,
            "interrupted_recovery_transition": binding,
            "deadline_orphan_finalization": {
                "contract": binding,
                "live_request_audit": binding,
                "request_accounting": {
                    "historical_live_request_count": 26,
                    "delta_live_request_count": 38,
                    "total_live_request_count": 64,
                    "finalizer_live_request_count": 0,
                    "failed_attempt_archives_enumerated": False,
                },
                "supervisor_terminal": {
                    "state": "watchdog-aborted",
                    "reason": "global-wall-clock-deadline-expired",
                    "batch_sequence": 5,
                },
            },
            "incremental_plan": {
                **plan_core,
                "plan_sha256": incremental_recovery.digest(plan_core),
            },
            "results": [
                {
                    "id": job_id,
                    "status": "complete",
                    "disposition": (
                        "reused" if job_id in reused else "executed"
                    ),
                    **(
                        {
                            "source_receipt": {
                                "path": "official/source/attempt.json",
                                "sha256": "2" * 64,
                                "bytes": 1,
                            }
                        }
                        if job_id in reused
                        else {}
                    ),
                }
                for job_id in [*reused, *executed]
            ],
            "environment": {
                "evidence_root": "/tmp/evidence",
                "before_probe": file_binding,
                "after_probe": file_binding,
                "restoration_report": file_binding,
                "arm64_before_receipt": file_binding,
                "arm64_after_receipt": file_binding,
            },
            "execution_error": {
                "type": "CampaignDeadlineExpired",
                "message": "deadline",
            },
            "restoration_error": None,
            "watchdog": {
                "schema_version": codex_upgrade.WATCHDOG_HEARTBEAT_SCHEMA,
                "budget_seconds": 100.0,
                "heartbeat_seconds": 5.0,
                "elapsed_seconds": 100.0,
                "remaining_seconds": 0.0,
                "heartbeat": file_binding,
                "timeout_checkpoint": file_binding,
                "last_completed_job_id": executed[-1],
            },
            "job_checkpoint": {
                "schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                "campaign_id": "campaign",
                "phase": "official",
                "attempt_id": "attempt",
                "run_nonce": "3" * 64,
                "path": "official/checkpoints",
                "record_count": 17,
                "last_sequence": 17,
                "last_sha256": "4" * 64,
            },
        }
        return payload, set(planned)

    def test_fixed_job_and_request_closure_is_enforced(self) -> None:
        payload, planned = self._attempt_validation_payload()
        codex_upgrade._validate_attempt_incremental_fields(payload, planned)
        plan = payload["incremental_plan"]
        self.assertEqual(len(plan["planned_job_ids"]), 29)
        self.assertEqual(len(plan["affected_job_ids"]), 27)
        self.assertEqual(len(plan["reused_job_ids"]), 2)
        self.assertEqual(len(plan["executed_job_ids"]), 15)
        self.assertEqual(len(plan["failed_job_ids"]), 0)
        self.assertEqual(len(plan["pending_job_ids"]), 12)
        self.assertEqual(
            payload["deadline_orphan_finalization"]["request_accounting"],
            {
                "historical_live_request_count": 26,
                "delta_live_request_count": 38,
                "total_live_request_count": 64,
                "finalizer_live_request_count": 0,
                "failed_attempt_archives_enumerated": False,
            },
        )

        invalid = json.loads(json.dumps(payload))
        invalid_plan = invalid["incremental_plan"]
        invalid_plan["affected_job_ids"] = invalid_plan["affected_job_ids"][:-1]
        unsigned = dict(invalid_plan)
        unsigned.pop("plan_sha256")
        invalid_plan["plan_sha256"] = incremental_recovery.digest(unsigned)
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "29/27/2/15/0/12|闭集",
        ):
            codex_upgrade._validate_attempt_incremental_fields(invalid, planned)

    def test_wrong_request_counts_fail_before_campaign_access(self) -> None:
        arguments = argparse.Namespace(
            expected_historical_live_requests=26,
            expected_delta_live_requests=37,
            expected_total_live_requests=63,
        )
        with (
            mock.patch.object(codex_upgrade, "load_campaign_manifest") as load,
            self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                r"26 \+ 38 = 64",
            ),
        ):
            codex_upgrade.finalize_vc1_deadline_orphan(arguments)
        load.assert_not_called()

    def _write_probe(self, root: Path, phase: str) -> dict[str, object]:
        snapshots = []
        for kind, name in codex_upgrade.ENVIRONMENT_STATE_FILES.items():
            path = root / name
            path.write_bytes((kind + "\n").encode("utf-8"))
            path.chmod(0o600)
            snapshots.append(
                {
                    "bytes": path.stat().st_size,
                    "comparison": {"mode": "byte_equal"},
                    "kind": kind,
                    "path": name,
                    "sha256": codex_upgrade.file_sha256(path),
                }
            )
        manifest = {
            "schema_version": (
                codex_upgrade.codex_upgrade_environment_probe.PROBE_MANIFEST_SCHEMA
            ),
            "phase": phase,
            "observed_at_utc": "2026-09-14T06:00:00Z",
            "snapshots": snapshots,
        }
        self._write_json(root / "probe-manifest.json", manifest)
        return manifest

    def test_after_publish_recovers_idempotently_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            attempt = root / "attempt"
            (attempt / "evidence" / "environment").mkdir(
                parents=True,
                mode=0o700,
            )
            source = {"attempt_root": attempt}
            deadline = incremental_recovery.WallClockDeadline(60)

            def probe(_manifest, output, phase, **_kwargs):
                return self._write_probe(output, phase)

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                    side_effect=probe,
                ) as invoke,
                mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_fsync_directory",
                    side_effect=RuntimeError("模拟 rename 后中断"),
                ),
                self.assertRaisesRegex(RuntimeError, "模拟 rename 后中断"),
            ):
                codex_upgrade._deadline_orphan_after_probe(
                    {}, source, deadline
                )
            self.assertTrue(
                (attempt / "evidence" / "environment" / "after").is_dir()
            )
            path, replayed = codex_upgrade._deadline_orphan_after_probe(
                {}, source, deadline
            )
            self.assertEqual(replayed["phase"], "after")
            self.assertEqual(path.name, "probe-manifest.json")
            self.assertEqual(invoke.call_count, 1)

    def _expired_ledger(self, root: Path) -> tuple[Path, dict[str, object]]:
        ledger = root / "ledger"
        started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
            minutes=2
        )
        timing.create_ledger(
            ledger,
            upgrade_id="upgrade-0154",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
            started_at_utc=started.isoformat(),
            total_budget_minutes=1,
        )
        events = (
            ("p0-receipts", "VC-0", "receipt_passed", None, None, 0, None),
            ("vc0-complete", "VC-0", "stage_completed", None, None, 0, None),
            ("vc1-start", "VC-1", "stage_started", None, None, 0, None),
            (
                "vc1-abandoned",
                "VC-1",
                "stage_abandoned",
                None,
                "vc0-closeout-fixture",
                0,
                "保留不可变现场并从最后合法 checkpoint 恢复。",
            ),
            ("request-accounting", "VC-1", "receipt_passed", None, None, 26, None),
        )
        for index, (
            event_id,
            phase,
            event_type,
            attempt_id,
            root_cause_id,
            count,
            next_action,
        ) in enumerate(
            events,
            1,
        ):
            timing.append_event(
                ledger,
                event_id=event_id,
                phase=phase,
                event_type=event_type,
                attempt_id=attempt_id,
                root_cause_id=root_cause_id,
                live_request_count=count,
                next_action=next_action,
                recorded_at_utc=(started + timedelta(seconds=index)).isoformat(),
            )
        prefix = timing.inspect_ledger(ledger, limit=6)
        self.assertIsNone(prefix["active_phase"])
        return ledger, {
            "ledger_dir": str(ledger),
            "prefix_head_sha256": prefix["head_sha256"],
            "active_phase": None,
        }

    def test_sequence_seven_stop_is_idempotent_and_totals_64(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            ledger, binding = self._expired_ledger(root)
            receipt_path = self._write_json(
                ledger / "receipts" / "accounting.json",
                {"count": 38},
            )
            receipts = [
                {
                    "role": "live_request_accounting",
                    "path": "receipts/accounting.json",
                    "sha256": codex_upgrade.file_sha256(receipt_path),
                }
            ]
            finalized_at = datetime.now(timezone.utc).isoformat(
                timespec="microseconds"
            )
            finalizer = {
                "finalized_at_utc": finalized_at,
                "ledger_stop_event": {
                    "event_id": "deadline-orphan-stop-fixture",
                    "attempt_id": "attempt-1",
                    "live_request_count": 38,
                    "next_action": "当前 Campaign 永久停线。",
                },
            }
            first = codex_upgrade._deadline_orphan_stop_ledger(
                binding,
                finalizer,
                receipts,
                expected_total_live_requests=64,
            )
            second = codex_upgrade._deadline_orphan_stop_ledger(
                binding,
                finalizer,
                receipts,
                expected_total_live_requests=64,
            )
            self.assertEqual(first["summary"]["head_sequence"], 7)
            self.assertEqual(second["summary"]["head_sequence"], 7)
            self.assertEqual(second["summary"]["total_live_request_count"], 64)
            self.assertIsNone(second["summary"]["active_phase"])
            self.assertEqual(len(list((ledger / "events").iterdir())), 7)

    def test_wrong_ledger_head_and_deployment_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            ledger = root / "ledger"
            timing.create_ledger(
                ledger,
                upgrade_id="upgrade-0154",
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose="production_replacement",
                evidence_decision="recapture",
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "sequence 6|无法重放",
            ):
                codex_upgrade._deadline_orphan_timing_prefix(
                    ledger,
                    {
                        "baseline_version": "0.151.0",
                        "target_version": "0.154.0",
                        "campaign_purpose": "production_replacement",
                    },
                    {"original_deadline_at_utc": "2026-09-14T04:56:55Z"},
                    expected_historical_live_requests=26,
                )
            malformed = self._write_json(root / "deploy.json", {})
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "部署收据",
            ):
                codex_upgrade._deadline_orphan_deployment_snapshot(malformed)

    def test_wrong_supervisor_terminal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            campaign = root / "campaign"
            run = root / "run"
            run.mkdir(mode=0o700)
            owner_nonce = "1" * 64
            deadline = "2026-09-14T04:56:55Z"
            deadline_epoch = datetime.fromisoformat(
                deadline.replace("Z", "+00:00")
            ).timestamp()
            run_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": "formal-live",
                "phase": "VC-1",
                "batch_sequence": 5,
                "no_op": False,
                "original_deadline_at_utc": deadline,
                "execute_items": ["execute"],
                "reuse_items": ["reuse"],
                "actions": [{"action_id": "capture", "item_ids": ["execute"]}],
            }
            state = {
                "campaign_id": "formal-live",
                "phase": "VC-1",
                "state": "watchdog-aborted",
                "owner_nonce": owner_nonce,
                "owner_pid": 999991,
                "monitor_pid": 999992,
                "deadline_at_epoch": deadline_epoch,
            }
            self._write_json(run / "state.json", state)
            self._write_json(
                run / "campaign-run-manifest.json",
                {
                    "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    "manifest": run_manifest,
                    "manifest_sha256": supervisor._sha256(
                        supervisor._canonical(run_manifest)
                    ),
                },
            )
            detected = "2026-09-14T04:56:55Z"
            stop_core = {
                "schema_version": supervisor.STOP_SCHEMA,
                "event_type": "watchdog-aborted",
                "reason": "错误的终态根因",
                "detected_at_utc": detected,
                "detected_at_epoch": deadline_epoch,
                "owner_pid": state["owner_pid"],
                "owner_nonce": owner_nonce,
                "campaign_id": "formal-live",
                "phase": "VC-1",
            }
            self._write_json(
                run / "stop-receipt.json",
                {
                    **stop_core,
                    "receipt_sha256": supervisor._sha256(
                        supervisor._canonical(stop_core)
                    ),
                },
            )
            source = {
                "reservation": {
                    "campaign_lease": {
                        "owner_nonce": owner_nonce,
                        "deadline_at_utc": deadline,
                    }
                },
                "planned_job_ids": ["execute", "reuse"],
                "affected_job_ids": ["execute"],
                "reused_job_ids": ["reuse"],
            }
            with (
                mock.patch.object(supervisor, "_read_state", return_value=state),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_manifest",
                    return_value=dict(run_manifest),
                ),
                mock.patch.object(
                    supervisor,
                    "_audit_command",
                    return_value={
                        "state": "watchdog-aborted",
                        "audit_incomplete": True,
                        "integrity_errors": [],
                    },
                ),
                mock.patch.object(supervisor, "_owner_alive", return_value=False),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "supervisor|deadline",
                ),
            ):
                codex_upgrade._deadline_orphan_supervisor_snapshot(
                    campaign,
                    {"campaign_id": "formal-live"},
                    run,
                    source,
                )

    def test_wrong_interrupted_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            campaign.chmod(0o700)
            prior_root = (
                campaign
                / "official"
                / "attempts"
                / "20260913T232116Z-bbbbbbbbbbbbbbbb"
            )
            prior_root.mkdir(parents=True, mode=0o700)
            prior_attempt_path = self._write_json(
                prior_root / "attempt.json",
                {"prior": True},
            )
            source_receipt = {
                "path": str(prior_attempt_path.relative_to(campaign)),
                "sha256": codex_upgrade.file_sha256(prior_attempt_path),
                "bytes": prior_attempt_path.stat().st_size,
            }
            contract_binding = {
                "path": "control/vc/recovery-contracts/0002-vc-1.json",
                "sha256": "2" * 64,
            }
            prior_attempt = {
                "attempt_id": prior_root.name,
                "attempt_digest": "3" * 64,
                "interrupted_recovery": {"contract": contract_binding},
            }
            transition_core = {
                "schema_version": codex_upgrade.INTERRUPTED_RECOVERY_TRANSITION_SCHEMA,
                "issued_at_utc": "2026-09-14T03:00:00Z",
                "campaign_id": "formal-live",
                "attempt_id": prior_root.name,
                "attempt_digest": prior_attempt["attempt_digest"],
                "contract": contract_binding,
                "from_tool_files_sha256": "4" * 64,
                "to_tool_files_sha256": "5" * 64,
                "changed_files": [],
                "allowed_production_paths": [],
                "affected_job_ids": ["wrong-job"],
                "recovery_scope": {
                    "planned_job_ids": ["execute", "reuse-a", "reuse-b"],
                    "completed_job_ids": ["reuse-a", "reuse-b"],
                    "execute_job_ids": ["execute"],
                },
                "allowed_operations": ["capture-run"],
                "zero_request_boundary": {
                    "reservation_exists": False,
                    "live_request_count": 0,
                    "scanned_bytes": 0,
                },
            }
            self._write_json(
                prior_root / codex_upgrade.INTERRUPTED_RECOVERY_TRANSITION_FILENAME,
                {
                    **transition_core,
                    "transition_sha256": codex_upgrade._fingerprint(
                        transition_core
                    ),
                },
            )
            source = {
                "results": [
                    {
                        "id": job_id,
                        "disposition": "reused",
                        "carried_from_attempt": prior_root.name,
                        "source_receipt": source_receipt,
                    }
                    for job_id in ("reuse-a", "reuse-b")
                ],
                "planned_job_ids": ["execute", "reuse-a", "reuse-b"],
                "affected_job_ids": ["execute"],
                "reused_job_ids": ["reuse-a", "reuse-b"],
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(prior_root, prior_attempt),
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "transition|闭集",
                ),
            ):
                codex_upgrade._deadline_orphan_execution_transition(
                    campaign,
                    {"campaign_id": "formal-live"},
                    source,
                    {"execute_job_ids": ["execute"]},
                )

    def test_deadline_audit_patterns_accept_frozen_and_host_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            campaign = data / "evidence" / "campaigns" / "formal-live"
            campaign.mkdir(parents=True)
            patterns = closeout._deadline_orphan_job_root_patterns(
                campaign,
                {
                    "configuration": {"capture_root": "/root/oauth-capture"},
                    "jobs": [
                        {
                            "id": "official-compact",
                            "phase": "official",
                            "evidence_roots": [
                                "/root/oauth-capture/runs/official-compact"
                            ],
                        }
                    ],
                },
            )
            self.assertEqual(
                patterns["official-compact"],
                (
                    "/root/oauth-capture/runs/official-compact",
                    (data.resolve() / "runs" / "official-compact").as_posix(),
                ),
            )

    def test_deadline_audit_ignores_failed_attempt_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            data = root / "data"
            campaign = data / "evidence" / "campaigns" / "formal-live"
            attempt = (
                campaign
                / "official"
                / "attempts"
                / "20260914T045007Z-aaaaaaaaaaaaaaaa"
            )
            attempt.mkdir(parents=True, mode=0o700)
            data.chmod(0o700)
            runs = data / "runs"
            base = runs / "executed"
            failed = runs / "executed.failed-attempt1"
            self._write_json(
                base / "manifest.json",
                {
                    "schema_version": "official-client-capture/v1",
                    "case_results": [{"scenario_result": {"turn_count": 1}}],
                },
            )
            self._write_json(
                failed / "manifest.json",
                {
                    "schema_version": "official-client-capture/v1",
                    "case_results": [{"scenario_result": {"turn_count": 99}}],
                },
            )
            jobs = [
                {
                    "id": job_id,
                    "phase": "official",
                    "evidence_roots": [f"/root/oauth-capture/runs/{job_id}"],
                }
                for job_id in ("executed", "reused", "pending")
            ]
            campaign_path = self._write_json(
                campaign / "campaign.json",
                {
                    "campaign_id": "formal-live",
                    "campaign_mode": "formal",
                    "configuration": {"capture_root": "/root/oauth-capture"},
                    "jobs": jobs,
                },
            )
            (campaign / "campaign.sha256").write_text(
                closeout._sha256_file(campaign_path) + "\n",
                encoding="ascii",
            )
            (campaign / "campaign.sha256").chmod(0o600)
            historical_source = self._write_json(
                root / "historical-source.json",
                {"historical": True},
            )
            historical = self._write_json(
                root / "historical-audit.json",
                {
                    "schema_version": closeout.LIVE_REQUEST_AUDIT_SCHEMA,
                    "status": "complete",
                    "campaign_id": "formal-live",
                    "observed_at_utc": "2026-09-14T04:40:00Z",
                    "counting_rule": "codex_model_turns_and_responses_requests/v1",
                    "live_request_count": 26,
                    "observed_job_ids": ["historical"],
                    "pending_job_ids": [],
                    "pre_request_zero_job_ids": [],
                    "sources": [
                        {
                            "kind": "historical",
                            "path": str(historical_source.resolve()),
                            "sha256": closeout._sha256_file(historical_source),
                            "live_request_count": 26,
                        }
                    ],
                },
            )
            reservation = {
                "planned_jobs": [{"id": row["id"]} for row in jobs],
                "run_nonce": "1" * 64,
            }
            records = [
                {
                    "result": {
                        "id": "reused",
                        "status": "complete",
                        "disposition": "reused",
                        "evidence_roots": [str((runs / "reused").resolve())],
                    },
                    "checkpoint_sequence": 1,
                    "checkpoint_sha256": "2" * 64,
                },
                {
                    "result": {
                        "id": "executed",
                        "status": "complete",
                        "disposition": "executed",
                        "evidence_roots": [str(base.resolve())],
                    },
                    "checkpoint_sequence": 2,
                    "checkpoint_sha256": "3" * 64,
                },
            ]
            store = mock.Mock()
            store.records.return_value = records
            with (
                mock.patch.object(
                    closeout.codex_upgrade,
                    "_load_capture_reservation",
                    return_value=reservation,
                ),
                mock.patch.object(
                    closeout.codex_upgrade.incremental_recovery,
                    "CheckpointStore",
                    return_value=store,
                ),
                mock.patch.object(
                    closeout.codex_upgrade,
                    "_validate_checkpoint_records",
                ),
            ):
                audit = closeout.build_deadline_orphan_live_request_audit(
                    campaign,
                    source_attempt=attempt,
                    historical_live_request_audit=historical,
                    expected_historical_live_requests=26,
                    expected_delta_live_requests=1,
                    expected_total_live_requests=27,
                    expected_executed_job_ids={"executed"},
                    expected_reused_job_ids={"reused"},
                    expected_pending_job_ids={"pending"},
                )
            self.assertEqual(audit["delta_live_request_count"], 1)
            self.assertFalse(audit["failed_attempt_archives_enumerated"])
            self.assertTrue(
                all(".failed-attempt" not in row["path"] for row in audit["sources"])
            )

    def test_direct_command_has_no_campaign_lease(self) -> None:
        parser = codex_upgrade._build_parser()
        arguments = parser.parse_args(
            [
                "finalize-vc1-deadline-orphan",
                "--campaign-dir",
                "/tmp/campaign",
                "--source-attempt",
                "/tmp/campaign/official/attempts/attempt",
                "--supervisor-run-dir",
                "/tmp/supervisor",
                "--timing-ledger-dir",
                "/tmp/ledger",
                "--historical-live-request-audit",
                "/tmp/historical.json",
                "--deployment-receipt",
                "/tmp/deploy.json",
                "--expected-historical-live-requests",
                "26",
                "--expected-delta-live-requests",
                "38",
                "--expected-total-live-requests",
                "64",
                "--max-finalization-seconds",
                "900",
            ]
        )
        self.assertIsNone(codex_upgrade._mutable_command_coordinates(arguments))
        with mock.patch.object(
            codex_upgrade,
            "_campaign_operation_lease",
        ) as lease:
            with codex_upgrade._main_command_lease(arguments) as active:
                self.assertIsNone(active)
        lease.assert_not_called()

    def test_finalizer_orchestration_never_reserves_or_runs_jobs(self) -> None:
        """主流程只能调用收口原语，不能落入普通 capture 数据面。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            campaign = root / "campaign"
            attempt = (
                campaign
                / "official"
                / "attempts"
                / "20260914T045007Z-aaaaaaaaaaaaaaaa"
            )
            attempt.mkdir(parents=True, mode=0o700)
            historical = self._write_json(root / "historical.json", {"ok": True})
            deployment_path = self._write_json(root / "deployment.json", {"ok": True})
            manifest = {
                "campaign_id": "formal-live",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
            }
            source = {
                "attempt_root": attempt,
                "heartbeat": {"last_completed_job_id": "job-16"},
                "arm64_before": {"continuity_identity_sha256": "1" * 64},
                "before_manifest": {"phase": "before"},
                "results": [
                    {
                        "disposition": "reused",
                        "carried_from_attempt": "source-attempt",
                    }
                ],
                "reservation": {"campaign_id": "formal-live"},
            }
            supervisor_snapshot = {
                "terminal": {
                    "state": "watchdog-aborted",
                    "reason": "global-wall-clock-deadline-expired",
                    "batch_sequence": 5,
                    "detected_at_utc": "2026-09-14T04:56:55Z",
                    "owner_nonce": "2" * 64,
                }
            }
            transition = {"execution_tool_files_sha256": "3" * 64}
            deployment = {
                "binding": {
                    "path": str(deployment_path),
                    "sha256": "4" * 64,
                    "bytes": 1,
                },
                "campaign_id": "deploy",
                "tool_files_sha256": "3" * 64,
                "supervisor_run_dir": str(root / "deploy-run"),
            }
            timing_binding = {
                "ledger_dir": str(root / "ledger"),
                "prefix_head_sha256": "5" * 64,
            }
            contract = {"contract_sha256": "6" * 64}
            audit = {
                "historical_live_request_count": 26,
                "delta_live_request_count": 38,
                "total_live_request_count": 64,
            }
            attempt_receipt = {"attempt_digest": "7" * 64}
            finalizer_receipt = {
                "finalized_at_utc": "2026-09-14T06:00:00Z",
                "ledger_stop_event": {},
            }
            ledger_result = {
                "summary": {
                    "head_sequence": 7,
                    "head_sha256": "8" * 64,
                }
            }
            deadline = mock.Mock()
            arguments = argparse.Namespace(
                campaign_dir=campaign,
                source_attempt=attempt,
                supervisor_run_dir=root / "supervisor",
                timing_ledger_dir=root / "ledger",
                historical_live_request_audit=historical,
                deployment_receipt=deployment_path,
                expected_historical_live_requests=26,
                expected_delta_live_requests=38,
                expected_total_live_requests=64,
                max_finalization_seconds=900,
            )
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_source_checkpoint",
                    return_value=source,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_supervisor_snapshot",
                    return_value=supervisor_snapshot,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_execution_transition",
                    return_value=transition,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_deployment_snapshot",
                    return_value=deployment,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_external_binding",
                    return_value={
                        "path": str(historical),
                        "sha256": "9" * 64,
                        "bytes": historical.stat().st_size,
                    },
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_timing_prefix",
                    return_value=timing_binding,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_contract_core",
                    return_value={
                        "campaign_id": "formal-live",
                        "attempt_id": attempt.name,
                    },
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_contract",
                    return_value=contract,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_finalization_deadline",
                    return_value=deadline,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_live_request_audit",
                    return_value=audit,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_timeout_checkpoint",
                    return_value=(attempt / "timeout-checkpoint.json", {}),
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_after_probe",
                    return_value=(attempt / "after" / "probe-manifest.json", {}),
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_arm64_after",
                    return_value=(
                        attempt / "arm64-after" / "receipt.json",
                        {"continuity_identity_sha256": "1" * 64},
                    ),
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_restoration",
                    return_value=(attempt / "restoration-report.json", {}),
                ))
                continuity = stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_verify_environment_continuity",
                    return_value={"schema_version": "continuity"},
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_attempt_core",
                    return_value={},
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_write_or_replay_attempt",
                    return_value=attempt_receipt,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_finalizer_receipt",
                    return_value=(attempt / "finalizer.json", finalizer_receipt),
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_ledger_receipts",
                    return_value=[],
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_deadline_orphan_stop_ledger",
                    return_value=ledger_result,
                ))
                stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt, attempt_receipt),
                ))
                reserve = stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_reserve_capture_attempt",
                ))
                run_job = stack.enter_context(mock.patch.object(
                    codex_upgrade,
                    "_run_job_with_retry",
                ))
                result = codex_upgrade.finalize_vc1_deadline_orphan(arguments)
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["total_live_request_count"], 64)
            self.assertFalse(result["new_reservation_created"])
            self.assertFalse(result["jobs_reexecuted"])
            self.assertFalse(result["model_requests_sent"])
            self.assertFalse(result["vc2_entered"])
            continuity.assert_called_once()
            reserve.assert_not_called()
            run_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
