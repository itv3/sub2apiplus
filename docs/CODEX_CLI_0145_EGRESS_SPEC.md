# Codex CLI 0.145.0 出站规格、实现与演进手册

版本绑定：`codex-cli 0.145.0`<br>
依赖锁定：`hyper 1.8.1` / `hyper-util 0.1.20` / `http 1.4.0` /
`tungstenite 0.27.0` / `h2 0.4.13` / `reqwest 0.12.28`<br>
末次更新：2026-07-31

本文是 Codex CLI 0.145.0 OpenAI OAuth 出站形态及 Sub2API 对齐工作的**唯一人类可读
权威文档**。读完本文应能回答四个问题：

1. 官方规则如何由源码、依赖和抓包形成；
2. Codex CLI 0.145.0 的规则是什么；
3. Sub2API 如何用版本画像驱动的 Go 薄层实现这些规则；
4. Codex CLI 升级、合并 Sub2API 上游和重新验收时应怎样处理。

原始源码、抓包、JSON Schema 和可执行工具仍是机器证据，不复制进 Markdown。本文会给出
唯一入口、精确路径和证据边界；审核者理解完整逻辑不需要再阅读其他过程文档。
`docs/EVIDENCE_INDEX.md` 是由工具生成的逐规则证据索引，不是另一份设计说明。

| 部分 | 回答的问题 | 权威内容 |
|---|---|---|
| 第一部分 | 规则从哪里来、怎样复算 | 范围、证据、抓包、源码分析和规则准入 |
| 第二部分 | 官方 0.145.0 是什么形态 | 53 个编号项原文，其中 42 项属于 Sub2API 对齐范围 |
| 第三部分 | Sub2API 怎样实现 | 伪装架构、版本画像、运行链、文件台账和合并边界 |
| 第四部分 | 下一版本怎样升级 | 五份版本清单、分阶段抓包、双侧断言、启用和回滚 |

---

# 第一部分 规则如何形成

## 1.1 目标、范围与当前结论

目标是让 Sub2API 使用 OpenAI OAuth 账号出站时，在第二部分规定的范围内产生与
Codex CLI 0.145.0 一致的可观测形态。范围包括 TLS、协议协商、连接生命周期、
HTTP/1.1、条件 HTTP/2、WebSocket、header、body、端点和跨请求状态。

本版只描述：

- 官方版本：`codex-cli 0.145.0`；
- 凭据路径：内置 OpenAI OAuth；
- 官方实测平台：第二部分各规则明确记录的平台和条件；
- Sub2API 实现：版本画像驱动的 Go 薄层伪装；
- 客户端入口：官方 Codex CLI，以及最终收敛到同一画像的 Compatible／Responses 入口。

Anthropic、OpenAI API Key mimic、其他供应商和可合法关闭的 plugins、apps、analytics、
otel 流量不属于本版规则。自定义 CA 和自定义 provider 的条件分支仍保留在第二部分，
但不计入内置 OAuth 的 42 项实现目标。

本文共定义 53 个编号项，其中 39 条内置 OpenAI OAuth 可见规则和 3 条派生／内部机制属于
Sub2API 对齐范围，合计 42 项；完整分类、验证状态和规则正文见第二部分。第二部分是规则
定义的唯一事实源。规则数量不会因为抓包有限、实现困难或某次场景未触发而改变；证据充分度、
实现状态和规则是否属于目标范围是三个独立维度。

当前仓库已经完成 0.145.0 完整版本画像、稳定执行引擎以及官方／第三方入口的统一应用点；
42/42 结论只绑定实际验收时冻结的源码树、镜像、画像摘要和封存证据，不能由发布版本号或
当前工作树自动继承。

## 1.2 证据位置与完整性

| 证据层 | 位置 | 用途 |
|---|---|---|
| 官方生产源码 | `local-analysis/sources/codex-cli-0.145/codex-rs/` | 读取调用链、条件和内部机制 |
| 锁定依赖源码 | `tools/spec_source_deps/` | 解释 hyper、h2、http、tungstenite 等线形行为 |
| 官方抓包 | `local-analysis/captures/` | 证明运行时实际发出的 TLS、HTTP 和 WS 形态 |
| Sub2API 源码 | `backend/` | 定位伪装实现的画像、执行器和上游接入点 |
| 证据索引 | `docs/EVIDENCE_INDEX.md` | 机器生成的规则编号、运行号、路径和证据强度 |
| 引用锚点 | `tools/spec_ref_anchors.json` | 防止源码版本、符号和行号静默漂移 |

官方源码树、L2 依赖版本、抓包运行号和文件摘要必须同时绑定。只保存结论 JSON、截图、
HTTP 200 或测试日志，不能替代可重新解析的原始证据。

证据分为四种：

| 类型 | 内容 | 能证明什么 |
|---|---|---|
| **P** | 原始 pcap | ClientHello、SNI、TLS 扩展、连接时序等被动线索 |
| **R** | 等长脱敏的中继原始字节 | HTTP/1.1 线序、body、WebSocket 握手和帧 |
| **J** | JSON 解码摘要 | 只证明摘要实际保留的字段 |
| **M** | manifest 绑定的脱敏分析 | 只证明清单绑定字段和摘要 |

R 类对 Authorization、Cookie、账号和游标等值做等长替换，保持 header 名、大小写、偏移、
长度和帧结构。未脱敏材料只能保留在采集机私有目录。样本若存在非预期单向或空连接，只能
支持已观察到的正例，不能支持“从未出现”、连接总数或最大值等全称命题。

## 1.3 从源码和抓包形成规则

一条规则按以下顺序产生：

1. **锁定版本与环境**：冻结官方源码、Cargo.lock、CLI 二进制、平台、配置和账号范围。
2. **读取官方生产调用链**：从入口追到请求构造、认证、Client 生命周期、TLS 和依赖层；
   测试代码只能作辅助，不得作为官方行为的唯一依据。
3. **列出自变量与场景**：按传输、端点、Lite、压缩、状态、错误和配置分支形成场景矩阵。
4. **选择观测通道**：TLS/SNI 使用 P；HTTP/WS 应用字节使用 R；结构化日志和 manifest
   只能证明其保存字段。
5. **双向验证**：既要从源码推导应观察什么，也要从抓包反查每个出站面由哪条生产路径产生。
6. **收窄命题并编号**：写明范围、命题、可变性、源码、实测、实现要求、状态和证据边界。
7. **执行完整性门禁**：检查引用、样本、规则分类、证据索引和未声明缺口。

源码与运行证据正交：源码能说明机制和条件，不能单独证明平台 TLS 或最终线序；抓包能证明
实际输出，不能自动解释内部来源。规则只有在两类证据达到其命题需要的强度后才能定型。

## 1.4 证据等级、可变性与观测边界

| 等级 | 含义 | 使用约束 |
|---|---|---|
| **L1** | 官方生产代码直读 | 必须确认处于生产调用链 |
| **L2** | 锁定依赖的行为 | 必须绑定准确版本、feature 和提交 |
| **L3** | 运行实测 | 用于源码无法给出的平台或 wire 事实 |
| **L4** | 单元测试、mock、合成输入 | 只能辅助，不得单独定义官方规则 |

规则中的“固定、随机、条件”分别表示每次一致、每次可变化、随明确自变量变化。随机项不能
钉死为单一样本；条件项必须在真实条件下验证，不能把代理、CA 或失败注入后的结果外推到
默认路径。

观测遵守以下边界：

- pcap 看不到加密后的 HTTP/WS 内容；中继字节不能替代客户端直连的 TLS 事实；
- 验证域名全集时，过滤条件不得预先包含被测域名；
- 自定义 CA 位于 HTTP TLS 选型上游，不能用于证明默认 TLS 画像；
- 受控干预只证明客户端收到指定输入后的反应，不自动等于自然成功链；
- plugins、apps、analytics、otel 等可关闭流量不作为必须出现的规则；
- h2、MITM 或服务端重建的连接不能冒充客户端原始 HTTP/1.1 线序。

## 1.5 证据复算、重新抓包与规则收口

本节覆盖同一证据工作流的两种使用场景：审核已有证据时直接运行离线审计工具；发起新版本
升级或重新验收时，通过统一编排入口启动新一轮抓包、比较和验收。两者不是平行工作流。

**审核官方 0.145.0 已有证据。** 以下命令只重算已归档证据，不启动真实客户端或网络任务：

```bash
# 样本完整性与中继字节解析
python3 tools/official_client_capture/check_sample_integrity.py \
  local-analysis/captures/raw-scrubbed/relay-imgedit1/relay
python3 tools/official_client_capture/relay_extract.py \
  --relay-dir local-analysis/captures/raw-scrubbed/relay-imgedit1/relay \
  --output /tmp/codex-0145-relay-check.json

# TLS ClientHello、SNI 和 ALPN 复算
python3 tools/official_client_capture/pcap_clienthello.py --by-subdir \
  --dir local-analysis/captures/wire-parity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/direct

# 文档、源码引用和证据门禁
python3 tools/check_spec_refs.py --symbol --cfg-test
python3 tools/spec_status.py --check
python3 tools/check_sample_counts.py
python3 tools/evidence_index.py --check
python3 tools/check_gaps.py
python3 -m unittest tools/test_check_spec_refs.py

# 实现侧门禁：台账完整性与版本标识符泄漏
python3 tools/check_ledger_completeness.py
python3 tools/check_version_leak.py
```

精确运行号、规则编号与证据路径由 `docs/EVIDENCE_INDEX.md` 生成。审核者不需要凭目录名猜测
哪次抓包有效。

**发起新一轮抓包或验收。** 新版本升级或重新验收时，发起官方抓包、Sub2API 候选抓包、
差异分类、离线比较和验收的唯一编排入口是：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

底层 `capture.py`、场景驱动器、解析器和 finalizer 是该入口的受控依赖，不再作为平行
工作流。完整升级步骤见第四部分。

## 1.6 规则准入、变更与停止标准

新增或修改规则必须满足：

1. 命题属于已声明的官方出站范围；
2. 已定位官方生产路径或明确属于只能实测的 L3 事实；
3. 已选择能够观察该命题的通道，并记录盲区；
4. 正例、负例和条件分支足以排除已知替代解释；
5. 规则编号、源码引用、运行号和证据摘要能够重新计算；
6. Sub2API 的实现要求描述可见结果，不强制复制官方内部类或函数；
7. 规则全集、版本画像、场景和断言画像之间没有未分类项。

停止采样不等于假定未来永远不变。达到场景覆盖、重复样本和源码闭环后，只能把结论限定在
当前版本、平台和条件内；Codex CLI 版本、依赖、平台或配置分支变化时，必须按第四部分重新
发现和分类。

---

# 第二部分 规则

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

对固定转发到 OpenAI 官方 OAuth 上游的实现口径：

- 对齐范围始终是 **39 条可见 wire 规则 + 3 条机制要求 = 42 项**；规则数量不随证据
  充分度改变，`SPEC-EP-012` 也属于必须实现和验收的 39 条可见规则。
