"""入口编排器（E2-06，``tools/ci/entry_orchestrator.py``）。

沿用 E2-05 的夹具（数据根、驱动、参数、判定用的命令替身），再给编排器一个命令替身：便宜检查、策略认证、入口门禁、
pre-A3、smoke、atomic-double、建账本、环境收据、checkpoint、预检 plan、Job 演练、启动探测、发布认证都按参数在夹具里
造出产物。按方案 E2-06 的验收核对：建账本之前一次报全、部署绑定没过时依赖步骤被阻塞；三处注入失败后重新执行同一条
命令从失败步骤续跑、账本不重建；失败的 Job 演练收据不沿用、续跑只重跑失败项；被占用的正式坐标换序号；起止步骤与只判定。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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
        self.environments: list[dict[str, str]] = []
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
        self.environments.append(dict(env))
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
        gate_parallel = kwargs.pop("gate_parallel", None)
        audit_reads = kwargs.pop("audit_reads", False)
        audit_strict = kwargs.pop("audit_strict", False)
        audit_policy = kwargs.pop("audit_policy", None)
        unit_inherit_request = kwargs.pop("unit_inherit_request", None)
        unit_inherit_full_gates_request = kwargs.pop("unit_inherit_full_gates_request", None)
        params = kwargs.pop("params", None)
        scope_options = {key: kwargs.pop(key) for key in ("gate_scope", "select_base") if key in kwargs}
        orchestrator = eo.Orchestrator(params or self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "runs" / str(self.runs),
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, masks=masks,
                                       approval=approval, reexecute_gates=reexecute_gates, full_set_request=full_set_request,
                                       full_set_work=full_set_work, gate_parallel=gate_parallel,
                                       audit_reads=audit_reads, audit_strict=audit_strict, audit_policy=audit_policy,
                                       unit_inherit_request=unit_inherit_request,
                                       unit_inherit_full_gates_request=unit_inherit_full_gates_request, **scope_options)
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
        self.assertTrue(all("--parallel" not in call for call in self.commands.calls), "缺省门禁 argv 不增加并发参数")
        reasons = next(step for step in second["steps"] if step["step_id"] == "entry-gates")["reasons"]
        self.assertIn("B-09", "；".join(reasons))

    def test_full_set_request_reaches_the_gate_and_force_reexecute_is_preserved(self) -> None:
        request = self.fx.root / "request.json"
        self.orchestrate(full_set_request=request, full_set_work=self.fx.root / "background-work")
        self.assertEqual(self.commands.gates_modes[-1], "full-set-pass")
        gate = next(call for call in self.commands.calls if "--full-set-request" in call)
        self.assertEqual(_option(gate, "--full-set-request"), str(request))
        self.assertEqual(_option(gate, "--work"), str(self.fx.root / "background-work"))
        for call, env in zip(self.commands.calls, self.commands.environments):
            if call[0] == "bash" and call[1].endswith("entry-gates.sh"):
                self.assertEqual({key: env.get(key) for key in ("GOCACHE", "GOMODCACHE", "GOTMPDIR")},
                                 {key: os.environ.get(key) for key in ("GOCACHE", "GOMODCACHE", "GOTMPDIR")},
                                 "B-09 不注入逐单元承接缓存，仍用原环境")
        self.orchestrate(full_set_request=request, reexecute_gates=True)
        self.assertEqual(self.commands.gates_modes[-1], "re-execute")

    def test_explicit_parallel_reaches_entry_and_pre_a3_only_calls(self) -> None:
        orchestrator = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "parallel-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner,
                                       reexecute_gates=True, gate_parallel=2)
        result = orchestrator.orchestrate(to_step="p0-receipt")
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
        orchestrator.entry_gates_and_pre_a3(False, True)
        gates = [call for call in self.commands.calls if call[:1] == ["bash"] and call[1].endswith("entry-gates.sh")]
        self.assertEqual([_option(call, "--profile") for call in gates], ["entry", "pre-a3"])
        self.assertEqual([_option(call, "--parallel") for call in gates], ["2", "2"])
        self.assertTrue(all("--work" not in call for call in gates), "普通入口沿用原工作目录")

    def test_two_requests_keep_pre_a3_reusable_across_the_actual_profile_change(self) -> None:
        """两轮参数及请求字节完全相同；只按真实组合选请求，不因切换输入让 pre-A3 循环重跑。"""

        primary, companion = self.fx.root / "entry-request.json", self.fx.root / "full-gates-request.json"
        _write_json(primary, {"profile": "entry", "approved": "首轮独立批准"})
        _write_json(companion, {"profile": "full-gates", "approved": "续跑独立批准"})
        (self.fx.driver / "unit_inherit.py").write_text("# 逐单元承接合同替身\n", encoding="utf-8")
        original = [path.read_bytes() for path in (primary, companion)]
        work = self.fx.root / "source-work"
        work.mkdir()
        self.fx.params[eo.ES.GATE_WORK_PARAM] = "外层残留必须清除"
        options = {"unit_inherit_request": primary, "unit_inherit_full_gates_request": companion, "full_set_work": work}
        first = self.orchestrate(to_step="atomic-double", **options)
        second = self.orchestrate(to_step="atomic-double", **options)
        third = self.orchestrate(to_step="p0-receipt", **options)
        self.assertEqual([run["exit_code"] for run in (first, second, third)], [0, 0, 0])
        self.assertEqual(self.actions(second)["pre-a3"], "沿用", self.actions(second))
        self.assertEqual(self.actions(third)["pre-a3"], "沿用", self.actions(third))
        gates = [call for call in self.commands.calls if call[0] == "bash" and call[1].endswith("entry-gates.sh")]
        self.assertEqual([_option(call, "--pre-a3-mode") for call in gates], ["run", "present", "present"])
        self.assertEqual([_option(call, "--unit-inherit-request") for call in gates], [str(primary), str(companion), str(companion)])
        self.assertEqual([_option(call, "--profile") for call in gates], ["entry"] * 3, "脚本按 pre-A3 模式生成真实门禁全集")
        self.assertEqual(self.commands.gates_modes, ["unit-inherit"] * 3)
        self.assertEqual([path.read_bytes() for path in (primary, companion)], original, "不改写任何请求或批准")
        self.assertEqual([_option(call, "--work") for call in gates], [str(work)] * 3)
        # pre-A3 单独重做的调用分支也必须使用同一工作区与三缓存。
        orchestrator = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "source-pre-a3",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, **options)
        orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
        orchestrator.entry_gates_and_pre_a3(False, True)
        self.assertEqual(_option(self.commands.calls[-1], "--profile"), "pre-a3")
        self.assertEqual(_option(self.commands.calls[-1], "--work"), str(work))
        expected_caches = {"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"),
                           "GOTMPDIR": str(work / "go-tmp")}
        for call, env in zip(self.commands.calls, self.commands.environments):
            actual = {key: env.get(key) for key in expected_caches}
            if call[0] == "bash" and call[1].endswith("entry-gates.sh"):
                self.assertEqual(actual, expected_caches)
            else:
                self.assertEqual(actual, {key: os.environ.get(key) for key in expected_caches}, "其它步骤不注入来源缓存")
        self.assertEqual(orchestrator.params[eo.ES.GATE_WORK_PARAM], str(work))
        plain = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "plain",
                                steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner)
        self.assertNotIn(eo.ES.GATE_WORK_PARAM, plain.params, "默认入口不能继承外层内部参数")
        # 编排器建成后、实际门禁派发前才替换缓存目录，也必须拒绝，不能只在构造时查 work 根。
        for name in ("go-build-cache", "go-mod-cache", "go-tmp"):
            with self.subTest(cache=name):
                cache = work / name
                cache.symlink_to(self.fx.root, target_is_directory=True)
                before = len(self.commands.calls)
                orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
                with self.assertRaisesRegex(eo.OrchestratorError, "无符号链接的规范绝对路径"):
                    orchestrator.entry_gates_and_pre_a3(False, True)
                self.assertEqual(len(self.commands.calls), before, "缓存链接变化时不得派发门禁子进程")
                cache.unlink()

    def test_strict_audit_reaches_entry_and_pre_a3_only_calls(self) -> None:
        """主入口必须运输严格审计和登记合同，两种门禁调用分支都不能丢参数。"""
        policy = self.fx.root / "audit-policy.json"
        _write_json(policy, {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}})
        orchestrator = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "audit-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner,
                                       reexecute_gates=True, audit_reads=True, audit_strict=True, audit_policy=policy)
        result = orchestrator.orchestrate(to_step="atomic-double")
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
        orchestrator.entry_gates_and_pre_a3(False, True)
        gates = [call for call in self.commands.calls if call[0] == "bash" and call[1].endswith("entry-gates.sh")]
        self.assertEqual([_option(call, "--profile") for call in gates], ["entry", "pre-a3"])
        self.assertTrue(all("--audit-reads" in call and "--audit-strict" in call for call in gates))
        self.assertEqual([_option(call, "--audit-policy") for call in gates], [str(policy)] * 2)

    def test_strict_policy_bytes_are_bound_and_the_ledger_is_preserved(self) -> None:
        """同一策略路径换字节须重验，未变时沿用 pre-A3，不能重建已完成账本。"""
        policy, request = self.fx.root / "audit-policy.json", self.fx.root / "inherit.json"
        _write_json(policy, {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}})
        _write_json(request, {"revision": 1})
        (self.fx.driver / "unit_inherit.py").write_text("# 逐单元承接合同替身\n", encoding="utf-8")
        options = {"unit_inherit_request": request, "audit_reads": True, "audit_strict": True, "audit_policy": policy}
        first = self.orchestrate(to_step="p0-receipt", **options)
        unchanged = self.orchestrate(to_step="p0-receipt", **options)
        self.assertEqual([run["exit_code"] for run in (first, unchanged)], [0, 0])
        self.assertEqual(self.actions(unchanged)["pre-a3"], "沿用")
        policy.write_text(policy.read_text() + "\n", encoding="utf-8")
        changed = self.orchestrate(to_step="p0-receipt", **options)
        self.assertEqual(changed["exit_code"], 0, self.actions(changed))
        self.assertEqual(self.actions(changed)["pre-a3"], "执行／passed")
        self.assertEqual(self.actions(changed)["ledger"], "沿用")
        self.assertEqual(self.commands.count("create"), 1)

        work = self.fx.root / "source-work"
        work.mkdir()
        options["full_set_work"] = work
        selected = self.orchestrate(to_step="p0-receipt", **options)
        self.assertEqual(self.actions(selected)["pre-a3"], "执行／passed")
        self.assertEqual(self.actions(self.orchestrate(to_step="p0-receipt", **options))["pre-a3"], "沿用")
        options["full_set_work"] = self.fx.root / "other-source-work"
        changed_work = self.orchestrate(to_step="p0-receipt", **options)
        self.assertEqual((changed_work["exit_code"], self.actions(changed_work)["pre-a3"], self.actions(changed_work)["ledger"]),
                         (0, "执行／passed", "沿用"))
        self.assertEqual(self.commands.count("create"), 1, "工作区变更只使受影响步骤失效，不重建账本")

    def test_changing_either_fixed_request_invalidates_pre_a3(self) -> None:
        primary, companion = self.fx.root / "entry-request.json", self.fx.root / "full-gates-request.json"
        for path in (primary, companion):
            _write_json(path, {"revision": 1})
        (self.fx.driver / "unit_inherit.py").write_text("# 逐单元承接合同替身\n", encoding="utf-8")
        options = {"unit_inherit_request": primary, "unit_inherit_full_gates_request": companion}
        self.assertEqual(self.orchestrate(to_step="atomic-double", **options)["exit_code"], 0)
        for path in (companion, primary):
            with self.subTest(request=path.name):
                _write_json(path, {"revision": 2})
                changed = self.orchestrate(to_step="atomic-double", **options)
                self.assertEqual(self.actions(changed)["pre-a3"], "执行／passed")
                gate = next(call for call in reversed(self.commands.calls) if call[0] == "bash" and call[1].endswith("entry-gates.sh"))
                self.assertEqual(_option(gate, "--pre-a3-mode"), "run")
                self.assertEqual(_option(gate, "--unit-inherit-request"), str(primary))

    def test_missing_companion_preserves_the_original_request_and_contract_fallback(self) -> None:
        primary = self.fx.root / "entry-request.json"
        _write_json(primary, {"profile": "entry"})
        (self.fx.driver / "unit_inherit.py").write_text("# 逐单元承接合同替身\n", encoding="utf-8")
        original = primary.read_bytes()
        for _round in range(2):
            self.orchestrate(to_step="atomic-double", unit_inherit_request=primary)
        gates = [call for call in self.commands.calls if call[0] == "bash" and call[1].endswith("entry-gates.sh")]
        self.assertEqual([_option(call, "--unit-inherit-request") for call in gates], [str(primary)] * 2)
        self.assertEqual([_option(call, "--pre-a3-mode") for call in gates], ["run", "present"])
        self.assertEqual(primary.read_bytes(), original, "缺配套请求不能改标签冒充 full-gates 批准")

    def test_companion_request_cli_rejects_orphan_relative_and_full_set_modes(self) -> None:
        cases = [(["--unit-inherit-full-gates-request", "/full.json"], "与 --unit-inherit-request 一起提供"),
                 (["--unit-inherit-request", "/entry.json", "--unit-inherit-full-gates-request", "relative.json"], "绝对路径"),
                 (["--unit-inherit-request", "/entry.json", "--unit-inherit-full-gates-request", "/full.json", "--reexecute-gates"], "不能与"),
                 (["--unit-inherit-request", "/entry.json", "--unit-inherit-full-gates-request", "/full.json", "--full-set-request", "/b09.json"], "不能与")]
        for argv, reason in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(eo.main(argv), 2)
                self.assertIn(reason, stderr.getvalue())
        work = self.fx.root / "source-work"
        work.mkdir()
        link = self.fx.root / "linked-work"
        link.symlink_to(work, target_is_directory=True)
        parent = self.fx.root / "linked-parent"
        parent.symlink_to(self.fx.root, target_is_directory=True)
        for path in ("relative", str(link), str(parent / work.name), str(work / ".." / work.name)):
            with self.subTest(work=path), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(eo.main(["--unit-inherit-request", "/entry.json", "--full-set-work", path]), 2)
                self.assertIn("规范绝对路径", stderr.getvalue())
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(eo.main(["--unit-inherit-request", "/entry.json", "--full-set-work", str(work)]), 2)
            self.assertIn("参数里没有 D 或 RUNROOT", stderr.getvalue(), "合法配对已进入原入口参数检查")

    def test_parallel_change_redoes_pre_a3_but_keeps_the_ledger(self) -> None:
        first = self.orchestrate(gate_parallel=2)
        self.assertEqual(first["exit_code"], 0, self.actions(first))
        unchanged = self.orchestrate(gate_parallel=2)
        self.assertEqual(self.actions(unchanged)["pre-a3"], "沿用")
        changed = self.orchestrate(gate_parallel=3)
        actions = self.actions(changed)
        self.assertEqual(changed["exit_code"], 0, actions)
        self.assertEqual((actions["entry-gates"], actions["pre-a3"]), ("执行／passed", "执行／passed"))
        self.assertEqual(actions["ledger"], "沿用")
        self.assertEqual(self.commands.count("create"), 1)

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


class OrchestratorGateScopeTests(OrchestratorTestCase):
    """优化第 9 项：Codex 升级的入口门禁缺省走按客户端筛选（``--gate-scope auto``），Claude、全集承接、读集审计、显式重新执行全集一律保持全集。"""

    BASE = "b" * 40

    def entry_gate_calls(self) -> list[list[str]]:
        return [call for call in self.commands.calls if call[0] == "bash" and call[1].endswith("entry-gates.sh")]

    def scope_param(self, orchestrator_params: dict) -> str | None:
        return orchestrator_params.get(eo.ES.GATE_SCOPE_PARAM)

    def test_codex_default_hands_auto_to_the_entry_gate_without_guessing_a_base(self) -> None:
        result = self.orchestrate(to_step="atomic-double")
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        [gate] = self.entry_gate_calls()
        self.assertEqual((_option(gate, "--profile"), _option(gate, "--gate-scope")), ("entry", "auto"))
        self.assertNotIn("--select-base", gate, "基点缺省由入口门禁取测试树里最近的发版 tag，编排器不猜")
        self.assertNotIn("--include-claude", gate)

    def test_an_explicit_base_is_forwarded_and_bound_into_the_step_input(self) -> None:
        orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "scope-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, select_base=self.BASE)
        self.assertEqual(orchestrator.gate_scope_args, ["--gate-scope", "auto", "--select-base", self.BASE])
        self.assertEqual(orchestrator.params[eo.ES.GATE_SCOPE_PARAM], f"auto:{self.BASE}")
        names = {item.name for item in orchestrator.ctx.step("entry-gates").inputs}
        self.assertIn(f"param:{eo.ES.GATE_SCOPE_PARAM}", names)
        self.assertNotIn(f"param:{eo.ES.GATE_SCOPE_PARAM}", {item.name for item in orchestrator.ctx.step("pre-a3").inputs},
                         "范围只绑定入口门禁一步：切换范围不让昂贵的 pre-A3 认证重做")

    def test_full_forces_the_whole_set_and_leaves_the_step_inputs_as_before(self) -> None:
        result = self.orchestrate(to_step="atomic-double", gate_scope="full")
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        [gate] = self.entry_gate_calls()
        self.assertNotIn("--gate-scope", gate)
        orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "full-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, gate_scope="full")
        self.assertNotIn(eo.ES.GATE_SCOPE_PARAM, orchestrator.params)
        self.assertNotIn(f"param:{eo.ES.GATE_SCOPE_PARAM}", {item.name for item in orchestrator.ctx.step("entry-gates").inputs})

    def test_every_flow_that_needs_the_whole_set_keeps_it(self) -> None:
        request = self.fx.root / "request.json"
        primary, companion = self.fx.root / "entry-request.json", self.fx.root / "full-gates-request.json"
        for path in (primary, companion):
            _write_json(path, {"approved": True})
        (self.fx.driver / "unit_inherit.py").write_text("# 逐单元承接合同替身\n", encoding="utf-8")
        policy = self.fx.root / "policy.json"
        _write_json(policy, {"units": {}})
        cases = {
            "全集通过（B-09）": {"full_set_request": request, "full_set_work": self.fx.root / "background-work"},
            "逐单元承接": {"unit_inherit_request": primary},
            "显式重新执行全集": {"reexecute_gates": True},
            "读集审计": {"reexecute_gates": True, "audit_reads": True},
            "读集合同": {"reexecute_gates": True, "audit_reads": True, "audit_strict": True, "audit_policy": policy},
        }
        for name, options in cases.items():
            with self.subTest(name):
                self.commands.calls.clear()
                orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / f"keep-{abs(hash(name))}",
                                               steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, **options)
                self.assertEqual(orchestrator.gate_scope_args, [])
                self.assertNotIn(eo.ES.GATE_SCOPE_PARAM, orchestrator.params)

    def test_claude_is_never_scoped(self) -> None:
        params = {**self.fx.params, "CLIENT": "claude"}
        orchestrator = eo.Orchestrator(params, driver_dir=self.fx.driver, run_dir=self.fx.root / "claude-run", steps_dir=self.fx.steps_dir,
                                       runner=self.commands, es_runner=self.fx.runner)
        self.assertEqual(orchestrator.gate_scope_args, [])
        self.assertNotIn(eo.ES.GATE_SCOPE_PARAM, orchestrator.params, "Claude 的步骤输入与改造前逐字相同")
        explicit = eo.Orchestrator(params, driver_dir=self.fx.driver, run_dir=self.fx.root / "claude-explicit", steps_dir=self.fx.steps_dir,
                                   runner=self.commands, es_runner=self.fx.runner, gate_scope="client-plan", select_base=self.BASE)
        with self.assertRaisesRegex(eo.OrchestratorError, "本版只启用 codex 计划"):
            explicit.orchestrate()

    def test_explicit_client_plan_needs_a_base_and_refuses_whole_set_flows(self) -> None:
        for options, message in (
                ({"gate_scope": "client-plan"}, "必须同时给 --select-base"),
                ({"gate_scope": "client-plan", "select_base": "HEAD~1"}, "40 位小写提交"),
                ({"gate_scope": "full", "select_base": self.BASE}, "只在 --gate-scope client-plan／auto 下有意义"),
                ({"gate_scope": "client-plan", "select_base": self.BASE, "full_set_request": self.fx.root / "request.json"}, "走全集通过"),
                ({"gate_scope": "client-plan", "select_base": self.BASE, "reexecute_gates": True, "audit_reads": True}, "读集审计")):
            with self.subTest(options=options):
                orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "bad-run",
                                               steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, **options)
                with self.assertRaisesRegex(eo.OrchestratorError, message):
                    orchestrator.orchestrate()
        self.assertEqual(self.commands.calls, [], "选项冲突在开跑前就拒绝，一条命令都没执行")
        bad = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "bad-scope", steps_dir=self.fx.steps_dir,
                              runner=self.commands, es_runner=self.fx.runner, gate_scope="everything")
        with self.assertRaisesRegex(eo.OrchestratorError, "--gate-scope 只接受"):
            bad.orchestrate()

    def test_explicit_client_plan_is_allowed_together_with_reexecute_gates(self) -> None:
        orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "explicit-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner, reexecute_gates=True,
                                       gate_scope="client-plan", select_base=self.BASE)
        self.assertEqual(orchestrator.gate_scope_args, ["--gate-scope", "client-plan", "--select-base", self.BASE],
                         "操作员明确要子集：显式 client-plan 不被 --reexecute-gates 拦下（auto 才让位给它）")

    def test_the_pre_a3_only_call_never_carries_the_scope(self) -> None:
        orchestrator = eo.Orchestrator(dict(self.fx.params), driver_dir=self.fx.driver, run_dir=self.fx.root / "pre-a3-run",
                                       steps_dir=self.fx.steps_dir, runner=self.commands, es_runner=self.fx.runner)
        self.assertEqual(orchestrator.orchestrate(to_step="p0-receipt")["exit_code"], 0)
        orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
        orchestrator.entry_gates_and_pre_a3(False, True)
        gates = self.entry_gate_calls()
        self.assertEqual([_option(call, "--profile") for call in gates], ["entry", "pre-a3"])
        self.assertEqual(_option(gates[0], "--gate-scope"), "auto")
        self.assertNotIn("--gate-scope", gates[1], "pre-A3 组合没有采集工具测试组，不带筛选")

    def test_switching_the_scope_does_not_redo_the_expensive_pre_a3_step(self) -> None:
        first = self.orchestrate(to_step="atomic-double", gate_scope="auto")
        second = self.orchestrate(to_step="atomic-double", gate_scope="full")
        self.assertEqual([run["exit_code"] for run in (first, second)], [0, 0])
        self.assertEqual(self.actions(second)["pre-a3"], "沿用", self.actions(second))

    def test_the_command_line_defaults_to_auto_and_validates_the_base(self) -> None:
        arguments = eo._parse([])
        self.assertEqual((arguments.gate_scope, arguments.select_base), ("auto", None))
        arguments = eo._parse(["--gate-scope", "client-plan", "--select-base", self.BASE])
        self.assertEqual((arguments.gate_scope, arguments.select_base), ("client-plan", self.BASE))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            eo._parse(["--gate-scope", "sometimes"])


class OrchestratorPieceTests(unittest.TestCase):
    def test_strict_audit_options_fail_before_starting_an_invalid_entry(self) -> None:
        cases = [["--audit-strict"], ["--audit-policy", "/missing-policy.json"],
                 ["--reexecute-gates", "--audit-reads", "--audit-strict", "--audit-policy", "relative.json"],
                 ["--reexecute-gates", "--audit-reads", "--audit-strict", "--audit-policy", "/missing-policy.json"]]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(eo.main(argv), 2)
        inherited = {eo.ES.AUDIT_READS_PARAM: "true", eo.ES.AUDIT_STRICT_PARAM: "true", eo.ES.AUDIT_POLICY_PARAM: "/old.json"}
        orchestrator = eo.Orchestrator(inherited, driver_dir=DRIVER, run_dir=DRIVER, steps_dir=DRIVER)
        self.assertTrue(all(key not in orchestrator.params for key in inherited), "环境变量不能隐式切换本次审计规格")

    def test_gate_parallel_requires_a_positive_integer(self) -> None:
        self.assertIsNone(eo._parse([]).gate_parallel)
        self.assertEqual(eo._parse(["--gate-parallel", "2"]).gate_parallel, 2)
        for value in ("0", "-1", "bad", "", "1.5"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                eo._parse(["--gate-parallel", value])
            self.assertEqual(error.exception.code, 2)
        for value in (0, -1, True, "2"):
            with self.subTest(value=value), self.assertRaisesRegex(eo.OrchestratorError, "正整数"):
                eo.Orchestrator({}, driver_dir=DRIVER, run_dir=DRIVER, steps_dir=DRIVER, gate_parallel=value)

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


class RecordedEntryBridgeTests(OrchestratorTestCase):
    """独立九认证的正式入口消费：缺步骤记录仍验真复用，失败不伪造通过，公共门禁保持实时执行。"""

    def make_pair(self, *, client: str = "claude", configured: bool = True, rc: int = 0,
                  reuse_status: str = "not_needed", omit_result: bool = False,
                  invalid_shape: bool = False, inherit: bool = True, reexecute: bool = False,
                  find_rc: int = 0) -> eo.Orchestrator:
        params = {**self.fx.params, "CLIENT": client}
        if configured:
            params.update({"CLAUDE_ENTRY_FULL79_SPEC": str(self.fx.root / "full79.json"),
                           "CLAUDE_ENTRY_FULL79_SPEC_SHA256": "1" * 64})
        self.reuse_receipt = self.fx.root / "original-reuse.json"
        _write_json(self.reuse_receipt, {"receipt": "real-command-result"})
        def inputs_runner(argv, env=None, cwd=None):
            if "recorded-full79-inputs" in argv:
                return 0, json.dumps({"inputs": [{"name": f"file:{index}", "sha256": "a" * 64,
                                                  "category": "environment"} for index in range(55)]})
            return self.fx.runner(argv, env, cwd)
        def command_runner(argv, cwd, env, log):
            if "find-reusable" in argv:
                return find_rc
            if not configured and "record-reuse" in argv:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("" if omit_result else json.dumps({
                    "reused_certification": _option(argv, "--certification"), "status": reuse_status,
                    "reuse_receipt": None if reuse_status == "not_needed" else str(self.reuse_receipt)}
                    if not invalid_shape else []) + "\n", encoding="utf-8")
                return 0
            if argv[0] == "bash" and argv[1].endswith("entry-gates.sh"):
                value = self.commands(argv, cwd, env, log)
                out = Path(_option(argv, "--out"))
                if not omit_result:
                    _write_json(out / "pre-a3-reuse.json", {
                        "reused_certification": _option(argv, "--pre-a3-certification"), "status": reuse_status,
                        "reuse_receipt": None if reuse_status == "not_needed" else str(self.reuse_receipt)}
                        if not invalid_shape else [])
                return rc or value
            return self.commands(argv, cwd, env, log)
        orchestrator = eo.Orchestrator(params, driver_dir=self.fx.driver, run_dir=self.fx.root / "pair-run",
                                       steps_dir=self.fx.steps_dir, runner=command_runner, es_runner=inputs_runner,
                                       unit_inherit_request=self.fx.root / "entry-request.json" if inherit else None,
                                       unit_inherit_full_gates_request=self.fx.root / "full-gates-request.json" if inherit else None,
                                       reexecute_gates=reexecute)
        for step_id in ("entry-gates", "pre-a3"):
            orchestrator.outcomes[step_id] = eo.Outcome(step_id, "run")
        step = orchestrator.ctx.step("policy-activation")
        eo.ES.begin(step, orchestrator.ctx)
        eo.ES.finish(step, orchestrator.ctx, status="passed", products={"activation": params["POLICY_ACTIVATION"]})
        return orchestrator

    def test_existing_independent_certification_uses_present_and_full_gates_request(self) -> None:
        for configured in (True, False):
            with self.subTest(carry_bridge=configured):
                orchestrator = self.make_pair(reuse_status="recorded", configured=configured)
                digest = hashlib.sha256(Path(self.fx.params["PRE_A3_CERTIFICATION"]).read_bytes()).hexdigest()
                with mock.patch.object(orchestrator, "latest_deploy", return_value=self.fx.root / "deploy.json"), \
                        mock.patch.object(orchestrator, "_ordinary_pre_a3_inputs", return_value=digest):
                    orchestrator.entry_gates_and_pre_a3(True, True)
                call = self.commands.calls[-1]
                self.assertEqual(_option(call, "--pre-a3-mode"), "present")
                self.assertEqual(_option(call, "--pre-a3-certification"), self.fx.params["PRE_A3_CERTIFICATION"])
                self.assertEqual(_option(call, "--unit-inherit-request"), str(self.fx.root / "full-gates-request.json"))
                self.assertEqual(orchestrator.outcomes["pre-a3"].status, "passed")
                record = eo.ES.read_record(self.fx.steps_dir, "pre-a3")
                self.assertEqual({item["name"] for item in record["products"]}, {"certification", "reuse-receipt"})
                self.assertEqual(record["origin"], "execution", "登记本次真实消费，不改旧单元的处置或记录")
                self.reuse_receipt.write_text("damaged", encoding="utf-8")
                self.assertTrue(eo.ES._product_problems(orchestrator.ctx.step("pre-a3"), record), "原步骤消费者继续核复用收据内容")
        self._assert_ordinary_current_inputs()

    def _assert_ordinary_current_inputs(self) -> None:
        """原签发器、原记录重验与当前输入展开实跑；合成场景只验证消费合同，不代表九链认证。"""
        from tools.ci import entry_gates as gates
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
        from tools.official_client_capture.tests import test_codex_upgrade_pre_a3_certification as certification_tests
        from tools.official_client_capture.tests import test_pre_a3_client_grouping as grouping_tests

        orchestrator = self.make_pair(configured=False)
        tree = self.fx.root / "entry-gates-work/test-tree"
        tree.mkdir(parents=True)
        modules = {pre_a3.scenario_test_module(name, "claude").replace(".", "/") + ".py" for name in pre_a3.scenario_names("claude")}
        paths = modules | {"tools/official_client_capture/core.py", "tools/official_client_capture/tests/project_ledger_fixture.py",
                           "tools/official_client_control/store.py", "tools/official_client_control/schemas/test.json",
                           "tools/official_client_control/tests/test_contract.py", "docs/guide.md", "tools/arm64_supervised_deploy.py"}
        for relative in paths:
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n" if target.suffix == ".json" else "# 合同输入夹具\n", encoding="utf-8")
        git = ["git", "-C", str(tree), "-c", "user.name=入口合同测试", "-c", "user.email=entry-test@invalid"]
        subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
        def commit() -> None:
            subprocess.run([*git, "add", "."], check=True, capture_output=True)
            subprocess.run([*git, "commit", "-qm", "输入夹具"], check=True, capture_output=True)
            orchestrator.params["ENTRY_COMMIT"] = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        commit()
        old_head = orchestrator.params["ENTRY_COMMIT"]
        subprocess.run([*git, "commit", "--allow-empty", "-qm", "本轮入口提交"], check=True, capture_output=True)
        orchestrator.params["ENTRY_COMMIT"] = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
        current_tree = self.fx.root / "ordinary-current-source-tree"
        shutil.copytree(tree, current_tree)
        for key in orchestrator.spec["pre_a3_fixture_keys"]:
            target = self.fx.root / "ordinary-fixtures" / key / "capture"
            target.mkdir(parents=True)
            (target / "fact.json").write_text("{}\n", encoding="utf-8")
            orchestrator.params[key] = str(target)
        values = {key: orchestrator.params[key] for key in orchestrator.spec["pre_a3_fixture_keys"]}
        fixtures = gates.pre_a3_fixture_resolved("claude", values)
        root = self.fx.root / "ordinary-source"
        root.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_UPGRADE_IDENTITY_MEMO": str(self.fx.root / "identity-memo")}):
            _identity, _compatibility, deployment, activation = grouping_tests._IdentityMemo.claude_bindings(self, root)
            store = certification_tests._SyntheticStore(root)
            registration = {item["id"]: item["test"] for item in pre_a3.real_chain_registration("claude")}
            records = gates._unit_records_module()
            repo, cache, entries = records.RepoIndex.load(tree), {}, []
            for name in pre_a3.scenario_names("claude"):
                result = store.result(name)
                result["scenario"]["test"] = registration[name]
                result = pre_a3._sealed({key: value for key, value in result.items() if key != "result_sha256"}, "result_sha256")
                test_file = pre_a3.scenario_test_module(name, "claude").replace(".", "/") + ".py"
                inputs = records.declared_inputs(repo, gates.pre_a3_inputs(test_file, fixtures, "claude"), cache)
                record = store.record(name, "ordinary-source", unit_id="pre-a3:claude:" + name,
                                      inputs=inputs, inputs_sha256=records.entries_sha256(inputs), log_text=store.log_text(result))
                entries.append(store.entry(record))
            manifest = store.manifest("ordinary-source", entries)
            receipt = pre_a3.issue_certification_from_records(self.fx.data / "staging/source", unit_manifest=manifest,
                record_store=store.store.root, deployment_receipt=deployment, policy_activation=activation, client="claude")
            source = pre_a3.write_certification(root / "pre-a3.json", receipt)
            raw = source.read_bytes()
            # 只替代机器专属的 Docker/总账探测；认证、记录、来源目录和仓库声明均使用原真实函数。
            self.assertEqual(pre_a3.find_reusable_certification(certification=source, deployment_receipt=deployment,
                                                              policy_activation=activation, client="claude"), source)
            with mock.patch.dict(sys.modules, {"entry_orchestrator_gates_sibling": gates}), \
                    mock.patch.object(gates, "pre_a3_data_root_inputs", return_value=[]):
                self.assertEqual(orchestrator._ordinary_pre_a3_inputs(source), hashlib.sha256(raw).hexdigest())
                # 门禁派发用替身，普通认证的两条正式 CLI、当前输入与步骤消费完整连通。
                orchestrator.params["PRE_A3_CERTIFICATION"] = str(source)
                orchestrator.ctx = eo.ES.Context(params=orchestrator.params, driver_dir=self.fx.driver,
                    steps_dir=self.fx.steps_dir, runner=self.fx.runner)
                step = orchestrator.ctx.step("policy-activation")
                eo.ES.begin(step, orchestrator.ctx)
                eo.ES.finish(step, orchestrator.ctx, status="passed", products={"activation": str(activation)})
                for explicit_work in (False, True):
                    for initial_tree in ("empty", "old-head"):
                        with self.subTest(explicit_work=explicit_work, initial_tree=initial_tree):
                            work = self.fx.root / ("explicit-entry-work" if explicit_work else "entry-gates-work")
                            active_tree = work / "test-tree"
                            shutil.rmtree(work, ignore_errors=True)
                            if initial_tree == "old-head":
                                shutil.copytree(current_tree, active_tree)
                                subprocess.run(["git", "-C", str(active_tree), "checkout", "--detach", old_head],
                                               check=True, capture_output=True)
                            orchestrator.full_set_work = work if explicit_work else None
                            orchestrator.params.pop(eo.ES.GATE_WORK_PARAM, None)
                            if explicit_work:
                                orchestrator.params[eo.ES.GATE_WORK_PARAM] = str(work)
                            orchestrator.ctx = eo.ES.Context(params=orchestrator.params, driver_dir=self.fx.driver,
                                steps_dir=self.fx.steps_dir, runner=self.fx.runner)
                            real_calls = []
                            def consume(argv, cwd, env, log):
                                self.assertNotIn("run-scenario", argv)
                                if argv[0] == "bash":
                                    self.assertEqual(_option(argv, "--pre-a3-mode"), "present")
                                    if explicit_work:
                                        self.assertEqual(_option(argv, "--work"), str(work))
                                    else:
                                        self.assertNotIn("--work", argv)
                                    if initial_tree == "empty":
                                        self.assertFalse(active_tree.exists(), "源认证可先验真，当前树由原门禁脚本准备")
                                    else:
                                        self.assertEqual(subprocess.check_output(["git", "-C", str(active_tree),
                                            "rev-parse", "HEAD"], text=True).strip(), old_head)
                                        shutil.rmtree(active_tree)
                                    # 模拟原 entry-gates.sh 的 clone_test_tree，当前输入在它返回后才复核。
                                    shutil.copytree(current_tree, active_tree)
                                    return self.commands(argv, cwd, env, log)
                                self.assertIn(argv[4], ("find-reusable", "record-reuse"))
                                real_calls.append(argv[4])
                                completed = subprocess.run(argv, cwd=REPO_ROOT, env={**env, "PYTHONPATH": str(REPO_ROOT)},
                                                           capture_output=True, text=True, timeout=60)
                                log.parent.mkdir(parents=True, exist_ok=True)
                                log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
                                return completed.returncode
                            orchestrator.runner = consume
                            with mock.patch.object(orchestrator, "latest_deploy", return_value=deployment):
                                orchestrator.entry_gates_and_pre_a3(True, True)
                            self.assertEqual(real_calls, ["find-reusable", "record-reuse"])
                            self.assertEqual(orchestrator.outcomes["pre-a3"].status, "passed", orchestrator.outcomes["pre-a3"].reasons)
                            self.assertEqual(source.read_bytes(), raw)
                orchestrator.full_set_work = None
                orchestrator.params.pop(eo.ES.GATE_WORK_PARAM, None)
                for relative in ("docs/guide.md", "tools/official_client_control/store.py", "tools/official_client_control/schemas/test.json"):
                    with self.subTest(current_input=relative):
                        target = tree / relative
                        original = target.read_bytes()
                        target.write_bytes(original + b"\n")
                        commit()
                        with self.assertRaisesRegex(eo.OrchestratorError, "当前输入已变化"):
                            orchestrator._ordinary_pre_a3_inputs(source)
                        target.write_bytes(original)
                        commit()
                target = Path(next(iter(values.values()))) / "fact.json"
                target.write_text('{"changed":true}\n', encoding="utf-8")
                with self.assertRaisesRegex(eo.OrchestratorError, "当前输入已变化"):
                    orchestrator._ordinary_pre_a3_inputs(source)
            self.assertEqual(source.read_bytes(), raw)

    def test_not_needed_is_legal_and_next_entry_still_verifies_original_source(self) -> None:
        for configured in (True, False):
            orchestrator = self.make_pair(configured=configured)
            digest = hashlib.sha256(Path(self.fx.params["PRE_A3_CERTIFICATION"]).read_bytes()).hexdigest()
            with mock.patch.object(orchestrator, "latest_deploy", return_value=self.fx.root / "deploy.json"), \
                    mock.patch.object(orchestrator, "_ordinary_pre_a3_inputs", return_value=digest):
                orchestrator.entry_gates_and_pre_a3(True, True)
                self.assertEqual(orchestrator.outcomes["pre-a3"].status, "passed")
                self.assertNotIn("reuse-receipt", orchestrator.outcomes["pre-a3"].products)
                orchestrator.entry_gates_and_pre_a3(True, False)
        self.assertEqual(len(self.commands.calls), 4, "两种来源的公共门禁仍实时执行两次")
        self.assertTrue(all(_option(call, "--pre-a3-mode") == "present" for call in self.commands.calls))
        self.assertEqual(orchestrator.outcomes["pre-a3"].status, "passed")

    def test_failed_source_or_record_reuse_never_marks_existing_certification_passed(self) -> None:
        for configured, rc, omit, status, invalid_shape in (
                (configured, *case) for configured in (True, False) for case in (
                    (3, False, "not_needed", False), (0, True, "not_needed", False),
                    (0, False, "failed", False), (0, False, "not_needed", True))):
            with self.subTest(carry_bridge=configured, rc=rc, missing=omit, status=status, invalid_shape=invalid_shape):
                orchestrator = self.make_pair(configured=configured, rc=rc, omit_result=omit, reuse_status=status, invalid_shape=invalid_shape)
                out = orchestrator.run_dir / "entry-gates/pre-a3-reuse.json"
                out.unlink(missing_ok=True)
                digest = hashlib.sha256(Path(self.fx.params["PRE_A3_CERTIFICATION"]).read_bytes()).hexdigest()
                with mock.patch.object(orchestrator, "latest_deploy", return_value=self.fx.root / "deploy.json"), \
                        mock.patch.object(orchestrator, "_ordinary_pre_a3_inputs", return_value=digest):
                    orchestrator.entry_gates_and_pre_a3(True, True)
                self.assertTrue(Path(self.fx.params["PRE_A3_CERTIFICATION"]).is_file())
                self.assertEqual(orchestrator.outcomes["pre-a3"].status, "failed")
                self.assertEqual(eo.ES.read_record(self.fx.steps_dir, "pre-a3")["status"], "failed")
        orchestrator = self.make_pair(configured=False, find_rc=1)
        before = len(self.commands.calls)
        with mock.patch.object(orchestrator, "latest_deploy", return_value=self.fx.root / "deploy.json"):
            orchestrator.entry_gates_and_pre_a3(True, True)
        self.assertEqual(len(self.commands.calls), before, "坏源必须在派发前拒绝，不降级另跑九链")
        self.assertEqual(orchestrator.outcomes["pre-a3"].status, "failed")

    def test_unconfigured_claude_and_codex_keep_fresh_run_path(self) -> None:
        for client, inherit, reexecute in (("claude", False, False), ("claude", False, True), ("codex", True, False)):
            with self.subTest(client=client, inherit=inherit, reexecute=reexecute):
                orchestrator = self.make_pair(client=client, configured=False, inherit=inherit, reexecute=reexecute)
                with mock.patch.object(orchestrator, "latest_deploy", return_value=self.fx.root / "deploy.json"):
                    orchestrator.entry_gates_and_pre_a3(True, True)
                call = self.commands.calls[-1]
                self.assertEqual(_option(call, "--pre-a3-mode"), "run")
                self.assertNotEqual(_option(call, "--pre-a3-certification"), self.fx.params["PRE_A3_CERTIFICATION"])
                if inherit:
                    self.assertEqual(_option(call, "--unit-inherit-request"), str(self.fx.root / "entry-request.json"))
                else:
                    self.assertNotIn("--unit-inherit-request", call)


if __name__ == "__main__":
    unittest.main()
