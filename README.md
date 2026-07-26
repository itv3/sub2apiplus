# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的自用增强版 fork。维护目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 项目状态

| 项 | 结论 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| 版本基线 | 最新已发布 Plus 版本为 `0.1.164-6`，部署和拉取镜像只能使用已发布版本；`backend/cmd/server/VERSION` 保存当前发布版本，发版后以最新 tag 为准。已合并上游 tag `v0.1.164`，自定义差异优先看 `v0.1.164..HEAD`。 |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| 命名约定 | 对外使用 `sub2apiplus` / `Sub2API Plus`；Go module 和 import 保留 `github.com/Wei-Shaw/sub2api`，降低上游合并成本。 |
| Go / 客户端版本 | 主服务 `go 1.26.5`（`backend/go.mod`）；keeper `go 1.24`（`keeper/go.mod`）；keeper 镜像固定安装 Claude CLI `2.1.210`。 |
| Docker 命名 | Compose service 保留 `sub2api`；默认容器名为 `sub2apiplus`、`sub2apiplus-postgres`、`sub2apiplus-redis`。 |

维护原则：

1. 长期跟随上游主线，发布版本按上游 release/tag 对齐。
2. Plus 账号级开关优先放入 `account.Extra`；mimic / 保活走 Plus 设置页，Antigravity 走账号编辑页；OAuth 官方客户端伪装为内置能力，不提供账号级开关。
3. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、反向代理配置、`.env` 和数据目录。

## 1. Plus 增强功能

管理后台侧边栏“Plus 增强功能”进入 `/admin/settings/plus-enhancements`，包含“API Key 官方客户端兼容”和“账号保活”两个 Tab；Antigravity 的模型、映射、官方伪装和计费配置位于账号创建/编辑页。

### 1.1 OAuth 官方客户端伪装

#### 1.1.1 目标与总结论

> **目标**：
1、Sub2API 对外提供标准 API 接口。无论入站来自官方客户端，还是通过标准 API 接入的第三方客户端，Anthropic/OpenAI OAuth 官方出站均自动使用对应版本 Claude Code / Codex CLI 的真实出站形态。
2、Sub2API 原有兼容层只负责入站协议转换、模型映射、工具转换及请求语义兼容；最终 OAuth 出站请求由内置官方客户端画像统一定型。
>
> **当前基线**：源码基线 Sub2API `v0.1.164`，Vircs 运行版本 `0.1.164-6`。当前 `active` 画像为 Claude Code CLI `2.1.220` 和 Codex CLI `0.145.0`，来源分别为 Vircs `oauth-20260726T014021Z` 与 `api-20260726T014252Z` 的 OAuth/API、direct/MITM、HTTP/WS 自动化抓包。2026-07-26 已在发布版本上用 Anthropic #50、OpenAI #90 重跑三条 OAuth 路径；三条路径的 S1/S2/S4、候选入站到出站语义守恒、应用层官方出站契约和 direct TLS 均通过，未声明差异为 0。独立运行产生的正文、动态身份、Header 顺序和 Codex 运行时 Cookie 仍使原始 `raw_equal=false`，因此“通过”表示官方出站契约一致，不表示两个独立请求逐字节相同。AnyRouter 实际 A/B 仍待其账号恢复。

| 当前路径 | Sub2API 实证（OAuth 官方画像内置生效） |
|---|---|
| **Anthropic HTTP** | `0.1.164-6` 的 S1/S2/S4 为 5/5；`contract_equal=true`、入站到出站语义守恒、未声明差异 0；direct 17-cipher、ALPN `http/1.1` 与官方基准一致 |
| **OpenAI HTTP** | `0.1.164-6` 的 S1/S2/S4 为 5/5；`contract_equal=true`、入站到出站语义守恒、未声明差异 0；direct 30-cipher、空 ALPN 与官方基准一致 |
| **OpenAI WebSocket** | `0.1.164-6` 的 4 次握手、9 条 `response.create` 和 S1/S2/S4 均成功；逐项 turn metadata、历史/当前 turn 分段、语义守恒和契约对比均通过；direct 10-cipher、空 ALPN 与官方基准一致 |

验收所用的阶段构建镜像、运行目录和回归时间戳统一见[实证归档](backend/internal/service/testdata/official_egress/README.md)；阶段构建不对外发布，命名规则见 §3.4。

#### 1.1.2 抓包方法、分组与证据边界

> **抓包操作手册**：OAuth/API 双任务、抓包环境、认证隔离、dry-run、运行和产物方法统一见[官方客户端双任务抓包工具](tools/official_client_capture/README.md)。抓包验证环境在本文统称 Vircs。本节只定义每套任务内部的两轮抓取法、S1/S2/S4、分组关系和证据边界。

客户端版本升级时必须分别执行两套任务，`all` 只负责顺序编排，不能把认证状态或证据合并：

1. **OAuth 任务**：Claude Code / Codex CLI 使用 OAuth 直连 Anthropic / OpenAI 官方平台，为 §1.1 建立基准。
2. **API 任务**：同版本 Claude Code / Codex CLI 使用 Sub2API 访问 Key，通过 Sub2API 公共 HTTPS Base URL 连接 Sub2API，为 §1.2 建立基准；禁止用本机明文 reverse ingress 的 pcap 代替 CLI 到 Sub2API 的真实 TLS。

API 任务生成的官方 CLI 入站基准，后续与 Kilo / Cline / Cursor / Roo Code 请求经 Sub2API mimic 后发往 AnyRouter 等第三方站点的出站形态对比。外部站点 A/B 是独立验收阶段，不属于基准抓包任务。

##### 1.1.2.1 抓包方法：两轮抓取法 + 三场景

同一“任务 + 主体 + 传输 + 场景”分别执行 direct 与 mitm，不能用其中一种证据代替另一种。当前双任务工具中，每个 S1/S2/S4 都单独启动抓包进程并写独立文件；基准工具只记录 CLI 所在边界，不同时记录 Sub2API ingress 或出站：

| 轮次 | 抓取方式 | 验证内容 | 不能证明 |
|---|---|---|---|
| **direct** | 在被测客户端或后续候选网关自身网络命名空间，通过 `tcpdump` 抓取未终止 TLS | ClientHello、cipher、扩展顺序、曲线、签名算法、版本、KeyShare、PSK 和客户端 offered ALPN | 加密后的 Header/Body/WS 帧；服务端协商结果；仅靠规范化结果不能断言连接复用 |
| **mitm** | 只给当前被测进程注入测试代理与 CA，记录目标 allowlist 流量 | URL、HTTP 版本、Header、Body 结构、响应及 WS 帧 | 未插入 MITM 时的真实 TLS 指纹 |

每轮使用相同模型和相同场景：

- **S1**：冷启动简单问答，验证基础请求、响应和身份字段。
- **S2**：同一会话连续多轮，验证会话生命周期和 `previous_response_id`；连接复用如需形成结论，另行分析原始 pcap。
- **S4**：完整工具闭环，验证工具定义、调用、结果回传、`call_id` 和最终回答。

执行要求：

1. 同一对比组使用相同客户端版本、模型、场景输入和重复次数；具体值写入运行元数据，本节不重复固定数值，当前验收基线以 §1.1.1 为准。
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
但不能替代主基线，当前 Kilo 覆盖范围以 §1.1.1 为准：

| 主体类型 | direct 动作 | mitm 动作 |
|---|---|---|
| **官方客户端基准** | 在 CLI 网络命名空间抓取 OAuth→官方平台或 API Key→Sub2API 公共 HTTPS 流量 | 只给 CLI 子进程配置测试 CA 与代理，解密同一目标边界 |
| **Sub2API 候选（后续 A/B）** | 在 Sub2API 网络命名空间抓取到官方平台或第三方 API 站点的出站流量 | 只解密 Sub2API 候选出站；如需证明改写关系，另存并关联对应 ingress 证据 |

