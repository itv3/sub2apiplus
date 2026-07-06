# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的个人自用增强版 fork。目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 当前状态

| 项 | 结论 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| Go module | 继续保留 `github.com/Wei-Shaw/sub2api` |

维护原则：

1. 长期跟随上游 `upstream/main` 或 release tag。
2. 对外名称使用 `sub2apiplus` / `Sub2API Plus`，内部 Go module 和 import 尽量保留上游命名，降低合并成本。
3. Plus 增强功能通过统一入口管理，账号级开关优先放入 `account.Extra`，自定义逻辑尽量放在独立 helper 文件或外置 sidecar。
4. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、Nginx、`.env` 和数据目录。

---

## Plus 增强功能入口

管理后台侧边栏将原“API Key 账号伪装”入口统一调整为“Plus 增强功能”。该入口作为 Sub2API Plus 自定义能力的聚合页，内部按功能分组：

| 功能 | 定位 | 配置归属 |
|---|---|---|
| API Key 账号伪装 | 让第三方客户端经 sub2apiplus 转发时尽量接近官方 Claude / Codex 客户端请求形态。 | `sub2apiplus` 管理后台和账号 `account.Extra`。 |
| 账号保活 | 通过官方 `codex` / `claude` 客户端对空闲账号做低频真实项目提问，保持上游账号活跃。 | 业务配置在 `sub2apiplus`，执行器运行在可选 keeper sidecar。 |

统一入口只负责业务配置、状态展示和历史查看；涉及官方客户端进程、worker 目录、项目目录和会话文件的运行时能力由 keeper sidecar 承担，避免膨胀主服务和主镜像。

---

## 1. API Key 账号伪装增强

### 1.1 功能总览

目标：让 Kilo / Cline / Cursor / Roo Code 等非官方客户端经 sub2apiplus 转发到第三方中转站 API 时，尽量接近“Claude / Codex 官方客户端直连该中转站 API”的 header、body、TLS 和路由形态。

核心规则：

1. 对 Anthropic / OpenAI 平台接入的 API Key 账号，通过账号级 `account.Extra` 开关启用 mimic 伪装增强。
2. mimic 与 passthrough 运行时互斥；如果透传与伪装同时开启，当前代码优先走 mimic，透传分支不会命中。
3. 测试连接也走官方客户端请求形态：OpenAI 明确复用 Codex mimic profile；Anthropic 默认构造 Claude Code 风格请求。

| 平台 | mimic 目标 | 已实现增强 |
|---|---|---|
| Anthropic API Key | Claude Code | Claude Code header、beta、TLS、body 规范化、`metadata.user_id`、工具名归一和响应侧回写。 |
| OpenAI API Key | Codex | Codex profile header、Responses 路由收敛、body 规范化、`prompt_cache_key` / `client_metadata`、TLS profile、mimic / 非 mimic probe 分键。 |

### 1.2 账号开关

