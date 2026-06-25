# sub2api：API Key 官方客户端伪装阶段一方案

更新时间：2026-06-25

> 目标：让通过 sub2api 转发的 API Key 账号，在上游和模型侧都尽量接近官方客户端请求形态。
>
> - Anthropic API Key：伪装成 Claude Code / Claude Desktop 系官方 API Key 请求。
> - OpenAI API Key：伪装成 Codex CLI / Codex Desktop 系官方 API Key 请求。
>
> 阶段一覆盖 `/v1/responses`、`/v1/chat/completions`、ARM64 账号自测、BWG 下游探活四条路径。OpenAI Codex mimic 的核心能力包括 header、TLS 发送路径、Codex instructions、stream/store/include、`prompt_cache_key`、裸 `role/content` message 规范化、`system -> developer`、Chat Completions 强制走 Responses 转换，以及 `gpt-5.5` base instructions 映射。

---

## 1. 当前结论

### 1.1 阶段一状态

阶段一已完成并部署到 ARM64 测试服。

当前代码分支：

- 本地仓库：`/Users/czs/Documents/sub2api`
- 分支：`mimic`
- 远端：`origin/mimic`

ARM64 测试服当前运行：

- 当前运行镜像：`sub2api:mimic-f18101f3-desktop-profile-arm64`
- 容器：`sub2api-mimic`
- 对外地址：`https://sg.3ab.in`
- Claude Base URL：`https://sg.3ab.in`
- Codex Base URL：`https://sg.3ab.in/v1`

### 1.2 两类“伪装”必须分开看

这里有两个不同目标，不能混为一谈：

| 目标 | 看哪里 | 当前状态 |
|---|---|---|
| 上游识别为官方客户端 | header、TLS、HTTP 发送路径、beta、UA、originator、Codex 必需 body 字段 | 阶段一已覆盖 Kilo/Cline、ARM64 自测和 BWG 下游探活 |
| 模型自我认知为官方客户端 | body 里的 `system` / `instructions` / `metadata` / `tools` / 原客户端身份提示 | 阶段一不做统一身份替换；只保证 Codex base instructions 在缺失时补齐 |

模型侧身份判断说明：

- Cline 回答自己在 Codex CLI，是因为请求缺少或没有强 Cline 身份上下文时，mimic 层会补 Codex CLI base `instructions`。
- Kilo 仍可能回答自己在 Kilo，是因为 Kilo 自己把 `Kilo-Code`、`Kilo developer instructions`、`kilo_local_recall` 等身份或工具线索放进请求正文。
- 模型通常看不到 HTTP `User-Agent`，只能根据 body 中可见的 `instructions`、`input`、`tools` 推断客户端身份；sub2api 不做响应侧文本替换。

### 1.3 阶段一边界

可以做：

- 注入真实 Claude Code / Claude Desktop 的 system prompt、billing block、metadata、tool 形态。
- 将第三方客户端工具名映射成官方工具名，回包时再映射回客户端能识别的名字。
- 对 OpenAI/Codex 侧保留或补齐必要 Codex body 兼容语义。
- 对比官方客户端真实请求后，按差异逐项收敛。

不能轻率承诺：

- 如果官方客户端还有服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 层上下文，单靠 sub2api 不能 100% 复制。
- 如果官方客户端和第三方客户端可用工具完全不同，强行映射可能影响工具调用稳定性。
- 如果上游或中转检查 HTTP/2 wire 指纹，Go `net/http` 默认传输层仍可能与官方客户端不同。
- OpenAI/Codex 侧保留 Cursor / Kilo / Cline / Roo Code 的真实客户端身份文本，避免影响工具调用、工作区路径、客户端能力提示和后续排障。

阶段一目标是：

> 让 Kilo / Cline / Cursor / Roo Code 等使用 `/v1/responses` 或 `/v1/chat/completions` 的非官方客户端，在官方客户端限制类 OpenAI API 上通过 ARM64 通用 Codex mimic 可用；同时让 ARM64 自测、BWG 下游探活、真实客户端请求都走同一套伪装后路径。

