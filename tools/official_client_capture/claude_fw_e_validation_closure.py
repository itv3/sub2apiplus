#!/usr/bin/env python3
"""闭合 FW-E 发现清单，并把多个发现项归并为语义候选。

本工具严格区分三层数据：``DiscoveryInventory`` 保存每个原始发现项；
``SemanticRuleCandidate`` 通过 ``source_ids`` 多对一归并语义线索；
``RuleLedger`` 只由跨来源矩阵中的可执行 SPEC 组成。本工具不生成任何 SPEC。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


SCHEMA_PRIOR = "claude-code-fw-e-cross-source-dispositions/v1"
SCHEMA_OUTPUT = "claude-code-fw-e-cross-source-dispositions/v3"
SCHEMA_SOURCE_TO_SINK = "claude-code-fw-e-validation-source-to-sink/v2"
SCHEMA_DOCUMENT_ATOMS = "claude-code-fw-e-hitcc-document-atoms/v2"
SCHEMA_DISCOVERY = "claude-code-fw-e-discovery-inventory/v1"
SCHEMA_CANDIDATES = "claude-code-fw-e-semantic-candidates/v1"
SCHEMA_REVIEW = "claude-code-fw-e-semantic-closure-review/v1"
PROVEN_NOT_TRAFFIC_SINK_ID = "TN-SINK-6f38bd6ba928e70c-1"
PROVEN_NOT_TRAFFIC_CALL = "r.currentTurn.files.get(l)"
MARKDOWN_LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(.+?)\s*$")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
SOURCE_LOCATION_RE = re.compile(r"^(.*?):\d+(?:-\d+)?$")


class ValidationClosureError(RuntimeError):
    """表示输入分母、证据绑定或语义归并边界不闭合。"""


def candidate_definition(
    candidate_kind: str,
    domain: str,
    retained_claim: str,
    scope: str,
    required_channels: list[str],
) -> dict[str, Any]:
    """构造固定候选定义，避免从发现项原文临时发明语义。"""

    return {
        "candidate_kind": candidate_kind,
        "domain": domain,
        "retained_claim": retained_claim,
        "scope": scope,
        "required_channels": required_channels,
    }


# 身份来自既有 2.1.220／HitCC 候选账本；它们是归并键，不是 SPEC。
SEMANTIC_CANDIDATE_CATALOG: dict[str, dict[str, Any]] = {
    "CAND-AUTH-BEARER": candidate_definition(
        "wire_semantic", "authentication",
        "第一方 OAuth 模式的 SDK 凭据选择与 Authorization: Bearer 出站语义。",
        "first-party OAuth inference authentication", ["J", "R", "M"],
    ),
    "CAND-BETA-CUSTOM": candidate_definition(
        "wire_semantic", "beta",
        "环境或调用方 beta 值经过解析、过滤、去重并进入最终 beta 序列。",
        "custom beta composition", ["J", "M"],
    ),
    "CAND-BG-SESSION": candidate_definition(
        "wire_semantic", "header",
        "前台与后台会话对 x-app、会话身份及请求状态采用不同条件语义。",
        "foreground and background sessions", ["J", "M"],
    ),
    "CAND-BODY-BILLING-CONDITIONS": candidate_definition(
        "wire_semantic", "body",
        "计费归因 system 文本的启用条件及 cc_version、cc_entrypoint、cc_prev_req、cc_is_subagent 等字段。",
        "billing attribution system block", ["J", "M"],
    ),
    "CAND-BODY-CCH-REWRITE": candidate_definition(
        "wire_semantic", "body",
        "计费归因中的 cch 占位值在发行物封装后被改写为最终客户端指纹。",
        "packaged cch attribution value", ["J", "M"],
    ),
    "CAND-BODY-CONDITIONAL-FIELDS": candidate_definition(
        "wire_semantic", "body",
        "thinking、temperature、context_management、output_config、speed、tool_choice、fallback 与 diagnostics 等条件字段。",
        "conditional inference body fields", ["J", "M"],
    ),
    "CAND-CACHE-DIAGNOSIS": candidate_definition(
        "wire_semantic", "cache",
        "cache diagnosis beta、diagnostics.previous_message_id 及不兼容响应后的定向移除重试。",
        "cache diagnosis request and retry", ["J", "M"],
    ),
    "CAND-CACHE-MESSAGE": candidate_definition(
        "wire_semantic", "cache",
        "消息级 cache breakpoint 的选择、左移、fork 钉住和数量边界。",
        "message cache breakpoints", ["J", "M"],
    ),
    "CAND-CACHE-SYSTEM-SCOPE": candidate_definition(
        "wire_semantic", "cache",
        "system prompt 的 cache_control、ttl、scope、动态边界及 MCP 降级规则。",
        "system prompt cache scopes", ["J", "M"],
    ),
    "CAND-EP-AUXILIARY": candidate_definition(
        "inventory_obligation", "endpoint",
        "Claude 账号、配置、用量、遥测、远程会话等非推理辅助端点的受管处置全集。",
        "non-inference auxiliary egress inventory", ["P", "R", "J", "M"],
    ),
    "CAND-EP-COUNTTOKENS": candidate_definition(
        "managed_semantic", "endpoint",
        "messages/count_tokens 的端点、请求体复用、占位消息、字段裁剪和失败回退。",
        "managed count_tokens egress", ["J", "R", "M"],
    ),
    "CAND-EP-FULLSET": candidate_definition(
        "inventory_obligation", "endpoint",
        "目标发行物全部真实网络发送点的完整分类与 source-to-sink 覆盖义务。",
        "complete target-native network surface", ["P", "R", "J", "M"],
    ),
    "CAND-EP-SDK-SURFACE": candidate_definition(
        "inventory_obligation", "endpoint",
        "内嵌 SDK 的 models、batches、files、skills、agents、sessions 等资源能力面。",
        "embedded SDK resource surface", ["J", "M"],
    ),
    "CAND-FALLBACK-PROTOCOL": candidate_definition(
        "wire_semantic", "fallback",
        "server-side fallback、fallback beta、fallback_credit_token 的请求与响应联动。",
        "server fallback protocol", ["J", "R", "M"],
    ),
    "CAND-HDR-CUSTOM-MATRIX": candidate_definition(
        "wire_semantic", "header",
        "ANTHROPIC_CUSTOM_HEADERS 的解析、过滤、合并、覆盖和最终 Header 顺序。",
        "custom header matrix", ["J", "M"],
    ),
    "CAND-HDR-DISPATCH-RETRY": candidate_definition(
        "wire_semantic", "header",
        "anthropic-dispatch-id 的条件发送以及首事件前失败后的移除重试。",
        "dispatch header retry", ["J", "R", "M"],
    ),
    "CAND-HDR-REMOTE-MATRIX": candidate_definition(
        "wire_semantic", "header",
        "agent、parent-agent、remote-container、remote-session、client-app Header 的组合和值关系。",
        "remote and agent identity header matrix", ["J", "M"],
    ),
    "CAND-HDR-TRACEPARENT": candidate_definition(
        "wire_semantic", "header",
        "traceparent 的 span、provider、host 和环境门控传播规则。",
        "trace context propagation", ["J", "M"],
    ),
    "CAND-HDR-USAGE-LIMIT": candidate_definition(
        "wire_semantic", "header",
        "anthropic-usage-limit Header 的 depth、source、first-party 与功能门控条件。",
        "usage-limit header conditions", ["J", "M"],
    ),
    "CAND-METADATA-EXTRA": candidate_definition(
        "wire_semantic", "metadata",
        "CLAUDE_CODE_EXTRA_METADATA 的对象校验、浅合并、身份键覆盖与错误边界。",
        "extra metadata composition", ["J", "M"],
    ),
    "CAND-NONMAIN-THREADS": candidate_definition(
        "wire_semantic", "body",
        "subagent、sidechain、fork、hook、compact 等非主线程请求的上下文裁剪和 system/messages 形态。",
        "non-main inference paths", ["J", "R", "M"],
    ),
    "CAND-QUOTA-PROBE": candidate_definition(
        "managed_semantic", "body",
        "通过 messages API 执行额度或 rate-limit 探测时的最小请求体。",
        "managed quota probe", ["J", "R", "M"],
    ),
    "CAND-RETRY-529-FALLBACK": candidate_definition(
        "wire_semantic", "connection_retry",
        "连续 529、fallback model、context overflow 与 max_tokens override 的联动。",
        "529 and context fallback retry", ["J", "R", "L", "M"],
    ),
    "CAND-RETRY-MATRIX": candidate_definition(
        "wire_semantic", "connection_retry",
        "网络错误、401、408、409、429、5xx、Retry-After、后台和持久模式的完整重试矩阵。",
        "application retry matrix", ["J", "R", "L", "M"],
    ),
    "CAND-SERVER-ADVISOR": candidate_definition(
        "wire_semantic", "tool",
        "advisor server tool 的条件注入、schema 和模型字段。",
        "advisor server tool", ["J", "M"],
    ),
    "CAND-SERVER-WEBSEARCH": candidate_definition(
        "wire_semantic", "tool",
        "web_search server tool 的 schema、域名限制、次数、tool_choice 与专用提示。",
        "web search server tool", ["J", "M"],
    ),
    "CAND-STREAM-NONSTREAM": candidate_definition(
        "wire_semantic", "fallback",
        "streaming 主路径切换到 non-streaming messages.create 的触发、请求改写与响应形态。",
        "stream to non-stream fallback", ["J", "R", "M"],
    ),
    "CAND-SYSTEM-SEMANTICS": candidate_definition(
        "wire_semantic", "body",
        "system identity、userContext、systemContext、附件和动态 section 的注入及顺序语义。",
        "system and context semantics", ["J", "M"],
    ),
    "CAND-TOOLS-DEFERRED": candidate_definition(
        "wire_semantic", "tool",
        "defer_loading、ToolSearch、tool_reference 与下一轮完整 schema 恢复。",
        "deferred tool loading", ["J", "M"],
    ),
    "CAND-TOOLS-EXTENDED": candidate_definition(
        "wire_semantic", "tool",
        "工具 schema 的 strict、eager_input_streaming、cache_control 及不支持字段裁剪。",
        "extended tool schema fields", ["J", "M"],
    ),
    "CAND-UA-CLI": candidate_definition(
        "wire_semantic", "header",
        "Claude CLI 版本、entrypoint、agent-sdk、client-app 与 workload 组成的 User-Agent。",
        "CLI User-Agent composition", ["J", "M"],
    ),
    "CAND-WORKLOAD": candidate_definition(
        "wire_semantic", "header",
        "workload 身份对 User-Agent、归因字段及请求条件的影响。",
        "workload identity", ["J", "M"],
    ),
}


# 2.1.88 中没有 CAND 引用的 29 个原子命题，显式归并到既有语义候选。
SOURCE_CANDIDATE_MAP: dict[str, list[str]] = {
    "SRC2188-AUTH-003": ["CAND-RETRY-MATRIX"],
    "SRC2188-BETA-008": ["CAND-BETA-CUSTOM"],
    "SRC2188-BETA-009": ["CAND-BETA-CUSTOM", "CAND-TOOLS-DEFERRED"],
    "SRC2188-BETA-010": ["CAND-BETA-CUSTOM", "CAND-TOOLS-EXTENDED"],
    "SRC2188-BODY-004": ["CAND-BODY-CONDITIONAL-FIELDS"],
    "SRC2188-BODY-008": ["CAND-BODY-CONDITIONAL-FIELDS"],
    "SRC2188-BODY-009": ["CAND-BODY-CONDITIONAL-FIELDS"],
    "SRC2188-BODY-012": ["CAND-BODY-CONDITIONAL-FIELDS"],
    "SRC2188-BODY-016": ["CAND-BODY-BILLING-CONDITIONS"],
    "SRC2188-CACHE-001": ["CAND-CACHE-SYSTEM-SCOPE"],
    "SRC2188-CACHE-003": ["CAND-CACHE-SYSTEM-SCOPE"],
    "SRC2188-CACHE-008": ["CAND-CACHE-MESSAGE"],
    "SRC2188-FALLBACK-001": ["CAND-STREAM-NONSTREAM"],
    "SRC2188-FALLBACK-002": ["CAND-STREAM-NONSTREAM"],
    "SRC2188-FALLBACK-003": ["CAND-STREAM-NONSTREAM", "CAND-RETRY-529-FALLBACK"],
    "SRC2188-HIST-003": ["CAND-BODY-BILLING-CONDITIONS"],
    "SRC2188-HIST-004": ["CAND-BODY-BILLING-CONDITIONS"],
    "SRC2188-REQ-003": ["CAND-RETRY-MATRIX"],
    "SRC2188-REQ-004": ["CAND-EP-FULLSET"],
    "SRC2188-RETRY-003": ["CAND-RETRY-MATRIX"],
    "SRC2188-RETRY-004": ["CAND-RETRY-MATRIX"],
    "SRC2188-RETRY-007": ["CAND-RETRY-529-FALLBACK"],
    "SRC2188-RETRY-008": ["CAND-RETRY-529-FALLBACK"],
    "SRC2188-TOOL-002": ["CAND-TOOLS-EXTENDED"],
    "SRC2188-TOOL-003": ["CAND-TOOLS-EXTENDED"],
    "SRC2188-TOOL-004": ["CAND-TOOLS-DEFERRED"],
    "SRC2188-TOOL-006": ["CAND-TOOLS-EXTENDED"],
    "SRC2188-TOOL-007": ["CAND-TOOLS-DEFERRED"],
    "SRC2188-TOOL-008": ["CAND-TOOLS-DEFERRED", "CAND-TOOLS-EXTENDED"],
}


TEXT_CANDIDATE_PATTERNS: dict[str, tuple[str, ...]] = {
    "CAND-AUTH-BEARER": (
        r"authorization\s*:\s*bearer",
        r"authToken",
        r"oauth.+(?:apiKey|bearer)",
    ),
    "CAND-BETA-CUSTOM": (r"ANTHROPIC_BETAS", r"custom beta", r"environment beta"),
    "CAND-BG-SESSION": (r"cli-bg", r"background session", r"后台.+x-app"),
    "CAND-BODY-BILLING-CONDITIONS": (r"billing", r"attribution", r"cc_(?:version|entrypoint|prev_req|is_subagent)"),
    "CAND-BODY-CCH-REWRITE": (r"\bcch\b", r"00000.+rewrite", r"五位.+指纹"),
    "CAND-BODY-CONDITIONAL-FIELDS": (r"max_tokens", r"thinking", r"temperature", r"context_management", r"output_config", r"tool_choice", r"fallback_credit_token", r"diagnostics\.previous_message_id", r"fast mode"),
    "CAND-CACHE-DIAGNOSIS": (r"cache[-_ ]diagnosis", r"diagnostics\.previous_message_id"),
    "CAND-CACHE-MESSAGE": (r"cache breakpoint", r"skipCacheWrite", r"forkPointUuid", r"message.+cache_control"),
    "CAND-CACHE-SYSTEM-SCOPE": (r"system.+cache", r"cache_control", r"ttl.?=.?1h", r"cache scope"),
    "CAND-EP-AUXILIARY": (r"/api/(?:oauth|claude|event_logging|frame|organizations)", r"control[- ]plane", r"usage", r"bootstrap", r"telemetry", r"remote session", r"worker/"),
    "CAND-EP-COUNTTOKENS": (r"count[_-]?tokens", r"countTokens"),
    "CAND-EP-SDK-SURFACE": (r"/v1/(?:files|models|messages/batches|skills|agents|sessions|memory_stores|vaults|environments)", r"SDK surface", r"resource call"),
    "CAND-FALLBACK-PROTOCOL": (r"server[- ]side fallback", r"fallback[-_ ]credit", r"fallback_credit_token"),
    "CAND-HDR-CUSTOM-MATRIX": (r"ANTHROPIC_CUSTOM_HEADERS", r"custom header"),
    "CAND-HDR-DISPATCH-RETRY": (r"anthropic-dispatch-id", r"dispatch.+retry"),
    "CAND-HDR-REMOTE-MATRIX": (r"x-claude-code-(?:agent|parent-agent)", r"x-claude-remote-", r"x-client-app", r"CLAUDE_CODE_(?:CONTAINER|REMOTE_SESSION)_ID", r"CLAUDE_AGENT_SDK_CLIENT_APP"),
    "CAND-HDR-TRACEPARENT": (r"traceparent", r"PROPAGATE_TRACEPARENT"),
    "CAND-HDR-USAGE-LIMIT": (r"anthropic-usage-limit", r"usage limit.+header"),
    "CAND-METADATA-EXTRA": (r"CLAUDE_CODE_EXTRA_METADATA", r"extra metadata"),
    "CAND-NONMAIN-THREADS": (r"subagent", r"sidechain", r"compact", r"hook_prompt", r"hook_agent", r"fork-family", r"non-main"),
    "CAND-QUOTA-PROBE": (r"quota.+probe", r"rate-limit.+probe", r"content.?[:=].?quota"),
    "CAND-RETRY-529-FALLBACK": (r"\b529\b.+fallback", r"fallback model", r"context overflow", r"maxTokensOverride"),
    "CAND-RETRY-MATRIX": (r"retry", r"Retry-After", r"maxRetries", r"\b(?:401|408|409|429|5\d\d)\b", r"exponential backoff", r"timeout"),
    "CAND-SERVER-ADVISOR": (r"advisor_\d+", r"server.+advisor", r"name.?[:=].?advisor"),
    "CAND-SERVER-WEBSEARCH": (r"web_search_\d+", r"WebSearchTool", r"server.+web.?search", r"allowed_domains"),
    "CAND-STREAM-NONSTREAM": (r"non[- ]?stream", r"streaming.+fallback", r"messages\.stream", r"stream.?[:=].?false"),
    "CAND-SYSTEM-SEMANTICS": (r"system prompt", r"systemContext", r"userContext", r"system-reminder", r"identity section", r"prompt assembly"),
    "CAND-TOOLS-DEFERRED": (r"defer_loading", r"deferred tool", r"ToolSearch", r"tool_reference"),
    "CAND-TOOLS-EXTENDED": (r"eager_input_streaming", r"tool schema", r"工具.+strict", r"input_schema"),
    "CAND-UA-CLI": (r"User-Agent", r"claude-cli/", r"entrypoint"),
    "CAND-WORKLOAD": (r"\bworkload\b", r"cc_workload"),
}
COMPILED_TEXT_PATTERNS = {
    identity: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for identity, patterns in TEXT_CANDIDATE_PATTERNS.items()
}
EGRESS_SIGNAL_RE = re.compile(
    r"(?:https?://|/v1/|/api/|fetch\b|websocket|eventsource|xmlhttprequest|"
    r"request|response|header|oauth|anthropic|network|endpoint|proxy|socket|tls|"
    r"请求|响应|出站|端点|网络|重试|遥测)",
    re.IGNORECASE,
)


def load_json(path: Path, label: str) -> dict[str, Any]:
    """读取普通 JSON 对象并拒绝符号链接。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"{label} 不是可信普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationClosureError(f"无法读取 {label}：{path}") from error
    if not isinstance(value, dict):
        raise ValidationClosureError(f"{label} 顶层必须是对象：{path}")
    return value


