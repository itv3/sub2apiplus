"""Claude 升级的版本化清单与 plan 作业展开（Claude 升级控制面设计 B2-05）。

本模块装载并校验三份按目标版本冻结的机器清单，并把场景清单展开成取证 Job：

* ``claude_upgrade_scenarios_<ver>.json``（``claude-upgrade-scenarios/v1``，结构对齐 Codex 场景清单）：变量合同、证据场景
  （FW-F v5 目录的 49 个矩阵维度——与 v4 同一组——加没有维度的 probe 归入的条件场景）、版本事实绑定与 official 取证 Job
  （79 个 probe 归并为 Job，每个 probe 一步，动作体 ``claude_fw_e_relay.py capture --fw-f-v5-probe``，同时逐字携带目录
  模块与版本事实参数）；
* ``claude_upgrade_rules_<ver>.json``（``claude-upgrade-rule-manifest/v1``）：本轮逐规则重测的必需规则全集，绑定基线
  ``claude_required_rules_<baseline>.json`` 的路径与摘要；
* ``claude_upgrade_evidence_labels_<ver>.json``（``claude-upgrade-evidence-labels/v1``）：逐 Job 证据子路径的标签；中继
  请求按「有效连接序号」逐连接贴标签，``match_relay_connections`` 是封存编目（B2-07）与逐连接顺序测试共用的匹配器。

非主模型的模型能力取证（B2-07 补充项）沿用 2.1.226 模型能力 Campaign 的做法：中继仍跑 FW-F v5 的基础 probe（``--probe-id``
与 ``--fw-f-v5-probe`` 都是基础 probe），``--model`` 写字面模型名，步骤的 ``probe_id`` 与输出目录名取「<模型>-<probe>」，
全部归入发现项场景 ``aux.model_capability``（不覆盖必需规则，只供 VC-3 Wire 模型能力目录逐模型投影）。主模型步骤的
``--model`` 一律是 Campaign 变量 ``{model}``，非主模型的字面模型名在 plan 时不得等于它；非主模型步骤的基础 probe 必须
也由主模型步骤执行。``probe_catalog.probe_count`` 仍是 FW-F v5 目录的 probe 数（对应主模型步骤），非主模型步骤不计入。

变量分两种作用域：``campaign`` 在 plan 时代入；``step_runtime``（宿主运行凭据的容器路径与摘要、run nonce）由取证动作体
（B2-06）在每步执行前用 ``bind_step_runtime`` 绑定，plan 展开时原样保留占位，因此模板里除变量占位外不允许出现花括号。

本模块不导入 ``codex_upgrade`` 与任何被 Claude 身份策略忽略的文件。编排器的 wire 根 ``_load_plan_jobs``、
``_job_context``、``_scenario_job_execution_contract`` 转调这里，按符号进入 Claude 的 wire 闭包；本文件按
``claude_upgrade_`` 前缀归 control 层。版本事实校验与 v5 probe 集合取自 ``capturelib/claude_fw_f_v5``（与中继同一份
实现），它是 wire 层文件，按整文件进入 wire 闭包。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256


SCENARIO_SCHEMA = "claude-upgrade-scenarios/v1"
RULE_MANIFEST_SCHEMA = "claude-upgrade-rule-manifest/v1"
EVIDENCE_LABELS_SCHEMA = "claude-upgrade-evidence-labels/v1"
BASELINE_RULE_SCHEMA = "claude-code-required-rule-manifest/v1"
PLAN_REPORT_SCHEMA = "claude-upgrade-plan/v1"
# 取证 probe 目录：FW-F v5（与 v4 同一组 probe 与驱动，判定按版本事实取；见 capturelib/claude_fw_f_v5.py）。
PROBE_CATALOG_SCHEMA = "claude-code-fw-f-v5-scenario-catalog/v1"
PROBE_CATALOG_PATH = "tools/official_client_capture/capturelib/claude_fw_f_v5.py"

TOOL_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
SCENARIO_PREFIX = "claude_upgrade_scenarios_"
RULE_MANIFEST_PREFIX = "claude_upgrade_rules_"
EVIDENCE_LABELS_PREFIX = "claude_upgrade_evidence_labels_"
BASELINE_RULE_PREFIX = "claude_required_rules_"
# 规格章节锚点 → 章节标题；章节摘要从标题行算到下一个一级标题之前（与 Codex 冻结画像的章节摘要同一算法）。
GUIDE_SECTION_HEADINGS = {"第二部分": "# 第二部分 Claude Code 客户端规则画像"}
# 取证动作体：Claude 官方取证的每一步都是这一个受管脚本的 capture 子命令。
RELAY_SCRIPT = "claude_fw_e_relay.py"
# 步骤的执行容器占位：真实上游步骤进联网的抓包容器，合成步骤进断网容器（见 _validate_step）。
LIVE_CONTAINER_PLACEHOLDER = "{capture_container}"
SYNTHETIC_CONTAINER_PLACEHOLDER = "{synthetic_capture_container}"
# 中继请求字节文件的统一命名（中继按连接号写 connNNN.client_to_upstream.bin）。
RELAY_REQUEST_GLOB_SUFFIX = "relay/conn*.client_to_upstream.bin"
# v5 步骤的四个开关：probe、目录模块、版本事实文件与其冻结摘要（中继按路径加载目录模块，见中继模块说明）。
FW_F_PROBE_FLAG = "--fw-f-v5-probe"
FW_F_CATALOG_FLAG = "--fw-f-v5-catalog"
FW_F_VERSION_FACTS_FLAG = "--fw-f-v5-version-facts"
FW_F_VERSION_FACTS_SHA256_FLAG = "--fw-f-v5-version-facts-sha256"
# 其他目录版本的 probe 开关不得出现在升级取证步骤里。
LEGACY_PROBE_FLAGS = ("--fw-f-probe", "--fw-f-v4-probe", "--scenario")
TOOL_ROOT_PLACEHOLDER = "{capture_tool_root}"
TOOL_ROOT_RELATIVE = "tools/official_client_capture"

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# 非主模型（模型能力取证）步骤的字面模型名；主模型步骤的 --model 只能是 Campaign 变量占位。
MODEL_ID_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.-]{0,62}$")
MODEL_VARIABLE_PLACEHOLDER = "{model}"
# 非主模型取证步骤归入的发现项场景（不覆盖必需规则，见模块说明）。
MODEL_CAPABILITY_SCENARIO = "aux.model_capability"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+:-]+$")
IMMUTABLE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}$")
RULE_RE = re.compile(r"^SPEC-[A-Z0-9]+-[0-9]{3}$")
VARIABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TEMPLATE_FIELD_RE = re.compile(r"\{([^{}]*)\}")
SCENARIO_ID_RE = re.compile(
    r"^(?:transport|entrypoint|agency|header|body|tool|failure|aux|privacy|condition)\.[a-z0-9_]+$"
)
TOOL_DEPENDENCY_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
REQUEST_LINE_RE = re.compile(r"^(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) (/[A-Za-z0-9._~/-]*)$")
GLOB_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9._*-]*$")

VARIABLE_TYPES = frozenset({"string", "integer", "absolute_path", "sha256", "image_reference", "ip_address"})
VARIABLE_SCOPES = frozenset({"campaign", "step_runtime"})
SCENARIO_PURPOSES = frozenset({"rule_evidence", "discovery"})
JOB_PHASES = frozenset({"official", "candidate"})
JOB_SUITES = frozenset({"core", "full"})
# 候选取证（B4-01）：每一步是宿主侧候选采集脚本包着同一份中继的候选模式——脚本在网关隔离环境（账号临时代理、服务容器
# 临时信任抓包 CA、keeper 停、调度门清零）里转调 ``docker exec {capture_container} python3 …claude_fw_e_relay.py capture``，
# 退出前精确恢复并核验。候选步骤逐一镜像某个主模型 official probe（同一 probe ID），由同一官方客户端驱动，只是客户端经
# base URL 加专用 API Key 打候选网关，中继以 connect 模式记录网关的最终出站字节，两侧证据因此可以按同一 probe 配对。
CANDIDATE_SCRIPT = "claude_run_candidate_capture.sh"
CANDIDATE_CAPTURE_GATE = "YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE"
CANDIDATE_RUNTIME_DIR_FLAG = "--candidate-runtime-dir"
CANDIDATE_RELAY_PORT_FLAG = "--candidate-relay-port"
# 中继在抓包容器内的 connect 端口（网关账号临时代理指向它）；冻结在步骤里，脚本与中继逐字核对。
CANDIDATE_RELAY_PORT = "18443"
# 候选步骤的触发方式：client 由官方客户端驱动；gateway-account-refresh 由中继在抓包窗口内调用网关管理接口刷新账号令牌
# （官方 probe 的驱动是客户端自己的 OAuth 刷新，经网关时客户端只持有 API Key，对应的候选出站是网关自己的刷新请求）。
CANDIDATE_TRIGGER_CLIENT = "client"
CANDIDATE_TRIGGER_ACCOUNT_REFRESH = "gateway-account-refresh"
CANDIDATE_ENVIRONMENT_KEYS = frozenset(
    {
        "CLAUDE_CANDIDATE_CAPTURE",
        "CAMPAIGN_DIR",
        "CAPTURE_ROOT",
        "CAPTURE_CONTAINER",
        "SERVICE_CONTAINER",
        "KEEPER_CONTAINER",
        "POSTGRES_CONTAINER",
        "ACCOUNT_ID",
        "API_KEY_ID",
        "RUN_ID",
        "PROBE_ID",
        "OUTPUT_ROOT",
        "CANDIDATE_RUNTIME_DIR",
        "RELAY_PORT",
        "GATEWAY_TRIGGER",
        "CA_CERT",
    }
)
# 候选步骤环境里取 Campaign 变量的键（值必须逐字是这个占位）。
CANDIDATE_ENVIRONMENT_TEMPLATES: Mapping[str, str] = {
    "CAMPAIGN_DIR": "{campaign_dir}",
    "CAPTURE_ROOT": "{capture_root}",
    "CAPTURE_CONTAINER": LIVE_CONTAINER_PLACEHOLDER,
    "SERVICE_CONTAINER": "{service_container}",
    "KEEPER_CONTAINER": "{keeper_container}",
    "POSTGRES_CONTAINER": "{postgres_container}",
    "ACCOUNT_ID": "{claude_account_id}",
    "API_KEY_ID": "{api_key_id}",
}
# 候选阶段专用的 Campaign 变量：只被候选步骤引用。official-only 的 Campaign（预检、只取官方证据）可以为空，候选 run
# 之前由编排器失败关闭核对非空；它们不进 ``CAMPAIGN_VARIABLES``，因此不改变官方 Job 的启动合同与 Job 演练合同。
CANDIDATE_CAMPAIGN_VARIABLES = (
    "service_container",
    "keeper_container",
    "postgres_container",
    "claude_account_id",
    "api_key_id",
)
# 三官方客户端正例里只能在 Mac 上驱动的两个（B4-01，已定）：不做成候选 Job，按 Codex 第三方客户端收据机制，由 Mac 侧
# ``claude_drive_desktop.py`` 驱动并产出收据，候选 seal 用 ``--client-evidence`` 核对。CLI 正例由候选 Job 本身承担。
REQUIRED_CLIENT_BINDINGS = frozenset({"claude-desktop", "claude-vscode"})
# attempt 级边界核对（B4-01，已定；实现见 claude_upgrade_boundary_checks）：候选 Job 之后、seal 之前由
# capture-candidate boundary-check 执行，证据进 attempt 的 client 证据目录，由 seal 与 accept 核对。
#   codex-final-wire-isolation：structural（候选源码树与生产部署的 Codex 构建输入逐字节不变，零请求）＋ runtime（现有 Codex
#     候选采集脚本不改、只跑 candidate-core-mitm，Codex checker 逐条判定，选中观测的检查全过）；
#   third-party-ingress-rejection：第三方与非法形态请求在读取 OAuth 凭据前被拒（网关零出站）。
REQUIRED_BOUNDARY_CHECKS = frozenset({"codex-final-wire-isolation", "third-party-ingress-rejection"})
CAMPAIGN_MODES = frozenset({"preflight_only", "formal"})
CAMPAIGN_PURPOSES = frozenset({"validation_only", "production_replacement"})
# Claude 尚未登记需要场景真实性收据的目标场景；字段为对齐 Codex 结构而保留，只能为空。
SCENARIO_RECEIPT_SCENARIOS: frozenset[str] = frozenset()
# 会改变 Job 实际执行或封存判定的字段（对齐 Codex 的 SCENARIO_JOB_EXECUTION_FIELDS，Claude 没有 Lite 模型轨道字段）。
SCENARIO_JOB_EXECUTION_FIELDS = frozenset(
    {"id", "phase", "suites", "required", "steps", "evidence_roots", "required_scenario_receipts", "tool_dependencies"}
)
# 证据标签允许的 artifact kind 及各自 parser；派生只允许中继请求字节投影成 process_trace。
LABEL_KIND_PARSERS: Mapping[str, frozenset[str]] = {
    "relay_binary": frozenset({"opaque_bound_source"}),
    "relay_manifest": frozenset({"claude_relay_manifest"}),
    "synthetic_intervention": frozenset({"jsonl"}),
    "pcap": frozenset({"pcap_client_hello"}),
    "debug_log": frozenset({"opaque_bound_source"}),
}
DERIVE_TARGETS = frozenset({("h1_request_stream", "process_trace")})
CONNECTION_COUNTS = frozenset({"one", "one_or_more"})

_SCENARIO_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "claude_version",
        "profile_id",
        "source_spec",
        "rule_manifest",
        "probe_catalog",
        "version_facts",
        "variable_contract",
        "evidence_scenarios",
        "capture_jobs",
        "required_client_bindings",
        "required_boundary_checks",
    }
)
_VARIABLE_FIELDS = frozenset({"name", "type", "required", "sensitive", "scope", "description"})
_SCENARIO_FIELDS = frozenset(
    {"scenario_id", "purpose", "description", "trigger", "preconditions", "probe_ids", "required_artifact_kinds", "covers"}
)
_JOB_FIELDS = frozenset(
    {
        "id",
        "phase",
        "suites",
        "scenario_ids",
        "description",
        "required",
        "steps",
        "evidence_roots",
        "covers",
        "required_scenario_receipts",
        "tool_dependencies",
    }
)
_STEP_FIELDS = frozenset({"probe_id", "argv", "environment", "timeout_seconds"})
_LABEL_RULE_FIELDS = frozenset({"glob", "scenario_ids", "kind", "parser", "labels", "rationale"})


class LabelSequenceMismatch(ConfigurationError):
    """中继有效连接与证据标签声明的逐连接序列不一致：必须失败关闭，不得静默错标。"""


# ---------------------------------------------------------------------------
# 通用读取与摘要
# ---------------------------------------------------------------------------


def versioned_manifest_path(prefix: str, version: str) -> Path:
    """按版本号定位受管工具根下的版本化清单（``<prefix><主_次_补丁>.json``）。"""

    if not VERSION_RE.fullmatch(str(version)):
        raise ConfigurationError(f"版本号不是三段形式：{version!r}")
    return TOOL_ROOT / f"{prefix}{str(version).replace('.', '_')}.json"


def scenario_manifest_path(version: str) -> Path:
    return versioned_manifest_path(SCENARIO_PREFIX, version)


def rule_manifest_path(version: str) -> Path:
    return versioned_manifest_path(RULE_MANIFEST_PREFIX, version)


def evidence_labels_path(version: str) -> Path:
    return versioned_manifest_path(EVIDENCE_LABELS_PREFIX, version)


def baseline_rule_manifest_path(version: str) -> Path:
    return versioned_manifest_path(BASELINE_RULE_PREFIX, version)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    """读取可信普通文件里的 JSON 对象；符号链接、非对象一律拒绝。"""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}不存在或不是可信普通文件：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取{label} {path}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象：{path}")
    return payload


def _repository_file(relative: Any, label: str) -> Path:
    """把清单里的仓库相对路径解析为受管仓库内的普通文件。"""

    text = str(relative or "")
    candidate = Path(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts or "\\" in text:
        raise ConfigurationError(f"{label}路径必须是仓库内相对路径：{text!r}")
    path = REPOSITORY_ROOT / candidate
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}不存在或不是可信普通文件：{text}")
    return path


def _binding(path: Path) -> dict[str, str]:
    """输入文件的冻结绑定：位于仓库内时记仓库相对路径，否则记绝对路径；摘要按文件字节。"""

    resolved = Path(path).resolve()
    try:
        shown = resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        shown = str(resolved)
    return {"path": shown, "sha256": file_sha256(resolved)}


def guide_section_sha256(path: Path, fragment: str) -> str:
    """规格章节摘要：从锚点对应的一级标题行算到下一个一级标题之前（不含），按 UTF-8 字节取 SHA-256。"""

    heading = GUIDE_SECTION_HEADINGS.get(fragment)
    if heading is None:
        raise ConfigurationError(f"场景清单 source_spec 章节锚点不受支持：{fragment!r}")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"无法读取来源规格章节：{error}") from error
    start = next((index for index, line in enumerate(lines) if line.rstrip("\r\n") == heading), None)
    if start is None:
        raise ConfigurationError(f"来源规格缺少章节：{heading}")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("# ")), len(lines))
    return hashlib.sha256("".join(lines[start:end]).encode("utf-8")).hexdigest()


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unique_strings(value: Any, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item for item in value)
        and len(value) == len(set(value))
    )


# ---------------------------------------------------------------------------
# 规则清单
# ---------------------------------------------------------------------------


def load_baseline_rules(path: Path, baseline_version: str) -> tuple[str, ...]:
    """基线 RequiredRules 清单（``claude-code-required-rule-manifest/v1``）的规则编号，按清单顺序。"""

    payload = _read_json(Path(path), "基线规则清单")
    if payload.get("schema_version") != BASELINE_RULE_SCHEMA:
        raise ConfigurationError("基线规则清单 schema_version 不受支持。")
    if payload.get("target_version") != baseline_version:
        raise ConfigurationError("基线规则清单版本与 --baseline-version 不一致。")
    raw_rules = payload.get("required_rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigurationError("基线规则清单 required_rules 不能为空。")
    rules = [item.get("spec_id") if isinstance(item, Mapping) else None for item in raw_rules]
    if any(not isinstance(rule, str) or not RULE_RE.fullmatch(rule) for rule in rules):
        raise ConfigurationError("基线规则清单包含非法规则编号。")
    if len(rules) != len(set(rules)) or payload.get("required_rule_count") != len(rules):
        raise ConfigurationError("基线规则清单规则重复或与 required_rule_count 不一致。")
    return tuple(rules)  # type: ignore[arg-type]


def load_target_rules(
    path: Path,
    *,
    target_version: str,
    baseline_rules: Sequence[str],
    baseline_manifest: Path,
) -> tuple[str, ...]:
    """目标版本规则清单（``claude-upgrade-rule-manifest/v1``）：必须逐字承接基线规则全集与基线清单摘要。"""

    payload = _read_json(Path(path), "目标规则清单")
    expected_fields = {
        "schema_version",
        "claude_version",
        "baseline_version",
        "baseline_rule_manifest",
        "description",
        "required_rules",
    }
    if set(payload) != expected_fields or payload.get("schema_version") != RULE_MANIFEST_SCHEMA:
        raise ConfigurationError("目标规则清单字段不闭合或 schema_version 不受支持。")
    if payload.get("claude_version") != target_version:
        raise ConfigurationError("目标规则清单 claude_version 与目标版本不一致。")
    if not VERSION_RE.fullmatch(str(payload.get("baseline_version", ""))) or not _nonempty_text(payload.get("description")):
        raise ConfigurationError("目标规则清单 baseline_version 或说明非法。")
    binding = payload.get("baseline_rule_manifest")
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256"}:
        raise ConfigurationError("目标规则清单的基线绑定字段不闭合。")
    bound = _repository_file(binding.get("path"), "目标规则清单的基线绑定")
    if bound.resolve() != Path(baseline_manifest).resolve():
        raise ConfigurationError("目标规则清单绑定的基线清单与 --rule-manifest 不是同一文件。")
    if file_sha256(bound) != binding.get("sha256"):
        raise ConfigurationError("目标规则清单绑定的基线清单摘要漂移。")
    rules = payload.get("required_rules")
    if not _unique_strings(rules) or any(not RULE_RE.fullmatch(rule) for rule in rules):
        raise ConfigurationError("目标规则清单 required_rules 非法或重复。")
    if tuple(rules) != tuple(baseline_rules):
        raise ConfigurationError("目标规则清单必须按相同顺序逐条承接基线规则全集（VC-2 前不增删规则）。")
    return tuple(rules)


# ---------------------------------------------------------------------------
# 场景清单：结构校验
# ---------------------------------------------------------------------------


def _validate_variable(index: int, variable: Any, seen: set[str]) -> None:
    if (
        not isinstance(variable, dict)
        or not _VARIABLE_FIELDS.issubset(variable)
        or set(variable) - _VARIABLE_FIELDS - {"default"}
    ):
        raise ConfigurationError(f"场景变量 {index} 字段不闭合。")
    name = variable.get("name")
    if (
        not isinstance(name, str)
        or not VARIABLE_NAME_RE.fullmatch(name)
        or name in seen
        or variable.get("type") not in VARIABLE_TYPES
        or variable.get("scope") not in VARIABLE_SCOPES
        or not isinstance(variable.get("required"), bool)
        or variable.get("sensitive") is not False
        or not _nonempty_text(variable.get("description"))
    ):
        # 与 Codex 相同：秘密不得进入版本任务模板，因此 sensitive 只能为 false。
        raise ConfigurationError(f"场景变量 {index} 定义非法（名称、类型、作用域或 sensitive）。")
    if "default" in variable and (isinstance(variable["default"], bool) or not isinstance(variable["default"], (str, int))):
        raise ConfigurationError(f"场景变量 {name} 默认值非法。")
    if variable["scope"] == "step_runtime" and (variable["required"] is not True or "default" in variable):
        raise ConfigurationError(f"运行期变量 {name} 必须 required 且不得有默认值。")
    seen.add(name)


def _validate_evidence_scenario(index: int, scenario: Any, seen: set[str]) -> None:
    if not isinstance(scenario, dict) or set(scenario) != _SCENARIO_FIELDS:
        raise ConfigurationError(f"证据场景 {index} 字段不闭合。")
    scenario_id = scenario.get("scenario_id")
    if not isinstance(scenario_id, str) or not SCENARIO_ID_RE.fullmatch(scenario_id) or scenario_id in seen:
        raise ConfigurationError(f"证据场景 {index} scenario_id 非法或重复。")
    if scenario.get("purpose") not in SCENARIO_PURPOSES:
        raise ConfigurationError(f"证据场景 {scenario_id} purpose 非法。")
    if not _nonempty_text(scenario.get("description")) or not _nonempty_text(scenario.get("trigger")):
        raise ConfigurationError(f"证据场景 {scenario_id} 描述或触发条件非法。")
    for field in ("preconditions", "probe_ids", "required_artifact_kinds"):
        if not _unique_strings(scenario.get(field)):
            raise ConfigurationError(f"证据场景 {scenario_id} 的 {field} 非法。")
    if any(not SAFE_ID_RE.fullmatch(probe) for probe in scenario["probe_ids"]):
        raise ConfigurationError(f"证据场景 {scenario_id} 的 probe_ids 非法。")
    if not set(scenario["required_artifact_kinds"]).issubset(LABEL_KIND_PARSERS):
        raise ConfigurationError(f"证据场景 {scenario_id} 的 required_artifact_kinds 含未登记 kind。")
    covers = scenario.get("covers")
    if not _unique_strings(covers, allow_empty=True) or any(not RULE_RE.fullmatch(rule) for rule in covers):
        raise ConfigurationError(f"证据场景 {scenario_id} covers 非法。")
    # 发现项场景（2.1.226 实测未形成必需规则，如 Hook、dispatch）不覆盖规则；其余场景必须覆盖至少一条。
    if (scenario["purpose"] == "discovery") != (not covers):
        raise ConfigurationError(f"证据场景 {scenario_id} 的 purpose 与 covers 不一致。")
    seen.add(scenario_id)


def _argument_value(argv: Sequence[str], flag: str) -> str | None:
    """取 argv 里某个开关的唯一取值；出现多次或没有取值即返回 None 以便调用方拒绝。"""

    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        return None
    return argv[positions[0] + 1]


def _fw_f_v5() -> Any:
    """FW-F v5 目录模块（版本事实校验与 probe 集合的唯一来源，plan 与中继共用同一份实现）。"""

    from tools.official_client_capture.capturelib import claude_fw_f_v5

    return claude_fw_f_v5


def _tool_root_argument(repository_path: str) -> str:
    """仓库相对路径 → 步骤 argv 里的容器内坐标（``{capture_tool_root}/<工具根内相对路径>``）。"""

    relative = PurePosixPath(repository_path)
    try:
        inside = relative.relative_to(TOOL_ROOT_RELATIVE)
    except ValueError as error:
        raise ConfigurationError(f"取证步骤引用的文件必须在受管工具根 {TOOL_ROOT_RELATIVE} 下：{repository_path}") from error
    return f"{TOOL_ROOT_PLACEHOLDER}/{inside.as_posix()}"


def _step_catalog_arguments(payload: Mapping[str, Any]) -> dict[str, str]:
    """每个 v5 步骤必须逐字携带的目录模块与版本事实参数（取自清单顶层绑定）。"""

    facts = payload["version_facts"]
    return {
        FW_F_CATALOG_FLAG: _tool_root_argument(PROBE_CATALOG_PATH),
        FW_F_VERSION_FACTS_FLAG: _tool_root_argument(str(facts["path"])),
        FW_F_VERSION_FACTS_SHA256_FLAG: str(facts["sha256"]),
    }


def _validate_step(job_id: str, step_index: int, step: Any, *, catalog_arguments: Mapping[str, str]) -> str:
    if not isinstance(step, dict) or set(step) != _STEP_FIELDS:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 字段不闭合。")
    probe_id = step.get("probe_id")
    argv = step.get("argv")
    environment = step.get("environment")
    timeout = step.get("timeout_seconds")
    if not isinstance(probe_id, str) or not SAFE_ID_RE.fullmatch(probe_id):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 probe_id 非法。")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 argv 非法。")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 environment 非法。")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 timeout_seconds 非法。")
    # 每一步都是一次受管 R 取证：动作体、子命令与 probe 身份必须在 argv 里逐字可核。
    if not any(item == RELAY_SCRIPT or item.endswith(f"/{RELAY_SCRIPT}") for item in argv) or "capture" not in argv:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 不是 {RELAY_SCRIPT} capture 动作体。")
    relay_probe = step_relay_probe(probe_id, argv)
    if "--execute" not in argv or relay_probe is None:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 --execute／--probe-id／{FW_F_PROBE_FLAG} 与 probe_id 不一致。")
    if any(flag in argv for flag in LEGACY_PROBE_FLAGS):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 只能用 FW-F v5 目录，不得再带 {list(LEGACY_PROBE_FLAGS)}。")
    if relay_probe not in _fw_f_v5().PROBE_IDS:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 probe {relay_probe} 不在 FW-F v5 目录里。")
    # 目录模块与版本事实：逐字等于清单顶层绑定（版本事实的文件摘要随步骤 argv 进入 Job 执行摘要）。
    for flag, expected in catalog_arguments.items():
        if _argument_value(argv, flag) != expected:
            raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 {flag} 必须是 {expected}。")
    live = "--acknowledge-live-requests" in argv
    synthetic = "--acknowledge-synthetic-responses" in argv
    if live == synthetic:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 必须且只能确认真实请求或合成响应之一。")
    # 中继规则：真实上游场景必须冻结上游 IP，合成故障场景禁止携带。
    if live != ("--upstream-ip" in argv):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 --upstream-ip 与请求确认方式不一致。")
    # 执行容器：真实上游步骤只能在联网的抓包容器里执行，合成步骤只能在断网容器（network_mode none）里执行。
    # 合成计划不转发上游，但客户端仍会访问没被 hosts 覆盖的域名并携带真实凭据，只有断网容器能保证一个真实请求都发不出去
    # （2.1.226 的 FW-F 完整 Campaign 同样让合成 probe 走 --network none 的容器）。写错容器一律失败关闭。
    expected_container = LIVE_CONTAINER_PLACEHOLDER if live else SYNTHETIC_CONTAINER_PLACEHOLDER
    if list(argv[:3]) != ["docker", "exec", expected_container]:
        kind = "真实上游" if live else "合成"
        raise ConfigurationError(
            f"{job_id} 步骤 {step_index} 是{kind}步骤，必须以 docker exec {expected_container} 执行。"
        )
    return probe_id


def step_relay_probe(probe_id: str, argv: Sequence[str]) -> str | None:
    """步骤身份 → 中继执行的 FW-F v5 基础 probe；``--probe-id`` 与 ``--fw-f-v5-probe`` 不一致或身份对不上时为 None。

    主模型步骤：``probe_id`` 就是基础 probe，``--model`` 是 Campaign 变量 ``{model}``；非主模型（模型能力取证）步骤：
    ``probe_id`` 是「<字面模型名>-<基础 probe>」，``--model`` 是该字面模型名（见模块说明）。
    """

    relay_probe = _argument_value(argv, "--probe-id")
    if relay_probe is None or _argument_value(argv, FW_F_PROBE_FLAG) != relay_probe:
        return None
    model = _argument_value(argv, "--model")
    if probe_id == relay_probe:
        return relay_probe if model == MODEL_VARIABLE_PLACEHOLDER else None
    if model is not None and MODEL_ID_RE.fullmatch(model) and probe_id == f"{model}-{relay_probe}":
        return relay_probe
    return None


def step_model(probe_id: str, argv: Sequence[str]) -> str | None:
    """非主模型（模型能力取证）步骤的字面模型名；主模型步骤为 None。"""

    relay_probe = step_relay_probe(probe_id, argv)
    if relay_probe is None or probe_id == relay_probe:
        return None
    return _argument_value(argv, "--model")


def candidate_trigger(probe_id: str) -> str:
    """候选步骤的触发方式：官方 probe 的驱动是客户端 OAuth 刷新时由网关自己刷新账号令牌，其余由官方客户端驱动。"""

    probe = _fw_f_v5().get_probe(probe_id)
    return CANDIDATE_TRIGGER_ACCOUNT_REFRESH if probe.driver == "oauth-refresh" else CANDIDATE_TRIGGER_CLIENT


def _validate_candidate_step(job_id: str, step_index: int, step: Any, *, catalog_arguments: Mapping[str, str]) -> str:
    """候选步骤合同：宿主侧候选采集脚本包着中继候选模式的 capture 命令，脚本参数全部经环境变量逐字给出。"""

    if not isinstance(step, dict) or set(step) != _STEP_FIELDS:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 字段不闭合。")
    probe_id = step.get("probe_id")
    argv = step.get("argv")
    environment = step.get("environment")
    timeout = step.get("timeout_seconds")
    if not isinstance(probe_id, str) or not SAFE_ID_RE.fullmatch(probe_id):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 probe_id 非法。")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 argv 非法。")
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 environment 非法。")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 timeout_seconds 非法。")
    if list(argv[:2]) != ["bash", f"{TOOL_ROOT_PLACEHOLDER}/{CANDIDATE_SCRIPT}"]:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 必须以 bash {TOOL_ROOT_PLACEHOLDER}/{CANDIDATE_SCRIPT} 执行。")
    inner = list(argv[2:])
    # 候选步骤一律在联网的抓包容器里执行：网关经 docker 网络连接容器内的 connect 中继，断网容器连不到。合成计划的
    # 生产隔离由中继的合成画像（没有任何上游配置，未知 host/path 本地 421）与网关账号临时代理共同保证。
    if inner[:3] != ["docker", "exec", LIVE_CONTAINER_PLACEHOLDER]:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的中继命令必须以 docker exec {LIVE_CONTAINER_PLACEHOLDER} 执行。")
    if not any(item.endswith(f"/{RELAY_SCRIPT}") for item in inner) or "capture" not in inner or "--execute" not in inner:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 不是 {RELAY_SCRIPT} capture --execute 动作体。")
    relay_probe = step_relay_probe(probe_id, inner)
    if relay_probe != probe_id:
        # 候选只镜像主模型 probe：probe_id、--probe-id 与 --fw-f-v5-probe 三者相等，--model 是 Campaign 主模型变量。
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 --probe-id／{FW_F_PROBE_FLAG} 必须等于 probe_id 且 --model 为 {MODEL_VARIABLE_PLACEHOLDER}。")
    if any(flag in inner for flag in LEGACY_PROBE_FLAGS):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 只能用 FW-F v5 目录，不得再带 {list(LEGACY_PROBE_FLAGS)}。")
    if relay_probe not in _fw_f_v5().PROBE_IDS:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 probe {relay_probe} 不在 FW-F v5 目录里。")
    for flag, expected in catalog_arguments.items():
        if _argument_value(inner, flag) != expected:
            raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 {flag} 必须是 {expected}。")
    live = "--acknowledge-live-requests" in inner
    synthetic = "--acknowledge-synthetic-responses" in inner
    if live == synthetic:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 必须且只能确认真实请求或合成响应之一。")
    if live != ("--upstream-ip" in inner):
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 --upstream-ip 与请求确认方式不一致。")
    if bool(_fw_f_v5().get_probe(probe_id).response_plan) != synthetic:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的请求确认方式与 probe 的合成计划不一致。")
    # 候选客户端只持有网关 API Key，绝不读取官方 OAuth 凭据。
    if "--claude-credentials-file" in inner:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 是候选步骤，不得携带 --claude-credentials-file。")
    runtime_dir = _argument_value(inner, CANDIDATE_RUNTIME_DIR_FLAG)
    if runtime_dir is None or _argument_value(inner, CANDIDATE_RELAY_PORT_FLAG) != CANDIDATE_RELAY_PORT:
        raise ConfigurationError(
            f"{job_id} 步骤 {step_index} 必须带 {CANDIDATE_RUNTIME_DIR_FLAG} 与 {CANDIDATE_RELAY_PORT_FLAG} {CANDIDATE_RELAY_PORT}。"
        )
    if set(environment) != CANDIDATE_ENVIRONMENT_KEYS:
        raise ConfigurationError(
            f"{job_id} 步骤 {step_index} 的 environment 键必须恰好是 {sorted(CANDIDATE_ENVIRONMENT_KEYS)}。"
        )
    expected_environment = {
        **CANDIDATE_ENVIRONMENT_TEMPLATES,
        "CLAUDE_CANDIDATE_CAPTURE": CANDIDATE_CAPTURE_GATE,
        "RUN_ID": _argument_value(inner, "--run-id"),
        "PROBE_ID": probe_id,
        "OUTPUT_ROOT": _argument_value(inner, "--output-root"),
        "CANDIDATE_RUNTIME_DIR": runtime_dir,
        "RELAY_PORT": CANDIDATE_RELAY_PORT,
        "GATEWAY_TRIGGER": candidate_trigger(probe_id),
        # 候选脚本把中继签叶证书用的同一张抓包 CA 装进服务容器（网关才肯信任 connect 中继）。
        "CA_CERT": _argument_value(inner, "--ca-cert"),
    }
    drift = sorted(key for key, value in expected_environment.items() if environment.get(key) != value)
    if drift:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 environment 与中继命令或合同不一致：{drift}")
    return probe_id


def _validate_capture_job(index: int, job: Any, seen: set[str], *, catalog_arguments: Mapping[str, str]) -> None:
    if not isinstance(job, dict) or set(job) != _JOB_FIELDS:
        raise ConfigurationError(f"场景任务 {index} 字段不闭合（tool_dependencies 必须逐作业声明）。")
    job_id = job.get("id")
    if not isinstance(job_id, str) or not SAFE_ID_RE.fullmatch(job_id) or job_id in seen:
        raise ConfigurationError(f"场景任务 {index} id 非法或重复。")
    if job.get("phase") not in JOB_PHASES:
        raise ConfigurationError(f"{job_id} 的 phase 非法。")
    suites = job.get("suites")
    if not _unique_strings(suites) or not set(suites).issubset(JOB_SUITES):
        raise ConfigurationError(f"{job_id} 的 suites 非法。")
    if not _unique_strings(job.get("scenario_ids")):
        raise ConfigurationError(f"{job_id} 的 scenario_ids 非法。")
    if not _nonempty_text(job.get("description")) or not isinstance(job.get("required"), bool):
        raise ConfigurationError(f"{job_id} 的 description 或 required 非法。")
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigurationError(f"{job_id} 没有可执行步骤。")
    validate = _validate_step if job["phase"] == "official" else _validate_candidate_step
    for step_index, step in enumerate(steps, 1):
        validate(job_id, step_index, step, catalog_arguments=catalog_arguments)
    # Claude 每个 Job 只有一个证据根，各步输出根都是它下面以 probe 命名的子目录。
    if not _unique_strings(job.get("evidence_roots")) or len(job["evidence_roots"]) != 1:
        raise ConfigurationError(f"{job_id} 的 evidence_roots 必须恰好一个。")
    # 只承载发现项场景的 Job（模型能力取证）不覆盖规则；场景闭合（covers 为空时全部场景须是发现项）在清单级核对。
    covers = job.get("covers")
    if not _unique_strings(covers, allow_empty=True) or any(not RULE_RE.fullmatch(rule) for rule in covers):
        raise ConfigurationError(f"{job_id} 的 covers 非法。")
    receipts = job.get("required_scenario_receipts")
    if not isinstance(receipts, list) or not set(map(str, receipts)).issubset(SCENARIO_RECEIPT_SCENARIOS):
        raise ConfigurationError(f"{job_id} 的 required_scenario_receipts 只能声明已登记的场景（Claude 当前为空）。")
    dependencies = job.get("tool_dependencies")
    if (
        not isinstance(dependencies, list)
        or not dependencies
        or not all(isinstance(item, str) and TOOL_DEPENDENCY_PATH_RE.fullmatch(item) for item in dependencies)
        or dependencies != sorted(set(dependencies))
    ):
        raise ConfigurationError(f"{job_id} 的 tool_dependencies 必须是非空、排序去重的相对工具路径列表。")
    seen.add(job_id)


def validate_scenario_manifest(payload: Any) -> None:
    """``claude-upgrade-scenarios/v1`` 的失败关闭结构校验（不依赖第三方 JSON Schema 库）。"""

    if not isinstance(payload, dict) or set(payload) - {"$schema"} != _SCENARIO_TOP_FIELDS:
        raise ConfigurationError("场景清单顶层字段不闭合。")
    if payload.get("schema_version") != SCENARIO_SCHEMA:
        raise ConfigurationError("场景清单 schema_version 不受支持。")
    if not VERSION_RE.fullmatch(str(payload.get("claude_version", ""))):
        raise ConfigurationError("场景清单 claude_version 非法。")
    if not SAFE_ID_RE.fullmatch(str(payload.get("profile_id", ""))):
        raise ConfigurationError("场景清单 profile_id 非法。")
    source = payload.get("source_spec")
    if (
        not isinstance(source, dict)
        or set(source) != {"path", "fragment", "sha256"}
        or not _nonempty_text(source.get("path"))
        or source.get("fragment") not in GUIDE_SECTION_HEADINGS
        or not SHA256_RE.fullmatch(str(source.get("sha256", "")))
    ):
        raise ConfigurationError("场景清单 source_spec 绑定非法。")
    rule_binding = payload.get("rule_manifest")
    if (
        not isinstance(rule_binding, dict)
        or set(rule_binding) != {"path", "sha256", "rule_count"}
        or not _nonempty_text(rule_binding.get("path"))
        or not SHA256_RE.fullmatch(str(rule_binding.get("sha256", "")))
        or not isinstance(rule_binding.get("rule_count"), int)
        or isinstance(rule_binding.get("rule_count"), bool)
        or rule_binding["rule_count"] <= 0
    ):
        raise ConfigurationError("场景清单 rule_manifest 绑定非法。")
    catalog = payload.get("probe_catalog")
    if (
        not isinstance(catalog, dict)
        or set(catalog) != {"path", "schema_version", "probe_count", "dimension_count"}
        or catalog.get("schema_version") != PROBE_CATALOG_SCHEMA
        or catalog.get("path") != PROBE_CATALOG_PATH
        or not isinstance(catalog.get("probe_count"), int)
        or catalog.get("dimension_count") != len(_fw_f_v5().REQUIRED_MATRIX_DIMENSIONS)
    ):
        raise ConfigurationError("场景清单 probe_catalog 绑定非法（必须指向 FW-F v5 目录）。")
    facts = payload.get("version_facts")
    if (
        not isinstance(facts, dict)
        or set(facts) != {"path", "sha256"}
        or not _nonempty_text(facts.get("path"))
        or not SHA256_RE.fullmatch(str(facts.get("sha256", "")))
    ):
        raise ConfigurationError("场景清单 version_facts 绑定非法。")
    catalog_arguments = _step_catalog_arguments(payload)

    variables = payload.get("variable_contract")
    if not isinstance(variables, list) or not variables:
        raise ConfigurationError("场景清单 variable_contract 不能为空。")
    variable_names: set[str] = set()
    for index, variable in enumerate(variables, 1):
        _validate_variable(index, variable, variable_names)

    scenarios = payload.get("evidence_scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ConfigurationError("场景清单 evidence_scenarios 不能为空。")
    scenario_ids: set[str] = set()
    for index, scenario in enumerate(scenarios, 1):
        _validate_evidence_scenario(index, scenario, scenario_ids)

    jobs = payload.get("capture_jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ConfigurationError("场景清单 capture_jobs 不能为空。")
    job_ids: set[str] = set()
    for index, job in enumerate(jobs, 1):
        _validate_capture_job(index, job, job_ids, catalog_arguments=catalog_arguments)

    # 场景、probe 与 Job 的绑定闭合（按阶段）：official 阶段每个 probe 恰好由一步执行，被执行的 probe 都属于某个场景，
    # 场景列出的 probe 都被执行；候选阶段每个 probe 至多一步，且只能镜像 official 已执行的主模型 probe（两侧按同一 probe
    # 配对）。Job 的 scenario_ids 等于其各步 probe 所属场景之并，covers 不超出这些场景的 covers。
    scenario_by_id = {item["scenario_id"]: item for item in scenarios}
    probe_scenarios: dict[str, set[str]] = {}
    for item in scenarios:
        for probe in item["probe_ids"]:
            probe_scenarios.setdefault(probe, set()).add(item["scenario_id"])
    official_jobs = [job for job in jobs if job["phase"] == "official"]
    candidate_jobs = [job for job in jobs if job["phase"] == "candidate"]
    executed: list[str] = [step["probe_id"] for job in official_jobs for step in job["steps"]]
    if len(executed) != len(set(executed)):
        raise ConfigurationError("同一 probe 只能由一个 official 取证步骤执行。")
    if set(executed) != set(probe_scenarios):
        raise ConfigurationError(
            "场景声明的 probe 与 Job 执行的 probe 不一致："
            f"未执行={sorted(set(probe_scenarios) - set(executed))}，未归属场景={sorted(set(executed) - set(probe_scenarios))}"
        )
    # probe_catalog.probe_count 是 FW-F v5 目录的 probe 数，对应主模型步骤；非主模型（模型能力取证）步骤复用主模型已执行的
    # 基础 probe（标签规则也照主模型步骤复制），不计入目录数。
    primary_relay = {
        step["probe_id"] for job in official_jobs for step in job["steps"] if step_model(step["probe_id"], step["argv"]) is None
    }
    if catalog["probe_count"] != len(primary_relay):
        raise ConfigurationError("场景清单 probe_catalog.probe_count 与主模型执行的 probe 数不一致。")
    mirrored = [step["probe_id"] for job in candidate_jobs for step in job["steps"]]
    if len(mirrored) != len(set(mirrored)):
        raise ConfigurationError("同一 probe 只能由一个候选取证步骤执行。")
    unpaired = sorted(set(mirrored) - primary_relay)
    if unpaired:
        raise ConfigurationError(f"候选步骤只能镜像 official 已执行的主模型 probe：{unpaired}")
    for job in jobs:
        for step in job["steps"]:
            relay_probe = step_relay_probe(step["probe_id"], step["argv"])
            if step_model(step["probe_id"], step["argv"]) is not None and relay_probe not in primary_relay:
                raise ConfigurationError(f"{job['id']}／{step['probe_id']}：非主模型步骤的基础 probe {relay_probe} 没有主模型步骤执行。")
    for job in jobs:
        expected = set().union(*(probe_scenarios[step["probe_id"]] for step in job["steps"]))
        if set(job["scenario_ids"]) != expected:
            raise ConfigurationError(f"{job['id']} 的 scenario_ids 必须等于其各步 probe 所属场景之并。")
        proven = set().union(*(set(scenario_by_id[sid]["covers"]) for sid in job["scenario_ids"]))
        if not set(job["covers"]).issubset(proven):
            raise ConfigurationError(f"{job['id']} 的 covers 未被绑定场景证明。")
        if not job["covers"] and any(scenario_by_id[sid]["purpose"] != "discovery" for sid in job["scenario_ids"]):
            raise ConfigurationError(f"{job['id']} 不覆盖规则，只能承载发现项场景。")
        # 非主模型（模型能力取证）步骤只归入模型能力场景，主模型步骤不归入它。
        for step in job["steps"]:
            capability = step_model(step["probe_id"], step["argv"]) is not None
            memberships = probe_scenarios[step["probe_id"]]
            if (capability and memberships != {MODEL_CAPABILITY_SCENARIO}) or (not capability and MODEL_CAPABILITY_SCENARIO in memberships):
                raise ConfigurationError(
                    f"{job['id']}／{step['probe_id']}：非主模型步骤必须且只能归入 {MODEL_CAPABILITY_SCENARIO}，主模型步骤不得归入它。"
                )
    # 模型能力场景只是发现项：非主模型的证据只供 VC-3 逐模型投影，不得用来证明必需规则。
    capability_scenario = scenario_by_id.get(MODEL_CAPABILITY_SCENARIO)
    if capability_scenario is not None and capability_scenario["purpose"] != "discovery":
        raise ConfigurationError(f"{MODEL_CAPABILITY_SCENARIO} 必须是发现项场景（purpose=discovery、covers 为空）。")
    roots = [root for job in jobs for root in job["evidence_roots"]]
    if len(roots) != len(set(roots)):
        raise ConfigurationError("同一证据根只能由单一 Job 独占。")
    # VC-3 ScenarioPlan 的场景全集就是覆盖目标规则的证据场景（rule_evidence）；Store 封存 CandidateEvidencePackage 时要求
    # 候选场景批准唯一覆盖它，因此只要清单声明了候选 Job，每个套件里的必需候选 Job 都必须覆盖全部 rule_evidence 场景。
    if candidate_jobs:
        plan_scenarios = {item["scenario_id"] for item in scenarios if item["covers"]}
        for suite in sorted(JOB_SUITES):
            covered = {
                scenario_id
                for job in candidate_jobs
                if job["required"] and suite in job["suites"]
                for scenario_id in job["scenario_ids"]
            }
            missing = sorted(plan_scenarios - covered)
            if missing:
                raise ConfigurationError(f"{suite} 套件的必需候选 Job 没有覆盖 VC-3 ScenarioPlan 场景：{missing}")
    _validate_attempt_requirements(payload)


def _validate_attempt_requirements(payload: Mapping[str, Any]) -> None:
    """attempt 级证据要求（B4-01）：三官方客户端正例里的 Mac 两项走客户端收据，边界核对两项走 attempt 级收据。

    两个字段在 Formal plan 之前随清单冻结；词表闭合，必需项缺一即失败关闭（产出方与核对方分别在候选驱动、seal 与 accept）。
    """

    for field, required in (
        ("required_client_bindings", REQUIRED_CLIENT_BINDINGS),
        ("required_boundary_checks", REQUIRED_BOUNDARY_CHECKS),
    ):
        value = payload.get(field)
        if (
            not _unique_strings(value)
            or any(not SAFE_ID_RE.fullmatch(item) for item in value)
            or value != sorted(value)
            or set(value) != required
        ):
            raise ConfigurationError(f"场景清单 {field} 必须是排序去重的 {sorted(required)}。")


# ---------------------------------------------------------------------------
# 变量合同与模板展开
# ---------------------------------------------------------------------------


def _variable_contract(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["name"]): dict(item) for item in payload["variable_contract"]}


def runtime_variable_types(payload: Mapping[str, Any]) -> dict[str, str]:
    """step_runtime 作用域变量 → 类型（取证动作体每步绑定）。"""

    return {name: str(item["type"]) for name, item in _variable_contract(payload).items() if item["scope"] == "step_runtime"}


def _value_matches_type(value: str, variable_type: str) -> bool:
    if variable_type == "integer":
        return value.isdecimal() and int(value) > 0
    if variable_type == "absolute_path":
        return bool(SAFE_ABSOLUTE_PATH_RE.fullmatch(value)) and ".." not in Path(value).parts
    if variable_type == "sha256":
        return bool(SHA256_RE.fullmatch(value))
    if variable_type == "image_reference":
        return bool(IMMUTABLE_IMAGE_RE.fullmatch(value))
    if variable_type == "ip_address":
        try:
            # 与中继相同口径：必须是公开上游地址。
            return ipaddress.ip_address(value).is_global
        except ValueError:
            return False
    return bool(value.strip()) and not any(character in value for character in "{}")


def _template_fields(value: str) -> list[str]:
    """模板里的变量占位名；除占位外出现任何花括号（含 ``{{`` 转义）都拒绝，保证运行期二次绑定无歧义。"""

    fields = TEMPLATE_FIELD_RE.findall(value)
    remainder = TEMPLATE_FIELD_RE.sub("", value)
    if "{" in remainder or "}" in remainder or any(not VARIABLE_NAME_RE.fullmatch(field) for field in fields):
        raise ConfigurationError(f"场景任务模板只允许 {{变量名}} 占位：{value}")
    return fields


def _job_templates(job: Mapping[str, Any]) -> list[str]:
    values = [str(root) for root in job["evidence_roots"]]
    for step in job["steps"]:
        values.extend(step["argv"])
        values.extend(step["environment"].values())
    return values


def validate_campaign_context(payload: Mapping[str, Any], context: Mapping[str, str]) -> None:
    """版本清单声明、模板引用与 Campaign 实际取值形成闭环（运行期变量在 plan 时只核声明，不核取值）。"""

    contract = _variable_contract(payload)
    runtime = runtime_variable_types(payload)
    # 候选阶段变量（B4-01）在没有候选 Job 的旧清单副本里没有声明：取值为空时视同不存在，有取值则照常拒绝。
    unexpected = sorted(
        name
        for name in set(context) - set(contract)
        if not (name in CANDIDATE_CAMPAIGN_VARIABLES and not str(context.get(name) or ""))
    )
    if unexpected:
        raise ConfigurationError(f"Campaign 取值含变量合同外的名字：{unexpected}")
    bound_runtime = sorted(set(context) & set(runtime))
    if bound_runtime:
        raise ConfigurationError(f"运行期变量不得在 plan 时取值：{bound_runtime}")
    for name, item in contract.items():
        if name in runtime:
            continue
        value = str(context.get(name, "") or "")
        if item["required"] and not value:
            raise ConfigurationError(f"场景必需变量 {name} 没有 Campaign 值。")
        if value and not _value_matches_type(value, str(item["type"])):
            raise ConfigurationError(f"场景变量 {name} 的实际值不符合 {item['type']}。")
    # 非主模型（模型能力取证）步骤的字面模型名不得等于 Campaign 的主模型（同一模型在两个目录下各取一遍证）。
    primary_model = context.get("model")
    for job in payload["capture_jobs"]:
        for step in job["steps"]:
            if primary_model and step_model(step["probe_id"], step["argv"]) == primary_model:
                raise ConfigurationError(f"{job['id']}／{step['probe_id']} 的非主模型与 Campaign 主模型 {primary_model} 相同。")
    # 合成步骤的断网容器必须独立于联网的抓包容器（plan 与从 Campaign 冻结副本展开同一口径）。
    live_container = context.get("capture_container")
    if live_container and context.get("synthetic_capture_container") == live_container:
        raise ConfigurationError("synthetic_capture_container 必须是独立的断网容器，不能与 capture_container 相同。")
    for job in payload["capture_jobs"]:
        for value in _job_templates(job):
            for field in _template_fields(value):
                if field not in contract:
                    raise ConfigurationError(f"场景任务模板引用了 variable_contract 外变量：{field}")
        for root in job["evidence_roots"]:
            if set(_template_fields(str(root))) & set(runtime):
                raise ConfigurationError(f"{job['id']} 的证据根不得引用运行期变量。")


def _expand(value: str, context: Mapping[str, str], runtime: Iterable[str]) -> str:
    runtime_names = set(runtime)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in runtime_names:
            return match.group(0)
        if name not in context:
            raise ConfigurationError(f"任务模板引用未知变量：{name}")
        return str(context[name])

    _template_fields(value)
    return TEMPLATE_FIELD_RE.sub(replace, value)


def _validate_expanded_step(job_id: str, evidence_root: str, step: Mapping[str, Any]) -> None:
    """展开后的确定性坐标复核：run-id 不超 128 字符的安全名，输出根是本 Job 证据根下以 probe 命名的子目录。"""

    argv = step["argv"]
    run_id = _argument_value(argv, "--run-id")
    output_root = _argument_value(argv, "--output-root")
    if run_id is None or not SAFE_ID_RE.fullmatch(run_id):
        raise ConfigurationError(
            f"{job_id}／{step['probe_id']} 的中继 run-id 超过 128 字符或含非法字符；必须缩短 Campaign ID 后重新 plan。"
        )
    if (
        output_root is None
        or not SAFE_ABSOLUTE_PATH_RE.fullmatch(output_root)
        or output_root != f"{evidence_root}/{step['probe_id']}"
    ):
        raise ConfigurationError(f"{job_id}／{step['probe_id']} 的 --output-root 必须是本 Job 证据根下以 probe 命名的子目录。")
    if CANDIDATE_RUNTIME_DIR_FLAG in argv:
        # 候选步骤的私有运行目录（网关 API Key、待扫描的账号令牌等只在执行期存在的文件）：展开后必须是安全绝对路径。
        runtime_dir = _argument_value(argv, CANDIDATE_RUNTIME_DIR_FLAG)
        if runtime_dir is None or not SAFE_ABSOLUTE_PATH_RE.fullmatch(runtime_dir) or ".." in PurePosixPath(runtime_dir).parts:
            raise ConfigurationError(f"{job_id}／{step['probe_id']} 的 {CANDIDATE_RUNTIME_DIR_FLAG} 展开后不是安全绝对路径。")


def expand_scenario_jobs(
    payload: Mapping[str, Any],
    context: Mapping[str, str],
    *,
    job_factory: Callable[..., Any],
    suite: str = "full",
) -> list[Any]:
    """把已校验的场景清单展开成 Job；运行期变量保留占位，每步登记其运行期变量类型。"""

    if suite not in JOB_SUITES:
        raise ConfigurationError(f"suite 非法：{suite!r}")
    runtime = runtime_variable_types(payload)
    jobs: list[Any] = []
    for raw in payload["capture_jobs"]:
        if suite not in raw["suites"]:
            continue
        evidence_root = _expand(str(raw["evidence_roots"][0]), context, runtime)
        if not SAFE_ABSOLUTE_PATH_RE.fullmatch(evidence_root):
            raise ConfigurationError(f"{raw['id']} 的证据根展开后不是安全绝对路径。")
        steps = []
        for raw_step in raw["steps"]:
            argv = [_expand(item, context, runtime) for item in raw_step["argv"]]
            environment = {key: _expand(value, context, runtime) for key, value in raw_step["environment"].items()}
            used = {field for value in [*raw_step["argv"], *raw_step["environment"].values()] for field in _template_fields(value)}
            step = {
                "probe_id": raw_step["probe_id"],
                "argv": argv,
                "environment": environment,
                "timeout": int(raw_step["timeout_seconds"]),
                "runtime_variables": {name: runtime[name] for name in sorted(used & set(runtime))},
            }
            _validate_expanded_step(str(raw["id"]), evidence_root, step)
            steps.append(step)
        jobs.append(
            job_factory(
                job_id=str(raw["id"]),
                phase=str(raw["phase"]),
                suites=tuple(str(item) for item in raw["suites"]),
                description=str(raw["description"]),
                steps=tuple(steps),
                evidence_roots=(evidence_root,),
                covers=tuple(str(item) for item in raw["covers"]),
                scenario_ids=tuple(str(item) for item in raw["scenario_ids"]),
                required=bool(raw["required"]),
                required_scenario_receipts=tuple(str(item) for item in raw["required_scenario_receipts"]),
                tool_dependencies=tuple(str(item) for item in raw["tool_dependencies"]),
            )
        )
    if not jobs:
        raise ConfigurationError(f"场景清单在 suite={suite} 下没有任何 Job。")
    return jobs


def bind_step_runtime(step: Mapping[str, Any], values: Mapping[str, str]) -> dict[str, Any]:
    """取证动作体（B2-06）每步执行前绑定运行期变量：取值逐项按类型核对，绑定后不得残留任何占位。"""

    declared = step.get("runtime_variables")
    if not isinstance(declared, Mapping):
        raise ConfigurationError("步骤缺少运行期变量声明。")
    if set(values) != set(declared):
        raise ConfigurationError(f"运行期变量取值与声明不一致：声明={sorted(declared)}，提供={sorted(values)}")
    for name, variable_type in declared.items():
        value = str(values[name])
        if not value or not _value_matches_type(value, str(variable_type)):
            raise ConfigurationError(f"运行期变量 {name} 的取值不符合 {variable_type}。")

    def bind(text: str) -> str:
        bound = TEMPLATE_FIELD_RE.sub(lambda match: str(values[match.group(1)]) if match.group(1) in values else match.group(0), text)
        if "{" in bound or "}" in bound:
            raise ConfigurationError(f"绑定后仍有未解析的占位：{text}")
        return bound

    return {
        "probe_id": step["probe_id"],
        "argv": [bind(str(item)) for item in step["argv"]],
        "environment": {key: bind(str(value)) for key, value in dict(step["environment"]).items()},
        "timeout": step["timeout"],
    }


# ---------------------------------------------------------------------------
# 场景清单：装载、Job 校验与覆盖
# ---------------------------------------------------------------------------


def read_scenario_manifest(
    path: Path,
    *,
    expected_version: str,
    rules: Sequence[str],
    verify_source_spec: bool = True,
) -> dict[str, Any]:
    """读取并校验场景清单与它绑定的规则清单、规格章节；返回已校验的清单对象。

    ``verify_source_spec`` 为假只用于读取 Campaign 已冻结的副本（其绑定由 Campaign 摘要承担，可变总手册之后的改动
    不应击穿历史 Campaign）；plan 一律核对当前手册章节摘要。
    """

    payload = _read_json(Path(path), "场景清单")
    validate_scenario_manifest(payload)
    if payload["claude_version"] != expected_version:
        raise ConfigurationError("场景清单 claude_version 与目标版本不一致。")
    binding = payload["rule_manifest"]
    rule_path = _repository_file(binding["path"], "场景清单绑定的规则清单")
    if file_sha256(rule_path) != binding["sha256"]:
        raise ConfigurationError("场景清单绑定了其他内容的规则清单（摘要漂移）。")
    if binding["rule_count"] != len(rules):
        raise ConfigurationError("场景清单的规则数量与本轮升级规则全集不一致。")
    if verify_source_spec:
        source = payload["source_spec"]
        guide = _repository_file(source["path"], "场景清单绑定的规格")
        if guide_section_sha256(guide, source["fragment"]) != source["sha256"]:
            raise ConfigurationError("场景清单绑定的规格章节摘要不一致。")
    catalog = payload["probe_catalog"]
    _repository_file(catalog["path"], "场景清单绑定的 probe 目录")
    # 版本事实：与中继同一份实现核文件摘要、字段与版本（plan 时就失败，不等到 ARM64 上中继拒绝）。
    facts = payload["version_facts"]
    facts_path = _repository_file(facts["path"], "场景清单绑定的版本事实")
    try:
        _fw_f_v5().load_version_facts(facts_path.resolve(), str(facts["sha256"]), expected_version=str(payload["claude_version"]))
    except _fw_f_v5().ClaudeFWFV5ProbeError as error:
        raise ConfigurationError(f"场景清单绑定的版本事实不合法：{error}") from error
    return payload


def validate_jobs(jobs: Sequence[Any], rules: Sequence[str]) -> None:
    """Job 级闭合：ID 唯一、同阶段证据根独占、covers 只引用规则全集、声明覆盖的 Job 必须绑定场景。"""

    identifiers = [job.job_id for job in jobs]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise ConfigurationError(f"任务 ID 重复：{duplicates}")
    owners: dict[tuple[str, str], set[str]] = {}
    for job in jobs:
        for root in job.evidence_roots:
            owners.setdefault((job.phase, root), set()).add(job.job_id)
    shared = {f"{phase}:{root}": sorted(ids) for (phase, root), ids in owners.items() if len(ids) > 1}
    if shared:
        raise ConfigurationError(f"同一阶段的证据根必须由单一任务独占：{shared}")
    unknown = sorted({rule for job in jobs for rule in job.covers} - set(rules))
    if unknown:
        raise ConfigurationError(f"任务引用规则清单外编号：{unknown}")
    unbound = sorted(job.job_id for job in jobs if job.covers and not job.scenario_ids)
    if unbound:
        raise ConfigurationError(f"只有绑定场景的任务可以声明规则覆盖：{unbound}")


def coverage_plan(jobs: Sequence[Any], rules: Sequence[str]) -> dict[str, Any]:
    mapped = {
        phase: {rule for job in jobs if job.phase == phase and job.required for rule in job.covers}
        for phase in ("official", "candidate")
    }
    return {
        "required_rule_count": len(rules),
        "official_unmapped": sorted(set(rules) - mapped["official"]),
        "candidate_unmapped": sorted(set(rules) - mapped["candidate"]),
    }


def validate_full_coverage(jobs: Sequence[Any], rules: Sequence[str]) -> None:
    """full 套件：official 与候选两个阶段的必需 Job 各自的 covers 都必须覆盖规则全集（与 Codex 同一口径，B4-01 起）。"""

    plan = coverage_plan(jobs, rules)
    incomplete = {
        phase: plan[f"{phase}_unmapped"] for phase in ("official", "candidate") if plan[f"{phase}_unmapped"]
    }
    if incomplete:
        raise ConfigurationError(f"full 场景清单存在未映射规则：{incomplete}")


def scenario_job_execution_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    """提取全部 Job 中会改变实际执行或封存判定的字段（与 Codex 同名函数同一口径）。"""

    jobs = payload.get("capture_jobs")
    return {
        "schema_version": payload.get("schema_version"),
        "claude_version": payload.get("claude_version"),
        "jobs": [
            {key: value for key, value in job.items() if key in SCENARIO_JOB_EXECUTION_FIELDS}
            for job in (jobs if isinstance(jobs, list) else [])
            if isinstance(job, dict)
        ],
    }


# ---------------------------------------------------------------------------
# 证据标签：结构、与场景清单的绑定、逐连接匹配
# ---------------------------------------------------------------------------


def _job_step_probes(payload: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Job → probe → {synthetic, scenarios}（来自已校验的场景清单）。

    按 Job 分开：候选步骤与它镜像的 official 步骤是同一个 probe ID，按 probe 单键会互相覆盖。"""

    scenarios: dict[str, set[str]] = {}
    for item in payload["evidence_scenarios"]:
        for probe in item["probe_ids"]:
            scenarios.setdefault(probe, set()).add(item["scenario_id"])
    probes: dict[str, dict[str, dict[str, Any]]] = {}
    for job in payload["capture_jobs"]:
        for step in job["steps"]:
            probes.setdefault(job["id"], {})[step["probe_id"]] = {
                "synthetic": "--acknowledge-synthetic-responses" in step["argv"],
                "scenarios": scenarios[step["probe_id"]],
            }
    return probes


def _validate_connection(probe: str, connection: Any, *, synthetic: bool) -> dict[str, Any]:
    if not isinstance(connection, dict):
        raise ConfigurationError(f"{probe} 的连接规则必须是对象。")
    keys = set(connection)
    if keys == {"order"}:
        # 候选侧零出站（B4-01）：官方 probe 的请求全是网关不转发的端点（本地拒绝、只打 hello／settings 等），网关
        # 不应产生任何上游连接；出现任何有效连接即失败关闭。
        if connection.get("order") != "none":
            raise ConfigurationError(f"{probe} 的零连接规则只能是 order=none。")
        return {"mode": "none"}
    if keys == {"order", "requests"}:
        requests = connection.get("requests")
        if connection.get("order") != "unordered" or not _unique_strings(requests) or any(
            not REQUEST_LINE_RE.fullmatch(item) for item in requests
        ):
            raise ConfigurationError(f"{probe} 的无序连接规则非法。")
        return {"mode": "unordered", "requests": set(requests)}
    request = connection.get("request")
    if not isinstance(request, str) or not REQUEST_LINE_RE.fullmatch(request):
        raise ConfigurationError(f"{probe} 的连接规则 request 必须是「方法 路径」（不含查询串）。")
    if connection.get("count") not in CONNECTION_COUNTS:
        raise ConfigurationError(f"{probe} 的连接规则 count 非法。")
    intervention = connection.get("intervention")
    if intervention is not None and not (isinstance(intervention, str) and SAFE_ID_RE.fullmatch(intervention)):
        raise ConfigurationError(f"{probe} 的连接规则 intervention 非法。")
    if synthetic != (intervention is not None) and "ordinal" in keys:
        # 合成计划的每条逐序号连接都必须冻结干预动作；真实上游连接没有干预动作。
        raise ConfigurationError(f"{probe} 的逐序号连接规则与合成计划的干预动作声明不一致。")
    if "ordinal" in keys:
        if keys - {"ordinal", "request", "count", "intervention"} or not isinstance(connection["ordinal"], int) or isinstance(
            connection["ordinal"], bool
        ) or connection["ordinal"] < 1:
            raise ConfigurationError(f"{probe} 的逐序号连接规则字段非法。")
        return {"mode": "ordinal", "ordinal": connection["ordinal"], "request": request, "count": connection["count"], "intervention": intervention}
    if "ordinal_from" in keys:
        if (
            keys != {"ordinal_from", "request", "count", "min_connections"}
            or not isinstance(connection["ordinal_from"], int)
            or isinstance(connection["ordinal_from"], bool)
            or connection["ordinal_from"] < 1
            or not isinstance(connection["min_connections"], int)
            or isinstance(connection["min_connections"], bool)
            or connection["min_connections"] < 1
        ):
            raise ConfigurationError(f"{probe} 的尾部连接规则字段非法。")
        return {
            "mode": "tail",
            "ordinal_from": connection["ordinal_from"],
            "request": request,
            "count": connection["count"],
            "min_connections": connection["min_connections"],
        }
    raise ConfigurationError(f"{probe} 的连接规则必须声明 ordinal、ordinal_from 或 order=unordered 之一。")


def connection_plan(rules: Sequence[tuple[int, Mapping[str, Any]]]) -> dict[str, Any]:
    """一个 probe 的中继请求连接规则（(规则下标, 已校验规则) 列表）→ 逐连接计划。

    只有三种合法形态：一条零连接规则独占（候选侧零出站）；一条无序规则独占；或逐序号规则 1..k 连续且唯一，加至多一条
    从 k+1 起的尾部规则。
    """

    empty = [(index, rule) for index, rule in rules if rule["mode"] == "none"]
    if empty:
        if len(rules) != 1:
            raise ConfigurationError("零连接规则必须是该 probe 唯一的连接规则。")
        return {"mode": "none", "rule_index": empty[0][0]}
    unordered = [(index, rule) for index, rule in rules if rule["mode"] == "unordered"]
    if unordered:
        if len(rules) != 1:
            raise ConfigurationError("无序连接规则必须是该 probe 唯一的连接规则。")
        return {"mode": "unordered", "rule_index": unordered[0][0], "requests": unordered[0][1]["requests"]}
    explicit = sorted((rule["ordinal"], index, rule) for index, rule in rules if rule["mode"] == "ordinal")
    tails = [(index, rule) for index, rule in rules if rule["mode"] == "tail"]
    ordinals = [ordinal for ordinal, _index, _rule in explicit]
    if ordinals != list(range(1, len(ordinals) + 1)):
        raise ConfigurationError(f"逐序号连接规则必须从 1 起连续且唯一：{ordinals}")
    if len(tails) > 1 or (tails and tails[0][1]["ordinal_from"] != len(ordinals) + 1):
        raise ConfigurationError("尾部连接规则至多一条，且必须紧接最后一个逐序号规则。")
    if not explicit and not tails:
        raise ConfigurationError("probe 缺少中继请求连接规则。")
    return {
        "mode": "ordered",
        "explicit": [(index, rule) for _ordinal, index, rule in explicit],
        "tail": tails[0] if tails else None,
    }


def load_evidence_labels(path: Path, *, expected_version: str, scenario_payload: Mapping[str, Any]) -> dict[str, Any]:
    """读取并校验证据标签，核对它与场景清单逐 Job、逐 probe 闭合；返回 ``{"document", "plans"}``。

    ``plans[job_id][probe_id]`` 是该 probe 的逐连接计划（``connection_plan`` 的结果），供封存编目与测试匹配。
    """

    document = _read_json(Path(path), "证据标签声明")
    if set(document) != {"schema_version", "claude_version", "description", "authority", "entries"}:
        raise ConfigurationError("证据标签声明顶层字段不闭合。")
    if document.get("schema_version") != EVIDENCE_LABELS_SCHEMA:
        raise ConfigurationError(f"证据标签声明 schema_version 必须是 {EVIDENCE_LABELS_SCHEMA}。")
    if document.get("claude_version") != expected_version:
        raise ConfigurationError("证据标签声明版本与目标版本不一致。")
    if not _nonempty_text(document.get("description")) or not _unique_strings(document.get("authority")):
        raise ConfigurationError("证据标签声明的说明或 authority 非法。")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError("证据标签声明 entries 不能为空。")
    job_steps = _job_step_probes(scenario_payload)
    job_phases = {job["id"]: job["phase"] for job in scenario_payload["capture_jobs"]}
    job_probes = {job["id"]: [step["probe_id"] for step in job["steps"]] for job in scenario_payload["capture_jobs"]}
    plans: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"job_id", "side", "rules"}:
            raise ConfigurationError("证据标签 entries 项字段不闭合。")
        job_id = entry.get("job_id")
        if job_id not in job_phases or job_id in plans:
            raise ConfigurationError(f"证据标签声明的 Job 不在场景清单或重复：{job_id}")
        if entry.get("side") != job_phases[job_id]:
            raise ConfigurationError(f"{job_id} 的 side 与 Job 阶段不一致。")
        rules = entry.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ConfigurationError(f"{job_id} 的 rules 不能为空。")
        by_probe: dict[str, dict[str, list[Any]]] = {}
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict) or not _LABEL_RULE_FIELDS.issubset(rule) or set(rule) - _LABEL_RULE_FIELDS - {
                "connection",
                "derive",
            }:
                raise ConfigurationError(f"{job_id} 的第 {index + 1} 条标签规则字段不闭合。")
            glob = str(rule.get("glob", ""))
            parts = glob.split("/")
            if (
                len(parts) < 2
                or any(not GLOB_COMPONENT_RE.fullmatch(part) for part in parts)
                or "*" in "".join(parts[:-1])
            ):
                raise ConfigurationError(f"{job_id} 的标签 glob 非法：{glob!r}（通配只允许出现在最后一段）")
            probe = parts[0]
            if probe not in job_probes[job_id]:
                raise ConfigurationError(f"{job_id} 的标签 glob 指向不属于本 Job 的 probe：{probe}")
            kind = rule.get("kind")
            if kind not in LABEL_KIND_PARSERS or rule.get("parser") not in LABEL_KIND_PARSERS[kind]:
                raise ConfigurationError(f"{job_id}／{probe} 的 kind 或 parser 未登记：{kind}／{rule.get('parser')}")
            derive = rule.get("derive")
            if derive is not None and (
                kind != "relay_binary"
                or not isinstance(derive, dict)
                or set(derive) != {"parser", "kind"}
                or (derive.get("parser"), derive.get("kind")) not in DERIVE_TARGETS
            ):
                raise ConfigurationError(f"{job_id}／{probe} 的 derive 非法。")
            scenario_ids = rule.get("scenario_ids")
            if not _unique_strings(scenario_ids) or not set(scenario_ids).issubset(job_steps[job_id][probe]["scenarios"]):
                raise ConfigurationError(f"{job_id}／{probe} 的 scenario_ids 必须是该 probe 所属场景的子集。")
            labels = rule.get("labels")
            if (
                not isinstance(labels, dict)
                or not all(isinstance(key, str) and _nonempty_text(value) for key, value in labels.items())
                or labels.get("probe") != probe
            ):
                raise ConfigurationError(f"{job_id}／{probe} 的 labels 必须是非空字符串映射且 probe 标签等于 glob 的 probe。")
            if not _nonempty_text(rule.get("rationale")):
                raise ConfigurationError(f"{job_id}／{probe} 的 rationale 不能为空。")
            is_request = kind == "relay_binary" and glob == f"{probe}/{RELAY_REQUEST_GLOB_SUFFIX}"
            if is_request != ("connection" in rule):
                raise ConfigurationError(f"{job_id}／{probe} 只有中继请求字节规则声明 connection，且必须声明。")
            slot = by_probe.setdefault(probe, {"connections": [], "kinds": []})
            slot["kinds"].append(kind)
            if is_request:
                connection = _validate_connection(probe, rule["connection"], synthetic=job_steps[job_id][probe]["synthetic"])
                if connection["mode"] == "none" and entry["side"] != "candidate":
                    # 官方客户端直连时每个 probe 至少有启动序列连接；零连接形态只给候选侧（网关不转发的端点）。
                    raise ConfigurationError(f"{job_id}／{probe} 是 official 侧，不得声明零连接规则。")
                slot["connections"].append((index, connection))
        job_plans: dict[str, Any] = {}
        for probe in job_probes[job_id]:
            slot = by_probe.get(probe)
            if slot is None or not slot["connections"]:
                raise ConfigurationError(f"{job_id}／{probe} 缺少中继请求的逐连接标签。")
            if "relay_manifest" not in slot["kinds"]:
                raise ConfigurationError(f"{job_id}／{probe} 缺少 relay_manifest 标签。")
            if job_steps[job_id][probe]["synthetic"] != ("synthetic_intervention" in slot["kinds"]):
                raise ConfigurationError(f"{job_id}／{probe} 的合成干预记录标签与请求确认方式不一致。")
            job_plans[probe] = connection_plan(slot["connections"])
        plans[job_id] = job_plans
    missing = sorted(set(job_phases) - set(plans))
    if missing:
        raise ConfigurationError(f"场景清单 Job 缺少证据标签：{missing}")
    return {"document": document, "plans": plans}


