# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的自用增强版 fork。维护目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 项目状态

| 项 | 说明 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| 版本与差异 | 源码版本以 `backend/cmd/server/VERSION` 为准，已发布版本以 GitHub Releases 为准。自定义差异看最近一次合并的上游 tag 至 `HEAD`，合并点可用 `git log --oneline --grep='sync upstream'` 查到。 |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| 命名约定 | 对外使用 `sub2apiplus` / `Sub2API Plus`；主服务 Go module 和 import 保留 `github.com/Wei-Shaw/sub2api`，降低上游合并成本。 |
| Go 版本 | 主服务 `go 1.27.3`（`backend/go.mod`）。 |
| Docker 命名 | Compose service 保留 `sub2api`；默认容器名为 `sub2apiplus`、`sub2apiplus-postgres`、`sub2apiplus-redis`。 |

维护原则：

1. 长期跟随上游主线，发布版本按上游 release/tag 对齐。
2. Plus 账号级开关优先放入 `account.Extra`；mimic / 三方客户端兼容走 Plus 设置页；OAuth 官方客户端伪装为内置能力，不提供账号级开关。
3. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、反向代理配置、`.env` 和数据目录。

## 1. Plus 增强功能

管理后台侧边栏“Plus 增强功能”进入 `/admin/settings/plus-enhancements`，包含“API Key 官方客户端兼容”和“三方客户端兼容”两个 Tab。

### 1.1 OAuth 官方客户端伪装

> 本节仅提供功能摘要。共享目标、运行架构、证据生命周期、升级、验收、发布与回滚合同以
> [官方 OAuth 客户端出站仿真共享框架](docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)为准；发生冲突时以该框架为准。

当业务系统最终选择官方 OAuth 账号出站时，入站客户端不拥有最终 wire。兼容层只负责协议、模型、工具和请求语义转换；Key、Group、账号路由、调度及计费归属继续由原业务系统管理。

| 仿真对象 | 允许的正向入站 | 最终出站规则 | 拒绝边界 |
|---|---|---|---|
| Codex CLI | 官方 Codex CLI，以及通过已批准接口接入且能够无损转换的第三方客户端 | 使用 OpenAI OAuth 账号出站时，由 production active Codex ReleaseBundle 统一生成最终 wire | 无法无损转换、未登记协议或范围外语义失败关闭 |
| Claude Code | 仅接受 OfficialIngressCatalog 已登记的 Claude 官方客户端 | 使用 Anthropic firstParty OAuth 出站时，由 production active Claude ReleaseBundle 统一生成最终 wire | 第三方客户端、未登记版本或不匹配的官方客户端形态在读取 OAuth 凭据前拒绝 |

请求通过准入后，由对应 Persona 的 Planner、Identity Authority、ReleaseBundle、DialectCompiler、Executor 和 Runtime Guard 完成最终定型。入站名称、版本、User-Agent、Header、协议形态或自报身份不能选择生产画像，也不能直接透传为官方客户端身份。

最终 wire 等价标准、Persona 隔离、active／rollback 选择、候选验收及生产激活流程均以共享框架为准。当前客户端版本、规则、证据和运行状态分别见：

- [Codex CLI 客户端仿真与版本演进手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)
- [Claude Code 客户端仿真与版本演进手册](docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)

API Key 官方客户端兼容不属于本节范围，见 §1.3。

### 1.2 多协议平台与账号级跨平台调度

本节说明两项配套能力：「自定义」多协议平台（`custom`），用来接 Ollama Cloud 这类没有独立平台、同时提供多种协议端点的站点；Composite 分组的账号级跨平台候选池，让声明同一个模型的多个账号不再因分属不同平台而判为歧义，而是先选「客户端协议零转换直通」的账号，再比账号优先级。比较对象是账号自己的优先级，不是平台的优先级；优先级配在账号上。

例如 Composite 分组里有 DeepSeek 官方、commandcode.ai 和 Ollama Cloud 三个账号，都声明 `deepseek-flash`：客户端无论用 Chat Completions、Responses 还是 Anthropic 格式请求，系统先在能零转换直通的账号里按优先级挑一个，用对应协议的原生端点发出去；没有零转换账号时，才按优先级挑一个账号做协议转换。

#### 1.2.1 自定义多协议平台（custom）

1. **用途与约束**：平台 ID `custom`，显示名「自定义」，由 OpenAI 网关按入站协议分流，排在 Composite 回退顺序末尾。账号类型只有 API Key，API 协议固定为自适应（表单不显示协议选择卡片），平台不带任何默认地址，协议是否可用完全由账号填写的地址决定。只有 Anthropic 接口的站点不适用本平台，用 Anthropic 平台的 API Key 账号接。
2. **三个地址框**：填了哪个协议的地址，账号才支持哪个协议；入站协议没有对应地址时回落 Chat Completions，所以 Chat Completions 必填。

   | 地址框 | 必填 | 填写规则 | 拼接结果 |
   |---|---|---|---|
   | Chat Completions | 必填 | 填到版本号为止，如 `https://ollama.com/v1` | 追加 `/chat/completions`；地址没有版本段时补 `/v1` |
   | Responses | 可选 | 填到版本号为止；站点路径特殊时可直接填到端点全路径，不会重复追加 | 版本感知追加 `/responses` |
   | Anthropic | 可选 | 填到域名为止，如 `https://ollama.com`，不要带 `/v1` | 追加 `/v1/messages` |

3. **认证头**：Chat Completions 与 Responses 端点一律 `Authorization: Bearer`。Anthropic 端点可选 `x-api-key` 或 Bearer（账号 Extra 里的 Anthropic 认证头选项）；出站地址是 Ollama Cloud 主机时，不管选哪项都强制 Bearer。
4. **后端统一校验**：创建（含复制）、单个更新、批量更新和导入四条管理路径都按「账号合并后的完整凭据」校验，不只看本次提交的增量：类型必须是 API Key，`api_protocol` 必须是 `adaptive`，缺 Chat Completions 地址返回 400（业务码 `CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED`）。校验通过后把 `base_url` 归一为 Chat Completions 地址，两个字段始终一致，避免缺地址时回退到 OpenAI 官方地址把请求发错上游。批量更新混合平台时，只有 `custom` 账号被校验和归一，其它平台账号不受影响；批量更新里 `base_url` 归一后确实变化的账号，旧的上游计费探测快照随之清空。
5. **Ollama Cloud 能力继承**：Ollama 的能力按实际出站地址识别，与账号所在平台无关，`custom` 账号自动继承。用量窗口的平台白名单已加入 `custom`（后端 `isOllamaCloudUsagePlatform` 与仓储 SQL 白名单两处同步）；Chat Completions 推理字段归一化与输出 token 上限夹紧按实际出站地址判定。
6. **Responses 一律无状态**：`custom` 的原生 Responses 出站强制 `store=false`，并删除 `previous_response_id` 与 `conversation`，客户端必须自带完整上下文（原因见 §1.2.5）。

#### 1.2.2 模型声明

候选池只看账号显式写下的模型声明，不看平台前缀规则，也不展开平台内置的默认映射。

1. **必须声明**：进 Composite 分组的账号必须配置模型白名单或模型映射（两者保存在同一个 `model_mapping` 字段，白名单是自己映射到自己），官方 OAuth 账号也不例外。`custom` 没有前缀规则，不声明就永远不会被选中。
2. **强弱声明**：精确映射（键与公开模型名完全相同，映射目标非空）是强声明；通配映射是弱声明。分组内有任何强声明账号时，只有强声明账号入池；完全没有强声明时才用弱声明账号。因此映射为 `*` 之类的兜底账号不会冒领已被其它账号显式声明的模型。
3. **空映射不入池**：映射为空的账号对任何模型都没有声明。

#### 1.2.3 账号级跨平台候选池

1. **生效入口**：Composite 分组的四种 POST 文本入口，以及不带 `/v1` 前缀的根路径别名：`/v1/messages`、`/v1/messages/count_tokens`、`/v1/responses`、`/v1/chat/completions`。
2. **解析顺序**：显式路由表（管理员配的「公开模型名 → 目标平台 + 上游模型名」）优先；其次候选池；候选池扫描到零个声明账号时，交回原有的账号归属判定与前缀猜测，行为与引入候选池之前一致。单账号同样生成候选集合，所以单账号的通配声明也能生效。
3. **不进池的入口**：Responses WebSocket、`/v1/responses` 的子路径（如 compact）、Gemini 原生路径、生图、embeddings、Live、`alpha/search`、realtime、`/backend-api/codex/responses` 直连路径、管理端预览路由。这些入口按设计保持原有行为。
4. **候选集合**：候选池把目标平台、公开模型名、来源标记 `account_model` 和一个「候选账号 ID 集合」写回路由决策；下游调度器仍是单平台的，只在集合内按原策略选号，三处调度声明闸门改为按「账号 ID 属于集合」放行，没有集合时沿用原来的精确判定。

#### 1.2.4 协议优先规则

