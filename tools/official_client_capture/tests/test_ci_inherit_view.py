"""第 10a 项：承接输入收窄（承接视图 ``unit-inherit-view/v1``）的合同测试，对应规格 T1～T36。

被测代码：``tools/ci/read_audit.py`` 的承接视图一节（``inherit_view``／``inherit_view_digest``／``inherit_current_digest``／
``narrowed_inherit_problems``）、``unit_records.py`` 的窄化分支（``_narrowed_source``／``_narrowed_problems``／``check_record``／
``verify_manifest``）、``unit_executor.py`` 的写入与清单承接项、``unit_inherit.verify_published``、``target_platform_gate`` 的跳过分支。

做法：用 ``unit_inherit_fixture.World`` 的存储与清单，另在世界里建一棵真实的临时“测试树”（文件、目录、符号链接、字节码镜像），
用合成 strace 行得到真实的严格读集轨迹，来源记录封存的访问类型与视图摘要一律调用真实实现生成（不手写摘要）。每个反例都改
真实的文件系统，再走与真实承接相同的流程：``unit_inherit._carry_host_inputs`` 沿用来源登记的宿主路径并现场快照，再
``unit_records.check_record``。既有测试文件一行不改，用来证明旧路径逐字节不变。

已知缺口（T10 固定为用例）：目录快照只有名字没有 ``d_type``，被枚举目录里“同名文件变目录”发现不了；由第 11、17 项的 git 类型变更
一票否决补上，不是回归。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import control_read_audit as control_audit
from tools.ci import read_audit as ra
from tools.ci import target_platform_gate as target
from tools.ci import unit_executor as ue
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_read_audit as base_audit
from tools.official_client_capture.tests import unit_inherit_fixture as fixture

FILE_MODE = 0o644
DIR_MODE = 0o755


def make_dir(path: Path) -> Path:
    """建目录并显式设为 ``DIR_MODE``（结论与进程 umask 无关）。"""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def write_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, FILE_MODE)
    return path


def same_size_change(path: Path) -> None:
    """内容变了、大小不变：条目摘要（无条件集）不变，只有按访问方式的内容比较能发现。"""
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])


class NarrowedWorld(unittest.TestCase):
    """一个带真实测试树的合成来源世界：来源记录严格闭合，封存了访问类型与承接视图摘要。

    测试树里每个文件／目录的访问方式（来源轨迹）：

    * ``read.txt``：read；``stat.txt``：stat；``unused.txt``：没被访问；
    * ``listed/``：stat＋list（里面 a.txt、b.txt）；``statdir/``：只 stat；``unlisted/``：没被访问；``absent.txt``：probe 到不存在；
    * ``pkg/mod.py``：只 stat，但它的镜像字节码被 read（G-P）；``pkg/nopyc.py``：只 stat、没有镜像字节码；
    * ``alias``（符号链接→``real.txt``）：stat；``ldir``（符号链接→``realdir``）下的 ``f.txt``：stat（途经中间链接）；
    * 测试树之外：``outside/lib.py`` 只 stat、其镜像字节码被 read（标准库同形，不提升）；``outside/sys.txt``：read。
    """

    UNIT = "test_alpha"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.world = fixture.World(base / "world", inputs={self.UNIT: self.nonhost()})
        self.tree = make_dir(self.world.root / "tree")
        self.outside = make_dir(self.world.root / "outside")
        self.pyc_prefix = make_dir(self.world.root / "pycache")
        self.read = write_file(self.tree / "read.txt", "read-me-12345678")
        self.stat = write_file(self.tree / "stat.txt", "stat-me-12345678")
        self.unused = write_file(self.tree / "unused.txt", "unused-12345678")
        self.listed = make_dir(self.tree / "listed")
        write_file(self.listed / "a.txt", "甲")
        write_file(self.listed / "b.txt", "乙")
        self.statdir = make_dir(self.tree / "statdir")
        write_file(self.statdir / "x.txt", "x")
        self.unlisted = make_dir(self.tree / "unlisted")
        write_file(self.unlisted / "y.txt", "y")
        self.absent = self.tree / "absent.txt"
        self.mod = write_file(self.tree / "pkg" / "mod.py", "VALUE = 11\n")
        self.nopyc = write_file(self.tree / "pkg" / "nopyc.py", "VALUE = 22\n")
        self.real = write_file(self.tree / "real.txt", "real-file-1234")
        self.other = write_file(self.tree / "other.txt", "other-file-123")
        self.alias = self.tree / "alias"
        self.alias.symlink_to(self.real)
        self.unused_alias = self.tree / "unused_alias"
        self.unused_alias.symlink_to(self.real)
        self.realdir = make_dir(self.tree / "realdir")
        write_file(self.realdir / "f.txt", "through-link")
        self.realdir2 = make_dir(self.tree / "realdir2")
        write_file(self.realdir2 / "f.txt", "through-link")
        self.ldir = self.tree / "ldir"
        self.ldir.symlink_to(self.realdir)
        self.through = self.ldir / "f.txt"
        self.pyc = self.pyc_file(self.mod)
        write_file(self.pyc, "pyc-of-mod-1234567")
        self.lib = write_file(self.outside / "lib.py", "LIB = 1\n")
        self.lib_pyc = self.pyc_file(self.lib)
        write_file(self.lib_pyc, "pyc-of-lib-1234567")
        self.sys_file = write_file(self.outside / "sys.txt", "system-12345678")

    # ------------------------------------------------------------------ 世界的构造

    def pyc_file(self, source: Path) -> Path:
        """PEP 3147 镜像字节码：``<前缀>/<源文件绝对路径去掉开头的 / 与 .py>.cpython-312.pyc``。"""
        return self.pyc_prefix / (str(source)[1:-3] + ".cpython-312.pyc")

    @staticmethod
    def nonhost(*, docs: str = "d1", head: str = "H1", spec: str = "S1", extra: dict | None = None) -> list[dict]:
        """当前声明里的非宿主条目：文件项、仓库范围项、HEAD（均属 F3 排除名单）和一个整份纳入的条目。"""
        entries = [ur.value_entry("tests", "file:test_alpha", "内容-test_alpha"), ur.value_entry("docs", "repo:docs", docs),
                   ur.value_entry("tests", "head", head), ur.value_entry("environment", "runtime-test-spec", spec)]
        for name, value in (extra or {}).items():
            entries.append(ur.value_entry("environment", name, value))
        return entries

    def trace_lines(self) -> list[str]:
        cwd = f"AT_FDCWD<{self.tree}>"
        stat = lambda path: f'1 newfstatat({cwd}, "{path}", 0x1, 0) = 0'
        read = lambda path: f'1 openat({cwd}, "{path}", O_RDONLY) = 3<{path}>'
        return [
            read(self.read), stat(self.stat), stat(self.listed), f'1 getdents64(3<{self.listed}>, 0x1 /* 3 entries */, 32768) = 80',
            stat(self.statdir), f'1 openat({cwd}, "{self.absent}", O_RDONLY) = -1 ENOENT (No such file or directory)',
            stat(self.mod), read(self.pyc), stat(self.nopyc), stat(self.alias), stat(self.through),
            stat(self.lib), read(self.lib_pyc), read(self.sys_file),
        ]

    def host_paths(self) -> list[Path]:
        """来源记录登记的全部宿主路径（含没被访问的）。"""
        return [self.read, self.stat, self.unused, self.listed, self.statdir, self.unlisted, self.absent, self.mod, self.nopyc,
                self.alias, self.unused_alias, self.through, self.pyc, self.lib, self.lib_pyc, self.sys_file]

    def put_trace(self, lines: list[str]) -> tuple[dict, str]:
        document = ra.filter_stream(lines, ["/"], strict=True)
        path = self.world.root / f"trace-{self.world.counter}.json"
        self.world.counter += 1
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        digest = ur.file_sha256(path)
        self.world.store.put_log(path, digest)
        return document, digest

    def record_inputs(self) -> list[dict]:
        return ur._unique([*self.nonhost(), *(ra.host_snapshot_v3(str(path)) for path in self.host_paths()),
                           ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])

    def audit_block(self, inputs: list[dict], document: dict, digest: str, **overrides) -> dict:
        modes = ra.host_path_modes(document, inputs)
        roots = (str(self.tree), None)
        block = {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1", "undeclared_count": 0,
                 "undeclared": [], "inputs_sha256": ur.entries_sha256(inputs), "trace": {"sha256": digest},
                 "repo_root": str(self.tree), "data_root": None, "host_path_modes": modes,
                 "inherit_view_schema": ra.INHERIT_VIEW_SCHEMA,
                 "inherit_inputs_sha256": ra.inherit_view_digest(ra.inherit_view(inputs, modes, roots=roots))}
        block.update(overrides)
        return block

    def publish(self, *, run_id: str = "narrow-run", inputs: list[dict] | None = None, lines: list[str] | None = None,
                completed: float | None = None, block: dict | None = None, drop: tuple = (), **record_overrides) -> dict:
        """发布一条窄化来源记录和它所在运行的清单；``drop`` 是要从 read_audit 里删掉的键，``block`` 是整块覆盖。"""
        inputs = inputs if inputs is not None else self.record_inputs()
        document, digest = self.put_trace(lines if lines is not None else self.trace_lines())
        audit = block if block is not None else self.audit_block(inputs, document, digest)
        for key in drop:
            audit.pop(key, None)
        record = self.world.source_record(self.UNIT, run_id=run_id, inputs=inputs, read_audit=audit, completed=completed,
                                          **record_overrides)
        self.world.publish_run(run_id, "re-execute", {self.UNIT: (record, "executed")}, decided=fixture.T0 - 3600)
        return record

    def check(self, record: dict, *, nonhost: list[dict] | None = None, repo_root: str | None = "auto", before=None,
              **facts) -> tuple[list[str], ur.Current]:
        """与真实承接相同的流程：沿用来源登记的宿主路径并现场快照，再 check_record。``before`` 在快照之后、核对之前调用（制造漂移）。"""
        current = self.world.current(self.UNIT, inputs=list(nonhost if nonhost is not None else self.nonhost()))
        ui._carry_host_inputs(current, record)
        current.inherit_repo_root = str(self.tree) if repo_root == "auto" else repo_root
        if before is not None:
            before(current)
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        return ur.check_record(self.world.store, path, record, current, self.world.facts(fixture.T0, **facts)), current

    def reseal(self, record: dict, mutate, *, run_id: str) -> dict:
        """改写记录、重算自摘要、另存为一条新记录并发布它所在的运行清单。"""
        body = {key: value for key, value in copy.deepcopy(record).items() if key != "record_sha256"}
        body["run"] = {**body["run"], "run_id": run_id}
        mutate(body)
        forged = ur.seal_record(body)
        self.world.store.put_record(forged)
        self.world.publish_run(run_id, "re-execute", {self.UNIT: (forged, "executed")}, decided=fixture.T0 - 3600)
        return forged

    def assertAccepted(self, problems: list[str]) -> None:
        self.assertEqual(problems, [])

    def assertRejected(self, problems: list[str], fragment: str) -> None:
        self.assertTrue(any(fragment in problem for problem in problems), problems)


class NarrowedCheckTests(NarrowedWorld):
    """T1～T18、T23～T25、T27、T28、T33～T35：``check_record`` 的窄化分支。"""

    # -- T1～T4：内容、大小、权限 -----------------------------------------------------------------------------------

    def test_t01_changing_an_unaccessed_file_still_inherits(self) -> None:
        record = self.publish()
        self.assertAccepted(self.check(record)[0])
        self.unused.write_text("面目全非且大小也变了的、来源没有访问过的文件", encoding="utf-8")
        problems, current = self.check(record)
        self.assertAccepted(problems)
        self.assertNotEqual(current.inputs_sha256, record["inputs_sha256"], "整份摘要确实不同——旧口径会因此拒绝")

    def test_t02_changing_a_read_file_is_rejected_even_when_the_size_is_unchanged(self) -> None:
        record = self.publish()
        same_size_change(self.read)
        problems, current = self.check(record)
        self.assertEqual(current.inputs_sha256, ur.entries_sha256(current.inputs), "（对照）当前输入的整份摘要照常可算")
        self.assertRejected(problems, "文件内容变了")

    def test_t03_changing_a_stat_only_file_keeps_inheriting_unless_the_size_changes(self) -> None:
        record = self.publish()
        same_size_change(self.stat)
        self.assertAccepted(self.check(record)[0])
        self.stat.write_text("长度变了的 stat 文件内容", encoding="utf-8")
        self.assertRejected(self.check(record)[0], "大小")

    def test_t04_permission_changes_are_rejected_on_the_file_and_on_its_ancestors(self) -> None:
        record = self.publish()
        os.chmod(self.stat, 0o600)
        self.assertRejected(self.check(record)[0], "权限")
        os.chmod(self.stat, FILE_MODE)
        self.assertAccepted(self.check(record)[0])
        os.chmod(self.statdir, 0o750)                       # 祖先目录权限（只 stat 了 statdir 里的……实际是目录自身被 stat）
        self.assertRejected(self.check(record)[0], "权限")
        os.chmod(self.statdir, DIR_MODE)
        os.chmod(self.tree, 0o750)                          # 所有被访问路径共有的祖先
        self.assertRejected(self.check(record)[0], "祖先")
        os.chmod(self.tree, DIR_MODE)
        self.assertAccepted(self.check(record)[0])

    # -- T5：新增、删除文件 ----------------------------------------------------------------------------------------

    def test_t05_added_and_removed_files_only_matter_where_the_unit_looked(self) -> None:
        record = self.publish()
        with self.subTest(case="a 被枚举的目录里新增"):
            extra = write_file(self.listed / "c.txt", "丙")
            self.assertRejected(self.check(record)[0], "目录的直接条目")
            extra.unlink()
        with self.subTest(case="b 只被 stat 的目录里新增（没枚举）"):
            extra = write_file(self.statdir / "new.txt", "新")
            self.assertAccepted(self.check(record)[0])
            extra.unlink()
        with self.subTest(case="c 来源判 missing 的路径现在存在"):
            write_file(self.absent, "出现了")
            self.assertRejected(self.check(record)[0], "类型")
            with self.subTest(case="d 又被删除（净无变化）"):
                self.absent.unlink()
                self.assertAccepted(self.check(record)[0])
        with self.subTest(case="e 被枚举的目录里删除一个文件"):
            (self.listed / "a.txt").unlink()
            self.assertRejected(self.check(record)[0], "目录的直接条目")

    def test_t06_a_deleted_read_file_is_rejected(self) -> None:
        record = self.publish()
        self.read.unlink()
        self.assertRejected(self.check(record)[0], "类型")

    # -- T7～T9：符号链接、硬链接、目录改名 ------------------------------------------------------------------------

    def test_t07_symlink_changes(self) -> None:
        record = self.publish()
        with self.subTest(case="被访问的链接改指向"):
            self.alias.unlink()
            self.alias.symlink_to(self.other)
            self.assertRejected(self.check(record)[0], "符号链接")
            self.alias.unlink()
            self.alias.symlink_to(self.real)
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="被访问的链接换成普通文件"):
            self.alias.unlink()
            write_file(self.alias, "real-file-1234")
            self.assertRejected(self.check(record)[0], "符号链接")
            self.alias.unlink()
            self.alias.symlink_to(self.real)
        with self.subTest(case="没被访问的链接变化"):
            self.unused_alias.unlink()
            self.unused_alias.symlink_to(self.other)
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="途经的中间链接改指向（目标目录里文件内容相同）"):
            self.ldir.unlink()
            self.ldir.symlink_to(self.realdir2)
            self.assertRejected(self.check(record)[0], "符号链接")

    def test_t08_hard_link_guard_only_applies_to_accessed_files_under_the_tree(self) -> None:
        record = self.publish()
        with self.subTest(case="被访问的测试树普通文件 nlink=2"):
            twin = self.tree / "read_twin.txt"
            os.link(self.read, twin)
            self.assertRejected(self.check(record)[0], "硬链接")
            twin.unlink()
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="同一个文件没被访问"):
            os.link(self.unused, self.tree / "unused_twin.txt")
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="测试树之外的系统路径 nlink>1"):
            os.link(self.sys_file, self.outside / "sys_twin.txt")
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="历史重放（消费端）不查硬链接"):
            os.link(self.read, self.tree / "read_twin2.txt")
            self.assertAccepted(self.check(record, historical_read_audit=True)[0])

    def test_t09_directory_rename(self) -> None:
        record = self.publish()
        self.unlisted.rename(self.tree / "unlisted_renamed")
        self.assertAccepted(self.check(record)[0])
        self.statdir.rename(self.tree / "statdir_renamed")
        self.assertRejected(self.check(record)[0], "类型")

    def test_t10_same_name_file_to_directory_flip_is_the_known_gap(self) -> None:
        """已知缺口 R2：目录快照只有名字没有 d_type，被枚举目录里同名“文件变目录”发现不了。

        这条用例把当前行为固定为“不发现”；由第 11、17 项用 ``git diff --name-status`` 的类型变更（T）一票否决补上，不是回归。"""
        record = self.publish()
        (self.listed / "a.txt").unlink()
        make_dir(self.listed / "a.txt")
        self.assertAccepted(self.check(record)[0])

    # -- T11：字节码镜像 -------------------------------------------------------------------------------------------

    def test_t11_pyc_promotion_guard(self) -> None:
        record = self.publish()
        audit = record["read_audit"]
        promoted = ra._pyc_promotions(audit["host_path_modes"], (str(self.tree), None))
        self.assertEqual(promoted, frozenset({str(self.mod)}), "只提升测试树下、镜像字节码被读过、自己只 stat 的 .py")
        with self.subTest(case="a 测试树 .py 只 stat、镜像 pyc 被读、源内容改且大小不变"):
            same_size_change(self.mod)
            self.assertRejected(self.check(record)[0], "镜像字节码")
            same_size_change(self.mod)
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="b 测试树之外的同形路径（标准库）不提升"):
            same_size_change(self.lib)
            self.assertAccepted(self.check(record)[0])
        with self.subTest(case="c 镜像 pyc 自己的内容变了"):
            same_size_change(self.pyc)
            self.assertRejected(self.check(record)[0], "文件内容变了")
            same_size_change(self.pyc)
        with self.subTest(case="d 没有镜像 pyc 的只 stat .py 内容改"):
            same_size_change(self.nopyc)
            self.assertAccepted(self.check(record)[0])

    # -- T12、T13：其它检查不被吞掉 --------------------------------------------------------------------------------

    def test_t12_environment_change_is_still_reported(self) -> None:
        record = self.publish()
        environment = [*self.world.environment, ur.value_entry("environment", "executor:arch", "x86_64")]
        problems, _ = self.check(record, environment=environment, environment_sha256=ur.entries_sha256(environment))
        self.assertRejected(problems, "环境变了")

    def test_t13_unit_spec_change_is_still_reported(self) -> None:
        record = self.publish()
        problems, current = self.check(record, before=lambda value: setattr(value, "spec_sha256", "0" * 64))
        self.assertRejected(problems, "单元规格变了")

    # -- T14：测试树坐标守卫 ---------------------------------------------------------------------------------------

    def test_t14_the_coordinate_guard_refuses_other_trees_without_emitting_view_reasons(self) -> None:
        record = self.publish()
        self.unused.write_text("同时改一个文件，确认守卫之后不再产出视图比较的原因", encoding="utf-8")
        same_size_change(self.read)
        problems, _ = self.check(record, repo_root="/other/commit-bound/test-tree")
        self.assertRejected(problems, "测试树坐标")
        self.assertFalse([problem for problem in problems if "文件内容变了" in problem or "宿主" in problem], problems)
        same_size_change(self.read)
        self.assertAccepted(self.check(record, repo_root=str(self.tree))[0])
        self.assertAccepted(self.check(record, repo_root=None)[0], )

    def test_t14b_every_current_produced_by_gate_currents_carries_the_coordinate(self) -> None:
        """防止以后新增构造点漏填而静默绕过守卫（失败关闭）：_gate_currents 产出的每个 Current 都带 inherit_repo_root。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = base_audit.ReadAuditExecutorTests("test_audit_is_only_for_re_execute")._repo(root)
            test_file = repo / "tools/official_client_capture/tests/test_alpha.py"
            group = ue.DiscoverGroup("capture-tools", test_file.parent, "test_*.py", (), ())
            unit = ue.Unit("test_alpha", "test_alpha", ("test_alpha.AlphaTests.test_a",), ue.Quota(1, 128), False, 1)
            command = ue.Unit("cmd:one", "cmd:one", (), ue.Quota(1, 128), False, 1, command=("true",))
            manifest = {"units": [{"unit_id": "cmd:one", "inheritable": False, "not_inheritable_reason": "测试"}]}
            records = ue._records_module()
            previous = Path.cwd()
            os.chdir(repo)
            try:
                with mock.patch.object(records.RepoIndex, "load", return_value=records.RepoIndex.load(repo)):
                    currents = ue._gate_currents(records, groups=[group], group_units={group.group_id: [unit]}, command_units=[command],
                                                 manifest=manifest, timeout=120)
                expected = str(Path.cwd().resolve())
            finally:
                os.chdir(previous)
        self.assertEqual(set(currents), {"test_alpha", "cmd:one"})
        for unit_id, current in currents.items():
            with self.subTest(unit=unit_id):
                self.assertEqual(current.inherit_repo_root, expected)

    # -- T15、T16、T17：旧记录与篡改 -------------------------------------------------------------------------------

    def test_t15_old_records_without_the_new_fields_keep_whole_digest_semantics(self) -> None:
        record = self.publish()
        stripped = self.reseal(record, lambda body: [body["read_audit"].pop(key) for key in ("inherit_view_schema", "inherit_inputs_sha256")],
                               run_id="old-run")
        self.assertFalse(ur._narrowed_source(stripped))
        self.assertTrue(ur._narrowed_source(record))
        self.unused.write_text("只改来源没访问过的文件", encoding="utf-8")
        problems, _ = self.check(stripped)
        self.assertRejected(problems, "输入变了")
        problems, _ = self.check(record)
        self.assertAccepted(problems)

    def test_t16_tampering_with_the_sealed_view_fields(self) -> None:
        record = self.publish()
        with self.subTest(case="改封存的视图摘要并重封自摘要"):
            forged = self.reseal(record, lambda body: body["read_audit"].__setitem__("inherit_inputs_sha256", "a" * 64), run_id="forged-digest")
            self.assertRejected(self.check(forged)[0], "承接视图摘要")
        with self.subTest(case="视图版本串改成未知值：走旧路径（整份比较）"):
            unknown = self.reseal(record, lambda body: body["read_audit"].__setitem__("inherit_view_schema", "unit-inherit-view/v9"),
                                  run_id="unknown-schema")
            self.assertFalse(ur._narrowed_source(unknown))
            self.assertAccepted(self.check(unknown)[0])
            self.unused.write_text("来源没访问过的文件", encoding="utf-8")
            self.assertRejected(self.check(unknown)[0], "输入变了")
            self.assertAccepted(self.check(record)[0])
            self.unused.write_text("unused-12345678", encoding="utf-8")
        with self.subTest(case="删掉字段：走旧路径"):
            deleted = self.reseal(record, lambda body: body["read_audit"].pop("inherit_inputs_sha256"), run_id="deleted-digest")
            self.assertFalse(ur._narrowed_source(deleted))
        with self.subTest(case="改封存的访问类型：既有交叉核对报错"):
            modes = copy.deepcopy(record["read_audit"]["host_path_modes"])
            modes[str(self.read)] = ["stat"]
            widened = self.reseal(record, lambda body: body["read_audit"].__setitem__("host_path_modes", modes), run_id="forged-modes")
            self.assertRejected(self.check(widened)[0], "访问类型")
        with self.subTest(case="摘要格式不是 64 位十六进制"):
            short = self.reseal(record, lambda body: body["read_audit"].__setitem__("inherit_inputs_sha256", "abc"), run_id="short-digest")
            self.assertFalse(ur._narrowed_source(short))

    def test_t17_inputs_not_matching_the_registered_digest_are_rejected(self) -> None:
        record = self.publish()

        def damage(body: dict) -> None:
            entry = next(item for item in body["inputs"] if item["name"] == "runtime-test-spec")
            entry["sha256"] = "b" * 64                       # 明细被改，inputs_sha256 与 read_audit.inputs_sha256 保持旧值

        forged = self.reseal(record, damage, run_id="forged-inputs")
        self.assertRejected(self.check(forged)[0], "输入明细与登记的输入摘要不符")

    def test_t18_repo_declarations_are_excluded_but_unknown_non_host_entries_are_not(self) -> None:
        record = self.publish()
        problems, _ = self.check(record, nonhost=self.nonhost(docs="d2", head="H2"))
        self.assertAccepted(problems)
        self.assertRejected(self.check(record, nonhost=self.nonhost(spec="S2"))[0], "非宿主输入变了")
        extra_inputs = [*self.record_inputs(), ur.value_entry("environment", "mystery-input", "m1")]
        unknown = self.publish(run_id="unknown-name", inputs=extra_inputs)
        self.assertAccepted(self.check(unknown, nonhost=self.nonhost(extra={"mystery-input": "m1"}))[0])
        self.assertRejected(self.check(unknown, nonhost=self.nonhost(extra={"mystery-input": "m2"}))[0], "非宿主输入变了")
        self.assertRejected(self.check(unknown)[0], "当前输入没有这个条目")
        old = self.reseal(record, lambda body: [body["read_audit"].pop(key) for key in ("inherit_view_schema", "inherit_inputs_sha256")],
                          run_id="old-for-t18")
        self.assertRejected(self.check(old, nonhost=self.nonhost(docs="d2", head="H2"))[0], "输入变了")

    # -- T23～T25：补跑记录、消费端摘要、读集要求不成立 --------------------------------------------------------------

    def test_t23_retry_records_are_refused_before_any_narrowed_work(self) -> None:
        record = self.publish(run_id="retry-run", attempt="retry", inheritable=False)
        current = self.world.current(self.UNIT, inputs=self.nonhost())
        ui._carry_host_inputs(current, record)
        current.inherit_repo_root = str(self.tree)
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        quick = ur.check_record(self.world.store, path, record, current, self.world.facts(fixture.T0), thorough=False)
        self.assertEqual(quick, ["来源记录明确不可承接"], "便宜的核对已经不通过：不进入贵的窄化核对")
        self.assertRejected(ur.check_record(self.world.store, path, record, current, self.world.facts(fixture.T0)), "来源记录明确不可承接")

    def test_t24_the_consumer_digest_must_match_the_record(self) -> None:
        record = self.publish()
        audit = record["read_audit"]
        good = ra.inherit_current_digest(record, self.check(record)[1].inputs)
        self.assertEqual(good, audit["inherit_inputs_sha256"])
        for digest, expected in ((good, None), ("0" * 64, "清单登记的当前承接视图摘要")):
            with self.subTest(digest=digest[:8]):
                problems, _ = self.check(record, historical_read_audit=True, before=lambda value: setattr(value, "inherit_inputs_sha256", digest))
                if expected is None:
                    self.assertAccepted(problems)
                else:
                    self.assertRejected(problems, expected)

    def test_t25_without_the_read_audit_requirement_the_old_path_applies(self) -> None:
        record = self.publish()
        current = self.world.current(self.UNIT, inputs=self.nonhost())          # 没沿用宿主快照，也没有 require-read-audit
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        problems = ur.check_record(self.world.store, path, record, current, self.world.facts(fixture.T0))
        self.assertRejected(problems, "输入变了")
        self.assertFalse([problem for problem in problems if "宿主" in problem or "承接视图" in problem], problems)

    # -- T27、T28：现场漂移、条目集合 ------------------------------------------------------------------------------

    def test_t27_live_drift_of_accessed_entries_is_caught_but_not_of_unaccessed_ones(self) -> None:
        record = self.publish()
        problems, _ = self.check(record, before=lambda value: self.unused.write_text("快照之后才变的、没被访问的文件", encoding="utf-8"))
        self.assertAccepted(problems)
        problems, _ = self.check(record, before=lambda value: same_size_change(self.read))
        self.assertRejected(problems, "宿主输入快照漂移")

    def test_t28_accessed_entries_must_exist_in_current_but_extra_current_entries_are_ignored(self) -> None:
        record = self.publish()
        problems, _ = self.check(record, before=lambda value: setattr(
            value, "inputs", [entry for entry in value.inputs if entry["name"] != f"host:{self.read}"]))
        self.assertRejected(problems, "当前输入没有这个被访问过的宿主条目")
        extra = [ra.host_snapshot_v3(str(self.other)), ur.value_entry("docs", "repo:rest", "旧口径范围项")]
        problems, _ = self.check(record, before=lambda value: setattr(value, "inputs", [*value.inputs, *extra]))
        self.assertAccepted(problems)

    # -- T33～T35 ---------------------------------------------------------------------------------------------------

    def test_t33_records_without_v3_host_entries_are_not_narrowed(self) -> None:
        plain = self.world.records[self.UNIT]                                      # World 默认记录：v2 宿主条目、没有新字段
        self.assertFalse(ur._narrowed_source(plain))
        empty = copy.deepcopy(self.publish(run_id="empty-modes"))
        empty["read_audit"]["host_path_modes"] = {}
        self.assertFalse(ur._narrowed_source(empty), "访问类型为空＝没有 v3 宿主条目，不判为可窄化")
        self.assertFalse(ur._narrowed_source(None))
        self.assertFalse(ur._narrowed_source({"read_audit": "不是对象"}))

    def test_t34_an_incomplete_trace_is_refused_rather_than_relaxed(self) -> None:
        document, digest = self.put_trace(self.trace_lines())
        broken = copy.deepcopy(document)
        broken["coverage"]["unresolved"] = 1
        path = self.world.root / "broken-trace.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        broken_digest = ur.file_sha256(path)
        self.world.store.put_log(path, broken_digest)
        inputs = self.record_inputs()
        block = self.audit_block(inputs, document, digest, trace={"sha256": broken_digest})
        record = self.publish(run_id="broken-run", inputs=inputs, block=block)
        problems, _ = self.check(record)
        self.assertTrue(problems, "轨迹不完整：访问方式不可信，必须拒绝")
        self.assertRejected(problems, "不能在来源自己的输入上重放")

    def test_t35_evaluate_prefers_the_newest_inheritable_candidate_whatever_its_format(self) -> None:
        old = self.publish(run_id="mix-old", completed=fixture.T0 - 7200)
        same_size_change(self.read)
        new = self.publish(run_id="mix-new", completed=fixture.T0 - 3600)          # 在新状态下封存的窄化记录
        current = self.world.current(self.UNIT, inputs=list(self.nonhost()))
        ui._carry_host_inputs(current, new)
        current.inherit_repo_root = str(self.tree)
        facts = self.world.facts(fixture.T0)
        decision = ur.evaluate(self.world.store, current, facts)
        self.assertEqual(decision.record["record_sha256"], new["record_sha256"], "两条都在库里：最新且可承接的优先，旧状态那条因读文件内容不同被拒")
        same_size_change(self.read)                                                  # 回到旧状态：只有较早那条可承接
        current = self.world.current(self.UNIT, inputs=list(self.nonhost()))
        ui._carry_host_inputs(current, old)
        current.inherit_repo_root = str(self.tree)
        decision = ur.evaluate(self.world.store, current, facts)
        self.assertEqual(decision.record["record_sha256"], old["record_sha256"])
        # 再放一条更新的旧口径记录（世界默认记录，整份比较）：它与当前输入不同，不能压过较早但可承接的窄化记录
        legacy = self.world.source_record(self.UNIT, run_id="mix-legacy", completed=fixture.T0 - 60)
        self.world.publish_run("mix-legacy", "re-execute", {self.UNIT: (legacy, "executed")}, decided=fixture.T0 - 3600)
        decision = ur.evaluate(self.world.store, current, facts)
        self.assertEqual(decision.record["record_sha256"], old["record_sha256"])