HTTP 路径只能对标对应的官方 HTTP 基准，WebSocket 路径只能对标官方 WebSocket 基准。协议、模型或场景不同的样本只能用于辅助观察，不能计入一致性结论。

##### 1.1.2.3 样本与证据边界

- **传输层证据**：仅使用未终止 TLS 的 direct pcap 判定 ClientHello、cipher、扩展顺序及客户端 offered ALPN；自动规范化不把 offered ALPN 当作协商结果，连接重试/复用需另行分析原始 pcap。
- **应用层证据**：使用 mitm 解密记录判定 URL、HTTP 版本、Header、Body、system、tools、响应和 WS 帧。
- **Sub2API 改写证据**：在后续候选 A/B 阶段保存同一次请求的 ingress 和出站关联，比较规范化语义；该要求不意味着基准工具会同时抓两条边界。
- **样本有效性**：必须确认命中指定账号和模型、没有 API Key 或其他账号 fallback、请求成功完成，并能与 usage/请求日志对应。
- **续轮与工具**：S2 必须形成真实连续会话；S4 必须完成“工具定义 → 工具调用 → 工具结果 → 最终回答”，发出即失败的样本无效。
- **版本边界**：每次结论只适用于运行元数据记录的客户端、服务镜像和 Profile；版本变化后必须重抓，不能直接沿用旧数值。
- **安全边界**：Token、Cookie、API Key 和动态身份值必须脱敏；包含完整请求 Body 的原始样本按敏感材料管理。
- **比较口径**：先保存原始严格差异，再按路径契约声明跨独立运行必然变化的正文、动态身份、Header 顺序及 Codex 运行时 Cookie；只有 `contract_equal=true`、候选 ingress→egress 语义守恒且未声明差异为 0 时才通过。不得以 `raw_equal=false` 单独判失败，也不得用声明规则掩盖候选自身语义丢失。

历史 OAuth Official Egress 实证的镜像、测试、运行目录和恢复记录统一归档在
`backend/internal/service/testdata/official_egress/README.md`。

#### 1.1.3 Sub2API `v0.1.164` 改造方案与实施结果

> **基线与证据**：本节沿用 §1.1.1 的实测基线，证据归档入口见 §1.1.2.3。Profile 目标随官方客户端版本更新；不得把单个 JA3、动态身份值或完整 system 文本写成永久常量。

##### 1.1.3.1 方案结论与边界

1. **范围不变**：复用 `v0.1.164` 的 composite 路由、鉴权、账号调度、重试、流式响应、usage、会话粘性和代理系统；官方客户端伪装只修正三条 OAuth 官方出站路径，不改变 Key、Group、模型路由或计费归属。
2. **薄层实现**：在现有协议转换完成后增加统一的 Profile Resolver、`OfficialEgressContext`、Finalizer 和 Transport/Dialer Provider，不分叉完整 Handler。
3. **最小改写**：保留入口已经携带且上游支持的语义，只修正抓包已证明不一致的字段；不得合成官方内部工具、复制完整 system 或无依据展开历史。
4. **应用与传输同时对齐**：Anthropic HTTP、OpenAI HTTP、OpenAI WebSocket 分别维护独立 Profile，同时约束 Header/Body、TLS/ALPN、协议选择和连接复用。
5. **身份与失败边界明确**：动态身份必须来自入口、会话、响应映射或账号配置等可追踪来源。API Key 与其他非适用账号保持原行为；OAuth Profile、Host 或身份状态校验失败时必须明确失败并记录脱敏原因，不能静默切换画像。

##### 1.1.3.2 三条路径的改造前缺口与结果

| 路径 | 改造前主要缺口 | 应用层结果 | 传输层结果 |
|---|---|---|---|
| **Anthropic HTTP** | 使用了错误的客户端 UA、HTTP/TLS 画像和固定会话身份；system/cache 结构未按 OAuth 客户端形态处理 | 修正 Claude Code UA、Beta、Header、system/cache 结构及 session/metadata 生命周期；保留工具名、工具闭环和无 `temperature` 注入 | 使用独立 Claude HTTP Profile，对齐官方 HTTP/1.1、TLS/ALPN 和连接行为 |
| **OpenAI HTTP** | 自动注入无依据的 `instructions`，改写工具 `call_id`；Header 与 Body 身份脱节，传输画像不符 | 取消无依据的 `instructions` 注入，保留 `call_id` 和入口 `additional_tools`、`client_metadata`、`prompt_cache_key` 等原生 Responses 字段；Header 与 Body 身份使用同一生命周期 | 使用独立 Codex HTTP Profile，并按代理边界选择对应传输画像 |
| **OpenAI WebSocket** | 有效 `previous_response_id` 被丢弃并展开历史；续轮重复工具字段，握手身份和 WS Dialer 不符 | 保留有效 `previous_response_id` 和首帧及入口 WS 帧的原始语义；第三方客户端的完整历史被归一化为预热、正式请求和最小工具结果续轮；握手时冻结 session/thread/window/turn 身份 | 使用专用 WS Dialer，对齐握手 Header、压缩协商、TLS/ALPN 和重连边界 |

三条路径还共同存在动态字段来源不统一、不同账号/Profile/代理/CA 可能错误复用连接的问题，必须由公共上下文和连接池键统一解决。

Profile 只在入口端点受支持，且目标平台、OAuth 账号、实际出站协议和官方 Host 均匹配时生效。OpenAI 自动透传和 WS mode 只决定请求处理与实际出站协议：实际走 HTTP 时应用 HTTP Profile，实际走 WebSocket 时应用 WebSocket Profile，不会关闭或绕过官方客户端伪装。

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

##### 1.1.3.4 验收标准与完成结果

| 验收项 | 完成结果 |
|---|---|
| **路由与账务** | 三条路径命中正确平台 OAuth 账号，重试、usage 和计费归属正确；Profile 不改变 Key/Group 权限 |
| **Anthropic HTTP** | S1/S2/S4 全部成功，工具和参数无回归，system/cache、动态身份及 HTTP/1.1 传输画像达到目标 |
| **OpenAI HTTP** | 非流式、SSE、工具、续轮和账号切换通过；入口语义、`call_id`、动态身份及 HTTP 传输画像达到目标 |
| **OpenAI WebSocket** | 首轮、续轮、工具、重连和 HTTP 回退通过；有效续轮、握手身份及 WS 传输画像达到目标 |
| **第三方客户端接入** | 六组合已使用真实 Kilo 界面完成 S1 与实际读取文件工具闭环。`0.1.164-6` 又单独重跑受本次修改影响的 OpenAI Responses → OpenAI WS：S1、S2 续轮、S4 单次读工具全部通过；Vircs 新增 4 条 usage，均命中 OAuth #90 且 `openai_ws_mode=true` |
| **模式独立性** | 自动透传开关及四种 WS mode 均不绕过 Profile，按实际出站协议选择 HTTP 或 WS 画像 |
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

详细请求计数、字段差异、TLS 数据、运行编号和恢复记录统一保存在[实证 README](backend/internal/service/testdata/official_egress/README.md)。

##### 1.1.3.5 编码任务分解（已完成）

所有任务均按“实现一项 → 自动化测试 → Vircs 抓包实证 → 进入下一项”的门禁执行。

