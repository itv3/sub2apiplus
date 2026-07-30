# Codex CLI 0.145.0 出站形态规格表

版本绑定：`codex-cli 0.145.0`　依赖锁定：`hyper 1.8.1` / `hyper-util 0.1.20` / `http 1.4.0` / `tungstenite 0.27.0` / `h2 0.4.13` / `reqwest 0.12.28`
末次更新 2026-07-30

本文分三部分：

- **一、背景与任务** —— 适用范围、证据口径与复核入口
- **二、规则** —— 内置 OpenAI OAuth 39 条、自定义 CA 条件分支 8 条、
  自定义 provider 条件分支 1 条，另有 3 条机制说明、2 条采集记录
- **三、方案** —— 方案甲的实现、差异与边界，以及方案乙的评估结论

> **审核入口**：先看第二部分导语和规则正文，再看
> [`EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) 的逐项证据结论；需要重新解析样本或运行门禁时，
> 执行 §1.2.1。

---

# 第一部分 背景与任务

## 1.1 这份文档是什么

它描述**官方 Codex CLI 用 OAuth 凭据向 OpenAI 请求时，在线上产生的可观测形态**，
以及 Sub2API 对每一项的对齐状态。**官方形态以本表为唯一事实源。**

本表不记录修复过程。实现差异与验收见第三部分；代码变更明细见
[官方出站 wire 一致性修复清单](OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md)。

## 1.2 证据在哪（审核者先读这段）

源码、依赖源码和抓包证据分别位于：

| 证据 | 位置 | 约束 |
|---|---|---|
| Codex CLI 源码 | `local-analysis/sources/codex-cli-0.145/codex-rs/` | 必须是 `0.145.0` 源码树 |
| Sub2API 源码 | `backend/` | 引用必须带完整相对路径 |
| L2 依赖源码 | `tools/spec_source_deps/` | 版本、提交及文件哈希由 `manifest.json` 锁定 |
| 抓包证据 | `local-analysis/captures/` | 运行号与证明范围以 `EVIDENCE_INDEX.md` 为准 |

第二部分源码引用均使用相对路径和精确行号。裸文件名歧义、行号漂移、引用测试代码、
L2 快照变化以及源码树版本不符都会使门禁失败。13 条纯 L2 规则
（`SPEC-H1-001~004`、`SPEC-H2-001~007`、`SPEC-WS-001/002`）及
`SPEC-WS-003` 的 L2 部分可通过本地快照离线复核；`tungstenite 0.27.0` 使用
Cargo.lock 指定的 OpenAI fork 提交
`4fffad30fe373adbdcffab9545e9e9bf4f2fc19f`。

逐项结论见 [`EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) §1。源码证据为充分 41、部分 7、
无／不适用 5；抓包证据为充分 50、有限 2、不适用 1。其中 52/53 项具有可重新解析的
P/R 原始证据；`SPEC-HDR-001` 描述内部调用顺序，wire 在结构上不适用。源码证据强度、
抓包证据强度和规则总体状态是三个独立维度。

### 1.2.1 既有证据的最小复现命令

