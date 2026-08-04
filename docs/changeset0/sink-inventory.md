# 变更集 0：发送面 sink 台账

> 本文件是 `SinkCatalog` 的人工判定结果，对应方案 §9。原始扫描结果见
> `sink-baseline.json`（由 `backend/cmd/egressscan` 生成，CI 输入，勿手工编辑）。
>
> 判定原则：**宁可多登记，不可漏登记**。误登记的代价是多写一行台账，漏登记的代价是
> Guard 出现看不见的缺口。

## 一、方法论

发送点枚举由 `backend/cmd/egressscan` 自动产出，不接受人工梳理作为唯一来源。

理由是实证的：三份独立的架构评审都遗漏了 `openai_privacy_service.go` 这条直发
chatgpt.com 的路径——它既不含任何伪装符号（躲过既有台账门禁的 `SURFACE_RE`），用的
又是第三套 HTTP 库（req/v3），任何按符号或按已知文件列表的梳理都发现不了。

### 为什么必须是类型感知而非文本匹配

方案 §9 要求「按包路径、接收者类型和方法识别调用，不能只按 `Do`、`Send` 等 selector
名做文本匹配」。这不是精度偏好，是能力差别——先前的正则实现实测漏掉了 **13 条真实
发送点**：

| 形态 | 正则漏掉的原因 | 类型感知命中数 |
|---|---|---|
| `RoundTripper.RoundTrip` | 接口方法，无 `client.` 前缀 | 8 |
| `(*net.Dialer).DialContext` | 只认 `net.Dial(` | 3 |
| `tls.Dial` / `tls.DialWithDialer` | 被更宽的 `\bDial(` 抢先误分类为 WS | 2 |
| `http.DefaultClient.Do` | 接收者不是名为 `client` 的变量 | 见自测用例 |
| 结构体字段 `s.upstream.Do` | 同上 | 同上 |

反向同样重要：正则会把业务代码里任意名为 `Do`/`Dial` 的方法误报为网络发送。
自测用例中的两条负例（`(*job).Do`、`(*pool).Dial`）就是为此设置的。

### 机器基线

`sink-baseline.json` 记录 `bootstrap_commit`、稳定 `scan_candidate_id`（**不含行号**，由
包路径 + 函数 + 文件 + sink 种类 + 函数内序号构成）、完整 callee/receiver、AST 指纹、
terminal/facade/factory 分类、目标解析状态（literal/const/constructed/dynamic/unknown）。

`make check-egress-spec` 每次重新扫描并与基线 diff：新增、消失、指纹变化、目标 host
变化都会使 CI 失败。已实测：注入一个 `http.DefaultClient.Do` 后 `make` 退出码为 2，
移除后恢复 0。

## 二、共享 facade：为什么 SinkID 必须在业务调用点生成

实测 `openai_gateway_forward.go` **没有任何发送调用**。主链的实际链路是：

```
openai_gateway_forward.go:980  Forward()
  → doOpenAIHTTPUpstreamForRequest()      openai_upstream_http.go:152
    → doOpenAIHTTPUpstreamWithProfile()   openai_upstream_http.go:118
      → httpUpstream.DoWithTLS()          openai_upstream_http.go:133/143
      → httpUpstream.Do()                 openai_upstream_http.go:147/149
```

`doOpenAIHTTPUpstreamWithProfile` 有 **8 个直接调用表达式**；其中一个是二级 facade
`doOpenAIHTTPUpstreamForRequest`，它自身又有 **3 个调用点**。展开后共 **11 个业务调用点**：

| 调用者 | 位置 | 业务语义 |
|---|---|---|
| `forwardOpenAIPassthrough` | openai_gateway_passthrough.go:227 | 透传主链 |
| `doOpenAIHTTPUpstreamForRequest` | openai_upstream_http.go:156 | 二级 facade（Forward 等经此进入） |
| `testOpenAIAccountConnection` | account_test_service.go:759 | 管理端账号测试 |
| `testOpenAIChatCompletionsConnection` | account_test_service.go:964 | 管理端 chat completions 测试 |
| `testOpenAICompactConnection` | account_test_service.go:1094 | 管理端 compact 测试 |
| `ProxyKeeperOpenAIAccount` | account_test_service_keeper.go:96 | keeper 代发 |
| `ProbeOpenAIAPIKeyResponsesSupport` | openai_apikey_responses_probe.go:203 | 非 mimic API key 能力探测（out-of-scope） |
| `proxyOpenAIWSHTTPBridgeTurn` | openai_ws_http_bridge.go:269 | WS/HTTP 桥 |

