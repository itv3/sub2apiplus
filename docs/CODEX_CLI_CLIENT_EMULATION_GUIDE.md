# Codex CLI 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 OpenAI OAuth 账号的 Codex CLI 客户端仿真
> **当前 active 基线**：`codex-cli 0.151.0`；previous 为 `codex-cli 0.149.1`；`0.147.0` 已退出 Runtime Catalog
> **当前生产事实**：正式镜像 `sha256:2589b419055073fc0d9f3b0c47d3efe3e0f0f93fac798604c91355d9a9e088ae`；canonical checkpoint `00000009`，待执行集合为空
> **依赖基线**：[`tools/spec_source_deps/manifest.json`](../tools/spec_source_deps/manifest.json)
> **文档定位**：本文是 Codex CLI 客户端规则、Sub2API 仿真实现和版本演进的人类可读权威入口；
> 逐规则机器证据见 [`docs/EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md)。
> **共享流程权威**：共同目标、运行架构、证据生命周期、变更分类、上游更新、发布与回滚规则以
> [`OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) 为准；本文只定义
> Codex CLI 的准入目标、官方事实、版本画像、实现、当前状态和公共流程的 Codex 专用增量

> **架构减法执行规则（2026-09-05 起生效）**：新建或恢复的正式 Campaign 只允许使用
> `tools/official_client_capture/codex_upgrade_supervisor.py campaign-run` 及其
> `codex-upgrade-campaign-run/v1` 预声明动作清单。本文后续出现的 `successor`、`control-epoch`、
> `runtime-repair`、`evaluation-transition` 和递增 `vN` 命令均为历史收据说明，不得照抄执行；与本规则
> 冲突时，以共享框架的单一恢复算法和 `campaign-run` 入口为准。

正式动作由一个父监督器统一记账。派发的 `codex_upgrade.py` 子命令复用父
`run_dir` 和原始 deadline，不得再启动独立 lease／monitor；动作清单中的
`execute_items` 才能运行，`reuse_items` 只能读 checkpoint，执行集合为空时
立即写 `incremental-noop`。旧的 successor、control-epoch、evaluation-transition
和 terminal-transition-preflight 写入入口在该上下文中硬拒绝。

候选若已是 `awaiting_receipts`，且九项 Job 全部为 `reused/complete`、执行／失败／待执行集合为空，
同时尚未生成 evidence manifest 或 seal 草案，则允许 `campaign-run` 对仅涉及
`control`、`evaluator`、`orchestrator` 的工具修复登记 `metadata_only_seal_repair` 后继续 seal。
该例外不重发请求、不创建 `evaluation-transition`，也不适用于已有失败 Job 或已开始扫描的 attempt。
若绑定的 UpgradeTimingLedger 已在 VC-0 因 `permanent-stop-*` 停线，仅当冻结 checkpoint
仍为 active、停线是其后唯一新增事件（head 只增加 1）且当前 live 请求数为 0 时，才可只读承接；
其他 stopped／stop_required 状态一律拒绝，不能借 metadata-only 例外绕过 active 门禁。
历史 control epoch 只有在 `boundary` 全零、Ledger 仅因预算到期为 `stop_required`、失败计数为空
且 live 请求为 0 时，才允许 metadata-only seal 回退到 Campaign 冻结控制；仍须通过冻结 VC-0
Ledger 的唯一 `permanent-stop-*` 校验，不得重试请求或创建新的 epoch／successor。

旧恢复实现的兼容边界固定在
`tools/official_client_capture/codex_upgrade_legacy_boundary.py`：它只登记历史命令和只读符号，
不创建新的正式收据。`codex_upgrade.py` 中的旧函数只有在历史离线夹具明确授权时才可派发；正式
0.151 Campaign 不得调用它们。后续清理按“先移除写入实现、再验证只读回放、最后删除无消费者符号”的顺序进行。

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

**遥测零流量边界。** Framework §1.2、§3.2 的公共规则适用于当前 active 0.151.0。官方源码中，
`config/src/types.rs:217-223` 的 `AnalyticsConfigToml.enabled=false` 经
`core/src/config/mod.rs:4182` 传入 analytics client，并由 `analytics/src/client.rs:222-233` 禁用事件队列。
OTEL 是独立配置：`otel.metrics_exporter=none` 才关闭默认 Statsig metrics；不能只写笼统的
`otel.exporter=none`，因为 `config/src/types.rs:585-592` 中 log／trace exporter 默认是 `None`，metrics
exporter 默认仍为 `Statsig`，`otel/src/provider.rs:194-230` 仅在 metrics exporter 非 `None` 时构建指标
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

本部分定义规则成立所需的证据标准、观测边界和 53 个编号项。当前生产 active 为 0.151.0，
previous 为 0.149.1；本轮差异规则、ARM64 身份事实和 Files C2PA 条件分支均已完成生产激活、
精确回滚、目标恢复和 0.147 Runtime Catalog 退休。

## 2.1 规则证据与准入标准

### 2.1.1 证据类型与位置

| 类型 | 材料 | 可以证明 |
|---|---|---|
| L1 | `local-analysis/sources/codex-cli-<version>/codex-rs/` 官方 stable 源码 | 调用链、条件和内部机制 |
| L2 | `tools/spec_source_deps/` 锁定依赖源码 | 指定依赖版本与 feature 下的行为 |
| P／R | pcap、等长脱敏原始字节 | TLS、连接、HTTP／WS 和 Body 的实际输出 |
| J／M／L4 | MITM 应用层 JSONL、解码摘要、manifest、测试和合成输入 | 摘要绑定与辅助验证，不能单独定义官方规则 |

当前 L2 依赖锁定为 `hyper 1.8.1`、`hyper-util 0.1.20`、`http 1.4.0`、`tungstenite 0.27.0`、
`h2 0.4.16` 和 `reqwest 0.12.28`；准确来源和摘要以依赖基线清单为准。Sub2API 实现证据位于
`backend/` 和 `docs/egress/`；逐规则索引及源码锚点分别见 `docs/EVIDENCE_INDEX.md` 和
`tools/spec_ref_anchors.json`。

当前规则画像基于官方 tag `rust-v0.149.1`（commit `ff29a44391deccde0aba0f8390337d7f3c319ea4`）；
官方 Linux amd64 二进制 SHA-256 为
`e24fb784c7d71140d67afb620f56e9137496cf7f6c9e19217fa3666dcf306278`。仓库 active 画像为
`codex-0.149.1-official-r1491-v2`，摘要为
`8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3`；Release graph 与
Snapshot catalog 摘要分别为
`057264d864aea27ebafecf504e95b8c948f25ac20f11fdabbfd2385d35c85465`、
`4b3e2aded6ad932a4f1adb5efefefe8dd5bad7092a1de3c0bddff54f4a84f57c`。0.149.1 的 HTTP、WS
Main 与 WS Lite 主采样分别绑定 run `codex-0_149_1-20260824T-http-main-r2`、
`codex-0_149_1-20260824T-ws-main-r2`、`codex-0_149_1-20260824T-ws-lite-r1`。
本次 Catalog 晋升与 ARM64 生产激活分别由
[`R28 catalog promotion receipt`](egress/maintenance/CODEX_CLI_0147_TO_01491_R28_CATALOG_PROMOTION_RECEIPT.json)
与
[`R34 production activation receipt`](egress/maintenance/CODEX_CLI_0147_TO_01491_R34_PRODUCTION_ACTIVATION_RECEIPT.json)
证明；逐轮 transition 已合并为
[`0.149.1 terminal state receipt`](egress/maintenance/CODEX_CLI_0147_TO_01491_TERMINAL_STATE_RECEIPT.json)，
不再作为运行时输入或独立测试留在仓库。
0.147 的 Catalog 晋升与生产事实仍分别由
[`K83 catalog promotion receipt`](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_CATALOG_PROMOTION_RECEIPT.json)
与
[`K83 production activation receipt`](egress/maintenance/CODEX_CLI_0145_TO_0147_K83_PRODUCTION_ACTIVATION_RECEIPT.json)
证明；它们现在是 previous 与历史生产证据，不表示本次修改或部署了 Vircs。各规则保留的早期 run ID
是未变化规则的原始证据，不代表 active 版本仍为旧版本。

0.151.0 候选绑定官方 tag `rust-v0.151.0`（commit
`78c290807ce710180111df227df3b7a4fe845452`）、`aarch64-unknown-linux-musl` 包和 ARM64 二进制
SHA-256 `56f026015ccc3ebc12895282200d89c216892bf6fa15fa7f228e6e0c6ad6ce76`。正式证据位于 Campaign
`c0151-formal-20260831t0220z-r8` 的 attempt `20260831T022126Z-901dd6612631b7bf`；29 个 Job、
权限、秘密扫描、环境恢复及 `172.30.0.10／172.25.0.3 → 179.255.100.158` 出口门禁均已封存。