以下命令只复算已经归档的证据，不启动 Codex CLI、网络中继或抓包进程。命令分为依赖、
R 类原始字节、P 类 pcap 和文档门禁四组；规则对应的运行号与目录由
[`EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) 提供。新版本的一键采集与比较见第四部分。

```bash
# ① 解析依赖与样本完整性
python3 -m pip install zstandard hpack
python3 tools/official_client_capture/check_sample_integrity.py \
    local-analysis/captures/raw-scrubbed/relay-imgedit1/relay

# ② 从 R 类原始字节复现客户端→上游的 header、body 和帧形态
python3 tools/official_client_capture/relay_extract.py \
    --relay-dir local-analysis/captures/raw-scrubbed/relay-imgedit1/relay \
    --output /tmp/check.json

# ③a SPEC-EP-002：普通会话、token 刷新和文件上传的 SNI
python3 tools/official_client_capture/pcap_clienthello.py \
    --dir local-analysis/captures/wire-parity-fix-20260727/official-baseline/oauth-ep002-allhosts/direct
python3 tools/official_client_capture/pcap_clienthello.py \
    --dir local-analysis/captures/wire-parity-fix-20260727/official-baseline/oauth-ep002-refresh/direct
python3 tools/official_client_capture/pcap_clienthello.py \
    --dir local-analysis/captures/raw-scrubbed/audit-ep002-file-upload-full2-20260730a/direct

# ③b SPEC-TLS-001/003、SPEC-PROTO-001：解析默认 HTTP 与 WS ClientHello
python3 tools/official_client_capture/pcap_clienthello.py --by-subdir \
    --dir local-analysis/captures/wire-parity-fix-20260727/official-baseline/oauth-20260727T091556Z-noplugins/direct

# ③c SPEC-TLS-002：解析有效自定义 CA 下的 HTTP ClientHello
python3 tools/official_client_capture/pcap_clienthello.py \
    --dir local-analysis/captures/wire-parity-fix-20260727/official-baseline/audit-tls002-ca-n0-20260730a/direct

# ④ 文档与证据门禁；全部命令必须零退出
python3 tools/check_spec_refs.py --symbol --cfg-test
python3 tools/spec_status.py --check
python3 tools/check_sample_counts.py
python3 tools/evidence_index.py --check
python3 tools/check_gaps.py
python3 -m unittest tools/test_check_spec_refs.py
```

③a 的普通会话、刷新和上传样本分别给出 `chatgpt.com`、`auth.openai.com` 和动态
`oaiusercontent.com` SNI；`api.openai.com` sideband 由 `SPEC-EP-002` 所列 R 类运行证明。
域名全集样本的过滤条件不得包含被测域名。③b 只判断已捕获 ClientHello 的内部结构，
预期为 37 个 native-tls 画像、4 个 rustls 画像，且 41 个 ClientHello 均不 offer ALPN；
③c 应得到 10 个 cipher，并依次 offer `h2`、`http/1.1`。R 类解析只能证明原始字节中
实际保存的方向和字段；J/M 摘要未记录的内容不能据此补推。

### 1.2.2 样本完整性与证据类型

`check_sample_integrity.py` 将连接分为双向、只有下行、只有上行和两向皆空四类。
存在非预期单向或空连接的样本只能支持已观察到的正例，不得用于缺失、总数、最大值等
全称或计数命题。

证据类型及边界统一定义在 [`EVIDENCE_INDEX.md`](EVIDENCE_INDEX.md) §0：

- **P**：原始 pcap，可复核 ClientHello、SNI、TLS 扩展等被动线索。
- **R**：等长脱敏的中继原始字节，可重新解析 HTTP/1.1、body 和 WS 帧。
- **J**：JSON 摘要，只能证明摘要已保存的字段。
- **M**：manifest 绑定的脱敏分析，可复核已绑定字段，不能代替原始字节。

R 类原始字节位于 `local-analysis/captures/raw-scrubbed/〈运行号〉/relay/*.bin`；凭据值
采用等长占位替换，长度字段和字节偏移不变。`local-analysis/` 不进入 Git，复核者需直接
访问工作区目录。

## 1.3 为什么要有这份表

源码能够说明生产路径和依赖归因，但不能单独确定运行时 TLS、线序和帧形态；抓包能够
证明实际输出，但不能自动说明触发条件和内部机制。因此每条规则同时记录源码依据、运行
证据、适用范围和观测边界，并通过编号把规格、证据和实现差异连接起来。

## 1.4 任务定义

**目标**：让 Sub2API 的 OpenAI OAuth 出站与官方 Codex CLI 0.145.0 一致。

五步流程规定一轮对齐如何验收；§1.5.1 的四阶段规定何时评估架构替代方案；Codex CLI
新版本的规则发现、抓包和画像升级见第四部分。

### 1.4.1 五步流程

| 步骤 | 动作 | 验收标准 |
|---|---|---|
| 1. 建立规则 | 读取官方生产源码、依赖源码和运行证据 | 每条规则具有范围、命题、证据等级和可变性 |
| 2. 验证规则 | 选择能够观测该命题的通道 | 可见 wire 规则绑定运行号；不可观测部分明确边界 |
| 3. 对比实现 | 按 SPEC 编号比较官方形态与 Sub2API | 每项差异可追溯到规则和证据 |
| 4. 实现修正 | 按差异实施，并限制在对应画像范围 | 不改变范围外 provider、协议或凭据路径 |
| 5. 出站验收 | 重新采集 Sub2API 出站并逐项复核 | wire 证据通过；源码检查和单测不能替代出站证据 |

规则数量与状态以第二部分导语为准，源码／抓包充分度以 `EVIDENCE_INDEX.md` 为准，
实现差异与验收状态见第三部分。规则按形态维度统计，端点按 URL 统计，两者不能换算。
抓包有限的两项及各自边界由 `SPEC-EP-012` 和 `SPEC-EP-023` 直接说明。

## 1.5 已确定的实现决策

1. **范围只含 OpenAI**；Anthropic 出站不属于本版。
2. **方案甲继续使用现有 Go 实现**；方案乙的同源 sidecar 已完成可行性评估，本版不实施。
3. **h1 wire 定型只在官方 OpenAI 画像下启用**，并保留关闭开关。不同上层调用不共享
   Client；同一次调用的 retry 必须保留连接池和断线重连语义，详见 `SPEC-CONN-001`。
4. **不 fork `x/net/http2`**。h2 形态只在有效自定义 CA 使 HTTP 协商到 h2 时暴露；
   本产品未开放该条件分支，因此不承担对应实现成本。
5. 画像逻辑放在 fork 独有文件或新包中，共享上游文件只保留最小接入点。

### 1.5.1 推进顺序：先穷尽方案甲，再评估方案乙

架构评估按以下顺序进行：

1. 用源码与抓包建立规则全集和官方基线。
2. 将方案甲修正到可达到的边界，并逐条完成出站验收。
3. 以方案甲无法消除的残留偏离定义硬边界。
4. 根据残留偏离的辨识度和实现成本评估方案乙。

当前硬边界和残留项已明确，剩余偏离不足以支持引入 sidecar；选型结论见 §3.3.4。
方案甲内尚未完成的实现或验收不改变该架构结论。

## 1.6 工作方法

### 1.6.1 证据等级

| 等级 | 含义 | 风险 |
|---|---|---|
| **L1** | 官方生产代码直读 | 最强，但要确认是生产路径而非测试 |
| **L2** | 依赖库行为推断（hyper/tungstenite/h2 的默认行为） | 官方源码里看不到，易整层漏掉 |
| **L3** | 实测为唯一依据（源码给不出答案） | 受样本覆盖度限制 |
| **L4** | 单元测试 / mock 数据 | **仅参考**，不得作为断言依据 |

判为 L3 前必须查完官方生产调用链和相关依赖层；TLS ClientHello、系统 TLS 策略及
WS 运行时帧形态可以合法使用 L3。L4 不得作为规则断言依据，`--cfg-test` 会阻止
L1/L2 引用落入测试代码。

### 1.6.2 可变性

- **固定**：每次请求都一样
- **随机**：官方每次都不同（复刻时必须同样随机，钉死单一样本反而是负指纹）
- **条件**：随场景变化（Lite/非 Lite、有无 attestation、直连/代理）

### 1.6.3 本版范围

本版只覆盖 Codex CLI 0.145.0 的 **OpenAI OAuth 出站**，包括第二部分列出的 TLS、
协议协商、连接、HTTP/1.1、HTTP/2、WebSocket、header、body 和端点辅助链。
Anthropic 出站不在本版范围。

### 1.6.4 什么不列为规则：可合法关闭的流量

官方允许关闭 plugins、apps、analytics 和 otel，因此这些流量是否出现不作为画像规则，
Sub2API 不发送它们也不构成偏离。采集基线时使用 `--disable plugins`、
`--disable apps`、`[analytics] enabled=false` 和 `[otel] exporter="none"`，并记录配置。
被关闭的请求只可作为同期同栈旁证，不得用于必须出现、连接总数或请求分布等命题。

### 1.6.5 观测通道与各自的盲区

完整通道定义和实验设计见
[`CODEX_CLI_0145_EMPIRICAL_VALIDATION_PLAN.md`](CODEX_CLI_0145_EMPIRICAL_VALIDATION_PLAN.md)。
本表使用以下判定原则：

1. **证据只能证明通道可见的内容。** pcap 可证明 ClientHello、SNI、TLS 扩展和 TCP
   时序，不能证明加密后的 h1/h2/WS；R 类中继可证明应用字节，不能证明客户端直连时的
   ServerHello、证书和 TLS record 分片。
2. **过滤条件不得包含被测命题。** 验证域名全集必须使用不含 host 条件的 BPF；hosts
   劫持或按目标 IP 过滤的样本只能证明已进入该通道的连接。
3. **干预按因果链限定结论。** 证据表述为
   `初始状态 S → 观测或输入 E → 官方路径 P → 输出 R`；干预发生前的输出不因后续输入
   自动失效，干预后的状态和后续轮次不得外推为自然链路。
4. **条件分支必须在其真实条件下采集。** 自定义 CA 位于 HTTP TLS 选型上游，因此该
   样本不能证明默认 TLS 画像，但可证明 `SPEC-H2-*` 所描述的自定义 CA 分支；WS 始终走
   rustls，与 HTTP 的 CA 分支条件不同。
5. **ALPN 必须镜像客户端 offer。** 中继两侧协商结果不一致时，该次运行不得用于协议
   结论；显式 `--assume-alpn` 必须与被动 pcap 结果一致。

受控干预只证明客户端收到指定输入后的反应；纯合成样本只用于网关鲁棒性，不参与
“官方是什么样”的结论。

### 1.6.6 工具与环境

关键工具均位于 `tools/official_client_capture/`：`pcap_clienthello.py` 解析 pcap，
`upstream_byte_relay.py` 和 `run_official_relay_scenario.sh` 采集应用字节，
`relay_extract.py`、`check_sample_integrity.py` 和 `scrub_raw_bytes.py` 分别负责解析、
完整性检查和等长脱敏。

采集环境必须满足：

1. 默认 HTTP TLS 画像使用系统信任库，不设置 `CODEX_CA_CERTIFICATE` 或
   `SSL_CERT_FILE`；自定义 CA 分支单独采集。
2. 按 §1.6.4 关闭非必要流量，并把启用条件记录到运行 manifest。
3. 需要 HTTP 降级时拒绝 WS 连接，不用 HTTP 400 代替连接失败。
4. 未脱敏原始字节不得离开采集机；归档前必须完成等长脱敏和凭据零命中检查。

**证据归档**：`local-analysis/captures/wire-parity-fix-20260727/`（已 gitignore）。

### 1.6.7 相关文档分工

- **本表**：官方形态与对齐状态的唯一事实源。
- `EVIDENCE_INDEX.md`：逐项证据充分度、运行号和文件路径。
- `CODEX_CLI_0145_EMPIRICAL_VALIDATION_PLAN.md`：观测通道、因果边界和采样设计。
- `tools/official_client_capture/README.md`：抓包环境、安全和执行说明。
- `OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md`：实现差异、代码修正和验收记录。
- `README.md` §1.1：对外现状与残留差异。

---

# 第二部分 规则

共 **53 个编号项**，按性质与适用范围分为五组：

<!-- SPEC_STATUS_START -->
| 分组 | 条数 | 当前验证状态 | Sub2API 需对齐项 |
|---|---:|---|---:|
| **① 内置 OpenAI OAuth 可见规则** | **39** | ✅ 38；🟡 1 | **当前 38；完整 Voice/realtime 后 39** |
| **② 自定义 CA 条件分支** | **8** | ✅ 8；🟡 0 | **0** |
| **③ 自定义 provider 条件分支** | **1** | ✅ 1；🟡 0 | **0** |
| **④ 派生／内部机制说明** | **3** | 源码机制 | **3** |
| **⑤ 采集与观测记录** | **2** | 观测记录 | **0** |
| **合计** | **53** | — | **当前 41；完整 Voice/realtime 后 42** |
<!-- SPEC_STATUS_END -->

对固定转发到 OpenAI 官方 OAuth 上游的实现口径：

- 当前 38 条包含已验证的 realtime 第一跳 `SPEC-EP-009`；只暂不计唯一部分项
  `SPEC-EP-012`，因此当前为 **38 条可见 wire 规则 + 3 条机制要求 = 41 项**。
- 完整覆盖 Voice/realtime 后为 **39 条可见 wire 规则 + 3 条机制要求 = 42 项**。
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

# 第三部分 方案与选型

本章只回答三个问题：两种方案如何覆盖必须实现的 42 项、如何跟进 Codex CLI 升级、
以及如何降低 Sub2API 长期合并上游的成本。规则定义和验收口径只以第二部分为准；
本章不维护易过期的逐条完成状态。

## 3.1 选型前提

完整目标是第二部分定义的 **42 项**：39 条内置 OpenAI OAuth 可见规则，加 3 条
派生／内部机制要求。自定义 CA 的 8 条、自定义 provider 的 1 条和 2 条采集记录
不属于该实现目标。

| 实现层 | 数量 | 范围 |
|---|---:|---|
| 传输、协议与连接 | 13 | `TLS-001/003`、`PROTO-001/002`、`CONN-001`、`H1-001~004`、`WS-001/002/004/005` |
| Header、Body 与端点行为 | 26 | `HDR-002/004~008`、`BODY-001/002/003/005/006`、15 条 `EP` 可见规则 |
| 派生／内部机制 | 3 | `HDR-001`、`BODY-004`、`EP-023` |
| **合计** | **42** | **必须全部对齐** |

因此，选型不能只比较谁更容易生成相同的 h1、h2 或 WS 字节。后两组共 **29 项**，
需要身份上下文、请求定型、状态管理和端点编排；仅替换 HTTP 客户端不能自动完成。

## 3.2 方案甲：Go 内薄层定型

### 3.2.1 架构

保留 Sub2API 现有路由、鉴权、账号调度、协议转换、重试、流式响应和计费，在协议转换
之后、最终出站之前增加一层仅服务于官方 OpenAI OAuth 的定型逻辑：

```text
目标账号与 CLI 版本
→ Profile Resolver
→ OfficialEgressContext
→ Header / Body / Endpoint Finalizer
→ HTTP / WebSocket 传输画像
→ 官方上游
```

| 组件 | 职责 |
|---|---|
| 版本化 Profile | 固定某一 Codex CLI 版本的端点、字段、header 和传输参数 |
| `OfficialEgressContext` | 保存身份值、来源、会话状态和生命周期 |
| 语义 Finalizer | 实现 26 条应用层规则及 3 条机制要求 |
| HTTP／WS 传输画像 | 实现 13 条传输、协议和连接规则 |
| 验收门禁 | 将每项规则同时映射到实现、官方证据和 Sub2API 出站证据 |

现有 `official_egress_*`、`tlsfingerprint` 和 `h1WireConn` 可作为该架构的基础，
但“已有基础”不等于“42 项已经完成”；是否完成只能按第二部分逐项验收。

### 3.2.2 覆盖 42 项的方式

| 范围 | 实现要求 |
|---|---|
| TLS 与协议选择 | 画像明确 TLS 栈、ALPN 和 HTTP／WS 分支，不从代理状态推断 |
| 连接生命周期 | 调用间、调用内 retry、断线重连分别按 `CONN-001` 管理 Client 与连接池 |
| HTTP/1.1 | 每个适用端点使用版本化线序清单；未配置端点不得以通用兜底冒充已对齐 |
| WebSocket | 同时定型握手大小写与顺序、压缩参数、帧状态和 `response.create` 字段 |
| Header 与 Body | 在最终出站点封闭字段集合、值、顺序、压缩和 Lite 变换 |
| 端点与机制 | 在 Go 内完成 images、compact、search、realtime、turn-state 和 reason 编排 |

方案甲不要求复制 Codex 的内部类或函数；只要可见结果与第二部分一致，内部实现可以
继续使用 Go 和 Sub2API 现有业务结构。

### 3.2.3 长期升级与合并方式

方案甲应由“散落的条件分支”收敛为“稳定执行引擎 + 不可变版本画像”：

1. 每个 Codex CLI 版本建立独立画像；升级时新增画像，不覆盖旧画像。
2. 端点 header 线序、字段集合和条件矩阵尽量数据化；状态机与传输适配器保持通用。
3. 画像只放在 fork 独有文件或新包中，共享上游文件只保留最小接入点。
4. 每次升级按“官方源码／抓包 → 规则差异 → 新画像 → 双向抓包验收”更新。
5. 未知版本或缺少端点画像时明确失败或退出官方画像，不得静默套用旧版本。
6. 合并 Sub2API 上游后运行同一套规则覆盖和出站字节门禁，及时发现接入点漂移。

这种组织方式把 Codex 升级成本集中在画像差异，把 Sub2API 合并冲突集中在少数稳定
接入点。它仍需持续抓包和维护传输细节，但不会额外引入跨进程协议与部署单元。

## 3.3 方案乙：同源 Rust sidecar

### 3.3.1 架构

Go 保留业务层和语义定型，将有序 header、body、连接参数及流式控制信息通过 IPC
交给 Rust sidecar；sidecar 锁定 Codex CLI 同版本依赖和配置分支，并负责最终 HTTP／WS
连接：

```text
Go 路由、鉴权、账号调度与语义 Finalizer
→ 保序 IPC
→ Rust 同版本传输层
→ 官方上游
```

要覆盖完整 42 项，方案乙仍须在 Go 内保留 26 条应用层规则和 3 条机制要求。sidecar
主要替代传输实现，不能从收到的请求中自动推导 Codex 的业务语义。

### 3.3.2 PoC 已确认的能力边界

| 层 | 结论 | 对 42 项的意义 |
|---|---|---|
| h1 header 线序、大小写和 `host` 位置 | 同版本依赖可自然产生相同字节 | 对 `H1-001~004` 有明显优势 |
| WS 握手前五项 | 可自然一致 | 覆盖 `WS-001` 的主要形态 |
| WS 剩余 header | 仍取决于插入顺序和 `swap_remove` 扰动 | `WS-002` 仍需显式定型 |
| h2 SETTINGS 与窗口 | 对齐官方配置分支后可以一致 | 优势明显，但自定义 CA 的 h2 规则不在 42 项内 |
| TLS ClientHello | 受系统 OpenSSL、平台和配置分支影响 | 不能仅靠锁定 Rust crate 自动获得 |
| 应用语义与端点编排 | sidecar 不会自动生成 | 29 项仍由 Go 实现 |

“使用相同依赖”只减少部分传输层复刻，并不等于整条 Codex 出站链自动同源。依赖版本、
feature、TLS 后端、构造顺序、Client 生命周期和配置分支仍须逐项匹配。

### 3.3.3 生产化成本

方案乙除 Rust 传输代码外，还必须长期维护：

- 保序且可版本演进的 IPC 协议，不能用无序对象传 header；
- SSE、普通响应和 WS 双向流的背压、取消、超时与断线传播；
- retry、连接池、代理、自定义 CA、认证刷新和错误语义；
- 计费、日志、指标、追踪、健康检查、灰度与回滚；
- Go 与 Rust 两套依赖升级、安全修复、构建和发布流程。

若把 29 项应用行为也迁入 sidecar，sidecar 将从“同源传输层”膨胀为 Codex 的应用层
分叉或嵌入，升级与合并成本反而更高。

## 3.4 两种方案对比

| 维度 | 方案甲：Go 内薄层 | 方案乙：Rust sidecar | 更优 |
|---|---|---|---|
| 完整覆盖 42 项 | 语义、机制和传输可在同一上下文闭环 | 29 项仍需 Go，sidecar 只接管部分传输项 | **甲** |
| 传输默认形态 | 需画像和少量 wire 定型 | 同依赖、同配置时部分形态可自然一致 | **乙** |
| Codex CLI 升级 | 新增版本画像并按规则重验 | 依赖默认变化可少量继承；应用语义、配置和 IPC 仍要更新 | **甲略优** |
| 合并 Sub2API 上游 | fork 独有包 + 少数稳定接入点 | Go Finalizer 仍在，并新增 IPC、sidecar 和部署接入 | **甲** |
| 运行与排障 | 单进程、沿用现有链路 | 多进程、跨语言、故障面更大 | **甲** |
| 回滚 | 切换版本画像或关闭官方画像 | 需同时协调主程序、协议和 sidecar 版本 | **甲** |
| 自定义 CA 下的 h2 | 实现成本高，且本版不要求 | 同源 Rust 配置更有优势 | **乙** |
| 当前基础 | 已有 Go 薄层和 wire 定型组件 | 只有传输可行性 PoC | **甲** |

### 3.4.1 选型结论

**生产方案选择方案甲；方案乙保留为传输层对照探针和后备路线。**

理由如下：

1. **目标是 42 项全覆盖，不是只复制传输栈。** 其中 29 项无论是否引入 sidecar，
   都需要 Sub2API 的身份上下文、语义定型和端点编排。
2. **方案乙最强的 h2 优势不属于当前必做范围。** 自定义 CA 的 8 条规则已明确排除，
   不能用非必做项的收益承担全链路 sidecar 成本。
3. **方案甲更利于双线升级。** 版本画像隔离 Codex 差异，fork 独有包和最小接入点
   隔离 Sub2API 上游变化；两类升级都可由同一套 42 项门禁约束。
4. **方案乙没有消除 Go 层，只是在其后增加一个分布式边界。** IPC、流式背压、
   连接生命周期、可观测性和发布协调会成为新的长期维护面。
5. **方案乙的优势仍然值得利用。** Rust PoC 可在 Codex 升级时充当传输基准，帮助
   判断 Go 画像差异，但不进入生产请求路径。

## 3.5 实施与重评估边界

方案甲达到完成状态必须同时满足：

1. 42 项分别映射到明确的实现点和触发条件，无通用兜底被计作已对齐。
2. 每项同时具备第二部分要求的官方证据和 Sub2API 出站证据。
3. CLI 版本画像相互隔离；旧版回归与新版验收可以同时运行。
4. 合并 Sub2API 上游后，规则覆盖、单元测试和原始字节门禁全部通过。
5. 完整 Voice/realtime 链完成后，目标从当前 41 项闭合为 42 项。

出现以下任一条件时，重新评估方案乙：

1. 产品将自定义 CA／h2 纳入必须支持范围。
2. Codex 新增高辨识度且无法在 Go 中稳定实现的必做传输规则。
3. 连续多个 CLI 版本表明 Go 传输画像的维护成本高于 sidecar 的运行与发布成本。
4. Codex 提供稳定、可复用的官方 Rust 出站边界，使 sidecar 不再依赖内部实现。

两种方案都只能对齐单次请求、连接和协议层的可见形态。Sub2API 作为多账号网关，
其并发度、请求节奏和跨账号统计特征不会因更换实现语言而自动等同于单用户 CLI；
该差异不属于 42 项，也不能作为选择 sidecar 的理由。

# 第四部分 Codex CLI 更新与规则演进

本部分定义从一个已验收版本升级到新版本的固定流程。**42 项只属于 0.145.0 基线，
不是未来版本的固定总数。** 新版本可以继承、修改、删除既有规则，也可以增加新的出站
形态；目标版本的规则总数只能在完成发现和分类后确定。

## 4.1 升级原则

每个新版本都必须重新完成规则覆盖验证，但不要求人工逐条执行 42 次抓包：

1. 旧版本源码和抓包是比较基线，不能直接充当新版本证据。
2. 一组场景可以同时覆盖多条规则；采集按场景编排，验收仍按规则编号统计。
3. 源码和依赖差异决定重点调查范围，不能替代新版本的运行证据。
4. 未变化规则可以复用实现逻辑，但目标版本仍须留下对应的源码与运行验证结果。
5. 新画像独立于旧画像；升级不得覆盖已经验收的版本配置。
6. 账号权限、服务端条件或场景未触发时记为阻塞，不得继承旧版本结论或记为通过。

## 4.2 一键升级审计

统一入口为 `tools/official_client_capture/codex_upgrade.py`。它只负责编排、清单、提取、
比较和报告；具体抓包仍由现有专用脚本执行。

先执行 dry-run，确认版本、路径、任务和规则映射，不写盘、不发请求：

```bash
python3 tools/official_client_capture/codex_upgrade.py \
  --dry-run \
  --baseline-version 0.145.0 \
  --target-version 0.146.0 \
  --baseline-source local-analysis/sources/codex-cli-0.145 \
  --target-source /path/to/codex-cli-0.146 \
  --baseline-evidence /path/to/0.145.0/official-evidence \
  --target-sha256 <64位小写SHA-256> \
  --runtime-image <镜像引用@sha256:digest> \
  --output /root/oauth-capture/runs/codex-upgrade-0.146.0
```

确认真实请求、账号配额和环境恢复条件后，一次执行完整采集与分析：

```bash
python3 tools/official_client_capture/codex_upgrade.py \
  --execute --acknowledge-live-requests \
  --baseline-version 0.145.0 \
  --target-version 0.146.0 \
  --baseline-source local-analysis/sources/codex-cli-0.145 \
  --target-source /path/to/codex-cli-0.146 \
  --baseline-evidence /path/to/0.145.0/official-evidence \
  --target-sha256 <64位小写SHA-256> \
  --runtime-image <镜像引用@sha256:digest> \
  --output /root/oauth-capture/runs/codex-upgrade-0.146.0
```

`--suite full` 是默认值；`--suite core` 只运行基础 HTTP／WS、compact 和 h1 场景，
不能作为完整版本验收。未来新增场景通过 `--extra-jobs` 加入同一次运行，不修改总入口。

### 4.2.1 固定执行顺序

```text
版本、哈希与环境预检
→ 旧版／新版源码和 Cargo.lock 出站面扫描
→ 官方 CLI direct、MITM 与原始字节场景
→ Sub2API 同条件 direct、MITM 与 wire 场景
→ JSON／JSONL／pcap／relay 统一提取
→ 旧版官方 vs 新版官方
→ 新版官方 vs Sub2API
→ 规则证据覆盖与新增形态门禁
→ report.json + report.md
```

运行目录一次性使用，禁止覆盖；每个子任务单独记录状态、日志、产物根和负责的规则。
任一环境恢复任务返回专用失败码时立即停止后续抓包，避免在污染环境中继续采集。

### 4.2.2 主要产物

| 文件 | 内容 |
|---|---|
| `manifest.json` | 版本、任务矩阵、命令参数和规则映射 |
| `analysis/baseline-source.json` | 旧版本源码出站面 |
| `analysis/target-source.json` | 新版本源码出站面 |
| `analysis/source-diff.json` | 新增／删除的调用点、端点、header 和依赖 |
| `analysis/official-surface.json` | 新版官方运行时出站面 |
| `analysis/candidate-surface.json` | Sub2API 运行时出站面 |
| `analysis/baseline-to-official.json` | 新旧官方动态形态差异 |
| `analysis/official-to-candidate.json` | 新版官方与 Sub2API 差异 |
| `analysis/coverage.json` | 每条规则的官方／候选证据任务及完成状态 |
| `report.json`、`report.md` | 机器与人工审阅入口 |

任务成功只表示证据已收集并完成结构比较，不自动等于规则通过。`ready` 还要求必需任务
成功、规则证据映射闭合、没有未分类的新形态，并且官方与候选动态出站面一致。

## 4.3 新出站形态发现

抓包只能看到被场景触发的路径，不能单独证明“没有其他出站”。新版本必须同时执行以下
四层发现：

### 4.3.1 源码调用点

扫描 Rust/TOML 中的 HTTP／WS Client、请求构造器、发送点、端点字符串和 header 插入点。
相关文件内容变化、新文件或新调用点都会进入 `source-diff.json`，等待人工分类。

### 4.3.2 网络依赖与配置分支

解析 `Cargo.lock` 中的 reqwest、hyper、h2、tungstenite、native-tls、rustls 等网络依赖。
版本、来源、checksum 或 TLS／feature 配置变化时，即使端点源码未变，也必须重验对应传输
规则。

### 4.3.3 动态出站面

从 JSON、JSONL、pcap 和 relay 原始上行统一提取以下签名：

```text
host/SNI + path及query键 + method + protocol
+ header名称与线序 + body结构哈希
+ WS事件类型、字段顺序与压缩
+ TLS cipher、扩展顺序与ALPN
```

新版官方出现旧版清单中不存在的签名时，必须产生新增形态候选，不能自动忽略。

### 4.3.4 覆盖责任

每个源码出站入口和动态形态必须处于以下状态之一：

- 已由场景覆盖并归入既有规则；
- 已建立新规则；
- 有证据证明不属于当前 OpenAI OAuth 范围；
- 因权限或外部条件阻塞；
- 尚未分类。

最后两种状态阻止目标画像发布。dry-run 中的 `official_unmapped` 和
`candidate_unmapped` 必须为空；新增场景可由 `--extra-jobs` 注入总编排器。

## 4.4 规则演进与画像更新

完成采集后，对每个差异作唯一分类：

| 分类 | 处理 |
|---|---|
| 继承 | 规则命题不变，为目标版本保存新的验证结果 |
| 变化 | 保留编号血缘，更新目标版本命题、证据和画像值 |
| 新增 | 分配新编号，补源码证据、官方抓包、Sub2API 实现和候选抓包 |
| 删除 | 证明对应出站入口已删除或条件不可达，不以一次未触发作为删除证据 |
| 条件变化 | 更新适用范围和触发矩阵，并补正例／负例 |
| 阻塞 | 记录缺少的权限或外部条件，禁止启用完整画像 |

目标版本规则清单由分类结果生成，不把 42 写死。每条规则仍须满足“实现点、触发条件、
源码证据、官方运行证据、Sub2API 运行证据”五项闭环。

## 4.5 启用与回滚门禁

新画像只有同时满足以下条件才能启用：

1. 新版本所有出站调用点均已分类，没有未知网络入口。
2. 目标规则清单中的必做项全部具有官方和 Sub2API 证据。
3. 官方与 Sub2API 的结构、线序、协议和条件行为逐项通过。
4. 旧版本画像回归测试仍通过，新画像没有覆盖旧画像数据。
5. 合并当前 Sub2API 上游后重新执行相同门禁并通过。

启用按版本显式路由；失败时切回上一已验收画像。未知 CLI 版本不得静默使用最新画像。
