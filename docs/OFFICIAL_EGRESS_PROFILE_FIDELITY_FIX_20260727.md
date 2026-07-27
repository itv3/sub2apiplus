# 官方出站画像保真修复与验收记录

日期：2026-07-27
状态：待验收
目标版本：`0.1.165-6`
基线版本：`0.1.165-5`（Vircs 生产运行镜像 `ghcr.io/itv3/sub2apiplus:0.1.165-5`）

## 1. 范围与口径

本轮处理源码复核发现的五条官方出站画像保真缺陷。判定标准沿用 README §1.1.2.3：只有
`contract_equal=true`、候选 ingress→egress 语义守恒且 `undeclared_differences=0` 才算通过。
行为层（辅助/遥测流量）按 README §1.1.2.3 不作对比维度。

复核过程中确认**不成立**、因而不在本轮范围内的项，一并记录以免重复排查：

| 曾怀疑的问题 | 证伪依据 |
|---|---|
| HTTP 入站会转 WS 出站且不套画像 | `openai_client_transport.go:63-71` 在入站为 HTTP 时无条件返回 HTTP 决策，`Responses` handler 固定标记 HTTP，该分支对 HTTP 入站不可达 |
| 出站泄漏 `Accept-Encoding` | 三个官方 TLS Profile 均设 `DisableCompression: true`，并有 `TestNewH2TransportDisableCompressionOmitsAcceptEncodingOnWire` 锁定 |
| 缺失 `x-openai-internal-codex-residency` | 官方 `default_client.rs:360-368` 仅在配置 US 驻留要求时才发送 |
| Chat Completions 入口 Lite 判定失效 | `bindOpenAIResponsesLiteCapability` 位于 `buildUpstreamRequest` 首行，该入口正常绑定 |
| `x-client-request-id` 与 thread_id 不一致 | derived 身份中 `threadID == sessionID == clientRequest`，满足官方契约 |
| WS 经代理未切换 TLS 画像 | WS 本就走 rustls 栈，代理不改变 ClientHello；HTTP 需切换是因直连走 native-tls 的 30-cipher |
| WS 压缩协商 `client_max_window_bits` 触发握手失败 | 实抓证明官方 CLI 与 Sub2API 的 offer 完全一致，上游均回 `permessage-deflate` 不回显该参数 |

## 2. 修复项

| 编号 | 问题 | 根因 | 修复 | 验收标准 |
|---|---|---|---|---|
| FX-01 | 入站 `accept-language` 泄漏到官方出站 | 两份透传白名单放行该头，OpenAI Finalizer 的删除列表未包含它（Anthropic 侧已删） | 抽 `officialEgressInboundHostHeaders` 共享常量（`accept-language`、`sec-fetch-mode`、`x-stainless-helper-method`），三条路径统一调用 `stripOfficialEgressInboundHostHeaders` | 第三方与官方入站携带该头时出站均不含；官方 UA/originator 保持不变 |
| FX-02 | WS turn-state 跨连接沿用 | `ingress.go` 写成"握手返回非空才覆盖"，新连接未下发时沿用上一条连接的值 | 提取 `replaceOpenAIWSTurnStateFromLease`，语义改为整体替换：握手未下发即返回空串 | 新连接握手无 turn-state 时解析为空，不回放旧值 |
| FX-03 | 模型能力合并语义与官方相反 | Sub2API 用 bundled 兜底 + 字段级覆盖；官方 `apply_remote_models` 在 ChatGPT 账号下远端整体接管、丢弃 bundled | 解析 `visibility`，满足接管条件时远端整条替换且缺失 slug 走 fallback（两个能力位均 false、known=true）；不满足时保留旧行为 | 远端下线某模型后不得回落 bundled 的 Lite 旧值；未加载 manifest 时 bundled 仍兜底 |
| FX-04 | CC / Messages 入口 JSON 保真损失 | 转换阶段裸 `json.Unmarshal`+`json.Marshal` 往返，大整数降级 float64、嵌套键按字典序重排 | 改用 `decodeOfficialJSONObjectUseNumber` + `marshalOfficialJSONObjectPreservingOrderAndRaw`，与 Responses 入口同一套工具 | 2^53+1 逐字节保留；工具 schema 嵌套键顺序不变 |
| FX-05 | WS passthrough 缺 prewarm 帧 | prewarm 构造与链接只在 ctx_pool 调用，passthrough 仅做单帧定型 | 在正式首帧写入前插入同步预热往返（走未包装的上游连接，不占用首输出超时状态机），再用预热响应 ID 链接业务帧 | passthrough 与 ctx_pool 形态一致：预热帧 `generate=false`、`request_kind=prewarm`、无 `turn_started_at_unix_ms`；业务帧 `previous_response_id` 指向预热响应 |
| FX-06 | 业务帧链接时丢失 turn-state（FX-05 过程中暴露的既有缺陷，ctx_pool 同样受影响） | `chainDerivedOpenAIOfficialEgressWSBusinessFrame` 整体替换 `client_metadata`，把先前注入的 turn-state 一并丢弃 | 重建 metadata 后从改写前的帧取回 turn-state 并合并 | 链接后的业务帧仍携带上游下发的 turn-state |

