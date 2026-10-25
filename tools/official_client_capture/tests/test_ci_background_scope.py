"""后台验证的"按客户端筛选"与防串用（Codex 升级工具优化第 9 项，S4，``tools/ci/background_validation.py``）。

Codex 与 Claude 共用同一份后台结论（两客户端都纳管时一次全量门禁、结论绑定两份部署收据）。Codex 计划的结论是子集，没跑 Claude 的重单元，
所以：

* start：``--gate-scope client-plan --select-base <提交>`` 进输入合同与结论，原样交给入口门禁；不接入口空跑、不沿用来源、不签 B-09 全集收据；
  缺省时输入合同、结论、命令行与改造前逐字相同；
* 防串用：结论登记的请求、登记的筛选摘要、入口门禁总摘要的回显三处互相印证；Codex 计划的结论给 Claude 的准入（check／VC-5 统一准入）一律判不通过，
  给 Codex 自己的准入通过并标明是子集；被改写的登记、没生效的筛选都不放行；
* 全集运行的结论不受影响；
* ``--gate-scope auto``（驱动入口对 Codex 缺省带）：由入口门禁决定子集还是回退全集，结论登记请求、实际范围（``gate_scope_resolved``）与筛选摘要，
  与入口门禁总摘要的回显互相印证；实际是子集的只给 Codex 准入、不接入口空跑、不作沿用／承接来源，实际回退全集的是全集结论、两个客户端都认；
  Claude 与 B-09／逐单元承接在起跑时就按全集处理。
"""

from __future__ import annotations

import json
import os
import re
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

# auto：入口门禁决定的实际范围（entry_gates.gate_scope_info）。子集：基点取最近的发版 tag；回退：选择器抛错等，没有基点之后的字段。
INFO_SUBSET = {"requested": "auto", "applied": "client-plan", "client": "codex", "base_commit": BASE, "base_source": "latest-release-tag",
               "base_tag": "v0.2.17-10", "unclassified": [], "stale_modules": []}
INFO_FALLBACK = {"requested": "auto", "applied": "full", "client": "codex", "fallback_reason": "ValueError：找不到发版 tag"}
INFO_TABLE_CHANGED = {**INFO_SUBSET, "applied": "full", "fallback_reason": "归属表自己在改动里（整轮回退全集）"}

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
if os.environ.get("FAKE_SCOPE_INFO_JSON"):
    entry["gate_scope"] = json.loads(os.environ["FAKE_SCOPE_INFO_JSON"])   # auto：入口门禁决定的实际范围（gate_scope_info）
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
                (("--select-base", BASE), "--select-base、--include-claude 只在"),
                (("--include-claude",), "--select-base、--include-claude 只在"),
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
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("整轮回退成了全集", message)
        self.assertNotIn("【子集", message)

    def test_the_conclusion_is_refused_for_the_issue_of_a_full_set_receipt(self) -> None:
        refused = bv._issue_full_set({"gate_scope": "client-plan", "out": str(self.root)})
        self.assertEqual((refused["status"], refused["reuse_enabled"]), ("refused", False))
        self.assertIn("子集", refused["reason"])
        refused = bv._issue_full_set({"selection": {"gate_scope": "client-plan"}, "out": str(self.root)})
        self.assertEqual(refused["status"], "refused")


