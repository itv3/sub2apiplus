## 3. OAuth官方客户端伪装

### 3.1 目标与总结论

> **目标**：
1、Sub2API 对外提供标准 API 接口。无论入站来自官方客户端，还是通过标准 API 接入的第三方客户端，Anthropic/OpenAI OAuth 官方出站均自动使用对应版本 Claude Code / Codex CLI 的真实出站形态。
2、Sub2API 原有兼容层只负责入站协议转换、模型映射、工具转换及请求语义兼容；最终 OAuth 出站请求由内置官方客户端画像统一定型。
>
> **实测基线**：Claude Code `2.1.218`、Codex CLI `0.145.0`，Sub2API `v0.1.164`；模型固定为 `claude-sonnet-5`、`gpt-5.6-luna`。官方客户端基准与 Sub2API 出站使用相同 S1/S2/S4，完成 direct + mitm + ingress 配对；另使用 Kilo Code VS Code 扩展从标准 API 接入，完成 Anthropic/OpenAI HTTP 的 S1/S4 和 OpenAI WebSocket 的 S1/S2/S4。Anthropic/OpenAI OAuth 官方画像为内置默认能力，Profile 版本由服务端自动绑定为 `phase0-2026-07-24`，不提供账号级开关或版本选择。

| 当前路径 | Sub2API 实证（OAuth 官方画像内置生效） |
|---|---|
| **Anthropic HTTP** | 官方基准 S1/S2/S4 为 5/5；Kilo S1/S4 通过 |
| **OpenAI HTTP** | 官方基准 5/5、自动透传补测 1/1；Kilo S1/S4 通过 |
| **OpenAI WebSocket** | 官方基准和 Kilo 的 S1/S2/S4 均通过 |

当前最终部署镜像为 `sub2apiplus:official-egress-built-in-a2-20260725`，运行版本为
`0.1.164-1-n2`。账号级开关和版本字段已从数据库删除；无账号配置条件下的最终
direct 回归目录为 `/root/oauth-capture/runs/t2-off-direct-20260725T114012Z`，
MITM + ingress 三路径完整回归时间戳为 `20260725T114144Z`，结果见实证归档。

### 3.2 抓包方法、分组与证据边界

> **抓包操作手册**：抓包环境、脚本及启动、抓取、拉取、停止、恢复方法统一见 [`抓包相关/README.md`](/Users/czs/Developer/抓包相关/README.md)。本节只定义本任务的两轮抓取法、S1/S2/S4、分组关系和证据边界。

#### 3.2.1 抓包方法：两轮抓取法 + 三场景

同一组请求分别执行 direct 与 mitm 两轮，不能用其中一轮代替另一轮：

| 轮次 | 抓取方式 | 验证内容 | 不能证明 |
|---|---|---|---|
| **direct** | 客户端或网关直连官方上游，通过 `tcpdump` 抓取真实出站流量 | ClientHello、cipher、扩展、ALPN、HTTP/WS 协议选择及连接复用 | 加密后的 Header、Body 和 WS 帧 |
| **mitm** | 注入测试 CA，使目标流量经过 mitmproxy，并同时记录网关入站 | URL、HTTP 版本、Header、Body、system、tools、响应及 WS 帧 | 网关直连官方时的真实 TLS 指纹 |

每轮使用相同模型和相同场景：

- **S1**：冷启动简单问答，验证基础请求、响应和身份字段。
- **S2**：同一会话连续多轮，验证会话生命周期、连接复用和 `previous_response_id`。
- **S4**：完整工具闭环，验证工具定义、调用、结果回传、`call_id` 和最终回答。

执行要求：

1. 同一对比组使用相同客户端版本、模型、场景输入和重复次数；具体值写入运行元数据，本节不重复固定数值，当前验收基线以 §3.1 为准。
2. 一次只运行一个“主体 + 平台 + 路径”，避免连接池、会话、账号调度和抓包文件互相污染。
3. direct 轮不得插入 MITM；mitm 轮必须同时保存解密后的出站数据和网关入站数据。
4. 每轮记录镜像、客户端版本、模型、账号、Profile、实际出站协议、开始时间和运行目录。
5. 测试结束后恢复代理、CA、账号配置和服务状态，并检查无残留抓包进程。

#### 3.2.2 抓包分组与动作

