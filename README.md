# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的自用增强版 fork。维护目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 项目状态

| 项 | 说明 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| 版本与差异 | 源码版本以 `backend/cmd/server/VERSION` 为准，已发布版本以 GitHub Releases 为准。自定义差异看最近一次合并的上游 tag 至 `HEAD`，合并点可用 `git log --oneline --grep='sync upstream'` 查到。 |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| 命名约定 | 对外使用 `sub2apiplus` / `Sub2API Plus`；主服务 Go module 和 import 保留 `github.com/Wei-Shaw/sub2api`，降低上游合并成本；keeper 为独立 module `github.com/itv3/sub2apiplus/keeper`，无上游对应物。 |
| Go / 客户端版本 | 主服务 `go 1.26.6`（`backend/go.mod`）；keeper `go 1.24`（`keeper/go.mod`）；keeper 固定 Claude CLI `2.1.210`，Codex 的 `CODEX_RELEASE` 当前仍为 `latest`，无缓存重建时记录实际安装版本。 |
| Docker 命名 | Compose service 保留 `sub2api`；默认容器名为 `sub2apiplus`、`sub2apiplus-postgres`、`sub2apiplus-redis`。 |

维护原则：

1. 长期跟随上游主线，发布版本按上游 release/tag 对齐。
2. Plus 账号级开关优先放入 `account.Extra`；mimic / 保活走 Plus 设置页，Antigravity 走账号编辑页；OAuth 官方客户端伪装为内置能力，不提供账号级开关。
3. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、反向代理配置、`.env` 和数据目录。

## 1. Plus 增强功能

管理后台侧边栏“Plus 增强功能”进入 `/admin/settings/plus-enhancements`，包含“API Key 官方客户端兼容”和“账号保活”两个 Tab；Antigravity 的模型、映射、官方伪装和计费配置位于账号创建/编辑页。

### 1.1 OAuth 官方客户端伪装

#### 1.1.1 目标与总结论

目标有两点：

1. 无论入站来自官方客户端还是第三方标准 API 客户端，Anthropic/OpenAI OAuth 官方出站均由内置画像统一定型为对应版本 Claude Code / Codex CLI 的真实形态(官方入站走校验保真，第三方走强制定型)。
2. 原有兼容层继续负责协议、模型、工具和请求语义转换；官方画像不改变 Key、Group、账号路由或计费归属。

本轮验收基于源码版本 `0.1.165-4`（已合并上游 `v0.1.165`），由 Vircs 直接编译并运行最终复核镜像；`active` 画像为 Claude Code CLI `2.1.220` 和 Codex CLI `0.145.0`。后续各轮的发布状态与证据范围见下表，逐路径结论表的“最后实证版本”列与本表对应：

| 版本 | 状态 | 本轮闭合 | 证据范围 |
|---|---|---|---|
| `0.1.165-4` | 阶段构建 | — | 三路径完整抓包对照，逐路径结论的原始来源 |
| `0.1.165-5` | 正式发布（Releases `v0.1.165-5`，镜像 `ghcr.io/itv3/sub2apiplus:0.1.165-5`），生产已切换为该镜像 | 数据保真、Lite 判定分裂、重复键 panic、API Key 画像 TLS 决策来源 | 未重跑三路径对照，逐路径结论沿用 `0.1.165-4`；历史过程材料已在规格收口后清理 |
| `0.1.165-6` | 未发布，仅由 Vircs 以阶段镜像验收 | 入站宿主头泄漏、WS turn-state 跨连接沿用、模型能力合并语义、Chat Completions/Messages 入口 JSON 保真、passthrough 预热缺失 | 只重跑 OpenAI 侧：官方 Codex CLI `0.145.0` 基准（HTTP/WS × direct/MITM × S1/S2/S4）与第三方定向 HTTP/WS 候选出站逐项对照通过，宿主头剥离与预热帧序列均有真实 wire 证据；turn-state 两项因上游始终未下发该头只达代码级验收。未重跑 Anthropic 三路径与 Kilo 六组合；历史过程材料已在规格收口后清理 |
| `0.1.165-8` | 未发布，仅由 Vircs 以阶段镜像验收 | h1 header 名全小写、compact 不压缩、models 请求纳入官方画像、未知顶层字段改白名单、三条旁路端点（count_tokens/images/alpha-search）纳入画像、WS turn-state 改从 `response.metadata` 事件提取 | 新建 h1 直连探针并取得首份 h1 wire 证据；当时规则与保留证据统一见 Codex 0.145.0 基线 |
| `0.1.165-9`·`-10` | 未发布，仅由 Vircs 以阶段镜像验收 | WS 握手头大小写修回 tungstenite 形态（前 5 项为大写驼峰，`0.1.165-8` 曾误压为小写）、h2 `MAX_HEADER_LIST_SIZE` 由 10MB 对齐到官方 16KB | **新建 h2 帧层探针**（CONNECT 代理 + h2 服务端），取得官方 SETTINGS/WINDOW_UPDATE/伪头顺序基线；mitmproxy 用自己的 h2 栈重建连接，看不到这些。残留差异见 §1.1.1.2 |
| `0.1.165-11` | 正式发布（Releases `v0.1.165-11`，镜像 `ghcr.io/itv3/sub2apiplus:0.1.165-11`），Vircs 已切换为该镜像 | 官方 OAuth 定型层按版本画像重建：Codex `0.145.0` 完整画像、稳定执行引擎、文件直传、OAuth、turn-state 隔离及失败关闭验收链 | Codex 0.145.0 的规则来源、42 项范围、客户端仿真实现、源码改动台账和升级流程统一见[客户端仿真与版本演进手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)；官方侧逐项证据路径由[证据索引](docs/EVIDENCE_INDEX.md)生成。发布状态本身不替代绑定具体源码树、镜像和画像摘要的 Campaign 验收。 |
| `0.1.165-12` | 正式发布（Releases `v0.1.165-12`，镜像 `ghcr.io/itv3/sub2apiplus:0.1.165-12`） | 入站身份失败关闭改为投影出站：删除 5 个拒绝点（UA 不匹配 surface、originator 不匹配、turn metadata 解析失败、subagent 校验失败、parent thread 冲突），官方 `0.146` 与非 Ubuntu 平台的官方 `0.145.0` 客户端不再被拒；新增版本快照注册表与 release 指针取版本；补齐投影降级与 body 闭集丢弃字段的可观测信号 | **画像摘要 `9b7dd12d…` 未变，出站形态不变**，本轮只改变失败关闭策略与代码结构，未重跑候选 Campaign。新增两道实现侧门禁（版本标识符泄漏基线、§3.5 台账完整性复算）与逐调用点终态定型配对检查；§3.5 台账补登 10 个此前漏登的出站定型文件，统计口径由单提交改为冻结上游基线累计。按 §4.1，源码树变化严格上仍应有候选验收，本次发布未执行 |

`0.1.165-6` 的宿主头剥离清单由三条路径共用，但只在 OpenAI HTTP/WS 侧取得 wire 证据；Anthropic 侧该项已无同版本证据，只能按共享逻辑外推，按 §1.1.2.3 的版本边界须重抓后才能计入实测结论。

解读逐路径结论时注意两条边界：

1. “通过”指应用层契约一致、语义守恒和 direct TLS 一致，不是逐字节相同；完整判定口径见 §1.1.2.3。
2. Anthropic 侧因官方上游返回 `400 organization disabled`（#50 组织禁用），本轮用单次真实第三方请求完成 system、动态 beta、应用契约和 direct TLS 验收，未执行 §1.1.2.2 要求的完整 S1/S2/S4，也不能宣称功能响应成功。

| 路径 | 最后实证版本 | 验收级别 | Sub2API 实证（OAuth 官方画像内置生效） |
|---|---|---|---|
| **Anthropic HTTP** | `0.1.165-4` | 画像/传输 | 三块 system 与 `custom.defer_loading` 真实第三方样本达到 `contract_equal=true`、语义守恒、动态 beta `exercised/valid=true`、`undeclared_differences=0`；direct TLS 一致；#50 上游组织禁用导致业务响应失败，削弱项见上方边界 2 |
| **OpenAI HTTP** | `0.1.165-6` | 完整功能 | Lite S1/S2/S4、非 Lite `gpt-5.4` 及真实第三方破坏参数样本均 `contract_equal=true`、语义守恒、`undeclared_differences=0`；Cookie 建立/回放闭环与 direct TLS 通过；`0.1.165-6` 重跑另覆盖宿主头剥离的真实 wire 证据 |
| **OpenAI WebSocket** | `0.1.165-6` | 完整功能（turn-state 仅代码级） | Lite S1/S2/S4、非 Lite 完整预热/业务帧及真实第三方业务帧均通过；逐项 turn metadata、语义守恒、契约和 direct TLS 一致；`0.1.165-6` 重跑另覆盖宿主头剥离与预热帧序列，turn-state 注入因上游始终未下发该头只达代码级验收 |

验收级别按 §1.1.2.3 的结论分级判定：“画像/传输”只验证路由、认证、静态 Header、必要 Body 外层字段和 TLS；“完整功能”另要求同模型、同场景输入并成功得到业务响应。

API Key active 画像的 AnyRouter A/B 见 §1.2。

##### 1.1.1.1 官方画像的功能守恒约束

官方画像必须只补齐客户端身份与上游协议约束，不能以“更像官方客户端”为由删除模型能力或改写调用方已明确表达的语义。当前实现遵循以下约束：

| 主题 | 约束 |
|---|---|
| **入站宿主头** | 三条路径在终态定型阶段统一剥离 `accept-language`、`sec-fetch-mode`、`x-stainless-helper-method`。这些头由浏览器或 IDE 宿主注入，官方 Claude Code / Codex CLI 从不发送；入站白名单会放行它们，不剥离就会与官方身份头出现在同一请求上，形成官方客户端不可能产生的混合形态。定向抓包的第三方客户端必须实际携带这些头，否则该约束在证据里不会被触发。`0.1.165-6` 的剥离实现由三条路径共用，但只在 OpenAI HTTP/WS 侧取得 wire 证据；Anthropic 路径当前是共享逻辑外推，按 §1.1.2.3 的版本边界须重抓后才能计入实测结论。 |
| **Anthropic 参数与 beta** | `anthropic-beta` 按请求能力动态补齐，保留 1M context、structured outputs 和 advanced tool use/tool search 所需 beta；启用 thinking（或入口本就未携带）时删除会导致上游 `400` 的 `temperature`；`top_p`、`top_k` 因官方客户端不发送而一律清理，不受 thinking 影响。端点判定统一使用规范化入口，`/messages`、`/v1/messages` 及兼容别名不能绕过画像。 |
| **Codex 模型能力** | 账号 `/backend-api/codex/models` 清单的合并语义对齐官方 `apply_remote_models`：清单非空且至少含一个 `visibility=list` 的模型时远端整体接管，bundled 不再参与，清单未列出的 slug 按官方 fallback 处理（`use_responses_lite` 与 `supports_parallel_tool_calls` 均为 `false`，且属于“已知不支持”而非“未知”）；只有不满足接管条件、或清单从未成功加载时才用 bundled 兜底。能力按“账号 + 精确模型 slug”缓存，加载失败进入退避并沿用当前有效值，不同步拒绝业务请求。bundled 的权威来源是实抓 manifest fixture，两者必须逐项一致。Lite 才迁移 `instructions/tools` 并设置 `reasoning.context=all_turns`；非 Lite 保留顶层 `instructions/tools` 且不发送 `reasoning.context`。两类路径共同固定 `tool_choice=auto`、`store=false`、`stream=true`、`include=[reasoning.encrypted_content]` 并删除 `max_output_tokens`；`parallel_tool_calls` 按能力位决定。 |
| **compact** | compact 请求补齐 `prompt_cache_key` 与 `X-Codex-Installation-ID`，不要求或生成普通 Responses 才有的 `x-client-request-id`。 |
| **OAuth 刷新身份** | Codex token exchange/refresh 使用与 active 画像同版本的 `codex_exec` User-Agent（具体串以画像常量为准），并携带官方 `originator`。请求体形态两者不同：refresh 使用官方 JSON 契约，exchange 仍按官方形态使用 form-encoded。两者的 token 端点形态均来自官方源码，当前抓包证据不覆盖 token/usage 端点。 |
| **会话状态** | 不再生成或注入私有 `cc_prev_req`；客户端提供的 `x-codex-turn-state` 被拒绝。HTTP 的 `turn_started_at_unix_ms` 按账号/会话/turn 保存首次真实时间；WS 仅在握手响应实际下发 turn-state 时注入同连接后续 `client_metadata`，不跨连接回放。Codex 0.145.0 的官方与候选 wire 均未观察到该状态。 |
| **Cookie** | OAuth HTTP 账号使用按“账号 + 代理”隔离的内存 Cookie jar，只接受 HTTPS ChatGPT 域名的 Cloudflare allowlist Cookie；模型清单复用同一策略。WS 不接入 Cookie jar，不发送 Cookie。 |

