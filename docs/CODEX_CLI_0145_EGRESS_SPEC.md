# Codex CLI 0.145.0 出站规格与实现手册

版本绑定：`codex-cli 0.145.0`<br>
依赖锁定：`hyper 1.8.1` / `hyper-util 0.1.20` / `http 1.4.0` /
`tungstenite 0.27.0` / `h2 0.4.13` / `reqwest 0.12.28`<br>
末次更新：2026-08-05

本文是 Codex CLI 0.145.0 OpenAI OAuth 出站形态及 Sub2API 实现的**唯一人类可读权威文档**，
内容包括：

1. 官方规则如何由源码、依赖和抓包形成；
2. Codex CLI 0.145.0 的规则是什么；
3. Sub2API 当前怎样实现并验收这些规则；
4. Codex CLI 与 Sub2API 上游升级必须遵守哪些约束。

原始源码、抓包、JSON Schema 和可执行工具是机器证据；`docs/EVIDENCE_INDEX.md` 是工具生成的
逐规则证据索引。

| 部分 | 回答的问题 | 权威内容 |
|---|---|---|
| 第一部分 | 规则依据与复算方式 | 范围、证据、抓包、源码分析和规则准入 |
| 第二部分 | 官方 0.145.0 是什么形态 | 53 个编号项原文，其中 42 项属于 Sub2API 对齐范围 |
| 第三部分 | Sub2API 怎样实现 | 伪装架构、版本画像、运行链、文件台账和合并边界 |
| 第四部分 | 版本怎样升级 | 五份版本清单、分阶段抓包、双侧断言、启用和回滚 |

---

# 第一部分 规则如何形成

## 1.1 目标、范围与当前结论

目标是让 Sub2API 使用 OpenAI OAuth 账号出站时，在第二部分规定的范围内产生与
Codex CLI 0.145.0 一致的可观测形态。范围覆盖 TLS、协议与连接、HTTP／WebSocket、
Header、Body、端点和跨请求状态；官方 Codex CLI、Compatible 和 Responses 入站最终使用
同一版本画像。

本版限定为内置 OpenAI OAuth 和各规则注明的平台／条件，不覆盖 Anthropic、OpenAI API Key
mimic、其他供应商及可关闭的 plugins、apps、analytics、otel 流量。自定义 CA 和自定义
provider 规则仅记录条件分支，不计入内置 OAuth 的实现目标。

第二部分是规则的唯一事实源：共 53 个编号项，其中 39 条可见规则和 3 条内部机制需要
Sub2API 对齐，合计 42 项。规则范围、证据充分度和实现状态彼此独立；42/42 结论只绑定验收
时冻结的源码树、镜像、画像摘要和证据，不能由版本号或当前工作树自动继承。

## 1.2 证据位置与完整性

| 证据层 | 位置 | 用途 |
|---|---|---|
| 官方生产源码 | `local-analysis/sources/codex-cli-0.145/codex-rs/` | 读取调用链、条件和内部机制 |
| 锁定依赖源码 | `tools/spec_source_deps/` | 解释 hyper、h2、http、tungstenite 等线形行为 |
| 官方抓包 | `local-analysis/captures/` | 证明运行时实际发出的 TLS、HTTP 和 WS 形态 |
| Sub2API 源码 | `backend/` | 定位伪装实现的画像、执行器和上游接入点 |
| Sub2API 机器证据 | `docs/egress/` | 保存发送面基线、迁移收据、final-wire、性能和维护复算资产 |
| 证据索引 | `docs/EVIDENCE_INDEX.md` | 机器生成的规则编号、运行号、路径和证据强度 |
| 引用锚点 | `tools/spec_ref_anchors.json` | 防止源码版本、符号和行号静默漂移 |

官方源码树、锁定依赖、抓包运行号和摘要必须同时绑定；截图、HTTP 200、测试日志或单独的结论
JSON 不能替代可重新解析的证据。

