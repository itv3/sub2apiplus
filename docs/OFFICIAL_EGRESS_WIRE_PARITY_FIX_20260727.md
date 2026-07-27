# 官方出站 wire 一致性修复清单

日期：2026-07-27
状态：进行中
基线版本：`0.1.165-6`
目标版本：`0.1.165-7`

## 1. 来源与验证方式

本清单来自两位外部工程师的独立审计，加上本地逐条复核。每条都标注**复核结论**：

- **成立**：本地读码或实测确认，可修。
- **部分成立**：声称方向对但细节需修正，已在条目内写明。
- **已证伪**：声称不成立，记录在 §5 以免重复排查。

复核手段分三类：读 `local-analysis/sources/codex-cli-0.145` 官方源码、读本仓库实现、写临时测试实测出站 body。

### 一个贯穿性的验证限制

官方 OpenAI **直连**画像是空 ALPN（`newOpenAIOfficialEgressHTTPTLSProfile`），`ProfileNegotiatesH2` 判定为 false，走 HTTP/1.1；**代理**画像 ALPN 含 h2，走 HTTP/2。而 MITM 抓包必须经代理，因此：

> **凡是只在 HTTP/1.1 上可见的差异（header 名大小写、header 顺序），现有全部 MITM 证据都看不到**——h2 协议强制小写并使用 HPACK。这类问题只能靠读码定位，验证需要另建 h1 直连的观测手段。

## 2. 本轮修复范围

| 优先级 | 条目 | 本轮 |
|---|---|---|
| P0 | 1~4：header 大小写、compact 压缩、models 传输栈、未知字段泄漏 | **已修** |
| P2 | 9、11、12：入站判定口径、能力预热、passthrough 清单 | **已修** |
| P2 | 10：`strictIngressIdentity` 分叉 | 按既定决策保留不改 |
| P1 | 5~8：四条旁路端点无画像 | **本轮不做**，理由见下 |
| P3 | 13：WS turn-state 来源 | **本轮不做**，理由见下 |
| P3 | 14~16：h2 SETTINGS、h1 header 顺序、WS 握手头序 | 官方 h1 基线已补齐（§6.7）；14 被基线缺失阻塞、15 是独立工程，结论见 §6.8 |
| P3 | 17：WS 连接池规模 | 记录，不改 |

### 范围收窄的理由

P0 与 P2 共 7 条已完成并通过全量测试。P1 与 P3-13 在本轮实施过程中被推迟，原因是同类改动的
**测试基础设施耦合成本**在前几条已充分暴露：

- P0-1 若在 Finalizer 层改写 header 形态，会波及 162 处测试断言，最终改为在 transport 层
  统一收口才得以解耦；
- P0-3 把 models 请求接入 `httpUpstream` 后，暴露出「模型清单与业务请求共用同一测试替身」
  的问题，连带 5 个 passthrough 用例失败，需要给替身加按 URL 分流才修复。

P1 要同时改动四条独立链路的传输栈与 header 构造，P3-13 需要新增 WS 事件流解析并改变
turn-state 的来源语义，两者的波及面都大于上述任一条。在同一轮里连做会让回归风险与验收
成本失控，因此拆到下一轮单独实施与验收。它们的证据与方案已在 §3 完整记录，不需要重新排查。

## 3. 逐条清单

### P0-1 HTTP/1.1 header 名大小写

**复核结论**：成立。

`finalizeOfficialOpenAIHTTPHeaders` 全部使用 `header.Set`，Go 的 `textproto.CanonicalMIMEHeaderKey` 会把 `session-id` 规范化成 `Session-Id`、`originator` 成 `Originator`、`x-codex-turn-metadata` 成 `X-Codex-Turn-Metadata`。官方 hyper 的 `write_headers` 用 `name.as_str()` 输出全小写，且 `title_case_headers` 默认关闭。

决定性证据是项目自身的不对称：`official_egress_anthropic.go` 用 `setHeaderRaw` **6 次**绕过规范化，`official_egress_openai_http.go` 用 **0 次**。同一个问题在 Anthropic 侧已解决，OpenAI 侧遗漏。

**修复**：改用现成的 `setHeaderRaw`；WS 握手 finalize 同步处理。
**验收**：断言 `http.Header` 的 map key 保持原始小写形态。

