"""声明级承接验证器的加固批次（步骤 5 工作包 WP1e；被测代码在 ``tools/ci/unit_records.py``）。

背景：独立验收（全新上下文）对 WP1～WP1d 的记录层与入口层做了对抗性核验，结论「有条件可以」：严重 0、重要 4、一般 10。调度者要求在步骤 8
更新钉值之前，把验证器侧能修的都修掉（否则每改一次规则就多一个身份变更周期）。本文件是这一批的正反例：

1. 重要 1 祖先目录：测试树严格祖先的目录键放行 list／dir／stat／missing，**read 只对 ``{WORK}/`` 这一个键放行**；
2. 重要 2 C6 git：从「出现 HEAD 才违规」的黑名单改成固定用法的白名单（抄采样器 ``git_usage_problem`` 的思路，不 import 它），
   并新增 ``C1-git-state``（读 .git 下引用库／索引状态而没有固定的 git 子进程解释）；
3. 重要 3 installed_dirs：前缀与合同绑定（范围校验按评审过的表核，消费者核验再拿合同条目的 detail.path 对一遍）；
4. 重要 4 畸形封存物：自洽的畸形封存物（不可哈希的条目名、几百层嵌套的合同 detail、深层嵌套的证据 JSON）落成「不可承接」的问题，不抛异常；
5. 一般 1 ``started_at_utc`` 不可解析时凭证「运行前核验」不得静默放过；
6. 一般 3 噪声探测去掉用户可写区与 ``{HOME}``；一般 4 scratch／virtual 前缀宽度；一般 8 glob 通配符上限；一般 9 ``valid`` 必须存在且为 True；
   一般 10 文档里的「放宽项汇总」；
7. 缓存合同的锁文件：声明了 ``cache:node-modules``／``cache:go-mod-cache`` 必须在 ``repo.files`` 里单独列出它的锁文件。

复用 ``test_ci_declared_inputs_records`` 与 ``test_ci_declared_inputs_probe_rules`` 的夹具。
"""

from __future__ import annotations

import copy
import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_probe_rules as probe
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base

try:  # 入口门禁侧：交叉核对常量用；不在的分支只跳过那一条
    from tools.ci import entry_gates
except ImportError:  # pragma: no cover
    entry_gates = None

A = base.COMMIT_A
B = base.COMMIT_B
# 真实布局：WORK 是测试树的父目录（TREE=WORK/test-tree），测试树的严格祖先链是 /、/var、/var/tmp、/var/tmp/gate、{WORK}。
WORK_PARENT = {"TREE": "/var/tmp/gate/work/test-tree", "WORK": "/var/tmp/gate/work", "HOME": "/root", "TMP": "/tmp"}
# WORK 是测试树的兄弟目录（不是祖先）。
WORK_SIBLING = {"TREE": "/var/tmp/gate/tree", "WORK": "/var/tmp/gate/work", "HOME": "/root", "TMP": "/tmp"}


def rules(result: dict) -> set[str]:
    return {item["rule"] for item in result["undeclared"]}


def cover(sample: dict, scope: dict | None = None, **kwargs) -> dict:
    return ur.declared_coverage(sample, scope if scope is not None else base.make_scope(), **kwargs)


def git_sample(commands: list, *, repo: tuple = ()) -> dict:
    """基准证据，git_commands 换成给定的命令，仓库内再追加给定的行。"""

    sample = base.make_sample()
    sample["git_commands"] = copy.deepcopy(commands)
    sample["repo_accesses"].extend(copy.deepcopy([list(row) for row in repo]))
    return sample


# ---------------------------------------------------------------------------
# 重要 1：测试树祖先目录键下，read 只对 {WORK}/ 放行
# ---------------------------------------------------------------------------


class AncestorReadTests(unittest.TestCase):
    def test_read_is_allowed_on_the_work_key_only(self) -> None:
        for key in ("{WORK}/", "{WORK}"):
            for kinds in (["read"], ["read", "stat"], ["list", "missing", "read", "stat"], ["dir", "read"]):
                with self.subTest(key=key, kinds=kinds):
                    result = probe.cover(outside=[[key, kinds, 5]], aliases=WORK_PARENT)
                    self.assertTrue(result["ok"], result["undeclared"])
        for key in ("/", "/var/", "/var/tmp/", "/var/tmp/gate/", "/var", "/var/tmp"):
            for kinds in (["read"], ["read", "stat"], ["list", "missing", "read", "stat"], ["dir", "read"]):
                with self.subTest(key=key, kinds=kinds):
                    self.assertEqual(probe.rules_of(probe.cover(outside=[[key, kinds, 5]], aliases=WORK_PARENT)), ["C2-outside-undeclared"],
                                     "祖先键下的 read 是读了该目录下别的直接子项（go.work、package.json、.npmrc……）")

    def test_the_other_kinds_stay_allowed_on_every_strict_ancestor(self) -> None:
        for key in ("/", "/var/", "/var/tmp/", "/var/tmp/gate/", "{WORK}/", "{WORK}"):
            for kinds in (["list"], ["dir", "stat"], ["missing"], ["list", "missing", "stat", "dir"]):
                with self.subTest(key=key, kinds=kinds):
                    result = probe.cover(outside=[[key, kinds, 3]], aliases=WORK_PARENT)
                    self.assertTrue(result["ok"], result["undeclared"])

    def test_write_and_exec_are_never_allowed_on_the_ancestors(self) -> None:
        for key in ("/", "/var/tmp/", "{WORK}/", "{WORK}"):
            for kinds in (["write"], ["exec"], ["read", "write"], ["read", "exec"], ["list", "write"]):
                with self.subTest(key=key, kinds=kinds):
                    self.assertEqual(probe.rules_of(probe.cover(outside=[[key, kinds, 1]], aliases=WORK_PARENT)), ["C2-outside-undeclared"])

    def test_the_work_key_needs_work_to_be_a_strict_ancestor_of_the_tree(self) -> None:
        """WORK 是测试树的兄弟目录（不在祖先链上）：{WORK}/ 下的 read 没有这条放行，要声明前缀。"""

        for aliases in (WORK_SIBLING, {**WORK_PARENT, "WORK": WORK_PARENT["TREE"]}, {**WORK_PARENT, "WORK": "relative/work"}):
            with self.subTest(aliases=aliases["WORK"]):
                self.assertEqual(probe.rules_of(probe.cover(outside=[["{WORK}/", ["read"], 1]], aliases=aliases)), ["C2-outside-undeclared"])

    def test_descendants_of_the_work_key_get_no_such_exemption(self) -> None:
        for key in ("{WORK}/go-build-cache/", "{WORK}/test-tree/", "{WORK}/x/y/"):
            with self.subTest(key=key):
                self.assertEqual(probe.rules_of(probe.cover(outside=[[key, ["read"], 1]], aliases=WORK_PARENT)), ["C2-outside-undeclared"])

    def test_a_tree_under_home_gets_no_read_on_the_home_key(self) -> None:
        aliases = {"TREE": "/root/ci/test-tree", "WORK": "/root/ci", "HOME": "/root", "TMP": "/tmp"}
        self.assertTrue(probe.cover(outside=[["{WORK}/", ["read", "list"], 1]], aliases=aliases)["ok"])
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{HOME}/", ["read"], 1]], aliases=aliases)), ["C2-outside-undeclared"])
        self.assertTrue(probe.cover(outside=[["{HOME}/", ["list", "missing"], 1]], aliases=aliases)["ok"])

    def test_the_shapes_of_the_final_real_samples_still_pass(self) -> None:
        """最终真实样本里：egress 两个单元的 {WORK}/ 键含 read；frontend:critical 的祖先链只有 dir／list／missing／stat。"""

        egress = [["{WORK}/", ["missing", "read", "stat"], 13]]
        frontend = [["/", ["dir", "list", "missing", "stat"], 23], ["/var/", ["dir", "list", "missing", "stat"], 16],
                    ["/var/tmp/", ["list", "missing"], 12], ["{WORK}/", ["list", "missing"], 12]]
        for label, rows in (("egress", egress), ("frontend", frontend)):
            with self.subTest(label):
                result = probe.cover(outside=rows, aliases=WORK_PARENT)
                self.assertTrue(result["ok"], result["undeclared"])


# ---------------------------------------------------------------------------
# 重要 2：C6 git 白名单与 C1-git-state
# ---------------------------------------------------------------------------