| 证据 | 内容与边界 | 等级 |
|---|---|---|
| **P** | 原始 pcap；证明 ClientHello、SNI、TLS 扩展和连接时序 | L3 |
| **R** | 等长脱敏的中继原始字节；证明 HTTP 线序、Body、WS 握手与帧 | L3 |
| **J** | JSON 解码摘要；只证明摘要保留的字段 | L3／L4 |
| **M** | manifest 绑定的脱敏分析；只证明清单绑定事实 | L3／L4 |
| 官方生产源码 | 位于真实调用链的直接事实 | L1 |
| 锁定依赖源码 | 准确版本与 feature 下的依赖行为 | L2 |
| 测试、mock、合成输入 | 只作辅助，不能单独定义官方规则 | L4 |

R 类只做等长秘密替换，保持名称、大小写、偏移、长度和帧结构；未脱敏材料不得离开采集机。

## 1.3 从源码和抓包形成规则

1. 冻结官方源码、依赖、二进制、平台、配置和账号范围。
2. 从生产入口追到请求构造、认证、Client 生命周期、TLS 和依赖层。
3. 按传输、端点、Lite、压缩、状态和错误分支建立场景矩阵并选择 P／R／J／M 通道。
4. 用源码推导预期，再用抓包反查实际发送路径；两侧证据不足时只收窄命题，不补猜测。
5. 为规则登记范围、可变性、源码、实测、实现要求、状态和边界，并执行完整性门禁。

源码解释机制与条件，运行证据证明实际输出；两者不能互相替代。

## 1.4 证据等级、可变性与观测边界

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
- 单向、空连接或受控失败样本只能证明观察到的正例，不能支持连接全集等全称结论。

## 1.5 证据复算、重新抓包与规则收口

日常复算只使用仓库总门禁；它会检查证据、规则、台账、版本泄漏和实现契约，不启动真实请求：

```bash
make check-egress-spec
```

该命令是本地完整门禁，额外读取被 `.gitignore` 排除的 Codex CLI 源码镜像以校验规格引用。
GitHub Actions 使用 `make check-egress-spec-ci`，只跳过这项本地源码引用检查，其余提交态
证据、执行契约和回归测试完全一致。

`docs/egress/` 只由本地门禁、CI、版本升级、上游合并和审计工具读取，不是服务运行时配置，
不参与请求转发或 Codex 版本激活，也不需要复制进生产镜像。

新版本抓包、候选抓包、比较和验收只使用：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

精确运行号和证据路径由 `docs/EVIDENCE_INDEX.md` 生成；底层采集、解析和 finalizer 只是该
入口的受控依赖，不形成平行流程。

## 1.6 规则准入、变更与停止标准

规则只有在范围明确、生产路径或 L3 属性明确、观测通道足够、正反例闭合、引用与摘要可复算，
且规则、画像、场景和断言之间没有未分类项时才能准入。实现要求描述可见结果，不要求复制
官方内部代码结构。

达到场景覆盖、重复样本和源码闭环即可停止当前采样，但结论只适用于已冻结版本、平台和
条件；版本、依赖、平台或配置分支变化时必须按第四部分重新分类。

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

## 3.1 总体架构与 persona 边界

本方案有两条总原则：

1. 无论入站来自官方客户端还是第三方标准 API 客户端，OpenAI OAuth 官方出站均由内置
   active 画像统一定型为对应版本 Codex CLI 的真实形态。
2. 原有兼容层继续负责协议、模型、工具和请求语义转换；官方画像不改变 Key、Group、账号
   路由或计费归属。

当前身份架构分为两个互不越权的平面：

| 平面 | 当前实现 | 可以影响 | 绝不能影响 |
|---|---|---|---|
| 版本发现与入口归一化 | GitHub `/releases/latest`、列表回退、6 小时间隔、启动防抖、UA/version 配对和账号 UA 兼容 | `openai_codex_client_version_synced`、管理端候选值、客户端名、OS／架构／终端指纹 | active ReleaseCatalog、画像摘要、最终出站 version 或任何 wire 契约 |
| active strict wire | ReleaseCatalog、ReleaseBundle、Compiler、Executor 与受信 adapter | URL、Header、Body、顺序、压缩、TLS、HTTP/1.1、WS、状态和连接生命周期 | 被候选版本、管理员 UA 版本段、账号级 UA 或入站身份覆盖 |

当前 active strict wire 固定为 Codex CLI 0.145.0。自动同步只更新候选版本；切换 active
ReleaseCatalog 必须经过目标版本证据验收和显式发布。

