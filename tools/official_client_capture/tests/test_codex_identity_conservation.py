"""Codex 身份守恒门禁（Claude 升级控制面设计 D7、B1-07）。

Claude 升级控制面改造对 Codex 只增不改：wire 层、evidence 层、两个编排器闭包与四项评估器摘要必须与改造开工前
的基线逐字相等；control 层、策略文件与整树摘要的变化必须逐文件落在 ``REGISTERED_CONTROL_CHANGES`` 登记的修改
清单里，且不得出现按默认层归类的文件；迁入共享模块的函数与常量不得属于任何身份闭包。

唯一例外是 ``REGISTERED_EVALUATOR_READER_CHANGES`` 单独登记的那一项评估器读侧变化（B5a-08，老板 10-07 定）：两项读侧
摘要允许从基线变成登记的值，变化的符号只能是登记的那几个（门禁收据生成器的钉住常量，以及改它时既有门禁连带要求的只读
重放登记）——测试把它们还原成变化前的摘要后，两个读侧闭包必须正好回到基线；wire、evidence、checker、builder 摘要与
编排器闭包不受这项登记影响，照旧逐字等于基线。

基线由 B1-01 开工前的 main（见基线的 ``source_commit``）算出，固化在
``fixtures/codex_identity_conservation_baseline.json``；本项目各批不得改动基线，文件摘要钉在 ``BASELINE_SHA256``。
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "codex_identity_conservation_baseline.json"
BASELINE_SHA256 = "e4310febbaeae49e74bbbe32b0093e7866d3c2302ead04db5d35c309d37814a1"
SHARED_CONTROL_PLANE = "upgrade_control_plane.py"

# 本项目登记的 Codex 受管树变化：受管树相对路径 → 工作项与理由。新增、删除、修改都要登记，且只能落在
# control 层。被 Codex 策略忽略的文件（``claude_`` 前缀等 Claude 专属文件）不影响 Codex 身份，无须登记。
REGISTERED_CONTROL_CHANGES: dict[str, str] = {
    "tool_identity_policy_v2.json": "B1-07 策略 v8：三个共享新文件登记进 control 层，先于文件本身进入受管树",
    "upgrade_client_adapter.py": "B1-01 客户端适配接口、编排器服务面与注册表；B2-08 新增三个适配器成员（attempt 环境已恢复口径、部署收据五摘要"
                                 "所在对象、有效 wire 身份），Codex 绑定到原写法或原函数",
    "codex_upgrade_client_adapter.py": "B1-01 Codex 适配器成员取值（逐字等于改造前写死的名字）；B2-08 新增两个成员函数，取值逐字等于对账器原写法"
                                       "（after 探针加恢复收据、部署收据顶层五摘要）",
    "codex_upgrade_supervisor.py": "B1-02 监督器解耦：编排器符号与客户端名字经适配器，Claude 清单合同",
    "codex_upgrade_reconciler.py": "B1-03 对账器判定流程经适配器；读侧闭包内 7 个函数原位不动；B2-08 读侧闭包外的 _environment_facts、"
                                   "_deployment_receipt、_identity_facts 三处改经适配器成员（Codex 取值逐字等价）；两类对账收据的易变字段加上 "
                                   "campaign_deadline_at_utc（批准延期后同一对象能再对账，原先在这里失败关闭）",
    "codex_upgrade_project_ledger.py": "B1-04 项目总账客户端维度：Claude v2 plan 带 client，码表、目录名与计量类别经适配器；Codex v1 逐字不变",
    "codex_upgrade_timing_ledger.py": "B1-05 计时账本客户端维度：Claude v2 计划带 client，默认预算、承接表与总账目录名按客户端；Codex v1 逐字不变",
    "codex_upgrade.py": "B1-09 迁出 108 个不在任何身份闭包内的共享流程函数，原位留同名薄壳；随迁常量留别名",
    "upgrade_control_plane.py": "B1-09 新增共享控制流程：迁入函数经 adapter.orchestrator 访问编排器",
    "codex_upgrade_wire_transition.py": "B5a-01 只改 main：当前工具身份经适配器取 _tool_identity（Codex 取值不变）；裁定函数与 DEPLOY_RECEIPT_GLOB 原位不动",
    "codex_upgrade_pre_a3_certification.py": "B5a-04 pre-A3 按客户端分组：场景表与真实链按客户端、单元前缀 pre-a3:claude:、Claude 签 v3；编排器引用经适配器（Codex 单元、命令与收据逐字不变）；"
                                             "B5a-08 签发与核验新增逐单元承接（unit-inherit）清单的形成期证明分支（新增函数），逐场景核验多一个缺省为全集通过的"
                                             " inheriting_mode 参数（Codex 的 v1／v2 签发与核验逐字不变，见 test_unit_inherit_codex_parity）；"
                                             "B5a-05 只填 Claude 的两张字面量登记表 CLAUDE_REAL_CHAIN_IDS、CLAUDE_SCENARIOS（最小恢复演练四条链），"
                                             "两者不在 Codex 评估读侧闭包内，Codex 场景表、单元与收据不变",
    "codex_upgrade_policy_certification.py": "B5a-04 策略认证按客户端：Claude 按 v3 引擎算身份、认 Claude 部署收据；编排器引用经适配器（Codex 合同与输出逐字不变）",
    "certify_release.py": "B5a-04 发布认证按客户端：Claude 签 tool-release-certification/v2（加 client）；编排器引用经适配器（Codex v1 逐字不变）",
    "codex_upgrade_vc_receipt.py": "B5a-08 P0 v2 重验新增逐单元承接（unit-inherit）清单的形成期证明分支（新增函数，验证器只从仓库 tools/ci 或"
                                   "已安装驱动加载）；逐条核验与门禁重验多一个缺省为全集通过的 inheriting_mode 参数；读侧闭包内的符号原位不动"
                                   "（Codex 两种模式的结果与报错逐字不变，见 test_unit_inherit_codex_parity）",
    "codex_upgrade_gate_receipt.py": "B5a-08 只改钉住常量 TARGET_VERIFIER_SHA256S 里 unit_records.py 的摘要：常量在评估器读侧闭包内，按"
                                     " REGISTERED_EVALUATOR_READER_CHANGES 单独登记",
}

# 本项目登记的 Codex 评估器读侧变化（见模块说明）：键是工作项，值登记变化的符号（「模块:符号」→ 前后符号摘要）、两项读侧
# 摘要的前后值与理由。这是 5a 批次唯一允许的 Codex 评估器身份变化；Codex 发布重认证随 5a 收口统一做。
REGISTERED_EVALUATOR_READER_CHANGES: dict[str, dict[str, Any]] = {
    "B5a-08": {
        "symbols": {
            # 钉住常量本身：unit_records.py 的摘要换成 B5a-01 与 B5a-08 改过之后的那份。
            "codex_upgrade_gate_receipt:TARGET_VERIFIER_SHA256S": {
                "before": "02825ab58147e9071ae19395590c2be90c58aefd6023a2013646a7605c7e37e8",
                "after": "b5abc7579c03d34b35801d13a3ff8f4cdc0714d15a174396788e46971cf6ae93"},
            # 改钉住值就是改门禁收据生成器：生成器只读重放登记门禁（test_producer_replay_registration_gate）要求同时登记修改前
            # 版本 ce9e3b79 的摘要，否则该版本已生成的 v4 门禁收据部署后无法重放。只新增这一项，已有登记一项不动。
            "codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES": {
                "before": "49e52aebf07af8149431fe658080b241cc0b8cb43d52a04f58a15c3ded6e774a",
                "after": "47a709cdee5443dd31397f06c5a3f23077699a3175039a4506690c5843758860"},
        },
        "compare_reader_sha256": {"before": "2a4ed24c04a1fee36be282fe2b57b4884f760b165e462c2f32cecaa806ebf62b",
                                  "after": "f49bc6a6ff1205b30cef7edf42466b7b68bdf74cc240eb0e48cf83918e86b992"},
        "accept_reader_sha256": {"before": "20773be4fb1fe1a103f874d9b4cbc6a1c78d0ca2830b0693aef8a882f074aff9",
                                 "after": "135ded5e4f6b46a96bfee2de0941c3c480d5ddaf323c00ba27305e433a85986c"},
        "reasons": (
            "B-10 目标平台单元承接验证器的依赖文件 tools/ci/unit_records.py 被 B5a-01 与 B5a-08 合法修改，钉住摘要随之更新"
            "（同组另外四个依赖 target_platform_gate.py、full_set_receipt.py、read_audit.py、read_audit_runtime.py 未改，钉住值不变）",
            "B-10 的 unit_execution 开关是关闭的：门禁收据项不带 unit_execution 时不加载这个验证器，Codex 现行门禁收据不受影响",
            "没有进行中的 Codex Campaign：冻结了评估器摘要的 Campaign 不会遇到新值；Codex 发布重认证随 5a 收口统一做",
            "只读重放登记那一项是改钉住值的连带：按既有门禁登记修改前生成器的摘要，只服务旧收据重放，不改变新收据的判定",
        ),
    },
}
READER_FIELDS = (("compare_reader_sha256", tip.EVALUATOR_COMPARE_READER_ROOTS), ("accept_reader_sha256", tip.EVALUATOR_ACCEPT_READER_ROOTS))


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
        """① wire、evidence、两个编排器闭包与四项评估器摘要逐字相等（两项读侧摘要等于单独登记的变化后值）。"""

        current = {
            "wire_producer_sha256": self.identity["wire_producer_sha256"],
            "evidence_semantics_sha256": self.identity["evidence_semantics_sha256"],
            **{field: self.evaluator[field] for field in tip.EVALUATOR_DIGEST_FIELDS},
        }
        expected = {field: self.baseline[field] for field in current}
        for change in REGISTERED_EVALUATOR_READER_CHANGES.values():
            for field, _roots in READER_FIELDS:
                self.assertEqual(change[field]["before"], self.baseline[field], f"登记的变化前摘要必须就是基线：{field}")
                expected[field] = change[field]["after"]
        for field, value in current.items():
            self.assertEqual(value, expected[field], f"Codex 身份敏感摘要变化：{field}")
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

    def test_evaluator_reader_change_is_only_the_registered_symbols(self) -> None:
        """⑤ 单独登记的评估器读侧变化只有登记的那几个符号：把它们还原成变化前的摘要后，两个读侧闭包正好回到基线，整文件计入
        的模块一个不变——不能借这次登记放过别的读侧变化。"""

        self.assertEqual(sorted(REGISTERED_EVALUATOR_READER_CHANGES), ["B5a-08"], "5a 批次唯一允许的 Codex 评估器身份变化")
        change = REGISTERED_EVALUATOR_READER_CHANGES["B5a-08"]
        self.assertEqual(sorted(change["symbols"]), ["codex_upgrade_gate_receipt:REGISTERED_REPLAY_PRODUCER_HASHES",
                                                     "codex_upgrade_gate_receipt:TARGET_VERIFIER_SHA256S"])
        self.assertEqual(len(change["reasons"]), 4)
        digests = tip._managed_tree_digests(TOOL_ROOT)
        for field, roots in READER_FIELDS:
            with self.subTest(field=field):
                closure = tip.evaluator_reader_closure(self.policy, TOOL_ROOT, list(roots), digests)
                self.assertEqual(closure["closure_sha256"], change[field]["after"])
                restored = copy.deepcopy({item: value for item, value in closure.items() if item != "closure_sha256"})
                for key, digest in change["symbols"].items():
                    module, name = key.split(":")
                    matches = [item for item in restored["symbols"] if (item["module"], item["name"]) == (module, name)]
                    self.assertEqual(len(matches), 1, f"登记的符号在读侧闭包里，按符号计入：{key}")
                    self.assertEqual(matches[0]["sha256"], digest["after"], key)
                    matches[0]["sha256"] = digest["before"]
                    self.assertNotIn(module, [item["module"] for item in closure["modules"]], "登记的模块不能整文件计入")
                self.assertEqual(tip._fingerprint(restored), self.baseline[field],
                                 "把登记的符号还原成变化前的摘要，读侧闭包必须正好回到基线")

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
