"""R16：把运行时重放对 ``/dev/null`` 的特例，延伸到另外三个无状态字符设备（``/dev/zero``、``/dev/urandom``、``/dev/random``）的只读访问。

背景（ARM64 真实严格审计）：OpenSSL／hashlib／uuid 等库初始化时会 ``open("/dev/urandom")`` 读熵，宿主快照（``host_snapshot``）拒绝
``/proc``、``/sys``、``/dev`` 下的路径（“虚拟或设备路径不能冒充稳定文件快照”），所以这类读取无法用声明覆盖，整个单元被挡在严格审计之外。
``/dev/null`` 早已由 ``read_audit_runtime.replay_runtime`` 直接放行（读恒空、写即丢弃），本轮把同一机制做成一张表
``STATELESS_DEVICE_KINDS``：路径在表里且该路径的访问类型集合是表里允许集合的子集，才算“已证明”。

========================  ==============================================  ==========================================
设备                      放行的访问类型                                    仍然拒绝
========================  ==============================================  ==========================================
``/dev/null``             read、write、stat（与改动前逐项相同）               exec、dir、list、missing
``/dev/zero``             read、stat                                       write、exec、dir、list、missing
``/dev/urandom``          read、stat                                       同上（写熵池会改内核状态）
``/dev/random``           read、stat                                       同上
其它（``/dev/ptmx``、``/dev/shm/*``、``/dev/sda``、``/proc/*``、``/sys/*``） 一律不放行                                 保持原行为
========================  ==============================================  ==========================================

合同建立（``contract_entry``）：对三个新设备做和 ``/dev/null``／``/dev/tty`` 同形态的身份校验（字符设备；Linux 上设备号 zero=1:5、
random=1:8、urandom=1:9），不符抛 ``RuntimeContractError("… 设备身份不符")``。校验结果**不进入**返回的 payload，所以合同摘要与改动前逐字节相同，
旧记录里封存的 ``runtime-contract`` 条目照旧能验真。

本模块的期望表 ``EXPECTED_*`` 与被测模块的常量是各写一份：被测常量被改（放宽或遗漏）时测试会失败，而不是跟着一起变。
"""

from __future__ import annotations

import copy
import itertools
import os
import stat
import sys
import types
import unittest
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

# --- 期望表（独立于被测模块的常量，故意各写一份） ---
EXPECTED_STATELESS = {
    "/dev/null": {"read", "write", "stat"},
    "/dev/zero": {"read", "stat"},
    "/dev/urandom": {"read", "stat"},
    "/dev/random": {"read", "stat"},
}
# Linux 上的标准设备号（major, minor）；/dev/tty 是 5:0，/dev/null 是 1:3。
EXPECTED_NUMBERS = {"/dev/zero": (1, 5), "/dev/random": (1, 8), "/dev/urandom": (1, 9)}
NEW_DEVICES = tuple(EXPECTED_NUMBERS)
ALL_KINDS = ("read", "write", "exec", "missing", "stat", "dir", "list")
# 与以上三个无关、也不得因此被放行的虚拟或设备路径。
OTHER_VIRTUAL_PATHS = (
    "/dev/ptmx", "/dev/shm/sem.x", "/dev/shm/x", "/dev/sda", "/dev/full", "/dev/kmsg", "/dev/mem", "/dev/stdin", "/dev/fd/0", "/dev/pts/0",
    "/dev/urandom2", "/dev/urandom/x", "/dev/zero/x", "/dev/./urandom", "/dev/null/x", "/devurandom",
    "/proc/sys/kernel/random/boot_id", "/proc/sys/kernel/random/uuid", "/sys/fs/selinux", "/proc/self/maps",
)


