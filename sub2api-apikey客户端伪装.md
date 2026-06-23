# sub2api：API Key 官方客户端伪装阶段一最终记录

更新时间：2026-06-23

> 目标：让通过 sub2api 转发的 API Key 账号，在上游和模型侧都尽量接近官方客户端请求形态。
>
> - Anthropic API Key：伪装成 Claude Code / Claude Desktop 系官方 API Key 请求。
> - OpenAI API Key：伪装成 Codex CLI / Codex Desktop 系官方 API Key 请求。
>
> 当前阶段一已经不是早期的“只做 header 伪装”。实际落地后，为了让 Kilo / Cursor 这类非官方客户端可用，已经做到了 header + TLS 发送路径 + Anthropic body 级 Claude Code 形态 + OpenAI Codex body 兼容修正。
>
> 2026-06-23 复测结论：OpenAI Codex mimic 路径不需要移植 CLIProxyAPI 式 Cursor/Kilo 身份清洗。Kilo / Cursor 不能使用官方客户端限制类 OpenAI API 的根因已经由两项兼容修正解决：裸 `role/content` message 规范化，以及缺失 `prompt_cache_key` 时补 `codex-mimic-*`。

---

## 1. 当前结论

### 1.1 阶段一状态

阶段一已完成并部署到 ARM64 测试服，本版本作为阶段一最终版保存。

当前代码分支：

- 本地仓库：`/Users/czs/Documents/sub2api`
- 分支：`mimic`
- 远端：`origin/mimic`
- 阶段一基线提交：`e4c93a4f fix: preserve context 1m beta for api key mimic`
- 阶段一最终提交：`b5dfa56e fix: finalize api key mimic phase one`

ARM64 测试服当前运行：

- 当前运行镜像：`sub2api:mimic-e4c93a4f-stage2j-arm64`
- 阶段一最终保存镜像：`sub2api:mimic-e4c93a4f-phase1-final-arm64`
- 镜像 ID：`sha256:9da1adfd400f553c6d51a64f7621ddfd2f5d20ed93ffb9516448ed92684fcd6d`
- 容器：`sub2api-mimic`
- 对外地址：`https://sg.3ab.in`
- Claude Base URL：`https://sg.3ab.in`
- Codex Base URL：`https://sg.3ab.in/v1`

Kilo 客户端已验证通过：

- Claude：`claude-opus-4-8`，`/v1/messages`，流式成功。
- Codex/OpenAI：`gpt-5.5`，`/v1/responses`，流式成功。

### 1.2 两类“伪装”必须分开看

这里有两个不同目标，不能混为一谈：

| 目标 | 看哪里 | 当前状态 |
|---|---|---|
| 上游识别为官方客户端 | header、TLS、HTTP 发送路径、beta、UA、originator | 阶段一已跑通 Kilo，BWG 端看到 ARM64 请求已像官方 CLI |
| 模型自我认知为官方客户端 | body 里的 `system` / `instructions` / `metadata` / `tools` / 原客户端身份提示 | Anthropic 侧后续可继续研究；OpenAI/Codex 侧本轮不再做 Cursor/Kilo 身份替换 |

2026-06-23 的同上游复测显示：Kilo 回答中出现 `Kilo Code / Codex CLI` 这类组合表述，是上游模型同时看到 Kilo 客户端上下文与 Codex CLI 基础 instructions 后的自然归纳，不是 sub2api 侧把 `Kilo` 改写成 `Codex`，也不是必须用 body identity sanitizer 解决的问题。

### 1.3 阶段一最终边界

可以做：

- 注入真实 Claude Code / Claude Desktop 的 system prompt、billing block、metadata、tool 形态。
- 将第三方客户端工具名映射成官方工具名，回包时再映射回客户端能识别的名字。
- 对 OpenAI/Codex 侧保留或补齐必要 Codex body 兼容语义。
- 对比官方客户端真实请求后，按差异逐项收敛。

不能轻率承诺：

- 如果官方客户端还有服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 层上下文，单靠 sub2api 不能 100% 复制。
- 如果官方客户端和第三方客户端可用工具完全不同，强行映射可能影响工具调用稳定性。
- 如果上游或中转检查 HTTP/2 wire 指纹，Go `net/http` 默认传输层仍可能与官方客户端不同。
- OpenAI/Codex 侧不再默认删除或改写 Cursor / Kilo 的真实客户端身份文本，避免影响工具调用、工作区路径、客户端能力提示和后续排障。

阶段一最终目标是：

> 让 Kilo / Cursor 等非官方客户端在官方客户端限制类 API 上可用，同时让 BWG 端看到的关键 header、TLS、OpenAI/Codex 兼容 body 字段满足上游要求。

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

### 2.2 Anthropic 已落地内容

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

需要阶段二继续收敛：

- 第三方客户端原始 system 中的“我是 Kilo / You are Kilo”类身份提示，目前不能只迁移，必要时要删除或改写。
- 工具名如果带 `kilo_` / `cline_` / `roo_` 等命名，会让模型继续感知第三方客户端，需要官方工具名映射和回写。
- 真实 Claude Desktop 与 Claude Code CLI 的 body 形态可能不同，需要分 profile。

