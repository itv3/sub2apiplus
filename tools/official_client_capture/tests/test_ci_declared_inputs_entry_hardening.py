"""入口层（``tools/ci/entry_gates.py``）声明级承接登记表自检的加固（步骤 5 WP3b，独立验收报告「一般」缺陷 2、5、7 与健壮性、WP1e 交叉核对）。

* **一般 2：make -n 展开探测并非只读**。GNU Make 下，带 ``+`` 前缀的配方行、含 ``$(MAKE)`` 的配方行（递归 make 继续传 -n，所以不被拦住），
  以及重建 makefile 自身的规则，都会在 ``make -n`` 下真的执行。扫描补上这三类，命中的单元转 rerun-only；执行期的两类只看声明目标的先决条件闭包里
  的配方行（真实仓库的 ``$(MAKE)`` 只在 build-backend、test-backend、test-frontend 里，不能因此挡住所有展开合同），闭包解析不了就退回整份文件。
  「保证只读」的说法改成准确的「保守扫描才允许探测」。
* **一般 5：证据与登记表的路径检查只拒绝最后一级符号链接**。从测试树到文件的每一级都不许是符号链接，resolve 之后仍在测试树内。
* **一般 7：make_expansion 不要求命令是 make**。声明了 make 展开合同的单元，命令（去掉启动前缀）的第一个词必须是 make。
* **健壮性**：登记表与证据文件的 JSON 嵌套过深时，统一报清晰的 ValueError（过去 RecursionError 原样冒出来）。
* **WP1e 交叉核对**：记录层按合同路径绑定 installed_dirs 前缀的常量表，必须与 DECLARED_CACHES 里 dependency-install 合同的 path 同源。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ci import entry_gates as eg
from tools.official_client_capture.tests import test_ci_declared_inputs_entry as et

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKE = shutil.which("make")


def is_gnu_make() -> bool:
    if MAKE is None:
        return False
    completed = subprocess.run([MAKE, "--version"], capture_output=True, text=True, timeout=30)
    return completed.returncode == 0 and "GNU Make" in completed.stdout


def write_tree(root: Path, makefile: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Makefile").write_text(makefile, encoding="utf-8")
    return root


class MakeScanPremiseTests(unittest.TestCase):
    """前提：这三类写法真的会在 make -n 下执行（用真实的 GNU Make 演示，各在自己的临时目录里）。"""

    def run_dry(self, files: dict[str, str], target: str = "t") -> list[str]:
        """在临时目录里跑 make -n，返回跑完之后目录里多出来的 marker* 文件名。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            completed = subprocess.run([MAKE, "-s", "--no-print-directory", "-n", target], cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return sorted(path.name for path in root.iterdir() if path.name.startswith("marker"))

    @unittest.skipUnless(is_gnu_make(), "需要 GNU Make")
    def test_a_plus_recipe_line_runs_under_dry_run(self) -> None:
        self.assertEqual(self.run_dry({"Makefile": "t:\n\t+touch marker\n"}), ["marker"])
        self.assertEqual(self.run_dry({"Makefile": "t:\n\t@touch marker\n"}), [], "对照：没有 + 的配方行只打印、不执行")

    @unittest.skipUnless(is_gnu_make(), "需要 GNU Make")
    def test_a_recursive_make_runs_its_plus_lines_under_dry_run(self) -> None:
        files = {"Makefile": "t:\n\t@$(MAKE) -f sub.mk\n", "sub.mk": "u:\n\t+touch marker\n"}
        self.assertEqual(self.run_dry(files), ["marker"])

    @unittest.skipUnless(is_gnu_make(), "需要 GNU Make")
    def test_a_rule_that_rebuilds_the_makefile_runs_under_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "Makefile").write_text("Makefile: gen.in\n\ttouch marker\nt:\n\t@echo hi\n", encoding="utf-8")
            (root / "gen.in").write_text("新\n", encoding="utf-8")
            os.utime(root / "Makefile", (1, 1))   # Makefile 比 gen.in 旧：make 启动时先重建它
            subprocess.run([MAKE, "-s", "--no-print-directory", "-n", "t"], cwd=root, capture_output=True, text=True, timeout=60)
            self.assertTrue((root / "marker").exists())


