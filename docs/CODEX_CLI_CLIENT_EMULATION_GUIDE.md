# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
> **当前 active 基线**：`codex-cli 0.147.0`；previous 为 `codex-cli 0.145.0`
> **依赖基线**：[`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)
> **文档定位**：本文是 Codex CLI 客户端规则、Sub2API 仿真实现和版本演进的人类可读权威入口；
> 逐规则机器证据见 [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。
> **共享流程权威**：共同目标、运行架构、证据生命周期、变更分类、上游更新、发布与回滚规则以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准；本文只定义
> Codex CLI 的准入目标、官方事实、版本画像、实现、当前状态和公共流程的 Codex 专用增量

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

**遥测零流量边界。** Framework §1.2、§3.2 的公共规则适用于当前 active 0.147.0。官方源码中，
`config/src/types.rs:219-223` 的 `AnalyticsConfigToml.enabled=false` 经
`core/src/config/mod.rs:4261` 传入 analytics client，并由 `analytics/src/client.rs:220-226` 禁用事件队列。
OTEL 是独立配置：`otel.metrics_exporter=none` 才关闭默认 Statsig metrics；不能只写笼统的
`otel.exporter=none`，因为 `config/src/types.rs:583-592` 中 log／trace exporter 默认是 `None`，metrics
exporter 默认仍为 `Statsig`，`otel/src/provider.rs:137-159` 仅在 metrics exporter 非 `None` 时构建指标
管线。上述配置及源码摘要冻结后，候选“零遥测”不计为仿真差异，也不能生成 RequiredRule；未关闭或
实际触发的请求仍按正常出站规则验收。

## 1.2 客户端仿真链路

```text
官方源码、锁定依赖与真实 wire
→ 客户端规则画像
→ active 版本画像
→ 统一出站定型
→ 候选验收、生产启用与回滚
```

该链路是 Framework §1.3 统一链路在 Codex Persona 上的投影。入站兼容层只提交请求语义和可验证
条件；账号选定后绑定 active 版本画像，由 Codex 方言完成最终 wire 定型。第二部分定义“应产生什么
Codex 行为”，第三部分说明“Sub2API 如何实现 Codex 方言”，第四部分补充换版专用步骤，第五部分只
列出共享非版本维护流程中的 Codex 附加门禁。

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
`94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc`；Campaign
`codex-0_147_0-20260815T055433Z` 的 42 项规则和 15 项门禁全部通过，acceptance SHA-256 为
`07d0006b2819effa47ee3301c0210a73aecd2047712fe76f992128e33e1579e6`。Catalog 选择和当前生产
事实分别由
[`K83 catalog promotion receipt`](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_CATALOG_PROMOTION_RECEIPT.json)
与
[`K83 production activation receipt`](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_PRODUCTION_ACTIVATION_RECEIPT.json)
证明。各规则保留的早期 run ID 是未变化规则的原始证据，不代表 active 版本仍为 0.145。

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
  `wham-get-paths` 断言通过；机器事实见 0.147 Campaign 验收回执与证据归档。
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

# 第三部分 Codex 画像、方言与 Sub2API 实现

本部分承接退役叙事中的“第三部分 Sub2API 客户端仿真实现”，并按当前共享框架拆分为画像、方言、
执行链和门禁四类可验证职责。

## 3.1 共享架构落地映射与 Persona 边界

共享执行链、代码依赖和 Guard 合同以 Framework §2 为唯一权威；Release 身份与选择规则见 Framework
§3.1。本节只记录这些合同在 Codex Persona 中的实现投影：

| 共享层 | Codex 实现投影 | Codex 专属责任 |
|---|---|---|
| 入站准入与适配 | `OfficialRouteCatalog`、HTTP／WS 归一化 | 区分官方及已批准第三方入口，只提交协议、模型、工具、语义和受信条件 |
| Persona 规划 | `codex-cli` Persona、`CodexIdentityFacts`、`CodexEgressPlan` | 生成 Codex 身份、条件和端点计划，不继承入站 wire 身份 |
| Release 控制 | `ReleaseCatalog`、active／previous `ReleaseBundle` | 将 Framework 的 production active／rollback 投影到当前 Codex 兼容合同 |
| 方言编译 | Codex Compiler、`CompiledExecution` | 定型 Codex URL、Header、Body、顺序、压缩、状态和 transport |
| 执行与保护 | `CodexEgressExecutor`、HTTP／req-profile／WS adapter、Runtime Guard | 签发 Token、执行受信 wire 变换并阻止旁路 |

Key、Group、账号路由和计费沿用 Framework §1.3 的业务所有权；上述 Codex 组件均不得改写其归属。

| 平面 | 拥有 | 不得影响 |
|---|---|---|
| 版本发现与入口归一化 | GitHub `/releases/latest`／列表回退、6 小时节流与启动防抖、UA/version 配对和账号 UA 兼容、`openai_codex_client_version_synced`、管理端候选值、客户端名和环境指纹 | active ReleaseCatalog、画像摘要、最终 version 和 wire 契约 |
| 生产 strict wire | ReleaseCatalog、ReleaseBundle、Compiler、Executor 和受信 adapter 定型 URL、Header、Body、顺序、压缩、传输、状态与连接 | 被候选版本、管理员／账号 UA 或入站身份覆盖 |

当前 active strict wire 是 Codex CLI 0.147.0；自动同步只更新候选值，active ReleaseCatalog 只能经证据验收后显式发布。

| persona／状态 | 端点范围 | 逻辑出口 | 约束 |
|---|---|---|---|
| `codex-cli` | ReleaseBundle 登记的 Codex 端点闭集 | `CodexEgressExecutor` | URL、Header、Body、传输、状态与生命周期由同一 Bundle 驱动 |
| `chatgpt-web/chrome` | privacy settings、accounts check、subscriptions | 浏览器身份 Plan／client | 保持独立 Chrome TLS／HTTP2／XHR 语义，禁止套用 Codex 画像 |
| `transport_only` | authorization-code OAuth exchange | 独立受审 transport | 只复用有证据的传输事实，不冒充 refresh 的 Body／行为契约 |
| `unclassified` | PAT whoami、Agent Identity task register | 精确登记的遗留路径 | 完成官方行为举证前不得凭官方 host 自动归入 Codex persona |
| 未登记 | Catalog 未知 route 或 SinkBinding | 无长期出口 | enforce 状态下 fail-close |

Codex persona 由 OAuth 账号类型和 route registry 确定，ReleaseCatalog 再按受控 mode 解析 ReleaseBundle；
入站版本、平台、surface 或 UA 均不能选择版本画像。入口分类只用于协议／语义适配、观测和条件类型识别：
Compatible／Responses 先适配语义再进入统一定型点，官方 HTTP 入站可保留 HTTP fallback 事实，第三方入站按
active 画像取得默认传输。

| 入站事实 | 处理 |
|---|---|
| UA／version／originator、会话 UUID、`client_metadata` | 不拥有 wire 身份；按 release Build 和 active 命名空间重建 |
| Header、Body 或 `client_metadata` 身份冲突 | 丢弃冲突原值，派生同一生命周期的新身份 |
| 条件事实缺失或冲突 | 按条件不成立处理，不伪造 Header |
| 顶层字段超出闭集 | 删除并按“入口类型 + 字段集合”去重告警 |
| 已知值不合契约（如 `tool_choice != auto`） | 按画像规范化，不拒绝请求 |

账号配置非法、route／Sink 未登记或终态篡改时 fail-close；其余身份不匹配只投影并告警。生产 HTTP／WS
不执行入站身份逐字段一致性校验，该校验只用于离线夹具、画像诊断和证据复算。

**第三方 Agent 工具映射。** 本段只适用于已进入 Codex `canonical-semantic` 正向 SupportEnvelope 的
第三方入口。第三方 IDE／Agent 自带工具目录不能因为 OpenAI API 接受自定义工具，或 Codex CLI 支持
MCP，就原样进入官方 Persona wire。接入时必须冻结第三方产品、版本、入口、工具目录摘要，以及工具
名称、说明、Schema、顺序、条件和多轮工具调用／结果关系，并为每项工具选择以下且仅以下
一种处置：

| 工具处置 | 运行语义 |
|---|---|
| `official_builtin_lossless` | 与目标 Codex CLI 内置工具的请求、结果和错误语义无损等价；由受管双向映射转换，最终目录仍由 ReleaseBundle 生成 |
| `official_mcp_bridge` | 没有内置等价项；先让目标 Codex CLI 加载冻结 MCP 配置取得证据，再由画像生成官方实测的 MCP 名称、说明、Schema、顺序、deferred 条件及工具往返 |
| `denied` | 无法无损转换、缺少官方证据或第三方目录摘要未知；Planner／Compiler fail-close |

`official_mcp_bridge` 是双向协议映射，不是第三方工具透传：官方工具调用必须转换为第三方客户端可执行
的调用，执行结果再转换为官方实测的工具结果；ID、并行关系、流式参数、错误和历史必须闭合。每个
第三方目录必须独立进入 SupportEnvelope、RequiredRules／PAIR 和最终 wire 对拍；名称、说明、Schema、
顺序或条件变化即视为新目录，未重新批准前 fail-close。该路径只能主张“目标 Codex CLI + 冻结 MCP
配置”的等价性，不能冒充默认无 MCP 的官方客户端，也不是 Codex Persona 上线的前置条件。

## 3.2 Codex 0.147.0 active 画像与发布执行契约

active／previous 画像均以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排：

| mode | 版本与画像摘要 | 端点闭集 | 用途 |
|---|---|---|---|
| active | 0.147.0；`94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc` | 16 个静态端点（含 `wham_settings_user`）+ 1 个 ReturnedURL 动态端点 | 生产默认 |
| previous | 0.145.0；`e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b` | 15 个静态端点 + 1 个 ReturnedURL 动态端点 | 受控回滚和历史复算 |

启动期解码、结构校验或摘要核对失败即阻止启动；运行时只读不可变快照，需改写的数据按次深拷贝。

Release 的内容寻址和只写追加规则以 Framework §3.1 为准。Codex Catalog 将 production active／rollback
投影为 active／previous，并让每个 mode 指向完整 release ID；ReleaseBundle 另外冻结 Codex 身份、端点、
Header／Body、feature、传输、连接、策略和 fallback 图。

CodexEgressPlan 只保存业务事实、IdentityMode、深拷贝后的 Header Override、各类 Policy 和
attempt-owned Body；`TransportSpec` 只能来自端点画像。Header 所有权固定如下：

| Header 类别 | 所有者与优先级 |
|---|---|
| Transport、Auth、Host、长度和 Body framing | 系统最终所有，账号及入站不能覆盖 |
| Release identity | strict／mimic／proxy 模式下由画像最终决定 |
| Account extensions | 普通 API Key 按产品规则覆盖；mimic／proxy 仅允许非保护字段 |
| Endpoint closed set | OAuth 官方端点删除画像闭集之外的字段 |
| Ingress headers | 最低优先级，只进入明确允许的字段 |

Compiler 生成 `CompiledExecution`；Executor 据此签发 FinalizationToken、构造 `PreparedRequest` 并选择受信
adapter。adapter 只能执行 Token 声明的 wire 等价变换，其余终态修改拒发。可重放 Body 按内容摘要创建
新 attempt；single-use stream 不得预读、复制或多次尝试。

SnapshotDoc／ProfileSpec 保存版本事实，ExecutableProfile 校验可执行闭集。新增版本只能追加
快照、发布图节点和证据，不原位覆盖旧画像，也不在 §3.5.2 共享接入点散布版本分支；文本与
Go AST 版本泄漏门禁负责执行这一约束。

文本版本泄漏的历史债务 baseline 必须为空。确属 API Key persona、入站兼容下限或冻结历史
证据分类等非 OAuth 出站画像语义的引用，只能按“精确路径＋内容指纹＋次数＋中文理由”逐项批准；
新增、漂移或已经消失却未删除的例外均失败关闭。`--update-baseline` 只复核零债务状态，不能吸收
当前命中。

Go AST 门禁负责裸版本字面量和跨行注释的归属判断。其命中只允许冻结版本化 Snapshot 之外经
人工确认的产品语义；命中减少时必须同步收紧，新增指纹或次数上升一律失败。换版和 promotion
均不得用更新文本／Go AST baseline 换取门禁通过；共享执行代码中的目标版本事实必须迁入
版本化 Snapshot，任何未分类指纹都必须先修复或返回 candidate，不得带入 production tree。

## 3.3 最终出站定型

### 3.3.1 运行上下文

入口只保存协议／语义事实、传输状态、业务历史和可验证条件类型；账号选定后，Identity Authority
将其投影为 `CodexIdentityFacts`，并与 ReleaseBundle、端点、模型能力、Lite、turn-state 和条件 Header
绑定。surface、终端指纹、originator、版本及 suffix 默认值来自受信 ReleaseCatalog Build，而不是入站
客户端。上下文只属于当前 invocation、attempt 或 WS 连接。

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
每项必须同时有画像或执行点、官方与候选证据及机器断言；官方与第三方入口必须在同一候选制品和画像下验收。
多账号调度、计费和服务级请求节奏不在 42 项内，画像不改写它们。

## 3.5 源码改动台账

| 台账项 | 当前值 |
|---|---|
| upstream 基线 | `v0.1.177` peeled commit `073e92d17178a1ccdb0a27017f572f10c9c7ab62` |
| 完整 overlay | `docs/egress/maintenance/upstream-v0.1.177-egress-merge-ledger.json` |
| 机器范围 | `strict_surface ∪ required_review_touchpoint ∪ identity_boundary` |
| 人工范围 | §3.5.2 的 12 个高风险接缝 |

overlay JSON 是文件路径、`upstream`／`fork` 来源、范围标签、计数和联合摘要的唯一事实源。

更新 upstream 基线时执行：

~~~bash
python3 tools/check_ledger_completeness.py --write-upstream-merge-ledger
git diff -- docs/egress/maintenance/upstream-v0.1.177-egress-merge-ledger.json
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

## 3.6 Codex 包落点与上游合并缝

共享包依赖和 adapter 信任边界见 Framework §2.5；Codex 的精确源码落点与人工复核缝分别由 §3.5.1
和 §3.5.2 定义。Codex 版本变化只追加 Snapshot、Release 节点、证据与测试；只有现有 Codex 方言无法
表达且确属共享控制面缺口时，才进入 Framework §5.4。

## 3.7 Guard、逐 Sink 灰度与静态门禁

公共 Guard 校验项和状态机见 Framework §2.5。Codex 最终摘要额外绑定 `ForceQuery`，签发后增删裸
`?` 也是篡改；受信 adapter 的 `wss → https` 等价变换仍按规范 scheme 计算。

Codex 新 Sink 必须从 canary 进入；紧急 observe 限定 Sink 和期限，不扩大遗留基线。静态门禁覆盖
net/http、HTTPUpstream、req/v3、WS、facade 和 client factory，并用变异测试发现包装旁路；Catalog 项
只凭 MigrationReceipt／RemovalReceipt 单调迁移或删除。

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

> **多 Persona 迁移兼容说明**：本文保留的 `active／previous` 和
> `candidate_release_mode=previous` 是现有 Codex RuntimeCatalog、工具及历史收据的机器合同。生产
> Catalog 中 `active／previous` 分别对应新框架的 `production_active／production_rollback`；§4.3～§4.4
> 在隔离候选 Catalog 中借 `previous` 槽位承载目标 Release，只是 Codex 工具兼容实现，不会修改生产
> selector，也不属于新 Persona 的通用合同。新 Persona 必须使用独立 ValidationCandidate Release 引用。
> 迁移边界与现有代码处置见共享框架第五部分。

当前 21 个 `codex_profile` Runtime Sink、29 条 route 全部 `enforced`，无 Codex `legacy_observe`；29 条由
变更集 3 的 28 条历史 route 加 0.147.0 的 `wham_settings_user` 版本 route 构成。HTTP、WS、fallback、
models、images、files、alpha-search、WHAM 和 OAuth refresh 都进入统一 Executor。ReleaseCatalog 预编译
不可变 active／previous；attempt Body 单次解析并有序输出，重复键 fail-close，`server_response` query
只经受信通道提交。

官方与第三方 OpenAI 入口共用 HTTP／WS 归一化和身份派生；身份冲突不返回 502，逐字段校验只用于离线证据与诊断。
机器清单冻结 strict surface 和相对 `v0.1.177` 的完整 overlay，Markdown 只保留 12 个高风险接缝；active／previous final-wire
使用空允许列表，实机以已接受 Campaign 和部署报告为准。

当前兼容边界如下：

| 分类 | 当前决定 | 原因 |
|---|---|---|
| active／previous、per-sink canary／override、FinalizationToken | 保留 | 提供升级、回滚和防旁路能力 |
| browser persona、OAuth exchange transport-only、unclassified sink | 独立治理 | 不属于 Codex Executor persona，禁止混用画像 |
| service 画像 DTO／projection | 保留 | API Key mimic 和业务读取面仍有生产消费者 |
| unsigned `LegacyCompiledDispatcher` HTTP 执行路径 | 禁止执行 | 当前 Codex Runtime Catalog 全部 enforced，旧上下文进入通用发送入口时 fail-close |

兼容代码的完整删除条件和顺序见 Framework §5.5.2 和本手册 §5.1。

---

# 第四部分 Codex CLI 版本演进流程

Framework §5.3 是升级总操作入口并规定 `VC-0～VC-6` 顺序；本部分是 Codex 轨道的参数、证据和门禁
权威，只补充官方源码、锁定依赖、HTTP／WS／TLS 取证、Campaign 工具和 Active／Previous 发布细节。
后继版本可以复用工具和流程，但不得复用目标版本应独立取得的源码、wire 或运行证据；本部分的工具状态
不能重定义 Framework 的通用状态语义。

| 公共阶段 | 本文入口 | Codex 专用产出／工具状态 |
|---|---|---|
| `VC-0` 预检与基线 | §4.0 | DOC-PRE／P0、工具能力和 Active／Previous 基线 |
| `VC-1` 目标取证 | §4.1 | 官方源码、依赖、P／R／J／M 和 `official_sealed` |
| `VC-2～VC-3` 规则迁移与批准 | §4.2 | 分类及五份批准清单、`profile_approved` |
| `VC-3～VC-4` 画像与候选制品 | §4.3 | Snapshot、候选 Catalog、构建制品和 inventory |
| `VC-4～VC-5` 候选封存 | §4.4 | 双入口候选证据和 `candidate_sealed` |
| `VC-5` 比较与验收 | §4.5 | comparison、逐规则断言及 `ready` |
| `VC-6` 晋升、发布与回滚 | §4.6 | promotion、正式镜像、canary、激活、回滚和恢复收据 |

## 4.0 全流程控制约定

### 4.0.1 DOC-PRE 与 P0

正式 Campaign 前先完成可丢弃的 DOC-PRE／P0。DOC-PRE 只登记并审核本次 maintenance
transition；合并后必须从干净 HEAD 执行 P0。P0 只发现阻断，不形成目标版本证据：

0.149.1 的 DOC-PRE 使用独立的
[`候选规则画像`](CODEX_CLI_0_149_1_CANDIDATE_RULE_PROFILE.md)、
`candidate_rule_expectations_0_149_1.json`、`codex_upgrade_scenarios_0_147_0.json`、
`codex_upgrade_scenarios_0_149_1.json`、`spec_ref_anchors_0_149_1.json` 与
`spec_source_deps/manifest_0_149_1.json`。这些文件只提供目标版本和工具能力输入；当前 production
active／previous 仍为 0.147.0／0.145.0，不能据此推断 Catalog 晋升、生产激活或部署完成。

| 类别 | P0 通过条件 |
|---|---|
| 身份与角色 | 冻结官方二进制、源码／依赖／平台／feature／镜像和网络条件；执行副本、测试树与 finalizer 同源 |
| 账号与工具 | 场景所需账号、模型、额度、请求键、观测／安全／finalizer 工具和构建资源可用 |
| 环境恢复 | 端口、挂载、容器、hosts、代理、CA、数据库及托管字段可按 before／after 语义恢复 |
| 生产隔离 | 只读记录镜像、compose、选择器、Active／Previous 和依赖服务；P0 与正式 Campaign 隔离 |

P0 还必须执行以下机器预检；临时画像和合成证据只验证工具能力，不得升级为正式证据：

| 预检面 | 最低检查 |
|---|---|
| 当前基线 | 在干净 HEAD 执行 `make test-capture-tools`、`make check-egress-spec`，记录命令、源码摘要、退出码和测试通过／失败／跳过数量 |
| 目标版本坐标 | 用真实 baseline／target 坐标试运行 `plan` 加载；对空值、错误值和正确值做 mutation，禁止缺失坐标静默回退当前画像 |
| 双版本与画像生成 | 用临时批准资产验证 `prepare-profile`／`stage-profile`、Active 不变、Active／Previous endpoint 并集和版本新增 route 的 fail-close 门禁 |
| 候选工具链 | 验证 candidate core／aux、WS、relay、manifest、trace、finalizer、Schema 和逐规则断言能识别目标版本，禁止遗留版本硬编码 |
| 执行身份 | 逐字核对受管工具树与实际执行副本；确认候选源码、测试树、目标架构和镜像构建输入可形成同源摘要链 |

所有 P0 输出都必须带输入和工具摘要、原始错误、退出码及临时资产 inventory；无法证明通过的
项目登记为阻断，不得用临时副本的修改结果创建 Campaign。

P0 产物标为 `preflight-only`，不得发送真实请求、使用 `--acknowledge-live-requests`、创建正式
Campaign，或修改 Active／Previous、运行环境和历史证据。阻断修复应独立提交，随后重跑 P0；
只有干净、同源的受管树才能创建正式 Campaign。

工具以不可省略的机器坐标隔离两类目录。P0 必须在新的持久目录执行
`plan --campaign-mode preflight_only --campaign-purpose <validation_only|production_replacement>`；该目录
只允许 `plan/status`，`capture-official`、`classify`、画像暂存、candidate、compare、accept、`all` 和
`resume` 均失败关闭。P0 通过后，必须换一个尚不存在的目录执行
`plan --campaign-mode formal --campaign-purpose <同一用途>`。模式缺失、非法、摘要篡改或试图借
preflight 目录续跑，均不得自动回退为 formal。

### 4.0.2 共享身份边界在 Codex 工具中的投影

Campaign、candidate 与 attempt 的规范身份边界以 Framework §3.3、§5.1 为准。下表只说明现有 Codex
工具如何把这些边界投影为新建操作，不产生另一套定义：

| 单元 | 必须新建的变化 |
|---|---|
| 版本 Campaign | 目标版本、官方二进制／源码／依赖／平台／默认 feature，或批准规则、场景、画像和断言变化 |
| 同版本后继 Campaign | 受管工具影响证据含义、环境无法证明恢复，或已冻结的机器角色、执行副本和 finalizer 身份错误 |
| 同 Campaign 新 candidate | Sub2API 源码树、测试树、构建 ID、部署版本、OCI digest、image ID 或 profile ID／digest 变化 |
| 同 candidate 新 attempt | 冻结身份不变，仅因网络、配额或临时运行失败重试；新 attempt 不覆盖旧记录 |

当同版本 Campaign 的官方阶段与五份分类清单已经完整封存，但 candidate 的冻结运行时身份、
执行副本或环境恢复窗口错误时，使用 `successor` 建立后继 Campaign，不得重写旧 attempt，也不得
把旧 candidate 的 Kilo 收据改绑到新 Campaign：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py successor \
  --predecessor-campaign-dir /absolute/path/to/predecessor \
  --campaign-dir /absolute/path/to/new-campaign \
  --campaign-id <new-id> \
  --codex-account-id <当前可用账号-id> \
  --reason candidate_runtime_identity_correction \
  --predecessor-candidate-id <old-candidate-id> \
  --predecessor-attempt-id <old-attempt-id>
~~~

若逐规则断言证明旧批准画像或断言与已封存的官方原始字节冲突，而官方 attempt、
inventory、安全扫描和原始证据本身仍完整，则使用分类事实纠正后继：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py successor \
  --predecessor-campaign-dir /absolute/path/to/predecessor \
  --campaign-dir /absolute/path/to/new-campaign \
  --campaign-id <new-id> \
  --codex-account-id <当前可用账号-id> \
  --reason classification_fact_correction
~~~

该入口签发 v3 `predecessor-import.json`，只复制计划期 inputs／analysis 和规范化
official surface；旧分类结果只作为被纠正事实绑定摘要，不复制批准五件套。新 Campaign
回到 `official_sealed`，必须重新执行 `prepare-profile`、五件套审核和 `classify`。它不会
重新发送官方 CLI 请求；若原始官方证据缺失必要事实、身份不可信或 evidence 语义本身需要
改变，则本入口失败关闭，必须建立新的正式官方取证 Campaign。

重建新 Campaign 坐标时，工具只允许逐字继承的前序场景清单保留历史章节摘要；该豁免仅限
`classification_fact_correction` 的计划重建调用。新批准场景必须重新绑定当前章节摘要，普通
后继、Candidate 执行和分类批准路径均不得使用历史摘要豁免。

最后两项可同时省略；提供时必须成对绑定。Codex 账号属于 Candidate 的运行前提，不属于可承接的
官方／分类事实；每个后继 Campaign 必须通过 `--codex-account-id` 重新显式选择当前可用账号。
工具只允许这一项运行配置改变，并在 v2 `predecessor-import.json` 中冻结前序值、后继值和原因；
历史 v1 收据仍按“配置逐字不变”只读重放。该命令只逐字复制计划期 inputs／analysis、五份批准
清单和规范化 official surface，并生成 `predecessor-import.json`。原始官方 evidence、attempt、
inventory 与安全收据继续位于前序 Campaign，保持只读；后继的 `status`、`compare`、`accept`
每次都从前序路径重放 Campaign manifest、官方 stage seal、证据 inventory／security、批准五件套
及其联合摘要；多级后继必须递归回到最初官方 attempt 的原始绝对 Campaign 目录校验，禁止把上游
相对 attempt 路径重新解释到中间后继目录。任一路径、文件摘要、package digest 或原始证据漂移均失败关闭。后继 Campaign
普通运行时纠正后继只能新跑 candidate 与第三方客户端验证；分类事实纠正后继允许重新批准规则、
场景、画像和断言，但仍不得改变目标版本、官方身份或已封存官方证据语义。后面三项发生变化时
必须按版本 Campaign 重新执行相应阶段。

采集、探针、relay、脱敏、收据生成、环境快照和编排等产出侧工具变化会改变证据字节，必须
新建 Campaign。评估侧工具只有在显式白名单内才允许漂移，并须登记摘要、重放全部受影响门禁；
新增或未分类工具默认属于产出侧。被校验的工具树必须就是实际执行的工具树。

每个 candidate 建立时还必须声明用途，且用途不可在验收后追认：

| 用途 | 含义 | 验收后的强制路径 |
|---|---|---|
| `validation_only` | 仅用于诊断、比较或证明修复，不申请改变生产 | 停止于 `accepted_not_activated`，不得宣称生产完成 |
| `production_replacement` | 计划替换当前生产实现 | `accept` 通过后必须继续执行 §4.6，形成该 candidate 独立的生产激活收据 |

同一 Campaign 后续出现新的 `production_replacement` candidate 时，旧生产收据只证明历史事实，
不得继续代表当前生产；新 candidate 不能借旧 candidate 的 canary、镜像、回滚演练或激活收据。
如果尚未完成 §4.6，其状态必须明确报告为 `accepted_not_activated`。

`campaign_mode`、`campaign_purpose` 和 `candidate_purpose` 必须进入 Campaign、预约、attempt、seal
预览、阶段收据、comparison、AcceptanceFact、evidence seal 与外部门禁重放。candidate 的用途必须
等于 Campaign 用途；缺失、漂移或把 `validation_only` 改写成 `production_replacement` 均须失败关闭。

### 4.0.3 Codex 工具状态投影与专用不变量

下列状态是 Codex Campaign 工具对 Framework `VC-0～VC-6` 的内部投影，只用于恢复和重放本客户端
流程，不得与 Evidence、Approval、Validation、Runtime Selector 或 Deployment 正交事实合并。
Campaign 工具状态只按以下顺序前进：

~~~text
planned → official_sealed → profile_approved → candidate_sealed → compared → ready
~~~

`status` 只读推导状态；`resume` 只能为身份未变化的允许重试创建 attempt。`ready` 之后的
promotion、activation 和 rollback 不改变 Campaign 状态，由生产收据独立证明。

Campaign 状态与 candidate 的生产状态相互独立。生产状态按 candidate 单调记录：

~~~text
accepted_not_activated → canary_passed → active → rollback_verified → restored_active
~~~

不得以 candidate 编号最大、`accepted=true` 或 Campaign 已为 `ready` 推断生产状态。当前生产
candidate 必须由最新有效激活收据、运行容器 digest 和 activation fact 共同确定；三者不一致时
状态为 `production_unverified`，禁止宣称升级完成。

全流程共同遵守以下不变量，后文不再重复展开：

| 不变量 | 要求 |
|---|---|
| 权威来源 | `classification/approved/` 是五份批准清单的唯一事实源；SnapshotCatalog、ReleaseCatalog 和生产收据决定实际版本选择 |
| 不可变性 | 清单、attempt、result、seal、Snapshot 和历史收据只追加、不可覆盖；身份变化不得借旧证据跨阶段 |
| 同源性 | 被测试源码、候选源码、构建产物、运行镜像、profile 和 finalizer 必须由摘要形成同一条可复算链 |
| 失败关闭 | 路径、权限、摘要、恢复、安全、身份或规则覆盖无法证明时停止，不以人工推断补足 |
| 证据保留 | 证据位置、复算和保留遵守 §2.1.1、§2.1.3；敏感原文不进 Git，历史资产只追加 |

新画像必须完整追加并保证 Active／Previous 同时可执行；Codex CLI 换版、Sub2API 上游更新和
兼容代码退休分别实施。

### 4.0.4 工具就绪状态与前置阻断

本节区分“当前工具已经强制执行”与“规范要求但尚未受管实现”。正式 P0 必须先读取本表，
任何“未受管实现”项都属于创建后继版本 Campaign 的前置阻断。

| 能力 | 当前状态 | 边界 |
|---|---|---|
| Campaign 状态、官方／candidate seal、comparison、逐规则断言和 accept | 已实现 | `codex_upgrade.py` 和现有 Schema 可重放 Campaign 证据；candidate seal 内含 assertion gate |
| candidate 外部测试门禁收据 | 已实现 | `codex_upgrade_gate_receipt.py` 生成并独立重放 `candidate_external` 收据；`accept` 强制接收证据根和收据，且重新校验 candidate／package／源码树／镜像身份 |
| Catalog promotion 与 promotion receipt | 已实现 | `egresscatalogpromote` 只生成确定性 Catalog／contract／receipt，不部署服务 |
| post-promotion gate receipt | 已实现 | 同一工具生成并独立重放 `post_promotion` 收据，绑定 acceptance、promotion、production tree 和目标架构；六项固定门禁均须零失败、零跳过 |
| production activation receipt | 已实现 | `production_activation_receipt.py` v2 强制消费 promotion、post-promotion gate、acceptance、production tree 和四阶段原始事实，生成不可覆盖收据并独立重放；历史 v1／K80 收据只证明当时事实 |
| 第三方客户端绑定 | 当前固定为 Kilo 双入口 | 工具和 Schema 明确要求 `kilo-compatible`、`kilo-responses`，文档不得单独泛化 |

后继版本创建正式 Campaign 前，必须对两阶段门禁生成并重放受管收据；缺少收据、摘要漂移、失败、
跳过、命令集合变化或身份不一致均使 P0／对应阶段失败关闭。Campaign 建立后再修改这些工具会触发
§4.0.2 的工具漂移边界。人工“已经运行”结论、终端截图或未绑定原始事实的静态 JSON 不能替代受管
收据。若未来要把 Kilo 泛化为可配置第三方客户端集合，也应先修改工具、Schema 和验收测试，再调整
本流程。

## 4.1 官方目标版本取证

本步从目标官方源码和真实抓包整理第二部分完整编号规则；开始前必须通过 §4.0 的 DOC-PRE／P0。

### 4.1.1 输入与执行

统一编排入口为：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py --help
~~~

| 输入 | 内容 |
|---|---|
| 基线 | 当前第二部分规则、官方源码／证据和 `--scenario-manifest` 场景清单 |
| 目标 | Codex CLI 版本、官方源码、Cargo.lock／依赖、二进制、SHA-256 及 `--target-scenario-manifest` 正式采集场景清单 |
| 条件 | 平台、运行镜像、默认 feature、模型、账号、代理和 TLS 条件 |
| 坐标 | 持久 Campaign 目录、采集机、证据目录和环境恢复坐标 |

Campaign 目录必须是持久、绝对、尚不存在且不经过符号链接的路径，不得位于临时目录。普通、
Lite 等互斥条件使用独立 track、job、evidence root 和 receipt；只有两侧模型及其他取证条件
相同时，差异才能归因于版本。

`plan` 必须同时冻结 baseline 与 target 两份场景清单：baseline 清单只用于升级前规则和差异分析，
运行目标官方 CLI 的 `capture-official` 只从 target 清单生成 Job。批准 `scenarios.json` 时允许调整
规则归属、coverage 和人工说明，但 official 的命令、环境、证据根、必需收据及模型轨道必须与
Formal Campaign 冻结的 target 执行契约逐摘要一致；不一致时必须新建 Campaign，不得借 baseline
命令模板执行目标版本。

| 顺序 | 命令 | 机器产物 |
|---:|---|---|
| 1 | `codex_upgrade.py plan --campaign-mode formal --campaign-purpose <用途>` | 在 P0 目录之外冻结正式输入，生成 `target-source.json`、`source-diff.json` 和 `baseline-surface.json` |
| 2 | `capture-official run` | 按场景采集 HTTP、WS、TLS、端点、状态和错误分支证据 |
| 3 | `capture-official seal` | 校验恢复、权限、秘密扫描、inventory 和 finalizer，进入 `official_sealed` |
| 4 | `classify`（不传批准清单） | 生成官方差异和 `classification/draft/<revision>/` 五份草案 |

### 4.1.2 规则整理

人工逐项复核源码 diff、官方 wire 差异、目标源码和原始抓包：

1. 从生产入口追到认证、Client、TLS、传输、Header、Body、端点和跨请求状态；
2. 判断范围、触发条件、固定／随机／条件属性及可观测边界；
3. 把成立的目标行为写入第二部分，保持规则字段完整；
4. 让 `target-rules.json` 与第二部分规则编号一一对应，所有证据可重新定位和解析。

工具只扫描、抓包、解析、差分并生成机器草案，不自动编写第二部分规则正文；本步的
`classify` 也不批准清单。

### 4.1.3 退出条件

目标规则、逐规则官方源码／依赖／P／R／J／M 证据和机器草案必须齐备；所有发现均已进入规则
或标为待处理的 `blocked`，官方场景、恢复、安全、inventory 和 seal 全部通过，Campaign 达到
`official_sealed`。

## 4.2 规则比较、画像准备与批准

本步把目标规则与当前基线逐项比较，完成分类、完整画像准备和五份清单批准。

### 4.2.1 规则分类

| 分类 | 含义 |
|---|---|
| `inherit` | 新旧规则编号和行为保持不变 |
| `change` | 规则仍存在，但可见行为变化 |
| `add` | 目标版本新增规则或出站面 |
| `delete` | 基线规则在目标版本中已不可达 |
| `condition_change` | 行为仍存在，但触发条件变化 |
| `blocked` | 证据不足，暂时不能得出结论 |

先确认触发条件、证据面、平台和会话状态可比，再判断行为变化。`delete` 必须同时具备目标源码
不可达结论、覆盖触发条件的正反场景、旧规则引用清单和 RemovalReceipt，否则保持 `blocked`。

`source` discovery 只有源码树和指纹完全一致时才可继承；`dynamic` discovery 绑定本轮真实
证据，必须重新分类。摘要相同不能替代源码、wire、场景覆盖和跨清单完整性证明。

### 4.2.2 画像与五份清单

在 `official_sealed` 状态执行 `prepare-profile`，把官方取证形成的完整 Snapshot 规范化为
Campaign 外的待审核 `profile.json`。随后审核：

| 清单 | 审核内容 |
|---|---|
| `target-rules.json` | 目标规则全集，与第二部分一致 |
| `rule-migration.json` | 新旧规则迁移、discovery 分类和证据引用 |
| `scenarios.json` | 官方／candidate 场景、规则覆盖和目标画像绑定 |
| `profile.json` | 完整目标 Snapshot、profile ID／digest，状态为 `approved` |
| `assertion-profile.json` | 逐规则断言、场景选择和第二部分摘要绑定 |

规则、场景、画像、断言和端点集合必须跨清单一致；所有 discovery 具有唯一分类和证据引用，
`rule-migration.json` 必须为 `approved`。

### 4.2.3 批准与退出条件

1. 使用五个 `--*-manifest` 参数执行 `classify`，暂不传
   `--approve-manifest-sha256`；工具校验后返回 `joint_manifest_sha256`。
2. 人工复核五份清单的文件摘要和联合摘要，不得手写或替换机器摘要。
3. 使用完全相同的清单和 `--approve-manifest-sha256` 再次执行 `classify`；工具只写一次地
   保存到 `classification/approved/`。

退出条件：规则和 discovery 无未分类项、`blocked=0`、联合摘要获批准，Campaign 进入
`profile_approved`。

## 4.3 候选画像入库与制品构建

本步把批准画像编译为不切换 Active 的候选 RuntimeCatalog，再纳入同源 candidate 树并构建制品。

### 4.3.1 画像暂存

~~~bash
python3 tools/official_client_capture/codex_upgrade.py stage-profile \
  --campaign-dir /绝对路径/campaign \
  --output /绝对路径/新的候选-runtime-catalog
~~~

`--output` 必须位于 Campaign 外、为尚不存在的绝对路径。工具从五份批准清单生成候选
RuntimeCatalog 和 `catalog-stage-receipt.json`，不修改仓库或生产 selector。收据必须证明：

- Campaign、classification、target version 和 profile digest 与批准事实一致；
- `active_unchanged=true`、`production_selector_changed=false`、
  `candidate_release_mode=previous`；
- inventory 精确覆盖输出目录，逐文件摘要和大小可复算。

### 4.3.2 入库与实现边界

将暂存的 Snapshot、Release graph 和 Catalog 清单经审核纳入 candidate 源码树，并满足：

1. 目标 Snapshot／Release 是新增节点，旧节点的路径、内容和摘要不变；
2. 批准的 endpoint、Header、Body、TLS、连接、状态和路由规则均可由画像表达；
3. 新端点在相关 mode 同时具备 binding、Bundle resolver、route catalog 和 release proof；既有
   route 继续受 MigrationReceipt 约束，版本新增 route 必须追加 version-route receipt，并绑定
   wire fixture、execution verification 和 canary acceptance；
4. 生产 Active 不变，目标 Release 仅作为 `previous` 候选供第四步显式选择；
5. 在途 invocation 保持原 Bundle，新 invocation 才解析新 selector，fallback 和连接池不得跨 Bundle。

版本新增 route 可在确实不含该端点的单个 Release 中零匹配，但 Compiler 端点集合必须等于
Active／Previous 并集，且每条 runtime-bindable route 在并集中至少有一个 binding。只有现有
Snapshot、Plan、Bundle 或 Executor 无法表达新机制时，才最小修改共享层并专项复验两个 mode。

### 4.3.3 构建与退出条件

从完成入库和测试的同一最终源码树准备前端产物、运行资源、目标平台二进制和镜像，记录
Git／tree／build／部署版本、二进制 SHA-256、架构、构建参数、image ID、OCI digest 和
profile ID／digest。证据机和低资源生产机不承担 Go／Node 编译。

退出条件：stage receipt 可复算，目标画像和制品同源，旧 Snapshot／Release 仍可执行，实现侧
测试通过，生产 Active 未改变，第四步所需 candidate 身份字段齐备。

## 4.4 候选验证与封存

本步用第三步的固定制品运行目标画像，并从批准场景和真实第三方入口收集候选证据。候选按
`candidate_release_mode=previous` 显式选择目标 Release，不得借当前 Active 或客户端自报版本
选择画像。操作约束和故障预防统一由本节规定。

### 4.4.1 Candidate 身份冻结

~~~bash
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
  --candidate-purpose <validation_only|production_replacement> \
  --acknowledge-live-requests
~~~

工具在真实请求前复算源码、运行镜像和画像，原子创建 attempt，冻结源码、构建、部署、
image／OCI 和 profile 身份，并生成 `attempt_id` 与 `run_nonce`。attempt、activation fact、
镜像构建证明和实测源码摘要必须指向同一源码树。

Campaign 与 candidate ID 还会和场景后缀、主体及 16 字符 UTC 窗口拼成 direct／mitm
运行坐标，最终值不得超过 128 字符。编排器必须在创建 reservation 前复算完整坐标并失败关闭；
不得等脚本启动后才留下必败 attempt。若某 candidate 已形成恢复完整的失败 attempt，同一
candidate 仍只能显式 `resume --rerun-failed`；身份或坐标需要变化时必须换新 candidate ID，
旧 candidate 只读保留且不能把整个 Campaign 永久锁死。

### 4.4.2 场景与第三方入口

`scenarios.json` 是任务、规则覆盖和必需客户端的事实源。每条规则必须有真实触发场景；每个
入口只需证明适用规则及向同一 candidate Release 收敛：

| 入口 | 必须证明 |
|---|---|
| 官方 Codex CLI／Desktop | 入站版本、平台、surface 或身份不影响 `previous` 候选 Release，身份冲突不返回 502 |
| `/v1/chat/completions` | Compatible 适配后进入目标 HTTP Responses 画像 |
| `/v1/responses` HTTP | Responses 入口使用同一目标 HTTP 画像 |
| `/v1/responses` WebSocket | WS、预热和 fallback 使用同一 Bundle，不跨版本回落 |
| Kilo Compatible | `kilo-compatible` 收据绑定模型、账号、请求、响应、usage、candidate 和 profile |
| Kilo Responses | `kilo-responses` 收据绑定相同 candidate 身份和 profile |

必需任务必须全部完成。可选任务只有在批准清单预先标为 optional、独有规则覆盖为零、替代证据
完整且缺口已封存时才可不阻断。

场景真实性门禁 `SCN-REALITY-01` 明确区分“job 退出成功”和“目标协议分支真实成立”。A11
realtime sideband、A13 OAuth refresh、A14 Files 三跳等高风险场景只有在原始 CLI／relay／pcap
或驱动事件能证明触发、关键中间事实和最终状态时才生成成功收据；编排器不得根据退出码补写。
收据还必须绑定 track、model、Lite 条件、evidence root、Campaign、attempt 和 `run_nonce`。

`run` 完成后、首次 `seal` 前，必须完成两条真实 Kilo 请求；其 ingress、runtime、response 和
usage 必须绑定本次 Campaign／attempt／`run_nonce`，并位于 attempt 开始与 client checkpoint
之间。两条入口统一使用 Campaign 已冻结的 `lite_model`；主轨 `model` 只用于官方／候选场景任务，
不得被 seal 隐式复用于 Kilo。历史 Campaign 未记录 `lite_model` 时只读重放才允许回退主轨模型。
两条请求之后不得再发送本 attempt 的客户端验证请求。

### 4.4.3 四阶段封存

1. **建立检查点**：两条 Kilo 请求完成后，首次执行 `capture-candidate seal`，采集
   `client-after` 并返回 `client_checkpoint_created`。
2. **生成收据**：在 candidate 源码树外运行受管生成器，形成 capture manifest、Go test trace、
   observed-profile 和两份 Kilo 收据。`build_*` 产物只是 finalizer 输入，不能直接提交给 seal；
   正式收据必须由受管 finalizer 生成。activation fact 必须由运行服务产生；测试 trace 必须来自
   同源树上的冻结测试日志，生成器不得合成二者。
3. **生成预览**：

   ~~~bash
   python3 tools/official_client_capture/codex_upgrade.py capture-candidate seal \
     --campaign-dir /绝对路径/campaign \
     --candidate-id <candidate-id> \
     --candidate-purpose <与 run 完全相同的用途> \
     --attempt-id <attempt-id> \
     --capture-manifest /绝对路径/capture-manifest.json \
     --assertion-evidence-root /绝对路径/attempt-evidence \
     --observed-profile-receipt /绝对路径/observed-profile-receipt.json \
     --client-evidence kilo-compatible=/绝对路径/kilo-compatible-receipt.json \
     --client-evidence kilo-responses=/绝对路径/kilo-responses-receipt.json
   ~~~

   工具重验身份、任务、时间窗、恢复、安全、inventory 和机器断言，返回 `review_sha256`。
4. **批准封存**：人工复核预览，以相同参数追加
   `--approve-seal-sha256 <review_sha256>`；Campaign 进入 `candidate_sealed`。

四路输入的路径和来源固定如下：

| 输入 | 路径与来源约束 |
|---|---|
| capture manifest／assertion bundle | 位于 attempt evidence root 内，并由 provenance 完整覆盖 |
| Go test trace | 以 bundle 相对路径登记；内部状态记录只能来自同源候选树的冻结测试日志 |
| 画像、断言和事实映射 | 位于 candidate 源码树内并与批准清单逐字绑定 |
| observed-profile／Kilo 收据 | 位于 attempt evidence root，绑定 Campaign／candidate／attempt／`run_nonce` 和镜像身份 |

证据标签只能从采集参数和场景 precondition 推出，不能根据待通过的 selector 或断言结果反推；
侧别豁免只允许结构上没有产出路径的 check，采集遗漏必须重采。

### 4.4.4 运行纪律与退出条件

- 开跑前机器预检必须覆盖不可变镜像 RepoDigest、挂载与 PID namespace、实际执行工具副本、冻结
  Codex CLI、构建 tag、模型与账号能力、Live／WS／compact 开关、管理凭据、activation 身份、
  账号熔断与配额、采集端口、run-root 标记及属主／权限；任一缺失在真实请求前失败关闭。
- `candidate-frozen-aux` 还必须在修改环境前确认隔离分组只含目标账号，并已启用 Live 与图片生成；
  `--live-attestation-compose-files` 中每个 compose 文件都按 `-f` 参数解释，允许兼容历史首个裸绝对
  路径，但拒绝相对路径、符号链接、其他 compose 选项和 shell `eval`。只读前检失败不得执行恢复
  钩子或伪造 `restoration_failed`，首个真实修改前才允许武装恢复。
- Campaign job 的有效参数以冻结 job definition 和 attempt `argv` 为准；脚本默认值或外部同名
  环境变量被 job 覆盖时不得据其推断实际执行条件。
- 固定镜像 digest，只替换应用容器并保留回滚点；运行期间不执行 `pull`、`compose down` 或
  `prune`，不重建数据和依赖服务。
- run 与 seal 之间不得修改 candidate 源码树；身份变化新建 Campaign／candidate，只有身份
  未变的临时失败才允许新 attempt 或 `resume --rerun-failed`。
- evidence root 外的 `runs/` 也纳入校验；目录权限至多 `0700`、文件至多 `0600`。
- 确认必败时执行受管停止和环境恢复，不得强杀并丢失 after 探针。

退出条件：必需任务和 Kilo 双入口均通过，运行画像、结构化测试、恢复、secret scan、inventory、
机器断言和 evidence seal 完整，`review_sha256` 已批准，Campaign 达到 `candidate_sealed`。

## 4.5 比较与验收

本步离线比较两侧封存证据，再执行逐规则机器断言，最后由 `accept` 重放 Campaign 门禁。

### 4.5.1 离线比较

~~~bash
python3 tools/official_client_capture/codex_upgrade.py compare \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id>
~~~

比较机必须能从封存绝对路径复算两侧证据；跨机器时先把完整 evidence roots 同步到原绝对路径
并执行 `status`。官方、candidate 收据和当前重放器的 finalizer `producer.tool.path` 必须逐字
一致，因此两台机器应使用同名、同绝对路径的 finalizer；内容摘要相同不能替代路径一致性。
工具只读重验身份、inventory、恢复、任务、规则覆盖和 profile 绑定，生成
`comparisons/<candidate-id>/result.json` 与 `results.template.json`，Campaign 进入
`compared`。

`equal` 只表示两侧完整 surface 集合相同，不是验收结论；采集计划不同可导致 `equal=false`。
只要 comparison 为 `complete`、`offline_only=true`，coverage 和 profile binding 完整，就可
进入 `compared`；行为一致性由逐规则断言决定。

### 4.5.2 逐规则机器断言

~~~bash
python3 tools/official_client_capture/build_rule_assertion_results.py \
  --config /绝对路径/assertion-config.json \
  --output /绝对路径/campaign/assertions/<candidate-id>/results.json \
  --results-dir /绝对路径/campaign/assertions/<candidate-id>/machine
~~~

配置必须绑定五份批准清单、目标版本和 profile digest，以及官方、candidate、comparison 的
package digest、capture manifest、证据根和逻辑路径前缀。

| validation mode | 机器判定 |
|---|---|
| `dual_wire` | 在官方和候选封存证据上执行同一规则的侧别检查，两侧均须通过 |
| `candidate_profile` | 在候选证据上验证 Sub2API 内部实现，并绑定批准的官方权威摘要 |

`results.json` 必须唯一覆盖目标规则全集；每条规则均为 `status=pass`、
`evidence_level=full`，不允许 fail、N／A、手写通过或未绑定 inventory 的证据路径。

### 4.5.3 accept 前置与正式验收

在同一 candidate 源码树执行 `make check-egress-spec`、`make test` 和目标平台测试，把命令、工作目录、
主机、架构、时间、退出码、通过／失败／跳过计数及输出证据写入权限为 `0600` 的
`candidate-gates.facts.json`。证据根必须是绝对路径、非符号链接且权限为 `0700`。随后生成并独立重放
`candidate_external` 收据：

~~~bash
python3 tools/official_client_capture/codex_upgrade_gate_receipt.py finalize \
  --evidence-root /绝对路径/candidate-gates \
  --facts candidate-gates.facts.json \
  --output candidate-gates.receipt.json

python3 tools/official_client_capture/codex_upgrade_gate_receipt.py replay \
  --evidence-root /绝对路径/candidate-gates \
  --receipt candidate-gates.receipt.json
~~~

finalizer 固定检查三项门禁均为退出码 0、失败 0、跳过 0，并绑定 formal 模式、Campaign／candidate
用途、目标版本／架构、Profile、candidate package、源码树和镜像。缺项、替换命令、用途漂移、证据摘要漂移或身份不一致时不得执行
`accept`。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py accept \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --assertions /绝对路径/campaign/assertions/<candidate-id>/results.json \
  --external-gate-root /绝对路径/candidate-gates \
  --external-gate-receipt candidate-gates.receipt.json
~~~

`accept` 重算逐规则断言并检查四组 Campaign 门禁：

| 门禁组 | 判定内容 |
|---|---|
| 套件与身份 | full suite、官方二进制身份、candidate 完整身份、运行 profile，以及可独立重放的 candidate 外部门禁收据 |
| 比较与规则 | comparison 完成、双侧规则覆盖、逐规则断言完整、分类无阻断 |
| 恢复与安全 | 两侧环境恢复、secret scan 和 evidence inventory 摘要 |
| 第三方入口 | 必需客户端收据齐全，重新解析后与封存绑定一致 |

`accept` 会重新独立重放外部门禁，并把收据、candidate identity、candidate package digest 和 evidence
seal 绑定为同一 AcceptanceFact。全部通过后，工具只写一次地保存断言、`accepted=true`、
`failed_gates=[]`、`production_state=accepted_not_activated` 和 evidence seal，Campaign 进入 `ready`；
收据事后漂移会使 `status` 重新退回非 ready。
失败 attempt 不可覆盖，Campaign 保持 `compared`。

### 4.5.4 ready 边界

`ready` 只表示固定 candidate 已通过目标规则和 Campaign 证据验收。模型收据、HTTP 200 或
`equal=true` 都不能替代逐规则断言；`ready` 也不表示已经晋升 Catalog、构建正式镜像、切换
生产或完成回滚演练。

`validation_only` candidate 到此结束并标记为 `accepted_not_activated`。`production_replacement`
candidate 不得在此结束；必须使用本次 acceptance 和已验收 candidate 源码继续执行 §4.6。
对后者，`status --candidate-id` 必须继续返回 `production_status=accepted_not_activated` 并明确提示
§4.6；在 promotion、canary、activation 和 rollback 收据完成前不得宣称升级完成。
候选验证镜像以 `previous` 运行，只证明候选规则，不能直接作为默认 `active` 的生产镜像。后继
candidate 一旦准备替换生产，旧 candidate 的生产收据不得复用。

## 4.6 生产启用与回滚

本节只补充 Framework §5.6 在 Codex 轨道中的 Catalog 晋升、终态门禁、正式镜像、canary 和生产
激活收据。所有输入和输出必须绑定同一个 candidate ID 和 acceptance SHA；候选验证源码／镜像与
晋升后的生产源码／镜像是两组不同身份，必须由 promotion receipt、差异清单和终态门禁连接，禁止
把 candidate 镜像摘要冒充 production 镜像摘要。

### 4.6.1 生产对账与回滚点

写操作前只读记录容器 digest、compose／override、selector、activation fact、Active／Previous、
数据与依赖服务、网络、挂载和代理／CA。名义 Catalog、强制 mode 与实际流量不一致时立即停止。

冻结上一已接受版本的 Release／profile、镜像 digest、compose 和必要配置，并在只读数据克隆或
等价隔离环境证明旧镜像可启动、读取数据并通过 health／鉴权。依赖可变标签、临时环境变量或
未经验证的 Previous 不能作为回滚点。

### 4.6.2 Catalog 晋升与正式制品

在第五步接受的 candidate 副本上确认 Catalog 为“Active＝回滚版本、Previous＝目标版本”，
然后在该源码的 `backend/` 目录执行：

~~~bash
go run ./cmd/egresscatalogpromote \
  -campaign-id <campaign-id> \
  -acceptance-sha256 <acceptance-result-sha256> \
  -target-version <target-version> \
  -target-profile-digest <target-profile-digest> \
  -rollback-version <rollback-version> \
  -rollback-profile-digest <rollback-profile-digest> \
  -output /绝对路径/新的-production-catalog
~~~

工具只离线交换已验收 Release mode，生成 production RuntimeCatalog、release contract graph
和 `catalog-promotion-receipt.json`。输出路径必须绝对、尚不存在且不经过符号链接。收据必须
绑定 acceptance、目标／回滚版本与 profile digest、两个 release digest、selector 变化和完整
inventory。

production tree 只允许三类变化：promotion inventory 声明的 Catalog／contract；candidate
冻结 Git 基线中已存在且摘要一致的通用 promotion 命令与实现；为 Active／Previous 互换而作的
生产模式测试期望和确定性 transition。不得修改业务运行时代码、依赖、Makefile、门禁脚本、
版本泄漏 baseline 或 acceptance 输入。必须生成 candidate→production 逐文件差异清单，清单外
变化或运行时代码变化必须返回 candidate／Campaign 重新验收，不能夹带进 promotion。

文本门禁的 `files` 历史债务必须为 `{}`；`approved_non_leak_references` 只容纳带理由的精确
非泄漏语义。promotion 阶段禁止运行任何会写回版本泄漏基线的命令；若 AST 命中已经减少，应先
作为独立维护变更收紧基线并重新形成 candidate，不能在 production tree 中顺手更新。

最终 production tree 必须重新执行，不能复用 candidate 结果：

~~~bash
make check-egress-spec
python3 tools/check_version_leak.py --self-test
python3 tools/check_version_leak.py
(cd backend && go test ./internal/service -run '^TestOfficialEgressVersionLeakAST')
~~~

还必须执行完整回归与目标架构测试。正式镜像需绑定 candidate／production tree digest、
acceptance、promotion receipt、inventory、门禁结果、构建输入、image ID 和 registry manifest
digest；构建不得携带 `candidatecapture` 等候选取证专用标签。任一摘要不一致时禁止构建或部署。

六项结果必须写入 `post-promotion-gates.facts.json`，并在 canary 前生成、重放
`post_promotion` 收据：

~~~bash
python3 tools/official_client_capture/codex_upgrade_gate_receipt.py finalize \
  --evidence-root /绝对路径/post-promotion-gates \
  --facts post-promotion-gates.facts.json \
  --output post-promotion-gates.receipt.json

python3 tools/official_client_capture/codex_upgrade_gate_receipt.py replay \
  --evidence-root /绝对路径/post-promotion-gates \
  --receipt post-promotion-gates.receipt.json
~~~

该阶段收据额外绑定 AcceptanceFact、promotion receipt 和 production tree；candidate 身份、目标架构、
Profile、package、源码树和镜像必须与验收阶段完全一致。任一门禁失败或跳过、两份输入摘要漂移、
production tree 不一致，均禁止构建正式镜像或开始 canary。

### 4.6.3 独立 Active canary

使用正式镜像 digest 建立与生产隔离的 canary，独立使用账号、`CODEX_HOME`、数据库、Redis、
配置、网络和证据目录。canary 必须按晋升后 Catalog 的默认 `active` 运行，禁止以强制 mode
命中目标画像，也禁止直接复用 activation fact 显示 `profile_mode=previous` 的候选验证镜像。

核对镜像架构、启动、health、HTTP／WebSocket／TLS、错误率、Guard 和 activation fact；事实
必须绑定目标 version、profile／release digest 和正式镜像，强制 mode 计数为 0。真实业务须
出现 §4.5 规定的完成事件；失败不得进入生产。

### 4.6.4 正式切换

部署前复核 `docker compose config` 或等价结果：应用服务必须绑定
`repository@sha256:<manifest-digest>`，数据库、Redis、keeper、挂载和网络保持不变。标准更新为：

~~~bash
docker compose -f docker-compose.yml -f /绝对路径/production-image.override.yml \
  up -d --no-deps sub2api
~~~

远端 registry 尚未缓存固定 digest 时才先执行定向 `pull`；本机 registry 已有精确 digest 时不得
为形式完整重复拉取。两种情况都必须在切换前后复核实际 image ID／RepoDigest。

禁止 `compose down` 和无范围 `prune`。部署后复核容器 digest、compose、health、日志、依赖、
挂载和 activation fact，确认 Active version、profile／release digest 与 promotion receipt
一致且没有强制 override。发现身份、安全、数据、恢复、旧画像兜底、跨 Bundle fallback 或
连接池混用时立即完整回滚，不在故障实例上补画像或改 selector。

### 4.6.5 回滚演练与目标恢复

正式切换后仅替换应用容器，切回 §4.6.1 冻结的旧镜像和 compose，复核 health、鉴权、数据、
依赖、挂载、代理／CA、入口和 final-wire；不得重建数据容器。随后恢复目标镜像，重复检查镜像、
Active Release、profile、activation fact、业务事件、完整性计数和 Guard。

只有 Previous、旧 Release、旧镜像和 compose 已完整绑定时，“切回 Previous”才是完整回滚的
简写；只改 mode 不能替代演练。回滚不得删除 Campaign、覆盖 Snapshot 或销毁证据。

### 4.6.6 激活证据与退出条件

生产激活证据必须绑定 Campaign／acceptance、promotion／inventory、production tree、终态门禁、
正式镜像，以及 canary、正式切换、旧版回滚和目标恢复四阶段的时间、compose、各类 digest、
activation fact、完整性、业务事件和日志结论。v2 收据强制接收 acceptance、promotion receipt 和
`post_promotion` gate receipt 的文件绑定，校验目标架构与 production tree，并要求终态门禁完成时间
早于 canary；缺少任一输入时不得用历史 v1 收据替代。

四阶段事实必须写入权限为 `0700` 的独立 evidence root，文件权限为 `0600`，再由受管工具生成
并重放不可覆盖收据：

~~~bash
python3 tools/official_client_capture/production_activation_receipt.py finalize \
  --evidence-root /绝对路径/activation-evidence \
  --facts facts.json \
  --output receipt.json

python3 tools/official_client_capture/production_activation_receipt.py replay \
  --evidence-root /绝对路径/activation-evidence \
  --receipt receipt.json
~~~

生产激活退出条件：运行容器与 production 镜像一致；candidate ID／acceptance 通过 promotion receipt、
差异清单和终态门禁连接到 production tree；Active／Previous、profile 和 activation fact 与
promotion receipt 一致。`receipt.json` 必须以
`codex-production-activation-receipt/v2` 独立重放成功。四阶段全部通过，晋升、终态门禁、构建和激活证据形成同一条可复算
链，且激活收据可从原始事实重放。Campaign 保持 `ready`，candidate 达到
`restored_active`；至此只能声明该 candidate 已完成生产激活，不能据此删除远端升级文件。后继
candidate 若仅达到 `accepted_not_activated`，不得沿用本结论。

### 4.6.7 权威源码、正式发版与远端清理

生产激活完成后，必须把最终 production tree 同步回本地权威仓库，再提交和正式发版。同步必须
以逐文件 manifest 和摘要为准，不得凭记忆挑选文件，也不得以 candidate tree、临时构建目录或
运行镜像反向覆盖本地后续已批准变更。至少校验：

1. `release-catalog.json` 的 Campaign／acceptance 与 promotion receipt 完全一致；ReleaseGraph、
   SnapshotCatalog、Active／Previous、profile／release digest 均可从仓库复算；
2. 本地最终树与已激活 production tree 的每项差异都有明确分类；属于后继维护变更的差异必须
   按第五部分独立验收，未分类差异禁止进入提交；
3. Git commit／tag、源码树摘要、构建参数和 amd64／arm64 发布镜像 digest 相互绑定；正式构建
   不得携带 `candidatecapture`，也不得复用候选镜像；
4. 使用正式发布镜像重新执行独立 canary、生产切换、固定回滚和目标恢复，并生成新的生产激活
   收据。GitHub 发版成功不等于生产已经更新。

GitHub 只保存可公开源码和发布制品，不能替代原始抓包、Campaign、acceptance 和四阶段激活事实。
远端清理前必须把这些材料写入受控私有归档；含凭据或未脱敏字节的内容不得提交 GitHub。归档必须
具有逐文件路径、大小和 SHA-256 清单，并在另一存储位置完成解包、摘要复算以及 acceptance、
promotion 和 activation receipt 重放。只有最终仓库、正式发布镜像和私有证据归档三者均可独立
恢复，才允许清理采集服务器。

清理前生成机器可读的保留／删除清单，并完成以下检查：

1. Vircs 正在运行正式发布镜像的固定 digest；正式 compose／override 已迁出升级临时目录，固定
   回滚镜像和配置仍可用；
2. `capture-cli-*` 和候选 Sub2API 容器不再被生产、归档或收据重放使用，停止后生产健康、网络和
   依赖状态不变；
3. 删除目标只包含已归档的 Campaign、candidate、run、临时源码、构建缓存和候选镜像；不得包含
   生产数据库、Redis、keeper、正式配置、当前镜像、回滚镜像或唯一证据副本；
4. 删除清单先以只读／dry-run 方式解析真实路径、大小和摘要，经人工批准后再按服务器分别执行。

远端清理授权的最终条件是：本地权威提交与 production tree 的差异闭合，正式发布镜像已完成生产
复验，私有证据归档可恢复和重放，删除清单不存在生产依赖或唯一副本。任一条件不满足时，只能停止
空闲采集容器，不得删除升级文件或证据。

---

# 第五部分 Codex 专用非版本门禁

本部分承接退役叙事中的“第五部分 非版本变更维护”，并将共享框架的维护合同收窄为 Codex Persona
专用门禁；跨 Persona 的通用流程仍以 Framework §5 为唯一权威。

## 5.1 兼容代码退休

先执行 Framework §5.5.2，再补充以下 Codex 约束：

- Active／Previous、HTTP／WS／fallback、辅助端点、turn-state、文件上传和 Kilo 双入口必须进入前后
  空 wire 允许列表比较；
- 不得删除仍承担平滑升级、回滚、非 Codex Persona、OpenAI API Key 或独立产品语义的兼容层；
- 旧入口在当前 Catalog 下只能失败时，应替换为清晰错误并删除不可达执行能力，不能保留可重新激活的
  unsigned binding／finalizer／wrapper；
- 业务调用点只能通过当前 attempt 接口开始或保留身份；共享 facade 不得补造或覆盖业务身份。

当前源码树的退休／保留闭集由
`docs/egress/maintenance/compatibility-code-retirement-closure.json` 固化。每个候选只能是
“已退休”或“因产品语义必须保留”；新增未分类标记必须使门禁失败。闭集完成只表示当前范围已审计，
不授权删除其中明确保留的 API Key、第三方入口、平滑升级或回滚语义。