### 2.3 OpenAI / Codex 已落地内容

主要文件：

- `backend/internal/service/account_apikey_mimicry.go`
- `backend/internal/service/apikey_mimic_identity.go`
- `backend/internal/service/openai_apikey_mimicry.go`
- `backend/internal/service/openai_gateway_service.go`
- `backend/internal/service/openai_codex_transform.go`
- `backend/internal/service/account.go`

已实现：

- `openai_passthrough` 与 `openai_apikey_mimic_codex_cli` 互斥，mimic 优先。
- 开启账号级 mimic 时，内部 `isCodexCLI` 视为 true。
- 这样可以保留 Codex 相关 body 语义，避免 Kilo 请求被当作普通第三方请求清理。
- 出站强制覆盖 Codex header：
  - `user-agent: codexCLIUserAgent`
  - `originator: codex_cli_rs`
  - `OpenAI-Beta: responses=experimental`
  - `version: codexCLIVersion`
- 删除客户端透传的 `session_id` / `conversation_id`。
- OpenAI API Key mimic 主 HTTP `/v1/responses` 走 `DoWithTLS`。
- TLS profile 为 nil 时保持兼容回退。
- 裸 `role/content` message 规范化为 Codex Desktop / Responses 风格：
  - 原始形态：`{"role":"user","content":"..."}`
  - 规范形态：`{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}`
- 仅当请求缺少 `prompt_cache_key` 时，自动补稳定的 `codex-mimic-*`。
- 如果客户端已有 `prompt_cache_key`，保持原值不覆盖。
- 不再对 OpenAI/Codex mimic 请求做 Cursor / Kilo / Cline / Roo / OpenCode 身份文本替换。
- 工具名、工具 description、工作区路径、客户端能力提示保持原样，避免破坏客户端工具执行链。

2026-06-23 确认：

- Cursor / Kilo 不能使用 `gpt-5.5` 官方客户端限制类 API 的根因是上面两项 body 兼容差异。
- 同上游复测后，`Kilo Code / Codex CLI` 与 `Codex CLI / Kilo Code` 的回答差异属于上游模型表达差异，不是 sub2api 文本替换。
- OpenAI/Codex 侧阶段一最终版不引入 CLIProxyAPI 式 body identity cloaking。

---

## 3. 已验证事实

### 3.1 本地单测

已通过：

```bash
go test ./internal/service
```

关键覆盖：

- Anthropic API Key mimic 丢弃客户端 header 和 OAuth beta。
- Anthropic API Key mimic 将第三方 body 改为 Claude Code 形态。
- `claude-opus-4-6/4-7/4-8` 额外保留 `context-1m-2025-08-07`。
- Anthropic `count_tokens` 使用 token-counting beta。
- OpenAI API Key Codex mimic 覆盖客户端 header。
- OpenAI API Key Codex mimic 走 TLS 路径。
- Kilo 请求在 OpenAI API Key mimic 下按 Codex CLI 语义处理。
- OpenAI API Key Codex mimic 裸 `role/content` message 规范化。
- OpenAI API Key Codex mimic 缺 `prompt_cache_key` 时补 `codex-mimic-*`。
- OpenAI API Key Codex mimic 保留 Cursor / Kilo 身份文本，不做替换。

### 3.2 ARM64 测试服

已验证：

- `https://sg.3ab.in/` 仍保持 API-only，根路径不展示网站页面。
- `https://sg.3ab.in/v1` 可用于 API 调用。
- Claude 非流式和流式请求成功。
- OpenAI/Codex 非流式和流式请求成功。
- Kilo 客户端分别测试 `claude-opus-4-8` 和 `gpt-5.5` 成功。

### 3.3 截图解读

ARM64 usage 页面里的 `USER-AGENT` 是客户端到 ARM64 的入口 UA，因此会显示 Kilo：

- `Kilo-Code/7.3.50 ...`

这不等价于 BWG 收到的出站伪装 UA。

BWG usage 页面里可以看到 ARM64 发过去的伪装请求已经变成：

- Claude：`claude-cli/... (external, cli)`
- Codex：`codex_cli_rs/...`

这说明阶段一的上游 header 伪装已生效。

如果模型回答中同时提到 Kilo Code 与 Codex CLI，需要结合上游账号和同源抓包判断。2026-06-23 同上游复测结论是：这类回答来自上游模型对 Kilo 上下文和 Codex CLI instructions 的合并理解，不是 sub2api 侧身份替换。

---

## 4. 后续计划

阶段一最终版已解决当前 Kilo / Cursor 接入官方客户端限制类 OpenAI API 的问题。后续如果继续做“模型侧完全像官方客户端”的研究，不建议直接盲改源码。正确顺序是：