### P0-2 compact 请求被 zstd 压缩

**复核结论**：成立。

官方 `RequestCompression` 的 `#[default]` 是 `None`（`http-client/src/request.rs`），compact 的 `execute_with` 只设置 timeout、不设压缩，因此官方 compact 发明文。Sub2API 的 `compressOfficialOpenAIHTTPRequest` 不接收 compact 标志、调用点也不判断，对所有 OAuth 请求一律 zstd。

**修复**：调用点加 `!isCompact` 判断。
**验收**：compact 出站无 `Content-Encoding`，普通 Responses 仍为 zstd。

### P0-3 models 请求使用标准 Go TLS 栈

**复核结论**：成立。

OAuth 路径走 `httpclient.GetClient`，该包无任何 `tlsfingerprint` 引用，即标准 Go ClientHello。manifest 有 5 分钟 TTL，过期后下一个业务请求触发刷新，因此同账号同 IP 上会周期性出现一个与官方画像不同的 ClientHello 打向 `chatgpt.com`。

**修复**：改走官方画像 transport。
**验收**：direct pcap 中 models 与 responses 的 ClientHello 一致。

### P0-4 未知顶层字段原样出站

**复核结论**：成立（实测）。

临时测试实测：第三方传入 `truncation`、`top_logprobs`、`background`、`max_tool_calls` 以及自造的 `totally_made_up_field`，**全部原样到达上游**。`official_egress_openai_json.go` 对未识别顶层字段按字典序追加输出，归一化只删已知字段清单。官方 `ResponsesApiRequest`（`codex-api/src/common.rs`）是固定 Rust 结构体，不可能携带这些字段。

**修复**：顶层改为白名单，清单外字段一律剔除。
**验收**：同一组探针字段全部不出现在出站 body。

### P1-5~8 旁路端点 OAuth 打官方域名但无画像

**复核结论**：全部成立。

画像闸门 `supportsOfficialEgressHTTPProfile` 对 OpenAI 只放行 `/v1/responses`、`/v1/responses/compact`、`/v1/chat/completions`、`/v1/messages` 四个入口，不在名单的**静默 fail-open**，TLS 画像、zstd、header 定型、body 定型、连接池隔离一次性全部失效。

| 入口 | 出站目标 | 可达性 | 额外问题 |
|---|---|---|---|
| `/v1/messages/count_tokens` | `api.openai.com/v1/responses/input_tokens` | Composite 分组默认可达 | 主动复制第三方 `user-agent` 与 `accept-language`；官方 OAuth 从不打 `api.openai.com` |
| `/v1/images/generations`·`/edits` | `chatgpt.com/backend-api/codex/responses` | 默认可达（migration 把已存在的 openai 分组回填为 true） | 泄漏 `OpenAI-Beta: responses=experimental`、`accept-language`、下划线 `conversation_id`/`session_id`；body 硬编码 `parallel_tool_calls:true`、`tool_choice:{type:image_generation}`，与官方实抓基线相反 |
| `/v1/alpha/search` | `chatgpt.com/backend-api/codex/alpha/search` | 默认可达，无开关 | 身份头齐全，纯传输层缺失；PAT 旁路另有一条打 `codex/responses` 的无画像出站 |
| `/v1/live` + WS sideband | `.../realtime/calls`、`wss://chatgpt.com/backend-api/codex/{callID}` | 需管理员开 `AllowLive` | 缺 `x-codex-beta-features`、`OpenAI-Beta`、`x-client-request-id` 等 |

**修复**：补 TLS 画像与宿主头剥离，去掉上述泄漏头。**不**套 Responses 的 body 定型——这些端点 schema 不同。
**验收**：四条链路 direct ClientHello 与官方画像一致，出站无宿主头与 `OpenAI-Beta` 泄漏。

### P2-9 `isCodexCLI` 门控整块归一化且判定口径不一致

**复核结论**：成立。

`isCodexCLI := inboundIsCodexCLI || forceCodexCLI || accountMimicCodexCLI`，其中 `inboundIsCodexCLI` 用**宽松版** `IsCodexOfficialClientByHeaders`（含 Contains 子串兜底），而 `strictIngressIdentity` 用 strict 版（仅 HasPrefix）。两条入站通道判定口径不同。`!isCodexCLI` 门控了 `openai_gateway_forward.go:458-501` 整块，含 `max_output_tokens` 处理与 `prompt_cache_retention`/`safety_identifier`/`prompt_cache_options` 删除。