验收所用的阶段构建镜像、基准与对比运行目录、回归时间戳统一见[实证归档](backend/internal/service/testdata/official_egress/README.md)；阶段构建不对外发布，命名规则见 §3.4。

##### 1.1.1.2 已知的 wire 层残留差异

上表的"通过"是应用层契约与 direct TLS 一致，**不含 wire 层逐字节**。截至
`0.1.165-10`，以下差异**仍然存在且已量化**，据此不能声称与官方出站"完全一致"：

| 差异 | 官方 | Sub2API | 影响路径 | 阻塞点 |
|---|---|---|---|---|
| h1 header 顺序与 `Host` 形态 | 全小写，`host` 倒数第二、`content-length` 最后 | `Host` 大写在最前，其余字典序 | **直连，默认主路径** | Go 的 `Request.write` 硬编码，须自写 h1 请求侧字节 |
| WS 握手头顺序 | tungstenite 固定 5 项 + 插入序 | Go 字典序 | WS | 同上（大小写已于 `0.1.165-9` 修齐） |
| h2 `INITIAL_WINDOW_SIZE` | 2 MB | 4 MB | 仅代理 | utls 与 net/http 的 h2 升级路径不兼容 |
| h2 首个 `WINDOW_UPDATE` | 5,177,345 | 1 GB | 仅代理 | 同上 |
| h2 伪头顺序 | `:method,:scheme,:authority,:path` | `:authority,:method,:path,:scheme` | 仅代理 | 硬编码于 `x/net/internal/httpcommon` |

已对齐的 wire 层项目同样记录在案：h1 header 名全小写（实测 17 头中 15 项）、h2 SETTINGS
帧内顺序与 `ENABLE_PUSH`/`MAX_FRAME_SIZE`（本就一致）、`MAX_HEADER_LIST_SIZE`、WS 握手
前 5 项大写驼峰。

**一条必须记住的验证限制**：官方并非处处小写——普通 HTTP（hyper）全小写，但 WS 握手
（tungstenite）前 5 项是大写驼峰。照"全小写"一刀切会制造新偏离，`0.1.165-8` 曾因此
引入回归并在 `0.1.165-9` 修回。

官方形态的逐条规则、证据等级、观测通道、实现和升级流程统一见
[Codex CLI 客户端仿真与版本演进手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)。

#### 1.1.2 抓包方法、分组与证据边界

> **抓包与升级操作手册**：Codex CLI 0.145.0 的证据生成、抓包、复算、验收和升级流程统一见[客户端仿真与版本演进手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)；[工具目录 README](tools/official_client_capture/README.md)只保留入口导航。抓包验证环境在本文统称 Vircs。

本地分析资料统一放在 `local-analysis/`（已被 `.gitignore` 整目录忽略，不进 Git，新环境需自行下载准备）：

```
local-analysis/
├── captures/    # 抓包与验收证据（运行目录、MANIFEST、报告）
└── sources/     # 官方客户端源码对照，目录名标注版本
```

使用 `sources/` 前必须与 §1.1.1 的 active 画像版本核对：版本一致时源码可作画像依据；不一致时基于源码的结论只能作外推线索。

客户端版本升级时必须分别执行两套任务，`all` 只负责顺序编排，不能把认证状态或证据合并：

1. **OAuth 任务**：Claude Code / Codex CLI 使用 OAuth 直连 Anthropic / OpenAI 官方平台，为 §1.1 建立基准。
2. **API 任务**：同版本 Claude Code / Codex CLI 使用 Sub2API 访问 Key，通过 Sub2API 公共 HTTPS Base URL 连接 Sub2API，为 §1.2 建立基准；禁止用本机明文 reverse ingress 的 pcap 代替 CLI 到 Sub2API 的真实 TLS。

必须区分以下三个流量边界：

1. **API 基准**：官方 CLI → Sub2API 公共 HTTPS 入口，用于提取同版本 CLI 的 API Key 请求画像。
2. **外部官方参考**：官方 CLI → AnyRouter 等目标站点，记录该目标实际接收的官方请求。
3. **候选出站**：非官方客户端 → Sub2API → 同一目标站点，在目标边界对比第 2、3 条流量。

第 1 条用于生成画像，但不能替代第 2 条；外部站点 A/B 是独立验收阶段，不属于基准抓包任务。

##### 1.1.2.1 抓包方法：两轮抓取法 + 三场景

同一“任务 + 主体 + 传输 + 场景”分别执行 direct 与 mitm，不能用其中一种证据代替另一种。双任务工具中，每个 S1/S2/S4 都单独启动抓包进程并写独立文件：

| 轮次 | 抓取方式 | 验证内容 | 不能证明 |
|---|---|---|---|
| **direct** | 在被测客户端或后续候选网关自身网络命名空间，通过 `tcpdump` 抓取未终止 TLS | ClientHello、cipher、扩展顺序、曲线、签名算法、版本、KeyShare、PSK 和客户端 offered ALPN | 加密后的 Header/Body/WS 帧；服务端协商结果；仅靠规范化结果不能断言连接复用 |
| **mitm** | 只给当前被测进程注入测试代理与 CA，记录目标 allowlist 流量 | URL、HTTP 版本、Header、Body 结构、响应及 WS 帧 | 未插入 MITM 时的真实 TLS 指纹 |

每轮使用相同模型和相同场景：

- **S1**：冷启动简单问答，验证基础请求、响应和身份字段。
- **S2**：同一会话连续多轮，验证会话生命周期和 `previous_response_id`；连接复用如需形成结论，另行分析原始 pcap。
- **S4**：完整工具闭环，验证工具定义、调用、结果回传、`call_id` 和最终回答。

S3 为历史编号，当前场景集不使用。

执行要求：

1. 同一对比组使用相同客户端版本、模型、场景输入和重复次数；具体值写入运行元数据，本节不重复固定数值，验收基线以 §1.1.1 为准。
2. 一次只运行一个“任务 + evidence + 主体 + 传输 + 场景”，避免连接池、会话、认证状态和抓包文件互相污染。
3. direct 不得插入 MITM；mitm 只保存本单元目标边界的解密数据。Sub2API ingress 与候选出站配对属于后续 A/B，不得混进官方 CLI 基准文件。
4. 每轮记录客户端版本与哈希、模型、Profile、目标、客户端 offered ALPN、开始时间、运行目录和清理结果。容器 image digest 若由调用方声明，必须明确标注未在容器内独立验证。
5. 测试结束后清理本工具启动的抓包/CLI 进程并确认 MITM 端口释放；代理、CA 和 API 状态只注入子进程/专用目录，不修改全局账号或服务配置。

##### 1.1.2.2 抓包分组与动作

分组编号描述固定的比较关系：`AH` 表示 Anthropic HTTP，`OH` 表示 OpenAI HTTP，`OW` 表示 OpenAI WebSocket；后缀 `1` 表示官方客户端基准，`2` 表示 Sub2API 候选出站。客户端版本、模型、账号和运行目录由每次运行元数据确定。双任务工具只生成 `1` 类基准；`2` 类候选必须在后续 A/B 单独抓取。

| 平台与路径 | 官方客户端基准 | Sub2API 出站 | 对比关系 |
|---|---|---|---|
| **Anthropic · HTTP** | AH-1（Claude Code） | AH-2 | AH-2 对标 AH-1 |
| **OpenAI · HTTP** | OH-1（Codex CLI） | OH-2 | OH-2 对标 OH-1 |
| **OpenAI · WS** | OW-1（Codex CLI） | OW-2 | OW-2 对标 OW-1 |

主基线的三组路径均执行相同的 S1/S2/S4；第三方客户端兼容性补测可以选取子集，
但不能替代主基线，Kilo 六组合的覆盖范围与历史回归结果见 §1.1.3.4：

| 主体类型 | direct 动作 | mitm 动作 |
|---|---|---|
| **官方客户端基准** | 在 CLI 网络命名空间抓取 OAuth→官方平台或 API Key→Sub2API 公共 HTTPS 流量 | 只给 CLI 子进程配置测试 CA 与代理，解密同一目标边界 |
| **Sub2API 候选（后续 A/B）** | 在 Sub2API 网络命名空间抓取到官方平台或第三方 API 站点的出站流量 | 只解密 Sub2API 候选出站；如需证明改写关系，另存并关联对应 ingress 证据 |

HTTP 路径只能对标对应的官方 HTTP 基准，WebSocket 路径只能对标官方 WebSocket 基准。协议、模型或场景不同的样本只能用于辅助观察，不能计入一致性结论。

##### 1.1.2.3 样本与证据边界

- **证据分层**：传输层结论只用 direct pcap，应用层结论只用 mitm 解密记录，两者的能力与盲区见 §1.1.2.1；自动规范化输出不把 offered ALPN 当作协商结果。
- **Sub2API 改写证据**：在后续候选 A/B 阶段保存同一次请求的 ingress 和出站关联，比较规范化语义。
- **样本有效性**：必须确认命中指定账号和模型、没有其他账号 fallback，并能与 usage/请求日志对应。功能验收还要求请求成功完成；若目标站点返回已确认的容量或配额错误，只能用于画像/传输验收，不能证明端到端业务成功。
- **续轮与工具**：S2 必须形成真实连续会话；S4 必须完成“工具定义 → 工具调用 → 工具结果 → 最终回答”，发出即失败的样本无效。
- **版本边界**：每次结论只适用于运行元数据记录的客户端、服务镜像和 Profile；版本变化后必须重抓，不能直接沿用旧数值。
- **行为层不作对比维度**：官方客户端原生支持合法关闭遥测与非必要流量，因此候选出站“零遥测”是官方允许的配置状态，不能作为“不像官方客户端”的判据，也不计入一致性差异。Codex 侧由 `codex-rs/config/src/types.rs` 的 `AnalyticsConfigToml.enabled=false` 关闭 analytics events，otel exporter 可配 `None` 关 Statsig metrics，该结论基于与 active 画像同版本的 `codex-cli-0.145` 源码。Claude 侧观察到 `DISABLE_TELEMETRY` 经 `isAnalyticsDisabled()` 关闭 Datadog 与 1P `event_logging`（`src/services/analytics/`），`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` 经 `isEssentialTrafficOnly()` 门控 mcp-registry / policy_limits / grove / releaseNotes / feedback / modelCapabilities / referral 等十余处非必要请求（`privacyLevel.ts` 注释即 “All nonessential network traffic disabled”）。
- **安全边界**：Token、Cookie、API Key 和动态身份值必须脱敏；包含完整请求 Body 的原始样本按敏感材料管理。
- **比较口径**：先保存原始严格差异，再按路径契约声明跨独立运行必然变化的正文、动态身份、Header 顺序、动态工具目录及 Codex 运行时 Cookie；只有 `contract_equal=true`、候选 ingress→egress 语义守恒且 `undeclared_differences=0` 时，画像契约才通过。不得以 `raw_equal=false` 单独判失败，也不得用声明规则掩盖候选自身语义丢失。
- **结论分级**：画像/传输验收验证路由、认证、静态 Header、必要 Body 外层字段和 TLS；完整功能验收还要求同模型、同场景输入并成功得到业务响应。文档必须明确报告所达到的级别。

