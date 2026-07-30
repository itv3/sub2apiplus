# 证据索引：编号项 ↔ 官方证据文件

> 由 `tools/evidence_index.py` 生成，不要手改。

本索引只接收 **Codex CLI 0.145.0 官方客户端**证据。Sub2API 出站验收目录
`sub2api-egress` 被生成器硬禁止；它只能用于第三部分的实现差异比对。

## 0. 证据类型与边界

| 类型 | 含义 | 能证明什么 | 来源边界 |
|---|---|---|---|
| **P** | 原始 pcap | ClientHello、SNI、TLS 扩展等被动线索 | 官方运行；部分归档无逐运行 manifest |
| **R** | 等长脱敏后的中继原始字节 `.bin` | HTTP/1.1 线序、完整 body、WS 帧；可重新解析 | 官方 relay；部分 `raw-scrubbed` 目录无逐运行 manifest／二进制哈希 |
| **J** | JSON 解码摘要 | 摘要中保留的 header／body 形态 | 无原始字节时不能升级成逐字节证据 |
| **M** | manifest 绑定的官方脱敏分析 | HTTP／WS 字段形态与取值 | 强校验版本、二进制 SHA、官方边界、artifact 哈希；不暴露 `raw_private` |

R 类已对 `Authorization`、`Cookie`、账号 ID 与游标等值做等长替换；header 名、
大小写、偏移、`Content-Length` 与帧长度保持不变。M 类只链接 manifest 中
标为 `redacted` 且哈希复核通过的分析文件。J 类是降维摘要，不能冒充原始字节。

当前索引引用 **36 个 R 目录**：**33 个无非预期连接缺口**；**2 个只承担正例**（realtime 受控第二跳、H2 原始帧交叉核验）；**1 个是可独立重解析的 EP-019 请求文件**。

## 1. 53 项源码／抓包双证据复核

源码证据：**充分 41、部分 7、无／不适用 5**。抓包证据：**充分 50、有限 2、不适用 1**。

**52/53 项已有可重新解析的 P/R 原始证据**；唯一例外 `SPEC-HDR-001` 描述内部调用顺序，抓包在结构上不适用。J/M 仍可作交叉验证，但不再是任何编号项唯一的抓包载体。