- `SPEC-EP-012` 当前以源码和受控 200 第二跳证据闭合规则形态，但自然 Voice/realtime
  成功样本仍标记为抓包有限；该证据限制不再把 42 项口径缩减为 41 项。
- ② 仅在配置有效自定义 CA bundle 时触发；产品不开放该能力时无需实现。
- ③ 仅适用于 Codex 自定义 provider；固定使用 OpenAI 官方上游时无需实现。
- ④ 不要求复制官方内部代码结构，但必须对齐其产生的可见结果；⑤ 只用于证据审计，
  不是线上出站实现要求。

功能条件不是额外分组：images、alpha-search、legacy compact、realtime 和条件 header
等规则，只在各自触发条件成立时产生对应形态。

每个编号项统一使用“范围—规则／机制／记录—源码—实测—实现—状态”六个字段。
“实现”描述的是可见行为要求；源码内部函数、语言或类结构无需照搬。

## 2.1 TLS

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
  默认客户端失败回退见 `login/src/auth/default_client.rs:238-251`。
- **实测**：`audit-tls002-ca-n0-20260730a`（P）验证 ClientHello；
  `official-h2-20260727T131936Z`（J）验证协商为 h2。
- **实现**：只有有效 CA bundle 才进入该分支；变量未设置、空值或证书无效不得按 h2 画像处理。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-TLS-003 WS ClientHello 扩展顺序不固定

- **范围**：内置 OpenAI OAuth；WS。
- **规则**：样本中的 WS ClientHello 扩展集合相同，但四次排列均不同；不得把某一次
  扩展顺序硬编码为固定常量。
- **源码**：[L3] 排列行为由抓包确认；WS 恒走 rustls 的归因见
  `websocket-client/src/lib.rs:45`。
- **实测**：`oauth-20260727T091556Z-noplugins`（P）取得四种扩展排列。
- **实现**：使用等价 rustls 行为；不要求每次都产生全新的排列，也不把有限样本外推为全局集合。
- **状态**：✅ 源码无／不适用；抓包充分。

## 2.2 协议与连接

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
- **源码**：[L1] `model-provider-info/src/lib.rs:140`、
  `core/src/client.rs:509`、`core/src/client.rs:930`、
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
- **源码**：[L1] `login/src/auth/default_client.rs:238`、
  `core/src/client.rs:964-977`、`codex-api/src/endpoint/session.rs:80-154`、
  `model-provider/src/models_endpoint.rs:75`、`ext/image-generation/src/backend.rs:27`、
  `ext/web-search/src/tool.rs:91`。
- **实测**：`clean2-conn-20260728T132008Z`、`audit-conn001-image-repeat-20260730a`、
  `audit-conn001-search-repeat-20260730a`、
  `audit-conn001-retry-keepalive-openai-http-20260730a`、
  `audit-conn001-retry-disconnect-openai-http-20260730a`（均为 R）。
- **实现**：按“上层调用”划分 Client 生命周期；不得把主模型链结论外推到 wham 等 backend-client。
- **状态**：✅ 源码充分；抓包充分。

## 2.3 HTTP/1.1

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
  `swap_remove` 时也不能把结果简化为原始插入序。
- **源码**：[L2] `tools/spec_source_deps/http-1.4.0/src/header/map.rs:923-928`、
  `tools/spec_source_deps/http-1.4.0/src/header/map.rs:1572-1602`；各端点的构造顺序由
  L1 调用链决定。
- **实测**：`audit-h1raw-20260730a`、`audit-ep014-turnstate-echo-20260730a`（R）
  验证 models、responses 及条件 turn-state 插槽。
- **实现**：逐端点复刻最终线序，不得使用统一字典排序或一份 header 并集。
- **状态**：✅ 源码充分；抓包充分。

## 2.4 HTTP/2（仅自定义 CA）

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

## 2.5 WebSocket

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
- **源码**：[L1] 注入入口 `model-provider-info/src/lib.rs:116`；[L2]
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
- **源码**：[L1] `codex-api/src/common.rs:266-292`、
  `core/src/client.rs:1604-1641`、`core/src/client.rs:897`。
- **实测**：`clean-tool-20260728T132346Z`（R）覆盖 Lite；
  `audit-ws005-nonlite-20260730a`（R）覆盖非 Lite、warmup 与增量帧。
- **实现**：按 serde 条件省略字段；不得把 Lite 的 13 项子集或“首轮／后续轮”写成固定规则。
- **状态**：✅ 源码充分；抓包充分。

## 2.6 Header

### SPEC-HDR-001 请求 header 的内部组装顺序

- **范围**：派生／内部机制。
- **机制**：请求先由 provider 构造，再合并端点额外头、body 和 configure 结果；
  每次 retry 最后执行认证。流式路径还会先转为 prepared request。Client 默认头是
  与请求级头并行的入口。
- **源码**：[L1] `codex-api/src/endpoint/session.rs:48`、
  `codex-api/src/endpoint/session.rs:80-154`、
  `login/src/auth/default_client.rs:310`。
- **实测**：不适用；wire 只能证明最终 header 集合与线序，不能反推内部调用顺序。
- **实现**：内部代码可不同，但覆盖、认证重试和最终线序结果必须与各可见规则一致。
- **状态**：— 源码充分；抓包不适用。

### SPEC-HDR-002 Client 默认 header 集合

- **范围**：内置 OpenAI OAuth。
- **规则**：Client 默认头为 `originator`、`user-agent`，以及条件性的
  `x-openai-internal-codex-residency`。residency 来自受管理的 requirements
  配置项 `enforce_residency`，不是环境变量。
- **源码**：[L1] `login/src/auth/default_client.rs:54`、
  `login/src/auth/default_client.rs:101-106`、
  `login/src/auth/default_client.rs:354-367`、
  `config/src/config_requirements.rs:868`、
  `exec/src/lib.rs:484`、`tui/src/lib.rs:1171`、
  `app-server/src/request_processors/initialize_processor.rs:132`。
- **实测**：`official-body2-20260728T000549Z`（J）验证默认集合；
  `audit-hdr002-residency-20260730a`（R）验证 `us` 正向分支。
- **实现**：未设置 residency 时不发；设置时按端点最终线序合并。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-004 OpenAI-Beta 仅由 Codex 加在 WS 握手

- **范围**：内置 OpenAI OAuth。
- **规则**：Codex 自身只在 WS 握手发送
  `openai-beta: responses_websockets=2026-02-06`；HTTP responses 和 images 不发送。
- **源码**：[L1] `core/src/client.rs:141`、`core/src/client.rs:1094`、
  `cli/src/doctor.rs:93`、`cli/src/doctor.rs:2382`。
- **实测**：`clean-tool-20260728T132346Z`（R）为 WS 正例；
  `audit-body002-plain-20260730a`（HTTP responses）、
  `clean-image-20260728T132405Z`（generations）、
  `relay-imgedit1`（edits）为 R 类反例。
- **实现**：只在 WS 握手添加；自定义 provider 主动注入同名头不属于本条。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-HDR-005 user-agent suffix

- **范围**：内置 OpenAI OAuth。
- **规则**：UA 为平台前缀加可选 ` ({name}; {version})` suffix。exec 使用
  `codex_exec/0.145.0` 与 `(codex_exec; 0.145.0)`；TUI 使用
  `codex-tui/0.145.0` 与 `(codex-tui; 0.145.0)`。启动首次 models 因进程级写入时序，
  有 suffix 和无 suffix 均属于官方观测集。
- **源码**：[L1] `login/src/auth/default_client.rs:41`、
  `login/src/auth/default_client.rs:161`、
  `app-server/src/request_processors/initialize_processor.rs:90-133`、
  `cloud-tasks/src/util.rs:10`。
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
  `core/src/realtime_conversation.rs:1606`。
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
- **源码**：[L1] `core/src/responses_metadata.rs:243-317`、
  `core/src/client.rs:721-754`、`core/src/client.rs:1089-1101`、
  `features/src/lib.rs:925-929`。
- **实测**：`relay-review4`、`audit-hdr008-guardian-20260730a`、
  `audit-hdr008-memgen-20260730a`、`relay-rtmetrics1`（R）。
- **实现**：按条件插入；`x-openai-subagent` 的 `Other(label)` 不得实现成封闭枚举。
- **状态**：✅ 源码充分；抓包充分。

## 2.7 Body

### SPEC-BODY-001 Responses 顶层字段由传输结构体封闭

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：HTTP 由 `ResponsesApiRequest` 序列化；WS 由
  `ResponseCreateWsRequest` 序列化并增加外层事件类型。不得发送对应结构体外字段。
- **源码**：[L1] `codex-api/src/common.rs:216-292`。
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
- **源码**：[L1] `features/src/lib.rs:1027-1030`、
  `core/src/session/session.rs:1120`、`http-client/src/request.rs:41-43`。
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
- **源码**：[L1] `core/src/client.rs:804-820`、
  `core/src/client.rs:843-864`、`core/src/client.rs:897`。
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
  `codex-api/src/endpoint/responses_websocket.rs:730-733`、
  `core/src/client.rs:1561-1565`、`core/src/client.rs:1885-1901`。
- **实测**：`audit-ep014-turnstate-echo-20260730a`、
  `audit-ep014-turnstate-compact-20260730a`、
  `audit-body004-ws-turnstate-20260730a`（R）完成三条输入→保存→回送闭环。
- **实现**：Sub2API 若终结上下游流，必须保存并回送；透明转发时不得丢失或重复生成。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-BODY-005 tool_choice 是字符串

- **范围**：内置 OpenAI OAuth；HTTP 与 WS。
- **规则**：`tool_choice` 为 JSON 字符串，当前值 `"auto"`，不是对象。
- **源码**：[L1] `codex-api/src/common.rs:223`、`core/src/client.rs:896`。
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
- **源码**：[L1] `codex-api/src/common.rs:217-238`、
  `core/src/client.rs:804-897`。
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

## 2.8 端点与辅助链

### SPEC-EP-001 生图工具呈现与独立 images 调用

- **范围**：内置 OpenAI OAuth；模型支持图像生成时。
- **规则**：非 Lite 可在顶层 `tools` 中发送 namespace `image_gen`／工具
  `imagegen`；Lite 将能力放入 `input.additional_tools` 的 exec 工具目录。
  模型调用后由客户端请求独立 `images/generations` 或 `images/edits`。
