# Official Egress T1–T3：源码落点、公共骨架与独立传输画像

## 1. 范围

本文档记录 Official Egress 改造任务分解中的 T1–T3（当前实现状态见 README §1.1.3）：

1. 定位 Anthropic HTTP、OpenAI Responses HTTP、OpenAI Responses WebSocket 三条真实出站路径。
2. 使用脱敏的阶段 0 结构夹具固化 `v0.1.164` 当前行为和已知缺口。
3. 建立公共 Profile、字段来源、冻结上下文与 OAuth 账号资格判断。
4. 为三条路径实现互相隔离的传输画像、连接池键及 direct/代理/CA 支持。

T3 只完成传输层对齐，不提前修改 T4–T6 的 Body、Header 和帧语义。

## 2. 证据基线

- 源码基线：Sub2API `v0.1.164`。
- Vircs 运行基线：`v0.1.164-1`。
- 官方客户端：Claude Code `2.1.218`、Codex CLI `0.145.0`。
- 测试账号：Anthropic `#50`、OpenAI `#94`；路由组为 composite `#8`。
- 模型：`claude-sonnet-5`、`gpt-5.6-luna`。
- 抓包：Vircs `/root/oauth-capture/runs/phase0-*` 的 direct、mitm 和 ingress 配对。

测试代码只复刻字段结构、前缀、长度和生命周期关系。Token、Cookie、真实账号标识、完整 system、提示词、工具参数和动态 ID 均未写入仓库。

## 3. 三条真实出站链路

### 3.1 Anthropic HTTP

```text
GatewayService.Forward
→ OAuth body/filter/model 处理
→ GatewayService.buildUpstreamRequest
→ GatewayService.resolveAnthropicTLSProfileForRequest
→ HTTPUpstream.DoWithTLS
```

稳定调用点应放在 `buildUpstreamRequest` 完成现有 Header/Body 组装之后、返回 `http.Request` 之前。传输选择继续由 `resolveAnthropicTLSProfileForRequest` 和 `DoWithTLS` 承接，不能在 `Forward` 中散落 TLS 常量。

当前关键事实：

- 官方客户端入站也会经过 `IdentityService.GetOrCreateFingerprint` 与 `ApplyFingerprint`。
- 账号缓存的 Desktop UA 可覆盖当前 Claude CLI UA。
- `session_id_masking_enabled=true` 时，不同客户端会话会收敛为同一个账号级掩码。
- 阶段 0 中 system、`Bash` 工具名和无 `temperature` 已正确，不应重复改写。

### 3.2 OpenAI Responses HTTP

```text
OpenAIGatewayService.Forward
→ requestView 与 Codex OAuth transform
→ OpenAIGatewayService.buildUpstreamRequest
→ OpenAIGatewayService.doOpenAIHTTPUpstreamForRequest
→ HTTPUpstream.Do / DoWithTLS
```

稳定应用层调用点应放在所有现有 Body 转换完成之后、`buildUpstreamRequest` 之前；最终 Header 调用点应放在 `buildUpstreamRequest` 返回之前。HTTP Transport Profile 必须在 `doOpenAIHTTPUpstreamForRequest` 选择，不能复用 Anthropic 或 WS 的传输状态。

当前关键事实：

- 缺少 `instructions` 时，`Forward` 会调用 `defaultCodexSynthInstructions` 自动补齐。
- 默认 OAuth transform 会把长度不超过 64 的 `call_*` 改为 `fc_*`。
- `additional_tools`、`client_metadata`、`prompt_cache_key`、`include` 和 `parallel_tool_calls=false` 已保留。
- 官方 `session-id/thread-id` 不在当前白名单中；出站改为基于 `prompt_cache_key` 的 16 字符 `session_id/conversation_id`。

### 3.3 OpenAI Responses WebSocket

```text
OpenAIGatewayService.ProxyResponsesWebSocketFromClient
→ parseClientPayload
→ OpenAIGatewayService.buildOpenAIWSHeaders
→ openAIWSConnPool.Acquire
→ openAIWSConnPool.dialConn
→ openAIWSClientDialer.Dial
→ openAIWSConnLease.WriteJSONWithContextTimeout
```

`passthrough` 模式另走 `proxyResponsesWebSocketV2Passthrough`，但最终仍使用同一个 Dialer 抽象。握手 Finalizer 的稳定调用点应在 `buildOpenAIWSHeaders` 末端；帧 Finalizer 应位于 `parseClientPayload` 完成必要的模型/安全策略之后、写入 lease 之前；Transport Profile 应由 Dialer Provider 决定。

当前关键事实：

- 握手只读取 `session_id/conversation_id`，不读取官方 `session-id/thread-id`。
- `x-client-request-id`、`thread-id` 和 `version` 未进入最终握手 Header。
- 阶段 0 的普通续轮因首帧含 `generate=false`、续轮不含该字段，被严格非 input 比较判为 `non_input_changed`。
- 该判断会删除有效 `previous_response_id`，并把首轮 `additional_tools` 与历史消息合并成 full create。
- 工具结果帧因专门保护可保留 `previous_response_id`，因此不能用“所有 WS 续轮都丢失”概括当前行为。

## 4. 动态字段来源表

| 字段 | 当前来源/行为 | T2–T6 约束 |
|---|---|---|
| Claude UA 与 Stainless Header | 账号级 `IdentityService` 缓存可覆盖入口 | Profile 版本决定静态形态；已识别官方入口不得被旧缓存降级 |
| Claude metadata device/account | 缓存 ClientID + 账号 `account_uuid` | 必须记录字段来源，不复制抓包值 |
| Claude metadata session | 入站 session 派生后可被账号级固定掩码覆盖 | 改为客户端会话级稳定生命周期，禁止跨会话合并 |
| `X-Claude-Code-Session-Id` | 从最终 metadata session 回写 | 与最终 metadata 同源、同生命周期 |
| OpenAI `prompt_cache_key` | 入口 Body | 原样保留，并作为身份上下文的候选真实来源 |
| OpenAI `client_metadata` | 入口 Body，部分字段可由账号补充 | 原样字段优先；补充项必须记录来源 |
| OpenAI HTTP `session_id/conversation_id` | `api_key_id + prompt_cache_key` 的 16 字符哈希 | 不作为官方身份；T5 改为连字符 Header 语义 |
| OpenAI WS `session_id/conversation_id` | 下划线入口头或 `prompt_cache_key`，OAuth 再哈希 | 握手时冻结 session/thread/window/turn 上下文 |
| `x-codex-window-id` | 入口 Header 白名单透传 | 与 session/thread 生命周期一起校验 |
| `call_id` | 默认 OAuth transform 规范化为 `fc_*` | 上游允许且不超长时原样保留；调用与结果必须一致 |
| `previous_response_id` | 严格非 input 比较决定保留或展开历史 | 官方有效续轮优先保留；只在明确恢复分支展开 |
| turn metadata | Header，可同步进 `client_metadata` | Header、Body 和连接冻结上下文必须同源 |

## 5. T1 契约测试

`backend/internal/service/official_egress_t1_contract_test.go` 包含三组特征测试，本目录用于集中保存后续脱敏夹具与对应证据说明：

1. Anthropic：复现缓存 Desktop UA 覆盖、不同客户端 session 被账号级掩码合并，同时确认 system、`Bash` 和 `temperature` 当前无需改造。
2. OpenAI HTTP：复现自动 `instructions`、`call_* → fc_*` 和下划线身份头，同时确认已有 Responses Lite 字段保持。
3. OpenAI WS：复现握手身份头缺口，以及 `non_input_changed → 删除 previous_response_id → 合并 5 项历史`。

这些断言记录实施前的历史基线。当前实现已改为 Anthropic/OpenAI OAuth 自动应用
Profile，API Key 和其他非适用账号承担“保持原行为”的隔离基准。

## 6. T2 预定公共调用点

T2 只允许引入薄的公共抽象：

1. 在账号选定后构造 `OfficialEgressContext`，HTTP 重试换账号时重建，WS 握手成功后冻结。
2. 在三条稳定调用点调用 Resolver/Finalizer，不分叉完整 Handler。
3. 非适用账号跳过所有 Finalizer 和专用 Transport，保持本文件的 T1 契约。
4. OAuth Profile 的 Host、字段来源、账号状态或传输配置矛盾时明确失败并记录脱敏原因。
5. T2 不写 Claude、OpenAI HTTP、OpenAI WS 的具体画像常量；路径实现分别留到 T4、T5、T6。

## 7. T2 实际落点

T2 已按上述边界实现，仍未写入三条路径的具体应用或传输画像：