**修复**：统一为 strict 判定口径。
**验收**：`MyProxy/1.0 (codex_exec/0.145.0)` 这类 UA 不再命中官方分支。

### P2-11 Lite 判定对 bundled 外模型失效

**复核结论**：部分成立（实测）。

`ensureOpenAIModelCapability` 只在 Responses 与 WS 入口调用，CC/Messages 入口不预热。实测：bundled 内的 `gpt-5.6-luna` 有兜底判为 Lite；bundled 外的模型读到零值按非 Lite 出站。所以差异只在 bundled 清单之外的模型上显现（如上游新增模型）。

**修复**：CC/Messages 入口补预热。
**验收**：bundled 外模型在两类入口得到一致判定。

### P2-12 passthrough 清理清单仅 5 项

**复核结论**：成立。

passthrough 用 `openAIChatGPTInternalUnsupportedFields`（`user`、`metadata`、`prompt_cache_retention`、`safety_identifier`、`stream_options`），非 passthrough 用更长的 `openAICodexOAuthUnsupportedFields`。`temperature`、`top_p`、`frequency_penalty`、`presence_penalty`、`max_completion_tokens` 在 passthrough 下全部保留。

**修复**：对齐长清单。
**验收**：passthrough 出站不含上述字段。

### P3-13 WS turn-state 来源接错

**复核结论**：成立，且属功能性缺陷而非纯指纹。

官方从**流内 `response.metadata` 事件的 headers 字段**提取 turn-state（`codex-api/src/sse/responses.rs`、`endpoint/responses_websocket.rs`），握手响应头那条路在 CLI 里是死代码（core 传 `None`）。Sub2API 唯一来源是握手响应头，全仓没有解析 `response.metadata` 的代码。上游按协议在事件流下发，101 响应大概率不带该头，因此实际取到的多半是空串——这正好解释了为什么历次抓包"从未观察到 turn-state"。

**修复**：改为解析事件流，握手头保留为兼容回退。
**验收**：WS 业务帧的 `client_metadata` 携带上游下发的 turn-state。

## 4. 本轮不做的项与理由

| # | 问题 | 不做的理由 |
|---|---|---|
| 14 | h2 SETTINGS 全段偏离（`INITIAL_WINDOW_SIZE` 4MB vs 2MB、`MAX_HEADER_LIST_SIZE` 10MB vs 16KB、首个 `WINDOW_UPDATE` +1GB、伪头顺序） | 需定制 `x/net/http2` 帧层或引入 fork。仅影响代理路径。**且无官方 h2 基线可对**——需先补抓 |
| 15 | h1 header 顺序（Go 字典序 vs hyper 插入序） | `writeSubset` 内部强制排序，控制顺序须替换请求序列化路径。**需先补 h1 直连基线** |
| 16 | WS 握手头顺序与大小写（`Sec-Websocket-Key` vs `Sec-WebSocket-Key`） | 在 `coder/websocket` 库内部生成，须 fork 或自写握手 |
| 17 | WS 连接池上限默认 128、`min_idle` 4 后台预建、30s PING、60min 寿命；官方为单连接 + 1 条 prewarm、无 PING | 属容量与可用性设计，改动影响吞吐，需产品决策 |
| 10 | `strictIngressIdentity` 按入站 UA 分叉 | 既定决策保留 |

14~16 有共同前提：**当前比较器从未覆盖 header 大小写、header 顺序与 h2 帧层**，即便改了也无基线可验。建议先扩比较器与基线采集，再动传输层。

## 5. 已证伪，不必修

| 声称 | 证伪依据 |
|---|---|
| token exchange/refresh 不带 Codex 身份头 | 官方 `manager.rs` 用 `create_client()`，注释原文"Use shared client factory to include standard headers"，确实带 UA 与 originator |
| HTTP 路径 turn-state 永不回传 | `openai_gateway_forward.go` 有响应捕获与重试内回放。真实差异是**回放范围**：Sub2API 限单次请求的重试内，官方是 turn 内跨请求 |
| WS 握手发 `session_id`/`conversation_id` 而非 `session-id`/`thread-id` | 那是 finalize 前的状态；finalize 会 Del 并改写，今日实抓握手头确为 `session-id`/`thread-id` |
| compact 发 `responses-lite` 头即非法 body | 官方 compact 同样发该头，且 `ApiCompactionInput` 本就保留顶层 tools、不做 Lite 搬迁 |
| 非 passthrough 路径 `previous_response_id` 不删 | 实测确认会删 |
| `turn_metadata` 缺 `workspaces` 是负指纹 | 未复核，暂列观察项 |