`account.Extra` 当前开关：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "openai_apikey_mimic_codex_cli": true,
  "openai_apikey_mimic_codex_profile": "desktop_0_142",
  "enable_tls_fingerprint": true
}
```

规则：

- 仅 API Key 账号生效，不改变 OAuth 账号逻辑。
- 与 `anthropic_passthrough` / `openai_passthrough` 运行时互斥；同开时 mimic 优先。
- `enable_tls_fingerprint` 仍是独立开关，mimic 只放开账号类型资格。
- `openai_apikey_mimic_codex_profile` 缺省为 `desktop_0_142`，可设 `cli_rs_0_125` 回滚到旧 Codex CLI 形态；非法值回退 `desktop_0_142`。

### 1.3 Anthropic / Claude Code

Anthropic API Key mimic 走独立 builder，独立处理 `/v1/messages` 和 `count_tokens`。

| 范围 | 当前行为 |
|---|---|
| 路由与认证 | `Forward()` / `ForwardCountTokens()` 中 passthrough 与 mimic 互斥，`/v1/messages` 和 `count_tokens` 走独立 builder；跳过客户端 header 白名单透传，出站认证保持 `x-api-key`。 |
| Header / beta / TLS | 复用 Claude Code 官方 header helper；不注入 `oauth-2025-04-20`；`count_tokens` 补 `token-counting-2024-11-01`；`claude-opus-4-6/4-7/4-8` 保留 `context-1m-2025-08-07`；billing block 不再携带 `cch`；允许 TLS fingerprint profile。 |
| Body | 对第三方客户端请求做 Claude Code body 形态处理；在系统提示注入开启且非 haiku 时重写 `system`；原客户端 `system` 会迁移到 messages 前置 user/assistant 对；补 `metadata.user_id`、`temperature`、`max_tokens`、`tools`、cache breakpoint。 |
| 工具名 | 按 CLIProxyAPI `oauthToolRenameMap` 做工具名归一，覆盖 `todowrite -> TodoWrite`；同步改写 `tools[].name`、`tool_choice.name`、历史 `tool_use.name`、`tool_reference.tool_name`；响应侧用 per-request reverseMap 结构化回写；`count_tokens` 只同步工具名。 |
| 测试连接 | `AccountTestService` 的 Anthropic 测试连接默认构造 Claude Code 风格请求体/header，并通过 `DoWithTLS` 发送；它不是按 mimic 开关分支，但测试请求形态与 Claude Code mimic 目标一致。 |

关键文件：

`backend/internal/service/account_apikey_mimicry.go`、`gateway_apikey_mimicry.go`、`gateway_tool_rewrite.go`、`gateway_service.go`、`account.go`。

### 1.4 OpenAI / Codex

OpenAI API Key mimic 由 `openai_apikey_mimic_codex_profile` 驱动，profile 同时影响 header、body 默认值、`prompt_cache_key`、探测请求和 TLS profile。

| 范围 | 当前行为 |
|---|---|
| 路由与发送 | `openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 运行时互斥；入口条件是 `account.IsOpenAIPassthroughEnabled() && !accountMimicCodexCLI`，因此同开时 mimic 优先。账号级 mimic 参与 `isCodexCLI`；`/v1/responses` 直接走 Codex mimic；`/v1/chat/completions` 先转 Responses 再进入同一套 body/header/TLS。 |
| 传输与探测 | mimic 主 HTTP 上游请求走 `DoWithTLS`，TLS profile 为 nil 时兼容回退；Responses capability probe 按 mimic / 非 mimic 分键持久化，避免污染普通 API Key 路由判定。 |
| 测试连接 | `AccountTestService` 会 resolve 同一个 Codex mimic profile；mimic 开启时先 `RewriteBody()`，再 `ApplyHeaders()`，最后通过 `doOpenAIHTTPUpstream()` 发送，因此账号自测会带上 Codex header/body/TLS 行为。 |
| fallback 约束 | mimic 账号不能被普通 `force_chat_completions` 或 negative probe 带到 raw Chat Completions fallback；所有入口统一收敛到 Responses 伪装链路。 |
| Header profile | `desktop_0_142` 默认模拟 Codex Desktop，移除 `OpenAI-Beta` / `version` 并补齐 Desktop session/window metadata；`cli_rs_0_125` 使用旧 Codex CLI header 形态回滚。 |

Codex mimic body 统一补齐或规范化：

- `instructions`：缺失时按模型补真实 Codex CLI base instructions。
- `stream: true`、`store: false`、`include: ["reasoning.encrypted_content"]`。
- `prompt_cache_key`：默认 Desktop profile 对齐为与 `session-id` 同源的 UUID；显式 `cli_rs_0_125` profile 保留旧 `codex-mimic-*` 不透明 key，且不覆盖客户端已有非空值。
- 默认 Desktop profile 额外注入与 header 同源的 `client_metadata`：`x-codex-installation-id`、`session_id`、`thread_id`、`turn_id`、`x-codex-window-id`、`x-codex-turn-metadata`；仅补缺，不覆盖已有非空值。
- 裸 `role/content` message 规范化为 Codex / Responses 风格；`role:"system"` 统一改为 `role:"developer"`，避免 Responses `input` 里的 `system` role 触发 400。
- 两种 profile 都删除客户端透传的 `session_id` / `conversation_id` 下划线形式。

Codex base instructions：含 `codex` 的模型使用 GPT-5-Codex prompt；`gpt-5.5` / `gpt-5.2` 非 codex 分别使用对应 prompt；其余 `gpt-5.x` 非 codex 宽匹配回落 GPT-5.1 prompt；其它模型回退 GPT-5-Codex prompt。

关键文件：

`backend/internal/service/account_apikey_mimicry.go`、`apikey_mimic_identity.go`、`openai_apikey_mimicry.go`、`openai_apikey_mimic_profile.go`、`openai_gateway_chat_completions.go`、`openai_gateway_service.go`、`openai_codex_transform.go`、`openai_apikey_responses_probe.go`、`openai_upstream_http.go`、`account_test_service.go`、`account.go`、`backend/internal/pkg/openai/constants.go`。