1. `official_egress_profile.go` 集中定义账号资格判断、服务端版本、Resolver、字段来源与生命周期、最终校验、连接池键和脱敏日志。
2. `official_egress_integration.go` 负责把公共上下文挂到 HTTP 请求或 WS 连接上下文；HTTP 每次重建请求时重新解析，WS 在拨号前冻结。
3. Anthropic HTTP、OpenAI HTTP 和 OpenAI WS 的大型 Handler 仅保留薄调用点，没有复制主链路。
4. Anthropic OAuth/SetupToken 与 OpenAI OAuth 自动使用官方画像；管理端不提供账号级开关或版本选择。
5. 当前唯一版本为 `phase0-2026-07-24`。旧 TLS 指纹、固定会话掩码和缓存 TTL 强制替换在适用账号上失效并在保存时清除；自定义转发地址与内置画像互斥，账号、入口、传输或官方 Host 不匹配时明确失败，不静默降级。

`official_egress_profile_test.go` 覆盖以下公共契约：

- API Key 等非适用账号的请求对象和上下文保持不变。
- 错误 Host、账号冲突和入口冲突明确失败。
- HTTP 重试/切换账号会重建独立上下文。
- WS 上下文在拨号前冻结，冻结后拒绝修改。
- 动态字段的值、来源或生命周期冲突时明确失败。
- 普通日志只记录字段名及来源，不包含字段原值。
- 连接池键区分账号、版本、传输、Host、代理与 CA。

## 8. T2 Vircs 实证

验证环境仍为 Vircs 独立目录 `/root/oauth-capture`，没有在本地或 Sub2API
生产容器内补建抓包工具。验证版本为 `0.1.164-1-t2`，官方客户端仍为
Claude Code `2.1.218` 与 Codex CLI `0.145.0`。

以下“关闭/开启”是 T2 开发阶段的历史 A/B 证据；当前版本已经取消账号级开关，
OAuth 自动使用内置 Profile。

### 8.1 Profile 关闭

- direct + ingress：`/root/oauth-capture/runs/t2-off-direct-20260724T171929Z`。
- 完整 MITM + ingress：时间戳 `20260724T172212Z`。
- Claude、OpenAI HTTP、OpenAI WS 的 S1/S2/S4、账号命中与 usage 全部通过。
- 与阶段 0 相比，三条路径的应用结构和 direct TLS/ALPN 均未变化；画像命中日志为 0。

### 8.2 Profile 开启

- direct + ingress：`/root/oauth-capture/runs/t2-off-direct-20260724T173120Z`。目录名沿用旧脚本命名，但运行时账号开关实际为开启。
- 完整 MITM + ingress：时间戳 `20260724T173157Z`。
- Claude、OpenAI HTTP、OpenAI WS 的 S1/S2/S4、账号命中与 usage 全部通过，应用结构仍与阶段 0 相同，证明 T2 没有提前实施 T4–T6 的路径画像。
- 日志分别记录 Anthropic HTTP、OpenAI HTTP 与 OpenAI WS 命中；WS 为 `frozen=true`。代理场景记录代理编号，但不记录身份值。
- direct ClientHello 仍为阶段 0 的 Sub2API 基线，符合 T2 只建立公共骨架、不改变 Transport 的任务边界。

验证结束后临时代理、CA 和 keeper 状态均已恢复，Sub2API 容器健康且无重启。

## 9. T3 独立传输画像

T3 将应用层画像与传输画像分离，新增三套集中管理的 Provider：

1. Anthropic HTTP：17 个密码套件、固定 Node.js 扩展顺序、ALPN `http/1.1`。
2. OpenAI HTTP：直连使用 30 个密码套件、Codex HTTP 扩展顺序和空 ALPN；
   HTTP CONNECT 代理边界使用同轮官方 `client_to_mitm` 已实证的 10 个密码套件、
   `h2,http/1.1` 和随机扩展顺序，通过 HTTP/2 发包。两种状态由代理配置选择，
   且不能共用 Transport。
3. OpenAI WebSocket：10 个密码套件、空 ALPN；扩展集合固定，但每次新握手随机排列，避免错误固化为单一 JA3。

HTTP Transport 缓存键和 WebSocket 连接兼容键均包含账号、Profile 版本、传输画像、目标 Host、代理与 CA 状态。三条路径不能跨画像复用连接；WebSocket 后台预热也必须沿用握手前冻结的 `OfficialEgressContext`。

代理与 CA 支持覆盖 direct、HTTP、HTTPS 和 SOCKS5。HTTPS 代理先与代理建立受验证的 TLS，再执行 CONNECT，随后使用目标路径画像完成上游 TLS。代理凭据只参与哈希后的内部状态键，不进入普通日志或连接池可见键。

对应测试位于：

- `backend/internal/pkg/tlsfingerprint/official_egress_t3_test.go`
- `backend/internal/repository/http_upstream_test.go`
- `backend/internal/service/official_egress_transport_test.go`

## 10. T3 Vircs 实证

验证镜像为 `sub2apiplus:official-egress-t3-20260725`，运行版本为
`0.1.164-1-t3`。抓包仍使用 Vircs 独立环境 `/root/oauth-capture`。

### 10.1 direct + ingress

- 运行目录：`/root/oauth-capture/runs/t2-off-direct-20260724T180150Z`。目录名沿用旧脚本，但运行镜像与账号开关实际为 T3 开启状态。
- Claude、OpenAI HTTP、OpenAI WS 的 S1、账号命中与 usage 均通过。
- Claude ClientHello 为 17 个密码套件、目标固定扩展顺序和 `http/1.1`。
- OpenAI HTTP ClientHello 为 30 个密码套件、目标固定扩展顺序和空 ALPN。
- OpenAI WS 的 4 次 ClientHello 均为目标 10 个密码套件和空 ALPN；扩展集合一致而顺序互不相同，符合官方客户端的随机排列特征。
- 命中日志分别记录三套不同 `transport_profile`，WS 为 `frozen=true`。

### 10.2 MITM + ingress

- 完整矩阵时间戳：`20260724T180244Z`。
- Claude：S1/S2/S4 全部通过，共 5 条 HTTP 记录，账号 #50 usage 增加 5。
- OpenAI HTTP：S1/S2/S4 全部通过，共 5 条 HTTP 记录，账号 #94 非 WS usage 增加 5。
- OpenAI WS：S1/S2/S4 全部通过，共 4 条握手记录与 137 条 WS 记录，账号 #94 WS usage 增加 9。
- 三条路径均通过临时 HTTPS MITM 代理和自定义 CA；日志中的代理编号、传输画像及 WS 冻结状态正确，未输出代理凭据或动态身份值。

验证结束后账号代理、fallback、CA 和 keeper 状态已恢复。当前容器健康、重启次数为 0。

后续 T5 抓包复核发现，本时间戳中的 OpenAI HTTP 代理请求仍为 HTTP/1.1；
阶段 0 官方代理边界实际为 10-cipher、`h2,http/1.1`、HTTP/2，而 direct
边界才是 30-cipher、空 ALPN。该 T3 残留已随 T5 修正为直连/代理自适应画像，
最终证据见第 14 节。

## 11. T4 Anthropic HTTP 应用画像

T4 在 `buildUpstreamRequest` 的末端接入 Claude 专用 Finalizer，不分叉既有转发、重试、
流式响应和计费链路：

1. 适用内置 Profile 的 OAuth 账号不再读取账号级 Desktop 指纹，也不再执行账号级固定会话掩码。
2. `device_id` 与 `session_id` 来自入口 `metadata.user_id`，账号 UUID 来自所选账号静态状态；
   Header session 与 metadata session 冲突时明确失败。
3. `x-client-request-id` 按请求生成；UA、Beta 与当前 Stainless/Header 画像集中在
   `phase0-2026-07-24` 版本中。
4. 三块入口 system 只在唯一结构边界
   `# Text output (does not apply to tool calls)` 存在时拆成四块，并按当前官方样本移动
   cache-control；不复制完整官方 system 文本。
5. billing 后缀跳过 `<system-reminder>`，由会话首轮真实用户提示派生；
   `cc_entrypoint=sdk-cli`。响应 `request-id` 只用于诊断，不回灌到下一轮请求体。
6. 当前官方二进制源代码只暴露 `cch=00000` 占位符，阶段 0 wire 值与仓库旧版
   xxHash64 算法、Bun 内置哈希均不一致。按照 §3.3 的安全边界，本实现省略 `cch`，
   不复制抓包值、不恢复已证伪的旧算法；其余 billing 字段均有可验证来源。

对应测试位于：

- `backend/internal/service/official_egress_anthropic_test.go`
- `backend/internal/repository/gateway_cache_official_egress_test.go`

## 12. T4 Vircs 实证

最终验证镜像为 `sub2apiplus:official-egress-t4-r2-20260725`，运行版本为
`0.1.164-1-t4r2`。抓包仍使用 Vircs 独立环境 `/root/oauth-capture`。

### 12.1 direct + ingress

- 运行目录：`/root/oauth-capture/runs/t2-off-direct-20260724T184108Z`。
- Claude、OpenAI HTTP、OpenAI WS 的 S1、账号命中与 usage 全部通过。
- Claude ClientHello 仍为 17 个密码套件、固定目标扩展顺序和 `http/1.1`；
  OpenAI HTTP/WS 的 T3 传输画像也无回归。

### 12.2 MITM + ingress