class ScopeProblemTests(unittest.TestCase):
    """三处互相印证：登记的请求、登记的筛选摘要、入口门禁总摘要的回显。"""

    def payload(self, root: Path, *, requested: object = "client-plan", recorded: object = SELECTION, echoed: object = SELECTION,
                extra: dict | None = None, info: object = None) -> dict:
        out = root / "out"
        out.mkdir(exist_ok=True)
        entry = {"status": "passed", "gates": []}
        if echoed is not None:
            entry["client_selection"] = echoed
        if info is not None:
            entry["gate_scope"] = info
        (out / "entry-gates.json").write_text(json.dumps(entry), encoding="utf-8")
        payload: dict = {"out": str(out), "status": "passed"}
        if requested is not None:
            payload["gate_scope"] = requested
        if recorded is not None:
            payload["selection"] = recorded
        payload.update(extra or {})
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

    def test_the_registered_base_and_client_must_match_the_echo(self) -> None:
        self.assertIsNone(self.problem(extra={"select_base": BASE, "select_client": "codex"}))
        self.assertIn("基点", self.problem(extra={"select_base": "c" * 40}) or "", "显式登记的基点与回显的基点不是同一个")
        self.assertIn("客户端", self.problem(extra={"select_client": "claude"}) or "", "登记的筛选客户端与回显的不是同一个")

    def test_subset_labels_state_the_scope_and_the_number_of_skipped_modules(self) -> None:
        label = bv.scope_label({"selection": SELECTION})
        self.assertEqual(label, f"codex 计划，跳过 4/10 个测试模块，基点 {BASE[:12]}")
        self.assertIsNone(bv.scope_label({"selection": {**SELECTION, "gate_scope": "full"}}), "整轮回退全集的不是子集")
        self.assertIsNone(bv.scope_label({"gate_scope": "auto"}), "auto 没有登记筛选摘要＝回退全集")
        self.assertIn("跳过 ?/?", bv.scope_label({"gate_scope": "client-plan", "select_base": BASE}) or "", "没有登记计数时写 ?，不悄悄当 0")
        self.assertEqual(bv.scope_note({}), "", "缺省全集没有任何额外提示")

    def test_a_payload_without_an_output_directory_is_a_full_conclusion_as_before(self) -> None:
        self.assertIsNone(bv.scope_problem({"status": "passed"}, "claude"))
        self.assertIsNone(bv.scope_problem({"status": "passed", "out": "/nonexistent/out"}, "claude"))


class TwoClientWorld(unittest.TestCase):
    """两客户端都纳管的夹具：一份结论绑定两份部署收据（本身没有用例，用例在子类）。"""

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

    def write_conclusion(self, *, echoed: object, recorded: object, requested: object = "client-plan", info: object = None,
                         resolved: object = None) -> Path:
        deployments = bv.deployment_set(self.data)
        out = self.root / "out"
        out.mkdir(exist_ok=True)
        entry = {"status": "passed", "gates": []}
        if echoed is not None:
            entry["client_selection"] = echoed
        if info is not None:
            entry["gate_scope"] = info
        (out / "entry-gates.json").write_text(json.dumps(entry), encoding="utf-8")
        payload = {"schema_version": bv.SCHEMA, "commit": COMMIT, "branch": "x", "bundle": "/b", "deployment": deployments["codex"],
                   "deployments": deployments, "profile": "full-gates", "mode": "re-execute", "status": "passed", "pid": None,
                   "started_at_utc": "2026-10-25T00:00:00Z", "completed_at_utc": "2026-10-25T01:00:00Z", "out": str(out),
                   "log": str(out) + ".log", "input_contract": {}, "failed_gates": [], "reason": None, "returncode": 0}
        if requested is not None:
            payload["gate_scope"] = requested
        if recorded is not None:
            payload["selection"] = recorded
        if resolved is not None:
            payload["gate_scope_resolved"] = resolved
        path = bv.result_path(self.codex_run, COMMIT, deployments["codex"], deployments)
        bv._write(path, payload)
        return path



class TwoClientScopeTests(TwoClientWorld):
    """Codex 的子集结论，Claude 的批次边界与 VC-5 前核对必须拒绝。"""

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


