"""后台验证里「读集审计证据是否成立」认得声明级承接记录（步骤 5 WP3b 第 2 项；``tools/ci/background_validation.py`` 的
``_audit_evidence_ok``）。

背景：后台跨提交承接的登记（``_inherit_registry``）把四个数分开统计成一个漏斗——登记数 registered ≥ 审计通过数 audit_passed ≥ 消费者接受数
consumer_accepted ＝ 实际承接数 inherited，不满足就抛 ``ContractError``（调用方拒绝通过）。审计通过数过去只认读集覆盖口径
``all-file-paths/v1``，声明级承接（``declared-inputs/v1``）的记录会被算成「审计没通过」，承接了声明级单元的后台结论就必然撞上这条漏斗检查。

这里用真实的声明级记录（经真实执行器进程产生：登记表证据来源、逐次采样来源、证据不完整、真违规）逐类核对：

* ``_audit_evidence_ok`` 的真值表：通过的 declared 记录成立；incomplete（只因证据不完整）与 failed（有未覆盖）的记录不成立；
  篡改、非正式执行、审计绑定的输入摘要不符、覆盖没闭合等也不成立；旧口径 ``all-file-paths/v1`` 的判定一字不变；
* 伪造记录的反例（独立验收 #3 一般 7）：``read_audit`` 里自述「声明级、通过、覆盖闭合、0 未声明、摘要一致」不够，记录自己的结构也要像一条声明级记录——
  输入里恰有一个 ``declared-inputs-scope`` 条目、``read_audit.trace`` 带 64 位十六进制 ``sha256``、``sample_binding`` 是对象且 ``unit_id`` 就是这条
  记录的单元、``sample_origin`` 是 registry 或 run；缺任何一项都不成立。放行判据仍是消费者核验 ``check_record``（被钉住的验证器，不在这里改）；
* 漏斗：同一次后台承接里，通过的 declared 来源记录进 audit_passed、重跑单元新记录的审计结论如实登记，四个数与消费凭证一致；
  把口径认知换回旧的就会撞上 ``ContractError``（回归证明）。
"""

from __future__ import annotations

import ast
import json
import shutil
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import background_validation as bv
from tools.ci import unit_inherit as ui
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_executor as dx

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools" / "ci" / "background_validation.py"
DECLARED = "declared-inputs/v1"
LEGACY = "all-file-paths/v1"


SCOPE_ENTRY = {"category": "environment", "name": "declared-inputs-scope", "sha256": "c" * 64, "detail": {}}


def sealed(**fields: Any) -> dict[str, Any]:
    """合成一条自摘要有效的记录（只带 ``_audit_evidence_ok`` 看的字段；缺省输入里有恰好一个声明范围条目，像一条声明级记录）。"""

    return ur.seal_record({"unit_id": "u", "kind": "formal", "inputs_sha256": "a" * 64, "inputs": [SCOPE_ENTRY], **fields})


def audit(scope: str = DECLARED, **changes: Any) -> dict[str, Any]:
    """合成的 ``read_audit``：缺省是一条合格的声明级审计（带封存证据的摘要、绑定到单元 u 的 sample_binding、登记表来源）。"""

    body = {"coverage_scope": scope, "coverage_complete": True, "status": "passed", "undeclared_count": 0, "inputs_sha256": "a" * 64,
            "trace": {"path": "/x/evidence.json", "sha256": "d" * 64}, "sample_origin": "registry",
            "sample_binding": {"unit_id": "u", "command_sha256": "e" * 64, "scope_sha256": "f" * 64, "evidence_sha256": "d" * 64}}
    body.update(changes)
    return body


