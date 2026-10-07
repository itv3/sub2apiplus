"""Claude 官方取证场景清单、规则清单与证据标签（Claude 升级控制面设计 B2-05）。

* 场景清单 ``claude-upgrade-scenarios/v1``：结构闭合、变量合同（设计规定的 14 个变量加三项实施补充）、与 FW-F v4
  目录（49 维 79 probe）逐项一致、规则覆盖与 ``tool_dependencies``（作业依赖分析器生成）逐项相等。
* 验收一：``plan --campaign-mode preflight_only`` 展开全部 Job（零写入），运行期变量保留占位并能逐步绑定。
* 验收二：证据标签逐连接顺序测试——每个 probe 的逐连接标签与 2.1.226 实测的中继有效连接序列逐条匹配（方法与
  路径、单连接请求数、合成计划的干预动作），任何错位都失败关闭。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import job_dependency_analyzer as analyzer

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_2_1_226_relay_connection_sequences.json"
# 设计 B2-05 规定的变量合同；实施补充 capture_root、capture_tool_root、host_runtime_receipt_sha256 与 B2-06 补充的
# synthetic_capture_container（合成 probe 的断网容器，见各自说明）。
DESIGN_VARIABLES = {
    "campaign_id", "campaign_dir", "target_version", "target_sha256", "runtime_image", "capture_container", "claude_bin",
    "model", "upstream_ip", "credentials_file", "ca_signing_pem", "ca_cert", "host_runtime_receipt", "run_nonce",
}
SUPPLEMENTED_VARIABLES = {"capture_root", "capture_tool_root", "host_runtime_receipt_sha256", "synthetic_capture_container"}
RUNTIME_VARIABLES = {"host_runtime_receipt", "host_runtime_receipt_sha256", "run_nonce"}
SHA_A = "a" * 64
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64


def _scenario_payload() -> dict:
    return json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))


def _baseline_rules() -> tuple[str, ...]:
    return plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE)


def _plan_argv(campaign_dir: Path, *extra: str, campaign_id: str = "claude-code-2_1_285-t1") -> list[str]:
    return [
        "plan", "--campaign-dir", str(campaign_dir), "--campaign-mode", "preflight_only",
        "--campaign-purpose", "validation_only", "--baseline-version", BASELINE, "--target-version", TARGET,
        "--target-sha256", SHA_A, "--runtime-image", IMAGE, "--model", "claude-sonnet-5",
        "--upstream-ip", "160.79.104.10", "--campaign-id", campaign_id, *extra,
    ]


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class ScenarioManifestTests(unittest.TestCase):
    """场景清单结构、绑定与变量合同。"""

    def test_real_manifest_validates_and_binds(self) -> None:
        payload = plan.read_scenario_manifest(plan.scenario_manifest_path(TARGET), expected_version=TARGET, rules=_baseline_rules())
        self.assertEqual((payload["schema_version"], payload["claude_version"]), (plan.SCENARIO_SCHEMA, TARGET))
        self.assertEqual(payload["rule_manifest"]["path"], f"tools/official_client_capture/claude_upgrade_rules_{TARGET.replace('.', '_')}.json")
        guide = REPOSITORY_ROOT / payload["source_spec"]["path"]
        self.assertEqual(plan.guide_section_sha256(guide, "第二部分"), payload["source_spec"]["sha256"])
        self.assertEqual(len(payload["capture_jobs"]), 12)
        self.assertTrue(all(job["phase"] == "official" for job in payload["capture_jobs"]))

    def test_variable_contract_covers_design_and_scopes(self) -> None:
        contract = {item["name"]: item for item in _scenario_payload()["variable_contract"]}
        self.assertEqual(set(contract), DESIGN_VARIABLES | SUPPLEMENTED_VARIABLES)
        self.assertEqual({name for name, item in contract.items() if item["scope"] == "step_runtime"}, RUNTIME_VARIABLES)
        self.assertTrue(all(item["sensitive"] is False for item in contract.values()))
        self.assertEqual(set(plan.CAMPAIGN_VARIABLES), {name for name, item in contract.items() if item["scope"] == "campaign"})
        self.assertEqual(contract["upstream_ip"]["type"], "ip_address")

    def test_schema_file_mirrors_validator(self) -> None:
        schema = json.loads((TOOL_ROOT / "claude_upgrade_scenarios.schema.json").read_text(encoding="utf-8"))
        definitions = schema["$defs"]
        self.assertEqual(set(schema["required"]), set(plan._SCENARIO_TOP_FIELDS))
        self.assertEqual(set(definitions["captureJob"]["required"]), set(plan._JOB_FIELDS))
        self.assertEqual(set(definitions["commandStep"]["required"]), set(plan._STEP_FIELDS))
        self.assertEqual(set(definitions["evidenceScenario"]["required"]), set(plan._SCENARIO_FIELDS))
        self.assertEqual(set(definitions["variable"]["required"]), set(plan._VARIABLE_FIELDS))
        self.assertEqual(set(definitions["variable"]["properties"]["type"]["enum"]), set(plan.VARIABLE_TYPES))
        self.assertEqual(set(definitions["variable"]["properties"]["scope"]["enum"]), set(plan.VARIABLE_SCOPES))
        self.assertEqual(set(definitions["artifactKind"]["enum"]), set(plan.LABEL_KIND_PARSERS))
        self.assertEqual(_scenario_payload()["$schema"], "./claude_upgrade_scenarios.schema.json")

    def test_shape_rejections(self) -> None:
        def mutate(change):
            payload = copy.deepcopy(_scenario_payload())
            change(payload)
            return payload

        first_job = lambda payload: payload["capture_jobs"][0]  # noqa: E731
        first_step = lambda payload: payload["capture_jobs"][0]["steps"][0]  # noqa: E731

        def drop_flag(payload, flag):
            argv = first_step(payload)["argv"]
            position = argv.index(flag)
            del argv[position:position + 2]

        cases = {
            "顶层多字段": (lambda p: p.update(extra=1), "顶层字段不闭合"),
            "敏感变量": (lambda p: p["variable_contract"][0].update(sensitive=True), "sensitive"),
            "运行期变量带默认值": (
                lambda p: next(v for v in p["variable_contract"] if v["name"] == "run_nonce").update(default="x"),
                "运行期变量",
            ),
            "发现项场景带规则": (
                lambda p: next(s for s in p["evidence_scenarios"] if s["purpose"] == "discovery")["covers"].append("SPEC-BODY-001"),
                "purpose 与 covers",
            ),
            "候选 Job": (lambda p: first_job(p).update(phase="candidate"), "B4-01"),
            "缺 fw-f-v4-probe": (
                lambda p: first_step(p)["argv"].__setitem__(first_step(p)["argv"].index("--fw-f-v4-probe") + 1, "v4-other"),
                "fw-f-v4-probe",
            ),
            "真实步骤缺上游 IP": (lambda p: drop_flag(p, "--upstream-ip"), "upstream-ip"),
            "probe 执行两次": (
                lambda p: p["capture_jobs"][1]["steps"].append(copy.deepcopy(first_step(p))),
                "只能由一个取证步骤",
            ),
            "scenario_ids 与步骤不符": (lambda p: first_job(p)["scenario_ids"].pop(), "scenario_ids 必须等于"),
            "covers 超出场景": (lambda p: p["capture_jobs"][-1]["covers"].append("SPEC-TLS-001"), "未被绑定场景证明"),
            "依赖未排序": (lambda p: first_job(p)["tool_dependencies"].reverse(), "tool_dependencies"),
            "两个证据根": (lambda p: first_job(p)["evidence_roots"].append("/capture/runs/x"), "恰好一个"),
            "收据场景非空": (lambda p: first_job(p)["required_scenario_receipts"].append("A11"), "required_scenario_receipts"),
            "真实步骤进断网容器": (
                lambda p: first_step(p)["argv"].__setitem__(2, "{synthetic_capture_container}"),
                "docker exec {capture_container}",
            ),
            "合成步骤进联网容器": (
                lambda p: next(job for job in p["capture_jobs"] if job["id"].startswith("official-synthetic-"))["steps"][0][
                    "argv"
                ].__setitem__(2, "{capture_container}"),
                "docker exec {synthetic_capture_container}",
            ),
            "步骤不经 docker exec": (
                lambda p: first_step(p).update(argv=first_step(p)["argv"][3:]),
                "docker exec {capture_container}",
            ),
        }
        for label, (change, message) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ConfigurationError, message):
                    plan.validate_scenario_manifest(mutate(change))

    def test_template_rules(self) -> None:
        payload = _scenario_payload()
        context = {name: "x" for name in plan.CAMPAIGN_VARIABLES}
        context.update(
            campaign_dir="/data/evidence/campaigns/c1", target_version=TARGET, target_sha256=SHA_A, runtime_image=IMAGE,
            capture_root="/capture", capture_tool_root="/root/oauth-capture/tools/official_client_capture",
            claude_bin="/opt/claude-2.1.285/claude", upstream_ip="160.79.104.10", credentials_file="/root/.claude/c.json",
            ca_signing_pem="/opt/mitm/a.pem", ca_cert="/opt/mitm/b.pem",
            capture_container="capture-cli", synthetic_capture_container="capture-cli-synthetic",
        )
        plan.validate_campaign_context(payload, context)
        with self.assertRaisesRegex(ConfigurationError, "独立的断网容器"):
            plan.validate_campaign_context(payload, {**context, "synthetic_capture_container": "capture-cli"})
        bad = copy.deepcopy(payload)
        bad["capture_jobs"][0]["steps"][0]["argv"].append("{undeclared_name}")
        with self.assertRaisesRegex(ConfigurationError, "variable_contract 外变量"):
            plan.validate_campaign_context(bad, context)
        escaped = copy.deepcopy(payload)
        escaped["capture_jobs"][0]["steps"][0]["argv"].append("{{literal}}")
        with self.assertRaisesRegex(ConfigurationError, "只允许"):
            plan.validate_campaign_context(escaped, context)
        with self.assertRaisesRegex(ConfigurationError, "运行期变量不得在 plan 时取值"):
            plan.validate_campaign_context(payload, {**context, "run_nonce": SHA_A})
        with self.assertRaisesRegex(ConfigurationError, "ip_address"):
            plan.validate_campaign_context(payload, {**context, "upstream_ip": "10.0.0.1"})
        root_runtime = copy.deepcopy(payload)
        root_runtime["capture_jobs"][0]["evidence_roots"] = ["{capture_root}/runs/{run_nonce}"]
        with self.assertRaisesRegex(ConfigurationError, "证据根不得引用运行期变量"):
            plan.validate_campaign_context(root_runtime, context)


class CatalogConsistencyTests(unittest.TestCase):
    """场景清单与 FW-F v4 目录（49 维 79 probe）逐项一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _scenario_payload()
        cls.steps = {step["probe_id"]: (job, step) for job in cls.payload["capture_jobs"] for step in job["steps"]}
        cls.scenarios = {item["scenario_id"]: item for item in cls.payload["evidence_scenarios"]}

    def test_every_probe_runs_exactly_once(self) -> None:
        executed = [step["probe_id"] for job in self.payload["capture_jobs"] for step in job["steps"]]
        self.assertEqual(sorted(executed), sorted(v4.PROBE_IDS))
        self.assertEqual(len(executed), len(set(executed)))
        catalog = self.payload["probe_catalog"]
        self.assertEqual((catalog["probe_count"], catalog["dimension_count"]), (len(v4.PROBES), len(v4.REQUIRED_MATRIX_DIMENSIONS)))
        self.assertEqual((catalog["probe_count"], catalog["dimension_count"]), (79, 49))

    def test_dimension_and_condition_scenarios(self) -> None:
        for dimension in v4.REQUIRED_MATRIX_DIMENSIONS:
            with self.subTest(dimension=dimension):
                expected = sorted(pid for pid, probe in v4.PROBES.items() if dimension in probe.dimensions)
                self.assertEqual(self.scenarios[dimension]["probe_ids"], expected)
        conditions = {sid: item for sid, item in self.scenarios.items() if sid.startswith("condition.")}
        self.assertEqual(set(self.scenarios) - set(conditions), set(v4.REQUIRED_MATRIX_DIMENSIONS))
        members = sorted(pid for item in conditions.values() for pid in item["probe_ids"])
        self.assertEqual(members, sorted(pid for pid, probe in v4.PROBES.items() if not probe.dimensions))
        discovery = sorted(sid for sid, item in self.scenarios.items() if item["purpose"] == "discovery")
        self.assertEqual(discovery, ["agency.hook", "aux.models", "header.dispatch_id", "header.usage_limit"])

    def test_steps_follow_probe_definitions(self) -> None:
        for probe_id, (job, step) in self.steps.items():
            probe = v4.PROBES[probe_id]
            with self.subTest(probe=probe_id):
                synthetic = "--acknowledge-synthetic-responses" in step["argv"]
                self.assertEqual(synthetic, bool(probe.response_plan))
                self.assertEqual("--upstream-ip" in step["argv"], not synthetic)
                container = "{synthetic_capture_container}" if synthetic else "{capture_container}"
                self.assertEqual(step["argv"][:3], ["docker", "exec", container])
                self.assertIn(f"{{capture_root}}/runs/claude-official/{{campaign_id}}/{job['id']}/{probe_id}", step["argv"])
                self.assertEqual(job["evidence_roots"], [f"{{capture_root}}/runs/claude-official/{{campaign_id}}/{job['id']}"])
                if synthetic:
                    self.assertTrue(job["id"].startswith("official-synthetic-"))

    def test_artifact_kinds_follow_probe_channels(self) -> None:
        for scenario_id, item in self.scenarios.items():
            probes = [v4.PROBES[pid] for pid in item["probe_ids"]]
            with self.subTest(scenario=scenario_id):
                kinds = set(item["required_artifact_kinds"])
                self.assertTrue({"relay_binary", "relay_manifest"} <= kinds)
                self.assertEqual("pcap" in kinds, any(probe.require_pcap for probe in probes))
                self.assertEqual("debug_log" in kinds, any(probe.require_debug_log for probe in probes))
                self.assertEqual("synthetic_intervention" in kinds, any(probe.response_plan for probe in probes))


