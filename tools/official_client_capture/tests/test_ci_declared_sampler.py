"""声明级承接读集采样器（``tools/ci/declared_sampler.py``）。

* T-API：用 ``test_ci_read_audit.py`` 里那份真实 strace 行喂 ``TraceParser(strict=True)``，钉住采样器依赖的全部公共行为
  （accesses、lines、unresolved、unresolved_reasons、pending、deferred_count，以及清空 accesses 与 samples 不影响解析状态）。
  审计器一路改名或改语义，这里会立刻红，而不是让采样悄悄变形；
* T-F1：filter 的旁路——ARM64 真实样本里的 detached 与 unfinished 标记行、connect 目的地、git 子进程 argv（含 argv 不可见
  的哨兵）、非 UTF-8 行、聚合块大小不改变结果、规模保护；
* T-M1：merge 的并集、tracked 标注（合成 git 仓库）和采样有效性判据，每一条判据都有反例；
* T-D1：draft 的归并阈值、上限、未归类清单与评审清单；
* T-R1：run 构造的 argv（launcher 在 strace 外侧、命令取自清单）、环境、冷热缓存、前后 git status、docker 钩子；用假 strace
  与假 launcher 在本机把 run、merge、draft 整条链跑通，不依赖 Linux 与真实 strace。

步骤 5 WP4d：S3（execveat 与别名启动的 git 照样收集，认不出形态的 exec 行记 ``sampler:exec-unparsed``）的过滤器用例在 ``FilterTests``；S6：docker 钩子用例
与机器上有没有 docker 无关（第二次运行把子进程环境的 PATH 限定成只含假 strace／假 launcher 的目录），CI 的 ubuntu runner 自带 docker 也稳定。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal as signals
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import declared_sampler as ds
from tools.official_client_capture.tests import declared_sampler_trace_fixture as real

REPO_ROOT = Path(__file__).resolve().parents[3]
UNIT = "backend:go-test"
HOME = "/home/sampler"
TMP = "/tmp/sampler-tmp"
SHA = "a" * 40
# test_ci_read_audit.py 里的那份真实 strace 行（ARM64，strace 6.8，-f -y）。
R = "/root/vc-rounds/entry-refactor/e3-04-probe"
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


# ---------------------------------------------------------------------------
# 合成 strace 行
# ---------------------------------------------------------------------------


def open_line(cwd: str, path: str, flags: str = "O_RDONLY|O_CLOEXEC", pid: int = 4242) -> str:
    return f'{pid} openat(AT_FDCWD<{cwd}>, "{path}", {flags}) = 3<{path}>'


def write_line(cwd: str, path: str) -> str:
    return open_line(cwd, path, "O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666")


def missing_line(cwd: str, path: str) -> str:
    return f'4242 openat(AT_FDCWD<{cwd}>, "{path}", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)'


def stat_line(cwd: str, path: str) -> str:
    return f'4242 newfstatat(AT_FDCWD<{cwd}>, "{path}", 0x1, 0) = 0'


def list_line(directory: str) -> str:
    return f"4242 getdents64(3<{directory}>, 0x1 /* 3 entries */, 32768) = 80"


def inet(port: int, address: str) -> str:
    return f'{{sa_family=AF_INET, sin_port=htons({port}), sin_addr=inet_addr("{address}")}}'


def connect_line(sockaddr: str, tail: str = ") = 0") -> str:
    return f"4242 connect(3<socket:[1]>, {sockaddr}, 16{tail}"


def git_line(argv: list[str]) -> str:
    return f'4242 execve("/usr/bin/git", [{", ".join(json.dumps(item) for item in argv)}], 0x1 /* 17 vars */) = 0'


def aliases_for(tree: object, work: object) -> dict[str, str]:
    return {"TREE": str(tree), "WORK": str(work), "HOME": HOME, "TMP": TMP}


def feed_all(aggregator: ds.StreamAggregator, lines: list[str]) -> dict:
    for line in lines:
        aggregator.feed(line + "\n")
    return aggregator.finish()


class ParserApiTests(unittest.TestCase):
    """T-API：采样器对审计器解析器的全部依赖。"""

    def test_strict_parser_public_surface_on_real_strace_lines(self) -> None:
        parser = ds.read_audit_module().TraceParser(strict=True)
        for line in PROBE_LINES.splitlines(True):
            parser.feed(line)
        paths = {path.replace(R, "R"): sorted(kinds) for path, kinds in parser.accesses.items()}
        self.assertEqual(paths["R/pkg/sub/mod.py"], ["read"])
        self.assertEqual(paths["R/pkg/sub"], ["list", "stat"], "getdents64 是 list，stat 成功是 stat")
        self.assertEqual(paths["R/pkg/out/中.txt"], ["write"], "八进制转义还原成中文")
        self.assertEqual((paths["/usr/bin/cat"], paths["/usr/local/sbin/cat"]), (["exec"], ["missing"]))
        self.assertEqual(parser.lines, 19)
        self.assertEqual(parser.unresolved, 3)
        self.assertEqual(parser.unresolved_reasons, {"result-or-string-incomplete:execve": 1, "unsupported-call:exit_group": 1,
                                                     "resume-without-start:wait4": 1}, "原因名是采样有效性判据的一部分")
        self.assertEqual((dict(parser.pending), parser.deferred_count), ({}, 0))
        self.assertIsInstance(parser.samples, dict, "采样器会清空它，必须存在且是字典")

    def test_pending_and_deferred_counters_expose_incomplete_streams(self) -> None:
        parser = ds.read_audit_module().TraceParser(strict=True)
        parser.feed('1 openat(AT_FDCWD</r>, "x", O_RDONLY <unfinished ...>\n')
        self.assertEqual((len(parser.pending), parser.unresolved), (1, 0), "未完成调用在 pending 里，由使用方计入 unresolved")
        broken = ds.read_audit_module().TraceParser(strict=True)
        for line in ['1 getcwd("/repo/a/b", 4096) = 10', '1 clone(child_stack=NULL, flags=CLONE_VFORK|SIGCHLD <unfinished ...>',
                     '2 chdir("../..") = 0', '2 execve("./bin/check", ["check"], 0x1) = 0']:
            broken.feed(line + "\n")
        self.assertGreater(broken.deferred_count, 0, "没有父进程返回行时，子进程的相对路径调用只能缓存")

    def test_clearing_accesses_and_samples_between_chunks_keeps_parser_state(self) -> None:
        """采样器裁剪内存的依据：清空 accesses 与 samples 不影响 cwd 等跨行状态，也不丢计数。"""
        parser = ds.read_audit_module().TraceParser(strict=True)
        for line in ['1 getcwd("/repo/a/b", 4096) = 10', '1 openat(AT_FDCWD</repo/a/b>, "f1", O_RDONLY) = 3</repo/a/b/f1>']:
            parser.feed(line + "\n")
        parser.accesses.clear()
        parser.samples.clear()
        for line in ['1 chdir("..") = 0', '1 stat("g", 0x1) = 0', '1 newfstatat(AT_FDCWD</repo/a>, "h", 0x1, AT_SYMLINK_NOFOLLOW) = 0']:
            parser.feed(line + "\n")
        self.assertEqual(sorted(parser.accesses), ["/repo/a", "/repo/a/g", "/repo/a/h"], "清空之后相对路径照样按保留的 cwd 还原")
        self.assertEqual((parser.lines, parser.unresolved_reasons), (5, {"symlink-metadata-not-bound:newfstatat": 1}))

    def test_marker_lines_are_unrecognized_without_the_side_channel(self) -> None:
        """没有旁路时，真实样本里的标记行会被严格解析记成 unrecognized-line——这就是 filter 必须先识别它们的原因。"""
        for name, expected in (("DETACHED_WINDOW", 1), ("UNFINISHED_WINDOW", 1), ("DETACHED_WINDOW_LATE", 1)):
            parser = ds.read_audit_module().TraceParser(strict=True)
            for line in getattr(real, name).splitlines(True):
                parser.feed(line)
            self.assertEqual(parser.unresolved_reasons, {"unrecognized-line": expected}, name)


class InteroperabilityTests(unittest.TestCase):
    """采样器与登记侧、审计器、入口脚本之间必须一致的几个约定：任何一边漂移，这里立刻红。"""

    def test_digest_algorithms_match_unit_records_and_the_fixed_vectors(self) -> None:
        from tools.ci import unit_records as ur
        value = {"argv": ["go", "test", "./...", "-count=1"], "cwd_rel": "backend"}
        self.assertEqual((ds.canonical(value), ds.sha256_json(value)), (ur.canonical(value), ur.sha256_json(value)))
        self.assertEqual(ds.command_digest(value["argv"], "backend"), "99d3cf30b19dd7f115666440a99a3b989308631b368cc92c143089b626b77ac4")
        self.assertEqual(ds.sha256_json([]), "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945", "空启动前缀的摘要")
        self.assertEqual(ds.sha256_json({"argv": ["echo", "中"], "cwd_rel": "."}), "22a36b5c3933f40da8a924d64e06db5516e63092fea227bbacf4f67fba6d9229",
                         "非 ASCII 不转义，与登记侧的规范化一致")

    def test_known_launcher_matches_the_runtime_contract_and_the_entry_script(self) -> None:
        from tools.ci import read_audit_runtime as runtime
        self.assertEqual(ds.KNOWN_LAUNCHER, runtime.LAUNCHER)
        script = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh").read_text(encoding="utf-8")
        found = [line for line in script.splitlines() if line.startswith("LAUNCHER='[")]
        self.assertEqual(len(found), 1)
        self.assertEqual(json.loads(found[0][len("LAUNCHER='"):-1]), ds.KNOWN_LAUNCHER, "入口脚本传给 plan 的启动前缀")

    def test_strace_options_are_a_superset_of_what_the_auditor_traces(self) -> None:
        from tools.ci import read_audit as audit

        def traced(options: tuple[str, ...]) -> set[str]:
            values = [options[index + 1] for index, item in enumerate(options[:-1]) if item == "-e" and options[index + 1].startswith("trace=")]
            return set(values[0][len("trace="):].split(","))

        self.assertTrue(traced(audit.STRACE_OPTIONS) <= traced(ds.STRACE_OPTIONS), "审计器新增的跟踪调用，采样器也必须跟踪，否则会漏读")
        for flag in ("-f", "-qq", "--seccomp-bpf", "-y"):
            self.assertIn(flag, audit.STRACE_OPTIONS)
            self.assertIn(flag, ds.STRACE_OPTIONS)
        # 容忍集合：步骤 5 后续改进（WP4b）加入 unfinished-calls-at-eof（精确匹配，与登记侧一致；上限与匹配语义见 test_ci_declared_sampler_followup）。
        self.assertEqual(ds.DECLARED_TOLERATED_UNRESOLVED, ("symlink-metadata-not-bound:", "unfinished-calls-at-eof"))


class FilterTests(unittest.TestCase):
    """T-F1。"""

    def setUp(self) -> None:
        self.aliases = {"TREE": real.TREE, "WORK": real.ROOT, "HOME": "/root", "TMP": "/tmp"}

    def test_real_sample_markers_are_counted_separately_and_do_not_invalidate(self) -> None:
        lines = "".join(real.ALL_SEGMENTS).splitlines()
        document = ds.filter_stream((line + "\n" for line in lines), self.aliases, unit_id=UNIT)
        coverage = document["coverage"]
        self.assertEqual(coverage["detached_markers"], 3, "两条 detached 与一条 unfinished 标记行")
        self.assertEqual(coverage["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 6}, "标记行没有变成 unrecognized-line")
        self.assertEqual((coverage["unresolved"], coverage["lines"], coverage["stream_complete"]), (6, len(lines), True))
        self.assertEqual(ds.validate_raw(document), [])
        repo = dict(document["repo_accesses"])
        self.assertEqual(repo["{TREE}/backend/go.mod"], ["stat"])
        self.assertEqual(repo["{TREE}/go.work"], ["missing"], "Go 往上找 go.work 的探测")
        outside = {key: kinds for key, kinds, _count in document["outside_accesses"]}
        self.assertEqual(outside["{HOME}/.config/go/env"], ["missing"], "HOME 下的配置逐条保留，别名替换后不含采样机路径")
        self.assertEqual(outside["/proc/self/cgroup"], ["read"], "/proc/<进程号>/ 规范成 /proc/self/")
        self.assertEqual(outside["{WORK}/go-tmp/"], ["read", "stat", "write"], "go-build 临时目录按暂存前缀整体折叠，kinds 取并集")
        self.assertEqual(outside["{WORK}/gc-c/e3/"], ["stat"], "GOCACHE 内容寻址文件按两位十六进制子目录聚合")
        self.assertEqual(outside["/usr/local/go/src/internal/abi/"], ["read"], "仓库外按目录聚合")
        self.assertEqual(repo["{TREE}/backend/internal/pkg"], ["dir", "list"], "getdents64 目录枚举记为 list")
        self.assertTrue(all(real.ROOT not in key for key in outside) and all(real.ROOT not in path for path in repo),
                        "证据里不含采样时的具体绝对路径")

    def test_a_non_marker_unrecognized_line_still_invalidates(self) -> None:
        aggregator = ds.StreamAggregator(self.aliases, unit_id=UNIT)
        document = feed_all(aggregator, ["3106947 ???( <detached ...> extra", "3106947 ???( <exited ...>", "没有进程号的杂散输出"])
        self.assertEqual(document["coverage"]["detached_markers"], 0, "只有精确匹配的两种标记才单独计数")
        self.assertEqual(document["coverage"]["unresolved_reasons"], {"unrecognized-line": 3})

    def test_parse_connect_on_the_formats_measured_on_arm64(self) -> None:
        """格式取自调度者在 ARM64（strace 6.8）的实测：verbose=none 时只有指针，verbose=connect 时三种地址族都能解码。"""
        measured = [
            ('3717360 connect(3<socket:[777911881]>, 0xef08dd1fe598, 16) = -1 EINPROGRESS (Operation now in progress)',
             ("unparsed", "3<socket:[777911881]>, 0xef08dd1fe598, 16) = -1 EINPROGRESS (Operation now in progress)")),
            ('3717361 connect(3<socket:[777911882]>, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}, 16) = '
             '-1 EINPROGRESS (Operation now in progress)', ("AF_INET", "127.0.0.1:9")),
            ('3717362 connect(3<socket:[777911883]>, {sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}, 23) = 0',
             ("AF_UNIX", "/var/run/docker.sock")),
            ('3717363 connect(3<socket:[777911884]>, {sa_family=AF_INET6, sin6_port=htons(9), sin6_flowinfo=htonl(0), '
             'inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}, 28) = -1 EINPROGRESS (Operation now in progress)', ("AF_INET6", "[::1]:9")),
            # 抽象套接字的两种写法统一成 @名字；路径被 -s 截断时目的地不全，记不可判。
            ('1 connect(3<socket:[1]>, {sa_family=AF_UNIX, sun_path=@"abstract"}, 11) = 0', ("AF_UNIX", "@abstract")),
            ('1 connect(3<socket:[1]>, {sa_family=AF_UNIX, sun_path="\\0abstract"}, 11) = 0', ("AF_UNIX", "@abstract")),
            ('1 connect(3<socket:[1]>, {sa_family=AF_UNIX, sun_path="/very/long/pa"...}, 110) = 0',
             ("unparsed", '3<socket:[1]>, {sa_family=AF_UNIX, sun_path="/very/long/pa"...}, 110) = 0')),
            ('1 connect(3<socket:[1]>, {sa_family=AF_UNSPEC, sa_data="\\0\\0"}, 16) = 0', ("AF_UNSPEC", "")),
            ('1 connect(3<socket:[1]>, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 0', ("AF_NETLINK", "")),
            ('1 openat(AT_FDCWD</r>, "/r/connect(x", O_RDONLY) = 3</r/connect(x>', None),
        ]
        for line, expected in measured:
            self.assertEqual(ds.parse_connect(line), expected, line)

    def test_connect_lines_collect_destinations_and_never_reach_the_parser(self) -> None:
        sockaddrs = [inet(5432, "127.0.0.1"), inet(80, "10.0.0.1"), inet(5432, "127.0.0.1"),
                     '{sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}',
                     '{sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}', '{sa_family=AF_UNIX, sun_path=@"abstract"}',
                     '{sa_family=AF_UNSPEC, sa_data="\\0\\0"}', "{sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}", "0x7ffd1234"]
        lines = [connect_line(item) for item in sockaddrs]
        lines.append('4242 connect(5<socket:[2]>, ' + inet(80, "10.0.0.2") + ', 16 <unfinished ...>')
        lines.append("4242 <... connect resumed>) = -1 EINPROGRESS (Operation now in progress)")
        document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)
        pairs = [(item["family"], item["target"]) for item in document["network"]]
        self.assertEqual(pairs, sorted({("AF_INET", "127.0.0.1:5432"), ("AF_INET", "10.0.0.1:80"), ("AF_INET", "10.0.0.2:80"),
                                        ("AF_INET6", "[::1]:443"), ("AF_UNIX", "/var/run/docker.sock"), ("AF_UNIX", "@abstract"),
                                        ("AF_UNSPEC", ""), ("AF_NETLINK", ""), ("unparsed", "3<socket:[1]>, 0x7ffd1234, 16) = 0")}))
        self.assertEqual(document["coverage"]["unresolved"], 0, "connect 与 resumed 行没有交给严格解析器（否则是 unsupported-call 和 resume-without-start）")

    def test_connect_start_and_resumed_lines_are_paired_by_pid(self) -> None:
        """多线程下 connect 被拆成 unfinished 与 resumed 两行，中间夹着别的进程的行；两种行都不交给严格解析器，且按进程号配对。"""
        t = real.TREE
        start_a = '100 connect(3<socket:[1]>, ' + inet(443, "10.0.0.1") + ', 16 <unfinished ...>'
        start_b = '300 connect(4<socket:[2]>, {sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}, 23 <unfinished ...>'
        paired = [start_a, open_line(t, f"{t}/backend/go.mod", pid=200), start_b, open_line(t, f"{t}/backend/a.go", pid=200),
                  "100 <... connect resumed>) = -1 EINPROGRESS (Operation now in progress)", "300 <... connect resumed>) = 0"]
        document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), paired)
        self.assertEqual([(item["family"], item["target"]) for item in document["network"]],
                         [("AF_INET", "10.0.0.1:443"), ("AF_UNIX", "/var/run/docker.sock")], "目的地来自 start 行，返回值不参与")
        self.assertEqual((document["coverage"]["unresolved"], document["coverage"]["unfinished_calls"]), (0, 0))
        self.assertEqual(sorted(path for path, _kinds in document["repo_accesses"]), ["{TREE}/backend/a.go", "{TREE}/backend/go.mod"],
                         "夹在中间的别的进程的行照常解析")
        cases = {
            "resumed 没有对应的 start": (["400 <... connect resumed>) = 0"], {"sampler:connect-resume-without-start": 1}, 0),
            "start 之后没等到 resumed": ([start_a], {"sampler:connect-unfinished-at-eof": 1}, 1),
            "同一进程连续两个 unfinished": ([start_a, start_a, "100 <... connect resumed>) = 0"],
                                   {"sampler:connect-overlapping-unfinished": 1}, 0),
            "resumed 配给了别的进程的 start": ([start_a, "200 <... connect resumed>) = 0"],
                                      {"sampler:connect-resume-without-start": 1, "sampler:connect-unfinished-at-eof": 1}, 1),
        }
        for name, (lines, reasons, unfinished) in cases.items():
            with self.subTest(name):
                coverage = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)["coverage"]
                self.assertEqual(coverage["unresolved_reasons"], reasons)
                self.assertEqual((coverage["unresolved"], coverage["unfinished_calls"]), (sum(reasons.values()), unfinished))

    def test_resumed_lines_of_other_calls_still_go_to_the_parser(self) -> None:
        """旁路只截 connect 的 resumed；别的调用的 resumed 照常交给严格解析器去配对。"""
        t = real.TREE
        lines = [f'4242 openat(AT_FDCWD<{t}>, "{t}/backend/go.mod", O_RDONLY <unfinished ...>', f"4242 <... openat resumed>) = 3<{t}/backend/go.mod>",
                 "4243 <... newfstatat resumed>) = 0"]
        reasons = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)["coverage"]["unresolved_reasons"]
        self.assertEqual(reasons, {"resume-without-start:newfstatat": 1})

    def test_git_child_argv_is_collected_and_hidden_argv_leaves_a_sentinel(self) -> None:
        lines = [git_line(["git", "show", f"{SHA}:docs/egress/a.json"]), git_line(["git", "merge-base", "--is-ancestor", SHA, "HEAD"]),
                 git_line(["git", "show", f"{SHA}:docs/egress/a.json"]),
                 '4242 execve("/usr/bin/git", 0xffffd2a979c8, 0xffffd2a979f0) = 0',
                 '4242 execve("/usr/bin/cat", ["cat", "x"], 0x1 /* 17 vars */) = 0',
                 '4242 execve("/usr/bin/env", ["git", "status"], 0x1 /* 17 vars */ <unfinished ...>',
                 "4242 <... execve resumed>) = 0"]
        document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)
        self.assertEqual(document["git_commands"], [["git", "show", f"{SHA}:docs/egress/a.json"], ["git", "merge-base", "--is-ancestor", SHA, "HEAD"],
                                                    ["git", ds.GIT_ARGV_HIDDEN], ["git", "status"]], "按 argv[0] 判定，去重，保持首次出现顺序")
        self.assertEqual(document["coverage"]["unresolved"], 0, "execve 行也照常交给严格解析器")

    def test_execveat_and_alias_invocations_of_git_are_collected(self) -> None:
        """S3：execveat 启动的 git（路径参数是 git，或 AT_EMPTY_PATH 时 dirfd 注解里的路径是 git）与别名调用（argv[0] 不是 git、可执行文件是 git）都收集；
        不是 git 的 exec 行（execve／execveat）一律不收集，也不记未解析原因。"""
        t = real.TREE
        lines = [
            f'4242 execveat(AT_FDCWD<{t}/backend>, "/usr/bin/git", ["git", "show", "{SHA}"], 0x1 /* 17 vars */, 0) = 0',
            f'4243 execveat(3</usr/bin/git>, "", ["git", "log", "-1", "{SHA}"], 0x1 /* 17 vars */, AT_EMPTY_PATH) = 0',
            f'4244 execveat(4</usr/bin/git>, "", ["gitx", "show", "{SHA}"], 0x1 /* 17 vars */, AT_EMPTY_PATH) = 0',  # 别名：argv[0] 不是 git，dirfd 注解里是 git
            f'4245 execve("/usr/bin/git", ["gitx", "show", "{SHA}"], 0x1 /* 17 vars */) = 0',  # 别名，argv 与上一条相同：去重
            f'4246 execveat(AT_FDCWD<{t}>, "/usr/bin/git", 0xffffd2a979c8, 0x2, 0) = 0',  # argv 不可见，路径是 git：哨兵
            f'4247 execveat(5</usr/bin/git>, "", 0xffffd2a979c8, 0x2, AT_EMPTY_PATH) = 0',  # AT_EMPTY_PATH 且 argv 不可见：从 dirfd 注解取 git，哨兵
            f'4248 execveat(AT_FDCWD<{t}>, "/usr/bin/cat", ["cat", "x"], 0x1, 0) = 0',  # 不是 git
            f'4249 execveat(6</usr/bin/cat>, "", ["cat", "x"], 0x1, AT_EMPTY_PATH) = 0',  # 不是 git
            f'4250 execve("/usr/bin/env", ["git", "status"], 0x1 /* 17 vars */) = 0',  # 按 argv[0] 判：env 启动的 git
            f'4251 execveat(7, "", ["git", "tag"], 0x1, AT_EMPTY_PATH) = 0',  # 没有注解，但 argv[0] 是 git：照样收集，不需要注解
            "4242 <... execve resumed>) = 0",  # resumed 行、路径里碰巧含 “execve(” 的普通行都不是 exec 调用行
            f'4242 openat(AT_FDCWD<{t}>, "/tmp/execve(1)", O_RDONLY) = 3</tmp/execve(1)>',
            f'4242 openat(AT_FDCWD<{t}>, "/tmp/x execveat(2) y", O_RDONLY) = 3</tmp/x execveat(2) y>',
        ]
        document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)
        self.assertEqual(document["git_commands"], [["git", "show", SHA], ["git", "log", "-1", SHA], ["gitx", "show", SHA], ["git", ds.GIT_ARGV_HIDDEN],
                                                    ["git", "status"], ["git", "tag"]], "按 argv[0] 或可执行文件的 basename 判定，去重，保持首次出现顺序")
        self.assertNotIn("sampler:exec-unparsed", document["coverage"]["unresolved_reasons"])
        self.assertEqual(ds.validate_raw(document), [])

    def test_parse_git_execve_on_the_exec_call_shapes(self) -> None:
        t = real.TREE
        sentinel = ("git", ds.GIT_ARGV_HIDDEN)
        cases = [
            (f'1 execve("/usr/bin/git", ["git", "show", "{SHA}"], 0x1) = 0', ("git", "show", SHA)),
            (f'execve("/usr/bin/git", ["git", "show"], 0x1) = 0', ("git", "show")),  # 没有进程号前缀
            ('1 execve("/usr/bin/git", ["gitx", "show"], 0x1) = 0', ("gitx", "show")),  # 别名
            ('1 execve("/opt/tools/git", ["/opt/tools/git", "show"], 0x1) = 0', ("/opt/tools/git", "show")),
            ('1 execve("/usr/bin/git", 0x7ffd, 0x1) = 0', sentinel),
            ('1 execve("/usr/bin/git", [], 0x1) = 0', sentinel),
            ('1 execve("/usr/bin/cat", ["cat"], 0x1) = 0', None),
            ('1 execve("/usr/bin/cat", 0x7ffd, 0x1) = 0', None),
            ('1 execve("", ["x"], 0x1) = -1 ENOENT (No such file or directory)', None),  # 空路径的 execve 不会成功，也不是 git
            ('1 execve("/usr/bin/env", ["git", "status"], 0x1) = 0', ("git", "status")),
            (f'1 execveat(AT_FDCWD<{t}>, "git", ["git", "show"], 0x1, 0) = 0', ("git", "show")),  # 相对路径的可执行文件
            (f'1 execveat(AT_FDCWD<{t}>, "/usr/bin/git", ["gitx"], 0x1, 0) = 0', ("gitx",)),
            ('1 execveat(3</usr/bin/git>, "", ["gitx"], 0x1, AT_EMPTY_PATH) = 0', ("gitx",)),
            ('1 execveat(3</usr/bin/git>, "", 0x7ffd, 0x1, AT_EMPTY_PATH) = 0', sentinel),
            ('1 execveat(3</usr/bin/cat>, "", ["cat"], 0x1, AT_EMPTY_PATH) = 0', None),
            ('1 execveat(3</usr/bin/cat>, "", 0x7ffd, 0x1, AT_EMPTY_PATH) = 0', None),
            ('1 execveat(3</a,b/git>, "", ["x"], 0x1, AT_EMPTY_PATH) = 0', ("x",)),  # 注解里带逗号：dirfd 的注解是路径本身，basename 是 git
            ('1 execveat(3, "", ["git", "tag"], 0x1, AT_EMPTY_PATH) = 0', ("git", "tag")),  # 没有注解但 argv[0] 是 git
            ('1 execveat(3</usr/bin/git>, "/usr/bin/cat", ["cat"], 0x1, 0) = 0', None),  # 路径参数非空时看路径参数，不看 dirfd 注解
            (f'1 execveat(3, "{t}/git", ["x"], 0x1, 0) = 0', ("x",)),
            ("1 <... execve resumed>) = 0", None), ('1 openat(AT_FDCWD</x>, "execve(1)", O_RDONLY) = 3</x/execve(1)>', None), ("没有进程号的杂散输出", None),
            ('1 clone(child_stack=NULL, flags=CLONE_VFORK|SIGCHLD <unfinished ...>', None),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(ds.parse_git_execve(line), expected)
        unparsed = [
            '1 execve(0x7ffd1234, 0x1, 0x2) = 0',  # 路径参数不是字符串
            '1 execve(NULL, 0x1, 0x2) = -1 EFAULT (Bad address)',
            f'1 execveat(AT_FDCWD<{t}>, 0x55d, 0x1, 0x2, 0) = 0',
            '1 execveat(3, "", ["sh", "-c", "x"], 0x1, AT_EMPTY_PATH) = 0',  # AT_EMPTY_PATH 没有路径注解，argv[0] 又不是 git：排除不了它是 git
            '1 execveat(3<pipe:[123]>, "", ["sh"], 0x1, AT_EMPTY_PATH) = 0',  # 注解不是绝对路径
            '1 execveat(3<socket:[1]>, "", 0x7ffd, 0x1, AT_EMPTY_PATH) = 0',
            '1 execveat(AT_FDCWD</x>, "", ["sh"], 0x1, AT_EMPTY_PATH) = 0',  # AT_FDCWD 的注解是当前目录，不是可执行文件
            '1 execveat(abc',  # 残缺
            '1 execveat(3</usr/bin/git>, "x"',  # 残缺：路径之后没有 “, ”
        ]
        for line in unparsed:
            with self.subTest(line=line):
                with self.assertRaises(ds.ExecLineUnparsed):
                    ds.parse_git_execve(line)

    def test_unrecognized_exec_forms_fail_closed_with_an_unresolved_reason(self) -> None:
        """S3：行首是 exec 调用却认不出形态，记 sampler:exec-unparsed（带原始行样例），不悄悄漏掉；能认出的形态（包括不是 git 的）不记。"""
        t = real.TREE
        unparsed = ['4243 execve(0x7ffd1234, 0x1, 0x2) = 0', '4243 execveat(3, "", ["sh", "-c", "x"], 0x1, AT_EMPTY_PATH) = 0',
                    f'4243 execveat(AT_FDCWD<{t}>, 0x55d, 0x1, 0x2, 0) = 0', '4243 execveat(3<pipe:[123]>, "", ["sh"], 0x1, AT_EMPTY_PATH) = 0']
        for line in unparsed:
            with self.subTest(line=line):
                document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), [line])
                reasons = document["coverage"]["unresolved_reasons"]
                self.assertEqual(reasons.get("sampler:exec-unparsed"), 1, reasons)
                self.assertEqual(document["coverage"]["unresolved_samples"]["sampler:exec-unparsed"], [line.replace(t, "{TREE}")])
                self.assertEqual(document["git_commands"], [])
                self.assertEqual(document["coverage"]["unresolved"], sum(reasons.values()))
                self.assertEqual(ds.validate_raw(document), [])
        three = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), unparsed[:2] + [unparsed[0]])
        self.assertEqual(three["coverage"]["unresolved_reasons"]["sampler:exec-unparsed"], 3, "每次出现都计数，样例最多 3 条")
        self.assertEqual(len(three["coverage"]["unresolved_samples"]["sampler:exec-unparsed"]), 3)
        # 认得出的形态不记：不是 git 的 exec、argv[0] 是 git 的 AT_EMPTY_PATH（不需要注解）。
        fine = ['4243 execve("/usr/bin/cat", ["cat"], 0x1) = 0', '4243 execveat(3</usr/bin/cat>, "", ["cat"], 0x1, AT_EMPTY_PATH) = 0',
                '4243 execveat(3, "", ["git", "tag"], 0x1, AT_EMPTY_PATH) = 0']
        self.assertNotIn("sampler:exec-unparsed", feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), fine)["coverage"]["unresolved_reasons"])

    def test_non_utf8_bytes_survive_the_cli_and_the_file_stays_ascii(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "tree"
            tree.mkdir()
            line = b'4242 openat(AT_FDCWD<' + str(tree).encode() + b'>, "' + str(tree).encode() + b'/\xffbad.txt", O_RDONLY) = 3<' + \
                   str(tree).encode() + b'/\xffbad.txt>\n'
            output = root / "raw.json"
            command = [sys.executable, "-B", str(ds.HERE), "filter", "--output", str(output), "--unit-id", UNIT]
            for name, value in aliases_for(tree, root).items():
                command += ["--alias", f"{name}={value}"]
            completed = subprocess.run(command, input=line, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            text = output.read_text(encoding="ascii")
            self.assertEqual(json.loads(text)["repo_accesses"], [["{TREE}/\udcffbad.txt", ["read"]]], "按 surrogateescape 读入，往返不丢字节")

    def test_chunk_size_does_not_change_the_result(self) -> None:
        lines = "".join(real.ALL_SEGMENTS).splitlines()
        results = []
        for chunk in (0, 1, 7, 100000):
            document = ds.filter_stream((line + "\n" for line in lines), self.aliases, unit_id=UNIT, chunk_lines=chunk)
            results.append((document["repo_accesses"], [row[:2] for row in document["outside_accesses"]], document["coverage"]))
        self.assertTrue(all(item == results[0] for item in results), "聚合块大小只影响内存，不影响内容（计数除外）")

    def test_git_object_store_reads_collapse_to_one_directory_entry(self) -> None:
        t = real.TREE
        lines = [open_line(t, f"{t}/.git/objects/ab/{'c' * 38}"), open_line(t, f"{t}/.git/objects/pack/pack-1.pack"),
                 open_line(t, f"{t}/.git/HEAD"), open_line(t, f"{t}/.git/packed-refs"), open_line(t, f"{t}/.git/objects/de/{'f' * 38}")]
        paths = dict(feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)["repo_accesses"])
        self.assertEqual(sorted(paths), ["{TREE}/.git/HEAD", "{TREE}/.git/objects/", "{TREE}/.git/packed-refs"],
                         "内容寻址的对象文件聚合到目录级，HEAD 与 packed-refs 这类少量文件仍逐个保留")
        self.assertEqual(paths["{TREE}/.git/objects/"], ["read"])

    def test_filter_ignores_termination_signals_so_a_timeout_still_leaves_a_raw(self) -> None:
        """过滤器与被跟踪命令同一个进程组：超时终止整组时，它不能跟着死，要读到 EOF 后把已有的读集写出来。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw.json"
            argv = ["filter", "--output", str(output), "--unit-id", UNIT]
            for name, value in aliases_for(directory, directory).items():
                argv += ["--alias", f"{name}={value}"]
            with mock.patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b""))), \
                    mock.patch.object(ds.signal, "signal") as handler:
                self.assertEqual(ds.main(argv), 0)
            handler.assert_has_calls([mock.call(signals.SIGTERM, signals.SIG_IGN), mock.call(signals.SIGINT, signals.SIG_IGN)], any_order=True)
            self.assertEqual(ds.load_document(output)["coverage"]["lines"], 0)

    def test_size_guards_turn_into_unresolved_reasons_instead_of_silently_dropping(self) -> None:
        lines = [open_line(real.TREE, f"{real.TREE}/f{i}") for i in range(4)] + [open_line(real.TREE, f"/opt/d{i}/x") for i in range(4)]
        with mock.patch.object(ds, "MAX_REPO_PATHS", 2), mock.patch.object(ds, "OUTSIDE_DIR_LIMIT", 2):
            document = feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT), lines)
        reasons = document["coverage"]["unresolved_reasons"]
        self.assertEqual(reasons["sampler:limit:repo-paths"], 2)
        self.assertGreaterEqual(reasons["sampler:limit:outside-directories"], 1)
        self.assertEqual(len(document["repo_accesses"]), 2, "内存有界")
        self.assertEqual(document["coverage"]["unresolved"], sum(reasons.values()))