分组编号描述固定的比较关系：`AH` 表示 Anthropic HTTP，`OH` 表示 OpenAI HTTP，`OW` 表示 OpenAI WebSocket；后缀 `1` 表示官方客户端基准，`2` 表示 Sub2API 出站。客户端版本、模型、账号和运行目录由每次运行元数据确定。

| 平台与路径 | 官方客户端基准 | Sub2API 出站 | 对比关系 |
|---|---|---|---|
| **Anthropic · HTTP** | AH-1（Claude Code） | AH-2 | AH-2 对标 AH-1 |
| **OpenAI · HTTP** | OH-1（Codex CLI） | OH-2 | OH-2 对标 OH-1 |
| **OpenAI · WS** | OW-1（Codex CLI） | OW-2 | OW-2 对标 OW-1 |

主基线的三组路径均执行相同的 S1/S2/S4；第三方客户端兼容性补测可以选取子集，
但不能替代主基线，当前 Kilo 覆盖范围以 §3.1 为准：

| 主体类型 | direct 动作 | mitm 动作 |
|---|---|---|
| **官方客户端** | 在客户端网络命名空间抓取直连官方流量 | 配置测试 CA 与代理，解密官方客户端请求 |
| **Sub2API** | 在 Sub2API 网络命名空间抓取直连官方流量 | 将被测账号绑定到 MITM，同时抓取 Sub2API 入站 |

HTTP 路径只能对标对应的官方 HTTP 基准，WebSocket 路径只能对标官方 WebSocket 基准。协议、模型或场景不同的样本只能用于辅助观察，不能计入一致性结论。

#### 3.2.3 样本与证据边界

- **传输层证据**：仅使用未终止 TLS 的 direct pcap 判定 ClientHello、cipher、扩展、ALPN、协议选择和连接复用。
- **应用层证据**：使用 mitm 解密记录判定 URL、HTTP 版本、Header、Body、system、tools、响应和 WS 帧。
- **Sub2API 改写证据**：必须保存同一次请求的入站和出站，比较规范化后的语义；不能只看出站结果。
- **样本有效性**：必须确认命中指定账号和模型、没有 API Key 或其他账号 fallback、请求成功完成，并能与 usage/请求日志对应。
- **续轮与工具**：S2 必须形成真实连续会话；S4 必须完成“工具定义 → 工具调用 → 工具结果 → 最终回答”，发出即失败的样本无效。
- **版本边界**：每次结论只适用于运行元数据记录的客户端、服务镜像和 Profile；版本变化后必须重抓，不能直接沿用旧数值。
- **安全边界**：Token、Cookie、API Key 和动态身份值必须脱敏；包含完整请求 Body 的原始样本按敏感材料管理。

当前实现的镜像、测试、运行目录和恢复记录统一归档在
`backend/internal/service/testdata/official_egress/README.md`。

### 3.3 Sub2API `v0.1.164` 改造方案与实施结果

> **基线与证据**：本节沿用 §3.1 的实测基线，证据归档入口见 §3.2.3。Profile 目标随官方客户端版本更新；不得把单个 JA3、动态身份值或完整 system 文本写成永久常量。

#### 3.3.1 方案结论与边界

1. **范围不变**：复用 `v0.1.164` 的 composite 路由、鉴权、账号调度、重试、流式响应、usage、会话粘性和代理系统；官方客户端伪装只修正三条 OAuth 官方出站路径，不改变 Key、Group、模型路由或计费归属。
2. **薄层实现**：在现有协议转换完成后增加统一的 Profile Resolver、`OfficialEgressContext`、Finalizer 和 Transport/Dialer Provider，不分叉完整 Handler。
3. **最小改写**：保留入口已经携带且上游支持的语义，只修正抓包已证明不一致的字段；不得合成官方内部工具、复制完整 system 或无依据展开历史。
4. **应用与传输同时对齐**：Anthropic HTTP、OpenAI HTTP、OpenAI WebSocket 分别维护独立 Profile，同时约束 Header/Body、TLS/ALPN、协议选择和连接复用。
5. **身份与失败边界明确**：动态身份必须来自入口、会话、响应映射或账号配置等可追踪来源。API Key 与其他非适用账号保持原行为；OAuth Profile、Host 或身份状态校验失败时必须明确失败并记录脱敏原因，不能静默切换画像。

