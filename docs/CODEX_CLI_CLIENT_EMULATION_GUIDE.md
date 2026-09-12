# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
>
> **当前版本**：Active 为 `codex-cli 0.151.0`，Previous 为 `codex-cli 0.149.1`；`0.147.0` 已退出 Runtime Catalog。完整生产身份见本文 §3.2。
>
> **权威入口**：共享目标、证据生命周期、升级、发布与回滚以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准；依赖基线见
> [`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)，逐规则机器证据见
> [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。本文只定义 Codex CLI 的规则、画像、实现和专用流程增量。

正式 Campaign 的阶段动作只允许由 `codex_upgrade_supervisor.py campaign-run` 派发；`plan`、
`reuse-official-evidence` 和 `compile-vc-batch` 是三个受限的 Campaign 引导／批次控制命令，适用边界见
第四部分公共执行约定和 Framework §5.1.2、§5.3.2～§5.3.4。历史恢复入口仅供解释旧收据，不得用于新
Campaign，兼容边界见附录 A。

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

本部分定义规则成立所需的证据标准、观测边界和 53 个编号项。当前生产 active 为 0.151.0，
previous 为 0.149.1；本轮差异规则、ARM64 身份事实和 Files C2PA 条件分支均已完成生产激活、
精确回滚、目标恢复和 0.147 Runtime Catalog 退休。

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
| 当前 Active／Previous 及生产身份 | 本文 §3.2、[`Runtime Release Catalog`](../backend/internal/officialegress/catalogdata/runtime/release-catalog.json) 与 [`0.151 终态收据`](egress/maintenance/CODEX_CLI_01491_TO_0151_TERMINAL_STATE_RECEIPT.json) |

每条规则的准入证据包必须绑定官方源码、依赖、二进制、平台、配置、账号、抓包运行号和摘要。只有能够
重新解析的材料可以作为规则依据；R 类材料只允许等长脱敏，未脱敏材料不得离开采集机。

证据基线与运行角色相互独立：经 VC-2 判定 `inherit` 的规则可以继续引用旧版本 L1／L2／P／R，但这不
表示旧版本仍是 Active。运行时 Active／Previous 的机器事实只读取 Release Catalog，§3.2 负责其人类可读
摘要；历史版本身份与原始 run 见附录 A。

### 2.1.2 规则准入与观测边界

规则只有在被测身份和适用条件明确、源码与合适的 wire 观测通道闭环、正反例充分、引用和摘要
可复算时才能准入。证据不足时只能收窄命题或保持未决。

“固定、随机、条件”分别表示每次一致、允许变化和随明确条件变化，不得把随机样本或条件结果
固化为默认行为。

- pcap、relay、MITM 和服务端重建只能证明各自可见的层次，不能互相替代；
- 自定义 CA、代理和受控失败等条件样本不能外推为默认路径或自然成功链；
- 全集、缺失和连接完整性结论必须基于无预设过滤的完整双向样本。

**遥测零流量判定（当前 active 0.151.0）。** Framework §1.2、§3.2 的公共规则适用；只有下列配置和
源码链均已冻结时，未产生的遥测才可排除在 strict 分母之外：

| 组件 | 关闭条件与源码闭环 |
|---|---|
| analytics | `config/src/types.rs:217-223` 的 `AnalyticsConfigToml.enabled=false` 经 `core/src/config/mod.rs:4182` 传入 analytics client，并由 `analytics/src/client.rs:222-233` 禁用事件队列 |
| OTEL metrics | 必须设置 `otel.metrics_exporter=none`；`config/src/types.rs:585-592` 中 log／trace exporter 默认虽为 `None`，metrics exporter 仍默认为 `Statsig`，且 `otel/src/provider.rs:194-230` 会在其非 `None` 时构建指标管线，因此仅设置笼统的 `otel.exporter=none` 不成立 |

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

本节只说明编号项如何分组、哪些进入验收分母。共 **53 个编号项**，每项继续使用“范围—规则／机制／
记录—源码—实测—实现—状态”六字段；下表由 [`tools/spec_status.py`](../tools/spec_status.py) 根据逐项状态生成。

<!-- SPEC_STATUS_START -->
| 分组 | 条数 | 当前验证状态 | 默认生产必验项 |
|---|---:|---|---:|
| **① 默认 OpenAI OAuth 可见规则** | **39** | ✅ 38；🟡 1 | **39** |
| **② 自定义 CA 条件分支** | **8** | ✅ 8；🟡 0 | **0** |
| **③ 自定义 provider 条件分支** | **1** | ✅ 1；🟡 0 | **0** |
| **④ 机制项（只对齐可见结果）** | **3** | 源码机制 | **3** |
| **⑤ 观测记录（仅证据审计）** | **2** | 观测记录 | **0** |
| **合计** | **53** | — | **42** |
<!-- SPEC_STATUS_END -->

```text
默认验收：39 个 OAuth 可见规则 + 3 个机制项 = 42 项
条件增量：自定义 CA 成立时增加 8 项；自定义 provider 成立时增加 1 项
仅作证据：2 个观测记录不进入 RequiredRules
```

默认 42 项的机器清单见
[`codex_upgrade_rules_0_151_0.json`](../tools/official_client_capture/codex_upgrade_rules_0_151_0.json)。条件分支的
“0”只表示默认生产条件未触发，不是永久豁免；条件成立时必须验收对应 8／1 项。证据充分度也不改变
验收分母，因此 `SPEC-EP-012` 即使自然 Voice／realtime 成功抓包有限，仍属于默认 39 项。

images、alpha-search、legacy compact、realtime 和条件 Header 只在各自条件成立时产生，不另立分组；
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
- **源码**：[L1] `model-provider-info/src/lib.rs:146`、
  `core/src/client.rs:524`、`core/src/client.rs:955`、
  `core/src/responses_retry.rs:85-99`。
- **实测**：`official-httpfb3-20260727T234853Z`（J）记录自然重试耗尽；
  `audit-ep014-turnstate-echo-20260730a`（R）记录受控 426 后的 HTTP 降级请求。
- **实现**：内置 provider 默认启用 WS；HTTP 只在明确降级条件成立时使用。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-CONN-001 主模型 HTTP 调用与 retry 的连接生命周期

- **范围**：内置 OpenAI OAuth；models、responses、legacy compact、images、alpha-search
  的 HTTP 链；不含长期持有 Client 的 backend-client 和 WS prewarm。
- **规则**：不同上层 API 调用各自新建 `reqwest::Client`，正常跨调用不复用 TCP；
  同一次调用的 retry 共享 Client，存活连接可复用，断连后由同一 Client 新建 TCP。
- **源码**：[L1] `login/src/auth/default_client.rs:226-228`、
  `core/src/client.rs:1014-1027`、`codex-api/src/endpoint/session.rs:80-154`、
  `model-provider/src/models_endpoint.rs:76`、`ext/image-generation/src/backend.rs:62-78`、
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
  `swap_remove` 时也不能把结果简化为原始插入序。0.149.1 的 Responses 中，
  `x-openai-internal-codex-responses-lite`（若有）位于 `x-codex-turn-metadata` 之后，
  `x-codex-routing-hint` 随后追加并位于 `x-client-request-id` 之前。`cookie` 仅在
  Cookie jar 已建立时出现；冷启动 Lite 样本不强制该头。
- **源码**：[L2] `tools/spec_source_deps/http-1.4.0/src/header/map.rs:923-928`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`；各端点的构造顺序由
  L1 `core/src/client.rs:1187-1211`、`core/src/client.rs:1491-1498`、
  `core/src/client.rs:1974-1980` 调用链决定。
- **实测**：`c1491-r14-f-lite-http-response/relay/conn005.client_to_upstream.bin`（R）
  验证冷启动 Lite Responses 的最终原始线序；models 与条件 turn-state 由同 Campaign
  其他受管样本覆盖。
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
  `generate=false` 只用于 warmup；`previous_response_id` 只在既有响应前缀可复用时出现。
- **源码**：[L1] `codex-api/src/common.rs:302-328`、
  `core/src/client.rs:1674-1710`、`core/src/client.rs:930`。
- **实测**：`clean-tool-20260728T132346Z`（R）覆盖 Lite；
  `audit-ws005-nonlite-20260730a`（R）覆盖非 Lite、warmup 与增量帧。
- **实现**：按 serde 条件省略字段；不得把 Lite 的 13 项子集或“首轮／后续轮”写成固定规则。
- **状态**：✅ 源码充分；抓包充分。

## 2.8 Header

### SPEC-HDR-001 请求 header 的内部组装顺序与 routing hint

- **范围**：派生／内部机制。
- **机制**：请求先由 provider 构造，再合并端点额外头、body 和 configure 结果；
  每次 retry 最后执行认证。流式路径还会先转为 prepared request。Client 默认头是
  与请求级头并行的入口。对外可见结果是：0.149.1 仅在内置 OpenAI ChatGPT OAuth 身份下，为普通 Responses HTTP、legacy
  compact 与 WS 握手添加 `x-codex-routing-hint`。值从同一次最终语义 Body 派生：无
  `service_tier` 或其值为 `null` 时为 `model=<model>`；存在字符串 tier 时为
  `model=<model>;tier=<service_tier>`。普通 header override、自定义 provider、API Key、环境变量
  key、experimental bearer、显式 auth 或 AWS provider 均不得生成或覆盖该头。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:48`、
  `codex-api/src/endpoint/session.rs:80-154`、
  `login/src/auth/default_client.rs:296-310`、
  `core/src/client.rs:630-635`、`core/src/client.rs:989-1011`、
  `core/src/client.rs:1140-1141`、`core/src/client.rs:1491-1498`、
  `core/src/client.rs:1619-1623`。
- **实测**：0.149.1 HTTP Main、WS Main 与 WS Lite 主采样均验证 model-only 线序；tier、`null`、
  重复键、非法 Header 字节与非 OAuth 身份由源码闭环和本地负例覆盖。wire 只能证明最终集合与线序，
  不能单独反推内部调用顺序。
- **实现**：内部代码可不同，但 routing hint 必须由通过重复键检查的最终 Body 与可信 OAuth 身份共同
  生成；入站同名头一律删除，Body 或 Header 值非法时 fail-close。覆盖、认证重试和最终线序结果必须
  与各可见规则一致。
- **状态**：— 源码充分；抓包不适用。

### SPEC-HDR-002 Client 默认 header 集合

- **范围**：内置 OpenAI OAuth。
- **规则**：Client 默认头为 `originator`、`user-agent`，以及条件性的
  `x-openai-internal-codex-residency`。residency 来自受管理的 requirements
  配置项 `enforce_residency`，不是环境变量。
- **源码**：[L1] `login/src/auth/default_client.rs:52`、
  `login/src/auth/default_client.rs:99-104`、
  `login/src/auth/default_client.rs:335-348`、
  `config/src/config_requirements.rs:954`、
  `exec/src/lib.rs:471`、`tui/src/lib.rs:1562`、
  `app-server/src/request_processors/initialize_processor.rs:136`。
- **实测**：`official-body2-20260728T000549Z`（J）验证默认集合；
  `audit-hdr002-residency-20260730a`（R）验证 `us` 正向分支。
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
  `core/src/realtime_conversation.rs:1672`。
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
- **源码**：[L1] `core/src/responses_metadata.rs:317-404`、
  `core/src/client.rs:742-775`、`core/src/client.rs:1139-1154`、
  `features/src/lib.rs:973-977`。
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
- **源码**：[L1] `features/src/lib.rs:1087-1090`、
  `core/src/session/session.rs:1403`、`http-client/src/request.rs:41-43`。
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
- **源码**：[L1] `core/src/client.rs:825-841`、
  `core/src/client.rs:868-897`、`core/src/client.rs:930`。
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
  `codex-api/src/endpoint/responses_websocket.rs:747-750`、
  `core/src/client.rs:1630-1634`、`core/src/client.rs:1954-1970`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、
  `audit-ep014-turnstate-compact-20260730a`、
  `audit-body004-ws-turnstate-20260730a`（R）完成三条输入→保存→回送闭环。
- **实现**：Sub2API 若终结上下游流，必须保存并回送；透明转发时不得丢失或重复生成。
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

- **范围**：内置 OpenAI OAuth；HTTP。
- **规则**：字段全集由 `ResponsesApiRequest` 定义。Lite 省略顶层
  `instructions/tools`、强制 `parallel_tool_calls=false` 并加入
  `reasoning.context=all_turns`；非 Lite 保留顶层 instructions/tools，
  `parallel_tool_calls` 取 prompt 值。Option 字段为空时省略。
- **源码**：[L1] `codex-api/src/common.rs:253-274`、
  `core/src/client.rs:825-930`。
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
  文件上传 PUT 使用服务端返回的区域 `*.oaiusercontent.com` URL。
- **Files 规则**：`POST /backend-api/files` 的基础 Body 为 `file_name, file_size, use_case`；hosted
  connector 调用必须再同时发送 `codex_connector_id, codex_action_name, codex_model`，三者必须全有
  或全无。随后 PUT 必须逐字使用 create 响应返回的完整 URL。0.149.1 最后以空对象 POST
  `/backend-api/files/{file_id}/uploaded`；0.151.0 在 create 响应没有
  `pdf_c2pa_reservation=true` 时仍发送空对象，条件成立时只发送
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
  `official-relay-file-upload-c2pa-negative／positive` 两个 Job 闭环。
- **实现**：使用配置或服务端返回 URL；不得硬编码单一区域上传 host。create 与 uploaded 分别冻结
  Body attestation，uploaded 的 retry 只复用自身 invocation，避免把 hosted create 条件扩散到空 Body。
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
- **规则**：`GET {base}/models?client_version=0.149.1`。
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

### SPEC-EP-014 legacy compact 的 header 集合

- **范围**：内置 OpenAI OAuth；legacy compact。
- **规则**：默认 Lite 线序的允许全集为
  `version, x-codex-installation-id, x-codex-window-id, x-codex-turn-metadata,
  session-id, thread-id, x-codex-routing-hint, x-openai-internal-codex-responses-lite, authorization,
  chatgpt-account-id, content-type, accept, originator, user-agent, cookie, host,
  content-length`。除 `cookie` 外各项必须存在且顺序固定；`cookie` 仅在 Cookie jar
  已建立时出现，并固定在 `user-agent` 与 `host` 之间。条件头位于
  `x-codex-installation-id` 之后、
  `x-codex-window-id` 之前：分别触发时，`x-codex-beta-features` 或
  `x-codex-turn-state` 占第 3 个 header 槽。
- **源码**：[L1] `core/src/client.rs:613-638`、`core/src/client.rs:1954-1971`、
  `codex-api/src/endpoint/responses.rs:89`。
- **实测**：`c1491-r14-f-lite-legacy-compact-default/relay/conn006.client_to_upstream.bin`
  证明冷启动 Lite 默认请求携带 Lite 头但没有 Cookie；
  `c1491-r14-f-official-legacy-compact-default/relay/conn007.client_to_upstream.bin`
  证明 Cookie jar 建立后的 main 默认请求在同一固定槽携带 Cookie。两份 R 证据分别
  冻结模型条件与 Cookie 条件，不要求把两个独立条件合并到同一官方请求。
  `audit-ep014-beta-legacy-20260730a`（beta）、
  `audit-ep014-turnstate-compact-20260730a`（turn-state），均为 R。
- **实现**：按模型、Cookie jar 和条件头事实分别决定是否出现，并按固定插槽生成；
  不得把缺失条件头简单追加到末尾，也不得把 Cookie 错误提升为 Lite 请求必选头。
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
  `backend-client/src/client.rs:226-245`、`backend-client/src/client.rs:463-480`、
  `backend-client/src/client.rs:642-646`。最终线序仍由 wire 确认。
- **实测**：正式 k80 Campaign 的 A12 取得三种 GET 与安全 consume；12 份冻结证据的
  `wham-get-paths` 断言通过；0.149.1 HTTP Main 又取得 `settings/user`，其余机器事实沿用已批准
  0.147 Campaign 验收回执与证据归档。
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
- **源码**：[L1] `core/src/compact_remote_v2_attempt.rs:77`、
  `model-provider/src/provider.rs:69-77`、
  `features/src/lib.rs:1529-1532`、`core/src/tasks/compact.rs:41-50`。
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
  `ext/image-generation/src/tool.rs:412-469`。
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
- **源码**：[L1] `analytics/src/facts.rs:404-417`、
  `core/src/tasks/compact.rs:34-65`、
  `core/src/compact_token_budget.rs:21-92`、
  `core/src/session/turn.rs:1013-1242`、
  `core/src/compact_model_fallback.rs:30-40`、
  `core/src/compact_remote_v2_attempt.rs:77`。
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

当前 active strict wire 是 Codex CLI 0.151.0；自动同步只更新候选值，active ReleaseCatalog 只能经证据验收后显式发布。

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

## 3.2 Codex 0.151.0 active 画像与发布执行契约

active／previous 画像均以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排：

| mode | 版本与画像摘要 | 端点闭集 | 用途 |
|---|---|---|---|
| active | 0.151.0；`dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39` | 16 个静态端点（含 `wham_settings_user`）+ 1 个 ReturnedURL 动态端点 | 生产默认 |
| previous | 0.149.1；`8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3` | 16 个静态端点（含 `wham_settings_user`）+ 1 个 ReturnedURL 动态端点 | 受控回滚和历史复算 |

当前 Active 的官方目标身份为 tag `rust-v0.151.0`（commit
`78c290807ce710180111df227df3b7a4fe845452`）、`aarch64-unknown-linux-musl` 包和 ARM64 二进制
SHA-256 `56f026015ccc3ebc12895282200d89c216892bf6fa15fa7f228e6e0c6ad6ce76`。原始证据源为 Campaign
`c0151-formal-20260831t0220z-r8` 的 attempt `20260831T022126Z-901dd6612631b7bf`；29 个 Job、权限、
秘密扫描、环境恢复及 `172.30.0.10／172.25.0.3 → 179.255.100.158` 出口门禁均已封存。后续分类纠正、
验收和生产激活统一由 `c0151-formal-rule-correction-20260905t0033z` 的 canonical 链承接。

当前生产镜像 ID 为 `sha256:2589b419055073fc0d9f3b0c47d3efe3e0f0f93fac798604c91355d9a9e088ae`；
canonical checkpoint 为 `00000009`，执行集合为空。机器事实分别见
[`0.147 Runtime Profile 退休收据`](egress/maintenance/CODEX_CLI_01491_TO_0151_RUNTIME_PROFILE_REMOVAL_RECEIPT.json)
和 [`0.151 终态收据`](egress/maintenance/CODEX_CLI_01491_TO_0151_TERMINAL_STATE_RECEIPT.json)。

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
| `backend/internal/service/official_egress_codex_*`、`official_client_profile_registry.go` | 0.149.1／0.151.0 不可变 Snapshot、可信 release Build 运行态投影、发布投影、端点编排、Files 与模型能力 |
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

兼容代码的完整删除条件和顺序见 Framework §5.5.2 和本手册 §5.1。

---

# 第四部分 Codex CLI 版本演进流程

Framework §5.3 是升级总操作入口并规定 `VC-0～VC-6` 顺序；本部分是 Codex 轨道的参数、证据和门禁
权威，只补充官方源码、锁定依赖、HTTP／WS／TLS 取证、Campaign 工具和 Active／Previous 发布细节。
后继版本可以复用工具和流程，但不得复用目标版本应独立取得的源码、wire 或运行证据；本部分的工具状态
不能重定义 Framework 的通用状态语义。

## VC-0～VC-6 执行导航

下表只负责导航，不重复建立另一套阶段标题。每个锚点直接落到唯一的详细执行章节；阶段输入、操作、产物、
完成标志和失败恢复均在该章节开头定义，不得从历史记录或相邻阶段拼接流程。

| 阶段 | 步骤 | 唯一详细章节 |
|---|---|---|
| VC-0 | 冻结升级输入 | [§4.0](#codex-vc-0) |
| VC-1 | 收集目标证据 | [§4.1](#codex-vc-1) |
| VC-2 | 逐规则判定差异 | [§4.2](#codex-vc-2) |
| VC-3 | 生成目标画像 | [§4.3](#codex-vc-3) |
| VC-4 | 实现固定 Candidate | [§4.4](#codex-vc-4) |
| VC-5 | 定向验证 | [§4.5](#codex-vc-5) |
| VC-6 | 交付或生产激活 | [§4.6](#codex-vc-6) |

## 第四部分公共执行约定（非独立阶段）

1. VC-0 冻结 Campaign 总计划、身份、阶段依赖、预算和原始 deadline；每个阶段或恢复批次只在前序
   checkpoint 封存后编译本批次不可变清单。不得在 VC-0 预填后续尚未产生的批准摘要、candidate／attempt
   ID、镜像 digest 或收据摘要，也不得在批次启动后补写。总计划是 Formal plan 产物，不是批次动作清单。
2. 阶段动作的唯一派发入口是 `codex_upgrade_supervisor.py campaign-run`。本部分的阶段命令块均为已冻结
   v2 清单中的 `action.command`，不是操作员可绕过监督器直接执行的入口。只有以下三个控制面命令直接
   执行：VC-0 的 `plan` 创建全新 Campaign、总计划和首批；`reuse-official-evidence` 创建全新 Campaign，
   以零请求导入已封存官方证据，并生成“全部 official Job 为 reuse”的 VC-1 no-op 批次及 checkpoint；
   `compile-vc-batch` 在前序 checkpoint 封存后只编译下一批。三者都不得放入 `campaign-run` 动作队列，
   不得延长原始 deadline 或执行阶段数据面动作。
3. 身份变化、失败恢复和 `execute／reuse` 计算统一执行 Framework §5.1.2、§5.3.2～§5.3.4；各阶段只写
   Codex 专用触发条件，不重复建立身份或恢复矩阵。
4. `validation_only` 和 `production_replacement` 都必须经过 VC-6。前者只完成只读交付出口，后者继续
   production promotion、canary、切流、实际回滚和目标恢复。

### Codex 依赖键、监督器与恢复

Framework §5.1.2 规定共享恢复语义；Codex 的结果键固定为：

```text
result_key = item_id + input_sha256 + environment_sha256 + direct_dependency_sha256
```

逐文件依赖必须登记到 `producer／evaluator／control／scenario／runtime／network／gate` 之一。正式阶段动作队列
唯一派发入口是 `tools/official_client_capture/codex_upgrade_supervisor.py campaign-run`；VC-0～VC-6 新流程使用
`codex-upgrade-campaign-run/v2`，并绑定 Campaign 总计划、批次、直接前序 checkpoint 和原始绝对 deadline。
`codex-upgrade-campaign-run/v1` 只保留给历史兼容与离线回归；`campaign-start`、`campaign-mark`、
`campaign-exec` 不得编排新 Campaign。

`campaign-run` 必须向动作注入父 `run_dir`、Campaign 身份、owner nonce 和原始 deadline；动作内的
`codex_upgrade.py` 只能附加到该父监督器，不能再创建 `CampaignLease`、`.supervisor/run-*` 或重置计时。
清单分别声明 `execute_items` 和 `reuse_items`。普通执行／恢复批次的前者为空时，必须在 reservation 前
写入 `incremental-noop`，并以 `scanned_bytes=0`、`live_request_count=0` 退出；唯一不创建 reservation 的
情况是 `reuse-official-evidence` 引导出的首个 VC-1 no-op 批次，它由导入收据和 VC-1 checkpoint 直接证明
全部 official Job 已复用且请求、扫描、执行均为零。正式上下文在取得 lease 前拒绝 `successor`、
`control-epoch`、`evaluation-transition`、`terminal-transition-preflight` 和旧写入入口；0.151 formal 的
capture、classify、profile、compare、accept、resume 及 canonical 写命令没有父上下文时同样拒绝。

仅有一种 metadata-only seal 例外：Candidate 已进入 `awaiting_receipts`，全部 Candidate Job 均为
`reused/complete`，executed／failed／pending 集合为空，且尚未生成 evidence manifest、seal draft 或
seal preview；同时变化只能属于 `control／evaluator／orchestrator`。此时 `campaign-run` 才能登记
`metadata_only_seal_repair`，不得重发请求、创建旧 evaluation transition，或承接已有失败和已开始深度扫描
的 attempt。

若来源 Ledger 已因 `permanent-stop-*` 停线，只允许在冻结 checkpoint 仍为 active、停线后唯一新增事件使
`head_sequence` 恰好加一且 `live_request_count=0` 时只读承接。历史 control epoch 还必须满足 boundary
全零、仅因预算到期进入 `stop_required` 且没有同根因失败；其他 stopped／stop_required、多个新增事件、
非零边界或 live 请求全部失败关闭。该分支必须核验来源 attempt 的 `environment/after`、`after_probe`、
`probe-manifest.json` 和五份状态快照，以不可覆盖副本写入当前 attempt；不得重新探测环境、发送请求或改写
来源文件。

旧恢复机制及 Kilo 历史事实只按
[历史审计](CODEX_CLI_CLIENT_EMULATION_HISTORY_AUDIT.md#codex-0151-historical-recovery)读取，不得成为新 Campaign
的前置条件。

### Codex 连续监督、时间账本与文档部署

VC-0 冻结 Campaign 总计划、阶段依赖、身份、预算和原始 deadline；每个阶段或恢复批次只根据前序封存
checkpoint 编译本批次清单。总计划不能预填未来的 approval SHA、Candidate／attempt ID、镜像 digest
或收据摘要；后续批次必须继承同一 Campaign ID、时间账本和 deadline。

每个批次使用独立父监督器并绑定同一 Campaign 控制链。监督器实时落盘：

- 动作开始、结束和失败事件立即追加并 `fsync`；
- 每 5 秒记录监督器和 worker 心跳；
- 每 60 秒把时间区间归类为 `planning／active／waiting`；新流程不得写 `orchestrator-idle`；
- worker 失联 20 秒、动作超时或编排器 15 秒未派发下一动作时立即停线；
- 正常停止、信号、会话断开和主机失联都必须留下可审计终态或明确缺口。

编排状态机固定为 `dispatching → executing → evaluating → terminal`。批次内部按冻结队列连续执行，禁止
人工补派或用 `post-action-idle` 保持心跳；阶段完成后以封存输出编译下一批次。每阶段立即记录起止时间、
耗时、execute／reuse、失败项、live 请求、扫描次数与字节及下一动作；出现无法分类的分钟即
`audit-incomplete`，禁止部署。

除 VC-0 已自动生成的首个 VC-1 批次外，每次交接先由操作员审核下一阶段的
`codex-upgrade-vc-action-plan/v1`，再直接运行：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py compile-vc-batch \
  --campaign-dir /绝对路径/campaign \
  --phase <VC-2...VC-6> \
  --sequence <全局连续序号，从 2 开始> \
  --predecessor-checkpoint /绝对路径/campaign/control/vc/<前序>-checkpoint.json \
  --action-plan /绝对路径/action-plan.json

python3 tools/official_client_capture/codex_upgrade_supervisor.py campaign-run \
  --state-dir /绝对路径/本批-supervisor \
  --manifest /绝对路径/campaign/control/vc/run-manifests/<序号>-<阶段>.json
~~~

`compile-vc-batch` 只接受规范直接前序 checkpoint，且本阶段尚未存在 checkpoint；不允许跳号、
延长 deadline、重编已封存批次或由 `campaign-run` 内部调用。动作返回
`awaiting_receipts`、`approval_required` 时，父 `campaign-run` 将其视为本批合法停靠点并正常封存；
同一命令绕过父监督器直接运行仍以退出码 2 提示未到终态。

Codex 工具运行时读取的 Framework 和客户端指南属于受管依赖。部署清单必须登记规范路径和摘要，与工具树
在同一可回滚事务中切换，并从生产运行根完成重放测试；具体生产步骤见 §4.6。

<a id="codex-vc-0"></a>
## 4.0 VC-0 冻结升级输入

- **输入**：当前 Active／Previous、目标版本及官方产物、账号与 API Key 身份、ARM64 环境、用途、预算和回退点。
- **操作与工具**：完成 DOC-PRE，执行 `preflight_only` plan、P0 离线门禁和 `campaign-run` 分批演练；随后以直接控制面命令 `plan` 冻结 Formal Campaign 总计划并编译首批。
- **产物**：DOC-PRE／P0 收据、时间账本、工具与环境摘要、Campaign 总计划和首个 Formal 批次清单。
- **完成标志**：工具阻断为零、网络与目录有效、live 请求为零、回退点可用。
- **失败恢复**：正式 Campaign 前停线，工具缺口拆成独立变更；修复后只重跑 VC-0。

VC-0 只回答“本次升级是否具备安全开工条件”。本阶段不收集目标 wire、不修改画像或实现、不创建
candidate，也不改生产 selector；这些工作分别从 VC-1、VC-3、VC-4 和 VC-6 开始。

### 4.0.1 DOC-PRE 与 P0：冻结清单和执行边界

通用冻结、恢复语义、环境与数据安全和时间控制分别以 Framework §5.3.1、§5.1.2、§5.1.4～§5.1.5
和 §5.3.5 为准；Codex 监督器规则见本部分公共执行约定，固定 ARM64 坐标见 §4.0.3。本节只列 Codex
Campaign 在 P0 中必须落盘的具体输入。

DOC-PRE 先登记并审核本次 maintenance transition；合并后从干净 HEAD 执行 P0。路径级
`from_sha256` 必须承接上一份机器 transition 的 `to_sha256`，`base_commit` 不能代替这条摘要链。
`UpgradeTimingLedger` 从 DOC-PRE 首项开始，不能在正式取证时重新起算。

| 冻结面 | Codex P0 必须记录 |
|---|---|
| 目标与基线 | baseline／target 版本、官方 tag／commit、源码、锁定依赖、平台、架构、feature、官方产物和 SHA-256；当前 Active／Previous 的 Release、Profile、selector、镜像及回退收据 |
| Campaign 身份 | `campaign_mode`、`campaign_purpose`、Campaign ID、证据根、目标场景、规则清单和受管工具版本 |
| 账号与模型 | 明确的 Codex 账号和 API Key 数据库 ID、权限、额度、目标模型及模型可见性；身份变化必须重新冻结 |
| 执行环境 | §4.0.3 的 ARM64 平台、固定网络、项目根、数据根、容器根、Compose、挂载、运行镜像和工具链 |
| 控制策略 | 全局／阶段墙钟预算、同根因重试上限、资源水位、回退点、`reuse／recapture` 决定及 `execute／reuse` 闭集 |
| 工具身份 | 采集、relay、脱敏、分类、断言、finalizer、环境快照、监督器、Schema、源码树和测试树摘要 |

P0 使用新的持久目录执行：

```bash
python3 tools/official_client_capture/codex_upgrade.py plan \
  --campaign-mode preflight_only \
  --campaign-purpose <validation_only|production_replacement> \
  ...
```

`preflight_only` 目录只允许计划、状态查询和离线演练；不得发送真实请求、使用
`--acknowledge-live-requests`、创建 Formal attempt、修改 Active／Previous 或写入历史证据。P0 通过后，
必须换一个尚不存在的目录执行同一用途的 `plan --campaign-mode formal`，禁止把预检目录直接续作。

`plan --rule-manifest` 绑定 baseline 的 `codex_upgrade_rules_<baseline>.json`；目标版本的
`candidate_rule_expectations_<target>.json` 只用于候选断言预检，不能代替 baseline 规则清单。

最低离线验证固定为：

1. 在干净 HEAD 运行 `make test-capture-tools` 和 `make check-egress-spec`，记录命令、摘要、退出码及
   passed／failed／approved_skip／unexpected_skip；正式结果要求 `unexpected_skip=0`。
2. 核对受管工具树、ARM64 执行副本、测试树和 finalizer 同源；目标版本、场景或证据标签中的旧版本硬编码
   必须被门禁识别。
3. 用最小历史夹具验证 `campaign-run`、Profile／Catalog 生成、candidate、seal、compare、accept 和部署
   预演；另用至少两组差异夹具证明批次继承原始 deadline、运行坐标覆盖拒绝账号字段、post-promotion
   门禁随批准规则集合变化而变化。P0 只证明工具能力，不生成目标版本证据。
4. 按 §4.0.3 完成 ARM64 环境检查、实规模成本检查和全部冻结 Job 的离线 rehearsal。
5. 冻结 Campaign 总计划，并从当前已知输入编译首个不可变 Formal 批次，明确本批次的
   `execute_items`、`reuse_items`、输入摘要和直接依赖；后续批次只能从前序封存输出生成。执行集合为空时
   必须生成 `incremental-noop`。

所有 P0 输出都必须携带输入、工具摘要、原始错误、退出码和临时资产 inventory。工具功能缺口必须在
Formal Campaign 前拆成独立变更并重新执行 P0；不得在正式 Campaign 上边运行边修工具。

### 4.0.2 Codex 专用身份、用途与检查点

Campaign、ApprovalFact、candidate、attempt 和 evaluator run 的通用身份边界只以 Framework §3.3、
§5.3.4 为准。Codex 轨道补充三项约束：五份批准清单及其联合摘要属于 ApprovalFact 身份；
`candidate-runtime-override` 只能在首个 attempt 前改变已登记的容器名、Codex 二进制路径或 Compose 坐标；
该接口若接受账号、API Key、权限、模型可见性、源码、镜像、Profile 或证据根字段，必须在 P0 记为工具阻断，
不得依靠操作员“不传这些参数”规避。

每个 Campaign 和 candidate 都必须在执行前声明同一用途：

| 用途 | 终点 |
|---|---|
| `validation_only` | VC-5 通过后保持 `accepted_not_activated`，进入 VC-6 只读交付出口，不得宣称已上线 |
| `production_replacement` | VC-5 通过后继续 VC-6，直至 canary、切流、回滚和目标恢复全部有收据 |

用途、账号、模型能力或证据语义不能在验收后追认。坐标覆盖不能用来承接身份漂移。

Codex Campaign 的内部检查点仅用于恢复和重放：

```text
planned → official_sealed → profile_approved → candidate_sealed → compared → ready
```

`ready` 不代表生产 Active。生产状态由 §4.6 的 activation、运行镜像和 selector 事实独立证明；历史
Campaign／candidate／attempt／收据只读，不得覆盖。旧 `successor／control-epoch／evaluation-transition`
仅按附录 A 的审计说明读取，新 Campaign 禁止执行。

### 4.0.3 ARM64 参数与离线预演

Framework §5.1.4～§5.1.5 只规定环境冻结、路径安全和数据治理的共享合同；本节是 Codex 取证、测试、
构建和部署环境的参数权威。上述动作统一在 ARM64 完成，固定网络与 `capture-cli` 坐标如下：

| 坐标 | 固定值 |
|---|---|
| Sub2API 容器 IP | `172.25.0.3` |
| `capture-cli` 容器 IP | `172.30.0.10` |
| 公网出口 | `179.255.100.158`，经 DMIT |
| `wg1` MTU | `1420`；同时核对宿主持久值、运行值和对端值 |
| 宿主项目根 | `/root/docker/capture-cli` |
| 宿主数据根 | `/root/docker/capture-cli/data`，权限 `0700`，版本控制忽略 |
| 容器运行根 | `/root/oauth-capture` |
| Compose | `/root/docker/capture-cli/docker-compose.yml`，从项目根以冻结项目名执行 |
| 历史宿主兼容根 | `/root/oauth-capture`，仅允许只读重放，禁止产生新版本数据 |

宿主的 state、runtime、work、evidence、control、audit、staging 和 archive 全部位于数据根；不得直接在
`/root` 创建源码树、bundle、patch、worktree、恢复树或抓包目录。后续宿主命令统一先声明：

```bash
export CAPTURE_HOST_PROJECT_ROOT=/root/docker/capture-cli
export CAPTURE_HOST_DATA_ROOT="$CAPTURE_HOST_PROJECT_ROOT/data"
export CAPTURE_CONTAINER_ROOT=/root/oauth-capture
```

P0 必须从 Compose 渲染结果和 `docker inspect capture-cli` 同时验证挂载、镜像、固定 IP、默认路由、
DMIT 公网出口和 `wg1` MTU；脚本不得修改网络、NAT／iptables、WireGuard 或容器地址来迁就测试。

ARM64 宿主与容器必须使用同一冻结 Go 工具链，构建设置 `GOPROXY=off`、`GOFLAGS=-mod=readonly`。
缺少前端依赖时，只能通过绝对路径 `CAPTURE_TYPESCRIPT_MODULE` 使用 Makefile 已锁定摘要的只读
TypeScript；禁止临时安装依赖、复制 `node_modules` 或切换工具链。`docker build --network=none`
不能证明基础镜像已离线，全部基础镜像 digest 和层仍须预先冻结。

| P0 检查 | 必须证明 |
|---|---|
| 目录与挂载 | build context、env file、业务 bind source 和全部写入均在登记根内；`/root` 第一层无本轮污染 |
| 网络与 TLS | `sub2apiplus` 与 `capture-cli` 使用本节固定地址并经 DMIT 同一出口；DNS、证书和 MTU 可复算 |
| 运行隔离 | 每个 attempt 使用独立、权限为 `0700` 的 `HOME／CODEX_HOME`，不读取其他账号或前序缓存 |
| 模型目录 | Main／Lite 仅各执行一次 initialize-only；不得用 thread、turn、Responses 或 WS 请求预热 |
| 同源依赖 | 工具、测试、candidate、finalizer、目标架构依赖和实际执行副本摘要一致 |
| 环境恢复 | 端口、hosts、CA、模型映射、relay、容器和托管字段具备 before／after 恢复语义 |
| 全部 Job | 展开 target 的完整 official／candidate Job 集，在真实 `capture-cli` 内验证命令、路径、环境变量、依赖和证据标签，不发送官方请求 |
| 成本模型 | 用不小于最大单一 manifest 的夹具证明 preview 只扫描一次，其余状态／批准／复用操作读取原始证据 0 字节 |

Job rehearsal 使用独立、权限为 `0700` 的证据根：

```bash
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt collect \
  --campaign-dir "$PREFLIGHT_CAMPAIGN" --evidence-root "$JOB_REHEARSAL_ROOT" --output facts.json
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt finalize \
  --evidence-root "$JOB_REHEARSAL_ROOT" --facts facts.json --output receipt.json
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt replay \
  --evidence-root "$JOB_REHEARSAL_ROOT" --receipt receipt.json
```

`collect` 只检查路径、依赖、语法、二进制、bubblewrap 和 zstd，不执行 Job、不发送请求。任一 Job
失败都必须先能形成失败收据并独立重放；失败、缺项、环境漂移或目标场景／工具摘要不一致时禁止创建
Formal Campaign。Formal `plan` 必须绑定上述 rehearsal receipt，并再次独立重放。

创建运行目录前，ARM64 根文件系统须同时满足使用率低于 70% 且可用空间不少于 30 GiB。达到水位后只能
按 manifest 清理未被收据引用的可再生缓存、worktree、镜像层和 staging，禁止删除证据或无界扫描。

### 4.0.4 退出条件与阻断处置

VC-0 的机器退出条件固定为：

```text
P0 收据通过
∧ 工具阻断为零
∧ campaign-run 分批执行、原始 deadline 承接与全部冻结 Job 的离线演练通过
∧ 网络、目录、资源水位和回退点有效
∧ live_request_count = 0
⇒ 创建新的 Formal Campaign，封存总计划并在 60 秒内启动 VC-1 首批动作
```

工具、Schema、场景、依赖、账号、权限、模型可见性、官方产物、环境、成本、磁盘或时间预算任一不满足，
均不得创建 Formal Campaign；按 Framework §5.3.4 给出最后 checkpoint、根因和唯一下一动作。全部通过后
封存 P0 收据、Campaign 总计划和首批动作清单，换新目录创建 Formal Campaign 并立即进入 VC-1。

“当前工具是否就绪”只能由本次 P0 收据、Job rehearsal 和门禁输出证明，不再在长期手册中维护容易过期的
状态表。除冻结身份实际漂移外，不得重复已经通过的离线演练，也不得以准备工作为由停留在 VC-0。

<a id="codex-vc-1"></a>
## 4.1 VC-1 收集目标证据

- **输入**：VC-0 收据、目标源码／二进制／依赖、target 场景清单和正式 Campaign 身份。
- **操作与工具**：新证据路径由 `campaign-run` 完成源码分析、必要的官方抓包、assertion bundle 和证据封存；已有可信官方证据则直接执行 `reuse-official-evidence` 完成零请求导入。
- **产物**：目标源码事实、P／R／J／M、`DiscoveryInventory`、官方证据包和 `official_sealed` checkpoint。
- **完成标志**：目标身份完整，目标发现无截断，逐项证据可定位、可解析且已封存。
- **失败恢复**：只为缺失事实定向补证；已经可信封存的官方请求不得重发。

VC-1 只回答“目标版本实际会产生什么行为”，不判定相对基线如何变化，也不生成目标画像。

### 4.1.1 证据来源与复用判定

| 情况 | 动作 |
|---|---|
| 同版本、同产物／平台／账号权限／模型可见性且语义未变的可信证据 | 以 `reuse-official-evidence` 只读导入 `official_sealed` |
| 可信证据只缺少某项目标事实 | 只对该事实及直接依赖场景定向取证 |
| target、官方产物、平台、账号身份／权限／模型可见性或证据语义变化 | 返回 VC-0 建立新 Campaign，重新取得受影响证据 |

导入命令必须为新 Campaign 重建总计划和 VC-0 checkpoint，将全部 official Job 登记为 `reuse_items`，
生成无动作的首个 VC-1 no-op 批次，并以 `executed_job_count=0`、`scanned_bytes=0`、
`live_request_count=0` 封存 VC-1 checkpoint。它不得复制或改写原始证据，也不得承接旧分类；分类仍在
VC-2 重新审核。工具、报告或 candidate 变化本身不能成为重发官方请求的理由。

官方 Release 下载前，由 `codex_upgrade_official_asset_receipt.py` 预连接 metadata 中的 CDN IPv4，
冻结成功地址、证书、asset 大小和 SHA-256；收据离线重放通过后只能从该地址下载。地址全部失败、
metadata 漂移或摘要不符时停线，不得改路由或使用未登记镜像站。

目标二进制绑定绝对路径、版本和摘要，源码绑定 tag／commit、Cargo.lock 和锁定依赖。Main、Lite 等
互斥条件使用独立 track、Job 和 evidence root；模型、账号、平台、代理或 TLS 条件不同的样本不可直接比较。

### 4.1.2 源码分析、抓包与封存

源码分析从生产入口追踪到认证、Client、TLS、传输、Header、Body、端点和跨请求状态；无截断记录
调用链、条件、平台／feature、固定／随机属性、可观测边界、新增 sink／host／path，以及对应的源码锚点、
运行场景和 P／R／J／M 引用。

| 顺序 | operation | 产物或结果 |
|---:|---|---|
| 1 | 重放 VC-0 Formal plan 并执行首批 | 确认 target source、source diff、baseline surface 和 target 场景执行合同未漂移 |
| 2 | 源码与依赖分析 | 形成目标发现和待验证事实 |
| 3 | `capture-official run` | 仅为目标事实采集 HTTP、WS、TLS、状态和错误分支 |
| 4 | `SIDE=official prepare_assertion_bundle.sh` | 从冻结 Job 根生成 capture manifest |
| 5 | `capture-official seal` | 校验恢复、权限、秘密扫描、inventory 和 finalizer，写入 `official_sealed` |

目标 CLI Job 只能来自 Formal 冻结的 target 场景。assertion bundle 必须绑定 Campaign、attempt 和
目标版本证据标签；标签缺失或残留旧版本时返回 VC-0 修复，不能现场手写 manifest。Job 结束后、生成
bundle 前，将证据目录／文件权限收口为 `0700／0600`；任何身份或执行合同漂移都不得 seal。

### 4.1.3 目标事实整理与退出

人工复核源码与 wire 闭环，形成包含行为、条件、可观测边界、证据引用和建议场景的目标事实清单，供
VC-2 更新规则正文并判定差异。VC-1 不执行 `classify`，也不写入任何迁移结论。

```text
目标身份完整 ∧ DiscoveryInventory 无截断 ∧ 证据缺口已明确
∧ 恢复、安全、inventory、finalizer 全部通过 ∧ Campaign = official_sealed
⇒ 进入 VC-2
```

VC-2 若发现分类仍缺事实，只返回 VC-1 补采该项；其他已封存 Job 和官方请求继续只读复用。

<a id="codex-vc-2"></a>
## 4.2 VC-2 逐规则判定差异

- **输入**：VC-1 checkpoint、封存的目标发现、当前基线规则、Active 画像和两版本可比证据。
- **操作与工具**：生成分类草案，逐规则判定 `inherit/change/condition_change/add/delete`，定稿迁移、原子断言、场景和目标画像草案，再通过 `classify` 双调用封存联合批准。
- **产物**：五份已批准清单、`classification/result.json`、`profile-derivation.json`、post-promotion 门禁需求和 VC-2 checkpoint。
- **完成标志**：所有发现具有唯一处置，五份清单联合摘要已批准，`blocked`和其他未决项均为零。
- **失败恢复**：事实不足返回 VC-1 定向补证；不得提前修改画像或实现。

### 4.2.1 执行步骤

1. 由 `campaign-run` 派发不带批准清单和批准摘要的 `classify`，在
   `classification/draft/<revision>/` 生成五份待审核草案。
2. 将草案视为编辑起点，不得视为迁移结论：工具会先复制基线规则，把既有规则暂填为 `inherit`、
   新 discovery 暂填为 `blocked`，并从基线生成断言画像占位内容。
3. 对照目标源码、wire 证据和基线行为，逐规则更新 `target-rules.json` 与
   `rule-migration.json`；先确认触发条件和证据可比，再选择分类。
4. 逐项处置 `source` 与 `dynamic` discovery，为每项填写唯一分类、目标规则、证据引用和理由。
5. 为每条受影响规则定稿可独立验证的原子断言；继承规则保持原断言语义，后续只重放既有收据。
6. 从当前 Active Snapshot 派生目标 Snapshot，用 `prepare-profile` 生成目标 `profile.json`，
   完成 `scenarios.json`、`profile.json` 和 `assertion-profile.json` 的交叉绑定。这些文件在此处
   只是 VC-2 联合判定的必需输入；尚未生成候选 RuntimeCatalog。
7. 将 `rule-migration.json` 和 `profile.json` 置为 `approved`，使用五份清单、当前
   Active 画像和画像补丁执行第一次 `classify`。人工核对返回的 `joint_manifest_sha256`
   后，以完全相同输入追加 `--approve-manifest-sha256` 再执行一次。批准调用自动
   封存五份清单、画像派生收据、动态门禁需求和 VC-2 checkpoint。

对应动作体为：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py prepare-profile \
  --campaign-dir /绝对路径/campaign \
  --snapshot /绝对路径/target-snapshot.json \
  --profile-id <target-profile-id> \
  --output /绝对路径/profile.json

python3 tools/official_client_capture/codex_upgrade.py classify \
  --campaign-dir /绝对路径/campaign \
  --target-rule-manifest /绝对路径/target-rules.json \
  --migration-manifest /绝对路径/rule-migration.json \
  --scenario-manifest /绝对路径/scenarios.json \
  --profile-manifest /绝对路径/profile.json \
  --assertion-profile-manifest /绝对路径/assertion-profile.json \
  --active-profile /绝对路径/production-active-profile.json \
  --profile-patch-manifest /绝对路径/profile-rule-patches.json

# 人工核对第一次返回的联合摘要后，原命令追加：
# --approve-manifest-sha256 <joint_manifest_sha256>
~~~

`prepare-profile --output` 必须位于 Campaign 外、尚不存在，其父目录权限为 `0700`。
缺少任一画像派生输入时，工具必须在读取官方证据前失败。

### 4.2.2 分类口径与阶段边界

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

`rule-migration/v1` 中 `entries[].classification` 表示规则迁移结论；
`discovery_classifications[].classification` 只表示本轮扫描形态的终态处置。后者标为 `change`
不等于对应规则必然变化，规则结论仍以 `entries` 为准；不得从 discovery 数量生成新规则。

VC-2 的机器终点是第二次 `classify` 成功写入 `classification/result.json` 和
`control/vc/vc-2-checkpoint.json`。草案生成、`prepare-profile` 或第一次批准预览都不是阶段完成；
五份清单任一摘要漂移，就不得编译 VC-3 批次。

<a id="codex-vc-3"></a>
## 4.3 VC-3 生成目标画像

- **输入**：VC-2 checkpoint、五份已批准清单、画像派生收据和 post-promotion 门禁需求。
- **操作与工具**：重放联合批准与画像派生绑定，用 `stage-profile` 把完整目标 Snapshot 编译为不切换 Active 的候选 RuntimeCatalog。
- **产物**：候选 Snapshot、ReleaseGraph、RuntimeCatalog、`catalog-stage-receipt.json` 和 VC-3 checkpoint。
- **完成标志**：画像差异和门禁需求全部可追溯，stage receipt 可复算，生产 selector 未改变。
- **失败恢复**：批准内容或派生绑定错误返回 VC-2；仅暂存环境或输出失败时留在 VC-3，保持批准内容不变后重试。

### 4.3.1 执行步骤

1. 重放 VC-2 checkpoint、`classification/result.json`、五份批准清单、
   `profile-derivation.json` 和 post-promotion 门禁需求；要求 `blocked=0`、联合摘要一致且
   `profile_diff_paths ⊆ version_identity_paths ∪ rule_field_paths[affected_rules]`。
2. 复用 VC-0 冻结的 Go 环境生成候选 Catalog：

   ~~~bash
   python3 tools/official_client_capture/codex_upgrade.py stage-profile \
     --campaign-dir /绝对路径/campaign \
     --output /绝对路径/candidate-catalog
   ~~~

   `--output` 的父目录须预先设为 `0700`，输出本身必须位于 Campaign 外且尚不存在。
   命令成功时自动生成 VC-3 checkpoint；不得再单独手写阶段完成事件。

| 清单 | 审核内容 |
|---|---|
| `target-rules.json` | 重放 VC-2 目标规则全集与第二部分的绑定 |
| `rule-migration.json` | 重放 VC-2 迁移决定、discovery 分类和证据引用 |
| `scenarios.json` | 重放官方／candidate 场景、规则覆盖和目标画像绑定 |
| `profile.json` | 重放 Active 派生的完整目标 Snapshot、profile ID／digest 和补丁映射 |
| `assertion-profile.json` | 重放原子断言、场景选择、画像和第二部分摘要绑定 |

规则、场景、画像、断言和端点集合必须跨清单一致；所有 discovery 具有唯一分类和证据引用，
`rule-migration.json` 必须为 `approved`。

### 4.3.2 暂存收据与退出条件

`stage-profile` 从五份批准清单生成包含目标 Snapshot、ReleaseGraph 和 SnapshotCatalog 的候选
RuntimeCatalog，不修改仓库或生产 selector。收据必须绑定 Campaign、联合摘要、目标版本、profile digest
和 post-promotion 门禁需求摘要，且 inventory 精确覆盖输出目录、逐文件摘要和大小可复算。

```text
Campaign = profile_approved ∧ blocked = 0 ∧ post-promotion 门禁需求闭合
∧ catalog-stage-receipt 可复算
∧ active_unchanged = true ∧ production_selector_changed = false
∧ candidate_release_mode = previous
⇒ 进入 VC-4
```

VC-3 只生成未入库的候选 Catalog；纳入同源 candidate 树并构建制品属于 VC-4。

<a id="codex-vc-4"></a>
## 4.4 VC-4 实现固定 Candidate

- **输入**：ApprovalFact、候选 Catalog、`affected_rules`、post-promotion 门禁需求及其直接依赖。
- **操作与工具**：只实现批准闭集，把画像、测试和代码纳入同一最终源码树，绑定门禁需求后再从该树构建目标架构制品。
- **产物**：候选源码树、版本专属测试资产、post-promotion 门禁执行计划、source transition、构建收据和完整 Candidate 身份元组。
- **完成标志**：实现闭集通过，源码、构建、镜像和 Profile 身份可复算，生产 Active 未改变且尚未发起候选请求。
- **失败恢复**：固定后的源码、构建、镜像或 Profile 发生变化时建立新 candidate，只重做受影响闭集。

### 4.4.1 入库与实现边界

将 VC-3 暂存的 Snapshot、ReleaseGraph 和 RuntimeCatalog 纳入 candidate 源码树，只实现
`affected_rules` 及其直接依赖。每项代码、测试和画像变化都必须能回指 `target-rules.json` 与
`rule-migration.json`；发现批准闭集外的新行为时停止实现，返回 VC-2 分类并从 VC-3 重新生成画像。
入库后必须同时满足：

1. 目标 Snapshot／Release 只追加新节点，不改写旧节点的路径、内容或摘要；
2. 批准的 endpoint、Header、Body、TLS、连接、状态和路由规则均由画像或明确批准的最小实现表达；
3. 新端点在相关 mode 同时具备 binding、Bundle resolver、route catalog 和 release proof；既有 route
   继续受 MigrationReceipt 约束。版本新增 route 在本阶段准备 wire fixture、execution verification
   及生产 canary 的绑定目标；实际 canary acceptance 必须由 VC-6 实测产生，不得在 VC-4 伪造占位收据；
4. 生产 Active 和默认 selector 不变，目标 Release 仅作为 `previous` 候选供 VC-5 显式选择；
5. 在途 invocation 保持原 Bundle，新 invocation 才解析新 selector，fallback 和连接池不得跨 Bundle；
6. 同批纳入 `candidate_test_fact_map_<version>.json`、
   `candidate_rule_expectations_<version>.json` 和 `candidate_test_trace.py`，并更新 trace 中的默认路径、
   `FROZEN_MAPPING_SHA256` 与 `FROZEN_PROFILE_SHA256`；版本或摘要不一致时禁止构建；
7. 在同源树内建立 `codex-post-promotion-gate-mapping/v2` 映射，把 VC-3 的每项
   post-promotion 门禁需求绑定到唯一 `test_id`、工作目录和字面命令；映射根必须绑定本轮
   `requirements_sha256`，每个 gate 还必须携带对应需求对象的 `requirement_sha256`。公共门禁与
   affected／inherited 划分必须逐项闭合，禁止夹入未批准规则或历史固定编号；
8. 实现测试及受影响闭集测试通过；继承规则只重放既有收据，不借机扩大为全量改造。

版本新增 route 可在确实不含该端点的单个 Release 中零匹配，但 Compiler 端点集合必须等于
Active／Previous 并集，且每条 runtime-bindable route 在并集中至少有一个 binding。只有现有
Snapshot、Plan、Bundle 或 Executor 无法表达新机制时，才最小修改共享层并专项复验两个 mode。

先用映射生成同源门禁执行计划，再把映射、计划、Catalog、实现和测试一并提交：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py plan-candidate-gates \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --candidate-source /绝对路径/candidate-source \
  --mapping /绝对路径/candidate-source/path/gate-mapping.json \
  --output /绝对路径/candidate-source/path/gate-plan.json
~~~

最终源码树必须是无未提交改动的 Git commit。随后在源码树外生成后继 source transition，并执行
`make check-egress-spec`：

~~~bash
python3 -m tools.upstream_merge freeze-successor-generate \
  --repository /绝对路径/candidate-source \
  --before <base-commit> \
  --after <final-commit> \
  --tag codex-0-154-candidate \
  --output /源码树外/source-transition.json \
  --reason "Codex 0.154.0 Candidate 同源实现"
~~~

Candidate 源码树与 Campaign 目录必须彼此独立；source transition 也不得放入源码树，否则会形成
自引用摘要。源码、测试、文档或 Catalog 后续再变化，原 transition 即失效，必须重建；不得累积多个
未登记提交后再进入 ARM64 门禁。transition 必须满足
`result=passed_local_evidence_successor`、`required_manual_actions=[]`、`unregistered_paths=[]`、
`deleted_frozen_paths=[]`，且 `verification` 明确包含 `make check-egress-spec`。其 `safety` 还必须同时明确
`deployment_performed=false`、`live_account_used=false`、`official_egress_profile_changed=false`、
`production_config_changed=false` 和 `wire_or_persona_selection_changed=false`；任一条件不满足都不得封存 Candidate。

### 4.4.2 同源构建

以 4.4.1 确定的同一最终源码树为唯一构建输入，依次生成前端产物、运行资源、ARM64 二进制和不可变镜像；
禁止分别从不同提交或未登记工作树拼装。构建收据至少记录 Git commit、源码树摘要、build ID、部署版本、
二进制 SHA-256、目标架构、构建参数、image ID、OCI manifest digest、Profile ID／digest，以及各产物
的来源关系，并绑定 post-promotion 门禁需求与执行计划摘要。镜像交接使用
`registry/repository@sha256:<manifest-digest>`，不能只写可变 tag。

Go 必须离线编译；ARM64 缺少前端依赖时，只允许通过 `capture-cli` 的固定 DMIT 出口取得依赖，然后将
前端产物、ARM64 二进制和运行资源叠加到冻结的 ARM64 基础镜像。证据机和低资源生产机不承担 Go／Node
编译，也不得用现场重编译产物替代构建收据中的制品。

在最终干净 commit 上运行实现闭集和 `make check-egress-spec`，将两类结果分别作为
`implementation_tests` 和 `check_egress_spec` 证据，生成 `kind=implementation_tests`的统一收据。
收据主体必须绑定本轮 Campaign、candidate、Git commit、源码树摘要和目标架构，且
affected gate 集合必须与 VC-3 需求精确相等：

~~~bash
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize \
  --evidence-root /绝对路径/implementation-tests \
  --facts implementation-tests.facts.json \
  --output implementation-tests.receipt.json

python3 tools/official_client_capture/codex_upgrade_vc_receipt.py replay \
  --evidence-root /绝对路径/implementation-tests \
  --receipt implementation-tests.receipt.json
~~~

构建完成后、创建任何 Candidate attempt 之前，执行：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py record-candidate-build \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --candidate-purpose <validation_only|production_replacement> \
  --candidate-source /绝对路径/candidate-source \
  --candidate-binary /绝对路径/candidate-binary \
  --runtime-image <registry/repository@sha256:manifest-digest> \
  --candidate-image-id <sha256:image-id> \
  --build-id <build-id> \
  --deployed-version <deployed-version> \
  --target-architecture <os/architecture> \
  --build-parameters /绝对路径/build-parameters.json \
  --catalog-stage-dir /绝对路径/candidate-source/path/candidate-catalog \
  --source-transition /源码树外/source-transition.json \
  --gate-plan /绝对路径/candidate-source/path/gate-plan.json \
  --implementation-test-root /绝对路径/implementation-tests \
  --implementation-test-receipt /绝对路径/implementation-tests/implementation-tests.receipt.json
~~~

工具复算干净 Git commit、源码树、二进制、不可变镜像、Catalog、画像派生、动态需求、执行计划和
source transition，并独立重放实现测试收据及其两份证据后，只写一次生成
`<campaign>/candidates/<candidate-id>/build-receipt.json`；失败时不得创建 attempt 或发送请求。
成功时同时封存 `control/vc/vc-4-checkpoint.json`。

### 4.4.3 Candidate 身份冻结与 VC-5 交接

交给 VC-5 的 Candidate 身份必须一次绑定完整，不能只记录镜像 tag 或 build ID：

| 身份层 | 必须冻结的字段 |
|---|---|
| Candidate | `candidate_id`、`candidate_purpose` |
| 源码 | `git_commit`、`source_tree_sha256` |
| 构建 | `build_id`、`deployed_version`、二进制 SHA-256、架构和构建参数 |
| 镜像 | `image_reference`、`image_id`、`image_digest`（OCI manifest digest） |
| 画像 | `profile_id`、`profile_digest` |

任一字段变化都表示原 Candidate 已失去同一性，必须建立新 candidate；不得通过改写收据维持旧 ID。

仅容器名、Codex 二进制路径或 Compose 坐标与 Campaign 冻结值不同时，才允许在该 candidate 首个
attempt 前登记一份写一次的运行坐标覆盖收据；`run` 与 `seal` 必须从同一收据读取生效值，磁盘清单与
`campaign.sha256` 保持不变。覆盖前必须证明二进制摘要、账号与 API Key 身份、权限、模型可见性和环境
语义均未变化。账号、权限、模型可见性、目标源码树、官方包、运行镜像、模型或证据根变化属于身份漂移，
按 Framework §5.3.4 处理。该命令只允许坐标字段白名单；如果工具接受 `codex_account_id`、`api_key_id`
或其他身份字段，VC-0 必须已经失败关闭。candidate 一旦已有 attempt 或已封存，也不得再登记坐标覆盖。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py candidate-runtime-override \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --reason "切换到已登记的服务容器坐标" \
  --set service_container=<容器名>
~~~

VC-4 只在构建收据中冻结最终制品，不调用“只落盘身份”的写入命令；VC-5 首次执行
`capture-candidate run` 时再复算上述身份并原子写入机器状态。VC-4 不得提前创建 attempt、reservation、
`attempt_id` 或 `run_nonce`，也不得发送候选真实请求。

退出条件固定为：

```text
实现闭集通过
∧ source/tree/build/image/profile 身份全部绑定
∧ post-promotion 门禁执行计划、stage receipt、source transition 与构建收据可复算
∧ production Active 和默认 selector 未改变
∧ attempt、reservation 和候选真实请求均未创建
⇒ 进入 VC-5
```

<a id="codex-vc-5"></a>
## 4.5 VC-5 定向验证

- **输入**：固定 candidate、官方证据包、五份批准清单、执行／复用闭集和 VC-4 构建收据。
- **操作与工具**：运行定向 Candidate Job 和 Kilo 双入口，封存证据，执行离线比较、逐规则断言、外部门禁、
  `accept`，并为生产替换建立 canonical 交接。
- **产物**：`candidate_sealed`、comparison、逐规则结果、外部门禁收据、AcceptanceFact、
  `vc5-completion.json`、VC-5 checkpoint 和生产用途所需的 canonical checkpoint。
- **完成标志**：受影响规则全部通过、继承规则可重放，Campaign 达到 `ready／accepted_not_activated`；
  VC-5 完成收据与 checkpoint 可重放。生产用途的 canonical 剩余集合必须精确为三个 VC-6 项。
- **失败恢复**：按 Framework §5.3.4 只执行失败、未完成或依赖变化项；继承结果只读复用。

本阶段用 VC-4 的固定制品运行目标画像，并从批准场景和真实第三方入口收集候选证据。候选按
`candidate_release_mode=previous` 显式选择目标 Release，不得借当前 Active 或客户端自报版本选择画像。

| 步骤 | 工作 | 检查点 |
|---|---|---|
| 1 | 冻结执行闭集，完成运行前检查并启动 Candidate | attempt 身份与 `run_nonce` 落盘 |
| 2 | 执行定向场景、目标模型双轨和 Kilo 双入口 | Candidate Job 与客户端事实闭合 |
| 3 | 建立客户端检查点并封存候选证据 | `candidate_sealed` |
| 4 | 只读比较官方与候选证据 | comparison `complete／offline_only` |
| 5 | 执行 affected 规则断言，重放 inherited 规则收据 | 目标规则全集通过 |
| 6 | 执行外部门禁并签发 AcceptanceFact | `ready／accepted_not_activated` |
| 7 | 按用途交接 VC-6；production replacement 另建 canonical 交接 | VC-5 完成收据与 checkpoint 已登记；生产用途只剩三个 VC-6 canonical 项 |

### 4.5.1 执行闭集、运行前检查与身份落盘

按 Framework §5.1.2 从 `affected_rule_ids`、场景依赖和最新合法 checkpoint 编译 Candidate Job 批次。
`scenarios.json` 定义完整覆盖，不代表全量重跑；继承规则和 `reused_item_ids` 只能重放来源收据。
若 `execute=[]`，须在 reservation 前写入 `incremental-noop`，保持 `live_request_count=0`、
`scanned_bytes=0` 并立即结束。

有待执行项时，机器预检必须在任何真实请求和环境修改之前完成：

- 复核不可变镜像 RepoDigest、挂载与 PID namespace、实际执行工具副本、目标 Codex 绝对路径及
  `codex-cli <target-version>`，禁止通过 `codex-capture`、`PATH` 或默认值选中旧版本；
- 复核账号、API Key、模型可见性、Live／WS／compact 开关、熔断与配额、activation 身份、采集端口、
  run-root、属主和权限，以及 §4.0.3 的固定容器 IP、DMIT 出口和 MTU；
- `candidate-frozen-aux` 在修改环境前确认隔离分组只含目标账号且已启用 Live 和图片生成；合并解析全部
  Compose 文件，必须指向同一 candidate 镜像并设置 `candidate_release_mode=previous`。拒绝相对路径、
  符号链接、其他 Compose 选项和 shell `eval`；`production_replacement` 不允许缺少 Compose 坐标；
- 固定镜像 digest，只替换应用容器并保留回滚点；运行期间不得执行 `pull`、`compose down`、`prune`，
  也不得重建数据库、Redis、网络、挂载或其他依赖服务；
- Job 的真实参数只认冻结 job definition 和 attempt `argv`；脚本默认值和外部同名环境变量不能替代它们。

运行前必须新签剩余有效期不少于 30 分钟的管理 JWT，保存为宿主机权限 `0400` 的普通文件并设置
`ADMIN_BEARER_TOKEN_FILE`；禁止复用过期 token。编排器须在创建 reservation 前检查格式、权限和
有效期，缺失或过期时立即失败。下列内容是 `campaign-run` 动作体：

~~~bash
export ADMIN_BEARER_TOKEN_FILE="$CAPTURE_HOST_DATA_ROOT/state/<upgrade-id>/admin-token"
python3 tools/official_client_capture/codex_upgrade.py capture-candidate run \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json \
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

工具必须在真实请求前复算 VC-4 冻结的源码、运行镜像、构建和画像身份；全部一致后才原子创建 attempt，
生成 `attempt_id` 与 `run_nonce`。attempt、activation fact、镜像构建证明和实测源码摘要必须指向同一
源码树。任一身份不一致时不得创建 reservation 或发送请求，应按 Framework §5.3.4 返回相应阶段。

Campaign 与 candidate ID 会和场景后缀、主体及 16 字符 UTC 窗口拼成 direct／mitm 运行坐标，最终值
不得超过 128 字符。编排器必须在 reservation 前复算完整坐标并失败关闭，不得留下必败 attempt。run 与
seal 之间不得修改 candidate 源码树；`runs/` 及 evidence root 的目录权限至多 `0700`、文件至多
`0600`。确认必败时执行受管停止和环境恢复，不得强杀并丢失 after 探针。

失败补跑先执行 `resume --rerun-failed --preview-recovery`。预览中的 execute／reuse 必须等于上述依赖
计算结果，并明确 `reservation_exists=false`、`live_request_count=0`、`scanned_bytes=0`；不得再固定为
“两个失败、七个复用”等某次历史数量。预览与正式补跑须提供逐字一致的 runtime image、image ID、source、
build、版本、profile、purpose 和 VC-4 build receipt。身份不变的临时失败才能续跑；身份或首个 attempt
后的运行坐标变化时，
按 Framework §5.3.4 建立新 candidate 或新 Campaign，旧 candidate 只读保留。

### 4.5.2 定向场景、模型双轨与第三方入口

`scenarios.json` 是任务、规则覆盖和必需客户端的事实源。每条规则必须有真实触发场景；每个
入口只需证明适用规则及向同一 candidate Release 收敛。批准清单中的完整场景定义必须保留，但本轮只执行
`execute_item_ids`；必需执行项必须全部完成。可选项只有预先标为 optional、独有规则覆盖为零、替代证据
完整且缺口已封存时才可不阻断。

| 入口 | 必须证明 |
|---|---|
| 官方 Codex CLI／Desktop | 入站版本、平台、surface 或身份不影响 `previous` 候选 Release，身份冲突不返回 502 |
| `/v1/chat/completions` | Compatible 适配后进入目标 HTTP Responses 画像 |
| `/v1/responses` HTTP | Responses 入口使用同一目标 HTTP 画像 |
| `/v1/responses` WebSocket | WS、预热和 fallback 使用同一 Bundle，不跨版本回落 |
| Kilo Compatible | `kilo-compatible` 收据绑定模型、账号、请求、响应、usage、candidate 和 profile |
| Kilo Responses | `kilo-responses` 收据绑定相同 candidate 身份和 profile |

模型轨道必须从 `capturelib/model.py` 的目标版本政策和正式 `/models` 证据共同确认，不能用历史版本或
全版本并集替代。0.151.0→0.154.0 的冻结坐标为：

| 轨道 | 模型 | 必须验证 | 用途 |
|---|---|---|---|
| main | `gpt-5.5` | `use_responses_lite=false` | 官方／Candidate 主场景 |
| lite | `gpt-6-astra` | `use_responses_lite=true` | Lite 专项及 Kilo 双入口 |

正式 initialize-only 模型目录证据必须同时确认两项。不得把 `gpt-6-astra` 放入 main 轨，也不得沿用
0.151.0 的 `gpt-5.6-terra` 作为 0.154.0 Lite 模型；缺失、互换或模型元数据不符均在请求前失败关闭。

Candidate MITM 矩阵不得继承 `capture-cli` 的 `CODEX_HOME`。每次 Job 必须创建独占空目录，只放入
`features.plugins=false`、禁更新和禁遥测配置，不复制 `auth.json`，结束时受控删除。目标版本未批准的
MCP／插件发现流量不得进入模型场景；MITM 单场景超时至少 120 秒。每个 `subject×scenario` 使用独立
run ID，场景结束立即写 JSONL／摘要 checkpoint；恢复只执行 checkpoint 缺失或失败的坐标，已通过坐标
禁止重发。临时上游关闭须封存当前场景并立即结束 Job，不继续余下场景或扩大为整个 Job 重跑。
MITM wrapper 的 `capture_mount=${CAPTURE_MOUNT:-/capture}` 初始化属于 VC-0 离线演练门禁；未通过时不得
进入本阶段现场修复。

`EnableRequestCompression=true` 表示画像支持 zstd，不表示每次请求都必须压缩。Candidate 自定义 provider
入站未携带 `Content-Encoding: zstd` 时，普通 Responses 出站不加 zstd；Lite 条件另行成立。成功与失败
样本均无该 Header 时，不得把它误判为临时上游关闭的根因。

场景真实性门禁 `SCN-REALITY-01` 明确区分“job 退出成功”和“目标协议分支真实成立”。A11
realtime sideband、A13 OAuth refresh、A14 Files 三跳等高风险场景只有在原始 CLI／relay／pcap
或驱动事件能证明触发、关键中间事实和最终状态时才生成成功收据；编排器不得根据退出码补写。
收据还必须绑定 track、model、Lite 条件、evidence root、Campaign、attempt 和 `run_nonce`。

`run` 完成后、首次 `seal` 前，必须完成两条真实 Kilo 请求；其 ingress、runtime、response 和
usage 必须绑定本次 Campaign／attempt／`run_nonce`，并位于 attempt 开始与 client checkpoint
之间。两条入口统一使用 Campaign 已冻结的 `lite_model`；对 0.154.0 即 `gpt-6-astra`。主轨 `model`
只用于官方／候选主场景，不得被 seal 隐式复用于 Kilo；历史 Campaign 未记录 `lite_model` 时，仅只读
重放允许回退主轨模型。
两条请求之后不得再发送本 attempt 的客户端验证请求。
Kilo runner 在写入任何检查点或请求前必须把 `evidence/client` 及其全部新建子目录显式设为 `0700`；不得只收紧 `client/raw` 而留下可被 group/other 遍历的父目录。
Kilo Responses 的 `@ai-sdk/openai` provider 必须显式设置 `options.websocket=true`；首次 `seal`
前必须同时核验 Compatible 入站为 `POST/200`、Responses 入站为 `GET/101`，以及两条 usage 的
`openai_ws_mode` 分别为 `false/true`。任一项不符即放弃本 attempt，禁止先建立 client checkpoint。
时间门禁按传输语义校验：HTTP 仍要求响应完成后记账；WebSocket 的 usage 可能在连接关闭前或后落库，
只要求它晚于入站且所有事实均在同一 attempt 时间窗，禁止用 HTTP 顺序误拒绝真实 WebSocket 收据。

### 4.5.3 Candidate 证据封存

封存保持四阶段，不能合并为一次不可审核写入：

1. **建立检查点**：两条 Kilo 请求完成后，首次执行下列动作体；工具采集 `client-after` 并返回
   `client_checkpoint_created`，此时尚未形成最终 seal。

   ~~~bash
   python3 tools/official_client_capture/codex_upgrade.py capture-candidate seal \
     --campaign-dir /绝对路径/campaign \
     --candidate-id <candidate-id> \
     --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json \
     --candidate-purpose <与 run 完全相同的用途> \
     --attempt-id <attempt-id>
   ~~~

2. **生成收据**：在 candidate 源码树外运行受管生成器，形成 capture manifest、Go test trace、
   observed-profile 和两份 Kilo 收据。`build_*` 产物只是 finalizer 输入，不能直接提交给 seal；
   正式收据必须由受管 finalizer 生成。activation fact 必须由运行服务产生；测试 trace 必须来自
   同源树上的冻结测试日志，生成器不得合成二者。
3. **生成预览并完成唯一深度扫描**：

   ~~~bash
   python3 tools/official_client_capture/codex_upgrade.py capture-candidate seal \
     --campaign-dir /绝对路径/campaign \
     --candidate-id <candidate-id> \
     --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json \
     --candidate-purpose <与 run 完全相同的用途> \
     --attempt-id <attempt-id> \
     --capture-manifest /绝对路径/capture-manifest.json \
     --assertion-evidence-root /绝对路径/attempt-evidence \
     --observed-profile-receipt /绝对路径/observed-profile-receipt.json \
     --client-evidence kilo-compatible=/绝对路径/kilo-compatible-receipt.json \
     --client-evidence kilo-responses=/绝对路径/kilo-responses-receipt.json
   ~~~

   工具先检查路径、符号链接、权限、属主、磁盘、身份和必需收据；任何廉价检查失败时不得读取原始
   内容。通过后只进行一次完整扫描，生成只写一次的 `evidence-manifest.json`、`seal-draft.json` 和
   `seal-preview.json`，记录扫描字节、耗时和根摘要并返回 `review_sha256`。扫描中断时从逐文件
   checkpoint 继续，已完成且边界未变的条目不得重新读取。
4. **批准封存**：人工复核预览后，只用 Campaign、candidate、attempt、用途和
   `review_sha256` 批准：

   ~~~bash
   python3 tools/official_client_capture/codex_upgrade.py capture-candidate seal \
     --campaign-dir /绝对路径/campaign \
     --candidate-id <candidate-id> \
     --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json \
     --candidate-purpose <与 run 完全相同的用途> \
     --attempt-id <attempt-id> \
     --approve-seal-sha256 <review_sha256>
   ~~~

   批准阶段只验证冻结草案、manifest 根摘要和不可变 stat 边界，`scanned_bytes=0`；不得重新生成
   surface、inventory 或 secret scan。通过后 Campaign 进入 `candidate_sealed`。

普通 `status`、compare 和 accept 只重放 manifest／摘要链。`deep-verify` 只用于缺少 manifest 的历史
导入边界，或人工明确要求的独立审计；不得由恢复判断隐式触发，也不覆盖历史文件。

四路输入的路径和来源固定如下：

| 输入 | 路径与来源约束 |
|---|---|
| capture manifest／assertion bundle | 位于 attempt evidence root 内，并由 provenance 完整覆盖 |
| Go test trace | 以 bundle 相对路径登记；内部状态记录只能来自同源候选树的冻结测试日志 |
| 画像、断言和事实映射 | 位于 candidate 源码树内并与批准清单逐字绑定 |
| observed-profile／Kilo 收据 | 位于 attempt evidence root，绑定 Campaign／candidate／attempt／`run_nonce` 和镜像身份 |

Candidate MITM Job 的 producer 合同只包含应用层 JSONL 和场景摘要，不生成 pcap；其清单没有 pcap 时，
必须以 `scanned_bytes=0` 结束 pcap 排查，禁止调用 tshark 或扩大扫描目录。

证据标签只能从采集参数和场景 precondition 推出，不能根据待通过的 selector 或断言结果反推；
侧别豁免只允许结构上没有产出路径的 check，采集遗漏必须重采。

`candidate_sealed` 只表示：本轮 execute／reuse Job 闭合、Kilo 双入口通过、运行画像和同源结构化测试
可复算、环境恢复、secret scan、inventory 与 evidence seal 完整，且 `review_sha256` 已批准。seal 内的
结构门禁不等于后续逐规则验收；comparison、4.5.5 的机器断言和 AcceptanceFact 尚未完成。

### 4.5.4 离线比较

~~~bash
python3 tools/official_client_capture/codex_upgrade.py compare \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id>
~~~

比较机必须能从收据登记的绝对 evidence root 复算两侧证据；跨机器时只同步 manifest 引用的不可变
证据根到登记路径，再执行 `status`，不得顺带复制未引用历史目录。evidence root 的绝对路径必须一致；
finalizer 的工作树绝对前缀可以变化，但 `producer.tool.path` 必须能解析为同一受管相对坐标，且工具摘要
必须是当前值或已登记历史值。不能再为对齐 finalizer 绝对路径复制整个源码树。
工具只读重验身份、inventory、恢复、任务、规则覆盖和 profile 绑定，生成
`comparisons/<candidate-id>/result.json` 与 `results.template.json`，Campaign 进入
`compared`。

`equal` 只表示两侧完整 surface 集合相同，不是验收结论；采集计划不同可导致 `equal=false`。
只要 comparison 为 `complete`、`offline_only=true`，coverage 和 profile binding 完整，就可
进入 `compared`；行为一致性由逐规则断言决定。

### 4.5.5 逐规则机器断言

~~~bash
python3 tools/official_client_capture/build_rule_assertion_results.py \
  --config /绝对路径/assertion-config.json \
  --output /绝对路径/campaign/assertions/<candidate-id>/results.json \
  --results-dir /绝对路径/campaign/assertions/<candidate-id>/machine
~~~

配置必须绑定五份批准清单、目标版本和 profile digest，以及官方、candidate、comparison 的
package digest、capture manifest、证据根和逻辑路径前缀。

selector 选择的 `record_type` 可能承载多个事实时，必须在 `where` 中声明字段存在性和适用条件；
断言读取的字段不是每条记录必有时，至少同时约束对应字段为 `operator=present`。例如 `SPEC-EP-002`
的 `file-url-chain` 只能选择同时存在 `data.create_upload_url_sha256` 与
`data.put_url_sha256` 的 `file_upload_chain` 记录，以免把 C2PA 正／负／retry 事实误纳入 URL 链断言。
不得放宽 `all_fields_equal` 或用 `any_equal` 掩盖缺失字段；selector 修正须按 Framework §5.3.4 停止
当前 Campaign，从 VC-2 建立新 Campaign，并按 §5.3.3 只读复用仍然有效的官方证据。

| validation mode | 机器判定 |
|---|---|
| `dual_wire` | 在官方和候选封存证据上执行同一规则的侧别检查，两侧均须通过 |
| `candidate_profile` | 在候选证据上验证 Sub2API 内部实现，并绑定批准的官方权威摘要 |

`results.json` 必须唯一覆盖目标规则全集：affected 规则执行本轮机器断言，inherited 规则只从批准迁移
收据重放。每条规则最终均为 `status=pass`、`evidence_level=full`；不允许 fail、N／A、手写通过、
未绑定 inventory 的证据路径，或把继承规则伪装成本轮执行。

### 4.5.6 外部门禁与 accept

在同一 candidate 源码树执行 `make check-egress-spec`、`make test` 和目标平台测试。首次 attempt 必须
执行全部三项；每次 attempt 均在首项门禁前和末项门禁后生成 `gate_before／gate_after` ARM64 环境
收据，并以 attempt ID 为主体。把 attempt ID、可空的根因、前序失败收据引用、两份环境收据、命令、
工作目录、主机、架构、时间、退出码、通过／失败／跳过计数及输出证据写入权限为 `0600` 的
`candidate-gates.facts.json`。新 facts 使用 `codex-upgrade-external-gate-facts/v4`，本阶段的
`gate_plan` 固定为 `null`；证据根必须是绝对路径、非符号链接且权限为 `0700`。随后生成并独立重放
`candidate_external` v4 收据：

~~~bash
python3 tools/official_client_capture/codex_upgrade_gate_receipt.py finalize \
  --evidence-root /绝对路径/candidate-gates \
  --facts candidate-gates.facts.json \
  --output candidate-gates.receipt.json

python3 tools/official_client_capture/codex_upgrade_gate_receipt.py replay \
  --evidence-root /绝对路径/candidate-gates \
  --receipt candidate-gates.receipt.json
~~~

finalizer 固定检查首次 attempt 完整覆盖三项门禁，并绑定 formal 模式、Campaign／candidate 用途、
目标版本／架构、Profile、candidate package、源码树和镜像。门禁失败时生成 `status=failed` 的只读
收据并登记 `root_cause_id`；只有最终有效集合全部退出码 0、失败 0、跳过 0 的 `status=passed` 收据才
能进入 `accept`。缺项、替换命令、用途漂移、证据摘要漂移或身份不一致时不得执行 `accept`。

门禁补跑按 Framework §5.1.2、§5.3.4 绑定唯一前序收据和 environment continuity，只重跑失败项；
已通过项只读承接，每次补跑使用新的 facts／receipt 路径。同根因第二次仍失败即停线；无法证明承接的
工具计为 P0 阻断，禁止复制 JSON、第三次 attempt 或反复全量执行。

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
| 套件与身份 | 批准的 full suite 身份、官方二进制身份、candidate 完整身份、运行 profile，以及可独立重放的 candidate 外部门禁收据；不表示重跑 inherited 项 |
| 比较与规则 | comparison 完成、双侧规则覆盖、逐规则断言完整、分类无阻断 |
| 恢复与安全 | 两侧环境恢复、secret scan 和 evidence inventory 摘要 |
| 第三方入口 | 必需客户端收据齐全，重新解析后与封存绑定一致 |

`accept` 会重新独立重放外部门禁，并把收据、candidate identity、candidate package digest 和 evidence
seal 绑定为同一 AcceptanceFact。全部通过后，工具只写一次地保存断言、`accepted=true`、
`failed_gates=[]`、`production_state=accepted_not_activated` 和 evidence seal，Campaign 进入 `ready`；
收据事后漂移会使 `status` 重新退回非 ready。
失败 attempt 不可覆盖，Campaign 保持 `compared`。

### 4.5.7 ready 与 canonical 交接

`ready` 只表示固定 candidate 已通过目标规则和 Campaign 证据验收。模型收据、HTTP 200 或
`equal=true` 都不能替代逐规则断言；`ready` 也不表示已经晋升 Catalog、构建正式镜像、切换
生产或完成回滚演练。

两个用途都必须生成
`control/vc/receipts/<candidate-id>/vc5-completion.json` 并以它封存
`control/vc/vc-5-checkpoint.json`，不能在本阶段宣称已经交付或上线。
`validation_only` 的 `accept` 成功后直接生成这两份制品，保持
`accepted_not_activated`，不创建 canonical 生产链。`production_replacement` 则在
`canonical-advance --canonical-step accept` 重放 AcceptanceFact 后生成完成收据和 VC-5 checkpoint。

`canonical-import` 只能作为 4.5.4～4.5.6 已完成后的交接，不能替代 comparison、逐规则断言、
`candidate_external` 或 AcceptanceFact；命令即使能读取更小输入集合，也禁止借此绕过上述门禁。

生产用途先用下列动作体生成零扫描、零请求预览；`--retire-version` 只冻结 VC-6 将处理的旧 Previous，不在
本阶段删除任何画像：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py canonical-import \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --attempt-id <attempt-id> \
  --kilo-facts /绝对路径/kilo-facts.json \
  --active-profile /绝对路径/production-active-profile.json \
  --profile-patch-manifest /绝对路径/profile-rule-patches.json \
  --profile-activation-fact /绝对路径/profile-activation-fact.json \
  --supervisor-run-dir /绝对路径/campaign-run \
  --phase VC-5 \
  --retire-version <旧-previous-version>
~~~

预览必须逐项给出 affected／inherited、execute／reuse、来源类型和 `review_sha256`，同时保持
`scanned_bytes=0`、`live_request_count=0`。复核后以完全相同参数追加
`--approve-import-sha256 <review_sha256>`，只写一次初始化 checkpoint；不得复制证据或修改既有 attempt。

随后仍由 `campaign-run` 按顺序派发三个动作体：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir /绝对路径/campaign --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step seal
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir /绝对路径/campaign --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step compare
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir /绝对路径/campaign --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step accept
~~~

三个步骤只聚合、比较和重放本节已经通过的事实，不发送请求、不扫描历史原始证据，也不得生成第二套
规则结论。accept 后 VC-5 自身待办必须为零；canonical `plan.execute_item_ids` 必须且只能剩下
`production-activation`、`rollback-verification` 和一个 `retire-<version>`。这三项属于 VC-6，
不能被记为 VC-5 未完成，也不能在 VC-5 提前执行。

对 `production_replacement`，`status --candidate-id` 必须继续返回
`production_status=accepted_not_activated` 并明确提示 §4.6；在 promotion、canary、activation 和
rollback 收据完成前不得宣称升级完成。
候选验证镜像以 `previous` 运行，只证明候选规则，不能直接作为默认 `active` 的生产镜像。

<a id="codex-vc-6"></a>
## 4.6 VC-6 交付或生产激活

- **输入**：VC-5 AcceptanceFact、固定 candidate 和 `campaign_purpose`；生产替换另需最新 canonical
  checkpoint、当前 Active／Previous 和 rollback。
- **操作与工具**：`validation_only` 只读核验并登记候选交付完成；`production_replacement` 生成 production
  Release 和最终发布镜像，再执行 canary、切流、实际回滚、目标恢复、归档和清理判定。
- **产物**：候选交付收据，或 promotion、post-promotion、正式发布、activation、rollback、restoration、
  RemovalReceipt、私有归档和清理决定；两个用途最终都生成 `vc6-completion.json` 与 VC-6 checkpoint。
- **完成标志**：`validation_only` 达到 `ready_for_operator_release`；`production_replacement` 的
  canonical 待执行项为零、私有归档可恢复、清理决定已登记，并达到
  `production_active_restored`。两者均必须能重放 VC-5／VC-6 完成收据和直接前序 checkpoint 链。
- **失败恢复**：`validation_only` 的 AcceptanceFact 重放失败时停在线上交付前并返回 VC-5 定位；
  `production_replacement` 只从最新 canonical checkpoint 续跑，禁止无依据重跑 VC-0～VC-5。

本节只补充 Framework §5.6 在 Codex 轨道中的 Catalog 晋升、终态门禁、正式镜像、canary 和生产
激活收据。所有输入和输出必须绑定同一个 candidate ID 和 acceptance SHA；候选验证源码／镜像与
晋升后的生产源码／镜像是两组不同身份，必须由 promotion receipt、差异清单和终态门禁连接，禁止
把 candidate 镜像摘要冒充 production 镜像摘要。

| 步骤 | 工作 | 检查点 |
|---|---|---|
| 1 | 分流用途：只读交付候选，或冻结生产现状和真实回滚点 | 交付完成事件，或 production snapshot／rollback 可复算 |
| 2 | 晋升 Catalog 并生成受限 production tree | promotion receipt 与逐文件差异闭合 |
| 3 | 执行 affected-rule 和公共 post-promotion 门禁 | `post_promotion` 收据通过 |
| 4 | 同步权威源码、正式发版并构建最终镜像 | Git／Release／最终镜像摘要一致 |
| 5 | 用最终镜像执行隔离 Active canary | `canary_passed` |
| 6 | 原子替换生产应用容器 | `active` |
| 7 | 实际回滚、恢复、签收并推进 canonical；首次登记生产交付 | `restored_active`、canonical 待执行项为零，`vc6_status=production_archive_pending` |
| 8 | 归档证据、登记清理决定，再次登记交付 | 两张统一收据可重放，VC-6 完成收据与 checkpoint 封存 |

### 4.6.1 用途分流、生产快照与回滚点

先重放 VC-5 AcceptanceFact，并核对 `candidate_id`、目标版本、Profile、candidate package、用途和
acceptance SHA。`validation_only` 还必须确认 Campaign 为 `ready`、生产状态仍为
`accepted_not_activated`、生产 selector 未变化，然后登记绑定上述摘要的 VC-6 交付完成事件并结束；
该出口的 live 请求数和原始证据扫描字节均为零，不创建 canonical、production attempt 或生产收据。

两个用途都使用同一只读命令登记交付状态：`validation_only` 在上述条件满足后执行，并以此结束 VC-6；
`production_replacement` 必须等到 §4.6.7 的 canonical checkpoint 达到 `restored_active` 且待执行项为零
后执行，形成生产恢复交付收据，再继续 §4.6.8 的归档、清理判定和最终完成事件。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py deliver-candidate \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --attempt-id <VC-5-attempt-id> \
  --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json
~~~

该命令只重放 Candidate、构建收据、AcceptanceFact 和必要的 canonical 终态，生成不可覆盖的
`delivery/<candidate-id>/receipt.json`；不发送请求，也不扫描原始证据。`validation_only` 的同一次调用还会生成
`control/vc/receipts/<candidate-id>/vc6-completion.json` 和 VC-6 checkpoint。对 `production_replacement`，首次调用只证明
`production_active_restored`，不能替代 §4.6.8 的私有归档、清理决定或 VC-6 最终完成事件。

进入生产分支前，最新 canonical checkpoint 必须已完成 candidate seal、compare、affected 规则断言、
inherited 收据重放和 acceptance，且 `candidate_id`、`attempt_id` 及上述身份与 VC-5 完全一致。用途不是
`production_replacement` 或仍有 VC-5 待执行项时不得进入生产分支。

写操作前只读记录容器 digest、compose／override、selector、activation fact、Active／Previous、
数据与依赖服务、网络、挂载、代理／CA 和 VC-0／P0 冻结的生产主机身份。名义 Catalog、强制 mode
与实际流量不一致，或当前生产身份已偏离 VC-0 快照时立即停止。

把当前 Active 冻结为本轮 rollback，绑定其 Release／Profile、镜像 digest、compose 和必要配置，并在
只读数据克隆或等价隔离环境证明旧镜像可启动、读取数据并通过 health／鉴权。旧 Previous 只作为待退休
对象，不得冒充 rollback；依赖可变标签、临时环境变量或未经验证的历史镜像不能作为回滚点。

### 4.6.2 Catalog 晋升与 production tree

执行 promotion 前必须具备 candidate／active 双模式夹具并证明 Go／Python 后继图一致。
在 VC-5 接受的 candidate 源码副本上确认 Catalog 为“Active＝回滚版本、Previous＝目标版本”，
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
变化或运行时代码变化必须按 Framework §5.3.4 建立新 candidate 或新 Campaign，不能夹带进 promotion。

文本门禁的 `files` 历史债务必须为 `{}`；`approved_non_leak_references` 只容纳带理由的精确
非泄漏语义。promotion 阶段禁止运行任何会写回版本泄漏基线的命令；若 AST 命中已经减少，应先
作为独立维护变更收紧基线并重新形成 candidate，不能在 production tree 中顺手更新。

### 4.6.3 动态 post-promotion 门禁

门禁集合必须由本轮批准事实计算，而不是写死上一版本的规则编号：

```text
execute = affected_rule_ids 对应的实现门禁 ∪ 公共终态门禁
reuse = inherited_rule_ids 收据及依赖未变化的既有结果
```

公共终态门禁至少覆盖 Catalog projection、版本泄漏判据自测、版本泄漏扫描和 AST 门禁。每个
affected rule 必须映射到明确测试；继承规则、已复用 Candidate Job 和 Kilo 只重放 canonical checkpoint，
不得运行全量 Candidate、全量规则回归或目标架构全量测试。

VC-6 不重新计算或修改门禁集合。它必须逐项核对 VC-3 的门禁需求、VC-4 的执行计划、ApprovalFact、
candidate 源码树和当前 production tree；门禁 ID、规则映射、命令或摘要任一不一致都不得执行。若工具
只能接受固定历史门禁、会自动加入未批准规则或遗漏 affected 规则，说明 VC-0 能力演练失真：停止当前
Campaign，将工具修复拆成独立变更并从 VC-0 重新开始，禁止在 VC-6 现场改清单。

首次 attempt 必须执行 VC-4 计划中的全部门禁。执行前，把已验收的 gate plan 按原字节复制到权限
`0700` 的独立 evidence root 内，文件权限设为 `0600`；不得从 production tree 重新生成计划。
`post-promotion-gates.facts.json` 必须使用
`codex-upgrade-external-gate-facts/v4`，其中 `gate_plan` 以 evidence root 内相对路径和文件摘要绑定该
副本；AcceptanceFact 的 candidate identity 同时绑定 plan、requirements 和文件摘要。

每个 gate 结果必须逐字复制计划中的 `gate_id`、`test_id`、`working_directory` 和 `command`，并记录
执行结果与证据；缺项、重复、额外 gate 或任一字段漂移都失败。每次 attempt 还须绑定以 attempt ID 为
主体的 `gate_before／gate_after` ARM64 环境收据、根因和可空的前序失败收据，并在 canary 前生成、重放
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

该阶段新收据为 `codex-upgrade-external-gate-receipt/v4`，额外绑定 AcceptanceFact、promotion receipt、
production tree 和 gate plan；candidate 身份、目标架构、Profile、package、源码树和镜像必须与验收阶段
完全一致。失败 attempt 只读保留并按 v4 合同仅补跑 `failed_gate_ids`；最终收据不是 `status=passed`、
仍有失败或跳过、输入或计划摘要漂移、production tree 不一致，均禁止正式发版、构建最终镜像或开始
canary。v3 facts 禁止再签发新收据，仅允许既有 v3 receipt 历史重放。

补跑统一遵守 Framework §5.1.2 和 §5.3.4，只执行失败、待执行或依赖变化的下游闭集；同根因第二次
仍失败即停线。失败时保持旧 Active，不得为通过门禁修改 production tree。

### 4.6.4 权威源码、正式发版与最终镜像

post-promotion 通过后，先把 production tree 按逐文件 manifest 同步到本地权威仓库，再提交、打 tag、
正式发版和构建最终镜像；不得先激活临时镜像，发版后再重复一轮生产切换。同步至少校验：

1. production Catalog 的 Campaign／acceptance 与 promotion receipt 一致，ReleaseGraph、SnapshotCatalog、
   Active／Previous 和 profile／release digest 均可从仓库复算；
2. 本地最终树与 production tree 的每项差异均已分类；后继维护变化须另行验收，未分类差异禁止提交；
3. Git commit／tag、源码树、构建参数和 amd64／arm64 镜像 digest 相互绑定；发版过程若改写 VERSION 或
   其他受管文件，原 post-promotion 收据立即失效，必须重新生成 production tree 并重跑受影响闭集。

最终 production 镜像必须绑定 candidate／production tree digest、AcceptanceFact、promotion receipt、
inventory、post-promotion 收据和构建输入，并使用 `repository@sha256:<manifest-digest>` 交接。
构建不得携带 `candidatecapture`，也不得复用候选镜像。后续 canary、生产切换、回滚和目标恢复必须使用
同一最终发布 digest；任一摘要不一致时禁止部署。

GitHub 只保存可公开源码和发布制品，不能替代原始抓包、Campaign、acceptance 或生产激活证据；
GitHub 发版成功也不等于生产已经更新。

### 4.6.5 独立 Active canary

使用 4.6.4 的最终发布镜像 digest 建立与生产隔离的 canary，独立使用账号、`CODEX_HOME`、数据库、Redis、
配置、网络和证据目录。canary 必须按晋升后 Catalog 的默认 `active` 运行，禁止以强制 mode
命中目标画像，也禁止直接复用 activation fact 显示 `profile_mode=previous` 的候选验证镜像。

核对镜像架构、启动、health、HTTP／WebSocket／TLS、错误率、Guard 和 activation fact；事实
必须绑定目标 version、profile／release digest 和正式镜像，强制 mode 计数为 0。真实业务须
出现 §4.5 规定的完成事件；失败不得进入生产。

### 4.6.6 原子生产切换

部署前复核 `docker compose config` 或等价结果，并在 VC-0／P0 冻结的生产主机上复核固定
容器 IP、DMIT 出口和 wg1 持久配置／运行时 MTU；当前 ARM64 基线为 1420，实际判定以 VC-0 冻结值为准。
应用服务必须绑定最终 `repository@sha256:<manifest-digest>`，数据库、Redis、keeper、挂载和网络保持
不变。冻结动作体为：

~~~bash
docker compose -f /绝对路径/production-compose.yml \
  -f /绝对路径/production-image.override.yml up -d --no-deps <application-service>
~~~

远端 registry 尚未缓存固定 digest 时才先执行定向 `pull`；本机 registry 已有精确 digest 时不得
为形式完整重复拉取。两种情况都必须在切换前后复核实际 image ID／RepoDigest。

禁止 `compose down` 和无范围 `prune`。部署后复核 wg1 配置／运行时 MTU、固定容器 IP／出口、
容器 digest、compose、health、日志、依赖、
挂载和 activation fact，确认 Active version、profile／release digest 与 promotion receipt
一致且没有强制 override。发现身份、安全、数据、恢复、旧画像兜底、跨 Bundle fallback 或
连接池混用时立即完整回滚，不在故障实例上补画像或改 selector。

### 4.6.7 回滚、目标恢复与 canonical 终态

正式切换后仅替换应用容器，切回 §4.6.1 冻结的旧镜像和 compose，复核 health、鉴权、数据、
依赖、挂载、代理／CA、入口和 final-wire；不得重建数据容器。随后恢复目标镜像，重复检查镜像、
Active Release、profile、activation fact、业务事件、完整性计数和 Guard。

只有冻结的 rollback Release／Profile、旧镜像和 compose 已完整绑定时，“切回 rollback”才是完整回滚的
简写；只改 mode 不能替代演练。回滚不得删除 Campaign、覆盖 Snapshot 或销毁证据。

生产激活证据必须绑定 Campaign／acceptance、promotion／inventory、production tree、终态门禁、
最终发布镜像，以及 canary、正式切换、旧版回滚和目标恢复四阶段的时间、compose、各类 digest、
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
promotion receipt 一致。`receipt.json` 必须以 `codex-production-activation-receipt/v2` 独立重放成功。
四阶段全部通过，晋升、终态门禁、构建和激活证据形成同一条可复算链。Campaign 保持 `ready`，candidate
达到 `restored_active`；后继 candidate 若仅达到 `accepted_not_activated`，不得沿用本结论。

**Codex 终态机器判定。** 自 0.151 起，`production_active_upgraded` 只能由仓库门禁判定。终态收据
`docs/egress/maintenance/CODEX_CLI_<前版>_TO_<本版>_TERMINAL_STATE_RECEIPT.json` 入库后，
`make check-egress-spec-ci` 以及 `backend/internal/officialegress`、`backend/internal/service` 的冻结测试
必须同时证明：收据结果和自摘要有效；Catalog 晋升、生产激活、post-promotion 门禁、运行画像退休四份
阶段收据与审计索引逐字在库；审计索引自摘要和复核通过；声明退休的运行画像已不存在；当前 Active 的
Runtime Catalog source 指向该收据 Campaign 链末级，ReleaseGraph 的 Active source 落在同一条链上。
当前 Active 缺少对应终态收据或任一检查失败时，不得声明 `production_active_upgraded`。

四阶段收据生成并重放后，必须从最新 canonical checkpoint 按顺序登记，禁止走旧 successor／epoch 链：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir <campaign-dir> --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step production-activation --step-receipt <activation-receipt>
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir <campaign-dir> --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step rollback-verification --step-receipt <activation-receipt>
~~~

canonical 初始化时冻结的旧 Previous 必须另行生成消费者扫描为零的 RemovalReceipt，再以同一入口登记；
它不得等于本轮 rollback 或目标版本：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir <campaign-dir> --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step retire --retire-version <旧-previous-version> \
  --step-receipt <removal-receipt>
~~~

RemovalReceipt 必须证明 Catalog、selector 和未知消费者均为零、运行投影已移除且历史证据保留。退休失败
只保留该项待执行，不得影响已恢复的目标 Active、删除 rollback，或回退重跑 VC-0～VC-5。全部三项完成且
最新 checkpoint 的 `plan.execute_item_ids=[]` 后，生产激活链才完成并进入 §4.6.8；这仍不授权删除远端
升级文件，也不是 VC-6 的最终完成事件。

### 4.6.8 私有归档与远端清理

归档前必须把原始抓包、Campaign、AcceptanceFact、promotion、post-promotion、activation 和
RemovalReceipt 写入受控私有归档。含凭据或未脱敏字节的内容不得提交 GitHub。归档须提供逐文件路径、
大小和 SHA-256 清单，并在另一存储位置完成解包、摘要复算及关键收据重放。只有权威仓库、最终发布镜像
和私有证据归档三者均可独立恢复，才允许清理采集服务器。

清理前生成机器可读的保留／删除清单，并完成以下检查：

1. VC-0／P0 冻结的生产主机正在运行最终发布镜像的固定 digest；正式 compose／override
   已迁出升级临时目录，固定 rollback 镜像和配置仍可用；
2. `capture-cli-*` 和候选 Sub2API 容器不再被生产、归档或收据重放使用，停止后生产健康、网络和
   依赖状态不变；
3. 删除目标只包含已归档的 Campaign、candidate、run、临时源码、构建缓存和候选镜像；不得包含
   生产数据库、Redis、keeper、正式配置、当前镜像、回滚镜像或唯一证据副本；
4. 删除清单先以只读／dry-run 方式解析真实路径、大小和摘要，经人工批准后再按服务器分别执行。

`/root/docker/capture-cli` 的 Compose、固定网络和受管工具属于可复用抓包基础设施，不随一次升级删除；
只有其 `data` 下已归档且不再被收据引用的本轮 Campaign／run 和临时缓存才能进入删除清单。实际删除
尚未获批时，必须登记延期原因、精确保留清单和磁盘水位，不能把“未清理”伪装成已执行。

VC-6 的最终条件是 production tree 与权威提交差异闭合、最终镜像完成生产复验、私有归档可恢复重放，
且清理决定具有不可覆盖收据：已批准的删除必须证明目标不存在生产依赖或唯一副本并完成复验；未批准或
条件不足时必须明确延期。满足这些条件后才写 VC-6 完成事件。

私有归档和清理决定都使用 `codex_upgrade_vc_receipt.py`。两份 facts 分别使用
`kind=private_archive` 和 `kind=cleanup_decision`，按
`codex_upgrade_vc_receipt.schema.json` 声明规定的 assertions 与证据角色；证据根、facts、
收据及其所有引用都必须位于当前 Campaign 内。工具只登记和重放决定，不会代替操作员删除 ARM64 文件。

~~~bash
python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize \
  --evidence-root /绝对路径/campaign \
  --facts control/vc/receipts/<candidate-id>/private-archive.facts.json \
  --output control/vc/receipts/<candidate-id>/private-archive.json

python3 tools/official_client_capture/codex_upgrade_vc_receipt.py finalize \
  --evidence-root /绝对路径/campaign \
  --facts control/vc/receipts/<candidate-id>/cleanup-decision.facts.json \
  --output control/vc/receipts/<candidate-id>/cleanup-decision.json

python3 tools/official_client_capture/codex_upgrade.py deliver-candidate \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --attempt-id <VC-5-attempt-id> \
  --build-receipt /绝对路径/campaign/candidates/<candidate-id>/build-receipt.json \
  --private-archive-receipt /绝对路径/campaign/control/vc/receipts/<candidate-id>/private-archive.json \
  --cleanup-decision-receipt /绝对路径/campaign/control/vc/receipts/<candidate-id>/cleanup-decision.json
~~~

生产用途第一次不携归档／清理收据调用 `deliver-candidate` 时，合法终止于
`production_archive_pending`，且不得生成 VC-6 checkpoint。第二次必须同时携带两张收据；只给一张、
收据不在本 Campaign 内、主体不一致或证据摘要漂移时全部失败关闭。两张收据重放通过后，
才生成 `control/vc/receipts/<candidate-id>/vc6-completion.json` 和
`control/vc/vc-6-checkpoint.json`，`status --candidate-id` 才能报告最终完成。

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
