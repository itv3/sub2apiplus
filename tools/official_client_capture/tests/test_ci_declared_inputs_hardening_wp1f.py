"""声明级承接验证器的第二轮加固（步骤 5 工作包 WP1f；被测代码在 ``tools/ci/unit_records.py``）。

背景：独立验收 #4 复核 WP1d／WP1e，结论「有条件可以」（严重 0、重要 3、一般 8）。验证器侧能修的这一批（F1～F8）的正反例都在这里；
WP1e 的既有用例里被新口径改变的几处就地改了（见各用例的注释），不在这里重复。

1. F1 ``--`` 的含义：merge-base、cat-file、rev-parse 里 ``--`` 只是「选项结束」，其后仍是修订——argv 里出现就不固定；``C6-head`` 对 ``--`` 之后的豁免
   只对 show、log、rev-list、ls-tree、diff-tree 生效；
2. F2 ``--format=``／``--pretty=`` 的值受限（精确 raw ＋ 白名单占位符的自定义格式串），``--oneline`` 删去，数字判定只认 ASCII 数字；
3. F3 噪声探测去掉 TMP 别名根、拆开 ``/var/``、排除 ``/run/user/``（口径改动的既有用例在 hardening／probe_rules 里就地改）；
4. F4 ``repo_ignored`` 的 glob 匹配线性化（与旧正则语义逐条对拍；600／1000／5000 字符的最坏情形都在毫秒级）；
5. F5 ``.git`` 状态路径默认拒绝（除 objects/ 与四个明确无害的文件外一律算状态）；
6. F7 ``started_at_utc`` 晚于 ``completed_at_utc`` 报问题；
7. F8 补测试缺口：TREE 比 WORK 深两级时 ``{WORK}/a/`` 的 read 必须拒绝、``git -C show <A>`` 不固定、``ls-tree <A> <路径>``（路径不在 ``--`` 之后）仍固定；
8. 已提交的首版登记表与证据在新规则下仍全部通过。

复用 ``test_ci_declared_inputs_hardening`` 与 ``test_ci_declared_inputs_probe_rules`` 的夹具。
"""

from __future__ import annotations

import json
import random
import re
import time
import unittest
from pathlib import Path

from tools.ci import unit_records as ur
from tools.official_client_capture.tests import scaling_guard
from tools.official_client_capture.tests import test_ci_declared_inputs_hardening as first
from tools.official_client_capture.tests import test_ci_declared_inputs_probe_rules as probe
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base

A = first.A
git_sample = first.git_sample
cover = first.cover
rules = first.rules
REPO_ROOT = Path(__file__).resolve().parents[3]


def show(*options: str) -> list[str]:
    """``git show <选项…> <A>``：选项原样放在固定修订前面。"""

    return ["git", "show", *options, A]


# ---------------------------------------------------------------------------
# F1：-- 的含义
# ---------------------------------------------------------------------------


