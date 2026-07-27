# P0–P2 最终复核缺口修复与验收记录

## 1. 结论与状态

2026-07-27，已按三方工程师最终复核逐项验证并完成源码修复、本地完整测试、Vircs 编译、生产镜像切换、官方/候选抓包和本地归档。

| 项目 | 最终结果 |
|---|---|
| 源码版本 | `0.1.165-3`，未正式发布 |
| Vircs 源码目录 | `/root/sub2apiplus-build/p0-p2-review-fix-20260726T213104Z` |
| 生产镜像 | `sub2apiplus:p0-p2-review-fix-0.1.165-3-20260726T213104Z` |
| 镜像 ID | `sha256:d2fb43c0588a27c6ac4e3c3e6574cd1043e0deda5c7977c88501c518429722af` |
| 二进制标识 | `Sub2API 0.1.165-3 (commit: 5770cff47d-p0p2-review-fix, built: 2026-07-26T21:31:04Z)` |
| 生产状态 | `healthy`，重启次数 0；PostgreSQL、Redis、keeper、数据卷和 `.env` 未切换 |
| OpenAI 官方出站 | HTTP、WS、非 Lite 应用契约与 direct TLS 全部 `equal=true`，语义守恒，未声明差异 0 |
| Anthropic 官方出站 | 实际出站应用契约与 direct TLS 均 `equal=true`；#50 被官方上游返回 `400 organization disabled`，不能宣称功能响应成功 |
| 本地证据 | `local-analysis/captures/official-egress-review-fix-20260727-062447/`，257 MB，私有权限与 SHA-256 清单已生成 |

本轮没有执行 Kilo 六组合；用户明确将本轮定义为新的“工程师复核意见验证与修复”任务。历史 Kilo 结果不冒充本轮证据。

## 2. 复核问题与实现结果

### 2.1 P0：数据保真、能力和真实功能

| 编号 | 修复结果 | 状态 | 验收证据 |
|---|---|---|---|
| RF-P0-01 | Anthropic、OpenAI HTTP/WS 改用 `Decoder.UseNumber()` 和顶层有序编码；嵌套复合值保留原始 JSON 字节，不再把大整数转为 `float64` 或排序用户对象 | 已验收 | 大于 2^53 整数、嵌套键序和 HTTP/WS 破坏性反例测试通过 |
| RF-P0-02 | 模型能力合并 bundled 与账号 manifest；增加 singleflight、有效缓存、过期值复用和有界负缓存；上游加载失败时按完整语义降级，不再同步拒绝业务请求 | 已验收 | 合并、过期、失败、并发和负缓存测试通过 |
| RF-P0-03 | tool search 同时识别工具顶层 `defer_loading`、`custom.defer_loading` 和既有 Bedrock 信号，并统一进入 BetaPolicy | 已验收 | 动态 beta 正反测试与 Anthropic 实际出站契约通过 |
| RF-P0-04 | Lite HTTP/WS 最终固定 `tool_choice: "auto"`；非 Lite 保留入口值 | 已验收 | HTTP/WS Lite、非 Lite专项测试；`gpt-5.4` 新抓包语义守恒 |
| RF-P0-05 | Lite HTTP/WS 最终正文删除 `max_output_tokens` | 已验收 | 双路径专项回归和 `gpt-5.6-luna` HTTP/WS 抓包通过 |

### 2.2 P1：身份、compact、TLS 与生命周期