- 最终矩阵时间戳：`20260724T185038Z`。
- Claude S1/S2/S4 为 5/5 HTTP 200，账号 #50 usage 增加 5。
- 五条请求保持三个客户端会话；Header session 与 metadata session 5/5 相等，
  `x-client-request-id` 5/5 不重复。
- 五条 system 均为四块；入口第三块与出站第三、四块重新拼接后 5/5 逐字相等；
  第三块 cache-control 为 `ephemeral + 1h + global`，第四块为 `ephemeral + 1h`。
- billing 前缀与入口官方客户端 5/5 相等；所有场景均不生成未被官方请求取证的
  `cc_prev_req`。
- 除 system 与 metadata 外的 Body 5/5 相等，`Bash` 保持不变且无 `temperature`；
  静态 Header 集合和值与官方 Claude Code 阶段 0 样本相等。
- 同轮 OpenAI HTTP 为 5/5、OpenAI WS 为 9/9，账号 #94 路由和 usage 无回归。

验证结束后账号代理、fallback、CA 和 keeper 状态已恢复。当前容器健康、重启次数为 0。

## 13. T5 OpenAI Responses HTTP 应用画像

T5 在现有 OAuth 转换之后、真正构建上游请求之前接入 OpenAI HTTP 专用
Body/Header Finalizer：

1. 入口没有 `instructions` 时跳过默认合成并移除转换过程可能补入的无依据值；
   入口显式 `instructions` 必须保持语义相等。
2. OAuth 转换保留合法 `call_id`，工具调用与工具结果不再发生
   `call_* → fc_*` 改写。
3. `additional_tools`、`client_metadata`、`prompt_cache_key`、`include` 和
   `parallel_tool_calls` 以入口契约为准；compact 只允许按既有 compact schema
   省略字段。
4. Body 的 session/thread/prompt cache 与入口连字符 Header、window 和 turn
   metadata 必须属于同一 UUID 生命周期；字段来源登记为入口显式值，冲突时明确失败。
5. 删除旧的 `session_id`、`conversation_id` 和 `OpenAI-Beta`，使用当前官方
   `session-id`、`thread-id`、`x-client-request-id` 及 Codex metadata Header。
6. HTTP 重试和账号切换每次重新挂载上下文及连接池身份，不复用前一账号状态。
7. OpenAI HTTP Transport 按实证区分 direct 与代理：direct 使用 30-cipher、
   空 ALPN；HTTP CONNECT 使用 10-cipher、`h2,http/1.1`、随机扩展顺序和
   HTTP/2。空 ALPN 直连不得强制发送 HTTP/2。

对应测试位于：

- `backend/internal/service/official_egress_openai_http_test.go`
- `backend/internal/service/openai_upstream_http_test.go`
- `backend/internal/service/official_egress_transport_test.go`

测试覆盖 Responses 非流式/SSE、工具与续轮、compact、拒绝字段重试、账号切换、
显式字段保持和身份冲突失败。`internal/service`、`internal/repository` 与
`internal/pkg/tlsfingerprint` 全包回归均通过。

## 14. T5 Vircs 实证

最终验证镜像为 `sub2apiplus:official-egress-t5-r3-20260725`，运行版本为
`0.1.164-1-t5r3`。抓包仍只使用 Vircs 独立环境 `/root/oauth-capture`。

### 14.1 传输层修正过程

- 初始 T5 MITM 时间戳 `20260724T193149Z` 已证明全部应用字段通过，但同时暴露
  OpenAI HTTP 仍为 HTTP/1.1。
- 随后的“空 ALPN 强制 H2”试验在 direct 运行
  `/root/oauth-capture/runs/t2-off-direct-20260724T194047Z` 中被上游明确拒绝，
  OpenAI HTTP 返回 502，因此未作为完成证据。
- 对照阶段 0 `client_to_mitm` 后确认官方 Codex HTTP 的代理边界为
  10-cipher、`h2,http/1.1`、HTTP/2，direct 边界为 30-cipher、空 ALPN；
  最终实现据此按代理状态选择两套已实证画像。

### 14.2 direct + ingress

- 最终运行目录：
  `/root/oauth-capture/runs/t2-off-direct-20260724T194927Z`。
- Claude、OpenAI HTTP、OpenAI WS 的 S1、账号命中与 usage 全部通过。
- Claude ClientHello 为 17-cipher、`http/1.1`；OpenAI HTTP 为
  30-cipher、空 ALPN；OpenAI WS 的四次目标握手均为 10-cipher、空 ALPN，
  且扩展顺序变化。

### 14.3 MITM + ingress

- 最终完整矩阵时间戳：`20260724T195015Z`。
- Claude 为 5/5，OpenAI HTTP 为 5/5，OpenAI WS 为 9/9；usage 分别精确增加
  5、5、9，并命中账号 #50、#94、#94。
- OpenAI HTTP 5/5 为 HTTP/2/200；代理边界 5/5 为官方同款 10 个密码套件、
  `h2,http/1.1`，随机扩展顺序由连接建立时确定。
- OpenAI HTTP ingress 与 egress 的 Body 5/5 完全相等；顶层键形态 5/5
  匹配官方有效基线，`instructions` 5/5 缺省。
- `additional_tools`、`call_id`、`client_metadata`、`prompt_cache_key`、
  `include`、`parallel_tool_calls` 均为 5/5 精确保持。
- 连字符身份 Header、Body 身份关系和 Codex metadata 5/5 一致；
  `session_id`、`conversation_id`、`OpenAI-Beta` 5/5 不存在。

验证结束后 #50/#94 的代理与 fallback 均为空，临时代理已删除，CA 哈希已恢复，
keeper 正常运行；Sub2API 容器健康且重启次数为 0。

## 15. T6 Vircs 实证

最终验证镜像为 `sub2apiplus:official-egress-t6-r2-20260725`，运行版本为
`0.1.164-1-t6r2`。

### 15.1 direct + ingress

- 最终运行目录：
  `/root/oauth-capture/runs/t2-off-direct-20260724T202412Z`。
- Claude、OpenAI HTTP、OpenAI WS 的 S1、账号命中与 usage 全部通过，分别增加
  1、1、2；日志没有 WS 终态校验失败或隐式回放。
- Claude ClientHello 为 17-cipher、`http/1.1`；OpenAI HTTP 为
  30-cipher、空 ALPN；OpenAI WS 的四次目标握手均为 10-cipher、空 ALPN，
  且扩展顺序随机变化。
- 初版 T6 r1 的 direct 实证发现连接级 prewarm Header 会覆盖后续帧自己的
  turn metadata；r2 改为冻结握手身份但保留逐帧 metadata，修正后没有重连补偿。

### 15.2 MITM + ingress

- 最终完整矩阵时间戳：`20260724T202500Z`。
- Claude 为 5/5，OpenAI HTTP 为 5/5，OpenAI WS 为 9/9；usage 分别精确增加
  5、5、9，并命中账号 #50、#94、#94。
- OpenAI WS 的 9 个 `response.create` 在入口与出口语义 9/9 完全相等；
  5 个有效续轮/工具结果帧 5/5 保留 `previous_response_id`。
- `input`、`client_metadata`、`prompt_cache_key` 均为 9/9 精确保持；
  续轮没有重复 `additional_tools`，工具结果帧的 `call_id` 未改写。
- 4 次握手均为 HTTP/1.1 101；Header 键集合与官方样本完全一致，
  `session-id`、`thread-id`、`x-client-request-id` 同源，旧
  `session_id/conversation_id` 与安装标识 Header 均不存在。
- 4 次握手的 `version=0.145.0`、OAuth Beta 与
  `permessage-deflate; client_max_window_bits` 压缩提议均与官方一致；
  ClientHello 均为 10-cipher、空 ALPN，扩展顺序各不相同。

验证结束后 #50/#94 的代理为空，临时代理已删除，CA 哈希一致，keeper 正常运行；
Sub2API 容器健康且重启次数为 0。

## 16. T7 回归、性能与合并

最终验证镜像为 `sub2apiplus:official-egress-t7-final-20260725`，运行版本为
`0.1.164-1-t7`。T7 只在 WS 帧 Finalizer 增加“候选帧与原帧相等时直接返回”
快路径，不改变 T6 的应用或传输画像。

### 16.1 自动化回归与性能

- 后端 `go test ./... -count=1` 全部通过。
- 前端类型检查、只读 lint、全量测试和构建全部通过；全量测试为
  189 个测试文件、1304 个用例。
- `internal/service` 的 Official Egress/OpenAI WS 竞态测试，以及
  `internal/repository` 的 Official Egress/缓存/HTTP 上游竞态测试全部通过。
- 196744 字节 WS `response.create` 样本连续三轮基准：
  无画像上下文为 2.535–2.555 ns/op，内置画像且帧未变化为
  5.652–5.661 ns/op；两者均为 0 B/op、0 allocs/op。
- Resolver 继续使用已经选定的账号与缓存状态，没有在缓存命中请求中新增 SQL 查询；
  HTTP Transport 和 WS 连接仍按稳定 Profile 兼容键复用。最终 direct S1 共形成
  Claude HTTP 1 次、OpenAI HTTP 1 次、OpenAI WS 4 次目标 TLS 握手；
  WS 的 2 条 usage 共用该账号的预热连接池，没有按帧重复握手。