## 3. 测试策略

每条修复都用**对照实验**确认测试非空跑：临时回退实现后相应用例必须失败。这是针对
"测试通过 ≠ 画像生效"盲区的强制步骤，结果记录如下。

| 编号 | 新增/更新用例 | 对照实验结果 |
|---|---|---|
| FX-01 | `official_egress_inbound_host_headers_test.go`（第三方入站、官方 strict 入站、WS 握手三例） | 清空共享常量后 3 例全部失败 |
| FX-02 | `openai_ws_turn_state_test.go` | — （语义由函数命名与直接赋值锁定） |
| FX-03 | `openai_model_capabilities_manifest_test.go`（接管、非接管、未加载、不继承 bundled 字段四例） | 强制 `remoteAuthoritative=false` 后接管用例失败 |
| FX-04 | `openai_gateway_compat_json_fidelity_test.go`（CC 与 Messages 两例） | 回退为 `json.Marshal` 后键顺序断言失败 |
| FX-05 | `openai_ws_passthrough_prewarm_test.go`（四例）+ 更新 `PassthroughHeadersUsePromptCacheAndTurnState` | 更新后的用例直接断言两帧形态 |
| FX-06 | 同上（`PassthroughHeadersUsePromptCacheAndTurnState` 的 turn-state 断言） | 修复前该断言实测失败 |

另外把实抓的官方 `/backend-api/codex/models` 响应补充 `visibility` 字段后固化进
`testdata/official_egress/codex_models_capabilities_0145.json`，继续由既有的
`TestBundledOpenAIModelCapabilitiesMatchesCapturedManifest` 与 bundled 对账。
本轮实抓复核结论：bundled 的 8 个 slug 与两个能力位与真实 manifest **完全一致**。

## 4. 本地门禁结果

| 项目 | 结果 |
|---|---|
| `go build ./...` | 通过 |
| `go vet ./...` | 通过 |
| `go test ./... -count=1` | 46 个包通过，0 失败 |
| `go test -tags=unit ./... -count=1` | 51 个包通过 |
| `go test -tags=integration ./... -count=1` | 46 个包通过 |
| keeper `build` / `vet` / `test` | 通过 |
| 前端 `typecheck` | 通过 |
| 前端 `test:run` | 191 文件 / 1318 测试通过 |
| `golangci-lint` | 本地未安装，转至 Vircs 容器执行 |
| 抓包工具 pytest | 本地无 pytest，转至 Vircs capture-cli 执行 |

## 5. 抓包验收计划

在 Vircs 构建 `0.1.165-6` 并切换运行镜像后执行，逐条对应第 2 节的验收标准：

1. 官方客户端基准：Codex CLI HTTP / WS 的 direct 与 MITM。
2. Sub2API 候选出站：同场景 HTTP / WS，第三方入站需**显式携带** `accept-language` 等宿主头
   （历史抓包的第三方客户端不带这些头，导致 FX-01 从未被触发）。
3. 逐条核对：FX-01 出站无宿主头、FX-03 Lite 判定、FX-04 正文保真、FX-05 预热两帧、
   FX-06 turn-state 保留。
4. 验收完成后完整恢复环境（账号代理、CA、keeper、临时代理），并核对 CA 哈希与原始状态一致。

## 6. 验收结果

### 6.1 Vircs 构建与切换

- 源码目录：`/root/sub2apiplus-build/profile-fidelity-fix-20260727T070913Z`
- 运行镜像：`sub2apiplus:profile-fidelity-fix-0.1.165-6-20260727T070913Z`
- 镜像 ID：`sha256:4f17ca662bf5694854f3c4afc7df309b7f072fc8732cc2147536cc81369a2071`
- 容器内二进制：`Sub2API 0.1.165-6 (commit: docker, built: 2026-07-27T07:32:00Z)`
- AppleDouble 预检：0 个残留
- compose 备份：`/root/Docker/sub2apiplus/backups/docker-compose.pre-0.1.165-6-20260727T073701Z.yml`
- 切换方式：新增 `/root/oauth-capture/runtime/sub2apiplus-t2.override.yml` 覆盖 image，
  主 compose 保持指向已发布 tag，回滚只需去掉 override
- 最终状态：`healthy`、`RestartCount=0`、启动日志无 ERROR/FATAL、健康端点 200

### 6.2 Vircs 侧门禁

| 项目 | 结果 |
|---|---|
| `golangci-lint v2.9`（容器，`--timeout=30m`） | **0 issues** |
| 抓包工具测试（`unittest discover`） | 100 个测试通过，1 个按环境跳过 |

### 6.3 抓包运行目录

