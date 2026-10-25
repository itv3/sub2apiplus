"""后台验证的"按客户端筛选"与防串用（Codex 升级工具优化第 9 项，S4，``tools/ci/background_validation.py``）。

Codex 与 Claude 共用同一份后台结论（两客户端都纳管时一次全量门禁、结论绑定两份部署收据）。Codex 计划的结论是子集，没跑 Claude 的重单元，
所以：

* start：``--gate-scope client-plan --select-base <提交>`` 进输入合同与结论，原样交给入口门禁；不接入口空跑、不沿用来源、不签 B-09 全集收据；
  缺省时输入合同、结论、命令行与改造前逐字相同；
* 防串用：结论登记的请求、登记的筛选摘要、入口门禁总摘要的回显三处互相印证；Codex 计划的结论给 Claude 的准入（check／VC-5 统一准入）一律判不通过，
  给 Codex 自己的准入通过并标明是子集；被改写的登记、没生效的筛选都不放行；
* 全集运行的结论不受影响。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.ci import background_validation as bv

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools" / "ci" / "background_validation.py"
COMMIT = "a" * 40
BASE = "b" * 40
SELECTION = {"gate_scope": "client-plan", "client": "codex", "base_commit": BASE, "head_commit": COMMIT, "selection_sha256": "s" * 64,
             "ownership_sha256": "o" * 64, "include_claude": False, "modules_total": 10, "modules_skipped": 4, "modules_pulled": 1, "advisories": 2}

# 入口门禁替身：记下收到的筛选参数；按 FAKE_SELECTION_* 环境变量决定总摘要里回显什么筛选摘要（不设就不回显）。
FAKE_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""; ARGS=(); FLAG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --gate-scope|--select-base|--client) echo "$1:$2" >> "$FAKE_GATES_CALLS"; shift 2 ;;
    --include-claude) echo "include-claude" >> "$FAKE_GATES_CALLS"; shift ;;
    --profile|--mode|--record-store|--work|--wait-lock|--parallel) shift 2 ;;
    --require-deployed|--require-deployed-all) FLAG="$1"; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
echo "$FLAG ${ARGS[2]}" >> "$FAKE_GATES_CALLS"
mkdir -p "$OUT"
python3 - "$OUT" "$FAKE_GATES_RECEIPT" "${FAKE_GATES_OTHERS-__unset__}" "$FLAG" <<'PY'
import json, os, sys
out, receipt, others, flag = sys.argv[1:5]
source = {"deploy_receipt": receipt}
if flag == "--require-deployed-all" and others != "__unset__":
    source["deploy_receipts_others"] = others
entry = {"status": "passed", "gates": [{"gate_id": "test-capture-tools", "status": "passed"}], "source": source}
if os.environ.get("FAKE_SELECTION_JSON"):
    entry["client_selection"] = json.loads(os.environ["FAKE_SELECTION_JSON"])
json.dump(entry, open(out + "/entry-gates.json", "w"))
PY
"""