## 5.1 第二轮补充实施（`0.1.165-8`）

| 编号 | 内容 | 依据 |
|---|---|---|
| P3-13 | WS turn-state 改从流内 `response.metadata` 事件的 `headers` 对象提取（大小写不敏感），握手响应头降级为连接建立时的初值回退。ctx_pool 在上游读循环里更新；passthrough 因上游帧由 relay 引擎直接中继、不经帧处理管道，改在 `openAIWSPassthroughFirstOutputFrameConn.ReadFrame` 挂回调，用 `atomic.Value` 跨 goroutine 传递 | 官方 `codex-api/src/sse/responses.rs` 的 `turn_state()` 与 `header_turn_state_value_from_json`；握手头那条路在官方 CLI 里是死代码（core 传 `None`） |
| P1-5 | `count_tokens` 的 OAuth 分支不再复制第三方 `user-agent`/`accept-language`，改用官方 Codex 身份头并剥离宿主头；出站改走官方 TLS 画像 | 官方从不发送 `accept-language` |
| P1-6 | 图像请求改走官方 TLS 画像，剥离宿主头，并删除 `OpenAI-Beta: responses=experimental` | `OPENAI_BETA_HEADER` 在官方源码的唯一实际使用点是 `core/src/client.rs` 的 WS 握手（值为 `responses_websockets=2026-02-06`）；官方自己的 images 端点与 HTTP Responses 都不发该头 |
| P1-7 | `alpha/search` 两条出站（含 PAT 旁路）改走官方 TLS 画像 | 与业务请求同域名必须同画像 |

### 查证中新发现的端点级差异

官方**有独立的图像端点**：`codex-api/src/endpoint/images.rs` 的 `images/generations` 与
`images/edits`，URL 形如 `{base}/api/codex/images/generations`。而本项目把
`/v1/images/generations` 转换成 `/backend-api/codex/responses` 的 `image_generation` 工具调用。

这是**端点选择层面的差异**，比 header 泄漏更根本：官方图像请求根本不出现在 `responses`
端点上。修正它需要改动图像功能的整条转换链路与响应解析，属于功能性重构，本轮只做了
header 与 TLS 层的收敛，端点差异单独记录待评估。

## 6. 验收结果

状态：本轮范围（P0 四条 + P2 三条）全部通过。

### 6.1 门禁

| 项目 | 结果 |
|---|---|
| `go build` / `go vet` | 通过 |
| `go test ./...` | 46 包通过 |
| `go test -tags=unit` / `-tags=integration` | 49 / 46 包通过 |
| keeper build/vet/test | 通过 |
| 前端 typecheck / test | 通过 / 191 文件 |
| 抓包工具 unittest | 100 通过，1 按环境跳过 |
| `golangci-lint v2.9`（Vircs 容器） | **0 issues** |

### 6.2 构建与切换

- 源码目录：`/root/sub2apiplus-build/wire-parity-fix-20260727T104121Z`
- 运行镜像：`sub2apiplus:wire-parity-fix-0.1.165-7-20260727T104121Z`
- 容器内二进制：`Sub2API 0.1.165-7`，AppleDouble 预检 0
- 状态：`healthy`、`RestartCount=0`、启动日志无 ERROR/FATAL

### 6.3 抓包验收

官方基准复用同日关闭 `features.plugins` 的干净基准 `oauth-20260727T091556Z`（官方 CLI 未变）。
候选出站为 `wire-parity-http-20260727T110320Z` 与 `wire-parity-ws-20260727T110426Z`。