class DeviceFixture(AuditFixture):
    """一份只含私有盘内一个目录的 v4 文档作底座，往它的访问集合里注入 /dev 路径，直接检验重放返回的“已证明路径”。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # replay_runtime 每次都会重新加载 read_audit.py；穷举用例要调用几百次，复用同一份模块对象即可（行为相同）。
        cls.audit_module = runtime._read_audit()

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(runtime, "_read_audit", return_value=self.audit_module)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base_document = self.document([self.mkdir_line("base")])

    def covered(self, accesses: dict[str, list[str]], *, historical: bool = False, document: dict[str, Any] | None = None) -> set[str]:
        """把 ``accesses`` 注入底座文档的访问集合后重放，返回已证明路径。"""
        doc = copy.deepcopy(self.base_document if document is None else document)
        doc["accesses"] = list(doc["accesses"]) + [[path, sorted(kinds)] for path, kinds in accesses.items()]
        return runtime.replay_runtime(doc, [self.entry], historical=historical)

    def is_covered(self, path: str, kinds: list[str] | set[str] | tuple[str, ...]) -> bool:
        return path in self.covered({path: list(kinds)})

    def strace_line(self, label: str, path: str) -> str:
        """真实 strace 形态的行（解析器给出的访问类型见各行右侧注释）。"""
        return {
            "open-read": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY|O_CLOEXEC) = 3<{path}>',             # read
            "open-write": f'1 openat(AT_FDCWD</repo>, "{path}", O_WRONLY) = 3<{path}>',                      # write
            "open-rdwr": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDWR) = 3<{path}>',                         # write
            "open-trunc": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY|O_TRUNC) = 3<{path}>',              # write
            "open-dir": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY|O_DIRECTORY) = 3<{path}>',            # dir
            "open-enoent": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = -1 ENOENT (No such file or directory)',   # missing
            "open-eacces": f'1 openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = -1 EACCES (Permission denied)',          # stat
            "stat": f'1 stat("{path}", {{st_mode=S_IFCHR|0666, st_rdev=makedev(0x1, 0x9)}}) = 0',            # stat
            "exec": f'1 execve("{path}", ["x"], 0x1 /* 3 vars */) = 0',                                      # exec
            "chmod": f'1 chmod("{path}", 0600) = 0',                                                         # write
            "list": f'1 getdents64(3<{path}>, 0x1 /* 0 entries */, 32768) = 0',                              # list
        }[label]

    def accesses_of(self, document: dict[str, Any], path: str) -> list[str] | None:
        return dict(document["accesses"]).get(path)


class TableContractTests(DeviceFixture):
    """表本身：内容固定、没有任何一个被放行的设备缺身份校验、熵源设备不放行写。"""

    def test_the_table_has_exactly_the_agreed_contents(self) -> None:
        self.assertEqual({path: set(kinds) for path, kinds in runtime.STATELESS_DEVICE_KINDS.items()}, EXPECTED_STATELESS)

    def test_the_kind_vocabulary_is_the_one_the_tests_enumerate(self) -> None:
        """穷举用例枚举 ALL_KINDS；解析器将来新增访问类型时，这里先失败，提醒回来看新类型该不该放行。"""
        self.assertEqual(set(ALL_KINDS), set(ra.JUDGED_KINDS) | set(ra.METADATA_KINDS))

    def test_no_zero_or_entropy_device_allows_write_exec_dir_list_or_missing(self) -> None:
        for path in NEW_DEVICES:
            with self.subTest(path=path):
                self.assertEqual(set(runtime.STATELESS_DEVICE_KINDS[path]) & {"write", "exec", "dir", "list", "missing"}, set())

    def test_every_covered_device_has_an_identity_check_in_the_contract(self) -> None:
        """/dev/null 的身份由 contract_entry 内联校验（且进摘要）；其余被放行的设备必须在设备号表里，否则放行了却没人核对过它是什么。"""
        self.assertEqual(set(runtime.STATELESS_DEVICE_KINDS) - {"/dev/null"}, set(runtime.STATELESS_DEVICE_NUMBERS))
        self.assertEqual({path: tuple(numbers) for path, numbers in runtime.STATELESS_DEVICE_NUMBERS.items()}, EXPECTED_NUMBERS)


class ReplayCoverageTests(DeviceFixture):
    """``replay_runtime`` 对四个设备的覆盖判定。"""

    def test_exhaustive_every_nonempty_subset_of_kinds_for_the_four_devices(self) -> None:
        """128×4 种访问类型组合逐一核对：被覆盖当且仅当类型集合 ⊆ 期望表。穷举比举例更不容易漏掉“多放行了一个类型”。"""
        mismatches = []
        for path, allowed in EXPECTED_STATELESS.items():
            for size in range(1, len(ALL_KINDS) + 1):
                for kinds in itertools.combinations(ALL_KINDS, size):
                    expected = set(kinds) <= allowed      # 注意：不能写成 a != b <= c（链式比较）
                    if self.is_covered(path, kinds) != expected:
                        mismatches.append((path, kinds, "应覆盖" if expected else "应拒绝"))
        self.assertEqual(mismatches, [])

    def test_read_only_access_to_the_three_new_devices_is_covered(self) -> None:
        for path in NEW_DEVICES:
            for kinds in (["read"], ["read", "stat"], ["stat"]):
                with self.subTest(path=path, kinds=kinds):
                    self.assertIn(path, self.covered({path: kinds}))

    def test_urandom_write_exec_dir_list_missing_stay_uncovered(self) -> None:
        for kind in ("write", "exec", "dir", "list", "missing"):
            for companion in ([], ["read"], ["read", "stat"]):
                with self.subTest(kind=kind, companion=companion):
                    self.assertFalse(self.is_covered("/dev/urandom", [kind, *companion]))

    def test_writing_zero_or_random_is_never_covered_even_next_to_reads(self) -> None:
        for path in ("/dev/zero", "/dev/random"):
            for kinds in (["write"], ["read", "write"], ["read", "stat", "write"]):
                with self.subTest(path=path, kinds=kinds):
                    self.assertFalse(self.is_covered(path, kinds))

    def test_dev_null_behaviour_is_unchanged_item_by_item(self) -> None:
        """改前的行为：kinds ⊆ {read, write, stat} 即覆盖，其余不覆盖。"""
        for kinds in (["read"], ["write"], ["stat"], ["read", "write"], ["read", "stat"], ["stat", "write"], ["read", "stat", "write"]):
            with self.subTest(kinds=kinds):
                self.assertTrue(self.is_covered("/dev/null", kinds))
        for kinds in (["exec"], ["dir"], ["list"], ["missing"], ["read", "exec"], ["write", "dir"], ["stat", "list"], ["read", "missing"]):
            with self.subTest(kinds=kinds):
                self.assertFalse(self.is_covered("/dev/null", kinds))

    def test_other_virtual_and_device_paths_are_not_widened(self) -> None:
        for path in OTHER_VIRTUAL_PATHS:
            for kinds in (["read"], ["stat"], ["read", "stat"], ["write"], ["missing"], ["dir"], ["list"]):
                with self.subTest(path=path, kinds=kinds):
                    self.assertFalse(self.is_covered(path, kinds))

    def test_the_table_is_looked_up_by_exact_path_not_by_prefix_or_substring(self) -> None:
        covered = self.covered({"/dev/urandom": ["read"], "/dev/urandom2": ["read"], "/dev/urandom/x": ["read"], "/dev/./urandom": ["read"],
                                "/dev/xurandom": ["read"], "/dev/zero/x": ["read"]})
        self.assertEqual({path for path in covered if path.startswith("/dev")}, {"/dev/urandom"})

    def test_dev_tty_is_still_decided_only_by_the_terminal_probe_contract(self) -> None:
        """/dev/tty 不在表里：成功打开、非 ENXIO 失败仍被拒（沿用终端探测合同，与改动前相同）。"""
        for line in ('1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDWR) = 3</dev/tty>',
                     '1 openat(AT_FDCWD</repo>, "/dev/tty", O_RDONLY) = 3</dev/tty>'):
            with self.subTest(line=line):
                document = self.document([self.mkdir_line("base"), line])
                self.assertFalse(self.audit(document)["coverage_complete"])
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime.replay_runtime(document, [self.entry])

    def test_historical_replay_gets_the_same_coverage(self) -> None:
        """历史重放（校验封存的合同摘要，不读现场设备）同样查这张表；旧记录里凡是访问过这些设备的，本来就进不了“已通过”。"""
        covered = self.covered({"/dev/urandom": ["read", "stat"], "/dev/null": ["write"]}, historical=True)
        self.assertLessEqual({"/dev/urandom", "/dev/null"}, covered)
        self.assertNotIn("/dev/urandom", self.covered({"/dev/urandom": ["write"]}, historical=True))

    def test_a_v3_document_goes_through_the_same_table(self) -> None:
        """v3 重放（OutputTracker）与 v4 共用 replay_runtime 末尾的这段判定；control 四链各有自己的 replay_runtime（本轮不动）。"""
        document = self.document([self.mkdir_line("base"), self.strace_line("open-read", "/dev/urandom"),
                                  self.strace_line("open-write", "/dev/zero")], "v3")
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        covered = runtime.replay_runtime(document, [self.entry])
        self.assertIn("/dev/urandom", covered)
        self.assertNotIn("/dev/zero", covered)


class EndToEndTests(DeviceFixture):
    """真实 strace 行 → 解析器 → 严格审计（``strict_audit_reads``），确认库初始化读熵不再被判未声明，越界仍然被判。"""

    def test_the_library_entropy_open_sequence_passes_the_strict_audit(self) -> None:
        lines = [self.mkdir_line("base"),
                 self.strace_line("open-read", "/dev/urandom"),
                 self.strace_line("stat", "/dev/urandom"),
                 self.strace_line("open-read", "/dev/random"),
                 self.strace_line("open-read", "/dev/zero")]
        document = self.document(lines)
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertEqual(self.accesses_of(document, "/dev/urandom"), ["read", "stat"])
        report = self.audit(document)
        self.assertEqual((report["status"], report["undeclared_count"]), ("passed", 0), report["undeclared"])
        self.assertTrue(report["coverage_complete"])

    def test_the_parser_kinds_assumed_by_the_other_tests_are_what_the_parser_really_gives(self) -> None:
        expected = {"open-read": "read", "open-write": "write", "open-rdwr": "write", "open-trunc": "write", "open-dir": "dir",
                    "open-enoent": "missing", "open-eacces": "stat", "stat": "stat", "exec": "exec", "chmod": "write", "list": "list"}
        for label, kind in expected.items():
            with self.subTest(label=label):
                document = self.document([self.mkdir_line("base"), self.strace_line(label, "/dev/urandom")])
                self.assertEqual(self.accesses_of(document, "/dev/urandom"), [kind])

    def test_out_of_bound_access_to_urandom_is_still_reported_undeclared(self) -> None:
        for label, kind in (("open-write", "write"), ("open-rdwr", "write"), ("open-trunc", "write"), ("chmod", "write"), ("exec", "exec"),
                            ("open-dir", "dir"), ("list", "list"), ("open-enoent", "missing")):
            with self.subTest(label=label):
                document = self.document([self.mkdir_line("base"), self.strace_line(label, "/dev/urandom")])
                report = self.audit(document)
                self.assertFalse(report["coverage_complete"])
                self.assertEqual([(item["path"], item["kinds"]) for item in report["undeclared"]], [("/dev/urandom", [kind])])

    def test_zero_write_and_other_devices_are_reported_undeclared(self) -> None:
        for path, label in (("/dev/zero", "open-write"), ("/dev/random", "open-rdwr"), ("/dev/ptmx", "open-read"), ("/dev/shm/sem.x", "open-read"),
                            ("/dev/sda", "open-read"), ("/proc/sys/kernel/random/boot_id", "open-read"), ("/sys/fs/selinux", "open-read")):
            with self.subTest(path=path, label=label):
                document = self.document([self.mkdir_line("base"), self.strace_line(label, path)])
                report = self.audit(document)
                self.assertFalse(report["coverage_complete"])
                self.assertEqual([item["path"] for item in report["undeclared"]], [path])

    def test_only_the_device_leaves_the_undeclared_list_other_host_reads_remain(self) -> None:
        document = self.document([self.mkdir_line("base"), self.strace_line("open-read", "/dev/urandom"),
                                  '1 openat(AT_FDCWD</repo>, "/etc/hosts", O_RDONLY) = 3</etc/hosts>'])
        report = self.audit(document)
        self.assertEqual([item["path"] for item in report["undeclared"]], ["/etc/hosts"])


def char_device(major: int, minor: int) -> types.SimpleNamespace:
    """lstat 的替身：字符设备，设备号用本机的 ``os.makedev`` 编码（与 ``os.major``／``os.minor`` 互逆）。"""
    return types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o666, st_uid=0, st_gid=0, st_rdev=os.makedev(major, minor))


def not_a_device(mode: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0, st_rdev=0)


STANDARD_LSTAT = {"/dev/null": char_device(1, 3), "/dev/tty": char_device(5, 0),
                  "/dev/zero": char_device(1, 5), "/dev/random": char_device(1, 8), "/dev/urandom": char_device(1, 9)}


class ContractIdentityTests(unittest.TestCase):
    """``contract_entry`` 建立合同时的设备身份校验；用替身 lstat 模拟设备被替换（普通文件、目录、符号链接、错误设备号）。"""

    def setUp(self) -> None:
        self.contract = {"schema_version": runtime.SCHEMA, "temporary_root": "/tmp/r16-private/a/b/output",
                         "mask_roots": [runtime.MASK_ROOT], "environment_paths": []}
        self.real_lstat = os.lstat

    def lstat_with(self, overrides: dict[str, Any]):
        """只替换 ``overrides`` 里的路径，其余走真实 lstat。"""
        def lstat(path, *args, **kwargs):
            if path in overrides:
                result = overrides[path]
                if isinstance(result, BaseException):
                    raise result
                return result
            return self.real_lstat(path, *args, **kwargs)
        return mock.patch.object(runtime.os, "lstat", side_effect=lstat)

    def entry(self, overrides: dict[str, Any], platform: str) -> dict[str, Any]:
        with mock.patch.object(runtime.sys, "platform", platform), self.lstat_with({**STANDARD_LSTAT, **overrides}):
            return runtime.contract_entry(self.contract)

    def test_standard_devices_are_accepted_on_linux(self) -> None:
        self.assertEqual(self.entry({}, "linux")["name"], "runtime-contract")

    def test_a_replaced_device_is_refused_on_linux_whatever_way_it_differs(self) -> None:
        bad = {"普通文件": not_a_device(stat.S_IFREG | 0o644), "目录": not_a_device(stat.S_IFDIR | 0o755),
               "符号链接": not_a_device(stat.S_IFLNK | 0o777), "块设备": not_a_device(stat.S_IFBLK | 0o660),
               "命名管道": not_a_device(stat.S_IFIFO | 0o600),
               "设备号 0:0": char_device(0, 0), "设备号 1:3（null 的）": char_device(1, 3), "设备号 5:0（tty 的）": char_device(5, 0),
               "设备号 1:255": char_device(1, 255), "设备号 2:9": char_device(2, 9)}
        for path in NEW_DEVICES:
            for label, replacement in bad.items():
                with self.subTest(path=path, replacement=label):
                    with self.assertRaises(runtime.RuntimeContractError) as caught:
                        self.entry({path: replacement}, "linux")
                    self.assertIn("设备身份不符", str(caught.exception))
                    self.assertIn(path, str(caught.exception))

    def test_the_three_standard_numbers_are_not_interchangeable(self) -> None:
        """urandom 冒充 random（1:8）、random 冒充 urandom（1:9）、zero 冒充 random：设备号必须各归各。"""
        for path, wrong in (("/dev/urandom", (1, 8)), ("/dev/random", (1, 9)), ("/dev/zero", (1, 8)), ("/dev/zero", (1, 9)),
                            ("/dev/random", (1, 5)), ("/dev/urandom", (1, 5))):
            with self.subTest(path=path, wrong=wrong):
                with self.assertRaises(runtime.RuntimeContractError):
                    self.entry({path: char_device(*wrong)}, "linux")

    def test_off_linux_only_the_character_device_type_is_checked(self) -> None:
        """与 /dev/null 同形态：非 Linux 不核对设备号（macOS 上各设备号完全不同），但仍要求是字符设备。"""
        with mock.patch.object(runtime.os, "major", side_effect=AssertionError("非 Linux 不应核对设备号")):
            for path in NEW_DEVICES:
                with self.subTest(path=path, case="odd-numbers"):
                    self.assertEqual(self.entry({path: char_device(9, 99)}, "darwin")["name"], "runtime-contract")
                for label, replacement in (("普通文件", not_a_device(stat.S_IFREG | 0o644)), ("目录", not_a_device(stat.S_IFDIR | 0o755))):
                    with self.subTest(path=path, case=label):
                        with self.assertRaises(runtime.RuntimeContractError) as caught:
                            self.entry({path: replacement}, "darwin")
                        self.assertIn("设备身份不符", str(caught.exception))

    def test_a_missing_device_fails_closed(self) -> None:
        for path in NEW_DEVICES:
            with self.subTest(path=path):
                with self.assertRaises((OSError, runtime.RuntimeContractError)):
                    self.entry({path: FileNotFoundError(2, "No such file or directory", path)}, "linux")

    def test_the_new_checks_do_not_replace_the_existing_ones(self) -> None:
        """/dev/null 与 /dev/tty 的校验仍在（消息不变）。"""
        with self.assertRaisesRegex(runtime.RuntimeContractError, r"/dev/null 设备身份不符"):
            self.entry({"/dev/null": char_device(1, 4)}, "linux")
        with self.assertRaisesRegex(runtime.RuntimeContractError, r"/dev/null 不是标准字符设备"):
            self.entry({"/dev/null": not_a_device(stat.S_IFREG | 0o644)}, "linux")
        with self.assertRaisesRegex(runtime.RuntimeContractError, r"/dev/tty 设备身份不符"):
            self.entry({"/dev/tty": char_device(5, 1)}, "linux")


def reference_contract_entry(contract: dict[str, Any]) -> dict[str, Any]:
    """改动之前的 ``contract_entry`` 逐行照抄（只用模块里没变的辅助函数）：只校验 /dev/null 与 /dev/tty，payload 里只有这两个设备。

    用来证明新增校验没有改变返回内容，因此合同摘要（以及封存在旧记录里的 ``runtime-contract`` 条目）与改动前逐字节相同。
    """
    import errno
    import platform
    contract = runtime.validate_contract(contract)
    device = os.lstat("/dev/null")
    if not stat.S_ISCHR(device.st_mode):
        raise runtime.RuntimeContractError("/dev/null 不是标准字符设备")
    if sys.platform.startswith("linux") and (os.major(device.st_rdev), os.minor(device.st_rdev)) != (1, 3):
        raise runtime.RuntimeContractError("/dev/null 设备身份不符")
    terminal = os.lstat("/dev/tty")
    if not stat.S_ISCHR(terminal.st_mode) or (sys.platform.startswith("linux")
            and (os.major(terminal.st_rdev), os.minor(terminal.st_rdev)) != (5, 0)):
        raise runtime.RuntimeContractError("/dev/tty 设备身份不符")
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno != errno.ENXIO:
            raise runtime.RuntimeContractError("控制终端探测不符合无终端合同") from error
        terminal_state = "absent-ENXIO"
    else:
        os.close(descriptor)
        terminal_state = "present"
    payload = {"contract": contract, "platform": {"system": platform.system(), "arch": platform.machine(),
               "kernel": platform.release()}, "environment": [runtime.environment_snapshot(path) for path in contract["environment_paths"]],
               "null_device": {"mode": device.st_mode, "uid": device.st_uid, "gid": device.st_gid, "rdev": device.st_rdev},
               "terminal": {"state": terminal_state, "mode": terminal.st_mode, "uid": terminal.st_uid,
                            "gid": terminal.st_gid, "rdev": terminal.st_rdev}}
    return {"category": "environment", "name": "runtime-contract", "sha256": runtime.digest(payload),
            "detail": {"schema_version": runtime.SCHEMA, **payload}}


class ContractDigestUnchangedTests(unittest.TestCase):
    """新增的设备身份校验只“校验”，不进返回内容：合同摘要不变，旧记录的 runtime-contract 条目不会因本轮而漂移。"""

    CONTRACTS = (
        {"schema_version": runtime.SCHEMA, "temporary_root": "/tmp/r16-private/a/b/output", "mask_roots": [runtime.MASK_ROOT], "environment_paths": []},
        {"schema_version": runtime.SCHEMA, "temporary_root": "/tmp/r16-private/a/b/output", "mask_roots": [],
         "environment_paths": sorted(runtime.ENVIRONMENT_PATHS)},
    )

    def test_the_entry_equals_what_the_pre_change_algorithm_returns(self) -> None:
        for contract in self.CONTRACTS:
            with self.subTest(mask_roots=contract["mask_roots"]):
                self.assertEqual(runtime.contract_entry(contract), reference_contract_entry(contract))

    def test_the_payload_still_binds_only_null_and_tty(self) -> None:
        detail = runtime.contract_entry(self.CONTRACTS[0])["detail"]
        self.assertEqual(set(detail), {"schema_version", "contract", "platform", "environment", "null_device", "terminal"})

    def test_the_historical_entry_digest_formula_is_untouched(self) -> None:
        """历史重放用封存的 detail 重算摘要（不读现场设备）：公式与现场路径一致。"""
        entry = runtime.contract_entry(self.CONTRACTS[0])
        detail = entry["detail"]
        self.assertEqual(entry["sha256"], runtime.digest({key: value for key, value in detail.items() if key != "schema_version"}))


if __name__ == "__main__":
    unittest.main()