| 任务 | 主要产物 | 完成门禁 |
|---|---|---|
| **T1 · 源码落点与契约测试** | 三条真实出站调用点、结构夹具、非适用账号隔离测试和字段来源表 | 稳定复现改造前缺口，不改变非 OAuth 行为 |
| **T2 · Profile 公共骨架** | Resolver、`OfficialEgressContext`、账号资格判断、校验及脱敏日志 | OAuth 自动生效，非适用账号零变化，错误状态明确失败，HTTP/WS 生命周期正确 |
| **T3 · Transport 与连接池** | 三套 Transport/Dialer、代理/CA 支持及隔离连接池键 | direct 与代理抓包达标，无跨账号/Profile/CA 复用 |
| **T4 · Anthropic HTTP** | Claude Finalizer、system/cache、动态身份及 HTTP/1.1 Profile | S1/S2/S4、工具、参数、mitm 和 direct 通过 |
| **T5 · OpenAI HTTP** | Body/Header Finalizer、`call_id`、身份上下文及 HTTP Profile | 非流式/SSE/工具/续轮/重试及抓包通过 |
| **T6 · OpenAI WebSocket** | Handshake/Frame Finalizer、WS Dialer及重连边界 | 首轮、有效续轮、工具、未知帧、身份冻结及抓包通过 |
| **T7 · 回归、性能与合并** | 全量/竞态/性能测试、三路径重抓及连续版本合并记录 | `0.1.164-6` 三路径 S1/S2/S4、direct TLS、应用层契约和 ingress→egress 语义守恒全部通过，未声明差异为 0 |
| **N2 · 第三方客户端适配** | Kilo 六种“入站协议 × 目标上游”组合及 OpenAI WebSocket 完整历史归一化 | 六组合 S1/S4 全部通过；`0.1.164-6` 又重跑 OpenAI Responses → OpenAI 的真实 Kilo S1/S2/S4，全部命中 WS 和 #90 |

T1–T7 与 N2 均已完成。`0.1.164-6` 已闭合上一版三条 OAuth 对比中的误报口径和 Codex WS 真实字段缺口；上表未列的 N1 是 `/v1/models`、Chat Completions 和 Gemini Native 的关联契约补测，不属于 Official Egress Profile 的核心改造范围。

**结论**：架构债务、Profile 升级、三条 OAuth 官方出站契约和真实 Kilo 六组合均已闭合。剩余 AnyRouter A/B 是外部中转兼容性验证，不改变当前官方平台边界的验收结论。

### 1.2 API Key 官方客户端兼容

API Key 官方客户端兼容让 Kilo / Cline / Cursor / Roo Code 等非官方客户端尽量接近 Claude Code / Codex CLI 在 **API Key 认证模式**下的 header、body、TLS 和路由形态。OAuth 和 API Key 共享经抓包证明一致的 `ClientBuild` 与传输数据，但认证、端点、beta、billing、cache 和 WS 开关仍是独立 Profile，不会把 OAuth 最终请求整体复制给 API Key。

当前默认画像已直接切换为 Claude Code CLI `2.1.220` 和 `codex_exec/0.145.0`；旧 Claude Desktop `2.1.209` 与 Codex Desktop `0.144.0-alpha.4` 仅作为服务级 `previous` 回退画像保留。不设账号级灰度或字段混用。

1. 仅 Anthropic / OpenAI 的 API Key 账号生效，不改变 OAuth 账号逻辑。
2. mimic 与 passthrough 运行时互斥；同时开启时，非官方客户端优先走 mimic。
3. 账号测试按非官方入站请求处理并走与正式 Gateway 相同的 Profile 解析、Header/Body Finalizer 和 TLS 决策。
4. 非官方客户端命中 mimic 时，关键身份 header 不允许被账号级 header override 覆盖；官方客户端跳过 mimic 后不应用该保护。
5. OpenAI Codex mimic 的 `/v1/messages` 固定进入 Responses mimic 链路，不受 `force_chat_completions`、普通 Responses probe false 或 `openai_responses_supported=false` 影响。
6. OpenAI Codex mimic 当前只支持 HTTP/SSE 上游，命中时不进入 Responses WebSocket；跳过 mimic 的官方 Codex 客户端按普通 API Key 账号的全局/账号 WS 开关、force HTTP 和 WSv2 mode 选择路由。该判断同时作用于账号调度、粘性账号复核和最终转发。
7. 官方客户端识别只依据 Gateway 入站请求上下文（UA、`originator`、`metadata.user_id`）。没有入站上下文的内部入口不做该识别，因此不会因为实际发起方是官方 CLI 而跳过 mimic。

三个入口的实际行为：

| 入口 | 客户端识别 | Anthropic | OpenAI |
|---|---|---|---|
| Gateway 公网入口 | 按 UA / `originator` / `metadata.user_id` 识别 | 官方客户端跳过 mimic | 官方客户端跳过 mimic |
| 管理后台账号测试 | 不识别（传入空 Gin Context） | 走当前 Claude Code CLI API Key 画像 | 走当前 Codex CLI API Key 画像 |
| keeper 内部代理 | 不识别 | `/v1/messages` 开关打开即走当前画像 | 不做 mimic，按 header allowlist 透传官方 Codex CLI 形态 |

```json
{"anthropic_apikey_mimic_claude_code":true,"openai_apikey_mimic_codex_cli":true,"enable_tls_fingerprint":true}
```

两个开关名与当前 CLI 目标一致。新的 API 基准已使用与 OAuth 相同版本的 Claude Code / Codex CLI，并让它们以 API Key 模式连接 Sub2API；后续升级可由一次自动化任务顺序覆盖两种认证、端点、HTTP/WS、direct/MITM 和 S1/S2/S4，但每个场景仍生成独立证据与 Profile。

| 平台 | mimic 目标 | 当前行为 |
|---|---|---|
| Anthropic API Key | Claude Code CLI `2.1.220` | `/v1/messages` 发送 `x-api-key` 且移除 Bearer 认证；UA、Linux/x64、Node `v26.3.0`、beta、`sdk-cli` billing、system 与 cache 形态来自 API 抓包。`/count_tokens` 暂使用独立 generic Contract，不冒充 messages 官方样本。 |
| OpenAI API Key | `codex_exec/0.145.0` | 默认 profile 为 `codex_exec_0_145`，发送 `originator=codex_exec`、`remote_compaction_v2`、`responses-lite=true`，不发 `OpenAI-Beta` 和 `version`；metadata 使用 `sandbox=seccomp`，`parallel_tool_calls=false`。API Key mimic 仍强制 HTTP/SSE，WS 画像只存档不激活。 |

Profile 数据集中在 `official_client_profile_registry.go`，每个 Wire Profile 都包含不可变 ID、抓包来源、适用端点、传输画像和 SHA-256 digest。默认使用 `gateway.official_client_profiles.mode: active`；紧急时可整体切换为 `previous`，未知模式或不完整画像会 fail-closed。历史账号字段 `openai_apikey_mimic_codex_profile` 仅作 dormant 数据保留，运行时不再覆盖服务级指针。

#### previous 回退画像历史记录

> 本节下方的 Desktop 字段只用于说明 `previous` 兼容性来源，不是当前 `active` 出站契约，不得作为新默认值引用。

##### Anthropic 1M 上下文 beta

非官方客户端命中 Anthropic API Key Desktop mimic 时，基础 `anthropic-beta` 固定对齐 2026-07-13 官方 Claude Desktop 抓包，并保持以下顺序：

```text
claude-code-20250219,
context-1m-2025-08-07,
interleaved-thinking-2025-05-14,
mid-conversation-system-2026-04-07,
effort-2025-11-24,
fallback-credit-2026-06-01
```

`context-1m-2025-08-07` 已是桌面基线的第 2 项，不再只按模型临时插入；保留的 `APIKeyMimicBetasWithContext1M()` 调用接口返回同一列表，避免重复 token。

