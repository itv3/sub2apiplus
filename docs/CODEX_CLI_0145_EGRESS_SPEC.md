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

- **规则**：10 个 cipher suite，**ALPN = `h2, http/1.1`**
- **依据**：`newOpenAIOfficialEgressHTTPProxyTLSProfile`　[L3]
- **观测**：CONNECT 隧道内的 TLS 握手
- **实测**：h2 探针记录 `negotiated_alpn = h2`
- **可变性**：固定
- **状态**：✅ 已对齐

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