### 16.2 连续三个 Plus 集成标签演练

仓库没有 `v0.1.163-1`，因此采用按提交时间连续可用的三个 Plus 集成标签
`v0.1.161-1`、`v0.1.162-1`、`v0.1.164-1`。直接对原始上游同名标签回放时，
旧标签缺少 Plus 专有依赖，整文件取新版会导致编译失败，故不把该错误基线计入
正式演练。

| 标签 | 冲突与处理 | 处理时间 | 验证结果 |
|---|---|---:|---|
| `v0.1.161-1` | `EditAccountModal.vue` 1 个 import 冲突块；保留旧标签能力，仅加入 Official Egress 组件。初次整文件取新版被类型检查发现会带入尚不存在的 Ollama 字段，已改为最小解 | 最终校正 8 分 43 秒，其中包含错误解法排查 | T2–T6 契约、repository、TLS 与前端类型检查通过 |
| `v0.1.162-1` | 同一文件、同一 import 冲突块；最小加入 Official Egress 组件 | 27 秒 | T2–T6 契约、repository、TLS 与前端类型检查通过 |
| `v0.1.164-1` | 0 个冲突文件、0 个冲突块 | 271 毫秒 | T1–T6 契约、repository、TLS 与前端类型检查通过 |

`v0.1.161-1` 与 `v0.1.162-1` 的两条 T1 OpenAI 基线刻画断言预期
`window-*:0`，而旧标签当时尚未生成该字段；这是 T1 针对 `v0.1.164-1`
刻画的版本差异，不是新 Profile 的功能失败。三次演练均未在后端高频 Handler
产生冲突，画像变化仍集中在 Profile/Finalizer。

### 16.3 最终 Vircs 抓包

- direct + ingress：
  `/root/oauth-capture/runs/t2-off-direct-20260724T205847Z`。
  Claude、OpenAI HTTP、OpenAI WS 的 S1 全部通过，usage 精确增加 1、1、2；
  pcap 共含 6 个目标 ClientHello：Claude 为 17-cipher/`http/1.1`，
  OpenAI HTTP 为 30-cipher/空 ALPN，OpenAI WS 四次均为
  10-cipher/空 ALPN且扩展顺序不同。
- 第一次完整 MITM 时间戳 `20260724T210004Z` 在 OpenAI HTTP S1 前发生
  MITM 到 `chatgpt.com` 的服务端 TLS 握手被关闭，尚未形成上游 HTTP 记录；
  该轮按 300 秒超时退出并完整恢复环境，不计入验收。随后独立
  `openssl s_client` 已确认上游 TLS 恢复。
- 最终完整 MITM + ingress 时间戳为 `20260724T210705Z`：
  Claude、OpenAI HTTP、OpenAI WS 的 S1/S2/S4 全部通过，usage 精确增加
  5、5、9；抓到 5 条 Claude HTTP、5 条 OpenAI HTTP、4 次 WS 握手和
  137 条 WS 记录。
- Claude 仍为 5/5 HTTP/1.1/200，三个会话保持三个身份，system 均为四块，
  `Bash` 与无 `temperature` 行为无回归。
- OpenAI HTTP 为 5/5 HTTP/2/200；入口与出口 Body JSON 5/5 语义相等，
  五类身份 Header 5/5 同源，旧下划线身份和旧 Beta 均不存在；代理边界为
  10-cipher、`h2,http/1.1`。
- OpenAI WS 的 9 个 `response.create` 入口与出口 9/9 语义相等；
  `input`、`client_metadata`、`prompt_cache_key`、`additional_tools` 和
  `previous_response_id` 均为 9/9 相等，5 个有效续轮均保留
  `previous_response_id`。Go WS 写出层会重新编码 JSON 转义，因此不把原始文本
  字节相等作为协议要求；规范化 JSON 为 9/9 相等，未知帧零改写另有字节级单测。
- 4 次 WS 握手均为 HTTP/1.1 101，规范化 Header 键集合 4/4 与官方入口一致；
  连字符身份同源、旧下划线身份与安装标识均不存在，
  `version=0.145.0`、OAuth Beta 和压缩提议均为 4/4 正确；
  ClientHello 均为 10-cipher/空 ALPN，扩展顺序各不相同。

最终检查确认 #50/#94 的代理和 fallback 为空、临时代理已删除、CA 哈希一致、
keeper 正常运行；Sub2API 使用最终 T7 镜像，容器健康且重启次数为 0。

## 17. N1 原生客户端补充契约

N1 与 T1–T7 的 Official Egress Profile 保持独立，只修复原生客户端入口契约和
本轮实证暴露的通用日志脱敏问题：

1. composite 分组携带 `client_version` 请求 `/v1/models` 时返回 Codex 所需的
   `models` manifest；账号选择仍只允许当前分组内的 OpenAI 账号。
2. Official Egress Profile 只挂载到已定义画像的端点：Anthropic 仅
   `/v1/messages`，OpenAI 仅 `/v1/responses` 与 `/v1/responses/compact`。
   Chat Completions 继续走原有兼容链路，不会因误挂 Responses Profile 而返回 502。
3. composite 的 Gemini Native 请求沿用已解析的 Gemini 目标平台，并保留项目原有的
   Gemini + `mixed_scheduling` Antigravity 调度语义；账号仍限定在当前 composite
   分组内。
4. 通用日志脱敏新增 `api_key` 字段和嵌套 JSON 字符串值处理；403 的普通日志与账号
   错误状态不再写入上游返回的明文密钥。

对应测试位于：

- `backend/internal/server/routes/gateway_codex_models_test.go`
- `backend/internal/handler/openai_codex_models_handler_test.go`
- `backend/internal/service/official_egress_profile_test.go`
- `backend/internal/service/gemini_messages_compat_service_test.go`
- `backend/internal/service/ratelimit_service_403_test.go`
- `backend/internal/util/logredact/redact_test.go`

### 17.1 测试凭据排除与安全实证

- Vircs 中历史 Gemini #9、#58 均为软删除账号。#9 API Key 的上游消费者已暂停；
  #58 OAuth 可以刷新 Token，但其 Google 项目未启用 Gemini for Google Cloud API。
  两者产生的 403/502 不作为功能验收样本，临时组关联、账号状态、凭据、Extra 和软删除
  时间均已恢复。
- 安全抓包目录为
  `/root/oauth-capture/runs/n1-security-20260724T214010Z`：失效 #9 请求形成 1 次真实
  Google ClientHello，服务日志中的明文密钥计数为 0、`api_key:***` 脱敏标记为 2。
- Gemini 成功验收使用组 8 原有 OAuth Antigravity #57，临时打开
  `schedulable + mixed_scheduling` 后通过 Gemini Native 入口调用；测试结束后配置哈希
  精确恢复。usage 与对应的 `last_used_at/updated_at` 作为验收遥测保留。

### 17.2 最终 N1 Vircs 抓包

最终镜像为 `sub2apiplus:official-egress-n1-final3-20260725`，运行版本为
`0.1.164-1-n1`。最终 direct + ingress 目录为
`/root/oauth-capture/runs/n1-final5-20260724T215324Z`：

- 六个入口请求全部为 200，ingress 保存 2 个 JSONL、共 6 条记录：
  Codex `/v1/models` 首次即返回 5 个 `models`；Chat Completions 非流式返回
  `chat.completion`，SSE 有非空 delta 和 `[DONE]`；Gemini models 返回 8 个模型，
  非流式与 SSE 均返回目标标记。
- Chat 非流式/流式分别命中 #94，各记录 1 条 usage；Gemini 非流式/流式分别命中
  #57，各记录 1 条 usage。日志中没有 “OpenAI HTTP Profile 仅支持 Responses”
  的越界错误。
- direct pcap 为 98477 字节、包含 3 个 ClientHello：
  `chatgpt.com` 的模型清单请求为 13-cipher/空 ALPN，Chat 链路为
  13-cipher/`h2,http/1.1`，Gemini Native 为
  `cloudcode-pa.googleapis.com` 13-cipher/`h2,http/1.1`。
- #57 的非遥测配置哈希恢复一致；#9/#58 不在组 8，#57 恢复原有组关系、
  `schedulable=false` 且没有 `mixed_scheduling`；主服务健康、重启次数为 0。

最终三路径回归目录为
`/root/oauth-capture/runs/t2-off-direct-20260724T215605Z`：Claude、Codex HTTP、
Codex WS 的 S1 全部 valid，usage 为 #50 1 条、#94 HTTP 1 条、#94 WS 2 条；
Codex stderr 的模型 schema 错误为 0。pcap 再次确认 Claude 17-cipher/`http/1.1`、
OpenAI HTTP 30-cipher/空 ALPN、OpenAI WS 10-cipher/空 ALPN，T1–T7 无回归。

