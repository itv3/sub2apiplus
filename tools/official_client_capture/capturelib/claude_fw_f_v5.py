"""Claude Code 升级取证 FW-F v5 场景目录：复用 v4 的 79 个 probe 与驱动，判定按场景清单冻结的版本事实取。

一、与 v4 的关系
================
v5 不新增、不删除、不改写任何场景：79 个 probe 的 ID、驱动、提示词、CLI 参数、注入环境、夹具、合成响应计划与维度
都取自 ``claude_fw_f_v4``（``PROBES`` 由 v4 逐个 ``dataclasses.replace`` 派生，``validate_probe_catalog`` 逐字段核对，
只有 ``PROBE_OVERRIDES`` 登记的字段与 v4 不同），执行直接调用 v4 的驱动函数。v4 文件本身不改：它在
``capturelib/identity.py`` 的执行源清单里，改它会让两个客户端的执行源摘要都变化。

变化只在判定：

* **advisor 正例**：wire 上 advisor 工具描述符的 ``type`` 必须等于版本事实冻结的工具类型，``model`` 必须等于客户端
  在 debug 日志里宣告的 advisor 模型（宣告句式也由版本事实冻结），不再写死 2.1.226 的 ``claude-fable-5``；
* **两个灰度 Header 与 /usage 的用量查询**（``anthropic-usage-limit``、``anthropic-dispatch-id``、``/api/oauth/usage``）：
  只记录、不让取证失败。逐请求留证（连接号、流偏移、是否子代理请求、Header 是否出现及取值，或用量查询的完整请求行），
  连同版本事实与条件（驱动、账号状态、隐私环境、请求计数）写进维度证据的 ``observed_only``，交给 B3／B4 分类评估；
  维度本身只要求「能观测」（有推理请求，或隐私条件按冻结值生效），不对「发了还是没发」下结论；
* **模型目录请求**：判定不变（隐私条件下不得请求 ``/v1/models``），只把结论名里的版本号去掉；另按路径（去掉查询串）
  记录全部模型目录请求，供 B3 核对；
* 其余维度（含 ``anthropic-beta`` 里 oauth 出现次数这类低风险项）原样沿用 v4 的判定函数。

二、版本事实
============
版本事实是随场景清单冻结的一份小 JSON（``claude-code-fw-f-v5-version-facts/v1``），只放判定依赖、并且会随版本变化
的字面量，以及它们的来源说明：

* ``advisor.tool_type``：advisor 工具描述符的类型常量；
* ``advisor.model_declaration``：客户端启用 advisor 时写进 debug 日志的宣告句式，拆成模型名前后两段固定文本；
* ``provenance``：这些字面量取自哪一份官方二进制（平台与摘要）与依据说明。

场景清单在顶层 ``version_facts`` 绑定文件路径与摘要，每个 v5 步骤以 ``--fw-f-v5-version-facts`` 与
``--fw-f-v5-version-facts-sha256`` 把同一份文件交给中继；中继读文件、核摘要与 ``claude_version``，再交给本模块。

三、中继如何加载本模块
=====================
中继按步骤 argv 的 ``--fw-f-v5-catalog`` 从自身工具根的 ``capturelib/`` 加载本模块，源码里不静态 import 本模块：
Codex 0.157.0／0.160.0 的场景清单经 ``capturelib/identity.py`` 的执行源清单把中继及其 import 闭包登记为作业依赖（R16
门禁要求声明与依赖分析逐项相等），中继每多一条静态 import，这些 Codex 清单就要跟着改（那是 Codex 的 wire 层）。本模块
对 Claude 作业的依赖由步骤 argv 里的路径体现，照常出现在 Claude 场景清单每个作业的 ``tool_dependencies`` 里。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.capturelib import claude_fw_f_v4 as v4
from tools.official_client_capture.capturelib.security import (
    ensure_private_directory,
    file_sha256,
    secure_write_json,
)


SCHEMA_CATALOG = "claude-code-fw-f-v5-scenario-catalog/v1"
SCHEMA_RESULT = "claude-code-fw-f-v5-probe-result/v1"
SCHEMA_DIMENSION_EVIDENCE = "claude-code-fw-f-v5-dimension-evidence/v1"
SCHEMA_VERSION_FACTS = "claude-code-fw-f-v5-version-facts/v1"
SUMMARY_FILENAME = "v5-summary.json"
DEBUG_LOG_FILENAME = "debug.log"
REQUIRED_MATRIX_DIMENSIONS = v4.REQUIRED_MATRIX_DIMENSIONS

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOOL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# advisor 模型 ID：官方模型 ID（可带 ``[1m]`` 这类上下文后缀），不接受空白与控制字符。
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?:\[[a-z0-9]{1,16}\])?$")
DECLARATION_TEXT_LIMIT = 256

# 判定口径的结论名：一律不含版本号（版本差异由版本事实与 B3 分类承担）。
ADVISOR_POSITIVE_CONCLUSION = "advisor_explicitly_enabled_positive"
ADVISOR_NEGATIVE_CONCLUSION = "advisor_default_disabled_negative"
MODELS_CONCLUSION = "model_capabilities_not_requested_under_privacy_state"
USAGE_LIMIT_CONCLUSION = "usage_limit_header_observed_only"
DISPATCH_CONCLUSION = "dispatch_header_observed_only"
USAGE_QUERY_CONCLUSION = "usage_query_observed_only"

# 只记录、不判失败的维度 → 观测对象（Header 名或请求路径）与对应结论名。
OBSERVED_ONLY_DIMENSIONS: Mapping[str, Mapping[str, str]] = {
    "header.usage_limit": {"kind": "header", "target": "anthropic-usage-limit", "conclusion": USAGE_LIMIT_CONCLUSION},
    "header.dispatch_id": {"kind": "header", "target": "anthropic-dispatch-id", "conclusion": DISPATCH_CONCLUSION},
    "aux.usage": {"kind": "request_path", "target": "/api/oauth/usage", "conclusion": USAGE_QUERY_CONCLUSION},
}
# 模型目录：v4 原判定按精确请求身份禁止这两条；v5 另按路径记录全部模型目录请求。
MODELS_FORBIDDEN_IDENTITIES = ("GET /v1/models", "GET /v1/models?beta=true")
MODELS_PATH = "/v1/models"

# v5 相对 v4 的全部字段差异（``validate_probe_catalog`` 要求逐项相符，不多不少）。
PROBE_OVERRIDES: Mapping[str, Mapping[str, Any]] = {
    "v4-agent-usage-limit": {"runtime_conclusion": USAGE_LIMIT_CONCLUSION},
    "v4-dispatch-account-negative": {"runtime_conclusion": DISPATCH_CONCLUSION},
    # 用量查询改为只记录：v4 的 forbidden_requests 会让 wire 合同在发出查询时失败，这里去掉，事实写进 observed_only。
    "v4-tui-usage": {"runtime_conclusion": USAGE_QUERY_CONCLUSION, "forbidden_requests": ()},
    "v4-models-privacy-state": {"runtime_conclusion": MODELS_CONCLUSION},
}

PROBES: dict[str, v4.ClaudeFWFCompleteProbe] = {
    probe_id: dataclasses.replace(v4.PROBES[probe_id], **PROBE_OVERRIDES.get(probe_id, {}))
    for probe_id in v4.PROBE_IDS
}
PROBE_IDS = tuple(sorted(PROBES))
SYNTHETIC_RESPONSE_PLANS = tuple(sorted({probe.response_plan for probe in PROBES.values() if probe.response_plan}))

_FACTS_FIELDS = frozenset({"schema_version", "claude_version", "advisor", "provenance"})
_ADVISOR_FIELDS = frozenset({"tool_type", "model_declaration"})
_DECLARATION_FIELDS = frozenset({"prefix", "suffix"})
_PROVENANCE_FIELDS = frozenset({"binary_platform", "binary_sha256", "basis"})


class ClaudeFWFV5ProbeError(v4.ClaudeFWFCompleteProbeError):
    """v5 场景目录、版本事实或判定不满足冻结合同（继承 v4 的错误类，中继的既有异常处理照常生效）。"""


def get_probe(probe_id: str) -> v4.ClaudeFWFCompleteProbe:
    try:
        return PROBES[probe_id]
    except KeyError as error:
        raise ClaudeFWFV5ProbeError(f"未知 FW-F v5 probe：{probe_id}") from error


# ---------------------------------------------------------------------------
# 目录：与 v4 同构
# ---------------------------------------------------------------------------


def validate_probe_catalog() -> None:
    """v4 目录本身合法，且 v5 与 v4 同构：同一组 probe，只有登记的覆盖字段不同，结论名不含版本号。"""

    v4.validate_probe_catalog()
    if PROBE_IDS != v4.PROBE_IDS:
        raise ClaudeFWFV5ProbeError("v5 目录的 probe 集合必须与 v4 完全相同。")
    unknown = sorted(set(PROBE_OVERRIDES) - set(PROBES))
    if unknown:
        raise ClaudeFWFV5ProbeError(f"v5 覆盖项引用了不存在的 probe：{unknown}")
    for probe_id, probe in PROBES.items():
        base = v4.PROBES[probe_id]
        changed = {field.name for field in dataclasses.fields(base) if getattr(probe, field.name) != getattr(base, field.name)}
        if changed != set(PROBE_OVERRIDES.get(probe_id, {})):
            raise ClaudeFWFV5ProbeError(f"{probe_id} 与 v4 的差异必须恰好是登记的覆盖字段：{sorted(changed)}")
        conclusion = probe.runtime_conclusion or ""
        if re.search(r"\d+_\d+_\d+", conclusion):
            raise ClaudeFWFV5ProbeError(f"{probe_id} 的结论名不得写死版本号：{conclusion}")
    for dimension, spec in OBSERVED_ONLY_DIMENSIONS.items():
        owners = [probe for probe in PROBES.values() if dimension in probe.dimensions]
        if not owners or any(probe.runtime_conclusion != spec["conclusion"] for probe in owners):
            raise ClaudeFWFV5ProbeError(f"只记录维度 {dimension} 必须且只能由结论为 {spec['conclusion']} 的 probe 覆盖。")
        if spec["kind"] == "request_path" and any(
            identity.split(" ", 1)[-1].split("?", 1)[0] == spec["target"]
            for probe in owners
            for identity in (*probe.forbidden_requests, *probe.required_requests)
        ):
            raise ClaudeFWFV5ProbeError(f"只记录维度 {dimension} 的观测请求不得再出现在 wire 合同里。")
    models = [probe for probe in PROBES.values() if "aux.models" in probe.dimensions]
    if not models or any(
        probe.runtime_conclusion != MODELS_CONCLUSION or set(probe.forbidden_requests) != set(MODELS_FORBIDDEN_IDENTITIES)
        for probe in models
    ):
        raise ClaudeFWFV5ProbeError("模型目录维度必须保持 v4 的禁止请求判定，结论名为版本无关的冻结值。")


def catalog_document() -> dict[str, Any]:
    validate_probe_catalog()
    dimensions = {
        dimension: sorted(probe_id for probe_id, probe in PROBES.items() if dimension in probe.dimensions)
        for dimension in sorted(REQUIRED_MATRIX_DIMENSIONS)
    }
    return {
        "schema_version": SCHEMA_CATALOG,
        "base_catalog_schema": v4.SCHEMA_CATALOG,
        "probe_count": len(PROBES),
        "required_matrix_dimension_count": len(REQUIRED_MATRIX_DIMENSIONS),
        "required_matrix_dimensions": dimensions,
        "observed_only_dimensions": {dimension: dict(spec) for dimension, spec in sorted(OBSERVED_ONLY_DIMENSIONS.items())},
        "probe_overrides": {probe_id: sorted(fields) for probe_id, fields in sorted(PROBE_OVERRIDES.items())},
        "version_facts_schema": SCHEMA_VERSION_FACTS,
        "privacy_configuration": {
            "telemetry_disabled": True,
            "nonessential_traffic_disabled": True,
            "absence_generates_rule": False,
        },
        "probes": [dataclasses.asdict(PROBES[probe_id]) for probe_id in PROBE_IDS],
    }


# ---------------------------------------------------------------------------
# 版本事实
# ---------------------------------------------------------------------------


def _fixed_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > DECLARATION_TEXT_LIMIT
        or any(character in value for character in "\r\n\x00")
    ):
        raise ClaudeFWFV5ProbeError(f"版本事实 {label} 必须是不含换行的非空短文本。")
    return value


def validate_version_facts(value: Any, *, expected_version: str) -> dict[str, Any]:
    """版本事实的失败关闭校验；返回深拷贝（调用方改动返回值不影响原文档）。"""

    if not isinstance(value, dict) or set(value) != _FACTS_FIELDS:
        raise ClaudeFWFV5ProbeError("版本事实字段不闭合。")
    if value.get("schema_version") != SCHEMA_VERSION_FACTS:
        raise ClaudeFWFV5ProbeError("版本事实 schema_version 不受支持。")
    if not isinstance(expected_version, str) or not VERSION_RE.fullmatch(expected_version):
        raise ClaudeFWFV5ProbeError("版本事实的期望版本必须是三段版本号。")
    if value.get("claude_version") != expected_version:
        raise ClaudeFWFV5ProbeError(
            f"版本事实属于 {value.get('claude_version')!r}，与本轮目标版本 {expected_version} 不一致。"
        )
    advisor = value.get("advisor")
    if not isinstance(advisor, dict) or set(advisor) != _ADVISOR_FIELDS:
        raise ClaudeFWFV5ProbeError("版本事实 advisor 字段不闭合。")
    if not isinstance(advisor.get("tool_type"), str) or not TOOL_TYPE_RE.fullmatch(advisor["tool_type"]):
        raise ClaudeFWFV5ProbeError("版本事实 advisor.tool_type 非法。")
    declaration = advisor.get("model_declaration")
    if not isinstance(declaration, dict) or set(declaration) != _DECLARATION_FIELDS:
        raise ClaudeFWFV5ProbeError("版本事实 advisor.model_declaration 字段不闭合。")
    _fixed_text(declaration.get("prefix"), "advisor.model_declaration.prefix")
    _fixed_text(declaration.get("suffix"), "advisor.model_declaration.suffix")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
        raise ClaudeFWFV5ProbeError("版本事实 provenance 字段不闭合。")
    if not isinstance(provenance.get("binary_platform"), str) or not re.fullmatch(r"[a-z]+/[a-z0-9_]+", provenance["binary_platform"]):
        raise ClaudeFWFV5ProbeError("版本事实 provenance.binary_platform 非法。")
    if not isinstance(provenance.get("binary_sha256"), str) or not SHA256_RE.fullmatch(provenance["binary_sha256"]):
        raise ClaudeFWFV5ProbeError("版本事实 provenance.binary_sha256 非法。")
    if not isinstance(provenance.get("basis"), str) or not provenance["basis"].strip():
        raise ClaudeFWFV5ProbeError("版本事实 provenance.basis 不能为空。")
    return json.loads(json.dumps(value, ensure_ascii=False))


def version_facts_sha256(value: Mapping[str, Any]) -> str:
    """版本事实内容的规范摘要（与文件字节无关，写进结果与维度证据，供 B3 对照）。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_version_facts(path: Path, expected_sha256: str, *, expected_version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取并核验一份版本事实文件：可信普通文件、文件摘要等于冻结值、内容合法且属于本轮目标版本。

    返回 ``(版本事实, 绑定)``；绑定只含路径、文件摘要、内容规范摘要与版本，不含文件内容以外的推断。
    """

    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ClaudeFWFV5ProbeError(f"版本事实文件必须是可信绝对普通文件：{path}")
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise ClaudeFWFV5ProbeError("版本事实文件的冻结摘要格式非法。")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ClaudeFWFV5ProbeError("版本事实文件摘要与场景清单冻结值不一致。")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClaudeFWFV5ProbeError("版本事实文件不是合法 JSON。") from error
    facts = validate_version_facts(value, expected_version=expected_version)
    binding = {
        "path": str(path),
        "sha256": digest,
        "canonical_sha256": version_facts_sha256(facts),
        "claude_version": facts["claude_version"],
    }
    return facts, binding


def advisor_model_declarations(text: str, facts: Mapping[str, Any]) -> list[str]:
    """debug 日志里客户端宣告的 advisor 模型（按出现顺序；句式取版本事实，模型名必须是合法 ID）。"""

    declaration = facts["advisor"]["model_declaration"]
    prefix, suffix = str(declaration["prefix"]), str(declaration["suffix"])
    models: list[str] = []
    for line in text.splitlines():
        start = line.find(prefix)
        if start < 0:
            continue
        rest = line[start + len(prefix):]
        end = rest.find(suffix)
        if end <= 0:
            continue
        model = rest[:end]
        if MODEL_ID_RE.fullmatch(model):
            models.append(model)
    return models


# ---------------------------------------------------------------------------
# 执行：复用 v4 驱动
# ---------------------------------------------------------------------------


def _debug_declarations(output_dir: Path, facts: Mapping[str, Any]) -> dict[str, Any]:
    """v4 驱动已把 debug 日志脱敏归档为 ``<output>/debug.log``；从中取 advisor 宣告（不改写日志）。"""

    path = Path(output_dir) / DEBUG_LOG_FILENAME
    if path.is_symlink() or not path.is_file():
        return {"source": DEBUG_LOG_FILENAME, "present": False, "advisor_models": []}
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "source": DEBUG_LOG_FILENAME,
        "present": True,
        "source_sha256": file_sha256(path),
        "advisor_models": advisor_model_declarations(text, facts),
    }


def run_claude_fw_f_v5_probe(
    *,
    claude_bin: str,
    model: str,
    probe_id: str,
    environment: dict[str, str],
    output_dir: Path,
    timeout: int,
    known_secrets: Mapping[str, str],
    version_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """执行一个冻结 v5 场景（驱动与 v4 相同），写入 ``v5-summary.json``。"""

    validate_probe_catalog()
    facts = validate_version_facts(dict(version_facts), expected_version=str(version_facts.get("claude_version", "")))
    probe = get_probe(probe_id)
    ensure_private_directory(output_dir)
    for key, value in probe.env_dict().items():
        if environment.get(key) != value:
            raise ClaudeFWFV5ProbeError(f"{probe_id} 运行环境与冻结目录不一致：{key}")
    if probe.driver == "v3":
        inner = v4.run_v3_probe(
            claude_bin=claude_bin,
            model=model,
            probe_id=str(probe.source_v3_probe),
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    elif probe.driver == "legacy":
        inner = v4.run_claude_scenario(
            claude_bin=claude_bin,
            model=model,
            scenario=str(probe.legacy_scenario),
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            runtime_secret=(known_secrets.get("access_token") or known_secrets.get("claude_oauth_access_token")),
            known_secrets=dict(known_secrets),
        )
    elif probe.driver == "tui":
        inner = v4._run_tui_probe(
            claude_bin=claude_bin,
            model=model,
            probe=probe,
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    else:
        inner = v4._run_print_probe(
            claude_bin=claude_bin,
            model=model,
            probe=probe,
            environment=environment,
            output_dir=output_dir,
            timeout=timeout,
            known_secrets=known_secrets,
        )
    if "tool.advisor" in probe.dimensions and probe.require_debug_log:
        inner = dict(inner)
        inner["debug_declarations"] = _debug_declarations(output_dir, facts)
    result = {
        "schema_version": SCHEMA_RESULT,
        "probe_id": probe_id,
        "driver": probe.driver,
        "source_v3_probe": probe.source_v3_probe,
        "legacy_scenario": probe.legacy_scenario,
        "dimensions": list(probe.dimensions),
        "target_host": probe.target_host,
        "response_plan": probe.response_plan,
        "expected_outcome": probe.expected_outcome,
        "message_request_expectation": probe.message_request_expectation,
        "require_pcap": probe.require_pcap,
        "runtime_conclusion": probe.runtime_conclusion,
        "inner_result": inner,
        "valid": inner.get("valid") is True,
        "version_facts": {"claude_version": facts["claude_version"], "canonical_sha256": version_facts_sha256(facts)},
        "catalog_producer": {
            "path": "tools/official_client_capture/capturelib/claude_fw_f_v5.py",
            "sha256": file_sha256(Path(__file__)),
        },
        "driver_producer": {
            "path": "tools/official_client_capture/capturelib/claude_fw_f_v4.py",
            "sha256": file_sha256(Path(v4.__file__)),
        },
    }
    secure_write_json(Path(output_dir) / SUMMARY_FILENAME, result)
    return result


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _path_of(request: Mapping[str, Any]) -> str:
    return str(request.get("request_target", "")).split("?", 1)[0]


def _conditions(probe: v4.ClaudeFWFCompleteProbe, scenario_summary: Mapping[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """只记录维度的观测条件：B3 据此判断「这个版本、这个条件下」是否发出。"""

    environment = v4._privacy_environment(scenario_summary)
    return {
        "driver": probe.driver,
        "requires_account_state": probe.requires_account_state,
        "injected_env_keys": sorted(probe.env_dict()),
        "privacy_environment": {
            "DISABLE_TELEMETRY": environment.get("DISABLE_TELEMETRY"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": environment.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
        },
        "messages_request_count": len(messages),
        "subagent_request_count": sum(1 for request in messages if "x-claude-code-agent-id" in request.get("header_map", {})),
    }


def _observed_header(
    dimension: str,
    probe: v4.ClaudeFWFCompleteProbe,
    requests: list[dict[str, Any]],
    scenario_summary: Mapping[str, Any],
    observation: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """灰度 Header：逐推理请求记录是否出现与取值；有推理请求即可观测，出现与否都不判失败。"""

    name = OBSERVED_ONLY_DIMENSIONS[dimension]["target"]
    messages = v4._message_requests(requests)
    rows = [
        {
            "connection_id": request["connection_id"],
            "stream_offset": request["stream_offset"],
            "subagent_request": "x-claude-code-agent-id" in request.get("header_map", {}),
            "header_present": name in request.get("header_map", {}),
            "value": request.get("header_map", {}).get(name),
        }
        for request in messages
    ]
    observation.update(
        {
            "judgment": "observed_only",
            "header": name,
            "observed": any(row["header_present"] for row in rows),
            "observed_values": sorted({str(row["value"]) for row in rows if row["value"] is not None}),
            "per_request": rows,
            "conditions": _conditions(probe, scenario_summary, messages),
        }
    )
    return bool(messages), observation


def _observed_usage_query(
    probe: v4.ClaudeFWFCompleteProbe,
    requests: list[dict[str, Any]],
    scenario_summary: Mapping[str, Any],
    observation: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """/usage 的用量查询：按路径（去掉查询串）记录全部查询请求；隐私条件按冻结值生效即可观测。"""

    target = OBSERVED_ONLY_DIMENSIONS["aux.usage"]["target"]
    rows = [
        {
            "connection_id": request["connection_id"],
            "stream_offset": request["stream_offset"],
            "method": request["method"],
            "request_target": request["request_target"],
        }
        for request in requests
        if _path_of(request) == target
    ]
    conditions = _conditions(probe, scenario_summary, v4._message_requests(requests))
    observation.update(
        {
            "judgment": "observed_only",
            "request_path": target,
            "observed": bool(rows),
            "observed_requests": rows,
            "conditions": conditions,
        }
    )
    return conditions["privacy_environment"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1", observation


def _advisor_positive(
    probe: v4.ClaudeFWFCompleteProbe,
    requests: list[dict[str, Any]],
    scenario_summary: Mapping[str, Any],
    facts: Mapping[str, Any],
    observation: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """advisor 正例：描述符类型等于版本事实，模型等于 debug 日志唯一宣告的模型。"""

    messages = v4._message_requests(requests)
    descriptors = [tool for tool in v4._message_tool_descriptors(messages) if tool.get("name") == "advisor"]
    inner = scenario_summary.get("inner_result") if isinstance(scenario_summary.get("inner_result"), Mapping) else {}
    declarations = inner.get("debug_declarations") if isinstance(inner.get("debug_declarations"), Mapping) else {}
    declared = [str(item) for item in declarations.get("advisor_models") or [] if isinstance(item, str)]
    distinct = sorted(set(declared))
    expected_type = str(facts["advisor"]["tool_type"])
    passed = len(distinct) == 1 and any(
        tool.get("type") == expected_type and tool.get("model") == distinct[0] for tool in descriptors
    )
    observation.update(
        {
            "judgment": "asserted",
            "expected_tool_type": expected_type,
            "declared_advisor_models": distinct,
            "declaration_count": len(declared),
            "debug_log_present": declarations.get("present") is True,
            "advisor_descriptors": [
                {"type": tool.get("type"), "model": tool.get("model"), "defer_loading": tool.get("defer_loading")}
                for tool in descriptors
            ],
        }
    )
    return passed, observation


def _models_not_requested(
    probe: v4.ClaudeFWFCompleteProbe, requests: list[dict[str, Any]], observation: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """模型目录：沿用 v4 的禁止请求判定（精确请求身份），另按路径记录全部模型目录请求。"""

    identities = [v4._request_identity(request) for request in requests]
    passed = probe.runtime_conclusion == MODELS_CONCLUSION and not any(
        identity in identities for identity in MODELS_FORBIDDEN_IDENTITIES
    )
    observation.update(
        {
            "judgment": "asserted",
            "model_list_requests": [
                {"connection_id": request["connection_id"], "stream_offset": request["stream_offset"], "request_target": request["request_target"]}
                for request in requests
                if request.get("method") == "GET" and _path_of(request) == MODELS_PATH
            ],
        }
    )
    return passed, observation


def _dimension_passed(
    dimension: str,
    probe: v4.ClaudeFWFCompleteProbe,
    requests: list[dict[str, Any]],
    scenario_summary: Mapping[str, Any],
    relay_integrity: Mapping[str, Any],
    pcap_receipt: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """v5 判定：先取 v4 的同名判定得到同形观测，再对登记的维度按 v5 口径改判并补充观测。"""

    v4_passed, observation = v4._dimension_passed(dimension, probe, requests, scenario_summary, relay_integrity, pcap_receipt)
    observation = dict(observation)
    if dimension in ("header.usage_limit", "header.dispatch_id"):
        return _observed_header(dimension, probe, requests, scenario_summary, observation)
    if dimension == "aux.usage":
        return _observed_usage_query(probe, requests, scenario_summary, observation)
    if dimension == "aux.models":
        return _models_not_requested(probe, requests, observation)
    if dimension == "tool.advisor" and probe.runtime_conclusion != ADVISOR_NEGATIVE_CONCLUSION:
        return _advisor_positive(probe, requests, scenario_summary, facts, observation)
    observation["judgment"] = "asserted"
    return v4_passed, observation


def _wire_inventory(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """与 v4 维度证据同形的逐请求清单（B2-07 的中继证据视图按它取连接号、方法、路径与流偏移）。"""

    return [
        {
            "connection_id": request["connection_id"],
            "evidence_file": request["evidence_file"],
            "stream_offset": request["stream_offset"],
            "method": request["method"],
            "request_target": request["request_target"],
            "http_version": request["http_version"],
            "header_names": [item["name"] for item in request["headers"]],
            "body_keys": list(request["body"].keys()) if isinstance(request["body"], dict) else [],
            "message_tool_names": (
                sorted(
                    str(tool.get("name"))
                    for tool in request["body"].get("tools", [])
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                )
                if isinstance(request["body"], dict) and isinstance(request["body"].get("tools"), list)
                else []
            ),
            "raw_sha256": request["raw_sha256"],
            "wire_body_sha256": request["wire_body_sha256"],
        }
        for request in requests
    ]


def validate_v5_probe_evidence(
    *,
    probe_id: str,
    relay_root: Path,
    scenario_summary: Mapping[str, Any],
    relay_integrity: Mapping[str, Any],
    pcap_receipt: Mapping[str, Any],
    version_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """按 v5 口径核对一个 probe 的实际 wire、工具与状态维度，写出完整观测（只记录维度不影响结果）。"""

    facts = validate_version_facts(dict(version_facts), expected_version=str(version_facts.get("claude_version", "")))
    probe = get_probe(probe_id)
    requests = v4._parse_wire_requests(Path(relay_root))
    identities = [v4._request_identity(request) for request in requests]
    messages = v4._message_requests(requests)
    message_tools = v4._message_tool_names(messages)
    contract_failures: list[str] = []
    if probe.message_request_expectation == "at-least-one" and not messages:
        contract_failures.append("messages_request_missing")
    if probe.message_request_expectation == "zero" and messages:
        contract_failures.append("unexpected_messages_request")
    for expected in probe.required_requests:
        if expected not in identities:
            contract_failures.append(f"required_request_missing:{expected}")
    for forbidden in probe.forbidden_requests:
        if forbidden in identities:
            contract_failures.append(f"forbidden_request_observed:{forbidden}")
    for expected in probe.required_message_tools:
        if expected not in message_tools:
            contract_failures.append(f"required_message_tool_missing:{expected}")
    for forbidden in probe.forbidden_message_tools:
        if forbidden in message_tools:
            contract_failures.append(f"forbidden_message_tool_observed:{forbidden}")
    assertions: list[dict[str, Any]] = []
    observed_only: list[dict[str, Any]] = []
    for dimension in probe.dimensions:
        passed, observation = _dimension_passed(
            dimension, probe, requests, scenario_summary, relay_integrity, pcap_receipt, facts
        )
        assertions.append(
            {
                "assertion_id": f"DIM-{probe_id}-{dimension}",
                "dimension": dimension,
                "result": "passed" if passed else "failed",
                "observation": observation,
            }
        )
        if dimension in OBSERVED_ONLY_DIMENSIONS:
            observed_only.append(
                {
                    "dimension": dimension,
                    "kind": OBSERVED_ONLY_DIMENSIONS[dimension]["kind"],
                    "target": OBSERVED_ONLY_DIMENSIONS[dimension]["target"],
                    "evaluable": passed,
                    "observed": observation.get("observed") is True,
                    "observed_values": list(observation.get("observed_values") or []),
                    "observed_request_count": len(observation.get("observed_requests") or []),
                    "conditions": observation.get("conditions"),
                }
            )
    failed_dimensions = [assertion["dimension"] for assertion in assertions if assertion["result"] != "passed"]
    return {
        "schema_version": SCHEMA_DIMENSION_EVIDENCE,
        "probe_id": probe_id,
        "runtime_conclusion": probe.runtime_conclusion,
        "version_facts": {"claude_version": facts["claude_version"], "canonical_sha256": version_facts_sha256(facts)},
        "actual_wire_inventory": _wire_inventory(requests),
        "wire_contract": {
            "required_requests": list(probe.required_requests),
            "forbidden_requests": list(probe.forbidden_requests),
            "required_message_tools": list(probe.required_message_tools),
            "forbidden_message_tools": list(probe.forbidden_message_tools),
            "failures": contract_failures,
        },
        "dimension_assertions": assertions,
        "observed_only": observed_only,
        "failed_dimensions": failed_dimensions,
        "result": "passed" if not contract_failures and not failed_dimensions else "failed",
    }