class MakeScanTests(unittest.TestCase):
    """扫描补上的三类写法：整份文件（没给目标）与按目标闭包两种口径。"""

    def problem(self, makefile: str, targets: tuple[str, ...] | None = None) -> str | None:
        with tempfile.TemporaryDirectory() as directory:
            return eg._declared_make_problem(write_tree(Path(directory), makefile), targets)

    WRITINGS = {
        "加号配方行": "t:\n\t+touch marker\n",
        "加号夹在 @ 后面": "t:\n\t@+touch marker\n",
        "加号在 @ 前面": "t:\n\t+@touch marker\n",
        "加号夹在 - 后面": "t:\n\t-+touch marker\n",
        "加号前有空格": "t:\n\t  + touch marker\n",
        "递归 make（圆括号）": "t:\n\t@$(MAKE) -f sub.mk\n",
        "递归 make（花括号）": "t:\n\t${MAKE} -C sub\n",
        "递归 make 在行中间": "t:\n\t@echo x && $(MAKE) u\n",
    }
    SELF_REBUILD = {
        "Makefile 自重建": "Makefile: gen.in\n\ttouch Makefile\nt:\n\t@echo hi\n",
        "makefile 自重建": "makefile: gen.in\n\ttouch makefile\nt:\n\t@echo hi\n",
        "GNUmakefile 自重建": "GNUmakefile: gen.in\n\ttouch GNUmakefile\nt:\n\t@echo hi\n",
        "带 ./ 前缀": "./Makefile: gen.in\n\ttouch Makefile\nt:\n\t@echo hi\n",
        "目标列表里有 Makefile": "all Makefile: gen.in\n\ttouch x\nt:\n\t@echo hi\n",
        "双冒号规则": "Makefile:: gen.in\n\ttouch x\nt:\n\t@echo hi\n",
    }

    def test_every_always_run_writing_is_refused_on_the_whole_file_scan(self) -> None:
        for label, makefile in self.WRITINGS.items():
            with self.subTest(label):
                found = self.problem(makefile)
                self.assertIsNotNone(found)
                self.assertIn("make -n 下仍会真的执行", found)
                self.assertIn("make -n 展开探测不是只读，不能用 make 展开合同，改为只重跑", found)

    def test_every_always_run_writing_is_refused_for_the_target_that_runs_it(self) -> None:
        for label, makefile in self.WRITINGS.items():
            with self.subTest(label):
                found = self.problem(makefile, ("t",))
                self.assertIsNotNone(found)
                self.assertIn("声明目标 t 的先决条件闭包里", found)

    def test_a_self_rebuilding_makefile_is_refused_whatever_the_declared_targets(self) -> None:
        for label, makefile in self.SELF_REBUILD.items():
            for targets in (None, ("t",)):
                with self.subTest(label, targets=targets):
                    found = self.problem(makefile, targets)
                    self.assertIsNotNone(found)
                    self.assertIn("重建 makefile 自身的规则", found)

    def test_recipe_prefix_changes_are_refused_because_recipe_lines_become_unrecognisable(self) -> None:
        found = self.problem(".RECIPEPREFIX = >\nt:\n>+touch marker\n", ("t",))
        self.assertIsNotNone(found)
        self.assertIn(".RECIPEPREFIX", found)

    def test_lookalikes_are_not_refused(self) -> None:
        harmless = (
            "MAKEFLAGS_NOTE := not a variable reference\n"
            "t:\n\t@echo $(MAKECMDGOALS) $(MAKEFILE_LIST) $(MAKELEVEL)\n"      # MAKE 开头的别的变量不是 $(MAKE)
            "\t@x=$$(echo hi); echo $$x\n"
            "\t@echo a+b +c\n"                                                   # 加号不在行首前缀里
            "\t@echo $$((1 + 2))\n"
            "Makefile.bak: x\n\t@echo not the makefile\n"                       # 名字只是长得像
            "makefile-notes := 1\n"
            "# \t+touch comment-only\n"
            "# $(MAKE) in a comment\n"
            "all: t\n")
        for targets in (None, ("t",), ("all",)):
            with self.subTest(targets=targets):
                self.assertIsNone(self.problem(harmless, targets))
        self.assertIsNone(self.problem("MAKE_JOBS := 4\nt:\n\t@echo $(MAKE_JOBS)\n"), "$(MAKE_JOBS) 不是 $(MAKE)")

    def test_only_the_closure_of_the_declared_targets_counts_for_the_execution_phase_writings(self) -> None:
        makefile = ("FRONTEND_LIST := \\\n\ta \\\n\tb\n"
                    "test-frontend-critical:\n\t@pnpm exec vitest run $(FRONTEND_LIST)\n"
                    "build-backend:\n\t@$(MAKE) -C backend build\n"
                    "deploy:\n\t+scripts/deploy.sh\n"
                    "test-frontend: prepare\n\t@$(MAKE) test-frontend-critical\n"
                    "prepare:\n\t@echo prepare\n"
                    "DEPENDENCIES := prepare build-backend\n"
                    "everything: test-frontend-critical $(DEPENDENCIES)\n\t@echo all\n"
                    ".PHONY: test-frontend-critical build-backend deploy test-frontend prepare everything\n")
        self.assertIsNone(self.problem(makefile, ("test-frontend-critical",)), "无关目标里的 $(MAKE) 与加号行不挡住声明目标")
        self.assertIsNone(self.problem(makefile, ("prepare",)))
        self.assertIsNotNone(self.problem(makefile), "没给目标＝整份文件：无关目标里的也算")
        for target, fragment in (("build-backend", "$(MAKE) -C backend build"), ("deploy", "+scripts/deploy.sh"), ("test-frontend", "$(MAKE) test-frontend-critical")):
            with self.subTest(target):
                found = self.problem(makefile, (target,))
                self.assertIsNotNone(found)
                self.assertIn(fragment, found)
        # 先决条件里引用变量（make 在读规则时就展开它，所以变量必须定义在规则之前）：DEPENDENCIES 要展开，闭包因此包含 build-backend。
        found = self.problem(makefile, ("everything",))
        self.assertIsNotNone(found)
        self.assertIn("$(MAKE) -C backend build", found)
        # 多个声明目标：任何一个的闭包里有就算。
        self.assertIsNotNone(self.problem(makefile, ("test-frontend-critical", "deploy")))

    def test_an_unparsable_makefile_falls_back_to_the_whole_file_instead_of_missing_a_recipe(self) -> None:
        unrelated = "t:\n\t@echo ok\nu:\n\t@$(MAKE) elsewhere\n"
        self.assertIsNone(self.problem(unrelated, ("t",)), "解析得了：按闭包")
        for label, extra in {"模式规则": "%.o: %.c\n\t@echo compile\n", "静态模式规则": "a.o b.o: %.o: %.c\n\t@echo compile\n",
                             "先决条件里的函数": "x: $(sort b a)\n", "没定义的变量": "x: $(UNDEFINED_VARIABLE)\n",
                             "?= 变量被先决条件引用": "WEAK ?= a\nx: $(WEAK)\n", ".DEFAULT": ".DEFAULT:\n\t@echo default\n",
                             "后缀规则": ".c.o:\n\t@echo compile\n", "先决条件带通配符": "x: *.c\n"}.items():
            with self.subTest(label):
                found = self.problem(unrelated + extra, ("t",))
                self.assertIsNotNone(found, "闭包解析不了：退回整份文件，无关目标里的 $(MAKE) 也算（失败关闭）")
                self.assertNotIn("先决条件闭包", found)
        # ?= 变量只要没被目标名或先决条件引用就不影响解析。
        self.assertIsNone(self.problem("WEAK ?= a\n" + unrelated, ("t",)))

    def test_define_blocks_are_part_of_every_scan(self) -> None:
        """define 宏体展开出的行也可能带 +，不管哪个目标引用它，宏体都在扫描范围内。"""

        makefile = "define BODY\n+touch marker\nendef\nt:\n\t$(BODY)\n"
        self.assertIsNotNone(self.problem(makefile, ("t",)))
        self.assertIsNotNone(self.problem(makefile))

    def test_the_real_repository_makefile_keeps_its_unrelated_targets_from_blocking_expansions(self) -> None:
        """真实仓库的 Makefile：$(MAKE) 只在 build-backend、test-backend、test-frontend 里。按目标扫描时，前端关键集合与各子检查目标仍可做展开合同；
        用 $(MAKE) 的目标被正确挡住；整份文件扫描（没给目标）会因为这几处挡住全部——所以 plan 一定是按目标扫描。"""

        self.assertIsNone(eg._declared_make_problem(REPO_ROOT, ("test-frontend-critical",)))
        self.assertIsNone(eg._declared_make_problem(REPO_ROOT, ("check-egress-spec",)))
        for target in ("test-frontend", "test-backend", "build-backend", "test"):
            with self.subTest(target):
                self.assertIn("make -n 下仍会真的执行", eg._declared_make_problem(REPO_ROOT, (target,)) or "")
        self.assertIn("make -n 下仍会真的执行", eg._declared_make_problem(REPO_ROOT) or "")