所有证据必须绑定官方源码、依赖、二进制、平台、配置、账号、抓包运行号和摘要。只有能够重新
解析的材料可以作为规则依据；R 类材料只允许等长脱敏，未脱敏材料不得离开采集机。

### 2.1.2 规则准入与观测边界

规则只有在被测身份和适用条件明确、源码与合适的 wire 观测通道闭环、正反例充分、引用和摘要
可复算时才能准入。证据不足时只能收窄命题或保持未决。

“固定、随机、条件”分别表示每次一致、允许变化和随明确条件变化，不得把随机样本或条件结果
固化为默认行为。

- pcap、relay、MITM 和服务端重建只能证明各自可见的层次，不能互相替代；
- Candidate MITM Job 的 producer 合同只有应用层 JSONL 和场景摘要，不生成 pcap；其清单中没有 pcap 时
  必须以 `scanned_bytes=0` 结束 pcap 排查，禁止调用 tshark 或扫描更大的目录；
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
  与请求级头并行的入口。
- **规则**：0.149.1 仅在内置 OpenAI ChatGPT OAuth 身份下，为普通 Responses HTTP、legacy
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

路径级 `from_sha256` 必须承接上一份机器 transition 的 `to_sha256`；已确认但尚未单独登记的前置修改须并入当前 transition，不能从工作树基线摘要另起一条断链。`base_commit` 只标识本变更集起点，不替代路径级前序摘要。

0.149.1 的 DOC-PRE 规则现已合并到本指南第二部分，不再维护独立候选规则文档。历史 DOC-PRE 配套输入为
`candidate_rule_expectations_0_149_1.json`、`codex_upgrade_scenarios_0_147_0.json`、
`codex_upgrade_scenarios_0_149_1.json`，规范锚点与依赖基线统一使用
`tools/spec_ref_anchors.json` 和 `tools/spec_source_deps/manifest.json`。这些文件只提供目标版本和工具能力输入；该次 DOC-PRE 冻结的
历史 production active／previous 为 0.147.0／0.145.0，仅用于解释旧收据。0.145 已在 0.149.1 升级后
退出，0.147 已在本轮 0.151 激活与回滚验证后退出；两者均不得恢复为运行画像。当前 previous 只允许
0.149.1。当前值必须从文首基线、Runtime Catalog 和最新有效激活收据共同复算，不能据历史 DOC-PRE
文件推断运行状态。

| 类别 | P0 通过条件 |
|---|---|
| 身份与角色 | 冻结官方二进制、源码／依赖／平台／feature／镜像和网络条件；执行副本、测试树与 finalizer 同源 |
| 账号与工具 | 场景所需账号、模型、额度、请求键、观测／安全／finalizer 工具和构建资源可用 |
| 环境恢复 | 端口、挂载、容器、hosts、代理、CA、数据库及托管字段可按 before／after 语义恢复 |
| 生产隔离 | 只读记录镜像、compose、选择器、Active／Previous 和依赖服务；P0 与正式 Campaign 隔离 |

P0 还必须执行以下机器预检；临时画像和合成证据只验证工具能力，不得升级为正式证据：
运行“当前基线”前，先备份并同步受管工具树；`_verify_execution_tree` 零漂移后才能启动测试。

| 预检面 | 最低检查 |
|---|---|
| 当前基线 | 在干净 HEAD 执行 `make test-capture-tools`、`make check-egress-spec`，记录命令、源码摘要、退出码和测试通过／失败／跳过数量 |
| 目标版本坐标 | 用真实 baseline／target 坐标试运行 `plan` 加载；对空值、错误值和正确值做 mutation，禁止缺失坐标静默回退当前画像 |
| 双版本与画像生成 | 用临时批准资产验证 `prepare-profile`／`stage-profile`、Active 不变、Active／Previous endpoint 并集和版本新增 route 的 fail-close 门禁 |
| 候选工具链 | 验证 candidate core／aux、WS、relay、manifest、trace、finalizer、Schema 和逐规则断言能识别目标版本；用历史导入夹具离线跑通 `deep-verify → status → seal → compare → accept`；逐项复算 test fact map 的测试／源码 SHA-256；目标版本证据标签声明必须逐 Job 精确覆盖正式清单，禁止遗留版本硬编码 |
| 执行身份 | 逐字核对受管工具树与实际执行副本；确认候选源码、测试树、目标架构和镜像构建输入可形成同源摘要链 |
| 官方证据 | 按 Framework §5.3.5 冻结并验证唯一 `reuse／recapture` 决定 |
| 成本模型 | 用不小于本次最大证据集的 ARM64 夹具测量完整扫描；证明 preview 只扫描一次，批准、`status` 和 successor 的原始证据扫描字节均为 0 |
| ARM64 执行 | 逐项通过 §4.0.5 的网络、运行时、模型目录、坐标、依赖、时间和存储门禁 |

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

`plan --rule-manifest` 固定绑定当前 baseline 的 `codex_upgrade_rules_<baseline>.json`；目标版
`candidate_rule_expectations_<target>.json` 只用于候选断言预检，禁止传给 `plan`。例如 0.149.1 →
0.151.0 必须传 `codex_upgrade_rules_0_149_1.json`。

### 4.0.2 共享身份边界在 Codex 工具中的投影

Campaign、candidate 与 attempt 的规范身份边界以 Framework §3.3、§5.1 为准。下表只说明现有 Codex
工具如何把这些边界投影为新建操作，不产生另一套定义：

| 单元 | 必须新建的变化 |
|---|---|
| 版本 Campaign | 目标版本、官方二进制／源码／依赖／平台／默认 feature，或批准规则、场景、画像和断言变化 |
| 同版本后继 Campaign | 受管工具影响证据含义、环境无法证明恢复，或已冻结的机器角色、执行副本和 finalizer 身份错误 |
| 同 Campaign 新 candidate | Sub2API 源码树、测试树、构建 ID、部署版本、OCI digest、image ID 或 profile ID／digest 变化 |
| 同 Campaign 同 candidate 的运行坐标覆盖 | 采集账号／API Key ID、五个容器名、四个 Codex 二进制路径或 Live attestation compose 坐标变化：在该候选首个 attempt 前用 `candidate-runtime-override` 登记一份写一次收据（见 §4.4.1），不新建 Campaign，也不新建 candidate |
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
工具还允许运行时纠正后继成对重绑 compose 坐标，并在 `predecessor-import.json` 中冻结前序值、后继值和原因；
历史 v1 收据仍按“配置逐字不变”只读重放。该命令只逐字复制计划期 inputs／analysis、五份批准
清单和规范化 official surface，并生成 `predecessor-import.json`。原始官方 evidence 与 attempt 继续
位于前序 Campaign，保持只读；后继只绑定直接前序 checkpoint、`EvidenceManifest` 根摘要、阶段 seal
和批准联合摘要。`status`、`compare`、`accept` 只重放这条小型摘要链，不递归读取任一级原始 evidence。
前序没有可信 manifest 时，必须在创建 successor 前执行唯一一次显式 `deep-verify` 建立迁移 checkpoint。
摘要承接必须以最多 64 份 transition 收据做确定性有界图可达验证；遇环、缺失、摘要漂移或超过上限
立即失败关闭，禁止逐层补 successor、递归重试或扫描历史 evidence。
任一路径、manifest 根摘要、package digest 或不可变边界漂移均失败关闭。后继 Campaign
普通运行时纠正后继只能新跑 candidate 与第三方客户端验证；分类事实纠正后继允许重新批准规则、
场景、画像和断言，但仍不得改变目标版本、官方身份或已封存官方证据语义。后面三项发生变化时
必须按版本 Campaign 重新执行相应阶段。

successor 不得用于超时、扫描过慢、评估侧工具修复或临时失败，也不得由工具自动创建。同一根因最多
允许一次人工批准的 successor；后继再次命中同一根因时必须停线，禁止继续形成 rN 链。

唯一例外是 Framework §5.3.4 的历史 VC-0 兼容恢复已经批准、但恢复 Ledger 在首个新 reservation 前
过期。确认新 attempt／reservation／checkpoint／live 请求均为 0 后，使用
`--reason candidate_recovery_control_refresh` 从该失败 Campaign 创建一次控制刷新后继，同时绑定旧停线
head、新 Ledger／P0 和当前增量 no-op 演练。该入口复用原失败闭集和已通过 Job，禁止改变 Candidate、
compose、场景或执行集合；发布后 60 秒内必须把新 Ledger 推进到 Candidate 所在阶段。它不是普通超时
重置入口，同一祖先链第二次使用立即停线。