class TwoClientAutoTests(TwoClientWorld):
    """auto 的准入语义：入口门禁实际决定子集的，只有 Codex 的准入认；实际回退全集的，两个客户端都认；任何伪造两个客户端都不放行。"""

    def conclusion(self, *, info: object, echoed: object, recorded: object, requested: object = "auto", resolved: object = "same") -> None:
        self.write_conclusion(requested=requested, echoed=echoed, recorded=recorded, info=info,
                              resolved=bv._scope_info(info) if resolved == "same" else resolved)

    def test_an_auto_subset_is_a_codex_pass_that_says_so_and_never_a_claude_pass(self) -> None:
        self.conclusion(info=INFO_SUBSET, echoed=SELECTION, recorded=SELECTION)
        code, message = bv.check(self.codex_run, self.data, require_passed=True, client="codex")
        self.assertEqual(code, 0, message)
        self.assertIn("按客户端筛选（codex 计划的子集，不是全集）", message)
        self.assertIn(f"【子集：codex 计划，跳过 4/10 个测试模块，基点 {BASE[:12]}，筛选摘要 {'s' * 12}】", message, "范围与跳过的模块数一眼可见")
        # 统一准入 vc5_admission.state 靠这个固定写法从提示里取范围说明（它自己不读结论文件）：两端的约定在这里钉住，任何一端改了写法都会红。
        taken = re.search(r"【子集：([^】]*)】", message)
        self.assertEqual(taken.group(1), f"codex 计划，跳过 4/10 个测试模块，基点 {BASE[:12]}，筛选摘要 {'s' * 12}")
        for require in (True, False):
            code, message = bv.check(self.codex_run, self.data, require_passed=require, client="claude")
            self.assertEqual(code, 3, f"Claude 的 {'VC-5 前核对' if require else '批次边界'}不能把 Codex 计划的子集当成全集")
            self.assertIn("防串用", message)

    def test_an_auto_that_fell_back_to_the_full_set_serves_both_clients_and_says_why(self) -> None:
        self.conclusion(info=INFO_FALLBACK, echoed=None, recorded=None)
        for client in ("codex", "claude"):
            code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
            self.assertEqual(code, 0, message)
            self.assertIn("门禁范围 auto：全集（回退原因：ValueError：找不到发版 tag）", message)
            self.assertNotIn("【子集", message)

    def test_a_change_to_the_ownership_table_itself_falls_back_to_the_full_set_for_both_clients(self) -> None:
        echoed = {**SELECTION, "gate_scope": "full"}   # 选择器整轮回退成全集：回显 scope=full，仍带 selection
        self.conclusion(info=INFO_TABLE_CHANGED, echoed=echoed, recorded=echoed)
        for client in ("codex", "claude"):
            code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
            self.assertEqual(code, 0, message)
            self.assertIn("归属表自己在改动里", message)
            self.assertNotIn("【子集", message)

    def test_a_forged_decision_is_refused_for_both_clients(self) -> None:
        subset_resolved = bv._scope_info(INFO_SUBSET)
        fallback_resolved = bv._scope_info(INFO_FALLBACK)
        cases = (
            ("藏起子集：登记成回退全集，但入口门禁总摘要的回显还是子集",
             dict(info=INFO_SUBSET, echoed=SELECTION, recorded=None, resolved=fallback_resolved), "不一致"),
            ("登记的实际范围被改成全集，筛选摘要还在", dict(info=INFO_SUBSET, echoed=SELECTION, recorded=SELECTION, resolved=fallback_resolved), "不一致"),
            ("没有登记实际范围", dict(info=INFO_SUBSET, echoed=SELECTION, recorded=SELECTION, resolved=None), "不一致"),
            ("入口门禁总摘要没有 auto 的实际范围回显", dict(info=None, echoed=SELECTION, recorded=SELECTION, resolved=subset_resolved), "没有 auto 的实际范围回显"),
            ("实际范围说子集却没有筛选回显", dict(info=INFO_SUBSET, echoed=None, recorded=None, resolved=subset_resolved), "对不上"),
            ("实际范围说全集却带着子集的筛选回显", dict(info=INFO_FALLBACK, echoed=SELECTION, recorded=SELECTION, resolved=fallback_resolved), "对不上"),
            ("筛选摘要登记与回显不是同一份",
             dict(info=INFO_SUBSET, echoed=SELECTION, recorded={**SELECTION, "selection_sha256": "x" * 64}, resolved=subset_resolved), "不是同一份"),
            ("实际范围的基点与回显的基点不是同一个",
             dict(info={**INFO_SUBSET, "base_commit": "c" * 40}, echoed=SELECTION, recorded=SELECTION), "基点"),
        )
        for label, kwargs, expected in cases:
            self.conclusion(**kwargs)
            for client in ("codex", "claude"):
                with self.subTest(label=label, client=client):
                    code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
                    self.assertEqual(code, 3, message)
                    self.assertIn(expected, message)

    def test_removing_the_auto_label_does_not_turn_the_subset_into_a_full_pass(self) -> None:
        self.conclusion(info=INFO_SUBSET, echoed=SELECTION, recorded=SELECTION, requested=None, resolved=None)
        for client in ("codex", "claude"):
            code, message = bv.check(self.codex_run, self.data, require_passed=True, client=client)
            self.assertEqual(code, 3, message)
            self.assertIn("没有登记筛选", message)