再上一层还有 `Forward()`（forward.go:980）、`ForwardAsChatCompletions`、
`ForwardAsAnthropic`、`ProxyResponsesWebSocketFromClient`，以及 handler 层的
`scheduleOpenAIResponsesProbe`（admin/account_handler.go:1086）。

注意 `forward.go:242` **不是发送点**：它是 `return s.forwardOpenAIPassthrough(...)`，
把请求分派到透传分支。调用链回溯会把它列出来，但它的身份归属属于 passthrough。

**结论**：若 `SinkID` 在 facade 处生成，上述全部路径会拿到同一个 ID，Guard 无法区分
"用户主链"与"后台探针"，per-sink enforcement 与 per-sink 回滚同时失效。这是方案 §8
要求 SinkID 必须在业务调用点生成、随 Plan/context 穿过 facade 与重试链的实证依据，
不是理论洁癖。

`ProbeOpenAIAPIKeyResponsesSupport` 与 `scheduleOpenAIResponsesProbe` 在此前三份评审
中均未被提及，是本次扫描新增发现；前者经完整调用链复核后归入 out-of-scope。

## 三、sink 台账

`SinkID` 命名规则：`<业务域>.<出口语义>`，在业务调用点声明，不随 facade 变化。

### 3.1 codex-cli persona

| SinkID | 调用点 | 目标 | 客户端/TLS | 状态 | 迁移变更集 |
|---|---|---|---|---|---|
| `codex.responses.forward` | forward.go:980 | chatgpt.com `/backend-api/codex/responses` | httpUpstream.DoWithTLS + Codex 画像 | 已画像 | 3 |
| `codex.responses.passthrough` | passthrough.go:227 | 同上 | 同上 | 已画像 | 3 |
| `codex.responses.chat_completions` | chat_completions.go:318 | 同上 | 同上 | 已画像 | 3 |
| `codex.responses.anthropic_compat` | messages.go:374 | 同上 | 同上 | 已画像 | 3 |
| `codex.responses.ws` | WS dialer / RoundTripper | chatgpt.com `/backend-api/codex/responses`（WS） | WebSocket backend | 已画像 | 3 |
| `codex.responses.ws_v2_passthrough` | ws_forwarder_ingress.go:596 | 同上 | WebSocket backend | 已画像 | 3 |
| `codex.responses.ws_http_bridge` | ws_http_bridge.go:269 | chatgpt.com `/backend-api/codex/responses`（HTTP fallback） | httpUpstream | 已画像 | 3 |
| `codex.oauth.refresh` | repository/openai_oauth_service.go:116 | auth.openai.com `/oauth/token` | `createOpenAIReqClientWithProfile` + Codex TLS | **已画像，硬编码 active** | 2（Bundle）/ 3（入口） |
| `codex.oauth.exchange` | repository/openai_oauth_service.go:67 | auth.openai.com `/oauth/token` | ReqProfile + Codex TLS/User-Agent/originator | **仅 transport/身份有证据；不能冒充 refresh Body 契约** | 2（补端点证据）/ 3（入口） |
| `codex.alpha_search.direct` | openai_alpha_search.go | chatgpt.com | DoWithTLS + Codex TLS，fail-close 已就位 | 已画像 | 3 |
| `codex.alpha_search.pat_fallback` | openai_alpha_search.go:257 | chatgpt.com `/backend-api/codex/responses` | DoWithTLS 有 TLS，**Header/Body 手工构造，无 finalize** | **半旁路** | 1B |
| `codex.usage.probe` | account_usage_service.go:785 | chatgpt.com `/backend-api/codex/responses` | **`httppool.GetClient`，标准 net/http，无 uTLS** | **P0 旁路** | 1B |
| `codex.admin_test.responses` | account_test_service.go:759 | chatgpt.com | httpUpstream，Header 手工构造 | 半旁路 | 1B |
| `codex.admin_test.chat_completions` | account_test_service.go:964 | chatgpt.com | 同上 | 半旁路 | 1B |
| `codex.admin_test.compact` | account_test_service.go:1094 | chatgpt.com `/compact` | 同上 | 半旁路 | 1B |
| `codex.admin_test.keeper` | account_test_service_keeper.go:96 | chatgpt.com | 同上，经 `ApplyHeaderOverridesForOfficialClientProxy` | 半旁路 | 1B |
| `codex.quota.wham` | openai_quota_service.go | chatgpt.com 三个 WHAM 端点 | 画像已登记端点 | 已画像 | 3 |
| `codex.models.list` | openai_codex_models_service.go | chatgpt.com `/backend-api/codex/models` | 三分支：`httpUpstream.Do` / `DoWithTLS` / `httpclient.GetClient` | **含无 uTLS 分支** | 1B |
| `codex.files.register` | official_egress_codex_files.go | chatgpt.com `/backend-api/files` 与 uploaded 确认 | httpUpstream | 已画像 | 3 |
| `codex.files.blob_upload` | official_egress_codex_files.go | `*.oaiusercontent.com/{server_returned_path}` | httpUpstream，动态 host/path | 已画像 | 3 |
| `codex.realtime.calls` | openai_live.go | chatgpt.com `/backend-api/codex/realtime/calls` | httpUpstream | 已画像 | 3 |
| `codex.realtime.sideband` | openai_live.go | api.openai.com `/v1/realtime`（WS） | WebSocket backend | 已画像 | 3 |
| `codex.images.responses` | openai_images_responses.go | chatgpt.com | 已画像 | 已画像 | 3 |
| ~~`codex.billing.probe`~~ | upstream_billing_probe.go | 账号自定义 base_url 的 sub2api 私有端点 | — | **判为 out-of-scope**，非官方 host | — |