---

## 2. 阶段一实际实现

### 2.1 账号开关

阶段一使用 `account.Extra` 承载开关，暂不做前端 UI：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "openai_apikey_mimic_codex_cli": true,
  "enable_tls_fingerprint": true
}
```

规则：

- 仅 API Key 账号生效。
- 与 passthrough 互斥，mimic 优先。
- 不改变 OAuth 账号现有逻辑。
- `enable_tls_fingerprint` 仍是独立开关，mimic 只放开账号类型资格。

### 2.2 Anthropic 实现内容

主要文件：

- `backend/internal/service/account_apikey_mimicry.go`
- `backend/internal/service/gateway_apikey_mimicry.go`
- `backend/internal/service/gateway_service.go`
- `backend/internal/service/account.go`

已实现：

- `Forward()` 和 `ForwardCountTokens()` 中 passthrough 与 mimic 互斥。
- `/v1/messages` 通过独立 builder 构造 API Key mimic 请求。
- `/v1/messages/count_tokens` 通过独立 builder 构造 API Key mimic 请求。
- 跳过客户端 header 白名单透传，避免 Kilo/Cline 等 header 污染官方指纹。
- 出站认证保持 `x-api-key`，不伪装 OAuth。
- 复用 Claude Code 官方 header helper。
- API Key mimic 不注入 `oauth-2025-04-20`。
- `count_tokens` 补 `token-counting-2024-11-01`。
- `claude-opus-4-6` / `claude-opus-4-7` / `claude-opus-4-8` 自动保留 `context-1m-2025-08-07`。
- 对第三方客户端请求做 Claude Code body 形态处理。
- 重写 `system` 为 billing block + Claude Code prompt + expansion block。
- 原客户端 `system` 会迁移到 messages 前置 user/assistant 对，以减少高优先级污染。
- 补 `metadata.user_id`。
- 补齐 `temperature`、`max_tokens`、`tools`、cache breakpoint。
- 对 billing block 做 `signBillingHeaderCCH`。
- Anthropic API Key mimic 允许走 TLS fingerprint profile。

阶段二继续收敛项：

- 第三方客户端原始 system 中的“我是 Kilo / You are Kilo”类身份提示不能只迁移，必要时要删除或改写。
- 工具名如果带 `kilo_` / `cline_` / `roo_` 等命名，会让模型继续感知第三方客户端，需要官方工具名映射和回写。
- 真实 Claude Desktop 与 Claude Code CLI 的 body 形态可能不同，需要分 profile。

### 2.3 OpenAI / Codex 实现内容

主要文件：

- `backend/internal/service/account_apikey_mimicry.go`
- `backend/internal/service/apikey_mimic_identity.go`
- `backend/internal/service/openai_apikey_mimicry.go`
- `backend/internal/service/openai_gateway_chat_completions.go`
- `backend/internal/service/openai_gateway_service.go`
- `backend/internal/service/openai_codex_transform.go`
- `backend/internal/service/account_test_service.go`
- `backend/internal/pkg/openai/constants.go`
- `backend/internal/service/account.go`

已实现：

- `openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 互斥，mimic 优先。
- 开启账号级 mimic 时，内部 `isCodexCLI` 视为 true。
- 这样可以保留 Codex 相关 body 语义，避免 Kilo 请求被当作普通第三方请求清理。
- `/v1/responses` 入站走通用 Codex mimic body/header/TLS 处理。
- `/v1/chat/completions` 入站对 API Key Codex mimic 账号强制走 Chat Completions -> Responses 转换，再进入同一套 Codex mimic body/header/TLS 处理。
- ARM64 账号自测使用同一套 Codex mimic body/header/TLS 处理。
- BWG 下游探活通过 ARM64 API key 进入后，也会走 ARM64 的通用 `/v1/responses` 或 `/v1/chat/completions` mimic 路径。
- 出站 Codex header 由 profile 驱动，默认 `desktop_0_142`（实测 `Codex Desktop/0.142.0`）：
  - `user-agent: Codex Desktop/0.142.0 ...`
  - `originator: Codex Desktop`
  - 注入 `x-codex-*` / `session-id` / `thread-id`
  - 移除旧 `version` / `OpenAI-Beta` / 下划线 session header
  - 显式 `cli_rs_0_125` profile 保留旧 `codex_cli_rs` header 形态，作为兼容/回滚
