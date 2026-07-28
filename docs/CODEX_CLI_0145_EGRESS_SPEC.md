# Codex CLI 0.145.0 出站形态规格表

版本绑定：`codex-cli 0.145.0`　依赖锁定：`hyper 1.8.1` / `http 1.4.0` / `tungstenite 0.27.0` / `h2 0.4.13` / `reqwest 0.12.28`
首版 2026-07-27，末次更新 2026-07-28

本文分三部分：

- **一、背景与任务** —— 为什么做、怎么做
- **二、规则** —— 官方形态是什么样，52 条
- **三、方案** —— **方案甲**（Go 内薄层定型，当前在用）与 **方案乙**（同源
  sidecar，理想的未来方案），以及方案甲的边界

**推进顺序已定**（§1.4.1）：先实证全部规则 → 把方案甲修到极限 → 看剩下什么改不动
→ 那个不可达集合才是评估方案乙的依据。**当前处在第二阶段，不做方案乙的选型决策。**
方案甲的实施记录（差异清单、修复、验收）归在 §3.1 之下，不与方案本身并列。

---

# 第一部分　背景与任务

## 1.1 这份文档是什么

它描述**官方 Codex CLI 用 OAuth 凭据向 OpenAI 请求时，在线上产生的可观测形态**，
以及 Sub2API 对每一项的对齐状态。**官方形态以本表为唯一事实源。**

它**不是**修复记录。修复过程见
[官方出站 wire 一致性修复清单](OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md)；
本表回答"官方是什么样"，那份文档回答"我们改了什么"。

## 1.2 为什么要有这份表

此前的工作方式是「抓包对比 → 改代码 → 抓包验证」，判断反复反转。复盘 2026-07-27
当天的 16 次反转，成因分布是：

| 成因 | 占比 | 典型 |
|---|---|---|
| 抓包通道自身有掩盖 | 3 | MITM 必经代理走 h2，HPACK 抹平 header 大小写与顺序 |
| 脚手架预置掩盖差异 | 2 | 预置模型清单掩盖第三方 Lite 失效 |
| 读源码读错层 | 1 | 把单元测试的 mock base 当成生产 URL |
| 读源码读漏调用点 | 1 | 误判 images 端点无人调用（实际在 `ext/image-generation`） |
| 常量语义误读 | 1 | 把 `defaultMaxReadFrameSize` 的**上限**当默认值 |
| 只读一层未及依赖库 | 2 | 读 hyper 得出"官方全小写"，对 HTTP 对、对 WS 错，**引入了生产回归** |
| **拿一处结论外推到别处** | 3 | 把 WS 握手序当作 `/responses` 的兜底（实测完全不同）；把 h1 顺序做成跨端点的并集清单（models 当场就错）；把 images 与 alpha-search 的 web-search 分支误判为 accept 不对齐（它们打的其实是 responses 端点） |
| **只看调用点不看调用链** | 2 | 误判 live 主请求缺 `openai-alpha`（实为经 `applyLiveUpstreamIdentityHeaders` 设置）；误判 `supportsOfficialEgressHTTPProfile` 是画像失效的根因（它管的是 Finalizer，TLS 画像走显式传参） |

结论有三条。其一，**"以源码为准"并不能解决问题**——中间四类全是源码推断出的错，
且比抓包错得更隐蔽（会一路自洽到部署）。其二，真正缺的是**规格这一层**：过去是
「抓包结论」直接对「代码实现」，中间没有可审计的东西，每次判断都活在对话里。
其三，**最后两类（外推与半截调用链）是当天后半段才暴露的**，且都发生在"已经建立
规格表之后"——说明规格表本身也会写错，**每条都必须落到实测**，这正是本表要求
每条声明观测通道、并把状态与实测运行号绑定的原因。

因此本表的设计原则是：**抓包与源码交叉验证，且每条规则都必须声明"用什么通道才能
观测到它"**——因为最贵的教训是，用看不到该项的通道去验，会得到虚假的通过。

## 1.3 任务定义

用户确定的五步流程，目标是让 Sub2API 的 OpenAI OAuth 出站与官方 Codex CLI 一致：

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | 列规则：读官方源码 + 抓包，列出全部端点形态 | **进行中** |
| 2 | 验规则：逐条抓包确认，标「验过了/⛔验不了」 | 未开始 |
| 3 | 对比：逐条对 Sub2API，产出差异清单 | 未开始 |
| 4 | 改代码：按差异清单改，含**自写 h1 wire**，部署 Vircs | 未开始 |
| 5 | 再抓包验收 | 未开始 |

## 1.4 已定的决策（不要重新讨论）

1. **范围**：七个端点全做，**只做 OpenAI**，不碰 Anthropic。
2. **暂不换 Rust**。同源方案已 PoC 验证可行（§3.2），但本轮用现有 Go 改。
3. **自写 h1 wire 要做**，解 SPEC-H1-002/003/004 + SPEC-WS-002。**带画像开关**，
   只对官方 OpenAI 画像启用，出问题可关开关回退。实际规模约 **500~800 行**
   （不止 header 输出——`http.Transport` 的写入无 hook，连接池必须一并自管；
   每请求新建连接会制造新偏离，因为官方是 keep-alive 复用）。
4. **不 fork `x/net/http2`**。h2 三项仅影响代理路径，而**实测生产 97 个账号全部
   直连、0 个绑代理**。且 `go.mod` 是上游 3 个月改了 7 次的高频文件，加 replace
   会持续冲突。h2 条目标"须 fork，暂不做"。
5. **合并上游的冲突面已查**：伪装逻辑几乎全在 fork 独有文件
   （`official_egress_*.go`、`tlsfingerprint/h2.go`、`lowercase_headers.go`），
   共享文件只有 `dialer.go`（上游 3 月**0 次**改动）与 `http_upstream.go`（2 次）。
   **自写 h1 wire 应放进 fork 独有的新包**，只在 `official_egress_*` 里引用，
   `http_upstream.go` 保持现有的一行包装，合并成本近零。

### 1.4.1 推进顺序：先穷尽方案甲，再评估方案乙

**方案乙是理想的未来方案，但不是现在要做的事。** 既定顺序：

1. **读源码 + 抓包实证所有规则**（第 1、2 步）——把"官方是什么样"钉死
2. **把方案甲修到极限**——逐条改、逐条 wire 验收，直到改不动为止
3. **看剩下什么改不动，边界就在哪里**——这个不可达集合才是决定是否需要方案乙的
   唯一依据
4. **最后才评估方案乙的可行性**

**为什么不先比**：现在比只能比"预估"。方案甲的真实边界必须由实测划出——本轮已两次
证明预估靠不住（首版并集清单让 models 顺序当场就错；A6 从"需修"翻转为"不修"）。
在边界探明前做架构决策，是拿猜测换架构复杂度。

**判据**：若穷尽方案甲后残留的偏离**少到不值得换架构**，方案乙就不必做；若残留的
恰是**高辨识度的强特征**，才值得付那笔架构成本。

## 1.5 工作方法

### 1.5.1 证据等级

| 等级 | 含义 | 风险 |
|---|---|---|
| **L1** | 官方生产代码直读 | 最强，但要确认是生产路径而非测试 |
| **L2** | 依赖库行为推断（hyper/tungstenite/h2 的默认行为） | 官方源码里看不到，易整层漏掉 |
| **L3** | 实测为唯一依据（源码给不出答案） | 受样本覆盖度限制 |
| **L4** | 单元测试 / mock 数据 | **仅参考**，不得作为断言依据 |

L4 单列是因为踩过：`images.rs` 的测试里 `base_url: "https://example.com/api/codex"`
是 mock，曾被当作真实 URL 写进结论。

L3 是合法等级，不是缺陷：`h2` crate 的 `Settings` 全为 `Option<u32>`，crate 本身
不定义客户端默认发送列表，官方实际发什么值**源码里推不出来**，只能实测。

### 1.5.2 可变性

- **固定**：每次请求都一样
- **随机**：官方每次都不同（复刻时必须同样随机，钉死单一样本反而是负指纹）
- **条件**：随场景变化（Lite/非 Lite、有无 attestation、直连/代理）

### 1.5.3 本版范围

**OpenAI OAuth 出站、responses 端点（HTTP + WS）**，覆盖 TLS / 协议协商 / wire /
header / body 五层。models、compact、旁路端点与 Anthropic 侧后续按同格式扩。

---

### 1.5.4 工具与环境

| 工具 | 用途 | 位置 |
|---|---|---|
| `h1_wire_probe.py` | h1 线形（header 大小写/顺序）**MITM 看不到这层** | `tools/official_client_capture/` |
| `h2_wire_probe.py` | h2 帧层（CONNECT 代理 + h2 服务端）**mitmproxy 看不到这层** | 同上 |
| `run_official_h1_baseline.sh` | 采官方 h1 基线 | 同上 |
| `run_official_h2_baseline.sh` | 采官方 h2（**会触发 rustls 分支，形态被污染**） | 同上 |
| `run_official_nativetls_baseline.sh` | 采官方 **native-tls 默认分支**（正确的采法） | 同上 |

**采集要点**：设 `CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE` 会触发官方
`custom_ca.rs:307` 的 `use_rustls_tls()`，采到的是 rustls 形态。要观测默认行为，
必须**把 CA 装进容器系统信任库**而非用环境变量。

**逼官方降级到 HTTP** 的正确方法：让 WS **连接被拒**（`Connection refused`），
而非回 HTTP 400。官方日志会打 `Falling back from WebSockets to HTTPS transport`。

**证据归档**：`local-analysis/captures/wire-parity-fix-20260727/`
（`h1-wire-probe/`、`h2-wire-probe/`、`rust-poc/`；已 gitignore，不入库）。

**Vircs 部署**：rsync 源码 → 远程 `docker build` → 改
`/root/oauth-capture/runtime/sub2apiplus-t2.override.yml` 的 image tag →
`docker compose ... up -d sub2api`。当前运行
`sub2apiplus:h2-settings-0.1.165-10-20260727T133353Z`。

### 1.5.5 相关文档分工

| 文档 | 记什么 |
|---|---|
| **本表** | 官方形态是什么样 + 对齐状态（**唯一事实源**） |
| `OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md` | 我们改了什么（§1.5.1 有已修/待修总表） |
| `README.md` §1.1 | 对外的现状与残留差异（§1.1.1.2） |
| `OFFICIAL_EGRESS_PROFILE_FIDELITY_FIX_20260727.md` | 第一轮修复记录 |

---

# 第二部分　规则

共 **52 条**（46 个独立条目，另有两组以表格合并计数）。每条声明规则、依据与证据
等级、观测通道、实测运行、可变性、对齐状态。

## 2.1 TLS 层

### SPEC-TLS-001　直连 ClientHello