class GitWhitelistTests(unittest.TestCase):
    def test_fixed_usages_are_allowed(self) -> None:
        fixed = [
            ["git", "show", f"{A}:docs/egress/x.json"], ["/usr/bin/git", "show", A], ["git", "--no-pager", "show", "-s", "--format=%H", A],
            ["git", "show", "--stat", "--name-only", f"{A}^{{tree}}"], ["git", "show", f"{A}^{{commit}}"],
            ["git", "cat-file", "-p", A], ["git", "cat-file", "-t", A], ["git", "cat-file", "blob", f"{A}:x"], ["git", "cat-file", "-e", f"{A}^{{tree}}"],
            ["git", "rev-parse", "--verify", f"{A}^{{commit}}"], ["git", "rev-parse", "-q", "--verify", A], ["git", "rev-parse", A],
            ["git", "merge-base", A, A], ["git", "merge-base", "--is-ancestor", A, A], ["git", "merge-base", "--all", A, A],
            ["git", "ls-tree", "-r", "--name-only", A, "--", "docs/egress"], ["git", "ls-tree", "-z", A, "--", ".git/HEAD"], ["git", "ls-tree", "--abbrev=8", A],
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", A], ["git", "diff-tree", "-r", A, A],
            ["git", "log", "--format=%H", f"{A}..{A}"], ["git", "log", "-n", "3", A], ["git", "log", "-5", A], ["git", "log", f"^{A}", A],
            ["git", "log", f"{A}...{A}", "--format=%H"], ["git", "log", "--max-count=2", A],
            ["git", "rev-list", "--count", f"{A}...{A}"], ["git", "rev-list", "--max-count=2", A], ["git", "rev-list", "--not", f"^{A}", A],
            # “:”之后是对象内的路径，“--”之后是路径：里面出现 HEAD 之类的字样不算引用。
            ["git", "show", f"{A}:HEAD"], ["git", "show", A, "--", "HEAD"], ["git", "log", A, "--", "HEAD", "refs/heads/main"],
        ]
        for argv in fixed:
            with self.subTest(argv=argv):
                self.assertEqual(ur._declared_git_usage_problem(argv), None)
                result = cover(git_sample([argv]))
                self.assertTrue(result["ok"], result["undeclared"])

    def test_unfixed_usages_are_violations(self) -> None:
        """每一类都带「为什么」：HEAD 一类浮动引用报 C6-head，其余一律 C6-git-unfixed。"""

        unfixed = {
            "HEAD": (["git", "show", "HEAD"], "C6-head"), "HEAD~1": (["git", "show", "HEAD~1"], "C6-head"), "HEAD^": (["git", "rev-parse", "HEAD^"], "C6-head"),
            "ORIG_HEAD": (["git", "show", "ORIG_HEAD:x"], "C6-head"), "FETCH_HEAD": (["git", "show", "FETCH_HEAD"], "C6-head"),
            "MERGE_HEAD": (["git", "rev-parse", "MERGE_HEAD"], "C6-head"), "范围写法里的 HEAD": (["git", "log", f"{A}..HEAD"], "C6-head"),
            "@": (["git", "show", "@"], "C6-git-unfixed"), "@{upstream}": (["git", "rev-parse", "@{upstream}"], "C6-git-unfixed"),
            "@:路径": (["git", "show", "@:x"], "C6-git-unfixed"),
            "缩写提交号": (["git", "show", "abc1234"], "C6-git-unfixed"), "12 位缩写": (["git", "show", A[:12]], "C6-git-unfixed"),
            "39 位": (["git", "show", A[:39]], "C6-git-unfixed"), "41 位": (["git", "show", A + "a"], "C6-git-unfixed"),
            "大写提交号": (["git", "show", A.upper()], "C6-git-unfixed"),
            "分支名": (["git", "show", "main"], "C6-git-unfixed"), "分支名:路径": (["git", "show", "main:x"], "C6-git-unfixed"),
            "标签名": (["git", "show", "v1.2.3"], "C6-git-unfixed"), "远端引用": (["git", "rev-parse", "origin/main"], "C6-git-unfixed"),
            "origin/HEAD": (["git", "log", "origin/HEAD", "--format=%H"], "C6-git-unfixed"), "refs/heads/main": (["git", "show", "refs/heads/main"], "C6-git-unfixed"),
            "没有修订（show）": (["git", "show"], "C6-git-unfixed"), "没有修订（log）": (["git", "log", "--oneline"], "C6-git-unfixed"),
            "没有修订（ls-tree）": (["git", "ls-tree", "-r"], "C6-git-unfixed"), "没有修订（cat-file）": (["git", "cat-file", "-p"], "C6-git-unfixed"),
            "-C": (["git", "-C", "/x", "show", A], "C6-git-unfixed"), "-c": (["git", "-c", "core.x=y", "show", A], "C6-git-unfixed"),
            "--git-dir": (["git", "--git-dir=/x/.git", "show", A], "C6-git-unfixed"), "--work-tree": (["git", "--work-tree=/x", "show", A], "C6-git-unfixed"),
            "cat-file --batch": (["git", "cat-file", "--batch"], "C6-git-unfixed"), "cat-file --batch-check": (["git", "cat-file", "--batch-check", A], "C6-git-unfixed"),
            # 选项本身不在放行清单里：单独写或者带上固定修订都要违规（只写选项时即使放行它，也会因「没给出修订」而违规，测不出选项清单）。
            "cat-file --batch 带固定修订": (["git", "cat-file", "--batch", A], "C6-git-unfixed"),
            "cat-file --batch-all-objects 带固定修订": (["git", "cat-file", "--batch-all-objects", A], "C6-git-unfixed"),
            "show --stdin": (["git", "show", "--stdin"], "C6-git-unfixed"), "rev-list --stdin": (["git", "rev-list", "--stdin"], "C6-git-unfixed"),
            "show --stdin 带固定修订": (["git", "show", "--stdin", A], "C6-git-unfixed"),
            "rev-list --stdin 带固定修订": (["git", "rev-list", "--stdin", A], "C6-git-unfixed"),
            "rev-list --all": (["git", "rev-list", "--all"], "C6-git-unfixed"), "log --all": (["git", "log", "--all", A], "C6-git-unfixed"),
            "rev-list --all 带固定修订": (["git", "rev-list", "--all", A], "C6-git-unfixed"),
            "rev-parse --all 带固定修订": (["git", "rev-parse", "--all", A], "C6-git-unfixed"),
            "diff-tree --stdin 带固定修订": (["git", "diff-tree", "--stdin", A], "C6-git-unfixed"),
            "merge-base --fork-point 带固定修订": (["git", "merge-base", "--fork-point", A, A], "C6-git-unfixed"),
            "log --since": (["git", "log", "--since=1.day", A], "C6-git-unfixed"),
            "只给排除的修订": (["git", "log", f"^{A}"], "C6-git-unfixed"), "-n 后不是数字": (["git", "log", "-n", "x", A], "C6-git-unfixed"),
            "rev-parse --show-toplevel": (["git", "rev-parse", "--show-toplevel"], "C6-git-unfixed"),
            "rev-parse --abbrev-ref": (["git", "rev-parse", "--abbrev-ref", A], "C6-git-unfixed"),
            "rev-parse --verify 两个修订": (["git", "rev-parse", "--verify", A, A], "C6-git-unfixed"),
            "merge-base 只有一个": (["git", "merge-base", A], "C6-git-unfixed"), "cat-file 两个对象": (["git", "cat-file", "-p", A, A], "C6-git-unfixed"),
            "log 范围一端不是提交号": (["git", "log", f"{A}..main"], "C6-git-unfixed"),
            "没有子命令": (["git"], "C6-git-unfixed"), "没有子命令（只有全局选项）": (["git", "--no-pager"], "C6-git-unfixed"),
        }
        for name in ("status", "diff", "describe", "for-each-ref", "tag", "branch", "ls-remote", "remote", "config", "stash", "init", "worktree",
                     "checkout", "fetch", "blame", "grep", "ls-files", "symbolic-ref", "update-ref", "reflog", "clone", "add", "commit"):
            unfixed[f"子命令 {name}"] = (["git", name, A], "C6-git-unfixed")
        for label, (argv, rule) in unfixed.items():
            with self.subTest(label):
                self.assertIsNotNone(ur._declared_git_usage_problem(argv), label)
                found = rules(cover(git_sample([argv])))
                self.assertIn(rule, found, (label, found))
                self.assertFalse(any(name.startswith("C7") for name in found), "C6 是真违规，不是证据不完整")

    def test_the_head_rule_ignores_object_paths_and_paths_after_the_separator(self) -> None:
        for argv in (["git", "show", f"{A}:HEAD"], ["git", "log", A, "--", "HEAD"], ["git", "ls-tree", A, "--", ".git/HEAD", "ORIG_HEAD"]):
            with self.subTest(argv=argv):
                self.assertTrue(cover(git_sample([argv]))["ok"])

    def test_only_unfixed_commands_get_the_new_rule_and_each_command_is_judged_alone(self) -> None:
        result = cover(git_sample([["git", "show", A], ["git", "status"], ["git", "log", A]]))
        self.assertEqual([(item["rule"], item["path"]) for item in result["undeclared"]], [("C6-git-unfixed", "git status")])
        self.assertIn("status", result["undeclared"][0]["reason"])

    def test_undeclared_commits_and_hidden_argv_and_foreign_commands_keep_their_rules(self) -> None:
        self.assertEqual(rules(cover(git_sample([["git", "show", f"{B}:x"]]))), {"C6-commit-undeclared"})
        self.assertEqual(rules(cover(git_sample([["git", "<argv-not-visible>"]]))), {"C6-git-argv-hidden"})
        self.assertEqual(rules(cover(git_sample([["curl", "http://x"]]))), {"C6-git"})
        self.assertEqual(rules(cover(git_sample([["git", "show", A, "<argv-not-visible>"]]))), {"C6-git-argv-hidden"})

    def test_the_new_rule_names_are_literals_in_the_source_and_never_c7(self) -> None:
        source = inspect.getsource(ur.declared_coverage)
        for name in ("C6-git-unfixed", "C1-git-state"):
            self.assertIn(f'"{name}"', source, "规则名必须以字面量出现在 declared_coverage 源码里（规则闭合词表测试靠它扫描）")
            self.assertFalse(name.startswith("C7"), "真违规，不进证据不完整类")

    def test_the_executor_treats_the_new_rules_as_real_violations(self) -> None:
        """执行器把「只因证据不完整」的未覆盖单独归类（DECLARED_INCOMPLETE_RULES，全是 C7 开头）；C6／C1 的新规则不得混进去。"""

        try:
            from tools.ci import unit_executor
        except ImportError:  # pragma: no cover
            self.skipTest("执行器不在本分支")
        for name in ("C6-git-unfixed", "C1-git-state", "C6-head", "C6-git", "C6-commit-undeclared", "C6-git-argv-hidden"):
            self.assertNotIn(name, unit_executor.DECLARED_INCOMPLETE_RULES, name)
            self.assertEqual(unit_executor.declared_audit_verdict([{"rule": name, "path": "", "kinds": [], "reason": "x"}]), "failed", name)


