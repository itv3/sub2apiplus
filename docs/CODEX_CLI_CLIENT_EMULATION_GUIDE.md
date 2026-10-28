# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
>
> **当前版本**：Active 为 `codex-cli 0.164.0`，Previous 为 `codex-cli 0.163.0`；`0.160.0`、`0.157.0`、`0.154.0`、`0.151.0` 与 `0.149.1` 已退出 Runtime Catalog（运行投影已移除，画像字节按 §4.6.7 第 1 类保留为冻结历史制品）。完整生产身份见本文 §3.2，生产激活、回滚与 0.160.0 退休的机器事实见 [`0.164 终态收据`](egress/maintenance/CODEX_CLI_0163_TO_0164_TERMINAL_STATE_RECEIPT.json)。第二部分描述当前 Active 0.164.0 的规则：本轮 43 条目标规则中 38 条继承，`SPEC-BODY-003`、`SPEC-BODY-006`、`SPEC-EP-002`、`SPEC-EP-022`、`SPEC-WS-005` 变化；增量工具默认开启后 Lite 前缀的 id 是记录时随机的 UUIDv7、随历史重放，WS 预热帧的 `input` 为空，令牌刷新请求体多 `refresh_reason`，图像请求多两个回合头。
>
> **权威入口**：共享目标、证据生命周期、升级、发布与回滚以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准；依赖基线见
> [`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)，逐规则机器证据见
> [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。本文只定义 Codex CLI 的规则、画像、实现和专用流程增量。

正式 Campaign 的阶段动作只允许由 `codex_upgrade_supervisor.py campaign-run` 派发；`plan`、
`reuse-official-evidence` 和 `compile-and-run-vc-batch` 是三个受限的 Campaign 引导／批次控制命令；
`compile-vc-batch` 仅保留给历史读取和内部测试，0.154.0 起 Formal CLI 会失败关闭。适用边界见
第四部分开头的[公共执行约定](#codex-part4-premises)、[§4.7](#codex-4-7) 与 Framework §5.1.2、§5.3.2～§5.3.6。历史恢复入口仅供解释旧收据，不得用于新
Campaign，兼容边界以 [`codex_upgrade_legacy_boundary.py`](../tools/official_client_capture/codex_upgrade_legacy_boundary.py) 及对应版本 Schema／测试为准。

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

本部分定义规则成立所需的证据标准、观测边界和 54 个编号项，描述当前生产 active 0.164.0 的规则（previous 为
0.163.0）；生产激活、精确回滚、目标恢复与 0.160.0 Runtime Catalog 退休的机器事实见
[`0.164 终态收据`](egress/maintenance/CODEX_CLI_0163_TO_0164_TERMINAL_STATE_RECEIPT.json)。各条实测中的“0.164.0
官方样本”来自 Campaign `c01640-formal-vc1-r1-20261027t000000z` 的 attempt `20261027T135446Z-d8a16a8e788de81e`
（本轮官方证据全部重新采集，47 个官方 Job 全部 complete）；继承规则正文里保留的“0.163.0 官方样本”（Campaign
`c01630-formal-vc1-r2-20261020t131500z` 的 attempt `20261021T003916Z-24ff3483a4288709`）、“0.160.0 官方样本”（Campaign
`c01600-formal-vc1-r1-20261003t084239z` 的 attempt `20261003T102647Z-6b5390a3ddffc296`）与“0.157.0 官方样本”
（Campaign `c01570-formal-vc1-r2-20260926t084354z` 的 attempt `20260926T094510Z-ce907a4fa1d25931`）是历史佐证；
“候选断言”指 `c01640-formal-vc1-r1-20261027t000000z` canonical 链中的 `assert-SPEC-*`（评估基线与 evidence_level 以该链的断言结果为准）。
本轮官方采集的主线（`gpt-6.1-sol`）与 Lite 轨（`gpt-6-astra`）都是 Lite 模型，非 Lite 分支没有 0.164.0
官方样本（`gpt-5.5` 已在采集账号下线），相关结论以源码为准（0.164.0 源码；0.165.0 源码在该分支的请求构造代码上只有两处变化：`reasoning.summary` 增加提供方能力判断，OpenAI 默认为开；`reasoning_effort` 覆盖默认开启，只对清单标记 `supports_reasoning_effort_updates` 的 Lite 模型生效，见 SPEC-BODY-006）。
源码字段的行号锚点以本地固化的 0.165.0 源码为准（`tools/spec_ref_anchors.json`，源码树 `local-analysis/sources/codex-cli-0.165.0`；
0.163.0 到 0.164.0、0.164.0 到 0.165.0 之间有变化的引用已逐条复核，行为有差异处在对应规则里以“0.164.0 复核”“0.165.0 复核”标出；
0.165.0 目前只做过源码复核，尚无 0.165.0 官方样本，各条实测里的样本类证据仍是 0.164.0 及更早版本的）；正文里 0.157.0 与 0.160.0 的
“文件 第 N 行”坐标只是历史对照，不再由门禁复核。

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
| 当前 Active／Previous 及生产身份 | 本文 §3.2、[`Runtime Release Catalog`](../backend/internal/officialegress/catalogdata/runtime/release-catalog.json) 与 [`0.164 终态收据`](egress/maintenance/CODEX_CLI_0163_TO_0164_TERMINAL_STATE_RECEIPT.json) |

每条规则的准入证据包必须绑定官方源码、依赖、二进制、平台、配置、账号、抓包运行号和摘要。只有能够
重新解析的材料可以作为规则依据；R 类材料只允许等长脱敏，未脱敏材料不得离开采集机。

证据基线与运行角色相互独立：经 VC-2 判定 `inherit` 的规则可以继续引用旧版本 L1／L2／P／R，但这不
表示旧版本仍是 Active。运行时 Active／Previous 的机器事实只读取 Release Catalog，§3.2 负责其人类可读
摘要；已退出 Runtime Catalog 的历史版本，其身份与原始 run 只按各自的 Catalog 晋升、生产激活与终态收据读取。

### 2.1.2 规则准入与观测边界

规则只有在被测身份和适用条件明确、源码与合适的 wire 观测通道闭环、正反例充分、引用和摘要
可复算时才能准入。证据不足时只能收窄命题或保持未决。

“固定、随机、条件”分别表示每次一致、允许变化和随明确条件变化，不得把随机样本或条件结果
固化为默认行为。

- pcap、relay、MITM 和服务端重建只能证明各自可见的层次，不能互相替代；
- 自定义 CA、代理和受控失败等条件样本不能外推为默认路径或自然成功链；
- 全集、缺失和连接完整性结论必须基于无预设过滤的完整双向样本。

**遥测零流量判定（当前 active 0.164.0）。** Framework §1.2、§3.2 的公共规则适用；只有下列配置和
源码链均已冻结时，未产生的遥测才可排除在 strict 分母之外：

| 组件 | 关闭条件与源码闭环 |
|---|---|
| analytics | `config/src/types.rs` 第 248-251 行的 `AnalyticsConfigToml.enabled=false` 经 `core/src/config/mod.rs` 第 4700 行传入 analytics client，并由 `analytics/src/client.rs` 第 314-324 行不创建事件队列 |
| OTEL metrics | 必须设置 `otel.metrics_exporter=none`；`config/src/types.rs` 第 673-684 行（`OtelConfig::default`）中 log／trace exporter 默认虽为 `None`，metrics exporter 仍默认为 `Statsig`，且 `otel/src/provider.rs` 第 197-232 行会在其非 `None` 时构建指标管线，因此仅设置笼统的 `otel.exporter=none` 不成立 |

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
- **源码**：[L1] `http-client/src/custom_ca.rs:300-324`、`http-client/src/custom_ca.rs:402`；
  默认客户端失败回退见 `login/src/auth/default_client.rs:425-430`。
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
  `websocket-client/src/lib.rs:93-98`（0.163.0 另增 `WebSocketTlsMode::Rustls(config)` 取值，仅 realtime sideband
  使用；Responses WS 仍经 `WebSocketConnector::new` 走 `ExplicitCodexTls`，即 rustls）。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）取得四种扩展排列；0.157.0 官方 direct 抓包的 4 份 rustls
  ClientHello（chatgpt.com 3、api.openai.com 1）扩展集合与上列一致、排列各不相同，signature_algorithms 为上列 13 项；
  候选断言 `assert-SPEC-TLS-003` 通过。0.157.0 的 rustls 为 0.23.45（`Cargo.lock` 第 13362 行；0.163.0 仍为 0.23.45，见第 13565 行；0.164.0 升到 0.23.46，见第 13553 行，同时 rustls-webpki 0.103.15→0.103.16、webpki-roots 1.0.5→1.0.10；0.165.0 的 `Cargo.lock` 里 rustls 仍为 0.23.46（第 13571 行）、
rustls-webpki 0.103.16、webpki-roots 1.0.10，与 0.164.0 相同，
ClientHello 是否随之变化未经官方抓包核实），provider 仍由
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
  `http-client/src/custom_ca.rs:300-324`。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）中 41 个 ClientHello 均无扩展 16。
- **实现**：默认分支不得固定 offer h2；启用有效 CA 后转入 SPEC-TLS-002／H2 分支。
- **状态**：✅ 源码无／不适用；抓包充分。

### SPEC-PROTO-002 Responses 默认 WS，HTTP 为降级路径

- **范围**：内置 OpenAI OAuth。
- **规则**：`supports_websockets=true` 时先走 WS；可重试错误（含 `slow_down`）耗尽重试预算并设置
  `force_http_fallback` 后，同一上层调用改走 HTTP POST；受控 426 同样降级到 HTTP。
- **源码**：[L1] `model-provider-info/src/lib.rs:204`、
  `core/src/client.rs:739`、`core/src/client.rs:1147`、
  `core/src/responses_retry.rs:120-139`。0.163.0 复核：降级触发条件未变（重试预算耗尽后切换，同一上层调用改走 HTTP）；
  变化只在重试判定与等待：判定改由 `protocol/src/error.rs` 第 418-470 行的 `CodexErr::retry_delay` 给出（`ServerOverloaded`
  与 `RetryLimit` 只在服务端给出 retry-after 时才可重试；0.164.0 新增的 `GatewayLoginRequired` 与其他需用户处理的错误一样不重试，
  只出现在自定义网关登录失效时，不影响内置 OpenAI OAuth），切换到 HTTP 之前若服务端给出 retry-after，会先等到该时刻
  （`core/src/responses_retry.rs` 第 126-129 行）。`unbounded_connection_retries` 在 0.149.1 已存在，对 HTTP 传输层连接失败
  的采样请求无限重试、不触发降级，本轮未变。0.165.0 复核：降级触发条件与重试判定未变；新增的终止性错误 `model_not_found`（SSE `error` 事件经
  `parse_terminal_provider_error` 解析，`codex-api/src/sse/responses.rs:349-355`；`protocol/src/error.rs:155`、`protocol/src/error.rs:430`）与其他需用户处理的错误一样不重试，
  不改变默认先走 WS、预算耗尽后降级 HTTP 的路径。
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
- **源码**：[L1] `login/src/auth/default_client.rs:217-219`、
  `core/src/client.rs:1299-1321`、`codex-api/src/endpoint/session.rs:80-154`、
  `model-provider/src/models_endpoint.rs:292-312`、`ext/image-generation/src/backend.rs:80-98`、
  `ext/web-search/src/tool.rs:113-119`。0.165.0 复核（该机制自 0.163.0 起如此，0.164.0 到 0.165.0 未变）：各端点改由 `login/src/auth/default_client.rs:258-306` 与
  `login/src/auth/default_client.rs:379-410` 的路由感知构造函数取得 Client；默认配置（未启用受管网络策略、出站代理策略为
  `ReqwestDefault`）下仍逐次调用新建 Client，只有受管网络策略或非默认代理策略才改用 `RouteAwareClientPool` 按路由复用，
  该分支不属于本条的默认范围。`core/src/client.rs:1299-1321` 的 `build_api_transport` 仍在每次调用里经 `create_client_for_route` 现场构造 Client，
  models 端点的 `RouteAwareModelsTransportBuilder`（`model-provider/src/models_endpoint.rs:292-312`）同样逐次构造；0.165.0 新增的
  `OpenAiModelsEndpoint::for_originator`（`model-provider/src/models_endpoint.rs:215-232`）只对 `codex_work_cca` 等非 CLI originator 起作用（`models-manager/src/originator.rs`），不进入本条范围。
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
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h1/role.rs:1608-1614`
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
  `tools/spec_source_deps/hyper-1.11.1/src/proto/h1/role.rs:1446-1455`、
  `tools/spec_source_deps/hyper-1.11.1/src/proto/h1/role.rs:1519-1548`。
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
  Lite 样本不强制该头。Lite 模型的默认 Responses 请求携带 `x-openai-internal-codex-responses-lite`，值为 `true`。
  0.163.0 复核：各端点的 header 构造顺序与 `HeaderMap`（http 1.4.0）行为均未变，header 名字面量集合不变；新增的
  `x-codex-turn-metadata` 值更长，不影响线序。
- **源码**：[L2] `tools/spec_source_deps/http-1.4.0/src/header/map.rs:923-928`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`；各端点的构造顺序由
  L1 `core/src/client.rs:1489-1519`、`core/src/client.rs:1884-1893`、
  `core/src/client.rs:2515-2521` 调用链决定；WS 握手头的构造见 `core/src/client.rs:1421-1455`。
  http 1.4.0 仍是锁定依赖（0.165.0 的 `Cargo.lock` 第 8903-8904 行；0.164.0 为第 8885-8886 行）。
- **实测**：`c1491-r14-f-lite-http-response/relay/conn005.client_to_upstream.bin`（R）验证冷启动 Lite Responses
  的原始线序。0.164.0 官方样本（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt
  `20261027T135446Z-d8a16a8e788de81e`；场景 A01、A03；匹配数不是去重请求数）：models 默认头线序（`variant=no_cookie`）
  `models-order` 匹配 2 条，header 名线序为上列 models 序；Responses 默认压缩头线序（`variant=http_default`）
  `responses-order` 匹配 1 条，线序落在上列允许集合内并含全部必含头；Lite 默认样本的
  `x-openai-internal-codex-responses-lite` 值为 `true`（`responses-lite-header-value` 匹配 1 条），
  `x-codex-routing-hint` 为 `model=gpt-6-astra`，即本轮 Lite 轨模型（`responses-routing-hint-value` 匹配 1 条）。候选断言
  `assert-SPEC-H1-004` 核对 models 线序、Responses 允许集合与 Lite 头、routing hint 取值；默认头合并时机见 0.157.0
  `http-client/src/client.rs` 第 121-129 行与第 163-170 行、`http-client/src/client_builder.rs` 第 357-392 行。
- **实现**：逐端点复刻最终线序，不得使用统一字典排序或一份 header 并集。
- **状态**：✅ 源码充分；抓包充分。

## 2.6 HTTP/2（仅自定义 CA）

### SPEC-H2-001 SETTINGS 参数顺序

- **范围**：自定义 CA 条件分支。
- **规则**：SETTINGS 帧按 `ENABLE_PUSH, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE,
  MAX_HEADER_LIST_SIZE` 输出，即参数 ID `2,4,5,6`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:115` 与
  `tools/spec_source_deps/h2-0.4.19/src/frame/settings.rs:213-259`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验，不用于计数或缺失命题。
- **实现**：只在有效自定义 CA 触发的 h2 分支复刻。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-002 ENABLE_PUSH

- **范围**：自定义 CA 条件分支。
- **规则**：`ENABLE_PUSH = 0`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:115`
  的 `enable_push(false)`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 2 写 0。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-003 INITIAL_WINDOW_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`INITIAL_WINDOW_SIZE = 2,097,152`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:49`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 4 写 2,097,152。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-004 MAX_FRAME_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`MAX_FRAME_SIZE = 16,384`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:50`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 5 写 16,384。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-005 MAX_HEADER_LIST_SIZE

- **范围**：自定义 CA 条件分支。
- **规则**：`MAX_HEADER_LIST_SIZE = 16,384`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:52`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：SETTINGS ID 6 写 16,384。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-006 首个连接级 WINDOW_UPDATE

- **范围**：自定义 CA 条件分支。
- **规则**：stream 0 的首个 WINDOW_UPDATE 增量为 `5,177,345`。
- **源码**：[L2] `tools/spec_source_deps/hyper-1.11.1/src/proto/h2/client.rs:48` 与
  `tools/spec_source_deps/h2-0.4.19/src/frame/settings.rs:43-44`。
- **实测**：`official-h2-20260727T131936Z`（J，3/3 完整连接）为主证据；
  `relay-h2-20260728T032147Z`（R）只作正向原始帧交叉核验。
- **实现**：复刻该连接窗口配置，不单独硬写无来源的帧常量。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-H2-007 请求伪头顺序

- **范围**：自定义 CA 条件分支。
- **规则**：`:method, :scheme, :authority, :path`。
- **源码**：[L2] `tools/spec_source_deps/h2-0.4.19/src/frame/headers.rs:713-743`、
  `tools/spec_source_deps/h2-0.4.19/src/hpack/encoder.rs:101-137`。
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
- **源码**：[L1] 注入入口 `model-provider-info/src/lib.rs:179`；[L2]
  `tools/spec_source_deps/tungstenite-openai-0.27.0/src/handshake/client.rs:190-206`。
- **实测**：`relay-wshdr3`（R）在同一运行取得 WS 正例与 HTTP 对照。
- **实现**：固定 OpenAI OAuth 上游无需实现；兼容 Codex 自定义 provider 时才实现。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-004 业务帧使用 permessage-deflate 与上下文接管

- **范围**：内置 OpenAI OAuth；WS 业务帧。
- **规则**：协商 `permessage-deflate`；压缩文本帧 RSV1 置位，payload 为 raw deflate，且解压上下文跨帧复用。每条应用层消息在线上只有一个数据帧：整条消息压缩后作为 FIN=1、RSV1=1 的单帧写出，不分片、没有续帧、没有空载荷的 FIN 帧；不论消息大小都压缩（没有长度门槛）。
- **源码**：[L1] `codex-api/src/endpoint/responses_websocket.rs:1003-1013`（`send_websocket_request` 整条序列化后单次 `send(Message::Text)`）；[L2] tungstenite fork`tools/spec_source_deps/tungstenite-openai-0.27.0/src/protocol/mod.rs:574-581`（`prepare_data_frame` 中 `IS_FINAL` 恒为真）、`tools/spec_source_deps/tungstenite-openai-0.27.0/src/extensions/compression/deflate/mod.rs:149-262`（整条消息一次压缩，去尾部 `00 00 ff ff`）。
- **实测**：`clean-tool-20260728T132346Z`（R）验证首字节、压缩 payload 与跨帧上下文。0.165.0 非正式实采（`sub2apiplus-evidence/codex-cli/0.165.0/informal/`（X 路 ARM64 非正式实采，没有账本与 Campaign；下称非正式实采））：124 条客户端消息全部是 1 个帧、FIN=1、RSV1=1、opcode 文本，没有任何分片、没有未压缩消息（最小 1.5 KB 的预热帧也压缩，最大 119 KB 仍是 1 帧）；每条连接的第一条消息能被全新解压器单独解开，之后各条不能（上下文接管在用，见 `ws-context-takeover-evidence.txt`）。
- **实现**：每条连接维护同一压缩／解压上下文；不得逐帧重置。候选出站在握手响应的底层连接上合并写方向的分片（`backend/internal/pkg/wsframemerge`），并把压缩门槛设为 1，使每条消息恰好一个 FIN=1、RSV1=1 的帧。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-WS-005 response.create 字段槽位与条件字段

- **范围**：内置 OpenAI OAuth；WS 业务帧。
- **规则**：字段槽位顺序为
  `type, model, stream, service_tier?, previous_response_id?, input, tools?, tool_choice,
  parallel_tool_calls, reasoning, store, stream_options?, include,
  prompt_cache_key?, text?, generate?, client_metadata?, access_programs?`；续接帧与首帧遵循同一顺序规则，
  都没有 `instructions` 槽位。`generate=false` 只用于 warmup（预热帧不带 `access_programs`）；`previous_response_id`
  只在既有响应前缀可复用时出现，连接键或账号 owner 变化、迟到的 tool-result metadata 都会使前缀不可复用。
  `access_programs` 的取值与出现条件见 SPEC-BODY-001。0.164.0 起 Lite 模型默认走增量工具（见 SPEC-BODY-003），
  预热帧（`generate=false`）的 `input` 是空数组 `[]`，顶层其余字段不变；工具目录与基础指令消息改由同一会话的第一条业务帧携带：该帧的
  `previous_response_id` 取预热响应的 id，`additional_tools` 与基础指令消息排在 `input` 最前，其后的增量帧只带新增项；
  全历史回退帧（没有 `previous_response_id`）带完整历史，前缀项的 id 与之前相同。从旧窗口续聊（历史里没有工具声明项）仍是旧形态。
  0.165.0：增量帧（带 `previous_response_id`）的 `input` 可含新增用户消息之后的 `configuration_update`；请求级 `reasoning.effort` 在窗口内保持基线，改 effort 不再改请求级 `reasoning`（0.164.0 及更早：改 effort 即改请求级 `reasoning`，使前缀不可复用，对应 `reasoning_changed`）。
  前缀不可复用时官方在**同一条连接**上整段重放。官方客户端每次发 WS 请求前先判断能否在上一次请求加其输出之上续链；以下任一情形不能续链：
  换模型、工具目录、`tool_choice`、`parallel_tool_calls`、推理档位（0.165.0 起为窗口内钉死的基线）、`store`、`stream`、`include`、`service_tier`、`prompt_cache_key`、`text` 任一变化；`input` 不再以上次请求加输出为前缀（`input_mismatch`／`input_shortened`，含上下文压缩后的新窗口）；0.165.0 起相邻 `configuration_update` 合并后与上次请求前缀不一致。
  此时客户端在同一连接上发一帧全量 `response.create`：不带 `previous_response_id`、不带 `generate`、`input` 为完整历史，不重新握手、不补预热。握手头（`x-codex-routing-hint` 的模型、`x-codex-window-id`）保持建连时的旧值，新模型与新窗口号只出现在每帧 `client_metadata`。压缩请求本身是增量帧（`previous_response_id` 加单项 `compaction_trigger`，`request_kind=compaction`），压缩完成后的下一轮才是全量帧，再下一轮恢复增量。只有连接键（`ResetOn`：认证代次、账号 owner、`base_url`）变化或连接已断才新建连接，新连接上同样直接发全量帧、不补预热。断线重连后的整段重放带窗口内最新一条 update；WS 且预热已完成时首轮不带 update。
  上下文压缩后进入新窗口：窗口号（`x-codex-window-id` 的 `<线程>:<窗口号>` 后缀、turn metadata 的 `window_id`／`window_number`）加一，`context_window_id` 换新；握手头只在建连时发出，复用的连接握手里仍是旧窗口号，新值只在每帧里；新建连接的握手用推进后的值。
  每条 `response.create` 消息的文本是 `serde_json` 序列化结果原样发出：没有末尾换行，字符串里的 `<`、`>`、`&`、U+2028、U+2029 按字面量写出，不转成 `\u003c` 等转义（非正式实采：官方两条消息都不以换行结尾，70967 字节那条消息里有 72 个字面 `<`）。
- **源码**：[L1] `codex-api/src/common.rs:331-357`、
  `core/src/client.rs:2120-2193`、`core/src/client.rs:1118`。续接前缀的判定由 `prepare_websocket_request`
  返回 `Result<Continuation, Failure>` 给出（0.163.0 起复用失败原因逐项记录，只影响遥测，不改变字段）。0.164.0 的预热入参
  来自 `core/src/session/startup_prewarm.rs` 第 398-433 行（`PrewarmInput::Base` 为空 `Vec`，增量窗口下 `tools` 与基础指令都被清空）；首个业务帧
  的增量项由 `get_incremental_items`（`core/src/client.rs` 第 1535-1566 行）取出，预热帧为空时整份 `request.input` 都是增量。官方单测
  `core/tests/suite/agent_websocket.rs` 的 incremental tools 用例断言预热帧 `input` 为 `[]`、业务帧续链预热响应且 `input` 含 `additional_tools`。
  整段重放的判定与构造：`core/src/client.rs:347-380`（`NonIncrementalReason`）、`core/src/client.rs:391-455`（`responses_request_properties_mismatch`）、`core/src/client.rs:1535-1566`、`core/src/client.rs:1578-1596`、`core/src/client.rs:1697-1760`（`websocket_connection` 只在 `responses_headers`／`connection_key` 变化、连接已关、认证 owner 变化时重建）、`core/src/client.rs:2184-2194`（全量帧构造）。官方单测 `core/tests/suite/client_websockets.rs` 第 2413-2461 行（`model_changed` 单连接）、第 2255-2300 行、第 2472-2570 行，`core/tests/suite/reasoning_effort_override.rs` 第 1017-1140 行（均为测试代码，仅作旁证）。
- **实测**：`clean-tool-20260728T132346Z`（R）覆盖 Lite；
  `audit-ws005-nonlite-20260730a`（R）覆盖非 Lite、warmup 与增量帧；这两份 R 类样本采集版本早于 0.164.0，字段顺序以
  0.164.0 官方样本为准，只用于说明 warmup 与增量帧的条件语义（其中预热帧携带前缀是 0.163.0 及更早的形态）。0.164.0 官方样本（Campaign
  `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：
  `field-slots` 匹配 16 条（场景 A05、A06 的 `response.create`），`warmup-input-empty` 匹配 3 条（预热帧 `generate=false` 且 `input` 为 `[]`）。
  `warmup-generate-false`、`incremental-prefix-reuse` 与七项 `first-business-frame-*`（首个业务帧带前缀并续链预热响应，以及逐项 passthrough 的键序、
  保留、`turn_id` 形态与等值）各匹配 0 条：这些是候选侧判据，按候选帧标签 `frame_role=first_business` 选帧，由 VC-5 候选断言核对，官方侧不适用。
  候选断言 `assert-SPEC-WS-005` 核对字段槽位、warmup 的 `generate=false` 且 `input` 为空、首个业务帧排在 `input` 最前的 `additional_tools` 前缀并以预热响应 id
  续链，以及增量帧只在可复用前缀时携带 `previous_response_id`；owner 变化时复位 WS 会话见 0.157.0
  `core/src/client.rs` 第 1538-1548 行。
  0.165.0 非正式实采（`sub2apiplus-evidence/codex-cli/0.165.0/informal/`（X 路 ARM64 非正式实采，没有账本与 Campaign；下称非正式实采））：m1（换模型 gpt-6.1-sol→gpt-6-astra→gpt-6.1-sol）、c1／c2（压缩后续聊）、e6（目录不支持 effort update 的 gpt-5.6-sol 改 effort）的整段重放全部在原连接上，握手只有一次且无新预热帧；握手里的 `x-codex-routing-hint` 与 `x-codex-window-id` 保持旧值（m1 的 hint 仍为首个模型，帧里 model 已换；c1 握手 `:0`、帧里 `:1`），c1 压缩后窗口号 `:0` 变 `:1`。124 条客户端消息均为 1 个帧。
- **实现**：按 serde 条件省略字段；不得把某一类样本的字段子集或“首轮／后续轮”写成固定规则。画像 `responses_ws` 的
  `Body.Fields` 按上列槽位重排并移除 `instructions`。画像 `BaseInstructions.IDScheme=uuidv7` 声明增量窗口形态：网关为第三方客户端派生的预热帧 `input` 为
  `[]`，首个业务帧保留 `additional_tools` 与基础指令消息（不再剥离），并以预热响应 id 续链。
  整段重放：画像 `WebSocketContinuation.ReplayOnSameConnection=true` 声明该形态。网关对本会话内自己发过 `generate=false` 预热帧的下游客户端（自管续链），其不带 `previous_response_id`、不带 `generate`、非工具结果续轮的全量帧复用当前上游连接原样转发：不新建连接、不补预热、不链 `previous_response_id`；复用的连接被对端关闭时换新连接重试同一帧，新连接上同样不补预热。从未自己发过预热帧的客户端（第三方每个用户轮次发整段历史）仍由“新连接＋网关预热＋链预热响应”承接，画像不声明该字段时一律如此。官方入站的整段重放帧里 `configuration_update` 在整段历史中原位原字节保留。官方出站的帧编译与最终写入不做 HTML 转义，也不追加换行（`marshalOpenAIWSReadonlyJSON`、`WritePreparedJSON`）。
  窗口推进：画像 `TurnMetadata.WindowTracking` 声明后网关读客户端原帧的窗口号推进；`CodexIdentityFacts.WindowAdvances` 令 invocation 摘要不再绑定窗口标识。已知偏差：窗口号来源是下游客户端帧里的窗口号，对不发 `x-codex-window-id` 的第三方客户端窗口号不推进（保持 `:0`）。
- **状态**：✅ 源码充分；抓包充分（0.165.0 非正式实采补充整段重放与改 effort 场景，正式场景随 0.165 登记补入）。

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
  `login/src/auth/default_client.rs:416-430`、
  `core/src/client.rs:1237-1246`、`core/src/client.rs:1281-1297`、
  `core/src/client.rs:1438-1439`、`core/src/client.rs:1884-1893`、
  `core/src/client.rs:2060-2065`、`core/src/client.rs:2483-2487`（guardian 审阅请求判定）。
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
- **源码**：[L1] `model-provider-info/src/lib.rs:51-66`、
  `login/src/auth/default_client.rs:46-47`、
  `login/src/auth/default_client.rs:456-469`、
  `config/src/config_requirements.rs:1084`、
  `exec/src/lib.rs:654`、`tui/src/startup_orchestration.rs:800`、
  `app-server/src/request_processors/initialize_processor.rs:191`。0.165.0 复核（迁移自 0.163.0 起如此，0.164.0 到 0.165.0 未变）：residency 全局状态与读写函数迁入
  `model-provider-info`（`login` 以别名重新导出），`ModelProviderInfo::to_api_provider` 另会把同一取值写入 provider 头
  （`model-provider-info/src/lib.rs:452-458`），结果头集合不变（0.149.1 由 `enforce_managed_residency` 做同样的事）。0.165.0 里 `default_headers()`（`login/src/auth/default_client.rs:456-469`）
  的 `originator`、`user-agent`、条件 residency 三项与顺序、三个设置入口（exec、tui、app-server）及受管配置项（`config/src/config_requirements.rs:1084`）均与 0.164.0 一致。
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
- **源码**：[L1] `core/src/client.rs:161`、`core/src/client.rs:1445`、
  `cli/src/doctor.rs:112`、`cli/src/doctor.rs:2360`。
- **实测**：`clean-tool-20260728T132346Z`（R）为 WS 正例；
  `audit-body002-plain-20260730a`（HTTP responses）、
  `clean-image-20260728T132405Z`（generations）、
  `relay-imgedit1`（edits）为 R 类反例。
- **实现**：只在 WS 握手添加；自定义 provider 主动注入同名头不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-005 user-agent suffix

- **范围**：内置 OpenAI OAuth。
- **规则**：UA 为平台前缀加可选 ` ({name}; {version})` suffix。exec 使用
  `codex_exec/0.164.0` 与 `(codex_exec; 0.164.0)`；TUI 使用
  `codex-tui/0.164.0` 与 `(codex-tui; 0.164.0)`。启动首次 models 因进程级写入时序，
  有 suffix 和无 suffix 均属于官方观测集；TUI initialize 之后的真实插件请求必须带 `codex-tui` 前缀与同名 suffix。
  0.163.0 复核：UA、originator 与 suffix 机制未改；exec 新增 `model/list` 后，首次 `/models` 更可能带 suffix，
  规则已允许有无两种。
- **源码**：[L1] `login/src/auth/default_client.rs:41`、
  `login/src/auth/default_client.rs:152`、
  `app-server/src/request_processors/initialize_processor.rs:131-192`、
  `cloud-tasks/src/util.rs:13`、`exec/src/lib.rs:268`。
- **实测**：`clean-search-20260728T132311Z`（exec）、`clean-legacy-20260728T132509Z`
  （TUI）、`audit-ep019-wham-consume-safe-20260730a`（`codex_exec` originator、
  `unknown` 终端标识）均为 R。0.164.0 官方 VC-1（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt
  `20261027T135446Z-d8a16a8e788de81e`）对场景 A15 的五项判据（`exec-identity`、`tui-identity`、
  `exec-startup-models-suffix-optional`、`tui-startup-models-suffix-optional`、`post-initialize-suffix-present`）各匹配
  0 条：这五项是候选侧判据，由 VC-5 候选断言核对，官方侧不适用。
- **实现**：按入口和进程状态生成 suffix；不得把一次首次 models 结果硬编码为固定值。ARM64 候选的平台前缀为
  `(Ubuntu 24.4.0; aarch64)`，suffix 的可选性和生成规则不变。
- **状态**：✅ 源码充分；抓包充分（R 类样本）；0.164.0 的身份判据是候选侧判据，由 VC-5 候选断言核对。

### SPEC-HDR-006 accept 按传输和端点变化

- **范围**：内置 OpenAI OAuth。
- **规则**：HTTP responses 为 `text/event-stream`；WS 握手无 `accept`；
  models、alpha-search、images generations／edits 为 `*/*`（reqwest 默认值，所处位置见 SPEC-HDR-002）。
- **源码**：[L1] HTTP responses 显式值见
  `codex-api/src/endpoint/responses.rs:143-146`；其余 `*/*` 是 reqwest 默认 wire 行为。
- **实测**：`audit-h1raw-20260730a`、`clean-tool-20260728T132346Z`、
  `clean-search-20260728T132311Z`、`clean-image-20260728T132405Z`、`relay-imgedit1`（R）。候选断言
  `assert-SPEC-HDR-006` 核对 HTTP Responses 为 SSE、WS 握手无 accept、辅助端点为 `*/*`。
- **实现**：按端点生成，不使用全局固定值。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-HDR-007 普通 Responses 的会话头

- **范围**：内置 OpenAI OAuth；普通 Responses（HTTP 与 WS 握手）。
- **规则**：发送小写连字符形式 `session-id`、`thread-id`；不发送 `session_id` 或 `conversation-id`。`session-id`
  取缓存亲和键：根会话取源会话 id 覆盖（临时 fork 为源会话的 session_id，guardian 审阅会话为 `guardian:{父线程}`），其次内部会话的
  `{来源}:{父线程}`，否则自身 session_id；非根 agent 取自身 session_id。0.164.0 复核：该取值改由 `session_derived_cache_key` 计算
  （`core/src/client.rs:666-678`、`core/src/client.rs:682-688`），不再等同于请求体 `prompt_cache_key`——后者受 `PromptCacheKey` 策略控制，
  策略只改请求体、不改这两个头；默认策略下两者取值与 0.163.0 相同。
  realtime 的 `x-session-id` 是独立分支（0.164.0 起 realtime 握手头在 `x-session-id` 之后另有 `voice-session-id`，仅当调用方给出语音会话 id 时发送；
  Sub2API 不实现 realtime 握手，不构成当前偏差）；alpha-search 不发送会话头。
- **源码**：[L1] `codex-api/src/requests/headers.rs:8`、
  `codex-api/src/requests/headers.rs:11`；realtime 见
  `core/src/realtime_conversation.rs:1979`、`core/src/realtime_conversation.rs:1982-1986`。
- **实测**：`audit-h1raw-20260730a`（responses）与 `clean-search-20260728T132311Z`（alpha-search 不发送）均为 R；
  0.157.0 官方普通 Responses 8 条均带 `session-id` 与 `thread-id`。候选断言 `assert-SPEC-HDR-007` 核对普通 Responses
  均含两头、所有请求不含 `session_id` 与 `conversation-id`、realtime 第一跳使用 `x-session-id`；取值见 0.157.0
  `core/src/client.rs` 第 571-593 行与第 1307-1310 行、`core/src/session/session.rs` 第 886-900 行。
- **实现**：严格按端点发送，不能把会话头扩散到 alpha-search；`session-id` 按上述缓存亲和键取值。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-008 四个条件 header

- **范围**：内置 OpenAI OAuth；仅在对应会话来源或 feature 成立时。
- **规则**：
  `x-openai-subagent` 来自子代理与内部会话（memory consolidation；0.163.0 起 `Internal(source)` 一律输出其名称，
  新增 `guardian`）；
  `x-openai-memgen-request: true` 来自内部 memory consolidation；
  `x-codex-parent-thread-id` 来自父线程；
  `x-responsesapi-include-timing-metrics: true` 来自 `runtime_metrics`。
- **源码**：[L1] `core/src/responses_metadata.rs:387-480`、
  `core/src/client.rs:873-906`、`core/src/client.rs:1437-1452`、
  `features/src/lib.rs:1326-1330`。0.165.0 复核：四个头的来源与取值未变——`subagent_header_value` 对 `Internal(source)` 仍输出来源名称
  （`core/src/responses_metadata.rs:470-487`），`InternalSessionSource` 仍只有 `MemoryConsolidation`、`Guardian`（`protocol/src/protocol.rs` 第 3055-3058 行、
  名称见同文件第 3226-3233 行），memgen 头仍只在 memory consolidation 会话写入（`core/src/client.rs:881-889`、`core/src/client.rs:898-906`），
  `runtime_metrics` 仍是开发中特性、默认关闭。
- **实测**：`relay-review4`、`audit-hdr008-guardian-20260730a`、
  `audit-hdr008-memgen-20260730a`、`relay-rtmetrics1`（R）。
- **实现**：按条件插入；`x-openai-subagent` 的 `Other(label)` 不得实现成封闭枚举。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-009 工作区路由 override 头

- **范围**：内置 OpenAI OAuth；ChatGPT 认证；Responses HTTP 与 WS 握手。
- **规则**：`GET /backend-api/wham/accounts/check` 给出非默认路由 override（`us`、`us_cr`）时，Responses 请求追加
  `x-openai-account-routing-override`；默认工作区路由（`NO_CONSTRAINT`）下 HTTP Responses 与 WS 握手都不携带该头。
- **源码**：[L1] 头名定义于 `model-provider/src/workspace_routing.rs:12`；`apply_workspace_routing` 在 `us`／`us_cr` 时写入该头、
  `NO_CONSTRAINT` 时移除（`model-provider/src/workspace_routing.rs:59-75`）；带该头的 provider 一律禁止跟随重定向
  （`core/src/client.rs:1305-1312`）。该机制在 0.149.1 不存在（对头名 0 命中）。
- **实测**：0.157.0 官方 A03（HTTP）与 A05（WS）默认路由样本均不带该头；候选断言 `assert-SPEC-HDR-009` 核对两类默认
  样本都不发送。`us`／`us_cr` 分支需要对应工作区账号，由官方源码闭环。
- **实现**：网关首期只做发现与失败关闭，画像在 WorkspaceRouting 记录头名与接受值。
- **状态**：🟡 源码充分；抓包有限，只覆盖默认路由。

### SPEC-HDR-010 guardian 审阅请求的 x-codex-guardian

- **范围**：内置 OpenAI OAuth；guardian 审阅请求（Responses HTTP 与 WS）。
- **规则**：guardian 审阅请求走普通 `/responses`，携带 `x-codex-guardian: reviewer`，不带 `x-codex-routing-hint`，
  请求体不带 `service_tier`；WS 握手中该头位于 `openai-beta` 之后、`sec-websocket-extensions` 之前。普通 Responses
  请求（HTTP 与 WS 握手）不携带 `x-codex-guardian`。
- **源码**：[L1] 头由 guardian 审阅会话作为 Responses 附加头注入（`ext/guardian-v2/src/sync_reviewer/mod.rs:101-108`，取值 `reviewer`），
  核心客户端按它判定：HTTP 的 `core/src/client.rs:1881-1893` 清空 `service_tier` 并跳过 routing hint，WS 的
  `core/src/client.rs:2051-2065` 同理，判定函数见 `core/src/client.rs:2483-2487`。0.163.0 另有取值 `classifier`
  （GuardianV2 异步评分器，`ext/guardian-v2/src/async_scorer/sampler/connection_pool.rs:409-411`），GuardianV2 是开发中特性、默认关闭。
  该机制在 0.149.1 不存在；0.164.0 官方样本同样未覆盖 guardian 审阅请求（`guardian-reviewer-value` 匹配 0 条，`http-guardian-absent` 匹配 1 条、`ws-guardian-absent` 匹配 5 条），头序与取值沿用 0.157.0 官方样本。
  0.165.0 复核：头的注入点（`ext/guardian-v2/src/sync_reviewer/mod.rs:101-108`，与 0.164.0 逐字节相同）与三处判定未变，仓库内 `x-codex-guardian` 字面量仍只出现在注入点、
  `classifier` 取值处与判定函数三处，没有新取值；0.165.0 新增的开发中特性 `GuardianV3` 与 `GuardianRecordedItems`（均默认关闭）不改变该头（后者只给审阅会话的 turn metadata 加键，见 SPEC-BODY-009）。
- **实测**：0.157.0 官方 guardian 审阅 WS 握手为 `…x-codex-turn-metadata, x-codex-parent-thread-id,
  x-openai-subagent, openai-beta, x-codex-guardian, sec-websocket-extensions`，取值 `reviewer`，没有 routing hint；
  A03、A05 普通请求均不带该头。候选断言 `assert-SPEC-HDR-010` 核对取值与两类普通请求不发送。
- **实现**：只在 guardian 审阅请求上生成；该请求不生成 routing hint、不带 service_tier。
- **状态**：✅ 源码充分；抓包充分。

## 2.9 Body

### SPEC-BODY-001 Responses 顶层字段由传输结构体封闭

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：HTTP 由 `ResponsesApiRequest` 序列化；WS 由
  `ResponseCreateWsRequest` 序列化并增加外层事件类型。不得发送对应结构体外字段。0.165.0 复核（`codex-api/src/common.rs` 与 0.164.0 逐字节相同）：封闭字段集与顺序自 0.163.0 起为：HTTP 为
  `model, stream, service_tier?, input, tools?, tool_choice, parallel_tool_calls, reasoning, store, stream_options?,
  include, prompt_cache_key?, text?, client_metadata?, access_programs?`；WS 顺序相同，另在首位增加 `type`，在 `input` 之前
  增加 `previous_response_id?`，在 `client_metadata?` 之前增加 `generate?`（完整槽位见 SPEC-WS-005）。`access_programs`
  是尾部可选字段：官方默认路径上，除预热帧（`generate=false`）外每轮都带 `{"cyber":"standard"}`；原因是 0.165.0 的 TUI 与
  exec（`exec/src/daybreak.rs:71-80`，0.165.0 只给不适用的 Gcp 账号加了一个分支）在 ChatGPT 账号、模型目录通告 `cyber` 含 `standard` 且 Daybreak 开关未开时，每轮都选择 `standard`（本轮采集账号
  目录里的全部模型都通告）。
- **源码**：[L1] `codex-api/src/common.rs:278-357`（`ResponsesApiRequest` 为第 278-304 行，`ResponseCreateWsRequest` 为第 331-357 行）。
  与 0.149.1 相比，两个结构体都不再有 `instructions`，`model`、`stream`、`service_tier` 前置，尾部新增 `access_programs`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（HTTP Lite）、
  `audit-body002-plain-20260730a`（HTTP 非 Lite）、
  `clean-tool-20260728T132346Z`（WS Lite）、
  `audit-ws005-nonlite-20260730a`（WS 非 Lite），均为 R；这四份 R 类样本采集版本早于 0.164.0，只用于说明封闭结构体
  原则（其中非 Lite 样本还含顶层 `instructions`），字段集与顺序以 0.164.0 官方样本为准。0.164.0 官方样本（Campaign
  `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：
  `http-closed-fields` 匹配 8 条（场景 A03、A04 的 HTTP Responses），`ws-closed-fields` 匹配 16 条（场景 A05、A06 的
  `response.create`）。本轮官方样本均为 Lite 模型（主线 gpt-6.1-sol、Lite 轨 gpt-6-astra），没有非 Lite 模型样本。
  `unknown-sentinel-absent`（刻意注入的未知顶层 sentinel 不会出现在候选出站）匹配 0 条：这是候选侧判据，由 VC-5 候选断言
  核对，官方侧不适用。
- **实现**：分别按两个结构体和 serde 省略条件生成，不使用统一 body 超集。画像侧由 `responses_http`／`responses_ws` 的
  `Body.Fields` 重排（`model`、`stream`、`service_tier` 前置，移除 `instructions`，尾部增加可选 `access_programs`），
  `access_programs` 由画像 `AccessPrograms` 节（允许 `standard`、默认 `standard`）与补丁逐项承载。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-002 请求压缩策略

- **范围**：内置 OpenAI OAuth。
- **规则**：`enable_request_compression` 为 Stable 特性且默认开启：ChatGPT 认证下普通 Responses 默认使用 zstd
  （`content-encoding: zstd`）；官方显式关闭压缩时明文，不发送 `content-encoding`。其余端点均不压缩（见 SPEC-EP-005）。
  0.165.0 复核（条件自 0.163.0 起未变，0.164.0 到 0.165.0 亦未变）：压缩条件仍是特性开启、Codex 后端认证且 OpenAI 提供方（`core/src/client.rs:1770-1779`），
  `enable_request_compression` 仍为 Stable 且默认开启（`features/src/lib.rs:1482-1485`）；压缩前字节因请求体形态变化而不同，策略不变。
- **源码**：[L1] `features/src/lib.rs:1482-1485`、
  `core/src/session/session.rs:1827`、`http-client/src/request.rs:41-43`、
  `core/src/client.rs:1770-1779`（压缩判定：特性开启、Codex 后端认证且 OpenAI 提供方才用 zstd）。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（zstd responses）与 `audit-body002-plain-20260730a`（关闭压缩），
  均为 R。0.164.0 官方样本（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt
  `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：场景 A03 默认压缩的 Responses POST 带
  `content-encoding: zstd`（`responses-zstd` 匹配 1 条）；场景 A04 的压缩关闭样本来自官方显式关闭压缩，官方在该配置下
  不发送该头，`responses-plain`（压缩关闭样本里出现 zstd 以外 `content-encoding` 的 Responses 请求数须为 0）匹配 0 条。
  该判据经 0.163.0 的 VC-5（attempt `20261022T085520Z-60fd5f255335c599`）批准修订（approval-revision，评估基线 b2）定为现行写法。
  候选断言 `assert-SPEC-BODY-002` 核对开启时 Responses 为 zstd，以及压缩关闭样本不出现 zstd 以外的 `content-encoding`。
- **实现**：只压缩 responses。Sub2API 候选在 Lite 主线固定呈现官方默认配置（`enable_request_compression` 默认开启）：
  普通 Responses 统一发 `content-encoding: zstd`，不跟随入站关闭压缩，因此压缩关闭样本可以带 zstd，但不得出现 zstd
  以外的 `content-encoding`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-003 Lite 模式变换

- **范围**：内置 OpenAI OAuth；模型 manifest 的 `use_responses_lite=true`。
- **规则**：instructions/tools 迁入 `input.additional_tools`；
  `reasoning.context=all_turns`；`parallel_tool_calls=false`；`additional_tools` 项只在工具目录非空时加入，键序为
  `type, id, role, tools`。增量工具（`incremental_tools`）自 0.164.0 起是 Stable 特性且默认开启，对 Lite 模型默认生效：
  工具目录与基础指令消息在新窗口开头作为历史条目记录一次，`input` 首项是 `additional_tools`（`id` 为 `at_` 加记录时随机生成的
  UUIDv7），第 2 项是基础指令消息（键序 `type, id, role, content, internal_chat_message_metadata_passthrough`，`id` 为 `msg_` 加随机
  UUIDv7；逐项 passthrough 键序为 `turn_id, create_time, content_item_kinds, content_item_metadata`），此后每个请求随历史原样重放同一对
  id，不再按“线程 id 加内容字节”现场重算 UUIDv5。工具集中途变化时，历史中间插追加只含变化定义的 `additional_tools`，不改前缀；
  压缩之后是新窗口，重新记录一对新 id。窗口是否增量只看历史里有没有工具声明项：从旧窗口续聊的请求仍是旧形态（前缀每次现场重算，
  id 为 UUIDv5），所以同一客户端会同时发出两种前缀形态，不能按版本号二选一。WS 的前缀只在第一条业务帧出现（预热帧的
  `input` 为空，见 SPEC-WS-005），其后的增量帧不重复发送。0.165.0 起 Lite 请求的 `input` 项集合另可含 `{"type":"configuration_update","reasoning":{"effort":…}}`（无 `id`、无逐项 passthrough，位于触发它的用户消息之后，不属于前缀；见 SPEC-BODY-006）。它不改变 `additional_tools` 与基础指令消息的位置、`id` 与键序。
- **源码**：[L1] `core/src/client.rs:957-977`、
  `core/src/client.rs:1016-1031`、`core/src/client.rs:1118`。0.165.0 源码（相对 0.164.0 仅行号平移）：`features/src/lib.rs` 第 1665-1670 行把
  `incremental_tools` 设为 Stable 且默认开启；`core/src/session/world_state.rs` 第 44-56 行按历史是否含工具声明项判定窗口是否增量；
  增量窗口里 `prompt.tools` 与 `prompt.base_instructions` 被清空（`core/src/session/turn.rs` 第 1691-1709 行），所以请求构造里按
  工具目录与基础指令现场拼装的两段不触发；`ResponseItemId::new` 为前缀加 `Uuid::now_v7()`（`protocol/src/response_item_id.rs`
  第 15-22 行），在项入历史时补 id（`core/src/session/mod.rs` 第 3633-3655 行）；`AdditionalTools` 的字段顺序见
  `protocol/src/models.rs` 第 1052-1060 行。0.163.0 默认路径（增量工具关闭）仍按旧方式现场拼装：`additional_tools` 项 id 为 `at_` 加
  UUIDv5（命名空间为 `UUIDv5(NAMESPACE_OID, thread_id)`，名称为该项 `tools` 的 JSON 序列化字节），0.149.1 无 `id` 且总是加入。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（HTTP）、
  `clean-tool-20260728T132346Z`（WS），均为 R，采集版本早于 0.164.0，只用于说明工具目录迁移的位置与 WS 增量语义。0.164.0 官方样本
  （Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：
  主线场景 A04（`gpt-6.1-sol`）的请求上，`main-additional-tools-item-form`、`main-base-instructions-item-form`、
  `main-base-instructions-passthrough-keys`、`main-passthrough-key-orders`、`main-passthrough-assistant-key-order`、
  `main-passthrough-carrier-types`、`main-messages-keep-passthrough`、`main-passthrough-turn-id-form`、
  `main-current-turn-item-turn-id`、`main-client-turn-id-matches-turn-metadata` 各匹配 3 条；`lite-transform` 与
  `incremental-no-repeat` 各匹配 0 条：这两项是候选侧判据，官方侧不适用。候选断言 `assert-SPEC-BODY-003` 核对 Lite 变换、首项
  `additional_tools` 与第 2 项基础指令消息的键序和 id 形态（id 为前缀加规范 UUID，不核对版本位与具体值）、逐项 passthrough 的键序与携带者类型、
  项级 `turn_id` 的规范形态且请求尾部项的 `turn_id` 等于出站 `client_metadata.turn_id`（后者又等于 `x-codex-turn-metadata` 里的 `turn_id`），以及 WS
  增量帧不重复首个业务帧已带的前缀。
- **实现**：由模型 manifest 驱动；不得把 Lite 变换应用到非 Lite 模型。画像 `BaseInstructions` 节的 `IDScheme=uuidv7` 声明增量窗口方案
  （缺省保持 UUIDv5 旧语义，旧画像字节不变）：入站前缀项是官方形态（带 `at_`／`msg_` 前缀 id 且类型匹配）时原样保留，不重写 id；第三方客户端入站时由网关合成，
  `additional_tools` 项带 `at_` 加稳定的 UUIDv7 形态 id（同一会话各请求相同），键序 `type, id, role, tools`，工具目录为空时不合成该项。
  0.165.0：官方入站的 `configuration_update` 不是前缀项，原样放行；网关把它归入所在回合，历史回合 `turn_id` 的派生不受影响。
  逐项 passthrough 在官方出站、画像 `uuidv7`、入站为官方形态三个条件同时满足时按项原样保留（含 `content_item_metadata`；Lite 与非 Lite
  各按自己的官方形态判定），缺任一条照旧整体剥离；客户端自带的逐项 `turn_id` 校验后改写成网关本轮的 `turn_id`，其余键逐字节保留，网关不为第三方入站合成逐项 passthrough。
  HTTP 出站的 `client_metadata.turn_id` 取自登记的 turn metadata，与其内的 `turn_id` 恒等。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-004 turn-state 的消费、保存与回送

- **范围**：派生／内部机制。
- **机制**：HTTP 从初始响应头 `x-codex-turn-state` 读取；WS 从 `response.metadata.headers` 读取。保存到当前 turn
  后，后续 HTTP responses 通过 header、WS 通过 `client_metadata` 原样回送。账号 owner 变化时清空已保存的
  turn-state，下一请求不回送。
  0.165.0：turn-state 的作用域是回合——官方每个回合新建模型客户端会话（`core/src/client.rs:694-709`、`core/src/session/turn.rs:194-195`），同一回合内的后续请求（工具结果续轮、同回合压缩）回送本回合已收到的值，新用户轮次的第一个请求不回送上一回合的值；预热帧响应里收到的值随预热过的会话交给第一个回合，所以紧跟预热帧的业务帧仍回送。
- **源码**：[L1] `codex-api/src/sse/responses.rs:66-74`、
  `codex-api/src/endpoint/responses_websocket.rs:849-852`、
  `core/src/client.rs:2112-2119`、`core/src/client.rs:2495-2511`、`core/src/client.rs:694-709`、`core/src/session/turn.rs:194-195`（回合作用域，见机制）。
- **实测**：`audit-ep014-turnstate-echo-20260730a` 与 `audit-body004-ws-turnstate-20260730a`（R）完成 HTTP、WS
  两条输入→保存→回送闭环。候选断言 `assert-SPEC-BODY-004` 核对回送值不变、两条通道均闭环、owner 变化后不回送；
  清空见 0.157.0 `core/src/client.rs` 第 1538-1542 行。
- **实现**：Sub2API 若终结上下游流，必须保存并回送；透明转发时不得丢失或重复生成；账号 owner 变化时清空。画像 `TurnState.ResetOnNewTurn=true` 时网关在新用户轮次（非工具续轮、非首帧、上一帧不是客户端预热帧）先清空已保存的值；不声明保持旧行为。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-005 tool_choice 是字符串

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：`tool_choice` 为 JSON 字符串，当前值 `"auto"`，不是对象。
- **源码**：[L1] `codex-api/src/common.rs:289`、`core/src/client.rs:1117`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `clean-tool-20260728T132346Z`、`audit-ws005-nonlite-20260730a`（R）。
- **实现**：所有传输和 Lite／非 Lite 分支均序列化为字符串。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-006 HTTP Responses 的 Lite／非 Lite 字段

- **范围**：内置 OpenAI OAuth；HTTP（`reasoning.effort` 的数字档位形态对 WS `response.create` 同样适用）。
- **规则**：字段全集由 `ResponsesApiRequest` 定义。Lite 省略顶层
  `instructions/tools`（工具目录放入 `input` 的 `additional_tools` 项）、强制 `parallel_tool_calls=false` 并加入
  `reasoning.context=all_turns`；Lite 的 `reasoning.effort` 取客户端配置或模型清单默认，且须是清单支持的档位，
  `reasoning.summary` 按清单默认 `none` 以 Option 省略，`text.verbosity` 为 `low`。非 Lite 保留顶层 `tools`，
  `parallel_tool_calls` 取 prompt 值，但顶层不再有 `instructions`：基础指令改为 `input` 最前的 developer 消息
  （`id` 为 `msg_` 加 UUIDv5(UUIDv5(NAMESPACE_OID, thread_id), 正文)，`passthrough` 仅带
  `content_item_kinds=[model.base_instructions]`）。Option 字段为空时省略。`reasoning.effort` 为已知档位或不能按 u64
  解析的自定义值时发 JSON 字符串；自定义档位能按 u64 解析时（如 `"3"`）发 JSON 整数，HTTP 与 WS `response.create` 共用同一
  结构体、形态相同；`x-codex-turn-metadata` 的 `reasoning_effort` 仍记字符串。默认内置模型目录的档位全是字符串，
  整数形态只在用户配置或远端目录给出纯数字自定义档位时出现。0.163.0 起主线模型 gpt-6.1-sol 是 Responses Lite。
  0.164.0：字段全集与顺序未变，Lite 请求里的基础指令消息与工具目录由增量窗口作为历史条目承载（见 SPEC-BODY-003），消息 id 由 `msg_` 加
  UUIDv5 改为记录时随机的 `msg_` 加 UUIDv7，其逐项 passthrough 为 `turn_id, create_time, content_item_kinds, content_item_metadata`；载体（`input` 最前的
  developer 消息、`content_item_kinds=[model.base_instructions]`）与顶层不带 `instructions` 的结论不变。非 Lite 不走增量工具，基础指令消息仍由请求构造现场拼出，
  形态不变。请求体另有三处变化——① 开启 `reasoning_effort` 覆盖时，`input` 中连续的 `ConfigurationUpdate` 项只保留最后一条
  （`core/src/client.rs:991-1007`）；② Lite 请求里带来源归因的 `input` 消息，其 `internal_chat_message_metadata_passthrough` 在
  `content_item_kinds` 之外另带逐项对齐的 `content_item_metadata` 数组（基础指令消息为 `[{"provenance":{"type":"harness","hook":false}}]`），
  非 Lite 请求在发出前清除该数组（`core/src/client.rs:1043-1048`），所以下文非 Lite 基础指令消息的 `passthrough` 仍只带 `content_item_kinds`；
  ③ 请求体 `prompt_cache_key` 改由 `PromptCacheKey` 策略决定（默认 `SessionDerived`，取值与 0.163.0 相同；宿主可配 `Custom` 或 `Omit`，`Omit` 时整字段省略，
  `core/src/client.rs:658-664`），官方 CLI 默认路径不受影响。② 由 SPEC-BODY-003 的 `main-base-instructions-passthrough-keys` 在 A04 官方样本上核对（匹配 3 条）；
  ① 与 ③ 依据源码，官方样本的判据不核对。
  0.165.0 复核：字段全集与顺序未变（`codex-api/src/common.rs` 与 0.164.0 逐字节相同）；`reasoning.summary` 增加提供方能力判断（`core/src/client.rs:967`，`model-provider/src/capabilities.rs:14-15`，默认 `true`），内置 OpenAI 不受影响。
  但 ① 的前提变了：`reasoning_effort_override` 由开发中改为默认开启（`features/src/lib.rs:2019-2024`；0.164.0 默认关闭，请求副本里的 `ConfigurationUpdate` 项被整体过滤，`core/src/client.rs:991-994`）。
  同时满足下列条件的会话生效（`core/src/client.rs:581-587`、`core/src/client.rs:626-629`）：provider 名恰为 `OpenAI`（`model-provider-info/src/lib.rs:649-651`，自定义名称的 provider 不受影响）、
  模型目录 `supports_reasoning_effort_updates=true`（内置 gpt-6.1-sol、gpt-6-astra 与新增的 gpt-6.1-terra）、不是 guardian／记忆合并会话和临时标题线程。生效时：
  ① 会话内改 effort，客户端**不改请求级 `reasoning.effort`**，而在 `input` 里追加 `{"type":"configuration_update","reasoning":{"effort":"<档位>"}}`
  （项只有 `type`、`reasoning.effort` 两个成员，没有 `id`，也没有 `internal_chat_message_metadata_passthrough`；`core/src/session/reasoning_effort.rs:28-91`，项定义见 `protocol/src/models/configuration_update.rs`），位于触发它的用户消息之后；
  ② 请求级 `reasoning.effort` 在一个上下文窗口内被钉在窗口第一次采样时的值（`core/src/session/reasoning_effort.rs:94-135`）。官方单测 `core/tests/suite/reasoning_effort_override.rs` 第 738-826 行（测试代码，仅作旁证）：
  四个回合 effort 依次 medium→high→（不变）→low，四个请求的 `reasoning.effort` 全是 `medium`，`input` 依次多出 update(medium)、update(high)、update(low)；
  ③ 因此“这次请求按什么 effort 跑”＝`input` 里最后一条 `configuration_update.reasoning.effort`，没有则取请求级值（下称生效 effort）；`x-codex-turn-metadata.reasoning_effort` 记的是用户当前选择（`core/src/turn_metadata.rs:70`），不是钉死的基线；
  ④ 自定义档位（`Custom(v)` 且 `v != "disabled"`，含能按 u64 解析的数字档位）不产生 update，请求级 effort 也不钉死（`core/src/session/reasoning_effort.rs:137-157`）；`ultra` 先按模型解析为实际档位，`persistent` 写作 `disabled`（`protocol/src/openai_models/reasoning_effort.rs` 第 10-40 行）；
  ⑤ 首轮是否带 update 取决于窗口是否已有钉子，**按 provider 形态判定，不是“有／无都合法”**：有 WS 预热的路径（TUI／exec 默认路径，含 WS 握手被拒后回退 HTTP）在首轮之前已由 `core/src/session/startup_prewarm.rs:442` 钉死基线，首轮**不带**（非正式实采 16/16）；
  名为 `OpenAI` 但 `supports_websockets=false` 的 provider（正式 HTTP 采集用的 `openai-http-probe`）没有预热，首轮 `input` 末尾**必带** `{"type":"configuration_update","reasoning":{"effort":"<请求 effort>"}}`（非正式实采 2/2），之后各请求随历史重放时它仍在首个用户轮次之后；
  ⑥ 压缩成功后钉子重置、旧 update 随历史压缩消失，下一次采样以当前选择重建基线；换模型时钉子按模型 slug 记，新模型第一次采样重建基线，新旧两条 update 都在 `input` 里（`core/tests/suite/reasoning_effort_override.rs` 第 1395-1452 行，测试代码，仅作旁证）；压缩请求自身的 effort 优先取钉死值（`core/src/compact_remote_v2.rs:299`、`core/src/compact_remote_v2.rs:337`）；
  ⑦ 发请求前连续的 update 只留最后一条（`core/src/client.rs:996-1006`），该帧 `input` 与上次请求前缀不符，WS 上因此改发**同一连接上、无 `previous_response_id` 的整段历史**，下一帧恢复增量续链（`core/tests/suite/reasoning_effort_override.rs` 第 1017-1140 行，测试代码，仅作旁证；见 SPEC-WS-005）。
  目录里 `supports_reasoning_effort_updates=false` 的模型（如 gpt-5.6-sol）改 effort 不产生 update，请求级 `reasoning.effort` 直接变化，WS 上同一连接发全量帧（非正式实采 e6）。本条判据语义不变。
- **源码**：[L1] `codex-api/src/common.rs:278-304`、
  `core/src/client.rs:957-1131`、`core/src/client.rs:991-1007`、`core/src/client.rs:1043-1048`、`core/src/client.rs:658-664`（`build_responses_request` 从第 980 行起，对任何模型都不再产生顶层 `instructions`）、
  `codex-api/src/common.rs:170-183`（数字档位序列化，内容与 0.160.0 一致）、
  `core/src/session/reasoning_effort.rs:28-157`、`core/src/client.rs:581-587`、`core/src/client.rs:626-629`、`core/src/client.rs:991-1006`、`core/src/session/turn.rs:567`、`core/src/session/startup_prewarm.rs:442`、
  `core/src/state/session.rs:35-70`、`core/src/turn_metadata.rs:60-91`、`features/src/lib.rs:2019-2024`（`reasoning_effort` 覆盖，见规则）。Lite 分支的端到端断言见
  `core/tests/suite/responses_lite.rs` 第 92-157 行（测试代码，仅作旁证）。turn metadata 字符串化见 0.160.0
  `core/src/turn_metadata.rs` 第 83-93 行。
- **实测**：`audit-ep014-turnstate-echo-20260730a`（Lite）与
  `audit-body002-plain-20260730a`（非 Lite），均为 R；这两份 R 类样本采集版本早于 0.163.0，其非 Lite 样本里的顶层
  `instructions` 在 0.163.0 起不再出现，不作为 0.164.0 非 Lite 的佐证。0.164.0 官方样本（Campaign
  `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：Lite 轨场景
  A03 的请求 `body.model` 为 gpt-6-astra（`lite-model-is-lite-track-model`），顶层无 `instructions` 与 `tools`
  （`lite-top-level-omission`、`lite-tools-omission`），`reasoning.context=all_turns`（`lite-reasoning-manifest-defaults`），
  `reasoning.effort` 为清单支持的档位（采集客户端配置为 low，`lite-reasoning-effort-present`），`reasoning.summary` 省略
  （`lite-reasoning-summary-omitted`），`text.verbosity=low`（`lite-text-verbosity-low`），以上各判据均匹配 1 条；主线场景
  A04（gpt-6.1-sol，0.163.0 起为 Lite 模型）顶层无 `instructions`（`main-instructions-absent` 匹配 3 条），工具目录在
  `input` 的 `additional_tools` 项里（`main-additional-tools-present` 匹配 3 条）；数字档位定向样本中 `reasoning.effort`
  为 JSON 整数 `3`（A04 HTTP 的 `numeric-effort-http-integer` 匹配 1 条，A05 WS `response.create` 的
  `numeric-effort-ws-integer` 匹配 2 条）；0.165.0 起这些数字档位样本的 `input` 类型里**没有** `configuration_update`（判据 `numeric-effort-http-no-configuration-update`、`numeric-effort-ws-no-configuration-update` 由 0.165 期望清单承载，官方侧匹配数待 VC-1 回填）。本轮官方采集的主线与 Lite 轨都是 Lite 模型，非 Lite
  分支（基础指令作为 `input` 最前的 developer 消息）没有本轮官方样本：gpt-5.5 在采集账号上已下线，0.164.0 内置目录再无
  非 Lite 模型；该分支的结论依据 0.164.0 与 0.165.0 源码（`core/src/client_common.rs` 两版逐字节相同），不用 0.160.0 的非 Lite 样本充当 0.164.0 的佐证。候选断言
  `assert-SPEC-BODY-006` 核对 Lite 省略顶层字段、Lite 的 reasoning 与 text 定型、主线请求无顶层 `instructions` 且工具目录在
  `additional_tools`，以及数字档位在 HTTP 与 WS 均为整数。
  0.165.0 非正式实采（`sub2apiplus-evidence/codex-cli/0.165.0/informal/`（X 路 ARM64 非正式实采，没有账本与 Campaign；下称非正式实采））：有 WS 预热的路径 16 个场景首轮都不带 `configuration_update`；`supports_websockets=false` 的 OpenAI provider（h1、h2）首轮 `input` 末尾带 update(请求 effort)；数字档位（n1、n2）不产生 update，请求 `reasoning.effort` 为整数 `3`；采集账号目录（真实上游 `GET /models?client_version=0.165.0`）里 gpt-6.1-sol、gpt-6-astra、gpt-6.1-terra、gpt-6-sol、gpt-6-luna 的 `supports_reasoning_effort_updates` 为 true，gpt-5.6-* 与隐藏模型为 false；窗口内改 effort（e1、e5）三个业务帧的请求级 `reasoning.effort` 全是同一个钉死值，`input` 末尾依次多出 update；update 的 JSON 为 `{"type":"configuration_update","reasoning":{"effort":"high"}}`，键序 `type, reasoning`（抽查 5 个场景 13 处，除取值外字节完全一致）。正式样本的匹配数由 VC-1 回填，对应判据 `configuration-update-http-first-turn`、`configuration-update-ws-absent-without-effort-change`。官方抓包未核实：上游确实按最后一条 update 执行。
- **实现**：按模型 manifest 与 Option 值序列化，不硬编码某个模型的一次字段子集。数字档位由画像 ReasoningEffort 节
  （`CustomNumericSerialization=u64_integer`）驱动：能按 u64 解析的档位以 JSON 整数出站，turn metadata 仍记字符串；
  节缺省的画像一律发字符串。非 Lite 请求不再发送顶层 `instructions`，基础指令作为 `input` 最前的 developer 消息，
  由画像新增的 `BaseInstructions` 节承载；`responses_http`／`responses_ws` 的 `Body.Fields` 均不再含 `instructions`。
  Lite 请求里的基础指令消息与 `additional_tools` 的 id 方案由该节的 `IDScheme=uuidv7` 声明，处理方式见 SPEC-BODY-003。
  0.165.0（网关）：官方入站（HTTP 与 WS）的 `configuration_update` 项原样透传，不补 `internal_chat_message_metadata_passthrough`、不改位置、不参与逐项 `turn_id` 定型（WS 的豁免不论画像版本，旧画像同样适用）。网关内所有“按什么 effort 跑”的读点统一取生效 effort：用量日志（`reasoning_effort`、`requested_reasoning_effort`）、渠道 `reasoning_effort_multipliers` 计费倍率、首包超时、官方 turn metadata 的 `reasoning_effort`；分组 effort 上限／映射／拒绝策略同样作用于每条 `configuration_update`（否则用户在窗口中途改档即可绕过上限）。WS 增量帧（`previous_response_id` 续链）的 `input` 只含新增项，之前帧里的升档靠连接状态沿用（有 update 则记住；无 update 且续链则沿用；首帧、整段重放清掉；预热帧不动），“是否续链”按客户端帧判断。转换到 Chat Completions／Anthropic 的路径上游不认识该项，转换前把最后一条 update 的 effort 折进请求级 effort。
  第三方入站按 provider 形态合成：**HTTP 出站**（画像 `ReasoningEffort.ConfigurationUpdate=http_first_turn`，对应官方不带 WS 预热的 provider）在模型目录声明 `supports_reasoning_effort_updates`、请求 effort 是已知档位、入站不是官方形态（入站不带官方形态的基础指令消息）、会话不是 guardian／记忆合并时，于“首个用户轮次之后、第一个模型输出项之前”（首轮即 `input` 末尾）合成 `{"type":"configuration_update","reasoning":{"effort":"<请求 effort>"}}`（键序固定，预编码 JSON），之后各请求随第三方客户端重放的历史在同一位置带同一条，同一窗口各请求 `input` 保持前缀关系；`input` 里已有 update、有压缩项、数字档位时不合成；**WS 出站**不合成（首轮由网关补的预热帧钉死，与官方一致）。第三方中途改档时 update 取值随当前请求变化（官方是请求级钉死加追加新 update），登记为已知偏差。
- **状态**：✅ 源码充分；抓包充分（Lite 分支）。非 Lite 分支没有本轮官方样本，结论依据 0.164.0 与 0.165.0 源码（该路径两版一致）。

### SPEC-BODY-007 编码工作流 input 类型分布记录

- **范围**：采集与观测记录；固定十轮编码任务。
- **记录**：20 个含 input 的请求／帧共 377 项，最大长度 95；类型为
  `message 134`、`reasoning 87`、`custom_tool_call_output 79`、
  `custom_tool_call 71`、`additional_tools 6`。这是历史采集记录，不改数字；0.165.0 起 `input` 类型另有 `configuration_update`（会话内改 effort 时出现，见 SPEC-BODY-006），本节记录的 0.157 样本不含该类型。
- **源码**：[L3] 场景统计无 L1/L2 协议常量。
- **实测**：`audit-body007-workflow-clean-20260730a`（R），20/20 连接双向完整。
- **实现**：不实现这些计数；仅用来确认测试场景确实包含真实工具调用。
- **状态**：✅ 源码无／不适用；抓包充分。

### SPEC-BODY-008 client_metadata 的 guardian_credits_requested 与 mcp_attribution

- **范围**：内置 OpenAI OAuth；Responses（HTTP body 与 WS `response.create`）。
- **规则**：普通会话每个 Responses 请求的 `client_metadata` 携带 `guardian_credits_requested`（字符串 `"true"`）与常量
  `mcp_attribution`（字符串 `{"status":"none"}`）；guardian 审阅请求同样写入 `mcp_attribution`。
  0.165.0：WS `response.create` 的 `client_metadata` 另带逐请求动态键：`root_turn_id`（回合帧，预热无）、`ws_request_header_x_openai_internal_codex_responses_lite`（本请求模型走 Lite 时为 `"true"`）、`x-codex-ws-stream-request-start-ms`（发送前一刻的 Unix 毫秒，每个 WS 请求都带，含预热帧）。预热帧 10 键，回合帧（含压缩帧）11 键。
- **源码**：[L1] `mcp_attribution` 的键常量与写入见 `core/src/responses_metadata.rs:48`、`core/src/responses_metadata.rs:371-382`，
  `guardian_credits_requested` 作为保留键见 `core/src/responses_metadata.rs:68-71`，写入条件（非 guardian 审阅、ChatGPT 认证、
  Codex 后端路由）见 `core/src/client.rs:1248-1279`。该机制在 0.149.1 不存在（对 `guardian_credits_requested` 0 命中）。
  0.163.0 起 `client_metadata` 里的内部元数据只发给 `api.openai.com` 与 ChatGPT 允许主机（`model-provider/src/provider.rs:122-130`），
  自定义 provider 默认不带。0.165.0 复核：以上写入与判定未变；保留键清单（只阻止 app-server 覆盖）另增 `guardian_recorded_items`
  （`core/src/responses_metadata.rs:70`、`core/src/responses_metadata.rs:112`），该键属于 turn metadata，不进入 `client_metadata` 顶层，见 SPEC-BODY-009。
- **实测**：0.157.0 官方 HTTP Responses 请求体 `client_metadata.mcp_attribution` 为字符串 `{"status":"none"}`、
  `guardian_credits_requested` 为 `"true"`，WS `response.create` 各场景均带 `mcp_attribution`。0.164.0 官方样本（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：
  `http-guardian-credits`、`http-mcp-attribution` 各匹配 4 条（场景 A03、A04 的 HTTP Responses），`ws-guardian-credits`、`ws-mcp-attribution` 各匹配 16 条（场景 A05、A06 的
  `response.create`）。候选断言 `assert-SPEC-BODY-008` 核对 HTTP 与 WS 两类载体的两项取值。
- **实现**：两项都按字符串写入，不得省略或改为布尔值、对象。画像 `ClientMetadata.DynamicKeys` 声明后由编译器写入动态键（`root_turn_id` 与 Lite 标记取自帧事实，时间戳编译该帧时取当前时间）。已知偏差：Lite 标记按连接冻结的 `egressContext.responsesLite` 写，官方按每帧模型写，同一连接上 Lite 与非 Lite 模型互换时会不一致（未核实，旧行为同）；HTTP 路径官方 `client_metadata` 也带 `root_turn_id`，网关 HTTP 尚未补。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-009 x-codex-turn-metadata 的键集合

- **范围**：内置 OpenAI OAuth；携带 `x-codex-turn-metadata` 的 Responses 请求。
- **规则**：`x-codex-turn-metadata` 的键是画像 `TurnMetadata.Keys` 允许集合（22 键）的有序子集，必含
  `analytics_enabled, history_initialization, installation_id, model, reasoning_effort, request_kind, sandbox, session_id,
  thread_id, thread_source, turn_id, turn_started_at_unix_ms, turn_trigger, window_id`（14 键）；允许集合另含 `agent_name,
  auto_review_enabled, context_window_id, node_repl_auto_review_required, node_repl_disabled, root_turn_id,
  sandbox_mode, window_number`（8 键）。默认配置下 `analytics_enabled` 为 `true`。`history_initialization` 对所有带元数据的
  Responses 请求恒在，取值有 6 种（如 `new`），出站位置在 `request_kind` 与 `root_turn_id` 之间。
  `turn_id` 是 UUIDv7（`Uuid::now_v7()`，同一进程内单调；`rand_a`（12 位）的第 2、3 位（自低位起，从 0 计）恒为 0，这是该库编码实现的特征），前 48 位是提交该回合的毫秒时间；`turn_started_at_unix_ms` 是任务启动时读的系统时钟，比 `turn_id` 内嵌毫秒晚 0～7 毫秒（官方实采 143 个回合：0 毫秒 2、1 毫秒 60、2 毫秒 37、3 毫秒 24、4 毫秒 13、5 毫秒 4、6 毫秒 2、7 毫秒 1；另有 1 个 29 毫秒的离群值，出现在采集机另有一路 TUI 争用 CPU 时，不计入分布）。
  预热帧（`request_kind=prewarm`）的 `turn_id` 为空串且没有 `turn_started_at_unix_ms`。
  `session_id`、`thread_id`、`prompt_cache_key`、`x-codex-window-id` 的前缀、`context_window_id`、`at_`／`msg_` item id 的 UUID 部分都是同一进程内单调的 UUIDv7，布局同 `turn_id`（`rand_a` 第 2、3 位恒为 0）；根线程的 `session_id` 与 `thread_id` 与 `x-client-request-id` 取值相同；`window_id` 是线程 id 加 `:0`；`installation_id` 是 UUIDv4。官方实采：358 个不同 UUIDv7 全部满足，`x-client-request-id` 与 `thread-id`、`session-id` 三者相同 90/90。子代理线程的 `thread_id` 与 `session_id` 不同、窗口号也可能不是 `:0`，上述相同关系只对根线程成立。
- **源码**：[L1] 键常量见 `core/src/responses_metadata.rs:46-47`（`analytics_enabled`、`history_initialization`）与
  `core/src/responses_metadata.rs:58`（`turn_trigger`），`model` 与 `reasoning_effort` 键见 `core/src/turn_metadata.rs:47-49`；
  载荷字段与出站顺序见 `core/src/responses_metadata.rs:417-467`，`history_initialization` 的取值与来源见
  `history/src/initialization.rs:12-34`、`core/src/session/session.rs:790-794`；回合 id 与开始时间见 `core/src/session/mod.rs:1206-1208`（`new_submission_id` 即 `Uuid::now_v7()`）、
  `core/src/tasks/mod.rs:306-322`（任务启动时 `mark_turn_started` 读时钟）、`core/src/turn_timing.rs:91-101`、`core/src/turn_timing.rs:224-229`；端到端断言见
  `core/tests/suite/direct_tool_metadata.rs` 第 921-952 行（测试代码，仅作旁证）。`turn_trigger` 在 0.149.1 不存在。0.165.0 复核：键常量、载荷字段顺序与 `history_initialization` 的六种取值未变；载荷新增一个条件键
  `guardian_recorded_items`（`core/src/responses_metadata.rs:556-557`，排在载荷首位；仅当 guardian 审阅会话的父会话安装了事件观察者时为 `true`，
  `core/src/session/session.rs:730`、`core/src/guardian/review_session_setup.rs:191`，由开发中、默认关闭的 `GuardianRecordedItems` 特性控制），
  不在上列 22 键允许集合内，普通会话不出现，不改变本条规则。
- **实测**：候选断言 `assert-SPEC-BODY-009` 核对：键为 22 键的有序子集、必含上列 14 键，默认配置
  `analytics_enabled=true`。13 个既有必含键与默认 `analytics_enabled=true` 的官方依据沿用 0.157.0 官方样本的封存断言
  （键为 21 键的有序子集、必含 13 键）；新增的 `history_initialization` 由源码确认（0.163.0 起，0.165.0 复核仍成立）。`turn-metadata-keys` 与
  `turn-metadata-analytics-default` 两项判据在 0.164.0 官方 VC-1（Campaign `c01640-formal-vc1-r1-20261027t000000z`，
  attempt `20261027T135446Z-d8a16a8e788de81e`）各匹配 0 条：这是候选侧判据，由 VC-5 候选断言核对，官方侧不适用。
- **实现**：网关生成有可信取值的键：基础 14 键；画像 `WindowTracking` 声明时另生成 `window_number`、`context_window_id`；画像 `RootThreadKeys` 声明时，根线程另生成 `agent_name`（`/root`）与 `root_turn_id`（回合帧，等于 `turn_id`，预热无）。`sandbox_mode`、`auto_review_enabled`、`node_repl_auto_review_required`、`node_repl_disabled` 取决于用户本地配置或模型目录，网关没有可信来源，维持不生成。压缩请求（`WSCompactionRequests`）：`request_kind=compaction`，带 `compaction{trigger,reason,implementation,phase,strategy}` 对象，不带 `model`、`reasoning_effort`、`turn_trigger`。画像 `TurnMetadata.Keys`
  增加 `history_initialization`（字典序位于 `context_window_id` 与 `installation_id` 之间）。网关生成回合 id 时按该库的位布局与单调上下文编码（内嵌毫秒回拨 7 毫秒），开始时间由 `turn_id` 推出：内嵌毫秒加以 `turn_id` 为种子、按上述 143 个样本的分布确定性抽取的 0～7 毫秒（29 毫秒离群值不计入）；会话、线程、item id 的内嵌毫秒另回拨 50 毫秒，使其总早于首个回合 id。
- **状态**：✅ 源码充分；抓包充分（13 个既有必含键沿用 0.157.0 官方样本，新增的 `history_initialization` 由源码确认，0.165.0
  复核仍成立）。

## 2.10 端点与辅助链

### SPEC-EP-001 生图工具呈现与独立 images 调用

- **范围**：内置 OpenAI OAuth；模型支持图像生成时。
- **规则**：非 Lite 可在顶层 `tools` 中发送 namespace `image_gen`／工具
  `imagegen`；Lite 将能力放入 `input.additional_tools` 的 exec 工具目录。
  模型调用后由客户端请求独立 `images/generations` 或 `images/edits`。0.164.0 起该独立请求的 header 另带
  `x-codex-turn-metadata`（值为当前 turn 的元数据 JSON，`image_request_headers` 在有 turn 元数据时插入），0.163.0 没有；
  两个回合头的线序位置已由 0.164.0 官方样本核对，见 SPEC-EP-022。
- **源码**：[L1] `core/src/tools/spec_plan.rs:103-104`、
  `tools/src/tool_spec.rs:22-45`、`ext/image-generation/src/backend.rs:80-132`、
  `ext/image-generation/src/backend.rs:135-155`、`codex-api/src/endpoint/images.rs:62-98`。
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
- **令牌刷新请求体**：`POST https://auth.openai.com/oauth/token`（JSON）的请求体由 `BTreeMap` 编码，键按字典序；0.164.0 起在
  `grant_type` 与 `refresh_token` 之间多一个 `refresh_reason`，即 `{"client_id":…,"grant_type":"refresh_token","refresh_reason":"<原因>","refresh_token":…}`，
  请求头不变。原因是闭集四值：`proactive_expiry`（access token 的 JWT `exp` 距现在不足 5 分钟）、`scheduled`（`exp` 不可解析且 `last_refresh`
  早于 8 天）、`unauthorized_recovery`（401 恢复流程里的刷新步）、`forced`（显式调用，例如 app-server `getAuthStatus(refreshToken:true)`）；
  它是调用方自报的诊断信息，不是令牌有效性证据。
- **Files 规则**：`POST /backend-api/files` 的基础 Body 为 `file_name, file_size, use_case`；hosted
  connector 调用必须再同时发送 `codex_connector_id, codex_action_name, codex_model`，三者必须全有
  或全无。随后 PUT 必须逐字使用 create 响应返回的完整 URL；PUT 遇 502／503／504 或传输错误最多重试 5 次，全部尝试共享
  5 分钟截止，每次使用新的 `x-ms-client-request-id`。最后 POST `/backend-api/files/{file_id}/uploaded`：create
  响应没有 `pdf_c2pa_reservation=true` 时发送空对象，条件成立时只发送
  `pdf_c2pa_create_request`，其值与本次 create 请求 JSON 等值。`status=retry` 复用 finalize invocation
  轮询。成功响应含
  `file_size_bytes` 时以它为最终大小，缺失时回退到请求大小。
- **源码**：[L1] `model-provider-info/src/lib.rs:531-534`、
  `login/src/auth/manager.rs:223`、`core/src/realtime_conversation.rs:1375-1392`、
  `core/src/mcp_tool_call.rs:522-530`、`core/src/mcp_openai_file.rs:191-250`、
  `codex-api/src/files.rs:29-42`、`codex-api/src/files.rs:128-202`、`codex-api/src/files.rs:203-353`、
  `codex-api/src/files.rs:355-432`。0.164.0 的刷新原因见 `login/src/oauth/client.rs` 第 39-62 行（`RefreshReason` 四个取值）与第 97-113 行
  （请求体拼装），调用点见 `login/src/auth/manager.rs` 第 1695-1720 行（原因入参）、第 2087-2101 行（401 恢复取 `UnauthorizedRecovery`）与第 3311-3329 行（主动刷新取
  `ProactiveExpiry` 或 `Scheduled`），显式调用取 `Forced`。
- **实测**：`oauth-ep002-allhosts`、`oauth-ep002-refresh`（P）；
  `audit-ep012-sideband-synth-20260730a`、
  `audit-ep002-file-upload-full2-20260730a`（R）；0.149.1 hosted 三元字段与大小优先级由官方源码测试、
  Sub2API 三跳集成测试及缺字段负例共同闭环。0.151.0 的 C2PA 正反样本由 Formal r8 的
  `official-relay-file-upload-c2pa-negative／positive` 两个 Job 闭环。0.164.0 官方样本（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；
  匹配数不是去重请求数）：`oauth-refresh-body-keys` 匹配 1 条（A13 刷新请求体键序为 `client_id, grant_type, refresh_reason, refresh_token`），
  `oauth-refresh-reason-closed-set` 匹配 1 条（`refresh_reason` 在四值闭集内；官方驱动走 `getAuthStatus(refreshToken:true)`，只能采到 `forced`，其余三个取值依据源码与官方单测）。
  候选断言 `assert-SPEC-EP-002` 核对四类域名
  SNI、create 返回 URL 与 PUT URL 一致，C2PA 正反与 retry 三种 uploaded Body，以及刷新请求体键序和 `refresh_reason` 取值；工作区路由见 0.157.0
  `app-server/src/request_processors/account_processor/workspace_routing.rs` 第 253-290 行，blob PUT 重试见
  `codex-api/src/files.rs` 第 200-345 行。
- **实现**：画像 `oauth_refresh` 端点的 `Body.Fields` 为 `client_id, grant_type, refresh_reason, refresh_token`（`Closed`），`refresh_token` 由编译器注入；
  `refresh_reason` 在服务内做四值闭集校验：后台刷新器取 `proactive_expiry`，其余入口（含管理端手动刷新）取 `forced`；网关没有“401 后刷新再重试”的路径，
  所以不产生 `unauthorized_recovery`。旧画像不声明该字段时不发送。使用配置或服务端返回 URL；不得硬编码单一区域上传 host。create 与 uploaded 分别冻结
  Body attestation，uploaded 的 retry 只复用自身 invocation，避免把 hosted create 条件扩散到空 Body。工作区路由的
  发现与失败关闭见 SPEC-HDR-009；blob PUT 的重试网关首期不仿真。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-005 只有 responses 可使用请求压缩

- **范围**：内置 OpenAI OAuth。
- **规则**：responses 的流式请求路径可设置 compression；models、alpha-search、images 均走不带
  compression 的 execute 路径并明文发送。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:63-154`、
  `codex-api/src/endpoint/responses.rs:130-148`、
  `codex-api/src/endpoint/models.rs:97-115`、
  `codex-api/src/endpoint/search.rs:34-44`、
  `codex-api/src/endpoint/images.rs:62-98`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、`audit-body002-plain-20260730a`、
  `audit-h1raw-20260730a`、
  `clean-search-20260728T132311Z`、`clean-image-20260728T132405Z`（R）。
- **实现**：不得给非 responses 端点添加 `content-encoding: zstd`。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-006 models 的 URL 与方法

- **范围**：内置 OpenAI OAuth。
- **规则**：`GET {base}/models?client_version=0.164.0`。0.163.0 复核：URL、方法与 query 形态不变，`client_version`
  的值随 CLI 版本；exec 新增 `model/list`（`OnlineIfUncached`），可能使冷 home 的首个 `/models` 提前发出并变为同步等待，
  请求形态不变。0.164.0 复核：`model/list` 改为用 catalog 当前快照里的 config 与 models_manager 取列表
  （`list_models_from_snapshot`），刷新策略仍是 `OnlineIfUncached`，URL、方法、query 不变。
- **源码**：[L1] `codex-api/src/endpoint/models.rs:33-46`、`codex-api/src/endpoint/models.rs:103-115`；请求在 `model-provider/src/models_endpoint.rs:154-158` 经路由感知 transport 发出，
  `model/list` 的触发见 `app-server/src/request_processors/catalog_processor.rs:296-304` 与 `exec/src/daybreak.rs:41-50`。
  0.163.0 另在 `codex-api/src/endpoint/models.rs:48-67` 增加 `model_catalog_url`（自定义 provider 专用）的 URL 构造，内置
  provider 不受影响。
- **实测**：`audit-h1raw-20260730a`（R）验证 URL、方法与 query 形态。0.164.0 官方样本（Campaign
  `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；匹配数不是去重请求数）：场景 A09 的
  `models-method-path-query` 匹配 17 条，method 为 GET，path 为 `/backend-api/codex/models`，query 只有
  `client_version=0.164.0`。
- **实现**：版本 query 与画像对应的 CLI 版本一致（0.164.0）。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-008 alpha-search 的 URL 与方法

- **范围**：内置 OpenAI OAuth；触发 web search。
- **规则**：`POST {base}/alpha/search`。
- **源码**：[L1] `codex-api/src/endpoint/search.rs:15`、`codex-api/src/endpoint/search.rs:34-47`。
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
  `core/src/realtime_conversation.rs:1375`、
  `core/src/realtime_conversation.rs:1442-1463`、
  `core/src/realtime_conversation.rs:1968`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:1038-1174`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:1328-1414`、
  `core/src/realtime_conversation/sideband.rs:60-87`。0.163.0 复核：第一跳成功后的 sideband 链未变；另增“附着已有通话”路径
  （`ConversationStartTransport::ExistingCall`，通话由客户端创建、Codex 不发第一跳，`codex-api/src/endpoint/realtime_websocket/methods.rs:1058-1088`
  在 404 时按 100／300／800 ms 重试），sideband URL 的 call_id 改为按路径段追加并拒绝 `.`／`..`；该路径不属于本条范围。
  0.164.0 复核：sideband 链未变，仅 `methods.rs` 内部转写缓冲改为按帧序淘汰（不影响握手与第一跳），握手头另有 `voice-session-id`（见 SPEC-HDR-007）。
- **实测**：`webrtc-20260728T134028Z`（R）自然第一跳返回 400；
  `live2-20260728T140403Z`、`audit-ep012-realtime-20260730a`（R）自然第一跳返回 403；
  `audit-ep012-sideband-synth-20260730a`（R）以受控 200 触发官方客户端第二跳。
- **实现**：具备 realtime 条件后按双跳链实现；当前不得把受控 200 写成生产自然成功。
- **状态**：🟡 源码充分；抓包有限。Voice/realtime 自然成功补采暂缓。

### SPEC-EP-013 内置 provider 的 query 边界

- **范围**：内置 OpenAI OAuth；由 `Provider::url_for_path()` 构造的 Codex API URL。
- **规则**：provider 级 `query_params=None`；query 只由端点自身添加。目前 models
  添加 `client_version=0.164.0`，realtime/calls 添加 `intent` 与 `architecture`，
  普通 responses 不带 query。0.163.0 复核：行为不变，provider 级 `query_params` 仍为 `None`。
- **源码**：[L1] `codex-client/src/provider.rs:55`、
  `model-provider-info/src/lib.rs:543`、
  `codex-api/src/endpoint/realtime_call.rs:213-224`。上列 `model-provider-info` 的引用即 `query_params: None`；
  `Provider` 与 `url_for_path` 在 0.163.0 移入 `codex-client`（原 `codex-api/src/provider.rs`）；models 的 `client_version`
  由 `codex-api/src/endpoint/models.rs:37-46` 追加，realtime/calls 的 query 见 `codex-api/src/endpoint/realtime_call.rs:221-222`。
- **实测**：`audit-h1raw-20260730a`（models／responses）与
  `webrtc-20260728T134028Z`（realtime），均为 R。0.164.0 官方样本（Campaign
  `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；场景 A01、A03、A11；匹配数不是去重
  请求数）：`models-query-boundary` 匹配 10 条，models 的 query 只有 `client_version=0.164.0`；`realtime-query-boundary`
  匹配 1 条，realtime calls 只有 `intent=quicksilver` 与 `architecture=avas`；`responses-no-query` 匹配 3 条，场景 A03 的普通
  Responses 不带 query。
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
  `ext/web-search/src/tool.rs:120-130`、`ext/web-search/src/tool.rs:195-205`。
  0.164.0 复核：线序与 body 未变；`x-codex-turn-metadata` 的值改由 `current_meta_value_for_extension_request` 生成（`core/src/turn_metadata.rs:282-289`），
  在原先的 MCP 模板之外带上已配置的 Responses 元数据（该元数据由嵌入宿主经 `Config` 传入，官方 CLI 默认为空），默认下键集合与 0.163.0 相同（未经官方样本核实）。
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
  `backend-client/src/client/rate_limit_resets.rs:69-153`、
  `backend-client/src/client.rs:293-312`、`backend-client/src/client.rs:574-591`、
  `backend-client/src/client.rs:763-767`。最终线序仍由 wire 确认。0.165.0 复核：WHAM 路径与头构造未变（上列行区间逐行与 0.164.0 相同）；
  0.165.0 新增的 `Client::for_config_endpoint` 与 `config_bundle_url` 在自定义配置服务器基址下改发 `/codex/config/bundle`（`backend-client/src/client.rs` 第 209-224 行、第 562-572 行），
  仅供受管配置包发现，不改变内置 OpenAI OAuth 的 WHAM 路径。
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
  0.165.0：压缩请求自身的 `reasoning.effort` 优先取窗口钉死的基线；压缩成功后钉子重置、旧 `configuration_update` 随历史压缩消失；WS 上压缩后的下一帧是无 `previous_response_id` 的整段重放，再下一轮恢复增量
  （非正式实采 c1、c2：压缩请求本身是增量帧 `previous_response_id`＋单项 `compaction_trigger`，`request_kind=compaction`，其 turn metadata 没有 `model`、`reasoning_effort` 键；压缩完成后的整段重放帧在同一连接上）。
  WS 上的压缩请求是 `previous_response_id` 加单项 `compaction_trigger` 的增量帧：请求体不带 `access_programs`，turn metadata 如 SPEC-BODY-009 所述。
- **源码**：[L1] `core/src/compact_remote_v2_attempt.rs:172`、
  `model-provider/src/capabilities.rs:21-44`、
  `features/src/lib.rs:2151-2156`、`core/src/tasks/compact.rs:63-76`。0.165.0 复核（该状态自 0.163.0 起如此，0.164.0 到 0.165.0 未变）：`RemoteCompactionSupport` 只剩 `V2` 与 `Unsupported`（V1 与 `/responses/compact`
  路径已移除），默认能力为 `Unsupported`，仅 OpenAI 与 Azure Responses 提供方在 `ProviderCapabilities::from_config`
  （上列 `capabilities.rs` 第 33-44 行）里提升为 `V2`；内置 OpenAI OAuth 不受影响，自定义 provider 默认改走本地压缩。0.165.0 里 `ProviderCapabilities`
  另增 `reasoning_summaries` 字段（默认 `true`），与压缩无关；`remote_compaction_v2` 仍是 Removed 键，`x-codex-beta-features` 仍恒含它
  （`core/src/session/mod.rs:1317-1329`，已启用的实验特性集合未变）。
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
  0.164.0 起两者 header 线序均为
  `version, x-codex-image-turn-id, x-codex-turn-metadata, authorization, chatgpt-account-id, content-type, originator,
  user-agent, accept, cookie, host, content-length`：`x-codex-image-turn-id`（本次图像请求的 turn id）与 `x-codex-turn-metadata`
  （值为当前 turn 的元数据 JSON）紧跟 `version`；官方 CLI 默认路径上元数据是 14 键，键序为 `session_id, thread_id, reasoning_effort, turn_id,
  model, thread_source, turn_trigger, sandbox, sandbox_mode, auto_review_enabled, node_repl_auto_review_required, node_repl_disabled,
  turn_started_at_unix_ms, codex_version`（`reasoning_effort` 在 `turn_id` 前、`model` 在 `turn_id` 后是 `swap_remove` 的副作用）；默认路径下官方不写
  `originator` 头，由 HTTP 客户端默认头补在 `content-type` 之后，`accept` 位于 `originator`、`user-agent` 之后。body 不变。
- **源码**：[L1] `codex-api/src/images.rs:6-31`、
  `codex-api/src/endpoint/images.rs:62-98`、
  `ext/image-generation/src/tool.rs:484-636`（`request_for_call_args` 在第 489-493 行选 background，`recent_images`、
  `output_images` 取按数量引用的图片），图片引用类型见 `protocol/src/models.rs:929-935`（`ImageReference`，untagged）。
  0.163.0 另在响应头读取 `x-codex-imagegen-request-id` 供界面条目使用（`codex-api/src/endpoint/images.rs:101-116`），不影响出站。
  0.164.0 复核：请求体（generations／edits 的字段与取值、`images` 的两种引用形态）未变，`request_for_call_args` 只是多返回入参图片的路径与 sha256 供界面使用；
  出站变化有二：header 另带 `x-codex-turn-metadata`（见 SPEC-EP-001，线序已由 0.164.0 官方样本核对）；生成结果会交给附件库上传（`UploadResult::File` 时界面条目引用 file_id，失败时退回内联，
  `ext/image-generation/src/tool.rs:259-292`），这是 0.164.0 新增的出站面，是否经 Files 端点及其形态未经官方样本核实，不改变 images 请求本身。另外 `ext/image-generation/src/backend.rs:141-143`
  在 0.163.0 已有 `x-codex-image-turn-id` header，0.160.0 官方样本的线序没有它；0.164.0 官方样本确认它与 `x-codex-turn-metadata` 一同紧跟 `version`。
- **实测**：`clean-image-20260728T132405Z`（generations）与 `relay-imgedit1`
  （edits），均含 R；0.160.0 官方 generations 与 edits 样本线序为不含两个回合头的十项，generations 默认 `background=opaque`
  （2 条）、要求透明时为 `transparent`（1 条），edits 首项为 file_id（1 条）或 image_url（2 条），background 均为
  `opaque`。0.164.0 官方样本（Campaign `c01640-formal-vc1-r1-20261027t000000z`，attempt `20261027T135446Z-d8a16a8e788de81e`；场景 A09；匹配数不是去重请求数）：
  `image-header-order` 匹配 41 条（generations 与 edits 的 header 线序都是上列十二项），`image-turn-metadata-default-keys` 匹配 41 条
  （`x-codex-turn-metadata` 为上列 14 键且键序一致），`generation-body-order` 与 `edit-body-order` 各匹配 2 条，`generation-background-default-opaque`
  匹配 2 条；`edit-json-data-url`、`generation-background-transparent`、`edit-file-id-reference`、`edit-file-id-without-url` 各匹配 0 条，
  这些取值形态沿用 0.160.0 官方样本，由候选断言核对。候选断言 `assert-SPEC-EP-022` 核对两者线序、两个回合头与 14 键默认元数据、两种 body、edits 的 JSON data URL、
  background 两种取值与 file_id 项不带 image_url；accept 位置由默认头合并时机决定（见 SPEC-HDR-002）。
- **实现**：`n=None` 时省略；edits 以内联 data URL 发送，远程 URL 拒绝。background 与 file_id 引用由画像
  ImageGeneration 节驱动：入站要求透明背景时发 `transparent`，其余发节内默认值 `opaque`；节允许 file_id 时入站
  file_id 引用以 `{"file_id": …}` 发出，否则失败关闭。节缺省的画像 background 原样透传、edits 只收 image_url。两个回合头由画像 `images_generations`、`images_edits` 端点按槽位声明
  （`x-codex-image-turn-id` 为 Slot 20，`x-codex-turn-metadata` 为 Slot 25），只在画像声明时生效，旧画像字节不变：`x-codex-image-turn-id` 取本次调用的 turn id；
  `x-codex-turn-metadata` 按官方扩展请求规则装配，入站带同回合 Responses 事实时取其会话字段，没有时（非 Codex 客户端直接调图像接口）由画像
  `TurnMetadata.SessionDefaults` 登记的默认会话模型补 `model`、`reasoning_effort` 与 `node_repl_auto_review_required`，凑齐 14 键，漏登记会静默退回 12 键。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-023 压缩选择与 reason

- **范围**：派生／内部机制。
- **机制**：TokenBudget 分支不产生摘要出站；远程 provider 恒选择 V2；非远程 provider 才可能走 local inline。
  reason 为 `user_requested`、`context_limit`、`model_downshift`、`comp_hash_changed`。遥测 implementation 标签只有
  `responses`、`responses_compaction_v2`，phase 包含 `post_turn`，二者都不能与运行时分支一一等同。输入已含
  compaction_trigger 时不再次追加。
- **源码**：[L1] `analytics/src/facts.rs:461-473`、
  `core/src/tasks/compact.rs:53-96`、
  `core/src/compact_token_budget.rs:16-68`、
  `core/src/session/turn.rs:1393-1615`、
  `core/src/compact_model_fallback.rs:43-52`、
  `core/src/compact_remote_v2_attempt.rs:172`。0.165.0 复核（以下分派自 0.163.0 起如此，0.164.0 到 0.165.0 未变；`core/src/session/turn.rs` 只新增
  用户提示遥测与一个 `PrivateSessionEvent` 匹配分支，与压缩选择无关；0.165 起会话内改 effort 走 `configuration_update`，只影响压缩请求携带的 effort，不影响选择与 reason）：四种 reason 与两种遥测 implementation 标签（`responses`、
  `responses_compaction_v2`）未变，`ResponsesCompact` 标签已随 V1 移除；预压缩新增异步压缩候选
  （`async_compaction`，开发中特性、默认关闭；`core/src/session/turn.rs:1405-1422`，候选就绪即按 `ContextLimit` 触发），
  模型降档与 `comp_hash` 变化合并为同一 reason 分派（`core/src/session/turn.rs:1438-1520`）。新增的稳定特性
  `compaction_image_budget`（默认开）在压缩后重装历史时按预算保留图片（`core/src/compact_remote_v2_installation.rs:192-202`），
  只影响后续请求 `input` 的内容，不改变字段结构，本轮未评估对仿真的影响。
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
- **源码**：[L1] `tui/src/slash_command.rs:41`、
  `tui/src/chatwidget/slash_dispatch.rs:332`。
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

当前 active strict wire 是 Codex CLI 0.164.0；自动同步只更新候选值，active ReleaseCatalog 只能经证据验收后显式发布。

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

## 3.2 Codex 0.164.0 active 画像与发布执行契约

active／previous 画像均以内容寻址 Snapshot 保存 exec／TUI 身份、feature、端点、Header／Body
闭集与顺序、压缩、TLS、连接、条件状态和文件上传编排：

| mode | 版本与画像摘要 | 端点闭集 | 用途 |
|---|---|---|---|
| active | 0.164.0；`871526df931ac6c0330886fef1a25ac98db1ac3e2cf4cf849828c296a0df3d9d` | 16 个静态端点（含 `wham_settings_user`、`wham_accounts_check`，与 0.163.0 相同）+ 1 个 ReturnedURL 动态端点；`BaseInstructions` 节新增 `IDScheme=uuidv7`（增量窗口方案：Lite 前缀的 id 是记录时随机的 UUIDv7，WS 预热帧 `input` 为空）；`oauth_refresh` 端点的 Body 闭集在 `grant_type` 与 `refresh_token` 之间增加必填 `refresh_reason`；`images_generations`、`images_edits` 两端点按槽位增加回合头 `x-codex-image-turn-id`（Slot 20）与 `x-codex-turn-metadata`（Slot 25）；`TurnMetadata` 节新增可选的 `SessionDefaults`（图像请求无同回合事实时补默认会话模型，凑齐 14 键） | 生产默认 |
| previous | 0.163.0；`3da20c7b13f93d98d2181b1de30b9e5cc4f86c1f6d5be94bc6d03cfc642618b6` | 16 个静态端点（含 `wham_settings_user`、`wham_accounts_check`）+ 1 个 ReturnedURL 动态端点；可选节 AccessPrograms、BaseInstructions 与 ReasoningEffort、ImageGeneration | 受控回滚和历史复算 |

当前 Active 的官方目标身份为 tag `rust-v0.164.0`（commit
`6aaac694803004a63c6f393d153c96d22521a6b9`）、`aarch64-unknown-linux-musl` 包
SHA-256 `5918add6c7e9cf1e14a9af1b4c0f4bf1d24a43903faac26df5dc4c019ea91bd5` 和 ARM64 二进制
SHA-256 `8813c8d8a4cea2eeba271b72ab22f49628b184b24ab18dbef06b4ce41e9a8d39`。原始证据源为 Campaign
`c01640-formal-vc1-r1-20261027t000000z` 的 attempt `20261027T135446Z-d8a16a8e788de81e`；
47 个官方 Job 全部 complete。官方证据由该 Campaign 的 VC-1 自采，后续分类、验收、生产激活与 0.160.0 退休由同一
Campaign 的 canonical 链承接（候选 `c01640-candidate-r1`，attempt `20261027T232211Z-5290aafbe759fbdc`；
43 项迁移为 inherit 38、change 5，目标规则 43 条；VC-5 候选断言经两次评估基线承接：b1 为工具演进（A06 候选首个业务帧标签的帧索引由 1 改为原始帧索引 7），b2 为 approval-revision（SPEC-BODY-003 main-messages-keep-passthrough 判据过严，A04 是第三方入站，按设计不合成逐项 passthrough，期望值由 0 改为 1）；b2 评估运行 43 条规则、201 条检查全部通过）。

VC-6 在 ARM64 上完成：四阶段激活（canary → 切换 → 回滚 → 恢复，每阶段一次真实业务请求均 HTTP 200）所用镜像为
`sha256:38991d66c2e1a5b0e8c8e605816d44c2db8f76954adce1027de8afa45235f80c`，退休 0.160.0 运行投影后重建的切换镜像
ID 为 `sha256:7e3eb0e354d12a910201e950d6a6fb2c8fc25f68343d35e19b92b7a2a8cea301`，固定回滚镜像为 v0.2.18-1
`ghcr.io/itv3/sub2apiplus@sha256:c822ea06e231fa61ef9bdc3999c52e3e320423573962371e47779dab2ea9f591`（0.163.0）；
canonical checkpoint 为 `00000012`，执行集合为空。收尾时 ARM64 运行本地构建的退休后切换镜像；0.164.0 已随发版 v0.2.18-2 进入 GHCR 发布镜像，
换发布镜像与 DMIT 等生产机更新按发布流程另行办理，生产机需另行批准。机器事实分别见
[`0.160.0 Runtime Profile 退休收据`](egress/maintenance/CODEX_CLI_0163_TO_0164_RUNTIME_PROFILE_REMOVAL_RECEIPT.json)
和 [`0.164 终态收据`](egress/maintenance/CODEX_CLI_0163_TO_0164_TERMINAL_STATE_RECEIPT.json)。
0.160.0 按 §4.6.7 第 1 类退休：Catalog、selector 与运行投影均已移除，画像字节 `affe6f66…`（0.160.0 期 Active）因被
此前各版本的晋升／激活／终态收据逐文件登记而原地保留。此前退休的 0.157.0、0.154.0 两份、0.151.0 一份
与 0.149.1 两份画像同样原地保留，分别仍由既往终态收据批准。不得据此认为这些文件已删除。

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
| `backend/internal/service/official_egress_codex_*`、`official_client_profile_registry.go` | 0.163.0／0.164.0 不可变 Snapshot、可信 release Build 运行态投影、发布投影、端点编排、Files 与模型能力 |
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

**执行目标：哪里坏修哪里，修好接着跑。** 当前工具已支持分阶段续跑、候选 revision、评估基线、按依赖范围补跑与同树沿用：
故障先暂停，修复、复核并对账通过后，从最近合法检查点继续；可信成果按合同承接。“整体约 6 小时”的旧目标依赖逐单元承接真正可用，
该前提尚未成立，已撤回，不作验收口径；不能通过省略验收、清零账本或把开工预检移出统计来宣称达标。

耗时统一统计从本轮首次开工预检到最终交付的墙钟，包含修复、等待和归档；只读用途以 VC-6 完成为终点，
生产用途还包含 GitHub 发版及生产服务器更新复核。VC-0～VC-6 与后续发布耗时分别列账再合计，
阶段有效耗时另行报告，不用它替代整体耗时。
当前 B-09／B-10 的真实承接仍关闭，B-11 仅部分单元读集闭合，B-12 本轮未启用候选采集并行；
相关节省不能计作已实现收益。首次实际升级应按上述口径报告总耗时、执行／承接范围及超时原因。

顺利一轮的耗时再分三段报告：准备段（版本登记、官方包收据，上游发版后先做）、正式段（首次开工预检到 VC-6）、收尾段（含 VC-5 等待期并行备好的仓库侧工作），
并报总工作量，不只报正式段。逐单元承接新增的承接视图、固定测试树坐标、中断运行的部分完成清单与收尾逐单元承接已入库，但尚未在真实升级里用过
（收尾仍为 `re-execute` 全集），收益不计。

[Framework §5.3](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 规定 `VC-0 → VC-1 → … → VC-6` 顺序；
本部分保留 Codex 专用操作与验收合同。脚本参数、安装及实现细节统一见
[驱动 README](../tools/arm64_capture_driver/README.md)；旧入口与历史事故只按生成时的收据原文只读解释，
不作为新 Campaign 的执行依据。

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
   输入形成凭证，输入漂移重新审核。工具缺陷、预算、账务、环境与根因上限先暂停；暂停放行与永久停线条件见
   [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)，Codex 入口见 [§4.7](#codex-4-7)。

**每轮参数只维护一份。** 从 [env.example.sh](../tools/arm64_capture_driver/driver/env.example.sh) 填写
`ARM64_VC_ENV`，由驱动安全解析，不直接 `source`。`round_context.py` 从正式账本取 Campaign／candidate／revision；
`phase_context.py` 取当前评估基线、读来源、写目标和唯一有效 attempt；`cleanup_context.py` 取收尾路径和收据。
显式 ID 仅作断言，不按“最新目录”猜测，不复制上一轮路径。历史只读 `sealed_receipt_binding` 不可用于新采集准入。

**证据目录统一。** 本机升级证据、临时工作材料及归档统一置于
`/Users/czs/Developer/sub2apiplus-evidence/codex-cli/` 的版本／轮次子目录；不放入 `.codex/artifacts`，
也不散放在 `Developer` 根目录。服务器活动 Campaign 仍使用冻结的宿主数据根，不能以本机归档路径替换。
迁移后保留完整 SHA-256 清单和旧→新路径映射；历史收据原字节不改，按证据库根目录的 `MIGRATION-*.json`
定位，执行重放所需的原路径按恢复合同重建。

<a id="codex-registration-prep"></a>
## 开工准备与版本登记（VC-0 之前）

上游发版后即可开始，不占 VC-0 的关键路径。一次改全版本登记变更集：目标版本的场景清单与期望清单（描述文字不得带上一版版本号）、事实映射、证据标签、模型表、
编排器的受支持升级对与文件名清单、该版本的定向样本、各处源码根版本号、`tools/spec_ref_anchors.json` 与依赖清单，并对第二部分的源码引用做增量复核；
登记完成后、建 Campaign 之前用 `test_codex_identity_conservation --seal-baseline` 重封基线（缺漏会被守恒测试拦下）；改完跑全量 `make test-capture-tools`。
两次升级之间只能改 control 层文件；wire_producer／evidence_semantics 层（场景清单、候选采集脚本）与 `TARGET_VERIFIER_SHA256S` 钉住的 5 个
`tools/ci` 验证器文件，只能随版本登记重封。受管驱动文件必须在 Campaign plan 之前改完。

<a id="codex-vc-0"></a>
## 4.0 VC-0 冻结升级输入

先准备本轮参数、干净工具提交、bundle、官方完整包、目标源码／锁定依赖、有效生产回退点与批准预算。
受管工具／文档用 `tools/arm64_supervised_deploy.py` 部署，随后按驱动 README 安装并复验驱动；
部署收据与实际三份执行副本一致后才开工。

新目标顺序固定：开工空跑 → `entry.sh` → VC-0 原子收口 → VC-1 首批。`entry.sh --plan` 只展示计划；
开工空跑在独立 staging 和 `fixture_only` 总账内执行，零真实请求，不写正式 Campaign。
`--opening` 重新执行全集并审计读集，演练通过不代表 B 类承接已获批准。

上游发版后、VC-0 之前，先跑 `chain-rehearsal.sh rehearse`（零请求）：用上一版缺陷目录回放、恢复分支故障注入，再把完整链（6 步）跑一遍，把工具缺陷一次报全；
依赖被阻断的步骤标“未执行”，不记通过。它**不覆盖**候选侧 seal／compare／accept、真实监督器下的候选采集脚本整段运行、VC-5 库表前置和收尾门禁环境，
这些仍靠 VC-4 期间只读核对与收尾前预跑，不能把“空跑全绿”当成没事。与后台验证、采集互斥。

### 4.0.1 DOC-PRE 与 P0：冻结清单与离线验证

入口编排器负责以下步骤，操作员按输出处理阻塞，不再逐项手填命令：

| 顺序 | 入口负责的工作 | 验收要点 |
|---|---|---|
| 1 | 便宜检查、策略兼容／激活认证 | 参数、部署、目标包、源码、场景及环境一次报全；另含模型可用性探测，以及 main 上最近一次 Security Scan 须成功且对应 main 当前头（缺省 36 小时内）。依赖失败项标阻塞；这两项确需跳过只能用 `OPENING_CHECKS_SKIP` 并写 `OPENING_CHECKS_SKIP_REASON`，记为 skipped，不是 passed。ARM64 没有 `gh` 时，在本机核绿后带理由跳过 Security Scan 项 |
| 2 | 入口门禁与 pre-A3、零请求 smoke、atomic-double | 同一次门禁产出 P0 证据与目标平台预跑记录；沿用 atomic-double 记录时同时核时效窗口；全部通过才建账本 |
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

认证工具按客户端分组：策略认证、pre-A3 与发布认证模块共用可选 `--client`，缺省为 `codex`；
Claude 须显式指定 `--client claude`。两客户端分别绑定本客户端工具身份、pre-A3 场景、部署收据与认证合同，
认证结果不相互承接。分组合同见[共享实现文档 §9.5](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md#95-认证链与真实链)。

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
   `vc2_assertion_preflight.py record／verify`，离线验证**全部官方适用判据**：官方侧只实跑 `dual_wire` 规则的判据，
   `candidate_profile` 规则的判据只在候选侧成立，预检报告记为 `pending_candidate`，不因官方证据不满足而失败。
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
3. 用 `driver/local/local-candidate-chain.sh` 形成 A（资产）→ RA（A 段承接收据）→ 需要时的 RR（版本 route 候选态收据
   重生成，属 A 段，非冻结路径）→ C（只切换两个冻结 Catalog 指针）→ D（相对 RR 或 RA 登记 C 的冻结承接）提交链和 bundle。
   D 收据必须 `unregistered_path_count=0`、无人工待办；C 段出现任何非冻结路径都会被 Campaign 的候选源码转移校验拒绝
   （RA 未登记的新增内容寻址资产不受此限）。生成 D 后先在本机用 `_validate_candidate_source_transition` 复核，再上 ARM64
   跑门禁；重建 Campaign 时重做本轮链，不能沿用绑定旧 Campaign 的提交。
4. ARM64 先运行 `arm64-vc4-gates.sh`，确认 `ARM64_VC4_GATES_DONE` 和门禁收据有效后再运行 `vc4-all.sh`，
   两者不并行。旧 `driver/local/local-vc4.sh` 仅用于解释历史本机门禁记录，新轮次使用 ARM64 同格式产物。

目标 Snapshot／Release 只追加，不改历史节点。新端点须同时闭合 binding、resolver、route 与 release proof；
生产 canary 收据只能由 VC-6 实测。退役 route 例外按
[退役登记](../backend/internal/officialegress/catalogdata/release-route-retirements.json)及其
[加载合同](../backend/internal/officialegress/catalog_route_retirements.go)，不以缺 binding 代替退休证明。

### 4.4.2 同源构建

驱动从同一干净提交准备源码／测试／构建／Docker context，生成前端、ARM64 二进制和不可变镜像，
重放实现测试后登记构建。完整历史、vendor 回退、构建网络及快照索引由准备门禁检查，不再现场手补。
Go 离线编译；需取得前端依赖时只走冻结出口。Docker 构建里的 RUN 容器不在出口守护登记范围，默认网络取不到包索引时
用构建网络合同：参数文件 `VC4_BUILD_NETWORK=host` 加 `VC4_BUILD_NETWORK_APPROVAL`（`vc4_contract.py network-intent` 取绑定，
经批准落 0600 批准件，绑定本轮 bundle、提交、主机与驱动摘要；建树前写进 `pre-build.json`，换模式须重走 `trees.sh`；
`build-network-receipt.json` 记录实际命令）。保留 Git 可执行位，二进制要求 `vcs.modified=false`。

`source transition` 在源码树外生成并通过 `make check-egress-spec`，不能形成自引用；任何输入变化重新生成。
门禁在不含 vendor 的测试树运行，vendor 仅在 `go.mod／go.sum` 逐字相同时沿用。
实现测试、Catalog、门禁计划、source transition、前端装配、二进制及镜像共同绑定到 `build-receipt.json`。

重入 `vc4-all.sh` 先复验完整收据；上传等待中断仅支持 `--resume-from upload-wait`，先核对四树、输入、
门禁与实物。等待受预算和心跳约束；镜像存在或旧日志成功不足以跳过构建。重入前根盘须不超过 69%（派发前守卫）：
失败构建留下的候选树可直接删（重跑会重建），入口日常维护 `entry-dryrun.sh housekeeping` 清旧暂存树与过期 Go 缓存。

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
| 生成并 finalize | bundle、Go test trace、observed-profile、两份 Kilo 收据；运行 fact 来自服务，trace 来自同源冻结测试日志；HTTP 入口的用量记录可以早于响应完成，时序判据与 WS 一致（installed ≤ ingress ≤ runtime ≤ response，且 ingress ≤ usage） |
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
`post_promotion` v4 收据。`gate_before` 环境收据是准入阶段，根盘超过 69% 硬失败（`*_after` 只记降级）：晋升里的 `go run`
会让编译缓存回涨，门禁前先清可再生缓存再核水位。缺项／多项、摘要漂移、失败或跳过均禁止构建切换镜像和 canary；保持旧 Active。

### 4.6.4 权威源码、切换镜像与正式发版

post-promotion 通过后，按逐文件 manifest 同步 production tree 到权威仓库并提交，再在 ARM64 从该提交构建
切换镜像；提交、源码、构建、镜像与 promotion／acceptance 全链可复算。不得带 `candidatecapture`，不得复用候选镜像。
切换镜像与退休镜像的 `docker build` 同样走 `--network=host`（R15 不放行 BuildKit RUN 容器）；同步提交与退休提交以
“前一提交..HEAD”增量 bundle 交给 ARM64，bundle 必须带 `HEAD` 引用（构建脚本按 `fetch <bundle> HEAD` 取）。
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
切换后复核 health、日志、依赖、出口、Active、activation fact 与业务结果。业务验证模型须是切换镜像与回滚镜像都实测
走通过同一入站接口的模型（0.163／0.164.0 轮用 `gpt-6-astra` 经 `/v1/chat/completions`：主线 `gpt-6.1-sol` 从未经该接口验证，
回滚镜像以 0.163.0 等旧身份出站也未必能用）。
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

收尾合并候选分支时，两边都改过的冻结文件在合并提交里先取升级侧（已有冻结登记），两侧合并结果放进下一个普通提交，
冻结承接才能在不含任何收据变化的区间里登记（冻结承接生成器拒绝区间内有收据变化的范围）。

合并候选链进收尾分支后、起收尾全量门禁之前，先在本机跑 `test_ci_background_preflight`（或全量 `make test-capture-tools`）核对“无未承接冻结路径”，不要省：
省掉的代价是约 2 小时后才红。收尾全量门禁中途根盘会比起跑时多涨约 8～9 GiB，起跑前按此核对剩余空间不触 69% 停线，并先删上一轮遗留的 Go 编译缓存。
`prepare --dry-run` 的第一步重放 Campaign 材料，可提前确认读侧一致。

**收尾工具**使用 [`tools/codex_closeout.py`](../tools/codex_closeout.py)：read-material／draft 可先生成指南草稿；
VC-6 收口后再 prepare（先 `--dry-run`）→ gates → 人工批准 → publish。工具同步需要的摘要并生成隔离候选材料，
第二部分未变时不触发规则画像摘要级联。配置见驱动 README 的“收尾编排”；工具不代替 canary、切换或回滚。

**人工批准门**：指南草稿、发布计划和同一候选的 full-gates 全集重跑通过后，才可消费指南／发布批准，
正式冻结承接、签发终态及推送；实际清理另有精确目标专项批准。隔离候选材料不代表正式生效。

收尾门禁（`codex_closeout.py gates --plan`）缺省重新执行同一候选的全集，不允许按客户端筛选，允许带标注的“补跑通过”单元，发布汇报里单列。
只有在收尾配置里给出 `gate.unit_inherit`、固定测试树坐标（`gate.work`）与记录库（`gate.record_store`），才允许前序后台验证向收尾提交逐单元承接（来源清单与决策收据须可重放，
类型翻转、符号链接、子模块变更一票否决）。0.164 未启用，收尾为 `re-execute` 全集。

实际清理先 dry-run：从 `cleanup_context.py` 解析当前 VC-5／VC-6／canonical 绑定，列出目标、备份与恢复演练。
批准、脚本结果和删除验证绑定同一上下文摘要。只删除已归档且无生产／重放依赖的临时产物，
保留正式配置、数据服务、当前和 rollback 镜像、唯一证据，以及可复用抓包基础设施。
中断后只对账，不盲目重派删除；条件不足继续 defer。实际删除另留收据，不回改 VC-6 的历史清理决定。

<a id="codex-4-7"></a>
## 4.7 公共控制面与恢复约定

<a id="codex-fix-and-continue"></a>
### 修好接着跑：出问题先暂停，修好后在原 Campaign 接着跑

固定闭环：**读诊断 → 修复最小范围 → 定向复核 → 登记工具／环境变化 → 对账 → 批准恢复范围 → 续派 → 重放完成收据**。
批次失败后**先对账再部署修复**：`campaign-resume` 要求部署收据晚于限额事件的记录时间，先部署后对账会被拒；
同树重部署后，若同一提交已有全集通过结论，可试 `background-validate.sh reuse`；缺省（按客户端筛选的子集）起跑的结论不能沿用，直接重起后台验证
（见下“重新部署受管工具之后”）。
对账返回 `paused`／review 时先解除对应条件，只有可恢复且取得授权后才按 `next_command` 续跑。
未失效的证据保留原字节和来源；需要新 candidate／Campaign 的变更按身份边界办理，不能强行原地覆盖。

`fix-and-continue.sh <轮次参数文件> --dry-run` 只读检查；正式入口按步骤记录和幂等键执行，
`--list` 查看、`--from <步骤>` 续作。它**仅覆盖 VC-5 封存前、非 recovery_revision 的候选采集**。
部署、演进、延期、恢复批准／授权消费独立有效凭证；缺失就输出 intent 并停下，
不能用参数里的批准人姓名代签。实际边界见[修复安全实现](../tools/arm64_capture_driver/driver/fix_safety.py)。

失败之后的“登记演进→对账→恢复预览→批准／授权→续跑”可用 `recovery-plan.sh <参数文件>`：缺省只读预览，列出保留／重验／重采的对象；
`--apply` 按现场状态跳过已完成步骤、按序执行，重复执行结果相同。写账本的动作各需人工提交的专项批准，缺批准只写意向并停下，不代签。
0.164 实际恢复仍走手工命令链，写动作端尚未经真实失败实练，首次使用先预览并与手工结果逐项对照。

**后台验证**（`background-validate.sh`）：起跑前自动做五项前置检查（冻结收据、基线重封、重部署、准入绑定、主机 Go 不低于 `backend/go.mod`），
任一项不过拒绝起跑，不得用 `--no-preflight` 绕过。Codex 缺省按客户端计划跳过与改动无关的 Claude 重模块（`--gate-scope auto`；Codex、共用、廉价 Claude 模块
及受改动影响的 Claude 重模块照跑；找不到发版基点或选择器出错时回退全集；Claude 轮次、`--opening`、收尾不筛选），子集结论不得冒充全集，也不能被 `reuse` 沿用。失败单元只在“命令正常退出但失败”时补跑一次：同一单元最近 5 轮补跑通过只许 1 次，
超时、被信号终止、读集未闭合不补跑；Go 门禁先核对包清单完整，再按失败包补跑。补跑通过的单元在结论里单列，两次日志都留；
结束时在结论目录另出核数偏差报告。

修复提交先完成相应离线回归再受监督部署；改收据生成器（如 `codex_upgrade_receipt_finalizer.py`）时把已部署过的旧版本摘要
登记进 `LEGACY_REPLAY_PRODUCER_HASHES`，部署前本机跑 `test_producer_replay_registration_gate` 与整套 `make test-capture-tools`；脚本在部署后复核并执行 regression，随后启动独立后台验证。
采集前申请整机，后台让路；批次边界发现后台失败／中止即停止后续派发，VC-5 accept 前必须已有通过结论。
CI 可与续跑并行，后台或 CI 失败则暂停，按部署收据经受监督部署回滚工具；新预算事件不兼容旧工具时保持暂停、前进修复。
收尾合入前 full-gates 使用 `re-execute`，正式发版前 CI 全绿。脚本 recover 只启动补跑，
等 `vc5-run-batch.out` 的 `RUN_BATCH_DONE` 和收据通过后，再接 `vc5-all.sh`。

<a id="codex-failure-matrix"></a>
#### 失败矩阵

对账入口按 [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 分流：本父 run 期间有**尚未完整收口的
采集预约**用 `reconcile-attempt`，否则用 `reconcile-supervisor-run`。

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

失败矩阵补充（0.163.0／0.164.0 实测，复核时逐条对照）：

- **VC-5 采集被截停留下无 `attempt.json` 的孤儿 attempt**：失败批次之后的零请求恢复预览改取该候选最新的已批准且已授权预览
  （只读，不代为授权）；先 `reconcile-supervisor-run` 登记失败的预览批次，再 `reconcile-attempt` 重新生成预览（账本头变了，
  旧预览作废）→ 批准 → 授权 → `vc5-recover.sh`。派补跑之前把 `runs/` 下该候选已存在的固定证据根逐个改名为
  `<根>.failed-attempt1`，否则首个作业因证据根被占用拒绝覆盖，并连环触发出口暂停。
- **作业失败触发 `environment_contaminated`**：归档陈旧证据根 → 恢复采集容器 → 干净环境收据（根盘须低于 70%）→
  `environment-isolate`（无窗口）→ `accounting-resolve`（未发请求时 `--estimated-count 0` 并附来源审计说明）→ `reconcile-attempt`
  → 批准 → 授权。run 期间已产生 reservation 时 `reconcile-supervisor-run` 拒绝，改用 `reconcile-attempt`；补账请求数按候选网关库
  `usage_logs` 逐行计数并按作业时间窗拆分。环境隔离作废且一个作业结果都没有的 attempt 是合法续跑来源，全部冻结作业重跑。
- **重新部署受管工具之后**：VC-5 准入要求当前部署的后台验证 passed。只有全集重新执行、带沿用绑定的通过结论可被 `background-validate.sh reuse` 沿用：
  工具身份五摘要、提交、门禁集合、执行器、调度策略、输入清单、平台环境七项绑定全部不变且自来源完成起不超过 24 小时才沿用，被拒会写明原因。
  按客户端筛选的子集结论既不能沿用也不能作沿用来源，缺省（Codex 子集）起跑多半被拒，此时直接重起后台验证（子集约 1.2 小时；全集约 2 小时），通过后再派恢复批次；
  阶段预算在等待期间到期用 `deadline-extend preview／apply --scope stage`（`--preview` 取 Campaign 内 `control/deadlines/` 的规范预览）；
  准入收据绑定漂移用 `vc5-precheck.sh --apply`（动作为空）重签。
- **候选采集脚本的主机名**：Sub2API 一侧的主机名必须取容器真实名称。监督器按 64 位完整容器 ID 钉住 `CAPTURE_CONTAINER`，
  超过 DNS 标签 63 位上限，`candidate-core-mitm` 首检会确定性失败；wire_producer 层演进部署前先确认变化路径的层、
  旧新两棵树的 `wire_closure_sha256` 不变、已部署编排器只读算出的 `_tool_path_job_map` 对已封存官方作业零映射。
- **VC-5 断言（assert-rules）因判据有误失败**：走 `evaluation-recover approval-revision`（零请求，不重建 Campaign）。只许改检查项的
  `select`／`assertion`／`description` 与规则描述，检查项 ID 集合、规则级字段（含 `scenario_ids`）与验收契约必须逐字不变；
  `--reason` 不超过 512 字，预览与批准逐字相同。顺序：`reconcile-supervisor-run` 登记失败批次 → Campaign 期间证据语义变过时先
  `evaluation-epoch --candidate-id`、再 `reevaluate`（`--fix-commit` 加当前部署收据）开工具演进基线 → `approval-revision` 预览 → 批准。
  写草稿后先用已部署工具的 `_approval_revision_delta` 与平铺布局断言离线试跑；新基线有自己的门禁目录，目标平台门禁在新基线重跑。
- **VC-6 交付登记批次**：两次 `deliver-candidate` 不含 canonical 项，驱动阶段解析按交付批次核对 Campaign、Candidate 与 attempt 绑定。
- **目标版本清单从上一版复制**：描述文字里“<上一版> 起……”之类带版本号的说法，会在 VC-2 classify 被 `_validate_assertion_profile_manifest`
  判为“残留 baseline 版本坐标”而拒。登记时就把描述写成不带版本号的说法，并在本机对新清单跑一次该校验函数。
- **换版时源码根版本号散在多处**：`Makefile`（`CODEX_SPEC_SOURCE_VERSION`）、`tools/check_spec_refs.py`、`tools/upstream_merge/gate_runner.py`、
  `tools/upstream_merge/request_template_v2.json`、`tools/spec_ref_anchors.json`、`tools/spec_source_deps/manifest.json`（`cargo_lock` 随新版本的 Cargo.lock 重建），
  同批改，漏一处门禁对错源码树；改完用新版本源码树跑 `make check-egress-spec`。
- **登记之后必须跑全量 capture-tools（含全部 `test_claude_rule_expectations_*`）**：登记改 Claude 场景清单时容易漏重生成旧版本期望，
  分组定向测试覆盖不到，只能靠全量或 CI 发现。
- **候选态与晋升后正式目录两个槽位都声明新可选节**：旧画像发布槽位不存在，依赖旧画像槽位的用例会整片红（同根因一度上百个，另有判别式门禁与继承特征门禁）。
  VC-4 之前先在“Active 旧版／Previous 目标版”的候选态上预演整包 `internal/service`；夹具改取冻结底稿，依赖旧槽位的端到端用例按画像结构事实跳过（不放宽断言），
  判别式门禁在两槽位都具备时按继承特征取较旧槽位重放。
- **晋升之后的形态要用模拟晋升树整包预演**：把候选链合并进收尾分支后，用 `egresscatalogpromote` 加 `retire-apply` 做出 Active／Previous 与正式晋升
  一致的本机树，整包跑 `go test -tags=unit ./...`。晋升前全绿、晋升后才红是这一类问题的共同特征：0.164 靠这一步在收尾前查出两处生产缺陷（管理后台“测试 OAuth 生图”漏绑运行态快照，
  晋升后会整条失败；WS→HTTP 桥接延迟正文分支漏置 additional_tools 官方键序）和写死上一版形态的测试（固定到 Active／Previous 里较旧的槽位，断言不放宽），修正先行提交。
- **晋升后判别式门禁会失去判别力**：以 `codexGateDiscriminatingReleaseMode` 取“唯一具备该事实的槽位”的门禁，在下一版登记使两个槽位都具备该事实后会红，
  届时改用 `codexGateTargetReleaseMode` 并另写新版本的判别式门禁。
- **复杂度守卫用例满载噪声**：分配次数测量（`testing.AllocsPerRun`）会被包内后台 goroutine 的突发分配撞红，
  改取多轮最小值（`minAllocsPerRun`），上限与“与命中数无关”的断言不变；`make test` 无补跑，撞上会让 VC-4 停（约 2 小时白跑）。
- **非 daemon 的 TUI 场景在 0.164 要显式 `--no-daemon`**：0.164 取消功能开关白名单，`--disable` 会触发 daemon 兼容检查致启动即退出。
- **入口门禁续跑**：Formal 建成前，入口编排器只在 `--full-set-request`、`--unit-inherit-request`、`--reexecute-gates` 三种请求模式下无条件重做入口门禁，
  缺省按步骤记录判定沿用（沿用记录完成超过 24 小时一律重做；门禁本轮要重做时判了沿用的 atomic-double 连带重采）。
- **VC-4 门禁运行期间（ARM64 只读）逐条核对 VC-5 候选作业脚本的库表前置**：三处硬条件在线上配置里不一定满足。① 采集账号 `credentials.model_mapping` 必须为空
  （候选就绪检查 `_model_mapping_fact`，派发前零请求即失败）；② 采集 Key 所在分组 `allow_live` 必须为 true（`run_candidate_aux_capture.sh`）；
  ③ 分组里活跃 Key 只许采集 Key 一把（`run_images_wire_probe.sh`）。②③ 在作业脚本里才报，每撞一次就停一次采集容器，同根因 `attempt.environment-contaminated` 计数顶向上限 2。
  不满足的按“记原值、只改一项、VC-6 后恢复”提前改好并把恢复项写进收尾清单；
  管理接口改分组会把 `codex_models_manifest_config`、`model_allowlist` 从 `{}` 归一成 `{"enabled": false}`，要用 SQL 写回。恢复链派出的批次进程名是 `vc5-recover.sh`，
  接续脚本不要按 `vc5-run-batch.sh` 判断“批次已消失”。要为 VC-5 阶段执行截止预留 `deadline-extend --scope stage --phase VC-5`。
- **采集账号“平时关、用时开”的，须在 VC-5 全部完成之后才能关**：seal checkpoint 的“Kilo 后环境恢复门禁”要求 Kilo 后探针的账号 status／schedulable 与候选采集后探针一致，
  Kilo 一结束就关号会被判环境污染，同根因计数顶到上限；编排脚本要等 `VC5_ALL_DONE` 之后才关。
- **证据标签的帧索引按原始帧序号声明会指到续帧上**：候选网关向上游发出的压缩 WS 消息会被拆成多个分片帧（coder/websocket 压缩写入，0.160 起就如此），官方是一条消息一帧。
  0.164 登记的 A06 标签把首个业务帧写成帧索引 1，实际预热消息占原始帧 0～6，首个业务消息起始帧是 7，VC-5 seal 预演被 SPEC-WS-005 的 7 条
  `first-business-frame-*` 检查拒收，靠工具演进把索引改成 7 才过。登记测试里的观测是手造的，覆盖不到这一类问题；
  下次登记在 VC-4 期间用非正式实采的候选证据先离线跑一遍 `_verify_selector_reachability` 与全部检查，并评估标签改按消息序号或事件类型命中，或让候选改单帧写入。
- **VC-5 断言因判据过严失败走 approval-revision**：SPEC-BODY-003 `main-messages-keep-passthrough` 期望 0，而 A04 是第三方入站，按设计不合成逐项 passthrough，实际为 1，
  封存后 assert-rules 必然红。登记时同样应在 VC-4 期间用真实候选证据离线试跑全部检查；恢复链为 `reconcile-supervisor-run` → 工具演进基线（`reevaluate`）→ `approval-revision`，
  修订后的评估运行 43 条规则、201 条检查全部通过。
- **seal 失败遗留的半成品会让后续预览失败（受管工具缺口，0.165 登记时修）**：seal 在 Kilo 后探针之后失败、attempt 被作废时，留下的 `client-after`（有 probe-manifest、无恢复收据）
  仍是“最新 attempt”，`codex_upgrade status` 报“Kilo 后检查点材料不完整或不可信”；在证据根内改名会触发“权限收口后的证据元数据边界漂移”，0.164 的处置是整体移出证据根归档。
  被隔离作废的旧 attempt 的 `attempt.json` 状态仍是 `awaiting_receipts`，监督器把它数成第二个等待收据的 attempt（`codex_upgrade_supervisor.py`），
  于是 assert-rules 失败被归为 `execution-failure` 而不是 `post-run-tooling`。应修成：作废或隔离的 attempt 不参与最新摘要、不计入等待收据。
- **VC-5 后半段有两个会过期的东西**：准入批准件（有效期写在批准件的 `expires_at_utc`，须覆盖到 canonical 为止；0.164 的批准件只给了 2 小时）与管理令牌
  （JWT，有最小剩余有效期要求，默认 43200 秒，配置也不得低于 21900 秒）。VC-5 每多拖一个缺陷，到 canonical 前这两个都可能过期，`vc5-canonical2.sh` 开头的
  `vc5_require_admission` 会静默退出，外层编排只看到“vc5-all 进程已消失”。排查用 `bash vc5-precheck.sh --dry-run` 看 `actions`（`renew_token`），
  处理是 `vc5-precheck.sh --apply --approval <批准件>` 重新准入后重入 `vc5-all.sh`。
- **自写的 VC-6 编排脚本按 bundle 取提交，不能写成 `git fetch <bundle> HEAD`**：`git bundle create "${DC}..${BRANCH}"` 的 bundle 里只有分支引用、没有 HEAD，
  会报 `couldn't find remote ref HEAD`（上一版的 bundle 里恰好有 HEAD，改写时没发现）；改成按 `refs/heads/${BUNDLE_BRANCH}` 取。
- **收尾分支要并入已受监督部署的工具演进，基线冲突不手工拼接**：Campaign 期间部署的工具演进（如证据标签文件改绑）最终入库的内容必须与 ARM64 正式运行的一致。
  收尾分支自己也可能因第二章摘要连锁重封过基线，两边的基线文件和 `BASELINE_SHA256` 必然冲突：取一侧占位，对合并后的树用 `--seal-baseline … --replace` 重封一次，
  只许被演进文件的条目与 `source_commit`、`files_sha256`、`evidence_semantics_sha256` 变化；源码、重封、承接收据分开提交。
- **热点文件里不要内联大段分支代码**：满载换号的入口接入代码内联在 `openai_gateway_handler.go`、`openai_chat_completions.go` 时，每次合并上游都要重做冲突处理；
  收进独立文件 `openai_gateway_composite_entry.go` 后，热点文件只留接线，并配接线行为测试与语法树守卫。
- **线上缺陷的修复要在收尾全量门禁之前并入**：例如模型清单冷缓存的并发缺陷（线上并发冷读同一账号会多打一次上游），
  按“源码提交 → 冻结承接收据（`--before` 取并入前提交）”并入收尾分支。
- **VC-6 结束后恢复 VC-5 为采集所做的库表临时改动**：把采集账号的 `credentials.model_mapping` 用备份原样写回，连同分组的 `allow_live`、多余的 Key 与采集账号的关号；
  账号关着时调度投影不会刷新（直接改库不产生事件），不影响功能，下次经管理接口开关账号时会重读整行。改的若是真实用户的 Key 或分组，在汇报里点名。
- **审计索引要显式登记私有归档坐标**：轮次目录里可能没有 `vc6-private-archive.out`，索引脚本的默认取法会落空；
  私有归档取最新的私有归档包，作为第三个参数显式传入，索引要登记归档坐标。

#### 暂停种类与放行

暂停种类、放行条件与永久停线判定见 [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)。Codex 入口：

| `pause_kinds` | 放行入口 |
|---|---|
| `deadline` | `deadline-extend preview／apply`（项目／Campaign／阶段分别批准） |
| `request_budget` | `request-budget-extend preview／apply` |
| `accounting` | 所属 Campaign 的 `accounting-resolve` |
| `environment` | 干净环境复核后 `environment-isolate` |
| `root_cause_repair` | 项目上限用 `record-root-cause-repair`；账本 `stop_required` 用 `campaign-resume` |

`fix-and-continue` 不代做补账、隔离、请求预算扩展或 campaign-resume；批准绑定漂移按工具要求重新预览。

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
- **工具分层**：wire、evidence_semantics、control 与 policy 变化按 Framework §5.3.4 身份表处理，演进登记见
  §5.3.6。Codex 用 `tool-evolution` 登记演进；policy 变化另需兼容／激活认证与受监督部署。
- **候选级 revision**：规则见 Framework §5.3.6。Codex 以账本 `stage_revision` 和 COMMIT 确认当前候选，r1 保留原
  checkpoint，后继落 `control/vc/revisions/r<N>/`；入口为 `invalidate-candidate` 与 `revision-open --supersedes`。
- **认证与历史**：生成器修改须登记旧摘要为只读重放身份；旧收据不能用旧身份新签。
  `awaiting_receipts` 导入必须满足 §4.0.4 的额外绑定，不能拼多 attempt 为一个可信来源。

两个客户端共用的适配接口、服务面与合同版本见[官方客户端升级控制面](OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md)。
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

批次 staging／COMMIT 的中断边界与续派规则见 [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)。
控制面完成 admission、直接前序 checkpoint、账本和 deadline 核对后启动唯一父监督器；操作员只使用
`compile-and-run-vc-batch` 编译派发。官方 bundle 已发布时只续未完成的 seal 动作。

### Codex 评估失败局部恢复与断点续跑

评估基线、恢复段与增量封存的规则见 [Framework §5.3.6](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)，本节只列 Codex 坐标。
评估基线为 b<K>，由账本 `evaluation_baseline` 与 COMMIT 授权；`stage_sources` 指定各阶段 local／reuse，
没有复用依据的结果源记为 `reuse_authority=none`。

恢复段为 ar<k>，编号取 `evaluation-recover apply` 的返回值，只补 J*；J* 沿段预约 → 基线 COMMIT →
`recovery.json` 推出。Codex 的恢复证明另须有效 after 探针和恢复报告。

增量封存顺序为 Kilo 后检查点 → 受管派生／finalizer → 复用根投影与 delta 扫描 → 预演／seal，随后重跑 compare。

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
