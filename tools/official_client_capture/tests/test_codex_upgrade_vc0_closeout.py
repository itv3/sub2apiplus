"""Codex VC-0 原子收口工具测试。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_arm_receipt,
    create_job_rehearsal_receipt,
)


class VC0CloseoutTests(unittest.TestCase):
    def _write(self, path: Path, value: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _synthetic_validated(self, root: Path) -> closeout.ValidatedInputs:
        root.chmod(0o700)
        preflight = root / "preflight"
        preflight.mkdir(mode=0o700)
        inputs_root = preflight / "inputs"
        inputs_root.mkdir(mode=0o700)
        for name in (
            "baseline-rules.json",
            "discovery-scenarios.json",
            "target-discovery-scenarios.json",
            "extra-jobs.json",
        ):
            self._write(inputs_root / name, {"name": name})
        configuration = {
            "baseline_source": "/evidence/source-0.151.0",
            "target_source": "/evidence/source-0.154.0",
            "target_package": "/evidence/codex-package.tar.gz",
            "baseline_evidence": "/evidence/active-profile.json",
            "runtime_image": f"capture@sha256:{'4' * 64}",
            "model": "gpt-5.5",
            "lite_model": "gpt-6-astra",
            "capture_root": "/root/oauth-capture",
            "capture_container": "capture-cli",
            "service_container": "sub2apiplus",
            "keeper_container": "sub2apiplus-keeper",
            "postgres_container": "sub2apiplus-postgres",
            "redis_container": "sub2apiplus-redis",
            "capture_codex_bin": "/opt/codex-0.154.0/bin/codex",
            "relay_codex_bin": "/opt/codex-0.154.0/bin/codex",
            "capture_code_mode_host_bin": (
                "/opt/codex-0.154.0/bin/codex-code-mode-host"
            ),
            "relay_code_mode_host_bin": (
                "/opt/codex-0.154.0/bin/codex-code-mode-host"
            ),
            "codex_account_id": 22,
            "api_key_id": 4,
            "live_attestation_compose_dir": "/srv/sub2api",
            "live_attestation_compose_files": "/srv/sub2api/docker-compose.yml",
        }
        manifest = {
            "campaign_id": "preflight-0154",
            "campaign_mode": "preflight_only",
            "campaign_purpose": "production_replacement",
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "target_sha256": "1" * 64,
            "suite": "full",
            "official_identity": {
                "package": {
                    "asset_sha256": "2" * 64,
                    "code_mode_host_sha256": "3" * 64,
                }
            },
            "configuration": configuration,
            "inputs": {
                "baseline_rules": {
                    "path": "inputs/baseline-rules.json",
                    "sha256": closeout._sha256_file(
                        inputs_root / "baseline-rules.json"
                    ),
                },
                "discovery_scenarios": {
                    "path": "inputs/discovery-scenarios.json",
                    "sha256": closeout._sha256_file(
                        inputs_root / "discovery-scenarios.json"
                    ),
                },
                "target_discovery_scenarios": {
                    "path": "inputs/target-discovery-scenarios.json",
                    "sha256": closeout._sha256_file(
                        inputs_root / "target-discovery-scenarios.json"
                    ),
                },
                "extra_jobs": {
                    "path": "inputs/extra-jobs.json",
                    "sha256": closeout._sha256_file(inputs_root / "extra-jobs.json"),
                },
            },
        }
        self._write(preflight / "campaign.json", manifest)

        ledger_root = root / "timing"
        timing.create_ledger(
            ledger_root,
            upgrade_id="upgrade-0154",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
        )
        timing_summary = timing.inspect_ledger(ledger_root)
        sources_root = root / "sources"
        sources_root.mkdir(mode=0o700)
        sources = []
        for role in closeout.INPUT_ROLES:
            source = self._write(sources_root / f"{role}.json", {"role": role})
            sources.append(closeout._binding_source(role, source))
        arm_root = root / "arm"
        job_root = root / "job"
        p0_root = root / "p0"
        for evidence_root in (arm_root, job_root, p0_root):
            evidence_root.mkdir(mode=0o700)
        arm_path = self._write(arm_root / "receipt.json", {"synthetic": True})
        job_path = self._write(job_root / "receipt.json", {"synthetic": True})
        p0_path = self._write(p0_root / "receipt.json", {"synthetic": True})
        return closeout.ValidatedInputs(
            preflight_dir=preflight,
            preflight_manifest=manifest,
            timing_ledger_dir=ledger_root,
            arm64_root=arm_root,
            arm64_receipt=arm_path,
            job_rehearsal_root=job_root,
            job_rehearsal_receipt=job_path,
            p0_gate_root=p0_root,
            p0_gate_receipt=p0_path,
            receipts=tuple(sources),
            timing_summary=timing_summary,
        )

    def _arguments(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            preflight_campaign_dir=root / "preflight",
            formal_campaign_dir=root / "campaigns" / "formal-0154",
            formal_campaign_id="formal-0154",
            job_rehearsal_root=root / "job",
            job_rehearsal_receipt=Path("receipt.json"),
            p0_gate_root=root / "p0",
            p0_gate_receipt=Path("receipt.json"),
            atomic_rehearsal_root=root / "atomic",
            atomic_rehearsal_receipt=Path("receipt.json"),
            managed_tool_deploy_receipt=root / "deploy.json",
            supervisor_state_dir=root / "control" / "vc1-supervisor",
            audit_dir=root / "audit" / "closeout-0154",
            heartbeat_seconds=5.0,
            watchdog_timeout_seconds=20.0,
            ledger_interval_seconds=60.0,
        )

    def _prepare_output_parents(self, root: Path) -> None:
        for path in (root / "campaigns", root / "control", root / "audit"):
            path.mkdir(mode=0o700)

    def _fake_formal_manifest(
        self,
        campaign_dir: Path,
        campaign_id: str,
    ) -> dict[str, object]:
        vc_root = campaign_dir / "control" / "vc"
        run_root = vc_root / "run-manifests"
        run_root.mkdir(parents=True, mode=0o700)
        vc_root.chmod(0o700)
        (campaign_dir / "control").chmod(0o700)
        plan = self._write(vc_root / "campaign-plan.json", {"plan": True})
        checkpoint = self._write(
            vc_root / "vc-0-checkpoint.json", {"checkpoint": True}
        )
        run = self._write(run_root / "0001-vc-1.json", {"run": True})
        manifest: dict[str, object] = {
            "campaign_id": campaign_id,
            "campaign_mode": "formal",
            "vc_control": {
                "campaign_plan": {
                    "path": "control/vc/campaign-plan.json",
                    "sha256": closeout._sha256_file(plan),
                },
                "vc0_checkpoint": {
                    "path": "control/vc/vc-0-checkpoint.json",
                    "sha256": closeout._sha256_file(checkpoint),
                },
                "first_campaign_run_manifest": {
                    "path": "control/vc/run-manifests/0001-vc-1.json",
                    "sha256": closeout._sha256_file(run),
                },
            },
        }
        campaign_path = self._write(campaign_dir / "campaign.json", manifest)
        digest_path = campaign_dir / "campaign.sha256"
        digest_path.write_text(
            closeout._sha256_file(campaign_path) + "\n",
            encoding="ascii",
        )
        digest_path.chmod(0o600)
        return manifest

    def _live_request_fixture(self, root: Path) -> tuple[Path, Path]:
        """构造完成、失败归档、relay、前置失败和待执行混合现场。"""

        data_root = root / "data"
        campaign_dir = data_root / "evidence" / "campaigns" / "formal-live"
        campaign_dir.mkdir(parents=True, mode=0o700)
        data_root.chmod(0o700)
        runs_root = data_root / "runs"
        runs_root.mkdir(mode=0o700)

        core_root = runs_root / "core"
        self._write(
            core_root / "manifest.json",
            {
                "schema_version": "official-client-capture/v1",
                "case_results": [
                    {"scenario_result": {"turn_count": 1}},
                    {"scenario_result": {"turn_count": 2}},
                ],
            },
        )
        for attempt, turns in ((1, 2), (2, 1)):
            self._write(
                runs_root
                / f"compact.failed-attempt{attempt}"
                / "result"
                / "direct"
                / "summary.json",
                {
                    "schema_version": "codex-compact-capture/v1",
                    "turn_completed_count": turns,
                },
            )

        relay_root = runs_root / "relay" / "relay"
        relay_root.mkdir(parents=True, mode=0o700)
        request_bodies = []
        for model in ("gpt-5.5", "gpt-6-astra"):
            body = json.dumps(
                {"model": model, "input": []},
                separators=(",", ":"),
            ).encode("utf-8")
            request_bodies.append(
                b"POST /backend-api/codex/responses HTTP/1.1\r\n"
                b"content-type: application/json\r\n"
                + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
        request_path = relay_root / "conn001.client_to_upstream.bin"
        request_path.write_bytes(b"".join(request_bodies))
        request_path.chmod(0o600)

        zero_relay_root = runs_root / "oauth-zero" / "relay"
        zero_relay_root.mkdir(parents=True, mode=0o700)
        zero_request_path = zero_relay_root / "conn001.client_to_upstream.bin"
        zero_request_path.write_bytes(
            b"POST /oauth/token HTTP/1.1\r\n"
            b"content-type: application/json\r\n"
            b"content-length: 2\r\n\r\n{}"
        )
        zero_request_path.chmod(0o600)

        empty_relay_root = runs_root / "relay-empty" / "relay"
        empty_relay_root.mkdir(parents=True, mode=0o700)
        self._write(
            empty_relay_root / "relay.json",
            {
                "schema_version": "byte-relay/v1",
                "mode": "direct",
                "connections": [],
            },
        )

        log_path = (
            campaign_dir
            / "official"
            / "attempts"
            / "attempt-1"
            / "logs"
            / "pre-request-job-1.log"
        )
        log_path.parent.mkdir(parents=True, mode=0o700)
        log_path.write_text(
            "mkdir: cannot create directory: Read-only file system\n",
            encoding="utf-8",
        )
        log_path.chmod(0o600)

        jobs = [
            {
                "id": "official-core",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/core"],
            },
            {
                "id": "official-compact",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/compact"],
            },
            {
                "id": "official-relay",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/relay"],
            },
            {
                "id": "official-relay-oauth-refresh",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/oauth-zero"],
            },
            {
                "id": "official-relay-pre-request-zero",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/relay-empty"],
            },
            {
                "id": "pre-request-job",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/pre-request"],
            },
            {
                "id": "pending-job",
                "phase": "official",
                "evidence_roots": ["/root/oauth-capture/runs/pending"],
            },
        ]
        campaign_path = self._write(
            campaign_dir / "campaign.json",
            {
                "campaign_id": "formal-live",
                "campaign_mode": "formal",
                "configuration": {"capture_root": "/root/oauth-capture"},
                "jobs": jobs,
            },
        )
        digest_path = campaign_dir / "campaign.sha256"
        digest_path.write_text(
            closeout._sha256_file(campaign_path) + "\n",
            encoding="ascii",
        )
        digest_path.chmod(0o600)
        return data_root, campaign_dir

    def test_live_request_audit_counts_mixed_formal_evidence(self) -> None:
        """逐一覆盖完成、失败归档、relay、前置失败和 pending 口径。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _data_root, campaign_dir = self._live_request_fixture(root)
            audit = closeout.build_live_request_audit(
                campaign_dir,
                formal_campaign_id="formal-live",
                observed_at_utc="2026-09-13T23:00:00Z",
            )

        self.assertEqual(audit["live_request_count"], 8)
        self.assertEqual(
            audit["observed_job_ids"],
            [
                "official-compact",
                "official-core",
                "official-relay",
                "official-relay-oauth-refresh",
                "official-relay-pre-request-zero",
                "pre-request-job",
            ],
        )
        self.assertEqual(audit["pending_job_ids"], ["pending-job"])
        self.assertEqual(audit["pre_request_zero_job_ids"], ["pre-request-job"])
        self.assertEqual(
            sorted(source["live_request_count"] for source in audit["sources"]),
            [0, 0, 1, 2, 2, 3],
        )

    def test_failed_closeout_closure_repair_is_append_only(self) -> None:
        """计数器修复只能承接原 closure-failed 诊断并追加阶段终态。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root, campaign_dir = self._live_request_fixture(root)
            ledger_root = data_root / "control" / "timing-ledger"
            ledger_root.parent.mkdir(parents=True, mode=0o700)
            now = datetime.now(timezone.utc)
            timing.create_ledger(
                ledger_root,
                upgrade_id="upgrade-0154",
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose="production_replacement",
                evidence_decision="recapture",
                started_at_utc=(now - timedelta(minutes=190)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="formal-live-p0-receipts-passed",
                phase="VC-0",
                event_type="receipt_passed",
                recorded_at_utc=(now - timedelta(minutes=189)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="formal-live-vc0-completed",
                phase="VC-0",
                event_type="stage_completed",
                recorded_at_utc=(now - timedelta(minutes=188)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="formal-live-vc1-started",
                phase="VC-1",
                event_type="stage_started",
                recorded_at_utc=(now - timedelta(minutes=187)).isoformat(),
            )
            timing_summary = timing.inspect_ledger(
                ledger_root,
                now=(now - timedelta(minutes=186)).isoformat(),
            )
            receipt_root = (
                ledger_root / "receipts" / "vc0-closeout" / "formal-live"
            )
            receipt_root.mkdir(parents=True, mode=0o700)

            source_audit = data_root / "audit" / "source-closeout"
            source_audit.mkdir(parents=True, mode=0o700)
            self._write(
                source_audit / "request.json",
                {
                    "formal_campaign_dir": str(campaign_dir),
                    "formal_campaign_id": "formal-live",
                },
            )
            self._write(
                source_audit / "failure.json",
                {
                    "schema_version": closeout.CLOSEOUT_DIAGNOSTIC_SCHEMA,
                    "status": "failed",
                    "failed_step": "dispatch-vc1",
                    "formal_campaign_path_exists": True,
                    "deadline_extended": False,
                    "cleanup_performed": False,
                    "timing_summary": timing_summary,
                    "timing_failure_closure": {
                        "status": "closure-failed",
                        "error_type": "VC0CloseoutError",
                        "message": closeout.FAILED_CLOSEOUT_CLOSURE_REPAIR_MESSAGE,
                    },
                },
            )
            repair_parent = data_root / "audit" / "repairs"
            repair_parent.mkdir(mode=0o700)

            forged_audit = data_root / "audit" / "forged-closeout"
            forged_audit.mkdir(mode=0o700)
            self._write(
                forged_audit / "request.json",
                {
                    "formal_campaign_dir": str(campaign_dir),
                    "formal_campaign_id": "formal-live",
                },
            )
            forged_failure = json.loads(
                (source_audit / "failure.json").read_text(encoding="utf-8")
            )
            forged_failure["timing_failure_closure"]["message"] = (
                "其他未批准的闭合失败"
            )
            self._write(forged_audit / "failure.json", forged_failure)
            before_forged = timing.inspect_ledger(ledger_root)
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "不属于可追加修复",
            ):
                closeout.repair_failed_closeout_closure(
                    formal_campaign_dir=campaign_dir,
                    timing_ledger_dir=ledger_root,
                    source_audit_dir=forged_audit,
                    audit_dir=repair_parent / "forged-repair",
                )
            self.assertEqual(timing.inspect_ledger(ledger_root), before_forged)

            receipt = closeout.repair_failed_closeout_closure(
                formal_campaign_dir=campaign_dir,
                timing_ledger_dir=ledger_root,
                source_audit_dir=source_audit,
                audit_dir=repair_parent / "repair-1",
            )

            summary = timing.inspect_ledger(ledger_root)
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(
                receipt["closure"]["status"],
                "stop-the-line-recorded",
            )
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertEqual(receipt["total_live_request_count"], 8)
            self.assertEqual(summary["status"], "stopped")
            self.assertEqual(summary["active_phase"], "VC-1")
            self.assertEqual(summary["total_live_request_count"], 8)

            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "原时间账本字段漂移",
            ):
                closeout.repair_failed_closeout_closure(
                    formal_campaign_dir=campaign_dir,
                    timing_ledger_dir=ledger_root,
                    source_audit_dir=source_audit,
                    audit_dir=repair_parent / "repair-2",
                )

    def test_failure_accounting_repair_is_append_only_and_not_repeatable(self) -> None:
        """历史失败事件保持逐字不变，计数只能追加一次。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root, campaign_dir = self._live_request_fixture(root)
            ledger_root = data_root / "control" / "timing-ledger"
            ledger_root.parent.mkdir(parents=True, mode=0o700)
            now = datetime.now(timezone.utc)
            timing.create_ledger(
                ledger_root,
                upgrade_id="upgrade-0154",
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose="production_replacement",
                evidence_decision="recapture",
                started_at_utc=(now - timedelta(minutes=10)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="vc0-closeout-failure-fixture",
                phase="VC-0",
                event_type="stage_abandoned",
                root_cause_id="capture-path-fixture",
                next_action="完成工具修复后恢复 VC-1",
                recorded_at_utc=(now - timedelta(minutes=5)).isoformat(),
            )
            historical = {
                path.name: path.read_bytes()
                for path in sorted((ledger_root / "events").iterdir())
            }
            receipt_root = (
                ledger_root / "receipts" / "vc0-closeout" / "formal-live"
            )
            receipt_root.mkdir(parents=True, mode=0o700)
            audit_parent = data_root / "audit"
            audit_parent.mkdir(mode=0o700)

            receipt = closeout.repair_failure_live_request_accounting(
                formal_campaign_dir=campaign_dir,
                timing_ledger_dir=ledger_root,
                audit_dir=audit_parent / "repair-1",
            )
            self.assertEqual(receipt["live_request_count"], 8)
            self.assertFalse(receipt["history_rewritten"])
            self.assertEqual(
                timing.inspect_ledger(ledger_root)["total_live_request_count"],
                8,
            )
            for name, raw in historical.items():
                self.assertEqual((ledger_root / "events" / name).read_bytes(), raw)

            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "已有 live 请求计量",
            ):
                closeout.repair_failure_live_request_accounting(
                    formal_campaign_dir=campaign_dir,
                    timing_ledger_dir=ledger_root,
                    audit_dir=audit_parent / "repair-2",
                )

    def test_recovers_every_formal_plan_parameter_from_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            formal = root / "formal"
            checkpoint = root / "timing" / "receipts" / "active.json"
            self._write(checkpoint, {"active": True})

            arguments = closeout.recover_formal_plan_arguments(
                validated,
                formal_campaign_dir=formal,
                formal_campaign_id="formal-0154",
                timing_receipt=checkpoint,
            )

        configuration = validated.preflight_manifest["configuration"]
        self.assertEqual(arguments.command, "plan")
        self.assertEqual(arguments.campaign_mode, "formal")
        self.assertEqual(arguments.campaign_id, "formal-0154")
        self.assertEqual(arguments.campaign_dir, formal)
        self.assertEqual(arguments.baseline_version, "0.151.0")
        self.assertEqual(arguments.target_version, "0.154.0")
        self.assertEqual(arguments.target_sha256, "1" * 64)
        self.assertEqual(arguments.target_package_sha256, "2" * 64)
        self.assertEqual(arguments.target_code_mode_host_sha256, "3" * 64)
        for field in (
            "baseline_source",
            "target_source",
            "target_package",
            "baseline_evidence",
            "runtime_image",
            "model",
            "lite_model",
            "capture_root",
            "capture_container",
            "service_container",
            "keeper_container",
            "postgres_container",
            "redis_container",
            "capture_codex_bin",
            "relay_codex_bin",
            "capture_code_mode_host_bin",
            "relay_code_mode_host_bin",
            "codex_account_id",
            "api_key_id",
            "live_attestation_compose_dir",
            "live_attestation_compose_files",
        ):
            expected = configuration[field]
            actual = getattr(arguments, field)
            if field in {
                "baseline_source",
                "target_source",
                "target_package",
                "baseline_evidence",
                "capture_root",
            }:
                expected = Path(str(expected))
            self.assertEqual(actual, expected, field)
        self.assertEqual(arguments.extra_jobs.name, "extra-jobs.json")

    def test_any_of_five_receipts_drifting_before_copy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            for index, source in enumerate(validated.receipts, 1):
                with self.subTest(role=source.role):
                    original = source.path.read_bytes()
                    source.path.write_bytes(original + b" ")
                    source.path.chmod(0o600)
                    with self.assertRaisesRegex(
                        closeout.VC0CloseoutError,
                        "复制结果漂移",
                    ):
                        closeout._copy_inputs_to_ledger(
                            validated,
                            validated.timing_ledger_dir
                            / "receipts"
                            / "vc0-closeout"
                            / f"drift-{index}",
                        )
                    source.path.write_bytes(original)
                    source.path.chmod(0o600)

    def test_job_receipt_without_failure_lifecycle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            job_root = root / "job"
            job_root.mkdir(mode=0o700)
            contract = {
                "schema_version": closeout.codex_upgrade_job_rehearsal_receipt.EXECUTION_CONTRACT_SCHEMA,
                "target_version": "0.154.0",
                "target_sha256": "1" * 64,
                "target_package_sha256": "2" * 64,
                "target_code_mode_host_sha256": "3" * 64,
                "suite": "full",
                "tool_files_sha256": codex_upgrade._tool_identity()[
                    "files_sha256"
                ],
                "configuration": {
                    field: (
                        "/root/oauth-capture"
                        if field == "capture_root"
                        else "capture-cli"
                        if field == "capture_container"
                        else f"fixture-{field}"
                    )
                    for field in closeout.codex_upgrade_job_rehearsal_receipt.EXECUTION_CONFIGURATION_FIELDS
                },
                "job_count": 1,
                "job_ids": ["job-a"],
                "job_phases": {"job-a": "official"},
                "step_counts": {"job-a": 1},
                "phase_counts": {"official": 1, "candidate": 0},
                "c2pa_job_identities": {},
                "target_scenario_sha256": "4" * 64,
                "evidence_label_declaration_sha256": "6" * 64,
                "job_templates_sha256": "7" * 64,
                "extra_jobs_sha256": None,
            }
            receipt_path = create_job_rehearsal_receipt(
                job_root,
                contract=contract,
                preflight_campaign_id="preflight-0154",
                preflight_campaign_dir=root / "preflight",
                preflight_manifest_sha256="5" * 64,
            )
            replayed = closeout.codex_upgrade_job_rehearsal_receipt.replay(
                job_root,
                receipt_path.name,
            )
            replayed.pop("failure_lifecycle_probe_sha256")
            preflight = root / "preflight"
            preflight.mkdir(mode=0o700)
            self._write(preflight / "campaign.json", {"preflight": True})
            with (
                mock.patch.object(
                    closeout.codex_upgrade_job_rehearsal_receipt,
                    "replay",
                    return_value=replayed,
                ),
                mock.patch.object(
                    closeout.codex_upgrade,
                    "_job_rehearsal_contract_from_manifest",
                    return_value=contract,
                ),
            ):
                with self.assertRaisesRegex(
                    closeout.VC0CloseoutError,
                    "Formal 所需完整 Job 演练收据未通过",
                ):
                    closeout._validate_job_rehearsal(
                        preflight,
                        {},
                        job_root,
                        Path("receipt.json"),
                    )

    def test_real_campaign_run_rehearsal_shape_is_accepted(self) -> None:
        """接受 ARM64 实际生成的乱序双批次 rehearsal 结构。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            preflight = (root / "preflight").resolve()
            preflight.mkdir(mode=0o700)
            control = preflight / "control" / "vc"
            control.mkdir(parents=True, mode=0o700)
            (preflight / "control").chmod(0o700)
            plan_path = self._write(control / "campaign-plan.json", {"plan": True})
            checkpoint_path = self._write(
                control / "vc-0-checkpoint.json",
                {"checkpoint": True},
            )
            deadline = "2026-09-13T10:11:41+00:00"
            tool_sha256 = "1" * 64
            manifest = {
                "campaign_id": "c0154-preflight-real-shape",
                "vc_control": {
                    "campaign_plan": {
                        "path": "control/vc/campaign-plan.json",
                        "sha256": closeout._sha256_file(plan_path),
                    },
                    "vc0_checkpoint": {
                        "path": "control/vc/vc-0-checkpoint.json",
                        "sha256": closeout._sha256_file(checkpoint_path),
                    },
                },
                "tool_identity": {
                    "entries": [
                        {
                            "path": "codex_upgrade_supervisor.py",
                            "sha256": tool_sha256,
                        }
                    ]
                },
            }
            runs = []
            inventory = [
                ".campaign-run.lock",
                "batch-1.json",
                "batch-2.json",
                "batch-3-deadline-drift.json",
                "batch-executions.json",
            ]
            for sequence, suffix in ((2, "b"), (1, "a")):
                run_dir = root / f"run-{suffix}"
                run_dir.mkdir(mode=0o700)
                inventory.append(run_dir.name)
                runs.append(
                    {
                        "schema_version": "codex-upgrade-campaign-run/v2",
                        "batch_id": f"rehearsal-batch-{sequence}",
                        "batch_sequence": sequence,
                        "original_deadline_at_utc": deadline,
                        "state": "stopped",
                        "audit_incomplete": False,
                        "event_count": 7,
                        "execute_items": [f"execute-{sequence}"],
                        "reuse_items": [f"reuse-{sequence}"],
                        "run_dir": str(run_dir),
                    }
                )
            receipt_path = self._write(
                root / "campaign-run-rehearsal.json",
                {
                    "schema_version": closeout.CAMPAIGN_RUN_REHEARSAL_SCHEMA,
                    "status": "passed",
                    "campaign_id": manifest["campaign_id"],
                    "inputs": {
                        "campaign_plan_sha256": "2" * 64,
                        "preflight_campaign": str(preflight),
                        "tool_sha256": tool_sha256,
                        "vc0_checkpoint_file_sha256": closeout._sha256_file(
                            checkpoint_path
                        ),
                    },
                    "multi_batch_passed": True,
                    "original_deadline_inherited": True,
                    "deadline_drift_rejected": True,
                    "live_request_count": 0,
                    "runs": runs,
                    "negative_fixture": {
                        "kind": "original_deadline_drift",
                        "rejected_before_action": True,
                        "returncode": 1,
                        "stderr": "Campaign 后继批次改变了原始 deadline。",
                    },
                    "temporary_asset_inventory": sorted(inventory),
                },
            )
            with (
                mock.patch.object(
                    closeout.codex_upgrade_vc_artifacts,
                    "validate_campaign_plan",
                    return_value={
                        "plan_sha256": "2" * 64,
                        "original_deadline_at_utc": deadline,
                    },
                ),
                mock.patch.object(
                    closeout.codex_upgrade_supervisor,
                    "_audit_command",
                    return_value={
                        "state": "stopped",
                        "audit_incomplete": False,
                        "event_count": 7,
                    },
                ),
            ):
                replayed = closeout._validate_campaign_run_rehearsal(
                    receipt_path,
                    preflight,
                    manifest,
                )
            self.assertEqual(replayed["runs"], runs)

    def test_real_managed_deploy_receipt_shape_is_accepted(self) -> None:
        """接受 ARM64 部署器当前生成的字段和空 runtime 文档列表。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            production_root = Path(closeout.__file__).resolve().parent
            assertion_preparer = production_root.parent / "prepare_assertion_bundle.sh"
            current_identity = codex_upgrade._tool_identity()
            for name in ("docs", "tool-backup", "doc-backup", "supervisor-run"):
                (root / name).mkdir(mode=0o700)
            assertion_backup = self._write(
                root / "assertion-backup",
                {"backup": True},
            )
            payload = {
                "schema_version": closeout.MANAGED_TOOL_DEPLOY_SCHEMA,
                "status": "passed",
                "campaign_id": "c0154-supervisor-enable-real-shape",
                "created_at_utc": "2026-09-13T09:01:48.578747Z",
                "architecture": "aarch64",
                "production_tool_root": str(production_root),
                "production_doc_root": str(root / "docs"),
                "tool_files_sha256": current_identity["files_sha256"],
                "supervisor_sha256": closeout._sha256_file(
                    production_root / "codex_upgrade_supervisor.py"
                ),
                "assertion_preparer_sha256": closeout._sha256_file(
                    assertion_preparer
                ),
                "rollback_backup": str(root / "tool-backup"),
                "assertion_preparer_rollback_backup": str(assertion_backup),
                "document_rollback_backup": str(root / "doc-backup"),
                "switched_archived_documents": list(
                    closeout.EXPECTED_MANAGED_DOCUMENTS
                ),
                "installed_runtime_documents": [],
                "supervisor_run_dir": str(root / "supervisor-run"),
            }
            receipt_path = root / "deploy.json"
            receipt_path.write_bytes(closeout._canonical(payload))
            receipt_path.chmod(0o600)
            with (
                mock.patch.object(closeout.platform, "machine", return_value="aarch64"),
                mock.patch.object(
                    closeout.codex_upgrade_supervisor,
                    "_audit_command",
                    return_value={
                        "run_dir": str((root / "supervisor-run").resolve()),
                        "state": "stopped",
                        "audit_incomplete": False,
                    },
                ),
                mock.patch.object(
                    closeout.codex_upgrade_supervisor,
                    "_read_state",
                    return_value={
                        "campaign_id": payload["campaign_id"],
                        "phase": "bootstrap",
                        "state": "stopped",
                    },
                ),
            ):
                source = closeout._validate_managed_tool_deploy(
                    receipt_path,
                    {"tool_identity": current_identity},
                )
            self.assertEqual(source.role, "managed_tool_deploy")
            self.assertEqual(source.sha256, closeout._sha256_file(receipt_path))

    def _timing_arm_manifest(
        self,
        root: Path,
        *,
        started_at: str,
    ) -> tuple[Path, dict[str, object]]:
        ledger_root = root / "ledger"
        timing.create_ledger(
            ledger_root,
            upgrade_id="upgrade-0154",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
            started_at_utc=started_at,
        )
        checkpoint = timing.checkpoint(ledger_root, "receipts/preflight.json")
        checkpoint_path = ledger_root / "receipts" / "preflight.json"
        arm_root = root / "arm"
        arm_path = create_arm_receipt(
            arm_root,
            phase="p0",
            subject_id="upgrade-0154",
            prefix="p0",
        )
        arm_receipt = json.loads(arm_path.read_text(encoding="utf-8"))
        manifest: dict[str, object] = {
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "campaign_purpose": "production_replacement",
            "control_receipts": {
                "upgrade_timing": {
                    "ledger_dir": str(ledger_root),
                    "ledger_plan_sha256": closeout._sha256_file(
                        ledger_root / "ledger.json"
                    ),
                    "receipt": {
                        "path": "receipts/preflight.json",
                        "sha256": closeout._sha256_file(checkpoint_path),
                        "bytes": checkpoint_path.stat().st_size,
                    },
                    "upgrade_id": "upgrade-0154",
                    "evidence_decision": "recapture",
                    "checkpoint_head_sha256": checkpoint["summary"]["head_sha256"],
                },
                "arm64_environment": {
                    "evidence_root": str(arm_root),
                    "receipt": {
                        "path": arm_path.name,
                        "sha256": closeout._sha256_file(arm_path),
                        "bytes": arm_path.stat().st_size,
                    },
                    "subject_id": "upgrade-0154",
                    "contract_sha256": arm_receipt["contract_sha256"],
                    "continuity_identity_sha256": arm_receipt[
                        "continuity_identity_sha256"
                    ],
                },
            },
        }
        return ledger_root, manifest

    def test_ledger_must_be_active_vc0(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            ledger_root, manifest = self._timing_arm_manifest(
                root,
                started_at=(now - timedelta(minutes=1)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="vc0-already-complete",
                phase="VC-0",
                event_type="stage_completed",
            )
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "active VC-0",
            ):
                closeout._validate_timing_and_arm64(
                    root,
                    manifest,
                    now=now.isoformat(),
                )

    def test_ledger_requires_at_least_five_minutes_remaining(self) -> None:
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=40, seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            _ledger_root, manifest = self._timing_arm_manifest(
                root,
                started_at=started.isoformat(),
            )
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "少于 300 秒",
            ):
                closeout._validate_timing_and_arm64(
                    root,
                    manifest,
                    now=now.isoformat(),
                )

    def test_restarted_vc0_accepts_frozen_historical_request_total_only(self) -> None:
        """新 VC-0 保留历史请求累计，但冻结后新增请求必须失败关闭。"""

        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            ledger_root, manifest = self._timing_arm_manifest(
                root,
                started_at=(now - timedelta(minutes=2)).isoformat(),
            )
            timing.append_event(
                ledger_root,
                event_id="vc0-complete-before-restart",
                phase="VC-0",
                event_type="stage_completed",
            )
            timing.append_event(
                ledger_root,
                event_id="vc1-start-before-restart",
                phase="VC-1",
                event_type="stage_started",
            )
            timing.append_event(
                ledger_root,
                event_id="vc1-live-accounting-before-restart",
                phase="VC-1",
                event_type="receipt_passed",
                live_request_count=8,
            )
            timing.append_event(
                ledger_root,
                event_id="vc1-abandoned-before-restart",
                phase="VC-1",
                event_type="stage_abandoned",
                root_cause_id="control-tool-gap",
                next_action="修复控制工具后从新 VC-0 承接",
            )
            timing.append_event(
                ledger_root,
                event_id="vc0-restarted",
                phase="VC-0",
                event_type="stage_started",
            )
            checkpoint = timing.checkpoint(
                ledger_root,
                "receipts/restarted-vc0.json",
            )
            checkpoint_path = ledger_root / "receipts/restarted-vc0.json"
            timing_control = manifest["control_receipts"]["upgrade_timing"]
            timing_control["receipt"] = {
                "path": "receipts/restarted-vc0.json",
                "sha256": closeout._sha256_file(checkpoint_path),
                "bytes": checkpoint_path.stat().st_size,
            }
            timing_control["checkpoint_head_sha256"] = checkpoint["summary"][
                "head_sha256"
            ]

            _timing_root, _arm_root, _arm_path, summary, _source = (
                closeout._validate_timing_and_arm64(
                    root,
                    manifest,
                    now=(now + timedelta(seconds=1)).isoformat(),
                )
            )
            self.assertEqual(summary["active_phase"], "VC-0")
            self.assertEqual(summary["total_live_request_count"], 8)

            timing.append_event(
                ledger_root,
                event_id="unexpected-live-after-vc0-freeze",
                phase="VC-0",
                event_type="receipt_passed",
                live_request_count=1,
            )
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "active VC-0"):
                closeout._validate_timing_and_arm64(
                    root,
                    manifest,
                    now=(now + timedelta(seconds=2)).isoformat(),
                )

    def test_atomic_rehearsal_is_replayed_in_capture_container(self) -> None:
        """宿主收口必须在 capture-cli 内重放同源 atomic-double 收据。"""

        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            preflight = data_root / "evidence/campaigns/preflight-0154"
            atomic_root = data_root / "staging/atomic-double"
            preflight.mkdir(parents=True, mode=0o700)
            atomic_root.mkdir(parents=True, mode=0o700)
            data_root.chmod(0o700)
            (data_root / "evidence").chmod(0o700)
            (data_root / "evidence/campaigns").chmod(0o700)
            (data_root / "staging").chmod(0o700)
            receipt = self._write(atomic_root / "receipt.json", {"atomic": True})
            completed = mock.Mock(
                returncode=0,
                stdout=(
                    b'{"campaign_id": null, "live_request_count": 0, '
                    b'"status": "passed"}\n'
                ),
                stderr=b"",
            )
            with mock.patch.object(
                closeout.subprocess,
                "run",
                return_value=completed,
            ) as run:
                source = closeout._validate_atomic_campaign_run_rehearsal(
                    atomic_root,
                    Path("receipt.json"),
                    preflight,
                    {"configuration": {"capture_container": "capture-cli"}},
                )
            self.assertEqual(source.role, "atomic_campaign_run_rehearsal")
            self.assertEqual(source.sha256, closeout._sha256_file(receipt))
            command = run.call_args.args[0]
            self.assertEqual(command[0:2], ["docker", "exec"])
            self.assertIn("/capture/staging/atomic-double", command)
            self.assertIn("atomic-double-replay", command)

    def test_atomic_rehearsal_rejects_failed_container_replay(self) -> None:
        """容器重放失败时不得把 atomic-double 文件仅按摘要放行。"""

        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            preflight = data_root / "evidence/campaigns/preflight-0154"
            atomic_root = data_root / "staging/atomic-double"
            preflight.mkdir(parents=True, mode=0o700)
            atomic_root.mkdir(parents=True, mode=0o700)
            data_root.chmod(0o700)
            (data_root / "evidence").chmod(0o700)
            (data_root / "evidence/campaigns").chmod(0o700)
            (data_root / "staging").chmod(0o700)
            self._write(atomic_root / "receipt.json", {"atomic": True})
            completed = mock.Mock(returncode=1, stdout=b"", stderr=b"tampered")
            with (
                mock.patch.object(
                    closeout.subprocess,
                    "run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(closeout.VC0CloseoutError, "容器重放失败"),
            ):
                closeout._validate_atomic_campaign_run_rehearsal(
                    atomic_root,
                    Path("receipt.json"),
                    preflight,
                    {"configuration": {"capture_container": "capture-cli"}},
                )

    def test_existing_formal_path_fails_before_receipt_or_event_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            self._prepare_output_parents(root)
            formal = root / "campaigns" / "formal-0154"
            formal.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "Formal Campaign 路径",
            ):
                closeout._precheck_outputs(
                    validated,
                    formal_campaign_dir=formal,
                    formal_campaign_id="formal-0154",
                    supervisor_state_dir=root / "control" / "vc1",
                )
            self.assertEqual(
                timing.inspect_ledger(validated.timing_ledger_dir)["head_sequence"],
                1,
            )

    def test_invalid_request_after_audit_creation_preserves_diagnostic(self) -> None:
        """请求字段早期失败也必须留下不可覆盖诊断。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            self._prepare_output_parents(root)
            arguments = self._arguments(root)
            arguments.formal_campaign_id = "bad/id"
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "安全标识"):
                closeout.closeout(arguments)
            diagnostic = json.loads(
                (Path(arguments.audit_dir) / "failure.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(diagnostic["failed_step"], "validate-request")
            self.assertIsNone(diagnostic["timing_failure_closure"])
            self.assertFalse((Path(arguments.audit_dir) / "request.json").exists())

    def test_symlinked_receipt_namespace_and_lock_are_rejected(self) -> None:
        """收据命名空间和账本锁均不得跟随最终符号链接。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            self._prepare_output_parents(root)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            namespace = validated.timing_ledger_dir / "receipts" / "vc0-closeout"
            namespace.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "命名空间"):
                closeout._precheck_outputs(
                    validated,
                    formal_campaign_dir=root / "campaigns" / "formal-0154",
                    formal_campaign_id="formal-0154",
                    supervisor_state_dir=root / "control" / "vc1",
                )
            namespace.unlink()
            lock_target = self._write(root / "lock-target", {"lock": True})
            (validated.timing_ledger_dir / ".vc0-closeout.lock").symlink_to(
                lock_target
            )
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "锁文件"):
                with closeout._ledger_lock(validated.timing_ledger_dir):
                    self.fail("符号链接锁不应被取得")

    def test_timing_head_drift_under_lock_fails_before_any_closeout_event(self) -> None:
        """输入校验与取得锁之间的账本事件漂移必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            self._prepare_output_parents(root)
            arguments = self._arguments(root)
            drifted = False

            @contextmanager
            def drifting_lock(_root: Path):
                nonlocal drifted
                if not drifted:
                    timing.append_event(
                        validated.timing_ledger_dir,
                        event_id="concurrent-p0-observation",
                        phase="VC-0",
                        event_type="receipt_passed",
                    )
                    drifted = True
                yield

            with (
                mock.patch.object(closeout, "validate_inputs", return_value=validated),
                mock.patch.object(closeout, "_ledger_lock", drifting_lock),
                self.assertRaisesRegex(closeout.VC0CloseoutError, "并发漂移"),
            ):
                closeout.closeout(arguments)
            summary = timing.inspect_ledger(validated.timing_ledger_dir)
            self.assertEqual(summary["head_sequence"], 2)
            self.assertEqual(summary["active_phase"], "VC-0")
            diagnostic = json.loads(
                (Path(arguments.audit_dir) / "failure.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                diagnostic["timing_failure_closure"]["status"],
                "not-required",
            )

    def test_duplicate_receipt_directory_or_event_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            self._prepare_output_parents(root)
            receipt_root = (
                validated.timing_ledger_dir
                / "receipts"
                / "vc0-closeout"
                / "formal-0154"
            )
            receipt_root.mkdir(parents=True, mode=0o700)
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "禁止重复写入",
            ):
                closeout._precheck_outputs(
                    validated,
                    formal_campaign_dir=root / "campaigns" / "formal-0154",
                    formal_campaign_id="formal-0154",
                    supervisor_state_dir=root / "control" / "vc1",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validated = self._synthetic_validated(root)
            self._prepare_output_parents(root)
            timing.append_event(
                validated.timing_ledger_dir,
                event_id="formal-0154-p0-receipts-passed",
                phase="VC-0",
                event_type="receipt_passed",
            )
            with self.assertRaisesRegex(
                closeout.VC0CloseoutError,
                "event_id 已存在",
            ):
                closeout._precheck_outputs(
                    validated,
                    formal_campaign_dir=root / "campaigns" / "formal-0154",
                    formal_campaign_id="formal-0154",
                    supervisor_state_dir=root / "control" / "vc1",
                )

    def _run_closeout_with_campaign_result(
        self,
        root: Path,
        campaign_result: object,
    ) -> tuple[
        argparse.Namespace,
        closeout.ValidatedInputs,
        mock.Mock,
        mock.Mock,
        dict[str, object] | None,
    ]:
        validated = self._synthetic_validated(root)
        self._prepare_output_parents(root)
        arguments = self._arguments(root)
        formal_manifest: dict[str, object] | None = None

        def create(arguments_value: argparse.Namespace) -> dict[str, object]:
            nonlocal formal_manifest
            arguments_value.campaign_dir.mkdir(mode=0o700)
            formal_manifest = self._fake_formal_manifest(
                arguments_value.campaign_dir,
                arguments_value.campaign_id,
            )
            return formal_manifest

        create_mock = mock.Mock(side_effect=create)
        campaign_mock = mock.Mock()
        if isinstance(campaign_result, BaseException):
            campaign_mock.side_effect = campaign_result
        else:
            campaign_mock.return_value = campaign_result
        with (
            mock.patch.object(closeout, "validate_inputs", return_value=validated),
            mock.patch.object(
                closeout.codex_upgrade,
                "create_campaign",
                create_mock,
            ),
            mock.patch.object(
                closeout.codex_upgrade,
                "load_campaign_manifest",
                side_effect=lambda _path: formal_manifest,
            ),
            mock.patch.object(
                closeout.codex_upgrade_supervisor,
                "_campaign_run_command",
                campaign_mock,
            ),
        ):
            result = closeout.closeout(arguments)
        return arguments, validated, create_mock, campaign_mock, result

    def test_campaign_run_not_started_or_failed_preserves_diagnostic(self) -> None:
        failures = (
            closeout.codex_upgrade_supervisor.SupervisorError("未能启动"),
            (1, {"status": "failed", "reason": "action-failed:capture"}),
        )
        for index, failure in enumerate(failures, 1):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(closeout.VC0CloseoutError):
                    self._run_closeout_with_campaign_result(root, failure)
                diagnostic_path = root / "audit" / "closeout-0154" / "failure.json"
                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["failed_step"], "dispatch-vc1")
                self.assertFalse(diagnostic["deadline_extended"])
                self.assertFalse(diagnostic["cleanup_performed"])
                self.assertTrue(diagnostic["formal_campaign_path_exists"])
                self.assertEqual(
                    diagnostic["timing_failure_closure"]["status"],
                    "stage-abandoned-recorded",
                )
                self.assertEqual(
                    timing.inspect_ledger(root / "timing")["active_phase"],
                    None,
                )

    def test_success_calls_create_and_campaign_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments, validated, create_mock, campaign_mock, result = (
                self._run_closeout_with_campaign_result(
                    root,
                    (
                        0,
                        {
                            "status": "stopped",
                            "reason": "queue-complete",
                            "run_dir": str(root / "control" / "vc1-supervisor" / "run-x"),
                            "actions": [],
                        },
                    ),
                )
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["create_campaign_call_count"], 1)
            self.assertEqual(result["campaign_run_call_count"], 1)
            create_mock.assert_called_once()
            campaign_mock.assert_called_once()
            self.assertTrue((Path(arguments.audit_dir) / "receipt.json").is_file())
            self.assertFalse((Path(arguments.audit_dir) / "failure.json").exists())
            summary = timing.inspect_ledger(validated.timing_ledger_dir)
            self.assertEqual(summary["active_phase"], "VC-1")
            self.assertEqual(summary["head_sequence"], 4)
            copied = (
                validated.timing_ledger_dir
                / "receipts"
                / "vc0-closeout"
                / arguments.formal_campaign_id
            )
            self.assertEqual(
                {path.name for path in copied.iterdir()},
                {
                    "arm64_environment.json",
                    "atomic_campaign_run_rehearsal.json",
                    "campaign_run_rehearsal.json",
                    "formal-campaign-plan.json",
                    "formal-vc0-checkpoint.json",
                    "job_rehearsal.json",
                    "managed_tool_deploy.json",
                    "p0_gate.json",
                    "timing-vc0-active.json",
                },
            )


if __name__ == "__main__":
    unittest.main()