class GitStateReadTests(unittest.TestCase):
    STATE = ("HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "index", "packed-refs", "refs", "refs/heads/main", "refs/replace", "refs/tags/v1",
             "logs", "logs/HEAD", "logs/refs/heads/main")

    def state_rows(self, kinds: list) -> tuple:
        return tuple([f"{{TREE}}/.git/{name}", list(kinds), False] for name in self.STATE)

    def test_state_reads_are_explained_only_by_a_fixed_git_subprocess(self) -> None:
        explained = git_sample([["git", "show", f"{A}:x"]], repo=self.state_rows(["read"]))
        self.assertTrue(cover(explained)["ok"], "有固定的 git 子进程：git 启动时会读 HEAD、packed-refs、refs/replace/")
        for commands, label in (([], "没有任何 git 子进程"), ([["git", "status"]], "只有不固定的 git 子进程"),
                                ([["git", "show", A], ["git", "log", "--oneline"]], "固定与不固定混着")):
            with self.subTest(label):
                result = cover(git_sample(commands, repo=self.state_rows(["read"])))
                self.assertEqual({item["path"] for item in result["undeclared"] if item["rule"] == "C1-git-state"},
                                 {f"{{TREE}}/.git/{name}" for name in self.STATE}, label)

    def test_list_counts_as_a_read_but_stat_dir_missing_do_not(self) -> None:
        for kinds, expected in ((["list"], True), (["read", "stat"], True), (["stat"], False), (["dir", "stat"], False), (["missing"], False), (["missing", "stat", "dir"], False)):
            with self.subTest(kinds=kinds):
                found = rules(cover(git_sample([], repo=self.state_rows(kinds))))
                self.assertEqual("C1-git-state" in found, expected)

    def test_other_git_paths_are_unchanged(self) -> None:
        rows = (["{TREE}/.git/config", ["read"], False], ["{TREE}/.git/objects/cd/ef01", ["read"], False], ["{TREE}/.git/objects/pack/pack-1.idx", ["read"], False],
                ["{TREE}/.git/objects/", ["read", "list"], False], ["{TREE}/.git/info/exclude", ["read", "missing"], False], ["{TREE}/.git", ["stat", "dir"], False],
                ["{TREE}/.git/commondir", ["missing"], False])
        self.assertTrue(cover(git_sample([], repo=rows))["ok"], "没有固定 git 子进程时，对象库、config 等读取仍照旧放行（声明了 git_history 即可）；"
                                                               "只 stat／dir／missing 的状态路径也不算读")

    def test_the_older_git_rules_keep_their_precedence(self) -> None:
        no_history = base.make_scope()
        del no_history["repo"]["git_history"]
        self.assertEqual(rules(cover(git_sample([], repo=(["{TREE}/.git/HEAD", ["read"], False],)), no_history)), {"C1-git-undeclared"})
        self.assertEqual(rules(cover(git_sample([["git", "show", A]], repo=(["{TREE}/.git/index", ["write"], False],)))), {"C1-git-kinds"})
        self.assertEqual(rules(cover(git_sample([["git", "show", A]], repo=(["{TREE}/.git/HEAD", ["read", "exec"], False],)))), {"C1-git-kinds"})

    def test_the_counts_distinguish_allowed_state_reads(self) -> None:
        result = cover(git_sample([["git", "show", A]], repo=self.state_rows(["read"])))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["counts"]["git_paths"], len(self.STATE))


# ---------------------------------------------------------------------------
# 重要 3：installed_dirs 前缀与合同绑定；缓存合同的锁文件
# ---------------------------------------------------------------------------