若后继由产出侧工具变化触发，先按 §4.0.5 用当前工具建立恢复用 `preflight_only` 并完成完整 Job
演练，再在 `successor` 命令追加 `--job-rehearsal-root <root> --job-rehearsal-receipt <receipt>`。
工具会按后继当前执行合同重放收据并替换旧绑定；缺少、部分提供或合同不一致均失败关闭。
前序 Candidate 的必需 Job 已通过、仅可选 Job 失败而状态仍为 `awaiting_receipts` 时，必须同时绑定该
`--predecessor-candidate-id/--predecessor-attempt-id`。只有未建立 Kilo 后检查点、未生成 seal 草案／预览，
且产出变化逐文件只影响这些失败项时，后继才可把它作为只读恢复源；否则停线，禁止 seal 或扩大重跑集合。
该路径固定使用 `--reason candidate_failed_job_tool_recovery`，并且必须同时提供旧停线 checkpoint、新
active Ledger／P0 和当前闭集演练收据。它不得携带新的 compose 坐标或 `--target-scenario-manifest`；这两类
变化仍只属于 `candidate_runtime_identity_correction`。两种原因分别最多使用一次，避免前一次运行时身份
纠正错误阻断后续独立的失败 Job 工具修复。
若前序场景遗漏了 Codex 二进制绑定，可再提供
`--target-scenario-manifest <当前场景>`。工具只接受固定五个 Candidate Job 新增
`CODEX_BIN={capture_codex_bin}`，要求其他字段及全部 official Job 逐字不变，并签发 v9 场景过渡收据；
增量恢复只重跑这五个 Job 和其他实际失败项。
v9 后继的首个 Candidate 命令必须使用 `resume --rerun-failed`；工具从
`abandoned_candidate_attempt` 计算“失败／未完成项与五个变化项”的并集，并把其余通过项写为
`reused`。没有唯一来源或闭集不一致时须在 reservation 前失败，禁止回退为全量九项。
历史结果只有粗粒度组件摘要时，还必须把每个产出侧变化文件精确映射到上述执行闭集；未知文件、触及
复用 Job 的文件或 successor 创建后的新增高风险变化都立即停线。不得用“允许 relay 变化”绕过整组件
摘要，只能排除已登记且已证明不触及复用项的具体文件。
若 `classification_fact_correction` 后继的历史 target 场景只因 `source_spec.sha256` 发生受管维护，
Job 合同必须以恢复 preflight 的当前受管场景重算，同时保留历史官方执行合同；不得复用旧场景合同，
也不得因此重发官方请求。

若 official 阶段已经封存，但其 evaluation transition 的恢复控制链没有被后续 `classify` 从不可变
Campaign 清单承接，不得修改 Campaign、追加第 4 个 attempt transition 或重抓。先确认封存 transition
实际绑定的 recovery Ledger 状态，再执行一次
`successor --reason sealed_stage_control_recovery`。该后继只导入 official 封存结果，必须保持
`classification_imported=false`、`executed_job_count=0`、`scanned_bytes=0` 和
`live_request_count=0`；后续 `classify` 只重放导入摘要、EvidenceManifest 边界和 surface，不扫描原始
official 证据。同一直接前序只能使用一次。

恢复控制严格二选一：

1. Ledger 仍 active：沿用同一 Ledger 和原 ARM64 P0，生成当前 head 的 `VC-2` checkpoint；
   `ledger_dir`、`upgrade_id`、plan、证据决定、累计 live 请求数及 ARM64 P0 均不得变化。
2. Ledger 已 stopped：绑定旧 `stop_the_line` checkpoint；新建不同目录和 `upgrade_id` 的 Ledger，并把
   新 Ledger 单调推进到当前 head 的 `VC-2`。ARM64 P0 的 `subject_id` 必须等于新 `upgrade_id`，所以必须
   重新签发 P0 收据，不能直接复用旧 P0；环境连续性未变化时仍可复用不受影响的 Job。新 Ledger 的 live 请求数必须为 0；旧 Ledger
   的累计耗时和 live 请求数由 successor 收据只读保留，不得清零或覆盖。

两条路径都先用最终采用的同一 checkpoint 创建 preflight；只对失效 Job 做闭集演练。active 路径提供：

~~~bash
--active-timing-ledger-dir <同一-ledger> \
--active-timing-receipt <当前-vc2-checkpoint> \
--active-arm64-environment-root <原-p0-root> \
--active-arm64-environment-receipt <原-p0-receipt>
~~~

stopped 路径改为提供：

~~~bash
--predecessor-stop-ledger-dir <旧-ledger> \
--predecessor-stop-receipt <旧-stop-checkpoint> \
--recovery-timing-ledger-dir <新-ledger> \
--recovery-timing-receipt <新-vc2-checkpoint> \
--recovery-arm64-environment-root <新建或只读承接的-p0-root> \
--recovery-arm64-environment-receipt <新建或只读承接的-p0-receipt>
~~~

两组参数不得混用。该 successor 的合法原因是封存阶段控制无法从不可变 Campaign 清单承接，不是超时
本身；执行 Job、原始证据扫描和官方请求仍必须全部为 0。

以下原地恢复只适用于显式白名单内的评估侧工具修复；评估实现及其收据 Schema 必须成对登记，工具身份分级与
sealed-stage 恢复必须使用同一白名单闭集，禁止同一文件先被判为评估侧、随后又被恢复门禁判为产出侧。
产出侧工具变化仍须新建 Campaign。控制面、环境面和数据面按 Framework §5.1.3 独立判定；控制面变化
不得自动重建 P0 或完整 Job 演练。旧 Ledger 已超时时先封存 `stop_the_line` checkpoint；只有 Ledger、
P0 producer／Schema 或相应语义实际失效时，才分别新建对应收据。
若原 attempt 已完成 live 请求、Kilo 后检查点完整且证据字节未变，严格按下列顺序恢复：

1. 先把 active Ledger 推进到原 active 阶段并封存 checkpoint；再用同一 checkpoint 创建恢复
   `preflight_only`，只生成失效的 P0 或 Job 演练收据，最后执行 `evaluation-transition` 两步批准并绑定
   旧停线 checkpoint、当前 Ledger 和新旧收据组合；preflight 与 transition 的 checkpoint 必须逐字一致。
   失败 partial attempt 允许建立 transition，但源 attempt 的授权仅为 `capture-run`。
   首个 Job 前失败仅在结果为空、全部 Job pending、checkpoint 为空链、末项 Job 为空、heartbeat 仍为
   `attempt:reserved`、Job／日志目录为空且只有可信 after 探针时允许 transition；恢复时执行全部冻结 Job。
2. 用 `resume --rerun-failed` 创建绑定同一 transition 的新 attempt；只复用源 attempt 已完成 Job，执行
   失败／未完成闭集。新 attempt 必须以完整 checkpoint 覆盖源计划、无额外执行项并进入 `awaiting_receipts`；
   源 attempt 永远不能直接 seal。
3. 仅为缺少 manifest 的 imported official／classify 建立一次 `deep-verify` checkpoint；恢复 attempt 的
   seal、`deep-verify`、compare 和 accept 才能使用该 transition，且仍为离线操作。

失败项为空时在 reservation 前立即写 `incremental-noop` 并成功退出：不创建 reservation／attempt，不启动
容器或探针，不读取大证据，不发请求；该收据不改变阶段状态。
恢复 transition 可以使用上述 no-op 证明当前合同无需执行，但必须现场重放 no-op 绑定的原始 `passed`
收据并从中承接运行时通过事实；普通 Formal `plan` 仍拒绝 no-op，不能把空操作冒充新通过事实。
恢复计划先闭合 Job ID 集合，再仅接受完整摘要相等或受管工作树迁移校验通过；旧版 `_safe_plan` 的
缺字段投影不能放宽脚本、参数、环境或证据根校验。

Formal run/resume 还必须先取得 Campaign 持久租约（`.campaign-lease.json` + 独立锁）。租约固定
owner PID/nonce、attempt、UTC 截止时间、最后 heartbeat 和当前命令；编排器崩溃、强制停止、心跳
超时或 deadline 到期时只追加 `stop-the-line` 收据并停线。只有同一失败源与冻结 `recovery_scope`
通过校验的显式 `resume --rerun-failed` 才能接管 stale 租约；旧 timing Ledger 的停线事实不阻断
该合法恢复。`status` 只读租约，不续租、不删除，也不触发深度扫描。