### 3.2 chatgpt-web/chrome persona

| SinkID | 调用点 | 目标 | 客户端/TLS | 状态 |
|---|---|---|---|---|
| `web.privacy.disable_training` | openai_privacy_service.go:54 | chatgpt.com `PATCH /backend-api/settings/account_user_setting` | `CreatePrivacyReqClient` → `Impersonate: true` | persona 归属正确，**wire 已证实有缺陷** |
| `web.privacy.account_info` | openai_privacy_service.go:120 | chatgpt.com `GET /backend-api/accounts/check/v4-2023-04-27` | 同上 | 同上 |
| `web.privacy.subscription` | openai_privacy_service.go:234 | chatgpt.com `GET /backend-api/subscriptions` | 同上 | 同上 |

三者的端点归属（浏览器端点）、Header 语义（`Origin`/`Referer`/`sec-fetch-*`）与 TLS
画像（Chrome impersonate）方向一致，**不得迁入 `CodexEgressExecutor`**：官方 Codex CLI
不会请求 settings/subscriptions 端点，强行统一反而制造出不存在的形态。

#### 但最终 wire 已验证为不正确

`Impersonate: true` 只是工厂层配置。本轮用本地 TLS server 捕获了真实 HTTP wire
（`backend/internal/repository/req_client_chrome_wire_test.go`），结果是：

`ImpersonateChrome()` 注入的是**导航请求（navigation）**的完整 header 组，而 privacy
service 只覆盖了 `sec-fetch-*` 三项，导航专属 header 原样留在 wire 上：

| Header | 实际值 | 真实 Chrome XHR | 判定 |
|---|---|---|---|
| `Sec-Fetch-User` | `?1` | **不携带** | ❌ 导航专属 |
| `Upgrade-Insecure-Requests` | `1` | **不携带** | ❌ 导航专属 |
| `Cache-Control` | `no-cache` | 默认不带 | ❌ 硬刷新语义 |
| `Pragma` | `no-cache` | 默认不带 | ❌ 同上 |
| **`Accept-Encoding`** | **`gzip`** | `gzip, deflate, br, zstd` | ❌ **Go 默认值** |
| `Sec-Fetch-Mode` | `cors` | `cors` | ✅ 覆盖生效 |
| `Sec-Fetch-Site` | `same-origin` | `same-origin` | ✅ |
| `Sec-Fetch-Dest` | `empty` | `empty` | ✅ |

`Accept-Encoding` 是三处里最易识别的一处：req/v3 的 `chromeHeaders` 常量里**根本没有它**，
于是 Go 标准库 Transport 自己补了 `gzip`。真实 Chrome 从不只 offer gzip，而这个 header
出现在**每一个请求**上。

**结论**：当前 wire 是「XHR 的 `sec-fetch-*` + 导航的 `Sec-Fetch-User`/
`Upgrade-Insecure-Requests`/`no-cache`」的混合形态。真实浏览器不会产生这种组合——
一个声明自己是 `cors`/`empty` 的请求同时携带 `Sec-Fetch-User: ?1`，在语义上自相矛盾。

另有一项可聚类特征：`Accept-Language` 固定为 `zh-CN,zh;q=0.9`，与账号或代理出口地域
无关。同一 IP 段下所有账号呈现同一语言偏好，是可用于关联的信号。