- 删除客户端透传的 `session_id` / `conversation_id`。
- OpenAI API Key mimic 主 HTTP `/v1/responses` 和 `/v1/chat/completions` 转换后的上游请求都走 `DoWithTLS`。
- TLS profile 为 nil 时保持兼容回退。
- Codex mimic body 统一补齐或规范化：
  - `instructions`：缺失时按模型补真实 Codex CLI base instructions。
  - `stream: true`：上游按 Codex 流式形态发送。
  - `store: false`。
  - `include: ["reasoning.encrypted_content"]`。
  - `prompt_cache_key`：默认 Desktop profile 用稳定 UUID 形态并补齐与 header 同源的 `client_metadata`；显式 `cli_rs_0_125` profile 保留旧 `codex-mimic-*` 不透明 key。
- 裸 `role/content` message 规范化为 Codex / Responses 风格：
  - 原始形态：`{"role":"user","content":"..."}`
  - 规范形态：`{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}`
- `role:"system"` 统一改为 `role:"developer"`，避免 Codex 上游对 Responses `input` 中的 `system` role 返回 400。
- `gpt-5.5` 使用首行为 `You are GPT-5.5 running in the Codex CLI` 的 Codex prompt。
- 仅当请求缺少 `prompt_cache_key` 时自动补值。
- 如果客户端已有 `prompt_cache_key`，保持原值不覆盖。
- OpenAI/Codex mimic 请求保留 Cursor / Kilo / Cline / Roo / OpenCode 身份文本。
- 工具名、工具 description、工作区路径、客户端能力提示保持原样，避免破坏客户端工具执行链。

行为边界：

- Cline 回答 Codex、Kilo 回答 Kilo，属于请求正文里的身份线索差异；模型通常不会读取 HTTP `User-Agent`。
- OpenAI/Codex 侧阶段一不引入 CLIProxyAPI 式 body identity cloaking。

---

## 3. 验收状态

### 3.1 本地测试

已通过：

```bash
go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service
```

### 3.2 ARM64 / BWG 验收

已验证：

- `https://sg.3ab.in/` 仍保持 API-only，根路径不展示网站页面。
- `https://sg.3ab.in/v1` 可用于 API 调用。
- Claude 非流式和流式请求成功。
- OpenAI/Codex `/v1/responses` 非流式和流式请求成功。
- OpenAI/Codex `/v1/chat/completions` 流式请求成功。
- Kilo 客户端分别测试 `claude-opus-4-8` 和 `gpt-5.5` 成功。
- Kilo 切换到 `/v1/chat/completions` 后成功。
- Cline 连接 ARM64 后成功，`gpt-5.5` 身份回答为 `GPT-5.5 / Codex CLI`。
- BWG 通过 ARM64 API key 下游探活 `/v1/responses` 和 `/v1/chat/completions` 成功。
- ARM64 自身后台测试连接成功。

### 3.3 已知边界

- ARM64 usage 页面里的 `USER-AGENT` 是客户端到 ARM64 的入口 UA，不等价于 ARM64 发往上游/BWG 的伪装 UA。
- BWG 侧应以 ARM64 出站请求为准判断上游伪装效果。
- 模型侧身份回答来自请求 body 中的 `instructions`、`input`、`tools` 等可见线索；sub2api 不做响应侧文本替换。

---

## 4. 下一阶段目标

阶段一已经解决当前 Kilo / Cline / Cursor / Roo Code 这类客户端通过 `/v1/responses` 或 `/v1/chat/completions` 接入官方客户端限制类 OpenAI API 的通用可用性问题。后续目标分为阶段二和阶段三：

