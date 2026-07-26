"""抓包生命周期的无网络安全测试。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.official_client_capture.capturelib.lifecycle import (
    CampaignLock,
    CaptureCleanupError,
    CaptureProcess,
    CaptureProcessError,
    _arm_linux_parent_death_signal,
    _popen_safety_options,
    build_capture_process,
    resolve_target_addresses,
)
from tools.official_client_capture.capturelib.model import build_campaign_plan


class LifecycleTest(unittest.TestCase):
    def _direct_case(self):
        plan = build_campaign_plan(
            task="oauth",
            batch_id="lifecycle-test",
            scenarios=("s1",),
            evidence_modes=("direct",),
            sub2api_base_url=None,
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        return plan.cases[0]

    def test_campaign_lock_rejects_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lock"
            with CampaignLock(path):
                with self.assertRaises(CaptureProcessError):
                    with CampaignLock(path):
                        self.fail("第二个锁不应成功")

    def test_campaign_lock_never_follows_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "do-not-touch.txt"
            target.write_text("unchanged\n", encoding="utf-8")
            lock_path = root / "capture.lock"
            lock_path.symlink_to(target)
            with self.assertRaises(CaptureProcessError):
                with CampaignLock(lock_path):
                    self.fail("符号链接锁不应成功")
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")

    def test_existing_output_directory_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "existing"
            output_dir.mkdir()
            process = CaptureProcess(
                case=self._direct_case(),
                output_dir=output_dir,
                command=["/usr/bin/false"],
                environment={},
                mitm_port=18080,
            )
            with self.assertRaises(CaptureProcessError):
                process.start()

    def test_target_resolution_normalizes_and_deduplicates_ips(self) -> None:
        def resolver(host, port, *, family, type):
            self.assertEqual((host, port), ("api.anthropic.com", 443))
            self.assertEqual(family, socket.AF_UNSPEC)
            self.assertEqual(type, socket.SOCK_STREAM)
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::10", 443, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443)),
            ]

        self.assertEqual(
            resolve_target_addresses(
                ("api.anthropic.com",), resolver=resolver
            ),
            ("203.0.113.10", "2001:db8::10"),
        )

    def test_target_resolution_rejects_empty_result(self) -> None:
        def resolver(_host, _port, *, family, type):
            return []

        with self.assertRaises(CaptureProcessError):
            resolve_target_addresses(
                ("api.anthropic.com",), resolver=resolver
            )

    def test_build_process_uses_runtime_resolved_target_bpf(self) -> None:
        def resolver(_host, _port, *, family, type):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", 443)),
            ]

        with tempfile.TemporaryDirectory() as directory:
            process = build_capture_process(
                case=self._direct_case(),
                output_dir=Path(directory) / "direct",
                base_environment={},
                tcpdump_bin="/usr/bin/tcpdump",
                mitmdump_bin="/usr/bin/mitmdump",
                mitm_addon=Path("/capture/addon.py"),
                mitm_confdir=Path("/opt/mitm"),
                mitm_port=18080,
                interface="any",
                address_resolver=resolver,
            )
        self.assertEqual(
            process.command[-1], "tcp port 443 and (host 203.0.113.20)"
        )

    def test_custom_https_port_is_used_for_dns_and_bpf(self) -> None:
        plan = build_campaign_plan(
            task="api",
            batch_id="custom-port",
            scenarios=("s1",),
            evidence_modes=("direct",),
            sub2api_base_url="https://gateway.example.com:8443",
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        resolved_ports: list[int] = []

        def resolver(_host, port, *, family, type):
            resolved_ports.append(port)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.30", port)),
            ]

        with tempfile.TemporaryDirectory() as directory:
            process = build_capture_process(
                case=plan.cases[0],
                output_dir=Path(directory) / "direct",
                base_environment={},
                tcpdump_bin="/usr/bin/tcpdump",
                mitmdump_bin="/usr/bin/mitmdump",
                mitm_addon=Path("/capture/addon.py"),
                mitm_confdir=Path("/opt/mitm"),
                mitm_port=18080,
                interface="any",
                address_resolver=resolver,
            )
        self.assertEqual(resolved_ports, [8443])
        self.assertEqual(
            process.command[-1], "tcp port 8443 and (host 203.0.113.30)"
        )

    def test_linux_popen_option_arms_parent_death_signal(self) -> None:
        with patch(
            "tools.official_client_capture.capturelib.lifecycle.os.getpid",
            return_value=4321,
        ), patch(
            "tools.official_client_capture.capturelib.lifecycle._arm_linux_parent_death_signal"
        ) as arm:
            options = _popen_safety_options("linux")
            options["preexec_fn"]()
        self.assertTrue(options["start_new_session"])
        arm.assert_called_once_with(4321)

    def test_parent_death_signal_handles_fork_race(self) -> None:
        libc = Mock()
        libc.prctl.return_value = 0
        with patch(
            "tools.official_client_capture.capturelib.lifecycle.ctypes.CDLL",
            return_value=libc,
        ), patch(
            "tools.official_client_capture.capturelib.lifecycle.os.getppid",
            return_value=999,
        ), patch(
            "tools.official_client_capture.capturelib.lifecycle.os.getpid",
            return_value=555,
        ), patch(
            "tools.official_client_capture.capturelib.lifecycle.os.kill"
        ) as kill, patch(
            "tools.official_client_capture.capturelib.lifecycle.signal.signal"
        ) as set_signal, patch(
            "tools.official_client_capture.capturelib.lifecycle.signal.pthread_sigmask"
        ) as set_mask:
            _arm_linux_parent_death_signal(4321)
        set_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        kill.assert_called_once_with(555, signal.SIGTERM)
        set_mask.assert_called_once_with(signal.SIG_SETMASK, set())

    @unittest.skipUnless(
        hasattr(signal, "pthread_sigmask"), "当前平台不支持 POSIX signal mask"
    )
    def test_child_clears_parent_blocked_termination_signals(self) -> None:
        blocked = {signal.SIGINT, signal.SIGTERM}
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_popen_safety_options(),
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        assert process is not None
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return_code = process.wait(timeout=3)
            self.assertEqual(return_code, -signal.SIGTERM)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)

    def test_failed_stop_retains_process_reference(self) -> None:
        child = Mock(pid=9876)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("tcpdump", 1)
        process = CaptureProcess(
            case=self._direct_case(),
            output_dir=Path("/capture/not-created"),
            command=["/usr/bin/tcpdump"],
            environment={},
            mitm_port=18080,
        )
        process.process = child
        with patch(
            "tools.official_client_capture.capturelib.lifecycle.os.killpg"
        ), self.assertRaises(CaptureCleanupError):
            process.stop()
        self.assertIs(process.process, child)
        self.assertFalse(process.cleanup_successful)


if __name__ == "__main__":
    unittest.main()