N1 完成后的后端全量测试、N1 相关 routes/handler/service/logredact 竞态测试均通过。
#50/#94 的代理与 fallback 仍为空，keeper 正常运行，主服务使用最终 N1 镜像且健康。

## 18. OpenAI 配置与 Official Egress 模式独立性补测

最终补丁镜像为 `sub2apiplus:official-egress-mode-independent-20260725`，运行版本为
`0.1.164-1-mode-independent`。修复后的边界是：自动透传与 WS mode 只选择处理
链路和实际出站协议，不能跳过 Official Egress Finalizer 或 Transport Provider。

- HTTP 自动透传开启、WS mode 为 `off`：
  `/root/oauth-capture/runs/mode-independent-http-20260725T001732Z`。
  Codex HTTP S1 成功并命中 #94；日志为 `openai-http-phase0-2026-07-24`，
  direct pcap 的目标 ClientHello 为 30-cipher、空 ALPN。
- WS mode 为 `ctx_pool`：
  `/root/oauth-capture/runs/mode-independent-ws-ctx-pool-20260725T001906Z`。
  Codex WS S1 成功并命中 #94；日志为 `openai-ws-phase0-2026-07-24`，
  四次目标 ClientHello 均为 10-cipher、空 ALPN。
- WS mode 为 `passthrough`：
  `/root/oauth-capture/runs/mode-independent-ws-passthrough-20260725T001951Z`。
  Codex WS S1 成功；日志仍为 `openai-ws-phase0-2026-07-24`，
  目标 ClientHello 仍为 10-cipher、空 ALPN。
- WS mode 为 `http_bridge`：
  `/root/oauth-capture/runs/mode-independent-ws-http-bridge-20260725T002049Z`。
  WebSocket 入站经 HTTP 上游完成；日志正确切换为
  `openai-http-phase0-2026-07-24`，不再产生虚假 WS Profile 日志；
  direct pcap 的目标 ClientHello 为 30-cipher、空 ALPN。
- WS mode 为 `off`：
  `/root/oauth-capture/runs/mode-independent-ws-off-20260725T002150Z`。
  Codex 收到 WS 错误事件后回退 HTTP 并完成 S1；唯一命中日志为
  `openai-http-phase0-2026-07-24`，目标 ClientHello 为 30-cipher、空 ALPN。

自动化测试覆盖自动透传在 `off/ctx_pool/passthrough/http_bridge` 四种 mode 下均
保持 HTTP Profile，以及 HTTP bridge 使用 HTTP Profile 和逐轮 turn metadata。
`go test ./internal/service -count=1` 与 `go test ./... -count=1` 均通过。测试后账号
#94 已恢复为 `openai_passthrough=false`、WS mode=`ctx_pool`；OAuth 官方画像
自动生效，主服务健康且重启次数为 0。

## 19. N2 Kilo 第三方客户端接入实证

N2 验证 Kilo Code VS Code 扩展从标准 API 接入时，Sub2API 仍按实际出站协议应用
Official Egress Profile。Anthropic 使用 OAuth #50 和 `claude-sonnet-5`；OpenAI
使用 OAuth #94 和 `gpt-5.6-luna`。

### 19.1 Anthropic HTTP

- direct：
  `/root/oauth-capture/runs/kilo-anthropic-t8a3-matrix-20260725T015954Z`。
- S1 MITM：
  `/root/oauth-capture/runs/kilo-anthropic-t8a2-mitm-20260725T015156Z`。
- S4 MITM：
  `/root/oauth-capture/runs/kilo-anthropic-a4-mitm-20260725T021334Z`。
- 两条 MITM 请求均命中 `api.anthropic.com/v1/messages?beta=true` 并返回 200；
  S4 请求包含配对的 `tool_use/tool_result`，同时保持 Claude Code
  `2.1.218` 的应用与 HTTP/1.1 传输画像。

### 19.2 OpenAI HTTP

- direct：
  `/root/oauth-capture/runs/kilo-openai-http-a3-direct-20260725T024736Z`。
- S1 MITM：
  `/root/oauth-capture/runs/kilo-openai-http-a3-mitm-20260725T025030Z`。
- S4 MITM：
  `/root/oauth-capture/runs/kilo-openai-http-a4-mitm-20260725T025640Z`。
- S1 为 1/1 HTTP 200；S4 的工具请求和工具结果续轮为 2/2 HTTP/2 200，最终返回
  `KILO_OAI_HTTP_S4_TOOL_OK`，并保持 Codex CLI `0.145.0` HTTP Profile。

### 19.3 OpenAI WebSocket

S1/S2/S4 运行目录为
`/root/oauth-capture/runs/kilo-openai-ws-a2-ctx-pool-mitm-20260725T035700Z`。
最终 S4 修复镜像为 `sub2apiplus:official-egress-kilo-openai-ws-a4-20260725`，
运行版本为 `0.1.164-1-kilo-openai-ws-a4`，MITM 目录为
`/root/oauth-capture/runs/kilo-openai-ws-a4-ctx-pool-mitm-20260725T052500Z`：

- Kilo 的完整历史被收敛为三条 `response.create`：`generate=false` 预热帧、关联
  预热响应的正式工具请求、只包含一个 `function_call_output` 的工具结果续轮。
- 三条上游响应均为 `response.completed`；两次 `previous_response_id` 分别精确关联
  前一条真实响应，工具结果的 `call_id` 与上游工具调用一致。
- 正式工具请求与工具结果续轮的 session/thread/window、`turn_id` 和
  `turn_started_at_unix_ms` 完全一致；Kilo 在工具执行前后变化的动态环境文本不会
  被误判为新用户轮次。
- 最终响应为 `KILO_OAI_WS_A4_S4_TOOL_OK`，工具闭环成功。
- 四次握手均为 HTTP/1.1 101；静态 Header、OAuth Beta、版本、压缩提议与
  Codex CLI `0.145.0` 官方样本一致。ClientHello 的 10 个密码套件和扩展集合相同，
  ALPN 均为空。

Kilo 的两条 HTTP 路径补测 S1/S4，WebSocket 路径完整执行 S1/S2/S4；HTTP
连续会话生命周期另由官方客户端配对样本和自动化测试覆盖。A3 抓包曾发现工具续轮
因第三方完整历史中的动态环境文本而生成新 `turn_id`；A4 已改为继承当前 turn 身份。

修复后 `go test ./internal/service -count=1` 和 `go test ./... -count=1` 全部通过。
验收结束后 #94 已解绑临时代理，OAuth 官方画像自动生效，WS mode=`ctx_pool`；
临时代理已删除，CA 哈希恢复一致，MITM 已停止，主服务健康且重启次数为 0。

## 20. 内置自动生效与账号字段删除验收

最终镜像为 `sub2apiplus:official-egress-built-in-a2-20260725`，运行版本为
`0.1.164-1-n2`。本轮删除账号级开关与版本选择，改为按最终选中的平台和认证类型
自动生效：

- Anthropic OAuth/SetupToken 自动使用 Claude HTTP Profile。
- OpenAI OAuth 按实际出站协议自动使用 Codex HTTP 或 WebSocket Profile。
- API Key、其他平台和未定义画像的入口保持原行为。

迁移完成后，数据库中携带 `official_egress_enabled` 或
`official_egress_profile_version` 的账号数为 0。管理端账号页面不再显示或提交这些
字段，Plus 页面只展示“OAuth 官方客户端伪装已内置启用”的说明。

### 20.1 direct + ingress

最终 direct 目录为
`/root/oauth-capture/runs/t2-off-direct-20260725T114012Z`。Claude、Codex HTTP、
Codex WebSocket 的 S1 均成功，usage 分别命中 #50 一次、#94 HTTP 一次和 #94
WebSocket 两次；Profile 日志在没有账号开关字段时自动命中三条路径。

pcap 由抓包容器内的 tshark 解析：

- Claude 为 17 个密码套件、ALPN `http/1.1`。
- Codex HTTP 为 30 个密码套件、空 ALPN。
- Codex WebSocket 为 10 个密码套件、空 ALPN，扩展顺序按握手随机化。

宿主机未安装 tshark，因此旧脚本输出的 `client_hello_count=0` 只是统计命令未执行，
不代表 pcap 没有 ClientHello；抓包容器解析得到 7 个 ClientHello。

### 20.2 MITM + ingress

最终完整回归时间戳为 `20260725T114144Z`：

- Claude：
  `/root/oauth-capture/runs/phase0-sub2api-claude-mitm-20260725T114144Z`，
  S1/S2/S4 为 5/5 HTTP 200；system 维持四块，`cache_control` 为三块，工具闭环成功。
- Codex HTTP：
  `/root/oauth-capture/runs/phase0-sub2api-codex-http-mitm-20260725T114144Z`，
  S1/S2/S4 为 5/5 HTTP/2 200；顶层工具被定型为 `additional_tools`，
  `tool_choice=auto`、`store=false`、`stream=true`。
- Codex WebSocket：
  `/root/oauth-capture/runs/phase0-sub2api-codex-ws-mitm-20260725T114144Z`，
  S1/S2/S4 为 6/6 握手 101、9/9 `response.create`；有效续轮保留
  `previous_response_id`，S4 工具闭环成功。