1. **先查绑定**：先看会话粘性与 Responses 续链（见 §1.2.5），有绑定直接用绑定账号。
2. **零转换优先**：没有绑定时，候选分为零转换组和需转换组。OpenAI 网关族账号用 `resolveUpstreamProtocol` 的返回值判定，等于入站协议即零转换；Anthropic 平台账号只对 Anthropic 入站（含 `count_tokens`）零转换，其它 Anthropic 网关族平台一律需转换。
3. **组内排序**：零转换组非空时只在该组内选，按账号 `Priority` 升序（数值小的优先），并列按账号 ID 升序，取首选；零转换组为空才用需转换组，排序相同。比的是账号优先级，不是平台优先级。
4. **集合怎么放**：集合只放与首选账号同平台、通过资格过滤的账号，再由该平台的调度器在集合内按原策略（优先级、负载、LRU）选号。不做请求内换组。
5. **协议随账号地址变化**：给 `custom` 账号清空 Responses 地址后，Responses 入站对它就变成需转换，它退到需转换组；同平台里 Responses 地址齐全的账号优先于只填了 Chat Completions 地址的账号，哪怕后者优先级数值更小。

#### 1.2.5 粘性与续链

1. **两套粘性契约分别查**：OpenAI 网关族的会话哈希算法与缓存键（带 `openai:` 前缀）和 Anthropic 网关族（裸键，Gemini 另带 `gemini:` 前缀）不同。同一条路径的请求可能进任一套网关，所以候选池按候选账号所属的网关族分别算哈希、分别查键，命中且绑定账号仍合格就用它。候选池只读，不写粘性，写入仍由各网关照旧。
2. **普通粘性**：目标平台跟绑定账号走，集合是绑定账号加同平台其它合格候选，不按零转换过滤。绑定账号即使需要协议转换也继续使用；它失败时可在同平台内换号，满并发时可按调度器的拥塞转移机制移到同平台其它账号。
3. **有状态续链只对 OpenAI 平台账号成立**：请求带 `previous_response_id` 且响应归属查得到、归属账号是 OpenAI 平台账号并仍合格时，集合只放该账号（锁定单账号，换账号就续不上），优先于普通粘性。国产平台与 `custom` 的 Responses 都是无状态的，不存在可续的链。
4. **无状态账号的续链请求**：带 `previous_response_id` 的请求落到无状态账号时按普通请求处理，转发层剥离该字段和 `conversation`，`store` 为 false。处理器仍有两道前置校验：形如消息 id 的值返回 400；不属于本用户本分组的响应 id 也返回 400（该 id 对此用户不可用）。合法 id 加完整历史，正常；只带增量输入（例如只有工具结果）时，剥离引用后上游没有历史，结果由上游决定，这是客户端责任。

#### 1.2.6 资格与配额

1. **资格过滤**：候选池按每个候选账号自己的平台，复用两套调度器现有的资格判断，不另造健康状态：停用、限流、临时不可调度、模型级限流、模型支持、利润门、渠道限制、进程内运行时封禁、影子账号母账号健康，Anthropic 网关族还包括账号额度、窗口费用与 RPM 限制。代理流隔离、compact 与生图能力筛选留给平台内调度器终检。
2. **用户 × 平台配额**：只读预检，按平台去重，沿用原有模式豁免：简易模式跳过，订阅模式豁免，余额模式才检查。预检不递增任何计数；正式准入与 RPM 计数仍按首选平台执行一次，多候选时分组与用户的 RPM 各只计 1 次。
3. **全部不合格时**：声明账号全部没通过资格过滤，不退回原流程猜平台，也不在候选池里新造错误，而是按排序写回，由下游调度器与准入检查返回它们原本的错误。持久停用（状态非启用，或关闭了可调度开关）的账号视同没有声明，不参与强弱分层；声明账号全部被持久停用时视同零声明，交回原有的归属判定与前缀猜测。冷却、限流、过载这类瞬态不合格仍按“全部不合格”处理：按排序写回，由下游报原本的错误。

#### 1.2.7 失败边界与生效时延

1. **失败边界**：请求中途上游失败只在同平台内换号；首选账号返回 429 或 529 会进入冷却，下一次请求选到次优账号；5xx 默认不冷却，需要时配置已有的「临时不可调度」规则；集合内账号被调度器终检全部否决时，本次请求报错，不换组；流式已经输出后不能换号。
2. **缓存**：账号配置态（分组里有哪些账号及其声明）复用模型列表缓存，TTL 由 `gateway.models_list_cache_ttl_seconds` 控制，默认 15 秒（取值 10 到 30）；声明命中的候选账号每次实时读取最新账号，状态、限流、冷却、优先级、地址等都以实时值为准。
3. **生效时延**：冷却、限流、停用、重新启用，以及修改优先级、映射（含精确映射与通配映射互改）、协议地址、解绑分组、删除账号，下一次请求立即生效；新增声明账号、新绑定分组、给账号新加原本没有的模型声明，最长延迟一个模型列表缓存 TTL（`gateway.models_list_cache_ttl_seconds`，默认 15 秒），与原有的账号归属解析缓存一致。粘性会话固定在原账号上，旧会话不会因为更高优先级账号恢复而迁移，新会话才重新选。

#### 1.2.8 计费

计费沿用渠道的计费基准设置（请求模型、渠道映射模型或上游响应模型），默认按映射后的上游模型名。全局价格按模型名匹配：Ollama 上的 `deepseek-v3.1` 会匹配到 DeepSeek 官方价，要按站点价计费，需在渠道定价里按 `custom` 平台手配。模型目录里没有的模型名，系统告警并按零成本落账，不拒绝请求，所以新站点的模型要先配价，否则会免费放行。

#### 1.2.9 已知限制

- **Responses 输出上限**：非 Codex 客户端请求 `custom` 原生 Responses 时，`max_output_tokens` 被删除，上游按默认上限输出，Ollama 的夹紧逻辑也无法恢复被删除的客户端上限。变通方法：改用 Chat Completions 的 `max_tokens`，或把账号的 Responses 地址留空，让 Responses 入站回落 Chat Completions（多一次协议转换）。该转发器属于 Codex 事实映射源码快照，修复要随下一次 Codex 目标版本登记一并重封身份基线，本次不改。
- **不做请求内跨平台换号**：请求中途失败只在同平台内换号，见 §1.2.7。

#### 1.2.10 ARM64 实测结论

2026-10-24 在 ARM64 测试机上，按 18 条验收标准逐条实测，全部通过，覆盖三种入站格式的零转换直通与端点、清空 Responses 地址后的退让、停用与 429 冷却后的切换、有状态与无状态续链、两套粘性、单账号与纯通配声明、RPM 只计一次、管理 API 校验与归一、用量计费、粘性账号需转换、同平台换号，以及旧调度器与高级调度器两套拥塞转移。

有两处与方案字面不同但符合规则的实测结果：

- commandcode.ai 对 deepseek 模型的 Anthropic 入站需转换为 Chat Completions，因此停用 Ollama 后，Anthropic 入站选中的是零转换的 DeepSeek 账号，而不是 commandcode.ai。
- Ollama 免费计划不含 `deepseek-v4.1-flash`，三协议原生直通的主表改用免费模型验证。

### 1.3 API Key 官方客户端兼容

API Key 官方客户端兼容让 Kilo / Cline / Cursor / Roo Code 等非官方客户端尽量接近 Claude Code / Codex CLI 在 **API Key 认证模式**下的 header、body、TLS 和路由形态。它是独立产品路径，不属于 §1.1 的 OAuth Persona，也不继承 OAuth ReleaseBundle、Approval 或 Runtime Selector；即使个别 `ClientBuild` 或传输事实取值相同，也必须在 API Key 自有 Profile 与证据边界内独立维护。

API Key 基准与 OAuth 仿真使用相同的 direct／MITM 证据分层，但认证、目标版本、Profile、运行状态和证据必须彼此独立；API Key 目标版本见 §1.3.2。抓包入口与证据边界分别见[官方客户端出站工具索引](tools/official_client_capture/README.md)和[脱敏实证索引](backend/internal/service/testdata/official_egress/README.md)。自动化任务可以顺序编排两类认证及其 HTTP／WebSocket 场景，但每个场景必须生成独立证据；外部站点 A/B 也必须作为独立验收阶段执行。

默认画像已直接切换为 CLI 目标（版本见 §1.3.2），旧 Desktop 画像仅作为服务级 `previous` 回退保留，见 §1.3.4。不设账号级灰度。

#### 1.3.1 生效范围与运行规则

1. 仅 Anthropic / OpenAI 的 API Key 账号生效，不改变 OAuth 账号逻辑。
2. mimic 与 passthrough 运行时互斥；同时开启时，非官方客户端优先走 mimic。
3. 账号测试按非官方入站请求处理并走与正式 Gateway 相同的 Profile 解析、Header/Body Finalizer 和 TLS 决策；`active` 与 `previous` 两种模式都由同一份请求级画像同时决定 Header/Body 与 TLS，不存在按模式分叉的独立判定。
4. 非官方客户端命中 mimic 时，关键身份 header 不允许被账号级 header override 覆盖；官方客户端跳过 mimic 后不应用该保护。
5. OpenAI Codex mimic 的 `/v1/messages` 固定进入 Responses mimic 链路，不受 `force_chat_completions`、普通 Responses probe false 或 `openai_responses_supported=false` 影响。
6. OpenAI Codex mimic 只支持 HTTP/SSE 上游，命中时不进入 Responses WebSocket（WS 画像在 registry 中只存档不激活）；跳过 mimic 的官方 Codex 客户端按普通 API Key 账号的全局/账号 WS 开关、force HTTP 和 WSv2 mode 选择路由。该判断同时作用于账号调度、粘性账号复核和最终转发。
7. 官方客户端识别只依据 Gateway 入站请求上下文（UA、`originator`、`metadata.user_id`）。没有入站上下文的内部入口不做该识别，因此不会因为实际发起方是官方 CLI 而跳过 mimic。
8. 管理后台账号测试在 HTTP 200 且流以 EOF、`[DONE]` 或 `message_stop` 任一路径正常结束时判成功；非 200、或正文命中服务暂不可用降级文案时失败并保留上游错误。