class InstalledDirsBindingTests(unittest.TestCase):
    def scope(self, installed: list, *, contracts=None) -> dict:
        scope = base.frontend_scope()
        scope["repo"]["installed_dirs"] = installed
        if contracts is not None:
            scope["contracts"] = contracts
        return scope

    def test_only_the_reviewed_contract_with_its_own_directory_is_accepted(self) -> None:
        good = {"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}
        self.assertEqual(ur.validate_declared_scope(self.scope([good]))["repo"]["installed_dirs"], [good])
        self.assertEqual(ur._DECLARED_INSTALLED_PREFIXES, {"cache:node-modules": "frontend/node_modules/"})
        for prefix in ("backend/internal/", "src/gen/", "tools/node_modules/", "backend/node_modules/", "frontend/node_modules/.pnpm/",
                       "frontend/node_modules/.vite/", "docs/egress/"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(self.scope([{"prefix": prefix, "contract": "cache:node-modules"}]))
            self.assertIn("必须等于合同 cache:node-modules 定义的安装目录", str(caught.exception))

    def test_other_cache_contracts_cannot_be_used_as_installed_dirs(self) -> None:
        for contract in ("cache:go-mod-cache", "cache:go-build-cache", "cache:other"):
            scope = self.scope([{"prefix": "frontend/node_modules/", "contract": contract}],
                               contracts=["toolchain:node", "toolchain:pnpm", "cache:node-modules", contract])
            scope["repo"]["files"] = [*scope["repo"]["files"], "backend/go.sum"]  # 免得先被「cache:go-mod-cache 要列锁文件」的规则拦下
            with self.subTest(contract=contract), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(scope)
            self.assertIn("不是评审过的依赖安装合同", str(caught.exception))

    def test_a_second_directory_for_the_same_contract_is_refused(self) -> None:
        both = [{"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}, {"prefix": "tools/node_modules/", "contract": "cache:node-modules"}]
        with self.assertRaises(ValueError):
            ur.validate_declared_scope(self.scope(both))

    def test_the_table_is_cross_checked_against_the_entry_gates_definition(self) -> None:
        if entry_gates is None:
            self.skipTest("入口门禁侧不在本分支")
        definition = entry_gates.DECLARED_CACHES["node-modules"]
        self.assertEqual(definition["path"] + "/", ur._DECLARED_INSTALLED_PREFIXES["cache:node-modules"])
        self.assertEqual(definition["role"], "dependency-install")
        for contract, lock in ur._DECLARED_CACHE_CONTRACT_LOCKS.items():
            name = contract.split(":", 1)[1]
            self.assertEqual(entry_gates.DECLARED_CACHES[name]["lock_file"], lock, contract)
            self.assertEqual(ur._DECLARED_LOCK_DEFAULTS[entry_gates.DECLARED_CACHES[name]["rule"]], lock, contract)

    def frontend_world(self, entries) -> base.DeclaredWorld:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return base.DeclaredWorld(Path(directory.name), scope=base.frontend_scope(), sample=base.frontend_sample(), contracts=entries,
                                  credentials=base.frontend_credentials(base.FRONTEND_LOCK_SHA, base.FRONTEND_LOCK_SHA), unit_id="frontend:lint", cwd_rel=".")

    def test_the_consumer_compares_the_prefix_with_the_contract_entry_path(self) -> None:
        self.assertEqual(self.frontend_world(base.frontend_contract_entries()).check(), [], "夹具有效：合同条目 path 是 frontend/node_modules")
        for label, path in (("path 不同", "backend/internal"), ("path 带尾斜杠", "frontend/node_modules/"), ("path 是空串", ""), ("path 不是字符串", ["frontend/node_modules"])):
            entries = base.frontend_contract_entries()
            entries[2] = base.contract_entry("cache", "node-modules", role="dependency-install", location="tree", rule="lock-derived-pnpm", path=path,
                                             lock_file="frontend/pnpm-lock.yaml", installed_lock="frontend/node_modules/.pnpm/lock.yaml")
            with self.subTest(label):
                problems = self.frontend_world(entries).check()
                self.assertTrue(any("与合同 cache:node-modules 条目里的 path" in problem for problem in problems), problems)
        entries = base.frontend_contract_entries()
        entries[2] = base.contract_entry("cache", "node-modules", role="dependency-install", location="tree", rule="lock-derived-pnpm",
                                         lock_file="frontend/pnpm-lock.yaml", installed_lock="frontend/node_modules/.pnpm/lock.yaml")
        problems = self.frontend_world(entries).check()
        self.assertTrue(any("与合同 cache:node-modules 条目里的 path" in problem and "None" in problem for problem in problems), "detail 里没有 path：报问题")


class CacheLockFileTests(unittest.TestCase):
    def test_declaring_a_lock_derived_cache_requires_its_lock_file_among_the_files(self) -> None:
        self.assertEqual(ur._DECLARED_CACHE_CONTRACT_LOCKS, {"cache:node-modules": "frontend/pnpm-lock.yaml", "cache:go-mod-cache": "backend/go.sum"})
        # 前端：node-modules 缓存
        scope = base.frontend_scope()
        ur.validate_declared_scope(scope)
        scope["repo"]["files"] = [path for path in scope["repo"]["files"] if path != "frontend/pnpm-lock.yaml"]
        scope["contracts"] = ["toolchain:node", "cache:node-modules"]  # 去掉 toolchain:pnpm，免得先被 toolchain 的锁文件规则拦
        with self.assertRaises(ValueError) as caught:
            ur.validate_declared_scope(scope)
        self.assertIn("声明了 cache:node-modules 就必须在 repo.files 里单独列出它的锁文件 frontend/pnpm-lock.yaml", str(caught.exception))
        # 后端：go-mod-cache
        backend = base.make_scope()
        ur.validate_declared_scope(backend)
        backend["repo"]["files"] = [path for path in backend["repo"]["files"] if path != "backend/go.sum"]
        backend["contracts"] = [item for item in backend["contracts"] if item != "toolchain:go"]
        backend["outside"] = [item for item in backend["outside"] if item.get("contract") != "toolchain:go"]
        with self.assertRaises(ValueError) as caught:
            ur.validate_declared_scope(backend)
        self.assertIn("声明了 cache:go-mod-cache 就必须在 repo.files 里单独列出它的锁文件 backend/go.sum", str(caught.exception))

    def test_other_caches_need_no_lock_file(self) -> None:
        def scope(*, with_mod_cache: bool, lock: bool) -> dict:
            built = probe.bare_scope()
            built["contracts"] = ["cache:go-build-cache"] + (["cache:go-mod-cache"] if with_mod_cache else [])
            built["outside"] = [{"prefix": "{WORK}/go-build-cache/", "class": "performance-cache", "contract": "cache:go-build-cache"}]
            if with_mod_cache:
                built["outside"].append({"prefix": "{WORK}/go-mod-cache/", "class": "dependency-content", "contract": "cache:go-mod-cache"})
            if lock:
                built["repo"]["files"] = ["backend/go.sum"]
            return built

        ur.validate_declared_scope(scope(with_mod_cache=False, lock=False))  # go-build-cache 不要求任何锁文件
        with self.assertRaises(ValueError):
            ur.validate_declared_scope(scope(with_mod_cache=True, lock=False))
        ur.validate_declared_scope(scope(with_mod_cache=True, lock=True))

    def test_a_lock_inside_a_range_is_not_enough(self) -> None:
        """范围聚合摘要里的锁文件不能被消费者单独交叉核对：必须是 repo.files 里的单文件条目。"""

        scope = base.frontend_scope()
        scope["repo"]["files"] = ["frontend/package.json", "Makefile"]  # frontend/ 这个 range 覆盖 frontend/pnpm-lock.yaml，但它不是单文件条目
        scope["contracts"] = ["toolchain:node", "cache:node-modules"]
        with self.assertRaises(ValueError):
            ur.validate_declared_scope(scope)


# ---------------------------------------------------------------------------
# 重要 4：自洽的畸形封存物不抛异常
# ---------------------------------------------------------------------------


class MalformedSealedRecordTests(base.DeclaredTestCase):
    def reseal_inputs(self, mutate_inputs, *, current_follows: bool = True) -> dict:
        """改输入明细、重算 inputs_sha256（read_audit 里的绑定一并改）、把「当前输入」换成改后的明细，再重新封存——自洽的畸形封存物。
        ``current_follows`` 为假时「当前输入」保持原样：当前输入是执行器按声明实时算出的，永远是格式合格的字典数组（不会混进字符串或
        null），封存的明细与它不符时，通用核对会报问题，声明级核验也必须照常返回而不是崩。"""

        world = self.world
        inputs = copy.deepcopy(world.base["record"]["inputs"])
        inputs = mutate_inputs(inputs) or inputs
        try:
            digest = ur.entries_sha256(inputs)
        except (KeyError, TypeError):  # 条目缺键之类：算不出摘要，用占位值（核验照样必须落成问题）
            digest = "0" * 64

        def mutate(body: dict) -> None:
            body["inputs"], body["inputs_sha256"] = inputs, digest
            body["read_audit"]["inputs_sha256"] = digest

        try:
            item = world.reseal(mutate)
        except RecursionError:  # 夹具自己封存这份畸形记录时就超了本 Python 的 JSON 编码递归上限（3.12 的 C 层上限比 3.13／3.14 小）
            self.skipTest("本 Python 的 JSON 编码递归上限太小，造不出这份嵌套很深的畸形封存物（纯函数用例仍覆盖遍历的迭代实现）")
        if current_follows:
            item["current"] = ur.Current(unit_id=world.unit_id, unit_type="command", spec=item["record"]["spec"],
                                         spec_sha256=ur.sha256_json(item["record"]["spec"]), inputs=inputs, inputs_sha256=digest, inheritable=True)
        return item

    def check_returns_problems(self, item: dict, *, fragment: str | None = None) -> list[str]:
        try:
            problems = self.world.check(item)
        except Exception as error:  # noqa: BLE001 - 目标就是任何异常
            self.fail(f"自洽的畸形封存物让核验抛了 {type(error).__name__}：{error}")
        self.assertTrue(problems, "畸形封存物必须被判不可承接")
        self.assertTrue(all(isinstance(problem, str) for problem in problems))
        if fragment is not None:
            self.assertTrue(any(fragment in problem for problem in problems), problems)
        return problems

    def audit_directly(self, item: dict) -> list[str]:
        """绕过 check_record 的路由包装，直接调声明级核验：包装之外的每一处内层加固自己也必须返回问题、不抛异常。"""

        try:
            return ur._check_declared_audit(self.world.store, item["record"], item["current"], item["facts"])
        except Exception as error:  # noqa: BLE001 - 目标就是任何异常
            self.fail(f"声明级核验本身（不经路由包装）抛了 {type(error).__name__}：{error}")

    # ---- 报告里的三处最小复现 ----

    def test_an_entry_name_that_is_a_list_or_a_dict_is_a_problem_not_a_type_error(self) -> None:
        for value in ([], ["x"], {}, {"a": 1}, 7, None):
            with self.subTest(name=value):
                item = self.reseal_inputs(lambda inputs, value=value: inputs + [{"category": "x", "name": value, "sha256": "0" * 64}])
                self.check_returns_problems(item)

    def test_a_contract_detail_nested_hundreds_of_levels_deep_is_a_problem_not_a_recursion_error(self) -> None:
        def nest(depth: int):
            node: object = "leaf"
            for _ in range(depth):
                node = [node]
            return node

        for depth in (600,):
            with self.subTest(depth=depth):
                def mutate_inputs(inputs, depth=depth):
                    for entry in inputs:
                        if entry.get("name") == "contract:cache:go-build-cache":
                            entry["detail"] = {**entry["detail"], "nested": nest(depth)}
                    return inputs

                item = self.reseal_inputs(mutate_inputs)
                self.check_returns_problems(item)

    def test_run_origin_evidence_that_is_deeply_nested_json_is_a_problem(self) -> None:
        world = self.world
        directory = Path(self._directory.name) / "deep"
        directory.mkdir()
        deep = directory / "deep.json"
        deep.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        digest = ur.file_sha256(deep)
        world.store.put_log(deep, digest)
        item = world.build(audit_overrides={"sample_origin": "run", "trace": {"path": "evidence.json", "sha256": digest, "stored": "x"}})
        self.check_returns_problems(item, fragment="读不出来或格式非法")

    # ---- 其它畸形变体 ----

    def test_each_inner_fix_works_without_the_route_wrapper(self) -> None:
        """路由包装是兜底；报告点名的三处还要各自在内层修好（条目名只收字符串、合同 detail 的遍历迭代且设深度上限、run 来源的证据解析捕获
        RecursionError），所以这里直接调 _check_declared_audit，不经包装。"""

        named = self.reseal_inputs(lambda inputs: inputs + [{"category": "x", "name": [], "sha256": "0" * 64}])
        self.assertTrue(any("条目名不是字符串" in problem for problem in self.audit_directly(named)))

        def nest(inputs):
            node: object = "leaf"
            for _ in range(600):
                node = [node]
            for entry in inputs:
                if entry.get("name") == "contract:cache:go-build-cache":
                    entry["detail"] = {**entry["detail"], "nested": node}
            return inputs

        self.assertTrue(any("unavailable" in problem for problem in self.audit_directly(self.reseal_inputs(nest))))
        directory = Path(self._directory.name) / "deep-direct"
        directory.mkdir()
        deep = directory / "deep.json"
        deep.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        digest = ur.file_sha256(deep)
        self.world.store.put_log(deep, digest)
        run_item = self.world.build(audit_overrides={"sample_origin": "run", "trace": {"path": "evidence.json", "sha256": digest, "stored": "x"}})
        self.assertTrue(any("读不出来或格式非法" in problem for problem in self.audit_directly(run_item)))
        # Python 3.13／3.14 的 json 解析器对 10 万层嵌套不再抛 RecursionError（走到后面的格式核验被判非法），3.12 会抛——解析器抛 RecursionError 的这条
        # 路径要在任何 Python 版本上都被钉住，所以另用一个对深层文本抛 RecursionError 的 json.loads 来确定性地触发它。
        real_loads = json.loads

        def loads_that_overflows_on_deep_text(text, *args, **kwargs):
            if isinstance(text, str) and text.startswith("[[[["):
                raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")
            return real_loads(text, *args, **kwargs)

        with mock.patch.object(ur.json, "loads", side_effect=loads_that_overflows_on_deep_text):
            problems = self.audit_directly(run_item)
        self.assertTrue(any("读不出来或格式非法" in problem and "maximum recursion depth" in problem for problem in problems), problems)

    def test_the_route_wrapper_turns_any_malformed_content_error_into_a_problem(self) -> None:
        item = self.world.base
        for error in (TypeError("x"), KeyError("k"), ValueError("v"), RecursionError("r"), UnicodeError("u"), OverflowError("o")):
            with self.subTest(error=type(error).__name__), mock.patch.object(ur, "_check_declared_audit", side_effect=error):
                problems = self.world.check(item)
                self.assertTrue(any("核验封存物时遇到畸形内容" in problem and type(error).__name__ in problem for problem in problems), problems)
        # 包装只管畸形内容会引出的那几种异常：程序缺陷最常见的 AttributeError（拼错属性名）和别的异常都不被吞掉。
        for bug in (RuntimeError("bug"), AttributeError("no attribute"), NameError("name"), ZeroDivisionError("zero"), AssertionError("assert")):
            with self.subTest(bug=type(bug).__name__), mock.patch.object(ur, "_check_declared_audit", side_effect=bug), self.assertRaises(type(bug)):
                self.world.check(item)

    def test_the_wrapper_names_the_frame_where_the_malformed_content_blew_up(self) -> None:
        """问题文本里带出错帧（函数名:行号）：真正抛异常的帧，而不是包装自己，免得程序缺陷被笼统的「畸形内容」盖住。"""

        def explode(store, record, current, facts):
            raise KeyError("sha256")

        with mock.patch.object(ur, "_check_declared_audit", explode):
            problems = self.world.check(self.world.base)
        found = [problem for problem in problems if "核验封存物时遇到畸形内容" in problem]
        self.assertEqual(len(found), 1, problems)
        self.assertRegex(found[0], r"出错帧 explode:\d+")
        self.assertIn("KeyError", found[0])

    def test_variants_of_self_consistent_malformed_sealed_records(self) -> None:
        def entry_named(name: str):
            return lambda inputs: next(entry for entry in inputs if entry.get("name") == name)

        variants = {
            "范围条目的 detail 嵌套几百层": lambda inputs: entry_named(ur.DECLARED_SCOPE_ENTRY)(inputs).update(detail=_deep_dict(600)),
            "输入条目缺 sha256": lambda inputs: inputs.append({"category": "x", "name": "file:backend/x"}),
            "输入条目是字符串": lambda inputs: inputs.append("not-an-entry"),
            "输入条目是 null": lambda inputs: inputs.append(None),
            "合同条目的 detail 是数组": lambda inputs: entry_named("contract:cache:go-build-cache")(inputs).update(detail=[1, 2, 3]),
            "合同条目的 detail 缺 type": lambda inputs: entry_named("contract:toolchain:go")(inputs)["detail"].pop("type"),
            "合同条目的 sha256 不是字符串": lambda inputs: entry_named("contract:toolchain:go")(inputs).update(sha256=["0" * 64]),
            "file: 条目的 sha256 是对象": lambda inputs: entry_named("file:backend/go.sum")(inputs).update(sha256={"a": 1}),
        }
        not_mappings = ("输入条目是字符串", "输入条目是 null")
        for label, change in variants.items():
            with self.subTest(label):
                item = self.reseal_inputs(lambda inputs, change=change: (change(inputs), inputs)[1], current_follows=label not in not_mappings)
                self.check_returns_problems(item)

    def test_a_declared_contracts_array_with_unhashable_ids_is_a_problem(self) -> None:
        for items in ([[]], [{}], [{"id": [], "kind": "cache", "verified_before_run": True, "observed": {}, "verified_at_utc": "2026-10-15T00:00:05Z"}],
                      [{"id": "cache:go-build-cache", "kind": [], "verified_before_run": True, "observed": {}, "verified_at_utc": "2026-10-15T00:00:05Z"}]):
            with self.subTest(items=items):
                item = self.world.reseal(lambda body, items=items: body.update(declared_contracts=items))
                self.check_returns_problems(item)

    def test_the_unavailable_scan_is_iterative_and_depth_capped(self) -> None:
        deep: object = "unavailable(x)"
        for _ in range(100000):
            deep = [deep]
        self.assertTrue(ur._declared_unavailable(deep), "超过深度上限视为不可用，且不抛 RecursionError")
        wide = {"a": {"b": [{"c": "ok"}, {"d": ["fine"]}]}}
        self.assertFalse(ur._declared_unavailable(wide))
        wide["a"]["b"][1]["d"].append("unavailable(no-home)")
        self.assertTrue(ur._declared_unavailable(wide))
        self.assertFalse(ur._declared_unavailable(None))
        self.assertFalse(ur._declared_unavailable(7))
        self.assertFalse(ur._declared_unavailable("available"))
        self.assertTrue(ur._declared_unavailable("unavailable"))
        edge: object = "fine"
        for _ in range(ur._DECLARED_MAX_NESTING):
            edge = {"k": edge}
        self.assertFalse(ur._declared_unavailable(edge), "恰在上限以内（容器层数等于上限）的嵌套不算异常")
        self.assertTrue(ur._declared_unavailable({"k": edge}), "再深一层就超限")

    def test_the_route_wrapper_does_not_change_the_generic_check_record(self) -> None:
        """包装只加在声明级核验上；通用项的问题照旧原样返回（check_record 本身一个字节没动，T-C 系列钉着）。"""

        item = self.world.base
        problems = self.world.check(item)
        self.assertEqual(problems, [])
        broken = self.world.reseal(lambda body: body.update(passed=False))
        self.assertTrue(self.world.check(broken))


def _deep_dict(depth: int) -> dict:
    node: object = "leaf"
    for _ in range(depth):
        node = {"k": node}
    return node  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 一般 1：started_at_utc 不可解析时凭证先后检查不得静默失效
# ---------------------------------------------------------------------------


class StartedAtTests(base.DeclaredTestCase):
    def test_an_unparseable_start_time_is_a_problem_for_every_credential(self) -> None:
        self.assertEqual(self.world.check(), [])
        for label, value in (("null", None), ("乱码", "garbage"), ("空串", ""), ("数字", 12345), ("空格分隔", "2026-10-15 00:00:10"), ("缺 Z", "2026-10-15T00:00:10"),
                             ("数组", ["2026-10-15T00:00:10Z"])):
            item = self.world.reseal(lambda body, value=value: body.update(started_at_utc=value))
            with self.subTest(label):
                problems = self.world.check(item)
                self.assertTrue(any("started_at_utc 不可解析" in problem for problem in problems), problems)

    def test_a_late_verification_is_still_reported_and_a_good_start_time_is_fine(self) -> None:
        late = self.world.reseal(lambda body: body["declared_contracts"][0].update(verified_at_utc="2030-01-01T00:00:00Z"))
        self.assertTrue(any("晚于单元启动时间" in problem for problem in self.world.check(late)))
        self.assertEqual(self.world.check(), [])


# ---------------------------------------------------------------------------
# 一般 3：噪声探测收紧
# ---------------------------------------------------------------------------


class NoiseTighteningTests(unittest.TestCase):
    def test_the_probe_roots_are_only_system_state(self) -> None:
        self.assertEqual(ur._DECLARED_PROBE_ROOTS, ("/usr/", "/opt/", "/var/lib/", "/var/cache/", "/var/run/", "/var/log/", "/run/", "/etc/", "/lib/",
                                                    "/lib64/", "/bin/", "/sbin/"))
        self.assertEqual(ur._DECLARED_PROBE_EXCLUDED, ("/run/user/",))
        for root in ur._DECLARED_PROBE_ROOTS:
            self.assertTrue(root.startswith("/") and root.endswith("/"), root)
            self.assertNotIn(root, ("/var/", "/var/tmp/", "/tmp/", "/home/", "/root/"), "用户可写区与整个 /var/ 不在放行根里")

    def test_var_is_split_and_only_the_system_maintained_subtrees_are_noise(self) -> None:
        for root in ("/var/lib/", "/var/cache/", "/var/run/", "/var/log/"):
            for kinds in (["missing"], ["stat"], ["dir", "stat"]):
                with self.subTest(root=root, kinds=kinds):
                    self.assertTrue(probe.cover(outside=[[f"{root}x/y/", kinds, 1]], aliases=probe.FAR)["ok"])
        # /var/tmp、/var/spool、/var/mail、/var/www……整个 /var/ 不再放行，只放系统自己维护的那四棵子树。
        for key in ("/var/tmp/x/", "/var/spool/x/", "/var/mail/x", "/var/www/x/", "/var/x/", "/var/tmpx/y/", "/var/libx/y/", "/var/runx/y/"):
            with self.subTest(key=key):
                self.assertEqual(probe.rules_of(probe.cover(outside=[[key, ["missing"], 1]], aliases=probe.FAR)), ["C3-missing-undeclared"])
        # 而且 read／list 之类不属于探测的访问仍然不放行。
        self.assertEqual(probe.rules_of(probe.cover(outside=[["/var/lib/x/", ["read"], 1]], aliases=probe.FAR)), ["C2-outside-undeclared"])

    def test_run_is_noise_except_the_per_user_runtime_directories(self) -> None:
        for key in ("/run/systemd/resolve/", "/run/nscd/", "/run/lock/x", "/run/"):
            with self.subTest(key=key):
                self.assertTrue(probe.cover(outside=[[key, ["missing"], 1]], aliases=probe.FAR)["ok"], key)
        for key in ("/run/user/1000/x", "/run/user/", "/run/user/0/bus"):
            with self.subTest(key=key):
                self.assertEqual(probe.rules_of(probe.cover(outside=[[key, ["missing"], 1]], aliases=probe.FAR)), ["C3-missing-undeclared"])
        self.assertTrue(probe.cover(outside=[["/run/userx/y", ["missing"], 1]], aliases=probe.FAR)["ok"], "/run/userx/ 不是 /run/user/（按目录判，不是字符串前缀）")

    def test_user_writable_areas_are_no_longer_noise(self) -> None:
        for root in ("/tmp", "/var/tmp", "/home", "/root", "/srv", "/mnt", "/media"):
            for kinds, rule in ((["missing"], "C3-missing-undeclared"), (["stat"], "C2-outside-undeclared"), (["dir", "stat"], "C2-outside-undeclared")):
                with self.subTest(root=root, kinds=kinds):
                    result = probe.cover(outside=[[f"{root}/x/y/", kinds, 1]], aliases=probe.FAR)
                    self.assertEqual(probe.rules_of(result), [rule])

    def test_an_alias_root_keeps_its_subtree_only_for_tree_and_work(self) -> None:
        """WP1e 时 TMP 还是别名根（采样器写下的 TMP 恒为 /tmp，于是 /tmp 之下的 missing／stat／dir 无条件放行，等于没去掉 /tmp 放行根）；
        WP1f 起别名根只取 TREE、WORK。TMP 之下的探测要靠声明的 {TMP}/ 前缀（登记表里本来就是 scratch）。"""

        for tmp in ("/tmp", "/data/ci/tmp"):
            aliases = {**probe.FAR, "TMP": tmp}
            for key in ("/tmp/x/", "{TMP}/y/", "{TMP}/q/", "/data/ci/tmp/z/"):
                with self.subTest(tmp=tmp, key=key):
                    self.assertEqual(probe.rules_of(probe.cover(outside=[[key, ["missing"], 1]], aliases=aliases)), ["C3-missing-undeclared"])
                    self.assertEqual(probe.rules_of(probe.cover(outside=[[key, ["stat"], 1]], aliases=aliases)), ["C2-outside-undeclared"])
        for key in ("{WORK}/x/", "/data/ci/run/tree/y/", "/DATA/CI/RUN/Z/"):
            with self.subTest(key=key):
                self.assertTrue(probe.cover(outside=[[key, ["missing"], 1]], aliases=probe.FAR)["ok"])
        # 登记表声明了 {TMP}/（scratch）后，TMP 之下照常通过——真实证据的 5 个范围都声明了它。
        scope = probe.bare_scope()
        scope["outside"] = [{"prefix": "{TMP}/", "class": "scratch"}]
        aliases = {**probe.FAR, "TMP": "/tmp"}
        self.assertTrue(probe.cover(outside=[["{TMP}/y/", ["missing", "stat", "read", "write"], 1]], scope=scope, aliases=aliases)["ok"])
        # TMP 自身及其祖先的 stat／dir 仍由祖先目录规则放行（与 HOME 同）。
        self.assertTrue(probe.cover(outside=[["{TMP}", ["stat", "dir"], 1], ["/", ["stat"], 1]], aliases=aliases)["ok"])
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{TMP}", ["missing"], 1]], aliases=aliases)), ["C3-missing-undeclared"])

    def test_home_subtree_probes_need_a_declaration(self) -> None:
        for key in ("{HOME}/.npmrc", "{HOME}/.config/pnpm/rc", "{HOME}/.config/", "{HOME}/.cache/x/", "/data/ci/home/.npmrc", "/DATA/CI/HOME/.NPMRC"):
            for kinds in (["missing"], ["stat"], ["dir", "stat"], ["missing", "stat"]):
                with self.subTest(key=key, kinds=kinds):
                    result = probe.cover(outside=[[key, kinds, 1]], aliases=probe.FAR)
                    self.assertFalse(result["ok"], (key, kinds, result["undeclared"]))
        # 键 {HOME}/ 与 {HOME} 是同一个目录：stat／dir 属「HOME 自身」放行，missing 不放行。
        self.assertTrue(probe.cover(outside=[["{HOME}/", ["stat", "dir"], 1]], aliases=probe.FAR)["ok"])
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{HOME}/", ["missing"], 1]], aliases=probe.FAR)), ["C3-missing-undeclared"])

    def test_home_itself_and_its_ancestors_keep_stat_and_dir(self) -> None:
        aliases = {"TREE": "/data/ci/run/tree", "WORK": "/data/ci/run", "TMP": "/data/ci/tmp", "HOME": "/data/home/user"}
        rows = [["{HOME}", ["stat", "dir"], 1], ["/data/home/", ["stat"], 1], ["/data/home", ["dir"], 1]]
        self.assertTrue(probe.cover(outside=rows, aliases=aliases)["ok"])
        self.assertEqual(probe.rules_of(probe.cover(outside=[["/data/home/", ["missing"], 1]], aliases=aliases)), ["C3-missing-undeclared"], "HOME 的祖先带 missing：不再是噪声")
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{HOME}", ["read"], 1]], aliases=aliases)), ["C2-outside-undeclared"])

    def test_a_declared_host_config_prefix_makes_the_home_probe_legal(self) -> None:
        scope = probe.bare_scope()
        scope["contracts"] = ["host-config:npmrc"]
        scope["outside"] = [{"prefix": "{HOME}/.npmrc", "class": "home-config", "contract": "host-config:npmrc"}]
        self.assertTrue(probe.cover(outside=[["{HOME}/.npmrc", ["missing"], 1]], scope=scope, aliases=probe.FAR)["ok"])
        self.assertEqual(probe.rules_of(probe.cover(outside=[["{HOME}/.yarnrc", ["missing"], 1]], scope=scope, aliases=probe.FAR)), ["C3-missing-undeclared"])

    def test_system_state_and_node_modules_segments_are_unchanged(self) -> None:
        for key in ("/usr/lib/x/", "/etc/ld.so.conf.d/", "/run/systemd/resolve/", "/var/lib/x/", "/var/cache/x/", "/var/log/x/", "/var/run/x/", "/opt/node/bin/",
                    "/lib/x86_64-linux-gnu/", "/sbin/x", "/bin/y", "/lib64/z", "/home/runner/node_modules/", "/srv/a/node_modules/b/", "/tmp/a/node_modules/b/"):
            with self.subTest(key=key):
                self.assertTrue(probe.cover(outside=[[key, ["missing"], 1]], aliases=probe.FAR)["ok"], key)


# ---------------------------------------------------------------------------
# 一般 4：outside 前缀宽度；一般 8：glob 通配符上限
# ---------------------------------------------------------------------------


class OutsidePrefixWidthTests(unittest.TestCase):
    def scope(self, *rows) -> dict:
        scope = probe.bare_scope()
        scope["outside"] = list(rows)
        return scope

    def test_scratch_prefixes_are_limited_to_the_reviewed_narrow_set(self) -> None:
        self.assertEqual(ur._DECLARED_SCRATCH_PREFIXES, ("/dev/null", "{TMP}/", "{WORK}/go-tmp/", "{HOME}/.config/go/telemetry/"))
        for prefix in ("/dev/null", "{TMP}/", "{TMP}/go-build/", "{TMP}/a/b/c/", "{WORK}/go-tmp/", "{WORK}/go-tmp/x/", "{HOME}/.config/go/telemetry/",
                       "{HOME}/.config/go/telemetry/local/"):
            with self.subTest(prefix=prefix):
                ur.validate_declared_scope(self.scope({"prefix": prefix, "class": "scratch"}))
        for prefix in ("/home/", "/root/", "/usr/", "/usr/lib/x/", "/var/", "/tmp/", "/tmp/x/", "/opt/", "/etc/", "/dev/", "/dev/null2", "/dev/nullx",
                       "{WORK}/go-build-cache/", "{WORK}/go-tmp", "{HOME}/.config/", "{HOME}/.ssh/", "{HOME}/.config/go/env"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(self.scope({"prefix": prefix, "class": "scratch"}))
            self.assertIn("class=scratch 的前缀只许", str(caught.exception))

    def test_virtual_prefixes_are_limited_to_the_reviewed_narrow_set(self) -> None:
        self.assertEqual(ur._DECLARED_VIRTUAL_PREFIXES, ("/proc/", "/sys/", "/dev/", "/run/"))
        for prefix in ("/proc/", "/sys/", "/dev/", "/run/", "/proc/self/", "/sys/devices/system/cpu/", "/dev/null", "/dev/pts/", "/run/systemd/resolve/"):
            with self.subTest(prefix=prefix):
                ur.validate_declared_scope(self.scope({"prefix": prefix, "class": "virtual"}))
        for prefix in ("/usr/lib/secret/", "/etc/", "/var/run/", "/home/", "/procx/", "/sys", "/tmp/", "{TMP}/", "{WORK}/go-tmp/", "/proc", "/runx/"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(self.scope({"prefix": prefix, "class": "virtual"}))
            self.assertIn("class=virtual 的前缀只许", str(caught.exception))

    def test_the_other_classes_are_not_limited(self) -> None:
        scope = self.scope({"prefix": "/usr/lib/", "class": "system"}, {"prefix": "/var/", "class": "system"}, {"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"},
                           {"prefix": "{WORK}/go-build-cache/", "class": "performance-cache", "contract": "cache:go-build-cache"})
        scope["contracts"] = ["toolchain:go", "cache:go-build-cache"]
        scope["repo"]["files"] = ["backend/go.mod", "backend/go.sum"]
        ur.validate_declared_scope(scope)


class GlobWildcardLimitTests(unittest.TestCase):
    def scope(self, pattern: str) -> dict:
        return probe.bare_scope(repo_ignored=[pattern])

    def test_at_most_four_wildcards_in_total(self) -> None:
        self.assertEqual(ur._DECLARED_MAX_GLOB_WILDCARDS, 4)
        for pattern in ("out/*.tmp", "out/*a*a*a*b", "out/?a?a?a?b", "out/*?*?x", "frontend/vitest.config.ts.timestamp-*", "a/b/c/d.txt"):
            with self.subTest(pattern=pattern):
                ur.validate_declared_scope(self.scope(pattern))
        for pattern in ("out/*a*a*a*a*b", "out/?????", "out/*?*?*", "out/*/*/*/*/*", "out/*a*b*c*d*e"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(self.scope(pattern))
            self.assertIn("最多 4 个", str(caught.exception))

    def test_matching_stays_fast_for_the_worst_allowed_patterns(self) -> None:
        """5 个通配符对 120 字符的路径已要约 0.2 秒、7 个要一分钟以上；上限 4 个把最坏情形压在毫秒级。"""

        path = "a" * 120
        for pattern in ("*a*a*a*b", "*a*a*a*a", "?*a*a*b", "*a?a*a*b", "a*a*a*a*"):
            with self.subTest(pattern=pattern):
                scope = self.scope(f"out/{pattern}")
                sample = base.make_sample()
                sample["repo_accesses"] = [["{TREE}/backend/a.go", ["read"], True], [f"{{TREE}}/out/{path}", ["write"], False]]
                sample["outside_accesses"], sample["git_commands"], sample["network"] = [], [], []
                scope["repo"]["ranges"] = [{"name": "backend", "include": ["backend/"], "exclude": []}]
                start = time.perf_counter()
                result = ur.declared_coverage(sample, scope)
                elapsed = time.perf_counter() - start
                self.assertLess(elapsed, 0.2, f"{pattern} 在 120 个 a 上耗时 {elapsed:.3f} 秒")
                self.assertIsInstance(result["ok"], bool)


# ---------------------------------------------------------------------------
# 一般 9：valid 必须存在且为 True
# ---------------------------------------------------------------------------


class ValidFlagTests(unittest.TestCase):
    def test_a_missing_valid_key_is_c7_invalid(self) -> None:
        sample = base.make_sample()
        self.assertIs(sample["valid"], True)
        self.assertTrue(cover(sample)["ok"])
        del sample["valid"]
        result = cover(sample)
        self.assertEqual(rules(result), {"C7-invalid"})
        self.assertIn("没有 valid 键", result["undeclared"][0]["reason"])

    def test_valid_false_and_invalid_reasons_stay_invalid(self) -> None:
        sample = base.make_sample()
        sample.update(valid=False, invalid_reasons=["[exit] x"])
        result = cover(sample)
        self.assertEqual(rules(result), {"C7-invalid"})
        self.assertTrue(result["undeclared"][0]["reason"].startswith("采样器自己判这份采样无效"))
        no_flag = base.make_sample()
        del no_flag["valid"]
        no_flag["invalid_reasons"] = ["[network] 非本机地址"]
        self.assertEqual(rules(cover(no_flag)), {"C7-invalid"})

    def test_valid_true_with_an_empty_reasons_list_and_a_review_is_fine(self) -> None:
        sample = base.make_sample()
        sample.update(invalid_reasons=[], review={"anything": ["不解释"]})
        self.assertTrue(cover(sample)["ok"])

    def test_non_boolean_values_and_contradictions_stay_format_problems(self) -> None:
        for change in ({"valid": "yes"}, {"valid": 1}, {"valid": None}, {"invalid_reasons": "oops"}, {"valid": True, "invalid_reasons": ["x"]},
                       {"valid": False, "invalid_reasons": []}):
            with self.subTest(change=change):
                self.assertEqual(rules(cover({**base.make_sample(), **change})), {"C7-format"})

    def test_the_other_c7_rules_are_independent_of_it(self) -> None:
        sample = base.make_sample()
        del sample["valid"]
        sample["coverage"]["stream_complete"] = False
        self.assertEqual(rules(cover(sample)), {"C7-invalid", "C7-stream"})


# ---------------------------------------------------------------------------
# 一般 10：文档里的「放宽项汇总」
# ---------------------------------------------------------------------------


class RelaxationSummaryTests(unittest.TestCase):
    """``declared_coverage`` 文档开头的「放宽项汇总」按条目逐项核对：条目名与顺序、每条的理由与边界、每条的关键事实、文中点名的常量真实存在且与代码一致。
    新增或删除任何放宽项都要同时改文档和这里——只查几个关键词的话，漏项照样绿（验收 #4 一般 4）。"""

    # （条目名, 该条正文里必须出现的关键事实）。条目名是文档里 ``N. *名字*（规则）：`` 的名字。
    ITEMS = (
        ("噪声探测", ("_DECLARED_PROBE_ROOTS", "_DECLARED_PROBE_EXCLUDED", "node_modules", "TREE、WORK", "用户可写区", "``{HOME}``、``{TMP}``")),
        ("祖先目录", ("read 只对 ``{WORK}/`` 这一个键放行", "stat／dir", "list 与 missing", "_DECLARED_ANCESTOR_PROBES")),
        ("root_listing", ("scope:root-listing", "树根这一层")),
        ("自产物读回", ("repo_ignored", "同一路径也出现", "C1-untracked-read")),
        ("installed_dirs", ("frontend/node_modules/", "cache:node-modules", "repo_ignored")),
        ("performance-cache 可执行", ("GOCACHE", "威胁模型", "go run")),
        ("/dev/null 可写", ("精确路径 ``/dev/null``",)),
        ("C6-head 的精度放宽", ("``:`` 之后", "``--`` 之后", "show、log、rev-list、ls-tree、diff-tree", "merge-base、cat-file、rev-parse")),
        ("有固定的 git 子进程时放行状态读取", ("git_explained", "折中", "C1-git-state", "strace 分不清")),
        (".git 非状态文件", ("objects/", "config", "description", "info/exclude", "info/attributes", "默认拒绝", "worktrees/")),
        ("notes 的已知残余", ("--no-notes", "refs/notes/commits", "残余风险")),
    )

    def summary_items(self) -> list[tuple[int, str, str]]:
        import re

        doc = inspect.cleandoc(ur.declared_coverage.__doc__ or "")  # Python 3.13 起编译器已去掉文档字符串的公共缩进，3.12 还没有：统一处理
        block = doc[doc.index("放宽项汇总"):doc.index("仓库外路径的键沿用采样器的约定")]
        heads = list(re.finditer(r"^\s*(\d+)\. \*(.+?)\*（", block, re.M))
        found = []
        for index, head in enumerate(heads):
            body = block[head.end():heads[index + 1].start() if index + 1 < len(heads) else len(block)]
            found.append((int(head.group(1)), head.group(2), body))
        return found

    def test_the_summary_lists_exactly_the_expected_relaxations_in_order(self) -> None:
        found = self.summary_items()
        self.assertEqual([(number, name) for number, name, _body in found], [(index + 1, name) for index, (name, _facts) in enumerate(self.ITEMS)],
                         "条目名与顺序必须和代码里的放宽项一一对应：新增放宽项要先加到文档和这个表")

    def test_every_item_states_what_why_and_where_it_stops(self) -> None:
        for number, name, body in self.summary_items():
            with self.subTest(item=f"{number}. {name}"):
                self.assertIn("理由", body, "每条写理由")
                self.assertIn("边界", body, "每条写边界")
                self.assertLess(body.index("理由"), body.index("边界"), "先理由后边界")
                self.assertGreater(len(body.strip()), 80, "一句话不够：要写清做什么、理由、边界")

    def test_every_item_carries_its_key_facts(self) -> None:
        facts = dict(self.ITEMS)
        for _number, name, body in self.summary_items():
            for fact in facts[name]:
                with self.subTest(item=name, fact=fact):
                    self.assertIn(fact, body)

    def test_identifiers_named_in_the_summary_exist_and_match_the_code(self) -> None:
        import re

        doc = ur.declared_coverage.__doc__ or ""
        for identifier in sorted(set(re.findall(r"``(_?[Dd][Ee][Cc][Ll][Aa][Rr][Ee][Dd]_[A-Za-z_]+)``", doc))):
            with self.subTest(identifier=identifier):
                self.assertTrue(hasattr(ur, identifier), f"文档点名的 {identifier} 在 unit_records 里不存在（改名后文档没跟上）")
        items = {name: body for _number, name, body in self.summary_items()}
        noise = items["噪声探测"]
        for root in (*ur._DECLARED_PROBE_ROOTS, *ur._DECLARED_PROBE_EXCLUDED):
            self.assertIn(f"``{root}``", noise, f"噪声探测一条没列出放行根／排除根 {root}")
        for area in ("/tmp", "/var/tmp", "/run/user", "/home", "/root", "/srv", "/mnt", "/media"):
            self.assertIn(f"``{area}``", noise, f"用户可写区 {area} 要写进边界")
            candidate = area + "/x/"
            covered = (any(candidate.startswith(root) for root in ur._DECLARED_PROBE_ROOTS)
                       and not any(candidate.startswith(excluded) for excluded in ur._DECLARED_PROBE_EXCLUDED))
            self.assertFalse(covered, f"文档说 {area} 不是噪声，代码里它却被放行根盖住了：{ur._DECLARED_PROBE_ROOTS}")
        self.assertNotIn("TREE、WORK、TMP", noise, "别名根不含 TMP")
        self.assertNotIn("TREE／WORK／TMP", noise)
        for name in ur._DECLARED_GIT_HARMLESS_FILES:
            self.assertIn(name, items[".git 非状态文件"], name)

    def test_the_misleading_old_phrases_are_gone(self) -> None:
        doc = ur.declared_coverage.__doc__ or ""
        for phrase in ("不含它们的后代", "不得引用 HEAD 一类浮动引用", "目录自身被枚举、被打开", "TREE／WORK／TMP"):
            self.assertNotIn(phrase, doc, phrase)
        self.assertIn("白名单", doc)
        source = Path(ur.__file__).read_text(encoding="utf-8")
        self.assertNotIn("目录自身被打开", source)
        self.assertNotIn("对 120 字符的路径已要约 0.2 秒", source, "glob 常量注释要写真话：匹配已改成线性")


if __name__ == "__main__":
    unittest.main()