- 阶段二：body cloaking / body identity mimic，目标是解决模型回答“我是 Kilo”这类模型侧身份认知问题。
- 阶段三：UI / 配置产品化，把阶段一和阶段二确认稳定的开关放进前端，避免长期靠手工改 `account.Extra`。

阶段二不建议直接盲改源码。正确顺序是：

1. BWG 侧采集真实官方客户端请求。
2. BWG 侧采集 ARM64 伪装请求。
3. 建差异表。
4. 只对高置信差异改源码。
5. 每改一项都用 Kilo + 官方客户端双向回归。

### 4.1 阶段二目标：body cloaking

阶段二的目标是解决“模型知道自己在 Kilo / Cursor / Cline 等非官方客户端中”的身份认知问题。

当前结论需要拆开：

- OpenAI/Codex 可用性问题由阶段一解决：`/v1/responses`、`/v1/chat/completions`、ARM64 自测、BWG 下游探活都走同一套 Codex mimic。
- OpenAI/Codex 阶段一不默认做 Cursor / Kilo / Cline / Roo Code 身份替换。
- CLIProxyAPI 的 body cloaking 策略仍然有价值，但优先作为 Anthropic mimic Claude Code 路径的候选方案灰度，不机械照抄到所有平台。

阶段二优先级：

1. 移植 `sanitizeForwardedSystemPrompt` 式身份清洗，但先限定作用范围。
2. `oauthToolRenameMap` 工具名归一 + 响应回写做成独立开关，先灰度。
3. 抓 Codex 真机 JA4/ALPN，确认官方 Codex 是 h1 还是 h2，再决定是否调整 OpenAI/Codex 的 ALPN/HTTP2 策略。
4. 敏感词零宽混淆、per-key 设备画像钉扎后置。
5. Anthropic 链路先守住当前 h1/Node TLS 方案，除非真机抓包证明官方 Claude Code 已变化。

### 4.2 阶段二第一步：Anthropic 身份清洗

第一步只移植 `sanitizeForwardedSystemPrompt` 式身份清洗，默认限定：

- Anthropic mimic Claude Code profile。
- 非官方 Claude Code 客户端，例如 Kilo / Cline / Roo 等。
- 独立开关控制，不影响普通 Anthropic API Key passthrough。
- 不影响 OpenAI/Codex 路径。

候选开关：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "anthropic_apikey_mimic_official_identity": true
}
```

处理流程：

1. 识别原客户端 system/instructions 中的身份声明，例如 Kilo、Cline、Roo、Cursor、Aider。
2. 删除或改写这些身份声明，只保留项目规则、用户偏好、工具使用规则。
3. 注入真实官方客户端的 system prompt。
4. 不在第一步改工具名，避免同时引入两个变量。
5. 用“你是什么模型 / 你在哪个客户端中”作为验收问题之一。

### 4.3 阶段二第二步：工具名归一与响应回写

第二步再做工具名归一，必须独立开关，不能和身份清洗绑死。

候选开关：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "anthropic_apikey_mimic_official_identity": true,
  "anthropic_apikey_mimic_tool_rename": true
}
```

处理重点：

- 参考 CLIProxyAPI 的 `oauthToolRenameMap`，把第三方工具名归一成更接近官方客户端的工具名。
- 上游返回 tool_use 后，必须把工具名回写成 Kilo 能识别的原始工具名。
- 需要同时覆盖流式和非流式响应。
- 必须加 Kilo 工具调用回归测试，至少覆盖一次工具调用、一次多工具、一次工具错误。

### 4.4 OpenAI/Codex 阶段二边界

OpenAI/Codex 侧当前不做 CLIProxyAPI 式 body identity cloaking。

已确认保留：

- 裸 `role/content` message 规范化。
- `role:"system"` 归一为 `role:"developer"`。
- 缺 `instructions` 时补 Codex CLI base instructions。
- 强制 `stream:true`、`store:false`、`include:["reasoning.encrypted_content"]`。
- 缺 `prompt_cache_key` 时补 `codex-mimic-*`。
- 保留 Cursor / Kilo / Cline / Roo Code 原始身份文本、工具 description、工作区路径和客户端能力提示。