# ---------------------------------------------------------------------------
# 合成 git 仓库与 raw 构造
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                            capture_output=True, text=True, check=True)
    return result.stdout


def make_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "tree"
    repo.mkdir()
    git(repo, "init", "-q")
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    return repo.resolve()


BASE_FILES = {".gitignore": "out/\n", "backend/go.mod": "module x\n", "backend/a.go": "package a\n", "docs/egress/foundation/sink-stats.md": "s\n",
              "docs/other.md": "o\n", "tools/x.py": "x = 1\n"}


def good_lines(tree: Path, work: Path) -> list[str]:
    """一份满足全部有效性判据的读集：只读跟踪文件、被忽略目录里的写入、缓存里的读写、工具链、系统文件和 /dev/null。"""
    t, w, cwd = str(tree), str(work), f"{tree}/backend"
    return [open_line(cwd, f"{t}/backend/go.mod"), open_line(cwd, f"{t}/backend/a.go"),
            open_line(cwd, f"{t}/docs/egress/foundation/sink-stats.md"), stat_line(t, f"{t}/backend"), list_line(f"{t}/backend"),
            missing_line(cwd, f"{t}/go.work"), open_line(cwd, "/usr/local/go/src/fmt/print.go"),
            open_line(cwd, f"{w}/go-build-cache/ab/abcd-d"), write_line(cwd, f"{w}/go-build-cache/ab/new-a"),
            write_line(cwd, f"{t}/out/result.bin"), open_line(cwd, "/etc/passwd"), open_line(cwd, "/proc/self/cgroup"),
            open_line(cwd, "/dev/null", "O_WRONLY|O_CLOEXEC"),
            # Go 的 os.Lstat：返回结构被缩略，严格解析记一次 symlink-metadata-not-bound，但路径照常记录——属于容忍集合。
            f'4242 newfstatat(AT_FDCWD<{cwd}>, "{t}/backend/a.go", 0x1, AT_SYMLINK_NOFOLLOW) = 0']