```text
HTTP／WS 入口、辅助端点与内部任务
→ 原有鉴权、Key／Group、账号调度、计费和协议兼容层
→ IngressSnapshot + OfficialRouteCatalog
→ codex-cli persona：ReleaseCatalog → ReleaseBundle → Plan → Executor
→ PreparedRequest + FinalizationToken + TransportSpec
→ 受信 HTTP／req-profile／WebSocket adapter → Runtime Guard → OpenAI
```

| 层 | 职责 | 不得承担 |
|---|---|---|
| 业务与兼容层 | 鉴权、Key／Group、账号调度、模型映射、工具和协议语义、重试、计费 | Codex 版本身份和最终 wire 定型 |
| 稳定执行引擎 | 解析画像、构造最终 URL／Header／Body、选择传输、管理连接与状态 | 版本常量、模型特判和业务归属 |
| 不可变版本画像 | 保存某一 Codex 版本的端点、字段、线序、条件和传输契约 | 账号选择、计费或协议桥接 |

| persona／状态 | 端点范围 | 逻辑出口 | 约束 |
|---|---|---|---|
| `codex-cli` | ReleaseBundle 登记的 Codex 端点闭集 | `CodexEgressExecutor` | URL、Header、Body、传输、状态与生命周期由同一 Bundle 驱动 |
| `chatgpt-web/chrome` | privacy settings、accounts check、subscriptions | 浏览器身份 Plan／client | 保持独立 Chrome TLS／HTTP2／XHR 语义，禁止套用 Codex 画像 |
| `transport_only` | authorization-code OAuth exchange | 独立受审 transport | 只复用有证据的传输事实，不冒充 refresh 的 Body／行为契约 |
| `unclassified` | PAT whoami、Agent Identity task register | 精确登记的遗留路径 | 完成官方行为举证前不得凭官方 host 自动归入 Codex persona |
| 未登记 | Catalog 未知 route 或 SinkBinding | 无长期出口 | enforce 状态下 fail-close |

画像由 OAuth 账号类型与 registry release 指针选择，不由客户端声明的版本、平台或 surface
选择。Compatible／Responses 入口先完成语义适配，再与官方客户端进入同一最终定型点。
入口分类只服务协议解析、语义适配、传输选择、观测与完整自洽的条件类型识别：例如官方
客户端已经使用 HTTP 入站时保持其 HTTP fallback，第三方入口则按 active 画像选择默认传输。
入站 `User-Agent`、`version`、`originator`、会话 UUID 和 `client_metadata` 原值均没有 wire
身份决定权；动态身份由 active 画像命名空间重新派生，条件类型成立时也只保留类型语义。

身份不匹配时投影并告警，不因为客户端声明拒绝业务：

- 任意入站 UA／version／originator：不参与 surface 匹配，按 release Build 默认运行态重建；
- Header、Body 或 `client_metadata` 身份互相冲突：丢弃冲突原值并派生同一生命周期的新身份；
- 条件事实缺失或冲突：按条件不成立处理，不伪造条件 Header；
- 顶层字段超出闭集：删除并按“入口类型 + 字段集合”去重告警；
- 已知字段的值不合契约（如 `tool_choice` 不是 `auto`）：按画像规范化，不拒绝请求。

未知字段只产生去重告警；账号配置非法、route／Sink 未登记或终态被篡改时 fail-close。

生产 HTTP／WS 路径不执行入站官方身份逐字段一致性校验；该校验器只用于离线夹具、画像诊断
和证据复算。Codex Desktop、Codex CLI 与第三方客户端均先归一化，再由 active Compiler 生成
0.145.0 wire persona。

## 3.2 Codex 0.145.0 画像与发布执行契约

`official_egress_codex_0145_profile.go` 保存 exec／TUI 身份、feature、端点、Header／Body
闭集、字段顺序、压缩、TLS、连接生命周期、条件状态和文件上传编排。画像在启动期完成解码、
结构校验和摘要核对，失败即阻止启动；运行时只读取不可变快照，需要改写的数据按次深拷贝。
当前画像摘要为：

```text
e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b
```

摘要变化表示完整画像发生变化，必须同步检查第二部分规则、版本清单、测试和抓包证据。