- **源码**：[L1] `core/src/tools/spec_plan.rs:93-94`、
  `tools/src/tool_spec.rs:17-40`、`ext/image-generation/src/backend.rs:27-64`、
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
- **源码**：[L1] `model-provider-info/src/lib.rs:329-332`、
  `login/src/auth/manager.rs:189`、`core/src/realtime_conversation.rs:1135-1142`、
  `core/src/mcp_openai_file.rs:145-182`、`codex-api/src/files.rs:131-290`。
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
- **规则**：`GET {base}/models?client_version=0.145.0`。
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
  `core/src/realtime_conversation.rs:1135`、
  `core/src/realtime_conversation.rs:1165-1183`、
  `core/src/realtime_conversation.rs:1595`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:729-793`、
  `codex-api/src/endpoint/realtime_websocket/methods.rs:896-971`、
  `core/src/realtime_conversation.rs:1701-1707`。
- **实测**：`webrtc-20260728T134028Z`（R）自然第一跳返回 400；
  `live2-20260728T140403Z`、`audit-ep012-realtime-20260730a`（R）自然第一跳返回 403；
  `audit-ep012-sideband-synth-20260730a`（R）以受控 200 触发官方客户端第二跳。
- **实现**：具备 realtime 条件后按双跳链实现；当前不得把受控 200 写成生产自然成功。
- **状态**：🟡 源码充分；抓包有限。Voice/realtime 自然成功补采暂缓。

### SPEC-EP-013 内置 provider 的 query 边界

- **范围**：内置 OpenAI OAuth；由 `Provider::url_for_path()` 构造的 Codex API URL。
- **规则**：provider 级 `query_params=None`；query 只由端点自身添加。目前 models
  添加 `client_version`，realtime/calls 添加 `intent` 与 `architecture`，
  普通 responses 不带 query。
- **源码**：[L1] `codex-api/src/provider.rs:53`、
  `model-provider-info/src/lib.rs:339`、
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
- **源码**：[L1] `core/src/client.rs:599-617`、`core/src/client.rs:1885-1902`、
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

### SPEC-EP-019 wham 路径与线序

- **范围**：内置 OpenAI OAuth；backend-client。
- **规则**：使用
  `GET /backend-api/wham/usage`、
  `GET /backend-api/wham/rate-limit-reset-credits`、
  `POST /backend-api/wham/rate-limit-reset-credits/consume`。
  两个 GET 的 header 线序为
  `user-agent, authorization, chatgpt-account-id, accept, host`；
  consume 再含 `content-type, content-length` 与 `redeem_request_id` body。
- **源码**：[L1] `backend-client/src/client/rate_limit_resets.rs:14-18`、
  `backend-client/src/client/rate_limit_resets.rs:30-109`、
  `backend-client/src/client.rs:212-231`。最终线序仍由 wire 确认。
- **实测**：`clean-legacy-20260728T132509Z`（两个生产 GET）与
  `audit-ep019-wham-consume-safe-20260730a`（无外网安全 consume），均为 R。
- **实现**：使用 backend-client 独立 header 形态；不得套用 Codex 主模型端点线序。
- **状态**：✅ 源码部分；抓包充分。

### SPEC-EP-020 legacy compact 的 body

- **范围**：内置 OpenAI OAuth；legacy compact。
- **规则**：结构体字段为
  `model, input, instructions?, tools?, parallel_tool_calls, reasoning?,
  service_tier?, prompt_cache_key?, text?`。现有 wire 子集为
  `model, input, parallel_tool_calls, reasoning, prompt_cache_key, text`。
- **源码**：[L1] `codex-api/src/common.rs:26-41`。
- **实测**：`clean-legacy-20260728T132509Z`（R）。
- **实现**：按 Option 条件省略；不得使用更宽的 ResponsesApiRequest。
- **状态**：✅ 源码充分；抓包充分。

### SPEC-EP-021 默认压缩使用 Remote Compaction V2

- **范围**：内置 OpenAI OAuth；默认配置。
- **规则**：默认走普通 `/responses`，并向 input 追加
  `{"type":"compaction_trigger"}`；manual 与 auto 都如此，不调用
  `/responses/compact`。
- **源码**：[L1] `core/src/compact_remote_v2_attempt.rs:78`、
  `model-provider-info/src/lib.rs:417-418`、
  `features/src/lib.rs:1379-1382`、`core/src/tasks/compact.rs:44`。
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
  `ext/image-generation/src/tool.rs:270-327`。
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
- **源码**：[L1] `analytics/src/facts.rs:368-381`、
  `core/src/tasks/compact.rs:35-76`、
  `core/src/compact_token_budget.rs:20-89`、
  `core/src/session/turn.rs:801-990`、
  `core/src/compact_model_fallback.rs:29-39`、
  `core/src/compact_remote_v2_attempt.rs:78`。
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
  `tui/src/chatwidget/slash_dispatch.rs:256`。
- **实测**：`relay-tui-recap-20260728T112358Z`（TUI 正例）与
  `audit-ep024-exec-negative-clean-20260730a`（exec 负例），均为 R。
- **实现**：不实现采集入口；产品若提供 TUI 兼容层，再按 surface 区分 slash command。
- **状态**：✅ 源码充分；抓包充分。

# 第三部分 Sub2API 伪装方案实现

## 3.1 版本画像驱动的运行架构

现有路由、鉴权、账号调度、协议转换、重试、流式响应和计费继续由 Go 承担；官方 OpenAI
OAuth 请求在最终出站前进入版本画像驱动的定型层。

```text
HTTP／WS 入口、辅助端点与内部任务
→ IngressSnapshot（脱离 Gin）
→ OfficialRouteCatalog 两阶段解析物理 route 与权威 SinkBinding
→ 按 persona 进入对应逻辑出口
   ├─ codex-cli → ReleaseCatalog（每次 invocation 只解析一次）
   │             → 不可变 ReleaseBundle
   │             → CodexEgressPlan
   │             → CodexEgressExecutor
   └─ chatgpt-web/chrome → BrowserEgressPlan → 浏览器身份出口