| 编号项 | 分类 | 源码证据 | 抓包证据 | 抓包类型 | 逐项复核结论／边界 |
|---|---|---|---|---|---|
| SPEC-BODY-001 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-BODY-002 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-BODY-003 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-BODY-004 | ④ 内部机制 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | HTTP 响应头与 WS response.metadata 两条输入路径均已完成输入→保存→后续出站回送闭环。 |
| SPEC-BODY-005 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-BODY-006 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-BODY-007 | ⑤ 采集记录 | — 无／不适用（只能实测（L3）） | ✅ 充分 | R | L3 观测记录；洁净固定十轮场景可完整复算，但计数不外推为协议封闭集合。 |
| SPEC-CONN-001 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 跨上层调用新 Client；调用内 retry 共享 Client。存活连接复用、断连后新建 TCP 均有受控 R。 |
| SPEC-EP-001 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-002 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | P+R | 默认 base 与三类例外有源码；区域 blob host 由服务端动态返回，生产三跳已由 P/R 补齐。 |
| SPEC-EP-005 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-006 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-007 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-008 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-009 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-012 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | 🟡 有限 | R | 第一跳有自然 R（1 次 400、2 次 403）；第二跳只有受控 200 后的官方请求，生产自然成功链按用户要求暂缓。 |
| SPEC-EP-013 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-014 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | R | header 集合可从源码读出，最终线序由默认／beta／turn-state 三组 R 补齐。 |
| SPEC-EP-015 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | R | body/header 构造有源码，最终线序与 commands 阶段变化由同一洁净 R 补齐。 |
| SPEC-EP-019 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | R | 两个 GET 是生产 R；consume 由无外网假 OAuth 环境中的官方生产 handler 生成。请求 wire 已充分，生产响应与账号副作用不属于本规则命题。 |
| SPEC-EP-020 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-021 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-EP-022 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | R+J | 端点选择与结构体有源码；实际线序、键序和值由 R 确认。 |
| SPEC-EP-023 | ④ 内部机制 | ✅ 充分（源码读出（L1/L2）） | 🟡 有限 | R+J | 四层内部分派只能由源码证明；四种 reason 的 R 只能旁证结果。 |
| SPEC-EP-024 | ⑤ 采集记录 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 洁净 TUI 正例与洁净 exec 负例交叉闭环；TUI 专属解析的全称边界由源码承担。 |
| SPEC-H1-001 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-H1-002 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-H1-003 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-H1-004 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-H2-001 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-002 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-003 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-004 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-005 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-006 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-H2-007 | ② 自定义 CA 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | J 类探针 3/3 完整；R 类样本只作正向原始帧交叉核验，不承担计数或缺失命题。 |
| SPEC-HDR-001 | ④ 内部机制 | ✅ 充分（源码读出（L1/L2）） | — 不适用 | — | 内部 build/configure/apply_auth 调用顺序只能由源码证明；wire 只显示最终头集合。 |
| SPEC-HDR-002 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-HDR-004 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-HDR-005 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-HDR-006 | ① OAuth 可见规则 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | R+J | 显式 accept 有源码；reqwest 默认 */* 与最终端点线序必须由 wire 补齐。 |
| SPEC-HDR-007 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-HDR-008 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-PROTO-001 | ① OAuth 可见规则 | — 无／不适用（只能实测（L3）） | ✅ 充分 | P | 无 ALPN 扩展是 P 的直接结论；源码只解释 CA 条件分支。 |
| SPEC-PROTO-002 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R+J | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-TLS-001 | ① OAuth 可见规则 | — 无／不适用（只能实测（L3）） | ✅ 充分 | P | 30 cipher／无 ALPN 只能由 P 证明；范围仅为 Ubuntu 24.04/OpenSSL 采集环境。 |
| SPEC-TLS-002 | ② 自定义 CA 分支 | 🟡 部分（源码读出（L1/L2）） | ✅ 充分 | P+J | 源码证明有效 CA 切 rustls；10 cipher 与 ALPN 顺序由 P/J 证明，空值或无效 CA 不适用。 |
| SPEC-TLS-003 | ① OAuth 可见规则 | — 无／不适用（只能实测（L3）） | ✅ 充分 | P | P 证明扩展序变化；WS 归属还需结合采集场景与 WS 恒走 rustls 的源码。 |
| SPEC-WS-001 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-WS-002 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-WS-003 | ③ 自定义 provider 分支 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |
| SPEC-WS-004 | ① OAuth 可见规则 | — 无／不适用（只能实测（L3）） | ✅ 充分 | R | permessage-deflate、RSV1 与上下文接管属于只能从 R 原始帧得出的 L3 结论。 |
| SPEC-WS-005 | ① OAuth 可见规则 | ✅ 充分（源码读出（L1/L2）） | ✅ 充分 | R | 源码命题与对应官方 P/R 证据语义一致；精确运行号和证明范围见下一节。 |

复核口径：`充分` 表示现有证据足以支撑**当前已收窄后的命题**；`部分`／`有限` 表示源码只能给机制、动态 wire 值需实测，或抓包只覆盖受控／结果分支。未带逐运行 manifest／二进制哈希的 P/R/J 证据，其来源依赖采集记录；具备 manifest 的证据还会校验版本、边界与 artifact 哈希。

## 2. 编号项 → 官方证据

| 编号项 | 分类 | 适用范围 | 验证状态 | 运行号 | 类型 | 证明范围 | 证据位置 |
|---|---|---|---|---|---|---|---|
| SPEC-BODY-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-ep014-turnstate-echo-20260730a` | **R** | 可重解析的 HTTP Lite R 类原始 body | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | 可重解析的 HTTP 非 Lite R 类原始 body | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `clean-tool-20260728T132346Z` | **R** | 官方 WS Lite 结构实例 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
|  |  |  |  | `audit-ws005-nonlite-20260730a` | **R** | 官方 WS 非 Lite 结构实例 | `raw-scrubbed/audit-ws005-nonlite-20260730a/` （4 个 .bin） |
| SPEC-BODY-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `audit-ep014-turnstate-echo-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
| SPEC-BODY-003 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-ep014-turnstate-echo-20260730a` | **R** | HTTP Lite R 类原始 body 直接证明 Lite 变换 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `clean-tool-20260728T132346Z` | **R** | WS Lite 变换 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
| SPEC-BODY-004 | ④ 内部机制 | — | 源码机制 | `audit-ep014-turnstate-echo-20260730a` | **R** | 受控 HTTP 响应头下发 turn-state 后，官方客户端在同一 turn 的后续 /responses 原样回送 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-ep014-turnstate-compact-20260730a` | **R** | 受控 HTTP 响应头下发 turn-state 后，官方客户端在同一 turn 的 legacy compact 原样回送 | `raw-scrubbed/audit-ep014-turnstate-compact-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-body004-ws-turnstate-20260730a` | **R** | 受控 WS response.metadata 下发 turn-state 后，官方客户端后续 2 个 response.create 均在 client_metadata 中原样回送；3/3 连接双向完整 | `raw-scrubbed/audit-body004-ws-turnstate-20260730a/` （6 个 .bin） |
| SPEC-BODY-005 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-ep014-turnstate-echo-20260730a` | **R** | HTTP Lite R 类原始 body：tool_choice=str:auto | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | HTTP 非 Lite R 类原始 body：tool_choice=str:auto | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `clean-tool-20260728T132346Z` | **R** | WS Lite 原始帧：tool_choice=str:auto | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
|  |  |  |  | `audit-ws005-nonlite-20260730a` | **R** | WS 非 Lite 原始帧：tool_choice=str:auto | `raw-scrubbed/audit-ws005-nonlite-20260730a/` （4 个 .bin） |
| SPEC-BODY-006 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-ep014-turnstate-echo-20260730a` | **R** | HTTP Lite R 类原始 body 的实际字段形态 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | HTTP 非 Lite R 类原始 body 的实际字段形态 | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
| SPEC-BODY-007 | ⑤ 采集记录 | — | 观测记录 | `audit-body007-workflow-clean-20260730a` | **R** | 洁净十轮编码工作流：20/20 双向，377 个 input 项、最大 95；计数只适用于该固定场景，不是协议封闭集合 | `raw-scrubbed/audit-body007-workflow-clean-20260730a/` （40 个 .bin） |
| SPEC-CONN-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean2-conn-20260728T132008Z` | **R** | 正常无重试的多轮主模型调用：跨调用各自使用独立连接 | `raw-scrubbed/clean2-conn-20260728T132008Z/` （12 个 .bin） |
|  |  |  |  | `audit-conn001-image-repeat-20260730a` | **R** | 同进程两次 images 上层调用落在两条独立 TCP | `raw-scrubbed/audit-conn001-image-repeat-20260730a/` （14 个 .bin） |
|  |  |  |  | `audit-conn001-search-repeat-20260730a` | **R** | 同进程两次 alpha-search 上层调用落在两条独立 TCP | `raw-scrubbed/audit-conn001-search-repeat-20260730a/` （14 个 .bin） |
|  |  |  |  | `audit-conn001-retry-keepalive-openai-http-20260730a` | **R** | 内置 OpenAI OAuth 的同一次 Responses 调用：500 后 retry 复用存活的同一 TCP | `raw-scrubbed/audit-conn001-retry-keepalive-openai-http-20260730a/` （6 个 .bin） |
|  |  |  |  | `audit-conn001-retry-disconnect-openai-http-20260730a` | **R** | 内置 OpenAI OAuth 的同一次 Responses 调用：断连后 retry 由同一 Client 新建 TCP | `raw-scrubbed/audit-conn001-retry-disconnect-openai-http-20260730a/` （7 个 .bin） |
| SPEC-EP-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-body002-plain-20260730a` | **R** | R 类 HTTP 非 Lite 原始 body 中的 namespace image_gen 工具呈现 | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `clean-image-20260728T132405Z` | **R** | Lite 工具呈现与 generations 调用 | `raw-scrubbed/clean-image-20260728T132405Z/` （6 个 .bin） |
|  |  |  |  | `relay-imgedit1` | **R+J** | Lite 工具呈现与 edits 调用 | `raw-scrubbed/relay-imgedit1/` （10 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-imgedit1.json` |
| SPEC-EP-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `oauth-ep002-allhosts` | **P** | 无 host 过滤 pcap：普通 OAuth 会话的 SNI 集合 | `wire-parity-fix-20260727/official-baseline/oauth-ep002-allhosts/` （3 个 pcap） |
|  |  |  |  | `oauth-ep002-refresh` | **P** | 无 host 过滤 pcap：真实 token 刷新访问 auth.openai.com | `wire-parity-fix-20260727/official-baseline/oauth-ep002-refresh/` （3 个 pcap） |
|  |  |  |  | `audit-ep012-sideband-synth-20260730a` | **R** | 受控首跳 200 后由官方 CLI 派生的 api.openai.com sideband | `raw-scrubbed/audit-ep012-sideband-synth-20260730a/` （15 个 .bin） |
|  |  |  |  | `audit-ep002-file-upload-full2-20260730a` | **R** | 生产文件上传三跳：chatgpt 首跳、服务端返回的 oaiusercontent PUT、chatgpt uploaded 确认；10/10 双向完整，预签名 query 已等长脱敏 | `raw-scrubbed/audit-ep002-file-upload-full2-20260730a/` （20 个 .bin） |
| SPEC-EP-005 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `clean-search-20260728T132311Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
|  |  |  |  | `clean-image-20260728T132405Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-image-20260728T132405Z/` （6 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `audit-ep014-turnstate-echo-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-h1raw-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-EP-006 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `official-body2-20260728T000549Z` | **J** | 仅证明 models URL 与方法 | `wire-parity-fix-20260727/h1-wire-probe/official-BODY-official-body2-20260728T000549Z.json` |
|  |  |  |  | `audit-h1raw-20260730a` | **R** | R 类原始请求直接证明 models URL 与方法 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-EP-007 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 仅证明 legacy compact URL 与方法 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
| SPEC-EP-008 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-search-20260728T132311Z` | **R** | 仅证明 alpha-search URL 与方法 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
| SPEC-EP-009 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `webrtc-20260728T134028Z` | **R** | 仅证明 realtime/calls 第一跳 URL 与方法 | `raw-scrubbed/webrtc-20260728T134028Z/` （6 个 .bin） |
|  |  |  |  | `live2-20260728T140403Z` | **R** | 仅证明 realtime/calls 第一跳 URL 与方法 | `raw-scrubbed/live2-20260728T140403Z/` （8 个 .bin） |
| SPEC-EP-012 | ① OAuth 可见规则 | 内置 OpenAI OAuth | 🟡 部分 | `webrtc-20260728T134028Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/webrtc-20260728T134028Z/` （6 个 .bin） |
|  |  |  |  | `live2-20260728T140403Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/live2-20260728T140403Z/` （8 个 .bin） |
|  |  |  |  | `audit-ep012-realtime-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep012-realtime-20260730a/` （6 个 .bin） |
|  |  |  |  | `audit-ep012-sideband-synth-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep012-sideband-synth-20260730a/` （15 个 .bin） |
| SPEC-EP-013 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-h1raw-20260730a` | **R** | R 类原始请求证明 models 固定 query、responses 无 query | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
|  |  |  |  | `webrtc-20260728T134028Z` | **R** | realtime/calls 端点固定 query | `raw-scrubbed/webrtc-20260728T134028Z/` （6 个 .bin） |
| SPEC-EP-014 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `audit-ep014-beta-legacy-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep014-beta-legacy-20260730a/` （12 个 .bin） |
|  |  |  |  | `audit-ep014-turnstate-compact-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep014-turnstate-compact-20260730a/` （10 个 .bin） |
| SPEC-EP-015 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-search-20260728T132311Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
| SPEC-EP-019 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 洁净原始字节证明 wham usage／rate-limit-reset-credits 两个 GET 路径及 5 项 header 线序 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `audit-ep019-wham-consume-safe-20260730a` | **R** | 无外网、全假 OAuth、本地 TLS 终结器下由官方 app-server 生产代码生成的 consume 请求行、7 项 header 线序与 body | `raw-scrubbed/audit-ep019-wham-consume-safe-20260730a/` （1 个 .bin） |
| SPEC-EP-020 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
| SPEC-EP-021 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `relay-tui-recap-20260728T112358Z` | **R+J** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-tui-recap-20260728T112358Z/` （192 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-tui-recap-20260728T112358Z.json` |
|  |  |  |  | `audit-ep021-auto-clean-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep021-auto-clean-20260730a/` （40 个 .bin） |
| SPEC-EP-022 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-image-20260728T132405Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-image-20260728T132405Z/` （6 个 .bin） |
|  |  |  |  | `relay-imgedit1` | **R+J** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-imgedit1/` （10 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-imgedit1.json` |
| SPEC-EP-023 | ④ 内部机制 | — | 源码机制 | `relay-tui-recap-20260728T112358Z` | **R+J** | 洁净 TUI 原始字节中的 user_requested compaction_trigger 结果旁证 | `raw-scrubbed/relay-tui-recap-20260728T112358Z/` （192 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-tui-recap-20260728T112358Z.json` |
|  |  |  |  | `audit-ep021-auto-clean-20260730a` | **R** | 洁净自动压缩原始字节中的 context_limit compaction_trigger 结果旁证 | `raw-scrubbed/audit-ep021-auto-clean-20260730a/` （40 个 .bin） |
|  |  |  |  | `audit-ep023-comphash-20260730b` | **R** | 生产模型目录自然换模后，官方 V2 压缩帧精确标记 reason=comp_hash_changed；只旁证可见结果，不证明完整内部分派 | `raw-scrubbed/audit-ep023-comphash-20260730b/` （6 个 .bin） |
|  |  |  |  | `audit-ep023-downshift-20260730b` | **R** | I 类受控阈值下，官方 V2 压缩帧精确标记 reason=model_downshift；只旁证条件成立后的结果，不代表默认生产阈值 | `raw-scrubbed/audit-ep023-downshift-20260730b/` （2 个 .bin） |
| SPEC-EP-024 | ⑤ 采集记录 | — | 观测记录 | `audit-ep024-exec-negative-clean-20260730a` | **R** | 洁净 codex exec 负例：/compact 精确作为普通 user message 发出；3/3 双向、compaction_trigger=0、/responses/compact=0 | `raw-scrubbed/audit-ep024-exec-negative-clean-20260730a/` （6 个 .bin） |
|  |  |  |  | `relay-tui-recap-20260728T112358Z` | **R+J** | 洁净真 TUI 运行（96/96 双向）实际解析 /compact 并发出 compaction_trigger | `raw-scrubbed/relay-tui-recap-20260728T112358Z/` （192 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-tui-recap-20260728T112358Z.json` |
| SPEC-H1-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-h1raw-20260730a` | **R** | R 类 models／responses 原始字节直接证明 HTTP header 名全小写 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-H1-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-h1raw-20260730a` | **R** | R 类 models／responses 原始字节直接证明 host 位于用户头之后 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-H1-003 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-h1raw-20260730a` | **R** | R 类 POST /responses 原始字节直接证明 content-length 位于 host 之后 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-H1-004 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-ep014-turnstate-echo-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
|  |  |  |  | `audit-h1raw-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-H2-001 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明 SETTINGS 参数顺序 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验，不用于计数或缺失命题 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-002 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明 ENABLE_PUSH=0 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-003 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明 INITIAL_WINDOW_SIZE=2097152 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-004 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明 MAX_FRAME_SIZE=16384 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-005 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明 MAX_HEADER_LIST_SIZE=16384 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-006 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明首个连接级 WINDOW_UPDATE=5177345 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作正向原始帧交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-H2-007 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | J 类 3/3 完整连接直接证明四个请求伪头的顺序 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `relay-h2-20260728T032147Z` | **R+J** | R 类只作 HPACK 正向交叉核验 | `raw-scrubbed/relay-h2-20260728T032147Z/` （106 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-h2-relay-h2-20260728T032147Z.json` |
| SPEC-HDR-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `official-body2-20260728T000549Z` | **J** | 正文实测段所述形态；精确证明边界见规则正文 | `wire-parity-fix-20260727/h1-wire-probe/official-BODY-official-body2-20260728T000549Z.json` |
|  |  |  |  | `audit-hdr002-residency-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-hdr002-residency-20260730a/` （4 个 .bin） |
| SPEC-HDR-004 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | R 类 WS 握手原始字节中的 openai-beta 正例 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
|  |  |  |  | `audit-body002-plain-20260730a` | **R** | R 类 HTTP /responses 原始字节中的无 openai-beta 反例 | `raw-scrubbed/audit-body002-plain-20260730a/` （4 个 .bin） |
|  |  |  |  | `clean-image-20260728T132405Z` | **R** | R 类 images/generations 原始字节中的无 openai-beta 反例 | `raw-scrubbed/clean-image-20260728T132405Z/` （6 个 .bin） |
|  |  |  |  | `relay-imgedit1` | **R+J** | R 类 images/edits 原始字节中的无 openai-beta 反例 | `raw-scrubbed/relay-imgedit1/` （10 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-imgedit1.json` |
| SPEC-HDR-005 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-search-20260728T132311Z` | **R** | codex_exec UA 与 suffix 实例 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
|  |  |  |  | `clean-legacy-20260728T132509Z` | **R** | codex-tui UA 与 suffix 实例 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `audit-ep019-wham-consume-safe-20260730a` | **R** | codex_exec originator、unknown 终端标识与 codex_exec suffix 的 UA 实例 | `raw-scrubbed/audit-ep019-wham-consume-safe-20260730a/` （1 个 .bin） |
| SPEC-HDR-006 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
|  |  |  |  | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `clean-search-20260728T132311Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
|  |  |  |  | `clean-image-20260728T132405Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-image-20260728T132405Z/` （6 个 .bin） |
|  |  |  |  | `audit-h1raw-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
|  |  |  |  | `relay-imgedit1` | **R+J** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-imgedit1/` （10 个 .bin）<br>`wire-parity-fix-20260727/relay/official-relay-imgedit1.json` |
| SPEC-HDR-007 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-legacy-20260728T132509Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-legacy-20260728T132509Z/` （12 个 .bin） |
|  |  |  |  | `clean-search-20260728T132311Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-search-20260728T132311Z/` （8 个 .bin） |
|  |  |  |  | `audit-h1raw-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-h1raw-20260730a/` （6 个 .bin） |
| SPEC-HDR-008 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `audit-hdr008-guardian-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-hdr008-guardian-20260730a/` （12 个 .bin） |
|  |  |  |  | `audit-hdr008-memgen-20260730a` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/audit-hdr008-memgen-20260730a/` （14 个 .bin） |
|  |  |  |  | `relay-review4` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-review4/` （12 个 .bin） |
|  |  |  |  | `relay-rtmetrics1` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-rtmetrics1/` （4 个 .bin） |
| SPEC-PROTO-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `oauth-20260727T091556Z-noplugins` | **P** | N0 pcap 直接证明未配置自定义 CA 的 ClientHello 不含 ALPN 扩展 | `wire-parity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap）<br>`profile-fidelity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap） |
| SPEC-PROTO-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `official-httpfb3-20260727T234853Z` | **J** | J 类摘要记录 WS 重试耗尽后同进程发出 HTTP POST /responses | `wire-parity-fix-20260727/h1-wire-probe/official-HTTP-FALLBACK-official-httpfb3-20260727T234853Z.json` |
|  |  |  |  | `audit-ep014-turnstate-echo-20260730a` | **R** | R 类原始字节记录受控 426 后同进程从 WS 降级到 HTTP POST；只证明降级结果，不代替重试耗尽条件的 J 类记录 | `raw-scrubbed/audit-ep014-turnstate-echo-20260730a/` （10 个 .bin） |
| SPEC-TLS-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `oauth-20260727T091556Z-noplugins` | **P** | 正文实测段所述形态；精确证明边界见规则正文 | `wire-parity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap）<br>`profile-fidelity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap） |
| SPEC-TLS-002 | ② 自定义 CA 分支 | 自定义 CA | ✅ 验过 | `official-h2-20260727T131936Z` | **J** | 配置自定义 CA 后直接证明 negotiated_alpn=h2 | `wire-parity-fix-20260727/h2-wire-probe/official-baseline-official-h2-20260727T131936Z.json` |
|  |  |  |  | `audit-tls002-ca-n0-20260730a` | **P** | N0 pcap 直接证明配 CA 的 HTTP ClientHello 为 10 cipher，并依次 offer h2、http/1.1 | `wire-parity-fix-20260727/official-baseline/audit-tls002-ca-n0-20260730a/` （1 个 pcap） |
| SPEC-TLS-003 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `oauth-20260727T091556Z-noplugins` | **P** | 正文实测段所述形态；精确证明边界见规则正文 | `wire-parity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap）<br>`profile-fidelity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/` （6 个 pcap） |
| SPEC-WS-001 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | R 类 WS 握手原始字节直接证明前五项大小写与顺序 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
| SPEC-WS-002 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | R 类 WS 握手原始字节直接证明剩余项大小写与实际线序 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
| SPEC-WS-003 | ③ 自定义 provider 分支 | 自定义 provider | ✅ 验过 | `relay-wshdr3` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/relay-wshdr3/` （18 个 .bin） |
| SPEC-WS-004 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | 正文实测段所述形态；精确证明边界见规则正文 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
| SPEC-WS-005 | ① OAuth 可见规则 | 内置 OpenAI OAuth | ✅ 验过 | `clean-tool-20260728T132346Z` | **R** | Lite WS 原始帧与键序 | `raw-scrubbed/clean-tool-20260728T132346Z/` （4 个 .bin） |
|  |  |  |  | `audit-ws005-nonlite-20260730a` | **R** | 非 Lite WS 原始帧：warmup／增量键序及 instructions／tools 正向形态 | `raw-scrubbed/audit-ws005-nonlite-20260730a/` （4 个 .bin） |

## 3. 运行 → 关联编号项（43 个运行号 = 43 次物理采集）

同一次采集可能同时有短目录名和带工具前缀的摘要别名；物理次数按时间戳归一。
“关联”只表示该运行支撑表一写明的证明范围，不表示它完整证明每条规则的全部分支。

| 运行号 | 关联编号项 |
|---|---|
| `audit-body002-plain-20260730a` | SPEC-BODY-001 SPEC-BODY-002 SPEC-BODY-005 SPEC-BODY-006 SPEC-EP-001 SPEC-EP-005 SPEC-HDR-004 |
| `audit-body004-ws-turnstate-20260730a` | SPEC-BODY-004 |
| `audit-body007-workflow-clean-20260730a` | SPEC-BODY-007 |
| `audit-conn001-image-repeat-20260730a` | SPEC-CONN-001 |
| `audit-conn001-retry-disconnect-openai-http-20260730a` | SPEC-CONN-001 |
| `audit-conn001-retry-keepalive-openai-http-20260730a` | SPEC-CONN-001 |
| `audit-conn001-search-repeat-20260730a` | SPEC-CONN-001 |
| `audit-ep002-file-upload-full2-20260730a` | SPEC-EP-002 |
| `audit-ep012-realtime-20260730a` | SPEC-EP-012 |
| `audit-ep012-sideband-synth-20260730a` | SPEC-EP-002 SPEC-EP-012 |
| `audit-ep014-beta-legacy-20260730a` | SPEC-EP-014 |
| `audit-ep014-turnstate-compact-20260730a` | SPEC-BODY-004 SPEC-EP-014 |
| `audit-ep014-turnstate-echo-20260730a` | SPEC-BODY-001 SPEC-BODY-002 SPEC-BODY-003 SPEC-BODY-004 SPEC-BODY-005 SPEC-BODY-006 SPEC-EP-005 SPEC-H1-004 SPEC-PROTO-002 |
| `audit-ep019-wham-consume-safe-20260730a` | SPEC-EP-019 SPEC-HDR-005 |
| `audit-ep021-auto-clean-20260730a` | SPEC-EP-021 SPEC-EP-023 |
| `audit-ep023-comphash-20260730b` | SPEC-EP-023 |
| `audit-ep023-downshift-20260730b` | SPEC-EP-023 |
| `audit-ep024-exec-negative-clean-20260730a` | SPEC-EP-024 |
| `audit-h1raw-20260730a` | SPEC-EP-005 SPEC-EP-006 SPEC-EP-013 SPEC-H1-001 SPEC-H1-002 SPEC-H1-003 SPEC-H1-004 SPEC-HDR-006 SPEC-HDR-007 |
| `audit-hdr002-residency-20260730a` | SPEC-HDR-002 |
| `audit-hdr008-guardian-20260730a` | SPEC-HDR-008 |
| `audit-hdr008-memgen-20260730a` | SPEC-HDR-008 |
| `audit-tls002-ca-n0-20260730a` | SPEC-TLS-002 |
| `audit-ws005-nonlite-20260730a` | SPEC-BODY-001 SPEC-BODY-005 SPEC-WS-005 |
| `clean-image-20260728T132405Z` | SPEC-EP-001 SPEC-EP-005 SPEC-EP-022 SPEC-HDR-004 SPEC-HDR-006 |
| `clean-legacy-20260728T132509Z` | SPEC-BODY-002 SPEC-EP-005 SPEC-EP-007 SPEC-EP-014 SPEC-EP-019 SPEC-EP-020 SPEC-HDR-005 SPEC-HDR-006 SPEC-HDR-007 |
| `clean-search-20260728T132311Z` | SPEC-EP-005 SPEC-EP-008 SPEC-EP-015 SPEC-HDR-005 SPEC-HDR-006 SPEC-HDR-007 |
| `clean-tool-20260728T132346Z` | SPEC-BODY-001 SPEC-BODY-003 SPEC-BODY-005 SPEC-HDR-004 SPEC-HDR-006 SPEC-WS-001 SPEC-WS-002 SPEC-WS-004 SPEC-WS-005 |
| `clean2-conn-20260728T132008Z` | SPEC-CONN-001 |
| `live2-20260728T140403Z` | SPEC-EP-009 SPEC-EP-012 |
| `oauth-20260727T091556Z-noplugins` | SPEC-PROTO-001 SPEC-TLS-001 SPEC-TLS-003 |
| `oauth-ep002-allhosts` | SPEC-EP-002 |
| `oauth-ep002-refresh` | SPEC-EP-002 |
| `official-body2-20260728T000549Z` | SPEC-EP-006 SPEC-HDR-002 |
| `official-h2-20260727T131936Z` | SPEC-H2-001 SPEC-H2-002 SPEC-H2-003 SPEC-H2-004 SPEC-H2-005 SPEC-H2-006 SPEC-H2-007 SPEC-TLS-002 |
| `official-httpfb3-20260727T234853Z` | SPEC-PROTO-002 |
| `relay-h2-20260728T032147Z` | SPEC-H2-001 SPEC-H2-002 SPEC-H2-003 SPEC-H2-004 SPEC-H2-005 SPEC-H2-006 SPEC-H2-007 |
| `relay-imgedit1` | SPEC-EP-001 SPEC-EP-022 SPEC-HDR-004 SPEC-HDR-006 |
| `relay-review4` | SPEC-HDR-008 |
| `relay-rtmetrics1` | SPEC-HDR-008 |
| `relay-tui-recap-20260728T112358Z` | SPEC-EP-021 SPEC-EP-023 SPEC-EP-024 |
| `relay-wshdr3` | SPEC-WS-003 |
| `webrtc-20260728T134028Z` | SPEC-EP-009 SPEC-EP-012 SPEC-EP-013 |

## 4. 没有可用于验证机制本身的抓包（1 项）

这些项不是漏建索引：它们描述客户端内部调用／分派。抓包只能看到结果，
不能反推出内部机制；相关可见结果已归入独立 wire 规则。

| 编号项 | 分类 | 适用范围 | 验证状态 | 原因 |
|---|---|---|---|---|
| SPEC-HDR-001 | ④ 内部机制 | — | 源码机制 | 内部 header 组装／认证调用顺序只能由生产源码确认；wire 只能展示最终结果 |
