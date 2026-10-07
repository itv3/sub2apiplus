"""Claude 升级的版本化清单与 plan 作业展开（Claude 升级控制面设计 B2-05）。

本模块装载并校验三份按目标版本冻结的机器清单，并把场景清单展开成取证 Job：

* ``claude_upgrade_scenarios_<ver>.json``（``claude-upgrade-scenarios/v1``，结构对齐 Codex 场景清单）：变量合同、证据场景
  （FW-F v4 的 49 个矩阵维度，加没有维度的 probe 归入的条件场景）与 official 取证 Job（79 个 probe 归并为 Job，每个
  probe 一步，动作体 ``claude_fw_e_relay.py capture``）；
* ``claude_upgrade_rules_<ver>.json``（``claude-upgrade-rule-manifest/v1``）：本轮逐规则重测的必需规则全集，绑定基线
  ``claude_required_rules_<baseline>.json`` 的路径与摘要；
* ``claude_upgrade_evidence_labels_<ver>.json``（``claude-upgrade-evidence-labels/v1``）：逐 Job 证据子路径的标签；中继
  请求按「有效连接序号」逐连接贴标签，``match_relay_connections`` 是封存编目（B2-07）与逐连接顺序测试共用的匹配器。

变量分两种作用域：``campaign`` 在 plan 时代入；``step_runtime``（宿主运行凭据的容器路径与摘要、run nonce）由取证动作体
（B2-06）在每步执行前用 ``bind_step_runtime`` 绑定，plan 展开时原样保留占位，因此模板里除变量占位外不允许出现花括号。

本模块不导入 ``codex_upgrade`` 与任何被 Claude 身份策略忽略的文件。编排器的 wire 根 ``_load_plan_jobs``、
``_job_context``、``_scenario_job_execution_contract`` 转调这里，按符号进入 Claude 的 wire 闭包；本文件按
``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import time
from pathlib import Path
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
PROBE_CATALOG_SCHEMA = "claude-code-fw-f-v4-scenario-catalog/v1"

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

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
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
        "variable_contract",
        "evidence_scenarios",
        "capture_jobs",
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


def _validate_step(job_id: str, step_index: int, step: Any) -> str:
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
    if "--execute" not in argv or _argument_value(argv, "--probe-id") != probe_id or _argument_value(argv, "--fw-f-v4-probe") != probe_id:
        raise ConfigurationError(f"{job_id} 步骤 {step_index} 的 --execute／--probe-id／--fw-f-v4-probe 与 probe_id 不一致。")
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


def _validate_capture_job(index: int, job: Any, seen: set[str]) -> None:
    if not isinstance(job, dict) or set(job) != _JOB_FIELDS:
        raise ConfigurationError(f"场景任务 {index} 字段不闭合（tool_dependencies 必须逐作业声明）。")
    job_id = job.get("id")
    if not isinstance(job_id, str) or not SAFE_ID_RE.fullmatch(job_id) or job_id in seen:
        raise ConfigurationError(f"场景任务 {index} id 非法或重复。")
    if job.get("phase") not in JOB_PHASES:
        raise ConfigurationError(f"{job_id} 的 phase 非法。")
    if job["phase"] != "official":
        # 候选取证 Job 的动作体与证据布局由 B4-01 落实；在那之前清单只能声明 official Job。
        raise ConfigurationError(f"{job_id}：Claude 候选取证 Job 由 B4-01 落实，当前场景清单只接受 official Job。")
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
    for step_index, step in enumerate(steps, 1):
        _validate_step(job_id, step_index, step)
    # Claude 每个 Job 只有一个证据根，各步输出根都是它下面以 probe 命名的子目录。
    if not _unique_strings(job.get("evidence_roots")) or len(job["evidence_roots"]) != 1:
        raise ConfigurationError(f"{job_id} 的 evidence_roots 必须恰好一个。")
    covers = job.get("covers")
    if not _unique_strings(covers) or any(not RULE_RE.fullmatch(rule) for rule in covers):
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
        or not _nonempty_text(catalog.get("path"))
        or not isinstance(catalog.get("probe_count"), int)
        or not isinstance(catalog.get("dimension_count"), int)
    ):
        raise ConfigurationError("场景清单 probe_catalog 绑定非法。")

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
        _validate_capture_job(index, job, job_ids)

    # 场景、probe 与 Job 的绑定闭合：每个 probe 恰好由一步执行；被执行的 probe 都属于某个场景，场景列出的 probe
    # 都被执行；Job 的 scenario_ids 等于其各步 probe 所属场景之并，covers 不超出这些场景的 covers。
    scenario_by_id = {item["scenario_id"]: item for item in scenarios}
    probe_scenarios: dict[str, set[str]] = {}
    for item in scenarios:
        for probe in item["probe_ids"]:
            probe_scenarios.setdefault(probe, set()).add(item["scenario_id"])
    executed: list[str] = [step["probe_id"] for job in jobs for step in job["steps"]]
    if len(executed) != len(set(executed)):
        raise ConfigurationError("同一 probe 只能由一个取证步骤执行。")
    if set(executed) != set(probe_scenarios):
        raise ConfigurationError(
            "场景声明的 probe 与 Job 执行的 probe 不一致："
            f"未执行={sorted(set(probe_scenarios) - set(executed))}，未归属场景={sorted(set(executed) - set(probe_scenarios))}"
        )
    if catalog["probe_count"] != len(executed):
        raise ConfigurationError("场景清单 probe_catalog.probe_count 与执行的 probe 数不一致。")
    for job in jobs:
        expected = set().union(*(probe_scenarios[step["probe_id"]] for step in job["steps"]))
        if set(job["scenario_ids"]) != expected:
            raise ConfigurationError(f"{job['id']} 的 scenario_ids 必须等于其各步 probe 所属场景之并。")
        proven = set().union(*(set(scenario_by_id[sid]["covers"]) for sid in job["scenario_ids"]))
        if not set(job["covers"]).issubset(proven):
            raise ConfigurationError(f"{job['id']} 的 covers 未被绑定场景证明。")
    roots = [root for job in jobs for root in job["evidence_roots"]]
    if len(roots) != len(set(roots)):
        raise ConfigurationError("同一证据根只能由单一 Job 独占。")


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
    unexpected = sorted(set(context) - set(contract))
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
        # 候选取证 Job 由 B4-01 落实；在那之前候选侧全部未映射，如实列出。
        "candidate_unmapped": sorted(set(rules) - mapped["candidate"]),
    }


def validate_official_coverage(jobs: Sequence[Any], rules: Sequence[str]) -> None:
    """full 套件：official 必需 Job 的 covers 必须覆盖规则全集（候选侧覆盖随 B4-01 的候选 Job 一并校验）。"""

    missing = coverage_plan(jobs, rules)["official_unmapped"]
    if missing:
        raise ConfigurationError(f"full 场景清单存在 official 未映射规则：{missing}")


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


def _step_probes(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """probe → {job_id, synthetic, scenarios}（来自已校验的场景清单）。"""

    scenarios: dict[str, set[str]] = {}
    for item in payload["evidence_scenarios"]:
        for probe in item["probe_ids"]:
            scenarios.setdefault(probe, set()).add(item["scenario_id"])
    probes: dict[str, dict[str, Any]] = {}
    for job in payload["capture_jobs"]:
        for step in job["steps"]:
            probes[step["probe_id"]] = {
                "job_id": job["id"],
                "synthetic": "--acknowledge-synthetic-responses" in step["argv"],
                "scenarios": scenarios[step["probe_id"]],
            }
    return probes


def _validate_connection(probe: str, connection: Any, *, synthetic: bool) -> dict[str, Any]:
    if not isinstance(connection, dict):
        raise ConfigurationError(f"{probe} 的连接规则必须是对象。")
    keys = set(connection)
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

    只有两种合法形态：一条无序规则独占；或逐序号规则 1..k 连续且唯一，加至多一条从 k+1 起的尾部规则。
    """

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
    probes = _step_probes(scenario_payload)
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
            if not _unique_strings(scenario_ids) or not set(scenario_ids).issubset(probes[probe]["scenarios"]):
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
                slot["connections"].append((index, _validate_connection(probe, rule["connection"], synthetic=probes[probe]["synthetic"])))
        job_plans: dict[str, Any] = {}
        for probe in job_probes[job_id]:
            slot = by_probe.get(probe)
            if slot is None or not slot["connections"]:
                raise ConfigurationError(f"{job_id}／{probe} 缺少中继请求的逐连接标签。")
            if "relay_manifest" not in slot["kinds"]:
                raise ConfigurationError(f"{job_id}／{probe} 缺少 relay_manifest 标签。")
            if probes[probe]["synthetic"] != ("synthetic_intervention" in slot["kinds"]):
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
    arguments.rule_manifest = Path(arguments.rule_manifest or baseline_rule_manifest_path(arguments.baseline_version))
    arguments.scenario_manifest = Path(arguments.scenario_manifest or scenario_manifest_path(arguments.target_version))
    arguments.evidence_labels = Path(arguments.evidence_labels or evidence_labels_path(arguments.target_version))
    payload = _read_json(arguments.scenario_manifest, "场景清单")
    validate_scenario_manifest(payload)
    contract = _variable_contract(payload)
    for name in CAMPAIGN_VARIABLES:
        if name not in contract or contract[name]["scope"] != "campaign":
            raise ConfigurationError(f"场景清单变量合同缺少 Campaign 变量 {name}。")
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
    return payload


def plan_context(arguments: argparse.Namespace) -> dict[str, str]:
    """Campaign 作用域变量的实际取值（字符串）；运行期变量不在其中。"""

    context: dict[str, str] = {}
    for name in CAMPAIGN_VARIABLES:
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
        validate_official_coverage(jobs, rules)
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
            name: str(getattr(arguments, name))
            for name in CAMPAIGN_VARIABLES
            if name not in {"campaign_id", "campaign_dir", "target_version", "target_sha256"}
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
