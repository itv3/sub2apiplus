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



def textwrap_dedent(source: str) -> str:
    """inspect.getsource 取到的是缩进为零的顶层函数，这里只做防御性去缩进。"""

    import textwrap

    return textwrap.dedent(source)

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

    def _wait_for_file(self, path: Path, *, timeout: float = 5) -> None:
        """有界等待文件出现（修好接着跑第 34 项：替代固定 sleep）。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                return
            time.sleep(0.02)
        self.fail(f"文件未在预算内出现：{path}")

    def _wait_campaign_monitor_exit(self, run_dir: Path, *, timeout: float = 5) -> None:
        """等父监督器常驻进程退出：终态之后它还会收尾写入，tempfile 清理前不等会偶发 Directory not empty（第 34 项）。"""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            pid = state.get("monitor_pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            time.sleep(0.05)
        self.fail("父监督器进程未在预算内退出")

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
        timeout_seconds: float = 3,
    ) -> subprocess.Popen[str]:
        """启动真实 campaign-exec，供退出和强停路径共用。

        ``timeout_seconds`` 是交给 campaign-exec 的动作超时（默认 3 秒与历史行为一致）；
        排空预算用例用它制造“动作＋排空窗口放不下”的条件（修好接着跑第 34 项扩展）。
        """

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
                str(timeout_seconds),
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

    def test_failed_official_timeout_cleanup_allows_normal_v2_recovery_preview(self) -> None:
        """动作执行截止到期、子进程在清理宽限内自行封口时，同样以普通 v2 预览承接。

        宽限耗尽被强杀（cleanup-window-expired）或诊断不是截止清理时仍失败关闭。
        """

        def build(root: Path, *, event_reason: str, failure_class: str) -> tuple[
            dict[str, object], dict[str, object], Path, dict[str, object]
        ]:
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            prior_dir = root / "run-prior"
            prior_dir.mkdir(mode=0o700)
            campaign_id = "campaign-official-timeout"
            owner_nonce = "8" * 64
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]
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
                            *prefix,
                            "capture-official",
                            "run",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["passed-job", "pending-job"],
                    }
                ],
                "execute_items": ["passed-job", "pending-job"],
                "reuse_items": [],
            }
            prior_state: dict[str, object] = {
                "state": "failed",
                "campaign_id": campaign_id,
                "phase": "VC-1",
                "owner_pid": os.getpid(),
                "owner_nonce": owner_nonce,
                "terminal_at_utc": "2026-09-25T06:07:17.000Z",
            }
            self._write_json(prior_dir / "state.json", prior_state)
            stop: dict[str, object] = {
                "schema_version": supervisor.STOP_SCHEMA,
                "campaign_id": campaign_id,
                "detected_at_epoch": 1005.0,
                "detected_at_utc": "2026-09-25T06:07:17.000Z",
                "event_type": "failed",
                "owner_nonce": owner_nonce,
                "owner_pid": os.getpid(),
                "phase": "VC-1",
                "reason": "SupervisorTimeout",
            }
            stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
            self._write_json(prior_dir / "stop-receipt.json", stop)
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(
                    prior_dir, "capture-official", create_directory=True
                ),
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="capture-official",
                owner_pid=os.getpid(),
                owner_nonce=owner_nonce,
                failure_kind="handled-error",
                failure_class=failure_class,
                error_type="CampaignCleanupRequested",
                message="父监督器数据面截止已到，正在原始 deadline 内执行 attempt 清理。",
            )
            # 最小合法事件链：只含父动作的失败事件，序号、自摘要与链摘要成立。
            event: dict[str, object] = {
                "sequence": 1,
                "event_type": "action-failed",
                "operation": "VC-1:capture-official",
                "reason": event_reason,
                "previous_event_sha256": None,
            }
            event["event_sha256"] = supervisor._sha256(supervisor._canonical(event))
            (prior_dir / "events.ndjson").write_text(
                json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            (prior_dir / "events.ndjson").chmod(0o600)
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
                        "timeout_seconds": 3600.0,
                        "command": [
                            *prefix,
                            "resume",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--rerun-failed",
                            "--preview-recovery",
                        ],
                        "item_ids": ["pending-job"],
                    }
                ],
                "execute_items": ["pending-job"],
                "reuse_items": ["passed-job"],
            }
            return prior_state, prior_manifest, prior_dir, successor

        with tempfile.TemporaryDirectory() as directory:
            prior_state, prior_manifest, prior_dir, successor = build(
                Path(directory).resolve(),
                event_reason="cleanup-requested-timeout",
                failure_class="deadline-expired",
            )
            ordered = supervisor._validate_batched_campaign_history(
                successor, [(prior_state, prior_manifest, prior_dir)]
            )
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

        # B4-1 改法 7：宽限耗尽强杀（cleanup-window-expired）或诊断不是截止清理时 attempt 可能未封口——不再按诊断
        # 白名单失败关闭，改为要求父 run 已对账（有预约认 attempt 收据、无预约认 supervisor-run 收据）后承接。
        for event_reason, failure_class in (
            ("cleanup-window-expired", "deadline-expired"),
            ("cleanup-requested-timeout", "execution-failure"),
        ):
            with self.subTest(event_reason=event_reason, failure_class=failure_class):
                with tempfile.TemporaryDirectory() as directory:
                    prior_state, prior_manifest, prior_dir, successor = build(
                        Path(directory).resolve(),
                        event_reason=event_reason,
                        failure_class=failure_class,
                    )
                    history = [(prior_state, prior_manifest, prior_dir)]
                    campaign_dir = prior_dir.parent / "campaign"
                    # 协议 8 从后继命令里就知道 Campaign 目录：不传目录参数也按同一目录核对对账收据。
                    with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"):
                        supervisor._validate_batched_campaign_history(successor, history)
                    with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"):
                        supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
                    self._b4_bind_supervisor_run_reconciliation(
                        campaign_dir, prior_dir, prior_state, prior_manifest, failure_class=failure_class
                    )
                    ordered = supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
                    self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

    def test_failed_recovery_preview_is_redispatched_verbatim_as_next_sequence(self) -> None:
        """零请求恢复预览因处理型错误失败后，修复部署即可以 N+1 逐字重派同一预览。

        0.156.1 VC-1：序号 2 预览因复用校验缺陷失败。后继只能是同一预览批次的逐字重派；
        改成真实补跑、改动 execute／reuse 分区，或父预览以截止清理失败，一律拒绝。
        """

        def build(root: Path, *, preview_failure: tuple[str, str, str]) -> tuple[
            list[tuple[dict[str, object], dict[str, object], Path]], dict[str, object]
        ]:
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_id = "campaign-official-preview-retry"
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]

            def manifest(sequence: int, actions: list, execute: list, reuse: list) -> dict[str, object]:
                return {
                    "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    "campaign_id": campaign_id,
                    "campaign_plan_sha256": "1" * 64,
                    "batch_id": f"vc-1-{sequence:04d}",
                    "batch_sequence": sequence,
                    "batch_sha256": str(sequence + 4) * 64,
                    "phase": "VC-1",
                    "predecessor_checkpoint": checkpoint,
                    "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                    "no_op": False,
                    "actions": actions,
                    "execute_items": execute,
                    "reuse_items": reuse,
                }

            def failed_run(name: str, owner_nonce: str, reason: str) -> tuple[dict[str, object], Path]:
                run_dir = root / name
                run_dir.mkdir(mode=0o700)
                state: dict[str, object] = {
                    "state": "failed",
                    "campaign_id": campaign_id,
                    "phase": "VC-1",
                    "owner_pid": os.getpid(),
                    "owner_nonce": owner_nonce,
                    "terminal_at_utc": "2026-09-25T07:16:32.000Z",
                }
                self._write_json(run_dir / "state.json", state)
                stop: dict[str, object] = {
                    "schema_version": supervisor.STOP_SCHEMA,
                    "campaign_id": campaign_id,
                    "detected_at_epoch": 1005.0,
                    "detected_at_utc": "2026-09-25T07:16:31.986Z",
                    "event_type": "failed",
                    "owner_nonce": owner_nonce,
                    "owner_pid": os.getpid(),
                    "phase": "VC-1",
                    "reason": reason,
                }
                stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
                self._write_json(run_dir / "stop-receipt.json", stop)
                return state, run_dir

            capture_manifest = manifest(
                1,
                [
                    {
                        "action_id": "capture-official",
                        "operation": "VC-1:capture-official",
                        "timeout_seconds": 3600.0,
                        "command": [
                            *prefix,
                            "capture-official",
                            "run",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["passed-job", "pending-job"],
                    }
                ],
                ["passed-job", "pending-job"],
                [],
            )
            capture_state, capture_dir = failed_run(
                "run-capture", "8" * 64, "action-failed:capture-official"
            )
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(
                    capture_dir, "capture-official", create_directory=True
                ),
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="capture-official",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
                failure_kind="child-returncode",
                error_type="ChildProcessError",
                message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
            )
            preview_actions = [
                {
                    "action_id": "preview-official-recovery",
                    "operation": "VC-1:official-recovery",
                    "timeout_seconds": 3600.0,
                    "command": [
                        *prefix,
                        "resume",
                        "--campaign-dir",
                        str(campaign_dir),
                        "--rerun-failed",
                        "--preview-recovery",
                    ],
                    "item_ids": ["pending-job"],
                }
            ]
            preview_manifest = manifest(2, preview_actions, ["pending-job"], ["passed-job"])
            preview_state, preview_dir = failed_run(
                "run-preview", "9" * 64, "action-failed:preview-official-recovery"
            )
            failure_kind, error_type, failure_class = preview_failure
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(
                    preview_dir, "preview-official-recovery", create_directory=True
                ),
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="preview-official-recovery",
                owner_pid=os.getpid(),
                owner_nonce="9" * 64,
                failure_kind=failure_kind,
                failure_class=failure_class,
                error_type=error_type,
                message="错误详情已按脱敏规则省略。",
            )
            history = [
                (capture_state, capture_manifest, capture_dir),
                (preview_state, preview_manifest, preview_dir),
            ]
            retry = manifest(3, copy.deepcopy(preview_actions), ["pending-job"], ["passed-job"])
            # B4-1 第 31 项：恢复链协议要求失败父 run 已对账——补一份形态合法的 supervisor-run 收据与总账绑定。
            self._b4_bind_supervisor_run_reconciliation(
                campaign_dir, preview_dir, preview_state, preview_manifest, failure_class=failure_class
            )
            return campaign_dir, history, retry

        handled = ("handled-error", "ConfigurationError", "execution-failure")
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, history, retry = build(Path(directory).resolve(), preview_failure=handled)
            ordered = supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])

            # 失败预览之后直接改走真实补跑：不是逐字重派，拒绝。
            live = copy.deepcopy(retry)
            live["actions"][0]["action_id"] = "run-official-recovery"
            live["actions"][0]["command"] = [
                *live["actions"][0]["command"][:2],
                "resume",
                "--campaign-dir",
                live["actions"][0]["command"][4],
                "--rerun-failed",
                "--recovery-preview",
                "/tmp/recovery-preview.json",
                "--acknowledge-live-requests",
            ]
            with self.assertRaisesRegex(SupervisorError, "逐字沿用父预览批次"):
                supervisor._validate_batched_campaign_history(live, history, campaign_dir=campaign_dir)

            # 改动 execute／reuse 分区：拒绝。
            drifted = copy.deepcopy(retry)
            drifted["execute_items"] = ["pending-job", "passed-job"]
            drifted["reuse_items"] = []
            drifted["actions"][0]["item_ids"] = ["pending-job", "passed-job"]
            with self.assertRaisesRegex(SupervisorError, "逐字沿用父预览批次"):
                supervisor._validate_batched_campaign_history(drifted, history, campaign_dir=campaign_dir)

        # 父预览以截止清理失败：不属于处理型失败，拒绝。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, history, retry = build(
                Path(directory).resolve(),
                preview_failure=("handled-error", "CampaignCleanupRequested", "deadline-expired"),
            )
            with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
        # 修好接着跑第 30 项：执行失败不再按错误类型白名单——工具缺陷抛的其它异常、意外异常、子进程非零退出都可承接；
        # 中断种类、非 execution-failure 类别、清理／中断异常仍拒绝。
        for accepted in (
            ("handled-error", "ValueError", "execution-failure"),
            ("unexpected-error", "RuntimeError", "execution-failure"),
            ("child-returncode", "ChildProcessError", "execution-failure"),
        ):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                campaign_dir, history, retry = build(Path(directory).resolve(), preview_failure=accepted)
                ordered = supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
                self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])
        for rejected in (
            ("interrupted", "KeyboardInterrupt", "execution-failure"),
            ("handled-error", "ConfigurationError", "deadline-expired"),
            ("handled-error", "CampaignCleanupRequested", "execution-failure"),
        ):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as directory:
                campaign_dir, history, retry = build(Path(directory).resolve(), preview_failure=rejected)
                with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                    supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)

    def test_failed_recovery_run_is_followed_by_a_new_zero_request_preview(self) -> None:
        """真实补跑失败后，以 N+1 派发新的普通零请求预览，执行集合不得扩大。

        0.156.1 VC-1：序号 4 补跑中 guardian 审阅作业卡在目录信任确认而失败，新 attempt
        按失败封口。后继只能是 N+1 的普通预览（同命令前缀、同 Campaign 目录），execute 为
        父补跑 execute 的非空子集且 execute∪reuse 不变；直接再补跑、扩大执行集合、父补跑以
        截止清理失败一律拒绝。
        """

        def build(root: Path, *, run_failure: tuple[str, str, str]) -> tuple[
            list[tuple[dict[str, object], dict[str, object], Path]], dict[str, object]
        ]:
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_id = "campaign-official-run-retry"
            checkpoint = {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            }
            prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]

            def manifest(sequence: int, actions: list, execute: list, reuse: list) -> dict[str, object]:
                return {
                    "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    "campaign_id": campaign_id,
                    "campaign_plan_sha256": "1" * 64,
                    "batch_id": f"vc-1-{sequence:04d}",
                    "batch_sequence": sequence,
                    "batch_sha256": str(sequence + 4) * 64,
                    "phase": "VC-1",
                    "predecessor_checkpoint": checkpoint,
                    "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                    "no_op": False,
                    "actions": actions,
                    "execute_items": execute,
                    "reuse_items": reuse,
                }

            def run(name: str, owner_nonce: str, state_name: str, reason: str | None) -> tuple[dict[str, object], Path]:
                run_dir = root / name
                run_dir.mkdir(mode=0o700)
                state: dict[str, object] = {
                    "state": state_name,
                    "campaign_id": campaign_id,
                    "phase": "VC-1",
                    "owner_pid": os.getpid(),
                    "owner_nonce": owner_nonce,
                    "terminal_at_utc": "2026-09-25T09:13:09.000Z",
                }
                self._write_json(run_dir / "state.json", state)
                if reason is not None:
                    stop: dict[str, object] = {
                        "schema_version": supervisor.STOP_SCHEMA,
                        "campaign_id": campaign_id,
                        "detected_at_epoch": 1005.0,
                        "detected_at_utc": "2026-09-25T09:13:09.251Z",
                        "event_type": "failed",
                        "owner_nonce": owner_nonce,
                        "owner_pid": os.getpid(),
                        "phase": "VC-1",
                        "reason": reason,
                    }
                    stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
                    self._write_json(run_dir / "stop-receipt.json", stop)
                return state, run_dir

            capture_manifest = manifest(
                1,
                [
                    {
                        "action_id": "capture-official",
                        "operation": "VC-1:capture-official",
                        "timeout_seconds": 3600.0,
                        "command": [
                            *prefix,
                            "capture-official",
                            "run",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["passed-job", "pending-job"],
                    }
                ],
                ["passed-job", "pending-job"],
                [],
            )
            capture_state, capture_dir = run("run-capture", "8" * 64, "failed", "action-failed:capture-official")
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(capture_dir, "capture-official", create_directory=True),
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="capture-official",
                owner_pid=os.getpid(),
                owner_nonce="8" * 64,
                failure_kind="child-returncode",
                error_type="ChildProcessError",
                message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
            )
            preview_command = [
                *prefix,
                "resume",
                "--campaign-dir",
                str(campaign_dir),
                "--rerun-failed",
                "--preview-recovery",
            ]
            preview_manifest = manifest(
                2,
                [
                    {
                        "action_id": "preview-official-recovery",
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 3600.0,
                        "command": preview_command,
                        "item_ids": ["pending-job"],
                    }
                ],
                ["pending-job"],
                ["passed-job"],
            )
            preview_state, preview_dir = run("run-preview", "9" * 64, "stopped", None)
            run_manifest = manifest(
                3,
                [
                    {
                        "action_id": "run-official-recovery",
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 5400.0,
                        "command": [
                            *prefix,
                            "resume",
                            "--campaign-dir",
                            str(campaign_dir),
                            "--rerun-failed",
                            "--recovery-preview",
                            str(campaign_dir / "control" / "recovery-preview-01.json"),
                            "--acknowledge-live-requests",
                        ],
                        "item_ids": ["pending-job"],
                    }
                ],
                ["pending-job"],
                ["passed-job"],
            )
            run_state, run_dir = run("run-recovery", "a" * 64, "failed", "action-failed:run-official-recovery")
            failure_kind, error_type, failure_class = run_failure
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(run_dir, "run-official-recovery", create_directory=True),
                campaign_id=campaign_id,
                phase="VC-1",
                action_id="run-official-recovery",
                owner_pid=os.getpid(),
                owner_nonce="a" * 64,
                failure_kind=failure_kind,
                failure_class=failure_class,
                error_type=error_type,
                message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
            )
            history = [
                (capture_state, capture_manifest, capture_dir),
                (preview_state, preview_manifest, preview_dir),
                (run_state, run_manifest, run_dir),
            ]
            successor = manifest(
                4,
                [
                    {
                        "action_id": "preview-official-recovery",
                        "operation": "VC-1:official-recovery",
                        "timeout_seconds": 3600.0,
                        "command": list(preview_command),
                        "item_ids": ["pending-job"],
                    }
                ],
                ["pending-job"],
                ["passed-job"],
            )
            # B4-1 第 31 项：恢复链协议要求失败父 run 已对账——补一份形态合法的 supervisor-run 收据与总账绑定。
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, run_dir, run_state, run_manifest, failure_class=failure_class)
            return campaign_dir, history, successor

        handled = ("child-returncode", "ChildProcessError", "execution-failure")
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, history, successor = build(Path(directory).resolve(), run_failure=handled)
            ordered = supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2, 3])

            # 补跑失败后直接再补跑：不是普通零请求预览，拒绝。
            live = copy.deepcopy(successor)
            live["actions"][0]["action_id"] = "run-official-recovery"
            live["actions"][0]["command"] = history[2][1]["actions"][0]["command"]
            with self.assertRaisesRegex(SupervisorError, "普通零请求预览"):
                supervisor._validate_batched_campaign_history(live, history, campaign_dir=campaign_dir)

            # 扩大执行集合（把已复用的 Job 放回执行）：拒绝。
            widened = copy.deepcopy(successor)
            widened["execute_items"] = ["passed-job", "pending-job"]
            widened["reuse_items"] = []
            widened["actions"][0]["item_ids"] = ["passed-job", "pending-job"]
            with self.assertRaisesRegex(SupervisorError, "不得扩大父补跑的执行集合"):
                supervisor._validate_batched_campaign_history(widened, history, campaign_dir=campaign_dir)

        # 父补跑以截止清理失败：不属于处理型失败，拒绝。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, history, successor = build(
                Path(directory).resolve(),
                run_failure=("handled-error", "CampaignCleanupRequested", "deadline-expired"),
            )
            with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
        # 修好接着跑第 30 项（194249z 批次 16 真实补跑以 ValueError 失败、修好后批次 17 零请求预览被拒的实测）：
        # 执行失败不再按错误类型白名单；中断种类、非 execution-failure 类别、清理／中断异常仍拒绝。
        for accepted in (
            ("handled-error", "ValueError", "execution-failure"),
            ("unexpected-error", "RuntimeError", "execution-failure"),
            ("child-returncode", "ChildProcessError", "execution-failure"),
        ):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                campaign_dir, history, successor = build(Path(directory).resolve(), run_failure=accepted)
                ordered = supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
                self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2, 3])
        for rejected in (
            ("interrupted", "KeyboardInterrupt", "execution-failure"),
            ("handled-error", "ConfigurationError", "deadline-expired"),
            ("handled-error", "CampaignCleanupRequested", "execution-failure"),
        ):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as directory:
                campaign_dir, history, successor = build(Path(directory).resolve(), run_failure=rejected)
                with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                    supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)

    def _environment_redispatch_fixture(
        self, root: Path
    ) -> tuple[Path, dict[str, object], dict[str, object], Path, dict[str, object], Path]:
        """reservation 前环境前提失败、已对账许可 redispatch-same-batch 的父批次与逐字相同的 N+1 后继。

        返回 (campaign_dir, prior_state, prior_manifest, prior_dir, successor, campaign_receipt)；
        修好接着跑第 9 项（B3-11）的身份重派用例共用这套夹具。
        """

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
        return campaign_dir, prior_state, prior_manifest, prior_dir, successor, campaign_receipt

    def test_environment_recovery_only_redispatches_exact_prior_batch(self) -> None:
        """reservation 前环境失败须有对账许可；后继按批次身份重派——修好接着跑第 9 项（B3-11）：执行细节可变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, prior_state, prior_manifest, prior_dir, successor, campaign_receipt = (
                self._environment_redispatch_fixture(root)
            )
            ordered = supervisor._validate_batched_campaign_history(
                successor,
                [(prior_state, prior_manifest, prior_dir)],
                campaign_dir=campaign_dir,
            )
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

            # 只改执行细节（timeout、命令 argv）：同阶段、同动作、同候选、同输入，身份不变，放行。
            details = copy.deepcopy(successor)
            details["actions"][0]["timeout_seconds"] = 61.0
            details["actions"][0]["command"] = [*details["actions"][0]["command"], "--heartbeat-seconds", "5"]
            ordered = supervisor._validate_batched_campaign_history(
                details,
                [(prior_state, prior_manifest, prior_dir)],
                campaign_dir=campaign_dir,
            )
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])

            campaign_receipt.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(SupervisorError, "对账收据绑定漂移"):
                supervisor._validate_batched_campaign_history(
                    successor,
                    [(prior_state, prior_manifest, prior_dir)],
                    campaign_dir=campaign_dir,
                )

    def test_environment_redispatch_rejects_identity_drift_but_allows_execution_details(self) -> None:
        """修好接着跑第 9 项（B3-11）：改 item_ids／operation／action_id／阶段／候选／分区即身份漂移，拒绝并点名字段；
        跨评估基线不是重派，拒绝并指向 evaluation-recover。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, prior_state, prior_manifest, prior_dir, successor, _campaign_receipt = (
                self._environment_redispatch_fixture(root)
            )

            def variant(**changes: object) -> dict[str, object]:
                manifest = copy.deepcopy(successor)
                action_changes = changes.pop("action", None)
                manifest.update(changes)
                if isinstance(action_changes, dict):
                    manifest["actions"][0].update(action_changes)
                return manifest

            cases = [
                ("actions", variant(action={"item_ids": ["other-item"]})),
                ("actions", variant(action={"operation": "VC-1:candidate-readiness-renamed"})),
                ("actions", variant(action={"action_id": "candidate-readiness-renamed"})),
                ("phase", variant(phase="VC-2")),
                ("candidate_id", variant(candidate_id="cand-other")),
                ("candidate_revision", variant(candidate_revision=2)),
                ("execute_items", variant(execute_items=["candidate-readiness", "other-item"])),
                ("reuse_items", variant(reuse_items=["reused-item"])),
                (
                    "predecessor_checkpoint",
                    variant(predecessor_checkpoint={**successor["predecessor_checkpoint"], "sha256": "9" * 64}),
                ),
            ]
            for field, manifest in cases:
                with self.subTest(field=field):
                    with self.assertRaisesRegex(SupervisorError, f"只允许原批次内容重派，漂移字段：{field}$"):
                        supervisor._validate_batched_campaign_history(
                            manifest, [(prior_state, prior_manifest, prior_dir)], campaign_dir=campaign_dir
                        )
            # 跨基线（后继带评估基线与 COMMIT，前序为 b0）：共享重派许可拒绝并指向 evaluation-recover。
            crossing = variant(evaluation_baseline=1, baseline_commit_sha256="a" * 64)
            with self.assertRaisesRegex(
                SupervisorError, "漂移字段：evaluation_baseline、baseline_commit_sha256；跨评估基线.*evaluation-recover"
            ):
                supervisor._validate_reconciled_redispatch_binding(
                    prior_state, prior_manifest, prior_dir, crossing, campaign_dir=campaign_dir,
                    effective_class="environment-prerequisite", label="环境前提失败",
                )

    def test_redispatch_identity_ignores_execution_details_and_orders_actions(self) -> None:
        """修好接着跑第 9 项（B3-11）：重派身份 = 12 个批次级／候选级字段 + 按 action_id 排序的 (action_id, operation, item_ids)。"""

        base: dict[str, object] = {
            "campaign_id": "c",
            "campaign_plan_sha256": "1" * 64,
            "phase": "VC-5",
            "predecessor_checkpoint": {"path": "p", "sha256": "2" * 64},
            "original_deadline_at_utc": "2099-01-01T00:00:00Z",
            "execute_items": ["a", "b"],
            "reuse_items": ["r"],
            "no_op": False,
            "candidate_revision": 1,
            "candidate_id": "cand",
            "evaluation_baseline": 1,
            "baseline_commit_sha256": "3" * 64,
            "batch_id": "vc-5-0001",
            "batch_sequence": 1,
            "batch_sha256": "4" * 64,
            "actions": [
                {"action_id": "act-b", "operation": "VC-5:b", "timeout_seconds": 10, "command": ["x", "b"], "item_ids": ["b"]},
                {
                    "action_id": "act-a",
                    "operation": "VC-5:a",
                    "timeout_seconds": 20,
                    "command": ["x", "a"],
                    "item_ids": ["a"],
                    "output_bindings": ["o1"],
                },
            ],
        }
        reordered = copy.deepcopy(base)
        reordered["actions"].reverse()
        reordered["actions"][0].update({"timeout_seconds": 99, "command": ["y", "--flag"], "output_bindings": ["o2"]})
        reordered.update(
            {"batch_id": "vc-5-0002", "batch_sequence": 2, "batch_sha256": "5" * 64, "evaluator_digests": {"checker_sha256": "6" * 64}}
        )
        identity = supervisor._redispatch_identity(base)
        self.assertEqual(identity, supervisor._redispatch_identity(reordered))
        self.assertEqual(set(identity), {*supervisor.REDISPATCH_IDENTITY_FIELDS, "actions"})
        self.assertEqual(len(supervisor.REDISPATCH_IDENTITY_FIELDS), 12)
        self.assertEqual(
            identity["actions"],
            [
                {"action_id": "act-a", "operation": "VC-5:a", "item_ids": ["a"]},
                {"action_id": "act-b", "operation": "VC-5:b", "item_ids": ["b"]},
            ],
        )
        self.assertEqual(supervisor._redispatch_identity_drift(base, reordered), [])
        for change in ({"item_ids": ["a", "c"]}, {"operation": "VC-5:renamed"}, {"action_id": "act-c"}):
            with self.subTest(change=change):
                drifted = copy.deepcopy(base)
                drifted["actions"][1].update(change)
                self.assertEqual(supervisor._redispatch_identity_drift(base, drifted), ["actions"])
        for field in supervisor.REDISPATCH_IDENTITY_FIELDS:
            with self.subTest(field=field):
                drifted = copy.deepcopy(base)
                drifted[field] = "changed"
                self.assertEqual(supervisor._redispatch_identity_drift(base, drifted), [field])
        # 动作列表缺失／含非对象项：身份不等，失败关闭。
        self.assertEqual(supervisor._redispatch_identity_drift(base, {**base, "actions": None}), ["actions"])
        self.assertEqual(
            supervisor._redispatch_identity_drift(base, {**base, "actions": [*base["actions"], "junk"]}), ["actions"]
        )
        # legacy 清单（无候选级键）：候选级键取 None，与自身身份相等。
        legacy = {
            key: value
            for key, value in base.items()
            if key not in {"candidate_revision", "candidate_id", "evaluation_baseline", "baseline_commit_sha256"}
        }
        self.assertIsNone(supervisor._redispatch_identity(legacy)["candidate_id"])
        self.assertEqual(supervisor._redispatch_identity_drift(legacy, copy.deepcopy(legacy)), [])
        # 拒绝信息：只有含评估基线字段时才指向 evaluation-recover。
        self.assertEqual(supervisor._redispatch_drift_message("x", ["actions"]), "x，漂移字段：actions")
        self.assertIn("evaluation-recover", supervisor._redispatch_drift_message("x", ["evaluation_baseline"]))
        self.assertIn("evaluation-recover", supervisor._redispatch_drift_message("x", ["actions", "baseline_commit_sha256"]))

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
            # 修好接着跑第 34 项：固定 sleep 0.35 秒在慢 CI runner 上偶发读到 running；改为有界等待终态。
            state = self._wait_campaign_state(client.run_dir, {"watchdog-aborted"}, timeout=5)
            self.assertEqual(state["state"], "watchdog-aborted")
            self._wait_for_file(client.run_dir / "stop-receipt.json", timeout=5)
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
            # 修好接着跑第 34 项扩展：默认 deadline 5 秒在 ARM64 慢机上会在两次 exec 之间耗尽，
            # 第二次 exec 被“剩余预算不足以容纳动作和终态排空窗口”拒绝（返回 1 而不是 3）。
            # 预算放大到 20 秒只改时间量级，不改“同一操作第二次失败必须停线”的被测条件；
            # 初始 planning 窗口 2 秒同样是第一次 exec 启动的紧约束，一并放到 6 秒。
            payload = self._campaign_start(
                Path(directory),
                initial_timeout=6,
                deadline_seconds=20,
            )
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="nonzero-exit",
                returncode=3,
            )
            # 两层 Python 进程启动在慢机上可能接近 3 秒；以下都只是上限，不放慢正常路径。
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 3)
            activity = self._wait_campaign_activity(
                run_dir,
                classification="planning",
                timeout=5,
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
            repeated.communicate(timeout=10)
            self.assertEqual(repeated.returncode, 3)
            state = self._wait_campaign_state(run_dir, {"failed"}, timeout=5)
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
            # 修好接着跑第 34 项扩展：原 deadline 1.5 秒在 ARM64 慢机上于 campaign-start 之后即到期，
            # 父监督器先结束，错误变成“父监督器已经结束”而不是被测的排空预算拒绝。
            # 被测条件是 requested_timeout + drain_seconds > remaining_seconds，其中
            # drain_seconds = max(watchdog 0.5, heartbeat 0.05 × 2) = 0.5；这里把量级放大为
            # deadline 8 秒、动作超时 10 秒：10 + 0.5 > 8 恒成立，条件与原来完全相同。
            # initial_timeout 同步放到 6 秒，避免初始 planning 派发超时先于 campaign-stop 到期。
            payload = self._campaign_start(
                Path(directory),
                initial_timeout=6,
                deadline_seconds=8,
            )
            run_dir = Path(str(payload["run_dir"]))
            process = self._campaign_exec_process(
                run_dir,
                operation="insufficient-drain-budget",
                timeout_seconds=10,
            )
            # 慢机上 Python 启动本身就可能超过 1 秒；这里只是上限，不放慢正常路径。
            _stdout, stderr = process.communicate(timeout=10)
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
            # 第 34 项：终态后父监督器还在收尾写入，等它退出再让 tempfile 清理目录。
            self._wait_campaign_monitor_exit(run_dir)

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
            # 第 34 项：与 SIGKILL 用例同理，等父监督器收尾退出再让 tempfile 清理（4 路分片并行实测撞到 Directory not empty）。
            self._wait_campaign_monitor_exit(run_dir)

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
            state = self._wait_campaign_state(run_dir, {"watchdog-aborted"}, timeout=5)
            # 修好接着跑第 34 项扩展：0.5 秒在 ARM64 实测 0.60 秒误判。断言的是“在 watchdog 窗口
            # 量级内即时封存”，即远快于心跳失联判定；改用文件顶部的量级阈值（2 秒），语义不变。
            self.assertLess(time.monotonic() - started, IMMEDIATE_DETECTION_SECONDS)
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
            # 第三批 B3-4（第 5 项①）：失败动作是零请求后处理动作（candidate-seal），即使 post-run-tooling 五条判据
            # 不成立（同 run 内开过 Kilo 窗口），也进入 recovery_required 而不是候选待审；请求账交对账核算。
            closeout = payload["timing_closeout"]
            self.assertEqual(closeout["ledger_status"], "recovery_required")
            self.assertIn("逐字重派", closeout["next_action"])
            summary = timing_ledger.inspect_ledger(ledger_root)
            self.assertEqual((summary["status"], summary["active_phase"]), ("recovery_required", "VC-5"))

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

    def test_budget_state_tolerance_only_aborts_after_sustained_failure(self) -> None:
        """修好接着跑第 25 项：看门狗不持锁重放三层预算，撞上动作进程写总账／账本的中间态会瞬时失败（194249z 批次 14
        在任何请求之前被判"预算状态无效"中止）。首次失败与窗口内持续失败都不中止，持续达到窗口才中止，任一次成功清零。"""

        window = int(supervisor.BUDGET_STATE_TOLERANCE_SECONDS * 1_000_000_000)
        self.assertGreaterEqual(window, 3 * supervisor.DEFAULT_HEARTBEAT_SECONDS * 1_000_000_000)
        tolerance = supervisor._BudgetStateTolerance(window)
        start = 1_000_000_000_000
        self.assertFalse(tolerance.failed(start))
        self.assertFalse(tolerance.failed(start + window - 1))
        tolerance.succeeded()
        # 成功后重新起算：原窗口之后的新一次失败仍是首次。
        self.assertFalse(tolerance.failed(start + window + 5))
        self.assertFalse(tolerance.failed(start + 2 * window + 4))
        self.assertTrue(tolerance.failed(start + 2 * window + 5))
        for invalid in (0, -1):
            with self.subTest(window=invalid), self.assertRaises(supervisor.SupervisorError):
                supervisor._BudgetStateTolerance(invalid)

    def test_watchdog_budget_abort_is_gated_by_tolerance(self) -> None:
        """结构：看门狗主循环里"预算状态无效"的中止只在容忍判定为真的分支内，重放成功时清零。"""

        import ast
        import inspect as inspect_module

        source = inspect_module.getsource(supervisor._monitor_impl)
        tree = ast.parse(textwrap_dedent(source))
        budget_aborts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                gated = (
                    isinstance(test, ast.Call)
                    and isinstance(test.func, ast.Attribute)
                    and test.func.attr == "failed"
                    and isinstance(test.func.value, ast.Name)
                    and test.func.value.id == "budget_tolerance"
                )
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "abort"
                        and inner.args
                        and "budget-state-invalid" in ast.unparse(inner.args[0])
                    ):
                        budget_aborts.append(gated)
        all_aborts = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "abort"
            and node.args and "budget-state-invalid" in ast.unparse(node.args[0])
        ]
        self.assertEqual(len(all_aborts), 1)
        self.assertEqual(budget_aborts, [True])
        self.assertIn("budget_tolerance.succeeded()", source)

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

    # ------------------------------------------------------------------
    # B4-1 后继协议层失败矩阵改造（草表 b4-successor-matrix-draft.md）
    # ------------------------------------------------------------------

    def _b4_failed_run(
        self,
        root: Path,
        name: str,
        *,
        reason: str,
        state_name: str = "failed",
        event_type: str = "failed",
        campaign_id: str = "campaign-b4",
        phase: str = "VC-1",
        owner_nonce: str = "8" * 64,
        diagnostics: tuple[tuple[str, tuple[str, str, str]], ...] = (),
        started_actions: tuple[str, ...] = (),
        message: str = "B4 夹具：错误详情已按脱敏规则省略。",
    ) -> tuple[dict[str, object], Path]:
        """B4-1 夹具：一个已终态的父 run 目录（state／stop receipt／可选的动作诊断与 action-started 事件链）。"""

        run_dir = root / name
        run_dir.mkdir(mode=0o700)
        state: dict[str, object] = {
            "state": state_name,
            "campaign_id": campaign_id,
            "phase": phase,
            "owner_pid": os.getpid(),
            "owner_nonce": owner_nonce,
            "terminal_at_utc": "2026-09-28T00:00:00.000Z",
        }
        self._write_json(run_dir / "state.json", state)
        supervisor._stop_receipt(
            run_dir,
            event_type=event_type,
            reason=reason,
            detected_at_epoch=1005.0,
            owner_pid=os.getpid(),
            owner_nonce=owner_nonce,
            campaign_id=campaign_id,
            phase=phase,
        )
        for action_id, (failure_kind, error_type, failure_class) in diagnostics:
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(run_dir, action_id, create_directory=True),
                campaign_id=campaign_id,
                phase=phase,
                action_id=action_id,
                owner_pid=os.getpid(),
                owner_nonce=owner_nonce,
                failure_kind=failure_kind,
                failure_class=failure_class,
                error_type=error_type,
                message=message,
            )
        for action_id in started_actions:
            supervisor._append_event(
                run_dir,
                event_type="action-started",
                operation=f"{phase}:{action_id}",
                owner_pid=os.getpid(),
                owner_nonce=owner_nonce,
                campaign_id=campaign_id,
                phase=phase,
                job_id=action_id,
                status="running",
                started_at_epoch=1000.0,
            )
        return state, run_dir

    def test_b4_1_failed_parent_facts_classifies_terminal_kinds_and_locates_action(self) -> None:
        """B4-1 改法 1：失败父 run 的共享事实按"终态种类＋失败动作"给出，不按异常名白名单。

        action-failed:<id> 直接取 stop reason；SupervisorTimeout／KeyboardInterrupt／其它异常类名的终态依次从唯一的
        动作诊断、事件链最后一条 action-started 定位动作；定位不到（无诊断且无事件、诊断不唯一、诊断指向清单外的
        动作）时 action_id 为 None——保守，不硬接；不是失败终态或 stop reason 不是异常类名／已知形态时返回 None。
        """

        handled = ("handled-error", "CampaignCleanupRequested", "deadline-expired")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest = {"actions": [{"action_id": "capture-official"}, {"action_id": "seal-official-preview"}]}

            state, run_dir = self._b4_failed_run(root, "run-action-failed", reason="action-failed:capture-official")
            facts = supervisor._failed_parent_facts(state, run_dir)
            self.assertEqual(
                (facts["terminal_kind"], facts["action_id"], facts["action_id_source"], facts["state"], facts["reason"]),
                ("action-failed", "capture-official", "stop-reason", "failed", "action-failed:capture-official"),
            )
            self.assertEqual(facts["stop"]["event_type"], "failed")

            # 动作级超时：stop reason 是异常类名 SupervisorTimeout，动作由唯一诊断定位。
            state, run_dir = self._b4_failed_run(
                root, "run-timeout", reason="SupervisorTimeout", diagnostics=(("capture-official", handled),)
            )
            facts = supervisor._failed_parent_facts(state, run_dir, prior_manifest=manifest)
            self.assertEqual(
                (facts["terminal_kind"], facts["action_id"], facts["action_id_source"]),
                ("action-timeout", "capture-official", "diagnostic"),
            )
            # 中断：无诊断时由事件链最后一条 action-started 定位。
            state, run_dir = self._b4_failed_run(
                root, "run-interrupt", reason="KeyboardInterrupt", started_actions=("capture-official", "seal-official-preview")
            )
            facts = supervisor._failed_parent_facts(state, run_dir, prior_manifest=manifest)
            self.assertEqual(
                (facts["terminal_kind"], facts["action_id"], facts["action_id_source"]),
                ("interrupted", "seal-official-preview", "events"),
            )
            state, run_dir = self._b4_failed_run(root, "run-exit", reason="SystemExit", started_actions=("capture-official",))
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["terminal_kind"], "interrupted")
            # 父进程其它异常：类名不在任何白名单里也能归类。
            state, run_dir = self._b4_failed_run(root, "run-other", reason="RuntimeError", diagnostics=(("capture-official", handled),))
            facts = supervisor._failed_parent_facts(state, run_dir, prior_manifest=manifest)
            self.assertEqual((facts["terminal_kind"], facts["action_id"]), ("other-exception", "capture-official"))
            # 定位不到动作：无诊断且无事件链；诊断不唯一；诊断指向清单外的动作。
            state, run_dir = self._b4_failed_run(root, "run-blind", reason="SupervisorTimeout")
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["action_id"], None)
            state, run_dir = self._b4_failed_run(
                root, "run-two", reason="SupervisorTimeout",
                diagnostics=(("capture-official", handled), ("seal-official-preview", handled)),
            )
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["action_id"], None)
            state, run_dir = self._b4_failed_run(root, "run-ghost", reason="SupervisorTimeout", diagnostics=(("ghost", handled),))
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir, prior_manifest=manifest)["action_id"], None)
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["action_id"], "ghost")
            # 看门狗中止与两类父失败：种类各自独立，不带动作（看门狗可由事件链定位）。
            state, run_dir = self._b4_failed_run(
                root, "run-watchdog", state_name="watchdog-aborted", event_type="watchdog-aborted",
                reason="owner-process-not-alive", started_actions=("capture-official",),
            )
            facts = supervisor._failed_parent_facts(state, run_dir)
            self.assertEqual((facts["terminal_kind"], facts["action_id"]), ("watchdog", "capture-official"))
            state, run_dir = self._b4_failed_run(root, "run-parent-start", reason="parent-start-failed")
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["terminal_kind"], "parent-start-failed")
            state, run_dir = self._b4_failed_run(root, "run-parent-finalize", reason="parent-finalize-lost")
            self.assertEqual(supervisor._failed_parent_facts(state, run_dir)["terminal_kind"], "parent-finalize-lost")
            # 不可归类：非失败终态；stop receipt 缺失；failed 终态却带 stopped 的 reason；state 与 stop 事件类型不符。
            state, run_dir = self._b4_failed_run(root, "run-stopped", state_name="stopped", event_type="stopped", reason="queue-complete")
            self.assertIsNone(supervisor._failed_parent_facts(state, run_dir))
            state, run_dir = self._b4_failed_run(root, "run-odd", reason="queue-complete")
            self.assertIsNone(supervisor._failed_parent_facts(state, run_dir))
            state, run_dir = self._b4_failed_run(root, "run-mismatch", state_name="watchdog-aborted", reason="owner-process-not-alive")
            self.assertIsNone(supervisor._failed_parent_facts(state, run_dir))
            missing = root / "run-missing"
            missing.mkdir(mode=0o700)
            self.assertIsNone(supervisor._failed_parent_facts({"state": "failed"}, missing))

    def test_b4_1_environment_redispatch_accepts_timeout_parent_with_located_action(self) -> None:
        """B4-1 改法 1（草表 D-04）：环境前提失败的父 run 以动作级超时终止（stop reason SupervisorTimeout）——
        诊断已定位到唯一动作、对账许可齐全时，逐字重派协议同样承接；定位不到动作（诊断不唯一）或 stop reason
        不是异常类名的失败终态仍不承接。"""

        def rewrite_stop(prior_dir: Path, prior_state: dict[str, object], reason: str) -> None:
            stop_path = prior_dir / "stop-receipt.json"
            stop_path.chmod(0o600)
            stop_path.unlink()
            supervisor._stop_receipt(
                prior_dir, event_type="failed", reason=reason, detected_at_epoch=1005.0,
                owner_pid=int(prior_state["owner_pid"]), owner_nonce=str(prior_state["owner_nonce"]),
                campaign_id=str(prior_state["campaign_id"]), phase="VC-1",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, prior_state, prior_manifest, prior_dir, successor, _receipt = (
                self._environment_redispatch_fixture(root)
            )
            history = [(prior_state, prior_manifest, prior_dir)]
            rewrite_stop(prior_dir, prior_state, "SupervisorTimeout")
            ordered = supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
            # 父进程其它异常同样按诊断定位动作后承接。
            rewrite_stop(prior_dir, prior_state, "RuntimeError")
            ordered = supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
            # 定位不到动作（第二份诊断让诊断不唯一）：不硬接，兜底文案说明无法定位失败动作。
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(prior_dir, "other-action", create_directory=True),
                campaign_id=str(prior_state["campaign_id"]), phase="VC-1", action_id="other-action",
                owner_pid=int(prior_state["owner_pid"]), owner_nonce=str(prior_state["owner_nonce"]),
                failure_kind="handled-error", failure_class="environment-prerequisite",
                error_type="ConfigurationError", message="第二份诊断。",
            )
            with self.assertRaisesRegex(SupervisorError, "无法定位失败动作"):
                supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, prior_state, prior_manifest, prior_dir, successor, _receipt = (
                self._environment_redispatch_fixture(root)
            )
            history = [(prior_state, prior_manifest, prior_dir)]
            # failed 终态却带 stopped 的 reason：不是异常类名，不可归类，任何协议都不承接。
            rewrite_stop(prior_dir, prior_state, "queue-complete")
            with self.assertRaisesRegex(SupervisorError, "没有被任何后继协议承接|唯一直接 v3 恢复后继"):
                supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)


    def test_b4_3_reservations_in_run_window_follow_reconciler_criteria(self) -> None:
        """B4-1 改法 3：0-W 分流用的"run 期间预约"只读判据与 reconciler 父 run 对账同口径——官方与候选主 attempt、
        恢复段都按 reservation.started_at_utc 不早于父 run 开始时刻计入；采集已完整收口（awaiting_receipts 且无失败
        Job）的 attempt／段不是中断，不计入；早于父 run 的预约不计入。"""

        def write(path: Path, payload: dict[str, object]) -> None:
            self._write_json(path, payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir = root / "campaign"
            official = campaign_dir / "official" / "attempts"
            candidate = campaign_dir / "candidates" / "cand-1" / "attempts"
            late = "2026-09-28T00:10:00.000Z"
            early = "2026-09-27T23:50:00.000Z"
            started = 1_790_553_600.0  # 2026-09-28T00:00:00Z
            write(official / "att-a" / "reservation.json", {"started_at_utc": late})
            write(official / "att-b" / "reservation.json", {"started_at_utc": early})
            write(candidate / "att-c" / "reservation.json", {"started_at_utc": late})
            write(candidate / "att-c" / "attempt.json", {"status": "awaiting_receipts", "results": [{"id": "j", "status": "complete"}]})
            write(candidate / "att-d" / "reservation.json", {"started_at_utc": late})
            write(candidate / "att-d" / "attempt.json", {"status": "awaiting_receipts", "results": [{"id": "j", "status": "failed"}]})
            write(candidate / "att-e" / "reservation.json", {"started_at_utc": early})
            write(candidate / "att-e" / "recovery" / "ar1" / "recovery-reservation.json", {"started_at_utc": late})
            write(candidate / "att-e" / "recovery" / "ar1" / "attempt-recovery.json", {"status": "awaiting_receipts", "results": []})
            write(candidate / "att-e" / "recovery" / "ar2" / "recovery-reservation.json", {"started_at_utc": late})
            found = supervisor._reservations_in_run_window(campaign_dir, started)
            self.assertEqual(
                [(candidate_id, subject) for candidate_id, subject, _root in found],
                [(None, "att-a"), ("cand-1", "att-d"), ("cand-1", "att-e:ar2")],
            )
            self.assertEqual(found[0][2], official / "att-a")
            self.assertEqual(found[2][2], candidate / "att-e" / "recovery" / "ar2")
            # 没有任何 attempt 目录的 Campaign：空。
            self.assertEqual(supervisor._reservations_in_run_window(root / "empty", started), [])

    def _b4_bind_supervisor_run_reconciliation(
        self,
        campaign_dir: Path,
        run_dir: Path,
        prior_state: dict[str, object],
        prior_manifest: dict[str, object],
        *,
        failure_class: str = "execution-failure",
    ) -> Path:
        """B4-1 第 31 项夹具：为失败父 run 写一份形态合法的 supervisor-run 对账收据，并在祖先项目总账登记
        reconcile-supervisor-run:<run> operation。

        字段集合是 verify_supervisor_run_reconciliation_binding 核对的那部分（与 reconciler.reconcile_supervisor_run
        的产出同形），不冒充完整收据；总账事件 payload 与 reconciler 的 reconciliation_committed 同形。
        """

        campaign_dir = Path(campaign_dir).resolve()
        campaign_path = campaign_dir / "campaign.json"
        if not campaign_path.is_file():
            self._write_json(campaign_path, {"campaign_id": str(prior_manifest["campaign_id"])})
        ledger_root = supervisor.project_ledger.find_project_ledger(campaign_dir)
        if ledger_root is None:
            ledger_root = project_ledger_fixture.install_fixture_ledger(campaign_dir.parent)
        receipt = {
            "schema_version": supervisor.SUPERVISOR_RUN_RECONCILIATION_SCHEMA,
            "campaign_id": str(prior_manifest["campaign_id"]),
            "campaign_manifest_sha256": supervisor._sha256(campaign_path.read_bytes()),
            "run": {
                "run_dir": str(run_dir.resolve()),
                "run_id": run_dir.name,
                "state": prior_state["state"],
                "phase": prior_manifest["phase"],
                "batch_id": prior_manifest["batch_id"],
                "batch_sequence": prior_manifest["batch_sequence"],
                "batch_sha256": prior_manifest["batch_sha256"],
                "execute_items": list(prior_manifest["execute_items"]),
                "reuse_items": list(prior_manifest["reuse_items"]),
                "failure_class": failure_class,
            },
            "failure_class": failure_class,
            "root_cause": {"root_cause_id": "b4-fixture-01"},
            "reservation_exists": False,
            "live_request_count": 0,
            "scanned_bytes": 0,
        }
        receipt_path = (
            campaign_dir / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json"
        )
        self._write_json(receipt_path, receipt)
        supervisor.project_ledger.append_project_event(
            ledger_root,
            operation_id=f"reconcile-supervisor-run:{run_dir.name}",
            event_type="reconciliation_committed",
            payload={
                "campaign_id": str(prior_manifest["campaign_id"]),
                "subject_kind": "supervisor_run",
                "subject_id": run_dir.name,
                "phase": prior_manifest["phase"],
                "request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0, "estimated_sources": []},
                "reconciliation_receipt_sha256": supervisor._sha256(receipt_path.read_bytes()),
            },
            source_batch_sha256=None,
        )
        return receipt_path

    def _b4_vc1_recovery_chain(
        self,
        root: Path,
        *,
        preview_stop: str,
        preview_diagnostic: tuple[str, str, str] | None,
        started_actions: tuple[str, ...] = (),
    ) -> tuple[Path, list[tuple[dict[str, object], dict[str, object], Path]], dict[str, object]]:
        """B4-1 夹具：VC-1 采集失败（序号 1）→ 零请求恢复预览失败（序号 2，终态由参数决定）→ 序号 3 逐字重派预览。"""

        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        campaign_id = "campaign-b4-chain"
        self._write_json(campaign_dir / "campaign.json", {"campaign_id": campaign_id})
        prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]
        checkpoint = {"path": "control/vc/vc-0-checkpoint.json", "sha256": "3" * 64, "phase": "VC-0", "checkpoint_sha256": "4" * 64}

        def manifest(sequence: int, actions: list, execute: list, reuse: list) -> dict[str, object]:
            return {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": "1" * 64,
                "batch_id": f"vc-1-{sequence:04d}",
                "batch_sequence": sequence,
                "batch_sha256": str(sequence + 4) * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                "no_op": False,
                "actions": actions,
                "execute_items": execute,
                "reuse_items": reuse,
            }

        capture_manifest = manifest(
            1,
            [{
                "action_id": "capture-official", "operation": "VC-1:capture-official", "timeout_seconds": 3600.0,
                "command": [*prefix, "capture-official", "run", "--campaign-dir", str(campaign_dir), "--acknowledge-live-requests"],
                "item_ids": ["passed-job", "pending-job"],
            }],
            ["passed-job", "pending-job"],
            [],
        )
        capture_state, capture_dir = self._b4_failed_run(
            root, "run-capture", reason="action-failed:capture-official", campaign_id=campaign_id,
            diagnostics=(("capture-official", ("child-returncode", "ChildProcessError", "execution-failure")),),
            message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
        )
        preview_actions = [{
            "action_id": "preview-official-recovery", "operation": "VC-1:official-recovery", "timeout_seconds": 3600.0,
            "command": [*prefix, "resume", "--campaign-dir", str(campaign_dir), "--rerun-failed", "--preview-recovery"],
            "item_ids": ["pending-job"],
        }]
        preview_manifest = manifest(2, preview_actions, ["pending-job"], ["passed-job"])
        preview_state, preview_dir = self._b4_failed_run(
            root, "run-preview", reason=preview_stop, campaign_id=campaign_id, owner_nonce="9" * 64,
            diagnostics=(("preview-official-recovery", preview_diagnostic),) if preview_diagnostic is not None else (),
            started_actions=started_actions,
        )
        history = [(capture_state, capture_manifest, capture_dir), (preview_state, preview_manifest, preview_dir)]
        retry = manifest(3, copy.deepcopy(preview_actions), ["pending-job"], ["passed-job"])
        return campaign_dir, history, retry

    def test_b4_4_recovery_chain_parent_requires_reconciliation_receipt(self) -> None:
        """B4-1 改法 4（第 31 项，草表 D-18）：恢复链协议（预览重派／补跑失败后的预览等）的失败父 run 必须已对账——
        无预约要求 supervisor-run 对账收据与总账 reconcile-supervisor-run:<run> 绑定；缺收据、绑定漂移、缺 Campaign 目录
        都失败关闭并指向对账入口。同时（改法 1 扩展到恢复链）动作级超时／中断的父预览能定位到该动作且已对账时同样承接；
        诊断为永久失败类、定位到的动作不是预览动作仍拒绝。"""

        handled = ("handled-error", "ValueError", "execution-failure")
        cleanup = ("handled-error", "CampaignCleanupRequested", "deadline-expired")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, history, retry = self._b4_vc1_recovery_chain(
                root, preview_stop="action-failed:preview-official-recovery", preview_diagnostic=handled
            )
            preview_state, preview_manifest, preview_dir = history[1]
            # 缺 Campaign 目录：无法核验对账收据，失败关闭。
            with self.assertRaisesRegex(SupervisorError, "对账收据需要 Campaign 目录"):
                supervisor._validate_batched_campaign_history(retry, history)
            # 未对账：明确指向 reconcile-supervisor-run。
            with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            receipt_path = self._b4_bind_supervisor_run_reconciliation(campaign_dir, preview_dir, preview_state, preview_manifest)
            ordered = supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])
            # 收据与总账绑定漂移（收据被改写）：拒绝。
            original = receipt_path.read_bytes()
            receipt = json.loads(original.decode("utf-8"))
            receipt_path.write_text(json.dumps(dict(receipt, failure_class="post-run-tooling"), ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(SupervisorError, "对账收据与项目总账绑定不一致|对账收据 schema 或身份不闭合"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            receipt_path.write_bytes(original)
            self.assertEqual(len(supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)), 2)
        # 动作级超时的父预览：诊断由清理信号写出（deadline-expired），已对账即承接。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, history, retry = self._b4_vc1_recovery_chain(root, preview_stop="SupervisorTimeout", preview_diagnostic=cleanup)
            preview_state, preview_manifest, preview_dir = history[1]
            with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            self._b4_bind_supervisor_run_reconciliation(
                campaign_dir, preview_dir, preview_state, preview_manifest, failure_class="deadline-expired"
            )
            ordered = supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])
        # 中断的父预览：没有诊断，动作由事件链最后一条 action-started 定位，已对账即承接。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, history, retry = self._b4_vc1_recovery_chain(
                root, preview_stop="KeyboardInterrupt", preview_diagnostic=None, started_actions=("preview-official-recovery",)
            )
            preview_state, preview_manifest, preview_dir = history[1]
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, preview_dir, preview_state, preview_manifest)
            ordered = supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])
        # 仍拒绝：超时前序的诊断是永久失败类；定位到的动作不是预览动作（诊断指向别的动作）。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, history, retry = self._b4_vc1_recovery_chain(
                root, preview_stop="SupervisorTimeout", preview_diagnostic=("handled-error", "PolicyDrift", "identity-drift")
            )
            preview_state, preview_manifest, preview_dir = history[1]
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, preview_dir, preview_state, preview_manifest, failure_class="identity-drift")
            with self.assertRaisesRegex(SupervisorError, "永久失败类"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, history, retry = self._b4_vc1_recovery_chain(root, preview_stop="SupervisorTimeout", preview_diagnostic=None)
            preview_state, preview_manifest, preview_dir = history[1]
            supervisor._write_action_diagnostic(
                supervisor._action_diagnostic_path(preview_dir, "capture-official", create_directory=True),
                campaign_id=str(preview_state["campaign_id"]), phase="VC-1", action_id="capture-official",
                owner_pid=int(preview_state["owner_pid"]), owner_nonce=str(preview_state["owner_nonce"]),
                failure_kind="handled-error", failure_class="deadline-expired", error_type="CampaignCleanupRequested", message="B4。",
            )
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, preview_dir, preview_state, preview_manifest, failure_class="deadline-expired")
            with self.assertRaisesRegex(SupervisorError, "定位到的失败动作不是"):
                supervisor._validate_batched_campaign_history(retry, history, campaign_dir=campaign_dir)

    def test_b4_6_seal_chain_attempt_target_recognizes_canonical_actions(self) -> None:
        """B4-1 改法 6（草表 D-09）：seal 链动作解析按 vc_artifacts 冻结映射识别 canonical 动作（VC-5 canonical-import／
        canonical-advance 与 VC-6 三步），给出它指向的（Campaign 目录, 候选侧, 候选, attempt）；不符合冻结映射（item 与
        子命令不对应）或不是 canonical／seal 链的动作仍返回 None。"""

        campaign = "/campaign"
        prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]
        target = supervisor._seal_chain_attempt_target
        import_action = {
            "action_id": "canonical-1-import", "operation": "VC-5:canonical-import", "timeout_seconds": 60.0,
            "command": [
                *prefix, "canonical-import", "--campaign-dir", campaign, "--candidate-id", "cand-1", "--attempt-id", "att-1",
                "--retire-version", "0.154.0", "--approve-import-sha256", "0" * 64,
            ],
            "item_ids": ["canonical-import"],
        }
        seal_action = {
            "action_id": "canonical-2-seal", "operation": "VC-5:canonical-advance", "timeout_seconds": 60.0,
            "command": [
                *prefix, "canonical-advance", "--campaign-dir", campaign, "--candidate-id", "cand-1", "--attempt-id", "att-1",
                "--canonical-step", "seal",
            ],
            "item_ids": ["canonical-seal"],
        }
        activation_action = {
            "action_id": "canonical-5-production-activation", "operation": "VC-6:canonical-advance", "timeout_seconds": 60.0,
            "command": [
                *prefix, "canonical-advance", "--campaign-dir", campaign, "--candidate-id", "cand-1", "--attempt-id", "att-1",
                "--canonical-step", "production-activation", "--step-receipt", "/campaign/control/canonical/step-4.json",
            ],
            "item_ids": ["production-activation"],
        }
        expected = (campaign, "candidate", "cand-1", "att-1")
        self.assertEqual(target(import_action), expected)
        self.assertEqual(target(seal_action), expected)
        self.assertEqual(target(activation_action), expected)
        # item 与子命令不对应（canonical-compare 挂在 seal 步上）：不符合冻结映射，不是 seal 链动作。
        self.assertIsNone(target(dict(seal_action, item_ids=["canonical-compare"])))
        # 普通评估动作与非 CLI 动作：None。
        self.assertIsNone(target({"command": [*prefix, "compare", "--campaign-dir", campaign], "item_ids": ["compare"]}))
        self.assertIsNone(target({"command": ["/usr/bin/true"], "item_ids": ["x"]}))
        # 既有 seal／断言包动作不受影响。
        self.assertEqual(
            target({"command": [*prefix, "capture-candidate", "seal", "--campaign-dir", campaign, "--candidate-id", "cand-1",
                                "--attempt-id", "att-1"], "item_ids": ["candidate-seal"]}),
            expected,
        )

    def _b4_vc1_capture_prior(
        self,
        root: Path,
        *,
        stop_reason: str,
        diagnostic: tuple[str, str, str] | None,
        message: str = "B4 夹具：错误详情已按脱敏规则省略。",
        event_reason: str | None = None,
    ) -> tuple[Path, dict[str, object], dict[str, object], Path, dict[str, object]]:
        """B4-1 夹具：VC-1 序号 1 的 capture-official 批次按给定终态失败，序号 2 是普通零请求恢复预览。"""

        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        campaign_id = "campaign-b4-capture"
        self._write_json(campaign_dir / "campaign.json", {"campaign_id": campaign_id})
        prefix = ["/usr/bin/python3", "/managed/codex_upgrade.py"]
        checkpoint = {"path": "control/vc/vc-0-checkpoint.json", "sha256": "3" * 64, "phase": "VC-0", "checkpoint_sha256": "4" * 64}

        def manifest(sequence: int, actions: list, execute: list, reuse: list) -> dict[str, object]:
            return {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_plan_sha256": "1" * 64,
                "batch_id": f"vc-1-{sequence:04d}",
                "batch_sequence": sequence,
                "batch_sha256": str(sequence + 4) * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": checkpoint,
                "original_deadline_at_utc": "2099-09-14T12:00:00Z",
                "no_op": False,
                "actions": actions,
                "execute_items": execute,
                "reuse_items": reuse,
            }

        prior_manifest = manifest(
            1,
            [{
                "action_id": "capture-official", "operation": "VC-1:capture-official", "timeout_seconds": 3600.0,
                "command": [*prefix, "capture-official", "run", "--campaign-dir", str(campaign_dir), "--acknowledge-live-requests"],
                "item_ids": ["passed-job", "pending-job"],
            }],
            ["passed-job", "pending-job"],
            [],
        )
        prior_state, prior_dir = self._b4_failed_run(
            root, "run-prior", reason=stop_reason, campaign_id=campaign_id,
            diagnostics=(("capture-official", diagnostic),) if diagnostic is not None else (), message=message,
            started_actions=("capture-official",),
        )
        if event_reason is not None:
            supervisor._append_event(
                prior_dir, event_type="action-failed", operation="VC-1:capture-official", owner_pid=os.getpid(),
                owner_nonce=str(prior_state["owner_nonce"]), campaign_id=campaign_id, phase="VC-1",
                job_id="capture-official", status="failed", reason=event_reason, started_at_epoch=1000.0, ended_at_epoch=1001.0,
            )
        successor = manifest(
            2,
            [{
                "action_id": "preview-official-recovery", "operation": "VC-1:official-recovery", "timeout_seconds": 3600.0,
                "command": [*prefix, "resume", "--campaign-dir", str(campaign_dir), "--rerun-failed", "--preview-recovery"],
                "item_ids": ["pending-job"],
            }],
            ["pending-job"],
            ["passed-job"],
        )
        return campaign_dir, prior_state, prior_manifest, prior_dir, successor

    def test_b4_7_official_preview_uses_recovery_failure_criteria_and_reconciled_kill(self) -> None:
        """B4-1 改法 7（草表 D-01／D-02／D-16）：VC-1 采集失败→零请求预览协议不再按错误类型／文案白名单——非超时分支
        按恢复链同一判据（处理型失败：失败种类＋类别，不比 message），超时分支只比 error_type／failure_class／failure_kind
        与事件链的清理完成事实；宽限耗尽强杀（cleanup-window-expired）或诊断不是截止清理时，attempt 可能未封口，改为
        要求父 run 已对账（有预约认 attempt 收据、无预约认 supervisor-run 收据）。中断种类的诊断与永久失败类仍拒绝。"""

        def check(successor, history, campaign_dir=None):
            return supervisor._validate_batched_campaign_history(successor, history, campaign_dir=campaign_dir)

        # ① 非超时分支：子进程自写的 handled-error 诊断（如 EnvironmentContinuityDrift），message 任意。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, state, manifest, run_dir, successor = self._b4_vc1_capture_prior(
                Path(directory).resolve(), stop_reason="action-failed:capture-official",
                diagnostic=("handled-error", "EnvironmentContinuityDrift", "execution-failure"),
            )
            ordered = check(successor, [(state, manifest, run_dir)])
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
        # ② 超时分支：清理宽限内自行封口，message 不逐字。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, state, manifest, run_dir, successor = self._b4_vc1_capture_prior(
                Path(directory).resolve(), stop_reason="SupervisorTimeout",
                diagnostic=("handled-error", "CampaignCleanupRequested", "deadline-expired"),
                message="清理信号：自定义文案。", event_reason="cleanup-requested-timeout",
            )
            ordered = check(successor, [(state, manifest, run_dir)])
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
        # ③ 宽限耗尽强杀：父兜底诊断（unexpected-error），attempt 可能未封口——要求父 run 已对账。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, state, manifest, run_dir, successor = self._b4_vc1_capture_prior(
                Path(directory).resolve(), stop_reason="SupervisorTimeout",
                diagnostic=("unexpected-error", "SupervisorTimeout", "execution-failure"), event_reason="cleanup-window-expired",
            )
            history = [(state, manifest, run_dir)]
            # 协议 8 从后继命令里就知道 Campaign 目录：不传目录参数也按同一目录核对对账收据。
            with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"):
                check(successor, history)
            with self.assertRaisesRegex(SupervisorError, "尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"):
                check(successor, history, campaign_dir)
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, run_dir, state, manifest)
            ordered = check(successor, history, campaign_dir)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
        # ④ 仍拒绝：中断种类的诊断；超时前序的诊断是永久失败类。
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, state, manifest, run_dir, successor = self._b4_vc1_capture_prior(
                Path(directory).resolve(), stop_reason="action-failed:capture-official",
                diagnostic=("interrupted", "KeyboardInterrupt", "execution-failure"),
            )
            with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                check(successor, [(state, manifest, run_dir)], campaign_dir)
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, state, manifest, run_dir, successor = self._b4_vc1_capture_prior(
                Path(directory).resolve(), stop_reason="SupervisorTimeout",
                diagnostic=("handled-error", "PolicyDrift", "identity-drift"), event_reason="cleanup-requested-timeout",
            )
            self._b4_bind_supervisor_run_reconciliation(campaign_dir, run_dir, state, manifest, failure_class="identity-drift")
            with self.assertRaisesRegex(SupervisorError, "永久失败类"):
                check(successor, [(state, manifest, run_dir)], campaign_dir)

class RootCauseLimitPermanentConditionTests(unittest.TestCase):
    """第三批 B3-9（第 10 项③）：收口的永久条件只看本次根因——总账别的根因达上限不牵连本次失败。"""

    def _hits(self, *, root_cause_id: str | None, failure_class: str = "execution-failure", status: str = "active") -> bool:
        head = {
            "root_causes_at_limit": ["rc1-a"],
            "root_causes_at_limit_by_version": {"0.157.0": ["rc1-a"], "0.154.0": []},
            "root_causes_at_limit_base": [],
        }
        with mock.patch.object(supervisor.project_ledger, "find_project_ledger", return_value=Path("/ledger")), mock.patch.object(
            supervisor.project_ledger, "replay_head", return_value=head
        ), mock.patch.object(supervisor.project_ledger, "project_lock", return_value=contextlib.nullcontext()), mock.patch.object(
            supervisor.project_ledger, "_load_plan", return_value=({}, b"")
        ):
            return supervisor._candidate_failure_hits_permanent_condition(
                Path("/campaign"), {"status": status, "target_version": "0.157.0"},
                failure_class=failure_class, root_cause_id=root_cause_id,
            )

    def test_only_this_failures_root_cause_at_limit_is_permanent(self) -> None:
        self.assertTrue(self._hits(root_cause_id="rc1-a"))
        self.assertFalse(self._hits(root_cause_id="rc1-b"))
        # 没有根因上下文：整版本保守判永久（与旧口径一致）。
        self.assertTrue(self._hits(root_cause_id=None))
        # 永久失败类与账本已停线／完成仍永久，不看根因。
        self.assertTrue(self._hits(root_cause_id="rc1-b", failure_class="evidence-integrity"))
        self.assertTrue(self._hits(root_cause_id="rc1-b", status="stopped"))
        self.assertTrue(self._hits(root_cause_id="rc1-b", status="stop_required"))


if __name__ == "__main__":
    unittest.main()