| 编号 | 修复结果 | 状态 | 验收证据 |
|---|---|---|---|
| RF-P1-01 | HTTP 按账号、会话、turn 缓存首次真实 `turn_started_at_unix_ms`，同 turn 重试/续轮稳定，TTL 有界 | 已验收 | 同 turn、跨 turn、TTL 和并发测试通过 |
| RF-P1-02 | compact strict ingress 不再要求官方 compact 不存在的 `client_metadata`；从标准 Header 和 installation-id 派生身份 | 已验收 | 无 `client_metadata` 的 compact fixture 与 Header/Body 专项测试通过 |
| RF-P1-03 | WS→HTTP bridge 以服务端模型能力为最终决定，客户端自报不能顶回 Lite Header 或已删除字段 | 已验收 | bridge 正反测试通过 |
| RF-P1-04 | Codex token exchange/refresh 使用官方 JSON 体、字段顺序、`codex_exec/0.145.0`、`originator` 和专用 TLS transport | 已验收 | OAuth body、身份、代理/CA/Host/TLS 测试通过 |
| RF-P1-05 | Anthropic 账号测试、用量查询和 OAuth 刷新集中使用当前 2.1.220 端点身份；axios 更新为 1.15.2 | 已验收但仅代码证据 | 身份一致性测试通过；本轮未把官方辅助流量缺失伪报成 wire 对齐 |
| RF-P1-06 | 非 Lite `gpt-5.4` 使用同场景 S2 双轮重抓，跨运行工具目录作为动态差异，同次候选 `tools` 必须严格守恒 | 已验收 | 应用契约、语义守恒、Cookie 冷启动闭环与 direct TLS 均通过 |

### 2.3 P2：比较器、证据和文档

| 编号 | 修复结果 | 状态 | 验收证据 |
|---|---|---|---|
| RF-P2-01 | Anthropic `system` 不再整体移出比较；第三方 system 通过同次 ingress→egress 文本语义守恒验证 | 已验收 | system 正反测试通过 |
| RF-P2-02 | WS turn-state 比较完整生命周期；只有官方基准实际下发状态时才要求候选回放，双方均无状态时不制造失败 | 已验收 | 当前官方/候选真实 wire 均为 0/0/0，生命周期契约通过；有状态正反测试通过 |
| RF-P2-03 | 比较器覆盖动态 beta、全部已知顶层参数、非 Lite、Cookie、WS turn metadata；独立 CLI 工具目录只跨运行声明，候选同次仍严格守恒 | 已验收 | 抓包工具 93 个测试；最终报告未声明差异 0 |
| RF-P2-04 | `x-codex-turn-metadata` 使用显式顶层顺序编码 | 已验收 | 字段顺序专项测试与当前 wire 证据通过 |
| RF-P2-05 | 文档删除“能力失败仍一定成功”“手动 compact 等于 `/responses/compact`”等超出证据的结论 | 已验收 | 本文、README §1.1.3、抓包工具 README 与实证索引已更新 |

## 3. 对原 §1.1.3 方案的影响

本轮没有推翻 README §1.1.3 的总体架构：仍是“协议转换后统一 Resolver/Context/Finalizer，再进入路径专用 Transport/Dialer”。变化属于实现约束收紧：

1. “最小改写”增加原始 JSON 数据保真门禁，不能用 `map[string]any` 重建嵌套用户数据。
2. Lite 是能力驱动的官方定型：`tool_choice` 固定为 `auto`、删除 `max_output_tokens`；非 Lite 保留第三方语义。
3. 模型能力从“单一来源覆盖”改为 bundled 与账号 manifest 合并，并在加载失败时使用已有/完整语义降级。
4. 比较器把跨独立运行的动态工具目录与同次候选语义守恒分开，不能用跨运行差异掩盖候选改写。
5. WS turn-state 是条件生命周期：官方握手实际下发才回放；当前 Codex 0.145.0 实测双方均未下发。

## 4. 完整测试门禁

### 4.1 后端、静态检查与构建

| 命令/范围 | 结果 |
|---|---|
| `go test ./... -count=1` | 通过 |
| `go test -tags=unit ./... -count=1` | 通过 |
| `go test -tags=integration ./... -count=1` | 通过 |
| `go vet ./...` | 通过 |
| golangci-lint v2.9.0 | `0 issues` |
| `go build ./...` | 通过 |
| `git diff --check` | 通过 |

### 4.2 前端、keeper 与抓包工具

| 范围 | 结果 |
|---|---|
| 前端 lint / typecheck / 全量测试 / build | 通过；191 个文件、1318 个测试 |
| keeper test / build | 通过 |
| 抓包工具 | 本地和 Vircs 均 93 个测试通过，1 个按环境跳过 |
| shell / Python 静态门禁 | 新增抓包脚本 `bash -n`、Python `py_compile` 全部通过 |

## 5. Vircs 构建、切换与恢复

