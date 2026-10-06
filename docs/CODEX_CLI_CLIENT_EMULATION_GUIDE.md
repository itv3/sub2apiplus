# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
>
> **当前版本**：Active 为 `codex-cli 0.160.0`，Previous 为 `codex-cli 0.157.0`；`0.154.0`、`0.151.0` 与 `0.149.1` 已退出 Runtime Catalog（运行投影已移除，画像字节按 §4.6.7 第 1 类保留为冻结历史制品）。完整生产身份见本文 §3.2，生产激活、回滚与 0.154.0 退休的机器事实见 [`0.160 终态收据`](egress/maintenance/CODEX_CLI_0157_TO_0160_TERMINAL_STATE_RECEIPT.json)。第二部分描述当前 Active 0.160.0 的规则：本轮 43 条目标规则中 41 条继承、`SPEC-BODY-006` 条件变化、`SPEC-EP-022` 变化，`SPEC-EP-019` 继承但 A12 路径判据改为官方允许集合。
>
> **权威入口**：共享目标、证据生命周期、升级、发布与回滚以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准；依赖基线见
> [`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)，逐规则机器证据见
> [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。本文只定义 Codex CLI 的规则、画像、实现和专用流程增量。

正式 Campaign 的阶段动作只允许由 `codex_upgrade_supervisor.py campaign-run` 派发；`plan`、
`reuse-official-evidence` 和 `compile-and-run-vc-batch` 是三个受限的 Campaign 引导／批次控制命令；
`compile-vc-batch` 仅保留给历史读取和内部测试，0.154.0 起 Formal CLI 会失败关闭。适用边界见
第四部分开头的[公共执行约定](#codex-part4-premises)、[§4.7](#codex-4-7) 与 Framework §5.1.2、§5.3.2～§5.3.4。历史恢复入口仅供解释旧收据，不得用于新
Campaign，兼容边界见[历史审计 §2](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md#codex-0151-historical-recovery)。

---

# 第一部分 目标、边界与链路

## 1.1 Codex 专用目标与范围

共享仿真目标和最终 wire 等价标准见 Framework §1.1～§1.3。本文只定义 Codex 投影：使用 OpenAI OAuth
账号出站时，最终 wire 由 production active ReleaseBundle 定型；入站兼容层只转换协议、模型、工具和
请求语义，不改变 Key、Group、账号路由或计费归属，也不能选择生产版本或画像。

| 范围 | 内容 |
|---|---|
| 直接覆盖 | 官方 Codex CLI，以及通过 Codex、Compatible、Responses 等接口接入且已批准无损转换的第三方客户端；最终 TLS、连接、HTTP／WebSocket、Header、Body、端点和跨请求状态均按 active 画像定型 |
| 条件覆盖 | 自定义 CA 和自定义 provider；仅在对应条件与证据已冻结时进入其条件分支，不外推为默认行为 |
| 不覆盖 | Anthropic、OpenAI API Key mimic、其他供应商，以及已关闭的 plugins、apps、analytics、otel 流量 |

当前版本和依赖基线见文首；各范围的具体规则与证据见第二部分。

## 1.2 版本演进与请求运行链路

```text
版本演进：官方源码、锁定依赖与真实 wire → 客户端规则画像 → 目标版本画像
         → Candidate → 定向验收 → Active／Previous

请求运行：入站请求语义 → 受信账号路由 → Active ReleaseBundle
         → Codex 方言编译与执行 → OpenAI OAuth 上游
```

两条链路使用同一套规则与画像事实：版本演进链决定“什么可以发布”，请求运行链决定“如何最终出站”。
完整组件和所有权见 Framework §1.3；第二部分定义 Codex 行为，第三部分说明 Sub2API 实现，第四部分
执行版本演进，第五部分补充 Codex 专用非版本门禁。

---

# 第二部分 Codex CLI 客户端规则画像

本部分定义规则成立所需的证据标准、观测边界和 54 个编号项，描述当前生产 active 0.160.0 的规则（previous 为
0.157.0）；生产激活、精确回滚、目标恢复与 0.154.0 Runtime Catalog 退休的机器事实见
[`0.160 终态收据`](egress/maintenance/CODEX_CLI_0157_TO_0160_TERMINAL_STATE_RECEIPT.json)。各条实测中的“0.160.0
官方样本”来自 Campaign `c01600-formal-vc1-r1-20261003t084239z` 的 attempt `20261003T102647Z-6b5390a3ddffc296`，
继承规则沿用的“0.157.0 官方样本”来自 Campaign `c01570-formal-vc1-r2-20260926t084354z` 的 attempt
`20260926T094510Z-ce907a4fa1d25931`；“候选断言”指 `c01600-formal-vc1-r1-20261003t084239z` canonical 链中的
`assert-SPEC-*`（evidence_level 均为 full）。源码字段的行号锚点以本地固化的 0.149.1 源码为准
（`tools/spec_ref_anchors.json`），0.157.0 与 0.160.0 的源码坐标以“文件 第 N 行”写在正文中。

## 2.1 规则证据与准入标准

### 2.1.1 证据类型与权威入口

| 类型 | 材料 | 可以证明 |
|---|---|---|
| L1 | `local-analysis/sources/codex-cli-<version>/codex-rs/` 官方 stable 源码 | 调用链、条件和内部机制 |
| L2 | `tools/spec_source_deps/` 锁定依赖源码 | 指定依赖版本与 feature 下的行为 |
| P／R | pcap、等长脱敏原始字节 | TLS、连接、HTTP／WS 和 Body 的实际输出 |
| J／M／L4 | MITM 应用层 JSONL、解码摘要、manifest、测试和合成输入 | 摘要绑定与辅助验证，不能单独定义官方规则 |

| 内容 | 权威入口 |
|---|---|
| 锁定依赖及摘要 | [`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json) |
| 逐规则机器证据 | [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) |
| 源码锚点 | [`tools/spec_ref_anchors.json`](../tools/spec_ref_anchors.json) |
| Sub2API 实现证据 | `backend/` 与 `docs/egress/` |
| 当前 Active／Previous 及生产身份 | 本文 §3.2、[`Runtime Release Catalog`](../backend/internal/officialegress/catalogdata/runtime/release-catalog.json) 与 [`0.160 终态收据`](egress/maintenance/CODEX_CLI_0157_TO_0160_TERMINAL_STATE_RECEIPT.json) |

每条规则的准入证据包必须绑定官方源码、依赖、二进制、平台、配置、账号、抓包运行号和摘要。只有能够
重新解析的材料可以作为规则依据；R 类材料只允许等长脱敏，未脱敏材料不得离开采集机。