class RuleCoverageTests(unittest.TestCase):
    """规则清单逐条承接基线；official Job 覆盖规则全集。"""

    def test_target_rules_carry_baseline(self) -> None:
        rules = _baseline_rules()
        self.assertEqual(len(rules), 40)
        target = plan.load_target_rules(
            plan.rule_manifest_path(TARGET), target_version=TARGET, baseline_rules=rules,
            baseline_manifest=plan.baseline_rule_manifest_path(BASELINE),
        )
        self.assertEqual(target, rules)

    def test_target_rules_rejections(self) -> None:
        rules = _baseline_rules()
        original = json.loads(plan.rule_manifest_path(TARGET).read_text(encoding="utf-8"))
        cases = {
            "顺序不同": (lambda p: p["required_rules"].reverse(), "相同顺序"),
            "基线摘要漂移": (lambda p: p["baseline_rule_manifest"].update(sha256=SHA_A), "摘要漂移"),
            "版本不符": (lambda p: p.update(claude_version="2.1.286"), "claude_version"),
            "少一条": (lambda p: p["required_rules"].pop(), "相同顺序"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for label, (change, message) in cases.items():
                with self.subTest(label=label):
                    payload = copy.deepcopy(original)
                    change(payload)
                    path = Path(directory) / f"{label}.json"
                    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigurationError, message):
                        plan.load_target_rules(path, target_version=TARGET, baseline_rules=rules,
                                               baseline_manifest=plan.baseline_rule_manifest_path(BASELINE))

    def test_official_jobs_cover_all_rules(self) -> None:
        rules = _baseline_rules()
        payload = _scenario_payload()
        covered = {rule for job in payload["capture_jobs"] for rule in job["covers"]}
        self.assertEqual(covered, set(rules))
        scenario_covers = {rule for item in payload["evidence_scenarios"] for rule in item["covers"]}
        self.assertEqual(scenario_covers, set(rules))


class ToolDependencyDeclarationTests(unittest.TestCase):
    """R16：Claude 场景清单逐作业 tool_dependencies 与作业依赖分析器的结果逐项相等（多登记、漏登记都失败）。"""

    def test_declared_dependencies_match_analysis(self) -> None:
        shared = analyzer.DependencyAnalyzer(analyzer.current_tool_index())
        manifests = sorted(TOOL_ROOT.glob("claude_upgrade_scenarios_*.json"))
        self.assertTrue(manifests)
        for manifest in manifests:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            for job in payload["capture_jobs"]:
                with self.subTest(manifest=manifest.name, job=job["id"]):
                    self.assertEqual(job["tool_dependencies"], shared.analyze_steps(job["steps"]))
                    self.assertIn(plan.RELAY_SCRIPT, job["tool_dependencies"])


class PlanExpansionTests(unittest.TestCase):
    """验收：plan --campaign-mode preflight_only 展开全部 Job（零写入）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.campaign_dir = self.root / "evidence" / "campaigns" / "claude-code-2_1_285-t1"

    def test_preflight_plan_expands_all_jobs(self) -> None:
        code, stdout, stderr = _run_main(_plan_argv(self.campaign_dir))
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        payload = _scenario_payload()
        self.assertEqual(report["schema_version"], plan.PLAN_REPORT_SCHEMA)
        self.assertEqual((report["campaign_created"], report["files_written"], report["live_request_count"]), (False, 0, 0))
        self.assertFalse(self.campaign_dir.exists())
        self.assertEqual([job["id"] for job in report["jobs"]], [job["id"] for job in payload["capture_jobs"]])
        self.assertEqual((report["job_count"], report["step_count"]), (12, 79))
        probes = [step["probe_id"] for job in report["jobs"] for step in job["steps"]]
        self.assertEqual(sorted(probes), sorted(v4.PROBE_IDS))
        self.assertEqual(report["coverage_plan"]["official_unmapped"], [])
        self.assertEqual(report["required_rules"], list(_baseline_rules()))
        self.assertEqual(report["configuration"]["claude_bin"], f"/opt/claude-{TARGET}/claude")
        self.assertEqual(report["configuration"]["capture_root"], "/capture")
        self.assertEqual(
            (report["configuration"]["capture_container"], report["configuration"]["synthetic_capture_container"]),
            ("capture-cli", "capture-cli-synthetic"),
        )
        for name, path in (
            ("target_scenarios", plan.scenario_manifest_path(TARGET)),
            ("evidence_labels", plan.evidence_labels_path(TARGET)),
            ("target_rules", plan.rule_manifest_path(TARGET)),
            ("baseline_rules", plan.baseline_rule_manifest_path(BASELINE)),
        ):
            self.assertEqual(report["inputs"][name]["sha256"], plan.file_sha256(path), name)
        for job in report["jobs"]:
            self.assertEqual(job["evidence_roots"], [f"/capture/runs/claude-official/claude-code-2_1_285-t1/{job['id']}"])
            for step in job["steps"]:
                with self.subTest(probe=step["probe_id"]):
                    self.assertEqual(set(step["runtime_variables"]), RUNTIME_VARIABLES)
                    placeholders = {field for item in step["argv"] for field in plan.TEMPLATE_FIELD_RE.findall(item)}
                    self.assertEqual(placeholders, RUNTIME_VARIABLES)
                    self.assertIn(f"claude-code-2_1_285-t1-{step['probe_id']}", step["argv"])
                    self.assertIn(TARGET, step["argv"])

    def test_runtime_binding(self) -> None:
        _code, stdout, _stderr = _run_main(_plan_argv(self.campaign_dir))
        step = json.loads(stdout)["jobs"][0]["steps"][0]
        values = {"host_runtime_receipt": "/capture/runs/receipts/r1.json", "host_runtime_receipt_sha256": SHA_A, "run_nonce": "c" * 64}
        bound = plan.bind_step_runtime(step, values)
        self.assertFalse(any("{" in item or "}" in item for item in bound["argv"]))
        self.assertEqual(bound["argv"][bound["argv"].index("--run-nonce") + 1], "c" * 64)
        self.assertEqual(set(bound), {"probe_id", "argv", "environment", "timeout"})
        with self.assertRaisesRegex(ConfigurationError, "不一致"):
            plan.bind_step_runtime(step, {key: value for key, value in values.items() if key != "run_nonce"})
        with self.assertRaisesRegex(ConfigurationError, "sha256"):
            plan.bind_step_runtime(step, {**values, "run_nonce": "not-a-nonce"})
        with self.assertRaisesRegex(ConfigurationError, "absolute_path"):
            plan.bind_step_runtime(step, {**values, "host_runtime_receipt": "relative/r1.json"})

    def test_plan_rejections(self) -> None:
        cases = {
            "formal 归 B2-11": (_plan_argv(self.campaign_dir)[:4] + ["formal"] + _plan_argv(self.campaign_dir)[5:], "B2-11"),
            "私网上游": ([*_plan_argv(self.campaign_dir)[:-4], "--upstream-ip", "10.1.2.3", "--campaign-id", "claude-code-2_1_285-t1"], "ip_address"),
            "目标版本无清单": ([item if item != TARGET else "2.1.299" for item in _plan_argv(self.campaign_dir)], "场景清单"),
            "run-id 超长": (_plan_argv(self.campaign_dir, campaign_id="c" * 100), "run-id"),
            "相对目录": (_plan_argv(Path("relative/campaign")), "安全绝对路径"),
            "模型名非法": ([item if item != "claude-sonnet-5" else "bad model" for item in _plan_argv(self.campaign_dir)], "--model 格式非法"),
            "合成容器与抓包容器相同": (
                _plan_argv(self.campaign_dir, "--synthetic-capture-container", "capture-cli"), "独立的断网容器"
            ),
        }
        for label, (argv, message) in cases.items():
            with self.subTest(label=label):
                code, _stdout, stderr = _run_main(argv)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)

    def test_tampered_scenario_binding_rejected(self) -> None:
        payload = _scenario_payload()
        payload["rule_manifest"]["sha256"] = SHA_A
        tampered = self.root / "scenarios.json"
        tampered.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code, _stdout, stderr = _run_main(_plan_argv(self.campaign_dir, "--scenario-manifest", str(tampered)))
        self.assertEqual(code, 1)
        self.assertIn("摘要漂移", stderr)

    def test_orchestrator_wire_roots(self) -> None:
        parser = claude_upgrade._build_parser()
        arguments = parser.parse_args(_plan_argv(self.campaign_dir))
        plan.prepare_plan_arguments(arguments)
        context = claude_upgrade._job_context(arguments)
        self.assertEqual(set(context), set(plan.CAMPAIGN_VARIABLES))
        self.assertFalse(set(context) & RUNTIME_VARIABLES)
        jobs, inputs = claude_upgrade._load_plan_jobs(arguments, _baseline_rules())
        self.assertTrue(all(isinstance(job, claude_upgrade.Job) for job in jobs))
        self.assertEqual(set(inputs), {"baseline_rules", "target_rules", "target_scenarios"})
        self.assertEqual(plan.evidence_labels_binding(arguments, _baseline_rules())["sha256"], plan.file_sha256(plan.evidence_labels_path(TARGET)))
        self.assertTrue(all(job.tool_dependencies for job in jobs))
        contract = claude_upgrade._scenario_job_execution_contract(_scenario_payload())
        self.assertEqual(contract["claude_version"], TARGET)
        self.assertTrue(all(set(job) <= plan.SCENARIO_JOB_EXECUTION_FIELDS for job in contract["jobs"]))
        self.assertNotIn("covers", contract["jobs"][0])

    def test_core_suite_selects_declared_jobs(self) -> None:
        code, stdout, stderr = _run_main(_plan_argv(self.campaign_dir, "--suite", "core"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["job_count"], len([job for job in _scenario_payload()["capture_jobs"] if "core" in job["suites"]]))


class EvidenceLabelSequenceTests(unittest.TestCase):
    """验收：证据标签逐连接顺序与 2.1.226 实测中继序列逐条一致；错位一律失败关闭。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _scenario_payload()
        cls.labels = plan.load_evidence_labels(plan.evidence_labels_path(TARGET), expected_version=TARGET, scenario_payload=cls.payload)
        cls.plans = {probe: probe_plan for job_plans in cls.labels["plans"].values() for probe, probe_plan in job_plans.items()}
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.rules = {
            entry["job_id"]: entry["rules"] for entry in cls.labels["document"]["entries"]
        }

    def test_fixture_is_metadata_only_and_complete(self) -> None:
        self.assertEqual(self.fixture["claude_version"], BASELINE)
        self.assertEqual(sorted(self.fixture["probes"]), sorted(v4.PROBE_IDS))
        for probe_id, record in self.fixture["probes"].items():
            for connection in record["connections"]:
                with self.subTest(probe=probe_id, connection=connection["connection_id"]):
                    self.assertEqual(set(connection), {"connection_id", "requests", "intervention"})
                    self.assertTrue(all(plan.REQUEST_LINE_RE.fullmatch(item) for item in connection["requests"]))
                    self.assertNotIn(connection["connection_id"], record["excluded_connection_ids"])

    def test_every_job_and_probe_has_labels(self) -> None:
        self.assertEqual(set(self.labels["plans"]), {job["id"] for job in self.payload["capture_jobs"]})
        self.assertEqual(sorted(self.plans), sorted(v4.PROBE_IDS))

    def test_label_sequences_match_observed_connections(self) -> None:
        """逐连接顺序：每个 probe 的有效连接逐条命中标签规则（方法与路径、单连接请求数、合成干预动作）。"""

        for probe_id, probe_plan in sorted(self.plans.items()):
            record = self.fixture["probes"][probe_id]
            with self.subTest(probe=probe_id):
                matches = plan.match_relay_connections(probe_plan, record["connections"])
                self.assertEqual([item["connection_id"] for item in matches], [c["connection_id"] for c in record["connections"]])
                self.assertEqual([item["ordinal"] for item in matches], list(range(1, len(matches) + 1)))

    def test_endpoint_labels_follow_declared_requests(self) -> None:
        endpoints = {
            "HEAD /api/hello": ("hello", "lifecycle"),
            "GET /api/claude_code/policy_limits": ("policy_limits", "startup"),
            "GET /api/claude_code/settings": ("settings", "startup"),
            "GET /api/oauth/profile": ("oauth_profile", "startup"),
            "GET /v1/mcp_servers": ("mcp_servers", "startup"),
            "POST /v1/messages": ("messages", "inference"),
            "POST /v1/messages/count_tokens": ("count_tokens", "auxiliary"),
            "POST /v1/oauth/token": ("oauth_token", "oauth"),
        }
        for job_id, rules in self.rules.items():
            attempts: dict[str, list[str]] = {}
            for rule in rules:
                connection = rule.get("connection")
                if connection is None or "request" not in connection:
                    continue
                with self.subTest(job=job_id, glob=rule["glob"], connection=connection):
                    labels = rule["labels"]
                    self.assertEqual((labels["endpoint"], labels["endpoint_class"]), endpoints[connection["request"]])
                    # 合成计划的逐序号连接都冻结了干预动作；真实上游连接（含尾部规则）没有干预动作。
                    self.assertEqual(labels["response_mode"] == "synthetic", "intervention" in connection)
                    if "attempt" in labels:
                        attempts.setdefault(labels["probe"], []).append(labels["attempt"])
            for probe_id, values in attempts.items():
                self.assertEqual(values, [str(index) for index in range(1, len(values) + 1)], probe_id)

    def test_synthetic_interventions_exist_in_relay_plans(self) -> None:
        """合成连接声明的干预动作必须是中继合成计划真能给出的动作名（中继改名时这里先红）。"""

        from tools.official_client_capture import upstream_byte_relay as relay

        lines = (
            ("api.anthropic.com", "HEAD /api/hello HTTP/1.1"),
            ("api.anthropic.com", "GET /api/claude_code/policy_limits HTTP/1.1"),
            ("api.anthropic.com", "GET /api/claude_code/settings HTTP/1.1"),
            ("api.anthropic.com", "POST /v1/messages?beta=true HTTP/1.1"),
            ("platform.claude.com", "POST /v1/oauth/token HTTP/1.1"),
        )
        for job_id, rules in self.rules.items():
            for rule in rules:
                connection = rule.get("connection") or {}
                if "intervention" not in connection:
                    continue
                relay_plan = rule["labels"]["response_plan"]
                produced = set()
                for host, line in lines:
                    for ordinal in (1, 2):
                        for body in (
                            {"model": "claude-sonnet-5", "stream": True},
                            {"model": "claude-sonnet-5", "stream": False},
                            {"model": "claude-haiku-4-5", "stream": True},
                        ):
                            response = relay._synthetic_claude_response(relay_plan, host, line, json.dumps(body).encode(), ordinal)
                            if response is not None:
                                produced.add(response.action)
                with self.subTest(job=job_id, probe=rule["labels"]["probe"], intervention=connection["intervention"]):
                    self.assertIn(connection["intervention"], produced)

    def test_mismatches_fail_closed(self) -> None:
        baseline = self.fixture["probes"]["v4-replay-baseline"]["connections"]
        retry = self.fixture["probes"]["v4-replay-retry-401"]["connections"]
        usage = self.fixture["probes"]["v4-tui-usage"]["connections"]
        swapped = copy.deepcopy(baseline)
        swapped[1]["requests"], swapped[2]["requests"] = swapped[2]["requests"], swapped[1]["requests"]
        extra = copy.deepcopy(usage) + [{"connection_id": 9, "requests": ["POST /v1/messages"], "intervention": None}]
        wrong_action = copy.deepcopy(retry)
        wrong_action[3]["intervention"] = "retry-401_success"
        doubled = copy.deepcopy(baseline)
        doubled[0]["requests"] = ["HEAD /api/hello", "HEAD /api/hello"]
        new_startup = copy.deepcopy(baseline)
        new_startup.insert(3, {"connection_id": 4, "requests": ["GET /api/claude_code/grove"], "intervention": None})
        for index, connection in enumerate(new_startup, 1):
            connection["connection_id"] = index
        cases = {
            "启动顺序对调": ("v4-replay-baseline", swapped, "不一致"),
            "缺少推理连接": ("v4-replay-baseline", baseline[:3], "至少需要"),
            "缺少逐序号连接": ("v4-replay-retry-401", retry[:4], "实际只有"),
            "多出连接": ("v4-tui-usage", extra, "没有对应的标签规则"),
            "合成干预错位": ("v4-replay-retry-401", wrong_action, "干预动作"),
            "单连接多请求": ("v4-replay-baseline", doubled, "只承载 1 次"),
            "新版本多一个启动端点": ("v4-replay-baseline", new_startup, "不一致"),
            "后台出现未声明端点": ("v4-background", [{"connection_id": 1, "requests": ["GET /v1/models"], "intervention": None}], "未声明"),
        }
        for label, (probe_id, connections, message) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(plan.LabelSequenceMismatch, message):
                    plan.match_relay_connections(self.plans[probe_id], connections)
        # 查询串不参与比较（beta=true 等属于规则断言而非标签）；连接号可以不连续（握手前断开的连接已被剔除）。
        with_query = copy.deepcopy(baseline)
        with_query[3]["requests"] = ["POST /v1/messages?beta=true"]
        for connection, number in zip(with_query, (1, 4, 5, 9)):
            connection["connection_id"] = number
        self.assertEqual(len(plan.match_relay_connections(self.plans["v4-replay-baseline"], with_query)), 4)

    def test_label_declaration_rejections(self) -> None:
        original = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))

        def first_rule(document, probe_id="v4-native-tls-baseline"):
            for entry in document["entries"]:
                for rule in entry["rules"]:
                    if rule["labels"].get("probe") == probe_id and "connection" in rule:
                        return entry, rule
            raise AssertionError(probe_id)

        cases = {
            "序号断档": (lambda d: first_rule(d)[1]["connection"].update(ordinal=5), "连续"),
            "缺 Job": (lambda d: d["entries"].pop(), "缺少证据标签"),
            "probe 标签不符": (lambda d: first_rule(d)[1]["labels"].update(probe="v4-bash"), "probe 标签"),
            "场景越界": (lambda d: first_rule(d)[1]["scenario_ids"].append("tool.bash"), "子集"),
            "合成缺干预": (lambda d: first_rule(d, "v4-replay-retry-401")[1]["connection"].pop("intervention"), "干预动作"),
            "未登记 parser": (lambda d: first_rule(d)[1].update(parser="raw"), "未登记"),
            "请求带查询串": (lambda d: first_rule(d)[1]["connection"].update(request="POST /v1/messages?beta=true"), "不含查询串"),
            "版本不符": (lambda d: d.update(claude_version="2.1.226"), "版本"),
        }
        with tempfile.TemporaryDirectory() as directory:
            for label, (change, message) in cases.items():
                with self.subTest(label=label):
                    document = copy.deepcopy(original)
                    change(document)
                    path = Path(directory) / "labels.json"
                    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigurationError, message):
                        plan.load_evidence_labels(path, expected_version=TARGET, scenario_payload=self.payload)