#### 1.1.3 OAuth 改造方案与实施结果

> **基线与证据**：本节沿用 §1.1.1 的实测基线，证据判定口径见 §1.1.2.3；详细请求计数、字段差异、TLS 数据、运行编号和恢复记录见[实证归档](backend/internal/service/testdata/official_egress/README.md)。Profile 目标随官方客户端版本更新；不得把单个 JA3、动态身份值或完整 system 文本写成永久常量。

本轮改动摘要：总体架构方案未变，`0.1.165-4` 在 `0.1.165-3` 的数据保真、能力降级、compact 身份、token TLS 和辅助身份基础上，补齐 Lite/非 Lite 终态定型、顶层 instructions、并行能力位、三块 system、动态 beta 与真实第三方抓包门禁。

##### 1.1.3.1 方案结论与边界

1. **范围不变**：复用现有 composite 路由、鉴权、账号调度、重试、流式响应、usage、会话粘性和代理系统；官方客户端伪装只修正三条 OAuth 官方出站路径，不改变 Key、Group、模型路由或计费归属。
2. **薄层实现**：在现有协议转换完成后增加统一的 Profile Resolver、`OfficialEgressContext`、Finalizer 和 Transport/Dialer Provider，不分叉完整 Handler。
3. **最小改写与数据保真**：保留入口已经携带且上游支持的语义，只修正抓包已证明不一致的字段；顶层定型不得通过 `map[string]any` 重建并改写嵌套用户 JSON。不得合成官方内部工具、复制完整 system 或无依据展开历史。Lite 能力位要求的结构转换属于受测协议约束（具体字段见 §1.1.1.1），非 Lite 不沿用。
4. **应用与传输同时对齐**：Anthropic HTTP、OpenAI HTTP、OpenAI WebSocket 分别维护独立 Profile，同时约束 Header/Body、TLS/ALPN、协议选择和连接复用。
5. **身份与失败边界明确**：动态身份必须来自入口、会话、响应映射或账号配置等可追踪来源。API Key 与其他非适用账号保持原行为；OAuth Profile、Host 或身份状态校验失败时必须明确失败并记录脱敏原因，不能静默切换画像。

##### 1.1.3.2 三条路径的改造前缺口与结果

| 路径 | 改造前主要缺口 | 应用层结果 | 传输层结果 |
|---|---|---|---|
| **Anthropic HTTP** | 使用了错误的客户端 UA、HTTP/TLS 画像和固定会话身份；system/cache 结构未按 OAuth 客户端形态处理 | 修正 Claude Code UA、动态 Beta/BetaPolicy、Header、system/cache 结构及 session/metadata 生命周期；识别顶层与 `custom.defer_loading`；只重排顶层并保留嵌套原始 JSON | 使用独立 Claude HTTP Profile，对齐官方 HTTP/1.1、TLS/ALPN 和连接行为 |
| **OpenAI HTTP** | 自动注入无依据的 `instructions`，改写工具 `call_id`；Header 与 Body 身份脱节，传输画像不符 | 取消无依据注入，保留 `call_id` 和入口语义；按能力位区分 Lite 与非 Lite 的结构转换；能力合并、Cookie、zstd/version、compact 和 token TLS 形成独立契约 | 使用独立 Codex HTTP Profile，并按代理边界选择对应传输画像 |
| **OpenAI WebSocket** | 有效 `previous_response_id` 被丢弃并展开历史；续轮重复工具字段，握手身份和 WS Dialer 不符 | 保留有效续链和入口 WS 帧语义；Lite 与 HTTP 共用终态定型；服务端能力优先于客户端自报 | 使用专用 WS Dialer，对齐握手 Header、压缩协商、TLS/ALPN 和重连边界 |

三条路径还共同存在动态字段来源不统一、不同账号/Profile/代理/CA 可能错误复用连接的问题，必须由公共上下文和连接池键统一解决。

Profile 只在入口端点受支持，且目标平台、OAuth 账号、实际出站协议和官方 Host 均匹配时生效。

##### 1.1.3.3 实际采用架构

```text
选定目标平台与 OAuth 账号
→ OfficialEgressProfileResolver
→ OfficialEgressContext（身份值、来源、生命周期）
→ HTTP / WebSocket Finalizer
→ 最终校验
→ 路径专用 Transport / WS Dialer
→ 官方上游
```

| 路径 | 最终出站组件 |
|---|---|
| Anthropic HTTP | Claude Finalizer + Claude HTTP Transport |
| OpenAI HTTP | Codex HTTP Finalizer + Codex HTTP Transport |
| OpenAI WebSocket | WS Handshake/Frame Finalizer + Codex WS Dialer |

运行规则：

1. HTTP 重试切换账号时重新构建上下文；WebSocket 握手成功后冻结账号和身份，切换时必须重新连接。
2. 连接池键同时包含账号、Profile 版本、协议、Host、代理、CA 和 TLS Profile，禁止跨画像复用。
3. Profile 常量集中按版本维护；动态字段只保存生成规则和来源，不保存完整抓包值。
4. 模型能力并发加载单飞，不能因辅助 `/models` 失败同步拒绝业务请求；合并与降级规则见 §1.1.1.1。

##### 1.1.3.4 工程验收结果

三条路径的画像契约结论见 §1.1.1，本表记录工程维度的验收：

| 验收项 | 完成结果 |
|---|---|
| **路由与账务** | 三条路径命中正确平台 OAuth 账号，重试、usage 和计费归属正确；Profile 不改变 Key/Group 权限 |
| **三条路径的实现层语义** | Anthropic 的 system/动态 beta 语义、OpenAI HTTP 的入口语义保真与 Cookie 生命周期、WebSocket 的逐项 turn metadata 与真实无 turn-state 基准均通过 |
| **真实第三方定型** | 非 Codex HTTP 与原始 RFC 6455 WS 实际发送 `tool_choice=required`、`max_output_tokens=123`、错误 store/stream/include/context；出站固定字段全部回到官方非 Lite 契约，顶层 instructions/tools 与业务 reasoning/text 守恒。Anthropic 三块 system 与 `custom.defer_loading` 也由真实请求触发，比较结果 `all_passed=true` |
| **历史 Kilo 六组合** | 六组合曾以真实 Kilo 界面完成 S1 与 S4 读文件工具闭环；`0.1.164-6` 另补 OpenAI Responses → OpenAI WS 的 S2。该矩阵是历史协议转换回归，不是 `0.1.165-4` 本轮新证据 |
| **模式独立性** | 画像由**实际出站协议**决定，不由账号配置直接决定：HTTP 入站恒走 HTTP 出站与 HTTP 画像，账号 WS mode 不参与；只有 WebSocket 入站时 mode 才生效——`ctx_pool` 与 `passthrough` 走 WS 出站与 WS 画像，`http_bridge` 转 HTTP 出站与 HTTP 画像，`off` 直接拒绝该连接而不是降级。全局 `mode_router_v2_enabled` 关闭时固定按 `ctx_pool` 处理。自动透传开关同样不绕过 Profile。两种 WS 出站 mode 都会为第三方入站补出 `generate=false` 预热往返并把业务帧挂到预热响应上，帧序列一致 |
| **适用边界** | Anthropic/OpenAI OAuth 自动生效；API Key、其他平台及非模型入口保持原行为 |
| **安全与回归** | 敏感值未进入普通日志；Host、代理和重定向边界受控；全量、竞态、性能及合并演练通过 |

第三方客户端回归矩阵：

| Kilo 入站格式 | 目标上游 | 最终出站画像 | 结果 |
|---|---|---|---|
| OpenAI Responses | OpenAI | Codex HTTP/WS Profile（实际为 WS） | `0.1.164-6` 真实 Kilo S1/S2/S4 通过，S4 完成一次真实读工具闭环 |
| OpenAI Compatible | OpenAI | Codex HTTP Profile | 真实 Kilo S1/S4 通过 |
| Anthropic | Anthropic | Claude HTTP Profile | 真实 Kilo S1/S4 通过 |
| OpenAI Responses | Anthropic | Claude HTTP Profile | 真实 Kilo S1/S4 通过 |
| OpenAI Compatible | Anthropic | Claude HTTP Profile | 真实 Kilo S1/S4 通过 |
| Anthropic | OpenAI | Codex HTTP Profile | 真实 Kilo S1/S4 通过 |

以上是六种协议转换与路由组合，不是六套伪装实现。以后修改入站协议转换、模型/账号路由、Finalizer 或 Transport 时必须重跑受影响组合；修改公共 Resolver 或目标平台公共出站链路时必须重跑全部六种组合。

2026-07-27 的最终复核任务没有重跑 Kilo；本节 Kilo 表仅保留历史回归结果，不能作为
`0.1.165-4` 的本轮新证据。当前 Codex 0.147.0 范围、证据和验收边界统一见
[客户端仿真与版本演进手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)。

##### 1.1.3.5 编码任务分解（已完成）

T1–T7 已按“契约测试 → 公共 Profile/Context → Transport 隔离 → 三条路径 Finalizer → 回归与抓包”的顺序完成；N2 已完成 Kilo 六种“入站协议 × 目标上游”组合和 OpenAI WebSocket 历史归一化。

### 1.2 API Key 官方客户端兼容

API Key 官方客户端兼容让 Kilo / Cline / Cursor / Roo Code 等非官方客户端尽量接近 Claude Code / Codex CLI 在 **API Key 认证模式**下的 header、body、TLS 和路由形态。OAuth 和 API Key 复用 registry 中经抓包证明一致的 `ClientBuild` 与传输数据（与 `pkg/claude` 的包级共享常量是两回事，后者的边界见 §4.3），但认证、端点、beta、billing、cache 和 WS 开关仍是独立 Profile，不会把 OAuth 最终请求整体复制给 API Key。

本节的抓包方法、两轮抓取法、样本有效性、比较口径和结论分级沿用 §1.1.2。API 基准由双任务工具的 API 任务生成，使用与 OAuth 相同版本的 Claude Code / Codex CLI 以 API Key 模式连接 Sub2API；后续升级可由一次自动化任务顺序覆盖两种认证、端点、HTTP/WS、direct/MITM 和 S1/S2/S4，但每个场景仍生成独立证据与 Profile。外部站点 A/B 属于 §1.1.2 定义的独立验收阶段。

默认画像已直接切换为 CLI 目标（版本见 §1.2.2），旧 Desktop 画像仅作为服务级 `previous` 回退保留，见 §1.2.4。不设账号级灰度。

#### 1.2.1 生效范围与运行规则