#### TLS 层：指纹版本过时

`ImpersonateChrome()` 固定使用 `utls.HelloChrome_120`——Chrome 120 发布于 2023 年底。
项目依赖的 utls **v1.8.2 实际提供到 `HelloChrome_133`**。

实测两者的 `supported_groups` 差异：

| 版本 | supported_groups |
|---|---|
| Chrome 120（当前） | GREASE, X25519, P-256, P-384 —— **无 post-quantum** |
| Chrome 133（库已支持） | GREASE, **X25519MLKEM768**, X25519, P-256, P-384 |

真实 Chrome 自 124 起默认启用 X25519MLKEM768。**一个在 2026 年不 offer PQ key share
的「Chrome」，仅凭 ClientHello 即可与真实浏览器区分**——这比 header 层的问题更根本，
因为它在任何 HTTP 内容之前就暴露了。

HTTP 层同样停留在 120：`sec-ch-ua` 声明 `"Chromium";v="120"`，User-Agent 是
`Chrome/120.0.0.0`。两层版本自洽，但整体是一个两年半前的浏览器。

#### 改造路径（变更集 1B）

`SetTLSFingerprint` 是 req/v3 的公开 API，在 `ImpersonateChrome()` 之后追加即可覆盖，
已实测生效。但**三件事必须一起改**，只改其一会制造新的不一致：

1. TLS 指纹 → `utls.HelloChrome_133`
2. 版本 header（`user-agent`、`sec-ch-ua`）→ 同步到 133
3. 删除导航专属 header，改为 XHR 形态

第 3 点的根因值得记录：req/v3 的 `chromeHeaders` 常量本身就是为**浏览器打开页面**
设计的——`sec-fetch-mode: navigate`、`sec-fetch-dest: document`、`sec-fetch-user: ?1`、
`accept: text/html,...`。privacy service 用它发 API 调用，属于用错了模板。
另有 `sec-ch-ua-platform: "macOS"` 与 `accept-language: "zh-CN,zh;q=0.9"` 均为硬编码，
与部署环境和账号地域无关。

上述缺陷已固化为 characterization test（`TestChromePersonaKnownWireDefects`、
`TestChromePersonaTLSVersionIsStale`），断言当前的错误值——**修复时测试会主动失败**，
提醒同步更新本表。

### 3.3 待分类（前置举证未完成）—— 且均为 P0 旁路

