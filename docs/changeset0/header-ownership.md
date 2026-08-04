# 变更集 0：Header 所有权与 Override 优先级矩阵

> 对应方案 §5。本文件固化**当前**行为事实，再据此定义目标矩阵。
>
> 标注约定：【已验证】= 已在本轮按生产调用链直接读码确认；【待补证】= 需要后续
> 抓包或官方行为证据，不能作为当前实现正确的依据。

## 一、一个决定矩阵形态的事实

**OpenAI 侧，账号 Header Override 与 official egress finalize 当前永不相遇。**【已验证】

| 门槛 | 定义 | OpenAI 生效条件 |
|---|---|---|
| Header Override | `IsHeaderOverrideEligible` [account_header_override.go](../../backend/internal/service/account_header_override.go#L116) | 仅 `AccountTypeAPIKey` |
| official egress | `usesBuiltInOfficialEgressProfile` [official_egress_profile.go](../../backend/internal/service/official_egress_profile.go#L797) | 仅 `AccountTypeOAuth` |

两个集合互斥。因此方案 §5 的矩阵中"Release identity 由画像最终决定"与"Account extensions 可覆盖"两行，
在当前 OpenAI 代码里**从未同时作用于同一请求**。

**这对迁移的含义**：统一 Executor 后，两条规则会第一次在同一条 Plan 上相遇。届时的行为不是
"保持现状"，而是**新行为**，必须显式设计并测试，不能假设"照搬现有逻辑即可"。

Anthropic 侧不同：official egress 覆盖 `AccountTypeOAuth` 与 `AccountTypeSetupToken`，
Header Override 同样仅 `AccountTypeAPIKey`，也互斥。

## 二、当前 Header 写入层次

一条 OpenAI OAuth 主链请求的 header 依次经过：

| 阶段 | 代表实现 | 动作 |
|---|---|---|
| 1. 凭据 | `buildOpenAIAuthenticationHeaders` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1208)【已验证】 | 写入 Bearer 或 Agent Assertion |
| 2. 账号信息 | `resolveAndSetOpenAIChatGPTAccountHeaders` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1222)【已验证】 | OAuth 写入 `chatgpt-account-id`、`x-openai-fedramp` |
| 3. 入站透传 | `openaiAllowedHeaders` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1228)【已验证】 | 按白名单复制入站头 |
| 4. OAuth 端点语义 | OAuth 分支 [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1237)【已验证】 | 设置 originator/Accept，并整理旧会话头 |
| 5. UA 与 mimic | 自定义 UA、`APIKeyCodexMimic`、`ForceCodexCLI` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1271)【已验证】 | 依次应用账号与身份模式要求 |
| 6. 浏览器 UA 兜底 | `overrideBrowserUserAgent` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1297)【已验证】 | 仅 OAuth 且当前 UA 为浏览器时替换 |
| 7. 身份配对收口 | `enforceCodexIdentityHeaders` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1301)【已验证】 | originator 与 UA 首段配套 |
| 8. 报文框架 | Content-Type 兜底 [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1305)【已验证】 | 缺失时写入 `application/json` |
| 9. 账号覆写 | `ApplyHeaderOverrides*` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1312)【已验证】 | OAuth 下为 no-op |
| 10. egress 绑定 | `attachOfficialEgressHTTPContext` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1317)【已验证】 | 注入 egressContext |
| 11. 语义定型 | `finalizeOpenAIOfficialEgressHTTPRequest` [openai_gateway_forward.go](../../backend/internal/service/openai_gateway_forward.go#L1321)【已验证】 | 闭集裁剪 + 槽位重建 |
| 12. wire 适配 | `lowercaseHeaderRoundTripper` [lowercase_headers.go](../../backend/internal/pkg/tlsfingerprint/lowercase_headers.go#L46)【已验证】 | 按画像执行 H1 header casing |

`applyOpenAICodexAuxiliaryHeaders` 与 `ensureCodexIdentityHeaders` 只出现在 models、usage、
PAT、count-tokens、messages 等辅助链，**不在上述主 `/responses` 构造函数中**。旧版表格
把辅助链函数列为主链第二阶段，现已按真实调用顺序纠正。

阶段 12 位于 transport adapter 层，与语义定型分层——这一分层是对的，后续 adapter
应继承它，不要把 wire casing 上移到业务 Plan 或语义 compiler。

最终 Header 约束有三层【已验证】，迁移时必须全部纳入 compiler/adapter 的对应层：

1. `officialCodex0145FinalizeEndpointHeaders`（端点级闭集，删除 `endpoint.Headers` 之外的全部 header）
2. `officialCodex0145ApplyHeaderContract`（槽位校验后清空整个 map 再写回）
3. `stripOfficialEgressInboundHostHeaders`（删除 `accept-language`、`sec-fetch-mode`、`x-stainless-helper-method`）

## 三、目标所有权矩阵

`SinkID` 与 `IdentityMode` 共同决定每一类 header 的最终所有者。

| Header 类别 | 所有者 | `CodexOAuthStrict` | `CodexAPIKeyMimic` | `OfficialClientProxy` | `GenericOpenAI` |
|---|---|---|---|---|---|
| Transport（`host`、`content-length`、`connection`、`upgrade`、`sec-websocket-*`） | 系统 | 传输层生成，任何层不得预置 | 同左 | 同左 | 同左 |
| Auth（`authorization`、`x-api-key`） | 系统 | 画像+凭据 | 同左 | 同左 | 同左 |
| Body framing（`content-type`、`content-encoding`） | 系统 | 画像 | 画像 | 画像 | 透传 |
| Release identity（`user-agent`、`originator`、`version`、`x-codex-*`） | 画像 | 画像最终决定 | 画像最终决定 | 画像最终决定 | 不适用 |
| Account extensions | 账号 | **不适用**（OAuth 不 eligible） | 仅非保护字段 | 仅非保护字段 | 全部允许 |
| Endpoint closed set | 画像 | 删除闭集外全部字段 | 不裁剪 | 不裁剪 | 不裁剪 |
| Ingress headers | 入站 | 仅白名单且经闭集裁剪 | 白名单 | 白名单 | 白名单 |

保护名单沿用 `apiKeyMimicHeaderOverrideProtectedNames`（28 条）【已验证】，黑名单沿用
`headerOverrideBlockedNames`（30 条）【已验证】。

## 四、迁移必须保留的产品语义

1. OpenAI OAuth 的账号 Header Override 为 no-op —— **由 eligibility 保证，不是由保护名单保证**。
   迁移后若把 override 统一建模为 Plan 输入，必须在 Plan 组装阶段就对 OAuth 账号产出空 override，
   而不是依赖下游保护名单过滤。两者语义不同：前者是"没有输入"，后者是"输入被忽略"，
   在日志与调试上表现完全不同。
2. API Key Codex mimic 下，账号不能覆盖 `user-agent`、`originator`、`version`、`x-codex-*`。
3. API Key mimic 允许保留非保护的自定义 header，迁移不得静默删除。
4. `authorization`、`content-type`、`host`、长度与连接控制字段始终由系统决定。

## 五、本次发现的缺陷（迁移时一并修复）

### 5.1 API Key capability probe 的 mimic 缺陷不成立【已验证并纠正】

旧版分析只读了 `buildOpenAIResponsesProbeRequest`：这个 helper 的确能构造
`mimicProfile.Enabled=true` 的请求；但生产调用者
`ProbeOpenAIAPIKeyResponsesSupport` 在账号启用 mimic 时会**提前返回**，根本不会调用
helper、`ApplyHeaderOverrides` 或发送请求。

因此“mimic 探针使用无保护 override，而真实转发使用受保护 override”不可达，不是生产
缺陷。真实可达分支只处理非 mimic API Key，请求账号 baseURL 的 `/v1/responses`；它是
通用兼容能力探测，不属于 Codex OAuth 官方端点闭集，已从 ReleaseBinding 改为
out-of-scope。

保留的工程约束是：若以后复用该 helper 发送 mimic 请求，必须在新调用点显式使用受保护
HeaderPolicy，并新增调用链测试；不能把 helper 的单测可达性当成生产可达性。

### 5.2 `ForOfficialClientProxy` 与 `ForAPIKeyMimic` 实现完全相同【已验证】

[account_header_override.go](../../backend/internal/service/account_header_override.go#L216) 的两个入口传入同一个保护 map，
两个函数逐字节等价，差异仅在命名与注释意图。

处置：目标架构下二者应统一为 `IdentityMode` 的两个取值，由同一套 `HeaderPolicy` 驱动，
而不是保留两个同实现的函数。若未来确需分化，也应先有真实差异再拆。

### 5.3 保护名单与黑名单有 6 条重叠【已验证】

`session_id`、`conversation_id`、`x-codex-turn-state`、`x-codex-turn-metadata`、
`x-client-request-id`、`x-claude-code-session-id` 同时出现在两张表里。黑名单在配置保存阶段
就拒绝了这些名字，保护名单里的对应条目永远不会被触发。

处置：无害冗余，但会误导读者以为"保护名单撤掉就能被覆写"。统一 `HeaderPolicy` 时清理。

### 5.4 注释与机制不符【已验证】

[openai_alpha_search.go](../../backend/internal/service/openai_alpha_search.go#L503) 的注释称
`account_header_override.go` 有"覆写白名单"。实际只有黑名单，`session-id`/`thread-id`
可被覆写是因为**不在黑名单**（黑名单只收了下划线写法 `session_id`）。

结论（这两个头需要防线）正确，机制描述错误。迁移时按实际机制实现，不要照注释设计。

## 六、已确认边界与后续设计项

- PAT 与 Agent Identity 均为 `AccountTypeOAuth`，不满足 OpenAI Header Override 的
  API Key eligibility；它们的当前问题是普通 Go TLS/身份举证，而不是账号 override。
- 5 套入站白名单（Anthropic、OpenAI、passthrough、CC raw、keeper）【已验证】在目标架构下
  是收敛为一套按 persona 参数化的白名单，还是保留分立。倾向前者，但需先确认 5 套之间的
  差异是有意为之还是历史遗留；该选择属于后续 Plan/HeaderPolicy 实现，不阻塞变更集 0。