同一时间窗内 Profile 日志计数为 Claude HTTP 5、Codex HTTP 5、Codex WebSocket
4（按 WS 连接解析并冻结）。验收结束后 #50/#94 的代理与 fallback 均为空，临时代理
已删除，CA 哈希恢复一致，keeper 正常运行，主服务健康且重启次数为 0。

## 21. `0.1.164-5` 发布版三路径与 Kilo 回归

2026-07-26 在 Vircs 已发布镜像 `ghcr.io/itv3/sub2apiplus:0.1.164-5` 上执行当前版本复验。
Anthropic 固定使用 OAuth #50，OpenAI 按本轮要求改用 OAuth #90；两者均为 active，
测试结束后 `proxy_id` 为空。主服务健康，主服务与 keeper 重启次数均为 0。

### 21.1 OAuth 三路径 direct + MITM

direct 合并运行目录为
`/root/oauth-capture/runs/v01645-oauth-egress-direct-20260726T042447Z`：

- Claude HTTP、Codex HTTP、Codex WS 的 S1/S2/S4 共 9 组摘要均为 `valid=true`。
- usage 分别命中 #50 的 Claude HTTP 5 次、#90 的 Codex HTTP 5 次和 #90 的 Codex WS 9 次。
- Claude direct 为 17 个密码套件、ALPN `http/1.1`；Codex HTTP 为 30 个密码套件、
  空 ALPN；Codex WS 为 10 个密码套件、空 ALPN，均达到当前 Profile 目标。

MITM 时间戳为 `20260726T042647Z`，三条候选目录分别为：

- `/root/oauth-capture/runs/phase0-sub2api-claude-mitm-20260726T042647Z`；
- `/root/oauth-capture/runs/phase0-sub2api-codex-http-mitm-20260726T042647Z`；
- `/root/oauth-capture/runs/phase0-sub2api-codex-ws-mitm-20260726T042647Z`。

S1/S2/S4 的业务语义均成功：Claude HTTP 5/5、Codex HTTP 5/5；Codex WS 为
4 次握手、9 条 `response.create`，S4 工具结果续轮使用真实 `previous_response_id`
和 `call_id`。但是与官方基准
`/root/oauth-capture/runs/official-client/oauth/oauth-20260726T014021Z`
执行严格规范化对比后，三条路径的 `equal` 均为 `false`。脱敏对比产物位于
`/root/oauth-capture/runs/official-client/comparisons/v01645-oauth-egress-20260726T042647Z`：

| 路径 | 已对齐 | 未闭合差异 |
|---|---|---|
| Claude HTTP | 5/5 请求、HTTP/1.1、静态 Header 名称和值 | 去除动态值和文本长度影响后仍有一条 S4 续轮结构差异：官方以 `tool_use` 开始，候选含额外 `thinking` 块 |
| Codex HTTP | 5/5 请求、HTTP/2、主要静态 Header | 官方基准含脱敏 Cookie 而候选没有；另有一条请求的 input 长度为官方 8、候选 7 |
| Codex WS | 4 次握手和 9 条客户端帧、静态握手画像 | 动态握手键属于预期差异，但仍有 5 个未匹配帧形状；候选部分帧缺少官方 `internal_chat_message_metadata_passthrough.turn_id` 等结构 |

因此本轮只能判定“业务场景与 direct 传输通过”，不能判定“应用层与官方样本完全一致”。

### 21.2 Kilo 六种协议转换与路由组合

先行自制请求脚本目录为
`/root/oauth-capture/runs/kilo-v01645-six-http-20260726T043627Z`。该脚本构造的两条
转 Anthropic 工具结果续轮返回 400，但其消息结构不是 Kilo 实际发送结构，因此不再作为
Kilo 回归结论，只保留为额外 API 兼容性观察。

随后直接操作本机真实 Kilo 界面，逐一选择用户配置的三种协议和六个模型组合。每组在
独立新会话内执行：S1 不调用工具并返回唯一标记；S4 明确显示“读取 README.md”工具，
授权一次只读访问后返回唯一标记和第一行 `# Sub2API Plus`。六组均通过：

| Kilo 选择 | 实际路由 | OAuth 账号 | S1/S4 | usage |
|---|---|---:|---|---:|
| OpenAI Responses / `claude-sonnet-5` | `/v1/responses → /v1/messages` | #50 | 通过 | 3 |
| OpenAI Responses / `gpt-5.6-luna` | `/v1/responses → /v1/responses`，WS | #90 | 通过 | 3 |
| Anthropic / `claude-sonnet-5` | `/v1/messages → /v1/messages` | #50 | 通过 | 3 |
| Anthropic / `gpt-5.6-luna` | `/v1/messages → /v1/responses` | #90 | 通过 | 3 |
| OpenAI Compatible / `claude-sonnet-5` | `/v1/chat/completions → /v1/messages` | #50 | 通过 | 3 |
| OpenAI Compatible / `gpt-5.6-luna` | `/v1/chat/completions → /v1/responses` | #90 | 通过 | 3 |

Vircs usage 时间窗为 2026-07-26 12:48:29–12:53:37（Asia/Shanghai）。每组的 3 条
记录分别对应 S1、S4 工具请求和 S4 工具结果续轮，均为流式请求；OpenAI Responses →
OpenAI 的 `openai_ws_mode=true`，其余五组为 HTTP。该证据覆盖真实 Kilo 的协议转换、
账号路由和工具闭环。

Kilo OpenAI Responses WebSocket 的修正后运行目录为
`/root/oauth-capture/runs/kilo-v01645-openai-ws-fixed-20260726T044227Z`，S1、S2 同连接
两轮续链、S4 `kilo_echo` 工具调用和结果续轮全部通过。第一次运行
`kilo-v01645-openai-ws-20260726T043947Z` 因测试脚本只检查
`response.completed.response.output` 而误判；实际流式文本和工具调用位于
`response.output_text.delta` 与 `response.output_item.done`，修正断言后复验通过，旧运行不计入验收。

本节复验未修改生产配置。结束时 #50/#90 均为 active 且无代理，未残留 MITM/tcpdump
进程或监听端口；主服务为 `healthy`、重启次数 0，keeper 运行且重启次数 0。

## 22. `0.1.164-6` OAuth 三路径差异闭合

2026-07-26 针对 §21 的三条未通过结论完成重新归因、代码修复、发布部署和真实流量复验。
发布标签为 `v0.1.164-6`，提交为 `e8b5c6051`，Vircs 主服务镜像为
`ghcr.io/itv3/sub2apiplus:0.1.164-6`，manifest digest 为
`sha256:0795f476b627e51b839a8c3aba350e85a185efe8e4e30e56d17df72673f7b7d9`。
主服务健康且重启次数为 0；keeper 本轮无代码变更，继续运行 `0.1.164-5`。

### 22.1 根因与修复边界

重新把候选 ingress、候选 egress 和官方独立运行三者配对后，上一版三条差异中只有一条
属于生产缺口：

1. Claude HTTP 的候选 ingress 与 egress 都带同一 `thinking`，官方样本来自另一次模型
   运行；这属于跨独立请求正文差异，不应由 Finalizer 删除。
2. Codex HTTP 的候选 ingress 与 egress 都为 7 项，官方独立运行为 8 项；官方 Cookie
   来自 CLI 的辅助 ChatGPT Cookie jar，也不应由 Sub2API 合成。
3. Codex WS 的正式业务帧中，`input` 项确实缺少官方逐项
   `internal_chat_message_metadata_passthrough.turn_id`。官方预热帧 `generate=false` 不带
   该字段；S2 第二轮的历史前缀使用上一轮 turn，当前后缀使用本轮 turn。

修复只作用于 OpenAI 官方 WS 帧 Finalizer：预热帧显式移除逐项 metadata；正式帧按
“历史前缀 / 当前后缀”分段写入 turn identity；工具结果续轮沿用当前 turn；已有冲突值
fail-closed。由 Kilo 完整历史派生的 WS 帧也复用同一规则。抓包比较工具新增三条路径契约，
同时保留 `raw_equal`、声明差异、未声明差异和 ingress→egress 语义守恒结果，旧的原始严格
比较入口没有删除。

### 22.2 MITM + ingress 契约复验

候选运行时间戳为 `20260726T055650Z`：

- Claude HTTP：
  `/root/oauth-capture/runs/phase0-sub2api-claude-mitm-20260726T055650Z`；
- Codex HTTP：
  `/root/oauth-capture/runs/phase0-sub2api-codex-http-mitm-20260726T055650Z`；
- Codex WebSocket：
  `/root/oauth-capture/runs/phase0-sub2api-codex-ws-mitm-20260726T055650Z`。

三条路径的 S1/S2/S4 均为 `valid=true`，usage 分别命中 #50 Claude HTTP 5 次、#90
Codex HTTP 5 次、#90 Codex WS 9 次。与官方基准
`/root/oauth-capture/runs/official-client/oauth/oauth-20260726T014021Z` 的脱敏结果保存在
`/root/oauth-capture/runs/official-client/comparisons/v01646-oauth-egress-20260726T055650Z`：