→ PreparedRequest + FinalizationToken + TransportSpec
→ HTTPUpstream／req-profile／WebSocket adapter
→ persona-aware Runtime Guard
→ OpenAI 上游
```

伪装实现由两层组成：

| 层 | 职责 | 约束 |
|---|---|---|
| 稳定执行引擎 | 解析画像、收集运行上下文、定型 URL/header/body、选择传输并管理状态 | 不包含按版本散落的常量和模型特判 |
| 不可变版本画像 | 保存某一 Codex 版本的 surface、端点、字段、线序、条件、连接和传输契约 | 新版本新增快照，不原位修改旧版本 |

画像选择由账号类型与 registry 的 release 指针完成，与入站客户端身份无关。Compatible
与 Responses 第三方入口使用相同画像，但必须先经过协议适配，再在最终出站点统一定型。

**入站身份不参与出站形态决策。** 只要账号是 OpenAI OAuth 且端点在画像闭集内，出站
一律由账号绑定的 active 画像定型：不区分官方或第三方客户端，不区分客户端声明的版本、
平台或 surface。改变出站画像的唯一途径是显式升级 active 画像。

依据是入站声明对上游不可见：网关在最终出站点用画像覆盖客户端身份，上游只观察到一个
自洽的 active 版本形态。入站写了什么既不会泄漏，也不构成拒绝服务的理由。反之，按入站
身份分流才是真正的风险——同一账号同一 IP 上出现两种出站形态，其不一致本身就是最强的
识别特征（同类教训见 SPEC-TLS-001 与 §3.5.2 的 `openai_gateway_count_tokens.go`）。

因此入站身份校验一律降级，不阻断请求：

- UA 匹配不上任何画像 surface：投影到画像默认运行态；
- originator 与 surface 不一致：按画像 originator 出站，仅保留 SPEC-HDR-005 的
  首次 models 阶段识别；
- 条件头证据缺失或与 turn metadata 冲突：按“条件不成立”处理，不发这些条件头。
  条件头本就是官方的条件性行为，不发产生的是官方最常见的合法形态。
- 顶层字段超出画像闭集：按闭集投影丢弃并告警，不拒绝请求。官方入口与派生入口在这一
  点上处理相同——严格入口此前是失败关闭，但官方客户端先于画像升级时，那会让真·官方
  新版客户端在整个升级窗口内不可用，而这恰恰是最不该失败的时刻。
- 已知字段的值不合契约（如 `tool_choice` 不是 `auto`）：按画像规范化，不拒绝请求。

每处降级都必须产生可观测信号。投影路径按画像闭集丢弃未知顶层字段（见 §3.3.2）时同样
必须告警：新版本客户端的新增字段会在此被抹掉，表现为“功能不生效但请求成功”，这是本
方案唯一的功能性风险面，也是运行时唯一能提示“该升级画像了”的信号。告警按“入口类型 +
字段名组合”去重，每种组合在进程内只报一次——投影发生在每个请求上，逐请求打印会淹没
日志，而重复出现同一组字段并不增加信息量。

唯一保留的失败关闭是账号侧配置错误（如 residency 配置了非法值）。那属于运维配置问题，
与客户端能否使用无关。

## 3.2 Codex 0.145.0 完整画像

`official_egress_codex_0145_profile.go` 保存完整封闭快照，包括：

- exec／TUI 两个 surface 的 user-agent、originator 和终端身份；
- 默认 feature、Remote Compaction V2、请求压缩和 Lite 能力来源；
- HTTP 默认传输、WS rustls 传输及连接生命周期；
- models、responses HTTP/WS、compact、alpha-search、images、realtime、wham、
  OAuth refresh 和文件上传端点；
- 每个端点的 method、host、path、query、accept、content-type、压缩、header 槽位、
  body 闭集和动态值来源；
- turn-state、subagent、memory、parent thread、residency、runtime metrics 等条件关系；
- 文件上传三阶段和服务端返回 URL 的唯一例外边界。

画像构造后编码成不可变规范快照，并在进程启动时一次性解码、结构校验和摘要核对，
任一步失败都在启动期暴露而非请求期。此后解析返回的是进程内只读单例，调用方不得
写入其字段；执行器唯一会就地改写的端点数据仍按次深拷贝，需要改写整份画像的场景
（画像 mutation 测试、未来的版本派生）必须显式取深拷贝。当前画像摘要由测试固定为：

```text
9b7dd12df50dbcff74594b1f05440161cd99b963019a4f316f20c08ed5f5ba1e
```

摘要变化表示完整画像发生变化，必须同步检查第二部分规则、版本清单、测试和抓包证据。

**版本快照目录。** 正式运行时通过 `internal/officialegress` 的 ReleaseCatalog 读取
`release-catalog.json`、release graph 和 SnapshotCatalog；快照以 `version + digest` 为
不可变坐标，未登记坐标明确失败，不回退到其他快照。active/previous 由发布图选择，业务
调用方只提交 release mode 和业务事实，不能自行选择具体版本或 transport ID。

**版本解耦已落地。** 传输画像按协议（`http/1.1`、`websocket`）解析，body 字段序、URL、
Header、状态键和连接生命周期均由启动期编译的 ExecutableProfile 进入统一 Executor。
变更集 5 将版本无关生产符号中的 `0145` 清零；生产标识符闭集只允许
`officialCodexVersion0145` 这一画像证据常量，以及
`openAIAPIKeyCodexMimicClientCodexExec0145` 这一不得机械改写的 API Key mimic 历史画像
绑定 ID。允许清单、文本泄漏基线和 Go AST 裸版本基线均在删除后立即收紧，不能回升到旧
水位。

画像校验分为证据与执行两层：SnapshotDoc/ProfileSpec 保留版本事实，ExecutableProfile
执行闭集枚举、URL、Header、Body、transport 和资源生命周期校验。新增版本必须追加新的
不可变快照坐标、发布图节点及对应证据，不能原位覆盖旧快照，也不能修改 §3.5.2 的共享
接入点来散布版本分支。快照内容仍必须来自第四部分完整抓包验收 Campaign，不能手工推断。

## 3.3 最终出站定型

### 3.3.1 运行上下文

入口先保存原始客户端 surface、user-agent、originator、压缩 feature 和会话信息；账号
选定后再绑定 OAuth、目标版本、端点、模型能力、Lite、turn-state 和条件 header。运行
上下文只在当前上层调用或 WS 连接内有效，不能跨无关请求复用。

### 3.3.2 URL、header 与 body

版本画像拥有 host、固定 path 和固定 query；执行器只接受画像声明的动态 path/query。
文件 blob 上传使用服务端返回的完整签名 URL，是唯一允许完整动态 URL 的端点。

header 按画像槽位和槽内序号生成，逻辑名与 wire 名分开保存。HTTP/1.1 在最终写出前完成
小写、顺序、host 和 content-length 定型；WS 保留 tungstenite 前五项固定大小写，并按
画像规定处理剩余 header。body 使用封闭字段集合和稳定编码顺序，未知顶层字段不能透传。

### 3.3.3 状态、连接与端点编排

turn-state 按调用／连接身份隔离并从官方响应指定位置更新。HTTP Client、调用内 retry、
WS 连接和后端长生命周期 Client 由端点画像分别声明，不能用单一全局池推断。

**turn-state 的跨请求复用只在会话身份来自入站显式锚点时成立**：入站会话头，或入站 body
**显式携带**的 `prompt_cache_key`。

这里的“显式”必须按 `promptCacheKeySet` 判断，不能只看键值是否非空：Chat Completions
兼容链会按模型、工具、system 与首条用户消息自动生成一个 `prompt_cache_key`，生成路径
明确不设该标志。那种键与首条消息兜底同样只保证“同内容得到同一个 ID”，若当作显式锚点，
隔离等于没做。

第三方请求缺少这些锚点时，会话身份由消息内容兜底推导：它只保证“同内容得到同一个 ID”，
不保证“同一个 ID 属于同一个会话”——同账号、同 API Key、同 UA 下两段开头相同且当前末条
消息也相同的独立对话会算出同一身份。此时放弃复用，因为缺失 turn-state 按 §3.1 只是不发
该条件头（官方最常见的合法形态），而把一段对话的上游状态句柄送进另一段没有安全的降级
形态。身份本身仍保持确定性，工具结果续轮因此仍复用同一个 turn。

images、compact、alpha-search、realtime、wham、OAuth refresh 和文件上传均通过画像端点
执行，不允许旁路通用请求器绕过定型。

## 3.4 42 项覆盖责任

| 责任面 | 数量 | 实现位置 |
|---|---:|---|
| TLS、协议、连接、h1、WS | 13 | 传输画像、`tlsfingerprint`、HTTP/WS 执行器 |
| Header、Body、端点可见行为 | 26 | 端点画像、header/body finalizer、端点编排 |
| 内部机制 | 3 | 运行上下文、turn-state、压缩选择 |
| **合计** | **42** | **同一版本画像和同一验收链闭合** |

一条规则只有同时具备画像／执行点、官方证据、候选证据和机器断言，才算完成。通用兜底、
单元测试、HTTP 200 或整体 surface 相似都不能替代逐规则验收。

## 3.5 源码改动台账

本节是以后合并 Sub2API 上游时的维护边界。`Fork 新增`表示文件不属于上游基础实现，应尽量
保持自包含；`上游接入点`表示合并时最容易冲突的共享文件，必须逐项复核。测试文件与生产
文件同名或在对应目录内，不逐个重复解释，但不能在合并时省略。

本次来源分类使用 upstream 基线 `26d894ef4f50645a4bf1030e378ac892f17d0223`
（2026-07-31，合并 v0.1.169 时的上游 tip）。以后合并上游时必须先记录新的明确 commit，
再刷新本台账；不能用未更新的远端引用继续判断“上游已有”。

上一版基线 `12d811bd7`（2026-07-09，v0.1.149）长期未刷新，其间已合并 13 次上游，
后果有两处：一是把上游自身演进算成了 fork 改动，冲突面因此高估约 2.4 倍；二是
`openai_live.go`（handler 与 service 两处）、`openai_alpha_search.go`、
`attestation.go` 在这段时间里进入上游，却仍被标为“Fork 已有”。本地 `upstream/main`
引用恰好也停在旧基线上——这正是本段警告的“用未更新的远端引用判断来源”。

**登记判据。** 一个生产 Go 文件只要引用 Codex/OpenAI 出站定型专属符号，就必须登记，
无论它是否携带版本字面量——WS 传输、连接池与握手定型点都不含版本号，却直接决定
ClientHello 与握手线序。判据刻意不使用通用的 `OfficialEgress` 前缀：该前缀同时覆盖
Anthropic 画像，会把 §1.1 已排除的供应商路径卷进来。Anthropic、API Key mimic 及其他
供应商路径不在本台账范围内。

**同步机制。** 本台账曾长期手工维护且无同步机制，因而漏登 10 个出站定型文件，其中
包含 WS TLS 画像解析、连接池身份 key 和 OAuth count_tokens 的 TLS 复用。现由门禁
按上述判据自动复算：

```bash
make check-egress-spec   # 判据自测 + 台账完整性 + 版本泄漏 + 规格引用
```

台账检查比对应登记文件集合与本节实际登记内容；版本泄漏检查按
`tools/version_leak_baseline.json` 的基线，禁止共享业务层与通用执行层新增版本标识符。
四项均已接入 `.github/workflows/backend-ci.yml`，不再依赖人工执行。

**两处判据本身必须与版本号解耦。** 早期实现把 `0.145.0`、`Codex0145` 写进正则，结果是
升级时整组失效：新代码写 `Codex0146` 或 `"0.146.0"` 完全不命中，而升级恰恰是这两个门禁
最该起作用的时刻。现按标识符形状匹配，`--self-test` 用内联样本把“判据是否仍与版本解耦”
本身变成机器断言。

**版本泄漏检查按证据类型分工。** 判断一个裸版本字面量算不算泄漏，取决于它属于谁——
Codex/OpenAI 配置写死版本是泄漏，Anthropic 画像的版本按 §1.1 不在范围内。这个归属问题
必须由语法结构回答：同一 `ValueSpec` 的多个名称与值、同一切片里的多个 `CallExpr`、同一
物理行上的多条记录，正则都区分不了，每补一条规则就在假绿与误报之间摆动一次。因此：

- 裸版本字面量的归属由 `backend/internal/service/official_egress_version_leak_ast_test.go`
  用 `go/ast` 判定（父节点栈定位 `CallExpr`／`ValueSpec`／`CompositeLit`，归属无法确认时
  按泄漏处理），基线在同目录 `testdata/` 下；
- 版本化符号名（`Codex0146`）与注释中的 codex 版本仍由 `check_version_leak.py` 的文本
  规则覆盖——那类模式是纯文本形态，不需要语法归属。

两侧都用指纹基线，两侧都随 `make check-egress-spec` 与 `go test ./...` 进入 CI。

**基线记录指纹而非计数。** 只比每个文件的命中数量时，同一文件删掉一处旧泄漏、同时新增
一处新泄漏即可通过；基线因此保存规范化行内容的哈希与出现次数，等量替换会被识别为新指纹。

**豁免必须难以触发。** 出站定型文件内的裸版本字面量默认算泄漏，只有确属其他供应商画像
（§1.1 已排除）才豁免。豁免判定基于剥离注释后的代码，并限定在同一条 `{}` 记录内：否则
一句 `// claude compatibility` 注释、或紧邻的 Anthropic 记录，都能把真实泄漏一并掩盖。

### 3.5.1 Fork 新增的画像与执行层

| 文件 | 责任 |
|---|---|
| `backend/internal/service/official_client_profile_registry.go` | 官方客户端画像注册与解析 |
| `backend/internal/service/official_egress_codex_0145_profile.go` | Codex 0.145.0 完整不可变画像 |
| `backend/internal/service/official_egress_codex_engine.go` | 画像编译、URL/header/body 和传输契约执行 |
| `backend/internal/service/official_egress_codex_release_projection.go` | 将正式 ReleaseCatalog 的不可变发布事实投影到过渡期 service DTO；不持有第二 active 事实源 |
| `backend/internal/service/official_egress_codex_integration.go` | 入口运行上下文与 Codex 画像接入 |
| `backend/internal/service/official_egress_codex_files.go` | 文件创建、blob 上传、完成轮询三阶段 |
| `backend/internal/service/official_egress_profile.go` | 通用官方出站上下文与画像接口 |
| `backend/internal/service/official_egress_integration.go` | HTTP/WS 官方画像的统一应用点 |
| `backend/internal/service/official_egress_openai_http.go` | OpenAI HTTP header/body 定型 |
| `backend/internal/service/official_egress_openai_ws.go` | OpenAI WS 握手、帧和 fallback 定型 |
| `backend/internal/service/official_egress_openai_json.go` | 稳定 JSON 顺序、闭集和原始数值保真 |
| `backend/internal/service/official_egress_openai_state.go` | turn-state 等跨请求状态隔离 |
| `backend/internal/service/official_egress_transport.go` | HTTP/WS 传输画像选择 |
| `backend/internal/service/official_egress_uuid.go` | 需要稳定关联时的 UUID v7 绑定 |
| `backend/internal/pkg/tlsfingerprint/h1_wire.go` | HTTP/1.1 最终 wire 名、顺序和 host 定型 |
| `backend/internal/pkg/tlsfingerprint/lowercase_headers.go` | header 大小写与保留名单适配 |
| `backend/internal/platform/liveattestation/attestation_candidate_capture.go` | 候选验收所需 attestation 观测 |
| `backend/internal/platform/liveattestation/attestation_unsupported_provider.go` | 不支持平台的显式边界 |
| `backend/internal/service/account_test_service_openai_files.go` | 管理接口的 Files 三阶段生产探针 |
| `backend/internal/service/testdata/official_egress/` | 模型能力 fixture 与脱敏实证说明 |
| `backend/internal/officialegress/` | 1A 生产 Catalog、RouteCatalog、Scope、Guard、Executor 与不可伪造定型凭证 |
| `backend/internal/officialegress/body_document.go` | 变更集 6 attempt-owned Body document：顶层单次解析、全局重复键 fail-close、dirty overlay 与最终单次有序输出 |
| `backend/internal/officialegress/profilecontract/profile.go` | 生产画像的版本化 header、body、transport 与端点契约模型 |
| `backend/internal/officialegress/profilecontract/snapshotdoc.go` | 画像快照文档模型的生产只读契约 |
| `backend/internal/officialegress/profilecontract/zerovalues.go` | 画像字段零值约束的生产校验 |
| `backend/internal/service/official_client_release_graph_export_contract.go` | 1A ReleaseProvider 读取现有发布图的生产桥 |
| `backend/internal/service/official_egress_profile_export_contract.go` | 1A RequestCompiler 读取现有画像的生产桥 |
| `backend/internal/service/official_egress_1a_transition.go` | 16 条业务 route 到 2 个发布 purpose 的过渡 Provider/Compiler |
| `backend/internal/service/official_egress_1b_executor.go` | 1B 已迁移 Responses/compact sink 的版本中立 Executor 与 HTTPUpstream terminal adapter |
| `backend/internal/service/official_egress_identity_authority.go` | 变更集 3 将账号投影、可信运行条件与 attempt-local 认证材料转换为版本中立结构化身份事实；不持有凭据或最终 Header 所有权 |
| `backend/internal/service/official_egress_http_invocation.go` | 变更集 3 独立 HTTP sink 的统一 invocation/attempt 执行边界 |
| `backend/internal/service/official_egress_websocket_invocation.go` | 变更集 3 独立 WebSocket sink 的统一 invocation/attempt 执行边界 |
| `backend/internal/service/official_egress_transport_adapters.go` | HTTP、req-profile、WebSocket 三类生产 adapter 接线与 WS Acquire 令牌准入 |
| `backend/internal/repository/official_egress_guard.go` | req-profile Guard 包装、OAuth transport 画像转换与受信物理资源接线 |
| `backend/internal/service/openai_forward_plan.go` | Forward invocation、attempt、预算与 WS→HTTP fallback transition 模型 |
| `backend/internal/service/openai_oauth_service.go` | OAuth refresh 的业务事实组装与统一 Executor 调用入口 |

