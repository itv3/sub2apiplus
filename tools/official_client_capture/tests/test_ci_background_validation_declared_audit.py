"""后台验证里「读集审计证据是否成立」认得声明级承接记录（步骤 5 WP3b 第 2 项；``tools/ci/background_validation.py`` 的
``_audit_evidence_ok``）。

背景：后台跨提交承接的登记（``_inherit_registry``）把四个数分开统计成一个漏斗——登记数 registered ≥ 审计通过数 audit_passed ≥ 消费者接受数
consumer_accepted ＝ 实际承接数 inherited，不满足就抛 ``ContractError``（调用方拒绝通过）。审计通过数过去只认读集覆盖口径
``all-file-paths/v1``，声明级承接（``declared-inputs/v1``）的记录会被算成「审计没通过」，承接了声明级单元的后台结论就必然撞上这条漏斗检查。

这里用真实的声明级记录（经真实执行器进程产生：登记表证据来源、逐次采样来源、证据不完整、真违规）逐类核对：

* ``_audit_evidence_ok`` 的真值表：通过的 declared 记录成立；incomplete（只因证据不完整）与 failed（有未覆盖）的记录不成立；
  篡改、非正式执行、审计绑定的输入摘要不符、覆盖没闭合等也不成立；旧口径 ``all-file-paths/v1`` 的判定一字不变；
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


def sealed(**fields: Any) -> dict[str, Any]:
    """合成一条自摘要有效的记录（只带 ``_audit_evidence_ok`` 看的字段）。"""

    return ur.seal_record({"unit_id": "u", "kind": "formal", "inputs_sha256": "a" * 64, **fields})


def audit(scope: str = DECLARED, **changes: Any) -> dict[str, Any]:
    body = {"coverage_scope": scope, "coverage_complete": True, "status": "passed", "undeclared_count": 0, "inputs_sha256": "a" * 64}
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
