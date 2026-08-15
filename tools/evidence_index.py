#!/usr/bin/env python3
"""生成 Codex CLI 0.145.0 规则与官方证据的双向索引。

索引只接受官方 Codex 客户端证据。Sub2API 出站验收可用于第三部分的实现比对，
但不得进入官方规则证据表；一旦规则的实测段误引该来源，本脚本直接失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from dataclasses import dataclass

import spec_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
OUT = ROOT / "docs" / "EVIDENCE_INDEX.md"
CAPTURES = ROOT / "local-analysis" / "captures"
RAW = CAPTURES / "raw-scrubbed"
SUMMARY_DIRS = [
    CAPTURES / "wire-parity-fix-20260727" / "relay",
    CAPTURES / "wire-parity-fix-20260727" / "h1-wire-probe",
    CAPTURES / "wire-parity-fix-20260727" / "h2-wire-probe",
]
BASELINE_ROOTS = [
    CAPTURES / "wire-parity-fix-20260727" / "official-baseline",
    CAPTURES / "profile-fidelity-fix-20260727" / "official-baseline",
]
FINAL_REVIEW_ROOT = (
    CAPTURES
    / "official-egress-final-review-fix-20260727-094500"
    / "official-client"
)
FORBIDDEN_ROOT = CAPTURES / "wire-parity-fix-20260727" / "sub2api-egress"
H2_PROBE = (
    CAPTURES
    / "wire-parity-fix-20260727"
    / "h2-wire-probe"
    / "official-baseline-official-h2-20260727T131936Z.json"
)
WHAM_CONSUME = RAW / "audit-ep019-wham-consume-safe-20260730a"

OFFICIAL_VERSION = "codex-cli 0.145.0"
OFFICIAL_SHA256 = "a2a05dafaa1acb002a45eaec0a462de5b13694fcfcd7bc43305f14781ce7be14"

HEAD_RE = re.compile(r"^#+ (SPEC-[A-Z0-9]+-\d+)(?:\s*[~/]\s*(\d+))?", re.M)
ANY_HEAD_RE = re.compile(r"^#{1,6} \S", re.M)
RUN_RE = re.compile(r"[A-Za-z][\w-]*?-?20260\d{3}T\d{6}Z")
TS_RE = re.compile(r"(20260\d{3}T\d{6}Z)")
NEG_RE = re.compile(
    r"验不了|不能验|不适用|无[^。；\n]{0,6}子目录|只跑了|错样本|已作废|"
    r"不含[^。；\n]{0,8}帧|不是本条|与本条无关|反过来"
)


@dataclass(frozen=True)
class EvidenceRef:
    run: str
    scope: str
    channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidencePath:
    kind: str
    rendered: str


# 词法扫描适合大多数独立标题；下列条目需要把“哪个运行证明哪一部分”钉死，
# 避免范围标题、相邻说明或同一 manifest 的多通道造成串挂。
RULE_EVIDENCE_OVERRIDES: dict[str, tuple[EvidenceRef, ...]] = {
    "SPEC-PROTO-001": (
        EvidenceRef(
            "oauth-20260727T091556Z-noplugins",
            "N0 pcap 直接证明未配置自定义 CA 的 ClientHello 不含 ALPN 扩展",
        ),
    ),
    "SPEC-PROTO-002": (
        EvidenceRef(
            "official-httpfb3-20260727T234853Z",
            "J 类摘要记录 WS 重试耗尽后同进程发出 HTTP POST /responses",
        ),
        EvidenceRef(
            "audit-ep014-turnstate-echo-20260730a",
            "R 类原始字节记录受控 426 后同进程从 WS 降级到 HTTP POST；"
            "只证明降级结果，不代替重试耗尽条件的 J 类记录",
        ),
    ),
    "SPEC-TLS-002": (
        EvidenceRef(
            "official-h2-20260727T131936Z",
            "配置自定义 CA 后直接证明 negotiated_alpn=h2",
        ),
        EvidenceRef(
            "audit-tls002-ca-n0-20260730a",
            "N0 pcap 直接证明配 CA 的 HTTP ClientHello 为 10 cipher，并依次 offer h2、http/1.1",
        ),
    ),
    "SPEC-CONN-001": (
        EvidenceRef("clean2-conn-20260728T132008Z", "正常无重试的多轮主模型调用：跨调用各自使用独立连接"),
        EvidenceRef("audit-conn001-image-repeat-20260730a", "同进程两次 images 上层调用落在两条独立 TCP"),
        EvidenceRef("audit-conn001-search-repeat-20260730a", "同进程两次 alpha-search 上层调用落在两条独立 TCP"),
        EvidenceRef(
            "audit-conn001-retry-keepalive-openai-http-20260730a",
            "内置 OpenAI OAuth 的同一次 Responses 调用：500 后 retry 复用存活的同一 TCP",
        ),
        EvidenceRef(
            "audit-conn001-retry-disconnect-openai-http-20260730a",
            "内置 OpenAI OAuth 的同一次 Responses 调用：断连后 retry 由同一 Client 新建 TCP",
        ),
    ),
    "SPEC-H1-001": (
        EvidenceRef("audit-h1raw-20260730a", "R 类 models／responses 原始字节直接证明 HTTP header 名全小写"),
    ),
    "SPEC-H1-002": (
        EvidenceRef("audit-h1raw-20260730a", "R 类 models／responses 原始字节直接证明 host 位于用户头之后"),
    ),
    "SPEC-H1-003": (
        EvidenceRef("audit-h1raw-20260730a", "R 类 POST /responses 原始字节直接证明 content-length 位于 host 之后"),
    ),
    "SPEC-H2-001": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明 SETTINGS 参数顺序"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验，不用于计数或缺失命题"),
    ),
    "SPEC-H2-002": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明 ENABLE_PUSH=0"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验"),
    ),
    "SPEC-H2-003": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明 INITIAL_WINDOW_SIZE=2097152"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验"),
    ),
    "SPEC-H2-004": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明 MAX_FRAME_SIZE=16384"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验"),
    ),
    "SPEC-H2-005": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明 MAX_HEADER_LIST_SIZE=16384"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验"),
    ),
    "SPEC-H2-006": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明首个连接级 WINDOW_UPDATE=5177345"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作正向原始帧交叉核验"),
    ),
    "SPEC-H2-007": (
        EvidenceRef("official-h2-20260727T131936Z", "J 类 3/3 完整连接直接证明四个请求伪头的顺序"),
        EvidenceRef("relay-h2-20260728T032147Z", "R 类只作 HPACK 正向交叉核验"),
    ),
    "SPEC-WS-001": (
        EvidenceRef("clean-tool-20260728T132346Z", "R 类 WS 握手原始字节直接证明前五项大小写与顺序"),
    ),
    "SPEC-WS-002": (
        EvidenceRef("clean-tool-20260728T132346Z", "R 类 WS 握手原始字节直接证明剩余项大小写与实际线序"),
    ),
    "SPEC-WS-005": (
        EvidenceRef("clean-tool-20260728T132346Z", "Lite WS 原始帧与键序"),
        EvidenceRef(
            "audit-ws005-nonlite-20260730a",
            "非 Lite WS 原始帧：warmup／增量键序及 instructions／tools 正向形态",
        ),
    ),
    "SPEC-BODY-001": (
        EvidenceRef("audit-ep014-turnstate-echo-20260730a", "可重解析的 HTTP Lite R 类原始 body"),
        EvidenceRef("audit-body002-plain-20260730a", "可重解析的 HTTP 非 Lite R 类原始 body"),
        EvidenceRef("clean-tool-20260728T132346Z", "官方 WS Lite 结构实例"),
        EvidenceRef("audit-ws005-nonlite-20260730a", "官方 WS 非 Lite 结构实例"),
    ),
    "SPEC-BODY-003": (
        EvidenceRef("audit-ep014-turnstate-echo-20260730a", "HTTP Lite R 类原始 body 直接证明 Lite 变换"),
        EvidenceRef("clean-tool-20260728T132346Z", "WS Lite 变换"),
    ),
    "SPEC-BODY-004": (
        EvidenceRef(
            "audit-ep014-turnstate-echo-20260730a",
            "受控 HTTP 响应头下发 turn-state 后，官方客户端在同一 turn 的后续 /responses 原样回送",
        ),
        EvidenceRef(
            "audit-ep014-turnstate-compact-20260730a",
            "受控 HTTP 响应头下发 turn-state 后，官方客户端在同一 turn 的 legacy compact 原样回送",
        ),
        EvidenceRef(
            "audit-body004-ws-turnstate-20260730a",
            "受控 WS response.metadata 下发 turn-state 后，官方客户端后续 2 个 "
            "response.create 均在 client_metadata 中原样回送；3/3 连接双向完整",
        ),
    ),
    "SPEC-BODY-005": (
        EvidenceRef("audit-ep014-turnstate-echo-20260730a", "HTTP Lite R 类原始 body：tool_choice=str:auto"),
        EvidenceRef("audit-body002-plain-20260730a", "HTTP 非 Lite R 类原始 body：tool_choice=str:auto"),
        EvidenceRef("clean-tool-20260728T132346Z", "WS Lite 原始帧：tool_choice=str:auto"),
        EvidenceRef("audit-ws005-nonlite-20260730a", "WS 非 Lite 原始帧：tool_choice=str:auto"),
    ),
    "SPEC-BODY-006": (
        EvidenceRef("audit-ep014-turnstate-echo-20260730a", "HTTP Lite R 类原始 body 的实际字段形态"),
        EvidenceRef("audit-body002-plain-20260730a", "HTTP 非 Lite R 类原始 body 的实际字段形态"),
    ),
    "SPEC-BODY-007": (
        EvidenceRef(
            "audit-body007-workflow-clean-20260730a",
            "洁净十轮编码工作流：20/20 双向，377 个 input 项、最大 95；"
            "计数只适用于该固定场景，不是协议封闭集合",
        ),
    ),
    "SPEC-EP-001": (
        EvidenceRef(
            "audit-body002-plain-20260730a",
            "R 类 HTTP 非 Lite 原始 body 中的 namespace image_gen 工具呈现",
        ),
        EvidenceRef("clean-image-20260728T132405Z", "Lite 工具呈现与 generations 调用"),
        EvidenceRef("relay-imgedit1", "Lite 工具呈现与 edits 调用"),
    ),
    "SPEC-EP-002": (
        EvidenceRef("oauth-ep002-allhosts", "无 host 过滤 pcap：普通 OAuth 会话的 SNI 集合"),
        EvidenceRef("oauth-ep002-refresh", "无 host 过滤 pcap：真实 token 刷新访问 auth.openai.com"),
        EvidenceRef("audit-ep012-sideband-synth-20260730a", "受控首跳 200 后由官方 CLI 派生的 api.openai.com sideband"),
        EvidenceRef(
            "audit-ep002-file-upload-full2-20260730a",
            "生产文件上传三跳：chatgpt 首跳、服务端返回的 oaiusercontent PUT、chatgpt uploaded 确认；"
            "10/10 双向完整，预签名 query 已等长脱敏",
        ),
    ),
    "SPEC-EP-006": (
        EvidenceRef("official-body2-20260728T000549Z", "仅证明 models URL 与方法"),
        EvidenceRef("audit-h1raw-20260730a", "R 类原始请求直接证明 models URL 与方法"),
    ),
    "SPEC-EP-007": (
        EvidenceRef("clean-legacy-20260728T132509Z", "仅证明 legacy compact URL 与方法"),
    ),
    "SPEC-EP-008": (
        EvidenceRef("clean-search-20260728T132311Z", "仅证明 alpha-search URL 与方法"),
    ),
    "SPEC-EP-009": (
        EvidenceRef("webrtc-20260728T134028Z", "仅证明 realtime/calls 第一跳 URL 与方法"),
        EvidenceRef("live2-20260728T140403Z", "仅证明 realtime/calls 第一跳 URL 与方法"),
    ),
    "SPEC-EP-013": (
        EvidenceRef("audit-h1raw-20260730a", "R 类原始请求证明 models 固定 query、responses 无 query"),
        EvidenceRef("webrtc-20260728T134028Z", "realtime/calls 端点固定 query"),
    ),
    "SPEC-EP-019": (
        EvidenceRef(
            "clean-legacy-20260728T132509Z",
            "洁净原始字节证明 wham usage／rate-limit-reset-credits 两个 GET 路径及 5 项 header 线序",
        ),
        EvidenceRef(
            "audit-ep019-wham-consume-safe-20260730a",
            "无外网、全假 OAuth、本地 TLS 终结器下由官方 app-server 生产代码生成的 consume 请求行、7 项 header 线序与 body",
        ),
    ),
    "SPEC-EP-023": (
        EvidenceRef(
            "relay-tui-recap-20260728T112358Z",
            "洁净 TUI 原始字节中的 user_requested compaction_trigger 结果旁证",
        ),
        EvidenceRef(
            "audit-ep021-auto-clean-20260730a",
            "洁净自动压缩原始字节中的 context_limit compaction_trigger 结果旁证",
        ),
        EvidenceRef(
            "audit-ep023-comphash-20260730b",
            "生产模型目录自然换模后，官方 V2 压缩帧精确标记 "
            "reason=comp_hash_changed；只旁证可见结果，不证明完整内部分派",
        ),
        EvidenceRef(
            "audit-ep023-downshift-20260730b",
            "I 类受控阈值下，官方 V2 压缩帧精确标记 reason=model_downshift；"
            "只旁证条件成立后的结果，不代表默认生产阈值",
        ),
    ),
    "SPEC-EP-024": (
        EvidenceRef(
            "audit-ep024-exec-negative-clean-20260730a",
            "洁净 codex exec 负例：/compact 精确作为普通 user message 发出；"
            "3/3 双向、compaction_trigger=0、/responses/compact=0",
        ),
        EvidenceRef(
            "relay-tui-recap-20260728T112358Z",
            "洁净真 TUI 运行（96/96 双向）实际解析 /compact 并发出 compaction_trigger",
        ),
    ),
    "SPEC-HDR-004": (
        EvidenceRef("clean-tool-20260728T132346Z", "R 类 WS 握手原始字节中的 openai-beta 正例"),
        EvidenceRef("audit-body002-plain-20260730a", "R 类 HTTP /responses 原始字节中的无 openai-beta 反例"),
        EvidenceRef("clean-image-20260728T132405Z", "R 类 images/generations 原始字节中的无 openai-beta 反例"),
        EvidenceRef("relay-imgedit1", "R 类 images/edits 原始字节中的无 openai-beta 反例"),
    ),
    "SPEC-HDR-005": (
        EvidenceRef("clean-search-20260728T132311Z", "codex_exec UA 与 suffix 实例"),
        EvidenceRef("clean-legacy-20260728T132509Z", "codex-tui UA 与 suffix 实例"),
        EvidenceRef(
            "audit-ep019-wham-consume-safe-20260730a",
            "codex_exec originator、unknown 终端标识与 codex_exec suffix 的 UA 实例",
        ),
    ),
}

# 这些条目曾因“范围标题／相邻正文自动继承运行号”而串挂。把精确归属写成门禁，
# 以后即使正文重排或 override 被误改，生成器也会失败而不是静默产出错误索引。
EXACT_RUN_BINDINGS = {
    "SPEC-PROTO-001": {"oauth-20260727T091556Z-noplugins"},
    "SPEC-PROTO-002": {
        "official-httpfb3-20260727T234853Z",
        "audit-ep014-turnstate-echo-20260730a",
    },
    "SPEC-TLS-002": {
        "official-h2-20260727T131936Z",
        "audit-tls002-ca-n0-20260730a",
    },
    "SPEC-CONN-001": {
        "clean2-conn-20260728T132008Z",
        "audit-conn001-image-repeat-20260730a",
        "audit-conn001-search-repeat-20260730a",
        "audit-conn001-retry-keepalive-openai-http-20260730a",
        "audit-conn001-retry-disconnect-openai-http-20260730a",
    },
    "SPEC-H2-001": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-002": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-003": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-004": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-005": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-006": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-H2-007": {"official-h2-20260727T131936Z", "relay-h2-20260728T032147Z"},
    "SPEC-BODY-004": {
        "audit-ep014-turnstate-echo-20260730a",
        "audit-ep014-turnstate-compact-20260730a",
        "audit-body004-ws-turnstate-20260730a",
    },
    "SPEC-BODY-007": {
        "audit-body007-workflow-clean-20260730a",
    },
    "SPEC-EP-002": {
        "oauth-ep002-allhosts",
        "oauth-ep002-refresh",
        "audit-ep012-sideband-synth-20260730a",
        "audit-ep002-file-upload-full2-20260730a",
    },
    "SPEC-EP-006": {
        "official-body2-20260728T000549Z",
        "audit-h1raw-20260730a",
    },
    "SPEC-EP-007": {"clean-legacy-20260728T132509Z"},
    "SPEC-EP-008": {"clean-search-20260728T132311Z"},
    "SPEC-EP-009": {
        "webrtc-20260728T134028Z",
        "live2-20260728T140403Z",
    },
    "SPEC-EP-019": {
        "clean-legacy-20260728T132509Z",
        "audit-ep019-wham-consume-safe-20260730a",
    },
    "SPEC-EP-023": {
        "relay-tui-recap-20260728T112358Z",
        "audit-ep021-auto-clean-20260730a",
        "audit-ep023-comphash-20260730b",
        "audit-ep023-downshift-20260730b",
    },
    "SPEC-EP-024": {
        "audit-ep024-exec-negative-clean-20260730a",
        "relay-tui-recap-20260728T112358Z",
    },
    "SPEC-HDR-004": {
        "clean-tool-20260728T132346Z",
        "audit-body002-plain-20260730a",
        "clean-image-20260728T132405Z",
        "relay-imgedit1",
    },
}

SOURCE_ONLY_REASONS = {
    "SPEC-HDR-001": "内部 header 组装／认证调用顺序只能由生产源码确认；wire 只能展示最终结果",
}

# 2026-07-30 人工逐条语义复核。这里评的是“该证据能否独立支撑完整命题”，
# 不是简单检查文件名是否存在。未列入下面集合的项默认是“充分”；集合之间必须互斥，
# build_content() 会按文档里的 53 个编号项做 fail-closed 校验。
SOURCE_AUDIT_PARTIAL = {
    "SPEC-TLS-002", "SPEC-HDR-006", "SPEC-EP-002", "SPEC-EP-019",
    "SPEC-EP-022", "SPEC-EP-014", "SPEC-EP-015",
}
SOURCE_AUDIT_NONE = {
    "SPEC-TLS-001", "SPEC-TLS-003", "SPEC-PROTO-001", "SPEC-WS-004",
    "SPEC-BODY-007",
}
CAPTURE_AUDIT_LIMITED = {
    "SPEC-EP-012", "SPEC-EP-023",
}
CAPTURE_AUDIT_NA = {"SPEC-HDR-001"}

AUDIT_NOTES = {
    "SPEC-TLS-001": "30 cipher／无 ALPN 只能由 P 证明；范围仅为 Ubuntu 24.04/OpenSSL 采集环境。",
    "SPEC-TLS-002": "源码证明有效 CA 切 rustls；10 cipher 与 ALPN 顺序由 P/J 证明，空值或无效 CA 不适用。",
    "SPEC-TLS-003": "P 证明扩展序变化；WS 归属还需结合采集场景与 WS 恒走 rustls 的源码。",
    "SPEC-PROTO-001": "无 ALPN 扩展是 P 的直接结论；源码只解释 CA 条件分支。",
    "SPEC-CONN-001": "跨上层调用新 Client；调用内 retry 共享 Client。存活连接复用、断连后新建 TCP 均有受控 R。",
    "SPEC-H2-001": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-002": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-003": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-004": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-005": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-006": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-H2-007": "J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。",
    "SPEC-WS-004": "permessage-deflate、RSV1 与上下文接管属于只能从 R 原始帧得出的 L3 结论。",
    "SPEC-HDR-001": "内部 build/configure/apply_auth 调用顺序只能由源码证明；wire 只显示最终头集合。",
    "SPEC-HDR-006": "显式 accept 有源码；reqwest 默认 */* 与最终端点线序必须由 wire 补齐。",
    "SPEC-BODY-004": "HTTP 响应头与 WS response.metadata 两条输入路径均已完成输入→保存→后续出站回送闭环。",
    "SPEC-BODY-007": "L3 观测记录；洁净固定十轮场景可完整复算，但计数不外推为协议封闭集合。",
    "SPEC-EP-002": "默认 base 与三类例外有源码；区域 blob host 由服务端动态返回，生产三跳已由 P/R 补齐。",
    "SPEC-EP-012": "第一跳有自然 R（1 次 400、2 次 403）；第二跳只有受控 200 后的官方请求，生产自然成功链按用户要求暂缓。",
    "SPEC-EP-019": "两个 GET 是生产 R；consume 由无外网假 OAuth 环境中的官方生产 handler 生成。请求 wire 已充分，生产响应与账号副作用不属于本规则命题。",
    "SPEC-EP-022": "端点选择与结构体有源码；实际线序、键序和值由 R 确认。",
    "SPEC-EP-023": "四层内部分派只能由源码证明；四种 reason 的 R 只能旁证结果。",
    "SPEC-EP-024": "洁净 TUI 正例与洁净 exec 负例交叉闭环；TUI 专属解析的全称边界由源码承担。",
    "SPEC-EP-014": "header 集合可从源码读出，最终线序由默认／beta／turn-state 三组 R 补齐。",
    "SPEC-EP-015": "body/header 构造有源码，最终线序与 commands 阶段变化由同一洁净 R 补齐。",
}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_h2_probe() -> None:
    """复算 3 条完整 h2 探针连接，拒绝摘要内容与七条 H2 规则漂移。"""
    data = json.loads(H2_PROBE.read_text(encoding="utf-8"))
    connections = data.get("connections")
    if data.get("schema_version") != "h2-wire-probe/v1" or not isinstance(connections, list):
        raise ValueError(f"H2 探针格式无效：{H2_PROBE}")
    if len(connections) != 3:
        raise ValueError(f"H2 探针应有 3 条完整连接，实际 {len(connections)}")

    expected_settings = [(2, 0), (4, 2097152), (5, 16384), (6, 16384)]
    expected_pseudo_headers = [":method", ":scheme", ":authority", ":path"]
    for index, connection in enumerate(connections, start=1):
        frames = connection.get("frames")
        if (
            connection.get("negotiated_alpn") != "h2"
            or connection.get("preface_ok") is not True
            or not isinstance(frames, list)
        ):
            raise ValueError(f"H2 探针第 {index} 条连接不完整")
        settings_frame = next(
            (
                frame for frame in frames
                if frame.get("type") == "SETTINGS" and frame.get("flags") == 0
            ),
            None,
        )
        window_frame = next(
            (
                frame for frame in frames
                if frame.get("type") == "WINDOW_UPDATE" and frame.get("stream_id") == 0
            ),
            None,
        )
        headers_frame = next(
            (frame for frame in frames if frame.get("type") == "HEADERS"),
            None,
        )
        settings = [
            (item.get("id"), item.get("value"))
            for item in (settings_frame or {}).get("settings_in_order", [])
        ]
        pseudo_headers = (headers_frame or {}).get("header_names_in_order", [])[:4]
        if (
            settings != expected_settings
            or (window_frame or {}).get("window_size_increment") != 5177345
            or pseudo_headers != expected_pseudo_headers
        ):
            raise ValueError(f"H2 探针第 {index} 条连接与规则常量不一致")


def validate_wham_consume() -> None:
    """重解析 EP-019 独立请求文件，并与同目录采集记录逐字段核对。"""
    metadata_path = WHAM_CONSUME / "capture.json"
    request_path = WHAM_CONSUME / "consume.request.bin"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    raw = request_path.read_bytes()
    try:
        raw_head, raw_body = raw.split(b"\r\n\r\n", 1)
        head_lines = raw_head.decode("latin-1").split("\r\n")
        request_line = head_lines[0]
        header_pairs = [line.split(":", 1) for line in head_lines[1:]]
        header_names = [name for name, _ in header_pairs]
        headers = {name.lower(): value.strip() for name, value in header_pairs}
        body = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"EP-019 consume 请求无法重解析：{request_path}") from exc

    if (
        metadata.get("schema_version") != "codex-wham-consume-safe/v1"
        or request_line != metadata.get("request_line")
        or header_names != metadata.get("header_names")
        or body != metadata.get("body")
        or int(headers.get("content-length", "-1")) != len(raw_body)
    ):
        raise ValueError("EP-019 consume 请求与 capture.json 不一致")


def manifest_index() -> dict[str, list[tuple[str, str]]]:
    """精确索引 manifest 绑定的官方脱敏分析，拒绝来源或哈希不合格的目录。"""
    out: dict[str, list[tuple[str, str]]] = {}
    if not FINAL_REVIEW_ROOT.is_dir():
        return out
    for manifest_path in sorted(FINAL_REVIEW_ROOT.glob("*/manifest.json")):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        codex = data.get("clients", {}).get("codex", {})
        if data.get("schema_version") != "official-client-capture/v1":
            continue
        if data.get("status") != "complete":
            continue
        if data.get("run_id") != manifest_path.parent.name:
            raise ValueError(
                f"manifest 的 run_id 与目录名不一致：{manifest_path}"
            )
        if codex.get("version") != OFFICIAL_VERSION or codex.get("sha256") != OFFICIAL_SHA256:
            continue
        valid_results = [
            row for row in data.get("case_results", [])
            if row.get("product") == "codex"
            and row.get("boundary") == "official_cli_to_official_platform"
            and row.get("evidence") == "mitm"
            and row.get("status") == "complete"
            and row.get("transport") in {"http", "ws"}
            and row.get("subject") == f"codex-{row.get('transport')}"
            and row.get("analysis_path")
        ]
        if not valid_results:
            continue
        artifacts = {row["path"]: row for row in data.get("artifacts", [])}
        selected: list[tuple[str, str]] = []
        for result in valid_results:
            rel = result["analysis_path"]
            artifact = artifacts.get(rel)
            path = manifest_path.parent / rel
            if not artifact or artifact.get("sensitivity") != "redacted" or not path.is_file():
                raise ValueError(f"manifest 的 M 类分析未登记为 redacted：{path}")
            if sha256_file(path) != artifact.get("sha256"):
                raise ValueError(f"manifest 的 M 类分析哈希不匹配：{path}")
            selected.append((result.get("transport", ""), rel))
        out[data["run_id"]] = selected
    return out


def forbidden_run_ids() -> set[str]:
    if not FORBIDDEN_ROOT.is_dir():
        return set()
    return {p.name for p in FORBIDDEN_ROOT.iterdir() if p.is_dir()}


def known_run_ids(manifests: dict[str, list[tuple[str, str]]]) -> set[str]:
    names = set(manifests)
    for root in [RAW, *SUMMARY_DIRS, *BASELINE_ROOTS]:
        if not root.is_dir():
            continue
        for p in root.iterdir():
            name = p.name[:-5] if p.suffix == ".json" else p.name
            if len(name) >= 8 and re.match(r"^[A-Za-z][\w.-]*$", name):
                names.add(name)
    return names


def canon_run(run: str) -> str:
    match = TS_RE.search(run)
    return match.group(1) if match else run


def mentioned_negatively(segment: str, run: str) -> bool:
    for sentence in re.split(r"[。；\n]", segment):
        if run in sentence and not NEG_RE.search(sentence):
            return False
    return run in segment


def rule_bodies(text: str):
    rules_text = spec_status.second_part(text)
    heads = [(m.group(1), m.group(2), m.start()) for m in HEAD_RE.finditer(rules_text)]
    all_heads = [m.start() for m in ANY_HEAD_RE.finditer(rules_text)]
    for sid, end, start in heads:
        stop = next((pos for pos in all_heads if pos > start), len(rules_text))
        body = rules_text[start:stop]
        if not end:
            yield sid, body
            continue
        # 只有 H2-002~005 确实共享同一份帧证据；其他范围标题一律拆开，避免串挂。
        if sid != "SPEC-H2-002" or end != "005":
            raise ValueError(f"未获准共享证据的范围标题：{sid}~{end}；请拆成独立标题")
        prefix, first = sid.rsplit("-", 1)
        for number in range(int(first), int(end) + 1):
            yield f"{prefix}-{number:0{len(first)}d}", body


def locate(
    run: str,
    manifests: dict[str, list[tuple[str, str]]],
    channels: tuple[str, ...] = (),
) -> list[EvidencePath]:
    hits: list[EvidencePath] = []
    if run in manifests:
        manifest_rel = (FINAL_REVIEW_ROOT / run / "manifest.json").relative_to(CAPTURES)
        available_channels = {channel for channel, _ in manifests[run]}
        missing_channels = set(channels) - available_channels
        if missing_channels:
            raise ValueError(
                f"manifest {run} 缺少请求的通道：{sorted(missing_channels)}"
            )
        analyses = []
        for channel, rel in manifests[run]:
            if channels and channel not in channels:
                continue
            analyses.append(f"`{(FINAL_REVIEW_ROOT / run / rel).relative_to(CAPTURES)}`")
        if analyses:
            hits.append(EvidencePath(
                "M",
                f"`{manifest_rel}` + " + "<br>".join(analyses),
            ))

    if RAW.is_dir():
        directories = [directory for directory in sorted(RAW.iterdir())
                       if directory.is_dir()]
        exact = [directory for directory in directories if directory.name == run]
        if exact:
            candidates = exact
        else:
            # 旧归档有“短运行号 ↔ 带工具前缀的目录名”的别名，只能做模糊匹配。
            # 最后一段兜底仅允许标准 UTC 时间戳；`audit-*-20260730a` 这类运行号
            # 共用日期后缀，若按 `endswith()` 匹配会把一条证据错误链接到全部目录。
            suffix = run.split("-")[-1]
            timestamp_suffix = bool(TS_RE.fullmatch(suffix))
            candidates = [
                directory for directory in directories
                if directory.name in run or run in directory.name
                or (timestamp_suffix and directory.name.endswith(suffix))
            ]
        for directory in candidates:
            count = len(list(directory.rglob("*.bin")))
            hits.append(EvidencePath(
                "R", f"`raw-scrubbed/{directory.name}/` （{count} 个 .bin）"
            ))

    for summary_dir in SUMMARY_DIRS:
        if not summary_dir.is_dir():
            continue
        for path in sorted(summary_dir.iterdir()):
            if run not in path.name:
                continue
            if path.is_dir():
                count = len(list(path.rglob("*.json")))
                suffix = f" （{count} 个 json）" if count else ""
                hits.append(EvidencePath("J", f"`{path.relative_to(CAPTURES)}/`{suffix}"))
            elif path.suffix == ".json":
                hits.append(EvidencePath("J", f"`{path.relative_to(CAPTURES)}`"))

    for baseline_root in BASELINE_ROOTS:
        if not baseline_root.is_dir():
            continue
        for directory in sorted(baseline_root.iterdir()):
            if directory.is_dir() and run in directory.name:
                count = len(list(directory.rglob("*.pcap")))
                if count:
                    hits.append(EvidencePath(
                        "P",
                        f"`{directory.relative_to(CAPTURES)}/` （{count} 个 pcap）",
                    ))
    return hits


def validate_raw_integrity(
    rule_refs: dict[str, list[EvidenceRef]],
    manifests: dict[str, list[tuple[str, str]]],
) -> tuple[int, int, int, int]:
    """检查索引实际引用的全部 R 目录，并显式隔离两个正例样本和一个独立请求。"""
    directories: set[pathlib.Path] = set()
    for refs in rule_refs.values():
        for ref in refs:
            for evidence_path in locate(ref.run, manifests, ref.channels):
                if evidence_path.kind != "R":
                    continue
                match = re.search(r"`raw-scrubbed/([^/]+)/`", evidence_path.rendered)
                if match:
                    directories.add(RAW / match.group(1))

    positive_only_names = {
        "audit-ep012-sideband-synth-20260730a",
        "relay-h2-20260728T032147Z",
    }
    standalone_names = {"audit-ep019-wham-consume-safe-20260730a"}
    if not positive_only_names <= {path.name for path in directories}:
        raise ValueError("R 类正例边界目录未完整进入索引")
    if not standalone_names <= {path.name for path in directories}:
        raise ValueError("EP-019 独立请求目录未进入索引")

    clean = positive_only = standalone = 0
    connection_re = re.compile(
        r"(conn\d+)\.(client_to_upstream|upstream_to_client)\.bin"
    )
    for directory in sorted(directories):
        if directory.name in standalone_names:
            validate_wham_consume()
            standalone += 1
            continue

        relay_dir = directory / "relay"
        expected_upstream_only: set[str] = set()
        manifest_path = relay_dir / "relay.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for connection in manifest.get("connections", []):
                connection_id = connection.get("connection_id")
                if (
                    isinstance(connection_id, int)
                    and connection.get("valid") is True
                    and connection.get("expected_upstream_only") is True
                ):
                    expected_upstream_only.add(f"conn{connection_id:03d}")

        connections: dict[str, dict[str, int]] = {}
        for path in relay_dir.glob("*.bin"):
            match = connection_re.fullmatch(path.name)
            if match:
                connections.setdefault(match.group(1), {})[match.group(2)] = path.stat().st_size
        if not connections:
            raise ValueError(f"R 类目录没有连接字节：{relay_dir}")

        bad = 0
        for connection_name, directions in connections.items():
            upstream = directions.get("client_to_upstream", 0)
            downstream = directions.get("upstream_to_client", 0)
            if upstream and downstream:
                continue
            if upstream and connection_name in expected_upstream_only:
                continue
            bad += 1

        if directory.name in positive_only_names:
            if bad == 0:
                raise ValueError(f"已登记正例边界的目录不再有缺口，请重核分类：{directory.name}")
            positive_only += 1
        elif bad:
            raise ValueError(f"R 类目录存在 {bad} 条未声明缺口：{directory.name}")
        else:
            clean += 1

    return len(directories), clean, positive_only, standalone


def extract_refs(
    sid: str,
    body: str,
    known_runs: set[str],
    forbidden_runs: set[str],
) -> tuple[list[EvidenceRef], str | None]:
    segment = body.split("**实测**", 1)[1] if "**实测**" in body else ""
    bad = sorted(run for run in forbidden_runs if run in segment)
    if FORBIDDEN_ROOT.name in segment or bad:
        detail = bad or [FORBIDDEN_ROOT.name]
        raise ValueError(f"{sid} 的官方实测段错误引用 Sub2API 出站证据：{detail}")

    # 源码机制条目仍保留统一的“实测”字段，但该字段明确写“不适用”。
    # 必须在普通实测文本解析前返回，否则会被误记成“有记录但无运行号”。
    if sid in SOURCE_ONLY_REASONS:
        return [], None

    if sid in RULE_EVIDENCE_OVERRIDES:
        refs = list(RULE_EVIDENCE_OVERRIDES[sid])
        bad = [ref.run for ref in refs if ref.run in forbidden_runs]
        if bad:
            raise ValueError(f"{sid} 的显式证据错误引用 Sub2API：{bad}")
        return refs, None
    if "**实测**" not in body:
        return [], None

    runs: list[str] = []
    for match in RUN_RE.finditer(segment):
        run = match.group(0).strip("-")
        if run not in runs:
            runs.append(run)
    for run in sorted(known_runs):
        if run in segment and run not in runs:
            runs.append(run)
    runs = [run for run in runs if not mentioned_negatively(segment, run)]
    # 同一物理采集常同时出现“短运行号”和“带工具前缀/后缀的目录名”。按时间戳
    # 去重，并优先保留磁盘上存在的完整名称，避免一份 pcap 被列成两份独立证据。
    deduped: dict[str, str] = {}
    for run in runs:
        key = canon_run(run)
        current = deduped.get(key)
        if current is None or (run in known_runs and current not in known_runs):
            deduped[key] = run
    runs = list(deduped.values())
    if runs:
        return [
            EvidenceRef(run, "正文实测段所述形态；精确证明边界见规则正文")
            for run in runs
        ], None
    if re.search(r"⛔\s*\*{0,2}(尚未采到|未采)", segment[:120]):
        return [], "not_yet"
    return [], re.sub(r"\s+", " ", segment).strip()[:70]


def sid_sort_key(sid: str):
    parts = sid.split("-")
    return parts[1], int(parts[-1])


def validate_semantic_bindings(rule_refs: dict[str, list[EvidenceRef]]) -> None:
    for sid, expected in EXACT_RUN_BINDINGS.items():
        actual = {ref.run for ref in rule_refs.get(sid, [])}
        if actual != expected:
            raise ValueError(
                f"{sid} 的证据归属漂移：期望 {sorted(expected)}，实际 {sorted(actual)}"
            )

    body4_scopes = [ref.scope for ref in rule_refs.get("SPEC-BODY-004", [])]
    if (
        len(body4_scopes) != 3
        or sum("受控 HTTP 响应头下发" in scope and "原样回送" in scope for scope in body4_scopes) != 2
        or not any(
            "受控 WS response.metadata 下发" in scope
            and "后续 2 个" in scope
            and "原样回送" in scope
            for scope in body4_scopes
        )
    ):
        raise ValueError(
            "SPEC-BODY-004 必须绑定 HTTP responses／compact 与 WS response.metadata 三条 turn-state 正向回送证据"
        )

    body7_scopes = [ref.scope for ref in rule_refs.get("SPEC-BODY-007", [])]
    if (
        len(body7_scopes) != 1
        or not any("洁净十轮" in scope and "固定场景" in scope for scope in body7_scopes)
    ):
        raise ValueError(
            "SPEC-BODY-007 必须只绑定洁净固定场景计数"
        )


def build_content() -> tuple[str, int, int, int, int]:
    validate_h2_probe()
    validate_wham_consume()
    text = SPEC.read_text(encoding="utf-8")
    format_errors = spec_status.validate_format(text)
    if format_errors:
        raise ValueError("第二部分格式错误：" + "；".join(format_errors))
    status_items = spec_status.parse(text)
    spec_status.validate_taxonomy(status_items)
    item_meta: dict[str, tuple[str, str, str]] = {}
    item_origin: dict[str, str] = {}
    for label, _, origin, status, _ in status_items:
        for sid in spec_status.expanded_ids(label):
            kind = spec_status.rule_kind(sid)
            scope = spec_status.rule_scope(sid)
            if kind == "W":
                kind_label = {
                    "OAUTH": "① OAuth 可见规则",
                    "CA": "② 自定义 CA 分支",
                    "CUSTOM_PROVIDER": "③ 自定义 provider 分支",
                }[scope]
            else:
                kind_label = {"M": "④ 内部机制", "E": "⑤ 采集记录"}[kind]
            scope_label = {
                "OAUTH": "内置 OpenAI OAuth",
                "CA": "自定义 CA",
                "CUSTOM_PROVIDER": "自定义 provider",
            }.get(scope, "—")
            verify_label = status if kind == "W" else ("源码机制" if kind == "M" else "观测记录")
            item_meta[sid] = (kind_label, scope_label, verify_label)
            item_origin[sid] = origin
    manifests = manifest_index()
    known_runs = known_run_ids(manifests)
    forbidden_runs = forbidden_run_ids()
    rule_refs: dict[str, list[EvidenceRef]] = {}
    run_rules: dict[str, set[str]] = {}
    no_run: list[tuple[str, str]] = []
    not_yet: list[str] = []
    source_only: list[str] = []

    for sid, body in rule_bodies(text):
        refs, missing_reason = extract_refs(sid, body, known_runs, forbidden_runs)
        if refs:
            for ref in refs:
                paths = locate(ref.run, manifests, ref.channels)
                if not paths:
                    raise ValueError(f"{sid} 的运行号没有可定位官方文件：{ref.run}")
            rule_refs[sid] = refs
            for ref in refs:
                run_rules.setdefault(ref.run, set()).add(sid)
        elif missing_reason == "not_yet":
            not_yet.append(sid)
        elif missing_reason:
            no_run.append((sid, missing_reason))
        elif sid in SOURCE_ONLY_REASONS:
            source_only.append(sid)
        else:
            raise ValueError(f"{sid} 既无官方证据文件，也未登记为源码机制／尚未采到")

    validate_semantic_bindings(rule_refs)
    raw_directory_count, raw_clean, raw_positive_only, raw_standalone = (
        validate_raw_integrity(rule_refs, manifests)
    )
    if set(source_only) != set(SOURCE_ONLY_REASONS):
        raise ValueError(
            "源码机制登记与正文漂移："
            f"期望 {sorted(SOURCE_ONLY_REASONS)}，实际 {sorted(source_only)}"
        )

    all_ids = set(item_meta)
    audit_sets = {
        "源码部分": SOURCE_AUDIT_PARTIAL,
        "源码无/不适用": SOURCE_AUDIT_NONE,
        "抓包有限": CAPTURE_AUDIT_LIMITED,
        "抓包不适用": CAPTURE_AUDIT_NA,
    }
    for label, values in audit_sets.items():
        unknown = values - all_ids
        if unknown:
            raise ValueError(f"{label}审计登记了文档外编号项：{sorted(unknown)}")
    if SOURCE_AUDIT_PARTIAL & SOURCE_AUDIT_NONE:
        raise ValueError("源码审计分类重叠")
    if CAPTURE_AUDIT_LIMITED & CAPTURE_AUDIT_NA:
        raise ValueError("抓包审计分类重叠")

    source_full = all_ids - SOURCE_AUDIT_PARTIAL - SOURCE_AUDIT_NONE
    capture_full = all_ids - CAPTURE_AUDIT_LIMITED - CAPTURE_AUDIT_NA

    capture_kinds: dict[str, set[str]] = {}
    for sid, refs in rule_refs.items():
        capture_kinds[sid] = {
            path.kind
            for ref in refs
            for path in locate(ref.run, manifests, ref.channels)
        }
    raw_count = sum(bool(capture_kinds.get(sid, set()) & {"P", "R"}) for sid in all_ids)
    if raw_count != len(all_ids) - len(CAPTURE_AUDIT_NA):
        missing_raw = sorted(
            sid for sid in all_ids - CAPTURE_AUDIT_NA
            if not capture_kinds.get(sid, set()) & {"P", "R"}
        )
        raise ValueError(f"仍缺 P/R 原始证据的可抓包编号项：{missing_raw}")

    lines = [
        "# 证据索引：编号项 ↔ 官方证据文件",
        "",
        "> 由 `tools/evidence_index.py` 生成，不要手改。",
        "",
        "本索引只接收 **Codex CLI 0.145.0 官方客户端**证据。Sub2API 出站验收目录",
        "`sub2api-egress` 被生成器硬禁止；它只能用于第三部分的实现差异比对。",
        "",
        "## 0. 证据类型与边界",
        "",
        "| 类型 | 含义 | 能证明什么 | 来源边界 |",
        "|---|---|---|---|",
        "| **P** | 原始 pcap | ClientHello、SNI、TLS 扩展等被动线索 | 官方运行；部分归档无逐运行 manifest |",
        "| **R** | 等长脱敏后的中继原始字节 `.bin` | HTTP/1.1 线序、完整 body、WS 帧；可重新解析 | 官方 relay；部分 `raw-scrubbed` 目录无逐运行 manifest／二进制哈希 |",
        "| **J** | JSON 解码摘要 | 摘要中保留的 header／body 形态 | 无原始字节时不能升级成逐字节证据 |",
        "| **M** | manifest 绑定的官方脱敏分析 | HTTP／WS 字段形态与取值 | 强校验版本、二进制 SHA、官方边界、artifact 哈希；不暴露 `raw_private` |",
        "",
        "R 类已对 `Authorization`、`Cookie`、账号 ID 与游标等值做等长替换；header 名、",
        "大小写、偏移、`Content-Length` 与帧长度保持不变。M 类只链接 manifest 中",
        "标为 `redacted` 且哈希复核通过的分析文件。J 类是降维摘要，不能冒充原始字节。",
        "",
        f"当前索引引用 **{raw_directory_count} 个 R 目录**："
        f"**{raw_clean} 个无非预期连接缺口**；**{raw_positive_only} 个只承担正例**"
        "（realtime 受控第二跳、H2 原始帧交叉核验）；"
        f"**{raw_standalone} 个是可独立重解析的 EP-019 请求文件**。",
        "",
        "## 1. 53 项源码／抓包双证据复核",
        "",
        f"源码证据：**充分 {len(source_full)}、部分 {len(SOURCE_AUDIT_PARTIAL)}、"
        f"无／不适用 {len(SOURCE_AUDIT_NONE)}**。抓包证据："
        f"**充分 {len(capture_full)}、有限 {len(CAPTURE_AUDIT_LIMITED)}、"
        f"不适用 {len(CAPTURE_AUDIT_NA)}**。",
        "",
        f"**{raw_count}/{len(all_ids)} 项已有可重新解析的 P/R 原始证据**；唯一例外 `SPEC-HDR-001` "
        "描述内部调用顺序，抓包在结构上不适用。J/M 仍可作交叉验证，但不再是任何"
        "编号项唯一的抓包载体。",
        "",
        "| 编号项 | 分类 | 源码证据 | 抓包证据 | 抓包类型 | 逐项复核结论／边界 |",
        "|---|---|---|---|---|---|",
    ]

    for sid in sorted(all_ids, key=sid_sort_key):
        if sid in SOURCE_AUDIT_NONE:
            source_label = f"— 无／不适用（{item_origin[sid]}）"
        elif sid in SOURCE_AUDIT_PARTIAL:
            source_label = f"🟡 部分（{item_origin[sid]}）"
        else:
            source_label = f"✅ 充分（{item_origin[sid]}）"
        if sid in CAPTURE_AUDIT_NA:
            capture_label = "— 不适用"
        elif sid in CAPTURE_AUDIT_LIMITED:
            capture_label = "🟡 有限"
        else:
            capture_label = "✅ 充分"
        kinds = "+".join(sorted(capture_kinds.get(sid, set()), key="PRJM".index)) or "—"
        note = AUDIT_NOTES.get(
            sid,
            "源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。",
        )
        lines.append(
            f"| {sid} | {item_meta[sid][0]} | {source_label} | {capture_label} | "
            f"{kinds} | {note} |"
        )

    lines += [
        "",
        "复核口径：`充分` 表示现有证据足以支撑**当前已收窄后的命题**；`部分`／`有限`"
        " 表示源码只能给机制、动态 wire 值需实测，或抓包只覆盖受控／结果分支。"
        "未带逐运行 manifest／二进制哈希的 P/R/J 证据，其来源依赖采集记录；"
        "具备 manifest 的证据还会校验版本、边界与 artifact 哈希。",
        "",
        "## 2. 编号项 → 官方证据",
        "",
        "| 编号项 | 分类 | 适用范围 | 验证状态 | 运行号 | 类型 | 证明范围 | 证据位置 |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for sid in sorted(rule_refs, key=sid_sort_key):
        kind, scope, status = item_meta[sid]
        for index, ref in enumerate(rule_refs[sid]):
            paths = locate(ref.run, manifests, ref.channels)
            kinds = "+".join(sorted({path.kind for path in paths}, key="PRJM".index))
            rendered = "<br>".join(path.rendered for path in paths)
            lines.append(
                f"| {sid if index == 0 else ''} | {kind if index == 0 else ''} | "
                f"{scope if index == 0 else ''} | {status if index == 0 else ''} | "
                f"`{ref.run}` | **{kinds}** | {ref.scope} | {rendered} |"
            )

    physical = len({canon_run(run) for run in run_rules})
    lines += [
        "",
        f"## 3. 运行 → 关联编号项（{len(run_rules)} 个运行号 = {physical} 次物理采集）",
        "",
        "同一次采集可能同时有短目录名和带工具前缀的摘要别名；物理次数按时间戳归一。",
        "“关联”只表示该运行支撑表一写明的证明范围，不表示它完整证明每条规则的全部分支。",
        "",
        "| 运行号 | 关联编号项 |",
        "|---|---|",
    ]
    for run in sorted(run_rules):
        lines.append(f"| `{run}` | {' '.join(sorted(run_rules[run]))} |")

    lines += [
        "",
        f"## 4. 没有可用于验证机制本身的抓包（{len(source_only)} 项）",
        "",
        "这些项不是漏建索引：它们描述客户端内部调用／分派。抓包只能看到结果，",
        "不能反推出内部机制；相关可见结果已归入独立 wire 规则。",
        "",
        "| 编号项 | 分类 | 适用范围 | 验证状态 | 原因 |",
        "|---|---|---|---|---|",
        *[
            f"| {sid} | {item_meta[sid][0]} | {item_meta[sid][1]} | "
            f"{item_meta[sid][2]} | {SOURCE_ONLY_REASONS[sid]} |"
            for sid in sorted(source_only, key=sid_sort_key)
        ],
    ]

    if not_yet:
        lines += [
            "",
            f"## 5. 明确标注尚未采到（{len(not_yet)} 项）",
            "",
            "这些编号项的实测段明确写明尚未采到，不是索引漏写：",
            "",
            *[f"- `{sid}`" for sid in not_yet],
        ]
    if no_run:
        lines += [
            "",
            f"## 5.1 有实测记录但无运行号（{len(no_run)} 项）",
            "",
            "| 编号项 | 现有记录 |",
            "|---|---|",
            *[f"| {sid} | {description} |" for sid, description in no_run],
        ]

    return "\n".join(lines) + "\n", len(rule_refs), len(run_rules), physical, len(no_run)


def main(write: bool) -> int:
    try:
        content, rule_count, run_count, physical, no_run_count = build_content()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"🔴 证据索引生成失败：{exc}", file=sys.stderr)
        return 1

    if not write:
        if not OUT.is_file():
            print("🔴 索引不存在，请先生成。", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") != content:
            print("🔴 索引已过期，请运行 python3 tools/evidence_index.py 重建。", file=sys.stderr)
            return 1
        print("✅ 索引与文档、来源门禁及 manifest 哈希同步（未写盘）")
        return 0

    OUT.write_text(content, encoding="utf-8")
    print(f"已生成 {OUT.relative_to(ROOT)}")
    print(f"  编号项→证据 {rule_count} 项；运行号 {run_count} 个 = {physical} 次物理采集")
    if no_run_count:
        print(f"  ⚠ {no_run_count} 项有实测记录但未写运行号")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写回")
    args = parser.parse_args()
    return main(write=not args.check)


if __name__ == "__main__":
    sys.exit(cli())