ReleaseCatalog 在启动时加载 release graph 和 SnapshotCatalog；`version + digest` 是不可变
坐标，active／previous 指向完整 release ID，未登记坐标不得回退。ReleaseBundle 冻结身份、
端点、Header／Body、feature、传输、连接、策略和 fallback 图；业务调用方只能提交 release
mode 与业务事实，不能自行选择具体版本或 transport ID。

CodexEgressPlan 只保存业务事实、IdentityMode、深拷贝后的 Header Override、各类 Policy 和
attempt-owned Body；`TransportSpec` 只能来自端点画像。Header 所有权固定如下：

| Header 类别 | 所有者与优先级 |
|---|---|
| Transport、Auth、Host、长度和 Body framing | 系统最终所有，账号及入站不能覆盖 |
| Release identity | strict／mimic／proxy 模式下由画像最终决定 |
| Account extensions | 普通 API Key 按产品规则覆盖；mimic／proxy 仅允许非保护字段 |
| Endpoint closed set | OAuth 官方端点删除画像闭集之外的字段 |
| Ingress headers | 最低优先级，只进入明确允许的字段 |

编译器生成语义终态；Executor 签发 FinalizationToken、注入 TransportSpec 并选择受信 adapter。
adapter 只能执行 Token 声明的 wire 等价变换，其他终态修改一律拒发。可重放 Body 按内容
摘要创建新 attempt；single-use stream 不得预读或复制，且只能尝试一次。

SnapshotDoc／ProfileSpec 保存版本事实，ExecutableProfile 校验可执行闭集。新增版本只能追加
快照、发布图节点和证据，不原位覆盖旧画像，也不在 §3.5.2 共享接入点散布版本分支；文本与
Go AST 版本泄漏门禁负责执行这一约束。

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

### 3.3.3 状态、连接与端点编排

turn-state 按 invocation／连接身份隔离，只从画像规定的响应位置更新。跨请求复用要求入站
会话头或入站 Body **显式携带** `prompt_cache_key`；必须以 `promptCacheKeySet` 判断，兼容层
自动生成的同名键不是显式锚点。没有可信锚点时仍可确定性派生当前 turn 身份，但不跨请求
复用上游状态，避免把一段对话的状态句柄带入另一段。

HTTP Client、retry、WS 和长生命周期 Client 均由端点画像声明；images、compact、
alpha-search、realtime、WHAM、OAuth refresh 和文件上传不得旁路统一执行器。

## 3.4 42 项覆盖与验收边界

| 责任面 | 数量 | 实现位置 |
|---|---:|---|
| TLS、协议、连接、h1、WS | 13 | 传输画像、`tlsfingerprint`、HTTP/WS 执行器 |
| Header、Body、端点可见行为 | 26 | 端点画像、header/body finalizer、端点编排 |
| 内部机制 | 3 | 运行上下文、turn-state、压缩选择 |
| **合计** | **42** | **同一版本画像和同一验收链闭合** |

规则只有同时具备画像／执行点、官方证据、候选证据和机器断言才算完成。官方与第三方入口
必须绑定同一源码树、镜像、profile digest 和规则清单；多账号调度、计费归属和服务级请求
节奏不属于 42 项，也不由画像改写。

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
| `backend/internal/service/official_egress_codex_*`、`official_client_profile_registry.go` | 0.145.0 不可变画像、可信 release Build 运行态投影、发布投影、端点编排、Files 与模型能力 |
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

fork 自有的画像、Catalog、Compiler、Executor 和 Guard 位于
`backend/internal/officialegress`。高频变化的 `service` 只采集业务事实、构造 Plan 和调用
Executor；`repository` 只提供连接池、代理和底层资源。

```text
service ───────────────────→ officialegress core
repository ────────────────→ officialegress core 的窄 port
officialegress/adapter/* ───→ core + 受信物理资源
wiring ────────────────────→ 注入闭集 adapter
```

`officialegress` 不得 import `service`／`repository`；公共边界只暴露中立 Plan、Release 和
port。socket adapter 依靠闭集登记、AdapterID、FinalizationToken、wire 测试和静态门禁建立
信任。Codex 版本通过新增画像和发布图实现；Executor 无法表达新端点机制时，才允许最小修改
共享文件并专项复验。

## 3.7 Guard、逐 Sink 灰度与静态门禁

