"""出口暂停的容器终止合同：身份绑定、独立收尾、对账和失败关闭。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.official_client_capture import codex_upgrade_supervisor as supervisor


class CaptureStopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.state = {"campaign_id": "capture-stop-fixture", "owner_nonce": "a" * 64,
                      "started_at_epoch": time.time() - 1, "deadline_at_epoch": time.time() + 30,
                      "started_monotonic_ns": time.monotonic_ns() - 10**9,
                      "egress_guard": {"required": True, "campaign_dir": str(self.root)}}
        self.container = {"id": "b" * 64, "image": "sha256:" + "c" * 64,
                          "started_at": "2026-10-06T00:00:00Z", "running": True,
                          "restarting": False, "pid": 123, "control_id": None, "pid_mode": ""}
        self.current = copy.deepcopy(self.container)
        self.sidecars = []
        self.stopped = []
        self.admission = {"runtime": {"services": {"capture-cli": {"container_id": self.container["id"]}}}}
        self.enterContext(mock.patch.object(supervisor.arm64_environment, "require_runtime_egress",
                                           side_effect=lambda: copy.deepcopy(self.admission)))
        self.docker = self.enterContext(mock.patch.object(supervisor, "_capture_docker", side_effect=self.docker_call))

    def docker_call(self, *args):
        if args[0] == "ps":
            return "\n".join(item["id"] for item in self.sidecars)
        containers = [self.current, *self.sidecars]
        current = next(item for item in containers if item["id"] == args[-1])
        if args[0] == "inspect":
            return json.dumps(current)
        self.assertEqual(args[:3], ("stop", "--time", "1"))
        self.stopped.append(current["id"])
        current.update(running=False, restarting=False, pid=0)
        return current["id"]

    def begin(self, **kwargs):
        return supervisor._capture_begin(self.root, self.state, operation="job:fixture:step-1",
                                          job_id="fixture", environment=kwargs or None)

    def pause(self):
        return supervisor._egress_pause(self.root, self.state, "隔离出口暂停")

    def test_paused_capture_and_bound_sidecar_stop_without_deletion(self):
        scope = self.begin()
        sidecar = {**self.container, "id": "d" * 64, "control_id": self.container["id"],
                   "pid_mode": "container:" + self.container["id"]}
        self.sidecars.append(sidecar)
        self.pause()
        result = supervisor._capture_stop(scope, self.state, reason="runtime-egress-paused")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.stopped, [self.container["id"], sidecar["id"]])
        self.assertTrue(all(not row["running"] and not row["pid"] for row in result["containers"]))
        self.assertEqual(result["accounting_status"], "reconcile-required")
        self.assertEqual(result["egress_pause_sha256"], supervisor._file_digest(self.root / "egress-pause.json"))
        self.assertEqual(result["binding_sha256"], supervisor._read_json(scope / "binding.json")["binding_sha256"])
        self.assertFalse(any(call.args[0] in {"rm", "start", "restart"} for call in self.docker.call_args_list))
        with self.assertRaises(supervisor.RuntimeEgressPaused):
            supervisor._capture_release(scope, self.state)

    def test_monitor_stops_even_after_host_command_registration_is_gone(self):
        scope = self.begin()
        self.pause()
        supervisor._interrupt_egress_commands(self.root, self.state, time.time())
        self.assertEqual(supervisor._read_json(scope / "stop-receipt.json")["status"], "stopped")
        self.assertFalse((self.root / "egress-commands").exists())

    def test_repeated_owner_and_monitor_cleanup_has_one_receipt(self):
        scope = self.begin()
        self.pause()
        first = supervisor._capture_stop(scope, self.state, reason="runtime-egress-paused")
        before = (scope / "stop-receipt.json").read_bytes()
        supervisor._capture_stop_active(self.root, self.state)
        self.assertEqual(supervisor._capture_stop(scope, self.state, reason="owner"), first)
        self.assertEqual((scope / "stop-receipt.json").read_bytes(), before)
        self.assertEqual(self.stopped, [self.container["id"]])

    def test_overlap_and_missing_cleanup_are_not_new_dispatch_authority(self):
        scope = self.begin()
        with self.assertRaisesRegex(supervisor.SupervisorError, "重叠"):
            self.begin()
        supervisor._capture_stop(scope, self.state, reason="command-failed")
        self.current = copy.deepcopy(self.container)
        with self.assertRaisesRegex(supervisor.SupervisorError, "重叠"):
            self.begin()

    def test_successful_release_allows_next_serial_step(self):
        scope = self.begin()
        supervisor._capture_release(scope, self.state)
        next_scope = self.begin()
        self.assertNotEqual(scope, next_scope)
        self.pause()
        supervisor._capture_stop_active(self.root, self.state)
        self.assertFalse((scope / "stop-receipt.json").exists())
        self.assertTrue((next_scope / "stop-receipt.json").exists())

    def test_restart_drift_does_not_stop_new_lifecycle(self):
        scope = self.begin()
        self.current["started_at"] = "2026-10-06T00:01:00Z"
        result = supervisor._capture_stop(scope, self.state, reason="runtime-egress-paused")
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(self.stopped, [])

    def test_wrong_container_and_untrusted_admission_block_before_launch(self):
        with self.assertRaises(supervisor.SupervisorError):
            self.begin(CAPTURE_CONTAINER="sub2apiplus")
        self.admission["runtime"]["services"]["capture-cli"]["container_id"] = "old-short-id"
        with self.assertRaises(supervisor.SupervisorError):
            self.begin()
        self.docker.assert_not_called()

    def test_existing_sidecar_or_nonrunning_container_blocks_dispatch(self):
        self.sidecars.append({**self.container, "id": "d" * 64, "control_id": self.container["id"]})
        with self.assertRaises(supervisor.SupervisorError):
            self.begin()
        self.sidecars.clear()
        self.current.update(running=False, pid=0)
        with self.assertRaises(supervisor.SupervisorError):
            self.begin()

    def test_stop_failure_preserves_accounting_and_blocks_success(self):
        scope = self.begin()
        self.docker.side_effect = supervisor.SupervisorError("隔离故障")
        result = supervisor._capture_stop(scope, self.state, reason="runtime-egress-paused")
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["accounting_status"], "reconcile-required")
        with self.assertRaises(supervisor.RuntimeEgressPaused):
            supervisor._capture_release(scope, self.state)

    def test_tampered_binding_and_receipt_are_not_trusted(self):
        scope = self.begin()
        result = supervisor._capture_stop(scope, self.state, reason="fixture")
        result["status"] = "forged"
        supervisor._write_json(scope / "stop-receipt.json", result, replace=True)
        with self.assertRaises(supervisor.SupervisorError):
            supervisor._capture_stop(scope, self.state, reason="fixture")
        self.assertEqual(self.stopped, [self.container["id"]])

    def test_finished_command_checks_identity_and_orphan_sidecars(self):
        scope = self.begin()
        self.sidecars.append({**self.container, "id": "d" * 64, "control_id": self.container["id"]})
        with self.assertRaises(supervisor.SupervisorError):
            supervisor._capture_release(scope, self.state)
        self.assertFalse((scope / "released.json").exists())

    def test_wrong_sidecar_namespace_fails_closed_and_other_owner_is_untouched(self):
        scope = self.begin()
        other = {**self.container, "id": "d" * 64, "control_id": "e" * 64}
        self.sidecars.append(other)
        result = supervisor._capture_stop(scope, self.state, reason="fixture")
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(other["running"])
        self.assertNotIn(other["id"], self.stopped)

    def test_sidecar_without_namespace_binding_is_rejected_before_dispatch(self):
        self.sidecars.append({**self.container, "id": "d" * 64, "control_id": self.container["id"]})
        with self.assertRaisesRegex(supervisor.SupervisorError, "命名空间"):
            self.begin()

    def test_sidecar_inventory_failure_still_stops_primary_and_keeps_unconfirmed(self):
        scope = self.begin()
        self.sidecars.append({**self.container, "id": "d" * 64, "control_id": self.container["id"]})
        receipt = supervisor._capture_stop(scope, self.state, reason="fixture")
        self.assertEqual(receipt["status"], "unconfirmed")
        self.assertEqual(self.stopped, [self.container["id"]])
        self.assertEqual(receipt["containers"][0]["pid"], 0)

    def test_forged_release_cannot_authorize_next_job(self):
        scope = self.begin()
        supervisor._write_json(scope / "released.json", {"binding_sha256": "e" * 64}, replace=False)
        with self.assertRaisesRegex(supervisor.SupervisorError, "结束凭证"):
            self.begin()

    def test_expired_control_window_cannot_start_another_docker_call(self):
        token = supervisor._CAPTURE_CONTROL_DEADLINE.set(time.monotonic() - 1)
        try:
            with mock.patch.object(subprocess, "run") as run:
                with self.assertRaisesRegex(supervisor.SupervisorError, "预算耗尽"):
                    self._original_control("inspect", "fixture")
                run.assert_not_called()
        finally:
            supervisor._CAPTURE_CONTROL_DEADLINE.reset(token)

    def test_dispatch_pins_container_id_in_shell_environment_and_docker_target(self):
        for command in (["docker", "exec", "capture-cli", "true"],
                        ["docker", "exec", "-i", "--env", "EXAMPLE=fixture", "capture-cli", "sh"]):
            pinned, environment = supervisor._capture_pin_command(list(command), {"CAPTURE_CONTAINER": "capture-cli"}, self.container["id"])
            self.assertNotIn("capture-cli", pinned)
            self.assertEqual(environment["CAPTURE_CONTAINER"], self.container["id"])
        command = ["bash", "/fixture/managed-capture.sh"]
        pinned, environment = supervisor._capture_pin_command(command, {}, self.container["id"])
        self.assertEqual(command, pinned)
        self.assertEqual(environment["CAPTURE_CONTAINER"], self.container["id"])
        with self.assertRaises(supervisor.SupervisorError):
            supervisor._capture_pin_command(["docker", "exec", "sub2apiplus", "true"], {}, self.container["id"])
        with self.assertRaises(supervisor.SupervisorError):
            supervisor._capture_pin_command(["docker", "exec", "--unknown", "capture-cli", "true"], {}, self.container["id"])

    def test_monitor_waits_for_complete_binding_before_cleanup(self):
        completed = threading.Event()
        threads = []
        original = supervisor._write_json
        def monitor():
            supervisor._capture_stop_active(self.root, self.state)
            completed.set()
        def write(path, payload, **kwargs):
            if path.name == "binding.json":
                self.pause()
                thread = threading.Thread(target=monitor)
                threads.append(thread)
                thread.start()
                self.assertFalse(completed.wait(.05))
            return original(path, payload, **kwargs)
        try:
            with mock.patch.object(supervisor, "_write_json", side_effect=write):
                scope = self.begin()
                self.assertTrue(completed.wait(3))
            self.assertEqual(supervisor._read_json(scope / "stop-receipt.json")["status"], "stopped")
        finally:
            for thread in threads:
                thread.join(timeout=3)

    def client(self):
        client = supervisor.SupervisorClient(self.root, campaign_id=self.state["campaign_id"], phase="official",
            deadline_at_epoch=time.time() + 30, heartbeat_seconds=.05, watchdog_timeout_seconds=2,
            ledger_interval_seconds=1, terminate_owner=False)
        client.owner_nonce = self.state["owner_nonce"]
        client._deadline_monotonic_ns = time.monotonic_ns() + 30 * 10**9
        client._require_started = lambda: self.root
        for name in ("_ensure_monitor_alive", "event_start", "event_end", "event_fail", "heartbeat"):
            setattr(client, name, mock.Mock())
        self.enterContext(mock.patch.object(supervisor, "_read_state", return_value=self.state))
        return client

    def test_run_command_pause_stops_container_and_never_records_success(self):
        client = self.client()
        ready = self.root / "ready"
        source = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(20)"
        def check(*args, **kwargs):
            if ready.exists():
                self.pause()
                raise supervisor.RuntimeEgressPaused("隔离出口失效")
        with mock.patch.object(supervisor, "_check_runtime_egress", side_effect=check):
            with self.assertRaises(supervisor.RuntimeEgressPaused):
                client.run_command([sys.executable, "-B", "-c", source], operation="job:fixture:step-1",
                                   job_id="fixture", timeout_seconds=10)
        self.assertEqual(self.stopped, [self.container["id"]])
        client.event_end.assert_not_called()
        client.event_fail.assert_called_once()

    def test_success_and_nonzero_command_follow_different_cleanup_paths(self):
        client = self.client()
        for code in (0, 3):
            result = client.run_command([sys.executable, "-B", "-c", f"raise SystemExit({code})"],
                operation="job:fixture:step-1", job_id="fixture", timeout_seconds=5)
            self.assertEqual(result.returncode, code)
        self.assertEqual(len(self.stopped), 1)
        self.assertEqual(client.event_end.call_count, 1)
        self.assertEqual(client.event_fail.call_count, 1)

    def test_docker_control_is_bounded_and_does_not_echo_errors(self):
        # 直接读取被替身替换前的函数，验证真实有界命令入口。
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 8)):
            original = self._original_control
            with self.assertRaisesRegex(supervisor.SupervisorError, "不可确认"):
                original("inspect", "fixture")

    _original_control = staticmethod(supervisor._capture_docker)


if __name__ == "__main__":
    unittest.main()