两个入口的实际行为：

| 入口 | 客户端识别 | Anthropic | OpenAI |
|---|---|---|---|
| Gateway 公网入口 | 按 UA / `originator` / `metadata.user_id` 识别 | 官方客户端跳过 mimic | 官方客户端跳过 mimic |
| 管理后台账号测试 | 不识别（传入空 Gin Context） | 走 Claude Code CLI API Key 画像 | 走 Codex CLI API Key 画像 |

```json
{"anthropic_apikey_mimic_claude_code":true,"openai_apikey_mimic_codex_cli":true,"enable_tls_fingerprint":true}
```

两个开关名与 CLI 目标一致。

#### 1.3.2 active 画像与 Profile Registry

| 平台 | mimic 目标 | 出站行为 |
|---|---|---|
| Anthropic API Key | Claude Code CLI `2.1.220` | `/v1/messages` 发送 `x-api-key` 且移除 Bearer 认证；UA、Linux/x64、Node `v26.3.0`、beta、`sdk-cli` billing、system 与 cache 形态来自 API 抓包。`/count_tokens` 使用独立 generic Contract，不冒充 messages 官方样本。 |
| OpenAI API Key | `codex_exec/0.145.0` | 默认 profile 为 `codex_exec_0_145`，发送 `originator=codex_exec`、`remote_compaction_v2`、`responses-lite=true`，不发 `OpenAI-Beta` 和 `version`；metadata 使用 `sandbox=seccomp`，`parallel_tool_calls=false`。 |

Profile 数据集中在 `official_client_profile_registry.go`，每个 Wire Profile 都包含不可变 ID、抓包来源、适用端点、传输画像和 SHA-256 digest。服务级指针由 `gateway.official_client_profiles.mode` 单独控制，只允许 `active`/`previous` 整体切换，未知模式或不完整画像会 fail-closed。历史账号字段 `openai_apikey_mimic_codex_profile` 仅作 dormant 数据保留，运行时不再覆盖服务级指针。

**Anthropic 1M beta**：1M 自动补全只作用于开启了 API Key 官方客户端兼容的
API 账号构造链——按最终映射模型自动补 `context-1m-2025-08-07`，不要求
Kilo / Cline / Cursor / Roo Code 自己发送。OAuth / SetupToken 官方出站是独立链路，
官方 CLI 默认流量不携带该 beta，因此不做任何 1M 自动补全。适用模型前缀为
`claude-opus-4-8`、`claude-opus-5` 和
`claude-fable-5`；其他模型不凭空添加。补齐后的顺序与 Claude Code `2.1.220 --betas context-1m-2025-08-07`
实抓请求一致：`context-1m-2025-08-07` 位于 `effort-2025-11-24` 之前，且不会重复。
默认 BetaPolicy 不得过滤这项身份 beta；`structured-outputs-2025-12-15` 等按 Body
动态触发的功能 beta 仍受 BetaPolicy 控制。

#### 1.3.3 AnyRouter A/B 验收结果

2026-07-26 已使用 AnyRouter Anthropic #15 / `claude-opus-4-8` 和 OpenAI #97 /
`gpt-5.6-sol`，分别比较“官方 CLI 直连 AnyRouter”与“非官方标准 API 入站 →
Sub2API API Key mimic → AnyRouter”两条 HTTP 出站路径：

| 验收项 | Anthropic HTTP | OpenAI HTTP |
|---|---|---|
| 路由与协议 | `POST /v1/messages?beta=true`、HTTP/1.1 一致 | `POST /v1/responses`、HTTP/2 一致 |
| 静态身份 | Claude Code `2.1.220` UA、Stainless、`x-app`、1M beta 及顺序一致 | `codex_exec/0.145.0` UA、`originator`、Responses Lite、beta features 一致 |
| direct TLS | 规范化 ClientHello 严格相等；官方 2 次、候选 1 次观察归一为同一画像 | 规范化 ClientHello 严格相等；官方 6 次、候选 1 次观察归一为同一画像 |
| 外部结果 | 管理端账号测试和候选 MITM 样本均曾返回 200；顺序修复后的重复请求遇到 AnyRouter 瞬时 520 | 官方与候选均进入已确认的 `gpt-5.6-sol` 负载上限分支；未出现 `client_restricted` |
| 验收级别 | 画像/传输契约通过；已有业务成功样本，但修复后同轮完整功能 A/B 尚待上游稳定时复验 | 画像/传输契约通过；端到端业务响应因模型负载上限尚未证明 |

验证效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。“画像/传输”只验证路由、认证、静态 Header、必要 Body 外层字段和 TLS；“完整功能”还要求同模型、同场景输入并成功得到业务响应。入站对话、工具 Schema、max tokens、thinking/effort 和完整 CLI 运行上下文由调用方语义决定；mimic 只统一路由、认证、官方静态身份、必要 Body 外层字段和 TLS 画像。私有原始证据位于 Vircs `/root/oauth-capture/runs/anyrouter-ab-wire-20260726T072501Z`，脱敏结论为 `analysis/apikey-anyrouter-ab-contract.json`。

#### 1.3.4 previous 回退画像

`previous` 仅用于服务级紧急整体回退，不是默认出站契约，也不得与 `active` 按字段混用：

| 平台 | previous 画像 | 与 active 的关键差异 |
|---|---|---|
| Anthropic | Claude Desktop `2.1.209` | `claude-desktop-3p` billing、Desktop beta/system/工具集合；未指定自定义 TLS Profile 时使用标准 Transport |
| OpenAI | Codex Desktop `0.144.0-alpha.4` | Desktop UA、Header/Body 和传输契约；未指定自定义 TLS Profile 时使用标准 Transport；API Key WS 同样不激活 |

Profile ID 归属：OpenAI 默认指针为 `codex_exec_0_145`（见 §1.3.2），`desktop_0_144` 及其旧别名仅属于 `previous`/配置兼容，`cli_rs_0_125` 保留独立历史兼容路径。

`v0.1.155-3` 的 AnyRouter ARM64 结果只证明 Anthropic Desktop `previous` 画像，不能作为 CLI `active` 画像的证据。旧字段、完整请求契约、工具列表和历史运行记录统一保存在[实证 README](backend/internal/service/testdata/official_egress/README.md)。

#### 1.3.5 发送与回退契约

OpenAI `/v1/responses/compact` 是特例：上游保持官方 unary JSON 形态，请求体按白名单重建，只保留 `model`、`input`、`instructions`、`tools`、`parallel_tool_calls`、`reasoning`、`text`、`service_tier`、`previous_response_id` 九个字段，其余（含 `stream`、`store`、`prompt_cache_key`、`client_metadata`）一律不带出；不补 Codex mimic body 默认值，并强制 `Accept: application/json`。该精简发生在入站规范化阶段，OAuth 路径随后按[Codex CLI 手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)的 SPEC-HDR-007 与 SPEC-EP-020 规则处理 `prompt_cache_key` 和 installation ID，API Key 路径不补。通过普通 `/v1/responses` body-signal 触发且原请求为 `stream:true` 时，下游桥接为最小 Responses SSE，确保包含 `response.output_item.done` 和 `response.completed`。

- mimic 只对齐 header、body、TLS 和路由，不复制服务端隐藏 prompt、账号状态、产品 memory 或 UI 上下文，也不替换响应文本或清洗客户端正文身份。
- Anthropic `active` 画像在启用 mimic 和 `enable_tls_fingerprint` 后，未指定 `tls_fingerprint_profile_id` 时使用内置 Claude Code TLS Profile；`previous` 回退画像才使用标准 Transport。管理员显式选择的固定或随机 TLS Profile 始终优先。
- Codex CLI `active` 画像在开启 TLS fingerprint 时使用 2026-07-26 抓包的 direct 30-cipher/空 ALPN 或 proxy h2 传输画像；管理员显式选择的 TLS profile 仍优先。API Key WebSocket 由路由决策层强制回落 HTTP，不会因注册表中存在未激活 WS 画像而开启（规则见 §1.3.1 第 6 条）。
- OpenAI `previous` 回退画像与 Anthropic 同规则：Desktop 画像没有可复用的实抓 TLS，未指定 `tls_fingerprint_profile_id` 时使用标准 Transport。TLS 决策只能来自请求级画像，正式 Gateway、账号测试和能力探测共用同一判定，任何路径都不得自行重解析出另一个 mode 的画像。
- HTTP/1.1 与 HTTP/2 Transport 的 `DisableCompression` 由 TLS Profile 的 `Transport` 字段驱动，不是固定值：Codex 的 direct/proxy/WS 画像显式设 `true` 以避免自动注入 gzip，Anthropic 画像未设置该字段（等于 `false`）；两者都不影响显式压缩响应的受控解压。
- Codex base instructions 由 `CodexBaseInstructionsForModel()` 按模型分四条分支：含 `codex` 的模型走 `DefaultInstructions`，`gpt-5.5` 走最新版本，`gpt-5.2` 与 `gpt-5.1` 各有单独维护的 prompt（内容为空时回退最新），其余模型回退最新版本。
- `Anthropic-Dangerous-Direct-Browser-Access: true` 在 `claude.DefaultHeaders` 共享常量和 registry 的 Wire Profile 中各有一份，分别服务非 mimic 路径与 mimic 出站，取值一致且都对应 2026-07-26 Claude Code CLI `2.1.220` API 实抓；mimic 运行时只读取自己的 Wire Profile，不回写共享常量。
- 管理后台账号测试只有在 HTTP 200、收到正常 SSE 内容并以 `message_stop` 结束时才成功；非 200 或 HTTP 200 但正文仅为服务暂不可用降级文案时都必须失败并保留上游错误。
- 效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。