对应的 `*_test.go` 覆盖画像摘要、端点全集、字段闭集、连接生命周期、OAuth、文件上传、
HTTP/WS、候选 trace 和配置失败关闭。新增版本时应新建版本画像及测试，不复制一套执行引擎。
其中 `official_egress_codex_version_upgrade_test.go` 是跨版本安全网，见 §3.2 末尾：单版本
测试无法暴露"某条路径没跟随 release 指针"，而这恰恰是升级时最容易发生、也最难在生产
中定位的偏差。

变更集 5 已删除 `official_egress_finalizer_pairing_test.go`。该门禁只证明旧 attach 与旧
finalizer 配对，不能证明端点没有绕过旧链；在 21 个 Runtime Sink 全部进入统一 Executor
后继续保留，反而会把无人调用的旧定义固化为兼容面。

替代约束分为四层：21 个 Runtime Sink 的精确 `enforced` 集合和 Executor AST 权威链；
旧 attach/finalizer/helper 的生产定义与调用同时为零；真实 Executor → WS Acquire 测试覆盖
Header 闭集、Host 清理、冻结身份和条件 Header；28 route、active/previous 共 56 份
final-wire 用统一严格比较器验证。源码绝迹门禁自带“包装函数调用旧符号”的 mutation 负例，
不能通过别名或增加一层 wrapper 复活。退休收据见
`docs/changeset5/pairing-gate-retirement.json`，变更集 3 的历史评估收据保持原文不变。

### 3.5.2 共享业务接入点

`上游已有`表示该文件存在于本轮记录的 upstream 基线；`Fork 已有`表示文件由本项目其他
能力先行引入，但本次伪装实现仍修改了它。两类都不是画像私有文件，合并时必须检查。

这里记录的是**初次接入的影响面和以后合并上游的风险面**，不是薄层核心的文件数量。
相对 §3.5 记录的上游基线，以下 36 个共享生产文件存在累计变更。它们是“与上游重叠的
冲突面”，与 52 个完整出站定型文件不是同一口径：前者证明重叠区域只减不增，后者防止
遗漏新发送路径，任何一套都不能替代另一套。

变更集 5 不同步上游，分类基线固定为
`26d894ef4f50645a4bf1030e378ac892f17d0223`；启动时观察到的远端 HEAD
`825ca7b1fc9335f904bc077f051de815fb61e47f` 只作事实记录。按 declaration 与文件结构
变更单元复算后，全量单元从 `260` 收缩为 `247`；raw governable 清单保持不可变的
`103 → 90`，分类 amendment overlay 将一个漏判的 Codex Images declaration 纳入后，
effective governable 集合为 `104 → 91`。共迁出 13 个官方出站专属单元；136 个被修改的
既有上游 declaration 集合保持完全相同，未新增修改、删除或重命名。完整证据见
`docs/changeset5/conflict-inventory/`、`post-refactor-conflict-inventory/`、
`conflict-classification-amendments.json` 与 `conflict-migration-receipt.json`。

变更集 5 的 final-wire 时间链分为 original pre、normalized pre 与 post。original pre 三件套
保持冻结 SHA-256 `959b3179… / 6446d2ce… / 3cb84244…`；normalization transition 只允许
active/previous 两条 OAuth capture 的 `/connection_pool_digest` 精确变化；normalized pre 与
post 对 56 份 capture 使用空允许列表严格比较。工作区证据另分为不可变 workspace baseline
与最终状态 workspace transition：558 个冻结路径必须逐项复算，未变化路径禁止登记，所有
实际变化必须唯一匹配前后文件类型、权限、存在状态和 SHA-256。

**统计必须以已明确冻结的分类基线为准。** 本地 `upstream/main` 仍停在 `12d811bd7`，
不得用它判断当前远端状态；远端观察值也不能在本变更集中临时替换分类基线。下次同步上游
必须先建立新的明确基线和前置台账，再重新分类，不能把两个基线的差异混进本次收缩结果。
薄的是画像驱动的最终定型核心；初次改造仍须把原有多个入口、独立端点和传输旁路一次性
接到核心。后续 Codex 版本升级若再次大面积修改这些文件，说明版本差异已经泄漏到业务层。

**稳定接入点。** 这些文件负责取得身份、传递上下文或在最终 HTTP／WS／TLS 边界调用统一
定型逻辑。完成初次接入后，版本升级原则上不应修改它们。

| 来源 | 文件或文件组 | 接入内容 | 合并上游时检查 |
|---|---|---|---|
| 上游已有 | `backend/internal/handler/openai_gateway_handler.go` | 在 body 标准化前保存官方客户端运行身份 | 调用顺序是否仍早于解压和协议转换 |
| Fork 已有 | `backend/internal/pkg/openaiidentity/codex.go` | Codex 版本和 surface 身份解析 | 新官方 UA 不能被旧正则静默接受 |
| 上游已有 | `backend/internal/pkg/tlsfingerprint/dialer.go` | 应用 TLS、h1 wire 与大小写适配 | transport 包装顺序和连接复用是否漂移 |
| 上游已有 | `backend/internal/repository/req_client_pool.go` | Client 池和调用内 retry 生命周期 | 不能恢复为跨上层调用的无条件复用 |
| 上游已有 | `backend/internal/service/openai_gateway_forward.go` | Compatible／Responses 到最终出站的公共路径 | 画像必须在协议适配后、发包前应用 |
| 上游已有 | `backend/internal/service/openai_gateway_chat_completions.go` | Chat Completions 派生到 Responses | 派生语义不能绕过 body 闭集 |
| 上游已有 | `backend/internal/service/openai_gateway_messages.go` | Anthropic 兼容入站派生到 Codex Responses | 入站协议转换不得改变同 invocation 的 Bundle 或绕过 Executor |
| 上游已有 | `backend/internal/service/openai_gateway_passthrough.go` | 直通路径冻结 Plan 并进入统一 HTTP invocation／Executor | 直通不得恢复旧 finalizer 或绕过 Executor |
| 上游已有 | `backend/internal/service/openai_gateway_count_tokens.go` | OAuth count_tokens 复用官方 HTTP TLS 画像 | 必须与业务请求同画像，否则同账号同 IP 暴露两种 ClientHello |
| Fork 已有 | `backend/internal/service/openai_apikey_mimic_profile.go` | `openAIUpstreamRequestPlan` 承载 body 契约与 turn-state | turn-state 只能由终态 Header Finalizer 写入 |
| Fork 已有 | `backend/internal/service/openai_upstream_http.go` | API Key／custom provider HTTP 最终发送；Codex 官方上下文误入时 fail-close | API Key mimic 画像保持同源，OAuth 不得绕过 Executor |
| 上游已有 | `backend/internal/service/openai_ws_forwarder_ingress.go` | WS 入站身份与首帧 | 官方／第三方入口不能混用未绑定上下文 |
| 上游已有 | `backend/internal/service/openai_ws_forwarder_support.go` | WS 能力、预热和 fallback | 画像默认值优先于模型名猜测 |
| 上游已有 | `backend/internal/service/openai_ws_forwarder_payload.go` | WS 握手 header 终态定型 | 握手头不得在 Finalizer 之后被补写 |
| 上游已有 | `backend/internal/service/openai_ws_forwarder_v2.go` | WS 最终连接与帧处理 | 握手、压缩和 fallback 仍按画像 |
| 上游已有 | `backend/internal/service/openai_ws_v2_passthrough_adapter.go` | WS 业务帧、预热帧与派生帧定型，握手 turn-state 消费 | 帧仍由画像构建函数生成；不得在 adapter 内自行拼帧 |
| 上游已有 | `backend/internal/service/openai_ws_client.go` | 解析 WS TLS 画像、构建传输、握手后补齐 header | RoundTripper 包装顺序漂移会直接改变 ClientHello 与握手线序 |
| 上游已有 | `backend/internal/service/openai_ws_pool.go` | 连接池身份 key 与画像上下文传递 | key 必须含画像身份，否则不同画像的连接会被错误复用 |
| 上游已有 | `backend/internal/service/openai_ws_http_bridge.go` | WS 到 HTTP 降级 | 同一调用的身份和状态必须连续 |

**一次性旁路收口。** 这些文件原本拥有独立请求构造器、Client、状态或多阶段编排，不能只靠
主 `/responses` 的 Finalizer 覆盖。初次改造需要接入画像；后续版本差异应由新画像表达，
只有端点机制本身变化且现有执行引擎无法表达时才允许再次修改。

