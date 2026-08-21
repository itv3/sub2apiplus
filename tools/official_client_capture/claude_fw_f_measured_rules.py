#!/usr/bin/env python3
"""从 Claude Code 2.1.226 的真实 R/M 样本生成实测规则台账。

本工具只接受 FW-E 已封存的目标版本 relay 证据。每条输出规则都必须执行对应
断言并绑定原始请求字节；没有断言、只有旧版本证据或只有静态线索的命题不能
进入台账。TLS ClientHello 指纹和 ALPN offer 未被当前证据直接观察，因此不会
生成相关规则。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_capture.claude_fw_f_profile import (  # noqa: E402
    ProfileBuildError,
    parse_http_stream,
)
from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


POLICY_SCHEMAS = {
    "claude-code-fw-f-discovery-clearance-policy/v2",
    "claude-code-fw-f-discovery-clearance-policy/v3",
}
PROFILE_POLICY_SCHEMAS = {
    "claude-code-fw-f-profile-policy/v3",
    "claude-code-fw-f-profile-policy/v4",
    "claude-code-fw-f-profile-policy/v5",
}
LEDGER_SCHEMA = "claude-code-fw-f-measured-rule-ledger/v2"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")
ATTRIBUTION_RE = re.compile(
    r"^x-anthropic-billing-header: "
    r"cc_version=(2\.1\.226\.[0-9a-f]{3}); "
    r"cc_entrypoint=(sdk-cli); cch=([0-9a-f]{5});"
    r"(?: cc_prev_req=(req_[A-Za-z0-9]+);)?"
    r"(?: cc_is_subagent=(true);)?$"
)

INFERENCE_HEADER_ORDER = [
    "Accept",
    "Authorization",
    "Content-Type",
    "User-Agent",
    "X-Claude-Code-Session-Id",
    "X-Stainless-Arch",
    "X-Stainless-Lang",
    "X-Stainless-OS",
    "X-Stainless-Package-Version",
    "X-Stainless-Retry-Count",
    "X-Stainless-Runtime",
    "X-Stainless-Runtime-Version",
    "X-Stainless-Timeout",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "anthropic-version",
    "x-app",
    "x-client-request-id",
    "Connection",
    "Host",
    "Accept-Encoding",
    "Content-Length",
]
LIFECYCLE_HEADERS = [
    ("Connection", "keep-alive"),
    ("User-Agent", "Bun/1.4.0"),
    ("Accept", "*/*"),
    ("Host", "api.anthropic.com"),
    ("Accept-Encoding", "gzip, deflate, br, zstd"),
]
BODY_KEY_ORDER = [
    "model",
    "messages",
    "system",
    "tools",
    "metadata",
    "max_tokens",
    "thinking",
    "context_management",
    "output_config",
    "stream",
]
BETA_BASE_VALUE = (
    "claude-code-20250219,oauth-2025-04-20,"
    "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "mid-conversation-system-2026-04-07,effort-2025-11-24"
)
BETA_MAIN_VALUE = BETA_BASE_VALUE + ",extended-cache-ttl-2025-04-11"
PRODUCT_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
TOOLS_DIGEST_BY_SCENARIO = {
    "a1": "39bc7d464750b03ff37224ec16eb08869e3d7dc306d177615752ce058a77446b",
    "s1": "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
    "s2": "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
    "s4": "f031209dabe4dfbd7f7f131290ec0741700ca7ec907ecf1db93a7cc659000271",
}
COMMON_APPLICABILITY = [
    "authentication=claude.ai-oauth",
    "binary_sha256=4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555",
    "platform=linux/amd64",
    "privacy=essential-traffic-no-telemetry",
    "provider=firstParty",
    "version=2.1.226",
]
APPLICABILITY = [*COMMON_APPLICABILITY, "entrypoint=sdk-cli", "model=claude-sonnet-5"]
TUI_PROFILE_APPLICABILITY = [*COMMON_APPLICABILITY, "entrypoint=cli"]
TUI_TITLE_APPLICABILITY = [
    *COMMON_APPLICABILITY,
    "entrypoint=cli",
    "model=claude-haiku-4-5-20251001",
]
TUI_MAIN_APPLICABILITY = [*COMMON_APPLICABILITY, "entrypoint=cli", "model=claude-sonnet-5"]
MIXED_ENTRYPOINT_APPLICABILITY = [
    *COMMON_APPLICABILITY,
    "entrypoint=cli|sdk-cli",
    "model=claude-haiku-4-5|claude-haiku-4-5-20251001|claude-sonnet-5",
]
FALLBACK_APPLICABILITY = [
    *COMMON_APPLICABILITY,
    "entrypoint=sdk-cli",
    "model-transition=claude-sonnet-5->claude-haiku-4-5",
]

# 规则必须绑定它实际约束的物理出站。多数规则只约束 messages 推理；生命周期、
# 设置和账号画像端点使用独立 egress identity。跨端点规则显式绑定全部参与端点，
# 禁止为了凑 SupportEnvelope 分母而把它们伪装成 messages 规则。
DEFAULT_EGRESS_ID = "egress-claude-messages-inference"
RULE_EGRESS_IDS: dict[str, tuple[str, ...]] = {
    "SPEC-EP-002": ("egress-claude-lifecycle-hello",),
    "SPEC-EP-005": ("egress-claude-policy-limits",),
    "SPEC-EP-006": ("egress-claude-settings",),
    "SPEC-EP-007": ("egress-claude-oauth-profile",),
    "SPEC-EP-008": (
        "egress-claude-lifecycle-hello",
        "egress-claude-messages-inference",
        "egress-claude-oauth-profile",
        "egress-claude-policy-limits",
        "egress-claude-settings",
    ),
    "SPEC-PROTO-001": (
        "egress-claude-lifecycle-hello",
        "egress-claude-messages-inference",
    ),
    "SPEC-TLS-003": (
        "egress-claude-lifecycle-hello",
        "egress-claude-messages-inference",
    ),
}

RULE_DEFINITIONS: dict[str, dict[str, str]] = {
    "SPEC-TLS-003": {"domain": "tls", "claim": "目标连接在 relay 握手元数据中使用 SNI api.anthropic.com。", "scope": "8 个承载已选请求的真实 TLS 连接"},
    "SPEC-PROTO-001": {"domain": "protocol", "claim": "推理与生命周期请求的应用层请求线均为 HTTP/1.1。", "scope": "8 条推理请求与 4 条生命周期请求"},
    "SPEC-EP-001": {"domain": "endpoint", "claim": "推理请求的 request-target 恰为 /v1/messages?beta=true。", "scope": "8 条推理请求"},
    "SPEC-EP-002": {"domain": "endpoint", "claim": "每次运行先通过独立连接发送无 Body 的 HEAD /api/hello，并使用固定五项 Header。", "scope": "4 次运行的生命周期请求"},
    "SPEC-EP-003": {"domain": "endpoint", "claim": "推理请求的方法恰为 POST。", "scope": "8 条推理请求"},
    "SPEC-EP-004": {"domain": "endpoint", "claim": "推理请求的 Host 恰为 api.anthropic.com。", "scope": "8 条推理请求"},
    "SPEC-HDR-001": {"domain": "header", "claim": "推理请求按实测大小写和相对顺序发送 22 项基础 Header；子代理只在 x-app 与 x-client-request-id 之间插入 agent-id。", "scope": "8 条推理请求"},
    "SPEC-HDR-002": {"domain": "header", "claim": "推理请求发送 User-Agent: claude-cli/2.1.226 (external, sdk-cli)。", "scope": "8 条推理请求"},
    "SPEC-HDR-003": {"domain": "header", "claim": "主请求发送实测的 9 项 anthropic-beta 有序序列；一级子代理省略末尾 extended-cache-ttl。", "scope": "7 条主请求与 1 条子代理请求"},
    "SPEC-HDR-004": {"domain": "header", "claim": "推理请求发送 anthropic-version: 2023-06-01。", "scope": "8 条推理请求"},
    "SPEC-HDR-005": {"domain": "header", "claim": "推理请求发送 Accept-Encoding: gzip, deflate, br, zstd。", "scope": "8 条推理请求"},
    "SPEC-HDR-006": {"domain": "header", "claim": "Linux/amd64 样本发送实测的 Stainless 架构、语言、系统、SDK、重试、运行时和超时向量。", "scope": "8 条推理请求"},
    "SPEC-HDR-007": {"domain": "header", "claim": "sdk-cli 前台推理请求发送 dangerous-direct-browser-access=true 与 x-app=cli。", "scope": "8 条推理请求"},
    "SPEC-HDR-012": {"domain": "header", "claim": "每条推理请求的 x-client-request-id 是 UUID，且 8 条样本内不复用。", "scope": "8 条推理请求"},
    "SPEC-HDR-013": {"domain": "header", "claim": "同一多请求运行复用同一个 X-Claude-Code-Session-Id。", "scope": "a1、s2、s4 三个多请求运行"},
    "SPEC-HDR-014": {"domain": "header", "claim": "一级子代理请求携带 17 位小写十六进制 x-claude-code-agent-id，位置在 x-app 之后；同场景主请求省略。", "scope": "1 条一级子代理正例与 7 条主请求负例"},
    "SPEC-HDR-015": {"domain": "header", "claim": "一级子代理请求复用对应主请求的 X-Claude-Code-Session-Id。", "scope": "a1 的子代理正例与对应主请求负例"},
    "SPEC-HDR-044": {"domain": "header", "claim": "Content-Length 等于实际序列化 JSON Body 的字节数。", "scope": "8 条推理请求"},
    "SPEC-AUTH-002": {"domain": "authentication", "claim": "firstParty OAuth 推理请求发送 Bearer Authorization；证据中的 token 已等长脱敏。", "scope": "8 条推理请求"},
    "SPEC-BODY-001": {"domain": "body", "claim": "推理 Body 顶层键按 model、messages、system、tools、metadata、max_tokens、thinking、context_management、output_config、stream 排列。", "scope": "8 条推理请求"},
    "SPEC-BODY-002": {"domain": "body", "claim": "metadata 仅含 user_id；其内嵌 JSON 恰含 device_id、account_uuid、session_id，且 session_id 等于会话 Header。", "scope": "8 条推理请求"},
    "SPEC-BODY-003": {"domain": "body", "claim": "主请求和子代理分别使用实测的四段/三段 system 结构、产品身份块与 cache_control 形态。", "scope": "7 条主请求与 1 条子代理请求"},
    "SPEC-BODY-004": {"domain": "body", "claim": "首轮主请求、续轮主请求和子代理请求分别使用实测的 messages 角色序列。", "scope": "4 条首轮主请求、3 条续轮主请求与 1 条子代理请求"},
    "SPEC-BODY-005": {"domain": "body", "claim": "tools 字段始终存在；无工具场景为 []，Agent/Bash 场景发送对应实测 JSON Schema。", "scope": "8 条推理请求"},
    "SPEC-BODY-007": {"domain": "body", "claim": "system[0].text 以 x-anthropic-billing-header: 开头并承载 attribution。", "scope": "8 条推理请求"},
    "SPEC-BODY-008": {"domain": "body", "claim": "当前具名样本的 model 恰为 claude-sonnet-5。", "scope": "8 条推理请求"},
    "SPEC-BODY-009": {"domain": "body", "claim": "当前具名样本的 max_tokens 恰为整数 64000。", "scope": "8 条推理请求"},
    "SPEC-BODY-010": {"domain": "body", "claim": "adaptive thinking 为缺省 display、summarized 或 omitted 三态；display 存在时字段顺序恰为 type、display。", "scope": "8 条基线推理请求及 2 条 thinking.display 条件请求"},
    "SPEC-BODY-011": {"domain": "body", "claim": "当前具名样本发送实测 clear_thinking_20251015 context_management。", "scope": "8 条推理请求"},
    "SPEC-BODY-012": {"domain": "body", "claim": "当前具名样本的 output_config 恰为 {\"effort\":\"high\"}。", "scope": "8 条推理请求"},
    "SPEC-BODY-013": {"domain": "body", "claim": "当前具名样本的 stream 恰为 JSON 布尔值 true。", "scope": "8 条推理请求"},
    "SPEC-BODY-014": {"domain": "body", "claim": "attribution 中 cc_version 匹配 2.1.226.<3 位小写十六进制>。", "scope": "8 条推理请求"},
    "SPEC-BODY-015": {"domain": "body", "claim": "attribution 中 cc_entrypoint 恰为 sdk-cli。", "scope": "8 条推理请求"},
    "SPEC-BODY-016": {"domain": "body", "claim": "attribution 中 cch 匹配 5 位小写十六进制。", "scope": "8 条推理请求"},
    "SPEC-BODY-018": {"domain": "body", "claim": "仅续轮主请求在 attribution 中携带 cc_prev_req=req_<标识>。", "scope": "3 条续轮正例与 5 条非续轮负例"},
    "SPEC-BODY-019": {"domain": "body", "claim": "仅子代理请求在 attribution 中携带 cc_is_subagent=true。", "scope": "1 条子代理正例与 7 条非子代理负例"},
    "SPEC-CONN-019": {"domain": "connection", "claim": "a1、s2、s4 的多轮推理请求复用同一条有效 HTTP/1.1 连接。", "scope": "3 个多请求运行"},
}

# v3 证据补齐了旧四场景没有覆盖的条件 Header、Body、真实 TUI、辅助端点、
# 会话恢复和隔离故障矩阵。这里按可独立断言的语义归并，不按探针数量生成规则。
RULE_DEFINITIONS.update(
    {
        "SPEC-EP-005": {"domain": "endpoint", "claim": "sdk-cli 启动阶段发送 GET /api/claude_code/policy_limits；请求使用 first-party OAuth、oauth beta、claude-code/2.1.226 UA 和实测七项基础 Header。", "scope": "除真实 TUI 外的 2.1.226 v3 官方运行"},
        "SPEC-EP-006": {"domain": "endpoint", "claim": "sdk-cli 启动阶段发送 GET /api/claude_code/settings；请求在 policy_limits 基础上增加 Cache-Control:no-cache 与 Pragma:no-cache。", "scope": "除真实 TUI 外的 2.1.226 v3 官方运行"},
        "SPEC-EP-007": {"domain": "endpoint", "claim": "真实 TUI 启动阶段发送 GET /api/oauth/profile，使用 axios/1.15.2 UA、JSON Content-Type、OAuth Authorization 与实测八项 Header。", "scope": "v3-tui 的 cli 入口"},
        "SPEC-EP-008": {"domain": "endpoint", "claim": "sdk-cli 每次调用的 essential 请求序列为 hello→policy/settings→零或多个 messages；真实 TUI 为 hello→oauth/profile→Haiku 标题→Sonnet 主推理，policy 与 settings 不规定彼此先后。", "scope": "34 个真实上游探针与 20 个隔离故障探针"},
        "SPEC-HDR-009": {"domain": "header", "claim": "additional-protection 条件成立时插入 x-anthropic-additional-protection:true，位置在 anthropic-version 与 x-app 之间；条件不成立时省略。", "scope": "v3-additional-protection、v3-header-combination 与基线负例"},
        "SPEC-HDR-016": {"domain": "header", "claim": "client-app 条件成立时发送 x-client-app；未设置时省略。", "scope": "v3-client-app、v3-header-combination 与基线负例"},
        "SPEC-HDR-017": {"domain": "header", "claim": "remote-container 条件成立时发送 x-claude-remote-container-id；未设置时省略。", "scope": "v3-remote-container、v3-header-combination 与基线负例"},
        "SPEC-HDR-018": {"domain": "header", "claim": "remote-session 条件成立时发送 x-claude-remote-session-id；未设置时省略。", "scope": "v3-remote-session、v3-header-combination 与基线负例"},
        "SPEC-HDR-021": {"domain": "header", "claim": "x-client-app 的值逐字节等于受控 client-app 输入。", "scope": "v3-client-app 与 v3-header-combination"},
        "SPEC-HDR-022": {"domain": "header", "claim": "client-app 同时生成 x-client-app 与 User-Agent 的 client-app/<值> 后缀，两处来自同一受控值。", "scope": "v3-client-app、v3-header-combination 与基线负例"},
        "SPEC-HDR-023": {"domain": "header", "claim": "x-claude-remote-container-id 的值逐字节等于受控 container ID。", "scope": "v3-remote-container 与 v3-header-combination"},
        "SPEC-HDR-024": {"domain": "header", "claim": "x-claude-remote-session-id 的值逐字节等于受控 remote session ID。", "scope": "v3-remote-session 与 v3-header-combination"},
        "SPEC-HDR-026": {"domain": "header", "claim": "多条件同时成立时，additional-protection、x-app、remote-container、remote-session、client-app、request-id 按实测顺序合并。", "scope": "v3-header-combination"},
        "SPEC-HDR-029": {"domain": "header", "claim": "ANTHROPIC_CUSTOM_HEADERS 按行解析，在首个冒号处分割并修剪名称和值；空行和无冒号行忽略，值内后续冒号保留。", "scope": "v3-custom-header-grammar"},
        "SPEC-HDR-030": {"domain": "header", "claim": "空自定义 Header 名在发送 messages 前本地 fail-close，完整 relay 中 messages 数为零。", "scope": "v3-custom-header-invalid-name"},
        "SPEC-HDR-031": {"domain": "header", "claim": "自定义 x-client-request-id 不能覆盖官方生成的 UUID。", "scope": "v3-custom-header-grammar"},
        "SPEC-HDR-032": {"domain": "header", "claim": "获准自定义 Header 保持输入顺序，插入 X-Claude-Code-Session-Id 与 X-Stainless-Arch 之间。", "scope": "v3-custom-header-grammar"},
        "SPEC-HDR-042": {"domain": "header", "claim": "agent-sdk 条件成立时 User-Agent 追加 agent-sdk/<受控版本> 段；基线不追加。", "scope": "v3-agent-sdk、v3-header-combination 与基线负例"},
        "SPEC-HDR-043": {"domain": "header", "claim": "workload 条件成立时 User-Agent 追加 workload/<受控值> 段；基线不追加。", "scope": "v3-workload、v3-header-combination 与基线负例"},
        "SPEC-BETA-008": {"domain": "beta", "claim": "ANTHROPIC_BETAS 按逗号拆分、修剪并丢弃空项后插入官方 effort/extended-cache 之前；与官方项及自身重复的值均原样保留，不去重。", "scope": "v3-beta-deduplicate 与基线"},
        "SPEC-BODY-017": {"domain": "body", "claim": "CLAUDE_CODE_ATTRIBUTION_HEADER=false 时移除首个 billing attribution system block，其余 system block 前移且内容保持。", "scope": "v3-attribution-disabled 与基线"},
        "SPEC-BODY-032": {"domain": "body", "claim": "non-stream fallback 省略 stream 顶层键、把 X-Stainless-Timeout 从 600 改为 300，重新生成 request-id 与 attribution cch；其余 Body 语义与会话身份保持。", "scope": "三个产生 non-stream fallback 的隔离故障运行"},
        "SPEC-BODY-039": {"domain": "body", "claim": "workload 条件成立时 billing attribution 在 cch 后追加 cc_workload=<受控值>;，未设置时省略。", "scope": "v3-workload、v3-header-combination 与基线负例"},
        "SPEC-BODY-040": {"domain": "body", "claim": "CLAUDE_CODE_EXTRA_BODY.max_tokens 与 CLAUDE_CODE_MAX_OUTPUT_TOKENS 均把 max_tokens 从 64000 覆盖为整数 2048，其他基础字段保持。", "scope": "v3-extra-body、v3-max-output-tokens 与基线"},
        "SPEC-BODY-041": {"domain": "body", "claim": "CLAUDE_CODE_DISABLE_THINKING=1 同时省略 thinking 与 context_management，顶层其余字段保持顺序。", "scope": "v3-thinking-disabled 与基线"},
        "SPEC-BODY-042": {"domain": "body", "claim": "在当前 Sonnet 5 范围，CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1 不改变 thinking、context_management 及其余非动态 Body 语义。", "scope": "v3-adaptive-thinking-disabled 与基线"},
        "SPEC-BODY-043": {"domain": "body", "claim": "启用请求 gzip 时在 request-id 后插入 Content-Encoding:gzip；Content-Length 等于 gzip wire 字节数，且解压后是完整可解析的目标 JSON 结构。", "scope": "v3-gzip-request"},
        "SPEC-BODY-044": {"domain": "body", "claim": "--system-prompt 使用 attribution、产品身份和自定义文本三个 system block，后两块使用 1h ephemeral cache_control。", "scope": "v3-custom-system"},
        "SPEC-BODY-045": {"domain": "body", "claim": "--append-system-prompt 生成四段 system：attribution、CLI-within-SDK 产品身份、核心提示和动态提示；追加文本放入最后一个 1h ephemeral block。", "scope": "v3-append-system"},
        "SPEC-BODY-046": {"domain": "body", "claim": "--exclude-dynamic-system-prompt-sections 只改变最后一个动态 system block，保留前述 block、顺序与 cache_control。", "scope": "v3-exclude-dynamic-system 与基线"},
        "SPEC-BODY-047": {"domain": "body", "claim": "获准自定义顶层 agent 使用 attribution、产品身份、自定义 agent prompt 三段 system，后两段使用 1h ephemeral cache_control。", "scope": "v3-custom-agent"},
        "SPEC-BODY-048": {"domain": "body", "claim": "真实 TUI 会先发 Haiku 标题请求：固定标题模型、32000 max_tokens、thinking disabled、temperature 1、JSON Schema output_config、空 tools 与单 user message。", "scope": "v3-tui 的标题请求"},
        "SPEC-BODY-049": {"domain": "body", "claim": "真实 TUI 的 Sonnet 主请求使用 cli attribution、TUI beta 序列、单 user message和 TUI 四段 system 形态。", "scope": "v3-tui 的主推理请求"},
        "SPEC-BODY-050": {"domain": "body", "claim": "fallback model 请求切换为 claude-haiku-4-5、max_tokens 32000、enabled thinking budget 31999、Haiku beta 与单 user message，省略 output_config，并保持会话身份。", "scope": "v3-fallback-model 的第四次 messages"},
        "SPEC-CACHE-005": {"domain": "cache", "claim": "DISABLE_PROMPT_CACHING=1 或 Sonnet 专用禁用条件均移除全部 system cache_control；其他 block 内容保持。", "scope": "v3-cache-disabled、v3-cache-sonnet-disabled 与基线"},
        "SPEC-CACHE-006": {"domain": "cache", "claim": "当前 Sonnet 基线默认即使用两个 1h system 缓存点；ENABLE_PROMPT_CACHING_1H=1 与基线的 system 文本及 cache_control 形态相同。", "scope": "v3-cache-one-hour 与基线"},
        "SPEC-META-001": {"domain": "metadata", "claim": "CLAUDE_CODE_EXTRA_METADATA 为合法对象时参与 metadata.user_id 内嵌 JSON 构造。", "scope": "v3-extra-metadata"},
        "SPEC-META-002": {"domain": "metadata", "claim": "额外 metadata 以浅合并方式置于 device_id、account_uuid、session_id 之前，嵌套对象原样保留。", "scope": "v3-extra-metadata"},
        "SPEC-TOOL-018": {"domain": "tool", "claim": "--json-schema 把输入 schema 包装为唯一 StructuredOutput 工具，使用固定说明和 input_schema，output_config.effort 仍为 high。", "scope": "v3-json-schema"},
        "SPEC-STATE-005": {"domain": "request_state", "claim": "safe-mode 下未获准的自定义 agent 在本地拒绝，完整 relay 中 messages 数为零。", "scope": "v3-custom-agent-safe-mode"},
        "SPEC-STATE-006": {"domain": "request_state", "claim": "--resume 复用原 Session-Id，metadata.session_id 与 Header 同值，并携带历史角色序列和 cc_prev_req。", "scope": "v3-session-resume"},
        "SPEC-STATE-007": {"domain": "request_state", "claim": "--fork-session 为第二次调用生成新 Session-Id，同时携带原会话历史角色序列和 cc_prev_req。", "scope": "v3-session-fork"},
        "SPEC-CONN-002": {"domain": "connection", "claim": "无 Retry-After 的应用层重试，首轮实测等待落在 500–750ms，第二轮落在 1000–1250ms。", "scope": "v21 replay-retry-limit 与 replay-fallback-model"},
        "SPEC-CONN-010": {"domain": "connection", "claim": "隔离状态矩阵中 401、408、409、429、500、502、503、529 各重试一次；400、403 不重试。", "scope": "10 个单状态隔离故障运行"},
        "SPEC-CONN-016": {"domain": "connection", "claim": "Retry-After:1 使重试间隔落在 1000–1100ms；未来 HTTP-date 在当前实现中未按日期等待，而在 500–700ms 后走默认退避。", "scope": "v21 两个 Retry-After 隔离故障 replay"},
        "SPEC-CONN-018": {"domain": "connection", "claim": "创建流收到 404 时总会转 non-stream；已建立流中断默认转 non-stream，disable flag 只阻止中断后的转换，不阻止创建 404 转换。", "scope": "四个 streaming fallback 隔离故障运行"},
        "SPEC-CONN-020": {"domain": "connection", "claim": "首个 messages 连接无响应断开后官方客户端重试；重发请求省略 Connection Header。", "scope": "v3-disconnect-retry"},
        "SPEC-CONN-021": {"domain": "connection", "claim": "应用层重试保持 Body、Session-Id 与主体 attribution，重新生成 x-client-request-id，X-Stainless-Retry-Count 始终为 0。", "scope": "状态重试、Retry-After、断连与 retry-limit 运行"},
        "SPEC-CONN-022": {"domain": "connection", "claim": "CLAUDE_CODE_MAX_RETRIES=2 时每个模型最多发送三次，配置 fallback model 时可再发一次 Haiku；值为 0 时不重试。", "scope": "v3-retry-limit、v3-fallback-model 与 v3-timeout"},
        "SPEC-CONN-023": {"domain": "connection", "claim": "配置 fallback model 且主 Sonnet 连续三次 529 后，第四次请求切换到 Haiku。", "scope": "v3-fallback-model"},
        "SPEC-CONN-024": {"domain": "connection", "claim": "API_TIMEOUT_MS=1000 映射为 X-Stainless-Timeout:1；配合 max retries 0 时 stalled 请求只发送一次。", "scope": "v3-timeout"},
    }
)

# 已有规则中，v3 把入口、effort 和会话条件从单一基线扩展为实测映射。
RULE_DEFINITIONS.update(
    {
        "SPEC-HDR-002": {"domain": "header", "claim": "User-Agent 基础为 claude-cli/2.1.226；sdk-cli 与真实 TUI 分别使用 external,sdk-cli 和 external,cli，并按已批准条件追加 agent-sdk、client-app、workload 段。", "scope": "旧 8 条推理样本与 v3 全部 messages 请求"},
        "SPEC-BODY-004": {"domain": "body", "claim": "首轮、续轮、resume、fork、子代理与真实 TUI 分别使用实测 messages 角色序列。", "scope": "旧 8 条推理样本、v3-session-* 与 v3-tui"},
        "SPEC-BODY-012": {"domain": "body", "claim": "output_config.effort 精确映射 low、medium、high、xhigh、max；基线为 high。", "scope": "五种 effort 值的 v3 正例"},
        "SPEC-BODY-015": {"domain": "body", "claim": "billing attribution 的 cc_entrypoint 与实际入口一致：print 为 sdk-cli，真实 TUI 为 cli。", "scope": "sdk-cli 与 v3-tui messages"},
        "SPEC-BODY-018": {"domain": "body", "claim": "存在前序请求关系的续轮、resume 与 fork 请求携带 cc_prev_req=req_<标识>；首轮省略。", "scope": "旧续轮样本与 v3-session-* 正负例"},
    }
)

FORBIDDEN_RULE_IDS = {
    "SPEC-TLS-001",
    "SPEC-TLS-002",
    "SPEC-HDR-011",
    "SPEC-HDR-034",
    "SPEC-HDR-035",
    "SPEC-HDR-036",
    "SPEC-STATE-002",
}


class MeasuredRuleError(RuntimeError):
    """表示真实证据、断言或规则闭集不一致。"""


def require(condition: bool, message: str) -> None:
    """在实测门禁失败时停止生成。"""

    if not condition:
        raise MeasuredRuleError(message)


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasuredRuleError(f"无法读取 JSON：{path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON 顶层必须是对象：{path}")
    return value


def repository_file(value: str, label: str) -> Path:
    """解析并校验仓库内普通文件。"""

    path = (ROOT / value).resolve()
    require(path.is_relative_to(ROOT), f"{label} 越过仓库根：{value}")
    require(path.is_file() and not path.is_symlink(), f"{label} 不是可信普通文件：{value}")
    return path


def binding(path: Path, channel: str, **facts: Any) -> dict[str, Any]:
    """为证据文件建立内容寻址引用。"""

    resolved = path.resolve()
    require(resolved.is_relative_to(ROOT), f"证据不在仓库内：{resolved}")
    return {
        "path": resolved.relative_to(ROOT).as_posix(),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
        "channel": channel,
        **facts,
    }


def header_map(sample: dict[str, Any]) -> dict[str, str]:
    """返回小写 Header 名映射。"""

    return {item["name"].lower(): item["value"] for item in sample["headers"]}


def parse_attribution(sample: dict[str, Any]) -> re.Match[str] | None:
    """解析 attribution 的当前实测格式。"""

    system = sample["body"].get("system")
    if not isinstance(system, list) or not system or not isinstance(system[0], dict):
        return None
    text = system[0].get("text")
    return ATTRIBUTION_RE.fullmatch(text) if isinstance(text, str) else None


def attribution_fields(sample: dict[str, Any]) -> dict[str, str]:
    """解析 v3 attribution 的有序键值；非 attribution block 返回空对象。"""

    blocks = sample.get("body", {}).get("system")
    if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
        return {}
    text = blocks[0].get("text")
    prefix = "x-anthropic-billing-header: "
    if not isinstance(text, str) or not text.startswith(prefix):
        return {}
    result: dict[str, str] = {}
    for segment in text[len(prefix):].split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            return {}
        key, value = segment.split("=", 1)
        if not key or key in result:
            return {}
        result[key] = value
    return result


def load_samples(profile_policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从四个目标 run 读取全部推理和生命周期请求。"""

    inference: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    expected_counts = {"a1": 3, "s1": 1, "s2": 2, "s4": 2}
    for scenario, run_value in sorted(profile_policy["sample_runs"]["target"].items()):
        run_dir = (ROOT / run_value).resolve()
        require(run_dir.is_relative_to(ROOT) and run_dir.is_dir() and not run_dir.is_symlink(), f"run 不可信：{run_value}")
        relay_path = run_dir / "relay" / "relay.json"
        manifest_path = run_dir / "relay-manifest.json"
        relay = load_json(relay_path)
        selected: list[dict[str, Any]] = []
        for connection in relay.get("connections", []):
            if not isinstance(connection, dict) or not connection.get("valid"):
                continue
            connection_id = connection.get("connection_id")
            require(isinstance(connection_id, int), f"{scenario} 有效连接缺少 connection_id")
            raw_path = run_dir / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
            if not raw_path.is_file():
                continue
            raw = raw_path.read_bytes()
            try:
                parsed = parse_http_stream(raw, f"{scenario}:{raw_path.name}")
            except ProfileBuildError:
                continue
            for request_index, request in enumerate(parsed, start=1):
                if (request["method"], request["request_target"]) not in {
                    ("POST", "/v1/messages?beta=true"),
                    ("HEAD", "/api/hello"),
                }:
                    continue
                start = request["stream_offset"]
                end = start + request["stream_length"]
                chunk = raw[start:end]
                split = chunk.find(b"\r\n\r\n")
                require(split >= 0, f"{scenario} 请求缺少 Header/Body 分隔")
                item = {
                    **request,
                    "scenario": scenario,
                    "request_index": request_index,
                    "connection_id": connection_id,
                    "connection": connection,
                    "raw_path": raw_path,
                    "relay_path": relay_path,
                    "manifest_path": manifest_path,
                    "raw_body_length": len(chunk) - split - 4,
                }
                selected.append(item)
                if request["method"] == "POST":
                    inference.append(item)
                else:
                    lifecycle.append(item)
        scenario_inference = [value for value in selected if value["method"] == "POST"]
        scenario_lifecycle = [value for value in selected if value["method"] == "HEAD"]
        require(len(scenario_inference) == expected_counts[scenario], f"{scenario} 推理请求数量不符")
        require(len(scenario_lifecycle) == 1, f"{scenario} 生命周期请求数量不符")
    require(len(inference) == 8 and len(lifecycle) == 4, "目标样本不是 8 条推理加 4 条生命周期")
    return inference, lifecycle