### 1.4 三方客户端兼容

在“Plus 增强功能 → 三方客户端兼容”中，为指定的 Anthropic API Key 账号打开“普通 API 兼容”，将官方 Claude Code 请求适配为普通 Anthropic API 请求。开关保存在 `account.Extra.anthropic_apikey_standard_api_compat`，默认关闭，不影响 OAuth 或其它平台账号。

1. **适用方向**：官方 Claude Code 客户端 → 普通 Anthropic API。与 §1.3 中“第三方客户端 → 官方客户端请求形态”的 mimic 方向相反，两项互斥；切换前先关闭另一项。页面开关与后端创建、编辑、批量更新、Extra PATCH 均校验此约束。
2. **处理范围**：出站使用普通 API User-Agent，仅过滤 `anthropic-beta` 中的 `claude-code-20250219`，并清理 `system` 中的 `x-anthropic-billing-header` 键值标识及 Claude Code 官方 CLI、Claude Agent SDK 两种固定身份介绍。真实桌面会话和 `sdk-cli` 可能使用 SDK 身份句，验收时必须覆盖这一形态。支持 system 字符串、文本块数组及同一块内混合业务指令；空身份块移除，其余块的属性保持不变。
3. **保留能力**：保留其它功能 beta、业务 system 指令、messages、tools、剩余块的缓存配置、metadata 和会话头。消息转发、透传、token 计数和账号连通性测试共用处理逻辑；在最终请求头覆写后生效，避免重试或账号覆写再次加入身份标识。
4. **配置与验收**：只为需要此适配的账号开启。用实际客户端验证普通对话、流式与工具调用，并核对用量记录中的账号 ID，避免分组切换到其它账号造成误判。开关只改变请求兼容行为，不承诺上游额度资格或计费方式；实际结果以上游规则和账单为准。