1. 仅 Anthropic / OpenAI 的 API Key 账号生效，不改变 OAuth 账号逻辑。
2. mimic 与 passthrough 运行时互斥；同时开启时，非官方客户端优先走 mimic。
3. 账号测试按非官方入站请求处理并走与正式 Gateway 相同的 Profile 解析、Header/Body Finalizer 和 TLS 决策；`active` 与 `previous` 两种模式都由同一份请求级画像同时决定 Header/Body 与 TLS，不存在按模式分叉的独立判定。
4. 非官方客户端命中 mimic 时，关键身份 header 不允许被账号级 header override 覆盖；官方客户端跳过 mimic 后不应用该保护。
5. OpenAI Codex mimic 的 `/v1/messages` 固定进入 Responses mimic 链路，不受 `force_chat_completions`、普通 Responses probe false 或 `openai_responses_supported=false` 影响。
6. OpenAI Codex mimic 只支持 HTTP/SSE 上游，命中时不进入 Responses WebSocket（WS 画像在 registry 中只存档不激活）；跳过 mimic 的官方 Codex 客户端按普通 API Key 账号的全局/账号 WS 开关、force HTTP 和 WSv2 mode 选择路由。该判断同时作用于账号调度、粘性账号复核和最终转发。
7. 官方客户端识别只依据 Gateway 入站请求上下文（UA、`originator`、`metadata.user_id`）。没有入站上下文的内部入口不做该识别，因此不会因为实际发起方是官方 CLI 而跳过 mimic；keeper 的 OpenAI 出口是另一回事，它按设计就不走 mimic 链路，而不是被识别成官方客户端后跳过。
8. 管理后台账号测试在 HTTP 200 且流以 EOF、`[DONE]` 或 `message_stop` 任一路径正常结束时判成功；非 200、或正文命中服务暂不可用降级文案时失败并保留上游错误。

三个入口的实际行为：

| 入口 | 客户端识别 | Anthropic | OpenAI |
|---|---|---|---|
| Gateway 公网入口 | 按 UA / `originator` / `metadata.user_id` 识别 | 官方客户端跳过 mimic | 官方客户端跳过 mimic |
| 管理后台账号测试 | 不识别（传入空 Gin Context） | 走 Claude Code CLI API Key 画像 | 走 Codex CLI API Key 画像 |
| keeper 内部代理 | 不识别 | `/v1/messages` 开关打开即走 active 画像 | 不做 mimic，按 header allowlist 透传官方 Codex CLI 形态 |

```json
{"anthropic_apikey_mimic_claude_code":true,"openai_apikey_mimic_codex_cli":true,"enable_tls_fingerprint":true}
```

两个开关名与 CLI 目标一致。

#### 1.2.2 active 画像与 Profile Registry

| 平台 | mimic 目标 | 出站行为 |
|---|---|---|
| Anthropic API Key | Claude Code CLI `2.1.220` | `/v1/messages` 发送 `x-api-key` 且移除 Bearer 认证；UA、Linux/x64、Node `v26.3.0`、beta、`sdk-cli` billing、system 与 cache 形态来自 API 抓包。`/count_tokens` 使用独立 generic Contract，不冒充 messages 官方样本。 |
| OpenAI API Key | `codex_exec/0.145.0` | 默认 profile 为 `codex_exec_0_145`，发送 `originator=codex_exec`、`remote_compaction_v2`、`responses-lite=true`，不发 `OpenAI-Beta` 和 `version`；metadata 使用 `sandbox=seccomp`，`parallel_tool_calls=false`。 |

Profile 数据集中在 `official_client_profile_registry.go`，每个 Wire Profile 都包含不可变 ID、抓包来源、适用端点、传输画像和 SHA-256 digest。服务级指针由 `gateway.official_client_profiles.mode` 单独控制，只允许 `active`/`previous` 整体切换，未知模式或不完整画像会 fail-closed。历史账号字段 `openai_apikey_mimic_codex_profile` 仅作 dormant 数据保留，运行时不再覆盖服务级指针。

**Anthropic 1M beta**：1M 自动补全只作用于开启了 API Key 官方客户端兼容的
API 账号构造链——按最终映射模型自动补 `context-1m-2025-08-07`，不要求
Kilo / Cline / Cursor / Roo Code 自己发送。OAuth / SetupToken 官方出站是独立链路，
官方 CLI 默认流量不携带该 beta，因此不做任何 1M 自动补全。适用模型前缀为
`claude-opus-4-8`、`claude-opus-5` 和
`claude-fable-5`；其他模型不凭空添加。补齐后的顺序与 Claude Code `2.1.220 --betas context-1m-2025-08-07`
实抓请求一致：`context-1m-2025-08-07` 位于 `effort-2025-11-24` 之前，且不会重复。
默认 BetaPolicy 不得过滤这项身份 beta；`structured-outputs-2025-12-15` 等按 Body
动态触发的功能 beta 仍受 BetaPolicy 控制。

#### 1.2.3 AnyRouter A/B 验收结果

2026-07-26 已使用 AnyRouter Anthropic #15 / `claude-opus-4-8` 和 OpenAI #97 /
`gpt-5.6-sol`，分别比较“官方 CLI 直连 AnyRouter”与“非官方标准 API 入站 →
Sub2API API Key mimic → AnyRouter”两条 HTTP 出站路径：

| 验收项 | Anthropic HTTP | OpenAI HTTP |
|---|---|---|
| 路由与协议 | `POST /v1/messages?beta=true`、HTTP/1.1 一致 | `POST /v1/responses`、HTTP/2 一致 |
| 静态身份 | Claude Code `2.1.220` UA、Stainless、`x-app`、1M beta 及顺序一致 | `codex_exec/0.145.0` UA、`originator`、Responses Lite、beta features 一致 |
| direct TLS | 规范化 ClientHello 严格相等；官方 2 次、候选 1 次观察归一为同一画像 | 规范化 ClientHello 严格相等；官方 6 次、候选 1 次观察归一为同一画像 |
| 外部结果 | 管理端账号测试和候选 MITM 样本均曾返回 200；顺序修复后的重复请求遇到 AnyRouter 瞬时 520 | 官方与候选均进入已确认的 `gpt-5.6-sol` 负载上限分支；未出现 `client_restricted` |
| 验收级别 | 画像/传输契约通过；已有业务成功样本，但修复后同轮完整功能 A/B 尚待上游稳定时复验 | 画像/传输契约通过；端到端业务响应因模型负载上限尚未证明 |

验证效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。表中验收级别按 §1.1.2.3 的结论分级判定。入站对话、工具 Schema、max tokens、thinking/effort 和完整 CLI 运行上下文由调用方语义决定；mimic 只统一路由、认证、官方静态身份、必要 Body 外层字段和 TLS 画像。私有原始证据位于 Vircs `/root/oauth-capture/runs/anyrouter-ab-wire-20260726T072501Z`，脱敏结论为 `analysis/apikey-anyrouter-ab-contract.json`。

#### 1.2.4 previous 回退画像

`previous` 仅用于服务级紧急整体回退，不是默认出站契约，也不得与 `active` 按字段混用：

| 平台 | previous 画像 | 与 active 的关键差异 |
|---|---|---|
| Anthropic | Claude Desktop `2.1.209` | `claude-desktop-3p` billing、Desktop beta/system/工具集合；未指定自定义 TLS Profile 时使用标准 Transport |
| OpenAI | Codex Desktop `0.144.0-alpha.4` | Desktop UA、Header/Body 和传输契约；未指定自定义 TLS Profile 时使用标准 Transport；API Key WS 同样不激活 |

Profile ID 归属：OpenAI 默认指针为 `codex_exec_0_145`（见 §1.2.2），`desktop_0_144` 及其旧别名仅属于 `previous`/配置兼容，`cli_rs_0_125` 保留独立历史兼容路径。

`v0.1.155-3` 的 AnyRouter ARM64 结果只证明 Anthropic Desktop `previous` 画像，不能作为 CLI `active` 画像的证据。旧字段、完整请求契约、工具列表和历史运行记录统一保存在[实证 README](backend/internal/service/testdata/official_egress/README.md)。

#### 1.2.5 发送与回退契约

OpenAI `/v1/responses/compact` 是特例：上游保持官方 unary JSON 形态，请求体按白名单重建，只保留 `model`、`input`、`instructions`、`tools`、`parallel_tool_calls`、`reasoning`、`text`、`service_tier`、`previous_response_id` 九个字段，其余（含 `stream`、`store`、`prompt_cache_key`、`client_metadata`）一律不带出；不补 Codex mimic body 默认值，并强制 `Accept: application/json`。该精简发生在入站规范化阶段，OAuth 路径随后由官方画像补回 `prompt_cache_key` 与 `X-Codex-Installation-ID`（见 §1.1.1.1），API Key 路径不补。通过普通 `/v1/responses` body-signal 触发且原请求为 `stream:true` 时，下游桥接为最小 Responses SSE，确保包含 `response.output_item.done` 和 `response.completed`。

- mimic 只对齐 header、body、TLS 和路由，不复制服务端隐藏 prompt、账号状态、产品 memory 或 UI 上下文，也不替换响应文本或清洗客户端正文身份。
- Anthropic `active` 画像在启用 mimic 和 `enable_tls_fingerprint` 后，未指定 `tls_fingerprint_profile_id` 时使用内置 Claude Code TLS Profile；`previous` 回退画像才使用标准 Transport。管理员显式选择的固定或随机 TLS Profile 始终优先。
- Codex CLI `active` 画像在开启 TLS fingerprint 时使用 2026-07-26 抓包的 direct 30-cipher/空 ALPN 或 proxy h2 传输画像；管理员显式选择的 TLS profile 仍优先。API Key WebSocket 由路由决策层强制回落 HTTP，不会因注册表中存在未激活 WS 画像而开启（规则见 §1.2.1 第 6 条）。
- OpenAI `previous` 回退画像与 Anthropic 同规则：Desktop 画像没有可复用的实抓 TLS，未指定 `tls_fingerprint_profile_id` 时使用标准 Transport。TLS 决策只能来自请求级画像，正式 Gateway、账号测试、能力探测和 keeper 内部代理共用同一判定，任何路径都不得自行重解析出另一个 mode 的画像。
- HTTP/1.1 与 HTTP/2 Transport 的 `DisableCompression` 由 TLS Profile 的 `Transport` 字段驱动，不是固定值：Codex 的 direct/proxy/WS 画像显式设 `true` 以避免自动注入 gzip，Anthropic 画像未设置该字段（等于 `false`）；两者都不影响显式压缩响应的受控解压。
- Codex base instructions 由 `CodexBaseInstructionsForModel()` 按模型分四条分支：含 `codex` 的模型走 `DefaultInstructions`，`gpt-5.5` 走最新版本，`gpt-5.2` 与 `gpt-5.1` 各有单独维护的 prompt（内容为空时回退最新），其余模型回退最新版本。
- `Anthropic-Dangerous-Direct-Browser-Access: true` 在 `claude.DefaultHeaders` 共享常量和 registry 的 Wire Profile 中各有一份，分别服务非 mimic 路径与 mimic 出站，取值一致且都对应 2026-07-26 Claude Code CLI `2.1.220` API 实抓；mimic 运行时只读取自己的 Wire Profile，不回写共享常量。
- 管理后台账号测试只有在 HTTP 200、收到正常 SSE 内容并以 `message_stop` 结束时才成功；非 200 或 HTTP 200 但正文仅为服务暂不可用降级文案时都必须失败并保留上游错误。
- 效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。

### 1.3 Antigravity 增强

Antigravity 增强用于让 Antigravity 账号新增后默认可用；新建账号默认白名单、默认映射和 `/models` 收敛到官方抓包确认的 8 个模型，同时保留账号编辑页“自定义模型名称”入口，允许管理员手动把后续新增的 Google / Antigravity 模型加入该账号白名单。

