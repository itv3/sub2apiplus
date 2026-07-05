# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的个人自用增强版 fork。目标是长期跟随上游升级，同时保留自建镜像、私有部署和 API Key 客户端伪装等增强能力。

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
3. API Key 客户端伪装通过账号级 `account.Extra` 控制，自定义逻辑尽量放在独立 helper 文件。
4. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、Nginx、`.env` 和数据目录。

---

## 1. API Key 客户端伪装增强

### 1.1 目标与边界

阶段一目标是让 Kilo / Cline / Cursor / Roo Code 等非官方客户端，通过 API Key 账号接入官方客户端限制类上游时，能走统一的 Anthropic Claude Code mimic 或 OpenAI Codex mimic 路径。

| 目标 | 看哪里 | 当前状态 |
|---|---|---|
| 上游识别为官方客户端 | header、TLS、HTTP 发送路径、beta、UA、originator、Codex 必需 body 字段 | 阶段一已覆盖 Kilo/Cline、ARM64 自测和 BWG 下游探活。 |
| Anthropic 工具名指纹 | body 里的 `tools` / `tool_choice` / `tool_use` / `tool_reference` | 已按 CLIProxyAPI 映射表做请求改名和结构化响应回写，覆盖 `todowrite -> TodoWrite` |
| 模型自我认知为官方客户端 | body 里的 `system` / `instructions` / `metadata` / `tools` / 原客户端身份提示 | 暂不做统一身份替换，只保证 Codex base instructions 在缺失时补齐 |

边界：

- 不承诺 100% 复制官方客户端；服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 上下文和 HTTP/2 wire 指纹都可能超出 sub2api 能力范围。
- 模型通常看不到 HTTP `User-Agent`，只能从 body 里的 `instructions`、`input`、`tools` 推断客户端身份；sub2api 不做响应侧文本替换。
- Kilo 使用 Claude 失败的最小触发点已确认是小写工具名 `todowrite`，不是 `You are Kilo` 文本。
- OpenAI/Codex 侧保留 Cursor / Kilo / Cline / Roo Code 的身份文本、工具描述、工作区路径和客户端能力提示，避免破坏工具链。
- ARM64 usage 页面里的 `USER-AGENT` 是客户端入口 UA，不等价于 ARM64 发往上游/BWG 的伪装 UA；判断 mimic 效果应比较 BWG 侧官方客户端直连请求和 ARM64 出站请求。

### 1.2 账号开关

当前使用 `account.Extra` 承载开关：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "openai_apikey_mimic_codex_cli": true,
  "openai_apikey_mimic_codex_profile": "desktop_0_142",
  "enable_tls_fingerprint": true
}
```

规则：

- 仅 API Key 账号生效，不改变 OAuth 账号现有逻辑。
- 与 `anthropic_passthrough` / `openai_passthrough` 互斥，mimic 优先。
- `enable_tls_fingerprint` 仍是独立开关，mimic 只放开账号类型资格。
- `openai_apikey_mimic_codex_profile` 缺省为 `desktop_0_142`，可设 `cli_rs_0_125` 回滚到旧 Codex CLI 形态；非法值回退 `desktop_0_142`。

### 1.3 Anthropic / Claude Code

| 范围 | 当前行为 |
|---|---|
| 路由与认证 | `Forward()` / `ForwardCountTokens()` 中 passthrough 与 mimic 互斥，`/v1/messages` 和 `count_tokens` 走独立 builder；跳过客户端 header 白名单透传，出站认证保持 `x-api-key`。 |
| Header / beta / TLS | 复用 Claude Code 官方 header helper；不注入 `oauth-2025-04-20`；`count_tokens` 补 `token-counting-2024-11-01`；`claude-opus-4-6/4-7/4-8` 保留 `context-1m-2025-08-07`；billing block 不再携带 `cch`；允许 TLS fingerprint profile。 |
| Body | 对第三方客户端请求做 Claude Code body 形态处理；`system` 重写为 billing block + Claude Code prompt + expansion block；原客户端 `system` 迁移到 messages 前置 user/assistant 对；补 `metadata.user_id`、`temperature`、`max_tokens`、`tools`、cache breakpoint。 |
| 工具名 | 按 CLIProxyAPI `oauthToolRenameMap` 做工具名归一，覆盖 `todowrite -> TodoWrite`；同步改写 `tools[].name`、`tool_choice.name`、历史 `tool_use.name`、`tool_reference.tool_name`；响应侧用 per-request reverseMap 结构化回写；`count_tokens` 只同步工具名。 |

当前边界：

- 第三方客户端原始 system 中的“我是 Kilo / You are Kilo”类身份提示暂不统一删除或改写。
- 真实 Claude Desktop 与 Claude Code CLI 的 body 形态可能不同，后续需要 profile 化，不能混成一个 profile。
- 如果要做 `anthropic_apikey_mimic_official_identity`，必须独立开关、默认关闭或灰度，并用 Kilo + 官方 Claude Code 双向回归。

关键文件：

`backend/internal/service/account_apikey_mimicry.go`、`gateway_apikey_mimicry.go`、`gateway_tool_rewrite.go`、`gateway_service.go`、`account.go`。

### 1.4 OpenAI / Codex

| 范围 | 当前行为 |
|---|---|
| 路由与发送 | `openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 互斥；账号级 mimic 参与 `isCodexCLI`；`/v1/responses` 直接走 Codex mimic；`/v1/chat/completions` 先转 Responses 再进入同一套 body/header/TLS；账号自测和下游探活复用同一路径。 |
| 传输与探测 | mimic 主 HTTP 上游请求走 `DoWithTLS`，TLS profile 为 nil 时兼容回退；Responses capability probe 按 mimic / 非 mimic 分键持久化，避免污染普通 API Key 路由判定。 |