- **规则**：30 个 cipher suite，**ALPN 为空**（不 offer 任何协议）
- **依据**：`newOpenAIOfficialEgressHTTPTLSProfile` 复刻自官方 pcap　[L3]
- **观测**：direct pcap ✅　／　MITM ❌（代理会替换 ClientHello）
- **实测**：direct pcap 三个 ClientHello 全部 30-cipher + 空 ALPN
- **可变性**：固定
- **状态**：✅ 已对齐

### SPEC-TLS-002　经代理 ClientHello

- **规则**：**取决于是否配置了自定义 CA**——
  ① 默认（native-tls）：**ALPN 为空**，同直连
  ② 配了 `CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE`（rustls）：10 cipher，`ALPN = h2, http/1.1`
- **依据**：`custom_ca.rs:398` `configured_ca_bundle()` 读到这两个变量之一即
  `use_rustls_tls()`（`custom_ca.rs:307`）　[L1]
- **观测**：CONNECT 隧道内的 TLS 握手
- **实测**：`official-nativetls-20260727T161531Z`——**不设 CA 变量、改把抓包 CA 装入
  系统信任库**后，官方经代理的 6 个连接 **全部 `negotiated_alpn = None`**
- **可变性**：**条件**（是否配置自定义 CA）
- **状态**：🔴 **未对齐，且方向相反** —— `newOpenAIOfficialEgressHTTPProxyTLSProfile`
  声明 `ALPN = h2, http/1.1`，复刻的是**官方在配了自定义 CA 时**的行为。而官方**默认
  经代理不 offer h2**。
- **⚠ 该画像本身很可能是观测污染的产物**：要 MITM 抓包就必须让官方信任 MITM 证书，
  而那正好触发 rustls 分支——于是采到的"官方经代理形态"实为 rustls 形态。**整个
  代理画像的 10-cipher + h2 ALPN 都建立在这个被污染的观测上。**

### SPEC-TLS-003　WS 握手 ClientHello 的扩展顺序

- **规则**：扩展集合固定，但**每次握手随机排序**
- **依据**：`newOpenAIOfficialEgressWebSocketTLSProfile` 注释：对应四个目标握手的
  不同 JA3，**禁止固定单一样本**　[L3]
- **观测**：direct pcap
- **可变性**：**随机** —— 复刻时必须同样随机化，钉死一个样本即成负指纹
- **状态**：✅ 已对齐

---


## 2.2 协议协商层

### SPEC-PROTO-001　直连恒为 HTTP/1.1

- **规则**：因 ALPN 为空（SPEC-TLS-001），服务端不协商，落到 HTTP/1.1
- **依据**：h1 探针实测官方 `negotiated_alpn = None`　[L3]
- **观测**：h1 直连探针
- **可变性**：固定
- **状态**：✅ 已对齐（`ProfileNegotiatesH2` 判定为假）
- **⚠ 连带影响**：**这是全表最重要的一条**。它意味着 h1 wire 形态（§2.3）作用于
  **默认主路径**，而 h2 帧层（§2.4）**仅在经代理时**才可见。

### SPEC-PROTO-002　responses 默认走 WebSocket，HTTP 是降级路径

- **规则**：官方默认 WS；HTTP 仅在 `force_http_fallback` 后启用
- **依据**：`core/src/client.rs:509` `force_http_fallback`，
  `client.rs:930` `responses_websocket_enabled`　[L1]
- **观测**：h1/h2 探针
- **实测**：探针拒绝 WS 升级（回 400）后官方仍走错误退出，**未观察到降级**
- **可变性**：条件
- **状态**：✅ **降级路径的基线已采到**（`official-httpfb3-20260727T234853Z`）
- **采法**（此前两次失败，记录以免重走弯路）：降级发生在**耗尽重试预算之后**
  （`client.rs:1849` 注释）。让探针对 WS 握手回 HTTP 400 → 官方走错误退出，不降级；
  改为**直接断开连接**才会重试，但采集上限设小了会在降级前提前退出。
  正确做法：断连 + 采集上限放到 40，让它把重试跑完，官方即打印
  `Falling back from WebSockets to HTTPS transport`，随后的 POST 即可采到。

---


## 2.3 HTTP/1.1 wire 层

> 本层全部条目**只能用 h1 直连探针观测**。MITM 一律不可见——经代理即 h2，
> HPACK 强制小写并重排。用 MITM 验本层等于自欺。

### SPEC-H1-001　header 名全小写

- **规则**：所有 header 名小写输出，**含 `host`**
- **依据**：hyper `src/proto/h1/role.rs` 默认分支 `write_headers`，
  `extend(dst, name.as_str().as_bytes())`；`http::HeaderName` 存储即小写　[L2]
  ─ hyper 另有 `HeaderCaseMap` 与 `title_case_headers` 两条保留大小写的分支，
  **官方未启用**，故小写是默认路径而非协议要求
- **观测**：h1 直连探针 ✅　／　MITM ❌
- **实测**：`official-h1-full-20260727T124125Z`，官方普通 HTTP 请求全小写
- **可变性**：固定
- **状态**：✅ 已对齐（`lowercaseHeaderRoundTripper`；实测 17 头中 15 项小写，
  `Host`/`Content-Length` 见 SPEC-H1-002/003）

### SPEC-H1-002　`host` 位置在用户头之后

- **规则**：`host` 排在所有用户 header **之后**（无 body 时为最后一项）
- **依据**：hyper 的 h1 编码器**对 `Host` 不做任何特殊处理**，它随 `HeaderMap`
  迭代序输出；`host` 由 reqwest 在用户头之后才插入　[L2]
- **观测**：h1 直连探针
- **实测**：`GET /codex/models` → `version, authorization, chatgpt-account-id,
  accept, originator, user-agent, host`
- **可变性**：固定
- **状态**：✅ **已对齐**（`h1_wire.go`，`h1-wire-v2-0.1.165-13`）——
  在 `net.Conn` 层拦截重写请求头字节。实测 `host` 已为小写且位于末尾。

### SPEC-H1-003　`content-length` 排在 `host` 之后

- **规则**：有 body 时 `content-length` 为**最后一项**，排在 `host` 后
- **依据**：由 hyper `set_length` 在 encode 之前塞入 `HeaderMap`，是最后插入项　[L2]
- **观测**：h1 直连探针
- **实测**：`POST /backend-api/ps/mcp` → `..., content-type, host, content-length`
- **可变性**：固定
- **状态**：✅ **已对齐** —— 实测 responses 请求为 `…, host, content-length`。

### SPEC-H1-004　其余 header 按插入序而非字典序

- **规则**：用户 header 按 `HeaderMap` 迭代序输出
- **依据**：`http::HeaderMap::iter()` 遍历 `entries` 数组，新 key `push` 到末尾、
  重复 insert 不改位置　[L2]
  ─ **但迭代序不等价于插入序**：`remove` 用 `swap_remove`
  （`map.rs: self.entries.swap_remove(found)`），会把末位元素换到被删位置
- **观测**：h1 直连探针
- **实测**：两个端点共有 header 的相对顺序不同（`originator` 在 models 里第 5、
  在 ps/mcp 里第 1），**证明是插入序而非哈希序**
- **可变性**：**条件** —— 不同端点构造路径不同，顺序不同。官方基础头
  `default_headers()` 的插入序是 `originator` → `user-agent` →（条件）`residency`
  （`login/src/auth/default_client.rs:354`　[L1]），但各端点会在其前后插入自己的头
- **状态**：✅ **models 与 responses 均已逐字节对齐**，其余端点为最近似值。
  ─ 顺序清单**必须按端点分派**：首版用一份并集清单，部署后抓包实测发现 models
  顺序当场就错（官方 `version` 打头，我们输出 `chatgpt-account-id` 打头）。
  已改为 `H1HeaderOrderRule` 按请求路径匹配。
  ─ HTTP POST `/responses` 的基线已补齐（见 SPEC-PROTO-002 的采法），实测次序为
  `version, x-codex-beta-features, x-codex-window-id, x-codex-turn-metadata,
  x-openai-internal-codex-responses-lite, x-client-request-id, session-id, thread-id,
  accept, content-encoding, content-type, authorization, chatgpt-account-id,
  originator, user-agent, host, content-length`——**与 WS 握手序完全不同**，
  此前拿 WS 序作兜底是错的，现已按路径单列。

---


## 2.4 HTTP/2 帧层

> ### ⚠ 本层基线存在观测污染，先读这段
>
> 采集官方 h2 基线时用了 `CODEX_CA_CERTIFICATE` 让官方信任探针证书。而官方
> `http-client/src/custom_ca.rs:307` 在**检测到自定义 CA 时会调用
> `builder.use_rustls_tls()`**——即从默认的 native-tls **切换到 rustls**。
>
> 也就是说，**本层记录的"官方值"实际是官方在 rustls 分支下的值**，不是它默认
> （native-tls）的行为。这是典型的观测污染：观测手段改变了被观测对象。
>
> 该形态仍是真实的官方形态之一（企业代理 + 自定义 CA 的用户就走这条），但
> **可变性是"条件"而非"固定"**，条件是「是否配置了自定义 CA」。
>
> **已补测（`official-nativetls-20260727T161531Z`）**：不设 CA 变量、改把抓包 CA 装入
> 系统信任库后，官方经代理的 6 个连接**全部 `negotiated_alpn = None`**——即
> **官方默认根本不产生 h2 流量**。因此本层全部条目仅在「配置了自定义 CA」这一条件
> 下成立，对官方默认行为**不适用**。详见 SPEC-TLS-002。
>
> **仅在经代理时可见**（SPEC-PROTO-001）。且 **mitmproxy 看不到本层**——它用自己的
> h2 栈重建连接，客户端原始 SETTINGS 的集合、取值与帧内顺序在转发后已丢失。
> 必须用 CONNECT 代理式 h2 探针。

### SPEC-H2-001　SETTINGS 帧内顺序

- **规则**：`ENABLE_PUSH, INITIAL_WINDOW_SIZE, MAX_FRAME_SIZE, MAX_HEADER_LIST_SIZE`
- **依据**：`h2` crate 的 `Settings` 全为 `Option<u32>`，**crate 不定义客户端默认
  发送列表**，实际发什么由上层决定 → 源码给不出答案　[L3]
- **观测**：h2 探针（CONNECT 代理 + h2 服务端）
- **实测**：`official-h2-20260727T131936Z`，三次连接一致
- **可变性**：固定
- **状态**：✅ **本就一致** —— Go 的 `initialSettings` 顺序相同
  （`x/net/http2/transport.go:526`）

### SPEC-H2-002 ~ 005　SETTINGS 取值