每次 Formal 命令同时启动独立监督器 `codex_upgrade_supervisor.py`：owner 心跳 5 秒、失联判定 20 秒、
分钟账本 60 秒；事件和账本逐条 `fsync`。Job 的每个步骤必须经 Campaign lease 的统一命令入口执行。
ARM64 离线门禁确认不会输出秘密时必须使用 `run --persist-output`，失败输出保存在对应 run 目录的
`command-output.log`；先从日志定位并只补跑失败项，不得为找错误再跑一遍完整门禁。live Job 禁止启用。
监督器在 `capture-cli` 容器内若不以 `$CAPTURE_CONTAINER_ROOT` 为当前目录启动绝对 Python 脚本，命令
必须显式加 `PYTHONPATH=$CAPTURE_CONTAINER_ROOT`；导入失败不得触发后续探针或扩大重跑范围。
强停后的 `audit` 只读检查若发现事件链或分钟区间缺口，立即返回失败，不能继续 ARM64 部署。
完整记录的业务失败必须分类为 `failed`；只有事件链损坏、监督器失联、分钟缺口或终态不可证明时才是
`audit-incomplete`，不能把正常 fail-close 误报为审计缺口。
ARM64 环境收据的中文错误标签只用于诊断，传给 heartbeat 的 operation 必须是固定 ASCII 标签；P0 和
Job 演练必须使用 Formal 同一 heartbeat 回调，实际覆盖 `docker inspect`、默认路由与公网出口探针。
从 macOS 向 ARM64 打包受管工具树时必须使用 `COPYFILE_DISABLE=1 tar --no-xattrs ...`，禁用
AppleDouble 旁车文件和 pax 扩展属性；暂存树的受管文件数和
工具摘要必须与源树精确一致，出现任何 `._*` 文件都要在原子交换前失败关闭并重新打包。两份客户端文档
必须同时存在于运行路径 `docs/` 和归档路径 `docs/repository-docs/`，且同名文件摘要相等。ARM64 解包必须
使用 `--no-same-owner` 或等价机制，确保暂存树全部为 `root:root` 且没有 group／other 写权限。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py evaluation-transition \
  --campaign-dir "$CAMPAIGN" --phase candidate \
  --candidate-id "$CANDIDATE" --attempt-id "$ATTEMPT" \
  --predecessor-stop-ledger-dir "$OLD_LEDGER" --predecessor-stop-receipt "$OLD_STOP" \
  --recovery-timing-ledger-dir "$NEW_LEDGER" --recovery-timing-receipt "$NEW_TIMING" \
  --recovery-arm64-environment-root "$ARM64_ROOT" --recovery-arm64-environment-receipt "$ARM64_RECEIPT" \
  --job-rehearsal-root "$REHEARSAL_ROOT" --job-rehearsal-receipt "$REHEARSAL_RECEIPT"
# 复核 review_sha256 后，原命令追加 --approve-transition-sha256 <review_sha256>
python3 tools/official_client_capture/codex_upgrade.py deep-verify \
  --campaign-dir "$CAMPAIGN" --candidate-id "$CANDIDATE" --attempt-id "$ATTEMPT"
~~~

transition、imported checkpoint、seal 批准、`status`、compare 和 accept 均不得读取原始证据；candidate
证据只允许 seal 预览扫描一次。任一步失败即继续停线，不得重发本次 r26 已完成的 91 个请求，也不得
新建 successor。新 Ledger 的 `create` 已自动写入 `doc-pre-p0-started`，不得再追加同阶段
`stage_started`。

机器 finalizer 收据的 producer 绑定使用受管相对坐标和已登记摘要，不绑定生成时工作树的绝对根；
重放时保留历史 producer 字段并重新计算业务结果。坐标或摘要未登记仍失败关闭，工作树迁移本身不使
已完成 Job 失效，也不触发官方请求重发。
finalizer 修复部署前还必须从当前真实 Campaign 的 restoration、画像、客户端和场景收据枚举全部
`producer.tool.sha256`，逐项核对新版本或精确历史只读白名单，并在 ARM64 私有挂载暂存树执行真实
`status --candidate-id`；仅跑合成夹具不算通过。

历史 Inventory 与新 manifest 只允许排序差异：去重后的 `(path,size,sha256)` 全集和安全结论必须完全
一致，旧 Inventory 摘要保持不变。若已批准 transition 后才发现新的评估侧缺陷，停线该控制链；每次
修复只更新 Framework §5.1.3 判定为失效的控制收据，并用上述全链离线回归追加替代 transition，禁止
无条件重建 Ledger、P0、完整 Job 演练，也禁止覆盖旧 transition。
每个 phase 总计最多三份 transition（原始一份、替代两份），第三份失败后永久停止恢复。
替代 transition 必须写入原失败 source attempt。已经完成全部 Job 的恢复 attempt 保留旧绑定作为来源
锚点，seal 按当前工具摘要选择同一 source 的更高序号替代槽位并把该新绑定写入阶段收据；不得改写
attempt 或重发已完成请求。
替代 transition 的引导加载只按冻结身份重放历史 transition 的摘要、预览、源 attempt 和闭集关系，
不得先用当前评估器重算旧 `recovery_scope` 再阻断用于批准该变化的命令；新 transition 本身仍必须按
当前规则生成、复核和批准。普通状态与后续阶段不得使用该引导例外。
seal 的 draft／preview 与 transition 使用同序号追加槽位：首份为 `seal-draft.json`／
`seal-preview.json`，第二份为 `seal-draft-02.json`／`seal-preview-02.json`，第三份同理。批准必须读取
当前有效 transition 对应槽位；preview 绑定同槽 draft，阶段结果同时绑定该 transition 和同槽 preview；
旧文件不得覆盖。
恢复 attempt 的 seal 只能把 transition 收据的 `{path, sha256}` 写入阶段结果；工具返回的影响分析包装
对象仅用于选择闭集，禁止直接写入 stage。回归必须同时覆盖 seal 预览和摘要批准。

采集、探针、relay、脱敏、收据生成、环境快照和编排等产出侧工具变化会改变证据字节，必须
新建 Campaign。评估侧工具只有在显式白名单内才允许漂移，并须登记摘要、重放全部受影响门禁；
新增或未分类工具默认属于产出侧。被校验的工具树必须就是实际执行的工具树。

canonical `campaign-run` Schema、旧入口边界、gate／activation 收据及
`profile_rule_patches_0_151_0.json` 均为控制／评估侧白名单文件；它们只影响调度或离线判定，
不改变已封存请求字节。旧 epoch 若曾将其中文件计入 production，必须按 plan 时摘要兼容重放，
不得改写历史 checkpoint。

正式 Campaign 建立后才发现产出侧工具阻断时，必须先封存失败 attempt、after 环境探针和恢复报告，
再以 `stage_abandoned` 事件登记当前阶段、根因和唯一下一动作。该事件只关闭当前阶段，不重置总墙钟、
失败计数或历史收据；独立工具修复和 ARM64 受影响闭集门禁通过后，以新的 `stage_started/VC-0` 返回，
按 Framework §5.1.3 只新建失效的 P0／演练收据，再创建 preflight 与 Formal Campaign。禁止把失败阶段记成 `stage_completed`，也禁止用旧 Campaign
的冻结 job 定义重跑已经变化的产出工具。

模型目录补采的临时重试日志必须在清理前回传到 attempt 日志；只剩返回码而无原始错误视为工具阻断。

原台账的 producer 绝对路径必须保持不变；工具摘要变化只接受维护 transition 自摘要、前序文件摘要及
该工具 `from_sha256 → to_sha256` 精确边全部可重放的已登记后继。未知摘要、路径替换或不连续边一律失败关闭，
不得覆盖 `ledger.json` 或伪造 checkpoint 来承接新工具。历史 checkpoint 保留生成时的 producer 原字节，
重放器只用同一后继链验证其身份，不得把历史收据重写为当前 producer。
后续 `append／checkpoint／replay／status` 必须直接执行 `ledger.json` 的 `producer.tool`；内容相同的同步副本也不能代替该绝对路径。

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

`status` 只读推导状态且必须是廉价操作：只读取 Campaign、阶段收据、checkpoint 和 manifest，禁止
递归枚举或重哈希原始证据。完整内容复验只能显式执行 `deep-verify`；`resume` 只能为身份未变化的
允许重试创建 attempt。`ready` 之后的 promotion、activation 和 rollback 不改变 Campaign 状态，由
生产收据独立证明。

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
| 时间、ARM64 环境与门禁承接 | 条件就绪 | 相关工具可生成并重放收据；只有目标版本全部生成 Job 通过 ARM64 离线演练后，才可视为已实现 |
| 单次深度验证与廉价状态 | 实现待验收 | preview 生成 `EvidenceManifest` 和冻结 seal 草案；批准、`status`、compare、accept、successor 的原始证据扫描字节为 0；`deep-verify` 仅补齐缺失的历史导入 manifest／checkpoint；ARM64 P0 未通过前仍阻断 Formal 续作 |
| 官方 Release 制品取得 | 已实现 | `codex_upgrade_official_asset_receipt.py` 逐个预连接 CDN IPv4，冻结 Release metadata、asset 摘要、证书和唯一精确地址；离线重放通过后才能下载 |
| 第三方客户端绑定 | 当前固定为 Kilo 双入口 | 工具和 Schema 明确要求 `kilo-compatible`、`kilo-responses`，文档不得单独泛化 |