| 来源 | 文件或文件组 | 接入内容 | 合并上游时检查 |
|---|---|---|---|
| 上游已有 | `backend/internal/handler/openai_codex_models_handler.go` | models 入口绑定 Codex surface 与版本 | 是否仍进入画像而非通用 HTTP |
| 上游已有 | `backend/internal/handler/openai_live.go` | realtime 入口保存运行上下文 | 第一跳和 sideband 是否保持同一身份 |
| 上游已有 | `backend/internal/repository/gateway_cache.go` | realtime 跨阶段保存画像运行身份 | cache key 和序列化必须保留影响出站的版本身份 |
| 上游已有 | `backend/internal/repository/openai_oauth_service.go` | OAuth refresh 接入版本化端点和传输画像 | refresh 不得丢失 surface／版本 |
| 上游已有 | `backend/internal/service/openai_images_responses.go` | 模型工具和独立 images 端点分流 | hosted tool 与 namespace/imagegen 不得混淆 |
| 上游已有 | `backend/internal/service/openai_alpha_search.go` | alpha-search 两阶段端点 | header/body 和会话状态必须走画像 |
| 上游已有 | `backend/internal/service/openai_compact_probe.go` | compact 选择与 reason | 不得重复压缩或丢失触发原因 |
| 上游已有 | `backend/internal/service/openai_codex_models_service.go` | models 能力和 Lite 来源 | 版本化 capability fixture 不得被通用值覆盖 |
| 上游已有 | `backend/internal/service/openai_quota_service.go` | WHAM usage、reset-credits 查询与 consume | 版本画像、backend-client 生命周期与 header/body/TLS 仍由统一执行层控制 |
| 上游已有 | `backend/internal/service/openai_live.go` | realtime 服务编排 | 第一跳和第二跳身份、URL、header 连续 |
| 上游已有 | `backend/internal/service/account_test_service.go` | 管理接口的 OAuth images／Files 生产探针 | 探针必须走画像端点，不得旁路通用请求器 |
| 上游已有 | `backend/internal/service/account_usage_service.go` | 普通 OAuth 的 WHAM-first 刷新与同调用 Responses fallback | WHAM 条件、fallback reason/频率上限和 Executor terminal 必须同时保留 |
| 上游已有 | `backend/internal/platform/liveattestation/attestation.go` | attestation 条件值 | 缺失时保持条件缺失，不能伪造常量 |

**基础设施接线。** 这些文件只负责让入口和新组件可达，不承载版本规则。

| 来源 | 文件或文件组 | 接入内容 | 合并上游时检查 |
|---|---|---|---|
| 上游已有 | `backend/internal/server/routes/gateway.go` | HTTP／WS 路由接入 | 新入口必须进入相同画像选择点 |
| 上游已有 | `backend/cmd/server/wire_gen.go`、`backend/internal/service/wire.go` | 新组件依赖注入 | 上游重新生成 wire 后必须复核注入仍存在 |
| 上游已有 | `backend/internal/repository/wire.go` | 受信传输资源与 Guard 的 repository 接线 | 只注入窄资源，不得把画像规则下沉到 repository |
| 上游已有 | `Dockerfile` | 候选镜像所需运行工具／身份 | 构建结果必须重新绑定 image digest |

上述三组只记录影响生产出站的共享接入面。纯测试和抓包工具仍由对应门禁覆盖；上游新增
OpenAI 路径时，应先判断能否进入现有稳定接入点，再决定是否新增旁路收口，不能直接把
版本常量或模型特判写入共享业务文件。

### 3.5.3 抓包与验收工具

| 路径 | 责任 |
|---|---|
| `tools/official_client_capture/codex_upgrade.py` | 唯一升级编排入口 |
| `tools/official_client_capture/codex_upgrade_environment_probe.py` | 数据、容器、账号和配置前后探针 |
| `tools/official_client_capture/codex_upgrade_receipt_finalizer.py` | 恢复、运行画像和 Kilo 机器收据 |
| `tools/official_client_capture/codex_upgrade_*schema.json` | Campaign、attempt、阶段、规则和客户端收据契约 |
| `tools/official_client_capture/codex_upgrade_rules_0_145_0.json` | 0.145.0 规则编号基线 |
| `tools/official_client_capture/codex_upgrade_scenarios_0_145_0.json` | 0.145.0 官方／候选场景基线 |
| `tools/official_client_capture/candidate_rule_assertion.py` | 官方与候选两侧逐规则断言 |
| `tools/official_client_capture/capture.py`、`capturelib/` | 编排器调用的底层采集与安全生命周期 |
| `tools/official_client_capture/pcap_clienthello.py`、`relay_extract.py` | TLS 与应用字节离线解析 |
| `tools/official_client_capture/check_sample_integrity.py` | 单向、空连接和中继样本完整性检查 |
| `tools/official_client_capture/scrub_raw_bytes.py` | 原始字节等长脱敏、秘密检查和脱敏结果验证 |
| `tools/check_spec_refs.py`、`tools/evidence_index.py`、`tools/spec_status.py` | 规则、源码引用和证据索引门禁 |
| `tools/check_ledger_completeness.py` | §3.5 台账完整性复算 |
| `tools/check_version_leak.py`、`tools/version_leak_baseline.json` | 生产代码版本标识符泄漏门禁与基线 |

底层脚本只有被版本场景清单引用时才属于正式工具链。历史一次性脚本不应重新成为平行入口。

## 3.6 当前验收边界

当前 0.145.0 伪装实现以第二部分 42 项为目标，官方客户端和第三方入口都必须绑定同一
源码树、镜像、profile ID／digest 和规则清单。验收结论只对已封存 Campaign 身份成立，
不外推到其他版本、平台、镜像或工作树。

Sub2API 是多账号网关，并发度、跨账号调度和整体请求节奏不会变成单用户 CLI；这些服务级
统计特征不属于 42 项。工具只能证明声明范围内的请求和恢复事实，不能替代生产备份、SSH
主机证明或任意未知秘密扫描。

## 3.7 长期架构原则与 persona 边界

本节给出已经落地、后续升级仍必须保持的架构边界。目标不是把所有官方域名请求都伪装成
Codex CLI，而是确保一个请求的端点行为、Header、Body、TLS／HTTP／WebSocket 画像、触发
时机和连接生命周期来自同一个 `persona`。

必须同时满足以下原则：

1. `codex-cli` 请求只能由 `CodexEgressExecutor` 这个逻辑出口发送；Executor 可以编排多个
   transport backend，但业务调用方不能绕过它直接发送。
2. route 不能只按 host 或调用方声明识别。先按物理 `method + host + path + protocol`
   匹配，再用 `SinkCatalog` 的权威 purpose、persona 和 backend 检查 SinkBinding。
3. Release 在 invocation、账号尝试或独立系统调用开始时只解析一次；retry、fallback、WS
   连接和辅助端点沿用同一个不可变 Bundle，禁止下游重新查询 active。
4. 调用方只能提交未定型 Plan；最终请求、TransportSpec 和 FinalizationToken 由 Executor
   生成并直接交给受信 adapter，定型后不得回交业务层继续修改。
5. Runtime Guard 与 source-to-sink 静态门禁必须同时存在：前者检查真实物理发送，后者阻止
   新增裸 sink、未登记 facade 和受审调用链外的发送代码。
6. 灰度、回滚和紧急覆盖按单个 Sink 生效；不得用一个全局开关静默放开全部官方出站。

persona 与出口的当前边界如下：

| persona／状态 | 端点范围 | 逻辑出口 | 约束 |
|---|---|---|---|
| `codex-cli` | ReleaseBundle 登记的 Codex 端点闭集 | `CodexEgressExecutor` | URL、Header、Body、传输、状态与生命周期由同一 Bundle 驱动 |
| `chatgpt-web/chrome` | privacy settings、accounts check、subscriptions | 浏览器身份 Plan／client | 保持独立 Chrome TLS／HTTP2／XHR 语义，禁止套用 Codex 画像 |
| `transport_only` | authorization-code OAuth exchange | 独立受审 transport | 只复用有证据的传输事实，不冒充 refresh 的 Body／行为契约 |
| `unclassified` | PAT whoami、Agent Identity task register | 精确登记的遗留路径 | 完成官方行为举证前不得凭官方 host 自动归入 Codex persona |
| 未登记 | Catalog 未知 route 或 SinkBinding | 无长期出口 | enforce 状态下 fail-close |

## 3.8 包边界、依赖方向与上游合并缝

fork 自有的画像、Catalog、Compiler、Executor 和 Guard 位于
`backend/internal/officialegress`。上游高频变化的 `service` 文件只负责采集业务事实、构造
Plan 和调用 Executor；`repository` 只提供连接池、代理和底层资源，不持有身份规则。

```text
service ───────────────────→ officialegress core
repository ────────────────→ officialegress core 的窄 port
officialegress/adapter/* ───→ core + 受信物理资源
wiring ────────────────────→ 注入闭集 adapter
```

`officialegress` 不得 import `service` 或 `repository`。Executor 的公共边界使用中立 Plan、
Release、Compiler port 和 transport port，不能暴露可继续修改的 `*http.Request`、
`*req.Client` 或具体仓储类型。真正操作 socket 的 adapter 属受信边界，其可信性来自闭集
登记、AdapterID／FinalizationToken、wire 契约测试和静态门禁，而不是形式上的只读 getter。

此依赖缝是降低上游合并冲突的核心：Codex CLI 换版原则上只新增内容寻址画像与发布图节点；
Sub2API 上游合并原则上只复核稳定接入钩子。只有端点机制本身无法由现有 Plan／Executor 表达
时，才允许修改共享业务文件，并必须记录原因和专项复验。

## 3.9 ReleaseBundle、Plan 与最终发送所有权

ReleaseCatalog 在启动时确定性加载并校验全部版本，active／previous 指向完整 release ID。
画像以 `version + digest` 为不可变坐标；相同版本字符串但 digest 不同，仍是两个不同发布。
ReleaseBundle 至少冻结以下事实：

- release mode、version、release／profile ID 和全部摘要；
- surface、User-Agent、originator 与身份变体；
- HTTP／WS 端点、Header、Body 字段序和 feature；
- TLS、压缩、连接生命周期和端点对应的 TransportSpec；
- ExecutionPolicy、DeploymentSupportPolicy、BehaviorPolicy 与 fallback 图。

CodexEgressPlan 保存业务事实、IdentityMode、深拷贝后的 Header Override、HeaderPolicy、
BodyPolicy、BehaviorPolicy 和 attempt-owned Body。`TransportSpec` 不属于 Plan：它只能由端点
画像决定，否则调用方就能自行选择 backend 并绕过画像。

Header 所有权固定为：

| Header 类别 | 所有者与优先级 |
|---|---|
| Transport、Auth、Host、长度和 Body framing | 系统最终所有，账号及入站不能覆盖 |
| Release identity | strict／mimic／proxy 模式下由画像最终决定 |
| Account extensions | 普通 API Key 按产品规则覆盖；mimic／proxy 仅允许非保护字段 |
| Endpoint closed set | OAuth 官方端点删除画像闭集之外的字段 |
| Ingress headers | 最低优先级，只进入明确允许的字段 |

编译器只做语义定型；Executor 原子签发 FinalizationToken、注入 TransportSpec 并选择
HTTPUpstream、req-profile 或 WebSocket adapter。adapter 只能执行 Token 中
`WireNormalizationPlan` 明确允许的等价变换，例如 HTTP 小写化、默认 User-Agent 抑制以及
WS 库内部 `wss → https` 的安全协议规范化；其他 URL、Header 或 Body 修改必须拒发。

replayable Body 绑定精确内容摘要，可从同一语义输入为 retry 创建新 attempt。single-use
stream 不得在 Prepare／Guard 中预读或复制，只绑定不可伪造 capability 与 ContentLength，
并强制 `MaxAttempts=1`、`Replayable=false` 和单次消费。

## 3.10 Guard、逐 Sink 灰度与静态门禁

Runtime Guard 的判定顺序固定如下：