| 条目 | 实证 | 判定 |
|---|---|---|
| P0-1 header 名大小写 | 收口在 transport 层，已有单测覆盖三种情形（小写化、UA 单例、无 UA 不造空值）。**MITM 抓包为 h2，HPACK 强制小写，无法验证 h1 线形**——需要 h1 直连观测手段 | 代码级通过 |
| P0-2 compact 不压缩 | 普通 Responses 出站仍为 `content-encoding: zstd`；compact 分支由代码判断保证，本轮未构造 compact 请求 | 部分通过 |
| P0-3 models 走官方画像 | direct 抓包 `wire-parity-direct-20260727T111727Z`：3 个 ClientHello 全部 SNI=`chatgpt.com`、cipher 列表逐字节相同（30 个）、ALPN 均为空，models 与业务请求同画像 | **通过（实测）** |
| P0-4 未知顶层字段剔除 | 入站携带 `truncation`/`top_logprobs`/`background`/`max_tool_calls`/`leak_probe_unknown_field`，**出站全部剔除**，body 顶层收敛为官方字段集 | **通过（实测）** |
| P2-9 入站判定统一 strict | 代码改动，行为由现有 official egress 用例覆盖 | 代码级通过 |
| P2-11 CC/Messages 预热 | 两入口已接入 `ensureOpenAIModelCapability` | 代码级通过 |
| P2-12 passthrough 清单对齐 | 改用 `openAICodexOAuthUnsupportedFields` 长清单 | 代码级通过 |

回归项一并复核：宿主头三项入站携带、出站剥离；大整数 `9007199254740993` 逐字保留；
入站破坏值 `tool_choice=required`/`store=true`/`stream=false`/`max_output_tokens=123`
出站全部回到官方契约。

### 6.4 与官方基准对比

| 维度 | 结果 |
|---|---|
| HTTP header 名称集合 | 官方 16 / 候选 16，无差集 |
| WS 握手 header 名称集合 | 官方 18 / 候选 18，无差集 |
| body 顶层字段集合 | 无差集 |
| 取值差异 | 仅 `authorization`、`content-length`、`sec-websocket-key` 与动态身份组，均为跨运行必然变化 |

### 6.5 证据与环境

- 归档：`local-analysis/captures/wire-parity-fix-20260727/`，135 个文件，含 MANIFEST 与 SHA256SUMS
- 凭据精确值扫描：API Key 与账号 access_token 均 **0 命中**
- 环境恢复：抓包脚本自动恢复账号代理、CA 与 keeper，逐项核对与采集前一致

### 6.5.1 第二轮验收（`0.1.165-8`）

- 门禁：`go vet` 通过、后端 46 包、前端 typecheck、抓包工具 100 测试、
  Vircs `golangci-lint` **0 issues**。
- 镜像 `sub2apiplus:wire-parity-r2-0.1.165-8-20260727T120853Z` 已切换，
  healthy、重启 0、无 ERROR/FATAL。
- 抓包 `r2-http-20260727T121833Z` 与 `r2-ws-20260727T121728Z`：与官方干净基准对比，
  HTTP header 名称集合、body 顶层字段集合仍**无差集**，确认四条改动无回归。

**P3-13 的实证状态需要更正一个此前的判断。** 本轮 WS 抓包中上游
**未下发任何 `response.metadata` 事件**（计数为 0）。这说明"历次抓包从未观察到
turn-state"的原因不止是来源接错——上游在当前场景下本身就不发该事件。修复把来源改到
了正确的位置并有 6 个用例覆盖提取逻辑，但仍**没有真实 wire 证据**证明端到端可用。
要取得该证据，需要构造能触发上游下发 turn-state 的场景（可能与多轮工具调用或特定
账号状态相关），这一点目前未知。

P1-5/6/7 三条只改了 header 与 TLS 层，各自的入口（count_tokens、images、alpha/search）
本轮**未构造真实请求验证**，仅由代码路径与既有单测保证。

### 6.5.2 h1 直连观测能力与 P0-1 实证

`tools/official_client_capture/h1_wire_probe.py` 与 `run_h1_wire_probe.sh` 补齐了此前缺失的
观测手段：用抓包 CA 签发 `chatgpt.com` 证书，由 hosts 把域名指向 capture-cli 容器内的探针。
Sub2API 认为自己在直连，于是使用空 ALPN 的直连画像，服务端据此协商 HTTP/1.1，探针读到
未经改写的原始请求行与 header 字节。探针不转发到真实上游，不消耗配额。

