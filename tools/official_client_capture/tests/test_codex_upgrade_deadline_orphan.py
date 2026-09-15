"""Codex VC-1 deadline 孤儿历史只读 loader 测试。

B5（2026-09-16）：直接封口的执行分支已删除；本文件只覆盖只读 loader 对已封存历史
孤儿 attempt 的结构校验，不再冻结任何一次事故的固定 Job／请求数量。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import incremental_recovery


class DeadlineOrphanHistoricalLoaderTests(unittest.TestCase):
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

    def test_job_closure_is_structurally_consistent_without_fixed_counts(self) -> None:
        payload, planned = self._attempt_validation_payload()
        codex_upgrade._validate_attempt_incremental_fields(payload, planned)
        plan = payload["incremental_plan"]
        self.assertEqual(
            set(plan["affected_job_ids"]),
            set(plan["executed_job_ids"]) | set(plan["pending_job_ids"]),
        )
        self.assertEqual(
            payload["deadline_orphan_finalization"]["request_accounting"]["total_live_request_count"],
            26 + 38,
        )
        # affected 缺一项即闭集不自洽。
        invalid = json.loads(json.dumps(payload))
        invalid_plan = invalid["incremental_plan"]
        invalid_plan["affected_job_ids"] = invalid_plan["affected_job_ids"][:-1]
        unsigned = dict(invalid_plan)
        unsigned.pop("plan_sha256")
        invalid_plan["plan_sha256"] = incremental_recovery.digest(unsigned)
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "闭集"):
            codex_upgrade._validate_attempt_incremental_fields(invalid, planned)

    def test_probe_root_replay_requires_complete_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "before"
            root.mkdir(mode=0o700)
            manifest = self._write_probe(root, "before")
            replayed = codex_upgrade._deadline_orphan_validate_probe_root(root, phase="before")
            self.assertEqual(replayed, manifest)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "身份或快照数量非法"):
                codex_upgrade._deadline_orphan_validate_probe_root(root, phase="after")
            first = root / next(iter(codex_upgrade.ENVIRONMENT_STATE_FILES.values()))
            first.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "摘要漂移"):
                codex_upgrade._deadline_orphan_validate_probe_root(root, phase="before")

    def test_finalizer_command_is_retired(self) -> None:
        """执行分支已删除：CLI 不再接受 finalize-vc1-deadline-orphan，也没有对应函数。"""

        parser = codex_upgrade._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["finalize-vc1-deadline-orphan", "--campaign-dir", "/tmp/x"])
        self.assertFalse(hasattr(codex_upgrade, "finalize_vc1_deadline_orphan"))
        self.assertFalse(hasattr(codex_upgrade, "DEADLINE_ORPHAN_EXPECTED_JOB_COUNTS"))

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


if __name__ == "__main__":
    unittest.main()
