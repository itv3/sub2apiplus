# Claude Code 客户端仿真与版本演进手册

> **适用范围**：Sub2API 使用 Anthropic OAuth（`authMethod=claude.ai`、`apiProvider=firstParty`）
> 出站时的 Claude Code 客户端仿真
> **取证基线**：`claude-code 2.1.220`——第一、二部分全部规则与证据的绑定版本；发行物身份、
> 摘要与运行时线索见 §2.1.1
> **升级目标**：`stable` 通道 `2.1.224`（2026-08-16 复核 npm：`stable` 2.1.224、`latest` 2.1.233）。
> 基线保持 2.1.220 不变——其 22 个完整运行与 12 项已验证规则是自洽证据资产，整体重新取证的成本
> 远高于增量比较；2.1.220 → 2.1.224 的差异须由第四部分流程逐项证明，且受管工具补齐前不得创建
> 正式升级 Campaign（§4.0.4）
> **机器台账**：`tools/official_client_capture/claude_21220/` 的规则、HitCC 覆盖与 2.1.88 覆盖
> 三份 JSON；逐规则状态以台账为准，正文数字由 §2.16 门禁强制对账
> **文档定位**：本文是 Claude Code 客户端规则、Sub2API 仿真实现与版本演进的人类可读权威入口。
> 本文没有未压缩 TS 源码可用，静态规则均从官方生产 bundle 逆向建立；方法与架构参照
> [`docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](CODEX_CLI_CLIENT_EMULATION_GUIDE.md)，但材料、
> 证据、规则与结论全部独立取自 Claude Code 自身，不继承 Codex 的任何事实结论
> **末次更新**：2026-08-16

---

# 第一部分 总体目标与仿真链路

## 1.1 总体目标与边界

目标是让 Sub2API 使用 Anthropic OAuth 出站时，无论入站来自官方客户端还是第三方标准 API
客户端，都产生与官方 Claude Code 2.1.220 一致的可观测形态。画像函数与仿真链路见 §1.2。

本版范围：

- `authMethod=claude.ai`、`apiProvider=firstParty` 的 Anthropic OAuth；
- TLS、连接、HTTP、header、body、端点及跨请求状态；
- 每条规则绑定二进制 SHA、OS、架构、运行时、`cli`／`sdk-cli` 入口、模型和账号／feature 条件。

API Key、Bedrock、Vertex、Foundry 不属于对齐目标；API Key 仅可作为 OAuth 的差分对照，
本文不沿用未经验证的 beta 结论。

**行为层不作对比维度。** 官方客户端原生支持合法关闭遥测与非必要流量，因此候选出站
「零遥测」是官方允许的配置状态，**不能**作为「不像官方客户端」的判据，也不计入一致性差异。
2.1.220 bundle 中该能力由一个三级隐私模型实现（[A1] 已复验）：

```js
function mRl(){
  if (process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC) return "essential-traffic";
  if (process.env.DISABLE_TELEMETRY) return "no-telemetry";
  if (Yt(process.env.DO_NOT_TRACK))  return "no-telemetry";
  return "default";
}
function ca(){ return mRl() === "essential-traffic" }   // 53 处门控
function V0e(){ return mRl() !== "default" }            // 7 处门控
```

`ca()` 在 bundle 中被调用 **53 次**，`V0e()` **7 次**，门控 mcp-registry、policy_limits、
grove、releaseNotes、feedback、modelCapabilities、referral 等非必要请求。
除文档已列的两个变量外还有第三个开关 `DO_NOT_TRACK`。Codex 侧的对应能力见
`codex-rs/config/src/types.rs` 的 `AnalyticsConfigToml.enabled=false` 与可配 `None` 的
otel exporter（基于与 active 画像同版本的 `codex-cli-0.145` 源码）。

## 1.2 客户端仿真链路

```text
官方生产 bundle、运行证据与旧版本线索
→ 客户端规则画像
→ active 版本画像
→ 统一出站定型
→ 验收、发布与回滚
```

出站形态由以下画像函数唯一决定：

```text
出站 = ClaudeProfile(规范化语义请求, 会话状态, 平台与入口条件, 服务端特性状态)
```

入站客户端类型**不是**画像函数的输入；实现阶段须用语义等价的成对入站请求验证官方入口与第三方
入口收敛。第二部分定义「官方应产生什么行为」，第三部分说明「Sub2API 如何实现」，第四部分规定
版本演进，第五部分处理非版本变更维护。

---

# 第二部分 Claude Code 2.1.220 规则画像

本部分定义规则成立所需的证据标准、观测边界和当前基线的 57 个编号项。它回答「什么行为可以成为
客户端规则」；目标版本如何执行取证并生成新规则，只由第四部分规定。

## 2.1 规则证据与准入标准

### 2.1.1 证据类型与位置

| 等级 | 材料与位置 | 用途 | 限制 |
|---|---|---|---|
| **A1 主** | 2.1.220 Linux 官方 npm 发行物：`local-analysis/sources/claude-code-2.1.220-official/package-linux-x64/`，来源与摘要见同目录 `SOURCE_MANIFEST.json` | 完整生产 bundle 的控制流、常量、条件、sink 和语义锚点；静态能力等同 Codex 的生产源码直读 | 标识符已 minify，符号名不能跨平台或跨版本引用；实际触发仍须运行证据 |
| **A1 交叉** | 2.1.220 Darwin 发行物：`local-analysis/sources/claude-code-2.1.220-official/` | 跨平台语义复核和平台差分 | minify 符号与 Linux 漂移，不能直接搬用符号级结论 |
| **A2** | 锁定的 Bun／SDK 源码 | 解释自动 header、序列化、连接和 TLS | 须证明与内嵌构建对应 |
| **A3** | 2.1.88 TS 源码：`local-analysis/sources/claude-code-2.1.88/src/` | **机制词典**：历史语义、模块、注释和 native 机制 | 不能定义 2.1.220 当前行为 |
| **A4** | HitCC 2.1.197：`local-analysis/sources/hitcc-2.1.197/docs/` | **近期线索地图**：sink、端点、header、feature、retry | 不能复用 minify 符号或单独成为规则证据 |
| — | 抓包：`local-analysis/captures/` | 对应运行范围内的实际 wire／HTTP 观察 | 只证明被采集和保存的内容 |

`strings` 只作应急定位，不能恢复别名写入点及其完整条件；静态结论必须回到提取后的 bundle 控制流。
2.1.88／HitCC 对定位和解释有帮助，但不能替代当前同平台 A1 与 P／R／J 运行证据。

来源清单至少绑定 registry URL、`dist.integrity`、下载时间、包名／版本、tgz 与二进制摘要、平台、
架构和内嵌构建信息。SHA-256 只能证明文件同一，不能单独证明来源官方。

| 载体 | 内容与证明边界 |
|---|---|
| **P** | 原始 pcap：ClientHello、SNI、TLS 扩展、ALPN、连接和时序 |
| **R** | 等长脱敏的客户端应用层原始字节：请求线、header 原始编码、body、压缩和帧 |
| **J** | MITM／解析器记录；J-raw 可证明解析后 H1 header 的字面大小写、顺序和重复项，以及解析后 body 摘要／语义 |
| **M** | manifest 绑定分析：环境、产物摘要、场景结果和分析摘要 |

J-raw 不是 wire 字节：不能证明请求行空白、分块、原始 body 或帧结构。J-normalized 会小写 header
名但保留列表顺序；后续 canonical comparison 还会排序。原始 MITM JSONL 属于 `raw_private`，不得分发。

结论状态只有五种：**候选**（未闭环）、**已观察**（限定运行内有正例）、**已验证**（证据与边界
闭环）、**冲突**（材料不一致）、**不可观测机制**（只能解释内部机制）。可变性另记为固定、随机或
条件；远程 feature、账号资格等无法冻结时必须写成条件。

本文绑定的两个官方发行物如下。SHA-256 只证明文件同一，不能单独证明来源官方；来源清单还须绑定
registry URL、`dist.integrity`、下载时间、包名／版本与平台架构：

| 角色 | npm 包 | 格式 | SHA-256 |
|---|---|---|---|
| 主静态产物与 wire 基准 | `@anthropic-ai/claude-code-linux-x64@2.1.220` | ELF x86_64 | `674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863` |
| 跨平台交叉复核样本 | `@anthropic-ai/claude-code-darwin-arm64@2.1.220` | Mach-O arm64 | `8addc857f3fe64d5a0368af9ee50321b50afb4a6918ba3ef018ab84f5dbbe081` |

Linux 产物与抓包客户端字节级相同，因此它同时是静态基准与 wire 基准；Darwin 只作交叉复核。
两平台内嵌 Bun 均为 `1.4.0+f6d0fcd24`；Linux J 通道上报 `X-Stainless-Package-Version: 0.94.0`，
该值只证明上报内容，不能单独锁定 SDK 实现。

Bun SEA 应先由 Mach-O `__BUN/__bun` 或 ELF `.bun` 定位，再校验 trailer magic 恰在该 section 尾部、
其前 32 字节为 Offsets、模块记录为 52 字节；不能用全文件 magic 搜索代替结构校验。主模块均为完整 JS：

| 产物 | 模块数 | 主模块 | 大小 | section 偏移／长度 |
|---|---:|---|---:|---|
| Linux x86_64 | 8 | `/$bunfs/root/src/entrypoints/cli.js` | 21,633,085 B | `.bun` 86315008／188646561 |
| Darwin arm64 | 14 | 同上 | 21,635,672 B | `__bun` 64831488／191359127 |

因此 A1 不是字面量库。minify 保留控制流、常量和条件，但局部符号会跨平台漂移；正式锚点分两层：

1. **产物锚点**：包来源／integrity、平台、二进制 SHA、模块路径、bundle SHA 和提取器 SHA；section
   偏移／长度只作该产物的提取记录。落地为 `bundle-anchors-linux-x64.json`。
2. **语义锚点**：稳定 header／gate／环境变量字面量、网络 sink，以及局部标识符 α-归一化后的
   子树摘要。落地为 `reachability-index-linux-x64.json` 与 `CANDIDATE_EVIDENCE.json`。

minify 符号名、行号、字节跨度只能辅助定位，不能作为跨平台或跨版本身份。α-归一化把非保留标识符
按首次出现顺序替换为占位符，保留关键字、成员属性名和全部字面量，因此同一段逻辑在两个平台上得到
同一摘要——原 8 条候选中 6 条锚点跨平台完全一致，尽管符号名分别为 `S8s`／`Tqs`、`Vtp`／`Ftp`；
其余 2 条逐词差异仅为构建标识常量 `BUILD_SOURCEMAP_GROUP`（Linux `"default"`／Darwin `"darwin"`）
落在锚点窗口内，出站逻辑本身一致。本轮新增的 client-app、remote-container、remote-session 与
parent-agent 四个结构锚点在 Linux／Darwin 各唯一命中，α 摘要均逐项完全一致。

### 2.1.2 规则准入与观测边界

`SPEC-*` 在本文件中只是可追踪命题 ID，**编号本身不代表已验证**；历史候选、已观察命题和已取代
命题都可保留编号。只有同时满足以下条件的原子命题才能标为 `disposition=verified`，并进入正式
Sub2API 实现合同：

1. 范围、官方来源、实际二进制、平台、运行环境和采集器均已绑定；
2. 需要静态解释时，证据来自运行产物或已证明等价的产物，并确认到网络 sink 可达；
3. 使用能观察命题的 P／R／J／M；J-raw 仅支持声明边界内的解析后 H1 命题，原始 wire 必须有 R；
4. 条件命题完成正负例、单变量对照和独立重复；未知远程状态已收窄为条件；
5. 全称结论具有无 host 预过滤发现、静态枚举和声明范围内的场景覆盖；
6. 锚点、运行号、证据及提取器摘要、秘密扫描和索引均可复算；
7. 实现要求只规定可见结果，且规则、场景、证据和实现断言之间无未分类项。

每条规则须声明停止条件和适用边界。版本／包摘要、内嵌 Bun／SDK、平台、entrypoint、provider、
关键配置、远程 feature 或采集器变化时重新发现和分类；minify 文本变化本身既不证明规则变化，
也不能作为继承旧规则的理由。

不同命题的最低闭环要求：

| 命题 | 最低要求 |
|---|---|
| TLS／SNI／ALPN | 同平台、同二进制 direct P 和独立重复；静态证据可不适用 |
| 解析后 H1 header 大小写／顺序 | 同平台 J-raw，并声明客户端→MITM 解析边界。重复项另需实际样本，而所选 `BASELINE-J` 的 5 个请求均无重复 header 名，该类命题当前**无正例** |
| 原始请求线／header 编码／body／压缩／帧 | 同平台 R；J 或对象构造顺序不能代替 |
| Header／body 存在性和值 | 当前产物 sink 可达证据 + J 或 R；条件项须差分 |
| 连接、retry、跨请求状态 | P／R／J 流级关联 + 成功／失败输入 + 重复 |
| 内部机制 | A1 或已锁定 A2；只有 A3／A4 时仍是机制假说 |
| 端点全集／全称否定 | 静态 sink 枚举 + 无 host 预过滤发现 + 明确场景边界 |

必须遵守的观测边界：

- Linux 同 SHA 产物是静态基准；Darwin 只作交叉验证，minify 符号不得跨平台引用。
- 当前抓包由 `claude -p` 驱动，只代表 `sdk-cli`；`cli` 必须单独采集，且必须在有真实 TTY 的
  环境里驱动 TUI。`.claude.json` 的 `hasCompletedOnboarding` 控制是否走首次引导，
  **logout 会把它重置为 false**——换账号后首次跑 TUI 必然遇到引导页。
- MITM 会改变 TLS。J-raw 支持解析后 H1 header 名与序；原始请求行、分隔符与 body 字节须用 R，
  R 通道已建立（`upstream_byte_relay.py`，两条 TLS 腿之间只复制明文字节）。
- 当前 Claude ClientHello 只提供 `http/1.1`；这排除本批样本的 H2 帧命题，不补足 H1 原始字节证据。
- `X-Stainless-Package-Version` 只证明上报值，不能单独锁定 SDK 实现。
- direct BPF 默认按 host 预过滤，会让端点命题变成循环论证；`CAPTURE_BPF_ALL_HOSTS=1` 可退化为
  只按端口过滤，端点类命题必须用该模式采集。正例仍不能证明“从未出现”。
- MITM addon 已补 `request`／`error`／`server_connect`／`server_disconnected` 钩子，失败流与连接
  生命周期落在 `lifecycle-*.ndjson`，带 flow ID 可与 HTTP 记录关联。
- 重试链路只能靠 `--fault-spec` 受控注入取正例；此类样本只证明客户端收到该输入后的反应，
  必须与自然样本分开引用。
- 账号、scope、远程 feature、模型能力和服务端响应属于自变量；可关闭的遥测不作为必现规则。
- **本文全部运行证据均在 `essential-traffic` 模式下采集**：采集器的 `PRIVACY_ENV` 固定注入
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 与 `DISABLE_TELEMETRY=1`。这不是缺陷——
  它与「行为层不作对比维度」一致——但它使端点类结论只在该模式下成立：`default` 模式会放行
  另外 53 处门控请求，端点集合必然更大。任何端点全集命题都必须声明隐私模式。

### 2.1.3 日常复算

日常复算分两类：仓库门禁与证据复算。门禁命令与逐项检查内容见 §2.16；它不发送真实请求，但要求被
`.gitignore` 排除的 `local-analysis/` 证据树完整存在，不是脱离证据归档即可运行的普通 CI 单测。

现有证据的复算入口如下：

现有 P 类证据可复算：

```bash
python3 tools/official_client_capture/pcap_clienthello.py --by-subdir \
  --dir local-analysis/captures/official-egress-final-review-fix-20260727-094500/official-client/oauth-oauth-p0p2-zstd-final-20260726T1420Z/direct/claude-http
```

只引用输出中的 ClientHello／SNI／ALPN 等原始字段，不引用其 Codex `SPEC-*` 判语。

A1 复算入口（提取、结构校验、产物锚点、sink 索引与候选语义锚点一次产出）：

```bash
python3 tools/official_client_capture/extract_claude_bundle.py \
  --binary local-analysis/sources/claude-code-2.1.220-official/package-linux-x64/claude \
  --expected-sha256 674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863 \
  --out-dir <提取目录> \
  --emit-anchors local-analysis/sources/claude-code-2.1.220-official/bundle-anchors-linux-x64.json \
  --emit-reachability-index local-analysis/sources/claude-code-2.1.220-official/reachability-index-linux-x64.json
```

该入口对 SHA 不匹配、非 Bun SEA 容器和被篡改的 trailer magic 均返回非零退出码，且同一二进制在不同
输出目录下产出相同锚点。Darwin 交叉样本用 `--check-only` 复核。

### 2.1.4 静态分析工具的能力边界

`extract_claude_bundle.py` 与 `claude_bundle_reachability.py` 只依赖标准库。可达性判定的实际强度
**弱于「调用图可达」**，使用时必须按下列边界解读：

- 已实现的是「写入点之前最近的顶层符号」加「前后 20 KB 窗口内是否存在网络 sink」，**不是数据流
  证明**。因此**肯定命题**（某 header 在某条件下会被发送）不能只靠静态可达性升格，必须有运行正例。
  SPEC-HDR-009／010、SPEC-BETA-001 已取得各 2/2 个注入正例，但旧 ID 仍合并多个原子结果，且绑定的
  未注入负例未纳入各自证据分母，所以当前只是样本观察，尚未原子化升格。
- **否定命题**同样必须限制证明范围。SPEC-HDR-008 的 `Og=oOl(ifh,null)` 加空 schema 只解释环境
  分支；`probe-dispatch-r1/r2` 的 2/2 个注入请求仍未出现 Header，只支持已采样本观察。远程 gate
  正例和绑定的未注入对照均缺失，不能据此推出“默认永不发送”或把静态近似当成完整否证。
- 声明分段不依赖全局括号配平。手写词法器在 21 MB minify 代码上仍有 2 处括号失配（已定位其一：
  模板字面量内 `http://` 的 `//` 被当作行注释），一次失配就足以让 40 字节的函数被算成 17.7 MB。
  改用声明锚定分段后对局部失配免疫，但分段是作用域的近似，`enclosing_symbol` 不等于所属函数。
- 顶层符号靠启发式识别：全 bundle 中只声明一次且名字长度不小于 2。实测能干净分开局部变量
  （`t` 声明 6379 次）与顶层符号，但这是经验规则，不是语言语义保证。

因此静态锚点可用于**否证跨版本同一性**（锚点变化即逻辑发生变化、跨平台差异可量化），但不能
单独证明运行时缺失；确认类命题和运行负命题仍须各自的运行证据闭环。

原采集账号（组织 `e6912c7a…`）曾返回 `400 This organization has been disabled.`，已更换为组织
`783b4782…`；两个组织的已采样本形态相容，但 SPEC-TLS-001 与 SPEC-HDR-001 均只到「已观察」，
不能据此证明账号或订阅档位全称不影响出站形态。扩大规则覆盖面还须补齐：

1. ~~归档 Linux npm 来源清单，实现确定性提取、bundle 锚点和 sink 索引~~ —— 已完成；
   AST／调用图可达性判定仍只达到 1.4.1 所述强度，需要真正的数据流分析才能满足准入条件第 2 条；
2. ~~以 Linux bundle 重做候选条件，Darwin 仅用于平台差分~~ —— 已完成，见 2.12 候选台账及机器台账；
3. client-app、remote container、remote session、agent 深度 1—3 和 500 count=3／5／9 已由
   本轮完整 M campaign 补齐；其余环境变量条件仍须拆分原子命题并绑定未注入负例，另缺 TUI、
   compact、其他非主会话，以及其他状态码／`Retry-After`／精确终止边界；
4. 为现有 R 补完整 M 绑定，并补无 host 预过滤发现、全生命周期 flow ID、秘密扫描、证据索引和
   实现断言映射。

## 2.2 当前规则分组与验收范围

本章当前共 **57 个编号项**。“Sub2API 需对齐项”表示该编号属于实现 1.1 目标时必须处理的责任；
“当前验证状态”表示官方行为证据的充分度，不代表 Sub2API 已经实现。与 Codex 文档相同，本轮拆分
或升格的 12 条规则统一按“范围—规则—静态—实测—实现—状态”阅读；先看本表和 2.2 即可确定
Sub2API 的责任边界。历史复合「已观察」条目保留额外边界说明，待以后原子化时再套用同一模板。

<!-- CLAUDE_ALIGNMENT_SUMMARY_START -->
| 分组 | 条数 | 当前验证状态 | Sub2API 需对齐项 |
|---|---:|---|---:|
| **① TLS** | **3** | 🟡 3 | **3** |
| **② HTTP 协议** | **1** | 🟡 1 | **1** |
| **③ Header** | **26** | ✅ 10；🟡 15；排除 1 | **25** |
| **④ Body** | **16** | 🟡 16 | **16** |
| **⑤ 端点** | **4** | 🟡 4 | **4** |
| **⑥ 连接与重试** | **3** | ✅ 2；🟡 1 | **3** |
| **⑦ Beta 条件机制** | **1** | 🟡 1 | **1** |
| **⑧ 响应兼容** | **3** | 下游响应记录 | **0** |
| **合计** | **57** | **✅ 12；🟡 41；排除 4** | **53** |
<!-- CLAUDE_ALIGNMENT_SUMMARY_END -->

对固定转发到 Anthropic firstParty OAuth 上游的实现口径如下：

```text
官方客户端入站 ─┐
                 ├─→ 规范化语义请求 → Claude220Profile
第三方 API 入站 ─┘                         ↓
                     Lifecycle／State／Endpoint／Header／Body／Connection／Transport
                                               ↓
                                      Anthropic OAuth 上游
```

官方客户端与第三方标准 API 的入站必须先归一化为等价语义请求，再进入同一个不可变版本画像和同一组
最终定型层。入站 `User-Agent`、客户端版本、Header 顺序或客户端类别不得直接决定出站形态。条件规则
只在画像条件成立时输出；动态值只对齐格式、来源、关联关系和生命周期，不固定某次抓包值；未被画像
消费的第三方入站 Header 不得直接透传到官方上游。

对固定转发到 Anthropic firstParty OAuth 上游，Sub2API 当前需要对齐 **53 项**：2.2.1 的 32 项主画像
责任，加 2.2.2 的 21 项条件责任。12 项已经达到第一章准入，可直接作为硬验收合同；41 项只有有限
运行观察，只能作为带范围的暂行画像，实施后仍不得声称“53/53 已验证”或“完整画像”。4 项排除项
不属于客户端出站实现。

本轮原 5 个待补证编号已全部原子化并闭环，所以**已编号待补证为 0**。这不表示发现工作已经穷尽：
HitCC、2.1.88 源码和未编号 `CAND-*` 仍有未闭合线索。尤其是 `CAND-AUTH-BEARER`、完整
ClientHello、原始 HTTP Header 合同、工具／compact／其他非主会话、辅助端点，以及 500 之外的
错误类别和 `Retry-After` 仍会阻止 1.1 最终封口。

机器责任枚举仍为
`egress_required／egress_conditional／egress_pending_evidence／downstream_response_compat／none_superseded`，
本轮数量依次为 32／21／0／3／1；前两类合计即 Sub2API 需对齐的 53 项。

<!-- CLAUDE_ALIGNMENT_REQUIRED_START -->
### 2.2.1 主画像必需：32 项

`SPEC-TLS-001`、`SPEC-TLS-002`、`SPEC-TLS-003`、`SPEC-PROTO-001`、
`SPEC-HDR-001`、`SPEC-HDR-002`、`SPEC-HDR-003`、`SPEC-HDR-004`、`SPEC-HDR-005`、
`SPEC-HDR-006`、`SPEC-HDR-007`、`SPEC-HDR-008`、`SPEC-HDR-012`、`SPEC-HDR-013`、
`SPEC-HDR-020`、`SPEC-BODY-001`、`SPEC-BODY-002`、`SPEC-BODY-003`、`SPEC-BODY-007`、
`SPEC-BODY-008`、`SPEC-BODY-009`、`SPEC-BODY-010`、`SPEC-BODY-011`、`SPEC-BODY-012`、
`SPEC-BODY-013`、`SPEC-BODY-014`、`SPEC-BODY-015`、`SPEC-BODY-016`、`SPEC-EP-001`、
`SPEC-EP-002`、`SPEC-EP-003`、`SPEC-EP-004`。
<!-- CLAUDE_ALIGNMENT_REQUIRED_END -->

“必需”表示属于主画像或主画像生命周期，不表示每个推理请求都出现该现象。例如 `SPEC-HDR-013`
需要跨请求才能观察复用；`SPEC-EP-002` 是生命周期责任，不能据此在每个推理请求前机械发送探测。
这 32 项当前均为 🟡「已观察」，可以做带范围的暂行实现，但还不是正式验收完成数。

<!-- CLAUDE_ALIGNMENT_CONDITIONAL_START -->
### 2.2.2 条件触发时必需：21 项

`SPEC-HDR-009`、`SPEC-HDR-010`、`SPEC-HDR-014`、`SPEC-HDR-015`、`SPEC-BODY-004`、
`SPEC-BODY-005`、`SPEC-BODY-006`、`SPEC-CONN-001`、`SPEC-BETA-001`、
`SPEC-HDR-016`、`SPEC-HDR-021`、`SPEC-HDR-022`、`SPEC-HDR-017`、`SPEC-HDR-023`、
`SPEC-HDR-018`、`SPEC-HDR-024`、`SPEC-HDR-019`、`SPEC-HDR-025`、`SPEC-HDR-026`、
`SPEC-CONN-002`、`SPEC-CONN-003`。
<!-- CLAUDE_ALIGNMENT_CONDITIONAL_END -->

触发条件分别来自官方画像环境、子 agent、轮次、工具状态、服务端故障和 beta。第三方入站只有在
规范化语义明确表达同一条件时才能触发，不能因为携带同名 Header 就绕过画像条件。
其中 `HDR-016/017/018/019/021..026`、`CONN-002/003` 共 12 项为 ✅「已验证」；其余 9 项为
🟡「已观察」。条件未成立时必须省略相应形态，不能为了固定 Header 数而发送空值。

<!-- CLAUDE_ALIGNMENT_PENDING_START -->
### 2.2.3 已编号待补证：0 项

原 5 个候选已拆成 12 个原子编号并由本轮 22 个完整 M 运行闭环；当前没有
`egress_pending_evidence` 编号。未编号线索仍保留在 2.12 候选台账，不得把“0 项”解释为候选清零。
<!-- CLAUDE_ALIGNMENT_PENDING_END -->

<!-- CLAUDE_ALIGNMENT_EXCLUDED_START -->
### 2.2.4 不计入客户端 egress：4 项

`SPEC-HDR-011`、`SPEC-RESP-001`、`SPEC-RESP-002`、`SPEC-RESP-003`。
<!-- CLAUDE_ALIGNMENT_EXCLUDED_END -->

其中 `SPEC-HDR-011` 已由 `SPEC-HDR-020` 与 `SPEC-BODY-007` 取代；三条 `RESP` 规则属于下游响应
兼容责任。

每个 `target_1_1=true` 的编号预留成对验收编号 `PAIR-<SPEC-ID>`。实现阶段必须让官方入口和第三方
入口分别执行该断言；对于相同规范化语义与画像条件，两侧必须命中相同的可见结果。动态字段按规则声明的格式、
相等关系和生命周期比较，不比较一次抓包中的字面值。当前实现位置和断言代码尚未审计，统一记为
`not_assessed`。证据已验证只表示官方规则闭环，不表示 Sub2API 已实现或通过成对验收。

## 2.3 证据状态与具名画像

运行证据仍只覆盖 Linux x86_64、`sdk-cli`、Sonnet 5 及已声明的受控环境／故障场景，不能外推到
TUI、其他模型与端点全集。完整 Linux bundle 足以可靠建立静态规则命题，该包、提取器和语义锚点均已
入库并可复算；只用 `strings` 不足以升格。历史基线根目录有 22 个 manifest（18 个 `complete`、
4 个 `failed`），其中旧 P／J 观察仍受原 M 缺口约束；R 类 `relay-r1/r2` 与 `subagent-r1/r2`
同样只作补充观察。

本轮另归档 `claude-code-2.1.220-pending-evidence-20260801/runs/` 下 **22 个运行且全部 `complete`**；
每个运行都绑定实际客户端 SHA、argv、完整环境、采集器摘要、宿主运行回执、运行内与终态精确秘密扫描及清理结果，
`m_binding.complete=true`。这些运行只闭环 client-app、remote container、remote session、
agent parent 以及 `500` 且无 `Retry-After` 的重试曲线，共支持第二章 12 个原子编号升级为「已验证」；
它们不会自动升级其余旧观察。尚未攻克的采集能力包括 `cli`（TUI）入口、compact、多工具／MCP、
其他条件 Header、其他错误类别／`Retry-After` 和辅助端点触发。

现有成功 manifest 位于 `local-analysis/captures/official-egress-final-review-fix-20260727-094500/`
`official-client/oauth-oauth-p0p2-zstd-final-20260726T1420Z/manifest.json`。它绑定 Linux 客户端 SHA、
`firstParty`／`claude.ai`、Sonnet 5、`s1`／`s2`／`s4` 和 direct／MITM，但未绑定实际 argv、采集脚本
摘要和完整环境，且 `runtime_image_verified=false`、`secret_scan.performed=false`。
因此它能把选定证据文件绑定到一次 `complete` 归档，**不能满足 §2.1.2 第 1、6 条所要求的完整 M**；
第二章凡依赖这些运行的命题只能记为有限样本「已观察」，不得用“文件摘要已绑定”替代完整准入。

与它分开，本轮 `claude-code-2.1.220-pending-evidence-20260801/runs/` 的 22 个 manifest 全部满足
`m_binding.complete=true`，并带宿主运行回执、实际 argv／环境、采集器摘要、运行内与终态精确秘密扫描和清理结果。
只有第二章显式引用这些 run 的 12 个原子规则使用该完整 M；不得用新 campaign 替旧 P／R／J 观察补 M。

36 个历史编号已经逐条复核，**不再使用「36/36 已验证」**。历史复合 ID 即使样本事实一致，也必须
先收窄或拆成原子命题，并满足域级最低证据与可复核 M 绑定，才可进入「已验证」。本轮只有
`SPEC-HDR-016/017/018/019` 和 `SPEC-CONN-002` 的收窄命题完成了这一步；其余历史编号维持原状态。

本轮新增十四个原子编号：`SPEC-HDR-020` 区分 HTTP Header 缺席；`SPEC-BODY-007` 只保留
billing attribution 前缀；`SPEC-BODY-008` 至 `016` 分别记录六个基线 Body 字段及 attribution 的
version、entrypoint、cch；`SPEC-EP-003/004` 分离 method 与 HTTP host；`SPEC-TLS-003` 单独记录
TLS SNI。原 `SPEC-EP-001` 已收窄为 request path。所有这些编号当前均为「已观察」。

继上轮十四个原子编号后，本轮新增七个编号：`SPEC-HDR-021` 至 `026` 分离条件 Header 的值、
UA 联动、直接父级关系和组合顺序；`SPEC-CONN-003` 单独记录九次失败后的第十次请求。由此当前
共 57 个编号，不复用旧 ID。

因此，对“能否按第一章可靠得出第二章规则”的回答是：**能可靠得出已完成证据闭环的原子规则，
但不能据此推出完整 Claude Code 2.1.220 发包画像。** 当前正式已验证为以下 **12 项**：
`SPEC-HDR-016/017/018/019/021/022/023/024/025/026`、`SPEC-CONN-002/003`；其余 41 项仍是
「已观察」。原先“29 条 verified ID”“6 条正式原子规则”及“正式为 0”的口径均已废止。

历史专项根目录 `local-analysis/captures/claude-spec-baseline-20260801/` 有 22 个 manifest：
18 个 `complete`、4 个 `failed`；R 类 `relay-r1/r2` 与 `subagent-r1/r2` 没有完整 manifest，故其中
旧命题仍只支持「已观察」。本轮完整 M 根目录
`local-analysis/captures/claude-code-2.1.220-pending-evidence-20260801/runs/` 有 22 个 manifest 且全部
`complete`。目录总数仍不是任一规则的分母；每条已验证规则只引用自己的正负例和运行号。
机器汇总 `pending-evidence-analysis.json` 为 `passed=true`，文件 SHA-256 为
`d7b6681e79531b0a84600c83bb2116eeb3c3932955e8e7f86e292e23e810ea11`，22-run campaign 绑定摘要为
`b2bdc967114b9bd4001ee8da0feec0f79e5a2490fc9bf03dc59780298a454d98`。22 份终态秘密扫描回执还逐一
绑定最终 `manifest.json`、运行目录完整文件清单及冻结扫描器 SHA-256
`af062e03f0e45315562577c07580185232d48b7db13be36cc31cba7d0626c035`；全部回执均为
`passed=true`、`matches=[]`、`scan_errors=[]`。22 个 run 的采集执行源按
各自 host receipt 闭合，共出现四个摘要版本，分布为 12／8／1／1；不得误写成全部运行使用同一源码摘要。

权威适用范围为：Linux x86_64、二进制 SHA-256 `674f61f2…`、`sdk-cli`、
`claude.ai`／`firstParty` OAuth、官方 base URL、`essential-traffic`。模型、轮次、工具、agent 层级、
环境变量、远程 gate 与服务端响应均是条件维度，不得再用「同上」把它们隐式外推。

为避免重复文字造成范围漂移，规则使用以下具名画像；引用画像不是继承其他规则：

- `SCOPE-BASELINE`：上述 Linux SHA；`sdk-cli`；firstParty OAuth；官方 base；essential-traffic；
  Sonnet 5；前台主会话；clean environment；已采 s1／s2／s4 成功请求。**它是抓包基线画像，
  不是“CLI 无参数默认值”**。当前采集器源码会构造 `-p --model claude-sonnet-5 --safe-mode
  --no-chrome --disable-slash-commands --prompt-suggestions false --no-session-persistence
  --max-budget-usd 0.25`，并按场景显式设置 `--tools`；但现有 manifest 没有归档 argv，故这些参数
  只解释画像来源，不作为已绑定运行事实。另 `oauth-claude-newaccount-baseline` 的 manifest 未记录
  `injected_probe_env`，独立 `oauth-probe-baseline-r2` 才明确记录为空对象；不得把字段缺失说成已记录
  空值。正式命题只约束 J 中实际可见的字段和值；第二章旧标题或说明里若仍出现“默认”，也只能按
  `SCOPE-BASELINE` 的“无额外探针基线”解读，不能外推为 CLI 无参数默认行为。
- `SCOPE-DIRECT`：`SCOPE-BASELINE` 的 direct 非 MITM TLS 连接。
- `SCOPE-J6`：精确枚举 `BASELINE-J` 的 s1／s2／s4 三个 J-raw 文件和
  `PROBE-BASELINE-J` 的 s1 文件；取其中全部 boundary=`official_cli_to_official_platform`、
  category=`claude`、subject=`claude-http` 且 `request` 为对象的记录，共 6 条。**这些文件不是
  无预过滤发现结果**：manifest 预设 `target_hosts=[api.anthropic.com]`，当前 MITM addon 先按
  `TARGET_HOSTS` 丢弃其他 host，再按 path 含 `/messages` 路由到 `claude-http.jsonl`。因此
  `SPEC-EP-004` 的 host 是采集预条件，`SPEC-EP-001` 的精确 path 也受 `/messages` 部分预筛；两条
  都不声明 `verification_denominators`。method、HTTP 版本、Header 和 Body 待证字段未参与这两个
  路由条件，另外 16 条有限样本规则仍可声明独立于各自待证字段的分母。当前 addon 摘要未进入运行
  manifest，这也是完整 M 缺口。
- `SCOPE-P8`：精确枚举 `BASELINE-P` 三个 direct pcap 与 `HISTORICAL-220` 的 s1 pcap；
  以原始 pcap 解析出的全部 8 个 ClientHello 为分母，不按待证 ALPN、SNI 或扩展值筛选。
- `SCOPE-ALLHOSTS-P4`：精确枚举 `oauth-discover-allhosts-r1/r2` 的两个 direct s1 pcap；
  以原始 pcap 解析出的全部 4 个 ClientHello 为分母，不按待证 SNI 或 ALPN 筛选。
- `SCOPE-BASELINE-NO-TOOLS`：`SCOPE-BASELINE` 中显式传入 `--tools ""` 的 s1／s2；不包含显式
  启用 Bash 的 s4。argv 本身仍受上述 manifest 缺失边界约束，正式结论只称已采记录中的空数组。
- `SCOPE-SUBAGENT-L1`：同一 Linux SHA／入口／认证下，一层 `general-purpose` 子 agent；当前只有
  缺完整 M 的 R 观察。
- `SCOPE-FAULT`：`SCOPE-BASELINE` 加 manifest 明确记录的单一受控故障。
- `SCOPE-PND-NEG2`：`oauth-pnd-base-neg-01c-20260801`、`oauth-pnd-base-neg-02-20260801`；
  两轮均未注入 client-app、container 或 remote-session 变量，各有一个主推理请求。
- `SCOPE-PND-HDR016`：`oauth-pnd-hdr016-pos-01-20260801`、
  `oauth-pnd-hdr016-pos-02-20260801` 两轮 client-app 单变量正例。
- `SCOPE-PND-HDR017`：`oauth-pnd-hdr017-pos-01-20260801`、
  `oauth-pnd-hdr017-pos-02-20260801` 两轮 container ID 单变量正例。
- `SCOPE-PND-HDR018`：`oauth-pnd-hdr018-pos-01-20260801`、
  `oauth-pnd-hdr018-pos-02-20260801` 两轮 remote session ID 单变量正例。
- `SCOPE-PND-COMBO2`：`oauth-pnd-hdr-combo-a2-01/02-20260801`；三项环境 Header 同时开启，
  每轮产生深度 `0→1→2→1→0` 的五个推理请求。
- `SCOPE-PND-AGENT6`：`oauth-pnd-agent-a1-01/02-20260801`、
  `oauth-pnd-agent-a2-01/03-20260801`、`oauth-pnd-agent-a3v-01-20260801`、
  `oauth-pnd-agent-a3t-02-20260801`；分别独立重复深度 1、2、3 的往返链。
- `SCOPE-PND-RETRY6`：`oauth-pnd-retry-c3v-01/02-20260801`、
  `oauth-pnd-retry-c5v-01/02-20260801`、`oauth-pnd-retry-c9v-01/02-20260801`；只注入
  `status=500`、count 分别为 3／5／9，且响应不带 `Retry-After`。
- `SCOPE-RESPONSE`：`SCOPE-BASELINE` 请求对应的官方上游响应；属于 response_compat 域。

机器台账中的 `verification_denominators` 只用于待证字段未参与上游捕获或路由选择的有限证据分母；
字段名不表示命题已达到 `disposition=verified`。正式状态只读每条规则的 `status.disposition`。

机器台账位于：

- `tools/official_client_capture/claude_21220/rules_2_1_220.json`；
- `tools/official_client_capture/claude_21220/hitcc_2_1_197_coverage.json`；
- `tools/official_client_capture/claude_21220/source_2_1_88_coverage.json`。


## 2.4 TLS

### SPEC-TLS-001 ClientHello 扩展类型序列固定

- **范围**：`674f61f2…` Linux x86_64；`sdk-cli`；`claude.ai`／`firstParty`；direct（非 MITM）。
- **规则**：ClientHello 的扩展类型按 `0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21`
  排列。命题只覆盖台账列出的已采 direct 场景；不外推未采模型或失败链。
- **静态证据**：不适用。TLS 画像由内嵌 Bun 的 TLS 栈决定，bundle 内无可固定该序列的常量。
- **运行证据**：P；`BASELINE-P` 三个场景共 6 个 ClientHello，加 `HISTORICAL-220` 的 s1
  两个 ClientHello，所选证据合计 8/8 命中该扩展序列。目录中的其他 run 不计入本条分母。
- **边界**：仅对该二进制、该平台成立。最早的 `oauth-20260726T011739Z`（`status=failed`）中有
  一个无 ALPN、仅 10 项扩展的 ClientHello，不计入本条；它说明失败链路可能产生不同形态。
- **Sub2API 实现要求（暂定）**：同平台复刻该扩展序；不得把其他平台或其他 TLS 栈的画像套用于此。
- **状态**：**已观察**；原 ID 同时合并精确序列与跨场景不变性，未拆成原子命题。

### SPEC-TLS-002 只 offer `http/1.1` 一种 ALPN

- **范围**：`SCOPE-P8`。
- **规则**：所选八个 ClientHello 的 ALPN 扩展都只包含 `http/1.1`，均未 offer `h2`。
- **静态证据**：不适用，理由同上。
- **运行证据**：P；`BASELINE-P` 三个场景 6 个 ClientHello，加 `HISTORICAL-220` 的 s1
  两个 ClientHello，合计 8/8 的 ALPN 均为 `["http/1.1"]`。
- **边界**：这排除了本批样本的 HTTP/2 帧命题，但不构成「客户端永不使用 h2」的全称结论——
  其他 provider 分支未采集。
- **Sub2API 实现要求（暂定）**：该样本画像只 offer `http/1.1`；出现 `h2` 与当前观察不一致。
- **状态**：**已观察**；P 已由原始 pcap 复算为 8/8，但 manifest 未归档实际 argv、采集器与
  完整环境，runtime image 未独立验证且未执行秘密扫描，未满足完整 M 准入。

### SPEC-TLS-003 所选 allhosts 样本的 SNI

- **范围**：`SCOPE-ALLHOSTS-P4`。
- **规则**：全部 4 个 ClientHello 的 SNI 均为 `api.anthropic.com`。
- **静态证据**：不适用；这是 TLS wire 命题。
- **运行证据**：P；`oauth-discover-allhosts-r1/r2` 的 BPF 均为 `tcp port 443`，未按 host 预过滤；
  机器门禁从两个原始 pcap 独立解析全部 4 个 ClientHello，4/4 的 SNI 命中该值，并与派生 JSON 对账。
- **边界**：只覆盖 `essential-traffic`、tcp/443、s1 两轮；不是 host 或端点全集，不能与 J 层
  HTTP host 合并为一个命题。
- **Sub2API 实现要求（暂定）**：该有限画像的 TLS SNI 使用 `api.anthropic.com`。
- **状态**：**已观察**；原始 P 事实可复算，但完整 M 未闭合。本条承接原 `SPEC-EP-001` 混入的
  TLS SNI 子命题。

## 2.5 协议与连接

### SPEC-PROTO-001 应用层协议为 HTTP/1.1

- **范围**：`SCOPE-J6`；MITM J 解析侧。
- **规则**：所选六个 J 记录的 `http_version` 均为 `HTTP/1.1`；不从另一组 direct pcap 的
  ALPN 反推因果关系。
- **静态证据**：不适用。
- **运行证据**：J；`BASELINE-J` 的 5 个请求与 `PROBE-BASELINE-J` 的独立 s1 请求合计
  6/6，`http_version` 均为 `HTTP/1.1`。
- **边界**：J 记录的是 MITM 解析结果；原始 wire 的请求行与分块形态需要 R，当前没有。
- **Sub2API 实现要求（暂定）**：该有限画像使用 HTTP/1.1。
- **状态**：**已观察**；J 的 6/6 解析事实成立，但完整 M 未闭合，且 J 只支持解析层协议命题。

## 2.6 Header

### SPEC-HDR-001 基线路径的 22 项 header 名与顺序

- **范围**：`SCOPE-BASELINE`。
- **规则**：请求按此顺序发送 22 个 header：`Accept`、`Authorization`、`Content-Type`、
  `User-Agent`、`X-Claude-Code-Session-Id`、`X-Stainless-Arch`、`X-Stainless-Lang`、
  `X-Stainless-OS`、`X-Stainless-Package-Version`、`X-Stainless-Retry-Count`、
  `X-Stainless-Runtime`、`X-Stainless-Runtime-Version`、`X-Stainless-Timeout`、
  `anthropic-beta`、`anthropic-dangerous-direct-browser-access`、`anthropic-version`、
  `x-app`、`x-client-request-id`、`Connection`、`Host`、`Accept-Encoding`、`Content-Length`。
  大小写按上述字面。固定。
- **静态证据**：不适用于顺序本身；顺序由 SDK 与 Bun HTTP 栈的写入次序决定。
- **运行证据**：J-raw，`BASELINE-J` 的 5/5 个请求完全一致；**R** 目录
  `claude-wire-r-20260801/relay-r1`／`relay-r2` 另观察到同一名称、大小写和顺序，
  但该 R 目录缺完整 M，不并入正式分母。
  条件探针产生的 23 项形态见 SPEC-HDR-009／010。
- **已观察补充（R 独有）**：请求行为 `POST /v1/messages?beta=true HTTP/1.1`；名值分隔符是
  **冒号加一个空格**；每行以 **CRLF** 结束，22 行全部如此。这些是 J 证明不了的。
- **条件插槽补充**：这 22 项是基础序列，A1 与专项组合运行显示两个条件构造槽：

  | 槽 | 位置 | 内容与槽内顺序 |
  |---|---|---|
  | **A** | `X-Claude-Code-Session-Id` 之后（基础第 4 项后） | `ANTHROPIC_CUSTOM_HEADERS` 解析出的各项 |
  | **B** | `x-app` 之后、`x-client-request-id` 之前 | 最终解析顺序为 `x-claude-code-agent-id` → `x-claude-code-parent-agent-id` → `x-claude-remote-container-id` → `x-claude-remote-session-id` → `x-client-app`；每项按条件省略 |

  最终顺序由 `SCOPE-PND-COMBO2` 两个深度 2 请求的七字段投影 2/2 证实，另 8 个深度 0／1 请求
  与按条件省略后的相对顺序相容，见 `SPEC-HDR-026`。它不是旧正文猜测的 container-first 顺序；
  静态对象构造顺序也不能替代解析后的 H1 顺序。
- **边界**：所选 `BASELINE-J` 的 5 个请求中未出现重复 header 名，重复项命题**无正例**。R 样本经等长脱敏，
  `check_sample_integrity.py` 判为「洁净，可用于全称与计数命题」。
- **Sub2API 实现要求（暂定）**：按此名称、大小写与相对顺序输出；`x-client-request-id` 与
  `X-Claude-Code-Session-Id` 每请求变化，只固定位置不固定值。
- **状态**：**已观察**；原 ID 合并名称、大小写、基础顺序与 R 字节细节，仍未整体闭环；条件槽 B
  的七字段投影已经由独立原子规则 `SPEC-HDR-026` 验证，不再属于本 ID 的候选缺口。

### SPEC-HDR-002 User-Agent

- **范围**：`SCOPE-J6`；入口固定为 `sdk-cli`。
- **规则**：所选六个 J 记录的 `User-Agent` 均恰为
  `claude-cli/2.1.220 (external, sdk-cli)`。
- **静态证据**：[A1] Linux bundle 中 UA 由 `claude-cli/${VERSION} (external, ${entrypoint}…)`
  模板构造，可选段依次为 agent-sdk、client-app、workload；语义锚点见
  `CANDIDATE_EVIDENCE.json` 的 `CAND-UA-ENTRYPOINT`（`a20d7b8f44d9`）。
- **运行证据**：J；`BASELINE-J` 的 5 个请求与 `PROBE-BASELINE-J` 的独立 s1 请求合计
  6/6，取值均为该精确字符串。
- **边界**：只覆盖 `sdk-cli`。`cli`（TUI）入口未采集，其 UA 尾段可能不同；三个可选段在本批
  样本中均未出现。
- **Sub2API 实现要求（暂定）**：`sdk-cli` 有限画像按此值输出；不得把该值用于其他 entrypoint。
- **状态**：**已观察**；J 的 6/6 精确值成立，但 A1 仅证明模板字面与局部 sink 近邻，未形成
  UA 字段级数据流；完整 M 亦未闭合。

### SPEC-HDR-003 Sonnet 5 基线 `anthropic-beta` 九项序列

- **范围**：`SCOPE-BASELINE`；未设置 `ANTHROPIC_BETAS`。
- **规则**：Sonnet 5 已采基线发送以下九项顺序。其他模型只记录观察或候选，不属于本条观察范围：

  | 模型 | 项数 | 顺序 |
  |---|---:|---|
  | `claude-sonnet-5` | 9 | `claude-code-20250219`、`oauth-2025-04-20`、`interleaved-thinking-2025-05-14`、`thinking-token-count-2026-05-13`、`context-management-2025-06-27`、`prompt-caching-scope-2026-01-05`、`mid-conversation-system-2026-04-07`、`effort-2025-11-24`、`extended-cache-ttl-2025-04-11` |
  | `claude-opus-4-8` | 9 | 单轮观察与 Sonnet 相同；尚未独立重复 |
  | `claude-haiku-4-5` | — | 当前 manifest 为 `failed`，旧正文所引 `relay-haiku-r1/r2` 不存在，不能定案 |

- **条件性**：A1 表明 beta 由条件累加构成，模型是自变量；未取得有效正例的模型不得复用
  Sonnet 列表，也不得从 HitCC 的旧版本列表继承。
- **静态证据**：[A1] 客户端注册表共 **33 条**注册、32 个语义名（`tool_search` 注册两个 flag
  值），远多于基线观察到的 9 条；Darwin 与 Linux 的语义名集合完全一致，注册函数分别为
  `Wv` 与 `WA`——符号跨平台漂移，集合不变。
- **运行证据**：`BASELINE-J` 的 5/5 个 Sonnet 请求序列一致；`oauth-model-opus-r1` 只支持一次 Opus 观察；
  `oauth-model-haiku-r1` 为失败 run，不作为正例。
- **边界**：只正式覆盖 Sonnet 5。claude-3-* 与其他模型未采集。
- **Sub2API 实现要求（暂定）**：Sonnet 5 按此九项与顺序输出；不得把 33 条注册表全量发送。
- **状态**：**已观察**（Sonnet 5 基线序列）；原 ID 是九值复合序列，Opus 为单次观察，Haiku 为**候选**。

### SPEC-HDR-004 `anthropic-version: 2023-06-01`

- **范围**：`SCOPE-BASELINE`。
- **规则**：固定为 `2023-06-01`。
- **静态证据**：不适用（由 SDK 常量提供）。
- **运行证据**：J；`BASELINE-J` 的 5/5 个请求取值一致。
- **边界**：无。
- **Sub2API 实现要求（暂定）**：按此值输出。
- **状态**：**已观察**；旧分母未绑定，且缺 2.1.220 当前 sink 的静态闭环。

### SPEC-HDR-005 `Accept-Encoding: gzip, deflate, br, zstd`

- **范围**：`SCOPE-BASELINE`。
- **规则**：固定为 `gzip, deflate, br, zstd`，含空格，顺序固定。
- **静态证据**：不适用（由 Bun HTTP 栈提供）。
- **运行证据**：J；`BASELINE-J` 的 5/5 个请求解析值一致。
- **边界**：J 证明的是解析后的取值，不是 wire 上的字节。
- **Sub2API 实现要求（暂定）**：按此字面输出，包括分隔符空格与 `zstd` 的存在。
- **状态**：**已观察**；J 只支持解析值，缺完整 M 的 R 仅补充观察 wire 字节。

### SPEC-HDR-006 `X-Stainless-*` 八项取值

- **范围**：`SCOPE-BASELINE`；内嵌运行时 v26.3.0。
- **规则**：已采 Linux 基线请求的值向量为 `Arch=x64`、`Lang=js`、`OS=Linux`、
  `Package-Version=0.94.0`、`Retry-Count=0`、`Runtime=node`、`Runtime-Version=v26.3.0`、
  `Timeout=600`。这是一个向量相等命题；平台、运行时或 SDK 变化即停止继承。
- **静态证据**：[A1] `0.94.0` 对应 bundle 内 SDK 版本常量。
- **运行证据**：J；`BASELINE-J` 的 5/5 个请求中八项取值向量一致。重试相关 run 只用于
  观察 `Retry-Count` 分支，不扩张本条基线分母。
- **边界**：`Retry-Count` 在所选 `BASELINE-J` 的 5/5 个请求及本条引用的重试样本中均为 `0`——
  重试观察见 SPEC-CONN-001。
  它只反映 SDK 内部重试，而 Claude Code 的重试发生在 SDK 之上，每次都是全新 SDK 调用。
  `Arch`／`OS` 绑定平台。
- **Sub2API 实现要求（暂定）**：按此输出。`Retry-Count` 在应用层重试时保持 `0`，不得为了「看起来像重试」而递增。
- **状态**：**已观察**；原 ID 合并八个字段，重试分支由 `SPEC-CONN-001` 另行限定。

### SPEC-HDR-007 `x-app` 与 `anthropic-dangerous-direct-browser-access`

- **范围**：`SCOPE-BASELINE`。
- **规则**：已采前台基线路径同时发送 `anthropic-dangerous-direct-browser-access: true` 与
  `x-app: cli`。A1 中的 `cli-bg` 分支没有运行正例，不属于本条正式命题。
- **静态证据**：[A1] header 构造链可达 `x-app`、`User-Agent`、
  `X-Claude-Code-Session-Id`、`ANTHROPIC_CUSTOM_HEADERS`（`lro()`）以及五个条件 Header 的写入点；
  该证据证明字段可达，不把 bundle 对象字面顺序外推为最终 H1 顺序。最终条件槽顺序见
  `SPEC-HDR-026` 的 J-raw 实测。
- **运行证据**：J；`BASELINE-J` 的 5/5 个前台请求均同时出现上述两个值；`cli-bg` 无正例
  （`--bg` 未采集）。
- **边界**：`x-app` 取值与 entrypoint 的 `sdk-cli` 不同，两者不可混用。后台分支见
  `CAND-BG-SESSION`。
- **Sub2API 实现要求（暂定）**：前台输出上述两个值；后台画像必须等待 `CAND-BG-SESSION` 闭环。
- **状态**：**已观察**（限前台）；原 ID 合并两个独立 Header，`cli-bg` 为候选。

### SPEC-HDR-008 基线不发送 `anthropic-dispatch-id`

- **范围**：`SCOPE-BASELINE`。
- **规则**：已采 OAuth 基线路径不发送 `anthropic-dispatch-id`。A1 另显示其写入点条件为
  `!retryFallback && querySource!=="auxiliary" && firstParty官方base &&
  (Og.CLAUDE_CODE_DISPATCH_V2S ?? gate("tengu_cedar_lattice", false))`，
  当前分析认为 `Og` 的环境分支为 undefined，实际正例仍由远程 gate 决定；该内部不可达解释
  和失败后 strip-retry 都留在候选台账，不并入“基线缺失”这一观察命题。
- **静态证据**：[A1] 语义锚点 `2770ffbfe958`（跨平台一致）。关键在于 `Og=oOl(ifh,null)` 且
  `ifh={}`：`oOl(schema, proto)` 只为 `schema` 中声明的键定义读 `process.env` 的 getter，
  空 schema 加 `null` 原型意味着 `Og` 上任何属性都取不到值。`CLAUDE_CODE_DISPATCH_V2S`
  在整个 bundle 中只出现这一次，没有其他地方向 `ifh` 添加该键。
- **运行证据**：J；`probe-dispatch-r1`／`r2` 显式注入
  `CLAUDE_CODE_DISPATCH_V2S=1` 后，2/2 个请求仍未出现该 header。未注入画像在其他基线样本中
  也观察到缺席，但未纳入本条所选证据分母，不能据此声称完成正负对照。
- **边界**：不能证明「该 header 永不出现」——远程 gate `tengu_cedar_lattice` 为真时仍会发送，
  该分支无正例。降级重试逻辑（首个事件前出错则去掉该 header 重试）同样无正例。
- **Sub2API 实现要求（暂定）**：基线不发送该 header；远程 gate 画像取得正例前不得臆造取值。
- **状态**：**已观察**；远程 gate 正例与绑定的未注入对照仍缺失，环境不可达解释仅作候选机制。

### SPEC-HDR-009 `CLAUDE_CODE_ADDITIONAL_PROTECTION` 控制的条件 header

- **范围**：`SCOPE-BASELINE` 加本条声明的单一环境变量。
- **规则**：`CLAUDE_CODE_ADDITIONAL_PROTECTION` 解析为真时发送
  `x-anthropic-additional-protection: true`，插入在 `anthropic-version` 之后、`x-app` 之前
  （无其他条件 header 时即绝对位置 16），使 header 总数变为 23；未设置时不发送。
  命题类型：条件；可变性：条件（环境）。
- **静态证据**：[A1] 语义锚点 `5bae1065d1b8`（跨平台一致），写入点为
  `if(Yt(process.env.CLAUDE_CODE_ADDITIONAL_PROTECTION)) p["x-anthropic-additional-protection"]="true"`。
- **运行证据**：J；`probe-protect-r1`／`r2` 两个 complete run 的 2/2 个请求均出现该 Header，
  取值和位置一致。两轮 manifest 均只记录这一注入变量；未注入负例未纳入本条所选证据分母。
- **边界**：只验证了「真值 `1`」这一种输入，`Yt()` 对其他取值的解析未做差分。
- **Sub2API 实现要求（暂定）**：仅在该条件成立时输出，并保持第 16 位；条件不成立时不得出现。
- **状态**：**已观察**（只覆盖变量值 `1` 的正例）；原 ID 合并触发、取值、位置与负例。

### SPEC-HDR-010 单行 `ANTHROPIC_CUSTOM_HEADERS` 注入

- **范围**：`SCOPE-BASELINE` 加本条声明的单一环境变量。
- **规则**：当变量恰为单行 `X-Egress-Probe: probe-alpha` 时，新增同名 header，值为
  `probe-alpha`，位于 `X-Claude-Code-Session-Id` 之后、`X-Stainless-Arch` 之前。
- **静态证据**：[A1] 语义锚点 `1a63a4012f4a`（跨平台一致）。注入语句为
  `o.defaultHeaders={...环境项, ...o.defaultHeaders}`——展开顺序决定了已有值覆盖环境值。
  读取用 `A0(name)`，其实现为 `process.env?.[name]?.trim() || undefined`。
- **运行证据**：J；`probe-custom-r1`／`r2` 两个 complete run 的 2/2 个请求均在第 5 位出现
  `X-Egress-Probe: probe-alpha`。未注入负例未纳入本条所选证据分母。
- **边界**：只验证单行单 header。A1 所示的首冒号解析、`trim`、多行顺序、同名覆盖与非法行
  没有运行正例，全部留候选。
- **Sub2API 实现要求（暂定）**：该变量未设置时不得出现任何额外 header；产品若不开放此能力，无需实现。
- **状态**：**已观察**（仅上述单行输入）；原 ID 合并出现、值、位置与解析边界。

### SPEC-HDR-011 已取代：billing 载体混淆

- **原问题**：旧命题只检查 HTTP Header，然后把同名 attribution 文本也错误判为“不出站”。
- **处置**：本编号不再作为活动规则；HTTP Header 由 `SPEC-HDR-020` 规定，Body 文本由
  `SPEC-BODY-007` 规定。
- **Sub2API 实现要求**：不实现本编号；编号不得复用，也不得重新合并 Header 缺席与 Body 存在两个命题。
- **状态**：**已取代**；编号保留且不得复用。

### SPEC-HDR-020 基线 HTTP Header 不含 `x-anthropic-billing-header`

- **范围**：`SCOPE-J6`。
- **规则**：HTTP Header 名集合不含 `x-anthropic-billing-header`。
- **静态证据**：[A1] 语义锚点 `d29b00f5d6f5` 只提供 attribution 文本构造与局部 sink 近邻；
  它不能证明所有路径都不写入同名 HTTP Header，故只作载体解释，不作为否定命题证明。
- **运行证据**：两个 complete baseline run 的 J 共 6/6 个主请求均未出现同名 HTTP Header。R 的默认、重试和
  子 agent 请求也观察到缺席，但 R 目录缺完整 M，只作补充观察，不扩张本条范围。
- **边界**：本条只否定基线主请求的 HTTP Header 载体，**不否定该字符串在请求体中出站**；
  子 agent、TUI、workload 与其他 provider 不在观察范围。
- **Sub2API 实现要求（暂定）**：该有限画像不新增同名 HTTP Header，并与 `SPEC-BODY-007` 的 Body 载体分开处理。
- **状态**：**已观察**；J 的 6/6 Header 缺席事实成立，但完整 M 未闭合。

### SPEC-HDR-012 `x-client-request-id` 样本内无碰撞

- **范围**：`SCOPE-BASELINE`。
- **观察**：已盘点请求各携带 UUID 形态的 `x-client-request-id`，样本内无碰撞，位于基线序列
  第 18 位。
- **静态证据**：不适用。
- **运行证据**：J；`BASELINE-J` 的 5/5 个请求各产生不同值，所选样本去重率 100%。
- **边界**：只证明「每请求不同」，未验证其生成算法或与服务端 `request-id` 的关系。
- **Sub2API 实现要求（暂定）**：实现应每请求生成新值；在当前 bundle 的生成器可达链或统计停止门槛闭环前，
  不把“永不复用”写成已验证全称。
- **状态**：**已观察**。

### SPEC-HDR-013 同一会话复用 `X-Claude-Code-Session-Id`

- **范围**：`SCOPE-BASELINE`。
- **规则**：同一已采会话内的多个请求共用一个 `X-Claude-Code-Session-Id`。
- **静态证据**：不适用。
- **运行证据**：J；`BASELINE-J` 共 3 个已采会话、5 个请求，`s2`／`s4` 两组会话内配对各含
  2 个请求且 session-id 逐组相同；`s1` 为单请求，不能支持会话内复用命题。
- **边界**：未验证会话如何界定边界（进程生命周期、compact 前后、子会话）。
- **Sub2API 实现要求（暂定）**：在会话内保持同值；跨会话生成策略仍须与 `CAND-HDR-SESSION-RANDOM` 一起补证。
- **状态**：**已观察**；只有两组会话内配对，跨会话生成与互异不作正式结论。

### SPEC-HDR-016 `x-client-app` 的条件存在性

- **范围**：`SCOPE-PND-NEG2` 与 `SCOPE-PND-HDR016`；受控变量仅比较“未设置”与
  `CLAUDE_AGENT_SDK_CLIENT_APP=probe-app-21220`。空串、纯空白、非 ASCII 与超长值不在范围内。
- **规则**：该变量有受支持的非空值时发送一个 `x-client-app`；变量未设置时省略。这里只规定
  Header 的存在性，精确值由 `SPEC-HDR-021` 规定，UA 联动由 `SPEC-HDR-022` 规定。
- **静态**：[A1] `CAND-HDR-CLIENT-APP-CONSTRUCTION` 绑定环境读取、条件展开、原值写入与
  Header 名；Linux 与 Darwin 各唯一命中且 α-归一化摘要一致，SHA-256
  `963a11f8028d44cf1b5c7158e18cd78d176e2987b0a04f72fe3cab45a750bd14`。
- **实测**：两轮负例各 1 个主请求，2/2 均缺席；两轮单变量正例各 1 个主请求，2/2 均出现且
  只有一个同名 Header。四个 run 均 `status=complete`、`m_binding.complete=true`，秘密扫描通过。
- **实现**：只从受控 `Claude220Profile.client_app` 生成该 Header；该画像字段未设置时省略。
  不得把官方或第三方入站携带的同名 Header 直接透传到 OAuth 上游。
- **状态**：**已验证** ✅；条件存在性闭环，值、UA 和组合顺序分别见 `HDR-021/022/026`。

### SPEC-HDR-021 `x-client-app` 等于受控 client-app 值

- **范围**：`SCOPE-PND-HDR016`；配置值固定为 `probe-app-21220`。组合运行只作交叉观察，不扩张
  本条正式分母。
- **规则**：`x-client-app` 的解析值逐字符等于受控 client-app 值，不添加前后缀。
- **静态**：[A1] 与 `SPEC-HDR-016` 同一可达锚点，写入表达式直接把环境值作为 Header 值。
- **实测**：两轮单变量正例 2/2 个请求均为 `x-client-app: probe-app-21220`；两轮组合运行的
  10/10 个请求另作一致性交叉观察。
- **实现**：从 `Claude220Profile.client_app` 取值并写入；不得从入站 Header 猜值或另行改写。
  对未覆盖字符集或长度的输入应由画像校验拒绝，不能把本条外推为任意字节透传规则。
- **状态**：**已验证** ✅；限已声明 ASCII 值，空白、非 ASCII 与超长值仍在候选边界。

### SPEC-HDR-022 client-app 与 User-Agent 联动

- **范围**：`SCOPE-PND-NEG2` 与 `SCOPE-PND-HDR016`；入口为 `sdk-cli`。组合运行只作交叉观察。
- **规则**：client-app 未设置时 UA 为 `claude-cli/2.1.220 (external, sdk-cli)`；值为
  `probe-app-21220` 时 UA 为
  `claude-cli/2.1.220 (external, sdk-cli, client-app/probe-app-21220)`。
- **静态**：[A1] UA 模板及 client-app 可选段由 `CAND-UA-ENTRYPOINT` 锚定；Header 写入由
  `SPEC-HDR-016` 的跨平台锚点证明可达。
- **实测**：两轮负例 2/2 命中无后缀 UA；两轮单变量正例 2/2 命中带后缀精确 UA，且同请求的
  `x-client-app` 值一致；两轮组合运行的 10/10 个请求另作一致性交叉观察。
- **实现**：`User-Agent` 与 `x-client-app` 必须由同一受控 `Claude220Profile.client_app` 一次生成；
  不能分别读取两个入站值，避免出现 Header 与 UA 后缀不一致。
- **状态**：**已验证** ✅；只覆盖 `sdk-cli` 与已声明 client-app 值，其他 entrypoint 另建画像。

### SPEC-HDR-017 `x-claude-remote-container-id` 的条件存在性

- **范围**：`SCOPE-PND-NEG2` 与 `SCOPE-PND-HDR017`；受控变量仅比较“未设置”与
  `CLAUDE_CODE_CONTAINER_ID=probe-container-21220`。真实远程容器生命周期不在本条范围内。
- **规则**：变量有受支持的非空值时发送一个 `x-claude-remote-container-id`；未设置时省略。
  精确值由 `SPEC-HDR-023` 规定。
- **静态**：[A1] `CAND-HDR-REMOTE-CONTAINER-CONSTRUCTION` 绑定环境读取、条件展开、原值写入与
  Header 名；Linux 与 Darwin 各唯一命中且 α-归一化摘要一致，SHA-256
  `4fd7907e0fe36a184dd2c1530eec1664af4a48b59ce40902565d641e35a62e4a`。
- **实测**：两轮负例 2/2 缺席；两轮单变量正例 2/2 出现且各只有一个同名 Header。四个 run 均
  完整 M、秘密扫描通过；正例 UA 仍为无 client-app 后缀的基线值。
- **实现**：只从受控 `Claude220Profile.remote_container_id` 生成；字段未设置时省略，不得透传
  任一入站同名 Header。
- **状态**：**已验证** ✅；受控环境映射闭环，不声明真实远程容器如何创建、轮换或回收该值。

### SPEC-HDR-023 remote-container Header 的精确值

- **范围**：`SCOPE-PND-HDR017`；配置值固定为 `probe-container-21220`。组合运行只作交叉观察。
- **规则**：`x-claude-remote-container-id` 的解析值逐字符等于受控 container ID，不添加前后缀。
- **静态**：[A1] 与 `SPEC-HDR-017` 同一可达锚点，写入表达式直接使用环境值。
- **实测**：两轮单变量正例 2/2 个请求均为 `probe-container-21220`；两轮组合运行的 10/10 个
  请求另作一致性交叉观察。
- **实现**：从 `Claude220Profile.remote_container_id` 取值；不得从第三方请求 Header 推断或复制。
- **状态**：**已验证** ✅；只验证该 ASCII 值，真实远程分配规则与异常输入仍属候选。

### SPEC-HDR-018 `x-claude-remote-session-id` 的条件存在性

- **范围**：`SCOPE-PND-NEG2` 与 `SCOPE-PND-HDR018`；受控变量仅比较“未设置”与
  `CLAUDE_CODE_REMOTE_SESSION_ID=probe-remote-session-21220`。真实远程会话生命周期不在范围内。
- **规则**：变量有受支持的非空值时发送一个 `x-claude-remote-session-id`；未设置时省略。
  精确值由 `SPEC-HDR-024` 规定。
- **静态**：[A1] `CAND-HDR-REMOTE-SESSION-CONSTRUCTION` 绑定环境读取、条件展开、原值写入与
  Header 名；Linux 与 Darwin 各唯一命中且 α-归一化摘要一致，SHA-256
  `de13c279b805e2dcf25c4260a339ec041bfe6fd6268e01bc5a14c27006ebda00`。
- **实测**：两轮负例 2/2 缺席；两轮单变量正例 2/2 出现且各只有一个同名 Header。四个 run 均
  完整 M、秘密扫描通过；它与 `X-Claude-Code-Session-Id` 是两个独立 Header 名。
- **实现**：只从受控 `Claude220Profile.remote_session_id` 生成；字段未设置时省略，不得透传
  任一入站同名 Header。
- **状态**：**已验证** ✅；受控环境映射闭环，不声明它与 Claude 主 session-id 的同源关系。

### SPEC-HDR-024 remote-session Header 的精确值

- **范围**：`SCOPE-PND-HDR018`；配置值固定为 `probe-remote-session-21220`。组合运行只作交叉观察。
- **规则**：`x-claude-remote-session-id` 的解析值逐字符等于受控 remote session ID，不添加前后缀。
- **静态**：[A1] 与 `SPEC-HDR-018` 同一可达锚点，写入表达式直接使用环境值。
- **实测**：两轮单变量正例 2/2 个请求均为 `probe-remote-session-21220`；两轮组合运行的 10/10 个
  请求另作一致性交叉观察。
- **实现**：从 `Claude220Profile.remote_session_id` 取值；不得从第三方请求 Header 推断或复制。
- **状态**：**已验证** ✅；只验证该 ASCII 值，真实远程分配规则与异常输入仍属候选。

### SPEC-HDR-014 子会话请求携带 `x-claude-code-agent-id`

- **范围**：`SCOPE-SUBAGENT-L1`。
- **规则**：由子 agent 发出的请求在基线 22 项之外多带一个 `x-claude-code-agent-id`；无其他
  条件 Header 时位于 `x-app` 之后、`x-client-request-id` 之前（绝对位置 17）。组合场景中它仍
  紧随 `x-app`，并位于 parent／remote／client-app 之前，见 `SPEC-HDR-026`。
  取值为 17 位十六进制串，每个子 agent 不同。主 agent 的请求不带该 Header。
- **静态证据**：[A1] `mde(e)=e.agentType==="subagent"` 区分主／子会话。
- **运行证据**：R 两轮观察，实际目录为 `subagent-r1/r2`。每轮三条连接：`conn001` 为
  `HEAD /api/hello`；`conn002` 承载主 agent 的两个请求，均为 22 项 Header；`conn003` 为子
  agent 请求，23 项 Header，agent-id 均在位置 17。
- **边界**：原 ID 还合并值格式、唯一性和多种 agent 类型；本轮完整 M 只用于
  `SPEC-HDR-019/025/026` 的收窄命题，不把该复合 ID 整体升级。
- **Sub2API 实现要求（暂定）**：主会话不得输出该 Header；子会话由内部 agent 上下文生成，
  不得接受入站 Header 覆盖。
- **状态**：**已观察**；原复合命题仍未整体闭环。

### SPEC-HDR-019 `x-claude-code-parent-agent-id` 的层级存在性

- **范围**：`SCOPE-PND-AGENT6`；内置 `general-purpose` agent 的嵌套深度 0、1、2、3。
- **规则**：深度 0 和 1 的请求不发送 `x-claude-code-parent-agent-id`；深度 2 和 3 的请求发送
  恰一个该 Header。这里只规定存在性，精确父子相等关系由 `SPEC-HDR-025` 规定。
- **静态**：[A1] `CAND-HDR-PARENT-AGENT-CONSTRUCTION` 绑定同一上下文的 `parentAgentId`、
  条件展开、编码调用与 Header 名；Linux 与 Darwin 各唯一命中且 α-归一化摘要一致，SHA-256
  `2f8ad3638a70a172b450dc987437c57de0005c872b718de903cc2a36fe3e82f8`。
- **实测**：深度 1、2、3 各有两轮独立运行；每轮请求深度序列分别为 `0→1→0`、
  `0→1→2→1→0`、`0→1→2→3→2→1→0`。六轮中所有深度 0／1 请求均缺席，所有深度 2／3
  请求均出现；六个 run 均完整 M、秘密扫描通过。
- **实现**：由 Sub2API 内部 agent 栈判断；当前 agent 有直接父 agent 时生成，主请求和一层子
  agent 请求省略。不得以入站同名 Header 作为层级来源。
- **状态**：**已验证** ✅；限深度 0—3 与 `general-purpose`，并发、其他 agent 类型及更深层级未外推。

### SPEC-HDR-025 parent-agent-id 等于直接父级 agent-id

- **范围**：`SCOPE-PND-AGENT6` 中 a2／a3 四轮的深度 2、3 请求；a1 没有适用请求。
- **规则**：当前请求的 `x-claude-code-parent-agent-id` 逐字符等于**直接父级请求**的
  `x-claude-code-agent-id`；深度 3 只引用深度 2 的 ID，不输出整条祖先链。
- **静态**：[A1] 与 `SPEC-HDR-019` 同一可达锚点，写入值来自当前 agent 上下文的
  `parentAgentId`。
- **实测**：六轮纯 agent 链中共有 8 个深度 2／3 请求，8/8 均等于上一层 agent-id；每层动态
  agent-id 还与对应 Agent `tool_result` 的 `agentId:` 一致，`parent_tool_use_id` 链逐层闭合。
- **实现**：内部创建 agent 时保存不可变 `agent_id` 和 `parent_agent_id`；Header 只写直接父级 ID，
  不从客户端入站或整条祖先数组拼接。
- **状态**：**已验证** ✅；只验证直接父级相等关系，不定义 agent-id 的生成算法。

### SPEC-HDR-026 条件 Header 的组合顺序

- **范围**：`SCOPE-PND-COMBO2` 中两轮深度 2 请求；J-raw 解析后的 HTTP/1.1 Header 列表。
  本条只约束七个相关名称的序列投影，不声称它们在完整 Header 列表中相邻。
- **规则**：将实际 Header 列表投影到七个相关名称，顺序固定为：
  `x-app → x-claude-code-agent-id → x-claude-code-parent-agent-id →
  x-claude-remote-container-id → x-claude-remote-session-id → x-client-app →
  x-client-request-id`。这不是 container-first 顺序。
- **静态**：[A1] 五个条件写入点均可达到同一请求 sink；静态对象构造顺序不足以证明最终 H1
  顺序，因此本条顺序只绑定 J-raw，不把旧 container-first 猜测作为证据。
- **实测**：两轮组合运行各有一个深度 2 请求，七项同时出现且 2/2 的投影严格相同。其余 8 个
  深度 0／1 请求分别省略 agent 两项或只保留 agent-id，与按条件省略后的相对顺序相容，但不并入
  本条七项同时出现的正式分母。
- **实现**：最终 Header 定型器按上述顺序生成条件槽；先由内部 agent 上下文写 agent 两项，再写
  remote container、remote session、client-app，最后写 request-id。不得按字典序排序，也不得沿用
  旧的 container-first 顺序。
- **状态**：**已验证** ✅；状态只覆盖深度 2 的七字段 J-raw 投影，原始 wire 名值分隔和 CRLF 仍需 R。

### SPEC-HDR-015 子会话共用主会话的 `X-Claude-Code-Session-Id`

- **范围**：`SCOPE-SUBAGENT-L1`。
- **观察**：子 agent 请求的 `X-Claude-Code-Session-Id` 与发起它的主会话相同。metadata
  内层相等属于 Body 命题，不并入本 Header 编号。
- **静态证据**：不适用。
- **运行证据**：R 两轮，`conn002`（主）与 `conn003`（子）的 session id 逐字符相同。
- **边界**：未验证跨会话的子 agent 或后台 agent（`--bg`）是否另起 session。
- **Sub2API 实现要求（暂定）**：子会话不得另生成 session id。
- **状态**：**已观察**；R 缺完整 M 绑定。

## 2.7 Body

### SPEC-BODY-001 请求体顶层键集合

- **范围**：`SCOPE-BASELINE`。
- **规则**：请求体顶层键恰为 `context_management`、`max_tokens`、`messages`、`metadata`、
  `model`、`output_config`、`stream`、`system`、`thinking`、`tools` 十项。
- **静态证据**：不适用。
- **运行证据**：`BASELINE-J` 的 5/5 个已采请求键集合一致；R 两轮另观察到序列化顺序：
  `model → messages → system → tools → metadata → max_tokens → thinking →
  context_management → output_config → stream`。注意这**不是**字母序。
  两轮特定输入的 body 长度同为 15450 字节，但长度取决于内容，不是规则。
- **边界**：只覆盖 `s1`／`s2`／`s4` 与 Sonnet 5。嵌套对象的键序未逐层验证。
- **Sub2API 实现要求（暂定）**：基线 Sonnet 场景输出这十个顶层键；条件字段出现时走候选分支，不能仍断言“恰为十项”。
- **状态**：**已观察**；原 ID 合并顶层键集合与序列化顺序，R 又缺完整 M。

### SPEC-BODY-002 `metadata.user_id` 是内嵌 JSON 字符串

- **范围**：`SCOPE-BASELINE`。
- **规则**：基线路径的 `metadata` 只有 `user_id` 一个键，其值不是普通标识符，而是 JSON 字符串，
  解析后恰含三个键：`device_id`、`account_uuid`、`session_id`。三者的可变性各不相同——
  `device_id` 机器级固定，`account_uuid` 账号级固定，`session_id` 会话级变化，且
  **与 `X-Claude-Code-Session-Id` header 取值完全相同**。
- **静态证据**：不适用。
- **运行证据**：J；`BASELINE-J` 的 5/5 个请求中，内层键组合一致，`device_id` 与
  `account_uuid` 在该单机单账号样本内各只有 1 种；`session_id` 与同请求的
  `X-Claude-Code-Session-Id` 5/5 逐个相等。
- **边界**：`device_id` 与 `account_uuid` 的单一性只在同一台机器、同一账号下观察到，
  未做跨机器或跨账号差分，因此只能证明「同机同账号内固定」，不能证明其生成算法。
  子会话已取得正例（见 SPEC-HDR-014），其 `metadata` 结构与主会话**完全相同**——
  静态证据里的 `subagent_type`、`is_built_in_agent` 并未进入 API 请求的 metadata，
  应属遥测属性而非出站字段。区分主／子靠的是 `x-claude-code-agent-id`。
- **Sub2API 实现要求（暂定）**：基线路径必须构造出这个内嵌 JSON 并保持三个键；`session_id` 必须与
  `X-Claude-Code-Session-Id` 同值——两者不一致是极易被察觉的形态破绽。
  不得把 `user_id` 实现成裸字符串。
- **状态**：**已观察**；原 ID 合并编码、键集、生命周期及跨层相等；device/account 生命周期和
  `CLAUDE_CODE_EXTRA_METADATA` 合并为候选。

### SPEC-BODY-007 `system[0].text` 携带 billing attribution

- **范围**：`SCOPE-J6`。
- **规则**：`system[0].text` 是字符串，且只断言它以字面 `x-anthropic-billing-header:` 开头。
  这是 Body attribution 文本，不是 HTTP Header。
- **静态证据**：[A1] 语义锚点 `d29b00f5d6f5` 提供构造字面与局部 sink 近邻；2.1.88 的
  `src/constants/system.ts` 与 `src/utils/api.ts` 只用于解释字段语义。现有静态工具尚未证明构造点经
  system block 到达 `messages.create` 的字段级数据流。
- **运行证据**：两个 complete run 的 J-raw 共 6/6 个请求均命中该前缀。机器门禁在已披露的
  host／path 上游预筛边界内枚举 `SCOPE-J6`，且 attribution 内容未参与选择，因此该待证字段的
  分母独立。
- **边界**：本条不合并 block type、`cc_version`、`cc_entrypoint`、`cch`、字段顺序或动态性；
  后三个字段分别见 `SPEC-BODY-014/015/016`。`cc_workload`、TUI、`cc_prev_req`、
  `cc_is_subagent` 与 attestation 改写机制仍是观察或候选。
- **Sub2API 实现要求（暂定）**：该有限画像生成这个 Body 文本前缀，同时遵守 `SPEC-HDR-020` 的载体区分。
- **状态**：**已观察**；J 的 6/6 前缀事实成立，但字段级 A1 数据流与完整 M 均未闭合。

### SPEC-BODY-008 基线 `model`

- **范围**：`SCOPE-J6`。
- **规则**：已采基线请求的 `model` 恰为字符串 `claude-sonnet-5`；不声称这是 CLI 无参数默认模型。
- **静态证据**：不适用；模型选择机制仍按候选处理。
- **运行证据**：J，`oauth-claude-newaccount-baseline` 的 s1／s2／s4 五个请求与独立
  `oauth-probe-baseline-r2` s1 一个请求，合计 6/6 相同。
- **边界**：不外推 Opus、Haiku、子 agent 模型选择或 fallback。
- **Sub2API 实现要求（暂定）**：该基线画像发送该精确字符串；模型选择分支必须另建规则。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-009 基线 `max_tokens`

- **范围**：`SCOPE-J6`。
- **规则**：`max_tokens` 恰为 JSON 整数 `64000`。
- **静态证据**：不适用。
- **运行证据**：与 SPEC-BODY-008 相同的两个 complete run，6/6 相同。
- **边界**：不外推其他模型、context overflow 改写或 quota 请求。
- **Sub2API 实现要求（暂定）**：发送数字 `64000`，不得发送字符串。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-010 基线 `thinking`

- **范围**：`SCOPE-J6`。
- **规则**：`thinking` 恰为 `{"type":"adaptive"}`，无其他键。
- **静态证据**：不适用。
- **运行证据**：与 SPEC-BODY-008 相同的两个 complete run，6/6 对象相同。
- **边界**：不外推禁用 thinking、固定 budget 或其他模型路径。
- **Sub2API 实现要求（暂定）**：保持对象结构和值，不得只保证 `type` 键存在。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-011 基线 `context_management`

- **范围**：`SCOPE-J6`。
- **规则**：`context_management` 恰为
  `{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}`。
- **静态证据**：不适用。
- **运行证据**：与 SPEC-BODY-008 相同的两个 complete run，6/6 对象相同。
- **边界**：不外推长上下文、compact 或服务端动态策略。
- **Sub2API 实现要求（暂定）**：保持单项 `edits` 数组、键名和值。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-012 基线 `output_config`

- **范围**：`SCOPE-J6`。
- **规则**：`output_config` 恰为 `{"effort":"high"}`，无其他键。
- **静态证据**：不适用。
- **运行证据**：与 SPEC-BODY-008 相同的两个 complete run，6/6 对象相同。
- **边界**：不外推用户显式 effort、其他模型或远程配置。
- **Sub2API 实现要求（暂定）**：保持对象结构与字符串值 `high`。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-013 基线 `stream`

- **范围**：`SCOPE-J6`。
- **规则**：`stream` 恰为 JSON 布尔值 `true`。
- **静态证据**：不适用。
- **运行证据**：与 SPEC-BODY-008 相同的两个 complete run，6/6 相同。
- **边界**：不外推异常后的 non-stream fallback 或辅助请求。
- **Sub2API 实现要求（暂定）**：发送布尔值 `true`，不得发送字符串。
- **状态**：**已观察**；两个 complete run 的可见值一致，但缺当前 sink 静态闭环且 argv 未归档。

### SPEC-BODY-014 attribution 的 `cc_version`

- **范围**：`SCOPE-J6`。
- **规则**：按 `;` 分段解析 `system[0].text` attribution 后，`cc_version` 标量匹配
  `^2\.1\.220\.[^;\s]+$`。
- **静态证据**：[A1] billing 构造候选绑定实际 Linux 二进制，但只有局部 sink 近邻。
- **运行证据**：J-raw；在已披露的 host／path 预筛边界内，6/6 个请求均匹配该形态，且
  `cc_version` 未参与选择。
- **边界**：只证明值形态，不证明 build 后缀的生成算法、稳定性或唯一性。
- **Sub2API 实现要求（暂定）**：版本前缀与目标二进制一致；后缀不得固定为某次样本。
- **状态**：**已观察**；字段级 A1 数据流与完整 M 未闭合。

### SPEC-BODY-015 attribution 的 `cc_entrypoint`

- **范围**：`SCOPE-J6`。
- **规则**：同一分段解析结果中的 `cc_entrypoint` 标量恰为 `sdk-cli`。
- **静态证据**：[A1] billing 文本与 entrypoint 的局部构造线索；不是字段级数据流证明。
- **运行证据**：J-raw；在已披露的 host／path 预筛边界内，6/6 个请求均为该值，且
  `cc_entrypoint` 未参与选择。
- **边界**：不外推 TUI、agent-sdk、client-app 或 workload。
- **Sub2API 实现要求（暂定）**：只在该有限 `sdk-cli` 画像发送此值。
- **状态**：**已观察**；字段级 A1 数据流与完整 M 未闭合。

### SPEC-BODY-016 attribution 的 `cch`

- **范围**：`SCOPE-J6`。
- **规则**：同一分段解析结果中的 `cch` 标量匹配 `^[A-Za-z0-9]{5}$`。
- **静态证据**：[A1] 当前 bundle 有 `cch=00000` 占位符线索，但尚未证明占位符到序列化后值的
  改写数据流。
- **运行证据**：J-raw；在已披露的 host／path 预筛边界内，6/6 个请求均为五字符字母数字值，且
  `cch` 未参与选择。
- **边界**：六个样本值相互不同只作观察；不证明随机性、每请求唯一性或生成算法。
- **Sub2API 实现要求（暂定）**：输出五字符值，不固定某次样本，也不声称已复刻内部 attestation 算法。
- **状态**：**已观察**；改写数据流与完整 M 未闭合。

### SPEC-BODY-003 主会话基线 `system` 四段结构

- **范围**：`SCOPE-BASELINE`。
- **规则**：主会话基线路径的 `system` 是长度为 4 的数组，四个元素的 `type` 均为 `text`；
  `cache_control` 出现在索引 2、3。
- **静态证据**：不适用。
- **运行证据**：J；`BASELINE-J` 的 5/5 个主会话请求结构与 `cache_control` 位置一致。
- **边界**：未验证四段各自的语义内容，也未验证 compact 或长上下文下段数是否变化。
- **Sub2API 实现要求（暂定）**：输出四段 text，缓存断点落在后两段；段数或断点位置不同即形态不一致。
- **状态**：**已观察**；原 ID 合并段数、类型和两个缓存位置，compact、子 agent 与动态
  cache scope 不在本条。

### SPEC-BODY-004 多轮会话的 `messages` 角色序列

- **范围**：`SCOPE-BASELINE` 的已采两轮 `s2`／`s4` 场景。
- **观察**：首轮 `messages` 为 `[user, system]` 两项；同一会话的续轮为
  `[user, system, assistant, user, system]` 五项——即在原有两项后追加
  `assistant`、`user`、`system`。命题类型：跨请求状态；可变性：条件（轮次）。
- **静态证据**：不适用。
- **运行证据**：J；所选 `BASELINE-J` 中，`s1` 的 1/1 个请求为两项；`s2`／`s4` 各有一组
  首轮两项、续轮五项的会话内配对。
- **边界**：只覆盖两轮。三轮及以上的增长形态、compact 触发后的裁剪未验证。
  注意 `system` 角色同时出现在 `messages` 数组内，与顶层 `system` 字段是两回事。
- **Sub2API 实现要求（暂定）**：实现需保留角色边界；补足两轮独立配对后再把精确五项序列升格。
- **状态**：**已观察**（限已采两轮）。

### SPEC-BODY-005 基线无工具时保留 `tools: []`

- **范围**：`SCOPE-BASELINE-NO-TOOLS`；只覆盖已采 s1／s2 的无工具记录，不包含启用 Bash 的 s4。
- **规则**：已采无工具记录中，`tools` 为显式空数组 `[]`，不是省略字段。
- **静态证据**：不适用。
- **运行证据**：J；`s1`／`s2` 合计 3/3 个请求为显式空数组；对照的 `s4` 2/2 个请求各含
  1 个 `Bash` 工具元素。
- **边界**：观察到两种单工具形态——`s4` 的 `Bash`，以及用 `--tools Task` 启动时实际下发的
  名为 `Agent` 的工具（工具名被映射，不等于命令行参数字面量）。多工具、内置工具与 MCP
  工具的键集合未验证。
- **Sub2API 实现要求（暂定）**：未启用工具时输出空数组。简单工具的三键形态只算已观察；`strict`、
  `eager_input_streaming`、`defer_loading`、MCP、ToolSearch 与 server tool 均按候选处理。
- **状态**：**已观察**；无工具空数组与简单单工具只覆盖上述已采记录。

### SPEC-BODY-006 子会话请求的 `system` 与 `messages` 形态

- **范围**：`SCOPE-SUBAGENT-L1`。
- **规则**：子 agent 的首个请求 `system` 为 **3 段**（主 agent 为 4 段），
  `messages` 只有 **1 项且角色为 `user`**（主 agent 首轮为 `[user, system]` 两项）。
  命题类型：条件；可变性：条件（会话角色）。
- **静态证据**：不适用。
- **运行证据**：R 目录 `subagent-r1/r2` 两轮均观察到 `conn003` 为
  `system=3`、`messages=1 [user]`；
  同轮 `conn002` 均为 `system=4`、`messages=2`。
- **边界**：只覆盖子 agent 的**首个**请求；子 agent 多轮后的增长形态未观测。
- **Sub2API 实现要求（暂定）**：子会话请求按 3 段 system、单条 user 消息构造，不得套用主会话的 4 段结构。
- **状态**：**已观察**；R 缺完整 M 绑定。

## 2.8 端点

### SPEC-EP-001 推理请求 path

- **范围**：`SCOPE-J6`。
- **规则**：`request.path` 标量恰为 `/v1/messages?beta=true`。
- **静态证据**：不适用。
- **运行证据**：J；已路由到 `claude-http.jsonl` 的全部 6 条请求记录中，6/6 的精确 path 均为该值。
- **边界**：addon 已用 path 含 `/messages` 做部分预筛，因此本条不是无 path 预过滤发现，也不声明
  `verification_denominators`。它仍记录 substring 之外的精确版本、查询串和值；method、HTTP host 与
  TLS SNI 分别由 `SPEC-EP-003`、`SPEC-EP-004`、`SPEC-TLS-003` 管理。它不是端点全集。
- **Sub2API 实现要求（暂定）**：该有限画像按此路径与查询串发送。
- **状态**：**已观察**；这是受部分 path 预筛的 6/6 观察，且完整 M 未闭合。

### SPEC-EP-003 推理请求方法

- **范围**：`SCOPE-J6`。
- **规则**：`request.method` 标量恰为 `POST`。
- **静态证据**：不适用。
- **运行证据**：J；全部 6 条请求记录中 6/6 为 `POST`，method 未参与 host allowlist 或 path 路由。
- **边界**：不继承 path、HTTP host、scheme、port 或 TLS SNI。
- **Sub2API 实现要求（暂定）**：该有限画像使用 `POST`。
- **状态**：**已观察**；J 事实成立，但完整 M 未闭合。

### SPEC-EP-004 推理请求 HTTP host

- **范围**：`SCOPE-J6`。
- **规则**：解析后的 `request.host` 标量恰为 `api.anthropic.com`。
- **静态证据**：不适用。
- **运行证据**：J；全部 6 条落盘记录中 6/6 为该 host，但 manifest 已预设
  `target_hosts=[api.anthropic.com]`，addon 会丢弃其他 host。
- **边界**：这是 host allowlist 的预条件观察，**不是独立 host 发现或验证**，故不声明
  `verification_denominators`。它不等同于 TLS SNI，也不是其他端点的 host 全集。scheme 与 port 只作为
  `SCOPE-J6` 的已采边界，未隐式并入本条。
- **Sub2API 实现要求（暂定）**：该有限画像使用该 HTTP host。
- **状态**：**已观察**；host 值由预筛选决定，另缺完整 M；无 host 预过滤的网络观察只支持
  `SPEC-TLS-003` 的 TLS SNI，不能替代 HTTP authority 命题。

### SPEC-EP-002 启动时的 `HEAD /api/hello` 连通性探测

- **范围**：`SCOPE-BASELINE`。
- **规则**：在推理请求之前，客户端先向同一 host 发一次 `HEAD /api/hello HTTP/1.1`，
  只带 5 个 header，按此顺序：`Connection`、`User-Agent`、`Accept`、`Host`、
  `Accept-Encoding`。该请求无 body。固定。
- **静态证据**：不适用（未在 bundle 中定位构造点）。
- **运行证据**：R 目录 `relay-r1/r2` 两轮均观察到该请求为 `conn001`，
  请求行与 5 项 header 逐字一致。J 侧看不到它——MITM addon 的 `_classify` 把非
  `/messages` 路径归入 `misc`，而本批 J 分析只取 `claude-http.jsonl`。
- **边界**：该探测走独立连接（`conn001`），与推理请求（`conn002`）不复用。其响应为
  `HTTP/1.1 200 OK`、10 项 header、**body 长度 0**（符合 HEAD 语义），4 组 R 样本一致。
  是否在所有 entrypoint 与所有会话都发送未验证。
- **Sub2API 实现要求（暂定）**：补齐 R 的完整 manifest，并分别验证请求字节、先后关系和独立连接后再作为硬规则。
- **状态**：**已观察**；R 缺完整 M 绑定。

## 2.9 连接与重试

### SPEC-CONN-001 单次 500 重试不递增 `X-Stainless-Retry-Count`

- **范围**：`SCOPE-FAULT`。
- **规则**：两轮受控 `status=500,count=1` 中，客户端各重发一次，两个请求的
  `X-Stainless-Retry-Count` 都为 `0`。
- **静态证据**：不适用。
- **运行证据**：J + 生命周期记录，三种输入：`oauth-retry-fault-r1`／`r2` 注入
  `status=500,count=1`，各 2 个 `request` 事件、1 个 `fault_status`，两次 retry-count 均为
  `0`，最终 200 成功。`oauth-retry-kill-r1` 与 `oauth-ratelimit-429-r1` 各只有一轮，分别作为
  断连重建与 429 重试的已观察样本，不并入正式命题。
- **边界**：这是**受控干预**产生的样本，只证明客户端收到该输入后的反应，不等于自然
  失败链。注入项记录在 manifest 的 `runtime.injected_fault_spec`，不得与自然样本混用。
  单次注入下的连接行为如上；连续失败的退避曲线见 SPEC-CONN-002。
- **Sub2API 实现要求（暂定）**：500 应用层重试保持 `Retry-Count: 0`；“重试位于 SDK 之上”是静态解释，不是 wire 命题。
- **状态**：**已观察**；两轮单次 500 受控干预均复现，但原 ID 合并重发与 Header 两个结果，
  且缺 P／R／J 的流级关联；429 与断连也只作观察。

### SPEC-CONN-002 `500` 且无 `Retry-After` 时的指数退避

- **范围**：`SCOPE-PND-RETRY6`；受控响应只覆盖连续 `status=500`、count 为 3／5／9，且不含
  `Retry-After`。不覆盖 401／403／408／409／429／529、连接中断、自然故障或有
  `Retry-After` 的响应。
- **规则**：第 `i` 次重试（`i` 从 1 开始）的无抖动基数和等待值为：

  ```text
  base_i  = min(500 * 2^(i-1), 32000) ms
  delay_i = round(base_i + random * 0.25 * base_i)
  ```

  因而每次报告值落在 `[base_i, 1.25 * base_i]`；32 秒是**无抖动基数上限**，封顶段可见值约在
  32—40 秒之间，不存在“固定约 35 秒”的规则。
- **静态**：[A1] 结构化探针的 α 摘要：退避公式
  `0741c6b39c891180a2551ec6984820a6d65704901be487edcd727012c1630ed3`；默认重试常量块
  `861b891ff160a8c909c2ace03f668b5b42cbf4022ef572c744005fe38870ac04`；主
  `messages.create` 调用点
  `64ad359ea64e390c06a48799a64a6ea543b430ce50527388c8fd6bf904d5d816`。
- **实测**：count 3／5／9 各两轮；每轮 `request/fault_status/api_retry` 分别为 `4/3/3`、
  `6/5/5`、`10/9/9`，attempt 均为 `1..N`、`error_status=500`、最终 200 成功。两条 count=9
  的 delay 序列分别为
  `[617,1066,2233,4460,9734,19585,32442,36358,33464]` 与
  `[571,1197,2151,4154,9027,17168,34105,36350,38828]` ms，全部落入公式区间；响应到下一请求的
  单调时钟 gap 均不小于报告 delay，额外开销为 24.264—53.018 ms。六个 run 均完整 M。
- **实现**：仅在本条 `500`／无 `Retry-After` 分支按公式调度；封顶前基数递增，封顶后允许随机
  回落。不得写成固定 35 秒，也不得把同一曲线未经证据套到其他状态码、断连或 `Retry-After` 分支。
- **状态**：**已验证** ✅；验证的是公式与区间，不证明随机数独立、均匀分布或自然故障等价性。

### SPEC-CONN-003 九次 `500` 后仍发送第十个请求

- **范围**：`SCOPE-PND-RETRY6` 中两轮 count=9；响应均无 `Retry-After`，第十个请求返回 200。
- **规则**：客户端在连续收到 9 个 `500` 后仍发送第 10 个请求；因此本范围只证明重试能力下界为
  **至少 9 次**。运行事件中的 `max_retries` 均报告为 `10`，但本条不声称第 10 次失败后一定终止。
- **静态**：[A1] 默认 `maxRetries=10` 及非 watchdog 上限机制由结构化常量块锚点
  `861b891ff160a8c909c2ace03f668b5b42cbf4022ef572c744005fe38870ac04` 定位；静态默认值只解释
  本次运行报告，不能替代 count=10／11 的终止实测。
- **实测**：`oauth-pnd-retry-c9v-01-20260801` 与 `oauth-pnd-retry-c9v-02-20260801` 均记录
  10 个 request、9 个 `fault_status`、9 个 `api_retry`，attempt 为 1—9，`max_retries=10`，
  第十个请求得到 200 并输出 `S1_OK`；两轮完整 M、秘密扫描通过。
- **实现**：本画像的 500 重试预算不得少于 9 次；在 count=10／11 终止探针闭环前，不把
  “恰好重试 10 次”或“无限重试”写成实现合同。
- **状态**：**已验证** ✅；仅验证九次失败后的第十次请求和运行报告值，精确终止边界仍属候选。

## 2.10 响应兼容（不计客户端 egress）

`SPEC-RESP-001/002/003` 只约束 Sub2API 对上游响应的传递方式；**响应兼容不计入 egress**。
HEAD 响应、非流式 fallback 响应和错误响应也必须留在本域，不能反向证明客户端如何发请求。

### SPEC-RESP-001 成功响应的流式传输形态

- **范围**：`SCOPE-RESPONSE`；`stream:true` 成功响应。
- **规则**：成功响应为 `Content-Type: text/event-stream; charset=utf-8`、
  `Transfer-Encoding: chunked`、`Content-Encoding: gzip`。固定。
- **静态证据**：不适用（服务端行为）。
- **运行证据**：J；所选 `BASELINE-J` 的 5/5 个 200 响应三项一致。受控 500 响应属于另一
  证据目录，未纳入本条分母，且其 `application/json` 形态不适用本条。
- **边界**：这是**服务端对该客户端的响应形态**，不是客户端出站画像。之所以纳入，是因为
  Sub2API 作为代理必须把等价形态转给下游；若上游协商结果不同，本条不成立。
  未验证非流式请求与错误响应的形态。
- **响应兼容要求**：向下游保持 SSE + 分块 + gzip 的等价形态；不得把流式响应缓冲成整体 JSON 再下发。
- **状态**：**响应兼容**；不计客户端出站规则。

### SPEC-RESP-002 响应 header 的稳定性分级

- **范围**：`SCOPE-RESPONSE`；`stream:true` 的 200 响应。
- **观察**：成功响应的 header 可按样本中的变化分三类：**逐请求变化**——`Date`、`request-id`、
  `traceresponse`；**随用量变化**——`anthropic-ratelimit-unified-*`
  的 `5h-reset`、`5h-utilization`、`7d-utilization`、`reset`；**所选样本内未见变化**，
  含 `Content-Type`、`Transfer-Encoding`、`Connection`、`Cache-Control`、
  `anthropic-organization-id`、`Server`、`Content-Encoding`、`vary`、`cf-cache-status`、
  `X-Robots-Tag`、`strict-transport-security` 与其余 ratelimit 项。
  **header 顺序不固定**：同一形态在 R 样本中观察到两种排列（`X-Robots-Tag` 与
  `cf-cache-status` 互换），且观察到 `server-timing` 条件出现；因此总数与顺序都不作固定规则。
- **静态证据**：不适用（服务端与 CDN 行为）。
- **运行证据**：J；所选 `BASELINE-J` 的 5 个 200 响应用于上述分级。R 目录另有 4 组 wire
  顺序变体，但缺完整 M，不并入正式分母。
- **边界**：这是服务端对该客户端的响应形态，不是客户端出站画像。顺序变体来自 CDN，
  **不得把任何一次观察到的响应 header 顺序当成固定序**。
- **响应兼容要求**：向下游透传时按类处理——每次必变项须重新生成或透传上游值，不得缓存复用；
  不得钉死响应 header 顺序。
- **状态**：**响应兼容／已观察**；待机器生成完整 header 明细与计数口径。

### SPEC-RESP-003 响应体为外层 chunked、内层 gzip

- **范围**：`SCOPE-RESPONSE`；`stream:true` 的 200 响应。
- **规则**：响应体的封装顺序是**先 gzip 压缩、再分块传输**——wire 上每个 chunk 以十六进制
  长度前缀加 CRLF 开头，chunk 内容的首两字节为 gzip magic `1f 8b`。
- **静态证据**：不适用。
- **运行证据**：**R**，4 组样本一致。以 `relay-r1/conn002` 为例，首个 chunk 前缀为 `1de`
  （478 字节），其内容起始为 `1f 8b 08 00`。J 只能证明两个 header 同时存在，证明不了嵌套次序。
- **边界**：只覆盖 200 流式响应。错误响应（受控注入的 500／429）为
  `application/json`，不适用本条。
- **响应兼容要求**：保持同样的嵌套次序；先分块再压缩会产生不同的 wire 形态。
- **状态**：**响应兼容／已观察**；R 缺完整 M 绑定。

## 2.11 Beta 机制

### SPEC-BETA-001 `ANTHROPIC_BETAS` 的追加位置

- **范围**：`SCOPE-BASELINE` 加本条声明的单一环境变量。
- **规则**：Sonnet 5 下设置单个合法值 `context-1m-2025-08-07` 时，它出现在结果的零基
  **索引 7（第 8 项）**，位于 `mid-conversation-system-2026-04-07` 与
  `effort-2025-11-24` 之间。
- **静态证据**：[A1] 语义锚点 `1796ceb8278d`（跨平台一致）：
  `let c=Z.ANTHROPIC_BETAS; if(c) t.push(...c.split(",").map(u=>u.trim()).filter(Boolean).map(…))`。
  与 SPEC-HDR-008 的 `Og` 不同，`Z` 的 schema 非空，因此该环境分支可达。
- **运行证据**：J；`probe-betas-r3`／`r4` 两个 complete run 的 2/2 个请求均注入
  `context-1m-2025-08-07`，得到 10 项且注入项位于索引 7。未注入负例未纳入本条所选证据分母。
  另有 `probe-betas-r1`／`r2` 注入了不存在的
  `probe-beta-2026-01-01`，服务端返回
  `400 Unexpected value(s) probe-beta-2026-01-01 for the anthropic-beta header`——该失败
  本身反证注入值确实进入了 header，但 run 为 `failed`，不计入正例计数。
- **边界**：只验证单个合法值。多值、重复值与非法值的最终排列未取正例。
- **Sub2API 实现要求（暂定）**：该变量未设置时不得改变基线观察到的 9 项。
- **状态**：**已观察**（Sonnet、单个合法值正例）；原 ID 合并存在、位置、负例与解析算法，
  多值、重复及非法值仍为候选。

## 2.12 候选台账（未闭环）

锚点列为 `CANDIDATE_EVIDENCE.json` 中该候选 Linux 侧 α-归一化摘要的前 12 位；「跨平台」列记录该
锚点与 Darwin 交叉样本是否一致。窗口 sink 按 1.4.1 的边界解读，不构成数据流证明。

| 编号 | 候选命题 | 当前依据 | 锚点／跨平台 | 主要缺口／状态 |
|---|---|---|---|---|
| `CAND-UA-CLI` | `cli`（TUI）入口的 UA 尾段为 `cli` 而非 `sdk-cli` | [A1] `Z.set("CLAUDE_CODE_ENTRYPOINT", e?"sdk-cli":"cli")`——由启动模式自设，带 `-p` 时即使外部预设 `cli` 也会被改回 `sdk-cli`，故只能真跑 TUI | `a20d7b8f44d9`／差异仅为 `BUILD_SOURCEMAP_GROUP` | 引导可用 `.claude.json` 的 `hasCompletedOnboarding=true` 跳过（该字段在 logout 时被重置为 false）；但 `pty` 在 `docker exec` 无 TTY 环境下父进程会卡在 select 且不响应 SIGTERM，八次尝试均未取得正例。需在有真实 TTY 的环境（宿主机 tmux／screen）重试；**候选** |
| `CAND-HDR-USAGE-LIMIT` | depth>0、非 auxiliary 或 compact、firstParty 官方 base、`cachedExtraUsageDisabledReason != null` 且 `tengu_lantern_spool` 为真时发送 `anthropic-usage-limit: extended` | Linux bundle 完整条件 | `2fa1d386deb5`／一致 | gate 无环境变量覆盖路径，触发与否由服务端 Statsig 决定，无法构造正例；**候选** |
| `CAND-BODY-BILLING-CONDITIONS` | attribution text 条件追加 `cc_prev_req`、`cc_is_subagent`、`cc_workload` | A1；`subagent-r1/r2` 已观察前两项 | `d29b00f5d6f5`／一致 | R 缺完整 M，workload 入口不可达；**部分观察／候选** |
| `CAND-BODY-CCH-REWRITE` | 内部 attestation 机制把 `cch` 占位值改写为五字符 token | 2.1.88 注释解释机制；2.1.220 wire 已看到动态五字符结果 | 未建数据流探针 | 五字符样本结果由 `SPEC-BODY-016` 记录，具体内部改写点仍不可观测；**不可观测机制** |
| `CAND-AUTH-BEARER` | `claude.ai` OAuth 路径使用 `Authorization: Bearer` 且不给 SDK apiKey | 2.1.88／HitCC 机制与 R 字节观察 | 未建当前数据流锚点 | R 缺完整 M，需把认证选择链绑定到 2.1.220 sink；**已观察待闭环** |
| `CAND-HDR-TRACEPARENT` | active span 下条件发送 `traceparent` | HitCC 2.1.197 条件说明 | 未建当前锚点 | 需 2.1.220 A1 可达链及有／无 span 单变量正负例；**候选** |
| `CAND-HDR-DISPATCH-RETRY` | 首事件前失败时移除 `anthropic-dispatch-id` 再重试 | HitCC 2.1.197 | 未建探针 | 先要取得远程 gate 正例，再注入指定失败；**候选** |
| `CAND-HDR-CUSTOM-MATRIX` | custom header 的多行、首冒号、重复名、覆盖与非法行规则 | A1 与 2.1.88 旧机制 | `1a63a4012f4a`／一致 | 当前只验证单行唯一名；**候选** |
| `CAND-HDR-REMOTE-MATRIX` | 条件 Header 的异常输入、真实远程来源与生命周期 | `SPEC-HDR-016/017/018/021/022/023/024/026` 已闭环受控未设置／ASCII 值、UA 联动及组合顺序 | 四个跨平台构造锚点＋22-run campaign | 已闭环部分不再是候选；仍需空串／纯空白／非 ASCII／超长值，以及真实 remote container/session 的分配、轮换、回收和与主 session 的关系；**剩余候选族** |
| `CAND-HDR-SESSION-RANDOM` | 不同会话 session-id 不复用及其生成算法 | 当前样本无碰撞 | 未建生成器锚点 | 定义会话边界、跨进程／compact／子 agent 差分与统计门槛；**候选** |
| `CAND-METADATA-EXTRA` | `CLAUDE_CODE_EXTRA_METADATA` 合并 JSON object，非法值报错 | 2.1.88 与 HitCC | 未建当前锚点 | 需合法／非 object／非法 JSON 差分及 wire 正例；**候选** |
| `CAND-BODY-CONDITIONAL-FIELDS` | 条件出现 `temperature`、`tool_choice`、`speed`、`fallbacks`、`fallback_credit_token`、`diagnostics` | HitCC request builder；2.1.88 部分同源 | 未建完整锚点 | 每个字段须拆成独立触发矩阵，不能由“默认十键”否定；**候选族** |
| `CAND-SYSTEM-SEMANTICS` | 四段依次为 billing attribution、产品身份、主 prompt、request-level addition | HitCC；默认 J-raw/R | 未建稳定摘要矩阵 | 需按段建立归一化摘要、动态字段规则及至少两轮重复；**部分覆盖** |
| `CAND-CACHE-MESSAGE` | 最后一个非 `api_system` message、`skipCacheWrite` 与 fork point 决定 message cache breakpoint | HitCC 2.1.197 | 未建当前锚点 | 需默认／skip／fork 三组正负例；**候选** |
| `CAND-CACHE-SYSTEM-SCOPE` | system cache scope 为 null/org/global，受动态边界与 MCP schema 影响 | HitCC 2.1.197 | 未建当前锚点 | 需 2.1.220 gate 可达链及 scope 差分；**候选** |
| `CAND-TOOLS-EXTENDED` | tool schema 可含 `strict`、`eager_input_streaming`、`defer_loading` | 2.1.88 与 HitCC | 未建当前锚点 | 当前简单工具三键不能外推全部工具；**候选** |
| `CAND-TOOLS-DEFERRED` | ToolSearch 与 deferred tool 改变 tools 数组及 beta | HitCC 2.1.197 | 未建当前锚点 | 需可触发 ToolSearch 的 TUI／MCP 场景；**候选** |
| `CAND-SERVER-WEBSEARCH` | `web_search_20250305`、domains、`max_uses:8` 与条件 tool_choice | HitCC 2.1.197 | 未建当前锚点 | 需功能 gate 正例；**候选** |
| `CAND-SERVER-ADVISOR` | `advisor_20260301` server-tool schema | HitCC 2.1.197 | 未建当前锚点 | 先判定 2.1.220 是否可达；**候选** |
| `CAND-QUOTA-PROBE` | quota_check 使用 `messages:[quota]`、`max_tokens:1` 与 metadata | HitCC 2.1.197 | 未建当前锚点 | 需构造 quota 触发并与普通 messages 分流；**候选** |
| `CAND-STREAM-NONSTREAM` | 缺 message_start、无完整 block、watchdog、stream exception 或 404 后重发 `stream:false` | 2.1.88 与 HitCC | 未建完整故障矩阵 | 每种触发原因至少两轮，并关联原请求 id；**候选族** |
| `CAND-FALLBACK-PROTOCOL` | fallback betas、`fallbacks`、credit token 与 400 strip/retry | HitCC 2.1.197 | 未建当前锚点 | 需账号／gate、合法 token 与受控 400 对照；**候选族** |
| `CAND-CACHE-DIAGNOSIS` | `cache-diagnosis-2026-04-07` 与 `diagnostics.previous_message_id` | HitCC 2.1.197 | 未建当前锚点 | 需关联前一响应 id 的两轮差分；**候选** |
| `CAND-RETRY-MATRIX` | 401／403／408／409／429／529、其他 5xx、Retry-After、x-should-retry、OAuth 刷新与精确终止边界 | 2.1.88 `withRetry.ts`；HitCC；`SPEC-CONN-002/003` | 500／无 Retry-After 的当前 A1 锚点与六轮完整 M | 普通 500 的公式和至少 9 次已闭环；仍需 count=10/11、各状态码、有／无／非法 Retry-After、断连及刷新分支；**剩余候选族** |
| `CAND-RETRY-529-FALLBACK` | 连续 529 达阈值后切换模型，context overflow 时改写 max_tokens | 2.1.88 与 HitCC | 未建当前锚点 | 需长链受控注入与模型／body 差分；**候选** |
| `CAND-NONMAIN-THREADS` | fork、side question、hook_prompt、hook_agent、compact summarize、后台与并发 agent 的专用形态 | HitCC 2.1.197；`SPEC-HDR-019/025/026` | general-purpose 深度 1／2／3 已有六轮完整 M | general-purpose 的 parent 存在、直接父级相等与组合顺序已闭环；其他非主会话类型、并发及各自 Body／Header 差分仍未采；**剩余候选族** |
| `CAND-BETA-CUSTOM` | 自定义 beta 与 API Key／OAuth 路径存在条件差异 | 当前警告文本与 2.1.88 参数指向“API Key 用户可用” | 未建探针 | API Key 路径不在对齐范围；**范围外** |
| `CAND-EP-COUNTTOKENS` | `/v1/messages/count_tokens?beta=true` 端点及其四项 beta 子集 | [A1] `i.filter(d => Aji.has(d))`；调用点为 `beta.messages.countTokens` | 未建探针 | 该端点在现有场景未触发；需构造 token 计数及 Haiku fallback 对照；**候选** |
| `CAND-EP-SDK-SURFACE` | batches、models、files 等 SDK surface 是否被 Claude Code 2.1.220 实际调用 | HitCC 端点清单 | 未建 reachability | “wrapper 存在”不等于业务可达；逐端点做当前调用链证明；**候选族** |
| `CAND-EP-AUXILIARY` | bootstrap、settings、policy_limits、OAuth refresh/profile/roles 等必要辅助请求 | 2.1.88 与 HitCC | 未建全量发现 | 在 default/essential 两种隐私模式下无 host 预过滤发现；**候选族** |
| `CAND-EP-FULLSET` | 客户端在本版本使用的端点全集 | `SPEC-EP-001` 记录推理 path，`SPEC-EP-002` 记录 HEAD 观察，`SPEC-TLS-003` 才是无 host 预过滤的 TLS 样本 | 未建探针 | 全部证据取自 `essential-traffic` 模式，`default` 模式下 53 处门控请求会放行；还需覆盖非 443、OAuth 刷新与其他场景；**候选** |
| `CAND-BG-SESSION` | 后台 agent 会话（`--bg`）的 `x-app` 为 `cli-bg` | [A1] `"x-app": rs()?"cli-bg":"cli"`，`rs()=Wot()==="bg"` | 未建探针 | **与 `CAND-UA-CLI` 同源**：客户端明确拒绝 `--bg` 与 `-p` 并用（"--bg and --print conflict: --print never starts the interactive session"），因此后台会话只能经 TUI 驱动，卡在同一个 TTY 障碍上；**候选** |
| `CAND-WORKLOAD` | attribution text 的 `cc_workload` 字段随 workload 变化 | [A1] `HYn()` 读 `AsyncLocalStorage`，已知值之一为 `cron` | 未建探针 | workload 由进程内路径设定，环境变量注入不了；当前 `sdk-cli` 入口不可达；**不可观测机制** |
| `CAND-WIRE-H2` | HTTP/2 帧、SETTINGS 与 HPACK 形态 | SPEC-TLS-002 表明基线只 offer `http/1.1` | 不适用 | 基线路径不协商 h2，无从观测；**不可观测机制**（除非出现 h2 分支） |

正文候选表按可执行抓包族合并，原子级完整清单在两份来源覆盖 JSON 中。锚点相同只说明两平台
出现同一段逻辑，不说明它在默认 OAuth 路径可达。

### 2.12.1 明确版本漂移、冲突与范围外

| 项目 | 处置 | 理由 |
|---|---|---|
| HitCC `currentDate` 三 bit marker | **已移除／不得迁入** | HitCC 自述从 2.1.201 起已看不到；不能定义 2.1.220 |
| HitCC “API messages 只含 user/assistant” | **已变化** | 2.1.220 抓包出现 mid-conversation `system` role |
| 2.1.88 自定义 header 覆盖优先级 | **已变化／待重验** | 旧 SDK 0.74.0，当前为 0.94.0，不能继承展开顺序 |
| 2.1.88 `GET /api/hello`／`GET /v1/oauth/hello` | **已变化** | 当前只观察 `HEAD /api/hello`，未观察 oauth hello |
| 2.1.88 beta 列表与排列 | **已变化** | 当前 A1 注册表、默认序列与插入点均不同 |
| API Key、Bedrock、Vertex、Foundry、Mantle、Gateway | **范围外** | 不属于 `claude.ai`／firstParty OAuth 对齐目标 |
| telemetry、GrowthBook、voice、transcript share、MCP proxy、update/plugin download | **外围出站** | 已按类别登记；具体机制与端点尚未全量原子化，不纳入模型数据面发包画像；隐私模式决定可达性 |
| CA／proxy 配置 | **机制信息** | 影响连接建立，不能推出 2.1.220 ClientHello wire 指纹 |

### 2.12.2 最小补抓顺序

client-app、container、remote-session、深度 1／2／3 agent、组合 Header 顺序，以及 500 的
count=3／5／9 已由本轮 22 个完整 M 运行补齐，**不再列为待补证**。剩余最小顺序为：

1. 为 `relay-r1/r2`、`subagent-r1/r2` 补齐二进制 SHA、argv、完整环境、采集器摘要、状态、脱敏与
   秘密扫描，再晋级原始 HTTP wire、HEAD 和旧复合子 agent 规则。
2. 补 remote/client-app 的空串／纯空白／非 ASCII／超长值，并在真实 remote container/session
   环境核对来源和生命周期；另补 retry count=10/11，确定精确终止边界。
3. 补 custom-header 多行／覆盖、beta 多值／重复、extra metadata、traceparent。
4. 补三轮会话、compact、fork／hook、简单多工具、MCP、ToolSearch、server tool、后台与并发 agent。
5. 按 401／403／408／409／429／529、其他 5xx、断连、`Retry-After` 与 OAuth 刷新分支补完整
   retry/fallback 矩阵；不能用已验证的 500／无 Retry-After 曲线替代。
6. 在 `essential-traffic` 与 `default` 两种隐私模式下，无 host 预过滤发现 count_tokens、quota、
   OAuth 与辅助端点。
7. 在真实 TTY 上采集 TUI 与 `cli-bg`；`-p` 会强制 `sdk-cli`，不能用环境变量伪造入口。

## 2.13 36 个历史编号迁移审计

下表只回答旧编号如何处置，不表示 Sub2API 实施优先级；当前对齐责任以 2.1、2.2 和机器台账为准。
`已验证` 仅指“保留命题”中的收窄命题，不继承旧标题或旧正文中的其他全称断言。

| 历史编号 | 状态 | 保留命题／处置 | 移出的命题与缺口 |
|---|---|---|---|
| `SPEC-TLS-001` | 已观察 | 该 Linux SHA 的已采 direct 场景出现同一 ClientHello 扩展序列 | 原 ID 合并序列与跨模型不变性；旧聚合分母未绑定 |
| `SPEC-TLS-002` | 已观察 | 所选 pcap 的全部 8 个 ClientHello 只 offer `http/1.1` | 完整 M 未闭合；不外推其他平台、provider 或失败链 |
| `SPEC-PROTO-001` | 已观察 | 所选 6 个 J 请求的解析结果为 HTTP/1.1 | 完整 M 未闭合；原始请求行另由 R 观察 |
| `SPEC-HDR-001` | 已观察 | 基线 Sonnet 推理请求的 22 项 header 名、大小写及相对顺序 | 原 ID 复合且旧聚合分母未绑定；条件组合顺序已拆到已验证的 `HDR-026` |
| `SPEC-HDR-002` | 已观察 | 所选 6 个 J 请求的 `sdk-cli` UA 精确值 | A1 只有局部 sink 近邻，完整 M 未闭合；可选段留候选 |
| `SPEC-HDR-003` | 已观察 | Sonnet 5 基线 9 项 beta 序列 | 序列为复合值；Opus 一轮，Haiku 正例不存在 |
| `SPEC-HDR-004` | 已观察 | 基线路径出现 `anthropic-version: 2023-06-01` | 旧聚合分母未绑定，缺当前 sink 静态闭环 |
| `SPEC-HDR-005` | 已观察 | 解析值为 `gzip, deflate, br, zstd` | 旧聚合分母未绑定；wire 字面只由缺 M 的 R 观察 |
| `SPEC-HDR-006` | 已观察 | 基线 Linux 的八项 Stainless 值向量 | 复合八字段；旧聚合分母未绑定 |
| `SPEC-HDR-007` | 已观察 | 前台基线出现 dangerous=`true`、`x-app=cli` | 两个独立 Header 合并；`cli-bg` 无正例 |
| `SPEC-HDR-008` | 已观察 | 基线路径未见 `anthropic-dispatch-id` | 复合负例与远程 gate；正例不可达 |
| `SPEC-HDR-009` | 已观察 | 变量值 `1` 时出现 additional-protection | 触发、值、位置和负例尚未拆号 |
| `SPEC-HDR-010` | 已观察 | 单行 `X-Egress-Probe: probe-alpha` 的出现、值与位置 | 多个可独立失败结果合并；扩展矩阵未采 |
| `SPEC-HDR-011` | 已取代 | 不再作为活动规则 | 混淆 HTTP Header 与 Body 文本；由 `HDR-020`、`BODY-007` 替代 |
| `SPEC-HDR-012` | 已观察 | 现有样本未见 request-id 碰撞 | 缺当前生成器可达证据与明确统计门槛 |
| `SPEC-HDR-013` | 已观察 | 同一已采会话内复用 session-id | 跨请求状态；旧聚合分母未机器绑定 |
| `SPEC-HDR-014` | 已观察 | 一层 `general-purpose` 子 agent 出现 agent-id | 证据实为 `subagent-r1/r2`，R 缺完整 M |
| `SPEC-HDR-015` | 已观察 | 已采主／子 agent 共用 Header session-id | metadata 相等属于 Body；R 缺完整 M |
| `SPEC-HDR-016` | 已验证 | 受控 client-app 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-021`，UA 联动拆到 `HDR-022`；异常输入仍候选 |
| `SPEC-HDR-017` | 已验证 | 受控 container ID 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-023`；真实远程来源与生命周期未证 |
| `SPEC-HDR-018` | 已验证 | 受控 remote session ID 未设置时 Header 缺席，设置为已采非空值时存在 | 精确值拆到 `HDR-024`；与主 session 的同源关系未证 |
| `SPEC-HDR-019` | 已验证 | `general-purpose` 深度 0／1 缺席、深度 2／3 存在 parent-agent-id | 直接父级相等拆到 `HDR-025`；其他 agent 类型、并发与更深层级未证 |
| `SPEC-BODY-001` | 已观察 | 基线 Sonnet 请求体顶层十键集合 | 键集合与序列化顺序合并；分母未绑定 |
| `SPEC-BODY-002` | 已观察 | metadata 的 user_id 内嵌 JSON 与 session-id 相等 | 编码、键集、生命周期、跨层相等四个命题合并；25 分母未绑定 |
| `SPEC-BODY-003` | 已观察 | 主会话基线为四段 text，索引 2、3 带 cache_control | 段数、类型和缓存位置未拆号；22 分母未绑定 |
| `SPEC-BODY-004` | 已观察 | 已采两轮会话出现两项→五项角色序列 | 每种续轮配对不足两次；三轮与 compact 未采 |
| `SPEC-BODY-005` | 已观察 | s1/s2 基线无工具时 `tools: []` 且字段不省略 | 须使用 `SCOPE-BASELINE-NO-TOOLS`；工具 schema 另拆 |
| `SPEC-BODY-006` | 已观察 | 子 agent 首请求为三段 system、单条 user message | R 缺完整 M；子 agent 续轮未采 |
| `SPEC-EP-001` | 已观察 | 已按 `/messages` 路由的 6 个 J 请求，其精确 path 为 `/v1/messages?beta=true` | path 部分预筛；method、HTTP host、TLS SNI 已拆号；完整 M 未闭合 |
| `SPEC-EP-002` | 已观察 | 启动前观察到 `HEAD /api/hello` 及五项 header | R 缺完整 M；先后关系、独立连接和响应应拆分 |
| `SPEC-CONN-001` | 已观察 | 两轮单次 500 后重发且 Stainless retry-count 仍为 0 | 重发与 Header 两结果合并，且缺 P/R/J 流级关联 |
| `SPEC-CONN-002` | 已验证 | `500` 且无 `Retry-After` 时为 500 ms 起步、2 倍指数、32 s 基数封顶、最高 25% 抖动 | 九次失败后的第十次请求拆到 `CONN-003`；其他错误、Retry-After、精确终止边界未证 |
| `SPEC-RESP-001` | 响应兼容 | 成功响应的 SSE／chunked／gzip 形态 | 不计客户端 egress |
| `SPEC-RESP-002` | 响应兼容 | 响应 header 动态性观察 | 旧聚合口径尚无机器全表，不得固定 CDN 顺序 |
| `SPEC-RESP-003` | 响应兼容 | 观察到先 gzip、后 chunked | R 缺完整 M，不计客户端 egress |
| `SPEC-BETA-001` | 已观察 | Sonnet 下单个合法 beta 位于零基索引 7（第 8 项） | 存在、位置、负例与解析算法未拆号 |

## 2.14 HitCC 2.1.197 线索覆盖结论

HitCC 目录固定为 commit `f4556e5b18a65232023998219e53c2598cc17d82`。仓库内有 112 个 Markdown
（其中 `docs/` 下 110 个），但没有文档反复引用的 pretty bundle，因此它是 A4 线索地图，不是
2.1.220 证据本体。

当前已抽取线索矩阵见 `hitcc_2_1_197_coverage.json`。目前**已抽取** 88 条原子线索：覆盖 9、部分覆盖 65、
缺失 4、范围外 10；每条都有 `covered／partial／missing／out_of_scope` 处置以及
`same／changed／removed／unknown／not_applicable` 漂移状态。这里的 `covered` 包含由正式规则或候选表
承接的线索，不能等同于 `verified`。

本轮 `HITCC-HDR-004/005/006/008` 四条由 `partial/unknown` 升为 `covered/same`，依据分别落在
remote container、remote session、client-app 和 parent-agent 的当前 A1 锚点及完整 M 规则；没有
因此改写其他 84 条线索的处置。

该计数已按当前原子 `retained_claim` 重算：默认 baseURL、完整会话生命周期、Anthropic AWS 条件、
普通工具三键 schema，以及 structured output／server-side tool 共用 messages 端点等旧复合命题，
都因现有规则只承接其中一部分而降为 `partial`；不能再由同名历史 ID 反向继承其余语义。

全库 112 篇 Markdown 的文档级盘点为：18 篇直接线索源已映射到上述 88 条，另有 **53 篇直接线索源
尚未逐条抽取**，33 篇仅交叉引用，8 篇没有独立出站线索。因此问题“所有 HitCC 线索是否都已在
规则中”的答案是：**否**。现有 88 条已全部分类，但它们不是 HitCC 全量线索；53 篇未抽取文档是
明确缺口，在完成原子化与 2.1.220 复验前不得宣称全覆盖。

这里的 112 个路径、88 个 clue ID 和反向映射可机器复核；`clue_source`／`cross_reference_only`／
`no_egress_clue` 以及“是否仍有独立线索”属于**人工语义判断**，checker 不能从 Markdown 自动证明
分类正确或抽取穷尽。71／18／53 是当前审阅台账口径，不是由文件数单独推出的客观常量；即使调整
边界文档的分类，当前仍有 4 条显式 `missing` 和大量未抽取文档，故“否”的结论不依赖争议分类。

## 2.15 Claude Code 2.1.88 源码迁移结论

2.1.88 完整取证根共 4756 个文件：`src/` 1902、`node_modules/` 2850、`vendor/` 4，三部分恰好闭合；
完整根确定性路径＋内容摘要为
`dafc1b37756e0f6bb312a8bf5c98c115c40a65d5d87cc1aa80910cf6e956878f`。其中 `src/` 摘要为
`d865dbfa59f24563fedc767a425f1e2e35ff15a458f478ebf6ac24d800cef4a8`；`node_modules/` 摘要为
`f049798ba432bd9286c299810c95e5c72d6ae40a4ff78911bcc3750b3e56ed2a`；`vendor/` 摘要为
`6d2d7395c398aa05e42e7b2b89c239b85de1ba9fd8621f3c4635924ed2ee6455`。`@anthropic-ai/sdk` 是
`node_modules/` 的 51 文件子集，摘要为
`0a1e18ded2ef751f5c8ff6e7d4199d4f855f916cc3094e69096ebd49447c1c30`；其 `version.mjs` 直接声明
0.74.0。该取证根没有可复核的官方来源 manifest，且目标版本 SDK 上报为 0.94.0，因此只能作为 A3
机制词典；机器门禁直接复核 `SCOPE-J6` 的 6/6 个请求均恰有一个
`x-stainless-package-version: 0.94.0`，不只读取迁移台账自报值。

`source_2_1_88_coverage.json` 当前只是 **`src/` 种子矩阵**：102 条候选与 1902 条
`file_inventory` 均只覆盖应用源码；依赖源码只完成确定性快照，**尚未做语义规则抽取**。目前已抽取
**102 条种子候选机制**：正式运行证实 4、仅静态／样本相容 39、已变化 3、未证实 38、范围外 18。
`runtime_verified` 使用硬门槛：旧命题的全部内容必须被当前 atomic verified SPEC 完整承接，不能把
旧版条件机制改写成一个相同基线值。

本轮人工复核后，`runtime_verified` 恰为四条：
`SRC2188-HDR-005`（container Header 条件存在）、`SRC2188-HDR-006`（remote-session Header
条件存在）、`SRC2188-HDR-007`（client-app Header 条件存在）及 `SRC2188-RETRY-006`（普通应用层
无可用 `Retry-After` 时的 500 ms 起步、32 s 基数封顶、最高 25% 抖动函数）。前三条由当前 A1
跨平台可达锚点和各两轮正负例闭环；第四条由当前 A1 通用 delay 函数及 500 count=3／5／9 各两轮
验证其运行可达与逐 attempt 区间。它**不**证实 `Retry-After`、persistent/watchdog 分支，也不证明
哪些其他状态可重试。

门禁要求这四条的集合与人工复核清单双向相等，并要求它们引用的 `spec_rule_ids` 都属于当前 verified
集合；它不要求每个新 verified SPEC 反向对应一条旧源码命题。checker 仍不能自动证明旧 proposition
被完整包含，完整承接需逐条人工语义复核。`x-app: cli`、metadata 三键结构、metadata 内 session-id
与 Header 相等三条历史命题虽与当前样本一致，但映射到的历史 SPEC 仍是复合 observed ID，因此只记
`static_only`。另有 7 条只观察到收窄后的当前子命题（UA／entrypoint／session 复用／protection 值 1／
billing 载体／adaptive thinking），保存在 `observed_subclaim`，同样不能计作旧机制整体得到证实。

`src/` 的 1902 个源码路径已确定性列举：21 个是种子规则直接来源，237 个是直接导入层支持，1644 个尚未
闭合。其中 458 个“命中词法候选信号”与 1186 个“未人工排除”只是台账现有 `scan_signals`
字段的内部计数；由于扫描器及其规则未归档，**不能从 2.1.88 源码独立复算该词法分组**。237 个
transitive 文件中又有 142 个同时出现非导入信号，单一 disposition 可能掩盖独立出站。已明确记录
referral、admin、
quota、voice、WebSocket、SSE、MCP 登录等 11 个漏分样本。因此这只是路径枚举和保守分流，不是完整
数据流证明；`node_modules/` 与 `vendor/` 又尚未进行语义盘点。答案是：**否；102 条只是 `src/`
种子候选，源码及依赖源码的所有出站机制尚未完整抽取；当前只有上述 4 条旧命题达到正式准入，
不能把它们外推为“所有源码规则均已证实”。**

## 2.16 机器门禁

运行：

```bash
python3 tools/official_client_capture/claude_21220/check_coverage.py
```

该命令是本地取证门禁，要求被 `.gitignore` 排除的 `local-analysis/` 证据树完整存在；它不是脱离证据
归档即可运行的普通 CI 单测。

门禁至少检查：57 个编号完整覆盖且唯一；`32 + 21 + 0 + 3 + 1` 的责任集合无重叠；文档、JSON 与
checker 同时对账目标 53、已验证 12、已观察 41、已编号待补证 0、排除 4；历史 36 ID 全处置且
`HDR-021..026`、`CONN-003` 不复用旧 ID；三份 JSON 可解析且枚举合法；
本轮 22 个 manifest 均为 `complete`、完整 M、运行镜像已验证、运行内及终态精确秘密扫描无命中且清理成功；
campaign 绑定摘要为 `b2bdc967114b9bd4001ee8da0feec0f79e5a2490fc9bf03dc59780298a454d98`，并按各自
host receipt 接受 12／8／1／1 四组采集执行源摘要，不要求错误的单一源码摘要；
四个条件 Header A1 锚点在 Linux／Darwin 各唯一命中且 α 摘要一致；正负例、agent 深度序列、
parent 直接相等关系、组合 Header 顺序及 500 count=3／5／9 的请求／故障／重试计数从原始 J 与
生命周期记录复算；
追溯 `SCOPE-J6` 的 host allowlist、`/messages` 路由和当前 addon 摘要，禁止 `EP-001/004` 声明
独立验证分母，并对其余 16 条有限样本规则复核待证字段独立性；所选 P 分母从原始 pcap 重算；实际 A1 Linux 二进制摘要与
SOURCE_MANIFEST 一致；HitCC 工作树 clean，112 篇 Markdown 的路径＋内容摘要与文档盘点闭合；
HitCC 已抽取统计为 covered 9、partial 65、missing 4、out_of_scope 10，且仍保留 53 篇直接线索源
未逐条抽取的边界；
`SCOPE-J6` 的 Stainless package version 为 0.94.0；2.1.88 完整取证根、`src/`、`node_modules/`、
`vendor/` 与 Anthropic SDK 的文件数和路径＋内容摘要可复算；`runtime_verified` 集合与人工复核清单
双向等于 `SRC2188-HDR-005/006/007`、`SRC2188-RETRY-006`，来源统计为 4／39／3／38／18，且每条
SPEC 引用单向属于当前 verified 集合；正文不得给出与 2.7／2.8 相反的肯定结论，也不得声称
“36/36 已验证”“全部 HitCC 已覆盖”或“全部 2.1.88 源码规则已证实”。门禁同时强制记录
当前 M、A1 数据流、依赖语义抽取和扫描器复算边界；它不把这些未完成项伪装成机器已证明。

## 2.17 正式规则模板

```text
### SPEC-<分组>-<编号> <名称>
- 范围：版本／摘要、平台、entrypoint、provider、模型／cohort、触发条件及明确不覆盖项
- 规则：一个可独立成败的官方可见命题；写明条件、缺席行为和可变性
- 静态：A1／A2 锚点及 sink 路径，或不适用原因
- 实测：P／R／J／M、具名运行号、正负例、重复数、完整性与观测边界
- 实现：Sub2API 的生成／省略／顺序／类型／生命周期；只描述可见结果
- 状态：verified／observed／candidate／response_compat／superseded；实现状态和 PAIR 断言见机器台账
```

新建或升格规则固定使用以上六字段和顺序；历史复合「已观察」条目可暂留边界补充。实现责任、成对
验收 ID、实现位置与断言状态放在机器台账，避免把证据状态与 Sub2API 实现状态混写。

---

# 第三部分 Sub2API 客户端仿真实现

**本部分是设计规范，不是现状记录。** 它规定 Sub2API 实现第二部分规则时**应当**具备的架构；
现状与之的差距集中登记在 §3.5，迁移路径见 §3.9。当代码与本部分冲突时，以本部分为准并改代码，
不得反过来把既有实现写成规范。

设计的第一驱动力是**换版频率**。Claude Code 的发布节奏远高于 Codex CLI：2026-07-24 至 08-14
的三周内发布了 2.1.220 至 2.1.233 共 13 个补丁版本。任何"换版需要修改执行代码"的架构都无法
持续跟随，因此本部分的硬性目标是：

> **换版只追加版本画像数据与证据，不修改执行代码。**
> 2.1.220 → 2.1.224 的升级，应当只需新增一个画像快照并通过第四部分验收。

由此派生四条不可让步的设计约束：版本身份必须收敛为数据；active／previous 必须并存且可回滚；
条件规则必须由画像声明而非代码分支；版本常量必须在业务代码中绝迹并由门禁强制。

## 3.1 总体架构与 persona 边界

账号选定后，生产请求绑定 active ReleaseBundle，再由 Compiler、Executor、受信传输适配器与
Runtime Guard 完成最终出站定型。previous 只用于受控回滚、测试或显式灰度；调用方不得直接选择
版本或传输 ID。

```text
官方 Claude Code 入站 ─┐
                        ├─→ 归一化语义请求 ─→ ClaudeIdentityFacts + ClaudeEgressPlan
第三方标准 API 入站 ───┘                              ↓
        ReleaseCatalog（生产默认 active）→ ReleaseBundle → Compiler → CompiledExecution
                              ↓
        Executor 签发 PreparedRequest + FinalizationToken + TransportSpec
                              ↓
        受信 HTTP adapter → Runtime Guard → Anthropic
```

入站客户端类型**不是**画像函数的输入（§1.1）。兼容层只提交协议、模型、工具、请求语义和可验证
条件；Key、Group、账号路由与计费仍由原有业务系统管理，兼容层不拥有版本身份、传输画像或最终 wire。

| 平面 | 拥有 | 不得影响 |
|---|---|---|
| 入口归一化 | 协议与语义适配、条件类型识别、观测标注 | active ReleaseCatalog、画像摘要、最终 version 与 wire 契约 |
| 生产 strict wire | ReleaseCatalog、ReleaseBundle、Compiler、Executor 与受信 adapter 定型的 URL、Header、Body、顺序、压缩、传输、状态与连接 | 被入站身份、账号 UA 或管理端候选值覆盖 |

persona 由账号认证类型与 route registry 确定，ReleaseCatalog 再按受控 mode 解析 ReleaseBundle：

| persona | 端点范围 | 约束 |
|---|---|---|
| `claude-code` | ReleaseBundle 登记的 Claude 端点闭集 | URL、Header、Body、传输、状态与生命周期由同一 Bundle 驱动 |
| API Key mimic | Anthropic API Key 产品路径 | 不属于 §1.1 对齐目标，**禁止套用 `claude-code` 画像**，独立治理 |
| `transport_only` | OAuth authorization-code 交换 | 只复用有证据的传输事实，不冒充业务请求的 Body 契约 |
| `unclassified` | 尚未举证的遗留路径 | 完成官方行为举证前不得凭官方 host 自动归入 `claude-code` |
| 未登记 | Catalog 未知 route 或 SinkBinding | enforce 状态下 fail-close |

入站事实的处置固定如下：

| 入站事实 | 处理 |
|---|---|
| UA、客户端版本、`x-app`、会话 UUID、metadata | 不拥有 wire 身份；按画像与 active 命名空间重建 |
| Header、Body 或 metadata 的身份冲突 | 丢弃冲突原值，派生同一生命周期的新身份 |
| 条件事实缺失或冲突 | 按条件不成立处理，**不伪造 Header**，不发送空值占位 |
| 顶层字段超出画像闭集 | 删除并按「入口类型 + 字段集合」去重告警 |
| 未被画像消费的第三方 Header | 不透传到官方上游 |

账号配置非法、route／Sink 未登记或终态被篡改时 fail-close；其余身份不匹配只投影并告警。生产路径
不执行入站身份逐字段一致性校验，该校验只用于离线夹具、画像诊断与证据复算。

## 3.2 版本画像的数据化契约

**这是支撑频繁换版的核心。** 版本画像必须是内容寻址的不可变数据文件，而不是 Go 常量、结构体
字面量或构造函数。参照 Codex 的既有实现（`backend/internal/officialegress/catalogdata/runtime/`
下按版本分目录、以画像摘要命名的 JSON），Claude 画像存放于：

```text
backend/internal/officialegress/catalogdata/claude/profiles/<version>/<digest>.json
```

`version + digest` 是不可变坐标。摘要变化即完整画像变化，必须同步检查第二部分规则、版本清单、
测试与抓包。启动期解码、结构校验或摘要核对失败即阻止启动；运行时为只读不可变快照，需改写的
数据按次深拷贝。

### 3.2.1 画像必须承载的内容

画像要覆盖第二部分全部 53 项对齐责任，因此至少包含以下段。它比 Codex 现有画像多出**条件 Header
规则**与**重试曲线参数**两段，因为 Claude 的条件规则占比高（21／53）：

| 段 | 承载内容 | 对应规则 |
|---|---|---|
| `Version` | 版本号与 UA 版本串的唯一来源 | `SPEC-HDR-002` |
| `RequiredRules` | 本版本必须对齐的 SPEC 编号集合与状态等级 | §2.2 全部 |
| `Transports` | TLS 扩展序列、cipher、groups、签名算法、ALPN、SNI 策略 | `SPEC-TLS-001/002/003`、`SPEC-PROTO-001` |
| `Endpoints` | 端点闭集：method、host、path、query、accept、content-type、压缩、连接生命周期、Header 顺序模式 | `SPEC-EP-001..004` |
| `HeaderSlots` | 逐槽位的名称、线上大小写、值来源、**触发条件**、互斥组与顺序 | `SPEC-HDR-001..026` |
| `BetaPolicy` | beta 序列、插入位置与条件项 | `SPEC-HDR-003`、`SPEC-BETA-001` |
| `BodyShape` | 顶层键闭集与顺序、system 段结构、cache_control 位置、metadata 编码 | `SPEC-BODY-001..016` |
| `RetryPolicy` | 起步延迟、指数底、封顶基数、抖动上限、终止边界 | `SPEC-CONN-001/002/003` |
| `PrivacyMode` | 本画像证据所属的隐私模式，以及各端点在该模式下是否放行 | §1.1 三级模型 |
| `Digest` | 画像自身的内容摘要 | 版本坐标 |

### 3.2.2 Header 槽位机制

条件 Header 是 Claude 与 Codex 差异最大的面，必须由画像声明，**不得写成代码里的 if 分支**。
每个槽位至少声明：

```text
Slot        槽位号，决定相对顺序；组合顺序由槽位号唯一确定
Name        内部名
WireName    线上实际大小写（H1 header 名的字面形态属于规则）
Value       常量值；非常量时留空
Source      constant | authentication | account | session | agent | environment | turn
Condition   always | 条件表达式（引用画像声明的条件类型）
AlternateGroup  互斥组：同组至多出现一个
```

条件求值只接受**画像声明的条件类型**，其输入来自归一化语义请求与运行上下文，不接受入站
Header 的原值。第三方入站只有在规范化语义明确表达同一条件时才能触发该槽位；携带同名 Header
不构成触发理由（§2.2.2）。条件不成立时必须整条省略，不得发送空值。

### 3.2.3 active／previous 与发布图

ReleaseCatalog 启动时加载 release graph 与画像目录；active／previous 指向完整 release ID，
未登记坐标不得回退。新增版本只能**追加**快照与发布图节点，不原位覆盖旧画像，也不在共享执行
路径上散布版本分支——这一约束由文本与 Go AST 版本泄漏门禁强制执行（§3.7）。

版本泄漏 baseline 只记录尚未消除的既有债务，不是新版本硬编码的批准清单。换版或晋升不得把新
指纹写入 baseline 以换取门禁通过。

## 3.3 最终出站定型

### 3.3.1 运行上下文

入口只保存协议与语义事实、传输状态、业务历史和可验证条件类型。账号选定后，Identity Authority
将其投影为 `ClaudeIdentityFacts`，与 ReleaseBundle、端点、模型能力、会话状态与条件 Header 绑定。
UA、版本、`x-app`、entrypoint 与 Stainless 向量的默认值来自受信 ReleaseCatalog Build，**不来自
入站客户端**。上下文只属于当前 invocation 或 attempt。

### 3.3.2 URL、Header 与 Body

画像拥有 host、固定 path／query、Header 槽位与 Body 闭集，只允许画像声明的动态字段。
HTTP/1.1 在写出前定型 header 名的大小写、顺序、host 与长度。Compiler 对静态端点执行封闭校验：

| URL 成分 | Compiler 契约 |
|---|---|
| 形态与 authority | 拒绝 opaque、userinfo、fragment 与显式端口；只接受精确小写 `https`；Host 与画像逐字相等 |
| path | 段数与画像模板相等；字面段逐字相等，`{param}` 段非空 |
| query | 只允许画像声明的键；`beta=true` 一类常量值必须逐字相等，不得被入站改写 |

Body 按画像的顶层键闭集与稳定顺序输出，并保持 JSON 数值保真。system 段结构、`cache_control`
位置与 metadata 的内嵌编码均由 `BodyShape` 定义。已知值不合契约时按画像规范化，不拒绝请求。

### 3.3.3 状态、连接与重试

会话状态按 invocation 身份隔离，只从画像规定的位置更新。跨请求复用要求入站显式携带可信锚点；
没有可信锚点时仍可确定性派生当前身份，但不跨请求复用上游状态，避免把一段对话的状态句柄带入
另一段。

HTTP Client、连接生命周期与重试曲线均由端点画像声明。重试必须按 `RetryPolicy` 执行，不得在
业务代码中另写退避逻辑；辅助端点与 OAuth 刷新不得旁路统一执行器。

## 3.4 对齐责任的落点与成对验收

§2.2 的 53 项责任必须在画像或执行点上各有唯一落点。按分组的落点归属如下：

| 规则分组 | 项数 | 画像落点 | 执行点 |
|---|---:|---|---|
| TLS | 3 | `Transports` | 受信 transport adapter |
| HTTP 协议 | 1 | `Transports.Protocol` | adapter 协商 |
| Header | 25 | `HeaderSlots` + `BetaPolicy` | Compiler 定型顺序与大小写 |
| Body | 16 | `BodyShape` | Compiler 序列化 |
| 端点 | 4 | `Endpoints` | Compiler URL 封闭校验 |
| 连接与重试 | 3 | `RetryPolicy` + `Endpoints.ClientLifecycle` | Executor 与连接池 |
| Beta 机制 | 1 | `BetaPolicy` | Compiler |

每项必须同时具备画像或执行点、官方与候选证据、机器断言三者。`PAIR-<SPEC-ID>` 要求官方入口与
第三方入口在同一候选制品与画像下分别执行断言：对相同规范化语义与画像条件，两侧必须命中相同的
可见结果；动态字段按规则声明的格式、相等关系与生命周期比较，不比较某次抓包的字面值。

多账号调度、计费与服务级请求节奏不在对齐责任内，画像不改写它们。

## 3.5 现状差距与改造台账

本节是本部分唯一记录现状的地方。当前实现**不满足** §3.2 的数据化契约，主要差距如下：

| # | 差距 | 证据 | 后果 |
|---:|---|---|---|
| 1 | 版本身份是 Go 常量 | `backend/internal/pkg/claude/constants.go`：`CLICurrentVersion = "2.1.220"`、`DefaultHeaders`（UA 与 Stainless 八项）、`DefaultBetaHeader`、`FullClaudeCodeMimicryBetas()` | 换版必须改代码、重编译、重部署 |
| 2 | 版本身份散布 | 17 个非测试文件引用 `internal/pkg/claude`；`identity_service.go`、`gateway_upstream_request.go`、`gateway_claude_oauth_body.go`、`upstream_models.go`、`account_test_service.go` 各自读取版本常量 | 换版要改 N 处，易漏 |
| 3 | 画像由代码构造 | `official_client_profile_registry.go` 的 `buildAnthropicClientProfileCatalog()` 从 `officialClientBuildDefinitions()` 等函数装配 | 画像不是数据，无法内容寻址、无法追加式换版 |
| 4 | 无版本化 Snapshot 与发布图 | `catalogdata/` 下只有 Codex 的 `0.145.0`／`0.147.0`，无 Claude 目录 | active／previous 无法并存，**无法回滚** |
| 5 | 无条件 Header 机制 | 画像结构 `officialClientWireProfile` 只有 `StaticHeaders` 与 `BetaHeader` | §2.2.2 的 21 项条件规则无处表达 |
| 6 | 出站逻辑耦合在 service | `official_egress_anthropic.go` 的 3 个 `GatewayService` 方法（`finalizeAnthropicOfficialEgressRequest`、`finalizeOfficialAnthropicHeaders`、`resolveOfficialAnthropicBetaHeader`）与 23 个包级函数全部位于 `service` 包 | 违反 §3.6 依赖方向，无 FinalizationToken 与终态校验 |
| 7 | 无 Claude Compiler／Guard 落点 | `officialegress` 的 `Persona` 仅有 `codex-cli`／`chatgpt-web`／`unclassified`／`dead-code` | Claude 请求不受 Guard 防旁路保护 |
| 8 | 版本口径已漂移 | `constants.go` 注释称「截至 2026-04」，而常量为 2.1.220 | 注释与事实脱节，无门禁发现 |

有利条件是骨架可复用：`officialegress` 的 `catalog.go` 与 `guard.go` 中不含任何 Codex 特化
（两文件 codex 命中数均为 0），`Persona` 机制、ReleaseCatalog、FinalizationToken 与 Guard 均已存在；
`official_client_profile_registry.go` 也已有 `officialClientReleasePointer{Active, Previous}` 的
概念雏形。改造是把 Claude 接入既有骨架并将画像数据化，不是重造一套。

改造按以下顺序分阶段执行，每阶段是独立变更集，不与换版 Campaign 混合（§5 待补部分同此原则）：

| 阶段 | 内容 | 完成判据 |
|---:|---|---|
| A | 新增 `PersonaClaudeCode`，登记 Claude route 与 SinkBinding，Guard 置于 `legacy_observe` | Claude 请求在 Guard 中可见且不改变 wire |
| B | 定义 §3.2 画像 schema，把 2.1.220 现有行为**原样**导出为首个画像快照 | 画像摘要可复算；final-wire 与改造前逐字节相等 |
| C | 实现 Claude Compiler 与 Executor 接入，改为从画像读取身份 | 空 wire 允许列表比较通过 |
| D | 业务代码停止读取版本常量，`constants.go` 的版本面退役 | 版本泄漏门禁覆盖 Claude 且 baseline 不新增 |
| E | 接入 active／previous 与回滚，Guard 升至 `canary_enforce` 直至 `enforced` | 可在不改代码的前提下切换与回滚画像 |

阶段 B 的**原样导出**是关键纪律：首个画像必须复制当前已验证行为，不得在数据化的同时修正规则。
任何规则修正都要走第四部分流程，与数据化改造分开验收，否则无法判断 wire 变化来自改造还是修正。

阶段 D 需注意 `constants.go` 同时服务 API Key mimic 等非对齐路径（§3.1 persona 表），退役范围
只限 `claude-code` persona 使用的版本面，不得连带删除仍有产品语义的常量。

## 3.6 包边界与依赖方向

`officialegress` 持有画像、Catalog、Compiler、Executor 与 Guard；`service` 提交业务事实与 Plan；
`repository` 提供连接池、代理与底层资源。

```text
service ───────────────────→ officialegress core
repository ────────────────→ officialegress core 的窄 port
officialegress/adapter/* ───→ core + 受信物理资源
wiring ────────────────────→ 注入闭集 adapter
```

`officialegress` **不得** import `service` 或 `repository`；公共边界只暴露中立的 Plan、Release 与
窄 port。稳定引擎不承担版本常量、模型特判或业务归属；版本画像也不承担账号、计费或协议桥接。

Claude 与 Codex 共用 Catalog、Guard、Release 与 Token 机制，但**各自拥有 Compiler**：两者的 URL
形态、Body 结构与传输能力不同（Claude 无 WebSocket 与 HTTP/2，基线只 offer `http/1.1`），
强行共用会把协议差异挤进条件分支。共用部分若需修改，必须专项复验两个 persona。

## 3.7 Guard、逐 Sink 灰度与静态门禁

Runtime Guard 先按 method、host、path、protocol 匹配 route，再验证 persona、SinkID、binding 与
状态；canary 与 enforced 还必须验证 FinalizationToken、Release／Profile digest、adapter 与最终
请求摘要。未知 route、无效 binding 或终态篡改按策略 fail-close。

状态只按 `legacy_observe → canary_enforce → enforced` 单调前进（enforced 可回滚至 canary）。
新 Sink 必须从 canary 进入；`legacy_observe` 只容纳封存基线。静态门禁覆盖 net/http、上游封装、
client factory 与 facade，并用变异测试发现包装旁路；Catalog 项只凭迁移或移除收据单调变更。

版本泄漏门禁必须扩展到 Claude：任何 `2\.1\.\d+` 形态的版本串、UA 模板与 Stainless 向量都不得
出现在业务代码与共享执行层，只能存在于画像数据与其加载器中。

## 3.8 行为策略与稳定来源

非用户直接触发的请求必须定义触发条件、频率、并发、副作用与删除期限。以下策略面须由画像或
显式策略声明，不得散落在调用点：

| 策略面 | 要求 |
|---|---|
| 重试 | 按 `RetryPolicy` 执行；`SPEC-CONN-002` 的曲线（500 ms 起步、2 倍指数、32 s 基数封顶、最高 25% 抖动）是画像参数而非代码常量 |
| 隐私模式 | 画像声明本版本证据所属模式；`essential-traffic` 下不得发送该模式门控的请求 |
| 缓存 | `cache_control` 的位置与 TTL 由 `BodyShape` 决定；客户端显式值优先的产品策略须单独声明并与画像分离 |
| 流式 | SSE 响应处理属下游兼容（§2.10 的 `RESP` 规则），不计入客户端 egress，也不得反向影响请求定型 |
| 辅助请求 | 端点、触发条件与生命周期由画像登记；不得旁路统一执行器 |

## 3.9 实施状态与迁移路径

**当前实施状态：未开始。** §3.5 的八项差距全部存在，Claude 尚无版本化画像、无 Guard 覆盖、
无 Compiler 落点。本部分描述的架构目前**没有任何一部分已在生产运行**，不得据本部分宣称
Claude 出站已画像化。

对第四部分的影响：§4.3「候选画像入库」在阶段 B 完成前没有落点；§4.6「画像晋升」在阶段 E 完成前
不可执行。二者已在 §4.0.4 登记为前置阻断。

阶段 A 至 C 完成后，第四部分可执行到 `candidate_sealed`；阶段 E 完成后方可执行完整的 §4.6。
在此之前，2.1.220 → 2.1.224 只能停留在 §4.1 取证与 §4.2.1 人工规则比较。

---

# 第四部分 Claude Code 版本演进流程

本部分规定 Claude Code 客户端画像的可复用换版流程：从目标版本官方取证开始，经规则比较批准、
候选画像与制品、候选验证封存、逐规则验收，最后完成生产启用与回滚闭环。

流程**框架**与 `CODEX_CLI_CLIENT_EMULATION_GUIDE.md` 第四部分同构，因为两者面对同一个 Sub2API
发布系统和同一类审计要求；但取证手段、规则继承判据、场景矩阵与工具现状全部是 Claude 特有的。
不得把 Codex 的证据、清单、收据或结论跨客户端复用。后继版本可以复用工具和流程，但不得复用
目标版本应独立取得的 bundle、wire 与运行证据。

| 步骤 | 核心问题与主要产出 | 状态／完成事实 |
|---|---|---|
| 1. 官方目标版本取证 | 目标版本客户端实际发出什么；形成目标规则与 bundle／wire 证据 | 新建 Campaign → `official_sealed` |
| 2. 规则比较与批准 | 哪些规则继承、改变、新增、删除或证据降级；批准清单 | `official_sealed → profile_approved` |
| 3. 候选画像与制品 | Sub2API 如何表达目标规则；生成候选画像与制品 | 保持 `profile_approved` |
| 4. 候选验证与封存 | candidate 是否从官方与第三方入口产生目标 wire | `profile_approved → candidate_sealed` |
| 5. 比较与验收 | 官方与候选是否逐规则一致 | `candidate_sealed → compared → ready` |
| 6. 生产启用与回滚 | 生产是否运行选定 candidate 且能完整退回 | Campaign 保持 `ready`；每个 candidate 独立封存激活事实 |

**本部分当前不可整体执行。** Claude 侧受管工具链只覆盖取证与门禁，尚不存在编排器、Campaign
状态机、批准清单机制、候选封存与晋升收据。§4.0.4 逐项登记这些缺口，它们是创建正式 Campaign 的
前置阻断。先把规范固定下来，是为了让补齐工具时有明确契约，也让「尚未做到」与「已经做到」在
文档中始终可区分。

## 4.0 全流程控制约定

### 4.0.1 DOC-PRE 与 P0 预检

正式 Campaign 前先完成可丢弃的 DOC-PRE／P0。DOC-PRE 只登记并审核本次换版的文档与台账变更；
合并后必须从干净 HEAD 执行 P0。P0 只发现阻断，不形成目标版本证据。

| 类别 | P0 通过条件 | 当前可执行性 |
|---|---|---|
| 身份与角色 | 冻结目标 npm 包、tgz 与二进制 SHA-256、平台、内嵌 Bun 构建串、entrypoint、隐私模式与网络条件 | 部分：`extract_claude_bundle.py` 可复算包与 bundle 摘要 |
| 采集能力 | P（pcap）、R（等长脱敏字节）、J（MITM）、M（manifest）四通道在目标镜像中可用，且 addon 摘要可入 manifest | 部分：relay 与 MITM addon 已有；addon 摘要进 manifest 仍是已知 M 缺口 |
| 账号与场景 | firstParty OAuth 账号、模型额度、受控故障注入与子 agent 深度链可达；TUI 类场景需真实 TTY | 部分：`--fault-spec` 与 subagent 链可用；TTY 仍阻断 |
| 环境恢复 | 端口、容器、hosts、代理、CA、`.claude.json` 与账号登录态可按 before／after 恢复 | 未受管：无环境探针收据 Schema。注意 logout 会把 `hasCompletedOnboarding` 重置为 false |
| 基线门禁 | 干净 HEAD 执行覆盖门禁并记录命令、源码摘要、退出码与跳过分类 | 可执行 |
| 生产隔离 | 只读记录生产镜像、compose、selector 与依赖服务；P0 与正式 Campaign 隔离 | 未受管：Claude 画像尚未进入生产 Catalog |

P0 产物标为 `preflight-only`，不得发送计入正式证据的真实请求，不得修改运行环境或历史证据。
无法证明通过的项目登记为阻断；阻断修复应独立提交后重跑 P0。

### 4.0.2 Campaign、candidate 与 attempt

Campaign 是一次目标版本升级的不可变证据容器；candidate 是其中一个固定的 Sub2API 源码与镜像
实现；attempt 是身份不变时的一次只写采集记录。

| 单元 | 必须新建的变化 |
|---|---|
| 版本 Campaign | 目标版本、npm 包或二进制摘要、内嵌 Bun 构建、平台、默认隐私模式，或批准规则、场景、画像与断言变化 |
| 同版本后继 Campaign | 受管工具影响证据含义、环境无法证明恢复，或已冻结的机器角色与执行副本身份错误 |
| 同 Campaign 新 candidate | Sub2API 源码树、测试树、构建 ID、部署版本、OCI digest、image ID 或画像 ID／摘要变化 |
| 同 candidate 新 attempt | 冻结身份不变，仅因网络、配额或临时运行失败重试；新 attempt 不覆盖旧记录 |

以下 Claude 特有的身份维度必须一并冻结，缺任一项不得开始采集：

- **隐私模式**：`essential-traffic`／`no-telemetry`／`default` 直接决定端点集合大小（§1.1 的三级
  模型中 `ca()` 门控 53 处、`V0e()` 门控 7 处）。同一 Campaign 内不得混用模式，跨模式结论必须
  分别封存。
- **entrypoint**：`sdk-cli`（`-p` 驱动）与 `cli`（真实 TTY 驱动 TUI）是两个身份；`-p` 会强制
  `sdk-cli`，不能用环境变量伪造入口。
- **平台角色**：Linux x86_64 是静态与 wire 主基准，Darwin arm64 只作交叉复核；minify 符号不得
  跨平台引用。

产出侧工具（采集、relay、脱敏、bundle 提取、收据生成、环境快照、编排）变化会改变证据字节，
必须新建 Campaign；评估侧工具只有在显式白名单内才允许漂移。被校验的工具树必须就是实际执行的
工具树。

每个 candidate 建立时声明用途且不可事后追认：`validation_only` 止于 `accepted_not_activated`；
`production_replacement` 在验收后必须继续执行 §4.6，并形成该 candidate 独立的激活收据。

### 4.0.3 状态、证据与全局不变量

Campaign 状态只按以下顺序前进：

~~~text
planned → official_sealed → profile_approved → candidate_sealed → compared → ready
~~~

candidate 的生产状态与 Campaign 状态相互独立，按 candidate 单调记录：

~~~text
accepted_not_activated → canary_passed → active → rollback_verified → restored_active
~~~

不得以 candidate 编号最大、验收通过或 Campaign 已 `ready` 推断生产状态。当前生产 candidate 由
最新有效激活收据、运行容器 digest 与 activation fact 共同确定；三者不一致时状态为
`production_unverified`，禁止宣称升级完成。

| 不变量 | 要求 |
|---|---|
| 权威来源 | 批准清单是规则与画像的唯一事实源；文档正文不得与机器台账冲突 |
| 不可变性 | 清单、attempt、result、seal、画像与历史收据只追加、不可覆盖；身份变化不得借旧证据跨阶段 |
| 同源性 | 被测源码、候选源码、构建产物、运行镜像、画像与 finalizer 由摘要形成同一条可复算链 |
| 失败关闭 | 路径、权限、摘要、恢复、安全、身份或规则覆盖无法证明时停止，不以人工推断补足 |
| 证据保留 | 遵守 §2.1.1 与 §2.1.2；R 类只允许等长脱敏，未脱敏材料不得离开采集机，原始 MITM JSONL 属 `raw_private` |
| 状态诚实 | 换版不是升格证据等级的时机；「已观察」不得因换版被静默改写为「已验证」（判据见 §4.2.1） |

### 4.0.4 工具就绪状态与前置阻断

本节区分「当前已强制执行」与「规范要求但尚未受管实现」，是 Claude 与 Codex 差距最大的一节。
Codex 侧有约九千行的 `codex_upgrade.py` 编排器、成套 Schema、晋升命令与激活收据生成器；
Claude 侧目前只有取证与门禁工具。**任何「未受管实现」项都是创建正式 Campaign 的前置阻断。**

| 能力 | Claude 当前状态 | 边界与缺口 |
|---|---|---|
| bundle 提取与可达性 | 已实现 | `extract_claude_bundle.py` 确定性解析 Bun SEA；`claude_bundle_reachability.py` 建立 sink 可达窗口，窗口按 §2.1.4 解读，不构成数据流证明 |
| 规则与覆盖门禁 | 已实现 | `claude_21220/check_coverage.py` 对账编号、责任集合、HitCC 与 2.1.88 覆盖及运行 manifest；要求 `local-analysis/` 证据树完整存在 |
| 运行证据分析 | 已实现 | `analyze_claude_21220_pending_evidence.py` 复算 22-run campaign 绑定、正负例与重试曲线 |
| R 通道采集 | 已实现 | `run_claude_relay_scenario.sh` 与 `upstream_byte_relay.py` 只在两条 TLS 腿之间复制明文字节 |
| 条件 Header 探针 | 已实现 | `capturelib/environment.py` 为条件候选取正负例 |
| TUI／`cli` 入口驱动 | 未攻克 | `drive_claude_tui.py` 已存在，但 `pty` 在无 TTY 的 `docker exec` 下父进程卡死；`CAND-UA-CLI`／`CAND-BG-SESSION` 至今无正例 |
| 升级编排器与 Campaign 状态机 | **未受管实现** | 无 `claude_upgrade.py`；`plan`／`status`／`resume`／`compare`／`accept` 均不存在。现有 `tests/test_claude_21220_campaign_gate.py` 只校验既有取证 campaign，不是换版状态机 |
| 批准清单机制 | **未受管实现** | 无 `classification/approved/` 等价物，无联合摘要两段式批准 |
| 画像暂存与入库 | **未受管实现** | 无 `prepare-profile`／`stage-profile`；且 Claude 版本化 Snapshot／ReleaseCatalog 尚不存在（架构见 §3.2，需先完成 §3.5 阶段 B） |
| 候选采集与四阶段封存 | **未受管实现** | 无 candidate 身份冻结、seal 预览与批准流程 |
| 逐规则机器断言 | **未受管实现** | 无 Claude 版 expectations 与断言执行器，`PAIR-<SPEC-ID>` 无法机器执行 |
| 画像晋升与激活收据 | **未受管实现** | 无 Claude 晋升命令；`docs/egress/maintenance/` 下 Claude 收据数为 **0** |
| 第三方入口绑定 | **未定义** | Codex 固定 Kilo 双入口；Claude 的第三方标准 API 入口集合尚未在工具与 Schema 中确定（见 §4.4.2） |

因此，对 2.1.220 → 2.1.224 的当前可执行范围是：**可以执行 §4.1 的取证与 §4.2.1 的人工规则比较**
（bundle 提取、可达性枚举与覆盖门禁均已具备），**不能**进入 §4.2.2 之后的批准、画像、候选封存、
验收与晋升。人工「已经跑过」的结论、终端记录或未绑定原始事实的静态 JSON 不能替代受管收据。

补齐顺序应与依赖一致：编排器与 Campaign 状态机 → 批准清单 → 版本化画像（依赖第三部分）→
候选封存与逐规则断言 → 晋升与激活收据。Campaign 建立后再修改这些工具会触发 §4.0.2 的工具漂移
边界，因此必须先补齐再开 Campaign。

## 4.1 官方目标版本取证

本步从目标版本官方发行物和真实抓包整理第二部分完整编号规则；开始前必须通过 §4.0 的 DOC-PRE／P0。

### 4.1.1 输入与执行

Claude 与 Codex 在这一步的根本差异是**没有官方源码**：Codex 直读 Rust stable 源码并做 diff，
Claude 只能从 minify 生产 bundle 逆向建立命题。因此输入以发行物为准：

| 输入 | 内容 |
|---|---|
| 基线 | 当前第二部分编号全集、三份机器台账与 `local-analysis/captures/` 既有运行 |
| 目标 | npm `stable` 版本号、tgz 与 `dist.integrity`、Linux x64 与 Darwin arm64 二进制 SHA-256、内嵌 Bun 构建串、上报的 SDK 版本 |
| 条件 | 平台、运行镜像、隐私模式、entrypoint、模型、账号、代理与 TLS 条件 |
| 坐标 | 持久 Campaign 目录、采集机、证据目录与环境恢复坐标 |

目标版本必须取自 `stable` dist-tag，并同时记录取包时刻的 `latest`，以便日后解释版本选择。
Campaign 目录必须是持久、绝对、尚不存在且不经符号链接的路径，不得位于临时目录。

执行顺序如下；在编排器补齐前，各步骤只能人工按序执行并自行留痕，其产物不构成受管收据：

| 顺序 | 动作 | 应产出 |
|---:|---|---|
| 1 | 冻结目标发行物：下载 tgz，记录 registry URL、`dist.integrity`、下载时间，复算 tgz 与二进制摘要 | `SOURCE_MANIFEST.json` 等价物 |
| 2 | 提取 bundle：确定性解析 Bun SEA | 提取物与提取器摘要 |
| 3 | 枚举 sink：建立入口到网络 sink 的可达窗口 | 端点／Header／Body／retry 候选清单 |
| 4 | 按场景采集：P／R／J／M 四通道覆盖批准场景矩阵 | 逐运行 manifest 与脱敏证据 |
| 5 | 封存：校验恢复、权限、秘密扫描、inventory 与 finalizer | 进入 `official_sealed` |
| 6 | 生成差异草案：目标与基线的规则级差异 | 分类草案（当前无受管生成器） |

SHA-256 只能证明文件同一，不能单独证明来源官方；来源清单必须绑定 registry 事实。

### 4.1.2 规则整理

人工逐项复核 bundle 控制流、wire 差异与原始抓包：

1. 从入口追到认证、Client、TLS、传输、Header、Body、端点与跨请求状态；
2. 判断范围、触发条件、固定／随机／条件属性及可观测边界；
3. 把成立的目标行为写入第二部分，保持 §2.17 的六字段完整；
4. 使机器台账与第二部分编号一一对应，所有证据可重新定位和解析。

**minify 符号处置**是 Claude 特有的关键约束。目标版本的标识符与基线不可比，`mRl`／`ca`／`V0e`
一类名称可能整体改名，因此：

- 锚点必须在目标 bundle 中**重新建立**，不得从 2.1.220 的锚点表继承；
- 锚点文本变化本身既不证明规则变化，也不构成继承旧规则的理由（§2.1.2）；
- 判据只能是**归一化后的语义结构**——控制流形状、常量、条件与 sink 的关系——并以 Linux 为主、
  Darwin 交叉复核；两平台 α-归一化摘要一致只说明同一段逻辑存在，不说明它在默认 OAuth 路径可达。

工具只负责提取、枚举、解析与差分，不自动编写规则正文，也不批准清单。

### 4.1.3 退出条件

目标规则、逐规则 bundle 与 P／R／J／M 证据、差异草案齐备；所有发现均已进入规则或标为 `blocked`；
场景、恢复、安全、inventory 与封存全部通过，Campaign 达到 `official_sealed`。

## 4.2 规则比较、画像准备与批准

### 4.2.1 规则分类

| 分类 | 含义 |
|---|---|
| `inherit` | 编号与官方可见行为均不变，且目标版本已重新取得同等级证据 |
| `change` | 规则仍存在，但可见行为变化 |
| `add` | 目标版本新增规则或出站面 |
| `delete` | 基线规则在目标版本已不可达 |
| `condition_change` | 行为仍存在，但触发条件变化 |
| `blocked` | 证据不足，暂时不能得出结论 |
| `regressed_evidence` | **Claude 特有**：基线为「已验证」，目标版本只重新取得「已观察」级证据 |

**「已观察」规则的继承判据是本步最关键的约束。** 基线 53 项对齐责任中有 41 项为「已观察」，
它们在基线中就未达到 §2.1.2 准入，因此：

- 「已观察」规则**不得**因为「上一版就是这样」而直接标 `inherit`。目标版本必须重新取得至少同
  等级的运行观察，否则只能是 `blocked`；
- 「已验证」规则要维持 `inherit`，必须在目标版本重新满足 §2.1.2 的全部七项，包括正负例、单变量
  对照与独立重复。只复用基线证据的记为 `regressed_evidence`：对齐责任保留，证据等级下调；
- 任何分类都不得使某编号在文档与机器台账之间出现状态不一致。

`delete` 必须同时具备目标 bundle 不可达结论、覆盖触发条件的正反场景、旧规则引用清单与移除
收据，否则保持 `blocked`。已知结构性不可达的候选——TTY 类 `CAND-UA-CLI`／`CAND-BG-SESSION`、
远程 gate 类 `CAND-HDR-USAGE-LIMIT`、进程内 `CAND-WORKLOAD`——在目标版本仍标 `blocked`：
它们不阻断换版，但不得计入覆盖，也不得因长期无正例而被降格为「不存在」。

### 4.2.2 画像与清单

在 `official_sealed` 状态下把目标规则规范化为 Campaign 外的待审核画像，并形成以下清单。
Codex 使用五份清单；Claude 因为存在成对验收责任，需在断言清单中额外承载 `PAIR-<SPEC-ID>`：

| 清单 | 审核内容 |
|---|---|
| 目标规则全集 | 与第二部分逐条一致，含状态等级与适用画像 |
| 规则迁移 | 新旧编号迁移、§4.2.1 分类与证据引用；含 `regressed_evidence` 明细 |
| 场景 | 官方与候选场景、规则覆盖、隐私模式与 entrypoint 绑定 |
| 画像 | 完整目标 Snapshot 与画像 ID／摘要 |
| 断言 | 逐规则断言、场景选择、第二部分摘要绑定，以及每个 `target_1_1=true` 编号的 `PAIR` 双入口断言 |

规则、场景、画像、断言与端点集合必须跨清单一致；**端点类清单必须声明隐私模式**，
`essential-traffic` 下得出的端点集合不得表述为该版本的端点全集。

### 4.2.3 批准与退出条件

批准采用两段式：先由工具校验清单并返回联合摘要；人工复核后，以完全相同的清单加该联合摘要
再次提交，只写一次地保存为批准事实。摘要不得手写或替换。

退出条件：规则与发现无未分类项，`blocked` 已逐项记录理由与补抓计划，联合摘要获批准，
Campaign 进入 `profile_approved`。

**当前阻断**：批准清单机制在 Claude 侧未受管实现（§4.0.4），本节不可执行。

## 4.3 候选画像入库与制品构建

### 4.3.1 画像暂存

把批准画像编译为**不切换 Active** 的候选画像，输出到 Campaign 之外、尚不存在的绝对路径，
不修改仓库或生产 selector。暂存收据必须证明画像摘要与批准事实一致、Active 未变、候选以
`previous` 模式登记，且 inventory 精确覆盖输出目录并逐文件可复算。

### 4.3.2 入库与实现边界

将暂存画像纳入 candidate 源码树，并满足：

1. 目标画像是**新增节点**，旧节点的路径、内容与摘要不变；
2. 批准的端点、Header、Body、TLS、连接与状态规则均可由画像表达，不靠代码分支特判；
3. 新端点具备 binding、resolver、route 登记与发布证明；
4. 生产 Active 不变，目标画像仅作为候选供第四步显式选择；
5. 在途请求保持原画像，新请求才解析新 selector，连接池与 fallback 不得跨画像混用。

只有现有画像结构无法表达新机制时，才最小修改共享执行层，并专项复验两个版本。

**当前阻断**：Claude 版本化 Snapshot／ReleaseCatalog 尚不存在，本节没有落点。画像结构见 §3.2，
须先完成 §3.5 的阶段 B（把现有行为原样导出为首个画像快照）。

### 4.3.3 构建与退出条件

从完成入库与测试的同一最终源码树构建目标平台制品，记录 Git／tree／build／部署版本、二进制
SHA-256、架构、构建参数、image ID 与 OCI digest。退出条件：暂存收据可复算，画像与制品同源，
旧画像仍可执行，实现侧测试通过，生产 Active 未改变。

## 4.4 候选验证与封存

### 4.4.1 Candidate 身份冻结

真实请求前复算源码、运行镜像与画像，原子创建 attempt，冻结源码、构建、部署、镜像与画像身份，
并生成 attempt 标识与运行 nonce。attempt、activation fact、镜像构建证明与实测源码摘要必须指向
同一源码树。候选必须**显式选择**目标画像，不得借当前 Active 或客户端自报版本选择画像。

### 4.4.2 场景与第三方入口

场景清单是任务、规则覆盖与必需客户端的事实源，每条规则必须有真实触发场景。Claude 的场景矩阵
与 Codex 完全不同：**没有 WebSocket 业务帧、Lite 模式、realtime、WHAM 与独立 images 端点**，
基线只 offer `http/1.1`。必须覆盖的场景族至少包括：

| 场景族 | 覆盖目标 | 当前状态 |
|---|---|---|
| 基线推理 | 主画像的 Header／Body／端点／TLS | 已有 22 个完整运行 |
| 条件环境 Header | client-app、container、remote-session 的单变量正负例与组合顺序 | 已闭环（`HDR-016..018`、`021..026`） |
| 子 agent 深度链 | parent-agent 的深度 0／1／2／3 与直接父级关系 | 已闭环（`HDR-019`、`025`） |
| 受控重试 | `500` 且无 `Retry-After` 的退避曲线与第十次请求 | 已闭环（`CONN-002`、`003`）；其余状态码未采 |
| 多轮／工具／compact | Body 角色序列、tools schema、压缩入口 | **未采**，属 §2.12.2 最小补抓顺序 |
| TUI／`cli-bg` | `cli` 入口的 UA 与 `x-app` | **阻断**：需真实 TTY |
| 端点发现 | 无 host 预过滤的端点集合 | 仅 `essential-traffic` 模式；须同时采 `default` 模式 |

第三方入口方面，Codex 固定绑定 Kilo 双入口；**Claude 尚未确定第三方标准 API 客户端集合**。
在工具与 Schema 中确定该集合前，`PAIR-<SPEC-ID>` 成对验收无法机器执行。成对验收的判据是：
对相同规范化语义与画像条件，官方入口与第三方入口必须命中相同的可见结果；动态字段按规则声明的
格式、相等关系与生命周期比较，不比较某次抓包中的字面值。

场景真实性必须区分「任务退出成功」与「目标分支真实成立」。重试曲线、子 agent 深度、条件 Header
组合等场景，只有当原始 J／R 或生命周期记录能证明触发、关键中间事实与最终状态时才生成成功收据，
编排器不得根据退出码补写。收据还必须绑定隐私模式、entrypoint、模型、证据根、Campaign、attempt
与运行 nonce。

### 4.4.3 四阶段封存

1. **建立检查点**：客户端验证请求完成后首次执行封存，采集 after 探针并返回检查点。
2. **生成收据**：在 candidate 源码树之外运行受管生成器，形成采集 manifest、测试 trace、观察画像
   收据与第三方入口收据。构建期中间产物只是 finalizer 的输入，不能直接提交封存；activation fact
   必须由运行服务产生，测试 trace 必须来自同源树的冻结日志，生成器不得合成二者。
3. **生成预览**：工具重验身份、任务、时间窗、恢复、安全、inventory 与机器断言，返回预览摘要。
4. **批准封存**：人工复核预览后，以相同参数追加该摘要提交，Campaign 进入 `candidate_sealed`。

证据标签只能从采集参数与场景前置条件推出，不得根据待通过的断言结果反推；侧别豁免只允许结构上
没有产出路径的检查，采集遗漏必须重采。

### 4.4.4 运行纪律与退出条件

- 开跑前机器预检必须覆盖不可变镜像 RepoDigest、挂载与 PID namespace、实际执行的工具副本、冻结的
  目标客户端、模型与账号能力、隐私模式注入、管理凭据、账号熔断与配额、采集端口及 run-root 的
  属主与权限；任一缺失在真实请求前失败关闭。
- 固定镜像 digest，只替换应用容器并保留回滚点；运行期间不执行 `pull`、`compose down` 或 `prune`，
  不重建数据与依赖服务。
- run 与 seal 之间不得修改 candidate 源码树；身份变化必须新建 candidate，只有身份未变的临时失败
  才允许新 attempt。
- 证据目录权限至多 `0700`、文件至多 `0600`；R 类必须等长脱敏后才可离开采集机。
- 确认必败时执行受管停止与环境恢复，不得强杀并丢失 after 探针。

退出条件：必需场景与已确定的第三方入口全部通过，运行画像、测试、恢复、秘密扫描、inventory、
机器断言与封存完整，预览摘要已批准，Campaign 达到 `candidate_sealed`。

## 4.5 比较与验收

### 4.5.1 离线比较

比较机必须能从封存的绝对路径复算两侧证据；跨机器时先把完整证据根同步到原绝对路径。工具只读
重验身份、inventory、恢复、任务、规则覆盖与画像绑定，生成比较结果，Campaign 进入 `compared`。

两侧 surface 集合「相等」不是验收结论——采集计划不同即可导致不等；行为一致性由逐规则断言决定。

### 4.5.2 逐规则机器断言

断言配置必须绑定批准清单、目标版本与画像摘要，以及官方、候选与比较三方的证据根、采集 manifest
与逻辑路径前缀。判定沿用两类模式：在两侧封存证据上执行同一规则的侧别检查；或在候选证据上验证
Sub2API 内部实现并绑定批准的官方权威摘要。

结果必须唯一覆盖目标规则全集，每条均为通过且证据完整，不允许失败、不适用、手写通过或未绑定
inventory 的证据路径。**Claude 补充要求**：`regressed_evidence` 规则必须在结果中显式标注，不得
与 `inherit` 混同；`PAIR-<SPEC-ID>` 必须官方与第三方两侧入口都实际执行。

### 4.5.3 accept 前置与正式验收

在同一 candidate 源码树完成并保留覆盖门禁与完整回归结果：

~~~bash
python3 tools/official_client_capture/claude_21220/check_coverage.py
~~~

该门禁要求 `local-analysis/` 证据树完整存在，不是脱离证据归档即可运行的普通 CI 单测。验收命令
本身不运行这些门禁，也不接收外部门禁收据（自动化边界见 §4.0.4）；命令失败、非预期跳过或源码
摘要变化时不得继续。

验收重算逐规则断言并检查四组门禁：套件与身份、比较与规则覆盖、恢复与安全、第三方入口收据。
全部通过后只写一次地保存断言与验收事实，Campaign 进入 `ready`；失败 attempt 不可覆盖，
Campaign 保持 `compared`。

### 4.5.4 ready 边界

`ready` 只表示固定 candidate 通过了目标规则与 Campaign 证据验收，**不表示**已晋升画像、构建正式
镜像、切换生产或完成回滚演练。`validation_only` candidate 到此结束并标记 `accepted_not_activated`；
`production_replacement` 必须继续执行 §4.6，且候选验证镜像以候选模式运行，不能直接作为默认
Active 的生产镜像。

同样重要的是：`ready` 不改变任何规则的证据等级。若本轮存在 `regressed_evidence` 或 `blocked`，
验收通过只说明「候选实现与已批准的目标规则一致」，不说明目标版本画像已被完整证明。

## 4.6 生产启用与回滚

Campaign `ready` 后，以选定 candidate 的晋升、终态门禁、正式镜像与激活证据，证明目标画像成为
Active、上一已接受画像可完整回滚、并在演练后恢复目标版本。本节所有输入输出必须绑定同一个
candidate 与验收摘要。候选验证镜像与晋升后的生产镜像是两组不同身份，必须由晋升收据、差异清单
与终态门禁连接，禁止把候选镜像摘要冒充生产镜像摘要。

### 4.6.1 生产对账与回滚点

写操作前只读记录容器 digest、compose、selector、activation fact、Active／Previous、数据与依赖
服务、网络、挂载及代理／CA。名义配置与实际流量不一致时立即停止。

冻结上一已接受版本的画像、镜像 digest 与必要配置，并在只读数据克隆或等价隔离环境中证明旧镜像
可启动、可读数据并通过健康与鉴权检查。依赖可变标签、临时环境变量或未经验证的 Previous 不能作为
回滚点。

### 4.6.2 画像晋升与正式制品

在已验收的 candidate 副本上，离线交换已验收画像的 Active／Previous 角色，生成生产画像目录与
晋升收据，绑定验收摘要、目标与回滚版本、两个画像摘要、selector 变化与完整 inventory。输出路径
必须绝对、尚不存在且不经符号链接。

生产树只允许三类变化：晋升 inventory 声明的画像与契约；candidate 冻结基线中已存在且摘要一致的
通用晋升实现；为角色互换而作的确定性测试期望调整。不得修改业务运行时代码、依赖、门禁脚本或
验收输入。必须生成 candidate → 生产的逐文件差异清单，清单外变化或运行时代码变化必须退回
candidate／Campaign 重新验收，不能夹带进晋升。

最终生产树必须**重新执行**门禁，不得复用 candidate 结果，并完成完整回归与目标架构测试。正式
镜像需绑定两侧 tree 摘要、验收、晋升收据、inventory、门禁结果、构建输入、image ID 与 registry
manifest digest，且不得携带取证专用构建标签。任一摘要不一致时禁止构建或部署。

### 4.6.3 独立 Active canary

使用正式镜像 digest 建立与生产隔离的 canary，独立使用账号、配置、数据库、网络与证据目录。
canary 必须按晋升后的**默认 Active** 运行，禁止以强制模式命中目标画像，也禁止复用 activation
fact 显示候选模式的验证镜像。核对镜像架构、启动、健康、HTTP／TLS、错误率、Guard 与 activation
fact，强制模式计数必须为 0，真实业务须出现验收规定的完成事件。失败不得进入生产。

### 4.6.4 正式切换

部署前复核编排配置：应用服务必须绑定精确的 `repository@sha256:<manifest-digest>`，数据库、
缓存、挂载与网络保持不变。仅替换应用容器；远端尚未缓存固定 digest 时才执行定向拉取，
禁止 `compose down` 与无范围 `prune`。

部署后复核容器 digest、编排配置、健康、日志、依赖、挂载与 activation fact，确认 Active 版本与
画像摘要同晋升收据一致且没有强制 override。发现身份、安全、数据、恢复、旧画像兜底或连接池混用
时立即完整回滚，不在故障实例上补画像或改 selector。

### 4.6.5 回滚演练与目标恢复

正式切换后仅替换应用容器，切回 §4.6.1 冻结的旧镜像与配置，复核健康、鉴权、数据、依赖、挂载、
代理／CA、入口与最终 wire；不得重建数据容器。随后恢复目标镜像，重复检查镜像、Active 画像、
activation fact、业务事件、完整性计数与 Guard。

只有旧画像、旧镜像与配置已完整绑定时，「切回 Previous」才是完整回滚的简写；只改模式不能替代
演练。回滚不得删除 Campaign、覆盖画像或销毁证据。

### 4.6.6 激活证据与退出条件

激活证据必须绑定 Campaign 与验收、晋升与 inventory、生产树、终态门禁、正式镜像，以及 canary、
正式切换、旧版回滚与目标恢复四阶段的时间、配置、各类摘要、activation fact、完整性计数、业务
事件与日志结论。四阶段事实必须写入权限受限的独立证据根，再由受管工具生成并可重放不可覆盖收据。

退出条件：运行容器与生产镜像一致；candidate 与验收摘要经晋升收据、差异清单与终态门禁连接到
生产树；Active／Previous、画像与 activation fact 三者一致；四阶段全部通过，晋升、终态门禁、
构建与激活证据形成同一条可复算链，且激活收据可从原始事实重放。Campaign 保持 `ready`，
candidate 达到 `restored_active`；至此才能声明该 candidate 的生产升级完成。后继 candidate 若
仅达到 `accepted_not_activated`，不得沿用本结论。

**当前阻断**：Claude 侧无晋升命令与激活收据生成器，且 Claude 画像尚未进入生产 Catalog
（须先完成 §3.5 的阶段 E），本节整体不可执行。

### 4.6.7 权威源码、正式发版与远端清理

生产激活完成后，必须把最终 production tree 同步回本地权威仓库，再提交并正式发版。同步以逐文件
manifest 与摘要为准，不得凭记忆挑选文件，也不得用 candidate tree、临时构建目录或运行镜像反向
覆盖本地后续已批准的变更。至少校验：

1. 画像目录（§3.2 的 `catalogdata/claude/profiles/<version>/<digest>.json`）与发布图的 Campaign、
   acceptance 同晋升收据完全一致，且 Active／Previous 与各画像摘要均可从仓库复算；
2. 本地最终树与已激活 production tree 的每项差异都有明确分类；属于后继维护变更的差异按第五部分
   独立验收，未分类差异禁止进入提交；
3. Git commit／tag、源码树摘要、构建参数与目标架构发布镜像 digest 相互绑定；正式构建不得携带
   取证专用标签，也不得复用候选镜像；
4. 用正式发布镜像重新执行独立 canary、生产切换、固定回滚与目标恢复，并生成新的生产激活收据。
   **发版成功不等于生产已经更新。**

GitHub 只保存可公开源码与发布制品，**不能**替代原始抓包、Campaign、acceptance 与四阶段激活事实。
Claude 的证据在这一点上比 Codex 更受限，以下材料**一律不得进入任何公开仓库**：

| 材料 | 约束来源 | 处置 |
|---|---|---|
| 原始 MITM 记录（J） | §2.1.1：属 `raw_private` | 只进受控私有归档 |
| 未脱敏的应用层字节（R） | §4.0.3：只允许等长脱敏，未脱敏材料不得离开采集机 | 留在采集机，或脱敏后归档 |
| 原始 pcap（P） | 含 SNI、连接时序与可能的凭据痕迹 | 只进私有归档 |
| 官方发行物与提取物（A1 bundle） | Anthropic 发行物，非本仓可再分发内容 | 只保留来源清单与摘要，不重新分发 |
| 账号、token 与 OAuth 凭据 | 全流程 | 任何形式均不得归档进公开位置 |

私有归档必须具有逐文件路径、大小与 SHA-256 清单，并在**另一存储位置**完成解包、摘要复算，以及
acceptance、promotion 与 activation receipt 的重放。归档还须标注该批证据的**隐私模式**与
entrypoint（§4.0.2 的冻结身份维度）——脱离模式的端点类证据无法被后续正确解读。只有最终仓库、
正式发布镜像与私有证据归档三者均可独立恢复，才允许清理采集服务器。

清理前生成机器可读的保留／删除清单，并完成以下检查：

1. 生产主机正在运行正式发布镜像的固定 digest；正式 compose／override 已迁出升级临时目录，
   固定回滚镜像与配置仍可用；
2. 采集容器与候选 Sub2API 容器不再被生产、归档或收据重放使用，停止后生产健康、网络与依赖状态不变；
3. 删除目标只包含已归档的 Campaign、candidate、run、临时源码、构建缓存与候选镜像；**不得**包含
   生产数据库、Redis、keeper、正式配置、当前镜像、回滚镜像或任何唯一证据副本；
4. 删除清单先以只读／dry-run 方式解析真实路径、大小与摘要，经人工批准后再按服务器分别执行。

删除前还须确认该批运行的终态秘密扫描回执为 `passed`（§2.3 要求逐运行绑定扫描器摘要与文件清单）；
扫描未通过或回执缺失的运行不得删除，它是判断证据能否离开采集机的唯一依据。

远端清理的最终授权条件是：本地权威提交与 production tree 的差异闭合、正式发布镜像已完成生产复验、
私有证据归档可恢复并可重放、删除清单不含生产依赖或唯一副本。任一条件不满足时，只能停止空闲采集
容器，不得删除升级文件或证据。

**当前阻断**：本节依赖 §4.6.1–§4.6.6 的产物，而 Claude 尚未具备晋升与激活能力；在此之前，
采集机上的 Campaign、run 与提取物**一律不得删除**——它们目前是 2.1.220 全部证据的唯一副本。

# 第五部分 非版本变更维护

本部分处理不属于 Claude Code 换版的维护变更。上游更新与兼容代码退休必须使用独立变更集，
不得借版本 Campaign 掩盖自身的行为变化；反过来，换版 Campaign 也不得夹带上游合并或代码清理。

| 变更类型 | 处理路径 | 最低证明 |
|---|---|---|
| 上游更新，且规则、画像、wire 与受管工具身份均不变 | §5.1 独立维护变更集 | overlay 台账可复算，Active／Previous final-wire 无差异，完整门禁通过 |
| Sub2API 实现变化，但批准规则与工具身份不变 | 按 §4.0.2 在原 Campaign 建立新 candidate；若替换生产则回到 §4.6 | 新 candidate 独立封存、比较与验收；生产替换还须形成新激活收据 |
| 规则、场景、画像、断言或产出侧工具身份变化 | 按 §4.0.2 建立同版本后继 Campaign | 重新批准并重放受影响的证据与门禁 |
| 兼容代码退休 | §5.2 独立退休变更集 | 消费者闭集、失败关闭、空 wire 允许列表与机器退休收据 |

判断顺序固定为：先问「最终 wire 是否可能改变」，再问「批准规则或工具身份是否改变」。两者都否才是
本部分的普通维护变更；只要其一为是，就必须回到第四部分的单元边界。

## 5.1 Sub2API 上游更新

Sub2API 上游更新与 Claude Code 换版必须拆成两个变更集，并按以下顺序执行：

1. 冻结 upstream commit、当前发送面与冲突面，以及 Active／Previous、release 与画像摘要；
2. 合并后重新生成机器 overlay 与 source-to-sink 台账，复核新增／删除差异、高风险合并缝，以及
   route、persona、SinkID 与后端归属；新增官方出站不得先放行裸 client；
3. 按上表与 §4.0.2 判断走普通维护变更、新 candidate 还是同版本后继 Campaign；纯业务变化不得
   改写画像，删除路径转入 §5.2；
4. 运行全量测试、静态门禁、版本泄漏检查，并以**空允许列表**比较 Active／Previous 的 final-wire；
   受影响入口必须复验，范围无法可靠收窄时执行完整候选验收，并从最终同源树构建目标平台制品；
5. 若制品只用于验证，明确记录 `validation_only`／`accepted_not_activated`；若准备替换生产，
   声明 `production_replacement` 并完整执行 §4.6，禁止复用旧 candidate 的激活收据。

不得手改机器计数，也不得把入站版本或账号 UA 接入 strict wire。

**当前阻断**：Claude 出站面**完全不在** overlay 台账的保护范围内——
`docs/egress/maintenance/upstream-v0.1.171-egress-merge-ledger.json` 登记的 86 个文件中，
Claude／Anthropic 相关条目为 **0**。这意味着上游合并可以静默改动
`official_egress_anthropic.go`、`official_client_profile_registry.go` 与 `pkg/claude/constants.go`
而不产生任何台账差异，第 2、4 步的机器复核对 Claude 目前是空转。

在 §3.5 的阶段 A（Claude route 纳入 Guard）与阶段 D（版本面收敛进画像）完成前，上游更新对 Claude
的保护只能靠人工复核：每次合并必须显式列出 §3.5 表中八项差距所涉文件的实际改动，并声明最终 wire
是否变化。补齐 overlay 覆盖应作为阶段 A 的一部分，而不是等到换版时才补。

## 5.2 兼容代码退休

每次清理按以下顺序执行：

1. 用类型扫描、调用图与 Catalog 证明全部生产消费者，并识别必须保留的产品语义；
2. 将真实消费者迁移到当前接口，使旧入口明确 fail-close；
3. 验证 Active／Previous、fallback、辅助端点与负例，并以空 wire 允许列表比较前后；
4. 删除旧类型、字段、构造接线与仅验证旧实现的测试；删除 Catalog 项前必须生成 RemovalReceipt，
   同时收紧源码绝迹门禁，禁止通过别名或 wrapper 恢复；
5. 写入机器退休收据，完成完整门禁后再进行实机部署。

不得删除仍承担平滑升级、回滚、非 `claude-code` persona 或 API Key 产品语义的兼容层。旧入口在当前
Catalog 下只能失败时，应替换成清晰错误并删除不可达的执行能力，避免未来重新激活未签名路径。

**§3.5 阶段 D 是 Claude 的第一个退休实例**，必须按本节流程执行而不是直接删除：`internal/pkg/claude`
当前被 **17 个非测试文件**引用，消费者闭集必须先证明再迁移。尤其注意退役范围只限 `claude-code`
persona 使用的版本面（`CLICurrentVersion`、`DefaultHeaders`、beta 序列常量）；`constants.go` 同时
服务 API Key mimic 等非对齐路径，那部分具有产品语义，属于第 1 步中「必须保留」的部分。

**当前阻断**：`docs/egress/maintenance/compatibility-code-retirement-closure.json` 不含任何 Claude
条目。在建立 Claude 退休闭集之前，不得声称 Claude 侧兼容代码已完成清理审计；阶段 D 完成时必须
同步把闭集扩展到 Claude，使每个候选只能是「已退休」或「因产品语义必须保留」。