| 场景 | beta 尾部 | 1M beta |
|---|---|---|
| 普通 `/v1/messages` | `effort-2025-11-24,fallback-credit-2026-06-01` | 固定为基础列表第 2 项。 |
| `output_config.format.type == "json_schema"` 的 `/v1/messages` | 将末尾 `fallback-credit-2026-06-01` 替换为 `structured-outputs-2025-12-15`。 | 保持基础列表第 2 项。 |
| `/v1/messages/count_tokens` | 基础列表后追加 `token-counting-2024-11-01`；无官方样本证明前不套用 structured outputs 条件切换。 | 保持基础列表第 2 项。 |

上述 beta 重写只属于实际命中 mimic 的请求；经 Gateway 入站的官方 Claude 客户端跳过 mimic，并保留其自身请求形态。

| 项目 | `/v1/messages` | `/v1/messages/count_tokens` |
|---|---|---|
| TLS | 默认使用标准 Transport，不再自动套用旧 Claude CLI 2.1.207 / Node.js 26 profile；管理员显式选择的固定或随机 TLS profile 仍优先。 | 使用相同规则。 |
| 压缩 | 始终显式发送 `Accept-Encoding: gzip, deflate, br, zstd`，由受控转发链解压，账号 header override 不得覆盖。 | 不照搬显式压缩头。 |
| 身份与会话 | 同时发送 `x-api-key` 和 Bearer token；移除 `x-client-request-id`、`x-stainless-helper-method`；从最终 `metadata.user_id` 提取同源 session ID。 | 只应用已确认的 Claude mimic header，不添加 SDK CLI 身份块或 session header。 |
| Body | 使用官方桌面抓包确认的 `cc_entrypoint=claude-desktop-3p` 和 Claude Agent SDK 身份提示；不自动补 `temperature=1`；仅删除无附加语义的 `tool_choice: {"type":"auto"}`，带 `disable_parallel_tool_use` 等附加字段时保留。 | 只应用已确认的 body 清理和工具名处理，不照搬 structured outputs 规则。 |
| Keeper 工具 | keeper 专用内部代理把精简 CLI 工具集合规范化为下述 27 项 Desktop 基线；同名真实工具保留原 schema，缺失项使用不可用说明和最小对象 schema。 | 不适用。 |

##### Anthropic / AnyRouter 旧账号测试记录

`v0.1.155-3` 将 Anthropic API Key 账号测试从“仅替换 `anthropic-beta`”改为复用正式 Gateway 的完整 Desktop mimic 构造链，并修复 AnyRouter 返回 HTTP 200 降级文案时被误判为成功的问题。后台测试接口为 `POST /api/v1/admin/accounts/:id/test`，实现和后续维护必须遵守以下分支优先级：

1. 经 Gateway 入站的官方 Claude Desktop / CLI 请求识别为官方客户端，跳过 API Key mimic；账号同时开启 `anthropic_passthrough` 时可继续进入 passthrough。
2. 非官方客户端同时命中 `anthropic_passthrough` 和 `anthropic_apikey_mimic_claude_code` 时，优先使用 mimic。
3. 管理后台账号测试必须按非官方入站请求处理，兼容开关开启后优先使用 mimic，不得因为同时开启 passthrough 而切换到透传。当前实现向 `GatewayService.buildUpstreamRequest` 传入空 Gin Context，保证测试不被误识别为官方客户端。
4. API Key 的普通模型映射和通配符模型映射必须先于测试请求构造执行，最终映射模型同时用于请求 body 和 mimic 规则判断。

AnyRouter 测试请求不得只修改单个 header，必须满足以下完整请求契约：

| 项目 | 要求 |
|---|---|
| 基础请求 | `model` 使用映射后的模型；prompt 固定为 `hi`；`max_tokens=512`；`stream=true`。 |
| 客户端身份 | UA 固定为 `claude-cli/2.1.209 (external, claude-desktop-3p, agent-sdk/0.3.209)`；`anthropic-beta` 按本节前述 6 项 Desktop 基线及顺序发送，其中 `context-1m-2025-08-07` 固定为第 2 项。 |
| 工具 | 测试 payload 必须携带下列 27 个官方工具名，每个工具至少包含合法的对象型 `input_schema`。 |
| System | Gateway 最终 body 必须包含 3 段 system block：billing attribution、Claude Agent SDK 身份文本和 Claude Code system prompt expansion；billing block 使用 `cc_entrypoint=claude-desktop-3p`。 |
| 会话与缓存 | 生成合法 `metadata.user_id`；`X-Claude-Code-Session-Id` 必须与 metadata 中的 session 一致；继续执行 mimic 的 cache-control 规范化。 |
| 鉴权与覆写 | 同时发送 `x-api-key` 与 `Authorization: Bearer <API Key>`；mimic 的受保护身份 header 不允许被账号级 header override 覆盖。 |
| TLS | 默认使用标准 Transport；只有管理员显式选择固定或随机 TLS profile 时才使用对应 profile，与正式转发规则一致。 |

```text
Agent, Bash, CronCreate, CronDelete, CronList, DesignSync, Edit,
EnterWorktree, ExitWorktree, Monitor, NotebookEdit, PushNotification,
Read, ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskCreate,
TaskGet, TaskList, TaskOutput, TaskStop, TaskUpdate, WebFetch,
WebSearch, Workflow, Write
```

BetaPolicy 必须区分静态身份 beta 和动态功能 beta：Desktop 静态身份基线（包括 `context-1m-2025-08-07`）不得被默认过滤策略剥离；根据结构化输出 body 动态加入的 `structured-outputs-2025-12-15` 仍受 BetaPolicy 控制。管理员禁用 structured outputs 时必须拒绝对应请求，不能以保护 1M beta 为由放开该动态 beta。

| 上游结果 | 后台测试判定 |
|---|---|
| HTTP 状态码不是 200 | 失败，显示上游状态码和响应正文。 |
| HTTP 200，收到正常 SSE 内容并以 `message_stop` 结束 | 成功，最终发送 `{"type":"test_complete","success":true}`。 |
| HTTP 200，但 SSE 正文仅为 `Service temporarily unavailable. Please retry later.` | 失败，发送错误事件，不得发送成功的 `test_complete`。 |

上述判定已在 `0.1.155-3` 的 AnyRouter ARM64 实测中验证通过，但它只证明旧 Desktop `previous` 画像。当前 CLI `active` 画像的 AnyRouter A/B 尚未执行。测试记录和文档不得包含账号密钥、Admin API Key、数据库密码或 scoped proxy token。

#### 当前发送与回退契约

OpenAI `/v1/responses/compact` 是特例：上游保持官方 unary JSON 形态，移除 `stream`、`store`、`prompt_cache_key`、`client_metadata`，不补 Codex mimic body 默认值，并强制 `Accept: application/json`。通过普通 `/v1/responses` body-signal 触发且原请求为 `stream:true` 时，下游桥接为最小 Responses SSE，确保包含 `response.output_item.done` 和 `response.completed`。

- mimic 只对齐 header、body、TLS 和路由，不复制服务端隐藏 prompt、账号状态、产品 memory 或 UI 上下文，也不替换响应文本或清洗客户端正文身份。
- Codex CLI `active` 画像在开启 TLS fingerprint 时使用 2026-07-26 抓包的 direct 30-cipher/空 ALPN 或 proxy h2 传输画像；管理员显式选择的 TLS profile 仍优先。API Key WebSocket 由路由决策层强制回落 HTTP，不会因注册表中存在未激活 WS 画像而开启。
- 效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。