def load_v3_runs(profile_policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """读取 54 个 v3 正式 run，并按连接打开时间恢复真实请求顺序。"""

    evidence = profile_policy.get("v3_evidence")
    require(isinstance(evidence, dict), "profile policy 缺少 v3_evidence")
    root_value = evidence.get("root")
    require(isinstance(root_value, str) and root_value, "v3_evidence.root 缺失")
    evidence_root = (ROOT / root_value).resolve()
    require(
        evidence_root.is_relative_to(ROOT)
        and evidence_root.is_dir()
        and not evidence_root.is_symlink(),
        f"v3 evidence root 不可信：{root_value}",
    )
    expected_source = evidence.get("capture_source_bundle_sha256")
    require(
        isinstance(expected_source, str) and re.fullmatch(r"[0-9a-f]{64}", expected_source) is not None,
        "v3 capture source 摘要非法",
    )
    group_specs = {
        "real-runs": evidence.get("real_probe_ids"),
        "runs": evidence.get("synthetic_probe_ids"),
    }
    runs: dict[str, dict[str, Any]] = {}
    for group, expected_values in group_specs.items():
        require(
            isinstance(expected_values, list)
            and expected_values == sorted(set(expected_values))
            and expected_values,
            f"v3 {group} probe ID 必须严格排序且非空",
        )
        group_root = evidence_root / group
        actual_values = sorted(path.name for path in group_root.iterdir() if path.is_dir())
        require(actual_values == expected_values, f"v3 {group} 正式目录集合不一致")
        for probe_id in expected_values:
            require(probe_id not in runs, f"v3 probe 重复：{probe_id}")
            run_dir = group_root / probe_id
            manifest_path = run_dir / "relay-manifest.json"
            relay_path = run_dir / "relay" / "relay.json"
            summary_path = run_dir / "results" / "summary.json"
            invocation_path = run_dir / "results" / "invocation.json"
            relay_invocation_path = run_dir / "relay-invocation.json"
            manifest = load_json(manifest_path)
            relay = load_json(relay_path)
            summary = load_json(summary_path)
            require(manifest.get("status") == "complete", f"{probe_id} status 不是 complete")
            require(manifest.get("capture_mode") == "fw-f-v3", f"{probe_id} capture mode 不一致")
            require(manifest.get("probe_id") == summary.get("probe_id") == probe_id, f"{probe_id} 身份不一致")
            require(manifest.get("m_binding", {}).get("complete") is True, f"{probe_id} M binding 未闭合")
            require(manifest.get("m_binding", {}).get("limitations") == [], f"{probe_id} M binding 仍有限制")
            require(manifest.get("scenario_result", {}).get("valid") is True, f"{probe_id} 场景结果无效")
            require(manifest.get("relay_integrity", {}).get("result") == "passed", f"{probe_id} relay 完整性未通过")
            require(manifest.get("client", {}).get("sha256") == evidence.get("target_binary_sha256"), f"{probe_id} 客户端摘要不一致")
            require(manifest.get("credential_scrubbing", {}).get("verified") is True, f"{probe_id} 未完成等长脱敏")
            require(manifest.get("cleanup") == {"hosts_restored": True, "relay_stopped": True}, f"{probe_id} cleanup 未闭合")
            require(manifest.get("secret_scan", {}).get("passed") is True, f"{probe_id} 秘密扫描未通过")
            require(
                manifest.get("runtime", {})
                .get("capture_tools", {})
                .get("execution_sources", {})
                .get("sha256")
                == expected_source,
                f"{probe_id} 执行源摘要不一致",
            )
            is_synthetic = group == "runs"
            require(
                relay.get("production_forwarding_enabled") is (not is_synthetic),
                f"{probe_id} 生产转发边界与分组不一致",
            )

            requests: list[dict[str, Any]] = []
            connections = relay.get("connections")
            require(isinstance(connections, list), f"{probe_id} connections 非数组")
            ordered_connections = sorted(
                (value for value in connections if isinstance(value, dict)),
                key=lambda value: (value.get("opened_at_unix_ms", -1), value.get("connection_id", -1)),
            )
            for connection in ordered_connections:
                if not connection.get("valid"):
                    continue
                connection_id = connection.get("connection_id")
                require(isinstance(connection_id, int), f"{probe_id} 有效连接缺少 connection_id")
                raw_path = run_dir / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
                require(raw_path.is_file(), f"{probe_id} 缺少客户端 R：{raw_path.name}")
                raw = raw_path.read_bytes()
                try:
                    parsed_requests = parse_http_stream(raw, f"{probe_id}:{raw_path.name}")
                except ProfileBuildError as exc:
                    raise MeasuredRuleError(str(exc)) from exc
                for request_index, request in enumerate(parsed_requests, start=1):
                    start = request["stream_offset"]
                    end = start + request["stream_length"]
                    chunk = raw[start:end]
                    split = chunk.find(b"\r\n\r\n")
                    require(split >= 0, f"{probe_id} 请求缺少 Header/Body 分隔")
                    upstream_path = run_dir / "relay" / f"conn{connection_id:03d}.upstream_to_client.bin"
                    requests.append(
                        {
                            **request,
                            "scenario": probe_id,
                            "request_index": request_index,
                            "connection_id": connection_id,
                            "connection": connection,
                            "raw_path": raw_path,
                            "upstream_raw_path": upstream_path,
                            "relay_path": relay_path,
                            "manifest_path": manifest_path,
                            "summary_path": summary_path,
                            "raw_body_length": len(chunk) - split - 4,
                        }
                    )
            messages = [value for value in requests if (value["method"], value["request_target"]) == ("POST", "/v1/messages?beta=true")]
            require(
                len(messages) == manifest.get("relay_integrity", {}).get("messages_request_count"),
                f"{probe_id} messages 数与 M 不一致",
            )
            m_paths = [manifest_path, relay_path, summary_path, invocation_path, relay_invocation_path]
            intervention_path = run_dir / "relay" / "intervention.jsonl"
            if intervention_path.is_file():
                m_paths.append(intervention_path)
            runs[probe_id] = {
                "probe_id": probe_id,
                "group": group,
                "is_synthetic": is_synthetic,
                "run_dir": run_dir,
                "manifest": manifest,
                "relay": relay,
                "summary": summary,
                "requests": requests,
                "messages": messages,
                "hello": [value for value in requests if (value["method"], value["request_target"]) == ("HEAD", "/api/hello")],
                "policy_limits": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/claude_code/policy_limits")],
                "settings": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/claude_code/settings")],
                "oauth_profile": [value for value in requests if (value["method"], value["request_target"]) == ("GET", "/api/oauth/profile")],
                "m_paths": m_paths,
                "intervention_path": intervention_path if intervention_path.is_file() else None,
            }
    require(len(runs) == 54, f"v3 正式 run 不是 54 个：{len(runs)}")
    require(sum(not value["is_synthetic"] for value in runs.values()) == 34, "v3 真实上游 run 不是 34 个")
    require(sum(value["is_synthetic"] for value in runs.values()) == 20, "v3 隔离故障 run 不是 20 个")
    return runs


def sample_evidence(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把规则命中的请求聚合成不重复的 R/M 证据引用。"""

    by_raw: dict[str, dict[str, Any]] = {}
    manifests: dict[str, Path] = {}
    relays: dict[str, Path] = {}
    for sample in samples:
        raw_path = sample["raw_path"]
        key = raw_path.resolve().relative_to(ROOT).as_posix()
        value = by_raw.setdefault(
            key,
            binding(raw_path, "R", scenarios=[], stream_offsets=[], raw_request_sha256s=[]),
        )
        value["scenarios"].append(sample["scenario"])
        value["stream_offsets"].append(sample["stream_offset"])
        value["raw_request_sha256s"].append(sample["raw_sha256"])
        manifests[sample["manifest_path"].as_posix()] = sample["manifest_path"]
        relays[sample["relay_path"].as_posix()] = sample["relay_path"]
    result = []
    for value in by_raw.values():
        value["scenarios"] = sorted(set(value["scenarios"]))
        value["stream_offsets"] = sorted(set(value["stream_offsets"]))
        value["raw_request_sha256s"] = sorted(set(value["raw_request_sha256s"]))
        result.append(value)
    result.extend(binding(path, "M") for path in manifests.values())
    result.extend(binding(path, "M") for path in relays.values())
    return sorted(result, key=lambda value: (value["path"], value["channel"]))


def v3_evidence(
    runs: Iterable[dict[str, Any]],
    *,
    requests: Iterable[dict[str, Any]] = (),
    include_all_client_requests: bool = False,
    include_responses: bool = False,
) -> list[dict[str, Any]]:
    """为 v3 规则绑定精确 R 请求／响应与完整 M 文件。"""

    run_values = list(runs)
    request_values = list(requests)
    if include_all_client_requests:
        request_values.extend(
            request
            for run in run_values
            for request in run["requests"]
        )
    by_ref: dict[tuple[str, str], dict[str, Any]] = {}
    for request in request_values:
        raw_ref = binding(
            request["raw_path"],
            "R",
            direction="client_to_upstream",
            scenarios=[request["scenario"]],
            connection_ids=[request["connection_id"]],
            stream_offsets=[request["stream_offset"]],
            raw_request_sha256s=[request["raw_sha256"]],
        )
        by_ref[(raw_ref["path"], raw_ref["channel"])] = raw_ref
        if include_responses and request["upstream_raw_path"].is_file():
            response_ref = binding(
                request["upstream_raw_path"],
                "R",
                direction="upstream_to_client",
                scenarios=[request["scenario"]],
                connection_ids=[request["connection_id"]],
            )
            by_ref[(response_ref["path"], response_ref["channel"])] = response_ref
    for run in run_values:
        for path in run["m_paths"]:
            value = binding(path, "M")
            by_ref[(value["path"], value["channel"])] = value
    return [by_ref[key] for key in sorted(by_ref)]


def request_header_names(sample: dict[str, Any]) -> list[str]:
    """返回保留大小写和线序的 Header 名列表。"""

    return [value["name"] for value in sample["headers"]]


def system_blocks(sample: dict[str, Any]) -> list[dict[str, Any]]:
    """严格取得 messages Body 的 system block。"""

    value = sample["body"].get("system")
    require(isinstance(value, list), f"{sample['scenario']} system 不是数组")
    require(all(isinstance(item, dict) for item in value), f"{sample['scenario']} system block 非对象")
    return value


def user_metadata(sample: dict[str, Any]) -> dict[str, Any]:
    """解析 metadata.user_id 的内嵌 JSON 对象。"""

    metadata = sample["body"].get("metadata")
    require(isinstance(metadata, dict) and isinstance(metadata.get("user_id"), str), f"{sample['scenario']} metadata.user_id 非字符串")
    try:
        value = json.loads(metadata["user_id"])
    except json.JSONDecodeError as exc:
        raise MeasuredRuleError(f"{sample['scenario']} metadata.user_id 不是 JSON") from exc
    require(isinstance(value, dict), f"{sample['scenario']} metadata.user_id 顶层非对象")
    return value


def messages_role_sequence(sample: dict[str, Any]) -> list[str]:
    """返回 messages 的角色序列。"""

    value = sample["body"].get("messages")
    require(isinstance(value, list), f"{sample['scenario']} messages 不是数组")
    return [item.get("role") for item in value if isinstance(item, dict)]


def run_open_times(run: dict[str, Any], requests: Iterable[dict[str, Any]]) -> list[int]:
    """按实际连接打开时间返回请求时间点。"""

    values = [value["connection"].get("opened_at_unix_ms") for value in requests]
    require(all(isinstance(value, int) for value in values), f"{run['probe_id']} 缺少连接时间")
    return values


def build_ledger(
    discovery_policy: dict[str, Any],
    profile_policy: dict[str, Any],
    campaign_identity_path: Path,
    relay_index_path: Path,
    *,
    prepared_samples: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    prepared_v3_runs: dict[str, dict[str, Any]] | None = None,
    identity_override: dict[str, Any] | None = None,
    relay_index_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行 88 条实测断言并生成规则台账。

    ``prepared_*`` 仅供完整 Campaign 的最终化工具复用同一组原子断言。传入后，
    样本仍必须来自调用方已经完成身份、R/M 完整性和秘密扫描校验的正式 attempt；
    本函数不会放宽任何规则断言。
    """

    policy_schema = discovery_policy.get("schema_version")
    require(policy_schema in POLICY_SCHEMAS, "discovery policy schema 不匹配")
    require(profile_policy.get("schema_version") in PROFILE_POLICY_SCHEMAS, "profile policy schema 不匹配")
    identity = identity_override if identity_override is not None else load_json(campaign_identity_path)
    relay_index = relay_index_override if relay_index_override is not None else load_json(relay_index_path)
    target_version = discovery_policy.get("target_version")
    require(target_version == profile_policy.get("target_version") == identity.get("target_version") == relay_index.get("target", {}).get("version"), "目标版本身份不一致")
    binary_sha256 = discovery_policy.get("target_binary_sha256")
    require(binary_sha256 == identity.get("target_binary_sha256") == relay_index.get("target", {}).get("binary_sha256"), "目标二进制摘要不一致")
    require(identity.get("privacy_environment") == {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}, "隐私环境不是 essential-only 且关闭遥测")
    expected_ids = discovery_policy.get("measured_rule_ids")
    if policy_schema == "claude-code-fw-f-discovery-clearance-policy/v3":
        require(expected_ids is None, "v3 策略不得重新预设规则数或 measured_rule_ids")
        expected_ids = sorted(RULE_DEFINITIONS)
    require(isinstance(expected_ids, list) and expected_ids == sorted(set(expected_ids)), "measured_rule_ids 必须严格排序且无重复")
    require(set(expected_ids) == set(RULE_DEFINITIONS), "策略与断言实现的规则闭集不一致")
    require(len(expected_ids) == 88, "实测规则闭集不是 88 条")
    require(not (set(expected_ids) & FORBIDDEN_RULE_IDS), "禁用规则进入实测闭集")

    if prepared_samples is None:
        inference, lifecycle = load_samples(profile_policy)
    else:
        inference, lifecycle = prepared_samples
        require(len(inference) == 8 and len(lifecycle) == 4, "预解析目标样本不是 8 条推理加 4 条生命周期")
    v3_runs = load_v3_runs(profile_policy) if prepared_v3_runs is None else prepared_v3_runs
    require(len(v3_runs) == 54, f"预解析 replay run 不是 54 个：{len(v3_runs)}")
    all_samples = inference + lifecycle
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in inference:
        by_scenario[sample["scenario"]].append(sample)
    for values in by_scenario.values():
        values.sort(key=lambda value: value["stream_offset"])

    records: dict[str, dict[str, Any]] = {}

    def record_rule(
        spec_id: str,
        refs: Iterable[dict[str, Any]],
        sample_scope: dict[str, Any],
        *,
        applicability: Iterable[str] = APPLICABILITY,
    ) -> None:
        """把已经通过的原子断言写入统一规则记录。"""

        require(spec_id not in records, f"规则重复断言：{spec_id}")
        definition = RULE_DEFINITIONS[spec_id]
        by_ref = {(value["path"], value["channel"]): value for value in refs}
        evidence_refs = [by_ref[key] for key in sorted(by_ref)]
        require(any(value["channel"] == "R" for value in evidence_refs), f"{spec_id} 缺少真实 R 证据")
        require(any(value["channel"] == "M" for value in evidence_refs), f"{spec_id} 缺少 M 证据")
        records[spec_id] = {
            "spec_id": spec_id,
            "domain": definition["domain"],
            "retained_claim": definition["claim"],
            "applicability_scope": definition["scope"],
            "assertion_id": f"PAIR-{spec_id}",
            "assertion_result": "passed",
            "sample_scope": sample_scope,
            "evidence_level": "observed",
            "rule_lifecycle": "candidate",
            "compatibility_class": "request_egress",
            "egress_ids": list(RULE_EGRESS_IDS.get(spec_id, (DEFAULT_EGRESS_ID,))),
            "migration_decision": "change",
            "production_eligibility": "validation_only",
            "evidence_channels": ["M", "R"],
            "evidence_refs": evidence_refs,
            "applicability": sorted(set(applicability)),
        }

    def add(
        spec_id: str,
        samples: list[dict[str, Any]],
        predicate: Callable[[dict[str, Any]], bool],
        *,
        unit: str = "request",
        eligible_count: int | None = None,
        positive_count: int | None = None,
        negative_count: int | None = None,
        extra_evidence: Iterable[Path] = (),
    ) -> None:
        failed = [f"{value['scenario']}@{value['stream_offset']}" for value in samples if not predicate(value)]
        require(not failed, f"{spec_id} 实测断言失败：{failed}")
        refs = sample_evidence(samples)
        refs.extend(binding(path, "M") for path in extra_evidence)
        scope: dict[str, Any] = {
            "unit": unit,
            "eligible_count": eligible_count if eligible_count is not None else len(samples),
            "matched_count": eligible_count if eligible_count is not None else len(samples),
            "scenarios": sorted({value["scenario"] for value in samples}),
        }
        if positive_count is not None:
            scope["positive_count"] = positive_count
        if negative_count is not None:
            scope["negative_count"] = negative_count
        record_rule(spec_id, refs, scope)

    def add_v3_rule(
        spec_id: str,
        condition: bool,
        run_ids: Iterable[str],
        requests: Iterable[dict[str, Any]],
        *,
        unit: str = "request",
        eligible_count: int | None = None,
        positive_count: int | None = None,
        negative_count: int | None = None,
        include_all_client_requests: bool = False,
        include_responses: bool = False,
        applicability: Iterable[str] = APPLICABILITY,
    ) -> None:
        """记录一条由 v3 单变量／故障矩阵直接验证的规则。"""

        selected_ids = sorted(set(run_ids))
        require(condition, f"{spec_id} v3 实测断言失败：{selected_ids}")
        selected_runs = [v3_runs[value] for value in selected_ids]
        request_values = list(requests)
        count = eligible_count if eligible_count is not None else len(request_values)
        scope: dict[str, Any] = {
            "unit": unit,
            "eligible_count": count,
            "matched_count": count,
            "scenarios": selected_ids,
        }
        if positive_count is not None:
            scope["positive_count"] = positive_count
        if negative_count is not None:
            scope["negative_count"] = negative_count
        record_rule(
            spec_id,
            v3_evidence(
                selected_runs,
                requests=request_values,
                include_all_client_requests=include_all_client_requests,
                include_responses=include_responses,
            ),
            scope,
            applicability=applicability,
        )

    def augment_rule_with_v3(
        spec_id: str,
        condition: bool,
        run_ids: Iterable[str],
        requests: Iterable[dict[str, Any]],
        *,
        include_responses: bool = False,
        applicability: Iterable[str] | None = None,
    ) -> None:
        """给已有基础规则追加 v3 条件分母和内容寻址证据。"""

        selected_ids = sorted(set(run_ids))
        require(condition, f"{spec_id} v3 扩展断言失败：{selected_ids}")
        require(spec_id in records, f"{spec_id} 尚未生成基础断言")
        request_values = list(requests)
        refs = records[spec_id]["evidence_refs"] + v3_evidence(
            [v3_runs[value] for value in selected_ids],
            requests=request_values,
            include_responses=include_responses,
        )
        by_ref = {(value["path"], value["channel"]): value for value in refs}
        records[spec_id]["evidence_refs"] = [by_ref[key] for key in sorted(by_ref)]
        scope = records[spec_id]["sample_scope"]
        base_scope = {
            "eligible_count": scope["eligible_count"],
            "matched_count": scope["matched_count"],
            "scenarios": scope["scenarios"],
        }
        scope["base"] = base_scope
        scope["eligible_count"] += len(request_values)
        scope["matched_count"] += len(request_values)
        scope["scenarios"] = sorted(set(scope["scenarios"]) | set(selected_ids))
        scope["v3_extension"] = {
            "eligible_count": len(request_values),
            "matched_count": len(request_values),
            "scenarios": selected_ids,
        }
        if applicability is not None:
            records[spec_id]["applicability"] = sorted(set(applicability))

    add("SPEC-PROTO-001", all_samples, lambda value: value["http_version"] == "HTTP/1.1")
    add("SPEC-EP-001", inference, lambda value: value["request_target"] == "/v1/messages?beta=true")
    add("SPEC-EP-003", inference, lambda value: value["method"] == "POST")
    add("SPEC-EP-004", inference, lambda value: header_map(value).get("host") == "api.anthropic.com")
    add("SPEC-HDR-002", inference, lambda value: header_map(value).get("user-agent") == "claude-cli/2.1.226 (external, sdk-cli)")
    add(
        "SPEC-HDR-003",
        inference,
        lambda value: header_map(value).get("anthropic-beta")
        == (BETA_BASE_VALUE if header_map(value).get("x-claude-code-agent-id") else BETA_MAIN_VALUE),
        positive_count=1,
        negative_count=7,
    )
    add("SPEC-HDR-004", inference, lambda value: header_map(value).get("anthropic-version") == "2023-06-01")
    add("SPEC-HDR-005", inference, lambda value: header_map(value).get("accept-encoding") == "gzip, deflate, br, zstd")
    stainless = {
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Linux",
        "x-stainless-package-version": "0.94.0",
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v26.3.0",
        "x-stainless-timeout": "600",
    }
    add("SPEC-HDR-006", inference, lambda value: all(header_map(value).get(name) == expected for name, expected in stainless.items()))
    add("SPEC-HDR-007", inference, lambda value: header_map(value).get("anthropic-dangerous-direct-browser-access") == "true" and header_map(value).get("x-app") == "cli")

    def header_order_matches(value: dict[str, Any]) -> bool:
        expected = list(INFERENCE_HEADER_ORDER)
        if "x-claude-code-agent-id" in header_map(value):
            expected.insert(expected.index("x-client-request-id"), "x-claude-code-agent-id")
        return [item["name"] for item in value["headers"]] == expected

    add("SPEC-HDR-001", inference, header_order_matches)
    request_ids = [header_map(value).get("x-client-request-id", "") for value in inference]
    require(len(set(request_ids)) == 8, "x-client-request-id 在样本内发生复用")
    add("SPEC-HDR-012", inference, lambda value: UUID_RE.fullmatch(header_map(value).get("x-client-request-id", "")) is not None)

    multi_samples = [value for scenario in ("a1", "s2", "s4") for value in by_scenario[scenario]]
    require(all(len({header_map(value)["x-claude-code-session-id"] for value in by_scenario[scenario]}) == 1 for scenario in ("a1", "s2", "s4")), "多请求运行没有复用 session id")
    add("SPEC-HDR-013", multi_samples, lambda value: True, unit="multi-request-run", eligible_count=3)

    subagents = [value for value in inference if header_map(value).get("x-claude-code-agent-id")]
    require(len(subagents) == 1, "子代理正例数量不是 1")
    add(
        "SPEC-HDR-014",
        inference,
        lambda value: (AGENT_ID_RE.fullmatch(header_map(value).get("x-claude-code-agent-id", "")) is not None) == (value is subagents[0]),
        positive_count=1,
        negative_count=7,
    )
    main_session = header_map(by_scenario["a1"][0])["x-claude-code-session-id"]
    session_role_samples = [by_scenario["a1"][0], subagents[0]]
    add(
        "SPEC-HDR-015",
        session_role_samples,
        lambda value: header_map(value).get("x-claude-code-session-id") == main_session,
        positive_count=1,
        negative_count=1,
    )
    add("SPEC-HDR-044", inference, lambda value: int(header_map(value).get("content-length", "-1")) == value["raw_body_length"])
    for manifest_path in sorted({value["manifest_path"] for value in inference}):
        scrubbing = load_json(manifest_path).get("credential_scrubbing")
        require(
            isinstance(scrubbing, dict)
            and scrubbing.get("method") == "equal_length_replacement"
            and scrubbing.get("verified") is True,
            f"Authorization 证据未证明等长脱敏：{manifest_path}",
        )
    add("SPEC-AUTH-002", inference, lambda value: re.fullmatch(r"Bearer <secret>X*", header_map(value).get("authorization", "")) is not None)
    add("SPEC-BODY-001", inference, lambda value: list(value["body"]) == BODY_KEY_ORDER)

    def metadata_matches(value: dict[str, Any]) -> bool:
        metadata = value["body"].get("metadata")
        if not isinstance(metadata, dict) or list(metadata) != ["user_id"] or not isinstance(metadata["user_id"], str):
            return False
        try:
            user_id = json.loads(metadata["user_id"])
        except json.JSONDecodeError:
            return False
        return (
            isinstance(user_id, dict)
            and list(user_id) == ["device_id", "account_uuid", "session_id"]
            and user_id["session_id"] == header_map(value).get("x-claude-code-session-id")
        )

    add("SPEC-BODY-002", inference, metadata_matches)

    def system_matches(value: dict[str, Any]) -> bool:
        system = value["body"].get("system")
        if not isinstance(system, list):
            return False
        is_subagent = value is subagents[0]
        if len(system) != (3 if is_subagent else 4):
            return False
        if any(not isinstance(block, dict) or block.get("type") != "text" for block in system):
            return False
        if system[1].get("text") != PRODUCT_IDENTITY:
            return False
        controls = [block.get("cache_control") for block in system]
        if is_subagent:
            return controls == [None, {"type": "ephemeral"}, {"type": "ephemeral"}]
        return controls == [None, None, {"type": "ephemeral", "ttl": "1h", "scope": "global"}, {"type": "ephemeral", "ttl": "1h"}]

    add("SPEC-BODY-003", inference, system_matches, positive_count=1, negative_count=7)

    first_roles = ["user", "system"]
    continuation_roles = ["user", "system", "assistant", "user", "system"]
    continuation_roles_after_agent = ["user", "system", "assistant", "user"]
    subagent_roles = ["user"]

    def messages_match(value: dict[str, Any]) -> bool:
        roles = [item.get("role") for item in value["body"].get("messages", [])]
        attribution = parse_attribution(value)
        if value is subagents[0]:
            return roles == subagent_roles
        if attribution is not None and attribution.group(4):
            return roles in (continuation_roles, continuation_roles_after_agent)
        return roles == first_roles

    add("SPEC-BODY-004", inference, messages_match, positive_count=4, negative_count=4)

    def tools_match(value: dict[str, Any]) -> bool:
        tools = value["body"].get("tools")
        if not isinstance(tools, list):
            return False
        return canonical_sha256(tools) == TOOLS_DIGEST_BY_SCENARIO[value["scenario"]]

    add("SPEC-BODY-005", inference, tools_match)
    add("SPEC-BODY-007", inference, lambda value: parse_attribution(value) is not None)
    add("SPEC-BODY-008", inference, lambda value: value["body"].get("model") == "claude-sonnet-5")
    add("SPEC-BODY-009", inference, lambda value: value["body"].get("max_tokens") == 64000 and not isinstance(value["body"].get("max_tokens"), bool))
    def adaptive_thinking_matches(value: dict[str, Any]) -> bool:
        thinking = value["body"].get("thinking")
        if not isinstance(thinking, dict) or thinking.get("type") != "adaptive":
            return False
        if list(thinking) == ["type"]:
            return True
        return list(thinking) == ["type", "display"] and thinking.get("display") in {
            "summarized",
            "omitted",
        }

    add("SPEC-BODY-010", inference, adaptive_thinking_matches)
    add("SPEC-BODY-011", inference, lambda value: value["body"].get("context_management") == {"edits": [{"keep": "all", "type": "clear_thinking_20251015"}]})
    add("SPEC-BODY-012", inference, lambda value: value["body"].get("output_config") == {"effort": "high"})
    add("SPEC-BODY-013", inference, lambda value: value["body"].get("stream") is True)
    add("SPEC-BODY-014", inference, lambda value: parse_attribution(value) is not None and parse_attribution(value).group(1).startswith("2.1.226."))
    add("SPEC-BODY-015", inference, lambda value: parse_attribution(value) is not None and parse_attribution(value).group(2) == "sdk-cli")
    add("SPEC-BODY-016", inference, lambda value: parse_attribution(value) is not None and len(parse_attribution(value).group(3)) == 5)

    continuation = [value for value in inference if parse_attribution(value) is not None and parse_attribution(value).group(4)]
    require(len(continuation) == 3, "cc_prev_req 正例数量不是 3")
    add(
        "SPEC-BODY-018",
        inference,
        lambda value: (parse_attribution(value).group(4) is not None)
        == (
            [item.get("role") for item in value["body"]["messages"]]
            in (continuation_roles, continuation_roles_after_agent)
        ),
        positive_count=3,
        negative_count=5,
    )
    add(
        "SPEC-BODY-019",
        inference,
        lambda value: (parse_attribution(value).group(5) == "true") == (value is subagents[0]),
        positive_count=1,
        negative_count=7,
    )

    def lifecycle_matches(value: dict[str, Any]) -> bool:
        current = [(item["name"], item["value"]) for item in value["headers"]]
        scenario_inference = by_scenario[value["scenario"]]
        return (
            value["method"] == "HEAD"
            and value["request_target"] == "/api/hello"
            and value["raw_body_length"] == 0
            and current == LIFECYCLE_HEADERS
            and value["connection_id"] != scenario_inference[0]["connection_id"]
            and value["connection"]["opened_at_unix_ms"] < scenario_inference[0]["connection"]["opened_at_unix_ms"]
        )

    add("SPEC-EP-002", lifecycle, lifecycle_matches)

    selected_connections: dict[tuple[str, int], dict[str, Any]] = {}
    for value in all_samples:
        selected_connections[(value["scenario"], value["connection_id"])] = value
    connection_samples = list(selected_connections.values())
    require(len(connection_samples) == 8, "承载目标请求的连接数量不是 8")
    add("SPEC-TLS-003", connection_samples, lambda value: value["connection"].get("sni") == "api.anthropic.com", unit="connection")

    for scenario in ("a1", "s2", "s4"):
        values = by_scenario[scenario]
        require(len({value["connection_id"] for value in values}) == 1, f"{scenario} 没有复用同一连接")
        require(all(value["connection"].get("client_alpn") == "http/1.1" for value in values), f"{scenario} 有效连接未选择 HTTP/1.1")
        require([value["stream_offset"] for value in values] == sorted(value["stream_offset"] for value in values), f"{scenario} 请求流偏移无序")
    add("SPEC-CONN-019", multi_samples, lambda value: True, unit="multi-request-run", eligible_count=3)

    def one_message(probe_id: str, index: int = 0) -> dict[str, Any]:
        values = v3_runs[probe_id]["messages"]
        require(len(values) > index, f"{probe_id} 缺少 messages[{index}]")
        return values[index]

    baseline_v3 = one_message("v3-baseline")
    baseline_headers = header_map(baseline_v3)
    baseline_header_names = request_header_names(baseline_v3)
    baseline_system = system_blocks(baseline_v3)

    # 辅助端点和入口级请求序列。
    sdk_run_ids = sorted(set(v3_runs) - {"v3-tui"})
    policy_requests = [request for probe_id in sdk_run_ids for request in v3_runs[probe_id]["policy_limits"]]
    settings_requests = [request for probe_id in sdk_run_ids for request in v3_runs[probe_id]["settings"]]
    require(len(policy_requests) == len(settings_requests) == 55, "sdk-cli 辅助端点分母不是各 55 条")

    def policy_request_matches(value: dict[str, Any]) -> bool:
        headers = header_map(value)
        names = request_header_names(value)
        base_names = ["Accept", "Authorization", "anthropic-beta", "User-Agent", "Accept-Encoding", "Host", "Connection"]
        cached_names = ["Accept", "Authorization", "anthropic-beta", "User-Agent", "If-None-Match", "Accept-Encoding", "Host", "Connection"]
        etag = headers.get("if-none-match")
        return (
            value["http_version"] == "HTTP/1.1"
            and value["raw_body_length"] == 0
            and names in (base_names, cached_names)
            and headers.get("accept") == "application/json, text/plain, */*"
            and re.fullmatch(r"Bearer <secret>X*", headers.get("authorization", "")) is not None
            and headers.get("anthropic-beta") == "oauth-2025-04-20"
            and headers.get("user-agent") == "claude-code/2.1.226"
            and headers.get("accept-encoding") == "gzip, compress, deflate, br"
            and headers.get("host") == "api.anthropic.com"
            and headers.get("connection") == "keep-alive"
            and (etag is None or re.fullmatch(r'"sha256:[0-9a-f]{64}"', etag) is not None)
        )

    add_v3_rule(
        "SPEC-EP-005",
        all(policy_request_matches(value) for value in policy_requests),
        sdk_run_ids,
        policy_requests,
    )

    def settings_request_matches(value: dict[str, Any]) -> bool:
        headers = header_map(value)
        names = request_header_names(value)
        base_names = ["Accept", "Authorization", "anthropic-beta", "User-Agent", "Cache-Control", "Pragma", "Accept-Encoding", "Host", "Connection"]
        cached_names = ["Accept", "Authorization", "anthropic-beta", "User-Agent", "Cache-Control", "Pragma", "If-None-Match", "Accept-Encoding", "Host", "Connection"]
        etag = headers.get("if-none-match")
        return (
            value["http_version"] == "HTTP/1.1"
            and value["raw_body_length"] == 0
            and names in (base_names, cached_names)
            and headers.get("accept") == "application/json, text/plain, */*"
            and re.fullmatch(r"Bearer <secret>X*", headers.get("authorization", "")) is not None
            and headers.get("anthropic-beta") == "oauth-2025-04-20"
            and headers.get("user-agent") == "claude-code/2.1.226"
            and headers.get("cache-control") == "no-cache"
            and headers.get("pragma") == "no-cache"
            and headers.get("accept-encoding") == "gzip, compress, deflate, br"
            and headers.get("host") == "api.anthropic.com"
            and headers.get("connection") == "keep-alive"
            and (etag is None or re.fullmatch(r'"sha256:[0-9a-f]{64}"', etag) is not None)
        )

    add_v3_rule(
        "SPEC-EP-006",
        all(settings_request_matches(value) for value in settings_requests),
        sdk_run_ids,
        settings_requests,
    )

    tui_run = v3_runs["v3-tui"]
    require(len(tui_run["oauth_profile"]) == 1, "v3-tui oauth profile 请求数量不是 1")
    tui_profile = tui_run["oauth_profile"][0]
    tui_profile_headers = header_map(tui_profile)
    add_v3_rule(
        "SPEC-EP-007",
        request_header_names(tui_profile)
        == ["Accept", "Content-Type", "Authorization", "Cache-Control", "User-Agent", "Accept-Encoding", "Host", "Connection"]
        and tui_profile["raw_body_length"] == 0
        and tui_profile_headers.get("accept") == "application/json, text/plain, */*"
        and tui_profile_headers.get("content-type") == "application/json"
        and re.fullmatch(r"Bearer <secret>X*", tui_profile_headers.get("authorization", "")) is not None
        and tui_profile_headers.get("cache-control") == "no-cache"
        and tui_profile_headers.get("user-agent") == "axios/1.15.2"
        and tui_profile_headers.get("accept-encoding") == "gzip, compress, deflate, br"
        and tui_profile_headers.get("host") == "api.anthropic.com"
        and tui_profile_headers.get("connection") == "keep-alive",
        ["v3-tui"],
        [tui_profile],
        applicability=TUI_PROFILE_APPLICABILITY,
    )

    sequence_requests: list[dict[str, Any]] = []
    sequence_ok = True
    for probe_id, run in sorted(v3_runs.items()):
        hello = run["hello"]
        require(len(hello) in {1, 2}, f"{probe_id} hello 数量非法")
        sequence_requests.extend(hello)
        if probe_id == "v3-tui":
            title, main = sorted(run["messages"], key=lambda value: value["connection"]["opened_at_unix_ms"])
            selected = [hello[0], run["oauth_profile"][0], title, main]
            sequence_ok = sequence_ok and run_open_times(run, selected) == sorted(run_open_times(run, selected))
            sequence_requests.extend([run["oauth_profile"][0], title, main])
            continue
        sequence_requests.extend(run["policy_limits"] + run["settings"] + run["messages"])
        groups: list[list[dict[str, Any]]] = []
        for request in run["requests"]:
            if (request["method"], request["request_target"]) == ("HEAD", "/api/hello"):
                groups.append([])
            require(groups, f"{probe_id} 在 hello 前出现请求")
            groups[-1].append(request)
        sequence_ok = sequence_ok and len(groups) == len(hello)
        for group in groups:
            request_lines = [(value["method"], value["request_target"]) for value in group]
            sequence_ok = sequence_ok and (
                len(request_lines) >= 3
                and request_lines[0] == ("HEAD", "/api/hello")
                and set(request_lines[1:3])
                == {
                    ("GET", "/api/claude_code/policy_limits"),
                    ("GET", "/api/claude_code/settings"),
                }
                and all(value == ("POST", "/v1/messages?beta=true") for value in request_lines[3:])
            )
    add_v3_rule(
        "SPEC-EP-008",
        sequence_ok,
        v3_runs,
        sequence_requests,
        unit="run",
        eligible_count=54,
        applicability=MIXED_ENTRYPOINT_APPLICABILITY,
    )

    # User-Agent 基础与条件段。
    all_v3_messages = [request for run in v3_runs.values() for request in run["messages"]]
    ua_by_probe = {
        "v3-agent-sdk": "claude-cli/2.1.226 (external, sdk-cli, agent-sdk/9.9.9-fw-f-v3)",
        "v3-client-app": "claude-cli/2.1.226 (external, sdk-cli, client-app/fw-f-v3-client)",
        "v3-header-combination": "claude-cli/2.1.226 (external, sdk-cli, agent-sdk/9.9.9-fw-f-v3-combo, client-app/fw-f-v3-combo-app, workload/fw-f-v3-combo-workload)",
        "v3-tui": "claude-cli/2.1.226 (external, cli)",
        "v3-workload": "claude-cli/2.1.226 (external, sdk-cli, workload/fw-f-v3-workload)",
    }
    augment_rule_with_v3(
        "SPEC-HDR-002",
        all(header_map(value).get("user-agent") == ua_by_probe.get(value["scenario"], "claude-cli/2.1.226 (external, sdk-cli)") for value in all_v3_messages),
        v3_runs,
        all_v3_messages,
        applicability=MIXED_ENTRYPOINT_APPLICABILITY,
    )

    header_baseline_negative = [baseline_v3]
    protection = one_message("v3-additional-protection")
    combination = one_message("v3-header-combination")
    add_v3_rule(
        "SPEC-HDR-009",
        header_map(protection).get("x-anthropic-additional-protection") == "true"
        and header_map(combination).get("x-anthropic-additional-protection") == "true"
        and "x-anthropic-additional-protection" not in baseline_headers
        and request_header_names(protection).index("x-anthropic-additional-protection") + 1 == request_header_names(protection).index("x-app"),
        ["v3-additional-protection", "v3-baseline", "v3-header-combination"],
        [protection, combination, *header_baseline_negative],
        positive_count=2,
        negative_count=1,
    )

    client_app = one_message("v3-client-app")
    remote_container = one_message("v3-remote-container")
    remote_session = one_message("v3-remote-session")
    add_v3_rule(
        "SPEC-HDR-016",
        header_map(client_app).get("x-client-app") == "fw-f-v3-client"
        and header_map(combination).get("x-client-app") == "fw-f-v3-combo-app"
        and "x-client-app" not in baseline_headers,
        ["v3-baseline", "v3-client-app", "v3-header-combination"],
        [baseline_v3, client_app, combination],
        positive_count=2,
        negative_count=1,
    )
    add_v3_rule(
        "SPEC-HDR-021",
        header_map(client_app).get("x-client-app") == v3_runs["v3-client-app"]["summary"]["injected_probe_env"]["CLAUDE_AGENT_SDK_CLIENT_APP"]
        and header_map(combination).get("x-client-app") == v3_runs["v3-header-combination"]["summary"]["injected_probe_env"]["CLAUDE_AGENT_SDK_CLIENT_APP"],
        ["v3-client-app", "v3-header-combination"],
        [client_app, combination],
    )
    add_v3_rule(
        "SPEC-HDR-022",
        header_map(client_app)["user-agent"].endswith(f"client-app/{header_map(client_app)['x-client-app']})")
        and f"client-app/{header_map(combination)['x-client-app']}" in header_map(combination)["user-agent"]
        and "client-app/" not in baseline_headers["user-agent"],
        ["v3-baseline", "v3-client-app", "v3-header-combination"],
        [baseline_v3, client_app, combination],
        positive_count=2,
        negative_count=1,
    )

    add_v3_rule(
        "SPEC-HDR-017",
        header_map(remote_container).get("x-claude-remote-container-id") == "fw-f-v3-container"
        and header_map(combination).get("x-claude-remote-container-id") == "fw-f-v3-combo-container"
        and "x-claude-remote-container-id" not in baseline_headers,
        ["v3-baseline", "v3-header-combination", "v3-remote-container"],
        [baseline_v3, combination, remote_container],
        positive_count=2,
        negative_count=1,
    )
    add_v3_rule(
        "SPEC-HDR-023",
        header_map(remote_container).get("x-claude-remote-container-id") == v3_runs["v3-remote-container"]["summary"]["injected_probe_env"]["CLAUDE_CODE_CONTAINER_ID"]
        and header_map(combination).get("x-claude-remote-container-id") == v3_runs["v3-header-combination"]["summary"]["injected_probe_env"]["CLAUDE_CODE_CONTAINER_ID"],
        ["v3-header-combination", "v3-remote-container"],
        [combination, remote_container],
    )
    add_v3_rule(
        "SPEC-HDR-018",
        header_map(remote_session).get("x-claude-remote-session-id") == "fw-f-v3-remote-session"
        and header_map(combination).get("x-claude-remote-session-id") == "fw-f-v3-combo-session"
        and "x-claude-remote-session-id" not in baseline_headers,
        ["v3-baseline", "v3-header-combination", "v3-remote-session"],
        [baseline_v3, combination, remote_session],
        positive_count=2,
        negative_count=1,
    )
    add_v3_rule(
        "SPEC-HDR-024",
        header_map(remote_session).get("x-claude-remote-session-id") == v3_runs["v3-remote-session"]["summary"]["injected_probe_env"]["CLAUDE_CODE_REMOTE_SESSION_ID"]
        and header_map(combination).get("x-claude-remote-session-id") == v3_runs["v3-header-combination"]["summary"]["injected_probe_env"]["CLAUDE_CODE_REMOTE_SESSION_ID"],
        ["v3-header-combination", "v3-remote-session"],
        [combination, remote_session],
    )
    add_v3_rule(
        "SPEC-HDR-026",
        request_header_names(combination)[15:23]
        == ["anthropic-version", "x-anthropic-additional-protection", "x-app", "x-claude-remote-container-id", "x-claude-remote-session-id", "x-client-app", "x-client-request-id", "Connection"],
        ["v3-header-combination"],
        [combination],
    )

    agent_sdk = one_message("v3-agent-sdk")
    workload = one_message("v3-workload")
    add_v3_rule(
        "SPEC-HDR-042",
        header_map(agent_sdk)["user-agent"].endswith("agent-sdk/9.9.9-fw-f-v3)")
        and "agent-sdk/9.9.9-fw-f-v3-combo" in header_map(combination)["user-agent"]
        and "agent-sdk/" not in baseline_headers["user-agent"],
        ["v3-agent-sdk", "v3-baseline", "v3-header-combination"],
        [agent_sdk, baseline_v3, combination],
        positive_count=2,
        negative_count=1,
    )
    add_v3_rule(
        "SPEC-HDR-043",
        header_map(workload)["user-agent"].endswith("workload/fw-f-v3-workload)")
        and "workload/fw-f-v3-combo-workload" in header_map(combination)["user-agent"]
        and "workload/" not in baseline_headers["user-agent"],
        ["v3-baseline", "v3-header-combination", "v3-workload"],
        [baseline_v3, combination, workload],
        positive_count=2,
        negative_count=1,
    )

    custom_header = one_message("v3-custom-header-grammar")
    custom_headers = header_map(custom_header)
    add_v3_rule(
        "SPEC-HDR-029",
        custom_headers.get("x-fw-f-probe") == "value:with:colon"
        and custom_headers.get("x-fw-f-trim") == "trimmed-value"
        and "badline" not in custom_headers,
        ["v3-custom-header-grammar"],
        [custom_header],
    )
    add_v3_rule(
        "SPEC-HDR-032",
        request_header_names(custom_header)[4:8]
        == ["X-Claude-Code-Session-Id", "X-FW-F-Probe", "X-FW-F-Trim", "X-Stainless-Arch"],
        ["v3-custom-header-grammar"],
        [custom_header],
    )
    add_v3_rule(
        "SPEC-HDR-031",
        custom_headers.get("x-client-request-id") != "11111111-2222-4333-8444-555555555555"
        and UUID_RE.fullmatch(custom_headers.get("x-client-request-id", "")) is not None,
        ["v3-custom-header-grammar"],
        [custom_header],
    )
    invalid_header_run = v3_runs["v3-custom-header-invalid-name"]
    add_v3_rule(
        "SPEC-HDR-030",
        invalid_header_run["messages"] == []
        and invalid_header_run["manifest"]["message_request_expectation"] == "zero"
        and invalid_header_run["summary"]["local_error_results"] == [True],
        ["v3-custom-header-invalid-name"],
        [],
        unit="run",
        eligible_count=1,
        include_all_client_requests=True,
    )

    beta_probe = one_message("v3-beta-deduplicate")
    beta_prefix, beta_effort = BETA_BASE_VALUE.rsplit(",effort-2025-11-24", 1)
    expected_beta = (
        beta_prefix
        + ",oauth-2025-04-20,claude-code-20250219,oauth-2025-04-20"
        + ",effort-2025-11-24"
        + beta_effort
        + ",extended-cache-ttl-2025-04-11"
    )
    add_v3_rule(
        "SPEC-BETA-008",
        header_map(beta_probe).get("anthropic-beta") == expected_beta,
        ["v3-baseline", "v3-beta-deduplicate"],
        [baseline_v3, beta_probe],
        positive_count=1,
        negative_count=1,
    )

    def same_body_fields(
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        excluded: Iterable[str],
    ) -> bool:
        """对比除动态身份或目标变量外的 Body 字段与顺序。"""

        omitted = set(excluded)
        left_body = left["body"]
        right_body = right["body"]
        left_keys = [key for key in left_body if key not in omitted]
        right_keys = [key for key in right_body if key not in omitted]
        return left_keys == right_keys and all(left_body[key] == right_body[key] for key in left_keys)

    def response_status(sample: dict[str, Any]) -> int | None:
        """读取当前请求所属连接的首个 HTTP 响应状态。"""

        path = sample["upstream_raw_path"]
        if not path.is_file():
            return None
        first_line = path.read_bytes().split(b"\r\n", 1)[0]
        match = re.fullmatch(br"HTTP/1\.[01] ([0-9]{3}) .+", first_line)
        return int(match.group(1)) if match is not None else None

    def response_header(sample: dict[str, Any], name: str) -> str | None:
        """读取当前请求对应响应的指定 Header。"""

        path = sample["upstream_raw_path"]
        if not path.is_file():
            return None
        raw_head = path.read_bytes().split(b"\r\n\r\n", 1)[0]
        lines = raw_head.decode("latin-1").split("\r\n")[1:]
        for line in lines:
            if ":" not in line:
                continue
            current_name, value = line.split(":", 1)
            if current_name.lower() == name.lower():
                return value.strip()
        return None

    def intervention_actions(probe_id: str) -> list[str]:
        """读取隔离故障 run 中 messages 的受控响应动作。"""

        run = v3_runs[probe_id]
        path = run["intervention_path"]
        require(path is not None, f"{probe_id} 缺少 intervention 记录")
        actions: list[str] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MeasuredRuleError(f"{probe_id} intervention[{line_number}] 不是 JSON") from exc
            require(isinstance(value, dict), f"{probe_id} intervention[{line_number}] 非对象")
            if value.get("message_ordinal", 0) > 0:
                action = value.get("action")
                require(isinstance(action, str) and action, f"{probe_id} intervention 缺少 action")
                actions.append(action)
        return actions

    # attribution、条件 Body、缓存和 system 组装。
    attribution_disabled = one_message("v3-attribution-disabled")
    add_v3_rule(
        "SPEC-BODY-017",
        attribution_fields(attribution_disabled) == {}
        and system_blocks(attribution_disabled) == baseline_system[1:]
        and same_body_fields(attribution_disabled, baseline_v3, excluded={"system", "metadata"}),
        ["v3-attribution-disabled", "v3-baseline"],
        [attribution_disabled, baseline_v3],
        positive_count=1,
        negative_count=1,
    )

    workload_fields = attribution_fields(workload)
    combination_fields = attribution_fields(combination)
    add_v3_rule(
        "SPEC-BODY-039",
        workload_fields.get("cc_workload") == "fw-f-v3-workload"
        and combination_fields.get("cc_workload") == "fw-f-v3-combo-workload"
        and "cc_workload" not in attribution_fields(baseline_v3)
        and list(workload_fields)[-1:] == ["cc_workload"]
        and list(combination_fields)[-1:] == ["cc_workload"],
        ["v3-baseline", "v3-header-combination", "v3-workload"],
        [baseline_v3, combination, workload],
        positive_count=2,
        negative_count=1,
    )

    extra_body = one_message("v3-extra-body")
    max_output = one_message("v3-max-output-tokens")
    token_overrides = [extra_body, max_output]
    add_v3_rule(
        "SPEC-BODY-040",
        all(
            value["body"].get("max_tokens") == 2048
            and not isinstance(value["body"].get("max_tokens"), bool)
            and same_body_fields(value, baseline_v3, excluded={"system", "metadata", "max_tokens"})
            and system_blocks(value)[1:] == baseline_system[1:]
            for value in token_overrides
        )
        and baseline_v3["body"].get("max_tokens") == 64000,
        ["v3-baseline", "v3-extra-body", "v3-max-output-tokens"],
        [baseline_v3, *token_overrides],
        positive_count=2,
        negative_count=1,
    )

    thinking_disabled = one_message("v3-thinking-disabled")
    add_v3_rule(
        "SPEC-BODY-041",
        list(thinking_disabled["body"])
        == ["model", "messages", "system", "tools", "metadata", "max_tokens", "output_config", "stream"]
        and "thinking" not in thinking_disabled["body"]
        and "context_management" not in thinking_disabled["body"]
        and same_body_fields(
            thinking_disabled,
            baseline_v3,
            excluded={"system", "metadata", "thinking", "context_management"},
        )
        and system_blocks(thinking_disabled)[1:] == baseline_system[1:],
        ["v3-baseline", "v3-thinking-disabled"],
        [baseline_v3, thinking_disabled],
        positive_count=1,
        negative_count=1,
    )

    adaptive_disabled = one_message("v3-adaptive-thinking-disabled")
    add_v3_rule(
        "SPEC-BODY-042",
        adaptive_disabled["body"].get("thinking") == {"type": "adaptive"}
        and adaptive_disabled["body"].get("context_management")
        == {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        and same_body_fields(adaptive_disabled, baseline_v3, excluded={"system", "metadata"})
        and system_blocks(adaptive_disabled)[1:] == baseline_system[1:],
        ["v3-adaptive-thinking-disabled", "v3-baseline"],
        [adaptive_disabled, baseline_v3],
        positive_count=1,
        negative_count=1,
    )

    gzip_request = one_message("v3-gzip-request")
    gzip_headers = header_map(gzip_request)
    add_v3_rule(
        "SPEC-BODY-043",
        gzip_request.get("content_encoding") == "gzip"
        and request_header_names(gzip_request)[17:20]
        == ["x-client-request-id", "Content-Encoding", "Connection"]
        and gzip_headers.get("content-encoding") == "gzip"
        and int(gzip_headers.get("content-length", "-1")) == gzip_request["raw_body_length"]
        and re.fullmatch(r"[0-9a-f]{64}", gzip_request.get("wire_body_sha256", "")) is not None
        and list(gzip_request["body"]) == BODY_KEY_ORDER
        and gzip_request["body"].get("model") == "claude-sonnet-5"
        and gzip_request["body"].get("max_tokens") == 64000
        and gzip_request["body"].get("thinking") == {"type": "adaptive"}
        and gzip_request["body"].get("context_management")
        == {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        and gzip_request["body"].get("output_config") == {"effort": "high"}
        and gzip_request["body"].get("stream") is True
        and messages_role_sequence(gzip_request) == ["user", "system"]
        and system_blocks(gzip_request)[1:] == baseline_system[1:]
        and user_metadata(gzip_request).get("session_id") == gzip_headers.get("x-claude-code-session-id"),
        ["v3-gzip-request"],
        [gzip_request],
    )

    custom_system = one_message("v3-custom-system")
    custom_system_blocks = system_blocks(custom_system)
    cache_1h = {"type": "ephemeral", "ttl": "1h"}
    add_v3_rule(
        "SPEC-BODY-044",
        len(custom_system_blocks) == 3
        and attribution_fields(custom_system).get("cc_entrypoint") == "sdk-cli"
        and custom_system_blocks[1]
        == {"type": "text", "text": PRODUCT_IDENTITY, "cache_control": cache_1h}
        and custom_system_blocks[2]
        == {"type": "text", "text": "FW-F v3 自定义系统提示词。", "cache_control": cache_1h},
        ["v3-custom-system"],
        [custom_system],
    )

    append_system = one_message("v3-append-system")
    append_blocks = system_blocks(append_system)
    append_text = "FW-F v3 追加系统提示词。"
    add_v3_rule(
        "SPEC-BODY-045",
        len(append_blocks) == 4
        and append_blocks[1]
        == {
            "type": "text",
            "text": "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.",
        }
        and append_blocks[2] == baseline_system[2]
        and append_blocks[3].get("cache_control") == cache_1h
        and append_blocks[3].get("text") == baseline_system[3].get("text") + "\n\n" + append_text,
        ["v3-append-system", "v3-baseline"],
        [append_system, baseline_v3],
    )

    exclude_dynamic = one_message("v3-exclude-dynamic-system")
    exclude_blocks = system_blocks(exclude_dynamic)
    add_v3_rule(
        "SPEC-BODY-046",
        len(exclude_blocks) == 4
        and exclude_blocks[1:3] == baseline_system[1:3]
        and exclude_blocks[3].get("cache_control") == baseline_system[3].get("cache_control")
        and exclude_blocks[3].get("text") != baseline_system[3].get("text")
        and hashlib.sha256(exclude_blocks[3].get("text", "").encode()).hexdigest()
        == "134f2e2776eaaf234befeab6e246e6d7f12d960861e488ffc17d0697ecce2ab8",
        ["v3-baseline", "v3-exclude-dynamic-system"],
        [baseline_v3, exclude_dynamic],
        positive_count=1,
        negative_count=1,
    )

    custom_agent = one_message("v3-custom-agent")
    custom_agent_blocks = system_blocks(custom_agent)
    add_v3_rule(
        "SPEC-BODY-047",
        len(custom_agent_blocks) == 3
        and custom_agent_blocks[1]
        == {"type": "text", "text": PRODUCT_IDENTITY, "cache_control": cache_1h}
        and custom_agent_blocks[2]
        == {"type": "text", "text": "你只执行固定出站取证任务。", "cache_control": cache_1h},
        ["v3-custom-agent"],
        [custom_agent],
    )

    cache_disabled = one_message("v3-cache-disabled")
    cache_sonnet_disabled = one_message("v3-cache-sonnet-disabled")
    cache_disabled_samples = [cache_disabled, cache_sonnet_disabled]
    add_v3_rule(
        "SPEC-CACHE-005",
        all(
            [block.get("text") for block in system_blocks(value)[1:]]
            == [block.get("text") for block in baseline_system[1:]]
            and all("cache_control" not in block for block in system_blocks(value))
            for value in cache_disabled_samples
        )
        and sum("cache_control" in block for block in baseline_system) == 2,
        ["v3-baseline", "v3-cache-disabled", "v3-cache-sonnet-disabled"],
        [baseline_v3, *cache_disabled_samples],
        positive_count=2,
        negative_count=1,
    )

    cache_one_hour = one_message("v3-cache-one-hour")
    add_v3_rule(
        "SPEC-CACHE-006",
        [block.get("text") for block in system_blocks(cache_one_hour)[1:]]
        == [block.get("text") for block in baseline_system[1:]]
        and [block.get("cache_control") for block in system_blocks(cache_one_hour)]
        == [block.get("cache_control") for block in baseline_system]
        == [None, None, {"type": "ephemeral", "ttl": "1h", "scope": "global"}, cache_1h],
        ["v3-baseline", "v3-cache-one-hour"],
        [baseline_v3, cache_one_hour],
        positive_count=1,
        negative_count=1,
    )

    extra_metadata = one_message("v3-extra-metadata")
    extra_metadata_value = user_metadata(extra_metadata)
    add_v3_rule(
        "SPEC-META-001",
        extra_metadata_value.get("fw_f_probe") == "v3"
        and extra_metadata_value.get("nested") == {"value": "measured"}
        and user_metadata(baseline_v3).get("fw_f_probe") is None,
        ["v3-baseline", "v3-extra-metadata"],
        [baseline_v3, extra_metadata],
        positive_count=1,
        negative_count=1,
    )
    add_v3_rule(
        "SPEC-META-002",
        list(extra_metadata_value)
        == ["fw_f_probe", "nested", "device_id", "account_uuid", "session_id"]
        and extra_metadata_value["nested"] == {"value": "measured"}
        and extra_metadata_value["session_id"] == header_map(extra_metadata).get("x-claude-code-session-id"),
        ["v3-extra-metadata"],
        [extra_metadata],
    )

    json_schema = one_message("v3-json-schema")
    expected_structured_tool = {
        "name": "StructuredOutput",
        "description": (
            "Use this tool to return your final response in the requested structured format. "
            "You MUST call this tool exactly once at the end of your response to provide the structured output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string", "const": "fw-f-v3-ok"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    add_v3_rule(
        "SPEC-TOOL-018",
        json_schema["body"].get("tools") == [expected_structured_tool]
        and json_schema["body"].get("output_config") == {"effort": "high"},
        ["v3-json-schema"],
        [json_schema],
    )

    # 真实 TUI、会话恢复和入口扩展。
    tui_messages = tui_run["messages"]
    require(len(tui_messages) == 2, "v3-tui messages 数量不是 2")
    tui_title = next((value for value in tui_messages if value["body"].get("model") == "claude-haiku-4-5-20251001"), None)
    tui_main = next((value for value in tui_messages if value["body"].get("model") == "claude-sonnet-5"), None)
    require(tui_title is not None and tui_main is not None, "v3-tui 缺少标题或主推理请求")
    title_system = system_blocks(tui_title)
    title_body = tui_title["body"]
    title_output = {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
        }
    }
    add_v3_rule(
        "SPEC-BODY-048",
        list(title_body)
        == ["model", "messages", "system", "tools", "metadata", "max_tokens", "thinking", "temperature", "output_config", "stream"]
        and title_body.get("model") == "claude-haiku-4-5-20251001"
        and title_body.get("max_tokens") == 32000
        and title_body.get("thinking") == {"type": "disabled"}
        and title_body.get("temperature") == 1
        and not isinstance(title_body.get("temperature"), bool)
        and title_body.get("output_config") == title_output
        and title_body.get("stream") is True
        and title_body.get("tools") == []
        and messages_role_sequence(tui_title) == ["user"]
        and len(title_system) == 3
        and title_system[1] == {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
        and hashlib.sha256(title_system[2].get("text", "").encode()).hexdigest()
        == "32b62cdd87ea0563dd904f0111ed7849fb795ab73722d78196ef984a4c0e0829",
        ["v3-tui"],
        [tui_title],
        applicability=TUI_TITLE_APPLICABILITY,
    )

    tui_main_system = system_blocks(tui_main)
    tui_main_headers = header_map(tui_main)
    tui_main_beta = (
        "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,"
        "redact-thinking-2026-02-12,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "mid-conversation-system-2026-04-07,effort-2025-11-24,extended-cache-ttl-2025-04-11"
    )
    add_v3_rule(
        "SPEC-BODY-049",
        attribution_fields(tui_main).get("cc_entrypoint") == "cli"
        and tui_main_headers.get("user-agent") == "claude-cli/2.1.226 (external, cli)"
        and tui_main_headers.get("anthropic-beta") == tui_main_beta
        and messages_role_sequence(tui_main) == ["user"]
        and len(tui_main_system) == 4
        and tui_main_system[1] == {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
        and tui_main_system[2] == baseline_system[2]
        and tui_main_system[3].get("cache_control") == cache_1h
        and tui_main_headers.get("x-claude-code-session-id") == header_map(tui_title).get("x-claude-code-session-id"),
        ["v3-tui"],
        [tui_title, tui_main],
        applicability=TUI_MAIN_APPLICABILITY,
    )

    fallback_messages = v3_runs["v3-fallback-model"]["messages"]
    require(len(fallback_messages) == 4, "v3-fallback-model messages 数量不是 4")
    fallback_haiku = fallback_messages[3]
    fallback_body = fallback_haiku["body"]
    fallback_headers = header_map(fallback_haiku)
    fallback_beta = (
        "oauth-2025-04-20,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
        "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
        "claude-code-20250219,extended-cache-ttl-2025-04-11"
    )
    add_v3_rule(
        "SPEC-BODY-050",
        list(fallback_body)
        == ["model", "messages", "system", "tools", "metadata", "max_tokens", "thinking", "context_management", "stream"]
        and fallback_body.get("model") == "claude-haiku-4-5"
        and fallback_body.get("max_tokens") == 32000
        and fallback_body.get("thinking") == {"budget_tokens": 31999, "type": "enabled"}
        and "output_config" not in fallback_body
        and messages_role_sequence(fallback_haiku) == ["user"]
        and fallback_headers.get("anthropic-beta") == fallback_beta
        and fallback_headers.get("x-claude-code-session-id")
        == header_map(fallback_messages[0]).get("x-claude-code-session-id")
        and user_metadata(fallback_haiku) == user_metadata(fallback_messages[0])
        and system_blocks(fallback_haiku)[1:] == system_blocks(fallback_messages[0])[1:],
        ["v3-fallback-model"],
        fallback_messages,
        positive_count=1,
        negative_count=3,
        include_responses=True,
        applicability=FALLBACK_APPLICABILITY,
    )

    safe_agent_run = v3_runs["v3-custom-agent-safe-mode"]
    add_v3_rule(
        "SPEC-STATE-005",
        safe_agent_run["messages"] == []
        and safe_agent_run["manifest"].get("message_request_expectation") == "zero"
        and safe_agent_run["summary"].get("local_error_results") == [True],
        ["v3-custom-agent-safe-mode"],
        [],
        unit="run",
        eligible_count=1,
        include_all_client_requests=True,
    )

    resume_messages = v3_runs["v3-session-resume"]["messages"]
    fork_messages = v3_runs["v3-session-fork"]["messages"]
    require(len(resume_messages) == len(fork_messages) == 2, "resume/fork messages 数量不是各 2")
    resume_sessions = [header_map(value).get("x-claude-code-session-id") for value in resume_messages]
    fork_sessions = [header_map(value).get("x-claude-code-session-id") for value in fork_messages]
    add_v3_rule(
        "SPEC-STATE-006",
        len(set(resume_sessions)) == 1
        and UUID_RE.fullmatch(resume_sessions[0] or "") is not None
        and all(user_metadata(value).get("session_id") == resume_sessions[0] for value in resume_messages)
        and messages_role_sequence(resume_messages[1]) == ["user", "system", "assistant", "user", "system"]
        and re.fullmatch(r"req_[A-Za-z0-9]+", attribution_fields(resume_messages[1]).get("cc_prev_req", "")) is not None
        and "cc_prev_req" not in attribution_fields(resume_messages[0]),
        ["v3-session-resume"],
        resume_messages,
        unit="session-transition",
        eligible_count=1,
    )
    add_v3_rule(
        "SPEC-STATE-007",
        len(set(fork_sessions)) == 2
        and all(UUID_RE.fullmatch(value or "") is not None for value in fork_sessions)
        and all(user_metadata(value).get("session_id") == fork_sessions[index] for index, value in enumerate(fork_messages))
        and messages_role_sequence(fork_messages[1]) == ["user", "system", "assistant", "user", "system"]
        and re.fullmatch(r"req_[A-Za-z0-9]+", attribution_fields(fork_messages[1]).get("cc_prev_req", "")) is not None
        and "cc_prev_req" not in attribution_fields(fork_messages[0]),
        ["v3-session-fork"],
        fork_messages,
        unit="session-transition",
        eligible_count=1,
    )

    session_and_tui_messages = [*resume_messages, *fork_messages, tui_title, tui_main]
    expected_roles_by_scenario = {
        "v3-session-resume": [["user", "system"], ["user", "system", "assistant", "user", "system"]],
        "v3-session-fork": [["user", "system"], ["user", "system", "assistant", "user", "system"]],
        "v3-tui": [["user"], ["user"]],
    }
    augment_rule_with_v3(
        "SPEC-BODY-004",
        all(
            [messages_role_sequence(value) for value in v3_runs[probe_id]["messages"]]
            == expected
            for probe_id, expected in expected_roles_by_scenario.items()
        ),
        expected_roles_by_scenario,
        session_and_tui_messages,
        applicability=MIXED_ENTRYPOINT_APPLICABILITY,
    )

    effort_samples = {
        "high": baseline_v3,
        "low": one_message("v3-effort-low"),
        "max": one_message("v3-effort-max"),
        "medium": one_message("v3-effort-medium"),
        "xhigh": one_message("v3-effort-xhigh"),
    }
    augment_rule_with_v3(
        "SPEC-BODY-012",
        all(value["body"].get("output_config") == {"effort": effort} for effort, value in effort_samples.items()),
        ["v3-baseline", "v3-effort-low", "v3-effort-max", "v3-effort-medium", "v3-effort-xhigh"],
        effort_samples.values(),
    )

    attributed_v3_messages = [
        value
        for run in v3_runs.values()
        for value in run["messages"]
        if attribution_fields(value)
    ]
    augment_rule_with_v3(
        "SPEC-BODY-015",
        all(
            attribution_fields(value).get("cc_entrypoint")
            == ("cli" if value["scenario"] == "v3-tui" else "sdk-cli")
            for value in attributed_v3_messages
        ),
        sorted({value["scenario"] for value in attributed_v3_messages}),
        attributed_v3_messages,
        applicability=MIXED_ENTRYPOINT_APPLICABILITY,
    )
    augment_rule_with_v3(
        "SPEC-BODY-018",
        all("cc_prev_req" not in attribution_fields(values[0]) for values in (resume_messages, fork_messages))
        and all(
            re.fullmatch(r"req_[A-Za-z0-9]+", attribution_fields(values[1]).get("cc_prev_req", "")) is not None
            for values in (resume_messages, fork_messages)
        ),
        ["v3-session-fork", "v3-session-resume"],
        [*resume_messages, *fork_messages],
    )

    # non-stream fallback 与受控故障重试矩阵。
    nonstream_ids = ["v3-stream-404-disable-flag", "v3-stream-404-fallback", "v3-stream-interrupt"]
    nonstream_requests = [value for probe_id in nonstream_ids for value in v3_runs[probe_id]["messages"]]
    nonstream_ok = True
    for probe_id in nonstream_ids:
        streaming, fallback = v3_runs[probe_id]["messages"]
        streaming_fields = attribution_fields(streaming)
        fallback_fields = attribution_fields(fallback)
        nonstream_ok = nonstream_ok and (
            streaming["body"].get("stream") is True
            and "stream" not in fallback["body"]
            and list(fallback["body"]) == [key for key in streaming["body"] if key != "stream"]
            and same_body_fields(streaming, fallback, excluded={"system", "stream"})
            and system_blocks(streaming)[1:] == system_blocks(fallback)[1:]
            and {key: value for key, value in streaming_fields.items() if key != "cch"}
            == {key: value for key, value in fallback_fields.items() if key != "cch"}
            and streaming_fields.get("cch") != fallback_fields.get("cch")
            and header_map(streaming).get("x-stainless-timeout") == "600"
            and header_map(fallback).get("x-stainless-timeout") == "300"
            and header_map(streaming).get("x-claude-code-session-id")
            == header_map(fallback).get("x-claude-code-session-id")
            and header_map(streaming).get("x-client-request-id")
            != header_map(fallback).get("x-client-request-id")
        )
    add_v3_rule(
        "SPEC-BODY-032",
        nonstream_ok,
        nonstream_ids,
        nonstream_requests,
        unit="fallback-transition",
        eligible_count=3,
        include_responses=True,
    )

    retry_timing_ids = ["v3-fallback-model", "v3-retry-limit"]
    retry_timing_requests = [value for probe_id in retry_timing_ids for value in v3_runs[probe_id]["messages"]]
    fallback_gaps = [
        right["connection"]["opened_at_unix_ms"] - left["connection"]["opened_at_unix_ms"]
        for left, right in zip(fallback_messages[:2], fallback_messages[1:3])
    ]
    retry_limit_messages = v3_runs["v3-retry-limit"]["messages"]
    retry_limit_gaps = [
        right["connection"]["opened_at_unix_ms"] - left["connection"]["opened_at_unix_ms"]
        for left, right in zip(retry_limit_messages, retry_limit_messages[1:])
    ]
    add_v3_rule(
        "SPEC-CONN-002",
        500 <= fallback_gaps[0] <= 750
        and 1000 <= fallback_gaps[1] <= 1250
        and 500 <= retry_limit_gaps[0] <= 750
        and 1000 <= retry_limit_gaps[1] <= 1250,
        retry_timing_ids,
        retry_timing_requests,
        unit="retry-interval",
        eligible_count=4,
        include_responses=True,
    )

    retry_statuses = [401, 408, 409, 429, 500, 502, 503, 529]
    retry_matrix_ids = [f"v3-retry-{status}" for status in retry_statuses]
    nonretry_ids = ["v3-nonretry-400", "v3-nonretry-403"]
    retry_matrix_requests = [
        value
        for probe_id in [*retry_matrix_ids, *nonretry_ids]
        for value in v3_runs[probe_id]["messages"]
    ]
    retry_matrix_ok = all(
        len(v3_runs[probe_id]["messages"]) == 2
        and response_status(v3_runs[probe_id]["messages"][0]) == status
        and response_status(v3_runs[probe_id]["messages"][1]) == 200
        for probe_id, status in zip(retry_matrix_ids, retry_statuses)
    )
    retry_matrix_ok = retry_matrix_ok and all(
        len(v3_runs[probe_id]["messages"]) == 1
        and response_status(v3_runs[probe_id]["messages"][0]) == status
        for probe_id, status in zip(nonretry_ids, [400, 403])
    )
    add_v3_rule(
        "SPEC-CONN-010",
        retry_matrix_ok,
        [*retry_matrix_ids, *nonretry_ids],
        retry_matrix_requests,
        unit="fault-scenario",
        eligible_count=10,
        positive_count=8,
        negative_count=2,
        include_responses=True,
    )

    after_seconds = v3_runs["v3-retry-after-seconds"]["messages"]
    after_date = v3_runs["v3-retry-after-date"]["messages"]
    after_seconds_gap = after_seconds[1]["connection"]["opened_at_unix_ms"] - after_seconds[0]["connection"]["opened_at_unix_ms"]
    after_date_gap = after_date[1]["connection"]["opened_at_unix_ms"] - after_date[0]["connection"]["opened_at_unix_ms"]
    add_v3_rule(
        "SPEC-CONN-016",
        response_status(after_seconds[0]) == response_status(after_date[0]) == 429
        and response_header(after_seconds[0], "retry-after") == "1"
        and re.fullmatch(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} "
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
            r"\d{4} \d{2}:\d{2}:\d{2} GMT",
            response_header(after_date[0], "retry-after") or "",
        )
        is not None
        and 1000 <= after_seconds_gap <= 1100
        and 500 <= after_date_gap <= 700,
        ["v3-retry-after-date", "v3-retry-after-seconds"],
        [*after_date, *after_seconds],
        unit="retry-after-scenario",
        eligible_count=2,
        include_responses=True,
    )

    fallback_behavior = {
        "v3-stream-404-disable-flag": (2, ["stream_404", "nonstream_fallback_success"]),
        "v3-stream-404-fallback": (2, ["stream_404", "nonstream_fallback_success"]),
        "v3-stream-interrupt": (2, ["stream_interrupted", "interrupt_nonstream_success"]),
        "v3-stream-interrupt-no-fallback": (1, ["stream_interrupted"]),
    }
    fallback_behavior_requests = [
        value
        for probe_id in fallback_behavior
        for value in v3_runs[probe_id]["messages"]
    ]
    add_v3_rule(
        "SPEC-CONN-018",
        all(
            len(v3_runs[probe_id]["messages"]) == expected_count
            and intervention_actions(probe_id) == expected_actions
            for probe_id, (expected_count, expected_actions) in fallback_behavior.items()
        ),
        fallback_behavior,
        fallback_behavior_requests,
        unit="fault-scenario",
        eligible_count=4,
        positive_count=3,
        negative_count=1,
        include_responses=True,
    )

    disconnect_messages = v3_runs["v3-disconnect-retry"]["messages"]
    add_v3_rule(
        "SPEC-CONN-020",
        len(disconnect_messages) == 2
        and intervention_actions("v3-disconnect-retry")
        == ["disconnect_without_response", "disconnect_retry_success"]
        and header_map(disconnect_messages[0]).get("connection") == "keep-alive"
        and "connection" not in header_map(disconnect_messages[1])
        and 500
        <= disconnect_messages[1]["connection"]["opened_at_unix_ms"]
        - disconnect_messages[0]["connection"]["opened_at_unix_ms"]
        <= 700,
        ["v3-disconnect-retry"],
        disconnect_messages,
        unit="retry-transition",
        eligible_count=1,
        include_responses=True,
    )

    retry_identity_ids = [
        *retry_matrix_ids,
        "v3-retry-after-date",
        "v3-retry-after-seconds",
        "v3-disconnect-retry",
        "v3-retry-limit",
        "v3-fallback-model",
    ]
    retry_identity_requests: list[dict[str, Any]] = []
    retry_identity_ok = True
    retry_transition_count = 0
    for probe_id in retry_identity_ids:
        values = v3_runs[probe_id]["messages"]
        if probe_id == "v3-fallback-model":
            values = values[:3]
        retry_identity_requests.extend(values)
        retry_transition_count += len(values) - 1
        bodies = {canonical_sha256(value["body"]) for value in values}
        sessions = {header_map(value).get("x-claude-code-session-id") for value in values}
        request_ids = [header_map(value).get("x-client-request-id", "") for value in values]
        retry_identity_ok = retry_identity_ok and (
            len(bodies) == 1
            and len(sessions) == 1
            and len(set(request_ids)) == len(request_ids)
            and all(UUID_RE.fullmatch(value) is not None for value in request_ids)
            and all(header_map(value).get("x-stainless-retry-count") == "0" for value in values)
        )
    add_v3_rule(
        "SPEC-CONN-021",
        retry_identity_ok,
        retry_identity_ids,
        retry_identity_requests,
        unit="retry-transition",
        eligible_count=retry_transition_count,
        include_responses=True,
    )

    timeout_messages = v3_runs["v3-timeout"]["messages"]
    add_v3_rule(
        "SPEC-CONN-022",
        len(retry_limit_messages) == 3
        and [value["body"].get("model") for value in fallback_messages]
        == ["claude-sonnet-5", "claude-sonnet-5", "claude-sonnet-5", "claude-haiku-4-5"]
        and len(timeout_messages) == 1,
        ["v3-fallback-model", "v3-retry-limit", "v3-timeout"],
        [*fallback_messages, *retry_limit_messages, *timeout_messages],
        unit="configured-run",
        eligible_count=3,
        include_responses=True,
        applicability=FALLBACK_APPLICABILITY,
    )
    add_v3_rule(
        "SPEC-CONN-023",
        len(fallback_messages) == 4
        and all(response_status(value) == 529 for value in fallback_messages[:3])
        and response_status(fallback_messages[3]) == 200
        and all(value["body"].get("model") == "claude-sonnet-5" for value in fallback_messages[:3])
        and fallback_messages[3]["body"].get("model") == "claude-haiku-4-5",
        ["v3-fallback-model"],
        fallback_messages,
        unit="fallback-transition",
        eligible_count=1,
        include_responses=True,
        applicability=FALLBACK_APPLICABILITY,
    )
    add_v3_rule(
        "SPEC-CONN-024",
        len(timeout_messages) == 1
        and header_map(timeout_messages[0]).get("x-stainless-timeout") == "1"
        and header_map(timeout_messages[0]).get("x-stainless-retry-count") == "0"
        and intervention_actions("v3-timeout") == ["stall_without_response"],
        ["v3-timeout"],
        timeout_messages,
        unit="configured-run",
        eligible_count=1,
        include_responses=True,
    )

    require(set(records) == set(expected_ids), "实测规则断言没有精确覆盖策略闭集")
    identity_refs = [binding(campaign_identity_path, "M"), binding(relay_index_path, "M")]
    for record in records.values():
        by_ref = {(value["path"], value["channel"]): value for value in record["evidence_refs"] + identity_refs}
        record["evidence_refs"] = [by_ref[key] for key in sorted(by_ref)]
        require(any(value["channel"] == "R" for value in record["evidence_refs"]), f"{record['spec_id']} 缺少真实 R 证据")

    entries = [records[spec_id] for spec_id in sorted(records)]
    return {
        "schema_version": LEDGER_SCHEMA,
        "target_version": target_version,
        "target_binary_sha256": binary_sha256,
        "privacy_environment": identity["privacy_environment"],
        "rule_count": len(entries),
        "evidence_level_counts": {"observed": len(entries), "verified": 0},
        "sample_counts": {
            "inference_request_count": len(inference),
            "lifecycle_request_count": len(lifecycle),
            "selected_connection_count": len(connection_samples),
            "scenario_count": 4,
        },
        "evidence_boundaries": {
            "native_pcap_present": False,
            "clienthello_fingerprint_rules_allowed": False,
            "client_alpn_offer_observed": False,
            "traffic_presence_comparison": "excluded_by_official_privacy_configuration",
            "unmeasured_feature_rules_allowed": False,
        },
        "input_bindings": {
            "campaign_identity": binding(campaign_identity_path, "M"),
            "relay_index": binding(relay_index_path, "M"),
            "generator": binding(Path(__file__), "M"),
            "http_parser": binding(ROOT / "tools/official_client_capture/claude_fw_f_profile.py", "M"),
            "discovery_policy_sha256": canonical_sha256(discovery_policy),
            "profile_policy_sha256": canonical_sha256(profile_policy),
        },
        "entries": entries,
        "result": "passed",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-policy", required=True, type=Path)
    parser.add_argument("--profile-policy", required=True, type=Path)
    parser.add_argument("--campaign-identity", required=True, type=Path)
    parser.add_argument("--relay-index", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """生成台账并输出摘要。"""

    args = parse_args(argv)
    try:
        ledger = build_ledger(
            load_json(args.discovery_policy),
            load_json(args.profile_policy),
            args.campaign_identity.resolve(),
            args.relay_index.resolve(),
        )
        require(not args.output.exists(), f"输出已存在，禁止覆盖：{args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json_bytes(ledger))
    except MeasuredRuleError as exc:
        print(f"FW-F 实测规则生成失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"result": "passed", "rule_count": ledger["rule_count"], "evidence_level_counts": ledger["evidence_level_counts"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