| 界面显示名 | 官方发包 model | 固定 `thinkingBudget` |
|---|---|---:|
| Claude Opus 4.6 Thinking | `claude-opus-4-6-thinking` | 1024 |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 1024 |
| GPT-OSS 120B Medium | `gpt-oss-120b-medium` | 8192 |
| Gemini 3.1 Pro High | `gemini-pro-agent` | 10001 |
| Gemini 3.1 Pro Low | `gemini-3.1-pro-low` | 1001 |
| Gemini 3.5 Flash High | `gemini-3-flash-agent` | 10000 |
| Gemini 3.5 Flash Low | `gemini-3.5-flash-extra-low` | 1000 |
| Gemini 3.5 Flash Medium | `gemini-3.5-flash-low` | 4000 |

> 上表的显示名与发包 model 存在系统性错位——界面「Gemini 3.5 Flash Low」发 `gemini-3.5-flash-extra-low`、「Medium」发 `gemini-3.5-flash-low`，「Gemini 3.1 Pro High」发 `gemini-pro-agent`；`thinkingBudget` 的 `10001`/`1001` 也不是整数。这些都与官方抓包一致，不是笔误，改成"看起来对"的值会直接改坏发包行为。

| 主题 | 行为 |
|---|---|
| 默认集合 | `model_mapping` 缺失或为空时，旧账号默认允许并展示官方 8 模型；新账号创建时默认写入 16 条——8 条 `模型 ID -> 模型 ID` 自映射和 8 条 `界面显示名 -> 模型 ID` 映射。管理员可通过“自定义模型名称”追加模型，手动“同步上游支持的模型”保存真实结果。 |
| 显式白名单 | 非空 `model_mapping` 中规范化后的 `model -> model` 自映射构成唯一允许集合，官方模型不会隐式补回；显式配置但无法解析出有效字符串映射时按空白名单处理。 |
| 映射与别名 | 模型映射保存“客户端请求模型名 -> 实际发包 model”，键既可以是模型 ID，也可以是界面显示名（默认落库的 8 条显示名映射即属此类）。但允许集合只由 `model -> model` 自映射构成，所以显示名映射、普通映射、通配符和 `gemini-3.1-pro-high` 等历史别名都只负责解析、不扩大允许集合，最终目标必须命中允许集合；历史别名不进入默认白名单或 `/models`。 |
| 模型广告 | `/antigravity/v1/models` 展示账号实际允许的界面模型和手动加入的模型，不展示兼容别名。**已知问题**：允许集合为空时（见“显式白名单”行）该端点仍回落展示官方 8 模型，而此时任何请求都会被拒 403，广告集合与可用集合完全不相交；修复方向是空集合时返回空列表。 |
| `web_search` | 固定使用 `gemini-3.5-flash-low`（即上表界面显示的 **Gemini 3.5 Flash Medium**）；存在显式白名单时必须保留该模型的自映射，不能绕过白名单。 |
| 官方伪装 | UA 默认为 `antigravity/hub/2.2.1 darwin/arm64`，版本号可由 `ANTIGRAVITY_USER_AGENT_VERSION` 环境变量或后台“网关转发 → `antigravity_user_agent_version`”覆盖；默认 8 模型忽略客户端 `thinking` / `output_config.effort`，使用表中固定预算；在内层 `request.labels` 补 `model_enum/trajectory_id` 等官方标签并生成同源 `requestId`，过滤无关 stop / sampling 参数。手动追加模型按其名称发包，无需进入全局官方模型表。 |
| 计费 | 按最终实际发包模型 `UpstreamModel` 查价，日志保留外部模型，且优先于渠道 `requested` / `channel_mapped`；`gpt-oss-120b-medium` 每 1M tokens 为输入 `$0.05`、缓存读取 `$0.01`、输出 `$0.20`。 |

### 1.4 账号保活

账号保活用于在 OpenAI / Anthropic API Key 账号空闲超过配置间隔后，通过官方 `codex` / `claude` 客户端在真实项目目录发起低频请求，维持上游账号活跃。

#### 1.4.1 架构、生效范围与配置

1. 主服务管理配置、账号引用、成本、最近使用时间和历史；`keeper/` 以独立 `sub2apiplus-keeper` sidecar 运行，负责调度官方客户端、worker 目录、会话、日志和本地 Web/API。
2. OpenAI 使用 `codex`，Anthropic 使用 `claude`；仅支持相应平台的 API Key 账号，OAuth、setup-token、upstream 等账号不进入候选。
3. 不同账号可并行执行，同一账号通过 `Running` 状态防止重复执行。
4. keeper 获取按账号、按平台签发的短期 scoped proxy token；官方客户端进程不能获得全局 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN`。
5. 只有 `IsSchedulable()` 通过且具备有效平台 API Key 的账号进入候选，其余不返回、不签发 token，恢复后自动重新进入；账号代理入口在实际执行前再次校验。排除项：停用、`error` 状态、`schedulable=false`、过载、限流、临时不可调度、配额耗尽，以及过期账号（仅在账号级 `auto_pause_on_expired` 开启时排除，该开关默认开）。
6. 官方客户端单次执行超时默认 2700 秒，该值同时是 Claude 链路的最小值，不能调得更低；`sub2apiplus.timeout_seconds` 仅控制 keeper 调主服务内部接口的超时，默认 180 秒。
7. keeper 容器内固定 Claude CLI `2.1.210`，与 mimic 出站画像版本（§1.2.2 的 `2.1.220`）是两件独立的事：本地跑的是真实 CLI，出站形态由内部代理按 active 画像改写，两者不需要对齐。

保活配置位于“Plus 增强功能 / 账号保活”；账号级设置保存在 `account.Extra`，全局约束和题库保存在 keeper state：
| 配置 | 说明 |
|---|---|
| 启用保活 | `keeper_keepalive_enabled`，控制是否参与自动保活。 |
| 保活间隔 | `keeper_keepalive_interval_minutes`，默认 8 分钟，取值范围 1 - 10080 分钟（7 天）。 |
| 工作时间 | 默认 `04:00` - `24:00`。 |
| 执行模式 | OpenAI 默认全新会话 `fresh`，Anthropic 默认接续上次会话 `resume_last`。 |
| 项目 | 从 `SUB2APIPLUS_KEEPER_PROJECTS` 暴露的项目列表选择，keeper 内部映射到 `/workspace/projects/<项目名>`。 |
| 最大输出 token | `keeper_keepalive_max_output_tokens`，默认 512；主服务按该值钳制请求，硬上限为 1024。 |

保活账号、模型和 prompt 题库在界面上选择，成本复用现有 usage 统计口径。管理界面分概览、配置、会话历史三个视图，其中非显而易见的行为是：概览只展示已启用目标；配置页支持全部/已启用/已禁用筛选，已启用优先、同状态按账号 ID 倒序；会话历史从全部 target 汇总，停用账号的既有记录仍可查看，完整上游/客户端错误保留在错误详情中。

#### 1.4.2 调度与失败处理

1. keeper 按 `scan_interval_seconds` 周期扫描账号；本仓库示例配置为 30 秒，未配置时程序默认 120 秒。
2. 下次触发时间取“最近真实请求”和“最近成功保活”分别加保活间隔后的较晚值；账号持续使用时会自动顺延，不产生额外保活请求。
3. 手动“立即执行”忽略空闲判断，但同账号运行中时不会重复启动。
4. 失败会记录错误和失败次数，并按账号间隔重新排队。连续失败计数只对一类错误豁免：Codex 常驻会话的可重试 WebSocket 类错误。其余情况一律累计，包括 Claude 子进程断开、`SIGTERM` 取消运行上下文产生的 `context.Canceled`；keeper 重启后对重启前处于 `running` 的目标还会额外累计一次，错误记为“重启前会话未完成”。
5. 收到 `SIGTERM` / `Interrupt` 后，keeper 会取消运行上下文、等待任务收尾并关闭持久连接。

#### 1.4.3 Anthropic keeper 的 mimic 兼容约束

Anthropic keeper 通过主服务内部代理转发 Claude CLI 请求。OpenAI keeper 的内部代理不做 mimic，按 header allowlist 透传官方 Codex CLI 形态（见 §1.2.1 入口表），本节约束不适用。该链路必须遵守：

1. mimic 只作用于 `/v1/messages` 这一条路径：账号开启 `anthropic_apikey_mimic_claude_code` 时，keeper 的 `/v1/messages` 必须复用账号测试和正式 Gateway 的 active Claude Code CLI API Key Profile；同时开启 passthrough 时仍由 mimic 优先（见 §1.2.1 规则 2）。keeper Anthropic 代理放行的另两条路径 `POST /v1/messages/count_tokens` 和 `GET /v1/models` 不进入 mimic，即使账号开着 mimic 也带 Claude CLI 自身 header 原样转发；mimic 关闭时三条路径均原样转发。
2. mimic 前必须执行账号模型映射和 `max_tokens` 钳制；最终 UA、billing 版本、beta、system、metadata、session、cache 和鉴权规则必须由同一 Registry Profile 解析，CWD 等项目上下文移入 messages 保留。
3. TLS 决策与 Anthropic 账号测试、正式 Gateway 完全一致，规则见 §1.2.5，此处不另行定义。
4. keeper 的权限限制会使 Claude CLI 只上报 `Read` 等少量工具；mimic 内部代理必须按 active 官方画像基线重排工具：保留同名真实工具定义、删除基线外工具、补齐缺失工具；补齐项必须明确标记为不可用，不能因此放开 keeper 的 Shell、写文件或联网权限。
5. Claude CLI 必须使用稳定的 `--name keeper-<账号ID>`，并在真实子进程环境设置 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`（两者的作用边界见 §1.4.4）。
6. 常驻 Claude 子进程必须显式继承 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<keeper_keepalive_max_output_tokens>`，与主服务请求钳制值保持一致；该值要传给 `exec.Command` 的环境，只写 `runtime.env` 不生效（见 §1.4.4）。
7. `api_retry` 中的瞬时 429 必须保留在 stdout 供审计，但不能覆盖同一轮后续的成功 `result`；最终有正常回复和非零 usage 时，keeper 应记录 `success` 并将连续失败次数清零。

#### 1.4.4 已知坑与排查方法

以下为排查经验，不是实现契约。涉及 CLI 内部行为和第三方站点响应的部分属于外部观测，仓库内无对应证据文件。

- **环境变量必须传给子进程**：只把 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 写入 `runtime.env` 而未传给 `exec.Command` 的环境，会使 CLI 仍按 64000 token 运行，并把上游的 512 token 截断误判为输出超限。在 Anthropic 保活任务执行期间可这样核对实际子进程：

  ```sh
  docker exec sub2apiplus-keeper pgrep -af -- '--name keeper-'
  docker exec sub2apiplus-keeper sh -lc 'pid=$(pgrep -o -f -- "--name keeper-"); tr "\0" "\n" < "/proc/${pid}/environ" | grep -E "^CLAUDE_CODE_(MAX_OUTPUT_TOKENS|DISABLE_NONESSENTIAL_TRAFFIC)="'
  ```
- **标题请求不是保活正文**：缺少 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 时，CLI 仍可能后台发送 `tools=[]`、`thinking=disabled`、带结构化 `output_config` 的标题请求，AnyRouter 会以 HTTP 429 拒绝。`--name` 只负责会话标识，不能单独关闭自动标题。
- **不要把 429 归因于 CLI 版本**：版本只能用于判断客户端请求形态。应使用同一账号和模型对照新版 CLI；若新旧版本均为 429，继续检查内部代理的 TLS、header、body 和上游状态。
- **排查期间暂停自动调度**：据观测 Claude CLI 对单次 429 最多自动重试 10 次，排查时应暂停调度或使用受控单次请求，避免把一轮失败误判为多轮保活。
- **区分持续 429 与瞬时重试**：判定契约见 §1.4.3 第 7 条。2026-07-15 实测中一轮保活的 6 个正文请求全部返回 HTTP 200，首个请求经 2 次瞬时 429 自动重试后恢复，最终仍记为 `success`，完整运行记录见实证归档。

### 1.5 其它优化

- Composite 与 Grok 同等豁免，使 Anthropic 协议也能访问 GPT、Grok 模型。
- Anthropic OAuth 账号增加“模型限制”区块，复用 `ModelWhitelistSelector`，支持同步最新模型、同步上游模型和清空模型；`oauth` / `setup-token` 账号的配置统一持久化到 `model_mapping`。

## 2. 全新服务器部署

推荐使用 Docker Compose。主服务目录为 `/root/docker/sub2apiplus/app`；keeper 配置、数据和项目位于 `/root/docker/sub2apiplus/keeper/app`，构建源码位于 `/root/docker/sub2apiplus/keeper/repo`，其中 `app/projects/<项目名>` 挂载到 `/workspace/projects/<项目名>`。以下流程统一使用运行目录中的 `docker-compose.yml`。

主服务和 keeper 来自同一仓库，但分别以 GHCR 镜像和本地构建 sidecar 部署。完整可复现部署应选择同一个已发布 Plus 版本，同时使用 `ghcr.io/itv3/sub2apiplus:<Plus版本>` 和 Git tag `v<Plus版本>`；不能把主服务 `latest` 与 keeper `main` 称为可复现组合，`latest` / `main` 只适合持续跟踪最新代码的环境。允许暂缓更新 keeper 的条件见 §3.4。

### 2.1 准备主服务

宿主机先安装 Docker、Docker Compose plugin、`git`、`curl` 和 `openssl`。快速部署可用一键脚本下载 `main` 模板、生成 `.env`，并自动填入 `JWT_SECRET`、`TOTP_ENCRYPTION_KEY` 和 `POSTGRES_PASSWORD`。该方式跟踪 `latest`，且不安装 keeper；需要 keeper 或可复现部署时应使用下方固定版本流程：

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -sSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-deploy.sh | bash
```