Runtime Guard 先按真实 method、host、path、protocol 匹配 route，再由 Catalog 校验 persona、
SinkID、binding 和状态；canary／enforced 必须验证 FinalizationToken、Release／Profile／Pool
digest、adapter 和最终请求摘要。未知 route、无效 binding 或终态篡改按策略 fail-close。

逐 Sink 状态机为：

```text
legacy_observe → canary_enforce → enforced
                       ↑              │
                       └──── 回滚 ────┘
```

`legacy_observe` 只允许封存基线中的既有 sink；新增 sink 必须从 canary 进入 enforced。紧急
observe 必须限定 Sink 和期限，不能扩大 legacy 基线。静态门禁覆盖 net/http、HTTPUpstream、
req/v3、WS、facade 和 client factory，并用变异测试证明能发现包装旁路。Catalog 项只能凭
MigrationReceipt／RemovalReceipt 单调迁移或删除。

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

当前实现状态为：

- 21 个 `codex_profile` Runtime Sink、28 条 route 全部为 `enforced`，0 个 Codex
  `legacy_observe`；HTTP、WS、fallback、models、images、files、alpha-search、WHAM 和 OAuth
  refresh 均进入统一 Executor；
- ReleaseCatalog 启动期预编译不可变 active／previous；attempt Body 单次解析、重复键
  fail-close，并最终单次有序输出；
- 官方与第三方 OpenAI 入口共用 HTTP／WS 归一化和身份派生；入口身份冲突不再触发 502，
  显式逐字段身份校验只用于离线证据与诊断；
- 机器清单冻结 strict surface；相对 `v0.1.171` 的完整出站 overlay 由结构化 JSON 复算，
  Markdown 只保留 12 个高风险人工复核接缝；
- active／previous final-wire 使用空允许列表比较；实机状态以接受 Campaign 和部署报告为准。

当前兼容边界如下：

| 分类 | 当前决定 | 原因 |
|---|---|---|
| active／previous、per-sink canary／override、FinalizationToken | 保留 | 提供升级、回滚和防旁路能力 |
| browser persona、OAuth exchange transport-only、unclassified sink | 独立治理 | 不属于 Codex Executor persona，禁止混用画像 |
| service 画像 DTO／projection | 保留 | API Key mimic 和业务读取面仍有生产消费者 |
| unsigned `LegacyCompiledDispatcher` HTTP 执行路径 | 禁止执行 | 当前 Codex Runtime Catalog 全部 enforced，旧上下文进入通用发送入口时 fail-close |

兼容代码只有在生产定义和调用为零、替代路径有负例门禁、wire 无变化、active／previous
均通过且存在退休收据时才能删除。

---

# 第四部分 版本升级与上游更新规范

## 4.1 何时必须启动升级

Campaign 冻结的是目标 Codex 事实。出现以下任一变化都要新建版本 Campaign：

- Codex CLI 版本或官方二进制 SHA-256 变化；
- Cargo.lock、网络依赖、TLS 后端或目标平台变化；
- 官方默认 feature、端点、header、body、连接或状态路径变化；
- 经 `classify` 批准的目标版本画像或规则清单变化；
- 合并上游后统一应用点、Client 生命周期或协议适配发生变化。

同一目标 Codex 事实下，Sub2API 侧的实现迭代不新建 Campaign，改用新的 `--candidate-id`
在同一 Campaign 内追加候选：

- Sub2API 源码树、候选镜像或构建 ID 变化；
- 候选部署版本、OCI digest、image ID 或候选侧 profile digest 绑定变化。

候选的 `--profile-id` / `--profile-digest` 只证明该候选实现绑定了本 Campaign 已批准的
画像，不构成第二个画像事实源；画像本身变化仍属上面的 Campaign 级条件。

每个 candidate 独立绑定源码树、镜像、构建 ID、测试结果和最终身份，互不覆盖；
`capture-candidate`、`compare`、`accept` 按 candidate 分目录封存。

每个目标版本必须具备独立运行证据，不得复用其他版本的验收结论；同一 Campaign 内的
不同 candidate 也不得复用彼此的候选抓包、断言或验收结论。

## 4.2 版本化五份清单

目标版本的权威输入按 `<用途>_<版本>.json` 平铺在 `tools/official_client_capture/`，
包含：

