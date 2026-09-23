"""抓包工具测试分层的防回流门禁（2026-09-21，三方裁定）。

背景：升级控制工具的两组真实评估链（``tests/real_chains/``，副本受管树 + 正式监督器子进程）
每条在 ARM64 上约 10 分钟，被 ``test_*.py`` 通配意外纳入默认 ``make test`` 后，候选的
``target-platform`` 门禁在 ARM64 上要跑约 5 小时，必然超出 VC-5 阶段预算（v14r2 实测）。
分层规则：

* 真实链只放在 ``tests/real_chains/``，该目录**没有** ``__init__.py``——这是它们不被
  ``unittest discover -s tools/official_client_capture/tests`` 递归发现的唯一机制；
* 独立目标 ``make test-capture-real-chains`` 以 ``CAPTURE_REAL_CHAIN_MODULES`` 逐模块显式执行，
  该清单必须与目录内容精确一致（不删除、不 skip、不遗漏）；
* 默认 ``make test`` 不得依赖该目标。

本文件只读 Makefile 与目录结构，不执行任何真实链。
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "tools" / "official_client_capture" / "tests"
REAL_CHAINS_DIR = TESTS_DIR / "real_chains"
MAKEFILE = REPO_ROOT / "Makefile"
REAL_CHAIN_PACKAGE = "tools.official_client_capture.tests.real_chains"


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _makefile_variable(name: str) -> list[str]:
    """读取形如 ``NAME := a \\\\n b`` 的多行 Make 变量，返回去空白后的词表。"""

    text = _makefile_text()
    match = re.search(rf"^{re.escape(name)}\s*:=\s*(.*?)(?=^\S)", text, re.M | re.S)
    if match is None:
        return []
    return match.group(1).replace("\\\n", " ").split()


def _makefile_recipe(target: str) -> str:
    text = _makefile_text()
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, re.M)
    return match.group(1) if match else ""


def _discovered_modules() -> set[str]:
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR), pattern="test_*.py", top_level_dir=str(REPO_ROOT))
    names: set[str] = set()

    def walk(item: unittest.TestSuite | unittest.TestCase) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            names.add(type(item).__module__)

    walk(suite)
    return names


class CaptureTestLayeringTests(unittest.TestCase):
    def test_real_chains_directory_is_not_a_package(self) -> None:
        self.assertTrue(REAL_CHAINS_DIR.is_dir(), "tests/real_chains/ 必须存在")
        self.assertFalse(
            (REAL_CHAINS_DIR / "__init__.py").exists(),
            "tests/real_chains/ 不得带 __init__.py，否则 unittest discover 会把真实链重新纳入默认 make test",
        )
        self.assertTrue(
            sorted(REAL_CHAINS_DIR.glob("test_*.py")),
            "tests/real_chains/ 至少要有一个真实链测试模块",
        )

    def test_default_discovery_excludes_real_chains(self) -> None:
        modules = _discovered_modules()
        self.assertFalse(
            {name for name in modules if name.startswith("unittest.loader")},
            "默认发现集合存在加载失败的模块",
        )
        leaked = sorted(name for name in modules if ".real_chains." in name or "real_chain" in name.rsplit(".", 1)[-1])
        self.assertEqual(leaked, [], f"真实链回流到默认发现集合：{leaked}")

    def test_default_target_unchanged_and_independent(self) -> None:
        recipe = _makefile_recipe("test-capture-tools")
        self.assertIn("python3 -m unittest discover", recipe)
        self.assertIn("-s tools/official_client_capture/tests -p 'test_*.py'", recipe)
        self.assertNotIn("real_chains", recipe)
        test_line = re.search(r"^test:\s*(.*)$", _makefile_text(), re.M)
        self.assertIsNotNone(test_line)
        assert test_line is not None
        self.assertNotIn("test-capture-real-chains", test_line.group(1).split())
        phony = re.search(r"^\.PHONY:\s*(.*)$", _makefile_text(), re.M)
        self.assertIsNotNone(phony)
        assert phony is not None
        self.assertIn("test-capture-real-chains", phony.group(1).split())

    def test_independent_target_lists_every_real_chain_module(self) -> None:
        expected = sorted(
            f"{REAL_CHAIN_PACKAGE}.{path.stem}" for path in REAL_CHAINS_DIR.glob("test_*.py")
        )
        listed = sorted(_makefile_variable("CAPTURE_REAL_CHAIN_MODULES"))
        self.assertEqual(listed, expected, "CAPTURE_REAL_CHAIN_MODULES 必须与 tests/real_chains/ 目录逐一对应")
        recipe = _makefile_recipe("test-capture-real-chains")
        self.assertIn(
            "python3 -m tools.official_client_capture.tests.real_chain_gate $(CAPTURE_REAL_CHAIN_MODULES)", recipe
        )
        self.assertNotIn("discover", recipe)
        gate = importlib.util.find_spec("tools.official_client_capture.tests.real_chain_gate")
        self.assertIsNotNone(gate, "真实链执行入口不可导入")
        for name in expected:
            spec = importlib.util.find_spec(name)
            self.assertIsNotNone(spec, f"真实链模块不可导入：{name}")


if __name__ == "__main__":
    unittest.main()