class AutoScopeStartTests(ScopeCase):
    """``--gate-scope auto`` 的起跑与运行：由入口门禁决定范围，结论登记请求、实际范围与筛选摘要。"""

    def settle(self, result: str, key: str) -> dict:
        """等结论里的 ``key`` 落盘（通过之后接着空跑那一步是结论 passed 之后才写的）。"""
        deadline = time.monotonic() + 60
        while True:
            payload = bv._read(Path(result)) or {}
            if payload.get(key) is not None:
                return payload
            self.assertLess(time.monotonic(), deadline, f"结论里的 {key} 没有在时限内落盘：{payload}")
            time.sleep(0.05)

    def test_auto_hands_the_request_to_the_gate_without_a_base_and_registers_the_decision(self) -> None:
        started = self.start("--gate-scope", "auto", FAKE_SELECTION_JSON=json.dumps(SELECTION), FAKE_SCOPE_INFO_JSON=json.dumps(INFO_SUBSET))
        self.assertEqual(started["gate_scope"]["requested"], "auto")
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual((payload["gate_scope"], payload["select_client"]), ("auto", "codex"))
        self.assertNotIn("select_base", payload, "没给基点：基点由入口门禁取最近的发版 tag，不在起跑时猜")
        self.assertNotIn("select_base", payload["input_contract"])
        self.assertEqual(payload["input_contract"]["gate_scope"], "auto")
        self.assertEqual(payload["selection"], SELECTION)
        self.assertEqual(payload["gate_scope_resolved"], bv._scope_info(INFO_SUBSET), "入口门禁实际决定的范围（含基点来源）登记进结论")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["--gate-scope:auto", "--require-deployed", COMMIT])
        self.assertNotIn("reuse_binding", payload, "子集结论不带沿用绑定")
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("【子集：codex 计划，跳过 4/10 个测试模块", message)
        log = Path(payload["log"]).read_text(encoding="utf-8")
        self.assertIn("后台验证门禁范围：按客户端筛选（codex 计划的子集，不是全集）", log, "运行日志写明范围")
        self.assertIn("跳过 4/10 个测试模块", log)

    def test_an_explicit_base_is_registered_in_the_contract_and_handed_over(self) -> None:
        started = self.start("--gate-scope", "auto", "--select-base", BASE, FAKE_SELECTION_JSON=json.dumps(SELECTION),
                             FAKE_SCOPE_INFO_JSON=json.dumps(INFO_SUBSET))
        payload = self.wait(started["result"])
        self.assertEqual((payload["select_base"], payload["input_contract"]["select_base"]), (BASE, BASE))
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(),
                         ["--gate-scope:auto", f"--select-base:{BASE}", "--require-deployed", COMMIT])
        self.assertIsNone(bv.scope_problem(payload, "codex"))
        self.assertIn("基点", bv.scope_problem({**payload, "select_base": "c" * 40}, "codex") or "", "显式基点被改写")

    def test_the_options_are_validated(self) -> None:
        outcome = self.start("--gate-scope", "auto", "--select-base", "HEAD~1", expect=2)
        self.assertIn("40 位小写提交号", outcome["stderr"])
        outcome = self.start("--gate-scope", "full", "--select-base", BASE, expect=2)
        self.assertIn("只在", outcome["stderr"])
        self.assertEqual(bv.results(self.runroot), [], "被拒绝的启动不留结论")

    def test_auto_that_fell_back_to_the_full_set_is_a_full_conclusion_for_both_clients(self) -> None:
        started = self.start("--gate-scope", "auto", FAKE_SCOPE_INFO_JSON=json.dumps(INFO_FALLBACK))
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertNotIn("selection", payload)
        self.assertEqual(payload["gate_scope_resolved"]["fallback_reason"], "ValueError：找不到发版 tag")
        for client in ("codex", "claude"):
            self.assertIsNone(bv.scope_problem(payload, client), client)
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("门禁范围 auto：全集（回退原因：ValueError：找不到发版 tag）", message)
        self.assertNotIn("【子集", message)
        self.assertIn("后台验证门禁范围：门禁范围 auto：全集", Path(payload["log"]).read_text(encoding="utf-8"))

    def test_a_change_to_the_ownership_table_falls_back_to_the_full_set(self) -> None:
        echoed = {**SELECTION, "gate_scope": "full"}
        started = self.start("--gate-scope", "auto", FAKE_SELECTION_JSON=json.dumps(echoed), FAKE_SCOPE_INFO_JSON=json.dumps(INFO_TABLE_CHANGED))
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(payload["selection"], echoed)
        self.assertFalse(bv.is_subset(payload))
        for client in ("codex", "claude"):
            self.assertIsNone(bv.scope_problem(payload, client), client)
        self.assertIn("归属表自己在改动里", self.boundary()[1])

    def test_a_run_whose_decision_does_not_add_up_is_not_a_passed_conclusion(self) -> None:
        for label, environ, expected in (
                ("入口门禁没有回显 auto 的实际范围", {"FAKE_SELECTION_JSON": json.dumps(SELECTION)}, "没有 auto 的实际范围回显"),
                ("实际范围说子集却没有筛选回显", {"FAKE_SCOPE_INFO_JSON": json.dumps(INFO_SUBSET)}, "对不上"),
                ("实际范围说全集却带着子集的筛选回显", {"FAKE_SELECTION_JSON": json.dumps(SELECTION), "FAKE_SCOPE_INFO_JSON": json.dumps(INFO_FALLBACK)},
                 "对不上")):
            with self.subTest(label=label):
                started = self.start("--gate-scope", "auto", **environ)
                payload = self.wait(started["result"])
                self.assertEqual(payload["status"], "failed", payload)
                self.assertIn(expected, payload["reason"])
                bv.stop(self.runroot, "下一个子用例")
                for item in (self.runroot / "background-validation").glob("*.json"):
                    item.unlink()

    def test_a_subset_skips_the_chained_dryrun_and_a_full_conclusion_keeps_it(self) -> None:
        started = self.start("--gate-scope", "auto", "--then-dryrun", FAKE_SELECTION_JSON=json.dumps(SELECTION),
                             FAKE_SCOPE_INFO_JSON=json.dumps(INFO_SUBSET))
        payload = self.settle(started["result"], "dryrun")
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(payload["dryrun"]["action"], "skipped")
        self.assertIn("子集结论不自动接入口空跑", payload["dryrun"]["reason"])

    def test_a_full_fallback_still_chains_the_dryrun(self) -> None:
        started = self.start("--gate-scope", "auto", "--then-dryrun", FAKE_SCOPE_INFO_JSON=json.dumps(INFO_FALLBACK))
        payload = self.settle(started["result"], "dryrun")
        # 驱动参数文件缺（测试没给 --vc-env）：空跑照常被尝试、因缺参数跳过——与子集"不接"是两个原因，说明全集结论走的是原来的路径。
        self.assertIn("缺少驱动参数文件", payload["dryrun"]["reason"])

    def test_auto_does_not_switch_reuse_off(self) -> None:
        started = self.start("--gate-scope", "auto", "--reuse", "auto", FAKE_SCOPE_INFO_JSON=json.dumps(INFO_FALLBACK))
        # 沿用被尝试了（没有来源可沿用就记原因、照常起完整验证）；client-plan 则根本不尝试（那条用例断言没有 reuse_refused）。
        self.assertEqual(started["reuse_refused"], ["同一提交没有带沿用绑定的通过结论可沿用"])
        self.wait(started["result"])

    def test_auto_with_a_full_set_receipt_is_a_full_start_as_before(self) -> None:
        context, clock = self.root / "context.json", self.root / "clock.json"
        context.write_text("{}", encoding="utf-8")
        clock.write_text("{}", encoding="utf-8")
        started = self.start("--gate-scope", "auto", "--full-set-context", str(context), "--full-set-clock", str(clock))
        self.assertEqual(started["gate_scope"]["applied"], "full")
        self.assertIn("B-09", started["gate_scope"]["reason"])
        self.wait(started["result"])
        payload = self.settle(started["result"], "full_set_receipt")   # 通过之后还要签（这里是拒签）B-09 收据：等它落盘再收尾
        for key in ("gate_scope", "select_client", "gate_scope_resolved", "selection"):
            self.assertNotIn(key, payload)
            self.assertNotIn(key, payload["input_contract"])
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["--require-deployed", COMMIT], "命令行里没有任何筛选参数")


