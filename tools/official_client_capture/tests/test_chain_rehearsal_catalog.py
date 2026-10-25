"""第 13 项：整链空跑登记目录的自检（本机可跑，不执行任何真实链）。

目录是人写的数据，最容易出的错是：探针引用的用例名写错（引擎会当成“探针出错”，但那要等真跑才发现）、修复补丁
在受管文件改动之后悄悄失效、新增了真实链却没登记进空跑。这些都在这里提前拦下。

覆盖的不变量：
1. 探针 id 唯一、能力名都已登记、前序探针存在且不成环；
2. 目录引用的每个单元用例名都能被加载（模块存在、类／方法存在）；
3. 每个反向补丁都能原样打到当前受管文件上（不然历史重放会静默变成“未执行”）；补丁夹具目录里没有无人引用的补丁；
4. 每个带注入器的探针都有缺陷签名，且签名确实出现在它引用的用例里（拼写错了签名永远命不中）；
5. Makefile 里登记的 Codex 真实链模块全部出现在目录里，目录引用的真实链都在 Makefile 里（不删除、不遗漏）。
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import chain_rehearsal as engine
from tools.official_client_capture.tests import chain_rehearsal_catalog as catalog
from tools.official_client_capture.tests import chain_rehearsal_inject as inject

REPO_ROOT = engine.REPO_ROOT
REAL_CHAIN_PREFIX = "tools.official_client_capture.tests.real_chains."
# Claude 升级处于停放状态，不属于 Codex 升级开工前的空跑范围；这些真实链不登记，也要在这里写明。
CLAUDE_REAL_CHAINS = {
    "test_claude_upgrade_control_chain", "test_claude_vc4_candidate_chain", "test_claude_vc1_recorded_chain",
    "test_claude_upgrade_fault_chains", "test_claude_candidate_recovery_projection",
}


def _registry() -> engine.Registry:
    return catalog.registry()


def _all_probes() -> list[engine.Probe]:
    registry = _registry()
    return list(registry.probes) + list(registry.chain)


def _module_of(test_id: str) -> str:
    """用例名 → 模块名（逐段试探：能导入的最长前缀就是模块）。"""

    parts = test_id.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        path = REPO_ROOT.joinpath(*parts[:end]).with_suffix(".py")
        if path.is_file():
            return candidate
    raise AssertionError(f"找不到用例对应的模块文件：{test_id}")


def _makefile_real_chain_modules() -> list[str]:
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^CAPTURE_REAL_CHAIN_MODULES\s*:=\s*(.*?)(?=^\S)", text, re.M | re.S)
    assert match is not None, "Makefile 里没有 CAPTURE_REAL_CHAIN_MODULES"
    return match.group(1).replace("\\\n", " ").split()


class CatalogShapeTest(unittest.TestCase):
    def test_registry_validates(self) -> None:
        registry = _registry()
        engine.validate_probes(list(registry.probes) + list(registry.chain), registry.checkpoints)

    def test_probe_ids_are_unique_across_probes_and_chain(self) -> None:
        ids = [probe.probe_id for probe in _all_probes()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_defect_probe_has_history_phase_and_step(self) -> None:
        for probe in catalog.DEFECT_PROBES:
            with self.subTest(probe=probe.probe_id):
                self.assertTrue(probe.history, "历史缺陷必须写明出处（提交／记忆）")
                self.assertRegex(probe.phase, r"^VC-[0-6]$")
                self.assertTrue(probe.step.strip())
                self.assertEqual(probe.kind, "defect")

    def test_the_five_named_0163_defects_are_all_registered(self) -> None:
        """任务第 13 项点名的 0.163 缺陷：容器 ID 当主机名、收尾断言漏改 Lite、Kilo 用量顺序、生成器重放登记、
        恢复预览与续跑口径不一致。"""

        ids = {probe.probe_id for probe in catalog.DEFECT_PROBES}
        for required in ("capture-host-container-id", "core-capture-lite-final-assertions", "kilo-http-usage-order",
                         "producer-replay-registration", "r17-preview-isolation-window"):
            self.assertIn(required, ids)

    def test_recovery_probes_cover_vc1_and_vc5(self) -> None:
        phases = {probe.phase for probe in catalog.RECOVERY_PROBES}
        self.assertTrue({"VC-1", "VC-5"} <= phases)
        self.assertTrue(all(probe.kind == "recovery" for probe in catalog.RECOVERY_PROBES))

    def test_chain_steps_are_ordered_dependencies_without_forward_references(self) -> None:
        seen: set[str] = set()
        for step in catalog.CHAIN_STEPS:
            self.assertTrue(set(step.requires_probes) <= seen, f"{step.probe_id} 引用了排在它后面的步骤")
            seen.add(step.probe_id)

    def test_chain_covers_vc1_through_vc6(self) -> None:
        phases = {step.phase for step in catalog.CHAIN_STEPS}
        self.assertTrue({"VC-1", "VC-5", "VC-6"} <= phases, phases)


class CatalogTestIdsTest(unittest.TestCase):
    def test_every_referenced_test_id_loads(self) -> None:
        missing = []
        for test_id in sorted(set(catalog.all_unittest_ids())):
            suite = __import__("unittest").defaultTestLoader.loadTestsFromName(test_id)
            if suite.countTestCases() == 0:
                missing.append(test_id)
                continue
            stack = [suite]
            while stack:
                item = stack.pop()
                if hasattr(item, "__iter__"):
                    stack.extend(item)
                elif item.__class__.__name__ == "_FailedTest":
                    missing.append(test_id)
        self.assertEqual(missing, [])


class CatalogInjectionTest(unittest.TestCase):
    def _patches(self) -> dict[str, tuple[engine.Probe, inject.ReversePatch]]:
        result = {}
        for probe in _registry().probes:
            for injection in probe.injections:
                if isinstance(injection, inject.ReversePatch):
                    result[injection.patch] = (probe, injection)
        return result

    def test_every_patch_fixture_is_referenced_and_every_reference_exists(self) -> None:
        referenced = set(self._patches())
        on_disk = {path.name for path in inject.FIXTURE_DIRECTORY.glob("*.patch")}
        self.assertEqual(referenced, on_disk)

    def test_every_injection_applies_to_the_current_files(self) -> None:
        """只复制注入器要改的文件（不复制整棵树），逐个注入器试打；打不上说明受管文件在修复之后又被改过，需要重新生成补丁。"""

        for probe in _registry().probes:
            for injection in probe.injections:
                with self.subTest(probe=probe.probe_id, injection=getattr(injection, "patch", getattr(injection, "path", ""))):
                    root = Path(tempfile.mkdtemp(prefix="inject-apply-"))
                    self.addCleanup(lambda root=root: shutil.rmtree(root, ignore_errors=True))
                    if isinstance(injection, inject.ReversePatch):
                        paths = [item.path for item in inject.parse_patch(inject.load_patch(injection.patch))]
                    else:
                        paths = [injection.path]
                    for relative in paths:
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(REPO_ROOT / relative, target)
                    changed = inject.inject(root, injection)
                    self.assertTrue(changed)
                    for relative in changed:
                        self.assertNotEqual((root / relative).read_bytes(), (REPO_ROOT / relative).read_bytes(),
                                            f"{relative} 注入后没有任何变化")

    def test_signatures_are_defect_phenomena_not_test_names(self) -> None:
        """M2：签名必须是只在缺陷现象里出现的片段（断言消息、缺失符号、被改坏的函数名、被拒绝的错误文本），
        不能是探针引用的用例／类／模块名——否则探针因任何原因失败都会“命中”，历史重放就成了摆设。
        实际能否命中由 ``replay-history`` 在真实注入上验证；这里守住“不得退回成测试名”。"""

        import unittest as _unittest

        for probe in _registry().probes:
            if not probe.injections:
                continue
            with self.subTest(probe=probe.probe_id):
                self.assertTrue(probe.signature, "带注入器的探针必须有缺陷签名")
                ids = list(probe.runner.test_ids) if isinstance(probe.runner, engine.UnittestRunner) else []
                names: set[str] = set()
                for test_id in ids:
                    names.update(test_id.split("."))
                    stack = [_unittest.defaultTestLoader.loadTestsFromName(test_id)]
                    while stack:
                        item = stack.pop()
                        if hasattr(item, "__iter__"):
                            stack.extend(item)
                        else:
                            names.add(item.id().split(".")[-1])
                            names.add(item.__class__.__name__)
                for fragment in probe.signature:
                    self.assertGreaterEqual(len(fragment), 6, f"签名 {fragment!r} 太短，容易误中")
                    for name in names:
                        self.assertNotIn(fragment, name, f"签名 {fragment!r} 是测试／类／模块名 {name!r} 的一部分")
                        if name.startswith("test_") or name.endswith(("Test", "Tests")):
                            self.assertNotIn(name, fragment, f"签名 {fragment!r} 含测试名 {name!r}")

    def test_replayed_defects_never_edit_test_files(self) -> None:
        for probe in _registry().probes:
            for injection in probe.injections:
                paths = ([item.path for item in inject.parse_patch(inject.load_patch(injection.patch))]
                         if isinstance(injection, inject.ReversePatch) else [injection.path])
                for path in paths:
                    self.assertNotIn("/tests/", path, f"{probe.probe_id} 的注入器改了测试文件 {path}")


class RealChainCoverageTest(unittest.TestCase):
    def test_every_codex_real_chain_in_the_makefile_is_in_the_rehearsal(self) -> None:
        makefile_modules = [item for item in _makefile_real_chain_modules() if item.startswith(REAL_CHAIN_PREFIX)]
        codex = {item[len(REAL_CHAIN_PREFIX):] for item in makefile_modules} - CLAUDE_REAL_CHAINS
        referenced = set()
        for test_id in catalog.all_unittest_ids():
            if test_id.startswith(REAL_CHAIN_PREFIX):
                referenced.add(test_id[len(REAL_CHAIN_PREFIX):].split(".")[0])
        self.assertEqual(sorted(codex - referenced), [], "Makefile 里有 Codex 真实链没登记进整链空跑")

    def test_rehearsal_only_references_chains_the_makefile_runs(self) -> None:
        makefile = {item[len(REAL_CHAIN_PREFIX):] for item in _makefile_real_chain_modules() if item.startswith(REAL_CHAIN_PREFIX)}
        referenced = {test_id[len(REAL_CHAIN_PREFIX):].split(".")[0] for test_id in catalog.all_unittest_ids()
                      if test_id.startswith(REAL_CHAIN_PREFIX)}
        self.assertEqual(sorted(referenced - makefile), [])

    def test_claude_exclusions_are_real_makefile_entries(self) -> None:
        makefile = {item[len(REAL_CHAIN_PREFIX):] for item in _makefile_real_chain_modules() if item.startswith(REAL_CHAIN_PREFIX)}
        self.assertTrue(CLAUDE_REAL_CHAINS <= makefile)


class CopyRepoSubsetTest(unittest.TestCase):
    def test_copy_contains_tool_tree_driver_and_ci_without_bytecode(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="subset-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        copied = catalog.copy_repo_subset(root / "tree")
        for relative in ("tools/official_client_capture/codex_upgrade.py", "tools/official_client_capture/tests/chain_rehearsal.py",
                         "tools/arm64_capture_driver/driver/phase_context.py", "tools/ci/unit_records.py"):
            self.assertTrue((copied / relative).is_file(), relative)
        self.assertEqual([path for path in copied.rglob("__pycache__")], [])


if __name__ == "__main__":
    unittest.main()
