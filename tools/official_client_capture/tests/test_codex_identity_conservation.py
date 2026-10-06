"""Codex 身份守恒门禁（Claude 升级控制面设计 D7、B1-07）。

Claude 升级控制面改造对 Codex 只增不改：wire 层、evidence 层、两个编排器闭包与四项评估器摘要必须与改造开工前
的基线逐字相等；control 层、策略文件与整树摘要的变化必须逐文件落在 ``REGISTERED_CONTROL_CHANGES`` 登记的修改
清单里，且不得出现按默认层归类的文件；迁入共享模块的函数与常量不得属于任何身份闭包。

基线由 B1-01 开工前的 main（见基线的 ``source_commit``）算出，固化在
``fixtures/codex_identity_conservation_baseline.json``；本项目各批不得改动基线，文件摘要钉在 ``BASELINE_SHA256``。
"""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "codex_identity_conservation_baseline.json"
BASELINE_SHA256 = "e4310febbaeae49e74bbbe32b0093e7866d3c2302ead04db5d35c309d37814a1"
SHARED_CONTROL_PLANE = "upgrade_control_plane.py"

# 本项目登记的 Codex 受管树变化：受管树相对路径 → 工作项与理由。新增、删除、修改都要登记，且只能落在
# control 层。被 Codex 策略忽略的文件（``claude_`` 前缀等 Claude 专属文件）不影响 Codex 身份，无须登记。
REGISTERED_CONTROL_CHANGES: dict[str, str] = {
    "tool_identity_policy_v2.json": "B1-07 策略 v8：三个共享新文件登记进 control 层，先于文件本身进入受管树",
    "upgrade_client_adapter.py": "B1-01 客户端适配接口、编排器服务面与注册表",
    "codex_upgrade_client_adapter.py": "B1-01 Codex 适配器成员取值（逐字等于改造前写死的名字）",
    "codex_upgrade_supervisor.py": "B1-02 监督器解耦：编排器符号与客户端名字经适配器，Claude 清单合同",
    "codex_upgrade_reconciler.py": "B1-03 对账器判定流程经适配器；读侧闭包内 7 个函数原位不动",
    "codex_upgrade_project_ledger.py": "B1-04 项目总账客户端维度：Claude v2 plan 带 client，码表、目录名与计量类别经适配器；Codex v1 逐字不变",
    "codex_upgrade_timing_ledger.py": "B1-05 计时账本客户端维度：Claude v2 计划带 client，默认预算、承接表与总账目录名按客户端；Codex v1 逐字不变",
    "codex_upgrade.py": "B1-09 迁出 108 个不在任何身份闭包内的共享流程函数，原位留同名薄壳；随迁常量留别名",
    "upgrade_control_plane.py": "B1-09 新增共享控制流程：迁入函数经 adapter.orchestrator 访问编排器",
    "codex_upgrade_wire_transition.py": "B5a-01 只改 main：当前工具身份经适配器取 _tool_identity（Codex 取值不变）；裁定函数与 DEPLOY_RECEIPT_GLOB 原位不动",
}


class CodexIdentityConservationTest(unittest.TestCase):
    """基线固定；身份敏感摘要逐字相等；control 层变化逐文件登记；迁入符号不在闭包内。"""

    @classmethod
    def setUpClass(cls) -> None:
        raw = BASELINE_PATH.read_bytes()
        cls.baseline_sha256 = hashlib.sha256(raw).hexdigest()
        cls.baseline = json.loads(raw)
        cls.policy = tip.load_policy()
        digests = tip._managed_tree_digests(TOOL_ROOT)
        cls.entries = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
        cls.identity = tip.compute_identity_v2(cls.policy, TOOL_ROOT, cls.entries)
        cls.evaluator = tip.evaluator_dependency_digests(TOOL_ROOT, cls.policy)

    def test_baseline_is_frozen(self) -> None:
        self.assertEqual(self.baseline_sha256, BASELINE_SHA256, "Codex 身份守恒基线被改动：本项目各批不得改基线")
        self.assertEqual(self.baseline["schema_version"], "codex-identity-conservation-baseline/v1")

    def test_identity_sensitive_digests_match_baseline(self) -> None:
        """① wire、evidence、两个编排器闭包与四项评估器摘要逐字相等。"""

        current = {
            "wire_producer_sha256": self.identity["wire_producer_sha256"],
            "evidence_semantics_sha256": self.identity["evidence_semantics_sha256"],
            **{field: self.evaluator[field] for field in tip.EVALUATOR_DIGEST_FIELDS},
        }
        for field, value in current.items():
            self.assertEqual(value, self.baseline[field], f"Codex 身份敏感摘要变化：{field}")
        for layer, expected in self.baseline["orchestrator_closure_sha256"].items():
            self.assertEqual(
                self.identity["orchestrator_closures"][layer]["closure_sha256"],
                expected,
                f"Codex 编排器 {layer} 闭包变化",
            )

    def test_wire_closure_unchanged_for_tool_evolution(self) -> None:
        """④ tool-evolution 预览按编排器 wire 闭包摘要判定 ``wire_closure_changed``，改造前后必须为 false。"""

        self.assertEqual(
            self.identity["orchestrator_closures"]["wire_producer"]["closure_sha256"],
            self.baseline["orchestrator_closure_sha256"]["wire_producer"],
        )

    def test_tree_changes_are_registered_control_changes(self) -> None:
        """② control、策略与整树摘要的变化逐文件对应登记清单；不得有按默认层归类的文件。"""

        before = {entry["path"]: entry["sha256"] for entry in self.baseline["entries"]}
        after = {entry["path"]: entry["sha256"] for entry in self.entries}
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        for path in changed:
            layer = tip.classify_path(self.policy, path)
            if layer is None:
                continue
            self.assertIn(path, REGISTERED_CONTROL_CHANGES, f"Codex 受管文件变化未登记：{path}（{layer} 层）")
            self.assertEqual(layer, "control", f"Codex 受管文件变化不在 control 层：{path}（{layer} 层）")
        self.assertEqual(self.identity["defaulted_paths"], [], "新增受管文件必须在 Codex 策略里登记层级")
        self.assertEqual(
            self.identity["policy_sha256"] != self.baseline["policy_sha256"],
            tip.POLICY_FILENAME in changed,
            "策略摘要变化必须对应策略文件的登记变化",
        )

    def test_migrated_symbols_stay_outside_identity_closures(self) -> None:
        """③ 迁入共享模块的函数与常量不得属于 wire、evidence 或评估读侧闭包。"""

        path = TOOL_ROOT / SHARED_CONTROL_PLANE
        defined: set[str] = set()
        if path.is_file():
            functions, constants, _imports = tip._module_symbols(ast.parse(path.read_text(encoding="utf-8")))
            defined = set(functions) | set(constants)
        closure: set[str] = set()
        for kinds in self.baseline["closure_symbols"].values():
            for names in kinds.values():
                closure.update(names)
        self.assertEqual(sorted(defined & closure), [], "迁入共享模块的符号落在 Codex 身份闭包内")


if __name__ == "__main__":
    unittest.main()