class AutoScopeClaudeTests(unittest.TestCase):
    """Claude 轮次的 auto 在起跑时就按全集处理：不登记筛选、不把筛选参数交给入口门禁，结论与改造前逐字相同。"""

    def test_a_claude_start_with_auto_is_a_full_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data, runroot = root / "data", root / "run"
            (data / "control").mkdir(parents=True)
            runroot.mkdir()
            stamp = "20261025t000000z"
            codex = data / "control" / f"codex-01600-supervisor-enable-{stamp}.json"
            codex.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64, "policy_version": 7}), encoding="utf-8")
            claude = data / "control" / f"claude-managed-deploy-{stamp}.json"
            claude.write_text(json.dumps({"status": "passed", "client": "claude",
                                          "identity": {"tool_files_sha256": "f" * 64, "policy_version": 2}}), encoding="utf-8")
            gates = root / "entry-gates.sh"
            gates.write_text(FAKE_GATES, encoding="utf-8")
            (root / "x.bundle").write_bytes(b"scope-bundle")
            calls = root / "calls.log"
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FAKE_GATES_CALLS": str(calls), "FAKE_GATES_RECEIPT": str(claude),
                           "FAKE_GATES_OTHERS": f"codex={codex.name}"}
            completed = subprocess.run(
                [sys.executable, "-B", str(MODULE), "start", "--runroot", str(runroot), "--data-root", str(data), "--bundle", str(root / "x.bundle"),
                 "--branch", "claude/test", "--commit", COMMIT, "--entry-gates", str(gates), "--client", "claude", "--gate-scope", "auto", "--preflight", "off", "--reuse", "off"],
                capture_output=True, text=True, timeout=120, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            started = json.loads(completed.stdout.strip().splitlines()[-1])
            self.addCleanup(lambda: bv.stop(runroot, "测试收尾"))
            self.assertEqual(started["gate_scope"]["applied"], "full")
            self.assertIn("只启用 codex 计划", started["gate_scope"]["reason"])
            deadline = time.monotonic() + 60
            while (bv._read(Path(started["result"])) or {}).get("status") not in ("passed", "failed", "aborted"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            payload = bv._read(Path(started["result"]))
            self.assertEqual(payload["status"], "passed", payload)
            for key in ("gate_scope", "select_client", "gate_scope_resolved", "selection"):
                self.assertNotIn(key, payload)
                self.assertNotIn(key, payload["input_contract"])
            self.assertEqual(calls.read_text(encoding="utf-8").split(), ["--require-deployed-all", COMMIT])
            code, message = bv.check(runroot, data, require_passed=True, client="claude")
            self.assertEqual(code, 0, message)


class SubsetNeverFeedsOthersTests(ScopeCase):
    """子集结论不作沿用来源、不作承接来源（只有全集结论可以）。"""

    def conclusion(self, **extra: object) -> dict:
        deployment = bv.latest_deployment(self.data, "codex")
        payload = {"schema_version": bv.SCHEMA, "commit": COMMIT, "branch": "x", "bundle": "/b", "deployment": deployment, "profile": "full-gates",
                   "mode": "re-execute", "status": "passed", "pid": None, "reuse_binding": {"schema_version": bv.REUSE_BINDING_SCHEMA},
                   "completed_at_utc": "2026-10-25T01:00:00Z", **extra}
        bv._write(bv.result_path(self.runroot, COMMIT, deployment), payload)
        return payload

    def args(self) -> object:
        import argparse

        return argparse.Namespace(runroot=self.runroot, commit=COMMIT, profile="full-gates")

    def test_a_subset_is_not_a_reuse_candidate_and_a_full_conclusion_is(self) -> None:
        self.conclusion()
        self.assertEqual(len(bv._reuse_candidates(self.args(), "codex")), 1)
        self.conclusion(selection=SELECTION, gate_scope="auto")
        self.assertEqual(bv._reuse_candidates(self.args(), "codex"), [], "即使带着沿用绑定，子集结论也不是沿用来源")
        self.conclusion(selection={**SELECTION, "gate_scope": "full"}, gate_scope="auto")
        self.assertEqual(len(bv._reuse_candidates(self.args(), "codex")), 1, "auto 回退全集的结论是全集结论，可以作沿用来源")

    def test_a_subset_is_not_an_inherit_source(self) -> None:
        import argparse

        for source in ({"status": "passed", "pid": None, "selection": SELECTION, "gate_scope": "auto"},
                       {"status": "failed", "pid": None, "gate_scope": "client-plan", "select_base": BASE}):
            with self.subTest(source=source):
                with self.assertRaisesRegex(bv.ValidationError, "不能作承接来源"):
                    bv._inherit_source_check(argparse.Namespace(), Path("/x/source.json"), (source, "d"), client="codex", work=Path("/w"),
                                             inputs={}, result_path=Path("/x/other.json"))


class WrapperDefaultScopeTests(unittest.TestCase):
    """驱动入口 ``background-validate.sh start``：Codex 缺省带 ``--gate-scope auto``，Claude 不带，操作者自己给了就不再带。"""

    WRAPPER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background-validate.sh"

    def run_wrapper(self, client: str, *options: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "bin").mkdir()
            # 替身 python3：把收到的命令行逐行写进文件，不真的起后台验证。
            (root / "bin" / "python3").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$FAKE_ARGV_OUT"\n', encoding="utf-8")
            (root / "bin" / "python3").chmod(0o755)
            (root / "driver").mkdir()
            (root / "driver" / "background-validate.sh").write_text(self.WRAPPER.read_text(encoding="utf-8"), encoding="utf-8")
            # 替身 lib.sh：只提供包装脚本用到的变量（客户端表在真实 lib.sh 里生成）。
            (root / "driver" / "lib.sh").write_text(
                'DRV="$(dirname "${BASH_SOURCE[0]}")"; RUNROOT=/run; D=/data; ARM64_VC_ENV=/env\n'
                f'CT_CLIENT={client}; CT_CLIENT_ARGV=(); if [ "$CT_CLIENT" != codex ]; then CT_CLIENT_ARGV=(--client "$CT_CLIENT"); fi\n', encoding="utf-8")
            output = root / "argv.txt"
            completed = subprocess.run(["bash", str(root / "driver" / "background-validate.sh"), "start", "/b.bundle", "br", COMMIT, *options],
                                       capture_output=True, text=True, timeout=60,
                                       env={**os.environ, "PATH": f"{root / 'bin'}:{os.environ['PATH']}", "FAKE_ARGV_OUT": str(output)})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return output.read_text(encoding="utf-8").splitlines()

    def scopes(self, argv: list[str]) -> list[str]:
        return [argv[index + 1] for index, item in enumerate(argv) if item.startswith("--gate-scope") and index + 1 < len(argv)]

    def test_codex_gets_auto_by_default_next_to_the_other_defaults(self) -> None:
        argv = self.run_wrapper("codex")
        self.assertEqual(self.scopes(argv), ["auto"])
        for default in ("--then-dryrun", "--preflight", "--reuse"):
            self.assertIn(default, argv)
        self.assertNotIn("--client", argv)

    def test_an_explicit_base_rides_along_with_the_default_auto(self) -> None:
        argv = self.run_wrapper("codex", "--select-base", BASE)
        self.assertEqual(self.scopes(argv), ["auto"])
        self.assertEqual(argv[argv.index("--select-base") + 1], BASE)

    def test_an_explicit_scope_replaces_the_default_in_both_spellings(self) -> None:
        self.assertEqual(self.scopes(self.run_wrapper("codex", "--gate-scope", "full")), ["full"])
        argv = self.run_wrapper("codex", "--gate-scope=client-plan", "--select-base", BASE, "--no-dryrun")
        self.assertEqual([item for item in argv if item.startswith("--gate-scope")], ["--gate-scope=client-plan"], "缺省的 auto 不再追加")
        self.assertNotIn("--then-dryrun", argv)

    def test_claude_stays_full_by_default(self) -> None:
        argv = self.run_wrapper("claude")
        self.assertEqual(self.scopes(argv), [])
        self.assertIn("claude", argv)

    def test_the_documentation_says_so(self) -> None:
        text = self.WRAPPER.read_text(encoding="utf-8")
        self.assertIn("--gate-scope auto", text)
        self.assertIn("gate_scope_resolved", text)


if __name__ == "__main__":
    unittest.main()