### 1.5 UI 配置入口

管理后台通过“Plus 增强功能”统一入口进入，内部保留“API Key 账号伪装”分组。旧的独立入口 `/admin/settings/api-key-mimic` 后续应收敛到统一入口或保留兼容跳转。

页面只展示 `platform in (anthropic, openai)` 且 `type = apikey` 的账号，表格字段为：

| 字段 | 含义 |
|---|---|
| 账号 | 账号名称和 ID。 |
| 平台类型 | Anthropic / OpenAI。 |
| 官方客户端兼容 | 单一开关，用于启用或关闭该账号的 mimic 伪装增强。 |
| 当前 profile / 状态 | 未开启、Claude Code、Codex Desktop、旧 Codex CLI；如透传也开启，会提示运行时优先 mimic。 |

开关写入规则：

- Anthropic 开启：写入 `anthropic_apikey_mimic_claude_code = true` 和 `enable_tls_fingerprint = true`；关闭时写为 `false`。
- OpenAI 开启：写入 `openai_apikey_mimic_codex_cli = true`、`openai_apikey_mimic_codex_profile = "desktop_0_142"` 和 `enable_tls_fingerprint = true`；关闭时 mimic / TLS 写为 `false`，profile 保留为当前值。
- 前端调用 `PATCH /api/v1/admin/accounts/:id/extra`，后端使用已有 `UpdateAccountExtra` 做 JSONB key 级合并，不新增 DB 字段，也不通过通用账号更新接口全量覆盖 `extra`。

关键文件：

`frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、`frontend/src/api/admin/accounts.ts`、`frontend/src/router/index.ts`、`frontend/src/components/layout/AppSidebar.vue`、`backend/internal/handler/admin/account_handler.go`、`backend/internal/server/routes/admin.go`。

### 1.6 当前边界

- 不承诺 100% 复制官方客户端；服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 上下文和 HTTP/2 wire 指纹都可能超出 sub2apiplus 能力范围。
- mimic 主要伪装 header、body、TLS 和路由形态；不做响应侧文本替换，也不清洗原客户端在正文、工具描述、路径或能力提示里的身份信息。
- Anthropic 阶段一只做已知工具名归一；真实 Claude Desktop 与 Claude Code CLI 的 body 形态可能不同，后续需要独立 profile 化。
- OpenAI/Codex profile 的具体 UA、version、beta features 以代码常量为准；Codex Desktop 真机样本曾记录为 `HTTP/1.1 + 无 ALPN`，版本变化时应重抓确认。
- ARM64 usage 页面里的 `USER-AGENT` 是客户端入口 UA，不等价于发往上游的伪装 UA；判断 mimic 效果应比较第三方中转站看到的上游入站请求形态。

---

## 2. Antigravity 增强

目标：Antigravity 账号新增后默认可用；白名单、映射、`/models` 和实际发包都统一到官方抓包确认的 8 个模型。

### 2.1 默认模型口径

| 外部显示模型 | 官方发包 model | 固定 `thinkingBudget` |
|---|---|---:|
| Claude Opus 4.6 Thinking | `claude-opus-4-6-thinking` | 1024 |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 1024 |
| GPT-OSS 120B Medium | `gpt-oss-120b-medium` | 8192 |
| Gemini 3.1 Pro High | `gemini-pro-agent` | 10001 |
| Gemini 3.1 Pro Low | `gemini-3.1-pro-low` | 1001 |
| Gemini 3.5 Flash High | `gemini-3-flash-agent` | 10000 |
| Gemini 3.5 Flash Low | `gemini-3.5-flash-extra-low` | 1000 |
| Gemini 3.5 Flash Medium | `gemini-3.5-flash-low` | 4000 |

规则:

1. 模型白名单保存官方发包 model。
2. 模型映射保存“外部显示模型 -> 官方发包 model”。
3. 外部 `/antigravity/v1/models` 默认只展示界面模型,不展示兼容别名。
4. `gemini-3.1-pro-high` 这类历史兼容别名可继续接收请求,但不进入默认白名单和 `/models`。

### 2.2 已实现功能

| 功能 | 当前行为 |
|---|---|
| 账号编辑 | Antigravity 与 OpenAI / Anthropic 一样支持“模型白名单 / 模型映射”。 |
| 模型收敛 | 新建账号、同步按钮、上游同步和 `/models` 都统一到官方 8 模型:白名单保存官方发包 model,映射保存显示名到发包 model,对外只返回 8 个显示模型。 |
| 官方伪装 | UA 使用 `antigravity/hub/2.2.1 darwin/arm64`;默认按官方 fixed `thinkingBudget` 发送,补 `labels/model_enum/trajectory_id` 和同源 `requestId`,并过滤无关 stop/sampling 参数。 |

### 2.3 成本口径

已确认 `gpt-oss-120b-medium` 成本:

| 模型 | 输入 / 1M | 缓存输入 / 1M | 输出 / 1M |
|---|---:|---:|---:|
| `gpt-oss-120b-medium` | `$0.05` | `$0.01` | `$0.20` |

Google 侧 Antigravity 模型还需要单独统一定价口径,只处理 `gemini-*` / `gemini-pro-agent` 这一组,不牵连 Claude。

### 2.4 关键文件

| 范围 | 文件 |
|---|---|
| 官方模型定义 | `backend/internal/pkg/antigravity/claude_types.go` |
| UA / OAuth | `backend/internal/pkg/antigravity/oauth.go` |
| 请求体 / fixed budget / labels | `backend/internal/pkg/antigravity/gemini_types.go`、`backend/internal/pkg/antigravity/request_transformer.go` |
| 请求体测试 | `backend/internal/pkg/antigravity/request_transformer_test.go` |
| 上游模型同步过滤 | `backend/internal/service/upstream_models.go` |
| `/models` 折叠和展示 | `backend/internal/service/gateway_service.go`、`backend/internal/handler/gateway_handler.go` |
| 管理后台账号模型 | `backend/internal/handler/admin/account_handler.go` |
| 前端白名单/映射默认值 | `frontend/src/composables/useModelWhitelist.ts` |
| 创建账号默认配置 | `frontend/src/components/account/CreateAccountModal.vue` |
| 编辑账号白名单/映射 | `frontend/src/components/account/EditAccountModal.vue`、`ModelWhitelistSelector.vue` |
| 成本 | `backend/resources/model-pricing/model_prices_and_context_window.json`、`backend/internal/service/billing_service.go` |

---

## 3. 账号保活

目标：把已验证的 keeper 保活能力作为 Plus 增强功能接入 sub2apiplus。管理员无需在 keeper 中重复配置账号 `base_url`、`api_key` 和模型成本，只需要在 sub2apiplus 中引用现有 API 账号并开启保活。

### 3.1 功能总览

账号保活用于处理部分上游中转账号长时间不使用后连接不稳定的问题。保活任务通过官方客户端在真实项目目录中低频提问，让账号保持活跃。

核心规则：

1. 保活配置、账号引用、成本口径、最近使用时间和历史展示都归 sub2apiplus 管理。
2. keeper 作为可选 sidecar，只负责运行 `codex` / `claude` 官方客户端、维护 worker 目录、会话文件和执行日志。
3. keeper 不再单独保存业务账号的 `base_url`、`api_key` 和 pricing；这些信息由 sub2apiplus 通过内部接口提供或代理执行。
4. 保活触发应基于账号真实最近使用时间；账号一直有正常请求时，不额外发起保活请求。

### 3.2 配置归属

业务配置放在 sub2apiplus 的“Plus 增强功能 / 账号保活”分组中：

| 配置 | 归属 | 说明 |
|---|---|---|
| 启用保活 | sub2apiplus | 账号级开关，建议写入 `account.Extra`。 |
| 保活间隔 | sub2apiplus | 例如 90 分钟；用于计算空闲多久后才需要保活。 |
| 工作时间 | sub2apiplus | 限制保活任务允许执行的时间段。 |
| 执行器 | sub2apiplus | OpenAI 使用 `codex`，Anthropic 使用 `claude`。 |
| 模型 | sub2apiplus | 从账号可用模型或默认模型中选择。 |
| 工作目录 | sub2apiplus | 展示 keeper 可用项目目录；实际目录由 keeper 挂载。 |
| prompt 约束和题库 | sub2apiplus | 包括全局只读约束、通用问题、项目问题和账号自定义 prompt。 |
| base_url / api_key | 现有账号 | 不在保活页重复配置，直接引用 API 账号已有配置。 |
| 模型成本 | sub2apiplus | 复用现有成本和 usage 统计口径。 |

keeper 只保留运行配置：

```yaml
sub2apiplus_url: http://sub2api:8080
internal_token: ${KEEPER_INTERNAL_TOKEN}
runtime_root: /app
projects_root: /workspace/projects
codex_path: codex
claude_path: claude
```

### 3.3 空闲触发策略

账号保活不应按固定时间无脑执行，而应与 sub2apiplus 现有调度和账号使用记录联动。

判断规则：

1. sub2apiplus 维护每个账号的最近真实使用时间 `last_used_at`，以及最近保活时间 `last_keepalive_at`。
2. keeper 准备执行前向 sub2apiplus 请求待保活账号。
3. sub2apiplus 判断 `max(last_used_at, last_keepalive_at) + interval_minutes` 是否已到期。
4. 如果账号最近真实使用时间距离现在小于保活间隔，则不执行保活，并把下一次时间重置为 `last_used_at + interval_minutes`。
5. 只有账号空闲超过保活间隔时，才允许 keeper 发起官方客户端请求。

预期效果：

- 如果账号一直在用，就一直不会产生额外保活请求。
- 只有账号空闲超过配置间隔时，才发起保活，最大限度节约 token 和成本。
- 手动“立即执行”仍应保留，但需要在 UI 中标明它会忽略空闲节流。

### 3.4 接口边界

sub2apiplus 建议提供内部接口给 keeper 使用：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活的账号、模型、prompt、项目目录、最近使用时间、下一次时间和 due 判断。 |
| `GET /api/v1/internal/keeper/accounts/:id/models` | 返回该账号可用于保活的模型列表。 |
| `POST /api/v1/internal/keeper/accounts/:id/keepalive` | keeper 回写执行状态、会话摘要、token、费用和错误信息。 |

内部接口复用现有管理员 API Key 鉴权，keeper 通过 `x-api-key: <admin-api-key>` 调用。接口不能暴露账号密钥、`Authorization`、`x-api-key` 或未脱敏日志。

### 3.5 UI 规划

“Plus 增强功能 / 账号保活”应包含三个视图：

| 视图 | 内容 |
|---|---|
| 概览 | 已启用账号数、运行中账号、今日成功 / 失败、最近结果、最近真实使用时间、下次保活时间。 |
| 配置 | 从现有 API 账号中开启保活，配置间隔、工作时间、模型、执行器、工作目录、prompt 和题库。 |
| 会话历史 | 展示每次保活的时间、账号、状态、模型、token、费用、结果摘要、错误和配置快照。 |

配置页不得出现独立的 `base_url`、`api_key` 或模型成本输入框；这些信息必须引用 sub2apiplus 现有账号和成本体系。

### 3.6 当前边界

- 不把 keeper 完整运行时编进 sub2apiplus 主进程。
- 不把 `codex` / `claude` 官方客户端安装进 sub2apiplus 主镜像。
- 不新增一套独立账号、独立密钥和独立成本配置。
- keeper sidecar 的 `workers`、`projects`、`data` 必须持久化，容器重建不能丢失会话和项目目录。
- 账号保活属于可选增强功能；keeper 不可用时，主网关转发能力应保持可用。

---

## 4. 发布和 ARM64 更新

发布前测试：

```sh
cd /Users/czs/Developer/sub2apiplus/backend
go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service

