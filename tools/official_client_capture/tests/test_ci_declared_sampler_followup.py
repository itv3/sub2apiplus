"""读集采样器（``tools/ci/declared_sampler.py``）的后续改进：步骤 5 的 WP4b。

十几个真实批量采样暴露的缺口，在这里逐条钉住（旧用例见 ``test_ci_declared_sampler.py``，一字不依赖这里）：

* 非 Go 单元的热缓存：``run --no-go-cache``；
* ``draft`` 写出 scope 顶层 ``unix_sockets``；
* ``merge --installed-dir``：依赖安装目录（node_modules）下的读取不再算未跟踪读取；
* ``merge --transient``：命令自产的暂存路径（eslintcache、vitest 随机名临时文件）写后读回放行；
* 无效原因旁保留未解析行的原始样例（别名化、截断），悬着调用的起始行样例；
* ``unfinished-calls-at-eof`` 的容忍（精确匹配，合并后总数上限 64）；
* ``draft`` 按合同补入依赖锁文件（go.mod、go.sum、pnpm-lock.yaml、package.json）；
* ``draft`` 对未固定的 git 用法（HEAD、引用、分支、标签，tag／worktree 等子命令，参数不可见）自动写成 rerun-only 并带 head: true；
* 与登记侧（``unit_records``）互操作：常量与取值规则两边一致，前端式草案端到端被登记侧的 scope 校验与覆盖检查接受。

所有新增的 raw／sample／review 键都是可选的：不给新选项时输出与从前逐字相同，旧文件缺新键照样通过校验。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import declared_sampler as ds
from tools.official_client_capture.tests import declared_sampler_trace_fixture as real
from tools.official_client_capture.tests.test_ci_declared_sampler import (
    BASE_FILES, FAKE_LAUNCHER, FAKE_STRACE, SHA, TempCase, UNIT, connect_line, feed_all, git_line, good_lines, install, list_line, make_raw,
    make_repo, open_line, stat_line, write_line)

SHA2 = "b" * 40
SHA3 = "c" * 40

# 合成仓库：在旧测试的 BASE_FILES 之上补 Makefile、go.sum 和前端的 package.json、锁文件、两个源文件；.gitignore 多忽略一个依赖目录。
FOLLOWUP_FILES = {
    **BASE_FILES,
    ".gitignore": "out/\nignored-deps/\n",
    "Makefile": "all:\n",
    "backend/go.sum": "x v1 h1:abc\n",
    "frontend/package.json": "{}\n",
    "frontend/pnpm-lock.yaml": "lockfileVersion: 9\n",
    "frontend/src/a.ts": "export {}\n",
    "frontend/src/b.ts": "export {}\n",
    # 垫上 25 个没被读到的跟踪文件：frontend/ 的命中率低、文件数超过叶子上限，draft 不会把整个 frontend/ 一口吞成 range，
    # 而是下钻到子目录——被枚举的 node_modules 才有机会成为 range，用来证明“安装目录里被枚举的目录不进 range”这条规则真起作用。
    **{f"frontend/pad/f{index}.txt": "x\n" for index in range(25)},
}
NODE_MODULES = "frontend/node_modules/"
NODE_CONTRACT = "cache:node-modules"


def unfinished_lines(tree: object, count: int, *, first_pid: int = 5000) -> list[str]:
    """count 个各属不同进程、流结束时仍悬着的 openat（多线程进程退出时 strace 留下的那种）。"""
    return [f'{first_pid + index} openat(AT_FDCWD<{tree}>, "x", O_RDONLY <unfinished ...>' for index in range(count)]


# ---------------------------------------------------------------------------
# 容忍集合、审计器接口、别名文本替换
# ---------------------------------------------------------------------------


class ToleranceTests(unittest.TestCase):
    def test_exact_and_prefix_matching_follow_the_registry_side_convention(self) -> None:
        """以 : 结尾的项前缀匹配，其余精确匹配（与登记侧一致）：名字相近的别种原因蹭不上 unfinished-calls-at-eof 的豁免。"""
        self.assertEqual(ds.DECLARED_TOLERATED_UNRESOLVED, ("symlink-metadata-not-bound:", "unfinished-calls-at-eof"))
        self.assertEqual(ds.DECLARED_MAX_UNFINISHED_AT_EOF, 64)
        for reason in ("symlink-metadata-not-bound:newfstatat", "symlink-metadata-not-bound:statx", "unfinished-calls-at-eof"):
            self.assertTrue(ds.reason_tolerated(reason), reason)
        for reason in ("unfinished-calls-at-eof-extra", "x-unfinished-calls-at-eof", "unfinished-calls-at-eof:openat", "sampler:connect-unfinished-at-eof",
                       "symlink-metadata-not-bound", "unrecognized-line", "deferred-process-events-at-eof", "resume-without-start:wait4"):
            self.assertFalse(ds.reason_tolerated(reason), reason)

    def test_sample_limits_match_the_registry_side_type_checks(self) -> None:
        """登记侧对 coverage.unfinished_samples 只核类型与长度：最多 8 条、每条 ≤300 字符；这里的上限不能比它大。"""
        self.assertEqual((ds.MAX_UNFINISHED_SAMPLES, ds.SAMPLE_LINE_CHARS), (8, 300))
        self.assertEqual((ds.MAX_SAMPLES_PER_REASON, ds.MAX_SAMPLE_REASONS, ds.INVALID_REASON_CHARS), (3, 20, 600))


class ParserSurfaceTests(unittest.TestCase):
    """T-API 的补充：诊断样例依赖的审计器内部形状（pending 的值、deferred 的结构）在这里钉住，审计器改了会立刻红。"""

    def test_pending_holds_the_call_start_and_deferred_holds_buffered_lines(self) -> None:
        parser = ds.read_audit_module().TraceParser(strict=True)
        parser.feed('7 openat(AT_FDCWD</r>, "x", O_RDONLY|O_CLOEXEC <unfinished ...>\n')
        self.assertEqual(dict(parser.pending), {"7": 'openat(AT_FDCWD</r>, "x", O_RDONLY|O_CLOEXEC '}, "起始部分＝进程号之后、<unfinished ...> 之前")
        self.assertEqual(parser.unresolved, 0)
        parser.feed("8 <... wait4 resumed>) = 1\n")
        self.assertEqual((parser.unresolved, parser.unresolved_reasons), (1, {"resume-without-start:wait4": 1}), "unresolved 是总计数，随原因同步增加")
        buffered = ds.read_audit_module().TraceParser(strict=True)
        for line in ['1 getcwd("/repo/a/b", 4096) = 10', '1 clone(child_stack=NULL, flags=CLONE_VFORK|SIGCHLD <unfinished ...>',
                     '2 chdir("../..") = 0', '2 execve("./bin/check", ["check"], 0x1) = 0']:
            buffered.feed(line + "\n")
        self.assertEqual({pid: list(lines) for pid, lines in buffered.deferred.items()},
                         {"2": ['2 chdir("../..") = 0', '2 execve("./bin/check", ["check"], 0x1) = 0']},
                         "没有父进程返回行时，子进程的相对路径调用连同它之后的行都按原样缓存")
        self.assertEqual(buffered.deferred_count, 2)


class AliasTextTests(unittest.TestCase):
    def test_absolute_roots_become_aliases_only_on_whole_path_prefixes(self) -> None:
        table = ds.AliasTable({"TREE": "/var/tmp/x/tree", "WORK": "/var/tmp/x", "HOME": "/home/h", "TMP": "/tmp/t"})
        cases = {
            '3 openat(AT_FDCWD</var/tmp/x/tree/backend>, "/var/tmp/x/tree/a.go", O_RDONLY) = 3</var/tmp/x/tree/a.go>':
                '3 openat(AT_FDCWD<{TREE}/backend>, "{TREE}/a.go", O_RDONLY) = 3<{TREE}/a.go>',
            "/var/tmp/x/tree": "{TREE}",
            "/var/tmp/x/go-build-cache/ab/c-d": "{WORK}/go-build-cache/ab/c-d",
            "/var/tmp/x/tree-other/f": "{WORK}/tree-other/f",        # TREE 的前缀后面紧跟 - ，不是整段前缀；落到更短的 WORK 上
            "/var/tmp/x/tree.bak": "{WORK}/tree.bak",
            "/y/var/tmp/x/tree/a": "/y/var/tmp/x/tree/a",            # 前面紧跟 / ，是别的路径的尾部
            "/home/h/.config/go/env": "{HOME}/.config/go/env",
            "/usr/local/go/bin/go": "/usr/local/go/bin/go",
            "": "",
        }
        for text, expected in cases.items():
            self.assertEqual(table.apply_text(text), expected, text)


# ---------------------------------------------------------------------------
# filter：未解析原因的原始行样例、悬着调用样例
# ---------------------------------------------------------------------------


class SampleFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.aliases = {"TREE": real.TREE, "WORK": real.ROOT, "HOME": "/root", "TMP": "/tmp"}
        self.t = real.TREE

    def feed(self, lines: list[str], **options) -> dict:
        return feed_all(ds.StreamAggregator(self.aliases, unit_id=UNIT, **options), lines)

    def test_unresolved_line_sample_is_kept_aliased_and_truncated(self) -> None:
        """lsetxattr 那次偶发就因为没留样例而无从查起：现在每个不容忍的原因留前几条原始行，采样根换成别名，截到 300 字符。"""
        long_name = "n" * 400
        line = f'7001 lsetxattr("{self.t}/backend/{long_name}"..., "user.xdg.origin.url", "value", 5, 0) = 0'
        coverage = self.feed([line])["coverage"]
        self.assertEqual(coverage["unresolved_reasons"], {"plain-path-not-resolved:lsetxattr": 1, "result-or-string-incomplete:lsetxattr": 1})
        samples = coverage["unresolved_samples"]
        self.assertEqual(sorted(samples), sorted(coverage["unresolved_reasons"]), "每个原因各有样例")
        sample = samples["plain-path-not-resolved:lsetxattr"][0]
        self.assertTrue(sample.startswith('7001 lsetxattr("{TREE}/backend/nnnn'), sample[:80])
        self.assertEqual(len(sample), ds.SAMPLE_LINE_CHARS, "超长的行截到 300 字符")
        self.assertNotIn(real.ROOT, sample, "样例里不含采样时的具体绝对路径")
        self.assertEqual(samples["plain-path-not-resolved:lsetxattr"], samples["result-or-string-incomplete:lsetxattr"], "同一行引入的两个原因共用这条样例")
        self.assertEqual(ds.validate_raw(self.feed([line])), [])
        self.assertNotIn("unfinished_samples", coverage, "没有悬着的调用时不写这个可选键")

    def test_resumed_line_sample_carries_the_call_start(self) -> None:
        """多线程下 lsetxattr 被拆成 unfinished 与 resumed 两行；引入原因的是 resumed 行，样例要带上起始行里的路径与参数。"""
        lines = [f'7001 lsetxattr("{self.t}/backend/f"..., "user.x", "v", 1, 0 <unfinished ...>',
                 open_line(self.t, f"{self.t}/backend/go.mod", pid=7002),
                 "7001 <... lsetxattr resumed>) = 0"]
        coverage = self.feed(lines)["coverage"]
        self.assertEqual(coverage["unresolved_samples"]["plain-path-not-resolved:lsetxattr"],
                         ['7001 lsetxattr("{TREE}/backend/f"..., "user.x", "v", 1, 0 ) = 0'], "起始部分＋resumed 之后的尾巴，与解析器自己的拼法一致")
        bare = self.feed(["8 <... wait4 resumed>) = 1"])["coverage"]
        self.assertEqual(bare["unresolved_samples"], {"resume-without-start:wait4": ["8 <... wait4 resumed>) = 1"]}, "没有起始部分就是这一行本身")
        # 路径里碰巧含有 "resumed>" 的普通行不是 resumed 行，不能被当成拼接对象。
        tricky = [f'9 openat(AT_FDCWD<{self.t}>, "x", O_RDONLY <unfinished ...>',
                  '9 frobnicate("/srv/<... openat resumed>") = 0']
        self.assertEqual(self.feed(tricky)["coverage"]["unresolved_samples"], {"unsupported-call:frobnicate": ['9 frobnicate("/srv/<... openat resumed>") = 0']})

    def test_samples_are_capped_per_reason_and_in_reason_count(self) -> None:
        many_lines = [f"4242 xcall(1) = {index}" for index in range(7)]
        samples = self.feed(many_lines)["coverage"]["unresolved_samples"]
        self.assertEqual(samples, {"unsupported-call:xcall": [f"4242 xcall(1) = {index}" for index in range(3)]}, "每个原因只留前 3 条")
        reasons = [f"4242 call{index:02d}(1) = 0" for index in range(25)]
        coverage = self.feed(reasons)["coverage"]
        self.assertEqual(len(coverage["unresolved_reasons"]), 25)
        self.assertEqual(sorted(coverage["unresolved_samples"]), sorted(f"unsupported-call:call{index:02d}" for index in range(20)), "最多 20 个原因，先到先得")
        self.assertEqual(ds.validate_raw(self.feed(reasons)), [])

    def test_tolerated_reasons_leave_no_unresolved_sample_but_unfinished_calls_are_sampled(self) -> None:
        lines = "".join(real.ALL_SEGMENTS).splitlines()
        coverage = self.feed(lines)["coverage"]
        self.assertEqual(coverage["unresolved_reasons"], {"symlink-metadata-not-bound:newfstatat": 6})
        self.assertNotIn("unresolved_samples", coverage, "容忍的原因不留未解析样例")
        pending = unfinished_lines(self.t, 10)
        coverage = self.feed(pending)["coverage"]
        self.assertEqual(coverage["unresolved_reasons"], {"unfinished-calls-at-eof": 10})
        self.assertEqual(coverage["unfinished_calls"], 10)
        self.assertEqual(coverage["unfinished_samples"], [f'{5000 + index} openat(AT_FDCWD<{{TREE}}>, "x", O_RDONLY <unfinished ...>' for index in range(8)],
                         "容忍的悬着调用最多留 8 条起始行（别名化），按开始顺序")
        self.assertNotIn("unresolved_samples", coverage, "unfinished-calls-at-eof 在容忍集合里，不占未解析原因的样例额度")
        self.assertEqual(ds.validate_raw(self.feed(pending)), [])

    def test_sampler_connect_reasons_and_deferred_events_are_sampled(self) -> None:
        start = '4242 connect(5<socket:[2]>, ' + '{sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("10.0.0.2")}' + ', 16 <unfinished ...>'
        coverage = self.feed([start, "4300 <... connect resumed>) = 0"])["coverage"]
        self.assertEqual(coverage["unresolved_reasons"], {"sampler:connect-resume-without-start": 1, "sampler:connect-unfinished-at-eof": 1})
        self.assertEqual(coverage["unresolved_samples"], {"sampler:connect-resume-without-start": ["4300 <... connect resumed>) = 0"],
                                                          "sampler:connect-unfinished-at-eof": [start]})
        deferred = self.feed(['1 getcwd("/repo/a/b", 4096) = 10', '1 clone(child_stack=NULL, flags=CLONE_VFORK|SIGCHLD <unfinished ...>',
                              '2 chdir("../..") = 0', '2 execve("./bin/check", ["check"], 0x1) = 0'])["coverage"]
        self.assertEqual(deferred["unresolved_reasons"]["deferred-process-events-at-eof"], 2)
        self.assertEqual(deferred["unresolved_samples"]["deferred-process-events-at-eof"], ['2 chdir("../..") = 0', '2 execve("./bin/check", ["check"], 0x1) = 0'])
        self.assertEqual(ds.validate_raw(self.feed([start])), [])

    def test_filter_cost_per_line_is_one_integer_comparison(self) -> None:
        """性能约束：常规行只多一次整数比较，慢路径只在引入新原因的行上进入（不要每行对字典求和）。"""
        clean = [open_line(self.t, f"{self.t}/backend/f{index}.go") for index in range(500)]
        aggregator = ds.StreamAggregator(self.aliases, unit_id=UNIT)
        with mock.patch.object(ds.StreamAggregator, "_note_unresolved", autospec=True, side_effect=ds.StreamAggregator._note_unresolved) as slow:
            for line in clean:
                aggregator.feed(line + "\n")
            self.assertEqual(slow.call_count, 0, "没有任何未解析原因时一次也不进慢路径")
            aggregator.feed("4242 frobnicate(1) = 0\n")
            self.assertEqual(slow.call_count, 1)
            for line in clean[:100]:
                aggregator.feed(line + "\n")
            self.assertEqual(slow.call_count, 1, "原因已经记过，后面的常规行仍不进慢路径")

    def test_raw_documents_with_samples_validate_and_bad_shapes_do_not(self) -> None:
        good = self.feed(unfinished_lines(self.t, 2) + ["4242 xcall(1) = 0"])
        self.assertEqual(ds.validate_raw(good), [])
        mutations = {
            "每个原因超过 3 条": lambda c: c["unresolved_samples"].update({"unsupported-call:xcall": ["a", "b", "c", "d"]}),
            "原因不在 unresolved_reasons 里": lambda c: c["unresolved_samples"].update({"nope": ["a"]}),
            "样例不是字符串": lambda c: c["unresolved_samples"].update({"unsupported-call:xcall": [1]}),
            "样例超过 300 字符": lambda c: c["unresolved_samples"].update({"unsupported-call:xcall": ["x" * 301]}),
            "样例是空列表": lambda c: c["unresolved_samples"].update({"unsupported-call:xcall": []}),
            "unresolved_samples 不是映射": lambda c: c.update({"unresolved_samples": ["a"]}),
            "unfinished_samples 超过 8 条": lambda c: c.update({"unfinished_samples": ["x"] * 9}),
            "unfinished_samples 有超长项": lambda c: c.update({"unfinished_samples": ["x" * 301]}),
            "unfinished_samples 不是列表": lambda c: c.update({"unfinished_samples": "x"}),
            "未知的 coverage 键": lambda c: c.update({"mystery": 1}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                broken = json.loads(json.dumps(good))
                mutate(broken["coverage"])
                self.assertTrue(ds.validate_raw(broken), name)


# ---------------------------------------------------------------------------
# merge / draft：共用一个合成仓库（测试只读它，所以整个类建一次）
# ---------------------------------------------------------------------------


class FollowupBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.tree = make_repo(cls.root, FOLLOWUP_FILES)
        cls.work = cls.root / "work"
        cls.work.mkdir()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def raw(self, lines: list[str], **options) -> dict:
        return make_raw(self.tree, self.work, lines, **options)

    def merge(self, *raws: dict, **options) -> dict:
        return ds.merge_raws(list(raws), tree=self.tree, **options)

    def good(self) -> list[str]:
        return good_lines(self.tree, self.work)

    def frontend(self, *, hits: int = 3) -> list[str]:
        """前端命令的读集：读两个跟踪的源文件、node_modules 下 hits 个未跟踪文件，枚举与探测 node_modules。"""
        t, cwd = str(self.tree), f"{self.tree}/frontend"
        reads = [open_line(cwd, f"{t}/frontend/package.json"), open_line(cwd, f"{t}/frontend/src/a.ts"), open_line(cwd, f"{t}/frontend/src/b.ts")]
        reads += [open_line(cwd, f"{t}/frontend/node_modules/.pnpm/p{index}/node_modules/p{index}/index.js") for index in range(hits)]
        return reads + [list_line(f"{t}/frontend/node_modules"), stat_line(cwd, f"{t}/frontend/node_modules/.bin")]


class MergeFollowupTests(FollowupBase):
    # -- unfinished-calls-at-eof -------------------------------------------------
    def test_unfinished_calls_at_eof_are_tolerated_up_to_64_across_runs(self) -> None:
        t = str(self.tree)
        for count, valid in ((1, True), (63, True), (64, True), (65, False)):
            with self.subTest(count=count):
                sample = self.merge(self.raw(self.good() + unfinished_lines(t, count)))
                self.assertEqual(sample["coverage"]["unresolved_reasons"]["unfinished-calls-at-eof"], count)
                self.assertEqual(sample["valid"], valid, sample["invalid_reasons"])
                self.assertEqual(ds.validate_sample(sample), [])
                if not valid:
                    self.assertTrue(any("unfinished-calls-at-eof×65 超过容忍上限 64" in reason for reason in sample["invalid_reasons"]), sample["invalid_reasons"])
        sample = self.merge(self.raw(self.good() + unfinished_lines(t, 64)))
        self.assertEqual(len(sample["coverage"]["unfinished_samples"]), 8, "样例最多 8 条")
        self.assertTrue(all(len(item) <= 300 and item.endswith("<unfinished ...>") for item in sample["coverage"]["unfinished_samples"]))
        self.assertEqual(sample["coverage"]["unfinished_calls"], 64)
        # 上限按合并后的总数算（与登记侧覆盖检查一致）：冷热各 32 个＝64 通过，各 33 个＝66 无效；两次相同的样例行去重。
        half = self.merge(self.raw(self.good() + unfinished_lines(t, 32)), self.raw(self.good() + unfinished_lines(t, 32), label="warm", cache="warm"))
        self.assertTrue(half["valid"], half["invalid_reasons"])
        self.assertEqual(half["coverage"]["unfinished_samples"], sample["coverage"]["unfinished_samples"], "同样的起始行跨两次运行只留一条")
        over = self.merge(self.raw(self.good() + unfinished_lines(t, 33)), self.raw(self.good() + unfinished_lines(t, 33), label="warm", cache="warm"))
        self.assertFalse(over["valid"])
        self.assertTrue(any("unfinished-calls-at-eof×66 超过容忍上限 64" in reason for reason in over["invalid_reasons"]))

    def test_only_the_exact_reason_is_tolerated(self) -> None:
        raw = self.raw(self.good())
        raw["coverage"]["unresolved_reasons"]["unfinished-calls-at-eof-extra"] = 1
        raw["coverage"]["unresolved"] += 1
        sample = self.merge(raw)
        self.assertFalse(sample["valid"])
        self.assertTrue(any("[unresolved]" in reason and "unfinished-calls-at-eof-extra×1" in reason for reason in sample["invalid_reasons"]))
        connect = self.merge(self.raw(self.good() + ["4242 connect(3<socket:[1]>, " + '{sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("127.0.0.1")}'
                                                     + ", 16 <unfinished ...>"]))
        self.assertTrue(any("sampler:connect-unfinished-at-eof" in reason for reason in connect["invalid_reasons"]), "connect 的悬着调用仍然不容忍")

    # -- 未解析行样例进无效原因 ----------------------------------------------------
    def test_unresolved_sample_is_appended_to_the_invalid_reason_within_600_chars(self) -> None:
        t = str(self.tree)
        line = f'7001 lsetxattr("{t}/backend/{"n" * 400}"..., "user.xdg.origin.url", "value", 5, 0) = 0'
        sample = self.merge(self.raw(self.good() + [line]))
        self.assertFalse(sample["valid"])
        reason = next(item for item in sample["invalid_reasons"] if item.startswith("[unresolved]"))
        self.assertIn("plain-path-not-resolved:lsetxattr×1", reason)
        self.assertIn("的原始行样例：7001 lsetxattr(\"{TREE}/backend/nnnn", reason, "第一条样例拼在原因清单后面")
        self.assertLessEqual(len(reason), ds.INVALID_REASON_CHARS)
        self.assertNotIn(str(self.tree), reason, "样例里不含采样时的具体绝对路径")
        review = sample["review"]["unresolved_samples"]
        self.assertEqual(sorted(review), ["plain-path-not-resolved:lsetxattr", "result-or-string-incomplete:lsetxattr"])
        self.assertEqual(ds.validate_sample(sample), [])
        # 冷热两次的同一条样例只留一条；上限 3 条。
        warm = self.raw(self.good() + [line], label="warm", cache="warm")
        merged = self.merge(self.raw(self.good() + [line]), warm)
        self.assertEqual(len(merged["review"]["unresolved_samples"]["plain-path-not-resolved:lsetxattr"]), 1)
        # 原因清单占去大半长度时，样例被截到刚好撑满 600 字符的总长上限；原因清单本身一个字都不截。
        twelve = [f'4242 c{index:02d}("{t}/backend/{"n" * 400}") = 0' for index in range(12)]
        text = next(item for item in self.merge(self.raw(self.good() + twelve))["invalid_reasons"] if item.startswith("[unresolved]"))
        self.assertEqual(len(text), ds.INVALID_REASON_CHARS)
        self.assertIn("unsupported-call:c00×1、", text)
        self.assertIn("unsupported-call:c11×1；unsupported-call:c00 的原始行样例：4242 c00(\"{TREE}/backend/nnn", text)
        # 清单本身就放不下时，样例一个字都不放，清单也不截。
        crowded = [f"4242 call{index:02d}_{'z' * 30}(1) = 0" for index in range(20)]
        text = self.merge(self.raw(self.good() + crowded))["invalid_reasons"][0]
        self.assertNotIn("原始行样例", text)
        self.assertTrue(text.endswith("×1"), text[-60:])
        self.assertGreater(len(text), ds.INVALID_REASON_CHARS)

    def test_a_raw_without_samples_gives_the_same_invalid_reason_text_as_before(self) -> None:
        raw = self.raw(self.good() + ["4242 frobnicate(1) = 0"])
        del raw["coverage"]["unresolved_samples"]  # 旧 raw 没有这个键
        sample = self.merge(raw)
        self.assertEqual(sample["invalid_reasons"], ["[unresolved] 存在不在容忍集合内的未解析原因：unsupported-call:frobnicate×1"])
        self.assertNotIn("unresolved_samples", sample["review"])

    # -- --allow-unix ---------------------------------------------------------------
    def test_allow_unix_is_validated_like_the_registry_side(self) -> None:
        for bad in ("relative/sock", "/a/../b", "/a/./b", "/a//b", "/a/", "/", "", "@abstract", "/a\\b", "/a\nb"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ds.SamplerError, "--allow-unix"):
                    self.merge(self.raw(self.good()), allow_unix=[bad])
        sample = self.merge(self.raw(self.good() + [connect_line('{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}')]),
                            allow_unix=["/var/run/nscd/socket", "/var/run/nscd/socket", "/var/run/docker.sock"])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(sample["review"]["allowances"]["unix_sockets"], ["/var/run/nscd/socket", "/var/run/docker.sock"], "去重、保持给出的顺序")
        self.assertEqual(sample["review"]["network"]["unix_allowed"], ["/var/run/nscd/socket"], "只列实际连过的")

    # -- --installed-dir --------------------------------------------------------------
    def test_installed_dir_exempts_reads_but_not_writes_and_leaves_a_trace(self) -> None:
        t = str(self.tree)
        lines = self.good() + self.frontend(hits=3)
        before = self.merge(self.raw(lines))
        self.assertTrue(any(reason.startswith("[untracked-read]") for reason in before["invalid_reasons"]), "不登记时仍是未跟踪读取")
        sample = self.merge(self.raw(lines), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(ds.validate_sample(sample), [])
        row, = sample["review"]["allowances"]["installed_dirs"]
        self.assertEqual({key: row[key] for key in ("prefix", "contract", "reads", "unused")}, {"prefix": NODE_MODULES, "contract": NODE_CONTRACT, "reads": 3, "unused": False})
        self.assertEqual(row["sample"], [f"frontend/node_modules/.pnpm/p{index}/node_modules/p{index}/index.js" for index in range(3)])
        self.assertEqual(sample["review"]["untracked_reads"]["count"], 0)
        # 前缀嵌套时读取记在最长的前缀名下（不重复计数）；外层前缀一条都没分到就标 unused。
        nested = self.merge(self.raw(lines), installed_dirs=[(NODE_MODULES, NODE_CONTRACT), (f"{NODE_MODULES}.pnpm/", "cache:pnpm-store")])
        rows = {item["prefix"]: item for item in nested["review"]["allowances"]["installed_dirs"]}
        self.assertTrue(nested["valid"], nested["invalid_reasons"])
        self.assertEqual((rows[f"{NODE_MODULES}.pnpm/"]["reads"], rows[NODE_MODULES]["reads"], rows[NODE_MODULES]["unused"]), (3, 0, True))
        # 前缀之外的未跟踪读取仍然判无效；前缀目录自身被读取也算在前缀下。
        outside = self.merge(self.raw(lines + [open_line(f"{t}/frontend", f"{t}/frontend/notes.txt")]), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertTrue(any("[untracked-read]" in reason and "frontend/notes.txt" in reason for reason in outside["invalid_reasons"]), outside["invalid_reasons"])
        itself = self.merge(self.raw(self.good() + [open_line(f"{t}/frontend", f"{t}/frontend/node_modules")]), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertTrue(itself["valid"], itself["invalid_reasons"])
        # 写入仍按原规则判：未被 .gitignore 忽略、也没登记 --transient，就是违规；落在被忽略的目录里则放行。
        wrote = self.merge(self.raw(lines + [write_line(f"{t}/frontend", f"{t}/frontend/node_modules/.cache/x.json")]), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertFalse(wrote["valid"])
        self.assertTrue(any("[write]" in reason and "frontend/node_modules/.cache/x.json" in reason for reason in wrote["invalid_reasons"]), wrote["invalid_reasons"])
        ignored = self.merge(self.raw(self.good() + [open_line(f"{t}/backend", f"{t}/ignored-deps/m/index.js"), write_line(f"{t}/backend", f"{t}/ignored-deps/m/cache.json")]),
                             installed_dirs=[("ignored-deps/", "cache:ignored-deps")])
        self.assertTrue(ignored["valid"], ignored["invalid_reasons"])
        # 一条都没读到的前缀标 unused，留痕但 draft 不会采用。
        idle = self.merge(self.raw(self.good()), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertEqual(idle["review"]["allowances"]["installed_dirs"][0]["unused"], True)
        self.assertEqual(idle["review"]["allowances"]["installed_dirs"][0]["reads"], 0)

    def test_installed_dir_over_tracked_reads_is_refused(self) -> None:
        t = str(self.tree)
        with self.assertRaisesRegex(ds.SamplerError, r"盖住了已跟踪的路径.*frontend/package\.json"):
            self.merge(self.raw(self.good() + self.frontend()), installed_dirs=[("frontend/", "cache:wide")])
        with self.assertRaisesRegex(ds.SamplerError, "盖住了已跟踪的路径"):
            self.merge(self.raw(self.good() + [list_line(f"{t}/frontend/src")]), installed_dirs=[("frontend/src/", "cache:src")])
        # 只做 stat 探测不算读取或枚举，不报错；前缀只是已跟踪路径的祖先（不在其下）也不报错。
        sample = self.merge(self.raw(self.good() + [stat_line(t, f"{t}/frontend/src/a.ts")]), installed_dirs=[("frontend/src/", "cache:src")])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.merge(self.raw(self.good() + self.frontend()), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])

    def test_installed_dir_arguments_are_validated(self) -> None:
        self.assertEqual(ds.parse_installed_dir("frontend/node_modules/=cache:node-modules"), (NODE_MODULES, NODE_CONTRACT))
        self.assertEqual(ds.parse_installed_dir("a=b/=cache:x.y_z-1"), ("a=b/", "cache:x.y_z-1"), "前缀里的 = 照字面算")
        for bad in ("没有等号", "frontend/node_modules=cache:x", "/abs/=cache:x", "a/../b/=cache:x", "a/./b/=cache:x", "a//b/=cache:x", "=cache:x",
                    "a/=toolchain:go", "a/=cache:", "a/=cache", "a/=cache:x:y", "a/=cache:-x", "a/=", "a\\b/=cache:x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ds.SamplerError):
                    ds.parse_installed_dir(bad)
        with self.assertRaisesRegex(ds.SamplerError, "两个不同的合同"):
            self.merge(self.raw(self.good()), installed_dirs=[("a/", "cache:x"), ("a/", "cache:y")])
        self.merge(self.raw(self.good()), installed_dirs=[("a/", "cache:x"), ("a/", "cache:x")])  # 完全相同的重复登记不报错

    # -- --transient ------------------------------------------------------------------
    def test_transient_exempts_written_then_read_back_paths_only(self) -> None:
        t = str(self.tree)
        cwd = f"{t}/frontend"
        cache = f"{t}/frontend/.eslintcache"
        temp = f"{t}/frontend/vitest.config.ts.timestamp-1791986238264-6fa7837989538.mjs"
        written_and_read = [open_line(cwd, temp), write_line(cwd, temp), write_line(cwd, cache)]
        bare = self.merge(self.raw(self.good() + written_and_read))
        self.assertTrue(any("[write]" in reason for reason in bare["invalid_reasons"]) and any("[untracked-read]" in reason for reason in bare["invalid_reasons"]))
        sample = self.merge(self.raw(self.good() + written_and_read), transient=["frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*"])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        self.assertEqual(ds.validate_sample(sample), [])
        rows = {row["pattern"]: row for row in sample["review"]["allowances"]["transient"]}
        self.assertEqual({key: rows["frontend/.eslintcache"][key] for key in ("paths", "read_back", "unused", "sample")},
                         {"paths": 1, "read_back": 0, "unused": False, "sample": ["frontend/.eslintcache"]})
        self.assertEqual({key: rows["frontend/vitest.config.ts.timestamp-*"][key] for key in ("paths", "read_back", "unused")}, {"paths": 1, "read_back": 1, "unused": False})
        self.assertEqual([row["pattern"] for row in sample["review"]["allowances"]["transient"]], sorted(rows), "模式排序、去重")
        self.assertEqual(sample["review"]["untracked_reads"]["count"], 0)
        # 只读不写的路径不豁免（外部放进来的东西不能借模式蒙混），模式没命中任何被写路径时标 unused。
        read_only = self.merge(self.raw(self.good() + [open_line(cwd, cache)]), transient=["frontend/.eslintcache"])
        self.assertFalse(read_only["valid"])
        self.assertTrue(any("[untracked-read]" in reason and "frontend/.eslintcache" in reason for reason in read_only["invalid_reasons"]), read_only["invalid_reasons"])
        self.assertEqual(read_only["review"]["allowances"]["transient"][0]["unused"], True)
        idle = self.merge(self.raw(self.good()), transient=["frontend/never-written", "elsewhere/*.tmp"])
        self.assertTrue(idle["valid"])
        self.assertEqual([(row["pattern"], row["paths"], row["unused"]) for row in idle["review"]["allowances"]["transient"]],
                         [("elsewhere/*.tmp", 0, True), ("frontend/never-written", 0, True)])

    def test_transient_globs_do_not_cross_directories_and_dir_prefix_covers_nested(self) -> None:
        match = {pattern: ds.PathMatcher(pattern) for pattern in ("frontend/*.mjs", "frontend/tmp-?.json", "scratch-out/", "frontend/cachedir", "a/b?c/*")}
        for pattern, relative, expected in (
                ("frontend/*.mjs", "frontend/a.mjs", True), ("frontend/*.mjs", "frontend/sub/a.mjs", False), ("frontend/*.mjs", "frontend/node_modules/x/y.mjs", False),
                ("frontend/*.mjs", "frontend/.mjs", True), ("frontend/*.mjs", "frontend/a.mjs/more", False),
                ("frontend/tmp-?.json", "frontend/tmp-1.json", True), ("frontend/tmp-?.json", "frontend/tmp-12.json", False),
                ("frontend/tmp-?.json", "frontend/tmp-/.json", False),
                ("scratch-out/", "scratch-out", True), ("scratch-out/", "scratch-out/a/b", True), ("scratch-out/", "scratch-out2/a", False),
                ("frontend/cachedir", "frontend/cachedir", True), ("frontend/cachedir", "frontend/cachedir/x", False),
                ("a/b?c/*", "a/bxc/f", True), ("a/b?c/*", "a/bxc/f/g", False), ("a/b?c/*", "a/b/c/f", False)):
            self.assertEqual(match[pattern].matches(relative), expected, (pattern, relative))
        t = str(self.tree)
        lines = self.good() + [write_line(t, f"{t}/frontend/sub/a.mjs"), write_line(t, f"{t}/frontend/b.mjs")]
        sample = self.merge(self.raw(lines), transient=["frontend/*.mjs"])
        self.assertFalse(sample["valid"], "glob 不跨 /：子目录里的写入仍是违规")
        self.assertTrue(any("frontend/sub/a.mjs" in reason for reason in sample["invalid_reasons"]))
        self.assertFalse(any("frontend/b.mjs" in reason for reason in sample["invalid_reasons"]))
        nested = self.merge(self.raw(self.good() + [write_line(t, f"{t}/scratch-out/a/b.json"), open_line(t, f"{t}/scratch-out/a/b.json")]), transient=["scratch-out/"])
        self.assertTrue(nested["valid"], nested["invalid_reasons"])

    def test_transient_does_not_loosen_other_rules(self) -> None:
        t = str(self.tree)
        tracked = self.merge(self.raw(self.good() + [write_line(t, f"{t}/backend/a.go")]), transient=["backend/*.go"])
        self.assertTrue(any("写入了被跟踪的路径" in reason for reason in tracked["invalid_reasons"]), "已跟踪的路径写入仍是违规，模式只管未跟踪的")
        self.assertEqual(tracked["review"]["allowances"]["transient"][0]["unused"], True)
        git_write = self.merge(self.raw(self.good() + [write_line(t, f"{t}/.git/index.lock")]), transient=[".git/index.lock", ".git/"])
        self.assertTrue(any("写入 .git 内部" in reason for reason in git_write["invalid_reasons"]))
        # “写完即删”靠运行前后 git status 兜底：运行后树不干净，模式再怎么匹配也判无效。
        leftover = f"{t}/frontend/.eslintcache"
        dirty = self.merge(self.raw(self.good() + [write_line(t, leftover)], after=("?? frontend/.eslintcache",)), transient=["frontend/.eslintcache"])
        self.assertFalse(dirty["valid"])
        self.assertTrue(any(reason.startswith("[git-status]") for reason in dirty["invalid_reasons"]))
        self.assertFalse(any(reason.startswith("[write]") for reason in dirty["invalid_reasons"]), "写入本身被模式豁免，无效原因只剩 git status")
        for bad in ("", "/abs/x", "a/../b", "a/./b", "a//b", "a/**/b", "a/**", "a/[xy]", "a]", "a*/", "a?/", "a\\b", "..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(ds.SamplerError):
                    ds.check_transient(bad)
        for good in ("frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*", "backend/ent/schema/.entc/", "a/b?c", "*"):
            self.assertEqual(ds.check_transient(good), good)

    # -- 默认行为与旧文件 ----------------------------------------------------------------
    def test_defaults_stay_structurally_identical_when_no_new_option_is_given(self) -> None:
        raw = self.raw(self.good())
        self.assertEqual(set(raw["coverage"]), ds.COVERAGE_KEYS)
        self.assertNotIn("no_go_cache", raw["run"])
        sample = self.merge(raw)
        self.assertEqual(set(sample["coverage"]), ds.COVERAGE_KEYS)
        self.assertEqual(set(sample["review"]), ds.REVIEW_KEYS, "没有新选项、没有不容忍的原因时 review 不多任何键")
        self.assertEqual(set(sample["review"]["allowances"]), {"unix_sockets", "class_prefixes"})
        self.assertEqual(set(sample["review"]["runs"][0]), {"label", "cache", "signal", "timed_out", "started_utc", "finished_utc", "env_overrides", "filter_wait_seconds"})
        self.assertEqual(ds.validate_sample(sample), [])

    def test_old_documents_without_the_new_keys_still_validate(self) -> None:
        """旧 raw／sample（只有旧键）仍能读取，新键缺省按空处理；新键的形状错了才拒绝。"""
        raw = self.raw(self.good())
        old_raw = json.loads(json.dumps(raw))
        for key in ds.RAW_COVERAGE_OPTIONAL_KEYS:
            old_raw["coverage"].pop(key, None)
        for key in ds.RUN_OPTIONAL_KEYS:
            old_raw["run"].pop(key, None)
        self.assertEqual(ds.validate_raw(old_raw), [])
        sample = self.merge(old_raw)
        old_sample = json.loads(json.dumps(sample))
        for key in ds.SAMPLE_COVERAGE_OPTIONAL_KEYS:
            old_sample["coverage"].pop(key, None)
        for key in ds.REVIEW_OPTIONAL_KEYS:
            old_sample["review"].pop(key, None)
        self.assertEqual(ds.validate_sample(old_sample), [])
        # 新键形状不对：run.no_go_cache 不是布尔、sample 的 coverage 里不认 unresolved_samples、allowances 新行缺键。
        broken = json.loads(json.dumps(raw))
        broken["run"]["no_go_cache"] = "yes"
        self.assertTrue(ds.validate_raw(broken))
        mixed = json.loads(json.dumps(sample))
        mixed["coverage"]["unresolved_samples"] = {}
        self.assertTrue(ds.validate_sample(mixed), "sample.coverage 对登记侧是闭合的，按原因归类的样例只能放 review")
        bad_rows = self.merge(self.raw(self.good() + self.frontend()), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        self.assertEqual(ds.validate_sample(bad_rows), [])
        for mutate in (lambda row: row.pop("unused"), lambda row: row.update({"reads": -1}), lambda row: row.update({"contract": "toolchain:go"}),
                       lambda row: row.update({"prefix": "no-slash"}), lambda row: row.update({"extra": 1}), lambda row: row.update({"sample": ["x"] * 21})):
            broken_rows = json.loads(json.dumps(bad_rows))
            mutate(broken_rows["review"]["allowances"]["installed_dirs"][0])
            self.assertTrue(ds.validate_sample(broken_rows))
        bad_transient = self.merge(self.raw(self.good()), transient=["x/y"])
        broken_transient = json.loads(json.dumps(bad_transient))
        broken_transient["review"]["allowances"]["transient"][0]["read_back"] = "0"
        self.assertTrue(ds.validate_sample(broken_transient))

    def test_no_go_cache_is_traced_in_review_runs_only_when_set(self) -> None:
        raw = self.raw(self.good())
        raw["run"]["no_go_cache"] = True
        self.assertEqual(ds.validate_raw(raw), [])
        sample = self.merge(raw)
        self.assertTrue(sample["review"]["runs"][0]["no_go_cache"])
        self.assertEqual(ds.validate_sample(sample), [])
        self.assertNotIn("no_go_cache", self.merge(self.raw(self.good()))["review"]["runs"][0])

    def test_merge_command_line_accepts_the_new_options_and_refuses_bad_ones(self) -> None:
        t = str(self.tree)
        lines = self.good() + self.frontend() + [write_line(t, f"{t}/frontend/.eslintcache"), connect_line('{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}')]
        raw_path = self.root / "cli.raw.json"
        ds.write_document(raw_path, self.raw(lines, unit="frontend:lint"))
        base = [sys.executable, "-B", str(ds.HERE), "merge", "--raw", str(raw_path), "--tree", str(self.tree), "--out", str(self.root / "cli.sample.json")]
        completed = subprocess.run([*base, "--installed-dir", f"{NODE_MODULES}={NODE_CONTRACT}", "--transient", "frontend/.eslintcache",
                                    "--allow-unix", "/var/run/nscd/socket"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((json.loads(completed.stdout)["valid"], json.loads(completed.stdout)["invalid_reasons"]), (True, []))
        document = ds.load_document(self.root / "cli.sample.json")
        self.assertEqual(document["review"]["allowances"]["unix_sockets"], ["/var/run/nscd/socket"])
        self.assertEqual(document["review"]["allowances"]["installed_dirs"][0]["contract"], NODE_CONTRACT)
        self.assertEqual(document["review"]["allowances"]["transient"][0]["pattern"], "frontend/.eslintcache")
        for extra, message in ((["--installed-dir", "frontend/node_modules/"], "前缀=合同"), (["--installed-dir", "a/=toolchain:go"], "cache:<名字>"),
                               (["--transient", "a/**/b"], "不支持 \\*\\*"), (["--allow-unix", "@abstract"], "规范绝对路径"),
                               (["--installed-dir", "frontend/=cache:x"], "盖住了已跟踪的路径")):
            with self.subTest(extra=extra):
                completed = subprocess.run([*base, *extra], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 2, completed.stdout)
                self.assertRegex(completed.stderr, "declared_sampler 拒绝：.*" + message)


class DraftBase(FollowupBase):
    def setUp(self) -> None:
        self.samples = self.root / f"samples-{self._testMethodName}"
        self.samples.mkdir(exist_ok=True)

    def draft(self, sample: dict, **options) -> tuple[dict, str]:
        ds.write_document(self.samples / "backend-go-test.sample.json", sample)
        return ds.draft_registry(self.samples, self.tree, **options)


class DraftFollowupTests(DraftBase):
    def frontend_sample(self, extra: list[str] = (), **merge_options) -> dict:
        options = dict(installed_dirs=[(NODE_MODULES, NODE_CONTRACT)], transient=["frontend/.eslintcache"])
        options.update(merge_options)
        t = str(self.tree)
        lines = self.good() + self.frontend() + [write_line(t, f"{t}/frontend/.eslintcache"), *extra]
        return self.merge(self.raw(lines), **options)

    def test_unix_sockets_are_written_to_the_scope_and_the_stale_hint_is_gone(self) -> None:
        sample = self.frontend_sample([connect_line('{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}')],
                                      allow_unix=["/var/run/nscd/socket", "/var/run/docker.sock"])
        registry, review = self.draft(sample)
        scope = registry["units"][UNIT]["scope"]
        self.assertEqual(scope["unix_sockets"], ["/var/run/nscd/socket"], "只写本单元实际连过且已登记的目标（排序、去重）")
        self.assertNotIn("没有承载", review)
        self.assertNotIn("无法写入", review)
        self.assertIn("`scope.unix_sockets`", review)
        self.assertIn("`/var/run/nscd/socket`", review)
        plain, plain_review = self.draft(self.merge(self.raw(self.good())))
        self.assertNotIn("unix_sockets", plain["units"][UNIT]["scope"], "没有登记的 AF_UNIX 目标时不写这个键")
        self.assertNotIn("scope.unix_sockets", plain_review)
        unix_two = self.merge(self.raw(self.good() + [connect_line('{sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}'),
                                                      connect_line('{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}')]),
                              allow_unix=["/var/run/nscd/socket", "/var/run/docker.sock"])
        two, _ = self.draft(unix_two)
        self.assertEqual(two["units"][UNIT]["scope"]["unix_sockets"], ["/var/run/docker.sock", "/var/run/nscd/socket"])

    def test_installed_dirs_and_contracts_reach_the_scope_and_skip_listed_directories(self) -> None:
        registry, review = self.draft(self.frontend_sample())
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "declared")
        repo = entry["scope"]["repo"]
        self.assertEqual(repo["installed_dirs"], [{"prefix": NODE_MODULES, "contract": NODE_CONTRACT}])
        self.assertIn(NODE_CONTRACT, entry["scope"]["contracts"])
        self.assertEqual(entry["scope"]["contracts"], sorted(set(entry["scope"]["contracts"])), "合同去重并排序")
        self.assertEqual(entry["scope"]["contracts"].count(NODE_CONTRACT), 1)
        self.assertFalse([item for item in repo["ranges"] if item["include"][0].startswith(NODE_MODULES.rstrip("/"))],
                         "被枚举的 node_modules 目录不能成为 range（登记侧要求安装目录前缀不得包含任何 range）")
        self.assertIn("依赖安装目录", review)
        self.assertIn(f"`{NODE_MODULES}` → 合同 `{NODE_CONTRACT}`：豁免未跟踪读取 3 个", review)
        # 对照：不登记 --installed-dir 时（旧行为）被枚举的 node_modules 会进 range——这就是要跳过它的原因。
        without = self.merge(self.raw(self.good() + [list_line(f"{self.tree}/frontend/node_modules")]))
        without_registry, _ = self.draft(without)
        self.assertTrue([item for item in without_registry["units"][UNIT]["scope"]["repo"]["ranges"] if item["include"][0].startswith("frontend/node_modules")])
        unused = self.merge(self.raw(self.good()), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        unused_registry, unused_review = self.draft(unused)
        self.assertNotIn("installed_dirs", unused_registry["units"][UNIT]["scope"]["repo"], "没读到任何路径的前缀不并进 scope")
        self.assertNotIn(NODE_CONTRACT, unused_registry["units"][UNIT]["scope"]["contracts"])
        self.assertIn("没有读到任何路径，未并入 installed_dirs 草案", unused_review)

    def test_transient_patterns_merge_into_repo_ignored_and_unused_ones_stay_out(self) -> None:
        t = str(self.tree)
        extra = [write_line(t, f"{t}/frontend/vitest.config.ts.timestamp-1-a.mjs"), open_line(t, f"{t}/frontend/vitest.config.ts.timestamp-1-a.mjs"),
                 write_line(t, f"{t}/frontend/.tmp-out/a.json"), list_line(f"{t}/frontend/.tmp-out")]
        sample = self.frontend_sample(extra, transient=["frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*", "frontend/.tmp-out/", "frontend/never-written"])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        registry, review = self.draft(sample)
        repo = registry["units"][UNIT]["scope"]["repo"]
        self.assertEqual(repo["repo_ignored"], sorted({"out/", "frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*", "frontend/.tmp-out/"}),
                         "与 .gitignore 忽略的写入目录合并、排序、去重；未命中任何写入的模式不并入")
        self.assertEqual(len(repo["repo_ignored"]), len(set(repo["repo_ignored"])))
        self.assertFalse([item for item in repo["ranges"] if ".tmp-out" in item["include"][0]], "暂存目录被枚举也不成为 range")
        self.assertIn("`frontend/never-written`：命中被写路径 0 个，其中读回 0 个；**没有命中任何被写路径，未并入 repo_ignored 草案**", review)
        self.assertIn("`frontend/vitest.config.ts.timestamp-*`：命中被写路径 1 个，其中读回 1 个", review)
        self.assertIn("“写完即删”靠它兜底", review)
        # 同一个目录既被 .gitignore 忽略又登记了 --transient：只出现一次。
        both = self.merge(self.raw(self.good()), transient=["out/"])
        self.assertEqual(self.draft(both)[0]["units"][UNIT]["scope"]["repo"]["repo_ignored"], ["out/"])

    def test_contract_required_lock_files_are_added_even_when_ranges_cover_them(self) -> None:
        sample = self.merge(self.raw(self.good()))
        registry, review = self.draft(sample)
        scope = registry["units"][UNIT]["scope"]
        self.assertIn("toolchain:go", scope["contracts"])
        repo = scope["repo"]
        self.assertTrue({"backend/go.mod", "backend/go.sum"} <= set(repo["files"]), repo["files"])
        self.assertTrue(any(item["include"] == ["backend/"] for item in repo["ranges"]) or "backend/go.mod" in repo["files"])
        self.assertEqual(repo["files"], sorted(repo["files"]))
        self.assertEqual(len(repo["files"]), len(set(repo["files"])), "files 里不重复（range 已覆盖也要补，但 files 自己不能重复）")
        self.assertIn("`backend/go.sum`（由合同要求补入）", review)
        self.assertIn("toolchain:go：补入", review)
        # toolchain:pnpm：经 --class-prefix 登记 pnpm 的安装位置后，要求 pnpm-lock.yaml 与 package.json。
        pnpm = ds.parse_class_prefix("/opt/pnpm/=toolchain:toolchain:pnpm")
        pnpm_sample = self.merge(self.raw(self.good() + self.frontend() + [open_line(f"{self.tree}/frontend", "/opt/pnpm/bin/pnpm")]),
                                 installed_dirs=[(NODE_MODULES, NODE_CONTRACT)], class_prefixes=[pnpm])
        pnpm_registry, pnpm_review = self.draft(pnpm_sample)
        pnpm_scope = pnpm_registry["units"][UNIT]["scope"]
        self.assertIn("toolchain:pnpm", pnpm_scope["contracts"])
        self.assertTrue({"frontend/pnpm-lock.yaml", "frontend/package.json", "backend/go.mod", "backend/go.sum"} <= set(pnpm_scope["repo"]["files"]))
        self.assertIn("`frontend/pnpm-lock.yaml`（由合同要求补入）", pnpm_review)
        # 没有工具链合同就不补：旧行为不变。
        bare = self.merge(self.raw([open_line(self.tree, f"{self.tree}/tools/x.py")]))
        self.assertFalse({"backend/go.mod", "backend/go.sum"} & set(self.draft(bare)[0]["units"][UNIT]["scope"]["repo"]["files"]))

    def test_missing_lock_files_are_flagged_not_invented(self) -> None:
        root = self.root / "no-locks"
        root.mkdir(exist_ok=True)
        tree = make_repo(root, {**BASE_FILES, "frontend/package.json": "{}\n"})  # 没有 go.sum、没有 pnpm-lock.yaml
        samples = self.root / "samples-no-locks"
        samples.mkdir(exist_ok=True)
        lines = [open_line(tree, f"{tree}/backend/go.mod"), open_line(tree, "/usr/local/go/src/fmt/print.go"), open_line(tree, "/opt/pnpm/bin/pnpm")]
        sample = ds.merge_raws([make_raw(tree, self.work, lines)], tree=tree, class_prefixes=[ds.parse_class_prefix("/opt/pnpm/=toolchain:toolchain:pnpm")])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        ds.write_document(samples / "backend-go-test.sample.json", sample)
        registry, review = ds.draft_registry(samples, tree)
        files = registry["units"][UNIT]["scope"]["repo"]["files"]
        self.assertIn("backend/go.mod", files)
        self.assertIn("frontend/package.json", files)
        self.assertNotIn("backend/go.sum", files, "不是仓库里跟踪的文件，不凭空添加")
        self.assertNotIn("frontend/pnpm-lock.yaml", files)
        self.assertIn("**依赖锁文件缺失**：`backend/go.sum`、`frontend/pnpm-lock.yaml`", review)
        self.assertIn("toolchain:go：缺失", review)

    def test_files_limit_hint_counts_the_added_lock_files(self) -> None:
        sample = self.merge(self.raw(self.good()))
        with mock.patch.object(ds, "MAX_FILES", 1):
            registry, review = self.draft(sample)
        self.assertGreaterEqual(len(registry["units"][UNIT]["scope"]["repo"]["files"]), 2)
        self.assertIn("超限待评审", review, "files 超限的提示照旧，补入的锁文件也计入")

    def test_review_lists_unresolved_samples_and_unfinished_samples(self) -> None:
        t = str(self.tree)
        sample = self.merge(self.raw(self.good() + unfinished_lines(t, 3) + [f'7001 lsetxattr("{t}/backend/f"..., "user.x", "v", 1, 0) = 0']))
        self.assertFalse(sample["valid"])
        registry, review = self.draft(sample)
        self.assertEqual(registry["units"][UNIT]["status"], "rerun-only")
        self.assertIn("plain-path-not-resolved:lsetxattr×1", registry["units"][UNIT]["reason"])
        self.assertIn("原始行样例", registry["units"][UNIT]["reason"], "rerun-only 的原因里带着第一条样例")
        section = review.split("### 9 unresolved_reasons")[1].split("### 10")[0]
        self.assertIn("`unfinished-calls-at-eof` 共 3 个在容忍集合内（合并后总数上限 64", section)
        self.assertIn('`5000 openat(AT_FDCWD<{TREE}>, "x", O_RDONLY <unfinished ...>`', section)
        self.assertIn("不在容忍集合内的未解析原因的原始 strace 行样例", section)
        self.assertIn('`7001 lsetxattr("{TREE}/backend/f"..., "user.x", "v", 1, 0) = 0`', section)
        self.assertNotIn(str(self.tree), review, "评审清单里的样例也不含采样时的具体绝对路径")
        clean_registry, clean_review = self.draft(self.merge(self.raw(self.good())))
        self.assertNotIn("原始 strace 行样例", clean_review.split("### 9 unresolved_reasons")[1].split("### 10")[0])
        self.assertEqual(clean_registry["units"][UNIT]["status"], "declared")


# ---------------------------------------------------------------------------
# draft：未固定的 git 用法自动转 rerun-only（head: true）
# ---------------------------------------------------------------------------


class GitUsageClassifierTests(unittest.TestCase):
    """git_usage_problem：固定＝所有修订参数都是完整 40 位小写十六进制提交号，且子命令属于只读、按对象寻址的一类；拿不准一律未固定。"""

    def test_fixed_usages_are_read_only_object_addressed_commands_with_full_commit_ids(self) -> None:
        fixed = [
            ["git", "show", f"{SHA}:docs/egress/a.json"], ["git", "show", SHA], ["git", "show", "--stat", f"{SHA}^{{commit}}"],
            ["git", "show", f"{SHA}^{{tree}}"], ["/usr/lib/git-core/git", "show", SHA], ["git", "--no-pager", "show", "-s", "--format=%B", SHA],
            ["git", "show", f"{SHA}:path with space.txt", f"{SHA2}:b.txt"],
            ["git", "cat-file", "-t", SHA], ["git", "cat-file", "-e", f"{SHA}^{{commit}}"], ["git", "cat-file", "blob", SHA],
            ["git", "cat-file", "-p", f"{SHA}:a/b.txt"],
            ["git", "rev-parse", "--verify", f"{SHA}^{{commit}}"], ["git", "rev-parse", "--verify", "-q", SHA],
            ["git", "rev-parse", f"{SHA}^{{tree}}"], ["git", "rev-parse", SHA, SHA2],  # 不带 --verify 但修订都固定：同样只按对象寻址
            ["git", "merge-base", SHA, SHA2], ["git", "merge-base", "--is-ancestor", SHA, SHA2], ["git", "merge-base", "--all", SHA, SHA2],
            ["git", "ls-tree", "--name-only", SHA, ".github/workflows/"], ["git", "ls-tree", "-r", f"{SHA}^{{tree}}"],
            ["git", "ls-tree", SHA, "--", "HEAD"],
            ["git", "diff-tree", "-r", "--name-only", SHA, SHA2], ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", SHA],
            ["git", "log", "-1", "--format=%B", SHA], ["git", "log", "--oneline", f"{SHA}..{SHA2}"], ["git", "log", f"{SHA}...{SHA2}"],
            ["git", "log", "-n", "3", SHA], ["git", "log", "-3", SHA], ["git", "log", SHA, "--", "backend/"], ["git", "log", "--max-count=2", SHA],
            ["git", "rev-list", "--parents", "-n", "1", SHA], ["git", "rev-list", "--count", SHA2, f"^{SHA}"], ["git", "rev-list", f"{SHA}..{SHA2}"],
            ["git", "rev-list", "--objects", SHA],
        ]
        for argv in fixed:
            with self.subTest(argv=argv):
                self.assertIsNone(ds.git_usage_problem(argv), " ".join(argv))

    def test_a_path_inside_a_pinned_object_is_content_not_a_reference(self) -> None:
        """修订之后的 :路径、格式串、“--”之后的路径里出现 HEAD、refs/ 的字样，不是引用。"""
        for argv in (["git", "show", f"{SHA}:refs/heads/x"], ["git", "show", f"{SHA}:HEAD"], ["git", "log", "-1", "--format=refs/heads/%H", SHA],
                     ["git", "ls-tree", SHA, "--", "origin", "main"], ["git", "log", SHA, "--", "HEAD", "refs/x"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ds.git_usage_problem(argv))

    def test_unfixed_usages_each_give_a_reason(self) -> None:
        unfixed = [
            # 浮动引用与各种非完整提交号的修订
            (["git", "merge-base", "--is-ancestor", SHA, "HEAD"], "'HEAD'"), (["git", "rev-parse", "--verify", "HEAD^{commit}"], "HEAD^{commit}"),
            (["git", "show", "refs/heads/x"], "refs/heads/x"), (["git", "show", "refs/tags/v1"], "refs/tags/v1"),
            (["git", "merge-base", SHA, "origin"], "'origin'"), (["git", "show", "origin/main"], "origin/main"), (["git", "log", "-1", "main"], "'main'"),
            (["git", "show", "master"], "'master'"), (["git", "show", "FETCH_HEAD"], "FETCH_HEAD"), (["git", "show", "@{u}"], "@{u}"),
            (["git", "show", "HEAD@{1}"], "HEAD@{1}"), (["git", "show", "main@{2.days.ago}"], "main@{2.days.ago}"),
            (["git", "show", "v1.0.1"], "v1.0.1"), (["git", "show", "badc8c5"], "badc8c5"), (["git", "show", SHA.upper()], SHA.upper()),
            (["git", "show", f"{SHA}~1"], "~1"), (["git", "show", f"{SHA}^"], "^"), (["git", "show", f"{SHA}^{{}}"], "^{}"),
            (["git", "show", "@"], "'@'"), (["git", "show", f"{SHA}x"], f"{SHA}x"),
            # 读引用库或写仓库状态的子命令
            (["git", "tag", "v1.0.1", SHA], "子命令 tag"), (["git", "worktree", "add", "--detach", "/tmp/x", SHA], "子命令 worktree"),
            (["git", "branch"], "子命令 branch"), (["git", "for-each-ref", "--format=%(objectname)"], "子命令 for-each-ref"),
            (["git", "ls-remote", "origin"], "子命令 ls-remote"), (["git", "remote", "add", "origin", "/tmp/o.git"], "子命令 remote"),
            (["git", "config", "user.name", "x"], "子命令 config"), (["git", "status", "--porcelain"], "子命令 status"),
            (["git", "stash", "create"], "子命令 stash"), (["git", "checkout", "-b", "x"], "子命令 checkout"), (["git", "fetch", "origin"], "子命令 fetch"),
            (["git", "update-ref", "HEAD", SHA], "子命令 update-ref"), (["git", "commit", "-m", "x"], "子命令 commit"), (["git", "init"], "子命令 init"),
            (["git", "diff", SHA, SHA2], "子命令 diff"), (["git", "grep", "x", SHA], "子命令 grep"), (["git", "symbolic-ref", "-q", "HEAD"], "子命令 symbolic-ref"),
            # 参数不可见、不是 git、没有子命令
            (["git", ds.GIT_ARGV_HIDDEN], "参数不可见"), (["python3", "x.py"], "不是 git"), ([], "不是 git"), (["git"], "没有子命令"),
            # 放行清单之外的全局选项与选项
            (["git", "-C", "/tmp/x", "show", SHA], "全局选项 -C"), (["git", "-c", "user.name=x", "show", SHA], "全局选项 -c"),
            (["git", "--git-dir=/tmp/x.git", "show", SHA], "全局选项 --git-dir"), (["git", "--shallow-file", "", "pack-objects"], "全局选项 --shallow-file"),
            (["git", "log", "--all", SHA], "选项 --all"), (["git", "log", "--since=1.day.ago", SHA], "选项 --since"), (["git", "rev-list", "--stdin"], "选项 --stdin"),
            (["git", "rev-list", "--all"], "选项 --all"), (["git", "cat-file", "--batch"], "选项 --batch"), (["git", "show", "--ext-diff", SHA], "选项 --ext-diff"),
            (["git", "rev-parse", "--show-toplevel"], "选项 --show-toplevel"), (["git", "rev-list", "--objects", "--stdin", "--not", "--all"], "选项 --stdin"),
            (["git", "log", "-n", "x", SHA], "-n 后面不是数字"), (["git", "show", "-"], "选项 -"),
            # 没给修订（缺省落到 HEAD）、少给、只给排除、范围另一端不固定
            (["git", "show"], "没有给出修订"), (["git", "log", "-1"], "没有给出修订"), (["git", "log", "-1", "--format=%B"], "没有给出修订"),
            (["git", "rev-list", "--count"], "没有给出修订"), (["git", "ls-tree", "-r"], "没有给出修订"), (["git", "rev-parse", "--verify"], "没有给出修订"),
            (["git", "cat-file", "-t"], "没有给出修订"), (["git", "merge-base", SHA], "两个固定修订"), (["git", "log", f"^{SHA}"], "只给了排除的修订"),
            (["git", "log", f"{SHA}..HEAD"], f"{SHA}..HEAD"), (["git", "log", "HEAD~3..HEAD"], "HEAD~3..HEAD"), (["git", "rev-list", f"{SHA}..main"], f"{SHA}..main"),
            (["git", "log", f"{SHA}:path"], f"{SHA}:path"), (["git", "log", f"{SHA}..{SHA2}..{SHA3}"], "..") ,
            # rev-parse：问仓库状态的用法与带 --verify 却给了多个修订的写法都不固定；cat-file 只接一个对象
            (["git", "rev-parse", "--abbrev-ref", SHA], "选项 --abbrev-ref"), (["git", "rev-parse", "--git-dir"], "选项 --git-dir"),
            (["git", "rev-parse", "--verify", SHA, SHA2], "只接一个修订"), (["git", "rev-parse", "HEAD"], "'HEAD'"),
            (["git", "cat-file", "blob", SHA, SHA2], "只放行一个固定对象"),
        ]
        for argv, expected in unfixed:
            with self.subTest(argv=argv):
                problem = ds.git_usage_problem(argv)
                self.assertIsNotNone(problem, " ".join(argv))
                self.assertIn(expected, problem, " ".join(argv))


class DraftGitUsageTests(DraftBase):
    """draft：只要有一条 git 子进程命令用法不固定，该单元就是 rerun-only 并带 head: true；纯固定提交号的保持 declared。"""

    def sample_with(self, *argvs: list[str], extra: list[str] = (), **merge_options) -> dict:
        return self.merge(self.raw(self.good() + [git_line(argv) for argv in argvs] + list(extra)), **merge_options)

    def test_each_kind_of_unfixed_usage_turns_the_unit_into_rerun_only_with_head(self) -> None:
        kinds = {
            "HEAD": ["git", "merge-base", "--is-ancestor", SHA, "HEAD"], "refs": ["git", "show", "refs/heads/x"], "origin": ["git", "merge-base", SHA, "origin"],
            "main": ["git", "log", "-1", "main"], "tag 子命令": ["git", "tag", "v1.0.1", SHA], "worktree 子命令": ["git", "worktree", "add", "--detach", "/tmp/w", SHA],
            "status 子命令": ["git", "status", "--porcelain"], "FETCH_HEAD": ["git", "show", "FETCH_HEAD"], "@{": ["git", "show", "@{u}"],
        }
        for name, argv in kinds.items():
            with self.subTest(name):
                sample = self.sample_with(argv)
                self.assertTrue(sample["valid"], sample["invalid_reasons"])
                registry, review = self.draft(sample)
                entry = registry["units"][UNIT]
                self.assertEqual(entry["status"], "rerun-only", name)
                self.assertIs(entry["head"], True)
                self.assertNotIn("scope", entry)
                self.assertNotIn("sample", entry)
                self.assertTrue(entry["reason"].startswith("git 子进程使用了未固定的修订（"), entry["reason"])
                self.assertTrue(entry["reason"].endswith("）：结果依赖 HEAD 或引用库，不可声明级承接"), entry["reason"])
                self.assertIn(" ".join(argv), entry["reason"])
                self.assertIn("**git 用法未固定**：1 条", review)
                self.assertIn("**未固定的 git 命令 1 条**", review)
                self.assertIn(f"`{' '.join(argv)}`：", review)
                self.assertIn("登记状态草案：`rerun-only`", review)
                self.assertNotIn("git_history", json.dumps(registry), "rerun-only 没有 scope，提交号不会出现在 git_history 之外的任何地方")
        # 参数不可见：merge 本来就判采样无效（[git-argv]），draft 在原因之外再并列“未固定”，head 取或。
        hidden = self.merge(self.raw(self.good() + ['4242 execve("/usr/bin/git", 0x1, 0x2) = 0']))
        self.assertFalse(hidden["valid"])
        entry = self.draft(hidden)[0]["units"][UNIT]
        self.assertEqual((entry["status"], entry["head"]), ("rerun-only", True))
        self.assertIn("采样无效：", entry["reason"])
        self.assertIn("git 子进程使用了未固定的修订（git <argv-not-visible>）", entry["reason"])

    def test_pinned_commit_commands_stay_declared_and_keep_the_mechanical_git_history(self) -> None:
        sample = self.sample_with(["git", "show", f"{SHA}:docs/egress/a.json"], ["git", "cat-file", "-t", SHA2], ["git", "merge-base", "--is-ancestor", SHA, SHA3],
                                  ["git", "rev-parse", "--verify", f"{SHA3}^{{commit}}"], ["git", "ls-tree", "--name-only", SHA, "docs/"])
        registry, review = self.draft(sample)
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "declared")
        self.assertNotIn("head", entry)
        self.assertEqual(entry["scope"]["repo"]["git_history"]["commits"], [SHA, SHA2, SHA3], "git_history 的机械提取保持原样")
        self.assertIs(entry["scope"]["repo"]["head"], False)
        stripped = json.loads(json.dumps(entry["scope"]))
        del stripped["repo"]["git_history"]
        self.assertNotIn(SHA, json.dumps(stripped), "固定的提交号只出现在 git_history 里")
        self.assertNotIn("git 用法未固定", review)
        self.assertNotIn("未固定的 git 命令", review)
        self.assertNotIn("转 rerun-only", review)
        self.assertEqual(self.draft(self.merge(self.raw(self.good())))[0]["units"][UNIT]["status"], "declared", "没有 git 命令的单元不受影响")

    def test_one_unfixed_command_among_pinned_ones_is_enough(self) -> None:
        sample = self.sample_with(["git", "show", f"{SHA}:a.json"], ["git", "cat-file", "-t", SHA], ["git", "rev-parse", "--verify", "HEAD^{commit}"])
        registry, review = self.draft(sample)
        entry = registry["units"][UNIT]
        self.assertEqual((entry["status"], entry["head"]), ("rerun-only", True))
        self.assertIn("（git rev-parse --verify HEAD^{commit}）", entry["reason"], "只列未固定的命令，固定的不列")
        self.assertNotIn(f"git show {SHA}", entry["reason"])
        section = review.split("### 11 ")[1]
        self.assertIn("`git rev-parse --verify HEAD^{commit}`：修订参数 'HEAD^{commit}' 不是完整 40 位提交号", section)
        self.assertIn("符号引用", section)
        self.assertIn("已按“未固定的 git 用法”转 rerun-only", section)

    def test_reason_lists_at_most_three_commands_cut_to_120_characters(self) -> None:
        long_name = "v" * 200
        argvs = [["git", "tag", f"{long_name}{index}", SHA] for index in range(5)]
        registry, review = self.draft(self.sample_with(*argvs))
        reason = registry["units"][UNIT]["reason"]
        shown = reason[len("git 子进程使用了未固定的修订（"):reason.index("；……共 5 条）")].split("；")
        self.assertEqual(len(shown), 3, "最多列 3 条")
        self.assertTrue(all(len(item) == 120 and item.startswith("git tag vvvv") for item in shown), shown)
        self.assertTrue(reason.endswith("；……共 5 条）：结果依赖 HEAD 或引用库，不可声明级承接"), reason[-80:])
        three = self.draft(self.sample_with(*argvs[:3]))[0]["units"][UNIT]["reason"]
        self.assertNotIn("共", three, "不超过 3 条时不写总数")
        self.assertIn("**未固定的 git 命令 5 条**", review, "评审清单列的是全部未固定命令")

    def test_review_lists_every_unfixed_command_up_to_the_list_limit(self) -> None:
        argvs = [["git", "tag", f"t{index}", SHA] for index in range(5)]
        with mock.patch.object(ds, "LIST_LIMIT", 3):
            _registry, review = self.draft(self.sample_with(*argvs))
        section = review.split("### 11 ")[1]
        listed = [line for line in section.splitlines() if "：子命令 tag 不在固定清单内" in line]
        self.assertEqual(len(listed), 3, "受 LIST_LIMIT 限制")
        self.assertIn("……共 5 项，只列前 3 项", section)
        _registry, full = self.draft(self.sample_with(*argvs))
        self.assertEqual(len([line for line in full.split("### 11 ")[1].splitlines() if "：子命令 tag 不在固定清单内" in line]), 5)
        self.assertIn("另有 1 个因 git 用法未固定", full.split("## 总览")[0], "汇总头部也写出转 rerun-only 的单元数")

    def test_invalid_sample_with_unfixed_git_lists_both_reasons_and_head_is_or(self) -> None:
        t = str(self.tree)
        only_invalid = self.merge(self.raw(self.good() + [write_line(t, f"{t}/backend/a.go")]))
        self.assertFalse(only_invalid["valid"])
        entry = self.draft(only_invalid)[0]["units"][UNIT]
        self.assertEqual(entry["status"], "rerun-only")
        self.assertNotIn("head", entry, "只有采样无效时不带 head（与从前相同）")
        self.assertTrue(entry["reason"].startswith("采样无效："))
        self.assertNotIn("git 子进程", entry["reason"])
        both = self.sample_with(["git", "merge-base", "--is-ancestor", SHA, "HEAD"], extra=[write_line(t, f"{t}/backend/a.go")])
        self.assertFalse(both["valid"])
        registry, review = self.draft(both)
        entry = registry["units"][UNIT]
        self.assertEqual((entry["status"], entry["head"]), ("rerun-only", True))
        self.assertTrue(entry["reason"].startswith("采样无效："), "原来的原因在前")
        self.assertIn("写入了被跟踪的路径", entry["reason"])
        self.assertIn("；git 子进程使用了未固定的修订（git merge-base --is-ancestor", entry["reason"], "两个原因并列")
        self.assertIn("采样有效：**否**", review)
        self.assertIn("**git 用法未固定**", review)

    def test_registry_entries_keep_the_documented_shape_for_rerun_only(self) -> None:
        """登记表的 rerun-only 条目只有 status、reason、可选 head: true 三个键（设计文档里的例子：读 git 历史的单元保留 head 输入）。"""
        entry = self.draft(self.sample_with(["git", "show", "HEAD"]))[0]["units"][UNIT]
        self.assertEqual(sorted(entry), ["head", "reason", "status"])
        self.assertIsInstance(entry["reason"], str)
        invalid = self.draft(self.merge(self.raw(self.good(), exit_code=1)))[0]["units"][UNIT]
        self.assertEqual(sorted(invalid), ["reason", "status"])


# ---------------------------------------------------------------------------
# 与登记侧（tools/ci/unit_records.py）互操作：采样器写出的东西，登记侧必须原样认得；两边的取值规则一致
# ---------------------------------------------------------------------------


def accepted(check, *args) -> bool:
    """调用校验函数：不抛异常算接受。登记侧抛 ValueError，采样器抛 SamplerError。"""
    try:
        check(*args)
    except (ValueError, ds.SamplerError):
        return False
    return True


class RecordLayerInteropTests(FollowupBase):
    def test_constants_and_tolerance_rules_agree_with_the_record_layer(self) -> None:
        from tools.ci import unit_records as ur
        self.assertEqual(ds.DECLARED_TOLERATED_UNRESOLVED, ur.DECLARED_TOLERATED_UNRESOLVED)
        self.assertEqual(ds.DECLARED_MAX_UNFINISHED_AT_EOF, ur._DECLARED_MAX_UNFINISHED_AT_EOF)
        self.assertEqual((ds.MAX_UNFINISHED_SAMPLES, ds.SAMPLE_LINE_CHARS), (ur._DECLARED_MAX_UNFINISHED_SAMPLES, ur._DECLARED_MAX_SAMPLE_CHARS))
        self.assertEqual(ds.TOOLCHAIN_LOCK_FILES, ur._DECLARED_TOOLCHAIN_LOCKS, "声明了工具链合同就要单独列出的锁文件，两边一张表")
        for reason in ("symlink-metadata-not-bound:newfstatat", "unfinished-calls-at-eof", "unfinished-calls-at-eof-extra", "unfinished-calls-at-eof:openat",
                       "sampler:connect-unfinished-at-eof", "unrecognized-line", "symlink-metadata-not-bound", "resume-without-start:wait4"):
            self.assertEqual(ds.reason_tolerated(reason), ur._declared_reason_tolerated(reason), reason)

    def test_argument_validators_accept_and_refuse_the_same_values_as_the_record_layer(self) -> None:
        from tools.ci import unit_records as ur
        for value in ("/var/run/docker.sock", "/var/run/nscd/socket", "/a", "relative/sock", "/a/../b", "/a/./b", "/a//b", "/a/", "/", "", "@abstract", "/a\\b", "/a\nb"):
            self.assertEqual(accepted(ds.check_unix_socket, value), accepted(ur._declared_unix_socket, value, "unix_sockets"), repr(value))
        for value in ("frontend/.eslintcache", "frontend/vitest.config.ts.timestamp-*", "backend/ent/schema/.entc/", "a/b?c", "*", "a/**/b", "a/**", "a/[xy]", "a]",
                      "/abs/x", "a/../b", "a/./b", "a//b", "a*/", "a?/", "", ".", "..", "a/", "a\\b", "x" * 300):
            self.assertEqual(accepted(ds.check_transient, value), accepted(ur._declared_ignored_item, value, "repo.repo_ignored"), repr(value))
        for prefix, contract in (("frontend/node_modules/", "cache:node-modules"), ("a/", "cache:x.y_z-1"), ("a", "cache:x"), ("/a/", "cache:x"), ("a/../b/", "cache:x"),
                                 ("a//b/", "cache:x"), ("", "cache:x"), ("a/", "toolchain:go"), ("a/", "cache:"), ("a/", "cache"), ("a/", "cache:x:y"),
                                 ("a/", "cache:-x"), ("a/", "host-config:x"), ("a/", "")):
            scope = {"schema_version": ur.DECLARED_SCOPE_SCHEMA, "repo": {"files": ["z.txt"], "installed_dirs": [{"prefix": prefix, "contract": contract}]},
                     "contracts": [contract]}
            self.assertEqual(accepted(ds.check_installed_dir, prefix, contract), accepted(ur.validate_declared_scope, scope), (prefix, contract))
        # glob 的匹配语义也一致：* 与 ? 不跨 /，整条路径完全匹配。
        for pattern in ("frontend/*.mjs", "frontend/tmp-?.json", "frontend/cachedir", "a/b?c/*", "*", "a/*/c"):
            for relative in ("frontend/a.mjs", "frontend/sub/a.mjs", "frontend/.mjs", "frontend/tmp-1.json", "frontend/tmp-12.json", "frontend/tmp-/.json",
                             "frontend/cachedir", "frontend/cachedir/x", "a/bxc/f", "a/bxc/f/g", "a/b/c/f", "top", "a/b/c", "a/c"):
                self.assertEqual(ds.PathMatcher(pattern).matches(relative), ur._declared_glob(pattern).fullmatch(relative) is not None, (pattern, relative))

    def frontend_lines(self) -> list[str]:
        """前端式读集（不含写 /dev/null：采样器把它当无害写入豁免，但草案把 /dev/ 归 virtual 类，登记侧不许往 virtual 类里写——见报告）。"""
        t, cwd = str(self.tree), f"{self.tree}/frontend"
        base = [line for line in self.good() if "/dev/null" not in line]
        return base + self.frontend() + [write_line(t, f"{t}/frontend/.eslintcache"), open_line(cwd, f"{t}/frontend/.eslintcache"),
                                         connect_line('{sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}')]

    def draft_scope(self, sample: dict) -> tuple[dict, dict]:
        samples = self.root / f"interop-{self._testMethodName}"
        samples.mkdir(exist_ok=True)
        ds.write_document(samples / "backend-go-test.sample.json", sample)
        registry, _review = ds.draft_registry(samples, self.tree)
        return ds.load_document(samples / "backend-go-test.sample.json"), registry["units"][UNIT]["scope"]

    def test_a_frontend_style_draft_passes_the_record_layer_checks_end_to_end(self) -> None:
        from tools.ci import unit_records as ur
        sample = self.merge(self.raw(self.frontend_lines()), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)], transient=["frontend/.eslintcache"],
                            allow_unix=["/var/run/nscd/socket"])
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        written, scope = self.draft_scope(sample)
        ur.validate_declared_scope(scope)  # scope 草案的形状：installed_dirs、repo_ignored 的 glob、unix_sockets、合同、锁文件，登记侧都认
        ur._declared_validate_sample(written)  # 证据文件的形状（含新增的 coverage.unfinished_samples 等）登记侧也认
        report = ur.declared_coverage(written, scope)
        self.assertTrue(report["ok"], report["undeclared"])
        # 安装目录前缀下的 5 个路径：3 个读取、被枚举的 node_modules 目录自身、被 stat 的 .bin；一个 AF_UNIX 连接按 unix_sockets 放行。
        self.assertEqual((report["counts"]["installed_paths"], report["counts"]["network"]), (5, 1))
        self.assertGreaterEqual(report["counts"]["ignored_paths"], 1)
        # 每个新增的草案字段都真的在起作用：拿掉就有未覆盖项。
        for name, mutate, rule in (("installed_dirs", lambda s: s["repo"].pop("installed_dirs"), "C1-undeclared"),
                                   ("unix_sockets", lambda s: s.pop("unix_sockets"), "C5-unix-undeclared"),
                                   ("repo_ignored", lambda s: s["repo"].update({"repo_ignored": []}), "C1-undeclared")):
            with self.subTest(name):
                stripped = json.loads(json.dumps(scope))
                mutate(stripped)
                if name == "installed_dirs":
                    stripped["contracts"] = [item for item in stripped["contracts"] if item != NODE_CONTRACT]
                result = ur.declared_coverage(written, stripped)
                self.assertFalse(result["ok"])
                self.assertIn(rule, {item["rule"] for item in result["undeclared"]})

    def test_the_unfinished_calls_limit_is_enforced_by_both_sides(self) -> None:
        from tools.ci import unit_records as ur
        t = str(self.tree)
        base = [line for line in self.good() if "/dev/null" not in line]
        at_limit = self.merge(self.raw(base + unfinished_lines(t, 64)))
        self.assertTrue(at_limit["valid"], at_limit["invalid_reasons"])
        written, scope = self.draft_scope(at_limit)
        self.assertTrue(ur.declared_coverage(written, scope)["ok"], "64 个悬着的调用：采样器有效，登记侧覆盖检查也通过（unfinished_samples 只核类型与长度）")
        self.assertEqual(len(written["coverage"]["unfinished_samples"]), 8)
        over = self.merge(self.raw(base + unfinished_lines(t, 65)))
        self.assertFalse(over["valid"])
        forced = {**over, "valid": True, "invalid_reasons": []}  # 抹掉采样器自己的无效标记，单看登记侧：它也独立地拒绝超过 64 个
        result = ur.declared_coverage(json.loads(ds.dumps_document(forced)), scope)
        self.assertEqual({item["rule"] for item in result["undeclared"]}, {"C7-unresolved"})
        self.assertIn("65", result["undeclared"][0]["reason"])


# ---------------------------------------------------------------------------
# run --no-go-cache
# ---------------------------------------------------------------------------


class RunNoGoCacheTests(TempCase):
    """沿用旧 RunTests 的假 strace、假 launcher（本机不依赖 Linux 与真实 strace）。"""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        install(self.bin / "strace", FAKE_STRACE)
        self.launcher = install(self.bin / "fake-launcher", FAKE_LAUNCHER)
        (self.work / "go-mod-cache").mkdir()
        self.out = self.root / "raw"
        lines_file = self.root / "fake-lines.txt"
        lines_file.write_text("\n".join(good_lines(self.tree, self.work)) + "\n", encoding="utf-8")
        self.env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
                    "FAKE_STRACE_LINES": str(lines_file)}
        self.manifest = self.root / "gates-manifest.json"
        item = {"unit_id": UNIT, "argv": [str(self.launcher), "/bin/sh", "-c", "exit 0"], "cwd": str(self.tree / "backend"), "cores": 0.7, "memory_mb": 5120,
                "timeout_seconds": 3600, "weight": 1.0, "inputs": {}, "reservation_restartable": True, "env": {}}
        self.manifest.write_text(json.dumps({"schema_version": ds.GATES_SCHEMA, "test_groups": [], "units": [item], "gates": [{"gate_id": "g", "units": [UNIT]}]}),
                                 encoding="utf-8")

    def run_once(self, cache: str = "cold", **options) -> tuple[int, Path, dict]:
        settings = dict(manifest=self.manifest, unit_id=UNIT, out_dir=self.out, tree=self.tree, work=self.work, cache=cache, label=None,
                        launcher_json=json.dumps([str(self.launcher)]), env_overrides={}, docker_hooks=False, timeout_factor=2.5, chunk_lines=1000,
                        base_env=self.env)
        settings.update(options)
        return ds.run_sample(**settings)

    def test_warm_requires_a_go_cache_by_default_but_not_with_the_switch(self) -> None:
        with self.assertRaisesRegex(ds.SamplerError, "热缓存要求"):
            self.run_once("warm")
        code, raw_path, summary = self.run_once("warm", no_go_cache=True)
        self.assertEqual(code, 0, summary)
        self.assertTrue(any("--no-go-cache" in note for note in summary["notes"]), "运行摘要里提示开关生效")
        self.assertTrue((self.work / "go-build-cache").is_dir() and not any((self.work / "go-build-cache").iterdir()), "目录不存在时建一个空的，不往里放东西")
        self.assertTrue((self.work / "go-tmp").is_dir())
        raw = ds.load_document(raw_path)
        self.assertEqual(ds.validate_raw(raw), [])
        self.assertIs(raw["run"]["no_go_cache"], True, "raw 的 run 记录如实记下开关")
        self.assertEqual(raw["run"]["cache"], "warm")

    def test_cold_does_not_rename_an_existing_go_cache_with_the_switch(self) -> None:
        build = self.work / "go-build-cache"
        build.mkdir()
        (build / "hot.txt").write_text("hot", encoding="utf-8")
        code, _path, summary = self.run_once("cold", no_go_cache=True)
        self.assertEqual(code, 0, summary)
        self.assertEqual([item.name for item in self.work.iterdir() if item.name.startswith("go-build-cache")], ["go-build-cache"], "没有改名保留的旧目录")
        self.assertEqual((build / "hot.txt").read_text(encoding="utf-8"), "hot", "同一 --work 下给 Go 单元写热的缓存原样保留")
        code, _path, summary = self.run_once("cold", label="default")  # 对照：默认仍把非空的旧目录改名保留
        self.assertEqual(code, 0, summary)
        self.assertEqual(len([item for item in self.work.iterdir() if item.name.startswith("go-build-cache.replaced-")]), 1)
        self.assertEqual(summary["notes"] and "未删除" in summary["notes"][0], True)

    def test_default_raw_has_no_switch_key_and_the_switch_flows_into_the_merged_review(self) -> None:
        code, path, _summary = self.run_once("cold")
        self.assertEqual(code, 0)
        default = ds.load_document(path)
        self.assertNotIn("no_go_cache", default["run"], "不带开关的 raw 与从前逐字相同")
        code, flagged_path, _summary = self.run_once("cold", label="flagged", no_go_cache=True)
        self.assertEqual(code, 0)
        sample = ds.merge_raws([default, ds.load_document(flagged_path)], tree=self.tree)
        self.assertEqual([item.get("no_go_cache", False) for item in sample["review"]["runs"]], [False, True])
        self.assertEqual(ds.validate_sample(sample), [])

    def test_command_line_switch(self) -> None:
        common = [sys.executable, "-B", str(ds.HERE), "run", "--manifest", str(self.manifest), "--unit", UNIT, "--out", str(self.out), "--tree", str(self.tree),
                  "--work", str(self.work), "--launcher-json", json.dumps([str(self.launcher)])]
        refused = subprocess.run([*common, "--cache", "warm"], capture_output=True, text=True, env=self.env)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("热缓存要求", refused.stderr)
        completed = subprocess.run([*common, "--cache", "warm", "--no-go-cache"], capture_output=True, text=True, env=self.env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        raw = ds.load_document(self.out / "backend-go-test.warm.raw.json")
        self.assertIs(raw["run"]["no_go_cache"], True)
        self.assertEqual(ds.validate_raw(raw), [])


if __name__ == "__main__":
    unittest.main()
