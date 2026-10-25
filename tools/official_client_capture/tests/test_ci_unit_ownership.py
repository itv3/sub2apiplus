"""门禁单元按客户端归属与按改动选择（Codex 升级工具优化第 9 项）：``tools/ci/unit_ownership.py`` 与数据文件 ``unit_ownership.json``。

两部分：

* 真实仓库的守卫（CI 里也跑）：数据文件与现推结果一致、覆盖全部测试模块、廉价阈值与计划状态自洽、被剔除的模块里没有访问真实 ARM64
  环境的路径字面量、Codex 计划与全集的差集每个模块都带理由；
* 合成仓库上的选择器行为（验收口径）：改一个共享文件，计划自动带上依赖它的 Claude 单元；改枢纽文件只提示不并入；联动文件只并入直接验证它们的模块；
  失败关闭各项；归属表自己在改动里就不筛选；摘要自检。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.ci import unit_ownership as uo

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE = "tools/official_client_capture"
TESTS = f"{CAPTURE}/tests"
HEAVY = 100.0
CHEAP = 5.0


def git(root: Path, *args: str) -> str:
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                             "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
                            capture_output=True, text=True, check=True, env=environment)
    return result.stdout.strip()


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

    def test_the_coupled_table_is_the_ten_modules_approved_for_the_registration_anchors(self) -> None:
        # 拍板 4：Claude 联动文件（场景清单、期望画像、冻结摘要）被改时，只并入直接验证它们的这 10 个单元；改这张表要在评审里显式看到。
        [entry] = self.data["coupled"]
        self.assertEqual(sorted(entry["modules"]), [
            "test_claude_build_rule_assertion_results", "test_claude_candidate_draft_projection", "test_claude_recorded_candidate_source",
            "test_claude_recorded_full_validation", "test_claude_rule_assertion", "test_claude_rule_expectations_2_1_226",
            "test_claude_upgrade_boundary_codex", "test_claude_upgrade_candidate_seal_evidence", "test_claude_upgrade_compare_accept",
            "test_claude_vc5_chain_fixture"])
        self.assertEqual(entry["ignore_assignments"], ["FROZEN_PROFILE_SHA256S"])
        self.assertEqual(len(entry["patterns"]), 3)


# --------------------------------------------------------------------------------------------- 合成仓库上的选择器行为


def base_data() -> dict[str, Any]:
    """合成仓库用的人工登记部分（模块表、枢纽、共用内核由 materialize 现推）。"""

    return {
        "schema_version": uo.SCHEMA, "description": "合成仓库", "calibration": {"run_id": "t", "source": "t", "note": "t"},
        "cheap_core_seconds": 30, "enabled_clients": ["codex"],
        "infra_prefixes": ["test_ci_"], "overrides": {}, "pinned": {}, "reviewed_hermetic": {},
        "p3_literals": ["/root/arm64-capture-driver"],
        "coupled": [{
            "patterns": [f"{CAPTURE}/claude_upgrade_scenarios_[0-9]*.json", f"{CAPTURE}/claude_rule_assertion.py"],
            "ignore_assignments": ["FROZEN_PROFILE_SHA256S"], "modules": ["test_claude_heavy_d"], "reason": "镜像式重锚",
        }],
        "recognized_areas": ["docs/", "backend/", "tools/ci/", f"{CAPTURE}/", "tools/official_client_control/", "Makefile"],
        "generic_refs": ["docs/egress"], "generic_names": ["__init__.py"],
        "hubs": [], "shared_kernel": [], "modules": {"placeholder": {"owner": "shared", "rule": "x", "core_seconds": None,
                                                                       "codex_plan": "run", "claude_plan": "run"}},
    }


WORLD: dict[str, str] = {
    f"{CAPTURE}/__init__.py": "",
    f"{CAPTURE}/kernel_util.py": "VALUE = 1\n",
    f"{CAPTURE}/codex_hub.py": "HUB = 1\n",
    f"{CAPTURE}/codex_only.py": "from tools.official_client_capture import kernel_util\nONLY = 1\n",
    f"{CAPTURE}/claude_lib.py": "from tools.official_client_capture import codex_hub, kernel_util\n",
    f"{CAPTURE}/claude_deep.py": "from tools.official_client_capture import claude_lib\n",
    f"{CAPTURE}/claude_rule_assertion.py": 'FROZEN_PROFILE_SHA256S: dict[str, str] = {"1": "a"}\nVALUE = 1\n',
    f"{CAPTURE}/claude_upgrade_scenarios_1_0_0.json": '{"tools": []}\n',
    f"{CAPTURE}/fixture_dep.py": "from tools.official_client_capture import far_dep\n",
    f"{CAPTURE}/far_dep.py": "FAR = 1\n",
    f"{CAPTURE}/shared_data.json": "{}\n",
    f"{CAPTURE}/loose_data.json": "{}\n",
    f"{CAPTURE}/codex_data_9_9_9.json": "{}\n",
    f"{CAPTURE}/codex_orphan_1_2_3.json": "{}\n",
    f"{CAPTURE}/hub_data.json": "{}\n",
    f"{TESTS}/__init__.py": "",
    f"{TESTS}/claude_fixture.py": "from tools.official_client_capture import fixture_dep\n",
    # 重模块 a：直接导入共用内核，并按路径字面量读 docs/guide.md
    f"{TESTS}/test_claude_heavy_a.py": ("from pathlib import Path\nfrom tools.official_client_capture import kernel_util\n"
                                        "GUIDE = Path('.') / 'docs' / 'guide.md'\nREAD = open('shared_data.json')\n"),
    # 重模块 b：只经一层夹具导入 fixture_dep（fixture_dep 再导入 far_dep，两层以外不算）
    f"{TESTS}/test_claude_heavy_b.py": "from tools.official_client_capture.tests import claude_fixture\n",
    # 重模块 c：只经 Claude 面文件链 claude_deep -> claude_lib -> codex_hub／kernel_util 间接依赖
    f"{TESTS}/test_claude_heavy_c.py": "from tools.official_client_capture import claude_deep\n",
    # 重模块 d：联动模块（直接核对冻结摘要）
    f"{TESTS}/test_claude_heavy_d.py": "from tools.official_client_capture import claude_rule_assertion\n",
    f"{TESTS}/test_claude_cheap.py": "from tools.official_client_capture import kernel_util\n",
    f"{TESTS}/test_codex_thing.py": "from tools.official_client_capture import codex_only\nDATA = 'codex_data_9_9_9.json'\n",
    f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture import kernel_util\n",
    "docs/guide.md": "# guide\n",
    "docs/other.md": "# other\n",
    "backend/main.go": "package main\n",
    "tools/ci/helper.py": "X = 1\n",
    "Makefile": "all:\n",
}
CALIBRATION = {"test_claude_heavy_a": HEAVY, "test_claude_heavy_b": HEAVY, "test_claude_heavy_c": HEAVY, "test_claude_heavy_d": HEAVY,
               "test_claude_cheap": CHEAP, "test_codex_thing": 50.0, "test_ci_shared_thing": 10.0}
HEAVY_MODULES = ["test_claude_heavy_a", "test_claude_heavy_b", "test_claude_heavy_c", "test_claude_heavy_d"]


class World:
    """合成 git 仓库：基点提交带数据文件，测试里再改文件、提交候选提交后选择。"""

    def __init__(self, case: unittest.TestCase, files: dict[str, str] | None = None) -> None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        git(self.root, "init", "-q")
        for rel, text in (files or WORLD).items():
            self.write(rel, text)
        self.data = uo.materialize(self.root, base_data(), CALIBRATION)
        self.write_data()
        self.base = self.commit("基点")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_data(self) -> None:
        (self.root / uo.DATA_PATH).parent.mkdir(parents=True, exist_ok=True)
        (self.root / uo.DATA_PATH).write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *changes: tuple[str, str | None], include_claude: bool = False) -> dict[str, Any]:
        """改文件（None 表示删除）→ 提交 → 以基点选择。"""

        for rel, text in changes:
            if text is None:
                (self.root / rel).unlink()
            else:
                self.write(rel, text)
        self.commit("候选")
        return uo.select(self.root, client="codex", base=self.base, include_claude=include_claude)


def skipped(selection: dict[str, Any]) -> list[str]:
    return sorted(module for module, state in selection["modules"].items() if state == uo.SKIP)


def pulled(selection: dict[str, Any]) -> dict[str, list[str]]:
    return {item["module"]: item["because"] for item in selection["pulled"]}


class SelectionTests(unittest.TestCase):
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