`claude-code-20250219` 是通过 `anthropic-beta` 发送的 Claude Code 相关实验特性标识，日期属于标识名称，不是客户端版本。它的完整内部语义未公开；本功能仅移除这一身份标识，其它功能 beta 按原有规则处理。参见 [Anthropic beta 请求头文档](https://platform.claude.com/docs/en/api/beta-headers)与[订阅者 API 赠送额度说明](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers)。

### 1.5 隐藏已映射模型

账号配置具体模型映射后，模型发现列表只展示供客户端请求的别名，隐藏同一账号中对应的上游原名。例如配置 `deepseek-flash → cline-pass/deepseek-v4.1-flash`，即使保留了原名的自映射，发现列表也只展示 `deepseek-flash`。

1. **仅影响展示**：普通、Composite 和 Codex 模型目录共用展示过滤规则，不删除账号映射，不改变模型准入、调度和请求转发。直接请求 `cline-pass/deepseek-v4.1-flash` 仍按原有配置处理；需要原名直调时，应保留其白名单或自映射。
2. **按账号过滤**：多个别名指向同一上游模型时保留全部别名；同组其它账号独立提供原名时，合并目录仍可展示原名。目标自身也是另一个模型的别名时保留该名称。
3. **兼容现有规则**：单纯自映射不隐藏；通配符、OpenAI 透传模式和各平台模型配置沿用现有语义。本功能不改变独立配置的模型广场与渠道模型目录。
4. **维护方式**：过滤逻辑集中在独立展示辅助函数中，接入现有目录生成入口，不增加数据库字段、配置开关或前端适配，便于长期合并上游更新。

### 1.6 带前缀模型定价回退

带供应商或渠道前缀的模型，在完整名称没有匹配到价格时，使用最后一个 `/` 后的模型名继续查价。例如 `cline-pass/deepseek-v4.1-flash`、`deepseek/deepseek-v4.1-flash` 都可使用 `deepseek-v4.1-flash` 的定价，避免因名称前缀导致费用漏记。

1. **完整名称优先**：完整名已有显式价格时优先使用，包括配置为 `0` 的合法免费价；分组和渠道的既有完整名通配符匹配也保持优先。未命中时才尝试去前缀名称及其现有匹配规则。
2. **保留定价层级**：继续按“分组 → 渠道 → 全局价格表 → 内置兜底”解析。去前缀仅补充名称匹配，不改变自定义价、区间价、分时倍率或图片专用价的适用规则。
3. **沿用模型计费规则**：实际使用裸名回退时，其默认价格、缓存计费和 DeepSeek 峰谷规则与直接请求裸名一致；分组、渠道自定义价格保留原有语义。完整名和裸名都无法定价时，继续报告缺少价格。
4. **生效与维护**：规则用于升级后的请求，历史用量记录不自动重算。改动限定在定价解析链路，请求转发、模型映射和用量记录中的模型名称保持原样，无需数据库迁移或新增配置。

### 1.7 Codex 三方模型启用

管理员按分组启用第三方模型后，Codex 客户端的模型选择器可以直接选到它，并带上正确的上下文、推理档位与工具配置。「启用」只决定能否在 Codex 中选到并带正确配置；未启用的模型仍可通过显式指定模型名调用，权限、路由与协议均沿用现有规则。

1. **管理入口与候选**：Plus 增强页「Codex 三方模型启用」标签。先选分组，页面列出该分组的候选：候选名单就是该分组默认启用的模型，即该分组 Key 调用普通 `/v1/models` 得到的名单（只含可调度账号、遵守分组白名单，并遵守 §1.5 的隐藏已映射模型规则：有映射的只显示映射后的对外名，没映射的显示原名，例如 `moonshotai/Kimi-K3`）。名单由通用网关 handler 生成并注入目录服务，管理页与运行时目录共用同一份；固定账号的 OpenAI 分组改取账号映射加默认清单（只查数据库）。原生 GPT 与没有账号承接、也没开启的条目不在列表里显示。新候选默认关闭；开启须先预览，服务端按路由、能力依据和最坏目录体积校验后整批原子保存，并发修改返回 409；关闭不依赖上游。已开启但不再属于分组默认模型名单的模型停发，页面保留一行以便关闭。
2. **页面读取与体积预算**：页面读取只查数据库，不发任何上游请求，也不触发账号模型清单同步。体积预算只看可调度的官方 ChatGPT 账号（中转账号不参与）：读取时若没有可用的目录缓存，预算显示「开启时检查」，不显示为通过；预览与保存时对官方账号限时 3 秒并发拉取目录，失败的账号跳过，至少一个成功或分组没有官方账号即算完成，全部失败则判未完成并拦住开启。预览与保存使用同一种模式，输入指纹一致。分组没有官方账号时，预算按本地生成的基础目录计算。
3. **客户端配置**：在现有 provider 下增加 `model_catalog_url = "https://<网关地址>/v1/models"`，并确认没有生效的 `model_catalog_json`。Codex 只有配置该项才会拉取在线目录；之后在启动时、每 4 分 30 秒、切换模型或打开选择器时刷新，缓存 300 秒，无需重启。目录拉取失败时 Codex 会清空整个模型列表，没有内置兜底。用 ChatGPT 账号登录的桌面版（含 ChatGPT 桌面版内嵌的 Codex）还要在 config.toml 的 `[features]` 段写明 `api_key_model_discovery = true`（已有 `[features]` 段时加在段内，不能重复建段）：桌面版会按 OpenAI 的灰度开关在运行时关闭这项在线目录发现，配了 `model_catalog_url` 的提供方又没有内置兜底，选择器会变成空白；配置文件里写明的特性不会被运行时覆盖。用 API Key 登录的客户端不需要这一行。
4. **同名透传与能力档案**：第三方模型的目录 slug、显示名、客户端提交名使用列表里的对外名（上游叫什么，Codex 里就显示什么），例如 `kimi-k3`；显式别名按原规则处理。条目内容取自集中维护的能力档案（`deepseek-flash`、`deepseek-v4-pro`、`kimi-k3`、`glm-5.3`），匹配时取上游型号最后一个 `/` 之后的部分并忽略大小写（与 §1.6 同一规则），不按站点域名区分；多个账号映射到不同原始上游名、规范化后命中同一档案不算冲突。`deepseek-flash` 与 `deepseek-v4-pro` 两个档案直接嵌入 DeepSeek 官方 Codex 教程的 models.json 条目，下发时只有 `slug` 与 `display_name` 取对外名，其余字段（包括官方指令模板、`apply_patch_tool_type=freeform`、`support_verbosity`）与官方逐键一致，官方没有的键不补；Kimi、GLM 仍是自写档案。条目再按承接账号的能力快照只做收窄（例如账号明确不含图片输入时移除图片能力）。没有可靠能力依据的型号（例如尚无档案的 Grok 等平台型号）显示「待补能力」，不能开启。
5. **原生 GPT 与分派**：原生 GPT 按真实路由判定，不凭名称前缀；只有官方 OpenAI 账号的分组，目录原样返回、逐字节不变。`/v1/models?client_version=...` 与 `/backend-api/codex/models` 对 OpenAI 分组走实时基础目录加第三方条目，对 Kimi、智谱、DeepSeek 等分组走空基础加第三方条目；Anthropic、Gemini 分组与不带 `client_version` 的普通模型列表不受影响。
6. **Composite 分组的在线目录**：基础目录只从可调度的官方 ChatGPT 账号拉取（沿用整次请求 3 秒的基础目录预算），中转 API Key 账号不拿来拉目录，因为中转站的 `/models` 可能是几千个各家模型的普通列表，客户端 5 秒预算内转换不完。没有可调度的官方账号，或官方账号拉取失败、超时，就按分组内 OpenAI 账号的模型映射在本地生成基础目录（遵守隐藏已映射模型和分组白名单，使用内置官方元数据），再叠加已开启的第三方条目，不再返回 500。
7. **客户端约束与降级**：目录响应必须不超过 1 MiB 并在 5 秒内返回，否则客户端会清空选择器，因此基础目录获取按整次请求限时 3 秒。第三方阶段出错或超限时，网关降级为只发布原生 GPT；纯第三方分组返回空目录，此时客户端须显式配置 `model`。只有基础目录本身不可得（Composite 分组除外，它会改为本地生成），或原生部分自身超限时才返回目录错误。
8. **运维注意（自定义工具）**：DeepSeek 官方条目要求 `freeform` 自定义工具（`apply_patch`），而有的中转（实测 HUB 类聚合中转）走 Responses 时不接受自定义工具，会返回 422，网关记为 502。遇到这种情况，把该中转账号的「Responses API 支持」设为「强制 Chat Completions」（网关会把自定义工具转成函数工具），或从该账号映射里去掉 DeepSeek 型号。
9. **已验证状态**：真实客户端验收通过后可在页面「标记已验证」，记录与路由、协议、能力档案和接入站点绑定的指纹；任一项变化后自动回落为「配置可用」。
10. **页面**：状态筛选只有「全部」「已开启」「未开启」「不可开启」四项，行内仍显示不能开启的原因。已删除「同步候选」按钮和「账号模型来源同步状态」区块（后端 `POST …/sync` 手动同步接口保留，页面不再调用）。目录体积预算正常时显示为工具栏里的小标签，悬停可看明细；超限、未完成、开启时检查三种状态才显示整块卡片。说明文字收为一行，客户端配置说明默认折叠；页面右上角的刷新同时刷新本标签。
11. **维护方式**：逻辑集中在 `codex_third_party_*` 新文件，配置与源快照存于 settings 表的 `plus_codex_model_visibility:<分组 ID>`、`plus_codex_catalog_source:<账号 ID>` 两类键（源快照只由手动同步写入，不再决定候选），不新增数据库表或实体字段；既有文件只保留单行接点，清单见 §4.1，合并核对项见 §4.2。

### 1.8 其它优化

- Composite 与 Grok 同等豁免，使 Anthropic 协议也能访问 GPT、Grok 模型。
- Composite 分组的模型列表按请求协议过滤：OpenAI Compatible 与 OpenAI Responses 共用 OpenAI 模型目录；携带 `Anthropic-Version` 或 Claude CLI 特征时仅返回 Anthropic 协议模型。Kimi、智谱、DeepSeek 按账号实际 `api_protocol` 归组；配置为自适应协议（`api_protocol=adaptive`）的账号会根据入站请求选择上游协议，因此其模型同时显示在 OpenAI 与 Anthropic 两类目录中。自定义模型列表仅在过滤结果上取交集；该功能只影响模型展示，不限制实际请求路由。
- Anthropic OAuth 账号增加“模型限制”区块，复用 `ModelWhitelistSelector`，支持同步最新模型、同步上游模型和清空模型；`oauth` / `setup-token` 账号的配置统一持久化到 `model_mapping`。

## 2. 全新服务器部署

推荐使用 Docker Compose。主服务目录为 `/root/docker/sub2apiplus/app`，以下流程统一使用运行目录中的 `docker-compose.yml`。维护既有实例前，应通过容器的 `com.docker.compose.project.working_dir` 标签确认真实目录，注意历史路径的大小写差异。

可复现部署应选择已发布 Plus 版本，同时使用 `ghcr.io/itv3/sub2apiplus:<Plus版本>` 和 Git tag `v<Plus版本>` 中的部署文件；`latest` 适合持续跟踪最新稳定版的环境。

### 2.1 准备主服务

宿主机先安装 Docker、Docker Compose plugin、`git`、`curl` 和 `openssl`。快速部署可用一键脚本下载 `main` 模板、生成 `.env`，并自动填入 `JWT_SECRET`、`TOTP_ENCRYPTION_KEY` 和 `POSTGRES_PASSWORD`。该方式跟踪 `latest`；需要可复现部署时应使用下方固定版本流程：

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -sSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-deploy.sh | bash
```

需要手动审查和准备文件时，先把 `VERSION` 替换为目标发布版本，再从同一 tag 下载部署文件并固定主服务镜像：

```sh
export VERSION="<已发布的 Plus 版本号，不含 v>"
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -fsSL "https://raw.githubusercontent.com/itv3/sub2apiplus/v${VERSION}/deploy/docker-compose.local.yml" -o docker-compose.yml
curl -fsSL "https://raw.githubusercontent.com/itv3/sub2apiplus/v${VERSION}/deploy/.env.example" -o .env
sed -i -E "s#image: ghcr.io/itv3/sub2apiplus:[^[:space:]]+#image: ghcr.io/itv3/sub2apiplus:${VERSION}#" docker-compose.yml
mkdir -p data postgres_data redis_data
```

主服务 `.env` 至少要设置以下变量，各变量说明统一见 §3.2：

```env
COMPOSE_PROJECT_NAME=sub2apiplus
POSTGRES_PASSWORD=<随机强密码>
JWT_SECRET=<openssl rand -hex 32>
TOTP_ENCRYPTION_KEY=<openssl rand -hex 32>
ADMIN_EMAIL=<自定义后台登录邮箱，可选>
ADMIN_PASSWORD=<自定义后台密码，可选>
```

```sh
docker compose up -d
curl -fsS http://127.0.0.1:8080/health
# 未设置 ADMIN_EMAIL / ADMIN_PASSWORD 时，从首次启动日志查看随机生成的登录邮箱与一次性密码
docker compose logs sub2api | grep "Generated admin"
```

后台地址为 `http://<服务器IP>:8080`。登录后先添加 API 账号，并确认平台、可用模型和模型成本配置正常。

### 2.2 Apple 容器部署（macOS）

Apple silicon Mac 可使用 Apple `container` 1.1+ 在本机运行 Sub2API Plus、PostgreSQL 和 Redis，无需 Docker Desktop。该方式面向本地开发和管理员维护的部署，不提供 Compose 等价的自动重启与持续编排能力；生产环境仍推荐 Docker Compose。

```sh
cd deploy
./apple-container.sh init
./apple-container.sh up
./apple-container.sh status
```

完整生命周期、持久化、升级和限制说明见 [`deploy/APPLE_CONTAINER.md`](deploy/APPLE_CONTAINER.md)。

## 3. 运维与升级

### 3.1 主服务常用命令

```sh
cd /root/docker/sub2apiplus/app
docker compose ps
docker compose logs --tail=100 sub2api
docker compose restart sub2api
```

镜像 `ghcr.io/itv3/sub2apiplus` 支持 `linux/amd64` 和 `linux/arm64`；`latest` 为最新稳定版，`<Plus 版本>`（如 `0.1.164-1`）固定版本，`<上游主版本>.<上游次版本>` 指向对应 minor 线的最新补丁。

### 3.2 关键环境变量

| 变量 | 用途 |
|---|---|
| `COMPOSE_PROJECT_NAME=sub2apiplus` | 固定 Compose 项目名和 Docker 网络名。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码，必须固定保存。 |
| `JWT_SECRET` | 登录会话签名密钥，生产环境必须固定。 |
| `TOTP_ENCRYPTION_KEY` | TOTP 加密密钥，生产环境必须固定。 |
| `ADMIN_EMAIL` | 管理员登录邮箱（即登录用户名）；留空时首次启动随机生成，从首次启动日志（`docker compose logs sub2api`）查看。 |
| `ADMIN_PASSWORD` | 管理员初始密码；留空时从 `docker compose logs sub2api` 查看自动生成值。 |

### 3.3 数据迁移

迁移时停止主服务，并打包整个 `/root/docker/sub2apiplus`：

```sh
docker compose -f /root/docker/sub2apiplus/app/docker-compose.yml down
cd /root/docker
tar czf sub2apiplus.tar.gz sub2apiplus/
```

在新服务器解压后启动：

```sh
docker compose -f /root/docker/sub2apiplus/app/docker-compose.yml up -d
```

### 3.4 发布和应用更新

发布前先跑定向测试；大范围合并或共享逻辑改动时扩大到 `go test ./...` 和完整前端测试：

```sh
(cd /path/to/sub2apiplus/backend && go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service)
(cd /path/to/sub2apiplus && make test-frontend)
```

Plus 版本在上游版本后追加自定义序号，例如 `0.1.164-1`；Git tag 为 `v0.1.164-1`，镜像 tag 为 `ghcr.io/itv3/sub2apiplus:0.1.164-1`。抓包验收使用的阶段构建（如 `0.1.164-1-n2`）不对外发布，也不进入该命名体系，只出现在实证归档。Release workflow 使用 annotated tag 的 message 生成 Release notes，因此必须使用 `git tag -a -m`；轻量 tag 不包含说明正文。

```sh
cd /path/to/sub2apiplus
VERSION="<待发布的 Plus 版本号，不含 v>"
echo "$VERSION" > backend/cmd/server/VERSION
git add backend/cmd/server/VERSION
git commit -m "chore: release v${VERSION}"
git push origin main
git tag -a "v${VERSION}" -m "v${VERSION}

- 本版改动要点 1
- 本版改动要点 2"
git push origin "v${VERSION}"
```

> 已经打成轻量 tag 时，可用 `gh release edit "v${VERSION}" --notes-file <文件>` 事后补写 Release 说明，无需重跑 CI。

如果 tag push 没触发 Release，手动触发：

```sh
gh workflow run Release --repo itv3/sub2apiplus --ref main -f tag="v${VERSION}" -f simple_release=false
```

发布完成后，AMD64 / ARM64 均使用固定版本更新主服务。升级前备份数据库，并保留当前镜像和 Compose 文件；更新只替换应用容器，保留 PostgreSQL、Redis、volume、配置和 `.env`。部分数据库迁移没有自动反向迁移，回滚方案应参考对应 Release notes。

```sh
export VERSION="<新发布的 Plus 版本号，不含 v>"
cd /root/docker/sub2apiplus/app
cp -p docker-compose.yml docker-compose.yml.bak
sed -i -E "s#image: ghcr.io/itv3/sub2apiplus:[^[:space:]]+#image: ghcr.io/itv3/sub2apiplus:${VERSION}#" docker-compose.yml
docker compose pull sub2api
docker compose up -d --no-deps sub2api
docker compose ps
docker compose logs --tail=100 sub2api
curl -fsS http://127.0.0.1:8080/health
```

远程更新可能影响当前请求链路时，应以单条命令启动 `systemd-run` 等后台更新事务：预拉镜像、备份、切换、健康检查和失败回滚均由远端进程完成，执行结果写入日志，避免依赖当前会话持续连接。健康检查端口按实例实际映射配置。

不要执行 `docker compose down -v`，不要删除 volume，不要覆盖 `.env`。维护既有实例前先读取容器的 `com.docker.compose.project.working_dir` 标签确认真实目录，历史实例的路径大小写可能与本文推荐路径不同。

Watchtower 可拉取并替换镜像仓库中的主服务镜像；要求发布与部署严格对应时，应固定镜像版本并在发布后执行健康检查。

### 3.5 发布、源码与验证镜像的三层状态

正式发布（GitHub Releases 的 tag 与多架构 GHCR 镜像）、当前源码（版本以 `backend/cmd/server/VERSION` 为准，可能尚未发布）和 Vircs 验证覆盖层（直接编译的阶段验收镜像，命名规则见 §3.4，不对外发布）必须分开理解，不能互相代替；生产部署只使用已发布 tag。各层当前的具体版本、镜像 digest、运行目录、场景计数、pcap、MITM 对比和恢复记录统一见[实证 README](backend/internal/service/testdata/official_egress/README.md)，本文不保存快照数值；原始 pcap、完整 Body 和 CLI 事件属于私有敏感证据，不提交 Git。

### 3.6 其它运行能力

Gemini 支持内置 Gemini CLI OAuth Client 的 Code Assist OAuth、通过 `.env` 配置 `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET` 的 AI Studio OAuth，以及后台直接添加 API Key。

后台“数据管理”入口当前仅保留兼容诊断；服务端固定返回 `DATA_MANAGEMENT_DEPRECATED`，不建议新部署 `datamanagementd`，也不要按旧流程挂载 `/tmp/sub2api-datamanagement.sock`。数据迁移优先使用第 3.3 节的本地目录迁移流程；数据库备份请在 PostgreSQL / Redis 层独立执行。

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径。

ChatGPT Live 实时会话由 `/v1/live` 与 Codex `/backend-api/codex/realtime/calls` 转发，受分组开关控制，并使用并发租约和会话数硬上限约束，用量按实时会话单独记录。

OpenAI WS 账号使用 `http_bridge` 模式前，必须先开启 WS v2 模式路由：在配置文件设置 `gateway.openai_ws.mode_router_v2_enabled: true`，或设置环境变量 `GATEWAY_OPENAI_WS_MODE_ROUTER_V2_ENABLED=true`。WS 连接使用 Redis 60 秒租约并每 20 秒续租；若连续一个完整租期无法确认租约，实例会主动关闭本地连接，避免多实例同时持有同一会话。

## 4. 维护参考

### 4.1 Plus 差异文件清单

发布基线为最近一次合并的上游 tag（查法见 §0）；审核 Plus 实现时优先看该 tag 到 `HEAD` 之间的 OAuth 官方出站、mimic、三方客户端兼容、Codex 三方模型启用、多协议平台与账号级候选池和 Plus UI。Composite 分组、ChatGPT Live、Ollama Cloud 用量同步、`batch_image`、提示词安全审计、异步图像任务和注册安全修复属于上游能力；`.codex-captures/` 和 `.kilo/` 是本地样本或工具配置，不计入源码清单。完整差异用以下命令生成：

```sh
BASE=$(git log --oneline --grep='sync upstream' -1 --format=%H)
git diff --name-only "$BASE..HEAD"
git diff --stat "$BASE..HEAD"
```

| 范围 | 入口文件 |
|---|---|
| 服务层画像投影与 API Key mimic | `backend/internal/service/official_client_profile_registry.go` 和 `official_egress_profile.go` 中保留的 Codex OAuth 运行态投影、API Key mimic 与兼容读取面；这些投影不得成为 OAuth production active／rollback 的第二选择源。 |
| OAuth 官方客户端出站仿真 | `backend/internal/officialegress/`（Persona Registry、Release Catalog/Bundle、Ingress Adapter、Dialect Compiler、Executor、Guard 和传输适配合同），`backend/internal/service/official_egress_1b_executor.go` 及 gateway／WebSocket 接入点，`backend/internal/service/official_egress_claude_state_redis.go`，以及 `backend/internal/repository/official_egress_guard.go` 等窄资源端口。 |
| API Key mimic | `backend/internal/service/*apikey_mimic*`、OpenAI gateway/scheduler/WS 相关 service、`backend/internal/pkg/tlsfingerprint/`、`backend/internal/repository/http_upstream.go`、`backend/internal/handler/openai_gateway_handler.go`。`backend/internal/pkg/claude/constants.go` 若仍服务 API Key 产品语义，只能保留为该独立路径的兼容事实，不得取得 Claude OAuth Persona 的画像或版本选择权。 |
| Codex 三方模型启用 | 新增：`backend/internal/service/codex_third_party_*.go`（契约、目录编排、候选发现、合并、发布判定、真实路由、能力档案、管理服务，以及 Composite 分组基础目录 `codex_third_party_composite_base.go`）、嵌入数据 `codex_third_party_profiles.json` 与 DeepSeek 官方教程 models.json 原文 `codex_third_party_deepseek_official_models.json`、测试数据 `backend/internal/service/testdata/codex_deepseek_official_models.json`（官方原文的测试副本）、`backend/internal/repository/codex_model_visibility_repo.go`、`backend/internal/handler/codex_third_party_catalog.go`（目录接点、分组默认模型名单、Composite 目录入口）、`backend/internal/handler/admin/codex_third_party_models_handler.go`、`backend/internal/server/routes/admin_codex_third_party_models.go`、`frontend/src/components/admin/CodexModelVisibilityPanel.vue`、`frontend/src/api/admin/codexThirdPartyModels.ts`、`frontend/src/i18n/locales/{zh,en}/admin/codexThirdPartyModels.ts`。专项测试：`backend/internal/service/codex_third_party_*_test.go`（含 `codex_third_party_candidates_fix_test.go`、`codex_third_party_composite_base_test.go`、`codex_third_party_official_profiles_test.go`）、`backend/internal/handler/codex_third_party_*_test.go`（含 `codex_third_party_composite_test.go`、`codex_third_party_group_models_test.go`）、`frontend/src/components/admin/__tests__/CodexModelVisibilityPanel.spec.ts` 与 `frontend/src/views/admin/__tests__/ApiKeyMimicSettingsView.codexTab.spec.ts`。既有接点：`backend/internal/server/routes/gateway.go` 目录分派、`backend/internal/handler/openai_codex_models_handler.go`（调度循环之前 1 处 `writeCodexCompositeCatalog` 接点，加上写出与基础目录获取处的接点）、`GatewayHandler.CodexModels`、两个网关 handler 各一个依赖字段、`handler.go` 管理 handler 字段、三个 wire 文件与 `wire_gen.go`、`routes/admin.go` 路由挂载、两个 i18n 汇总文件、Plus 设置页标签（`ApiKeyMimicSettingsView.vue`）。 |
| 多协议平台与账号级候选池 | 新增后端（均在 `backend/internal/service/`，另有同名 `_test.go`）：`custom_platform.go`、`custom_platform_credentials.go`、`composite_account_claims.go`（改编自上游 PR #7114）、`composite_candidate_pool.go`、`composite_candidate_pool_config.go`、`composite_candidate_pool_eligibility.go`、`composite_candidate_pool_sticky.go`；`backend/internal/server/routes/composite_candidate_pool_entry.go` 及端到端测试 `composite_candidate_pool_e2e_test.go`。改动的既有后端：平台登记 `backend/internal/domain/platforms.go`、`backend/internal/domain/constants.go`、`service/domain_constants.go`；协议支持判定 `service/provider_profile.go`；模型列表聚合 `service/composite_model_protocol.go`；路由决策与上下文 `service/composite_model_route.go`、`service/composite_platform.go`；候选池挂钩 `service/composite_route_resolver.go`、`backend/internal/server/routes/gateway.go`、`backend/internal/handler/wire.go`；三处声明闸门 `service/openai_gateway_scheduling.go`、`service/openai_account_scheduler.go`、`service/gateway_scheduling.go`；管理路径校验 `service/admin_account.go`；Ollama 能力继承 `service/ollama_cloud_usage.go`、`backend/internal/repository/account_repo_ollama_cloud_usage.go`、`service/openai_gateway_ollama_cloud_cc_reasoning.go`；无状态归一化 `service/openai_gateway_request_body.go`。前端：`frontend/src/constants/platformCatalog.builtin.json`、`frontend/src/components/account/{CreateAccountModal.vue,EditAccountModal.vue,credentialsBuilder.ts}`、`frontend/src/components/common/PlatformIcon.vue`、`frontend/src/components/keys/UseKeyModal.vue`、`frontend/src/utils/{platformColors.ts,keyGroupProviders.ts}`、`frontend/src/types/index.ts`、`frontend/src/i18n/locales/{zh,en}/admin/{accounts,overview}.ts`。专项测试：`CompositeCandidatePool`、`CompositeAccountClaim`、`CustomPlatform`、`NormalizeCustomPlatformCredentials`、`AdminBulkUpdateCustomAccounts` 等 `_test.go`，以及上述前端文件的 `__tests__` 用例。 |
| Plus UI | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、账号 API、路由、侧边栏、i18n、账号创建/编辑和模型白名单相关组件。 |
| 发布部署 | `README.md`、`deploy/.env.example`、`deploy/docker-compose.local.yml`、`.github/workflows/release.yml`、`backend/cmd/server/VERSION`。 |

### 4.2 上游合并检查

合并上游后先按[共享框架 §5.2](docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#52-合并-sub2api-上游更新)建立独立 upstream changeset，并执行 `U-0～U-6`；README 不重复定义 OAuth 客户端字段级规则。

**OAuth 官方客户端出站仿真**

- Codex active wire、rollback wire、ingress matrix 与 Claude active wire、rollback wire、ingress matrix 六类客户端门禁必须全部通过；客户端专用范围分别以 [Codex CLI 手册](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md)和[Claude Code 手册](docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md)为准。
- 同时复核 `cross_persona`、`inventory_closure`、原业务回归、secret scan、共享完整回归和共享静态检查；任何未知入口、route 或 Sink 保持失败关闭。
- `ProductionIngressInventory`、`EgressDispositionInventory`、active／rollback Release、Runtime Selector 和最终 wire 必须绑定同一候选源码树与收据，禁止入站版本、自动发现结果或账号字段取得生产画像选择权。
- 上游源码基线合并完成不等于候选已交付或生产已更新；需要生产激活时继续执行共享框架 §5.6 的 canary、切换、真实回滚、恢复和收据流程。

**API Key mimic**
- **触发条件与身份头保护**：入站只对非官方客户端触发、内部入口不做识别、命中 mimic 时关键身份头不被账号 header override 覆盖——见 §1.3.1 第 4、7 条与入口表。
- **Anthropic 构造边界**：mimic 专用 beta 列表不影响普通 API Key；`/v1/messages` 与 `/v1/messages/count_tokens` 各自独立构造，1M beta 的适用模型前缀不得扩大——见 §1.3.1、§1.3.2。工具名归一和 per-request reverseMap 只修改结构化工具字段。
- **画像来源隔离**：Anthropic API Key `active` 的 Header/Body/TLS 画像只能来自 registry 的局部 Wire Profile，不得反向写入 `pkg/claude` 共享常量或 `defaultFingerprint`；共享常量随 OAuth 画像版本同步更新，两者取值可能相同但来源必须独立。管理员显式选择的 TLS profile 仍优先。
- **OpenAI 路由与 compact**：mimic 强制 HTTP，跳过 mimic 后账号调度、previous response 粘连复核和最终转发三处都恢复普通 WS/HTTP 路由；`/v1/messages` 固定走 Responses mimic；compact 保持上游 unary JSON 的白名单形态并按需桥接下游 SSE——见 §1.3.1 第 5、6 条与 §1.3.5。
- **Profile 指针**：默认与 `previous`/历史兼容 ID 的归属见 §1.3.2、§1.3.4；CLI 画像的 direct/proxy TLS 数据与管理员显式 profile 优先级见 §1.3.5。
- **压缩与 probe 分键**：`DisableCompression` 的驱动规则见 §1.3.5；Responses capability probe 继续按 mimic 状态分键。
- **Codex base instructions**：`CodexBaseInstructionsForModel()` 保持 §1.3.5 定义的四条分支，不得增删或改变回退顺序。

**三方客户端兼容**（判定标准见 §1.4）

- **范围与配置**：普通 API 兼容默认关闭，仅适用于 Anthropic API Key 账号；关闭时、OAuth 和其它平台保持原有行为。创建、编辑、批量更新和 Extra PATCH 均须维持与 Anthropic API Key mimic 的互斥校验，增量保存时不得覆盖其它 Extra 字段。
- **入口与顺序**：复核消息转发、消息透传、Token 计数及其透传、账号测试这五个调用点，均共用 `account_anthropic_standard_api_compat.go` 的处理逻辑，并在正文构造、账号请求头覆写及其它身份补全之后生效。上游拆分构造函数或新增出站路径时须同步接入，不能仅以 Git 合并无冲突作为验收依据。
- **内容与重试**：仅处理 §1.4 规定的 User-Agent、单项 beta 和 system 固定身份标识；保留业务 system、messages、tools、其它功能 beta、metadata、会话头及剩余块的缓存属性。正文修改后 `Body`、`ContentLength`、`GetBody` 保持一致，重试处理幂等，原始入站请求保持完整。
- **客户端身份演进**：Claude Code CLI、桌面端或 Agent SDK 升级后，核对真实请求中的固定身份句；出现新形态时先用受控请求确认差异，再更新集中规则和回归用例，避免扩大到任意 Claude 文本过滤。
- **回归与界面**：运行 `AnthropicStandardAPICompat` 专项及 Plus 设置页测试，覆盖开关关闭、互斥、各构造入口和 SDK 身份句；核对“三方客户端兼容”的筛选、状态及保存行为。上线验收使用真实客户端验证普通对话、流式和工具调用，确认用量归属目标账号且账号保持正常可调度。

**Codex 三方模型启用**（判定标准见 §1.7）

- **入口与分派**：复核 `routes/gateway.go` 中 `/v1/models?client_version=...` 与 `/backend-api/codex/models` 的平台分派、OpenAI 目录 handler 三条成功路径共用的写出调用与两处基础目录获取，以及 `GatewayHandler.CodexModels` 的接点。`openai_codex_models_handler.go` 在调度循环之前有 1 处 `writeCodexCompositeCatalog` 接点（Composite 分组不进入调度循环），调度失败分支原来的 Composite 空基础接点已撤掉；上游改动这些入口、拆分写出函数或新增目录成功路径时须同步接入，不能仅以 Git 合并无冲突作为验收依据。
- **原生 GPT 保真**：只有官方 OpenAI 账号的分组目录逐字节不变的回归用例必须通过；上游新增的目录字段默认保留，不得用本地字段白名单丢弃。
- **路由与协议复用**：原生判定与候选账号复用 Composite resolver、渠道映射、账号映射和协议选择；上游调整这些函数的签名或语义时，复核 `codex_third_party_routes.go` 的判定是否仍与实际调度一致。
- **候选名单**：候选名单来自 `GatewayHandler.Models`（OpenAI、Composite 分组）与 `codexModelIDsForGroup`（其它兼容平台和固定账号的 OpenAI 分组），上游改动这两处的名单生成或隐藏已映射模型规则时，复核 `codexThirdPartyGroupModelIDs` 仍与普通 `/v1/models` 一致。
- **Composite 本地生成**：上游若改动 `BuildGroupConfiguredCodexModelsManifest` 用到的辅助函数（`loadCodexGroupCatalogAccounts`、`openAIConfiguredCodexModelIDsForGroup`、`buildCodexModelsManifestForAccounts`、`mergeConfiguredCodexModelsManifest`）或其签名，须同步核对 `codex_third_party_composite_base.go` 的本地生成。
- **客户端契约**：Codex 升级后用对应版本源码核对 ModelInfo 必填字段、1 MiB 上限、5 秒超时、失败清空与刷新周期，再调整能力档案与测试。
- **能力档案**：厂商新增型号只改 `codex_third_party_profiles.json`；新协议或新字段语义先适配并补测试再开启，档案中的指令模板摘要随模板变化同步更新。DeepSeek 官方教程更新时，重新提取 models.json 替换 `codex_third_party_deepseek_official_models.json`（测试副本 `testdata/codex_deepseek_official_models.json` 同步替换），并更新两个档案里固定的指令模板摘要。
- **回归与界面**：运行 `CodexThirdParty`、`CodexModels`、`CodexComposite` 专项与现有 `Codex` 目录用例、Plus 面板测试；上线验收用真实桌面端核对选择器显示、流式、`apply_patch`、工具续接、推理档位与图片输入。

**多协议平台与账号级候选池**（判定标准见 §1.2）

- **三处接入**：`composite_route_resolver.go` 的 `Resolve` 里候选池挂钩必须位于显式路由之后、账号归属判定之前；`routes/gateway.go` 的 `compositeTargetPlatformMiddleware` 里入口标记 `withCompositeCandidatePoolEntry` 按精确路径映射四种文本入口，不得改成 `compositeRouteEndpointForPath` 的子串匹配（它会把 `alpha/search`、realtime、Live 也归到 responses）；`handler/wire.go` 的 `ProvideGatewayHandler` 里 `AttachCompositeCandidatePool` 接线保持在装配处理器处，不改 wire 依赖图。
- **三处声明闸门**：`openai_gateway_scheduling.go`、`openai_account_scheduler.go`、`gateway_scheduling.go` 统一调用 `CompositeAccountClaimAllowed`（上下文带候选集合按集合判定，无集合沿用精确判定）。上游调整这些闸门的位置、新增调度路径或新增第四处按模型声明过滤的判断时，必须同步接入，否则通配声明账号或零转换优先会在新路径上失效。
- **协议支持判定**：账号层 `providerSupportsProtocol` 对 `CredentialEndpoints` 的 profile 改看凭据 `api_base_urls`；`resolveUpstreamProtocol` 的自适应分支保持不改。上游改动这两处，或新增只看 profile 默认地址的协议判定时，核对 `custom` 是否仍按凭据判定。
- **模型列表聚合**：`accountSupportsCompositeModelListProtocol` 的 `default` 分支回落 `customPlatformSupportsModelListProtocol`；上游把该函数改为按平台硬编码时，不能让 `custom` 账号的模型从 Composite 模型列表消失。
- **管理路径校验**：创建（含复制与导入）、单个更新、批量更新三条写入路径都要经过 `NormalizeCustomPlatformCredentials` 或 `planCustomBulkBaseURLs`，按合并后的完整凭据校验并归一 `base_url`；上游新增账号写入入口（新的批量、克隆、导入）时同步接入，否则缺 Chat Completions 地址的 `custom` 账号会回退到 OpenAI 官方地址。
- **Ollama 白名单与钩子**：`isOllamaCloudUsagePlatform` 与仓储 SQL 白名单 `ollamaCloudUsagePlatformsSQL` 两处必须同步包含 `custom`；Chat Completions 推理字段归一化与 token 上限夹紧按实际出站地址判定，上游改回按平台判定时需复核。
- **无状态归一化**：`normalizeDeepSeekResponsesRequestBody` 对 `custom` 与国产原生 Responses 强制 `store=false`，删除 `previous_response_id` 与 `conversation`；该文件在冻结承接台账内，改动需补承接收据。
- **复刻与薄适配**：候选池的 Messages 入口粘性哈希复刻了处理器 `resolveOpenAIMessagesMetadataSession` 的规则，处理器调整时同步 `openAIFamilySessionHash`；资格过滤薄适配直接调用 `openAICompatibleAccountEligibilityFailureReason`、`isAccountSchedulableForSelection` 等现有函数，上游改签名或新增资格项时核对 `composite_candidate_pool_eligibility.go`。
- **上游迁移评估**：上游合入 PR #7114 时，先核对声明强度、候选集合、粘性与续链、失败和配额边界，通过 §1.2 所述行为的回归测试后，再逐步替换本地候选池与挂钩；`custom` 平台、零转换策略及仍需适配的本地规则独立保留。上游合入 issue #8043 一类的平台级方案时，保留本地账号级候选池，评估组合方式与兼容性。上游尚未合入时，每次合并复核以上各项并运行相关语义测试。`composite_account_claims.go` 头部注释记录了来源提交与本地差异，需保留版权与 LGPL-3.0 许可信息。
- **回归**：运行 `CompositeCandidatePool`、`CompositeAccountClaim`、`CustomPlatform`、`NormalizeCustomPlatformCredentials`、`AdminBulkUpdateCustomAccounts` 专项，以及上游既有的 Composite 闸门与调度测试（不得为迁就候选池而修改）。

**Plus 界面与发布**

- Plus 路由、侧边栏、i18n、账号 API 和设置页与后端保持一致，页面保留“API Key 官方客户端兼容”“三方客户端兼容”“Codex 三方模型启用”三个 Tab。
- Release / Docker 继续发布 `ghcr.io/itv3/sub2apiplus`，应用更新只替换 app 容器。

重点文件以第 4.1 节清单为准。

### 4.3 Mimic 对齐原则

继续提升官方客户端一致性时，不直接盲改源码，按下面顺序处理：
1. 采集真实官方客户端请求。
2. 采集经 sub2apiplus 的伪装请求。
3. 建差异表。
4. 用失败请求做 A/B 消融重放，每次只改一个变量或一个可解释变量组。
5. 只对高置信差异改源码。
6. 每改一项都做官方客户端和第三方客户端双向回归。

同时必须遵守的边界：

- 画像统一由 Registry 按“平台 × 认证 × 端点 × 传输”维护独立 Wire Profile，禁止重新引入账号级版本/Profile 覆盖或 `active`/`previous` 字段混用。
- UI 不得展示密钥、token、authorization 或 `x-api-key`；高风险 body cloaking 开关默认关闭或仅灰度启用。

## 5. 其它文档索引

README 只保留当前契约和操作入口；详细设计、抓包步骤与运行证据分别维护在以下文档：
官方 OAuth 客户端仿真的人类可读权威规范仅有 Framework、Codex CLI 和 Claude Code 三份；工具
README、机器生成证据索引及 JSON 收据只承担操作或审计责任，不构成第四份规范。

| 文档 | 说明 |
|---|---|
| [`tools/official_client_capture/README.md`](tools/official_client_capture/README.md) | 官方客户端出站工具索引；Codex 具有受管编排器，Claude 当前仅有取证与门禁工具。 |
| [`backend/internal/service/testdata/official_egress/README.md`](backend/internal/service/testdata/official_egress/README.md) | OAuth、API Key、Kilo、AnyRouter 和 Vircs 的脱敏实证索引。 |
| [`docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md) | 官方 OAuth 客户端共用的目标、扩展架构、证据生命周期与发布门槛。 |
| [`docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md) | Codex CLI 客户端规则、Sub2API 仿真实现和版本演进规范；当前 active 基线为 0.149.1。 |
| [`docs/EVIDENCE_INDEX.md`](docs/EVIDENCE_INDEX.md) | Codex 官方规则编号与证据文件的机器生成索引。 |
| [`docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) | Claude Code 规则与证据、环境职责、Sub2API 实现和版本演进规范。 |
| [`docs/COMPOSITE_GROUPS.md`](docs/COMPOSITE_GROUPS.md) | Composite Groups 路由、管理流程和使用边界。 |
| [`deploy/APPLE_CONTAINER.md`](deploy/APPLE_CONTAINER.md) | Apple silicon Mac 的原生容器部署、升级、备份和限制说明。 |
| [`deploy/EDGE_SECURITY.md`](deploy/EDGE_SECURITY.md) | CDN、反向代理可信链和真实客户端 IP 的安全配置说明。 |
| [`docs/PAYMENT_CN.md`](docs/PAYMENT_CN.md) | 支付能力中文说明。 |
| [`docs/PAYMENT.md`](docs/PAYMENT.md) | 支付能力英文说明。 |
| [`docs/ADMIN_PAYMENT_INTEGRATION_API.md`](docs/ADMIN_PAYMENT_INTEGRATION_API.md) | 管理端支付集成 API。 |
| [`docs/BATCH_IMAGE_MVP.md`](docs/BATCH_IMAGE_MVP.md) | 批量生图 MVP（上游能力，非 Plus 自研增强）。 |
| [`docs/ASYNC_IMAGE_TASKS.md`](docs/ASYNC_IMAGE_TASKS.md) | 异步图像生成与编辑任务 API（上游能力）。 |
| [`docs/legal/admin-compliance.zh.md`](docs/legal/admin-compliance.zh.md) | 管理端合规说明（中文）。 |
| [`docs/legal/admin-compliance.en.md`](docs/legal/admin-compliance.en.md) | 管理端合规说明（英文）。 |

## 6. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