Campaign v3 的 `plan` 必须显式提供 `--timing-ledger-dir`、`--timing-receipt`、
`--arm64-environment-root` 和 `--arm64-environment-receipt`。时间 checkpoint 必须仍为 active，且
upgrade ID、基线、目标版本和用途与计划一致；ARM64 收据必须为 `status=passed`、`phase=p0`，并以
同一 upgrade ID 为主体。Campaign manifest 绑定两份收据的相对路径、摘要、字节数、合同摘要和环境
连续性身份；后续受管阶段每次执行前重新检查时间台账，超出阶段或总墙钟预算立即停线。官方／candidate
抓包 attempt 还必须在真实请求前后自动生成、重放并绑定 `attempt_before／attempt_after` 环境收据，
前后连续性不成立时不得封存证据。

调用 ARM64 环境收据的 `collect` 前必须先显式执行 `mkdir -m 0700 "$ARM64_ROOT"`；该工具只接受已存在、
非符号链接且权限精确为 0700 的 evidence root，不会代替操作员创建目录。

`candidate_external` 与 `post_promotion` 门禁均使用 v3 attempt 收据。首次 attempt 执行冻结合同的全部
门禁；失败时登记 `root_cause_id` 并保留失败收据。补跑必须引用唯一前序失败收据，逐项重放同一阶段、
主体、输入和 ARM64 环境连续性，只执行前序 `failed_gate_ids`；已通过项从前序收据承接，禁止再次执行。
前序已通过、根因变化、收据链循环、输入或环境漂移、补跑集合扩大，以及同根因第三次 attempt 均失败
关闭。生产激活只接受最终 `status=passed` 且 `failed_gate_ids=[]` 的 v3 `post_promotion` 收据。

缺少收据、摘要漂移、失败、跳过、命令集合变化或身份不一致均使 P0／对应阶段失败关闭。Campaign 建立
后再修改这些工具会触发 §4.0.2 的工具漂移边界。人工“已经运行”结论、终端截图或未绑定原始事实的
静态 JSON 不能替代受管收据。若未来要把 Kilo 泛化为可配置第三方客户端集合，也应先修改工具、Schema
和验收测试，再调整本流程。

### 4.0.5 ARM64 执行、时间与资源硬门禁

本环境后续 Codex 升级的 P0、取证、Candidate、Kilo、门禁、canary 和部署验证均只在 ARM64 执行。
DMIT 归档只读复用，不登录或修改 DMIT 主机。ARM64 固定出站边界如下：

#### 4.0.5.1 ARM64 抓包目录坐标

本机 Docker 项目统一归入 `/root/docker/<项目名>`。`capture-cli` 的规范坐标固定为：

| 变量／坐标 | 固定值 | 作用 |
|---|---|---|
| `CAPTURE_HOST_PROJECT_ROOT` | `/root/docker/capture-cli` | 宿主机 Compose、镜像构建输入和配置根 |
| `CAPTURE_HOST_DATA_ROOT` | `/root/docker/capture-cli/data` | 宿主机唯一可写数据根，权限 `0700`、Git 忽略 |
| `CAPTURE_CONTAINER_ROOT` | `/root/oauth-capture` | 容器内运行根，不表示允许同名宿主目录继续写入 |
| 历史宿主兼容根 | `/root/oauth-capture` | 迁移期只读重放坐标，禁止产生 0.154 新数据 |

标准目录布局为：

```text
/root/docker/capture-cli/
├── docker-compose.yml
├── image/
├── config/
└── data/
    ├── state/
    ├── runtime/
    ├── work/
    ├── evidence/
    │   ├── campaigns/
    │   ├── control/
    │   └── audit/
    ├── staging/
    └── archive/
```

宿主机命令和容器内命令必须分别使用上述变量，不得因两侧历史上都出现 `/root/oauth-capture` 而混淆
bind source 与容器 target。Compose 文件固定为
`$CAPTURE_HOST_PROJECT_ROOT/docker-compose.yml`，工作目录固定为 `$CAPTURE_HOST_PROJECT_ROOT`；项目名
必须显式冻结，不能继续由旧目录名 `deploy` 或当前工作目录推导。`image/` 和 `config/` 只放受管输入，
所有可变对象只能写入 `data/` 的对应子目录。

截至本规则生效时，现有 Compose 和可写数据仍位于
`/root/oauth-capture/deploy/docker-compose.yml` 与 `/root/oauth-capture`。它们是待迁移的历史坐标，
不是规范例外；规则生效后只允许写入目录迁移本身的控制账本和收据，其他用途仅可执行历史只读复验与
回退。0.154 的 P0／Formal Campaign 前必须生成并通过目录迁移收据，至少绑定旧新 inventory、Compose
原文与渲染摘要、bind 映射、镜像、容器、固定 IP／出口、权限、迁移前后字节数及历史兼容 bind mount。
迁移期间旧宿主路径不得接收新的 staging、bundle、patch、源码树或 Campaign；迁移完成后的兼容 bind
只读，不能形成第二份可写数据。

P0 必须同时通过以下目录门禁：

1. 解析 Compose 后，本地 build context、env file 和业务 bind source 均位于项目根或数据根；
2. `docker inspect capture-cli` 的实际挂载、镜像、网络和固定 IP 与迁移收据逐项一致；
3. 有界枚举 `/root` 第一层，不得出现本轮新增的 `codex-*`、bundle、patch、worktree、恢复树或抓包目录；
4. `data/staging` 中每个临时对象均有 owner、用途、创建时间、上限和清理状态；
5. 清理只接受冻结 manifest，完成引用／占用／挂载检查和异机归档后生成前后复验收据，禁止无范围删除。

后续命令统一先声明：

```bash
export CAPTURE_HOST_PROJECT_ROOT=/root/docker/capture-cli
export CAPTURE_HOST_DATA_ROOT="$CAPTURE_HOST_PROJECT_ROOT/data"
export CAPTURE_CONTAINER_ROOT=/root/oauth-capture
```

ARM64 宿主机的 Go 固定使用 `$CAPTURE_HOST_DATA_ROOT/state/local/go1.27.0/bin`；`capture-cli` 容器内使用
`$CAPTURE_CONTAINER_ROOT/state/local/go1.27.0/bin`。两者必须解析为同一冻结工具链摘要。构建和 Go 门禁统一设置
`GOPROXY=off`、`GOFLAGS=-mod=readonly`，不得临时下载或切换工具链。
`docker build --network=none` 只限制 Dockerfile 的 `RUN`，不限制基础镜像解析；声称离线构建前必须
确认全部基础镜像 digest 和层已在本机冻结。

干净树没有 `frontend/node_modules` 时，`make test-capture-tools` 必须通过
`CAPTURE_TYPESCRIPT_MODULE=<绝对路径>` 读取 ARM64 已有的只读 TypeScript 5.6.3；门禁固定校验
`typescript.js` 摘要 `f316520790d4db220a10d890c5f85310e26a1bd3c104b8d3b5eb62ba0491651b`。
禁止为跑门禁安装依赖、复制 `node_modules` 或使用相对路径／符号链接。
需要验证 world-traversable `/opt` 的正向测试夹具必须显式建在 `/tmp`，不得建在
`/root` 内再把宿主权限误报为运行时缺陷。

| 对象 | 强制出站网络坐标 | 公网出口 | 禁止变化 |
|---|---|---|---|
| `sub2apiplus` | `proxy-network`：`172.25.0.3`，网关 `172.25.0.1` | `179.255.100.158` | compose 网络、地址、默认出站路由、NAT／iptables |
| `capture-cli` | `capture-network`：`172.30.0.10`，网关 `172.30.0.1` | `179.255.100.158` | compose 网络、地址、默认出站路由、NAT／iptables |
| ARM64 `wg1` | `/etc/wireguard/wg1.conf` 显式 `MTU = 1420`，运行时 MTU 1420 | 与 DMIT 已冻结 `wg1` MTU 1420 一致 | 删除／重复 MTU、依赖 9000 上联自动推导或运行时漂移 |

