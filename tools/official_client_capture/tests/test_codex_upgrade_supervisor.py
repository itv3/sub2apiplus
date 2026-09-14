"""独立监督器的收口、心跳超时和离线审计回归测试。"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.codex_upgrade_supervisor import (
    SupervisorClient,
    SupervisorError,
    _audit_command,
    _campaign_run_manifest,
    build_campaign_run_manifest,
    main,
)


# worker 丢失／会话挂断必须在心跳失联判定（DEFAULT_HEARTBEAT_SECONDS × 1.25 = 6.25 秒）之前被检测到。
# 2 秒既能证明走的是即时检测路径，又给 CI runner 的进程调度留出余量；0.5 秒曾在 CI 上以 2.6 毫秒之差误判。
IMMEDIATE_DETECTION_SECONDS = 2.0


class SupervisorTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    @staticmethod
    def _write_permission_compensation_events(
        path: Path,
        *,
        campaign_id: str,
        owner_pid: int,
        owner_nonce: str,
        prepare_status: str = "passed",
    ) -> None:
        """生成只覆盖权限补偿判定所需动作的完整摘要链。"""

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        specifications = [
            (
                "action-started",
                "prepare-official-assertion-bundle",
                "VC-1:prepare-official-assertion-bundle",
                "running",
                None,
                {},
            ),
            (
                "action-finished",
                "prepare-official-assertion-bundle",
                "VC-1:prepare-official-assertion-bundle",
                prepare_status,
                None,
                {"duration_seconds": 1.0, "returncode": 0},
            ),
            (
                "action-started",
                "seal-official-preview",
                "VC-1:capture-official-seal-preview",
                "running",
                None,
                {},
            ),
            (
                "action-failed",
                "seal-official-preview",
                "VC-1:capture-official-seal-preview",
                "failed",
                "returncode=1",
                {"duration_seconds": 1.0},
            ),
        ]
        records: list[dict[str, object]] = []
        previous: str | None = None
        for sequence, specification in enumerate(specifications, 1):
            event_type, job_id, operation, status, reason, metadata = specification
            unsigned: dict[str, object] = {
                "schema_version": supervisor.EVENT_SCHEMA,
                "sequence": sequence,
                "recorded_at_utc": f"2026-09-14T11:06:{18 + sequence:02d}.000Z",
                "recorded_at_epoch": 1000.0 + sequence,
                "event_type": event_type,
                "operation": operation,
                "campaign_id": campaign_id,
                "phase": "VC-1",
                "owner_pid": owner_pid,
                "owner_nonce": owner_nonce,
                "job_id": job_id,
                "status": status,
                "reason": reason,
                "started_at_epoch": 1000.0 + sequence,
                "ended_at_epoch": (
                    None if event_type == "action-started" else 1000.5 + sequence
                ),
                "metadata": metadata,
                "previous_event_sha256": previous,
            }
            event = dict(unsigned)
            digest = supervisor._sha256(supervisor._canonical(unsigned))
            event["event_sha256"] = digest
            records.append(event)
            previous = digest
        path.write_bytes(b"".join(supervisor._canonical(value) for value in records))
        path.chmod(0o600)

    @staticmethod
    def _write_permission_alias_failure_events(
        path: Path,
        *,
        campaign_id: str,
        owner_pid: int,
        owner_nonce: str,
    ) -> None:
        """生成 sequence 3 首动作失败且 seal 从未启动的完整摘要链。"""

        specifications = [
            ("command-started", None, "supervisor:start", "running", None),
            (
                "action-started",
                "harden-official-evidence-permissions",
                "VC-1:harden-official-evidence-permissions",
                "running",
                None,
            ),
            (
                "action-failed",
                "harden-official-evidence-permissions",
                "VC-1:harden-official-evidence-permissions",
                "failed",
                "returncode=1",
            ),
            (
                "stop-requested",
                None,
                "supervisor:stop-request",
                "stopping",
                "action-failed:harden-official-evidence-permissions",
            ),
            (
                "failed",
                None,
                "supervisor:stop",
                "failed",
                "action-failed:harden-official-evidence-permissions",
            ),
        ]
        records: list[dict[str, object]] = []
        previous: str | None = None
        for sequence, specification in enumerate(specifications, 1):
            event_type, job_id, operation, status, reason = specification
            unsigned: dict[str, object] = {
                "schema_version": supervisor.EVENT_SCHEMA,
                "sequence": sequence,
                "recorded_at_utc": f"2026-09-14T12:31:{12 + sequence:02d}.000Z",
                "recorded_at_epoch": 2000.0 + sequence,
                "event_type": event_type,
                "operation": operation,
                "campaign_id": campaign_id,
                "phase": "VC-1",
                "owner_pid": owner_pid,
                "owner_nonce": owner_nonce,
                "job_id": job_id,
                "status": status,
                "reason": reason,
                "started_at_epoch": (
                    2000.0 + sequence if event_type == "action-started" else None
                ),
                "ended_at_epoch": (
                    2000.5 + sequence if event_type == "action-failed" else None
                ),
                "metadata": (
                    {"duration_seconds": 0.2}
                    if event_type == "action-failed"
                    else {}
                ),
                "previous_event_sha256": previous,
            }
            event = dict(unsigned)
            digest = supervisor._sha256(supervisor._canonical(unsigned))
            event["event_sha256"] = digest
            records.append(event)
            previous = digest
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(b"".join(supervisor._canonical(value) for value in records))
        path.chmod(0o600)

    def _permission_compensation_fixture(
        self,
        root: Path,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        Path,
        dict[str, object],
        dict[str, str],
    ]:
        """建立可离线验证的一次性 v2→v2 权限补偿夹具。"""

        root = root.resolve(strict=True)
        campaign_id = supervisor.VC1_PERMISSION_COMPENSATION_CAMPAIGN_ID
        attempt_id = supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
        data_root = root / "data"
        campaign_dir = data_root / "evidence" / "campaigns" / campaign_id
        attempt_root = campaign_dir / "official" / "attempts" / attempt_id
        assertion_root = attempt_root / "evidence" / "assertion-bundle"
        logs_root = attempt_root / "logs"
        assertion_root.mkdir(parents=True, mode=0o700)
        assertion_root.chmod(0o700)
        logs_root.mkdir(parents=True, mode=0o700)
        logs_root.chmod(0o700)
        self._write_json(
            assertion_root / "capture-manifest.json",
            {"fixture": True},
        )

        external_roots: list[Path] = []
        for index in range(30):
            evidence_root = root / "evidence-roots" / f"root-{index:02d}"
            evidence_root.mkdir(parents=True, mode=0o700)
            evidence_root.chmod(0o700)
            external_roots.append(evidence_root)
        failed_root = external_roots[0] / "direct"
        failed_root.mkdir(mode=0o700)
        failed_root.chmod(0o755)
        evidence_roots = [
            *(str(value) for value in external_roots),
            str(attempt_root / "evidence"),
            str(logs_root),
        ]
        results: list[dict[str, object]] = []
        for index in range(29):
            assigned = [str(external_roots[index])]
            if index == 0:
                assigned.append(str(external_roots[-1]))
            results.append(
                {
                    "job_id": f"job-{index:02d}",
                    "status": "complete",
                    "evidence_roots": assigned,
                }
            )
        attempt = {
            "campaign_id": campaign_id,
            "attempt_id": attempt_id,
            "status": "awaiting_receipts",
            "results": results,
            "evidence_roots": evidence_roots,
        }
        attempt_path = attempt_root / "attempt.json"
        self._write_json(attempt_path, attempt)
        attempt_sha256 = hashlib.sha256(attempt_path.read_bytes()).hexdigest()
        roots_sha256 = hashlib.sha256(
            json.dumps(
                evidence_roots,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        seal_command = [
            "/usr/bin/python3",
            str(data_root / "tools/official_client_capture/codex_upgrade.py"),
            "capture-official",
            "seal",
            "--campaign-dir",
            str(campaign_dir),
            "--attempt-id",
            attempt_id,
            "--capture-manifest",
            str(assertion_root / "capture-manifest.json"),
            "--assertion-evidence-root",
            str(assertion_root),
            "--max-wall-seconds",
            "1440",
            "--heartbeat-seconds",
            "5",
        ]
        prepare_action = {
            "action_id": "prepare-official-assertion-bundle",
            "operation": "VC-1:prepare-official-assertion-bundle",
            "timeout_seconds": 300.0,
            "command": [
                "/usr/bin/env",
                f"CAMPAIGN_DIR={campaign_dir}",
                f"ATTEMPT_ID={attempt_id}",
                "SIDE=official",
                f"REPO_ROOT={data_root}",
                f"TOOL_ROOT={data_root / 'tools/official_client_capture'}",
                "/usr/bin/bash",
                str(data_root / "tools/prepare_assertion_bundle.sh"),
            ],
            "item_ids": ["prepare-official-assertion-bundle"],
        }
        seal_action = {
            "action_id": "seal-official-preview",
            "operation": "VC-1:capture-official-seal-preview",
            "timeout_seconds": 1500.0,
            "command": seal_command,
            "item_ids": ["seal-official-preview"],
        }
        checkpoint = {
            "path": "control/vc/vc-0-checkpoint.json",
            "sha256": "3" * 64,
            "phase": "VC-0",
            "checkpoint_sha256": "4" * 64,
        }
        prior_manifest: dict[str, object] = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": campaign_id,
            "campaign_plan_sha256": (
                supervisor.VC1_PERMISSION_COMPENSATION_CAMPAIGN_PLAN_SHA256
            ),
            "batch_id": "vc-1-0002",
            "batch_sequence": 2,
            "batch_sha256": (
                supervisor.VC1_PERMISSION_COMPENSATION_FAILED_BATCH_SHA256
            ),
            "phase": "VC-1",
            "predecessor_checkpoint": checkpoint,
            "original_deadline_at_utc": "2099-09-14T12:00:00Z",
            "no_op": False,
            "actions": [prepare_action, seal_action],
            "execute_items": [
                "prepare-official-assertion-bundle",
                "seal-official-preview",
            ],
            "reuse_items": [],
        }

        action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"
        action_input_dir.mkdir(parents=True, mode=0o700)
        action_input_dir.chmod(0o700)
        helper_path = action_input_dir / "vc1_evidence_permission_closeout.py"
        helper_path.write_text("# fixture helper\n", encoding="utf-8")
        helper_path.chmod(0o600)
        helper_sha256 = hashlib.sha256(helper_path.read_bytes()).hexdigest()
        receipt_path = action_input_dir / "sequence3-permission-closeout-receipt.json"
        harden_action = {
            "action_id": "harden-official-evidence-permissions",
            "operation": "VC-1:harden-official-evidence-permissions",
            "timeout_seconds": 120.0,
            "command": [
                "/usr/bin/python3",
                str(helper_path),
                "--campaign-id",
                campaign_id,
                "--attempt-id",
                attempt_id,
                "--attempt",
                str(attempt_path),
                "--attempt-sha256",
                attempt_sha256,
                "--roots-sha256",
                roots_sha256,
                "--self-sha256",
                helper_sha256,
                "--receipt",
                str(receipt_path),
            ],
            "item_ids": ["harden-official-evidence-permissions"],
        }
        successor_manifest: dict[str, object] = {
            **prior_manifest,
            "batch_id": "vc-1-0003",
            "batch_sequence": 3,
            "batch_sha256": (
                supervisor.VC1_PERMISSION_COMPENSATION_SUCCESSOR_BATCH_SHA256
            ),
            "actions": [harden_action, seal_action],
            "execute_items": [
                "harden-official-evidence-permissions",
                "seal-official-preview",
            ],
            "reuse_items": ["prepare-official-assertion-bundle"],
        }
        action_plan_path = action_input_dir / "vc1-sequence3-action-plan.json"
        self._write_json(
            action_plan_path,
            {
                "schema_version": "codex-upgrade-vc-action-plan/v1",
                "execute_item_ids": successor_manifest["execute_items"],
                "reuse_item_ids": successor_manifest["reuse_items"],
                "actions": successor_manifest["actions"],
            },
        )
        action_plan_sha256 = hashlib.sha256(action_plan_path.read_bytes()).hexdigest()

        prior_dir = root / "run-failed-v2"
        prior_dir.mkdir(mode=0o700)
        prior_dir.chmod(0o700)
        prior_state: dict[str, object] = {
            "state": "failed",
            "campaign_id": campaign_id,
            "phase": "VC-1",
            "owner_pid": os.getpid(),
            "owner_nonce": "8" * 64,
            "terminal_at_utc": "2026-09-14T11:06:29.796Z",
        }
        self._write_json(prior_dir / "state.json", prior_state)
        self._write_json(
            prior_dir / "campaign-run-manifest.json",
            {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "manifest": prior_manifest,
                "manifest_sha256": supervisor._sha256(
                    supervisor._canonical(prior_manifest)
                ),
            },
        )
        stop: dict[str, object] = {
            "schema_version": supervisor.STOP_SCHEMA,
            "campaign_id": campaign_id,
            "detected_at_epoch": 1005.0,
            "detected_at_utc": "2026-09-14T11:06:29.784Z",
            "event_type": "failed",
            "owner_nonce": prior_state["owner_nonce"],
            "owner_pid": prior_state["owner_pid"],
            "phase": "VC-1",
            "reason": "action-failed:seal-official-preview",
        }
        stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
        self._write_json(prior_dir / "stop-receipt.json", stop)
        self._write_permission_compensation_events(
            prior_dir / "events.ndjson",
            campaign_id=campaign_id,
            owner_pid=os.getpid(),
            owner_nonce=str(prior_state["owner_nonce"]),
        )
        diagnostic_path = supervisor._action_diagnostic_path(
            prior_dir,
            "seal-official-preview",
            create_directory=True,
        )
        diagnostic = supervisor._write_action_diagnostic(
            diagnostic_path,
            campaign_id=campaign_id,
            phase="VC-1",
            action_id="seal-official-preview",
            owner_pid=os.getpid(),
            owner_nonce=str(prior_state["owner_nonce"]),
            failure_kind="handled-error",
            error_type="ConfigurationError",
            message=(
                "seal 廉价前检失败（scanned_bytes=0）：证据目录向 group/other 开放，"
                f"必须先修正权限：{failed_root}"
            ),
        )
        bindings = {
            "VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256": attempt_sha256,
            "VC1_PERMISSION_COMPENSATION_ROOTS_SHA256": roots_sha256,
            "VC1_PERMISSION_COMPENSATION_HELPER_SHA256": helper_sha256,
            "VC1_PERMISSION_COMPENSATION_ACTION_PLAN_SHA256": action_plan_sha256,
            "VC1_PERMISSION_COMPENSATION_DIAGNOSTIC_SHA256": str(
                diagnostic["diagnostic_sha256"]
            ),
        }
        return prior_state, prior_manifest, prior_dir, successor_manifest, bindings

    def _permission_alias_fixture(
        self,
        root: Path,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        Path,
        dict[str, object],
        dict[str, str | int | Path],
        tuple[dict[str, object], dict[str, object], Path],
    ]:
        """在原权限补偿夹具上建立唯一 sequence 3→4 失败后继。"""

        sequence_two_state, sequence_two, sequence_two_dir, sequence_three, bindings = (
            self._permission_compensation_fixture(root)
        )
        campaign_id = str(sequence_three["campaign_id"])
        campaign_dir = Path(
            sequence_three["actions"][1]["command"][
                sequence_three["actions"][1]["command"].index("--campaign-dir") + 1
            ]
        )
        data_root = campaign_dir.parents[2]
        attempt_path = (
            campaign_dir
            / "official/attempts"
            / supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
            / "attempt.json"
        )
        failed_run_name = "run-" + "d" * 64
        sequence_three_dir = (
            root / f"{campaign_id}-supervisor" / failed_run_name
        )
        sequence_three_dir.mkdir(parents=True, mode=0o700)
        sequence_three_dir.chmod(0o700)
        sequence_three_state: dict[str, object] = {
            "state": "failed",
            "campaign_id": campaign_id,
            "phase": "VC-1",
            "owner_pid": os.getpid(),
            "owner_nonce": failed_run_name.removeprefix("run-"),
            "terminal_at_utc": "2026-09-14T12:31:18.169Z",
        }
        self._write_json(sequence_three_dir / "state.json", sequence_three_state)
        self._write_json(
            sequence_three_dir / "campaign-run-manifest.json",
            {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "manifest": sequence_three,
                "manifest_sha256": supervisor._sha256(
                    supervisor._canonical(sequence_three)
                ),
            },
        )
        stop: dict[str, object] = {
            "schema_version": supervisor.STOP_SCHEMA,
            "campaign_id": campaign_id,
            "detected_at_epoch": 2006.0,
            "detected_at_utc": "2026-09-14T12:31:18.160Z",
            "event_type": "failed",
            "owner_nonce": sequence_three_state["owner_nonce"],
            "owner_pid": sequence_three_state["owner_pid"],
            "phase": "VC-1",
            "reason": "action-failed:harden-official-evidence-permissions",
        }
        stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
        self._write_json(sequence_three_dir / "stop-receipt.json", stop)
        self._write_permission_alias_failure_events(
            sequence_three_dir / "events.ndjson",
            campaign_id=campaign_id,
            owner_pid=os.getpid(),
            owner_nonce=str(sequence_three_state["owner_nonce"]),
        )
        diagnostic_path = supervisor._action_diagnostic_path(
            sequence_three_dir,
            "harden-official-evidence-permissions",
            create_directory=True,
        )
        diagnostic = supervisor._write_action_diagnostic(
            diagnostic_path,
            campaign_id=campaign_id,
            phase="VC-1",
            action_id="harden-official-evidence-permissions",
            owner_pid=os.getpid(),
            owner_nonce=str(sequence_three_state["owner_nonce"]),
            failure_kind="child-returncode",
            error_type="ChildProcessError",
            message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
        )

        helper_path = (
            data_root
            / "tools/official_client_capture/codex_upgrade_vc1_permission_alias_closeout.py"
        )
        helper_path.parent.mkdir(parents=True, mode=0o700)
        helper_path.write_text("# alias fixture helper\n", encoding="utf-8")
        helper_path.chmod(0o600)
        helper_sha256 = hashlib.sha256(helper_path.read_bytes()).hexdigest()
        deployment_path = (
            data_root / "control/codex-0154-supervisor-enable-fixture.json"
        )
        tool_files_sha256 = "a" * 64
        self._write_json(
            deployment_path,
            {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "architecture": "aarch64",
                "production_tool_root": str(helper_path.parent),
                "tool_files_sha256": tool_files_sha256,
                "supervisor_sha256": hashlib.sha256(
                    Path(supervisor.__file__).read_bytes()
                ).hexdigest(),
            },
        )
        deployment_sha256 = hashlib.sha256(deployment_path.read_bytes()).hexdigest()
        action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"
        receipt_path = (
            action_input_dir / "sequence4-permission-alias-closeout-receipt.json"
        )
        readonly_runs_root = root / "readonly-runs"
        alias_action = {
            "action_id": "harden-official-evidence-permissions-via-alias",
            "operation": "VC-1:harden-official-evidence-permissions-via-alias",
            "timeout_seconds": 180.0,
            "command": [
                "/usr/bin/python3",
                str(helper_path),
                "--campaign-id",
                campaign_id,
                "--attempt-id",
                supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID,
                "--attempt",
                str(attempt_path),
                "--attempt-sha256",
                str(bindings["VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256"]),
                "--roots-sha256",
                str(bindings["VC1_PERMISSION_COMPENSATION_ROOTS_SHA256"]),
                "--self-sha256",
                helper_sha256,
                "--readonly-runs-root",
                str(readonly_runs_root),
                "--writable-runs-root",
                str(data_root / "runs"),
                "--deployment-receipt",
                str(deployment_path),
                "--deployment-receipt-sha256",
                deployment_sha256,
                "--tool-files-sha256",
                tool_files_sha256,
                "--receipt",
                str(receipt_path),
            ],
            "item_ids": ["harden-official-evidence-permissions-via-alias"],
        }
        successor: dict[str, object] = {
            **sequence_three,
            "batch_id": "vc-1-0004",
            "batch_sequence": 4,
            "batch_sha256": "b" * 64,
            "actions": [alias_action, sequence_three["actions"][1]],
            "execute_items": [
                "harden-official-evidence-permissions-via-alias",
                "seal-official-preview",
            ],
            "reuse_items": ["prepare-official-assertion-bundle"],
        }
        self._write_json(
            action_input_dir / "vc1-sequence4-action-plan.json",
            {
                "schema_version": "codex-upgrade-vc-action-plan/v1",
                "execute_item_ids": successor["execute_items"],
                "reuse_item_ids": successor["reuse_items"],
                "actions": successor["actions"],
            },
        )
        bindings.update(
            {
                "VC1_PERMISSION_ALIAS_FAILED_RUN_NAME": failed_run_name,
                "VC1_PERMISSION_ALIAS_STATE_FILE_SHA256": hashlib.sha256(
                    (sequence_three_dir / "state.json").read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_MANIFEST_FILE_SHA256": hashlib.sha256(
                    (sequence_three_dir / "campaign-run-manifest.json").read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_STOP_FILE_SHA256": hashlib.sha256(
                    (sequence_three_dir / "stop-receipt.json").read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_EVENTS_FILE_SHA256": hashlib.sha256(
                    (sequence_three_dir / "events.ndjson").read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_DIAGNOSTIC_FILE_SHA256": hashlib.sha256(
                    diagnostic_path.read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_FAILURE_DIAGNOSTIC_SHA256": str(
                    diagnostic["diagnostic_sha256"]
                ),
                "VC1_PERMISSION_ALIAS_HELPER_SHA256": helper_sha256,
                "VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT": readonly_runs_root,
            }
        )
        return (
            sequence_three_state,
            sequence_three,
            sequence_three_dir,
            successor,
            bindings,
            (sequence_two_state, sequence_two, sequence_two_dir),
        )

    def _permission_alias_predispatch_fixture(
        self,
        root: Path,
    ) -> dict[str, object]:
        """建立 sequence 4 未创建父 run 时唯一 sequence 5 的完整夹具。"""

        (
            sequence_three_state,
            sequence_three,
            sequence_three_dir,
            sequence_four,
            bindings,
            sequence_two,
        ) = self._permission_alias_fixture(root)
        campaign_id = str(sequence_four["campaign_id"])
        campaign_dir = Path(
            sequence_four["actions"][1]["command"][
                sequence_four["actions"][1]["command"].index("--campaign-dir") + 1
            ]
        )
        data_root = campaign_dir.parents[2]
        action_input_dir = data_root / "control" / f"{campaign_id}-action-inputs"

        compiled_at = "2000-01-01T00:00:00+00:00"
        must_start_by = "2000-01-01T00:01:00+00:00"
        sequence_four_batch_path = (
            campaign_dir / "control/vc/batches/0004-vc-1.json"
        )
        self._write_json(
            sequence_four_batch_path,
            {
                "sequence": 4,
                "batch_id": "vc-1-0004",
                "batch_sha256": sequence_four["batch_sha256"],
                "compiled_at_utc": compiled_at,
                "must_start_by_utc": must_start_by,
            },
        )
        sequence_four_manifest_path = (
            campaign_dir / "control/vc/run-manifests/0004-vc-1.json"
        )
        self._write_json(sequence_four_manifest_path, sequence_four)
        sequence_four_action_plan_path = (
            action_input_dir / "vc1-sequence4-action-plan.json"
        )

        sequence_one_manifest: dict[str, object] = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": campaign_id,
            "campaign_plan_sha256": sequence_four["campaign_plan_sha256"],
            "batch_id": "vc-1-0001",
            "batch_sequence": 1,
            "batch_sha256": "7" * 64,
            "phase": "VC-1",
            "original_deadline_at_utc": sequence_four["original_deadline_at_utc"],
        }
        sequence_one_dir = root / "run-sequence-one"
        sequence_one_dir.mkdir(mode=0o700)
        sequence_one_state: dict[str, object] = {"state": "stopped"}
        self._write_json(
            sequence_one_dir / "campaign-run-manifest.json",
            {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "manifest": sequence_one_manifest,
                "manifest_sha256": supervisor._sha256(
                    supervisor._canonical(sequence_one_manifest)
                ),
            },
        )
        history = [
            (sequence_one_state, sequence_one_manifest, sequence_one_dir),
            sequence_two,
            (sequence_three_state, sequence_three, sequence_three_dir),
        ]

        helper_path = (
            data_root
            / "tools/official_client_capture/"
            "codex_upgrade_vc1_permission_alias_closeout.py"
        )
        helper_sha256 = hashlib.sha256(helper_path.read_bytes()).hexdigest()
        closeout_tool_path = (
            data_root
            / "tools/official_client_capture/"
            "codex_upgrade_vc1_permission_alias_predispatch_closeout.py"
        )
        closeout_tool_path.write_text("# predispatch fixture\n", encoding="utf-8")
        closeout_tool_path.chmod(0o600)
        closeout_tool_sha256 = hashlib.sha256(
            closeout_tool_path.read_bytes()
        ).hexdigest()
        current_tool_files_sha256 = "f" * 64
        current_deployment_path = (
            data_root / "control/codex-0154-supervisor-enable-sequence5.json"
        )
        self._write_json(
            current_deployment_path,
            {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "architecture": "aarch64",
                "production_tool_root": str(helper_path.parent),
                "tool_files_sha256": current_tool_files_sha256,
                "supervisor_sha256": hashlib.sha256(
                    Path(supervisor.__file__).read_bytes()
                ).hexdigest(),
            },
        )
        current_deployment_sha256 = hashlib.sha256(
            current_deployment_path.read_bytes()
        ).hexdigest()

        boundary = mock.Mock(
            entries=(object(), object()),
            gaps=(object(),),
            gap_sha256="6" * 64,
            boundary_sha256="5" * 64,
        )
        run_history: list[dict[str, object]] = []
        for _state, manifest, run_dir in history:
            run_history.append(
                {
                    "batch_sequence": manifest["batch_sequence"],
                    "run_name": run_dir.name,
                    "manifest_file_sha256": hashlib.sha256(
                        (run_dir / "campaign-run-manifest.json").read_bytes()
                    ).hexdigest(),
                }
            )
        receipt_path = (
            action_input_dir / "sequence4-predispatch-closeout-receipt.json"
        )
        receipt_core: dict[str, object] = {
            "schema_version": "codex-vc1-permission-alias-predispatch-closeout/v1",
            "status": "passed",
            "campaign_id": campaign_id,
            "attempt_id": supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID,
            "created_at_utc": "2026-09-14T14:00:00Z",
            "source_sequence": 4,
            "source_batch_file_sha256": hashlib.sha256(
                sequence_four_batch_path.read_bytes()
            ).hexdigest(),
            "source_batch_sha256": sequence_four["batch_sha256"],
            "source_manifest_file_sha256": hashlib.sha256(
                sequence_four_manifest_path.read_bytes()
            ).hexdigest(),
            "source_action_plan_sha256": hashlib.sha256(
                sequence_four_action_plan_path.read_bytes()
            ).hexdigest(),
            "source_failed_deployment_sha256": str(
                sequence_four["actions"][0]["command"][
                    sequence_four["actions"][0]["command"].index(
                        "--deployment-receipt-sha256"
                    )
                    + 1
                ]
            ),
            "source_compiled_at_utc": compiled_at,
            "source_must_start_by_utc": must_start_by,
            "source_actions": [
                "harden-official-evidence-permissions-via-alias",
                "seal-official-preview",
            ],
            "source_run_created": False,
            "source_run_history": run_history,
            "deterministic_error_type": "PermissionAliasCloseoutError",
            "deterministic_error": supervisor.VC1_PERMISSION_ALIAS_PREDISPATCH_ERROR,
            "historical_helper_deployment_receipt": str(current_deployment_path),
            "historical_helper_deployment_receipt_sha256": (
                current_deployment_sha256
            ),
            "historical_helper_rollback": str(data_root / "historical-rollback"),
            "historical_helper_sha256": helper_sha256,
            "current_deployment_receipt": str(current_deployment_path),
            "current_deployment_receipt_sha256": current_deployment_sha256,
            "current_tool_files_sha256": current_tool_files_sha256,
            "closeout_tool_sha256": closeout_tool_sha256,
            "current_helper_sha256": helper_sha256,
            "boundary": {
                "entry_count": len(boundary.entries),
                "gap_count": len(boundary.gaps),
                "gap_sha256": boundary.gap_sha256,
                "stable_boundary_sha256": boundary.boundary_sha256,
            },
            "scanned_bytes": 0,
            "live_request_count": 0,
        }
        receipt = {
            **receipt_core,
            "receipt_sha256": supervisor._sha256(
                supervisor._canonical(receipt_core)
            ),
        }
        self._write_json(receipt_path, receipt)

        permission_receipt_path = (
            action_input_dir / "sequence5-permission-alias-closeout-receipt.json"
        )
        alias_action = {
            "action_id": "harden-official-evidence-permissions-via-alias-v2",
            "operation": "VC-1:harden-official-evidence-permissions-via-alias-v2",
            "timeout_seconds": 180.0,
            "command": [
                "/usr/bin/python3",
                str(helper_path),
                "--campaign-id",
                campaign_id,
                "--attempt-id",
                supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID,
                "--attempt",
                str(
                    campaign_dir
                    / "official/attempts"
                    / supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
                    / "attempt.json"
                ),
                "--attempt-sha256",
                str(bindings["VC1_PERMISSION_COMPENSATION_ATTEMPT_SHA256"]),
                "--roots-sha256",
                str(bindings["VC1_PERMISSION_COMPENSATION_ROOTS_SHA256"]),
                "--self-sha256",
                helper_sha256,
                "--readonly-runs-root",
                str(bindings["VC1_PERMISSION_ALIAS_READONLY_RUNS_ROOT"]),
                "--writable-runs-root",
                str(data_root / "runs"),
                "--deployment-receipt",
                str(current_deployment_path),
                "--deployment-receipt-sha256",
                current_deployment_sha256,
                "--tool-files-sha256",
                current_tool_files_sha256,
                "--receipt",
                str(permission_receipt_path),
            ],
            "item_ids": ["harden-official-evidence-permissions-via-alias-v2"],
        }
        sequence_five: dict[str, object] = {
            **sequence_four,
            "batch_id": "vc-1-0005",
            "batch_sequence": 5,
            "batch_sha256": "e" * 64,
            "actions": [alias_action, sequence_three["actions"][1]],
            "execute_items": [
                "harden-official-evidence-permissions-via-alias-v2",
                "seal-official-preview",
            ],
            "reuse_items": ["prepare-official-assertion-bundle"],
        }
        sequence_five_action_plan_path = (
            action_input_dir / "vc1-sequence5-action-plan.json"
        )
        self._write_json(
            sequence_five_action_plan_path,
            {
                "schema_version": "codex-upgrade-vc-action-plan/v1",
                "execute_item_ids": sequence_five["execute_items"],
                "reuse_item_ids": sequence_five["reuse_items"],
                "actions": sequence_five["actions"],
            },
        )
        historical_deployment_path = Path(
            sequence_four["actions"][0]["command"][
                sequence_four["actions"][0]["command"].index(
                    "--deployment-receipt"
                )
                + 1
            ]
        )
        bindings.update(
            {
                "VC1_PERMISSION_ALIAS_ENTRY_COUNT": len(boundary.entries),
                "VC1_PERMISSION_ALIAS_GAP_COUNT": len(boundary.gaps),
                "VC1_PERMISSION_ALIAS_GAP_SHA256": boundary.gap_sha256,
                "VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_FILE_SHA256": receipt_core[
                    "source_batch_file_sha256"
                ],
                "VC1_PERMISSION_ALIAS_PREDISPATCH_BATCH_SHA256": sequence_four[
                    "batch_sha256"
                ],
                "VC1_PERMISSION_ALIAS_PREDISPATCH_MANIFEST_FILE_SHA256": receipt_core[
                    "source_manifest_file_sha256"
                ],
                "VC1_PERMISSION_ALIAS_PREDISPATCH_ACTION_PLAN_SHA256": receipt_core[
                    "source_action_plan_sha256"
                ],
                "VC1_PERMISSION_ALIAS_PREDISPATCH_DEPLOYMENT_SHA256": hashlib.sha256(
                    historical_deployment_path.read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_PREDISPATCH_TOOL_FILES_SHA256": sequence_four[
                    "actions"
                ][0]["command"][
                    sequence_four["actions"][0]["command"].index(
                        "--tool-files-sha256"
                    )
                    + 1
                ],
                "VC1_PERMISSION_ALIAS_PREDISPATCH_SUPERVISOR_SHA256": hashlib.sha256(
                    Path(supervisor.__file__).read_bytes()
                ).hexdigest(),
                "VC1_PERMISSION_ALIAS_PREDISPATCH_COMPILED_AT_UTC": compiled_at,
                "VC1_PERMISSION_ALIAS_PREDISPATCH_MUST_START_BY_UTC": must_start_by,
                "VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_PATH": (
                    current_deployment_path
                ),
                "VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_DEPLOYMENT_SHA256": (
                    current_deployment_sha256
                ),
                "VC1_PERMISSION_ALIAS_HISTORICAL_HELPER_ROLLBACK_PATH": (
                    data_root / "historical-rollback"
                ),
                "VC1_PERMISSION_ALIAS_V2_HELPER_SHA256": helper_sha256,
                "VC1_PERMISSION_ALIAS_PREDISPATCH_CLOSEOUT_TOOL_SHA256": (
                    closeout_tool_sha256
                ),
            }
        )
        return {
            "successor": sequence_five,
            "history": history,
            "bindings": bindings,
            "boundary": boundary,
            "receipt_path": receipt_path,
            "permission_receipt_path": permission_receipt_path,
            "sequence_four_batch_path": sequence_four_batch_path,
        }

    def _recovery_manifest(self, root: Path) -> dict[str, object]:
        contract = root / "recovery-contract.json"
        contract.write_text("{}\n", encoding="utf-8")
        contract.chmod(0o600)
        return {
            "schema_version": supervisor.CAMPAIGN_RUN_RECOVERY_SCHEMA,
            "campaign_id": "campaign-recovery",
            "campaign_plan_sha256": "1" * 64,
            "batch_id": "vc-1-0002",
            "batch_sequence": 2,
            "batch_sha256": "2" * 64,
            "phase": "VC-1",
            "predecessor_checkpoint": {
                "path": "control/vc/VC-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            },
            "original_deadline_at_utc": "2099-09-14T12:00:00Z",
            "recovery_mode": "interrupted-vc1-preview",
            "recovery_contract": {
                "path": str(contract),
                "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
            },
            "recovery_predecessor": {
                "run_dir": str(root / "run-prior"),
                "state_sha256": "5" * 64,
                "manifest_sha256": "6" * 64,
                "stop_receipt_sha256": "7" * 64,
                "owner_nonce": "8" * 64,
                "terminal_at_utc": "2026-09-14T01:00:00Z",
                "state": "failed",
                "reason": "KeyboardInterrupt",
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "9" * 64,
            },
            "no_op": False,
            "actions": [
                {
                    "action_id": "recover-vc1-interruption-preview",
                    "operation": "VC-1:recover-interruption-preview",
                    "timeout_seconds": 60,
                    "command": [
                        sys.executable,
                        "/srv/tools/codex_upgrade.py",
                        "recover-vc1-interruption",
                        "--campaign-dir",
                        "/srv/campaign",
                        "--recovery-contract",
                        str(contract),
                    ],
                    "item_ids": ["job-b", "job-c"],
                }
            ],
            "execute_items": ["job-b", "job-c"],
            "reuse_items": ["job-a"],
        }

    def _recovery_continuation_manifest(
        self,
        root: Path,
        manifest_path: Path,
    ) -> dict[str, object]:
        recovery = self._recovery_manifest(root)
        deployment = root / "deployment.json"
        self._write_json(deployment, {"fixture": True})
        maintenance = {
            "from_tool_files_sha256": "a" * 64,
            "to_tool_files_sha256": "b" * 64,
            "changed_files": [
                {
                    "path": "codex_upgrade_supervisor.py",
                    "from_sha256": "c" * 64,
                    "to_sha256": "d" * 64,
                    "classification": "evaluation",
                    "affected_job_ids": [],
                }
            ],
            "allowed_production_paths": [],
            "affected_job_ids": [],
        }
        payload = {
            **recovery,
            "schema_version": supervisor.CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            "batch_id": "vc-1-0003",
            "batch_sequence": 3,
            "batch_sha256": "e" * 64,
            "recovery_mode": "interrupted-vc1-preview-continuation",
            "continuation_predecessor": {
                "run_dir": str(root / "run-failed-v3"),
                "state_sha256": "1" * 64,
                "manifest_sha256": "2" * 64,
                "stop_receipt_sha256": "3" * 64,
                "action_diagnostic_sha256": "4" * 64,
                "owner_nonce": "5" * 64,
                "terminal_at_utc": "2026-09-14T02:08:21Z",
                "state": "failed",
                "reason": "action-failed:recover-vc1-interruption-preview",
                "error_type": "ConfigurationError",
                "message": "watchdog heartbeat 越出当前 attempt。",
                "batch_id": "vc-1-0002",
                "batch_sequence": 2,
                "batch_sha256": "6" * 64,
            },
            "deployment_receipt": {
                "path": str(deployment),
                "sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
                "tool_files_sha256": "b" * 64,
            },
            "maintenance_tool_transition": maintenance,
            "effective_tool_transition": maintenance,
            "actions": [
                {
                    "action_id": "continue-vc1-interruption-preview",
                    "operation": "VC-1:continue-interruption-preview",
                    "timeout_seconds": 60,
                    "command": [
                        sys.executable,
                        "/srv/tools/codex_upgrade.py",
                        "continue-vc1-interruption",
                        "--campaign-dir",
                        "/srv/campaign",
                        "--recovery-contract",
                        recovery["recovery_contract"]["path"],
                        "--continuation-manifest",
                        str(manifest_path),
                    ],
                    "item_ids": ["job-b", "job-c"],
                }
            ],
        }
        return payload

    def _recovery_finalization_manifest(
        self,
        root: Path,
        continuation_path: Path,
        manifest_path: Path,
    ) -> dict[str, object]:
        continuation = self._recovery_continuation_manifest(
            root,
            continuation_path,
        )
        self._write_json(continuation_path, continuation)
        deployment = root / "finalization-deployment.json"
        self._write_json(deployment, {"fixture": "finalization"})
        maintenance = {
            "from_tool_files_sha256": "b" * 64,
            "to_tool_files_sha256": "f" * 64,
            "changed_files": [
                {
                    "path": "codex_upgrade.py",
                    "from_sha256": "d" * 64,
                    "to_sha256": "e" * 64,
                    "classification": "evaluation",
                    "affected_job_ids": [],
                }
            ],
            "allowed_production_paths": [],
            "affected_job_ids": [],
        }
        effective = {
            **maintenance,
            "from_tool_files_sha256": "a" * 64,
        }
        return {
            **continuation,
            "schema_version": supervisor.CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
            "batch_id": "vc-1-0004",
            "batch_sequence": 4,
            "batch_sha256": "0" * 64,
            "recovery_mode": "interrupted-vc1-preview-finalization",
            "continuation_manifest": {
                "path": str(continuation_path),
                "sha256": hashlib.sha256(
                    continuation_path.read_bytes()
                ).hexdigest(),
            },
            "finalization_predecessor": {
                "run_dir": str(root / "run-failed-v4"),
                "state_sha256": "7" * 64,
                "manifest_sha256": "8" * 64,
                "stop_receipt_sha256": "9" * 64,
                "action_diagnostic_sha256": "a" * 64,
                "owner_nonce": "b" * 64,
                "terminal_at_utc": "2026-09-14T03:05:40Z",
                "state": "failed",
                "reason": "action-failed:continue-vc1-interruption-preview",
                "error_type": "ConfigurationError",
                "message": "中断恢复续接父 v4 清单、自绑定或动作漂移。",
                "batch_id": "vc-1-0003",
                "batch_sequence": 3,
                "batch_sha256": "e" * 64,
            },
            "deployment_receipt": {
                "path": str(deployment),
                "sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
                "tool_files_sha256": "f" * 64,
            },
            "maintenance_tool_transition": maintenance,
            "effective_tool_transition": effective,
            "actions": [
                {
                    "action_id": "continue-vc1-interruption-preview",
                    "operation": "VC-1:continue-interruption-preview",
                    "timeout_seconds": 60,
                    "command": [
                        sys.executable,
                        "/srv/tools/codex_upgrade.py",
                        "continue-vc1-interruption",
                        "--campaign-dir",
                        "/srv/campaign",
                        "--recovery-contract",
                        continuation["recovery_contract"]["path"],
                        "--continuation-manifest",
                        str(manifest_path),
                    ],
                    "item_ids": ["job-b", "job-c"],
                }
            ],
        }

    def _campaign_command(self, *arguments: str) -> dict[str, object]:
        """通过真实 CLI 进程验证常驻 Campaign 接口。"""

        script = Path(__file__).parents[1] / "codex_upgrade_supervisor.py"
        result = subprocess.run(
            [sys.executable, str(script), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def _campaign_start(
        self,
        root: Path,
        *,
        initial_timeout: float = 2,
        deadline_seconds: float = 5,
    ) -> dict[str, object]:
        return self._campaign_command(
            "campaign-start",
            "--state-dir",
            str(root / "campaign"),
            "--campaign-id",
            "campaign-parent",
            "--phase",
            "official",
            "--deadline-seconds",
            str(deadline_seconds),
            "--initial-operation",
            "test-planning",
            "--initial-timeout-seconds",
            str(initial_timeout),
            "--heartbeat-seconds",
            "0.05",
            "--watchdog-timeout-seconds",
            "0.5",
            "--ledger-interval-seconds",
            "0.05",
        )

    def _wait_campaign_state(
        self,
        run_dir: Path,
        expected: set[str],
        *,
        timeout: float = 2,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            if state.get("state") in expected:
                return state
            time.sleep(0.05)
        self.fail(f"Campaign 未在预算内进入终态：{sorted(expected)}")

    def _wait_campaign_activity(
        self,
        run_dir: Path,
        *,
        classification: str,
        require_command_pid: bool = False,
        timeout: float = 2,
    ) -> dict[str, object]:
        """等待父监督器活动原子切换到目标分类。"""

        deadline = time.monotonic() + timeout
        path = run_dir / "campaign-activity.json"
        while time.monotonic() < deadline:
            activity = json.loads(path.read_text(encoding="utf-8"))
            if activity.get("classification") == classification and (
                not require_command_pid or isinstance(activity.get("command_pid"), int)
            ):
                return activity
            time.sleep(0.02)
        self.fail(f"Campaign 未在预算内进入 {classification}")

    def _campaign_exec_process(
        self,
        run_dir: Path,
        *,
        operation: str,
        returncode: int = 0,
        sleep_seconds: float = 0,
        accepted_returncodes: tuple[int, ...] = (),
    ) -> subprocess.Popen[str]:
        """启动真实 campaign-exec，供退出和强停路径共用。"""

        script = Path(__file__).parents[1] / "codex_upgrade_supervisor.py"
        command = (
            "import sys,time; "
            f"time.sleep({sleep_seconds!r}); sys.exit({returncode!r})"
        )
        argv = [
                sys.executable,
                str(script),
                "campaign-exec",
                "--state-dir",
                str(run_dir),
                "--operation",
                operation,
                "--timeout-seconds",
                "3",
        ]
        for accepted in accepted_returncodes:
            argv.extend(["--accept-returncode", str(accepted)])
        argv.extend(
            [
                "--",
                sys.executable,
                "-c",
                command,
            ]
        )
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _client(self, root: Path, *, timeout: float = 0.5) -> SupervisorClient:
        return SupervisorClient(
            root / "supervisor",
            campaign_id="campaign",
            phase="official",
            deadline_at_epoch=time.time() + 5,
            heartbeat_seconds=0.05,
            watchdog_timeout_seconds=timeout,
            ledger_interval_seconds=0.05,
            terminate_owner=False,
        )

    def _write_campaign_run_manifest(
        self,
        root: Path,
        *,
        actions: list[dict[str, object]],
        no_op: bool = False,
    ) -> Path:
        """写入 canonical campaign-run 清单并固定为 0600。"""

        manifest = root / "campaign-run.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "codex-upgrade-campaign-run/v1",
                    "campaign_id": "canonical-run",
                    "phase": "official",
                    "deadline_seconds": 5,
                    "no_op": no_op,
                    "actions": actions,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest.chmod(0o600)
        return manifest

    def _campaign_run(
        self,
        root: Path,
        *,
        actions: list[dict[str, object]],
        no_op: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).parents[1] / "codex_upgrade_supervisor.py"
        manifest = self._write_campaign_run_manifest(
            root,
            actions=actions,
            no_op=no_op,
        )
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "campaign-run",
                "--state-dir",
                str(root / "campaign"),
                "--manifest",
                str(manifest),
                "--heartbeat-seconds",
                "0.05",
                "--watchdog-timeout-seconds",
                "0.5",
                "--ledger-interval-seconds",
                "0.05",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_normal_stop_is_idempotent_and_preserves_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client.start()
            client.event_start("job:one:step-1", job_id="one")
            client.event_end("job:one:step-1", job_id="one")
            client.heartbeat("idle", force=True)
            client.stop()
            client.stop()
            state = json.loads((client.run_dir / "state.json").read_text())
            self.assertEqual(state["state"], "stopped")
            report = _audit_command(client.run_dir)
            self.assertFalse(report["audit_incomplete"])
            self.assertGreaterEqual(report["event_count"], 4)

    def test_campaign_run_executes_declared_queue_under_one_supervisor(self) -> None:
        """canonical 队列不需要外部逐项派发，也不产生 dispatch gap。"""

        with tempfile.TemporaryDirectory() as directory:
            result = self._campaign_run(
                Path(directory),
                actions=[
                    {
                        "action_id": "one",
                        "operation": "queue-one",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "pass"],
                    },
                    {
                        "action_id": "two",
                        "operation": "queue-two",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "pass"],
                    },
                ],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "stopped")
            self.assertEqual([item["status"] for item in payload["actions"]], ["passed", "passed"])
            run_dir = Path(str(payload["run_dir"]))
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])

    def test_campaign_run_child_attaches_without_nested_supervisor(self) -> None:
        """队列子命令复用父 run，不能创建第二个 monitor。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = (
                "from tools.official_client_capture import codex_upgrade_supervisor as s; "
                "c=s.SupervisorClient.attach_from_environment(); "
                "assert c is not None and c.attached; "
                "c.event_start('child:action'); c.event_end('child:action'); c.stop()"
            )
            result = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "attach",
                        "operation": "queue-attach",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", child],
                    }
                ],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            run_dir = Path(str(payload["run_dir"]))
            self.assertEqual(len(list(run_dir.parent.glob("run-*/state.json"))), 1)
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])

    def test_campaign_run_stops_on_first_failed_action(self) -> None:
        """队列动作失败后立即封存，不继续执行后续动作。"""

        with tempfile.TemporaryDirectory() as directory:
            result = self._campaign_run(
                Path(directory),
                actions=[
                    {
                        "action_id": "failed",
                        "operation": "queue-failed",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "import sys; sys.exit(3)"],
                    },
                    {
                        "action_id": "must-not-run",
                        "operation": "queue-after-failure",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "raise SystemExit(9)"],
                    },
                ],
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual([item["action_id"] for item in payload["actions"]], ["failed"])
            run_dir = Path(str(payload["run_dir"]))
            diagnostic_binding = payload["actions"][0]["diagnostic"]
            diagnostic_path = run_dir / diagnostic_binding["path"]
            self.assertEqual(diagnostic_path.stat().st_mode & 0o777, 0o600)
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["failure_kind"], "child-returncode")
            self.assertEqual(
                diagnostic_binding["sha256"],
                diagnostic["diagnostic_sha256"],
            )
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])

    def test_campaign_run_child_failure_diagnostic_is_bounded_and_redacted(self) -> None:
        """子进程可留下原因类型，但 argv、环境和原始输出不得持久化。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = (
                "import sys; "
                "from tools.official_client_capture import codex_upgrade_supervisor as s; "
                "error=RuntimeError('argv=hidden-argument environment=hidden-variable "
                "stdout=hidden-output token=hidden-token'); "
                "s.write_campaign_run_action_diagnostic("
                "failure_kind='unexpected-error', error=error); sys.exit(7)"
            )
            result = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "redacted",
                        "operation": "queue-redacted",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", child],
                    }
                ],
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            run_dir = Path(str(payload["run_dir"]))
            binding = payload["actions"][0]["diagnostic"]
            diagnostic_path = run_dir / binding["path"]
            raw_diagnostic = diagnostic_path.read_text(encoding="utf-8")
            diagnostic = json.loads(raw_diagnostic)
            self.assertEqual(diagnostic["failure_kind"], "unexpected-error")
            self.assertEqual(diagnostic["error_type"], "RuntimeError")
            self.assertEqual(diagnostic["message"], "错误详情已按脱敏规则省略。")
            self.assertLessEqual(len(diagnostic["message"]), 512)
            for hidden in (
                "hidden-argument",
                "hidden-variable",
                "hidden-output",
                "hidden-token",
            ):
                self.assertNotIn(hidden, raw_diagnostic)
            self.assertEqual(diagnostic_path.stat().st_mode & 0o777, 0o600)

    def test_campaign_run_records_codex_upgrade_handled_failure(self) -> None:
        """真实编排器的已知异常路径必须产出由父进程验证的诊断绑定。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upgrade_script = Path(__file__).parents[1] / "codex_upgrade.py"
            result = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "handled",
                        "operation": "queue-handled",
                        "timeout_seconds": 2,
                        "command": [
                            sys.executable,
                            str(upgrade_script),
                            "status",
                            "--campaign-dir",
                            str(root / "missing-campaign"),
                        ],
                    }
                ],
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            run_dir = Path(str(payload["run_dir"]))
            binding = payload["actions"][0]["diagnostic"]
            diagnostic = json.loads(
                (run_dir / binding["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["failure_kind"], "handled-error")
            self.assertEqual(diagnostic["error_type"], "ConfigurationError")
            self.assertEqual(binding["sha256"], diagnostic["diagnostic_sha256"])

    def test_archive_failure_diagnostic_preserves_redacted_errno(self) -> None:
        """归档失败的异常类型与 errno 必须穿过父监督器持久化。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = (
                "失败任务证据归档失败：error_type=OSError "
                "errno=30(EROFS) rollback_complete=true"
            )
            child = (
                "import sys; "
                "from tools.official_client_capture.capturelib.model "
                "import ConfigurationError; "
                "from tools.official_client_capture "
                "import codex_upgrade_supervisor as s; "
                f"error=ConfigurationError({message!r}); "
                "s.write_campaign_run_action_diagnostic("
                "failure_kind='handled-error',error=error); sys.exit(1)"
            )
            result = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "archive-failure",
                        "operation": "queue-archive-failure",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", child],
                    }
                ],
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            run_dir = Path(str(payload["run_dir"]))
            binding = payload["actions"][0]["diagnostic"]
            diagnostic = json.loads(
                (run_dir / binding["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["error_type"], "ConfigurationError")
            self.assertEqual(diagnostic["message"], message)
            self.assertEqual(binding["sha256"], diagnostic["diagnostic_sha256"])

    def test_campaign_run_empty_queue_is_immediate_noop(self) -> None:
        """空执行集合必须立即写 no-op 并结束，不启动任何动作。"""

        with tempfile.TemporaryDirectory() as directory:
            result = self._campaign_run(Path(directory), actions=[], no_op=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["reason"], "incremental-noop")
            self.assertEqual(payload["actions"], [])
            report = _audit_command(Path(str(payload["run_dir"])))
            self.assertFalse(report["audit_incomplete"])

    def test_vc6_legacy_supervisor_entry_is_rejected_before_start(self) -> None:
        """VC-6 不能从旧 campaign-start 入口启动，且拒绝时不创建 run。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "campaign"
            script = Path(__file__).parents[1] / "codex_upgrade_supervisor.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "campaign-start",
                    "--state-dir",
                    str(root),
                    "--campaign-id",
                    "vc6-formal",
                    "--phase",
                    "VC-6",
                    "--deadline-seconds",
                    "75",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("VC-6 正式流程拒绝旧监督器入口", result.stderr)
            self.assertFalse(root.exists())

    def test_vc6_manifest_rejects_dynamic_dispatch_action(self) -> None:
        """正式 VC-6 清单不能把旧 planning/dispatch 当成动作。"""

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-upgrade-campaign-run/v1",
                        "campaign_id": "vc6-formal",
                        "phase": "VC-6",
                        "deadline_seconds": 75,
                        "no_op": False,
                        "actions": [
                            {
                                "action_id": "dispatch",
                                "operation": "dispatch-next-action",
                                "timeout_seconds": 15,
                                "command": [sys.executable, "-c", "pass"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest.chmod(0o600)
            with self.assertRaisesRegex(SupervisorError, "不得声明动态 planning/dispatch"):
                _campaign_run_manifest(manifest)

    def test_campaign_run_manifest_separates_execute_and_reuse(self) -> None:
        payload = build_campaign_run_manifest(
            "campaign-build",
            "official",
            30,
            actions=[
                {
                    "action_id": "affected-rule",
                    "operation": "VC-4:affected-rule",
                    "timeout_seconds": 10,
                    "command": [sys.executable, "-c", "pass"],
                }
            ],
            reuse_items=["inherited-rule"],
        )
        self.assertEqual(payload["execute_items"], ["affected-rule"])
        self.assertEqual(payload["reuse_items"], ["inherited-rule"])
        self.assertFalse(payload["no_op"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(_campaign_run_manifest(path)["reuse_items"], ["inherited-rule"])

    def test_campaign_run_manifest_rejects_control_and_legacy_write_commands_before_start(
        self,
    ) -> None:
        for command in (
            "successor",
            "plan",
            "reuse-official-evidence",
            "compile-vc-batch",
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = root / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "schema_version": "codex-upgrade-campaign-run/v1",
                            "campaign_id": "campaign-build",
                            "phase": "official",
                            "deadline_seconds": 30,
                            "no_op": False,
                            "actions": [
                                {
                                    "action_id": "forbidden",
                                    "operation": "VC-2:forbidden",
                                    "timeout_seconds": 10,
                                    "command": [
                                        sys.executable,
                                        "codex_upgrade.py",
                                        command,
                                    ],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                manifest.chmod(0o600)
                with self.assertRaisesRegex(
                    SupervisorError,
                    "控制面或旧写入入口",
                ):
                    _campaign_run_manifest(manifest)

    def test_v2_cannot_invoke_interrupted_recovery_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            self._write_json(
                manifest,
                {
                    "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    "campaign_id": "campaign-recovery",
                    "campaign_plan_sha256": "1" * 64,
                    "batch_id": "vc-1-0002",
                    "batch_sequence": 2,
                    "batch_sha256": "2" * 64,
                    "phase": "VC-1",
                    "predecessor_checkpoint": {
                        "path": "checkpoint.json",
                        "sha256": "3" * 64,
                        "phase": "VC-0",
                        "checkpoint_sha256": "4" * 64,
                    },
                    "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                    "no_op": False,
                    "actions": [
                        {
                            "action_id": "recover",
                            "operation": "VC-1:recover",
                            "timeout_seconds": 60,
                            "command": [
                                sys.executable,
                                "/srv/tools/codex_upgrade.py",
                                "recover-vc1-interruption",
                            ],
                            "item_ids": ["job-a"],
                        }
                    ],
                    "execute_items": ["job-a"],
                    "reuse_items": [],
                },
            )
            with self.assertRaisesRegex(SupervisorError, "控制面或旧写入入口"):
                _campaign_run_manifest(manifest)

    def test_v3_requires_the_unique_bound_preview_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._recovery_manifest(root)
            manifest = root / "manifest.json"
            self._write_json(manifest, payload)
            self.assertEqual(
                _campaign_run_manifest(manifest)["schema_version"],
                supervisor.CAMPAIGN_RUN_RECOVERY_SCHEMA,
            )

            payload["actions"][0]["action_id"] = "different-action"
            self._write_json(root / "invalid.json", payload)
            with self.assertRaisesRegex(SupervisorError, "唯一零请求预览动作"):
                _campaign_run_manifest(root / "invalid.json")

    def test_v3_builder_allows_atomic_contract_to_land_after_structure_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._recovery_manifest(root)
            contract_path = Path(payload["recovery_contract"]["path"])
            contract_path.unlink()
            built = supervisor.build_recovery_campaign_run_manifest(
                campaign_id=payload["campaign_id"],
                campaign_plan_sha256=payload["campaign_plan_sha256"],
                batch_id=payload["batch_id"],
                batch_sequence=payload["batch_sequence"],
                batch_sha256=payload["batch_sha256"],
                phase=payload["phase"],
                predecessor_checkpoint=payload["predecessor_checkpoint"],
                original_deadline_at_utc=payload["original_deadline_at_utc"],
                recovery_contract=payload["recovery_contract"],
                recovery_predecessor=payload["recovery_predecessor"],
                actions=payload["actions"],
                execute_items=payload["execute_items"],
                reuse_items=payload["reuse_items"],
            )
            self.assertEqual(
                built["schema_version"],
                supervisor.CAMPAIGN_RUN_RECOVERY_SCHEMA,
            )
            manifest = root / "not-executable.json"
            self._write_json(manifest, built)
            with self.assertRaisesRegex(SupervisorError, "路径或摘要漂移"):
                _campaign_run_manifest(manifest)

    def test_v4_requires_exact_continuation_and_control_only_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "continuation.json"
            payload = self._recovery_continuation_manifest(root, manifest_path)
            self._write_json(manifest_path, payload)
            parsed = _campaign_run_manifest(manifest_path)
            self.assertEqual(
                parsed["schema_version"],
                supervisor.CAMPAIGN_RUN_RECOVERY_CONTINUATION_SCHEMA,
            )

            payload["maintenance_tool_transition"]["changed_files"][0][
                "classification"
            ] = "failed_job_production"
            payload["maintenance_tool_transition"]["changed_files"][0][
                "affected_job_ids"
            ] = ["job-b"]
            payload["maintenance_tool_transition"]["allowed_production_paths"] = [
                "codex_upgrade_supervisor.py"
            ]
            payload["maintenance_tool_transition"]["affected_job_ids"] = ["job-b"]
            invalid = root / "invalid-continuation.json"
            payload["actions"][0]["command"][-1] = str(invalid)
            self._write_json(invalid, payload)
            with self.assertRaisesRegex(SupervisorError, "风险分类非法"):
                _campaign_run_manifest(invalid)

    def test_v5_requires_exact_failed_v4_and_control_only_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            continuation_path = root / "continuation.json"
            manifest_path = root / "finalization.json"
            payload = self._recovery_finalization_manifest(
                root,
                continuation_path,
                manifest_path,
            )
            self._write_json(manifest_path, payload)
            parsed = _campaign_run_manifest(manifest_path)
            self.assertEqual(
                parsed["schema_version"],
                supervisor.CAMPAIGN_RUN_RECOVERY_FINALIZATION_SCHEMA,
            )

            payload["finalization_predecessor"]["message"] = "其他错误"
            invalid = root / "invalid-finalization.json"
            payload["actions"][0]["command"][-1] = str(invalid)
            self._write_json(invalid, payload)
            with self.assertRaisesRegex(
                SupervisorError,
                "finalization_predecessor",
            ):
                _campaign_run_manifest(invalid)

    def test_failed_v2_only_allows_direct_v3_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_dir = root / "run-prior"
            prior_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": "campaign-recovery",
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "9" * 64,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
            }
            prior_state = {
                "state": "failed",
                "campaign_id": "campaign-recovery",
                "owner_nonce": "8" * 64,
                "terminal_at_utc": "2026-09-14T01:00:00Z",
            }
            self._write_json(prior_dir / "state.json", prior_state)
            self._write_json(
                prior_dir / "campaign-run-manifest.json",
                {"manifest": prior_manifest},
            )
            self._write_json(
                prior_dir / "stop-receipt.json",
                {
                    "event_type": "failed",
                    "reason": "KeyboardInterrupt",
                    "owner_nonce": prior_state["owner_nonce"],
                    "campaign_id": prior_state["campaign_id"],
                },
            )
            recovery = self._recovery_manifest(root)
            recovery["recovery_predecessor"] = supervisor._recovery_predecessor_from_run(
                prior_state,
                prior_manifest,
                prior_dir,
            )
            ordinary = dict(recovery)
            ordinary.pop("recovery_mode")
            ordinary.pop("recovery_contract")
            ordinary.pop("recovery_predecessor")
            ordinary["schema_version"] = supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA
            with self.assertRaisesRegex(SupervisorError, "唯一直接 v3"):
                supervisor._validate_batched_campaign_history(
                    ordinary,
                    [(prior_state, prior_manifest, prior_dir)],
                )
            supervisor._validate_batched_campaign_history(
                recovery,
                [(prior_state, prior_manifest, prior_dir)],
            )

    def test_failed_v2_allows_only_frozen_permission_compensation_successor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_state, prior_manifest, prior_dir, successor, bindings = (
                self._permission_compensation_fixture(root)
            )
            sequence_one_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": successor["campaign_id"],
                "campaign_plan_sha256": successor["campaign_plan_sha256"],
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "7" * 64,
                "original_deadline_at_utc": successor["original_deadline_at_utc"],
            }
            sequence_one_state = {"state": "stopped"}
            with mock.patch.multiple(supervisor, **bindings):
                self.assertTrue(
                    supervisor._validate_permission_preflight_compensation_successor(
                        prior_state,
                        prior_manifest,
                        prior_dir,
                        successor,
                    )
                )
                ordered = supervisor._validate_batched_campaign_history(
                    successor,
                    [
                        (
                            sequence_one_state,
                            sequence_one_manifest,
                            root / "run-sequence-one",
                        ),
                        (prior_state, prior_manifest, prior_dir),
                    ],
                )
            self.assertEqual(
                [item[1]["batch_sequence"] for item in ordered],
                [1, 2],
            )

    def test_permission_compensation_rejects_nonzero_scan_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_state, prior_manifest, prior_dir, successor, bindings = (
                self._permission_compensation_fixture(root)
            )
            diagnostic_path = (
                prior_dir
                / "action-diagnostics"
                / "action-seal-official-preview-failure.json"
            )
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            diagnostic.pop("diagnostic_sha256")
            diagnostic["message"] = (
                "seal 廉价前检失败（scanned_bytes=1）：证据目录向 group/other 开放，"
                "必须先修正权限：/fixture"
            )
            diagnostic["diagnostic_sha256"] = supervisor._sha256(
                supervisor._canonical(diagnostic)
            )
            self._write_json(diagnostic_path, diagnostic)
            bindings["VC1_PERMISSION_COMPENSATION_DIAGNOSTIC_SHA256"] = str(
                diagnostic["diagnostic_sha256"]
            )
            with mock.patch.multiple(supervisor, **bindings):
                with self.assertRaisesRegex(SupervisorError, "scanned_bytes=0"):
                    supervisor._validate_permission_preflight_compensation_successor(
                        prior_state,
                        prior_manifest,
                        prior_dir,
                        successor,
                    )

    def test_permission_compensation_rejects_helper_identity_drift(self) -> None:
        mutations = ("path", "sha256", "permissions")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prior_state, prior_manifest, prior_dir, successor, bindings = (
                    self._permission_compensation_fixture(root)
                )
                harden = successor["actions"][0]
                helper_path = Path(harden["command"][1])
                if mutation == "path":
                    harden["command"][1] = str(helper_path.with_name("other.py"))
                elif mutation == "sha256":
                    helper_path.write_text("# drifted helper\n", encoding="utf-8")
                    helper_path.chmod(0o600)
                else:
                    helper_path.chmod(0o644)
                with mock.patch.multiple(supervisor, **bindings):
                    with self.assertRaisesRegex(SupervisorError, "权限.*补偿"):
                        supervisor._validate_permission_preflight_compensation_successor(
                            prior_state,
                            prior_manifest,
                            prior_dir,
                            successor,
                        )

    def test_permission_compensation_rejects_attempt_and_roots_drift(self) -> None:
        for mutation in ("attempt", "roots"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prior_state, prior_manifest, prior_dir, successor, bindings = (
                    self._permission_compensation_fixture(root)
                )
                harden_command = successor["actions"][0]["command"]
                attempt_path = Path(
                    harden_command[harden_command.index("--attempt") + 1]
                )
                if mutation == "attempt":
                    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
                    attempt["unexpected"] = True
                    self._write_json(attempt_path, attempt)
                else:
                    roots_index = harden_command.index("--roots-sha256") + 1
                    drifted = "f" * 64
                    harden_command[roots_index] = drifted
                    bindings["VC1_PERMISSION_COMPENSATION_ROOTS_SHA256"] = drifted
                with mock.patch.multiple(supervisor, **bindings):
                    with self.assertRaisesRegex(SupervisorError, "attempt|32 根"):
                        supervisor._validate_permission_preflight_compensation_successor(
                            prior_state,
                            prior_manifest,
                            prior_dir,
                            successor,
                        )

    def test_permission_compensation_rejects_action_or_seal_drift(self) -> None:
        for mutation in ("reuse", "seal"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prior_state, prior_manifest, prior_dir, successor, bindings = (
                    self._permission_compensation_fixture(root)
                )
                if mutation == "reuse":
                    successor["reuse_items"] = []
                else:
                    successor["actions"][1]["command"].append("--unexpected")
                with mock.patch.multiple(supervisor, **bindings):
                    with self.assertRaisesRegex(SupervisorError, "权限前检补偿"):
                        supervisor._validate_permission_preflight_compensation_successor(
                            prior_state,
                            prior_manifest,
                            prior_dir,
                            successor,
                        )

    def test_permission_compensation_rejects_existing_seal_artifact(self) -> None:
        for name in (
            "evidence-manifest.json",
            "seal-draft.json",
            "seal-preview.json",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prior_state, prior_manifest, prior_dir, successor, bindings = (
                    self._permission_compensation_fixture(root)
                )
                harden_command = successor["actions"][0]["command"]
                attempt_path = Path(
                    harden_command[harden_command.index("--attempt") + 1]
                )
                self._write_json(attempt_path.parent / name, {"unexpected": True})
                with mock.patch.multiple(supervisor, **bindings):
                    with self.assertRaisesRegex(SupervisorError, "已存在 seal 制品"):
                        supervisor._validate_permission_preflight_compensation_successor(
                            prior_state,
                            prior_manifest,
                            prior_dir,
                            successor,
                        )

    def test_permission_compensation_requires_successful_assertion_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_state, prior_manifest, prior_dir, successor, bindings = (
                self._permission_compensation_fixture(root)
            )
            self._write_permission_compensation_events(
                prior_dir / "events.ndjson",
                campaign_id=str(prior_state["campaign_id"]),
                owner_pid=int(prior_state["owner_pid"]),
                owner_nonce=str(prior_state["owner_nonce"]),
                prepare_status="failed",
            )
            with mock.patch.multiple(supervisor, **bindings):
                with self.assertRaisesRegex(SupervisorError, "assertion bundle 成功"):
                    supervisor._validate_permission_preflight_compensation_successor(
                        prior_state,
                        prior_manifest,
                        prior_dir,
                        successor,
                    )

    def test_failed_sequence_three_allows_only_permission_alias_successor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state, manifest, run_dir, successor, bindings, sequence_two = (
                self._permission_alias_fixture(root)
            )
            sequence_one_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": successor["campaign_id"],
                "campaign_plan_sha256": successor["campaign_plan_sha256"],
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "7" * 64,
                "original_deadline_at_utc": successor["original_deadline_at_utc"],
            }
            with (
                mock.patch.multiple(supervisor, **bindings),
                mock.patch.object(
                    supervisor.permission_alias_closeout,
                    "inspect_permission_boundary",
                    return_value=object(),
                ) as inspect_boundary,
            ):
                self.assertTrue(
                    supervisor._validate_permission_alias_closeout_successor(
                        state,
                        manifest,
                        run_dir,
                        successor,
                    )
                )
                ordered = supervisor._validate_batched_campaign_history(
                    successor,
                    [
                        (
                            {"state": "stopped"},
                            sequence_one_manifest,
                            root / "run-sequence-one",
                        ),
                        sequence_two,
                        (state, manifest, run_dir),
                    ],
                )
            self.assertEqual(
                [item[1]["batch_sequence"] for item in ordered],
                [1, 2, 3],
            )
            self.assertEqual(inspect_boundary.call_count, 2)

    def test_permission_alias_successor_rejects_boundary_or_deployment_drift(
        self,
    ) -> None:
        for mutation in ("boundary", "deployment"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                state, manifest, run_dir, successor, bindings, _sequence_two = (
                    self._permission_alias_fixture(root)
                )
                alias_command = successor["actions"][0]["command"]
                if mutation == "deployment":
                    index = alias_command.index("--tool-files-sha256") + 1
                    alias_command[index] = "c" * 64
                    boundary_side_effect = None
                else:
                    boundary_side_effect = (
                        supervisor.permission_alias_closeout.PermissionAliasCloseoutError(
                            "双别名 inode 漂移"
                        )
                    )
                with (
                    mock.patch.multiple(supervisor, **bindings),
                    mock.patch.object(
                        supervisor.permission_alias_closeout,
                        "inspect_permission_boundary",
                        side_effect=boundary_side_effect,
                        return_value=object(),
                    ),
                ):
                    with self.assertRaisesRegex(
                        SupervisorError,
                        "边界前检失败|部署收据|sequence 4 动作",
                    ):
                        supervisor._validate_permission_alias_closeout_successor(
                            state,
                            manifest,
                            run_dir,
                            successor,
                        )

    def test_permission_alias_successor_rejects_seal_or_receipt_preexistence(
        self,
    ) -> None:
        for mutation in ("seal", "receipt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                state, manifest, run_dir, successor, bindings, _sequence_two = (
                    self._permission_alias_fixture(root)
                )
                campaign_dir = Path(
                    successor["actions"][1]["command"][
                        successor["actions"][1]["command"].index("--campaign-dir") + 1
                    ]
                )
                if mutation == "seal":
                    target = (
                        campaign_dir
                        / "official/attempts"
                        / supervisor.VC1_PERMISSION_COMPENSATION_ATTEMPT_ID
                        / "seal-preview.json"
                    )
                else:
                    target = Path(successor["actions"][0]["command"][-1])
                self._write_json(target, {"unexpected": True})
                with (
                    mock.patch.multiple(supervisor, **bindings),
                    mock.patch.object(
                        supervisor.permission_alias_closeout,
                        "inspect_permission_boundary",
                        return_value=object(),
                    ),
                ):
                    with self.assertRaisesRegex(
                        SupervisorError,
                        "已存在 seal 制品|sequence 4 动作或收据",
                    ):
                        supervisor._validate_permission_alias_closeout_successor(
                            state,
                            manifest,
                            run_dir,
                            successor,
                        )

    def test_permission_alias_predispatch_allows_only_exact_sequence_five(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._permission_alias_predispatch_fixture(
                Path(directory).resolve()
            )
            with (
                mock.patch.multiple(supervisor, **fixture["bindings"]),
                mock.patch.object(
                    supervisor.permission_alias_closeout,
                    "inspect_permission_boundary",
                    return_value=fixture["boundary"],
                ) as inspect_boundary,
            ):
                ordered = supervisor._validate_batched_campaign_history(
                    fixture["successor"],
                    fixture["history"],
                )
            self.assertEqual(
                [item[1]["batch_sequence"] for item in ordered],
                [1, 2, 3],
            )
            self.assertEqual(inspect_boundary.call_count, 2)

    def test_permission_alias_predispatch_rejects_any_frozen_fact_drift(
        self,
    ) -> None:
        for mutation in ("batch", "seal", "receipt", "output"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = self._permission_alias_predispatch_fixture(
                    Path(directory).resolve()
                )
                successor = fixture["successor"]
                if mutation == "batch":
                    Path(fixture["sequence_four_batch_path"]).write_text(
                        "{}\n",
                        encoding="utf-8",
                    )
                elif mutation == "seal":
                    successor = copy.deepcopy(successor)
                    successor["actions"][1]["timeout_seconds"] = 1499.0
                elif mutation == "receipt":
                    receipt_path = Path(fixture["receipt_path"])
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    receipt["live_request_count"] = 1
                    receipt.pop("receipt_sha256")
                    receipt["receipt_sha256"] = supervisor._sha256(
                        supervisor._canonical(receipt)
                    )
                    self._write_json(receipt_path, receipt)
                else:
                    self._write_json(
                        Path(fixture["permission_receipt_path"]),
                        {"unexpected": True},
                    )
                with (
                    mock.patch.multiple(supervisor, **fixture["bindings"]),
                    mock.patch.object(
                        supervisor.permission_alias_closeout,
                        "inspect_permission_boundary",
                        return_value=fixture["boundary"],
                    ),
                ):
                    with self.assertRaisesRegex(
                        SupervisorError,
                        "制品摘要|逐字复用 seal|封口收据|动作或输出",
                    ):
                        supervisor._validate_batched_campaign_history(
                            successor,
                            fixture["history"],
                        )

    def test_permission_alias_predispatch_rejects_existing_sequence_four_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._permission_alias_predispatch_fixture(
                Path(directory).resolve()
            )
            history = list(fixture["history"])
            sequence_four = copy.deepcopy(fixture["successor"])
            sequence_four["batch_id"] = "vc-1-0004"
            sequence_four["batch_sequence"] = 4
            history.append(({"state": "stopped"}, sequence_four, Path(directory)))
            with mock.patch.multiple(supervisor, **fixture["bindings"]):
                self.assertFalse(
                    supervisor._validate_permission_alias_predispatch_successor(
                        fixture["history"][-1][0],
                        fixture["history"][-1][1],
                        fixture["history"][-1][2],
                        fixture["successor"],
                        history,
                    )
                )

    def test_failed_v3_only_allows_exact_sequence_three_v4_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v2_dir = root / "run-failed-v2"
            v2_manifest = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": "campaign-recovery",
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "9" * 64,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
            }
            v2_state = {
                "state": "failed",
                "campaign_id": "campaign-recovery",
                "owner_nonce": "8" * 64,
                "terminal_at_utc": "2026-09-14T01:00:00Z",
            }
            self._write_json(v2_dir / "state.json", v2_state)
            self._write_json(
                v2_dir / "campaign-run-manifest.json",
                {"manifest": v2_manifest},
            )
            self._write_json(
                v2_dir / "stop-receipt.json",
                {
                    "event_type": "failed",
                    "reason": "KeyboardInterrupt",
                    "owner_nonce": v2_state["owner_nonce"],
                    "campaign_id": v2_state["campaign_id"],
                },
            )
            v3_dir = root / "run-failed-v3"
            v3_manifest = self._recovery_manifest(root)
            v3_manifest["recovery_predecessor"] = (
                supervisor._recovery_predecessor_from_run(
                    v2_state,
                    v2_manifest,
                    v2_dir,
                )
            )
            v3_state = {
                "state": "failed",
                "campaign_id": "campaign-recovery",
                "phase": "VC-1",
                "owner_pid": os.getpid(),
                "owner_nonce": "a" * 64,
                "terminal_at_utc": "2026-09-14T02:08:21Z",
            }
            self._write_json(v3_dir / "state.json", v3_state)
            self._write_json(
                v3_dir / "campaign-run-manifest.json",
                {"manifest": v3_manifest},
            )
            self._write_json(
                v3_dir / "stop-receipt.json",
                {
                    "event_type": "failed",
                    "reason": "action-failed:recover-vc1-interruption-preview",
                    "owner_nonce": v3_state["owner_nonce"],
                    "campaign_id": v3_state["campaign_id"],
                },
            )
            diagnostic_path = supervisor._action_diagnostic_path(
                v3_dir,
                "recover-vc1-interruption-preview",
                create_directory=True,
            )
            supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id="campaign-recovery",
                phase="VC-1",
                action_id="recover-vc1-interruption-preview",
                owner_pid=os.getpid(),
                owner_nonce="a" * 64,
                failure_kind="handled-error",
                error_type="ConfigurationError",
                message="watchdog heartbeat 越出当前 attempt。",
            )
            v4 = self._recovery_continuation_manifest(
                root,
                root / "continuation.json",
            )
            v4["recovery_predecessor"] = v3_manifest["recovery_predecessor"]
            v4["continuation_predecessor"] = (
                supervisor._recovery_continuation_predecessor_from_run(
                    v3_state,
                    v3_manifest,
                    v3_dir,
                )
            )
            supervisor._validate_batched_campaign_history(
                v4,
                [
                    (v2_state, v2_manifest, v2_dir),
                    (v3_state, v3_manifest, v3_dir),
                ],
            )

            v4_path = root / "continuation-history.json"
            self._write_json(v4_path, v4)
            v4_dir = root / "run-failed-v4"
            v4_state = {
                "state": "failed",
                "campaign_id": "campaign-recovery",
                "phase": "VC-1",
                "owner_pid": os.getpid(),
                "owner_nonce": "b" * 64,
                "terminal_at_utc": "2026-09-14T03:05:40Z",
            }
            self._write_json(v4_dir / "state.json", v4_state)
            self._write_json(
                v4_dir / "campaign-run-manifest.json",
                {"manifest": v4},
            )
            self._write_json(
                v4_dir / "stop-receipt.json",
                {
                    "event_type": "failed",
                    "reason": "action-failed:continue-vc1-interruption-preview",
                    "owner_nonce": v4_state["owner_nonce"],
                    "campaign_id": v4_state["campaign_id"],
                },
            )
            v4_diagnostic_path = supervisor._action_diagnostic_path(
                v4_dir,
                "continue-vc1-interruption-preview",
                create_directory=True,
            )
            supervisor._write_action_diagnostic(
                v4_diagnostic_path,
                campaign_id="campaign-recovery",
                phase="VC-1",
                action_id="continue-vc1-interruption-preview",
                owner_pid=os.getpid(),
                owner_nonce="b" * 64,
                failure_kind="handled-error",
                error_type="ConfigurationError",
                message="中断恢复续接父 v4 清单、自绑定或动作漂移。",
            )
            v5 = self._recovery_finalization_manifest(
                root,
                root / "continuation-v5-fixture.json",
                root / "finalization.json",
            )
            v5["recovery_predecessor"] = v3_manifest["recovery_predecessor"]
            v5["continuation_predecessor"] = v4["continuation_predecessor"]
            v5["continuation_manifest"] = {
                "path": str(v4_path),
                "sha256": hashlib.sha256(v4_path.read_bytes()).hexdigest(),
            }
            v5["finalization_predecessor"] = (
                supervisor._recovery_finalization_predecessor_from_run(
                    v4_state,
                    v4,
                    v4_dir,
                )
            )
            supervisor._validate_batched_campaign_history(
                v5,
                [
                    (v2_state, v2_manifest, v2_dir),
                    (v3_state, v3_manifest, v3_dir),
                    (v4_state, v4, v4_dir),
                ],
            )

    def test_campaign_run_rejects_reusing_campaign_id(self) -> None:
        """同一逻辑 Campaign 不能靠再次启动重新获得 deadline。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "one",
                        "operation": "queue-one",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "pass"],
                    }
                ],
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "two",
                        "operation": "queue-two",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", "pass"],
                    }
                ],
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("重新起算 deadline", second.stderr)

    def test_observed_owner_failure_is_failed_not_audit_incomplete(self) -> None:
        """已落盘的业务异常不能伪装成审计缺口。"""

        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "expected"):
                with client:
                    client.event_start("seal:preview")
                    client.event_fail("seal:preview", reason="expected-error")
                    raise RuntimeError("expected")
            state = json.loads((client.run_dir / "state.json").read_text())
            self.assertEqual(state["state"], "failed")
            report = _audit_command(client.run_dir)
            self.assertFalse(report["audit_incomplete"])
            self.assertEqual(set(report["classification_counts"]), {"failed"})
            self.assertGreaterEqual(report["classification_counts"]["failed"], 1)

    def test_heartbeat_gap_is_watchdog_abort_without_owner_kill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory), timeout=0.15)
            client.start()
            time.sleep(0.35)
            state = json.loads((client.run_dir / "state.json").read_text())
            self.assertEqual(state["state"], "watchdog-aborted")
            self.assertTrue((client.run_dir / "stop-receipt.json").is_file())
            # owner 仍在运行，说明 Campaign lease 的共享宿主不会被误杀。
            self.assertTrue(client.status()["owner_alive"])
            client.stop()

    def test_audit_reports_event_digest_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client.start()
            client.stop()
            events = client.run_dir / "events.ndjson"
            raw = events.read_text(encoding="utf-8")
            events.write_text(raw.replace("command-started", "command-tampered", 1))
            events.chmod(0o600)
            report = _audit_command(client.run_dir)
            self.assertTrue(report["audit_incomplete"])
            self.assertTrue(report["integrity_errors"])

    def test_monitor_crash_is_not_released_as_normal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client.start()
            assert client.process is not None
            os.kill(client.process.pid, signal.SIGKILL)
            client.process.wait(timeout=2)
            with self.assertRaises(SupervisorError):
                client.heartbeat("after-monitor-crash", force=True)
            state = json.loads((client.run_dir / "state.json").read_text())
            self.assertEqual(state["state"], "audit-incomplete")
            self.assertTrue((client.run_dir / "stop-receipt.json").is_file())
            client.stop()
            report = _audit_command(client.run_dir)
            self.assertTrue(report["audit_incomplete"])

    def test_run_can_persist_private_offline_gate_output(self) -> None:
        """显式启用后，离线门禁输出必须和失败终态一起保留。"""

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                return_code = main(
                    [
                        "run",
                        "--state-dir",
                        str(state_dir),
                        "--campaign-id",
                        "campaign",
                        "--phase",
                        "tool-fix",
                        "--deadline-seconds",
                        "5",
                        "--operation",
                        "offline-gate",
                        "--heartbeat-seconds",
                        "0.05",
                        "--watchdog-timeout-seconds",
                        "0.5",
                        "--ledger-interval-seconds",
                        "0.05",
                        "--persist-output",
                        "--",
                        sys.executable,
                        "-c",
                        "import sys; print('safe-output'); print('safe-error', file=sys.stderr); sys.exit(3)",
                    ]
                )
            self.assertEqual(return_code, 3)
            payload = json.loads(output.getvalue())
            log_path = Path(payload["output_log"])
            self.assertEqual(log_path.name, "command-output.log")
            self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("safe-output", content)
            self.assertIn("safe-error", content)
            report = _audit_command(Path(payload["run_dir"]))
            self.assertFalse(report["audit_incomplete"])
            self.assertGreaterEqual(report["classification_counts"].get("failed", 0), 1)
            # 命令运行期间账本按 heartbeat state=running 写出 active 桶属于合法分类；
            # 子进程启动慢于一个账本间隔（CI runner 常见）就会出现，不能据此判失败。
            # 这里只排除 audit-incomplete / stopped / planning 等不该出现的分类。
            self.assertTrue(
                set(report["classification_counts"]).issubset(
                    {"failed", "waiting", "active"}
                )
            )

    def test_campaign_parent_covers_planning_waiting_and_stop(self) -> None:
        """命令之间的规划和外部等待必须持续记账，正常结束不得留下缺口。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            time.sleep(0.12)
            self._campaign_command(
                "campaign-mark",
                "--state-dir",
                str(run_dir),
                "--classification",
                "planning",
                "--operation",
                "review-next-step",
                "--timeout-seconds",
                "1",
            )
            time.sleep(0.12)
            self._campaign_command(
                "campaign-mark",
                "--state-dir",
                str(run_dir),
                "--classification",
                "waiting",
                "--operation",
                "await-external-input",
                "--timeout-seconds",
                "1",
            )
            time.sleep(0.12)
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(run_dir),
                "--reason",
                "campaign-test-complete",
            )
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])
            self.assertGreaterEqual(report["classification_counts"].get("planning", 0), 1)
            self.assertGreaterEqual(report["classification_counts"].get("waiting", 0), 1)

    def test_campaign_exec_closes_normal_exit_and_requires_next_dispatch(self) -> None:
        """真实命令成功后必须进入短派发窗口，不得进入无限 idle。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(run_dir, operation="normal-exit")
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            self._wait_campaign_activity(
                run_dir,
                classification="planning",
            )
            activity = json.loads(
                (run_dir / "campaign-activity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(activity["operation"], "dispatch-next-action")
            self.assertLessEqual(
                activity["deadline_at_epoch"] - activity["started_at_epoch"],
                1.1,
            )
            events = (run_dir / "events.ndjson").read_text(encoding="utf-8")
            self.assertIn('"operation":"active:normal-exit"', events)
            self.assertIn('"event_type":"action-finished"', events)
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(run_dir),
                "--reason",
                "normal-exit-complete",
            )

    def test_campaign_exec_without_next_dispatch_fails_fast(self) -> None:
        """动作成功但没有下一动作时，派发 watchdog 必须快速停线。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(run_dir, operation="missing-next-dispatch")
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            state = self._wait_campaign_state(run_dir, {"failed"}, timeout=2)
            self.assertEqual(state["state"], "failed")
            events = (run_dir / "events.ndjson").read_text(encoding="utf-8")
            self.assertIn('"reason":"orchestrator-dispatch-timeout-1s"', events)
            # 终态写入与两个监督进程退出之间存在极短排空窗口；等待它们
            # 完全退出后再让临时目录清理，避免残留心跳文件造成竞态。
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = self._campaign_command(
                    "status", "--state-dir", str(run_dir)
                )
                if not status["owner_alive"] and not status["monitor_alive"]:
                    break
                time.sleep(0.02)

    def test_campaign_exec_allows_one_diagnosis_then_stops_repeated_failure(self) -> None:
        """同一操作首次失败进入诊断，第二次失败必须立即停线。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="nonzero-exit",
                returncode=3,
            )
            process.communicate(timeout=3)
            self.assertEqual(process.returncode, 3)
            activity = self._wait_campaign_activity(
                run_dir,
                classification="planning",
            )
            self.assertEqual(activity["operation"], "failure-diagnosis")
            state = json.loads(
                (run_dir / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["state"], "running")

            repeated = self._campaign_exec_process(
                run_dir,
                operation="nonzero-exit",
                returncode=3,
            )
            repeated.communicate(timeout=3)
            self.assertEqual(repeated.returncode, 3)
            state = self._wait_campaign_state(run_dir, {"failed"})
            self.assertEqual(state["state"], "failed")
            events = (run_dir / "events.ndjson").read_text(encoding="utf-8")
            self.assertIn('"reason":"returncode=3"', events)
            self.assertIn('"reason":"repeated-returncode=3"', events)

    def test_campaign_resume_inherits_deadline_and_records_gap(self) -> None:
        """父监督器重启只能续接原 deadline，未监管间隔必须显式暴露。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._campaign_start(root, deadline_seconds=10)
            predecessor = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                predecessor,
                operation="resume-source-failure",
                returncode=3,
            )
            process.communicate(timeout=3)
            self._wait_campaign_activity(
                predecessor,
                classification="planning",
            )
            repeated = self._campaign_exec_process(
                predecessor,
                operation="resume-source-failure",
                returncode=3,
            )
            repeated.communicate(timeout=3)
            self._wait_campaign_state(predecessor, {"failed"})
            predecessor_state = json.loads(
                (predecessor / "state.json").read_text(encoding="utf-8")
            )

            resumed = self._campaign_command(
                "campaign-start",
                "--state-dir",
                str(root / "campaign"),
                "--campaign-id",
                "campaign-parent",
                "--phase",
                "official",
                "--resume-from-run-dir",
                str(predecessor),
                "--initial-operation",
                "resume-planning",
                "--initial-timeout-seconds",
                "2",
                "--heartbeat-seconds",
                "0.05",
                "--watchdog-timeout-seconds",
                "0.5",
                "--ledger-interval-seconds",
                "0.05",
            )
            resumed_dir = Path(str(resumed["run_dir"]))
            resumed_state = json.loads(
                (resumed_dir / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                resumed_state["deadline_at_epoch"],
                predecessor_state["deadline_at_epoch"],
            )
            self.assertEqual(
                resumed_state["predecessor_run_dir"], str(predecessor)
            )
            with self.assertRaises(AssertionError):
                self._campaign_command(
                    "campaign-start",
                    "--state-dir",
                    str(root / "campaign"),
                    "--campaign-id",
                    "campaign-parent",
                    "--phase",
                    "official",
                    "--resume-from-run-dir",
                    str(predecessor),
                    "--initial-timeout-seconds",
                    "2",
                )
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(resumed_dir),
                "--reason",
                "resume-test-complete",
            )
            report = _audit_command(resumed_dir)
            self.assertTrue(report["audit_incomplete"])
            self.assertEqual(
                report["continuity"]["gap_classification"], "audit-incomplete"
            )

    def test_campaign_exec_accepts_explicit_negative_result(self) -> None:
        """只有逐个声明的诊断退出码可以按通过收口。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="expected-negative",
                returncode=1,
                accepted_returncodes=(1,),
            )
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            result = json.loads(stdout)
            self.assertEqual(result["returncode"], 1)
            self.assertEqual(result["accepted_returncodes"], [0, 1])
            self._wait_campaign_activity(run_dir, classification="planning")
            activity = json.loads(
                (run_dir / "campaign-activity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(activity["operation"], "dispatch-next-action")
            actions = list((run_dir / "campaign-actions").glob("*.json"))
            self.assertEqual(len(actions), 1)
            action = json.loads(actions[0].read_text(encoding="utf-8"))
            self.assertEqual(action["returncode"], 1)
            self.assertEqual(action["accepted_returncodes"], [0, 1])
            self.assertEqual(action["reason"], "accepted-returncode=1")
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(run_dir),
                "--reason",
                "expected-negative-complete",
            )

    def test_campaign_exec_rejects_action_without_terminal_drain_budget(self) -> None:
        """动作和排空窗口放不下时，必须在进入 active 前拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(
                Path(directory),
                deadline_seconds=1.5,
            )
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="insufficient-drain-budget",
            )
            _stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 1)
            self.assertIn("终态排空窗口", stderr)
            activity = json.loads(
                (run_dir / "campaign-activity.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(activity["classification"], "active")
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(run_dir),
                "--reason",
                "drain-budget-test-complete",
            )
            self.assertFalse(_audit_command(run_dir)["audit_incomplete"])

    def test_campaign_exec_sigkill_is_detected_without_waiting_for_deadline(self) -> None:
        """执行包装器被 SIGKILL 后必须在 watchdog 窗口内停线。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="sigkill-exit",
                sleep_seconds=10,
            )
            activity = self._wait_campaign_activity(
                run_dir,
                classification="active",
                require_command_pid=True,
            )
            started = time.monotonic()
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            state = self._wait_campaign_state(run_dir, {"failed"})
            # 断言的是“立即检测到 worker 丢失”，即远快于心跳失联判定（heartbeat_seconds × 1.25）；
            # 阈值取 2 秒，既保留量级差异，又不被 CI runner 的调度抖动误判。
            self.assertLess(time.monotonic() - started, IMMEDIATE_DETECTION_SECONDS)
            self.assertEqual(state["state"], "failed")
            archive = run_dir / "campaign-actions" / f"{activity['action_id']}.json"
            receipt = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(receipt["reason"], "worker-lost")

    def test_campaign_exec_session_hangup_is_detected(self) -> None:
        """会话断开使包装器收到 SIGHUP 时，不得留下假 active。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="session-hangup",
                sleep_seconds=10,
            )
            self._wait_campaign_activity(
                run_dir,
                classification="active",
                require_command_pid=True,
            )
            started = time.monotonic()
            os.kill(process.pid, signal.SIGHUP)
            process.communicate(timeout=2)
            state = self._wait_campaign_state(run_dir, {"failed"})
            self.assertLess(time.monotonic() - started, IMMEDIATE_DETECTION_SECONDS)
            self.assertEqual(state["state"], "failed")

    def test_campaign_dispatch_timeout_is_failed_without_audit_gap(self) -> None:
        """父编排器未派发下一动作必须自动失败，不能伪装成审计不完整。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            self._campaign_command(
                "campaign-mark",
                "--state-dir",
                str(run_dir),
                "--classification",
                "planning",
                "--operation",
                "dispatch-next-action",
                "--timeout-seconds",
                "0.15",
            )
            state = self._wait_campaign_state(run_dir, {"failed"})
            self.assertEqual(state["state"], "failed")
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])
            self.assertGreaterEqual(report["classification_counts"].get("planning", 0), 1)
            self.assertGreaterEqual(report["classification_counts"].get("failed", 0), 1)

    def test_campaign_mark_rejects_new_orchestrator_idle(self) -> None:
        """新流程不能重新写入历史兼容用的 orchestrator-idle。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            script = Path(__file__).parents[1] / "codex_upgrade_supervisor.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "campaign-mark",
                    "--state-dir",
                    str(run_dir),
                    "--classification",
                    "orchestrator-idle",
                    "--operation",
                    "legacy-idle",
                    "--timeout-seconds",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("禁止登记 orchestrator-idle", result.stderr)
            self._campaign_command(
                "campaign-stop",
                "--state-dir",
                str(run_dir),
                "--reason",
                "reject-legacy-idle-complete",
            )

    def test_campaign_owner_kill_is_sealed_by_monitor(self) -> None:
        """常驻 owner 被强杀后，独立 monitor 必须在 watchdog 窗口内封存。"""

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            started = time.monotonic()
            os.kill(int(payload["owner_pid"]), signal.SIGKILL)
            state = self._wait_campaign_state(run_dir, {"watchdog-aborted"})
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(state["state"], "watchdog-aborted")
            report = _audit_command(run_dir)
            self.assertTrue(report["audit_incomplete"])
            self.assertTrue((run_dir / "stop-receipt.json").is_file())

    def test_campaign_mark_short_dispatch_timeout_never_reports_unconfirmed_switch(
        self,
    ) -> None:
        """派发超时先于心跳回显到期时，campaign-mark 仍返回 0，父监督器判 failed。

        0.01 秒远小于心跳间隔，父编排器几乎必然先于回显检测到到期并停线；这
        正是 CI 上偶发「未及时确认活动切换」的竞争路径，必须视为切换已被消费。
        """

        with tempfile.TemporaryDirectory() as directory:
            payload = self._campaign_start(Path(directory))
            run_dir = Path(str(payload["run_dir"]))
            self._campaign_command(
                "campaign-mark",
                "--state-dir",
                str(run_dir),
                "--classification",
                "planning",
                "--operation",
                "dispatch-next-action",
                "--timeout-seconds",
                "0.01",
            )
            state = self._wait_campaign_state(run_dir, {"failed"})
            self.assertEqual(state["state"], "failed")
            request = json.loads(
                (run_dir / "stop-request.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                str(request["reason"]).startswith("orchestrator-dispatch-timeout")
            )
            report = _audit_command(run_dir)
            self.assertFalse(report["audit_incomplete"])


if __name__ == "__main__":
    unittest.main()
