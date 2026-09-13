"""Codex 升级 attempt watchdog 与 Job checkpoint 的边界测试。"""

from __future__ import annotations

import argparse
import tempfile
import unittest
import json
import copy
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import incremental_recovery


class WatchdogTests(unittest.TestCase):
    @staticmethod
    def _lease_campaign(root: Path) -> Path:
        campaign = root / "lease-campaign"
        campaign.mkdir(mode=0o700)
        manifest = campaign / "campaign.json"
        manifest.write_text("{}\n", encoding="utf-8")
        manifest.chmod(0o600)
        return campaign

    def test_campaign_lease_serializes_owner_and_releases_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = self._lease_campaign(Path(directory))
            deadline = incremental_recovery.WallClockDeadline(30)
            deadline.heartbeat_seconds = 1
            lease = codex_upgrade.CampaignLease(
                campaign,
                phase="official",
                candidate_id=None,
                deadline=deadline,
                command="capture-official",
                campaign_id="lease-campaign",
            )
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value={"campaign_id": "lease-campaign"},
            ):
                lease.acquire()
                try:
                    payload = json.loads(
                        (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(payload["state"], "active")
                    self.assertEqual(payload["owner_pid"], __import__("os").getpid())
                    self.assertEqual(payload["campaign_id"], "lease-campaign")
                    competing = codex_upgrade.CampaignLease(
                        campaign,
                        phase="official",
                        candidate_id=None,
                        deadline=incremental_recovery.WallClockDeadline(30),
                        command="capture-official",
                        campaign_id="lease-campaign",
                    )
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError, "lease 已被占用"
                    ):
                        competing.acquire()
                finally:
                    lease.release()
            released = json.loads(
                (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(released["state"], "released")

    def test_stale_campaign_lease_writes_immutable_stop_and_requires_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = self._lease_campaign(Path(directory))
            old = datetime.now(timezone.utc) - timedelta(minutes=10)
            payload = {
                "schema_version": codex_upgrade.CAMPAIGN_LEASE_SCHEMA,
                "campaign_id": "lease-campaign",
                "phase": "official",
                "candidate_id": None,
                "owner_pid": 999999999,
                "owner_nonce": "a" * 64,
                "attempt_id": None,
                "started_at_utc": (old - timedelta(minutes=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                "deadline_at_utc": (old + timedelta(minutes=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                "last_heartbeat_at_utc": old.isoformat().replace("+00:00", "Z"),
                "heartbeat_seconds": 30,
                "operation": "capture-official",
                "state": "active",
                "recovered_owner_nonce": None,
            }
            (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).write_text(
                json.dumps(payload) + "\n", encoding="utf-8"
            )
            (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).chmod(0o600)
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value={"campaign_id": "lease-campaign"},
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError, "已过期并已停线"
                ):
                    codex_upgrade.CampaignLease(
                        campaign,
                        phase="official",
                        candidate_id=None,
                        deadline=incremental_recovery.WallClockDeadline(30),
                        command="capture-official",
                        campaign_id="lease-campaign",
                    ).acquire()
                stops = sorted(
                    (campaign / codex_upgrade.CAMPAIGN_LEASE_STOP_DIRECTORY).glob("*.json")
                )
                self.assertEqual(len(stops), 1)
                stop_before = stops[0].read_bytes()
                recovering = codex_upgrade.CampaignLease(
                    campaign,
                    phase="official",
                    candidate_id=None,
                    deadline=incremental_recovery.WallClockDeadline(30),
                    command="resume-rerun-failed",
                    allow_stale_recovery=True,
                    campaign_id="lease-campaign",
                )
                with recovering:
                    current = json.loads(
                        (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(current["recovered_owner_nonce"], "a" * 64)
                self.assertEqual(stop_before, stops[0].read_bytes())

    def test_campaign_lease_deadline_creates_stop_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = self._lease_campaign(Path(directory))
            deadline = incremental_recovery.WallClockDeadline(0.08)
            deadline.heartbeat_seconds = 0.02
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value={"campaign_id": "lease-campaign"},
            ):
                lease = codex_upgrade.CampaignLease(
                    campaign,
                    phase="official",
                    candidate_id=None,
                    deadline=deadline,
                    command="capture-official",
                    campaign_id="lease-campaign",
                )
                lease.acquire()
                time_module = __import__("time")
                time_module.sleep(0.15)
                with self.assertRaises(incremental_recovery.WallClockTimeoutError):
                    lease.check("lease-test")
                lease.release()
            self.assertTrue(
                list((campaign / codex_upgrade.CAMPAIGN_LEASE_STOP_DIRECTORY).glob("*.json"))
            )

    def test_active_campaign_deadline_is_rebound_to_official_without_reset(
        self,
    ) -> None:
        """campaign-run 复用父 deadline 时先绑定 official，且不重置时间锚。"""

        class PlannedStop(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            campaign.mkdir(mode=0o700)
            deadline = incremental_recovery.WallClockDeadline(120)
            deadline.phase = "stage"
            started = deadline.started_monotonic
            ending = deadline.deadline_monotonic
            active = mock.Mock()
            active.deadline = deadline
            arguments = argparse.Namespace(campaign_dir=campaign)
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_ACTIVE_CAMPAIGN_LEASE",
                    active,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_lease_identity_matches",
                    return_value=True,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value={"campaign_id": "campaign-a"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_assert_initial_vc1_handoff_window",
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_reject_contaminated_campaign",
                    side_effect=PlannedStop,
                ),
                self.assertRaises(PlannedStop),
            ):
                codex_upgrade._run_capture_attempt(arguments, "official")
            self.assertIs(active.deadline, deadline)
            self.assertEqual(deadline.phase, "official")
            self.assertEqual(deadline.started_monotonic, started)
            self.assertEqual(deadline.deadline_monotonic, ending)

    def test_watchdog_files_keep_independent_low_risk_components(self) -> None:
        expected = {
            "codex_upgrade_arm64_environment_receipt.py": "environment",
            "codex_upgrade_environment_probe.py": "environment",
            "codex_upgrade_timing_ledger.py": "control",
            "codex_upgrade_supervisor.py": "control",
        }
        for path, component in expected.items():
            self.assertEqual(
                codex_upgrade._tool_component_for_path(path), component
            )

    def test_legacy_shared_watchdog_classification_does_not_invalidate_jobs(self) -> None:
        current = codex_upgrade._tool_identity(include_git=False)
        legacy = copy.deepcopy(current)
        # 0.149.1 Campaign 将这些接线文件记在 shared；模拟旧组件摘要，
        # 内容保持不变，比较器应把它们规范化到当前独立组件后判定无漂移。
        legacy.pop("component_identity_sha256", None)
        for path in codex_upgrade._WATCHDOG_ONLY_TOOL_FILES:
            moved = False
            for component, value in legacy["components"].items():
                for index, entry in enumerate(value["entries"]):
                    if entry["path"] == path:
                        legacy["components"]["shared"]["entries"].append(
                            value["entries"].pop(index)
                        )
                        moved = True
                        break
                if moved:
                    break
            self.assertTrue(moved, path)
        for value in legacy["components"].values():
            value["entries"].sort(key=lambda item: item["path"])
            value["entry_count"] = len(value["entries"])
            value["sha256"] = incremental_recovery.digest(
                {"entries": value["entries"]}
            )
        legacy.pop("component_identity_sha256", None)
        self.assertEqual(
            codex_upgrade._tool_component_drift(legacy, current)["changed_components"],
            [],
        )
        self.assertEqual(
            codex_upgrade._tool_identity_side_digest(legacy, "production"),
            codex_upgrade._tool_identity_side_digest(current, "production"),
        )

    def test_legacy_shared_evaluator_file_reclassification_does_not_invalidate_jobs(self) -> None:
        current = codex_upgrade._tool_identity(include_git=False)
        legacy = copy.deepcopy(current)
        moved = False
        for component, value in legacy["components"].items():
            for index, entry in enumerate(value["entries"]):
                if entry["path"] == "codex_upgrade_receipt_finalizer.py":
                    legacy["components"]["shared"]["entries"].append(
                        value["entries"].pop(index)
                    )
                    moved = True
                    break
            if moved:
                break
        self.assertTrue(moved)
        for value in legacy["components"].values():
            value["entries"].sort(key=lambda item: item["path"])
            value["entry_count"] = len(value["entries"])
            value["sha256"] = incremental_recovery.digest(
                {"entries": value["entries"]}
            )
        legacy.pop("component_identity_sha256", None)
        drift = codex_upgrade._tool_component_drift(legacy, current)
        self.assertEqual(drift["changed_components"], [])
        self.assertEqual(
            codex_upgrade._tool_component_digest_map(legacy, canonical=True),
            codex_upgrade._tool_component_digest_map(current, canonical=True),
        )

    def test_reused_result_survives_evaluator_reclassification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            attempt_root = campaign / "official" / "attempts" / "A1"
            attempt_root.mkdir(parents=True, mode=0o700)
            receipt = attempt_root / "attempt.json"
            receipt.write_text("source\n", encoding="utf-8")
            receipt.chmod(0o600)

            current = codex_upgrade._tool_identity(include_git=False)
            legacy = copy.deepcopy(current)
            for component, value in legacy["components"].items():
                for index, entry in enumerate(value["entries"]):
                    if entry["path"] == "codex_upgrade_receipt_finalizer.py":
                        legacy["components"]["shared"]["entries"].append(
                            value["entries"].pop(index)
                        )
                        break
                else:
                    continue
                break
            for value in legacy["components"].values():
                value["entries"].sort(key=lambda item: item["path"])
                value["entry_count"] = len(value["entries"])
                value["sha256"] = incremental_recovery.digest(
                    {"entries": value["entries"]}
                )
            legacy.pop("component_identity_sha256", None)

            job = codex_upgrade.Job(
                job_id="job-shared",
                phase="official",
                suites=("full",),
                description="shared dependency",
                steps=({"argv": ["bash", "/tmp/shared-helper.sh"]},),
                evidence_roots=(),
                covers=(),
            )
            identity = {"cli_version": "0.151.0"}
            historical = codex_upgrade._job_incremental_metadata(
                job,
                identity=identity,
                tool_identity=legacy,
            )
            result = {
                "id": job.job_id,
                "phase": "official",
                "status": "complete",
                "execution_sha256": historical["input_sha256"],
                "incremental_result_key": historical["result_key"],
                "tool_components": historical["components"],
                "tool_component_digests": historical["component_digests"],
                "tool_dependency_files": historical["tool_dependency_files"],
                "input_sha256": historical["input_sha256"],
                "environment_sha256": historical["environment_sha256"],
                "dependency_sha256": historical["dependency_sha256"],
            }
            payload = {"status": "failed", "identity": identity, "results": [result]}
            manifest = {"campaign_id": "camp", "tool_identity": legacy}
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_ordered_capture_attempts",
                    return_value=[(attempt_root, {})],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, payload),
                ),
            ):
                reused = codex_upgrade._prior_complete_results(
                    campaign,
                    Path("official"),
                    [job],
                    phase="official",
                    candidate_id=None,
                    identity=identity,
                    tool_identity=current,
                    expected_reuse_job_ids=[job.job_id],
                    source_attempt_id="A1",
                )
            self.assertEqual([item["id"] for item in reused], [job.job_id])
            self.assertEqual(reused[0]["disposition"], "reused")
            current_metadata = codex_upgrade._job_incremental_metadata(
                job,
                identity=identity,
                tool_identity=current,
            )
            self.assertEqual(
                reused[0]["incremental_result_key"],
                current_metadata["result_key"],
            )
            self.assertEqual(
                result["incremental_result_key"],
                current_metadata["result_key"],
            )

    def test_hybrid_orchestrator_change_does_not_invalidate_unrelated_job(self) -> None:
        """阶段限定编排器修复只应触发 transition，不应误伤 relay 结果。"""

        current = codex_upgrade._tool_identity(include_git=False)
        historical = copy.deepcopy(current)
        old_sha = "a" * 64
        changed = False
        for entry in historical["entries"]:
            if entry["path"] == "codex_upgrade.py":
                entry["sha256"] = old_sha
                changed = True
                break
        self.assertTrue(changed)
        for value in historical["components"].values():
            for entry in value["entries"]:
                if entry["path"] == "codex_upgrade.py":
                    entry["sha256"] = old_sha
            value["sha256"] = incremental_recovery.digest(
                {"entries": value["entries"]}
            )
        historical["files_sha256"] = codex_upgrade._fingerprint(
            {"entries": historical["entries"]}
        )
        historical["component_identity_sha256"] = codex_upgrade._fingerprint(
            {
                "schema_version": incremental_recovery.SCHEMA_VERSION,
                "component_count": len(historical["components"]),
                "components": historical["components"],
                "all_sha256": historical["files_sha256"],
            }
        )
        sides = codex_upgrade._tool_identity_sides(historical["entries"])
        historical.update(sides)

        job = codex_upgrade.Job(
            job_id="job-relay",
            phase="official",
            suites=("full",),
            description="relay dependency",
            steps=(
                {"argv": ["bash", "run_official_codex_compact_capture.sh"]},
            ),
            evidence_roots=(),
            covers=(),
        )
        identity = {"cli_version": "0.151.0"}
        metadata = codex_upgrade._job_incremental_metadata(
            job,
            identity=identity,
            tool_identity=historical,
        )
        result = {
            "id": job.job_id,
            "phase": job.phase,
            "status": "complete",
            "execution_sha256": metadata["input_sha256"],
            "incremental_result_key": metadata["result_key"],
            "tool_components": metadata["components"],
            "tool_component_digests": metadata["component_digests"],
            "tool_dependency_files": metadata["tool_dependency_files"],
            "input_sha256": metadata["input_sha256"],
            "environment_sha256": metadata["environment_sha256"],
            "dependency_sha256": metadata["dependency_sha256"],
        }
        self.assertTrue(
            codex_upgrade._historical_result_metadata_matches(
                result,
                job,
                identity,
                historical,
                metadata["input_sha256"],
                current_tool=current,
            )
        )

    def test_incremental_noop_isolated_from_attempts_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            campaign.mkdir(mode=0o700)
            campaign_manifest = campaign / "campaign.json"
            campaign_manifest.write_text("{}\n", encoding="utf-8")
            campaign_manifest.chmod(0o600)
            source = campaign / "official" / "attempts" / "A1" / "attempt.json"
            source.parent.mkdir(parents=True, mode=0o700)
            source.write_text('{"status":"failed"}\n', encoding="utf-8")
            source.chmod(0o600)
            source_binding = {
                "path": source.relative_to(campaign).as_posix(),
                "sha256": codex_upgrade.file_sha256(source),
                "bytes": source.stat().st_size,
            }
            manifest = {"campaign_id": "camp-1"}
            result = codex_upgrade._write_incremental_noop_receipt(
                campaign,
                manifest,
                phase="official",
                candidate_id=None,
                identity={"cli_version": "0.151.0"},
                planned_job_ids=["job-1"],
                reused_results=[{"id": "job-1", "source_receipt": source_binding}],
                changed_components=["evaluator"],
                tool_identity={"components": {}},
            )
            self.assertEqual(result["status"], "incremental-noop")
            noop_path = Path(result["noop_receipt"]["path"])
            self.assertTrue(noop_path.is_file())
            self.assertIn("incremental-noop", noop_path.parts)
            self.assertNotIn("attempts", noop_path.parts)
            payload = json.loads(noop_path.read_text(encoding="utf-8"))
            codex_upgrade._validate_incremental_noop_receipt(
                campaign, payload, planned_job_ids=["job-1"]
            )
            self.assertEqual(payload["execute_job_ids"], [])
            self.assertEqual(payload["failed_job_ids"], [])
            self.assertEqual(payload["scanned_bytes"], 0)
            self.assertEqual(payload["live_request_count"], 0)

    def test_capture_noop_is_success_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                codex_upgrade,
                "_run_capture_attempt",
                return_value={"status": "incremental-noop"},
            ):
                code = codex_upgrade.main(
                    [
                        "capture-official",
                        "run",
                        "--campaign-dir",
                        str(Path(directory) / "campaign"),
                    ]
                )
            self.assertEqual(code, 0)

    def test_capture_noop_short_circuits_before_reservation_and_runtime(self) -> None:
        """冻结 scope 为空时不得创建 attempt、探针或触发真实请求。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            campaign.mkdir(mode=0o700)
            campaign_file = campaign / "campaign.json"
            campaign_file.write_text("{}\n", encoding="utf-8")
            campaign_file.chmod(0o600)
            source_root = campaign / "official" / "attempts" / "source"
            source_root.mkdir(parents=True, mode=0o700)
            source_receipts: list[dict[str, object]] = []
            for index in (1, 2):
                path = source_root / f"job-{index}.json"
                path.write_text("{}\n", encoding="utf-8")
                path.chmod(0o600)
                source_receipts.append(
                    {
                        "id": f"job-{index}",
                        "source_receipt": {
                            "path": path.relative_to(campaign).as_posix(),
                            "sha256": codex_upgrade.file_sha256(path),
                            "bytes": path.stat().st_size,
                        },
                    }
                )
            jobs = [
                codex_upgrade.Job(
                    job_id=f"job-{index}",
                    phase="official",
                    suites=("full",),
                    description="no-op boundary",
                    steps=({"argv": ["true"]},),
                    evidence_roots=(),
                    covers=(),
                )
                for index in (1, 2)
            ]
            manifest = {
                "campaign_id": "noop-campaign",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "official_identity": {"cli_version": "0.151.0"},
            }
            scope = {
                "planned_job_ids": ["job-1", "job-2"],
                "completed_job_ids": ["job-1", "job-2"],
                "failed_job_ids": [],
                "pending_job_ids": [],
                "execute_job_ids": [],
            }
            arguments = type(
                "Arguments",
                (),
                {
                    "campaign_dir": campaign,
                    "candidate_id": None,
                    "rerun_failed": True,
                    "acknowledge_live_requests": False,
                    "max_wall_seconds": 30,
                    "heartbeat_seconds": 1,
                    "attempt_id": None,
                    "capture_manifest": None,
                    "assertion_evidence_root": None,
                    "restoration_report": None,
                    "evidence_root": [],
                    "approve_seal_sha256": None,
                    "observed_profile_receipt": None,
                    "client_evidence": [],
                },
            )()
            forbidden = {
                name: mock.patch.object(
                    codex_upgrade,
                    name,
                    side_effect=AssertionError(f"no-op 不得调用 {name}"),
                )
                for name in (
                    "_reserve_capture_attempt",
                    "_verify_plan_identity",
                    "_verify_execution_tree",
                    "_verify_official_binaries",
                    "_probe_capture_environment",
                    "_capture_arm64_environment_receipt",
                )
            }
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_load_stage_result",
                        side_effect=codex_upgrade.ConfigurationError(
                            "阶段尚未封存：capture-official"
                        ),
                    )
                )
                stack.enter_context(
                    mock.patch.object(codex_upgrade, "_reject_contaminated_campaign")
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade, "_campaign_jobs", return_value=jobs
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade, "_active_unsealed_attempts", return_value=[]
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_tool_identity",
                        return_value={"components": {}},
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_cheap_capture_tool_impact",
                        return_value={
                            "changed_components": [],
                            "affected_job_ids": [],
                        },
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_latest_failed_attempt_for_identity",
                        return_value=(source_root, {"status": "failed"}),
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_phase_evaluation_recovery_scope",
                        return_value=scope,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_validate_recovery_scope_plan",
                        return_value=(set(scope["completed_job_ids"]), set()),
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        codex_upgrade,
                        "_prior_complete_results",
                        return_value=source_receipts,
                    )
                )
                for patcher in forbidden.values():
                    stack.enter_context(patcher)
                result = codex_upgrade._run_capture_attempt(
                    arguments,
                    "official",
                    _lease=mock.Mock(),
                    _manifest=manifest,
                    _deadline=incremental_recovery.WallClockDeadline(30),
                )
            self.assertEqual(result["status"], "incremental-noop")
            self.assertEqual(result["execute_job_ids"], [])
            self.assertEqual(result["reused_job_ids"], ["job-1", "job-2"])
            self.assertEqual(
                sorted(path.name for path in (campaign / "official" / "attempts").iterdir()),
                ["source"],
            )
            self.assertTrue((campaign / "incremental-noop" / "official").is_dir())

    def test_mutating_cli_command_gets_one_persistent_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = self._lease_campaign(Path(directory))
            manifest = {"campaign_id": "lease-campaign"}
            with (
                mock.patch.object(
                    codex_upgrade, "load_campaign_manifest", return_value=manifest
                ),
                mock.patch.object(
                    codex_upgrade, "_main_without_campaign_lease", return_value=0
                ),
            ):
                code = codex_upgrade.main(
                    [
                        "compare",
                        "--campaign-dir",
                        str(campaign),
                        "--candidate-id",
                        "candidate-a",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(
                (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(payload["state"], "released")
            self.assertEqual(payload["phase"], "candidate")
            self.assertEqual(payload["candidate_id"], "candidate-a")

    def test_failed_mutating_cli_command_leaves_stop_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = self._lease_campaign(Path(directory))
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value={"campaign_id": "lease-campaign"},
                ),
                mock.patch.object(
                    codex_upgrade, "_main_without_campaign_lease", return_value=1
                ),
            ):
                code = codex_upgrade.main(
                    [
                        "compare",
                        "--campaign-dir",
                        str(campaign),
                        "--candidate-id",
                        "candidate-a",
                    ]
                )
            self.assertEqual(code, 1)
            stops = list(
                (campaign / codex_upgrade.CAMPAIGN_LEASE_STOP_DIRECTORY).glob("*.json")
            )
            self.assertEqual(len(stops), 1)
            self.assertEqual(
                json.loads(
                    (campaign / codex_upgrade.CAMPAIGN_LEASE_FILENAME).read_text(
                        encoding="utf-8"
                    )
                )["state"],
                "stop_the_line",
            )
            supervisor_runs = list((campaign / ".supervisor").glob("run-*/state.json"))
            self.assertEqual(len(supervisor_runs), 1)
            supervisor_state = json.loads(
                supervisor_runs[0].read_text(encoding="utf-8")
            )
            self.assertEqual(supervisor_state["state"], "failed")
            report = codex_upgrade.codex_upgrade_supervisor._audit_command(
                supervisor_runs[0].parent
            )
            self.assertFalse(report["audit_incomplete"])

    def _valid_payload(self, root: Path) -> tuple[Path, dict[str, object], set[str]]:
        campaign = root / "campaign"
        campaign.mkdir(mode=0o700)
        attempt = campaign / "official" / "attempts" / "A1"
        attempt.mkdir(parents=True, mode=0o700)
        started = (
            datetime.now(timezone.utc) - timedelta(seconds=2)
        ).isoformat().replace("+00:00", "Z")
        deadline = incremental_recovery.WallClockDeadline(120)
        deadline.phase = "official"
        deadline.heartbeat_seconds = 30
        deadline.last_completed_job_id = None
        heartbeat = attempt / "watchdog-heartbeat.json"
        codex_upgrade._write_attempt_heartbeat(
            heartbeat,
            deadline,
            operation="attempt:reserved",
            force=True,
            attempt_root=attempt,
        )
        result = {
            "id": "job-1",
            "status": "complete",
            "disposition": "executed",
            "incremental_result_key": "a" * 64,
        }
        store = incremental_recovery.CheckpointStore(attempt / "checkpoints")
        store.append(
            {
                "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                "campaign_id": "camp-id",
                "phase": "official",
                "attempt_id": "A1",
                "run_nonce": "b" * 64,
                "item_id": "job-1",
                "status": "complete",
                "disposition": "executed",
                "result_sha256": incremental_recovery.digest(result),
                "result_key": "a" * 64,
                "result": result,
                "previous_checkpoint_sha256": None,
            }
        )
        completed = codex_upgrade._utc_now()
        payload: dict[str, object] = {
            "campaign_id": "camp-id",
            "phase": "official",
            "attempt_id": "A1",
            "run_nonce": "b" * 64,
            "started_at_utc": started,
            "completed_at_utc": completed,
            "execution_error": None,
            "results": [result],
            "watchdog": {
                "schema_version": codex_upgrade.WATCHDOG_HEARTBEAT_SCHEMA,
                "budget_seconds": 120,
                "heartbeat_seconds": 30,
                "elapsed_seconds": 0,
                "remaining_seconds": 119,
                "heartbeat": {
                    "path": "official/attempts/A1/watchdog-heartbeat.json",
                    "sha256": codex_upgrade.file_sha256(heartbeat),
                    "bytes": heartbeat.stat().st_size,
                },
                "timeout_checkpoint": None,
                "last_completed_job_id": None,
            },
            "job_checkpoint": {
                "schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                "campaign_id": "camp-id",
                "phase": "official",
                "attempt_id": "A1",
                "run_nonce": "b" * 64,
                "path": "official/attempts/A1/checkpoints",
                "record_count": 1,
                "last_sequence": 1,
                "last_sha256": store.records()[-1]["checkpoint_sha256"],
            },
        }
        return campaign, payload, {"job-1"}

    def test_valid_bindings_replay_without_reading_other_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, payload, planned = self._valid_payload(Path(directory))
            attempt = campaign / "official" / "attempts" / "A1"
            codex_upgrade._validate_attempt_watchdog_fields(payload, planned)
            codex_upgrade._validate_attempt_watchdog_bindings(
                campaign, attempt, payload, planned
            )

    def test_failed_official_heartbeat_replays_without_phase_drift(self) -> None:
        """失败 official attempt 的 status／recovery 校验不再看到 stage phase。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign, payload, planned = self._valid_payload(Path(directory))
            attempt = campaign / "official" / "attempts" / "A1"
            deadline = incremental_recovery.WallClockDeadline(120)
            deadline.phase = "stage"
            started = deadline.started_monotonic
            ending = deadline.deadline_monotonic
            codex_upgrade._bind_attempt_deadline_metadata(deadline, "official")
            heartbeat = attempt / "watchdog-heartbeat.json"
            codex_upgrade._write_attempt_heartbeat(
                heartbeat,
                deadline,
                operation="attempt:failed",
                force=True,
                attempt_root=attempt,
            )
            payload["execution_error"] = {
                "type": "ConfigurationError",
                "message": "合成失败",
            }
            payload["completed_at_utc"] = (
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            payload["watchdog"]["heartbeat"] = {
                "path": "official/attempts/A1/watchdog-heartbeat.json",
                "sha256": codex_upgrade.file_sha256(heartbeat),
                "bytes": heartbeat.stat().st_size,
            }

            codex_upgrade._validate_attempt_watchdog_fields(payload, planned)
            codex_upgrade._validate_attempt_watchdog_bindings(
                campaign,
                attempt,
                payload,
                planned,
            )
            document = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(document["phase"], "official")
            self.assertEqual(deadline.started_monotonic, started)
            self.assertEqual(deadline.deadline_monotonic, ending)

    def test_file_binding_cannot_cross_to_sibling_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, payload, _ = self._valid_payload(Path(directory))
            sibling = campaign / "official" / "attempts" / "A2"
            sibling.mkdir(mode=0o700)
            binding = dict(payload["watchdog"]["heartbeat"])
            binding["path"] = "official/attempts/A2/watchdog-heartbeat.json"
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "当前 attempt"):
                codex_upgrade._resolve_attempt_binding(
                    campaign,
                    campaign / "official" / "attempts" / "A1",
                    binding,
                    label="watchdog heartbeat",
                    expected_name="watchdog-heartbeat.json",
                )

    def test_checkpoint_identity_and_result_digest_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign, payload, planned = self._valid_payload(Path(directory))
            attempt = campaign / "official" / "attempts" / "A1"
            checkpoint = attempt / "checkpoints" / "00000001.json"
            original = checkpoint.read_text(encoding="utf-8")
            checkpoint.write_text(original.replace('"attempt_id": "A1"', '"attempt_id": "A2"'), encoding="utf-8")
            checkpoint.chmod(0o600)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "checkpoint"):
                codex_upgrade._validate_attempt_watchdog_bindings(
                    campaign, attempt, payload, planned
                )

    def test_heartbeat_rejects_secret_operation_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory) / "attempt"
            attempt.mkdir(mode=0o700)
            deadline = incremental_recovery.WallClockDeadline(30)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._write_attempt_heartbeat(
                    attempt / "watchdog-heartbeat.json",
                    deadline,
                    operation="authorization=do-not-write-this-token",
                    force=True,
                    attempt_root=attempt,
                )
            self.assertFalse((attempt / "watchdog-heartbeat.json").exists())

    def test_wait_process_raises_global_timeout_and_kills_group(self) -> None:
        process = mock.Mock()
        process.pid = 4242
        process.poll.return_value = None
        process.wait.side_effect = __import__("subprocess").TimeoutExpired("x", 0.01)
        deadline = incremental_recovery.WallClockDeadline(0.001)
        with mock.patch.object(codex_upgrade.os, "killpg") as killpg:
            with self.assertRaises(incremental_recovery.WallClockTimeoutError):
                codex_upgrade._wait_process(
                    process,
                    30,
                    deadline=deadline,
                    operation="job:job-1:step-1",
                )
            self.assertTrue(killpg.called)


if __name__ == "__main__":
    unittest.main()
