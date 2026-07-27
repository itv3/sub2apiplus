# Codex CLI 0.145.0 出站形态规格表

版本绑定：`codex-cli 0.145.0`　依赖锁定：`hyper 1.8.1` / `http 1.4.0` / `tungstenite 0.27.0` / `h2 0.4.13` / `reqwest 0.12.28`
首版日期：2026-07-27

## 0. 这份文档是什么

它描述**官方 Codex CLI 用 OAuth 凭据向 OpenAI 请求时，在线上产生的可观测形态**，
以及 Sub2API 对每一项的对齐状态。

它**不是**修复记录。修复过程见
[官方出站 wire 一致性修复清单](OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md)；
本表回答的是"官方是什么样"，那份文档回答的是"我们改了什么"。

### 0.1 为什么要有这份表

此前的工作方式是「抓包对比 → 改代码 → 抓包验证」，判断反复反转。复盘 2026-07-27
当天的 11 次反转，成因分布是：

| 成因 | 占比 | 典型 |
|---|---|---|
| 抓包通道自身有掩盖 | 3 | MITM 必经代理走 h2，HPACK 抹平 header 大小写与顺序 |
| 脚手架预置掩盖差异 | 2 | 预置模型清单掩盖第三方 Lite 失效 |
| 读源码读错层 | 1 | 把单元测试的 mock base 当成生产 URL |
| 读源码读漏调用点 | 1 | 误判 images 端点无人调用（实际在 `ext/image-generation`） |
| 常量语义误读 | 1 | 把 `defaultMaxReadFrameSize` 的**上限**当默认值 |
| 只读一层未及依赖库 | 2 | 读 hyper 得出"官方全小写"，对 HTTP 对、对 WS 错，**引入了生产回归** |

结论有两条。其一，**"以源码为准"并不能解决问题**——后四类全是源码推断出的错，
且比抓包错得更隐蔽（会一路自洽到部署）。其二，真正缺的是**规格这一层**：过去是
「抓包结论」直接对「代码实现」，中间没有可审计的东西，每次判断都活在对话里。

因此本表的设计原则是：**抓包与源码交叉验证，且每条规则都必须声明"用什么通道才能
观测到它"**——因为最贵的教训是，用看不到该项的通道去验，会得到虚假的通过。

### 0.2 证据等级

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

### 0.3 可变性

- **固定**：每次请求都一样
- **随机**：官方每次都不同（复刻时必须同样随机，钉死单一样本反而是负指纹）
- **条件**：随场景变化（Lite/非 Lite、有无 attestation、直连/代理）

### 0.4 本版范围

**OpenAI OAuth 出站、responses 端点（HTTP + WS）**，覆盖 TLS / 协议协商 / wire /
header / body 五层。models、compact、旁路端点与 Anthropic 侧后续按同格式扩。

---

## 1. TLS 层

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

## 2. 协议协商层

### SPEC-PROTO-001　直连恒为 HTTP/1.1

- **规则**：因 ALPN 为空（SPEC-TLS-001），服务端不协商，落到 HTTP/1.1
- **依据**：h1 探针实测官方 `negotiated_alpn = None`　[L3]
- **观测**：h1 直连探针
- **可变性**：固定
- **状态**：✅ 已对齐（`ProfileNegotiatesH2` 判定为假）
- **⚠ 连带影响**：**这是全表最重要的一条**。它意味着 h1 wire 形态（§3）作用于
  **默认主路径**，而 h2 帧层（§4）**仅在经代理时**才可见。

### SPEC-PROTO-002　responses 默认走 WebSocket，HTTP 是降级路径

- **规则**：官方默认 WS；HTTP 仅在 `force_http_fallback` 后启用
- **依据**：`core/src/client.rs:509` `force_http_fallback`，
  `client.rs:930` `responses_websocket_enabled`　[L1]
- **观测**：h1/h2 探针
- **实测**：探针拒绝 WS 升级（回 400）后官方仍走错误退出，**未观察到降级**
- **可变性**：条件
- **状态**：⚠ **官方参照物难得** —— Sub2API 默认 HTTP 出站，其官方对照物是官方
  自己极少走的降级路径。这影响 §3 全部条目的优先级判断。

---

## 3. HTTP/1.1 wire 层

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
- **状态**：❌ **未对齐** —— Go 的 `Request.write` 硬编码
  `fmt.Fprintf(w, "Host: %s\r\n", host)` 且置于最前。**影响直连主路径**。
  须自写 h1 请求侧字节，无配置入口。

### SPEC-H1-003　`content-length` 排在 `host` 之后

- **规则**：有 body 时 `content-length` 为**最后一项**，排在 `host` 后
- **依据**：由 hyper `set_length` 在 encode 之前塞入 `HeaderMap`，是最后插入项　[L2]
- **观测**：h1 直连探针
- **实测**：`POST /backend-api/ps/mcp` → `..., content-type, host, content-length`
- **可变性**：固定
- **状态**：❌ 未对齐（Go 将其排除在 `writeSubset` 外并前置）。与 SPEC-H1-002 同一修复。

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
- **状态**：❌ 未对齐（Go `writeSubset` 强制字典序）。与 SPEC-H1-002 同一修复。