附加 Docker 网络不得改变上表选路。每次 P0、attempt、Kilo、canary 和部署验证都在首个请求前及恢复后
记录网络摘要和独立出口证明；坐标不符即停线，脚本不得改网络、NAT／iptables 或切换 ARM64 本机出口。
ARM64 环境 facts／receipt 必须记录 wg1 配置摘要、配置 MTU、运行时 MTU 和 DMIT 冻结 MTU；四项由
producer v3 强制校验。历史 v1/v2 收据只允许按登记摘要重放，不得用于新 P0 或部署门禁。

| P0 检查 | 必须证明 |
|---|---|
| 端口与恢复 | 实际调用容器可访问发布端口；hosts、CA、模型映射和 relay 按 before／after 完整恢复 |
| 隔离 | 每个 attempt 使用独立、权限为 `0700` 的 `HOME／CODEX_HOME`，不读取其他账号或前序缓存 |
| 模型目录 | Main／Lite 仅以 initialize-only 各请求一次；禁止 `thread/start`、turn、Responses 或 WS 预热；MITM 补采须显式验证目标版本的系统代理路由开关 |
| 出站与 TLS | DNS 冻结精确 IP 并在 CLI 计时前预连接，不得静默回退其他地址 |
| 运行坐标 | reservation 前确认 ID 不超过 128 字符，失败证据完成归档和收据重定位后才补跑 |
| 同源环境 | 工具、候选、finalizer、目标架构依赖摘要一致，完整环境烟测稳定通过 |
| 完整 Job 演练 | 展开目标版本全部官方／candidate Job，在 ARM64 实际 `capture-cli` 内逐项验证路径、依赖、环境变量、Job 身份和执行树摘要；演练不得发送官方请求 |

完整 Job 演练必须生成并重放工具就绪收据。任一 Job 未通过时禁止创建 Formal Campaign；修复后须重新完整演练并冻结工具摘要。

演练事实的 `jobs[]` 字段必须由采集器和 finalizer 使用同一闭集合同：基础身份字段始终必需，失败项必须
带 `error`，可选 `duration_seconds` 只能是有限的非负数。任何失败 Job 都必须先能封存、再能独立重放；
字段增删必须同步更新运行时校验、Schema／本手册和失败路径回归测试，不能等到 38 项执行完才在 finalizer
阶段暴露合同不一致。步骤 `environment` 的值严格服从场景 Schema；可选变量允许空字符串，演练校验器
不得擅自收紧为非空。
恢复 preflight 仅因 Campaign ID 改变时，演练器按 Framework §5.1.2 的封闭字段替换校验旧 Job 摘要；
校验通过的 Job 必须复用，不能把新的 `RUN_ID` 或证据目录名当成全量失效原因。
跨 Campaign 调用 `codex_upgrade_job_rehearsal_receipt.py collect` 时必须同时提供
`--previous-receipt <原相对路径>` 和 `--previous-receipt-root <原 evidence root>`；禁止把前序
receipt／facts 复制进新根后伪装成原始来源。
演练依赖映射中每个 `run_*`／`drive_*` runner 独立计算组件摘要，监督器与租约文件固定归入 `control`；
历史 facts 含完整工具条目时按当前映射只读重算，禁止因旧 `relay/shared` 粗分组扩大为全量演练。

P0 还必须在 ARM64 使用不小于本次“最大单一 manifest 边界”的实规模夹具验证：廉价前检失败时读取
0 字节；preview 完整扫描恰好一次；批准、普通 `status` 和 successor 读取原始证据 0 字节；同一根因的
第二个 successor 被拒绝；中断后从 checkpoint 续作而不是重跑。规模以 attempt 冻结根的逻辑字节数为准，
禁止把全部历史 Campaign 或归档累计成几十 GiB 的人造夹具。每项记录字节数和墙钟，任一最坏耗时无法
装入 Framework §5.3.5 预算即为 P0 阻断。该测试不得改变两张固定 Docker 网络或公网出口。
演练合同还必须绑定目标版本证据标签声明摘要，并验证声明与正式 official／candidate Job 集逐项完全一致；缺失、多余或旧版本声明均在 P0 失败关闭。

执行顺序固定为：先创建 `preflight_only` Campaign，再在 ARM64 运行以下三个离线命令；`collect` 只做路径、
依赖、语法、二进制、bubblewrap 和 zstd 探针，不执行 Job，也不发送官方请求。

```bash
mkdir -m 0700 "$JOB_REHEARSAL_ROOT"
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt collect \
  --campaign-dir "$PREFLIGHT_CAMPAIGN" --evidence-root "$JOB_REHEARSAL_ROOT" \
  --output facts.json
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt finalize \
  --evidence-root "$JOB_REHEARSAL_ROOT" --facts facts.json --output receipt.json
python3 -m tools.official_client_capture.codex_upgrade_job_rehearsal_receipt replay \
  --evidence-root "$JOB_REHEARSAL_ROOT" --receipt receipt.json
```

随后新建 Formal Campaign，并在原 `plan` 参数后追加
`--job-rehearsal-root "$JOB_REHEARSAL_ROOT" --job-rehearsal-receipt "$JOB_REHEARSAL_ROOT/receipt.json"`。
Formal 会再次独立重放收据，并拒绝目标场景、Job 集、工具树、容器、Codex／code-mode-host 路径或运行镜像漂移。

恢复例外只有一个：Formal 已进入 `VC-1～VC-6` 后发生产出侧工具变化时，可在当前 active 阶段新建
`preflight_only` 重做上述离线演练，再由 `successor` 绑定新收据；不得发送官方请求或推进阶段。
普通 Formal `plan` 仍只允许 `VC-0`。旧 Ledger 已停线时按 §4.0.2 建立受管恢复计时链，不得伪造 active。

从官方 GitHub Release 取得 ARM64 制品时同样不得把 DNS 轮询当作隐式重试。下载前必须在
`capture-cli` 内用 `codex_upgrade_official_asset_receipt.py` 逐一 TLS 预连接解析所得的全部 IPv4，
把唯一选中的成功地址、证书摘要、Release metadata、asset 大小和 SHA-256 封存并离线重放；实际
下载以收据中的 `curl_resolve` 精确固定该地址。全部地址失败、元数据漂移或下载字节不匹配均停线，
禁止改网络、改路由或转用未经登记的镜像站。

时间、归档复用、重试和门禁补跑统一遵守 Framework §5.3.5；`UpgradeTimingLedger` 从 DOC-PRE 首项开始。
每个事件的 `live-request-count` 是本事件新增量，不是累计值；累计值由 Ledger 自行求和。
创建运行目录前，ARM64 根文件系统须同时满足使用率低于 70% 且可用空间不少于 30 GiB。达到水位后仅按
manifest 清理未被收据引用的可再生缓存、worktree、镜像层和 staging，禁止删除证据或无界递归扫描。

## 4.1 官方目标版本取证

本步从目标官方源码和真实抓包整理第二部分完整编号规则；开始前必须通过 §4.0 的 DOC-PRE／P0。

### 4.1.1 输入与执行

统一编排入口为：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py --help
~~~

官方阶段一旦封存就只读复用。之后因产出侧工具修复、账号变化等原因需要新的正式 Campaign 时，
不重新取证，改用 `reuse-official-evidence` 把已封存官方阶段导入新 Campaign：新 Campaign 从
`official_sealed` 开始，只需重新执行 `classify` 与批准；同一份官方证据可以被连续导入多次。
产出侧工具变化后须先按 §4.0.5 用当前 preflight_only 完成完整 Job 演练，并把收据一并绑定。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py reuse-official-evidence \
  --predecessor-campaign-dir /绝对路径/已封存官方阶段的Campaign \
  --campaign-dir /绝对路径/新Campaign \
  --campaign-id <new-id> \
  --codex-account-id <当前可用账号-id> \
  --job-rehearsal-root /绝对路径/rehearsal-root \
  --job-rehearsal-receipt /绝对路径/rehearsal-root/receipt.json
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
| 3 | `SIDE=official bash tools/prepare_assertion_bundle.sh` | 从本 attempt 的冻结 Job 根生成 `assertion-bundle/capture-manifest.json` |
| 4 | `capture-official seal` | 校验恢复、权限、秘密扫描、inventory 和 finalizer，进入 `official_sealed` |
| 5 | `classify`（不传批准清单） | 生成官方差异和 `classification/draft/<revision>/` 五份草案 |

第 3 步必须显式提供 `CAMPAIGN_DIR` 和 `ATTEMPT_ID`；目标版本证据标签声明不存在时立即回到 P0 修复，不能临时手写 manifest。
每个 official Job 结束后、生成 assertion bundle 前，统一把证据目录／文件权限收口为 `0700／0600`。

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