| 路径 | `raw_equal` | `contract_equal` | ingress→egress 语义 | 未声明差异 |
|---|---:|---:|---:|---:|
| Claude HTTP | false | true | true | 0 |
| Codex HTTP | false | true | true | 0 |
| Codex WS | false | true | true | 0 |

`raw_equal=false` 保留并展示跨独立运行正文、动态身份、Header 顺序及 Codex 运行时 Cookie
等真实差异；它不等于契约失败。Codex WS 的逐项 turn metadata 校验为 true：4 个预热帧
均无逐项 metadata，5 个业务帧的全部 input 项均有 metadata；S2 第二个业务帧的历史前缀
共享上一轮 turn，当前后缀使用本轮 turn；工具结果续轮使用当前 turn。

### 22.3 direct TLS 复验

direct 合并运行目录为
`/root/oauth-capture/runs/v01646-oauth-egress-direct-20260726T060439Z`。Claude HTTP、
Codex HTTP、Codex WS 的 S1/S2/S4 共 9 个场景全部有效，pcap 大小为 1,408,887 字节。
结构契约结果保存在上述 comparisons 目录的
`direct-contract-20260726T060439Z.json`：

- Claude HTTP 匹配官方 17-cipher、ALPN `http/1.1`，扩展顺序严格一致；
- Codex HTTP 匹配官方 30-cipher、空 ALPN，扩展顺序严格一致；
- Codex WS 共 4 个握手匹配官方 10-cipher、空 ALPN，扩展集合一致并允许官方随机顺序。

合并 pcap 还包含一个 13-cipher 的 OAuth 辅助连接，不属于三条业务 Transport Profile，
已在结构报告中单独列为 auxiliary，未拿它代替或污染业务画像结论。

### 22.4 真实 Kilo 受影响路径回归

直接操作本机真实 Kilo，选择 OpenAI Responses / `gpt-5.6-luna`，依次执行：

1. S1 新会话只返回 `KILO_OAI_WS_FIX_S1_OK`；
2. 同一会话 S2 续轮只返回 `KILO_OAI_WS_FIX_S2_OK`；
3. 独立 S4 只调用一次读取工具读取 `backend/go.mod`，随后返回
   `KILO_OAI_WS_FIX_S4_TOOL_OK`。

三项均通过。Vircs 在 2026-07-26 14:11:32–14:12:22（Asia/Shanghai）新增 4 条 usage，
全部命中 OAuth #90、模型 `gpt-5.6-luna` 且 `openai_ws_mode=true`，覆盖首轮、真实续轮、
工具请求与工具结果续轮。

复验结束后 #50/#90 的代理均为空，临时 MITM 代理已删除，CA 哈希和 keeper 状态已恢复，
direct/ingress 抓包进程均正常停止。本节执行当时 AnyRouter 账号仍不可用，因此当时标记为
待执行；后续已在 §23 完成，这不改变本节官方平台边界的通过结论。

### 22.5 Vircs 直接编译复核

当前完整工作区已同步到 Vircs `/root/sub2apiplus-build/v01646-final-20260726T062500Z`，
并在服务器侧直接完成以下验证，不使用 GitHub 编译：

1. Go `1.26.5` 环境执行 `go test ./...`，全部通过；
2. Docker 多阶段构建完成前端生产构建和后端 `-tags embed` 编译；
3. 隔离镜像 `sub2apiplus-vircs-test:v01646-final` 的镜像 ID 为
   `sha256:d9ac65dc1ce45af5ab145a1f37d075863dae950a5e968591d9b02b2e8c12b981`；
4. 镜像内 `/app/sub2api --version` 返回 `0.1.164-6-vircs-test`，提交标识为
   `worktree-20260726T062500Z`，二进制和资源目录布局检查通过。

该隔离镜像只用于验证当前工作区，没有替换生产容器。生产服务继续运行已完成本节
MITM、direct 和 Kilo 真实验收的官方发布镜像 `0.1.164-6`。

## 23. API Key active 画像 AnyRouter A/B 与 1M beta 顺序闭合

2026-07-26 使用 Claude Code `2.1.220`、Codex CLI `0.145.0` 和当前 API Key mimic，完成
“官方 CLI 直连 AnyRouter”与“标准 API 入站 → Sub2API → AnyRouter”的 HTTP A/B。账号与
模型固定为 Anthropic #15 / `claude-opus-4-8`、OpenAI #97 / `gpt-5.6-sol`；测试前确认
同一 Composite Group 中没有其他可调度账号接管这两个模型。API Key Codex WS Profile
仍为 inactive，因此本节不包含 WS。

私有原始运行目录：

- 官方 CLI 功能样本：
  `/root/oauth-capture/runs/anyrouter-ab-official-cli-20260726T071126Z`；
- HTTP MITM 与 direct TLS：
  `/root/oauth-capture/runs/anyrouter-ab-wire-20260726T072501Z`；
- 脱敏综合结论：
  `analysis/apikey-anyrouter-ab-contract.json`。

### 23.1 功能结果

1. Claude Code 首次不带 1M beta 的直连请求被 AnyRouter 明确要求启用 1M；显式传入
   `context-1m-2025-08-07` 并保留正常工具后返回精确 `S1_OK`。管理端 #15 账号测试和
   Sub2API 候选 MITM 样本均取得过 HTTP 200。
2. Codex CLI 直连和 Sub2API 候选均进入 AnyRouter 的 `gpt-5.6-sol` 负载上限分支；这是
   用户预先确认的正常结果。30 次官方 CLI 重试与 1 次候选请求均未出现
   `client_restricted`。
3. 后续 direct TLS 重复请求中，Anthropic 碰到瞬时 520、OpenAI 继续返回负载错误；这些
   响应不改变已经捕获的 ClientHello、HTTP 身份契约和先前成功样本。

### 23.2 MITM 应用层对比

| 项目 | Claude HTTP | Codex HTTP |
|---|---|---|
| 路由 | `POST /v1/messages?beta=true`、HTTP/1.1 一致 | `POST /v1/responses`、HTTP/2 一致 |
| 静态身份 | Claude Code UA、Stainless、`x-app`、API Key 认证一致 | Codex UA、`originator`、Responses Lite、beta features、Bearer 认证一致 |
| Body 固定契约 | model、stream、三段 system 及前两段固定文本一致；1M beta 自动补齐 | model、store、stream、parallel tools、include、client metadata 字段集合一致 |
| 声明差异 | 独立对话、工具 Schema、max tokens、thinking/effort 和运行上下文 | 独立对话、工具/开发者上下文、动态身份和官方 CLI 重试次数 |

这不是逐字节复制：第三方客户端输入的对话和工具语义必须保留，不能为了伪装强行替换成
Claude/Codex 本次 CLI 运行的完整上下文。验收只要求路由、认证、静态身份、必要 Body
外层字段、语义守恒和传输画像成立。

初次候选 Claude 请求已正确包含 `context-1m-2025-08-07`，但它被简单追加在
`effort-2025-11-24` 之后；官方 Claude Code `2.1.220 --betas context-1m-2025-08-07`
实抓顺序相反。本轮把动态 1M beta 插入到 effort 之前，并增加顺序回归测试。修复后再次
MITM：1M beta 存在、无重复、完整 beta 顺序和全部静态 Header 与官方样本一致。

### 23.3 direct TLS 对比

使用同一规范化器过滤目标 SNI `anyrouter.top`，去除目标 IP、帧号、重连次数并归一化
GREASE 后结果如下：

| 路径 | 官方观察数 | 候选观察数 | 唯一画像数 | 结果 |
|---|---:|---:|---:|---|
| Claude HTTP | 2 | 1 | 1 / 1 | `equal=true` |
| Codex HTTP | 6 | 1 | 1 / 1 | `equal=true` |

比较结果分别保存在 `analysis/claude-tls-diff.json` 和
`analysis/codex-tls-diff.json`。MITM 使用代理 TLS，不能替代本节 direct pcap；direct pcap
又无法读取 Header/Body，二者共同构成证据。

### 23.4 Vircs 编译、部署与恢复

修复工作区同步到
`/root/sub2apiplus-build/v01647-apikey-anyrouter-20260726T155000Z`，未通过 GitHub 编译。
Vircs 的全量 Go 测试除只读源码挂载阻止测试创建 `.entc` 外全部一次通过；改为可写挂载后
该失败包通过。Docker 多阶段构建成功，初次验证镜像
`sub2apiplus-vircs-test:v01647-apikey-anyrouter` 的镜像 ID 为
`sha256:c9fae693b3fa92c7d4545b83d72742b346cc056f0eb2fda7fbab05d736788261`，二进制版本为
`0.1.164-7-vircs-test`。

测试镜像替换主服务后状态为 healthy、重启次数 0。测试期间只临时把 #15 指向 MITM proxy；
结束后 #15/#97 的 `proxy_id` 均恢复为空，临时 MITM 进程和 CA 已清理，keeper、PostgreSQL、
Redis、`.env` 和数据卷均未修改。API Key 精确值扫描未在文本产物中发现泄露。