class MakeScanPlanTests(unittest.TestCase):
    """plan 接线：同一次计划里，只有闭包里有这些写法的单元转 rerun-only，其余声明了展开合同的单元照常。"""

    MAKEFILE = et.makefile_text() + "test-capture-tools-prerequisites:\n\t@$(MAKE) -C tools prerequisites\n"

    def test_only_the_unit_that_runs_the_writing_is_converted(self) -> None:
        world = et.World(self, makefile=self.MAKEFILE)
        world.declare("frontend:critical", et.default_scope(contracts=("toolchain:go", "toolchain:make"), make_expansion=("test-frontend-critical",)))
        world.declare("capture:prerequisites", et.default_scope(contracts=("toolchain:go", "toolchain:make"),
                                                                make_expansion=("test-capture-tools-prerequisites",)))
        world.write()
        manifest = world.plan()
        units = et.inputs_of(manifest)
        self.assertEqual(set(units["frontend:critical"]["inputs"]), {"declared_scope", "declared_sample"}, "闭包干净：照常 declared")
        self.assertIs(units["capture:prerequisites"]["inheritable"], False)
        self.assertIn("声明目标 test-capture-tools-prerequisites 的先决条件闭包里", units["capture:prerequisites"]["not_inheritable_reason"])
        self.assertEqual(set(manifest["declared_inputs"]["converted_to_rerun_only"]), {"capture:prerequisites"})
        self.assertEqual((manifest["declared_inputs"]["declared"], manifest["declared_inputs"]["rerun_only"]), (1, 1))

    def test_a_self_rebuilding_makefile_converts_every_unit_with_an_expansion_contract(self) -> None:
        # plan 阶段自己会跑一次 make print-egress-spec-checks：gen.in 必须存在（它比 Makefile 新，make 会先执行 touch Makefile 重建它，在临时树里无害）。
        world = et.World(self, makefile=et.makefile_text(extra="Makefile: gen.in\n\ttouch Makefile\n"), extra_files={"gen.in": "输入\n"})
        world.declare("frontend:critical", et.default_scope(contracts=("toolchain:go", "toolchain:make"), make_expansion=("test-frontend-critical",)))
        world.declare("backend:go-test")
        world.write()
        manifest = world.plan()
        units = et.inputs_of(manifest)
        self.assertIs(units["frontend:critical"]["inheritable"], False)
        self.assertIn("重建 makefile 自身的规则", units["frontend:critical"]["not_inheritable_reason"])
        self.assertEqual(set(units["backend:go-test"]["inputs"]), {"declared_scope", "declared_sample"}, "没有展开合同的单元不受 Makefile 影响")

    def test_the_probe_clears_the_make_environment_that_could_change_the_expansion(self) -> None:
        runner = et.FakeRunner({("make", "-s", "--no-print-directory", "-n", "test-frontend-critical"): (0, "echo hi\n")})
        environ = {"PATH": "/x", "MAKEFLAGS": "-j8 -t", "MAKEFILES": "/etc/extra.mk"}
        world = et.World(self)
        scope = et.default_scope(contracts=(), make_expansion=("test-frontend-critical",))
        eg.declared_probe(runner=runner, environ=environ, tree=world.tree).entries(et.declaration(scope))
        self.assertEqual(runner.calls[0][2], {"PATH": "/x", "MAKEFLAGS": "", "MAKEFILES": ""}, "调用方环境里的 -j、-t 与额外 makefile 不进展开")
        self.assertEqual(environ["MAKEFLAGS"], "-j8 -t", "不改调用方传进来的环境")


