"""入口门禁（E2-04，``tools/ci/entry_gates.py``）：make 子检查并行、门禁清单生成、一次运行的记录导出。

* ``make-checks``：子检查各一个单元并行执行，一项失败其余照跑，失败项附日志尾部；
* ``plan``：三种组合（preflight＝make test、full-gates、entry）的门禁项构成，测试树单元套隔离前缀、pre-A3 不套，
  macOS 专用的部署脚本测试在 Linux 上记为不执行，生成的清单能被执行器的闭合校验接受；
* ``export``：门禁记录与 write_gate_json 同一组字段，P0 证据与手写 P0 脚本同一形状并能照 0.159.2 轮的组装方式
  签出 P0 收据（finalize＋replay），承接了单元时两份 P0 证据写 v2（E3-03，重验见 test_codex_upgrade_p0_manifest_evidence），
  预跑与全量门禁记录保持原形状。

全部在临时目录里用小 Makefile、合成 CI 定义与合成执行器汇总驱动，不跑真实门禁。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.ci import entry_gates as eg
from tools.ci import entry_steps as es
from tools.ci import unit_executor as ue
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt

REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRY_GATES = REPO_ROOT / "tools" / "ci" / "entry_gates.py"
LAUNCHER = ["unshare", "-m", "--propagation", "private", "bash", "-c", "mount -t tmpfs tmpfs /root/oauth-capture && exec \"$@\"", "entry-gates"]
CI_WORKFLOW = """jobs:
  shell:
    steps:
      - name: Check deploy scripts
        run: |
          /bin/bash -n deploy/apple-container.sh
          /bin/bash deploy/tests/apple-container-test.sh
          /bin/sh deploy/tests/docker-compose-security-test.sh
  test:
    steps:
      - name: Check Docker Compose simple mode environment
        run: /bin/sh deploy/tests/docker-compose-simple-mode-env-test.sh
      - name: Not a deploy test
        run: echo hi; /bin/sh deploy/x.sh