代码与文档提交为 `4098fad53`。提交后使用该提交号重新构建的最终运行镜像为
`sub2apiplus-vircs-test:v01647-anyrouter-final`，镜像 ID
`sha256:2f744e51229d9331a0169c8022ac204a1ca819448adc01c597a77f7115bbd5a5`；容器内版本为
`0.1.164-7-vircs-test / 4098fad53`。最终主服务 healthy、重启次数 0，keeper 为 running、
重启次数 0；#15/#97 代理为空，临时 MITM 进程和 CA 均不存在。

## 24. P0–P2 修复后的 OAuth 基准与 Vircs 阶段构建

2026-07-26 在 Vircs 更新抓包工具后先执行
`oauth-oauth-p0p1p2-20260726T1050Z`。该 run 的 direct 用例完成，但 Codex HTTP MITM
只观察到 WebSocket GET，证明向 `model_catalog_json` 添加
`prefer_websockets=false` 不会改变 Codex CLI `0.145.0` 的传输选择；run 按失败证据保留，
清理状态为成功。

随后依据 Codex `0.145.0` 官方 `create_openai_provider` 定义修正 HTTP provider：使用独立
ID，保留 `name="OpenAI"`、官方 Base URL、OAuth 认证和 `version: 0.145.0`，仅将
`supports_websockets` 设为 `false`。`oauth-oauth-provider-smoke-20260726T1056Z` 的 MITM
S1 三路径烟测通过；最终 `oauth-oauth-p0p1p2-final-20260726T1057Z` 完成 18 个 case：
Claude HTTP、Codex HTTP、Codex WS × direct/MITM × S1/S2/S4。manifest 状态为
`complete`，抓包清理成功。Codex HTTP 应用层证据为
`POST /backend-api/codex/responses`、HTTP/2、`content-encoding: zstd` 和
`version: 0.145.0`。

本轮源码位于 Vircs
`/root/sub2apiplus-build/p0-p1-p2-20260726T103655Z`，阶段镜像为
`sub2apiplus:p0-p1-p2-20260726`，二进制版本为
`0.1.165-1-p0p1p2-20260726 / working-tree-p0p1p2`。该镜像只用于编译验收，没有替换
生产 `0.1.165-1` 容器。

## 25. P0–P2 最终构建、生产切换与完整抓包

§24 记录的是修复过程中的阶段构建，不代表最终状态。最终源码版本为 `0.1.165-2`，同步至
Vircs `/root/sub2apiplus-build/p0-p1-p2-final-20260726T140102Z` 并直接完成 Docker 多阶段
构建。运行镜像为
`sub2apiplus:p0-p1-p2-final-0.1.165-2-20260726T140102Z`，镜像 ID 为
`sha256:6c0e54f4e28476604aef489fe90ce3bdf50e9ea8d7b196da2ed93681499e4713`；镜像内二进制
报告 `0.1.165-2 / 5770cff47-p0p1p2-final`。

生产编排只替换主服务镜像，PostgreSQL、Redis、keeper、`.env` 与数据卷均未修改。切换后
主服务为 healthy、重启次数 0，根路径、`/health` 和真实 `/v1/models` 探测成功。旧镜像
`ghcr.io/itv3/sub2apiplus:0.1.165-1` 仍保留，编排回滚备份为
`/root/Docker/sub2apiplus/app/docker-compose.yml.before-p0-p1-p2-final-20260726T140102Z`。

### 25.1 官方基准与候选应用层

官方完整基准目录为
`/root/oauth-capture/runs/official-client/oauth/oauth-oauth-p0p2-zstd-final-20260726T1420Z`，
Claude HTTP、Codex HTTP、Codex WS 的 direct/MITM × S1/S2/S4 共 18 个 case 全部完成，
manifest 为 `complete`。候选 MITM + ingress 运行时间戳统一为 `20260726T142438Z`：Claude
HTTP 5 个请求、Codex HTTP 5 个请求、Codex WS 9 条 `response.create` 均业务有效。

最终报告目录为
`/root/oauth-capture/runs/official-client/comparisons/p0-p2-final-0.1.165-2-20260726T142438Z-v2`：

| 路径 | `raw_equal` | `contract_equal` | ingress→egress 语义 | 未声明差异 |
|---|---:|---:|---:|---:|
| Claude HTTP | false | true | true | 0 |
| Codex HTTP | false | true | true | 0 |
| Codex WS | false | true | true | 0 |

比较器会解析 zstd 请求体，覆盖 compact 与所有已知顶层参数，并严格校验 Cookie 生命周期、
thinking/non-thinking sampling 规则及 WS turn metadata。独立对话正文、动态身份、Header 顺序、
经 `Set-Cookie → 后续 Cookie` 证据闭合的冷 Cookie jar 和独立响应链属于声明差异；这些差异
仍保留在 raw 报告，不能掩盖候选 ingress→egress 的语义损失。

### 25.2 候选 direct TLS

候选主运行目录为
`/root/oauth-capture/runs/p0-p2-final-direct-0.1.165-2-20260726T143437Z`。连接池复用导致部分
S2/S4 没有产生新 ClientHello，因此保留原始证据，并在
`/root/oauth-capture/runs/p0-p2-final-direct-0.1.165-2-20260726T143915Z` 按单元清空连接池
补抓 Claude HTTP S2/S4 与 Codex HTTP S2/S4。最终映射结果为 9/9 `equal=true`：Claude
HTTP 为 17-cipher + `http/1.1`，Codex HTTP 为 30-cipher + 空 ALPN，Codex WS 为
10-cipher + 空 ALPN。WS pcap 中 30-cipher 的 `/models` 辅助连接单列计数，没有替代业务画像。

### 25.3 本地证据与完整性

本节原始阶段抓包已被当前规则证据替代并清理。Codex CLI 0.145.0 的保留证据及完整性边界
统一见 `docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md` 和 `docs/EVIDENCE_INDEX.md`。

## 26. 0.1.165-3 最终复核修复、生产切换与新证据

三方复核指出的数据保真、Lite 定型、turn 时间、能力加载、compact 身份、token TLS、
辅助身份和比较器盲区已在 `0.1.165-3` 修复。Vircs 源码目录为
`/root/sub2apiplus-build/p0-p2-review-fix-20260726T213104Z`，生产运行镜像为
`sub2apiplus:p0-p2-review-fix-0.1.165-3-20260726T213104Z`，镜像 ID
`sha256:d2fb43c0588a27c6ac4e3c3e6574cd1043e0deda5c7977c88501c518429722af`。
容器内二进制报告 `0.1.165-3 / 5770cff47d-p0p2-review-fix`。切换只改 compose 主服务
镜像；最终主服务 healthy、重启次数 0，#50/#90 代理为空，临时代理、CA、抓包进程和端口
均已恢复。

本轮 OpenAI 官方标准基准为
`oauth-review-fix-codex-20260726T214417Z`，非 Lite `gpt-5.4` S2 基准为
`oauth-review-fix-nonlite-s2-20260726T221530Z`。候选标准 direct 为
`p0-p2-review-fix-direct-openai-0.1.165-3-20260726T214834Z`，MITM HTTP/WS 为
`p0-p2-review-fix-mitm-openai-0.1.165-3-*-20260726T215027Z`；非 Lite 候选为时间戳
`20260726T221002Z/20260726T221051Z`。最终报告：

`/root/oauth-capture/runs/official-client/comparisons/review-fix-0.1.165-3-20260726T2217Z-v5`

Codex HTTP、Codex WS、非 Lite及三类 direct TLS 均为 `equal=true`；应用层
`candidate_semantic_preserved=true`、`undeclared_differences=0`。官方与候选 wire 均没有
观察到 `x-codex-turn-state`，比较器按“官方实际下发才要求回放”的条件生命周期判定通过。
跨独立 CLI 运行的工具目录可以不同，但候选同次 ingress→egress 的 `tools` 仍严格比较。

Claude Code 2.1.220 完整基准继续使用同日成功的
`oauth-oauth-p0p2-zstd-final-20260726T1420Z`。#50 当前被 Anthropic 上游返回
`400 organization disabled`，官方 CLI 和 Sub2API 均复现；当前镜像的实际 Anthropic 出站
保存在 `phase0-sub2api-claude-mitm-20260726T222040Z`。虽然响应失败，应用层请求契约与
direct TLS 均 `equal=true`、未声明差异 0。该结果只能验收画像，不能写成业务响应成功。

Codex `thread/compact/start` 的真实 0.145.0 wire 使用带 `compaction_trigger` 的普通
`/responses`，以第二个 `turn/completed` 完成，并不发送 `/responses/compact`。该控制面
证据被单列，未冒充 compact 端点官方基准；compact 端点契约由专项代码测试覆盖。

本节原始阶段抓包已被当前规则证据替代并清理。当前范围、测试、保留证据和未覆盖边界统一
见 `docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md` 和 `docs/EVIDENCE_INDEX.md`。