后续只做传输层研究：

- 抓 Codex 真机 JA4/ALPN。
- 确认官方 Codex 是 h1 还是 h2。
- 再决定 sub2api 是否需要调整 OpenAI/Codex 的 ALPN/HTTP2 策略。

### 4.5 阶段三目标：UI / 配置产品化

阶段三目标是把已经验证稳定的 mimic 能力产品化到前端 UI，减少直接改数据库或 `account.Extra` 的维护成本。

阶段三不应早于阶段二核心行为稳定。建议 UI 分批上线：

第一批，阶段一稳定开关：

- `anthropic_apikey_mimic_claude_code`
- `openai_apikey_mimic_codex_cli`
- `enable_tls_fingerprint`
- Anthropic / OpenAI API Key mimic 与 passthrough 互斥提示。

第二批，阶段二灰度开关：

- `anthropic_apikey_mimic_official_identity`
- `anthropic_apikey_mimic_tool_rename`
- `anthropic_apikey_mimic_profile`

第三批，诊断能力：

- 显示当前账号实际出站 profile。
- 显示是否启用 TLS fingerprint。
- 显示最近一次 mimic 请求的脱敏诊断摘要。
- 提供“仅脱敏导出”的抓包辅助入口。

UI 原则：

- 默认保持阶段一稳定行为。
- 高风险 body cloaking 开关默认关闭或灰度。
- 每个开关要有简短风险提示，尤其是工具名归一和响应回写。
- 不在 UI 中展示密钥、token、authorization、x-api-key。

### 4.6 阶段二采样与对比

必抓样本：

- Claude 官方客户端 / Claude Code：`/v1/messages` 非流式。
- Claude 官方客户端 / Claude Code：`/v1/messages` 流式。
- Claude 官方客户端 / Claude Code：`/v1/messages/count_tokens`。
- Codex 官方客户端：`/v1/responses` 非流式。
- Codex 官方客户端：`/v1/responses` 流式。
- 带 tools / function calling 的请求。
- 带长上下文、`context-1m-2025-08-07` 或 reasoning 的请求。
- 直接问“你是什么模型 / 你在哪个客户端中”的请求。

每个样本记录：

- method、path、query。
- 完整 header 和顺序。
- body 原文。
- response status、stream event 类型。
- usage。
- UA、beta、version、originator、metadata、session 类字段。

| 维度 | 对比内容 | 目的 |
|---|---|---|
| header | UA、beta、version、originator、x-stainless、session 类字段 | 让 BWG/上游看到官方客户端形态 |
| body identity | system、instructions、metadata、model alias、客户端身份文本 | 阶段二 Anthropic 优先解决“我是 Kilo” |
| tools | 工具名称、description、schema、tool_choice、cache_control | 阶段二第二步灰度工具名归一与响应回写 |
| stream | SSE 事件顺序、event 类型、done 方式 | 保持客户端兼容 |
| TLS/HTTP2 | JA3/JA4、ALPN、h2 settings、header 顺序 | 评估是否需要 transport 级改造 |

注意：抓包和日志必须在服务器本地脱敏，密钥、token、authorization、x-api-key 不发到聊天里。

### 4.7 是否要模拟 Claude Desktop 还是 Claude Code

这点需要先定目标：

- 如果目标是 Claude Code：在阶段一基础上继续补 identity sanitizer 和工具映射即可。
- 如果目标是 Claude Desktop：需要单独采 Claude Desktop 真实请求，因为 Desktop 的 system prompt、UA、metadata、工具语义可能不同。

不能把 Claude Code、Claude Desktop、Claude agent-sdk 三者混成一个 profile。阶段二建议做成 profile 化：

```json
{
  "anthropic_apikey_mimic_profile": "claude_code"
}
```

后续可扩展：

```json
{
  "anthropic_apikey_mimic_profile": "claude_desktop"
}
```

---

## 5. 代码维护范围

阶段一相对 upstream 的主要改动范围：