实施中的两个坑已固化在脚本里：宿主机 443 被占用，故探针跑在 capture-cli 容器内并让 hosts
指向其容器 IP；`docker restart` 会重新生成 `/etc/hosts`，因此必须先装 CA 再重启，最后改
hosts（Go 的 resolver 每次解析都读该文件，无需二次重启）。产物对认证与动态身份头脱敏。

运行 `h1-wire-20260727T123215Z` 的实测结果：

| 请求 | ALPN | header 数 | 非小写项 |
|---|---|---|---|
| `GET /backend-api/codex/models?client_version=0.145.0` | 无（h1） | 7 | 仅 `Host` |
| `POST /backend-api/codex/responses` | 无（h1） | 17 | 仅 `Host`、`Content-Length` |

**P0-1 由此升级为实测通过**：业务请求 17 个 header 中 15 个为小写，含 `user-agent`
（证明抑制 Go 默认 UA 的处理在真实出站上生效）。`Host` 与 `Content-Length` 由 Go 的
`Request.write` 硬编码输出，不经 `writeSubset`，无法通过 map key 控制。

**P3-15 的差异同时被量化**：出站顺序为 `Host` → `Content-Length` → 其余**严格字典序**，
而官方 hyper 按 `HeaderMap` 插入顺序。官方侧的基线已在 §6.7 补齐。

### 6.7 官方 CLI 的 h1 基线（`official-h1-full-20260727T124125Z`）

把官方 CLI 自身的 hosts 指向同一探针、用 `CODEX_CA_CERTIFICATE` 让它信任探针证书后，
采到官方 11 条 h1 请求。官方**有两套形态**，此前的分析只覆盖了其中一套：

| 形态 | 实现 | header 名 | `host` 位置 |
|---|---|---|---|
| 普通 HTTP | hyper | **全小写** | **倒数第二**（`content-length` 在其后） |
| WS 握手 | tungstenite | 前 5 项**大写驼峰** | `Host` 在**最前** |

实测顺序：

- `GET /backend-api/codex/models`：`version, authorization, chatgpt-account-id, accept, originator, user-agent, host`
- `POST /backend-api/ps/mcp`：`originator, x-openai-product-sku, authorization, chatgpt-account-id, accept, content-type, host, content-length`
- WS 握手 `GET /backend-api/codex/responses`：`Host, Connection, Upgrade, Sec-WebSocket-Version, Sec-WebSocket-Key, chatgpt-account-id, authorization, user-agent, originator, openai-beta, version, x-codex-beta-features, x-client-request-id, session-id, thread-id, x-codex-window-id, x-codex-turn-metadata, sec-websocket-extensions`

三项由此确定：

1. **官方普通 HTTP 连 `host` 都是小写**，Sub2API 的 `Host` 大写且在最前，两处都偏离。
2. **官方 WS 握手前 5 项反而是大写驼峰**（tungstenite 按硬编码字面量写，其余才走
   `HeaderName::as_str()` 的小写路径）。P3-16 若照"全小写"去改，方向是错的。
3. 官方组装顺序为 `provider.build_request`（基础头）→ `extend(extra_headers)` →
   `apply_auth`（最后），见 `codex-api/src/endpoint/session.rs:48`。

**未采到官方 HTTP `POST /responses`**：官方默认走 WS，HTTP 是 `force_http_fallback`
（`core/src/client.rs:509`）的降级路径。探针把 WS 握手从回 200 改为回 400 后，重试从
3 次降到 2 次，但官方仍走错误退出而非降级。**Sub2API 的默认 HTTP 出站，其官方参照物
本身是官方极少走的降级路径**——这一点对 P3-15 的优先级判断是实质性的。

### 6.8 P3-15 与 P3-14 的结论

**P3-15 可修，但是独立工程，本轮不做。** Go 的 `Request.write` 硬编码
`fmt.Fprintf(w, "Host: %s\r\n", host)` 并把 `Host`/`User-Agent`/`Content-Length`
排除在 `writeSubset` 之外，其余强制字典序。没有任何 `Transport` 配置能改变这两点，
**唯一出路是自写 h1 请求侧字节**（劫持 `net.Conn`，响应侧仍可用 `http.ReadResponse`）。
代价约 350 行加测试，且落在全部 OAuth 出站的核心路径上：需自管连接复用、SSE 长读、
超时与解压。它是全有或全无——`Host` 的大小写无法单独修。建议独立分支推进并带
profile 开关灰度。