def workspace_relative(workspace_root: Path, path: Path, label: str) -> str:
    """把可信普通文件转换为工作区相对路径。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"{label} 不是可信普通文件：{path}")
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError as error:
        raise ValidationClosureError(f"{label} 位于工作区外：{path}") from error


def future_workspace_relative(workspace_root: Path, path: Path, label: str) -> str:
    """转换尚未写出的输出路径，并要求输出仍位于工作区内。"""

    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError as error:
        raise ValidationClosureError(f"{label} 位于工作区外：{path}") from error


def binding(workspace_root: Path, path: Path, label: str) -> dict[str, str]:
    """生成带摘要的工作区输入绑定。"""

    return {
        "path": workspace_relative(workspace_root, path, label),
        "sha256": sha256_file(path),
    }


def write_private_json(path: Path, value: Any) -> None:
    """以规范 JSON 写入仅当前用户可读的文件。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_json_bytes(value))
    os.chmod(path, 0o600)


def indexed(rows: Any, key: str, label: str) -> dict[str, dict[str, Any]]:
    """把身份唯一的对象数组转换为索引。"""

    if not isinstance(rows, list):
        raise ValidationClosureError(f"{label} 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationClosureError(f"{label} 条目必须是对象")
        identity = row.get(key)
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValidationClosureError(f"{label} 身份缺失或重复：{identity}")
        result[identity] = row
    return result


def stable_hash(value: str, length: int = 12) -> str:
    """生成用于稳定身份的短摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()


def document_atom_id(path: str, line: int, text: str) -> str:
    """生成独立于候选和 SPEC 的文档原子身份。"""

    return f"HDOC-{stable_hash(path)}-L{line}-{stable_hash(text)}"


def parse_markdown_list_atoms(path: Path, relative_path: str) -> list[dict[str, Any]]:
    """无截断提取 Markdown 非代码区的每个列表项及其续行。"""

    if path.is_symlink() or not path.is_file():
        raise ValidationClosureError(f"HitCC 文档不是可信普通文件：{path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    atoms: list[dict[str, Any]] = []
    headings: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        heading = MARKDOWN_HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            headings = headings[: level - 1]
            headings.append(heading.group(2).strip())
            index += 1
            continue
        match = MARKDOWN_LIST_RE.match(raw)
        if not match:
            index += 1
            continue
        start = index + 1
        parts = [match.group(1).strip()]
        end = start
        lookahead = index + 1
        while lookahead < len(lines):
            continuation = lines[lookahead]
            if not continuation.strip():
                break
            if MARKDOWN_HEADING_RE.match(continuation) or MARKDOWN_LIST_RE.match(continuation):
                break
            if continuation.lstrip().startswith(("```", "~~~")):
                break
            if continuation.startswith((" ", "\t")):
                parts.append(continuation.strip())
                end = lookahead + 1
                lookahead += 1
                continue
            break
        text = " ".join(part for part in parts if part).strip()
        if text:
            atoms.append(
                {
                    "atom_id": document_atom_id(relative_path, start, text),
                    "path": relative_path,
                    "line_start": start,
                    "line_end": end,
                    "heading": " / ".join(headings),
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        index = max(index + 1, lookahead)
    identities = [row["atom_id"] for row in atoms]
    if len(identities) != len(set(identities)):
        raise ValidationClosureError(f"HitCC 文档原子身份重复：{relative_path}")
    return atoms


def candidate_refs(value: dict[str, Any]) -> list[str]:
    """读取历史账本中已经存在的 CAND 语义引用。"""

    refs = value.get("spec_rule_ids", [])
    if not isinstance(refs, list):
        raise ValidationClosureError("历史候选 spec_rule_ids 必须是数组")
    result = sorted({str(item) for item in refs if str(item).startswith("CAND-")})
    unknown = sorted(set(result) - set(SEMANTIC_CANDIDATE_CATALOG))
    if unknown:
        raise ValidationClosureError(f"引用未知语义候选：{unknown}")
    return result


def spec_refs(value: dict[str, Any]) -> list[str]:
    """读取历史账本中已经存在的 SPEC 引用。"""

    refs = value.get("spec_rule_ids", [])
    if not isinstance(refs, list):
        raise ValidationClosureError("历史候选 spec_rule_ids 必须是数组")
    return sorted({str(item) for item in refs if str(item).startswith("SPEC-")})


def classify_text_candidates(text: str, *, include_fullset: bool) -> list[str]:
    """把文本证据映射到既有语义候选，不从文本生成新身份。"""

    matches = {
        identity
        for identity, patterns in COMPILED_TEXT_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }
    if include_fullset and EGRESS_SIGNAL_RE.search(text):
        matches.add("CAND-EP-FULLSET")
    return sorted(matches)


def classify_target_candidates(evidence: dict[str, Any]) -> list[str]:
    """把一个真实 AST 网络调用归入一个或多个语义候选簇。"""

    call = evidence.get("call")
    if not isinstance(call, dict):
        raise ValidationClosureError("目标 AST 调用缺少 call 证据")
    category = str(evidence.get("category", ""))
    callee = ".".join(str(item) for item in call.get("callee_tail", []))
    excerpt = str(call.get("excerpt", ""))
    matches = set(
        classify_text_candidates(" ".join((category, callee, excerpt)), include_fullset=True)
    )
    matches.add("CAND-EP-FULLSET")
    if "count_tokens" in category or "countTokens" in excerpt:
        matches.add("CAND-EP-COUNTTOKENS")
    if "_client." in callee or re.search(
        r"/v1/(?:files|models|messages/batches|skills|agents|sessions|memory_stores|vaults|environments)",
        excerpt,
    ):
        matches.add("CAND-EP-SDK-SURFACE")
    if re.search(r"(?:/api/|/worker/|oauth|frame|remote|telemetry)", excerpt, re.I):
        matches.add("CAND-EP-AUXILIARY")
    return sorted(matches)


def build_validation_closure(
    *,
    workspace_root: Path,
    prior_dispositions_path: Path,
    target_inventory_path: Path,
    sink_containment_path: Path,
    source_2188_path: Path,
    source_2188_root: Path,
    hitcc_path: Path,
    capture_index_path: Path,
    output_root: Path,
    producer_path: Path = Path(__file__),
) -> dict[str, Any]:
    """生成 v3 dispositions、发现清单和多对一语义候选。"""

    if output_root.exists():
        raise ValidationClosureError("output-root 必须不存在，禁止覆盖")
    try:
        output_root.resolve().relative_to(workspace_root.resolve())
    except ValueError as error:
        raise ValidationClosureError("output-root 必须位于工作区内") from error

    prior = load_json(prior_dispositions_path, "上一版 dispositions")
    target_inventory = load_json(target_inventory_path, "目标 sink inventory")
    containment = load_json(sink_containment_path, "sink containment")
    source = load_json(source_2188_path, "2.1.88 coverage")
    hitcc = load_json(hitcc_path, "HitCC coverage")
    capture = load_json(capture_index_path, "capture index")
    target_version = prior.get("target_version")
    if prior.get("schema_version") != SCHEMA_PRIOR:
        raise ValidationClosureError("上一版 dispositions 必须是 v1 显式处置结果")
    if not isinstance(target_version, str) or not target_version:
        raise ValidationClosureError("上一版 dispositions 缺少目标版本")
    versions = {
        target_version,
        target_inventory.get("target_version"),
        containment.get("target_version"),
        capture.get("target_version"),
    }
    if versions != {target_version}:
        raise ValidationClosureError(
            f"目标版本绑定不一致：{sorted(str(value) for value in versions)}"
        )
    if capture.get("result") != "passed":
        raise ValidationClosureError("capture index 尚未通过")
    completeness = containment.get("completeness")
    if (
        containment.get("schema_version")
        != "claude-code-fw-e-sink-containment-evidence/v1"
        or not isinstance(completeness, dict)
        or completeness.get("result") != "passed"
        or completeness.get("unmatched_sink_ids") != []
    ):
        raise ValidationClosureError("sink containment 未形成无遗漏闭集")

    target_rows = indexed(target_inventory.get("sinks"), "sink_id", "target inventory")
    containment_rows = indexed(containment.get("evidence"), "sink_id", "containment evidence")
    if set(target_rows) != set(containment_rows):
        raise ValidationClosureError("target inventory 与 containment 身份集合不一致")
    prior_target = indexed(prior.get("target_sinks"), "sink_id", "prior target_sinks")
    if set(prior_target) != set(target_rows):
        raise ValidationClosureError("上一版 target dispositions 与目标分母不一致")

    source_rows = indexed(source.get("rules"), "source_rule_id", "2.1.88 rules")
    prior_source = indexed(
        prior.get("historical_source_candidates"),
        "source_rule_id",
        "prior historical_source_candidates",
    )
    if set(prior_source) != set(source_rows):
        raise ValidationClosureError("上一版 2.1.88 dispositions 与分母不一致")
    clue_rows = indexed(hitcc.get("clues"), "clue_id", "HitCC clues")
    prior_clues = indexed(prior.get("hitcc_clues"), "clue_id", "prior hitcc_clues")
    if set(prior_clues) != set(clue_rows):
        raise ValidationClosureError("上一版 HitCC clue dispositions 与分母不一致")
    all_documents = indexed(
        hitcc.get("document_inventory"), "path", "HitCC document inventory"
    )
    expected_documents = {
        path: row
        for path, row in all_documents.items()
        if row.get("disposition") == "clue_source"
    }
    prior_documents = indexed(
        prior.get("hitcc_documents"), "path", "prior hitcc_documents"
    )
    if set(prior_documents) != set(expected_documents):
        raise ValidationClosureError("上一版 HitCC document dispositions 与分母不一致")

    output_root.mkdir(parents=True, mode=0o700)
    source_to_sink_path = output_root / "source-to-sink.json"
    document_atoms_path = output_root / "document-atoms.json"
    discovery_path = output_root / "discovery-inventory.json"
    candidates_path = output_root / "semantic-candidates.json"
    dispositions_path = output_root / "dispositions.json"
    review_path = output_root / "closure-review.json"
    source_to_sink_relative = future_workspace_relative(
        workspace_root, source_to_sink_path, "source-to-sink 输出"
    )
    document_atoms_relative = future_workspace_relative(
        workspace_root, document_atoms_path, "document-atoms 输出"
    )
    discovery_relative = future_workspace_relative(
        workspace_root, discovery_path, "discovery inventory 输出"
    )
    target_inventory_relative = workspace_relative(
        workspace_root, target_inventory_path, "目标 sink inventory"
    )
    containment_relative = workspace_relative(
        workspace_root, sink_containment_path, "sink containment"
    )
    source_relative = workspace_relative(
        workspace_root, source_2188_path, "2.1.88 coverage"
    )
    hitcc_relative = workspace_relative(
        workspace_root, hitcc_path, "HitCC coverage"
    )

    discovery_rows: dict[str, dict[str, Any]] = {}
    candidate_sources: dict[str, set[str]] = {}
    candidate_evidence: dict[str, set[str]] = {}
    candidate_observed: set[str] = set()

    def link_candidates(
        identities: list[str],
        source_id: str,
        evidence_paths: list[str],
        *,
        observed: bool,
    ) -> None:
        for identity in identities:
            if identity not in SEMANTIC_CANDIDATE_CATALOG:
                raise ValidationClosureError(f"未知语义候选：{identity}")
            candidate_sources.setdefault(identity, set()).add(source_id)
            candidate_evidence.setdefault(identity, set()).update(evidence_paths)
            if observed:
                candidate_observed.add(identity)

    def add_discovery(
        *,
        discovery_id: str,
        source_kind: str,
        proposition: str,
        disposition: str,
        semantic_candidate_ids: list[str],
        spec_ids: list[str],
        evidence_paths: list[str],
        rationale: str,
    ) -> None:
        if discovery_id in discovery_rows:
            raise ValidationClosureError(f"发现项身份重复：{discovery_id}")
        discovery_rows[discovery_id] = {
            "discovery_id": discovery_id,
            "source_kind": source_kind,
            "proposition": proposition,
            "disposition": disposition,
            "semantic_candidate_ids": sorted(set(semantic_candidate_ids)),
            "spec_ids": sorted(set(spec_ids)),
            "evidence_paths": sorted(set(evidence_paths)),
            "rationale": rationale,
        }

    source_to_sink_rows: list[dict[str, Any]] = []
    proven_not_traffic_sink_ids: list[str] = []
    next_target: list[dict[str, Any]] = []
    for sink_id in sorted(prior_target):
        previous = dict(prior_target[sink_id])
        previous["candidate_ids"] = []
        if previous.get("disposition") != "unclassified":
            next_target.append(previous)
            continue
        evidence = containment_rows[sink_id]
        if evidence.get("structural_finding") != "exact_ast_call":
            raise ValidationClosureError(
                f"未分类目标项不是精确 AST 调用，禁止自动处置：{sink_id}"
            )
        call = evidence.get("call")
        if not isinstance(call, dict) or not isinstance(call.get("excerpt"), str):
            raise ValidationClosureError(f"精确 AST 调用缺少调用原文：{sink_id}")
        relevant_literals = call.get("relevant_literals", [])
        environment_keys = call.get("environment_keys", [])
        compact_call = {
            "kind": call.get("kind"),
            "sha256": call.get("sha256"),
            "excerpt": call.get("excerpt"),
            "excerpt_truncated": call.get("excerpt_truncated"),
            "callee_tail": call.get("callee_tail"),
            "argument_shapes": call.get("argument_shapes"),
            "privacy_keys": call.get("privacy_keys"),
            "environment_key_count": len(environment_keys) if isinstance(environment_keys, list) else None,
            "environment_keys_sha256": canonical_sha256(environment_keys),
            "relevant_literal_count": len(relevant_literals) if isinstance(relevant_literals, list) else None,
            "relevant_literals_sha256": canonical_sha256(relevant_literals),
        }
        compact_evidence = {
            "sink_id": sink_id,
            "category": evidence.get("category"),
            "source_start": evidence.get("source_start"),
            "source_end": evidence.get("source_end"),
            "semantic_sha256": evidence.get("semantic_sha256"),
            "owner_symbol": target_rows[sink_id].get("owner_symbol"),
            "structural_finding": evidence.get("structural_finding"),
            "structural_reason": evidence.get("structural_reason"),
            "source_window": evidence.get("source_window"),
            "call": compact_call,
        }
        if sink_id == PROVEN_NOT_TRAFFIC_SINK_ID:
            if (
                call.get("excerpt") != PROVEN_NOT_TRAFFIC_CALL
                or call.get("callee_tail") != ["r", "currentTurn", "files", "get"]
            ):
                raise ValidationClosureError("已证明非网络调用的结构证据发生漂移")
            compact_evidence.update(
                {
                    "disposition": "out_of_scope_proven",
                    "proof": "Map 状态读取，不是 Anthropic resource 发送",
                    "semantic_candidate_ids": [],
                }
            )
            proven_not_traffic_sink_ids.append(sink_id)
            previous.update(
                {
                    "traffic_class": "not_traffic",
                    "disposition": "out_of_scope_proven",
                    "rationale": (
                        "精确调用原文是当前 turn 文件 Map 的状态读取，不是网络发送。"
                    ),
                    "spec_ids": [],
                    "candidate_ids": [],
                    "scenario_ids": [],
                    "evidence_paths": sorted(
                        set(previous.get("evidence_paths", []))
                        | {source_to_sink_relative, containment_relative}
                    ),
                }
            )
            next_target.append(previous)
            source_to_sink_rows.append(compact_evidence)
            continue
        semantic_ids = classify_target_candidates(evidence)
        compact_evidence.update(
            {
                "disposition": "mapped_validation",
                "semantic_candidate_ids": semantic_ids,
            }
        )
        target_evidence = [
            source_to_sink_relative,
            target_inventory_relative,
            containment_relative,
        ]
        link_candidates(semantic_ids, sink_id, target_evidence, observed=True)
        add_discovery(
            discovery_id=sink_id,
            source_kind="target_ast_call",
            proposition=(
                f"目标 {target_version} bundle 存在 {evidence.get('category')} 精确 AST 调用："
                f"{call['excerpt']}"
            ),
            disposition="mapped_semantic_candidate",
            semantic_candidate_ids=semantic_ids,
            spec_ids=[],
            evidence_paths=target_evidence,
            rationale=(
                "只证明调用存在；通过稳定候选身份多对一归并，不生成 SPEC，"
                "也不证明触发条件、流量类别或目标 wire。"
            ),
        )
        previous.update(
            {
                "traffic_class": "unknown",
                "disposition": "mapped_validation",
                "rationale": (
                    "精确 AST 调用已进入发现清单并归并到语义候选；"
                    "候选不是规则，禁止自动增加 RuleLedger。"
                ),
                "spec_ids": [],
                "candidate_ids": semantic_ids,
                "scenario_ids": [],
                "evidence_paths": sorted(
                    set(previous.get("evidence_paths", [])) | set(target_evidence)
                ),
                "migration_decision": "change",
            }
        )
        next_target.append(previous)
        source_to_sink_rows.append(compact_evidence)

    source_to_sink = {
        "schema_version": SCHEMA_SOURCE_TO_SINK,
        "target_version": target_version,
        "input_bindings": {
            "target_inventory": binding(workspace_root, target_inventory_path, "目标 sink inventory"),
            "sink_containment": binding(workspace_root, sink_containment_path, "sink containment"),
        },
        "entry_count": len(source_to_sink_rows),
        "entries": sorted(source_to_sink_rows, key=lambda row: row["sink_id"]),
        "counts": dict(sorted(Counter(row["disposition"] for row in source_to_sink_rows).items())),
        "limitations": [
            "exact_ast_call 只证明目标 bundle 中存在调用，不证明运行触发或 wire 语义。",
            "semantic candidate 不是 SPEC，不进入 RuleLedger 或 production SupportEnvelope。",
        ],
    }
    write_private_json(source_to_sink_path, source_to_sink)

    next_source: list[dict[str, Any]] = []
    for source_id in sorted(prior_source):
        previous = dict(prior_source[source_id])
        previous["candidate_ids"] = []
        if previous.get("disposition") != "unclassified":
            next_source.append(previous)
            continue
        raw = source_rows[source_id]
        source_evidence = [source_relative]
        for location in raw.get("source_paths", []):
            if not isinstance(location, str):
                raise ValidationClosureError(f"2.1.88 source_paths 非法：{source_id}")
            match = SOURCE_LOCATION_RE.fullmatch(location)
            if match is None:
                raise ValidationClosureError(f"2.1.88 源码坐标非法：{source_id}={location}")
            source_path = source_2188_root / match.group(1)
            source_evidence.append(
                workspace_relative(workspace_root, source_path, f"2.1.88 源码 {source_id}")
            )
        semantic_ids = sorted(
            set(candidate_refs(raw)) | set(SOURCE_CANDIDATE_MAP.get(source_id, []))
        )
        if not semantic_ids:
            raise ValidationClosureError(f"2.1.88 未分类命题没有语义归并：{source_id}")
        link_candidates(semantic_ids, source_id, source_evidence, observed=False)
        add_discovery(
            discovery_id=source_id,
            source_kind="historical_source_2_1_88",
            proposition=str(raw.get("proposition")),
            disposition="mapped_semantic_candidate",
            semantic_candidate_ids=semantic_ids,
            spec_ids=spec_refs(raw),
            evidence_paths=source_evidence,
            rationale="历史源码原子命题多对一归并到既有 CAND 身份，不生成目标 SPEC。",
        )
        previous.update(
            {
                "disposition": "mapped_validation",
                "spec_ids": spec_refs(raw),
                "candidate_ids": semantic_ids,
                "rationale": (
                    "历史源码命题已绑定原始源码并归并到语义候选；"
                    "目标 stable 语义仍待证明，且候选禁止生产。"
                ),
                "evidence_paths": sorted(set(source_evidence)),
            }
        )
        next_source.append(previous)

    next_clues: list[dict[str, Any]] = []
    clue_semantic_ids: dict[str, list[str]] = {}
    clue_spec_ids: dict[str, list[str]] = {}
    for clue_id in sorted(clue_rows):
        raw = clue_rows[clue_id]
        clue_semantic_ids[clue_id] = candidate_refs(raw)
        clue_spec_ids[clue_id] = spec_refs(raw)
        previous = dict(prior_clues[clue_id])
        previous["candidate_ids"] = []
        if previous.get("disposition") != "unclassified":
            next_clues.append(previous)
            continue
        source_path_value = raw.get("source_path")
        if not isinstance(source_path_value, str) or not source_path_value:
            raise ValidationClosureError(f"HitCC clue 缺少来源：{clue_id}")
        source_path = workspace_root / source_path_value
        clue_evidence = [
            hitcc_relative,
            workspace_relative(workspace_root, source_path, f"HitCC clue {clue_id}"),
        ]
        semantic_ids = clue_semantic_ids[clue_id]
        specs = clue_spec_ids[clue_id]
        if semantic_ids:
            discovery_disposition = "mapped_semantic_candidate"
            outer_disposition = "mapped_validation"
            rationale = "HitCC 原子线索归并到既有语义候选；候选不是目标规则。"
            link_candidates(semantic_ids, clue_id, clue_evidence, observed=False)
        elif specs:
            discovery_disposition = "mapped_existing_rule"
            outer_disposition = "mapped_historical"
            rationale = "HitCC 有限线索只作为既有 SPEC 的历史支持证据，不新增规则。"
        else:
            semantic_ids = classify_text_candidates(
                str(raw.get("proposition", "")), include_fullset=True
            )
            if not semantic_ids:
                raise ValidationClosureError(f"HitCC 未分类线索没有语义归并：{clue_id}")
            discovery_disposition = "mapped_semantic_candidate"
            outer_disposition = "mapped_validation"
            rationale = "HitCC 线索经固定语义词表归并到既有候选；候选不是目标规则。"
            link_candidates(semantic_ids, clue_id, clue_evidence, observed=False)
        add_discovery(
            discovery_id=clue_id,
            source_kind="hitcc_clue_2_1_197",
            proposition=str(raw.get("proposition")),
            disposition=discovery_disposition,
            semantic_candidate_ids=semantic_ids,
            spec_ids=specs,
            evidence_paths=clue_evidence,
            rationale=rationale,
        )
        previous.update(
            {
                "disposition": outer_disposition,
                "spec_ids": specs,
                "candidate_ids": semantic_ids,
                "rationale": rationale,
                "evidence_paths": sorted(set(clue_evidence)),
            }
        )
        next_clues.append(previous)

    document_records: list[dict[str, Any]] = []
    next_documents: list[dict[str, Any]] = []
    for document_path_value in sorted(prior_documents):
        previous = dict(prior_documents[document_path_value])
        previous["candidate_ids"] = []
        if previous.get("disposition") != "unclassified":
            next_documents.append(previous)
            continue
        raw = expected_documents[document_path_value]
        document_path = workspace_root / document_path_value
        document_relative = workspace_relative(
            workspace_root, document_path, "HitCC clue_source 文档"
        )
        clue_ids = sorted(str(item) for item in raw.get("clue_ids", []) if str(item))
        semantic_ids = sorted(
            {
                candidate_id
                for clue_id in clue_ids
                for candidate_id in clue_semantic_ids.get(clue_id, [])
            }
        )
        specs = sorted(
            {
                spec_id
                for clue_id in clue_ids
                for spec_id in clue_spec_ids.get(clue_id, [])
            }
        )
        atoms: list[dict[str, Any]] = []
        atom_disposition_counts: Counter[str] = Counter()
        if raw.get("mapping_status") == "unmapped":
            if clue_ids:
                raise ValidationClosureError(
                    f"HitCC unmapped 文档不得声明 clue_ids：{document_path_value}"
                )
            atoms = parse_markdown_list_atoms(document_path, document_relative)
            if not atoms:
                raise ValidationClosureError(
                    f"HitCC unmapped 文档没有可原子化列表项：{document_path_value}"
                )
            for atom in atoms:
                atom_ids = classify_text_candidates(
                    " ".join((atom["heading"], atom["text"])),
                    include_fullset=True,
                )
                if atom_ids:
                    atom_disposition = "mapped_semantic_candidate"
                    atom_rationale = (
                        "文档列表项命中固定出站语义词表，归并到既有候选；"
                        "原文仍以独立 discovery_id 保留。"
                    )
                    semantic_ids = sorted(set(semantic_ids) | set(atom_ids))
                    link_candidates(
                        atom_ids,
                        atom["atom_id"],
                        [document_atoms_relative, document_relative],
                        observed=False,
                    )
                else:
                    atom_disposition = "catalogued_context"
                    atom_rationale = (
                        "该列表项未命中固定出站语义词表，仅登记为可追溯历史上下文；"
                        "这不是范围外证明，不生成规则，后续换版仍可重新分类。"
                    )
                atom_disposition_counts[atom_disposition] += 1
                atom["disposition"] = atom_disposition
                atom["semantic_candidate_ids"] = atom_ids
                add_discovery(
                    discovery_id=atom["atom_id"],
                    source_kind="hitcc_document_atom_2_1_197",
                    proposition=atom["text"],
                    disposition=atom_disposition,
                    semantic_candidate_ids=atom_ids,
                    spec_ids=[],
                    evidence_paths=[document_atoms_relative, document_relative],
                    rationale=atom_rationale,
                )
        elif raw.get("mapping_status") != "mapped":
            raise ValidationClosureError(
                f"HitCC 文档 mapping_status 非法：{document_path_value}"
            )
        if semantic_ids:
            outer_disposition = "mapped_validation"
            rationale = (
                "文档通过 clue 或逐项列表原子归并到语义候选；"
                "文档及每个原子均保留反向来源，不生成 SPEC。"
            )
        elif specs:
            outer_disposition = "mapped_historical"
            rationale = "文档只作为既有 SPEC 的历史支持证据，不新增规则。"
        else:
            outer_disposition = "catalogued_context"
            rationale = (
                "文档列表原子均已登记为可追溯上下文；"
                "不据此主张范围外或目标语义已穷尽。"
            )
        document_records.append(
            {
                "path": document_relative,
                "sha256": sha256_file(document_path),
                "mapping_status": raw.get("mapping_status"),
                "clue_ids": clue_ids,
                "atom_count": len(atoms),
                "atom_disposition_counts": dict(sorted(atom_disposition_counts.items())),
                "semantic_candidate_ids": semantic_ids,
                "spec_ids": specs,
                "atoms": atoms,
            }
        )
        previous.update(
            {
                "disposition": outer_disposition,
                "spec_ids": specs,
                "candidate_ids": semantic_ids,
                "rationale": rationale,
                "evidence_paths": sorted(
                    set(previous.get("evidence_paths", []))
                    | {document_atoms_relative, discovery_relative, document_relative}
                ),
            }
        )
        next_documents.append(previous)

    document_atoms = {
        "schema_version": SCHEMA_DOCUMENT_ATOMS,
        "source_version": hitcc.get("source_version"),
        "target_version": target_version,
        "input_binding": binding(workspace_root, hitcc_path, "HitCC coverage"),
        "document_count": len(document_records),
        "atom_count": sum(row["atom_count"] for row in document_records),
        "documents": document_records,
        "extraction_policy": {
            "unit": "markdown_list_item_outside_fenced_code",
            "continuations": "indented_lines_until_blank_heading_fence_or_next_item",
            "truncation": "forbidden",
            "semantic_status": "discovery_evidence_only",
        },
    }
    write_private_json(document_atoms_path, document_atoms)

    ordered_discovery = [discovery_rows[key] for key in sorted(discovery_rows)]
    discovery_ids = set(discovery_rows)
    for candidate_id, source_ids in candidate_sources.items():
        unknown_sources = sorted(source_ids - discovery_ids)
        if unknown_sources:
            raise ValidationClosureError(
                "语义候选 source_ids 没有对应发现项："
                f"{candidate_id}={unknown_sources}"
            )
    for discovery_id, discovery in discovery_rows.items():
        for candidate_id in discovery["semantic_candidate_ids"]:
            if discovery_id not in candidate_sources.get(candidate_id, set()):
                raise ValidationClosureError(
                    "发现项与语义候选 source_ids 未双向绑定："
                    f"{discovery_id}={candidate_id}"
                )
    discovery_inventory = {
        "schema_version": SCHEMA_DISCOVERY,
        "target_version": target_version,
        "item_count": len(ordered_discovery),
        "counts_by_source_kind": dict(
            sorted(Counter(row["source_kind"] for row in ordered_discovery).items())
        ),
        "counts_by_disposition": dict(
            sorted(Counter(row["disposition"] for row in ordered_discovery).items())
        ),
        "items": ordered_discovery,
        "rule_generation": "forbidden",
    }
    write_private_json(discovery_path, discovery_inventory)

    ordered_candidates: list[dict[str, Any]] = []
    for identity in sorted(candidate_sources):
        ordered_candidates.append(
            {
                "id": identity,
                **SEMANTIC_CANDIDATE_CATALOG[identity],
                "evidence_level": "observed" if identity in candidate_observed else "blocked",
                "evidence_paths": sorted(candidate_evidence[identity]),
                "source_ids": sorted(candidate_sources[identity]),
            }
        )
    semantic_candidates = {
        "schema_version": SCHEMA_CANDIDATES,
        "target_version": target_version,
        "input_bindings": {
            "producer": binding(workspace_root, producer_path, "semantic closure producer"),
            "prior_dispositions": binding(workspace_root, prior_dispositions_path, "上一版 dispositions"),
            "target_inventory": binding(workspace_root, target_inventory_path, "目标 sink inventory"),
            "sink_containment": binding(workspace_root, sink_containment_path, "sink containment"),
            "source_2_1_88": binding(workspace_root, source_2188_path, "2.1.88 coverage"),
            "hitcc_2_1_197": binding(workspace_root, hitcc_path, "HitCC coverage"),
            "capture_index": binding(workspace_root, capture_index_path, "capture index"),
            "discovery_inventory": binding(workspace_root, discovery_path, "discovery inventory"),
        },
        "candidate_count": len(ordered_candidates),
        "counts_by_kind": dict(
            sorted(Counter(row["candidate_kind"] for row in ordered_candidates).items())
        ),
        "counts_by_evidence_level": dict(
            sorted(Counter(row["evidence_level"] for row in ordered_candidates).items())
        ),
        "candidates": ordered_candidates,
        "rule_ledger_membership": "denied",
        "production_eligibility": "denied",
    }
    write_private_json(candidates_path, semantic_candidates)

    dispositions = {
        "schema_version": SCHEMA_OUTPUT,
        "target_version": target_version,
        "target_sinks": sorted(next_target, key=lambda row: row["sink_id"]),
        "historical_source_candidates": sorted(next_source, key=lambda row: row["source_rule_id"]),
        "hitcc_clues": sorted(next_clues, key=lambda row: row["clue_id"]),
        "hitcc_documents": sorted(next_documents, key=lambda row: row["path"]),
        "runtime_observations": prior.get("runtime_observations"),
        "semantic_candidates": ordered_candidates,
        "discovery_inventory": {
            "path": discovery_relative,
            "sha256": sha256_file(discovery_path),
            "item_count": len(ordered_discovery),
        },
    }
    if not isinstance(dispositions["runtime_observations"], list):
        raise ValidationClosureError("上一版 runtime observations 非法")
    remaining = {
        key: sum(row.get("disposition") == "unclassified" for row in dispositions[key])
        for key in (
            "target_sinks",
            "historical_source_candidates",
            "hitcc_clues",
            "hitcc_documents",
            "runtime_observations",
        )
    }
    if any(remaining.values()):
        raise ValidationClosureError(f"semantic closure 仍含 unclassified：{remaining}")
    write_private_json(dispositions_path, dispositions)

    prior_unclassified = {
        key: sum(row.get("disposition") == "unclassified" for row in prior[key])
        for key in (
            "target_sinks",
            "historical_source_candidates",
            "hitcc_clues",
            "hitcc_documents",
            "runtime_observations",
        )
    }
    review = {
        "schema_version": SCHEMA_REVIEW,
        "target_version": target_version,
        "input_bindings": semantic_candidates["input_bindings"],
        "output_bindings": {
            "source_to_sink": binding(workspace_root, source_to_sink_path, "source-to-sink 输出"),
            "document_atoms": binding(workspace_root, document_atoms_path, "document-atoms 输出"),
            "discovery_inventory": binding(workspace_root, discovery_path, "discovery inventory 输出"),
            "semantic_candidates": binding(workspace_root, candidates_path, "semantic candidates 输出"),
            "dispositions": binding(workspace_root, dispositions_path, "dispositions 输出"),
        },
        "prior_unclassified_counts": prior_unclassified,
        "prior_unclassified_total": sum(prior_unclassified.values()),
        "final_unclassified_counts": remaining,
        "final_unclassified_total": sum(remaining.values()),
        "discovery_item_count": len(ordered_discovery),
        "discovery_counts_by_source_kind": discovery_inventory["counts_by_source_kind"],
        "discovery_counts_by_disposition": discovery_inventory["counts_by_disposition"],
        "semantic_candidate_count": len(ordered_candidates),
        "semantic_candidate_counts_by_kind": semantic_candidates["counts_by_kind"],
        "semantic_candidate_counts_by_evidence_level": semantic_candidates["counts_by_evidence_level"],
        "generated_rule_count": 0,
        "proven_not_traffic_sink_ids": sorted(proven_not_traffic_sink_ids),
        "production_eligibility": "denied",
        "result": "passed",
    }
    write_private_json(review_path, review)
    return review


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--prior-dispositions", required=True, type=Path)
    parser.add_argument("--target-inventory", required=True, type=Path)
    parser.add_argument("--sink-containment", required=True, type=Path)
    parser.add_argument("--source-2188", required=True, type=Path)
    parser.add_argument("--source-2188-root", required=True, type=Path)
    parser.add_argument("--hitcc", required=True, type=Path)
    parser.add_argument("--capture-index", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main() -> int:
    """运行发现清单与语义候选闭合。"""

    arguments = build_parser().parse_args()
    try:
        review = build_validation_closure(
            workspace_root=arguments.workspace_root,
            prior_dispositions_path=arguments.prior_dispositions,
            target_inventory_path=arguments.target_inventory,
            sink_containment_path=arguments.sink_containment,
            source_2188_path=arguments.source_2188,
            source_2188_root=arguments.source_2188_root,
            hitcc_path=arguments.hitcc,
            capture_index_path=arguments.capture_index,
            output_root=arguments.output_root,
        )
    except (ValidationClosureError, OSError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "FW-E 发现清单与语义候选已闭合："
        f"discoveries={review['discovery_item_count']} "
        f"semantic_candidates={review['semantic_candidate_count']} "
        f"generated_rules={review['generated_rule_count']} "
        f"unclassified={review['final_unclassified_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