class WalkExemptionTests(NarrowedWorld):
    """B2 比对侧豁免：整树遍历签名下，受管根测试目录里只被 stat 的文件，视图忽略它的 ``core.size``（其余照比）。

    签名＝来源记录里该测试目录被枚举，且目录下“只 stat”的直接子文件不少于 50 个、占目录项 80% 以上（工具身份遍历 ``rglob`` 整棵受管树的形状）。
    零星 stat 个别测试文件的单元不满足签名，不豁免；用例用 60 个测试文件造出两种形状。"""

    FILES = 60
    STAT_ONLY = 60

    def setUp(self) -> None:
        super().setUp()
        self.tests_dir = make_dir(self.tree / "tools" / "official_client_capture" / "tests")
        self.test_files = [write_file(self.tests_dir / f"test_{index:03d}.py", f"# 测试文件 {index:03d}\n") for index in range(self.FILES)]

    def host_paths(self) -> list[Path]:
        return [*super().host_paths(), self.tests_dir, *self.test_files]

    def trace_lines(self) -> list[str]:
        cwd = f"AT_FDCWD<{self.tree}>"
        lines = super().trace_lines()
        lines.append(f'1 newfstatat({cwd}, "{self.tests_dir}", 0x1, 0) = 0')
        lines.append(f'1 getdents64(3<{self.tests_dir}>, 0x1 /* {self.FILES} entries */, 32768) = 80')
        lines += [f'1 newfstatat({cwd}, "{path}", 0x1, 0) = 0' for path in self.test_files[:self.STAT_ONLY]]
        return lines

    def signature(self, record: dict) -> frozenset:
        return ra.walk_stat_directories(record["inputs"], record["read_audit"]["host_path_modes"], (str(self.tree), None))


