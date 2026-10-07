#!/usr/bin/env python3
"""Claude 内容寻址 Profile、Wire、模型能力目录与 Release 身份的确定性投影工具。

历史上它从 FW-F 受批 Snapshot 与 2.1.226 原生证据生成 FW-G 制品；Claude 升级控制面（设计 B3-03 及其补充项）把它
改成与证据布局无关的投影工具，VC-3 的 ``prepare-profile`` 直接调用这里的投影函数（指南 §4.3.2：Profile、Wire、
模型目录和 Release 全部进入同一份批准摘要时，本工具才是该阶段的确定性投影工具）。

一、确定性投影（B3-03）
=====================
同一组冻结输入在任何工作目录、任何时间重跑得到逐字相同的输出：

* 版本、平台与时间不写死：平台取已校验画像的 ``identity.platform``，迁移收据的日期由 ``--receipt-date`` 显式给出，
  Release 的 persona 与收据阶段是本工具合同的具名常量；
* 输出里记录的证据与制品路径一律是仓库相对路径（``repository_relative``），与调用方传绝对路径还是相对路径无关；
* 输出只写一次：目标已存在且内容相同即复用，内容不同即拒绝覆盖；
* 迁移收据里源码 transition 的「之后」摘要取 ``--source-commit`` 指定提交里的 blob，不读工作树源码。

二、两份策略（B3-03 补充项：拆分策略 Schema）
==========================================
* **generation policy**（``claude-fw-g-generation-policy/v2``，历史 FW-G 专用、内容冻结）：冻结输入摘要、前一 Release、
  模型能力 Campaign 计数与迁移收据坐标，以及 official finalize／acceptance 等后续阶段身份。只给历史 CLI 用，指南 §4.3.1
  不许把它复制成新 Campaign 的 VC-3 policy。
* **Wire 投影策略**（``claude-code-wire-projection-policy/v1``，每个版本一份、只含投影所需的版本事实）：模型闭集与角色
  （主模型、TUI 标题模型、fallback 模型、legacy retry fallback、server fallback 锁存）、effort 闭集、版本指纹与 cch
  的 attestation、工具目录选择器（deferred MCP 与 background 工具数、advisor 工具类型、system 块数、background 请求数）、
  TLS 期望向量、实现策略的 identity／headers／retry 三段、thinking.display 取值、模型目录的别名与未知模型策略。
  原先散在代码里的模型名、版本指纹 salt 与 message index 等字面量全部移入这里；2.1.226 的值见
  ``tools/official_client_capture/claude_upgrade_wire_projection_2_1_226.json``。

三、证据来源（按证据布局分离）
============================
投影函数只经「证据来源」对象取请求，不认目录布局：

* ``FwfCampaignSource``：历史 FW-F／FW-G 布局（``attempts/<probe>/attempt-NNN/relay``、thinking.display 补充 Campaign、
  模型能力 Campaign），R 通道按 MeasuredRuleLedger 绑定；只用于重放 2.1.226 历史制品，逐字复算已入库字节。
* Claude 升级的 VC-1 布局由 ``tools/official_client_capture/claude_upgrade_release.py`` 提供同名方法的来源对象
  （capture manifest 的逐连接标签 + EvidencePackage v3 的外部证据绑定）。

来源对象的方法：``messages``（某 probe 的推理请求，按连接序）、``select_messages``、``base_select``（主模型 probe 的
任意请求）、``model_select``／``model_requests``（非主模型 probe）、``thinking_display_request``、
``thinking_production_receipt``、``evidence_ledger``、``catalog_evidence``。非主模型的 probe ID 为
``<模型>-<基础 probe>``（FW-G 模型能力 Campaign 的约定）。

本工具不再导入 ``claude_fw_f_profile``（附录 C：只读保留、不调用）：HTTP/1.1 请求流解析在本文件内，语义与原解析器
逐字相同，由 2.1.226 历史回归（逐字复算已入库 Profile／Wire）锁定。
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.official_client_capture.claude_generation_policy import (
    GenerationPolicyError,
    load_generation_policy,
)
from tools.official_client_control.canonical import canonical_sha256


EXPECTED_PROFILE_DIGEST: str
PREVIOUS_WIRE_DIGEST: str
PREVIOUS_RELEASE_DIGEST: str
PREVIOUS_BUNDLE_DIGEST: str
EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST: str
EXPECTED_ATOMIC_LEDGER_DIGEST: str
EXPECTED_SNAPSHOT_DIGEST: str
EXPECTED_EVIDENCE_PACKAGE_DIGEST: str
EXPECTED_SUPPORT_ENVELOPE_DIGEST: str
EXPECTED_RULE_COUNT: int
EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT: int
EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT: int
EXPECTED_STRICT_ENDPOINT_COUNT: int
EXPECTED_VERSION: str
EXPECTED_MESSAGES_EVIDENCE_DIGEST: str
EXPECTED_MESSAGES_BODY_DIGEST: str
EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS: dict[str, str]
EXPECTED_TLS_PCAP_DIGEST: str
EXPECTED_SYSTEM_TEXT_DIGESTS: list[str]
EXPECTED_CIPHERS: list[int]
EXPECTED_GROUPS: list[int]
EXPECTED_POINT_FORMATS: list[int]
EXPECTED_SIGNATURE_ALGORITHMS: list[int]
EXPECTED_SUPPORTED_VERSIONS: list[int]
EXPECTED_KEY_SHARE_GROUPS: list[int]
EXPECTED_PSK_MODES: list[int]
EXPECTED_WITH_ALPN_EXTENSIONS: list[int]
EXPECTED_WITHOUT_ALPN_EXTENSIONS: list[int]
MODEL_CAPABILITY_MODELS: tuple[str, ...]
MODEL_CAPABILITY_EFFORTS: tuple[str, ...]
MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS: int
MODEL_CAPABILITY_FAILED_ATTEMPTS: set[str]
MODEL_CAPABILITY_BASE_COMMIT: str
EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# 本生成器合同的固定身份（不随版本变化）：Release bundle 的 persona 与迁移收据的阶段名。
RELEASE_BUNDLE_PERSONA = "claude-code-oauth"
RECEIPT_PHASE = "FW-G"
RECEIPT_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MODEL_CAPABILITY_PRIOR_TRANSITIONS: tuple[tuple[str, str], ...]
MODEL_CAPABILITY_SOURCE_TRANSITIONS: tuple[tuple[str, str, str], ...]

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


def configure_generation_policy(policy_path: Path) -> None:
    """原子加载版本事实；生成器控制流不再因普通换版而修改。"""

    global EXPECTED_PROFILE_DIGEST
    global PREVIOUS_WIRE_DIGEST, PREVIOUS_RELEASE_DIGEST, PREVIOUS_BUNDLE_DIGEST
    global EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST, EXPECTED_ATOMIC_LEDGER_DIGEST
    global EXPECTED_SNAPSHOT_DIGEST, EXPECTED_EVIDENCE_PACKAGE_DIGEST
    global EXPECTED_SUPPORT_ENVELOPE_DIGEST, EXPECTED_RULE_COUNT
    global EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT, EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
    global EXPECTED_STRICT_ENDPOINT_COUNT
    global EXPECTED_VERSION, EXPECTED_MESSAGES_EVIDENCE_DIGEST
    global EXPECTED_MESSAGES_BODY_DIGEST, EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS
    global EXPECTED_TLS_PCAP_DIGEST, EXPECTED_SYSTEM_TEXT_DIGESTS
    global EXPECTED_CIPHERS, EXPECTED_GROUPS, EXPECTED_POINT_FORMATS
    global EXPECTED_SIGNATURE_ALGORITHMS, EXPECTED_SUPPORTED_VERSIONS
    global EXPECTED_KEY_SHARE_GROUPS, EXPECTED_PSK_MODES
    global EXPECTED_WITH_ALPN_EXTENSIONS, EXPECTED_WITHOUT_ALPN_EXTENSIONS
    global MODEL_CAPABILITY_MODELS, MODEL_CAPABILITY_EFFORTS
    global MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS, MODEL_CAPABILITY_FAILED_ATTEMPTS
    global MODEL_CAPABILITY_BASE_COMMIT, MODEL_CAPABILITY_PRIOR_TRANSITIONS
    global MODEL_CAPABILITY_SOURCE_TRANSITIONS

    try:
        document = load_generation_policy(policy_path)
        target = document["target"]
        frozen = document["frozen_inputs"]
        previous = document["previous_release"]
        tls = document["tls"]
        model = document["model_capability"]

        EXPECTED_VERSION = str(target["version"])
        EXPECTED_RULE_COUNT = int(target["required_rule_count"])
        EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT = int(
            target["profile_atomic_assertion_count"]
        )
        EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT = int(
            target["total_atomic_assertion_count"]
        )
        EXPECTED_STRICT_ENDPOINT_COUNT = int(target["strict_endpoint_count"])
        MODEL_CAPABILITY_MODELS = tuple(target["models"])
        MODEL_CAPABILITY_EFFORTS = tuple(target["efforts"])

        EXPECTED_PROFILE_DIGEST = frozen["profile_sha256"]
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST = frozen[
            "required_rules_manifest_sha256"
        ]
        EXPECTED_ATOMIC_LEDGER_DIGEST = frozen["atomic_ledger_sha256"]
        EXPECTED_SNAPSHOT_DIGEST = frozen["snapshot_sha256"]
        EXPECTED_EVIDENCE_PACKAGE_DIGEST = frozen["evidence_package_sha256"]
        EXPECTED_SUPPORT_ENVELOPE_DIGEST = frozen["support_envelope_sha256"]
        EXPECTED_MESSAGES_EVIDENCE_DIGEST = frozen["messages_evidence_sha256"]
        EXPECTED_MESSAGES_BODY_DIGEST = frozen["messages_body_sha256"]
        EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS = dict(
            frozen["thinking_display_request_sha256"]
        )
        EXPECTED_TLS_PCAP_DIGEST = frozen["tls_pcap_sha256"]
        EXPECTED_SYSTEM_TEXT_DIGESTS = list(frozen["system_text_sha256"])

        PREVIOUS_WIRE_DIGEST = previous["wire_sha256"]
        PREVIOUS_RELEASE_DIGEST = previous["release_sha256"]
        PREVIOUS_BUNDLE_DIGEST = previous["bundle_sha256"]

        EXPECTED_CIPHERS = list(tls["cipher_suites"])
        EXPECTED_GROUPS = list(tls["supported_groups"])
        EXPECTED_POINT_FORMATS = list(tls["point_formats"])
        EXPECTED_SIGNATURE_ALGORITHMS = list(tls["signature_algorithms"])
        EXPECTED_SUPPORTED_VERSIONS = list(tls["supported_versions"])
        EXPECTED_KEY_SHARE_GROUPS = list(tls["key_share_groups"])
        EXPECTED_PSK_MODES = list(tls["psk_modes"])
        EXPECTED_WITH_ALPN_EXTENSIONS = list(tls["with_alpn_extensions"])
        EXPECTED_WITHOUT_ALPN_EXTENSIONS = list(tls["without_alpn_extensions"])

        MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS = int(model["successful_attempts"])
        MODEL_CAPABILITY_FAILED_ATTEMPTS = set(
            model["historical_failed_attempt_ids"]
        )
        MODEL_CAPABILITY_BASE_COMMIT = model["base_commit"]
        MODEL_CAPABILITY_PRIOR_TRANSITIONS = tuple(
            (item["path"], item["sha256"])
            for item in model["prior_transitions"]
        )
        MODEL_CAPABILITY_SOURCE_TRANSITIONS = tuple(
            (item["path"], item["from_sha256"], item["reason"])
            for item in model["source_transitions"]
        )
    except (GenerationPolicyError, KeyError, TypeError, ValueError) as exc:
        raise ProjectionError(
            f"Claude FW-G generation policy 非法：{policy_path}: {exc}"
        ) from exc

    digest_values = [
        EXPECTED_PROFILE_DIGEST,
        PREVIOUS_WIRE_DIGEST,
        PREVIOUS_RELEASE_DIGEST,
        PREVIOUS_BUNDLE_DIGEST,
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        EXPECTED_ATOMIC_LEDGER_DIGEST,
        EXPECTED_SNAPSHOT_DIGEST,
        EXPECTED_EVIDENCE_PACKAGE_DIGEST,
        EXPECTED_SUPPORT_ENVELOPE_DIGEST,
        EXPECTED_MESSAGES_EVIDENCE_DIGEST,
        EXPECTED_MESSAGES_BODY_DIGEST,
        EXPECTED_TLS_PCAP_DIGEST,
        *EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS.values(),
        *EXPECTED_SYSTEM_TEXT_DIGESTS,
    ]
    if (
        not EXPECTED_VERSION
        or min(
            EXPECTED_RULE_COUNT,
            EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT,
            EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT,
            EXPECTED_STRICT_ENDPOINT_COUNT,
        )
        <= 0
        or not MODEL_CAPABILITY_MODELS
        or not MODEL_CAPABILITY_EFFORTS
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in digest_values
        )
    ):
        raise ProjectionError(f"Claude FW-G generation policy 坐标不完整：{policy_path}")


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


def check_projection_matches_generation_policy(policy: dict[str, Any]) -> None:
    """历史 CLI：Wire 投影策略与 v2 generation policy 的重叠事实（版本、模型、effort、TLS 向量）必须逐字相同。"""

    tls = {
        "cipher_suites": EXPECTED_CIPHERS,
        "supported_groups": EXPECTED_GROUPS,
        "point_formats": EXPECTED_POINT_FORMATS,
        "signature_algorithms": EXPECTED_SIGNATURE_ALGORITHMS,
        "supported_versions": EXPECTED_SUPPORTED_VERSIONS,
        "key_share_groups": EXPECTED_KEY_SHARE_GROUPS,
        "psk_modes": EXPECTED_PSK_MODES,
        "with_alpn_extensions": EXPECTED_WITH_ALPN_EXTENSIONS,
        "without_alpn_extensions": EXPECTED_WITHOUT_ALPN_EXTENSIONS,
    }
    if (
        policy["version"] != EXPECTED_VERSION
        or tuple(policy["models"]["ordered"]) != MODEL_CAPABILITY_MODELS
        or tuple(policy["efforts"]) != MODEL_CAPABILITY_EFFORTS
        or policy["tls"] != tls
    ):
        raise ProjectionError("Wire 投影策略与 generation policy 的版本、模型、effort 或 TLS 向量不一致")


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


def repository_relative(path: Path) -> str:
    """输出里记录的路径：一律换算成仓库相对路径，不随调用方的工作目录或传参形式变化。"""

    absolute = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    try:
        return absolute.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ProjectionError(f"制品或证据路径不在仓库内：{absolute}") from exc


def write_once(path: Path, raw: bytes) -> str:
    """只写一次：不存在时独占新建；已存在且逐字相同返回 ``reused``，内容不同即拒绝覆盖。"""

    if path.is_symlink():
        raise ProjectionError(f"输出路径是符号链接，拒绝写入：{path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != raw:
            raise ProjectionError(f"输出已存在且内容不同，禁止覆盖：{path}")
        return "reused"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "generated"


def git_blob_digest_reader(commit: str) -> Callable[[str], str]:
    """返回「仓库相对路径 → 该提交里 blob 的 SHA-256」的读取器；读 Git 对象库，不读工作树。"""

    if not GIT_COMMIT_RE.fullmatch(commit):
        raise ProjectionError(f"--source-commit 必须是 40 位小写提交号：{commit!r}")

    def digest(path: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "blob", f"{commit}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise ProjectionError(f"模型能力源码 transition 在 {commit} 缺少文件：{path}")
        return sha256_hex(completed.stdout)

    return digest


def build_model_capability_source_transitions(
    profile_path: Path,
    profile_digest: str,
    wire_path: Path,
    wire_digest: str,
    source_digest: Callable[[str], str],
) -> list[dict[str, str]]:
    """迁移收据的 transition：策略登记的源码取冻结提交里的摘要，本次生成的 Profile、Wire 取内存摘要。

    不读工作树：工作树随后续开发变化，读它会让同一组输入在不同时刻得到不同收据。
    """

    generated = {
        repository_relative(profile_path): profile_digest,
        repository_relative(wire_path): wire_digest,
    }
    specs = list(MODEL_CAPABILITY_SOURCE_TRANSITIONS)
    specs.extend(
        [
            (
                repository_relative(profile_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Profile，冻结三模型显式闭集和能力目录摘要。",
            ),
            (
                repository_relative(wire_path),
                EMPTY_FILE_SHA256,
                "新增内容寻址 Wire，冻结三模型逐场景官方请求与状态差异。",
            ),
        ]
    )
    paths = [path for path, _, _ in specs]
    if len(paths) != len(set(paths)):
        raise ProjectionError("模型能力源码 transition 路径重复")
    transitions: list[dict[str, str]] = []
    for path, before_digest, reason in sorted(specs):
        after_digest = generated[path] if path in generated else source_digest(path)
        if before_digest == after_digest:
            raise ProjectionError(f"模型能力源码 transition 未发生变化：{path}")
        transitions.append(
            {
                "path": path,
                "from_sha256": before_digest,
                "to_sha256": after_digest,
                "reason": reason,
            }
        )
    return transitions


def load_frozen_json(path: Path, expected_digest: str, label: str) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = sha256_hex(raw)
    if digest != expected_digest:
        raise ProjectionError(f"{label} 摘要不一致：{digest}")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ProjectionError(f"{label} 不是 JSON 对象")
    return document


def required_rule_mapping(rules: Any, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(rules, list) or len(rules) != EXPECTED_RULE_COUNT:
        raise ProjectionError(
            f"{label} RequiredRules 数量不是 {EXPECTED_RULE_COUNT}"
        )
    result: dict[str, tuple[str, ...]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise ProjectionError(f"{label} RequiredRule 不是对象")
        spec_id = rule.get("spec_id")
        atomic_ids = rule.get("atomic_assertion_ids")
        if (
            not isinstance(spec_id, str)
            or not spec_id.startswith("SPEC-")
            or not isinstance(atomic_ids, list)
            or not atomic_ids
            or any(not isinstance(item, str) or not item.startswith("SPEC-") for item in atomic_ids)
            or spec_id in result
        ):
            raise ProjectionError(f"{label} RequiredRule 或原子映射非法：{spec_id}")
        result[spec_id] = tuple(atomic_ids)
    return result


def validate_fw_f_rule_authorities(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    atomic_ledger: dict[str, Any],
    evidence_package: dict[str, Any],
    support_envelope: dict[str, Any],
) -> dict[str, Any]:
    try:
        profile_document = snapshot["payload"]["document"]
        evidence_rules = evidence_package["payload"]["rules"]
        support_spec_ids = support_envelope["payload"]["target_spec_ids"]
    except (KeyError, TypeError) as exc:
        raise ProjectionError("FW-F Snapshot／EvidencePackage／SupportEnvelope 结构不完整") from exc
    if not isinstance(profile_document, dict):
        raise ProjectionError("FW-F Snapshot profile document 非法")

    manifest_mapping = required_rule_mapping(manifest.get("required_rules"), "manifest")
    snapshot_mapping = required_rule_mapping(profile_document.get("rules"), "Snapshot")
    if manifest_mapping != snapshot_mapping:
        raise ProjectionError("RequiredRules manifest 与 Snapshot 的规则／原子映射不一致")

    manifest_profile_atomic = [
        assertion_id
        for assertion_ids in manifest_mapping.values()
        for assertion_id in assertion_ids
    ]
    if (
        len(manifest_profile_atomic) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
        or len(set(manifest_profile_atomic)) != EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT
    ):
        raise ProjectionError(
            f"{EXPECTED_PROFILE_ATOMIC_ASSERTION_COUNT} 条画像原子断言"
            "未恰好一次归属 RequiredRules"
        )

    scenario_groups = manifest.get("scenario_only_groups")
    if not isinstance(scenario_groups, list):
        raise ProjectionError("RequiredRules manifest 缺少 scenario-only 分组")
    scenario_atomic = [
        assertion_id
        for group in scenario_groups
        if isinstance(group, dict)
        for assertion_id in group.get("atomic_assertion_ids", [])
    ]
    all_manifest_atomic = manifest_profile_atomic + scenario_atomic
    ledger_entries = atomic_ledger.get("entries")
    if not isinstance(ledger_entries, list):
        raise ProjectionError("AtomicAssertionLedger entries 非法")
    ledger_atomic = [entry.get("spec_id") for entry in ledger_entries if isinstance(entry, dict)]
    if (
        len(all_manifest_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(set(all_manifest_atomic)) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or len(ledger_atomic) != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
        or set(ledger_atomic) != set(all_manifest_atomic)
    ):
        raise ProjectionError(
            f"{EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT} 条 AtomicAssertionLedger "
            "未与画像／scenario-only 映射完全一致"
        )

    evidence_spec_ids = [
        rule.get("spec_id") for rule in evidence_rules if isinstance(rule, dict)
    ]
    required_spec_ids = set(manifest_mapping)
    if (
        len(evidence_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(evidence_spec_ids)) != EXPECTED_RULE_COUNT
        or set(evidence_spec_ids) != required_spec_ids
        or not isinstance(support_spec_ids, list)
        or len(support_spec_ids) != EXPECTED_RULE_COUNT
        or len(set(support_spec_ids)) != EXPECTED_RULE_COUNT
        or set(support_spec_ids) != required_spec_ids
    ):
        raise ProjectionError("EvidencePackage／SupportEnvelope RequiredRule 集合不一致")

    strict_endpoints = profile_document.get("strict_endpoints")
    if (
        not isinstance(strict_endpoints, list)
        or len(strict_endpoints) != EXPECTED_STRICT_ENDPOINT_COUNT
    ):
        raise ProjectionError(
            "Snapshot strict endpoint 数量不是 "
            f"{EXPECTED_STRICT_ENDPOINT_COUNT}"
        )
    endpoint_spec_ids: list[str] = []
    for endpoint in strict_endpoints:
        spec_ids = endpoint.get("spec_ids") if isinstance(endpoint, dict) else None
        if (
            not isinstance(spec_ids, list)
            or not spec_ids
            or len(spec_ids) != len(set(spec_ids))
            or not set(spec_ids).issubset(required_spec_ids)
        ):
            raise ProjectionError("Snapshot strict endpoint 规则引用非法")
        endpoint_spec_ids.extend(spec_ids)
    if set(endpoint_spec_ids) != required_spec_ids:
        raise ProjectionError(
            f"{EXPECTED_STRICT_ENDPOINT_COUNT} 个 strict endpoint 的 SPEC 并集"
            f"未覆盖 {EXPECTED_RULE_COUNT} 条 RequiredRules"
        )
    return profile_document


# ---------------------------------------------------------------------------
# HTTP/1.1 请求流（与历史解析器逐字同义；本工具不再导入 claude_fw_f_profile）
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


def collect_measured_raw_sha256s(ledger_path: Path) -> set[str]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if (
        ledger.get("target_version") != EXPECTED_VERSION
        or ledger.get("rule_count") != EXPECTED_TOTAL_ATOMIC_ASSERTION_COUNT
    ):
        raise ProjectionError("MeasuredRuleLedger 身份或原子断言数量不一致")
    raw_sha256s: set[str] = set()
    for entry in ledger.get("entries", []):
        for reference in entry.get("evidence_refs", []):
            raw_sha256s.update(reference.get("raw_request_sha256s", []))
    if not raw_sha256s:
        raise ProjectionError("MeasuredRuleLedger 没有 R 通道请求摘要")
    return raw_sha256s


def load_scenario_messages(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
) -> list[dict[str, Any]]:
    relay_root = campaign_root / "attempts" / scenario / "attempt-001" / "relay"
    requests: list[dict[str, Any]] = []
    for path in sorted(relay_root.glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            if request.get("request_target") != "/v1/messages?beta=true":
                continue
            raw_sha256 = request.get("raw_sha256", "")
            if raw_sha256 not in allowed_raw_sha256s:
                raise ProjectionError(f"{scenario} 请求未绑定 MeasuredRuleLedger：{raw_sha256}")
            request = dict(request)
            request["evidence_path"] = repository_relative(path)
            requests.append(request)
    if not requests:
        raise ProjectionError(f"{scenario} 没有 messages R 通道")
    return requests


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


def successful_model_attempts(campaign_root: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    attempts_root = campaign_root / "attempts"
    successful: dict[str, Path] = {}
    failed: set[str] = set()
    attempt_receipts: list[dict[str, Any]] = []
    telemetry_markers = ("telemetry", "event_logging", "datadog", "statsig", "analytics")
    for scenario_root in sorted(attempts_root.iterdir()):
        if not scenario_root.is_dir():
            continue
        for attempt_root in sorted(scenario_root.glob("attempt-*")):
            manifest_path = attempt_root / "relay-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            attempt_id = f"{scenario_root.name}/{attempt_root.name}"
            complete = (
                manifest.get("status") == "complete"
                and manifest.get("m_binding", {}).get("complete") is True
                and manifest.get("secret_scan", {}).get("passed") is True
                and manifest.get("dimension_evidence", {}).get("result") == "passed"
                and manifest.get("m_binding", {})
                .get("requirements", {})
                .get("hosts_restored")
                is True
            )
            if not complete:
                failed.add(attempt_id)
                attempt_receipts.append(
                    {
                        "attempt_id": attempt_id,
                        "status": "historical_failed",
                        "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    }
                )
                continue
            for item in manifest.get("dimension_evidence", {}).get(
                "actual_wire_inventory", []
            ):
                identity = " ".join(
                    str(item.get(name, ""))
                    for name in ("request_target", "host", "evidence_file")
                ).lower()
                if any(marker in identity for marker in telemetry_markers):
                    raise ProjectionError(f"模型能力 Campaign 混入遥测／行为请求：{attempt_id}")
            runtime_path = (
                campaign_root
                / "runtime-receipts"
                / f"{scenario_root.name}-{attempt_root.name}.json"
            )
            if not runtime_path.is_file():
                raise ProjectionError(f"模型能力 attempt 缺少 runtime receipt：{attempt_id}")
            successful[scenario_root.name] = attempt_root
            attempt_receipts.append(
                {
                    "attempt_id": attempt_id,
                    "status": "complete",
                    "manifest_sha256": sha256_hex(manifest_path.read_bytes()),
                    "runtime_receipt_sha256": sha256_hex(runtime_path.read_bytes()),
                }
            )
    complete_count = sum(item["status"] == "complete" for item in attempt_receipts)
    if complete_count != MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS:
        raise ProjectionError(
            "模型能力成功 attempt 数量不是 "
            f"{MODEL_CAPABILITY_SUCCESSFUL_ATTEMPTS}：{complete_count}"
        )
    if failed != MODEL_CAPABILITY_FAILED_ATTEMPTS:
        raise ProjectionError(f"模型能力历史失败集合漂移：{sorted(failed)}")

    production_path = campaign_root / "environment" / "production-diff.json"
    production = json.loads(production_path.read_text(encoding="utf-8"))
    if production.get("identical") is not True or production.get("changed_fields") != []:
        raise ProjectionError("模型能力 Campaign 改变了 Vircs 生产服务")
    evidence = {
        "campaign_root": repository_relative(campaign_root),
        "successful_attempts": complete_count,
        "historical_failed_attempts": len(failed),
        "attempts": attempt_receipts,
        "production_diff": {
            "path": repository_relative(production_path),
            "sha256": sha256_hex(production_path.read_bytes()),
            "identical": True,
        },
        "behavior_traffic_disposition": "excluded-from-rules-and-difference-judgement",
    }
    evidence["campaign_sha256"] = sha256_hex(canonical_bytes(evidence))
    return successful, evidence


def load_attempt_requests(attempt_root: Path, scenario: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
        for request in parse_http_stream(path.read_bytes(), scenario):
            request = dict(request)
            request["evidence_path"] = repository_relative(path)
            requests.append(request)
    return requests


def select_base_campaign_request(
    campaign_root: Path,
    scenario: str,
    allowed_raw_sha256s: set[str],
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt_roots = sorted((campaign_root / "attempts" / scenario).glob("attempt-*"))
    selected_attempt = None
    for attempt_root in attempt_roots:
        manifest_path = attempt_root / "relay-manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") == "complete"
            and manifest.get("m_binding", {}).get("complete") is True
            and manifest.get("secret_scan", {}).get("passed") is True
        ):
            selected_attempt = attempt_root
    if selected_attempt is None:
        raise ProjectionError(f"基础 Campaign 缺少成功场景：{scenario}")
    for request in load_attempt_requests(selected_attempt, scenario):
        if not predicate(request):
            continue
        if request.get("raw_sha256") not in allowed_raw_sha256s:
            raise ProjectionError(f"{scenario} 请求未绑定 MeasuredRuleLedger")
        return request
    raise ProjectionError(f"{scenario} 没有 {description}")


def select_attempt_request(
    attempts: dict[str, Path],
    scenario: str,
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    attempt = attempts.get(scenario)
    if attempt is None:
        raise ProjectionError(f"模型能力 Campaign 缺少成功场景：{scenario}")
    selected = next(
        (request for request in load_attempt_requests(attempt, scenario) if predicate(request)),
        None,
    )
    if selected is None:
        raise ProjectionError(f"{scenario} 没有 {description}")
    return selected


# ---------------------------------------------------------------------------
# 历史 FW-F／FW-G 证据来源（只用于重放 2.1.226 历史制品）
# ---------------------------------------------------------------------------


class FwfCampaignSource:
    """历史 FW-F／FW-G 布局的证据来源。

    基础 Campaign 的推理请求只取 ``attempt-001`` 且必须绑定 MeasuredRuleLedger；主模型的任意请求取最后一个完整 attempt；
    非主模型取模型能力 Campaign 的成功 attempt（生成器历史行为，逐字保留）。thinking.display 取补充 Campaign 的冻结
    attempt 并核对 v2 generation policy 冻结的请求摘要。
    """

    # thinking.display 补充 Campaign 里各取值的成功 attempt 编号（历史布局事实）。
    THINKING_DISPLAY_ATTEMPTS = {"summarized": "002", "omitted": "001"}

    def __init__(self, campaign_root: Path, measured_ledger: Path, thinking_display_root: Path, capability_root: Path) -> None:
        self.campaign_root = campaign_root
        self.measured_ledger = measured_ledger
        self.thinking_display_root = thinking_display_root
        self.capability_root = capability_root
        self._allowed: set[str] | None = None
        self._capability: tuple[dict[str, Path], dict[str, Any]] | None = None

    def allowed(self) -> set[str]:
        if self._allowed is None:
            self._allowed = collect_measured_raw_sha256s(self.measured_ledger)
        return self._allowed

    def messages(self, probe: str) -> list[dict[str, Any]]:
        return load_scenario_messages(self.campaign_root, probe, self.allowed())

    def select_messages(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_request(self.messages(probe), predicate, description, probe)

    def base_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_base_campaign_request(self.campaign_root, probe, self.allowed(), predicate, description)

    def capability(self) -> tuple[dict[str, Path], dict[str, Any]]:
        if self._capability is None:
            self._capability = successful_model_attempts(self.capability_root)
        return self._capability

    def model_select(self, probe: str, predicate: Any, description: str) -> dict[str, Any]:
        return select_attempt_request(self.capability()[0], probe, predicate, description)

    def model_requests(self, probe: str) -> list[dict[str, Any]]:
        attempt = self.capability()[0].get(probe)
        if attempt is None:
            raise ProjectionError(f"模型能力 Campaign 缺少成功场景：{probe}")
        return load_attempt_requests(attempt, probe)

    def thinking_display_request(self, display: str) -> dict[str, Any]:
        probe = f"{PROBE_THINKING_DISPLAY_PREFIX}{display}"
        attempt = self.THINKING_DISPLAY_ATTEMPTS.get(display)
        if attempt is None:
            raise ProjectionError(f"thinking.display 补充 Campaign 没有 {display} 的冻结 attempt")
        attempt_root = self.thinking_display_root / "attempts" / probe / f"attempt-{attempt}"
        manifest = json.loads((attempt_root / "relay-manifest.json").read_text(encoding="utf-8"))
        dimension = manifest.get("dimension_evidence") or {}
        if (
            manifest.get("status") != "complete"
            or manifest.get("m_binding", {}).get("complete") is not True
            or manifest.get("secret_scan", {}).get("passed") is not True
            or dimension.get("result") != "passed"
        ):
            raise ProjectionError(f"{probe} R/M 没有完整通过")
        summary = json.loads((attempt_root / "results" / "v4-summary.json").read_text(encoding="utf-8"))
        argv = summary.get("inner_result", {}).get("invocation", {}).get("argv_redacted", [])
        expected_pair = ["--thinking-display", display]
        if summary.get("valid") is not True or not any(argv[index : index + 2] == expected_pair for index in range(len(argv) - 1)):
            raise ProjectionError(f"{probe} 未绑定冻结 CLI 参数")
        requests: list[dict[str, Any]] = []
        for path in sorted((attempt_root / "relay").glob("conn*.client_to_upstream.bin")):
            for request in parse_http_stream(path.read_bytes(), probe):
                if request.get("request_target") != MESSAGES_TARGET:
                    continue
                request = dict(request)
                request["evidence_path"] = repository_relative(path)
                requests.append(request)
        if len(requests) != 1:
            raise ProjectionError(f"{probe} messages R 通道数量不是 1")
        if requests[0].get("raw_sha256") != EXPECTED_THINKING_DISPLAY_REQUEST_DIGESTS[display]:
            raise ProjectionError(f"{probe} R 通道摘要漂移")
        return requests[0]

    def thinking_production_receipt(self) -> dict[str, str]:
        path = self.thinking_display_root / "environment" / "production-change-receipt.json"
        production = json.loads(path.read_text(encoding="utf-8"))
        if production.get("result") != "passed" or production.get("differences") != []:
            raise ProjectionError("thinking.display 补充 Campaign 影响了生产环境")
        return {"path": repository_relative(path), "sha256": sha256_hex(path.read_bytes())}

    def evidence_ledger(self) -> dict[str, str]:
        return {"path": repository_relative(self.measured_ledger), "sha256": sha256_hex(self.measured_ledger.read_bytes())}

    def catalog_evidence(self) -> dict[str, Any]:
        return self.capability()[1]


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


def extend_profile_for_model_capabilities(
    base_document: dict[str, Any],
    model_catalog: dict[str, Any],
    required_rule_count: int,
) -> dict[str, Any]:
    """历史 FW-G：把模型能力目录的闭集、别名／未知模型策略与目录摘要写进画像。"""

    document = copy.deepcopy(base_document)
    document["schema_version"] = "claude-code-fw-f-target-profile/v6"
    identity = document.setdefault("identity", {})
    identity["supported_models"] = [model["canonical_model"] for model in model_catalog["models"]]
    identity["model_alias_policy"] = model_catalog["alias_policy"]
    identity["unknown_model_policy"] = model_catalog["unknown_model_policy"]
    identity["model_capability_catalog_sha256"] = sha256_hex(canonical_bytes(model_catalog))
    body = document.setdefault("body", {})
    field_types = body.setdefault("field_types", {})
    field_types["fallbacks"] = ["array"]
    optional = body.setdefault("optional_fields", [])
    if "fallbacks" not in optional:
        optional.append("fallbacks")
    optional.sort()
    if len(document.get("rules", [])) != required_rule_count:
        raise ProjectionError(f"模型能力扩展改变了 {required_rule_count} 条 RequiredRules")
    return document


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


def build_model_capability_release_identity(
    profile_digest: str,
    wire_digest: str,
    model_catalog_digest: str,
    platform: str,
) -> tuple[str, str]:
    """历史 CLI：Release 身份按 v2 generation policy 的目标坐标计算。"""

    return build_release_identity(
        version=EXPECTED_VERSION,
        platform=platform,
        profile_digest=profile_digest,
        wire_digest=wire_digest,
        model_catalog_digest=model_catalog_digest,
        required_rules_manifest_sha256=EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        required_rule_count=EXPECTED_RULE_COUNT,
    )


def build_model_capability_receipt(
    model_catalog: dict[str, Any],
    profile_path: Path,
    profile_digest: str,
    wire_path: Path,
    wire_digest: str,
    release_digest: str,
    bundle_digest: str,
    *,
    receipt_date: str,
    platform: str,
    source_digest: Callable[[str], str],
) -> dict[str, Any]:
    if not RECEIPT_DATE_RE.fullmatch(receipt_date):
        raise ProjectionError(f"--receipt-date 必须是 YYYY-MM-DD：{receipt_date!r}")
    model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
    return {
        "schema_version": "official-egress-claude-model-capability-source-transition/v1",
        "date": receipt_date,
        "phase": RECEIPT_PHASE,
        "base_commit": MODEL_CAPABILITY_BASE_COMMIT,
        "prior_transitions": [
            {"path": path, "sha256": digest}
            for path, digest in MODEL_CAPABILITY_PRIOR_TRANSITIONS
        ],
        "target": {
            "product": "claude-code",
            "version": EXPECTED_VERSION,
            "platform": platform,
            "models": list(MODEL_CAPABILITY_MODELS),
        },
        "invariants": {
            "required_rule_count_before": EXPECTED_RULE_COUNT,
            "required_rule_count_after": EXPECTED_RULE_COUNT,
            "required_rules_duplicated_per_model": False,
            "unknown_model_policy": "deny",
            "alias_policy": "explicit-only",
            "telemetry_and_behavior_traffic": "excluded",
        },
        "evidence": model_catalog["evidence"],
        "model_capability_catalog_sha256": model_catalog_digest,
        "artifacts": {
            "before": {
                "profile_sha256": EXPECTED_PROFILE_DIGEST,
                "wire_sha256": PREVIOUS_WIRE_DIGEST,
                "release_sha256": PREVIOUS_RELEASE_DIGEST,
                "bundle_sha256": PREVIOUS_BUNDLE_DIGEST,
            },
            "after": {
                "profile": repository_relative(profile_path),
                "profile_sha256": profile_digest,
                "wire": repository_relative(wire_path),
                "wire_sha256": wire_digest,
                "release_sha256": release_digest,
                "bundle_sha256": bundle_digest,
            },
        },
        "transitions": build_model_capability_source_transitions(
            profile_path,
            profile_digest,
            wire_path,
            wire_digest,
            source_digest,
        ),
        "safety": {
            "vircs_production_changed": False,
            "production_selector_changed": False,
            "dmit_deployed": False,
        },
        "result": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--generation-policy",
        type=Path,
        required=True,
        help="冻结目标版本、输入摘要、模型能力 Campaign 计数与迁移收据坐标的历史 FW-G 策略（v2）",
    )
    parser.add_argument(
        "--projection-policy",
        type=Path,
        help="生成模型能力目录与 Wire 时必填：该版本的 Wire 投影策略（模型角色、attestation、选择器、TLS 与实现策略三段）",
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--required-rules-manifest", type=Path, required=True)
    parser.add_argument("--measured-ledger", type=Path, required=True)
    parser.add_argument("--evidence-package", type=Path, required=True)
    parser.add_argument("--support-envelope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--messages-evidence", type=Path)
    parser.add_argument("--tls-pcap", type=Path)
    parser.add_argument("--wire-output", type=Path)
    parser.add_argument("--campaign-root", type=Path)
    parser.add_argument("--thinking-display-campaign-root", type=Path)
    parser.add_argument("--model-capability-campaign-root", type=Path)
    parser.add_argument("--model-capability-receipt-output", type=Path)
    parser.add_argument(
        "--receipt-date",
        help="写迁移收据时必填：收据日期（YYYY-MM-DD），由调用方按批准时刻给出，生成器不取当前时间",
    )
    parser.add_argument(
        "--source-commit",
        help="写迁移收据时必填：源码 transition 的「之后」摘要所取的冻结提交（40 位），不读工作树",
    )
    args = parser.parse_args()

    configure_generation_policy(args.generation_policy)

    snapshot = load_frozen_json(args.snapshot, EXPECTED_SNAPSHOT_DIGEST, "Snapshot")
    manifest = load_frozen_json(
        args.required_rules_manifest,
        EXPECTED_REQUIRED_RULES_MANIFEST_DIGEST,
        "RequiredRules manifest",
    )
    atomic_ledger = load_frozen_json(
        args.measured_ledger,
        EXPECTED_ATOMIC_LEDGER_DIGEST,
        "AtomicAssertionLedger",
    )
    evidence_package = load_frozen_json(
        args.evidence_package,
        EXPECTED_EVIDENCE_PACKAGE_DIGEST,
        "EvidencePackage",
    )
    support_envelope = load_frozen_json(
        args.support_envelope,
        EXPECTED_SUPPORT_ENVELOPE_DIGEST,
        "SupportEnvelope",
    )
    document = validate_fw_f_rule_authorities(
        snapshot,
        manifest,
        atomic_ledger,
        evidence_package,
        support_envelope,
    )
    if document.get("identity", {}).get("version") != EXPECTED_VERSION:
        raise ProjectionError(f"Snapshot 目标版本不是 {EXPECTED_VERSION}")
    platform = document.get("identity", {}).get("platform")
    if not isinstance(platform, str) or not platform:
        raise ProjectionError("Snapshot 缺少 identity.platform")

    model_catalog = None
    source = None
    projection = None
    implementation_policy = None
    if args.model_capability_campaign_root is not None:
        if args.campaign_root is None or args.thinking_display_campaign_root is None:
            raise ProjectionError("模型能力目录需要基础 Campaign 与 thinking.display Campaign")
        if args.projection_policy is None:
            raise ProjectionError("模型能力目录需要 --projection-policy（该版本的 Wire 投影策略）")
        projection = load_projection_policy(args.projection_policy)
        check_projection_matches_generation_policy(projection)
        source = FwfCampaignSource(
            args.campaign_root,
            args.measured_ledger,
            args.thinking_display_campaign_root,
            args.model_capability_campaign_root,
        )
        implementation_policy = build_implementation_policy(source, projection)
        model_catalog = build_model_capability_catalog(
            source,
            projection,
            implementation_policy["scenarios"],
            EXPECTED_RULE_COUNT,
        )
        document = extend_profile_for_model_capabilities(document, model_catalog, EXPECTED_RULE_COUNT)

    raw = canonical_bytes(document)
    digest = hashlib.sha256(raw).hexdigest()
    if model_catalog is None and digest != EXPECTED_PROFILE_DIGEST:
        raise ProjectionError(f"画像摘要不一致：{digest}")

    state = write_once(args.output, raw)
    print(f"{state} {args.output} sha256={digest} bytes={len(raw)}")
    wire_args = (
        args.messages_evidence,
        args.tls_pcap,
        args.wire_output,
        args.campaign_root,
        args.thinking_display_campaign_root,
        args.model_capability_campaign_root,
        args.model_capability_receipt_output,
    )
    if any(wire_args) and not all(wire_args):
        raise ProjectionError("生成 wire artifact 时必须同时提供五个 wire 参数")
    if all(wire_args):
        if model_catalog is None or projection is None or implementation_policy is None:
            raise ProjectionError("生成新版 wire artifact 时缺少模型能力目录")
        if not args.receipt_date or not args.source_commit:
            raise ProjectionError("写迁移收据时必须提供 --receipt-date 与 --source-commit")
        source_digest = git_blob_digest_reader(args.source_commit)
        wire_document = build_wire_artifact(
            projection,
            implementation_policy,
            model_catalog,
            args.messages_evidence.read_bytes(),
            repository_relative(args.messages_evidence),
            args.tls_pcap.read_bytes(),
            repository_relative(args.tls_pcap),
            digest,
            platform,
            frozen={
                "messages_sha256": EXPECTED_MESSAGES_EVIDENCE_DIGEST,
                "messages_body_sha256": EXPECTED_MESSAGES_BODY_DIGEST,
                "system_text_sha256": EXPECTED_SYSTEM_TEXT_DIGESTS,
                "tls_pcap_sha256": EXPECTED_TLS_PCAP_DIGEST,
            },
        )
        wire_raw = canonical_bytes(wire_document)
        wire_digest = sha256_hex(wire_raw)
        state = write_once(args.wire_output, wire_raw)
        print(
            f"{state} {args.wire_output} sha256={wire_digest} "
            f"bytes={len(wire_raw)}"
        )
        model_catalog_digest = sha256_hex(canonical_bytes(model_catalog))
        release_digest, bundle_digest = build_model_capability_release_identity(
            digest, wire_digest, model_catalog_digest, platform
        )
        receipt = build_model_capability_receipt(
            model_catalog,
            args.output,
            digest,
            args.wire_output,
            wire_digest,
            release_digest,
            bundle_digest,
            receipt_date=args.receipt_date,
            platform=platform,
            source_digest=source_digest,
        )
        receipt_raw = canonical_bytes(receipt)
        state = write_once(args.model_capability_receipt_output, receipt_raw)
        print(
            f"{state} {args.model_capability_receipt_output} "
            f"sha256={sha256_hex(receipt_raw)} bytes={len(receipt_raw)}"
        )
        print(f"release_sha256={release_digest} bundle_sha256={bundle_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
