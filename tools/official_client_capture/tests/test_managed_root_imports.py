"""受管根的导入边界（2026-10-07）：两个受管根内的模块不得导入受管根外的仓库模块。

背景：``claude_upgrade_release`` 曾在模块顶层导入仓库根的 ``tools/generate_claude_fw_g_profile.py``。ARM64 受监督部署、
真实链外壳与 pre-A3 运行器的副本树都只交换 ``tools/official_client_capture`` 与 ``tools/official_client_control``，根外
模块在那里不存在：Claude 编排器一走到 VC-2／VC-3 相关路径（批次模块、编排器延迟导入画像模块，画像再导入 release）就
ImportError，B5a-05 的四条真实链在本机与 ARM64 预跑全部失败；根外模块的变化也不进 Claude 工具身份，是个盲区。修法是把
投影算法逐字搬进受管模块 ``claude_upgrade_wire_projection``，历史生成器反过来从它导入。

规则（静态扫描，不导入、不执行被扫描的模块）：``tools`` 包下只许导入两个受管根（以 Claude 身份策略的 ``roots`` 为准），
下面几种写法一律算越界——

* ``import tools``、``import tools.<根外>``；``from tools import <根外>``、``from tools.<根外> import …``；
* 相对导入跳出受管根（``from .. import <根外>``，或越过顶层包）；
* ``importlib.import_module`` ／ ``import_module`` ／ ``__import__`` 的字符串字面量指向根外模块。

模块顶层、函数体、条件分支与 ``try`` 块里的导入都算（部署树里任何一处被执行到都会失败）。扫描范围与 Claude 身份引擎的
受管清单同一口径：两个受管根下全部 ``.py``，排除 ``tests``、``versions``、``__pycache__``——测试目录在仓库里运行，测的
正是根外的 CI 入口、部署器、驱动与历史生成器本身。Claude 身份引擎在闭包里按同一规则失败关闭
（``claude_tool_identity_policy._resolve_imports``），本测试覆盖闭包之外的全部受管模块。
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Iterable

from tools.official_client_capture import claude_tool_identity_policy as claude_identity

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DYNAMIC_IMPORT_CALLS = frozenset({"import_module", "__import__"})


def _managed_roots(policy: dict) -> list[tuple[Path, str]]:
    """(根目录相对仓库的路径, 根目录名)，取自 Claude 身份策略的 ``roots``。"""

    return [(Path(str(root).rstrip("/")), str(root).rstrip("/").rsplit("/", 1)[-1]) for root in policy["roots"]]


def _outside(parts: list[str], managed: frozenset[str]) -> bool:
    return bool(parts) and parts[0] == "tools" and (len(parts) < 2 or parts[1] not in managed)


def outside_imports(repository_root: Path, roots: Iterable[tuple[Path, str]], excluded: Iterable[str]) -> list[str]:
    """扫描受管根内全部 ``.py``，返回越界导入的「条目键:行号 写法」清单（按条目键与行号排序）。"""

    roots = list(roots)
    managed = frozenset(name for _relative, name in roots)
    skip = frozenset(excluded)
    findings: list[tuple[str, int, str]] = []
    for relative_root, name in roots:
        base = Path(repository_root) / relative_root
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(base)
            if skip.intersection(relative.parts) or path.is_symlink():
                continue
            key = f"{name}/{relative.as_posix()}"
            module_parts = ["tools", name, *relative.with_suffix("").parts]
            package = module_parts[:-1]
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            def record(node: ast.AST, text: str) -> None:
                findings.append((key, getattr(node, "lineno", 0), text))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if _outside(alias.name.split("."), managed):
                            record(node, f"import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        if node.level - 1 >= len(package):
                            record(node, f"from {'.' * node.level}{node.module or ''} import …（越过顶层包）")
                            continue
                        owner = package[: len(package) - (node.level - 1)] + (node.module.split(".") if node.module else [])
                        written = f"from {'.' * node.level}{node.module or ''} import"
                    else:
                        owner = (node.module or "").split(".")
                        written = f"from {node.module} import"
                    if owner == ["tools"]:
                        for alias in node.names:
                            if alias.name not in managed:
                                record(node, f"{written} {alias.name}")
                    elif _outside(owner, managed):
                        record(node, f"{written} …（{'.'.join(owner)}）")
                elif isinstance(node, ast.Call):
                    function = node.func
                    called = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
                    if (
                        called in DYNAMIC_IMPORT_CALLS
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and _outside(node.args[0].value.split("."), managed)
                    ):
                        record(node, f"{called}({node.args[0].value!r})")
    return [f"{key}:{line} {text}" for key, line, text in sorted(findings)]


class ManagedRootImportTests(unittest.TestCase):
    def test_managed_modules_do_not_import_outside_roots(self) -> None:
        policy = claude_identity.load_policy()
        findings = outside_imports(REPOSITORY_ROOT, _managed_roots(policy), claude_identity.EXCLUDED_PARTS)
        self.assertEqual(
            findings,
            [],
            "受管根内的模块导入了受管根外的仓库模块（部署树与副本树只有两个受管根，根外变化也不进 Claude 身份）："
            "把被依赖的代码搬进受管根，根外工具反过来从受管模块导入。",
        )

    def test_scope_matches_identity_policy(self) -> None:
        """扫描的根与排除目录取自 Claude 身份策略与引擎：受管清单变了，本测试的范围跟着变。"""

        policy = claude_identity.load_policy()
        self.assertEqual(
            [name for _relative, name in _managed_roots(policy)], ["official_client_capture", "official_client_control"]
        )
        self.assertEqual(set(claude_identity.EXCLUDED_PARTS), {"tests", "versions", "__pycache__"})

    def test_scanner_finds_every_outside_form(self) -> None:
        """扫描器自检：各种越界写法全部报出，两个受管根之间、标准库与同目录的导入不误报，排除目录不扫。"""

        files = {
            "tools/official_client_capture/sample.py": '''
                import json
                import tools
                import tools.ci.entry_gates
                from tools import generate_claude_fw_g_profile, official_client_control
                from tools.arm64_capture_driver.driver import main
                from tools.official_client_capture import claude_upgrade_store
                from tools.official_client_control.canonical import canonical_sha256
                from . import sibling
                from .. import check_ledger_completeness
                from ..official_client_control import store


                def lazy():
                    from tools.codex_closeout import run
                    import importlib
                    importlib.import_module("tools.ci.unit_executor")
                    importlib.import_module("tools.official_client_capture.claude_upgrade_vc")
                    __import__("tools.arm64_supervised_deploy")
                    return run
            ''',
            "tools/official_client_capture/nested/inner.py": '''
                from ... import outside_tool
                from .... import beyond_top
            ''',
            "tools/official_client_capture/tests/test_ignored.py": "from tools import arm64_supervised_deploy\n",
            "tools/official_client_capture/versions/old.py": "from tools import codex_closeout\n",
            "tools/official_client_control/cli.py": "from tools.official_client_capture import capture\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
            roots = [
                (Path("tools/official_client_capture"), "official_client_capture"),
                (Path("tools/official_client_control"), "official_client_control"),
            ]
            findings = outside_imports(root, roots, claude_identity.EXCLUDED_PARTS)
        self.assertEqual(
            findings,
            [
                "official_client_capture/nested/inner.py:1 from ... import outside_tool",
                "official_client_capture/nested/inner.py:2 from .... import …（越过顶层包）",
                "official_client_capture/sample.py:2 import tools",
                "official_client_capture/sample.py:3 import tools.ci.entry_gates",
                "official_client_capture/sample.py:4 from tools import generate_claude_fw_g_profile",
                "official_client_capture/sample.py:5 from tools.arm64_capture_driver.driver import …（tools.arm64_capture_driver.driver）",
                "official_client_capture/sample.py:9 from .. import check_ledger_completeness",
                "official_client_capture/sample.py:14 from tools.codex_closeout import …（tools.codex_closeout）",
                "official_client_capture/sample.py:16 import_module('tools.ci.unit_executor')",
                "official_client_capture/sample.py:18 __import__('tools.arm64_supervised_deploy')",
            ],
        )


if __name__ == "__main__":
    unittest.main()
