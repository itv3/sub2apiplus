# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的个人自用 fork。

本仓库用于维护自建镜像、私有部署和定制补丁，不作为公开项目介绍页使用。对外仓库名、Docker 镜像名、页面标题和默认站点名使用 `sub2apiplus` / `Sub2API Plus`；为降低上游同步成本，Go module、Go import、二进制名、systemd service 名和部分默认路径仍保留 `sub2api`。

## 维护重点

- 长期跟随上游 Sub2API 更新，按需合并 `upstream/main` 或上游 release tag。
- 自定义补丁尽量集中记录在 [PATCHES.md](PATCHES.md)。
- API Key 客户端伪装方案记录在 [sub2api-apikey客户端伪装.md](sub2api-apikey客户端伪装.md)。
- Docker 镜像发布到 `ghcr.io/itv3/sub2apiplus`。
- ARM64 部署使用 Docker Compose，只替换应用镜像，不动数据库、Redis、Nginx 和数据卷。

## 主要增强方向

### 1. API Key 客户端伪装

本 fork 重点增强了 API Key 账号在官方客户端场景下的请求形态模拟能力，主要覆盖 Anthropic / Claude Code 和 OpenAI / Codex 两条路径。

增强目标是让第三方客户端接入 API Key 账号时，请求头、请求体、TLS 行为、缓存字段和路由判断更接近官方客户端形态，同时避免破坏普通 API Key 调用路径。

相关文档：

- [PATCHES.md](PATCHES.md)
- [sub2api-apikey客户端伪装.md](sub2api-apikey客户端伪装.md)

### 2. Anthropic / Claude Code 增强

Anthropic 方向主要增强 Claude Code API Key 场景：

- 增加 `anthropic_apikey_mimic_claude_code` 账号级开关
- 让 API Key mimic 与 `anthropic_passthrough` 保持互斥，避免行为混杂
- 为 `/v1/messages` 和 `count_tokens` 保留 mimic-aware 分支
- 补齐 Claude Code 风格 header、beta、billing、cache-control 等请求语义
- 放开 API Key mimic 场景下的 TLS fingerprint 启用条件
- 强化请求形态验证和回归测试，避免 mimic 流量意外落回普通路径

### 3. OpenAI / Codex 增强

OpenAI 方向主要增强 Codex API Key 场景：

- 增加 `openai_apikey_mimic_codex_cli` 账号级开关
- 支持 profile 驱动的 Codex mimic，目前以 Codex Desktop 形态为默认 profile
- 对 `/v1/responses`、`/v1/chat/completions`、探测和自测路径复用统一 mimic profile
- 重写 Codex 相关 header、`originator`、`User-Agent`、`x-codex-*`、`session-id`、`thread-id`
- 补齐 `prompt_cache_key`、`client_metadata`、`system -> developer`、`stream`、`store`、`include` 等 body 语义
- mimic 账号的 Responses 能力探测与普通 API Key 探测分键保存，避免互相污染
- mimic 流量不会因为普通 capability probe、`force_chat_completions` 等配置误落回普通 raw chat completions 路径
- TLS fingerprint 开启后可进入 `DoWithTLS` 发送路径，并优先使用 Codex profile

### 4. Grok / xAI OAuth 支持

本 fork 保留并强化 Grok / xAI 订阅账号接入能力：

- 支持 xAI OAuth 账号授权和刷新
- 支持 OpenAI-compatible Responses 入口
- 支持将 Claude-compatible `/v1/messages` 转换到 xAI Responses 上游
- 支持 Codex CLI 风格 Responses WebSocket 入口到 HTTP / SSE 上游的桥接
- 使用 xAI 返回的 rate-limit / quota header 进行被动额度记录

### 5. Antigravity 支持

本 fork 保留 Antigravity 账号接入能力：

- 支持 Antigravity 账号授权
- 提供 Claude 模型专用入口 `/antigravity/v1/messages`
- 提供 Gemini 模型专用入口 `/antigravity/v1beta/`
- 可选开启 hybrid scheduling，让通用入口也能调度到 Antigravity 账号

### 6. 部署和发布增强

本 fork 的发布面已经独立于上游：

- GitHub 仓库改为 `itv3/sub2apiplus`
- Docker 镜像改为 `ghcr.io/itv3/sub2apiplus`
- GoReleaser 和 release workflow 改为发布 `sub2apiplus` 镜像
- Docker Compose 示例默认使用自建镜像
- 页面标题和默认站点名改为 `Sub2API Plus`

推荐部署方式仍然是 Docker Compose，并让 Watchtower 只跟踪自建镜像，不要直接跟踪上游官方镜像。

## 本地验证

后端 Go module 位于 `backend` 目录：

```bash
cd backend
make test-unit
make test-integration
```

前端位于 `frontend` 目录：

```bash
make test-frontend
```

## 发版流程

1. 更新 `backend/cmd/server/VERSION`。
2. 提交代码并推送 `main`。
3. 创建并推送 `v<version>` tag。
4. 通过 `.github/workflows/release.yml` 发布 GitHub Release 和 GHCR 镜像。
5. 在 ARM64 上拉取正式镜像并用 Docker Compose 重建 `sub2apiplus` 应用容器。

Release notes 只写当前版本变化和验证结果，不再重复安装命令、仓库链接和文档链接。

## 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。使用本项目接入第三方 AI 服务可能违反相关服务商的用户协议，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