COMMAND = {"launcher": [], "argv": ["go", "test", "./...", "-count=1"], "cwd_rel": "backend"}


def make_raw(tree: Path, work: Path, lines: list[str], *, label: str = "cold", cache: str = "cold", exit_code: int | None = 0,
             signal: int | None = None, timed_out: bool = False, before: tuple = (), after: tuple = (), unit: str = UNIT,
             command: dict | None = None, commit: str | None = None) -> dict:
    document = ds.filter_stream((line + "\n" for line in lines), aliases_for(tree, work), unit_id=unit)
    command = {**(command or COMMAND)}
    command["sha256"] = ds.command_digest(command["argv"], command["cwd_rel"])
    document["run"] = {
        "label": label, "cache": cache, "exit_code": exit_code, "signal": signal, "timed_out": timed_out, "seconds": 1.5,
        "started_utc": "2026-10-14T00:00:00Z", "finished_utc": "2026-10-14T00:00:02Z", "command": command,
        "git_status_before": list(before), "git_status_after": list(after), "commit": commit or git(tree, "rev-parse", "HEAD").strip(),
        "kernel": "6.8.0", "strace": "6.8", "sampler_sha256": "b" * 64, "env_overrides": {}, "docker": None, "filter_wait_seconds": 0.0}
    return document


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tree = make_repo(self.root, BASE_FILES)
        self.work = self.root / "work"
        self.work.mkdir()


