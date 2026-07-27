# P0-P2 官方出站最终复核修复与验收记录

日期：2026-07-27  
状态：已完成并通过验收  
目标版本：`0.1.165-4`

## 1. 范围与结论口径

本轮只处理第三方工程师最终复核确认的剩余问题，不重复此前 Kilo 六组合回归。修复范围覆盖 OpenAI OAuth 官方出站 HTTP/WS 的 Lite/非 Lite 定型、官方 compact 身份、Anthropic system 与动态 beta 比较器，以及能够真实触发这些分支的定向抓包场景。

验收采用三层证据：代码和破坏性单元测试、官方 Codex/Claude 基准与 Sub2API 同场景出站对比、Vircs 生产镜像健康与版本一致性。没有实际触发的场景不得标记为 wire 验收通过。

## 2. 已验证问题与修复要求

| 编号 | 问题 | 修复要求 | 验收标准 | 状态 |
|---|---|---|---|---|
| FR-P0-01 | 非 Lite HTTP/WS 绕过官方固定字段定型 | `tool_choice=auto`、`store=false`、`stream=true`、`include=[reasoning.encrypted_content]`；删除 `max_output_tokens`；非 Lite 删除 `reasoning.context`，Lite 固定 `all_turns` | HTTP/WS × Lite/非 Lite 破坏性测试；定向非 Lite 出站抓包 | 已通过 |
| FR-P0-02 | `parallel_tool_calls` 只按 Lite 处理，能力快照缺少并行能力位 | manifest 与 bundled 快照同时记录 `use_responses_lite`、`supports_parallel_tool_calls`；最终值按官方能力与 Lite 状态决定 | manifest 覆盖、冷启动、失败降级及 HTTP/WS 测试 | 已通过 |
| FR-P0-03 | derived 路径无条件把非 Lite 顶层 `instructions` 搬入 `input` | 仅 Lite 迁移 `instructions/tools`；非 Lite 保留顶层 `instructions/tools` | 非 Lite HTTP/WS 输入条数不增加，正文逐字节守恒 | 已通过 |
| FR-P1-01 | 官方 compact 不发 `x-client-request-id`，当前必填、UUID、一致性、登记和 Header 输出仍要求该字段 | `isCompact` 全链路豁免 client request；普通 Responses 继续严格校验 | 官方源码形态 fixture 成功；普通 Responses 缺失仍拒绝 | 已通过 |
| FR-P1-02 | 非 Lite 错误回归测试固化第三方顶层值 | 重写为官方固定契约断言，并补 HTTP/WS 对称测试 | 旧错误值全部被测试拒绝 | 已通过 |
| FR-P1-03 | Anthropic system 三块及以上直接短路 | 按官方 split marker 重组 system；第三方 system 通过同次 ingress→egress 摘要校验，不以块数豁免 | 1/3/4 块正例与删改反例；真实三块 ingress 抓包 | 已通过 |
| FR-P1-04 | 动态 beta 未触发时返回 `true` 形成空真 | 输出 `exercised` 与 `valid`；专用 tool-search 门禁必须实际触发 `defer_loading` | 顶层/custom 两种信号正例、缺 beta 反例、空工具未覆盖状态；真实 custom 信号抓包 | 已通过 |
| FR-P1-05 | 当前抓包全部来自官方 Codex 入站，无法证明第三方异常字段被定型 | 增加非 Codex 定向客户端，请求携带 `tool_choice=required`、`max_output_tokens`、错误 include/store/stream/context 和 tool search | 入站明确包含破坏值，出站为官方契约，业务字段守恒 | 已通过 |
| FR-P2-01 | compaction 普通 `/responses` 样本未检查语义守恒 | 对该组普通 Responses 检查应用层语义；`/responses/compact` 独立 fixture；自定义 app-server `clientInfo` 产生的 UA/originator 不冒充 Codex Exec 固定身份 | compaction 语义守恒；compact fixture 通过；控制样本身份差异明确保留 | 已通过（控制样本不计固定身份契约） |

不处理项：`OfficialEgressFinalizationResult.Modifications` 当前没有读取或序列化消费者，其记录不准确不影响请求与验收判定，本轮不扩大范围。

## 3. 官方 0.145.0 契约

普通 Responses：

- Lite：`instructions` 迁入 `input`，工具迁入 `additional_tools`，`reasoning.context=all_turns`，`parallel_tool_calls=false`。
- 非 Lite：保留顶层 `instructions` 和 `tools`，不序列化 `reasoning.context`。
- 两者共同固定：`tool_choice=auto`、ChatGPT OAuth `store=false`、`stream=true`、`include=[reasoning.encrypted_content]`，不发送 `max_output_tokens`。
- `text.verbosity` 可受 Codex 用户配置覆盖，不作为无条件固定 low 的字段。

compact：发送 installation/session/thread/window/turn metadata 与 `prompt_cache_key`，不发送 `x-client-request-id`，Body 使用 compact 独立 schema。

## 4. 实施与验收步骤

1. 修改能力快照、HTTP/WS Finalizer、compact 身份与测试。
2. 修改抓包规范化和比较器，消除 system 与动态 beta 空转。
3. 运行 Go 全量测试、unit/integration tag、vet、golangci-lint、后端/前端/keeper 构建测试、抓包工具测试。
4. 将完整源码同步到 Vircs 独立构建目录，构建唯一标签镜像，切换 compose 主服务并确认二进制版本、镜像 ID、健康状态和重启次数。
5. 重跑官方 Codex/Claude 基准与 Sub2API 定向出站；Anthropic 若仍受上游账号状态阻断，应用/TLS 与成功响应必须分别报告。
6. 将全部原始 pcap、MITM JSONL、入站记录、CLI 事件、比较报告和失败尝试拉回本地新目录，生成 `MANIFEST.md` 与 `SHA256SUMS`，并执行凭据精确扫描。
7. 回填本文件与 README。只有实际通过的门禁才标记为通过。

