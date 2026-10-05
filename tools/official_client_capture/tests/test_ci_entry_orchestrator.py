"""入口编排器（E2-06，``tools/ci/entry_orchestrator.py``）。

沿用 E2-05 的夹具（数据根、驱动、参数、判定用的命令替身），再给编排器一个命令替身：便宜检查、策略认证、入口门禁、
pre-A3、smoke、atomic-double、建账本、环境收据、checkpoint、预检 plan、Job 演练、启动探测、发布认证都按参数在夹具里
造出产物。按方案 E2-06 的验收核对：建账本之前一次报全、部署绑定没过时依赖步骤被阻塞；三处注入失败后重新执行同一条
命令从失败步骤续跑、账本不重建；失败的 Job 演练收据不沿用、续跑只重跑失败项；被占用的正式坐标换序号；起止步骤与只判定。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from tools.ci import entry_orchestrator as eo
from tools.official_client_capture.tests.test_ci_entry_steps import Fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _option(argv: Sequence[str], name: str) -> str:
    return argv[list(argv).index(name) + 1]


class Commands:
    """编排器的命令替身：按参数造产物；被注入遮蔽（unshare 包住）的命令一律失败。"""

    def __init__(self, fx: Fixture) -> None:
        self.fx = fx
        self.calls: list[list[str]] = []
        self.preflight_failed: list[str] = []
        self.gates_status = "passed"
        self.pre_a3_fail = False
        self.job_statuses: list[str] = []
        # 收口模块的结果（E2-07）：依次取用，空了就是通过；元素是（退出码，标准输出最后一行 JSON）。
        self.closeout_results: list[tuple[int, dict]] = []
        # 入口门禁导出的 P0 证据里标成没通过的门禁项（防御性核对：门禁记录与 P0 证据不一致时不签 P0）。
        self.p0_evidence_failed: set[str] = set()
        # 入口门禁扣下的 P0 证据（E3-03：承接了单元却写不出 v2——记录库里没有已发布的清单、五摘要算不出来）；每次调用
        # 入口门禁时传入的模式。
        self.p0_evidence_withheld: set[str] = set()
        self.gates_modes: list[str] = []
        # VC-0 收口前后的整机预约（E4-01）：依次记下 acquire／closeout／release；acquire_granted 为假时调度器不批准。
        self.machine: list[str] = []
        self.acquire_granted = True

    def count(self, needle: str) -> int:
        return sum(1 for call in self.calls if needle in call)

    def __call__(self, argv: Sequence[str], cwd: Path, env: dict, log: Path) -> int:
        argv = list(argv)
        self.calls.append(argv)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(argv) + "\n")
        if argv[0] == "unshare":
            return 1
        joined = " ".join(argv)
        if argv[0] == "bash" and argv[1].endswith("entry-preflight.sh"):
            out = Path(env["ENTRY_PREFLIGHT_OUT"])
            _write_json(out / "summary.json", {"status": "failed" if self.preflight_failed else "passed",
                                               "failed": list(self.preflight_failed)})
            return 1 if self.preflight_failed else 0
        if argv[0] == "bash" and argv[1].endswith("entry-gates.sh"):
            out = Path(_option(argv, "--out"))
            self.gates_modes.append(_option(argv, "--mode"))
            _write_json(out / "entry-gates.json", {"status": self.gates_status, "p0_evidence_withheld": {
                gate_id: "示例：本次运行承接了单元，要写 v2 P0 证据，但运行清单没有发布进记录库" for gate_id in sorted(self.p0_evidence_withheld)}})
            for gate_id in ("test-capture-tools", "check-egress-spec"):
                if gate_id in self.p0_evidence_withheld:
                    continue
                passed = self.gates_status == "passed" and gate_id not in self.p0_evidence_failed
                _write_json(out / "p0" / f"{gate_id}.json", {
                    "gate_id": gate_id, "status": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
                    "passed": 10, "failed": 0 if passed else 1, "approved_skip": 2, "unexpected_skip": 0})
            if _option(argv, "--pre-a3-mode") == "run" and not self.pre_a3_fail:
                _write_json(Path(_option(argv, "--pre-a3-certification")), {"status": "passed"})
            return 0 if self.gates_status == "passed" and not self.pre_a3_fail else 1
        if "codex_upgrade_policy_certification" in joined:
            _write_json(Path(_option(argv, "--output")), {"kind": "policy", "policy_sha256": "p" * 64})
            return 0
        if "codex_upgrade_pre_a3_certification" in joined and "plan" in argv:
            root = Path(_option(argv, "--staging-root")) / "run-1"
            root.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"staging_root": str(root)}) + "\n")
            return 0
        if "unit_executor.py" in joined and "acquire" in argv:
            self.machine.append("acquire")
            if not self.acquire_granted:
                return 2
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"owner": _option(argv, "--owner"), "granted_at_utc": "2026-10-03T00:00:00Z",
                                         "granted_by": "no-scheduler"}) + "\n")
            return 0
        if "unit_executor.py" in joined and "release" in argv:
            self.machine.append("release")
            return 0
        if "unit_executor.py" in joined:
            return 0
        if "codex_upgrade_pre_a3_certification" in joined and "issue" in argv:
            if not self.pre_a3_fail:
                _write_json(Path(_option(argv, "--output")), {"status": "passed"})
            return 1 if self.pre_a3_fail else 0
        if "codex_upgrade_pre_a3_certification" in joined and "record-reuse" in argv:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"reuse_receipt": None}) + "\n")
            return 0
        if "codex_upgrade_zero_request_smoke" in joined:
            _write_json(Path(_option(argv, "--output")), {"status": "passed"})
            return 0
        if argv[:2] == ["docker", "exec"]:
            root = self.fx.data / "staging" / Path(_option(argv, "--evidence-root")).name
            _write_json(root / "receipt.json", {"status": "passed"})
            return 0
        if "codex_upgrade_timing_ledger" in joined and "create" in argv:
            ledger = Path(_option(argv, "--ledger-dir"))
            ledger.mkdir()
            _write_json(ledger / "ledger.json", {
                "upgrade_id": _option(argv, "--upgrade-id"), "campaign_purpose": _option(argv, "--campaign-purpose"),
                "baseline_version": _option(argv, "--baseline-version"), "target_version": _option(argv, "--target-version")})
            return 0
        if "codex_upgrade_timing_ledger" in joined and "checkpoint" in argv:
            _write_json(Path(_option(argv, "--ledger-dir")) / _option(argv, "--output"), {"checkpoint": True})
            return 0
        if "codex_upgrade_arm64_environment_receipt" in joined:
            root = Path(_option(argv, "--evidence-root"))
            _write_json(root / ("facts.json" if "collect" in argv else "receipt.json"), {"phase": "p0"})
            return 0
        if "tools.official_client_capture.codex_upgrade" in argv and "plan" in argv:
            campaign = Path(_option(argv, "--campaign-dir"))
            campaign.mkdir()
            _write_json(campaign / "campaign.json", {"campaign_id": _option(argv, "--campaign-id")})
            return 0
        if "codex_upgrade_job_rehearsal_receipt" in joined:
            root = Path(_option(argv, "--evidence-root"))
            if "collect" in argv:
                _write_json(root / "facts.json", {"rerun_failed": "--rerun-failed" in argv})
            else:
                status = self.job_statuses.pop(0) if self.job_statuses else "passed"
                _write_json(root / "receipt.json", {"status": status})
            return 0
        if "client_launch_probe.py" in joined:
            if "run" in argv:
                _write_json(Path(_option(argv, "--output-dir")) / "report.json", {"status": "passed"})
            return 0
        if "certify_release" in joined:
            if "issue" in argv:
                _write_json(Path(_option(argv, "--output")), {"status": "passed"})
            return 0
        if "codex_upgrade_vc_receipt" in joined:
            root = Path(_option(argv, "--evidence-root"))
            if "finalize" in argv:
                self.p0_facts = json.loads((root / _option(argv, "--facts")).read_text(encoding="utf-8"))
                _write_json(root / _option(argv, "--output"), {"kind": "p0_gate", "status": "passed"})
            return 0
        if "codex_upgrade_vc0_closeout" in joined:
            self.machine.append("closeout")
            self.closeout_argv = argv
            rc, payload = self.closeout_results.pop(0) if self.closeout_results else (0, {"status": "passed"})
            if rc == 0:
                _write_json(Path(_option(argv, "--audit-dir")) / "receipt.json", payload)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return rc
        raise AssertionError(f"替身没有覆盖这条命令：{argv}")


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.fx = Fixture(root)
        # E2-05 夹具预先放了账本与 smoke 收据（作为现成产物）；编排器测试从没有账本的新一轮开始。
        shutil.rmtree(self.fx.data / "control" / f"{self.fx.params['UP']}-timing-ledger")
        (self.fx.data / "audit" / f"zero-request-smoke-{self.fx.params['STAMP']}.json").unlink()
        self.fx.params.update({"RUNROOT": str(root / "run"), "ENTRY_BUNDLE": str(root / "x.bundle"),
                               "ENTRY_BRANCH": "codex/test", "BASELINE_RULES_JSON": str(root / "rules.json"),
                               "BASELINE_SCENARIOS_JSON": str(root / "scenarios.json")})
        self.commands = Commands(self.fx)
        self.runs = 0

    def orchestrate(self, **kwargs: Any) -> dict:
        self.runs += 1
        masks = kwargs.pop("masks", None)
        approval = kwargs.pop("approval", None)
        reexecute_gates = kwargs.pop("reexecute_gates", False)
        full_set_request = kwargs.pop("full_set_request", None)
        full_set_work = kwargs.pop("full_set_work", None)
        orchestrator = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "runs" / str(self.runs),
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, masks=masks,
                                       approval=approval, reexecute_gates=reexecute_gates, full_set_request=full_set_request, full_set_work=full_set_work)
        return orchestrator.orchestrate(**kwargs)

    @staticmethod
    def actions(result: dict) -> dict[str, str]:
        return {step["step_id"]: step["action"] + (("／" + step["status"]) if step["status"] else "") for step in result["steps"]}


class OrchestratorFlowTests(OrchestratorTestCase):
    def test_rerun_rechecks_gates_and_never_rebuilds_the_ledger(self) -> None:
        first = self.orchestrate()
        self.assertEqual(first["exit_code"], 0, self.actions(first))
        self.assertTrue(all(action == "执行／passed" for action in self.actions(first).values()), self.actions(first))
        second = self.orchestrate()
        self.assertEqual(second["exit_code"], 0)
        actions = self.actions(second)
        self.assertEqual({step for step, action in actions.items() if action != "沿用"}, {"entry-preflight", "client-launch-probe", "entry-gates", "p0-receipt", "vc0-closeout"})
        self.assertEqual(self.commands.count("create"), 1, "账本只建一次")

    def test_occupied_formal_coordinates_get_serial_paths_and_downstream_uses_the_record(self) -> None:
        result = self.orchestrate()
        products = {step["step_id"]: step["products"] for step in result["steps"]}
        compat = Path(products["policy-compatibility"]["compatibility"])
        self.assertEqual(compat.name, "compat-r2.json", "夹具里的正式坐标被占用（没有记录的旧产物），不沿用、换序号")
        activation_call = next(call for call in self.commands.calls if "activation" in call)
        self.assertEqual(_option(activation_call, "--compatibility-receipt"), str(compat))

    def test_pre_ledger_failures_are_reported_together(self) -> None:
        self.commands.preflight_failed = ["required-parameters", "plan-audit"]
        self.commands.gates_status = "failed"
        result = self.orchestrate()
        actions = self.actions(result)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(actions["entry-preflight"], "执行／failed")
        self.assertEqual(actions["entry-gates"], "执行／failed", "便宜检查没过，入口门禁照样跑完报出失败的测试")
        self.assertEqual((actions["zero-request-smoke"], actions["atomic-double"]), ("执行／passed", "执行／passed"))
        self.assertEqual({actions[step] for step in eo.ORDER if step not in eo.PRE_LEDGER}, {"未到达"}, "建账本之前没全过，不建账本")
        self.assertEqual(result["preflight_failed"], ["required-parameters", "plan-audit"])
        self.assertEqual(self.commands.count("create"), 0)

    def test_failed_deployment_binding_blocks_its_dependents(self) -> None:
        self.commands.preflight_failed = ["deployment-identity"]
        result = self.orchestrate()
        actions = self.actions(result)
        self.assertEqual({step: actions[step] for step in ("policy-activation", "entry-gates", "pre-a3")},
                         {"policy-activation": "被阻塞", "entry-gates": "被阻塞", "pre-a3": "被阻塞"})
        self.assertEqual(actions["policy-compatibility"], "执行／passed")
        self.assertIn("便宜检查 deployment-identity", "；".join(next(s for s in result["steps"] if s["step_id"] == "entry-gates")["reasons"]))
        self.assertEqual(self.commands.count("--pre-a3-mode"), 0, "被阻塞的不执行")

    def test_injected_failures_resume_from_the_failed_step_without_rebuilding_the_ledger(self) -> None:
        """预检 plan、Job 演练、发布认证各注入一次：每次「修好后重新执行同一条命令」都从失败的那一步接着做，同时带上
        下一处注入；前面的步骤沿用（便宜检查与启动探测每次执行），账本只建一次。"""

        chain = (("preflight-plan", "/env-mask"), ("job-rehearsal", "/root/oauth-capture"),
                 ("release-certification", "/usr/local/go"), (None, None))
        previous: str | None = None
        for step, mask in chain:
            result = self.orchestrate(masks={step: mask} if step else None)
            actions = self.actions(result)
            if previous:
                self.assertEqual(actions[previous], "执行／passed", f"{previous} 续跑通过")
                before = eo.ORDER[:eo.ORDER.index(previous)]
                self.assertEqual({s for s in before if actions[s] != "沿用"},
                                 {s for s in before if eo.ES.STEP_BY_ID[s].live or s == "entry-gates"}, f"{previous} 之前只重做实时检查和 B-09 准入")
            if step:
                self.assertEqual((result["exit_code"], actions[step]), (1, "执行／failed"), step)
                self.assertIn(f"{step} 没通过", result["stopped_because"])
            else:
                self.assertEqual(result["exit_code"], 0, actions)
            previous = step
        self.assertEqual(self.commands.count("create"), 1, "三次注入、三次续跑，账本只建了一次")

    def test_failed_job_rehearsal_receipt_is_not_reused_and_the_rerun_only_retries_failures(self) -> None:
        self.commands.job_statuses = ["failed"]
        failed = self.orchestrate()
        self.assertEqual(self.actions(failed)["job-rehearsal"], "执行／failed")
        self.assertIn("Job 演练收据不是通过", "；".join(next(s for s in failed["steps"] if s["step_id"] == "job-rehearsal")["reasons"]))
        first_root = Path(next(s for s in failed["steps"] if s["step_id"] == "job-rehearsal")["products"]["receipt"]).parent
        resumed = self.orchestrate()
        self.assertEqual(resumed["exit_code"], 0)
        collect = [call for call in self.commands.calls if "codex_upgrade_job_rehearsal_receipt" in " ".join(call) and "collect" in call][-1]
        self.assertIn("--rerun-failed", collect)
        self.assertEqual(collect[collect.index("--previous-receipt-root") + 1], str(first_root))
        second_root = Path(collect[collect.index("--evidence-root") + 1])
        self.assertEqual(second_root.name, first_root.name + "-r2")

    def test_legacy_failed_receipt_without_record_is_not_reused(self) -> None:
        self.orchestrate(to_step="preflight-plan")
        base = self.fx.data / "control" / f"{self.fx.params['CAMPAIGN_PREFIX']}-job-rehearsal-vc5-{self.fx.params['ROUND']}-{self.fx.params['STAMP']}"
        _write_json(base / "receipt.json", {"status": "failed"})
        result = self.orchestrate()
        self.assertEqual(result["exit_code"], 0)
        job = next(s for s in result["steps"] if s["step_id"] == "job-rehearsal")
        self.assertEqual(Path(job["products"]["receipt"]).parent.name, base.name + "-r2")

    def test_plan_only_range_and_formal_freeze(self) -> None:
        planned = self.orchestrate(plan_only=True)
        self.assertEqual(set(self.actions(planned).values()), {"只判定"})
        self.assertEqual(self.commands.calls, [])
        with self.assertRaisesRegex(eo.OrchestratorError, "之前的步骤 .* 要重做"):
            self.orchestrate(from_step="job-rehearsal")
        partial = self.orchestrate(to_step="ledger")
        actions = self.actions(partial)
        self.assertEqual(actions["ledger"], "执行／passed")
        self.assertEqual(actions["environment-p0"], "范围外")
        # E2-07：Formal 建成后（收口模块判定），创建链冻结，只剩收口这一步续作；收口完成后再执行就全部沿用／冻结。
        _write_json(self.fx.data / "evidence" / "campaigns" / self.fx.params["NEW"] / "campaign.json", {})
        self.fx.params["P0_ROLLBACK_EVIDENCE"] = self.fx.params["P0_ROLLBACK_EVIDENCE"]
        frozen = self.orchestrate()
        actions = self.actions(frozen)
        self.assertEqual({step for step, action in actions.items() if action != "冻结"}, {"vc0-closeout"})
        self.assertEqual(frozen["exit_code"], 1, "前序步骤从没执行过：收口取不到预检 Campaign 记录，失败而不是越过")
        self.assertIn("preflight-plan", "；".join(next(s for s in frozen["steps"] if s["step_id"] == "vc0-closeout")["reasons"]))


class OrchestratorCloseoutTests(OrchestratorTestCase):
    """E2-07：P0 收据与 VC-0 收口两个固定步骤，以及 Formal 建成之后只剩收口续作。"""

    def test_p0_receipt_is_assembled_from_the_entry_gates_run_and_the_ledger_plan(self) -> None:
        result = self.orchestrate()
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        products = {step["step_id"]: step["products"] for step in result["steps"]}
        root = Path(products["p0-receipt"]["receipt"]).parent
        self.assertEqual(sorted(path.name for path in (root / "evidence").iterdir()),
                         ["check-egress-spec.json", "release-certification.json", "rollback-rollback-receipt.json",
                          "test-capture-tools.json"])
        facts = self.commands.p0_facts
        self.assertEqual(facts["subject"]["upgrade_id"], self.fx.params["UP"])
        self.assertEqual((facts["subject"]["baseline_version"], facts["subject"]["target_version"]), ("0.157.0", "0.160.0"))
        self.assertEqual([gate["gate_id"] for gate in facts["assertions"]["offline_gates"]], ["check-egress-spec", "test-capture-tools"])
        self.assertEqual({item["role"] for item in facts["evidence"]},
                         {"check_egress_spec", "release_certification", "rollback", "test_capture_tools"})
        closeout = self.commands.closeout_argv
        self.assertEqual(_option(closeout, "--p0-gate-receipt"), "p0-receipt.json")
        self.assertEqual(_option(closeout, "--supervisor-state-dir"),
                         str(self.fx.data / "control" / f"{self.fx.params['NEW']}-supervisor"), "状态目录在控制根下一层")
        self.assertEqual(_option(closeout, "--fix-commit"), self.fx.params["ENTRY_COMMIT"])
        self.assertEqual(_option(closeout, "--regression-receipt"), products["entry-gates"]["summary"])
        self.assertNotIn("--approve-sha256", closeout)

    def test_failed_gate_evidence_refuses_to_sign_p0(self) -> None:
        self.commands.p0_evidence_failed = {"test-capture-tools"}
        result = self.orchestrate()
        self.assertEqual(self.actions(result)["p0-receipt"], "执行／failed")
        self.assertIn("test-capture-tools 证据不是通过", "；".join(next(s for s in result["steps"] if s["step_id"] == "p0-receipt")["reasons"]))
        self.assertEqual(self.commands.count(eo.VC_RECEIPT_MODULE), 0, "证据没通过就不签 P0")
        self.assertEqual(self.actions(result)["vc0-closeout"], "未到达")

    def test_entry_gates_default_to_reexecute_and_never_skip_contract_check(self) -> None:
        """B-09：无请求默认全量重跑；再次开工也必须重新核对，旧步骤缓存不能旁路。"""

        first = self.orchestrate()
        self.assertEqual(first["exit_code"], 0, self.actions(first))
        self.assertEqual(self.commands.gates_modes, ["re-execute"])
        second = self.orchestrate(reexecute_gates=True)
        self.assertEqual(second["exit_code"], 0, self.actions(second))
        actions = self.actions(second)
        self.assertEqual((actions["entry-gates"], actions["p0-receipt"], actions["vc0-closeout"]), ("执行／passed",) * 3)
        self.assertEqual(actions["ledger"], "沿用")
        self.assertEqual(self.commands.gates_modes, ["re-execute", "re-execute"])
        reasons = next(step for step in second["steps"] if step["step_id"] == "entry-gates")["reasons"]
        self.assertIn("B-09", "；".join(reasons))

    def test_full_set_request_reaches_the_gate_and_force_reexecute_is_preserved(self) -> None:
        request = self.fx.root / "request.json"
        self.orchestrate(full_set_request=request, full_set_work=self.fx.root / "background-work")
        self.assertEqual(self.commands.gates_modes[-1], "full-set-pass")
        gate = next(call for call in self.commands.calls if "--full-set-request" in call)
        self.assertEqual(_option(gate, "--full-set-request"), str(request))
        self.assertEqual(_option(gate, "--work"), str(self.fx.root / "background-work"))
        self.orchestrate(full_set_request=request, reexecute_gates=True)
        self.assertEqual(self.commands.gates_modes[-1], "re-execute")

    def test_pre_a3_redo_still_rechecks_the_full_set_contract(self) -> None:
        """pre-A3 缺失时重签，同时重新核对 B-09；不能因旧入口记录仍在就跳过时效检查。"""

        first = self.orchestrate()
        self.assertEqual(first["exit_code"], 0, self.actions(first))
        products = {step["step_id"]: step["products"] for step in first["steps"]}
        Path(products["pre-a3"]["certification"]).unlink()
        before = len(self.commands.calls)
        second = self.orchestrate()
        actions = self.actions(second)
        self.assertEqual((actions["entry-gates"], actions["pre-a3"]), ("执行／passed", "执行／passed"), actions)
        gates = [call for call in self.commands.calls[before:] if call[0] == "bash" and call[1].endswith("entry-gates.sh")]
        self.assertEqual(len(gates), 1, gates)
        self.assertEqual((_option(gates[0], "--profile"), _option(gates[0], "--pre-a3-mode"), _option(gates[0], "--mode")),
                         ("entry", "run", "re-execute"))
        self.assertEqual(gates[0][-3:], [self.fx.params[key] for key in ("ENTRY_BUNDLE", "ENTRY_BRANCH", "ENTRY_COMMIT")])
        self.assertFalse(any("run-commands" in call for call in self.commands.calls), "不再走 run-commands")

    def test_withheld_p0_evidence_fails_the_p0_step_with_the_reason(self) -> None:
        """入口门禁承接了单元却写不出 v2 P0 证据、扣下证据时（E3-03）：入口门禁这一步照样通过（两份 P0 证据是可选产物），
        P0 收据这一步失败并写明原因与出路（--reexecute-gates）。"""

        self.commands.p0_evidence_withheld = {"test-capture-tools"}
        result = self.orchestrate()
        actions = self.actions(result)
        self.assertEqual((actions["entry-gates"], actions["p0-receipt"]), ("执行／passed", "执行／failed"))
        reasons = "；".join(next(step for step in result["steps"] if step["step_id"] == "p0-receipt")["reasons"])
        self.assertIn("test-capture-tools 没有可签 P0 收据的证据", reasons)
        self.assertIn("运行清单没有发布进记录库", reasons)
        self.assertIn("--reexecute-gates", reasons)
        self.assertFalse(any("codex_upgrade_vc_receipt" in " ".join(call) for call in self.commands.calls), "不签 P0 收据")

    def test_closeout_needing_approval_blocks_then_the_approval_is_passed_through(self) -> None:
        self.commands.closeout_results = [(3, {"status": "blocked", "kind": "approval-required", "message": "等待批准",
                                               "next_command": "同一命令加 --approve-sha256 abc --approved-by <批准人>"})]
        blocked = self.orchestrate()
        self.assertEqual(blocked["exit_code"], 3)
        self.assertEqual(self.actions(blocked)["vc0-closeout"], "阻塞")
        self.assertIn("approval-required", blocked["stopped_because"])
        self.assertIn("--approve-sha256 abc", blocked["stopped_because"])
        approved = self.orchestrate(approval={"approve_sha256": "abc", "approved_by": "测试批准人", "reason": "已修复"})
        self.assertEqual(approved["exit_code"], 0, self.actions(approved))
        closeout = self.commands.closeout_argv
        self.assertEqual((_option(closeout, "--approve-sha256"), _option(closeout, "--approved-by"), _option(closeout, "--reason")),
                         ("abc", "测试批准人", "已修复"))
        before = eo.ORDER[:eo.ORDER.index("vc0-closeout")]
        self.assertEqual({step for step in before if self.actions(approved)[step] != "沿用"},
                         {step for step in before if eo.ES.STEP_BY_ID[step].live or step in {"entry-gates", "p0-receipt"}}, "批准后仍核对 B-09，重签相应 P0，账本不重建")
        self.assertEqual(self.commands.count("create"), 1)

    def test_closeout_holds_the_machine_reservation_around_the_first_batch(self) -> None:
        """收口同步派发 VC-1 首批（真实采集）：开始前向统一调度执行器申请整机、收口返回后归还（失败也归还）；调度器不批准
        时不收口（E4-01）。"""

        self.commands.closeout_results = [(1, {"status": "failed"})]
        failed = self.orchestrate()
        self.assertEqual(self.actions(failed)["vc0-closeout"], "执行／failed")
        self.assertEqual(self.commands.machine, ["acquire", "closeout", "release"], "收口失败也归还整机")
        acquire = next(call for call in self.commands.calls if "unit_executor.py" in " ".join(call) and "acquire" in call)
        self.assertEqual((_option(acquire, "--owner"), _option(acquire, "--owner-pid")),
                         (f"vc0-closeout-{self.fx.params['NEW']}", str(os.getpid())))
        self.commands.machine.clear()
        self.commands.acquire_granted = False
        refused = self.orchestrate()
        closeout = next(step for step in refused["steps"] if step["step_id"] == "vc0-closeout")
        self.assertEqual((refused["exit_code"], closeout["status"]), (1, "failed"))
        self.assertIn("申请整机没有批准", "；".join(closeout["reasons"]))
        self.assertEqual(self.commands.machine, ["acquire"], "没批准就不收口、也不用归还")

    def test_rehearsal_root_never_runs_the_closeout(self) -> None:
        """验收演练根（ENTRY_ROOT 在数据根 staging 下）不做 VC-0 收口：首批是真实官方取证，会发正式请求。"""

        rehearsal = self.fx.data / "staging" / "e2-07-rehearsal"
        rehearsal.mkdir(parents=True)
        self.fx.params["ENTRY_ROOT"] = str(rehearsal)
        result = self.orchestrate()
        closeout = next(step for step in result["steps"] if step["step_id"] == "vc0-closeout")
        self.assertEqual((result["exit_code"], closeout["action"], closeout["status"]), (1, "执行", "failed"))
        self.assertIn("不做 VC-0 收口", "；".join(closeout["reasons"]))
        self.assertEqual(self.actions(result)["p0-receipt"], "执行／passed", "P0 收据不发请求，演练根照样签")
        self.assertFalse(any(eo.CLOSEOUT_MODULE in " ".join(call) for call in self.commands.calls))

    def test_after_formal_is_built_only_the_closeout_continues(self) -> None:
        self.commands.closeout_results = [(1, {"status": "failed"})]
        first = self.orchestrate()
        self.assertEqual((first["exit_code"], self.actions(first)["vc0-closeout"]), (1, "执行／failed"))
        # 收口建成了 Formal 之后失败（例如派发首批前预算暂停）：再执行时创建链冻结，只续作收口。
        _write_json(self.fx.data / "evidence" / "campaigns" / self.fx.params["NEW"] / "campaign.json", {})
        resumed = self.orchestrate()
        actions = self.actions(resumed)
        self.assertEqual(resumed["exit_code"], 0, actions)
        self.assertEqual({step for step, action in actions.items() if action != "冻结"}, {"vc0-closeout"})
        self.assertEqual(actions["vc0-closeout"], "执行／passed")
        probe_verifies = [call for call in self.commands.calls
                          if any(item.endswith("client_launch_probe.py") for item in call) and "verify" in call]
        self.assertEqual(len(probe_verifies), 2, "Formal 已建后不再复核启动探测（只有前一次执行里步骤本身与收口前预检两次）")
        again = self.orchestrate()
        self.assertEqual(self.actions(again)["vc0-closeout"], "沿用")
        self.assertIn("收口已完成", again["stopped_because"])


class OrchestratorPieceTests(unittest.TestCase):
    def test_fresh_and_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(eo.fresh(root / "a.json"), root / "a.json")
            (root / "a.json").write_text("{}")
            (root / "a-r2.json").write_text("{}")
            self.assertEqual(eo.fresh(root / "a.json"), root / "a-r3.json")
            (root / "dir").mkdir()
            self.assertEqual(eo.fresh(root / "dir"), root / "dir-r2")
        argv = eo.mask_argv(["python3", "-m", "x"], "/root/oauth-capture")
        self.assertEqual(argv[:4], ["unshare", "-m", "--propagation", "private"])
        self.assertIn("tmpfs /root/oauth-capture", argv[6])
        self.assertEqual(argv[-3:], ["python3", "-m", "x"])

    def test_driver_carries_identical_copies_and_the_wrapper(self) -> None:
        for name in ("entry_orchestrator.py", "entry_steps.py"):
            self.assertEqual((DRIVER / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes(), name)
        wrapper = (DRIVER / "entry.sh").read_text(encoding="utf-8")
        self.assertIn('source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"', wrapper)
        self.assertIn('exec python3 -B "$DRV/entry_orchestrator.py" "$@"', wrapper)

    def test_entry_parameters(self) -> None:
        loader = __import__("importlib.util").util
        spec = loader.spec_from_file_location("parse_env_entry", DRIVER / "parse_env.py")
        assert spec is not None and spec.loader is not None
        parse_env = loader.module_from_spec(spec)
        spec.loader.exec_module(parse_env)

        def text(**extra: str) -> str:
            values = {key: "x" for key in parse_env.REQUIRED_KEYS}
            # 入口参数仍遵守 A-08 的规范路径合同，其余必需字段可使用安全占位值。
            values.update({"D": "/data", "RUNROOT": "/rounds/entry-test", "C": "a" * 40, "DC": "b" * 40, "BASELINE_VERSION": "0.157.0", "TARGET_VERSION": "0.160.0",
                           "CODEX_BIN_SHA256": "c" * 64, "OFFICIAL_ASSET_SHA256": "d" * 64, "PROFILE_DIGEST": "e" * 64,
                           "KILO_SHA256": "f" * 64, "CODEX_ACCOUNT_ID": "90", "API_KEY_ID": "7", "PROFILE_ID": "p1",
                           "TARGET_PROFILE_ID": "p1", "MAIN_MODEL": "m", "LITE_MODEL": "l"})
            values.update(extra)
            return "".join(f"{key}={value}\n" for key, value in values.items())

        values = parse_env.parse(text())
        self.assertEqual(parse_env.derive(values)["ENTRY_ROOT"], "/data", "产物根缺省是数据根")
        values = parse_env.parse(text(ENTRY_ROOT="/data/staging/e2-06-x", ENTRY_COMMIT="1" * 40, ENTRY_BRANCH="codex/a",
                                      ENTRY_BUNDLE="/b/x.bundle"))
        self.assertEqual(parse_env.derive(values)["ENTRY_ROOT"], "/data/staging/e2-06-x")
        for key, value in (("ENTRY_COMMIT", "abc"), ("ENTRY_ROOT", "/elsewhere"), ("ENTRY_ROOT", "/data/staging/../x"),
                           ("ENTRY_BRANCH", "bad.branch!"), ("ENTRY_BUNDLE", "relative.bundle")):
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(parse_env.EnvFileError, key):
                    parse_env.parse(text(**{key: value}))


if __name__ == "__main__":
    unittest.main()