#### 3.3.2 改造前主要缺口

| 路径 | 改造前主要缺口 | 必须保留的正确行为 |
|---|---|---|
| **Anthropic HTTP** | 使用了错误的客户端 UA、HTTP/TLS 画像和固定会话身份；system/cache 结构未按 OAuth 客户端形态处理 | 工具名、工具闭环及无 `temperature` 注入 |
| **OpenAI HTTP** | 自动注入无依据的 `instructions`，改写工具 `call_id`；Header 与 Body 身份脱节，传输画像不符 | 入口 `additional_tools`、`client_metadata`、`prompt_cache_key` 等原生 Responses 字段 |
| **OpenAI WebSocket** | 有效 `previous_response_id` 被丢弃并展开历史；续轮重复工具字段，握手身份和 WS Dialer 不符 | 首帧及入口 WS 帧的原始语义 |

三条路径还共同存在动态字段来源不统一、不同账号/Profile/代理/CA 可能错误复用连接的问题，必须由公共上下文和连接池键统一解决。

#### 3.3.3 三条路径改造结果

| 路径 | 应用层结果 | 传输层结果 |
|---|---|---|
| **Anthropic HTTP** | 修正 Claude Code UA、Beta、Header、system/cache 结构及 session/metadata 生命周期；保留原本正确的工具与参数语义 | 使用独立 Claude HTTP Profile，对齐官方 HTTP/1.1、TLS/ALPN 和连接行为 |
| **OpenAI HTTP** | 取消无依据的 `instructions` 注入，保留入口字段和 `call_id`；Header 与 Body 身份使用同一生命周期 | 使用独立 Codex HTTP Profile，并按代理边界选择对应传输画像 |
| **OpenAI WebSocket** | 保留有效 `previous_response_id`；第三方客户端的完整历史被归一化为预热、正式请求和最小工具结果续轮；握手时冻结 session/thread/window/turn 身份 | 使用专用 WS Dialer，对齐握手 Header、压缩协商、TLS/ALPN 和重连边界 |

Profile 只在入口端点受支持，且目标平台、OAuth 账号、实际出站协议和官方 Host 均匹配时生效。OpenAI 自动透传和 WS mode 只决定请求处理与实际出站协议：实际走 HTTP 时应用 HTTP Profile，实际走 WebSocket 时应用 WebSocket Profile，不会关闭或绕过官方客户端伪装。

#### 3.3.4 实际采用架构

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

#### 3.3.5 实施顺序（已全部完成）

| 阶段 | 任务 | 完成条件 |
|---|---|---|
| 0 · 基线 | 使用相同 S1/S2/S4 完成官方客户端与 Sub2API 的 direct、mitm、ingress 配对 | 三条路径的应用与传输差异可稳定复现 |
| 1 · 路由与契约 | 固化三路径路由、账号命中、usage、重试及必要的原生客户端契约 | 三条路径命中正确平台账号，非 Profile 契约问题独立处理 |
| 2 · 公共骨架 | 实现 Profile、上下文、字段来源、校验、脱敏日志和账号资格判断 | API Key 等非适用账号零行为变化，HTTP 重试重建上下文，WS 身份冻结 |
| 3 · 传输层 | 实现三套 Transport/Dialer 和隔离连接池键 | 直连、代理、CA、账号和 Profile 之间无错误复用 |
| 4 · 应用层 | 依次完成 Anthropic HTTP、OpenAI HTTP、OpenAI WebSocket Finalizer | 三条路径的 S1/S2/S4、工具和续轮语义通过 |
| 5 · 抓包验收 | 每完成一条路径即在 Vircs 重跑 direct、mitm、ingress | 应用层与传输层均达到对应官方 Profile |
| 6 · 回归与合并 | 全量、竞态、性能测试及连续版本合并演练 | 无功能、安全和性能回归，核心 Handler 保持薄调用点 |

#### 3.3.6 验收标准与完成结果