需要手动审查和准备文件时，先把 `VERSION` 替换为目标发布版本，再从同一 tag 下载部署文件并固定主服务镜像：

```sh
export VERSION="<已发布的 Plus 版本号，不含 v>"
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -fsSL "https://raw.githubusercontent.com/itv3/sub2apiplus/v${VERSION}/deploy/docker-compose.local.yml" -o docker-compose.yml
curl -fsSL "https://raw.githubusercontent.com/itv3/sub2apiplus/v${VERSION}/deploy/.env.example" -o .env
sed -i -E "s#image: ghcr.io/itv3/sub2apiplus:[^[:space:]]+#image: ghcr.io/itv3/sub2apiplus:${VERSION}#" docker-compose.yml
mkdir -p data postgres_data redis_data
```

主服务 `.env` 至少要设置以下变量，各变量说明统一见 §3.2：

```env
COMPOSE_PROJECT_NAME=sub2apiplus
POSTGRES_PASSWORD=<随机强密码>
JWT_SECRET=<openssl rand -hex 32>
TOTP_ENCRYPTION_KEY=<openssl rand -hex 32>
ADMIN_PASSWORD=<自定义后台密码，可选>
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<openssl rand -hex 32>
SUB2APIPLUS_KEEPER_PROJECTS=homeproxy
# 可选；留空默认 http://sub2apiplus-keeper:38090
# SUB2APIPLUS_KEEPER_BASE_URL=http://sub2apiplus-keeper:38090
```

```sh
docker compose up -d
curl -fsS http://127.0.0.1:8080/health
# 未设置 ADMIN_PASSWORD 时查看首次密码
docker compose logs sub2api | grep "admin password"
```

后台地址为 `http://<服务器IP>:8080`。登录后先添加 API 账号，并确认平台、可用模型和模型成本配置正常。

### 2.2 准备 keeper

keeper 镜像构建时会在容器内安装官方 `codex` / `claude` 客户端；它们不进入 sub2apiplus 主镜像，也不要求宿主机单独安装。Claude CLI 由 `keeper/Dockerfile` 的 `CLAUDE_RELEASE` 固定版本，更新版本时必须同时修改该默认值，不能依赖可被 Docker 缓存的 `latest` 安装层。Codex CLI 的 `CODEX_RELEASE` 当前仍为 `latest`，每次无缓存重建都会安装当时的最新版；需要可复现的 Codex 版本时应同样改为具体版本号。

准备 keeper 源码和配置：

```sh
: "${VERSION:?请先设置为与主服务相同的 Plus 版本号}"
mkdir -p /root/docker/sub2apiplus/keeper/app/{data,projects} /root/docker/sub2apiplus/keeper/repo
rm -rf /tmp/sub2apiplus-src
git clone --branch "v${VERSION}" --depth 1 https://github.com/itv3/sub2apiplus.git /tmp/sub2apiplus-src
cp -a /tmp/sub2apiplus-src/keeper/. /root/docker/sub2apiplus/keeper/repo/
cp /tmp/sub2apiplus-src/keeper/keeper.example.yaml /root/docker/sub2apiplus/keeper/app/keeper.yaml
rm -rf /tmp/sub2apiplus-src
```

下载保活项目。`SUB2APIPLUS_KEEPER_PROJECTS` 使用单级项目名，多个项目用英文逗号分隔（如 `homeproxy,sub2apiplus`）；不接受绝对路径、`..` 或多级路径，每个项目名的同名目录必须放在 keeper 的 `projects` 下：

```sh
cd /root/docker/sub2apiplus/keeper/app/projects
git clone --depth 1 https://github.com/itv3/homeproxy.git homeproxy
```

准备 keeper `.env`，其中 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 必须与主服务 `.env` 完全一致：

```env
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<与主服务相同>
KEEPER_BIND_HOST=127.0.0.1
KEEPER_HOST_PORT=38091
# 留空规则见 §3.2
KEEPER_WEB_USERNAME=
KEEPER_WEB_PASSWORD=
```

检查 keeper `keeper.yaml`。完整示例为 `keeper/keeper.example.yaml`；提示词题库可先使用示例值，后台保存后持久化到 `/app/data/state.json`。`sub2apiplus.base_url` 必须是容器内可访问的主服务地址，默认使用 `http://sub2apiplus:8080`：

```yaml
timezone: Asia/Shanghai
scan_interval_seconds: 30
state_path: /app/data/state.json
projects_root: /workspace/projects
runtime_root: /app/data/runtime

sub2apiplus:
  base_url: http://sub2apiplus:8080
  internal_token: ${SUB2APIPLUS_KEEPER_INTERNAL_TOKEN}
  timeout_seconds: 180

web:
  enabled: true
  listen: 0.0.0.0:38090
  username: ${KEEPER_WEB_USERNAME}
  password: ${KEEPER_WEB_PASSWORD}
```

下载 keeper compose（与主服务同一 tag）：

```sh
cd /root/docker/sub2apiplus/keeper/app
curl -fsSL "https://raw.githubusercontent.com/itv3/sub2apiplus/v${VERSION}/deploy/docker-compose.keeper.yml" -o docker-compose.yml
```

模板中的 `sub2api-network.name` 必须与主服务网络一致；设置 `COMPOSE_PROJECT_NAME=sub2apiplus` 后固定为 `sub2apiplus_sub2api-network`，可用 `docker network ls | grep sub2apiplus` 确认。

构建并启动 keeper。标准更新必须禁用构建缓存，避免复用旧的官方客户端安装层：

```sh
cd /root/docker/sub2apiplus/keeper/app
docker compose build --pull --no-cache --build-arg VERSION="${VERSION}" keeper
docker compose up -d keeper
docker exec sub2apiplus-keeper /app/sub2apiplus-keeper -version
docker exec sub2apiplus-keeper codex --version
docker exec sub2apiplus-keeper claude --version
```

构建完成后，keeper 版本必须与主服务 Plus 版本一致，`claude --version` 必须与 `keeper/Dockerfile` 的 `CLAUDE_RELEASE` 一致；任一不一致时不得继续保活验证。

keeper 需要 `CAP_SYS_ADMIN`、`seccomp=unconfined`、`apparmor=unconfined` 以运行官方客户端和 `bubblewrap` 沙箱；`data`、`runtime`、`projects` 必须持久化。

### 2.3 后台激活和验证

1. 在后台添加 API 账号，确认模型和成本配置正常。
2. 进入“Plus 增强功能 / 账号保活 / 配置”，添加 OpenAI / Anthropic API Key 账号；平台自动选择 `codex` / `claude`。
3. 配置模型、项目、间隔、工作时间、执行模式、全局约束和题库后保存。
4. 点击“立即执行”，到“会话历史”确认回复、token 和费用。
5. Anthropic 任务执行期间检查真实 Claude 子进程的命令行与环境变量，核对项见 §1.4.3 第 5、6 条，验证命令见 §1.4.4。
6. 任务结束后确认最终状态为 `success`、usage 非零、连续失败次数归零；瞬时 429 的判定口径见 §1.4.3 第 7 条。

验证命令：

```sh
curl -fsS http://127.0.0.1:8080/health
# 不带凭据访问返回 401 即为正常结果：端口映射通、服务在监听、鉴权生效
curl -s -o /dev/null -w 'keeper web: %{http_code}\n' http://127.0.0.1:38091/
# 需要真正读取 keeper 状态时用内部 token；token 从容器环境变量取，不写入命令历史
docker exec sub2apiplus-keeper sh -lc 'curl -fsS -o /dev/null -H "x-api-key: ${SUB2APIPLUS_KEEPER_INTERNAL_TOKEN}" http://127.0.0.1:38090/api/state && echo "keeper api OK"'
cd /root/docker/sub2apiplus/keeper/app && docker compose ps
docker inspect sub2apiplus --format '{{.Config.Image}}'
docker exec sub2apiplus /app/sub2api -version
docker exec sub2apiplus-keeper /app/sub2apiplus-keeper -version
docker exec sub2apiplus-keeper claude --version
```

### 2.4 Apple 容器部署（macOS）

Apple silicon Mac 可使用 Apple `container` 1.1+ 在本机运行 Sub2API Plus、PostgreSQL 和 Redis，无需 Docker Desktop。该方式面向本地开发和管理员维护的部署，不提供 Compose 等价的自动重启与持续编排能力；生产环境仍推荐 Docker Compose。

```sh
cd deploy
./apple-container.sh init
./apple-container.sh up
./apple-container.sh status
```

完整生命周期、持久化、升级和限制说明见 [`deploy/APPLE_CONTAINER.md`](deploy/APPLE_CONTAINER.md)。

## 3. 运维与升级

### 3.1 主服务常用命令

```sh
cd /root/docker/sub2apiplus/app
docker compose ps
docker compose logs --tail=100 sub2api
docker compose restart sub2api
```

镜像 `ghcr.io/itv3/sub2apiplus` 支持 `linux/amd64` 和 `linux/arm64`；`latest` 为最新稳定版，`<Plus 版本>`（如 `0.1.164-1`）固定版本，`<上游主版本>.<上游次版本>` 指向对应 minor 线的最新补丁。

### 3.2 关键环境变量