class AuditEvidenceTruthTableTests(unittest.TestCase):
    """合成记录上的真值表：每个字段单独变一下，结论只在该变的时候变。"""

    def verdict(self, record: dict[str, Any], expected: str | None = None) -> bool:
        return bv._audit_evidence_ok(ur, record, expected if expected is not None else record["record_sha256"])

    def test_the_scope_names_agree_across_modules(self) -> None:
        self.assertEqual(bv.DECLARED_COVERAGE_SCOPE, DECLARED)
        self.assertEqual(ur.DECLARED_COVERAGE_SCOPE, DECLARED)
        self.assertEqual(dx.ue.DECLARED_COVERAGE_SCOPE, DECLARED)
        self.assertEqual(bv.DECLARED_SCOPE_ENTRY, "declared-inputs-scope")
        self.assertEqual(ur.DECLARED_SCOPE_ENTRY, bv.DECLARED_SCOPE_ENTRY)
        self.assertEqual(dx.ue.DECLARED_SCOPE_ENTRY, bv.DECLARED_SCOPE_ENTRY)
        self.assertEqual(SCOPE_ENTRY["name"], bv.DECLARED_SCOPE_ENTRY, "夹具里的范围条目名与真实名字一致")
        self.assertEqual(bv.DECLARED_SAMPLE_ORIGINS, ("registry", "run"), "执行器只写这两种证据来源")

    def test_a_passed_declared_audit_is_established(self) -> None:
        self.assertTrue(self.verdict(sealed(read_audit=audit())))
        self.assertTrue(self.verdict(sealed(read_audit=audit(sample_origin="run"))), "证据来源（登记表或本次采样）不影响审计这一层")

    def test_incomplete_and_failed_declared_audits_are_not_established(self) -> None:
        cases = {
            "incomplete（只因证据不完整）": audit(status="incomplete", coverage_complete=False),
            "incomplete 但覆盖标记被改成闭合": audit(status="incomplete"),
            "failed": audit(status="failed", coverage_complete=False, undeclared_count=2),
            "status 通过但覆盖没闭合": audit(coverage_complete=False),
            "覆盖闭合但有未声明项": audit(undeclared_count=1),
            "未声明数缺失": {key: value for key, value in audit().items() if key != "undeclared_count"},
            "覆盖标记不是布尔真": audit(coverage_complete="yes"),
            "审计绑定的输入摘要不符": audit(inputs_sha256="b" * 64),
            "审计里没有输入摘要": {key: value for key, value in audit().items() if key != "inputs_sha256"},
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.assertFalse(self.verdict(sealed(read_audit=body)))

    def test_acceptable_variations_of_a_declared_record_stay_established(self) -> None:
        """收紧只收紧「像不像一条声明级记录」，不误伤真记录会有的形态：别的输入条目、trace 与绑定里多出来的键、两种证据来源。"""

        other = {"category": "tests", "name": "scope:backend", "sha256": "2" * 64, "detail": {}}
        for label, record in {
            "登记表来源": sealed(read_audit=audit(sample_origin="registry")),
            "本次采样来源": sealed(read_audit=audit(sample_origin="run")),
            "范围条目旁边还有别的输入条目": sealed(read_audit=audit(), inputs=[other, SCOPE_ENTRY, {**other, "name": "contract:toolchain:go"}]),
            "trace 里多一个 stored 路径（执行器真会写）": sealed(read_audit=audit(trace={"path": "/x", "sha256": "d" * 64, "stored": "/y"})),
            "sample_binding 里多一个键": sealed(read_audit=audit(sample_binding={"unit_id": "u", "extra": 1})),
        }.items():
            with self.subTest(label):
                self.assertTrue(self.verdict(record), label)

    def test_a_forged_declared_record_that_only_claims_the_audit_is_not_established(self) -> None:
        """独立验收 #3 一般 7：声明级分支过去只信 ``read_audit`` 的自述——任何单元类型的记录都能自述「声明级、通过、覆盖闭合、0 未声明、摘要一致」
        而被算进审计通过。现在记录自己的结构也要像一条声明级记录：输入里恰有一个范围条目、trace 带 64 位十六进制摘要、sample_binding 绑定到这个单元、
        证据来源是 registry 或 run。逐项缺失或造假都不成立（基准是同一条合格记录，只变一处）。"""

        self.assertTrue(self.verdict(sealed(read_audit=audit())), "基准：合格的声明级记录成立")
        without = lambda key: {name: value for name, value in audit().items() if name != key}   # noqa: E731
        binding = {"unit_id": "u", "command_sha256": "e" * 64, "scope_sha256": "f" * 64, "evidence_sha256": "d" * 64}
        cases = {
            "输入里没有范围条目（别的单元类型的记录自述声明级）": sealed(read_audit=audit(), inputs=[]),
            "输入里有两个范围条目": sealed(read_audit=audit(), inputs=[SCOPE_ENTRY, {**SCOPE_ENTRY, "sha256": "9" * 64}]),
            "输入不是数组": sealed(read_audit=audit(), inputs={"declared-inputs-scope": SCOPE_ENTRY}),
            "输入是 None": sealed(read_audit=audit(), inputs=None),
            "输入里只有别的条目": sealed(read_audit=audit(), inputs=[{"category": "tests", "name": "repo:all", "sha256": "1" * 64, "detail": {}}]),
            "输入里的条目不是对象": sealed(read_audit=audit(), inputs=["declared-inputs-scope"]),
            "审计里没有 trace": sealed(read_audit=without("trace")),
            "trace 不是对象": sealed(read_audit=audit(trace="/x/evidence.json")),
            "trace 没有 sha256": sealed(read_audit=audit(trace={"path": "/x/evidence.json"})),
            "trace.sha256 不是字符串": sealed(read_audit=audit(trace={"path": "/x", "sha256": 7})),
            "trace.sha256 太短": sealed(read_audit=audit(trace={"path": "/x", "sha256": "d" * 63})),
            "trace.sha256 太长": sealed(read_audit=audit(trace={"path": "/x", "sha256": "d" * 65})),
            "trace.sha256 带大写": sealed(read_audit=audit(trace={"path": "/x", "sha256": "D" * 64})),
            "trace.sha256 不是十六进制": sealed(read_audit=audit(trace={"path": "/x", "sha256": "z" * 64})),
            "trace.sha256 后面带换行": sealed(read_audit=audit(trace={"path": "/x", "sha256": "d" * 64 + "\n"})),
            "审计里没有 sample_binding": sealed(read_audit=without("sample_binding")),
            "sample_binding 不是对象": sealed(read_audit=audit(sample_binding="u")),
            "sample_binding 是数组": sealed(read_audit=audit(sample_binding=[binding])),
            "sample_binding 没有 unit_id": sealed(read_audit=audit(sample_binding={key: value for key, value in binding.items() if key != "unit_id"})),
            "sample_binding 绑定的是别的单元": sealed(read_audit=audit(sample_binding={**binding, "unit_id": "other"})),
            "sample_binding.unit_id 不是字符串": sealed(read_audit=audit(sample_binding={**binding, "unit_id": ["u"]})),
            "记录没有单元且绑定也没有": sealed(unit_id=None, read_audit=audit(sample_binding={"command_sha256": "e" * 64})),
            "审计里没有 sample_origin": sealed(read_audit=without("sample_origin")),
            "sample_origin 是 none": sealed(read_audit=audit(sample_origin="none")),
            "sample_origin 大小写不对": sealed(read_audit=audit(sample_origin="Registry")),
            "sample_origin 是数组": sealed(read_audit=audit(sample_origin=["registry"])),
            "sample_origin 是 None": sealed(read_audit=audit(sample_origin=None)),
        }
        for label, record in cases.items():
            with self.subTest(label):
                self.assertFalse(self.verdict(record), f"{label}：自述通过也不能算审计通过")

    def test_an_undeclared_count_that_only_compares_equal_to_zero_is_not_established(self) -> None:
        """独立验收 #5 一般 4：``False == 0``、``0.0 == 0`` 在 Python 里成立，重新封存的记录把 ``read_audit.undeclared_count`` 改成 ``False`` 或 ``0.0``
        不能冒充「未声明数为零」。只认类型恰为 int 且等于 0；声明级与旧口径两个分支都一样。"""

        for scope in (DECLARED, LEGACY):
            self.assertTrue(self.verdict(sealed(read_audit=audit(scope, undeclared_count=0))), f"{scope}：基准，整数 0 成立")
            for label, value in {"布尔 False": False, "浮点 0.0": 0.0, "浮点 -0.0": -0.0, "布尔 True": True, "整数 1": 1, "整数 -1": -1,
                                 "字符串 '0'": "0", "空字符串": "", "None": None, "空数组": [], "数组 [0]": [0], "对象": {}, "浮点 0.5": 0.5}.items():
                with self.subTest(scope=scope, value=label):
                    self.assertFalse(self.verdict(sealed(read_audit=audit(scope, undeclared_count=value))), f"{scope}：未声明数是 {label}")

    def test_an_int_subclass_does_not_pass_as_an_exact_zero(self) -> None:
        class ZeroLike(int):
            pass

        self.assertTrue(bv._exact_zero(0))
        for value in (ZeroLike(0), False, 0.0, None, "0", 0j, [0]):
            with self.subTest(value=repr(value)):
                self.assertFalse(bv._exact_zero(value))

    def test_two_missing_or_malformed_inputs_digests_do_not_pass_as_equal(self) -> None:
        """审计绑定的输入摘要与记录自己的输入摘要用 ``==`` 比，两边都缺（``None == None``）、两边都是同一个非摘要的值就会冒充「摘要一致」。
        现在两边都得是 64 位小写十六进制字符串且相等。"""

        without = {key: value for key, value in audit().items() if key != "inputs_sha256"}
        self.assertTrue(self.verdict(sealed(read_audit=audit())), "基准：两边都是同一个 64 位十六进制摘要")
        for scope in (DECLARED, LEGACY):
            body = {**without, "coverage_scope": scope}
            cases = {
                "两边都缺": sealed(read_audit=body, inputs_sha256=None),
                "审计缺、记录里是 None 字符串": sealed(read_audit=body, inputs_sha256="None"),
                "两边都是空字符串": sealed(read_audit={**body, "inputs_sha256": ""}, inputs_sha256=""),
                "两边都是同一个短串": sealed(read_audit={**body, "inputs_sha256": "abc"}, inputs_sha256="abc"),
                "两边都是同一个大写摘要": sealed(read_audit={**body, "inputs_sha256": "A" * 64}, inputs_sha256="A" * 64),
                "两边都是同一个数字": sealed(read_audit={**body, "inputs_sha256": 123}, inputs_sha256=123),
                "两边都是同一个数组": sealed(read_audit={**body, "inputs_sha256": ["a" * 64]}, inputs_sha256=["a" * 64]),
                "两边都是 64 位但不是十六进制": sealed(read_audit={**body, "inputs_sha256": "z" * 64}, inputs_sha256="z" * 64),
                "后面带换行": sealed(read_audit={**body, "inputs_sha256": "a" * 64 + "\n"}, inputs_sha256="a" * 64 + "\n"),
            }
            for label, record in cases.items():
                with self.subTest(scope=scope, case=label):
                    self.assertFalse(self.verdict(record), f"{scope}：{label}")
            self.assertTrue(self.verdict(sealed(read_audit={**without, "coverage_scope": scope, "inputs_sha256": "0" * 64}, inputs_sha256="0" * 64)),
                            f"{scope}：两边相同的合格摘要成立")

    def test_the_other_fields_have_no_loose_equality_problem(self) -> None:
        """同类字段逐个核过：``kind``／``status``／``coverage_scope`` 是与字符串字面量比较，非字符串永远不等；``coverage_complete`` 是 ``is True``。
        布尔、数字、数组、大小写变体都不成立。"""

        for scope in (DECLARED, LEGACY):
            self.assertTrue(self.verdict(sealed(read_audit=audit(scope, coverage_complete=True))), "基准：布尔 True 成立")
            for value in (1, 1.0, "true", "yes", [True], None, False):
                with self.subTest(field="coverage_complete", value=repr(value)):
                    self.assertFalse(self.verdict(sealed(read_audit=audit(scope, coverage_complete=value))))
            for value in (True, 1, "Passed", "PASSED", ["passed"], None, ""):
                with self.subTest(field="status", value=repr(value)):
                    self.assertFalse(self.verdict(sealed(read_audit=audit(scope, status=value))))
            for value in (True, 1, "Formal", ["formal"], None):
                with self.subTest(field="kind", value=repr(value)):
                    self.assertFalse(self.verdict(sealed(read_audit=audit(scope), kind=value)))

    def test_the_structure_checks_do_not_touch_the_legacy_judgement(self) -> None:
        """旧口径 all-file-paths/v1 的记录没有声明范围条目、trace 与绑定的形态要求：判定一字不变。"""

        legacy = audit(LEGACY)
        for label, record in {
            "没有输入条目": sealed(read_audit=legacy, inputs=[]),
            "没有 sample_binding／sample_origin": sealed(read_audit={key: value for key, value in legacy.items() if key not in ("sample_binding", "sample_origin")}),
            "没有 trace": sealed(read_audit={key: value for key, value in legacy.items() if key != "trace"}),
        }.items():
            with self.subTest(label):
                self.assertTrue(self.verdict(record), label)

    def test_the_record_itself_must_be_a_sealed_formal_execution(self) -> None:
        record = sealed(read_audit=audit())
        self.assertFalse(self.verdict(record, "c" * 64), "不是登记的那一条（摘要对不上）")
        self.assertFalse(self.verdict({**record, "unit_id": "tampered"}), "自摘要失效（被改过）")
        self.assertFalse(self.verdict(sealed(read_audit=audit(), kind="diagnostic")), "诊断执行不算")
        self.assertFalse(bv._audit_evidence_ok(ur, None, "x"), "读不出来的记录")
        self.assertFalse(self.verdict(sealed()), "没有读集审计")
        self.assertFalse(self.verdict(sealed(read_audit="passed")), "读集审计不是对象")
        self.assertFalse(self.verdict(sealed(read_audit=audit(), inputs_sha256="d" * 64)), "记录自己的输入摘要与审计绑定的不符")

    def test_the_legacy_scope_judgement_is_unchanged_and_unknown_scopes_are_refused(self) -> None:
        self.assertTrue(self.verdict(sealed(read_audit=audit(LEGACY))))
        self.assertFalse(self.verdict(sealed(read_audit=audit(LEGACY, status="failed"))))
        self.assertFalse(self.verdict(sealed(read_audit=audit(LEGACY, coverage_complete=False))))
        self.assertFalse(self.verdict(sealed(read_audit=audit("not-observed"))), "未观察到读集的单元（整仓声明被跳过审计）不算成立")
        self.assertFalse(self.verdict(sealed(read_audit=audit("declared-inputs/v2"))), "不认识的口径一律不成立")
        self.assertFalse(self.verdict(sealed(read_audit=audit(None))))
        self.assertFalse(self.verdict(sealed(read_audit={**audit(), "coverage_scope": [DECLARED]})), "口径不是字符串")

    def test_the_only_literal_legacy_scope_branch_is_the_audit_evidence_check(self) -> None:
        """核对 background_validation 里其它按读集覆盖口径分支的地方：全模块（不含 docstring）只有 ``_audit_evidence_ok`` 一处写了旧口径字面量，
        别的函数若再按这个口径分支，必须同时认声明级——新增这样的分支会让本用例红，提醒评审。"""

        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        owners: list[str] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant) and child.value is node.body[0].value:
                        continue   # 函数的 docstring 不算
                    self.visit(child)
                self.stack.pop()

            def visit_Constant(self, node: ast.Constant) -> None:
                if node.value == LEGACY:
                    owners.append(self.stack[-1] if self.stack else "<module>")

        Visitor().visit(tree)
        self.assertEqual(owners, ["_audit_evidence_ok"])


