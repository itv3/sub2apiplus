"""读集审计（E3-04，``tools/ci/read_audit.py`` 与执行器 ``run-gates --audit-reads``）。

* 解析：用 ARM64（strace 6.8）实验轨迹的真实行——``-y`` 标注的 dirfd 还原相对路径、chdir 之后的相对路径、clone 与
  execve 的 unfinished／resumed、ENOENT 探测、八进制转义的中文路径、两路径的 renameat2、AT_EMPTY_PATH、字节码缓存；
* 覆盖：范围项、文件项、上级目录元数据、HEAD 与只读提交号模块的 .git 例外、数据根同布局与豁免、已算明细的绝对路径；
* 执行器：PATH 里放假 strace（按单元写合成轨迹并经 ``-o '|…'`` 管道交给过滤器），真跑 run-gates，未声明读取逐条报出、
  整次判失败，声明齐全时通过；审计失败同步写入执行记录，禁止后续误承接；只许 re-execute。
* 真 strace：Linux 上有 strace 且本进程没被跟踪时，跑一次真实轨迹（入口门禁带审计跑全集时本测试已在 strace 下，跳过）。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

from tools.ci import read_audit as ra
from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
R = "/root/vc-rounds/entry-refactor/e3-04-probe"
# ARM64 实验轨迹的真实行（strace 6.8，-f -qq --seccomp-bpf -y），后几行按同一格式补了写入、中文路径与两路径调用。
PROBE_LINES = f"""535302 execve("/usr/bin/python3", ["python3", "-B", "-c", "import os"...], 0xffffd7b0b308 /* 17 vars */) = 0
535302 newfstatat(AT_FDCWD<{R}>, "pkg/sub", {{st_mode=S_IFDIR|0755, st_size=4096, ...}}, 0) = 0
535302 getdents64(3<{R}/pkg/sub>, 0x1061e880 /* 3 entries */, 32768) = 80
535302 openat(AT_FDCWD<{R}>, "{R}/pkg/sub/__pycache__/mod.cpython-312.pyc", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
535302 openat(AT_FDCWD<{R}>, "{R}/pkg/sub/mod.py", O_RDONLY|O_CLOEXEC) = 3<{R}/pkg/sub/mod.py>
535302 chdir("pkg")                     = 0
535302 newfstatat(AT_FDCWD<{R}/pkg>, "nope.txt", 0xffffc6aae940, 0) = -1 ENOENT (No such file or directory)
535302 clone(child_stack=0xffffc6aae230, flags=CLONE_VM|CLONE_VFORK|SIGCHLD <unfinished ...>
535311 execve("/usr/local/sbin/cat", ["cat", "sub/mod.py"], 0xffffc6aaef60 /* 17 vars */) = -1 ENOENT (No such file or directory)
535311 execve("/usr/bin/cat", ["cat", "sub/mod.py"], 0xffffc6aaef60 /* 17 vars */ <unfinished ...>
535302 <... clone resumed>)             = 535311
535311 <... execve resumed>)            = 0
535311 openat(AT_FDCWD<{R}/pkg>, "sub/mod.py", O_RDONLY) = 3<{R}/pkg/sub/mod.py>
535311 openat(AT_FDCWD<{R}/pkg>, "out/\\344\\270\\255.txt", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3<{R}/pkg/out/中.txt>
535311 newfstatat(3<{R}/pkg/sub/mod.py>, "", {{st_mode=S_IFREG|0644, st_size=6, ...}}, AT_EMPTY_PATH) = 0
535311 renameat2(AT_FDCWD<{R}/pkg>, "a.tmp", AT_FDCWD<{R}/pkg>, "a.json", RENAME_NOREPLACE) = 0
535311 exit_group(0)                    = ?
535302 <... wait4 resumed>[{{WIFEXITED(s) && WEXITSTATUS(s) == 0}}], 0, NULL) = 535311
535302 --- SIGCHLD {{si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=535311, si_uid=0, si_status=0}} ---
"""


def _range(name: str, include: list[str], exclude: list[str] | None = None) -> dict:
    return {"category": "tests", "name": name, "sha256": "0" * 64, "detail": {"include": include, "exclude": exclude or [], "files": 1}}


def _file(relative: str) -> dict:
    return {"category": "tests", "name": f"file:{relative}", "sha256": "1" * 64, "detail": {"path": relative}}


class ReadAuditParserTests(unittest.TestCase):
    def test_real_strace_lines_resolve_paths_and_kinds(self) -> None:
        document = ra.filter_stream(PROBE_LINES.splitlines(True), [R])
        accesses = {path.replace(R, "R"): kinds for path, kinds in document["accesses"]}
        self.assertEqual(accesses, {
            "R/pkg": ["dir"],                    # chdir("pkg")：按进程最近的工作目录还原，只算目录元数据
            "R/pkg/a.json": ["write"], "R/pkg/a.tmp": ["write"],  # renameat2 两个路径都记
            "R/pkg/nope.txt": ["missing"],       # ENOENT：探测不存在的路径
            "R/pkg/out/中.txt": ["write"],       # 八进制转义还原成中文
            "R/pkg/sub": ["list", "stat"],       # stat 成功与 getdents64：元数据
            "R/pkg/sub/mod.py": ["read"],        # 绝对路径、chdir 后的相对路径、子进程的读取合并
        })
        self.assertNotIn("R/pkg/sub/__pycache__/mod.cpython-312.pyc", accesses, "字节码缓存不是输入")
        self.assertEqual(document["lines"], len(PROBE_LINES.splitlines()))
        self.assertIn("nope.txt", document["samples"][f"{R}/pkg/nope.txt"])
        self.assertEqual(ra.filter_stream(PROBE_LINES.splitlines(True), ["/elsewhere"])["accesses"], [], "根之外的路径不留")

    def test_unescape_and_result_parsing(self) -> None:
        self.assertEqual(ra.unescape('a\\"b\\\\c\\n\\x41\\101'), 'a"b\\c\nAA')
        parser = ra.TraceParser()
        parser.feed('7 openat(AT_FDCWD</r>, "x) = 3", O_RDONLY) = -1 ENOENT (No such file or directory)\n')
        self.assertEqual(parser.accesses, {"/r/x) = 3": {"missing"}}, "参数里像返回值的字样不影响判定")
        parser.feed('7 openat(AT_FDCWD</r>, "d", O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_DIRECTORY) = 3</r/d>\n')
        parser.feed('7 newfstatat(AT_FDCWD</r>, "s", 0xffff, 0) = -1 EACCES (Permission denied)\n')
        self.assertEqual((parser.accesses["/r/d"], parser.accesses["/r/s"]), ({"dir"}, {"stat"}), "打开目录与无权限只算元数据")

    def test_driver_carries_an_identical_copy(self) -> None:
        """驱动随附一份（执行器在 ARM64 上从驱动目录运行，按路径加载同目录的 read_audit.py）。"""

        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "read_audit.py"
        self.assertEqual(driver.read_bytes(), (REPO_ROOT / "tools" / "ci" / "read_audit.py").read_bytes(),
                         "驱动里的 read_audit.py 与 tools/ci 原件不一致")

    def test_strace_argv_pipes_to_the_filter_and_refuses_shell_metacharacters(self) -> None:
        argv = ra.strace_argv(["python3", "-c", "pass"], output=Path("/out/u.trace.json"), roots=["/repo", "/data"], python="/usr/bin/python3")
        self.assertEqual(argv[:len(ra.STRACE_OPTIONS) + 1], ["strace", *ra.STRACE_OPTIONS])
        pipe = argv[argv.index("-o") + 1]
        self.assertTrue(pipe.startswith("|grep --line-buffered -E '"), "先经 grep 预筛")
        self.assertIn("' | /usr/bin/python3 -B ", pipe)
        self.assertIn("filter --output /out/u.trace.json --root /repo --root /data", pipe)
        self.assertEqual(argv[argv.index("--") + 1:], ["python3", "-c", "pass"])
        with self.assertRaisesRegex(ra.AuditError, "空白"):
            ra.strace_argv(["true"], output=Path("/out dir/u.json"), roots=["/repo"])


class ReadAuditPrefilterTests(unittest.TestCase):
    def test_prefilter_keeps_root_paths_relative_paths_and_process_lines_only(self) -> None:
        pattern = ra.prefilter_pattern(["/repo", "/data"])
        lines = {
            '1 openat(AT_FDCWD</repo>, "/usr/lib/python3.12/json/__init__.py", O_RDONLY) = 3</usr/lib/x>': False,
            '1 openat(AT_FDCWD</repo>, "/repo/tools/a.py", O_RDONLY) = 3</repo/tools/a.py>': True,
            '1 openat(AT_FDCWD</repo>, "tools/b.py", O_RDONLY <unfinished ...>': True,
            '1 openat(AT_FDCWD</tmp>, "c.py", O_RDONLY) = 3</tmp/c.py>': False,
            '1 newfstatat(AT_FDCWD</repo>, "/repoX/y", 0x1, 0) = -1 ENOENT (No such file or directory)': False,
            '1 newfstatat(AT_FDCWD</repo>, "/repo", 0x1, 0) = 0': True,
            '1 openat(AT_FDCWD</>, "/data/control/x", O_RDONLY) = 3</data/control/x>': True,
            '1 execve("/usr/bin/git", ["git"], 0x1 /* 3 vars */) = 0': True,
            '1 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 7': True,
            '1 <... clone resumed>) = 7': True,
            '1 chdir("pkg") = 0': True,
        }
        grep = shutil.which("grep")
        if grep is None:
            self.skipTest("没有 grep")
        kept = set(subprocess.run([grep, "-E", pattern], input="\n".join(lines) + "\n", capture_output=True, text=True).stdout.splitlines())
        self.assertEqual({line for line in lines if line in kept}, {line for line, keep in lines.items() if keep})

    def test_whole_repo_declarations_are_not_audited(self) -> None:
        occ = "tools/official_client_capture/"
        standard = [_range("repo:managed", [occ], [occ + "tests/"]), _range("repo:tests", [occ + "tests/"]), _range("repo:docs", ["docs/"]),
                    _range("repo:rest", [""], [occ, "docs/"])]
        self.assertTrue(ra.declares_whole_repo(standard))
        self.assertTrue(ra.declares_whole_repo([_range("repo:all", [""])]))
        self.assertFalse(ra.declares_whole_repo(standard[:3]))
        self.assertFalse(ra.declares_whole_repo([_range("repo:rest", [""], ["tools/"])]))


class ReadAuditCoverageTests(unittest.TestCase):
    INPUTS = [
        _range("repo:managed", ["tools/official_client_capture/"], ["tools/official_client_capture/tests/"]),
        _range("repo:tests-fixtures", ["tools/official_client_capture/tests/fixtures/"]),
        _file("tools/official_client_capture/tests/test_alpha.py"),
        {"category": "docs", "name": "tree:maintenance", "sha256": "2" * 64, "detail": {"path": "/data/docs/egress/maintenance", "files": 3}},
        {"category": "environment", "name": "recorded", "sha256": "3" * 64, "detail": {"campaign": "/rec/c", "paths": ["/rec/c", "/rec/runs/a"]}},
    ]

    def test_ranges_files_ancestors_and_absolute_entries(self) -> None:
        declared = ra.Declared(self.INPUTS)
        for path in ("tools/official_client_capture/codex_upgrade.py", "tools/official_client_capture/tests/test_alpha.py",
                     "tools/official_client_capture/tests/fixtures/x/y.json", "tools/official_client_capture/tests", "tools", ""):
            self.assertTrue(declared.covers_repo(path), path)
        for path in ("tools/official_client_capture/tests/test_beta.py", "backend/main.go", ".git/HEAD"):
            self.assertFalse(declared.covers_repo(path), path)
        self.assertTrue(declared.covers_absolute("/data/docs/egress/maintenance/a.json"))
        self.assertTrue(declared.covers_absolute("/rec/runs/a/x"))
        self.assertFalse(declared.covers_absolute("/rec/runs/b/x"))

    def test_git_needs_head_except_head_id_only_metadata(self) -> None:
        declared = ra.Declared(self.INPUTS)
        self.assertTrue(declared.covers_repo(".git/HEAD", head_id_only=True))
        self.assertTrue(declared.covers_repo(".git/refs/heads/main", head_id_only=True))
        self.assertFalse(declared.covers_repo(".git/objects/ab/cdef", head_id_only=True), "只读提交号的模块读对象库照样报出")
        self.assertTrue(ra.Declared([*self.INPUTS, {"category": "tests", "name": "head", "sha256": "4" * 64, "detail": {"value": "x"}}])
                        .covers_repo(".git/objects/ab/cdef"))

    def test_audit_reads_reports_undeclared_repo_and_data_root_paths(self) -> None:
        document = {"schema_version": ra.TRACE_SCHEMA, "lines": 9, "samples": {"/repo/backend/main.go": "openat(...main.go...)"}, "accesses": [
            ["/repo/tools/official_client_capture/tests/test_alpha.py", ["read"]],
            ["/repo/tools/official_client_capture/tests/test_beta.py", ["read"]],
            ["/repo/tools/official_client_capture/tests", ["list"]],
            ["/repo/tools/official_client_capture/tests/test_gamma.py", ["stat"]],
            ["/repo/tools/official_client_capture/tests/__init__.abi3.so", ["missing"]],
            ["/repo/tools/official_client_capture/tests/fixtures/new.json", ["missing"]],
            ["/repo/tools/official_client_capture/tests/test_delta.py", ["missing"]],
            ["/repo/backend/main.go", ["read", "stat"]],
            ["/data/control/deploy.json", ["read"]],
            ["/data/docs/egress/maintenance/a.json", ["read"]],
        ]}
        result = ra.audit_reads(document, self.INPUTS, repo_root="/repo", data_root="/data")
        self.assertEqual([(item["root"], item["path"], item["kinds"]) for item in result["undeclared"]], [
            ("data", "control/deploy.json", ["read"]),
            ("repo", "backend/main.go", ["read"]),
            ("repo", "tools/official_client_capture/tests/test_beta.py", ["read"]),
            ("repo", "tools/official_client_capture/tests/test_delta.py", ["missing"]),
        ], "只 stat 的 test_gamma 不报；夹具目录里探测不存在的文件已声明；.so 变体是导入系统的试探")
        self.assertEqual((result["undeclared_count"], result["metadata_only"], result["import_probes"], result["repo_paths"], result["data_paths"]),
                         (4, 2, 1, 5, 2))
        self.assertIn("WHOLE_TESTS_READERS", result["undeclared"][2]["suggestion"])
        self.assertEqual(result["undeclared"][1]["sample"], "openat(...main.go...)")

    def test_data_root_units_mirror_the_repository_layout_and_honor_the_exemptions(self) -> None:
        document = {"schema_version": ra.TRACE_SCHEMA, "lines": 4, "samples": {}, "accesses": [
            ["/data/tools/official_client_capture/codex_upgrade.py", ["read"]],
            ["/data/tools/official_client_capture/tests/test_alpha.py", ["read"]],
            ["/data/staging/pre-a3-scenarios/run-1/ledger.json", ["read", "write"]],
            ["/data/staging/other/x.json", ["read"]],
        ]}
        inside = ra.audit_reads(document, self.INPUTS, repo_root="/repo", data_root="/data", in_data_root=True)
        self.assertEqual([item["path"] for item in inside["undeclared"]], ["staging/other/x.json"])
        noise = {"schema_version": ra.TRACE_SCHEMA, "lines": 6, "samples": {}, "accesses": [
            ["/data/.git", ["missing"]], ["/data/HEAD", ["missing"]], ["/data/staging/.git/HEAD", ["missing"]], ["/data/-lbpf", ["missing"]],
            ["/data/tools/__init__.py", ["missing"]],
            ["/data/tools.official_client_capture.tests.real_chains.test_x.Chain._exercise", ["missing"]],
            ["/data/go.mod", ["missing"]], ["/data/staging/go.work", ["missing"]], ["/data/.hg", ["missing"]],
            ["/data/staging/_FOSSIL_", ["missing"]], ["/data/.r18-namespace-probe-1606558", ["write"]],
            ["/data/control/receipt.json", ["missing"]], ["/data/control/go.mod", ["missing"]],
            ["/data/.r18-namespace-probe-x", ["write"]],
        ]}
        flagged = ra.audit_reads(noise, self.INPUTS, repo_root="/repo", data_root="/data", in_data_root=True)
        self.assertEqual([item["path"] for item in flagged["undeclared"]],
                         [".r18-namespace-probe-x", "control/go.mod", "control/receipt.json"],
                         "git 仓库发现、命名空间包标记、编译器与 unittest 试探、Go 往上找模块与版本控制目录、R18 命名空间写探测"
                         "豁免；同名文件在别的位置、不合格式的探测文件、探测生产控制目录照样报出")
        outside = ra.audit_reads(document, self.INPUTS, repo_root="/repo", data_root="/data", in_data_root=False)
        self.assertEqual(outside["undeclared_count"], 4, "测试树单元读到数据根一律报出")


FAKE_STRACE = """#!{python}
# 假 strace（测试用）：按单元（UNIT_EXECUTOR_UNIT）从 FAKE_STRACE_READS 取合成的读取，写成 strace -f -y 格式的行，经 -o 给的
# 管道交给过滤器；然后在原工作目录运行 -- 之后的命令，以它的退出码退出。
import json, os, subprocess, sys
args = sys.argv[1:]
pipe = args[args.index("-o") + 1]
command = args[args.index("--") + 1:]
reads = json.loads(os.environ.get("FAKE_STRACE_READS") or "{{}}").get(os.environ.get("UNIT_EXECUTOR_UNIT", ""), [])
cwd = os.getcwd()
lines = "".join('4242 openat(AT_FDCWD<%s>, "%s", O_RDONLY|O_CLOEXEC) = 3<%s>\\n' % (cwd, path, path) for path in reads)
assert pipe.startswith("|")
subprocess.run(["/bin/sh", "-c", pipe[1:]], input=lines.encode("utf-8"), check=True)
sys.exit(subprocess.run(command).returncode)
"""


class StrictReadAuditTests(unittest.TestCase):
    """宿主机读取不能由仓库范围声明、目录前缀或一次空轨迹代替。"""

    def test_strict_parser_keeps_host_bytecode_and_resumes_file_calls(self):
        lines = ['1 openat(AT_FDCWD</repo>, "/host/__pycache__/a.pyc", O_RDONLY <unfinished ...>\n',
                 '1 <... openat resumed>) = 3</host/__pycache__/a.pyc>\n']
        report = ra.filter_stream(lines, ["/"], strict=True)
        self.assertEqual(report["accesses"], [["/host/__pycache__/a.pyc", ["read"]]])
        self.assertEqual(report["coverage"]["unresolved"], 0)
        self.assertEqual(report["lines"], 2)

    def test_strict_parser_keeps_dereferenced_target_and_rejects_incomplete(self):
        report = ra.filter_stream(['1 openat(AT_FDCWD</repo>, "/repo/link", O_RDONLY) = 3</host/data>\n'], ["/"], strict=True)
        self.assertEqual({row[0] for row in report["accesses"]}, {"/repo/link", "/host/data"})
        pending = ra.filter_stream(['1 openat(AT_FDCWD</repo>, "x", O_RDONLY <unfinished ...>\n'], ["/"], strict=True)
        self.assertEqual(pending["coverage"]["unresolved"], 1)

    def test_plain_open_keeps_resolved_target_and_unparsed_calls_fail_closed(self):
        trace = ra.filter_stream(['1 open("/repo/link", O_RDONLY) = 3</host/input>\n'], ["/"], strict=True)
        self.assertEqual({row[0] for row in trace["accesses"]}, {"/repo/link", "/host/input"})
        for line in ('1 open(0x1, O_RDONLY) = -1 EFAULT (Bad address)', '1 getdents64(3, 0x1, 10) = 0'):
            self.assertGreater(ra.filter_stream([line], ["/"], strict=True)["coverage"]["unresolved"], 0)
        trace = ra.filter_stream(['1 getcwd("/repo", 4096) = 6', '1 open("input", O_RDONLY) = 3</repo/input>'], ["/"], strict=True)
        self.assertEqual(trace["coverage"]["unresolved"], 0)
        self.assertEqual(dict(trace["accesses"])["/repo/input"], ["read"])

    def test_strict_summary_missing_unit_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            auditor = ue.ReadAuditor(out_dir=Path(directory), repo_root=REPO_ROOT, data_root=None,
                                     currents={"a": object(), "b": object()}, strict=True)
            auditor.results["a"] = {"coverage_complete": True, "undeclared_count": 0}
            report = auditor.report()
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["missing_units"], ["b"])

    def test_written_and_empty_inputs_never_get_reusable_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "input.txt"; path.write_text("不变字节也不能承接写操作")
            trace = ra.filter_stream([f'1 open("{path}", O_RDWR) = 3<{path}>'], ["/"], strict=True)
            self.assertFalse(ra.strict_audit_reads(trace, [ra.host_snapshot(str(path))], repo_root="/repo", data_root=None)["coverage_complete"])
            trace["accesses"] = []
            self.assertFalse(ra.strict_audit_reads(trace, [], repo_root="/repo", data_root=None)["coverage_complete"])

    def test_host_input_declaration_always_requires_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "input.txt"; path.write_text("输入")
            entries = ur.declared_inputs(mock.Mock(), {"host_paths": [str(path)], "require_read_audit": False})
            self.assertIn("require-read-audit", {entry["name"] for entry in entries})
            for invalid in ({"host_paths": "wrong"}, {"host_paths": [str(path), str(path)]}, {"require_read_audit": "true"}):
                with self.assertRaises(ur.RecordsError):
                    ur.declared_inputs(mock.Mock(), invalid)

    def test_exact_host_snapshot_binds_content_metadata_and_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            file = root / "input.txt"; file.write_text("本轮输入")
            absent = root / "absent.txt"
            inputs = [ra.host_snapshot(str(file)), ra.host_snapshot(str(absent))]
            trace = ra.filter_stream([f'1 openat(AT_FDCWD<{root}>, "{file}", O_RDONLY) = 3<{file}>\n',
                                     f'1 access("{absent}", F_OK) = -1 ENOENT (No such file or directory)\n'], ["/"], strict=True)
            check = lambda: ra.audit_reads(trace, inputs, repo_root="/repo", strict=True)
            self.assertTrue(check()["coverage_complete"])
            file.write_text("变更输入")
            self.assertFalse(check()["coverage_complete"])
            absent.write_text("原来不存在")
            self.assertGreater(check()["undeclared_count"], 0)

    def test_directory_and_whole_repo_cannot_cover_unbound_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / "child"; path.write_text("输入")
            trace = ra.filter_stream([f'1 openat(AT_FDCWD<{root}>, "{path}", O_RDONLY) = 3<{path}>\n'], ["/"], strict=True)
            inputs = [ra.host_snapshot(str(root)), _range("repo:all", [""])]
            result = ra.audit_reads(trace, inputs, repo_root=str(root), strict=True)
            self.assertFalse(result["coverage_complete"])
            self.assertEqual(result["undeclared"][0]["path"], str(path))

    def test_virtual_relative_and_parent_paths_cannot_be_snapshots(self):
        for path in ("/proc/self/status", "/dev/null", "/sys/kernel", "relative", "/root/../etc/passwd"):
            with self.subTest(path=path), self.assertRaises(ra.AuditError):
                ra.host_snapshot(path)

    def test_symlink_retargeting_invalidates_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("a", "b"):
                (root / name).write_text("相同字节")
            link = root / "link"; link.symlink_to(root / "a")
            first = ra.host_snapshot(str(link))
            link.unlink(); link.symlink_to(root / "b")
            self.assertNotEqual(first, ra.host_snapshot(str(link)))

    def test_scoped_or_empty_trace_never_proves_complete_coverage(self):
        scoped = ra.filter_stream([], ["/repo"])
        self.assertFalse(ra.audit_reads(scoped, [], repo_root="/repo")["coverage_complete"])
        self.assertFalse(ra.audit_reads(scoped, [], repo_root="/repo", strict=True)["coverage_complete"])
        empty = ra.filter_stream([], ["/"], strict=True)
        self.assertFalse(ra.audit_reads(empty, [], repo_root="/repo", strict=True)["coverage_complete"])

    def test_strict_wrapper_does_not_prefilter_host_and_never_skips_whole_repo(self):
        argv = ra.strace_argv(["true"], output=Path("/out/trace.json"), roots=["/repo"], strict=True)
        pipe = argv[argv.index("-o") + 1]
        self.assertNotIn("grep", pipe)
        self.assertIn("--strict --root /", pipe)
        with tempfile.TemporaryDirectory() as directory:
            current = mock.Mock(inputs=[_range("repo:all", [""])])
            auditor = ue.ReadAuditor(out_dir=Path(directory), repo_root=Path(directory), data_root=None,
                                     currents={"unit": current}, strict=True)
            self.assertFalse(auditor.skipped(mock.Mock(unit_id="unit")))

    def test_failed_audit_cannot_be_masked_by_successful_exit(self):
        record = {"exit_code": 0, "signal": None, "timed_out": False, "unit_type": "command", "passed": True,
                  "read_audit": {"status": "failed", "undeclared_count": 1}}
        self.assertFalse(ur.derived_pass(record)[0])


class ReadAuditExecutorTests(unittest.TestCase):
    """真跑执行器 run-gates：测试组一个模块、命令单元两个（声明不同的输入），假 strace 按单元写合成读取。"""

    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        tests = repo / "tools" / "official_client_capture" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_alpha.py").write_text("import unittest\nclass AlphaTests(unittest.TestCase):\n    def test_a(self): pass\n", encoding="utf-8")
        (tests / "helper_unused.py").write_text("X = 1\n", encoding="utf-8")
        (repo / "data").mkdir()
        (repo / "data" / "a.txt").write_text("a\n", encoding="utf-8")
        (repo / "data" / "b.txt").write_text("b\n", encoding="utf-8")
        git = ["git", "-C", str(repo), "-c", "user.name=e3", "-c", "user.email=e3@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
        return repo.resolve()

    def _run(self, root: Path, repo: Path, reads: dict[str, list[str]], *, mode: str = "re-execute", label: str = "out",
             strict: bool = False, host_paths: dict | None = None, scopes: tuple = (),
             environment: list | None = None) -> tuple[int, dict, str]:
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "strace"
        fake.write_text(FAKE_STRACE.format(python=sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        unit = lambda unit_id, inputs: {"unit_id": unit_id, "argv": ["true"], "cwd": str(repo), "cores": 1, "memory_mb": 128,  # noqa: E731
                                        "timeout_seconds": 60, "inputs": inputs}
        manifest = root / "gates.json"
        manifest.write_text(json.dumps({
            "schema_version": ue.GATES_SCHEMA,
            "test_groups": [{"group_id": "capture-tools", "start": "tools/official_client_capture/tests", "pattern": "test_*.py"}],
            "units": [unit("cmd:a", {"files": [{"category": "tests", "path": "data/a.txt"}]}),
                      unit("cmd:all", {"ranges": [{"category": "tests", "name": "repo:all", "include": [""], "exclude": []}]})],
            "gates": [{"gate_id": "capture", "test_groups": ["capture-tools"], "units": ["cmd:a"]}, {"gate_id": "spec", "units": ["cmd:all"]}],
        }), encoding="utf-8")
        if scopes or environment is not None:
            document = json.loads(manifest.read_text())
            eg.apply_input_scopes(document, scopes)
            if environment is not None:
                document["environment"] = environment
            manifest.write_text(json.dumps(document))
        config = root / "config.json"
        config.write_text(json.dumps({"schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 2, "default_quota": {"cores": 1, "memory_mb": 128},
                                      "quotas": {}, "splits": {}, "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 1}),
                          encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT), "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                    "FAKE_STRACE_READS": json.dumps(reads)})
        out = root / label
        options = ["--audit-strict"] if strict else []
        if host_paths is not None:
            host_file = root / "host-inputs.json"
            host_file.write_text(json.dumps({"schema_version": "unit-host-inputs/v1", "units": host_paths}))
            options += ["--audit-host-inputs", str(host_file)]
        completed = subprocess.run(
            [sys.executable, str(EXECUTOR), "run-gates", "--manifest", str(manifest), "--config", str(config), "--weights", str(root / "none.json"),
             "--durations", str(root / "none.json"), "--parallel", "2", "--cores", "2", "--state-dir", str(root / "state"), "--out-dir", str(out),
             "--shared-caches", "off", "--record-store", str(root / "store"), "--mode", mode, "--audit-reads", "--audit-data-root", str(root / "data-root"), *options],
            cwd=repo, capture_output=True, text=True, timeout=300, env=env)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8")) if (out / "summary.json").is_file() else {}
        return completed.returncode, summary, completed.stderr

    def test_undeclared_reads_fail_the_run_and_are_listed_per_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self._repo(root)
            tests = repo / "tools" / "official_client_capture" / "tests"
            reads = {
                "cmd:a": [str(repo / "data" / "a.txt"), str(repo / "data" / "b.txt"), str(root / "data-root" / "control" / "x.json")],
                "cmd:all": [str(repo / "data" / "b.txt"), str(root / "data-root" / "control" / "y.json")],
                "test_alpha": [str(tests / "test_alpha.py"), str(tests / "helper_unused.py"), str(repo / "data" / "a.txt")],
            }
            code, summary, stderr = self._run(root, repo, reads)
            self.assertEqual(code, 1, stderr[-2000:])
            audit = summary["read_audit"]
            self.assertEqual((audit["status"], audit["units"], audit["skipped_whole_repo"], audit["units_with_findings"]),
                             ("failed", 2, 1, ["cmd:a", "test_alpha"]), "声明整个仓库的 cmd:all 不审计")
            report = json.loads(Path(audit["report"]).read_text(encoding="utf-8"))
            paths = {unit_id: [(item["root"], item["path"]) for item in result["undeclared"]] for unit_id, result in report["units"].items()}
            self.assertEqual(paths["cmd:a"], [("data", "control/x.json"), ("repo", "data/b.txt")], "只声明了 a.txt；读到数据根一律报出")
            self.assertEqual(report["units"]["cmd:all"]["skipped"], "输入声明了整个仓库，不审计")
            self.assertEqual(report["skipped_whole_repo"], ["cmd:all"])
            self.assertEqual(paths["test_alpha"], [("repo", "tools/official_client_capture/tests/helper_unused.py")],
                             "测试单元的闭包只有自己；data/a.txt 在其余部分范围里")
            self.assertIn("读集审计（2 个单元，声明整个仓库不审计的 1 个）：不通过", stderr)
            self.assertTrue(any(gate["status"] == "failed" for gate in summary["gates"]), "审计失败必须同步到门禁结论")
            self.assertEqual(summary["unit_manifest"]["self_check"], "passed", "失败记录仍须完整可审计，清单结构自检照常")
            records = [json.loads(path.read_text()) for path in (root / "store/records").rglob("*.json")]
            failed = [record for record in records if record["unit_id"] == "cmd:a" and record["kind"] == "formal"]
            self.assertEqual(len(failed), 1)
            self.assertFalse(failed[0]["passed"])
            self.assertFalse(ur.derived_pass({**failed[0], "passed": True})[0], "改写 passed 不能掩盖原始审计失败")

            clean = {"cmd:a": [str(repo / "data" / "a.txt")], "cmd:all": [str(repo / "data" / "b.txt")],
                     "test_alpha": [str(tests / "test_alpha.py"), str(tests / "__pycache__" / "x.pyc")]}
            code, summary, stderr = self._run(root, repo, clean, label="out-clean")
            self.assertEqual((code, summary["read_audit"]["status"], summary["read_audit"]["undeclared_total"]), (0, "passed", 0), stderr[-2000:])

    def test_strict_host_coverage_replays_and_missing_trace_or_input_drift_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); repo = self._repo(root)
            host = root / "host.txt"; host.write_text("本轮宿主输入")
            reads = {"cmd:a": [str(repo / "data/a.txt")], "cmd:all": [str(host)],
                     "test_alpha": [str(repo / "tools/official_client_capture/tests/test_alpha.py")]}
            code, summary, stderr = self._run(root, repo, reads, strict=True, host_paths=reads)
            self.assertEqual(code, 0, stderr[-3000:])
            self.assertTrue(summary["read_audit"]["coverage_complete"])
            self.assertEqual(summary["read_audit"]["skipped_whole_repo"], 0)
            store = ur.RecordStore(root / "store")
            path, record = store.candidates("cmd:all")[0]
            current = ur.Current("cmd:all", "command", record["spec"], record["spec_sha256"], record["inputs"], record["inputs_sha256"], True)
            facts = ur.RunFacts(record["policy_sha256"], record["environment"], record["environment_sha256"], record["executor"],
                                1.0, ur.parse_utc(record["completed_at_utc"]), require_read_audit=True)
            self.assertEqual(ur.check_record(store, path, record, current, facts), [])
            trace = store.log_path(record["read_audit"]["trace"]["sha256"])
            before = trace.read_bytes(); trace.write_bytes(before + b" ")
            self.assertTrue(any("轨迹" in reason for reason in ur.check_record(store, path, record, current, facts)))
            trace.write_bytes(before); host.write_text("输入已变化")
            self.assertTrue(any("轨迹" in reason for reason in ur.check_record(store, path, record, current, facts)))

    def test_strict_whole_repo_with_unbound_host_is_a_failed_nonreusable_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); repo = self._repo(root)
            host = root / "host.txt"; host.write_text("未登记的宿主输入")
            code, summary, stderr = self._run(root, repo, {"cmd:all": [str(host)]}, strict=True)
            self.assertEqual(code, 1, stderr[-1000:])
            self.assertFalse(summary["read_audit"]["coverage_complete"])
            records = [record for _, record in ur.RecordStore(root / "store").candidates("cmd:all")]
            self.assertEqual(len(records), 1, "只因覆盖缺口失败时不得隐式重复执行命令")
            record = records[0]
            self.assertEqual(record["kind"], "formal")
            self.assertFalse(record["passed"])
            self.assertFalse(record["read_audit"]["coverage_complete"])

    def test_narrow_scope_requires_origin_replay_and_new_strict_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); repo = self._repo(root)
            reads = {"cmd:a": [str(repo / "data/a.txt")], "cmd:all": [str(repo / "data/a.txt")],
                     "test_alpha": [str(repo / "tools/official_client_capture/tests/test_alpha.py")]}
            code, summary, stderr = self._run(root, repo, reads, strict=True, host_paths=reads)
            self.assertEqual(code, 0, stderr[-3000:])
            store = ur.RecordStore(root / "store")
            source_path, source = store.candidates("cmd:all")[0]
            trace_path = store.log_path(source["read_audit"]["trace"]["sha256"])
            scope = ra.narrow_input_scope(source_path, trace_path)
            self.assertTrue(scope["requires_validation"])
            self.assertFalse(scope["reuse_enabled"])
            self.assertEqual(scope["inputs"]["host_paths"], reads["cmd:all"])
            before = ur.declared_inputs(ur.RepoIndex.load(repo), scope["inputs"])
            (repo / "data/b.txt").write_text("无关文件变更\n")
            after = ur.declared_inputs(ur.RepoIndex.load(repo), scope["inputs"])
            self.assertEqual(ur.entries_sha256(before), ur.entries_sha256(after))
            (repo / "data/b.txt").write_text("b\n")
            code, _summary, stderr = self._run(root, repo, reads, strict=True, host_paths=reads, scopes=(scope,), label="narrow")
            self.assertEqual(code, 0, stderr[-3000:])
            records = store.candidates("cmd:all")
            self.assertEqual(len(records), 2, "收窄后必须留下新的实际执行记录")
            new_record = next(record for path, record in records if path != source_path)
            self.assertNotEqual(source["inputs_sha256"], new_record["inputs_sha256"])
            self.assertTrue(new_record["read_audit"]["coverage_complete"])
            code, _summary, stderr = self._run(root, repo, reads, mode="full-set-pass", scopes=(scope,), label="refused")
            self.assertEqual(code, 2)
            self.assertIn("必须重新执行严格审计", stderr)
            code, _summary, stderr = self._run(root, repo, reads, strict=True, host_paths=reads, scopes=(scope,), label="platform",
                environment=[ur.value_entry("environment", "platform-override", "另一内核")])
            self.assertEqual(code, 2)
            self.assertIn("平台环境", stderr)
            origin = store.manifest_path(source["run"]["run_id"])
            origin_bytes = origin.read_bytes(); origin.unlink()
            with self.assertRaisesRegex(ra.AuditError, "原运行"):
                ra.narrow_input_scope(source_path, trace_path)
            origin.write_bytes(origin_bytes)
            (repo / "data/a.txt").write_text("相关输入变更\n")
            changed = ur.declared_inputs(ur.RepoIndex.load(repo), scope["inputs"])
            self.assertNotEqual(ur.entries_sha256(before), ur.entries_sha256(changed))
            with self.assertRaises(ra.AuditError):
                ra.narrow_input_scope(source_path, trace_path)

    def test_scope_refuses_tampered_failed_and_diagnostic_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); repo = self._repo(root)
            reads = {"cmd:a": [str(repo / "data/a.txt")], "cmd:all": [str(repo / "data/b.txt")],
                     "test_alpha": [str(repo / "tools/official_client_capture/tests/test_alpha.py")]}
            code, _summary, stderr = self._run(root, repo, reads, strict=True, host_paths=reads)
            self.assertEqual(code, 0, stderr[-1000:])
            store = ur.RecordStore(root / "store")
            path, record = store.candidates("cmd:all")[0]
            trace = store.log_path(record["read_audit"]["trace"]["sha256"])
            scope = ra.narrow_input_scope(path, trace)
            manifest = json.loads((root / "gates.json").read_text())
            for key, value in (("cores", 2), ("argv", ["false"]), ("exclusive", True), ("env", {"A": "changed"})):
                changed = json.loads(json.dumps(manifest)); changed["units"][1][key] = value
                with self.assertRaisesRegex(ValueError, "命令、环境或额度"):
                    eg.apply_input_scopes(changed, (scope,))
            altered = json.loads(json.dumps(scope)); altered["inputs"]["host_paths"] = []
            with self.assertRaisesRegex(ValueError, "无法重放"):
                eg.apply_input_scopes(manifest, (altered,))
            for changes in ({"passed": False}, {"kind": "diagnostic"}, {"read_audit": {"coverage_complete": False}}):
                other = ur.seal_record({**record, **changes})
                other_path = store.put_record(other)
                with self.assertRaises(ra.AuditError):
                    ra.narrow_input_scope(other_path, trace)
            original = trace.read_bytes(); trace.write_bytes(original + b" ")
            with self.assertRaises(ra.AuditError):
                ra.narrow_input_scope(path, trace)

    def test_audit_is_only_for_re_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self._repo(root)
            code, _summary, stderr = self._run(root, repo, {}, mode="full-set-pass")
            self.assertEqual(code, 2)
            self.assertIn("只许与 --mode re-execute 同用", stderr)


def _traced() -> bool:
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith("TracerPid:") and line.split()[1] != "0" for line in status.splitlines())


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("strace") and not _traced(),
                     "要 Linux 上的 strace，且本进程没被跟踪（入口门禁带审计跑全集时本测试已在 strace 下）")
class ReadAuditRealStraceTests(unittest.TestCase):
    def test_real_host_paths_snapshot_and_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.txt"; source.write_text("真实宿主输入\n")
            output = root / "trace.json"
            command = ra.strace_argv(["/usr/bin/cat", str(source)], output=output, roots=["/"], strict=True)
            environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
            subprocess.run(command, env=environment, cwd=root, check=True, stdout=subprocess.DEVNULL, timeout=120)
            discovery = ra.load_trace(output)
            self.assertEqual(discovery["coverage"]["unresolved"], 0)
            self.assertTrue(any(not path.startswith(str(root)) for path, _ in discovery["accesses"]), "真实动态链接器与宿主依赖必须出现")
            inputs = [ra.host_snapshot(path) for path, _ in discovery["accesses"]]
            subprocess.run(command, env=environment, cwd=root, check=True, stdout=subprocess.DEVNULL, timeout=120)
            trace = ra.load_trace(output)
            report = ra.strict_audit_reads(trace, inputs, repo_root=str(root), data_root=None)
            self.assertTrue(report["coverage_complete"], report)
            source.write_text("真实输入漂移\n")
            self.assertFalse(ra.strict_audit_reads(trace, inputs, repo_root=str(root), data_root=None)["coverage_complete"])

    def test_real_strace_through_the_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "pkg").mkdir()
            (root / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
            output = root / "trace.json"
            script = textwrap.dedent("""
                import os, subprocess
                open("pkg/mod.py").read()
                os.path.exists("pkg/nope.txt")
                subprocess.run(["cat", "pkg/mod.py"], stdout=subprocess.DEVNULL, check=True)
            """)
            argv = ra.strace_argv([sys.executable, "-B", "-c", script], output=output, roots=[str(root)])
            subprocess.run(argv, cwd=root, check=True, timeout=120)
            accesses = dict((path, kinds) for path, kinds in ra.load_trace(output)["accesses"])
            self.assertIn("read", accesses[str(root / "pkg" / "mod.py")])
            self.assertEqual(accesses[str(root / "pkg" / "nope.txt")], ["missing"])


if __name__ == "__main__":
    unittest.main()