| 变量 | 用途 |
|---|---|
| `COMPOSE_PROJECT_NAME=sub2apiplus` | 固定 Docker 网络名，供 keeper sidecar 接入。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码，必须固定保存。 |
| `JWT_SECRET` | 登录会话签名密钥，生产环境必须固定。 |
| `TOTP_ENCRYPTION_KEY` | TOTP 加密密钥，生产环境必须固定。 |
| `ADMIN_PASSWORD` | 管理员初始密码；留空时从 `docker compose logs sub2api` 查看自动生成值。 |
| `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` | 主服务与 keeper 的内部鉴权 token，双方必须一致。 |
| `SUB2APIPLUS_KEEPER_BASE_URL` | 主服务代理 keeper Web/API 时使用的地址；写入主服务 `.env` 并由 compose 注入。留空时默认 `http://sub2apiplus-keeper:38090`。 |
| `SUB2APIPLUS_KEEPER_PROJECTS` | 账号保活项目下拉框项目名，多个项目用英文逗号分隔。 |
| `KEEPER_BIND_HOST` | keeper Web 端口绑定地址，默认建议 `127.0.0.1`，由 sub2apiplus 后台代理访问。 |
| `KEEPER_HOST_PORT` | keeper Web 映射到宿主机的端口，默认示例为 `38091`。 |
| `KEEPER_WEB_USERNAME` / `KEEPER_WEB_PASSWORD` | keeper 独立 Web 入口的 Basic Auth；留空或只配置一项时不会放行，只能通过内部 token 或完整 Basic Auth 访问。 |

### 3.3 数据迁移

迁移时停止主服务和 keeper，并打包整个 `/root/docker/sub2apiplus`：

```sh
docker compose -f /root/docker/sub2apiplus/app/docker-compose.yml down
docker compose -f /root/docker/sub2apiplus/keeper/app/docker-compose.yml down
cd /root/docker
tar czf sub2apiplus.tar.gz sub2apiplus/
```

在新服务器解压后分别启动：

```sh
docker compose -f /root/docker/sub2apiplus/app/docker-compose.yml up -d
docker compose -f /root/docker/sub2apiplus/keeper/app/docker-compose.yml up -d
```

### 3.4 发布和应用更新

发布前先跑定向测试；大范围合并或共享逻辑改动时扩大到 `go test ./...` 和完整前端测试：

```sh
(cd /path/to/sub2apiplus/backend && go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service)
(cd /path/to/sub2apiplus/keeper && go test ./...)
(cd /path/to/sub2apiplus && make test-frontend)
```

Plus 版本在上游版本后追加自定义序号，例如 `0.1.164-1`；Git tag 为 `v0.1.164-1`，镜像 tag 为 `ghcr.io/itv3/sub2apiplus:0.1.164-1`。抓包验收使用的阶段构建（如 `0.1.164-1-n2`）不对外发布，也不进入该命名体系，只出现在实证归档。Release workflow 使用 annotated tag 的 message 生成 Release notes，因此必须使用 `git tag -a -m`；轻量 tag 不包含说明正文。

```sh
cd /path/to/sub2apiplus
VERSION="<待发布的 Plus 版本号，不含 v>"
echo "$VERSION" > backend/cmd/server/VERSION
git add backend/cmd/server/VERSION
git commit -m "chore: release v${VERSION}"
git push origin main
git tag -a "v${VERSION}" -m "v${VERSION}

- 本版改动要点 1
- 本版改动要点 2"
git push origin "v${VERSION}"
```

> 已经打成轻量 tag 时，可用 `gh release edit "v${VERSION}" --notes-file <文件>` 事后补写 Release 说明，无需重跑 CI。

如果 tag push 没触发 Release，手动触发：

```sh
gh workflow run Release --repo itv3/sub2apiplus --ref main -f tag="v${VERSION}" -f simple_release=false
```

默认按同一 tag 更新主服务和 keeper，获得严格可复现的完整部署。若 Release notes 明确确认没有 keeper 或内部代理变更，可以只更新主服务并暂时保留上一版 keeper，但必须记录版本错位；一旦 keeper 或内部代理有改动，两者必须同步更新，不能只更新其中一方。AMD64 / ARM64 均使用以下完整流程，不动 PostgreSQL、Redis、volume、keeper 数据目录和 `.env`：

升级前必须备份数据库。部分迁移会安装数据库触发器且没有自动反向迁移，回退到旧应用时不能只回退镜像，必须同时采用经审核的触发器停用或反向迁移方案，否则相关 outbox 表会继续累积。每个版本引入的具体迁移编号见对应 Release notes。

```sh
export VERSION="<新发布的 Plus 版本号，不含 v>"

# 1. 更新主服务到指定版本并确认健康
cd /root/docker/sub2apiplus/app
sed -i -E "s#image: ghcr.io/itv3/sub2apiplus:[^[:space:]]+#image: ghcr.io/itv3/sub2apiplus:${VERSION}#" docker-compose.yml
docker compose pull sub2api
docker compose up -d --no-deps sub2api
docker compose ps
docker compose logs --tail=100 sub2api
curl -fsS http://127.0.0.1:8080/health

# 2. 用同一 tag 替换 keeper 构建源码，保留 app 下的配置、数据和项目
rm -rf /tmp/sub2apiplus-src
git clone --branch "v${VERSION}" --depth 1 https://github.com/itv3/sub2apiplus.git /tmp/sub2apiplus-src
find /root/docker/sub2apiplus/keeper/repo -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a /tmp/sub2apiplus-src/keeper/. /root/docker/sub2apiplus/keeper/repo/
rm -rf /tmp/sub2apiplus-src

# 3. 无缓存重建 keeper，并核对版本和日志
cd /root/docker/sub2apiplus/keeper/app
docker compose build --pull --no-cache --build-arg VERSION="${VERSION}" keeper
docker compose up -d --no-deps keeper
docker exec sub2apiplus-keeper /app/sub2apiplus-keeper -version
docker exec sub2apiplus-keeper claude --version
docker compose ps
docker compose logs --tail=100 keeper
```

不要执行 `docker compose down -v`，不要删除 volume，不要覆盖 `.env`。维护既有实例前先读取容器的 `com.docker.compose.project.working_dir` 标签确认真实目录，历史实例的路径大小写可能与本文推荐路径不同，不要直接照抄。

Watchtower 只能拉取并替换镜像仓库中的主服务镜像。按本文部署的 keeper 使用本地源码构建镜像 `sub2apiplus-keeper:latest`，没有对应的 GHCR 发布镜像；Watchtower 不会拉取 Git tag、替换 `/root/docker/sub2apiplus/keeper/repo` 或执行 `docker compose build`，因此不会自动更新 keeper。启用 Watchtower 的服务器在主服务自动更新后仍需按上述第 2、3 步手动更新 keeper；否则主服务和 keeper 会处于不同版本。要求严格版本配套时，应固定主服务镜像版本，并在发布后统一执行完整的三步升级流程。

### 3.5 发布、源码与验证镜像的三层状态

正式发布（GitHub Releases 的 tag 与多架构 GHCR 镜像）、当前源码（版本以 `backend/cmd/server/VERSION` 为准，可能尚未发布）和 Vircs 验证覆盖层（直接编译的阶段验收镜像，命名规则见 §3.4，不对外发布）必须分开理解，不能互相代替；生产部署只使用已发布 tag。各层当前的具体版本、镜像 digest、运行目录、场景计数、pcap、MITM 对比和恢复记录统一见[实证 README](backend/internal/service/testdata/official_egress/README.md)，本文不保存快照数值；原始 pcap、完整 Body 和 CLI 事件属于私有敏感证据，不提交 Git。

### 3.6 其它运行能力

Gemini 支持内置 Gemini CLI OAuth Client 的 Code Assist OAuth、通过 `.env` 配置 `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET` 的 AI Studio OAuth，以及后台直接添加 API Key。

后台“数据管理”入口当前仅保留兼容诊断；服务端固定返回 `DATA_MANAGEMENT_DEPRECATED`，不建议新部署 `datamanagementd`，也不要按旧流程挂载 `/tmp/sub2api-datamanagement.sock`。数据迁移优先使用第 3.3 节的本地目录迁移流程；数据库备份请在 PostgreSQL / Redis 层独立执行。

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径，不安装 keeper sidecar；需要账号保活时使用 Docker Compose 部署。

ChatGPT Live 实时会话由 `/v1/live` 与 Codex `/backend-api/codex/realtime/calls` 转发，受分组开关控制，并使用并发租约和会话数硬上限约束，用量按实时会话单独记录。

OpenAI WS 账号使用 `http_bridge` 模式前，必须先开启 WS v2 模式路由：在配置文件设置 `gateway.openai_ws.mode_router_v2_enabled: true`，或设置环境变量 `GATEWAY_OPENAI_WS_MODE_ROUTER_V2_ENABLED=true`。WS 连接使用 Redis 60 秒租约并每 20 秒续租；若连续一个完整租期无法确认租约，实例会主动关闭本地连接，避免多实例同时持有同一会话。

## 4. 维护参考

### 4.1 keeper 内部接口

sub2apiplus 提供内部接口给 keeper 和 Plus 增强功能页面使用。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活、可调度（判定见 §1.4.1 第 5 条）且具备有效平台 API Key 凭据的 OpenAI / Anthropic API Key 账号，以及模型、prompt、项目、最大输出 token、最近使用时间、下一次时间和 due 判断。 |
| `GET /api/v1/internal/keeper/projects` | 返回可在保活配置页选择的项目列表，来源为 `SUB2APIPLUS_KEEPER_PROJECTS`。 |
| `GET /api/v1/internal/keeper/state` | 代理 keeper 状态，用于概览、会话历史和运行状态展示。 |
| `GET /api/v1/internal/keeper/settings` | 读取 keeper 版本、全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/settings` | 保存全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/run?target=<target>` | 立即执行指定保活目标；`target` 可匹配目标 ID、账号 ID 或账号名称。 |
| `GET /api/v1/internal/keeper/accounts/:id/models` | 返回该账号可用于保活的模型列表。 |
| `POST /api/v1/internal/keeper/accounts/:id/keepalive` | keeper sidecar 回写状态、token、费用和错误信息；带 `prompt` 的主服务直连执行请求会被拒绝。 |
| `GET/POST /api/v1/internal/keeper/openai/accounts/:id/*` | Codex 代理；POST 仅允许 `/v1/responses`、`/responses`、`/v1/chat/completions`、`/chat/completions`，GET 仅允许 `/v1/models`、`/models`、`/v1/responses/*`、`/responses/*`。 |
| `GET/POST /api/v1/internal/keeper/anthropic/accounts/:id/*` | Claude 代理；POST 仅允许 `/v1/messages`、`/v1/messages/count_tokens`，GET 仅允许 `/v1/models`。 |

| 接口类型 | 允许的鉴权 |
|---|---|
| 账号列表、项目、state、settings、立即执行和模型列表 | 全局内部 token 或后台 admin auth。 |
| `keepalive` 回写 | 仅全局内部 token。 |
| OpenAI / Anthropic 账号代理 | 仅主服务按账号和平台签发的 scoped proxy token，不接受 admin auth。 |

代理路径拒绝 query、fragment、`.`、`..`、`%2e` 等不安全片段；请求和响应 header 使用显式 allowlist，避免 `Cookie`、`Set-Cookie`、账号密钥、上游鉴权信息或未脱敏日志越界。

keeper 通过 `max_output_tokens` 把账号级最大输出 token 传回主服务。主服务内部代理会按该值钳制 OpenAI Responses 的 `max_output_tokens`、OpenAI Chat Completions 的 `max_completion_tokens` / `max_tokens`，以及 Anthropic Messages 的 `max_tokens`；请求未带这些字段时会补默认值，超过上限时会降到账号配置值。

### 4.2 Plus 差异文件清单

发布基线为最近一次合并的上游 tag（查法见 §0）；审核 Plus 实现时优先看该 tag 到 `HEAD` 之间的 OAuth 官方出站、mimic、Antigravity、keeper 和 Plus UI。Composite 分组、ChatGPT Live、Ollama Cloud 用量同步、`batch_image`、提示词安全审计、异步图像任务和注册安全修复属于上游能力；`keeper/` 是新增源码，`.codex-captures/` 和 `.kilo/` 是本地样本或工具配置，不计入源码清单。完整差异用以下命令生成：

