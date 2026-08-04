# 变更集 0：persona 与 route 分类表

> 对应方案 §1。这是 `OfficialRouteCatalog` 的数据源。
>
> 解析键为 `method + host + path + purpose` 四元组。**四个维度缺一不可**：
> - 同一 host 上有多个 persona（chatgpt.com 同时承载 codex-cli 与 chatgpt-web）；
> - 同一 path 上有多个 method+transport（`/backend-api/codex/responses` 的 POST 是 HTTP、GET 是 WS）；
> - 同一 route 上有多个 purpose（`/responses` 承载主推理、管理测试、用量探针、PAT fallback）。

## 数据来源

端点闭集由版本画像文件**结构化提取**，逐条核对 `Method`、`TransportID`、`Host`、
`Upgrade`、`HostFromResponse` 字段；业务 purpose、SinkID 和 backend 来自
`sink-baseline.json`，不能从画像端点 ID 推导。

前一版本用 grep 抓路径字符串、未解析结构体字段，产生 4 处会导致误拦截的错误
（WS 的 method 写成 POST、realtime/calls 归成 WS、漏 sideband WS、漏 blob 上传）。
凡后续变更此表，必须重新结构化提取，不得手工增删。

## 一、persona 与状态定义

**persona 与"是否已登记"是两个正交维度**，不能混为一谈：

| 概念 | 含义 |
|---|---|
| `codex-cli` / `chatgpt-web` | 已完成身份举证的 persona |
| `unclassified` | **已登记 route**，但 persona 举证未完成。Guard 决策走第 2/3 档（sink 状态），不走 unknown route 分支 |
| 未登记 route | **不在本表内**的官方 host route。Guard 决策走第 1 档 `UnknownRoutePolicy` |

前一版本把 `unclassified` 同时描述成"已登记 persona"和"未知 route"，与 Guard 的三级
决策顺序冲突。现按上表区分：`unclassified` 是 route 表内的一行，有 SinkCatalog 条目、
有 EnforcementState；未登记 route 根本不在表内。

## 二、`codex-cli` route 闭集（16 条，画像权威）

| Method | Host | Path | Transport | 备注 |
|---|---|---|---|---|
| POST | chatgpt.com | `/backend-api/codex/responses` | HTTP | 主推理 |
| **GET** | chatgpt.com | `/backend-api/codex/responses` | **WebSocket** | `Upgrade: websocket`；**与上一行同 path 不同 method/transport** |
| POST | chatgpt.com | `/backend-api/codex/responses/compact` | HTTP | 上下文压缩 |
| GET | chatgpt.com | `/backend-api/codex/models` | HTTP | 模型清单 |
| POST | chatgpt.com | `/backend-api/codex/alpha/search` | HTTP | 搜索 |
| POST | chatgpt.com | `/backend-api/codex/images/generations` | HTTP | 图片生成 |
| POST | chatgpt.com | `/backend-api/codex/images/edits` | HTTP | 图片编辑 |
| POST | chatgpt.com | `/backend-api/codex/realtime/calls` | **HTTP** | `TransportID: HTTPDefault`，**不是 WebSocket** |
| **GET** | **api.openai.com** | **`/v1/realtime`** | **WebSocket** | realtime sideband；**唯一在 api.openai.com 上的 codex-cli route** |
| POST | chatgpt.com | `/backend-api/files` | HTTP | 文件上传登记 |
| **PUT** | ***.oaiusercontent.com** | **`{server_returned_path}`** | HTTP | blob 上传；**`HostFromResponse: true`，host 与 path 均由上游响应给出** |
| POST | chatgpt.com | `/backend-api/files/{file_id}/uploaded` | HTTP | 上传确认 |
| GET | chatgpt.com | `/backend-api/wham/usage` | HTTP | 用量查询 |
| GET | chatgpt.com | `/backend-api/wham/rate-limit-reset-credits` | HTTP | 重置额度查询 |
| POST | chatgpt.com | `/backend-api/wham/rate-limit-reset-credits/consume` | HTTP | 重置额度消费 |
| POST | auth.openai.com | `/oauth/token` | HTTP（ReqProfile backend） | token 刷新；authorization-code exchange 共用 route，但当前只有传输/身份证据，没有 refresh 之外的端点画像 |

### 两条对 Guard 设计有直接影响的特例

**blob 上传的 host 是动态的。** `HostFromResponse: true` 意味着目标 host 由前一步
`/backend-api/files` 的响应给出，静态扫描解析不到，Guard 也无法用固定 host 闭集校验。
它必须按 route 模板（`PUT *.oaiusercontent.com`）匹配，并在运行时校验 host 后缀。
这是方案 §9 检查项 4「动态 URL 是否同时具有运行时 Guard」的具体落点。

**persona 与发送栈是多对多。** `codex-cli` 同时使用 HTTPUpstream（主链）、
ReqProfile（OAuth refresh）与 WebSocket（responses WS、realtime sideband）三种 backend。
身份不能由"用了哪个库"推断。

**同 route 不代表同端点契约。** OAuth refresh 与 authorization-code exchange 都请求
`POST /oauth/token`，但 body discriminator 分别是 `refresh_token` 与
`authorization_code`。0.145 画像只定义了 refresh；exchange 当前只有 Codex TLS、
User-Agent 与 originator 证据，必须标记 `transport_only`，不能按 route 自动映射到
`oauth_refresh`。

## 三、purpose 维度：同一 route 的多个业务语义