1. 完全忽略未验证的 purpose／persona，按真实 method、host、path、protocol 匹配物理 route；
2. 未匹配 route 时应用 UnknownRoutePolicy；
3. 已匹配 route 但 SinkID 缺失、未登记或 binding 不符时应用 UnregisteredSinkPolicy；
4. 使用 Catalog 权威 binding 二次解析，再按 SinkEnforcementState 决策；
5. canary／enforced 路径必须验证 FinalizationToken、Release／Profile／Pool digest 和 adapter；
6. 检查最终请求摘要，发现 finalize 后篡改立即 fail-close。

逐 Sink 状态机为：

```text
legacy_observe → canary_enforce → enforced
                       ↑              │
                       └──── 回滚 ────┘
```

`legacy_observe` 只允许封存基线中的历史 sink，且必须有责任人和到期条件；bootstrap 后新增
sink 不得进入该状态。`canary_enforce` 只对指定实例、账号或 route 拒绝违规。`enforced` 是
正式状态；紧急 `observe_until` 必须按 Sink、有截止时间、持续记录违规且不能扩大 legacy
基线。未登记 route 与无效 SinkBinding 即使处在观察窗口，也必须使用不同 reason code。

静态门禁使用 `go/packages + go/types + AST` 识别 net/http、HTTPUpstream、req/v3、WS、
项目 facade 和 client factory 的完整限定签名，并把请求构造事实与实际发送参数绑定。它必须
覆盖函数值、接口转发、多层包装和生产构建矩阵，且用变异测试证明能发现裸 client、
`DoWithTLS` 半旁路、req/v3 直发和 WS 直拨，同时不误报数据库或 singleflight 的同名 `Do`。

legacy 基线一经 seal 只能凭 MigrationReceipt／RemovalReceipt 单调减少。历史 receipt、ceiling、
supplement 与受保护 base commit 的摘要不可协调改写；facade 和 dead code 只留在证据目录，
不能继续进入运行时 allowlist。

## 3.11 行为策略与稳定策略来源

非用户直接触发的请求不仅要“长得像 Codex”，还必须有官方等价行为、触发条件、频率、
抖动、并发上限、副作用和删除期限。普通 OAuth 用量刷新采用 WHAM-first：优先请求
`GET /backend-api/wham/usage`，只接受结构完整且窗口时长属于已知闭集的 RateLimit；仅在同一
刷新调用内、兼容性条件满足时进入画像化 Responses fallback，凭据失效和安全错误不得被
fallback 掩盖。

以下 ASCII anchor 是生产策略 `PolicySource` 的稳定引用。标题可以演进，anchor 不得复用：

<a id="policy-changeset-1b"></a>

- `policy-changeset-1b`：WHAM-first、管理端 Responses／compact、alpha-search 及已知画像修复。

<a id="policy-changeset-2"></a>

- `policy-changeset-2`：不可变 ReleaseBundle、单次解析、transport adapter 与正式回滚。

<a id="policy-changeset-3"></a>

- `policy-changeset-3`：21 个 Codex Runtime Sink 的统一 Executor、Forward 与辅助端点收敛。

## 3.12 当前实施状态与兼容代码边界

变更集 0 至 6 已全部完成。当前正式运行状态为：

- 21 个 `codex_profile` Runtime Sink、28 条 route 全部为 `enforced`，0 个 Codex
  `legacy_observe`；HTTP、WS、fallback、models、images、files、alpha-search、WHAM 和 OAuth
  refresh 均进入统一 Executor；
- 画像已进入内容寻址运行时目录，ReleaseCatalog 启动期预编译 active／previous；
- attempt Body 已实现单次顶层解析、duplicate fail-close、不可变 backing 和最终单次有序输出；
- 52 个完整出站定型文件与 36 个上游冲突文件使用两套独立口径门禁；冲突变更单元已从
  effective `104` 收缩到 `91`，共享上游已有 declaration 集合没有扩大；
- active／previous 共 56 份 final-wire capture 使用空允许列表严格比较；
- 2026-08-04 已在 DMIT 使用 OAuth 与 API Key 连续真实请求通过首次实机验收。

兼容机制按“是否仍承担运行责任”分类：

| 分类 | 当前决定 | 原因 |
|---|---|---|
| active／previous、per-sink canary／override、FinalizationToken | 长期保留 | 这是平滑升级、回滚和防旁路能力，不是过渡垃圾 |
| browser persona、OAuth exchange transport-only、unclassified sink | 保留并独立治理 | 它们不是 Codex Executor 的旧实现，错误删除会混淆 persona 或破坏低频流程 |
| service 画像 DTO／projection | 暂时保留 | API Key mimic、旧业务读取面和迁移证据仍有真实消费者；迁移消费者后才能删除 |
| 旧 attach／finalizer／pairing gate | 已删除 | 21 个 Runtime Sink 已由 Executor、AST 门禁与 final-wire 证据替代 |
| unsigned `LegacyCompiledDispatcher` HTTP 执行路径 | 退休 | 仅接受 `legacy_observe`，与当前全部 enforced 的 Codex Runtime Catalog 不再相容；旧上下文误入通用发送入口时直接 fail-close |

判断兼容代码能否删除必须同时满足：生产定义和调用为零，替代路径有负例门禁，wire 对比无
变化，active／previous 均通过，并有可追溯退休收据。只因名称包含 `transition`、`legacy` 或
版本号不能作为删除理由。

---

# 第四部分 Codex CLI 升级与上游演进

## 4.1 何时必须启动升级

出现以下任一变化都要新建版本 Campaign：

- Codex CLI 版本或官方二进制 SHA-256 变化；
- Cargo.lock、网络依赖、TLS 后端或目标平台变化；
- 官方默认 feature、端点、header、body、连接或状态路径变化；
- Sub2API 源码树、候选镜像、构建 ID 或版本画像变化；
- 合并上游后统一应用点、Client 生命周期或协议适配发生变化。

旧版规则、实现逻辑和场景可以作为发现线索，旧版运行证据不能替代目标版本证据。

## 4.2 版本化五份清单

目标版本的权威输入按 `<用途>_<版本>.json` 平铺在 `tools/official_client_capture/`，
不使用 `versions/<版本>/` 子目录。至少包含：

| 文件 | 内容 | 0.145.0 现状 |
|---|---|---|
| `codex_upgrade_rules_<版本>.json` | 目标版本规则全集、范围、必做性和断言器 | 已封存 |
| `codex_upgrade_rule_migration` | 旧规则的 inherit/change/delete 和新规则的 add 分类 | 仅有 schema：0.145.0 是规则基线，没有旧版本可迁移 |
| `codex_upgrade_scenarios_<版本>.json` | 官方／候选场景、变量、前置条件、清理动作和规则覆盖 | 已封存 |
| `codex_egress_profile`（画像清单） | Go 薄层可直接实现的完整版本画像，不是旧版增量 | **仅有 schema，无实例** |
| `candidate_rule_expectations_<版本>.json` | 独立于候选实现的正例、负例和逐规则期望 | 已封存 |

五份清单分别计算 SHA-256，并以联合摘要人工批准。规则数量由迁移结果产生；42 只属于
0.145.0，后续版本不得写死为 42。

**正式画像事实源已经进入不可变快照目录。** `internal/officialegress/profilecontract` 负责
严格解析 SnapshotDoc、构造 ProfileSpec 并编译 ExecutableProfile；SnapshotCatalog 以
`version + digest` 固定快照坐标，ReleaseCatalog 再把 active/previous 发布节点绑定到对应
坐标。旧 service 画像仍作为迁移证据和过渡投影存在，但不再是 Runtime Executor 可自行
替换的第二发布事实源。

## 4.3 单向状态机与统一命令

```text
planned
→ official_sealed
→ profile_approved
→ candidate_sealed
→ compared
→ ready
```

唯一入口：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

| 阶段 | 核心动作 | 完成条件 |
|---|---|---|
| `plan` | 冻结版本、源码、二进制、规则、场景和环境身份 | 新建不可变 Campaign |
| `capture-official run/seal` | 对目标官方 CLI 执行场景、恢复和证据封存 | 官方目标出站面完整 |
| `classify` | 比较源码、依赖和动态出站面，完成五份清单 | 无未分类项，联合摘要获批 |
| `capture-candidate run/seal` | 对固定源码树、镜像和画像执行候选及第三方入口 | 候选身份、恢复和客户端收据封存 |
| `compare` | 断网重哈希并比较官方／候选证据 | 生成双侧逐规则断言模板 |
| `accept` | 重放机器断言、收据、安全和 evidence seal | 全部门禁通过，进入 `ready` |
| `status` | 从不可变收据推导状态 | 不修改 Campaign |
| `resume` | 只续跑允许自动重试的失败阶段 | 不代替人工分类或摘要批准 |

真实请求和封存分开：`run` 只采集并保存 attempt，`seal` 只允许绑定该 attempt 已声明的证据。
候选第一次 seal 建立 Kilo 后环境检查点，第二次生成预览，第三次按相同 review digest 封存。

## 4.4 升级步骤

1. **冻结基线**：记录旧版 profile digest、规则清单、官方证据和已验收镜像。
2. **取得目标版本**：锁定官方源码、Cargo.lock、二进制 SHA-256、运行镜像和平台。
3. **官方发现**：扫描新增／删除调用点，执行 full 官方场景并封存证据。
4. **规则迁移**：逐项分类 inherit、change、add、delete、condition_change 或 blocked。
5. **实现新画像**：追加以 `version + digest` 为坐标的 SnapshotCatalog 快照，补充 release
   graph 节点并调整 active/previous 指针（见 §3.2）；稳定执行引擎只在新形态无法表达时
   修改。快照内容全部来自第 3–4 步的证据，不得手工推断。
6. **候选验收**：最终镜像覆盖官方 Codex CLI、Compatible、Responses HTTP/WS 和 Kilo。
7. **双侧断言**：每条目标规则分别运行官方和候选机器断言，随后 compare 和 accept。
8. **合并上游复验**：以合并后的最终源码树和镜像新建 candidate attempt，不能复用合并前结论。
9. **灰度与回滚准备**：只发布不可变画像和镜像，保留上一已验收版本。

目标版本出现新出站面不是失败；未发现、未分类或用旧画像静默兜底才是失败。

## 4.5 必须保持的可信边界

以下约束不能因为简化文档而删除：

1. Campaign 核心清单、attempt、preview 和阶段 result 均只写一次。
2. 每次 run 在发请求前原子预约并生成随机 `run_nonce`；只留下 reservation 的中断和孤儿
   预约均按失败关闭，不允许自动重跑。
3. 官方身份绑定版本、二进制、源码树、依赖和实际运行镜像。
4. 候选身份绑定源码树、构建 ID、部署版本、OCI digest、image ID 和 profile digest。
5. evidence root、日志、pcap、relay、收据和恢复报告共同进入逐文件 inventory。
6. compare、accept 和后续 status 都重新计算摘要并重放机器 finalizer；机器收据必须由
   `codex_upgrade_receipt_finalizer.py` 从原始事实生成，人工只能批准机器给出的
   `review_sha256`，不能手写通过结论。
