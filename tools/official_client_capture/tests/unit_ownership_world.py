"""归属表与按客户端筛选相关测试共用的合成仓库夹具（``test_ci_unit_ownership``、``test_ci_gate_scope``）。

合成 git 仓库：一个带归属数据文件的基点提交，测试再改文件、提交候选提交后，用真实的选择器 ``tools/ci/unit_ownership.py`` 选择。
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

    def __init__(self, case: unittest.TestCase, files: dict[str, str] | None = None, calibration: dict[str, float] | None = None) -> None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        git(self.root, "init", "-q")
        for rel, text in (files or WORLD).items():
            self.write(rel, text)
        self.data = uo.materialize(self.root, base_data(), calibration or CALIBRATION)
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
