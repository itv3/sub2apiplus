"""codex-final-wire-isolation 边界核对（Claude 升级控制面设计 B4-01 f 段，structural＋runtime）。

不起 Docker、不联网：沿用第三方负例的夹具（真实 Campaign 租约与共享监督器，先跑出等待封存的候选 attempt），另加

* 候选源码仓：真实 git 仓，生产提交与候选工作树按用例改动（officialegress 的 Codex 文件、Claude 专属文件、包外定型面、
  人工复核接缝、Codex 目录数据），Codex 运行目录的 active 选择指向一个 0.160.0 画像；
* 工具执行副本里的 Codex 替身：mitm 矩阵脚本替身按冻结环境写出真实形态的 mitm 证据根（计数走真实的 Codex mitm 逐请求
  计数），编目、收口、派生与逐规则 checker 替身按控制文件写出（逐规则输出含覆盖检查、选中观测的检查与未选中观测的检查）；
  场景清单里的 candidate-core-mitm 原样取自真实的 0.160.0 清单；
* Codex 专用账号事实换成读一份可被 mitm 替身改写的文件。

覆盖：轮次参数、structural 三组比较（Claude 专属改动不算、Codex 改动逐条列出、structural 不过不发真实请求）、只读前检
（运行画像副本、账号隔离）、runtime 的冻结步骤展开与计数入账、逐条检查分类（已判失败即失败、未覆盖不判、已判为零失败）、
环境恢复失败与账号漂移判污染、通过即签收据且校验入口通过；常量与出站门禁（tools/check_ledger_completeness.py）一致。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import check_ledger_completeness as ledger
from tools.official_client_capture import claude_upgrade_boundary_checks as boundary_checks
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_boundary_checks as boundary_tests

TOOL_ROOT = Path(__file__).resolve().parents[1]
CODEX = boundary_checks.CHECK_CODEX
VERSION = "0.160.0"
SNAPSHOT = "d" * 64
CODEX_ACCOUNT, CODEX_KEY = 31, 41
REAL_JOB = next(
    job
    for job in json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json").read_text(encoding="utf-8"))["capture_jobs"]
    if job["id"] == boundary_checks.CODEX_JOB_ID
)
COVERED_RULES = sorted(REAL_JOB["covers"])

FAKE_MITM = r'''#!/usr/bin/env python3
"""run_sub2api_openai_mitm_matrix.sh 的离线替身：记下环境，按 SUBJECTS × SCENARIOS 写出真实形态的 mitm 证据根。"""
import hashlib, json, os, sys
from pathlib import Path
control = json.load(open(os.environ["FAKE_CODEX_CONTROL"], encoding="utf-8"))
with open(os.environ["FAKE_CODEX_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({key: os.environ.get(key) for key in (
        "CAPTURE_CONTAINER", "CAPTURE_ROOT", "SERVICE_CONTAINER", "KEEPER_CONTAINER", "POSTGRES_CONTAINER", "CODEX_ACCOUNT_ID",
        "API_KEY_ID", "SUBJECTS", "SCENARIOS", "CODEX_MODEL", "CODEX_VERSION", "CODEX_BIN", "RUN_ID_PREFIX", "WINDOW_ID",
        "CAPTURE_TOOL_ROOT")}) + "\n")
runs = Path(os.environ["CAPTURE_ROOT"]) / "runs"
prefix, window = os.environ["RUN_ID_PREFIX"], os.environ["WINDOW_ID"]
RESPONSES = "/backend-api/codex/responses"
def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    path.chmod(0o600)
    return path
for subject in os.environ["SUBJECTS"].split():
    for scenario in os.environ["SCENARIOS"].split():
        run_id = f"{prefix}-{subject}-{scenario}-a1-{window}"
        root = runs / run_id
        if subject == "codex-http":
            rows = [{"_run_id": run_id, "_flow_id": f"f{i}", "request": {"method": "POST", "path": RESPONSES,
                     "body": {"json": {"model": os.environ["CODEX_MODEL"]}}}, "response": {"status": 200}} for i in range(2)]
            jsonl = write(root / "mitm" / subject / "codex-http.jsonl", rows)
        else:
            rows = [{"_run_id": run_id, "_websocket": True, "from_client": True, "path": RESPONSES,
                     "json": {"type": "response.create", "model": os.environ["CODEX_MODEL"]}},
                    {"_run_id": run_id, "_websocket": True, "from_client": False, "path": RESPONSES, "json": {"type": "response.completed"}}]
            jsonl = write(root / "mitm" / subject / "codex-ws.jsonl", rows)
        summary = root / "result" / scenario / "summary.json"
        summary.parent.mkdir(parents=True, mode=0o700)
        summary.write_text(json.dumps({"scenario": scenario, "valid": True, "turn_count": 1}), encoding="utf-8")
        summary.chmod(0o600)
        write(root / "result" / scenario / "turn1-events.jsonl", [{"type": "turn.started"}, {"type": "turn.completed"}])
        raw = jsonl.read_bytes()
        if f"{subject}-{scenario}" == control.get("fail_coordinate"):
            os.rename(root, str(root) + ".failed")
            sys.exit(1)
        run_summary = root / "run-summary.json"
        run_summary.write_text(json.dumps({
            "schema_version": "sub2api-openai-mitm-scenario/v2", "run_id": run_id, "status": "complete", "driver_return_code": 0,
            "subject": subject, "scenario": scenario, "scenario_result": {"valid": True, "error_type": ""},
            "jsonl": [{"path": jsonl.relative_to(root).as_posix(), "bytes": len(raw), "records": len(rows),
                       "sha256": hashlib.sha256(raw).hexdigest()}],
        }), encoding="utf-8")
        run_summary.chmod(0o600)
if control.get("account_drift"):
    path = Path(os.environ["FAKE_CODEX_ACCOUNT"])
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts["account"]["proxy_state"] = "9|NULL"
    path.write_text(json.dumps(facts), encoding="utf-8")
sys.exit(int(control.get("exit_code", 0)))
'''

FAKE_CATALOG = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
roots = [args[i + 1] for i, item in enumerate(args) if item == "--job-root"]
output = Path(args[args.index("--output-dir") + 1])
output.mkdir(parents=True, mode=0o700)
artifacts = []
for item in roots:
    _job, name, path = item.split("=", 2)
    for file in sorted(Path(path).joinpath("mitm").rglob("*.jsonl")):
        artifacts.append({"path": f"{name}/{file.relative_to(path).as_posix()}", "kind": "wire_dump", "scenario_ids": ["A01", "A09"]})
for name, payload in (("bundle-plan.json", {"entries": roots}), ("derivation-plan.json", {"entries": []}),
                      ("manifest-draft.json", {"artifacts": artifacts})):
    (output / name).write_text(json.dumps(payload), encoding="utf-8")
'''

FAKE_BUNDLE = r'''#!/usr/bin/env python3
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
if "--verify" in args:
    sys.exit(0)
bundle = Path(args[args.index("--bundle-dir") + 1])
bundle.mkdir(parents=True, mode=0o700)
for i, item in enumerate(args):
    if item == "--source-root":
        name, path = args[i + 1].split("=", 1)
        shutil.copytree(Path(path) / "mitm", bundle / name / "mitm")
'''

FAKE_CHECKER = r'''#!/usr/bin/env python3
"""candidate_rule_assertion.py 替身：覆盖检查（A01／A09 齐、A03 缺）、选中观测的检查与未选中观测的检查。"""
import json, os, sys
args = sys.argv[1:]
rule = args[args.index("--rule-id") + 1]
control = json.load(open(os.environ["FAKE_CODEX_CONTROL"], encoding="utf-8"))
checks = [
    {"id": "scenario-artifact-coverage", "passed": False, "expected": {"A01": ["wire_dump"], "A03": ["pcap"]},
     "actual": {"A01": ["wire_dump"], "A03": []}, "evidence_paths": []},
    {"id": "observed", "passed": rule not in control.get("failing_rules", []), "expected": {}, "actual": {"values": ["x", "x"]}, "evidence_paths": []},
    {"id": "no-observation", "passed": False, "expected": {}, "actual": {"matched_count": 0}, "evidence_paths": []},
    {"id": "needs-a03", "passed": False, "expected": {}, "actual": {"values": ["y"]}, "evidence_paths": []},
]
if rule in control.get("nothing_judged", []) or control.get("nothing_judged_all"):
    checks[1]["actual"] = {"values": []}
document = {"schema_version": "codex-candidate-rule-assertion/v1", "rule_id": rule, "status": "fail", "exit_code": 1, "checks": checks}
output = args[args.index("--output") + 1]
open(output, "w", encoding="utf-8").write(json.dumps(document))
sys.exit(1)
'''


def _expectations() -> dict[str, Any]:
    rule = lambda rule_id: {  # noqa: E731
        "rule_id": rule_id,
        "scenario_ids": ["A01"],
        "checks": [
            {"id": "observed", "select": {"record_type": "process_trace"}, "assertion": {"operator": "all_equal", "path": "x", "value": "x"}},
            {"id": "no-observation", "select": {"record_type": "process_trace"}, "assertion": {"operator": "count_equal", "value": 0}},
            {"id": "needs-a03", "select": {"record_type": "pcap", "scenario_ids": ["A03"]}, "assertion": {"operator": "all_equal", "path": "y", "value": "y"}},
        ],
    }
    return {"schema_version": "codex-candidate-rule-expectations/v1", "codex_version": VERSION, "scenarios": [], "rules": [rule(item) for item in COVERED_RULES]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    path.chmod(mode)


def build_source_repo(root: Path) -> str:
    """候选源码仓：Codex 运行目录（active 0.160.0）、Codex Go 文件、Claude 专属文件、包外定型面与复核接缝；返回提交。"""

    package = root / "backend" / "internal" / "officialegress"
    profile = json.dumps({"Digest": SNAPSHOT, "version": VERSION})
    _write(package / "catalogdata" / "runtime" / "profiles" / VERSION / f"{SNAPSHOT}.json", profile)
    graph = json.dumps({"schema_version": 1, "nodes": [
        {"purpose": purpose, "mode": "active", "build": {"version": VERSION}, "snapshot": {"version": VERSION, "digest": SNAPSHOT}}
        for purpose in ("openai_oauth_responses_http", "openai_oauth_responses_ws")
    ] + [{"purpose": "openai_oauth_responses_http", "mode": "previous", "build": {"version": "0.157.0"}, "snapshot": {"digest": "e" * 64}}]})
    import hashlib

    graph_sha = hashlib.sha256(graph.encode()).hexdigest()
    _write(package / "catalogdata" / "runtime" / "release-graphs" / f"{graph_sha}.json", graph)
    snapshots = json.dumps({"snapshots": []})
    snapshots_sha = hashlib.sha256(snapshots.encode()).hexdigest()
    _write(package / "catalogdata" / "runtime" / "snapshot-catalogs" / f"{snapshots_sha}.json", snapshots)
    _write(
        package / "catalogdata" / "runtime" / "release-catalog.json",
        json.dumps({
            "schema_version": 1,
            "release_graph": {"path": f"catalogdata/runtime/release-graphs/{graph_sha}.json", "sha256": graph_sha},
            "snapshot_catalog": {"path": f"catalogdata/runtime/snapshot-catalogs/{snapshots_sha}.json", "sha256": snapshots_sha},
        }),
    )
    _write(package / "catalogdata" / "claude" / "release-catalog.json", "{}\n")
    _write(package / "catalogdata" / "runtime" / "extra.json", "{}\n")
    _write(package / "executor.go", "package officialegress\n")
    _write(package / "claude_runtime.go", "package officialegress\n")
    _write(package / "executor_test.go", "package officialegress\n")
    _write(root / "backend" / "internal" / "service" / "openai_gateway_service.go", "package service\n")
    _write(root / "backend" / "internal" / "service" / "codex_surface.go", "package service\n// OpenAIOfficialEgress\n")
    _write(root / "backend" / "internal" / "service" / "gateway_claude.go", "package service\n")
    _write(root / "backend" / "main.go", "package main\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "production")
    return _git(root, "rev-parse", "HEAD")


def commit_variant(root: Path, base: str, changes: dict[str, str | None]) -> str:
    """不碰工作树与默认索引，在 ``base`` 上造一个改了若干文件的提交（当作生产部署提交）；``None`` 表示删除。"""

    index = root / ".git" / "variant-index"
    environment = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def git(*args: str, data: bytes | None = None) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, input=data, env=environment).stdout.decode().strip()

    git("read-tree", base)
    for path, content in changes.items():
        if content is None:
            git("update-index", "--force-remove", path)
        else:
            blob = git("hash-object", "-w", "--stdin", data=content.encode("utf-8"))
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    tree = git("write-tree")
    commit = git("commit-tree", tree, "-p", base, "-m", "variant")
    index.unlink()
    return commit


class CodexFixture(boundary_tests.BoundaryFixture):
    def setUp(self) -> None:
        super().setUp()
        shutil.rmtree(self.source)
        self.production_commit = build_source_repo(self.source)
        for name, body in (
            (boundary_checks.CODEX_MITM_SCRIPT, '#!/usr/bin/env bash\nexec python3 "$FAKE_CODEX_MITM" "$@"\n'),
            ("build_evidence_catalog.py", FAKE_CATALOG),
            ("build_assertion_bundle.py", FAKE_BUNDLE),
            ("derive_official_observations.py", "import sys\nsys.exit(0)\n"),
            ("candidate_rule_assertion.py", FAKE_CHECKER),
            ("codex_upgrade_live_request_provenance.py", "# 执行副本里的计数器（计数经仓库包调用）\n"),
            ("codex_upgrade_scenarios_0_160_0.json", json.dumps({"codex_version": VERSION, "capture_jobs": [REAL_JOB]})),
            ("codex_upgrade_evidence_labels_0_160_0.json", "{}\n"),
            ("candidate_rule_expectations_0_160_0.json", json.dumps(_expectations())),
            ("codex_upgrade_rules_0_160_0.json", "{}\n"),
        ):
            _write(self.tool_copy / name, body, 0o700)
        self.fake_mitm = self.root / "fake-codex-mitm.py"
        _write(self.fake_mitm, FAKE_MITM, 0o700)
        self.codex_control = self.root / "codex-control.json"
        self.codex_calls = self.root / "codex-calls.jsonl"
        self.codex_account = self.root / "codex-account.json"
        self.set_codex_control({})
        self.codex_account.write_text(json.dumps(self.account_facts()), encoding="utf-8")
        profile = (self.source / "backend/internal/officialegress/catalogdata/runtime/profiles" / VERSION / f"{SNAPSHOT}.json").read_bytes()
        _write(self.data / "runtime" / f"codex-profile-{VERSION}.json", profile)
        _write(self.data / "runtime" / "codex-fingerprint-capture-proxy", "#!/bin/sh\n", 0o700)

    def account_facts(self, **changes: Any) -> dict[str, Any]:
        facts = {
            "account": {"id": CODEX_ACCOUNT, "shape": boundary_checks.EXPECTED_CODEX_ACCOUNT_SHAPE, "proxy_state": "NULL|NULL"},
            "api_key": {"id": CODEX_KEY, "shape": "active|12|live", "eligible_accounts": str(CODEX_ACCOUNT)},
            "residual_temporary_proxies": 0,
        }
        facts.update(changes)
        return facts

    def set_codex_control(self, control: dict) -> None:
        self.codex_control.write_text(json.dumps(control), encoding="utf-8")

    def fake_account_facts(self, manifest, account_id, api_key_id, *, deadline=None):
        del manifest, deadline
        facts = json.loads(self.codex_account.read_text(encoding="utf-8"))
        assert (facts["account"]["id"], facts["api_key"]["id"]) == (account_id, api_key_id)
        return facts

    @contextlib.contextmanager
    def codex_patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(boundary_checks, "_codex_account_facts", self.fake_account_facts))
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_CODEX_MITM": str(self.fake_mitm),
                        "FAKE_CODEX_CONTROL": str(self.codex_control),
                        "FAKE_CODEX_CALLS": str(self.codex_calls),
                        "FAKE_CODEX_ACCOUNT": str(self.codex_account),
                    },
                )
            )
            yield

    def codex_arguments(self, **overrides: str) -> list[str]:
        values = {
            "--codex-account-id": str(CODEX_ACCOUNT),
            "--codex-api-key-id": str(CODEX_KEY),
            "--codex-model": "gpt-5.6-luna",
            "--production-commit": self.production_commit,
            **overrides,
        }
        return ["--acknowledge-live-requests", *[item for pair in values.items() for item in pair]]

    def run_codex(self, campaign: Path, attempt_id: str, **overrides: str) -> tuple[int, dict, str]:
        with self.codex_patched():
            return self.run_check(campaign, attempt_id, *self.codex_arguments(**overrides), check=CODEX)

    def mitm_calls(self) -> list[dict]:
        if not self.codex_calls.exists():
            return []
        return [json.loads(line) for line in self.codex_calls.read_text(encoding="utf-8").splitlines()]


class CodexPassTests(CodexFixture):
    def test_structural_and_runtime_pass_writes_receipt(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        # 候选与生产只差 Claude 专属文件、Claude 目录数据、包外非 Codex 文件与测试文件：都不进 Codex 比较集合。
        self.production_commit = commit_variant(
            self.source,
            self.production_commit,
            {
                "backend/internal/officialegress/claude_runtime.go": "package officialegress\n// 生产旧版\n",
                "backend/internal/officialegress/catalogdata/claude/release-catalog.json": '{"old": 1}\n',
                "backend/internal/service/gateway_claude.go": "package service\n// 生产旧版\n",
                "backend/internal/officialegress/executor_test.go": "package officialegress\n// 旧测试\n",
            },
        )
        code, result, stderr = self.run_codex(campaign, attempt_id)
        self.assertEqual((code, result.get("status")), (0, "passed"), stderr + json.dumps(result, ensure_ascii=False))
        receipt = boundary_checks.verify_boundary_receipt(attempt_root, CODEX, boundary_checks.receipt_path(attempt_root, CODEX))
        self.assertEqual(receipt["conclusion_scope"], "构建输入零差异＋mitm 观测维度零差异（不是 Codex 验收口径的全部规则通过）")
        # 真实请求：2 个主体 × 3 个场景，HTTP 每根 2 条、WS 每根 1 条 response.create（服务端帧不计）。
        self.assertEqual(receipt["live_requests"]["count"], 9)
        self.assertEqual(receipt["live_requests"]["by_endpoint"], {"http": 6, "ws": 3})
        self.assertTrue(receipt["live_requests"]["exact"])
        roles = {entry["role"] for entry in receipt["evidence"]}
        self.assertEqual({role for role in roles if ":" not in role}, boundary_checks.EVIDENCE_ROLES[CODEX])
        self.assertEqual(len([role for role in roles if role.startswith("rule_assertion:")]), len(COVERED_RULES))
        self.assertEqual(len([role for role in roles if role.startswith("codex_evidence_root:")]), 6)
        facts = receipt["facts"]
        self.assertEqual((facts["codex_version"], facts["production_commit"]), (VERSION, self.production_commit))
        self.assertEqual(facts["judged_check_count"], len(COVERED_RULES))
        self.assertEqual(facts["uncovered_check_count"], 3 * len(COVERED_RULES))
        self.assertEqual(facts["failed_checks"], [])
        self.assertTrue(all(group["differences"] == 0 for group in facts["structural"].values()))
        # 冻结步骤照 0.160.0 清单展开：只换 Campaign 坐标、数据根别名、容器、账号与模型；工具执行副本另给。
        call = self.mitm_calls()[-1]
        self.assertEqual((call["CODEX_ACCOUNT_ID"], call["API_KEY_ID"], call["CODEX_MODEL"]), (str(CODEX_ACCOUNT), str(CODEX_KEY), "gpt-5.6-luna"))
        self.assertEqual((call["SUBJECTS"], call["SCENARIOS"], call["WINDOW_ID"]), ("codex-http codex-ws", "s1 s2 s4", "run"))
        self.assertEqual((call["CODEX_VERSION"], call["CODEX_BIN"]), (VERSION, f"/opt/codex-{VERSION}/bin/codex"))
        self.assertEqual(call["CAPTURE_ROOT"], str(self.data))
        self.assertEqual(call["CAPTURE_TOOL_ROOT"], str(self.tool_copy))
        run_name = Path(receipt["boundary_run"]["path"]).name
        self.assertEqual(call["RUN_ID_PREFIX"], f"{boundary_checks.CODEX_RUN_PREFIX}{run_name}-candidate-mitm-core")
        self.assertEqual(call["CAPTURE_CONTAINER"], boundary_tests.candidate_tests.CAPTURE)
        self.assertIn(f'"job:boundary-{CODEX}:step-1:attempt-1"', self.ledgers(campaign))
        report = json.loads((attempt_root / receipt["boundary_run"]["path"] / "codex" / "runtime-report.json").read_text(encoding="utf-8"))
        row = report["rules"][0]
        self.assertEqual((row["judged"], row["failed"]), (["observed"], []))
        self.assertEqual(
            sorted(item["reason"] for item in row["uncovered"]),
            ["no-observation-selected", "scenario-coverage-not-judged", "scenario-not-captured"],
        )
        self.assertEqual(set(report["codex_tools"]), set(boundary_checks.CODEX_TOOL_SCRIPTS) | set(boundary_checks.CODEX_TOOL_DATA))


class CodexFailureTests(CodexFixture):
    def test_structural_difference_fails_without_live_requests(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        base = self.production_commit
        # 生产提交相对候选工作树：改了 Codex 执行器（候选为 modified）、少了 Codex 目录数据与包外定型面文件（候选为 added）、
        # 多了一个人工复核接缝文件（候选为 removed）。
        cases = (
            ("backend/internal/officialegress/executor.go", "package officialegress\n// 生产旧版\n", "codex_egress_go", "modified"),
            ("backend/internal/officialegress/catalogdata/runtime/extra.json", None, "codex_runtime_projection", "added"),
            ("backend/internal/service/codex_surface.go", None, "codex_egress_go", "added"),
            ("backend/internal/service/openai_ws_forwarder.go", "package service\n", "codex_egress_go", "removed"),
        )
        for relative, content, group, change in cases:
            with self.subTest(path=relative):
                variant = commit_variant(self.source, base, {relative: content})
                code, result, stderr = self.run_codex(campaign, attempt_id, **{"--production-commit": variant})
                self.assertEqual((code, result.get("status")), (1, "failed"), stderr)
                self.assertEqual(result["failure_codes"], ["structural-difference"])
                self.assertEqual(result["live_request_count"], 0)
                verdict = json.loads((Path(result["run"]) / boundary_checks.VERDICT_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(verdict["facts"]["structural_differences"], {group: [{"path": relative, "change": change}]})
        self.assertEqual(self.mitm_calls(), [])
        self.assertFalse(boundary_checks.receipt_path(attempt_root, CODEX).exists())

    def test_runtime_failures_and_contamination(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        for control, failure in (
            ({"failing_rules": [COVERED_RULES[0]]}, f"codex-check-failed:{COVERED_RULES[0]}:observed"),
            ({"nothing_judged_all": True}, "codex-runtime-nothing-judged"),
            ({"fail_coordinate": "codex-ws-s2", "exit_code": 1}, "codex-capture-failed-coordinates"),
        ):
            with self.subTest(control=control):
                self.set_codex_control(control)
                code, result, stderr = self.run_codex(campaign, attempt_id)
                self.assertEqual((code, result.get("status")), (1, "failed"), stderr)
                self.assertIn(failure, result["failure_codes"])
                self.assertGreater(result["live_request_count"], 0)
                self.assertTrue(result["live_requests"]["exact"])
        self.assertFalse(boundary_checks.receipt_path(attempt_root, CODEX).exists())
        # 账号在执行前后漂移（Codex 脚本没把代理恢复成空）：判环境污染。
        self.set_codex_control({"account_drift": True})
        code, result, stderr = self.run_codex(campaign, attempt_id)
        self.assertEqual((code, result.get("status")), (1, "environment_contaminated"), stderr)
        self.assertIn("codex-account-drifted", result["failure_codes"])

    def test_preflight_refusals_leave_no_run(self) -> None:
        campaign, attempt_id, attempt_root = self.formal_attempt()
        runs = attempt_root / "client-evidence" / CODEX / "runs"
        for label, mutate, message in (
            ("账号不隔离", lambda: self.codex_account.write_text(json.dumps(self.account_facts(api_key={"id": CODEX_KEY, "shape": "active|12|live", "eligible_accounts": "31,32"})), encoding="utf-8"), "codex-api-key-group-not-isolated"),
            ("画像副本不一致", lambda: _write(self.data / "runtime" / f"codex-profile-{VERSION}.json", "{}"), "运行画像副本"),
            ("缺判定工具", lambda: (self.tool_copy / "candidate_rule_assertion.py").unlink(), "candidate_rule_assertion.py"),
        ):
            with self.subTest(label=label):
                mutate()
                code, _result, stderr = self.run_codex(campaign, attempt_id)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertFalse(runs.exists())
                self.setUp_restore()
        for overrides, message in (
            ({"--production-commit": "abc"}, "--production-commit"),
            ({"--codex-model": "bad model"}, "--codex-model"),
            ({"--production-commit": "0" * 40}, "git cat-file"),
        ):
            with self.subTest(overrides=overrides):
                code, _result, stderr = self.run_codex(campaign, attempt_id, **overrides)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
        self.assertEqual(self.mitm_calls(), [])

    def setUp_restore(self) -> None:
        self.codex_account.write_text(json.dumps(self.account_facts()), encoding="utf-8")
        profile = (self.source / "backend/internal/officialegress/catalogdata/runtime/profiles" / VERSION / f"{SNAPSHOT}.json").read_bytes()
        _write(self.data / "runtime" / f"codex-profile-{VERSION}.json", profile)
        _write(self.tool_copy / "candidate_rule_assertion.py", FAKE_CHECKER, 0o700)


class ClassificationTests(unittest.TestCase):
    def test_selected_count_shapes(self) -> None:
        self.assertEqual(boundary_checks._selected_count({"matched_count": 3}), 3)
        self.assertEqual(boundary_checks._selected_count({"values": [1, 2]}), 2)
        self.assertEqual(boundary_checks._selected_count({"pairs": []}), 0)
        self.assertEqual(boundary_checks._selected_count({"groups": [{"record_count": 2}], "invalid_record_ids": ["x"]}), 3)
        self.assertIsNone(boundary_checks._selected_count({"other": 1}))

    def test_input_validation_and_unknown_checks_are_judged_failures(self) -> None:
        rule = {"rule_id": "SPEC-X-001", "scenario_ids": ["A01"], "checks": []}
        row = boundary_checks._classify_rule(
            {"rule_id": "SPEC-X-001", "checks": [{"id": "input-validation", "passed": False}, {"id": "mystery", "passed": True}]}, rule
        )
        self.assertEqual((row["judged"], row["failed"]), (["input-validation", "mystery"], ["input-validation", "mystery"]))


class StructuralConstantsTests(unittest.TestCase):
    """structural 的比较集合与出站门禁同一界定（受管根不得导入根外模块，常量逐字复制，这里对照原模块）。"""

    def test_constants_match_ledger_gate(self) -> None:
        self.assertEqual(boundary_checks.CODEX_SURFACE_RE.pattern, ledger.SURFACE_RE.pattern)
        self.assertEqual(boundary_checks.CODEX_SURFACE_SCOPE_EXCLUSIONS, frozenset(ledger.SCOPE_EXCLUSIONS))
        seams = set(ledger.REQUIRED_REVIEW_TOUCHPOINTS) | set(ledger.IDENTITY_BOUNDARY_TOUCHPOINTS) | set(ledger.HUMAN_REVIEW_SEAMS)
        expected = {path for path in seams if path.startswith("backend/") and path.endswith(".go")}
        self.assertEqual(boundary_checks.CODEX_REVIEW_TOUCHPOINTS, frozenset(expected))

    def test_real_codex_runtime_catalog_resolves_active(self) -> None:
        active = boundary_checks._codex_active(TOOL_ROOT.parents[1])
        # 0.163.0 收尾（10-23）：VC-6 晋升后 Runtime Catalog 的 Active 为 0.163.0（Previous 0.160.0）。
        self.assertEqual(active["version"], "0.163.0")
        self.assertEqual(len(active["files"]), 4)

    def test_real_codex_job_shape(self) -> None:
        job = boundary_checks._codex_job(TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json", "0.160.0")
        self.assertEqual(len(job["covers"]), 24)
        with self.assertRaises(ConfigurationError):
            boundary_checks._codex_job(TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json", "0.157.0")


if __name__ == "__main__":
    unittest.main()