7. 环境恢复失败会污染整个 Campaign，不允许自动 resume 或继续 seal。
8. 完整失败只能显式执行 `resume --rerun-failed`；失败重跑创建新 attempt 并重新执行全部
   任务，不跨 attempt 复用日志或证据。
9. 真实请求只有在明确提供 `--acknowledge-live-requests` 后才能执行。
10. 抓包和验收不得执行 `compose down`、`pull` 或 `prune`，不得替换、重建数据容器。
11. 凭据、Cookie、完整代理 URL 和未脱敏原始字节不得进入 Git 或阶段身份文件。
12. `ready` 只表示证据门禁通过，不表示已部署、已备份或已完成生产回滚演练。
13. 新 Codex CLI 版本只新增版本快照、版本清单和测试，不修改 §3.5.2 的共享接入点。
    §3.2 的版本快照注册表与 release 指针使该约束可以实际达成，不再只是原则；
    `tools/check_version_leak.py` 的基线机制负责看住它——共享接入点的版本标识符命中数
    只许降不许升，基线外的文件必须为零。若现有画像和稳定执行引擎确实无法表达新机制、
    必须修改共享文件，则要记录受影响规则、无法表达的原因、共享文件最小差异和专项复验
    结果，并显式更新泄漏基线使该例外可见可审计，不能以版本升级为由重新散落条件分支。

## 4.6 官方与第三方入口矩阵

| 入口 | 必须证明 |
|---|---|
| 官方 Codex CLI | 无论声明何种版本、平台或 surface，均收敛到 active 画像，不落入通用兜底 |
| `/v1/chat/completions` | Compatible 适配后进入目标 HTTP Responses 画像 |
| `/v1/responses` HTTP | Responses 入口使用同一 HTTP 画像 |
| `/v1/responses` WebSocket | WS、预热和 fallback 使用同一版本画像 |
| Kilo Compatible | `kilo-compatible` 收据绑定模型、账号、请求、响应、usage 和 profile |
| Kilo Responses | `kilo-responses` 收据绑定相同候选身份和 profile |

Kilo 的 installation 可以早于 attempt；ingress、runtime、response 和 usage 必须绑定相同
Campaign、attempt、`run_nonce`，并位于 attempt 开始与 client checkpoint 之间。检查点后
不得继续发送 Kilo 模型请求。第三方入口证明“不同入站协议收敛到同一出站画像”，不要求每个
客户端独立触发规则全集。

## 4.7 合并 Sub2API 上游

每次合并按以下顺序处理：

1. 更新并记录明确的上游 commit，不用含糊的“最新 main”作为基线；
2. 先合并上游业务代码，不同时改版本画像；
3. 按 §3.5.2 的上游接入点逐文件复核调用顺序、上下文和旁路；
4. 重新生成 wire 依赖注入和其他生成代码；
5. 运行 Go 全量测试、抓包工具测试和文档门禁，其中必须包含
   `tools/check_ledger_completeness.py` 与 `tools/check_version_leak.py`；
6. 用合并后源码构建新镜像，并记录 source tree digest、build ID、OCI digest 和 image ID；
7. 新建候选 attempt，重跑官方、候选、Kilo、compare 和 accept；
8. 若上游新增出站路径，先归入规则发现和版本清单，不能在共享文件内追加临时特判；
9. 更新 §3.5 的文件台账，删除已不再存在的接入点并登记新增接入点。

画像与 adapter 文件尽量保持 fork 独有；上游文件只保留取得 Snapshot、构造 Plan 和进入
统一 Executor 的最小调用。这样 Codex 升级成本集中在版本画像，上游合并冲突集中在有限
接入点。

## 4.8 启用与回滚

新画像只有在 Campaign `ready`、最终镜像身份一致、合并上游后 full 门禁通过时才能灰度。

灰度维度是 registry 的 release 指针与 profile mode，**不是入站客户端声明的版本**：按
§3.1，出站画像与客户端身份无关，同一时刻所有 OAuth 出站使用同一 active 画像。把新画像
指向 `Active`、上一已验收画像保留在 `Previous`，即可通过 profile mode 在两者间切换。

出现逐规则回归、运行 profile digest 不一致、入口落入通用兜底、服务健康失败、账号映射
异常、恢复失败、秘密扫描失败或证据摘要不一致时，立即停止扩大灰度并切回上一已验收画像
和镜像。保留 `Previous` 指针的价值正在于此：回滚是切换指针而非改代码重建镜像。回滚不能
删除 Campaign、覆盖画像或重建数据容器；回滚后复核服务、数据挂载、账号、keeper、
代理／CA 残留和旧版入口。

## 4.9 低侵入上游同步工作法

上游 Sub2API 更新与 Codex CLI 换版必须拆成两个独立变更集，不能在一次 diff 中同时解释
业务变化和画像变化。推荐顺序是：

1. 记录明确的 upstream commit，冻结当前 52 文件完整发送面和 36 文件冲突面；
2. 只合并上游业务代码，解决稳定接入钩子的冲突，不修改版本画像；
3. 重生 wire 等生成代码，复算 source-to-sink 台账并处理新增 route；
4. 运行全量测试、静态门禁和当前画像 final-wire 对比；
5. 从合并后的源码构建新候选镜像并完成实机验收；
6. 如需升级 Codex CLI，再单独新建版本 Campaign，只追加快照、发布图、场景与规则迁移。

共享文件只保留 Snapshot、Plan 和 Executor 调用。发现上游新增官方出站时，先登记 route、
persona、SinkID 和 backend，再部署调用代码；不得先临时放行裸 client。删除路径时则先删除
调用，再凭 RemovalReceipt 删除 Catalog 项。

## 4.10 兼容代码退休流程

每次清理按以下顺序执行：

1. 用类型扫描和调用图证明候选兼容层的全部生产消费者；
2. 将真实消费者迁到当前接口，并为旧入口添加明确 fail-close；
3. active／previous、HTTP／WS／fallback 和辅助端点负例全部通过；
4. 删除旧类型、字段、构造接线、scanner 分类及只验证旧实现的测试；
5. 收紧源码绝迹门禁，禁止通过别名或 wrapper 恢复；
6. 使用空 wire 允许列表比较变更前后；
7. 写入机器退休收据，再进行实机部署。

不得删除仍承担平滑升级、回滚、非 Codex persona 或 API Key 产品语义的兼容层。若一个旧
入口在当前 Catalog 下只能失败，则应替换成清晰错误并删除不可达执行能力，避免未来错误地
重新激活 unsigned 路径。

## 4.11 DMIT 部署与真实验收

DMIT 验收只替换 Sub2API 主应用，禁止执行 `compose down`、`pull`、`prune`，禁止重建或修改
PostgreSQL、Redis、keeper、环境文件和数据卷。标准流程为：

1. 在独立临时目录同步精确源码树，记录 commit／tree digest；
2. 使用唯一镜像标签构建，并记录 OCI revision、image ID 和镜像摘要；
3. 保存当前应用镜像与 compose 配置作为回滚点；
4. 只替换主应用容器，确认 health、日志和 `/v1/models`；
5. 使用服务器既有凭据分别执行 OAuth 与 API Key 真实请求，覆盖 Responses 流式完成事件；
6. 在条件允许时覆盖 WS、HTTP fallback、管理端探针和辅助端点；
7. 检查不存在 `request_modified_after_finalize`、缺 Token、legacy passthrough 或其他 Guard
   violation；
8. 验收失败立即恢复旧应用镜像；成功后清理临时构建目录和无用缓存，但保留回滚镜像与报告。

凭据、Cookie、完整代理 URL 和请求 Body 不得进入日志或报告。HTTP 200 不是唯一成功标准；
必须看到目标业务完成事件，并确认数据库、Redis、keeper 和应用健康状态没有变化。

## 4.12 文档、工具与证据保留

本文件是唯一叙事文档；以下内容仍作为机器资产保留：

| 资产 | 保留原因 |
|---|---|
| `docs/EVIDENCE_INDEX.md` | 从本文件和证据映射生成的审核索引 |
| `docs/maintenance/official-egress-consolidation-retirement.json` | 单文档合并、旧 Dispatcher 退休、facade／源码摘要过渡和删除事实 |
| `docs/maintenance/bootstrap-inventory-lock.json` | 旧 facade 退休后当前 scanner 算法的复审锁；历史变更集 5 锁保持不变 |
| `docs/maintenance/removal-receipts.json` | 将四条历史 `legacy_delegated` facade 收据单调提升为已退休状态 |
| `docs/maintenance/post-conflict-inventory/` | 记录本次维护后的冲突面清单、生成收据，以及相对变更集 6 冻结清单的精确过渡 |
| `docs/maintenance/workspace-transition/` | 从执行前基准 commit 独立复算本次完整变更集，防止新增、修改或删除文件漏出审核范围 |
| `tools/spec_source_deps/` | 第二部分 L2 引用的锁定依赖 |
| `tools/official_client_capture/` 正式入口、场景依赖和 Schema | 重新抓包和验收所必需 |
| `local-analysis/sources/codex-cli-0.145/` | 第二部分 L1 引用的官方生产源码 |
| `local-analysis/captures/raw-scrubbed/` 中索引引用的 36 个目录 | 第二部分 R 类规则可重新解析的基础 |
| `official-egress-final-review-fix-20260727-094500`、`profile-fidelity-fix-20260727`、`wire-parity-fix-20260727` | 第二部分 P/J/M 证据及其完整 manifest |
| `kilo-r11-20260731T220626Z` | 当前官方／第三方入口收敛验收的 Kilo 封存证据 |
| 已接受 Campaign 的 result、assertions 和 evidence seal | 证明某个候选身份曾达到 ready |

历史过程文档、一次性脚本和未被规则／Campaign 引用的抓包不属于长期权威输入。清理前必须
先生成保留清单，证明第二部分、`EVIDENCE_INDEX.md`、版本场景和已接受 Campaign 均不再
引用；随后先移入隔离目录，复验全部门禁后再删除。不得根据文件名、日期或“看起来过时”
直接删除证据。

2026-07-31 已按上述规则完成第一次清理：删除 22 个无规则引用的 R 类重复／失败尝试目录、
3 个已被当前证据替代的历史 Campaign、11 个根目录一次性脚本、历史过程文档和生成缓存；
共清理 3533 个抓包文件，约 738 MiB。删除前后均通过证据索引、样本计数、缺口、规格引用、
抓包工具测试和 Kilo 摘要校验；上述保留资产未改写。

2026-08-04 起，原“Sub2API 官方出站身份伪装优化方案”的长期架构、实施状态、升级、回滚、
兼容代码和部署规则已并入本文件；原文件删除。本文件同时承担 0.145.0 版本规格和长期架构
手册职责。历史 changeset manifest 中保留的旧路径及 SHA-256 是不可变审计事实，不表示旧文档
仍是有效权威来源，也不得为了消除字符串引用而改写历史证据。