证据基线与运行角色相互独立：经 VC-2 判定 `inherit` 的规则可以继续引用旧版本 L1／L2／P／R，但这不
表示旧版本仍是 Active。运行时 Active／Previous 的机器事实只读取 Release Catalog，§3.2 负责其人类可读
摘要；历史版本身份与原始 run 见[历史审计 §1 版本证据沿革](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md#1-01491-与-01470-版本证据沿革)。

### 2.1.2 规则准入与观测边界

规则只有在被测身份和适用条件明确、源码与合适的 wire 观测通道闭环、正反例充分、引用和摘要
可复算时才能准入。证据不足时只能收窄命题或保持未决。

“固定、随机、条件”分别表示每次一致、允许变化和随明确条件变化，不得把随机样本或条件结果
固化为默认行为。

- pcap、relay、MITM 和服务端重建只能证明各自可见的层次，不能互相替代；
- 自定义 CA、代理和受控失败等条件样本不能外推为默认路径或自然成功链；
- 全集、缺失和连接完整性结论必须基于无预设过滤的完整双向样本。

**遥测零流量判定（当前 active 0.160.0）。** Framework §1.2、§3.2 的公共规则适用；只有下列配置和
源码链均已冻结时，未产生的遥测才可排除在 strict 分母之外：

| 组件 | 关闭条件与源码闭环 |
|---|---|
| analytics | `config/src/types.rs` 第 224-227 行的 `AnalyticsConfigToml.enabled=false` 经 `core/src/config/mod.rs` 第 4481 行传入 analytics client，并由 `analytics/src/client.rs` 第 314-324 行不创建事件队列 |
| OTEL metrics | 必须设置 `otel.metrics_exporter=none`；`config/src/types.rs` 第 646-661 行中 log／trace exporter 默认虽为 `None`，metrics exporter 仍默认为 `Statsig`，且 `otel/src/provider.rs` 第 197-232 行会在其非 `None` 时构建指标管线，因此仅设置笼统的 `otel.exporter=none` 不成立 |

符合上述条件的“零遥测”不计为仿真差异，也不能生成 RequiredRule；未关闭或实际触发的请求仍按正常
出站规则验收。

实现只需对齐官方可见结果，不复制官方内部结构。场景矩阵、重复样本和源码闭环后，可以停止当前
采样；被测身份或条件变化时必须按第四部分重新分类。

### 2.1.3 日常复算

日常复算使用不发送真实请求的仓库门禁：

```bash
make check-egress-spec
```

本地门禁额外校验未提交的官方源码镜像；CI 使用 `make check-egress-spec-ci`，其余规则、证据、
台账和实现契约检查保持一致。

开发机因缺少 `mitmproxy`、pcap 或 Linux 能力而跳过测试，只表示该环境未执行，不能算通过。正式升级
必须在冻结抓包镜像和目标架构中执行依赖门禁，并满足 §4.0.1 的收据分类及 `unexpected_skip=0` 要求。

## 2.2 编号项分组与验收口径

本节只说明编号项如何分组、哪些进入验收分母。共 **54 个编号项**，每项继续使用“范围—规则／机制／
记录—源码—实测—实现—状态”六字段；下表由 [`tools/spec_status.py`](../tools/spec_status.py) 根据逐项状态生成。

<!-- SPEC_STATUS_START -->
| 分组 | 条数 | 当前验证状态 | 默认生产必验项 |
|---|---:|---|---:|
| **① 默认 OpenAI OAuth 可见规则** | **40** | ✅ 38；🟡 2 | **40** |
| **② 自定义 CA 条件分支** | **8** | ✅ 8；🟡 0 | **0** |
| **③ 自定义 provider 条件分支** | **1** | ✅ 1；🟡 0 | **0** |
| **④ 机制项（只对齐可见结果）** | **3** | 源码机制 | **3** |
| **⑤ 观测记录（仅证据审计）** | **2** | 观测记录 | **0** |
| **合计** | **54** | — | **43** |
<!-- SPEC_STATUS_END -->

```text
默认验收：40 个 OAuth 可见规则 + 3 个机制项 = 43 项
条件增量：自定义 CA 成立时增加 8 项；自定义 provider 成立时增加 1 项
仅作证据：2 个观测记录不进入 RequiredRules
```

默认 43 项的机器判据见 VC-2 批准断言画像（含 VC-5 批准修订）
[`candidate_rule_expectations_0_160_0.json`](../tools/official_client_capture/candidate_rule_expectations_0_160_0.json)。条件分支的
“0”只表示默认生产条件未触发，不是永久豁免；条件成立时必须验收对应 8／1 项。证据充分度也不改变
验收分母，因此 `SPEC-EP-012` 即使自然 Voice／realtime 成功抓包有限，仍属于默认 40 项。

images、alpha-search、realtime 和条件 Header 只在各自条件成立时产生，不另立分组；
机制项只对齐官方可见结果，不复制内部结构，观测记录只用于证据审计。

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
  默认客户端失败回退见 `login/src/auth/default_client.rs:305-310`。
- **实测**：`audit-tls002-ca-n0-20260730a`（P）验证 ClientHello；
  `official-h2-20260727T131936Z`（J）验证协商为 h2。
- **实现**：只有有效 CA bundle 才进入该分支；变量未设置、空值或证书无效不得按 h2 画像处理。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-TLS-003 WS ClientHello 扩展顺序不固定

- **范围**：内置 OpenAI OAuth；WS（rustls）。
- **规则**：WS ClientHello 的扩展集合为 `0, 5, 10, 11, 13, 23, 35, 43, 45, 51`，不携带 ALPN；扩展顺序每次随机，
  不得把某一次排列硬编码为固定常量。signature_algorithms 依次为
  `0503, 0403, 0603, 0807, 0806, 0805, 0804, 0601, 0501, 0401, 0904, 0905, 0906`，末三项为 ML-DSA-44／65／87；
  cipher suites、supported_groups（首项 X25519MLKEM768）与 key_share 由传输画像 `codex-0.157.0-ws-rustls` 承载。
- **源码**：[L3] 排列行为由抓包确认；WS 恒走 rustls 的归因见
  `websocket-client/src/lib.rs:68-73`。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）取得四种扩展排列；0.157.0 官方 direct 抓包的 4 份 rustls
  ClientHello（chatgpt.com 3、api.openai.com 1）扩展集合与上列一致、排列各不相同，signature_algorithms 为上列 13 项；
  候选断言 `assert-SPEC-TLS-003` 通过。0.157.0 的 rustls 为 0.23.45（`Cargo.lock` 第 13362 行），provider 仍由
  `utils/rustls-provider/src/lib.rs` 安装 aws-lc-rs。
- **实现**：使用等价 rustls 行为并按传输画像发送 signature_algorithms；不要求每次都产生全新的排列，也不把有限样本
  外推为全局集合。
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
- **规则**：`supports_websockets=true` 时先走 WS；可重试错误（含 `slow_down`）耗尽重试预算并设置
  `force_http_fallback` 后，同一上层调用改走 HTTP POST；受控 426 同样降级到 HTTP。
- **源码**：[L1] `model-provider-info/src/lib.rs:146`、
  `core/src/client.rs:524`、`core/src/client.rs:955`、
  `core/src/responses_retry.rs:85-99`。
- **实测**：`official-httpfb3-20260727T234853Z`（J）记录自然重试耗尽；
  `audit-ep014-turnstate-echo-20260730a`（R）记录受控 426 后的 HTTP 降级请求。候选断言 `assert-SPEC-PROTO-002`
  核对默认先走 WS、预算耗尽后以 HTTP 结束且属于同一上层调用；`slow_down` 按可重试错误解析重试间隔见 0.157.0
  `codex-api/src/sse/responses.rs` 第 465 行与第 698-703 行。
- **实现**：内置 provider 默认启用 WS；HTTP 只在明确降级条件成立时使用。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-CONN-001 主模型 HTTP 调用与 retry 的连接生命周期

- **范围**：内置 OpenAI OAuth；models、responses、images、alpha-search 的 HTTP 链；不含长期持有 Client 的
  backend-client 和 WS prewarm。
- **规则**：不同上层 API 调用各自新建 `reqwest::Client`，正常跨调用不复用 TCP；
  同一次调用的 retry 共享 Client，存活连接可复用，断连后由同一 Client 新建 TCP。
- **源码**：[L1] `login/src/auth/default_client.rs:226-228`、
  `core/src/client.rs:1014-1027`、`codex-api/src/endpoint/session.rs:80-154`、
  `model-provider/src/models_endpoint.rs:76`、`ext/image-generation/src/backend.rs:62-78`、
  `ext/web-search/src/tool.rs:91`。
- **实测**：`clean2-conn-20260728T132008Z`、`audit-conn001-image-repeat-20260730a`、
  `audit-conn001-search-repeat-20260730a`、
  `audit-conn001-retry-keepalive-openai-http-20260730a`、
  `audit-conn001-retry-disconnect-openai-http-20260730a`（均为 R）。候选断言 `assert-SPEC-CONN-001` 核对跨调用
  不复用、同调用 keepalive retry 复用、断连 retry 新建连接。
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
- **规则**：用户 header 按 `HeaderMap.entries` 迭代序输出，不按字典序；发生 `swap_remove` 时也不能把结果简化为
  原始插入序。Client 默认头（`originator`、`user-agent`、条件 residency）在请求交给 reqwest 之前按缺失项追加，
  reqwest 随后才补 `accept: */*` 与 `cookie`：未显式设置 accept 的端点，`accept` 位于默认头之后、`cookie`／`host`
  之前，例如 models 为 `version, authorization, chatgpt-account-id, originator, user-agent, accept, host`。
  Responses 默认线序为 `version, x-codex-beta-features, x-codex-window-id, x-codex-turn-metadata,
  x-openai-internal-codex-responses-lite?, x-codex-routing-hint, x-client-request-id, session-id, thread-id, accept,
  content-encoding, content-type, authorization, chatgpt-account-id, originator, user-agent, cookie?, host,
  content-length`。`cookie` 仅在 Cookie jar 中已有白名单 Cookie（Cloudflare Cookie 或 `__oailb`）时出现，冷启动
  Lite 样本不强制该头。
- **源码**：[L2] `tools/spec_source_deps/http-1.4.0/src/header/map.rs:923-928`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`；各端点的构造顺序由
  L1 `core/src/client.rs:1187-1211`、`core/src/client.rs:1491-1498`、
  `core/src/client.rs:1974-1980` 调用链决定。
- **实测**：`c1491-r14-f-lite-http-response/relay/conn005.client_to_upstream.bin`（R）验证冷启动 Lite Responses
  的原始线序；0.157.0 官方 models 与 Responses 样本线序为上列两种。候选断言 `assert-SPEC-H1-004` 核对 models 线序、
  Responses 允许集合与 Lite 头、routing hint 取值；默认头合并时机见 0.157.0 `http-client/src/client.rs`
  第 121-129 行与第 163-170 行、`http-client/src/client_builder.rs` 第 357-392 行。
- **实现**：逐端点复刻最终线序，不得使用统一字典排序或一份 header 并集。
- **状态**：✅ 源码充分；抓包充分。

## 2.6 HTTP/2（仅自定义 CA）

### SPEC-H2-001 SETTINGS 参数顺序

- **范围**：自定义 CA 条件分支。
- **规则**：SETTINGS 帧按 `ENABLE_PUSH, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE,
  MAX_HEADER_LIST_SIZE` 输出，即参数 ID `2,4,5,6`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.8.1/src/proto/h2/client.rs:110` 与
  `tools/spec_source_deps/h2-0.4.16/src/frame/settings.rs:213-259`。
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
  `tools/spec_source_deps/h2-0.4.16/src/frame/settings.rs:43-44`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：复刻该连接窗口配置，不单独硬写无来源的帧常量。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-007 请求伪头顺序

- **范围**：自定义 CA 条件分支。
- **规则**：`:method, :scheme, :authority, :path`。
- **源码**：[L2] `tools/spec_source_deps/h2-0.4.16/src/frame/headers.rs:698-718`、
  `tools/spec_source_deps/h2-0.4.16/src/hpack/encoder.rs:61-78`。
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
- **规则**：前五项之后的普通 header 小写输出；其顺序是逐个移除前五项后的 `HeaderMap.swap_remove` 结果。握手前若请求
  未显式带 Cookie 且共享 Cookie jar 中有白名单 Cookie，则把 `cookie` 追加到 HeaderMap 末尾，它因此落在前五项之后的
  首位。默认完整握手的剩余线序为 `cookie?, chatgpt-account-id, authorization, user-agent, originator, version,
  x-codex-beta-features, x-client-request-id, session-id, thread-id, x-codex-window-id, x-codex-turn-metadata,
  x-codex-routing-hint, openai-beta, sec-websocket-extensions`；`x-codex-beta-features` 恒在，其余条件头缺失时顺序可能
  整体变化。
- **源码**：[L2]
  `tools/spec_source_deps/tungstenite-openai-0.27.0/src/handshake/client.rs:159-206`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`。
- **实测**：`clean-tool-20260728T132346Z`（R）。候选断言 `assert-SPEC-WS-002` 核对剩余头名全部小写、默认握手
  按上列顺序，关闭 `remote_compaction_v2` 配置的独立握手样本仍带 `x-codex-beta-features`；握手注入 Cookie 见
  0.157.0 `websocket-client/src/lib.rs` 第 171-174 行。
- **实现**：不得把某一完整样本简化为“缺项后原位跳过”的静态数组；Cookie jar 有白名单 Cookie 时必须携带 `cookie`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-003 自定义 provider 注入头的两个大小写特例

- **范围**：自定义 provider；仅当 `http_headers` 注入对应头。
- **规则**：WS 握手中 `origin` 输出为 `Origin`，
  `sec-websocket-protocol` 输出为 `Sec-WebSocket-Protocol`；普通 HTTP 仍为小写。
- **源码**：[L1] 注入入口 `model-provider-info/src/lib.rs:122`；[L2]
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
  `generate=false` 只用于 warmup；`previous_response_id` 只在既有响应前缀可复用时出现，连接键或账号 owner 变化、
  迟到的 tool-result metadata 都会使前缀不可复用。
- **源码**：[L1] `codex-api/src/common.rs:302-328`、
  `core/src/client.rs:1674-1710`、`core/src/client.rs:930`。
- **实测**：`clean-tool-20260728T132346Z`（R）覆盖 Lite；
  `audit-ws005-nonlite-20260730a`（R）覆盖非 Lite、warmup 与增量帧。候选断言 `assert-SPEC-WS-005` 核对字段槽位、
  warmup 的 `generate=false` 与增量帧只在可复用前缀时携带 `previous_response_id`；owner 变化时复位 WS 会话见 0.157.0
  `core/src/client.rs` 第 1538-1548 行。
- **实现**：按 serde 条件省略字段；不得把 Lite 的 13 项子集或“首轮／后续轮”写成固定规则。
- **状态**：✅ 源码充分；抓包充分。

## 2.8 Header

### SPEC-HDR-001 请求 header 的内部组装顺序与 routing hint

- **范围**：派生／内部机制。
- **机制**：请求先由 provider 构造，再合并端点额外头、body 和 configure 结果；每次 retry 最后执行认证。流式路径还会
  先转为 prepared request。Client 默认头在请求交给 reqwest 之前按缺失项补入。对外可见结果是：仅在内置 OpenAI
  ChatGPT OAuth 身份下，为普通 Responses HTTP 与 WS 握手添加 `x-codex-routing-hint`；guardian 审阅请求
  （`x-codex-guardian: reviewer`）不生成该头。值从同一次最终语义 Body 派生：无 `service_tier` 或其值为 `null` 时为
  `model=<model>`；存在字符串 tier 时为 `model=<model>;tier=<service_tier>`。普通 header override、自定义 provider、
  API Key、环境变量 key、experimental bearer、显式 auth 或 AWS provider 均不得生成或覆盖该头。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:48`、
  `codex-api/src/endpoint/session.rs:80-154`、
  `login/src/auth/default_client.rs:296-310`、
  `core/src/client.rs:989-1011`、
  `core/src/client.rs:1140-1141`、`core/src/client.rs:1491-1498`、
  `core/src/client.rs:1619-1623`。
- **实测**：0.149.1 HTTP Main、WS Main 与 WS Lite 主采样均验证 model-only 线序；tier、`null`、重复键、非法 Header
  字节与非 OAuth 身份由源码闭环和本地负例覆盖。wire 只能证明最终集合与线序，不能单独反推内部调用顺序。候选断言
  `assert-SPEC-HDR-001` 核对组装阶段顺序、retry 最后认证与 guardian 审阅请求不生成 routing hint；guardian 判定见
  0.157.0 `core/src/client.rs` 第 1677-1685 行（HTTP）与第 1831-1846 行（WS）。
- **实现**：内部代码可不同，但 routing hint 必须由通过重复键检查的最终 Body 与可信 OAuth 身份共同生成；入站同名头
  一律删除，Body 或 Header 值非法时 fail-close；guardian 审阅请求不生成。覆盖、认证重试和最终线序结果必须与各可见
  规则一致。
- **状态**：— 源码充分；抓包不适用。

### SPEC-HDR-002 Client 默认 header 集合

- **范围**：内置 OpenAI OAuth。
- **规则**：Client 默认头为 `originator`、`user-agent`，以及条件性的
  `x-openai-internal-codex-residency`。residency 来自受管理的 requirements
  配置项 `enforce_residency`，不是环境变量。默认头在请求交给 reqwest 之前按缺失项追加，未显式设置 accept 的端点
  （models、alpha-search、images、realtime calls、OAuth 刷新）因此是
  `…, originator, user-agent, x-openai-internal-codex-residency?, accept, cookie?, host, …`。backend-client 形态的
  WHAM 请求（含 `accounts/check`、`settings/user`）只带自身的 `user-agent`，不带 `originator` 与 residency。
- **源码**：[L1] `login/src/auth/default_client.rs:52`、
  `login/src/auth/default_client.rs:99-104`、
  `login/src/auth/default_client.rs:335-348`、
  `config/src/config_requirements.rs:954`、
  `exec/src/lib.rs:471`、`tui/src/lib.rs:1562`、
  `app-server/src/request_processors/initialize_processor.rs:136`。
- **实测**：`official-body2-20260728T000549Z`（J）验证默认集合；
  `audit-hdr002-residency-20260730a`（R）验证 `us` 正向分支。候选断言 `assert-SPEC-HDR-002` 核对默认身份头与
  residency 正反例；默认头集合见 0.157.0 `login/src/auth/default_client.rs` 第 433-446 行，合并时机见
  `http-client/src/client.rs` 第 121-129 行与第 163-169 行。
- **实现**：未设置 residency 时不发；设置时按端点最终线序合并。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-004 OpenAI-Beta 仅由 Codex 加在 WS 握手

- **范围**：内置 OpenAI OAuth。
- **规则**：Codex 自身只在 WS 握手发送
  `openai-beta: responses_websockets=2026-02-06`；HTTP responses 和 images 不发送。
- **源码**：[L1] `core/src/client.rs:143`、`core/src/client.rs:1147`、
  `cli/src/doctor.rs:110`、`cli/src/doctor.rs:2396`。
- **实测**：`clean-tool-20260728T132346Z`（R）为 WS 正例；
  `audit-body002-plain-20260730a`（HTTP responses）、
  `clean-image-20260728T132405Z`（generations）、
  `relay-imgedit1`（edits）为 R 类反例。
- **实现**：只在 WS 握手添加；自定义 provider 主动注入同名头不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-005 user-agent suffix

- **范围**：内置 OpenAI OAuth。
- **规则**：UA 为平台前缀加可选 ` ({name}; {version})` suffix。exec 使用
  `codex_exec/0.149.1` 与 `(codex_exec; 0.149.1)`；TUI 使用
  `codex-tui/0.149.1` 与 `(codex-tui; 0.149.1)`。启动首次 models 因进程级写入时序，
  有 suffix 和无 suffix 均属于官方观测集。
- **源码**：[L1] `login/src/auth/default_client.rs:39`、
  `login/src/auth/default_client.rs:164`、
  `app-server/src/request_processors/initialize_processor.rs:94-137`、
  `cloud-tasks/src/util.rs:13`。
- **实测**：`clean-search-20260728T132311Z`（exec）、`clean-legacy-20260728T132509Z`
  （TUI）、`audit-ep019-wham-consume-safe-20260730a`（`codex_exec` originator、
  `unknown` 终端标识）均为 R。
- **实现**：按入口和进程状态生成 suffix；不得把一次首次 models 结果硬编码为固定值。
- **0.151.0 ARM64 候选**：exec／TUI 的版本均改为 `0.151.0`，平台前缀为
  `(Ubuntu 24.4.0; aarch64)`；suffix 的可选性和生成规则不变。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-006 accept 按传输和端点变化

- **范围**：内置 OpenAI OAuth。
- **规则**：HTTP responses 为 `text/event-stream`；WS 握手无 `accept`；
  models、alpha-search、images generations／edits 为 `*/*`（reqwest 默认值，所处位置见 SPEC-HDR-002）。
- **源码**：[L1] HTTP responses 显式值见
  `codex-api/src/endpoint/responses.rs:149`；其余 `*/*` 是 reqwest 默认 wire 行为。
- **实测**：`audit-h1raw-20260730a`、`clean-tool-20260728T132346Z`、
  `clean-search-20260728T132311Z`、`clean-image-20260728T132405Z`、`relay-imgedit1`（R）。候选断言
  `assert-SPEC-HDR-006` 核对 HTTP Responses 为 SSE、WS 握手无 accept、辅助端点为 `*/*`。
- **实现**：按端点生成，不使用全局固定值。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-HDR-007 普通 Responses 的会话头

- **范围**：内置 OpenAI OAuth；普通 Responses（HTTP 与 WS 握手）。
- **规则**：发送小写连字符形式 `session-id`、`thread-id`；不发送 `session_id` 或 `conversation-id`。`session-id`
  取缓存亲和键：根会话取 `prompt_cache_key`（临时 fork 为源会话的 session_id），非根 agent 取自身 session_id。
  realtime 的 `x-session-id` 是独立分支；alpha-search 不发送会话头。
- **源码**：[L1] `codex-api/src/requests/headers.rs:8`、
  `codex-api/src/requests/headers.rs:11`；realtime 见
  `core/src/realtime_conversation.rs:1672`。
- **实测**：`audit-h1raw-20260730a`（responses）与 `clean-search-20260728T132311Z`（alpha-search 不发送）均为 R；
  0.157.0 官方普通 Responses 8 条均带 `session-id` 与 `thread-id`。候选断言 `assert-SPEC-HDR-007` 核对普通 Responses
  均含两头、所有请求不含 `session_id` 与 `conversation-id`、realtime 第一跳使用 `x-session-id`；取值见 0.157.0
  `core/src/client.rs` 第 571-593 行与第 1307-1310 行、`core/src/session/session.rs` 第 886-900 行。
- **实现**：严格按端点发送，不能把会话头扩散到 alpha-search；`session-id` 按上述缓存亲和键取值。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-008 四个条件 header

- **范围**：内置 OpenAI OAuth；仅在对应会话来源或 feature 成立时。
- **规则**：
  `x-openai-subagent` 来自子代理／memory consolidation；
  `x-openai-memgen-request: true` 来自内部 memory consolidation；
  `x-codex-parent-thread-id` 来自父线程；
  `x-responsesapi-include-timing-metrics: true` 来自 `runtime_metrics`。
- **源码**：[L1] `core/src/responses_metadata.rs:317-404`、
  `core/src/client.rs:742-775`、`core/src/client.rs:1139-1154`、
  `features/src/lib.rs:973-977`。
- **实测**：`relay-review4`、`audit-hdr008-guardian-20260730a`、
  `audit-hdr008-memgen-20260730a`、`relay-rtmetrics1`（R）。
- **实现**：按条件插入；`x-openai-subagent` 的 `Other(label)` 不得实现成封闭枚举。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-009 工作区路由 override 头

- **范围**：内置 OpenAI OAuth；ChatGPT 认证；Responses HTTP 与 WS 握手。
- **规则**：`GET /backend-api/wham/accounts/check` 给出非默认路由 override（`us`、`us_cr`）时，Responses 请求追加
  `x-openai-account-routing-override`；默认工作区路由（`NO_CONSTRAINT`）下 HTTP Responses 与 WS 握手都不携带该头。
- **源码**：[L1] 反证：锚点基线 0.149.1 源码对 `x-openai-account-routing-override` 0 命中；0.157.0 官方源码中头名
  定义于 `model-provider/src/workspace_routing.rs` 第 12 行，Responses 侧判定见 `core/src/client.rs` 第 1181 行。
- **实测**：0.157.0 官方 A03（HTTP）与 A05（WS）默认路由样本均不带该头；候选断言 `assert-SPEC-HDR-009` 核对两类默认
  样本都不发送。`us`／`us_cr` 分支需要对应工作区账号，由官方源码闭环。
- **实现**：网关首期只做发现与失败关闭，画像在 WorkspaceRouting 记录头名与接受值。
- **状态**：🟡 源码充分；抓包有限，只覆盖默认路由。

### SPEC-HDR-010 guardian 审阅请求的 x-codex-guardian

- **范围**：内置 OpenAI OAuth；guardian 审阅请求（Responses HTTP 与 WS）。
- **规则**：guardian 审阅请求走普通 `/responses`，携带 `x-codex-guardian: reviewer`，不带 `x-codex-routing-hint`，
  请求体不带 `service_tier`；WS 握手中该头位于 `openai-beta` 之后、`sec-websocket-extensions` 之前。普通 Responses
  请求（HTTP 与 WS 握手）不携带 `x-codex-guardian`。
- **源码**：[L1] 反证：锚点基线 0.149.1 源码对 `x-codex-guardian` 0 命中；0.157.0 官方源码中 HTTP 判定见
  `core/src/client.rs` 第 1677-1685 行，WS 判定见第 1831-1846 行。
- **实测**：0.157.0 官方 guardian 审阅 WS 握手为 `…x-codex-turn-metadata, x-codex-parent-thread-id,
  x-openai-subagent, openai-beta, x-codex-guardian, sec-websocket-extensions`，取值 `reviewer`，没有 routing hint；
  A03、A05 普通请求均不带该头。候选断言 `assert-SPEC-HDR-010` 核对取值与两类普通请求不发送。
- **实现**：只在 guardian 审阅请求上生成；该请求不生成 routing hint、不带 service_tier。
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
- **规则**：`enable_request_compression` 默认开启时普通 Responses 使用 zstd（`content-encoding: zstd`）；
  关闭时明文。其余端点均不压缩（见 SPEC-EP-005）。
- **源码**：[L1] `features/src/lib.rs:1087-1090`、
  `core/src/session/session.rs:1403`、`http-client/src/request.rs:41-43`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（zstd responses）与 `audit-body002-plain-20260730a`（关闭压缩），
  均为 R。候选断言 `assert-SPEC-BODY-002` 核对开启时 Responses 为 zstd、关闭时不发送 `content-encoding`；条件见
  0.157.0 `core/src/client.rs` 第 1581-1590 行与 `features/src/lib.rs` 第 1277-1282 行。
- **实现**：只压缩 responses，且尊重 feature 开关。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-003 Lite 模式变换

- **范围**：内置 OpenAI OAuth；模型 manifest 的 `use_responses_lite=true`。
- **规则**：instructions/tools 迁入 `input.additional_tools`；
  `reasoning.context=all_turns`；`parallel_tool_calls=false`。WS 增量帧不重复发送
  已复用的 additional_tools 前缀。
- **源码**：[L1] `core/src/client.rs:825-841`、
  `core/src/client.rs:868-897`、`core/src/client.rs:930`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（HTTP）、
  `clean-tool-20260728T132346Z`（WS），均为 R。
- **实现**：由模型 manifest 驱动；不得把 Lite 变换应用到非 Lite 模型。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-004 turn-state 的消费、保存与回送

- **范围**：派生／内部机制。
- **机制**：HTTP 从初始响应头 `x-codex-turn-state` 读取；WS 从 `response.metadata.headers` 读取。保存到当前 turn
  后，后续 HTTP responses 通过 header、WS 通过 `client_metadata` 原样回送。账号 owner 变化时清空已保存的
  turn-state，下一请求不回送。
- **源码**：[L1] `codex-api/src/sse/responses.rs:62-70`、
  `codex-api/src/endpoint/responses_websocket.rs:747-750`、
  `core/src/client.rs:1630-1634`、`core/src/client.rs:1954-1970`。
- **实测**：`audit-ep014-turnstate-echo-20260730a` 与 `audit-body004-ws-turnstate-20260730a`（R）完成 HTTP、WS
  两条输入→保存→回送闭环。候选断言 `assert-SPEC-BODY-004` 核对回送值不变、两条通道均闭环、owner 变化后不回送；
  清空见 0.157.0 `core/src/client.rs` 第 1538-1542 行。
- **实现**：Sub2API 若终结上下游流，必须保存并回送；透明转发时不得丢失或重复生成；账号 owner 变化时清空。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-005 tool_choice 是字符串

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：`tool_choice` 为 JSON 字符串，当前值 `"auto"`，不是对象。
- **源码**：[L1] `codex-api/src/common.rs:259`、`core/src/client.rs:929`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `clean-tool-20260728T132346Z`、`audit-ws005-nonlite-20260730a`（R）。
- **实现**：所有传输和 Lite／非 Lite 分支均序列化为字符串。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-006 HTTP Responses 的 Lite／非 Lite 字段

- **范围**：内置 OpenAI OAuth；HTTP（`reasoning.effort` 的数字档位形态对 WS `response.create` 同样适用）。
- **规则**：字段全集由 `ResponsesApiRequest` 定义。Lite 省略顶层
  `instructions/tools`、强制 `parallel_tool_calls=false` 并加入
  `reasoning.context=all_turns`；非 Lite 保留顶层 instructions/tools，
  `parallel_tool_calls` 取 prompt 值。Option 字段为空时省略。`reasoning.effort` 为已知档位或不能按 u64 解析的
  自定义值时发 JSON 字符串；自定义档位能按 u64 解析时（如 `"3"`）发 JSON 整数，HTTP 与 WS `response.create` 共用同一
  结构体、形态相同；`x-codex-turn-metadata` 的 `reasoning_effort` 仍记字符串。默认内置模型目录的档位全是字符串，
  整数形态只在用户配置或远端目录给出纯数字自定义档位时出现。
- **源码**：[L1] `codex-api/src/common.rs:253-274`、
  `core/src/client.rs:825-930`。数字档位序列化见 0.160.0 `codex-api/src/common.rs` 第 159-183 行
  （`serialize_reasoning_effort`），turn metadata 字符串化见 0.160.0 `core/src/turn_metadata.rs` 第 83-93 行。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（Lite）与
  `audit-body002-plain-20260730a`（非 Lite），均为 R。0.160.0 官方数字档位定向样本中 `reasoning.effort` 为 JSON
  整数 `3`（A04 HTTP 1 条、A05 WS `response.create` 2 条）。候选断言 `assert-SPEC-BODY-006` 核对 Lite 省略与非 Lite
  保留顶层字段、Lite 的 reasoning 与 text 定型，以及数字档位在 HTTP 与 WS 均为整数。
- **实现**：按模型 manifest 与 Option 值序列化，不硬编码某个模型的一次字段子集。数字档位由画像 ReasoningEffort 节
  （`CustomNumericSerialization=u64_integer`）驱动：能按 u64 解析的档位以 JSON 整数出站，turn metadata 仍记字符串；
  节缺省的画像一律发字符串。
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

### SPEC-BODY-008 client_metadata 的 guardian_credits_requested 与 mcp_attribution

- **范围**：内置 OpenAI OAuth；Responses（HTTP body 与 WS `response.create`）。
- **规则**：普通会话每个 Responses 请求的 `client_metadata` 携带 `guardian_credits_requested`（字符串 `"true"`）与常量
  `mcp_attribution`（字符串 `{"status":"none"}`）；guardian 审阅请求同样写入 `mcp_attribution`。
- **源码**：[L1] 反证：锚点基线 0.149.1 源码对 `guardian_credits_requested` 0 命中；0.157.0 官方源码中键名见
  `core/src/responses_metadata.rs` 第 46 行（`mcp_attribution`）与第 67 行（`guardian_credits_requested`）。
- **实测**：0.157.0 官方 HTTP Responses 请求体 `client_metadata.mcp_attribution` 为字符串 `{"status":"none"}`、
  `guardian_credits_requested` 为 `"true"`，WS `response.create` 各场景均带 `mcp_attribution`。候选断言
  `assert-SPEC-BODY-008` 核对 HTTP 与 WS 两类载体的两项取值。
- **实现**：两项都按字符串写入，不得省略或改为布尔值、对象。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-009 x-codex-turn-metadata 的键集合

- **范围**：内置 OpenAI OAuth；携带 `x-codex-turn-metadata` 的 Responses 请求。
- **规则**：`x-codex-turn-metadata` 的键是画像 `TurnMetadata.Keys` 允许集合（21 键）的有序子集，必含
  `analytics_enabled, installation_id, model, reasoning_effort, request_kind, sandbox, session_id, thread_id,
  thread_source, turn_id, turn_started_at_unix_ms, turn_trigger, window_id`；允许集合另含 `agent_name,
  auto_review_enabled, context_window_id, node_repl_auto_review_required, node_repl_disabled, root_turn_id,
  sandbox_mode, window_number`。默认配置下 `analytics_enabled` 为 `true`。
- **源码**：[L1] 反证：锚点基线 0.149.1 源码对 `turn_trigger` 0 命中；0.157.0 官方源码中 `analytics_enabled` 与
  `turn_trigger` 键见 `core/src/responses_metadata.rs` 第 45 行与第 56 行，`model` 与 `reasoning_effort` 键见
  `core/src/turn_metadata.rs` 第 47 行与第 49 行。
- **实测**：0.157.0 官方样本的封存断言与候选断言 `assert-SPEC-BODY-009` 均满足：键为 21 键的有序子集、必含上列
  13 键，默认配置 `analytics_enabled=true`。
- **实现**：网关只生成有可信取值的 13 键；允许集合里另外 8 键没有可信取值，网关不生成。
- **状态**：✅ 源码充分；抓包充分。

## 2.10 端点与辅助链

### SPEC-EP-001 生图工具呈现与独立 images 调用

- **范围**：内置 OpenAI OAuth；模型支持图像生成时。
- **规则**：非 Lite 可在顶层 `tools` 中发送 namespace `image_gen`／工具
  `imagegen`；Lite 将能力放入 `input.additional_tools` 的 exec 工具目录。
  模型调用后由客户端请求独立 `images/generations` 或 `images/edits`。
- **源码**：[L1] `core/src/tools/spec_plan.rs:106-107`、
  `tools/src/tool_spec.rs:22-45`、`ext/image-generation/src/backend.rs:61-110`、
  `codex-api/src/endpoint/images.rs:33-68`。
- **实测**：`audit-body002-plain-20260730a`（非 Lite）、
  `clean-image-20260728T132405Z`（Lite + generations）、
  `relay-imgedit1`（edits），均含 R。
- **实现**：按 Lite 模式呈现工具；不得改成 hosted `{"type":"image_generation"}`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-002 OAuth 域名分布与 Files 三跳

- **范围**：内置 OpenAI OAuth。
- **规则**：模型与常规业务默认使用 `chatgpt.com/backend-api/*`。条件例外为：
  token 刷新到 `auth.openai.com`；realtime sideband 默认到 `api.openai.com`；
  文件上传 PUT 使用服务端返回的区域 `*.oaiusercontent.com` URL。ChatGPT 认证下工作区路由先经
  `GET /backend-api/wham/accounts/check` 发现 `workspace_backend_origin`，Responses 的 origin 按其改写；默认路由
  （`NO_CONSTRAINT`）下保持 `chatgpt.com`。
- **Files 规则**：`POST /backend-api/files` 的基础 Body 为 `file_name, file_size, use_case`；hosted
  connector 调用必须再同时发送 `codex_connector_id, codex_action_name, codex_model`，三者必须全有
  或全无。随后 PUT 必须逐字使用 create 响应返回的完整 URL；PUT 遇 503 或传输错误最多重试 5 次，全部尝试共享
  5 分钟截止，每次使用新的 `x-ms-client-request-id`。最后 POST `/backend-api/files/{file_id}/uploaded`：create
  响应没有 `pdf_c2pa_reservation=true` 时发送空对象，条件成立时只发送
  `pdf_c2pa_create_request`，其值与本次 create 请求 JSON 等值。`status=retry` 复用 finalize invocation
  轮询。成功响应含
  `file_size_bytes` 时以它为最终大小，缺失时回退到请求大小。
- **源码**：[L1] `model-provider-info/src/lib.rs:377-380`、
  `login/src/auth/manager.rs:194`、`core/src/realtime_conversation.rs:1161-1169`、
  `core/src/mcp_tool_call.rs:441-449`、`core/src/mcp_openai_file.rs:198-243`、
  `codex-api/src/files.rs:26-39`、`codex-api/src/files.rs:119-188`、
  `codex-api/src/files.rs:254-319`。
- **实测**：`oauth-ep002-allhosts`、`oauth-ep002-refresh`（P）；
  `audit-ep012-sideband-synth-20260730a`、
  `audit-ep002-file-upload-full2-20260730a`（R）；0.149.1 hosted 三元字段与大小优先级由官方源码测试、
  Sub2API 三跳集成测试及缺字段负例共同闭环。0.151.0 的 C2PA 正反样本由 Formal r8 的
  `official-relay-file-upload-c2pa-negative／positive` 两个 Job 闭环。候选断言 `assert-SPEC-EP-002` 核对四类域名
  SNI、create 返回 URL 与 PUT URL 一致，以及 C2PA 正反与 retry 三种 uploaded Body；工作区路由见 0.157.0
  `app-server/src/request_processors/account_processor/workspace_routing.rs` 第 253-290 行，blob PUT 重试见
  `codex-api/src/files.rs` 第 200-345 行。
- **实现**：使用配置或服务端返回 URL；不得硬编码单一区域上传 host。create 与 uploaded 分别冻结
  Body attestation，uploaded 的 retry 只复用自身 invocation，避免把 hosted create 条件扩散到空 Body。工作区路由的
  发现与失败关闭见 SPEC-HDR-009；blob PUT 的重试网关首期不仿真。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-005 只有 responses 可使用请求压缩

- **范围**：内置 OpenAI OAuth。
- **规则**：responses 的流式请求路径可设置 compression；models、alpha-search、images 均走不带
  compression 的 execute 路径并明文发送。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:63-154`、
  `codex-api/src/endpoint/responses.rs:135-153`、
  `codex-api/src/endpoint/models.rs:46-62`、
  `codex-api/src/endpoint/search.rs:35-45`、
  `codex-api/src/endpoint/images.rs:33-68`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `audit-h1raw-20260730a`、
  `clean-search-20260728T132311Z`、`clean-image-20260728T132405Z`（R）。
- **实现**：不得给非 responses 端点添加 `content-encoding: zstd`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-006 models 的 URL 与方法

- **范围**：内置 OpenAI OAuth。
- **规则**：`GET {base}/models?client_version=0.149.1`。
- **源码**：[L1] `codex-api/src/endpoint/models.rs:31-55`。
- **实测**：`audit-h1raw-20260730a`（R）。
- **实现**：版本 query 与 CLI 基线一致。
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
  `core/src/realtime_conversation.rs:1161`、
  `core/src/realtime_conversation.rs:1192-1210`、
  `core/src/realtime_conversation.rs:1661`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:808-886`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:1014-1089`、
  `core/src/realtime_conversation/sideband.rs:50-59`。
- **实测**：`webrtc-20260728T134028Z`（R）自然第一跳返回 400；
  `live2-20260728T140403Z`、`audit-ep012-realtime-20260730a`（R）自然第一跳返回 403；
  `audit-ep012-sideband-synth-20260730a`（R）以受控 200 触发官方客户端第二跳。
- **实现**：具备 realtime 条件后按双跳链实现；当前不得把受控 200 写成生产自然成功。
- **状态**：🟡 源码充分；抓包有限。Voice/realtime 自然成功补采暂缓。

### SPEC-EP-013 内置 provider 的 query 边界

- **范围**：内置 OpenAI OAuth；由 `Provider::url_for_path()` 构造的 Codex API URL。
- **规则**：provider 级 `query_params=None`；query 只由端点自身添加。目前 models
  添加 `client_version=0.149.1`，realtime/calls 添加 `intent` 与 `architecture`，
  普通 responses 不带 query。
- **源码**：[L1] `codex-api/src/provider.rs:53`、
  `model-provider-info/src/lib.rs:387`、
  `codex-api/src/endpoint/realtime_call.rs:213-224`。
- **实测**：`audit-h1raw-20260730a`（models／responses）与
  `webrtc-20260728T134028Z`（realtime），均为 R。
- **实现**：不得给全部 Codex API URL 透传统一 query；自定义 provider 不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-015 alpha-search 的 header 与 body

- **范围**：内置 OpenAI OAuth；alpha-search。
- **规则**：header 线序为
  `version, x-codex-turn-metadata, authorization, chatgpt-account-id, content-type,
  originator, user-agent, accept, cookie, host, content-length`。
  body 顶层字段为 `id, model, input, commands, settings, max_output_tokens`；
  `commands` 随检索阶段变化。
- **源码**：[L1] `codex-api/src/search.rs:9-21`、
  `ext/web-search/src/tool.rs:110-120`、`ext/web-search/src/tool.rs:185-195`。
- **实测**：`clean-search-20260728T132311Z`（R）在同一运行取得两次请求和两种 commands；0.157.0 官方
  alpha-search 样本线序为上列十一项。候选断言 `assert-SPEC-EP-015` 核对两次请求线序一致、body 顶层字段与两阶段
  command 不同；请求构造见 0.157.0 `ext/web-search/src/tool.rs` 第 113-119 行与第 193-205 行。
- **实现**：不发送 responses 的 session/thread header；保留阶段性 commands。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-019 WHAM 路径与线序

- **范围**：内置 OpenAI OAuth；backend-client。
- **规则**：使用
  `GET /backend-api/wham/accounts/check`、
  `GET /backend-api/wham/usage`、
  `GET /backend-api/wham/rate-limit-reset-credits`、
  `GET /backend-api/wham/settings/user`、
  `POST /backend-api/wham/rate-limit-reset-credits/consume`。官方 WHAM GET 路径集合另含 TUI 启动与重连时本地预取的
  `GET /backend-api/wham/security-setup`：只在 openai 提供方、ChatGPT 登录且非 FedRAMP、非远程工作区时发，3 秒超时、
  不跟随重定向，每次单独一条连接，线序 `authorization, chatgpt-account-id, user-agent, accept, host`，不带 cookie 与
  originator。TUI 启动期另发 `GET /backend-api/accounts/verified_access`。这两条属 TUI 本地预取，不在网关仿真范围。
  `accounts/check` 是 backend client 的首个请求：exec 每次启动都发出；TUI 经共享本地 daemon 时只在 daemon 冷启动发出，
  daemon 已运行时命中进程级路由缓存。GET 的默认 header 线序为 `user-agent, authorization, chatgpt-account-id, accept, host`；
  usage 在 ChatGPT 认证且非 FedRAMP 账号时于 `chatgpt-account-id` 之后携带 `x-openai-codex-luna-reserve: 1`
  （画像 Slot 34，TUI 周期刷新与手动刷新都带），`accounts/check`、`rate-limit-reset-credits` 与 `settings/user`
  都不带该头；settings/user 在 account-id 后增加 `cache-control: no-cache, no-store`；Cookie jar 建立后在 `accept`
  与 `host` 之间发送 `cookie`。consume 线序为
  `user-agent, authorization, chatgpt-account-id, content-type, accept, host, content-length`，body 顶层只有
  `redeem_request_id`。
- **源码**：[L1] `backend-client/src/client/rate_limit_resets.rs:15-19`、
  `backend-client/src/client/rate_limit_resets.rs:31-109`、
  `backend-client/src/client.rs:226-245`、`backend-client/src/client.rs:463-480`、
  `backend-client/src/client.rs:642-646`。最终线序仍由 wire 确认。
- **实测**：正式 k80 Campaign 的 A12 取得三种 GET 与安全 consume；0.149.1 HTTP Main 取得 `settings/user`；
  Luna Reserve 头由 Campaign `c0154-formal-vc1-recapture-20260915t230327z` 的三份 `/wham/usage` 样本
  （conn003／conn013／conn015）闭环，同轮 `rate-limit-reset-credits`（conn002）与 `settings/user`（conn007／conn010）
  均不带该头。0.160.0 官方 A12 的 WHAM GET 只出现官方路径集合内的五条：security-setup 2 次、各自单独一条连接，
  `accounts/check`、usage、`rate-limit-reset-credits` 与 consume 的形态与上列规则一致；该作业本轮没有发
  `settings/user`（git 署名扩展在会话回合内解析策略时才发，同轮其余 TUI 作业各发 2 次）。候选断言
  `assert-SPEC-EP-019` 核对 WHAM GET 只落在官方五条内、四类 GET 与 consume 的线序、Luna Reserve 取值与 consume body。
  0.160.0 源码：`backend-client/src/client.rs` 第 269-289 行（headers）与第 398-408 行（accounts/check），
  `app-server/src/request_processors/account_processor.rs` 第 1139-1147 行（Luna Reserve 分派），
  `tui/src/startup_orchestration.rs` 第 494-557 行（daemon 启动），`tui/src/security_setup.rs` 第 71-117 行
  （security-setup 预取），`tui/src/daybreak.rs` 第 79 行（verified_access）。
- **实现**：使用 backend-client 独立 header 形态；不得套用 Codex 主模型端点线序；`accounts/check` 计入 WHAM 请求闭集。
  网关不仿真 TUI 启动与重连预取，画像不含 security-setup 与 verified_access 路径。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-021 压缩使用 Remote Compaction V2

- **范围**：内置 OpenAI OAuth；manual 与 auto 压缩。
- **规则**：压缩走普通 `/responses`，并向 input 追加 `{"type":"compaction_trigger"}`，不调用
  `/responses/compact`；`remote_compaction_v2` 配置键被忽略，HTTP Responses 与 WS 握手恒发送
  `x-codex-beta-features: remote_compaction_v2`。TUI 压缩走 WS 时触发项位于 `response.create` 帧中。
- **源码**：[L1] `core/src/compact_remote_v2_attempt.rs:77`、
  `model-provider/src/provider.rs:69-77`、
  `features/src/lib.rs:1529-1532`、`core/src/tasks/compact.rs:41-50`。
- **实测**：`relay-tui-recap-20260728T112358Z`（manual）与
  `audit-ep021-auto-clean-20260730a`（auto），均为 R。候选断言 `assert-SPEC-EP-021` 核对含 compaction_trigger 的
  POST、没有 `/responses/compact` 请求、HTTP 与 WS 恒带 beta 头；`remote_compaction_v2` 为 Removed 键见 0.157.0
  `features/src/lib.rs` 第 1821-1826 行，manual 按 V2 能力直接进入 V2 见 `core/src/tasks/compact.rs` 第 41-49 行。
- **实现**：恒走 V2；入站 legacy compact 请求在出站前失败关闭，不计入账号错误。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-022 独立 images 端点形态

- **范围**：内置 OpenAI OAuth；图像生成／编辑。
- **规则**：
  generations 请求 body 为 `prompt, background, model, quality, size`；
  edits 在首位增加 `images`，不使用 multipart。`background` 按工具参数取值：要求透明背景时为 `transparent`，
  否则为 `opaque`，generations 与 edits 相同。edits 的 `images` 项按引用方式取两种形态之一：按路径引用本地图片时为
  `{"image_url": data URL}`；按数量引用会话近期图片时，会话里 file-backed 的图片为 `{"file_id": …}`、不附带
  image_url，内联图片仍为 `{"image_url": …}`。
  两者 header 线序均为
  `version, authorization, chatgpt-account-id, content-type, originator,
  user-agent, accept, cookie, host, content-length`。
- **源码**：[L1] `codex-api/src/images.rs:5-30`、
  `codex-api/src/endpoint/images.rs:33-68`、
  `ext/image-generation/src/tool.rs:412-469`。background 取值与 edits 两种引用分支见 0.160.0
  `ext/image-generation/src/tool.rs` 第 427-496 行（`request_for_call_args`，第 432-436 行选 background）与第 499-570 行
  （`recent_images`、`output_images`），图片引用类型见 0.160.0 `protocol/src/models.rs` 第 898-904 行
  （`ImageReference`，untagged）。
- **实测**：`clean-image-20260728T132405Z`（generations）与 `relay-imgedit1`
  （edits），均含 R；0.160.0 官方 generations 与 edits 样本线序为上列十项，generations 默认 `background=opaque`
  （2 条）、要求透明时为 `transparent`（1 条），edits 首项为 file_id（1 条）或 image_url（2 条），background 均为
  `opaque`。候选断言 `assert-SPEC-EP-022` 核对两者线序、两种 body、edits 的 JSON data URL、background 两种取值与
  file_id 项不带 image_url；accept 位置由默认头合并时机决定（见 SPEC-HDR-002）。
- **实现**：`n=None` 时省略；edits 以内联 data URL 发送，远程 URL 拒绝。background 与 file_id 引用由画像
  ImageGeneration 节驱动：入站要求透明背景时发 `transparent`，其余发节内默认值 `opaque`；节允许 file_id 时入站
  file_id 引用以 `{"file_id": …}` 发出，否则失败关闭。节缺省的画像 background 原样透传、edits 只收 image_url。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-023 压缩选择与 reason

- **范围**：派生／内部机制。
- **机制**：TokenBudget 分支不产生摘要出站；远程 provider 恒选择 V2；非远程 provider 才可能走 local inline。
  reason 为 `user_requested`、`context_limit`、`model_downshift`、`comp_hash_changed`。遥测 implementation 标签只有
  `responses`、`responses_compaction_v2`，phase 包含 `post_turn`，二者都不能与运行时分支一一等同。输入已含
  compaction_trigger 时不再次追加。
- **源码**：[L1] `analytics/src/facts.rs:404-417`、
  `core/src/tasks/compact.rs:34-65`、
  `core/src/compact_token_budget.rs:21-92`、
  `core/src/session/turn.rs:1013-1242`、
  `core/src/compact_model_fallback.rs:30-40`、
  `core/src/compact_remote_v2_attempt.rs:77`。
- **实测**：`relay-tui-recap-20260728T112358Z`（user_requested）、
  `audit-ep021-auto-clean-20260730a`（context_limit）、
  `audit-ep023-comphash-20260730b`、`audit-ep023-downshift-20260730b`（R）。候选断言 `assert-SPEC-EP-023` 核对
  四种 reason、远程默认 V2、TokenBudget 零出站、已有触发项不重复追加且没有 legacy 实现；标签与 phase 见 0.157.0
  `analytics/src/facts.rs` 第 458-471 行。
- **实现**：对第三方请求做 Codex OAuth 转换时必须保持选择语义和 reason；官方请求已含
  compaction_trigger 时不得再次压缩。内部函数结构无需相同。
- **状态**：🟡 源码充分；抓包有限，wire 只能验证分派结果。

### SPEC-EP-024 /compact 是 TUI 采集入口

- **范围**：采集与观测记录。
- **记录**：TUI 将 `/compact` 解析为手动压缩；`codex exec '/compact'` 将其作为普通
  user message 发送，不产生 compaction_trigger 或 legacy compact 请求。
- **源码**：[L1] `tui/src/slash_command.rs:40`、
  `tui/src/chatwidget/slash_dispatch.rs:264`。
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

当前 active strict wire 是 Codex CLI 0.160.0；自动同步只更新候选值，active ReleaseCatalog 只能经证据验收后显式发布。

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

## 3.2 Codex 0.160.0 active 画像与发布执行契约

active／previous 画像均以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排：

| mode | 版本与画像摘要 | 端点闭集 | 用途 |
|---|---|---|---|
| active | 0.160.0；`d33a4f097f86d57929995760b7e51c4ddf672e5f2db5cb36c0d0815afc3ad12c` | 16 个静态端点（含 `wham_settings_user`、`wham_accounts_check`，与 0.157.0 相同）+ 1 个 ReturnedURL 动态端点；新增可选节 ReasoningEffort（数字档位以 JSON 整数出站）与 ImageGeneration（background 取值与 edits 的 file_id 引用） | 生产默认 |
| previous | 0.157.0；`3edd1c7bd487021469a932ff63599e581000402dd8633dfe02101a2ec4e3ae9d` | 16 个静态端点（含 `wham_settings_user`、`wham_accounts_check`）+ 1 个 ReturnedURL 动态端点 | 受控回滚和历史复算 |

当前 Active 的官方目标身份为 tag `rust-v0.160.0`（commit
`a956835d020762cb2b570053af06f643a11c0ecc`）、`aarch64-unknown-linux-musl` 包
SHA-256 `7f0fe42ff22ecfa3a47bc4a34f5b22c4218b431a4ec0aba51c7d98299f07900c` 和 ARM64 二进制
SHA-256 `50b06603bdcdac39b714f5c3e68583c002b8ad8779ebfdaaf4932ff016b379c0`。原始证据源为 Campaign
`c01600-formal-vc1-r1-20261003t084239z` 的 attempt `20261003T102647Z-6b5390a3ddffc296`；
41 个官方 Job 全部 complete。官方证据由该 Campaign 的 VC-1 自采，后续分类、验收、生产激活与 0.154.0 退休由同一
Campaign 的 canonical 链承接（候选 `c01600-candidate-r1`，attempt `20261003T174604Z-548f4afa2b7d613c`；
43 项迁移为 inherit 41、condition_change 1、change 1，目标规则 43 条）。

VC-6 在 ARM64 上完成：四阶段激活（canary → 切换 → 回滚 → 恢复）所用镜像为
`sha256:3edaaa640945ae8ec71247b4305d85f6e1a96c66b0ebd9cde89edd517d2b26d5`，退休 0.154.0 运行投影后重建的切换镜像
ID 为 `sha256:03bf92c8bcd40dd8b5d84645417168dd6ae28710a41feb3b8395c10bbecc5752`，固定回滚镜像为 v0.2.10-1
`ghcr.io/itv3/sub2apiplus@sha256:ce214cc6103b34859198cbe01450d901c44ded809549a378acd9b9b6a8739ce0`（0.157.0）；
canonical checkpoint 为 `00000009`，执行集合为空。ARM64 当前运行本地构建的切换镜像；0.160 尚未发版，最新发布
镜像仍是 v0.2.10-1（0.157.0），发版与 BWG 等生产机更新按发布流程另行批准。机器事实分别见
[`0.154.0 Runtime Profile 退休收据`](egress/maintenance/CODEX_CLI_0157_TO_0160_RUNTIME_PROFILE_REMOVAL_RECEIPT.json)
和 [`0.160 终态收据`](egress/maintenance/CODEX_CLI_0157_TO_0160_TERMINAL_STATE_RECEIPT.json)。
0.154.0 按 §4.6.7 第 1 类退休：Catalog、selector 与运行投影均已移除，两份画像字节因被既往收据逐文件登记而原地
保留：`31d8654f…`（0.154.0 期 Active）由 `0.151→0.154` 生产激活收据与 `0.154→0.157` 终态链登记，且是发布退役
覆盖层中 legacy compact route 的最后声明画像；`33a537a3…`（0.154.0 早期候选）由 `0.151→0.154` 与
`0.154→0.157` 两份晋升收据清单登记。此前退休的 0.151.0 一份与 0.149.1 两份画像同样原地保留，分别仍由
`0.154→0.157`、`0.151→0.154` 终态收据批准。不得据此认为这些文件已删除。

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

Body 定型只改写画像声明的字段。未被改动的嵌套值（input 项、工具项及其成员）必须逐字节复用入站正文中的
原始区间，不得经 `map[string]any` 往返重编码，否则嵌套键会被改成字典序、大整数会经 float64 改写。被改动的
对象保留其余成员的原始顺序，新增键按字典序追加在末尾；数组项先按内容匹配原始项，匹配不到才按位置对应。
该规则由 `official_egress_json_fidelity_regression_test.go` 与 final-wire 测试锁定：实现方式（解码比对或
字节区间拼接）可以变，输出字节不能变。

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

## 3.4 43 项覆盖与验收边界

43 项包括 13 项 TLS／协议／连接／h1／WS、27 项 Header／Body／端点和 3 项运行上下文／turn-state／压缩机制。
每项必须同时有画像或执行点、官方与候选证据及机器断言；官方与第三方入口必须在同一候选制品和画像下验收。
多账号调度、计费和服务级请求节奏不在 43 项内，画像不改写它们。

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
| `backend/internal/service/official_egress_codex_*`、`official_client_profile_registry.go` | 0.157.0／0.160.0 不可变 Snapshot、可信 release Build 运行态投影、发布投影、端点编排、Files 与模型能力 |
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
变更集 3 的 28 条历史 route 加 `wham_settings_user` 版本 route 构成。HTTP、WS、fallback、
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

兼容代码的完整删除条件和顺序见 Framework §5.5.2 和本手册第五部分。

---

# 第四部分 Codex CLI 版本演进流程

**执行目标：哪里坏修哪里，修好接着跑，整体升级约 6 小时。** 当前工具已支持分阶段续跑、候选 revision、
评估基线及按依赖范围补跑：故障先暂停，修复、复核并对账通过后，从最近合法检查点继续；可信成果按合同承接。
约 6 小时是优化目标，尚无完整实测证明；不能通过省略验收、清零账本或把开工预检移出统计来宣称达标。

耗时统一统计从本轮首次开工预检到最终交付的墙钟，包含修复、等待和归档；只读用途以 VC-6 完成为终点，
生产用途还包含 GitHub 发版及生产服务器更新复核。VC-0～VC-6 与后续发布耗时分别列账再合计，
阶段有效耗时另行报告，不用它替代整体耗时。
当前 B-09／B-10 的真实承接仍关闭，B-11 仅部分单元读集闭合，B-12 本轮未启用候选采集并行；
相关节省不能计作已实现收益。首次实际升级应按上述口径报告总耗时、执行／承接范围及超时原因。

[Framework §5.3](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 规定 `VC-0 → VC-1 → … → VC-6` 顺序；
本部分保留 Codex 专用操作与验收合同。脚本参数、安装及实现细节统一见
[驱动 README](../tools/arm64_capture_driver/README.md)；旧入口与历史事故见
[历史审计](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md)，不作为新 Campaign 的执行依据。

<a id="codex-driver-entries"></a>
## VC-0～VC-6 执行导航

每阶段完成后先重放收据与 checkpoint，再推进下一阶段。脚本成功提示仅供定位，不能替代验收。
下表为唯一阶段入口表；故障分类统一查 [§4.7 失败矩阵](#codex-failure-matrix)。
现成 VC-4／VC-5 整链按 `production_replacement` 编排；`validation_only` 使用对应用途的受管批次，
不能直接套用含生产用途参数和 canonical 步骤的驱动计划。

| 阶段 | 必备输入 | 首跑入口 | 完成标志 | 续跑入口 |
|---|---|---|---|---|
| [VC-0](#codex-vc-0) 冻结输入 | 目标／基线、用途、环境、预算、官方产物和回退点 | 开工 `entry-dryrun.sh … --opening`；新目标 `entry.sh`；同目标官方证据复用见 §4.0.4 | P0、发布认证及 VC-0 checkpoint 可重放，正式取证前 live 请求为零 | 重跑同一 `entry.sh`；旧链已建账本后只用 `stage1-finish.sh` |
| [VC-1](#codex-vc-1) 目标取证 | VC-0、目标源码／依赖及场景 | VC-0 收口派发；同目标用 `reuse-official-evidence` | `official_sealed`，发现和证据无缺口 | 按预约分流对账，再补跑或续 seal 链 |
| [VC-2](#codex-vc-2) 分类批准 | 封存官方证据、基线规则、五清单草案 | `vc23.sh`，内含批准前离线判据门 | 联合摘要已批准，`blocked=0`，VC-2 checkpoint | 对账后按批次身份重派 |
| [VC-3](#codex-vc-3) 生成画像 | VC-2 批准、画像派生及门禁需求 | `vc23.sh` 的 `stage-profile` 批次 | Catalog 暂存收据、VC-3 checkpoint；Active 未变 | 对账后重派，完整产物验证后承接 |
| [VC-4](#codex-vc-4) 固定候选 | 候选 Catalog、批准实现闭集 | 本机 `local-candidate-chain.sh`；ARM64 `arm64-vc4-gates.sh` → `vc4-all.sh` | 同源构建收据、当前 revision 的 VC-4 checkpoint | 重入 `vc4-all.sh`；上传中断用 `--resume-from upload-wait` |
| [VC-5](#codex-vc-5) 定向验收 | 固定候选、准入批准与 VC-4 收据 | `vc5-precheck.sh` 预演／批准补齐 → `vc5-all.sh` | AcceptanceFact、VC-5 完成收据与 checkpoint；生产用途仅剩三个 canonical 项 | 封存前按预约恢复；封存后 `evaluation-recover` |
| [VC-6](#codex-vc-6) 交付／激活 | VC-5；生产用途另需 canonical、Active／Previous 与 rollback | `compile-and-run-vc-batch` 受管批次；收尾材料用 `codex_closeout.py` | 只读交付完成，或目标恢复、归档与清理决定齐全；VC-6 checkpoint | 从最新合法 canonical checkpoint 续派；只读交付重放失败返回 VC-5 |

<a id="codex-part4-premises"></a>
## 第四部分公共执行约定

1. **身份与顺序**：VC-0 冻结 Campaign 总计划、阶段依赖、用途、预算和原始 deadline。后续批次在前序
   checkpoint 封存后编译，继承同一 Campaign 和账本；不能预填尚未产生的 candidate、批准或收据摘要。
2. **派发**：阶段动作只由 `codex_upgrade_supervisor.py campaign-run` 执行。VC-2～VC-6 通过
   `compile-and-run-vc-batch` 原子编译并派发；本节提到的 `classify`、`seal`、`accept`、
   `deliver-candidate` 等均为批次动作，不能绕过监督器直接执行。只读预览及有明确合同的控制入口除外。
   VC-0 预检 `plan`、原子 `vc0_closeout`、`reuse-official-evidence` 和批次编译入口不进入阶段动作队列。
3. **恢复范围**：只执行失败、未完成或依赖变化的闭集。承接须有完整来源、身份、环境、依赖与有效批准；
   文件存在、旧日志绿色或提交相同均不足以放行。工具不支持或证明不足时按原合同重跑。
4. **身份变化**：目标、基线、用途、账号权限或官方产物变化建新 Campaign；规则／场景／目标画像变化走批准
   修订或后继 Campaign。候选源码或构建真实变化且画像不变时，同 Campaign 开新 candidate revision；
   单纯改名、改路径或 build ID 不算真实变化。仅断言 selector 的批准修正见 §4.5.5。
5. **完成与批准**：两个用途均须完成 VC-6。预演不代表批准，工程实现不代表生产已启用；已有授权按精确
   输入形成凭证，输入漂移重新审核。工具缺陷、预算、账务、环境与根因上限先暂停；永久停线条件见 §4.7。

**每轮参数只维护一份。** 从 [env.example.sh](../tools/arm64_capture_driver/driver/env.example.sh) 填写
`ARM64_VC_ENV`，由驱动安全解析，不直接 `source`。`round_context.py` 从正式账本取 Campaign／candidate／revision；
`phase_context.py` 取当前评估基线、读来源、写目标和唯一有效 attempt；`cleanup_context.py` 取收尾路径和收据。
显式 ID 仅作断言，不按“最新目录”猜测，不复制上一轮路径。历史只读 `sealed_receipt_binding` 不可用于新采集准入。

**证据目录统一。** 本机升级证据、临时工作材料及归档统一置于
`/Users/czs/Developer/sub2apiplus-evidence/codex-cli/` 的版本／轮次子目录；不放入 `.codex/artifacts`，
也不散放在 `Developer` 根目录。服务器活动 Campaign 仍使用冻结的宿主数据根，不能以本机归档路径替换。
迁移后保留完整 SHA-256 清单和旧→新路径映射；历史收据原字节不改，按证据库根目录的 `MIGRATION-*.json`
定位，执行重放所需的原路径按恢复合同重建。

<a id="codex-vc-0"></a>
## 4.0 VC-0 冻结升级输入

先准备本轮参数、干净工具提交、bundle、官方完整包、目标源码／锁定依赖、有效生产回退点与批准预算。
受管工具／文档用 `tools/arm64_supervised_deploy.py` 部署，随后按驱动 README 安装并复验驱动；
部署收据与实际三份执行副本一致后才开工。

新目标顺序固定：开工空跑 → `entry.sh` → VC-0 原子收口 → VC-1 首批。`entry.sh --plan` 只展示计划；
开工空跑在独立 staging 和 `fixture_only` 总账内执行，零真实请求，不写正式 Campaign。
`--opening` 重新执行全集并审计读集，演练通过不代表 B 类承接已获批准。

### 4.0.1 DOC-PRE 与 P0：冻结清单与离线验证

入口编排器负责以下步骤，操作员按输出处理阻塞，不再逐项手填命令：

| 顺序 | 入口负责的工作 | 验收要点 |
|---|---|---|
| 1 | 便宜检查、策略兼容／激活认证 | 参数、部署、目标包、源码、场景及环境一次报全；依赖失败项标阻塞 |
| 2 | 入口门禁与 pre-A3、零请求 smoke、atomic-double | 同一次门禁产出 P0 证据与目标平台预跑记录；全部通过才建账本 |
| 3 | 时间账本、ARM64 环境收据、checkpoint、预检 Campaign | `preflight_only` 零请求；账本创建后沿用，不重建计时 |
| 4 | 完整 Job 演练、客户端启动探测 | 启动／重试／清理链可运行；目标 TUI 能在本地替身上发出首帧，daemon 场景确认实际模式 |
| 5 | 发布认证、P0 finalize／replay | 当前部署、认证、门禁、环境和回退依据绑定一致 |
| 6 | 原子 VC-0 收口 | Formal 创建与 VC-1 首批派发由同一入口完成 |

重新取证路径的 P0 必须包含 `make test-capture-tools`、`make check-egress-spec` 的完整结论，
`unexpected_skip=0`。目标平台预跑只提前暴露问题，不替代 VC-5 的正式目标平台门禁。
ARM64 后台运行使用 `setsid -f … < /dev/null`；`nohup` 继承的 SIGHUP 忽略状态会破坏挂断用例。

### 4.0.2 用途

`validation_only`：VC-5 验收后，在 VC-6 零请求只读交付。
`production_replacement`：继续 Catalog 晋升、Active canary、切换、实际回滚、目标恢复与归档。
用途、账号／模型能力和证据语义在 VC-0 冻结，验收后不能追认或修改。

### 4.0.3 ARM64 环境

取证、各类门禁、候选构建及切换演练在 ARM64 完成；本机负责权威源码、提交链和打包。

| 必须核对 | 要求 |
|---|---|
| 数据和权限 | 宿主 `/root/docker/capture-cli/data`；活动数据只放其下，目录 `0700`、文件 `0600`；禁止向 `/root` 解包 |
| 目标客户端 | 宿主和采集容器的官方整包、绝对路径、版本与摘要一致；daemon 所需配套文件齐全，不只拷主二进制 |
| 镜像与依赖 | 两运行镜像 digest、Compose 渲染摘要、静态容器 IP、挂载、抓包拓扑、代理／CA 与冻结环境一致 |
| 出口 | 读取受限运维策略，两容器及两端保护通过实时准入；禁止自动学习出口、回退直连或改网络迁就检查 |
| 资源 | 根盘已用 ≤69%、可用 ≥30 GiB；驱动派发前可用 ≥40 GiB。清理只针对未被收据引用的缓存和临时产物 |

网络配置、接口与切换步骤见[运行时出口运维说明](egress/runtime-egress-operations.md)，本节不重复具体拓扑。
环境 producer 按当前策略、boot ID、租期和目标 Codex Rust TLS 探针核验；旧环境收据不能替代当前准入。
经批准的出口策略变更重新准入即可，不因此重签工具身份或作废未受影响证据；真实依赖变化仍按合同核对。

建账本后的失败重跑同一 `entry.sh`：完整步骤按绑定承接，半成品使用新坐标并保留原记录，启动探测重新执行。
Formal 已建后创建链冻结，只补收口；已派发首批不得再次派发，转对账恢复。

### 4.0.4 退出条件与建 Campaign

退出条件：P0、两端保护／指定出口／TLS、发布认证、目录与资源、回退点全部有效，工具阻断为零，
正式取证前 `live_request_count=0`。任一失败即留在 VC-0。

- **新目标或官方证据身份失效**：`entry.sh` 最后调用 `codex_upgrade_vc0_closeout`，重放当前部署、P0、
  发布认证和启动探测，要求 active VC-0 剩余至少 300 秒；申请整机后原子创建 Formal 并派发首批。
  不可人工调用 `plan --campaign-mode formal`。正式监督器状态目录保留在控制根或其下一层，供首批对账定位。
- **同目标可信官方证据复用**：使用 `pre-a3.sh → stage1.sh → vc0-gate-target.sh → stage2.sh`；
  `pre-all.sh` 可替代最后一步并继续 VC-2／VC-3，且只接受 `EVIDENCE_DECISION=reuse`。
  已建账本但 stage1 收尾失败，只用 `stage1-finish.sh`。`stage2.sh` 在导入前复验启动探测和认证。
- `reuse-official-evidence` 建新 Campaign，沿用前序冻结的 P0／认证，绑定新的时间账本、环境和 Job 演练。
  导入后原子登记 VC-0／VC-1 零请求事件；中断重跑原命令只补缺失步骤，不改旧证据。
  前序为 `awaiting_receipts` 时须按受管入口补齐审计、身份裁定、权限及认证绑定，完成 seal 后再对齐账本。

### 4.0.5 受管工具发布认证

认证链：策略兼容（策略版本变化时）→ 当前部署的策略激活 → pre-A3 → 发布认证 issue／verify。
发布认证绑定部署收据的 `policy`、`wire_producer`、`evidence_semantics`、`control`、`tool_files` 五摘要，
完整 Job 演练及 atomic-double；在具备 Go／Docker 的 Linux ARM64 上签发。

登记的真实链必须完整执行，失败、重复、缺失或 skip 均拒签。跨部署沿用 pre-A3 时通过 `record-reuse`
登记复用收据并纳入新发布认证；无有效证明就重跑。旧认证按冻结合同只读重放，不追改历史字节。
改变受管门禁、不变式或失败分类时，修复验证还须覆盖 `make test-capture-real-chains`；
ARM64 自查设置 `CAPTURE_REAL_CHAINS_REQUIRE_EXECUTION=1`，不得把缺环境跳过记作通过。

<a id="codex-vc-1"></a>
## 4.1 VC-1 收集目标证据

本阶段只确定目标版本事实，不作规则迁移结论。输入是 VC-0 checkpoint、官方源码／二进制／锁定依赖和
冻结目标场景；产物是源码事实、P／R／J／M、`DiscoveryInventory` 与封存的官方证据包。

### 4.1.1 证据来源与复用判定

- 同版本、同官方产物／平台／账号权限／模型可见性且证据语义不变：只读导入 `official_sealed`。
- 仅缺某项事实：定向补证该事实及直接依赖场景；工具、报告或 candidate 变化本身不构成重发官方请求的理由。
- 目标或官方身份变化：返回 VC-0 建新 Campaign。不同模型、平台、会话、代理或 TLS 条件不可直接比较。

官方包下载须先由 `codex_upgrade_official_asset_receipt.py` 冻结 metadata、可用 CDN 地址、证书、大小和 SHA-256；
按收据下载，漂移或摘要不符即停止。源码绑定 tag／commit、Cargo.lock 和锁定依赖，二进制绑定绝对路径、版本与摘要。

### 4.1.2 源码分析、抓包与封存

1. 重放 Formal plan；从生产入口追踪认证、Client、TLS、协议、Header、Body、端点与跨请求状态。
2. 记录调用链、条件、平台／feature、固定／随机属性及新增出站面；每项关联源码、场景和证据位置。
3. 执行冻结的 `capture-official run`；每个 track／Job 使用独立证据根，真实性以协议分支和原始事件为准。
4. 在生成 assertion bundle／manifest 前一次收口权限，再生成 bundle、预览并批准 `capture-official seal`。
5. 重放恢复、secret scan、inventory 和 finalizer，执行 `account-sealed-official` 核算请求并封存 checkpoint。

TUI／daemon 的工作目录、启动参数、模式判定与清理交给冻结场景和驱动；启动探测先在 VC-0 通过。
发现其它会话遗留 daemon／updater 时先核查，不把它混入本轮证据。官方 seal 的端点删除／弃用取值零命中，
只能按工具声明登记延后项交 VC-2；未弃用却零命中仍失败，候选侧不适用该延后规则。

### 4.1.3 目标事实整理与退出

目标身份完整、发现无截断、每项证据可定位解析，恢复／安全／inventory／finalizer 均通过，
且 `official_sealed` checkpoint 可重放，才进入 VC-2。人工复核源码与 wire 闭环，不在此处执行 `classify`。
VC-2 若仍缺事实，只补缺项，其他可信 Job 保持只读。

<a id="codex-vc-2"></a>
## 4.2 VC-2 逐规则判定差异

输入为 VC-1、基线规则、Active 画像和可比证据。五份联合清单是 `target-rules.json`、
`rule-migration.json`、`scenarios.json`、`profile.json`、`assertion-profile.json`。

### 4.2.1 执行步骤

1. 对照目标事实逐条维护规则、迁移、场景及原子断言，用 `prepare-profile` 从 Active Snapshot 派生目标画像。
   自动草案中的 `inherit`／`blocked` 是占位，不是审核结论；每项 source／dynamic discovery 必须唯一处置。
2. 复核五清单交叉绑定和画像补丁，取得本轮联合摘要。新目标先完成人工判定，再调用 `vc23.sh`；
   同目标可用已审核仓库文件重新生成本轮画像／场景绑定，不能直接沿用旧 Campaign 的批准收据。
3. 驱动依次派发 prepare-profile、classify 草案及批准预览；在批准前调用
   `vc2_assertion_preflight.py record／verify`，离线验证**全部官方适用判据**。
   候选内部判据保留到 VC-5；预检失败停在批准前，修正后重新形成摘要和报告。
4. 对已审核的同一联合摘要派发批准批次；VC-3 前再次 verify 预检报告，拒绝输入漂移和旧绿色报告。

驱动通过 `vc2-approve-and-stage.sh` 强制预检次序；具体命令见驱动 README。
`prepare-profile` 输出位于 Campaign 外、事先不存在、父目录 `0700`。
完成以 `classification/result.json`、画像派生收据、动态门禁需求和 VC-2 checkpoint 为准，要求 `blocked=0`。

### 4.2.2 分类口径与阶段边界

| 分类 | 判定 |
|---|---|
| `inherit` | 编号与行为不变 |
| `change` | 规则仍存在，可见行为变化 |
| `condition_change` | 行为仍存在，触发条件变化 |
| `add` | 新规则或新出站面 |
| `delete` | 目标源码不可达，正反场景覆盖、旧引用清单及 RemovalReceipt 齐全 |
| `blocked` | 证据不足，禁止进入 VC-3 |

先证明触发条件与证据可比。source discovery 仅在源码树／指纹相同时继承；dynamic discovery 重新分类。
迁移结论以 `entries[].classification` 为准，discovery 的处置名称不直接代表规则变化，不能按发现数量造新规则。

<a id="codex-vc-3"></a>
## 4.3 VC-3 生成目标画像

### 4.3.1 执行步骤

`vc23.sh` 紧接批准派发 `stage-profile`，用冻结 Go 环境生成候选 Snapshot、ReleaseGraph 和 RuntimeCatalog。
工具重放 VC-2、五清单、派生绑定和门禁需求；画像差异必须满足
`profile_diff_paths ⊆ version_identity_paths ∪ rule_field_paths[affected_rules]`。
输出位于 Campaign 外、尚不存在，父目录 `0700`；不能覆盖半成品。

### 4.3.2 暂存收据与退出条件

`catalog-stage-receipt.json` 与 VC-3 checkpoint 可复算，`blocked=0`，门禁需求闭合，
`active_unchanged=true`、`production_selector_changed=false`、`candidate_release_mode=previous`，才进 VC-4。
批准内容有误回 VC-2；暂存环境故障在当前阶段修复，对账后承接完整已验证产物。入库和构建留到 VC-4。

<a id="codex-vc-4"></a>
## 4.4 VC-4 实现固定 Candidate

### 4.4.1 入库与实现边界

仅实现 `affected_rules` 及直接依赖。每项代码、测试、画像变化均回指已批准规则；发现新行为返回 VC-2。
生产 Active 不变，目标以 `previous` 供候选显式选择；新调用解析新 Bundle，在途调用及连接池不能跨 Bundle。

1. 将 VC-3 Catalog、版本专属测试快照、事实映射与断言画像纳入同源树；同步测试 trace 的默认路径及冻结摘要。
2. 建立本轮 post-promotion 映射，逐项绑定 requirements 摘要、gate／test ID、工作目录和命令。
   `plan-candidate-gates` 生成计划；公共／affected／inherited 集合闭合，不夹入历史固定编号。
3. 用 `driver/local/local-candidate-chain.sh` 形成 A（资产）、C（两个冻结 Catalog 指针）、D（冻结承接）提交链
   和 bundle；重建 Campaign 时重做本轮链，不能沿用绑定旧 Campaign 的提交。
4. ARM64 先运行 `arm64-vc4-gates.sh`，确认 `ARM64_VC4_GATES_DONE` 和门禁收据有效后再运行 `vc4-all.sh`，
   两者不并行。旧 `driver/local/local-vc4.sh` 仅用于解释历史本机门禁记录，新轮次使用 ARM64 同格式产物。

目标 Snapshot／Release 只追加，不改历史节点。新端点须同时闭合 binding、resolver、route 与 release proof；
生产 canary 收据只能由 VC-6 实测。退役 route 例外按
[退役登记](../backend/internal/officialegress/catalogdata/release-route-retirements.json)及其
[加载合同](../backend/internal/officialegress/catalog_route_retirements.go)，不以缺 binding 代替退休证明。

### 4.4.2 同源构建

驱动从同一干净提交准备源码／测试／构建／Docker context，生成前端、ARM64 二进制和不可变镜像，
重放实现测试后登记构建。完整历史、vendor 回退、构建网络及快照索引由准备门禁检查，不再现场手补。
Go 离线编译；需取得前端依赖时只走冻结出口。保留 Git 可执行位，二进制要求 `vcs.modified=false`。

`source transition` 在源码树外生成并通过 `make check-egress-spec`，不能形成自引用；任何输入变化重新生成。
门禁在不含 vendor 的测试树运行，vendor 仅在 `go.mod／go.sum` 逐字相同时沿用。
实现测试、Catalog、门禁计划、source transition、前端装配、二进制及镜像共同绑定到 `build-receipt.json`。

重入 `vc4-all.sh` 先复验完整收据；上传等待中断仅支持 `--resume-from upload-wait`，先核对四树、输入、
门禁与实物。等待受预算和心跳约束；镜像存在或旧日志成功不足以跳过构建。

### 4.4.3 Candidate 身份冻结与 VC-5 交接

| 身份层 | 冻结字段 |
|---|---|
| Candidate | `candidate_id`、`candidate_purpose` |
| 源码 | `git_commit`、`source_tree_sha256` |
| 构建 | `build_id`、`deployed_version`、二进制 SHA-256、架构、构建参数 |
| 镜像 | `image_reference`、`image_id`、OCI `image_digest`，以 `repository@sha256:…` 交接 |
| 画像 | `profile_id`、画像内容 `profile_digest`；文件 SHA-256 另绑定 inventory |

驱动先激活 revision，再由 `record-candidate-build` 完成 revision-seal 和 VC-4 checkpoint。
首次为 r1；源码／构建真实变化时先作废旧候选，再 `revision-open --supersedes`，保留可信 VC-0～VC-3。
Catalog stage 字节必须与 VC-3 一致；画像变化走批准修订／后继 Campaign，不伪装为构建修复。

实现测试只有输入键与来源收据完整一致时承接；仅构建参数变化按合同补目标平台门禁，源码、依赖、工具链、
基础镜像或门禁需求变化全量执行相应测试。新 candidate 的候选抓包从新 attempt 执行批准的全部候选 Job，
不承接旧镜像 Job。尚无 attempt 时允许受管 `candidate-runtime-override` 登记等价容器／路径坐标，
不能用它改账号、镜像、模型或证据身份；首个 attempt 后不再变更。

退出：实现闭集通过、身份和构建收据可重放、Active 未变，尚无候选 attempt／reservation／真实请求。

<a id="codex-vc-5"></a>
## 4.5 VC-5 定向验证

固定 candidate 以 `previous` 运行。主链由 `vc5-all.sh` 按正式阶段收据续作：
准入 → 定向采集与 Kilo 双入口 → 封存 → compare → 逐规则断言 → 外部门禁／accept → 完成收据／canonical。
`VC5_ALL_DONE` 后仍须重放 VC-5 checkpoint；不能只看进程退出或文件存在。

### 4.5.1 执行闭集、运行前检查与身份落盘

1. `vc5-precheck.sh --dry-run` 只读枚举阻塞和拟补齐动作，不建预约、不签 token、不发请求。
2. 绑定 `admission_key`、精确动作集和有效期形成批准，再执行 `--apply --approval <批准件>`；
   仅补齐批准的画像／token 并封存准入。已合法完成时只读复核，失败补偿自身写入，不能盲目重复 apply。
3. `vc5-all.sh` 派发前调用 `--consume`，核对收据、有效参数和当前事实；漂移先返回准入处理。
4. 采集前再复算候选源码、镜像、Profile、VC-4 收据与运行环境，一致后才创建 attempt、`run_nonce` 和预约。

JWT 按实际 `sub2api-admin-jwt/v1` 合同检查三段结构、HS256 签名及 `user_id/email/role/token_version/iat/exp/nbf`，
不自行添加 `iss/sub/aud/jti`。凭据为当前执行用户所有的私有普通文件，无符号链接；旧 `0400` 仅作为待规范化输入，
批准补齐后为 `0600`。剩余有效期按准入报告：默认至少 43200 秒，配置也不得低于 21900 秒（6 小时加 5 分钟）；
日志和错误不得输出 token／签名密钥。有效收据与 token 可复核时无需重新签发。

依赖预检覆盖账号隔离、模型双轨、Live／WS／图片能力、配额、端口、Compose、镜像、挂载、出口与权限。
只替换应用容器并保留回滚点；采集中不 pull、不 compose down、不 prune、不重建数据与网络。
Job 参数只认冻结定义和 attempt argv。`execute=[]` 时在预约前登记 `incremental-noop`，保持零请求、零扫描。

### 4.5.2 定向场景、模型双轨与第三方入口

场景清单定义完整覆盖，本轮只执行批准的 execute 闭集。main／lite 模型由目标版本条目与正式 `/models`
证据共同冻结，分别验证 `use_responses_lite=false/true`，禁止用旧版本或全版本模型并集替代。
官方 CLI／Desktop、Chat Completions、Responses HTTP／WS 均须收敛到同一 candidate Release；
身份冲突、fallback 或连接池不能串版本。

- MITM Job 使用独占空 `CODEX_HOME`，禁插件／更新／遥测、不复制 auth；每个 subject×scenario 独立 checkpoint。
  上游临时关闭即封存当前失败并停下，恢复不重发可信通过坐标。JSONL 场景不产出 pcap 时不得扩大抓包扫描。
- realtime、OAuth refresh、Files 三跳等须由原始事件证明目标分支真实成立；退出码 0 不能代替 `SCN-REALITY-01`。
- run 后、首次 seal 前完成 Kilo Compatible 与 Kilo Responses 两条真实请求，使用冻结 lite 模型及不可变 Kilo 副本。
  发送前确认账号调度投影已恢复；检查 Compatible `POST/200`、Responses `GET/101`、usage 的 WS 模式分别为 false／true。
  响应、usage、账号、模型、镜像和 profile 必须落同一 attempt／nonce 时间窗，WS 不套用 HTTP 的关闭记账顺序。
- client 证据及父目录均为 `0700`；建立 client checkpoint 后不得追加本 attempt 的客户端验证请求。
  请求压缩按画像触发条件判断，不能把支持 zstd 写成每次都必须压缩。

### 4.5.3 Candidate 证据封存

四步顺序保留，驱动负责编排和绑定，人工审核批准不能省略：

| 步骤 | 产物／判定 |
|---|---|
| client checkpoint | Kilo 后采集 client-after，首次 seal 返回 `client_checkpoint_created`，尚未最终封存 |
| 生成并 finalize | bundle、Go test trace、observed-profile、两份 Kilo 收据；运行 fact 来自服务，trace 来自同源冻结测试日志 |
| seal 预览 | 先廉价预检，再唯一深度扫描，生成 manifest／draft／preview 和 `review_sha256`；扫描中断从逐文件 checkpoint 续作 |
| 批准 seal | 仅消费同一草案与摘要，零深度扫描；生成 `candidate_sealed` |

capture manifest／bundle、observed-profile 与 Kilo 收据位于 attempt evidence 根；画像、断言和事实映射位于
同源候选树；trace 用 bundle 相对路径登记。批次动作精确覆盖 execute 项，bundle 与预览各自一批。
底层预览退出码 2 表示待批准，由父监督器识别为合法停靠点，不当成普通失败反复执行。

权限收口先于 manifest。manifest 发布后不得 chmod／chown／改写已绑定证据；仅 mtime／ctime／inode
漂移且其余内容、路径、类型、mode、属主完全一致时，用 `harden-evidence-permissions rebind-boundary`
登记新边界后继续。其余完整性异常永久停线。普通 status／compare／accept 重放摘要链，不能隐式 deep-verify。

标签来自采集参数／场景，不从待通过断言反推；请求序列变化须同步标签。出口探针的 `environment_probe_sni`
只按策略在候选 pcap 声明，不能豁免 OpenAI 业务域名或官方侧。变更须登记工具演进并重建受影响派生物。
封存只证明证据闭合，不代表行为验收通过。

### 4.5.4 离线比较

`compare` 只读复核两侧身份、inventory、恢复、覆盖和 Profile，产出 comparison 与结果模板。
要求 `complete`、`offline_only=true`；`equal=false` 可由采集 surface 不同造成，行为是否通过由逐规则断言判定。
跨机仅同步 manifest 引用的不可变根，保留登记路径；finalizer 只须解析到同一受管相对坐标和有效工具摘要，
不为对齐工作树前缀复制整仓。

### 4.5.5 逐规则机器断言

配置绑定五清单、版本／Profile、双侧 package、capture manifest、证据根和比较结果。
`dual_wire` 两侧均须通过；`candidate_profile` 验证候选内部实现并绑定官方权威摘要。
结果唯一覆盖规则全集：affected 机器执行，inherited 按批准来源重放；全部 `pass/full`，不允许 N/A 或手写通过。

selector 必须包含字段存在性和适用条件，不能用宽松运算掩盖采集缺失。仅修改断言 select／assertion／description，
走 `evaluation-recover approval-revision` 预览与摘要批准，在同 candidate 开新评估基线零请求重评；
规则集合、场景或画像 digest 变化仍需后继 Campaign。

builder 为每规则／侧写依赖投影和链式 checkpoint，最后写 `evaluation-run.json`。
只有投影相同且来源被正式输出绑定授权的 pass 规则可复用；同基线重入不覆盖既有结果，失败恢复开新基线。

### 4.5.6 外部门禁与 accept

当前默认执行同候选源码上的 `make check-egress-spec`、`make test` 及目标平台测试，首次 attempt 完整覆盖。
每次绑定 gate-before／after 环境、attempt、命令、时间、退出码、计数和日志，生成并独立重放
`candidate_external` v4 收据（`gate_plan=null`）；最终失败／跳过均为零才放行。
门禁失败用新收据补失败项，已通过项须证明环境连续性；不能复制旧 JSON 冒充本轮执行。

B-10 真实承接当前关闭。以后启用须同时具备 B-09 完整来源、B-11 读集、平台兼容签字、精确候选绑定及专项批准，
并在排队前、取锁后、执行结束和 accept 前重新核验；失效重新执行，不能以同提交替代证明。

`accept` 独立核验套件／身份、比较／规则、恢复／安全和第三方入口，并重放 executed／reused 断言的各自来源。
通过后写 AcceptanceFact、`accepted=true`、`failed_gates=[]`、`accepted_not_activated`，Campaign 为 `ready`。
收据漂移时 ready 失效；失败记录不覆盖，已作废／被取代 candidate 或非 active 账本不能写验收。

### 4.5.7 ready 与 canonical 交接

两个用途都须有 `vc5-completion.json` 和当前 revision／重开基线的 VC-5 checkpoint。
`validation_only` 由 accept 生成；生产用途在已完成比较、断言、外部门禁和 accept 后才做 canonical 交接。

生产交接先零请求预览 `canonical-import`，核对 execute／reuse 和摘要，再由纯 canonical 批次顺序执行
import（带批准）→ seal → compare → accept。三次 advance 只聚合已通过事实，不能生成第二套规则结论。
完成后只剩 `production-activation`、`rollback-verification`、`retire-<旧 Previous>` 三项，交 VC-6。
`ready` 不代表生产上线，`previous` 候选镜像不能冒充默认 `active` 切换镜像。

### 4.5.8 评估失败的分类与局部恢复

封存前走预约对账／resume；封存后先对账失败父 run，再 `evaluation-recover preview`，按返回的
`admissible_classes` 批准 apply，不猜测 Job 闭集。恢复分流和逐段收口见 [§4.7](#codex-failure-matrix)。
评估器／证据语义变更先登记工具演进及需要的 evaluation epoch；已有评估结果用 `reevaluate` 新开基线。
VC-5 已完成时写 `evaluation_reopened`，新产物落 `control/vc/reopen-b<K>/`，VC-6 绑定新 checkpoint，旧结果只读。

<a id="codex-vc-6"></a>
## 4.6 VC-6 交付或生产激活

所有步骤绑定同一 candidate 和 acceptance SHA。生产用途的候选源码／镜像与晋升后的生产源码／镜像是
两组身份，通过 promotion、逐文件差异和终态门禁连接。生产主链顺序固定：
快照／rollback → promotion → post-promotion → 切换镜像 → canary → 切换 → 实际回滚／目标恢复 → 退休 → 归档收口。

### 4.6.1 用途分流、生产快照与回滚点

- `validation_only`：重放 ready、AcceptanceFact、构建身份和 selector 未变，由受管批次
  `deliver-candidate` 生成交付、VC-6 完成收据与 checkpoint，达到 `ready_for_operator_release`；零请求、零原始扫描。
- `production_replacement`：确认 canonical 仅剩三个 VC-6 项，先记录真实容器 digest、Compose、selector、
  Active／Previous、数据／依赖、网络、挂载、代理／CA 与主机身份。与 VC-0 不符先停止。
  冻结**当前 Active** 的镜像／Profile／Compose 为 rollback，在隔离数据克隆上验证启动、health 与鉴权；
  旧 Previous 只是退休对象，不能充当回滚点。

### 4.6.2 Catalog 晋升与 production tree

在验收源码副本上运行 `backend/cmd/egresscatalogpromote`，离线交换 Release mode，输出 production Catalog、
contract graph 和 promotion receipt；输出须为尚不存在的绝对普通路径。执行前须有双模式夹具和 Go／Python 图一致证明。

production tree 只允许 promotion inventory、已冻结的通用 promotion 实现、mode 互换测试期望及确定性 transition。
生成 candidate→production 逐文件差异；不得夹带业务代码、依赖、Makefile、门禁或版本泄漏 baseline 变化。
超出范围回到候选流程；文本门禁历史债务 `files={}`，不能在晋升时更新基线来绕过检查。

### 4.6.3 动态 post-promotion 门禁

逐项重放 VC-3 需求、VC-4 计划、批准与候选／production tree；公共终态门禁加每个 affected rule 的明确测试，
inherited 和已验收 Job／Kilo 仅重放，不重新执行全量候选和全量目标平台测试。
第一次执行完整已批准计划，后续只补失败项；计划不能在 VC-6 现场改写或加入历史固定编号。

复制原字节 gate plan 到独立私有证据根，记录原命令、工作目录、环境前后探针、结果与日志，生成并重放
`post_promotion` v4 收据。缺项／多项、摘要漂移、失败或跳过均禁止构建切换镜像和 canary；保持旧 Active。

### 4.6.4 权威源码、切换镜像与正式发版

post-promotion 通过后，按逐文件 manifest 同步 production tree 到权威仓库并提交，再在 ARM64 从该提交构建
切换镜像；提交、源码、构建、镜像与 promotion／acceptance 全链可复算。不得带 `candidatecapture`，不得复用候选镜像。
canary、切换、回滚后的目标恢复必须使用同一切换镜像 digest。

VC-6 在 ARM64 完成切换演练与归档收口后，按 §4.6.8 的人工批准门推送，CI 全绿后打注释 tag 正式发版。
GHCR 多架构发版镜像与 ARM64 切换演练镜像分别登记；bot 回写 VERSION 后按合同补冻结承接。
生产服务器另按标准部署流程备份数据库／Compose、拉取固定发版镜像、替换应用并复核健康与身份。
GitHub 发版成功不等于生产已更新，也不能代替私有证据归档。

### 4.6.5 独立 Active canary

用切换镜像建立独立账号、CODEX_HOME、数据库、Redis、配置、网络和证据目录的 canary，按默认 `active` 运行，
强制 mode 计数为零。核对架构、health、HTTP／WS／TLS、错误率、Guard、activation fact 与真实业务完成事件；
版本、profile／release digest 和镜像一致才放行。

### 4.6.6 原子生产切换

先复核 Compose、静态 IP、两端出口保护和固定镜像，只替换应用容器，数据、依赖服务、挂载和网络不变。
仅 registry 缺精确 digest 时定向 pull；前后都复核 image ID／RepoDigest。禁止 compose down 和无范围 prune。
切换后复核 health、日志、依赖、出口、Active、activation fact 与业务结果。
身份、安全、数据、恢复、旧画像兜底、跨 Bundle fallback 或连接池混用异常，立即按冻结回滚点完整回滚。

### 4.6.7 回滚、目标恢复与 canonical 终态

实际切回旧镜像及 Compose，复核 health／鉴权／数据／依赖／final-wire，再恢复目标镜像并重复验证。
只改 selector／mode 不算回滚；不重建数据容器，不删除历史 Snapshot 或证据。
canary、切换、旧版回滚、目标恢复四阶段事实用受管生产激活生成器 finalize／replay，形成同一可复算链。

由 VC-6 批次按顺序推进 `production-activation`、`rollback-verification`、`retire-<旧 Previous>`。
退休对象不能是目标或 rollback；RemovalReceipt 证明消费者为零、运行投影已移除、历史证据保留。
画像字节按以下优先级处理：

1. 历史终态收据逐文件绑定的画像原路径、原字节保留，记 `retained_as_frozen_terminal_artifact`；
2. 仅被 route migration 引用的画像迁入 `version-route-migration-artifacts/frozen-profiles/`，登记保留证明；
3. 两类引用均无才删除，记 `absent` 和 `deleted_in_commit`。

终态清单分别用 `retained_runtime_profiles`、`relocated_runtime_profiles`、`retired_runtime_profiles` 表达上述处置，
不可把保留画像写入必须不存在的删除清单。当前 SnapshotCatalog 确定性裁剪，历史图和晋升中间快照不改写；
运行投影由 `check_runtime_catalog_projection.py` 按收据核对，不能裸 diff 后删除“多余”画像。

`production_active_upgraded` 必须有入库终态收据、promotion／activation／post-promotion／removal、
`codex_audit_index.py generate／check` 生成验证的审计索引，且通过仓库规范门禁和冻结测试。
Active source 必须落同一终态链；VC-4 候选中间态仅按工具明定例外承接，不改变 Active 的来源要求。
canonical 待执行项清零且 `restored_active` 后，首次 `deliver-candidate` 仅到 `production_archive_pending`，继续下一节。

### 4.6.8 私有归档与远端清理

归档包含原始抓包、Campaign、构建、acceptance、promotion、post-promotion、activation、RemovalReceipt 和账本。
本机存放于统一证据库；生成完整路径／大小／SHA-256 清单，并在另一存储位置解包、复算和重放关键收据。
含凭据或未脱敏内容不进 GitHub。权威仓库、镜像与私有归档均可独立恢复后才允许清理服务器。

先登记清理决定。`cleanup_context.py` 必须重放已完成的 VC-6，因此采用新收尾工具执行删除时，
本阶段以延期决定登记精确保留清单、原因和磁盘水位，待 VC-6 完成后再清理，避免依赖自身完成收据。
生产用途的 `private_archive`、`cleanup_decision` 两份统一收据及其引用按 schema 放入当前 Campaign，
由 `codex_upgrade_vc_receipt.py finalize／replay` 验证。随后受管批次再次 `deliver-candidate`，必须同时绑定两份收据；
才生成 `vc6-completion.json` 和当前 VC-6 checkpoint。归档主副本与 Campaign 内封存的凭证用途不同，不改写原路径合同。

**收尾工具**使用 [`tools/codex_closeout.py`](../tools/codex_closeout.py)：read-material／draft 可先生成指南草稿；
VC-6 收口后再 prepare（先 `--dry-run`）→ gates → 人工批准 → publish。工具同步需要的摘要并生成隔离候选材料，
第二部分未变时不触发规则画像摘要级联。配置见驱动 README 的“收尾编排”；工具不代替 canary、切换或回滚。

**人工批准门**：指南草稿、发布计划和同一候选的 full-gates 全集重跑通过后，才可消费指南／发布批准，
正式冻结承接、签发终态及推送；实际清理另有精确目标专项批准。隔离候选材料不代表正式生效。

实际清理先 dry-run：从 `cleanup_context.py` 解析当前 VC-5／VC-6／canonical 绑定，列出目标、备份与恢复演练。
批准、脚本结果和删除验证绑定同一上下文摘要。只删除已归档且无生产／重放依赖的临时产物，
保留正式配置、数据服务、当前和 rollback 镜像、唯一证据，以及可复用抓包基础设施。
中断后只对账，不盲目重派删除；条件不足继续 defer。实际删除另留收据，不回改 VC-6 的历史清理决定。

<a id="codex-4-7"></a>
## 4.7 公共控制面与恢复约定

<a id="codex-fix-and-continue"></a>
### 修好接着跑：出问题先暂停，修好后在原 Campaign 接着跑

固定闭环：**读诊断 → 修复最小范围 → 定向复核 → 登记工具／环境变化 → 对账 → 批准恢复范围 → 续派 → 重放完成收据**。
对账返回 `paused`／review 时先解除对应条件，只有可恢复且取得授权后才按 `next_command` 续跑。
未失效的证据保留原字节和来源；需要新 candidate／Campaign 的变更按身份边界办理，不能强行原地覆盖。

`fix-and-continue.sh <轮次参数文件> --dry-run` 只读检查；正式入口按步骤记录和幂等键执行，
`--list` 查看、`--from <步骤>` 续作。它**仅覆盖 VC-5 封存前、非 recovery_revision 的候选采集**。
部署、演进、延期、恢复批准／授权消费独立有效凭证；缺失就输出 intent 并停下，
不能用参数里的批准人姓名代签。实际边界见[修复安全实现](../tools/arm64_capture_driver/driver/fix_safety.py)。

修复提交先完成相应离线回归再受监督部署；脚本在部署后复核并执行 regression，随后启动独立后台全量验证。
采集前申请整机，后台让路；批次边界发现后台失败／中止即停止后续派发，VC-5 accept 前必须已有通过结论。
CI 可与续跑并行，后台或 CI 失败则暂停，按部署收据经受监督部署回滚工具；新预算事件不兼容旧工具时保持暂停、前进修复。
收尾合入前 full-gates 使用 `re-execute`，正式发版前 CI 全绿。脚本 recover 只启动补跑，
等 `vc5-run-batch.out` 的 `RUN_BATCH_DONE` 和收据通过后，再接 `vc5-all.sh`。

<a id="codex-failure-matrix"></a>
#### 失败矩阵

按本父 run 期间是否有**尚未完整收口的采集预约**分流：有预约用 `reconcile-attempt`，否则
`reconcile-supervisor-run`；已有 attempt 但采集已收口的 seal／评估链走后者。不能以“目录有 attempt”代替判定。

| 故障类型 | 修复／恢复入口 | 复核与下一步 |
|---|---|---|
| VC-0 未完成 | 重入 `entry.sh`；旧链 `stage1-finish.sh` | 复验步骤绑定，沿用已建账本；Formal 已建只补收口 |
| VC-1 采集失败 | 预约对账 → 批准恢复预览 → `resume --rerun-failed --recovery-preview` | 只补失败／未完成／依赖变化 Job；已通过来源需完整恢复证明 |
| VC-1 seal 链失败 | 父 run 对账，修工具后续同 attempt seal 链 | 已发布 assertion bundle 只读复验，不重新发布；幂等证明不足保持 review |
| VC-2／VC-3 父动作失败 | `reconcile-supervisor-run` | 批准和完整产物核验后重派，半成品不覆盖 |
| VC-4 工具或上传失败 | 父 run 对账；上传用 `--resume-from upload-wait` | plan/build 失败只有阶段幂等证明成立且候选未变、无 attempt 才同 revision 续作 |
| VC-5 封存前非恢复段采集失败 | `fix-and-continue.sh` 或预约对账／批准／授权／resume | 预览冻结 execute／reuse 和身份，补跑完成后接主链 |
| 已封存评估器缺陷 | `evaluation-recover preview／apply --root-cause-class evaluator-defect` | 修工具、部署并登记演进，新基线复用采集，只重评受影响部分 |
| 已封存证据对应临时环境故障 | `evaluation-recover … transient-environment` | 工具定位非空 Job 闭集 J*，开 ar<k> 补采并增量封存 |
| 恢复段失败／中断 | 已预约：`reconcile-attempt --recovery-revision ar<k>`；未预约：父 run 对账 | 已预约失败段只读，批准／授权后开后继段；未预约按同段批次身份重派 |
| 候选源码／构建变化 | 对账 → `invalidate-candidate preview／apply` → `revision-open --supersedes` | 新 candidate 从 VC-4 起，保留可信 VC-0～VC-3 |
| 批准输入有误 | selector 修正用 `approval-revision`；其余按批准输入修订／后继 Campaign | 新批准生效后旧验收不能直接沿用 |
| 仅证据元数据漂移 | `harden-evidence-permissions rebind-boundary` | 内容／路径／类型／权限／属主全等才允许继续 |
| 外部门禁失败 | 新 facts／receipt 补跑失败项 | 绑定唯一前序和环境连续性；通过后回 accept 或 post-promotion |
| VC-6 批次失败 | 父 run 对账后按 canonical 续派 | 只补未完成闭集；运行安全异常先完整回滚 |
| 父进程／owner 丢失 | 按预约分流对账 | 自动补收账，按 COMMIT 和正式许可续派，不手写账本事件 |
| 完整性损坏或永久身份失效 | 对账终态 | 旧链只读，按 Framework 建新候选／后继或新 Campaign |

#### 暂停种类与放行

| `pause_kinds` | 必须补齐的条件 |
|---|---|
| `deadline` | `deadline-extend preview／apply`，项目／Campaign／阶段各自批准；保留原始截止和累计时间 |
| `request_budget` | `request-budget-extend preview／apply`，新增预算有凭证，不清零已发生请求 |
| `accounting` | 在所属 Campaign 用 `accounting-resolve`，精确核算或批准上界；未知不能记零 |
| `environment` | 干净环境复核后 `environment-isolate`；隔离影响项不能 seal／复用，可信窗口外结果按证明保留 |
| `root_cause_repair` | 项目上限用 `record-root-cause-repair`；账本 `stop_required` 用 `campaign-resume`，绑定修复／回归／部署 |

解除后重新对账，不自动派发。仍暂停即停在原步骤；`fix-and-continue` 不代做补账、隔离、请求预算扩展或 campaign-resume。
延期不会清空耗时／请求／根因记录，禁止以新建账本绕过预算；批准绑定漂移按工具要求重新预览。

永久停线仅按对账器判定：不可变控制或证据损坏、未被合法演进承接的有效 wire／policy 身份变化、
账本已停止／完成、已封存官方侧受污染、显式放弃。预算到期或同根因达上限不能直接当作永久失败。

### Codex 项目总账、工具身份策略 v2、对账与只读导入

- **两本账**：项目总账跨 Campaign 累计请求／根因和绝对截止；Campaign 时间账本记录阶段、attempt、暂停及恢复。
  事件与 outbox／COMMIT 是权威，head 仅缓存，工具负责锁内重放和幂等补齐；操作员不修 JSON。
- **三层预算**：使用项目、Campaign、阶段最早有效截止。阶段预算取已批准 `STAGE_BUDGETS`，是带恢复余量的上限，
  不能相加当成 6 小时估算；默认 360 分钟也不代表实测耗时。开工前置门禁和阶段间等待仍计端到端墙钟。
- **请求账务**：`live-request-provenance/v2` 按 HTTP POST 模型请求或 WS `response.create` 去重核算。
  `resolved/estimated/unresolved` 如实记录；归属明确的未决挡该 Campaign，无归属未决挡整个项目。
  成功 seal 后用 account-sealed 入口登记，不能从脚本退出码宣称零计费。
- **根因**：使用受管码表和稳定维度，同目标在项目内跨 Campaign／revision／基线累计，默认重试上限 2。
  码表变化先登记迁移，不丢累计次数；修复凭证闭合后按上述入口解除暂停。
- **工具分层**：wire 变化按受影响 Job 和两阶段 transition 决定补采；全部受影响或官方封存被污染不能强行承接。
  evidence_semantics 变化登记 evaluation epoch 并重评；control 变化复验控制门禁。
  policy 变化须兼容／激活认证、受监督部署及 `tool-evolution` 登记；未登记时先补登记，不能直接篡改有效身份。
- **候选级 revision**：以账本 `stage_revision` 和 COMMIT 确认当前候选；r1 保留原 checkpoint，后继落
  `control/vc/revisions/r<N>/`。作废／被取代候选只读；review 状态对账只入账，不能当成重派许可。
- **认证与历史**：生成器修改须登记旧摘要为只读重放身份；旧收据不能用旧身份新签。
  `awaiting_receipts` 导入必须满足 §4.0.4 的额外绑定，不能拼多 attempt 为一个可信来源。

合同字段与原子写入细节以
[Campaign 工具](../tools/official_client_capture/codex_upgrade.py)、
[项目总账](../tools/official_client_capture/codex_upgrade_project_ledger.py)、
[工具身份策略](../tools/official_client_capture/tool_identity_policy_v2.json)及各收据 schema 为准。

### Codex 依赖键、监督器与恢复

`result_key = item_id + input_sha256 + environment_sha256 + direct_dependency_sha256`。
依赖按 producer／evaluator／control／scenario／runtime／network／gate 登记；声明的逐 Job 依赖由分析器生成并验收，
不以手工缩小列表换取承接。缺少完整绑定按重跑处理。

B-09／B-10 不是默认跳过全集的开关：完整宿主读集、运行镜像／平台／内核、外部依赖／数据快照、来源工作区和
独立审核／变更批准都须闭合。B-09 最长 24 小时，重复签发不续期；校时偏差 ≤60 秒、样本年龄 ≤300 秒，
撤销查询年龄 ≤60 秒，`now >= expires_at` 立即失效。B-10 另绑定目标 candidate 和平台兼容批准。
失效全量重跑，不能靠移动来源目录、缓存或补写 JSON 保持有效；开关仍关闭，详见驱动 README 的 B-09／B-10。

#### 父 run、批次与重派

批次采用 staging／WAL，只有完整 COMMIT 占用序号。控制面负责 admission、直接前序 checkpoint、
账本和 deadline 核对后启动唯一父监督器；操作员只使用编译派发入口。

| 中断边界 | 续派规则 |
|---|---|
| staging／prepare 失败，未 COMMIT | 工具对账后同序号新 staging；保留原失败收据 |
| COMMIT 已写但父 run 未启动或终态收口丢失 | 对账获许可后 N+1 重派同批次身份；执行细节仅按修复合同调整 |
| COMMIT 摘要／owner／坐标不符 | 完整性异常，禁止重派 |
| 有尚未收口预约 | 转 attempt／恢复段对账，不能用父 run 对账绕过请求核算 |

批次身份、动作三元组和来源链须一致；换基线不是普通重派。官方 bundle 已发布时只续未完成 seal 动作。
对账显示 review、缺幂等证明或失败动作无法可信定位时保持停止，不猜序号，不强行重编已封存批次。

### Codex 评估失败局部恢复与断点续跑

评估基线 b<K> 由账本 `evaluation_baseline` 与 COMMIT 授权，不按最大目录选取。
`stage_sources` 指定各阶段 local／reuse；四项 evaluator 摘要、输出绑定与逐规则投影共同决定重评范围。
结果源未锚定或证明不足时 `reuse_authority=none`，不能把旧 pass 直接搬入新基线。

恢复段 ar<k> 使用 apply 返回编号，只补 J*；原 attempt 和失败段不可覆盖。
段失败后按段预约→基线 COMMIT→recovery.json 推出权威 J*，批准／授权后开后继段；每个可复用 Job 必须同时有
完整 result／checkpoint、逐文件摘要和权限边界、相同执行合同、有效 after 探针和恢复报告。
缺证明进入 execute；复用 Job 启动数为零，只按实际新增请求入账。

增量封存仍按 Kilo 后检查点 → 受管派生／finalizer → 复用根投影与 delta 扫描 → 预演／seal。
只扫描 delta，重采根与复用根互斥，最终根集合精确闭合；随后重跑 compare，断言按新依赖投影判定 execute／reuse。
完整结果已写但父收口丢失时，由对账证明后幂等重派补收口，不重发已成功 Job。

### Codex 连续监督、时间账本与文档部署

监督器即时记录动作事件，每 5 秒心跳、每 60 秒分类 planning／active／waiting；worker 失联、超时或派发停滞
按合同停止。截止前保留 120 秒采集清理及最多 5 秒父终态排空，先触发正常 finally，再按预算强停。
每阶段记录起止、执行／承接、失败、请求及扫描量，不能留下未分类时间后宣称完成。

出口异常保存 `egress-pause.json` 和不确定窗口，停止派发并受控收口；网络恢复不让旧 run 自动续跑，
须核算窗口内受影响 Job／请求。重启／重建走 `egress-transition` 维护入口，最多 60 秒且不延长 deadline；
期间业务闭锁，结束前双容器普通准入通过，不用维护瞬态签发环境收据。

Framework 与本指南是受管依赖，部署清单登记路径及完整摘要，与工具同一可回滚事务切换；
部署前复算指南第二部分摘要与场景清单。受监督部署后重新安装／复验驱动，保留 Git 可执行位。
本机修改文档不等于服务器已部署；升级开工前必须检查最新提交的工具、文档、驱动安装收据一致。

---

# 第五部分 Codex 兼容代码退休附加门禁

先执行 Framework §5.5.2，再满足以下 Codex 专用约束：

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

历史版本证据、0.151 旧恢复机制和旧章节编号映射仅用于审计，见
[`CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md`](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md)。
