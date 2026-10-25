"""门禁清单与执行器的"按客户端筛选"（Codex 升级工具优化第 9 项，S3）：``entry_gates.plan_gates(gate_scope="client-plan")`` 写入 ``selection``，
执行器 ``run-gates`` 在 ``plan_units`` 之后据此只保留标 run 的测试模块。

* 清单：缺省不筛选、清单与改造前逐字相同；筛选时只多一个 ``selection`` 块，命令单元与测试组不变；各项互斥与前置条件失败关闭；
* 真实执行器（临时目录里的小测试树）：被跳过的模块不执行、全集核对只含保留的测试、单元规格与全集运行逐个相同（记录互通）、
  运行清单与汇总带筛选信息；被改过的 selection、陈旧的模块表、不允许的模式都拒绝；
* 导出：总摘要、全量门禁记录与预跑记录回显筛选摘要，没有筛选时与改造前相同。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_ownership as uo
from tools.official_client_capture.tests import test_ci_entry_gates as entry_gates_tests
from tools.official_client_capture.tests.unit_ownership_world import CAPTURE, TESTS, WORLD, World, git

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"


def module_text(name: str) -> str:
    return f"import unittest\n\n\nclass {name.title().replace('_', '')}(unittest.TestCase):\n    def test_ok(self) -> None:\n        pass\n"


# 小测试树：每个模块一个必过的用例；claude 重模块（200 核·秒）会被 Codex 计划剔除，轻的与 codex、基础设施模块照跑。
MINI = {
    "Makefile": "test-capture-tools-prerequisites:\n\t@true\n",
    f"{CAPTURE}/__init__.py": "",
    f"{TESTS}/__init__.py": "",
    **{f"{TESTS}/{name}.py": module_text(name) for name in ("test_claude_big", "test_claude_small", "test_codex_x", "test_ci_y")},
}
MINI_CALIBRATION = {"test_claude_big": 200.0, "test_claude_small": 3.0, "test_codex_x": 40.0, "test_ci_y": 10.0}


class GateScopePlanTests(unittest.TestCase):
    def plan(self, world: World, **kwargs: object) -> dict:
        return eg.plan_gates(world.root, profile="regression", launcher=[], **kwargs)

    def test_the_default_scope_adds_nothing_and_the_plan_equals_the_old_plan(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        default = self.plan(world)
        self.assertNotIn("selection", default)
        self.assertEqual(self.plan(world, gate_scope="full"), default)

    def test_client_plan_only_adds_a_selection_block_and_leaves_units_and_groups_alone(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        full = self.plan(world)
        scoped = self.plan(world, gate_scope="client-plan", select_base=world.base)
        self.assertEqual({key: value for key, value in scoped.items() if key != "selection"}, full)
        selection = scoped["selection"]
        self.assertEqual(selection["modules"], {"test_ci_y": "run", "test_claude_big": "skip", "test_claude_small": "run", "test_codex_x": "run"})
        self.assertEqual((selection["client"], selection["scope"], selection["base_commit"]), ("codex", "client-plan", world.base))
        self.assertTrue(uo.seal_ok(selection))
        self.assertEqual(scoped["gates"], full["gates"], "门禁项集合不变：命令单元与测试组都还在")

    def test_a_base_is_mandatory_and_never_guessed(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        with self.assertRaisesRegex(ValueError, "必须同时给 --select-base"):
            self.plan(world, gate_scope="client-plan")
        with self.assertRaisesRegex(ValueError, "只在 --gate-scope client-plan 下有意义"):
            self.plan(world, select_base=world.base)
        with self.assertRaisesRegex(ValueError, "只在 --gate-scope client-plan 下有意义"):
            self.plan(world, include_claude=True)
        with self.assertRaisesRegex(ValueError, "未知的门禁范围"):
            self.plan(world, gate_scope="everything")

    def test_the_scope_is_refused_without_the_capture_group_and_with_the_full_set_only_features(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        manifest: dict = {}
        common = dict(tree=world.root, gate_scope="client-plan", select_base=world.base, select_client="codex", include_claude=False,
                      audit_policy=None, input_scopes=(), recorded_bridge=None)
        with self.assertRaisesRegex(ValueError, "没有 test-capture-tools"):
            eg.apply_gate_scope(manifest, gates_wanted=("backend-go-test",), **common)
        for name, value in (("audit_policy", {"units": {}}), ("input_scopes", ({"x": 1},)), ("recorded_bridge", {"unit": {}})):
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, "不能与读集合同"):
                    eg.apply_gate_scope(manifest, gates_wanted=("test-capture-tools",), **{**common, name: value})
        self.assertEqual(manifest, {})

    def test_only_codex_plans_are_enabled_in_this_version(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        with self.assertRaisesRegex(ValueError, "只启用"):
            self.plan(world, gate_scope="client-plan", select_base=world.base, select_client="claude")

    def test_the_manual_switch_keeps_the_heavy_claude_modules(self) -> None:
        world = World(self, MINI, MINI_CALIBRATION)
        selection = self.plan(world, gate_scope="client-plan", select_base=world.base, include_claude=True)["selection"]
        self.assertTrue(selection["include_claude"])
        self.assertEqual(set(selection["modules"].values()), {"run"})

    def test_the_driver_copies_are_byte_identical_to_the_ci_files(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("entry_gates.py", "unit_executor.py", "unit_ownership.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes())


class AutoScopeTests(unittest.TestCase):
    """``--gate-scope auto``（升级流程的缺省）：能筛选就筛选，基点缺省取最近的发版 tag，任何一步不行就回退全集、原因照实写明。"""

    def setUp(self) -> None:
        # 小测试树里调不起真实的执行器 plan（它按仓库的调度配置核对登记的模块都存在）：这里替身成"执行器 discover 出的就是测试目录里的模块"；
        # 真实 discover 与归属表的一致性在 RealTreeDiscoveryTests 里用真实测试树核对。
        def discovered(tree: Path) -> set[str]:
            return {path.stem for path in (Path(tree) / TESTS).glob("test_*.py")}

        patcher = mock.patch.object(eg, "executor_modules", side_effect=discovered)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, world: World, **kwargs: object) -> dict:
        return eg.plan_gates(world.root, profile="regression", launcher=[], gate_scope="auto", **kwargs)

    def world(self, **kwargs: object) -> World:
        return World(self, MINI, MINI_CALIBRATION, **kwargs)

    def test_an_explicit_base_selects_and_records_where_it_came_from(self) -> None:
        world = self.world()
        manifest = self.plan(world, select_base=world.base)
        info = manifest["gate_scope_info"]
        self.assertEqual((info["requested"], info["applied"], info["base_source"], info["base_commit"]), ("auto", "client-plan", "explicit", world.base))
        self.assertNotIn("fallback_reason", info)
        self.assertEqual(manifest["selection"]["modules"]["test_claude_big"], "skip")
        self.assertEqual(manifest["selection"]["base_commit"], world.base)

    def test_the_default_base_is_the_nearest_release_tag(self) -> None:
        world = self.world()
        git(world.root, "tag", "v0.2.18-1", world.base)
        world.commit("候选")
        info = self.plan(world)["gate_scope_info"]
        self.assertEqual((info["applied"], info["base_source"], info["base_tag"], info["base_commit"]), ("client-plan", "latest-release-tag", "v0.2.18-1", world.base))

    def test_only_release_formatted_tags_count(self) -> None:
        world = self.world()
        git(world.root, "tag", "vendor-snapshot", world.base)
        git(world.root, "tag", "v1", world.base)
        world.commit("候选")
        info = self.plan(world)["gate_scope_info"]
        self.assertEqual(info["applied"], "full")
        self.assertIn("找不到最近的发版 tag", info["fallback_reason"])

    def assert_full(self, manifest: dict, reason: str) -> None:
        self.assertNotIn("selection", manifest, "回退全集：清单不带 selection，执行器照常跑全集")
        info = manifest["gate_scope_info"]
        self.assertEqual((info["requested"], info["applied"]), ("auto", "full"))
        self.assertIn(reason, info["fallback_reason"])

    def test_every_selector_failure_falls_back_to_the_whole_set_with_the_reason(self) -> None:
        world = self.world()
        self.assert_full(self.plan(world), "找不到最近的发版 tag")                                   # 没有 tag
        self.assert_full(self.plan(world, select_base="0" * 40), "不在测试树的历史里")                     # 基点不对
        self.assert_full(self.plan(world, select_base="HEAD"), "40 位小写提交")                           # 基点不是完整提交
        self.assert_full(self.plan(world, select_base=world.base, select_client="claude"), "只启用")      # 客户端未启用
        world.write("Makefile", "dirty:\n")
        self.assert_full(self.plan(world, select_base=world.base), "不干净")                              # 树不干净
        git(world.root, "checkout", "--", "Makefile")
        (world.root / uo.DATA_PATH).write_text("{坏的", encoding="utf-8")
        world.commit("归属数据坏了")
        self.assert_full(self.plan(world, select_base=world.base), "读不到归属数据")                       # 归属数据坏

    def test_an_identity_mismatch_with_the_executor_falls_back_instead_of_aborting_the_run(self) -> None:
        world = self.world()
        common = dict(tree=world.root, gates_wanted=("test-capture-tools",), gate_scope="auto", select_base=world.base, select_client="codex",
                      include_claude=False, audit_policy=None, input_scopes=(), recorded_bridge=None)
        modules = {"test_ci_y", "test_claude_big", "test_claude_small", "test_codex_x"}
        manifest: dict = {}
        eg.apply_gate_scope(manifest, discover=lambda _tree: modules, **common)
        self.assertEqual(manifest["gate_scope_info"]["applied"], "client-plan", "执行器 discover 与归属表一致：筛选")
        for discovered, text in ((modules | {"test_ghost"}, "多 []，少 ['test_ghost']"), (modules - {"test_codex_x"}, "多 ['test_codex_x']")):
            manifest = {}
            eg.apply_gate_scope(manifest, discover=lambda _tree, value=discovered: value, **common)
            self.assert_full(manifest, "全集身份对不上")
            self.assertIn(text, manifest["gate_scope_info"]["fallback_reason"])
        manifest = {}

        def failing(_tree: Path) -> set[str]:
            raise ValueError("执行器 plan 没跑通")

        eg.apply_gate_scope(manifest, discover=failing, **common)
        self.assert_full(manifest, "执行器 plan 没跑通")

    def test_incompatible_selections_fall_back_quietly_instead_of_failing(self) -> None:
        world = self.world()
        common = dict(tree=world.root, select_base=world.base, select_client="codex", include_claude=False, audit_policy=None, input_scopes=(),
                      recorded_bridge=None, gate_scope="auto", discover=lambda _tree: {"test_ci_y", "test_claude_big", "test_claude_small", "test_codex_x"})
        for name, overrides, reason in (("没有测试组", {"gates_wanted": ("backend-go-test",)}, "没有 test-capture-tools"),
                                        ("读集合同", {"audit_policy": {"units": {}}}, "走全集"),
                                        ("收窄提案", {"input_scopes": ({"x": 1},)}, "走全集"),
                                        ("固定79接线", {"recorded_bridge": {"unit": {}}}, "走全集")):
            with self.subTest(name):
                manifest: dict = {}
                eg.apply_gate_scope(manifest, **{"gates_wanted": ("test-capture-tools",), **common, **overrides})
                self.assert_full(manifest, reason)

    def test_the_candidate_changing_its_own_rules_makes_the_selector_fall_back_to_the_whole_set(self) -> None:
        world = self.world()
        world.data["cheap_core_seconds"] = 31
        world.write_data()
        world.commit("候选改了归属表")
        manifest = self.plan(world, select_base=world.base)
        info = manifest["gate_scope_info"]
        self.assertEqual((info["applied"], manifest["selection"]["scope"]), ("full", "full"))
        self.assertIn("归属表或选择器本身在改动里", info["fallback_reason"])
        self.assertEqual(set(manifest["selection"]["modules"].values()), {"run"})

    def test_unclassified_and_stale_modules_are_reported_and_never_skipped(self) -> None:
        world = self.world()
        world.write(f"{TESTS}/test_claude_new.py", module_text("test_claude_new"))
        (world.root / TESTS / "test_codex_x.py").unlink()
        world.commit("候选")
        manifest = eg.plan_gates(world.root, profile="regression", launcher=[], gate_scope="auto", select_base=world.base)
        info = manifest["gate_scope_info"]
        self.assertEqual((info["applied"], info["unclassified"], info["stale_modules"]), ("client-plan", ["test_claude_new"], ["test_codex_x"]))
        self.assertEqual(manifest["selection"]["modules"]["test_claude_new"], "run")
        self.assertNotIn("test_codex_x", manifest["selection"]["modules"])

    def test_the_plan_command_prints_the_scope_and_the_reason(self) -> None:
        world = self.world()
        output = Path(world.root.parent) / f"{world.root.name}-auto-plan.json"
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        completed = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "tools" / "ci" / "entry_gates.py"), "plan", "--tree", str(world.root),
                                    "--profile", "regression", "--launcher-json", "[]", "--gate-scope", "auto", "--output", str(output)],
                                   capture_output=True, text=True, timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        printed = [json.loads(line) for line in completed.stdout.strip().splitlines()]
        scope = next(item["gate_scope"] for item in printed if "gate_scope" in item)
        self.assertEqual((scope["requested"], scope["applied"]), ("auto", "full"))
        self.assertIn("找不到最近的发版 tag", scope["fallback_reason"])
        self.assertFalse(any("selection" in item for item in printed))
        self.assertNotIn("selection", json.loads(output.read_text(encoding="utf-8")))


class GateScopeExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self, MINI, MINI_CALIBRATION)
        self.root = self.world.root.parent / f"{self.world.root.name}-run"
        self.root.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.tree = self.world.root
        (self.root / "config.json").write_text(json.dumps({
            "schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 2, "default_quota": {"cores": 1, "memory_mb": 128},
            "quotas": {}, "splits": {}, "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 1}), encoding="utf-8")

    def run_gates(self, manifest: dict, out: str, *extra: str) -> tuple[subprocess.CompletedProcess, dict, dict]:
        path = self.root / f"{out}-gates.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, "-B", str(EXECUTOR), "run-gates", "--manifest", str(path), "--config", str(self.root / "config.json"),
                   "--weights", str(self.root / "none-weights.json"), "--durations", str(self.root / "none-durations.json"), "--cores", "4",
                   "--state-dir", str(self.root / "state"), "--out-dir", str(self.root / out), "--shared-caches", "off",
                   "--record-store", str(self.root / "store"), *extra]
        completed = subprocess.run(command, cwd=self.tree, capture_output=True, text=True, timeout=300,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        summary_path = self.root / out / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        unit_manifest_path = self.root / out / "unit-manifest.json"
        unit_manifest = json.loads(unit_manifest_path.read_text(encoding="utf-8")) if unit_manifest_path.exists() else {}
        return completed, summary, unit_manifest

    def plans(self) -> tuple[dict, dict]:
        full = eg.plan_gates(self.tree, profile="regression", launcher=[])
        scoped = eg.plan_gates(self.tree, profile="regression", launcher=[], gate_scope="client-plan", select_base=self.world.base)
        return full, scoped

    def test_a_scoped_run_executes_only_the_kept_modules_and_its_records_match_the_full_run(self) -> None:
        full, scoped = self.plans()
        completed_full, summary_full, manifest_full = self.run_gates(full, "full")
        self.assertEqual(completed_full.returncode, 0, completed_full.stderr[-2000:])
        completed, summary, manifest = self.run_gates(scoped, "scoped")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        executed = sorted(unit["unit_id"] for unit in manifest["units"])
        self.assertEqual(executed, ["capture:prerequisites", "test_ci_y", "test_claude_small", "test_codex_x"], "重模块 test_claude_big 没有被规划")
        group = summary["test_groups"]["capture-tools"]
        self.assertEqual((group["status"], group["expected_tests"], group["reported_tests"]), ("passed", 3, 3), "全集核对只含保留的模块")
        self.assertEqual(summary_full["test_groups"]["capture-tools"]["expected_tests"], 4)
        self.assertEqual(summary["client_selection"]["modules_skipped"], 1)
        self.assertEqual(summary["client_selection"]["scope"], "client-plan")
        self.assertNotIn("client_selection", summary_full)
        self.assertEqual(manifest["client_selection"], scoped["selection"], "完整的 selection 封进运行清单")
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])
        self.assertNotIn("client_selection", manifest_full)
        self.assertIn("按客户端筛选（codex 计划，client-plan）", completed.stderr)
        # 记录互通：交集上的单元规格摘要逐个相同，差集恰好是被剔除的重模块，且与选择记录一致。
        report = uo.diff_report(manifest_full, manifest)
        self.assertTrue(report["consistent"], report)
        self.assertEqual([row["unit_id"] for row in report["dropped"]], ["test_claude_big"])
        self.assertEqual(report["spec_mismatch"], [])
        self.assertEqual(report["common"], 4)

    def test_a_tampered_selection_is_refused_before_anything_runs(self) -> None:
        _full, scoped = self.plans()
        scoped["selection"]["modules"]["test_codex_x"] = "skip"
        completed, summary, _manifest = self.run_gates(scoped, "tampered")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("自摘要不符", completed.stderr)
        self.assertEqual(summary, {})

    def test_a_stale_or_incomplete_module_table_is_refused(self) -> None:
        _full, scoped = self.plans()
        stale = copy.deepcopy(scoped)
        stale["selection"]["modules"]["test_gone"] = "run"
        stale["selection"] = uo.seal(stale["selection"])
        completed, _summary, _manifest = self.run_gates(stale, "stale")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("全集身份不匹配", completed.stderr)
        self.assertIn("test_gone", completed.stderr)
        missing = copy.deepcopy(scoped)
        del missing["selection"]["modules"]["test_codex_x"]
        missing["selection"] = uo.seal(missing["selection"])
        completed, _summary, _manifest = self.run_gates(missing, "missing")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("test_codex_x", completed.stderr)

    def test_a_scoped_manifest_refuses_inheritance_modes_and_a_selection_that_keeps_nothing(self) -> None:
        _full, scoped = self.plans()
        completed, _summary, _manifest = self.run_gates(scoped, "inherit", "--mode", "full-set-pass")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("只许 --mode re-execute", completed.stderr)
        nothing = copy.deepcopy(scoped)
        nothing["selection"]["modules"] = {name: "skip" for name in nothing["selection"]["modules"]}
        nothing["selection"] = uo.seal(nothing["selection"])
        completed, _summary, _manifest = self.run_gates(nothing, "nothing")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("一个模块都不保留", completed.stderr)

    def test_a_scoped_manifest_cannot_carry_an_audit_policy(self) -> None:
        _full, scoped = self.plans()
        scoped["audit_policy"] = {"schema_version": "x", "units": {}}
        completed, _summary, _manifest = self.run_gates(scoped, "policy")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("不能同时带读集合同", completed.stderr)

    def test_export_echoes_the_selection_into_the_entry_summary_and_the_scope_records(self) -> None:
        # 导出：用真实的执行器汇总喂 export_records（只含测试组与前置检查，没有 make test 的完整组成，所以不写预跑与全量门禁记录）；
        # 全量门禁与预跑记录的回显在下面的合成树用例里。
        _full, scoped = self.plans()
        completed, summary, _manifest = self.run_gates(scoped, "export")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        out = self.root / "exported"
        entry = eg.export_records(self.root / "export-gates.json", self.root / "export" / "summary.json", out,
                                  source={"commit": "c" * 40, "tree_head": "c" * 40}, subject="s", round_id="r", tree=str(self.tree),
                                  isolation="x", host="h", architecture="a")
        echoed = entry["client_selection"]
        self.assertEqual((echoed["gate_scope"], echoed["client"], echoed["modules_skipped"]), ("client-plan", "codex", 1))
        self.assertEqual(echoed["selection_sha256"], scoped["selection"]["selection_sha256"])
        self.assertEqual(json.loads((out / "entry-gates.json").read_text(encoding="utf-8"))["client_selection"], echoed)


class GateScopeExportTests(unittest.TestCase):
    CHECKS = entry_gates_tests.EntryGatesPlanTests.CHECKS

    def export(self, root: Path, selection: dict | None, info: dict | None = None) -> tuple[dict, Path]:
        tree = entry_gates_tests._tree(root, self.CHECKS)
        manifest = eg.plan_gates(tree, profile="full-gates", launcher=[], platform="linux")
        if selection is not None:
            manifest["selection"] = selection
        if info is not None:
            manifest["gate_scope_info"] = info
        manifest_path, summary_path = root / "manifest.json", root / "summary.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        summary_path.write_text(json.dumps(entry_gates_tests._summary_for(manifest)), encoding="utf-8")
        out = root / "out"
        entry = eg.export_records(manifest_path, summary_path, out, source={"commit": "c" * 40, "tree_head": "c" * 40, "bundle": "/b"},
                                  subject="entry-gates-x", round_id="r1", tree=str(tree), isolation="unshare -m", host="arm64",
                                  architecture="linux/aarch64", target_version=None, bytecode_cache="/pyc")
        return entry, out

    def test_the_auto_scope_info_is_echoed_even_when_it_fell_back_to_the_whole_set(self) -> None:
        info = {"requested": "auto", "client": "codex", "applied": "full", "fallback_reason": "ValueError：测试树里找不到最近的发版 tag"}
        with tempfile.TemporaryDirectory() as directory:
            entry, out = self.export(Path(directory).resolve(), None, info)
            self.assertEqual(entry["gate_scope"], info, "回退全集不是静默的：请求、实际范围与原因都在总摘要里")
            self.assertNotIn("client_selection", entry)
            for name in ("entry-gates.json", "full-gates-summary.json", "preflight.json"):
                with self.subTest(name):
                    self.assertEqual(json.loads((out / name).read_text(encoding="utf-8"))["gate_scope"], info)

    def test_the_full_gates_and_preflight_records_say_they_are_a_client_plan(self) -> None:
        selection = uo.seal({"schema_version": uo.SELECTION_SCHEMA, "client": "codex", "scope": "client-plan", "include_claude": False,
                             "ownership_sha256": "o" * 64, "selector_sha256": "s" * 64, "base_commit": "b" * 40, "head_commit": "c" * 40,
                             "changed_files_count": 0, "changed_files_sha256": "f" * 64,
                             "modules": {"test_a": "run", "test_b": "skip"}, "pulled": [], "advisories": [], "unclassified": [],
                             "fail_closed": [], "fallback_reasons": []})
        with tempfile.TemporaryDirectory() as directory:
            entry, out = self.export(Path(directory).resolve(), selection)
            echoed = entry["client_selection"]
            self.assertEqual((echoed["gate_scope"], echoed["modules_total"], echoed["modules_skipped"], echoed["selection_sha256"]),
                             ("client-plan", 2, 1, selection["selection_sha256"]))
            for name in ("full-gates-summary.json", "preflight.json"):
                with self.subTest(name):
                    self.assertEqual(json.loads((out / name).read_text(encoding="utf-8"))["client_selection"], echoed)
            evidence = json.loads((out / "p0" / "test-capture-tools.json").read_text(encoding="utf-8"))
            self.assertIn("按客户端筛选，只执行 codex 计划的测试模块（共 2 个，跳过 1 个、并入 0 个），不是测试组全集", evidence["executed_as"],
                          "P0 证据如实写明这是子集（自由文本字段，字段集合不变）")
            self.assertIn(selection["selection_sha256"][:12], evidence["executed_as"])

    def test_without_a_selection_the_records_are_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry, out = self.export(Path(directory).resolve(), None)
            evidence = json.loads((out / "p0" / "test-capture-tools.json").read_text(encoding="utf-8"))
            self.assertNotIn("筛选", evidence["executed_as"])
            self.assertIn("测试组全集", evidence["executed_as"])
            self.assertNotIn("client_selection", entry)
            for name in ("full-gates-summary.json", "preflight.json", "entry-gates.json"):
                payload = json.loads((out / name).read_text(encoding="utf-8"))
                self.assertNotIn("client_selection", payload)
                self.assertNotIn("gate_scope", payload)


class EntryGatesScopeScriptTests(unittest.TestCase):
    """``entry-gates.sh`` 的筛选参数解析与互斥校验：把脚本里的参数解析段原样截出来在 bash 里跑（不跑门禁本身），与声明级采样透传测试同一做法。"""

    SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh"
    BASE = "b" * 40

    def parse(self, *args: str, client: str = "codex") -> tuple[int, list[str], str]:
        text = self.SCRIPT.read_text(encoding="utf-8")
        begin = text.index('PROFILE=entry; OUT="";')
        segment = text[begin:text.index('for file in "$POLICY_ACTIVATION" "$PRE_A3_CERTIFICATION"; do', begin)]
        snippet = (f'set -Eeuo pipefail\nusage() {{ echo "用法" >&2; }}\nD=/data; HISTORY_TEST_TREE=/hist; CT_CLIENT={client}\n' + segment +
                   'for item in ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"}; do printf "[%s]" "$item"; done; printf "\\n"\n')
        completed = subprocess.run(["bash", "-c", snippet, "entry-gates", *args, "/bundle", "codex/x", "c" * 40], capture_output=True, text=True,
                                   timeout=30, env={**os.environ, "CLAUDE_ENTRY_FULL79_SPEC": "", "CLAUDE_ENTRY_FULL79_SPEC_SHA256": ""})
        self.stdout = completed.stdout
        scope = [] if not completed.stdout.strip() else __import__("re").findall(r"\[([^]]*)\]", completed.stdout.splitlines()[-1])
        return completed.returncode, scope, completed.stderr

    def test_auto_hands_the_client_and_an_optional_base_to_the_plan(self) -> None:
        for profile in ("entry", "full-gates", "preflight"):
            with self.subTest(profile=profile):
                code, scope, stderr = self.parse("--profile", profile, "--gate-scope", "auto")
                self.assertEqual((code, scope), (0, ["--gate-scope", "auto", "--client", "codex"]), stderr)
        code, scope, _stderr = self.parse("--gate-scope", "auto", "--select-base", self.BASE, "--include-claude")
        self.assertEqual((code, scope), (0, ["--gate-scope", "auto", "--client", "codex", "--select-base", self.BASE, "--include-claude"]))

    def test_auto_keeps_the_whole_set_quietly_when_the_flow_needs_it(self) -> None:
        cases = {
            "Claude": (("--gate-scope", "auto"), {"client": "claude"}, "本版只启用 codex 计划"),
            "pre-a3 组合": (("--profile", "pre-a3", "--gate-scope", "auto"), {}, "没有采集工具测试组"),
            "定向回归组合": (("--profile", "regression", "--gate-scope", "auto"), {}, "没有采集工具测试组"),
            "全集通过": (("--gate-scope", "auto", "--full-set-request", "/r.json"), {}, "模式或请求要求全集"),
            "逐单元承接": (("--gate-scope", "auto", "--unit-inherit-request", "/r.json"), {}, "模式或请求要求全集"),
            "读集审计": (("--gate-scope", "auto", "--audit-reads"), {}, "读集审计"),
            "读集合同": (("--gate-scope", "auto", "--audit-reads", "--audit-strict", "--audit-policy", "/p.json"), {}, "读集审计"),
            "收窄提案": (("--gate-scope", "auto", "--audit-reads", "--audit-strict", "--input-scope", "/s.json"), {}, "读集审计"),
        }
        for name, (options, extra, message) in cases.items():
            with self.subTest(name):
                code, scope, stderr = self.parse(*options, **extra)
                self.assertEqual((code, scope), (0, []), stderr)
                self.assertIn("按客户端筛选（auto）未启用，保持全集", self.stdout)
                self.assertIn(message, self.stdout)

    def test_the_vc0_preflight_script_hands_auto_to_codex_and_leaves_other_clients_alone(self) -> None:
        text = (self.SCRIPT.parent / "vc0-gate-target.sh").read_text(encoding="utf-8")
        line = next(item for item in text.splitlines() if item.startswith("GATE_SCOPE_ARGS=()"))
        call = next(item for item in text.splitlines() if item.startswith('bash "$DRV/entry-gates.sh" --profile preflight'))
        self.assertIn('${GATE_SCOPE_ARGS[@]+"${GATE_SCOPE_ARGS[@]}"}', call)
        self.assertLess(text.index(line), text.index(call))
        for client, expected in (("codex", ["--gate-scope", "auto"]), ("claude", [])):
            with self.subTest(client=client):
                snippet = (f'set -Eeuo pipefail\nCT_CLIENT={client}\n{line}\n'
                           'for item in ${GATE_SCOPE_ARGS[@]+"${GATE_SCOPE_ARGS[@]}"}; do printf "[%s]" "$item"; done\n')
                completed = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=30)
                self.assertEqual((completed.returncode, __import__("re").findall(r"\[([^]]*)\]", completed.stdout)), (0, expected), completed.stderr)

    def test_auto_still_rejects_a_malformed_base(self) -> None:
        code, scope, stderr = self.parse("--gate-scope", "auto", "--select-base", "HEAD~1")
        self.assertEqual((code, scope), (2, []))
        self.assertIn("必须是 40 位小写提交", stderr)

    def test_the_default_passes_nothing_to_the_plan(self) -> None:
        self.assertEqual(self.parse()[:2], (0, []))
        self.assertEqual(self.parse("--gate-scope", "full")[:2], (0, []))
        self.assertEqual(self.parse("--mode", "full-set-pass")[:2], (0, []), "没有全集请求的 full-set-pass 本来就回退成 re-execute")

    def test_client_plan_hands_the_base_and_the_client_to_the_plan(self) -> None:
        for profile in ("entry", "full-gates", "preflight"):
            with self.subTest(profile=profile):
                code, scope, stderr = self.parse("--profile", profile, "--gate-scope", "client-plan", "--select-base", self.BASE)
                self.assertEqual((code, scope), (0, ["--gate-scope", "client-plan", "--select-base", self.BASE, "--client", "codex"]), stderr)
        code, scope, _stderr = self.parse("--gate-scope", "client-plan", "--select-base", self.BASE, "--include-claude")
        self.assertEqual((code, scope[-1]), (0, "--include-claude"))
        self.assertEqual(self.parse("--gate-scope", "client-plan", "--select-base", self.BASE, "--bind-only")[0], 0, "沿用绑定的现算也认筛选")

    def test_invalid_or_conflicting_options_exit_2_before_anything_runs(self) -> None:
        cases = {
            "缺基点": (("--gate-scope", "client-plan"), "必须同时给 --select-base"),
            "基点不是 40 位": (("--gate-scope", "client-plan", "--select-base", "HEAD~1"), "必须同时给 --select-base"),
            "没有筛选却给基点": (("--select-base", self.BASE), "只在 --gate-scope client-plan／auto 下有意义"),
            "没有筛选却给手动开关": (("--include-claude",), "只在 --gate-scope client-plan／auto 下有意义"),
            "未知范围": (("--gate-scope", "everything"), "未知的门禁范围"),
            "pre-a3 组合": (("--profile", "pre-a3", "--gate-scope", "client-plan", "--select-base", self.BASE), "只用于含采集工具测试组的全量组合"),
            "定向回归组合": (("--profile", "regression", "--gate-scope", "client-plan", "--select-base", self.BASE), "只用于含采集工具测试组的全量组合"),
            "全集请求": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--full-set-request", "/r.json"), "只许 --mode re-execute"),
            "逐单元承接": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--unit-inherit-request", "/r.json"), "只许 --mode re-execute"),
            "逐单元承接提案": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--unit-inherit-propose", "/not/exist/proposal"), "只许 --mode re-execute"),
            "读集审计": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--audit-reads"), "不能与读集审计"),
            "读集合同": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--audit-reads", "--audit-strict", "--audit-policy", "/p.json"), "不能与读集审计"),
            "收窄提案": (("--gate-scope", "client-plan", "--select-base", self.BASE, "--audit-reads", "--audit-strict", "--input-scope", "/s.json"), "不能与读集审计"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                code, scope, stderr = self.parse(*options)
                self.assertEqual((code, scope), (2, []), stderr)
                self.assertIn(message, stderr)


class RealTreeDiscoveryTests(unittest.TestCase):
    def test_the_executors_discovered_modules_are_exactly_the_ownership_table(self) -> None:
        """选择的模块表必须恰好等于执行器在真实测试树里 discover 出的模块（去掉 +pkg）：否则执行器失败关闭，整轮 ARM64 门禁起不来。"""

        # 用 auto 模式实际调用的函数（执行器 plan 子命令在真实测试树里 discover），保证 auto 的核对和执行器开跑时的核对是同一件事。
        self.assertEqual(sorted(eg.executor_modules(REPO_ROOT)), sorted(uo.load_data(REPO_ROOT)["modules"]))


if __name__ == "__main__":
    unittest.main()