## 5. 最终结果

### 5.1 实现结果

1. HTTP 与 WS 终态定型已统一覆盖 Lite/非 Lite。非 Lite 保留顶层 `instructions/tools`，删除 `max_output_tokens` 与 `reasoning.context`；两类路径共同固定 `tool_choice/store/stream/include`。
2. 能力解析同时携带 `use_responses_lite` 与 `supports_parallel_tool_calls`，bundled、账号 manifest、旧值降级和失败退避路径均有回归测试。
3. compact 全链路不再要求 `x-client-request-id`；普通 Responses 仍保持严格身份校验。
4. Anthropic 比较器以正文摘要验证 system 拆分或迁移，并要求原 messages 完整保留；三块 system 不再短路。动态 beta 同时输出 `exercised/valid`，固定画像比较只剔除已经由功能门禁独立验证的动态 token。
5. 新增真实第三方 HTTP、原始 RFC 6455 WS 与 Anthropic 定向客户端，实际发送破坏值和 `custom.defer_loading=true`，不是用官方 CLI 入站制造空真。

### 5.2 本地工程门禁

以下命令均在最终运行时代码完成后通过：

- 后端：`go test ./... -count=1`、`go test -tags=unit ./... -count=1`、`go test -tags=integration ./... -count=1`、`go vet ./...`、`go build ./...`。
- 静态检查：CI 同版 `golangci-lint v2.9.0`，结果 `0 issues`。
- 前端：lint、typecheck、Vitest `191` 个文件 / `1318` 个测试、生产构建全部通过。
- keeper：test、vet、build 全部通过。
- 抓包工具：最终 `100` 个测试通过，`1` 个按环境条件跳过。

### 5.3 Vircs 构建与生产切换

- 源码目录：`/root/sub2apiplus-build/p0-p2-final-review-fix-20260727T004258Z`。
- 运行镜像：`sub2apiplus:p0-p2-final-review-fix-0.1.165-4-20260727T004258Z`。
- 镜像 ID：`sha256:5995e15f9d2a793a568bbe6e125393f548b8d4fcc3390df2df5d2c63feacd774`。
- 二进制：`Sub2API 0.1.165-4`，commit `5770cff47d66-p0p2-final-review-fix`。
- compose 备份：`/root/Docker/sub2apiplus/backups/docker-compose.pre-0.1.165-4-20260727T004258Z.yml`。
- 最终状态：主服务 `healthy`、`RestartCount=0`；生产 compose 已指向上述镜像。

### 5.4 抓包验收

主比较报告：`final-review-0.1.165-4-comparison-20260727T0138Z/summary.json`，结果 `all_equal=true`。Lite HTTP/WS、非 Lite HTTP 及对应 TLS 均为 `undeclared_differences=0`；标准非 Lite WS 的完整预热与业务帧序列由最终扩展门禁再次验证。

第三方扩展报告：`final-review-0.1.165-4-final-verification-20260727T0157Z/summary.json`，结果 `all_passed=true`、`tls_passed=true`：

- 非 Lite HTTP/WS 入站确实携带 `tool_choice=required`、`max_output_tokens=123`、错误 store/stream/include/context；出站逐项定型正确，顶层 instructions、tools、reasoning effort/summary 与 text 保持。
- HTTP Cookie 形成“首请求无 Cookie → 首响应 Set-Cookie → 第二请求回放 Cookie”的闭环。
- Anthropic 入站为三块 system，工具含 `custom.defer_loading=true`；出站 system/messages 语义守恒，并发送 `advanced-tool-use-2025-11-20`，`exercised=true`、`valid=true`。
- 非 Lite WS 与 Anthropic HTTP 的 direct TLS 均与官方基准契约一致。

compact 控制动作实测由 Codex app-server 发出普通 `/responses`，不是 `/responses/compact`。其正文语义守恒已检查；由于测试 app-server 的自定义 `clientInfo=sub2api-capture` 会改变官方直连的 UA/originator，该样本保留 `acceptance_contract=false`，不冒充 Codex Exec 固定身份门禁。真正 compact 的“无 `x-client-request-id`”契约由官方源码形态 fixture 与普通 Responses 反例锁定。

Anthropic #50 的真实请求已命中账号并到达上游，但上游返回 `400 This organization has been disabled`，经 Sub2API failover 后客户端看到 `502`。该外部账号状态不影响请求出站画像、system/beta 与 direct TLS 证据，但不能宣称 Anthropic 端到端业务成功。

### 5.5 证据归档与恢复

- 本地私有归档：`local-analysis/captures/official-egress-final-review-fix-20260727-094500/`。
- 归档包含完整官方 direct pcap/MITM JSONL/CLI 事件，以及 Sub2API direct pcap、ingress、MITM JSONL、结果、全部比较报告与失败尝试；不是只保存规范化摘要。
- Vircs 运行时 API Key、#50/#90 的 access/refresh/id token 已对全部纳入目录执行精确值扫描，结果无命中。
- #90 已恢复 `active/schedulable=true`、无代理；#50 精确恢复采集前的 `error/schedulable=false`、无代理。临时代理数为 0，CA 恢复，抓包端口与 PID 文件清空，keeper 运行。
- 本地归档生成 `MANIFEST.md` 与 `SHA256SUMS`，文件权限收紧为仅当前用户可读写。

结论：本文件列出的最终复核修复均已完成；所有适用的代码、应用层与 TLS 门禁通过。唯一未通过的是 Anthropic 上游业务响应，原因是账号组织被官方禁用，已作为外部阻断明确保留，不计为伪装实现通过。