`Anthropic-Dangerous-Direct-Browser-Access: true` 仅作为 Claude Desktop `previous` 画像的历史字段保留；当前 Claude Code CLI `active` 画像以 2026-07-26 API 抓包为准。

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

| 主题 | 当前行为 |
|---|---|
| 默认集合 | `model_mapping` 缺失或为空时，旧账号默认允许并展示官方 8 模型；新账号默认写入其自映射。管理员可通过“自定义模型名称”追加模型，手动“同步上游支持的模型”保存真实结果。 |
| 显式白名单 | 非空 `model_mapping` 中规范化后的 `model -> model` 自映射构成唯一允许集合，官方模型不会隐式补回；显式配置但无法解析出有效字符串映射时按空白名单处理。 |
| 映射与别名 | 模型映射保存“客户端请求模型名 -> 实际发包 model”，键值为模型 ID，不是上表的界面显示名；普通映射、通配符和 `gemini-3.1-pro-high` 等历史别名只负责解析，最终目标必须命中允许集合，历史别名不进入默认白名单或 `/models`。 |
| 模型广告 | `/antigravity/v1/models` 只展示账号实际允许的界面模型和手动加入的模型，不展示兼容别名。 |
| `web_search` | 固定使用 `gemini-3.5-flash-low`；存在显式白名单时必须保留该模型的自映射，不能绕过白名单。 |
| 官方伪装 | UA 为 `antigravity/hub/2.2.1 darwin/arm64`；默认 8 模型忽略客户端 `thinking` / `output_config.effort`，使用表中固定预算；在内层 `request.labels` 补 `model_enum/trajectory_id` 等官方标签并生成同源 `requestId`，过滤无关 stop / sampling 参数。手动追加模型按其名称发包，无需进入全局官方模型表。 |
| 计费 | 按最终实际发包模型 `UpstreamModel` 查价，日志保留外部模型，且优先于渠道 `requested` / `channel_mapped`；`gpt-oss-120b-medium` 为 `$0.05 / $0.01 / $0.20` 每 1M tokens。 |

### 1.4 账号保活

账号保活用于在 OpenAI / Anthropic API Key 账号空闲超过配置间隔后，通过官方 `codex` / `claude` 客户端在真实项目目录发起低频请求，维持上游账号活跃。

1. 主服务管理配置、账号引用、成本、最近使用时间和历史；`keeper/` 以独立 `sub2apiplus-keeper` sidecar 运行，负责调度官方客户端、worker 目录、会话、日志和本地 Web/API。
2. OpenAI 使用 `codex`，Anthropic 使用 `claude`；仅支持相应平台的 API Key 账号，OAuth、setup-token、upstream 等账号不进入候选。
3. 不同账号可并行执行，同一账号通过 `Running` 状态防止重复执行。
4. keeper 获取按账号、按平台签发的短期 scoped proxy token；官方客户端进程不能获得全局 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN`。
5. 只有当前可调度且具备有效平台 API Key 的账号进入候选；停用、过期、过载、限流、临时不可调度或配额耗尽的账号不返回、不签发 token，恢复后自动重新进入。账号代理入口在实际执行前再次校验状态。
6. 官方客户端单次执行超时默认 2700 秒；`sub2apiplus.timeout_seconds` 仅控制 keeper 调主服务内部接口的超时，默认 180 秒。

保活配置位于“Plus 增强功能 / 账号保活”；账号级设置保存在 `account.Extra`，全局约束和题库保存在 keeper state：
| 配置 | 说明 |
|---|---|
| 保活账号 | 从当前可调度且具备有效凭据的 OpenAI / Anthropic API Key 账号中选择。 |
| 启用保活 | `keeper_keepalive_enabled`，控制是否参与自动保活。 |
| 保活间隔 | `keeper_keepalive_interval_minutes`，默认 8 分钟，最小 1 分钟。 |
| 工作时间 | 默认 `04:00` - `24:00`。 |
| 执行模式 | OpenAI 默认全新会话 `fresh`，Anthropic 默认接续上次会话 `resume_last`。 |
| 模型 | 从账号可用模型列表选择。 |
| 项目 | 从 `SUB2APIPLUS_KEEPER_PROJECTS` 暴露的项目列表选择，keeper 内部映射到 `/workspace/projects/<项目名>`。 |
| 最大输出 token | `keeper_keepalive_max_output_tokens`，默认 512；主服务按该值钳制请求，硬上限为 1024。 |
| prompt 约束和题库 | 全局只读约束、通用问题、项目问题和账号自定义 prompt。 |
| 模型成本 | 复用 sub2apiplus 现有成本和 usage 统计口径。 |

keeper 的调度与失败处理：

1. keeper 按 `scan_interval_seconds` 周期扫描账号；本仓库示例配置为 30 秒，未配置时程序默认 120 秒。
2. 下次触发时间取“最近真实请求”和“最近成功保活”分别加保活间隔后的较晚值；账号持续使用时会自动顺延，不产生额外保活请求。
3. 手动“立即执行”忽略空闲判断，但同账号运行中时不会重复启动。
4. 失败会记录错误和失败次数，并按账号间隔重新排队；客户端断开、服务退出等非业务失败不会一律累计为连续失败。
5. 收到 `SIGTERM` / `Interrupt` 后，keeper 会取消运行上下文、等待任务收尾并关闭持久连接。

Anthropic keeper 通过主服务内部代理转发 Claude CLI 请求。该链路必须遵守以下兼容约束（OpenAI keeper 的内部代理不做 mimic，按 header allowlist 透传官方 Codex CLI 形态，见第 1.2 节入口表，下述约束不适用）：

1. 账号开启 `anthropic_apikey_mimic_claude_code` 时，keeper 的 `/v1/messages` 必须复用账号测试和正式 Gateway 的当前 Claude Code CLI API Key Profile（keeper 内部代理不做官方客户端识别，见第 1.2 节入口表）；同时开启 passthrough 时仍由 mimic 优先。只有 mimic 关闭时才原样转发官方 CLI 请求。
2. mimic 前必须执行账号模型映射和 `max_tokens` 钳制；最终 UA、billing 版本、beta、system、metadata、session、cache 和鉴权规则必须由同一 Registry Profile 解析，CWD 等项目上下文移入 messages 保留。
3. `enable_tls_fingerprint=true` 但未显式选择 `tls_fingerprint_profile_id` 时，内部代理必须使用标准 Transport，不得自动套用内置 Node.js profile；该规则与 Anthropic 账号测试一致。
4. keeper 的权限限制会使 Claude CLI 只上报 `Read` 等少量工具；mimic 内部代理必须按当前官方画像基线重排工具：保留同名真实工具定义、删除基线外工具、补齐缺失工具；补齐项必须明确标记为不可用，不能因此放开 keeper 的 Shell、写文件或联网权限。
5. Claude CLI 必须使用稳定的 `--name keeper-<账号ID>`，并在真实子进程环境设置 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。`--name` 只负责会话标识，不能单独关闭自动标题；缺少环境开关时 CLI 仍可能后台发送 `tools=[]`、`thinking=disabled`、带结构化 `output_config` 的标题请求，该请求不属于保活正文，AnyRouter 会以 HTTP 429 拒绝。
6. 常驻 Claude 子进程必须显式继承 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<keeper_keepalive_max_output_tokens>`，与主服务请求钳制值保持一致。只把该值写入 `runtime.env` 而未传给 `exec.Command` 的环境，会使 CLI 仍按 64000 token 运行，并把上游的 512 token 截断误判为输出超限。
7. Claude CLI 版本只能用于判断客户端请求形态，不能把 AnyRouter 的 HTTP 429 直接归因于版本。应使用同一账号和模型对照新版 CLI；若新旧版本均为 429，继续检查内部代理的 TLS、header、body 和上游状态。
8. Claude CLI 对单次 429 最多自动重试 10 次，排查期间应暂停自动调度或使用受控单次请求，避免把一轮失败误判为多轮保活。
9. `api_retry` 中的瞬时 429 必须保留在 stdout 供审计，但不能覆盖同一轮后续的成功 `result`；最终有正常回复和非零 usage 时，keeper 应记录 `success` 并将连续失败次数清零。