`rule-migration/v1` 中 `entries[].classification` 表示规则迁移结论；
`discovery_classifications[].classification` 只表示本轮扫描形态的终态处置。后者标为 `change`
不等于对应规则必然变化，规则结论仍以 `entries` 为准；不得从 discovery 数量生成新规则。

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

源码、测试、文档或 Catalog 一旦变化，必须在当次提交生成后继 source transition 并重跑
`check-egress-spec`；不得累积多个未登记提交后再进入 ARM64 全门禁。

~~~bash
export PATH="$CAPTURE_HOST_DATA_ROOT/state/local/go1.27.0/bin:$PATH"
export GOPROXY=off
export GOFLAGS=-mod=readonly
python3 tools/official_client_capture/codex_upgrade.py stage-profile \
  --campaign-dir /绝对路径/campaign \
  --output /绝对路径/新的候选-runtime-catalog
~~~

`--output` 必须位于 Campaign 外、为尚不存在的绝对路径。工具从五份批准清单生成候选
RuntimeCatalog 和 `catalog-stage-receipt.json`，不修改仓库或生产 selector。收据必须证明：

执行前必须先创建 `--output` 的父目录并设为 `0700`；`--output` 本身仍须不存在。

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
6. 同批纳入目标版本的 test fact map、批准断言画像，并更新 trace 工具的默认路径与两个冻结摘要；
   三者版本或摘要不一致时禁止构建 candidate。

版本新增 route 可在确实不含该端点的单个 Release 中零匹配，但 Compiler 端点集合必须等于
Active／Previous 并集，且每条 runtime-bindable route 在并集中至少有一个 binding。只有现有
Snapshot、Plan、Bundle 或 Executor 无法表达新机制时，才最小修改共享层并专项复验两个 mode。

### 4.3.3 构建与退出条件

从完成入库和测试的同一最终源码树准备前端产物、运行资源、目标平台二进制和镜像，记录
Git／tree／build／部署版本、二进制 SHA-256、架构、构建参数、image ID、OCI digest 和
profile ID／digest。证据机和低资源生产机不承担 Go／Node 编译。
本 ARM64 环境缓存不完整时，前端依赖只经 `capture-cli` 固定出口取得；Go 仍离线编译，再将二进制和
运行资源叠加到已冻结的 ARM64 运行基础镜像，并在构建收据中绑定三者。

退出条件：stage receipt 可复算，目标画像和制品同源，旧 Snapshot／Release 仍可执行，实现侧
测试通过，生产 Active 未改变，第四步所需 candidate 身份字段齐备。

## 4.4 候选验证与封存

本步用第三步的固定制品运行目标画像，并从批准场景和真实第三方入口收集候选证据。候选按
`candidate_release_mode=previous` 显式选择目标 Release，不得借当前 Active 或客户端自报版本
选择画像。操作约束和故障预防统一由本节规定。

### 4.4.1 Candidate 身份冻结

采集账号、API Key ID、容器名、Codex 二进制路径或 compose 坐标与 Campaign 冻结值不同时，
不新建 Campaign：在该候选首个 attempt 前登记一份写一次的运行坐标覆盖收据，`run` 与 `seal`
都从同一份收据读取生效值，磁盘清单与 `campaign.sha256` 保持不变。目标源码树、官方包、
运行镜像、模型和证据根属于证据语义，不能这样覆盖。候选一旦有 attempt 或已封存，只能换
新的 candidate-id 再登记。

~~~bash
python3 tools/official_client_capture/codex_upgrade.py candidate-runtime-override \
  --campaign-dir /绝对路径/campaign \
  --candidate-id <candidate-id> \
  --reason "切换到当前可用采集账号" \
  --set codex_account_id=<账号ID> \
  --set service_container=<容器名>
~~~

运行前必须新签剩余有效期不少于 30 分钟的管理 JWT，保存为宿主机 `0400` 普通文件并设置
`ADMIN_BEARER_TOKEN_FILE`；禁止复用过期 token。编排器须在创建 reservation 前完成格式、权限和
有效期检查，缺失或过期时立即失败，不得先执行其他候选 Job。

~~~bash
export ADMIN_BEARER_TOKEN_FILE="$CAPTURE_HOST_DATA_ROOT/state/<upgrade-id>/admin-token"
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

Candidate MITM 矩阵不得继承 `capture-cli` 的 `CODEX_HOME`。每次 Job 必须创建独占空目录，只放入
`features.plugins=false`、禁更新和禁遥测配置，不复制 `auth.json`，结束时受控删除。0.151 的可选
MCP／插件发现流量不得进入模型场景；MITM 单场景超时固定至少 120 秒。每个 `subject×scenario` 使用
独立 run ID，场景结束立即写 JSONL／摘要 checkpoint；恢复只执行 checkpoint 缺失或失败的坐标，已通过
坐标禁止重发。临时上游关闭须封存当前场景并立即结束 Job，不得继续余下场景或重跑整个 Job。失败后只
重跑两个 MITM Job 中的实际失败坐标，已通过的 direct、wire、frozen Job 保持复用。正式补跑前先执行
`resume --rerun-failed --preview-recovery`；输出必须为两个失败项、其余七项复用，并明确
`reservation_exists=false`、`live_request_count=0`、`scanned_bytes=0`，否则不得追加真实请求确认参数。
预览命令仍须提供真实补跑使用的完整 Candidate 身份参数（runtime image、image ID、source、build、版本、
profile 和 purpose）；两次参数必须逐字一致。MITM wrapper 还必须在首次使用前显式初始化
`capture_mount=${CAPTURE_MOUNT:-/capture}`，离线测试未通过不得部署。

`EnableRequestCompression=true` 表示画像支持 zstd，不表示每次请求都必须压缩。Candidate 自定义 provider
入站未携带 `Content-Encoding: zstd` 时，普通 Responses 出站不加 zstd；Lite 条件另行成立。成功与失败
样本均无该 Header 时，不得把它误判为临时上游关闭的根因。

场景真实性门禁 `SCN-REALITY-01` 明确区分“job 退出成功”和“目标协议分支真实成立”。A11
realtime sideband、A13 OAuth refresh、A14 Files 三跳等高风险场景只有在原始 CLI／relay／pcap
或驱动事件能证明触发、关键中间事实和最终状态时才生成成功收据；编排器不得根据退出码补写。
收据还必须绑定 track、model、Lite 条件、evidence root、Campaign、attempt 和 `run_nonce`。

`run` 完成后、首次 `seal` 前，必须完成两条真实 Kilo 请求；其 ingress、runtime、response 和
usage 必须绑定本次 Campaign／attempt／`run_nonce`，并位于 attempt 开始与 client checkpoint
之间。两条入口统一使用 Campaign 已冻结的 `lite_model`；主轨 `model` 只用于官方／候选场景任务，
不得被 seal 隐式复用于 Kilo。历史 Campaign 未记录 `lite_model` 时只读重放才允许回退主轨模型。
两条请求之后不得再发送本 attempt 的客户端验证请求。
Kilo runner 在写入任何检查点或请求前必须把 `evidence/client` 及其全部新建子目录显式设为 `0700`；不得只收紧 `client/raw` 而留下可被 group/other 遍历的父目录。
Kilo Responses 的 `@ai-sdk/openai` provider 必须显式设置 `options.websocket=true`；首次 `seal`
前必须同时核验 Compatible 入站为 `POST/200`、Responses 入站为 `GET/101`，以及两条 usage 的
`openai_ws_mode` 分别为 `false/true`。任一项不符即放弃本 attempt，禁止先建立 client checkpoint。
时间门禁按传输语义校验：HTTP 仍要求响应完成后记账；WebSocket 的 usage 可能在连接关闭前或后落库，
只要求它晚于入站且所有事实均在同一 attempt 时间窗，禁止用 HTTP 顺序误拒绝真实 WebSocket 收据。

### 4.4.3 四阶段封存

1. **建立检查点**：两条 Kilo 请求完成后，首次执行 `capture-candidate seal`，采集
   `client-after` 并返回 `client_checkpoint_created`。
2. **生成收据**：在 candidate 源码树外运行受管生成器，形成 capture manifest、Go test trace、
   observed-profile 和两份 Kilo 收据。`build_*` 产物只是 finalizer 输入，不能直接提交给 seal；
   正式收据必须由受管 finalizer 生成。activation fact 必须由运行服务产生；测试 trace 必须来自
   同源树上的冻结测试日志，生成器不得合成二者。