class MergeTests(TempCase):
    """T-M1。"""

    def merge(self, *raws: dict, **options) -> dict:
        return ds.merge_raws(list(raws), tree=self.tree, **options)

    def raw(self, lines: list[str], **options) -> dict:
        return make_raw(self.tree, self.work, lines, **options)

    def test_union_tracked_labels_and_a_clean_sample_is_valid(self) -> None:
        t = str(self.tree)
        cold_lines = good_lines(self.tree, self.work)
        warm_lines = cold_lines[:5] + [open_line(t, f"{t}/tools/x.py"), open_line(t, f"{self.work}/go-build-cache/cd/other-d")]
        sample = self.merge(self.raw(cold_lines), self.raw(warm_lines, label="warm", cache="warm"))
        self.assertEqual((sample["valid"], sample["invalid_reasons"]), (True, []))
        self.assertEqual(ds.validate_sample(sample), [])
        self.assertEqual(sample["coverage"]["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 1}, "只有容忍集合里的原因，仍然有效")
        self.assertEqual((sample["coverage"]["unresolved"], sample["coverage"]["lines"]), (1, len(cold_lines) + len(warm_lines)), "各次运行的计数求和")
        repo = {path: (kinds, tracked) for path, kinds, tracked in sample["repo_accesses"]}
        self.assertEqual(repo["{TREE}/backend/go.mod"], (["read"], True))
        self.assertEqual(repo["{TREE}/tools/x.py"], (["read"], True), "仅在热缓存那次出现的路径也要取并集")
        self.assertEqual(repo["{TREE}/backend"], (["list", "stat"], True), "含跟踪文件的目录算 tracked")
        self.assertEqual(repo["{TREE}/out/result.bin"], (["write"], False))
        self.assertEqual(repo["{TREE}/go.work"], (["missing"], False))
        outside = {key: kinds for key, kinds, _count in sample["outside_accesses"]}
        self.assertEqual(outside["{WORK}/go-build-cache/ab/"], ["read", "write"])
        self.assertEqual(outside["{WORK}/go-build-cache/cd/"], ["read"])
        self.assertEqual([item["cache"] for item in sample["sampled"]["runs"]], ["cold", "warm"])
        differences = dict(map(tuple, sample["review"]["run_differences"]["repo"]["items"]))  # type: ignore[arg-type]
        self.assertEqual(differences["{TREE}/tools/x.py"], [1], "冷热差异：只出现在第 2 次")
        self.assertEqual(sample["review"]["writes"]["repo_ignored"], ["out/"])
        self.assertEqual(sample["review"]["writes"]["sinks"], ["/dev/null"])
        self.assertEqual(sample["command"]["sha256"], ds.command_digest(COMMAND["argv"], "backend"))
        path = self.root / "sample.json"
        ds.write_document(path, sample)
        self.assertTrue(path.read_bytes().isascii())
        self.assertEqual(ds.load_document(path), sample, "序列化往返不丢信息")
        with self.assertRaisesRegex(ds.SamplerError, "拒绝覆盖"):
            ds.write_document(path, sample, exclusive=True)

    def test_every_validity_rule_has_a_counterexample(self) -> None:
        t, w = str(self.tree), str(self.work)
        cwd = f"{t}/backend"
        cases = [
            ("exit", {}, {"exit_code": 1}, "[exit]"),
            ("signal", {}, {"exit_code": None, "signal": 9}, "[exit]"),
            ("timeout", {}, {"timed_out": True, "exit_code": None, "signal": 15}, "[exit]"),
            ("git-status-after", {}, {"after": ("?? backend/new.txt",)}, "[git-status]"),
            ("git-status-before", {}, {"before": (" M backend/a.go",)}, "[git-status]"),
            ("unrecognized", {"lines": ["杂散输出"]}, {}, "unrecognized-line"),
            ("unsupported-call", {"lines": ["4242 exit_group(0)                    = ?"]}, {}, "unsupported-call:exit_group"),
            # 容忍 unfinished-calls-at-eof 之后，少量悬着的调用不再使采样无效（多线程进程退出时的正常现象）；反例改成超过上限 64 的 65 个
            # （各属不同进程）。1 个、64 个仍有效、样例上限等见 test_ci_declared_sampler_followup。
            ("unfinished-at-eof-over-cap", {"lines": [f'{5000 + index} openat(AT_FDCWD<{t}>, "x", O_RDONLY <unfinished ...>' for index in range(65)]},
             {}, "unfinished-calls-at-eof×65 超过容忍上限 64"),
            ("untracked-read", {"lines": [open_line(cwd, f"{t}/backend/untracked.txt")]}, {}, "[untracked-read]"),
            ("network-external", {"lines": [connect_line(inet(443, "1.2.3.4"))]}, {}, "[network]"),
            ("network-external-einprogress", {"lines": [connect_line(inet(443, "1.2.3.4"), ") = -1 EINPROGRESS (Operation now in progress)")]}, {},
             "非本机地址"),  # 只看地址不看返回值：连不可达的外部地址同样是 EINPROGRESS
            ("network-unix-unregistered", {"lines": [connect_line('{sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}')]}, {}, "[network]"),
            ("network-sockaddr-still-a-pointer", {"lines": [connect_line("0xef08dd1fe598")]}, {}, "网络目的地不可判"),
            ("network-unix-path-truncated", {"lines": [connect_line('{sa_family=AF_UNIX, sun_path="/var/run/do"...}')]}, {}, "网络目的地不可判"),
            ("connect-resumed-without-start", {"lines": ["4242 <... connect resumed>) = 0"]}, {}, "sampler:connect-resume-without-start"),
            ("connect-unfinished-at-eof", {"lines": ["4242 connect(3<socket:[1]>, " + inet(80, "127.0.0.1") + ", 16 <unfinished ...>"]}, {},
             "sampler:connect-unfinished-at-eof"),
            ("write-tracked", {"lines": [write_line(cwd, f"{t}/backend/a.go")]}, {}, "写入了被跟踪的路径"),
            ("write-unignored", {"lines": [write_line(cwd, f"{t}/backend/scratch.tmp")]}, {}, "未被 .gitignore 忽略"),
            ("write-git", {"lines": [write_line(cwd, f"{t}/.git/index.lock")]}, {}, "写入 .git 内部"),
            ("write-toolchain", {"lines": [write_line(cwd, "/usr/local/go/pkg/x.a")]}, {}, "toolchain 类"),
            ("write-home", {"lines": [write_line(cwd, f"{HOME}/.config/go/telemetry/x.count")]}, {}, "home-config 类"),
            ("write-unclassified", {"lines": [write_line(cwd, "/opt/other/x")]}, {}, "未归类"),
            ("git-argv-hidden", {"lines": ['4242 execve("/usr/bin/git", 0x1, 0x2) = 0']}, {}, "[git-argv]"),
            # 步骤 5 WP4d 的 S3、S1：认不出形态的 exec 调用，和“被当作垃圾丢弃的行，它的 tid 之后又出现了”，都是不可容忍的未解析原因，采样判无效。
            ("exec-unparsed", {"lines": ['4242 execve(0x7ffd1234, 0x1, 0x2) = 0']}, {}, "sampler:exec-unparsed"),
            ("detached-not-terminal", {"lines": ['711353 lsetxattr(0x1, 0x2 <detached ...>', open_line(cwd, f"{t}/backend/a.go", pid=711353)]}, {},
             "sampler:detached-not-terminal:711353"),
            ("no-lines", {"replace": []}, {}, "没有任何 strace 行"),
        ]
        for name, extra, options, expected in cases:
            with self.subTest(name):
                lines = extra.get("replace", good_lines(self.tree, self.work) + extra.get("lines", []))
                sample = self.merge(self.raw(lines, **options))
                self.assertFalse(sample["valid"])
                self.assertEqual(ds.validate_sample(sample), [])
                self.assertTrue(any(expected in reason for reason in sample["invalid_reasons"]), (expected, sample["invalid_reasons"]))
        raw = self.raw(good_lines(self.tree, self.work))
        raw["coverage"]["stream_complete"] = False
        self.assertTrue(any("[stream]" in reason for reason in self.merge(raw)["invalid_reasons"]), "流不完整")

    def test_a_path_git_cannot_evaluate_is_a_violation_not_a_crash(self) -> None:
        """写入路径越过了仓库里的符号链接，git check-ignore 整批返回 128；逐条重试，坏路径判违规，其余照常判。"""
        os.symlink("backend", self.tree / "link")
        git(self.tree, "add", "link")
        git(self.tree, "commit", "-q", "-m", "link")
        t = str(self.tree)
        sample = self.merge(self.raw(good_lines(self.tree, self.work) + [write_line(f"{t}/backend", f"{t}/link/x.tmp")]))
        self.assertFalse(sample["valid"])
        self.assertTrue(any("link/x.tmp" in reason and "未被 .gitignore 忽略" in reason for reason in sample["invalid_reasons"]), sample["invalid_reasons"])
        self.assertEqual(sample["review"]["writes"]["repo_ignored"], ["out/"], "好的那条仍被识别为被忽略")

    def test_registered_allowances_make_the_sample_valid_and_leave_a_trace(self) -> None:
        cwd = f"{self.tree}/backend"
        lines = good_lines(self.tree, self.work) + [connect_line('{sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}'),
                                                    connect_line(inet(5432, "127.0.0.1")),
                                                    write_line(cwd, f"{HOME}/.config/go/telemetry/local/go.count")]
        prefix = ds.parse_class_prefix("{HOME}/.config/go/telemetry/=performance-cache:cache:go-telemetry")
        sample = self.merge(self.raw(lines), allow_unix=["/var/run/docker.sock"], class_prefixes=[prefix])
        self.assertEqual((sample["valid"], sample["invalid_reasons"]), (True, []))
        self.assertEqual(sample["review"]["allowances"], {"unix_sockets": ["/var/run/docker.sock"],
                                                          "class_prefixes": [["{HOME}/.config/go/telemetry/", "performance-cache", "cache:go-telemetry"]]})
        self.assertEqual(sample["review"]["network"]["unix_allowed"], ["/var/run/docker.sock"])
        self.assertEqual(sample["review"]["network"]["loopback"], 1)
        for bad in ("没有等号", "/x=未知类别", "相对路径/=scratch", "/opt/x/=toolchain"):
            with self.assertRaises(ds.SamplerError, msg=bad):
                ds.parse_class_prefix(bad)

    def test_inconsistent_or_unfinished_inputs_are_refused_or_flagged(self) -> None:
        base = self.raw(good_lines(self.tree, self.work))
        with self.assertRaisesRegex(ds.SamplerError, "不同的单元"):
            self.merge(base, self.raw(good_lines(self.tree, self.work), unit="backend:unit"))
        other = {"launcher": [], "argv": ["go", "vet", "./..."], "cwd_rel": "backend"}
        with self.assertRaisesRegex(ds.SamplerError, "命令.*不一致"):
            self.merge(base, self.raw(good_lines(self.tree, self.work), command=other))
        sample = self.merge(base, self.raw(good_lines(self.tree, self.work), commit="c" * 40, label="warm", cache="warm"))
        self.assertTrue(any("[consistency]" in reason and "采样提交" in reason for reason in sample["invalid_reasons"]))
        no_run = {**base, "run": None}
        with self.assertRaisesRegex(ds.SamplerError, "缺少 run 信息"):
            self.merge(no_run)
        with self.assertRaisesRegex(ds.SamplerError, "不合法"):
            self.merge({**base, "unexpected": 1})
        with self.assertRaisesRegex(ds.SamplerError, "至少需要一份"):
            ds.merge_raws([], tree=self.tree)


class DraftTests(TempCase):
    """T-D1。"""

    def setUp(self) -> None:
        super().setUp()
        files = dict(BASE_FILES)
        for index in range(30):
            for directory in ("wide", "sparse", "listed"):
                files[f"{directory}/f{index}.txt"] = "x\n"
        for index in range(25):
            files[f"deep/a/f{index}.txt"] = "x\n"
            files[f"deep/b/f{index}.txt"] = "x\n"
        for index in range(5):
            files[f"hot/f{index}.txt"] = "x\n"
        self.root2 = self.root / "second"
        self.root2.mkdir()
        self.tree = make_repo(self.root2, files)
        self.samples = self.root / "samples"
        self.samples.mkdir()

    def lines(self, extra: list[str] | None = None) -> list[str]:
        t, cwd = str(self.tree), str(self.tree)
        # hot：5 个文件读 1 个（命中率 0.2，只能靠“文件不超过 20 个”的叶子规则进 include）；wide：30 个读 20 个（命中率规则）；
        # sparse：30 个只读 2 个（既不够命中率也超过叶子上限，下钻到单文件）；deep/a 同理。
        reads = (["hot/f0.txt"] + [f"wide/f{i}.txt" for i in range(20)] + ["sparse/f1.txt", "sparse/f9.txt", "deep/a/f3.txt", "backend/go.mod",
                                                                          ".gitignore"])
        return ([open_line(cwd, f"{t}/{rel}") for rel in reads] + [list_line(f"{t}/listed"), list_line(t), stat_line(t, f"{t}/deep"),
                                                                      stat_line(t, f"{t}/deep/b")] + (extra or []))

    def draft(self, lines: list[str], **options) -> tuple[dict, str, dict]:
        sample = ds.merge_raws([make_raw(self.tree, self.work, lines)], tree=self.tree, **options.pop("merge", {}))
        path = self.samples / "backend-go-test.sample.json"
        ds.write_document(path, sample)
        # 这一组用例钉的是“按读集机械归并”本身（命中率、叶子规则、未归类前缀……），样本是合成的，仓库也没有 go.sum 之类登记侧要求的文件，
        # 过不了登记侧的覆盖自检；所以关掉覆盖闭合与自检（步骤 5 的 WP4c 之后 draft 默认开启，开启后的行为见 test_ci_declared_sampler_coverage）。
        registry, review = ds.draft_registry(self.samples, self.tree, **{"coverage_check": False, **options})
        return registry, review, sample

    def test_ranges_follow_hit_ratio_leaf_size_and_listed_directories(self) -> None:
        registry, review, sample = self.draft(self.lines())
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        entry = registry["units"][UNIT]
        repo = entry["scope"]["repo"]
        self.assertEqual(sorted(item["include"][0] for item in repo["ranges"]), ["backend/", "hot/", "listed/", "wide/"],
                         "命中率不低于 0.5、被读过且文件不超过 20、被枚举的目录整体进 include；稀疏目录不进")
        self.assertEqual(repo["files"], [".gitignore", "deep/a/f3.txt", "sparse/f1.txt", "sparse/f9.txt"], "稀疏目录下钻到单文件；根目录文件直接是单文件")
        self.assertEqual((repo["list_ok"], repo["head"]), ([], False), "list_ok 一律留给人决定")
        self.assertEqual(sorted(item["name"] for item in repo["ranges"]), ["backend", "hot", "listed", "wide"])
        self.assertIn("| `wide/` | 30 | 20 | 10 | 0.667 | 否 |", review, "过宽度量：纳入但没被读的文件数")
        self.assertIn("| `hot/` | 5 | 1 | 4 | 0.200 | 否 |", review, "叶子规则：命中率不够但文件很少，整体 include，过宽度量照样列出")
        self.assertIn("按顶层目录汇总", review)
        self.assertIn("| `wide/` | 30 | 20 | 0.667 |", review, "顶层目录汇总：真实读集横跨很多目录时，评审者靠它决定合并粒度")
        self.assertIn("| `sparse/` | 30 | 2 | 0.067 |", review, "没有进 include 的稀疏目录在汇总里也看得到")
        self.assertIn("| `(根目录文件)` | 1 | 1 | 1.000 |", review, "根目录下的文件单独一行")
        self.assertIn("被读文件最多的二级目录", review)
        self.assertIn("| `deep/a/` | 25 | 1 | 0.040 |", review, "二级目录汇总把 deep 拆开看：读取集中在 deep/a")
        self.assertIn("| `deep/` | 50 | 1 | 0.020 |", review)
        self.assertEqual(ds.rollup(["a/b/c.txt", "a/b/d.txt", "a/e.txt", "r.txt"], 2), [("a/b", 2), ("(根目录文件)", 1), ("a", 1)])
        self.assertEqual(ds.rollup(["a/b/c.txt", "a/e.txt"], 1), [("a", 2)])
        self.assertIn("| `listed/` | 30 | 0 | 30 | 0.000 | 是 |", review)
        self.assertIn("根目录，不会整体 include", review, "仓库根被枚举时不整体 include，交给评审")
        self.assertEqual(entry["status"], "declared")
        evidence = self.samples / "backend-go-test.sample.json"
        self.assertEqual(entry["sample"], {
            "evidence": "tools/ci/command_input_samples/backend-go-test.sample.json", "evidence_sha256": ds.file_sha256(evidence),
            "command_sha256": sample["command"]["sha256"], "launcher_sha256": ds.sha256_json([]), "sampled_commit": sample["sampled"]["commit"],
            "sampled_at_utc": "2026-10-14T00:00:02Z", "sampler_sha256": "b" * 64, "runs": 1})

    def test_limits_are_flagged_for_review_but_the_draft_is_still_written(self) -> None:
        with mock.patch.object(ds, "MAX_RANGES", 2), mock.patch.object(ds, "MAX_FILES", 1):
            registry, review, _sample = self.draft(self.lines())
        self.assertEqual(len(registry["units"][UNIT]["scope"]["repo"]["ranges"]), 4)
        self.assertIn("超限待评审", review)

    def test_outside_directories_are_classified_and_the_rest_listed_for_review(self) -> None:
        cwd = str(self.tree)
        extra = [open_line(cwd, "/etc/hosts"), open_line(cwd, "/opt/other/tool"), open_line(cwd, "/var/lib/state"), open_line(cwd, f"{HOME}/.gitconfig"),
                 open_line(cwd, "/usr/local/go/src/fmt/print.go"), open_line(cwd, f"{self.work}/go-mod-cache/m/x"), stat_line(cwd, "/usr/local"),
                 open_line(cwd, "/proc/4242/status"), open_line(cwd, f"{self.work}/go-tmp/go-build1/b001/x.a")]
        registry, review, sample = self.draft(self.lines(extra), merge={})
        scope = registry["units"][UNIT]["scope"]
        outside = {item["prefix"]: item for item in scope["outside"]}
        self.assertEqual(outside["/etc/"], {"prefix": "/etc/", "class": "system"})
        self.assertEqual(outside["/usr/local/go/"], {"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"})
        self.assertEqual(outside["{HOME}/.gitconfig"], {"prefix": "{HOME}/.gitconfig", "class": "home-config", "contract": "host-config:gitconfig"})
        self.assertEqual(outside["{WORK}/go-mod-cache/"]["contract"], "cache:go-mod-cache")
        self.assertEqual(outside["{WORK}/go-tmp/"]["class"], "scratch")
        self.assertNotIn("/opt/other/", outside)
        self.assertEqual(scope["contracts"], sorted({"toolchain:go", "host-config:gitconfig", "cache:go-mod-cache"}))
        self.assertEqual(scope["network"], "loopback-only")
        self.assertIn("未归类 2 项", review)
        self.assertIn("`/opt/other/`", review)
        self.assertIn("`/var/lib/`", review)
        self.assertIn("仅元数据的上层目录探测", review)
        self.assertIn("`/usr/`", review, "只 stat 的上层目录是祖先探测，不算未归类")
        self.assertIn(["/proc/self/status", ["read"], 1], sample["outside_accesses"], "进程号规范成 self")
        self.assertEqual(ds.validate_sample(sample), [])

    def test_git_history_commits_are_extracted_mechanically(self) -> None:
        # 步骤 5 的 WP4b 之后，git 命令里只要出现 HEAD 之类的浮动引用，draft 就把该单元写成 rerun-only（没有 scope，也就没有 git_history）；
        # 所以这里只用固定的提交号（第二条命令原先是 `--is-ancestor b…b HEAD`）。含 HEAD 时的行为见 test_ci_declared_sampler_followup。
        argvs = [git_line(["git", "show", f"{SHA}:docs/egress/a.json"]), git_line(["git", "merge-base", "--is-ancestor", "b" * 40, "c" * 40])]
        registry, review, _sample = self.draft(self.lines(argvs))
        history = registry["units"][UNIT]["scope"]["repo"]["git_history"]
        self.assertEqual(history["commits"], [SHA, "b" * 40, "c" * 40])
        self.assertEqual(registry["units"][UNIT]["status"], "declared", "只用固定的提交号，git 用法固定，保持 declared")

    def test_an_invalid_sample_becomes_rerun_only_with_the_reason(self) -> None:
        registry, review, sample = self.draft(self.lines([write_line(str(self.tree), f"{self.tree}/wide/f1.txt")]))
        self.assertFalse(sample["valid"])
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("采样无效", entry["reason"])
        self.assertIn("写入了被跟踪的路径", entry["reason"])
        self.assertNotIn("scope", entry)
        self.assertIn("采样有效：**否**", review)

    def test_review_lists_every_required_section(self) -> None:
        _registry, review, _sample = self.draft(self.lines())
        for heading in ("### 1 采样有效性", "### 2 过宽度量", "### 3 被 list 枚举的目录", "### 4 写入集合", "### 5 untracked_reads",
                        "### 6 network", "### 7 冷热差异", "### 8 git status 前后", "### 9 unresolved_reasons", "### 10 仓库外目录归类",
                        "### 11 git 历史与其它提示", "## 总览", "不做任何语义判断的自动放行"):
            self.assertIn(heading, review)

    def test_draft_command_line_writes_the_registry_and_the_review(self) -> None:
        self.draft(self.lines())
        registry, review = self.root / "registry.json", self.root / "review.md"
        stdout = subprocess.run([sys.executable, "-B", str(ds.HERE), "draft", "--samples", str(self.samples), "--tree", str(self.tree),
                                 "--registry-out", str(registry), "--review-out", str(review)], capture_output=True, text=True)
        self.assertEqual(stdout.returncode, 0, stdout.stderr)
        self.assertEqual(json.loads(stdout.stdout), {"declared": 1, "rerun_only": 0, "units": 1})
        self.assertEqual(json.loads(registry.read_text())["schema_version"], ds.REGISTRY_SCHEMA)
        self.assertIn("# 声明级承接采样评审清单", review.read_text(encoding="utf-8"))
        (self.samples / "wrong-name.sample.json").write_text((self.samples / "backend-go-test.sample.json").read_text())
        with self.assertRaisesRegex(ds.SamplerError, "文件名必须是"):
            ds.draft_registry(self.samples, self.tree)


# ---------------------------------------------------------------------------
# run：假 strace、假 launcher、假 docker
# ---------------------------------------------------------------------------

FAKE_STRACE = """#!@PYTHON@
# 假 strace（测试用）：--version 照实回答；否则把 FAKE_STRACE_LINES 文件的内容经 -o 给的管道交给过滤器，记录自己看到的
# argv 与环境，再在原工作目录运行 -- 之后的命令，以它的退出码退出。
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("strace -- version 6.8")
    sys.exit(0)
pipe = args[args.index("-o") + 1]
command = args[args.index("--") + 1:]
if os.environ.get("FAKE_STRACE_RECORD"):
    keys = ("UNIT_EXECUTOR_UNIT", "UNIT_EXECUTOR_KIND", "UNIT_EXECUTOR_CORES", "GOMAXPROCS", "GOCACHE", "GOMODCACHE", "GOTMPDIR", "FAKE_LAUNCHER",
            "GOPROXY", "PYTHONDONTWRITEBYTECODE", "UNIT_EXECUTOR_RECORDS_MODULE", "UNIT_EXECUTOR_PARENT_STATE_DIR")
    json.dump({"args": args, "env": {key: os.environ.get(key) for key in keys}}, open(os.environ["FAKE_STRACE_RECORD"], "w"))
data = open(os.environ["FAKE_STRACE_LINES"], "rb").read() if os.environ.get("FAKE_STRACE_LINES") else b""
if not os.environ.get("FAKE_STRACE_SKIP_PIPE"):
    subprocess.run(["/bin/sh", "-c", pipe[1:]], input=data, check=True)
sys.exit(subprocess.run(command).returncode)
"""
# 假 launcher：与 ARM64 的 unshare 前缀一样，先设一个环境变量再 exec 后面的命令。它在 strace 外侧时，strace 才看得到这个变量。
FAKE_LAUNCHER = '#!/bin/sh\nFAKE_LAUNCHER=1 exec "$@"\n'
FAKE_DOCKER = """#!/bin/sh
case "$1" in
  ps)
    n=$(cat "$FAKE_DOCKER_STATE" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_DOCKER_STATE"
    echo "aaa111 postgres:old"
    if [ "$n" -ge 1 ]; then echo "bbb222 postgres:18.1-alpine3.23"; fi ;;
  events)
    echo '{"Type":"container","Action":"create","Actor":{"ID":"bbb222","Attributes":{"image":"postgres:18.1-alpine3.23"}}}'
    exec sleep 30 ;;
esac
"""


def install(path: Path, text: str) -> Path:
    path.write_text(text.replace("@PYTHON@", sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class RunTests(TempCase):
    """T-R1。"""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        install(self.bin / "strace", FAKE_STRACE)
        self.launcher = install(self.bin / "fake-launcher", FAKE_LAUNCHER)
        (self.work / "go-mod-cache").mkdir()
        self.out = self.root / "raw"
        self.record = self.root / "record.json"
        lines_file = self.root / "fake-lines.txt"
        lines_file.write_text("\n".join(good_lines(self.tree, self.work)) + "\n", encoding="utf-8")
        self.env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
                    "FAKE_STRACE_LINES": str(lines_file), "FAKE_STRACE_RECORD": str(self.record)}
        self.manifest = self.root / "gates-manifest.json"

    def write_manifest(self, command: list[str], *, launcher: list[str] | None = None, **unit) -> None:
        launcher = [str(self.launcher)] if launcher is None else launcher
        item = {"unit_id": UNIT, "argv": [*launcher, *command], "cwd": str(self.tree / "backend"), "cores": 0.7, "memory_mb": 5120,
                "timeout_seconds": 3600, "weight": 1.0, "inputs": {}, "reservation_restartable": True, "env": {"GOMAXPROCS": "2"}, **unit}
        self.manifest.write_text(json.dumps({"schema_version": ds.GATES_SCHEMA, "test_groups": [], "units": [item],
                                             "gates": [{"gate_id": "g", "units": [UNIT]}]}), encoding="utf-8")

    def run_once(self, cache: str = "cold", **options) -> tuple[int, Path, dict]:
        settings = dict(manifest=self.manifest, unit_id=UNIT, out_dir=self.out, tree=self.tree, work=self.work, cache=cache, label=None,
                        launcher_json=json.dumps([str(self.launcher)]), env_overrides={}, docker_hooks=False, timeout_factor=2.5,
                        chunk_lines=1000, base_env=self.env)
        settings.update(options)
        return ds.run_sample(**settings)

    def test_strace_argv_puts_the_launcher_outside_and_the_command_inside(self) -> None:
        argv = ds.build_strace_argv(launcher=["unshare", "-m"], strace="/usr/bin/strace", pipe="python3 -B s.py filter", cwd="/t/backend",
                                    command=["go", "test", "./..."])
        self.assertEqual(argv[:3], ["unshare", "-m", "/usr/bin/strace"], "launcher 在 strace 外侧")
        self.assertEqual(argv[argv.index("--") + 1:], ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', "/t/backend", "go", "test", "./..."])
        self.assertEqual(argv[argv.index("-o") + 1], "|python3 -B s.py filter")
        options = list(ds.STRACE_OPTIONS)
        self.assertIn("-f", options)
        self.assertIn("-y", options)
        self.assertIn("--seccomp-bpf", options)
        self.assertEqual(options[options.index("-s") + 1], "4096")
        self.assertIn("verbose=execve,execveat,connect", options, "execve 的 argv 与 connect 的地址都要能看见（verbose=none 时两者都是指针）")
        self.assertNotIn("verbose=none", options)
        self.assertIn("connect", options[options.index("-e", options.index("signal=none")) + 1].split(","), "connect 在跟踪集合里")
        with self.assertRaisesRegex(ds.SamplerError, "不能交给 strace 的管道"):
            ds.filter_pipe(output=Path("/a b/raw.json"), unit_id=UNIT, aliases=aliases_for("/t", "/w"), chunk_lines=10)

    def test_launcher_is_split_from_the_manifest_command_or_refused(self) -> None:
        command = ["go", "test", "./..."]
        self.assertEqual(ds.split_launcher([*ds.KNOWN_LAUNCHER, *command], None), (ds.KNOWN_LAUNCHER, command))
        self.assertEqual(ds.split_launcher(command, None), ([], command))
        self.assertEqual(ds.split_launcher(["a", *command], json.dumps(["a"])), (["a"], command))
        for argv, given, message in ((["unshare", "-m", *command], None, "不是已知的启动前缀"), (command, json.dumps(["unshare"]), "不以"),
                                     (["a"], json.dumps(["a"]), "没有命令"), (command, "不是 JSON", "不是合法 JSON"), (command, "[1]", "非空字符串")):
            with self.assertRaisesRegex(ds.SamplerError, message):
                ds.split_launcher(argv, given)

    def test_environment_matches_what_the_scheduler_gives_a_command_unit(self) -> None:
        self.write_manifest(["go", "test"])
        unit = ds.load_unit(self.manifest, UNIT)
        env = ds.build_environment(base={"PATH": "/bin", "GOPROXY": "https://proxy"}, unit=unit, work=self.work, out_dir=self.out,
                                   overrides={"GOPROXY": "off"})
        self.assertEqual((env["UNIT_EXECUTOR_CORES"], env["GOMAXPROCS"]), ("1", "2"), "核数向上取整；清单自带的 GOMAXPROCS 优先")
        self.assertEqual((env["UNIT_EXECUTOR_UNIT"], env["UNIT_EXECUTOR_KIND"], env["PYTHONDONTWRITEBYTECODE"]), (UNIT, "formal", "1"))
        self.assertEqual(Path(env["UNIT_EXECUTOR_RECORDS_MODULE"]), ds.HERE.with_name("unit_records.py"))
        self.assertEqual((env["GOCACHE"], env["GOMODCACHE"], env["GOTMPDIR"]),
                         (f"{self.work}/go-build-cache", f"{self.work}/go-mod-cache", f"{self.work}/go-tmp"))
        self.assertEqual(env["GOPROXY"], "off", "--env 覆盖最后生效")
        for base, overrides in (({"GOCACHE": "/elsewhere"}, {}), ({}, {"GOCACHE": "/x"})):
            with self.assertRaises(ds.SamplerError):
                ds.build_environment(base=base, unit=unit, work=self.work, out_dir=self.out, overrides=overrides)

    def test_run_merge_and_draft_chain_works_end_to_end_with_the_fake_strace(self) -> None:
        self.write_manifest(["/bin/sh", "-c", 'echo hot > "$GOCACHE/hot.txt"'])
        code, raw_path, summary = self.run_once("cold")
        self.assertEqual((code, summary["unresolved"], summary["git_status_after"]), (0, 1, 0), summary)
        raw = ds.load_document(raw_path)
        self.assertEqual(ds.validate_raw(raw), [])
        run = raw["run"]
        self.assertEqual(run["command"], {"launcher": [str(self.launcher)], "argv": ["/bin/sh", "-c", 'echo hot > "$GOCACHE/hot.txt"'],
                                         "cwd_rel": "backend", "sha256": ds.command_digest(["/bin/sh", "-c", 'echo hot > "$GOCACHE/hot.txt"'], "backend")})
        self.assertEqual((run["cache"], run["label"], run["exit_code"], run["signal"], run["timed_out"]), ("cold", "cold", 0, None, False))
        self.assertEqual((run["git_status_before"], run["git_status_after"], run["strace"], run["env_overrides"]), ([], [], "6.8", {}))
        self.assertEqual(run["sampler_sha256"], ds.file_sha256(ds.HERE))
        self.assertEqual(raw["aliases"]["TREE"], str(self.tree))
        seen = json.loads(self.record.read_text())
        self.assertEqual(seen["env"]["FAKE_LAUNCHER"], "1", "假 launcher 先于 strace 运行：launcher 确实在 strace 外侧")
        self.assertEqual(seen["env"]["UNIT_EXECUTOR_UNIT"], UNIT)
        self.assertEqual((seen["env"]["UNIT_EXECUTOR_CORES"], seen["env"]["GOMAXPROCS"]), ("1", "2"))
        self.assertEqual(seen["env"]["GOCACHE"], str(self.work / "go-build-cache"))
        self.assertEqual(seen["args"][seen["args"].index("--") + 1:],
                         ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', str(self.tree / "backend"), "/bin/sh", "-c", 'echo hot > "$GOCACHE/hot.txt"'],
                         "被跟踪的是切目录再 exec 清单命令的 sh，命令取自清单")
        self.assertTrue((self.work / "go-build-cache" / "hot.txt").is_file(), "命令确实在单元环境里运行了")
        code, warm_path, _summary = self.run_once("warm")
        self.assertEqual(code, 0)
        sample = ds.merge_raws([ds.load_document(raw_path), ds.load_document(warm_path)], tree=self.tree)
        self.assertEqual((sample["valid"], sample["invalid_reasons"]), (True, []))
        self.assertEqual([item["cache"] for item in sample["sampled"]["runs"]], ["cold", "warm"])
        samples = self.root / "samples"
        samples.mkdir()
        ds.write_document(samples / "backend-go-test.sample.json", sample)
        # 假 strace 产出的是合成读集，登记侧的覆盖自检（WP4c 起默认开启）不认它；这里验证的是 run→merge→draft 的链路，所以关掉自检。
        registry, _review = ds.draft_registry(samples, self.tree, coverage_check=False)
        self.assertEqual(registry["units"][UNIT]["status"], "declared")
        self.assertEqual(registry["units"][UNIT]["sample"]["launcher_sha256"], ds.sha256_json([str(self.launcher)]))
        self.assertEqual(registry["units"][UNIT]["sample"]["runs"], 2)
        completed = subprocess.run([sys.executable, "-B", str(ds.HERE), "merge", "--raw", str(raw_path), "--raw", str(warm_path), "--tree",
                                    str(self.tree), "--out", str(self.root / "from-cli.sample.json")], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary_line = json.loads(completed.stdout)
        self.assertEqual((summary_line["unit_id"], summary_line["valid"], summary_line["invalid_reasons"]), (UNIT, True, []))
        self.assertEqual(ds.load_document(self.root / "from-cli.sample.json"), sample, "命令行与函数调用产出同一份证据")

    def test_cold_cache_renames_a_non_empty_directory_and_warm_requires_one(self) -> None:
        self.write_manifest(["/bin/sh", "-c", 'echo hot > "$GOCACHE/hot.txt"'])
        with self.assertRaisesRegex(ds.SamplerError, "热缓存要求"):
            self.run_once("warm")
        self.assertFalse(self.out.exists() and any(self.out.iterdir()), "校验失败不留下任何产物")
        self.assertEqual(self.run_once("cold")[0], 0)
        code, _path, summary = self.run_once("cold", label="cold2")
        self.assertEqual(code, 0)
        renamed = [path for path in self.work.iterdir() if path.name.startswith("go-build-cache.replaced-")]
        self.assertEqual(len(renamed), 1, "旧的非空 GOCACHE 只改名、不删除")
        self.assertTrue(summary["notes"] and "未删除" in summary["notes"][0])
        self.assertTrue((renamed[0] / "hot.txt").is_file())
        self.assertEqual(sorted(path.name for path in (self.work / "go-build-cache").iterdir()), ["hot.txt"], "新目录里只有第二次运行写的内容")

    def test_unclean_trees_duplicate_labels_and_unknown_units_are_refused(self) -> None:
        self.write_manifest(["/bin/sh", "-c", "exit 0"])
        self.assertEqual(self.run_once("cold")[0], 0)
        with self.assertRaisesRegex(ds.SamplerError, "已存在"):
            self.run_once("cold")
        (self.tree / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ds.SamplerError, "运行前就不干净"):
            self.run_once("warm")
        (self.tree / "stray.txt").unlink()
        with self.assertRaisesRegex(ds.SamplerError, "没有命令单元"):
            self.run_once("warm", unit_id="backend:nope")
        with self.assertRaisesRegex(ds.SamplerError, "label"):
            self.run_once("warm", label="带 空格")
        self.write_manifest(["/bin/sh", "-c", "exit 0"], cwd="/elsewhere")
        with self.assertRaisesRegex(ds.SamplerError, "不在采样树内"):
            self.run_once("warm")

    def test_a_command_that_dirties_the_tree_is_recorded_and_invalidates_the_sample(self) -> None:
        self.write_manifest(["/bin/sh", "-c", "touch leftover.txt"])
        code, raw_path, summary = self.run_once("cold")
        self.assertEqual((code, summary["git_status_after"]), (0, 1))
        raw = ds.load_document(raw_path)
        self.assertEqual(raw["run"]["git_status_after"], ["?? backend/leftover.txt"])
        sample = ds.merge_raws([raw], tree=self.tree)
        self.assertTrue(any("[git-status]" in reason for reason in sample["invalid_reasons"]))

    def test_exit_code_timeout_and_missing_filter_output(self) -> None:
        self.write_manifest(["/bin/sh", "-c", "exit 3"])
        code, raw_path, summary = self.run_once("cold")
        self.assertEqual((code, ds.load_document(raw_path)["run"]["exit_code"]), (1, 3))
        self.write_manifest(["/bin/sh", "-c", "sleep 20"], timeout_seconds=1)
        code, raw_path, _summary = self.run_once("cold", label="timeout", timeout_factor=1.0)
        run = ds.load_document(raw_path)["run"]
        self.assertEqual((code, run["timed_out"], run["exit_code"], run["signal"]), (1, True, None, 15))
        self.assertLess(run["seconds"], 15, "整个进程组被终止，不等命令自己结束")
        sample = ds.merge_raws([ds.load_document(raw_path)], tree=self.tree)
        self.assertTrue(any("[exit]" in reason and "超时" in reason for reason in sample["invalid_reasons"]))
        self.write_manifest(["/bin/sh", "-c", "exit 0"])
        self.env["FAKE_STRACE_SKIP_PIPE"] = "1"
        with mock.patch.object(ds, "FILTER_WAIT_SECONDS", 0.5):
            code, _path, summary = self.run_once("cold", label="nofilter")
        self.assertEqual(code, 3)
        self.assertIn("过滤器没有产出", summary["error"])
        self.write_manifest(["/bin/sh", "-c", "sleep 20"], timeout_seconds=1)
        with mock.patch.object(ds, "FILTER_WAIT_SECONDS", 0.5):
            code, _path, summary = self.run_once("cold", label="timeout-nofilter", timeout_factor=1.0)
        self.assertEqual(code, 1, "超时且没有 raw 不是过滤器故障")
        self.assertIn("超时", summary["error"])

    def test_docker_hooks_report_new_containers_and_tolerate_a_missing_docker(self) -> None:
        self.write_manifest(["/bin/sh", "-c", "exit 0"])
        install(self.bin / "docker", FAKE_DOCKER)
        self.env["FAKE_DOCKER_STATE"] = str(self.root / "docker-state")
        code, raw_path, _summary = self.run_once("cold", docker_hooks=True)
        self.assertEqual(code, 0)
        docker = ds.load_document(raw_path)["run"]["docker"]
        wanted = [{"id": "bbb222", "image": "postgres:18.1-alpine3.23"}]
        self.assertEqual(docker, {"enabled": True, "error": None, "new_containers": wanted, "created": wanted})
        # 第二次：真的没有 docker。不能只删掉 self.bin 里的假 docker——PATH 后面的真实目录里要是装了 docker（GitHub 的 ubuntu runner 自带 /usr/bin/docker），
        # 采样器照样找得到它，run.docker.error 就是 None，用例在装了 docker 的机器上会红（本机 Mac 没有 docker 所以一直绿）。做法与机器上有没有 docker 无关：
        # 子进程环境的 PATH 只留 self.bin（里面是假 strace 与假 launcher，没有 docker；run 本身用到的 sh、python 都是绝对路径，不靠 PATH）。仍然是真的
        # 子进程、真的 PATH 查找——只是这个 PATH 上解析不出 docker。
        (self.bin / "docker").unlink()
        no_docker = {**self.env, "PATH": str(self.bin)}
        self.assertIsNone(shutil.which("docker", path=no_docker["PATH"]), "前提：这个 PATH 上没有 docker")
        self.assertEqual(sorted(path.name for path in self.bin.iterdir()), ["fake-launcher", "strace"], "PATH 上只有假 strace 与假 launcher")
        code, raw_path, _summary = self.run_once("cold", label="no-docker", docker_hooks=True, base_env=no_docker)
        self.assertEqual(code, 0)
        docker = ds.load_document(raw_path)["run"]["docker"]
        self.assertTrue(docker["error"], "Docker 不可用只记录错误，不中断采样")
        self.assertEqual((docker["enabled"], docker["new_containers"], docker["created"]), (True, [], []))

    def test_command_line_exit_codes_and_error_messages(self) -> None:
        self.write_manifest(["/bin/sh", "-c", "exit 0"])
        common = ["run", "--manifest", str(self.manifest), "--out", str(self.out), "--tree", str(self.tree), "--work", str(self.work)]
        completed = subprocess.run([sys.executable, "-B", str(ds.HERE), *common, "--unit", "backend:nope", "--launcher-json", json.dumps([str(self.launcher)])],
                                   capture_output=True, text=True, env=self.env)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("declared_sampler 拒绝：清单里没有命令单元", completed.stderr)
        completed = subprocess.run([sys.executable, "-B", str(ds.HERE), "merge", "--raw", str(self.root / "none.json"), "--tree", str(self.tree),
                                    "--out", str(self.root / "s.json")], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