| 编号 | 参数 | 官方值 | Go 默认 | 状态 |
|---|---|---|---|---|
| SPEC-H2-002 | `ENABLE_PUSH` | 0 | 0 | ✅ 本就一致 |
| SPEC-H2-003 | `INITIAL_WINDOW_SIZE` | 2,097,152 | 4,194,304 | ❌ 未对齐 |
| SPEC-H2-004 | `MAX_FRAME_SIZE` | 16,384 | 16,384 | ✅ 本就一致 |
| SPEC-H2-005 | `MAX_HEADER_LIST_SIZE` | 16,384 | 10,485,760 | ✅ **已对齐** |

- **依据**：全部 [L3]（同 SPEC-H2-001，源码无答案）
- **可变性**：固定
- **SPEC-H2-004 备注**：曾误判为偏离——`defaultMaxReadFrameSize = 1 << 20` 是**上限
  不是默认值**，实测两侧同为 16,384。此条是"常量语义误读"的典型。
- **SPEC-H2-003 状态说明**：取自 `conf.MaxUploadBufferPerStream`，**可经
  `http.Transport.HTTP2` 配置**，但那要求 transport 由 `ConfigureTransports(t1)`
  创建——而这样创建的 transport 用 `noDialClientConnPool`，不能独立拨号；改走
  `t1.RoundTrip` 则 `dialConn` 会把连接断言为 `*tls.Conn`，utls 的 `UConn` 过不去。
  **是 utls 与 net/http 的 h2 升级路径不兼容卡住的，不是配置项缺失。**

### SPEC-H2-006　首个 WINDOW_UPDATE 增量

- **规则**：`5,177,345`（stream 0）
- **依据**：[L3]
- **实测**：三次连接一致
- **可变性**：固定
- **状态**：❌ 未对齐（Go 为 1,073,741,824）。原因同 SPEC-H2-003。

### SPEC-H2-007　伪头顺序

- **规则**：`:method, :scheme, :authority, :path`
- **依据**：[L3] 实测；Go 侧对照为 `internal/httpcommon/request.go:138` 硬编码
  `:authority, :method, :path, :scheme`　[L1]
- **观测**：h2 探针（需 HPACK 解码）
- **可变性**：固定
- **状态**：❌ 未对齐，**无配置入口**，须 fork `x/net/http2`

---


## 2.5 WebSocket 握手层

### SPEC-WS-001　前 5 项为大写驼峰

- **规则**：`Host, Connection, Upgrade, Sec-WebSocket-Version, Sec-WebSocket-Key`
  按此**固定顺序**、**大写驼峰**写在最前
- **依据**：tungstenite 硬编码
  `WEBSOCKET_HEADERS = ["Host","Connection","Upgrade","Sec-WebSocket-Version",KEY_HEADERNAME]`，
  先按该数组序写出，之后才写剩余头　[L2]
- **观测**：h1 直连探针（WS 握手是 h1 请求）
- **实测**：`official-h1-full-20260727T124125Z`
- **可变性**：固定
- **状态**：✅ 已对齐（`PreserveHeaderCase`）
- **⚠ 教训**：本项曾因"官方全小写"的错误推断被压成小写，把 `Connection`/`Upgrade`
  从**本已一致**改成偏离。**官方并非处处小写**——普通 HTTP 走 hyper 全小写，
  WS 握手走 tungstenite 前 5 项大写。一刀切会制造新偏离。

### SPEC-WS-002　剩余 header 小写且顺序受 swap_remove 扰动

- **规则**：前 5 项之后的 header 小写输出；其顺序是 `HeaderMap` 被逐个 `remove`
  之后的**扰动结果**，非原始插入序
- **依据**：tungstenite 对那 5 个必需头逐个 `headers.remove(header)` 取值，
  每次 `remove` 都触发 `swap_remove` 扰动剩余顺序　[L2]
- **观测**：h1 直连探针
- **实测**：`chatgpt-account-id, authorization, user-agent, originator, openai-beta,
  version, x-codex-beta-features, x-client-request-id, session-id, thread-id,
  x-codex-window-id, x-codex-turn-metadata, sec-websocket-extensions`
- **可变性**：**条件** —— ⚠ 因 swap_remove，握手头**缺项时剩余顺序可能整体重排**，
  **不能按清单跳过缺失项**。HTTP 路径无 remove，跳过是安全的。
- **状态**：⚠ 大小写已对齐，**顺序未对齐**（Go 为字典序）。须自写 wire，与
  SPEC-H1-002 同一工程。

### SPEC-WS-003　两个特例改写

- **规则**：剩余头中 `sec-websocket-protocol` → `Sec-WebSocket-Protocol`，
  `origin` → `Origin`
- **依据**：tungstenite 显式判断改写　[L2]
- **可变性**：条件（官方当前不发这两个头）
- **状态**：⚠ 未覆盖 —— 当前无影响，但复刻实现须一并处理，否则将来一旦发出即露馅

---


## 2.6 header 语义层

### SPEC-HDR-001　组装顺序

- **规则**：`provider.build_request`（基础头）→ `extend(extra_headers)`
  → `apply_auth`（最后）
- **依据**：`codex-api/src/endpoint/session.rs:48` `make_request`　[L1]
- **可变性**：固定
- **状态**：ℹ️ 语义参考项——决定 SPEC-H1-004 的顺序，本身无需对齐

### SPEC-HDR-002　基础头集合

- **规则**：`originator` → `user-agent` →（条件）`residency`
- **依据**：`login/src/auth/default_client.rs:354` `default_headers()`　[L1]
- **可变性**：条件（`residency` 仅当 `REQUIREMENTS_RESIDENCY` 已设置）
- **状态**：✅ 已对齐

### SPEC-HDR-005　`user-agent` 的 suffix 语义

- **规则**：`UA = prefix + suffix`
  - `prefix = "{originator}/{version} ({os_type} {os_version}; {arch}) {terminal}"`
  - `terminal` 由终端检测得出；**完全无终端时为 `unknown`**
    （`terminal-detection/src/lib.rs:204` `TerminalName::Unknown => "unknown"`）
  - `suffix` 仅当 `USER_AGENT_SUFFIX` 非空时追加，格式 `" ({name}; {version})"`
- **依据**：`login/src/auth/default_client.rs:161` `get_codex_user_agent()`　[L1]
  ─ `USER_AGENT_SUFFIX` 全仓**只有两个设置点**：`mcp-server/src/message_processor.rs:230`
  与 `app-server/src/request_processors/initialize_processor.rs:133`，且值为
  `format!("{name}; {version})")`，其中 name/version 是**连接进来的第三方客户端**
  的名字与版本（`initialize_processor.rs:90`）　[L1]
- **观测**：任意通道
- **实测**：官方 `codex exec` 的 UA 为
  `codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) xterm-256color`——**无 suffix**
- **可变性**：条件（CLI 无 suffix；app-server/mcp-server 场景才有）
- **状态**：🔴 **未对齐** —— Sub2API 硬编码
  `codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)`
  （`internal/pkg/openaiidentity/codex.go:7`）。
  - 中间的 `unknown` **是对的**：服务端无终端，官方在该条件下同样输出 `unknown`
  - 但尾部 `(codex_exec; 0.145.0)` 是**官方永不产生的组合**：普通 CLI 不带 suffix；
    app-server 场景下 suffix 是第三方客户端的 name/version，而 name 等于 originator
    本身既不合语义、还会被 `NON_ORIGINATING_CLIENT_NAMES` 过滤
  - **该常量为全局共用，修一处则所有端点受益**

### SPEC-HDR-006　`accept`：仅 responses 显式设，其余一律 reqwest 默认 `*/*`

- **规则**：
  - **responses**：显式 `text/event-stream`（`endpoint/responses.rs:149`
    `req.headers.insert(ACCEPT, HeaderValue::from_static("text/event-stream"))`）　[L1]
  - **其余端点**（models / compact / alpha-search / images）：端点层**不设**
    accept，由 reqwest 补默认值 **`*/*`**　[L1 + L3]
- **观测**：任意通道
- **实测**：`official-h1-full-20260727T124125Z` 中 accept 只有两种取值——
  `*/*`（models、plugins/featured、ps/plugins/installed、ps/plugins/list）
  与 `text/event-stream, application/json`（ps/mcp 的 SSE 端点）
- **可变性**：条件（按端点）
- **状态**：🔴 **四个端点全部未对齐**。根因是 Sub2API 按"该端点返回什么"来设
  accept，而官方是"除 responses 外一律交给 reqwest 默认"：

| 请求 | 实际打的端点 | 官方 | Sub2API | 状态 |
|---|---|---|---|---|
| responses | responses | `text/event-stream` | `text/event-stream` | ✅ |
| compact | responses/compact | `*/*` | 原为**无 accept 头** | ✅ **已修** |
| models | models | `*/*` | 原为 `application/json` | ✅ **已修** |
| alpha-search 主请求 | alpha/search | `*/*` | 原为 `application/json` | ✅ **已修** |
| alpha-search web-search | **responses** | `text/event-stream` | `text/event-stream` | ✅ 本就一致 |
| images | **responses** | `text/event-stream` | `text/event-stream` | ✅ 本就一致 |

> **本表曾写错两行**（初版把 images 与 alpha-search 的 web-search 分支标为未对齐）。
> 二者打的都是 **responses 端点**而非各自的同名端点：images 走
> `image_generation` 工具调用（SPEC-EP-001），alpha-search 的
> `buildOpenAIAlphaSearchResponsesWebSearchRequest` 也打 responses。
> 因此它们的 `text/event-stream` 本就正确。**实际需修的是 3 处而非 4 处。**

  ─ compact 那条最明显：**官方一定带 accept，而 Sub2API 原本完全没有**
  （`header.Del("Accept")`，而 Go 不像 reqwest 会自动补默认值）。

### SPEC-HDR-007　会话头一律用连字符，且无 `conversation-id`

- **规则**：官方会话头只有两个，**均为连字符小写**：`session-id`、`thread-id`
- **依据**：`codex-api/src/requests/headers.rs:8,11` `build_session_headers()`
  `insert_header(&mut headers, "session-id"/"thread-id", …)`　[L1]
  ─ 下划线形式的 `session_id` 在官方仅出现于 **JSON body 的 key**
  （`core/src/responses_metadata.rs:27`）与工具参数，**header 层绝无**　[L1 反证]
  ─ 官方**没有** `conversation-id` / `conversation_id` 这个 header（`thread-id`
  是它的对应物）
- **观测**：任意通道
- **可变性**：固定
- **状态**：🔴 **未对齐** —— `openai_alpha_search.go:255-256` 设
  `Session_ID` 与 `Conversation_ID`（下划线）。两处都错：形式用了下划线，
  且 `Conversation_ID` 是官方不存在的 header。
  ─ 该文件经 `enforceCodexIdentityHeaders` 最终把 UA 设为
  `codexCLIUserAgent = openaiidentity.CodexUserAgent`（`openai_gateway_service.go:38`），
  即同一个带错误 suffix 的常量，**印证 SPEC-HDR-005 的全局性**