class IdentityLayerTests(unittest.TestCase):
    """新文件按 Claude 策略归层；wire 根经 claude_upgrade_plan 按符号进入 Claude wire 闭包。"""

    def test_new_files_layers(self) -> None:
        policy = claude_identity.load_policy()
        expected = {
            "claude_upgrade_scenarios_2_1_285.json": "wire_producer",
            "claude_upgrade_evidence_labels_2_1_285.json": "evidence_semantics",
            "claude_upgrade_rules_2_1_285.json": "control",
            "claude_upgrade_plan.py": "control",
            "claude_upgrade_scenarios.schema.json": "control",
        }
        for name, layer in expected.items():
            with self.subTest(name=name):
                self.assertEqual(claude_identity.classify_path(policy, f"official_client_capture/{name}"), layer)

    def test_wire_closure_reaches_plan_module(self) -> None:
        policy = claude_identity.load_policy()
        digests = claude_identity.managed_tree_digests(REPOSITORY_ROOT, policy)
        index = claude_identity._module_index(policy, digests)
        roots = claude_identity._orchestrator_roots(policy, index, policy["orchestrator"]["wire_roots"])
        wire = claude_identity.closure(policy, REPOSITORY_ROOT, digests, roots, layer="wire_producer")
        symbols = {(item["module"], item["name"]) for item in wire["symbols"]}
        for name in ("plan_jobs", "plan_context", "expand_scenario_jobs", "scenario_job_execution_contract"):
            self.assertIn(("tools.official_client_capture.claude_upgrade_plan", name), symbols)
        # 证据标签的校验与逐连接匹配属于 evidence 语义，不得进入 wire 闭包（否则改标签校验会被判成 wire 变化而重采）。
        for name in ("load_evidence_labels", "match_relay_connections", "evidence_labels_binding", "verify_target_rules"):
            self.assertNotIn(("tools.official_client_capture.claude_upgrade_plan", name), symbols)
        # 同层（wire）模块整文件计入：取证动作体依赖的 capturelib 由 plan 模块引用而进入闭包。
        modules = {item["module"] for item in wire["modules"]}
        self.assertIn("tools.official_client_capture.capturelib.security", modules)
        self.assertEqual(claude_identity.compute_identity(policy, REPOSITORY_ROOT)["defaulted_paths"], [])


if __name__ == "__main__":
    unittest.main()