class WalkSignatureTests(WalkExemptionTests):
    def test_b2_whole_directory_walk_exempts_the_size_of_stat_only_test_files(self) -> None:
        record = self.publish()
        self.assertEqual(self.signature(record), frozenset({str(self.tests_dir)}))
        self.assertAccepted(self.check(record)[0])
        self.test_files[7].write_text("# 大小变了的测试文件，内容也不同\n" * 3, encoding="utf-8")
        problems, current = self.check(record)
        self.assertAccepted(problems)
        self.assertNotEqual(current.inputs_sha256, record["inputs_sha256"], "整份摘要早就不同——旧口径会因此拒绝")
        consumer, _ = self.check(record, historical_read_audit=True, before=lambda value: setattr(
            value, "inherit_inputs_sha256", ra.inherit_current_digest(record, value.inputs)))
        self.assertAccepted(consumer)

    def test_b2_everything_else_about_those_files_is_still_compared(self) -> None:
        record = self.publish()
        with self.subTest(case="权限变了"):
            os.chmod(self.test_files[3], 0o600)
            self.assertRejected(self.check(record)[0], "权限")
            os.chmod(self.test_files[3], FILE_MODE)
        with self.subTest(case="被删除（目录项变了）"):
            self.test_files[4].unlink()
            self.assertRejected(self.check(record)[0], "目录的直接条目")
            write_file(self.test_files[4], "# 测试文件 004\n")
        with self.subTest(case="新增文件（目录被枚举）"):
            extra = write_file(self.tests_dir / "test_new.py", "# 新增\n")
            self.assertRejected(self.check(record)[0], "目录的直接条目")
            extra.unlink()
        with self.subTest(case="文件换成符号链接"):
            self.test_files[5].unlink()
            self.test_files[5].symlink_to(self.other)
            self.assertRejected(self.check(record)[0], "符号链接")

    def test_b2_the_exemption_does_not_leak_to_other_files(self) -> None:
        record = self.publish()
        self.stat.write_text("大小变了的、不在测试目录里的 stat 文件", encoding="utf-8")
        self.assertRejected(self.check(record)[0], "大小")

    def test_b2_the_tampered_exempt_digest_is_caught(self) -> None:
        record = self.publish()
        forged = self.reseal(record, lambda body: body["read_audit"].__setitem__("inherit_inputs_sha256", "d" * 64), run_id="forged-walk")
        self.assertRejected(self.check(forged)[0], "承接视图摘要")


