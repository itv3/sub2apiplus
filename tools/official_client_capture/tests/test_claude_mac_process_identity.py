"""只读进程原语回归；真实自身／无网络helper，不启动目标产品或发信号。"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_mac_process_identity as p


def _fake_bindings(*, identities=None, paths=None, boots=None, info_return=None,
                   path_return=None, boot_return=0, boot_length=None, native_errno=0):
    """只替换内核输出，用于短读、竞争与错误回归；不能登记正式来源。"""
    original = {"pid": 321, "ppid": 123, "uid": 501, "seconds": 100, "microseconds": 42}
    identities = list(identities or [original, original])
    paths = list(paths or [b"/fixture/python\0", b"/fixture/python\0"])
    boots = list(boots or [{"seconds": 50, "microseconds": 3}] * 2)

    def info(pid, flavor, arg, buffer, size):
        if native_errno:
            ctypes.set_errno(native_errno)
        row = identities.pop(0)
        out = ctypes.cast(buffer, ctypes.POINTER(p._ProcBSDInfo)).contents
        out.pbi_pid, out.pbi_ppid, out.pbi_uid = row["pid"], row["ppid"], row["uid"]
        out.pbi_start_tvsec, out.pbi_start_tvusec = row["seconds"], row["microseconds"]
        return size if info_return is None else info_return

    def path(pid, buffer, size):
        if native_errno:
            ctypes.set_errno(native_errno)
        raw = paths.pop(0)
        ctypes.memmove(buffer, raw, len(raw))
        return len(raw) - 1 if path_return is None else path_return

    def boot(name, buffer, length, new_value, new_length):
        if native_errno:
            ctypes.set_errno(native_errno)
        row = boots.pop(0)
        out = ctypes.cast(buffer, ctypes.POINTER(p._Timeval)).contents
        out.tv_sec, out.tv_usec = row["seconds"], row["microseconds"]
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t)).contents.value = (
            ctypes.sizeof(p._Timeval) if boot_length is None else boot_length)
        if name != b"kern.boottime" or new_value is not None or new_length != 0:
            raise AssertionError("sysctl必须是固定只读查询")
        return boot_return

    return p._NativeBindings(info, path, boot)


class ProcessIdentityTests(unittest.TestCase):
    def self_executable_from_dyld(self):
        """独立dyld只读原件；Python启动器路径不能冒充实际运行executable。"""
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        query = library._NSGetExecutablePath
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        query.restype = ctypes.c_int
        value, size = ctypes.create_string_buffer(4096), ctypes.c_uint32(4096)
        self.assertEqual(query(value, ctypes.byref(size)), 0)
        return Path(value.value.decode("utf-8", errors="strict"))

    @contextmanager
    def helper(self):
        """helper只写ready并等待stdin结束；回收只走输入与wait，不发送信号。"""
        with tempfile.TemporaryDirectory() as temporary:
            command = [sys.executable, "-B", "-c",
                       "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.buffer.read(1)"]
            child = subprocess.Popen(command, cwd=temporary, env={}, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                readable, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(readable, "无网络helper必须在5秒内准备好")
                self.assertEqual(child.stdout.readline(), "ready\n")
                yield child
            finally:
                if child.poll() is None:
                    child.communicate(input="x", timeout=5)
                else:
                    child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)

    def sample(self, **kwargs):
        with mock.patch.object(p, "_load_native", return_value=_fake_bindings(**kwargs)):
            return p.read_process_snapshot(321)

    @unittest.skipUnless(sys.platform == "darwin", "真实 libproc 自进程读取只在 Darwin 验收")
    def test_real_self_libproc_is_stable_and_full_snapshot_keeps_boot_permission_boundary(self):
        bindings = p._load_native()
        first, second = p._read_bsd(bindings, os.getpid()), p._read_bsd(bindings, os.getpid())
        self.assertEqual(first, second)
        self.assertEqual(first["ppid"], os.getppid())
        self.assertEqual(first["uid"], os.getuid())
        path = p._read_path(bindings, os.getpid())
        self.assertEqual(path, p._read_path(bindings, os.getpid()))
        self.assertTrue(Path(path).samefile(self.self_executable_from_dyld()))
        try:
            actual = p.read_process_snapshot(os.getpid())
        except p.ProcessSnapshotUnavailable as error:
            # 当前沙箱确实拒绝kern.boottime；只接受这个实际错误，不把其他失败吞掉。
            self.assertRegex(str(error), r"^内核启动时间不可读，errno=1：")
        else:
            p.verify_same_process(actual, p.read_process_snapshot(os.getpid()))
            self.assertNotIn("boot_id", actual)
            self.assertNotIn("executable_sha256", actual)
            for field in ("registration_verified", "formal_ready", "launch_allowed",
                          "process_events_complete", "executed_image_verified"):
                self.assertIs(actual[field], False)

    @unittest.skipUnless(sys.platform == "darwin", "真实 libproc 子进程读取只在 Darwin 验收")
    def test_real_helper_parent_start_and_exit_are_read_without_signals(self):
        with self.helper() as child:
            bindings = p._load_native()
            first, second = p._read_bsd(bindings, child.pid), p._read_bsd(bindings, child.pid)
            self.assertEqual(first, second)
            self.assertEqual(first["ppid"], os.getpid())
            self.assertEqual(first["uid"], os.getuid())
            self.assertTrue(Path(p._read_path(bindings, child.pid)).samefile(self.self_executable_from_dyld()))
            try:
                actual = p.read_process_snapshot(child.pid)
            except p.ProcessSnapshotUnavailable as error:
                self.assertRegex(str(error), r"^内核启动时间不可读，errno=1：")
            else:
                p.verify_same_process(actual, p.read_process_snapshot(child.pid))
            child.communicate(input="x", timeout=5)
            with self.assertRaises(p.ProcessSnapshotUnavailable):
                p._read_bsd(bindings, child.pid)

    @unittest.skipUnless(sys.platform == "darwin", "真实 kern.boottime 查询只在 Darwin 验收")
    def test_real_boot_query_is_readonly_and_permission_denial_stays_unavailable(self):
        try:
            value = p.read_kernel_boot_time()
        except p.ProcessSnapshotUnavailable as error:
            self.assertRegex(str(error), r"^内核启动时间不可读，errno=1：")
        else:
            self.assertEqual(set(value), {"seconds", "microseconds"})
            self.assertGreater(value["seconds"], 0)
            self.assertGreaterEqual(value["microseconds"], 0)
            self.assertLess(value["microseconds"], 1_000_000)

    def test_invalid_pid_is_rejected_before_loading_native(self):
        with mock.patch.object(p, "_load_native") as load:
            for value in (True, False, 0, -1, 2_147_483_648, "321", 321.0, None):
                with self.subTest(value=value), self.assertRaises(p.ProcessSnapshotError):
                    p.read_process_snapshot(value)
            load.assert_not_called()

    def test_unsupported_platform_and_machine_are_unavailable(self):
        for system, machine in (("Linux", "arm64"), ("Darwin", "i386")):
            with self.subTest(system=system, machine=machine), mock.patch.object(p.platform, "system", return_value=system), \
                    mock.patch.object(p.platform, "machine", return_value=machine), mock.patch.object(p.ctypes, "CDLL") as load:
                with self.assertRaises(p.ProcessSnapshotUnavailable):
                    p.read_process_snapshot(321)
                load.assert_not_called()

    def test_abi_mismatch_is_rejected_before_loading_library(self):
        original = ctypes.sizeof
        def changed(value):
            return original(value) + (8 if value is p._ProcBSDInfo else 0)
        # 固定受支持平台后才破坏结构长度，避免 Linux 在平台门禁就退出而没有覆盖 ABI 拒绝。
        with mock.patch.object(p.platform, "system", return_value="Darwin"), \
                mock.patch.object(p.platform, "machine", return_value="arm64"), \
                mock.patch.object(p.ctypes, "sizeof", side_effect=changed), mock.patch.object(p.ctypes, "CDLL") as load:
            with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "ABI"):
                p.read_process_snapshot(321)
            load.assert_not_called()

    def test_missing_native_library_is_unavailable(self):
        # 同样固定平台，确保本负例实际到达库加载失败，而非误把平台拒绝当成覆盖。
        with mock.patch.object(p.platform, "system", return_value="Darwin"), \
                mock.patch.object(p.platform, "machine", return_value="arm64"), \
                mock.patch.object(p.ctypes, "CDLL", side_effect=OSError("不可加载")) as load:
            with self.assertRaises(p.ProcessSnapshotUnavailable):
                p.read_process_snapshot(321)
            load.assert_called_once()

    def test_native_info_short_read_is_not_a_valid_identity(self):
        for returned in (1, 135, 137):
            with self.subTest(returned=returned), self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "短读|ABI"):
                self.sample(info_return=returned)

    def test_native_permission_error_is_not_zero_process(self):
        with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "errno=1"):
            self.sample(info_return=0, native_errno=errno.EPERM)

    def test_native_boot_error_and_short_read_are_unavailable(self):
        with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "errno=5"):
            self.sample(boot_return=-1, native_errno=errno.EIO)
        with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "短读"):
            self.sample(boot_length=8)

    def test_pid_start_parent_and_uid_changes_during_read_are_rejected(self):
        initial = {"pid": 321, "ppid": 123, "uid": 501, "seconds": 100, "microseconds": 42}
        for key, value in (("pid", 322), ("ppid", 124), ("uid", 502), ("seconds", 101), ("microseconds", 43)):
            changed = {**initial, key: value}
            with self.subTest(key=key), self.assertRaises(p.ProcessSnapshotUnavailable):
                self.sample(identities=[initial, changed])

    def test_boot_time_change_during_read_is_rejected(self):
        with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "读取期间变化"):
            self.sample(boots=[{"seconds": 50, "microseconds": 3}, {"seconds": 51, "microseconds": 3}])

    def test_executable_path_change_during_read_is_rejected(self):
        with self.assertRaisesRegex(p.ProcessSnapshotUnavailable, "读取期间变化"):
            self.sample(paths=[b"/fixture/python\0", b"/fixture/another\0"])

    def test_path_native_error_and_length_are_not_success(self):
        for returned in (0, -1, 4096, 4097, 2):
            with self.subTest(returned=returned), self.assertRaises(p.ProcessSnapshotUnavailable):
                self.sample(path_return=returned)

    def test_unterminated_relative_and_invalid_utf8_paths_are_rejected(self):
        for raw in (b"/fixture/no-nul", b"relative\0", b"/fixture/\xff\0", b"/fixture/\n\0"):
            with self.subTest(raw=raw), self.assertRaises(p.ProcessSnapshotUnavailable):
                self.sample(paths=[raw, raw])

    def test_illegal_microsecond_and_wrong_pid_are_unavailable(self):
        initial = {"pid": 321, "ppid": 123, "uid": 501, "seconds": 100, "microseconds": 42}
        for changed in ({**initial, "pid": 322}, {**initial, "microseconds": 1_000_000}, {**initial, "seconds": 0}):
            with self.subTest(changed=changed), self.assertRaises(p.ProcessSnapshotUnavailable):
                self.sample(identities=[changed, changed])

    def test_verify_rejects_pid_reuse_parent_uid_path_and_boot_changes(self):
        expected = self.sample()
        for key, value in (("pid", 322), ("ppid", 124), ("uid", 502), ("start_time_seconds", 101),
                           ("start_time_microseconds", 43), ("executable_path", "/another"),
                           ("kernel_boot_time", {"seconds": 51, "microseconds": 3})):
            changed = {**expected, key: value}
            with self.subTest(key=key), self.assertRaises(p.ProcessSnapshotError):
                p.verify_same_process(expected, changed)

    def test_verify_rejects_extra_fields_or_self_reported_permissions(self):
        expected = self.sample()
        for changed in ({**expected, "boot_id": "伪造"}, {**expected, "launch_allowed": True},
                        {**expected, "executed_image_verified": True}, {**expected, "process_events_complete": True},
                        {**expected, "uid": True}, {**expected, "observed_at_utc": "2026-10-08T12:00:00"}):
            with self.subTest(changed=changed), self.assertRaises(p.ProcessSnapshotError):
                p.verify_same_process(expected, changed)

    def test_snapshot_has_no_environment_command_or_signal_side_effect(self):
        bindings = _fake_bindings()
        with mock.patch.object(p, "_load_native", return_value=bindings), \
                mock.patch.object(os, "getenv", side_effect=AssertionError("不得读环境")), \
                mock.patch.object(os, "kill", side_effect=AssertionError("不得发信号")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不得启动")):
            value = p.read_process_snapshot(321)
        self.assertIs(value["launch_allowed"], False)


if __name__ == "__main__":
    unittest.main()