class RealRecordsTests(unittest.TestCase):
    """经真实执行器进程产生的四种声明级记录：登记表证据来源（α'）、逐次采样来源（β）、β 证据不完整、β 真违规。"""

    def test_real_records_of_every_kind_get_the_right_verdict(self) -> None:
        world = dx.World(self)
        calm = world.run()
        self.assertEqual(calm.code, 0, calm.stderr[-1500:])
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [dx.GARBAGE]})
        incomplete_run = world.run("--declared-sample", "always")
        self.assertEqual(incomplete_run.code, 0, "只因证据不完整：整次运行不失败")
        stray = dx.open_line(world.cwd_of("backend:go-test"), str(world.tree / "frontend/src/x.ts"))
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [stray]})
        failed_run = world.run("--declared-sample", "always")
        self.assertEqual(failed_run.code, 1)

        def ok(run: Any, unit_id: str) -> bool:
            record = world.record_of(run, unit_id)
            return bv._audit_evidence_ok(ur, record, record["record_sha256"])

        registry = world.record_of(calm, "backend:go-test")
        self.assertEqual((registry["read_audit"]["sample_origin"], registry["read_audit"]["status"]), ("registry", "passed"))
        self.assertTrue(ok(calm, "backend:go-test"), "α'：登记表证据来源、通过")
        sampled = world.record_of(incomplete_run, "backend:integration")
        self.assertEqual((sampled["read_audit"]["sample_origin"], sampled["read_audit"]["status"]), ("run", "passed"))
        self.assertTrue(ok(incomplete_run, "backend:integration"), "β：本次采样来源、通过")
        # 收紧后的结构要求，真记录一直满足：恰有一个范围条目、trace 带 64 位十六进制摘要、绑定到自己的单元、来源是 registry 或 run。
        for record in (registry, sampled):
            part = record["read_audit"]
            self.assertEqual(sum(1 for item in record["inputs"] if item["name"] == "declared-inputs-scope"), 1)
            self.assertRegex(part["trace"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(part["sample_binding"]["unit_id"], record["unit_id"])
            self.assertIn(part["sample_origin"], ("registry", "run"))
        # 在真记录上造假（改完重新封存，自摘要有效）：去掉范围条目、把绑定改到别的单元、把来源改成不认识的值——都不成立。
        for label, change in {
            "去掉范围条目": lambda body: body.update(inputs=[item for item in body["inputs"] if item["name"] != "declared-inputs-scope"]),
            "重复范围条目": lambda body: body.update(inputs=[*body["inputs"], *[item for item in body["inputs"] if item["name"] == "declared-inputs-scope"]]),
            "绑定到别的单元": lambda body: body["read_audit"]["sample_binding"].update(unit_id="backend:integration"),
            "去掉证据摘要": lambda body: body["read_audit"].pop("trace"),
            "来源改成别的值": lambda body: body["read_audit"].update(sample_origin="elsewhere"),
        }.items():
            with self.subTest(f"真记录上造假：{label}"):
                body = json.loads(json.dumps({key: value for key, value in registry.items() if key != "record_sha256"}))
                change(body)
                forged = ur.seal_record(body)
                self.assertTrue(ur.seal_ok(forged, "record_sha256"), "造假的记录自摘要有效——只信自述的统计会把它算成审计通过")
                self.assertFalse(bv._audit_evidence_ok(ur, forged, forged["record_sha256"]))
        weak = world.record_of(incomplete_run, "backend:go-test")
        self.assertEqual((weak["read_audit"]["status"], weak["passed"], weak["inheritable"]), ("incomplete", True, False))
        self.assertFalse(ok(incomplete_run, "backend:go-test"), "incomplete：命令结论不变，但读集证据不足以认证")
        bad = world.record_of(failed_run, "backend:go-test")
        self.assertEqual((bad["read_audit"]["status"], bad["passed"]), ("failed", False))
        self.assertFalse(ok(failed_run, "backend:go-test"), "failed：有未覆盖")
        self.assertFalse(ok(calm, "deploy:check"), "rerun-only 单元的记录没有读集审计，不算审计通过")


class InheritCountsFunnelTests(unittest.TestCase):
    """后台跨提交承接登记的四个数（registered／audit_passed／consumer_accepted／inherited）在 declared 记录上的口径。

    场景：第一轮（逐次采样）四个声明级单元都通过；改了后端源码后第二轮逐单元承接——前端单元承接第一轮的 declared 记录，三个后端单元重跑：
    一个通过、一个证据不完整（乱码行）、一个真违规（读了范围外的文件）。"""

    def setUp(self) -> None:
        self.world = dx.World(self)
        self.control = self.world.root / "data" / "control"
        self.control.mkdir(parents=True)
        (self.control / "codex-e2e-supervisor-enable-20261007t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.clock = self.world.root / "clock" / "sample.json"
        self.clock.parent.mkdir()

    def sample_clock(self) -> None:
        self.clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0,
                                          "sampled_at_utc": ui.fs.timestamp(time.time())}), encoding="utf-8")

    def inherit_args(self, request: Path | None = None) -> list[str]:
        return ["--unit-inherit-client", "codex", "--unit-inherit-control", str(self.control)] + (
            ["--unit-inherit-request", str(request)] if request is not None else [])

    def request(self, name: str) -> Path:
        fs, world = ui.fs, self.world
        proposal = world.root / f"proposal-{name}"
        proposed = world.run("--decide-only", "--unit-inherit-propose", str(proposal), *self.inherit_args(), mode="unit-inherit", expect_summary=False)
        self.assertEqual(proposed.code, 0, proposed.stderr[-1500:])
        binding = json.loads((proposal / "binding.json").read_text(encoding="utf-8"))
        sources = json.loads((proposal / "sources.json").read_text(encoding="utf-8"))
        now = time.time()
        self.sample_clock()
        clock = world.root / f"request-clock-{name}.json"
        clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0, "sampled_at_utc": fs.timestamp(now)}), encoding="utf-8")
        revocation = world.root / f"request-revocation-{name}.json"
        revocation.write_text(json.dumps(ui.query_revocation(world.store, sorted(sources["runs"]), "pre-ledger", now)), encoding="utf-8")
        approvals = []
        for role in sorted(fs.APPROVAL_ROLES):
            path = world.root / f"approval-{name}-{role}.json"
            path.write_text(json.dumps({"role": role, "status": "approved", "account": "替身审核人", "scope": ui.CONTRACT,
                                        "binding_sha256": binding["binding_sha256"], "approved_at_utc": fs.timestamp(now - 3600),
                                        "expires_at_utc": fs.timestamp(now + 86400)}), encoding="utf-8")
            approvals.append(path)
        output = world.root / f"request-{name}.json"
        ui.assemble_request(proposal, clock=clock, revocation=revocation, approvals=approvals, reuse_enabled=True, output=output,
                            clock_source=f"file:{self.clock}")
        return output

    def two_rounds(self) -> tuple[Any, Any, Path]:
        world = self.world
        world.plan_good_strace()
        first = world.run("--declared-sample", "always")
        self.assertEqual(first.code, 0, first.stderr[-1500:])
        world.commit({"backend/a.go": "package main\n// 改\n"})
        plan = {unit_id: {"lines": world.good_lines(unit_id)} for unit_id in dx.DECLARED_IDS}
        plan["backend:integration"]["lines"] = world.good_lines("backend:integration") + [dx.GARBAGE]
        plan["egress-spec:egress-spec-go-test"]["lines"] = world.good_lines("egress-spec:egress-spec-go-test") + [
            dx.open_line(world.cwd_of("egress-spec:egress-spec-go-test"), str(world.tree / "frontend/src/x.ts"))]
        world.set_strace_plan(plan)
        request = self.request("second")
        self.sample_clock()
        second = world.run("--declared-sample", "always", *self.inherit_args(request), mode="unit-inherit")
        self.assertEqual(second.manifest.get("mode"), "unit-inherit", second.stderr[-2000:])
        return first, second, request

    def payload(self, first: Any, second: Any, request: Path) -> dict[str, Any]:
        """后台结论里 ``_inherit_registry`` 要读的那几项：来源登记（第一轮的运行）、入口门禁目录（里面 executor/unit-manifest.json）、记录库、请求。"""

        out = self.world.root / "background-out"
        (out / "executor").mkdir(parents=True)
        shutil.copy(second.out / "unit-manifest.json", out / "executor" / "unit-manifest.json")
        return {"unit_inherit_source": {"executor_run_id": first.summary["run_id"], "executor_manifest_sha256": first.manifest["manifest_sha256"],
                                        "commit": "c" * 40, "result": {"path": "/x/result.json"}, "ancestry": {"status": "verified"}},
                "out": str(out), "record_store": str(self.world.store),
                "unit_inherit_request": {"path": str(request), "sha256": bv._sha256_file(request)}}

    def test_the_four_counts_form_a_consistent_funnel_over_declared_records(self) -> None:
        first, second, request = self.two_rounds()
        self.assertEqual(second.inherited, {"frontend:critical"})
        self.assertEqual(second.executed, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test", "deploy:check"})
        payload = self.payload(first, second, request)
        proof = {"inherited": 1, "run_id": second.summary["run_id"], "executed": 4}
        registry = bv._inherit_registry(payload, proof=proof)
        self.assertEqual(registry["inherit_counts"], {"registered": 4, "audit_passed": 1, "consumer_accepted": 1, "inherited": 1},
                         "登记＝4 个声明级单元；审计通过＝来源集合里那条通过的 declared 来源记录；承接＝消费者接受＝1")
        counts = registry["inherit_counts"]
        self.assertLessEqual(counts["inherited"], counts["audit_passed"])
        self.assertLessEqual(counts["audit_passed"], counts["registered"])
        self.assertEqual(counts["consumer_accepted"], counts["inherited"])
        flags = {item["unit_id"]: item["audit_passed"] for item in registry["rerun_units"]}
        self.assertEqual(flags, {"backend:go-test": True, "backend:integration": False, "egress-spec:egress-spec-go-test": False,
                                 "deploy:check": False},
                         "重跑单元新记录的审计结论：通过的成立，证据不完整与真违规的不成立，rerun-only 没有读集审计")
        summary = registry["inherit_summary"]
        self.assertEqual((summary["executed"], summary["inherited"], summary["executed_audit_passed"], summary["audit_ready_after_run"]), (4, 1, 1, 2))
        self.assertEqual(summary["rerun_failed_units"], ["egress-spec:egress-spec-go-test"])
        # 回归证明：口径认知换回旧的，漏斗检查就撞上 ContractError（审计通过数 0 < 实际承接数 1）。
        with mock.patch.object(bv, "DECLARED_COVERAGE_SCOPE", "no-such-scope"):
            with self.assertRaisesRegex(ui.ContractError, "审计通过 0"):
                bv._inherit_registry(payload, proof=proof)

    def test_without_a_consumption_proof_the_counts_are_registered_but_not_asserted(self) -> None:
        """失败的后台结论尽力登记（没有消费凭证）：consumer_accepted 是 None，漏斗不检查，审计通过数仍按 declared 口径如实统计。"""

        first, second, request = self.two_rounds()
        registry = bv._inherit_registry(self.payload(first, second, request), proof=None)
        self.assertEqual(registry["inherit_counts"], {"registered": 4, "audit_passed": 1, "consumer_accepted": None, "inherited": 1})


if __name__ == "__main__":
    unittest.main()