class SparseStatTests(WalkExemptionTests):
    """零星 stat 个别测试文件（20 个，不到 80%，也不到 50 个）：不满足签名，大小照比。"""

    STAT_ONLY = 20

    def test_b2_sparse_stat_only_units_get_no_exemption(self) -> None:
        record = self.publish()
        self.assertEqual(self.signature(record), frozenset())
        self.test_files[7].write_text("# 大小变了的测试文件，内容也不同\n" * 3, encoding="utf-8")
        self.assertRejected(self.check(record)[0], "大小")


class ViewPropertyTests(NarrowedWorld):
    """T19 防分叉、T31～T32、T36。"""

    def test_t19_view_digest_equality_is_equivalent_to_an_empty_problem_list(self) -> None:
        """固定种子 ≥200 次随机变异：视图摘要相等 ⇔ ``narrowed_inherit_problems(live=False)`` 为空，覆盖 kind／target／links／
        ancestors／core／内容／目录项／声明字段／未访问条目。变异后的条目用真实构造函数重建（自洽复核通过），不碰宿主。"""
        record = self.publish()
        document = json.loads(self.world.store.log_path(record["read_audit"]["trace"]["sha256"]).read_text())
        audit = record["read_audit"]
        inputs = record["inputs"]
        modes = audit["host_path_modes"]
        roots = (str(self.tree), None)
        source = ra.inherit_view(inputs, modes, roots=roots)
        source_digest = ra.inherit_view_digest(source)
        host_indices = [index for index, entry in enumerate(inputs) if entry["name"].startswith("host:")]
        rng = random.Random(20261025)

        def mutate(entry: dict, how: str) -> dict:
            snapshot = copy.deepcopy(entry["detail"]["snapshot"])
            dependency = entry["detail"].get("metadata_dependency")
            if how == "content" and snapshot["kind"] == "file":
                snapshot["content_sha256"] = hashlib.sha256(os.urandom(8)).hexdigest()
            elif how == "entries" and snapshot["kind"] == "directory":
                snapshot["entries"] = sorted([*snapshot["entries"], f"n{rng.randrange(10 ** 6)}"])
            elif how == "core" and snapshot["kind"] != "missing":
                snapshot["core"]["mode"] ^= 0o111
            elif how == "size" and snapshot["kind"] == "file":
                snapshot["core"]["size"] += rng.randrange(1, 9)
            elif how == "ancestors" and snapshot["ancestors"]:
                snapshot["ancestors"][-1]["mode"] ^= 0o200
            elif how == "links":
                snapshot["links"] = [{"path": entry["detail"]["path"], "target": f"/elsewhere{rng.randrange(99)}", "mode": 41471,
                                      "uid": 0, "gid": 0, "size": 9}]
            elif how == "target":
                snapshot["target"] = f"/moved{rng.randrange(99)}" + snapshot["target"]
            elif how == "kind":
                snapshot = {"kind": "missing", "target": snapshot["target"], "links": snapshot["links"], "ancestors": snapshot["ancestors"]}
            elif how == "volatile" and snapshot["kind"] != "missing":
                snapshot["volatile"]["mtime_ns"] += 1
            elif how == "dependency" and snapshot["kind"] != "missing":
                dependency = {"fields": ["mtime_ns"], "basis": "测试里的审核依据"}
            return ra.host_entry_v3(entry["detail"]["path"], snapshot, dependency)

        kinds = ["content", "entries", "core", "size", "ancestors", "links", "target", "kind", "volatile", "dependency", "none"]
        equal_cases = unequal_cases = 0
        for round_number in range(240):
            current = copy.deepcopy(inputs)
            for _ in range(rng.randrange(1, 4)):
                index = rng.choice(host_indices)
                current[index] = mutate(current[index], rng.choice(kinds))
            if rng.random() < 0.3:
                current.append(ur.value_entry("docs", "repo:docs", f"范围项{round_number}"))
            cur_view = ra.inherit_view(current, modes, roots=roots, only=set(source))
            digest_equal = ra.inherit_view_digest(cur_view) == source_digest and set(cur_view) == set(source)
            problems = ra.narrowed_inherit_problems(document, inputs, current, audit, live=False)
            with self.subTest(round=round_number):
                self.assertEqual(digest_equal, not problems, problems)
            equal_cases += digest_equal
            unequal_cases += not digest_equal
        self.assertGreater(equal_cases, 20, "随机变异里要有足够多的“摘要相等”样本")
        self.assertGreater(unequal_cases, 20, "也要有足够多的“摘要不等”样本")

    def test_t31_the_schema_string_is_equal_in_both_files(self) -> None:
        """两个文件互不导入，各自写了一份视图版本串：测试固定它们相等（仿 parser／replay v4 limits 的做法）。"""
        self.assertEqual(ur._INHERIT_VIEW_SCHEMA, ra.INHERIT_VIEW_SCHEMA)
        self.assertEqual(ra.INHERIT_VIEW_SCHEMA, "unit-inherit-view/v1")

    def test_t32_driver_copies_are_byte_identical_for_the_touched_files(self) -> None:
        repo = Path(ra.__file__).resolve().parents[2]
        for name in ("read_audit.py", "unit_records.py", "unit_executor.py", "unit_inherit.py", "target_platform_gate.py"):
            with self.subTest(name=name):
                self.assertEqual((repo / "tools/ci" / name).read_bytes(), (repo / "tools/arm64_capture_driver/driver" / name).read_bytes())

    def test_t36_building_the_view_for_thousands_of_entries_is_recorded_not_bounded(self) -> None:
        """3,538 条合成条目（与真实实例同量级）的视图构造耗时只记录、不卡阈值（避免第 7 项会拦的绝对时间阈值）。"""
        template = ra.host_snapshot_v3(str(self.read))
        entries, modes = [], {}
        for index in range(3538):
            path = f"/synthetic/path/{index}.txt"
            snapshot = copy.deepcopy(template["detail"]["snapshot"])
            snapshot["target"] = path
            entries.append(ra.host_entry_v3(path, snapshot))
            modes[path] = ["read"] if index % 5 == 0 else []
        started = time.perf_counter()
        view = ra.inherit_view(entries, modes, roots=("/synthetic",))
        digest = ra.inherit_view_digest(view)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(view), 3538 // 5 + 1)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        print(f"[T36] 3538 条合成条目的视图构造 {elapsed * 1000:.1f} 毫秒（视图 {len(view)} 条）")