验收时必须区分“最终失败的持续 429”和“最终成功前的瞬时上游重试”：2026-07-15 ARM64 实测（Claude CLI `2.1.210`）中，一轮保活的 6 个正文请求全部返回 HTTP 200，首个请求的 2 次瞬时 429 由同一 CLI 请求自动重试后恢复，最终仍记录为 `success`、usage 非零、连续失败归零。

| 视图 | 内容 |
|---|---|
| 概览 | keeper 版本、账号数、成功/失败、运行中账号、24 小时用量/费用、最近结果、下次时间和立即执行；账号列表只展示已启用目标。 |
| 配置 | 管理账号、模型、项目、间隔、工作时间、执行模式、账号 prompt、全局约束和题库；支持全部/已启用/已禁用筛选，已启用优先，同状态按账号 ID 倒序。 |
| 会话历史 | 展示时间、账号、状态、模型、token、费用、摘要、错误、提示词和 assistant 回复；从全部 target 汇总，停用账号的既有记录仍可查看，完整上游/客户端错误保留在错误详情中。 |

### 1.5 其它优化
1、Composite 与 Grok 同等豁免，实现Anthropic协议也能访问 GPT、Grok模型
2、Anthropic平台Oauth账号添加"模型限制"区块，复用现有 ModelWhitelistSelector 组件（含"同步最新支持模型 / 同步上游支持的模型 / 清除所有模型"三个按钮，和 OpenAI 页面一致），加载与保存逻辑把 anthropic && (oauth || setup-token) 纳入 model_mapping 持久化

## 2. 全新服务器部署

推荐使用 Docker Compose。主服务目录为 `/root/docker/sub2apiplus/app`；keeper 配置、数据和项目位于 `/root/docker/sub2apiplus/keeper/app`，构建源码位于 `/root/docker/sub2apiplus/keeper/repo`，其中 `app/projects/<项目名>` 挂载到 `/workspace/projects/<项目名>`。以下流程统一使用运行目录中的 `docker-compose.yml`。

> **Vircs 现有实例说明（2026-07-26）**：该服务器的历史目录实际为 `/root/Docker/sub2apiplus`，其中 `Docker` 首字母大写；Linux 路径区分大小写。维护既有实例时应先读取容器的 `com.docker.compose.project.working_dir` 标签确认真实目录，不得直接照抄新部署的推荐路径。

主服务和 keeper 属于同一仓库、同一发布版本，但分别以 GHCR 镜像和本地构建 sidecar 的形式部署。生产环境必须先选择一个已发布的 Plus 版本，并同时使用 `ghcr.io/itv3/sub2apiplus:<Plus版本>` 和 Git tag `v<Plus版本>`；不能把主服务 `latest` 与 keeper `main` 作为可复现的版本组合。`latest` / `main` 只适合持续跟踪最新代码的环境。

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
sed -i "s#ghcr.io/itv3/sub2apiplus:latest#ghcr.io/itv3/sub2apiplus:${VERSION}#" docker-compose.yml
mkdir -p data postgres_data redis_data
```

主服务 `.env` 至少要设置：

```env
COMPOSE_PROJECT_NAME=sub2apiplus
POSTGRES_PASSWORD=<随机强密码>
JWT_SECRET=<openssl rand -hex 32>
TOTP_ENCRYPTION_KEY=<openssl rand -hex 32>
ADMIN_PASSWORD=<自定义后台密码，可选>
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<openssl rand -hex 32>
SUB2APIPLUS_KEEPER_PROJECTS=homeproxy
# 可选；主服务代理 keeper 时使用。留空默认 http://sub2apiplus-keeper:38090
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

下载保活项目。`SUB2APIPLUS_KEEPER_PROJECTS` 使用单级项目名，不接受绝对路径、`..` 或多级路径；同名目录必须放在 keeper 的 `projects` 下：

```sh
cd /root/docker/sub2apiplus/keeper/app/projects
git clone --depth 1 https://github.com/itv3/homeproxy.git homeproxy
```

多个项目用英文逗号分隔，例如 `SUB2APIPLUS_KEEPER_PROJECTS=homeproxy,sub2apiplus`，并确保两个同名目录均存在。

准备 keeper `.env`，其中 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 必须与主服务 `.env` 完全一致：

```env
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<与主服务相同>
KEEPER_BIND_HOST=127.0.0.1
KEEPER_HOST_PORT=38091
# 留空表示关闭 keeper 独立 Web 的 Basic Auth 入口，仅允许内部 token 访问；详见 3.2 节
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

生成 keeper compose：

```sh
cd /root/docker/sub2apiplus/keeper/app
cat > docker-compose.yml <<'YAML'
name: sub2apiplus-keeper

services:
  keeper:
    build:
      context: ../repo
      dockerfile: Dockerfile
    image: sub2apiplus-keeper:latest
    container_name: sub2apiplus-keeper
    restart: unless-stopped
    cap_add:
      - CAP_SYS_ADMIN
    security_opt:
      - seccomp=unconfined
      - apparmor=unconfined
    env_file:
      - ./.env
    environment:
      - KEEPER_CONFIG=/app/keeper.yaml
    volumes:
      - ./keeper.yaml:/app/keeper.yaml:ro
      - ./data:/app/data
      - ./projects:/workspace/projects:ro
    ports:
      - "${KEEPER_BIND_HOST:-127.0.0.1}:${KEEPER_HOST_PORT:-38091}:38090"
    networks:
      - sub2api-network

networks:
  sub2api-network:
    external: true
    name: sub2apiplus_sub2api-network
YAML
```

`sub2api-network.name` 必须与主服务网络一致；设置 `COMPOSE_PROJECT_NAME=sub2apiplus` 后固定为 `sub2apiplus_sub2api-network`，可用 `docker network ls | grep sub2apiplus` 确认。

构建并启动 keeper。标准更新必须禁用构建缓存，避免复用旧的官方客户端安装层：

```sh
cd /root/docker/sub2apiplus/keeper/app
docker compose build --pull --no-cache --build-arg VERSION="${VERSION}" keeper
docker compose up -d keeper
docker exec sub2apiplus-keeper /app/sub2apiplus-keeper -version
docker exec sub2apiplus-keeper codex --version
docker exec sub2apiplus-keeper claude --version
```

构建完成后，keeper 版本必须与主服务 Plus 版本一致，`claude --version` 必须与 `keeper/Dockerfile` 的 `CLAUDE_RELEASE` 一致；任一不一致时不得继续保活验证。`codex --version` 只记录本次实际安装的版本，`CODEX_RELEASE` 为 `latest` 时该值会随重建变化。更新只替换 keeper 容器，`data`、`runtime`、`projects` 和配置挂载保持不变。

keeper 需要 `CAP_SYS_ADMIN`、`seccomp=unconfined`、`apparmor=unconfined` 以运行官方客户端和 `bubblewrap` 沙箱；`data`、`runtime`、`projects` 必须持久化。

### 2.3 后台激活和验证

1. 在后台添加 API 账号，确认模型和成本配置正常。
2. 进入“Plus 增强功能 / 账号保活 / 配置”，添加 OpenAI / Anthropic API Key 账号；平台自动选择 `codex` / `claude`。
3. 配置模型、项目、间隔、工作时间、执行模式、全局约束和题库后保存。
4. 点击“立即执行”，到“会话历史”确认回复、token 和费用。
5. Anthropic 任务执行期间检查真实 Claude 进程，确认命令包含 `--name keeper-<账号ID>`，环境包含 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<账号配置值>` 和 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。
6. 任务结束后确认最终状态为 `success`、usage 非零、连续失败次数归零；stdout 中最终成功前的 `api_retry` 瞬时 429 仅作为审计信息，不能单独判为保活失败。

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
# 下面两条需要在 Anthropic 保活任务执行期间运行
docker exec sub2apiplus-keeper pgrep -af -- '--name keeper-'
docker exec sub2apiplus-keeper sh -lc 'pid=$(pgrep -o -f -- "--name keeper-"); tr "\0" "\n" < "/proc/${pid}/environ" | grep -E "^CLAUDE_CODE_(MAX_OUTPUT_TOKENS|DISABLE_NONESSENTIAL_TRAFFIC)="'
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
VERSION=0.1.164-1
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