### SPEC-HDR-003　不发 `accept-language`

- **规则**：官方**从不**发送 `accept-language`
- **依据**：`default_headers()` 与各端点构造均无该头　[L1 反证]
- **观测**：任意通道（MITM 亦可见）
- **可变性**：固定
- **状态**：✅ 已对齐（`stripOfficialEgressInboundHostHeaders` 剥离入站宿主头）

### SPEC-HDR-004　`OpenAI-Beta` 仅用于 WS 握手

- **规则**：`OPENAI_BETA_HEADER` 唯一实际使用点是 WS 握手，值为
  `responses_websockets=2026-02-06`。官方 images 端点与 HTTP Responses **都不发**
- **依据**：`core/src/client.rs`　[L1]
- **可变性**：条件
- **状态**：✅ 已对齐（images 路径已删除该头）

---


## 2.7 body 层

### SPEC-BODY-001　顶层字段为固定结构体

- **规则**：官方 body 由 Rust 结构体 `ResponsesApiRequest` 序列化，
  **不可能携带结构体外的字段**
- **依据**：`codex-api/src/common.rs`　[L1]
- **观测**：任意通道
- **实测**：第三方传入的 `truncation`/`top_logprobs`/`background`/`max_tool_calls`
  及自造字段全部剔除
- **可变性**：固定
- **状态**：✅ 已对齐（顶层改白名单）

### SPEC-BODY-005　`tool_choice` 必须是 JSON 字符串，不能是对象

- **规则**：`tool_choice` 的类型是 **`String`**，实际取值为 `"auto"`
- **依据**：`codex-api/src/common.rs:223` `pub tool_choice: String`　[L1]
  ─ 取值实测于 `endpoint/responses_websocket.rs:922` `tool_choice: "auto".to_string()`
  及 `core/src/client_common_tests.rs` 多处　[L1]
- **观测**：h1 探针（**须解 zstd**，官方 responses 的 body 是压缩的）
- **实测**：`official-body2-20260728T000549Z` → `tool_choice = str:auto`，
  **确证为 JSON 字符串**
- **可变性**：固定
- **状态**：🔴 **类型层面的偏离，强负指纹** —— `openai_images_responses.go:357`
  的硬编码模板发的是**对象**：
  ```json
  "tool_choice":{"type":"image_generation"}
  ```
  官方的 Rust 结构体 `pub tool_choice: String` **在类型层面就不可能序列化出对象**。
  检测方只需判断该字段的 JSON 类型即可区分，无需比对取值。
- **⚠ 与 SPEC-EP-001 的关系**：官方确实也走 responses + `image_generation` 工具
  （故端点选择本身不算偏离），但官方是**让模型自主决定**是否调用该工具，
  `tool_choice` 保持 `"auto"`；Sub2API 改为强制指定工具，于是被迫用了对象形式。
  语义上可理解（用户明确要生图），形态上是官方不可能产生的。

### SPEC-BODY-006　images 硬编码模板的其余字段

- **规则**：官方 body 由 `ResponsesApiRequest` 序列化，字段集合固定
  （`common.rs:217-238`：`model / instructions / input / tools / tool_choice /
  parallel_tool_calls / reasoning / store / stream / stream_options / include /
  service_tier / prompt_cache_key / text / client_metadata`）　[L1]
- **状态**：⚠ 待第 2 步实测 —— Sub2API 的硬编码模板
  （`openai_images_responses.go:357`）为
  `instructions / stream / reasoning / parallel_tool_calls / include / model /
  store / tool_choice`，字段名均在官方集合内，但取值需逐项比：
  - `parallel_tool_calls` 硬编码 `true`，而 **Lite 模式下官方应为 `false`**
    （SPEC-BODY-003），该模板不区分 Lite
  - `reasoning.effort` 硬编码 `medium`、`summary` 硬编码 `auto`，是否随场景变化待验
  - **缺 `input`**：官方 `pub input: Vec<ResponseItem>` 非 Option，一定存在

### SPEC-BODY-002　压缩策略

- **规则**：普通 Responses 用 zstd；**compact 请求不压缩**
- **依据**：`RequestCompression` 的 `#[default]` 为 `None`
  （`http-client/src/request.rs`），compact 的 `execute_with` 不设压缩　[L1]
- **可变性**：条件
- **状态**：✅ 已对齐

### SPEC-BODY-003　Lite 模式变换

- **规则**：`use_responses_lite` 为真时 instructions/tools 迁入 `input`、
  `reasoning.context = all_turns`、`parallel_tool_calls = false`
- **依据**：官方 Lite 路径　[L1]；由 `/backend-api/codex/models` manifest 驱动
- **可变性**：**条件** —— 随 manifest 变化，且 manifest 有 5 分钟 TTL
- **状态**：✅ 已对齐
- **⚠ 观测陷阱**：抓包脚手架曾**预置模型清单**，掩盖了第三方客户端 Lite 判定失效。
  验证本项必须确保脚手架不预置 manifest。

### SPEC-BODY-004　turn-state 来源

- **规则**：从**流内 `response.metadata` 事件的 `headers` 对象**提取
  （大小写不敏感）；握手响应头那条路在官方 CLI 里是**死代码**（core 传 `None`）
- **依据**：`codex-api/src/sse/responses.rs` 的 `turn_state()` 与
  `header_turn_state_value_from_json`　[L1]
- **观测**：SSE / WS 事件流
- **可变性**：条件
- **状态**：⚠ 已对齐但**无 wire 证据** —— 上游在历次采集中从未下发该事件

---


## 2.8 端点选择

### SPEC-EP-001　images 有独立端点，但 responses + 工具调用也是官方路径

- **规则**：官方**两条路都走**——
  ① `POST https://chatgpt.com/backend-api/codex/images/generations`
     （`ext/image-generation`，编译进主 CLI，`core/Cargo.toml:141`）　[L1]
  ② responses + `image_generation` hosted tool
     （识别于 `rollout-trace/src/tool_dispatch.rs:267`，响应项处理于
     `reducer/conversation/normalize.rs:116`）　[L1]
- **观测**：真实请求
- **实测**：带 OAuth token POST 空 body 到 ① 返回 `400 Missing required parameter:
  'prompt'` —— 认证通过、端点存在、路由正确
- **可变性**：条件
- **状态**：ℹ️ Sub2API 走 ②。**因官方自己也走 ②，不构成"非官方"指纹特征**，
  仅入口映射不同（属产品选择）。
- **⚠ 教训**：该端点 URL 曾被写成 `{base}/api/codex/images/generations` —— 那是
  单元测试的 mock base　[L4]，生产走 `api_provider()`，与 responses 同 base。

---


## 2.9 端点全集

官方端点定义（`codex-api/src/endpoint/*.rs` 的 `fn path()`，base 为
`https://chatgpt.com/backend-api/codex`）　[L1]：

| 端点 | 方法 | path | 官方源文件 |
|---|---|---|---|
| responses | POST | `responses` | `responses.rs` |
| responses（WS） | GET+Upgrade | `responses` | `responses_websocket.rs` |
| compact | POST | `responses/compact` | `compact.rs` |
| models | GET | `models`（带 `client_version` query） | `models.rs` |
| alpha/search | POST | `alpha/search` | `search.rs` |
| live | POST | `realtime/calls`（另有 `live`，FramelessBidi） | `realtime_call.rs` |
| images | POST | `images/generations`·`images/edits` | `images.rs` |

### SPEC-EP-002　官方 OAuth 只访问 chatgpt.com

- **规则**：OAuth 凭据下**所有**出站都打 `chatgpt.com/backend-api/*`，
  **从不访问 `api.openai.com`**
- **依据**：官方全部端点的 base 均来自同一 provider　[L1]
- **可变性**：固定
- **状态**：⚠ 部分偏离 —— Sub2API 的 `count_tokens` 打
  `api.openai.com/v1/responses/input_tokens`（P1-5 已收敛画像与身份头，但**域名本身
  仍是偏离**）

### SPEC-EP-003　官方无 count_tokens 端点

- **规则**：官方**没有**任何 token 计数端点。`input_tokens` 仅作为 images 响应体内
  的**字段**出现（`images.rs:157`），不是端点
- **依据**：全仓检索无对应 `fn path()`　[L1 反证]
- **可变性**：固定
- **状态**：🔴 **无官方对应物** —— Sub2API 的 `/v1/messages/count_tokens` 是为兼容
  Anthropic 接口而造，官方形态里不存在这条链路，**无法"对齐"，只能评估是否关闭或
  改走本地估算**

### SPEC-EP-004　出站面比预期宽：六个额外端点

第 1 步扫描 Sub2API 全部出站 URL 时发现，除上表七个端点外还有六条打 `chatgpt.com`
的链路。逐条核对官方是否有对应物：

| Sub2API 出站 | 实现文件 | 官方是否有 |
|---|---|---|
| `backend-api/wham/rate-limit-reset-credits` | `openai_quota_service.go` | ⚠ **路径不同**：官方为 `{base}/wham/rate-limit-reset-credits`（`backend-client/src/client/rate_limit_resets.rs:93`），无 `/backend-api` 前缀 |
| `backend-api/accounts/check/v4-2023-04-27` | `openai_oauth_service.go` | ⚠ **路径不同**：官方为 `{base}/api/codex/accounts/check` 或 `{base}/wham/accounts/check`（`backend-client/src/client.rs:306-307`），**无 `v4-2023-04-27` 版本后缀** |
| `backend-api/conversation/` | `openai_alpha_search.go` | 🔴 **官方无** |
| `backend-api/subscriptions` | `openai_privacy_service.go:98` | 🔴 **官方无** |
| `backend-api/files` | `openai_images.go` | 🔴 **官方无生产调用**（`core/src/mcp_openai_file.rs:268` 是 wiremock 测试桩） |
| `backend-api/settings/account_user_setting` | `openai_privacy_service.go:18` | 🔴 **官方无** |

> **本表初版有误，已更正**：初版靠粗略 grep 计数判定前五条"官方有"，精确查证后
> **四条官方根本没有、两条路径与官方不同**。教训同 §1.2 的"外推"类——
> **grep 命中数不等于端点存在**，`subscriptions` 的命中来自
> `utils/readiness/src/lib.rs` 的注释，`files` 的命中来自测试桩。

### SPEC-EP-018　四条官方不存在的端点

- **规则**：官方 Codex CLI **从不访问** `backend-api/conversation/`、
  `backend-api/subscriptions`、`backend-api/files`、
  `backend-api/settings/account_user_setting`　[L1 反证]
- **观测**：⛔ 负面命题，无法用抓包证明不存在
- **状态**：🔴 **四条链路每次调用都是官方永不产生的出站**。其中
  `settings/account_user_setting` 每加一个 OAuth 账号即触发一次（SPEC-EP-010）。