| 文件 | 内容 |
|---|---|
| `codex_upgrade_rules_<版本>.json` | 目标版本规则全集、范围、必做性和断言器 |
| `codex_upgrade_rule_migration` | inherit／change／delete／add 分类 |
| `codex_upgrade_scenarios_<版本>.json` | 官方／候选场景、变量、清理动作和规则覆盖 |
| `codex_egress_profile` | 可直接编译的完整版本画像，不是旧版增量 |
| `candidate_rule_expectations_<版本>.json` | 独立于候选实现的正反例和逐规则期望 |

五份清单分别计算 SHA-256，并以联合摘要人工批准。规则数量由迁移结果产生；42 只属于
0.145.0，后续版本不得写死为 42。

运行事实源是 `internal/officialegress/profilecontract` 的 SnapshotCatalog；ReleaseCatalog
把 active／previous 绑定到 `version + digest`，其他 service 投影不得成为第二发布事实源。

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

`run` 只采集 attempt，`seal` 只绑定该 attempt 已声明的证据；状态只能前进，失败重跑必须新建
attempt，不能改写已有阶段。

## 4.4 升级步骤

目标侧证据由 Codex CLI 生产源码、锁定依赖源码和官方客户端抓包共同组成：客户端源码确定
调用链与条件，依赖源码解释 TLS、HTTP、WS 和连接机制，抓包证明目标环境中的实际 wire。
`docs/egress/` 是实现侧证据，用于证明 Sub2API 按同一规则生成了对应 wire，并且升级后没有
引入未登记漂移。目标侧证据与实现侧证据必须绑定同一 Campaign，但不能互相替代。

| 步骤 | 输入与动作 | 必须输出 | 完成条件 |
|---|---|---|---|
| 1. 冻结目标 | 冻结旧画像、规则、证据和镜像；锁定目标版本、源码、依赖、二进制 SHA-256、平台、配置和抓包环境 | 不可变 Campaign、环境身份、五份版本化清单草案 | 所有后续产物均能反向定位到同一目标二进制、源码树和环境 |
| 2. 官方取证 | 解读目标 Codex CLI 生产源码和锁定依赖，从生产入口追到请求构造、认证、Client、TLS 与传输调用链；再按场景清单执行 `capture-official run/seal`，采集 TLS、HTTP、WS、Body、端点和跨请求状态 | L1／L2 源码引用与锚点、调用链和条件结论，以及 `local-analysis/captures/` 或隔离采集机中的原始抓包、脱敏派生物、inventory、attempt result 和 evidence seal | 源码机制、依赖行为与动态 wire 形成闭环；官方目标出站面覆盖完整；秘密扫描与环境恢复通过 |
| 3. 更新规则 | 使用第 2 步形成的完整静态与动态证据比较旧／新版本；逐条分类 `inherit`、`change`、`add`、`delete`、`condition_change` 或 `blocked` | 完整的 `codex_upgrade_rules`、`codex_upgrade_rule_migration`、场景清单和独立候选期望 | 无未分类项；`blocked` 为零；联合摘要完成人工批准 |
| 4. 建立画像 | 根据已批准规则生成完整 SnapshotCatalog 快照和 release graph 节点；只有现有执行器无法表达新机制时才修改共享引擎 | 新版本完整画像、profile digest、active／previous 候选节点和正反例 | 新快照可独立编译、复算和回滚；此时不得改写当前 active release |
| 5. 候选验收 | 用最终源码树、镜像和新画像覆盖官方 Codex CLI／Desktop、Compatible、Responses HTTP／WS 与 Kilo，执行 `capture-candidate`、`compare` | 候选抓包、逐规则双侧断言，以及 `docs/egress/` 对应语义目录中的新基线、final-wire、迁移／删除收据、冲突和验收证据 | 官方与候选逐规则一致；第三方入口收敛到同一画像；`make check-egress-spec` 与完整回归通过 |
| 6. 接受与启用 | 执行 `accept`，重新计算全部摘要、收据和 evidence seal；按 §4.8 进行人工灰度或切换 | `ready` Campaign、Active／Previous 切换记录和可用回滚镜像 | `ready` 只授权后续启用；实际切换成功且回滚点保留后才算升级完成 |

`delete` 不能由单次抓包“没有出现”直接得出，必须同时具备目标版本源码可达性结论、覆盖该规则
触发条件的正反场景、旧规则引用清单和 RemovalReceipt。任一证据不足时只能标记 `blocked`，
不得删除规则或让新画像进入 `ready`。