class ScopeCase(unittest.TestCase):
    """只有 Codex 纳管（单客户端）的运行根；两客户端的情形在 TwoClientScopeTests。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        self.runroot = self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text(FAKE_GATES, encoding="utf-8")
        (self.root / "x.bundle").write_bytes(b"scope-bundle")
        self.calls = self.root / "calls.log"
        self.receipt = self.deploy("20261025t000000z")
        self.addCleanup(lambda: bv.stop(self.runroot, "测试收尾"))

    def deploy(self, stamp: str) -> Path:
        path = self.data / "control" / f"codex-01600-supervisor-enable-{stamp}.json"
        path.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64, "policy_version": 7}), encoding="utf-8")
        return path

    def environ(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FAKE_GATES_CALLS": str(self.calls), "FAKE_GATES_RECEIPT": str(self.receipt), **extra}

    def cli(self, *argv: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120, env=self.environ(**extra))

    def start(self, *options: str, expect: int = 0, **extra: str) -> dict:
        completed = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                             "--branch", "codex/test", "--commit", COMMIT, "--entry-gates", str(self.gates), "--preflight", "off", "--reuse", "off", *options, **extra)
        self.assertEqual(completed.returncode, expect, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1]) if expect == 0 else {"stderr": completed.stderr}

    def wait(self, result: str) -> dict:
        deadline = time.monotonic() + 60
        while True:
            payload = bv._read(Path(result)) or {}
            if payload.get("status") in ("passed", "failed", "aborted", "superseded"):
                return payload
            self.assertLess(time.monotonic(), deadline, f"后台验证没有在时限内结束：{payload}")
            time.sleep(0.05)

    def boundary(self, name: str = "require-passed", client: str = "codex") -> tuple[int, str]:
        completed = self.cli(name, "--runroot", str(self.runroot), "--data-root", str(self.data), "--client", client)
        return completed.returncode, completed.stdout + completed.stderr


class ClientPlanStartTests(ScopeCase):
    def test_the_default_start_is_exactly_as_before(self) -> None:
        started = self.start(FAKE_SELECTION_JSON="")
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        for key in ("gate_scope", "select_base", "select_client", "include_claude", "selection"):
            self.assertNotIn(key, payload)
            self.assertNotIn(key, payload["input_contract"])
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["--require-deployed", COMMIT], "命令行里没有任何筛选参数")
        self.assertIsNone(bv.scope_problem(payload, "codex"))
        self.assertIsNone(bv.scope_problem(payload, "claude"), "全集结论对任何客户端都不受影响")

    def test_a_client_plan_start_binds_the_base_and_hands_the_options_to_the_gate(self) -> None:
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE, FAKE_SELECTION_JSON=json.dumps(SELECTION))
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual((payload["gate_scope"], payload["select_base"], payload["select_client"]), ("client-plan", BASE, "codex"))
        self.assertEqual(payload["input_contract"]["gate_scope"], "client-plan")
        self.assertEqual(payload["input_contract"]["select_base"], BASE)
        self.assertEqual(payload["selection"], SELECTION, "入口门禁总摘要回显的筛选摘要登记进结论")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(),
                         ["--gate-scope:client-plan", f"--select-base:{BASE}", "--require-deployed", COMMIT])
        self.assertNotIn("reuse_binding", payload, "子集结论不带沿用绑定，之后不能被沿用")
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("按客户端筛选（codex 计划的子集，不是全集）", message)

    def test_the_manual_switch_reaches_the_gate_and_the_contract(self) -> None:
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE, "--include-claude",
                             FAKE_SELECTION_JSON=json.dumps({**SELECTION, "include_claude": True}))
        payload = self.wait(started["result"])
        self.assertTrue(payload["input_contract"]["include_claude"] and payload["include_claude"])
        self.assertIn("include-claude", self.calls.read_text(encoding="utf-8").split())

    def test_the_options_are_validated_and_exclusive_with_full_set_features(self) -> None:
        for options, message in (
                (("--gate-scope", "client-plan"), "必须同时给 --select-base"),
                (("--gate-scope", "client-plan", "--select-base", "HEAD~1"), "必须同时给 --select-base"),
                (("--select-base", BASE), "只在 --gate-scope client-plan 下有意义"),
                (("--include-claude",), "只在 --gate-scope client-plan 下有意义"),
                (("--gate-scope", "client-plan", "--select-base", BASE, "--then-dryrun"), "不能接入口空跑"),
                (("--gate-scope", "client-plan", "--select-base", BASE, "--rerun-full-set"), "不能接入口空跑"),
                (("--gate-scope", "client-plan", "--select-base", BASE, "--audit-policy", str(self.gates)), "不能接入口空跑"),
                (("--gate-scope", "client-plan", "--select-base", BASE, "--reuse", "only"), "不沿用来源结论")):
            with self.subTest(options=options):
                outcome = self.start(*options, expect=2)
                self.assertIn(message, outcome["stderr"])
        self.assertEqual(bv.results(self.runroot), [], "被拒绝的启动不留结论")

    def test_a_plan_run_and_a_full_run_for_the_same_commit_do_not_dedupe_each_other(self) -> None:
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE, FAKE_SELECTION_JSON=json.dumps(SELECTION))
        self.wait(started["result"])
        outcome = self.start(expect=2)
        self.assertIn("输入或缓存隔离合同不同", outcome["stderr"], "同一提交同一部署已有子集结论：要全集须先显式停止并重新验证")

    def test_auto_reuse_is_skipped_for_a_client_plan_start(self) -> None:
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE, "--reuse", "auto", FAKE_SELECTION_JSON=json.dumps(SELECTION))
        self.assertEqual(started["action"], "started")
        self.assertNotIn("reuse_refused", started)
        self.wait(started["result"])


class ClientPlanRunTests(ScopeCase):
    def test_a_plan_whose_selection_did_not_take_effect_is_not_a_passed_conclusion(self) -> None:
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE)   # 替身不回显筛选摘要
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "failed", payload)
        self.assertIn("筛选没有生效", payload["reason"])

    def test_a_plan_that_fell_back_to_the_full_set_is_a_full_conclusion(self) -> None:
        # 归属表自己在改动里：选择器整轮回退成全集，总摘要回显 scope=full——结论是全集结论，对任何客户端都成立。
        echoed = {**SELECTION, "gate_scope": "full"}
        started = self.start("--gate-scope", "client-plan", "--select-base", BASE, FAKE_SELECTION_JSON=json.dumps(echoed))
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertIsNone(bv.scope_problem(payload, "claude"))

    def test_the_conclusion_is_refused_for_the_issue_of_a_full_set_receipt(self) -> None:
        refused = bv._issue_full_set({"gate_scope": "client-plan", "out": str(self.root)})
        self.assertEqual((refused["status"], refused["reuse_enabled"]), ("refused", False))
        self.assertIn("子集", refused["reason"])
        refused = bv._issue_full_set({"selection": {"gate_scope": "client-plan"}, "out": str(self.root)})
        self.assertEqual(refused["status"], "refused")


class ScopeProblemTests(unittest.TestCase):
    """三处互相印证：登记的请求、登记的筛选摘要、入口门禁总摘要的回显。"""

    def payload(self, root: Path, *, requested: object = "client-plan", recorded: object = SELECTION, echoed: object = SELECTION) -> dict:
        out = root / "out"
        out.mkdir(exist_ok=True)
        entry = {"status": "passed", "gates": []}
        if echoed is not None:
            entry["client_selection"] = echoed
        (out / "entry-gates.json").write_text(json.dumps(entry), encoding="utf-8")
        payload: dict = {"out": str(out), "status": "passed"}
        if requested is not None:
            payload["gate_scope"] = requested
        if recorded is not None:
            payload["selection"] = recorded
        return payload

    def problem(self, client: str = "codex", **kwargs: object) -> str | None:
        with tempfile.TemporaryDirectory() as directory:
            return bv.scope_problem(self.payload(Path(directory), **kwargs), client)

    def test_a_codex_plan_serves_codex_and_never_claude(self) -> None:
        self.assertIsNone(self.problem("codex"))
        message = self.problem("claude")
        self.assertIn("codex 计划的子集结论", message)
        self.assertIn("不能当作 claude 的全集通过", message)
        self.assertIn("防串用", message)

    def test_a_plan_conclusion_that_hides_its_label_or_its_summary_is_caught(self) -> None:
        self.assertIn("结论里却没有登记筛选", self.problem(requested=None) or "", "只删了请求标签")
        self.assertIn("不一致", self.problem(requested=None, recorded=None) or "", "标签与登记的摘要都删了，回显还在")
        self.assertIn("不一致", self.problem(recorded=None) or "", "登记的筛选摘要被删掉，回显还在")
        self.assertIn("不一致", self.problem(echoed=None) or "", "总摘要的回显被删掉（或没按登记的范围运行）")
        self.assertIn("筛选没有生效", self.problem(recorded=None, echoed=None) or "")
        self.assertIn("不是同一份", self.problem(recorded={**SELECTION, "selection_sha256": "x" * 64}) or "")
        self.assertIn("不是同一份", self.problem(recorded={**SELECTION, "client": "claude"}) or "")

    def test_a_forged_client_in_both_places_still_fails_for_the_other_client(self) -> None:
        forged = {**SELECTION, "client": "claude"}
        self.assertIsNone(self.problem("claude", recorded=forged, echoed=forged))
        self.assertIn("claude 计划的子集结论", self.problem("codex", recorded=forged, echoed=forged) or "")

    def test_a_full_set_conclusion_has_no_problem(self) -> None:
        self.assertIsNone(self.problem("codex", requested=None, recorded=None, echoed=None))
        self.assertIsNone(self.problem("claude", requested=None, recorded=None, echoed=None))
        self.assertIsNone(self.problem("claude", requested="client-plan", recorded={**SELECTION, "gate_scope": "full"},
                                       echoed={**SELECTION, "gate_scope": "full"}))

    def test_a_payload_without_an_output_directory_is_a_full_conclusion_as_before(self) -> None:
        self.assertIsNone(bv.scope_problem({"status": "passed"}, "claude"))
        self.assertIsNone(bv.scope_problem({"status": "passed", "out": "/nonexistent/out"}, "claude"))


class TwoClientScopeTests(unittest.TestCase):
    """两客户端都纳管：一份结论绑定两份部署收据；Codex 的子集结论，Claude 的批次边界与 VC-5 前核对必须拒绝。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        (self.data / "control").mkdir(parents=True)
        self.codex_run, self.claude_run = self.root / "codex-run", self.root / "claude-run"
        for runroot in (self.codex_run, self.claude_run):
            runroot.mkdir()
        stamp = "20261025t000000z"
        self.codex = self.data / "control" / f"codex-01600-supervisor-enable-{stamp}.json"
        self.codex.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64, "policy_version": 7}), encoding="utf-8")
        self.claude = self.data / "control" / f"claude-managed-deploy-{stamp}.json"
        self.claude.write_text(json.dumps({"status": "passed", "client": "claude",
                                           "identity": {"tool_files_sha256": "f" * 64, "policy_version": 2}}), encoding="utf-8")

    def write_conclusion(self, *, echoed: object, recorded: object, requested: object = "client-plan") -> Path:
        deployments = bv.deployment_set(self.data)
        out = self.root / "out"
        out.mkdir(exist_ok=True)
        entry = {"status": "passed", "gates": []}
        if echoed is not None:
            entry["client_selection"] = echoed
        (out / "entry-gates.json").write_text(json.dumps(entry), encoding="utf-8")
        payload = {"schema_version": bv.SCHEMA, "commit": COMMIT, "branch": "x", "bundle": "/b", "deployment": deployments["codex"],
                   "deployments": deployments, "profile": "full-gates", "mode": "re-execute", "status": "passed", "pid": None,
                   "started_at_utc": "2026-10-25T00:00:00Z", "completed_at_utc": "2026-10-25T01:00:00Z", "out": str(out),
                   "log": str(out) + ".log", "input_contract": {}, "failed_gates": [], "reason": None, "returncode": 0}
        if requested is not None:
            payload["gate_scope"] = requested
        if recorded is not None:
            payload["selection"] = recorded
        path = bv.result_path(self.codex_run, COMMIT, deployments["codex"], deployments)
        bv._write(path, payload)
        return path

    def test_a_codex_plan_conclusion_is_not_a_claude_pass(self) -> None:
        self.write_conclusion(echoed=SELECTION, recorded=SELECTION)
        code, message = bv.check(self.codex_run, self.data, require_passed=True, client="codex")
        self.assertEqual(code, 0, message)
        self.assertIn("按客户端筛选（codex 计划的子集，不是全集）", message)
        code, message = bv.check(self.codex_run, self.data, require_passed=True, client="claude")
        self.assertEqual(code, 3, "Claude 的 VC-5 前核对不能把 Codex 计划的子集当成全集通过")
        self.assertIn("防串用", message)
        code, _message = bv.check(self.codex_run, self.data, require_passed=False, client="claude")
        self.assertEqual(code, 3, "批次边界同样拒绝（passed 但范围不满足）")

    def test_a_full_conclusion_serves_both_clients(self) -> None:
        self.write_conclusion(echoed=None, recorded=None, requested=None)
        for client in ("codex", "claude"):
            code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
            self.assertEqual(code, 0, message)
            self.assertNotIn("按客户端筛选", message)

    def test_removing_the_label_does_not_turn_the_subset_into_a_full_pass(self) -> None:
        for recorded, text in ((SELECTION, "没有登记筛选"), (None, "不一致")):
            self.write_conclusion(echoed=SELECTION, recorded=recorded, requested=None)
            for client in ("codex", "claude"):
                with self.subTest(recorded=recorded is not None, client=client):
                    code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
                    self.assertEqual(code, 3, message)
                    self.assertIn(text, message)


if __name__ == "__main__":
    unittest.main()
