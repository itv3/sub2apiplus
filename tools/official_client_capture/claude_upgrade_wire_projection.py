"""Claude Wire 投影算法（受管）：Profile、Wire、模型能力目录与 Release 身份的确定性投影函数（Claude 升级 B3-03 补充项）。

一、为什么在受管根里
==================
这些函数原在仓库根的 ``tools/generate_claude_fw_g_profile.py``（指南 §4.3.2 的确定性投影工具）。VC-3 的
``claude_upgrade_release`` 在进程内调用它们，可 ARM64 受监督部署、真实链外壳与 pre-A3 运行器的副本树都只交换两个受管根
（``tools/official_client_capture``、``tools/official_client_control``）：根外的生成器在部署树里不存在，一导入就失败；
它的变化也不进 Claude 工具身份（身份只覆盖两个受管根），是个盲区。2026-10-07 起投影函数逐字搬进本模块，历史生成器改为
从这里导入，历史命令行与 2.1.226 逐字回归（四个摘要）不变。受管根内的模块一律不得导入根外的仓库模块，由
``tests/test_managed_root_imports.py`` 静态扫描、Claude 身份引擎在闭包里失败关闭两道把守。

二、本模块包含什么
================
只含投影算法与它的合同常量，全部是纯函数：

* Wire 投影策略（``claude-code-wire-projection-policy/v1``）的校验与加载：模型闭集与角色、effort 闭集、版本指纹与 cch 的
  attestation、工具目录选择器、TLS 期望向量、实现策略三段、thinking.display 取值、模型目录的别名与未知模型策略；
* probe 命名（FW-F v5 probe 目录的冻结事实；非主模型的 probe 为 ``<模型>-<基础 probe>``）与投影所需 probe 的推导；
* HTTP/1.1 请求流解析（与历史 ``claude_fw_f_profile`` 解析器逐字同义，不导入它）、TLS ClientHello 解析与期望向量核对；
* 版本指纹复算（按策略的 salt 与 message index 复算观测请求里的 ``cc_version``）；
* 实现策略、工具策略、thinking.display 策略、模型场景与模型能力目录、Wire 制品、Release／bundle 身份的构造。

投影函数只经「证据来源」对象取请求，不认目录布局，也不读写文件（``load_projection_policy`` 读调用方给出的策略文件除外）。
来源对象的方法：``messages``（某 probe 的推理请求，按连接序）、``select_messages``、``base_select``（主模型 probe 的任意
请求）、``model_select``／``model_requests``（非主模型 probe）、``thinking_display_request``、``thinking_production_receipt``、
``evidence_ledger``、``catalog_evidence``。两种来源：

* Claude 升级 VC-1 布局：``claude_upgrade_release.UpgradeEvidenceSource``（capture manifest 的逐连接标签 + EvidencePackage v3
  的外部证据绑定）；
* 历史 FW-F／FW-G 布局：生成器里的 ``FwfCampaignSource``，只用于重放 2.1.226 历史制品。

三、本模块不含什么
================
历史 FW-G 专用的部分留在生成器：generation policy（``claude-fw-g-generation-policy/v2``）的版本事实与冻结输入、
``FwfCampaignSource`` 与历史布局的读取、追加式迁移收据、源码 transition、历史 Release 身份与命令行入口。它们只用于
重放 2.1.226 历史制品，不在升级流程里，也不随升级部署。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层，与调用它的
``claude_upgrade_release``、``claude_upgrade_profile`` 同层：VC-3 的准备结果冻结了生成时的有效工具身份，本模块的任何变化
都会让未批准的准备草案失配。
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import re
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.official_client_control.canonical import canonical_sha256


# ---------------------------------------------------------------------------
# 合同常量
# ---------------------------------------------------------------------------

# 投影合同的固定身份（不随版本变化）：Release bundle 的 persona。
RELEASE_BUNDLE_PERSONA = "claude-code-oauth"

# Wire 与目录的合同名（Go 加载器按这些名字核对，不随版本变化）。
WIRE_ARTIFACT_SCHEMA = "claude-code-fw-g-wire-artifact/v3"
IMPLEMENTATION_POLICY_V2_SCHEMA = "claude-code-fw-g-implementation-policy/v2"
IMPLEMENTATION_POLICY_V3_SCHEMA = "claude-code-fw-g-implementation-policy/v3"
TOOL_POLICY_SCHEMA = "claude-code-fw-g-tool-policy/v1"
THINKING_POLICY_SCHEMA = "claude-code-thinking-display-policy/v1"
MODEL_CATALOG_SCHEMA = "claude-code-model-capability-catalog/v1"
RELEASE_IDENTITY_SCHEMA = "claude-code-fw-g-model-capability-release/v1"
BUNDLE_IDENTITY_SCHEMA = "claude-code-fw-g-model-capability-bundle/v1"
PROJECTION_POLICY_SCHEMA = "claude-code-wire-projection-policy/v1"
BEHAVIOR_TRAFFIC_DISPOSITION = "excluded-from-rules-and-difference-judgement"
TOOL_UNKNOWN_POLICY = "deny"
THINKING_TYPE = "adaptive"
THINKING_FIELD_ORDER = ("type", "display")

# FW-F v5 probe 目录的 probe ID（冻结目录事实，不随版本变化）；非主模型的 probe 加「<模型>-」前缀。
PROBE_BASELINE = "v4-replay-baseline"
PROBE_AGENT = "v4-agent-depth1"
PROBE_TUI = "v4-replay-tui"
PROBE_FALLBACK = "v4-replay-fallback-model"
PROBE_BACKGROUND = "v4-background"
PROBE_BACKGROUND_SUBAGENT = "v4-background-subagent"
PROBE_CUSTOM_SYSTEM = "v4-replay-custom-system"
PROBE_APPEND_SYSTEM = "v4-replay-append-system"
PROBE_EXCLUDE_DYNAMIC = "v4-replay-exclude-dynamic-system"
PROBE_CUSTOM_AGENT = "v4-replay-custom-agent"
PROBE_WEB_SEARCH = "v4-web-search"
PROBE_JSON_SCHEMA = "v4-replay-json-schema"
PROBE_BASH = "v4-bash"
PROBE_MCP_DEFERRED = "v4-mcp-deferred"
PROBE_ADVISOR = "v4-advisor-enabled-positive"
PROBE_COUNT_TOKENS = "v4-tui-count-tokens"
PROBE_THINKING_DISABLED = "v4-replay-thinking-disabled"
PROBE_TLS = "v4-native-tls-baseline"
PROBE_EFFORT_PREFIX = "v4-replay-effort-"
PROBE_THINKING_DISPLAY_PREFIX = "v4-thinking-display-"
# effort=high 是官方缺省 effort：证据取基线 probe，其余 effort 取 ``v4-replay-effort-<effort>``。
BASELINE_EFFORT = "high"

# 官方客户端的工具名与请求坐标（协议事实）。
TOOL_STRUCTURED_OUTPUT = "StructuredOutput"
TOOL_AGENT = "Agent"
TOOL_BASH = "Bash"
TOOL_WEB_SEARCH_OUTER = "WebSearch"
TOOL_WEB_SEARCH_SERVER = "web_search"
MESSAGES_TARGET = "/v1/messages?beta=true"
COUNT_TOKENS_TARGET_PREFIX = "/v1/messages/count_tokens"
TLS_SNI = "api.anthropic.com"
TLS_ALPN = "http/1.1"
TLS_MIN_VERSION = 771
TLS_MAX_VERSION = 772
# TLS probe 的连接设计：两条带 ALPN、两条不带 ALPN 的 ClientHello。
TLS_HELLOS_WITH_ALPN = 2
TLS_HELLOS_WITHOUT_ALPN = 2
FINGERPRINT_ALGORITHM = "sha256-salt-message-index-v1"
BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"
SYSTEM_REMINDER_PREFIX = "<system-reminder>"
HTTP_SPLIT = b"\r\n\r\n"
REQUEST_LINE_RE = re.compile(r"^([A-Z]+) ([^ ]+) (HTTP/\d\.\d)$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PROBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProjectionError(SystemExit):
    """投影失败：输入证据或策略不能形成目标制品。

    是 ``SystemExit`` 的子类：命令行照旧以消息退出；VC-3 在进程内调用投影函数时按本类捕获并转成门禁原因。
    """


# ---------------------------------------------------------------------------
# Wire 投影策略（claude-code-wire-projection-policy/v1）
# ---------------------------------------------------------------------------

PROJECTION_POLICY_FIELDS = frozenset(
    {"schema_version", "version", "models", "efforts", "attestation", "selectors", "tls", "implementation", "thinking_display_values"}
)
PROJECTION_MODEL_FIELDS = frozenset(
    {"ordered", "title_model", "fallback_model", "legacy_retry_fallback", "server_fallbacks", "alias_policy", "unknown_model_policy"}
)
SERVER_FALLBACK_FIELDS = frozenset({"model", "fallback_model", "probe"})
ATTESTATION_FIELDS = frozenset({"version_fingerprint", "cch"})
FINGERPRINT_FIELDS = frozenset({"algorithm", "salt", "message_indexes", "hex_length"})
CCH_FIELDS = frozenset({"algorithm", "hex_length", "lifecycle"})
SELECTOR_FIELDS = frozenset(
    {"system_block_count", "background_request_count", "mcp_deferred_tool_count", "background_tool_count", "advisor_tool_type"}
)
TLS_POLICY_FIELDS = (
    "cipher_suites",
    "supported_groups",
    "point_formats",
    "signature_algorithms",
    "supported_versions",
    "key_share_groups",
    "psk_modes",
    "with_alpn_extensions",
    "without_alpn_extensions",
)
IMPLEMENTATION_FIELDS = frozenset({"identity", "headers", "retry"})


def _policy_object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        actual = set(value) if isinstance(value, dict) else set()
        raise ProjectionError(f"Wire 投影策略 {label} 字段不闭合：缺失={sorted(fields - actual)}，多余={sorted(actual - fields)}")
    return value


def _policy_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProjectionError(f"Wire 投影策略 {label} 必须是非空文字（无首尾空白）")
    return value


def _policy_texts(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ProjectionError(f"Wire 投影策略 {label} 必须是{'' if allow_empty else '非空的'}文字数组")
    texts = [_policy_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(texts) != len(set(texts)):
        raise ProjectionError(f"Wire 投影策略 {label} 有重复项")
    return texts


def _policy_count(value: Any, label: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or (maximum is not None and value > maximum):
        raise ProjectionError(f"Wire 投影策略 {label} 必须是 {minimum} 起的整数")
    return value


def _policy_integers(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value)
        or len(value) != len(set(value))
    ):
        raise ProjectionError(f"Wire 投影策略 {label} 必须是不重复的非负整数数组")
    return list(value)


def validate_projection_policy(document: Any) -> dict[str, Any]:
    """校验 Wire 投影策略：字段闭集、类型与模型角色闭合；返回深拷贝（调用方改动不影响原文）。"""

    policy = _policy_object(document, PROJECTION_POLICY_FIELDS, "顶层")
    if policy["schema_version"] != PROJECTION_POLICY_SCHEMA:
        raise ProjectionError(f"Wire 投影策略 schema_version 必须是 {PROJECTION_POLICY_SCHEMA}")
    if not isinstance(policy["version"], str) or not VERSION_RE.fullmatch(policy["version"]):
        raise ProjectionError("Wire 投影策略 version 必须是三段式版本号")
    models = _policy_object(policy["models"], PROJECTION_MODEL_FIELDS, "models")
    ordered = _policy_texts(models["ordered"], "models.ordered")
    for key in ("title_model", "fallback_model", "alias_policy", "unknown_model_policy"):
        _policy_text(models[key], f"models.{key}")
    legacy = _policy_texts(models["legacy_retry_fallback"], "models.legacy_retry_fallback", allow_empty=True)
    if not set(legacy) <= set(ordered):
        raise ProjectionError("Wire 投影策略 models.legacy_retry_fallback 只能取模型闭集里的模型")
    fallbacks = models["server_fallbacks"]
    if not isinstance(fallbacks, list):
        raise ProjectionError("Wire 投影策略 models.server_fallbacks 必须是数组")
    seen: set[str] = set()
    for index, entry in enumerate(fallbacks):
        entry = _policy_object(entry, SERVER_FALLBACK_FIELDS, f"models.server_fallbacks[{index}]")
        model = _policy_text(entry["model"], f"models.server_fallbacks[{index}].model")
        _policy_text(entry["fallback_model"], f"models.server_fallbacks[{index}].fallback_model")
        if model not in ordered[1:] or model in seen:
            raise ProjectionError("Wire 投影策略 server fallback 只能登记非主模型，且每个模型至多一条")
        if not isinstance(entry["probe"], str) or not PROBE_ID_RE.fullmatch(entry["probe"]):
            raise ProjectionError(f"Wire 投影策略 models.server_fallbacks[{index}].probe 不是合法 probe ID")
        seen.add(model)
    efforts = _policy_texts(policy["efforts"], "efforts")
    if BASELINE_EFFORT not in efforts:
        raise ProjectionError(f"Wire 投影策略 efforts 必须含官方缺省 effort {BASELINE_EFFORT}")
    attestation = _policy_object(policy["attestation"], ATTESTATION_FIELDS, "attestation")
    fingerprint = _policy_object(attestation["version_fingerprint"], FINGERPRINT_FIELDS, "attestation.version_fingerprint")
    if fingerprint["algorithm"] != FINGERPRINT_ALGORITHM:
        raise ProjectionError(f"Wire 投影策略版本指纹算法必须是 {FINGERPRINT_ALGORITHM}")
    _policy_text(fingerprint["salt"], "attestation.version_fingerprint.salt")
    indexes = fingerprint["message_indexes"]
    if not isinstance(indexes, list) or not indexes or not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in indexes):
        raise ProjectionError("Wire 投影策略 attestation.version_fingerprint.message_indexes 必须是非空的非负整数数组")
    _policy_count(fingerprint["hex_length"], "attestation.version_fingerprint.hex_length", maximum=64)
    cch = _policy_object(attestation["cch"], CCH_FIELDS, "attestation.cch")
    _policy_text(cch["algorithm"], "attestation.cch.algorithm")
    _policy_count(cch["hex_length"], "attestation.cch.hex_length", maximum=64)
    _policy_text(cch["lifecycle"], "attestation.cch.lifecycle")
    selectors = _policy_object(policy["selectors"], SELECTOR_FIELDS, "selectors")
    _policy_count(selectors["system_block_count"], "selectors.system_block_count", minimum=2)
    for key in ("background_request_count", "mcp_deferred_tool_count", "background_tool_count"):
        _policy_count(selectors[key], f"selectors.{key}")
    _policy_text(selectors["advisor_tool_type"], "selectors.advisor_tool_type")
    tls = _policy_object(policy["tls"], frozenset(TLS_POLICY_FIELDS), "tls")
    for key in TLS_POLICY_FIELDS:
        _policy_integers(tls[key], f"tls.{key}")
    implementation = _policy_object(policy["implementation"], IMPLEMENTATION_FIELDS, "implementation")
    for key in IMPLEMENTATION_FIELDS:
        if not isinstance(implementation[key], dict) or not implementation[key]:
            raise ProjectionError(f"Wire 投影策略 implementation.{key} 必须是非空对象")
    _policy_texts(policy["thinking_display_values"], "thinking_display_values")
    return copy.deepcopy(policy)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectionError(f"Wire 投影策略包含重复字段：{key}")
        result[key] = value
    return result


def load_projection_policy(path: Path) -> dict[str, Any]:
    """读取并校验 Wire 投影策略文件（拒绝符号链接与重复字段）。"""

    if path.is_symlink() or not path.is_file():
        raise ProjectionError(f"Wire 投影策略不是可信普通文件：{path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProjectionError(f"无法读取 Wire 投影策略：{error}") from error
    return validate_projection_policy(document)


def base_model(policy: dict[str, Any]) -> str:
    """主模型：模型闭集的第一个（基础场景、公共基线与 Release 模型顺序的起点）。"""

    return str(policy["models"]["ordered"][0])


def model_probe(policy: dict[str, Any], model: str, probe: str) -> str:
    """某模型取证的 probe ID：主模型就是基础 probe，其余模型为 ``<模型>-<基础 probe>``。"""

    return probe if model == base_model(policy) else f"{model}-{probe}"


def effort_probe(effort: str) -> str:
    return PROBE_BASELINE if effort == BASELINE_EFFORT else f"{PROBE_EFFORT_PREFIX}{effort}"


# 主模型公共场景与非主模型逐场景投影各自读取的基础 probe（与下面的投影函数一一对应）。
PRIMARY_SCENARIO_PROBES = (
    PROBE_BASELINE,
    PROBE_AGENT,
    PROBE_TUI,
    PROBE_FALLBACK,
    PROBE_BACKGROUND,
    PROBE_CUSTOM_SYSTEM,
    PROBE_APPEND_SYSTEM,
    PROBE_EXCLUDE_DYNAMIC,
    PROBE_CUSTOM_AGENT,
    PROBE_WEB_SEARCH,
)
MODEL_SCENARIO_PROBES = (
    PROBE_BASELINE,
    PROBE_AGENT,
    PROBE_TUI,
    PROBE_BACKGROUND,
    PROBE_BACKGROUND_SUBAGENT,
    PROBE_WEB_SEARCH,
    PROBE_CUSTOM_SYSTEM,
    PROBE_APPEND_SYSTEM,
    PROBE_EXCLUDE_DYNAMIC,
    PROBE_CUSTOM_AGENT,
)
TOOL_POLICY_PROBES = (PROBE_JSON_SCHEMA, PROBE_AGENT, PROBE_BASH, PROBE_MCP_DEFERRED, PROBE_ADVISOR, PROBE_BACKGROUND, PROBE_WEB_SEARCH)


def catalog_probes(policy: dict[str, Any]) -> list[str]:
    """模型能力目录引用的全部 probe：主模型的公共场景与能力证据、每个非主模型的逐场景 probe、server fallback probe。"""

    capability = (PROBE_COUNT_TOKENS, PROBE_THINKING_DISABLED, *(effort_probe(effort) for effort in policy["efforts"]))
    probes = set(PRIMARY_SCENARIO_PROBES) | set(capability)
    for model in policy["models"]["ordered"][1:]:
        probes.update(model_probe(policy, model, probe) for probe in (*MODEL_SCENARIO_PROBES, *capability))
    for entry in policy["models"]["server_fallbacks"]:
        probes.add(model_probe(policy, entry["model"], entry["probe"]))
    return sorted(probes)


def required_probes(policy: dict[str, Any]) -> list[str]:
    """投影 Wire 需要的全部 probe：模型能力目录、工具策略、thinking.display 各取值与 P 通道。"""

    probes = set(catalog_probes(policy)) | set(TOOL_POLICY_PROBES) | {PROBE_TLS}
    probes.update(f"{PROBE_THINKING_DISPLAY_PREFIX}{display}" for display in policy["thinking_display_values"])
    return sorted(probes)


def canonical_bytes(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# HTTP/1.1 请求流（与历史解析器逐字同义；不导入 claude_fw_f_profile）
# ---------------------------------------------------------------------------


def parse_http_request(content: bytes, label: str) -> dict[str, Any]:
    """从已脱敏的原始 R 字节解析一条 HTTP/1.1 请求（Authorization 必须已脱敏为 ``<secret>``）。"""

    if HTTP_SPLIT not in content:
        raise ProjectionError(f"{label} 缺少 HTTP Header/Body 分隔")
    raw_head, raw_body = content.split(HTTP_SPLIT, 1)
    try:
        lines = raw_head.decode("latin-1").split("\r\n")
    except UnicodeDecodeError as exc:
        raise ProjectionError(f"{label} Header 无法解析") from exc
    match = REQUEST_LINE_RE.fullmatch(lines[0])
    if match is None:
        raise ProjectionError(f"{label} 请求行不合法：{lines[0]}")
    headers: list[dict[str, str]] = []
    for index, line in enumerate(lines[1:]):
        if ":" not in line:
            raise ProjectionError(f"{label} Header[{index}] 缺少冒号")
        name, value = line.split(":", 1)
        headers.append({"name": name, "value": value.lstrip(" ")})
    lowered = {item["name"].lower(): item["value"] for item in headers}
    content_encoding = lowered.get("content-encoding")
    decoded_body = raw_body
    if content_encoding is not None:
        if content_encoding != "gzip":
            raise ProjectionError(f"{label} 使用未支持的 Content-Encoding：{content_encoding}")
        try:
            decoded_body = gzip.decompress(raw_body)
        except (OSError, EOFError) as exc:
            raise ProjectionError(f"{label} gzip Body 无法解压：{exc}") from exc
    try:
        body = json.loads(decoded_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"{label} Body 不是 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise ProjectionError(f"{label} Body 顶层必须是对象")
    if "<secret>" not in lowered.get("authorization", ""):
        raise ProjectionError(f"{label} Authorization 未脱敏")
    return {
        "method": match.group(1),
        "request_target": match.group(2),
        "http_version": match.group(3),
        "headers": headers,
        "body": body,
        "content_encoding": content_encoding,
        "wire_body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "raw_sha256": hashlib.sha256(content).hexdigest(),
        "body_sha256": canonical_sha256(body),
    }


def parse_http_stream(content: bytes, label: str) -> list[dict[str, Any]]:
    """按 Content-Length 拆分连接复用产生的连续 HTTP/1.1 请求；每条带流内偏移与长度。"""

    requests: list[dict[str, Any]] = []
    offset = 0
    while offset < len(content):
        head_end = content.find(HTTP_SPLIT, offset)
        if head_end < 0:
            raise ProjectionError(f"{label} offset={offset} 缺少 Header/Body 分隔")
        raw_head = content[offset:head_end]
        try:
            lines = raw_head.decode("latin-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ProjectionError(f"{label} offset={offset} Header 无法解析") from exc
        match = REQUEST_LINE_RE.fullmatch(lines[0])
        if match is None:
            raise ProjectionError(f"{label} offset={offset} 请求行不合法：{lines[0]}")
        content_length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError as exc:
                    raise ProjectionError(f"{label} Content-Length 非整数") from exc
        body_start = head_end + len(HTTP_SPLIT)
        end = body_start + content_length
        if end > len(content):
            raise ProjectionError(f"{label} Content-Length 越过流结尾")
        chunk = content[offset:end]
        if content_length == 0:
            request = {
                "method": match.group(1),
                "request_target": match.group(2),
                "http_version": match.group(3),
                "headers": [
                    {"name": line.split(":", 1)[0], "value": line.split(":", 1)[1].lstrip(" ")}
                    for line in lines[1:]
                ],
                "body": {},
                "raw_sha256": hashlib.sha256(chunk).hexdigest(),
                "body_sha256": canonical_sha256({}),
            }
        else:
            request = parse_http_request(chunk, f"{label}@{offset}")
        request["stream_offset"] = offset
        request["stream_length"] = len(chunk)
        requests.append(request)
        offset = end
    return requests


def wire_json(value: Any) -> str:
    """保留官方请求中对象字段的插入顺序，供 Go 编译器逐字节重放。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_u16(raw: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(raw):
        raise ValueError("ClientHello 的 uint16 越界")
    return struct.unpack_from(">H", raw, offset)[0], offset + 2


def read_vector_u16(raw: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = read_u16(raw, offset)
    end = offset + length
    if end > len(raw):
        raise ValueError("ClientHello 的 uint16 vector 越界")
    return raw[offset:end], end


def parse_u16_list(raw: bytes) -> list[int]:
    if len(raw) % 2 != 0:
        raise ValueError("ClientHello 的 uint16 列表长度非法")
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


def parse_client_hello(record: bytes) -> dict[str, Any]:
    if len(record) < 9 or record[0] != 22 or record[5] != 1:
        raise ValueError("不是 TLS ClientHello record")
    record_length = struct.unpack_from(">H", record, 3)[0]
    if len(record) != record_length + 5:
        raise ValueError("TLS record 长度不一致")
    handshake_length = int.from_bytes(record[6:9], "big")
    if handshake_length + 4 != record_length:
        raise ValueError("ClientHello handshake 长度不一致")
    body = record[9:]
    if len(body) < 35:
        raise ValueError("ClientHello body 过短")
    offset = 34
    session_length = body[offset]
    offset += 1 + session_length
    cipher_raw, offset = read_vector_u16(body, offset)
    if offset >= len(body):
        raise ValueError("ClientHello 缺少 compression methods")
    compression_length = body[offset]
    offset += 1 + compression_length
    extensions_raw, offset = read_vector_u16(body, offset)
    if offset != len(body):
        raise ValueError("ClientHello extensions 后有多余字节")

    extensions: list[int] = []
    extension_values: dict[int, bytes] = {}
    ext_offset = 0
    while ext_offset < len(extensions_raw):
        ext_id, ext_offset = read_u16(extensions_raw, ext_offset)
        ext_value, ext_offset = read_vector_u16(extensions_raw, ext_offset)
        extensions.append(ext_id)
        extension_values[ext_id] = ext_value

    sni = ""
    if 0 in extension_values:
        names, _ = read_vector_u16(extension_values[0], 0)
        if len(names) < 3 or names[0] != 0:
            raise ValueError("ClientHello SNI 结构非法")
        name_length = struct.unpack_from(">H", names, 1)[0]
        sni = names[3 : 3 + name_length].decode("ascii")

    groups: list[int] = []
    if 10 in extension_values:
        group_raw, _ = read_vector_u16(extension_values[10], 0)
        groups = parse_u16_list(group_raw)

    point_formats: list[int] = []
    if 11 in extension_values:
        value = extension_values[11]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello point formats 结构非法")
        point_formats = list(value[1:])

    signature_algorithms: list[int] = []
    if 13 in extension_values:
        signature_raw, _ = read_vector_u16(extension_values[13], 0)
        signature_algorithms = parse_u16_list(signature_raw)

    alpn: list[str] = []
    if 16 in extension_values:
        names, _ = read_vector_u16(extension_values[16], 0)
        name_offset = 0
        while name_offset < len(names):
            name_length = names[name_offset]
            name_offset += 1
            alpn.append(names[name_offset : name_offset + name_length].decode("ascii"))
            name_offset += name_length

    supported_versions: list[int] = []
    if 43 in extension_values:
        value = extension_values[43]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello supported versions 结构非法")
        supported_versions = parse_u16_list(value[1:])

    key_share_groups: list[int] = []
    if 51 in extension_values:
        shares, _ = read_vector_u16(extension_values[51], 0)
        share_offset = 0
        while share_offset < len(shares):
            group, share_offset = read_u16(shares, share_offset)
            key, share_offset = read_vector_u16(shares, share_offset)
            if not key:
                raise ValueError("ClientHello key share 为空")
            key_share_groups.append(group)

    psk_modes: list[int] = []
    if 45 in extension_values:
        value = extension_values[45]
        if not value or value[0] + 1 != len(value):
            raise ValueError("ClientHello PSK modes 结构非法")
        psk_modes = list(value[1:])

    return {
        "cipher_suites": parse_u16_list(cipher_raw),
        "supported_groups": groups,
        "point_formats": point_formats,
        "signature_algorithms": signature_algorithms,
        "alpn": alpn,
        "extensions": extensions,
        "supported_versions": supported_versions,
        "key_share_groups": key_share_groups,
        "psk_modes": psk_modes,
        "sni": sni,
        "tls_min_version": 771,
        "tls_max_version": 772,
    }


def extract_client_hellos(pcap: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    while offset + 9 <= len(pcap):
        hit = pcap.find(b"\x16\x03\x01", offset)
        if hit < 0:
            break
        record_length = struct.unpack_from(">H", pcap, hit + 3)[0]
        end = hit + 5 + record_length
        if end <= len(pcap):
            try:
                parsed = parse_client_hello(pcap[hit:end])
            except (UnicodeDecodeError, ValueError):
                pass
            else:
                if parsed["sni"] == "api.anthropic.com":
                    records.append(parsed)
                    offset = end
                    continue
        offset = hit + 1
    return records


def validate_tls_vector(vector: dict[str, Any], with_alpn: bool, tls: dict[str, Any], version: str) -> None:
    """ClientHello 向量与投影策略登记的 TLS 期望逐字相同（含 ALPN 条件下的扩展顺序）。"""

    expected = {
        "cipher_suites": tls["cipher_suites"],
        "supported_groups": tls["supported_groups"],
        "point_formats": tls["point_formats"],
        "signature_algorithms": tls["signature_algorithms"],
        "supported_versions": tls["supported_versions"],
        "key_share_groups": tls["key_share_groups"],
        "psk_modes": tls["psk_modes"],
        "sni": TLS_SNI,
        "tls_min_version": TLS_MIN_VERSION,
        "tls_max_version": TLS_MAX_VERSION,
    }
    for name, value in expected.items():
        if vector.get(name) != value:
            raise ProjectionError(f"ClientHello {name} 与 {version} P 通道不一致")
    expected_alpn = [TLS_ALPN] if with_alpn else []
    expected_extensions = tls["with_alpn_extensions"] if with_alpn else tls["without_alpn_extensions"]
    if vector["alpn"] != expected_alpn or vector["extensions"] != expected_extensions:
        raise ProjectionError(f"ClientHello ALPN／扩展序与 {version} P 通道不一致")


def tls_transports(tls_pcap: bytes, tls: dict[str, Any], version: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """从 P 通道抽出 ClientHello：必须形成 2+2 的 ALPN 条件对照，且每条与策略 TLS 期望一致；返回 (带 ALPN, 不带 ALPN)。"""

    hellos = extract_client_hellos(tls_pcap)
    expected_count = TLS_HELLOS_WITH_ALPN + TLS_HELLOS_WITHOUT_ALPN
    if len(hellos) != expected_count:
        raise ProjectionError(f"TLS P 通道 ClientHello 数量不是 {expected_count}：{len(hellos)}")
    with_alpn = [hello for hello in hellos if hello["alpn"]]
    without_alpn = [hello for hello in hellos if not hello["alpn"]]
    if len(with_alpn) != TLS_HELLOS_WITH_ALPN or len(without_alpn) != TLS_HELLOS_WITHOUT_ALPN:
        raise ProjectionError("TLS P 通道没有形成 2+2 ALPN 条件对照")
    for vector in with_alpn:
        validate_tls_vector(vector, True, tls, version)
    for vector in without_alpn:
        validate_tls_vector(vector, False, tls, version)
    return with_alpn[0], without_alpn[0]


# ---------------------------------------------------------------------------
# 版本指纹（attestation）：按策略的 salt 与 message index 复算观测请求里的 cc_version
# ---------------------------------------------------------------------------


def _real_user_text(value: str) -> bool:
    trimmed = value.strip()
    return bool(trimmed) and not trimmed.startswith(SYSTEM_REMINDER_PREFIX)


def first_real_user_text(body: dict[str, Any]) -> str | None:
    """版本指纹依据的「首条真实用户文本」：第一条 user 消息的字符串内容或其第一个非空 text 块（跳过 system-reminder）。"""

    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for item in messages:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            if _real_user_text(content):
                return content
            continue
        if not isinstance(content, list):
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and _real_user_text(block["text"]):
                return block["text"]
    return None


def version_fingerprint(policy: dict[str, Any], version: str, text: str) -> str:
    """``sha256(salt + 用户文本在各 message index 上的字符（越界记 0） + 版本)`` 的前 hex_length 位。"""

    fingerprint = policy["attestation"]["version_fingerprint"]
    selected = "".join(text[index] if 0 <= index < len(text) else "0" for index in fingerprint["message_indexes"])
    digest = hashlib.sha256((fingerprint["salt"] + selected + version).encode("utf-8")).hexdigest()
    return digest[: fingerprint["hex_length"]]


def billing_attribution(body: dict[str, Any]) -> dict[str, str] | None:
    """system[0] 计费头 ``x-anthropic-billing-header: cc_version=…; cc_entrypoint=…; cch=…;`` 的键值；没有计费头为 None。"""

    system = body.get("system")
    if not isinstance(system, list) or not system or not isinstance(system[0], dict):
        return None
    text = system[0].get("text")
    if not isinstance(text, str) or not text.startswith(BILLING_HEADER_PREFIX):
        return None
    values: dict[str, str] = {}
    for part in text[len(BILLING_HEADER_PREFIX) :].split(";"):
        part = part.strip()
        if not part:
            continue
        key, separator, value = part.partition("=")
        if not separator or key.strip() in values:
            raise ProjectionError(f"计费头字段非法或重复：{part}")
        values[key.strip()] = value.strip()
    return values


def verify_version_fingerprints(requests: list[dict[str, Any]], policy: dict[str, Any], version: str) -> int:
    """逐条核对推理请求计费头里的版本指纹：按策略复算，任何一条不符即拒绝；返回核对条数（至少一条）。"""

    verified = 0
    for request in requests:
        body = request.get("body")
        if not isinstance(body, dict):
            continue
        attribution = billing_attribution(body)
        if attribution is None or "cc_version" not in attribution:
            continue
        text = first_real_user_text(body)
        if text is None:
            continue
        expected = f"{version}.{version_fingerprint(policy, version, text)}"
        if attribution["cc_version"] != expected:
            raise ProjectionError(
                f"版本指纹与 Wire 投影策略不符：{request.get('evidence_path')} 观测 {attribution['cc_version']}，按策略复算 {expected}"
            )
        verified += 1
    if verified == 0:
        raise ProjectionError("没有可核对版本指纹的推理请求（计费头或首条用户文本缺失）")
    return verified


def header_value(request: dict[str, Any], name: str) -> str:
    target = name.lower()
    for header in request.get("headers", []):
        if str(header.get("name", "")).lower() == target:
            return str(header.get("value", ""))
    return ""


def public_system_blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = request.get("body", {}).get("system", [])
    if not isinstance(blocks, list) or len(blocks) < 1:
        raise ProjectionError("场景请求缺少 Persona system blocks")
    return blocks[1:]


def scenario_projection(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body", {})
    header_order = [str(header.get("name", "")) for header in request.get("headers", [])]
    return {
        "evidence": {
            "path": request["evidence_path"],
            "raw_sha256": request["raw_sha256"],
        },
        "model": body.get("model"),
        # 制品本身按 canonical JSON 内容寻址，会递归重排对象键。以下字段必须
        # 另存原 wire JSON 字符串，否则 context_management、tools 等对象的键序
        # 会在生成制品时被破坏，FW-G 无法做逐字节 PAIR。
        "max_tokens_json": wire_json(body.get("max_tokens")),
        "thinking_json": wire_json(body.get("thinking")),
        "context_management_json": wire_json(body.get("context_management")),
        "fallbacks_json": wire_json(body.get("fallbacks")),
        "output_config_json": wire_json(body.get("output_config")),
        "temperature_json": wire_json(body.get("temperature")),
        "thinking_present": "thinking" in body,
        "context_management_present": "context_management" in body,
        "fallbacks_present": "fallbacks" in body,
        "output_config_present": "output_config" in body,
        "temperature_present": "temperature" in body,
        "stream_present": "stream" in body,
        "stream_json": wire_json(body.get("stream")),
        "tools_present": "tools" in body,
        "tools_json": wire_json(body.get("tools")),
        "system_present": "system" in body,
        "system_blocks_json": wire_json(public_system_blocks(request)),
        "metadata_present": "metadata" in body,
        "body_order": list(body),
        "header_order": header_order,
        "fallback_latched_by_present": bool(
            header_value(request, "x-cc-fallback-latched-by")
        ),
        "refusal_fallback_value": header_value(request, "x-is-refusal-fallback"),
        "user_agent": header_value(request, "User-Agent"),
        "anthropic_beta": header_value(request, "anthropic-beta"),
        "x_app": header_value(request, "x-app"),
    }


def tool_policy_projection(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body", {})
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise ProjectionError("工具策略证据的 tools 不是数组")
    return {
        "evidence": {
            "path": request["evidence_path"],
            "raw_sha256": request["raw_sha256"],
        },
        "tools_json": wire_json(tools),
        "tool_choice_json": wire_json(body.get("tool_choice")),
        "anthropic_beta": header_value(request, "anthropic-beta"),
    }


def select_request(requests: list[dict[str, Any]], predicate: Any, description: str, label: str) -> dict[str, Any]:
    """按谓词选第一条请求；没有即投影失败。"""

    selected = next((request for request in requests if predicate(request)), None)
    if selected is None:
        raise ProjectionError(f"{label} 没有 {description} 请求")
    return selected


def _body(request: dict[str, Any]) -> dict[str, Any]:
    body = request.get("body")
    return body if isinstance(body, dict) else {}


def _tools(request: dict[str, Any]) -> list[Any]:
    tools = _body(request).get("tools", [])
    return tools if isinstance(tools, list) else []


def _first_tool_name(request: dict[str, Any]) -> Any:
    tools = _body(request).get("tools", [{}])
    if not isinstance(tools, list) or not tools or not isinstance(tools[0], dict):
        return None
    return tools[0].get("name")


def _has_tool(name: str) -> Callable[[dict[str, Any]], bool]:
    return lambda request: any(isinstance(tool, dict) and tool.get("name") == name for tool in _tools(request))


def _effort(request: dict[str, Any]) -> Any:
    config = _body(request).get("output_config", {})
    return config.get("effort") if isinstance(config, dict) else None


def _evidence(request: dict[str, Any]) -> dict[str, str]:
    return {"path": request["evidence_path"], "raw_sha256": request["raw_sha256"]}


# ---------------------------------------------------------------------------
# 投影：实现策略、工具策略、thinking.display、模型能力目录、Wire 与 Release 身份
# ---------------------------------------------------------------------------


def build_thinking_display_policy(source: Any, policy: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """thinking.display：缺省证据取基线请求，各取值取对应 probe 的唯一推理请求；顶层键序必须与基线相同。"""

    expected_body_keys = list(_body(baseline))
    if _body(baseline).get("thinking") != {"type": THINKING_TYPE}:
        raise ProjectionError("thinking.display 缺省证据不是 adaptive 单键对象")
    evidence: dict[str, dict[str, Any]] = {"default": _evidence(baseline)}
    for display in policy["thinking_display_values"]:
        request = source.thinking_display_request(display)
        thinking = _body(request).get("thinking")
        if (
            not isinstance(thinking, dict)
            or list(thinking) != list(THINKING_FIELD_ORDER)
            or thinking != {"type": THINKING_TYPE, "display": display}
            or list(_body(request)) != expected_body_keys
        ):
            raise ProjectionError(f"{PROBE_THINKING_DISPLAY_PREFIX}{display} thinking wire 或顶层顺序不一致")
        evidence[display] = _evidence(request)
    evidence["production_change_receipt"] = source.thinking_production_receipt()
    return {
        "schema_version": THINKING_POLICY_SCHEMA,
        "type": THINKING_TYPE,
        "field_order": list(THINKING_FIELD_ORDER),
        "display_values": list(policy["thinking_display_values"]),
        "evidence": evidence,
    }


def build_tool_policy(source: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """工具目录：StructuredOutput、Agent、Bash、deferred MCP、advisor、background 与 WebSearch 两种形态。"""

    selectors = policy["selectors"]
    structured = source.select_messages(PROBE_JSON_SCHEMA, _has_tool(TOOL_STRUCTURED_OUTPUT), TOOL_STRUCTURED_OUTPUT)
    agent = source.select_messages(PROBE_AGENT, _has_tool(TOOL_AGENT), TOOL_AGENT)
    bash = source.select_messages(PROBE_BASH, _has_tool(TOOL_BASH), TOOL_BASH)
    mcp = source.select_messages(
        PROBE_MCP_DEFERRED,
        lambda request: len(_tools(request)) == selectors["mcp_deferred_tool_count"],
        f"{selectors['mcp_deferred_tool_count']} 项 deferred MCP 目录",
    )
    advisor = source.select_messages(
        PROBE_ADVISOR,
        lambda request: any(isinstance(tool, dict) and tool.get("type") == selectors["advisor_tool_type"] for tool in _tools(request)),
        "advisor",
    )
    background = source.select_messages(
        PROBE_BACKGROUND,
        lambda request: len(_tools(request)) == selectors["background_tool_count"],
        f"{selectors['background_tool_count']} 项 background 目录",
    )
    web_search_requests = source.messages(PROBE_WEB_SEARCH)
    web_search_outer = next(
        (
            request
            for request in web_search_requests
            if _first_tool_name(request) == TOOL_WEB_SEARCH_OUTER and "tool_choice" not in _body(request)
        ),
        None,
    )
    web_search_server = next(
        (
            request
            for request in web_search_requests
            if _first_tool_name(request) == TOOL_WEB_SEARCH_SERVER and "tool_choice" in _body(request)
        ),
        None,
    )
    if web_search_outer is None or web_search_server is None:
        raise ProjectionError(f"{PROBE_WEB_SEARCH} 没有形成外层与 server 派生请求")
    return {
        "schema_version": TOOL_POLICY_SCHEMA,
        "unknown_tool_policy": TOOL_UNKNOWN_POLICY,
        "structured_output": tool_policy_projection(structured),
        "agent": tool_policy_projection(agent),
        "bash": tool_policy_projection(bash),
        "mcp_deferred": tool_policy_projection(mcp),
        "advisor": tool_policy_projection(advisor),
        "background": tool_policy_projection(background),
        "web_search_outer": tool_policy_projection(web_search_outer),
        "web_search_server": tool_policy_projection(web_search_server),
    }


def build_implementation_policy(source: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """主模型的公共场景投影与实现策略（identity／headers／retry 三段取自投影策略）。"""

    models = policy["models"]
    primary = base_model(policy)
    baseline = source.messages(PROBE_BASELINE)[0]
    agent = select_request(
        source.messages(PROBE_AGENT), lambda request: bool(header_value(request, "x-claude-code-agent-id")), "带 agent-id 的", PROBE_AGENT
    )
    tui_requests = source.messages(PROBE_TUI)
    tui_main = next((request for request in tui_requests if _body(request).get("model") == primary), None)
    tui_title = next((request for request in tui_requests if _body(request).get("model") == models["title_model"]), None)
    if tui_main is None or tui_title is None:
        raise ProjectionError("TUI 场景没有形成标题请求与主请求条件对照")
    fallback = next(
        (request for request in source.messages(PROBE_FALLBACK) if _body(request).get("model") == models["fallback_model"]),
        None,
    )
    if fallback is None:
        raise ProjectionError(f"fallback 场景没有 {models['fallback_model']} 回退请求")
    background_requests = source.messages(PROBE_BACKGROUND)
    custom_system = source.messages(PROBE_CUSTOM_SYSTEM)[0]
    append_system = source.messages(PROBE_APPEND_SYSTEM)[0]
    exclude_dynamic = source.messages(PROBE_EXCLUDE_DYNAMIC)[0]
    custom_agent = source.messages(PROBE_CUSTOM_AGENT)[0]
    web_search_server = source.select_messages(PROBE_WEB_SEARCH, lambda request: "tool_choice" in _body(request), "server web_search")
    web_search_outer = source.select_messages(
        PROBE_WEB_SEARCH,
        lambda request: _first_tool_name(request) == TOOL_WEB_SEARCH_OUTER and "tool_choice" not in _body(request),
        "outer WebSearch",
    )
    background = [scenario_projection(request) for request in background_requests]
    baseline_projection = scenario_projection(baseline)
    if baseline_projection["model"] != primary:
        raise ProjectionError(f"基线模型不是主模型 {primary}")
    implementation = policy["implementation"]
    return {
        "schema_version": IMPLEMENTATION_POLICY_V2_SCHEMA,
        "evidence_ledger": source.evidence_ledger(),
        "scenarios": {
            "sdk_cli": baseline_projection,
            "agent": scenario_projection(agent),
            "tui_main": scenario_projection(tui_main),
            "tui_title": scenario_projection(tui_title),
            "fallback": scenario_projection(fallback),
            "background": background,
            "custom_system": scenario_projection(custom_system),
            "append_system": scenario_projection(append_system),
            "exclude_dynamic": scenario_projection(exclude_dynamic),
            "custom_agent": scenario_projection(custom_agent),
            "web_search_outer": scenario_projection(web_search_outer),
            "web_search_server": scenario_projection(web_search_server),
        },
        "identity": copy.deepcopy(implementation["identity"]),
        "headers": copy.deepcopy(implementation["headers"]),
        "thinking": build_thinking_display_policy(source, policy, baseline),
        "retry": copy.deepcopy(implementation["retry"]),
        "tool_policy": build_tool_policy(source, policy),
    }


def build_model_scenarios(model: str, source: Any, policy: dict[str, Any], base_scenarios: dict[str, Any]) -> dict[str, Any]:
    """非主模型的逐场景投影：在主模型公共场景之上替换该模型自己的请求；登记了 server fallback 的模型补锁存场景。"""

    def probe(name: str) -> str:
        return model_probe(policy, model, name)

    def primary(name: str) -> dict[str, Any]:
        return source.model_select(
            probe(name),
            lambda request: request.get("request_target") == MESSAGES_TARGET and _body(request).get("model") == model,
            model + " messages",
        )

    agent = source.model_select(
        probe(PROBE_AGENT),
        lambda request: _body(request).get("model") == model and bool(header_value(request, "x-claude-code-agent-id")),
        "agent 子请求",
    )
    tui_main = source.model_select(probe(PROBE_TUI), lambda request: _body(request).get("model") == model, "TUI 主请求")
    background_requests = [request for request in source.model_requests(probe(PROBE_BACKGROUND)) if request.get("request_target") == MESSAGES_TARGET]
    if len(background_requests) != policy["selectors"]["background_request_count"]:
        raise ProjectionError(f"{model} background 请求数量不是 {policy['selectors']['background_request_count']}")
    background_agent_requests = [
        request
        for request in source.model_requests(probe(PROBE_BACKGROUND_SUBAGENT))
        if request.get("request_target") == MESSAGES_TARGET and _body(request).get("model") == model
    ]
    sdk_cli_background_agent = next(
        (request for request in background_agent_requests if not header_value(request, "x-claude-code-agent-id")), None
    )
    agent_background = next((request for request in background_agent_requests if header_value(request, "x-claude-code-agent-id")), None)
    if sdk_cli_background_agent is None or agent_background is None:
        raise ProjectionError(f"{model} background subagent 形态不完整")
    web_search_requests = source.model_requests(probe(PROBE_WEB_SEARCH))
    web_search_outer = next(
        (
            request
            for request in web_search_requests
            if _body(request).get("model") == model
            and _first_tool_name(request) == TOOL_WEB_SEARCH_OUTER
            and "tool_choice" not in _body(request)
        ),
        None,
    )
    web_search_server = next(
        (
            request
            for request in web_search_requests
            if _body(request).get("model") == model
            and _first_tool_name(request) == TOOL_WEB_SEARCH_SERVER
            and "tool_choice" in _body(request)
        ),
        None,
    )
    if web_search_outer is None or web_search_server is None:
        raise ProjectionError(f"{model} WebSearch 外层／server 形态不完整")

    scenarios = copy.deepcopy(base_scenarios)
    scenarios.update(
        {
            "sdk_cli": scenario_projection(primary(PROBE_BASELINE)),
            "agent": scenario_projection(agent),
            "tui_main": scenario_projection(tui_main),
            "background": [scenario_projection(request) for request in background_requests],
            "custom_system": scenario_projection(primary(PROBE_CUSTOM_SYSTEM)),
            "append_system": scenario_projection(primary(PROBE_APPEND_SYSTEM)),
            "exclude_dynamic": scenario_projection(primary(PROBE_EXCLUDE_DYNAMIC)),
            "custom_agent": scenario_projection(primary(PROBE_CUSTOM_AGENT)),
            "sdk_cli_background_agent": scenario_projection(sdk_cli_background_agent),
            "agent_background": scenario_projection(agent_background),
            "web_search_outer": scenario_projection(web_search_outer),
            "web_search_server": scenario_projection(web_search_server),
            "server_fallback": None,
        }
    )
    for entry in policy["models"]["server_fallbacks"]:
        if entry["model"] != model:
            continue
        fallback = source.model_select(
            probe(entry["probe"]),
            lambda request, expected=entry["fallback_model"]: _body(request).get("model") == expected
            and header_value(request, "x-is-refusal-fallback") == "true"
            and bool(header_value(request, "x-cc-fallback-latched-by")),
            "server fallback 锁存请求",
        )
        scenarios["server_fallback"] = scenario_projection(fallback)
    return scenarios


def build_model_capability_catalog(
    source: Any,
    policy: dict[str, Any],
    base_scenarios: dict[str, Any],
    required_rule_count: int,
) -> dict[str, Any]:
    """模型能力目录：模型闭集按投影策略顺序，主模型复用公共场景，其余模型逐场景投影自身证据。"""

    evidence = source.catalog_evidence()
    primary = base_model(policy)
    primary_scenarios = copy.deepcopy(base_scenarios)
    primary_scenarios["server_fallback"] = None
    efforts = list(policy["efforts"])
    models: list[dict[str, Any]] = []
    for model in policy["models"]["ordered"]:
        if model == primary:
            scenarios = primary_scenarios
            count_tokens = source.base_select(
                PROBE_COUNT_TOKENS,
                lambda request, expected=model: str(request.get("request_target", "")).startswith(COUNT_TOKENS_TARGET_PREFIX)
                and _body(request).get("model") == expected,
                f"{model} count_tokens",
            )
            effort_evidence = {
                effort: scenario_projection(
                    source.select_messages(
                        effort_probe(effort),
                        lambda request, expected=effort: _effort(request) == expected,
                        f"{model} effort={effort}",
                    )
                )["evidence"]
                for effort in efforts
            }
            thinking_disabled = source.select_messages(
                PROBE_THINKING_DISABLED, lambda request: "thinking" not in _body(request), f"{model} thinking disabled"
            )
        else:
            scenarios = build_model_scenarios(model, source, policy, base_scenarios)
            count_tokens = source.model_select(
                model_probe(policy, model, PROBE_COUNT_TOKENS),
                lambda request, expected=model: str(request.get("request_target", "")).startswith(COUNT_TOKENS_TARGET_PREFIX)
                and _body(request).get("model") == expected,
                model + " count_tokens",
            )
            effort_evidence = {}
            for effort in efforts:
                request = source.model_select(
                    model_probe(policy, model, effort_probe(effort)),
                    lambda request, expected=effort: _effort(request) == expected,
                    f"{model} effort={effort}",
                )
                effort_evidence[effort] = _evidence(request)
            thinking_disabled = source.model_select(
                model_probe(policy, model, PROBE_THINKING_DISABLED),
                lambda request, expected=model: _body(request).get("model") == expected and "thinking" not in _body(request),
                model + " thinking disabled",
            )
        models.append(
            {
                "canonical_model": model,
                "aliases": [model],
                "effort_values": list(efforts),
                "effort_evidence": effort_evidence,
                "thinking_disable_evidence": _evidence(thinking_disabled),
                "count_tokens_evidence": _evidence(count_tokens),
                "legacy_retry_fallback_supported": model in policy["models"]["legacy_retry_fallback"],
                "scenarios": scenarios,
            }
        )
    return {
        "schema_version": MODEL_CATALOG_SCHEMA,
        "required_rule_count": required_rule_count,
        "unknown_model_policy": policy["models"]["unknown_model_policy"],
        "alias_policy": policy["models"]["alias_policy"],
        "evidence": evidence,
        "models": models,
    }


def build_wire_artifact(
    policy: dict[str, Any],
    implementation_policy: dict[str, Any],
    model_catalog: dict[str, Any],
    messages_raw: bytes,
    messages_path: str,
    tls_pcap: bytes,
    tls_path: str,
    profile_digest: str,
    platform: str,
    *,
    frozen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wire：基线推理请求的静态 system 块与缺省 beta、P 通道 TLS 向量、attestation、实现策略与模型能力目录。

    ``frozen`` 只给历史 CLI：v2 generation policy 冻结的 messages／Body／system 文本／pcap 摘要，逐项核对。
    """

    version = str(policy["version"])
    if frozen is not None and sha256_hex(messages_raw) != frozen["messages_sha256"]:
        raise ProjectionError("messages R 通道摘要不一致")
    try:
        raw_head, raw_body = messages_raw.split(HTTP_SPLIT, 1)
    except ValueError as exc:
        raise ProjectionError("messages R 通道不是完整 HTTP/1.1 请求") from exc
    if frozen is not None and sha256_hex(raw_body) != frozen["messages_body_sha256"]:
        raise ProjectionError("messages Body 摘要不一致")
    content_length_match = re.search(br"(?im)^Content-Length: (\d+)$", raw_head)
    if content_length_match is None or int(content_length_match.group(1)) != len(raw_body):
        raise ProjectionError("messages R 通道 Content-Length 不一致")
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"messages Body 不是 JSON：{exc}") from exc
    system = body.get("system") if isinstance(body, dict) else None
    block_count = policy["selectors"]["system_block_count"]
    if not isinstance(system, list) or len(system) != block_count:
        raise ProjectionError(f"messages 基线不是 {block_count} 段 system")
    static_blocks = system[1:]
    system_digests: list[str] = []
    for index, block in enumerate(static_blocks, start=1):
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            raise ProjectionError(f"messages system[{index}] 缺少文本")
        system_digests.append(sha256_hex(text.encode("utf-8")))
    if frozen is not None and system_digests != list(frozen["system_text_sha256"]):
        raise ProjectionError("messages system 静态块文本摘要不一致")
    if frozen is not None and sha256_hex(tls_pcap) != frozen["tls_pcap_sha256"]:
        raise ProjectionError("TLS P 通道摘要不一致")
    with_alpn, without_alpn = tls_transports(tls_pcap, policy["tls"], version)
    head_text = raw_head.decode("latin1")
    if "anthropic-beta: " not in head_text:
        raise ProjectionError("messages 基线缺少 anthropic-beta")
    return {
        "schema_version": WIRE_ARTIFACT_SCHEMA,
        "identity": {
            "version": version,
            "platform": platform,
            "profile_digest": profile_digest,
            "model_capability_catalog_digest": sha256_hex(canonical_bytes(model_catalog)),
        },
        "evidence": {
            "messages_request": {"path": messages_path, "sha256": sha256_hex(messages_raw), "bytes": len(messages_raw)},
            "messages_body_sha256": sha256_hex(raw_body),
            "tls_pcap": {"path": tls_path, "sha256": sha256_hex(tls_pcap), "bytes": len(tls_pcap)},
        },
        "attestation": copy.deepcopy(policy["attestation"]),
        "messages": {
            "default_beta": head_text.split("anthropic-beta: ", 1)[1].split("\r\n", 1)[0],
            "system_blocks_json": wire_json(static_blocks),
            "system_text_sha256": system_digests,
        },
        "implementation_policy": {
            **implementation_policy,
            "schema_version": IMPLEMENTATION_POLICY_V3_SCHEMA,
            "model_catalog": model_catalog,
        },
        "transports": {"http1_with_alpn": with_alpn, "http1_without_alpn": without_alpn},
    }


def build_release_identity(
    *,
    version: str,
    platform: str,
    profile_digest: str,
    wire_digest: str,
    model_catalog_digest: str,
    required_rules_manifest_sha256: str,
    required_rule_count: int,
) -> tuple[str, str]:
    """Release 与 ReleaseBundle 的内容寻址摘要（与 Go 加载器 ``validateClaudeCatalogArtifactIdentities`` 同一算法）。"""

    release = {
        "schema_version": RELEASE_IDENTITY_SCHEMA,
        "version": version,
        "platform": platform,
        "profile_sha256": profile_digest,
        "wire_sha256": wire_digest,
        "model_capability_catalog_sha256": model_catalog_digest,
        "required_rules_manifest_sha256": required_rules_manifest_sha256,
        "required_rule_count": required_rule_count,
    }
    release_digest = sha256_hex(canonical_bytes(release))
    bundle = {
        "schema_version": BUNDLE_IDENTITY_SCHEMA,
        "persona": RELEASE_BUNDLE_PERSONA,
        "release_sha256": release_digest,
        "profile_sha256": profile_digest,
        "wire_sha256": wire_digest,
    }
    return release_digest, sha256_hex(canonical_bytes(bundle))