1. BWG 侧采集真实官方客户端请求。
2. BWG 侧采集 ARM64 伪装请求。
3. 建差异表。
4. 只对高置信差异改源码。
5. 每改一项都用 Kilo + 官方客户端双向回归。

### 4.1 必抓样本

Anthropic：

- Claude 官方客户端 / Claude Code：`/v1/messages` 非流式。
- Claude 官方客户端 / Claude Code：`/v1/messages` 流式。
- Claude 官方客户端 / Claude Code：`/v1/messages/count_tokens`。
- 带 tools 的请求。
- 带长上下文或 `context-1m-2025-08-07` 的请求。
- 直接问“你是什么模型 / 你在哪个客户端中”的请求。

OpenAI / Codex：

- Codex 官方客户端：`/v1/responses` 非流式。
- Codex 官方客户端：`/v1/responses` 流式。
- 带 tools/function calling 的请求。
- 带 reasoning 的请求。
- 直接问“你是什么模型 / 你在哪个客户端中”的请求。
- 仅用于对照，不作为当前阶段必须继续移植 body identity sanitizer 的依据。

每个样本记录：

- method、path、query。
- 完整 header 和顺序。
- body 原文。
- response status、stream event 类型。
- usage。
- UA、beta、version、originator、metadata、session 类字段。

注意：抓包和日志必须在服务器本地脱敏，密钥、token、authorization、x-api-key 不发到聊天里。

### 4.2 对比维度

| 维度 | 对比内容 | 目的 |
|---|---|---|
| header | UA、beta、version、originator、x-stainless、session 类字段 | 让 BWG/上游看到官方客户端形态 |
| body identity | system、instructions、metadata、model alias、客户端身份文本 | Anthropic 侧可继续研究；OpenAI/Codex 侧当前保持客户端原文 |
| tools | 工具名称、description、schema、tool_choice、cache_control | Anthropic 侧如需官方工具名映射再单独灰度；OpenAI/Codex 侧当前不改工具名 |
| stream | SSE 事件顺序、event 类型、done 方式 | 保持客户端兼容 |
| TLS/HTTP2 | JA3/JA4、ALPN、h2 settings、header 顺序 | 评估是否需要 transport 级改造 |

### 4.3 针对 Anthropic “我是 Kilo”的可选修正方向

如果后续仍要处理 Anthropic 模型侧身份认知，优先做 Anthropic。OpenAI/Codex 路径已经确认不需要靠身份清洗解决当前可用性问题。

建议新增一个可控策略，不要一上来硬编码删除所有 system：

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
4. 如果工具名泄漏客户端身份，将工具名映射成官方工具名。
5. 上游 tool_use 回来后，再把工具名映射回 Kilo 能识别的名字。
6. 用“你是什么模型 / 你在哪个客户端中”作为验收问题之一。

OpenAI/Codex 侧当前不做类似开关。已删除的方向包括：

- 不默认把 `Kilo` / `Cursor` / `Cline` / `Roo` / `OpenCode` 替换成 `Codex`。
- 不默认改写工具 description、工作区路径、客户端能力提示。
- 不默认映射工具名，避免破坏 Kilo / Cursor 的工具调用与执行链。

### 4.4 是否要模拟 Claude Desktop 还是 Claude Code

这点需要先定目标：

- 如果目标是 Claude Code：当前阶段一已经接近这个方向，继续补 identity sanitizer 和工具映射即可。
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
  - `backend/internal/service/openai_apikey_mimicry.go`
  - `PATCHES.md`
- 既有热点文件：
  - `backend/internal/service/gateway_service.go`
  - `backend/internal/service/openai_gateway_service.go`
  - `backend/internal/service/account.go`
- 测试文件：
  - Anthropic passthrough/mimic 相关测试。
  - OpenAI gateway/mimic 相关测试。
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
8. 用 Kilo、Claude 官方客户端、Codex 官方客户端做回归。

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

---

## 7. 后续验收口径

当前阶段一最终版可以说：

> 阶段一已完成 API Key 官方客户端伪装基础能力，Kilo / Cursor 已能通过 OpenAI Codex 官方客户端限制类 API；OpenAI/Codex 路径只保留裸 `role/content` 规范化与 `prompt_cache_key` 补齐两项 body 兼容修正。

如果未来继续做 Anthropic 或更深度 body mimic，完成后才可以升级描述为：

> API Key 请求在目标样本范围内与 Claude/Codex 官方客户端形态一致，且模型侧不再泄漏 Kilo 等第三方客户端身份。

后续深度 body mimic 验收必须同时满足：

- BWG 端看到 ARM64 请求的关键 header 与官方样本一致或差异已解释。
- Anthropic 侧 body 中没有第三方客户端身份泄漏。
- OpenAI/Codex 侧不得为了身份认知破坏 Cursor / Kilo 原始工具链。
- “你是什么模型 / 你在哪个客户端中”回答接近官方客户端。
- tools 场景不因工具名映射破坏。
- streaming 和 non-streaming 都通过。
- Claude 和 Codex 两条链路都能正常计费和记录 usage。