class DashDashTests(unittest.TestCase):
    def test_a_separator_in_merge_base_cat_file_or_rev_parse_is_never_fixed(self) -> None:
        """这三个子命令里 ``--`` 只是选项结束，其后的参数仍是修订：``merge-base A A -- HEAD`` 在真实 git 里输出随 HEAD 变化。"""

        cases = {
            "merge-base A A -- HEAD": (["git", "merge-base", A, A, "--", "HEAD"], "C6-head"),
            "merge-base --all A A -- main": (["git", "merge-base", "--all", A, A, "--", "main"], "C6-git-unfixed"),
            "merge-base A A -- refs/heads/main": (["git", "merge-base", A, A, "--", "refs/heads/main"], "C6-git-unfixed"),
            "merge-base A A -- @": (["git", "merge-base", A, A, "--", "@"], "C6-git-unfixed"),
            "merge-base A A --（其后没有参数）": (["git", "merge-base", A, A, "--"], "C6-git-unfixed"),
            "merge-base --is-ancestor A -- HEAD": (["git", "merge-base", "--is-ancestor", A, "--", "HEAD"], "C6-head"),
            "merge-base -- A A": (["git", "merge-base", "--", A, A], "C6-git-unfixed"),
            "cat-file -p A -- HEAD": (["git", "cat-file", "-p", A, "--", "HEAD"], "C6-head"),
            "cat-file -t A --": (["git", "cat-file", "-t", A, "--"], "C6-git-unfixed"),
            "rev-parse A -- HEAD": (["git", "rev-parse", A, "--", "HEAD"], "C6-head"),
            "rev-parse --verify A --": (["git", "rev-parse", "--verify", A, "--"], "C6-git-unfixed"),
            "rev-parse -- A": (["git", "rev-parse", "--", A], "C6-git-unfixed"),
            "全局选项之后的 merge-base": (["git", "--no-pager", "merge-base", A, A, "--", "HEAD"], "C6-head"),
        }
        for label, (argv, rule) in cases.items():
            with self.subTest(label):
                problem = ur._declared_git_usage_problem(argv)
                self.assertIsNotNone(problem, label)
                found = rules(cover(git_sample([argv])))
                self.assertIn(rule, found, (label, found))
                self.assertFalse(any(name.startswith("C7") for name in found))
                if rule == "C6-git-unfixed":
                    self.assertIn("--", problem)

    def test_the_same_commands_without_the_separator_stay_fixed(self) -> None:
        for argv in (["git", "merge-base", A, A], ["git", "merge-base", "--all", A, A], ["git", "merge-base", "--is-ancestor", A, A], ["git", "cat-file", "-p", A],
                     ["git", "cat-file", "blob", f"{A}:x"], ["git", "rev-parse", A], ["git", "rev-parse", "--verify", A]):
            with self.subTest(argv=argv):
                self.assertIsNone(ur._declared_git_usage_problem(argv))
                self.assertTrue(cover(git_sample([argv]))["ok"])

    def test_the_separator_still_starts_the_paths_in_show_log_rev_list_ls_tree_and_diff_tree(self) -> None:
        for argv in (["git", "show", A, "--", "HEAD"], ["git", "log", A, "--", "HEAD", "refs/heads/main"], ["git", "rev-list", A, "--", "HEAD"],
                     ["git", "ls-tree", A, "--", "HEAD"], ["git", "diff-tree", A, A, "--", "HEAD"], ["git", "--no-pager", "show", A, "--", "HEAD"],
                     ["git", "show", A, "--", "main", "@", "refs/x"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ur._declared_git_usage_problem(argv))
                result = cover(git_sample([argv]))
                self.assertTrue(result["ok"], result["undeclared"])

    def test_head_after_the_separator_is_checked_everywhere_else(self) -> None:
        """豁免只对五个「--」之后是路径的子命令；别的子命令（本来就不固定）和子命令前带别的全局选项的写法，「--」之后的 HEAD 也照查。"""

        for argv in (["git", "status", "--", "HEAD"], ["git", "-C", "x", "show", A, "--", "HEAD"], ["git", "diff", A, "--", "ORIG_HEAD"],
                     ["git", "merge-base", A, A, "--", "FETCH_HEAD"], ["git", "cat-file", "-p", "--", "MERGE_HEAD"], ["git", "rev-parse", "--", "HEAD"]):
            with self.subTest(argv=argv):
                self.assertIn("C6-head", rules(cover(git_sample([argv]))))

    def test_the_part_after_a_colon_is_a_path_inside_the_object_for_every_subcommand(self) -> None:
        for argv in (["git", "show", f"{A}:HEAD"], ["git", "cat-file", "-p", f"{A}:HEAD"], ["git", "rev-parse", f"{A}:HEAD"], ["git", "ls-tree", f"{A}:docs"],
                     ["git", "show", f"{A}:docs/HEAD", f"{A}:ORIG_HEAD"]):
            with self.subTest(argv=argv):
                self.assertTrue(cover(git_sample([argv]))["ok"], argv)

    def test_the_two_subcommand_sets_are_the_ones_in_the_spec(self) -> None:
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_REVISION_COMMANDS, frozenset({"merge-base", "cat-file", "rev-parse"}))
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_PATH_COMMANDS, frozenset({"show", "log", "rev-list", "ls-tree", "diff-tree"}))
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_REVISION_COMMANDS & ur._DECLARED_GIT_DASHDASH_PATH_COMMANDS, frozenset())
        self.assertEqual(ur._DECLARED_GIT_DASHDASH_REVISION_COMMANDS | ur._DECLARED_GIT_DASHDASH_PATH_COMMANDS, frozenset(ur._DECLARED_GIT_FIXED_SUBCOMMANDS),
                         "固定子命令表里的每个子命令都归入这两类之一")


# ---------------------------------------------------------------------------
# F2：格式串、--oneline、数字
# ---------------------------------------------------------------------------