- **备注**：这类偏离**换 TLS 画像解决不了**——问题是"访问了不该访问的端点"，
  不是"用错了指纹"。只能由产品决定是否保留这些功能。

### SPEC-EP-019　两条端点的路径与官方不同

| 端点 | 官方 | Sub2API |
|---|---|---|
| 配额 | `{base}/wham/rate-limit-reset-credits` | `chatgpt.com/backend-api/wham/rate-limit-reset-credits` |
| 账号校验 | `{base}/api/codex/accounts/check` 或 `{base}/wham/accounts/check` | `chatgpt.com/backend-api/accounts/check/**v4-2023-04-27**` |

- **依据**：`backend-client/src/client/rate_limit_resets.rs:93`、
  `backend-client/src/client.rs:306-307`　[L1]
- **状态**：⚠ 待核 —— 需确认官方的 `{base}` 是否含 `/backend-api`；
  `v4-2023-04-27` 后缀在官方源码中不存在，疑似取自 ChatGPT 网页版

> 本节修正了此前"七个端点"的范围假设：实际出站面为 **7 + 6 = 13 条**。

### SPEC-EP-005　压缩仅用于 responses 流式请求

- **规则**：只有 responses 走 `stream_encoded_json_with`（带 `compression` 参数）
  才可能 zstd；models / compact / alpha-search / images 全部走
  `execute` / `execute_with`，**无 compression 参数，一律明文**
- **依据**：`endpoint/session.rs` 两个方法的签名差异——`execute` 不接收 compression，
  故落到 `RequestCompression` 的 `#[default] = None`　[L1]
- **观测**：任意通道（`content-encoding` 头可见）
- **可变性**：条件（responses 压、compact 不压）
- **状态**：✅ **已对齐** —— Sub2API 的 `compressOfficialOpenAIHTTPRequest` 全仓
  仅一处调用点（`official_egress_openai_http.go:292`，且带 `!plan.IsCompact` 判断），
  alpha-search / images / count_tokens / models 四条链路压缩相关命中均为 0

### SPEC-EP-006 ~ 009　各端点的 URL 与方法对齐状态

| 编号 | 端点 | 官方 | Sub2API | 状态 |
|---|---|---|---|---|
| SPEC-EP-006 | models | `GET {base}/models?client_version=…` | `chatgpt.com/backend-api/codex/models` | ✅ 一致 |
| SPEC-EP-007 | compact | `POST {base}/responses/compact` | 同 | ✅ 一致 |
| SPEC-EP-008 | alpha-search | `POST {base}/alpha/search` | `chatgpt.com/backend-api/codex/alpha/search` | ✅ 一致 |
| SPEC-EP-009 | live | `POST {base}/realtime/calls`（另有 `live`，FramelessBidi） | `chatgpt.com/backend-api/codex/realtime/calls` | ✅ 一致 |

- **依据**：各端点 `fn path()`　[L1]
- **可变性**：固定
- **备注**：URL 与方法层面这四条均已对齐；各自的 **header 顺序**受 §3 的 h1 wire
  问题影响（全部未对齐），**body 字段**待第 2 步逐条验证。

### SPEC-EP-012　live 端点：URL 带了官方没有的 query，且缺 `openai-alpha`

- **规则**：
  - URL 为 `{base}/realtime/calls`，**不带任何 query 参数**
  - quicksilver 版本经 **header** 声明：`openai-alpha: quicksilver=v1`（V1 parser）
    或 `quicksilver=v2`（FramelessBidi）
- **依据**：`core/src/client.rs:158`
  `const REALTIME_CALLS_ENDPOINT: &str = "/realtime/calls"`、
  `endpoint/realtime_call.rs:63` `fn path() -> "realtime/calls"`　[L1]
  ─ header 见 `core/src/realtime_conversation.rs:1595`
    `headers.insert("openai-alpha", HeaderValue::from_static("quicksilver=v1"))`　[L1]
- **观测**：任意通道
- **可变性**：条件（v1 / v2 取决于 event parser）
- **状态**：🔴 **两项未对齐**（`openai_live.go`）
  1. **URL 多了 query**：`openai_live.go:35` 为
     `.../realtime/calls?intent=quicksilver&architecture=avas`。官方**不带 query**，
     且 `intent` / `architecture` 这两个参数名在官方源码中不存在
  2. ~~主请求缺 `openai-alpha`~~ —— **该判断有误，已更正**：`openai_live.go:298`
     调用了 `applyLiveUpstreamIdentityHeaders`，其中 `openai_live.go:396` 设置了
     `OpenAI-Alpha: quicksilver=v2`，经 transport 层小写化后即为官方形态。**此项本就对齐。**
- **备注**：真正的偏离只有 URL query 一项。它属于**反向问题**——不是少发 header，
  而是多发了官方不存在的参数。
- **暂不修**：live 需管理员开 `AllowLive` 才可达，无法实测验证去掉 query 后上游
  是否仍接受。改动风险大于收益，留待该端点真正启用时评估。

### SPEC-EP-013　OAuth 路径不得透传入站 query

- **规则**：官方 provider 的 `query_params` 在所有构造点均为 `None`
  （`model-provider-info/src/lib.rs:339,384,534`），除 models 端点由官方自行追加
  `client_version` 外，**URL 不带任何 query**
- **依据**：`codex-api/src/provider.rs:53` `url_for_path()` 仅在
  `self.query_params` 非空时拼接　[L1]
- **观测**：任意通道（URL 可见）
- **可变性**：固定
- **状态**：✅ **已修** —— `openai_alpha_search.go:339` 原先**无条件把入站 query
  原样透传**到出站 URL，等于让第三方客户端往打给官方的 URL 上注入任意参数。
  与 P0-4 的未知顶层字段泄漏同类，只是发生在 **URL 层**而非 body。
  ─ 已改为仅在**非 OAuth 路径**透传：API Key 打的是第三方 base URL，不受官方画像
  约束，保持原行为。

### SPEC-EP-010　隐私设置端点：双重偏离

- **规则**：官方 Codex CLI **从不修改账号隐私设置**，无任何 `settings/*` 出站
- **依据**：全仓检索 `account_user_setting` 官方命中 **0**　[L1 反证]
- **观测**：任意通道
- **可变性**：固定
- **状态**：🔴 **双重偏离**
  1. **端点本身官方从不访问**：`openai_privacy_service.go:18` 打
     `https://chatgpt.com/backend-api/settings/account_user_setting`
  2. **不走官方画像**：该服务用 `PrivacyClientFactory` 返回的 `req.Client`
     （第三方 imroc/req 库），既非 `tlsfingerprint` 画像也非 `DoWithTLS`，
     即 TLS 指纹、header 定型、连接池隔离全部失效
- **触发**：账号级一次性设置（`privacy_mode == "training_off"`，见
  `account.go:240`）。频率低，但**每添加一个 OAuth 账号就产生一次官方永不产生的
  出站**，属强特征而非概率特征。

### SPEC-EP-011　旁路链路的画像失效是系统性的

顺着 SPEC-EP-010 逐条核查其余五条打 `chatgpt.com` 的链路，**六条里有五条不走官方
画像**：

| 端点 | 实现文件 | 客户端 | 官方有无此端点 |
|---|---|---|---|
| `settings/account_user_setting` | `openai_privacy_service.go:18` | 🔴 `req.Client` | ❌ 官方无 |
| `subscriptions` | `openai_privacy_service.go:98` | 🔴 `req.Client` | ✅ 有 |
| `accounts/check/v4-2023-04-27` | `openai_privacy_service.go:97` | 🔴 `req.Client` | ✅ 有 |
| `wham/rate-limit-reset-credits` | `openai_quota_service.go` | 🔴 非画像客户端 | ✅ 有 |
| `backend-api/files` | `openai_images.go` | 🔴 非画像客户端（3 处） | ✅ 有 |
| `conversation/` | `openai_alpha_search.go` | ✅ 官方画像（4 处） | ✅ 有 |

- **依据**：源码检索 `req.Client` / `DoWithTLS` / `OfficialEgress*TLSProfile` 的
  命中分布　[L1]
- **状态**：🔴 **五条链路 TLS 指纹与连接池隔离失效**
- **⚠ 根因判断已两次更正，最终结论如下**：
  - **初版**写"根因是 `supportsOfficialEgressHTTPProfile` 默认放行"——不成立。
    该闸门管的是 **Finalizer（body/header 定型）**，TLS 画像走调用点显式传参。
  - **二版**写"这些服务各自建了独立客户端、忘了接 `httpUpstream`，修法是改走
    `DoWithTLS`"——**也不成立**。
  - **实际情况**：`CreatePrivacyReqClient`（`repository/req_client_pool.go:115`）
    显式设 `Impersonate: true`，注释写明 *"Uses Chrome TLS fingerprint
    impersonation to bypass Cloudflare checks"*。**这是有意用 Chrome 指纹，不是
    遗漏。** privacy 与 quota 两个服务共用该工厂。
- **因此不应改成 Codex 画像**，理由有二：
  1. 这些端点（`settings/account_user_setting`、`accounts/check`、`subscriptions`、
     `wham/rate-limit`）是 **ChatGPT 网页版端点**，官方 Codex CLI 本就不访问
     （SPEC-EP-010 实测官方命中 0）。既然官方不访问，套 Codex 画像并不会让它
     "更像官方"。
  2. 改掉 Chrome 指纹很可能**触发 Cloudflare 拦截**，把一个形态问题换成功能故障。
- **真正的问题在 SPEC-EP-010 而非画像**：官方从不修改账号隐私设置，**这个动作本身**
  就是官方永不产生的。换任何指纹都解决不了，只能由产品决定是否保留该功能。
- **状态**：⏸ **不修**（结论从"需修"翻转）。仅
  `openai_images.go:1433` 的 `fetchOpenAIImageDownloadURL` 例外——它打的 `files`
  与 `conversation` 是官方**确实会访问**的端点，值得单独评估。
- **逐条复核后的准确状态**：

| 链路 | 出站方式 | 是否需修 |
|---|---|---|
| `settings/account_user_setting` / `subscriptions` / `accounts/check` | `req.Client`（`openai_privacy_service.go`） | 🔴 需修，改造成本中（三处共用 `PrivacyClientFactory`） |
| `wham/rate-limit-reset-credits` | `req.Client`（`openai_quota_service.go`） | 🔴 需修，同上 |
| `backend-api/files/{id}/download`、`conversation/{id}/attachment/…` | `req.Client`（`openai_images.go:1433` `fetchOpenAIImageDownloadURL`） | 🔴 需修，需重构函数签名以注入 `httpUpstream` |
| `openai_images.go:621` 主路径 | `httpUpstream.Do`（无画像） | ✅ **不需修** —— 打的是 `api.openai.com`（API Key 路径），不受官方画像约束 |

  ─ 最后一行是复核中避免的一次误改：主路径的 `Do`（而非 `DoWithTLS`）是**正确的**。