class ExecutorManifestTests(NarrowedWorld):
    """执行器真实的 ``_unit_manifest``：窄化承接项带 inherit_check（当前侧视图摘要与测试树坐标），旧口径承接项不带。"""

    def manifest_entry(self, record: dict, current: ur.Current) -> dict:
        unit = ue.Unit(self.UNIT, "test_alpha", ("test_alpha.AlphaTests.test_a",), ue.Quota(1, 128), False, 1)
        path = self.world.store.record_path(self.UNIT, record["record_sha256"])
        decision = ur.Decision(record, path, [])
        outcome = ue._inherited_outcome(unit, decision, self.world.store)
        manifest = ue._unit_manifest(
            ur, run_id="exec-manifest", mode=ur.UNIT_INHERIT, max_age=168.0, store=self.world.store, out_dir=self.world.root / "out",
            decided_at=fixture.stamp(fixture.T0), policy=self.world.policy, environment=self.world.environment, executor=self.world.executor,
            units=[unit], gates=[], group_units={}, expected={}, currents={self.UNIT: current}, decisions={self.UNIT: decision},
            outcomes=[outcome], diagnostic=[])
        return manifest["units"][0]

    def test_narrowed_inherited_entries_carry_the_current_side_view_digest_and_the_coordinate(self) -> None:
        record = self.publish()
        problems, current = self.check(record)
        self.assertAccepted(problems)
        entry = self.manifest_entry(record, current)
        self.assertEqual(entry["disposition"], "inherited")
        self.assertEqual(entry["inherit_check"], {"schema_version": ra.INHERIT_VIEW_SCHEMA, "repo_root": str(self.tree),
                                                  "inherit_inputs_sha256": ra.inherit_current_digest(record, current.inputs)})
        self.assertEqual(entry["inherit_check"]["inherit_inputs_sha256"], record["read_audit"]["inherit_inputs_sha256"])

    def test_old_format_inherited_entries_have_no_inherit_check(self) -> None:
        legacy = self.world.records[self.UNIT]
        current = self.world.current(self.UNIT)
        self.assertNotIn("inherit_check", self.manifest_entry(legacy, current))


