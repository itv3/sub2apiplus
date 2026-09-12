"""独立监督器的收口、心跳超时和离线审计回归测试。"""

from __future__ import annotations

import contextlib
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
            report = _audit_command(Path(str(payload["run_dir"])))
            self.assertFalse(report["audit_incomplete"])

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
