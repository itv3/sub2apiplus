"""门禁单元按客户端归属与按改动选择（Codex 升级工具优化第 9 项）：``tools/ci/unit_ownership.py`` 与数据文件 ``unit_ownership.json``。

两部分：

* 真实仓库的守卫（CI 里也跑）：数据文件与现推结果一致、覆盖全部测试模块、廉价阈值与计划状态自洽、被剔除的模块里没有访问真实 ARM64
  环境的路径字面量、Codex 计划与全集的差集每个模块都带理由；
* 合成仓库上的选择器行为（验收口径）：改一个共享文件，计划自动带上依赖它的 Claude 单元；改枢纽文件只提示不并入；联动文件只并入直接验证它们的模块；
  失败关闭各项；归属表自己在改动里就不筛选；摘要自检。
"""

from __future__ import annotations

import fnmatch
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.ci import unit_ownership as uo
from tools.official_client_capture.tests.unit_ownership_world import (
    CALIBRATION, CAPTURE, CHEAP, HEAVY, HEAVY_MODULES, TESTS, WORLD, World, base_data, git, pulled, skipped)

REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------------------------- 真实仓库的守卫


class RealTreeGuardTests(unittest.TestCase):
    """数据文件、现推结果与真实测试树必须一致（改规则或新增测试模块后要重新物化：``python3 -B tools/ci/unit_ownership.py materialize``）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = uo.Graph(REPO_ROOT)
        cls.data = uo.load_data(REPO_ROOT)

    def test_the_data_file_matches_the_derivation_and_covers_every_test_module(self) -> None:
        self.assertEqual(uo.check(REPO_ROOT, graph=self.graph), [])

    def test_only_codex_is_enabled_in_this_version(self) -> None:
        self.assertEqual(self.data["enabled_clients"], ["codex"])

    def test_every_removed_module_is_claude_owned_heavy_and_unpinned_with_a_written_reason(self) -> None:
        rows = uo.report(REPO_ROOT, "codex")
        removed = [row for row in rows if row["plan"] == uo.SKIP]
        self.assertTrue(removed, "Codex 计划必须有可剔除的 Claude 重模块，否则本项没有收益")
        for row in removed:
            with self.subTest(module=row["module"]):
                self.assertEqual(row["owner"], "claude")
                self.assertGreaterEqual(row["core_seconds"], self.data["cheap_core_seconds"])
                self.assertNotIn(row["module"], self.data["pinned"])
                self.assertIn("核·秒", row["reason"])
                self.assertIn("ARM64", row["reason"])
        kept = [row for row in rows if row["plan"] == uo.RUN]
        for row in kept:
            self.assertTrue(row["reason"], row["module"])
        self.assertEqual(len(removed) + len(kept), len(self.data["modules"]))

    def test_cheap_claude_modules_and_uncalibrated_modules_always_run(self) -> None:
        cheap = self.data["cheap_core_seconds"]
        for module, row in self.data["modules"].items():
            if row["owner"] == "claude" and (row["core_seconds"] is None or row["core_seconds"] < cheap):
                self.assertEqual(row["codex_plan"], uo.RUN, module)
            if row["owner"] != "claude":
                self.assertEqual(row["codex_plan"], uo.RUN, module)

    def test_removed_modules_do_not_touch_the_real_arm64_install_or_data_root(self) -> None:
        self.assertEqual(uo.hermetic_problems(REPO_ROOT, self.data, self.graph), [])

    def test_a_new_arm64_literal_in_a_removed_module_is_caught_unless_reviewed(self) -> None:
        heavy = next(module for module, row in self.data["modules"].items() if row["codex_plan"] == uo.SKIP
                     and module not in self.data["reviewed_hermetic"])
        rel = f"{TESTS}/{heavy}.py"
        graph = uo.Graph.__new__(uo.Graph)
        graph.__dict__.update(self.graph.__dict__)
        graph.text = {**self.graph.text, rel: self.graph.text[rel] + '\nROOT = "/root/docker/capture-cli/data"\n'}
        problems = uo.hermetic_problems(REPO_ROOT, self.data, graph)
        self.assertTrue(any(heavy in problem and "/root/docker/capture-cli/data" in problem for problem in problems), problems)
        reviewed = {**self.data, "reviewed_hermetic": {**self.data["reviewed_hermetic"], heavy: "人工复核：只是配置字面量"}}
        self.assertEqual(uo.hermetic_problems(REPO_ROOT, reviewed, graph), [])

    def test_pinned_modules_are_never_removed_even_when_they_are_heavy_claude_modules(self) -> None:
        row = {"owner": "claude", "rule": "name:claude", "core_seconds": 999.0}
        self.assertEqual(uo.plan_state(row, "codex", cheap=30, pinned=False), uo.SKIP)
        self.assertEqual(uo.plan_state(row, "codex", cheap=30, pinned=True), uo.RUN)
        self.assertEqual(uo.plan_state(row, "claude", cheap=30, pinned=False), uo.RUN)
        self.assertEqual(uo.plan_state({**row, "core_seconds": 29.9}, "codex", cheap=30, pinned=False), uo.RUN, "低于阈值：廉价保留")
        self.assertEqual(uo.plan_state({**row, "core_seconds": None}, "codex", cheap=30, pinned=False), uo.RUN, "没标定：保守照跑")

    def test_hubs_and_the_shared_kernel_are_the_documented_policy_inputs(self) -> None:
        for path in ("tools/official_client_capture/codex_upgrade.py", "tools/official_client_capture/codex_upgrade_reconciler.py"):
            self.assertIn(path, self.data["hubs"])
        for path in ("tools/official_client_capture/capturelib/model.py", "tools/official_client_capture/capturelib/security.py"):
            self.assertIn(path, self.data["shared_kernel"])
        self.assertFalse(set(self.data["hubs"]) & set(self.data["shared_kernel"]))

    def test_the_ownership_table_and_selector_are_listed_as_self_paths(self) -> None:
        self.assertIn(uo.DATA_PATH, uo.SELF_PATHS)
        self.assertIn("tools/ci/unit_ownership.py", uo.SELF_PATHS)
        self.assertIn("tools/arm64_capture_driver/driver/unit_ownership.py", uo.SELF_PATHS)

    def test_the_executor_version_files_and_the_scheduling_policy_are_self_paths_too(self) -> None:
        """修好接着跑的第二层：执行器自己（14 个版本文件，含驱动副本）与调度策略文件在改动里，选择器整轮不筛选。清单必须与执行器真实记录的版本文件、
        调度策略文件逐个对得上——执行器以后增减版本文件，这里先红，免得自身路径集悄悄过期。"""

        from tools.ci import unit_records as ur

        repo = Path(__file__).resolve().parents[3]
        self.assertEqual(set(uo.EXECUTOR_VERSION_FILES), set(ur.EXECUTOR_FILES) | {"bytecode_cache.py", "retry_policy.py", "go_test_packages.py", "declared_sampler.py"})
        self.assertEqual(len(uo.EXECUTOR_VERSION_FILES), 14)
        for name in uo.EXECUTOR_VERSION_FILES:
            self.assertTrue((repo / "tools/ci" / name).is_file() or (repo / "tools/arm64_capture_driver/driver" / name).is_file(), name)
            self.assertIn(f"tools/arm64_capture_driver/driver/{name}", uo.EXECUTOR_SELF_PATHS)
        for name in uo.SCHEDULING_POLICY_FILES:
            self.assertTrue((repo / "tools/ci" / name).is_file(), name)
            self.assertIn(f"tools/ci/{name}", uo.EXECUTOR_SELF_PATHS)
        self.assertEqual(set(uo.SELF_PATHS) & set(uo.EXECUTOR_SELF_PATHS), set(), "归属表与选择器的自身路径另有理由文字，两个集合不重叠")

    def test_the_coupled_table_is_the_eleven_modules_approved_for_the_registration_anchors(self) -> None:
        # 拍板 4：Claude 联动文件（场景清单、期望画像、冻结摘要）被改时，只并入直接验证它们的这 11 个单元；改这张表要在评审里显式看到。
        # 验收修正补进 test_claude_upgrade_scenarios（它用 glob("claude_upgrade_scenarios_*.json") 核对每份场景清单的 tool_dependencies）。
        [entry] = self.data["coupled"]
        self.assertEqual(sorted(entry["modules"]), [
            "test_claude_build_rule_assertion_results", "test_claude_candidate_draft_projection", "test_claude_recorded_candidate_source",
            "test_claude_recorded_full_validation", "test_claude_rule_assertion", "test_claude_rule_expectations_2_1_226",
            "test_claude_upgrade_boundary_codex", "test_claude_upgrade_candidate_seal_evidence", "test_claude_upgrade_compare_accept",
            "test_claude_upgrade_scenarios", "test_claude_vc5_chain_fixture"])
        self.assertEqual(entry["ignore_assignments"], ["FROZEN_PROFILE_SHA256S"])
        self.assertEqual(len(entry["patterns"]), 3)


# --------------------------------------------------------------------------------------------- 合成仓库上的选择器行为


class SelectionTests(unittest.TestCase):
    def test_a_change_to_the_executor_or_its_scheduling_policy_makes_the_selector_fall_back_to_the_whole_set(self) -> None:
        """验收 V4 的反例：只在 unit_executor.py（及驱动副本）末尾加一行注释，原来 scope=client-plan、没有回退；现在整轮全集并写明原因。"""

        paths = ("tools/ci/unit_executor.py", "tools/arm64_capture_driver/driver/unit_executor.py", "tools/ci/unit_records.py",
                 "tools/arm64_capture_driver/driver/entry-gates.sh", "tools/arm64_capture_driver/driver/bytecode_cache.py", "tools/ci/entry_gates.py",
                 "tools/ci/unit_executor.json", "tools/ci/capture_test_weights.json", "tools/ci/capture_test_durations.json",
                 "tools/arm64_capture_driver/driver/unit_executor.json")
        for path in paths:
            with self.subTest(path=path):
                world = World(self)
                selection = world.select((path, "# 只多一行注释\n" if path.endswith((".py", ".sh")) else "{}\n"))
                self.assertEqual(selection["scope"], uo.SCOPE_FULL)
                self.assertEqual(set(selection["modules"].values()), {uo.RUN}, "整轮不筛选")
                self.assertEqual(selection["fallback_reasons"], [f"执行器版本文件或调度策略文件在改动里：{path}"])
        world = World(self)
        quiet = world.select(("tools/ci/helper.py", "X = 2\n"))
        self.assertEqual((quiet["scope"], quiet["fallback_reasons"]), (uo.SCOPE_CLIENT_PLAN, []), "执行器之外的 tools/ci 文件照常筛选")

    def test_the_synthetic_world_classifies_as_designed(self) -> None:
        world = World(self)
        modules = world.data["modules"]
        self.assertEqual({name: row["codex_plan"] for name, row in modules.items()},
                         {"test_claude_heavy_a": "skip", "test_claude_heavy_b": "skip", "test_claude_heavy_c": "skip", "test_claude_heavy_d": "skip",
                          "test_claude_cheap": "run", "test_codex_thing": "run", "test_ci_shared_thing": "run"})
        self.assertEqual(world.data["hubs"], [f"{CAPTURE}/codex_hub.py"], "Codex 命名却被 Claude 面文件导入")
        self.assertIn(f"{CAPTURE}/kernel_util.py", world.data["shared_kernel"])
        self.assertEqual(uo.check(world.root), [])

    def test_no_change_skips_every_heavy_claude_module_and_keeps_the_rest(self) -> None:
        selection = World(self).select()
        self.assertEqual(selection["scope"], uo.SCOPE_CLIENT_PLAN)
        self.assertEqual(skipped(selection), HEAVY_MODULES)
        self.assertEqual(selection["pulled"], [])
        self.assertEqual(selection["changed_files_count"], 0)
        self.assertTrue(uo.seal_ok(selection))
        self.assertEqual(selection["client"], "codex")

    def test_changing_a_shared_kernel_file_pulls_the_claude_modules_that_import_it(self) -> None:
        # 验收口径：人为改一个共享文件，计划自动带上依赖它的 Claude 单元（直接导入、或经一层夹具导入）。
        world = World(self)
        selection = world.select((f"{CAPTURE}/kernel_util.py", "VALUE = 2\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_a"])
        self.assertEqual(pulled(selection)["test_claude_heavy_a"], [f"{CAPTURE}/kernel_util.py"])
        self.assertEqual(skipped(selection), ["test_claude_heavy_b", "test_claude_heavy_c", "test_claude_heavy_d"])

    def test_one_layer_of_fixtures_counts_but_two_layers_do_not(self) -> None:
        world = World(self)
        selection = world.select((f"{CAPTURE}/fixture_dep.py", "from tools.official_client_capture import far_dep\nX = 2\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_b"], "夹具直接导入 fixture_dep：并入")
        world = World(self)
        selection = world.select((f"{CAPTURE}/far_dep.py", "FAR = 2\n"))
        self.assertEqual(pulled(selection), {}, "far_dep 在两层以外：设计的 1 跳口径不拉（收尾全集与 GitHub CI 兜底）")

    def test_a_changed_test_module_pulls_itself_and_a_changed_fixture_pulls_its_importers(self) -> None:
        world = World(self)
        selection = world.select((f"{TESTS}/test_claude_heavy_c.py", "from tools.official_client_capture import claude_deep\nY = 1\n"),
                                 (f"{TESTS}/claude_fixture.py", "from tools.official_client_capture import fixture_dep\nZ = 1\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_b", "test_claude_heavy_c"])

    def test_a_changed_hub_file_is_only_advised_and_never_merged(self) -> None:
        # 拍板 1：枢纽文件被改，只在计划里提示，不自动并入 Claude 重单元。
        world = World(self)
        hub = f"{CAPTURE}/codex_hub.py"
        selection = world.select((hub, "HUB = 2\n"))
        self.assertEqual(selection["pulled"], [])
        self.assertEqual(skipped(selection), HEAVY_MODULES, "枢纽被改，重模块照样全部剔除")
        self.assertEqual([item["file"] for item in selection["advisories"]], [hub], "即使没有重模块直接导入，枢纽被改也写进提示")
        advisory = selection["advisories"][0]
        self.assertEqual((advisory["claude_modules"], advisory["core_seconds"], advisory["modules"]), (0, 0.0, []))

    def test_a_hub_that_a_heavy_module_imports_directly_shows_up_in_the_advisory_with_core_seconds(self) -> None:
        files = {**WORLD, f"{TESTS}/test_claude_heavy_a.py": WORLD[f"{TESTS}/test_claude_heavy_a.py"] + "from tools.official_client_capture import codex_hub\n"}
        world = World(self, files)
        selection = world.select((f"{CAPTURE}/codex_hub.py", "HUB = 3\n"))
        self.assertEqual(selection["advisories"][0]["modules"], ["test_claude_heavy_a"])
        self.assertEqual(selection["advisories"][0]["core_seconds"], HEAVY)
        self.assertEqual(selection["pulled"], [])

    def test_a_claude_face_change_follows_the_claude_import_chain(self) -> None:
        world = World(self)
        selection = world.select((f"{CAPTURE}/claude_lib.py", "from tools.official_client_capture import codex_hub, kernel_util\nCHANGED = 1\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_c"], "heavy_c 经 claude_deep→claude_lib 间接依赖，Claude 面改动沿链传递")

    def test_coupled_files_pull_only_the_modules_that_verify_them(self) -> None:
        # 拍板 4：联动文件只并入直接验证它们的模块。
        world = World(self)
        selection = world.select((f"{CAPTURE}/claude_upgrade_scenarios_1_0_0.json", '{"tools": ["new_script.sh"]}\n'),
                                 (f"{CAPTURE}/claude_rule_assertion.py", 'FROZEN_PROFILE_SHA256S: dict[str, str] = {"1": "b", "2": "c"}\nVALUE = 1\n'))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_d"])
        self.assertEqual(skipped(selection), ["test_claude_heavy_a", "test_claude_heavy_b", "test_claude_heavy_c"])

    def test_a_coupled_python_file_changed_in_other_ways_falls_back_to_the_normal_rules(self) -> None:
        world = World(self)
        selection = world.select((f"{CAPTURE}/claude_rule_assertion.py", 'FROZEN_PROFILE_SHA256S: dict[str, str] = {"1": "a"}\nVALUE = 2\n'))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_d"], "非联动改动按 Claude 面规则：d 直接导入它")

    def test_the_ownership_table_or_selector_in_the_changes_means_no_filtering(self) -> None:
        world = World(self)
        world.data["cheap_core_seconds"] = 31
        world.write_data()
        selection = world.select()
        self.assertEqual(selection["scope"], uo.SCOPE_FULL)
        self.assertEqual(skipped(selection), [])
        self.assertTrue(selection["fallback_reasons"])
        self.assertTrue(all(state == uo.RUN for state in selection["modules"].values()))
        self.assertTrue(uo.seal_ok(selection))

    def test_an_unrecognized_path_fails_closed_and_pulls_every_heavy_module(self) -> None:
        selection = World(self).select(("brand_new_top_level/file.txt", "x\n"))
        self.assertEqual(skipped(selection), [])
        self.assertEqual(selection["fail_closed"], [{"reason": "改动路径不在已登记的目录范围", "file": "brand_new_top_level/file.txt"}])
        for because in pulled(selection).values():
            self.assertTrue(because[0].startswith("失败关闭："))

    def test_a_python_file_that_fails_to_parse_fails_closed(self) -> None:
        selection = World(self).select(("tools/ci/helper.py", "def broken(:\n"))
        self.assertEqual(skipped(selection), [])
        self.assertEqual(selection["fail_closed"][0]["file"], "tools/ci/helper.py")

    def test_documents_are_matched_by_the_path_literal_index_of_the_heavy_modules(self) -> None:
        world = World(self)
        selection = world.select(("docs/guide.md", "# changed\n"), ("docs/other.md", "# changed\n"), ("backend/main.go", "package main\n// c\n"))
        self.assertEqual(pulled(selection), {"test_claude_heavy_a": ["docs/guide.md"]}, "只有引用了 docs/guide.md 的模块被拉入")
        self.assertEqual(selection["fail_closed"], [])

    def test_generic_directory_references_do_not_count(self) -> None:
        files = {**WORLD, f"{TESTS}/test_claude_heavy_b.py": WORLD[f"{TESTS}/test_claude_heavy_b.py"] + "ROOT = Path('.') / 'tools'\nD = 'docs/egress'\n"}
        world = World(self, files)
        selection = world.select(("docs/egress/receipt.json", "{}\n"), ("tools/ci/helper.py", "X = 2\n"))
        self.assertEqual(pulled(selection), {}, "单独的 tools、被登记为泛目录的 docs/egress 不计入路径索引")

    def test_a_data_file_in_the_managed_tree_follows_its_reader(self) -> None:
        world = World(self)
        # shared_data.json 被重模块 a 的源码提到：并入 a；loose_data.json 没有任何读取者：失败关闭
        selection = world.select((f"{CAPTURE}/shared_data.json", '{"a": 1}\n'))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_a"])
        world = World(self)
        selection = world.select((f"{CAPTURE}/loose_data.json", '{"a": 1}\n'))
        self.assertEqual(skipped(selection), [])
        self.assertEqual(selection["fail_closed"][0]["file"], f"{CAPTURE}/loose_data.json")

    def test_codex_named_data_without_a_reader_is_not_a_reason_to_fail_closed(self) -> None:
        selection = World(self).select((f"{CAPTURE}/codex_orphan_1_2_3.json", '{"a": 1}\n'))
        self.assertEqual(selection["fail_closed"], [])
        self.assertEqual(skipped(selection), HEAVY_MODULES)

    def test_a_data_file_read_by_a_hub_is_only_advised(self) -> None:
        files = {**WORLD, f"{CAPTURE}/codex_hub.py": "NAME = 'hub_data.json'\n"}
        world = World(self, files)
        selection = world.select((f"{CAPTURE}/hub_data.json", '{"a": 1}\n'))
        self.assertEqual(selection["pulled"], [])
        self.assertEqual([item["file"] for item in selection["advisories"]], [f"{CAPTURE}/hub_data.json"])

    def test_deleting_a_python_file_that_a_heavy_module_still_mentions_pulls_that_module(self) -> None:
        world = World(self)
        selection = world.select((f"{CAPTURE}/far_dep.py", None))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_b"], "夹具闭包的文本里仍提到 far_dep")

    def test_the_manual_switch_pulls_every_heavy_module_but_stays_a_client_plan(self) -> None:
        selection = World(self).select(include_claude=True)
        self.assertEqual(selection["scope"], uo.SCOPE_CLIENT_PLAN)
        self.assertTrue(selection["include_claude"])
        self.assertEqual(skipped(selection), [])
        self.assertEqual(sorted(pulled(selection)), HEAVY_MODULES)

    def test_a_test_module_missing_from_the_table_runs_and_is_reported(self) -> None:
        world = World(self)
        selection = world.select((f"{TESTS}/test_claude_brand_new.py", "X = 1\n"))
        self.assertEqual(selection["unclassified"], ["test_claude_brand_new"])
        self.assertEqual(selection["modules"]["test_claude_brand_new"], uo.RUN)
        self.assertTrue(uo.check(world.root), "守卫要求登记：数据文件没有覆盖新模块")

    def test_a_test_module_deleted_from_the_tree_is_reported_as_a_stale_table_row(self) -> None:
        world = World(self)
        selection = world.select((f"{TESTS}/test_claude_cheap.py", None))
        self.assertEqual(selection["stale_modules"], ["test_claude_cheap"])
        self.assertNotIn("test_claude_cheap", selection["modules"])
        self.assertEqual(World(self).select()["stale_modules"], [])

    def test_the_latest_release_tag_is_the_default_base(self) -> None:
        world = World(self)
        with self.assertRaisesRegex(uo.OwnershipError, "找不到最近的发版 tag"):
            uo.latest_release_base(world.root)
        git(world.root, "tag", "v0.2.17", world.base)
        world.commit("第二个提交")
        git(world.root, "tag", "v0.2.18-1")
        git(world.root, "tag", "vendor-note")
        world.commit("候选")
        commit, name = uo.latest_release_base(world.root)
        self.assertEqual(name, "v0.2.18-1")
        self.assertEqual(commit, git(world.root, "rev-parse", "v0.2.18-1^{commit}"))
        git(world.root, "tag", "-d", "v0.2.18-1")
        self.assertEqual(uo.latest_release_base(world.root), (world.base, "v0.2.17"), "最近的是更早的那个发版 tag")
        git(world.root, "tag", "-d", "v0.2.17")
        git(world.root, "tag", "v7", world.base)
        with self.assertRaisesRegex(uo.OwnershipError, "找不到最近的发版 tag"):
            uo.latest_release_base(world.root)   # v7 不是发版格式（vX.Y.Z[-N]）

    def test_the_selection_digest_detects_edits(self) -> None:
        selection = World(self).select()
        self.assertTrue(uo.seal_ok(selection))
        for mutate in (lambda s: s["modules"].__setitem__("test_claude_heavy_a", uo.RUN), lambda s: s.__setitem__("client", "claude")):
            tampered = json.loads(json.dumps(selection))
            mutate(tampered)
            self.assertFalse(uo.seal_ok(tampered))

    def test_the_selection_is_bound_to_the_ownership_data_and_the_selector(self) -> None:
        world = World(self)
        selection = world.select()
        self.assertEqual(selection["ownership_sha256"], uo.sha256_file(world.root / uo.DATA_PATH))
        self.assertEqual(selection["selector_sha256"], uo.sha256_file(Path(uo.__file__)))
        self.assertEqual(selection["base_commit"], world.base)
        self.assertEqual(selection["head_commit"], git(world.root, "rev-parse", "HEAD"))

    def test_a_dirty_tree_an_unrelated_base_and_a_disabled_client_are_refused(self) -> None:
        world = World(self)
        world.commit("候选")
        world.write("Makefile", "dirty:\n")
        with self.assertRaisesRegex(uo.OwnershipError, "不干净"):
            uo.select(world.root, client="codex", base=world.base)
        git(world.root, "checkout", "--", "Makefile")
        with self.assertRaisesRegex(uo.OwnershipError, "40 位"):
            uo.select(world.root, client="codex", base="HEAD~1")
        with self.assertRaisesRegex(uo.OwnershipError, "不在测试树的历史里"):
            uo.select(world.root, client="codex", base="0" * 40)
        with self.assertRaisesRegex(uo.OwnershipError, "只启用"):
            uo.select(world.root, client="claude", base=world.base)
        git(world.root, "checkout", "-q", "--orphan", "other")
        git(world.root, "commit", "-q", "--allow-empty", "-m", "孤立提交")
        with self.assertRaisesRegex(uo.OwnershipError, "不是候选提交"):
            uo.select(world.root, client="codex", base=world.base)

    def test_a_head_that_is_not_the_tree_head_is_refused(self) -> None:
        world = World(self)
        world.commit("候选")
        with self.assertRaisesRegex(uo.OwnershipError, "不是测试树当前 HEAD"):
            uo.select(world.root, client="codex", base=world.base, head="1" * 40)


class DriverScriptsLoadedByNameTests(unittest.TestCase):
    """验收修正：驱动目录里只在驱动存在的脚本，被测试用 ``load_script("名字")``／``load_driver("名字")`` 按名字加载，导入图原来没有这条边。"""

    DRIVER = "tools/arm64_capture_driver/driver"
    SCRIPTS = ("vc4_resume", "implementation_gates", "claude_catalog_chain", "phase_context", "round_context")

    def world(self, extra: dict[str, str] | None = None, heavy: tuple[str, ...] = ()) -> World:
        files = {
            **WORLD,
            f"{self.DRIVER}/vc4_resume.py": "RESUME = 1\n",
            f"{self.DRIVER}/implementation_gates.py": "GATES = 1\n",
            f"{self.DRIVER}/claude_catalog_chain.py": "CHAIN = 1\n",
            f"{self.DRIVER}/round_context.py": "ROUND = 1\n",
            # phase_context 导入同族的 round_context（驱动脚本族内部的导入）
            f"{self.DRIVER}/phase_context.py": "from tools.arm64_capture_driver.driver import round_context\nPHASE = 1\n",
            # 重模块按名字加载（字符串字面量整串等于脚本 stem），没有任何 import 语句
            f"{TESTS}/test_claude_vc4_driver_x.py": ("def load_driver(name):\n    return name\n\n"
                                                    "RESUME = load_driver('vc4_resume')\nGATES = load_driver(\"implementation_gates\")\n"
                                                    "CHAIN = load_driver('claude_catalog_chain')\n"),
            f"{TESTS}/test_claude_vc5_driver_x.py": "def load_script(name):\n    return name\n\nPHASE = load_script('phase_context')\n",
            **(extra or {}),
        }
        calibration = {**CALIBRATION, "test_claude_vc4_driver_x": HEAVY, "test_claude_vc5_driver_x": HEAVY, **{name: HEAVY for name in heavy}}
        return World(self, files, calibration)

    def test_a_string_literal_equal_to_a_script_stem_is_an_import_edge(self) -> None:
        world = self.world()
        graph = uo.Graph(world.root)
        edges = graph.imports[f"{TESTS}/test_claude_vc4_driver_x.py"]
        for name in ("vc4_resume", "implementation_gates", "claude_catalog_chain"):
            self.assertIn(f"{self.DRIVER}/{name}.py", edges)
        self.assertNotIn(f"{self.DRIVER}/phase_context.py", edges)

    def test_changing_each_driver_script_pulls_the_driver_module_that_loads_it(self) -> None:
        expected = {
            "vc4_resume": ["test_claude_vc4_driver_x"], "implementation_gates": ["test_claude_vc4_driver_x"],
            "claude_catalog_chain": ["test_claude_vc4_driver_x"], "phase_context": ["test_claude_vc5_driver_x"],
            # round_context 只被同族的 phase_context 导入：加载 phase_context 的测试同样在测它（驱动脚本族内部的传递）
            "round_context": ["test_claude_vc5_driver_x"],
        }
        for name, modules in expected.items():
            with self.subTest(script=name):
                selection = self.world().select((f"{self.DRIVER}/{name}.py", "CHANGED = 2\n"))
                self.assertEqual(sorted(pulled(selection)), modules)
                self.assertEqual(selection["fail_closed"], [])
                # 其余重模块照样剔除
                self.assertIn("test_claude_heavy_a", skipped(selection))

    def test_the_family_transitivity_stays_inside_the_driver_directory(self) -> None:
        # 重模块只导入一个 Claude 面文件、它再导入 phase_context：不沿 Claude 面文件链把驱动脚本拉进来（否则几乎所有重模块都被拉入）。
        extra = {f"{CAPTURE}/claude_bridge.py": "from tools.arm64_capture_driver.driver import phase_context\n",
                 f"{TESTS}/test_claude_bridge_x.py": "from tools.official_client_capture import claude_bridge\n"}
        world = self.world(extra, heavy=("test_claude_bridge_x",))
        selection = world.select((f"{self.DRIVER}/phase_context.py", "CHANGED = 3\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_vc5_driver_x"])

    def test_a_literal_that_is_not_a_script_stem_is_not_an_edge(self) -> None:
        extra = {f"{TESTS}/test_claude_vc5_driver_x.py": "NOTE = 'phase_context_is_documented_elsewhere'\nOTHER = 'round'\n"}
        selection = self.world(extra).select((f"{self.DRIVER}/phase_context.py", "CHANGED = 4\n"))
        self.assertEqual(pulled(selection), {})


class UnresolvedDynamicLoadTests(unittest.TestCase):
    """验收修正：重模块可达的文件里有解析不了目标的动态加载，而改动的 Python 文件没有任何静态使用者时失败关闭——并入这些重模块；
    有静态使用者的文件按导入边处理，解析得了的动态加载不受影响。"""

    LOADER = (
        "import importlib\n\n"
        "def run(config):\n    return importlib.import_module(config['module'])\n")
    ORPHAN = "tools/ci/brand_new_tool.py"

    def world(self, text: str, extra: dict[str, str] | None = None) -> World:
        files = {**WORLD, f"{TESTS}/test_claude_dynamic_e.py": text, **(extra or {})}
        return World(self, files, {**CALIBRATION, "test_claude_dynamic_e": HEAVY})

    def test_a_python_file_nobody_uses_pulls_the_module_with_an_unresolvable_dynamic_load(self) -> None:
        world = self.world(self.LOADER)
        graph = uo.Graph(world.root)
        self.assertEqual(sorted(graph.unresolved_dynamic), [f"{TESTS}/test_claude_dynamic_e.py"])
        selection = world.select((self.ORPHAN, "X = 1\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_dynamic_e"])
        self.assertIn("失败关闭：含解析不了的动态加载", pulled(selection)["test_claude_dynamic_e"][0])
        self.assertEqual([item["file"] for item in selection["fail_closed"]], [self.ORPHAN])
        self.assertIn("test_claude_dynamic_e", selection["fail_closed"][0]["reason"])
        self.assertEqual(skipped(selection), HEAVY_MODULES, "其余重模块照样剔除")

    def test_a_python_file_somebody_imports_or_names_is_handled_by_its_edges_instead(self) -> None:
        # kernel_util 被别的文件导入（有入边）；helper 没有入边，但另一个文件的文本里提到了它的名字。
        world = self.world(self.LOADER, {f"{CAPTURE}/mentions_helper.py": "NOTE = 'see helper for the details'\n"})
        selection = world.select((f"{CAPTURE}/kernel_util.py", "VALUE = 2\n"), ("tools/ci/helper.py", "X = 2\n"))
        self.assertNotIn("test_claude_dynamic_e", pulled(selection))
        self.assertEqual(selection["fail_closed"], [])

    def test_a_change_without_python_does_not_trigger_it(self) -> None:
        selection = self.world(self.LOADER).select(("docs/other.md", "# other changed\n"))
        self.assertEqual(pulled(selection), {})
        self.assertEqual(selection["fail_closed"], [])

    def test_a_deleted_python_file_is_not_an_unused_file(self) -> None:
        selection = self.world(self.LOADER, {self.ORPHAN: "X = 1\n"}).select((self.ORPHAN, None))
        self.assertNotIn("test_claude_dynamic_e", pulled(selection), "删除的文件走已有的“仍被提到”规则，不在这条里")

    def test_the_failure_is_only_for_modules_that_can_reach_the_site(self) -> None:
        # 动态加载点在夹具里，只有导入这个夹具的重模块 e 被拉入；没有这条可达路径的 a～d 不被拉入。
        extra = {f"{TESTS}/dynamic_fixture.py": self.LOADER}
        world = self.world("from tools.official_client_capture.tests import dynamic_fixture\n", extra)
        selection = world.select((self.ORPHAN, "X = 3\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_dynamic_e"])

    def test_resolvable_dynamic_loads_are_not_a_reason_to_fail_closed(self) -> None:
        resolvable = {
            "字面量目标": "import importlib\nMODULE = importlib.import_module('tools.official_client_capture.codex_only')\n",
            "参数转发的加载器": ("import importlib.util\n\ndef load(name):\n"
                          "    return importlib.util.spec_from_file_location(name, 'x/' + name + '.py')\n"),
            "绑定过字面量的名字": ("import importlib.util\nHELPER = 'tools/ci/helper.py'\n"
                           "SPEC = importlib.util.spec_from_file_location('helper', HELPER)\n"),
            "标准库的字面量": "MODULE = __import__('hashlib')\n",
        }
        for label, text in resolvable.items():
            with self.subTest(label):
                world = self.world(text)
                self.assertEqual(uo.Graph(world.root).unresolved_dynamic, {})
                selection = world.select((self.ORPHAN, "X = 4\n"))
                self.assertEqual(selection["fail_closed"], [])
                self.assertEqual(pulled(selection), {})

    def test_a_value_taken_from_a_table_is_not_a_resolved_target(self) -> None:
        # 参数转发看起来像"解析得了"，但从数据表里取出来的名字（下标、.get）不是调用者传的字面量。
        for label, text in {
                "下标": "import importlib\n\ndef run(row):\n    return importlib.import_module(row['orchestrator_module'])\n",
                ".get": "import importlib\n\ndef run(row):\n    return importlib.import_module(row.get('orchestrator_module'))\n",
                "运行时拼出": "import importlib\n\ndef run():\n    return importlib.import_module(compute_name())\n"}.items():
            with self.subTest(label):
                self.assertEqual(sorted(uo.Graph(self.world(text).root).unresolved_dynamic), [f"{TESTS}/test_claude_dynamic_e.py"])

    def test_the_real_tree_exposure_is_the_two_reviewed_table_driven_loads(self) -> None:
        # 现状核对：真实树里重模块可达的、解析不了目标的动态加载点只有这两处（都是按客户端表的行加载编排器／账本模块）。以后谁再加一个，这条会红，
        # 提醒评审看清楚它加载什么、值不值得并入。
        selector = uo.Selector(REPO_ROOT, uo.load_data(REPO_ROOT), "codex")
        self.assertEqual(selector.exposed(), {
            "test_claude_sealed_gate_materials": ["tools/arm64_capture_driver/driver/vc5_admission.py"],
            "test_claude_vc4_driver": ["tools/arm64_capture_driver/driver/vc4_client.py"]})


class ReviewRequirementTests(unittest.TestCase):
    """验收修正：联动清单、剔除逐个列理由、基点是否发版提交。"""

    def test_a_new_scenario_version_file_pulls_test_claude_upgrade_scenarios(self) -> None:
        real = uo.load_data(REPO_ROOT)
        [entry] = real["coupled"]
        self.assertIn("test_claude_upgrade_scenarios", entry["modules"])
        pattern = [item for item in entry["patterns"] if "claude_upgrade_scenarios" in item]
        self.assertTrue(pattern and fnmatch.fnmatchcase(f"{CAPTURE}/claude_upgrade_scenarios_0_164_0.json", pattern[0]))
        # 合成仓库用真实的联动表：模拟新增 _0_164_0 版本的场景清单
        base = base_data()
        base["coupled"] = real["coupled"]
        files = {**WORLD, f"{TESTS}/test_claude_upgrade_scenarios.py": "import glob\nPATHS = glob.glob('claude_upgrade_scenarios_*.json')\n",
                 f"{CAPTURE}/claude_upgrade_scenarios_0_163_0.json": '{"tools": []}\n'}
        world = World(self, files, {**CALIBRATION, "test_claude_upgrade_scenarios": HEAVY}, base=base)
        selection = world.select((f"{CAPTURE}/claude_upgrade_scenarios_0_164_0.json", '{"tools": ["x.sh"]}\n'))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_upgrade_scenarios"])
        self.assertEqual(selection["fail_closed"], [])

    def test_every_skipped_module_is_listed_with_its_reason(self) -> None:
        selection = World(self).select()
        rows = selection["skipped"]
        self.assertEqual([row["module"] for row in rows], skipped(selection))
        for row in rows:
            self.assertEqual(sorted(row), ["core_seconds", "module", "owner", "reason", "rule"])
            self.assertEqual(row["owner"], "claude")
            self.assertEqual(row["core_seconds"], HEAVY)
            self.assertIn("核·秒", row["reason"])
            self.assertIn("守卫已核", row["reason"])
        self.assertTrue(uo.seal_ok(selection), "清单进了 selection 的自摘要")

    def test_pulled_modules_are_not_listed_as_skipped_and_a_whole_set_fallback_lists_none(self) -> None:
        selection = World(self).select((f"{CAPTURE}/claude_lib.py", "from tools.official_client_capture import codex_hub, kernel_util\nCHANGED = 1\n"))
        self.assertEqual(sorted(pulled(selection)), ["test_claude_heavy_c"])
        self.assertNotIn("test_claude_heavy_c", [row["module"] for row in selection["skipped"]])
        world = World(self)
        world.data["cheap_core_seconds"] = 31
        world.write_data()
        fallback = world.select()
        self.assertEqual(fallback["scope"], uo.SCOPE_FULL)
        self.assertEqual(fallback["skipped"], [])

    def test_the_base_records_whether_it_is_a_release_tag_commit(self) -> None:
        tagged = World(self, base_tag="v0.2.18-1").select()
        self.assertEqual((tagged["base_is_release"], tagged["base_release_tags"]), (True, ["v0.2.18-1"]))
        plain = World(self).select()
        self.assertEqual((plain["base_is_release"], plain["base_release_tags"]), (False, []))
        odd = World(self, base_tag="nightly-1").select()
        self.assertEqual((odd["base_is_release"], odd["base_release_tags"]), (False, []), "只认 vX.Y.Z[-N] 形式的发版 tag")



class DiffReportTests(unittest.TestCase):
    def test_the_diff_between_a_full_run_and_a_plan_run_is_explained_unit_by_unit(self) -> None:
        full = {"units": [{"unit_id": "test_claude_heavy_a", "spec_sha256": "1"}, {"unit_id": "test_claude_heavy_b#1", "spec_sha256": "2"},
                          {"unit_id": "test_codex_thing", "spec_sha256": "3"}]}
        plan = {"units": [{"unit_id": "test_codex_thing", "spec_sha256": "3"}],
                "selection": {"modules": {"test_claude_heavy_a": "skip", "test_claude_heavy_b": "skip", "test_codex_thing": "run"}}}
        report = uo.diff_report(full, plan)
        self.assertTrue(report["consistent"])
        self.assertEqual([row["unit_id"] for row in report["dropped"]], ["test_claude_heavy_a", "test_claude_heavy_b#1"])
        self.assertTrue(all(row["reason"] for row in report["dropped"]))
        plan["selection"]["modules"]["test_claude_heavy_a"] = "run"
        self.assertFalse(uo.diff_report(full, plan)["consistent"], "清单说 run 却没有这个单元：差集与选择记录对不上")
        plan["selection"]["modules"]["test_claude_heavy_a"] = "skip"
        plan["units"][0]["spec_sha256"] = "x"
        self.assertEqual(uo.diff_report(full, plan)["spec_mismatch"], ["test_codex_thing"])

    def test_p4_audit_check_flags_removed_modules_reading_the_real_data_root(self) -> None:
        data = World(self).data
        record = {"unit_id": "test_claude_heavy_a", "read_audit": {"host_path_modes": {"/root/docker/capture-cli/data/x": ["read"]}}}
        clean = {"unit_id": "test_claude_heavy_a", "read_audit": {"host_path_modes": {"/usr/lib/x": ["read"]}}}
        shared = {"unit_id": "test_ci_shared_thing", "read_audit": {"host_path_modes": {"/root/docker/capture-cli/data/x": ["read"]}}}
        problems = uo.audit_check(data, [record, clean, shared], ["/root/docker/capture-cli/data"])
        self.assertEqual(len(problems), 1)
        self.assertIn("test_claude_heavy_a", problems[0])


class MaterializeAndCommandLineTests(unittest.TestCase):
    def test_materialize_is_idempotent_and_calibration_changes_the_plan(self) -> None:
        world = World(self)
        again = uo.materialize(world.root, world.data, {name: row["core_seconds"] for name, row in world.data["modules"].items()})
        self.assertEqual(again, world.data)
        slower = uo.materialize(world.root, world.data, {**CALIBRATION, "test_claude_cheap": 31.0})
        self.assertEqual(slower["modules"]["test_claude_cheap"]["codex_plan"], uo.SKIP)

    def test_the_command_line_reports_and_selects(self) -> None:
        world = World(self)
        world.commit("候选")
        self.assertEqual(uo.main(["check", "--tree", str(world.root)]), 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "selection.json"
            self.assertEqual(uo.main(["select", "--tree", str(world.root), "--base", world.base, "--output", str(output)]), 0)
            self.assertTrue(uo.seal_ok(json.loads(output.read_text(encoding="utf-8"))))
        self.assertEqual(uo.main(["select", "--tree", str(world.root), "--base", "f" * 40]), 2)

    def test_the_calibration_sources_agree_on_module_core_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            table = Path(directory) / "units.csv"
            table.write_text("unit_id,kind,core_seconds\ntest_a#1,test,10\ntest_a#2,test,5.5\ntest_a!exclusive,test,1\nbackend:unit,command,99\n", encoding="utf-8")
            self.assertEqual(uo.calibration_from_csv(table), {"test_a": 16.5})
            record = Path(directory) / "record.json"
            record.write_text(json.dumps({"seconds": 10.0, "cores": 2.0}), encoding="utf-8")
            manifest = Path(directory) / "unit-manifest.json"
            manifest.write_text(json.dumps({"units": [{"unit_id": "test_b", "unit_type": "test", "record_path": str(record)},
                                                      {"unit_id": "cmd", "unit_type": "command", "record_path": str(record)}]}), encoding="utf-8")
            self.assertEqual(uo.calibration_from_manifest(manifest), {"test_b": 20.0})


if __name__ == "__main__":
    unittest.main()