class GitFormatTests(unittest.TestCase):
    ALLOWED_PLACEHOLDERS = ("%H", "%T", "%P", "%an", "%ae", "%aI", "%at", "%cn", "%ce", "%cI", "%ct", "%s", "%b", "%B", "%n", "%%", "%x00", "%x0a", "%x7F", "%xaB")
    FORBIDDEN_PLACEHOLDERS = {
        "%D": "带出 HEAD／分支／标签", "%d": "带出 HEAD／分支／标签", "%N": "notes", "%S": "来源引用", "%gd": "reflog", "%gD": "reflog", "%gn": "reflog", "%ge": "reflog",
        "%gs": "reflog", "%(describe)": "读引用库", "%(describe:tags)": "读引用库", "%(decorate)": "读引用库", "%(trailers)": "括号形式", "%(": "括号形式",
        "%h": "缩写长度随对象库变", "%t": "缩写长度随对象库变", "%p": "缩写长度随对象库变", "%ad": "随 log.date 变", "%cd": "随 log.date 变", "%aD": "随 log.date 变",
        "%cD": "随 log.date 变", "%ar": "相对当前时间", "%cr": "相对当前时间", "%ai": "不在表里", "%ci": "不在表里", "%aN": "读 .mailmap", "%aE": "读 .mailmap",
        "%cN": "读 .mailmap", "%cE": "读 .mailmap", "%C(red)": "颜色", "%Cred": "颜色", "%w(80)": "折行", "%<(10)": "补白", "%>(10)": "补白", "%+H": "魔法前缀",
        "%-H": "魔法前缀", "% H": "魔法前缀", "%e": "编码", "%m": "不在表里", "%G?": "签名", "%f": "不在表里", "%x": "缺十六进制", "%x0": "只有一位十六进制", "%xZZ": "不是十六进制",
        "%xg0": "不是十六进制", "%": "孤立的 %", "100%": "结尾的 %", "%z": "未知", "%%%D": "%% 之后的 %D",
    }

    def fixed(self, *argv: str) -> bool:
        result = cover(git_sample([list(argv)]))
        return result["ok"]

    def test_every_allowed_placeholder_is_fixed_in_every_spelling(self) -> None:
        for placeholder in self.ALLOWED_PLACEHOLDERS:
            for option in (f"--format={placeholder}", f"--format=a {placeholder} b", f"--pretty=format:{placeholder}", f"--pretty=tformat:{placeholder}",
                           f"--format=format:{placeholder}", f"--format=tformat:{placeholder}"):
                with self.subTest(option=option):
                    self.assertIsNone(ur._declared_git_usage_problem(show("-s", option)))
                    self.assertTrue(self.fixed(*show("-s", option)), option)

    def test_every_forbidden_placeholder_is_not_fixed_in_every_spelling(self) -> None:
        for placeholder, reason in self.FORBIDDEN_PLACEHOLDERS.items():
            for option in (f"--format={placeholder}", f"--format=ok %H {placeholder} ok", f"--pretty=format:{placeholder}", f"--pretty=tformat:{placeholder}",
                           f"--format=format:{placeholder}"):
                with self.subTest(option=option, reason=reason):
                    self.assertIsNotNone(ur._declared_git_usage_problem(show("-s", option)), option)
                    self.assertIn("C6-git-unfixed", rules(cover(git_sample([show("-s", option)]))), option)
        # log 里同样（show 与 log 都放行格式选项）。
        self.assertIsNone(ur._declared_git_usage_problem(["git", "log", "--format=%H%n%an", A]))
        self.assertIsNotNone(ur._declared_git_usage_problem(["git", "log", "--format=%D", A]))
        self.assertIsNotNone(ur._declared_git_usage_problem(["git", "log", "--pretty=format:%h", A]))

    def test_exactly_raw_is_the_only_named_format(self) -> None:
        for option in ("--format=raw", "--pretty=raw"):
            self.assertTrue(self.fixed(*show(option)), option)
        for name in ("oneline", "short", "medium", "full", "fuller", "reference", "email", "mboxrd", "on", "m", "foo", "raw ", "Raw", "RAW", "raw%", "rawx", ""):
            for prefix in ("--format=", "--pretty="):
                with self.subTest(option=prefix + name):
                    problem = ur._declared_git_usage_problem(show(prefix + name))
                    self.assertIsNotNone(problem, prefix + name)

    def test_a_value_without_percent_and_without_a_prefix_is_a_builtin_name_not_a_custom_format(self) -> None:
        """git 对不含 % 又没有 format:／tformat: 前缀的值先当内置格式名（或它们的唯一前缀缩写）、再当 pretty.<名> 配置别名解析，所以
        ``--format=oneline`` 不是「普通字符的自定义格式串」；显式写 format:／tformat: 前缀时才是字面文字。"""

        self.assertIn("内置格式名", ur._declared_git_format_problem("--format=oneline") or "")
        self.assertIsNone(ur._declared_git_format_problem("--pretty=format:oneline"))
        self.assertIsNone(ur._declared_git_format_problem("--pretty=tformat:oneline"))
        self.assertIsNone(ur._declared_git_format_problem("--format=format:oneline"))
        self.assertIsNone(ur._declared_git_format_problem("--format=hello %n world"))
        self.assertIsNone(ur._declared_git_format_problem("--pretty=format:"))
        self.assertIsNone(ur._declared_git_format_problem("--pretty=tformat:"))
        self.assertIsNotNone(ur._declared_git_format_problem("--pretty="))

    def test_options_without_a_value_or_without_the_equals_sign_are_not_fixed(self) -> None:
        for option in ("--format", "--pretty", "--pretty-format=%H", "--formatted=%H", "-pretty=%H", "--format %H"):
            with self.subTest(option=option):
                self.assertIsNotNone(ur._declared_git_usage_problem(show(option)), option)

    def test_percent_percent_is_a_literal_percent_and_does_not_start_a_placeholder(self) -> None:
        self.assertIsNone(ur._declared_git_format_problem("--format=%%D"), "%% 是字面百分号，后面的 D 只是字母")
        self.assertIsNone(ur._declared_git_format_problem("--format=100%% sure %H"))
        self.assertIsNotNone(ur._declared_git_format_problem("--format=%%%D"), "%% 之后的 %D 才是占位符")

    def test_the_placeholder_table_is_the_one_in_the_spec(self) -> None:
        self.assertEqual(set(ur._DECLARED_GIT_FORMAT_PLACEHOLDERS), {"H", "T", "P", "an", "ae", "aI", "at", "cn", "ce", "cI", "ct", "s", "b", "B", "n", "%"})
        self.assertEqual(ur._DECLARED_GIT_FORMAT_PREFIXES, ("--format=", "--pretty="))
        for placeholder in self.ALLOWED_PLACEHOLDERS:
            self.assertIsNotNone(ur._DECLARED_GIT_FORMAT_PLACEHOLDER.match(placeholder), placeholder)
        for placeholder in self.FORBIDDEN_PLACEHOLDERS:
            if placeholder.startswith("%") and len(placeholder) > 1 and not placeholder.startswith("%%"):
                self.assertIsNone(ur._DECLARED_GIT_FORMAT_PLACEHOLDER.fullmatch(placeholder), placeholder)

    def test_oneline_is_gone(self) -> None:
        """``--oneline`` 等于 ``--pretty=oneline --abbrev-commit``，缩写长度随对象库大小与 core.abbrev 变化。"""

        for subcommand in ("show", "log"):
            self.assertNotIn("--oneline", ur._DECLARED_GIT_FIXED_SUBCOMMANDS[subcommand][0])
            problem = ur._declared_git_usage_problem(["git", subcommand, "--oneline", A])
            self.assertIsNotNone(problem)
            self.assertIn("--oneline", problem)
            self.assertIn("C6-git-unfixed", rules(cover(git_sample([["git", subcommand, "--oneline", A]]))))

    def test_numbers_are_ascii_digits_only(self) -> None:
        for good in (["git", "log", "-n", "3", A], ["git", "log", "-5", A], ["git", "log", "--max-count=10", A], ["git", "rev-list", "--max-count=2", A],
                     ["git", "log", "-n", "0", A]):
            with self.subTest(argv=good):
                self.assertIsNone(ur._declared_git_usage_problem(good))
        for bad in (["git", "log", "-n", "²", A], ["git", "log", "-n", "٣", A], ["git", "log", "-n", "３", A], ["git", "log", "-n", "x", A], ["git", "log", "-n", "", A],
                    ["git", "log", "-٣", A], ["git", "log", "--max-count=²", A], ["git", "log", "--max-count=", A], ["git", "log", "--max-count=abc", A],
                    ["git", "log", "--max-count=-1", A], ["git", "rev-list", "--max-count=²", A], ["git", "ls-tree", "--abbrev=²", A], ["git", "ls-tree", "--abbrev=", A],
                    ["git", "ls-tree", "--abbrev=x", A], ["git", "log", "-n"]):
            with self.subTest(argv=bad):
                self.assertIsNotNone(ur._declared_git_usage_problem(bad), bad)

    def test_ls_tree_abbrev_is_not_fixed_whatever_the_number(self) -> None:
        """WP1g：``ls-tree --abbrev=<n>`` 的 ``<n>`` 只是缩写长度的下限，对象库里出现碰撞时输出会更长，所以不固定；没有任何已登记单元用它。
        数字再合规（ASCII、含 0 与 40）也不固定，写在位置参数前后、与别的选项并用、别的子命令里也都不固定；去掉它的同一条命令仍然固定。"""

        self.assertNotIn("--abbrev=", ur._DECLARED_GIT_FIXED_SUBCOMMANDS["ls-tree"][1])
        self.assertEqual(ur._DECLARED_GIT_FIXED_SUBCOMMANDS["ls-tree"][1], ())
        self.assertEqual(ur._DECLARED_GIT_NUMERIC_PREFIXES, ("--max-count=",))
        for argv in (["git", "ls-tree", "--abbrev=7", A], ["git", "ls-tree", "--abbrev=8", A], ["git", "ls-tree", "--abbrev=40", A], ["git", "ls-tree", "--abbrev=0", A],
                     ["git", "ls-tree", "-r", "--abbrev=7", A], ["git", "ls-tree", A, "--abbrev=7"], ["git", "ls-tree", "--abbrev=7", A, "--", "docs"],
                     ["git", "ls-tree", "--abbrev=7", "--name-only", A, "docs/egress"], ["git", "--no-pager", "ls-tree", "--abbrev=7", A],
                     ["git", "show", "--abbrev=7", A], ["git", "log", "--abbrev=7", A], ["git", "rev-list", "--abbrev=7", A]):
            with self.subTest(argv=argv):
                problem = ur._declared_git_usage_problem(argv)
                self.assertIsNotNone(problem, argv)
                self.assertIn("--abbrev=", problem)
                self.assertIn("C6-git-unfixed", rules(cover(git_sample([argv]))))
        for argv in (["git", "ls-tree", "-r", "--name-only", A], ["git", "ls-tree", A], ["git", "ls-tree", "-l", A, "docs/egress"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ur._declared_git_usage_problem(argv))
                self.assertTrue(cover(git_sample([argv]))["ok"])


# ---------------------------------------------------------------------------
# F8：既有实现正确但测试没覆盖的几处
# ---------------------------------------------------------------------------


class CoverageGapTests(unittest.TestCase):
    def test_a_tree_two_levels_below_work_gets_no_read_on_the_work_children_key(self) -> None:
        """TREE 比 WORK 深两级：{WORK}/ 的直接子项里有一个是 TREE 的祖先目录（{WORK}/a），键 ``{WORK}/a/`` 也在测试树的祖先链上——但 read 只对
        ``{WORK}/`` 这一个键放行，不对链上别的键放行（变异检验 M02：把判定改成前缀匹配时，这里会被放行）。"""

        aliases = {"TREE": "/var/tmp/gate/work/a/test-tree", "WORK": "/var/tmp/gate/work", "HOME": "/root", "TMP": "/tmp"}
        self.assertTrue(probe.cover(outside=[["{WORK}/", ["read", "list"], 1], ["{WORK}", ["read", "stat"], 1]], aliases=aliases)["ok"])
        for key in ("{WORK}/a/", "{WORK}/a", "{WORK}/a/b/"):
            for kinds in (["read"], ["read", "stat"], ["list", "read"]):
                with self.subTest(key=key, kinds=kinds):
                    self.assertEqual(probe.rules_of(probe.cover(outside=[[key, kinds, 1]], aliases=aliases)), ["C2-outside-undeclared"])
        # 同一个键的 list／dir／stat／missing 照旧放行（它在祖先链上）。
        self.assertTrue(probe.cover(outside=[["{WORK}/a/", ["list", "dir", "stat", "missing"], 1]], aliases=aliases)["ok"])
        # 别的深度：TREE 在 WORK 之下三级。
        deeper = {**aliases, "TREE": "/var/tmp/gate/work/a/b/test-tree"}
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{WORK}/a/b/", ["read"], 1]], aliases=deeper)), ["C2-outside-undeclared"])
        self.assertTrue(probe.cover(outside=[["{WORK}/", ["read"], 1]], aliases=deeper)["ok"])

    def test_git_with_a_directory_option_is_never_fixed_even_if_the_directory_looks_like_a_subcommand(self) -> None:
        """变异检验 M15：把 ``-C`` 误加进无害全局选项后，``git -C show <A>`` 会被当成 ``git show <A>``（-C 吞掉 show 当目录，其实根本没有子命令）。"""

        for argv in (["git", "-C", "show", A], ["git", "-C", "x", "show", A], ["git", "-c", "show", A], ["git", "--git-dir", "show", A], ["git", "--git-dir=show", "show", A],
                     ["git", "--work-tree", "show", A], ["git", "-p", "show", A], ["git", "--paginate", "show", A], ["git", "--no-pager", "-C", "x", "show", A],
                     ["git", "--literal-pathspecs", "show", A], ["git", "--exec-path=x", "show", A], ["git", "-C"]):
            with self.subTest(argv=argv):
                problem = ur._declared_git_usage_problem(argv)
                self.assertIsNotNone(problem, argv)
                self.assertIn("C6-git-unfixed", rules(cover(git_sample([argv]))))
        self.assertEqual(ur._DECLARED_GIT_PLAIN_GLOBALS, frozenset({"--no-pager", "--no-optional-locks"}))

    def test_ls_tree_only_takes_the_first_positional_as_a_revision(self) -> None:
        """变异检验 M57：``ls-tree <树> <路径…>`` 的路径不一定在 ``--`` 之后，第一个位置参数之后的都是路径。"""

        for argv in (["git", "ls-tree", A, "docs/egress"], ["git", "ls-tree", A, "docs/egress", "backend/go.mod"], ["git", "ls-tree", "-r", A, "HEAD"],
                     ["git", "ls-tree", A, "refs/heads/main", "main", "@"], ["git", "ls-tree", f"{A}:docs", "egress"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ur._declared_git_usage_problem(argv))
                result = cover(git_sample([argv]))
                self.assertTrue(result["ok"] or rules(result) == {"C6-head"}, (argv, result["undeclared"]))
        for argv in (["git", "ls-tree", "docs/egress"], ["git", "ls-tree", "HEAD", A], ["git", "ls-tree", "main", "docs"], ["git", "ls-tree", "-r"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(ur._declared_git_usage_problem(argv), argv)
        # 路径里出现 HEAD 字样：不在 -- 之后时 C6-head 的字面检查仍会看见它（保守，不影响「固定」判定）。
        self.assertTrue(cover(git_sample([["git", "ls-tree", A, "docs/egress"]]))["ok"])

    def test_every_subcommand_in_the_table_is_exercised_by_a_fixed_and_an_unfixed_example(self) -> None:
        fixed = {"show": ["git", "show", A], "cat-file": ["git", "cat-file", "-p", A], "rev-parse": ["git", "rev-parse", A], "merge-base": ["git", "merge-base", A, A],
                 "ls-tree": ["git", "ls-tree", A], "diff-tree": ["git", "diff-tree", A, A], "log": ["git", "log", A], "rev-list": ["git", "rev-list", A]}
        self.assertEqual(set(fixed), set(ur._DECLARED_GIT_FIXED_SUBCOMMANDS))
        for name, argv in fixed.items():
            with self.subTest(name):
                self.assertIsNone(ur._declared_git_usage_problem(argv))
                self.assertIsNotNone(ur._declared_git_usage_problem([*argv[:2], "--not-an-option", *argv[2:]]), "未知选项一律不固定")


# ---------------------------------------------------------------------------
# F4：glob 线性匹配
# ---------------------------------------------------------------------------


def reference_regex(pattern: str) -> "re.Pattern[str]":
    """WP1e 及以前 ``_declared_glob`` 的实现（正则），作对拍基准。"""

    return re.compile("".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in pattern))


class GlobLinearTests(unittest.TestCase):
    def test_the_linear_matcher_agrees_with_the_old_regex_on_random_patterns(self) -> None:
        rng = random.Random(20261015)
        checked = 0
        for _ in range(6000):
            pattern = "".join(rng.choice("ab*?/.") for _ in range(rng.randint(1, 9))).strip("/") or "a"
            if "//" in pattern:
                continue
            text = "".join(rng.choice("ab/.*?x") for _ in range(rng.randint(0, 11)))
            expected = reference_regex(pattern).fullmatch(text) is not None
            actual = ur._declared_glob(pattern).fullmatch(text) is not None
            self.assertEqual(actual, expected, (pattern, text))
            checked += 1
        self.assertGreater(checked, 5000)

    def test_the_linear_matcher_agrees_with_the_old_regex_on_hand_written_cases(self) -> None:
        cases = {
            "frontend/*.mjs": ("frontend/a.mjs", "frontend/sub/a.mjs", "frontend/.mjs", "frontend/a.mjsx"),
            "frontend/tmp-?.json": ("frontend/tmp-1.json", "frontend/tmp-12.json", "frontend/tmp-/.json", "frontend/tmp-.json"),
            "frontend/vitest.config.ts.timestamp-*": ("frontend/vitest.config.ts.timestamp-1-a.mjs", "frontend/vitest.config.ts.timestamp-", "frontend/x/vitest.config.ts.timestamp-1"),
            "a/b?c/*": ("a/bxc/f", "a/bxc/f/g", "a/b/c/f", "a/bc/f"), "*": ("top", "a/b", "", "."), "a/*/c": ("a/b/c", "a/b/b/c", "a/c", "a//c"),
            "*a*a*a*b": ("aaab", "aaaa", "baaab", "xaxaxab", "a" * 50 + "b", "a" * 50), "?*?": ("ab", "a", "abc", "a/b"), "**": ("", "a", "a/b"),
            "a*": ("a", "ab", "a/b", "ba"), "*a": ("a", "ba", "ab", "b/a"), "x?y?z": ("xayaz", "xyz", "xaya", "xa/yaz"),
        }
        for pattern, texts in cases.items():
            for text in texts:
                with self.subTest(pattern=pattern, text=text):
                    self.assertEqual(ur._declared_glob(pattern).fullmatch(text) is not None, reference_regex(pattern).fullmatch(text) is not None)

    def test_star_and_question_mark_never_cross_a_slash(self) -> None:
        self.assertIsNone(ur._declared_glob("a/*").fullmatch("a/b/c"))
        self.assertIsNone(ur._declared_glob("a*c").fullmatch("a/c"))
        self.assertIsNone(ur._declared_glob("a?c").fullmatch("a/c"))
        self.assertIsNone(ur._declared_glob("*").fullmatch("a/b"))
        self.assertTrue(ur._declared_glob("a/*/c").fullmatch("a/b/c"))
        self.assertTrue(ur._declared_glob("*").fullmatch(""), "* 可以匹配空段")
        self.assertIsNone(ur._declared_glob("a/*").fullmatch("a"), "段数必须相同")

    def test_match_returns_true_or_none_so_both_truthiness_and_is_not_none_work(self) -> None:
        """采样器的一致性测试用 ``fullmatch(...) is not None`` 对拍匹配语义；返回 False 会让它永远为真。"""

        self.assertIs(ur._declared_glob("a*").fullmatch("ab"), True)
        self.assertIsNone(ur._declared_glob("a*").fullmatch("b"))

    def test_the_worst_allowed_patterns_stay_fast_for_long_paths(self) -> None:
        """WP1e 的回溯正则：``out/*a*a*a*b`` 对 400 字符 0.85 秒、600 字符 4.1 秒、1000 字符超过 25 秒；线性匹配对 5000 字符也是毫秒级。"""

        patterns = ("out/*a*a*a*b", "out/*a*a*a*a", "out/?*a*a*b", "out/*a?a*a*b", "out/a*a*a*a*", "out/*?*?*?", "out/?a*?a*a*b")
        # 不用绝对阈值（满载下会抖红）：每个模式按长度加倍做相对比较，第一次越界就失败，更长的长度不会继续跑
        # （回溯实现遇到 5000 字符会算上几个小时）；见 scaling_guard 的说明。最长 3840 字符。
        for pattern in patterns:
            glob = ur._declared_glob(pattern)
            scaling_guard.assert_growth_is_bounded(
                self, lambda length, glob=glob: (lambda: glob.fullmatch(f"out/{'a' * length}")), (120, 240, 480, 960, 1920, 3840), label=pattern)
            for length in (120, 3840):
                self.assertIn(glob.fullmatch(f"out/{'a' * length}"), (True, None))

    def test_declared_coverage_with_a_worst_case_glob_and_long_paths_is_fast(self) -> None:
        for pattern in ("out/*a*a*a*b", "out/*a*a*a*a"):
            scope = probe.bare_scope(repo_ignored=[pattern])

            def prepare(length: int, scope=scope):
                sample = base.make_sample()
                sample["repo_accesses"] = [["{TREE}/backend/a.go", ["read"], True], [f"{{TREE}}/out/{'a' * length}", ["write"], False],
                                           [f"{{TREE}}/out/{'a' * length}b", ["write"], False]]
                sample["outside_accesses"], sample["git_commands"], sample["network"] = [], [], []
                return lambda: ur.declared_coverage(sample, scope)

            # 同样用长度加倍的相对比较（第一个越界就失败，不让更长的路径继续跑），不用绝对阈值。
            scaling_guard.assert_growth_is_bounded(self, prepare, (500, 1000, 2000, 4000), label=f"覆盖检查 {pattern}")
            # *a*a*a*b 匹配第二条（以 b 结尾的 a 串）：它被当作输出区，只剩第一个模式下的另一条违规或全部通过。
            self.assertIsInstance(prepare(600)()["ok"], bool)

    def test_the_wildcard_limit_is_kept_as_hygiene(self) -> None:
        ur.validate_declared_scope(probe.bare_scope(repo_ignored=["out/*a*a*a*b"]))
        with self.assertRaises(ValueError):
            ur.validate_declared_scope(probe.bare_scope(repo_ignored=["out/*a*a*a*a*b"]))
        self.assertNotIn("回溯", (ur._declared_ignored_item.__doc__ or "").split("例如")[0].replace("没有", ""), "常量与文档不再说匹配靠会回溯放大的正则")


# ---------------------------------------------------------------------------
# F5：.git 状态默认拒绝
# ---------------------------------------------------------------------------


class GitStateDefaultDenyTests(unittest.TestCase):
    STATE_PATHS = (
        "HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "BISECT_LOG", "BISECT_START", "BISECT_TERMS",
        "index", "packed-refs", "shallow", "refs/heads/main", "refs/tags/v1", "refs/replace/abc", "refs", "logs/HEAD", "logs/refs/heads/main",
        "worktrees/w/HEAD", "worktrees/w/gitdir", "modules/m/HEAD", "modules/m/config", "rebase-merge/head-name", "rebase-apply/patch", "sequencer/todo",
        "info/grafts", "info/refs", "HEAD.lock", "index.lock", "refs/heads/main.lock", "packed-refs.lock", "hooks/pre-commit", "commondir", "gitdir", "info",
        "COMMIT_EDITMSG", "MERGE_MSG", "SQUASH_MSG", "NOTES_MERGE_REF", "AUTO_MERGE", "lost-found/x", "branches/x", "remotes/origin/x", "svn/x", "objectsx/y", "objects-old/x",
    )
    # objects/ab/cdef 是夹具默认就带的路径（再放一遍会因路径重复被判证据格式非法），所以这里用别的对象路径。
    HARMLESS_PATHS = ("config", "description", "info/exclude", "info/attributes", "objects", "objects/cd/ef01", "objects/pack/pack-1.idx", "objects/info/packs")

    def rows(self, names, kinds) -> tuple:
        return tuple([f"{{TREE}}/.git/{name}", list(kinds), False] for name in names)

    def test_state_is_everything_except_objects_and_the_four_harmless_files(self) -> None:
        for name in self.STATE_PATHS:
            with self.subTest(name=name):
                self.assertTrue(ur._declared_git_state_path(f".git/{name}"), name)
        for name in self.HARMLESS_PATHS:
            with self.subTest(name=name):
                self.assertFalse(ur._declared_git_state_path(f".git/{name}"), name)
        self.assertFalse(ur._declared_git_state_path(".git"), "仓库根下的 .git 目录自身不是 .git 下的路径")
        self.assertFalse(ur._declared_git_state_path("backend/.git/HEAD"), "只管树根下的 .git/")
        self.assertEqual(ur._DECLARED_GIT_HARMLESS_FILES, frozenset({"config", "description", "info/exclude", "info/attributes"}))

    def test_without_a_fixed_git_subprocess_every_state_read_is_a_violation(self) -> None:
        for kinds in (["read"], ["list"], ["read", "stat"]):
            result = cover(git_sample([], repo=self.rows(self.STATE_PATHS, kinds)))
            self.assertEqual({item["path"] for item in result["undeclared"] if item["rule"] == "C1-git-state"},
                             {f"{{TREE}}/.git/{name}" for name in self.STATE_PATHS}, kinds)
            self.assertEqual(rules(result), {"C1-git-state"}, kinds)

    def test_harmless_paths_are_allowed_with_or_without_a_git_subprocess(self) -> None:
        for commands in ([], [["git", "show", A]], [["git", "status"]]):
            with self.subTest(commands=commands):
                result = cover(git_sample(commands, repo=self.rows(self.HARMLESS_PATHS, ["read", "list"])))
                self.assertNotIn("C1-git-state", rules(result), result["undeclared"])

    def test_metadata_only_accesses_to_state_paths_are_not_reads(self) -> None:
        for kinds in (["stat"], ["dir"], ["stat", "dir"], ["missing"], ["missing", "stat", "dir"]):
            result = cover(git_sample([], repo=self.rows(self.STATE_PATHS, kinds)))
            self.assertNotIn("C1-git-state", rules(result), kinds)

    def test_a_fixed_git_subprocess_explains_the_reads_but_only_the_reads(self) -> None:
        """放宽项汇总第 9 条记载的折中：有一条固定的 git 子进程，状态路径的 read／list 全放行；写入、执行仍是 C1-git-kinds。"""

        explained = cover(git_sample([["git", "show", f"{A}:x"]], repo=self.rows(self.STATE_PATHS, ["read", "list"])))
        self.assertTrue(explained["ok"], explained["undeclared"])
        written = cover(git_sample([["git", "show", f"{A}:x"]], repo=self.rows(("HEAD", "refs/heads/main", "worktrees/w/HEAD"), ["write"])))
        self.assertEqual(rules(written), {"C1-git-kinds"})
        executed = cover(git_sample([["git", "show", f"{A}:x"]], repo=self.rows(("hooks/pre-commit",), ["read", "exec"])))
        self.assertEqual(rules(executed), {"C1-git-kinds"})

    def test_one_unfixed_git_command_removes_the_explanation(self) -> None:
        result = cover(git_sample([["git", "show", A], ["git", "status"]], repo=self.rows(("HEAD", "worktrees/w/HEAD"), ["read"])))
        self.assertEqual({item["path"] for item in result["undeclared"] if item["rule"] == "C1-git-state"}, {"{TREE}/.git/HEAD", "{TREE}/.git/worktrees/w/HEAD"})
        self.assertIn("C6-git-unfixed", rules(result))

    def test_a_declared_history_is_still_required_for_every_git_path(self) -> None:
        no_history = base.make_scope()
        del no_history["repo"]["git_history"]
        for name in (*self.STATE_PATHS[:5], *self.HARMLESS_PATHS):
            self.assertEqual(rules(cover(git_sample([], repo=self.rows([name], ["read"])), no_history)), {"C1-git-undeclared"}, name)


# ---------------------------------------------------------------------------
# F7：started_at_utc 与 completed_at_utc
# ---------------------------------------------------------------------------


class StartedCompletedTests(base.DeclaredTestCase):
    def test_an_inverted_start_and_completion_is_a_problem(self) -> None:
        self.assertEqual(self.world.check(), [])
        late = self.world.reseal(lambda body: body.update(started_at_utc="2099-01-01T00:00:00Z"))
        problems = self.world.check(late)
        self.assertTrue(any("started_at_utc 晚于 completed_at_utc" in problem for problem in problems), problems)
        # 倒置还让「核验时间不得晚于启动时间」的检查失去依据：凭证核验时间在 2098 年也过得了那一条，所以倒置必须单独报。
        forged = self.world.reseal(lambda body: (body.update(started_at_utc="2099-01-01T00:00:00Z"),
                                                 [item.update(verified_at_utc="2098-06-01T00:00:00Z") for item in body["declared_contracts"]]))
        self.assertTrue(any("started_at_utc 晚于 completed_at_utc" in problem for problem in self.world.check(forged)))

    def test_equal_times_and_ordered_times_are_fine(self) -> None:
        equal = self.world.reseal(lambda body: body.update(started_at_utc=body["completed_at_utc"], completed_at_utc=body["completed_at_utc"]))
        # 凭证核验时间 00:00:05Z 早于 00:05:00Z，相等的启动／完成时间不倒置。
        self.assertFalse(any("晚于 completed_at_utc" in problem for problem in self.world.check(equal)))
        self.assertFalse(any("晚于 completed_at_utc" in problem for problem in self.world.check()))

    def test_unparseable_times_do_not_trigger_the_inversion_message(self) -> None:
        for value in (None, "garbage", "", 12345):
            item = self.world.reseal(lambda body, value=value: body.update(completed_at_utc=value))
            self.assertFalse(any("晚于 completed_at_utc" in problem for problem in self.world.check(item)), value)


# ---------------------------------------------------------------------------
# 已提交的首版登记表与证据在新规则下仍全部通过
# ---------------------------------------------------------------------------


class CommittedRegistryTests(unittest.TestCase):
    def test_every_declared_unit_of_the_committed_registry_still_passes_its_own_evidence(self) -> None:
        registry_path = REPO_ROOT / "tools" / "ci" / "command_input_scopes.json"
        if not registry_path.is_file():
            self.skipTest("首版登记表不在本分支")
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        checked = 0
        for unit_id, entry in sorted(registry["units"].items()):
            if entry.get("status") != "declared":
                continue
            evidence = REPO_ROOT / entry["sample"]["evidence"]
            with self.subTest(unit_id):
                sample = json.loads(evidence.read_text(encoding="utf-8"))
                ur.validate_declared_scope(entry["scope"])
                result = ur.declared_coverage(sample, entry["scope"], command_sha256=entry["sample"]["command_sha256"])
                self.assertTrue(result["ok"], result["undeclared"][:5])
                checked += 1
        # H10：frontend 三个单元在生产 WORK 位置会向上探测 {HOME} 祖先目录里不存在的配置文件，覆盖规则只许 /、/var/、/var/tmp/、{WORK}/ 为祖先，
        # 正式后台带采样时读集审计判未声明读取，已降为 rerun-only（理由写在 command_input_scopes.json 里）；这里剩 2 个 declared 单元。
        self.assertGreaterEqual(checked, 2, "登记表有 2 个 declared 单元（egress-scanner-check、egress-spec-egressscan-self-test）")


if __name__ == "__main__":
    unittest.main()