| SinkID | 调用点 | 目标 | 客户端/TLS | Header | 待举证 |
|---|---|---|---|---|---|
| `unclassified.agent.task_register` | [openai_agent_identity.go](../../backend/internal/service/openai_agent_identity.go#L188) | auth.openai.com `/api/accounts/v1/agent/{runtime}/task/register` | **`httpclient.GetClient`，无 uTLS** | **仅 Content-Type + Accept，无 Codex 身份头** | 官方等价行为 |
| `unclassified.pat.whoami` | [openai_codex_pat_service.go](../../backend/internal/service/openai_codex_pat_service.go#L46) | auth.openai.com `/api/accounts/v1/user-auth-credential/whoami` | **`httpclient.GetClient`，无 uTLS** | 有完整 Codex 身份头 | 官方等价行为 |

这两条是本次扫描 + 行为调查的**重大增量**：此前三份架构评审都只发现了
`codex.usage.probe` 一条。

**表述必须收紧**：目前可确认的是「**至少 3 条直接使用 ordinary client（无 uTLS）的
sink**」，不能说成"全仓 TLS 旁路共 3 条"。管理端 Responses/compact 等路径虽经
`httpUpstream` facade 发送，但缺少 `OfficialEgressContext` 时同样可能落到普通 TLS——
它们的实际 TLS 形态取决于运行期 context，静态扫描判定不了。完整结论要等变更集 1A
的 observe 指标。

- Agent Identity 注册是最裸的一条：TLS 与 Header **两层都不是 Codex**。
- PAT whoami 更微妙：Header 声称 Codex CLI，TLS 是 Go 标准库，**主动声明了与传输层
  矛盾的身份**，比单纯"两层都不对"更容易被识别。

二者位于官方 host 不足以自动归入 `codex-cli`，但无论最终归属哪个 persona，
"Go 标准库 ClientHello 打官方 host"这一点都必须在变更集 1B 修复。

详见 [probe-behavior.md §1](probe-behavior.md)。

### 3.4 死代码中的 sink（建议删除）

| 位置 | 目标 | 判定 |
|---|---|---|
| `openai_images.go:1447` `fetchOpenAIImageDownloadURL` | chatgpt.com `/backend-api/conversation/{id}/attachment/{id}/download` | 死代码 |
| `openai_images.go:1483,1500` `downloadOpenAIImageBytes` | 同上 | 死代码 |

唯一入口 `resolveOpenAIImageBytes`（[openai_images.go](../../backend/internal/service/openai_images.go#L1300)）
在**生产代码中没有任何调用者**。

它有一个测试调用者 [openai_images_test.go](../../backend/internal/service/openai_images_test.go#L442)
（`TestResolveOpenAIImageBytes_PrefersInlineBase64`），但该用例传入 `client = nil` 且只走
base64 内联分支——**两个网络 sink 既无生产调用者，也无测试覆盖**。

处置建议：**变更集 1B 直接删除**，不要迁移。理由：

- 它带一个 `attempt < 8` 的重试循环直打 chatgpt.com；
- 客户端由参数注入，无法静态确定画像；
- 留着它等于留一个随时可能被接线的现成旁路。

删除时需连带处理该测试（保留 base64 归一化能力的话，把 `normalizeOpenAIImageBase64`
单独留下并改测它）。若日后确有下载需求，应按 Executor 契约重写。

人工梳理不会去检查死代码，这一条只有扫描器能发现。

### 3.5 判定为 out-of-scope

`api.openai.com` 命中中，`crs_sync_service.go`、`antigravity_*.go`、
`batch_image_provider_*.go`、`openai_embeddings.go`、`gemini_*.go` 属于第三方 API 兼容
与同步路径，不参与官方客户端身份伪装，**本方案不覆盖**。

但需注意：主业务链的 API-key Codex mimic 会以 Codex 形态请求第三方 `/v1/responses`，
那条路径属于 `CodexAPIKeyMimic` 身份模式，与此处的通用兼容层不同。两者都打
`api.openai.com`，仅凭 host 无法区分——这再次印证 route 必须按
`method + host + path + purpose` 四元组解析，不能只按 host。

`ProbeOpenAIAPIKeyResponsesSupport` 不属于 mimic：生产调用者对 mimic 账号提前返回，
可达分支只处理非 mimic 账号的自定义 baseURL，故同样归入 out-of-scope。

## 四、分类完成度与遗留

`sink-baseline.json` 的候选已全部完成分类。分类规则见
`backend/cmd/egressscan/classify.go`，按声明顺序匹配，未命中任何规则即扫描失败——
默认归入 out-of-scope 是被禁止的。

候选、persona、迁移归属、端点证据、责任人、构建矩阵和请求事实解析率均由同一基线
生成，见 [sink-stats.md](sink-stats.md)。禁止在本文件再复制这些数字；生成物由
`make check-egress-spec` 逐字节校验。

请求事实使用函数内数据流追踪；跨 helper 与运行期 URL 保持 dynamic/unknown。
method、host、path 任一单独变更都有反向变异用例证明会触发基线漂移。

先前列为「待确认」的四项已全部澄清：

- **`forward.go:242` 不是发送点**（已证伪，`forward.early` 条目已删除），
  它是 `return s.forwardOpenAIPassthrough(...)` 分派。
  `Forward` 只有 `:980` 一个发送点；本文档 §2 早前的「两个发送点」说法据此更正。
- **PAT 与 Agent Identity 账号的 `Account.Type` 均为 `AccountTypeOAuth`**——两者的判定
  函数都以 `IsOpenAIOAuth()` 为前置。因此它们的 header override 是 no-op，且**都属于
  official egress 的启用集合**。这与它们当前使用无 uTLS 客户端发送直接矛盾，是 1B 必修项。
- WebSocket Dial 清单已由类型感知扫描给出精确结果（`ws_coder_dial` 2 条），不再有
  置信度问题。
- `api.openai.com` 边界见 [persona-catalog.md](persona-catalog.md) §六。

chatgpt-web 的本地证据已补齐两层：repository 测试捕获 Chrome 工厂的 h2/H1 wire，
service 测试通过 `PrivacyClientFactory` 直接调用三个生产函数，确认真实 method、URL、
query 与请求级 Header。它们共同证实当前存在 Chrome 120 过时、导航 Header 混入 XHR、
Accept-Encoding 只有 gzip、两个 GET 缺 Fetch Metadata 等缺陷。

仍需外部抓包对照真实 Chrome 的 HTTP/2 SETTINGS、pseudo-header/HPACK 顺序；该证据用于
1B 修复验收，不阻塞 1A observe，也不能据此把当前 wire 记为正确。
