"""Claude 收尾编排（Claude 升级控制面设计 B5b-06）的验收。

* 终态合同（``tools/claude_terminal_state.py``）：真实仓库的 production_active 恰是合同生效前基线、三项全等才豁免；合成夹具
  的正负例；历史收据的目录类绑定按收据入库提交回读（临时 Git 仓库）；Go 读侧同款的 Approval 身份核对；
* 两个门禁消费者：``check_runtime_catalog_projection`` 实检在 Codex 运行投影通过时仍按 Claude 终态缺失、损坏失败关闭，
  stdout 只有运行投影报告；``evidence_index`` 的 Claude 分支与 Codex 分支的参数边界；
* Claude 收尾（``codex_closeout`` 配 ``claude-upgrade-closeout/v1``）：演练仓库上 ``prepare --dry-run`` 零写入、prepare／gates／
  publish 重跑幂等，签出的终态收据通过 Claude 校验器与两个消费者；第二部分摘要连锁在隔离配对夹具上恰是三处唯一替换；
* 材料读侧：独立解释器在受管根（合成的两棵受管树）上重放 VC-2 批准包与 VC-1 官方观测，含只读复用沿前序取证。

全部只读真实仓库的工作树文件（不读 Git 历史）；Git 操作都在临时仓库里。
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import claude_closeout
from tools import codex_closeout as closeout
from tools.ci import client_table, entry_gates

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS = REPO_ROOT / "tools"
CLAUDE = client_table.spec("claude")
CLAUDE_GUIDE = "docs/" + CLAUDE["guide_file"]
CLAUDE_PART2 = CLAUDE["guide_part2_heading"]
OFFICIAL_CAMPAIGN = "c-claude-fixture-official"
FINAL_CAMPAIGN = "c-claude-fixture-final"
TERMINAL_RELATIVE = "docs/egress/maintenance/CLAUDE_CODE_998_TO_999_TERMINAL_STATE_RECEIPT.json"


def load_tool(name: str, relative: str):
    """按文件加载 ``tools/`` 下的脚本型工具（它们按 ``tools`` 目录在 sys.path 上的方式互相导入）。"""

    module = sys.modules.get(name)
    if module is not None:
        return module
    added = str(TOOLS) not in sys.path
    if added:
        sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location(name, TOOLS / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(str(TOOLS))
    return module


terminal = load_tool("claude_terminal_state", "claude_terminal_state.py")
projection = load_tool("check_runtime_catalog_projection", "check_runtime_catalog_projection.py")
evidence_index = load_tool("evidence_index", "evidence_index.py")


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    return path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "隔离验收")
    git(repo, "config", "user.email", "closeout@example.invalid")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-gpg-sign", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def claude_guide(note: str) -> str:
    return (
        "# Claude Code 客户端仿真与版本演进手册\n\n# 第一部分 目标\n\n### SPEC-FIXTURE-001 别处\n原文\n\n"
        + CLAUDE_PART2 + "\n\n" + note + "\n\n### SPEC-FIXTURE-001 规则\n旧说明\n\n# 第三部分 实现\n\n结束\n"
    )


# ---------------------------------------------------------------------------
# 终态合同
# ---------------------------------------------------------------------------


class TerminalStateTests(unittest.TestCase):
    def test_real_repository_is_the_exact_pre_contract_baseline(self) -> None:
        state = terminal.release_catalog_state(REPO_ROOT)
        baseline = terminal.PRE_CONTRACT_BASELINE
        self.assertEqual(
            (state["version"], state["production_active"]["release_sha256"], state["production_active"]["approval"]["sha256"]),
            (baseline["version"], baseline["release_sha256"], baseline["approval_sha256"]),
        )
        for phrase in ("合同生效前", "FW-G", "FW-H", "Campaign 链"):
            self.assertIn(phrase, baseline["reason"])
        report = terminal.validate_claude_terminal_state_receipts(REPO_ROOT)
        self.assertEqual(report["status"], "pre_contract_baseline")
        self.assertIsNone(report["receipt"])
        # 三项任一不同都不是基线。
        for key in ("version", "release_sha256", "approval_sha256"):
            with self.subTest(key=key):
                near = {**baseline, key: "0.0.0" if key == "version" else "0" * 64}
                with self.assertRaisesRegex(terminal.TerminalStateError, "缺少终态收据"):
                    terminal.validate_claude_terminal_state_receipts(REPO_ROOT, baseline=near)

    def test_synthetic_cases(self) -> None:
        self.assertEqual(terminal.self_test(), (3, 10))

    def test_approval_must_match_the_selected_release_like_the_go_read_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = terminal.write_fixture_catalog(root, "9.9.9")
            approval = root / terminal.OFFICIALEGRESS_RELATIVE / state["production_active"]["approval"]["path"]
            document = json.loads(approval.read_text())
            document["target"]["release_bundle_sha256"] = "0" * 64
            approval.write_text(json.dumps(document))
            catalog_path = root / terminal.CATALOG_RELATIVE
            catalog = json.loads(catalog_path.read_text())
            catalog["selectors"]["production_active"]["approval"]["sha256"] = terminal._sha256(approval.read_bytes())
            catalog_path.write_text(json.dumps(catalog))
            with self.assertRaisesRegex(terminal.TerminalStateError, "生产批准与 production_active"):
                terminal.release_catalog_state(root)

    def test_historical_receipts_replay_catalog_bindings_from_their_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            init_repo(repo)
            old = terminal.write_fixture_catalog(repo, "9.9.8")
            terminal.write_fixture_receipt(repo, terminal.fixture_terminal_receipt(repo, old, previous_version="9.9.7"))
            commit_all(repo, "9.9.8 终态")
            new = terminal.write_fixture_catalog(repo, "9.9.9")
            current = terminal.write_fixture_receipt(repo, terminal.fixture_terminal_receipt(repo, new, previous_version="9.9.8"))
            commit_all(repo, "9.9.9 终态")
            report = terminal.validate_claude_terminal_state_receipts(repo)
            self.assertEqual(report["status"], "validated")
            self.assertEqual(report["receipt"], current.relative_to(repo).as_posix())
            self.assertEqual(len(report["validated_receipts"]), 2)
            # 生产不变、下一轮候选改了 validation_candidate：当前收据的目录绑定按入库提交回读，照样有效。
            catalog_path = repo / terminal.CATALOG_RELATIVE
            catalog = json.loads(catalog_path.read_text())
            catalog["selectors"]["validation_candidate"]["changeset"] = "claude-fixture-next-candidate"
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
            commit_all(repo, "下一轮候选")
            self.assertEqual(terminal.validate_claude_terminal_state_receipts(repo)["status"], "validated")
            # 收据登记的目录摘要既不是当前文件、也不是收据入库提交里的那份：历史回读救不回来。
            terminal._reseal(current, lambda receipt: receipt["release_catalog"]["catalog"].update(sha256="0" * 64))
            with self.assertRaisesRegex(terminal.TerminalStateError, "摘要漂移"):
                terminal.validate_claude_terminal_state_receipts(repo)


# ---------------------------------------------------------------------------
# 两个门禁消费者
# ---------------------------------------------------------------------------


class ConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def combined(self, name: str, *, receipt: bool = True):
        root, dump, runtime = projection._build_fixture(self.base / name)
        state = terminal.write_fixture_catalog(root, "9.9.9")
        path = terminal.write_fixture_receipt(root, terminal.fixture_terminal_receipt(
            root, state, previous_version="9.9.8", campaign_id=FINAL_CAMPAIGN, official_campaign_id=OFFICIAL_CAMPAIGN))
        if not receipt:
            path.unlink()
        return root, dump, runtime, path

    def run_main(self, root: Path, dump: Path, runtime: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = projection.main(["--root", str(root), "--dump", str(dump), "--repo", str(runtime)])
        return code, out.getvalue(), err.getvalue()

    def test_projection_check_keeps_codex_report_and_fails_on_claude_terminal(self) -> None:
        root, dump, runtime, _path = self.combined("ok")
        code, out, err = self.run_main(root, dump, runtime)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(projection.verify(root, dump, runtime), ensure_ascii=False) + "\n")
        root, dump, runtime, _path = self.combined("missing", receipt=False)
        projection.verify(root, dump, runtime)
        code, out, err = self.run_main(root, dump, runtime)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Claude 终态门禁失败", err)
        self.assertIn("缺少终态收据", err)
        root, dump, runtime, path = self.combined("corrupt")
        path.write_text("{")
        code, out, err = self.run_main(root, dump, runtime)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Claude 终态门禁失败", err)
        # Codex 运行投影失败仍按原报错先报出。
        root, dump, runtime, _path = self.combined("codex-orphan")
        (runtime / "orphan.json").write_text("{}")
        code, out, err = self.run_main(root, dump, runtime)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("🔴 运行投影闭集校验失败：仓库存在未经终态收据批准的多余文件"), err)

    def test_projection_self_test_reports_codex_first_then_claude(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(projection.self_test(), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "运行投影闭集门禁自测通过：正例 2 项、负例 16 项",
            "Claude 终态门禁自测通过：正例 4 项、负例 12 项",
        ])

    def test_evidence_index_claude_branch(self) -> None:
        root, _dump, _runtime, path = self.combined("index")
        text = claude_guide(f"官方证据 Campaign {OFFICIAL_CAMPAIGN}，末级 {FINAL_CAMPAIGN}。")
        found = evidence_index.active_campaign_evidence(text, client="claude", root=root)
        receipt = json.loads(path.read_text())
        self.assertEqual(found.version, "9.9.9")
        self.assertEqual(found.receipt, path.relative_to(root).as_posix())
        self.assertEqual((found.official_campaigns, found.canonical_campaign), ((OFFICIAL_CAMPAIGN,), FINAL_CAMPAIGN))
        self.assertEqual((found.archive_path, found.archive_sha256, found.archive_bytes),
                         (receipt["evidence_archive"]["path"], receipt["evidence_archive"]["sha256"], receipt["evidence_archive"]["bytes"]))
        with self.assertRaisesRegex(ValueError, "未写明"):
            evidence_index.active_campaign_evidence(claude_guide(f"只写了 {FINAL_CAMPAIGN}"), client="claude", root=root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Claude 终态门禁失败"):
            evidence_index.active_campaign_evidence(text, client="claude", root=root)
        # 真实仓库：合同生效前基线没有 Campaign 链，返回 None。
        real = (REPO_ROOT / CLAUDE_GUIDE).read_text()
        self.assertIsNone(evidence_index.active_campaign_evidence(real, client="claude"))
        # Codex 分支只读本仓库固定坐标。
        with self.assertRaisesRegex(ValueError, "客户端或仓库根非法"):
            evidence_index.active_campaign_evidence("", root=root)
        with self.assertRaisesRegex(ValueError, "客户端或仓库根非法"):
            evidence_index.active_campaign_evidence("", client="other")


# ---------------------------------------------------------------------------
# Claude 收尾演练仓库
# ---------------------------------------------------------------------------


class ClaudeCloseoutTests(unittest.TestCase):
    """真实 Git 候选、真实 Claude 终态签发与本地推送；Campaign 材料读侧用夹具替代（另由 MaterialReaderTests 覆盖）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo, self.work = self.root / "repo", self.root / "work"
        init_repo(self.repo)
        guide = claude_guide("测试基线")
        (self.repo / CLAUDE_GUIDE).parent.mkdir(parents=True)
        (self.repo / CLAUDE_GUIDE).write_text(guide)
        (self.repo / "tools").mkdir()
        shutil.copyfile(TOOLS / "claude_terminal_state.py", self.repo / "tools/claude_terminal_state.py")
        capture = self.repo / "tools/official_client_capture"
        scenarios = {"schema_version": "claude-upgrade-scenarios/v1", "claude_version": "9.9.9",
                     "source_spec": {"path": CLAUDE_GUIDE, "fragment": "第二部分", "sha256": closeout.part2_sha(guide, heading=CLAUDE_PART2)},
                     "evidence_scenarios": []}
        write_json(capture / "claude_upgrade_scenarios_9_9_9.json", scenarios)
        expectations = {"schema_version": "claude-candidate-rule-expectations/v1", "claude_version": "9.9.8",
                        "generated_from": {"scenarios": {"claude_version": "9.9.9", "sha256": claude_closeout.canonical_sha256(scenarios)}},
                        "rules": []}
        expectations_path = write_json(capture / "claude_rule_expectations_9_9_8.json", expectations)
        (capture / "claude_rule_assertion.py").write_text(
            "FROZEN_PROFILE_SHA256S: dict[str, str] = {\n    \"9.9.8\": \"" + closeout.sha(expectations_path.read_bytes()) + "\",\n}\n")
        state = terminal.write_fixture_catalog(self.repo, "9.9.9")
        facts = terminal.fixture_terminal_receipt(self.repo, state, previous_version="9.9.8",
                                                  campaign_id=FINAL_CAMPAIGN, official_campaign_id=OFFICIAL_CAMPAIGN)
        self.base = commit_all(self.repo, "隔离基线")
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.material = {"client": "claude", "campaign_path": str(self.root / "campaign"), "campaign_id": FINAL_CAMPAIGN,
                         "candidate_id": "candidate-test", "target_version": "9.9.9",
                         "classification_sha256": "a" * 64, "official_stage_sha256": "b" * 64}
        write_json(self.root / "material.json", self.material)
        write_json(self.root / "facts.json", facts)
        self.new_guide = claude_guide(f"经审核的新规则；官方证据 Campaign {OFFICIAL_CAMPAIGN}，末级 {FINAL_CAMPAIGN}。")
        (self.root / "guide.md").write_text(self.new_guide)
        (self.root / "env.sh").write_text("# 隔离门禁参数\n")
        (self.root / "driver").mkdir()
        (self.root / "driver/entry-gates.sh").write_text("#!/bin/bash\nexit 1\n")
        (self.root / "frontend").mkdir()
        (self.root / "frontend/pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        write_json(self.root / "decision.json", {"reason": "演练没有生产清理对象"})
        script = self.root / "verify.py"
        script.write_text("#!/usr/bin/env python3\nimport os, json\nfrom pathlib import Path\np=Path(os.environ['CODEX_CLOSEOUT_RESULT'])\np.parent.mkdir(parents=True, exist_ok=True)\nv={'schema_version':'codex-closeout-action-result/v1','status':'passed','operation_key':os.environ['CODEX_CLOSEOUT_OPERATION_KEY'],'review_sha256':os.environ['CODEX_CLOSEOUT_REVIEW_SHA256'],'execution_receipt':json.loads(Path(__file__).with_name('action-proof.json').read_text())}\np.write_text(json.dumps(v))\n")
        script.chmod(0o700)
        write_json(self.root / "action-proof.json", closeout.safety.file_binding(self.root / "decision.json"))
        self.config = {"schema_version": CLAUDE["closeout_schema"], "repo": str(self.repo), "work_root": str(self.work),
                       "guide": str(self.root / "guide.md"), "material": str(self.root / "material.json"),
                       "terminal_facts": str(self.root / "facts.json"), "terminal_relative": TERMINAL_RELATIVE, "tag": "claude-isolated-test",
                       "gate": {"driver": str(self.root / "driver/entry-gates.sh"), "vc_env": str(self.root / "env.sh"),
                                "node_modules": str(self.root / "frontend")},
                       "cleanup": {"mode": "defer", "reason": "没有清理对象", "inputs": [str(self.root / "decision.json")]},
                       "deployment_check": {"script": str(script), "arguments": [],
                                            "inputs": [str(self.root / "action-proof.json"), str(self.root / "decision.json")],
                                            "result": str(self.work / "deployment.json")},
                       "push": {"remote": "origin", "ref": "refs/heads/main", "expected_tip": self.base}}
        self.plan_path = self.work / "plan.json"
        for patcher in (
            mock.patch.object(closeout, "collect_guide_material", return_value=self.material),
            mock.patch("tools.upstream_merge.freeze.generate_freeze_successor", return_value={"transitions": []}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def approvals(self, plan) -> dict[str, Path]:
        now = datetime.now(timezone.utc)
        result = {}
        for scope in ("guide-review", "release-publication"):
            path = self.root / (scope + ".json")
            write_json(path, {"schema_version": "guide-review-approval/v1" if scope == "guide-review" else "codex-closeout-operation-approval/v1",
                              "status": "approved", "review_sha256": plan["review_sha256"], "scope": scope, "approved_by": "隔离测试角色",
                              "approved_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                              "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                              "proof": closeout.safety.file_binding(self.root / "decision.json")})
            result[scope] = path
        return result

    def gate_result(self, plan):
        """按真实导出的字段构造门禁结果夹具（与 Codex 收尾测试同一形状）；不宣称实际运行全量业务测试。"""
        root = self.work / "gates"
        rows, manifest_rows, units = [], [], []
        for name in entry_gates.profile_gates("full-gates"):
            command, directory = entry_gates.GATE_COMMANDS[name]
            log = root / "executor" / (name + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("隔离结果夹具\n")
            write_json(root / "logs" / (name + ".gate.json"), {
                "gate_id": name, "command": command, "working_directory": directory, "status": "passed", "exit_code": 0,
                "tree_head": plan["candidate_commit"], "mode": "re-execute", "failed_units": [], "inherited_units": [],
                "started_at_utc": "2026-10-08T00:00:00Z", "completed_at_utc": "2026-10-08T00:01:00Z"})
            rows.append({"gate_id": name, "status": "passed", "exit_code": 0, "gate_json": "logs/" + name + ".gate.json"})
            manifest_rows.append({"gate_id": name, "units": [name]})
            units.append({"unit_id": name, "passed": True, "exit_code": 0, "orphans": 0, "log": str(log)})
        write_json(root / "entry-gates.json", {"schema_version": entry_gates.ENTRY_SUMMARY_SCHEMA, "status": "passed", "profile": "full-gates",
                   "mode": "re-execute", "source": {"commit": plan["candidate_commit"], "tree_head": plan["candidate_commit"]},
                   "gates": rows, "executor_summary": str(root / "executor/summary.json")})
        write_json(root / "gates-manifest.json", {"schema_version": entry_gates.GATES_SCHEMA, "profile": "full-gates", "gates": manifest_rows,
                   "units": [{"unit_id": row["unit_id"]} for row in units]})
        write_json(root / "executor/summary.json", {"schema_version": entry_gates.GATES_SUMMARY_SCHEMA, "status": "passed", "mode": "re-execute",
                   "units": units, "units_not_run": [], "test_groups": {}})
        closeout.journal(plan, "gates", "started", {"fixture": True})
        return closeout.run_gates(self.plan_path)

    def expected_outputs(self) -> list[str]:
        return sorted([CLAUDE_GUIDE, "tools/official_client_capture/claude_upgrade_scenarios_9_9_9.json",
                       "tools/official_client_capture/claude_rule_expectations_9_9_8.json",
                       "tools/official_client_capture/claude_rule_assertion.py", TERMINAL_RELATIVE])

    def test_dry_run_is_zero_write_and_lists_the_claude_cascade(self) -> None:
        result = closeout.prepare(self.config, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["changed_paths"], self.expected_outputs())
        self.assertFalse(self.work.exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(closeout.prepare(self.config, dry_run=True), result)

    def test_prepare_signs_a_receipt_that_passes_the_validator_and_both_consumers(self) -> None:
        plan = closeout.prepare(self.config)
        self.assertEqual(plan["schema_version"], "claude-upgrade-closeout/v1")
        self.assertEqual(plan["outputs"], self.expected_outputs())
        self.assertEqual(closeout.prepare(self.config), plan)
        candidate = Path(plan["candidate"])
        self.assertEqual(git(candidate, "rev-parse", "claude-closeout-candidate"), plan["candidate_commit"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        # 终态收据：Claude 校验器与两个门禁消费者。
        report = terminal.validate_claude_terminal_state_receipts(candidate)
        self.assertEqual((report["status"], report["receipt"]), ("validated", TERMINAL_RELATIVE))
        self.assertEqual(projection.verify_claude_terminal(candidate)["status"], "validated")
        found = evidence_index.active_campaign_evidence(self.new_guide, client="claude", root=candidate)
        self.assertEqual((found.version, found.receipt, found.canonical_campaign), ("9.9.9", TERMINAL_RELATIVE, FINAL_CAMPAIGN))
        # 三处摘要连锁在候选里自洽。
        capture = candidate / "tools/official_client_capture"
        scenarios = json.loads((capture / "claude_upgrade_scenarios_9_9_9.json").read_text())
        self.assertEqual(scenarios["source_spec"]["sha256"], closeout.part2_sha(self.new_guide, heading=CLAUDE_PART2))
        expectations_path = capture / "claude_rule_expectations_9_9_8.json"
        expectations = json.loads(expectations_path.read_text())
        self.assertEqual(expectations["generated_from"]["scenarios"]["sha256"], claude_closeout.canonical_sha256(scenarios))
        self.assertIn(closeout.sha(expectations_path.read_bytes()), (capture / "claude_rule_assertion.py").read_text())
        # 同一工作根换了输入（终态坐标）必须新建工作根。
        with self.assertRaisesRegex(closeout.CloseoutError, "本轮输入已变化"):
            closeout.prepare({**self.config, "terminal_relative": "docs/egress/maintenance/CLAUDE_CODE_997_TO_999_TERMINAL_STATE_RECEIPT.json"})

    def test_gates_and_publish_are_idempotent(self) -> None:
        plan = closeout.prepare(self.config)
        gates = self.gate_result(plan)
        self.assertEqual(closeout.run_gates(self.plan_path), gates)
        approvals = self.approvals(plan)
        approvals_map = {"guide-review": approvals["guide-review"], "release-publication": approvals["release-publication"], "cleanup": None}
        published = closeout.publish(self.plan_path, approvals_map)
        self.assertEqual(published["status"], "passed")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), plan["candidate_commit"])
        remote = git(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        self.assertEqual(remote, plan["candidate_commit"])
        self.assertEqual(closeout.publish(self.plan_path, approvals_map), published)
        self.assertEqual(terminal.validate_claude_terminal_state_receipts(self.repo)["receipt"], TERMINAL_RELATIVE)
        # 发布之后不重签：原坐标已存在、换坐标同一版本都拒绝。
        again = {**self.config, "work_root": str(self.root / "work-again"),
                 "deployment_check": {**self.config["deployment_check"], "result": str(self.root / "work-again/deployment.json")},
                 "push": {**self.config["push"], "expected_tip": plan["candidate_commit"]}}
        with self.assertRaisesRegex(closeout.CloseoutError, "终态坐标已存在"):
            closeout.prepare(again, dry_run=True)
        with self.assertRaisesRegex(closeout.CloseoutError, "目标版本已有终态收据"):
            closeout.prepare({**again, "terminal_relative": "docs/egress/maintenance/CLAUDE_CODE_997_TO_999_TERMINAL_STATE_RECEIPT.json"},
                             dry_run=True)

    def test_gate_driver_gets_the_claude_candidate_branch(self) -> None:
        plan = closeout.prepare(self.config)
        real_run, driver_calls = subprocess.run, []

        def run(argv, *args, **kwargs):
            if argv[0] != "bash":
                return real_run(argv, *args, **kwargs)
            driver_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1)

        with mock.patch.object(closeout.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(closeout.CloseoutError, "收尾全量门禁失败"):
                closeout.run_gates(self.plan_path)
        self.assertEqual(len(driver_calls), 1)
        argv = driver_calls[0]
        self.assertEqual(argv[-3:], ["claude-closeout-candidate", plan["candidate_commit"], str(self.root / "frontend")])
        # Codex 一行的三项就是改造前收尾里的字面。
        codex = client_table.spec("codex")
        self.assertEqual((codex["closeout_schema"], codex["closeout_candidate_branch"], codex["terminal_receipt_prefix"]),
                         (closeout.SCHEMA, "codex-closeout-candidate", "CODEX_CLI_"))
        self.assertEqual(closeout.CLIENT_SCHEMAS, {"codex-upgrade-closeout/v1": "codex", "claude-upgrade-closeout/v1": "claude"})

    def test_receipt_coordinates_version_and_material_are_checked_before_any_write(self) -> None:
        cases = (
            ({"terminal_relative": "docs/egress/maintenance/CODEX_CLI_998_TO_999_TERMINAL_STATE_RECEIPT.json"}, None, "终态收据坐标"),
            ({}, lambda facts: facts["release_catalog"]["production_active"].update(changeset="other"), "不满足合同"),
        )
        original = json.loads((self.root / "facts.json").read_text())
        for overrides, mutate, message in cases:
            with self.subTest(message=message):
                facts = json.loads(json.dumps(original))
                if mutate:
                    mutate(facts)
                write_json(self.root / "facts.json", facts)
                with self.assertRaisesRegex(closeout.CloseoutError, message):
                    closeout.prepare({**self.config, **overrides}, dry_run=True)
                self.assertFalse(self.work.exists())
        write_json(self.root / "facts.json", original)
        # 终态事实与材料同为另一版本时，签发端按当前 production_active 拒绝。
        other = json.loads(json.dumps(original))
        other["target"]["version"] = "9.9.7"
        with self.assertRaisesRegex(closeout.CloseoutError, "当前 production_active 版本"):
            closeout.terminal_for("claude")(self.repo, other, TERMINAL_RELATIVE)
        write_json(self.root / "material.json", {**self.material, "client": None})
        with self.assertRaisesRegex(closeout.CloseoutError, "不是本客户端"):
            closeout.prepare(self.config, dry_run=True)
        with self.assertRaisesRegex(closeout.CloseoutError, "字段不完整"):
            closeout.validate_config({**self.config, "schema_version": "other-closeout/v1"})


class SourceCascadeTests(unittest.TestCase):
    def paired_inputs(self, repo: Path, guide: str) -> tuple[str, str, str]:
        """当前指南、场景、期望与冻结表成对生成，版本号只用于 regression_fixture。"""

        capture = "tools/official_client_capture/"
        scenario_name = capture + "claude_upgrade_scenarios_9_9_9.json"
        expectations_name = capture + "claude_rule_expectations_9_9_8.json"
        checker_name = capture + "claude_rule_assertion.py"
        (repo / "docs").mkdir(exist_ok=True)
        (repo / CLAUDE_GUIDE).write_text(guide)
        scenarios = {"source_spec": {"path": CLAUDE_GUIDE, "fragment": "第二部分", "sha256": closeout.part2_sha(guide, heading=CLAUDE_PART2)}}
        write_json(repo / scenario_name, scenarios)
        expectations = write_json(repo / expectations_name, {
            "claude_version": "9.9.8", "generated_from": {"scenarios": {"sha256": claude_closeout.canonical_sha256(scenarios)}}
        })
        (repo / checker_name).write_text('FROZEN_PROFILE_SHA256S = {"9.9.8": "' + closeout.sha(expectations.read_bytes()) + '"}\n')
        return scenario_name, expectations_name, checker_name

    def test_paired_fixture_cascade_is_three_unique_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            init_repo(repo)
            before = claude_guide("成对基线")
            scenario_name, expectations_name, checker_name = self.paired_inputs(repo, before)
            commit_all(repo, "成对基线")
            original_bytes = {name: (repo / name).read_bytes() for name in (CLAUDE_GUIDE, scenario_name, expectations_name, checker_name)}
            changed = before.replace(CLAUDE_PART2, CLAUDE_PART2 + "\n\n测试新增说明。", 1)
            updates = closeout.source_updates(repo, changed, client="claude")
            self.assertEqual(sorted(updates), sorted(original_bytes))
            scenarios = json.loads(updates[scenario_name]["text"])
            expected = json.loads(original_bytes[scenario_name])
            expected["source_spec"]["sha256"] = closeout.part2_sha(changed, heading=CLAUDE_PART2)
            self.assertEqual(scenarios, expected)
            expectations_text = updates[expectations_name]["text"]
            expected = json.loads(original_bytes[expectations_name])
            expected["generated_from"]["scenarios"]["sha256"] = claude_closeout.canonical_sha256(scenarios)
            self.assertEqual(json.loads(expectations_text), expected)
            self.assertEqual(updates[checker_name]["text"], original_bytes[checker_name].decode().replace(
                closeout.sha(original_bytes[expectations_name]), closeout.sha(expectations_text.encode())))
            self.assertEqual({name: (repo / name).read_bytes() for name in original_bytes}, original_bytes)
            # 同一份指南（第二部分不变）只更新指南本身。
            self.assertEqual(list(closeout.source_updates(repo, before + "\n", client="claude")), [CLAUDE_GUIDE])

    def test_historical_scenario_keeps_original_bytes_and_rejects_uncommitted_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            init_repo(repo)
            (repo / "docs").mkdir()
            historical_guide = claude_guide("历史章节")
            (repo / CLAUDE_GUIDE).write_text(historical_guide)
            historical_name = "tools/official_client_capture/claude_upgrade_scenarios_9_9_8.json"
            historical = write_json(repo / historical_name, {
                "source_spec": {"path": CLAUDE_GUIDE, "fragment": "第二部分", "sha256": closeout.part2_sha(historical_guide, heading=CLAUDE_PART2)}
            })
            commit_all(repo, "历史章节及场景")
            current_guide = claude_guide("当前章节")
            current_names = self.paired_inputs(repo, current_guide)
            commit_all(repo, "当前成对输入")
            original_bytes = historical.read_bytes()
            changed = claude_guide("本轮新增说明")
            updates = closeout.source_updates(repo, changed, client="claude")
            self.assertEqual(sorted(updates), sorted((CLAUDE_GUIDE, *current_names)))
            self.assertNotIn(historical_name, updates)
            self.assertEqual(historical.read_bytes(), original_bytes)
            # 即使旧章节能由 Git 重放，历史清单与 HEAD 不同也必须拒绝，不能顺便重写历史链。
            historical.write_bytes(original_bytes + b"\n")
            before = {name: (repo / name).read_bytes() for name in (CLAUDE_GUIDE, *current_names, historical_name)}
            with self.assertRaisesRegex(closeout.CloseoutError, "无历史绑定"):
                closeout.source_updates(repo, changed, client="claude")
            self.assertEqual({name: (repo / name).read_bytes() for name in before}, before)

    def test_cascade_fails_closed_when_a_digest_cannot_be_uniquely_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            init_repo(repo)
            guide = claude_guide("基线")
            (repo / "docs").mkdir()
            (repo / CLAUDE_GUIDE).write_text(guide)
            capture = repo / "tools/official_client_capture"
            scenarios = {"source_spec": {"path": CLAUDE_GUIDE, "fragment": "第二部分", "sha256": closeout.part2_sha(guide, heading=CLAUDE_PART2)}}
            write_json(capture / "claude_upgrade_scenarios_9_9_9.json", scenarios)
            canonical = claude_closeout.canonical_sha256(scenarios)
            expectations = write_json(capture / "claude_rule_expectations_9_9_8.json",
                                      {"claude_version": "9.9.8", "generated_from": {"scenarios": {"sha256": canonical}}})
            checker = capture / "claude_rule_assertion.py"
            checker.write_text('FROZEN_PROFILE_SHA256S = {"9.9.8": "' + "0" * 64 + '"}\n')
            commit_all(repo, "基线")
            changed = claude_guide("改过")
            with self.assertRaisesRegex(closeout.CloseoutError, "原摘要不符"):
                closeout.source_updates(repo, changed, client="claude")
            checker.write_text('FROZEN_PROFILE_SHA256S = {"9.9.8": "' + closeout.sha(expectations.read_bytes()) + '"}\n')
            write_json(expectations, {"claude_version": "9.9.8", "note": canonical, "generated_from": {"scenarios": {"sha256": canonical}}})
            with self.assertRaisesRegex(closeout.CloseoutError, "不能唯一定位"):
                closeout.source_updates(repo, changed, client="claude")
            # 场景清单绑定的既不是当前章节、也不是历史章节：拒绝批量覆盖。
            write_json(capture / "claude_upgrade_scenarios_9_9_9.json", {"source_spec": {**scenarios["source_spec"], "sha256": "f" * 64}})
            with self.assertRaisesRegex(closeout.CloseoutError, "无历史绑定"):
                closeout.source_updates(repo, changed, client="claude")


# ---------------------------------------------------------------------------
# 材料读侧：独立解释器 + 合成受管根
# ---------------------------------------------------------------------------

STUB_UPGRADE = '''import json
from pathlib import Path


def _read(path):
    return json.loads(Path(path).read_text())


def load_campaign_manifest(campaign):
    return _read(Path(campaign) / "campaign.json")


def _load_stage_result(campaign, stage, candidate_id=None, *, _verified_campaign_manifest=None):
    assert _verified_campaign_manifest == load_campaign_manifest(campaign)
    return _read(Path(campaign) / "stages" / {"classify": "classify.json", "capture-official": "official.json"}[stage])


def _effective_classification_view(campaign, candidate, classification):
    assert candidate == "candidate-a"
    return {**classification, "assertion_profile_manifest": _read(Path(campaign) / "stages" / "effective.json")}
'''

STUB_CHECKER = '''import json
from pathlib import Path


def bind_capture_manifest(capture, bindings, *, capture_id):
    return {"capture_id": capture_id, "artifacts": [dict(item, bound=item["path"] in bindings) for item in capture["artifacts"]]}


def observations_from_manifest(manifest, evidence_root):
    observations = []
    for item in manifest["artifacts"]:
        assert item["bound"], item
        observations.extend(json.loads((Path(evidence_root) / item["path"]).read_text()))
    return observations


def _select_observations(observations, selector, default_scenarios):
    return [item for item in observations if item["probe"] == selector["probe"] and item["scenario"] in default_scenarios]
'''


class MaterialReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name).resolve() / "data"
        for package, files in (("official_client_capture", {"claude_upgrade.py": STUB_UPGRADE, "claude_rule_assertion.py": STUB_CHECKER}),
                               ("official_client_control", {"contracts.py": "# 合成受管控制根\n"})):
            root = self.data / "tools" / package
            root.mkdir(parents=True)
            (root / "__init__.py").write_text("")
            for name, text in files.items():
                (root / name).write_text(text)
        self.campaign = self.campaign_dir("c-final")
        self.write_captured(self.campaign)

    def campaign_dir(self, campaign_id: str) -> Path:
        campaign = self.data / "evidence/campaigns" / campaign_id
        write_json(campaign / "campaign.json", {"campaign_id": campaign_id, "target_version": "9.9.9"})
        rows = [{"spec_id": "SPEC-FIXTURE-001", "migration_decision": "change", "rationale": "按目标证据改写", "evidence_item_ids": ["EVIDENCE-p1"]},
                {"spec_id": "SPEC-FIXTURE-002", "migration_decision": "delete", "rationale": "目标版本退休", "evidence_item_ids": []},
                {"spec_id": "SPEC-FIXTURE-003", "migration_decision": "add", "rationale": "目标新增", "evidence_item_ids": ["EVIDENCE-p2"]}]
        migration = write_json(campaign / "classification/approved/rule-migration.json", {
            "schema_version": "claude-upgrade-rule-migration-draft/v1", "baseline_spec_ids": ["SPEC-FIXTURE-001", "SPEC-FIXTURE-002"],
            "target_spec_ids": ["SPEC-FIXTURE-001", "SPEC-FIXTURE-003"], "rules": rows})
        target = write_json(campaign / "classification/approved/target-rules.json", {"rules": ["SPEC-FIXTURE-001", "SPEC-FIXTURE-003"]})
        profile = write_json(campaign / "assertions/approved-assertion-profile.json", {"rules": [
            {"rule_id": "SPEC-FIXTURE-001", "scenario_ids": ["s1"], "checks": [{"id": "a", "select": {"probe": "p1"}, "assertion": {"operator": "count"}}]},
            {"rule_id": "SPEC-FIXTURE-003", "scenario_ids": ["s1", "s2"], "checks": [{"id": "b", "select": {"probe": "p2"}, "assertion": {"operator": "count"}}]}]})

        def bound(path: Path) -> dict:
            return {"path": path.relative_to(campaign).as_posix(), "sha256": closeout.sha(path.read_bytes()), "bytes": path.stat().st_size}

        write_json(campaign / "stages/classify.json", {"status": "complete", "approved": {"rule_migration": bound(migration), "target_rules": bound(target)}})
        write_json(campaign / "stages/effective.json", {key: bound(profile)[key] for key in ("path", "sha256")})
        return campaign

    def write_captured(self, campaign: Path) -> None:
        evidence = write_json(campaign / "runs/probe.json", [
            {"probe": "p1", "scenario": "s1"}, {"probe": "p1", "scenario": "s1"}, {"probe": "p1", "scenario": "s9"},
            {"probe": "p2", "scenario": "s2"}])
        relative = evidence.relative_to(self.data).as_posix()
        inventory = write_json(campaign / "official/seal/evidence-manifest.json", {"entries": [
            {"path": relative, "sha256": closeout.sha(evidence.read_bytes()), "bytes": evidence.stat().st_size}]})
        capture = write_json(campaign / "official/seal/drafts/capture-manifest.json", {"artifacts": [{"path": relative}]})

        def bound(path: Path) -> dict:
            return {"path": path.relative_to(campaign).as_posix(), "sha256": closeout.sha(path.read_bytes())}

        draft = write_json(campaign / "official/seal/seal-draft.json", {"attempt_id": "attempt-1", "drafts": {"capture_manifest": bound(capture)}})
        write_json(campaign / "stages/official.json", {"status": "complete", "mode": "captured",
                                                       "seal": {"evidence_manifest": bound(inventory), "seal_draft": bound(draft)},
                                                       "store": {"evidence_fact_ref": {"kind": "fact", "sha256": "e" * 64}}})

    def test_independent_reader_replays_vc2_and_vc1_from_the_managed_roots(self) -> None:
        material = closeout.collect_guide_material(self.campaign, "candidate-a", client="claude")
        self.assertEqual(material["client"], "claude")
        self.assertEqual(material["reader_root"], str(self.data))
        self.assertEqual((material["campaign_id"], material["official_evidence_campaign_id"], material["target_version"]),
                         ("c-final", "c-final", "9.9.9"))
        self.assertEqual(material["official_observation_counts"], {"SPEC-FIXTURE-001": {"a": 2}, "SPEC-FIXTURE-003": {"b": 1}})
        self.assertEqual([(row["target_rule"], row["baseline_rule"], row["classification"]) for row in material["migration"]["entries"]],
                         [("SPEC-FIXTURE-001", "SPEC-FIXTURE-001", "change"), (None, "SPEC-FIXTURE-002", "delete"),
                          ("SPEC-FIXTURE-003", None, "add")])
        self.assertEqual(sorted(material["references"]), ["assertion_profile_manifest", "rule_migration", "target_rules"])
        self.assertTrue(any("official_client_control" in path for path in material["reader_files"]))
        # 同一封存链重放结果逐字相同（prepare／publish 用它核对材料）。
        self.assertEqual(closeout.collect_guide_material(self.campaign, "candidate-a", client="claude"), material)
        # 指南草稿按 Claude 第二部分标题生成。
        original = claude_guide("说明").replace("### SPEC-FIXTURE-001 规则\n旧说明\n", "### SPEC-FIXTURE-001 规则\n旧说明\n\n### SPEC-FIXTURE-002 旧规则\n旧\n")
        draft = closeout.guide_draft(original, material, {"rules": []}, heading=CLAUDE_PART2)
        self.assertEqual(draft["affected_rule_ids"], ["SPEC-FIXTURE-001", "SPEC-FIXTURE-002", "SPEC-FIXTURE-003"])
        self.assertIn("匹配 2 条", draft["guide_text"])
        self.assertIn("迁移分类**：delete", draft["guide_text"])
        self.assertTrue(draft["guide_text"].endswith("# 第三部分 实现\n\n结束\n"))

    def test_predecessor_reuse_reads_the_predecessor_official_evidence(self) -> None:
        reuse = self.campaign_dir("c-reuse")
        write_json(reuse / "stages/official.json", {"status": "complete", "mode": "predecessor_reuse", "seal": None,
                                                    "predecessor_reuse": {"predecessor": {"campaign_id": "c-final"},
                                                                          "source_evidence_fact_ref": {"kind": "fact", "sha256": "e" * 64}}})
        material = closeout.collect_guide_material(reuse, "candidate-a", client="claude")
        self.assertEqual((material["campaign_id"], material["official_evidence_campaign_id"]), ("c-reuse", "c-final"))
        self.assertEqual(material["official_observation_counts"]["SPEC-FIXTURE-001"], {"a": 2})
        write_json(reuse / "stages/official.json", {"status": "complete", "mode": "predecessor_reuse", "seal": None,
                                                    "predecessor_reuse": {"predecessor": {"campaign_id": "c-final"},
                                                                          "source_evidence_fact_ref": {"kind": "fact", "sha256": "d" * 64}}})
        with self.assertRaisesRegex(closeout.CloseoutError, "不是只读复用来源"):
            closeout.collect_guide_material(reuse, "candidate-a", client="claude")

    def test_drifted_approval_or_evidence_fails_closed(self) -> None:
        migration = self.campaign / "classification/approved/rule-migration.json"
        migration.write_text(migration.read_text().replace("按目标证据改写", "被改写"))
        with self.assertRaisesRegex(closeout.CloseoutError, "摘要漂移"):
            closeout.collect_guide_material(self.campaign, "candidate-a", client="claude")
        with self.assertRaisesRegex(closeout.CloseoutError, "受管根"):
            closeout.collect_guide_material(self.data / "elsewhere", "candidate-a", client="claude")


if __name__ == "__main__":
    unittest.main()