---

## 2.10 剩余端点的 header 集合与 body

> **顺序一律标为待实测，不做推导。** 理由：responses 的实测顺序里 `originator` 与
> `user-agent` 出现在**倒数**位置，而按 `default_headers()` 的插入序推导它们应在最前
> ——说明「基础头 → extend → apply_auth」这条链推不出真实次序。当天已因外推错过
> 三次（§1.2），故此处只记可确证的集合与字段。

### SPEC-EP-014　compact 的 header 集合

- **规则**：`extra_headers` 的构造链为（`core/src/client.rs:599-617`）　[L1]
  1. `x-codex-installation-id`（条件：有安装 ID 时）
  2. `build_responses_headers()` → `x-codex-beta-features`、`x-codex-turn-state`
  3. `add_originator_header()` → `originator`
  4. `build_responses_compatibility_headers()`
  5. `build_session_headers()` → `session-id`、`thread-id`
  6. `x-oai-attestation`（条件：能生成 attestation 时）
  7. `add_responses_lite_header()` → `x-openai-internal-codex-responses-lite`（Lite 时）
- **与 responses 的差异**：compact **不设** `x-client-request-id`
  （responses 在 `endpoint/responses.rs:89` 单独 `insert_header`），也不设 `accept`
  （SPEC-HDR-006）
- **顺序**：⛔ **待实测** —— 落 h1 wire 的兜底清单
- **可变性**：条件（installation-id / attestation / Lite 三项各自可缺）

### SPEC-EP-015　alpha-search 的 header 集合与 body 字段

- **body**：`SearchRequest { id, model, reasoning?, input?, commands? }`
  （`codex-api/src/search.rs:9`）　[L1]
- **header 集合**：`search_request_headers()` 只设**两项**
  （`ext/web-search/src/tool.rs:185-195`）　[L1]：
  1. `x-codex-turn-metadata`（条件：有 turn metadata 时）
  2. `originator`（经 `add_originator_header`）
  ─ **不设** `accept`（由 reqwest 补 `*/*`）、**不设** `session-id`/`thread-id`
- **顺序**：⛔ 待实测
- **状态**：🔴 Sub2API 多发了 `session-id`/`thread-id`（SPEC-HDR-007 已改为连字符，
  但**官方在该端点上根本不发这两个头**），待第 2 步核对

### SPEC-EP-020　compact 的 body 字段集比 responses 窄

- **规则**：`CompactionInput { model, input, instructions, tools?,
  parallel_tool_calls, reasoning?, service_tier?, prompt_cache_key? }`
  （`codex-api/src/common.rs:26`）　[L1]
- **与 responses 的差异**：**不含** `tool_choice`、`store`、`stream`、`include`、
  `text`、`client_metadata` —— 这与 SPEC-BODY-002（compact 不压缩）、
  SPEC-EP-014（compact 不设 `accept`/`x-client-request-id`）共同说明
  **compact 是一条独立形态，不能套 responses 的定型**
- **顺序**：⛔ 待实测
- **状态**：⚠ 待核对 Sub2API 的 compact 出站是否多发上述六个字段

### SPEC-EP-016　images 走 responses 端点，header 与 body 同 responses

- **规则**：Sub2API 的 images 打的是 `/codex/responses`（SPEC-EP-001），因此其
  header 与 body 约束**完全等同 responses**，不适用官方 `images/generations` 的
  `ImageGenerationRequest { prompt, background?, model, n?, quality?, size? }`
- **顺序**：✅ 命中 responses 的实测清单（已逐字节对齐）
- **状态**：⚠ body 仍有 SPEC-BODY-005（`tool_choice` 类型）未修

### SPEC-EP-017　count_tokens 无官方对应物

- 见 SPEC-EP-003。官方无该端点，**无形态可列、无基线可采**，不参与第 2 步验证。

## 2.11 规则的验证状态

标注口径：**✅ 验过了**＝有官方实测运行号；**⛔ 验不了**＝客观无法采到，附原因；
**⚠ 可验未验**＝观测手段具备但尚未执行。

#### 2.11.1 ✅ 验过了（27 条）

| 条目 | 实测运行 |
|---|---|
| SPEC-TLS-001/002 | direct pcap、`official-nativetls-20260727T161531Z` |
| SPEC-PROTO-001/002 | h1 探针（官方 `alpn=None`）、`official-httpfb3-20260727T234853Z` |
| SPEC-H1-001/002/003/004 | `official-h1-full-20260727T124125Z` + `VERIFY-h1wire-v3` |
| SPEC-H2-001~007 | `official-h2-20260727T131936Z`（**仅 rustls 分支**，见 §4 污染说明） |
| SPEC-WS-001/002 | `official-h1-full-20260727T124125Z` |
| SPEC-HDR-002/003/004/005/006/007 | h1 基线（基础头集合、无 `accept-language`、`openai-beta` 仅 WS、UA、accept、会话头） |
| SPEC-BODY-001/002/003/005 | `official-body2-20260728T000549Z` |
| SPEC-EP-001/005/006/013/016 | 400 探测、h1 基线、URL 实测、单元测试 |

#### 2.11.2 ⛔ 验不了（8 条）

| 条目 | 原因 |
|---|---|
| SPEC-HDR-001（组装顺序） | 顺序层面已被 responses 实测**证伪**——推导链得不到真实次序，该条降为语义参考，不作断言 |
| SPEC-WS-003（两个特例改写） | 官方当前不发 `sec-websocket-protocol` / `origin`，**无样本可采** |
| SPEC-BODY-004（turn-state 来源） | 上游在历次采集中**从未下发** `response.metadata` 事件 |
| SPEC-EP-003 / 017（无 count_tokens 端点） | **负面命题**——无法用抓包证明某端点不存在，只能靠源码反证 |
| SPEC-EP-002（只访问 chatgpt.com） | 同为负面命题；已采样本中官方确实全打 chatgpt.com，但不构成穷尽证明 |
| SPEC-EP-012（live） | 需管理员开 `AllowLive` 才可达 |
| SPEC-EP-010 / 011（画像失效） | 属 **Sub2API 侧**实现问题，非官方形态，用源码检索确认而非抓包 |

#### 2.11.3 ⚠ 可验未验（8 条）

| 条目 | 缺什么 |
|---|---|
| SPEC-TLS-003（WS 扩展随机序） | 需 direct pcap 多次采样比对 JA3 差异 |
| SPEC-EP-004（六个额外端点） | 官方基线已含 plugins/ps 系列，但未逐条对账 |
| SPEC-EP-007/008/009（compact/search/live 的 URL） | 仅 models 有实测，其余未触发 |
| SPEC-EP-014（compact header 集合） | 需触发官方 compact（对话超长才发生） |
| SPEC-EP-015（search body） | 需触发官方 web search（模型自主决定，探针环境下不会发生） |
| SPEC-BODY-006（images 模板其余字段） | 需触发官方生图 |

> **⚠ 类的共同障碍**：探针不转发上游，官方 CLI 拿不到真实响应，因此**依赖模型自主
> 决策的后续动作（工具调用、生图、compact）都触发不了**。要采这些，须让探针能返回
> 可驱动后续流程的伪造响应——那是另一项观测能力建设。

---

# 第三部分　方案

迄今讨论过**两种方案**，本部分先并列说明，再分别记录各自的进展与证据。

| | 方案甲：Go 内薄层定型 | 方案乙：同源 sidecar |
|---|---|---|
| **形态从哪来** | 在 Go 内**复刻**官方形态（utls 画像、Finalizer、自写 h1 wire） | **直接用官方同款依赖库**产生形态 |
| **架构** | 单体 Go，出站侧加一层薄层 | Go 主程序 + Rust/Node.js sidecar |
| **状态** | ✅ **当前在用**，正在修到极限（§1.4.1 第二阶段） | 🔬 **理想的未来方案**；已 PoC 验证可行，**评估推后至方案甲边界探明之后** |
| **形态维护成本** | 官方每升级一次就要重新逆向对齐一次 | 跟依赖版本号即可 |
| **架构复杂度** | 不变 | 显著上升（IPC、跨进程流式、多语言维护） |

**为什么两条都要留在文档里**：方案甲是当前生产形态，其规则与验收必须可追溯；
方案乙是既定的理想方向，其 PoC 结论需要留档待用。但**两者现在不比较**——
按 §1.4.1，要先用方案甲把边界实测出来，才有对比的基准。

## 3.1 方案甲：Go 内薄层定型（当前在用）

### 3.1.1 方案边界

取自 README §1.1.3.1「方案结论与边界」，五条约束：

1. **范围不变**：复用现有 composite 路由、鉴权、账号调度、重试、流式响应、usage、
   会话粘性和代理系统；官方客户端伪装只修正三条 OAuth 官方出站路径，不改变 Key、
   Group、模型路由或计费归属。
2. **薄层实现**：在现有协议转换完成后增加统一的 Profile Resolver、
   `OfficialEgressContext`、Finalizer 和 Transport/Dialer Provider，**不分叉完整
   Handler**。
3. **最小改写与数据保真**：保留入口已携带且上游支持的语义，只修正抓包已证明不一致
   的字段；顶层定型不得通过 `map[string]any` 重建并改写嵌套用户 JSON。
4. **应用与传输同时对齐**：Anthropic HTTP、OpenAI HTTP、OpenAI WebSocket 分别维护
   独立 Profile，同时约束 Header/Body、TLS/ALPN、协议选择和连接复用。
5. **身份与失败边界明确**：动态身份必须来自入口、会话、响应映射或账号配置等可追踪
   来源；OAuth Profile、Host 或身份状态校验失败时必须明确失败并记录脱敏原因，
   **不能静默切换画像**。

### 3.1.2 实际架构

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

**本轮在该架构内新增的一层**：`h1WireConn`（`tlsfingerprint/h1_wire.go`）——
Transport 之下、`net.Conn` 之上，用于重写 h1 请求头字节。它是方案甲能对齐
SPEC-H1-002~004 的关键，也是薄层里唯一触及 wire 字节的组件（详见 §3.1.5.2）。

### 3.1.3 该方案的固有上限

- **形态靠复刻，不靠同源**：官方每次升级都要重新抓包、重新对齐。而 Codex 2.1.113
  起官方转二进制分发，**新版源码永久不可得**（§3.4.1），逆向只会越来越难。
- **部分项无配置入口**：h2 的 `INITIAL_WINDOW_SIZE`、`WINDOW_UPDATE`、伪头顺序
  须 fork `x/net/http2`（§1.4 已决定不做）。
- **语言层差异消除不了**：Go 与 Rust 在 TCP 时序、连接复用节奏上的差异（§3.4.2）。

### 3.1.4 差异清单

**A 类 — 值/结构差异，不依赖自写 wire，可直接改：**