| 用途 | 运行 ID |
|---|---|
| 官方客户端基准（Codex HTTP/WS，direct+MITM，S1/S2/S4） | `official-client/oauth/oauth-20260727T073930Z`（status=complete，6 pcap / 41 jsonl / 112M） |
| Sub2API 候选出站 · 第三方 HTTP | `fidelity-fix-third-party-http-20260727T074207Z` |
| Sub2API 候选出站 · 第三方 WS（passthrough） | `fidelity-fix-third-party-ws-20260727T074402Z` |

抓包脚手架同步修复：`run_third_party_profile_scenario.py` 的定向客户端此前只发
`Content-Type` 与 `User-Agent`，不带任何宿主环境头，这正是 FX-01 在历史全部抓包中
从未被触发的原因。本轮为其补上 `accept-language` / `sec-fetch-mode` /
`x-stainless-helper-method`，并在工具 schema 中加入大整数与乱序键保真探针。

### 6.4 逐条验收

| 编号 | 实证结果 | 判定 |
|---|---|---|
| FX-01 | 入站三个宿主头全部携带（`accept-language=zh-CN,zh;q=0.9,en-US;q=0.8`、`sec-fetch-mode=cors`、`x-stainless-helper-method=stream`）；HTTP 出站与 WS 握手头三者均为空，官方 `codex_exec/0.145.0` UA 与 `codex_exec` originator 保持 | 通过 |
| FX-03 | `gpt-5.6-luna` 出站为 Lite 形态：顶层 `instructions`/`tools` 均不存在、`input[0].type=additional_tools`、`reasoning.context=all_turns`、`parallel_tool_calls=false`。实抓 manifest 的 8 个 slug 与两个能力位与 bundled 完全一致 | 通过 |
| FX-04 | 出站逐字保留 `9007199254740993`（2^53+1 未降级）；`z_first` 位置 195 早于 `a_second` 位置 222，嵌套键顺序未被字典序重排 | 通过 |
| FX-05 | 候选 WS 上行 2 个 `response.create`：帧1 `generate=false`、`request_kind=prewarm`、无 `previous_response_id`、无 `turn_started_at_unix_ms`、`input=[additional_tools, message]`；帧2 `request_kind=turn`、`previous_response_id` 指向帧1 响应、含 `turn_started_at_unix_ms`。与官方基准帧序列结构逐项一致 | 通过 |
| FX-06 | 由更新后的 `PassthroughHeadersUsePromptCacheAndTurnState` 断言覆盖（修复前该断言实测失败）。上游在本轮抓包中仍未下发 `x-codex-turn-state`，因此无真实 wire 证据，只达到代码级验收 | 部分通过 |
| FX-02 | 同 FX-06：上游从未下发 turn-state，跨连接回放路径无法在真实流量中触发，只达到代码级验收 | 部分通过 |

破坏性字段定型一并复核：入站显式发送 `tool_choice=required`、`max_output_tokens=123`、
`store=true`、`stream=false`、`include=[message.output_text.logprobs]`、
`reasoning.context=none`，出站全部回到官方契约（`auto` / 已删除 / `false` / `true` /
`[reasoning.encrypted_content]` / `all_turns`）。

### 6.5 与官方基准的逐项对照

同模型 `gpt-5.6-luna` 下，官方 Codex CLI 0.145.0 实抓出站与 Sub2API 候选出站：

| 字段 | 官方基准 | Sub2API 候选 |
|---|---|---|
| 顶层 `instructions` / `tools` | 均不存在 | 均不存在 |
| `input[0].type` | `additional_tools` | `additional_tools` |
| `tool_choice` / `store` / `stream` | `auto` / `false` / `true` | 一致 |
| `include` | `[reasoning.encrypted_content]` | 一致 |
| `max_output_tokens` | 不存在 | 不存在 |
| `parallel_tool_calls` | `false` | `false` |
| `reasoning.context` | `all_turns` | `all_turns` |
| `accept-language` / `sec-fetch-mode` | 无 | 无 |
| `User-Agent` | `codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)` | 一致 |
| WS 上行帧序列 | 预热帧 + 业务帧 | 结构一致 |
| WS `Sec-WebSocket-Extensions` | `permessage-deflate; client_max_window_bits` | 一致 |

差异仅剩入站对话内容决定的 input 消息条数（官方 3 条、候选 2 条），属 README §1.1.2.3
定义的声明差异。

### 6.6 环境恢复

抓包脚本按单元自动恢复，最终核对与采集前一致：账号 #90 为 `active / schedulable=true /
无代理 / 无 fallback`；代理表回到原有 6 条；容器 `/usr/local/share/ca-certificates/` 为空；
`sub2apiplus` 与 `keeper` 均在运行且 healthy；capture-cli 无残留 mitmdump 进程。

### 6.7 未覆盖边界

1. FX-02 与 FX-06 只达到代码级验收：上游在本轮及历史全部抓包中都未下发
   `x-codex-turn-state`，跨连接回放与 turn-state 保留两条路径无法在真实流量中触发。
2. 本轮只跑了第三方定向客户端的单场景 HTTP 与 WS，没有重跑 Kilo 六组合协议转换矩阵。
3. Anthropic 侧只做了共享常量重构（行为不变），未新抓 Anthropic 基准。