本仓库中的主服务和 keeper 可能在同一次发布中修改。凡 Release notes 标明 keeper 或 keeper 内部代理有改动，必须先更新主服务，再从同一个 tag 更新 keeper 源码并无缓存重建；不能只更新其中一方。AMD64 / ARM64 均使用以下流程，不动 PostgreSQL、Redis、volume、keeper 数据目录和 `.env`：

`v0.1.161` 新增的 `184_auth_cache_invalidation_outbox.sql` 会在 API Key、用户和分组相关表上安装触发器，且没有自动反向迁移。升级前必须备份数据库；若回退到旧应用，应同时采用经审核的触发器停用或反向迁移方案，不能只回退镜像，否则 outbox 会继续累积。

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

不要执行 `docker compose down -v`，不要删除 volume，不要覆盖 `.env`。

Watchtower 只能拉取并替换镜像仓库中的主服务镜像。按本文部署的 keeper 使用本地源码构建镜像 `sub2apiplus-keeper:latest`，没有对应的 GHCR 发布镜像；Watchtower 不会拉取 Git tag、替换 `/root/docker/sub2apiplus/keeper/repo` 或执行 `docker compose build`，因此不会自动更新 keeper。启用 Watchtower 的服务器在主服务自动更新后仍需按上述第 2、3 步手动更新 keeper；否则主服务和 keeper 会处于不同版本。要求严格版本配套时，应固定主服务镜像版本，并在发布后统一执行完整的三步升级流程。

### 3.5 `0.1.164-6` Vircs 发布与复验记录

2026-07-26 已发布注释标签 `v0.1.164-6`，对应提交 `e8b5c6051`。Release workflow 重跑后成功生成多架构 GHCR 镜像；Vircs 只替换主服务镜像，没有重建 PostgreSQL、Redis，也没有修改 `.env` 或数据卷。keeper 本轮无代码变更，继续运行上一版镜像。

| 验收项 | 结果 |
|---|---|
| 主服务 | `ghcr.io/itv3/sub2apiplus:0.1.164-6`，manifest digest `sha256:0795f476b627e51b839a8c3aba350e85a185efe8e4e30e56d17df72673f7b7d9`；`/app/sub2api --version` 为 `0.1.164-6` / `e8b5c6051`，状态 `healthy`、重启次数 `0`。 |
| keeper | `sub2apiplus-keeper:0.1.164-5`，本轮未改 keeper；进程运行且重启次数 `0`。 |
| OAuth MITM | Claude HTTP、Codex HTTP、Codex WS 的 S1/S2/S4 全部 `valid=true`；三条 `contract_equal=true`、ingress→egress 语义守恒、未声明差异为 0，Codex WS 逐项 turn metadata 校验通过。 |
| OAuth direct | `v01646-oauth-egress-direct-20260726T060439Z` 共 9 个场景全部有效；业务画像分别匹配官方 17/30/10-cipher 及对应 ALPN。合并 pcap 中另有一个 13-cipher OAuth 辅助连接，不参与三条业务 Profile 判定。 |
| Kilo 复验 | OpenAI Responses / `gpt-5.6-luna` 的 S1、S2 续轮、S4 单次读取 `backend/go.mod` 全部通过；2026-07-26 14:11:32–14:12:22 的 4 条 usage 均命中 #90 且 `openai_ws_mode=true`。 |
| 客户端版本 | 抓包容器为 Claude Code `2.1.220`、Codex CLI `0.145.0`；keeper 镜像仍按其 Dockerfile 固定 Claude Code `2.1.210`，Codex CLI 实际安装为 `0.145.0`。keeper 版本不能冒充抓包画像来源。 |
| 外部 A/B | `external_ab_executed=false`；AnyRouter 账号恢复前不标记为通过，Codex API Key mimic 继续保持 HTTP/SSE-only。 |
| Vircs 直接编译 | 当前完整工作区已同步到 `/root/sub2apiplus-build/v01646-final-20260726T062500Z`；Vircs 使用 Go `1.26.5` 执行 `go test ./...` 全部通过，Docker 多阶段构建（含前端生产构建和后端 `embed` 编译）成功。隔离验证镜像为 `sub2apiplus-vircs-test:v01646-final`，镜像 ID `sha256:d9ac65dc1ce45af5ab145a1f37d075863dae950a5e968591d9b02b2e8c12b981`，二进制版本为 `0.1.164-6-vircs-test`。本次补充验证未提交到 GitHub 编译，也未替换已通过真实流量验收的生产镜像。 |

MITM 脱敏对比保存在 Vircs `/root/oauth-capture/runs/official-client/comparisons/v01646-oauth-egress-20260726T055650Z`；候选原始运行目录分别为 `phase0-sub2api-claude-mitm-20260726T055650Z`、`phase0-sub2api-codex-http-mitm-20260726T055650Z` 和 `phase0-sub2api-codex-ws-mitm-20260726T055650Z`。direct pcap 位于 `/root/oauth-capture/runs/v01646-oauth-egress-direct-20260726T060439Z`。原始 pcap、MITM Body 和 CLI 事件继续按私有证据管理，不提交 Git。

### 3.6 其它运行能力

Gemini 支持内置 Gemini CLI OAuth Client 的 Code Assist OAuth、通过 `.env` 配置 `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET` 的 AI Studio OAuth，以及后台直接添加 API Key。

后台“数据管理”入口当前仅保留兼容诊断；服务端固定返回 `DATA_MANAGEMENT_DEPRECATED`，不建议新部署 `datamanagementd`，也不要按旧流程挂载 `/tmp/sub2api-datamanagement.sock`。数据迁移优先使用第 3.3 节的本地目录迁移流程；数据库备份请在 PostgreSQL / Redis 层独立执行。

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径，不安装 keeper sidecar；需要账号保活时使用 Docker Compose 部署。

TLS fingerprint 的 profile、ALPN 和 HTTP/2 行为见第 1.2、4.3 节。账号需启用对应 mimic 开关和 `enable_tls_fingerprint`；Anthropic 还必须显式选择 `tls_fingerprint_profile_id`，否则走标准 Transport，OpenAI 未显式选择时回落到内置 Codex profile。

OpenAI WS 账号使用 `http_bridge` 模式前，必须先开启 WS v2 模式路由：在配置文件设置 `gateway.openai_ws.mode_router_v2_enabled: true`，或设置环境变量 `GATEWAY_OPENAI_WS_MODE_ROUTER_V2_ENABLED=true`。WS 连接使用 Redis 60 秒租约并每 20 秒续租；若连续一个完整租期无法确认租约，实例会主动关闭本地连接，避免多实例同时持有同一会话。