---

## 4. HTTP/2 帧层

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

## 5. WebSocket 握手层

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

## 6. header 语义层

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

### SPEC-HDR-006　`accept` 按端点取值

- **规则**：models 端点为 `*/*`
- **依据**：官方基线实测　[L3]
- **观测**：任意通道
- **实测**：`official-h1-full-20260727T124125Z` 的 `GET /codex/models` →
  `accept: */*`
- **可变性**：条件（responses 为 `text/event-stream`，见 SPEC-HDR-00x 未列项）
- **状态**：🔴 **未对齐** —— Sub2API 在
  `openai_codex_models_service.go:318` 设 `Accept: application/json`

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

## 7. body 层

### SPEC-BODY-001　顶层字段为固定结构体

- **规则**：官方 body 由 Rust 结构体 `ResponsesApiRequest` 序列化，
  **不可能携带结构体外的字段**
- **依据**：`codex-api/src/common.rs`　[L1]
- **观测**：任意通道
- **实测**：第三方传入的 `truncation`/`top_logprobs`/`background`/`max_tool_calls`
  及自造字段全部剔除
- **可变性**：固定
- **状态**：✅ 已对齐（顶层改白名单）

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

## 8. 端点选择

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

## 8.5 端点全集（第 1 步产出，2026-07-28）

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
| `backend-api/conversation/` | `openai_alpha_search.go` | ✅ 有 |
| `backend-api/wham/rate-limit-reset-credits` | `openai_quota_service.go` | ✅ 有 |
| `backend-api/subscriptions` | `admin_user.go` | ✅ 有 |
| `backend-api/accounts/check/v4-2023-04-27` | `openai_oauth_service.go` | ✅ 有 |
| `backend-api/files` | `openai_images.go` | ✅ 有（`files.rs`） |
| `backend-api/settings/account_user_setting` | `openai_privacy_service.go` | 🔴 **官方命中 0** |

**`settings/account_user_setting` 是官方从不访问的端点**，需进一步确认它在 OAuth
路径下的可达性与触发条件——若 OAuth 账号会打它，即为明确的非官方特征。

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
- **状态**：🔴 **五条链路 TLS 指纹、header 定型、连接池隔离全部失效**
- **⚠ 这比 P1-5~7 处理的范围更大**。上一轮只收敛了 `count_tokens` / `images` /
  `alpha-search` 三条，而画像失效实际是**系统性的**：只要不在
  `supportsOfficialEgressHTTPProfile` 白名单内就静默 fail-open，新增链路会默认漏网。
  **根因是"默认放行"而非"默认拦截"**，逐条补白名单治标不治本。

---

## 8.6 交接说明（2026-07-28 收盘，**新会话先读这一节**）

### A. 任务定义

用户确定的五步流程，目标是让 Sub2API 的 OpenAI OAuth 出站与官方 Codex CLI 一致：

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | 列规则：读官方源码 + 抓包，列出全部端点形态 | **进行中** |
| 2 | 验规则：逐条抓包确认，标「验过了/⛔验不了」 | 未开始 |
| 3 | 对比：逐条对 Sub2API，产出差异清单 | 未开始 |
| 4 | 改代码：按差异清单改，含**自写 h1 wire**，部署 Vircs | 未开始 |
| 5 | 再抓包验收 | 未开始 |

### B. 已定的决策（不要重新讨论）

1. **范围**：七个端点全做，**只做 OpenAI**，不碰 Anthropic。
2. **暂不换 Rust**。同源方案已 PoC 验证可行（§10），但本轮用现有 Go 改。
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

### C. 工具与环境

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

### D. 相关文档分工

| 文档 | 记什么 |
|---|---|
| **本表** | 官方形态是什么样 + 对齐状态（**唯一事实源**） |
| `OFFICIAL_EGRESS_WIRE_PARITY_FIX_20260727.md` | 我们改了什么（§0 有已修/待修总表） |
| `README.md` §1.1 | 对外的现状与残留差异（§1.1.1.2） |
| `OFFICIAL_EGRESS_PROFILE_FIDELITY_FIX_20260727.md` | 第一轮修复记录 |

### E. 第 1 步未完成项（下次从这里接）

1. **各端点的 header 集合与插入顺序**——逐个读官方 `extra_headers` 构造链，
   再对 Sub2API 逐条比。目前只对齐了 URL 与方法（SPEC-EP-006~009）。
2. **各端点的 body 字段**——models / compact / search / images 的请求体结构未列。
3. **五条画像失效链路的处理方案**——SPEC-EP-011 已定位，但未定怎么修。
   建议一并评估把 `supportsOfficialEgressHTTPProfile` 从"默认放行"改为
   "默认拦截"，否则新增链路仍会漏网。
