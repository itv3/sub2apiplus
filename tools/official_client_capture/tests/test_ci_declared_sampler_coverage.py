"""读集采样器（``tools/ci/declared_sampler.py``）的覆盖闭合与覆盖自检：步骤 5 的 WP4c 之 T2～T5。

真实数据（ARM64 上采的 egress scanner-check 与前端 critical／lint／typecheck）让 ``draft`` 起草的 scope 过不了登记侧的覆盖检查
（``unit_records.declared_coverage``）。这里把每一条补救都钉住，正反例都用合成夹具：

* T2：仓库内的 missing 探测、只被 stat／dir 且其下没有声明内容的目录，补最近的非树根父目录（目录自身）的 range；
* T3：树根被枚举／读取，或树根直接子项有 missing 探测，写 ``repo.root_listing: true``；``go.work`` 一类祖先探测名不触发；
* T4：``repo_ignored`` 不得盖住依赖安装目录；被写入的 ``/dev/null`` 补 scratch 精确行；``/run/`` 归 virtual；
* T5：起草完用登记侧的 ``declared_coverage`` 自检，不通过写 rerun-only（原因带规则统计和前 3 处），评审清单第 13 节列全部未覆盖项；
* 祖先链目录上的 ``read`` 要区分“打开目录”与“读文件”：别名根与祖先目录的 ``read`` 记成 ``dir``。

WP4d：登记侧（WP1e 起）要求声明了 cache:node-modules 就单独列出 frontend/pnpm-lock.yaml，``draft`` 补入它，前端式草案才过自检（``ClosureDraftTests``）。

与登记侧的互操作都是直接断言（``repo.root_listing`` 已随 WP1c 进入登记侧）。旧用例见 ``test_ci_declared_sampler.py`` 与
``test_ci_declared_sampler_followup.py``：它们钉的是按读集机械归并本身，所以调用 ``draft`` 时用 ``coverage_check=False`` 关掉自检。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import declared_sampler as ds
from tools.ci import unit_records as ur
from tools.official_client_capture.tests.test_ci_declared_sampler import (
    HOME, TMP, UNIT, aliases_for, feed_all, good_lines, list_line, make_raw, make_repo, missing_line, open_line, stat_line, write_line)
from tools.official_client_capture.tests.test_ci_declared_sampler_followup import FOLLOWUP_FILES, DraftBase, NODE_CONTRACT, NODE_MODULES

# 合成仓库：在 FOLLOWUP_FILES 之上补两份法律文档（docs/legal 有跟踪文件）、src/lib/util.ts（src/__tests__ 则没有任何跟踪文件）；
# .gitignore 另外忽略前端的 node_modules、eslint 缓存和树根下的一个缓存文件。
COVERAGE_FILES = {
    **FOLLOWUP_FILES,
    ".gitignore": "out/\nignored-deps/\nfrontend/node_modules/\nfrontend/.eslintcache\n/.root-cache\n",
    "docs/legal/terms.md": "t\n",
    "docs/legal/privacy.md": "p\n",
    "src/lib/util.ts": "export {}\n",
}


def rules_of(report: dict) -> set[str]:
    return {item["rule"] for item in report["undeclared"]}


# ---------------------------------------------------------------------------
# range 名字
# ---------------------------------------------------------------------------


class RangeNameTests(unittest.TestCase):
    def test_conforming_directories_keep_exactly_the_old_names(self) -> None:
        for directory in ("backend", "docs/egress/foundation", "frontend/src/__tests__", "a.b/c-d", "x9"):
            self.assertEqual(ds.range_name(directory), directory.replace("/", "-"), "已经合规的目录，结果与从前逐字相同")

    def test_unconventional_characters_are_sanitized_the_way_the_record_layer_demands(self) -> None:
        cases = {"Docs/Legal": "docs-legal", ".github/workflows": "github-workflows", "@scope/pkg": "scope-pkg", "node_modules/node:fs": "node_modules-node-fs",
                 "a b/c": "a-b-c", "_x": "x", "---": "dir", ".": "dir", "中文/目录": "dir", "UPPER": "upper"}
        for directory, expected in cases.items():
            with self.subTest(directory):
                self.assertEqual(ds.range_name(directory), expected)

    def test_names_are_unique_and_never_the_reserved_root_listing_name(self) -> None:
        self.assertEqual(ds.name_ranges(["a-b", "a/b", "a_b"]), ["a-b", "a-b-2", "a_b"])
        self.assertEqual(ds.name_ranges(["Docs", "docs"]), ["docs", "docs-2"], "规范化之后重名也要错开")
        self.assertEqual(ds.name_ranges(["root/listing"]), ["root-listing-2"], "root-listing 是 repo.root_listing 的输入条目名，range 不能叫它")
        self.assertEqual(ds.name_ranges([]), [])

    def test_every_generated_name_is_accepted_by_the_record_layer(self) -> None:
        directories = ["Docs/Legal", ".github/workflows", "@scope/pkg", "node_modules/node:fs", "a b/c", "中文", "root/listing", "x/y", "x-y"]
        names = ds.name_ranges(directories)
        self.assertEqual(len(set(names)), len(names))
        scope = {"schema_version": ur.DECLARED_SCOPE_SCHEMA,
                 "repo": {"ranges": [{"name": name, "include": [directory + "/"], "exclude": []} for name, directory in zip(names, directories)]}}
        ur.validate_declared_scope(scope)  # 不抛异常；直接用目录名当 range 名的旧写法，对这些目录会被登记侧拒绝


# ---------------------------------------------------------------------------
# 覆盖闭合（纯函数）
# ---------------------------------------------------------------------------


def closure(accesses, *, tracked=(), include_dirs=(), declared_files=(), ignored_items=(), installed_prefixes=()) -> dict:
    return ds.close_repo_scope(accesses, tracked=set(tracked), include_dirs=list(include_dirs), declared_files=list(declared_files),
                               ignored_items=list(ignored_items), installed_prefixes=list(installed_prefixes))


class CloseRepoScopeTests(unittest.TestCase):
    def test_a_missing_probe_gets_its_nearest_non_root_parent_directory(self) -> None:
        result = closure([["{TREE}/docs/legal/x.md?raw.ts", ["missing"], False], ["{TREE}/docs/legal", ["stat"], True]], tracked={"docs/legal/terms.md"})
        self.assertEqual(result["extra"], ["docs/legal"], "目录自身的 stat 也被这个 range 盖住，不再单独补")
        self.assertEqual(result["why"]["docs/legal"], {"missing": ["docs/legal/x.md?raw.ts"], "stat": ["docs/legal"]})
        self.assertFalse(result["root_listing"])

    def test_shallow_directories_come_first_so_they_absorb_deeper_probes(self) -> None:
        result = closure([["{TREE}/zz/y/x", ["missing"], False], ["{TREE}/zz/y/c/z", ["missing"], False], ["{TREE}/zz/y/c/d/w", ["missing"], False]])
        self.assertEqual(result["extra"], ["zz/y"])
        self.assertEqual(result["why"]["zz/y"]["missing"], ["zz/y/c/d/w", "zz/y/c/z", "zz/y/x"], "深处的探测归到盖住它的那个 range 名下")

    def test_probes_directly_under_the_tree_root_become_root_listing_not_a_range(self) -> None:
        result = closure([["{TREE}/package.json", ["missing"], False], ["{TREE}/pnpm-workspace.yaml", ["missing"], False]])
        self.assertEqual((result["extra"], result["root_listing"], result["root_missing"], result["root_listed"]),
                         ([], True, ["package.json", "pnpm-workspace.yaml"], False))

    def test_ancestor_probe_names_at_the_root_trigger_nothing(self) -> None:
        names = sorted(ds.ANCESTOR_PROBES)
        result = closure([[f"{{TREE}}/{name}", ["missing"], False] for name in names])
        self.assertEqual((result["extra"], result["root_listing"], result["root_missing"]), ([], False, []), "登记侧本来就放行，不补也不触发 root_listing")
        mixed = closure([["{TREE}/go.work", ["missing"], False], ["{TREE}/RCS", ["missing"], False]])
        self.assertEqual((mixed["root_listing"], mixed["root_missing"]), (True, ["RCS"]), "同时有别的树根探测时，只列需要绑定的那个")
        # 同名但不在树根的（backend/go.work）不是祖先探测，要被 range 覆盖。
        nested = closure([["{TREE}/backend/go.work", ["missing"], False]])
        self.assertEqual(nested["extra"], ["backend"])

    def test_the_tree_root_itself_listed_or_read_needs_root_listing(self) -> None:
        for kinds, expected in ((["dir", "list", "stat"], True), (["read"], True), (["list"], True), (["dir", "read", "stat"], True), (["stat"], False),
                                (["dir", "stat"], False), (["list", "write"], False), (["read", "exec"], False)):
            with self.subTest(kinds=kinds):
                result = closure([["{TREE}", kinds, True]])
                self.assertEqual(result["root_listing"], expected)
                self.assertEqual(result["root_listed"], expected)

    def test_root_children_with_mixed_kinds_are_not_covered_by_root_listing(self) -> None:
        result = closure([["{TREE}/maybe", ["missing", "stat"], False]])
        self.assertEqual((result["extra"], result["root_listing"]), ([], False), "登记侧只放行纯 missing 的树根直接子项，混合类型留给自检报告")

    def test_stat_only_directories_get_a_range_for_the_directory_itself_deepest_first(self) -> None:
        alone = closure([["{TREE}/docs/legal", ["stat"], True]], tracked={"docs/legal/terms.md"})
        self.assertEqual(alone["extra"], ["docs/legal"])
        self.assertEqual(alone["why"]["docs/legal"], {"missing": [], "stat": ["docs/legal"]})
        # 浅的先补会补出很宽的 a；深的先补，a 就成了已声明内容的上级目录，不必再补。
        nested = closure([["{TREE}/a", ["stat"], True], ["{TREE}/a/b", ["dir", "stat"], True]], tracked={"a/b/f.txt"})
        self.assertEqual(nested["extra"], ["a/b"])

    def test_stat_only_directories_above_declared_content_need_nothing(self) -> None:
        accesses = [["{TREE}/frontend", ["stat"], True], ["{TREE}/frontend/src", ["dir"], True], ["{TREE}/lib", ["stat"], True]]
        result = closure(accesses, include_dirs=["frontend/src/deep"], declared_files=["lib/x.txt"], tracked={"frontend/src/deep/a", "lib/x.txt"})
        self.assertEqual(result["extra"], [], "range 或单文件在它下面的目录，登记侧只看元数据就放行")
        by_ignored = closure([["{TREE}/frontend", ["stat"], True]], ignored_items=["frontend/.cache/"])
        self.assertEqual(by_ignored["extra"], [], "输出区目录前缀的上级目录同理")
        by_glob = closure([["{TREE}/frontend", ["stat"], True]], ignored_items=["frontend/vite.timestamp-*"])
        self.assertEqual(by_glob["extra"], [], "glob 的字面目录部分的上级同理")
        by_installed = closure([["{TREE}/frontend", ["stat"], True]], installed_prefixes=["frontend/node_modules/"])
        self.assertEqual(by_installed["extra"], [])

    def test_a_stat_only_tracked_file_is_left_for_the_self_check_to_report(self) -> None:
        result = closure([["{TREE}/docs/other.md", ["stat"], True]], tracked={"docs/other.md"})
        self.assertEqual((result["extra"], result["root_listing"]), ([], False), "单文件条目是“读过”的语义：被 stat 的已跟踪文件不凭空声明")

    def test_covered_and_exempt_paths_add_nothing(self) -> None:
        accesses = [["{TREE}/backend/zzz.go", ["missing"], False], ["{TREE}/backend", ["stat"], True],                          # 已被 range 覆盖（含目录自身）
                    ["{TREE}/frontend/node_modules/@types/x", ["missing"], False], ["{TREE}/frontend/node_modules", ["stat"], False],  # 安装目录前缀内（含目录自身）
                    ["{TREE}/out/none.txt", ["missing"], False], ["{TREE}/frontend/vite.timestamp-1", ["missing"], False],         # 输出区的目录前缀与 glob
                    ["{TREE}/.git/refs/foo", ["missing"], False], ["{TREE}/.git", ["stat"], False], ["{TREE}/.git/objects/", ["read"], False],
                    ["/elsewhere/x", ["missing"], False], ["{TREE}/backend/a.go", ["read"], True]]                            # 不是树内路径、读取类
        result = closure(accesses, include_dirs=["backend"], ignored_items=["out/", "frontend/vite.timestamp-*"],
                         installed_prefixes=["frontend/node_modules/"])
        self.assertEqual((result["extra"], result["root_listing"]), ([], False))
        # 单文件条目盖住的路径同理（同一个文件在冷热两次运行里一次读到、一次探测不到，也只是被单文件条目盖住）。
        by_file = closure([["{TREE}/lib/x.txt", ["missing"], False]], declared_files=["lib/x.txt"])
        self.assertEqual(by_file["extra"], [])
        # range 的目录自身也属于这个 range（登记侧同口径：include 是目录前缀时，目录本身属于声明范围）。
        own = closure([["{TREE}/backend", ["missing"], False]], include_dirs=["backend"])
        self.assertEqual((own["extra"], own["root_listing"]), ([], False))
        # 读、写、枚举、执行这些各有归并或判无效规则的访问类型，不归覆盖闭合管：未被覆盖的未跟踪读取也不会被闭合成 range。
        other_kinds = closure([["{TREE}/lib/untracked-read.txt", ["read"], False], ["{TREE}/lib/listed", ["list"], False],
                               ["{TREE}/lib/exec.sh", ["exec"], True], ["{TREE}/lib/written", ["write"], False]])
        self.assertEqual((other_kinds["extra"], other_kinds["root_listing"]), ([], False))

    def test_the_tree_root_node_modules_is_not_the_installed_dir(self) -> None:
        accesses = [["{TREE}/node_modules/@types", ["missing"], False], ["{TREE}/node_modules/jsdom/package.json", ["missing"], False],
                    ["{TREE}/node_modules/node:fs/package.json", ["missing"], False]]
        result = closure(accesses, installed_prefixes=["frontend/node_modules/"])
        self.assertEqual(result["extra"], ["node_modules"], "树根下的 node_modules 要补 range，它不是 frontend/node_modules/ 这个安装目录前缀")
        self.assertEqual(len(result["why"]["node_modules"]["missing"]), 3)

    def test_results_are_sorted_and_independent_of_input_order(self) -> None:
        accesses = [["{TREE}/zeta/x", ["missing"], False], ["{TREE}/alpha/y", ["missing"], False], ["{TREE}/mid", ["stat"], True]]
        forward, backward = closure(accesses), closure(list(reversed(accesses)))
        self.assertEqual(forward["extra"], ["alpha", "mid", "zeta"])
        self.assertEqual(forward, backward)

    def test_an_unusable_ignored_item_does_not_crash_the_closure(self) -> None:
        result = closure([["{TREE}/a/x", ["missing"], False]], ignored_items=["bad[name]/", "ok/"])
        self.assertEqual(result["extra"], ["a"], "含方括号的写法登记侧也会拒绝，这里不抛异常，交给覆盖自检去报")


# ---------------------------------------------------------------------------
# repo_ignored 与依赖安装目录
# ---------------------------------------------------------------------------


class BuildRepoIgnoredTests(unittest.TestCase):
    INSTALLED = ["frontend/node_modules/"]

    def build(self, ignored, written=None, transient=(), installed=()):
        return ds.build_repo_ignored(ignored, ignored if written is None else written, transient, installed)

    def test_without_installed_dirs_the_result_is_the_old_one(self) -> None:
        items, exact = self.build(["out/result.bin", "frontend/.eslintcache"], transient=["frontend/.eslintcache", "frontend/vite.timestamp-*"])
        self.assertEqual(items, ["frontend/", "frontend/.eslintcache", "frontend/vite.timestamp-*", "out/"], "目录前缀 + 暂存模式，去重排序，与从前一致")
        self.assertEqual(exact, [])

    def test_a_file_written_directly_inside_the_installed_dir_uses_its_exact_path(self) -> None:
        items, exact = self.build(["frontend/node_modules/.modules.yaml"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/node_modules/.modules.yaml"], "绝不能把 frontend/node_modules/ 整个放进 repo_ignored")
        self.assertEqual(exact, [("frontend/node_modules/", "frontend/node_modules/.modules.yaml")])

    def test_a_file_written_in_a_parent_of_the_installed_dir_uses_its_exact_path(self) -> None:
        items, exact = self.build(["frontend/.eslintcache"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/.eslintcache"], "目录前缀 frontend/ 会盖住安装目录（也盖住 range 里已跟踪的输入）")
        self.assertEqual(exact, [("frontend/", "frontend/.eslintcache")])

    def test_a_root_level_file_has_no_directory_prefix_and_uses_its_exact_path(self) -> None:
        items, exact = self.build([".root-cache"], installed=self.INSTALLED)
        self.assertEqual((items, exact), ([".root-cache"], [("(树根)", ".root-cache")]))
        # 没有安装目录时树根下的文件同样用精确路径：目录前缀无从谈起（从前这类写入没有任何条目）。
        self.assertEqual(self.build([".root-cache"])[0], [".root-cache"])

    def test_directories_strictly_inside_the_installed_dir_keep_their_directory_prefix(self) -> None:
        items, exact = self.build(["frontend/node_modules/.vite/vitest/results.json", "frontend/node_modules/.vue-global-types/index.d.ts"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/node_modules/.vite/vitest/", "frontend/node_modules/.vue-global-types/"], "登记侧允许的“安装目录里的输出区”")
        self.assertEqual(exact, [])

    def test_a_transient_directory_pattern_that_covers_the_installed_dir_is_replaced_by_the_written_paths(self) -> None:
        written = ["frontend/a.tmp", "frontend/sub/b.tmp", "frontend/node_modules/.cache/c.tmp", "other/d.tmp"]
        items, exact = self.build([], written=written, transient=["frontend/", "other/"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/a.tmp", "frontend/node_modules/.cache/c.tmp", "frontend/sub/b.tmp", "other/"],
                         "盖住安装目录的暂存目录模式改成命中它的被写路径；不盖住的照旧")
        self.assertEqual(len(exact), 3)

    def test_an_exact_path_already_covered_by_a_directory_prefix_is_dropped(self) -> None:
        # .vite 自身（父目录是安装目录，改用精确路径）和 .vite 下的文件（父目录 .vite/ 严格位于安装目录之内，用目录前缀）：目录前缀连目录
        # 自身也盖得住，.vite 的精确路径就多余了，连同记录一起去掉。
        items, exact = self.build(["frontend/node_modules/.vite", "frontend/node_modules/.vite/a.js"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/node_modules/.vite/"])
        self.assertEqual(exact, [])
        # 没有被任何目录前缀盖住的精确路径保留，记录也保留。
        items, exact = self.build(["frontend/node_modules/.vite", "frontend/node_modules/.other/a.js"], installed=self.INSTALLED)
        self.assertEqual(items, ["frontend/node_modules/.other/", "frontend/node_modules/.vite"])
        self.assertEqual(exact, [("frontend/node_modules/", "frontend/node_modules/.vite")])


# ---------------------------------------------------------------------------
# draft：端到端（合成仓库）
# ---------------------------------------------------------------------------


class CoverageBase(DraftBase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.tree = make_repo(cls.root, COVERAGE_FILES)
        cls.work = cls.root / "work"
        cls.work.mkdir()

    def base_lines(self) -> list[str]:
        t = str(self.tree)
        return [open_line(f"{t}/backend", f"{t}/backend/go.mod"), open_line(f"{t}/backend", f"{t}/backend/go.sum"),
                open_line(f"{t}/backend", f"{t}/backend/a.go")]

    def sample(self, extra=(), **merge_options) -> dict:
        sample = self.merge(self.raw(self.base_lines() + list(extra)), **merge_options)
        self.assertTrue(sample["valid"], sample["invalid_reasons"])
        return sample

    def unit(self, sample: dict, **options) -> tuple[dict, str]:
        registry, review = self.draft(sample, **options)
        return registry["units"][UNIT], review

    def written(self) -> dict:
        return ds.load_document(self.samples / "backend-go-test.sample.json")

    def coverage_of(self, scope: dict) -> dict:
        return ur.declared_coverage(self.written(), scope)


class ClosureDraftTests(CoverageBase):
    def test_missing_probes_and_stat_only_directories_make_the_draft_registrable(self) -> None:
        t = str(self.tree)
        sample = self.sample([missing_line(t, f"{t}/docs/legal/terms.md?raw.ts"), missing_line(t, f"{t}/src/__tests__/setup.ts"),
                              missing_line(t, f"{t}/src/__tests__/setup.ts.js"), stat_line(t, f"{t}/docs/legal")])
        entry, review = self.unit(sample)
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        repo = entry["scope"]["repo"]
        self.assertEqual(sorted(item["include"][0] for item in repo["ranges"]), ["backend/", "docs/legal/", "src/__tests__/"])
        self.assertEqual(sorted(item["name"] for item in repo["ranges"]), ["backend", "docs-legal", "src-__tests__"])
        self.assertTrue(all(item["exclude"] == [] for item in repo["ranges"]))
        self.assertNotIn("root_listing", repo, "树根以下的探测不触发 root_listing")
        self.assertIn("**覆盖闭合补入的 range** 2 个", review)
        self.assertIn("`docs/legal/`（跟踪文件 2 个）：missing 探测 1 个，例如 `docs/legal/terms.md?raw.ts`；目录自身被 stat／dir，其下没有声明内容", review)
        self.assertIn("`src/__tests__/`（跟踪文件 0 个）：missing 探测 2 个", review, "目录不存在或没有跟踪文件也照补")
        self.assertIn("**覆盖自检通过**", review)
        # 每个补入的 range 都不可缺：拿掉它，登记侧的覆盖检查立刻报未覆盖。
        for directory, rules in (("docs/legal/", {"C3-missing-undeclared", "C1-undeclared"}), ("src/__tests__/", {"C3-missing-undeclared"})):
            with self.subTest(directory):
                scope = copy.deepcopy(entry["scope"])
                scope["repo"]["ranges"] = [item for item in scope["repo"]["ranges"] if item["include"] != [directory]]
                report = self.coverage_of(scope)
                self.assertFalse(report["ok"])
                self.assertEqual(rules_of(report), rules)
        # 关掉覆盖闭合就是从前的草案：没有这些 range（登记侧会拒绝）。
        raw_entry, raw_review = self.unit(sample, coverage_check=False)
        self.assertEqual([item["include"][0] for item in raw_entry["scope"]["repo"]["ranges"]], ["backend/"])
        self.assertNotIn("覆盖闭合补入", raw_review)

    def test_the_draft_with_closure_is_exactly_the_old_draft_plus_the_extra_ranges(self) -> None:
        t = str(self.tree)
        sample = self.sample([missing_line(t, f"{t}/src/__tests__/setup.ts")])
        with_closure, _ = self.unit(sample)
        without, _ = self.unit(sample, coverage_check=False)
        old_scope, new_scope = without["scope"], with_closure["scope"]
        self.assertEqual([item["include"][0] for item in new_scope["repo"]["ranges"]], ["backend/", "src/__tests__/"])
        new_scope = copy.deepcopy(new_scope)
        new_scope["repo"]["ranges"] = [item for item in new_scope["repo"]["ranges"] if item["include"] != ["src/__tests__/"]]
        self.assertEqual(new_scope, old_scope, "补入 range 之外，scope 的其余部分逐字不变")

    def test_covered_probes_add_nothing(self) -> None:
        t = str(self.tree)
        extra = self.frontend_reads() + [missing_line(t, f"{t}/backend/zzz.go"), missing_line(t, f"{t}/frontend/node_modules/@types/x"),
                                         write_line(t, f"{t}/out/result.bin"), missing_line(t, f"{t}/out/none.txt")]
        sample = self.sample(extra, installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        with_closure, _ = self.unit(sample)
        without, _ = self.unit(sample, coverage_check=False)
        self.assertEqual(with_closure["status"], "declared", with_closure.get("reason"))
        self.assertEqual(with_closure["scope"]["repo"]["ranges"], without["scope"]["repo"]["ranges"], "已被 range、安装目录、输出区覆盖的探测不补任何 range")

    def frontend_reads(self) -> list[str]:
        t, cwd = str(self.tree), f"{self.tree}/frontend"
        reads = [open_line(cwd, f"{t}/frontend/package.json"), open_line(cwd, f"{t}/frontend/src/a.ts"), open_line(cwd, f"{t}/frontend/src/b.ts")]
        reads += [open_line(cwd, f"{t}/frontend/node_modules/.pnpm/p{index}/node_modules/p{index}/index.js") for index in range(3)]
        return reads + [list_line(f"{t}/frontend/node_modules"), stat_line(cwd, f"{t}/frontend/node_modules/.bin")]

    def test_the_tree_root_node_modules_gets_a_range_and_stays_apart_from_the_installed_dir(self) -> None:
        t = str(self.tree)
        extra = self.frontend_reads() + [missing_line(t, f"{t}/node_modules/@types"), missing_line(t, f"{t}/node_modules/jsdom/package.json"),
                                         missing_line(t, f"{t}/node_modules/node:fs/package.json")]
        entry, _ = self.unit(self.sample(extra, installed_dirs=[(NODE_MODULES, NODE_CONTRACT)]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        repo = entry["scope"]["repo"]
        self.assertEqual([item for item in repo["ranges"] if item["include"][0].startswith("node_modules")],
                         [{"name": "node_modules", "include": ["node_modules/"], "exclude": []}])
        self.assertEqual(repo["installed_dirs"], [{"prefix": NODE_MODULES, "contract": NODE_CONTRACT}])
        self.assertFalse([item for item in repo["ranges"] if item["include"][0].startswith(NODE_MODULES)], "安装目录前缀之下不能有 range")
        ur.validate_declared_scope(entry["scope"])

    def test_a_stat_only_directory_with_nothing_declared_below_gets_a_range_of_its_own(self) -> None:
        t = str(self.tree)
        entry, review = self.unit(self.sample([stat_line(t, f"{t}/docs/legal")]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        self.assertEqual(sorted(item["include"][0] for item in entry["scope"]["repo"]["ranges"]), ["backend/", "docs/legal/"])
        self.assertIn("目录自身被 stat／dir，其下没有声明内容", review)
        # 上级目录：frontend 下面有声明内容（读过的文件），单独 stat 它不需要 range。
        above, _ = self.unit(self.sample(self.frontend_reads() + [stat_line(t, f"{t}/frontend")], installed_dirs=[(NODE_MODULES, NODE_CONTRACT)]))
        self.assertEqual(above["status"], "declared", above.get("reason"))
        self.assertFalse([item for item in above["scope"]["repo"]["ranges"] if item["include"][0] == "frontend/"])

    def test_the_cache_contract_lock_file_is_what_makes_a_frontend_draft_registrable(self) -> None:
        """WP4d：登记侧（WP1e 起）要求声明了 cache:node-modules 就单独列出 frontend/pnpm-lock.yaml。draft 补入它，覆盖自检才通过；拿掉它登记侧报 C0-scope。"""
        entry, review = self.unit(self.sample(self.frontend_reads(), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        self.assertIn("frontend/pnpm-lock.yaml", entry["scope"]["repo"]["files"])
        self.assertIn("`frontend/pnpm-lock.yaml`（由合同要求补入）", review)
        self.assertIn("**覆盖自检通过**", review)
        scope = copy.deepcopy(entry["scope"])
        scope["repo"]["files"].remove("frontend/pnpm-lock.yaml")
        report = self.coverage_of(scope)
        self.assertFalse(report["ok"])
        self.assertEqual(rules_of(report), {"C0-scope"})
        self.assertIn("cache:node-modules", report["undeclared"][0]["reason"])
        # 对照：没有安装目录（没有 cache:node-modules 合同）就不补，也不需要。
        plain, _ = self.unit(self.sample())
        self.assertNotIn("frontend/pnpm-lock.yaml", plain["scope"]["repo"]["files"])

    def test_a_stat_only_tracked_file_makes_the_self_check_fail_visibly(self) -> None:
        t = str(self.tree)
        entry, review = self.unit(self.sample([stat_line(t, f"{t}/docs/other.md")]))
        self.assertEqual(entry["status"], "rerun-only")
        self.assertTrue(entry["reason"].startswith(ds.COVERAGE_REASON_PREFIX))
        self.assertIn("C1-undeclared {TREE}/docs/other.md [stat]", entry["reason"], "已知缺口：只被 stat 的已跟踪文件不凭空声明，由自检如实报出")
        self.assertIn("`{TREE}/docs/other.md` stat", review)


class RootListingDraftTests(CoverageBase):
    def test_root_listing_is_written_when_the_tree_root_is_listed_or_read(self) -> None:
        t = str(self.tree)
        for name, line in (("list", list_line(t)), ("read", open_line(t, t))):
            with self.subTest(name):
                entry, review = self.unit(self.sample([line]))
                self.assertEqual(entry["status"], "declared", entry.get("reason"))
                self.assertIs(entry["scope"]["repo"]["root_listing"], True)
                ur.validate_declared_scope(entry["scope"])  # 登记侧认识这个键（WP1c）
                self.assertIn("**`repo.root_listing: true`**：测试树根目录被枚举或读取", review)
                # 没有它，树根枚举/读取在登记侧是未覆盖的。
                scope = copy.deepcopy(entry["scope"])
                del scope["repo"]["root_listing"]
                report = self.coverage_of(scope)
                self.assertFalse(report["ok"])
                self.assertEqual(rules_of(report), {"C1-undeclared"})

    def test_root_listing_is_written_for_missing_probes_of_root_children(self) -> None:
        t = str(self.tree)
        entry, review = self.unit(self.sample([missing_line(t, f"{t}/package.json"), missing_line(t, f"{t}/pnpm-workspace.yaml")]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        self.assertIs(entry["scope"]["repo"]["root_listing"], True)
        self.assertEqual([item["include"][0] for item in entry["scope"]["repo"]["ranges"]], ["backend/"], "树根直接子项不补 range")
        self.assertIn("树根直接子项有 2 个 missing 探测（`package.json`、`pnpm-workspace.yaml`）", review)
        scope = copy.deepcopy(entry["scope"])
        del scope["repo"]["root_listing"]
        report = self.coverage_of(scope)
        self.assertEqual(rules_of(report), {"C3-missing-undeclared"})

    def test_nothing_is_written_when_the_root_is_only_stat_ed_or_only_ancestor_names_are_probed(self) -> None:
        t = str(self.tree)
        for name, extra in (("none", []), ("root stat", [stat_line(t, t)]),
                            ("ancestor probes", [missing_line(t, f"{t}/go.work"), missing_line(t, f"{t}/go.mod"), missing_line(t, f"{t}/.hg")])):
            with self.subTest(name):
                entry, review = self.unit(self.sample(extra))
                self.assertEqual(entry["status"], "declared", entry.get("reason"))
                self.assertNotIn("root_listing", entry["scope"]["repo"], "默认行为不变：不需要时不写这个键")
                self.assertNotIn("repo.root_listing: true", review)
        # 祖先探测名之外只要有一个别的树根探测，就要写。
        entry, _ = self.unit(self.sample([missing_line(t, f"{t}/go.work"), missing_line(t, f"{t}/RCS")]))
        self.assertIs(entry["scope"]["repo"]["root_listing"], True)

    def test_a_frontend_shaped_sample_gets_everything_at_once(self) -> None:
        t = str(self.tree)
        extra = (self.frontend_like() + [list_line(t), missing_line(t, f"{t}/eslint.config.cjs"), missing_line(t, f"{t}/node_modules"),
                                         missing_line(t, f"{t}/src/__tests__/setup.ts"), missing_line(t, f"{t}/node_modules/@types"),
                                         stat_line(t, f"{t}/docs/legal"), missing_line(t, f"{t}/docs/legal/terms.md?raw.ts")])
        entry, review = self.unit(self.sample(extra, installed_dirs=[(NODE_MODULES, NODE_CONTRACT)]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        repo = entry["scope"]["repo"]
        self.assertIs(repo["root_listing"], True)
        self.assertEqual(sorted(item["include"][0] for item in repo["ranges"] if not item["include"][0].startswith(("backend/", "frontend/"))),
                         ["docs/legal/", "node_modules/", "src/__tests__/"])
        includes = [item["include"][0] for item in repo["ranges"]]
        self.assertEqual(includes, sorted(includes), "登记表要确定性：读集归并出的 range 与补入的 range 合在一起按路径排序")
        self.assertEqual(len({item["name"] for item in repo["ranges"]}), len(includes))
        self.assertIn("**覆盖自检通过**", review)

    def frontend_like(self) -> list[str]:
        t, cwd = str(self.tree), f"{self.tree}/frontend"
        reads = [open_line(cwd, f"{t}/frontend/package.json"), open_line(cwd, f"{t}/frontend/src/a.ts")]
        return reads + [open_line(cwd, f"{t}/frontend/node_modules/.pnpm/p/node_modules/p/index.js"), list_line(f"{t}/frontend/node_modules")]


class OutsideRowsDraftTests(CoverageBase):
    def test_a_written_dev_null_gets_a_scratch_row_on_top_of_the_virtual_dev_row(self) -> None:
        t = str(self.tree)
        entry, review = self.unit(self.sample([open_line(t, "/dev/null", "O_WRONLY|O_CLOEXEC")]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        rows = {row["prefix"]: row for row in entry["scope"]["outside"]}
        self.assertEqual(rows["/dev/null"], {"prefix": "/dev/null", "class": "scratch"})
        self.assertEqual(rows["/dev/"]["class"], "virtual", "原有分类照旧，精确行只是补充")
        self.assertIn("`/dev/null` 被写入：草案 outside 在 `/dev/`（virtual 类）之外，另补了精确前缀 `/dev/null`", review)

    def test_a_read_only_dev_null_gets_no_scratch_row(self) -> None:
        t = str(self.tree)
        entry, review = self.unit(self.sample([open_line(t, "/dev/null")]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        self.assertEqual([row["prefix"] for row in entry["scope"]["outside"] if row["prefix"].startswith("/dev")], ["/dev/"])
        self.assertNotIn("被写入：草案 outside", review)

    def test_run_is_a_virtual_prefix(self) -> None:
        self.assertEqual(ds.Classifier().classify("/run/systemd/resolve/"), ("virtual", None, "/run/"))
        self.assertEqual(ds.Classifier().classify("/run/"), ("virtual", None, "/run/"))
        self.assertEqual(ds.Classifier([("/run/special/", "scratch", None)]).classify("/run/special/x")[0], "scratch", "更具体的人工登记仍然优先")
        t = str(self.tree)
        entry, _ = self.unit(self.sample([open_line(t, "/run/systemd/resolve/stub-resolv.conf")]))
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        self.assertIn({"prefix": "/run/", "class": "virtual"}, entry["scope"]["outside"])
        # virtual 类不许写：往 /run 下写的采样本身就无效。
        written = self.merge(self.raw(self.base_lines() + [write_line(t, "/run/systemd/resolve/x")]))
        self.assertFalse(written["valid"])
        self.assertIn("写入了 virtual 类路径", "；".join(written["invalid_reasons"]))

    def test_the_review_points_out_reads_on_the_ancestor_chain_keys(self) -> None:
        t, parent = str(self.tree), self.root.parent
        # 打开祖先目录本身：采样器记成 dir，聚合键上没有 read。
        _entry, review = self.unit(self.sample([open_line(t, str(self.root))]))
        section = review.split("### 10")[1].split("### 11")[0]
        self.assertIn("- 祖先链目录的聚合键上没有 read", section)
        self.assertNotIn("**祖先链目录的聚合键上有 read**", section)
        # 读祖先目录下的一个文件：聚合键丢了文件名，键上留着 read，清单点名让人确认。
        _entry, review = self.unit(self.sample([open_line(t, f"{parent}/ancestor-file.cfg")]))
        section = review.split("### 10")[1].split("### 11")[0]
        self.assertIn(f"- **祖先链目录的聚合键上有 read** 1 个：`{parent}/`", section)
        self.assertIn("请确认读的不是意外的宿主文件", section)
        # 祖先链之外的目录读文件不算。
        _entry, review = self.unit(self.sample([open_line(t, "/opt/other/tool")]))
        self.assertIn("- 祖先链目录的聚合键上没有 read", review.split("### 10")[1].split("### 11")[0])

    def test_usr_aarch64_linux_gnu_and_usr_gnu_are_deliberately_not_classified(self) -> None:
        classifier = ds.Classifier()
        self.assertEqual(classifier.classify("/usr/aarch64-linux-gnu/"), None)
        self.assertEqual(classifier.classify("/usr/gnu/"), None, "这两处是 missing 探测，由登记侧的噪声放行处理，不进内置前缀表")


class IgnoredHygieneDraftTests(CoverageBase):
    def frontend_with_writes(self) -> list[str]:
        t, cwd = str(self.tree), f"{self.tree}/frontend"
        reads = [open_line(cwd, f"{t}/frontend/package.json"), open_line(cwd, f"{t}/frontend/src/a.ts"), open_line(cwd, f"{t}/frontend/src/b.ts"),
                 open_line(cwd, f"{t}/frontend/node_modules/.pnpm/p/node_modules/p/index.js"), list_line(f"{t}/frontend/node_modules")]
        writes = [write_line(t, f"{t}/frontend/node_modules/.modules.yaml"), write_line(t, f"{t}/frontend/node_modules/.vite/vitest/results.json"),
                  write_line(t, f"{t}/frontend/.eslintcache"), write_line(t, f"{t}/.root-cache"), write_line(t, f"{t}/out/result.bin")]
        return reads + writes

    def test_repo_ignored_never_covers_the_installed_dir_or_its_parent(self) -> None:
        sample = self.sample(self.frontend_with_writes(), installed_dirs=[(NODE_MODULES, NODE_CONTRACT)])
        entry, review = self.unit(sample)
        self.assertEqual(entry["status"], "declared", entry.get("reason"))
        ignored = entry["scope"]["repo"]["repo_ignored"]
        self.assertEqual(ignored, sorted([".root-cache", "frontend/.eslintcache", "frontend/node_modules/.modules.yaml", "frontend/node_modules/.vite/vitest/", "out/"]))
        self.assertNotIn("frontend/node_modules/", ignored)
        self.assertNotIn("frontend/", ignored)
        self.assertIn("**repo_ignored 里有 3 项改用了被写文件的精确路径**", review)
        self.assertIn("`frontend/node_modules/`→`frontend/node_modules/.modules.yaml`", review)
        # 这条规则是真起作用的：照旧用目录前缀的话，frontend/ 盖住了 range 里已跟踪的输入，登记侧的覆盖检查就不通过。
        scope = copy.deepcopy(entry["scope"])
        scope["repo"]["repo_ignored"] = ["frontend/", "frontend/node_modules/", ".root-cache", "out/"]
        report = self.coverage_of(scope)
        self.assertFalse(report["ok"])
        self.assertIn("C1-ignored-tracked", rules_of(report))

    def test_without_an_installed_dir_the_ignored_directories_are_the_old_ones(self) -> None:
        t = str(self.tree)
        sample = self.sample([write_line(t, f"{t}/frontend/.eslintcache"), write_line(t, f"{t}/out/result.bin")])
        entry, review = self.unit(sample, coverage_check=False)
        self.assertEqual(entry["scope"]["repo"]["repo_ignored"], ["frontend/", "out/"])
        self.assertNotIn("改用了被写文件的精确路径", review)


# ---------------------------------------------------------------------------
# 覆盖自检
# ---------------------------------------------------------------------------


def fake_module(report=None, *, error: Exception | None = None):
    """只有 declared_coverage 与 validate_declared_scope 的假登记侧模块；记录每次调用收到的参数。"""
    calls: list[tuple] = []

    def declared_coverage(sample, scope, *, command_sha256=None):
        calls.append((sample, scope, command_sha256))
        if error is not None:
            raise error
        return report

    return types.SimpleNamespace(declared_coverage=declared_coverage, validate_declared_scope=lambda scope: scope), calls


def failed_report(findings: list[dict], *, total: int | None = None) -> dict:
    return {"ok": False, "undeclared": findings, "counts": {"undeclared": len(findings) if total is None else total}}


def finding(rule: str, path: str, kinds=("missing",), reason: str = "原因") -> dict:
    return {"rule": rule, "path": path, "kinds": list(kinds), "reason": reason}


class SelfCheckTests(CoverageBase):
    def patched(self, module, note: str = ""):
        return mock.patch.object(ds, "unit_records_module", return_value=(module, note))

    def test_a_passing_check_keeps_the_declared_entry_and_hands_the_record_layer_the_sample_and_the_scope(self) -> None:
        module, calls = fake_module({"ok": True, "undeclared": [], "counts": {"undeclared": 0, "repo_paths": 7, "outside_paths": 3, "exempt_paths": 1}})
        sample = self.sample()
        with self.patched(module):
            entry, review = self.unit(sample)
        self.assertEqual(entry["status"], "declared")
        (given_sample, given_scope, given_digest), = calls
        self.assertEqual(given_digest, sample["command"]["sha256"], "命令摘要用证据自己的，与写进登记表的 command_sha256 同一个值")
        self.assertEqual(given_scope, entry["scope"], "核对的就是写进登记表的那份 scope")
        self.assertEqual(given_sample["unit_id"], UNIT)
        self.assertEqual(entry["sample"]["command_sha256"], given_digest)
        self.assertIn("**覆盖自检通过**：起草的 scope 通过了登记侧的覆盖检查（unit_records.declared_coverage）：仓库内路径 7、仓库外目录 3、祖先探测等豁免 1。", review)

    def test_a_failing_check_turns_the_entry_into_rerun_only_with_statistics_and_the_first_three(self) -> None:
        # 明细按登记侧给的顺序取前 3 处；规则统计与分组则按规则名排序，不依赖明细的顺序。
        findings = [finding("C3-missing-undeclared", "{TREE}/c"), finding("C1-undeclared", "{TREE}/a", ["stat"]), finding("C1-undeclared", "{TREE}/b", ["dir", "stat"]),
                    finding("C3-missing-undeclared", "/x/y/"), finding("C3-missing-undeclared", "/z/")]
        module, _ = fake_module(failed_report(findings))
        with self.patched(module):
            entry, review = self.unit(self.sample())
        self.assertEqual(entry["status"], "rerun-only")
        self.assertEqual(sorted(entry), ["reason", "status"], "head 为 false：不带 head 键，也没有 scope 与 sample")
        self.assertEqual(entry["reason"], "声明级覆盖自检不通过：5 处未覆盖（规则统计 C1-undeclared×2、C3-missing-undeclared×3；"
                                          "前 3 处：C3-missing-undeclared {TREE}/c [missing]；C1-undeclared {TREE}/a [stat]；C1-undeclared {TREE}/b [dir,stat]）")
        self.assertIn("- **覆盖自检不通过**：起草的 scope 没能通过登记侧的覆盖检查", review.split("### 2 ")[0])
        section = review.split("### 13")[1]
        self.assertIn("共 5 处未覆盖，登记状态草案已改为 `rerun-only`。规则统计：`C1-undeclared`×2、`C3-missing-undeclared`×3", section)
        self.assertLess(section.index("`C1-undeclared` ×2"), section.index("`C3-missing-undeclared` ×3"), "按规则分组")
        for path in ("{TREE}/a", "{TREE}/b", "{TREE}/c", "/x/y/", "/z/"):
            self.assertIn(f"`{path}`", section, "全部未覆盖项都列出，不止前 3 处")
        self.assertIn("处理方向（按规则前缀", section)
        self.assertIn("`draft --class-prefix 前缀=类别[:合同]`", section)
        self.assertIn("总览", review)
        self.assertIn("另有 1 个因覆盖自检不通过（起草的 scope 过不了登记侧的覆盖检查）转 rerun-only", review)

    def test_the_reason_notes_when_the_record_layer_returned_only_part_of_the_details(self) -> None:
        findings = [finding("C3-missing-undeclared", f"/p{index}/") for index in range(200)]
        module, _ = fake_module(failed_report(findings, total=500))
        with self.patched(module):
            entry, review = self.unit(self.sample())
        self.assertTrue(entry["reason"].startswith("声明级覆盖自检不通过：500 处未覆盖（规则统计 C3-missing-undeclared×200，登记侧只返回前 200 处明细，统计按这些算；"))
        self.assertIn("登记侧只返回前 200 处明细（总数 500）", review)

    def test_long_paths_in_the_reason_are_shortened_but_the_review_keeps_them_whole(self) -> None:
        long_path = "{WORK}/go-build-cache/79/" + "a" * 300 + "-d/"
        module, _ = fake_module(failed_report([finding("C2-kinds", long_path, ["exec", "read"], "class=performance-cache 不允许的访问类型")]))
        with self.patched(module):
            entry, review = self.unit(self.sample())
        shown = entry["reason"].split("前 3 处：")[1].rstrip("）")
        self.assertEqual(len(shown), 160)
        self.assertTrue(shown.endswith("…"))
        self.assertIn(long_path, review)

    def test_the_review_lists_at_most_list_limit_items_per_rule(self) -> None:
        module, _ = fake_module(failed_report([finding("C3-missing-undeclared", f"/q{index}/") for index in range(7)]))
        with self.patched(module), mock.patch.object(ds, "LIST_LIMIT", 3):
            _entry, review = self.unit(self.sample())
        section = review.split("### 13")[1]
        self.assertEqual(section.count("  - `/q"), 3)
        self.assertIn("……共 7 项，只列前 3 项", section)

    def test_per_item_reasons_are_shown_only_when_a_rule_has_several_different_ones(self) -> None:
        module, _ = fake_module(failed_report([finding("C2-kinds", "{WORK}/a/", ["exec"], "class=performance-cache 不允许 exec"),
                                               finding("C2-kinds", "{WORK}/b/", ["write"], "class=dependency-content 不允许 write"),
                                               finding("C5-network", "10.0.0.1:80", [], "AF_INET 外连了非回环地址"),
                                               finding("C5-network", "10.0.0.2:80", [], "AF_INET 外连了非回环地址")]))
        with self.patched(module):
            _entry, review = self.unit(self.sample())
        section = review.split("### 13")[1]
        self.assertIn("`C2-kinds` ×2\n", section, "理由不同：不在组标题上写")
        self.assertIn("`{WORK}/a/` exec：class=performance-cache 不允许 exec", section)
        self.assertIn("`C5-network` ×2：AF_INET 外连了非回环地址", section, "理由相同：写在组标题上，逐项不重复")

    def test_an_exception_in_the_check_fails_closed(self) -> None:
        module, _ = fake_module(error=RuntimeError("boom"))
        with self.patched(module):
            entry, review = self.unit(self.sample())
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("声明级覆盖自检不通过：核对函数自己抛出异常（RuntimeError：boom），无法证明声明范围覆盖了采样的读集", entry["reason"])
        self.assertIn("**覆盖自检无法执行**：核对函数抛出异常（RuntimeError：boom）", review)

    def test_when_the_record_layer_is_unavailable_the_check_is_skipped_and_noted(self) -> None:
        with self.patched(None, "同目录没有 unit_records.py"):
            entry, review = self.unit(self.sample())
        self.assertEqual(entry["status"], "declared", "找不到登记侧模块时条目保持原状")
        self.assertIn("**覆盖自检未执行**：同目录没有 unit_records.py", review)
        self.assertNotIn("转 rerun-only", review.split("## 总览")[0])

    def test_invalid_samples_and_unfixed_git_usage_are_not_checked(self) -> None:
        t = str(self.tree)
        module, calls = fake_module({"ok": True, "undeclared": [], "counts": {}})
        invalid = self.merge(self.raw(self.base_lines() + [write_line(t, f"{t}/backend/a.go")]))
        self.assertFalse(invalid["valid"])
        unfixed = self.merge(self.raw(self.base_lines() + [self.git_head_line()]))
        self.assertTrue(unfixed["valid"], unfixed["invalid_reasons"])
        with self.patched(module):
            for sample in (invalid, unfixed):
                entry, review = self.unit(sample)
                self.assertEqual(entry["status"], "rerun-only")
                self.assertNotIn(ds.COVERAGE_REASON_PREFIX, entry["reason"])
                self.assertIn("本单元的采样无效或 git 用法未固定，登记状态草案已是 `rerun-only`，没有做覆盖自检", review)
        self.assertEqual(calls, [], "已经是 rerun-only 的单元不调用登记侧")

    def git_head_line(self) -> str:
        return '4242 execve("/usr/bin/git", ["git", "rev-parse", "HEAD"], 0x1 /* 17 vars */) = 0'

    def test_coverage_check_off_means_no_closure_and_no_self_check(self) -> None:
        t = str(self.tree)
        module, calls = fake_module({"ok": False, "undeclared": [finding("C1-undeclared", "{TREE}")], "counts": {"undeclared": 1}})
        sample = self.sample([list_line(t), missing_line(t, f"{t}/src/__tests__/setup.ts")])
        with self.patched(module):
            entry, review = self.unit(sample, coverage_check=False)
        self.assertEqual(entry["status"], "declared")
        self.assertEqual(calls, [])
        self.assertNotIn("root_listing", entry["scope"]["repo"])
        self.assertEqual([item["include"][0] for item in entry["scope"]["repo"]["ranges"]], ["backend/"])
        self.assertIn("覆盖闭合与覆盖自检已被 `--no-coverage-check` 关闭", review)

    def test_the_real_record_layer_is_loaded_by_path_and_used(self) -> None:
        module, note = ds.unit_records_module()
        self.assertIsNotNone(module, note)
        self.assertEqual(module.DECLARED_SCOPE_SCHEMA, ds.SCOPE_SCHEMA)
        self.assertIs(ds.unit_records_module()[0], module, "进程内缓存")
        # 真实函数：缺 go.sum 的仓库起草出来的 toolchain:go 范围非法，C0-scope 如实写进原因（旧夹具仓库就是这样）。
        root = self.root / "no-locks"
        root.mkdir(exist_ok=True)
        tree = make_repo(root, {"backend/go.mod": "module x\n", "backend/a.go": "package a\n"})
        sample = ds.merge_raws([make_raw(tree, self.work, [open_line(tree, f"{tree}/backend/go.mod"), open_line(tree, "/usr/local/go/src/fmt/print.go")])], tree=tree)
        samples = root / "samples"
        samples.mkdir(exist_ok=True)
        ds.write_document(samples / "backend-go-test.sample.json", sample)
        registry, review = ds.draft_registry(samples, tree)
        entry = registry["units"][UNIT]
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("C0-scope", entry["reason"])
        self.assertIn("toolchain:go", entry["reason"], "登记侧对范围非法的说明原样带进原因（缺依赖锁文件）")
        self.assertIn("**依赖锁文件缺失**", review, "原有的缺失提示照旧")

    def test_an_unclassified_outside_path_is_reported_and_a_class_prefix_repairs_the_draft(self) -> None:
        t = str(self.tree)
        sample = self.sample([open_line(t, "/opt/other/tool")])
        entry, review = self.unit(sample)
        self.assertEqual(entry["status"], "rerun-only")
        self.assertIn("C2-outside-undeclared /opt/other/ [read]", entry["reason"])
        self.assertIn("`/opt/other/` read", review.split("### 13")[1])
        repaired, repaired_review = self.unit(sample, class_prefixes=[ds.parse_class_prefix("/opt/other/=system")])
        self.assertEqual(repaired["status"], "declared", repaired.get("reason"))
        self.assertIn("**覆盖自检通过**", repaired_review)


class UnitRecordsLoaderTests(unittest.TestCase):
    # 和真实的 unit_records.py 一样用 from __future__ import annotations：dataclass 要按模块名在 sys.modules 里查自己的命名空间。
    GOOD = ("from __future__ import annotations\nfrom dataclasses import dataclass\n\n@dataclass\nclass Marker:\n    value: int = 1\n\n"
            "def declared_coverage(sample, scope, *, command_sha256=None):\n    return {'ok': True, 'undeclared': [], 'counts': {}}\n\n"
            "def validate_declared_scope(scope):\n    return scope\n")

    def load(self, files: dict[str, str], *, bytecode: bool = False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "declared_sampler.py").write_text("")
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            previous = sys.dont_write_bytecode
            sys.dont_write_bytecode = bytecode
            try:
                with mock.patch.object(ds, "HERE", root / "declared_sampler.py"), mock.patch.object(ds, "_UNIT_RECORDS", None):
                    result = ds.unit_records_module()
                    again = ds.unit_records_module()
                after = sys.dont_write_bytecode
            finally:
                sys.dont_write_bytecode = previous
            return result, again, after, sorted(os.listdir(root))

    def test_a_missing_file_gives_none_and_says_why(self) -> None:
        (module, note), _again, _after, _left = self.load({})
        self.assertIsNone(module)
        self.assertEqual(note, "同目录没有 unit_records.py")

    def test_a_module_without_the_two_functions_is_not_usable(self) -> None:
        for name, text in (("neither", "VALUE = 1\n"), ("only coverage", "def declared_coverage(*args, **kwargs):\n    return {}\n"),
                           ("only validator", "def validate_declared_scope(scope):\n    return scope\n"),
                           ("names but not callable", "declared_coverage = 1\nvalidate_declared_scope = 2\n")):
            with self.subTest(name):
                (module, note), _again, _after, _left = self.load({"unit_records.py": text})
                self.assertIsNone(module)
                self.assertIn("没有 declared_coverage 与 validate_declared_scope", note)
                self.assertNotIn("_declared_sampler_unit_records", sys.modules, "不可用的模块不留在 sys.modules 里")

    def test_a_module_that_fails_to_load_gives_none_with_the_error(self) -> None:
        for text, name in (("def broken(:\n", "SyntaxError"), ("raise RuntimeError('boom')\n", "RuntimeError")):
            with self.subTest(name):
                (module, note), _again, _after, _left = self.load({"unit_records.py": text})
                self.assertIsNone(module)
                self.assertIn(f"加载 unit_records.py 失败：{name}", note)
                self.assertNotIn("_declared_sampler_unit_records", sys.modules)

    def test_a_good_module_is_loaded_cached_and_leaves_no_bytecode_behind(self) -> None:
        for previous_flag in (False, True):
            with self.subTest(dont_write_bytecode=previous_flag):
                (module, note), again, after, left = self.load({"unit_records.py": self.GOOD}, bytecode=previous_flag)
                self.assertIsNotNone(module, note)
                self.assertEqual(note, "")
                self.assertIs(again[0], module, "结果缓存在进程内")
                self.assertEqual(module.Marker().value, 1, "dataclass 能用：模块在 exec 之前已放进 sys.modules")
                self.assertEqual(after, previous_flag, "加载期间强制不写字节码，加载完恢复原来的设置")
                self.assertEqual(left, ["declared_sampler.py", "unit_records.py"], "驱动目录里不留 __pycache__")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


class CommandLineTests(CoverageBase):
    def run_draft(self, *extra: str) -> subprocess.CompletedProcess:
        registry, review = self.root / f"registry-{self._testMethodName}.json", self.root / f"review-{self._testMethodName}.md"
        completed = subprocess.run([sys.executable, "-B", str(ds.HERE), "draft", "--samples", str(self.samples), "--tree", str(self.tree),
                                    "--registry-out", str(registry), "--review-out", str(review), *extra], capture_output=True, text=True)
        self.registry_path, self.review_path = registry, review
        return completed

    def test_the_command_line_closes_and_checks_by_default_and_the_flag_turns_both_off(self) -> None:
        t = str(self.tree)
        self.draft(self.sample([list_line(t), missing_line(t, f"{t}/src/__tests__/setup.ts")]), coverage_check=False)
        completed = self.run_draft()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"declared": 1, "rerun_only": 0, "units": 1})
        scope = json.loads(self.registry_path.read_text())["units"][UNIT]["scope"]
        self.assertIs(scope["repo"]["root_listing"], True)
        self.assertEqual([item["include"][0] for item in scope["repo"]["ranges"]], ["backend/", "src/__tests__/"])
        self.assertIn("**覆盖自检通过**", self.review_path.read_text(encoding="utf-8"))
        off = self.run_draft("--no-coverage-check")
        self.assertEqual(off.returncode, 0, off.stderr)
        scope = json.loads(self.registry_path.read_text())["units"][UNIT]["scope"]
        self.assertNotIn("root_listing", scope["repo"])
        self.assertEqual([item["include"][0] for item in scope["repo"]["ranges"]], ["backend/"])
        self.assertIn("已被 `--no-coverage-check` 关闭", self.review_path.read_text(encoding="utf-8"))

    def test_the_draft_command_line_refuses_an_over_wide_class_prefix(self) -> None:
        """WP4d：draft 的 --class-prefix 与登记侧一致，scratch／virtual 的前缀宽度受限；合格的照常。"""
        self.draft(self.sample(), coverage_check=False)
        for spec, message in (("/home/=scratch", "class=scratch 的前缀只许"), ("{TMP}=scratch", "--class-prefix 必须是"), ("/opt/=virtual", "class=virtual 的前缀只许")):
            completed = self.run_draft("--class-prefix", spec)
            self.assertEqual(completed.returncode, 2, spec)
            self.assertIn(message, completed.stderr, spec)
        good = self.run_draft("--class-prefix", "{HOME}/.config/go/telemetry/=scratch", "--class-prefix", "/run/special/=virtual")
        self.assertEqual(good.returncode, 0, good.stderr)

    def test_a_failed_check_counts_as_rerun_only_in_the_command_line_summary(self) -> None:
        t = str(self.tree)
        self.draft(self.sample([open_line(t, "/opt/other/tool")]), coverage_check=False)
        completed = self.run_draft()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"declared": 0, "rerun_only": 1, "units": 1})
        self.assertTrue(json.loads(self.registry_path.read_text())["units"][UNIT]["reason"].startswith(ds.COVERAGE_REASON_PREFIX))


# ---------------------------------------------------------------------------
# 祖先链目录上的 read：打开目录与读文件要分开
# ---------------------------------------------------------------------------


class AncestorDirectoryReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tree, self.work = self.root / "tree", self.root / "work"
        self.cwd = str(self.tree)

    def outside(self, lines: list[str]) -> dict[str, list[str]]:
        aggregator = ds.StreamAggregator(aliases_for(self.tree, self.work), unit_id=UNIT)
        document = feed_all(aggregator, lines)
        return {key: kinds for key, kinds, _count in document["outside_accesses"]}

    def test_the_set_of_directory_paths_is_the_alias_roots_and_all_their_ancestors(self) -> None:
        aggregator = ds.StreamAggregator(aliases_for(self.tree, self.work), unit_id=UNIT)
        self.assertEqual({"{TREE}", "{WORK}", "{HOME}", "{TMP}", "/", str(self.root)} - aggregator.directory_paths, set())
        for ancestor in Path(self.root).parents:
            self.assertIn(str(ancestor), aggregator.directory_paths)
        self.assertNotIn(str(self.root / "other-dir"), aggregator.directory_paths)

    def test_opening_an_ancestor_directory_is_a_dir_access_not_a_read(self) -> None:
        key = f"{self.root.parent}/"
        self.assertEqual(self.outside([open_line(self.cwd, str(self.root))]), {key: ["dir"]})

    def test_reading_a_file_directly_under_an_ancestor_stays_a_read(self) -> None:
        key = f"{self.root.parent}/"
        self.assertEqual(self.outside([open_line(self.cwd, f"{self.root.parent}/ancestor-file.cfg")]), {key: ["read"]})
        both = self.outside([open_line(self.cwd, str(self.root)), open_line(self.cwd, f"{self.root.parent}/ancestor-file.cfg")])
        self.assertEqual(both, {key: ["dir", "read"]}, "同一个聚合键上两种访问现在分得开了：dir 是打开祖先目录，read 是读了它下面的东西")

    def test_a_read_of_a_non_ancestor_sibling_directory_stays_a_read(self) -> None:
        self.assertEqual(self.outside([open_line(self.cwd, str(self.root / "other-dir"))]), {f"{self.root}/": ["read"]})

    def test_alias_roots_opened_as_directories_are_dir_accesses(self) -> None:
        lines = [open_line(self.cwd, str(self.work)), open_line(self.cwd, HOME), open_line(self.cwd, TMP)]
        self.assertEqual(self.outside(lines), {"{WORK}": ["dir"], "{HOME}": ["dir"], "{TMP}": ["dir"]})

    def test_the_filesystem_root_itself_is_a_directory(self) -> None:
        self.assertEqual(self.outside([open_line(self.cwd, "/")]), {"/": ["dir"]})
        self.assertEqual(self.outside([open_line(self.cwd, "/some-file")]), {"/": ["read"]})

    def test_only_the_read_bit_is_rewritten(self) -> None:
        # 往祖先目录下写、对它 stat、枚举它，都不是 read，保持原样。
        lines = [write_line(self.cwd, f"{self.root}/new-file"), stat_line(self.cwd, str(self.root)), list_line(str(self.root))]
        result = self.outside(lines)
        self.assertEqual(result[f"{self.root}/"], ["list", "write"])
        self.assertEqual(result[f"{self.root.parent}/"], ["stat"])

    def test_ancestor_keys_are_the_aggregated_keys_of_the_ancestor_chain(self) -> None:
        keys = ds.ancestor_keys(aliases_for(self.tree, self.work))
        for key in ("/", f"{self.root.parent}/", f"{self.root}/", "{WORK}/", "{WORK}", "{HOME}/", "{HOME}", "{TMP}/", "{TMP}", "/home/", "/tmp/"):
            self.assertIn(key, keys)
        self.assertNotIn(f"{self.root}/other-dir/", keys, "祖先链之外的目录不在其中")
        self.assertNotIn("{TREE}", keys, "TREE 自身是仓库内路径，不在仓库外的键空间里")

    def test_symlinked_roots_contribute_the_chain_of_their_real_path_too(self) -> None:
        """内核经 -y 给出的是解析后的真实路径（macOS 的 /tmp 就是 /private/tmp 的链接）：别名根的真实路径的祖先链也要算进来。"""
        real = self.root / "elsewhere" / "target"
        real.mkdir(parents=True)
        link = self.root / "link"
        link.symlink_to(real)
        aggregator = ds.StreamAggregator({**aliases_for(self.tree, self.work), "TMP": str(link)}, unit_id=UNIT)
        self.assertIn(f"{self.root}/elsewhere", aggregator.directory_paths)
        self.assertIn(f"{self.root}/elsewhere/", ds.ancestor_keys({**aliases_for(self.tree, self.work), "TMP": str(link)}))
        # 真实路径的祖先目录被打开，同样记成 dir。
        document = feed_all(aggregator, [open_line(self.cwd, f"{self.root}/elsewhere")])
        self.assertEqual({key: kinds for key, kinds, _count in document["outside_accesses"]}, {f"{self.root}/": ["dir"]})

    def test_repo_accesses_are_untouched(self) -> None:
        aggregator = ds.StreamAggregator(aliases_for(self.tree, self.work), unit_id=UNIT)
        document = feed_all(aggregator, [open_line(self.cwd, str(self.tree))])
        self.assertEqual(document["repo_accesses"], [["{TREE}", ["read"]]], "树根自身的 read 属于 repo_accesses，由 root_listing 处理")


if __name__ == "__main__":
    unittest.main()