- 新增自有文件：
  - `backend/internal/service/account_apikey_mimicry.go`
  - `backend/internal/service/gateway_apikey_mimicry.go`
  - `backend/internal/service/apikey_mimic_identity.go`
  - `backend/internal/service/openai_apikey_mimicry.go`
  - `PATCHES.md`
- 既有热点文件：
  - `backend/internal/service/gateway_service.go`
  - `backend/internal/service/openai_gateway_service.go`
  - `backend/internal/service/openai_gateway_chat_completions.go`
  - `backend/internal/service/account_test_service.go`
  - `backend/internal/pkg/openai/constants.go`
  - `backend/internal/service/account.go`
- 测试文件：
  - Anthropic passthrough/mimic 相关测试。
  - OpenAI gateway/mimic 相关测试。
  - OpenAI account test / 探活相关测试。
  - OpenAI model instructions 相关测试。
  - 受接口签名变化影响的 handler/service 测试。

升级时重点看 `PATCHES.md`，不要只看本文档。

---

## 6. 部署与升级

### 6.1 当前 fork 信息

```bash
origin   https://github.com/itv3/sub2api.git
upstream https://github.com/Wei-Shaw/sub2api.git
```

当前分支：

```bash
mimic -> origin/mimic
main  -> upstream/main
```

### 6.2 Fork / 镜像 / 部署流程

线上环境不要跟踪官方 `weishaw/sub2api:latest`。官方镜像更新会覆盖自定义 mimic 代码。

当前采用流程：

1. 在 `/Users/czs/Documents/sub2api` 的 `mimic` 分支开发。
2. 从 `upstream/main` 合并官方更新到本地。
3. 解决冲突后运行 mimic 相关单测。
4. 推送到 `origin/mimic`。
5. 构建自定义镜像，例如 `sub2api:mimic-<sha>-arm64`。
6. ARM64 服务器只替换应用镜像，不动 `.env`、PostgreSQL/Redis volume、Nginx 主结构。
7. 重启 `sub2api-mimic` 后验证健康状态和 API-only 行为。
8. 用 Kilo、Cline、Claude 官方客户端、Codex 官方客户端做回归。
9. 对 ARM64 自测、BWG 下游 `/v1/responses` 探活、BWG 下游 `/v1/chat/completions` 探活做回归。

当前 ARM64 compose 路径：

```bash
/root/docker/sub2api-mimic/app/docker-compose.yml
```

当前 ARM64 源码路径：

```bash
/root/docker/sub2api-mimic/repo
```

服务器升级原则：

- 只更新自定义镜像。
- 不覆盖 `.env`。
- 不删除数据库 volume。
- 不重写 Nginx 主配置。
- 修改 compose 前先备份。

### 6.3 Watchtower 策略

如果继续使用 Watchtower，只允许它跟踪自定义镜像，不要跟踪官方 `weishaw/sub2api:latest`。

推荐：

- 自定义镜像 tag 固定带 commit sha。
- 测试失败不推新镜像。
- ARM64 只拉取已验证镜像。
- 当前阶段一运行镜像为 `sub2api:mimic-f18101f3-desktop-profile-arm64`。

---

## 7. 深度 mimic 验收标准

如果未来继续做 Anthropic 或更深度 body mimic，完成后才可以把阶段能力升级描述为：

> API Key 请求在目标样本范围内与 Claude/Codex 官方客户端形态一致，且模型侧身份回答接近官方客户端。

后续深度 body mimic 验收必须同时满足：

- BWG 端看到 ARM64 请求的关键 header 与官方样本一致或差异已解释。
- Anthropic 侧 body 中没有第三方客户端身份泄漏。
- OpenAI/Codex 侧不得为了身份认知破坏 Cursor / Kilo / Cline / Roo Code 原始工具链。
- “你是什么模型 / 你在哪个客户端中”回答接近官方客户端。
- tools 场景保持可用。
- streaming 和 non-streaming 都通过。
- Claude 和 Codex 两条链路都能正常计费和记录 usage。
