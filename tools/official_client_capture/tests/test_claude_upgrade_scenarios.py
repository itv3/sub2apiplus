"""Claude 官方取证场景清单、规则清单与证据标签（Claude 升级控制面设计 B2-05）。

* 场景清单 ``claude-upgrade-scenarios/v1``：结构闭合、变量合同（设计规定的 14 个变量加四项实施补充）、主模型步骤与
  FW-F v5 目录（与 v4 同构：49 维 79 probe）逐项一致、版本事实绑定、规则覆盖与 ``tool_dependencies``（作业依赖分析器
  生成）逐项相等。
* 模型能力取证（B2-07 补充项）：非主模型步骤恰好是 Active 投影策略推出的 33 个「<模型>-<基础 probe>」，逐字复制主模型
  同名基础 probe 的步骤（只换 probe_id、--run-id、--output-root 与 --model），全部归入发现项场景 aux.model_capability。
* 验收一：``plan --campaign-mode preflight_only`` 展开全部 Job（零写入），运行期变量保留占位并能逐步绑定。
* 验收二：证据标签逐连接顺序测试——每个 probe 的逐连接标签与 2.1.226 实测的中继有效连接序列逐条匹配（方法与
  路径、单连接请求数、合成计划的干预动作），任何错位都失败关闭；非主模型 probe 的标签照主模型复制，另与 2.1.226
  模型能力 Campaign 的实测序列逐条核对，已知例外写死。
* 候选 Job（B4-01）：12 个候选 Job 逐一镜像主模型 official Job（同一 probe，动作体换成宿主侧候选采集脚本包着中继候选
  模式），候选变量、attempt 级要求两个字段与 Job 合同逐项核对；候选标签顺序测试——由 2.1.226 实测序列推出网关应有的
  最终出站（只留网关转发的 messages／count_tokens，账号刷新触发另记 OAuth 刷新），逐连接与连接池两种形态都命中候选
  标签计划，出现不转发的端点或零连接 probe 出现连接即失败关闭。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import re
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import claude_upgrade_wire_projection as wire_projection
from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
from tools.official_client_capture.capturelib import claude_fw_f_v5 as v5
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import job_dependency_analyzer as analyzer
from tools.official_client_capture.tests.claude_current_fixture_inputs import freeze_current_scenario_inputs

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
TARGET = "2.1.285"
BASELINE = "2.1.226"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_2_1_226_relay_connection_sequences.json"
# 2.1.226 模型能力 Campaign 里 33 个非主模型 probe 的逐连接序列（只读抽取，口径同上）。
MODEL_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_2_1_226_model_capability_connection_sequences.json"
MODEL_JOB_PREFIX = "official-model-capability-"
# 模型能力实测与照主模型复制的标签不同的已知例外：两个非主模型的 TUI 启动序列缺 oauth_profile 连接（当次运行器状态与
# 主模型取证不同）。标签仍以主模型序列为初始模型；VC-1 实测不同即由封存编目失败关闭并修订标签。
MODEL_SEQUENCE_EXCEPTIONS = frozenset(
    f"{model}-{probe}" for model in ("claude-opus-5", "claude-fable-5") for probe in ("v4-replay-tui", "v4-tui-count-tokens")
)
# 设计 B2-05 规定的变量合同；实施补充 capture_root、capture_tool_root、host_runtime_receipt_sha256 与 B2-06 补充的
# synthetic_capture_container（合成 probe 的断网容器，见各自说明）。
DESIGN_VARIABLES = {
    "campaign_id", "campaign_dir", "target_version", "target_sha256", "runtime_image", "capture_container", "claude_bin",
    "model", "upstream_ip", "credentials_file", "ca_signing_pem", "ca_cert", "host_runtime_receipt", "run_nonce",
}
SUPPLEMENTED_VARIABLES = {"capture_root", "capture_tool_root", "host_runtime_receipt_sha256", "synthetic_capture_container"}
# 候选阶段变量（B4-01）：只被候选 Job 引用，official-only 的 Campaign 可以为空。
CANDIDATE_VARIABLES = set(plan.CANDIDATE_CAMPAIGN_VARIABLES)
CANDIDATE_JOB_PREFIX = "candidate-"
# 网关转发上游的端点（其余官方端点网关不转发）。
GATEWAY_FORWARDED = {"POST /v1/messages", "POST /v1/messages/count_tokens"}
RUNTIME_VARIABLES = {"host_runtime_receipt", "host_runtime_receipt_sha256", "run_nonce"}
SHA_A = "a" * 64
IMAGE = "oauth-egress-capture-capture-cli@sha256:" + "b" * 64


def _scenario_payload() -> dict:
    return json.loads(plan.scenario_manifest_path(TARGET).read_text(encoding="utf-8"))


def _official_jobs(payload: dict) -> list[dict]:
    return [job for job in payload["capture_jobs"] if job["phase"] == "official"]


def _candidate_jobs(payload: dict) -> list[dict]:
    return [job for job in payload["capture_jobs"] if job["phase"] == "candidate"]


def _baseline_rules() -> tuple[str, ...]:
    return plan.load_baseline_rules(plan.baseline_rule_manifest_path(BASELINE), BASELINE)


def _model_capability_probes() -> list[str]:
    """Active（基线）投影策略推出的非主模型 probe：模型能力目录引用的 probe 去掉主模型的基础 probe。"""

    policy, _binding = release.active_projection_policy(BASELINE)
    return sorted(probe for probe in wire_projection.catalog_probes(policy) if probe not in v5.PROBE_IDS)


def _capability_argv(primary_step: dict, model: str, job_id: str) -> list[str]:
    """主模型步骤 argv 换成非主模型步骤应有的 argv：只换 --run-id、--output-root 与 --model 三处取值。"""

    base = primary_step["probe_id"]
    expected = list(primary_step["argv"])
    replacements = {
        "--run-id": f"{{campaign_id}}-{model}-{base}",
        "--output-root": f"{{capture_root}}/runs/claude-official/{{campaign_id}}/{job_id}/{model}-{base}",
        "--model": model,
    }
    for flag, value in replacements.items():
        expected[expected.index(flag) + 1] = value
    return expected


def _plan_argv(
    campaign_dir: Path, *extra: str, campaign_id: str = "claude-code-2_1_285-t1", scenario_manifest: Path | None = None,
) -> list[str]:
    return [
        "plan", "--campaign-dir", str(campaign_dir), "--campaign-mode", "preflight_only",
        "--campaign-purpose", "validation_only", "--baseline-version", BASELINE, "--target-version", TARGET,
        "--target-sha256", SHA_A,
        *(["--scenario-manifest", str(scenario_manifest)] if scenario_manifest is not None else []),
        "--runtime-image", IMAGE, "--model", "claude-sonnet-5",
        "--upstream-ip", "160.79.104.10", "--campaign-id", campaign_id, *extra,
    ]


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class ScenarioManifestTests(unittest.TestCase):
    """场景清单结构、绑定与变量合同。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.scenario_inputs = freeze_current_scenario_inputs(
            plan.scenario_manifest_path(TARGET), Path(directory.name).resolve() / "current-scenario-inputs"
        )

    def test_real_manifest_validates_and_binds(self) -> None:
        payload = plan.read_scenario_manifest(self.scenario_inputs.manifest_path, expected_version=TARGET, rules=_baseline_rules())
        self.assertEqual((payload["schema_version"], payload["claude_version"]), (plan.SCENARIO_SCHEMA, TARGET))
        self.assertEqual(payload["rule_manifest"]["path"], f"tools/official_client_capture/claude_upgrade_rules_{TARGET.replace('.', '_')}.json")
        guide = REPOSITORY_ROOT / payload["source_spec"]["path"]
        self.assertEqual(plan.guide_section_sha256(guide, "第二部分"), payload["source_spec"]["sha256"])
        # official：12 个主模型 Job 加 2 个非主模型的模型能力取证 Job（claude-opus-5、claude-fable-5）；候选：逐一镜像
        # 12 个主模型 Job（B4-01），排在全部 official Job 之后。
        official = _official_jobs(payload)
        self.assertEqual((len(payload["capture_jobs"]), len(official)), (26, 14))
        self.assertEqual(payload["capture_jobs"][:14], official)
        self.assertEqual(
            [job["id"] for job in official if job["id"].startswith(MODEL_JOB_PREFIX)],
            [f"{MODEL_JOB_PREFIX}claude-opus-5", f"{MODEL_JOB_PREFIX}claude-fable-5"],
        )
        self.assertEqual(
            [job["id"] for job in _candidate_jobs(payload)],
            [CANDIDATE_JOB_PREFIX + job["id"][len("official-"):] for job in official if not job["id"].startswith(MODEL_JOB_PREFIX)],
        )
        self.assertEqual(payload["required_client_bindings"], ["claude-desktop", "claude-vscode"])
        self.assertEqual(payload["required_boundary_checks"], ["codex-final-wire-isolation", "third-party-ingress-rejection"])

    def test_variable_contract_covers_design_and_scopes(self) -> None:
        contract = {item["name"]: item for item in _scenario_payload()["variable_contract"]}
        self.assertEqual(set(contract), DESIGN_VARIABLES | SUPPLEMENTED_VARIABLES | CANDIDATE_VARIABLES)
        self.assertEqual({name for name, item in contract.items() if item["scope"] == "step_runtime"}, RUNTIME_VARIABLES)
        self.assertTrue(all(item["sensitive"] is False for item in contract.values()))
        self.assertEqual(
            set(plan.CAMPAIGN_VARIABLES) | CANDIDATE_VARIABLES,
            {name for name, item in contract.items() if item["scope"] == "campaign"},
        )
        self.assertFalse(set(plan.CAMPAIGN_VARIABLES) & CANDIDATE_VARIABLES)
        self.assertEqual(contract["upstream_ip"]["type"], "ip_address")
        # 候选变量都不必填：容器名有缺省，账号与 API Key 的 ID 只登记 ID、没有缺省（候选 run 之前失败关闭核对）。
        self.assertTrue(all(contract[name]["required"] is False for name in CANDIDATE_VARIABLES))
        self.assertEqual(
            {name: contract[name].get("default") for name in ("service_container", "keeper_container", "postgres_container")},
            {"service_container": "sub2apiplus", "keeper_container": "sub2apiplus-keeper", "postgres_container": "sub2apiplus-postgres"},
        )
        for name in ("claude_account_id", "api_key_id"):
            self.assertEqual((contract[name]["type"], "default" in contract[name]), ("integer", False))

    def test_schema_file_mirrors_validator(self) -> None:
        schema = json.loads((TOOL_ROOT / "claude_upgrade_scenarios.schema.json").read_text(encoding="utf-8"))
        definitions = schema["$defs"]
        self.assertEqual(set(schema["required"]), set(plan._SCENARIO_TOP_FIELDS))
        self.assertEqual(set(definitions["captureJob"]["required"]), set(plan._JOB_FIELDS))
        self.assertEqual(set(definitions["commandStep"]["required"]), set(plan._STEP_FIELDS))
        self.assertEqual(set(definitions["candidateCommandStep"]["required"]), set(plan._STEP_FIELDS))
        properties = schema["properties"]
        self.assertEqual(set(properties["required_client_bindings"]["items"]["enum"]), set(plan.REQUIRED_CLIENT_BINDINGS))
        self.assertEqual(set(properties["required_boundary_checks"]["items"]["enum"]), set(plan.REQUIRED_BOUNDARY_CHECKS))
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
        model_job = lambda payload: next(job for job in payload["capture_jobs"] if job["id"].startswith(MODEL_JOB_PREFIX))  # noqa: E731
        model_step = lambda payload: model_job(payload)["steps"][0]  # noqa: E731
        scenario = lambda payload, sid: next(item for item in payload["evidence_scenarios"] if item["scenario_id"] == sid)  # noqa: E731

        def drop_flag(payload, flag):
            argv = first_step(payload)["argv"]
            position = argv.index(flag)
            del argv[position:position + 2]

        def set_flag(step, flag, value):
            step["argv"][step["argv"].index(flag) + 1] = value

        def model_step_also_in_discovery(payload):
            # 非主模型步骤兼归另一个发现项场景（Job 的 scenario_ids 同步，绕过前面的闭合检查）。
            scenario(payload, "aux.models")["probe_ids"].append(model_step(payload)["probe_id"])
            model_job(payload)["scenario_ids"] = sorted([*model_job(payload)["scenario_ids"], "aux.models"])

        def primary_step_in_capability(payload):
            scenario(payload, plan.MODEL_CAPABILITY_SCENARIO)["probe_ids"].append(first_step(payload)["probe_id"])
            first_job(payload)["scenario_ids"] = sorted([*first_job(payload)["scenario_ids"], plan.MODEL_CAPABILITY_SCENARIO])

        def capability_as_rule_evidence(payload):
            scenario(payload, plan.MODEL_CAPABILITY_SCENARIO).update(purpose="rule_evidence", covers=["SPEC-BODY-001"])

        # 候选 Job（B4-01）：第一个候选 Job 的第一步（真实上游）与合成重试候选 Job 的第一步。
        candidate_job = lambda payload: _candidate_jobs(payload)[0]  # noqa: E731
        candidate_step = lambda payload: candidate_job(payload)["steps"][0]  # noqa: E731
        synthetic_candidate_step = lambda payload: next(  # noqa: E731
            job for job in _candidate_jobs(payload) if job["id"] == "candidate-synthetic-retry"
        )["steps"][0]

        def set_inner(step, flag, value):
            step["argv"][step["argv"].index(flag) + 1] = value

        def drop_inner(step, flag):
            position = step["argv"].index(flag)
            del step["argv"][position:position + 2]

        def mirror_model_probe(payload):
            # 候选步骤去镜像非主模型 probe：probe_id 与中继 probe 不再相等。
            step = candidate_step(payload)
            step["probe_id"] = "claude-opus-5-" + step["probe_id"]

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
            "official 步骤冒充候选 Job": (lambda p: first_job(p).update(phase="candidate"), "claude_run_candidate_capture.sh"),
            "fw-f-v5-probe 与 probe_id 不符": (
                lambda p: first_step(p)["argv"].__setitem__(first_step(p)["argv"].index("--fw-f-v5-probe") + 1, "v4-other"),
                "fw-f-v5-probe",
            ),
            "仍带 v4 开关": (lambda p: first_step(p)["argv"].extend(["--fw-f-v4-probe", first_step(p)["probe_id"]]), "只能用 FW-F v5"),
            "probe 不在 v5 目录": (
                lambda p: [
                    first_step(p).update(probe_id="v4-unknown"),
                    first_step(p)["argv"].__setitem__(first_step(p)["argv"].index("--probe-id") + 1, "v4-unknown"),
                    first_step(p)["argv"].__setitem__(first_step(p)["argv"].index("--fw-f-v5-probe") + 1, "v4-unknown"),
                ],
                "不在 FW-F v5 目录",
            ),
            "目录模块指向别处": (
                lambda p: first_step(p)["argv"].__setitem__(
                    first_step(p)["argv"].index("--fw-f-v5-catalog") + 1, "{capture_tool_root}/capturelib/claude_fw_f_v4.py"
                ),
                "--fw-f-v5-catalog",
            ),
            "缺版本事实摘要": (lambda p: drop_flag(p, "--fw-f-v5-version-facts-sha256"), "--fw-f-v5-version-facts-sha256"),
            "步骤版本事实摘要与顶层不符": (
                lambda p: first_step(p)["argv"].__setitem__(first_step(p)["argv"].index("--fw-f-v5-version-facts-sha256") + 1, SHA_A),
                "--fw-f-v5-version-facts-sha256",
            ),
            "顶层版本事实绑定缺摘要": (lambda p: p["version_facts"].pop("sha256"), "version_facts"),
            "目录绑定仍指向 v4": (
                lambda p: p["probe_catalog"].update(path="tools/official_client_capture/capturelib/claude_fw_f_v4.py"),
                "FW-F v5",
            ),
            "目录 schema 仍是 v4": (
                lambda p: p["probe_catalog"].update(schema_version="claude-code-fw-f-v4-scenario-catalog/v1"), "probe_catalog"
            ),
            "真实步骤缺上游 IP": (lambda p: drop_flag(p, "--upstream-ip"), "upstream-ip"),
            "probe 执行两次": (
                lambda p: p["capture_jobs"][1]["steps"].append(copy.deepcopy(first_step(p))),
                "只能由一个 official 取证步骤",
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
            # 模型能力取证（B2-07 补充项）：步骤身份、模型名与场景归属。
            "主模型步骤写字面模型名": (lambda p: set_flag(first_step(p), "--model", "claude-sonnet-5"), "与 probe_id 不一致"),
            "非主模型步骤沿用模型变量": (lambda p: set_flag(model_step(p), "--model", "{model}"), "与 probe_id 不一致"),
            "非主模型步骤身份与模型不符": (lambda p: set_flag(model_step(p), "--model", "claude-fable-5"), "与 probe_id 不一致"),
            "非主模型步骤中继跑模型名": (
                lambda p: [set_flag(model_step(p), flag, model_step(p)["probe_id"]) for flag in ("--probe-id", "--fw-f-v5-probe")],
                "与 probe_id 不一致|不在 FW-F v5 目录",
            ),
            "非主模型步骤兼归其他场景": (model_step_also_in_discovery, "必须且只能归入"),
            "主模型步骤归入模型能力场景": (primary_step_in_capability, "必须且只能归入"),
            "模型能力场景证明规则": (capability_as_rule_evidence, "发现项"),
            "不覆盖规则的 Job 承载规则场景": (lambda p: first_job(p).update(covers=[]), "只能承载发现项场景"),
            "目录 probe 数计入非主模型步骤": (lambda p: p["probe_catalog"].update(probe_count=112), "probe_count"),
            # 候选合同（B4-01）。
            "候选步骤不经候选采集脚本": (
                lambda p: candidate_step(p).update(argv=candidate_step(p)["argv"][2:]), "claude_run_candidate_capture.sh"
            ),
            "候选步骤进断网容器": (
                lambda p: synthetic_candidate_step(p)["argv"].__setitem__(4, "{synthetic_capture_container}"),
                "docker exec {capture_container}",
            ),
            "候选步骤读取官方凭据": (
                lambda p: candidate_step(p)["argv"].extend(["--claude-credentials-file", "{credentials_file}"]),
                "不得携带 --claude-credentials-file",
            ),
            "候选步骤缺运行目录": (lambda p: drop_inner(candidate_step(p), "--candidate-runtime-dir"), "--candidate-runtime-dir"),
            "候选中继端口漂移": (lambda p: set_inner(candidate_step(p), "--candidate-relay-port", "18444"), "--candidate-relay-port"),
            "候选环境少一个键": (lambda p: candidate_step(p)["environment"].pop("CA_CERT"), "environment 键"),
            "候选环境与命令不一致": (
                lambda p: candidate_step(p)["environment"].update(OUTPUT_ROOT="{capture_root}/runs/elsewhere"), "OUTPUT_ROOT"
            ),
            "候选环境开关被改": (
                lambda p: candidate_step(p)["environment"].update(CLAUDE_CANDIDATE_CAPTURE="yes"), "CLAUDE_CANDIDATE_CAPTURE"
            ),
            "候选账号引用别的变量": (
                lambda p: candidate_step(p)["environment"].update(ACCOUNT_ID="{api_key_id}"), "ACCOUNT_ID"
            ),
            "候选触发方式漂移": (
                lambda p: candidate_step(p)["environment"].update(GATEWAY_TRIGGER="gateway-account-refresh"), "GATEWAY_TRIGGER"
            ),
            "候选合成步骤冒充真实请求": (
                lambda p: [
                    synthetic_candidate_step(p)["argv"].__setitem__(
                        synthetic_candidate_step(p)["argv"].index("--acknowledge-synthetic-responses"), "--acknowledge-live-requests"
                    ),
                    synthetic_candidate_step(p)["argv"].extend(["--upstream-ip", "{upstream_ip}"]),
                ],
                "合成计划不一致",
            ),
            "候选镜像非主模型 probe": (mirror_model_probe, "必须等于 probe_id"),
            "同一 probe 两个候选步骤": (
                lambda p: _candidate_jobs(p)[1]["steps"].append(copy.deepcopy(candidate_step(p))),
                "只能由一个候选取证步骤",
            ),
            "候选 Job 漏掉 ScenarioPlan 场景": (
                lambda p: [job for job in _candidate_jobs(p) if job["id"] == "candidate-tui"][0].update(required=False),
                "没有覆盖 VC-3 ScenarioPlan 场景",
            ),
            # 客户端收据字段已放宽为词表子集（可为空），所以“少一项”不再是拒绝条件；改测未排序与重复。
            "客户端收据未排序": (lambda p: p["required_client_bindings"].reverse(), "required_client_bindings"),
            "客户端收据重复": (
                lambda p: p.update(required_client_bindings=["claude-desktop", "claude-desktop"]), "required_client_bindings",
            ),
            "未登记的客户端收据": (
                lambda p: p.update(required_client_bindings=["claude-desktop", "claude-vscode", "kilo-compatible"]),
                "required_client_bindings",
            ),
            "边界核对未排序": (lambda p: p["required_boundary_checks"].reverse(), "required_boundary_checks"),
            "缺边界核对字段": (lambda p: p.pop("required_boundary_checks"), "顶层字段不闭合"),
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
        # 非主模型的字面模型名不得等于 Campaign 主模型（同一模型在两个目录下各取一遍证）。
        plan.validate_campaign_context(payload, {**context, "model": "claude-sonnet-5"})
        with self.assertRaisesRegex(ConfigurationError, "与 Campaign 主模型 claude-opus-5 相同"):
            plan.validate_campaign_context(payload, {**context, "model": "claude-opus-5"})
        with self.assertRaisesRegex(ConfigurationError, "ip_address"):
            plan.validate_campaign_context(payload, {**context, "upstream_ip": "10.0.0.1"})
        root_runtime = copy.deepcopy(payload)
        root_runtime["capture_jobs"][0]["evidence_roots"] = ["{capture_root}/runs/{run_nonce}"]
        with self.assertRaisesRegex(ConfigurationError, "证据根不得引用运行期变量"):
            plan.validate_campaign_context(root_runtime, context)
        # 候选变量（B4-01）：取值按声明类型核对；没有候选变量声明的旧清单副本只容忍空取值。
        candidate_context = {**context, "service_container": "sub2apiplus", "claude_account_id": "7", "api_key_id": "9"}
        plan.validate_campaign_context(payload, candidate_context)
        with self.assertRaisesRegex(ConfigurationError, "integer"):
            plan.validate_campaign_context(payload, {**candidate_context, "claude_account_id": "seven"})
        legacy = copy.deepcopy(payload)
        legacy["capture_jobs"] = _official_jobs(legacy)
        legacy["variable_contract"] = [item for item in legacy["variable_contract"] if item["name"] not in CANDIDATE_VARIABLES]
        plan.validate_campaign_context(legacy, {**context, **{name: "" for name in CANDIDATE_VARIABLES}})
        with self.assertRaisesRegex(ConfigurationError, "变量合同外"):
            plan.validate_campaign_context(legacy, {**context, "claude_account_id": "7"})


class VersionFactsBindingTests(unittest.TestCase):
    """版本事实：顶层绑定的文件、摘要与目标版本闭合，每个步骤逐字携带同一份绑定；plan 时就失败关闭。"""

    def test_binding_matches_file_and_steps(self) -> None:
        payload = _scenario_payload()
        binding = payload["version_facts"]
        path = REPOSITORY_ROOT / binding["path"]
        self.assertEqual(plan.file_sha256(path), binding["sha256"])
        facts, loaded = v5.load_version_facts(path.resolve(), binding["sha256"], expected_version=TARGET)
        self.assertEqual((facts["claude_version"], facts["advisor"]["tool_type"]), (TARGET, "advisor_20260301"))
        self.assertEqual(loaded["sha256"], binding["sha256"])
        expected = {
            "--fw-f-v5-catalog": "{capture_tool_root}/capturelib/claude_fw_f_v5.py",
            "--fw-f-v5-version-facts": "{capture_tool_root}/capturelib/claude_fw_f_v5_version_facts_2_1_285.json",
            "--fw-f-v5-version-facts-sha256": binding["sha256"],
        }
        for job in payload["capture_jobs"]:
            for step in job["steps"]:
                with self.subTest(probe=step["probe_id"]):
                    argv = step["argv"]
                    for flag, value in expected.items():
                        self.assertEqual(argv[argv.index(flag) + 1], value)
                    self.assertNotIn("--fw-f-v4-probe", argv)
                    self.assertIn("capturelib/claude_fw_f_v5.py", job["tool_dependencies"])
                    self.assertIn("capturelib/claude_fw_f_v5_version_facts_2_1_285.json", job["tool_dependencies"])

    def test_read_rejects_drifted_or_foreign_facts(self) -> None:
        """在临时仓库根里放改过的版本事实（不写受管树）：其他版本、字段不闭合、类型非法、句式带换行、摘要漂移都拒绝。"""

        payload_template = _scenario_payload()
        binding = payload_template["version_facts"]
        original = json.loads((REPOSITORY_ROOT / binding["path"]).read_text(encoding="utf-8"))
        rule_relative = payload_template["rule_manifest"]["path"]

        def attempt(facts: dict | None, *, sha_override: str | None = None) -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for relative in (rule_relative, plan.PROBE_CATALOG_PATH):
                    (root / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(REPOSITORY_ROOT / relative, root / relative)
                facts_path = root / binding["path"]
                facts_path.write_text(json.dumps(facts if facts is not None else original, ensure_ascii=False), encoding="utf-8")
                digest = sha_override or plan.file_sha256(facts_path)
                payload = copy.deepcopy(payload_template)
                payload["version_facts"] = {"path": binding["path"], "sha256": digest}
                for job in payload["capture_jobs"]:
                    for step in job["steps"]:
                        step["argv"][step["argv"].index("--fw-f-v5-version-facts-sha256") + 1] = digest
                manifest = root / "scenarios.json"
                manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(plan, "REPOSITORY_ROOT", root):
                    plan.read_scenario_manifest(manifest, expected_version=TARGET, rules=_baseline_rules(), verify_source_spec=False)

        attempt(None)  # 对照：原样复制的版本事实照常通过，下面每个失败都只来自对应的改动。
        for label, mutate, message in (
            ("其他版本的事实", lambda facts: facts.update(claude_version="2.1.226"), "目标版本"),
            ("字段不闭合", lambda facts: facts.update(extra=1), "字段不闭合"),
            ("工具类型非法", lambda facts: facts["advisor"].update(tool_type="Advisor Tool"), "tool_type"),
            ("宣告句式带换行", lambda facts: facts["advisor"]["model_declaration"].update(prefix="a\nb"), "换行"),
        ):
            with self.subTest(label=label):
                facts = copy.deepcopy(original)
                mutate(facts)
                with self.assertRaisesRegex(ConfigurationError, message):
                    attempt(facts)
        with self.assertRaisesRegex(ConfigurationError, "摘要与场景清单冻结值不一致"):
            attempt(None, sha_override=SHA_A)


class CatalogConsistencyTests(unittest.TestCase):
    """主模型步骤与 FW-F v5 目录（与 v4 同构：49 维 79 probe）逐项一致；非主模型步骤逐字复制主模型步骤。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _scenario_payload()
        cls.steps = {step["probe_id"]: (job, step) for job in _official_jobs(cls.payload) for step in job["steps"]}
        cls.scenarios = {item["scenario_id"]: item for item in cls.payload["evidence_scenarios"]}
        cls.primary = {probe: pair for probe, pair in cls.steps.items() if plan.step_model(probe, pair[1]["argv"]) is None}
        cls.capability = {probe: pair for probe, pair in cls.steps.items() if probe not in cls.primary}

    def base_probe(self, probe_id: str) -> str:
        return str(plan.step_relay_probe(probe_id, self.steps[probe_id][1]["argv"]))

    def test_every_probe_runs_exactly_once(self) -> None:
        executed = [step["probe_id"] for job in _official_jobs(self.payload) for step in job["steps"]]
        self.assertEqual(len(executed), len(set(executed)))
        self.assertEqual(sorted(self.primary), sorted(v5.PROBE_IDS))
        self.assertEqual(v5.PROBE_IDS, v4.PROBE_IDS)
        # 非主模型步骤恰好是 Active 投影策略推出的 33 个 probe（opus、fable 各 16 个，另加 fable 的 server fallback）。
        self.assertEqual(sorted(self.capability), _model_capability_probes())
        self.assertEqual(len(self.capability), 33)
        catalog = self.payload["probe_catalog"]
        self.assertEqual((catalog["probe_count"], catalog["dimension_count"]), (len(v5.PROBES), len(v5.REQUIRED_MATRIX_DIMENSIONS)))
        self.assertEqual((catalog["probe_count"], catalog["dimension_count"]), (79, 49))
        self.assertEqual((catalog["path"], catalog["schema_version"]), (plan.PROBE_CATALOG_PATH, v5.SCHEMA_CATALOG))

    def test_dimension_and_condition_scenarios(self) -> None:
        for dimension in v5.REQUIRED_MATRIX_DIMENSIONS:
            with self.subTest(dimension=dimension):
                expected = sorted(pid for pid, probe in v5.PROBES.items() if dimension in probe.dimensions)
                self.assertEqual(self.scenarios[dimension]["probe_ids"], expected)
        conditions = {sid: item for sid, item in self.scenarios.items() if sid.startswith("condition.")}
        self.assertEqual(set(self.scenarios) - set(conditions) - {plan.MODEL_CAPABILITY_SCENARIO}, set(v5.REQUIRED_MATRIX_DIMENSIONS))
        members = sorted(pid for item in conditions.values() for pid in item["probe_ids"])
        self.assertEqual(members, sorted(pid for pid, probe in v5.PROBES.items() if not probe.dimensions))
        discovery = sorted(sid for sid, item in self.scenarios.items() if item["purpose"] == "discovery")
        self.assertEqual(discovery, ["agency.hook", "aux.model_capability", "aux.models", "header.dispatch_id", "header.usage_limit"])
        capability = self.scenarios[plan.MODEL_CAPABILITY_SCENARIO]
        self.assertEqual((capability["probe_ids"], capability["covers"]), (_model_capability_probes(), []))

    def test_model_capability_steps_copy_primary_steps(self) -> None:
        """非主模型步骤：中继跑主模型已执行的同名基础 probe，argv 只换 --run-id、--output-root 与 --model。"""

        for probe_id, (job, step) in sorted(self.capability.items()):
            model = plan.step_model(probe_id, step["argv"])
            base = self.base_probe(probe_id)
            with self.subTest(probe=probe_id):
                self.assertEqual(probe_id, f"{model}-{base}")
                _primary_job, primary_step = self.primary[base]
                self.assertEqual(step["argv"], _capability_argv(primary_step, str(model), job["id"]))
                self.assertEqual((step["environment"], step["timeout_seconds"]), (primary_step["environment"], primary_step["timeout_seconds"]))
                self.assertEqual(job["id"], f"{MODEL_JOB_PREFIX}{model}")
                self.assertEqual((job["covers"], job["scenario_ids"], job["required"]), ([], [plan.MODEL_CAPABILITY_SCENARIO], True))
                self.assertEqual(job["suites"], ["core", "full"])

    def test_steps_follow_probe_definitions(self) -> None:
        for probe_id, (job, step) in self.steps.items():
            probe = v5.PROBES[self.base_probe(probe_id)]
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
            probes = [v5.PROBES[self.base_probe(pid)] for pid in item["probe_ids"]]
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

    def test_official_and_candidate_jobs_cover_all_rules(self) -> None:
        rules = _baseline_rules()
        payload = _scenario_payload()
        for phase, jobs in (("official", _official_jobs(payload)), ("candidate", _candidate_jobs(payload))):
            with self.subTest(phase=phase):
                covered = {rule for job in jobs if job["required"] for rule in job["covers"]}
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
        self.scenario_inputs = freeze_current_scenario_inputs(
            plan.scenario_manifest_path(TARGET), self.root / "current-scenario-inputs"
        )

    def _argv(self, *extra: str, campaign_dir: Path | None = None, campaign_id: str = "claude-code-2_1_285-t1") -> list[str]:
        """所有当前计划路径均消费新冻结副本；旧模板仍只读保留。"""

        return _plan_argv(
            self.campaign_dir if campaign_dir is None else campaign_dir, *extra,
            campaign_id=campaign_id, scenario_manifest=self.scenario_inputs.manifest_path,
        )

    def test_preflight_plan_expands_all_jobs(self) -> None:
        code, stdout, stderr = _run_main(self._argv())
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        payload = _scenario_payload()
        self.assertEqual(report["schema_version"], plan.PLAN_REPORT_SCHEMA)
        self.assertEqual((report["campaign_created"], report["files_written"], report["live_request_count"]), (False, 0, 0))
        self.assertFalse(self.campaign_dir.exists())
        self.assertEqual([job["id"] for job in report["jobs"]], [job["id"] for job in payload["capture_jobs"]])
        self.assertEqual((report["job_count"], report["step_count"]), (26, 191))
        official_probes = [step["probe_id"] for job in report["jobs"] if job["phase"] == "official" for step in job["steps"]]
        self.assertEqual(sorted(official_probes), sorted([*v5.PROBE_IDS, *_model_capability_probes()]))
        candidate_probes = [step["probe_id"] for job in report["jobs"] if job["phase"] == "candidate" for step in job["steps"]]
        self.assertEqual(sorted(candidate_probes), sorted(v5.PROBE_IDS))
        self.assertEqual((report["coverage_plan"]["official_unmapped"], report["coverage_plan"]["candidate_unmapped"]), ([], []))
        # 候选变量写进 configuration：容器名取变量合同缺省，账号与 API Key 的 ID 没给就是空字符串（预检 Campaign 允许）。
        self.assertEqual(
            {name: report["configuration"][name] for name in sorted(CANDIDATE_VARIABLES)},
            {"api_key_id": "", "claude_account_id": "", "keeper_container": "sub2apiplus-keeper",
             "postgres_container": "sub2apiplus-postgres", "service_container": "sub2apiplus"},
        )
        self.assertEqual(report["required_rules"], list(_baseline_rules()))
        self.assertEqual(report["configuration"]["claude_bin"], f"/opt/claude-{TARGET}/claude")
        self.assertEqual(report["configuration"]["capture_root"], "/capture")
        self.assertEqual(
            (report["configuration"]["capture_container"], report["configuration"]["synthetic_capture_container"]),
            ("capture-cli", "capture-cli-synthetic"),
        )
        for name, path in (
            ("target_scenarios", self.scenario_inputs.manifest_path),
            ("evidence_labels", plan.evidence_labels_path(TARGET)),
            ("target_rules", plan.rule_manifest_path(TARGET)),
            ("baseline_rules", plan.baseline_rule_manifest_path(BASELINE)),
        ):
            self.assertEqual(report["inputs"][name]["sha256"], plan.file_sha256(path), name)
        for job in report["jobs"]:
            side = "claude-official" if job["phase"] == "official" else "claude-candidate"
            self.assertEqual(job["evidence_roots"], [f"/capture/runs/{side}/claude-code-2_1_285-t1/{job['id']}"])
            for step in job["steps"]:
                with self.subTest(probe=step["probe_id"]):
                    self.assertEqual(set(step["runtime_variables"]), RUNTIME_VARIABLES)
                    placeholders = {field for item in step["argv"] for field in plan.TEMPLATE_FIELD_RE.findall(item)}
                    self.assertEqual(placeholders, RUNTIME_VARIABLES)
                    self.assertIn(f"claude-code-2_1_285-t1-{step['probe_id']}", step["argv"])
                    self.assertIn(TARGET, step["argv"])

    def test_runtime_binding(self) -> None:
        _code, stdout, _stderr = _run_main(self._argv())
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
            "formal 只由 VC-0 收口建立": (self._argv()[:4] + ["formal"] + self._argv()[5:], "VC-0 收口"),
            "私网上游": ([*self._argv()[:-4], "--upstream-ip", "10.1.2.3", "--campaign-id", "claude-code-2_1_285-t1"], "ip_address"),
            "目标版本无清单": ([item if item != TARGET else "2.1.299" for item in _plan_argv(self.campaign_dir)], "场景清单"),
            "run-id 超长": (self._argv(campaign_id="c" * 100), "run-id"),
            "相对目录": (self._argv(campaign_dir=Path("relative/campaign")), "安全绝对路径"),
            "模型名非法": ([item if item != "claude-sonnet-5" else "bad model" for item in self._argv()], "--model 格式非法"),
            "合成容器与抓包容器相同": (
                self._argv("--synthetic-capture-container", "capture-cli"), "独立的断网容器"
            ),
            "主模型与非主模型取证模型相同": (
                [item if item != "claude-sonnet-5" else "claude-fable-5" for item in self._argv()],
                "与 Campaign 主模型 claude-fable-5 相同",
            ),
            "候选账号 ID 非整数": (self._argv("--claude-account-id", "acct-7"), "--claude-account-id 必须是正整数"),
            "候选服务容器名非法": (self._argv("--service-container", "bad name"), "--service-container 格式非法"),
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
        code, _stdout, stderr = _run_main(self._argv("--scenario-manifest", str(tampered)))
        self.assertEqual(code, 1)
        self.assertIn("摘要漂移", stderr)

    def test_orchestrator_wire_roots(self) -> None:
        parser = claude_upgrade._build_parser()
        arguments = parser.parse_args(self._argv())
        plan.prepare_plan_arguments(arguments)
        context = claude_upgrade._job_context(arguments)
        self.assertEqual(set(context), set(plan.CAMPAIGN_VARIABLES) | CANDIDATE_VARIABLES)
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
        code, stdout, stderr = _run_main(self._argv("--suite", "core"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["job_count"], len([job for job in _scenario_payload()["capture_jobs"] if "core" in job["suites"]]))


class EvidenceLabelSequenceTests(unittest.TestCase):
    """验收：证据标签逐连接顺序与 2.1.226 实测中继序列逐条一致；错位一律失败关闭。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _scenario_payload()
        cls.labels = plan.load_evidence_labels(plan.evidence_labels_path(TARGET), expected_version=TARGET, scenario_payload=cls.payload)
        # official 与候选按 Job 分开：候选步骤镜像同一 probe ID，按 probe 单键会互相覆盖。
        official_ids = {job["id"] for job in _official_jobs(cls.payload)}
        cls.plans = {
            probe: probe_plan
            for job_id, job_plans in cls.labels["plans"].items()
            if job_id in official_ids
            for probe, probe_plan in job_plans.items()
        }
        cls.candidate_plans = {
            probe: probe_plan
            for job_id, job_plans in cls.labels["plans"].items()
            if job_id not in official_ids
            for probe, probe_plan in job_plans.items()
        }
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.model_fixture = json.loads(MODEL_FIXTURE.read_text(encoding="utf-8"))
        cls.rules = {
            entry["job_id"]: entry["rules"] for entry in cls.labels["document"]["entries"]
        }

    def test_fixture_is_metadata_only_and_complete(self) -> None:
        for fixture, expected in ((self.fixture, sorted(v5.PROBE_IDS)), (self.model_fixture, _model_capability_probes())):
            self.assertEqual(fixture["claude_version"], BASELINE)
            self.assertEqual(sorted(fixture["probes"]), expected)
            for probe_id, record in fixture["probes"].items():
                for connection in record["connections"]:
                    with self.subTest(probe=probe_id, connection=connection["connection_id"]):
                        self.assertEqual(set(connection), {"connection_id", "requests", "intervention"})
                        self.assertTrue(all(plan.REQUEST_LINE_RE.fullmatch(item) for item in connection["requests"]))
                        self.assertNotIn(connection["connection_id"], record["excluded_connection_ids"])
        # 模型能力夹具另记中继清单的模型与基础 probe：目录名就是「<模型>-<基础 probe>」，中继跑的是主模型目录里的基础 probe。
        for probe_id, record in self.model_fixture["probes"].items():
            with self.subTest(probe=probe_id):
                self.assertEqual(probe_id, f"{record['model']}-{record['relay_probe']}")
                self.assertIn(record["relay_probe"], v5.PROBE_IDS)
                self.assertEqual((record["response_mode"], record["response_plan"]), ("live", None))

    def test_every_job_and_probe_has_labels(self) -> None:
        self.assertEqual(set(self.labels["plans"]), {job["id"] for job in self.payload["capture_jobs"]})
        self.assertEqual(sorted(self.plans), sorted([*v5.PROBE_IDS, *_model_capability_probes()]))
        self.assertEqual(sorted(self.candidate_plans), sorted(v5.PROBE_IDS))

    def test_label_sequences_match_observed_connections(self) -> None:
        """逐连接顺序：每个主模型 probe 的有效连接逐条命中标签规则（方法与路径、单连接请求数、合成干预动作）。"""

        for probe_id, probe_plan in sorted(self.plans.items()):
            if probe_id not in v5.PROBE_IDS:
                continue  # 非主模型 probe 见 test_model_capability_labels_follow_primary
            record = self.fixture["probes"][probe_id]
            with self.subTest(probe=probe_id):
                matches = plan.match_relay_connections(probe_plan, record["connections"])
                self.assertEqual([item["connection_id"] for item in matches], [c["connection_id"] for c in record["connections"]])
                self.assertEqual([item["ordinal"] for item in matches], list(range(1, len(matches) + 1)))

    def test_model_capability_labels_follow_primary(self) -> None:
        """非主模型 probe 的标签规则照主模型同名基础 probe 复制（只换 glob、probe 标签与场景，另加 model 标签）；逐连接
        计划命中主模型的 2.1.226 实测序列，也命中 2.1.226 模型能力实测序列——已知例外（TUI 缺 oauth_profile 启动连接）
        在这里写死：例外之外的 probe 必须匹配，例外必须仍然不匹配（实测或标签变了要回来改这张表）。"""

        primary_rules: dict[str, list[dict]] = {}
        for entry in self.labels["document"]["entries"]:
            if entry["side"] == "official" and not entry["job_id"].startswith(MODEL_JOB_PREFIX):
                for rule in entry["rules"]:
                    primary_rules.setdefault(rule["labels"]["probe"], []).append(rule)
        for entry in self.labels["document"]["entries"]:
            if not entry["job_id"].startswith(MODEL_JOB_PREFIX):
                continue
            model = entry["job_id"][len(MODEL_JOB_PREFIX):]
            by_probe: dict[str, list[dict]] = {}
            for rule in entry["rules"]:
                by_probe.setdefault(rule["labels"]["probe"], []).append(rule)
            for probe_id, rules in by_probe.items():
                base = probe_id[len(model) + 1:]
                with self.subTest(probe=probe_id):
                    self.assertEqual(probe_id, f"{model}-{base}")
                    expected = []
                    for rule in primary_rules[base]:
                        copied = copy.deepcopy(rule)
                        copied["glob"] = f"{probe_id}/" + rule["glob"][len(base) + 1:]
                        copied["scenario_ids"] = [plan.MODEL_CAPABILITY_SCENARIO]
                        copied["labels"] = {**rule["labels"], "probe": probe_id, "model": model}
                        copied["rationale"] = f"{model} 模型能力取证，照主模型 {base} 的规则：{rule['rationale']}"
                        expected.append(copied)
                    self.assertEqual(rules, expected)
        for probe_id in _model_capability_probes():
            record = self.model_fixture["probes"][probe_id]
            with self.subTest(probe=probe_id):
                primary = self.fixture["probes"][record["relay_probe"]]["connections"]
                self.assertTrue(plan.match_relay_connections(self.plans[probe_id], primary))
                if probe_id in MODEL_SEQUENCE_EXCEPTIONS:
                    with self.assertRaisesRegex(plan.LabelSequenceMismatch, "GET /api/oauth/profile"):
                        plan.match_relay_connections(self.plans[probe_id], record["connections"])
                else:
                    matches = plan.match_relay_connections(self.plans[probe_id], record["connections"])
                    self.assertEqual([item["connection_id"] for item in matches], [c["connection_id"] for c in record["connections"]])

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

    def candidate_expectations(self) -> dict[str, set[str]]:
        """候选 probe → 网关应有的上游请求行：官方序列里网关转发的端点；账号刷新触发只记网关自己的 OAuth 刷新。"""

        expected: dict[str, set[str]] = {}
        for probe_id in v5.PROBE_IDS:
            requests = {
                line.split("?", 1)[0]
                for connection in self.fixture["probes"][probe_id]["connections"]
                for line in connection["requests"]
            }
            if v5.PROBES[probe_id].driver == "oauth-refresh":
                expected[probe_id] = {"POST /v1/oauth/token"}
            else:
                expected[probe_id] = requests & GATEWAY_FORWARDED
        return expected

    def test_candidate_labels_mirror_official_rules(self) -> None:
        """候选标签：请求规则换成无序（网关转发的请求行）或零连接；其余规则逐字照抄官方，只加 egress 标签与说明前缀。"""

        official_rules = {
            (rule["labels"]["probe"], rule["glob"], rule["kind"]): rule
            for entry in self.labels["document"]["entries"]
            if entry["side"] == "official" and not entry["job_id"].startswith(MODEL_JOB_PREFIX)
            for rule in entry["rules"]
            if "connection" not in rule
        }
        expected = self.candidate_expectations()
        for entry in self.labels["document"]["entries"]:
            if entry["side"] != "candidate":
                continue
            for rule in entry["rules"]:
                probe_id = rule["labels"]["probe"]
                with self.subTest(job=entry["job_id"], glob=rule["glob"]):
                    self.assertEqual(rule["labels"]["egress"], "candidate-gateway")
                    if "connection" in rule:
                        allowed = expected[probe_id]
                        self.assertEqual(
                            rule["connection"],
                            {"order": "unordered", "requests": sorted(allowed)} if allowed else {"order": "none"},
                        )
                        self.assertNotIn("endpoint", rule["labels"])
                        continue
                    official = official_rules[(probe_id, rule["glob"], rule["kind"])]
                    copied = copy.deepcopy(official)
                    copied["labels"]["egress"] = "candidate-gateway"
                    self.assertEqual({key: value for key, value in rule.items() if key != "rationale"},
                                     {key: value for key, value in copied.items() if key != "rationale"})
                    self.assertTrue(rule["rationale"].startswith("候选网关镜像："))
        zero = sorted(probe for probe, allowed in expected.items() if not allowed)
        self.assertEqual(zero, ["v4-replay-custom-agent-safe-mode", "v4-replay-custom-header-invalid-name", "v4-tui-usage"])

    def test_candidate_label_sequences_match_gateway_egress(self) -> None:
        """候选标签顺序测试：由 2.1.226 实测序列推出的网关出站，逐连接与连接池两种形态都命中候选计划。"""

        for probe_id, allowed in sorted(self.candidate_expectations().items()):
            probe_plan = self.candidate_plans[probe_id]
            with self.subTest(probe=probe_id):
                lines = [
                    line.split("?", 1)[0]
                    for connection in self.fixture["probes"][probe_id]["connections"]
                    for line in connection["requests"]
                    if line.split("?", 1)[0] in allowed
                ] or (sorted(allowed) if allowed else [])
                if not allowed:
                    self.assertEqual(plan.match_relay_connections(probe_plan, []), [])
                    with self.assertRaisesRegex(plan.LabelSequenceMismatch, "零连接规则"):
                        plan.match_relay_connections(
                            probe_plan, [{"connection_id": 1, "requests": ["POST /v1/messages"], "intervention": None}]
                        )
                    continue
                separate = [
                    {"connection_id": index, "requests": [line], "intervention": None} for index, line in enumerate(lines, 1)
                ]
                pooled = [{"connection_id": 3, "requests": lines, "intervention": None}]
                for connections in (separate, pooled):
                    matches = plan.match_relay_connections(probe_plan, connections)
                    self.assertEqual([item["connection_id"] for item in matches], [c["connection_id"] for c in connections])

    def test_candidate_mismatches_fail_closed(self) -> None:
        baseline = self.candidate_plans["v4-replay-baseline"]
        cases = {
            "网关转发了不转发的端点": ([{"connection_id": 1, "requests": ["HEAD /api/hello"], "intervention": None}], "未声明"),
            "启动端点混进推理连接": (
                [{"connection_id": 1, "requests": ["POST /v1/messages", "GET /api/claude_code/settings"], "intervention": None}],
                "未声明",
            ),
            "网关零出站": ([], "至少要有一条"),
        }
        for label, (connections, message) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(plan.LabelSequenceMismatch, message):
                    plan.match_relay_connections(baseline, connections)

    def test_label_declaration_rejections(self) -> None:
        original = json.loads(plan.evidence_labels_path(TARGET).read_text(encoding="utf-8"))

        def first_rule(document, probe_id="v4-native-tls-baseline"):
            for entry in document["entries"]:
                for rule in entry["rules"]:
                    if rule["labels"].get("probe") == probe_id and "connection" in rule:
                        return entry, rule
            raise AssertionError(probe_id)

        def candidate_rule(document, probe_id):
            for entry in document["entries"]:
                if entry["side"] != "candidate":
                    continue
                for rule in entry["rules"]:
                    if rule["labels"].get("probe") == probe_id and "connection" in rule:
                        return rule
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
            "非主模型标签越出模型能力场景": (
                lambda d: first_rule(d, "claude-opus-5-v4-replay-tui")[1]["scenario_ids"].append("entrypoint.tui"), "子集"
            ),
            "official 侧声明零连接": (
                lambda d: [
                    rule.update(connection={"order": "none"})
                    for rule in d["entries"][0]["rules"]
                    if rule["labels"].get("probe") == "v4-native-tls-baseline" and "connection" in rule
                ],
                "零连接规则必须是该 probe 唯一|不得声明零连接",
            ),
            "零连接规则 order 非法": (
                lambda d: candidate_rule(d, "v4-tui-usage").update(connection={"order": "unordered"}), "零连接规则只能是 order=none"
            ),
            "候选 side 写成 official": (lambda d: d["entries"][-1].update(side="official"), "side 与 Job 阶段不一致"),
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


class CandidateMirrorTests(unittest.TestCase):
    """候选 Job（B4-01）逐一镜像主模型 official Job：同一 probe、同一覆盖，动作体换成宿主侧候选采集脚本包着中继候选模式。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _scenario_payload()
        cls.official = {job["id"]: job for job in _official_jobs(cls.payload) if not job["id"].startswith(MODEL_JOB_PREFIX)}
        cls.candidates = _candidate_jobs(cls.payload)

    @staticmethod
    def expected_step(official_job_id: str, candidate_job_id: str, step: dict) -> dict:
        """独立复算候选步骤：容器固定联网抓包容器，去掉官方凭据，输出根换到 claude-candidate，加运行目录与中继端口。"""

        probe = step["probe_id"]
        inner = ["docker", "exec", "{capture_container}", *step["argv"][3:]]
        position = inner.index("--claude-credentials-file")
        del inner[position:position + 2]
        output_root = f"{{capture_root}}/runs/claude-candidate/{{campaign_id}}/{candidate_job_id}/{probe}"
        self_check = inner[inner.index("--output-root") + 1]
        assert self_check == f"{{capture_root}}/runs/claude-official/{{campaign_id}}/{official_job_id}/{probe}", self_check
        inner[inner.index("--output-root") + 1] = output_root
        runtime_dir = f"{{capture_root}}/runtime/claude-candidate/{{campaign_id}}-{probe}"
        inner += ["--candidate-runtime-dir", runtime_dir, "--candidate-relay-port", "18443"]
        trigger = "gateway-account-refresh" if v5.PROBES[probe].driver == "oauth-refresh" else "client"
        return {
            "probe_id": probe,
            "argv": ["bash", "{capture_tool_root}/claude_run_candidate_capture.sh", *inner],
            "environment": {
                "CLAUDE_CANDIDATE_CAPTURE": "YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE",
                "CAMPAIGN_DIR": "{campaign_dir}",
                "CAPTURE_ROOT": "{capture_root}",
                "CAPTURE_CONTAINER": "{capture_container}",
                "SERVICE_CONTAINER": "{service_container}",
                "KEEPER_CONTAINER": "{keeper_container}",
                "POSTGRES_CONTAINER": "{postgres_container}",
                "ACCOUNT_ID": "{claude_account_id}",
                "API_KEY_ID": "{api_key_id}",
                "RUN_ID": f"{{campaign_id}}-{probe}",
                "PROBE_ID": probe,
                "OUTPUT_ROOT": output_root,
                "CANDIDATE_RUNTIME_DIR": runtime_dir,
                "RELAY_PORT": "18443",
                "GATEWAY_TRIGGER": trigger,
                "CA_CERT": "{ca_cert}",
            },
            "timeout_seconds": step["timeout_seconds"] + 600,
        }

    def test_jobs_mirror_primary_official_jobs(self) -> None:
        self.assertEqual(len(self.candidates), len(self.official))
        for candidate in self.candidates:
            official_id = "official-" + candidate["id"][len(CANDIDATE_JOB_PREFIX):]
            official = self.official[official_id]
            with self.subTest(job=candidate["id"]):
                for field in ("suites", "scenario_ids", "covers", "required", "required_scenario_receipts"):
                    self.assertEqual(candidate[field], official[field], field)
                self.assertEqual(candidate["evidence_roots"], [f"{{capture_root}}/runs/claude-candidate/{{campaign_id}}/{candidate['id']}"])
                self.assertEqual(
                    candidate["steps"], [self.expected_step(official_id, candidate["id"], step) for step in official["steps"]]
                )
                self.assertIn(plan.CANDIDATE_SCRIPT, candidate["tool_dependencies"])

    def test_triggers_and_plan_contract_agree(self) -> None:
        refresh = [
            step["probe_id"]
            for job in self.candidates
            for step in job["steps"]
            if step["environment"]["GATEWAY_TRIGGER"] == plan.CANDIDATE_TRIGGER_ACCOUNT_REFRESH
        ]
        self.assertEqual(refresh, ["v4-oauth-refresh"])
        for job in self.candidates:
            for step in job["steps"]:
                with self.subTest(probe=step["probe_id"]):
                    self.assertEqual(step["environment"]["GATEWAY_TRIGGER"], plan.candidate_trigger(step["probe_id"]))
                    synthetic = "--acknowledge-synthetic-responses" in step["argv"]
                    self.assertEqual(synthetic, bool(v5.PROBES[step["probe_id"]].response_plan))
                    self.assertEqual("--upstream-ip" in step["argv"], not synthetic)
                    self.assertNotIn("{credentials_file}", step["argv"])


class ClientBindingSubsetTests(unittest.TestCase):
    """required_client_bindings 放宽为词表子集（可为空）：校验器、schema 与各消费端对空列表／单项都泛化处理。

    本轮（2.1.309）清单登记空列表，代表验收范围不含 Desktop／VS Code；2.1.285 仍恰好两项，必须继续通过。
    """

    def _validate(self, bindings: list) -> None:
        payload = copy.deepcopy(_scenario_payload())
        payload["required_client_bindings"] = bindings
        plan.validate_scenario_manifest(payload)

    def test_empty_single_and_both_pass(self) -> None:
        for bindings in ([], ["claude-desktop"], ["claude-vscode"], ["claude-desktop", "claude-vscode"]):
            with self.subTest(bindings=bindings):
                self._validate(bindings)

    def test_unknown_unsorted_duplicate_and_wrong_type_rejected(self) -> None:
        cases = {
            "未知客户端名": ["claude-desktop", "kilo-compatible"],
            "只有未知客户端名": ["kilo-compatible"],
            "未排序": ["claude-vscode", "claude-desktop"],
            "重复": ["claude-desktop", "claude-desktop"],
            "含空串": [""],
            "不是列表": "claude-desktop",
        }
        for label, bindings in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ConfigurationError, "required_client_bindings"):
                self._validate(bindings)

    def test_boundary_checks_stay_exact(self) -> None:
        """放宽只针对客户端收据：边界核对字段仍必须恰好两项，不得为空或缺一。"""

        for boundary in ([], ["third-party-ingress-rejection"]):
            payload = copy.deepcopy(_scenario_payload())
            payload["required_boundary_checks"] = boundary
            with self.subTest(boundary=boundary), self.assertRaisesRegex(ConfigurationError, "required_boundary_checks"):
                plan.validate_scenario_manifest(payload)

    def test_schema_allows_empty_subset_of_the_vocabulary(self) -> None:
        schema = json.loads((TOOL_ROOT / "claude_upgrade_scenarios.schema.json").read_text(encoding="utf-8"))
        clients = schema["properties"]["required_client_bindings"]
        self.assertEqual((clients["minItems"], clients["maxItems"], clients["uniqueItems"]), (0, 2, True))
        boundary = schema["properties"]["required_boundary_checks"]
        self.assertEqual((boundary["minItems"], boundary["maxItems"]), (2, 2))

    def test_registered_manifests_still_pass_read(self) -> None:
        """2.1.285（恰好两项）与 2.1.309（空列表）都能经正式读取入口；2.1.285 的来源章节摘要是既有陈旧值，走冻结副本。"""

        rules = _baseline_rules()
        with tempfile.TemporaryDirectory() as directory:
            inputs = freeze_current_scenario_inputs(plan.scenario_manifest_path("2.1.285"), Path(directory).resolve() / "inputs")
            old = plan.read_scenario_manifest(inputs.manifest_path, expected_version="2.1.285", rules=rules)
        self.assertEqual(old["required_client_bindings"], ["claude-desktop", "claude-vscode"])
        new = plan.read_scenario_manifest(plan.scenario_manifest_path("2.1.309"), expected_version="2.1.309", rules=rules)
        self.assertEqual(new["required_client_bindings"], [])

    def test_consumers_handle_empty_client_bindings(self) -> None:
        """逐个核对消费端对空客户端收据登记的行为：不暂停、不拒绝合法输入，只拒绝清单没登记的 --client-evidence。"""

        from tools.official_client_capture import claude_upgrade_acceptance as acceptance
        from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
        from tools.official_client_capture import claude_upgrade_evidence as evidence

        scenarios = {"required_client_bindings": [], "required_boundary_checks": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # seal：没有登记的客户端不允许带 --client-evidence。
            self.assertEqual(candidate_seal._client_evidence(root, scenarios, []), {"client_bindings": {}, "boundary_checks": {}})
            with self.assertRaisesRegex(ConfigurationError, "不在场景清单登记"):
                candidate_seal._client_evidence(root, scenarios, [f"claude-desktop={root / 'x.json'}"])
            # acceptance：两边皆空通过；候选给出多余的客户端收据即不一致。
            manifest = {"campaign_id": "c1"}
            candidate = {"candidate_id": "cand", "client_evidence": {"client_bindings": {}, "boundary_checks": {}}}
            attempt = {"attempt_id": "a1", "run_nonce": "n1"}
            frozen = root / "scenarios.json"
            frozen.write_text(json.dumps(scenarios), encoding="utf-8")
            with mock.patch.object(acceptance.claude_upgrade_seal, "_frozen_input", return_value=frozen):
                self.assertEqual(
                    acceptance.client_receipts(root, manifest, candidate, attempt),
                    {"client_bindings": {}, "boundary_checks": {}},
                )
                candidate["client_evidence"]["client_bindings"] = {"claude-desktop": {"path": "x", "sha256": "0" * 64}}
                with self.assertRaisesRegex(ConfigurationError, "client_bindings 与场景清单登记的不一致"):
                    acceptance.client_receipts(root, manifest, candidate, attempt)
        # evidence：空登记下没有客户端缺失原因；多给未登记的客户端收据失败关闭。
        view = {
            "scenarios": scenarios, "attempt": {"attempt_id": "a1", "run_nonce": "n1"}, "campaign_id": "c1", "candidate_id": "cand",
            "client_evidence": {"client_bindings": {}, "boundary_checks": {}},
        }
        self.assertEqual(
            evidence._candidate_client_evidence(view),
            {"client_bindings": {}, "boundary_checks": {}, "reasons": []},
        )
        view["scenarios"] = {"required_client_bindings": [], "required_boundary_checks": ["third-party-ingress-rejection"]}
        self.assertEqual(evidence._candidate_client_evidence(view)["reasons"], ["boundary_check_missing"])
        view["client_evidence"]["client_bindings"] = {"claude-desktop": {"path": "x", "payload": {}}}
        with self.assertRaisesRegex(evidence.SealEvidenceError, "没有登记"):
            evidence._candidate_client_evidence(view)


# 已登记的目标版本：新增登记必须同步补这张表（否则下面的逐版校验会失败并提示），避免新登记绕过逐项审计。
REGISTERED_TARGETS = ("2.1.285", "2.1.309")
# 各版本的非主模型能力取证 Job（顺序即清单顺序）；主模型是 Campaign 变量，不在清单里。
EXPECTED_MODEL_JOBS = {
    "2.1.285": ("claude-opus-5", "claude-fable-5"),
    "2.1.309": ("claude-opus-5-5", "claude-fable-5-1", "claude-haiku-5-5"),
}
# 各版本的客户端收据登记：2.1.309 本轮验收范围不含 Desktop／VS Code（登记为未验证）。
EXPECTED_CLIENT_BINDINGS = {"2.1.285": ["claude-desktop", "claude-vscode"], "2.1.309": []}
# 冻结的官方二进制摘要（版本事实出处的 binary_sha256）。
EXPECTED_BINARY_SHA256 = {
    "2.1.285": "24fac77749bed3d91365d6b6915aa4b824e14318ecb6bc17adbc192f01c9173d",
    "2.1.309": "9796dd26899337f687739a0b39a7353b639f062a3436a7b941fcf19a1b757a9b",
}
# server fallback 锁存只在 fable 上取证（Active 投影策略的 server_fallbacks）。
SERVER_FALLBACK_PROBE = "v4-bash"


def _underscored(version: str) -> str:
    return version.replace(".", "_")


def _facts_relative(version: str) -> str:
    return f"capturelib/claude_fw_f_v5_version_facts_{_underscored(version)}.json"


def _registered_versions() -> list[str]:
    prefix = plan.SCENARIO_PREFIX
    return sorted(
        path.name[len(prefix):-len(".json")].replace("_", ".") for path in TOOL_ROOT.glob(f"{prefix}[0-9]*.json")
    )


class RegisteredTargetsTests(unittest.TestCase):
    """对每个已登记的目标版本逐一校验四个登记文件，并审计新版相对上一版模板只有允许的几类差异。"""

    def test_registered_set_is_known(self) -> None:
        self.assertEqual(_registered_versions(), sorted(REGISTERED_TARGETS), "新增登记请同步 REGISTERED_TARGETS 等期望表与差异审计")

    def _model_base_probes(self) -> list[str]:
        policy, _binding = release.active_projection_policy(BASELINE)
        capability = (wire_projection.PROBE_COUNT_TOKENS, wire_projection.PROBE_THINKING_DISABLED,
                      *(wire_projection.effort_probe(effort) for effort in policy["efforts"]))
        return sorted({*wire_projection.MODEL_SCENARIO_PROBES, *capability})

    def _load(self, version: str) -> tuple[dict, dict]:
        rules = _baseline_rules()
        if version == "2.1.285":
            # 2.1.285 清单内的来源章节摘要是既有陈旧值，走冻结副本读取（与其余 2.1.285 测试一致）。
            with tempfile.TemporaryDirectory() as directory:
                inputs = freeze_current_scenario_inputs(plan.scenario_manifest_path(version), Path(directory).resolve() / "inputs")
                payload = plan.read_scenario_manifest(inputs.manifest_path, expected_version=version, rules=rules)
        else:
            # 其他版本按正式严格模式读取：必须钉住当前第二部分摘要。
            payload = plan.read_scenario_manifest(plan.scenario_manifest_path(version), expected_version=version, rules=rules)
        labels = plan.load_evidence_labels(plan.evidence_labels_path(version), expected_version=version, scenario_payload=payload)
        return payload, labels

    def test_each_registered_target(self) -> None:
        rules = _baseline_rules()
        base_probes = self._model_base_probes()
        for version in REGISTERED_TARGETS:
            with self.subTest(version=version):
                payload, labels = self._load(version)
                u = _underscored(version)
                # 四个登记文件与版本命名。
                for path in (
                    plan.scenario_manifest_path(version), plan.rule_manifest_path(version), plan.evidence_labels_path(version),
                    TOOL_ROOT / _facts_relative(version),
                ):
                    self.assertTrue(path.is_file(), path)
                self.assertEqual((payload["claude_version"], payload["profile_id"]), (version, f"claude-code-{version}"))
                # 规则清单：40 条逐条承接基线，绑定文件摘要。
                target_rules = plan.load_target_rules(
                    plan.rule_manifest_path(version), target_version=version, baseline_rules=rules,
                    baseline_manifest=plan.baseline_rule_manifest_path(BASELINE),
                )
                self.assertEqual(target_rules, rules)
                self.assertEqual(payload["rule_manifest"]["sha256"], plan.file_sha256(plan.rule_manifest_path(version)))
                # 版本事实：文件、摘要、出处二进制摘要、advisor 事实，每个步骤逐字携带同一绑定。
                facts_path = TOOL_ROOT / _facts_relative(version)
                binding = payload["version_facts"]
                self.assertEqual(binding, {"path": f"tools/official_client_capture/{_facts_relative(version)}", "sha256": plan.file_sha256(facts_path)})
                facts, _loaded = v5.load_version_facts(facts_path.resolve(), binding["sha256"], expected_version=version)
                self.assertEqual(facts["provenance"]["binary_sha256"], EXPECTED_BINARY_SHA256[version])
                self.assertEqual(facts["advisor"]["tool_type"], "advisor_20260301")
                self.assertEqual(facts["advisor"]["model_declaration"], {
                    "prefix": "[AdvisorTool] Server-side tool enabled with ", "suffix": " as the advisor model"})
                for job in payload["capture_jobs"]:
                    self.assertIn(f"capturelib/{facts_path.name}", job["tool_dependencies"])
                    for step in job["steps"]:
                        argv = step["argv"]
                        self.assertEqual(argv[argv.index("--fw-f-v5-version-facts") + 1], f"{{capture_tool_root}}/{_facts_relative(version)}")
                        self.assertEqual(argv[argv.index("--fw-f-v5-version-facts-sha256") + 1], binding["sha256"])
                # 其他已登记版本的专属文件名不得出现在本版四个文件里（描述、路径都不带上一版版本号）。
                for other in REGISTERED_TARGETS:
                    if other == version:
                        continue
                    for path in (plan.scenario_manifest_path(version), plan.rule_manifest_path(version),
                                 plan.evidence_labels_path(version), facts_path):
                        text = path.read_text(encoding="utf-8")
                        self.assertNotIn(other, text, f"{path.name} 带有 {other}")
                        self.assertNotIn(_underscored(other), text, f"{path.name} 带有 {_underscored(other)}")
                # 作业结构：12 个主模型 Job + 非主模型 Job + 12 个候选 Job（候选排在最后）。
                models = EXPECTED_MODEL_JOBS[version]
                official, candidate = _official_jobs(payload), _candidate_jobs(payload)
                self.assertEqual((len(official), len(candidate)), (12 + len(models), 12))
                self.assertEqual(payload["capture_jobs"][: len(official)], official)
                self.assertEqual([job["id"] for job in official if job["id"].startswith(MODEL_JOB_PREFIX)],
                                 [f"{MODEL_JOB_PREFIX}{model}" for model in models])
                self.assertEqual(payload["required_client_bindings"], EXPECTED_CLIENT_BINDINGS[version])
                self.assertEqual(payload["required_boundary_checks"], ["codex-final-wire-isolation", "third-party-ingress-rejection"])
                # 非主模型 Job 的 probe：每个模型一套基础 probe，fable 另加 server fallback 锁存。
                all_capability: list[str] = []
                for model in models:
                    job = next(item for item in official if item["id"] == f"{MODEL_JOB_PREFIX}{model}")
                    expected = sorted(f"{model}-{probe}" for probe in base_probes)
                    if "fable" in model:
                        expected = sorted([*expected, f"{model}-{SERVER_FALLBACK_PROBE}"])
                    self.assertEqual(sorted(step["probe_id"] for step in job["steps"]), expected, model)
                    all_capability.extend(expected)
                scenario = next(item for item in payload["evidence_scenarios"] if item["scenario_id"] == plan.MODEL_CAPABILITY_SCENARIO)
                self.assertEqual(scenario["probe_ids"], sorted(all_capability))
                self.assertEqual(scenario["covers"], [])
                self.assertIn(f"执行 {len(all_capability)} 个冻结 probe", scenario["trigger"])
                for model in models:
                    self.assertIn(model, scenario["trigger"])
                # 标签：每个 Job 都有；非主模型标签照主模型同名基础 probe 复制（同 2.1.285 的复制公式）。
                self.assertEqual(set(labels["plans"]), {job["id"] for job in payload["capture_jobs"]})
                self._assert_model_labels_follow_primary(labels)

    @staticmethod
    def _assert_model_labels_follow_primary(labels: dict) -> None:
        entries = labels["document"]["entries"]
        primary: dict[str, list[dict]] = {}
        for entry in entries:
            if entry["side"] == "official" and not entry["job_id"].startswith(MODEL_JOB_PREFIX):
                for rule in entry["rules"]:
                    primary.setdefault(rule["labels"]["probe"], []).append(rule)
        for entry in entries:
            if not entry["job_id"].startswith(MODEL_JOB_PREFIX):
                continue
            model = entry["job_id"][len(MODEL_JOB_PREFIX):]
            by_probe: dict[str, list[dict]] = {}
            for rule in entry["rules"]:
                by_probe.setdefault(rule["labels"]["probe"], []).append(rule)
            for probe_id, rules in by_probe.items():
                base = probe_id[len(model) + 1:]
                expected = []
                for rule in primary[base]:
                    copied = copy.deepcopy(rule)
                    copied["glob"] = f"{probe_id}/" + rule["glob"][len(base) + 1:]
                    copied["scenario_ids"] = [plan.MODEL_CAPABILITY_SCENARIO]
                    copied["labels"] = {**rule["labels"], "probe": probe_id, "model": model}
                    copied["rationale"] = f"{model} 模型能力取证，照主模型 {base} 的规则：{rule['rationale']}"
                    expected.append(copied)
                assert rules == expected, probe_id

    def test_2_1_309_differs_from_2_1_285_only_in_allowed_ways(self) -> None:
        """精确差异审计：把 2.1.285 的四个文件按“版本、模型、facts 摘要、第二部分摘要、模型 Job、客户端收据登记”做允许的
        变换，结果必须与 2.1.309 的文件逐项相等；tool_dependencies 单独核对（只允许版本事实文件名这一处不同）。
        2.1.285 退休时删除本用例。"""

        old, new = "2.1.285", "2.1.309"
        ou, nu = _underscored(old), _underscored(new)

        def raw(path: Path) -> str:
            return path.read_text(encoding="utf-8")

        def rename(text: str) -> str:
            text = text.replace(old, new).replace(ou, nu)
            for before, after in (("claude-opus-5", "claude-opus-5-5"), ("claude-fable-5", "claude-fable-5-1")):
                text = re.sub(re.escape(before) + r"\b", after, text)
            return text

        def with_haiku(items: list[dict], key: str, prefix_key: str) -> list[dict]:
            """在第一个候选项之前插入由 opus 条目复制并换模型名的 haiku 条目（标签与清单共用）。"""

            opus = next(item for item in items if item[key] == f"{prefix_key}claude-opus-5-5")
            haiku = json.loads(json.dumps(opus).replace("claude-opus-5-5", "claude-haiku-5-5"))
            position = next(index for index, item in enumerate(items) if item[key].startswith("candidate-"))
            return [*items[:position], haiku, *items[position:]]

        # facts：只有版本、出处二进制摘要与出处说明不同。
        facts_old = json.loads(raw(TOOL_ROOT / _facts_relative(old)))
        facts_new = json.loads(raw(TOOL_ROOT / _facts_relative(new)))
        for field in ("schema_version", "advisor"):
            self.assertEqual(facts_old[field], facts_new[field])
        self.assertEqual(facts_old["provenance"]["binary_platform"], facts_new["provenance"]["binary_platform"])
        self.assertEqual(facts_new["claude_version"], new)
        facts_sha_old, facts_sha_new = plan.file_sha256(TOOL_ROOT / _facts_relative(old)), plan.file_sha256(TOOL_ROOT / _facts_relative(new))
        # 规则清单：只有版本与描述里的版本号不同。
        self.assertEqual(json.loads(rename(raw(plan.rule_manifest_path(old)))), json.loads(raw(plan.rule_manifest_path(new))))
        rules_sha_old, rules_sha_new = plan.file_sha256(plan.rule_manifest_path(old)), plan.file_sha256(plan.rule_manifest_path(new))
        # 证据标签：版本、模型改名，另加 haiku 条目（opus 复制）。
        labels_expected = json.loads(rename(raw(plan.evidence_labels_path(old))))
        labels_expected["entries"] = with_haiku(labels_expected["entries"], "job_id", MODEL_JOB_PREFIX)
        self.assertEqual(labels_expected, json.loads(raw(plan.evidence_labels_path(new))))
        # 场景清单。
        current_spec = plan.guide_section_sha256(REPOSITORY_ROOT / "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", "第二部分")
        old_text = raw(plan.scenario_manifest_path(old))
        old_payload = json.loads(old_text)
        for before, after in ((facts_sha_old, facts_sha_new), (rules_sha_old, rules_sha_new), (old_payload["source_spec"]["sha256"], current_spec)):
            old_text = old_text.replace(before, after)
        expected = json.loads(rename(old_text))
        expected["capture_jobs"] = with_haiku(expected["capture_jobs"], "id", MODEL_JOB_PREFIX)
        expected["required_client_bindings"] = EXPECTED_CLIENT_BINDINGS[new]
        capability = next(item for item in expected["evidence_scenarios"] if item["scenario_id"] == plan.MODEL_CAPABILITY_SCENARIO)
        capability_probes = sorted(
            step["probe_id"] for job in expected["capture_jobs"] if job["id"].startswith(MODEL_JOB_PREFIX) for step in job["steps"]
        )
        capability["probe_ids"] = capability_probes
        actual = json.loads(raw(plan.scenario_manifest_path(new)))
        actual_capability = next(item for item in actual["evidence_scenarios"] if item["scenario_id"] == plan.MODEL_CAPABILITY_SCENARIO)
        # trigger 与前置条件第三条是描述文字：只核对模型清单与数量，再对齐后做整体相等。
        self.assertIn("执行 49 个冻结 probe", actual_capability["trigger"])
        self.assertEqual(len(capability_probes), 49)
        self.assertEqual(actual_capability["preconditions"][:2], capability["preconditions"][:2])
        capability["trigger"] = actual_capability["trigger"]
        capability["preconditions"] = actual_capability["preconditions"]
        self.assertIn("、".join(EXPECTED_MODEL_JOBS[new]), actual_capability["trigger"])
        self.assertIn("、".join(EXPECTED_MODEL_JOBS[new]), actual_capability["preconditions"][2])
        # tool_dependencies 另行核对：与 2.1.285 对应 Job 相比，只换版本事实文件名（模型 Job 对照 opus 的 2.1.285 Job）。
        deps_old = {job["id"]: job["tool_dependencies"] for job in old_payload["capture_jobs"]}
        for job in actual["capture_jobs"]:
            template_id = job["id"]
            if template_id.startswith(MODEL_JOB_PREFIX):
                template_id = f"{MODEL_JOB_PREFIX}claude-opus-5"
            self.assertEqual(
                sorted(job["tool_dependencies"]),
                sorted(_facts_relative(new) if item == _facts_relative(old) else item for item in deps_old[template_id]),
                job["id"],
            )
        for source in (expected, actual):
            for job in source["capture_jobs"]:
                job["tool_dependencies"] = None
        self.assertEqual(expected, actual)


class IdentityLayerTests(unittest.TestCase):
    """新文件按 Claude 策略归层；wire 根经 claude_upgrade_plan 按符号进入 Claude wire 闭包。"""

    def test_new_files_layers(self) -> None:
        policy = claude_identity.load_policy()
        expected = {
            "claude_upgrade_scenarios_2_1_285.json": "wire_producer",
            "claude_upgrade_evidence_labels_2_1_285.json": "evidence_semantics",
            "claude_upgrade_rules_2_1_285.json": "control",
            "claude_upgrade_scenarios_2_1_309.json": "wire_producer",
            "claude_upgrade_evidence_labels_2_1_309.json": "evidence_semantics",
            "claude_upgrade_rules_2_1_309.json": "control",
            "capturelib/claude_fw_f_v5_version_facts_2_1_285.json": "wire_producer",
            "capturelib/claude_fw_f_v5_version_facts_2_1_309.json": "wire_producer",
            "claude_upgrade_plan.py": "control",
            "claude_upgrade_scenarios.schema.json": "control",
            "claude_run_candidate_capture.sh": "wire_producer",
            "claude_fw_e_relay.py": "wire_producer",
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
