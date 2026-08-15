# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
> **当前 active 基线**：`codex-cli 0.147.0`；previous 为 `codex-cli 0.145.0`
> **依赖基线**：[`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)
> **文档定位**：本文是 Codex CLI 客户端规则、Sub2API 仿真实现和版本演进的人类可读权威入口；
> 逐规则机器证据见 [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。
>
> **当前终态门禁状态**：0.147 的 42 项行为验收、生产激活和回滚演练已经完成；完成后复核在
> Vircs 正式源码树检出 10 个基线外文本版本指纹，因此当前运行事实有效，但 promotion 后仓库
> 总门禁尚未闭环。历史更正见
> [`0.145→0.147 升级计划`](egress/maintenance/CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md)；
> 本手册以下规则用于纠正发布及后继升级。

---

# 第一部分 总体目标与仿真链路

## 1.1 总体目标与边界

无论入站来自官方 Codex CLI，还是通过 Codex、Compatible、Responses 等接口接入的第三方客户端，
只要最终使用 OpenAI OAuth 账号出站，最终 wire 均由当前 active 的 Codex CLI 版本画像统一定型。
兼容层仅负责协议、模型、工具和请求语义转换，不改变 Key、Group、账号路由或计费归属，也不拥有
最终 wire。

当前 active 画像必须在第二部分规定的范围内，统一约束官方与第三方客户端的 TLS、连接、
HTTP／WebSocket、Header、Body、端点和跨请求状态。当前版本及依赖基线见文首。

本文仅覆盖内置 OpenAI OAuth 和规则明确注明的条件分支，不覆盖 Anthropic、OpenAI API Key
mimic、其他供应商及可关闭的 plugins、apps、analytics、otel 流量。自定义 CA 和自定义
provider 规则仅作为条件分支记录。

## 1.2 客户端仿真链路

```text
官方源码、锁定依赖与真实 wire
→ 客户端规则画像
→ active 版本画像
→ 统一出站定型
→ 验收、发布与回滚
```

入站兼容层只提交请求语义和可验证条件；账号选定后绑定 active 版本画像，由统一执行链生成最终
wire。第二部分定义“应产生什么行为”，第三部分说明“Sub2API 如何实现”，第四部分规定版本演进，
第五部分处理非版本变更维护。

---

# 第二部分 Codex CLI 客户端规则画像

本部分定义规则成立所需的证据标准、观测边界和当前 active 基线的 53 个编号项。它回答“什么
行为可以成为客户端规则”；目标版本如何执行取证并生成新规则，只由第四部分规定。

## 2.1 规则证据与准入标准

### 2.1.1 证据类型与位置

| 类型 | 材料 | 可以证明 |
|---|---|---|
| L1 | `local-analysis/sources/codex-cli-0.147/codex-rs/` 官方 stable 源码 | 调用链、条件和内部机制 |
| L2 | `tools/spec_source_deps/` 锁定依赖源码 | 指定依赖版本与 feature 下的行为 |
| P／R | pcap、等长脱敏原始字节 | TLS、连接、HTTP／WS 和 Body 的实际输出 |
| J／M／L4 | 解码摘要、manifest、测试和合成输入 | 摘要绑定与辅助验证，不能单独定义官方规则 |

当前 L2 依赖锁定为 `hyper 1.8.1`、`hyper-util 0.1.20`、`http 1.4.0`、`tungstenite 0.27.0`、
`h2 0.4.13` 和 `reqwest 0.12.28`；准确来源和摘要以依赖基线清单为准。Sub2API 实现证据位于
`backend/` 和 `docs/egress/`；逐规则索引及源码锚点分别见 `docs/EVIDENCE_INDEX.md` 和
`tools/spec_ref_anchors.json`。

当前规则画像基于官方 tag `rust-v0.147.0`（commit `be6e8eac029b183056b7e4402879f15d2c85f61b`）
及正式 0.145→0.147 Campaign。批准画像为 `codex-0.147.0-official-k59-v1`，摘要为
`94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc`；42 项对齐范围已全部
通过机器断言和验收。完整身份与证据摘要见
[`CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md`](egress/maintenance/CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md)
§6.5、§10.1.1。各规则保留的早期 run ID 是未变化规则的原始证据，不代表 active 版本仍为 0.145。

所有证据必须绑定官方源码、依赖、二进制、平台、配置、账号、抓包运行号和摘要。只有能够重新
解析的材料可以作为规则依据；R 类材料只允许等长脱敏，未脱敏材料不得离开采集机。

### 2.1.2 规则准入与观测边界

规则只有在被测身份和适用条件明确、源码与合适的 wire 观测通道闭环、正反例充分、引用和摘要
可复算时才能准入。证据不足时只能收窄命题或保持未决。

“固定、随机、条件”分别表示每次一致、允许变化和随明确条件变化，不得把随机样本或条件结果
固化为默认行为。

- pcap、relay、MITM 和服务端重建只能证明各自可见的层次，不能互相替代；
- 自定义 CA、代理和受控失败等条件样本不能外推为默认路径或自然成功链；
- 全集、缺失和连接完整性结论必须基于无预设过滤的完整双向样本。

实现只需对齐官方可见结果，不复制官方内部结构。场景矩阵、重复样本和源码闭环后，可以停止当前
采样；被测身份或条件变化时必须按第四部分重新分类。

### 2.1.3 日常复算

日常复算使用不发送真实请求的仓库门禁：

```bash
make check-egress-spec
```

本地门禁额外校验未提交的官方源码镜像；CI 使用 `make check-egress-spec-ci`，其余规则、证据、
台账和实现契约检查保持一致。

依赖 `mitmproxy`、压缩库、pcap 或 Linux 能力的抓包工具测试，正式结果必须在 Campaign 冻结的
抓包镜像和目标架构中运行；宿主机或开发机因依赖缺失触发的 `skipUnless` 只说明该环境未执行
测试，既不能算通过，也不能作为升级缺口。测试收据必须分别记录 `passed`、`failed`、
`approved_skip` 和 `unexpected_skip`；正式升级要求 `unexpected_skip=0`。依赖门禁型用例即使
允许在开发机跳过，也必须在冻结镜像中实际通过，才能进入 candidate 或 production 结论。

## 2.2 当前规则分组与验收范围

共 **53 个编号项**，按性质与适用范围分为五组：

<!-- SPEC_STATUS_START -->
| 分组 | 条数 | 当前验证状态 | Sub2API 需对齐项 |
|---|---:|---|---:|
| **① 内置 OpenAI OAuth 可见规则** | **39** | ✅ 38；🟡 1 | **39** |
| **② 自定义 CA 条件分支** | **8** | ✅ 8；🟡 0 | **0** |
| **③ 自定义 provider 条件分支** | **1** | ✅ 1；🟡 0 | **0** |
| **④ 派生／内部机制说明** | **3** | 源码机制 | **3** |
| **⑤ 采集与观测记录** | **2** | 观测记录 | **0** |
| **合计** | **53** | — | **42** |
<!-- SPEC_STATUS_END -->

固定转发到 OpenAI 官方 OAuth 上游时，对齐范围始终是 **39 条可见 wire 规则 + 3 条机制 = 42 项**；
证据充分度不改变范围，因此 `SPEC-EP-012` 即使自然 Voice/realtime 成功抓包有限，仍属于必须实现和验收的可见规则。
②③只在有效自定义 CA／provider 条件成立时适用；④对齐可见结果而非内部结构；⑤只作证据审计。

images、alpha-search、legacy compact、realtime 和条件 header 等只在各自条件成立时产生，不另立分组。
每项使用“范围—规则／机制／记录—源码—实测—实现—状态”六字段；“实现”只规定可见行为。

## 2.3 TLS

### SPEC-TLS-001 Ubuntu 24.04 下默认 HTTP ClientHello

- **范围**：内置 OpenAI OAuth；Ubuntu 24.04/OpenSSL；HTTP；未配置自定义 CA。
- **规则**：ClientHello 使用 30 个 cipher suite，且不携带 ALPN 扩展。
- **源码**：[L3] 系统 native-tls 的动态画像，无可固定该 cipher 集合的 L1/L2 证据。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）中该分支为 30 cipher、无 ALPN。
- **实现**：仅在同平台画像范围复刻；macOS、Windows或其他 OpenSSL 策略必须另建画像。
- **状态**：✅ 源码无／不适用；抓包充分。

### SPEC-TLS-002 有效自定义 CA 下的 HTTP ClientHello

- **范围**：自定义 CA 条件分支；HTTP。
- **规则**：`CODEX_CA_CERTIFICATE` 或 `SSL_CERT_FILE` 指向非空、可读且可解析的
  CA bundle 时切换到 rustls；实测为 10 cipher，ALPN 依次 offer `h2`、`http/1.1`。
- **源码**：[L1] `http-client/src/custom_ca.rs:296-320`、`http-client/src/custom_ca.rs:398`；
  默认客户端失败回退见 `login/src/auth/default_client.rs:300-305`。
- **实测**：`audit-tls002-ca-n0-20260730a`（P）验证 ClientHello；
  `official-h2-20260727T131936Z`（J）验证协商为 h2。
- **实现**：只有有效 CA bundle 才进入该分支；变量未设置、空值或证书无效不得按 h2 画像处理。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-TLS-003 WS ClientHello 扩展顺序不固定

- **范围**：内置 OpenAI OAuth；WS。
- **规则**：样本中的 WS ClientHello 扩展集合相同，但四次排列均不同；不得把某一次
  扩展顺序硬编码为固定常量。
- **源码**：[L3] 排列行为由抓包确认；WS 恒走 rustls 的归因见
  `websocket-client/src/lib.rs:68-73`。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）取得四种扩展排列。
- **实现**：使用等价 rustls 行为；不要求每次都产生全新的排列，也不把有限样本外推为全局集合。
- **状态**：✅ 源码无／不适用；抓包充分。

## 2.4 协议与连接

### SPEC-PROTO-001 默认 HTTP 不 offer ALPN并落到 HTTP/1.1

- **范围**：内置 OpenAI OAuth；HTTP；未配置有效自定义 CA。
- **规则**：ClientHello 不含 ALPN 扩展，因此该条件下使用 HTTP/1.1；决定因素是
  自定义 CA 条件，不是直连或代理。
- **源码**：[L3] 无直接源码常量；CA 分支选择机制见
  `http-client/src/custom_ca.rs:296-320`。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）中 41 个 ClientHello 均无扩展 16。
- **实现**：默认分支不得固定 offer h2；启用有效 CA 后转入 SPEC-TLS-002／H2 分支。
- **状态**：✅ 源码无／不适用；抓包充分。

### SPEC-PROTO-002 Responses 默认 WS，HTTP 为降级路径

- **范围**：内置 OpenAI OAuth。
- **规则**：`supports_websockets=true` 时先走 WS；重试预算耗尽并设置
  `force_http_fallback` 后改走 HTTP POST。
- **源码**：[L1] `model-provider-info/src/lib.rs:139`、
  `core/src/client.rs:522`、`core/src/client.rs:949`、
  `core/src/responses_retry.rs:35-45`。
- **实测**：`official-httpfb3-20260727T234853Z`（J）记录自然重试耗尽；
  `audit-ep014-turnstate-echo-20260730a`（R）记录受控 426 后的 HTTP 降级请求。
- **实现**：内置 provider 默认启用 WS；HTTP 只在明确降级条件成立时使用。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-CONN-001 主模型 HTTP 调用与 retry 的连接生命周期

- **范围**：内置 OpenAI OAuth；models、responses、legacy compact、images、alpha-search
  的 HTTP 链；不含长期持有 Client 的 backend-client 和 WS prewarm。
- **规则**：不同上层 API 调用各自新建 `reqwest::Client`，正常跨调用不复用 TCP；
  同一次调用的 retry 共享 Client，存活连接可复用，断连后由同一 Client 新建 TCP。
- **源码**：[L1] `login/src/auth/default_client.rs:221-223`、
  `core/src/client.rs:983-996`、`codex-api/src/endpoint/session.rs:80-154`、
  `model-provider/src/models_endpoint.rs:75`、`ext/image-generation/src/backend.rs:30`、
  `ext/web-search/src/tool.rs:91`。
- **实测**：`clean2-conn-20260728T132008Z`、`audit-conn001-image-repeat-20260730a`、
  `audit-conn001-search-repeat-20260730a`、
  `audit-conn001-retry-keepalive-openai-http-20260730a`、
  `audit-conn001-retry-disconnect-openai-http-20260730a`（均为 R）。
- **实现**：按“上层调用”划分 Client 生命周期；不得把主模型链结论外推到 wham 等 backend-client。
- **状态**：✅ 源码充分；抓包充分。

## 2.5 HTTP/1.1

### SPEC-H1-001 普通 HTTP header 名全小写

- **范围**：内置 OpenAI OAuth；普通 HTTP，不含 WS 握手。
- **规则**：所有线上 header 名均以小写输出，包括 `host`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h1/role.rs:1572-1578`
  的 `write_headers` 默认分支；官方未启用保留大小写选项。
- **实测**：`audit-h1raw-20260730a`（R）验证 models 与 responses。
- **实现**：普通 HTTP 使用小写 header；不得把 WS 的大写前五项套入本分支。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H1-002 host 位于用户 header 之后

- **范围**：内置 OpenAI OAuth；普通 HTTP。
- **规则**：`host` 在用户 header 之后插入；无 body 时为最后一项。
- **源码**：[L2]
  `tools/spec_source_deps/hyper-util-0.1.20/src/client/legacy/client.rs:298-306`、
  `tools/spec_source_deps/hyper-util-0.1.20/src/client/legacy/client.rs:1033-1036`。
- **实测**：`audit-h1raw-20260730a`（R）验证 models／responses 线序。
- **实现**：不得把 `host` 提前到用户 header 之前。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H1-003 自动 content-length 位于 host 之后

- **范围**：内置 OpenAI OAuth；由 hyper 根据完整 body 自动计算长度的 HTTP 请求。
- **规则**：`content-length` 为最后一项，位于 `host` 之后；显式预置长度的上传请求
  不属于本条。
- **源码**：[L2]
  `tools/spec_source_deps/hyper-1.8.1/src/proto/h1/role.rs:1410-1419`、
  `tools/spec_source_deps/hyper-1.8.1/src/proto/h1/role.rs:1483-1512`。
- **实测**：`audit-h1raw-20260730a`（R）验证 POST /responses。
- **实现**：自动长度分支保持 `…, host, content-length`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H1-004 用户 header 按 HeaderMap 迭代序输出

- **范围**：内置 OpenAI OAuth；普通 HTTP。
- **规则**：用户 header 按 `HeaderMap.entries` 迭代序输出，不按字典序；发生
  `swap_remove` 时也不能把结果简化为原始插入序。0.147.0 的默认 Lite Responses 样本中，
  `x-openai-internal-codex-responses-lite` 位于 `x-codex-turn-metadata` 与
  `x-client-request-id` 之间。
- **源码**：[L2] `tools/spec_source_deps/http-1.4.0/src/header/map.rs:923-928`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`；各端点的构造顺序由
  L1 `core/src/client.rs:1153-1177`、`core/src/client.rs:1918-1924` 调用链决定。
- **实测**：`audit-h1raw-20260730a`、`audit-ep014-turnstate-echo-20260730a`（R）
  验证 models、responses 及条件 turn-state 插槽。
- **实现**：逐端点复刻最终线序，不得使用统一字典排序或一份 header 并集。
- **状态**：✅ 源码充分；抓包充分。

## 2.6 HTTP/2（仅自定义 CA）

### SPEC-H2-001 SETTINGS 参数顺序

- **范围**：自定义 CA 条件分支。
- **规则**：SETTINGS 帧按 `ENABLE_PUSH, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE,
  MAX_HEADER_LIST_SIZE` 输出，即参数 ID `2,4,5,6`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:110` 与
  `tools/spec_source_deps/h2-0.4.13/src/frame/settings.rs:213-259`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验，不用于计数或缺失命题。
- **实现**：只在有效自定义 CA 触发的 h2 分支复刻。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-002 ENABLE_PUSH

- **范围**：自定义 CA 条件分支。
- **规则**：`ENABLE_PUSH = 0`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:110`
  的 `enable_push(false)`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 2 写 0。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-003 INITIAL_WINDOW_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`INITIAL_WINDOW_SIZE = 2,097,152`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:49`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 4 写 2,097,152。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-004 MAX_FRAME_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`MAX_FRAME_SIZE = 16,384`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:50`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 5 写 16,384。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-005 MAX_HEADER_LIST_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`MAX_HEADER_LIST_SIZE = 16,384`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:52`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 6 写 16,384。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-006 首个连接级 WINDOW_UPDATE

- **范围**：自定义 CA 条件分支。
- **规则**：stream 0 的首个 WINDOW_UPDATE 增量为 `5,177,345`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:48` 与
  `tools/spec_source_deps/h2-0.4.13/src/frame/settings.rs:43-44`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：复刻该连接窗口配置，不单独硬写无来源的帧常量。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-007 请求伪头顺序

- **范围**：自定义 CA 条件分支。
- **规则**：`:method, :scheme, :authority, :path`。
- **源码**：[L2] `tools/spec_source_deps/h2-0.4.13/src/frame/headers.rs:698-718`、
  `tools/spec_source_deps/h2-0.4.13/src/hpack/encoder.rs:61-78`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作 HPACK 正向交叉核验。
- **实现**：保持该伪头顺序。
- **状态**：✅ 源码充分；抓包充分。

## 2.7 WebSocket

### SPEC-WS-001 握手前五项固定大写与顺序

- **范围**：内置 OpenAI OAuth；WS 握手。
- **规则**：前五项固定为 `Host, Connection, Upgrade, Sec-WebSocket-Version,
  Sec-WebSocket-Key`。
- **源码**：[L2]
  `tools/spec_source_deps/tungstenite-openai-0.27.0/src/handshake/client.rs:137-175`。
- **实测**：`clean-tool-20260728T132346Z`（R）。
- **实现**：保持大写形式和固定顺序。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-002 握手剩余 header 的大小写与扰动顺序

- **范围**：内置 OpenAI OAuth；WS 握手。
- **规则**：前五项之后的普通 header 小写输出；其顺序是逐个移除前五项后的
  `HeaderMap.swap_remove` 结果，缺项时可能整体变化。
- **源码**：[L2]
  `tools/spec_source_deps/tungstenite-openai-0.27.0/src/handshake/client.rs:159-206`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`。
- **实测**：`clean-tool-20260728T132346Z`（R）。
- **实现**：不得把某一完整样本简化为“缺项后原位跳过”的静态数组。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-003 自定义 provider 注入头的两个大小写特例

- **范围**：自定义 provider；仅当 `http_headers` 注入对应头。
- **规则**：WS 握手中 `origin` 输出为 `Origin`，
  `sec-websocket-protocol` 输出为 `Sec-WebSocket-Protocol`；普通 HTTP 仍为小写。
- **源码**：[L1] 注入入口 `model-provider-info/src/lib.rs:115`；[L2]
  `tools/spec_source_deps/tungstenite-openai-0.27.0/src/handshake/client.rs:190-206`。
- **实测**：`relay-wshdr3`（R）在同一运行取得 WS 正例与 HTTP 对照。
- **实现**：固定 OpenAI OAuth 上游无需实现；兼容 Codex 自定义 provider 时才实现。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-004 业务帧使用 permessage-deflate 与上下文接管

- **范围**：内置 OpenAI OAuth；WS 业务帧。
- **规则**：协商 `permessage-deflate`；压缩文本帧 RSV1 置位，payload 为 raw deflate，
  且解压上下文跨帧复用。
- **源码**：[L3] 这是 wire 行为，无独立 L1/L2 断言。
- **实测**：`clean-tool-20260728T132346Z`（R）验证首字节、压缩 payload 与跨帧上下文。
- **实现**：每条连接维护同一压缩／解压上下文；不得逐帧重置。
- **状态**：✅ 源码无／不适用；抓包充分。

### SPEC-WS-005 response.create 字段槽位与条件字段

- **范围**：内置 OpenAI OAuth；WS 业务帧。
- **规则**：字段槽位顺序为
  `type, model, instructions?, previous_response_id?, input, tools?, tool_choice,
  parallel_tool_calls, reasoning, store, stream, stream_options?, include,
  service_tier?, prompt_cache_key?, text?, generate?, client_metadata?`。
  `generate=false` 只用于 warmup；`previous_response_id` 只在既有响应前缀可复用时出现。
- **源码**：[L1] `codex-api/src/common.rs:302-328`、
  `core/src/client.rs:1618-1654`、`core/src/client.rs:924`。
- **实测**：`clean-tool-20260728T132346Z`（R）覆盖 Lite；
  `audit-ws005-nonlite-20260730a`（R）覆盖非 Lite、warmup 与增量帧。
- **实现**：按 serde 条件省略字段；不得把 Lite 的 13 项子集或“首轮／后续轮”写成固定规则。
- **状态**：✅ 源码充分；抓包充分。

## 2.8 Header

### SPEC-HDR-001 请求 header 的内部组装顺序

- **范围**：派生／内部机制。
- **机制**：请求先由 provider 构造，再合并端点额外头、body 和 configure 结果；
  每次 retry 最后执行认证。流式路径还会先转为 prepared request。Client 默认头是
  与请求级头并行的入口。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:48`、
  `codex-api/src/endpoint/session.rs:80-154`、
  `login/src/auth/default_client.rs:291-305`。
- **实测**：不适用；wire 只能证明最终 header 集合与线序，不能反推内部调用顺序。
- **实现**：内部代码可不同，但覆盖、认证重试和最终线序结果必须与各可见规则一致。
- **状态**：— 源码充分；抓包不适用。

### SPEC-HDR-002 Client 默认 header 集合

- **范围**：内置 OpenAI OAuth。
- **规则**：Client 默认头为 `originator`、`user-agent`，以及条件性的
  `x-openai-internal-codex-residency`。residency 来自受管理的 requirements
  配置项 `enforce_residency`，不是环境变量。
- **源码**：[L1] `login/src/auth/default_client.rs:52`、
  `login/src/auth/default_client.rs:99-104`、
  `login/src/auth/default_client.rs:330-343`、
  `config/src/config_requirements.rs:929`、
  `exec/src/lib.rs:460`、`tui/src/lib.rs:1150`、
  `app-server/src/request_processors/initialize_processor.rs:136`。
- **实测**：`official-body2-20260728T000549Z`（J）验证默认集合；
  `audit-hdr002-residency-20260730a`（R）验证 `us` 正向分支。
- **实现**：未设置 residency 时不发；设置时按端点最终线序合并。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-004 OpenAI-Beta 仅由 Codex 加在 WS 握手

- **范围**：内置 OpenAI OAuth。
- **规则**：Codex 自身只在 WS 握手发送
  `openai-beta: responses_websockets=2026-02-06`；HTTP responses 和 images 不发送。
- **源码**：[L1] `core/src/client.rs:142`、`core/src/client.rs:1113`、
  `cli/src/doctor.rs:93`、`cli/src/doctor.rs:2393`。
- **实测**：`clean-tool-20260728T132346Z`（R）为 WS 正例；
  `audit-body002-plain-20260730a`（HTTP responses）、
  `clean-image-20260728T132405Z`（generations）、
  `relay-imgedit1`（edits）为 R 类反例。
- **实现**：只在 WS 握手添加；自定义 provider 主动注入同名头不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-005 user-agent suffix

- **范围**：内置 OpenAI OAuth。
- **规则**：UA 为平台前缀加可选 ` ({name}; {version})` suffix。exec 使用
  `codex_exec/0.147.0` 与 `(codex_exec; 0.147.0)`；TUI 使用
  `codex-tui/0.147.0` 与 `(codex-tui; 0.147.0)`。启动首次 models 因进程级写入时序，
  有 suffix 和无 suffix 均属于官方观测集。
- **源码**：[L1] `login/src/auth/default_client.rs:39`、
  `login/src/auth/default_client.rs:159`、
  `app-server/src/request_processors/initialize_processor.rs:94-137`、
  `cloud-tasks/src/util.rs:13`。
- **实测**：`clean-search-20260728T132311Z`（exec）、`clean-legacy-20260728T132509Z`
  （TUI）、`audit-ep019-wham-consume-safe-20260730a`（`codex_exec` originator、
  `unknown` 终端标识）均为 R。
- **实现**：按入口和进程状态生成 suffix；不得把一次首次 models 结果硬编码为固定值。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-006 accept 按传输和端点变化

- **范围**：内置 OpenAI OAuth。
- **规则**：HTTP responses 为 `text/event-stream`；WS 握手无 `accept`；
  models、legacy compact、alpha-search、images generations／edits 为 `*/*`。
- **源码**：[L1] HTTP responses 显式值见
  `codex-api/src/endpoint/responses.rs:149`；其余 `*/*` 是 reqwest 默认 wire 行为。
- **实测**：`audit-h1raw-20260730a`、`clean-tool-20260728T132346Z`、
  `clean-legacy-20260728T132509Z`、`clean-search-20260728T132311Z`、
  `clean-image-20260728T132405Z`、`relay-imgedit1`（R）。
- **实现**：按端点生成，不使用全局固定值。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-HDR-007 普通 responses／legacy compact 的会话头

- **范围**：内置 OpenAI OAuth；普通 responses 与 legacy compact。
- **规则**：发送小写连字符形式 `session-id`、`thread-id`；不发送
  `session_id` 或 `conversation-id`。realtime 的 `x-session-id` 是独立分支。
- **源码**：[L1] `codex-api/src/requests/headers.rs:8`、
  `codex-api/src/requests/headers.rs:11`；realtime 见
  `core/src/realtime_conversation.rs:1668`。
- **实测**：`audit-h1raw-20260730a`（responses）、`clean-legacy-20260728T132509Z`
  （compact）、`clean-search-20260728T132311Z`（alpha-search 不发送）均为 R。
- **实现**：严格按端点发送，不能把会话头扩散到 alpha-search。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-008 四个条件 header

- **范围**：内置 OpenAI OAuth；仅在对应会话来源或 feature 成立时。
- **规则**：
  `x-openai-subagent` 来自子代理／memory consolidation；
  `x-openai-memgen-request: true` 来自内部 memory consolidation；
  `x-codex-parent-thread-id` 来自父线程；
  `x-responsesapi-include-timing-metrics: true` 来自 `runtime_metrics`。
- **源码**：[L1] `core/src/responses_metadata.rs:255-334`、
  `core/src/client.rs:734-767`、`core/src/client.rs:1108-1120`、
  `features/src/lib.rs:982-986`。
- **实测**：`relay-review4`、`audit-hdr008-guardian-20260730a`、
  `audit-hdr008-memgen-20260730a`、`relay-rtmetrics1`（R）。
- **实现**：按条件插入；`x-openai-subagent` 的 `Other(label)` 不得实现成封闭枚举。
- **状态**：✅ 源码充分；抓包充分。

## 2.9 Body

### SPEC-BODY-001 Responses 顶层字段由传输结构体封闭

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：HTTP 由 `ResponsesApiRequest` 序列化；WS 由
  `ResponseCreateWsRequest` 序列化并增加外层事件类型。不得发送对应结构体外字段。
- **源码**：[L1] `codex-api/src/common.rs:252-328`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（HTTP Lite）、
  `audit-body002-plain-20260730a`（HTTP 非 Lite）、
  `clean-tool-20260728T132346Z`（WS Lite）、
  `audit-ws005-nonlite-20260730a`（WS 非 Lite），均为 R。
- **实现**：分别按两个结构体和 serde 省略条件生成，不使用统一 body 超集。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-002 请求压缩策略

- **范围**：内置 OpenAI OAuth。
- **规则**：`enable_request_compression` 默认开启时普通 responses 使用 zstd；
  关闭时明文。legacy compact 始终明文。
- **源码**：[L1] `features/src/lib.rs:1084-1087`、
  `core/src/session/session.rs:1169`、`http-client/src/request.rs:41-43`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（zstd responses）、
  `audit-body002-plain-20260730a`（关闭压缩）、`clean-legacy-20260728T132509Z`
  （明文 compact），均为 R。
- **实现**：只压缩 responses，且尊重 feature 开关。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-003 Lite 模式变换

- **范围**：内置 OpenAI OAuth；模型 manifest 的 `use_responses_lite=true`。
- **规则**：instructions/tools 迁入 `input.additional_tools`；
  `reasoning.context=all_turns`；`parallel_tool_calls=false`。WS 增量帧不重复发送
  已复用的 additional_tools 前缀。
- **源码**：[L1] `core/src/client.rs:817-833`、
  `core/src/client.rs:862-891`、`core/src/client.rs:924`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（HTTP）、
  `clean-tool-20260728T132346Z`（WS），均为 R。
- **实现**：由模型 manifest 驱动；不得把 Lite 变换应用到非 Lite 模型。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-004 turn-state 的消费、保存与回送

- **范围**：派生／内部机制。
- **机制**：HTTP 从初始响应头 `x-codex-turn-state` 读取；WS 从
  `response.metadata.headers` 读取。保存到当前 turn 后，后续 responses／legacy
  compact 通过 header，WS 通过 `client_metadata` 原样回送。
- **源码**：[L1] `codex-api/src/sse/responses.rs:62-70`、
  `codex-api/src/endpoint/responses_websocket.rs:739-742`、
  `core/src/client.rs:1575-1579`、`core/src/client.rs:1898-1914`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、
  `audit-ep014-turnstate-compact-20260730a`、
  `audit-body004-ws-turnstate-20260730a`（R）完成三条输入→保存→回送闭环。
- **实现**：Sub2API 若终结上下游流，必须保存并回送；透明转发时不得丢失或重复生成。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-005 tool_choice 是字符串

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：`tool_choice` 为 JSON 字符串，当前值 `"auto"`，不是对象。
- **源码**：[L1] `codex-api/src/common.rs:259`、`core/src/client.rs:923`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `clean-tool-20260728T132346Z`、`audit-ws005-nonlite-20260730a`（R）。
- **实现**：所有传输和 Lite／非 Lite 分支均序列化为字符串。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-006 HTTP Responses 的 Lite／非 Lite 字段

- **范围**：内置 OpenAI OAuth；HTTP。
- **规则**：字段全集由 `ResponsesApiRequest` 定义。Lite 省略顶层
  `instructions/tools`、强制 `parallel_tool_calls=false` 并加入
  `reasoning.context=all_turns`；非 Lite 保留顶层 instructions/tools，
  `parallel_tool_calls` 取 prompt 值。Option 字段为空时省略。
- **源码**：[L1] `codex-api/src/common.rs:253-274`、
  `core/src/client.rs:817-924`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（Lite）与
  `audit-body002-plain-20260730a`（非 Lite），均为 R。
- **实现**：按模型 manifest 与 Option 值序列化，不硬编码某个模型的一次字段子集。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-007 编码工作流 input 类型分布记录

- **范围**：采集与观测记录；固定十轮编码任务。
- **记录**：20 个含 input 的请求／帧共 377 项，最大长度 95；类型为
  `message 134`、`reasoning 87`、`custom_tool_call_output 79`、
  `custom_tool_call 71`、`additional_tools 6`。
- **源码**：[L3] 场景统计无 L1/L2 协议常量。
- **实测**：`audit-body007-workflow-clean-20260730a`（R），20/20 连接双向完整。
- **实现**：不实现这些计数；仅用来确认测试场景确实包含真实工具调用。
- **状态**：✅ 源码无／不适用；抓包充分。

## 2.10 端点与辅助链

### SPEC-EP-001 生图工具呈现与独立 images 调用

- **范围**：内置 OpenAI OAuth；模型支持图像生成时。
- **规则**：非 Lite 可在顶层 `tools` 中发送 namespace `image_gen`／工具
  `imagegen`；Lite 将能力放入 `input.additional_tools` 的 exec 工具目录。
  模型调用后由客户端请求独立 `images/generations` 或 `images/edits`。
- **源码**：[L1] `core/src/tools/spec_plan.rs:103-104`、
  `tools/src/tool_spec.rs:22-45`、`ext/image-generation/src/backend.rs:30-78`、
  `codex-api/src/endpoint/images.rs:33-68`。
- **实测**：`audit-body002-plain-20260730a`（非 Lite）、
  `clean-image-20260728T132405Z`（Lite + generations）、
  `relay-imgedit1`（edits），均含 R。
- **实现**：按 Lite 模式呈现工具；不得改成 hosted `{"type":"image_generation"}`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-002 OAuth 域名分布

- **范围**：内置 OpenAI OAuth。
- **规则**：模型与常规业务默认使用 `chatgpt.com/backend-api/*`。条件例外为：
  token 刷新到 `auth.openai.com`；realtime sideband 默认到 `api.openai.com`；
  文件上传 PUT 使用服务端返回的区域 `*.oaiusercontent.com` URL。
- **源码**：[L1] `model-provider-info/src/lib.rs:331-334`、
  `login/src/auth/manager.rs:192`、`core/src/realtime_conversation.rs:1157-1165`、
  `core/src/mcp_openai_file.rs:147-189`、`codex-api/src/files.rs:126-275`。
- **实测**：`oauth-ep002-allhosts`、`oauth-ep002-refresh`（P）；
  `audit-ep012-sideband-synth-20260730a`、
  `audit-ep002-file-upload-full2-20260730a`（R）。
- **实现**：使用配置或服务端返回 URL；不得硬编码单一区域上传 host。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-005 只有 responses 可使用请求压缩

- **范围**：内置 OpenAI OAuth。
- **规则**：responses 的流式请求路径可设置 compression；models、legacy compact、
  alpha-search、images 均走不带 compression 的 execute 路径并明文发送。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:63-154`、
  `codex-api/src/endpoint/responses.rs:135-153`、
  `codex-api/src/endpoint/models.rs:46-62`、
  `codex-api/src/endpoint/compact.rs:46-56`、
  `codex-api/src/endpoint/search.rs:35-45`、
  `codex-api/src/endpoint/images.rs:33-68`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `audit-h1raw-20260730a`、`clean-legacy-20260728T132509Z`、
  `clean-search-20260728T132311Z`、`clean-image-20260728T132405Z`（R）。
- **实现**：不得给非 responses 端点添加 `content-encoding: zstd`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-006 models 的 URL 与方法

- **范围**：内置 OpenAI OAuth。
- **规则**：`GET {base}/models?client_version=0.147.0`。
- **源码**：[L1] `codex-api/src/endpoint/models.rs:31-55`。
- **实测**：`audit-h1raw-20260730a`（R）。
- **实现**：版本 query 与 CLI 基线一致。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-007 legacy compact 的 URL 与方法

- **范围**：内置 OpenAI OAuth；关闭 `remote_compaction_v2` 后的 legacy 分支。
- **规则**：`POST {base}/responses/compact`。
- **源码**：[L1] `codex-api/src/endpoint/compact.rs:35-50`。
- **实测**：`clean-legacy-20260728T132509Z`（R）。
- **实现**：仅 legacy 分支使用；默认 V2 见 SPEC-EP-021。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-008 alpha-search 的 URL 与方法

- **范围**：内置 OpenAI OAuth；触发 web search。
- **规则**：`POST {base}/alpha/search`。
- **源码**：[L1] `codex-api/src/endpoint/search.rs:31-44`。
- **实测**：`clean-search-20260728T132311Z`（R）。
- **实现**：仅触发 alpha-search 工具时发送。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-009 realtime 第一跳

- **范围**：内置 OpenAI OAuth；WebRTC realtime。
- **规则**：`POST {base}/realtime/calls?intent=quicksilver&architecture=avas`。
  非 backend provider 的 `/live` 不属于本范围。
- **源码**：[L1] `codex-api/src/endpoint/realtime_call.rs:62-72`、
  `codex-api/src/endpoint/realtime_call.rs:108-155`。
- **实测**：`webrtc-20260728T134028Z`、`live2-20260728T140403Z`（R）均取得第一跳。
- **实现**：只有启用相应 realtime 功能时发送。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-012 realtime 成功后的双出站链

- **范围**：内置 OpenAI OAuth；WebRTC；第一跳成功并返回 `call_id`。
- **规则**：第一跳为 SPEC-EP-009；随后客户端以
  `GET+Upgrade wss://api.openai.com/v1/realtime?intent=quicksilver&call_id=…`
  建立 sideband，并发送 `openai-alpha: quicksilver=v1`。
- **源码**：[L1] `codex-api/src/endpoint/realtime_call.rs:213-224`、
  `core/src/realtime_conversation.rs:1157`、
  `core/src/realtime_conversation.rs:1188-1206`、
  `core/src/realtime_conversation.rs:1657`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:742-815`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:919-994`、
  `core/src/realtime_conversation.rs:1763-1769`。
- **实测**：`webrtc-20260728T134028Z`（R）自然第一跳返回 400；
  `live2-20260728T140403Z`、`audit-ep012-realtime-20260730a`（R）自然第一跳返回 403；
  `audit-ep012-sideband-synth-20260730a`（R）以受控 200 触发官方客户端第二跳。
- **实现**：具备 realtime 条件后按双跳链实现；当前不得把受控 200 写成生产自然成功。
- **状态**：🟡 源码充分；抓包有限。Voice/realtime 自然成功补采暂缓。

### SPEC-EP-013 内置 provider 的 query 边界

- **范围**：内置 OpenAI OAuth；由 `Provider::url_for_path()` 构造的 Codex API URL。
- **规则**：provider 级 `query_params=None`；query 只由端点自身添加。目前 models
  添加 `client_version=0.147.0`，realtime/calls 添加 `intent` 与 `architecture`，
  普通 responses 不带 query。
- **源码**：[L1] `codex-api/src/provider.rs:53`、
  `model-provider-info/src/lib.rs:341`、
  `codex-api/src/endpoint/realtime_call.rs:213-224`。
- **实测**：`audit-h1raw-20260730a`（models／responses）与
  `webrtc-20260728T134028Z`（realtime），均为 R。
- **实现**：不得给全部 Codex API URL 透传统一 query；自定义 provider 不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-014 legacy compact 的 header 集合

- **范围**：内置 OpenAI OAuth；legacy compact。
- **规则**：默认线序为
  `version, x-codex-installation-id, x-codex-window-id, x-codex-turn-metadata,
  session-id, thread-id, x-openai-internal-codex-responses-lite, authorization,
  chatgpt-account-id, content-type, accept, originator, user-agent, cookie, host,
  content-length`。条件头位于 `x-codex-installation-id` 之后、
  `x-codex-window-id` 之前：分别触发时，`x-codex-beta-features` 或
  `x-codex-turn-state` 占第 3 个 header 槽。
- **源码**：[L1] `core/src/client.rs:612-630`、`core/src/client.rs:1898-1915`、
  `codex-api/src/endpoint/responses.rs:89`。
- **实测**：`clean-legacy-20260728T132509Z`（默认）、
  `audit-ep014-beta-legacy-20260730a`（beta）、
  `audit-ep014-turnstate-compact-20260730a`（turn-state），均为 R。
- **实现**：按条件和固定插槽生成；不得把缺失条件头简单追加到末尾。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-015 alpha-search 的 header 与 body

- **范围**：内置 OpenAI OAuth；alpha-search。
- **规则**：header 线序为
  `version, x-codex-turn-metadata, authorization, chatgpt-account-id, content-type,
  accept, originator, user-agent, cookie, host, content-length`。
  body 顶层字段为 `id, model, input, commands, settings, max_output_tokens`；
  `commands` 随检索阶段变化。
- **源码**：[L1] `codex-api/src/search.rs:9-21`、
  `ext/web-search/src/tool.rs:110-120`、`ext/web-search/src/tool.rs:185-195`。
- **实测**：`clean-search-20260728T132311Z`（R）在同一运行取得两次请求和两种 commands。
- **实现**：不发送 responses 的 session/thread header；保留阶段性 commands。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-019 WHAM 路径与线序

- **范围**：内置 OpenAI OAuth；backend-client。
- **规则**：使用
  `GET /backend-api/wham/usage`、
  `GET /backend-api/wham/rate-limit-reset-credits`、
  `GET /backend-api/wham/settings/user`、
  `POST /backend-api/wham/rate-limit-reset-credits/consume`。
  前两个 GET 的默认 header 线序为
  `user-agent, authorization, chatgpt-account-id, accept, host`；
  settings/user 在 account-id 后增加 `cache-control: no-cache, no-store`，并在有会话
  cookie 时于 `accept` 后发送 `cookie`；
  consume 再含 `content-type, content-length` 与 `redeem_request_id` body。
- **源码**：[L1] `backend-client/src/client/rate_limit_resets.rs:15-19`、
  `backend-client/src/client/rate_limit_resets.rs:31-109`、
  `backend-client/src/client.rs:221-240`、`backend-client/src/client.rs:458-475`、
  `backend-client/src/client.rs:637-641`。最终线序仍由 wire 确认。
- **实测**：正式 k80 Campaign 的 A12 取得三种 GET 与安全 consume；12 份冻结证据的
  `wham-get-paths` 断言通过，详见版本演进计划 §10.2.1。
- **实现**：使用 backend-client 独立 header 形态；不得套用 Codex 主模型端点线序。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-020 legacy compact 的 body

- **范围**：内置 OpenAI OAuth；legacy compact。
- **规则**：结构体字段为
  `model, input, instructions?, tools?, parallel_tool_calls, reasoning?,
  service_tier?, prompt_cache_key?, text?`。现有 wire 子集为
  `model, input, parallel_tool_calls, reasoning, prompt_cache_key, text`。
- **源码**：[L1] `codex-api/src/common.rs:28-43`。
- **实测**：`clean-legacy-20260728T132509Z`（R）。
- **实现**：按 Option 条件省略；不得使用更宽的 ResponsesApiRequest。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-021 默认压缩使用 Remote Compaction V2

- **范围**：内置 OpenAI OAuth；默认配置。
- **规则**：默认走普通 `/responses`，并向 input 追加
  `{"type":"compaction_trigger"}`；manual 与 auto 都如此，不调用
  `/responses/compact`。
- **源码**：[L1] `core/src/compact_remote_v2_attempt.rs:71`、
  `model-provider/src/provider.rs:55-63`、
  `features/src/lib.rs:1472-1475`、`core/src/tasks/compact.rs:41-50`。
- **实测**：`relay-tui-recap-20260728T112358Z`（manual）与
  `audit-ep021-auto-clean-20260730a`（auto），均为 R。
- **实现**：默认 V2；只有显式关闭 V2 后才进入 legacy compact。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-022 独立 images 端点形态

- **范围**：内置 OpenAI OAuth；图像生成／编辑。
- **规则**：
  generations 请求 body 为 `prompt, background, model, quality, size`；
  edits 在首位增加 `images`，内容为 data URL，不使用 multipart。
  两者 header 线序均为
  `version, authorization, chatgpt-account-id, content-type, accept, originator,
  user-agent, cookie, host, content-length`。
- **源码**：[L1] `codex-api/src/images.rs:5-30`、
  `codex-api/src/endpoint/images.rs:33-68`、
  `ext/image-generation/src/tool.rs:279-336`。
- **实测**：`clean-image-20260728T132405Z`（generations）与 `relay-imgedit1`
  （edits），均含 R。
- **实现**：`n=None` 时省略；edits 以内联 data URL 发送。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-023 压缩选择与 reason

- **范围**：派生／内部机制。
- **机制**：TokenBudget 分支不产生摘要出站；远程压缩默认选择 V2，关闭
  `remote_compaction_v2` 后选择 legacy；非远程 provider 才可能走 local inline。
  reason 为 `user_requested`、`context_limit`、`model_downshift`、
  `comp_hash_changed`。遥测 implementation 标签只有
  `responses`、`responses_compaction_v2`、`responses_compact`，不能与运行时分支一一等同。
- **源码**：[L1] `analytics/src/facts.rs:354-367`、
  `core/src/tasks/compact.rs:34-65`、
  `core/src/compact_token_budget.rs:21-92`、
  `core/src/session/turn.rs:979-1208`、
  `core/src/compact_model_fallback.rs:30-40`、
  `core/src/compact_remote_v2_attempt.rs:71`。
- **实测**：`relay-tui-recap-20260728T112358Z`（user_requested）、
  `audit-ep021-auto-clean-20260730a`（context_limit）、
  `audit-ep023-comphash-20260730b`、`audit-ep023-downshift-20260730b`（R）。
- **实现**：对第三方请求做 Codex OAuth 转换时必须保持选择语义和 reason；官方请求已含
  compaction_trigger 时不得再次压缩。内部函数结构无需相同。
- **状态**：🟡 源码充分；抓包有限，wire 只能验证分派结果。

### SPEC-EP-024 /compact 是 TUI 采集入口

- **范围**：采集与观测记录。
- **记录**：TUI 将 `/compact` 解析为手动压缩；`codex exec '/compact'` 将其作为普通
  user message 发送，不产生 compaction_trigger 或 legacy compact 请求。
- **源码**：[L1] `tui/src/slash_command.rs:40`、
  `tui/src/chatwidget/slash_dispatch.rs:262`。
- **实测**：`relay-tui-recap-20260728T112358Z`（TUI 正例）与
  `audit-ep024-exec-negative-clean-20260730a`（exec 负例），均为 R。
- **实现**：不实现采集入口；产品若提供 TUI 兼容层，再按 surface 区分 slash command。
- **状态**：✅ 源码充分；抓包充分。

# 第三部分 Sub2API 客户端仿真实现

## 3.1 总体架构与 persona 边界

为实现第一部分的总体目标，账号选定后统一绑定 active ReleaseBundle，并由 Compiler、Executor、
受信传输适配器和 Runtime Guard 完成最终出站定型。官方与第三方入站兼容层只提交协议、模型、
工具、请求语义和可验证条件；Key、Group、账号路由和计费仍由原有业务系统管理，兼容层不拥有
版本身份、传输画像或最终 wire。

| 平面 | 拥有 | 不得影响 |
|---|---|---|
| 版本发现与入口归一化 | GitHub `/releases/latest`／列表回退、6 小时节流与启动防抖、UA/version 配对和账号 UA 兼容、`openai_codex_client_version_synced`、管理端候选值、客户端名和环境指纹 | active ReleaseCatalog、画像摘要、最终 version 和 wire 契约 |
| active strict wire | ReleaseCatalog、ReleaseBundle、Compiler、Executor 和受信 adapter 定型 URL、Header、Body、顺序、压缩、传输、状态与连接 | 被候选版本、管理员／账号 UA 或入站身份覆盖 |

当前 active strict wire 是 Codex CLI 0.147.0；自动同步只更新候选值，active ReleaseCatalog 只能经证据验收后显式发布。

```text
HTTP／WS 入口、辅助端点与内部任务
→ 原有鉴权、Key／Group、账号调度、计费和协议兼容层
→ IngressSnapshot + OfficialRouteCatalog
→ codex-cli persona：ReleaseCatalog → ReleaseBundle → Plan → Executor
→ PreparedRequest + FinalizationToken + TransportSpec
→ 受信 HTTP／req-profile／WebSocket adapter → Runtime Guard → OpenAI
```

| persona／状态 | 端点范围 | 逻辑出口 | 约束 |
|---|---|---|---|
| `codex-cli` | ReleaseBundle 登记的 Codex 端点闭集 | `CodexEgressExecutor` | URL、Header、Body、传输、状态与生命周期由同一 Bundle 驱动 |
| `chatgpt-web/chrome` | privacy settings、accounts check、subscriptions | 浏览器身份 Plan／client | 保持独立 Chrome TLS／HTTP2／XHR 语义，禁止套用 Codex 画像 |
| `transport_only` | authorization-code OAuth exchange | 独立受审 transport | 只复用有证据的传输事实，不冒充 refresh 的 Body／行为契约 |
| `unclassified` | PAT whoami、Agent Identity task register | 精确登记的遗留路径 | 完成官方行为举证前不得凭官方 host 自动归入 Codex persona |
| 未登记 | Catalog 未知 route 或 SinkBinding | 无长期出口 | enforce 状态下 fail-close |

画像由 OAuth 账号类型和 registry release 指针选择，不由入站版本、平台或 surface 决定。入口分类只用于协议／语义适配、
传输选择、观测和条件类型识别，条件成立时也只保留类型语义；Compatible／Responses 先适配语义再进统一定型点，
官方 HTTP 入站保留 HTTP fallback 事实，
第三方入站按 active 画像选默认传输。

| 入站事实 | 处理 |
|---|---|
| UA／version／originator、会话 UUID、`client_metadata` | 不拥有 wire 身份；按 release Build 和 active 命名空间重建 |
| Header、Body 或 `client_metadata` 身份冲突 | 丢弃冲突原值，派生同一生命周期的新身份 |
| 条件事实缺失或冲突 | 按条件不成立处理，不伪造 Header |
| 顶层字段超出闭集 | 删除并按“入口类型 + 字段集合”去重告警 |
| 已知值不合契约（如 `tool_choice != auto`） | 按画像规范化，不拒绝请求 |

账号配置非法、route／Sink 未登记或终态篡改时 fail-close，其余身份不匹配只投影并告警。生产 HTTP／WS
不执行入站官方身份逐字段一致性校验；该校验只用于离线夹具、画像诊断和证据复算。

## 3.2 Codex 0.147.0 active 画像与发布执行契约

0.147.0 active 画像以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排；0.145.0 previous 画像继续不可变保留。
启动期解码、结构校验和摘要核对失败即阻止启动；运行时只读不可变快照，需改写的数据按次
深拷贝。当前 active 画像摘要为：

```text
94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc
```

previous 0.145.0 画像摘要为
`e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b`，只用于回滚和历史复算，
不得作为 active wire 的默认值。

摘要变化即完整画像变化，必须同步检查第二部分规则、版本清单、测试和抓包。ReleaseCatalog 启动时加载
release graph 和 SnapshotCatalog；`version + digest` 是不可变坐标，active／previous 指向完整 release ID，
未登记坐标不得回退。ReleaseBundle 冻结身份、端点、Header／Body、feature、传输、连接、策略和 fallback 图；
调用方只提交 release mode 和业务事实，不得选择具体版本或 transport ID。

CodexEgressPlan 只保存业务事实、IdentityMode、深拷贝后的 Header Override、各类 Policy 和
attempt-owned Body；`TransportSpec` 只能来自端点画像。Header 所有权固定如下：

| Header 类别 | 所有者与优先级 |
|---|---|
| Transport、Auth、Host、长度和 Body framing | 系统最终所有，账号及入站不能覆盖 |
| Release identity | strict／mimic／proxy 模式下由画像最终决定 |
| Account extensions | 普通 API Key 按产品规则覆盖；mimic／proxy 仅允许非保护字段 |
| Endpoint closed set | OAuth 官方端点删除画像闭集之外的字段 |
| Ingress headers | 最低优先级，只进入明确允许的字段 |

编译器生成语义终态；Executor 签发 FinalizationToken、注入 TransportSpec 并选受信 adapter，adapter 只能执行
Token 声明的 wire 等价变换，其余终态修改拒发。可重放 Body 按内容摘要创建新 attempt；single-use stream 不得预读、复制或多次尝试。

SnapshotDoc／ProfileSpec 保存版本事实，ExecutableProfile 校验可执行闭集。新增版本只能追加
快照、发布图节点和证据，不原位覆盖旧画像，也不在 §3.5.2 共享接入点散布版本分支；文本与
Go AST 版本泄漏门禁负责执行这一约束。

版本泄漏 baseline 只记录尚未消除的既有债务，不是新版本硬编码的批准清单。换版或 promotion
不得把新指纹写入文本／Go AST baseline 以换取门禁通过；只有命中减少、语义已迁入版本化 Snapshot
或确认属于画像文件豁免时，才能在独立审阅后收紧 baseline。任何新增指纹都必须先修复或返回
candidate，不得带入 production tree。

## 3.3 最终出站定型

### 3.3.1 运行上下文

入口只保存协议／语义事实、传输状态、业务历史和可验证条件类型；账号选定后绑定 OAuth、
release Build、版本画像、端点、模型能力、Lite、turn-state 和条件 Header。surface、终端指纹、
originator、版本及 suffix 默认值来自受信 ReleaseCatalog Build，而不是入站客户端。上下文只
属于当前 invocation、attempt 或 WS 连接。

### 3.3.2 URL、header 与 body

画像拥有 host、固定 path/query、Header 槽位和 Body 闭集；只允许画像声明的动态字段。
HTTP/1.1 在写出前定型大小写、顺序、host 和长度，WS 按 tungstenite 线序定型，Body 保持
稳定字段顺序和 JSON 数值保真。服务端返回的文件上传签名 URL 是完整动态 URL 的唯一例外。

Compiler 对静态 endpoint 的调用方 URL 不做宽松归一化，而是以本次 invocation 已绑定的
ReleaseBundle endpoint 和 protocol 为权威执行以下封闭校验：

| URL 成分 | Compiler 契约 |
|---|---|
| 形态与 authority | 拒绝 opaque、userinfo、fragment、`ForceQuery` 和所有显式端口；HTTP／WS 分别只接受精确小写 `https`／`wss`，Host 与画像逐字相等 |
| path | `EscapedPath()` 必须与画像模板段数相等；字面段逐字相等，`{param}` 段非空 |
| query | 画像名称不得为空、`*` 或重复，source 只允许 `constant`／`server_response`，`constant + required` 值非空；输入拒绝空 component、解析失败、多值、画像外键、required 缺失和 constant 改写，但允许键顺序与合法等价转义差异并保留原始 `RawQuery` |

`server_response` query 不得从 URL 自证可信；唯一通道 `EndpointDynamicInputs.ServerResponseQuery` 的键必须位于
画像闭集，值非空且与受信响应事实逐字相等（realtime sideband 提交 `record.CallID`）。Compiler 在入口深拷贝该 map。

ReturnedURL 动态 endpoint 以服务端完整 URL 为权威，与 `ServerResponseQuery` 互斥。校验失败时 Compiler 不产生
`CompiledExecution`，Executor 不签发 `FinalizationToken` 或调用 adapter。

### 3.3.3 状态、连接与端点编排

turn-state 按 invocation／连接身份隔离，只从画像规定的响应位置更新。跨请求复用要求入站
会话头或入站 Body **显式携带** `prompt_cache_key`；必须以 `promptCacheKeySet` 判断，兼容层
自动生成的同名键不是显式锚点。没有可信锚点时仍可确定性派生当前 turn 身份，但不跨请求
复用上游状态，避免把一段对话的状态句柄带入另一段。

HTTP Client、retry、WS 和长生命周期 Client 均由端点画像声明；images、compact、
alpha-search、realtime、WHAM、OAuth refresh 和文件上传不得旁路统一执行器。

## 3.4 42 项覆盖与验收边界

42 项包括 13 项 TLS／协议／连接／h1／WS、26 项 Header／Body／端点和 3 项运行上下文／turn-state／压缩机制。
每项必须同时有画像／执行点、官方与候选证据、机器断言；官方与第三方入口绑定同一源码树、镜像、
profile digest 和规则清单。多账号调度、计费和服务级请求节奏不在 42 项内，画像不改写它们。

## 3.5 源码改动台账

| 台账项 | 当前值 |
|---|---|
| upstream 基线 | `v0.1.171` peeled commit `f0e7a9c7a23a7d02fb159b62fa809621eb0475a6` |
| 完整 overlay | `docs/egress/maintenance/upstream-v0.1.171-egress-merge-ledger.json` |
| 机器范围 | `strict_surface ∪ required_review_touchpoint ∪ identity_boundary` |
| 人工范围 | §3.5.2 的 12 个高风险接缝 |

overlay JSON 是文件路径、`upstream`／`fork` 来源、范围标签、计数和联合摘要的唯一事实源。

更新 upstream 基线时执行：

~~~bash
python3 tools/check_ledger_completeness.py --write-upstream-merge-ledger
git diff -- docs/egress/maintenance/upstream-v0.1.171-egress-merge-ledger.json
make check-egress-spec
~~~

必须检查 JSON 差异；常规门禁从当前源码复算并逐字段核对台账。

### 3.5.1 Fork 自有画像与执行核心

下表定义 Fork 自有模块的当前所有权；精确文件闭集以机器台账为准。

| 路径组 | 责任 |
|---|---|
| `backend/internal/officialegress/` | ReleaseCatalog、RouteCatalog、Scope、Compiler、Executor、Guard、FinalizationToken 与画像契约 |
| `backend/internal/service/official_egress_codex_*`、`official_client_profile_registry.go` | 0.145.0／0.147.0 不可变 Snapshot、可信 release Build 运行态投影、发布投影、端点编排、Files 与模型能力 |
| `backend/internal/service/official_egress_openai_http.go`、`official_egress_openai_ws.go` | HTTP／WS 统一入口归一化：保留业务语义，重建动态身份，禁止官方／第三方入口形成两套 wire 权威 |
| `backend/internal/service/official_egress_*invocation.go`、`official_egress_transport_adapters.go` | HTTP／WS invocation、attempt 和受信 terminal adapter |
| `backend/internal/service/official_egress_upstream_identity_bridge.go` | 把上游身份设施的 canonical/version 读取源单向桥接到 active 已验收 ReleaseBundle |
| `backend/internal/pkg/tlsfingerprint/`、`backend/internal/repository/official_egress_guard.go` | TLS／HTTP/1.1 wire、连接资源和 socket 前最后一道 Guard |
| `backend/internal/service/openai_forward_plan.go`、`account_test_service_openai_files.go` | 版本中立 Plan、fallback transition 和独立 Files 生产探针 |
| `backend/internal/platform/liveattestation/` | 有证据的条件 attestation；缺失时保持缺失，禁止伪造 |

Codex 版本通过新增 Snapshot、Release 节点、证据与测试实现，不复制执行引擎，不在共享业务层
增加版本分支。Executor AST、Runtime Sink、final-wire 和变异负例共同阻止旧发送路径恢复。

### 3.5.2 高风险人工复核缝

下表定义必须人工确认的所有权和调用顺序；完整 overlay 以结构化 JSON 为准。
`tools/check_ledger_completeness.py` 校验 12 个精确路径及本节说明。

| 边界 | 精确路径 | 合并上游时必须确认 |
|---|---|---|
| 上游 UA 组装与归一化 | `backend/internal/pkg/openai/request.go`；`backend/internal/service/openai_codex_identity.go` | 只复用客户端名、OS／架构／终端指纹和 UA/version 配对机制；任何输入版本段都按 active 已验收版本重建 |
| 版本发现与运行设置 | `backend/internal/service/openai_codex_version_sync_service.go`；`backend/internal/service/setting_gateway_runtime.go` | GitHub `/releases/latest`、列表回退、6 小时节流和启动防抖只更新 `discovered_latest`，不得写入 active release |
| 单向身份桥 | `backend/internal/service/official_egress_upstream_identity_bridge.go`；`backend/internal/service/wire.go` | 上游 canonical resolver 只能读取 active ReleaseBundle；依赖注入不得回接“最新发现版本”或形成第二版本事实源 |
| 身份事实与终态权限 | `backend/internal/service/official_egress_identity_authority.go`；`backend/internal/officialegress/compiler.go`；`backend/internal/officialegress/executor.go` | Authority 只组装事实，Compiler 只生成语义终态，Executor 是唯一有权签发 FinalizationToken 并决定最终 wire 的组件 |
| WebSocket 握手 | `backend/internal/service/openai_ws_forwarder_payload.go` | 入站或账号 UA 只可用于观测／协议兼容，不得选择 surface、写入最终 version，或在终结后补写握手头 |
| WHAM／用量探针 | `backend/internal/service/openai_quota_service.go` | 可复用上游缓存和配置读取，但账号 UA 不拥有 wire 身份；真实请求仍必须进入同一 Executor、ReleaseBundle 和传输画像 |
| 管理端可观测性 | `frontend/src/views/admin/SettingsView.vue` | “发现到的最新版”与“strict 当前生效版本”分开展示，禁止把自动发现描述成自动激活 |

人工复核必须确认版本权威方向和终结顺序；其余路径的来源与范围由机器台账核对。

### 3.5.3 抓包与验收工具

| 路径组 | 责任 |
|---|---|
| `tools/official_client_capture/codex_upgrade.py` | 唯一升级、抓包、比较和验收入口 |
| `tools/official_client_capture/codex_upgrade_*` | Campaign Schema、环境探针、收据和版本清单 |
| `tools/official_client_capture/capture.py`、`capturelib/` | 受编排器调用的底层采集与安全生命周期 |
| `tools/official_client_capture/pcap_clienthello.py`、`relay_extract.py`、`scrub_raw_bytes.py` | TLS／应用字节解析与脱敏 |
| `tools/check_*`、`tools/evidence_index.py`、`tools/spec_status.py` | 规格、台账、版本泄漏和证据门禁 |

正式工具链只包含版本场景清单引用的脚本。

## 3.6 包边界、依赖方向与上游合并缝

`officialegress` 持有画像、Catalog、Compiler、Executor 和 Guard；`service` 只提交业务事实和 Plan，
`repository` 只提供连接池、代理和底层资源。
稳定引擎只解析画像、定型 wire 并管理传输／状态，不承担版本常量、模型特判或业务归属；版本画像也不承担账号、计费或协议桥接。

```text
service ───────────────────→ officialegress core
repository ────────────────→ officialegress core 的窄 port
officialegress/adapter/* ───→ core + 受信物理资源
wiring ────────────────────→ 注入闭集 adapter
```

`officialegress` 不得 import `service`／`repository`；公共边界只暴露中立 Plan、Release 和窄 port。adapter 以闭集登记、
AdapterID、FinalizationToken、wire 测试和静态门禁建立信任。新版本新增画像和发布图；只有新机制无法表达时才最小修改共享引擎并专项复验。

## 3.7 Guard、逐 Sink 灰度与静态门禁

Runtime Guard 先按 method、host、path、protocol 匹配 route，再验证 persona、SinkID、binding 和状态；canary／enforced 还必须验证
FinalizationToken、Release／Profile／Pool digest、adapter 和最终请求摘要。未知 route、无效 binding 或终态篡改按策略 fail-close。
摘要独立绑定 `ForceQuery`，签发后增删裸 `?` 也是篡改；受信 adapter 的 `wss → https` 等价变换仍按规范 scheme 计算。

状态只按 `legacy_observe → canary_enforce → enforced` 单调前进（enforced 可回滚至 canary）。`legacy_observe` 只容纳封存基线，新 sink
必须从 canary 进入；紧急 observe 限定 Sink 和期限，不扩大 legacy 基线。静态门禁覆盖 net/http、HTTPUpstream、req/v3、WS、facade 和 client factory，
并用变异测试发现包装旁路；Catalog 项只凭 MigrationReceipt／RemovalReceipt 单调迁移或删除。

## 3.8 行为策略与稳定策略来源

非用户直接触发的请求还必须定义触发条件、频率、并发、副作用和删除期限。普通 OAuth 用量
刷新采用 WHAM-first；只有结构或兼容条件允许时才在同一调用进入画像化 Responses fallback，
凭据失效和安全错误不得被 fallback 掩盖。

以下 ASCII anchor 是生产策略 `PolicySource` 的稳定引用，anchor 不得复用：

<a id="policy-changeset-1b"></a>

- `policy-changeset-1b`：WHAM-first、管理端 Responses／compact、alpha-search 及已知画像修复。

<a id="policy-changeset-2"></a>

- `policy-changeset-2`：不可变 ReleaseBundle、单次解析、transport adapter 与正式回滚。

<a id="policy-changeset-3"></a>

- `policy-changeset-3`：21 个 Codex Runtime Sink 的统一 Executor、Forward 与辅助端点收敛。

## 3.9 当前实施状态与兼容边界

当前 21 个 `codex_profile` Runtime Sink、28 条 route 全部 `enforced`，无 Codex `legacy_observe`；HTTP、WS、fallback、models、
images、files、alpha-search、WHAM 和 OAuth refresh 都进统一 Executor。ReleaseCatalog 预编译不可变 active／previous，
attempt Body 单次解析与有序输出，重复键 fail-close。active 0.147.0 Snapshot 有 16 个静态 endpoint
（包含新增 `wham_settings_user`）和 1 个 ReturnedURL 动态 endpoint；previous 0.145.0 Snapshot
保留 15 个静态 endpoint 和 1 个 ReturnedURL 动态 endpoint。`server_response` query 经受信通道提交。

官方与第三方 OpenAI 入口共用 HTTP／WS 归一化和身份派生；身份冲突不返回 502，逐字段校验只用于离线证据与诊断。
机器清单冻结 strict surface 和相对 `v0.1.171` 的完整 overlay，Markdown 只保留 12 个高风险接缝；active／previous final-wire
使用空允许列表，实机以已接受 Campaign 和部署报告为准。

当前兼容边界如下：

| 分类 | 当前决定 | 原因 |
|---|---|---|
| active／previous、per-sink canary／override、FinalizationToken | 保留 | 提供升级、回滚和防旁路能力 |
| browser persona、OAuth exchange transport-only、unclassified sink | 独立治理 | 不属于 Codex Executor persona，禁止混用画像 |
| service 画像 DTO／projection | 保留 | API Key mimic 和业务读取面仍有生产消费者 |
| unsigned `LegacyCompiledDispatcher` HTTP 执行路径 | 禁止执行 | 当前 Codex Runtime Catalog 全部 enforced，旧上下文进入通用发送入口时 fail-close |

兼容代码的完整删除条件和顺序见 §5.2。

---

# 第四部分 Codex CLI 版本演进流程

本部分规定 Codex CLI 版本演进流程。每次升级必须基于目标版本的官方源码和真实 wire 提取规则，
完成 Sub2API 候选实现的一致性验收，并保证新旧画像可以并存和完整回滚。全文以下列六步为
唯一主流程。后继版本可以复用工具，不能复用目标版本应独立取得的证据或缩减步骤。

| 步骤 | 核心问题与主要产出 | 状态／完成事实 |
|---|---|---|
| 1. 目标版本取证与规则差异发现 | 官方客户端实际会发出什么；形成第二部分规则及源码／wire 证据 | 新建 Campaign → `official_sealed` |
| 2. 新旧规则比较与批准 | 继承、改变、新增或删除了什么；批准五份清单及联合摘要 | `official_sealed → profile_approved` |
| 3. 目标画像实现与候选构建 | Sub2API 如何表达目标规则；生成候选 Catalog、收据和制品 | 保持 `profile_approved` |
| 4. 候选行为验证与封存 | candidate 是否从必要入口产生目标 wire；封存候选证据 | `profile_approved → candidate_sealed` |
| 5. 官方—候选比较与验收 | 两侧是否逐规则一致；生成 comparison、断言和验收 seal | `candidate_sealed → compared → ready` |
| 6. 生产启用与回滚闭环 | 生产是否运行目标画像且可完整退回；生成晋升、激活与回滚收据 | Campaign 保持 `ready`，生产事实独立封存 |

**升级前置与控制约定（不计入六步）**

正式 Campaign 前先完成可丢弃的 DOC-PRE／P0；预检只发现阻断，不形成目标版本证据。修复并
复验后，才从干净、同源的受管树创建 Campaign。P0 只核对：

| 类别 | 通过条件 |
|---|---|
| 身份与角色 | 冻结官方二进制、源码／依赖／平台／feature／镜像／网络条件；机器职责正确，执行副本、测试树和 finalizer 同源 |
| 账号与工具 | 场景所需账号、模型、额度、请求键、观测／安全／finalizer 工具和构建资源可用 |
| 环境恢复 | 端口、挂载、容器、hosts、代理、CA、数据库及托管字段可在 before／after 语义下恢复 |
| 生产基线与隔离 | 只读记录镜像、compose、选择器、Active／Previous 和依赖服务；P0 账号、环境与正式 Campaign 隔离 |

DOC-PRE 只登记并审核本次 maintenance transition；合并后从干净 HEAD 执行 P0。P0 产物标为
`preflight-only`，不得发送真实请求、使用 `--acknowledge-live-requests`、创建正式 Campaign，
也不得修改 Active／Previous、运行环境或历史证据。阻断修复作为独立变更提交，随后重跑 P0。

**控制对象与身份边界**

Campaign 是一次目标版本升级的不可变证据容器；candidate 是其中一个固定的 Sub2API 源码与
镜像实现；attempt 是身份不变时的一次只写采集记录。身份变化必须按下表新建对应单元：

| 单元 | 必须新建的变化 |
|---|---|
| 版本 Campaign | 目标版本、官方二进制／源码／依赖／平台／默认 feature，或批准规则、场景、画像和断言发生变化 |
| 同版本后继 Campaign | 受管工具影响证据含义、环境无法证明恢复，或已冻结的机器角色、执行副本和 finalizer 身份错误 |
| 同 Campaign 新 candidate | Sub2API 源码树、测试树、构建 ID、部署版本、OCI digest、image ID 或 profile ID／digest 发生变化 |
| 同 candidate 新 attempt | 冻结身份不变，仅因允许重试的网络、配额或临时运行失败重跑；新 attempt 不覆盖失败记录 |

受管工具按证据影响面分级：采集、探针、relay、脱敏、收据生成、环境快照和编排等**产出侧**
变化会改变证据字节，必须新建 Campaign；只读封存证据的**评估侧**仅在显式白名单内允许漂移，
且须写入漂移台账并重放全部受影响门禁。新增或未分类工具默认属于产出侧；没有分组摘要的旧
Campaign 继续按“任何工具漂移均拒绝”处理。被校验的工具树必须就是实际执行的工具树。

Campaign 状态只按
`planned → official_sealed → profile_approved → candidate_sealed → compared → ready`
单向前进；§4.6 的生产事实由 promotion／activation receipt 独立证明。`status` 只读推导状态；
`resume` 只能新建身份未变且允许重试的 attempt。核心清单、attempt、result 和 seal 只写一次，
不得通过补写、覆盖或借用证据跨过身份变化。

**权威资产与证据保留**：`classification/approved/` 是批准清单的唯一事实源；SnapshotCatalog、
ReleaseCatalog、晋升收据、镜像 digest 和 activation fact 是生产选择事实。证据位置、复算与
保留遵守 §2.1.1、§2.1.3；历史资产只追加、敏感原文不进 Git。新画像完整追加且 Active／Previous
同时可执行；版本升级、上游更新和兼容代码退休分别实施。

## 4.1 目标版本取证与规则差异发现

本步使用统一编排工具，从目标官方源码和真实抓包整理出第二部分的完整编号规则；正式取证前
必须通过本部分的 DOC-PRE／P0。

### 4.1.1 工具与输入

唯一入口（底层采集、场景、relay、解析和 finalizer 不单独形成流程）：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

开始前准备以下输入：

| 输入 | 内容 |
|---|---|
| 基线 | 与当前第二部分编号一致的规则清单、官方源码、官方证据和场景清单 |
| 目标 | Codex CLI 版本、官方源码、Cargo.lock／依赖、二进制及 SHA-256 |
| 取证条件 | 目标平台、运行镜像、默认 feature、模型、账号、代理和 TLS 条件 |
| 运行位置 | Campaign 目录、采集机、证据目录和环境恢复坐标 |

正式 Campaign 目录必须是持久、绝对、尚不存在且不经过符号链接的路径，不得位于临时目录。
普通、Lite 等互斥条件使用独立 track、job、evidence root 和 receipt；`track_not_applicable` 只
表示本轨不适用，不能替代联合覆盖。只有两侧模型及其他取证条件相同时，差异才可归因于版本。

### 4.1.2 执行顺序

| 顺序 | 命令 | 动作与机器产物 |
|---:|---|---|
| 1 | `codex_upgrade.py plan` | 冻结输入并扫描新旧源码，生成 `analysis/target-source.json`、`analysis/source-diff.json` 和 `analysis/baseline-surface.json` |
| 2 | `codex_upgrade.py capture-official run` | 按场景清单运行目标官方 CLI，采集 HTTP、WS、TLS、端点、状态和错误分支的真实证据 |
| 3 | `codex_upgrade.py capture-official seal` | 校验恢复、证据权限、秘密扫描、inventory 和 finalizer 产物，封存官方证据并进入 `official_sealed` |
| 4 | `codex_upgrade.py classify` | 不传批准清单，只生成发现结果和分类草案，不执行批准 |

第 4 项生成官方 wire 差异 `classification/discovery/baseline-to-target-official.json`，以及
`classification/draft/<revision>/` 中供第二步审核的规则、迁移、场景、画像和断言草案。

随后人工逐项复核 `source-diff.json`、`baseline-to-target-official.json`、目标源码及原始抓包：

1. 从生产入口追到认证、Client、TLS、传输、Header、Body、端点和跨请求状态；
2. 判断每项行为的范围、触发条件、固定／随机／条件属性及可观测边界；
3. 将可成立的目标行为写入第二部分，保持“范围—规则／机制／记录—源码—实测—实现—状态”
   字段完整；
4. 让 `target-rules.json` 的规则编号与第二部分一一对应，每项证据都能重新定位和解析。

工具负责扫描、抓包、解析、差分和生成机器草案，**不会自动编写第二部分的 Markdown 规则正文**；
规则正文必须依据源码和 wire 证据人工整理。`classify` 在本步只生成草案，第二步才提交五份审核
清单并批准联合摘要。

### 4.1.3 产出与退出条件

本步产出第二部分目标规则、逐规则官方源码／依赖／P／R／J／M 证据和 Campaign 机器草案。
退出条件：规则编号与 `target-rules.json` 一致，所有发现均已进入规则或标为待第二步处理的
`blocked`；证据引用可回到源码和抓包，官方场景、恢复、安全、inventory 和 seal 全部通过，
Campaign 达到 `official_sealed`。

## 4.2 新旧规则比较与批准

本步把目标规则与当前基线逐项比较，完成人工分类、完整画像准备和五份清单批准；第一步的
`classify` 只生成草案，本步才正式批准。

### 4.2.1 输入与分类

输入为第二部分目标规则、源码／官方 wire 差异，以及 `classification/draft/<revision>/` 的
五份草案。

逐条填写 `rule-migration.json` 的规则迁移和 discovery 分类：

| 分类 | 含义 |
|---|---|
| `inherit` | 新旧规则编号和行为均保持不变 |
| `change` | 规则仍存在，但可见行为发生变化 |
| `add` | 目标版本新增规则或出站面 |
| `delete` | 基线规则在目标版本中已不可达 |
| `condition_change` | 行为本身存在，但触发条件发生变化 |
| `blocked` | 证据不足，暂时不能得出结论 |

先判断触发条件、证据面、平台和会话状态是否可比，再判断行为是否改变；不得因为版本号相同、
源码 diff 很小或一次抓包未出现就直接继承或删除。`delete` 必须同时具备目标源码不可达结论、
覆盖触发条件的正反场景、旧规则引用清单和 RemovalReceipt，否则保持 `blocked`。

`source` discovery 仅在源码树和指纹完全一致时可以继承分类；`dynamic` discovery 绑定本轮真实
证据，必须按本轮指纹重新分类。摘要相同只证明输入结果一致，不能证明端点完整、路径可达或
规则无遗漏；完整性仍由源码、wire、场景覆盖和跨清单门禁共同证明。

### 4.2.2 画像准备与清单审核

在 `official_sealed` 状态执行 `codex_upgrade.py prepare-profile`，输入官方取证形成的完整 Snapshot、
目标 `profile_id` 和 Campaign 外的输出路径，生成待审核 `profile.json`。随后完成以下五份清单：

| 清单 | 审核内容 |
|---|---|
| `target-rules.json` | 目标版本完整规则编号，与第二部分一致 |
| `rule-migration.json` | 新旧规则迁移、全部源码／动态发现分类及证据引用 |
| `scenarios.json` | 官方与 candidate 场景、规则覆盖和目标画像绑定 |
| `profile.json` | 完整目标 Snapshot、profile ID／digest，必需状态为 `approved` |
| `assertion-profile.json` | 逐规则断言、场景选择及第二部分摘要绑定 |

规则、场景、画像、断言和端点集合必须做跨清单一致性检查，禁止出现“判据认识某端点但画像
无法表达”等自相矛盾状态。`rule-migration.json` 必须设为 `approved`，所有 discovery 均有唯一
分类和证据引用。

### 4.2.3 机器校验、人工批准与产出

1. 使用 `--target-rule-manifest`、`--migration-manifest`、`--scenario-manifest`、
   `--profile-manifest` 和 `--assertion-profile-manifest` 执行 `codex_upgrade.py classify`，暂不提供
   `--approve-manifest-sha256`。工具校验版本、规则全集、迁移闭环、场景覆盖、画像摘要、断言
   以及官方任务结果；通过后返回 `approval_required` 和 `joint_manifest_sha256`。
2. 人工复核五份清单的逐文件 SHA-256 和 `joint_manifest_sha256`，不得手写或替换机器摘要。
3. 使用完全相同的五份清单再次执行 `classify`，并通过 `--approve-manifest-sha256` 回传该联合
   摘要。工具将清单只写一次地保存到 `classification/approved/`，不得覆盖旧批准结果。

本步产出 `classification/approved/` 中五份不可覆盖的批准清单及 `joint_manifest_sha256`。
退出条件：规则与 discovery 无未分类项、`blocked=0`、联合摘要获人工批准，Campaign 进入
`profile_approved`。

## 4.3 目标画像实现与候选构建

本步把批准画像编译为不切换 Active 的候选 RuntimeCatalog，纳入候选源码树并构建制品。新画像
完整追加，不原地修改旧画像；candidate 身份到第四步 `capture-candidate run` 才冻结。

### 4.3.1 画像暂存

Campaign 必须处于 `profile_approved`，并且 `classification/approved/` 中五份清单及
`joint_manifest_sha256` 均未变化。执行：

```bash
python3 tools/official_client_capture/codex_upgrade.py stage-profile \
  --campaign-dir /绝对路径/campaign \
  --output /绝对路径/新的候选-runtime-catalog
```

`--output` 必须是 Campaign 外尚不存在的绝对路径。工具从五份清单生成候选 RuntimeCatalog 和
`catalog-stage-receipt.json`，不修改仓库或生产 selector。收据必须证明：

- `campaign_id`、`classification_sha256`、`target_version` 和 `target_profile_digest` 与批准事实一致；
- `active_unchanged=true`、`production_selector_changed=false`、`candidate_release_mode=previous`；
- `inventory` 与 `inventory_sha256` 精确覆盖输出目录，逐文件摘要和大小均可复算。

### 4.3.2 画像入库与实现边界

将暂存的 Snapshot、Release graph 和 Catalog 清单经审核纳入 candidate 源码树；
`stage-profile` 的确定性生成不替代入库审核。必须满足：

1. 目标 Snapshot／Release 是新增节点，旧 Snapshot／Release 的路径、内容和摘要保持不变；
2. 批准清单中的 endpoint、header、body、TLS、连接、状态和路由规则均可由新画像表达；
3. 新增端点的所有相关 mode 同步具备 binding、Bundle resolver、route catalog、迁移收据和
   release proof，缺少任一项时失败关闭；
4. 生产 Active selector 保持不变，目标 Release 只作为 `previous` 候选供第四步显式选择。

版本新增 route 允许在确实不含该端点的单个 Release 中零匹配，但 Compiler 验证的 endpoint ID
必须等于 Active／Previous 的并集，每条 runtime-bindable route 在并集中至少有一个 binding；
单 Release 多匹配、并集零匹配或错误 override 均失败关闭。切换时在途 invocation 保持原 Bundle，
新 invocation 才解析新 Active；fallback 和连接池不得跨 Bundle。

只有新机制无法由现有 Snapshot、Plan、Bundle 或 Executor 表达时，才允许最小化修改共享层；
此类修改必须补充对应测试，并专项复验 Active／Previous 两个 mode，不能用共享层补丁掩盖画像
遗漏。

### 4.3.3 候选构建与退出条件

从完成入库和测试的同一最终源码树准备前端嵌入产物及运行资源，按实际目标平台构建二进制和
镜像。源码、测试、前端产物、二进制与镜像必须同源，并记录 Git／tree／build／部署版本，
二进制 SHA-256、目标架构和构建参数，以及镜像标签、image／OCI digest、profile ID／digest。

构建机可原生或交叉编译；证据机和低资源生产机不承担 Go／Node 编译。二进制架构必须匹配
部署目标，镜像身份可反向追溯至源码、测试和批准画像。

本步退出条件：候选 RuntimeCatalog 与 stage receipt 已生成并通过复算，目标画像已进入同源
candidate 源码和制品，旧 Snapshot／Release 仍完整可用，实现侧测试通过，生产 Active 保持
不变；用于第四步冻结 candidate 的全部身份字段已经齐备。

## 4.4 候选行为验证与封存

本步使用第三步产生的固定制品运行目标画像，从批准场景和真实第三方入口收集候选证据，并经
人工摘要复核封存为 `candidate_sealed`。候选阶段按 stage receipt 的
`candidate_release_mode=previous` 显式选择目标 Release；不得把当前生产 Active 当成目标画像，
也不得按客户端自报版本选择 Release。

开跑前的机器角色、环境、路径和收据检查表见
[`CAPTURE_OPERATIONS_NOTES.md`](egress/maintenance/CAPTURE_OPERATIONS_NOTES.md)；该文件只承载
操作检查和故障定位，与本规范冲突时以本规范为准。

### 4.4.1 Candidate 身份冻结与运行

确认 Campaign 为 `profile_approved`、候选服务已经按不可变镜像 digest 部署后，执行：

```bash
python3 tools/official_client_capture/codex_upgrade.py capture-candidate run \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --runtime-image <registry/repository@sha256:manifest-digest> \
  --candidate-image-id <sha256:image-id> \
  --candidate-source /绝对路径/candidate-source \
  --build-id <build-id> \
  --deployed-version <deployed-version> \
  --profile-id <approved-profile-id> \
  --profile-digest <approved-profile-digest> \
  --acknowledge-live-requests
```

工具在真实请求前复算源码和运行镜像、核对画像并原子创建 attempt，冻结源码、构建、部署、
image／OCI 和 profile 身份，生成 `attempt_id` 与 `run_nonce`。随后按 `scenarios.json` 执行任务并
采集环境探针，成功返回 `awaiting_receipts`；测试轨迹、客户端收据、业务事件、恢复、安全、
inventory 和 seal 必须全部绑定该 candidate／attempt／`run_nonce`。

attempt 的 `source_tree_sha256`、`--candidate-source` 实测摘要、运行 activation fact 和镜像
构建证明必须指向同一源码树，任一不一致立即停止，不得靠镜像内字符串或版本号推定同源。

### 4.4.2 场景验证与第三方入口

`scenarios.json` 是任务、规则覆盖和必需客户端的事实源。每条规则必须有真实触发场景；每个
入口只证明自身适用规则及向同一 candidate Release 收敛，不必分别触发规则全集：

| 入口 | 必须证明 |
|---|---|
| 官方 Codex CLI／Desktop | 无论客户端声明的版本、平台、surface 或 Header／Body 身份是否一致，均按 `previous` 候选 mode 收敛到目标 Release，不因入口身份冲突返回 502 |
| `/v1/chat/completions` | Compatible 适配后进入目标 HTTP Responses 画像 |
| `/v1/responses` HTTP | Responses 入口使用同一目标 HTTP 画像 |
| `/v1/responses` WebSocket | WS、预热和 fallback 使用同一目标版本 Bundle，不跨版本回落 |
| Kilo Compatible | `kilo-compatible` 收据绑定模型、账号、请求、响应、usage、candidate 和 profile |
| Kilo Responses | `kilo-responses` 收据绑定相同 candidate 身份和 profile |

必需任务必须全部完成。可选任务失败只有在批准的 `scenarios.json` 事先标为 optional、独有规则
覆盖为零、替代证据完整且已封存已知缺口时才不阻断；不得在失败后临时降级任务或规则。

`run` 完成后、首次 `seal` 前，真实 Kilo 必须分别完成 Compatible 和 Responses 请求；两者的
ingress、runtime、response 与 usage 绑定本次 Campaign／attempt／`run_nonce`，并位于 attempt
开始和 client checkpoint 之间。目标、基线及第三方入口必须命中同一 target release、profile、
version 和动态身份命名空间，差异只来自业务内容、条件和传输分支。

### 4.4.3 收据生成与候选封存

候选封存固定为四个阶段，不得调换 Kilo 请求与检查点的先后关系：

1. **建立检查点**：两条 Kilo 请求完成后，以 Campaign、candidate 和 attempt 三个参数首次执行
   `capture-candidate seal`；工具采集 `client-after`，返回 `client_checkpoint_created`。此后不得再
   发送本 attempt 的客户端验证请求。
2. **生成收据**：使用 `build_capture_manifest.py`、`candidate_test_trace.py`、
   `build_observed_profile_runtime_audit.py`、`build_kilo_client_receipts.py` 和
   `codex_upgrade_receipt_finalizer.py`，从本 attempt 的既有事实生成 manifest、Go test trace、
   observed-profile 和两份 Kilo 收据。生成器在 candidate 源码树外运行，正式产物写入证据根；
   草稿不能冒充 finalizer 收据。
   observed-profile 的原始 activation fact 必须由运行服务产生，工具只能校验并绑定 attempt，
   不能合成运行事实。Go test trace 必须来自同源源码树上另行执行的冻结测试日志，收据生成器
   不负责运行测试。
3. **生成预览**：携带正式产物再次执行 `seal`：

   ```bash
   python3 tools/official_client_capture/codex_upgrade.py capture-candidate seal \
     --campaign-dir /绝对路径/campaign \
     --candidate-id <candidate-id> \
     --attempt-id <attempt-id> \
     --capture-manifest /绝对路径/capture-manifest.json \
     --assertion-evidence-root /绝对路径/attempt-evidence \
     --observed-profile-receipt /绝对路径/observed-profile-receipt.json \
     --client-evidence kilo-compatible=/绝对路径/kilo-compatible-receipt.json \
     --client-evidence kilo-responses=/绝对路径/kilo-responses-receipt.json
   ```

   工具重验身份、任务、客户端时间窗、恢复、安全、inventory 和机器断言，生成
   `seal-preview.json`，返回 `approval_required` 与 `review_sha256`。
4. **批准并封存**：人工复核预览后，以相同参数追加
   `--approve-seal-sha256 <review_sha256>`；摘要一致后只写一次地保存结果，Campaign 进入
   `candidate_sealed`。

### 4.4.4 运行纪律、失败处理与退出条件

验证部署使用固定 digest，只替换应用容器并保留回滚点；运行期间不执行 `pull`、`compose down`
或 `prune`，不重建数据和依赖服务。身份变化按本部分控制对象边界新建 Campaign 或 candidate；
只有身份未变的临时失败才允许新建 attempt 或 `resume --rerun-failed`。不得覆盖失败记录、跨
candidate／attempt 补证，或在 run 与 seal 之间修改 candidate 源码树。

seal 还必须验证全部原始证据目录和文件（包括 evidence root 外的 `runs/` 资产）无 group／other
权限，目录至多 `0700`、文件至多 `0600`。确认必败时必须走受管停止和环境恢复，不得强杀并
丢失 after 探针。

本步退出条件：全部必需候选任务完成，两条 Kilo 入口均有真实正式收据，运行画像、结构化测试
轨迹、恢复、secret scan、inventory、机器断言和 evidence seal 全部通过，`review_sha256` 已获
人工批准，Campaign 达到 `candidate_sealed`。

## 4.5 官方—候选比较与验收

本步先对官方与候选封存证据做离线比较，再依据批准的 assertion profile 生成并执行逐规则机器
断言，最后由 `accept` 重放全部 Campaign 门禁。`compare` 不发请求，也不直接完成逐规则验收；
逐规则结论以机器断言结果为准。

### 4.5.1 离线比较与比较产物

官方证据和 candidate 均已 seal、Campaign 达到 `candidate_sealed` 后，比较机必须能按封存的
绝对路径完整复算两侧证据；跨机器时先同步全部 evidence roots 并执行 `status`。官方、candidate
收据及当前重放器的 finalizer `producer.tool.path` 必须一致，只有内容摘要相同不足以通过。
随后执行：

```bash
python3 tools/official_client_capture/codex_upgrade.py compare \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id>
```

工具只读重验两侧身份、inventory、恢复、任务、规则覆盖和 profile 绑定，并比较规范化 surface；
生成 `comparisons/<candidate-id>/result.json`、逐规则 `results.template.json`，Campaign 进入
`compared`。

`equal` 只比较两侧完整 surface 集合，不是验收门禁；采集计划不同可以导致 `equal=false`。
只要 comparison 为 `complete`、`offline_only=true`，coverage 和 profile binding 完整，结果仍
进入 `compared`（当前 CLI 返回码为 2）；行为一致性由下一节逐规则断言判定。

### 4.5.2 逐规则机器断言

以五份批准清单、comparison、两侧 capture manifest 和 evidence root 生成断言配置，再执行：

```bash
python3 tools/official_client_capture/build_rule_assertion_results.py \
  --config /绝对路径/assertion-config.json \
  --output /绝对路径/campaign/assertions/<candidate-id>/results.json \
  --results-dir /绝对路径/campaign/assertions/<candidate-id>/machine
```

配置必须绑定批准的 `target-rules.json`、`assertion-profile.json`、目标版本和 profile digest，
以及官方、candidate、comparison 的 package digest、capture manifest、证据根和逻辑路径前缀。
工具按批准 assertion profile 决定每条规则的 validation mode：

| validation mode | 机器判定 |
|---|---|
| `dual_wire` | 分别在官方与候选封存证据上执行同一规则的侧别检查，两侧均须通过 |
| `candidate_profile` | 在候选证据上验证 Sub2API 内部实现事实，并逐字绑定批准的官方权威摘要 |

最终 `results.json` 必须唯一覆盖目标规则全集；每条规则均为 `status=pass`、
`evidence_level=full`，机器 check 集合与批准画像完全一致，不允许 fail、N／A、手写通过结论或
未绑定 inventory 的证据路径。

### 4.5.3 正式验收与门禁

调用 `accept` 前，必须在同一 candidate 源码树完成并保留 `make check-egress-spec` 和完整回归
结果；`accept` 不会自动运行这两项。结果缺失或失败时不得继续，也不得向已 seal 的证据根补写日志。

外部门禁必须形成不可覆盖的 candidate gate receipt，至少绑定：candidate tree digest、目标平台、
完整命令及退出码、原始输出摘要、文本／Go AST 版本泄漏 baseline 摘要、测试通过／失败／跳过
数量和完成时间。`skipped` 必须拆分为批准与非预期两类；任一命令失败、`unexpected_skip>0`、
源码摘要变化或收据字段缺失时，candidate 不具备 `accept` 资格。后继版本的 `accept` finalizer
必须机器校验该收据；在受管 Schema 和校验入口落地前，不得用人工“已运行”结论替代。

外部门禁通过后执行：

```bash
python3 tools/official_client_capture/codex_upgrade.py accept \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --assertions /绝对路径/campaign/assertions/<candidate-id>/results.json
```

`accept` 重算逐规则断言，并检查分为以下四组的十五项 Campaign 门禁：

| 门禁组 | 判定内容 |
|---|---|
| 套件与身份 | full suite、官方身份与二进制、candidate 完整身份、运行 profile 绑定 |
| 比较与规则 | 离线 comparison 完成、双侧规则覆盖、逐规则断言完整、分类无阻断 |
| 恢复与安全 | 官方／候选环境恢复、两侧 secret scan、evidence inventory 摘要一致 |
| 第三方入口 | 必需客户端收据齐全，重新解析后与封存绑定一致 |

全部通过后，工具只写一次地保存断言结果、`accepted=true` 且 `failed_gates=[]` 的 acceptance
result 和 evidence seal，Campaign 进入 `ready`。失败结果以不可覆盖的 acceptance attempt 保存，
Campaign 保持 `compared`；身份变化按前置边界新建 candidate 或 Campaign，只有身份不变的评估
重放才能复用封存证据。

### 4.5.4 `ready` 的边界与退出条件

各门禁独立判定：模型收据、HTTP 200 或 `equal=true` 均不能替代目标路径、业务完成事件或逐规则断言。

本步退出条件：`make check-egress-spec`、完整回归和目标规则全集断言通过，acceptance result 为
`accepted=true`，evidence seal 已封存，Campaign 达到 `ready`。`ready` 只表示目标规则与固定
candidate 已通过证据验收，不表示已经发布镜像、切换生产、完成备份或执行生产回滚演练。

## 4.6 生产启用与回滚闭环

Campaign `ready` 只是进入生产阶段的必要条件。本步不再改变 Campaign 状态，而是以 Catalog
晋升收据、正式镜像 digest 和生产激活收据证明：目标 Release 已成为默认 Active，上一已接受
Release 是可执行的完整回滚点，并且目标版本在回滚演练后已经恢复。

### 4.6.1 生产现状对账与回滚点冻结

写操作前只读记录容器 digest、compose／override、选择器、activation fact、Active／Previous、
数据与依赖服务、网络、挂载和代理／CA。名义 Catalog、强制 mode 与实际流量不一致时必须停止
并形成受审处置。

冻结上一已接受版本的 Release／profile、镜像 digest、compose 和必要配置，并用只读数据克隆
或等价隔离证明旧镜像可启动、读取数据并通过 health／鉴权。未经验证或依赖可变标签、临时环境
变量的 `Previous` 不能作为回滚点。

### 4.6.2 Catalog 离线晋升与正式制品

在第五步接受的 candidate 源码副本上确认当前 Catalog 为“Active＝回滚版本、Previous＝已验收
目标版本”，然后在该源码的 `backend/` 目录执行：

```bash
go run ./cmd/egresscatalogpromote \
  -campaign-id <campaign-id> \
  -acceptance-sha256 <acceptance-result-sha256> \
  -target-version <target-version> \
  -target-profile-digest <target-profile-digest> \
  -rollback-version <rollback-version> \
  -rollback-profile-digest <rollback-profile-digest> \
  -output /绝对路径/新的-production-catalog
```

`-output` 必须是父目录已存在、不经过符号链接且自身尚不存在的绝对路径。工具只离线交换已
验收的两个 Release mode，不改写 Snapshot、运行服务或 registry；输出 production RuntimeCatalog、
release contract graph 和 `catalog-promotion-receipt.json`。收据必须证明：

- acceptance、target／rollback version 和 profile digest 与第五步及生产对账一致；
- `production_selector_changed=true`，Active 为目标、Previous 为回滚版本，两个 release digest
  均有记录且没有折叠；
- inventory 精确覆盖 production Catalog／contract，收据自身另算 SHA-256 并独立封存。

production 源码树只允许在 candidate 只读副本中纳入 promotion inventory 的确定性输出；其
tree digest 差异必须由 acceptance SHA-256、promotion receipt 和 inventory 完整解释。清单外
代码、依赖、画像或构建输入发生变化时，按身份边界返回新 candidate 或 Campaign。

promotion inventory 是唯一变更白名单，只允许晋升工具声明的 production Catalog、contract 和
receipt 确定性输出。共享 `.go` 文件、依赖清单、测试、Makefile、门禁脚本、文本／Go AST 版本
泄漏 baseline 或 acceptance 输入均不得在 promotion 阶段修改。若目标 Active 必须修改 UA、
version Header、transport ID、探针或其他共享生产代码，说明 candidate 尚未完成：必须在 candidate
阶段实现版本中立派生，重新构建、采集、compare 和 accept，禁止把该修改夹带进 promotion。

在最终 production tree 上必须重新执行以下终态检查；candidate 阶段的结果不得复用：

```bash
make check-egress-spec
python3 tools/check_version_leak.py --self-test
python3 tools/check_version_leak.py
(cd backend && go test ./internal/service -run '^TestOfficialEgressVersionLeakAST')
```

其中 `make check-egress-spec` 是仓库总门禁，后两组显式命令用于在收据中分别固定文本和 Go AST
版本泄漏结果；除此之外还必须执行仓库完整回归与目标架构测试。全部通过后生成不可覆盖的
`post-promotion-gate-receipt.json`，至少绑定：candidate／production tree digest、acceptance 与
promotion receipt 摘要、promotion inventory 及允许路径、目标平台、每条命令与退出码、输出摘要、
测试计数、两套版本泄漏 baseline 摘要、构建输入摘要和完成时间。任一变更无法由 inventory 解释、
任一门禁失败或 receipt 与最终 tree digest 不一致时，禁止构建正式镜像。

正式镜像的源码、构建、image ID 和 registry manifest digest 必须同时绑定 candidate、acceptance、
promotion receipt 与 post-promotion gate receipt；缺少最后一项不构成正式制品。

### 4.6.3 独立 Active canary

使用正式镜像 digest 建立与生产隔离的 canary：独立账号、`CODEX_HOME`、数据库、Redis、配置、
网络和证据目录，不使用生产凭据或生产数据写路径。canary 必须按晋升后 Catalog 的默认
`active` mode 运行，禁止通过强制 `previous`／`active` 环境变量命中目标画像。

核对镜像架构、启动、health、HTTP／WebSocket／TLS、错误率和 Guard；activation fact 必须绑定
目标 version、profile／release digest 和正式镜像，强制 mode 计数为 0。真实业务须出现 §4.5
规定的完成事件；canary 失败不得进入生产，成功后保留证据再清理资源。

### 4.6.4 正式镜像切换与运行核验

部署前使用 `docker compose config` 或等价命令复核最终配置：应用服务必须精确绑定
`repository@sha256:<manifest-digest>`，除应用容器外的数据库、Redis、keeper、挂载和网络配置
保持不变。确认待部署 digest 与 production 构建证明一致后，标准应用更新为：

```bash
docker compose pull sub2api
docker compose up -d --no-deps sub2api
```

`pull` 只拉取 compose 固定的 digest，`--no-deps` 只重建 `sub2api`；禁止 `compose down` 和无范围
`prune`。部署后复核容器 digest、compose、health、日志、依赖服务、挂载和 activation fact，
确认 Active version、profile／release digest 与 promotion receipt 一致且无强制 override。
门禁、身份、安全、数据或恢复任一失败，或出现旧画像兜底、跨 Bundle fallback、连接池混用，
立即执行完整回滚，不得在故障实例上补画像或改 selector。

### 4.6.5 完整回滚演练与目标恢复

正式切换后仅替换应用容器，切回 §4.6.1 的旧镜像和 compose，复核 health、鉴权、数据与依赖、
挂载、代理／CA、入口和 final-wire；不得重建数据容器。随后恢复目标镜像，重复检查镜像、
Active Release、profile、activation fact、业务事件、完整性计数和 Guard。

只有 Previous、旧 Release、旧镜像和 compose 已完整绑定时，“切回 Previous”才是完整回滚的
简写；只改 mode 不能替代演练。回滚不得删除 Campaign、覆盖 Snapshot 或销毁证据。

### 4.6.6 激活收据与升级完成条件

生产激活收据必须绑定 Campaign／acceptance、promotion／inventory、post-promotion gate receipt、
production 源码与镜像，记录 canary、切换、旧版回滚、目标恢复四阶段的时间、compose、各类
digest、activation fact、完整性、业务事件和日志结论；敏感原文不得进入收据。

0.147 的静态 activation receipt 只作为历史事实；后继版本必须先补齐受管 Schema、生成器和
独立 finalizer，由其从四阶段原始事实生成并封存收据，才能通过本步。finalizer 必须重新计算
production tree、promotion inventory、post-promotion gate receipt 和镜像源码绑定；receipt 缺失、
门禁非零、存在非预期跳过、允许路径越界或任一摘要不一致时一律失败关闭。

本步退出条件：生产镜像、Active／Previous、profile 和 activation fact 与 promotion receipt
一致；四阶段全部通过，晋升、终态门禁、构建和激活收据均已封存，并且其源码与镜像摘要形成
同一条可复算链。Campaign 保持 `ready`，生产完成由独立收据证明；至此才可声明升级完成。

# 第五部分 非版本变更维护

本部分处理不属于 Codex CLI 六步升级流程的独立变更。Sub2API 上游更新和兼容代码退休必须分别
建立变更集，不得借用版本升级 Campaign 的证据掩盖自身行为变化。

## 5.1 Sub2API 上游更新

Sub2API 上游更新与 Codex CLI 换版必须拆成两个变更集。上游更新先固定 upstream commit、当前
发送面、冲突面、Release 和画像摘要，然后：

1. 重新生成 §3.5 的机器 overlay，审阅新增／删除／来源／范围差异，逐项复核高风险合并缝的
   版本权威方向、调用顺序、上下文和旁路；
2. 重新生成 wire 等生成代码，复算 source-to-sink 台账，处理新增或删除的 route；新增官方
   出站必须先登记 route、persona、SinkID 和 backend，禁止先放行裸 client；
3. 运行全量测试、静态门禁和当前 Active／Previous 的 final-wire 空允许列表对比，至少包含
   `tools/check_ledger_completeness.py` 与 `tools/check_version_leak.py`；
4. 上游变化若触及统一应用点、Client 生命周期、协议适配、官方 Sink、画像字段或受管取证
   工具，按第四部分的控制对象与身份边界新建 candidate 或 Campaign；纯业务变化不得借此改写画像；
5. 从最终同源树按目标平台构建候选镜像，重跑受影响入口、Kilo、compare／accept 和生产前
   门禁；影响范围无法可靠收窄时执行完整候选验收；
6. 删除路径时先删除调用，再凭 RemovalReceipt 删除 Catalog 项；提交前再次精确比较机器
   overlay，禁止手改计数或把 `discovered_latest` 接到 Active strict wire。

Fork 文件持有画像和 adapter；共享文件只保留候选身份基础设施、Snapshot、Plan、Bundle 与
Executor 的最小接入。自定义 UA 只能贡献客户端名和环境指纹，版本段由 Active 画像重建。

## 5.2 兼容代码退休

每次清理按以下顺序执行：

1. 用类型扫描和调用图证明候选兼容层的全部生产消费者；
2. 将真实消费者迁到当前接口，并为旧入口添加明确 fail-close；
3. Active／Previous、HTTP／WS／fallback 和辅助端点负例全部通过；
4. 删除旧类型、字段、构造接线、scanner 分类及只验证旧实现的测试；
5. 收紧源码绝迹门禁，禁止通过别名或 wrapper 恢复；
6. 使用空 wire 允许列表比较变更前后；
7. 写入机器退休收据，再进行实机部署。

不得删除仍承担平滑升级、回滚、非 Codex persona 或 API Key 产品语义的兼容层。旧入口在当前
Catalog 下只能失败时，应替换成清晰错误并删除不可达执行能力，避免未来重新激活 unsigned 路径。