cd /Users/czs/Developer/sub2apiplus
make test-frontend
```

大范围合并或共享逻辑改动时扩大为：

```sh
cd /Users/czs/Developer/sub2apiplus/backend
go test ./...

cd /Users/czs/Developer/sub2apiplus
make test-frontend
```

版本规则：

| 项 | 规则 | 示例 |
|---|---|---|
| Plus 版本 | 上游版本后追加自定义序号 | `0.1.144-1` |
| Git tag | `v<Plus 版本>` | `v0.1.144-1` |
| GHCR 镜像 | `ghcr.io/itv3/sub2apiplus:<Plus 版本>` | `ghcr.io/itv3/sub2apiplus:0.1.144-1` |

发布：

```sh
cd /Users/czs/Developer/sub2apiplus
VERSION=0.1.145-1
echo "$VERSION" > backend/cmd/server/VERSION
git push origin main
git tag "v${VERSION}"
git push origin "v${VERSION}"
```

如果 tag push 没触发 Release：

```sh
gh workflow run Release --repo itv3/sub2apiplus --ref main -f tag="v${VERSION}" -f simple_release=false
```

ARM64 只更新应用容器：

```sh
ssh ARM64
cd /root/docker/sub2apiplus/app
docker compose pull sub2api
docker compose up -d --no-deps sub2api
docker compose ps
docker compose logs --tail=100 sub2api
curl -fsS http://127.0.0.1:8080/health
```

不要执行 `docker compose down -v`，不要删除 volume，不要覆盖 `.env`。

---

## 5. 上游合并检查

合并上游后重点确认：

- Anthropic mimic 与 passthrough 仍互斥，`/v1/messages` 和 `/v1/messages/count_tokens` 仍走独立 mimic builder。
- Anthropic 工具名归一和 per-request reverseMap 仍只改结构化工具字段。
- OpenAI API Key mimic 仍参与 `isCodexCLI` 和 Responses 路由判定。
- `desktop_0_142` 仍是默认 Codex profile，TLS fingerprint 未显式绑定时不回退到 Node.js profile。
- Responses capability probe 仍按 mimic / 非 mimic 分键保存。
- `CodexBaseInstructionsForModel()` 的 `gpt-5.5` / `gpt-5.2` / 其余 `gpt-5*` 映射仍符合当前策略。
- Antigravity 账号编辑、默认白名单/映射、同步上游和 `/models` 仍统一到官方 8 模型,不出现 16 个重复模型。
- Antigravity 官方伪装仍保留官方 UA、fixed `thinkingBudget`、`labels.model_enum`、`labels.trajectory_id`、同源 `requestId`,并过滤无关 stop/sampling 参数。
- Antigravity 成本仍保留 `gpt-oss-120b-medium` 的 `$0.05 / $0.01 / $0.20` 每 1M tokens。
- Plus 增强功能入口仍包含 API Key 账号伪装和账号保活，账号保活业务配置仍由 sub2apiplus 管理，keeper 仍只作为可选 sidecar 执行器。
- Release / Docker 仍发布并部署 `ghcr.io/itv3/sub2apiplus`，ARM64 更新只替换 app 容器。

重点文件：`gateway_service.go`、`gateway_apikey_mimicry.go`、`gateway_tool_rewrite.go`、`openai_gateway_service.go`、`openai_gateway_chat_completions.go`、`openai_apikey_mimic_profile.go`、`openai_codex_transform.go`、`openai_upstream_http.go`、`account.go`、`backend/internal/pkg/openai/constants.go`、`backend/internal/pkg/antigravity/request_transformer.go`、`backend/internal/pkg/antigravity/claude_types.go`、`backend/internal/pkg/antigravity/oauth.go`。

---

## 6. 后续路线

### 6.1 阶段二：body identity mimic

阶段二不建议直接盲改源码。正确顺序：

1. BWG 侧采集真实官方客户端请求。
2. BWG 侧采集 ARM64 伪装请求。
3. 用失败请求做 A/B 消融重放，每次只改一个变量或一个可解释变量组。
4. 建差异表。
5. 只对高置信差异改源码。
6. 每改一项都用 Kilo + 官方客户端双向回归。

Anthropic 阶段二优先级：

1. 已完成：参考 CLIProxyAPI 的 `oauthToolRenameMap`，做 Anthropic/Kilo 工具名归一 + 结构化响应回写。
2. 待做：用 Kilo 真实请求复测 `claude-opus-4-8`。
3. 待做：再评估是否移植 `sanitizeForwardedSystemPrompt` 式身份清洗，必须独立开关、灰度启用。
4. 已记录：OpenAI/Codex 真机 JA4/ALPN 样本是 `HTTP/1.1 + 无 ALPN`；后续 TLS profile 绑定按该样本保守处理，不默认补 `h2`。
5. 后置：敏感词零宽混淆、per-key 设备画像钉扎。
6. Anthropic 链路先守住当前 h1/Node TLS 方案，除非真机抓包证明官方 Claude Code 已变化。

### 6.2 阶段三：UI / 配置产品化

阶段三目标是把已经验证稳定的 mimic 能力产品化到“Plus 增强功能”统一入口，减少直接改数据库或 `account.Extra` 的维护成本。

第一批，阶段一稳定开关：

- `anthropic_apikey_mimic_claude_code`
- `openai_apikey_mimic_codex_cli`
- `enable_tls_fingerprint`
- Anthropic / OpenAI API Key mimic 与 passthrough 互斥提示

第二批，阶段二灰度开关：

- `anthropic_apikey_mimic_official_identity`
- `anthropic_apikey_mimic_profile`

第三批，诊断能力：

- 显示当前账号实际出站 profile。
- 显示是否启用 TLS fingerprint。
- 显示最近一次 mimic 请求的脱敏诊断摘要。
- 提供“仅脱敏导出”的抓包辅助入口。

UI 中不得展示密钥、token、authorization、x-api-key。高风险 body cloaking 开关默认关闭或灰度。

---

## 7. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