写入 `docs/egress/` 时，当前可复算基线由对应工具生成；已封存的历史 JSON、摘要和收据不得
覆盖。新版本必须新增版本快照、证据文件或后继 transition，并在 `maintenance/` 记录画像、
源码、工作区和上游 overlay 的变化。

目标版本出现新出站面不是失败；未发现、未分类或用旧画像静默兜底才是失败。

## 4.5 必须保持的可信边界

可信边界如下：

1. Campaign 核心清单、attempt、preview 和阶段 result 均只写一次。
2. 每次 run 在发请求前原子预约并生成 `run_nonce`；孤儿预约失败关闭，不自动重跑。
3. 官方身份绑定版本、二进制、源码树、依赖和实际运行镜像。
4. 候选身份绑定源码树、构建 ID、部署版本、OCI digest、image ID 和 profile digest。
5. evidence root、日志、pcap、relay、收据和恢复报告共同进入逐文件 inventory。
6. compare、accept、status 都重算摘要并重放机器 finalizer；人工只能批准生成的
   `review_sha256`，不能手写通过结论。
7. 环境恢复失败会污染整个 Campaign，不允许自动 resume 或继续 seal。
8. `resume --rerun-failed` 必须创建新 attempt，不跨 attempt 复用日志或证据。
9. 真实请求只有在明确提供 `--acknowledge-live-requests` 后才能执行。
10. 抓包和验收不得执行 `compose down`、`pull` 或 `prune`，不得替换、重建数据容器。
11. 凭据、Cookie、完整代理 URL 和未脱敏原始字节不得进入 Git 或阶段身份文件。
12. `ready` 只表示证据门禁通过，不表示已部署、已备份或已完成生产回滚演练。
13. 新版本被自动发现后只更新候选值，不得自动改写 active ReleaseCatalog。正式升级原则上只
    新增快照、清单和测试；确因执行器无法表达新机制而修改共享层时，必须记录受影响规则、
    最小差异和专项复验，并显式更新版本泄漏基线与机器 overlay 台账。

## 4.6 官方与第三方入口矩阵

| 入口 | 必须证明 |
|---|---|
| 官方 Codex CLI／Desktop | 无论声明何种版本、平台、surface，或 Header／Body 身份是否一致，均收敛到 active 画像，不因入口身份冲突返回 502 |
| `/v1/chat/completions` | Compatible 适配后进入目标 HTTP Responses 画像 |
| `/v1/responses` HTTP | Responses 入口使用同一 HTTP 画像 |
| `/v1/responses` WebSocket | WS、预热和 fallback 使用同一版本画像 |
| Kilo Compatible | `kilo-compatible` 收据绑定模型、账号、请求、响应、usage 和 profile |
| Kilo Responses | `kilo-responses` 收据绑定相同候选身份和 profile |

Kilo 的 installation 可以早于 attempt；ingress、runtime、response 和 usage 必须绑定相同
Campaign、attempt、`run_nonce`，并位于 attempt 开始与 client checkpoint 之间。检查点后
不得继续发送 Kilo 模型请求。第三方入口证明“不同入站协议收敛到同一出站画像”，不要求每个
客户端独立触发规则全集。

入口矩阵只证明语义桥接正确，不建立多套 persona。Desktop `0.147`、CLI `0.145` 和 Kilo
最终都必须命中相同 active release ID、profile digest、version、originator、UA 构造器及
动态身份命名空间；允许的差异只来自业务内容、条件类型和画像规定的传输分支。

## 4.7 Sub2API 上游更新

Sub2API 上游更新与 Codex CLI 换版必须拆成两个变更集。上游更新要求如下：

1. 固定上游 commit、当前完整发送面、冲突面和画像摘要；
2. 上游业务代码与身份基础设施不得修改 active 版本画像；自动同步值只进入候选版本字段；
3. 重新生成 §3.5 的机器 overlay，审阅新增／删除／来源／范围差异，并逐项复核 12 个高风险
   合并缝的版本权威方向、调用顺序、上下文和旁路；