源码先以 checksum 模式同步到 `/root/sub2apiplus-build/p0-p2-review-fix-20260726T213104Z`，Vircs 完成 Docker 多阶段构建后生成镜像
`sub2apiplus:p0-p2-review-fix-0.1.165-3-20260726T213104Z`。生产 compose 备份为：

`/root/Docker/sub2apiplus/app/docker-compose.yml.before-p0-p2-review-fix-20260726T213104Z`

compose 只变更主服务 `image` 一行。最终镜像 ID、容器二进制、健康接口均与本文件第 1 节一致；重启次数 0，无 panic/FATAL。#50/#90 均恢复为 `active + schedulable`，代理/fallback 为空，临时代理数量 0，抓包端口和进程无残留，CA 哈希恢复。

## 6. 抓包验收

### 6.1 OpenAI 标准与非 Lite

最终报告：

`/root/oauth-capture/runs/official-client/comparisons/review-fix-0.1.165-3-20260726T2217Z-v5/summary.json`

| 契约 | 应用层 | 语义守恒 | 未声明差异 | direct TLS |
|---|---:|---:|---:|---:|
| Codex HTTP，`gpt-5.6-luna`，S1/S2/S4 | true | true | 0 | true |
| Codex WS，`gpt-5.6-luna`，S1/S2/S4 | true | true | 0 | true |
| Codex HTTP 非 Lite，`gpt-5.4`，S2 双轮 | true | true | 0 | true |

官方与候选是独立运行，所以 `raw_equal=false`；这保留正文、动态身份、响应链、Header 顺序和动态工具目录的真实差异，不影响同次候选语义门禁。

### 6.2 Anthropic #50

官方 Claude Code 2.1.220 完整基准沿用同日成功的
`oauth-oauth-p0p2-zstd-final-20260726T1420Z`。本轮用 #50 重新请求时，官方 CLI 和 Sub2API 都收到 Anthropic `400 This organization has been disabled`。这排除了抓包器注入和路由差异，但也意味着无法取得成功响应。

当前实际 Sub2API 请求仍完成了两类可验证门禁：

- 应用层请求契约：`equal=true`、语义守恒、动态 beta 有效、未声明差异 0；
- direct TLS：官方/候选业务 Profile `equal=true`。

因此“代码与出站画像”通过，“Anthropic 功能成功响应”被外部账号状态阻塞。文档不得把该 400 写成成功。

### 6.3 compact 边界

Codex 0.145.0 的 `thread/compact/start` 实测发送带 `compaction_trigger` 的普通 `/responses`，以第二个 `turn/completed` 表示完成，并不发送 `/responses/compact`，也不产生 `thread/compacted`。这组 direct/MITM 控制面运行成功且 TLS 相等，但没有被冒充为 compact 端点官方基准。`/responses/compact` 的 `prompt_cache_key`、installation-id、无 `Accept`、无 `client_metadata` 等契约由专项代码测试验收。

## 7. 本地私有证据

完整原始证据已拉回：

`/Users/czs/Developer/sub2apiplus/local-analysis/captures/official-egress-review-fix-20260727-062447/`

目录含 561 个纳入哈希的文件，约 257 MB；包括完整 pcap、MITM JSONL、CLI 事件、日志、manifest/run-summary、比较报告、失败批次和部署前后 compose。目录权限为 0700、文件为 0600，并由 `.gitignore` 排除。

以实际测试 API Key、#50 access token、#90 access token 对远端和本地做精确值扫描均为 0 命中。启发式 `sk-*` 扫描命中的内容经 JSON 路径和哈希分类，为 WS `encrypted_content` 或官方插件网站 URL 子串，不是上述实际凭据。`SHA256SUMS` 覆盖除自身外的全部文件。

## 8. 最终判定

- P0、P1、P2 源码修复：**通过**。
- 本地完整测试、Vircs 构建和生产切换：**通过**。
- OpenAI HTTP/WS/非 Lite 应用层与 TLS：**通过**。
- Anthropic 出站应用层与 TLS：**通过**。
- Anthropic #50 成功业务响应：**未通过，外部账号组织被禁用**。

因此本轮不能写成“所有端到端功能均通过”。除 #50 上游账号状态外，其余已完成；恢复或更换用户指定的 Anthropic OAuth 账号后，只需重跑 Anthropic 成功响应门禁，无需重新编译当前镜像。