| 验收项 | 完成结果 |
|---|---|
| **路由与账务** | 三条路径命中正确平台 OAuth 账号，重试、usage 和计费归属正确；Profile 不改变 Key/Group 权限 |
| **Anthropic HTTP** | S1/S2/S4 全部成功，工具和参数无回归，system/cache、动态身份及 HTTP/1.1 传输画像达到目标 |
| **OpenAI HTTP** | 非流式、SSE、工具、续轮和账号切换通过；入口语义、`call_id`、动态身份及 HTTP 传输画像达到目标 |
| **OpenAI WebSocket** | 首轮、续轮、工具、重连和 HTTP 回退通过；有效续轮、握手身份及 WS 传输画像达到目标 |
| **第三方客户端接入** | Kilo 六种“入站协议 × 目标上游”组合均通过；协议转换、工具语义和多轮上下文正确，最终统一使用目标平台对应的官方出站画像 |
| **模式独立性** | 自动透传开关及四种 WS mode 均不绕过 Profile，按实际出站协议选择 HTTP 或 WS 画像 |
| **适用边界** | Anthropic/OpenAI OAuth 自动生效；API Key、其他平台及非模型入口保持原行为 |
| **安全与回归** | 敏感值未进入普通日志；Host、代理和重定向边界受控；全量、竞态、性能及合并演练通过 |

第三方客户端回归矩阵：

| Kilo 入站格式 | 目标上游 | 最终出站画像 | 结果 |
|---|---|---|---|
| OpenAI Responses | OpenAI | Codex HTTP/WS Profile（按实际出站协议） | 通过 |
| OpenAI Compatible | OpenAI | Codex HTTP Profile | 通过 |
| Anthropic | Anthropic | Claude HTTP Profile | 通过 |
| OpenAI Responses | Anthropic | Claude HTTP Profile | 通过 |
| OpenAI Compatible | Anthropic | Claude HTTP Profile | 通过 |
| Anthropic | OpenAI | Codex HTTP Profile | 通过 |

以上是六种协议转换与路由组合，不是六套伪装实现。以后修改入站协议转换、模型/账号路由、Finalizer 或 Transport 时必须重跑受影响组合；修改公共 Resolver 或目标平台公共出站链路时必须重跑全部六种组合。

详细请求计数、字段差异、TLS 数据、运行编号和恢复记录统一保存在[实证 README](/Users/czs/Developer/sub2apiplus/backend/internal/service/testdata/official_egress/README.md)。

#### 3.3.7 编码任务分解（已完成）

所有任务均按“实现一项 → 自动化测试 → Vircs 抓包实证 → 进入下一项”的门禁执行。

| 任务 | 主要产物 | 完成门禁 |
|---|---|---|
| **T1 · 源码落点与契约测试** | 三条真实出站调用点、结构夹具、非适用账号隔离测试和字段来源表 | 稳定复现改造前缺口，不改变非 OAuth 行为 |
| **T2 · Profile 公共骨架** | Resolver、`OfficialEgressContext`、账号资格判断、校验及脱敏日志 | OAuth 自动生效，非适用账号零变化，错误状态明确失败，HTTP/WS 生命周期正确 |
| **T3 · Transport 与连接池** | 三套 Transport/Dialer、代理/CA 支持及隔离连接池键 | direct 与代理抓包达标，无跨账号/Profile/CA 复用 |
| **T4 · Anthropic HTTP** | Claude Finalizer、system/cache、动态身份及 HTTP/1.1 Profile | S1/S2/S4、工具、参数、mitm 和 direct 通过 |
| **T5 · OpenAI HTTP** | Body/Header Finalizer、`call_id`、身份上下文及 HTTP Profile | 非流式/SSE/工具/续轮/重试及抓包通过 |
| **T6 · OpenAI WebSocket** | Handshake/Frame Finalizer、WS Dialer及重连边界 | 首轮、有效续轮、工具、未知帧、身份冻结及抓包通过 |
| **T7 · 回归、性能与合并** | 全量/竞态/性能测试、三路径重抓及连续版本合并记录 | §3.3.6 全部通过，无安全和行为回归 |
| **N2 · 第三方客户端适配** | Kilo 六种“入站协议 × 目标上游”组合及 OpenAI WebSocket 完整历史归一化 | 六路协议转换与路由通过；HTTP S1/S4、WebSocket S1/S2/S4、工具身份及传输画像通过 |

T1–T7 与 N2 均已完成。N1 是 `/v1/models`、Chat Completions 和 Gemini Native 的关联契约补测，不属于 Official Egress Profile 的核心改造范围，亦已完成。

**结论**：§3.1 所述目标已在当前版本、Profile 及已验收的官方客户端和 Kilo 入站场景内完成。