## 4. 维护参考

### 4.1 keeper 内部接口

sub2apiplus 提供内部接口给 keeper 和 Plus 增强功能页面使用。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活、当前可调度且具备有效平台 API Key 凭据的 OpenAI / Anthropic API Key 账号，以及模型、prompt、项目、最大输出 token、最近使用时间、下一次时间和 due 判断。 |
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

### 4.2 v0.1.164 差异文件清单

发布基线为上游 tag `v0.1.164`，当前分支已通过合并提交同步；审核 Plus 实现时优先看 `v0.1.164..HEAD` 中 OAuth 官方出站、mimic、Antigravity、keeper 和 Plus UI。Composite 分组、Ollama Cloud 用量同步、`batch_image`、提示词安全审计和异步图像任务属于上游；`keeper/` 是新增源码，`.codex-captures/` 和 `.kilo/` 是本地样本或工具配置，不计入源码清单。完整差异用以下命令生成：

```sh
git diff --name-only v0.1.164..HEAD
git diff --stat v0.1.164..HEAD
```

| 范围 | 入口文件 |
|---|---|
| 官方客户端画像核心 | `backend/internal/service/official_client_profile_registry.go`（ClientBuild、Wire Profile、active/previous、digest 与抓包来源）、`official_egress_profile.go`（激活决策与可逆配置）。 |
| OAuth 官方客户端伪装 | `backend/internal/service/official_egress_*.go`（Resolver、三条路径 Finalizer、终态校验、Transport/Dialer）、gateway 与 OpenAI WS 转发链中的调用点、`backend/internal/repository/http_upstream.go`。 |
| API Key mimic | `backend/internal/service/*apikey_mimic*`、OpenAI gateway/scheduler/WS 相关 service、`backend/internal/pkg/tlsfingerprint/`、`backend/internal/repository/http_upstream.go`、`backend/internal/handler/openai_gateway_handler.go`。`backend/internal/pkg/claude/constants.go` 已恢复为上游共享默认值，不再存放 mimic 画像。 |
| Antigravity | `backend/internal/pkg/antigravity/`、`backend/internal/service/antigravity_*`、`upstream_models.go`、`model_rate_limit.go`、`backend/resources/model-pricing/model_prices_and_context_window.json`。 |
| 账号保活 | `keeper/`、`backend/internal/handler/admin/account_handler_keeper.go`、`backend/internal/service/*keeper*`、`backend/internal/server/routes/admin.go`。 |
| Plus UI | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、账号 API、路由、侧边栏、i18n、账号创建/编辑和模型白名单相关组件。 |
| 发布部署 | `README.md`、`deploy/.env.example`、`deploy/docker-compose.local.yml`、`.github/workflows/release.yml`、`backend/cmd/server/VERSION`。 |

### 4.3 上游合并检查

合并上游后按第 1 节功能规则重点确认：
**OAuth 官方客户端伪装**
- 三条 OAuth 出站路径继续自动命中 Registry 的服务级 `active/previous` 画像；账号级开关和任意版本字段不得回流到 API、数据库或管理端表单。
- Profile 只在入口端点、目标平台、OAuth 账号、实际出站协议和官方 Host 全部匹配时生效；API Key、其他平台和非模型入口保持原行为。
- 连接池键继续包含账号、Profile 版本、协议、Host、代理、CA 和 TLS Profile；HTTP 重试重建上下文，WebSocket 握手后冻结账号与身份。
- 自动透传开关和四种 WS mode 不绕过 Profile，按实际出站协议选择 HTTP 或 WebSocket 画像。
- Profile ID/digest/source、身份来源和最终 Method/Host/Path 受控；HTTP 禁止自动重定向，校验失败时明确失败并记录脱敏原因，不静默切换或拼装画像。
- OAuth 冲突配置必须作为 dormant 数据保留；新建或从关闭切到开启的冲突值要显式拒绝，不得在保存时静默删除。

**API Key mimic**
- Gateway 入站只对非官方客户端触发，官方 Claude / Codex 客户端回到 passthrough 或普通 API Key 逻辑；内部入口按第 1.2 节入口表处理，不做客户端识别；命中 mimic 时关键身份头不被账号 header override 覆盖。
- Anthropic 使用 mimic 专用完整 beta 列表，不影响普通 API Key；`/v1/messages` 与 `/v1/messages/count_tokens` 保持第 1.2 节的独立构造边界，工具名归一和 per-request reverseMap 只修改结构化工具字段。
- Anthropic API Key `active` 使用 Claude Code CLI 2.1.220 的局部 Header/Body/TLS 画像，不得修改 `pkg/claude` 共享常量或 `defaultFingerprint`；管理员显式选择的 TLS profile 仍优先。
- OpenAI mimic 强制 HTTP，跳过 mimic 后账号调度、previous response 粘连复核和最终转发都恢复普通 WS/HTTP 路由；`/v1/messages` 固定走 Responses mimic，compact 保持上游 JSON 并按需桥接下游 SSE。
- `codex_exec_0_145` 为默认 profile；`desktop_0_144` 及其旧别名仅属于 `previous`/配置兼容，`cli_rs_0_125` 保留独立历史兼容路径。当前 CLI 画像使用官方 direct/proxy TLS 数据和 HTTP/SSE 路由，管理员显式 TLS profile 继续优先。
- HTTP/1.1 与 HTTP/2 Transport 均保持 `DisableCompression=true`，避免自动注入 gzip，同时不影响显式压缩响应的受控解压；Responses capability probe 继续按 mimic 状态分键。
- `CodexBaseInstructionsForModel()` 保持 `gpt-5.5` / `gpt-5.2` 策略，未单独维护 prompt 的后续版本回退到最新版本（当前 GPT-5.5）。

**Antigravity**
- 新账号默认白名单、映射和 `/models` 统一为官方 8 模型，不产生重复模型；自定义模型和上游同步结果仍按真实配置保存。
- 显式白名单只由自映射构成，请求校验与 `/models` 使用同一允许集合；默认表、别名、通配符和 `web_search` 都不能重新放开已移除模型。
- 官方 UA、固定 `thinkingBudget`、labels、同源 `requestId` 和最终 `UpstreamModel` 计费保持有效，包括 `gpt-oss-120b-medium` 的既定价格。

**Keeper 与 UI**
- 账号列表、项目、settings/state、立即执行、最大输出 token 钳制和会话回写保持对齐；候选和实际代理都校验可调度状态，恢复后自动重新进入。
- Plus 路由、侧边栏、i18n、账号 API 和设置页与后端保持一致；mimic 与保活筛选、启用优先排序继续有效。
- 保活概览只展示启用目标，历史仍能读取停用目标记录；Antigravity 编辑页保留模型白名单和映射。
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

后续维护方向：
- 继续对齐官方客户端 body identity、system prompt、metadata 和工具调用形态。
- 为 Anthropic / OpenAI 提供独立 profile，包括 `anthropic_apikey_mimic_official_identity` 和 `anthropic_apikey_mimic_profile`，避免客户端形态互相污染。
- 展示账号实际出站 profile、TLS fingerprint 状态和最近一次 mimic 的脱敏诊断摘要，并提供仅脱敏导出的抓包入口。
- 增加脱敏差异采集和 A/B 消融辅助能力，以真实失败样本决定源码调整。
- UI 不得展示密钥、token、authorization 或 x-api-key；高风险 body cloaking 开关默认关闭或仅灰度启用。

## 5. 其它文档索引

以下文档位于 `docs/` 和 `deploy/`，不属于 Plus 四项增强的主线说明，但部署或二次开发时可能用到：

| 文档 | 说明 |
|---|---|
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