class PathContainmentTests(unittest.TestCase):
    """一般 5：从测试树到文件的每一级都不许是符号链接，resolve 之后仍在测试树内。"""

    def declared_world(self) -> et.World:
        world = et.World(self)
        world.declare("backend:go-test")
        world.write()
        world.plan()   # 先确认干净的树是通过的
        return world

    def test_a_symlinked_samples_directory_is_refused(self) -> None:
        world = self.declared_world()
        samples = world.tree / eg.DECLARED_SAMPLES_DIR
        outside = world.root / "outside-samples"
        samples.rename(outside)
        samples.symlink_to(outside, target_is_directory=True)
        self.assertTrue((samples / "backend-go-test.sample.json").is_file(), "符号链接之后证据文件照样读得到——所以必须靠路径检查拦")
        with self.assertRaisesRegex(ValueError, "证据路径不在测试树的真实目录内.*符号链接"):
            world.plan()

    def test_a_samples_directory_that_links_to_another_place_inside_the_tree_is_refused_too(self) -> None:
        world = self.declared_world()
        samples = world.tree / eg.DECLARED_SAMPLES_DIR
        inside = world.tree / "docs" / "real-samples"
        inside.parent.mkdir(parents=True, exist_ok=True)
        samples.rename(inside)
        samples.symlink_to(inside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "证据路径不在测试树的真实目录内"):
            world.plan()

    def test_a_symlinked_tools_ci_directory_hides_the_registry_and_is_refused(self) -> None:
        world = self.declared_world()
        ci = world.tree / "tools" / "ci"
        outside = world.root / "outside-ci"
        ci.rename(outside)
        ci.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "登记表的路径不在测试树的真实目录内.*符号链接"):
            world.plan()

    def test_the_helper_reports_the_first_symlinked_level_and_leaves_the_trees_own_ancestors_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real = root / "real"
            (real / "tree" / "a" / "b").mkdir(parents=True)
            (real / "tree" / "a" / "b" / "f.json").write_text("{}", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            tree = alias / "tree"   # 测试树自己的上级目录是符号链接：不查（临时目录常见，例如 macOS 的 /var）
            self.assertIsNone(eg._declared_tree_path_problem(tree, tree / "a" / "b" / "f.json"))
            (real / "tree" / "link").symlink_to(real / "tree" / "a", target_is_directory=True)
            problem = eg._declared_tree_path_problem(tree, tree / "link" / "b" / "f.json")
            self.assertIsNotNone(problem)
            self.assertIn("link", problem)
            self.assertIn("符号链接", problem)
            self.assertIsNone(eg._declared_tree_path_problem(tree, root / "elsewhere" / "registry.json"), "不在测试树之下的路径不归这里管")
            self.assertIsNone(eg._declared_tree_path_problem(tree, tree / "missing" / "f.json"), "还不存在的路径不是问题")

    def test_an_explicit_registry_outside_the_tree_is_still_allowed(self) -> None:
        """命令行 --declared-registry 显式指定的外部登记表（测试用）保持原行为；只拒绝显式路径本身是符号链接。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "tree"
            tree.mkdir()
            registry = root / "elsewhere.json"
            registry.write_text(json.dumps({"schema_version": eg.DECLARED_REGISTRY_SCHEMA, "units": {}}), encoding="utf-8")
            self.assertEqual(eg.load_declared_registry(tree, path=registry)["units"], {})
            link = root / "link.json"
            link.symlink_to(registry)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                eg.load_declared_registry(tree, path=link)


class MakeCommandTests(unittest.TestCase):
    """一般 7：声明了 make 展开合同的单元，命令（去掉启动前缀）的第一个词必须是 make。"""

    SCOPE = et.default_scope(contracts=("toolchain:go", "toolchain:make"), make_expansion=("test-frontend-critical",))

    def test_a_make_command_may_have_an_expansion_contract(self) -> None:
        world = et.World(self)
        self.assertEqual(world.command_of("frontend:critical")[0][0], "make")
        world.declare("frontend:critical", self.SCOPE)
        world.write()
        self.assertEqual(world.plan()["declared_inputs"]["declared_units"], ["frontend:critical"])

    def test_a_non_make_command_may_not_(self) -> None:
        world = et.World(self)
        self.assertEqual(world.command_of("backend:go-test")[0][0], "go")
        world.declare("backend:go-test", self.SCOPE)
        world.write()
        with self.assertRaisesRegex(ValueError, "backend:go-test.*声明了 make 展开合同，但单元命令的第一个词是 'go' 而不是 make"):
            world.plan()

    def test_the_first_word_is_judged_after_the_launcher_and_by_its_basename(self) -> None:
        world = et.World(self)
        unit = world.units["frontend:critical"]
        self.assertEqual(unit["argv"][:len(world.launcher)], world.launcher, "启动前缀在 argv 前面，不算命令的第一个词")
        for first, accepted in (("make", True), ("/usr/bin/make", True), ("gmake", False), ("env", False), ("sh", False), ("makefile", False)):
            with self.subTest(first=first):
                world.units["frontend:critical"] = {**unit, "argv": [*world.launcher, first, "--no-print-directory", "test-frontend-critical"]}
                world.registry["units"].clear()
                world.declare("frontend:critical", self.SCOPE)
                units = list(world.units.values())
                if accepted:
                    eg.validate_declared_registry(world.registry, units, world.tree, launcher=world.launcher)
                else:
                    with self.assertRaisesRegex(ValueError, "不是 make|而不是 make"):
                        eg.validate_declared_registry(world.registry, units, world.tree, launcher=world.launcher)

    def test_contract_errors_still_come_first_and_units_without_an_expansion_are_not_affected(self) -> None:
        world = et.World(self)
        world.declare("backend:go-test", et.default_scope(contracts=("toolchain:go",), make_expansion=("test-frontend-critical",)))
        world.write()
        with self.assertRaisesRegex(ValueError, "toolchain:make"):
            world.plan()   # 缺 toolchain:make 的报错在先，原有的拒绝文案不变
        world.registry["units"].clear()
        world.declare("backend:go-test", et.default_scope(contracts=("toolchain:go",)))
        world.write()
        self.assertEqual(world.plan()["declared_inputs"]["declared_units"], ["backend:go-test"])


class JsonDepthTests(unittest.TestCase):
    """登记表与证据文件的 JSON 嵌套过深时，统一报清晰的 ValueError（过去 RecursionError 原样冒出来）。"""

    DEEP = "[" * 200000 + "]" * 200000

    def test_a_deeply_nested_registry_is_a_clear_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory).resolve()
            path = tree / eg.DECLARED_REGISTRY_PATH
            path.parent.mkdir(parents=True)
            path.write_text(self.DEEP, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "登记表的 JSON 嵌套过深"):
                eg.load_declared_registry(tree)
            path.write_text('{"schema_version": "' + eg.DECLARED_REGISTRY_SCHEMA + '", "units": ' + self.DEEP + "}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "登记表的 JSON 嵌套过深"):
                eg.load_declared_registry(tree)

    def test_a_deeply_nested_evidence_file_is_a_clear_value_error(self) -> None:
        world = et.World(self)
        entry = world.declare("backend:go-test")
        (world.tree / entry["sample"]["evidence"]).write_text(self.DEEP, encoding="utf-8")
        world.write()
        with self.assertRaisesRegex(ValueError, "backend:go-test.*证据文件 JSON 嵌套过深"):
            world.plan()

    def test_ordinary_malformed_json_keeps_its_old_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory).resolve()
            path = tree / eg.DECLARED_REGISTRY_PATH
            path.parent.mkdir(parents=True)
            path.write_text("{不是 json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "登记表读不出来或不是 JSON"):
                eg.load_declared_registry(tree)
        world = et.World(self)
        entry = world.declare("backend:go-test")
        (world.tree / entry["sample"]["evidence"]).write_text("{不是 json", encoding="utf-8")
        world.write()
        with self.assertRaisesRegex(ValueError, "证据文件不是 JSON"):
            world.plan()


class InstallPrefixCrossCheckTests(unittest.TestCase):
    """WP1e 交叉核对：记录层按合同路径绑定 installed_dirs 前缀的常量表，必须与入口层 DECLARED_CACHES 里 dependency-install 合同的 path 同源。
    常量表在 WP1e 合入后叫 ``_DECLARED_INSTALLED_PREFIXES``（合同 ID → 安装目录前缀）；其它写法留作兜底，找不到任何一个名字时本用例跳过。"""

    NAMES = ("_DECLARED_INSTALLED_PREFIXES", "_DECLARED_INSTALL_PREFIXES", "DECLARED_INSTALL_PREFIXES")

    def table(self) -> dict[str, str]:
        records = eg._unit_records_module()
        for name in self.NAMES:
            value = getattr(records, name, None)
            if value is not None:
                return dict(value)
        self.skipTest(f"unit_records 还没有 {' / '.join(self.NAMES)}（WP1e 合入之前）：合入之后本用例自动生效")
        return {}

    def test_the_prefix_table_agrees_with_the_dependency_install_contracts(self) -> None:
        """入口层每个 dependency-install 合同的 ``path + "/"`` 必须等于记录层前缀表里这个合同对应的值。表的键可能写成缓存名本身，也可能带 ``cache:`` 前缀
        （以 WP1e 实际合入为准），两种都认；找不到对应的键直接报错并列出表里的键，免得形式对不上时悄悄漏检。"""

        table = self.table()
        checked = 0
        for name, definition in eg.DECLARED_CACHES.items():
            if definition["role"] != "dependency-install":
                continue
            key = next((candidate for candidate in (name, f"cache:{name}") if candidate in table), None)
            self.assertIsNotNone(key, f"前缀表里找不到依赖安装合同 {name}（表的键：{sorted(table)}）")
            self.assertEqual(table[key], definition["path"] + "/", name)
            checked += 1
        self.assertGreaterEqual(checked, 1, "入口层至少有 node-modules 这个依赖安装合同")

    def test_every_dependency_install_contract_has_a_path_and_no_other_role_does_not_need_one(self) -> None:
        for name, definition in eg.DECLARED_CACHES.items():
            if definition["role"] == "dependency-install":
                self.assertTrue(definition.get("path"), name)
                self.assertFalse(definition["path"].endswith("/"), f"{name}：path 不带尾斜杠，前缀由 path + '/' 得到")


if __name__ == "__main__":
    unittest.main()