| # | 差异 | 规格 | 状态 |
|---|---|---|---|
| A1 | UA 多了官方永不产生的 suffix `(codex_exec; 0.145.0)` | SPEC-HDR-005 | ✅ **已修 + wire 验收**（§3.1.5） |
| A2 | `accept` 三处不对（compact 甚至完全没有该头） | SPEC-HDR-006 | ✅ **已修 + wire 验收**（§3.1.5） |
| A3 | alpha-search 会话头用下划线，且含官方不存在的 `Conversation_ID` | SPEC-HDR-007 | ✅ **已修** |
| A8 | OAuth 路径把入站 query 原样透传到官方 URL | SPEC-EP-013 | ✅ **已修** |
| A4 | `tool_choice` 发对象，官方类型是 `String` | SPEC-BODY-005 | ✅ **已修**（改为 `"auto"`，官方实测值） |
| A6 | 五条旁路链路不走官方画像 | SPEC-EP-011 | ⏸ **不修**（实为有意用 Chrome 指纹绕 Cloudflare；端点本身官方不访问，套 Codex 画像无益且有故障风险） |
| A7 | 隐私端点访问官方从不访问的 `settings/account_user_setting` | SPEC-EP-010 | ❌ **未修**（需产品决策：该端点官方无对应物） |
| A5 | live URL 多带 `?intent=&architecture=` | SPEC-EP-012 | ⏸ **暂不修**（需开 `AllowLive` 才可达，无法实测验证） |

**A1 优先级最高**：全局常量，一处修复覆盖所有端点。
**A6 建议连根治**：把 `supportsOfficialEgressHTTPProfile` 从"默认放行"改"默认拦截"。

**B 类 — 须自写 h1 wire：**

| # | 差异 | 规格 | 状态 |
|---|---|---|---|
| B1 | `Host` 大小写与位置 | SPEC-H1-002 | ✅ **已修 + wire 验收**（§3.1.5） |
| B2 | `content-length` 位置 | SPEC-H1-003 | ✅ **已修 + wire 验收** |
| B3 | 其余 header 顺序 | SPEC-H1-004 | ✅ **models 与 responses 逐字节一致**；compact/alpha-search/images/live 落兜底清单，无基线 |
| B4 | WS 握手剩余头顺序 | SPEC-WS-002 | ⚠ 大小写已对齐，顺序落兜底清单 |

> **重要**：不必逐端点比 header 顺序——Go 的 `http.Header` 是 map，构造顺序不体现
> 在 wire 上，永远字典序。各端点的顺序差异是 B1~B3 的同一实例，统一由自写 wire 解决。
> 但**自写 wire 时需要按端点配置顺序清单**，因为官方各端点的插入序不同
> （models 以 `version` 打头、ps/mcp 以 `originator` 打头）。

**C 类 — 需产品决策：**

| # | 事项 | 规格 |
|---|---|---|
| C1 | `count_tokens` 无官方对应物，且打 `api.openai.com` | SPEC-EP-003 |
| C2 | 代理画像 ALPN 方向相反（改空 ALPN 则 h2 差异全消） | SPEC-TLS-002 |

**第 1 步剩余（可与第 2 步合并做）：**
- compact / models / search 的 body 字段逐项比（images 已比，见 SPEC-BODY-006）
- 各端点 header 的**完整集合**比对（目前比的是取值，尚未确认有无多发/少发）

### 3.1.5 已完成的修复与验收

#### 3.1.5.1 A 类修复的 wire 级验收（`egress-a-fixes-0.1.165-11`）

改完即部署 Vircs 并用 h1 直连探针实测，**不接受"自以为改对了"**：

| 项 | 官方基线 | 修复前 | 修复后实测 | 结论 |
|---|---|---|---|---|
| models `accept` | `*/*` | `application/json` | `*/*` | ✅ |
| UA | `…x86_64) xterm-256color` | `…x86_64) unknown (codex_exec; 0.145.0)` | `…x86_64) unknown` | ✅ |
| responses `accept` | `text/event-stream` | 同 | `text/event-stream` | ✅ 本就一致 |
| 会话头 | `session-id` / `thread-id` | responses 路径本就正确 | `session-id` / `thread-id` | ✅ |

**UA 残留的 terminal 段差异（`xterm-256color` vs `unknown`）不是偏离**：官方基线
是在带终端的容器里采的，而 Sub2API 是服务端进程、无终端；官方在同样条件下输出的
也是 `unknown`（`terminal-detection/src/lib.rs:204`）。

证据：`local-analysis/captures/wire-parity-fix-20260727/h1-wire-probe/VERIFY-a-fixes-*.json`

#### 3.1.5.2 B 类（自写 h1 wire）的 wire 级验收（`h1-wire-v2-0.1.165-13`）

| 项 | 官方基线 | 修复后实测 | 结论 |
|---|---|---|---|
| models 完整顺序 | `version, authorization, chatgpt-account-id, accept, originator, user-agent, host` | **完全相同** | ✅ **逐字节一致** |
| `host` 大小写 | 小写 | 小写 | ✅ |
| `host` 位置 | 用户头之后 | 末尾（无 body）/ 倒数第二（有 body） | ✅ |
| `content-length` | 最后 | 最后 | ✅ |
| 全小写 | 是 | 是 | ✅ |

**实现要点**：没有自写 RoundTripper（那要自管连接池、keep-alive、响应读取，
500~800 行；且改成每请求新建连接又会因失去 keep-alive 制造新偏离），改为在
`net.Conn` 层拦截重写请求头字节，Go 的连接池与响应处理全部复用。任何无法确信
改写正确的情形（chunked、超长头、解析失败）一律 passthrough 原样透传。

**开关**：画像的 `H1HeaderOrders`，留空即完全不介入。目前只给 OpenAI 直连画像
配置，代理与 Anthropic 路径不受影响。

**`h1-wire-v3-0.1.165-14` 追加验收**：补采官方降级路径的基线后，
`POST /codex/responses` 也已与官方**逐字节一致**：

    version, x-codex-beta-features, x-codex-window-id, x-codex-turn-metadata,
    x-openai-internal-codex-responses-lite, x-client-request-id, session-id,
    thread-id, accept, content-encoding, content-type, authorization,
    chatgpt-account-id, originator, user-agent, host, content-length

**残留**：models 与 responses 之外的端点（compact / alpha-search / images / live）
仍无逐字节基线，落兜底清单，为最近似值。

#### 3.1.5.3 官方 responses body 的实测基线（`official-body2-20260728T000549Z`）

探针原先只读 header 段，body 类规则**一条都验不了**。补上 body 采集后（只提取
**结构**不记录取值——body 含用户对话内容，产物要归档），并加 zstd 解压（官方
responses 的 body 是压缩的，不解压取不到结构），得到：

**顶层字段序**：
`model, input, tool_choice, parallel_tool_calls, reasoning, store, stream, include,
prompt_cache_key, text, client_metadata`

**关键字段形态**：

| 字段 | 官方实测 | 意义 |
|---|---|---|
| `tool_choice` | `str:auto` | **确证是字符串** → SPEC-BODY-005 的类型偏离成立 |
| `parallel_tool_calls` | `bool:false` | 该样本为 Lite 模式，印证 SPEC-BODY-003 |
| `stream` | `bool:true` | |
| `store` | `bool:false` | |
| `include` | `array:<len=1>` | 对应 `reasoning.encrypted_content` |
| `reasoning` | `object:{context,effort}` | Lite 下含 `context`，印证 SPEC-BODY-003 |

> **采集要点**：body 必须解 zstd。首次采集因未解压而拿到空结构，一度误以为探针
> 没抓到 body。

#### 3.1.5.4 最终验收（`tool-choice-0.1.165-15`）

| 项 | 官方基线 | 实测 | 结论 |
|---|---|---|---|
| `GET /codex/models` header 全序 | 7 项 | 相同 | ✅ **逐字节一致** |
| `POST /codex/responses` header 全序 | 17 项 | 相同 | ✅ **逐字节一致** |
| responses body `tool_choice` | `str:auto` | `str:auto` | ✅ |

##### 一处验证盲区，必须记录

上表的 `tool_choice` 取自**普通 responses 请求**，而它本来就是 `auto`——
**这不能证明 images 模板的改动生效**。SPEC-BODY-005 修的是
`openai_images_responses.go:357` 的硬编码模板，只有走生图入口才会命中。

尝试补验时被拦下：`POST /v1/images/generations` 返回
`403 Image generation is not enabled for this group`。属**分组配置限制**，
非探针能力不足。

**结论**：A4 的代码改动已通过编译与全量测试，但**未取得 wire 级证据**。
要补验需先为测试分组开启生图权限，然后跑
`tools/official_client_capture/run_images_wire_probe.sh`（已就位）。

### 3.1.6 已定位但未解决的重点

- **SPEC-TLS-002 代理画像方向相反**：官方默认经代理不 offer h2，而我们的代理
  画像声明 `h2`。该画像很可能整个是观测污染的产物。**修法可能是把代理画像也改成
  空 ALPN**——那样代理路径也走 h1，h2 层全部差异自动消失。**这是尚未验证的推论。**
- **SPEC-EP-011 画像失效是系统性的**：根因是白名单"默认放行"。

## 3.2 方案乙：同源 sidecar（理想的未来方案，评估推后）

> **定位**：这是既定的理想方向，但按 §1.4.1，**评估推后至方案甲的边界探明之后**。
> 本节只留档 PoC 结论与架构设想，不作选型论证。


### 3.2.1 可行性验证（PoC）

背景：本表的 ❌ 项大多卡在「Go 标准库无配置入口，须自写 wire 或 fork」。若出站层
改用**官方同版本的 Rust 依赖**，这些形态可能无需复刻即自动一致。已做 PoC 验证。

PoC 依赖严格锁定官方 `Cargo.lock`：`rust 1.95.0` / `reqwest =0.12.28` /
`tungstenite =0.27.0`，reqwest 的 features 照抄官方（`cookies`）。

| 层 | 结果 | 说明 |
|---|---|---|
| **h1 header 顺序 / 大小写 / `host` 位置** | ✅ **逐字节一致** | 与官方基线完全相同，**未写一行顺序控制代码** |
| **WS 握手前 5 项** | ✅ **完全一致** | `Host, Connection, Upgrade, Sec-WebSocket-Version, Sec-WebSocket-Key` 大写驼峰且顺序对 |
| **WS 剩余头顺序** | ⚠ **取决于插入顺序** | 实测输出为插入序的**逆序**——`swap_remove` 扰动为实（SPEC-WS-002）。要匹配官方须对齐插入顺序，不是白拿 |
| **h2 SETTINGS 四项 + `WINDOW_UPDATE`** | ✅ **逐项一致** | 需显式 `.use_rustls_tls()`，即复刻官方 `custom_ca.rs:307` 的分支 |