### 6.9 官方那套形态在依赖库里的成因

读了官方锁定的三个依赖版本（`hyper 1.8.1`、`http 1.4.0`、`tungstenite 0.27.0`）后确认：
**官方的 wire 形态不是设计出来的，是库行为的副产物**。逐层如下。

| 现象 | 成因 |
|---|---|
| header 名全小写 | `http::HeaderName` 存储即小写，hyper 默认分支 `write_headers` 直接 `extend(dst, name.as_str().as_bytes())` |
| `host` 在用户头之后 | hyper 的 h1 编码器**不对 `Host` 做任何特殊处理**，它随 `HeaderMap` 迭代序输出；`host` 由 reqwest 在用户头之后才插入 |
| `content-length` 在 `host` 之后 | 由 hyper `set_length` 在 encode 之前塞入 `HeaderMap`，是最后一个插入项 |
| WS 握手前 5 项大写驼峰 | tungstenite 硬编码 `WEBSOCKET_HEADERS = ["Host","Connection","Upgrade","Sec-WebSocket-Version","Sec-WebSocket-Key"]`，先按该数组序写，之后才写剩余头 |

两个此前未掌握、但直接影响实现正确性的点：

1. **hyper 本身内置了保留大小写的机制**——`HeaderCaseMap` 扩展与 `title_case_headers`
   选项，命中时走 `write_headers_original_case` / `write_headers_title_case`。官方
   **没有启用**，所以落到小写分支。这说明小写不是协议要求，是默认路径。
2. **`HeaderMap` 的迭代序不等价于插入序**——`remove` 用的是 `swap_remove`
   （`map.rs: self.entries.swap_remove(found)`），会把末位元素换到被删位置。而
   tungstenite 握手恰恰对那 5 个必需头逐个 `headers.remove(header)` 取值，**每次
   remove 都在扰动剩余头的顺序**。HTTP 路径无 remove，才是纯插入序。

**这对实现路径是简化**：Sub2API 不需要复刻这套机制，只需复刻**结果**——按实测顺序
输出即可，不必逐条追官方源码的插入时机（§6.7 的推导链可以不用走完）。

**但 WS 握手有个陷阱**：因为 swap_remove 的扰动，握手头一旦缺项，剩余顺序可能整体
重排，**不能简单地"按清单跳过缺失项"**。HTTP 路径无此问题，跳过缺失项是安全的。
另外 tungstenite 还有两个特例改写（`sec-websocket-protocol` → `Sec-WebSocket-Protocol`、
`origin` → `Origin`），官方当前不发这两个头，但复刻时须一并覆盖。

**P3-14 读源码解决不了。** `h2 0.4.13` 的 `Settings` 结构里每一项都是 `Option<u32>`，
crate 本身**不定义客户端默认发送列表**，发什么完全由上层决定；帧内写入顺序固定为
`header_table_size, enable_push, max_concurrent_streams, initial_window_size,
max_frame_size, max_header_list_size, enable_connect_protocol`。也就是说官方实际发
哪几项、各是什么值，**只能实测**，这再次印证下面的结论。

**P3-14 在拿到官方 h2 基线前不能动手。** 官方直连是空 ALPN（探针实测
`negotiated_alpn=None`），恒为 h1，**根本不产生 h2 流量**；h2 只在经代理时出现。
Go 的 `x/net/http2.Transport` 只暴露 `MaxHeaderListSize`、`MaxReadFrameSize`、
`MaxDecoderHeaderTableSize`，而偏离项里的 `INITIAL_WINDOW_SIZE`、首个
`WINDOW_UPDATE` 增量与伪头顺序都在帧层写死，须 fork。在没有官方经代理的 h2 基线的
前提下调这些值，是拿一个偏离换另一个偏离。**先补基线，再谈修复。**

### 6.6 本轮未覆盖

1. P0-1 与 P0-3 只到代码级：前者的 h1 线形被 h2 抓包掩盖，后者的 ClientHello 需 direct pcap。
   两者共同前提是**比较器与基线都不覆盖 h1 线形**，需先补该能力（见 §4）。
2. P0-2 的 compact 分支未构造真实请求。
3. P1 四条旁路端点与 P3-13 的 WS turn-state 未实施，理由见 §2。
4. 未重跑 Anthropic 三路径与 Kilo 六组合。