```sh
BASE=$(git log --oneline --grep='sync upstream' -1 --format=%H)
git diff --name-only "$BASE..HEAD"
git diff --stat "$BASE..HEAD"
```

| 范围 | 入口文件 |
|---|---|
| 官方客户端画像核心 | `backend/internal/service/official_client_profile_registry.go`（ClientBuild、Wire Profile、active/previous、digest 与抓包来源）、`official_egress_profile.go`（激活决策与可逆配置）。 |
| OAuth 官方客户端伪装 | `backend/internal/service/official_egress_*.go`（Resolver、三条路径 Finalizer、终态校验、Transport/Dialer）、gateway 与 OpenAI WS 转发链中的调用点、`backend/internal/repository/http_upstream.go`。 |
| API Key mimic | `backend/internal/service/*apikey_mimic*`、OpenAI gateway/scheduler/WS 相关 service、`backend/internal/pkg/tlsfingerprint/`、`backend/internal/repository/http_upstream.go`、`backend/internal/handler/openai_gateway_handler.go`。`backend/internal/pkg/claude/constants.go` 随 OAuth 画像版本维护，mimic 画像不写入该文件。 |
| Antigravity | `backend/internal/pkg/antigravity/`、`backend/internal/service/antigravity_*`、`upstream_models.go`、`model_rate_limit.go`、`backend/resources/model-pricing/model_prices_and_context_window.json`。 |
| 账号保活 | `keeper/`、`backend/internal/handler/admin/account_handler_keeper.go`、`backend/internal/service/*keeper*`、`backend/internal/server/routes/admin.go`。 |
| Plus UI | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、账号 API、路由、侧边栏、i18n、账号创建/编辑和模型白名单相关组件。 |
| 发布部署 | `README.md`、`deploy/.env.example`、`deploy/docker-compose.local.yml`、`.github/workflows/release.yml`、`backend/cmd/server/VERSION`。 |

### 4.3 上游合并检查

合并上游后按第 1 节功能规则重点确认：
**OAuth 官方客户端伪装**（判定标准一律以 §1.1 为准，本清单只列合并后必须逐项复核的点，不重复定义规则）
- 三条 OAuth 出站路径继续自动命中 Registry 的服务级 `active/previous` 画像；账号级开关和任意版本字段不得回流到 API、数据库或管理端表单。
- **Profile 生效条件与适用边界**：入口端点、目标平台、OAuth 账号、实际出站协议、官方 Host 五项全匹配才生效，API Key 与非模型入口保持原行为——见 §1.1.3.2、§1.1.3.4。
- **Anthropic 参数与 beta**：动态 beta 合并、`temperature` 与 `top_p`/`top_k` 各自的清理条件、入口别名规范化——见 §1.1.1.1。
- **入站宿主头剥离**：三条路径共用同一份剥离清单，`accept-language`、`sec-fetch-mode`、`x-stainless-helper-method` 不得随任一路径的 HTTP 请求或 WS 握手上行；抓包脚手架的第三方客户端必须实际携带这些头才能验证该约束——见 §1.1.1.1。
- **Codex 模型能力**：远端清单的接管条件（非空且含 `visibility=list`）与接管后缺失 slug 走 fallback 而非回落 bundled、能力位来源与缓存键、Lite 与非 Lite 的结构转换差异、能力加载失败不得同步拒绝业务请求、bundled 与实抓 manifest fixture 的一致性断言——见 §1.1.1.1、§1.1.3.3。
- **compact 身份**：`prompt_cache_key` 与 installation ID 的补齐——见 §1.1.1.1；比较器需同时覆盖 compact 与顶层语义参数。
- **连接池与身份生命周期**：连接池键构成、HTTP 重试重建上下文、WebSocket 握手后冻结身份——见 §1.1.3.3。
- **Cookie 与会话状态**：Cookie jar 的隔离维度与域名 allowlist、WS 不接入 jar、`x-codex-turn-state` 的拒绝与回放条件——见 §1.1.1.1。
- **模式独立性**：画像按实际出站协议选择，不由账号配置直接决定——HTTP 入站恒走 HTTP 画像且 WS mode 不参与；WS 入站时 `ctx_pool`/`passthrough` 走 WS 画像、`http_bridge` 转 HTTP 画像、`off` 拒绝连接；`mode_router_v2_enabled` 关闭时固定按 `ctx_pool` 处理；两种 WS 出站 mode 都要补预热往返并链接业务帧——见 §1.1.3.4。
- **身份与失败边界**：Profile ID/digest/source 与最终 Method/Host/Path 受控、禁止自动重定向、校验失败时明确失败并记录脱敏原因——见 §1.1.3.1、§1.1.3.4。
- OAuth 冲突配置必须作为 dormant 数据保留；新建或从关闭切到开启的冲突值要显式拒绝，不得在保存时静默删除。

**API Key mimic**
- **触发条件与身份头保护**：入站只对非官方客户端触发、内部入口不做识别、命中 mimic 时关键身份头不被账号 header override 覆盖——见 §1.2.1 第 4、7 条与入口表。
- **Anthropic 构造边界**：mimic 专用 beta 列表不影响普通 API Key；`/v1/messages` 与 `/v1/messages/count_tokens` 各自独立构造，1M beta 的适用模型前缀不得扩大——见 §1.2.1、§1.2.2。工具名归一和 per-request reverseMap 只修改结构化工具字段。
- **画像来源隔离**：Anthropic API Key `active` 的 Header/Body/TLS 画像只能来自 registry 的局部 Wire Profile，不得反向写入 `pkg/claude` 共享常量或 `defaultFingerprint`；共享常量随 OAuth 画像版本同步更新，两者取值可能相同但来源必须独立。管理员显式选择的 TLS profile 仍优先。
- **OpenAI 路由与 compact**：mimic 强制 HTTP，跳过 mimic 后账号调度、previous response 粘连复核和最终转发三处都恢复普通 WS/HTTP 路由；`/v1/messages` 固定走 Responses mimic；compact 保持上游 unary JSON 的白名单形态并按需桥接下游 SSE——见 §1.2.1 第 5、6 条与 §1.2.5。
- **Profile 指针**：默认与 `previous`/历史兼容 ID 的归属见 §1.2.2、§1.2.4；CLI 画像的 direct/proxy TLS 数据与管理员显式 profile 优先级见 §1.2.5。
- **压缩与 probe 分键**：`DisableCompression` 的驱动规则见 §1.2.5；Responses capability probe 继续按 mimic 状态分键。
- **Codex base instructions**：`CodexBaseInstructionsForModel()` 保持 §1.2.5 定义的四条分支，不得增删或改变回退顺序。

**Antigravity**（判定标准见 §1.3）
- 新账号默认写入的 16 条映射（8 条自映射与 8 条显示名映射）与官方 8 模型集合保持一致，不产生重复模型；自定义模型和上游同步结果仍按真实配置保存。
- 允许集合只由自映射构成；默认表、别名、通配符和 `web_search` 都不能重新放开已移除模型。注意允许集合为空时 `/models` 仍会回落广告官方 8 模型，属 §1.3「模型广告」行记录的已知问题，修复前不能把广告集合与允许集合视为始终一致。
- 官方 UA（默认值可被环境变量与后台设置覆盖）、固定 `thinkingBudget`、labels、同源 `requestId` 和最终 `UpstreamModel` 计费保持有效，具体取值见 §1.3。

**Keeper 与 UI**
- 内部接口的账号列表、项目、settings/state、立即执行、token 钳制和会话回写保持对齐（接口与鉴权清单见 §4.1）；候选和实际代理都校验可调度状态，判定条件见 §1.4.1 第 5 条。
- Plus 路由、侧边栏、i18n、账号 API 和设置页与后端保持一致；mimic 与保活筛选、启用优先排序继续有效。
- 保活概览、配置页筛选排序和会话历史的行为见 §1.4.1；Antigravity 编辑页保留模型白名单和映射。
- Release / Docker 继续发布 `ghcr.io/itv3/sub2apiplus`，应用更新只替换 app 容器。

重点文件以第 4.2 节清单为准。

### 4.4 Mimic 对齐原则

继续提升官方客户端一致性时，不直接盲改源码，按下面顺序处理：
1. 采集真实官方客户端请求。
2. 采集经 sub2apiplus 的伪装请求。
3. 建差异表。
4. 用失败请求做 A/B 消融重放，每次只改一个变量或一个可解释变量组。
5. 只对高置信差异改源码。
6. 每改一项都做官方客户端和第三方客户端双向回归。

同时必须遵守的边界：

- 画像统一由 Registry 按“平台 × 认证 × 端点 × 传输”维护独立 Wire Profile，禁止重新引入账号级版本/Profile 覆盖或 `active`/`previous` 字段混用。
- UI 不得展示密钥、token、authorization 或 `x-api-key`；高风险 body cloaking 开关默认关闭或仅灰度启用。

## 5. 其它文档索引

README 只保留当前契约和操作入口；详细设计、抓包步骤与运行证据分别维护在以下文档：
官方 OAuth 客户端仿真的人类可读权威规范仅有 Framework、Codex CLI 和 Claude Code 三份；工具
README、机器生成证据索引及 JSON 收据只承担操作或审计责任，不构成第四份规范。

| 文档 | 说明 |
|---|---|
| [`tools/official_client_capture/README.md`](tools/official_client_capture/README.md) | 官方客户端出站工具索引；Codex 具有受管编排器，Claude 当前仅有取证与门禁工具。 |
| [`backend/internal/service/testdata/official_egress/README.md`](backend/internal/service/testdata/official_egress/README.md) | OAuth、API Key、Kilo、AnyRouter 和 Vircs 的脱敏实证索引。 |
| [`docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) | 官方 OAuth 客户端共用的目标、扩展架构、证据生命周期与发布门槛。 |
| [`docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md) | Codex CLI 客户端规则、Sub2API 仿真实现和版本演进规范；当前 active 基线为 0.147.0。 |
| [`docs/EVIDENCE_INDEX.md`](docs/EVIDENCE_INDEX.md) | Codex 官方规则编号与证据文件的机器生成索引。 |
| [`docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) | Claude Code 规则与证据、环境职责、Sub2API 实现和版本演进规范。 |
| [`docs/COMPOSITE_GROUPS.md`](docs/COMPOSITE_GROUPS.md) | Composite Groups 路由、管理流程和使用边界。 |
| [`deploy/APPLE_CONTAINER.md`](deploy/APPLE_CONTAINER.md) | Apple silicon Mac 的原生容器部署、升级、备份和限制说明。 |
| [`deploy/EDGE_SECURITY.md`](deploy/EDGE_SECURITY.md) | CDN、反向代理可信链和真实客户端 IP 的安全配置说明。 |
| [`docs/PAYMENT_CN.md`](docs/PAYMENT_CN.md) | 支付能力中文说明。 |
| [`docs/PAYMENT.md`](docs/PAYMENT.md) | 支付能力英文说明。 |
| [`docs/ADMIN_PAYMENT_INTEGRATION_API.md`](docs/ADMIN_PAYMENT_INTEGRATION_API.md) | 管理端支付集成 API。 |
| [`docs/BATCH_IMAGE_MVP.md`](docs/BATCH_IMAGE_MVP.md) | 批量生图 MVP（上游能力，非 Plus 自研四项增强）。 |
| [`docs/ASYNC_IMAGE_TASKS.md`](docs/ASYNC_IMAGE_TASKS.md) | 异步图像生成与编辑任务 API（上游能力）。 |
| [`docs/legal/admin-compliance.zh.md`](docs/legal/admin-compliance.zh.md) | 管理端合规说明（中文）。 |
| [`docs/legal/admin-compliance.en.md`](docs/legal/admin-compliance.en.md) | 管理端合规说明（英文）。 |

## 6. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
