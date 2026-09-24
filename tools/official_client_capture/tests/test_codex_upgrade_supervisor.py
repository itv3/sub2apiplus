"""独立监督器的收口、心跳超时和离线审计回归测试。"""

from __future__ import annotations

import argparse
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

from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import runtime_egress_fixtures
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade_timing_ledger as timing_ledger,
)
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
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
    def setUp(self) -> None:
        # 本类只构造离线父动作与账本；实时出口的拒绝、竞态及清理在独立测试类验证。
        self.enterContext(runtime_egress_fixtures.offline_campaign_egress())

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

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

    def test_legacy_action_diagnostic_replays_as_nonrecoverable(self) -> None:
        """历史 v1 诊断没有 failure_class，重放时必须保守映射且不猜错误文本。"""

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve()
            run_dir.chmod(0o700)
            diagnostic_path = supervisor._action_diagnostic_path(
                run_dir,
                "legacy-action",
                create_directory=True,
            )
            payload = supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id="legacy-campaign",
                phase="VC-5",
                action_id="legacy-action",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
                failure_kind="handled-error",
                error_type="ConfigurationError",
                message="历史环境前提文字不参与分类。",
            )
            payload["schema_version"] = supervisor.ACTION_DIAGNOSTIC_LEGACY_SCHEMA
            payload.pop("failure_class")
            payload.pop("failure_observations")
            payload.pop("diagnostic_sha256")
            payload["diagnostic_sha256"] = supervisor._sha256(
                supervisor._canonical(payload)
            )
            self._write_json(diagnostic_path, payload)
            replayed = supervisor._validate_action_diagnostic(
                diagnostic_path,
                run_dir=run_dir,
                campaign_id="legacy-campaign",
                phase="VC-5",
                action_id="legacy-action",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
            )
            self.assertEqual(replayed["failure_class"], "execution-failure")
            self.assertEqual(replayed["failure_observations"], [])

    def test_v2_action_diagnostic_replays_with_empty_observations(self) -> None:
        """历史 v2 保留机器 failure_class，但没有观测数组时按空集重放。"""

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve()
            run_dir.chmod(0o700)
            diagnostic_path = supervisor._action_diagnostic_path(
                run_dir,
                "v2-action",
                create_directory=True,
            )
            payload = supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id="v2-campaign",
                phase="VC-5",
                action_id="v2-action",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
                failure_kind="handled-error",
                failure_class="environment-prerequisite",
                failure_observations=[
                    {"check_id": "readiness", "failure_code": "failed"}
                ],
                error_type="ConfigurationError",
                message="历史 v2 诊断。",
            )
            payload["schema_version"] = supervisor.ACTION_DIAGNOSTIC_V2_SCHEMA
            payload.pop("failure_observations")
            payload.pop("diagnostic_sha256")
            payload["diagnostic_sha256"] = supervisor._sha256(
                supervisor._canonical(payload)
            )
            self._write_json(diagnostic_path, payload)
            replayed = supervisor._validate_action_diagnostic(
                diagnostic_path,
                run_dir=run_dir,
                campaign_id="v2-campaign",
                phase="VC-5",
                action_id="v2-action",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
            )
            self.assertEqual(
                replayed["failure_class"], "environment-prerequisite"
            )
            self.assertEqual(replayed["failure_observations"], [])

    def test_action_diagnostic_preserves_enumerated_failure_observations(self) -> None:
        """就绪失败的枚举观测须去重排序，并把账务别名收敛为冻结分类。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = (
                "import sys; "
                "from tools.official_client_capture import "
                "codex_upgrade_supervisor as s; "
                "error=RuntimeError('readiness failed'); "
                "error.failure_observations=["
                "{'check_id':'candidate-readiness.storage','failure_code':'not-writable'},"
                "{'check_id':'candidate-readiness.image','failure_code':'tag-mismatch'},"
                "{'check_id':'candidate-readiness.storage','failure_code':'not-writable'}]; "
                "s.write_campaign_run_action_diagnostic("
                "failure_kind='handled-error', "
                "failure_class='accounting-uncertain', error=error); "
                "sys.exit(7)"
            )
            result = self._campaign_run(
                root,
                actions=[
                    {
                        "action_id": "readiness",
                        "operation": "queue-readiness",
                        "timeout_seconds": 2,
                        "command": [sys.executable, "-c", child],
                    }
                ],
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            run_dir = Path(str(payload["run_dir"]))
            diagnostic = json.loads(
                (
                    run_dir
                    / payload["actions"][0]["diagnostic"]["path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                diagnostic["schema_version"],
                supervisor.ACTION_DIAGNOSTIC_SCHEMA,
            )
            self.assertEqual(
                diagnostic["failure_class"],
                "request-accounting-uncertain",
            )
            self.assertEqual(
                diagnostic["failure_observations"],
                [
                    {
                        "check_id": "candidate-readiness.image",
                        "failure_code": "tag-mismatch",
                    },
                    {
                        "check_id": "candidate-readiness.storage",
                        "failure_code": "not-writable",
                    },
                ],
            )
            self.assertEqual(
                payload["actions"][0]["diagnostic"]["failure_observations"],
                diagnostic["failure_observations"],
            )

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
            "recover-candidate-failed-jobs",
            "plan",
            "reuse-official-evidence",
            "harden-evidence-permissions",
            "reconcile-attempt",
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

    def test_failed_v2_only_allows_direct_v3_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
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
            # 改造 5：所有 stop-receipt 读点走 read_stop_receipt，夹具必须是自摘要闭合的 v2 收据。
            supervisor._stop_receipt(
                prior_dir,
                event_type="failed",
                reason="KeyboardInterrupt",
                detected_at_epoch=1_700_000_000.0,
                owner_pid=1,
                owner_nonce=prior_state["owner_nonce"],
                campaign_id=prior_state["campaign_id"],
                phase="VC-1",
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

    def test_failed_official_v2_allows_exact_normal_v2_recovery_preview(self) -> None:
        """完整失败 attempt 以普通 v2 预览承接，不经过历史 v3。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            prior_dir = root / "run-prior"
            prior_dir.mkdir(mode=0o700)
            campaign_id = "campaign-official-preview"
            owner_nonce = "8" * 64
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            command_prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]
            prior_manifest: dict[str, object] = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "5" * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                "no_op": False,
                "actions": [
                    {
                        "action_id": "capture-official",
                        "operation": "VC-1:capture-official",
                        "timeout_seconds": 3600.0,
                        "command": [
                            *command_prefix,
                            "capture-official",
                            "run",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["failed-job", "passed-job"],
                    }
                ],
                "execute_items": ["failed-job", "passed-job"],
                "reuse_items": [],
            }
            prior_state: dict[str, object] = {
                "state": "failed",
                "campaign_id": campaign_id,
                "phase": "VC-1",
                "owner_pid": os.getpid(),
                "owner_nonce": owner_nonce,
                "terminal_at_utc": "2026-09-15T03:08:05.545Z",
            }
            self._write_json(prior_dir / "state.json", prior_state)
            stop: dict[str, object] = {
                "schema_version": supervisor.STOP_SCHEMA,
                "campaign_id": campaign_id,
                "detected_at_epoch": 1005.0,
                "detected_at_utc": "2026-09-15T03:08:05.531Z",
                "event_type": "failed",
                "owner_nonce": owner_nonce,
                "owner_pid": os.getpid(),
                "phase": "VC-1",
                "reason": "action-failed:capture-official",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            self._write_json(prior_dir / "stop-receipt.json", stop)
            diagnostic_path = supervisor._action_diagnostic_path(
                prior_dir,
                "capture-official",
                create_directory=True,
            )
            supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="capture-official",
                owner_pid=os.getpid(),
                owner_nonce=owner_nonce,
                failure_kind="child-returncode",
                error_type="ChildProcessError",
                message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
            )
            successor: dict[str, object] = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0002",
                "batch_sequence": 2,
                "batch_sha256": "6" * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                "no_op": False,
                "actions": [
                    {
                        "action_id": "preview-official-recovery",
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 600.0,
                        "command": [
                            *command_prefix,
                            "resume",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--rerun-failed",
                            "--preview-recovery",
                        ],
                        "item_ids": ["failed-job"],
                    }
                ],
                "execute_items": ["failed-job"],
                "reuse_items": ["passed-job"],
            }
            ordered = supervisor._validate_batched_campaign_history(
                successor,
                [(prior_state, prior_manifest, prior_dir)],
            )
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

            drifted = copy.deepcopy(successor)
            drifted["reuse_items"] = []
            with self.assertRaisesRegex(SupervisorError, "execute/reuse"):
                supervisor._validate_batched_campaign_history(
                    drifted,
                    [(prior_state, prior_manifest, prior_dir)],
                )

    def test_environment_recovery_only_redispatches_exact_prior_batch(self) -> None:
        """reservation 前环境失败须有对账许可，且后继只能逐字重派原批次。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, _fixture = self._timing_closeout_fixture(
                root
            )
            campaign_id = "campaign-closeout"
            prior_dir = root / "run-prior"
            prior_dir.mkdir(mode=0o700)
            owner_nonce = "8" * 64
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            action = {
                "action_id": "candidate-readiness",
                "operation": "VC-1:candidate-readiness",
                "timeout_seconds": 60.0,
                "command": [
                    sys.executable,
                    "/managed/codex_upgrade.py",
                    "candidate-readiness",
                    "--campaign-dir",
                    str(campaign_dir),
                ],
                "item_ids": ["candidate-readiness"],
            }
            prior_manifest: dict[str, object] = {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "5" * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                "no_op": False,
                "actions": [action],
                "execute_items": ["candidate-readiness"],
                "reuse_items": [],
            }
            prior_state: dict[str, object] = {
                "state": "failed",
                "campaign_id": campaign_id,
                "phase": "VC-1",
                "owner_pid": os.getpid(),
                "owner_nonce": owner_nonce,
                "terminal_at_utc": "2026-09-17T01:00:00Z",
            }
            self._write_json(prior_dir / "state.json", prior_state)
            stop: dict[str, object] = {
                "schema_version": supervisor.STOP_SCHEMA,
                "campaign_id": campaign_id,
                "detected_at_epoch": 1005.0,
                "detected_at_utc": "2026-09-17T01:00:00Z",
                "event_type": "failed",
                "owner_nonce": owner_nonce,
                "owner_pid": os.getpid(),
                "phase": "VC-1",
                "reason": "action-failed:candidate-readiness",
            }
            stop["receipt_sha256"] = supervisor._sha256(
                supervisor._canonical(stop)
            )
            self._write_json(prior_dir / "stop-receipt.json", stop)
            diagnostic_path = supervisor._action_diagnostic_path(
                prior_dir,
                "candidate-readiness",
                create_directory=True,
            )
            supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="candidate-readiness",
                owner_pid=os.getpid(),
                owner_nonce=owner_nonce,
                failure_kind="handled-error",
                failure_class="environment-prerequisite",
                error_type="ConfigurationError",
                message="候选就绪前提失败。",
            )

            timing_ledger.append_event(
                ledger_root,
                event_id="environment-prerequisite-paused",
                phase="VC-1",
                event_type="recovery_required",
                root_cause_id="campaign-run.action-failed",
                next_action="reconcile-supervisor-run",
            )
            reconciliation = {
                "schema_version": "supervisor-run-reconciliation/v1",
                "campaign_id": campaign_id,
                "failure_class": "environment-prerequisite",
                "reservation_exists": False,
                "live_request_count": 0,
                "scanned_bytes": 0,
                "run": {
                    "run_dir": str(prior_dir.resolve(strict=True)),
                    "run_id": prior_dir.name,
                    "state": "failed",
                    "phase": "VC-1",
                    "batch_id": "vc-1-0001",
                    "batch_sequence": 1,
                    "batch_sha256": "5" * 64,
                    "execute_items": ["candidate-readiness"],
                    "reuse_items": [],
                    "failure_class": "environment-prerequisite",
                },
            }
            campaign_receipt = (
                campaign_dir
                / "control"
                / "reconciliation"
                / f"run-{prior_dir.name}"
                / "supervisor-run-reconciliation.json"
            )
            ledger_receipt = (
                ledger_root
                / "receipts"
                / "reconciliation"
                / f"run-{prior_dir.name}"
                / "reconciliation.json"
            )
            provenance = ledger_receipt.with_name("provenance.json")
            self._write_json(campaign_receipt, reconciliation)
            self._write_json(ledger_receipt, reconciliation)
            # Campaign 收据按可读格式写入，而 TimingLedger 副本按紧凑格式保存；
            # 两者只要 JSON 事实相同，就不能因空白字节不同误报绑定漂移。
            campaign_receipt.write_text(
                json.dumps(reconciliation, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            campaign_receipt.chmod(0o600)
            self._write_json(provenance, {"status": "complete"})
            timing_ledger.append_event(
                ledger_root,
                event_id=f"reconcile-run-passed-{prior_dir.name}",
                phase="VC-1",
                event_type="receipt_passed",
                receipts=[
                    {
                        "role": "provenance",
                        "path": provenance.relative_to(ledger_root).as_posix(),
                        "sha256": timing_ledger._sha256_file(provenance),
                    },
                    {
                        "role": "reconciliation",
                        "path": ledger_receipt.relative_to(ledger_root).as_posix(),
                        "sha256": timing_ledger._sha256_file(ledger_receipt),
                    },
                ],
                next_action="redispatch-same-batch",
            )

            successor = copy.deepcopy(prior_manifest)
            successor["batch_id"] = "vc-1-0002"
            successor["batch_sequence"] = 2
            successor["batch_sha256"] = "6" * 64
            ordered = supervisor._validate_batched_campaign_history(
                successor,
                [(prior_state, prior_manifest, prior_dir)],
                campaign_dir=campaign_dir,
            )
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

            drifted = copy.deepcopy(successor)
            drifted["actions"][0]["timeout_seconds"] = 61.0
            with self.assertRaisesRegex(SupervisorError, "原批次内容重派"):
                supervisor._validate_batched_campaign_history(
                    drifted,
                    [(prior_state, prior_manifest, prior_dir)],
                    campaign_dir=campaign_dir,
                )

            campaign_receipt.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(SupervisorError, "对账收据绑定漂移"):
                supervisor._validate_batched_campaign_history(
                    successor,
                    [(prior_state, prior_manifest, prior_dir)],
                    campaign_dir=campaign_dir,
                )

    def test_campaign_run_locked_dispatches_failed_v2_normal_v2_preview(self) -> None:
        """真实父入口必须能从失败 sequence 1 派发普通 sequence 2 预览。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            state_dir = root / "supervisor-state"
            state_dir.mkdir(mode=0o700)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            driver = root / "codex_upgrade.py"
            driver.write_text(
                """from pathlib import Path
import sys

if "capture-official" in sys.argv:
    raise SystemExit(7)
if "resume" in sys.argv and "--preview-recovery" in sys.argv:
    campaign = Path(sys.argv[sys.argv.index("--campaign-dir") + 1])
    (campaign / "preview-ran.txt").write_text("passed\\n", encoding="utf-8")
    raise SystemExit(0)
raise SystemExit(9)
""",
                encoding="utf-8",
            )
            driver.chmod(0o600)
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            common = {
                "campaign_id": "campaign-official-preview-dispatch",
                "campaign_plan_sha256": "1" * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-15T08:12:43Z",
            }
            command_prefix = [sys.executable, str(driver)]
            first = supervisor.build_batched_campaign_run_manifest(
                **common,
                batch_id="vc-1-0001",
                batch_sequence=1,
                batch_sha256="5" * 64,
                actions=[
                    {
                        "action_id": "capture-official",
                        "operation": "VC-1:capture-official",
                        "timeout_seconds": 5.0,
                        "command": [
                            *command_prefix,
                            "capture-official",
                            "run",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["failed-job", "passed-job"],
                    }
                ],
                execute_items=["failed-job", "passed-job"],
                reuse_items=[],
            )
            args = argparse.Namespace(
                heartbeat_seconds=0.05,
                watchdog_timeout_seconds=0.5,
                ledger_interval_seconds=0.05,
            )
            first_returncode, first_payload = supervisor._campaign_run_locked(
                args,
                manifest=first,
                state_dir=state_dir,
            )
            self.assertEqual(first_returncode, 1)
            self.assertEqual(first_payload["reason"], "action-failed:capture-official")

            second = supervisor.build_batched_campaign_run_manifest(
                **common,
                batch_id="vc-1-0002",
                batch_sequence=2,
                batch_sha256="6" * 64,
                actions=[
                    {
                        "action_id": "preview-official-recovery",
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 5.0,
                        "command": [
                            *command_prefix,
                            "resume",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--rerun-failed",
                            "--preview-recovery",
                        ],
                        "item_ids": ["failed-job"],
                    }
                ],
                execute_items=["failed-job"],
                reuse_items=["passed-job"],
            )
            second_returncode, second_payload = supervisor._campaign_run_locked(
                args,
                manifest=second,
                state_dir=state_dir,
            )
            self.assertEqual(second_returncode, 0)
            self.assertEqual(second_payload["status"], "stopped")
            self.assertEqual(second_payload["reason"], "queue-complete")
            self.assertEqual(
                (campaign_dir / "preview-ran.txt").read_text(encoding="utf-8"),
                "passed\n",
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

    def _timing_closeout_fixture(
        self,
        root: Path,
    ) -> tuple[Path, Path, dict[str, object]]:
        """建立 active/VC-1 的正式 Campaign 与时间账本。"""

        data_root = root / "data"
        campaign_dir = data_root / "evidence" / "campaigns" / "campaign-closeout"
        campaign_dir.mkdir(parents=True, mode=0o700)
        project_ledger_fixture.install_fixture_ledger(data_root)
        ledger_root = data_root / "control" / "timing-ledger"
        ledger_root.parent.mkdir(parents=True, mode=0o700)
        timing_ledger.create_ledger(
            ledger_root,
            upgrade_id="upgrade-closeout",
            baseline_version="0.151.0",
            target_version="0.154.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
        )
        timing_ledger.append_event(
            ledger_root,
            event_id="fixture-vc0-completed",
            phase="VC-0",
            event_type="stage_completed",
            next_action="启动 VC-1",
        )
        timing_ledger.append_event(
            ledger_root,
            event_id="fixture-vc1-started",
            phase="VC-1",
            event_type="stage_started",
            next_action="运行父批次",
        )
        campaign = {
            "campaign_id": "campaign-closeout",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "control_receipts": {
                "upgrade_timing": {
                    "ledger_dir": str(ledger_root),
                    "ledger_plan_sha256": supervisor._sha256(
                        (ledger_root / "ledger.json").read_bytes()
                    ),
                    "upgrade_id": "upgrade-closeout",
                }
            },
        }
        self._write_json(campaign_dir / "campaign.json", campaign)
        project_ledger_fixture.register_fixture_campaign(campaign_dir)
        manifest = build_campaign_run_manifest(
            "campaign-closeout",
            "VC-1",
            30,
            actions=[
                {
                    "action_id": "failing-action",
                    "operation": "VC-1:failing-action",
                    "timeout_seconds": 5,
                    "command": [sys.executable, "-c", "raise SystemExit(7)"],
                }
            ],
        )
        return campaign_dir, ledger_root, manifest

    def _batched_closeout_fixture(
        self,
        root: Path,
    ) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        """在 v1 夹具上补齐 v2 分批清单所需的总计划绑定。"""

        from tools.official_client_capture import (
            codex_upgrade_vc_artifacts as vc_artifacts,
        )

        campaign_dir, ledger_root, _manifest = self._timing_closeout_fixture(root)
        plan = vc_artifacts.build_campaign_plan(
            campaign_id="campaign-closeout",
            campaign_mode="formal",
            campaign_purpose="production_replacement",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-15T00:00:00Z",
            original_deadline_at_utc="2026-09-15T06:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="4" * 64,
            p0_gate_sha256="5" * 64,
        )
        plan_path = campaign_dir / "control" / "vc" / "campaign-plan.json"
        self._write_json(plan_path, plan)
        campaign = json.loads((campaign_dir / "campaign.json").read_text("utf-8"))
        campaign["vc_control"] = {
            "campaign_plan": {
                "path": "control/vc/campaign-plan.json",
                # 绑定记录的是文件字节摘要，与计划内嵌的 plan_sha256 不同。
                "sha256": supervisor._sha256(plan_path.read_bytes()),
            }
        }
        self._write_json(campaign_dir / "campaign.json", campaign)
        manifest: dict[str, object] = {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": "campaign-closeout",
            "phase": "VC-1",
            "campaign_plan_sha256": plan["plan_sha256"],
            "batch_sequence": 1,
        }
        return campaign_dir, ledger_root, manifest, plan

    def test_batched_failure_closeout_accepts_plan_self_digest(self) -> None:
        """v2 批次的 campaign_plan_sha256 是计划自摘要，合法父批次必须能关账本。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest, _plan = self._batched_closeout_fixture(
                root
            )
            result = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                manifest,
                failed_action_id="failing-action",
            )
            self.assertEqual(result["status"], "passed")
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual(summary["status"], "stage_review_required")
            self.assertIsNone(summary["active_phase"])
            events = [
                event["event_type"]
                for event, _raw in timing_ledger._load_events(ledger_root)
            ]
            self.assertEqual(events[-2:], ["stage_abandoned", "stage_review_required"])

    def test_environment_prerequisite_pauses_without_abandoning_stage(self) -> None:
        """机器分类为环境前提失败时只写 recovery_required，保留当前阶段。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest, _plan = self._batched_closeout_fixture(
                root
            )
            result = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                manifest,
                failed_action_id="failing-action",
                failure_class="environment-prerequisite",
            )
            self.assertEqual(result["ledger_status"], "recovery_required")
            self.assertFalse(result["idempotent"])
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual(summary["status"], "recovery_required")
            self.assertEqual(summary["active_phase"], "VC-1")
            self.assertEqual(
                summary["recovery_root_cause_id"], result["root_cause_id"]
            )
            events = [
                event["event_type"]
                for event, _raw in timing_ledger._load_events(ledger_root)
            ]
            self.assertEqual(events[-1:], ["recovery_required"])
            self.assertNotIn("stage_abandoned", events)
            replay = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                manifest,
                failed_action_id="failing-action",
                failure_class="environment-prerequisite",
            )
            self.assertTrue(replay["idempotent"])
            self.assertEqual(replay["head_sha256"], result["head_sha256"])

    def test_parent_uses_action_diagnostic_failure_class_for_pause(self) -> None:
        """子动作显式分类必须穿过诊断收据，驱动父账本进入暂停态。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._timing_closeout_fixture(root)
            child = (
                "import sys; "
                "from tools.official_client_capture import "
                "codex_upgrade_supervisor as s; "
                "error=RuntimeError('环境前提失败'); "
                "s.write_campaign_run_action_diagnostic("
                "failure_kind='handled-error', "
                "failure_class='environment-prerequisite', error=error); "
                "sys.exit(7)"
            )
            manifest["actions"][0]["command"] = [sys.executable, "-c", child]
            state_dir = root / "supervisor-state"
            state_dir.mkdir(mode=0o700)
            returncode, payload = supervisor._campaign_run_locked(
                argparse.Namespace(
                    heartbeat_seconds=0.05,
                    watchdog_timeout_seconds=0.5,
                    ledger_interval_seconds=0.05,
                ),
                manifest=manifest,
                state_dir=state_dir,
                campaign_dir=campaign_dir,
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(
                payload["actions"][0]["diagnostic"]["failure_class"],
                "environment-prerequisite",
            )
            self.assertEqual(
                payload["timing_closeout"]["ledger_status"],
                "recovery_required",
            )
            self.assertEqual(
                timing_ledger.inspect_ledger(ledger_root)["status"],
                "recovery_required",
            )

    _POST_RUN_JOB_IDS = (
        "candidate-compact-direct",
        "candidate-compact-mitm",
        "candidate-core-direct",
        "candidate-core-mitm",
        "candidate-frozen-aux",
        "candidate-frozen-core",
        "candidate-h1-wire",
        "candidate-images-wire",
        "candidate-ws-handshake-repeat",
    )

    def _post_run_tooling_fixture(
        self,
        root: Path,
        *,
        kilo_window_offset_seconds: float | None = None,
        incomplete_job: bool = False,
        reservation_offset_seconds: float = -600.0,
        execute_items: tuple[str, ...] = ("candidate-seal",),
        child_command: list[str] | None = None,
        actions: list[dict[str, object]] | None = None,
    ) -> tuple[Path, Path, dict[str, object], Path]:
        """VC-5 active 的 batched Campaign 与一个 Job 全部 complete 的候选 attempt。

        ``kilo_window_offset_seconds`` 相对"现在"写 Kilo 运行窗口：负值表示
        Kilo 早于即将启动的父 run（零请求失败），正值表示本 run 内发过请求。
        """

        campaign_dir, ledger_root, _manifest, plan = self._batched_closeout_fixture(root)
        # 夹具账本停在 VC-1 active；seal 段失败发生在 VC-5，逐阶段推进。
        for completed, started in (
            ("VC-1", "VC-2"),
            ("VC-2", "VC-3"),
            ("VC-3", "VC-4"),
            ("VC-4", "VC-5"),
        ):
            timing_ledger.append_event(
                ledger_root,
                event_id=f"fixture-{completed.lower()}-completed",
                phase=completed,
                event_type="stage_completed",
                next_action=f"启动 {started}",
            )
            if started == "VC-4":
                # 改造 2：候选级阶段开工前账本必须先激活 r1。
                timing_ledger.append_event(
                    ledger_root,
                    event_id="fixture-stage-revision-r1",
                    phase="VC-4",
                    event_type="stage_revision",
                    revision=1,
                    candidate_id="cand-1",
                    revision_commit_sha256="6" * 64,
                    next_action="派发 VC-4 首批",
                )
            timing_ledger.append_event(
                ledger_root,
                event_id=f"fixture-{started.lower()}-started",
                phase=started,
                event_type="stage_started",
                next_action="运行父批次",
            )
        now = time.time()
        attempt_id = "20260917T190726Z-96ecf4e9a4848948"
        attempt_root = (
            campaign_dir / "candidates" / "cand-1" / "attempts" / attempt_id
        )
        attempt_root.mkdir(parents=True, mode=0o700)
        for path in (campaign_dir / "candidates", campaign_dir / "candidates" / "cand-1",
                     campaign_dir / "candidates" / "cand-1" / "attempts"):
            path.chmod(0o700)
        results = [
            {"id": job_id, "status": "complete", "disposition": "reused"}
            for job_id in self._POST_RUN_JOB_IDS
        ]
        if incomplete_job:
            results[-1]["status"] = "failed"
        self._write_json(
            attempt_root / "attempt.json",
            {
                "schema_version": "codex-upgrade-capture-attempt/v3",
                "attempt_id": attempt_id,
                "candidate_id": "cand-1",
                "campaign_id": "campaign-closeout",
                "phase": "candidate",
                "status": "awaiting_receipts",
                "results": results,
            },
        )
        for index, job_id in enumerate(self._POST_RUN_JOB_IDS, 1):
            self._write_json(
                attempt_root / "checkpoints" / f"{index:08d}.json",
                {
                    "checkpoint_sequence": index,
                    "item_id": job_id,
                    "status": "complete",
                    "disposition": "reused",
                },
            )
        self._write_json(
            attempt_root / "reservation.json",
            {
                "schema_version": "codex-upgrade-capture-reservation/v1",
                "attempt_id": attempt_id,
                "started_at_utc": supervisor._epoch_to_utc(now + reservation_offset_seconds),
                "run_nonce": "8" * 64,
            },
        )
        if kilo_window_offset_seconds is not None:
            self._write_json(
                attempt_root / "evidence" / "client" / "raw" / "kilo-run-window.json",
                {
                    "schema_version": "vc5-kilo-run-window/v1",
                    "started_at_utc": supervisor._epoch_to_utc(
                        now + kilo_window_offset_seconds
                    ),
                    "finished_at_utc": supervisor._epoch_to_utc(
                        now + kilo_window_offset_seconds + 30.0
                    ),
                    "request_count": 2,
                },
            )
        manifest = supervisor.build_batched_campaign_run_manifest(
            campaign_id="campaign-closeout",
            campaign_plan_sha256=str(plan["plan_sha256"]),
            batch_id="vc-5-0001",
            batch_sequence=1,
            batch_sha256="6" * 64,
            phase="VC-5",
            # 改造 2：staging 模型的候选级清单绑定 r1／cand-1。
            candidate_revision=1,
            candidate_id="cand-1",
            predecessor_checkpoint={
                "path": "control/vc/vc-4-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-4",
                "checkpoint_sha256": "4" * 64,
            },
            original_deadline_at_utc="2099-09-15T08:12:43Z",
            actions=actions
            if actions is not None
            else [
                {
                    "action_id": "prepare-candidate-assertion-bundle",
                    "operation": "VC-5:prepare-candidate-assertion-bundle",
                    "timeout_seconds": 5.0,
                    "command": child_command
                    or [sys.executable, "-c", "raise SystemExit(3)"],
                    "item_ids": ["candidate-seal"],
                }
            ],
            execute_items=list(execute_items),
            reuse_items=list(self._POST_RUN_JOB_IDS),
        )
        return campaign_dir, ledger_root, manifest, attempt_root

    def _run_post_run_tooling_parent(
        self, root: Path, manifest: dict[str, object], campaign_dir: Path
    ) -> tuple[int, dict[str, object], Path]:
        state_dir = root / "supervisor-state"
        state_dir.mkdir(mode=0o700)
        returncode, payload = supervisor._campaign_run_locked(
            argparse.Namespace(
                heartbeat_seconds=0.05,
                watchdog_timeout_seconds=0.5,
                ledger_interval_seconds=0.05,
            ),
            manifest=manifest,
            state_dir=state_dir,
            campaign_dir=campaign_dir,
        )
        return returncode, payload, Path(str(payload["run_dir"]))

    def test_post_run_tooling_failure_pauses_and_writes_receipt(self) -> None:
        """Job 全部 complete 后零请求的 seal 段失败：升级为 post-run-tooling 并暂停。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest, _attempt_root = (
                self._post_run_tooling_fixture(root, kilo_window_offset_seconds=-120.0)
            )
            returncode, payload, run_dir = self._run_post_run_tooling_parent(
                root, manifest, campaign_dir
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(
                payload["reason"], "action-failed:prepare-candidate-assertion-bundle"
            )
            diagnostic = payload["actions"][0]["diagnostic"]
            # 诊断文件保持子进程默认分类，升级只体现在有效分类与独立收据上。
            self.assertEqual(diagnostic["failure_class"], "execution-failure")
            self.assertEqual(diagnostic["effective_failure_class"], "post-run-tooling")
            receipt_binding = diagnostic["post_run_tooling_receipt"]
            receipt_path = run_dir / receipt_binding["path"]
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(payload["timing_closeout"]["ledger_status"], "recovery_required")
            self.assertEqual(payload["timing_closeout"]["failure_class"], "post-run-tooling")
            self.assertIn("逐字重派同一批次", payload["timing_closeout"]["next_action"])
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual(summary["status"], "recovery_required")
            self.assertEqual(summary["active_phase"], "VC-5")
            events = [
                event["event_type"] for event, _raw in timing_ledger._load_events(ledger_root)
            ]
            self.assertEqual(events[-1], "recovery_required")
            self.assertNotIn("stage_abandoned", events)
            self.assertNotIn("stop_the_line", events)

            # 收据复算：判据文件未变时必须得到相同事实，并把有效分类交给对账方。
            state = supervisor._read_state(run_dir)
            stored_diagnostic = supervisor._validate_action_diagnostic(
                run_dir / diagnostic["path"],
                run_dir=run_dir,
                campaign_id="campaign-closeout",
                phase="VC-5",
                action_id="prepare-candidate-assertion-bundle",
                owner_pid=int(state["owner_pid"]),
                owner_nonce=str(state["owner_nonce"]),
            )
            effective, receipt = supervisor.effective_action_failure_class(
                run_dir,
                stored_diagnostic,
                campaign_dir=campaign_dir,
                inner_manifest=manifest,
                campaign_id="campaign-closeout",
                phase="VC-5",
                action_id="prepare-candidate-assertion-bundle",
                owner_pid=int(state["owner_pid"]),
                owner_nonce=str(state["owner_nonce"]),
                run_started_at_utc=str(state["started_at_utc"]),
            )
            self.assertEqual(effective, "post-run-tooling")
            self.assertTrue(receipt["recomputed"])
            self.assertEqual(receipt["facts"]["attempt_id"], "20260917T190726Z-96ecf4e9a4848948")
            self.assertEqual(receipt["facts"]["complete_job_count"], 9)
            self.assertEqual(receipt["facts"]["kilo_window_request_count"], 2)
            self.assertEqual(
                [item["role"] for item in receipt["facts"]["bound_files"]],
                ["attempt", "job_checkpoint_tail", "reservation", "kilo_run_window"],
            )
            self.assertEqual(receipt["facts"]["checkpoint_complete_job_count"], 9)
            # 篡改收据即失败关闭。
            tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
            tampered["facts"]["complete_job_count"] = 8
            receipt_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(supervisor.SupervisorError, "摘要非法"):
                supervisor.effective_action_failure_class(
                    run_dir,
                    stored_diagnostic,
                    campaign_dir=campaign_dir,
                    inner_manifest=manifest,
                    campaign_id="campaign-closeout",
                    phase="VC-5",
                    action_id="prepare-candidate-assertion-bundle",
                    owner_pid=int(state["owner_pid"]),
                    owner_nonce=str(state["owner_nonce"]),
                    run_started_at_utc=str(state["started_at_utc"]),
                )

    def test_post_run_tooling_rejects_run_with_kilo_requests(self) -> None:
        """本次父 run 内启动过 Kilo 窗口的失败不是零请求失败，仍按既有规则停线。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest, _attempt_root = (
                self._post_run_tooling_fixture(root, kilo_window_offset_seconds=120.0)
            )
            returncode, payload, run_dir = self._run_post_run_tooling_parent(
                root, manifest, campaign_dir
            )
            self.assertEqual(returncode, 1)
            diagnostic = payload["actions"][0]["diagnostic"]
            self.assertEqual(diagnostic["effective_failure_class"], "execution-failure")
            self.assertTrue(
                any("Kilo" in reason for reason in diagnostic["post_run_tooling_rejected"])
            )
            self.assertFalse(
                list((run_dir / "action-diagnostics").glob("*-post-run-tooling.json"))
            )
            # 改造 2：候选级（VC-5）execution-failure 未命中永久条件时不再直接停线，
            # 而是 stage_abandoned + candidate_review_required（只读等待人工对账／作废）。
            self.assertEqual(payload["timing_closeout"]["ledger_status"], "candidate_review_required")
            self.assertEqual(timing_ledger.inspect_ledger(ledger_root)["status"], "candidate_review_required")

    def test_post_run_tooling_facts_negative_cases(self) -> None:
        """五条判据逐条失效时都不得升级，且不抛异常。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, _ledger_root, manifest, attempt_root = (
                self._post_run_tooling_fixture(root, incomplete_job=True)
            )
            run_started = supervisor._epoch_to_utc(time.time())
            incomplete = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertFalse(incomplete["qualifies"])
            self.assertTrue(any("尚未 complete" in note for note in incomplete["reasons"]))

            # execute_items 混入 Candidate Job 本身：不是后处理批次。
            mixed = dict(manifest)
            mixed["execute_items"] = ["candidate-seal", "candidate-frozen-core"]
            rejected = supervisor.post_run_tooling_facts(
                campaign_dir, mixed, run_started_at_utc=run_started
            )
            self.assertTrue(any("非零请求后处理" in note for note in rejected["reasons"]))

            # reuse_items 未覆盖全部 Job。
            attempt = json.loads((attempt_root / "attempt.json").read_text("utf-8"))
            for item in attempt["results"]:
                item["status"] = "complete"
            self._write_json(attempt_root / "attempt.json", attempt)
            narrowed = dict(manifest)
            narrowed["reuse_items"] = list(self._POST_RUN_JOB_IDS[:-1])
            uncovered = supervisor.post_run_tooling_facts(
                campaign_dir, narrowed, run_started_at_utc=run_started
            )
            self.assertTrue(any("未覆盖" in note for note in uncovered["reasons"]))

            # 某个 Job 缺少 complete checkpoint：result 与 checkpoint 必须同时存在。
            tail = attempt_root / "checkpoints" / "00000009.json"
            tail_payload = json.loads(tail.read_text("utf-8"))
            tail_payload["status"] = "failed"
            self._write_json(tail, tail_payload)
            short = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertTrue(any("checkpoint" in note for note in short["reasons"]))
            tail_payload["status"] = "complete"
            self._write_json(tail, tail_payload)

            # reservation 在本 run 内创建：属于 attempt 中断。
            future_run = supervisor._epoch_to_utc(time.time() - 3600.0)
            interrupted = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=future_run
            )
            self.assertTrue(any("reservation" in note for note in interrupted["reasons"]))

            # 第二个 awaiting_receipts attempt：目标不唯一。
            second = attempt_root.parent / "20260917T200000Z-0000000000000000"
            second.mkdir(mode=0o700)
            self._write_json(second / "attempt.json", {**attempt, "attempt_id": second.name})
            ambiguous = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertTrue(any("恰好一个" in note for note in ambiguous["reasons"]))

            # 正例：全部判据满足。
            (second / "attempt.json").unlink()
            second.rmdir()
            accepted = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertTrue(accepted["qualifies"], accepted["reasons"])
            self.assertIsNone(accepted["facts"]["kilo_window_started_at_utc"])

    @staticmethod
    def _canonical_action(campaign_dir: Path, item_id: str, *, attempt_id: str) -> dict[str, object]:
        subcommand, step = vc_artifacts.CANONICAL_VC5_ITEM_COMMANDS[item_id]
        command = [
            sys.executable,
            str(Path(supervisor.__file__).with_name("codex_upgrade.py")),
            subcommand,
            "--campaign-dir",
            str(campaign_dir),
            "--candidate-id",
            "cand-1",
            "--attempt-id",
            attempt_id,
        ]
        if step is None:
            command += [
                "--kilo-facts",
                str(campaign_dir / "missing-kilo-facts.json"),
                "--active-profile",
                str(campaign_dir / "missing-active-profile.json"),
                "--profile-patch-manifest",
                str(campaign_dir / "missing-patches.json"),
                "--profile-activation-fact",
                str(campaign_dir / "missing-activation.json"),
                "--phase",
                "VC-5",
                "--retire-version",
                "0.149.1",
                "--approve-import-sha256",
                "a" * 64,
            ]
        else:
            command += ["--canonical-step", step]
        return {
            "action_id": f"canonical-{vc_artifacts.CANONICAL_VC5_ITEM_ORDER.index(item_id) + 1}-{step or 'import'}",
            "operation": f"VC-5:{item_id}",
            "timeout_seconds": 30.0,
            "command": command,
            "item_ids": [item_id],
        }

    def test_post_run_tooling_canonical_batch_checks_only_bound_attempt(self) -> None:
        """canonical 批次按冻结映射从命令提取 attempt，只检查该 attempt；套名与混入都拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, _ledger_root, seed, attempt_root = self._post_run_tooling_fixture(root)
            attempt_id = attempt_root.name
            items = sorted(vc_artifacts.CANONICAL_VC5_ITEM_COMMANDS)
            actions = [
                self._canonical_action(campaign_dir, item, attempt_id=attempt_id)
                for item in vc_artifacts.CANONICAL_VC5_ITEM_ORDER
            ]
            manifest = supervisor.build_batched_campaign_run_manifest(
                campaign_id="campaign-closeout",
                campaign_plan_sha256=str(seed["campaign_plan_sha256"]),
                batch_id="vc-5-0002",
                batch_sequence=2,
                batch_sha256="7" * 64,
                phase="VC-5",
                candidate_revision=1,
                candidate_id="cand-1",
                predecessor_checkpoint=dict(seed["predecessor_checkpoint"]),
                original_deadline_at_utc="2099-09-15T08:12:43Z",
                actions=actions,
                execute_items=items,
                reuse_items=list(self._POST_RUN_JOB_IDS),
            )
            run_started = supervisor._epoch_to_utc(time.time())
            accepted = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertTrue(accepted["qualifies"], accepted["reasons"])
            self.assertEqual(accepted["facts"]["attempt_id"], attempt_id)
            self.assertEqual(
                accepted["facts"]["canonical_binding"],
                {"candidate_id": "cand-1", "attempt_id": attempt_id, "item_ids": items},
            )

            # 同一 Campaign 出现第二个等待收据的 attempt：旧路径会因"恰好一个"拒绝，
            # canonical 批次只看命令指定的 attempt，仍然成立。
            attempt = json.loads((attempt_root / "attempt.json").read_text("utf-8"))
            second = attempt_root.parent / "20260918T000000Z-0000000000000000"
            second.mkdir(mode=0o700)
            self._write_json(second / "attempt.json", {**attempt, "attempt_id": second.name})
            still = supervisor.post_run_tooling_facts(
                campaign_dir, manifest, run_started_at_utc=run_started
            )
            self.assertTrue(still["qualifies"], still["reasons"])
            legacy = supervisor.post_run_tooling_facts(
                campaign_dir, seed, run_started_at_utc=run_started
            )
            self.assertTrue(any("恰好一个" in note for note in legacy["reasons"]))

            # 指定的 attempt 不存在。
            missing_actions = [
                self._canonical_action(campaign_dir, item, attempt_id="20260918T111111Z-1111111111111111")
                for item in vc_artifacts.CANONICAL_VC5_ITEM_ORDER
            ]
            missing = supervisor.post_run_tooling_facts(
                campaign_dir, {**manifest, "actions": missing_actions}, run_started_at_utc=run_started
            )
            self.assertTrue(any("不存在" in note for note in missing["reasons"]))

            # 给别的命令套 canonical item 名换取可恢复分类：拒绝。
            disguised = dict(manifest)
            disguised["actions"] = [
                {
                    "action_id": "canonical-2-seal",
                    "operation": "VC-5:canonical-seal",
                    "timeout_seconds": 5.0,
                    "command": [sys.executable, "-c", "raise SystemExit(3)"],
                    "item_ids": ["canonical-seal"],
                }
            ]
            disguised["execute_items"] = ["canonical-seal"]
            rejected = supervisor.post_run_tooling_facts(
                campaign_dir, disguised, run_started_at_utc=run_started
            )
            self.assertTrue(any("冻结映射" in note for note in rejected["reasons"]))

            # 混入普通后处理项：拒绝。
            mixed = dict(manifest)
            mixed["actions"] = [
                *actions,
                {
                    "action_id": "seal",
                    "operation": "VC-5:seal",
                    "timeout_seconds": 5.0,
                    "command": [sys.executable, "-c", "raise SystemExit(3)"],
                    "item_ids": ["candidate-seal"],
                },
            ]
            mixed["execute_items"] = ["candidate-seal", *items]
            self.assertTrue(
                any(
                    "冻结映射" in note
                    for note in supervisor.post_run_tooling_facts(
                        campaign_dir, mixed, run_started_at_utc=run_started
                    )["reasons"]
                )
            )

            # 指向别的 Campaign 目录：拒绝。
            foreign_actions = [
                {**action, "command": [
                    str(root / "other-campaign") if token == str(campaign_dir) else token
                    for token in action["command"]
                ]}
                for action in actions
            ]
            (root / "other-campaign").mkdir(mode=0o700)
            foreign = supervisor.post_run_tooling_facts(
                campaign_dir, {**manifest, "actions": foreign_actions}, run_started_at_utc=run_started
            )
            self.assertTrue(any("不是本 Campaign" in note for note in foreign["reasons"]))

    def test_post_run_tooling_canonical_action_failure_pauses_for_redispatch(self) -> None:
        """端到端：canonical 批次里真实 canonical-import 失败，父监督器升级为 post-run-tooling 并暂停。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, seed, attempt_root = self._post_run_tooling_fixture(
                root, kilo_window_offset_seconds=-120.0
            )
            items = sorted(vc_artifacts.CANONICAL_VC5_ITEM_COMMANDS)
            manifest = supervisor.build_batched_campaign_run_manifest(
                campaign_id="campaign-closeout",
                campaign_plan_sha256=str(seed["campaign_plan_sha256"]),
                batch_id="vc-5-0001",
                batch_sequence=1,
                batch_sha256="7" * 64,
                phase="VC-5",
                candidate_revision=1,
                candidate_id="cand-1",
                predecessor_checkpoint=dict(seed["predecessor_checkpoint"]),
                original_deadline_at_utc="2099-09-15T08:12:43Z",
                actions=[
                    self._canonical_action(campaign_dir, item, attempt_id=attempt_root.name)
                    for item in vc_artifacts.CANONICAL_VC5_ITEM_ORDER
                ],
                execute_items=items,
                reuse_items=list(self._POST_RUN_JOB_IDS),
            )
            returncode, payload, run_dir = self._run_post_run_tooling_parent(
                root, manifest, campaign_dir
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(payload["reason"], "action-failed:canonical-1-import")
            diagnostic = payload["actions"][0]["diagnostic"]
            self.assertEqual(diagnostic["failure_class"], "execution-failure")
            self.assertEqual(diagnostic["effective_failure_class"], "post-run-tooling")
            self.assertTrue((run_dir / diagnostic["post_run_tooling_receipt"]["path"]).is_file())
            self.assertEqual(payload["timing_closeout"]["ledger_status"], "recovery_required")
            self.assertEqual(timing_ledger.inspect_ledger(ledger_root)["status"], "recovery_required")

    def test_post_run_tooling_never_overrides_explicit_child_classification(self) -> None:
        """子进程显式给出的非默认分类（如 evidence-integrity）不得被升级或改写。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            child = (
                "import sys; "
                "from tools.official_client_capture import "
                "codex_upgrade_supervisor as s; "
                "error=RuntimeError('证据完整性失败'); "
                "s.write_campaign_run_action_diagnostic("
                "failure_kind='handled-error', "
                "failure_class='evidence-integrity', error=error); "
                "sys.exit(7)"
            )
            campaign_dir, ledger_root, manifest, _attempt_root = (
                self._post_run_tooling_fixture(
                    root, child_command=[sys.executable, "-c", child]
                )
            )
            returncode, payload, run_dir = self._run_post_run_tooling_parent(
                root, manifest, campaign_dir
            )
            self.assertEqual(returncode, 1)
            diagnostic = payload["actions"][0]["diagnostic"]
            self.assertEqual(diagnostic["failure_class"], "evidence-integrity")
            self.assertEqual(diagnostic["effective_failure_class"], "evidence-integrity")
            self.assertNotIn("post_run_tooling_receipt", diagnostic)
            self.assertEqual(payload["timing_closeout"]["ledger_status"], "stopped")
            self.assertEqual(timing_ledger.inspect_ledger(ledger_root)["status"], "stopped")

    def test_batched_failure_closeout_rejects_plan_digest_drift(self) -> None:
        """自摘要不符或计划文件被改写时都必须失败关闭，且账本保持 active。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest, plan = self._batched_closeout_fixture(
                root
            )
            drifted = dict(manifest)
            drifted["campaign_plan_sha256"] = "3" * 64
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "父批次与 Campaign 总计划摘要不一致"
            ):
                supervisor._close_failed_campaign_timing_ledger(
                    campaign_dir,
                    drifted,
                    failed_action_id="failing-action",
                )
            plan_path = campaign_dir / "control" / "vc" / "campaign-plan.json"
            tampered = dict(plan)
            tampered["campaign_purpose"] = "validation_only"
            self._write_json(plan_path, tampered)
            with self.assertRaisesRegex(
                supervisor.SupervisorError, "Campaign 总计划文件或摘要漂移"
            ):
                supervisor._close_failed_campaign_timing_ledger(
                    campaign_dir,
                    manifest,
                    failed_action_id="failing-action",
                )
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual(summary["active_phase"], "VC-1")

    def test_parent_action_failure_closes_upgrade_timing_ledger(self) -> None:
        """父动作非零后不得留下 active/VC-1。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._timing_closeout_fixture(root)
            state_dir = root / "supervisor-state"
            state_dir.mkdir(mode=0o700)
            returncode, payload = supervisor._campaign_run_locked(
                argparse.Namespace(
                    heartbeat_seconds=0.05,
                    watchdog_timeout_seconds=0.5,
                    ledger_interval_seconds=0.05,
                ),
                manifest=manifest,
                state_dir=state_dir,
                campaign_dir=campaign_dir,
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(payload["timing_closeout"]["status"], "passed")
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual(summary["status"], "stage_review_required")
            self.assertIsNone(summary["active_phase"])
            events = [
                event["event_type"]
                for event, _raw in timing_ledger._load_events(ledger_root)
            ]
            self.assertEqual(events[-2:], ["stage_abandoned", "stage_review_required"])

    def test_timing_closeout_recovers_partial_stage_abandoned(self) -> None:
        """首个事件已落盘而第二个事件失败时，重入只能确定性补齐。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._timing_closeout_fixture(root)
            real_append = timing_ledger.append_event
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise timing_ledger.TimingLedgerError("模拟第二次追加失败")
                return real_append(*args, **kwargs)

            with (
                mock.patch.object(
                    supervisor.timing_ledger,
                    "append_event",
                    side_effect=fail_second,
                ),
                self.assertRaisesRegex(supervisor.SupervisorError, "stop_the_line"),
            ):
                supervisor._close_failed_campaign_timing_ledger(
                    campaign_dir,
                    manifest,
                    failed_action_id="failing-action",
                )
            middle = timing_ledger.inspect_ledger(ledger_root)
            self.assertIsNone(middle["active_phase"])
            self.assertTrue(str(middle["last_event_id"]).endswith("stage-abandoned"))
            result = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                manifest,
                failed_action_id="failing-action",
            )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(timing_ledger.inspect_ledger(ledger_root)["status"], "stage_review_required")

    def _budget_bound_closeout_fixture(self, root: Path):
        """VC-1 已开始且阶段预算已到期的正式 Campaign；账本绑定项目总账，可真实暂停与批准延期。"""

        from datetime import datetime, timedelta, timezone
        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)

        def at(seconds: int) -> str:
            return (start + timedelta(seconds=seconds)).isoformat()

        # 仅供夹具的总账必须位于 staging 目录树内。
        data_root = root / "staging"
        data_root.mkdir(mode=0o700)
        project_root = data_root / project_ledger.LEDGER_DIR_NAME
        project_ledger.create_project_ledger(
            project_root, project_id="r4r8-fixture", absolute_deadline_utc=at(24 * 3600),
            deadline_approved_by="fixture", estimation_policy="none", estimation_policy_approved_by="fixture",
            fixture_only=True, started_at_utc=at(0), initial_precise_count=0,
        )
        campaign_dir = data_root / "evidence" / "campaigns" / "campaign-closeout"
        campaign_dir.mkdir(parents=True, mode=0o700)
        for parent in (data_root / "evidence", data_root / "evidence" / "campaigns"):
            parent.chmod(0o700)
        ledger_root = data_root / "control" / "timing-ledger"
        ledger_root.parent.mkdir(mode=0o700)
        timing_ledger.create_ledger(
            ledger_root, upgrade_id="campaign-closeout", baseline_version="0.151.0", target_version="0.154.0",
            campaign_purpose="production_replacement", evidence_decision="recapture", started_at_utc=at(0),
            total_budget_minutes=600, stage_budgets_minutes={phase: 1 for phase in timing_ledger.PHASE_ORDER},
            project_ledger_dir=project_root,
        )
        timing_ledger.append_event(ledger_root, event_id="fixture-vc0-completed", phase="VC-0",
                                   event_type="stage_completed", next_action="启动 VC-1", recorded_at_utc=at(1))
        timing_ledger.append_event(ledger_root, event_id="fixture-vc1-started", phase="VC-1",
                                   event_type="stage_started", next_action="运行父批次", recorded_at_utc=at(2))
        self._write_json(campaign_dir / "campaign.json", {
            "campaign_id": "campaign-closeout", "campaign_mode": "formal",
            "campaign_purpose": "production_replacement", "baseline_version": "0.151.0", "target_version": "0.154.0",
            "control_receipts": {"upgrade_timing": {
                "ledger_dir": str(ledger_root), "upgrade_id": "campaign-closeout",
                "ledger_plan_sha256": supervisor._sha256((ledger_root / "ledger.json").read_bytes()),
            }},
        })
        (campaign_dir / "control" / "vc").mkdir(parents=True, mode=0o700)
        (campaign_dir / "control").chmod(0o700)
        self._write_json(campaign_dir / "control" / "vc" / "campaign-plan.json",
                         {"campaign_id": "campaign-closeout", "original_deadline_at_utc": at(600 * 60)})
        project_ledger.register_existing_campaign(campaign_dir)
        manifest = build_campaign_run_manifest(
            "campaign-closeout", "VC-1", 30,
            actions=[{"action_id": "failing-action", "operation": "VC-1:failing-action", "timeout_seconds": 5,
                      "command": [sys.executable, "-c", "raise SystemExit(7)"]}],
        )
        return campaign_dir, ledger_root, manifest

    def test_abandon_then_budget_pause_completes_review_before_pausing(self) -> None:
        """R4×R8：放弃已写、审核未写时被杀且预算随后到期；重入先补齐审核再登记暂停，阶段层延期随后可用。"""

        from datetime import datetime, timedelta, timezone
        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._budget_bound_closeout_fixture(root)
            real_append = timing_ledger.append_event
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise timing_ledger.TimingLedgerError("模拟写入 review 之前进程被杀")
                return real_append(*args, **kwargs)

            # 首次收口发生在预算到期之前：替身只让这一次看不到暂停，从而落到“已放弃、未审核”的中间态。
            not_yet_paused = {**vc_artifacts.effective_deadlines(campaign_dir), "paused_scopes": []}
            with (
                mock.patch.object(supervisor.timing_ledger, "append_event", side_effect=fail_second),
                mock.patch.object(supervisor.vc_artifacts, "effective_deadlines", return_value=not_yet_paused),
                self.assertRaisesRegex(supervisor.SupervisorError, "stop_the_line"),
            ):
                supervisor._close_failed_campaign_timing_ledger(campaign_dir, manifest, failed_action_id="failing-action")
            events = [event["event_type"] for event, _ in timing_ledger._load_events(ledger_root)]
            self.assertEqual(events[-1], "stage_abandoned")

            result = supervisor._close_failed_campaign_timing_ledger(campaign_dir, manifest, failed_action_id="failing-action")
            self.assertEqual(result["ledger_status"], "deadline_paused")
            events = [event["event_type"] for event, _ in timing_ledger._load_events(ledger_root)]
            self.assertEqual(events[-3:], ["stage_abandoned", "stage_review_required", "deadline_paused"])
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual((summary["status"], summary["status_before_pause"]), ("deadline_paused", "stage_review_required"))

            preview = project_ledger.preview_deadline_extension(
                campaign_dir, scope="stage", phase="VC-1",
                new_deadline_at_utc=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                reason="隔离测试：补齐审核后继续人工对账",
            )
            project_ledger.apply_deadline_extension(
                campaign_dir, preview_path=Path(preview["preview_path"]),
                approve_sha256=preview["review_sha256"], approved_by="fixture-reviewer",
            )
            self.assertEqual(timing_ledger.inspect_ledger(ledger_root)["status"], "stage_review_required")
            again = supervisor._close_failed_campaign_timing_ledger(campaign_dir, manifest, failed_action_id="failing-action")
            self.assertEqual((again["ledger_status"], again["idempotent"]), ("stage_review_required", True))

    def test_runtime_budget_deadline_missing_anchor_is_state_contract_error(self) -> None:
        """R8：父 run 状态缺 deadline_at_epoch 时按状态合同报 SupervisorError，而不是 KeyError。"""

        with self.assertRaisesRegex(supervisor.SupervisorError, "deadline_at_epoch"):
            supervisor._runtime_budget_deadline({})

    def test_runtime_budget_deadline_without_parsable_layers_uses_parent_anchor(self) -> None:
        """三层都没有可解析截止（历史 Campaign 无计时账本）时只受父 run 自身时间锚约束。"""

        state = {"deadline_at_epoch": 1_900_000_000.0, "budget_guard": {"campaign_dir": "/nonexistent/campaign"}}
        with mock.patch.object(supervisor.vc_artifacts, "effective_deadlines",
                               return_value={"paused_scopes": [], "execution_deadline_at_utc": None}):
            self.assertEqual(supervisor._runtime_budget_deadline(state), 1_900_000_000.0)

    def test_closeout_with_only_extension_pending_fails_explicitly_without_writes(self) -> None:
        """R8：只有其他 Campaign 的延期未闭合（extension_pending）时不冒报 deadline_paused，明确失败且不写账本。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._timing_closeout_fixture(root)
            before = {path.name: path.read_bytes() for path in (ledger_root / "events").iterdir()}
            pending = {**vc_artifacts.effective_deadlines(campaign_dir), "paused_scopes": ["extension_pending"]}
            with mock.patch.object(supervisor.vc_artifacts, "effective_deadlines", return_value=pending):
                with self.assertRaisesRegex(supervisor.SupervisorError, "extension_pending"):
                    supervisor._close_failed_campaign_timing_ledger(campaign_dir, manifest, failed_action_id="failing-action")
            self.assertEqual(before, {path.name: path.read_bytes() for path in (ledger_root / "events").iterdir()})

    def test_closeout_reports_budget_race_instead_of_claiming_pause(self) -> None:
        """判定到期后、登记暂停前预算状态变化（登记返回 not_expired）时明确失败，不冒报 deadline_paused。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, ledger_root, manifest = self._timing_closeout_fixture(root)
            expired = {**vc_artifacts.effective_deadlines(campaign_dir), "paused_scopes": ["stage"]}
            with (
                mock.patch.object(supervisor.vc_artifacts, "effective_deadlines", return_value=expired),
                mock.patch.object(supervisor.project_ledger, "pause_campaign_deadline", return_value={"status": "not_expired"}),
                self.assertRaisesRegex(supervisor.SupervisorError, "收口期间变化"),
            ):
                supervisor._close_failed_campaign_timing_ledger(campaign_dir, manifest, failed_action_id="failing-action")

    def test_parent_reports_timing_closeout_failure(self) -> None:
        """账本闭合失败必须进入父结果，不能只留下 action-failed。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign_dir, _ledger_root, manifest = self._timing_closeout_fixture(root)
            state_dir = root / "supervisor-state"
            state_dir.mkdir(mode=0o700)
            with mock.patch.object(
                supervisor,
                "_close_failed_campaign_timing_ledger",
                side_effect=supervisor.SupervisorError("模拟账本闭合失败"),
            ):
                returncode, payload = supervisor._campaign_run_locked(
                    argparse.Namespace(
                        heartbeat_seconds=0.05,
                        watchdog_timeout_seconds=0.5,
                        ledger_interval_seconds=0.05,
                    ),
                    manifest=manifest,
                    state_dir=state_dir,
                    campaign_dir=campaign_dir,
                )
            self.assertEqual(returncode, 1)
            self.assertEqual(payload["timing_closeout"]["status"], "failed")
            self.assertIn("模拟账本闭合失败", payload["timing_closeout"]["message"])

    def test_vc1_assertion_seal_order_and_permission_receipt_gate(self) -> None:
        """新 Attempt 只有在权限收据通过且 assertion 位于 seal 前时可派发。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            data_root = root / "data"
            (data_root / "runs").mkdir(parents=True, mode=0o700)
            campaign_dir = data_root / "evidence" / "campaigns" / "campaign-gate"
            attempt_root = campaign_dir / "official" / "attempts" / "attempt-gate"
            evidence_root = attempt_root / "evidence"
            logs_root = attempt_root / "logs"
            evidence_root.mkdir(parents=True, mode=0o700)
            logs_root.mkdir(mode=0o700)
            attempt_root.chmod(0o700)
            evidence_root.chmod(0o700)
            self._write_json(
                campaign_dir / "campaign.json",
                {"campaign_id": "campaign-gate"},
            )
            receipt_path, _receipt = supervisor.evidence_permissions.close_evidence_permissions(
                attempt_root,
                [evidence_root, logs_root],
                managed_data_root=data_root,
            )
            binding = supervisor.evidence_permissions.receipt_binding(
                attempt_root,
                receipt_path,
            )
            attempt = {
                "schema_version": "codex-upgrade-capture-attempt/v3",
                "campaign_id": "campaign-gate",
                "phase": "official",
                "candidate_id": None,
                "attempt_id": "attempt-gate",
                "status": "awaiting_receipts",
                "evidence_roots": [str(evidence_root), str(logs_root)],
                "evidence_permission_closeout": binding,
                "evidence_permission_error": None,
            }
            attempt["attempt_digest"] = supervisor._attempt_fingerprint(attempt)
            self._write_json(attempt_root / "attempt.json", attempt)
            assertion = {
                "action_id": "prepare-official-assertion-bundle",
                "operation": "VC-1:prepare-official-assertion-bundle",
                "timeout_seconds": 5.0,
                "command": [
                    "/usr/bin/env",
                    f"CAMPAIGN_DIR={campaign_dir}",
                    "ATTEMPT_ID=attempt-gate",
                    "SIDE=official",
                    "/usr/bin/bash",
                    "/tmp/prepare-assertion.sh",
                ],
                "item_ids": ["prepare-official-assertion-bundle"],
            }
            seal = {
                "action_id": "seal-official-preview",
                "operation": "VC-1:capture-official-seal-preview",
                "timeout_seconds": 5.0,
                "command": [
                    sys.executable,
                    "/tmp/codex_upgrade.py",
                    "capture-official",
                    "seal",
                    "--campaign-dir",
                    str(campaign_dir),
                    "--attempt-id",
                    "attempt-gate",
                ],
                "item_ids": ["seal-official-preview"],
            }
            supervisor._validate_vc1_assertion_seal_gate(
                schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                campaign_id="campaign-gate",
                phase="VC-1",
                actions=[assertion, seal],
                require_bound_files=True,
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "必须先生成"):
                supervisor._validate_vc1_assertion_seal_gate(
                    schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    campaign_id="campaign-gate",
                    phase="VC-1",
                    actions=[seal, assertion],
                    require_bound_files=True,
                )

            attempt["evidence_permission_closeout"] = None
            attempt["evidence_permission_error"] = {
                "type": "FixtureError",
                "message": "模拟权限收口失败",
            }
            attempt.pop("attempt_digest")
            attempt["attempt_digest"] = supervisor._attempt_fingerprint(attempt)
            (attempt_root / "attempt.json").write_text(
                json.dumps(attempt, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (attempt_root / "attempt.json").chmod(0o600)
            with self.assertRaisesRegex(supervisor.SupervisorError, "没有通过"):
                supervisor._validate_vc1_assertion_seal_gate(
                    schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    campaign_id="campaign-gate",
                    phase="VC-1",
                    actions=[assertion, seal],
                    require_bound_files=True,
                )


if __name__ == "__main__":
    unittest.main()