3. **生成预览并完成唯一深度扫描**：

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

   工具先检查路径、符号链接、权限、属主、磁盘、身份和必需收据；任何廉价检查失败时不得读取原始
   内容。通过后只进行一次完整扫描，生成只写一次的 `evidence-manifest.json`、`seal-draft.json` 和
   `seal-preview.json`，记录扫描字节、耗时和根摘要并返回 `review_sha256`。扫描中断时从逐文件
   checkpoint 继续，已完成且边界未变的条目不得重新读取。
4. **批准封存**：人工复核预览后，只用 Campaign、candidate、attempt、用途和
   `--approve-seal-sha256 <review_sha256>` 批准。批准阶段只验证冻结草案、manifest 根摘要和不可变
   stat 边界，`scanned_bytes=0`；不得重新生成 surface、inventory 或 secret scan。通过后 Campaign
   进入 `candidate_sealed`。

普通 `status`、compare、accept 和 successor 都只重放 manifest／摘要链。`deep-verify` 只用于缺少
manifest 的历史导入边界，或人工明确要求的独立审计；不得由恢复判断隐式触发，也不覆盖历史文件。

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
- 每个 candidate Job 必须使用 Campaign 冻结的目标 Codex 绝对路径，并在 reservation 前逐字核验
  `codex-cli <target-version>`；不得使用 `codex-capture`、`PATH` 或脚本默认值间接选中旧版本。
- `candidate-frozen-aux` 还必须在修改环境前确认隔离分组只含目标账号，并已启用 Live 与图片生成；
  `--live-attestation-compose-files` 中每个 compose 文件都按 `-f` 参数解释，允许兼容历史首个裸绝对
  路径，但拒绝相对路径、符号链接、其他 compose 选项和 shell `eval`。只读前检失败不得执行恢复
  钩子或伪造 `restoration_failed`，首个真实修改前才允许武装恢复。全部 compose 文件合并解析后必须
  同时指向同一 candidate 镜像且 `candidate_release_mode=previous`，任一文件仍指向其他镜像或 mode
  都在 A11 重建前失败关闭。Formal `production_replacement` 不允许这两个 compose 参数为空；
  candidatecapture 注入或重建失败时立即停线，不得带着普通 Linux provider 继续 A11～A14。
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

selector 选择的 `record_type` 可能承载多个事实时，必须在 `where` 中声明字段存在性和适用条件；
断言读取的字段不是每条记录必有时，至少同时约束对应字段为 `operator=present`。例如 `SPEC-EP-002`
的 `file-url-chain` 只能选择同时存在 `data.create_upload_url_sha256` 与
`data.put_url_sha256` 的 `file_upload_chain` 记录，以免把 C2PA 正／负／retry 事实误纳入 URL 链断言。
不得放宽 `all_fields_equal` 或用 `any_equal` 掩盖缺失字段；selector 修正须走 Framework §5.3.4 的
`classification_fact_correction` 后继流程。

| validation mode | 机器判定 |
|---|---|
| `dual_wire` | 在官方和候选封存证据上执行同一规则的侧别检查，两侧均须通过 |
| `candidate_profile` | 在候选证据上验证 Sub2API 内部实现，并绑定批准的官方权威摘要 |

`results.json` 必须唯一覆盖目标规则全集；每条规则均为 `status=pass`、
`evidence_level=full`，不允许 fail、N／A、手写通过或未绑定 inventory 的证据路径。

### 4.5.3 accept 前置与正式验收

在同一 candidate 源码树执行 `make check-egress-spec`、`make test` 和目标平台测试。首次 attempt 必须
执行全部三项；每次 attempt 均在首项门禁前和末项门禁后生成 `gate_before／gate_after` ARM64 环境
收据，并以 attempt ID 为主体。把 attempt ID、可空的根因、前序失败收据引用、两份环境收据、命令、
工作目录、主机、架构、时间、退出码、通过／失败／跳过计数及输出证据写入权限为 `0600` 的
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

finalizer 固定检查首次 attempt 完整覆盖三项门禁，并绑定 formal 模式、Campaign／candidate 用途、
目标版本／架构、Profile、candidate package、源码树和镜像。门禁失败时生成 `status=failed` 的只读
收据并登记 `root_cause_id`；只有最终有效集合全部退出码 0、失败 0、跳过 0 的 `status=passed` 收据才
能进入 `accept`。缺项、替换命令、用途漂移、证据摘要漂移或身份不一致时不得执行 `accept`。

门禁补跑遵守 Framework §5.3.5：以唯一前序收据和 environment continuity 证明承接，只重跑失败项；
已通过项只从前序收据承接，禁止再次执行；每个补跑使用新的 facts／receipt 路径。同根因第二次仍失败
即停线，禁止第三次 attempt。身份变化时只重跑受影响闭集。无法证明承接的工具计为 P0 阻断，禁止
复制 JSON 或反复全量执行。

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

最终 production tree 只重新执行两条 `affected_rules`（`SPEC-EP-002`、`SPEC-HDR-005`）的实现测试和
公共终态门禁：

~~~bash
python3 tools/check_version_leak.py --self-test
python3 tools/check_version_leak.py
(cd backend && go test ./internal/service -run '^TestOfficialEgressVersionLeakAST')
~~~

继承规则、九项 Candidate Job 和 Kilo 只重放 canonical checkpoint，不再运行全量回归或目标架构全量测试。
正式镜像需绑定 candidate／production tree digest、
acceptance、promotion receipt、inventory、门禁结果、构建输入、image ID 和 registry manifest
digest；构建不得携带 `candidatecapture` 等候选取证专用标签。任一摘要不一致时禁止构建或部署。

首次 attempt 的六项结果必须完整写入 `post-promotion-gates.facts.json`；每次 attempt 同样绑定以
attempt ID 为主体的 `gate_before／gate_after` ARM64 环境收据、根因和可空的前序失败收据，并在
canary 前生成、重放 `post_promotion` 收据：

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
Profile、package、源码树和镜像必须与验收阶段完全一致。失败 attempt 只读保留并按 v3 合同仅补跑
`failed_gate_ids`；最终收据不是 `status=passed`、仍有失败或跳过、两份输入摘要漂移、production tree
不一致，均禁止构建正式镜像或开始 canary。

post-promotion 门禁同样遵守 Framework §5.3.5。promotion 前必须具备 candidate／active 双模式夹具并
证明 Go／Python 后继图一致；失败时保留旧 Active 或完整回滚。

### 4.6.3 独立 Active canary

使用正式镜像 digest 建立与生产隔离的 canary，独立使用账号、`CODEX_HOME`、数据库、Redis、
配置、网络和证据目录。canary 必须按晋升后 Catalog 的默认 `active` 运行，禁止以强制 mode
命中目标画像，也禁止直接复用 activation fact 显示 `profile_mode=previous` 的候选验证镜像。

核对镜像架构、启动、health、HTTP／WebSocket／TLS、错误率、Guard 和 activation fact；事实
必须绑定目标 version、profile／release digest 和正式镜像，强制 mode 计数为 0。真实业务须
出现 §4.5 规定的完成事件；失败不得进入生产。

### 4.6.4 正式切换

部署前复核 `docker compose config` 或等价结果，并再次确认 ARM64 wg1 持久配置／运行时 MTU 均为
1420、与 DMIT 冻结值一致；应用服务必须绑定
`repository@sha256:<manifest-digest>`，数据库、Redis、keeper、挂载和网络保持不变。标准更新为：

~~~bash
docker compose -f docker-compose.yml -f /绝对路径/production-image.override.yml \
  up -d --no-deps sub2api
~~~

远端 registry 尚未缓存固定 digest 时才先执行定向 `pull`；本机 registry 已有精确 digest 时不得
为形式完整重复拉取。两种情况都必须在切换前后复核实际 image ID／RepoDigest。

禁止 `compose down` 和无范围 `prune`。部署后复核 wg1 配置／运行时 MTU、固定容器 IP／出口、
容器 digest、compose、health、日志、依赖、
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

四阶段收据生成并重放后，必须从最新 canonical checkpoint 按顺序登记，禁止走旧 successor／epoch 链：

~~~bash
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir <campaign-dir> --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step production-activation --step-receipt <activation-receipt>
python3 tools/official_client_capture/codex_upgrade.py canonical-advance \
  --campaign-dir <campaign-dir> --candidate-id <candidate-id> --attempt-id <attempt-id> \
  --canonical-step rollback-verification --step-receipt <activation-receipt>
~~~

删除 0.147 必须另行生成消费者扫描为零的 RemovalReceipt，再用同一入口登记；删除失败不得影响已激活的
0.151，也不得回退或重跑 VC-0～VC-5。本轮已由 `codex-runtime-profile-removal/v1` 收据完成登记，
canonical checkpoint `00000009` 的 `plan.execute_item_ids=[]`。

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