`POST chatgpt.com /backend-api/codex/responses` 一条 route 当前承载 10 个 purpose。
它们的 persona 相同、route 相同，但 SinkID、BehaviorPolicy 与 EnforcementState 各不相同：

| SinkID | purpose | 业务调用点 | BehaviorKind | 迁移变更集 |
|---|---|---|---|---|
| `codex.responses.forward` | `user_request.responses` | `Forward` (forward.go:980) | user_request | 3 |
| `codex.responses.passthrough` | `user_request.passthrough` | `forwardOpenAIPassthrough` | user_request | 3 |
| `codex.responses.chat_completions` | `user_request.chat_completions` | `ForwardAsChatCompletions` | user_request | 3 |
| `codex.responses.anthropic_compat` | `user_request.anthropic_compat` | `ForwardAsAnthropic` | user_request | 3 |
| `codex.responses.ws_http_bridge` | `user_request.ws_http_fallback` | `proxyOpenAIWSHTTPBridgeTurn` | user_request | 3 |
| `codex.admin_test.responses` | `admin_test.responses` | `testOpenAIAccountConnection` | admin_test | 1B |
| `codex.admin_test.chat_completions` | `admin_test.chat_completions` | `testOpenAIChatCompletionsConnection` | admin_test | 1B |
| `codex.admin_test.keeper` | `admin_test.keeper_proxy` | `ProxyKeeperOpenAIAccount` | admin_test | 1B |
| `codex.usage.probe` | `usage_probe` | `probeOpenAICodexSnapshot` | background_probe | **1B（P0 旁路）** |
| `codex.alpha_search.pat_fallback` | `user_request.alpha_search_pat` | `forwardAlphaSearchViaResponsesWebSearch` | user_request | 1B（半旁路） |

`GET /backend-api/wham/usage` 同样有两个 purpose：Spark 影子的后台刷新，与管理端手动
查询——后者当前**完全无节流**。

**purpose 是 SinkID 的组成部分，不是注释。** 缺少它，用量探针与主推理会共用一个
route 判定，无法分别设置 EnforcementState 与 BehaviorPolicy。

## 四、`chatgpt-web` route 闭集

| Method | Host | Path | purpose |
|---|---|---|---|
| PATCH | chatgpt.com | `/backend-api/settings/account_user_setting` | 关闭训练授权 |
| GET | chatgpt.com | `/backend-api/accounts/check/v4-2023-04-27` | 账号信息 |
| GET | chatgpt.com | `/backend-api/subscriptions` | 订阅信息 |

path 前缀是 chatgpt.com 上区分两种 persona 的主判据：codex-cli 占用
`/backend-api/{codex,wham,files}`，chatgpt-web 占用
`/backend-api/{settings,accounts,subscriptions}`，互不重叠。

官方 Codex CLI 不请求这三个端点，强行迁入 `CodexEgressExecutor` 会制造出真实世界不
存在的形态。保持 Chrome 身份是正确设计。

## 五、`unclassified`：已登记但举证未完成

| Method | Host | Path | 当前实现 | 待举证 |
|---|---|---|---|---|
| POST | auth.openai.com | `/api/accounts/v1/agent/{runtime}/task/register` | **普通 Go TLS + 无身份头** | 官方等价行为 |
| GET | auth.openai.com | `/api/accounts/v1/user-auth-credential/whoami` | **普通 Go TLS + Codex 身份头** | 官方等价行为 |

无论最终归属哪个 persona，"Go 标准库 ClientHello 打官方 host"都必须在变更集 1B
修复。详见 [sink-inventory.md §3.3](sink-inventory.md)。

**已从待确认中移除**：`/.well-known/jwks.json` 与 `/oauth/authorize` 经类型感知扫描确认
**没有任何生产发送点**——它们只出现在常量与注释里。`/api/accounts` 前缀下也只有上表
两条。这三项此前是基于 grep 结果的猜测，不成立。

## 六、明确排除

`api.openai.com` 上除 `/v1/realtime` 外的路径属第三方 API 兼容层（crs_sync、antigravity、
batch_image、embeddings、gemini 兼容），不参与官方客户端身份伪装。

**但边界必须精确定义**：API-Key Codex mimic 会以 Codex 形态请求第三方 `/v1/responses`，
同样打 `api.openai.com`，属 `CodexAPIKeyMimic` 身份模式。它与通用兼容层同 host、
路径前缀也可能相同，**只能靠 purpose 区分**。

**判据**：由 `purpose` 区分，不由 host/path。`CodexAPIKeyMimic` 身份模式的 sink 其
purpose 以 `user_request.` 或 `admin_test.` 开头且 IdentityMode 为 `codex_apikey_mimic`；
通用兼容层的 purpose 不带 Codex 语义、IdentityMode 为 `generic_openai`。
两者在 SinkCatalog 中是不同的 SinkBinding，Guard 按 binding 匹配而非按 host 猜测。

`ProbeOpenAIAPIKeyResponsesSupport` 不是 CodexAPIKeyMimic sink：生产调用者对 mimic
账号提前返回，可达分支只探测非 mimic 账号 baseURL 的 `/v1/responses`。它曾被错误登记
为 chatgpt.com Codex route，现已按源码调用链改为 out-of-scope。

`api.anthropic.com` / `console.anthropic.com` 属 Anthropic 画像体系，persona 化另行规划。

## 七、死代码 route（不登记）

`GET chatgpt.com /backend-api/conversation/{id}/attachment/{id}/download` —— 生产无调用者，
网络分支无测试覆盖。建议删除，详见 [sink-inventory.md §3.4](sink-inventory.md)。