4. 重新生成 wire 等生成代码，复算 source-to-sink 台账并处理新增或删除的 route；
5. 运行全量测试、静态门禁和当前画像 final-wire 空允许列表对比，其中必须包含
   `tools/check_ledger_completeness.py` 与 `tools/check_version_leak.py`；
6. 按 §4.10 从最终源码树在本地交叉编译 Linux 二进制，并在 DMIT 封装最小镜像；
7. 新建候选 attempt，重跑官方、候选、Kilo、compare 和 accept；
8. 新增官方出站必须先登记 route、persona、SinkID 和 backend，禁止先放行裸 client；删除
   路径时先删除调用，再凭 RemovalReceipt 删除 Catalog 项；
9. 提交前再次运行机器 overlay 精确比对；禁止手改计数、漏登身份边界，或把
   `discovered_latest` 接到 active strict wire。

Fork 文件持有画像与 adapter；共享文件只保留候选身份基础设施、Snapshot、Plan 和 Executor
的最小接入。自定义 UA 只能贡献客户端名和环境指纹，版本段由 active 画像重建。Codex CLI
换版必须使用独立 Campaign。

## 4.8 启用与回滚

新画像只有在 Campaign `ready`、最终镜像身份一致且 full 门禁通过后才能灰度。灰度维度是
registry release 指针，不是入站客户端版本；新画像指向 Active，上一已验收画像保留为
Previous。

逐规则回归、digest 不一致、通用兜底、健康失败、账号异常、恢复失败或秘密扫描失败时，立即
切回 Previous 和旧镜像。回滚不得删除 Campaign、覆盖画像或重建数据容器；随后复核服务、
挂载、账号、keeper、代理／CA 和入口。

## 4.9 兼容代码退休流程

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

## 4.10 DMIT 部署与真实验收

DMIT 验收只替换 Sub2API 主应用，禁止执行 `compose down`、`pull`、`prune`，禁止重建或修改
PostgreSQL、Redis、keeper、环境文件和数据卷。标准流程为：

1. 冻结 commit／tree digest，准备同源码树的前端嵌入产物和运行资源。
2. 本地使用 `CGO_ENABLED=0`、`GOOS=linux`、实际 `GOARCH`、`-trimpath`、正式 build tags 和
   ldflags 交叉编译；记录二进制 SHA-256、架构、版本和 commit。
3. 只上传二进制、最小运行时构建文件和必要资源；DMIT 不运行 Go／Node 编译。
4. 用唯一标签封装不含源码、编译器和缓存的最小镜像，并记录 OCI revision、image ID 和摘要。
5. 保存旧镜像与 compose 回滚点，只替换主应用；检查 health、日志、`/v1/models`、数据库、
   Redis、keeper 和挂载均正常。
6. 分别完成 OAuth 与 API Key 真实请求并看到 `response.completed`；检查没有终态篡改、缺 Token、
   legacy passthrough 或 Guard violation。失败立即恢复旧镜像，成功后清理临时产物并保留报告。

凭据、Cookie、完整代理 URL 和请求 Body 不得进入报告；HTTP 200 不能替代业务完成事件。

## 4.11 文档、工具与证据保留

本文件是唯一叙事文档；以下内容仍作为机器资产保留：

| 资产组 | 保留原因 |
|---|---|
| `docs/EVIDENCE_INDEX.md` | 从本文件和证据映射生成的审核索引 |
| `docs/egress/` | 当前发送面、生命周期、final-wire、性能、冲突和维护机器证据；目录职责见其中的 `README.md` |
| `tools/spec_source_deps/`、`tools/official_client_capture/` | L2 依赖、版本清单、场景、Schema 和唯一编排入口 |
| `local-analysis/sources/codex-cli-0.145/` | 第二部分 L1 官方生产源码 |
| `local-analysis/captures/` 中被索引引用的脱敏证据 | 第二部分 P／R／J／M 规则可重新解析的基础 |
| 已接受 Campaign 的 result、assertions、inventory 和 evidence seal | 证明固定候选身份曾达到 ready |

`docs/egress/` 仅在开发、CI、版本升级、上游合并、回归和审计时使用。当前基线与统计由对应
工具确定性生成；已经封存的收据和历史摘要只追加后继 transition，不在原文件上改写。

只保留被规则或 Campaign 引用的文档、脚本和抓包。删除前必须生成保留清单、确认引用为零并
复验全部门禁。
