"""门禁单元按依赖精度选择（Codex 升级工具优化 OPT-02）：``tools/ci/unit_ownership.py`` 的 ``DependencyModel`` 与依赖追踪表
``tools/ci/unit_dependencies.json``。

背景：同一 Campaign 内修一个文件之后，后台验证与门禁续跑第二层本应只重测依赖这个文件的模块；旧口径只能剔除"另一客户端的重模块"，
Codex 与共用模块一律照跑（387／425）。新口径把"依赖闭包里没有任何改动文件"的其余模块也剔除，三部分取并集：静态导入闭包、测试自己的精确提法、
追踪表里实际观察到的打开与枚举。**宁可多选，不能漏选**：本文件的变异用例逐条证明每一种依赖渠道被改动时，依赖它的模块都被选中。

两部分：合成仓库上的选择器行为与变异；真实仓库上的守卫（追踪表合法、覆盖全部模块、提交在历史里、没有悄悄放水的口径）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.ci import dependency_trace as dt
from tools.ci import unit_ownership as uo
from tools.official_client_capture.tests.unit_ownership_world import CALIBRATION, CAPTURE, TESTS, WORLD, World, git, pulled, skipped

REPO_ROOT = Path(__file__).resolve().parents[3]
LIGHT = ["test_ci_shared_thing", "test_claude_cheap", "test_codex_thing"]   # 合成世界里旧口径一律照跑的三个模块


class DepWorld(World):
    """带依赖追踪表的合成仓库：先提交世界，再写追踪表（追踪提交＝世界的基点提交），之后以含表的提交作基点选择。"""

    def __init__(self, case: unittest.TestCase, files: dict[str, str] | None = None, *, observed: dict[str, tuple[list[str], list[str]]] | None = None,
                 unobserved: dict[str, str] | None = None, calibration: dict[str, float] | None = None) -> None:
        super().__init__(case, files, calibration)
        self.traced = self.base
        self.install_table(observed or {}, unobserved or {})

    def install_table(self, observed: dict[str, tuple[list[str], list[str]]], unobserved: dict[str, str], *, commit: str | None = None) -> None:
        names = sorted({path for opened, listed in observed.values() for path in (*opened, *listed)})
        index = {name: position for position, name in enumerate(names)}
        modules = {module: {"opened": sorted(index[path] for path in opened), "listed": sorted(index[path] for path in listed)}
                   for module, (opened, listed) in observed.items()}
        for module in LIGHT:
            if module not in modules and module not in unobserved:
                modules[module] = {"opened": [], "listed": []}
        table = {"schema_version": uo.DEPENDENCY_TABLE_SCHEMA, "model": uo.DEPENDENCY_MODEL, "generated_at_commit": commit or self.traced,
                 "tracer": {"python": "t", "platform": "t"}, "files": names, "modules": modules, "unobserved": unobserved}
        self.write(uo.DEPENDENCY_TABLE_PATH, json.dumps(table, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        self.base = self.commit("依赖追踪表")

    def pick(self, *changes: tuple[str, str | None], precision: str = uo.PRECISION_DEPENDENCY) -> dict[str, Any]:
        for rel, text in changes:
            if text is None:
                (self.root / rel).unlink()
            else:
                self.write(rel, text)
        self.commit("候选")
        return uo.select(self.root, client="codex", base=self.base, precision=precision)


def reasons(selection: dict[str, Any], module: str) -> list[str]:
    return pulled(selection).get(module, [])


def lite(selection: dict[str, Any]) -> dict[str, list[str]]:
    """被拉入的轻模块（旧口径一律照跑的那三个）；重模块走旧规则，不在这里断言。"""

    return {module: because for module, because in pulled(selection).items() if module in LIGHT}


# --------------------------------------------------------------------------------------------- 选择器行为


class PrecisionSwitchTests(unittest.TestCase):
    def test_without_the_section_or_the_table_the_selection_is_the_old_one_byte_for_byte(self) -> None:
        world = World(self)
        plain = world.select()
        self.assertEqual(skipped(plain), ["test_claude_heavy_a", "test_claude_heavy_b", "test_claude_heavy_c", "test_claude_heavy_d"])
        self.assertNotIn("precision", plain, "旧口径：selection 不多任何键")
        owner = uo.select(world.root, client="codex", base=world.base, precision="owner")
        self.assertEqual({key: value for key, value in owner.items() if key != "selection_sha256"},
                         {key: value for key, value in plain.items() if key != "selection_sha256"})

    def test_a_missing_table_falls_back_under_auto_and_is_refused_when_dependency_is_demanded(self) -> None:
        world = World(self)
        world.commit("再来一个提交")
        auto = uo.select(world.root, client="codex", base=world.base, precision="auto")
        self.assertNotIn("precision", auto)
        with self.assertRaises(uo.OwnershipError):
            uo.select(world.root, client="codex", base=world.base, precision="dependency")

    def test_auto_uses_the_old_rule_on_a_release_tag_base_and_the_dependency_rule_otherwise(self) -> None:
        tagged = DepWorld(self)
        git(tagged.root, "tag", "v1.2.3", tagged.base)
        selection = uo.select(tagged.root, client="codex", base=tagged.base, precision="auto")
        self.assertNotIn("precision", selection, "基点是发版 tag 提交：第一次验证，沿用按归属选择")
        untagged = DepWorld(self)
        selection = untagged.pick(precision="auto")
        self.assertEqual((selection["precision"], selection["dependency_model"]), ("dependency", uo.DEPENDENCY_MODEL))
        self.assertIn("同一 Campaign", selection["precision_reason"])
        self.assertTrue(uo.seal_ok(selection))

    def test_the_data_file_section_sets_the_default_and_a_bad_value_is_refused(self) -> None:
        world = DepWorld(self)
        data = {**world.data, "dependency": {"precision": "dependency", "reason": "t"}}
        world.data = data
        world.write_data()
        world.base = world.commit("数据文件开启依赖精度")
        selection = world.pick()
        self.assertEqual(selection["precision"], "dependency")
        with self.assertRaises(uo.OwnershipError):
            uo.validate_data({**world.data, "dependency": {"precision": "weird"}})
        with self.assertRaises(uo.OwnershipError):
            uo.validate_data({**world.data, "dependency": {"precision": "auto", "extra": 1}})


class DependencySelectionTests(unittest.TestCase):
    def test_nothing_changed_skips_the_light_modules_too_and_lists_every_reason(self) -> None:
        selection = DepWorld(self).pick()
        self.assertEqual(skipped(selection), sorted([*LIGHT, "test_claude_heavy_a", "test_claude_heavy_b", "test_claude_heavy_c", "test_claude_heavy_d"]))
        self.assertEqual(selection["scope"], uo.SCOPE_CLIENT_PLAN, "仍是 Codex 计划的子集：VC-5 准入口径不变")
        rows = {row["module"]: row["reason"] for row in selection["skipped"]}
        for module in LIGHT:
            self.assertIn("依赖闭包碰不到任何改动文件", rows[module])

    def test_a_file_nobody_depends_on_pulls_nothing(self) -> None:
        selection = DepWorld(self).pick((f"{CAPTURE}/loose_data.json", '{"x": 1}\n'))
        self.assertEqual(lite(selection), {})

    def test_imports_are_followed_all_the_way_down(self) -> None:
        files = {**WORLD,
                 f"{CAPTURE}/deep_a.py": "from tools.official_client_capture import deep_b\n",
                 f"{CAPTURE}/deep_b.py": "from tools.official_client_capture import deep_c\n",
                 f"{CAPTURE}/deep_c.py": "VALUE = 1\n",
                 f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture import deep_a\n"}
        selection = DepWorld(self, files).pick((f"{CAPTURE}/deep_c.py", "VALUE = 2\n"))
        self.assertEqual(sorted(lite(selection)), ["test_ci_shared_thing"], "三层间接导入也算依赖（旧口径的一跳在这里不够）")
        self.assertIn("导入", reasons(selection, "test_ci_shared_thing")[0])

    def test_importing_a_submodule_executes_its_parent_packages(self) -> None:
        selection = DepWorld(self).pick((f"{CAPTURE}/__init__.py", "# 包初始化变了\n"))
        self.assertEqual(sorted(lite(selection)), LIGHT, "导入包内模块先执行 __init__.py：全部轻模块都依赖它")

    def test_a_literal_file_name_or_path_in_the_test_is_a_dependency(self) -> None:
        files = {**WORLD,
                 f"{TESTS}/test_ci_shared_thing.py": ("from pathlib import Path\nDATA = Path(__file__).parent.parent / 'shared_data.json'\n"
                                                      "NAMED = 'loose_data.json'\nFULL = 'tools/official_client_capture/hub_data.json'\n")}
        world = DepWorld(self, files)
        selection = world.pick((f"{CAPTURE}/shared_data.json", '{"v": 2}\n'), (f"{CAPTURE}/loose_data.json", '{"v": 2}\n'),
                               (f"{CAPTURE}/hub_data.json", '{"v": 2}\n'))
        self.assertEqual(sorted(lite(selection)), ["test_ci_shared_thing"])
        self.assertEqual(len(reasons(selection, "test_ci_shared_thing")), 3, "三个提法各自记一条缘由")

    def test_a_dotted_module_path_and_a_script_stem_loaded_by_name_are_dependencies(self) -> None:
        files = {**WORLD, f"{CAPTURE}/solo_mod.py": "SOLO = 1\n",
                 f"{TESTS}/test_ci_shared_thing.py": ("ARGV = ['python3', '-m', 'tools.official_client_capture.solo_mod']\n"
                                                      "LOAD = load_script('far_dep')\n")}
        world = DepWorld(self, files)
        selection = world.pick((f"{CAPTURE}/solo_mod.py", "SOLO = 3\n"), (f"{CAPTURE}/far_dep.py", "FAR = 3\n"))
        self.assertEqual(sorted(lite(selection)), ["test_ci_shared_thing"])
        self.assertEqual(len(reasons(selection, "test_ci_shared_thing")), 2)

    def test_docstrings_and_comments_are_not_dependencies(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": '"""说明里提到 loose_data.json 与 shared_data.json。"""\n# 注释里提到 hub_data.json\nX = 1\n'}
        selection = DepWorld(self, files).pick((f"{CAPTURE}/loose_data.json", '{"v": 2}\n'), (f"{CAPTURE}/shared_data.json", '{"v": 2}\n'),
                                              (f"{CAPTURE}/hub_data.json", '{"v": 2}\n'))
        self.assertNotIn("test_ci_shared_thing", pulled(selection))

    def test_a_fixture_counts_only_its_module_level_mentions_and_a_function_body_is_left_to_the_trace(self) -> None:
        files = {**WORLD,
                 f"{TESTS}/the_fixture.py": ("TOP = 'shared_data.json'\n\n\ndef helper():\n    return 'loose_data.json'\n"),
                 f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture.tests import the_fixture\n"}
        world = DepWorld(self, files)
        selection = world.pick((f"{CAPTURE}/shared_data.json", '{"v": 2}\n'), (f"{CAPTURE}/loose_data.json", '{"v": 2}\n'))
        self.assertEqual(reasons(selection, "test_ci_shared_thing")[0].count("shared_data.json"), 1)
        self.assertFalse(any("loose_data.json" in text for text in reasons(selection, "test_ci_shared_thing")),
                         "夹具函数体里的提法不算静态依赖（执行到了才由追踪表给出）")
        traced = DepWorld(self, files, observed={"test_ci_shared_thing": ([f"{CAPTURE}/loose_data.json"], [])})
        self.assertIn("test_ci_shared_thing", pulled(traced.pick((f"{CAPTURE}/loose_data.json", '{"v": 2}\n'))))

    def test_the_test_module_itself_counts_every_mention_including_inside_its_methods(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "class T:\n    def test_a(self):\n        return open('loose_data.json')\n"}
        selection = DepWorld(self, files).pick((f"{CAPTURE}/loose_data.json", '{"v": 2}\n'))
        self.assertIn("test_ci_shared_thing", pulled(selection))


class TraceTableTests(unittest.TestCase):
    def test_a_file_the_trace_saw_opened_is_a_dependency_even_when_no_static_edge_exists(self) -> None:
        world = DepWorld(self, observed={"test_codex_thing": ([f"{CAPTURE}/loose_data.json"], [])})
        selection = world.pick((f"{CAPTURE}/loose_data.json", '{"v": 2}\n'))
        self.assertEqual(sorted(lite(selection)), ["test_codex_thing"])
        self.assertIn("追踪到被打开", reasons(selection, "test_codex_thing")[0])

    def test_an_enumerated_directory_depends_on_additions_and_deletions_and_on_every_file_below_it(self) -> None:
        world = DepWorld(self, observed={"test_codex_thing": ([], [CAPTURE])})
        selection = world.pick((f"{CAPTURE}/brand_new.json", '{"v": 2}\n'))
        self.assertIn("test_codex_thing", pulled(selection), "目录下新增文件")
        world = DepWorld(self, observed={"test_codex_thing": ([], [CAPTURE])})
        selection = world.pick((f"{CAPTURE}/loose_data.json", None))
        self.assertIn("test_codex_thing", pulled(selection), "目录下删除文件")
        world = DepWorld(self, observed={"test_codex_thing": ([], [CAPTURE])})
        selection = world.pick((f"{CAPTURE}/far_dep.py", "FAR = 9\n"))
        self.assertIn("test_codex_thing", pulled(selection), "枚举了整个目录的模块，目录下任何一个文件改了都可能改变结果")
        other = DepWorld(self, observed={"test_codex_thing": ([], [CAPTURE])})
        self.assertNotIn("test_codex_thing", pulled(other.pick(("docs/other.md", "# 变了\n"))), "目录之外的改动不牵到它")

    def test_modules_without_a_usable_record_always_run(self) -> None:
        world = DepWorld(self, unobserved={"test_codex_thing": "追踪运行退出码 1"})
        selection = world.pick()
        self.assertEqual(sorted(lite(selection)), ["test_codex_thing"])
        self.assertIn("追踪运行退出码 1", reasons(selection, "test_codex_thing")[0])
        world = DepWorld(self)
        table = json.loads((world.root / uo.DEPENDENCY_TABLE_PATH).read_text(encoding="utf-8"))
        del table["modules"]["test_ci_shared_thing"]
        world.write(uo.DEPENDENCY_TABLE_PATH, json.dumps(table))
        world.base = world.commit("表里少一个模块")
        selection = world.pick()
        self.assertIn("没有这个模块", reasons(selection, "test_ci_shared_thing")[0], "新模块或追踪表过期：照跑")

    def test_a_change_after_the_trace_keeps_pulling_the_module_even_though_the_base_already_passed_it(self) -> None:
        """追踪表是追踪提交上的快照：追踪之后依赖文件变过的模块，读集可能已经不同，记录过期只能多选，所以按"追踪提交到候选"的改动对依赖求交。"""

        world = DepWorld(self, observed={"test_codex_thing": ([f"{CAPTURE}/loose_data.json"], [])})
        world.write(f"{CAPTURE}/loose_data.json", '{"edited": "after the trace"}\n')
        world.base = world.commit("追踪之后、基点之前改了依赖文件（基点那一轮已经通过）")
        selection = world.pick((f"{CAPTURE}/shared_data.json", '{"v": 2}\n'))
        self.assertIn("test_codex_thing", pulled(selection), "基点..候选里没有它的依赖，但追踪..候选里有：记录可能过期，继续选")
        self.assertEqual(selection["changed_files_count"], 1, "对外记录的仍是基点..候选的改动数")

    def test_a_trace_commit_outside_the_history_fails_closed(self) -> None:
        world = DepWorld(self)
        git(world.root, "checkout", "-q", "-b", "side", world.traced)
        side = world.commit("侧枝上的追踪提交")
        git(world.root, "checkout", "-q", "-")
        world.install_table({}, {}, commit=side)
        selection = world.pick()
        self.assertEqual(sorted(lite(selection)), LIGHT)
        self.assertIn("不在候选提交的历史里", reasons(selection, LIGHT[0])[0])

    def test_the_trace_table_in_the_changes_means_no_filtering_under_dependency_but_not_under_the_old_rule(self) -> None:
        world = DepWorld(self)
        table = json.loads((world.root / uo.DEPENDENCY_TABLE_PATH).read_text(encoding="utf-8"))
        table["modules"]["test_codex_thing"] = {"opened": [], "listed": []}
        selection = world.pick((uo.DEPENDENCY_TABLE_PATH, json.dumps(table)))
        self.assertEqual((selection["scope"], set(selection["modules"].values())), (uo.SCOPE_FULL, {uo.RUN}))
        self.assertIn(uo.DEPENDENCY_TABLE_PATH, selection["fallback_reasons"][0])
        # 旧口径根本不读追踪表：升级登记时重新追踪出的新表，不该让第一次验证（基点是发版 tag）整轮回退全集。
        tagged = DepWorld(self)
        git(tagged.root, "tag", "v9.9.9", tagged.base)
        tagged.write(uo.DEPENDENCY_TABLE_PATH, json.dumps(table))
        tagged.commit("重新追踪")
        old = uo.select(tagged.root, client="codex", base=tagged.base, precision="auto")
        self.assertEqual(old["scope"], uo.SCOPE_CLIENT_PLAN)
        self.assertNotIn("precision", old)

    def test_table_problems_are_refused(self) -> None:
        good = {"schema_version": uo.DEPENDENCY_TABLE_SCHEMA, "model": uo.DEPENDENCY_MODEL, "generated_at_commit": "a" * 40, "tracer": {},
                "files": ["a"], "modules": {"m": {"opened": [0], "listed": []}}, "unobserved": {"n": "原因"}}
        self.assertEqual(uo.dependency_table_problems(good), [])
        for broken in ({**good, "model": "x"}, {**good, "generated_at_commit": "zz"}, {**good, "modules": {"m": {"opened": [5], "listed": []}}},
                       {**good, "unobserved": {"m": "又在这里"}}, {**good, "unobserved": {"n": ""}}, {**good, "schema_version": "old"}):
            with self.subTest(broken=str(broken)[:60]):
                self.assertTrue(uo.dependency_table_problems(broken))


class BuildTableTests(unittest.TestCase):
    """``dependency-table``：把审计钩子的追踪结果汇成追踪表（字节码还原成源文件、子进程参数里的仓库文件、失败的运行进 unobserved）。"""

    def test_trace_records_become_a_table_with_sources_directories_and_unobserved_modules(self) -> None:
        world = World(self)
        trace = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(trace, ignore_errors=True))

        def record(module: str, rc: int, events: dict[str, Any]) -> None:
            folder = trace / module
            folder.mkdir()
            (folder / "_result.json").write_text(json.dumps({"module": module, "rc": rc, "seconds": 1.0, "tail": "boom\nFAILED"}), encoding="utf-8")
            (folder / "123.json").write_text(json.dumps(events), encoding="utf-8")

        record("test_codex_thing", 0, {
            "opened": [f"{CAPTURE}/__pycache__/codex_only.cpython-314.pyc", f"{CAPTURE}/shared_data.json", "not/in/repo.txt", ".git/config"],
            "listed": [CAPTURE, "not/a/dir"], "spawned": [["bash", f"{CAPTURE}/loose_data.json", "--flag"]]})
        record("test_ci_shared_thing", 1, {"opened": [f"{CAPTURE}/shared_data.json"], "listed": []})
        table = uo.build_dependency_table(trace, world.root, world.base, ["test_codex_thing", "test_ci_shared_thing", "test_claude_cheap"],
                                          {"python": "t"})
        self.assertEqual(uo.dependency_table_problems(table), [])
        names = table["files"]
        row = table["modules"]["test_codex_thing"]
        self.assertEqual(sorted(names[i] for i in row["opened"]), [f"{CAPTURE}/codex_only.py", f"{CAPTURE}/loose_data.json", f"{CAPTURE}/shared_data.json"],
                         "字节码还原成源文件；仓库外与未跟踪的路径丢掉；子进程参数里的仓库文件算打开过")
        self.assertEqual([names[i] for i in row["listed"]], [CAPTURE])
        self.assertIn("退出码 1", table["unobserved"]["test_ci_shared_thing"])
        self.assertEqual(table["unobserved"]["test_claude_cheap"], "没有追踪记录")
        self.assertNotIn("test_ci_shared_thing", table["modules"])

    def test_the_real_tracer_sees_a_file_a_test_opens_and_a_directory_it_lists_but_not_the_import_machinery(self) -> None:
        files = {**WORLD, f"{CAPTURE}/runtime_data.json": "{}\n",
                 f"{TESTS}/test_traced_thing.py": ("import os\nimport unittest\nfrom pathlib import Path\n\n\nclass Traced(unittest.TestCase):\n"
                                                   "    def test_reads(self) -> None:\n"
                                                   "        root = Path(__file__).resolve().parents[1]\n"
                                                   "        (root / 'runtime_data.json').read_text()\n"
                                                   "        self.assertTrue(sorted(os.listdir(root / 'tests')))\n")}
        world = World(self, files, CALIBRATION)
        out = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(out, ignore_errors=True))
        results = dt.run(world.root, out, ["test_traced_thing"], 1)
        self.assertEqual([(name, code) for name, code, _seconds in results], [("test_traced_thing", 0)])
        table = uo.build_dependency_table(out, world.root, world.base, ["test_traced_thing"], {})
        names = table["files"]
        row = table["modules"]["test_traced_thing"]
        self.assertIn(f"{CAPTURE}/runtime_data.json", [names[i] for i in row["opened"]])
        self.assertIn(f"{TESTS}/test_traced_thing.py", [names[i] for i in row["opened"]], "测试模块自己的源文件也被打开")
        self.assertEqual([names[i] for i in row["listed"]], [TESTS], "只有测试自己列的目录；导入 tools 包时的 FileFinder 列目录不算")


class MutationTests(unittest.TestCase):
    """变异用例：依赖渠道被改动而依赖它的模块没被选中＝漏选，这里每个用例都让一种渠道变异，断言被抓住。"""

    def test_mutation_an_indirect_data_file_read_only_by_a_library_is_caught_by_the_trace(self) -> None:
        files = {**WORLD, f"{CAPTURE}/reader_lib.py": "def load():\n    return open(__file__.replace('reader_lib.py', 'hidden_data.json')).read()\n",
                 f"{CAPTURE}/hidden_data.json": "{}\n",
                 f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture import reader_lib\nreader_lib.load()\n"}
        silent = DepWorld(self, files).pick((f"{CAPTURE}/hidden_data.json", '{"v": 2}\n'))
        self.assertNotIn("test_ci_shared_thing", pulled(silent), "静态看不到：没有追踪记录时库里拼出来的文件名漏选（这正是追踪表存在的理由）")
        traced = DepWorld(self, files, observed={"test_ci_shared_thing": ([f"{CAPTURE}/hidden_data.json", f"{CAPTURE}/reader_lib.py"], [])})
        self.assertIn("test_ci_shared_thing", pulled(traced.pick((f"{CAPTURE}/hidden_data.json", '{"v": 2}\n'))))

    def test_mutation_a_script_run_by_a_subprocess_is_caught_statically_and_by_the_trace(self) -> None:
        files = {**WORLD, f"{CAPTURE}/run_probe.sh": "echo probe\n",
                 f"{TESTS}/test_ci_shared_thing.py": "import subprocess\nsubprocess.run(['bash', 'tools/official_client_capture/run_probe.sh'])\n"}
        selection = DepWorld(self, files).pick((f"{CAPTURE}/run_probe.sh", "echo changed\n"))
        self.assertIn("test_ci_shared_thing", pulled(selection))
        called_by_library = {**WORLD, f"{CAPTURE}/run_probe.sh": "echo probe\n",
                             f"{CAPTURE}/runner_lib.py": "import subprocess\n\n\ndef go(name):\n    subprocess.run(['bash', 'tools/official_client_capture/' + name])\n",
                             f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture import runner_lib\nrunner_lib.go('run_probe.sh')\n"}
        traced = DepWorld(self, called_by_library, observed={"test_ci_shared_thing": ([f"{CAPTURE}/run_probe.sh"], [])})
        self.assertIn("test_ci_shared_thing", pulled(traced.pick((f"{CAPTURE}/run_probe.sh", "echo changed\n"))))

    def test_mutation_a_file_deleted_while_a_test_still_names_it_is_caught(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "NAME = 'loose_data.json'\n"}
        self.assertIn("test_ci_shared_thing", pulled(DepWorld(self, files).pick((f"{CAPTURE}/loose_data.json", None))))
        self.assertIn("test_ci_shared_thing", pulled(DepWorld(self, observed={"test_ci_shared_thing": ([f"{CAPTURE}/loose_data.json"], [])})
                                                     .pick((f"{CAPTURE}/loose_data.json", None))), "追踪到打开过的文件被删除")
        importer = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture import far_dep\n"}
        self.assertIn("test_ci_shared_thing", pulled(DepWorld(self, importer).pick((f"{CAPTURE}/far_dep.py", None))), "导入的模块被删除")

    def test_mutation_a_new_file_added_under_a_name_the_test_looks_up_is_caught(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "from pathlib import Path\nMAYBE = Path('tools/official_client_capture/optional_extra.json')\n"}
        selection = DepWorld(self, files).pick((f"{CAPTURE}/optional_extra.json", '{"v": 2}\n'))
        self.assertIn("test_ci_shared_thing", pulled(selection), "测试按名字找一个可选文件，新增它会改变结果")

    def test_mutation_a_build_tool_run_by_a_test_depends_on_everything_outside_tools(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "import subprocess\nsubprocess.run(['go', 'test', './...'], cwd='backend')\n"}
        selection = DepWorld(self, files).pick(("backend/main.go", "package main\n// 变了\n"))
        self.assertIn("test_ci_shared_thing", pulled(selection))
        self.assertIn("构建工具", reasons(selection, "test_ci_shared_thing")[0])
        quiet = DepWorld(self).pick(("backend/main.go", "package main\n// 变了\n"))
        self.assertEqual(lite(quiet), {}, "没有拉起构建工具的模块不受 tools/ 以外文件牵连")

    def test_mutation_a_changed_test_module_or_a_changed_fixture_it_imports_is_caught(self) -> None:
        files = {**WORLD, f"{TESTS}/the_fixture.py": "X = 1\n", f"{TESTS}/test_ci_shared_thing.py": "from tools.official_client_capture.tests import the_fixture\n"}
        selection = DepWorld(self, files).pick((f"{TESTS}/the_fixture.py", "X = 2\n"), (f"{TESTS}/test_codex_thing.py", "from tools.official_client_capture import codex_only\nQ = 1\n"))
        self.assertEqual(sorted(lite(selection)), ["test_ci_shared_thing", "test_codex_thing"])

    def test_mutation_an_unrecognized_path_fails_closed_for_the_light_modules_too(self) -> None:
        selection = DepWorld(self).pick(("brand_new_top/dir/file.txt", "x\n"))
        self.assertEqual(sorted(pulled(selection)), sorted([*LIGHT, "test_claude_heavy_a", "test_claude_heavy_b", "test_claude_heavy_c", "test_claude_heavy_d"]))
        self.assertTrue(selection["fail_closed"])

    def test_mutation_a_python_file_that_does_not_parse_fails_closed(self) -> None:
        selection = DepWorld(self).pick((f"{CAPTURE}/far_dep.py", "def broken(:\n"))
        self.assertTrue(set(LIGHT) <= set(pulled(selection)))

    def test_mutation_a_module_that_touches_the_real_arm64_install_is_never_skipped_unless_reviewed(self) -> None:
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": "ROOT = '/root/arm64-capture-driver/driver'\n"}
        selection = DepWorld(self, files).pick()
        self.assertIn("test_ci_shared_thing", pulled(selection))
        self.assertIn("ARM64", reasons(selection, "test_ci_shared_thing")[0])

    def test_mutation_a_new_python_file_nobody_uses_pulls_only_the_modules_that_can_load_by_a_computed_name(self) -> None:
        loader = ("import importlib\nNAMES = {}\n\n\ndef load(key):\n    return importlib.import_module(NAMES[key])\n")
        files = {**WORLD, f"{TESTS}/test_ci_shared_thing.py": loader}
        added = DepWorld(self, files).pick((f"{CAPTURE}/brand_new_lib.py", "NEW = 1\n"))
        self.assertEqual(sorted(lite(added)), ["test_ci_shared_thing"], "测试里有解析不了目标的动态加载：追踪之后新增、没人使用的 Python 文件可能被它加载")
        self.assertIn("动态加载", reasons(added, "test_ci_shared_thing")[0])
        edited = DepWorld(self, files).pick(("tools/ci/helper.py", "X = 3\n"))
        self.assertEqual(lite(edited), {}, "追踪时就存在的文件，被谁加载过追踪表里有记录；只是改动它不触发")
        tests_only = DepWorld(self, files).pick((f"{TESTS}/test_brand_new_thing.py", "X = 1\n"))
        self.assertEqual(lite(tests_only), {}, "新增的测试模块天生没有使用者，它是入口不是被加载的对象")

    def test_mutation_the_old_heavy_rules_are_not_weakened(self) -> None:
        """重模块仍走旧口径：改共享内核照样牵到直接导入它的重模块，改枢纽文件只提示（拍板 1）。"""

        selection = DepWorld(self).pick((f"{CAPTURE}/kernel_util.py", "VALUE = 5\n"))
        self.assertIn("test_claude_heavy_a", pulled(selection))
        hub = DepWorld(self).pick((f"{CAPTURE}/codex_hub.py", "HUB = 5\n"))
        self.assertEqual([row["file"] for row in hub["advisories"]], [f"{CAPTURE}/codex_hub.py"])
        self.assertNotIn("test_claude_heavy_c", pulled(hub))

    def test_mutation_forced_modules_run_regardless_of_dependencies(self) -> None:
        world = DepWorld(self)
        world.commit("空提交")
        selection = uo.select(world.root, client="codex", base=world.base, precision="dependency", force_modules=["test_codex_thing"])
        self.assertEqual(pulled(selection), {"test_codex_thing": ["上一轮失败或没执行完，必须重跑"]})


class SecondLayerSharesTheDependencyDataTests(unittest.TestCase):
    """门禁续跑第二层（换提交按归属表补跑）与后台验证 last-passed 共用同一个 ``select``，所以依赖口径同时对两处生效。"""

    def test_the_entry_gates_plan_uses_the_dependency_rule_for_a_campaign_internal_base(self) -> None:
        from tools.ci import entry_gates as eg

        world = DepWorld(self)
        world.data = {**world.data, "dependency": {"precision": "auto", "reason": "t"}}
        world.write_data()
        world.base = world.commit("数据文件开启 auto 依赖精度")
        world.write(f"{CAPTURE}/shared_data.json", '{"v": 2}\n')
        world.commit("修复一个共用数据文件")
        manifest: dict[str, Any] = {}
        eg.apply_gate_scope(manifest, world.root, gates_wanted=["test-capture-tools"], gate_scope="auto", select_base=world.base, select_client="codex",
                            include_claude=False, audit_policy=None, input_scopes=[], recorded_bridge=None,
                            discover=lambda tree: set(world.data["modules"]), force_modules=["test_codex_thing"])
        selection = manifest["selection"]
        self.assertEqual((selection["precision"], selection["scope"]), ("dependency", "client-plan"))
        self.assertEqual(selection["force_modules"], ["test_codex_thing"])
        # test_claude_heavy_a 自己按文件名读 shared_data.json，走旧口径被拉入；其余重模块与不相干的轻模块被剔除，强制模块照跑。
        self.assertEqual(skipped(selection), sorted(["test_ci_shared_thing", "test_claude_cheap", "test_claude_heavy_b",
                                                     "test_claude_heavy_c", "test_claude_heavy_d"]))
        self.assertEqual(manifest["gate_scope_info"]["applied"], "client-plan")


# --------------------------------------------------------------------------------------------- 真实仓库的守卫


class RealTreeGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = uo.load_data(REPO_ROOT)
        cls.table = uo.load_dependency_table(REPO_ROOT)

    def test_the_real_data_file_enables_auto_precision_and_the_table_exists(self) -> None:
        self.assertEqual((self.data.get("dependency") or {}).get("precision"), "auto")
        self.assertIsNotNone(self.table)

    def test_the_table_is_well_formed_and_its_trace_commit_is_in_this_history(self) -> None:
        self.assertEqual(uo.dependency_table_problems(self.table), [])
        commit = self.table["generated_at_commit"]
        if git(REPO_ROOT, "rev-parse", "--is-shallow-repository") == "true":
            self.skipTest("浅克隆里看不到追踪提交；选择器遇到这种情形对所有模块失败关闭（多选不漏选）")
        found = git(REPO_ROOT, "cat-file", "-t", commit) if _commit_exists(commit) else "missing"
        self.assertEqual(found, "commit", "追踪提交必须在仓库历史里，否则选择器对所有模块失败关闭")

    def test_the_table_records_or_explains_every_module_that_the_dependency_rule_could_skip(self) -> None:
        modules = self.data["modules"]
        heavy = {module for module, row in modules.items()
                 if uo.plan_state(row, "codex", cheap=self.data["cheap_core_seconds"], pinned=module in self.data["pinned"]) == uo.SKIP}
        wanted = {module for module in uo.discover_modules(REPO_ROOT) if module not in heavy}
        have = set(self.table["modules"]) | set(self.table["unobserved"])
        stale = sorted(have - set(uo.discover_modules(REPO_ROOT)))
        self.assertEqual(stale, [], "追踪表里有已经不存在的测试模块")
        missing = sorted(wanted - have)
        self.assertTrue(len(missing) <= 25, f"追踪表之后新增的测试模块太多（{len(missing)}），重新追踪：{missing[:6]}")
        self.assertTrue(len(self.table["unobserved"]) <= len(wanted) * 0.12, "追踪失败的模块超过 12%：追踪环境有问题，不要提交这样的表")

    def test_the_table_is_not_a_self_path_of_the_old_rule_but_the_selector_and_ownership_data_are(self) -> None:
        self.assertNotIn(uo.DEPENDENCY_TABLE_PATH, uo.SELF_PATHS)
        self.assertIn("tools/ci/unit_ownership.py", uo.SELF_PATHS)

    def test_the_real_table_separates_a_driver_leaf_from_a_core_library(self) -> None:
        """真实依赖的缩影：改驱动目录里的叶子脚本只牵到二三十个模块；改受管目录的叶子脚本牵到约一百个（九十个模块会复制或摘要整个受管树，
        它们对受管目录里的每个文件都有真实依赖）；改核心库牵到近三百个。三者都远少于旧口径的 387 个。"""

        driver_leaf = uo.what_if(REPO_ROOT, ["tools/arm64_capture_driver/driver/phase_context.py"])
        tool_leaf = uo.what_if(REPO_ROOT, ["tools/official_client_capture/drive_candidate_gateway_ws.py"])
        core = uo.what_if(REPO_ROOT, ["tools/official_client_capture/capturelib/model.py"])
        self.assertLessEqual(driver_leaf["modules"], 60, sorted(driver_leaf["pulled"])[:12])
        self.assertLessEqual(tool_leaf["modules"], 160, sorted(tool_leaf["pulled"])[:12])
        self.assertGreater(core["modules"], tool_leaf["modules"] + 100)
        self.assertLess(core["modules"], 387)
        for module in ("test_candidate_gateway_ws", "test_candidate_core_capture"):
            self.assertIn(module, tool_leaf["pulled"], "直接测这个脚本的模块必须在")

    def test_the_reviewed_files_exist_and_each_has_a_written_reason(self) -> None:
        reviewed = self.data["dependency"]["reviewed_files"]
        self.assertTrue(reviewed)
        for path, why in reviewed.items():
            self.assertTrue((REPO_ROOT / path).is_file(), path)
            self.assertTrue(why.strip(), path)
        self.assertNotIn("tools/official_client_capture/tests/vc1_recorded_replay.py", reviewed, "按真实宿主数据根枚举 runs 目录的辅助：真访问，不能复核放行")


def _commit_exists(commit: str) -> bool:
    import subprocess

    return subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True).returncode == 0


if __name__ == "__main__":
    unittest.main()
