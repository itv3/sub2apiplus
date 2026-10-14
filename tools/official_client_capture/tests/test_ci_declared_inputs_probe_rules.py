"""声明级承接的覆盖规则扩展（步骤 5 工作包 WP1c；被测代码在 ``tools/ci/unit_records.py``）。

用 ARM64 上真实采样（egress scanner-check 冷热、frontend critical／lint／typecheck）重跑覆盖检查，四个单元全部不通过：make 在测试树根枚举
目录项，Go 把 ``go run``／``go tool`` 的可执行文件缓存在 GOCACHE 里并从那里执行，Node／Go 向上找工作区根与模块根，PATH 搜索、cgo 编译器
搜索路径、Node 模块解析逐级探测大量不存在的候选路径，TypeScript 对自身路径做大小写变体探测，写 ``/dev/null``。调度者裁决的规则扩展：

1. performance-cache 类允许 ``exec``（其它类不变）；
2. ``repo.root_listing``（布尔，缺省 false）：放行测试树根目录自身的 dir／list／stat／read 与树根直接子项的 missing 探测，不存在由输入里的
   ``scope:root-listing`` 条目（被 git 跟踪的顶层名字的摘要）绑定；
3. 仓库外「噪声探测」：没有声明前缀命中、访问类型只有 missing／stat／dir 的键，在系统目录放行根之下、在证据别名路径之下或是它们的祖先
   （不分大小写）、或含 ``node_modules`` 路径段，就放行；测试树的严格祖先目录另许 list／read；写入、执行、读已存在的非祖先目录照旧要声明前缀；
4. 写精确路径 ``/dev/null`` 放行，``/dev/`` 下其它路径照旧不许写；
5. 树根以下的仓库内 missing 与目录 stat 不放宽。

每一条都有正例和反例；复用 ``test_ci_declared_inputs_records`` 的夹具。
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_records as base
from tools.official_client_capture.tests.test_ci_unit_records import _git

# 别名路径全在系统目录放行根之外：这样「在别名路径之下／是它们的祖先」这一条能和「在系统目录放行根之下」分开验证。
FAR = {"TREE": "/data/ci/run/tree", "WORK": "/data/ci/run", "HOME": "/data/ci/home", "TMP": "/data/ci/tmp"}
# HOME 放在测试树祖先链之外，用来区分「测试树的严格祖先」与「别的别名根的祖先」。
FAR_HOME = {"TREE": "/data/ci/run/tree", "WORK": "/data/ci/run", "HOME": "/data/home/user", "TMP": "/data/ci/tmp"}


def bare_scope(**repo) -> dict:
    """最小声明范围：一个 backend range，没有任何 outside 前缀——所以仓库外的访问全靠放行规则。"""

    return {"schema_version": ur.DECLARED_SCOPE_SCHEMA, "repo": {"ranges": [{"name": "backend", "include": ["backend/"], "exclude": []}], **repo}}


def bare_sample(*, repo=(), outside=(), aliases=None) -> dict:
    """只含给定访问的采样证据（其余字段合格）：一行已跟踪的 backend 读取，加上调用方给的仓库内、仓库外行。"""

    sample = base.make_sample()
    sample["repo_accesses"] = [["{TREE}/backend/a.go", ["read"], True], *copy.deepcopy([list(row) for row in repo])]
    sample["outside_accesses"] = copy.deepcopy([list(row) for row in outside])
    sample["git_commands"] = []
    sample["network"] = []
    sample["aliases"] = dict(aliases if aliases is not None else FAR)
    return sample


def rules_of(result: dict) -> list[str]:
    return sorted({item["rule"] for item in result["undeclared"]})


def cover(*, repo=(), outside=(), aliases=None, scope=None, **kwargs) -> dict:
    return ur.declared_coverage(bare_sample(repo=repo, outside=outside, aliases=aliases), scope if scope is not None else bare_scope(), **kwargs)


class PerformanceCacheExecTests(unittest.TestCase):
    """裁决 1：Go 1.24 起 ``go run``／``go tool`` 的可执行文件缓存在 GOCACHE 里并从那里执行。"""

    def scope(self) -> dict:
        scope = bare_scope()
        scope["repo"]["files"] = ["backend/go.sum"]  # WP1e 起声明 cache:go-mod-cache 必须单独列出它的锁文件
        scope["contracts"] = ["cache:go-build-cache", "cache:go-mod-cache", "host-config:gitconfig"]
        scope["outside"] = [{"prefix": "{WORK}/go-build-cache/", "class": "performance-cache", "contract": "cache:go-build-cache"},
                            {"prefix": "{WORK}/go-mod-cache/", "class": "dependency-content", "contract": "cache:go-mod-cache"},
                            {"prefix": "/proc/", "class": "virtual"}, {"prefix": "{HOME}/.gitconfig", "class": "home-config", "contract": "host-config:gitconfig"},
                            {"prefix": "/usr/lib/", "class": "system"}, {"prefix": "{TMP}/", "class": "scratch"}]
        return scope

    def test_the_class_table_only_changed_for_performance_cache(self) -> None:
        self.assertEqual(ur.DECLARED_CLASSES["performance-cache"], frozenset({"read", "write", "exec", "stat", "dir", "list", "missing"}))
        self.assertEqual(ur.DECLARED_CLASSES["toolchain"], frozenset({"read", "exec", "stat", "dir", "list", "missing"}))
        self.assertEqual(ur.DECLARED_CLASSES["dependency-content"], frozenset({"read", "stat", "dir", "list", "missing"}))
        self.assertEqual(ur.DECLARED_CLASSES["scratch"], frozenset({"read", "exec", "write", "missing", "stat", "dir", "list"}))
        self.assertEqual(ur.DECLARED_CLASSES["system"], frozenset({"read", "exec", "stat", "dir", "list", "missing"}))
        self.assertEqual(ur.DECLARED_CLASSES["virtual"], frozenset({"read", "stat", "dir", "list", "missing"}))
        self.assertEqual(ur.DECLARED_CLASSES["home-config"], frozenset({"read", "stat", "missing"}))
        doc = ur.declared_coverage.__doc__ or ""
        self.assertIn("GOCACHE", doc)
        self.assertIn("威胁模型", doc, "文档里要写明放行 exec 的理由和风险（有人能写 GOCACHE 即可替换可执行文件，不在威胁模型内）")

    def test_executing_a_cached_binary_is_allowed_in_the_build_cache_only(self) -> None:
        scope = self.scope()
        cached = ["{WORK}/go-build-cache/79/79bdbbed0f69-d/", ["exec", "list", "missing", "read", "stat", "write"], 12]
        self.assertTrue(cover(outside=[cached], scope=scope)["ok"], "构建缓存里写出再执行：放行（go run／go tool）")
        for label, row, rule in (("依赖内容不许执行", ["{WORK}/go-mod-cache/x/", ["read", "exec"], 1], "C2-kinds"),
                                 ("虚拟文件系统不许执行", ["/proc/x/", ["exec"], 1], "C2-kinds"),
                                 ("HOME 配置不许执行", ["{HOME}/.gitconfig", ["read", "exec"], 1], "C2-kinds")):
            result = cover(outside=[row], scope=scope)
            self.assertEqual(rules_of(result), [rule], label)
        for label, row in (("系统库本来就可执行", ["/usr/lib/x/", ["exec", "read"], 1]), ("临时输出本来就可执行", ["{TMP}/x/", ["exec", "write"], 1])):
            self.assertTrue(cover(outside=[row], scope=scope)["ok"], label)

    def test_a_cache_prefix_does_not_make_other_kinds_legal(self) -> None:
        scope = self.scope()
        # 放行 exec 只是多一种访问类型：没有前缀命中的路径照旧要声明，执行依然不在「噪声探测」之列。
        result = cover(outside=[["/opt/tool/", ["exec"], 1]], scope=scope)
        self.assertEqual(rules_of(result), ["C2-outside-undeclared"])
        self.assertEqual(rules_of(cover(outside=[["{WORK}/other-cache/", ["exec"], 1]], scope=scope)), ["C2-outside-undeclared"])


class RootListingScopeTests(unittest.TestCase):
    """裁决 2 的校验：布尔值、保留的 range 名、文档化的键集合。"""

    def test_root_listing_must_be_a_boolean(self) -> None:
        for value in (True, False):
            checked = ur.validate_declared_scope(bare_scope(root_listing=value))
            self.assertIs(checked["repo"]["root_listing"], value)
        for bad in (1, 0, "true", None, [], {}, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                ur.validate_declared_scope(bare_scope(root_listing=bad))
            self.assertIn("repo.root_listing 必须是布尔值", str(caught.exception))
        self.assertNotIn("root_listing", ur.validate_declared_scope(bare_scope())["repo"], "缺省不会自动补键")

    def test_root_listing_is_a_reserved_range_name(self) -> None:
        scope = bare_scope()
        scope["repo"]["ranges"].append({"name": ur.DECLARED_ROOT_LISTING_NAME, "include": ["docs/"], "exclude": []})
        with self.assertRaises(ValueError) as caught:
            ur.validate_declared_scope(scope)
        self.assertIn("root-listing", str(caught.exception))
        self.assertEqual(ur.DECLARED_ROOT_LISTING_ENTRY, "scope:root-listing")

    def test_the_documentation_names_the_new_key_and_its_boundary(self) -> None:
        doc = ur.validate_declared_scope.__doc__ or ""
        self.assertIn("repo.root_listing", doc)
        self.assertIn("未跟踪", doc, "边界要写明：只绑定被跟踪的顶层名字，未跟踪的新文件不绑定")


class RootListingExpansionTests(base.DeclaredTestCase):
    """裁决 2 的展开：输入里多一个 ``scope:root-listing`` 条目，摘要＝被跟踪的顶层名字的规范化 JSON 摘要。"""

    def entries(self, repo=None, *, root_listing: bool | None = True) -> list[dict]:
        scope = base.make_scope()
        if root_listing is not None:
            scope["repo"]["root_listing"] = root_listing
        return ur.declared_inputs(repo or self.world.repo, self.world.declaration(scope=scope))

    def listing(self, entries: list[dict]) -> dict:
        return next(entry for entry in entries if entry["name"] == ur.DECLARED_ROOT_LISTING_ENTRY)

    def reload(self) -> "ur.RepoIndex":
        return ur.RepoIndex.load(self.world.root)

    def test_the_entry_exists_only_when_asked_for_and_has_the_documented_shape(self) -> None:
        entry = self.listing(self.entries())
        names = [".gitignore", "Makefile", "VERSION", "backend/", "docs/", "frontend/", "pkg/", "tools/"]
        self.assertEqual(entry["category"], "tests")
        self.assertEqual(entry["detail"], {"names": names}, "文件取文件名，目录取目录名加尾斜杠，排序")
        self.assertEqual(entry["sha256"], ur.sha256_json(names))
        for flag in (False, None):
            plain = self.entries(root_listing=flag)
            self.assertNotIn(ur.DECLARED_ROOT_LISTING_ENTRY, [item["name"] for item in plain], flag)
        flagged = [item["name"] for item in self.entries()]
        self.assertEqual(sorted(set(flagged) - {ur.DECLARED_ROOT_LISTING_ENTRY}), sorted(item["name"] for item in self.entries(root_listing=False)),
                         "除了这一个条目，其余条目的名字不变")

    def test_the_digest_follows_tracked_top_level_names_and_nothing_else(self) -> None:
        root = self.world.root
        before = self.listing(self.entries())["sha256"]
        # 内容变、嵌套新文件、未跟踪的新文件：都不改变「被跟踪的顶层名字」。
        base._write(root, {"VERSION": "0.0.2\n"})
        base._commit(root, "改顶层文件内容")
        base._write(root, {"backend/new.go": "package main\n"})
        base._commit(root, "已有目录里加嵌套文件")
        base._write(root, {"untracked-at-root.txt": "未跟踪\n"})
        self.assertEqual(self.listing(self.entries(self.reload()))["sha256"], before, "未跟踪的根下新文件不绑定（与「未跟踪读取一律违规」同一口径）")
        (root / "untracked-at-root.txt").unlink()  # 下面的提交用 add -A，别把它顺手提交进去
        # 新增被跟踪的顶层文件、顶层目录；删除被跟踪的顶层文件：摘要都变，且彼此不同。
        base._write(root, {"package.json": "{}\n"})
        base._commit(root, "加顶层文件")
        with_file = self.listing(self.entries(self.reload()))
        self.assertNotEqual(with_file["sha256"], before)
        self.assertIn("package.json", with_file["detail"]["names"])
        self.assertNotIn("untracked-at-root.txt", with_file["detail"]["names"])
        base._write(root, {"src/x.ts": "export {}\n"})
        base._commit(root, "加顶层目录")
        with_dir = self.listing(self.entries(self.reload()))
        self.assertIn("src/", with_dir["detail"]["names"])
        self.assertNotEqual(with_dir["sha256"], with_file["sha256"])
        _git(root, "rm", "-q", "package.json")
        base._commit(root, "删顶层文件")
        removed = self.listing(self.entries(self.reload()))
        self.assertNotIn("package.json", removed["detail"]["names"])
        self.assertNotEqual(removed["sha256"], with_dir["sha256"])

    def test_a_tracked_file_appearing_under_a_probed_name_changes_the_binding(self) -> None:
        """树根下探测的「不存在」候选名靠这个摘要绑定：同名的跟踪文件一出现，摘要变，单元不再被承接。"""

        root = self.world.root
        before = self.listing(self.entries())["sha256"]
        base._write(root, {"eslint.config.js": "export default []\n"})
        base._commit(root, "出现被探测过的配置名")
        self.assertNotEqual(self.listing(self.entries(self.reload()))["sha256"], before)

    def test_the_scope_entry_digest_changes_with_the_flag(self) -> None:
        on = next(item for item in self.entries() if item["name"] == ur.DECLARED_SCOPE_ENTRY)
        off = next(item for item in self.entries(root_listing=False) if item["name"] == ur.DECLARED_SCOPE_ENTRY)
        self.assertNotEqual(on["sha256"], off["sha256"], "声明变了，单元失效")


class RootListingCoverageTests(unittest.TestCase):
    """裁决 2 的覆盖：树根目录自身的 dir／list／stat／read，树根直接子项的 missing；树根以下一概不放宽（裁决 5）。"""

    def root_rows(self, kinds):
        return [["{TREE}", list(kinds), False]]

    def test_the_root_directory_itself_may_be_listed_and_read_only_with_the_flag(self) -> None:
        flagged = bare_scope(root_listing=True)
        for kinds in (["stat"], ["dir"], ["dir", "stat"], ["list"], ["dir", "list", "stat"], ["read"], ["dir", "list", "read", "stat"]):
            with self.subTest(kinds=kinds):
                result = cover(repo=self.root_rows(kinds), scope=flagged)
                self.assertTrue(result["ok"], result["undeclared"])
        counts = cover(repo=self.root_rows(["dir", "list", "read", "stat"]), scope=flagged)["counts"]
        self.assertEqual((counts["root_listing_paths"], counts["exempt_paths"]), (1, 0))
        # 没有这个键（缺省或 false）：枚举、读取树根照旧违规，stat／dir 照旧放行。
        for plain in (bare_scope(), bare_scope(root_listing=False)):
            self.assertTrue(cover(repo=self.root_rows(["dir", "stat"]), scope=plain)["ok"])
            for kinds in (["list"], ["read"], ["dir", "list", "stat"]):
                self.assertEqual(rules_of(cover(repo=self.root_rows(kinds), scope=plain)), ["C1-undeclared"], kinds)
        # 有键也只放行枚举与读取：写入、执行树根仍然违规。
        for kinds in (["write"], ["exec"], ["list", "write"], ["read", "exec"]):
            self.assertEqual(rules_of(cover(repo=self.root_rows(kinds), scope=flagged)), ["C1-undeclared"], kinds)

    def test_direct_children_of_the_root_may_be_probed_missing_only_with_the_flag(self) -> None:
        names = ["RCS", "SCCS", ".browserslistrc", "browserslist", "browserslist-stats.json", "eslint.config.js", "eslint.config.cjs", "lerna.json",
                 "package.json", "pnpm-workspace.yaml", "node_modules"]
        rows = [[f"{{TREE}}/{name}", ["missing"], False] for name in names]
        result = cover(repo=rows, scope=bare_scope(root_listing=True))
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertEqual(result["counts"]["root_listing_paths"], len(names))
        for plain in (bare_scope(), bare_scope(root_listing=False)):
            result = cover(repo=rows, scope=plain)
            self.assertEqual(rules_of(result), ["C3-missing-undeclared"])
            self.assertEqual(result["counts"]["undeclared"], len(names), "缺省与 false 时与从前逐字相同：每个候选名都违规")

    def test_only_single_names_directly_under_the_root_are_relaxed(self) -> None:
        flagged = bare_scope(root_listing=True)
        for path in ("{TREE}/src/__tests__/setup.ts", "{TREE}/node_modules/@types", "{TREE}/node_modules/node:fs/package.json", "{TREE}/docs/legal/a.md?raw",
                     "{TREE}/a/b"):
            with self.subTest(path=path):
                self.assertEqual(rules_of(cover(repo=[[path, ["missing"], False]], scope=flagged)), ["C3-missing-undeclared"])

    def test_the_relaxation_is_for_missing_probes_only(self) -> None:
        flagged = bare_scope(root_listing=True)
        for label, kinds, rule in (("不存在又被 stat", ["missing", "stat"], "C1-undeclared"), ("不存在又被读", ["missing", "read"], "C1-undeclared"),
                                   ("存在的未声明文件被读", ["read"], "C1-undeclared"), ("存在的未声明文件被 stat", ["stat"], "C1-undeclared"),
                                   ("探测之后被写出", ["missing", "write"], "C1-undeclared"), ("存在的目录被枚举", ["list"], "C1-list-undeclared")):
            with self.subTest(label):
                self.assertEqual(rules_of(cover(repo=[["{TREE}/package.json", kinds, False]], scope=flagged)), [rule])

    def test_directory_stat_and_nested_missing_probes_stay_strict(self) -> None:
        """裁决 5：非树根的仓库内 missing 与「目录被 stat 而下面没有声明内容」不放宽，由 draft 补 range。"""

        flagged = bare_scope(root_listing=True)
        self.assertEqual(rules_of(cover(repo=[["{TREE}/docs/legal", ["stat"], False]], scope=flagged)), ["C1-undeclared"])
        self.assertEqual(rules_of(cover(repo=[["{TREE}/docs/legal/a.en.md?raw.ts", ["missing"], False]], scope=flagged)), ["C3-missing-undeclared"])
        covered = bare_scope(root_listing=True)
        covered["repo"]["ranges"].append({"name": "legal", "include": ["docs/legal/"], "exclude": []})
        self.assertTrue(cover(repo=[["{TREE}/docs/legal", ["stat"], False], ["{TREE}/docs/legal/a.en.md?raw.ts", ["missing"], False]], scope=covered)["ok"],
                        "补上 range 就覆盖（draft 的活）")


class RootListingConsumerTests(base.DeclaredTestCase):
    """裁决 2 的消费者核验：声明了 root_listing 的记录必须带格式合格、摘要自洽的 ``scope:root-listing`` 条目。"""

    def world_with(self, *, root_listing: bool) -> base.DeclaredWorld:
        scope = base.make_scope()
        if root_listing:
            scope["repo"]["root_listing"] = True
        return base.DeclaredWorld(Path(self._directory.name) / ("flagged" if root_listing else "plain"), scope=scope)

    def retarget(self, world: base.DeclaredWorld, change) -> dict:
        """改封存记录的输入明细，并把 inputs_sha256 与 read_audit 的绑定一并重算（让核验走到根目录列表这一步）。"""

        def apply(body):
            change(body)
            body["inputs_sha256"] = ur.entries_sha256(body["inputs"])
            body["read_audit"]["inputs_sha256"] = body["inputs_sha256"]

        return world.reseal(apply)

    def reject(self, world: base.DeclaredWorld, item: dict, fragment: str) -> None:
        problems = world.check(item)
        self.assertTrue(any(fragment in problem for problem in problems), f"应报「{fragment}」：{problems}")

    def test_a_record_with_a_well_formed_root_listing_entry_passes(self) -> None:
        world = self.world_with(root_listing=True)
        self.assertEqual([item["name"] for item in world.base["record"]["inputs"] if item["name"].endswith("root-listing")], [ur.DECLARED_ROOT_LISTING_ENTRY])
        self.assertEqual(world.check(), [])
        self.assertEqual(world.check(historical_read_audit=True, require_read_audit=True), [])

    def test_a_missing_or_malformed_entry_is_refused(self) -> None:
        world = self.world_with(root_listing=True)
        fragment = "声明范围要求 repo.root_listing"

        def entry(body):
            return next(item for item in body["inputs"] if item["name"] == ur.DECLARED_ROOT_LISTING_ENTRY)

        cases = {
            "条目不见了": lambda body: body["inputs"].remove(entry(body)),
            "类别不对": lambda body: entry(body).update(category="policy"),
            "摘要与名字清单不符": lambda body: entry(body).update(sha256="0" * 64),
            "名字没排序": lambda body: entry(body)["detail"].update(names=list(reversed(entry(body)["detail"]["names"]))),
            "名字有重复": lambda body: entry(body)["detail"].update(names=[*entry(body)["detail"]["names"], "VERSION"]),
            "名字不是字符串": lambda body: entry(body)["detail"].update(names=[1, 2]),
            "名字清单不是数组": lambda body: entry(body)["detail"].update(names="VERSION"),
            "detail 不是对象": lambda body: entry(body).update(detail=[]),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.reject(world, self.retarget(world, change), fragment)

    def test_a_duplicated_entry_is_refused_as_a_duplicate_name(self) -> None:
        world = self.world_with(root_listing=True)
        item = self.retarget(world, lambda body: body["inputs"].append(copy.deepcopy(next(e for e in body["inputs"] if e["name"] == ur.DECLARED_ROOT_LISTING_ENTRY))))
        self.reject(world, item, "重名条目")

    def test_an_entry_the_scope_did_not_ask_for_is_refused(self) -> None:
        world = self.world_with(root_listing=False)
        self.assertEqual(world.check(), [])
        names = [".gitignore", "Makefile"]
        extra = {"category": "tests", "name": ur.DECLARED_ROOT_LISTING_ENTRY, "sha256": ur.sha256_json(names), "detail": {"names": names}}
        self.reject(world, self.retarget(world, lambda body: body["inputs"].append(extra)), "声明范围没有 repo.root_listing")


class ProbeNoiseTests(unittest.TestCase):
    """裁决 3：仓库外「噪声探测」放行，以及测试树严格祖先目录的 list／read。"""

    PROBES = (["missing"], ["stat"], ["dir"], ["missing", "stat"], ["dir", "stat"], ["missing", "stat", "dir"])
    BEYOND_PROBES = (["read"], ["list"], ["write"], ["exec"], ["missing", "read"], ["stat", "list"], ["missing", "write"], ["dir", "exec"])

    def test_system_directories_absorb_probes_but_not_reads_listings_writes_or_executions(self) -> None:
        for root in ur._DECLARED_PROBE_ROOTS:
            key = f"{root}deep/dir/"
            for kinds in self.PROBES:
                with self.subTest(key=key, kinds=kinds):
                    result = cover(outside=[[key, kinds, 3]])
                    self.assertTrue(result["ok"], result["undeclared"])
            for kinds in self.BEYOND_PROBES:
                with self.subTest(key=key, beyond=kinds):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 3]])), ["C2-outside-undeclared"])
        self.assertEqual(rules_of(cover(outside=[["/usr/deep/", ["missing"], 1]], scope=bare_scope())), [])
        counts = cover(outside=[["/usr/", ["missing", "stat"], 1], ["/opt/", ["missing"], 1]])["counts"]
        self.assertEqual((counts["probe_paths"], counts["exempt_paths"]), (2, 0))

    def test_the_probe_roots_are_directories_not_string_prefixes(self) -> None:
        for key in ("/usrx/y/", "/optional/", "/variable/x/", "/homer/", "/rooted/", "/data/x/", "/work/x/", "/srvx/"):
            with self.subTest(key=key):
                self.assertEqual(rules_of(cover(outside=[[key, ["missing"], 1]])), ["C3-missing-undeclared"])
        self.assertTrue(cover(outside=[["/usr", ["stat"], 1], ["/etc", ["stat"], 1]])["ok"], "放行根自身（不带尾斜杠的精确键）也算")

    def test_node_modules_segments_are_probe_noise_wherever_they_are(self) -> None:
        for key in ("/node_modules/", "/node_modules/node:fs/", "/work/frontend/node_modules/.pnpm/vitest@2.1.9/node_modules/vitest/", "/a/b/node_modules",
                    "/var/tmp/node_modules/.vue-global-types/", "{WORK}/node_modules/node:path/"):
            for kinds in (["missing"], ["stat", "dir"]):
                with self.subTest(key=key, kinds=kinds):
                    self.assertTrue(cover(outside=[[key, kinds, 1]])["ok"])
            with self.subTest(key=key, beyond="read"):
                self.assertEqual(rules_of(cover(outside=[[key, ["read"], 1]])), ["C2-outside-undeclared"])
        for key in ("/a/node_modulesX/", "/a/my_node_modules/", "/a/node-modules/", "/a/Node_Modules/"):
            with self.subTest(key=key):
                self.assertEqual(rules_of(cover(outside=[[key, ["missing"], 1]])), ["C3-missing-undeclared"], "必须是整段 node_modules")

    def test_paths_under_or_above_the_evidence_aliases_are_noise_ignoring_case(self) -> None:
        # WP1e 起 HOME 之下的探测不再是噪声，WP1f 起 TMP 也不是（采样器写下的 TMP 恒为 /tmp，是用户可写区）：别名根只取 TREE、WORK。原来这里还有
        # /data/ci/home/.config/、{HOME}/.cfg/、/data/ci/tmp/x/、{TMP}/y/，移到下面的违规断言。
        for key in ("/data/ci/run/tree/frontend/", "/DATA/CI/RUN/TREE/FRONTEND/NODE_MODULES/.PNPM/TYPESCRIPT@5.6.3/LIB/", "/Data/Ci/Run/Tree/",
                    "/data/ci/run/anything/deeper/", "{WORK}/x/", "{WORK}/"):
            for kinds in (["missing"], ["stat"], ["dir", "stat"]):
                with self.subTest(key=key, kinds=kinds):
                    result = cover(outside=[[key, kinds, 1]])
                    self.assertTrue(result["ok"], result["undeclared"])
        for key in ("/data/ci/home/.config/", "{HOME}/.cfg/", "/DATA/CI/HOME/.CONFIG/", "/data/ci/tmp/x/", "{TMP}/y/", "/DATA/CI/TMP/Z/"):
            for kinds, rule in ((["missing"], "C3-missing-undeclared"), (["stat"], "C2-outside-undeclared"), (["dir", "stat"], "C2-outside-undeclared")):
                with self.subTest(key=key, kinds=kinds, home_or_tmp=True):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 1]])), [rule], "HOME、TMP 之下的探测要登记 host-config／scratch")
        # 读取、枚举、写入、执行不在放行之列。
        for key in ("{WORK}/x/", "/data/ci/home/.config/", "/DATA/CI/RUN/TREE/X/"):
            for kinds in (["read"], ["list"], ["write"], ["exec"]):
                with self.subTest(key=key, beyond=kinds):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 1]])), ["C2-outside-undeclared"])
        # 不相干的路径，包括只是字符串前缀相同的兄弟目录，照旧违规。
        for key in ("/data/other/", "/data/ci2/", "/data/ci/runner/", "/data/ci/run2/x/", "/datax/"):
            with self.subTest(key=key):
                self.assertEqual(rules_of(cover(outside=[[key, ["missing"], 1]])), ["C3-missing-undeclared"])

    def test_ancestors_of_every_alias_root_may_be_probed(self) -> None:
        # WORK／TREE／TMP 的祖先：missing／stat 都放行（噪声探测）。WP1e 起 HOME 不再算别名根：HOME 的祖先 /data/home/（不在其它别名根的
        # 祖先链上）只放行 stat／dir（祖先目录规则，WP1e 之前还放行 missing），HOME 自身同样。
        self.assertTrue(cover(outside=[["/data/", ["missing"], 1], ["/", ["stat", "missing"], 1], ["/data/ci/", ["missing", "stat"], 1]],
                              aliases=FAR_HOME)["ok"])
        self.assertTrue(cover(outside=[["/data/home/", ["stat", "dir"], 1], ["{HOME}", ["stat"], 1]], aliases=FAR_HOME)["ok"])
        self.assertEqual(rules_of(cover(outside=[["/data/home/", ["missing", "stat"], 1]], aliases=FAR_HOME)), ["C2-outside-undeclared"],
                         "HOME 的祖先带 missing：不再是噪声")
        self.assertEqual(rules_of(cover(outside=[["/data/home/other/", ["missing"], 1]], aliases=FAR_HOME)), ["C3-missing-undeclared"])
        self.assertEqual(rules_of(cover(outside=[["/data/home/", ["list"], 1]], aliases=FAR_HOME)), ["C2-outside-undeclared"], "HOME 的祖先不在测试树祖先链上：不许枚举")

    def test_without_trusted_alias_paths_only_the_roots_and_node_modules_apply(self) -> None:
        broken = {"TREE": "relative/tree", "WORK": "", "HOME": "~", "TMP": "/"}
        self.assertEqual(rules_of(cover(outside=[["/data/ci/run/tree/x/", ["missing"], 1]], aliases=broken)), ["C3-missing-undeclared"])
        self.assertTrue(cover(outside=[["/usr/x/", ["missing"], 1], ["/a/node_modules/", ["missing"], 1]], aliases=broken)["ok"])

    def test_strict_ancestors_of_the_test_tree_may_be_listed_and_read(self) -> None:
        # FAR 的测试树链：/、/data、/data/ci、{WORK}（/data/ci/run 在键空间里写成 {WORK}）。
        # WP1e 起：所有严格祖先键放行 list／dir／stat／missing，read 只对 {WORK} 这一个键放行（原来 /、/data/ci/ 这里也带 read，现在移到下面的违规断言）。
        chain = [["/", ["list", "missing", "stat"], 9], ["/data/", ["list", "missing"], 3], ["/data/ci/", ["list", "stat"], 2],
                 ["{WORK}", ["read", "stat"], 1], ["{WORK}/", ["list", "missing", "read"], 1]]
        result = cover(outside=chain)
        self.assertTrue(result["ok"], result["undeclared"])
        self.assertEqual(result["counts"]["probe_paths"], len(chain))
        for key in ("/", "/data/", "/data/ci/"):
            for kinds in (["read"], ["read", "stat"], ["list", "missing", "read"]):
                with self.subTest(key=key, kinds=kinds, read_on_other_ancestors=True):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 1]])), ["C2-outside-undeclared"], "祖先键下的 read 是读了该目录下别的子项")
        # 链之外的目录、测试树的后代、别的别名根：不许枚举、不许读。
        for key in ("/data/ci/other/", "{WORK}/tree/", "{HOME}/", "{HOME}", "{TMP}/", "/data/ci/home/x/", "/opt/"):
            for kinds in (["list"], ["read"], ["missing", "list"], ["stat", "read"]):
                with self.subTest(key=key, kinds=kinds):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 1]])), ["C2-outside-undeclared"])
        # 严格祖先上的写入与执行照旧违规。
        for key in ("/", "/data/", "/data/ci/", "{WORK}", "{WORK}/"):
            for kinds in (["write"], ["exec"], ["stat", "write"], ["read", "exec"]):
                with self.subTest(key=key, beyond=kinds):
                    self.assertEqual(rules_of(cover(outside=[[key, kinds, 1]])), ["C2-outside-undeclared"])
        # 证据没给出可信的测试树绝对路径时，没有「严格祖先」这个概念。
        self.assertEqual(rules_of(cover(outside=[["/", ["list"], 1]], aliases={"TREE": "relative/tree", "WORK": "/data/ci/run", "HOME": "", "TMP": ""})),
                         ["C2-outside-undeclared"])

    def test_another_alias_root_is_not_a_strict_ancestor_of_the_tree(self) -> None:
        # HOME 在 /data/home/user，不是测试树的祖先：它的祖先 /data/home/ 可以被 stat／探测，但不能枚举。
        self.assertEqual(rules_of(cover(outside=[["/data/home/", ["list"], 1], ["{HOME}", ["read"], 1]], aliases=FAR_HOME)), ["C2-outside-undeclared"])

    def test_declared_prefixes_still_decide_for_the_paths_they_cover(self) -> None:
        scope = bare_scope()
        scope["contracts"] = ["host-config:gitconfig"]
        scope["outside"] = [{"prefix": "{HOME}/.gitconfig", "class": "home-config", "contract": "host-config:gitconfig"}, {"prefix": "/proc/", "class": "virtual"}]
        # 命中声明前缀的键按 class 判，噪声探测不会越过它（枚举 home-config 仍然违规；virtual 里的执行仍然违规）。
        self.assertEqual(rules_of(cover(outside=[["{HOME}/.gitconfig", ["read", "list"], 1]], scope=scope)), ["C2-kinds"])
        self.assertEqual(rules_of(cover(outside=[["/proc/x/", ["exec"], 1]], scope=scope)), ["C2-kinds"])
        self.assertTrue(cover(outside=[["{HOME}/.gitconfig", ["missing"], 1], ["/proc/x/", ["missing", "read"], 1]], scope=scope)["ok"])


class DevNullTests(unittest.TestCase):
    """裁决 4：写精确路径 ``/dev/null`` 放行（merge 也这么豁免），``/dev/`` 下其它路径照旧不许写。"""

    def scope(self, *extra) -> dict:
        scope = bare_scope()
        scope["outside"] = [{"prefix": "/dev/", "class": "virtual"}, *extra]
        return scope

    def test_writing_dev_null_is_allowed_and_other_dev_paths_are_not(self) -> None:
        scope = self.scope()
        self.assertTrue(cover(outside=[["/dev/null", ["read", "stat", "write"], 7]], scope=scope)["ok"])
        self.assertTrue(cover(outside=[["/dev/null", ["write"], 7]], scope=scope)["ok"])
        for key in ("/dev/zero", "/dev/nullx", "/dev/null2", "/dev/pts/0", "/dev/shm/x", "/dev/tty"):
            with self.subTest(key=key):
                self.assertEqual(rules_of(cover(outside=[[key, ["write"], 1]], scope=scope)), ["C4-write-class"])
        self.assertTrue(cover(outside=[["/dev/zero", ["read"], 1], ["/dev/urandom", ["read", "stat"], 1]], scope=scope)["ok"], "读照旧放行")

    def test_only_the_write_is_forgiven(self) -> None:
        scope = self.scope()
        for kinds, rule in ((["exec"], "C2-kinds"), (["write", "exec"], "C2-kinds"), (["write", "exec", "read"], "C2-kinds")):
            with self.subTest(kinds=kinds):
                self.assertEqual(rules_of(cover(outside=[["/dev/null", kinds, 1]], scope=scope)), [rule])

    def test_a_declared_prefix_is_still_needed_and_a_specific_scratch_row_still_works(self) -> None:
        bare = bare_scope()
        self.assertEqual(rules_of(cover(outside=[["/dev/null", ["write"], 1]], scope=bare)), ["C2-outside-undeclared"], "仍然要有声明前缀")
        with_row = self.scope({"prefix": "/dev/null", "class": "scratch"})
        self.assertTrue(cover(outside=[["/dev/null", ["read", "stat", "write"], 1]], scope=with_row)["ok"], "草案里补一行 scratch 也行（最长前缀，scratch 本来就可写）")
        self.assertEqual(rules_of(cover(outside=[["/dev/zero", ["write"], 1]], scope=with_row)), ["C4-write-class"])


class RealisticScenarioTests(unittest.TestCase):
    """把真实采样里出现过的几类未覆盖项原样搬进来（路径取自 ARM64 的四份真实证据），整体过一遍。"""

    def test_the_uncovered_classes_found_in_the_real_samples_are_now_covered_or_still_precisely_uncovered(self) -> None:
        aliases = {"TREE": "/var/tmp/probe-run/test-tree", "WORK": "/var/tmp/probe-run", "HOME": "/root", "TMP": "/tmp"}
        scope = bare_scope(root_listing=True)
        covered = [
            ["/usr/", ["missing", "stat"], 40], ["/usr/aarch64-linux-gnu/bin/", ["missing"], 2], ["/usr/local/include/bits/types/", ["missing"], 1],
            ["/usr/local/bin/", ["missing"], 1], ["/opt/", ["missing", "stat"], 3], ["/node_modules/node:fs/", ["missing"], 1],
            ["/var/node_modules/", ["missing"], 1], ["/var/tmp/node_modules/.vue-global-types/", ["missing"], 1],
            ["/work/frontend/node_modules/.pnpm/vitest@2.1.9/node_modules/vitest/", ["missing"], 1],
            ["/VAR/TMP/PROBE-RUN/TEST-TREE/FRONTEND/NODE_MODULES/.PNPM/TYPESCRIPT@5.6.3/LIB/", ["missing"], 1], ["{WORK}/node_modules/", ["missing"], 1],
            # WP1e：祖先键下不再带 read（WP4c 之后采样器把对祖先目录自身的 read 记成 dir；最终真实样本里只有 egress 两个单元的 {WORK}/ 键含 read）。
            ["/", ["dir", "list", "missing", "stat"], 30], ["/var/", ["dir", "list", "missing", "stat"], 12], ["/var/tmp/", ["list", "missing"], 6],
            ["{WORK}", ["read", "stat"], 5], ["{WORK}/", ["list", "missing", "read"], 4],
        ]
        repo = [["{TREE}", ["dir", "list", "read", "stat"], False], ["{TREE}/RCS", ["missing"], False], ["{TREE}/SCCS", ["missing"], False],
                ["{TREE}/.browserslistrc", ["missing"], False], ["{TREE}/eslint.config.mjs", ["missing"], False], ["{TREE}/node_modules", ["missing"], False]]
        result = cover(repo=repo, outside=covered, aliases=aliases, scope=scope)
        self.assertTrue(result["ok"], result["undeclared"])
        still = [["{TREE}/node_modules/@types", ["missing"], False], ["{TREE}/src/__tests__/setup.ts.cjs", ["missing"], False],
                 ["{TREE}/docs/legal", ["stat"], False], ["{TREE}/docs/legal/a.en.md?raw.ts", ["missing"], False]]
        result = cover(repo=still, outside=[["/run/systemd/resolve/", ["read"], 1]], aliases=aliases, scope=scope)
        self.assertEqual([(item["rule"], item["path"]) for item in result["undeclared"]],
                         [("C1-undeclared", "{TREE}/docs/legal"), ("C2-outside-undeclared", "/run/systemd/resolve/"),
                          ("C3-missing-undeclared", "{TREE}/docs/legal/a.en.md?raw.ts"), ("C3-missing-undeclared", "{TREE}/node_modules/@types"),
                          ("C3-missing-undeclared", "{TREE}/src/__tests__/setup.ts.cjs")],
                         "仍然要补的：树根以下的仓库内 missing、目录 stat（draft 补 range），以及读已存在的非祖先目录（声明 system 前缀）")


if __name__ == "__main__":
    unittest.main()
