"""抓包生命周期的无网络安全测试。"""

from __future__ import annotations

import contextlib
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
    ensure_mitm_port_available,
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
        self.assertEqual(
            process.metadata["invocation"]["argv_redacted"], process.command
        )
        self.assertEqual(len(process.metadata["invocation"]["argv_sha256"]), 64)

    def test_all_host_capture_removes_direct_host_prefilter(self) -> None:
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
                capture_all_hosts=True,
            )
        self.assertEqual(process.command[-1], "tcp port 443")
        self.assertEqual(process.metadata["host_scope"], "all")

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


class MitmPortPreflightTest(unittest.TestCase):
    """开跑前的 MITM 端口预检。

    k61 的第一次报废：前轮异常收尾遗留的 mitmdump 占着 18080，而端口检查只在单个
    mitm case 启动时才做，于是排在它前面的 direct case 全部跑完、真实请求都发出去了，
    才在采集中途暴露。预检必须在整轮开跑前完成。
    """

    def test_free_port_passes(self) -> None:
        with contextlib.closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ensure_mitm_port_available(port)

    def test_occupied_port_is_rejected_with_port_in_message(self) -> None:
        with contextlib.closing(socket.socket()) as occupier:
            occupier.bind(("127.0.0.1", 0))
            occupier.listen(1)
            port = occupier.getsockname()[1]
            with self.assertRaises(CaptureProcessError) as raised:
                ensure_mitm_port_available(port)
        self.assertIn(str(port), str(raised.exception))

    def test_capture_process_start_reuses_the_same_guard(self) -> None:
        """单 case 启动前的兜底检查与预检必须同源，避免两处消息或判断分叉。"""

        with contextlib.closing(socket.socket()) as occupier:
            occupier.bind(("127.0.0.1", 0))
            occupier.listen(1)
            port = occupier.getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                plan = build_campaign_plan(
                    task="oauth",
                    batch_id="lifecycle-test",
                    scenarios=("s1",),
                    evidence_modes=("mitm",),
                    sub2api_base_url=None,
                    api_key_env="SUB2API_CAPTURE_API_KEY",
                )
                process = CaptureProcess(
                    case=plan.cases[0],
                    # start() 先建私有输出目录且拒绝覆盖已存在的，故给一个未创建的子路径
                    output_dir=Path(tmp) / "out",
                    command=["/bin/true"],
                    environment={},
                    mitm_port=port,
                )
                with self.assertRaises(CaptureProcessError) as raised:
                    process.start()
        self.assertIn(str(port), str(raised.exception))