出站 Codex header 由 `openai_apikey_mimic_codex_profile` 驱动：

| profile | 用途 | header 形态 |
|---|---|---|
| `desktop_0_142` | 默认，实测 Codex Desktop | `user-agent: Codex Desktop/0.142.0 (Electron 38.2.2; macOS 15.6.1; arm64)`、`originator: Codex Desktop`、`x-codex-beta-features: responses=experimental`、`x-client-request-id`、`session-id`、`thread-id`、`x-codex-window-id`、`x-codex-turn-metadata`，并移除 `OpenAI-Beta`、`version` |
| `cli_rs_0_125` | 旧 Codex CLI 兼容/回滚 | `user-agent: codex_cli_rs/0.45.0 (Mac OS 15.6.1; arm64) Terminal`、`originator: codex_cli_rs`、`OpenAI-Beta: responses=experimental`、`version: 0.45.0` |

两种 profile 都删除客户端透传的 `session_id` / `conversation_id` 下划线形式。

Codex mimic body 统一补齐或规范化：

- `instructions`：缺失时按模型补真实 Codex CLI base instructions。
- `stream: true`：上游按 Codex 流式形态发送。
- `store: false`。
- `include: ["reasoning.encrypted_content"]`。
- `prompt_cache_key`：默认 Desktop profile 用基于稳定 seed 的 UUID 形态；显式 `cli_rs_0_125` profile 保留旧 `codex-mimic-*` 不透明 key。
- 默认 Desktop profile 额外注入与 header 同源的 `client_metadata`：`x-codex-installation-id`、`session_id`、`originator: codex_desktop`、`x-codex-window-id`、`x-codex-turn-metadata`；仅补缺，不覆盖已有值。
- 裸 `role/content` message 规范化为 Codex / Responses 风格：`{"role":"user","content":"..."}` 变为 `{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}`。
- `role:"system"` 统一改为 `role:"developer"`，避免 Codex 上游对 Responses `input` 中的 `system` role 返回 400。
- 如果客户端已有 `prompt_cache_key`，保持原值不覆盖。

Codex base instructions：含 `codex` 的模型使用 GPT-5-Codex prompt；`gpt-5.5` / `gpt-5.2` 非 codex 分别使用对应 prompt；其余 `gpt-5.x` 非 codex 宽匹配回落 GPT-5.1 prompt；其它模型回退 GPT-5-Codex prompt。

当前边界：

- OpenAI/Codex mimic 请求保留 Cursor / Kilo / Cline / Roo / OpenCode 身份文本。
- 工具名、工具 description、工作区路径、客户端能力提示保持原样，避免破坏客户端工具执行链。
- OpenAI/Codex 侧阶段一不引入 CLIProxyAPI 式 body identity cloaking。
- Codex Desktop 真机样本记录为 `HTTP/1.1 + 无 ALPN`，后续 TLS profile 不默认补 `h2`，版本变化时再重抓确认。

关键文件：

`backend/internal/service/account_apikey_mimicry.go`、`apikey_mimic_identity.go`、`openai_apikey_mimicry.go`、`openai_apikey_mimic_profile.go`、`openai_gateway_chat_completions.go`、`openai_gateway_service.go`、`openai_codex_transform.go`、`openai_apikey_responses_probe.go`、`openai_upstream_http.go`、`account_test_service.go`、`account.go`、`backend/internal/pkg/openai/constants.go`。

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

## 3. 发布和 ARM64 更新

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

## 4. 上游合并检查

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
- Release / Docker 仍发布并部署 `ghcr.io/itv3/sub2apiplus`，ARM64 更新只替换 app 容器。

重点文件：`gateway_service.go`、`gateway_apikey_mimicry.go`、`gateway_tool_rewrite.go`、`openai_gateway_service.go`、`openai_gateway_chat_completions.go`、`openai_apikey_mimic_profile.go`、`openai_codex_transform.go`、`openai_upstream_http.go`、`account.go`、`backend/internal/pkg/openai/constants.go`、`backend/internal/pkg/antigravity/request_transformer.go`、`backend/internal/pkg/antigravity/claude_types.go`、`backend/internal/pkg/antigravity/oauth.go`。

---

## 5. 后续路线

### 5.1 阶段二：body identity mimic

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

### 5.2 阶段三：UI / 配置产品化

阶段三目标是把已经验证稳定的 mimic 能力产品化到前端 UI，减少直接改数据库或 `account.Extra` 的维护成本。

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

## 6. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
