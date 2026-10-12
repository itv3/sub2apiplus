"""B5a-04 验收：Codex 的 44 个 pre-A3 场景单元（ID、命令、额度、测试模块）与入口门禁给它们的输入声明，和改造前逐字相同。

改造前的认证模块与入口门禁取自 B5a-04 开工时本分支的 HEAD（``BASELINE_COMMIT``），从真实仓库的 git 历史读出，作为独立模块
对象加载；与改造后的模块在同一组参数下各生成一次：

* 认证模块 ``plan_scenario_commands``（入口门禁 ``plan --staging-parent`` 的清单）逐字相同，缺省与显式 ``client="codex"`` 相同；
* 场景全集、真实链登记逐项相同；
* 入口门禁 ``plan_gates``（pre-a3 组合）给这些单元的合并规格（额度、预计秒数、环境、输入声明）与门禁项逐字相同，门禁记录
  里的字面命令也相同。

基线提交读不到（浅克隆）或基线之后 Codex 场景表已变（同一输入不再成立）时跳过并写明原因。本模块读真实仓库的 git 历史，
按 ``unit_records.GIT_READERS`` 登记。

另核 B5a-05：认证模块相对 B5a-08 提交只填了 Claude 的两张登记表（真实链 ID 与场景表），收尾时再给派发秒数表
``SCENARIO_SECONDS`` 补 Claude 四条真实链的实测秒数。按 AST 逐个顶层语句对照——除这两张表的赋值与秒数表以外，每个语句
（Codex 场景表、全部函数与常量）与 B5a-08 提交逐字相同；秒数表的 Codex 七项逐项不变，新增的键恰好是 Claude 的真实链 ID；
注释改写不进 AST，不影响判定。B5a-08 的源码残差对照因本文件在它之后又改过而跳过，这一项接着守住 Codex 部分不变。

5a 收口（B5a-11）在此之外只许改三处顶层语句，逐项单独核对：去掉模块级 ``from tools.official_client_capture.tests import
project_ledger_fixture``（运行时模块不导入测试目录）；``FIXTURE_ONLY_ENV`` 由引用夹具常量改为同值字面量；
``_bind_rehearsal_receipt`` 只改 Claude 分支的拒绝报错文字（campaign-run 分批演练绑定只属于 Codex），其余逐字不变。

B5b-02 固定79场景根仅增加三个原函数的明确接口分支；各函数的前后完整 AST 摘要逐项冻结，不允许任意函数变化。
Codex 的真实清单、门禁输入和旧 CLI 行为仍由实际生成与运行回归核对。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

from tools.ci import entry_gates
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture.tests import project_ledger_fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_COMMIT = "68745c9dd"
# B5a-05 的对照基线：B5a-08 的提交（本分支历史上的认证模块最后一次由 B5a-08 修改）。
B5A05_BASELINE_COMMIT = "c53571c7d"
# B5a-05 只许改这两张表的赋值（Claude 的真实链登记与场景表）。
B5A05_CLAUDE_TABLES = ("CLAUDE_REAL_CHAIN_IDS", "CLAUDE_SCENARIOS")
# B5a-05 收尾只许给派发秒数表追加 Claude 真实链的键（Codex 的键与秒数不变）。
B5A05_SECONDS_TABLE = "SCENARIO_SECONDS"
# 5a 收口（B5a-11）许改的三处顶层语句：去掉的测试目录导入、改成字面量的夹具环境变量、只改拒绝报错文字的绑定函数。
B5A11_TESTS_IMPORT_MODULE = "tools.official_client_capture.tests"
B5A11_FIXTURE_ENV_NAME = "FIXTURE_ONLY_ENV"
B5A11_BIND_FUNCTION = "_bind_rehearsal_receipt"
STAGING_PARENT = Path("/root/docker/capture-cli/data/staging/pre-a3-scenarios")
WORKDIR = Path("/root/docker/capture-cli/data")
# B5b-03 扩时与 full79 v3 时间合同只允许十一个新增声明和 planner 中一个 Claude 专属表达式。
# 固定 AST 摘要防止借历史对照例外接纳其它修改；行为与方法数由预算合同回归独立核对。
CLAUDE_TIME_AST = {
    "CLAUDE_FAULT_CONTINUOUS_SECONDS": "c70c911e8b9ed0e8a9423ac0d8ee15dfbb48cae45f29589770ebe3b29a8e3e22",
    'CLAUDE_RECORDED_CHAIN_IDS': '2507db6a6af099ccbca7866438729c8be0e76ad29feb8ada6bdd05844f9b57d9',
    'claude_recorded_time_budget': 'b6acbe8252c84dd70c5a0b01eebd64b2d1d66639b1809b3ad60c627422c8cad2',

    "CLAUDE_FAULT_TIME_SCHEMA": "fb10ba0aff91363c6c6cbaa14a8b7cea48deb2689d7b6853b600b6d224871630",
    "CLAUDE_FAULT_METHOD_SECONDS": "4143cca82261e5ef6187a302787b58d91fce4cc60b63e60600f8ac358cd10d9d",
    "CLAUDE_FAULT_METHOD_COUNTS": "4f1d9f648e8d71ec4450cb49b08f3fba45159f2d4d9937133055e1163c6a9ed7",
    "claude_fault_time_budget": "8377a6f63768b145c2799c5db197dc8b37a224228c435df684e1b81603cd23f9",

    "CLAUDE_FULL79_NAME": "7a37f5f4d5426967525d39960081790032268c306f51c1f197a9557cd4a0618f",
    "CLAUDE_FULL79_TIME_SCHEMA": "4d35dd1afebd59f76f4bea62d1550b2f2d75a790a90481178ce8a742e34907e5",
    "claude_full79_time_budget": "1c5d9868115ee0b68481dbe387164a3b854c0cd4575968c8694db9400bc239d8",
    "claude_unit_timeout_seconds": "ed3f715b8edde810c1ede2982ebd41f6e60d76e556285e5c46da3d1e736c9d16",
}
CLAUDE_TIME_EXPRESSION = '''claude_fault_time_budget(name)["unit_timeout_seconds"]
    if client == "claude" and name in CLAUDE_FAULT_METHOD_COUNTS else
    claude_unit_timeout_seconds(name)
    if client == "claude" and name in CLAUDE_RECORDED_CHAIN_IDS else
    REAL_CHAIN_TIMEOUT_SECONDS if name in real_chains else SCENARIO_TIMEOUT_SECONDS'''

# B5b-02 只登记这三个原函数的已批准接口差异；前后均核完整 AST，不按函数名豁免其内容。
B5B02_FIXED_ROOT_BASELINE_AST = {
    "run_scenario_unit": "c7ef546aecb967f8402606e3e5f5afbec6c09d862e90c60ad9207e865bce2025",
    "build_parser": "898f71fedcf2c1d5e095adb937c2dae2b6965fb09f17a26b4394d9ae4262f2f5",
    "main": "402964d55722d4d16125434e4d19fa1e7949fbf36a291c2e80d0d2820ef8d01b",
}
B5B02_FIXED_ROOT_AST = {
    "run_scenario_unit": "d0fd41c694c33f4cc92cf29477d300cac2abfdef531f68d7d93ad4bbd505d9c9",
    "build_parser": "cc86a42c022262247d54899a9ff4072ba811a83cf3da097de7f0cb395d32cf10",
    "main": "f5bd18d94e4c205c9da6117352451855b3f5fc6ad110291e90d3fdeb34b651fe",
}


def full_ast_dump(node: ast.AST) -> str:
    """保留 AST 的全部空字段，使 Python 3.12 与默认省略空字段的新版本产生同一份精确摘要。

    旧版没有 show_empty 参数，本来就完整输出空字段；新版显式开启它。只统一序列化格式，不移除任何语法字段。
    """

    options = {"show_empty": True} if "show_empty" in inspect.signature(ast.dump).parameters else {}
    return ast.dump(node, **options)


def load_baseline(relative: str, name: str) -> types.ModuleType | None:
    """从 git 历史读出改造前的模块源码，作为独立模块对象加载（不写入 sys.modules；``__file__`` 指向仓库里的同一位置，
    模块按它找同目录的兄弟文件）。"""

    try:
        source = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{BASELINE_COMMIT}:{relative}"],
                                capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / relative)
    exec(compile(source, f"{BASELINE_COMMIT}:{relative}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


class PreA3CodexParityTests(unittest.TestCase):
    def test_codex_scenario_units_and_gate_inputs_match_pre_b5a04(self) -> None:
        before = load_baseline("tools/official_client_capture/codex_upgrade_pre_a3_certification.py", "pre_a3_before_b5a04")
        gates_before = load_baseline("tools/ci/entry_gates.py", "entry_gates_before_b5a04")
        if before is None or gates_before is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的认证模块或入口门禁（浅克隆或不在仓库内）")
        if (before.SCENARIOS, before.REAL_CHAIN_IDS) != (pre_a3.SCENARIOS, pre_a3.REAL_CHAIN_IDS):
            self.skipTest("基线之后 Codex 场景表已变，同一输入不再成立；以新的基线提交重新对照")
        old_units = before.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=WORKDIR)
        new_units = pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=WORKDIR)
        self.assertEqual(len(old_units["units"]), 44, "43 条登记场景加补账场景")
        self.assertEqual(new_units, old_units, "场景单元 ID、命令、额度与测试模块逐字相同")
        self.assertEqual(pre_a3.plan_scenario_commands(STAGING_PARENT, python="python3", cwd=WORKDIR, client="codex"), old_units)
        self.assertEqual(pre_a3.scenario_names(), before.scenario_names())
        self.assertEqual(pre_a3.real_chain_registration(), before.real_chain_registration())
        with tempfile.TemporaryDirectory() as directory:
            units_file = Path(directory) / "pre-a3-units.json"
            units_file.write_text(json.dumps(new_units), encoding="utf-8")
            data_inputs = [{"category": "environment", "name": "tree:fixed", "sha256": "0" * 64, "detail": {}}]
            arguments = dict(profile="pre-a3", launcher=["launcher"], pre_a3_units=units_file, pre_a3_env={"PYTHONPATH": "."},
                             pre_a3_data_inputs=data_inputs, platform="linux")
            old_plan = gates_before.plan_gates(Path("."), **arguments)
            new_plan = entry_gates.plan_gates(Path("."), **arguments)
        self.assertEqual(new_plan["units"], old_plan["units"], "入口门禁给 44 个场景单元的规格与输入声明逐字相同")
        self.assertEqual(new_plan["gates"], old_plan["gates"])
        unit_ids = [unit["unit_id"] for unit in new_plan["units"]]
        self.assertEqual(entry_gates.gate_literal_command("pre-a3", unit_ids), gates_before.GATE_COMMANDS["pre-a3"],
                         "门禁记录里的字面命令相同")

    def test_b5a05_only_fills_the_claude_tables(self) -> None:
        """B5a-05：除两张 Claude 登记表的赋值、秒数表里追加的 Claude 项与 5a 收口的三处外，认证模块的每个顶层语句与 B5a-08
        提交逐字相同（AST 级）；5a 收口的三处逐项单独核对。"""

        relative = "tools/official_client_capture/codex_upgrade_pre_a3_certification.py"
        try:
            source = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{B5A05_BASELINE_COMMIT}:{relative}"],
                                    capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout.decode("utf-8")
        except (OSError, subprocess.CalledProcessError):
            self.skipTest(f"读不到基线提交 {B5A05_BASELINE_COMMIT} 的认证模块（浅克隆或不在仓库内）")

        def statements(text: str) -> tuple[list[str], dict[str, str], dict[str, float], dict[str, Any]]:
            """顶层语句的 AST 转储：两张 Claude 表单独取出，秒数表取字面值，5a 收口的三处另取，其余按原顺序。"""

            others: list[str] = []
            tables: dict[str, str] = {}
            seconds: dict[str, float] = {}
            closeout: dict[str, Any] = {"tests_imports": []}
            for node in ast.parse(text).body:
                target = node.target if isinstance(node, ast.AnnAssign) else None
                names = [item.id for item in node.targets if isinstance(item, ast.Name)] if isinstance(node, ast.Assign) else []
                name = (node.name if isinstance(node, ast.FunctionDef) else target.id if isinstance(target, ast.Name)
                        else names[0] if len(names) == 1 else "")
                if isinstance(node, ast.FunctionDef) and name in B5B02_FIXED_ROOT_AST:
                    digest = hashlib.sha256(full_ast_dump(node).encode()).hexdigest()
                    self.assertIn(digest, (B5B02_FIXED_ROOT_BASELINE_AST[name], B5B02_FIXED_ROOT_AST[name]))
                    closeout.setdefault("fixed_root_shapes", {})[name] = digest
                    continue
                if name in CLAUDE_TIME_AST:
                    self.assertEqual(hashlib.sha256(full_ast_dump(node).encode()).hexdigest(), CLAUDE_TIME_AST[name])
                    closeout.setdefault("time_additions", []).append(name)
                    continue
                if isinstance(node, ast.FunctionDef) and node.name == "plan_scenario_commands":
                    expected = ast.parse("(" + CLAUDE_TIME_EXPRESSION + ")", mode="eval").body
                    for item in ast.walk(node):
                        if isinstance(item, ast.Dict):
                            for index, key in enumerate(item.keys):
                                if isinstance(key, ast.Constant) and key.value == "timeout_seconds":
                                    expression = item.values[index]
                                    if isinstance(expression, ast.IfExp) and isinstance(expression.test, ast.BoolOp):
                                        self.assertEqual(ast.dump(expression), ast.dump(expected))
                                        closeout["time_overrides"] = closeout.get("time_overrides", 0) + 1
                                        item.values[index] = expression.orelse.orelse
                if isinstance(target, ast.Name) and target.id in B5A05_CLAUDE_TABLES:
                    tables[target.id] = ast.dump(node)
                elif isinstance(target, ast.Name) and target.id == B5A05_SECONDS_TABLE:
                    # 注解与赋值形状不变，只比较字面值（追加的键另行核对）。
                    others.append(ast.dump(node.annotation))
                    seconds = ast.literal_eval(node.value)
                elif isinstance(node, ast.ImportFrom) and node.module == B5A11_TESTS_IMPORT_MODULE:
                    closeout["tests_imports"].append([alias.name for alias in node.names])
                elif names == [B5A11_FIXTURE_ENV_NAME]:
                    closeout["fixture_env"] = node.value
                elif isinstance(node, ast.FunctionDef) and node.name == B5A11_BIND_FUNCTION:
                    # 把函数里 raise 的报错参数换成占位，其余结构逐字比较；报错文字单独取出。
                    messages: list[str] = []
                    for raise_node in [item for item in ast.walk(node) if isinstance(item, ast.Raise)]:
                        call = raise_node.exc
                        if isinstance(call, ast.Call) and call.args:
                            messages.append(ast.unparse(call.args[0]))
                            call.args = [ast.Constant(value="<报错文字>")]
                    closeout["bind_shape"] = ast.dump(node)
                    closeout["bind_messages"] = messages
                else:
                    others.append(ast.dump(node))
            return others, tables, seconds, closeout

        old_others, old_tables, old_seconds, old_closeout = statements(source)
        new_others, new_tables, new_seconds, new_closeout = statements((REPO_ROOT / relative).read_text(encoding="utf-8"))
        self.assertEqual(sorted(old_tables), sorted(B5A05_CLAUDE_TABLES))
        self.assertEqual(sorted(new_tables), sorted(B5A05_CLAUDE_TABLES))
        self.assertEqual(new_others, old_others,
                         "Claude 登记表、秒数表、5a 收口三处、精确时间合同与固定根三函数精确摘要之外均须逐字相同")
        self.assertEqual(old_closeout["fixed_root_shapes"], B5B02_FIXED_ROOT_BASELINE_AST)
        self.assertEqual(new_closeout["fixed_root_shapes"], B5B02_FIXED_ROOT_AST)
        self.assertEqual(old_closeout.get("time_additions", []), [])
        self.assertEqual(sorted(new_closeout["time_additions"]), sorted(CLAUDE_TIME_AST))
        self.assertEqual(new_closeout["time_overrides"], 1)
        # 5a 收口①：基线在模块级导入测试夹具，现在不再导入测试目录。
        self.assertEqual(old_closeout["tests_imports"], [["project_ledger_fixture"]])
        self.assertEqual(new_closeout["tests_imports"], [])
        # 5a 收口②：基线引用夹具常量，现在是与夹具同值的字面量。
        self.assertEqual(ast.unparse(old_closeout["fixture_env"]), "project_ledger_fixture.FIXTURE_ONLY_ENV")
        self.assertIsInstance(new_closeout["fixture_env"], ast.Constant)
        self.assertEqual(new_closeout["fixture_env"].value, project_ledger_fixture.FIXTURE_ONLY_ENV)
        # 5a 收口③：绑定函数除拒绝报错文字外逐字相同；只有一处报错，文字改成当前事实。
        self.assertEqual(new_closeout["bind_shape"], old_closeout["bind_shape"])
        self.assertEqual(len(old_closeout["bind_messages"]), 1)
        self.assertEqual(len(new_closeout["bind_messages"]), 1)
        self.assertIn("B5a-09", old_closeout["bind_messages"][0])
        self.assertIn("只属于 Codex 的分批演练合同", new_closeout["bind_messages"][0])
        self.assertNotEqual(new_tables, old_tables, "B5a-05 填了两张 Claude 登记表")
        # 秒数表：Codex 的键与秒数逐项不变；新增的键恰好是 Claude 的真实链 ID，秒数是正数。
        self.assertTrue(old_seconds)
        self.assertEqual({name: value for name, value in new_seconds.items() if name in old_seconds}, old_seconds)
        self.assertEqual(sorted(set(new_seconds) - set(old_seconds)), sorted(pre_a3.CLAUDE_REAL_CHAIN_IDS))
        self.assertTrue(all(isinstance(value, float) and value > 0 for value in new_seconds.values()))


if __name__ == "__main__":
    unittest.main()