实测记录：`local-analysis/captures/wire-parity-fix-20260727/rust-poc/`

#### 3.2.1.1 由此得到的两条结论

**一、"用同样的库"≠"自动一致"，还须对齐配置分支。** h2 那组数值在裸 reqwest 下
根本测不到（不 offer h2），加上 `.use_rustls_tls()` 后才逐项吻合。**这恰好也是
SPEC-H2 段观测污染的成因**——两件事是同一个开关。

**二、IPC 协议必须保序。** h1 的自动一致性来自 `HeaderMap` 的插入序。若 Go 侧把
header 用 map / JSON object 传给 Rust，顺序当场丢失，hyper 的保序输出随之失效，
等于绕一圈回到原点。**跨语言协议必须用数组传 header**。

#### 3.2.1.2 未验证的部分

- **TLS ClientHello 不在"白拿"清单内**：官方 HTTP 默认走 native-tls，底层是
  **系统 OpenSSL**，形态取决于容器内的 OpenSSL 版本而非 Rust 依赖。
  （反过来说官方自身也因用户机器而异，是分布而非单值，要求本就较低。）
- h2 伪头顺序未采到（PoC 连接在 HEADERS 帧前结束）
- 架构代价一分未减：IPC、SSE 流式透传的背压、Go 侧 official_egress 的迁移、
  计费与日志跨进程、Rust 侧重建重试与连接池、以及长期的 Rust 维护能力

### 3.2.2 目标架构

> ⚠ **本小节是方案设想，不是规格。** 本表其余部分记录的是**已验证的事实**（官方
> 形态与对齐状态），本节记录的是**尚未决策的架构方向**。不要把它当作已定的架构。
> Anthropic 侧一并记于此只为不散落，其规格另立文档。

```
Go 主程序（路由 / 鉴权 / 计费 / 调度 / 语义定型）
  ├─ Rust sidecar     → OpenAI      （同源 Codex CLI，已 PoC 验证，见 §1.20）
  └─ Node.js sidecar  → Anthropic   （同源 Claude Code，HTTP 栈待验证）
```

Anthropic 侧是 **Node.js 而非 Rust**——`newAnthropicOfficialEgressTLSProfile`
的注释即写明复现的是 "Claude Code 2.1.220 的 Node.js ClientHello（17 个 cipher、
HTTP/1.1 ALPN）"。要同源就得用 Node.js，因此是三语言架构。

#### 现有薄层四个组件的去向

README §1.1.3.1 描述的薄层（Profile Resolver / OfficialEgressContext / Finalizer /
Transport Dialer Provider）位于**出站侧**（"在现有协议转换完成后"），不是入口前置层。
在同源方案下四者命运不同：

| 组件 | 去向 |
|---|---|
| Profile Resolver | **留在 Go** —— 属业务逻辑 |
| OfficialEgressContext | **留在 Go** |
| Finalizer | **只有形态层被取代**，语义层留在 Go（见下） |
| Transport / Dialer Provider | **整个消失** —— TLS 指纹与 h1/h2 wire 由 sidecar 的库天然产生 |

Finalizer 现在承担两类活，同源方案只接管其中一类：

- **形态层**（header 名大小写、顺序、`Host` 位置、SETTINGS）→ **sidecar 白拿**，
  §3.2 已逐项验证
- **语义层**（header 放什么值、body 剔除哪些字段、Lite 变换、顶层白名单）→
  **仍须 Go 实现**。sidecar 不会自动做这些，传什么字段就发什么字段

#### 三条必须遵守的约束

1. **跨语言协议必须保序。** h1 的自动一致性来自 `HeaderMap` 插入序；若用 map /
   JSON object 传 header，顺序当场丢失，等于绕一圈回到原点。**必须用数组。**
2. **必须复刻官方的配置分支，不能只对齐依赖版本。** §3.2 已证：裸 reqwest 经代理
   根本不 offer h2，须显式 `.use_rustls_tls()`（即官方 `custom_ca.rs:307` 那个判断）
   才与官方一致。
3. **Anthropic 侧第一步是验证 HTTP 栈是否变更。** 用 2.1.88 源码定位它用哪个 HTTP
   库与哪些配置分支（架构性信息跨版本相对稳定），**再用 2.1.220 实测验证该结论仍
   成立**。低成本对账法：若该库的默认 ClientHello 恰为现有画像的 17-cipher，说明
   栈未换、结论可用。

> 关于「2.1.88 属外推、不得降为画像基准」这条既有裁定：它在**复刻方案**下完全成立
> （复刻就是照抄值）。**同源方案不抄值**，只用旧源码回答"用什么库、有哪些分支"，
> 具体取值一律以 2.1.220 实测为准，因此两者不冲突。

#### 尚未决策 / 未评估

- Go ↔ sidecar 的 IPC 形式与 SSE 流式透传的背压处理
- 现有 Go 侧 official_egress 代码的迁移路径
- 计费、日志、指标跨进程传递
- sidecar 侧重建重试、超时与连接池
- 长期的 Rust / Node.js 维护能力
- **流量的统计特征**：官方 CLI 是单用户单进程单账号、节奏由人操作决定；Sub2API 是
  网关，多账号复用、高频转发、连接池共享。即使单个请求逐字节一致，请求间的时间
  分布与并发模式在统计层面仍不同。**这一层换语言解决不了**，若验收标准写成"官方
  无法区分"，它会是最后也最难的门槛。

## 3.3 方案甲的边界（决定是否需要方案乙）

> **本节不是两方案的选型对比**（那要等边界探明后才做，见 §1.4.1）。它记录的是
> **穷尽方案甲之后仍然改不动的项**——这个不可达集合才是评估方案乙的唯一依据。

### 3.3.1 边界的当前状态

下表只填**已实测**的项，未实测的留空——不做推断。「方案乙」一列仅作参照，
说明该项在同源方案下是否天然消失，**不构成选型结论**。

| 形态项 | 官方基线 | 方案甲（当前在用） | 方案乙（PoC） |
|---|---|---|---|
| h1 header 顺序 | `version, authorization, …, host` | ✅ 逐字节一致（自写 `h1WireConn`） | ✅ 逐字节一致（**零复刻代码**） |
| `host` 大小写与位置 | 小写、用户头之后 | ✅ 一致 | ✅ 一致 |
| WS 握手前 5 项 | 大写驼峰 | ✅ 一致（`PreserveHeaderCase`） | ✅ 一致（tungstenite 原生） |
| WS 握手剩余头顺序 | swap_remove 扰动序 | ⚠ 兜底清单，非实测 | ⚠ 取决于插入顺序，非白拿 |
| h2 SETTINGS 四项 | 见 §2.4 | ❌ 两项须 fork（已决定不做） | ✅ 逐项一致（需 `use_rustls_tls()`） |
| h2 `WINDOW_UPDATE` | 5,177,345 | ❌ 1,073,741,824 | ✅ 5,177,345 |
| h2 伪头顺序 | `:method,:scheme,:authority,:path` | ❌ 无配置入口 | 未采到 |
| TLS ClientHello | 见 §2.1 | ✅ utls 复刻 | ⚠ **不在白拿清单**，取决于系统 OpenSSL |

### 3.3.2 已确认的硬边界（方案甲改不动）

| 项 | 为什么改不动 | 影响面 |
|---|---|---|
| h2 `INITIAL_WINDOW_SIZE`、首个 `WINDOW_UPDATE` | 取自 `conf.MaxUploadBufferPer{Stream,Connection}`，可经 `http.Transport.HTTP2` 配置，但那要求 transport 由 `ConfigureTransports` 创建——而那样创建的用 `noDialClientConnPool` 不能独立拨号；改走 `t1.RoundTrip` 则 `dialConn` 会把连接断言为 `*tls.Conn`，utls 过不去。**是 utls 与 net/http 的 h2 升级路径不兼容**，非配置缺失 | 仅代理路径（实测生产 97 账号全部直连） |
| h2 伪头顺序 | 硬编码于 `x/net/internal/httpcommon/request.go:138` | 同上 |
| Go 与 Rust 的运行时差异 | TCP 时序、连接复用节奏、重试退避——非配置项，无从复刻 | 全路径，但难以量化 |
| 流量统计特征 | 官方是单用户单进程单账号、节奏由人操作决定；Sub2API 是网关、多账号复用高频转发 | 全路径，**换语言也解决不了** |

### 3.3.3 边界尚未探明的部分

以下项**还没被证明改不动**，属于方案甲仍应继续推进的范围：

- **WS 握手剩余头顺序**（SPEC-WS-002）——当前落兜底清单，未取得官方逐字节基线
- **compact / alpha-search / live 的 header 顺序**——同上，缺基线而非缺手段
- **访问了官方不访问的端点**（SPEC-EP-018 四条）——这不是形态问题，是**功能取舍**，
  须产品决策而非技术攻关
- **`tool_choice` 的 wire 级证据**——代码已改，因生图权限未开而未验

> **在这些项穷尽之前，不做方案乙的选型决策。** 把未探明的项算进"边界"会高估
> 方案甲的不足，进而高估方案乙的必要性。

## 3.4 本版未覆盖

按 §1.5.3 的范围界定，以下**尚未纳入**，不代表已对齐：

1. **models / compact / 旁路端点**（`count_tokens`、`alpha/search`、`/v1/live`）的
   逐项形态
2. **Anthropic 侧全部形态** —— Anthropic 画像未开启 `LowercaseHeaders`
   （Claude Code 是 Node.js，形态未验证）
3. **连接管理与时序**：连接复用节奏、重试退避、WS 连接池规模（官方为单连接 +
   1 条 prewarm、无 PING；Sub2API 为上限 128、`min_idle` 4、30s PING）
4. **Go 与 Rust 的运行时差异**：TCP 时序等难以写成规格的项

### 3.4.1 一条必须记住的紧迫性

记忆中的「官方源码基线陷阱」：**Codex 2.1.113 起官方转为二进制分发，新版源码永久
不可得**。本表所有 L1/L2 依据都建立在 0.145.0 源码可读的前提上。源码消失后，抓包
将成为唯一手段，而本表会是唯一能回答"官方为什么是这个形态"的历史锚点。

**趁源码还在，把规格固化下来——现在不做，以后做不了。**

### 3.4.2 对"官方无法区分"这一目标的诚实评估

本表建成也达不到该目标。已确认的硬边界：SPEC-H1-002/003/004 须自写 h1 wire、
SPEC-H2-003/006/007 须 fork `x/net/http2`，更不论 Go 与 Rust 在 TCP 时序与连接
复用节奏上的差异。

本表能给出的是：**把"我们觉得差不多"变成"差 N 项，每项差多少、为什么改不了、
修它要付什么代价"**。这是能守住的目标；追求绝对不可区分会导致无限投入，且永远
无法证明达成。

---