"""


def _tree(root: Path, checks: list[str]) -> Path:
    """最小测试树：Makefile 只有子检查清单，CI 定义里有部署脚本测试，采集工具测试目录存在。"""

    tree = root / "tree"
    (tree / ".github" / "workflows").mkdir(parents=True)
    (tree / ".github" / "workflows" / "backend-ci.yml").write_text(CI_WORKFLOW, encoding="utf-8")
    (tree / "tools" / "official_client_capture" / "tests").mkdir(parents=True)
    (tree / "Makefile").write_text(f"print-egress-spec-checks:\n\t@echo {' '.join(checks)}\n", encoding="utf-8")
    return tree.resolve()


class EntryGatesMakeChecksTests(unittest.TestCase):
    def test_every_check_runs_and_failures_are_listed_with_log_tails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "Makefile").write_text(textwrap.dedent("""\
                ok-a:
                \t@echo a > a.marker
                broken:
                \t@echo 断言细节：第 7 项不一致; exit 3
                ok-b:
                \t@echo b > b.marker
                """), encoding="utf-8")
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_STATE_DIR": str(root / "state"),
                           "UNIT_EXECUTOR_OUT_DIR": str(root / "out")}
            completed = subprocess.run([sys.executable, str(ENTRY_GATES), "make-checks", "--name", "demo", "ok-a", "broken", "ok-b"],
                                       cwd=root, capture_output=True, text=True, timeout=300, env=environment)
            self.assertEqual(completed.returncode, 1, completed.stderr[-2000:])
            self.assertTrue((root / "a.marker").is_file() and (root / "b.marker").is_file(), "一项失败其余照跑")
            self.assertIn("子检查未通过：make broken（退出码 2）", completed.stderr)
            self.assertIn("断言细节：第 7 项不一致", completed.stderr, "失败项附日志尾部")
            self.assertIn("demo 未通过：broken", completed.stderr)
            summary = json.loads((root / "out" / "demo" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(row["unit_id"] for row in summary["units"]), ["egress-spec:broken", "egress-spec:ok-a", "egress-spec:ok-b"])

    def test_duplicate_targets_are_rejected(self) -> None:
        self.assertEqual(eg.make_checks("demo", ["a", "a"]), 2)

    def test_make_test_does_not_list_a_check_that_check_egress_spec_already_runs(self) -> None:
        """子检查跑在执行器另起的 make 进程里，不和 make test 的先决目标去重：test 目标再单列哪一项，那一项就执行两次。"""

        test_line = re.search(r"^test:\s*(.*)$", (REPO_ROOT / "Makefile").read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(test_line)
        assert test_line is not None
        prerequisites = test_line.group(1).split()
        self.assertIn("check-egress-spec", prerequisites)
        self.assertEqual(sorted(set(prerequisites) & set(eg.egress_spec_checks(REPO_ROOT))), [])


class EntryGatesPlanTests(unittest.TestCase):
    CHECKS = ["check-egress-spec-local-source", "test-official-client-control", "egress-spec-go-test", "egress-spec-version-leak"]

    def _pre_a3_units(self, root: Path, data_root: Path) -> Path:
        path = root / "pre-a3-units.json"
        path.write_text(json.dumps({"schema_version": eg.COMMANDS_SCHEMA, "staging_root": str(data_root / "staging" / "x"), "units": [
            {"unit_id": f"pre-a3:{name}", "argv": ["python3", "-m", "tools.x", "run-scenario", "--name", name], "cwd": str(data_root),
             "cores": 1, "memory_mb": 1024, "timeout_seconds": 900, "weight": 30.0, "env": {"OWN": "1"}}
            for name in ("alpha", "beta")]}), encoding="utf-8")
        return path

    def test_profiles_compose_make_test_full_gates_and_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = _tree(root, self.CHECKS)
            preflight = eg.plan_gates(tree, profile="preflight", launcher=LAUNCHER, typescript_module="/ts.js", platform="linux")
            self.assertEqual([gate["gate_id"] for gate in preflight["gates"]], list(eg.MAKE_TEST_GATES))
            gates = {gate["gate_id"]: gate for gate in preflight["gates"]}
            self.assertEqual(gates["test-official-client-control"]["units"], ["egress-spec:test-official-client-control"],
                             "make test 的一项与 check-egress-spec 共用同一个单元，只执行一次")
            self.assertEqual(gates["check-egress-spec"]["units"], [f"egress-spec:{name}" for name in self.CHECKS])
            self.assertEqual(gates["test-capture-tools"], {"gate_id": "test-capture-tools", "units": ["capture:prerequisites"],
                                                           "test_groups": [eg.CAPTURE_GROUP]})
            group = preflight["test_groups"][0]
            self.assertEqual((group["start"], group["pattern"], group["env"], group["launcher"]),
                             ("tools/official_client_capture/tests", "test_*.py", {"CLAUDE_AST_TYPESCRIPT_MODULE": "/ts.js"}, LAUNCHER))
            units = {unit["unit_id"]: unit for unit in preflight["units"]}
            self.assertTrue(all(unit["argv"][:len(LAUNCHER)] == LAUNCHER for unit in units.values()), "测试树单元都套隔离前缀")
            self.assertEqual(units["backend:go-test"]["argv"][len(LAUNCHER):], ["go", "test", "./...", "-count=1"])
            self.assertEqual(units["backend:go-test"]["cwd"], str(tree / "backend"))
            self.assertEqual(units["egress-spec:egress-spec-go-test"]["argv"][len(LAUNCHER):], ["make", "--no-print-directory", "egress-spec-go-test"])

            full = eg.plan_gates(tree, profile="full-gates", launcher=LAUNCHER, platform="linux")
            self.assertEqual([gate["gate_id"] for gate in full["gates"]], list(eg.MAKE_TEST_GATES + eg.FULL_GATES_EXTRA))
            deploy = next(gate for gate in full["gates"] if gate["gate_id"] == "deploy-scripts")
            self.assertEqual(deploy["units"], ["deploy:apple-container.sh-syntax", "deploy:tests-docker-compose-security-test.sh",
                                               "deploy:tests-docker-compose-simple-mode-env-test.sh"])
            self.assertEqual([item["command"] for item in deploy["not_executed"]], [["/bin/bash", "deploy/tests/apple-container-test.sh"]])
            self.assertIn("BSD stat", deploy["not_executed"][0]["reason"])
            integration = next(unit for unit in full["units"] if unit["unit_id"] == "backend:integration")
            self.assertEqual(integration["env"], {"GOMAXPROCS": "2", "CI": "true"}, "没有 Docker 时失败而不是静默跳过")
            go_test = next(unit for unit in full["units"] if unit["unit_id"] == "backend:go-test")
            self.assertEqual((go_test["cores"], go_test["env"]), (0.7, {"GOMAXPROCS": "2"}), "按实测 CPU 占比排程，内部并行度另给")
            on_mac = eg.plan_gates(tree, profile="full-gates", launcher=[], platform="darwin")
            self.assertEqual(next(g for g in on_mac["gates"] if g["gate_id"] == "deploy-scripts")["not_executed"], [])

            data_root = root / "data"
            entry = eg.plan_gates(tree, profile="entry", launcher=LAUNCHER, pre_a3_units=self._pre_a3_units(root, data_root),
                                  pre_a3_env={"PYTHONPYCACHEPREFIX": "/prod-pyc"}, platform="linux")
            self.assertEqual(entry["gates"][-1], {"gate_id": "pre-a3", "units": ["pre-a3:alpha", "pre-a3:beta"]})
            alpha = next(unit for unit in entry["units"] if unit["unit_id"] == "pre-a3:alpha")
            self.assertEqual(alpha["argv"][0], "python3", "pre-A3 在数据根的生产布局里运行，不套隔离")
            self.assertEqual((alpha["cores"], alpha["memory_mb"], alpha["weight"]), (0.5, 768, 30.0), "没有实测的场景按 0.5 核、沿用认证模块的预计秒数")
            self.assertEqual((alpha["cwd"], alpha["env"]), (str(data_root), {"OWN": "1", "PYTHONPYCACHEPREFIX": "/prod-pyc"}))
            for manifest in (preflight, full, entry):
                path = root / f"{manifest['profile']}.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                ue.load_gates_manifest(path, machine_cores=4)  # 执行器的闭合校验接受

    def test_plan_rejects_inconsistent_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = _tree(root, self.CHECKS)
            pre_a3 = self._pre_a3_units(root, root / "data")
            with self.assertRaisesRegex(ValueError, "pre-A3"):
                eg.plan_gates(tree, profile="entry", launcher=[])
            with self.assertRaisesRegex(ValueError, "pre-A3"):
                eg.plan_gates(tree, profile="preflight", launcher=[], pre_a3_units=pre_a3)
            other = _tree(root / "other", ["egress-spec-go-test"])
            with self.assertRaisesRegex(ValueError, "test-official-client-control"):
                eg.plan_gates(other, profile="preflight", launcher=[])
            (tree / ".github" / "workflows" / "backend-ci.yml").write_text("jobs: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "部署脚本测试"):
                eg.plan_gates(tree, profile="full-gates", launcher=[])
        self.assertEqual([match.group(1) for match in eg.DEPLOY_TEST_LINE.finditer(CI_WORKFLOW)][-1],
                         "/bin/sh deploy/tests/docker-compose-simple-mode-env-test.sh", "一行里混了别的命令的不取")

    def test_new_form_pre_a3_units_declare_scenario_inputs_and_are_inheritable(self) -> None:
        """E3-02：认证模块 plan --staging-parent 给的新形式场景单元（带 scenario_test_file）按场景声明输入、可以承接：受管树
        （不含测试）、夹具、文档、部署脚本副本、场景测试模块（执行器展开闭包）与数据根算好的明细，不加 HEAD。pre-a3 组合
        只有场景单元。旧形式（不带 scenario_test_file）仍不可承接。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = _tree(root, self.CHECKS)
            data_root = root / "data"
            path = root / "pre-a3-v2.json"
            path.write_text(json.dumps({"schema_version": eg.COMMANDS_SCHEMA, "staging_parent": str(data_root / "staging" / "pre-a3-scenarios"),
                                        "units": [{"unit_id": f"pre-a3:{name}", "argv": ["python3", "-m", "tools.x", "run-scenario", "--name", name],
                                                   "cwd": str(data_root), "cores": 1, "memory_mb": 1024, "timeout_seconds": 900, "weight": 30.0,
                                                   "scenario_test_file": f"tools/official_client_capture/tests/test_{name}.py"}
                                                  for name in ("alpha", "beta")]}), encoding="utf-8")
            resolved = [{"category": "docs", "name": "tree:{D}/docs/egress/maintenance#pycache", "sha256": "c" * 64, "detail": {}}]
            manifest = eg.plan_gates(tree, profile="pre-a3", launcher=LAUNCHER, pre_a3_units=path, pre_a3_data_inputs=resolved, platform="linux")
            self.assertEqual(([gate["gate_id"] for gate in manifest["gates"]], manifest["test_groups"]), (["pre-a3"], []))
            alpha = next(unit for unit in manifest["units"] if unit["unit_id"] == "pre-a3:alpha")
            self.assertNotIn("scenario_test_file", alpha, "清单字段原样交给执行器，不带执行器不认识的字段")
            self.assertTrue(alpha.get("inheritable", True))
            inputs = alpha["inputs"]
            self.assertEqual([item["name"] for item in inputs["ranges"]], ["repo:managed", "repo:tests-fixtures", "repo:docs"])
            self.assertEqual(inputs["files"], [{"category": "managed", "path": "tools/arm64_supervised_deploy.py"}])
            self.assertEqual((inputs["test_modules"], inputs["resolved"]), (["tools/official_client_capture/tests/test_alpha.py"], resolved))
            self.assertNotIn("head", inputs, "场景在数据根运行，读不到 git")
            old = eg.plan_gates(tree, profile="pre-a3", launcher=LAUNCHER, pre_a3_units=self._pre_a3_units(root, data_root), platform="linux")
            self.assertEqual({unit.get("inheritable") for unit in old["units"]}, {False}, "旧形式命令带每次新建的认证根，承接不了")
            saved = root / "pre-a3-manifest.json"
            saved.write_text(json.dumps(manifest), encoding="utf-8")
            ue.load_gates_manifest(saved, machine_cores=4)  # 执行器的闭合校验接受

    def test_plan_declares_inputs_scheduling_environment_and_pre_a3_not_inheritable(self) -> None:
        """E3-01：命令单元按 E2-05 从宽声明输入（整个仓库四段＋HEAD），子检查另加历史源码树；清单带额度表与环境事实；
        pre-A3 场景标为不可承接；额度表与组合无关（组合不同不改调度策略版本）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = _tree(root, self.CHECKS)
            environment = [{"category": "environment", "name": "env:go", "sha256": "a" * 64, "detail": {"value": "go1.27.1"}}]
            historical = {"category": "environment", "name": "tree:/hist#pycache", "sha256": "b" * 64, "detail": {}}
            full = eg.plan_gates(tree, profile="full-gates", launcher=LAUNCHER, platform="linux", environment=environment,
                                 egress_extra_inputs=[historical])
            self.assertEqual((full["scheduling"], full["environment"]), (eg.scheduling_table(), environment))
            for unit in full["units"]:
                inputs = unit["inputs"]
                self.assertEqual(([item["name"] for item in inputs["ranges"]], inputs["head"]),
                                 (["repo:managed", "repo:tests", "repo:docs", "repo:rest"], True), unit["unit_id"])
                expected = [historical] if unit["unit_id"].startswith(eg.EGRESS_SPEC_UNIT_PREFIX) else []
                self.assertEqual(inputs["resolved"], expected, unit["unit_id"])
            managed = full["units"][0]["inputs"]["ranges"][0]
            self.assertEqual((managed["include"], managed["exclude"]),
                             (["tools/official_client_capture/"], ["tools/official_client_capture/tests/"]))
            entry = eg.plan_gates(tree, profile="entry", launcher=LAUNCHER, pre_a3_units=self._pre_a3_units(root, root / "data"),
                                  platform="linux")
            alpha = next(unit for unit in entry["units"] if unit["unit_id"] == "pre-a3:alpha")
            self.assertEqual((alpha["inheritable"], "inputs" in alpha), (False, False))
            self.assertIn("E3-02", alpha["not_inheritable_reason"])
            self.assertEqual(entry["scheduling"], full["scheduling"], "额度表与组合无关")
            config = ue.load_config(REPO_ROOT / ue.DEFAULT_CONFIG)
            self.assertEqual(ue.gates_policy_digest(config, {}, {}, 8, full["scheduling"]), ue.gates_policy_digest(config, {}, {}, 8, entry["scheduling"]))
            changed = json.loads(json.dumps(full["scheduling"]))
            changed["go_quota"]["cores"] = 1.0
            self.assertNotEqual(ue.gates_policy_digest(config, {}, {}, 8, changed), ue.gates_policy_digest(config, {}, {}, 8, full["scheduling"]),
                                "改额度表：调度策略版本变（全部不承接）")
            path = root / "entry.json"
            path.write_text(json.dumps(entry), encoding="utf-8")
            ue.load_gates_manifest(path, machine_cores=4)


RUN_ID = "20261001t100000z-1-abcdef"


def _summary_for(manifest: dict, *, fail_units: set[str] = frozenset(), skipped: int = 2,
                 inherited: frozenset[str] = frozenset(), mode: str | None = None, store: str = "/store") -> dict:
    """照执行器 summarize_gates 的形状合成一次运行的汇总（测试组只有一个单元 test_x）。"""

    stamp = "2026-10-01T10:00:00Z", "2026-10-01T10:20:00Z"
    rows = [{"type": "command", "unit_id": unit["unit_id"], "kind": "formal", "passed": unit["unit_id"] not in fail_units,
             "exit_code": 1 if unit["unit_id"] in fail_units else 0, "signal": None, "timed_out": False, "seconds": 5.0,
             "cpu_seconds": 4.0, "max_rss_mb": 50.0, "orphans": 0, "log": f"/logs/{unit['unit_id']}.log", "argv": unit["argv"],
             "cwd": unit["cwd"], "started_at_utc": stamp[0], "completed_at_utc": stamp[1],
             "disposition": "inherited" if unit["unit_id"] in inherited else "executed"} for unit in manifest["units"]]
    by_id = {row["unit_id"]: row for row in rows}
    for group in manifest["test_groups"]:
        rows.append({"type": "test", "group_id": group["group_id"], "unit_id": "test_x", "kind": "formal", "passed": True, "exit_code": 0,
                     "signal": None, "timed_out": False, "seconds": 5.0, "log": "/logs/test_x.log", "tests": 10,
                     "started_at_utc": stamp[0], "completed_at_utc": stamp[1],
                     "disposition": "inherited" if "test_x" in inherited else "executed"})
    groups = {}
    for group in manifest["test_groups"]:
        groups[group["group_id"]] = {
            "status": "passed", "start": group["start"], "pattern": group["pattern"], "expected_tests": 10, "reported_tests": 10,
            "counts": {"passed": 10 - skipped, "failed": 0, "error": 0, "skipped": skipped, "expected_failure": 0, "unexpected_success": 0},
            "full_set": {"missing": [], "duplicated": [], "unexpected": [], "units_not_run": []}, "failed_units": [], "units": ["test_x"],
            "skipped": [{"test_id": f"test_x.T.test_skip_{i}", "reason": "需要 Linux root"} for i in range(skipped)],
        }
    gates = []
    for gate in manifest["gates"]:
        failed = [unit for unit in gate["units"] if not by_id[unit]["passed"]]
        members = list(gate["units"]) + (["test_x"] if gate.get("test_groups") else [])
        gates.append({"gate_id": gate["gate_id"], "status": "failed" if failed else "passed", "units": gate["units"],
                      "test_groups": gate.get("test_groups", []), "failed_units": failed, "not_executed": gate.get("not_executed", []),
                      "inherited_units": sorted(set(members) & set(inherited)),
                      "started_at_utc": stamp[0], "completed_at_utc": stamp[1], "unit_seconds": 5.0 * len(gate["units"])})
    summary = {"schema_version": eg.GATES_SUMMARY_SCHEMA, "status": "failed" if fail_units else "passed", "policy_sha256": "p" * 64,
               "elapsed_seconds": 1200.0, "gates": gates, "test_groups": groups, "units": rows, "units_not_run": [],
               "failed_units": sorted(fail_units), "diagnostic": [], "max_cores_in_use": 4.0}
    if mode is not None:
        summary.update(mode=mode, run_id=RUN_ID,
                       inheritance={"mode_label": "全集通过", "executed": len(rows), "inherited": len(inherited), "inherited_tests": 3},
                       unit_manifest={"path": "/out/executor/unit-manifest.json", "manifest_sha256": "m" * 64, "self_check": "passed",
                                      "problems": []},
                       record_store=store)
    return summary


class EntryGatesPreA3InputsTests(unittest.TestCase):
    def test_runner_module_level_test_imports_are_declared_for_every_scenario(self) -> None:
        """场景运行器（pre-A3 认证模块）模块级导入的测试目录模块每个场景都会读（E3-04 读集审计实测），必须都在
        PRE_A3_RUNNER_FIXTURES 里；运行器以后新增这类导入而忘了登记时这里失败。"""

        import ast

        runner = REPO_ROOT / "tools" / "official_client_capture" / "codex_upgrade_pre_a3_certification.py"
        tree = ast.parse(runner.read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "tools.official_client_capture.tests":
                imported.update(f"tools/official_client_capture/tests/{alias.name}.py" for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tools.official_client_capture.tests."):
                imported.add(node.module.replace(".", "/") + ".py")
        self.assertTrue(imported, "运行器应当在模块级导入了测试夹具（否则这条核对就没有意义了）")
        self.assertEqual(imported, set(eg.PRE_A3_RUNNER_FIXTURES))
        declared = eg.pre_a3_inputs("tools/official_client_capture/tests/test_x.py", [])
        self.assertTrue(set(eg.PRE_A3_RUNNER_FIXTURES) <= {item["path"] for item in declared["files"]})

    def test_data_root_probe_locations_are_resolved_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve()
            entries = eg.pre_a3_data_root_inputs(data)
            probe = [entry for entry in entries if (entry.get("detail") or {}).get("path") == str(data / "staging" / "upgrade-project-ledger")]
            self.assertEqual(len(probe), 1, "项目总账的探测位置声明为数据根输入")
            self.assertEqual(probe[0]["sha256"], "missing", "现在不存在：记为 missing，一旦出现输入摘要就变")
            (data / "staging" / "upgrade-project-ledger").mkdir(parents=True)
            (data / "staging" / "upgrade-project-ledger" / "plan.json").write_text("{}", encoding="utf-8")
            again = [entry for entry in eg.pre_a3_data_root_inputs(data) if entry["name"] == probe[0]["name"]]
            self.assertNotEqual(again[0]["sha256"], "missing")


class EntryGatesPreA3QuotaTests(unittest.TestCase):
    def test_measured_scenarios_get_quota_from_cpu_share_and_the_critical_chain_keeps_a_core(self) -> None:
        self.assertEqual(eg.pre_a3_quota("vc-chain.vc1-recovery-chain"), (1.0, 548.0))
        self.assertEqual(eg.pre_a3_quota("deadline-extension.sigkill-resume"), (0.25, 185.0), "占比 0.03 按最低 0.25 核")
        self.assertEqual(eg.pre_a3_quota("segment-recovery.completed-job-reuse"), (0.75, 204.0), "占比 0.54 向上取到 0.75")
        self.assertEqual(eg.pre_a3_quota("vc-chain.vc1-capture"), (1.0, 212.0))
        self.assertEqual(eg.pre_a3_quota("unknown.scenario"), (0.5, None))


class EntryGatesExportTests(unittest.TestCase):
    def _export(self, root: Path, profile: str, *, fail_units: set[str] = frozenset(), pre_a3: Path | None = None,
                inherited: frozenset[str] = frozenset(), mode: str | None = None, store: Path | None = None,
                tool_identity: object = None, label: str = "") -> tuple[dict, Path]:
        """``store`` 给出时把本次运行的清单（只含汇总登记的摘要）发布进这个记录库。"""

        tree = root / "tree" if (root / "tree").exists() else _tree(root, EntryGatesPlanTests.CHECKS)
        manifest = eg.plan_gates(tree, profile=profile, launcher=[], pre_a3_units=pre_a3, platform="linux")
        manifest_path, summary_path = root / f"{profile}-manifest.json", root / f"{profile}-summary.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        summary = _summary_for(manifest, fail_units=fail_units, inherited=inherited, mode=mode, store=str(store or "/store"))
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        if store is not None:
            (store / "runs").mkdir(parents=True, exist_ok=True)
            (store / "runs" / f"{RUN_ID}.json").write_text(json.dumps({"run_id": RUN_ID, "manifest_sha256": "m" * 64}), encoding="utf-8")
        out = root / f"out-{profile}-{len(fail_units)}-{len(inherited)}{label}"
        entry = eg.export_records(manifest_path, summary_path, out, source={"commit": "c" * 40, "tree_head": "c" * 40, "bundle": "/b"},
                                  subject="entry-gates-x", round_id="r1", tree=str(tree), isolation="unshare -m", host="arm64",
                                  architecture="linux/aarch64", target_version="0.159.3", bytecode_cache="/pyc", tool_identity=tool_identity)
        return entry, out

    def test_records_keep_gate_json_fields_and_composites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry, out = self._export(root, "full-gates")
            self.assertEqual(entry["status"], "passed")
            record = json.loads((out / "logs" / "backend-unit.gate.json").read_text(encoding="utf-8"))
            for key in ("gate_id", "command", "working_directory", "host", "architecture", "started_at_utc", "completed_at_utc",
                        "exit_code", "tree", "tree_head", "isolation"):
                self.assertIn(key, record, "与 lib.sh 的 write_gate_json 同一组字段")
            self.assertEqual((record["command"], record["working_directory"]), (["go", "test", "-tags=unit", "./...", "-count=1"], "backend"))
            # E2-05：每个门禁项带输入明细与摘要；源码提交取测试树的提交，后端测试另列 Go 工具链。
            inputs = {item["name"]: item for item in record["inputs"]}
            self.assertEqual(inputs["source_commit:ENTRY_COMMIT"]["detail"]["value"], "c" * 40)
            self.assertIn("env:go", inputs)
            self.assertEqual(record["inputs_sha256"], es.inputs_sha256(record["inputs"]))
            frontend = json.loads((out / "logs" / "frontend-lint.gate.json").read_text(encoding="utf-8"))
            self.assertIn("env:node", {item["name"] for item in frontend["inputs"]})
            self.assertNotIn("env:go", {item["name"] for item in frontend["inputs"]})
            self.assertEqual({gate["gate_id"]: gate["inputs_sha256"] for gate in entry["gates"]}["backend-unit"], record["inputs_sha256"])
            regression = json.loads((out / "logs" / "full-regression.gate.json").read_text(encoding="utf-8"))
            self.assertEqual((regression["command"], regression["exit_code"], regression["composed_of"]), (["make", "test"], 0, list(eg.MAKE_TEST_GATES)))
            full = json.loads((out / "full-gates-summary.json").read_text(encoding="utf-8"))
            self.assertEqual((full["schema_version"], full["status"], [gate["gate_id"] for gate in full["gates"]]),
                             (eg.FULL_GATES_SCHEMA, "passed", ["full-regression", *eg.FULL_GATES_EXTRA]))
            preflight = json.loads((out / "preflight.json").read_text(encoding="utf-8"))
            self.assertEqual((preflight["schema_version"], preflight["purpose"], preflight["accept_gate_receipt"], preflight["status"],
                              preflight["gate"]["gate_json"]), (eg.PREFLIGHT_SCHEMA, "vc0-preflight", False, "passed", "logs/full-regression.gate.json"))
            # 一个子检查失败：check-egress-spec 与 make test 都不通过，别的门禁项照常。
            failed_entry, failed_out = self._export(root, "full-gates", fail_units={"egress-spec:egress-spec-go-test"})
            statuses = {gate["gate_id"]: gate["status"] for gate in failed_entry["gates"]}
            self.assertEqual((statuses["check-egress-spec"], statuses["backend-unit"]), ("failed", "passed"))
            self.assertEqual(failed_entry["composites"]["full-regression"]["exit_code"], 1)
            regression = json.loads((failed_out / "logs" / "full-regression.gate.json").read_text(encoding="utf-8"))
            self.assertEqual(regression["failed_gates"], ["check-egress-spec"])
            self.assertEqual(json.loads((failed_out / "full-gates-summary.json").read_text(encoding="utf-8"))["status"], "failed")
            evidence = json.loads((failed_out / "p0" / "check-egress-spec.json").read_text(encoding="utf-8"))
            self.assertEqual((evidence["status"], evidence["exit_code"], evidence["failed"], evidence["raw_errors"]),
                             ("failed", 1, 1, ["egress-spec:egress-spec-go-test"]))

    def test_p0_evidence_signs_a_p0_receipt_the_way_the_closeout_assembles_it(self) -> None:
        """一次运行导出的两份 P0 证据，照 0.159.2 轮 VC-0 脚本的组装方式（facts 从证据取数）签出 P0 收据并重放通过。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _entry, out = self._export(root, "preflight")
            p0 = root / "p0-gate"
            (p0 / "evidence").mkdir(parents=True, mode=0o700)
            p0.chmod(0o700)
            for name in ("check-egress-spec", "test-capture-tools"):
                (p0 / "evidence" / f"{name}.json").write_bytes((out / "p0" / f"{name}.json").read_bytes())
            (p0 / "evidence" / "release-certification.json").write_text('{"schema_version": "合成发布认证"}', encoding="utf-8")
            (p0 / "evidence" / "rollback.json").write_text('{"rollback": true}', encoding="utf-8")
            gates = {}
            for name in ("check-egress-spec", "test-capture-tools"):
                evidence = json.loads((p0 / "evidence" / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual((evidence["schema_version"], evidence["gate_id"], evidence["status"], evidence["exit_code"],
                                  evidence["failed"], evidence["unexpected_skip"], evidence["command"]),
                                 (eg.P0_EVIDENCE_SCHEMA, name, "passed", 0, 0, 0, ["make", name]))
                gates[name] = {"gate_id": name, "command": ["make", name], "exit_code": 0, "passed": evidence["passed"], "failed": 0,
                               "approved_skip": evidence["approved_skip"], "unexpected_skip": 0, "kind": "public"}
            capture = json.loads((p0 / "evidence" / "test-capture-tools.json").read_text(encoding="utf-8"))
            self.assertEqual((capture["passed"], capture["approved_skip"], len(capture["skipped"])), (8, 2, 2), "跳过逐条带原因")
            spec = json.loads((p0 / "evidence" / "check-egress-spec.json").read_text(encoding="utf-8"))
            self.assertEqual((spec["passed"], len(spec["checks"])), (len(EntryGatesPlanTests.CHECKS), len(EntryGatesPlanTests.CHECKS)))
            facts = {
                "schema_version": vc_receipt.FACTS_SCHEMA, "kind": "p0_gate",
                "subject": {"upgrade_id": "codex-0157-to-01593-r1", "campaign_id": None, "campaign_purpose": "production_replacement",
                            "baseline_version": "0.157.0", "target_version": "0.159.3", "candidate_id": None, "attempt_id": None},
                "assertions": {"offline_gates": [gates["check-egress-spec"], gates["test-capture-tools"]], "tool_blockers": [],
                               "rollback_ready": True,
                               "release_certification_sha256": hashlib.sha256((p0 / "evidence" / "release-certification.json").read_bytes()).hexdigest()},
                "evidence": [
                    {"path": "evidence/check-egress-spec.json", "role": "check_egress_spec"},
                    {"path": "evidence/release-certification.json", "role": "release_certification"},
                    {"path": "evidence/rollback.json", "role": "rollback"},
                    {"path": "evidence/test-capture-tools.json", "role": "test_capture_tools"},
                ],
            }
            (p0 / "p0-facts.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
            for path in (p0 / "evidence").iterdir():
                path.chmod(0o600)
            vc_receipt.finalize(p0, "p0-facts.json", "p0-receipt.json")
            replayed = vc_receipt.replay(p0, "p0-receipt.json")
            self.assertEqual(replayed["kind"], "p0_gate")

    def test_inherited_units_switch_both_p0_evidence_files_to_v2_or_withhold_both(self) -> None:
        """E3-03：任一门禁项承接了单元（E3-01 全集通过模式）时两份 P0 证据都写 v2——在 v1 字段之外登记运行清单、记录库、
        命令单元与测试组、本次执行／承接的单元数和测试树五摘要；记录库里没有已发布的清单、五摘要算不出来时两份都不写，
        总摘要写明原因。没有承接时照旧写 v1。门禁记录与总摘要带运行模式、承接单元与清单位置。"""

        identity = {name: "a" * 64 for name in eg.IDENTITY_FIELDS}
        inherited = frozenset({"test_x"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry, out = self._export(root, "preflight", inherited=inherited, mode="full-set-pass", tool_identity=lambda tree: identity)
            self.assertEqual((entry["p0_evidence"], sorted(entry["p0_evidence_withheld"]), entry["p0_evidence_schema"]),
                             ({}, ["check-egress-spec", "test-capture-tools"], None), "一项承接，两份同一形状：记录库里没有清单就都不写")
            self.assertIn("运行清单没有发布进记录库", entry["p0_evidence_withheld"]["check-egress-spec"])
            self.assertFalse((out / "p0").exists())
            record = json.loads((out / "logs" / "test-capture-tools.gate.json").read_text(encoding="utf-8"))
            self.assertEqual((record["mode"], record["inherited_units"]), ("full-set-pass", ["test_x"]))
            self.assertEqual((entry["mode"], entry["unit_manifest"]["self_check"], entry["record_store"]), ("full-set-pass", "passed", "/store"))

            store = root / "store"
            entry, out = self._export(root, "preflight", inherited=inherited, mode="full-set-pass", store=store,
                                      tool_identity=lambda tree: identity, label="-v2")
            self.assertEqual((sorted(entry["p0_evidence"]), entry["p0_evidence_withheld"], entry["p0_evidence_schema"]),
                             (["check-egress-spec", "test-capture-tools"], {}, eg.P0_EVIDENCE_SCHEMA_V2))
            capture = json.loads((out / "p0" / "test-capture-tools.json").read_text(encoding="utf-8"))
            spec = json.loads((out / "p0" / "check-egress-spec.json").read_text(encoding="utf-8"))
            reference = {"run_id": RUN_ID, "manifest_sha256": "m" * 64, "mode": "full-set-pass"}
            self.assertEqual((capture["schema_version"], capture["unit_manifest"], capture["record_store"], capture["tool_identity"],
                              capture["units"], capture["test_group"], capture["unit_counts"], capture["passed"], capture["approved_skip"]),
                             (eg.P0_EVIDENCE_SCHEMA_V2, reference, str(store), identity, ["capture:prerequisites"], eg.CAPTURE_GROUP,
                              {"executed": 1, "inherited": 1}, 8, 2))
            checks = EntryGatesPlanTests.CHECKS
            self.assertEqual((spec["schema_version"], spec["test_group"], spec["unit_counts"], spec["units"], spec["unit_manifest"]),
                             (eg.P0_EVIDENCE_SCHEMA_V2, None, {"executed": len(checks), "inherited": 0},
                              [f"{eg.EGRESS_SPEC_UNIT_PREFIX}{check}" for check in checks], reference))
            # 字段与收据模块重验的闭合字段集合一致（v1 字段加 v2 登记项）。
            self.assertEqual(set(capture), set(vc_receipt.P0_V2_COMMON_FIELDS | vc_receipt.P0_V2_GATE_FIELDS["test-capture-tools"]))
            self.assertEqual(set(spec), set(vc_receipt.P0_V2_COMMON_FIELDS | vc_receipt.P0_V2_GATE_FIELDS["check-egress-spec"]))

            def unavailable(tree: str) -> dict:
                raise ValueError("模拟：测试树导入失败")

            entry, out = self._export(root, "preflight", inherited=inherited, mode="full-set-pass", store=store,
                                      tool_identity=unavailable, label="-no-identity")
            self.assertEqual((entry["p0_evidence"], sorted(entry["p0_evidence_withheld"])), ({}, ["check-egress-spec", "test-capture-tools"]))
            self.assertIn("工具五摘要算不出来", entry["p0_evidence_withheld"]["test-capture-tools"])

            entry, out = self._export(root, "preflight", mode="re-execute")
            self.assertEqual((sorted(entry["p0_evidence"]), entry["p0_evidence_withheld"], entry["p0_evidence_schema"]),
                             (["check-egress-spec", "test-capture-tools"], {}, eg.P0_EVIDENCE_SCHEMA))

    def test_tree_identity_uses_the_certification_function(self) -> None:
        """测试树五摘要（v2 P0 证据绑定）与发布认证同一个函数：在本仓库起子进程算的结果等于进程内直接算的。"""

        from tools.official_client_capture import codex_upgrade_policy_certification as certification

        self.assertEqual(eg.IDENTITY_FIELDS, certification.IDENTITY_FIELDS)
        identity = certification.current_identity()
        self.assertEqual(eg.tree_tool_identity(str(REPO_ROOT)), {name: identity[name] for name in certification.IDENTITY_FIELDS})

    def test_pre_a3_sub_summary_holds_only_pre_a3_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pre_a3 = EntryGatesPlanTests()._pre_a3_units(root, root / "data")
            entry, out = self._export(root, "entry", pre_a3=pre_a3)
            summary = json.loads((out / "pre-a3-executor-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema_version"], eg.COMMANDS_SUMMARY_SCHEMA)
            self.assertEqual([row["unit_id"] for row in summary["units"]], ["pre-a3:alpha", "pre-a3:beta"])
            self.assertTrue(all(row["kind"] == "formal" for row in summary["units"]))
            self.assertEqual(entry["pre_a3_executor_summary"], str(out / "pre-a3-executor-summary.json"))


class EntryGatesDriverCopyTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        """入口门禁脚本 entry-gates.sh 用驱动随附的一份（数据根与测试树之外也能跑），与 tools/ci 原件逐字节相同。"""

        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry_gates.py"
        self.assertEqual(driver.read_bytes(), ENTRY_GATES.read_bytes(), "驱动里的 entry_gates.py 与 tools/ci 原件不一致")


if __name__ == "__main__":
    unittest.main()