class ExecutorWritesTests(NarrowedWorld):
    """T26：执行器写入——严格闭合单元封存的视图摘要等于重算值；v2／control 单元的记录键集合与改动前完全相同。"""

    def collect(self, inputs: list[dict], lines: list[str]) -> dict:
        auditor = ue.ReadAuditor(out_dir=self.world.root / "audit-out", repo_root=self.tree, data_root=None, currents={}, strict=True)
        current = ur.Current(self.UNIT, "test", self.world.spec(self.UNIT), "s" * 64, inputs, ur.entries_sha256(inputs), True)
        auditor.currents = {self.UNIT: current}
        unit = ue.Unit(self.UNIT, "test_alpha", ("test_alpha.AlphaTests.test_a",), ue.Quota(1, 128), False, 1)
        document = ra.filter_stream(lines, ["/"], strict=True)
        auditor.trace_path(unit).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return auditor.collect(unit)

    def test_t26_strict_closed_v3_units_seal_the_view_digest(self) -> None:
        inputs = self.record_inputs()
        result = self.collect(inputs, self.trace_lines())
        self.assertTrue(result["coverage_complete"], result.get("undeclared"))
        modes = result["host_path_modes"]
        self.assertEqual(result["inherit_view_schema"], ra.INHERIT_VIEW_SCHEMA)
        self.assertEqual(result["inherit_inputs_sha256"],
                         ra.inherit_view_digest(ra.inherit_view(inputs, modes, roots=(str(self.tree), None))))

    def test_t26_v2_units_keep_the_old_record_keys(self) -> None:
        inputs = [*self.nonhost(), ra.host_snapshot(str(self.read)), ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")]
        result = self.collect(inputs, [f'1 openat(AT_FDCWD<{self.tree}>, "{self.read}", O_RDONLY) = 3<{self.read}>'])
        self.assertTrue(result["coverage_complete"], result.get("undeclared"))
        self.assertNotIn("host_path_modes", result)
        self.assertNotIn("inherit_view_schema", result)
        self.assertNotIn("inherit_inputs_sha256", result)
        self.assertFalse(ur._narrowed_source({"read_audit": result}))

    def test_t22_control_chain_is_untouched(self) -> None:
        """control 四链 sidecar 复用本模块的函数对象：既有函数签名不变，control 记录不带窄化字段，窄化路径不会选中它。"""
        self.assertEqual(inspect.signature(control_audit.strict_audit_reads), inspect.signature(ra.strict_audit_reads))
        self.assertEqual(inspect.signature(control_audit.host_snapshot), inspect.signature(ra.host_snapshot))
        self.assertEqual(control_audit.HOST_SCHEMA, "read-audit-host-snapshot/v2")
        v2 = control_audit.host_snapshot(str(self.read))
        self.assertEqual(ra.host_path_modes({}, [v2]), {}, "纯 v2 条目没有访问类型要封存")
        self.assertFalse(ur._narrowed_source({"read_audit": {"coverage_complete": True, "host_path_modes": {}}}))


class ManifestAndConsumerTests(NarrowedWorld):
    """T20、T21、T29、T30：清单承接项的 inherit_check、消费端不碰宿主、propose 不改 Current、目标门禁跳过分支。"""

    def setUp(self) -> None:
        super().setUp()
        self.record = self.publish(run_id="manifest-source", completed=fixture.T0 - 60)       # 比 World 自带的旧口径记录新

    def inherited_manifest(self, *, check: dict | None = "auto", entry_inputs_sha256: str | None = None) -> tuple[dict, ur.RecordStore]:
        """一份只含一个窄化承接项的 unit-inherit 清单（不走决策收据，直接调 verify_manifest 的承接项分支）。"""
        current = self.check(self.record)[1]
        row = self.world.entry(self.record, "inherited")
        row["inputs_sha256"] = entry_inputs_sha256 or current.inputs_sha256
        if check == "auto":
            check = {"schema_version": ra.INHERIT_VIEW_SCHEMA,
                     "inherit_inputs_sha256": ra.inherit_current_digest(self.record, current.inputs), "repo_root": str(self.tree)}
        if check is not None:
            row["inherit_check"] = check
        manifest = ur.build_manifest(
            run_id="manifest-consumer", mode=ur.UNIT_INHERIT, inheritance_max_age_hours=168.0, record_store=str(self.world.store.root),
            out_dir="/out", decided_at_utc=fixture.stamp(fixture.T0), completed_at_utc=fixture.stamp(fixture.T0),
            policy_sha256=self.world.policy, environment=self.world.environment,
            environment_sha256=ur.entries_sha256(self.world.environment), executor=self.world.executor,
            planned_units=[self.UNIT], units=[row], diagnostic=[], test_groups={}, counts={})
        return manifest, self.world.store

    def narrowed_problems(self, manifest: dict, store, historical: bool = True) -> list[str]:
        """verify_manifest 里只看承接项核验与输入摘要两类问题（决策收据等不在本测试范围）。"""
        return [problem for problem in ur.verify_manifest(manifest, store=store, historical=historical)
                if "决策收据" not in problem and "测试组" not in problem]

    def test_t20_verify_manifest_binds_current_to_source_through_inherit_check(self) -> None:
        self.unused.write_text("来源没访问过的文件变了：当前侧整份输入摘要因此不再等于记录的", encoding="utf-8")
        manifest, store = self.inherited_manifest()
        self.assertEqual(self.narrowed_problems(manifest, store), [], "正确的 inherit_check：没有问题，且整份输入摘要不等也不报警")
        self.assertNotEqual(manifest["units"][0]["inputs_sha256"], self.record["inputs_sha256"])
        for label, check, fragment in (
                ("缺 inherit_check", None, "inherit_check"),
                ("摘要不符", {"schema_version": ra.INHERIT_VIEW_SCHEMA, "inherit_inputs_sha256": "c" * 64, "repo_root": str(self.tree)}, "清单登记的当前承接视图摘要"),
                ("坐标不符", {"schema_version": ra.INHERIT_VIEW_SCHEMA, "inherit_inputs_sha256": self.record["read_audit"]["inherit_inputs_sha256"],
                              "repo_root": "/other/tree"}, "测试树坐标"),
                ("版本串不符", {"schema_version": "unit-inherit-view/v9", "inherit_inputs_sha256": self.record["read_audit"]["inherit_inputs_sha256"],
                               "repo_root": str(self.tree)}, "inherit_check"),
                ("多余键", {"schema_version": ra.INHERIT_VIEW_SCHEMA, "inherit_inputs_sha256": self.record["read_audit"]["inherit_inputs_sha256"],
                           "repo_root": str(self.tree), "extra": 1}, "inherit_check")):
            with self.subTest(case=label):
                bad, store = self.inherited_manifest(check=check)
                self.assertRejected(self.narrowed_problems(bad, store), fragment)

    def test_t20_old_record_inherited_entries_still_require_equal_digests_and_executed_entries_are_unaffected(self) -> None:
        legacy = self.world.records[self.UNIT]
        row = self.world.entry(legacy, "inherited")
        row["inputs_sha256"] = "d" * 64
        manifest = ur.build_manifest(
            run_id="legacy-consumer", mode=ur.UNIT_INHERIT, inheritance_max_age_hours=168.0, record_store=str(self.world.store.root),
            out_dir="/out", decided_at_utc=fixture.stamp(fixture.T0), completed_at_utc=fixture.stamp(fixture.T0),
            policy_sha256=self.world.policy, environment=self.world.environment,
            environment_sha256=ur.entries_sha256(self.world.environment), executor=self.world.executor,
            planned_units=[self.UNIT], units=[row], diagnostic=[], test_groups={}, counts={})
        self.assertRejected(self.narrowed_problems(manifest, self.world.store), "记录的单元规格或输入摘要与清单不符")
        executed = self.world.entry(self.record, "executed")
        self.assertNotIn("inherit_check", executed)

    def test_t21_the_consumer_never_touches_the_host(self) -> None:
        """历史重放的消费端不能重新快照：把现场取快照的函数换成抛异常的桩，窄化承接项仍通过；清单里的摘要被改则拒绝。"""
        manifest, store = self.inherited_manifest()
        bad, _ = self.inherited_manifest(check={"schema_version": ra.INHERIT_VIEW_SCHEMA, "inherit_inputs_sha256": "e" * 64,
                                                "repo_root": str(self.tree)})
        audit = ur._audit_module()

        def forbidden(*args, **kwargs):
            raise AssertionError("历史重放不得读取宿主")

        with mock.patch.object(audit, "host_snapshot_v3", forbidden), mock.patch.object(audit, "host_snapshot", forbidden):
            self.assertEqual(self.narrowed_problems(manifest, store), [])
            self.assertRejected(self.narrowed_problems(bad, store), "清单登记的当前承接视图摘要")

    def test_t29_proposing_never_mutates_the_original_current(self) -> None:
        """提案只在试验副本上沿用宿主快照（``_propose_current`` 的 ``trial = replace(...)``），原 Current 不被改动。"""
        current = self.world.current(self.UNIT, inputs=list(self.nonhost()))
        current.inherit_repo_root = str(self.tree)
        before = (list(current.inputs), current.inputs_sha256)
        proposal = ui.propose(self.world.store.root, {self.UNIT: current}, self.world.facts(fixture.T0), client="codex")
        self.assertEqual((current.inputs, current.inputs_sha256), before)
        self.assertEqual(proposal["units"], {self.UNIT: {"run_id": "manifest-source", "record_sha256": self.record["record_sha256"]}})

    def test_t30_target_platform_gate_skips_the_whole_digest_comparison_only_for_narrowed_records(self) -> None:
        from tools.official_client_capture.tests import test_ci_target_platform_gate as gate_tests

        case = gate_tests.TargetPlatformGateTests("test_spec_input_and_new_units_are_differences_not_reused")
        case.setUp()
        try:
            records = target.fs.records_module()
            case.currents["b"].inputs_sha256 = "0" * 64
            verdict, decisions = case.assess()
            self.assertFalse(decisions["b"].inherit, "旧口径记录：整份输入摘要不同仍然完整执行")
            with mock.patch.object(records, "_narrowed_source", return_value=True), \
                    mock.patch.object(records, "check_record", return_value=[]):
                verdict, decisions = case.assess()
                self.assertTrue(decisions["b"].inherit, verdict)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
