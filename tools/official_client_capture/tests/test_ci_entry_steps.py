"""入口步骤的输入摘要与失效判定（E2-05，``tools/ci/entry_steps.py``）。

夹具搭一个最小的数据根、驱动目录、参数和全部产物，命令（docker、go、受管事实子进程等）由替身返回固定值；先把全部
步骤记成通过，再按方案 E2-05 的验收逐类各改一项（参数、测试文件、指南一行、驱动、部署收据、镜像），核对每次只有
依赖它的步骤和它们的下游失效、原因写对；另有记录被改、上次失败、快照、产物被改、Job 演练收据不是通过、Formal 建成后
冻结、账本绝不重建、受管事实算不出来、命令行与退出码。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_steps as es

REPO_ROOT = Path(__file__).resolve().parents[3]
HEADING = "# 第二部分 Codex CLI 客户端规则画像"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _section_sha256(guide: Path) -> str:
    """与受管 ``source_spec_section_sha256`` 同一规则：从第二部分标题到下一个一级标题。"""

    lines = guide.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if line.rstrip("\n") == HEADING)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("# ")), len(lines))
    return hashlib.sha256("".join(lines[start:end]).encode("utf-8")).hexdigest()


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = data = root / "data"
        occ = data / "tools" / "official_client_capture"
        for name in ("tool_identity_policy_v2.json", "codex_upgrade_arm64_environment_receipt.py", "incremental_recovery.py",
                     "codex_upgrade_timing_ledger.py", "codex_upgrade_vc_receipt.py", "codex_upgrade.py"):
            _write(occ / name, f"# {name}\n")
        _write(occ / "fingerprint_proxy" / "proxy.go", "package proxy\n")
        _write(occ / "tests" / "test_x.py", "# 测试\n")
        _write(occ / "tests" / "project_ledger_fixture.py", "# 夹具\n")
        self.scenarios = _write(occ / "codex_upgrade_scenarios_0_160_0.json", json.dumps(
            {"source_spec": {"path": "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "fragment": "第二章"}}))
        _write(data / "tools" / "arm64_supervised_deploy.py", "# 部署\n")
        _write(data / "tools" / "prepare_assertion_bundle.sh", "#!/bin/bash\n")
        self.guide = _write(data / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
                            f"# 第一部分 总则\n第一部分正文\n{HEADING}\n第二部分正文\n# 第三部分 其他\n第三部分正文\n")
        _write(data / "docs" / "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "# 框架\n")
        _write(data / "docs" / "egress" / "maintenance" / "a.json", "{}\n")
        self.recorded = [str(_write(data / "evidence" / "campaigns" / "recorded-campaign" / "campaign.json", "{}\n").parent),
                         str(_write(data / "runs" / "job-a" / "result.json", "{}\n").parent)]
        self.identity = {"policy_version": 7, "policy_sha256": "p" * 64, "wire_producer_sha256": "w" * 64,
                         "evidence_semantics_sha256": "e" * 64, "control_sha256": "c" * 64, "tool_files_sha256": "f" * 64}
        self.deploy(stamp="20261001t000000z")
        self.driver = root / "drv" / "driver"
        names = {item.arg for step in es.STEPS for item in step.inputs if item.kind == "driver"}
        for name in names:
            _write((self.driver / name).resolve() if name.startswith("../") else self.driver / name, f"# 驱动 {name}\n")
        self.images = {name: f"sha256:{index}" * 8 for index, name in enumerate(es.JOB_CONTAINERS)}
        self.image_refs = {"alpine:3.21": "sha256:alpine", "ghcr.io/x/runtime@sha256:aa": "sha256:runtime"}
        self.facts_fail = False
        products = root / "products"
        _write(root / "prev-policy.json", "{}\n")
        _write(root / "hist" / "main.go", "package main\n")
        _write(root / "histtree" / "frontend" / "pnpm-lock.yaml", "lock\n")
        _write(root / "histtree" / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js", "ts\n")
        _write(root / "src-base" / "a.rs", "fn a() {}\n")
        _write(root / "src-target" / "a.rs", "fn a() {}\n")
        _write(root / "pkg.tar.gz", "pkg\n")
        _write(root / "active.json", "{}\n")
        _write(root / "compose" / "docker-compose.yml", "services: {}\n")
        _write(root / "codex-bin", "bin\n")
        self.params = {
            "D": str(data), "UP": "u01600-r1", "NEW": "c0160-formal-r1", "ROUND": "r1", "STAMP": "20261002t000000z",
            "BASELINE_VERSION": "0.157.0", "TARGET_VERSION": "0.160.0", "EVIDENCE_DECISION": "recapture",
            "STAGE_BUDGETS": "VC-0=165", "PROJECT_DEADLINE_UTC": "2026-10-10T15:59:00Z", "MIN_FREE_GIB": "40",
            "PREVIOUS_POLICY": str(root / "prev-policy.json"),
            "POLICY_COMPAT_RECEIPT": str(_write(products / "compat.json", "{}\n")),
            "POLICY_ACTIVATION": str(_write(products / "activation.json", json.dumps({"policy_sha256": "p" * 64}))),
            "PRE_A3_CERTIFICATION": str(_write(products / "pre-a3.json", "{}\n")),
            "RELEASE_CERTIFICATION": str(_write(products / "release.json", "{}\n")),
            "ENTRY_COMMIT": "a" * 40, "HISTORY_TEST_TREE": str(root / "histtree"),
            "HISTORICAL_SOURCE_ROOT": str(root / "hist"), "SCENARIOS_JSON": str(self.scenarios),
            "BASELINE_SOURCE": str(root / "src-base"), "TARGET_SOURCE": str(root / "src-target"),
            "ACTIVE_PROFILE": str(root / "active.json"), "CODEX_BIN": str(root / "codex-bin"), "CODEX_BIN_SHA256": "b" * 64,
            "TARGET_PACKAGE": str(root / "pkg.tar.gz"), "OFFICIAL_ASSET_SHA256": "d" * 64,
            "TARGET_CODE_MODE_HOST_SHA256": "h" * 64, "CAPTURE_RUNTIME_IMAGE": "ghcr.io/x/runtime@sha256:aa",
            "MAIN_MODEL": "gpt-5.6", "LITE_MODEL": "gpt-5.6-mini", "CODEX_ACCOUNT_ID": "90", "API_KEY_ID": "7",
            "COMPOSE_DIR": str(root / "compose"), "CAMPAIGN_PREFIX": "c0160",
        }
        _write(data / "control" / "u01600-r1-timing-ledger" / "ledger.json", "{}\n")
        _write(data / "audit" / "zero-request-smoke-20261002t000000z.json", "{}\n")
        self.products = {
            "entry-gates": {name: str(_write(products / f"entry-{name}.json", "{}\n"))
                            for name in ("summary", "p0-test-capture-tools", "p0-check-egress-spec")},
            "atomic-double": {"receipt": str(_write(products / "atomic.json", "{}\n"))},
            "environment-p0": {"receipt": str(_write(products / "env.json", "{}\n"))},
            "ledger-checkpoint": {"checkpoint": str(_write(products / "checkpoint.json", "{}\n"))},
            "preflight-plan": {"campaign": str(_write(products / "campaign.json", "{}\n"))},
            "job-rehearsal": {"receipt": str(_write(products / "job.json", json.dumps({"status": "passed"})))},
            "p0-receipt": {"receipt": str(_write(products / "p0.json", "{}\n"))},
            "vc0-closeout": {"receipt": str(_write(products / "closeout.json", "{}\n"))},
        }
        self.steps_dir = root / "steps"

    def deploy(self, *, stamp: str) -> Path:
        return _write(self.data / "control" / f"codex-01600-supervisor-enable-{stamp}.json",
                      json.dumps({"status": "passed", "tool_files_sha256": self.identity["tool_files_sha256"], "stamp": stamp}))

    def runner(self, argv, env=None, cwd=None) -> tuple[int, str]:
        argv = list(argv)
        if argv[:3] == [sys.executable, "-B", "-c"]:
            if self.facts_fail:
                return 1, "Traceback: 受管树坏了"
            request = json.loads(argv[4])
            out = {}
            if request.get("identity"):
                out["identity"] = dict(self.identity)
            if request.get("sections"):
                out["sections"] = [_section_sha256(Path(path)) for path, _fragment in request["sections"]]
            if request.get("recorded"):
                out["recorded"] = list(self.recorded)
            return 0, json.dumps(out) + "\n"
        if argv[:4] == ["docker", "inspect", "--format", "{{.Image}}"]:
            return 0, self.images[argv[4]] + "\n"
        if argv[:5] == ["docker", "image", "inspect", "--format", "{{.Id}}"]:
            return 0, self.image_refs.get(argv[5], "") + "\n"
        fixed = {("docker", "exec"): "Python 3.12.3", ("docker", "info"): "27.3.1", ("go", "version"): "go version go1.27.0",
                 ("node", "--version"): "v20.18.0", ("golangci-lint", "--version"): "golangci-lint 2.13.1",
                 ("bash", "--version"): "GNU bash 5.2"}
        return 0, fixed[(argv[0], argv[1])] + "\n"

    def ctx(self) -> es.Context:
        return es.Context(params=dict(self.params), driver_dir=self.driver, steps_dir=self.steps_dir, runner=self.runner)

    def record(self, step_id: str, *, status: str = "passed", origin: str = "execution") -> dict:
        step, ctx = es.STEP_BY_ID[step_id], self.ctx()
        es.begin(step, ctx)
        return es.finish(step, ctx, status=status, products=self.products.get(step_id, {}), origin=origin)

    def record_all(self, *, origin: str = "execution") -> None:
        for step in es.STEPS:
            if not step.live:
                self.record(step.step_id, origin=origin)

    def evaluate(self, *, formal: bool = False, accept_snapshot: bool = False) -> dict:
        return es.evaluate(self.ctx(), is_formal_built=formal, accept_snapshot=accept_snapshot)


def _by(result: dict, decision: str) -> set[str]:
    return {step["step_id"] for step in result["steps"] if step["decision"] == decision}


def _step(result: dict, step_id: str) -> dict:
    return next(step for step in result["steps"] if step["step_id"] == step_id)


NON_LIVE = {step.step_id for step in es.STEPS if not step.live}
LIVE = {step.step_id for step in es.STEPS if step.live}


class EntryStepsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = Fixture(Path(directory.name).resolve())
        self.fx.record_all()

    def assertReruns(self, expected: set[str]) -> dict:
        result = self.fx.evaluate()
        self.assertEqual(_by(result, "run"), expected)
        self.assertEqual(_by(result, "reuse"), NON_LIVE - expected)
        self.assertEqual(_by(result, "live"), LIVE)
        return result


class EntryStepsAcceptanceTests(EntryStepsTestCase):
    """方案 E2-05 的验收：逐类各改一项，只有依赖它的步骤和它们的下游失效，没依赖的保持沿用，失效原因写对。"""

    def test_baseline_reuses_every_step_and_live_steps_always_run(self) -> None:
        result = self.assertReruns(set())
        self.assertEqual(es.exit_code(result), 0)

    def test_changing_a_parameter(self) -> None:
        self.fx.params["MAIN_MODEL"] = "gpt-5.7"
        result = self.assertReruns({"preflight-plan", "job-rehearsal", "release-certification", "p0-receipt", "vc0-closeout"})
        self.assertIn("输入变了：参数 param:MAIN_MODEL", _step(result, "preflight-plan")["reasons"])
        self.assertIn("上游要重做：preflight-plan", _step(result, "job-rehearsal")["reasons"])
        self.assertEqual(es.exit_code(result), 1)

    def test_changing_a_test_file(self) -> None:
        test_file = self.fx.data / "tools" / "official_client_capture" / "tests" / "test_x.py"
        test_file.write_text("# 测试（改了）\n", encoding="utf-8")
        self.fx.params["ENTRY_COMMIT"] = "b" * 40
        result = self.assertReruns({"entry-gates", "pre-a3", "zero-request-smoke", "release-certification", "p0-receipt",
                                    "vc0-closeout"})
        name = f"tree:{es.OCC}/tests#pycache"
        self.assertIn(f"输入变了：测试与夹具 {name}", _step(result, "pre-a3")["reasons"])
        self.assertIn("输入变了：测试与夹具 source_commit:ENTRY_COMMIT", _step(result, "entry-gates")["reasons"])

    def test_changing_a_guide_line_outside_and_inside_part_two(self) -> None:
        self.fx.guide.write_text(self.fx.guide.read_text(encoding="utf-8").replace("第一部分正文", "第一部分正文（改）"),
                                 encoding="utf-8")
        self.assertReruns({"pre-a3", "release-certification", "p0-receipt", "vc0-closeout"})
        self.fx.guide.write_text(self.fx.guide.read_text(encoding="utf-8").replace("第二部分正文", "第二部分正文（改）"),
                                 encoding="utf-8")
        result = self.assertReruns({"pre-a3", "preflight-plan", "job-rehearsal", "release-certification", "p0-receipt",
                                    "vc0-closeout"})
        self.assertIn("输入变了：文档 section:{SCENARIOS_JSON}", _step(result, "preflight-plan")["reasons"])

    def test_reinstalling_the_driver(self) -> None:
        for path in [path for path in self.fx.driver.parent.rglob("*")]:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                path.unlink()
                path.write_text(text, encoding="utf-8")   # 同内容重装：新文件、新时间
        self.assertReruns(set())
        stage1_finish = self.fx.driver / "stage1-finish.sh"
        stage1_finish.write_text(stage1_finish.read_text(encoding="utf-8") + "# 改了一行\n", encoding="utf-8")
        result = self.assertReruns({"atomic-double", "job-rehearsal", "release-certification", "p0-receipt", "vc0-closeout"})
        self.assertIn("输入变了：命令与驱动 driver:stage1-finish.sh", _step(result, "atomic-double")["reasons"])

    def test_changing_the_deployment_receipt(self) -> None:
        self.fx.deploy(stamp="20261002t010000z")   # 同内容重新部署：整树摘要不变，收据换新
        result = self.assertReruns({"policy-activation", "release-certification", "p0-receipt", "vc0-closeout"})
        self.assertIn("输入变了：上游产物 deploy_file:", _step(result, "policy-activation")["reasons"])
        # pre-A3 跨部署复用（第 19 项）、入口门禁与预检 plan 只看整树摘要字段，都不失效。
        self.assertEqual({step: _step(result, step)["decision"] for step in ("pre-a3", "entry-gates", "preflight-plan")},
                         {"pre-a3": "reuse", "entry-gates": "reuse", "preflight-plan": "reuse"})

    def test_changing_an_image(self) -> None:
        self.fx.images["capture-cli"] = "sha256:" + "9" * 64
        result = self.assertReruns({"atomic-double", "environment-p0", "preflight-plan", "job-rehearsal",
                                    "release-certification", "p0-receipt", "vc0-closeout"})
        self.assertIn("输入变了：环境 container_image:capture-cli", _step(result, "environment-p0")["reasons"])
        self.assertIn("上游要重做：environment-p0", _step(result, "preflight-plan")["reasons"])


class EntryStepsRuleTests(EntryStepsTestCase):
    def test_every_reason_is_reported_at_once(self) -> None:
        self.fx.params["MAIN_MODEL"] = "gpt-5.7"
        self.fx.images["capture-cli"] = "sha256:" + "9" * 64
        reasons = _step(self.fx.evaluate(), "job-rehearsal")["reasons"]
        self.assertIn("输入变了：环境 container_image:capture-cli", reasons)
        self.assertIn("上游要重做：preflight-plan", reasons)

    def test_recorded_replay_data_is_an_input_of_pre_a3(self) -> None:
        _write(Path(self.fx.recorded[1]) / "result.json", '{"changed": true}\n')
        result = self.fx.evaluate()
        self.assertIn("输入变了：环境 recorded:", _step(result, "pre-a3")["reasons"])
        self.assertEqual(_by(result, "run"), {"pre-a3", "release-certification", "p0-receipt", "vc0-closeout"})

    def test_tampered_record_is_not_reused(self) -> None:
        path = es.record_path(self.fx.steps_dir, "policy-compatibility")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "passed "
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "policy-compatibility")["decision"], "run")
        self.assertIn("自摘要不符", _step(result, "policy-compatibility")["reasons"][0])
        self.assertEqual(_step(result, "policy-activation")["decision"], "run")   # 下游跟着重做

    def test_failed_last_time_is_not_reused(self) -> None:
        self.fx.record("zero-request-smoke", status="failed")
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "zero-request-smoke")["decision"], "run")
        self.assertIn("上次执行没有通过", _step(result, "zero-request-smoke")["reasons"][0])

    def test_snapshot_records_need_explicit_acceptance(self) -> None:
        self.fx.record_all(origin="snapshot")
        result = self.fx.evaluate()
        self.assertEqual(_by(result, "run"), NON_LIVE - {"ledger"})
        self.assertEqual(_by(result, "blocked"), {"ledger"})
        self.assertEqual(_by(self.fx.evaluate(accept_snapshot=True), "reuse"), NON_LIVE)

    def test_changed_or_missing_product(self) -> None:
        Path(self.fx.params["POLICY_ACTIVATION"]).write_text(json.dumps({"policy_sha256": "p" * 64, "x": 1}), encoding="utf-8")
        result = self.fx.evaluate()
        self.assertTrue(any("产物 activation 被改动" in reason for reason in _step(result, "policy-activation")["reasons"]))
        self.assertEqual(_step(result, "pre-a3")["decision"], "reuse")   # 只看策略摘要字段，字段没变
        Path(self.fx.products["atomic-double"]["receipt"]).unlink()
        result = self.fx.evaluate()
        self.assertTrue(any("产物 receipt 不在了" in reason for reason in _step(result, "atomic-double")["reasons"]))

    def test_failed_job_rehearsal_receipt_cannot_be_recorded_as_passed_nor_reused(self) -> None:
        receipt = Path(self.fx.products["job-rehearsal"]["receipt"])
        receipt.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
        with self.assertRaisesRegex(es.EntryStepsError, "内容不合格：status=failed"):
            self.fx.record("job-rehearsal")
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "job-rehearsal")["decision"], "run")

    def test_formal_built_freezes_the_creation_chain(self) -> None:
        self.fx.params["MAIN_MODEL"] = "gpt-5.7"
        _write(self.fx.data / "evidence" / "campaigns" / self.fx.params["NEW"] / "campaign.json", "{}\n")
        ctx = self.fx.ctx()
        self.assertTrue(es.formal_built(ctx))
        result = es.evaluate(ctx, is_formal_built=es.formal_built(ctx))
        self.assertEqual(_by(result, "frozen"), NON_LIVE)
        self.assertEqual(es.exit_code(result), 0)

    def test_ledger_is_never_rebuilt(self) -> None:
        self.fx.params["STAGE_BUDGETS"] = "VC-0=200"
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "ledger")["decision"], "blocked")
        self.assertIn("绝不重建", _step(result, "ledger")["reasons"][0])
        self.assertEqual(_step(result, "ledger-checkpoint")["decision"], "run")
        self.assertEqual(es.exit_code(result), 3)
        # 截止时间只在建账本时折算预算，之后的延期记在账本事件里：改了不阻塞。
        self.fx.params["STAGE_BUDGETS"] = "VC-0=165"
        self.fx.params["PROJECT_DEADLINE_UTC"] = "2026-10-20T15:59:00Z"
        self.assertEqual(_step(self.fx.evaluate(), "ledger")["decision"], "reuse")
        # 没有步骤记录但账本已在：同样阻塞，不重建。
        es.record_path(self.fx.steps_dir, "ledger").unlink()
        self.assertEqual(_step(self.fx.evaluate(), "ledger")["decision"], "blocked")

    def test_unavailable_managed_facts_mark_inputs_missing_instead_of_aborting(self) -> None:
        self.fx.facts_fail = True
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "policy-activation")["decision"], "run")
        self.assertEqual(_step(result, "ledger")["decision"], "reuse")
        changed = _step(result, "policy-activation")["changed_inputs"]
        self.assertTrue(all(entry["after"] == es.MISSING for entry in changed if entry["name"].startswith("identity:")))

    def test_changed_step_declaration(self) -> None:
        path = es.record_path(self.fx.steps_dir, "zero-request-smoke")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["spec_sha256"] = "0" * 64
        path.write_text(json.dumps(es.seal(payload)), encoding="utf-8")
        self.assertIn("步骤的输入声明变了（工具升级）", _step(self.fx.evaluate(), "zero-request-smoke")["reasons"])

    def test_history_keeps_every_record(self) -> None:
        self.fx.record("zero-request-smoke", status="failed")
        names = sorted(path.name for path in (self.fx.steps_dir / "history").glob("zero-request-smoke-*"))
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].endswith(".json") and any("-failed-" in name for name in names))

    def test_passed_record_requires_its_products(self) -> None:
        step, ctx = es.STEP_BY_ID["atomic-double"], self.fx.ctx()
        es.begin(step, ctx)
        with self.assertRaisesRegex(es.EntryStepsError, "没有默认坐标"):
            es.finish(step, ctx, status="passed", products={})
        with self.assertRaisesRegex(es.EntryStepsError, "没有这些产物"):
            es.finish(step, ctx, status="passed", products={"other": "/nonexistent"})
        failed = es.finish(step, ctx, status="failed", products={"receipt": str(self.fx.root / "absent.json")})
        self.assertEqual(failed["products"][0]["sha256"], es.MISSING)
        es.begin(step, ctx)
        self.assertEqual(es.finish(step, ctx, status="failed", products={})["products"], [], "记失败时拿不到坐标的产物就不记")


class EntryStepsTableAndCliTests(unittest.TestCase):
    def test_step_table_is_ordered_and_closed(self) -> None:
        seen: set[str] = set()
        for step in es.STEPS:
            self.assertTrue(set(step.upstream) <= seen, step.step_id)
            seen.add(step.step_id)
        self.assertEqual({step.step_id for step in es.STEPS if step.live}, {"entry-preflight", "client-launch-probe"})
        self.assertEqual([step.step_id for step in es.STEPS if step.ledger], ["ledger"])
        self.assertEqual({step.segment for step in es.STEPS}, set(es.SEGMENTS))

    def test_tree_digest_ignores_bytecode_and_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / "a" / "b.py", "x = 1\n")
            before = es.tree_digest(root, es.TREE_EXCLUDES["pycache"])
            _write(root / "a" / "__pycache__" / "b.cpython-312.pyc", "bytecode")
            os.utime(root / "a" / "b.py", (1, 1))
            self.assertEqual(es.tree_digest(root, es.TREE_EXCLUDES["pycache"]), before)
            _write(root / "a" / "b.py", "x = 2\n")
            self.assertNotEqual(es.tree_digest(root, es.TREE_EXCLUDES["pycache"]), before)

    def test_cli_evaluate_snapshot_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(Path(directory).resolve())
            products = [f"{step}:{name}={path}" for step, mapping in fx.products.items() for name, path in mapping.items()]
            with mock.patch.object(es, "_context", lambda args: fx.ctx()):
                self.assertEqual(es.main(["evaluate"]), 3)          # 没有记录、账本已在：账本阻塞，其余要做
                snapshot = [argument for item in products for argument in ("--product", item)]
                self.assertEqual(es.main(["snapshot", *snapshot]), 0)
                self.assertEqual(es.main(["evaluate"]), 3)          # 快照默认不接受
                out = Path(directory) / "evaluation.json"
                self.assertEqual(es.main(["evaluate", "--accept-snapshot", "--json", str(out)]), 0)
                self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["schema_version"], es.EVALUATION_SCHEMA)
                self.assertEqual(es.main(["record", "--step", "nonexistent", "--status", "passed"]), 2)
                self.assertEqual(es.main(["snapshot", "--product", "atomic-double=/x"]), 2)   # 缺步骤前缀
            self.assertEqual(es.main(["tree-digest", "--root", str(fx.data / "docs")]), 0)

    def test_deploy_consistency_compares_tests_guides_and_deploy_script(self) -> None:
        """整树身份不含测试目录：测试改了、没重新部署，整树摘要相等也要拦下（10-02 ARM64 实测就是这样）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("tree", "data"):
                _write(root / side / "tools" / "official_client_capture" / "codex_upgrade.py", "# 受管\n")
                _write(root / side / "tools" / "official_client_capture" / "tests" / "test_a.py", "# 测试\n")
                _write(root / side / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "# 指南\n")
                _write(root / side / "docs" / "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "# 框架\n")
                _write(root / side / "tools" / "arm64_supervised_deploy.py", "# 部署\n")
            _write(root / "data" / "tools" / "official_client_capture" / "__pycache__" / "x.pyc", "字节码不计")
            self.assertEqual(es.deploy_consistency(root / "tree", root / "data"), [])
            _write(root / "data" / "tools" / "official_client_capture" / "tests" / "test_a.py", "# 旧测试\n")
            (root / "data" / "tools" / "arm64_supervised_deploy.py").unlink()
            self.assertEqual(es.deploy_consistency(root / "tree", root / "data"),
                             ["tools/official_client_capture", "tools/arm64_supervised_deploy.py"])
            self.assertEqual(es.main(["deploy-consistency", "--tree", str(root / "tree"), "--data-root", str(root / "data")]), 1)

    def test_context_takes_directories_from_the_driver_environment(self) -> None:
        arguments = es._parse(["evaluate"])
        with mock.patch.dict(os.environ, {"RUNROOT": "/r", "DRV": "/d", "D": "/data"}):
            os.environ.pop("ENTRY_STEPS_DIR", None)
            ctx = es._context(arguments)
        self.assertEqual((ctx.steps_dir, ctx.driver_dir, ctx.data_root), (Path("/r/entry-steps"), Path("/d"), Path("/data")))
        with mock.patch.dict(os.environ, {"D": "/data"}, clear=True):
            with self.assertRaisesRegex(es.EntryStepsError, "没有步骤记录目录"):
                es._context(arguments)
        with mock.patch.dict(os.environ, {"RUNROOT": "/r"}, clear=True):
            with self.assertRaisesRegex(es.EntryStepsError, "没有数据根 D"):
                es._context(arguments)

    def test_driver_carries_an_identical_copy(self) -> None:
        original = REPO_ROOT / "tools" / "ci" / "entry_steps.py"
        copy = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry_steps.py"
        self.assertEqual(copy.read_bytes(), original.read_bytes())


if __name__ == "__main__":
    unittest.main()