def match_relay_connections(plan: Mapping[str, Any], connections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把一个 probe 的有效中继连接逐条匹配到标签规则；任何不一致都抛 ``LabelSequenceMismatch``。

    ``connections`` 每项 ``{"connection_id": int, "requests": ["方法 路径", ...], "intervention": str | None}``，只含
    中继有效连接（中继清单登记的握手前断开连接须由调用方先剔除）；请求只比方法与去掉查询串的路径。返回每条连接命中
    的规则下标与有效连接序号。
    """

    records = []
    for connection in connections:
        connection_id = connection.get("connection_id")
        requests = connection.get("requests")
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id < 1
            or not isinstance(requests, list)
            or not requests
            or not all(isinstance(item, str) for item in requests)
        ):
            raise LabelSequenceMismatch(f"中继连接记录非法：{connection!r}")
        records.append((connection_id, [item.split("?", 1)[0] for item in requests], connection.get("intervention")))
    records.sort(key=lambda item: item[0])
    if len({item[0] for item in records}) != len(records):
        raise LabelSequenceMismatch("中继连接号重复。")
    matches: list[dict[str, Any]] = []
    if plan["mode"] == "none":
        if records:
            raise LabelSequenceMismatch(f"零连接规则不允许任何有效连接，实际 {len(records)} 条：{[item[0] for item in records]}")
        return matches
    if plan["mode"] == "unordered":
        allowed = plan["requests"]
        for ordinal, (connection_id, requests, _intervention) in enumerate(records, 1):
            unexpected = sorted(set(requests) - set(allowed))
            if unexpected:
                raise LabelSequenceMismatch(f"连接 {connection_id} 出现无序规则未声明的请求：{unexpected}")
            matches.append({"connection_id": connection_id, "ordinal": ordinal, "rule_index": plan["rule_index"]})
        if not records:
            raise LabelSequenceMismatch("无序连接规则至少要有一条有效连接。")
        return matches
    explicit = plan["explicit"]
    tail = plan["tail"]
    for ordinal, (connection_id, requests, intervention) in enumerate(records, 1):
        if ordinal <= len(explicit):
            rule_index, rule = explicit[ordinal - 1]
        elif tail is not None:
            rule_index, rule = tail
        else:
            raise LabelSequenceMismatch(f"第 {ordinal} 条有效连接（连接号 {connection_id}）没有对应的标签规则。")
        if any(item != rule["request"] for item in requests):
            raise LabelSequenceMismatch(
                f"第 {ordinal} 条有效连接（连接号 {connection_id}）的请求 {requests} 与标签声明 {rule['request']} 不一致。"
            )
        if rule["count"] == "one" and len(requests) != 1:
            raise LabelSequenceMismatch(f"第 {ordinal} 条有效连接应只承载 1 次请求，实际 {len(requests)} 次。")
        if rule.get("intervention") is not None and intervention != rule["intervention"]:
            raise LabelSequenceMismatch(
                f"第 {ordinal} 条有效连接的合成干预动作 {intervention!r} 与声明 {rule['intervention']!r} 不一致。"
            )
        matches.append({"connection_id": connection_id, "ordinal": ordinal, "rule_index": rule_index})
    if len(records) < len(explicit):
        raise LabelSequenceMismatch(f"标签声明了 {len(explicit)} 条逐序号连接，实际只有 {len(records)} 条有效连接。")
    if tail is not None and len(records) - len(explicit) < tail[1]["min_connections"]:
        raise LabelSequenceMismatch(f"尾部规则至少需要 {tail[1]['min_connections']} 条连接，实际 {len(records) - len(explicit)} 条。")
    return matches


# ---------------------------------------------------------------------------
# plan：参数、上下文、展开与报告
# ---------------------------------------------------------------------------

# Campaign 作用域变量 ← plan 参数名（变量名即参数 dest；campaign_dir 来自 --campaign-dir）。
CAMPAIGN_VARIABLES = (
    "campaign_id",
    "campaign_dir",
    "target_version",
    "target_sha256",
    "runtime_image",
    "capture_root",
    "capture_tool_root",
    "capture_container",
    "synthetic_capture_container",
    "claude_bin",
    "model",
    "upstream_ip",
    "credentials_file",
    "ca_signing_pem",
    "ca_cert",
)


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """``claude_upgrade.py plan`` 的参数（B2-05：展开与校验；建 Campaign 所需的账本与收据参数由 B2-11 增加）。"""

    parser.add_argument("--campaign-dir", "--output", dest="campaign_dir", type=Path, required=True)
    parser.add_argument("--campaign-mode", choices=sorted(CAMPAIGN_MODES), required=True)
    parser.add_argument("--campaign-purpose", choices=sorted(CAMPAIGN_PURPOSES), required=True)
    parser.add_argument("--baseline-version", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--target-sha256", required=True, help="B2-01 冻结的官方二进制 binary_sha256")
    parser.add_argument("--runtime-image", required=True, help="官方采集运行时 repository@sha256 镜像引用")
    parser.add_argument("--model", required=True)
    parser.add_argument("--upstream-ip", required=True, help="api.anthropic.com 的冻结公开上游 IP")
    parser.add_argument("--campaign-id", default="", help="留空时按目标版本与 UTC 时间生成")
    parser.add_argument("--suite", choices=sorted(JOB_SUITES), default="full")
    parser.add_argument("--rule-manifest", type=Path, help="基线 RequiredRules 清单；缺省 claude_required_rules_<基线>.json")
    parser.add_argument("--scenario-manifest", type=Path, help="目标场景清单；缺省 claude_upgrade_scenarios_<目标>.json")
    parser.add_argument("--evidence-labels", type=Path, help="目标证据标签；缺省 claude_upgrade_evidence_labels_<目标>.json")
    parser.add_argument("--claude-bin", default="", help="缺省 /opt/claude-<目标版本>/claude")
    # 以下容器内坐标缺省取场景清单变量合同的 default（单一来源，不在这里重复写死）。
    for flag in (
        "--capture-root",
        "--capture-tool-root",
        "--capture-container",
        "--synthetic-capture-container",
        "--credentials-file",
        "--ca-signing-pem",
        "--ca-cert",
    ):
        parser.add_argument(flag, default=None)
    # 候选阶段（B4-01）：网关侧容器名缺省取变量合同 default；候选账号与 ingress API Key 的数据库 ID 只有候选 run 需要，
    # official-only 的 Campaign 可以不给（候选 run 之前由编排器失败关闭核对非空）。清单里只登记 ID，不保存 key 值。
    for flag in ("--service-container", "--keeper-container", "--postgres-container"):
        parser.add_argument(flag, default=None)
    parser.add_argument("--claude-account-id", default="", help="候选阶段专用 Anthropic OAuth 验收账号的数据库 ID")
    parser.add_argument("--api-key-id", default="", help="候选阶段专用 ingress API Key 的数据库 ID（单账号隔离分组）")
    # B5a-09：官方包与只审计。官方包是 B2-01 冻结收据 freeze.json（驱动参数 TARGET_PACKAGE），摘要为本轮登记的
    # OFFICIAL_ASSET_SHA256；--audit-only 只审参数、官方包、清单与工具身份，不建 Campaign、不写任何文件。
    parser.add_argument("--target-package", type=Path, default=None, help="B2-01 冻结收据 freeze.json（绝对路径）")
    parser.add_argument("--target-package-sha256", default="", help="冻结收据 freeze.json 的登记摘要")
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help=(
            "B5a-09：只做计划输入审计——参数、官方包（B2-01 冻结目录）、规则／场景清单／证据标签与 Job 展开、工具身份，"
            "逐项报全；不读计时账本与环境收据、不建 Campaign、不写任何文件，供建账本之前的入口便宜检查使用。"
        ),
    )


def default_manifest_paths(arguments: argparse.Namespace) -> None:
    """规则清单、场景清单与证据标签的缺省路径（就地写回 ``arguments``）：没有显式给出时按版本派生。

    plan 的参数准备与 ``--audit-only`` 的清单项共用这一份规则：参数项中途失败时清单项照样按同一路径审计。"""

    arguments.rule_manifest = Path(arguments.rule_manifest or baseline_rule_manifest_path(arguments.baseline_version))
    arguments.scenario_manifest = Path(arguments.scenario_manifest or scenario_manifest_path(arguments.target_version))
    arguments.evidence_labels = Path(arguments.evidence_labels or evidence_labels_path(arguments.target_version))


def prepare_plan_arguments(arguments: argparse.Namespace) -> dict[str, Any]:
    """校验并补齐 plan 参数（就地写回 ``arguments``），返回读取到的目标场景清单原文（尚未做绑定核对）。"""

    if getattr(arguments, "campaign_mode", None) not in CAMPAIGN_MODES:
        raise ConfigurationError("--campaign-mode 必须显式为 preflight_only 或 formal。")
    if getattr(arguments, "campaign_purpose", None) not in CAMPAIGN_PURPOSES:
        raise ConfigurationError("--campaign-purpose 必须显式为 validation_only 或 production_replacement。")
    for flag, value in (("--baseline-version", arguments.baseline_version), ("--target-version", arguments.target_version)):
        if not VERSION_RE.fullmatch(str(value)):
            raise ConfigurationError(f"{flag} 必须是三段版本号。")
    if arguments.baseline_version == arguments.target_version:
        raise ConfigurationError("--target-version 必须不同于 --baseline-version。")
    if arguments.campaign_id:
        if not SAFE_ID_RE.fullmatch(arguments.campaign_id):
            raise ConfigurationError("--campaign-id 格式非法。")
    else:
        arguments.campaign_id = (
            f"claude-code-{arguments.target_version.replace('.', '_')}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        )
    campaign_dir = Path(arguments.campaign_dir)
    if not campaign_dir.is_absolute() or not SAFE_ABSOLUTE_PATH_RE.fullmatch(str(campaign_dir)):
        raise ConfigurationError("--campaign-dir 必须是安全绝对路径。")
    default_manifest_paths(arguments)
    payload = _read_json(arguments.scenario_manifest, "场景清单")
    validate_scenario_manifest(payload)
    contract = _variable_contract(payload)
    for name in CAMPAIGN_VARIABLES:
        if name not in contract or contract[name]["scope"] != "campaign":
            raise ConfigurationError(f"场景清单变量合同缺少 Campaign 变量 {name}。")
        if getattr(arguments, name, None) in (None, "") and "default" in contract[name]:
            setattr(arguments, name, str(contract[name]["default"]))
    for name in CANDIDATE_CAMPAIGN_VARIABLES:
        # 候选变量在没有候选 Job 的旧清单里可以缺席；声明了就同样取 default，并必须是 Campaign 作用域。
        if name in contract:
            if contract[name]["scope"] != "campaign":
                raise ConfigurationError(f"场景清单变量 {name} 必须是 Campaign 作用域。")
            if getattr(arguments, name, None) in (None, "") and "default" in contract[name]:
                setattr(arguments, name, str(contract[name]["default"]))
    if not arguments.claude_bin:
        arguments.claude_bin = f"/opt/claude-{arguments.target_version}/claude"
    # 与 Codex plan 同一约束：模型与容器名只能是安全标识（进入 argv 与运行坐标）。
    for flag, value in (
        ("--model", arguments.model),
        ("--capture-container", arguments.capture_container),
        ("--synthetic-capture-container", arguments.synthetic_capture_container),
    ):
        if not SAFE_ID_RE.fullmatch(str(value or "")):
            raise ConfigurationError(f"{flag} 格式非法。")
    if arguments.synthetic_capture_container == arguments.capture_container:
        raise ConfigurationError("--synthetic-capture-container 必须是独立的断网容器，不能与 --capture-container 相同。")
    for flag, name in (
        ("--service-container", "service_container"),
        ("--keeper-container", "keeper_container"),
        ("--postgres-container", "postgres_container"),
    ):
        value = str(getattr(arguments, name, None) or "")
        if value and not SAFE_ID_RE.fullmatch(value):
            raise ConfigurationError(f"{flag} 格式非法。")
    for flag, name in (("--claude-account-id", "claude_account_id"), ("--api-key-id", "api_key_id")):
        value = str(getattr(arguments, name, None) or "")
        if value and not (value.isdecimal() and int(value) > 0):
            raise ConfigurationError(f"{flag} 必须是正整数。")
    return payload


def plan_context(arguments: argparse.Namespace) -> dict[str, str]:
    """Campaign 作用域变量的实际取值（字符串）；运行期变量不在其中。"""

    context: dict[str, str] = {}
    for name in (*CAMPAIGN_VARIABLES, *CANDIDATE_CAMPAIGN_VARIABLES):
        value = getattr(arguments, name, None)
        context[name] = "" if value is None else str(value)
    return context


def verify_target_rules(arguments: argparse.Namespace, rules: Sequence[str]) -> None:
    """目标版本规则清单逐条承接基线（plan 控制流调用；不在 wire 根路径上，规则清单本身归 control 层）。"""

    load_target_rules(
        rule_manifest_path(arguments.target_version),
        target_version=arguments.target_version,
        baseline_rules=rules,
        baseline_manifest=arguments.rule_manifest,
    )


def plan_jobs(
    arguments: argparse.Namespace,
    rules: Sequence[str],
    *,
    context: Mapping[str, str],
    job_factory: Callable[..., Any],
) -> tuple[list[Any], dict[str, dict[str, str]]]:
    """按目标场景清单展开 plan 的全部 Job 并做 Job 闭合与覆盖校验；返回 (Job 列表, 三份输入的路径与摘要)。

    本函数在 wire 根 ``_load_plan_jobs`` 的路径上，只放决定 Job 展开的逻辑；证据标签的校验属于 evidence 语义，
    由 plan 控制流另行调用 ``evidence_labels_binding``，标签校验代码的变化因此不会改变 Claude 的 wire 身份。
    """

    target_rules_path = rule_manifest_path(arguments.target_version)
    payload = read_scenario_manifest(arguments.scenario_manifest, expected_version=arguments.target_version, rules=rules)
    if _repository_file(payload["rule_manifest"]["path"], "场景清单绑定的规则清单").resolve() != target_rules_path.resolve():
        raise ConfigurationError("场景清单绑定的规则清单不是目标版本规则清单。")
    validate_campaign_context(payload, context)
    jobs = expand_scenario_jobs(payload, context, job_factory=job_factory, suite=arguments.suite)
    validate_jobs(jobs, rules)
    if arguments.suite == "full":
        validate_full_coverage(jobs, rules)
    inputs = {
        "baseline_rules": _binding(arguments.rule_manifest),
        "target_rules": _binding(target_rules_path),
        "target_scenarios": _binding(arguments.scenario_manifest),
    }
    return jobs, inputs


def evidence_labels_binding(arguments: argparse.Namespace, rules: Sequence[str]) -> dict[str, str]:
    """校验证据标签与目标场景清单逐 Job、逐 probe 闭合，返回标签文件的路径与摘要（plan 控制流调用）。"""

    payload = read_scenario_manifest(arguments.scenario_manifest, expected_version=arguments.target_version, rules=rules)
    load_evidence_labels(arguments.evidence_labels, expected_version=arguments.target_version, scenario_payload=payload)
    return _binding(arguments.evidence_labels)


def plan_report(
    arguments: argparse.Namespace,
    jobs: Sequence[Any],
    rules: Sequence[str],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """plan 的零写入报告：全部 Job 的展开结果、覆盖计划与输入绑定（B2-11 建 Campaign 时冻结同一内容）。"""

    return {
        "schema_version": PLAN_REPORT_SCHEMA,
        "mode": "dry-run",
        "campaign_created": False,
        "files_written": 0,
        "live_request_count": 0,
        "campaign_mode": arguments.campaign_mode,
        "campaign_purpose": arguments.campaign_purpose,
        "campaign_id": arguments.campaign_id,
        "campaign_dir": str(arguments.campaign_dir),
        "baseline_version": arguments.baseline_version,
        "target_version": arguments.target_version,
        "target_sha256": arguments.target_sha256,
        "suite": arguments.suite,
        "inputs": dict(inputs),
        # 容器内坐标与取证参数（B2-11 写进 Campaign 总计划 configuration；B1-11 计量按 capture_root 映射证据根）。
        "configuration": {
            **{
                name: str(getattr(arguments, name))
                for name in CAMPAIGN_VARIABLES
                if name not in {"campaign_id", "campaign_dir", "target_version", "target_sha256"}
            },
            # 候选阶段变量（B4-01）：official-only 的 Campaign 可以为空字符串，候选 run 之前核对非空。
            **{name: str(getattr(arguments, name, None) or "") for name in CANDIDATE_CAMPAIGN_VARIABLES},
        },
        "required_rules": list(rules),
        "coverage_plan": coverage_plan(jobs, rules),
        "job_count": len(jobs),
        "step_count": sum(len(job.steps) for job in jobs),
        "jobs": [
            {
                "id": job.job_id,
                "phase": job.phase,
                "description": job.description,
                "steps": [
                    {
                        "probe_id": step["probe_id"],
                        "argv": list(step["argv"]),
                        "environment": dict(step["environment"]),
                        "timeout": step["timeout"],
                        "runtime_variables": dict(step["runtime_variables"]),
                    }
                    for step in job.steps
                ],
                "evidence_roots": list(job.evidence_roots),
                "covers": list(job.covers),
                "scenario_ids": list(job.scenario_ids),
                "tool_dependencies": list(job.tool_dependencies),
            }
            for job in jobs
        ],
    }


# ---------------------------------------------------------------------------
# plan --audit-only（B5a-09）：建计时账本之前把 plan 能查的输入一次查全
# ---------------------------------------------------------------------------

PLAN_AUDIT_SCHEMA = "claude-upgrade-plan-audit/v1"
# --audit-only 不校验的项：它们在建计时账本之后才存在，由建 Campaign 时的 plan（B2-11）校验；名字与 Codex 的同一组。
PLAN_AUDIT_UNCHECKED = (
    "timing-ledger-and-checkpoint",
    "arm64-environment-receipt",
    "p0-environment-producer",
    "vc-control-artifacts",
)
# 编排器总计划的工具身份字段 ↔ Store Campaign v2 的工具身份字段：同一指纹，键名按各自设计不同（设计 B1-12），按值比对。
TOOL_IDENTITY_FIELD_PAIRS = (
    ("policy_version", "policy_version"),
    ("policy_sha256", "policy_sha256"),
    ("wire_producer_sha256", "wire_producer_sha256"),
    ("evidence_semantics_sha256", "evidence_semantics_sha256"),
    ("control_sha256", "control_sha256"),
    ("files_sha256", "tool_files_sha256"),
)


def plan_audit(
    arguments: argparse.Namespace,
    *,
    load_plan_jobs: Callable[[argparse.Namespace, tuple[str, ...]], tuple[list[Any], dict[str, dict[str, str]]]],
    tool_identity: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    """``plan --audit-only``：只审参数、官方包、清单与工具身份，逐项报全；不读账本、不建 Campaign、不写任何文件。

    各项互相独立，一项失败其余照查；依赖失败项的标为被阻塞（``blocked_by``）。输出形状与 Codex 的
    ``codex-upgrade-plan-audit/v1`` 相同（入口便宜检查按同一字段打印）。检查项：

    * ``arguments``：plan 的参数准备（``prepare_plan_arguments``，同 plan 一份）与 Campaign 作用域变量的取值合同；
    * ``official-package``：B2-01 冻结目录逐项复核（``claude_upgrade_freeze.verify_freeze_receipt``），目标版本、目标二进制
      摘要（``--target-sha256``）与冻结收据摘要（``--target-package-sha256``）等于本轮登记值；
    * ``manifests``：基线与目标规则清单、目标场景清单（结构、规则绑定、版本事实）与证据标签（逐 Job、逐 probe 闭合）——
      不依赖参数项，缺省路径与 plan 同一规则（``default_manifest_paths``）；
    * ``jobs``：经编排器的 wire 根 ``load_plan_jobs`` 展开全部 Job（与 plan 同一函数）并核对 Job 闭合与 official 覆盖；
    * ``tool-identity``：编排器的工具身份（``tool_identity``，即总计划冻结的那一份）与 Store 现算的七项身份按值一致。
    """

    checks: list[dict[str, Any]] = []
    state: dict[str, Any] = {}

    def run(name: str, action: Callable[[], Any], *, requires: tuple[str, ...] = ()) -> None:
        blocked = [item["name"] for item in checks if item["name"] in requires and item["status"] != "passed"]
        if blocked:
            checks.append({"name": name, "status": "blocked", "blocked_by": blocked})
            return
        try:
            detail = action()
        except Exception as error:  # noqa: BLE001 - 审计要如实报出每一项的失败，一项异常不能中断其余各项
            checks.append({"name": name, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            return
        checks.append({"name": name, "status": "passed", "detail": detail})

    def check_arguments() -> dict[str, Any]:
        payload = prepare_plan_arguments(arguments)
        validate_campaign_context(payload, plan_context(arguments))
        return {"campaign_id": arguments.campaign_id, "campaign_mode": arguments.campaign_mode, "suite": arguments.suite}

    def check_official_package() -> dict[str, Any]:
        from tools.official_client_capture import claude_upgrade_freeze

        package = getattr(arguments, "target_package", None)
        expected = str(getattr(arguments, "target_package_sha256", "") or "")
        if package is None or not str(package):
            raise ConfigurationError("缺少 --target-package（B2-01 冻结收据 freeze.json）。")
        if not expected:
            raise ConfigurationError("缺少 --target-package-sha256（本轮登记的冻结收据摘要）。")
        result = claude_upgrade_freeze.verify_freeze_receipt(
            Path(package),
            expected_version=str(arguments.target_version),
            expected_binary_sha256=str(arguments.target_sha256),
            expected_freeze_sha256=expected,
        )
        return {
            "freeze_sha256": result["freeze_sha256"],
            "binary_sha256": result["binary_sha256"],
            "binary_equivalence": result["binary_equivalence"],
            "verified_file_count": len(result["verified_files"]),
        }

    def check_manifests() -> dict[str, Any]:
        default_manifest_paths(arguments)
        rules = load_baseline_rules(arguments.rule_manifest, arguments.baseline_version)
        verify_target_rules(arguments, rules)
        payload = read_scenario_manifest(arguments.scenario_manifest, expected_version=arguments.target_version, rules=rules)
        labels = evidence_labels_binding(arguments, rules)
        state["rules"] = rules
        return {
            "rule_count": len(rules),
            "capture_job_count": len(payload["capture_jobs"]),
            "scenario_manifest": _binding(arguments.scenario_manifest),
            "evidence_labels": labels,
        }

    def check_jobs() -> dict[str, Any]:
        jobs, inputs = load_plan_jobs(arguments, state["rules"])
        coverage = coverage_plan(jobs, state["rules"])
        return {
            "job_count": len(jobs),
            "step_count": sum(len(job.steps) for job in jobs),
            "official_unmapped": coverage["official_unmapped"],
            "candidate_unmapped": coverage["candidate_unmapped"],
            "inputs": inputs,
        }

    def check_tool_identity() -> dict[str, Any]:
        from tools.official_client_control import receipts as control_receipts

        orchestrator = tool_identity()
        store = control_receipts.current_tool_identity("claude")
        drift = [f"{left}↔{right}" for left, right in TOOL_IDENTITY_FIELD_PAIRS if orchestrator.get(left) != store.get(right)]
        if drift:
            raise ConfigurationError(f"编排器工具身份与 Store 现算身份不一致：{'、'.join(drift)}")
        return {left: orchestrator[left] for left, _right in TOOL_IDENTITY_FIELD_PAIRS}

    run("arguments", check_arguments)
    run("official-package", check_official_package)
    run("manifests", check_manifests)
    run("jobs", check_jobs, requires=("arguments", "manifests"))
    run("tool-identity", check_tool_identity)
    failed = [item["name"] for item in checks if item["status"] != "passed"]
    return {
        "schema_version": PLAN_AUDIT_SCHEMA,
        "status": "passed" if not failed else "failed",
        "baseline_version": getattr(arguments, "baseline_version", None),
        "target_version": getattr(arguments, "target_version", None),
        "campaign_mode": getattr(arguments, "campaign_mode", None),
        "checks": checks,
        "failed_checks": failed,
        "unchecked": list(PLAN_AUDIT_UNCHECKED),
        "files_written": 0,
        "live_request_count": 0,
    }