4. **`count_tokens` 的去留**——SPEC-EP-003 已证官方无对应物，需产品决策。

### F. 本轮未解决但已定位的重点

- **SPEC-TLS-002 代理画像方向相反**：官方默认经代理不 offer h2，而我们的代理
  画像声明 `h2`。该画像很可能整个是观测污染的产物。**修法可能是把代理画像也改成
  空 ALPN**——那样代理路径也走 h1，h2 层全部差异自动消失。**这是尚未验证的推论。**
- **SPEC-EP-011 画像失效是系统性的**：根因是白名单"默认放行"。

## 9. 本版未覆盖

按 §0.4 的范围界定，以下**尚未纳入**，不代表已对齐：

1. **models / compact / 旁路端点**（`count_tokens`、`alpha/search`、`/v1/live`）的
   逐项形态
2. **Anthropic 侧全部形态** —— Anthropic 画像未开启 `LowercaseHeaders`
   （Claude Code 是 Node.js，形态未验证）
3. **连接管理与时序**：连接复用节奏、重试退避、WS 连接池规模（官方为单连接 +
   1 条 prewarm、无 PING；Sub2API 为上限 128、`min_idle` 4、30s PING）
4. **Go 与 Rust 的运行时差异**：TCP 时序等难以写成规格的项

### 9.1 一条必须记住的紧迫性

记忆中的「官方源码基线陷阱」：**Codex 2.1.113 起官方转为二进制分发，新版源码永久
不可得**。本表所有 L1/L2 依据都建立在 0.145.0 源码可读的前提上。源码消失后，抓包
将成为唯一手段，而本表会是唯一能回答"官方为什么是这个形态"的历史锚点。

**趁源码还在，把规格固化下来——现在不做，以后做不了。**

### 9.2 对"官方无法区分"这一目标的诚实评估

本表建成也达不到该目标。已确认的硬边界：SPEC-H1-002/003/004 须自写 h1 wire、
SPEC-H2-003/006/007 须 fork `x/net/http2`，更不论 Go 与 Rust 在 TCP 时序与连接
复用节奏上的差异。

本表能给出的是：**把"我们觉得差不多"变成"差 N 项，每项差多少、为什么改不了、
修它要付什么代价"**。这是能守住的目标；追求绝对不可区分会导致无限投入，且永远
无法证明达成。

---

## 10. Rust 同源方案的可行性验证（2026-07-27）

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

### 10.1 由此得到的两条结论

**一、"用同样的库"≠"自动一致"，还须对齐配置分支。** h2 那组数值在裸 reqwest 下
根本测不到（不 offer h2），加上 `.use_rustls_tls()` 后才逐项吻合。**这恰好也是
SPEC-H2 段观测污染的成因**——两件事是同一个开关。

**二、IPC 协议必须保序。** h1 的自动一致性来自 `HeaderMap` 的插入序。若 Go 侧把
header 用 map / JSON object 传给 Rust，顺序当场丢失，hyper 的保序输出随之失效，
等于绕一圈回到原点。**跨语言协议必须用数组传 header**。

### 10.2 未验证的部分

- **TLS ClientHello 不在"白拿"清单内**：官方 HTTP 默认走 native-tls，底层是
  **系统 OpenSSL**，形态取决于容器内的 OpenSSL 版本而非 Rust 依赖。
  （反过来说官方自身也因用户机器而异，是分布而非单值，要求本就较低。）
- h2 伪头顺序未采到（PoC 连接在 HEADERS 帧前结束）
- 架构代价一分未减：IPC、SSE 流式透传的背压、Go 侧 official_egress 的迁移、
  计费与日志跨进程、Rust 侧重建重试与连接池、以及长期的 Rust 维护能力

### 10.3 据此设想的目标架构

> ⚠ **本小节是方案设想，不是规格。** 本表其余部分记录的是**已验证的事实**（官方
> 形态与对齐状态），本节记录的是**尚未决策的架构方向**。不要把它当作已定的架构。
> Anthropic 侧一并记于此只为不散落，其规格另立文档。

```
Go 主程序（路由 / 鉴权 / 计费 / 调度 / 语义定型）
  ├─ Rust sidecar     → OpenAI      （同源 Codex CLI，已 PoC 验证，见 §10）
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
  §10 已逐项验证
- **语义层**（header 放什么值、body 剔除哪些字段、Lite 变换、顶层白名单）→
  **仍须 Go 实现**。sidecar 不会自动做这些，传什么字段就发什么字段

#### 三条必须遵守的约束

1. **跨语言协议必须保序。** h1 的自动一致性来自 `HeaderMap` 插入序；若用 map /
   JSON object 传 header，顺序当场丢失，等于绕一圈回到原点。**必须用数组。**
2. **必须复刻官方的配置分支，不能只对齐依赖版本。** §10.1 已证：裸 reqwest 经代理
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
