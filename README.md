# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的自用增强版 fork。维护目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 项目状态

| 项 | 结论 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| 版本基线 | 当前 Plus 版本为 `0.1.151-3`（见 `backend/cmd/server/VERSION`，发版后以最新 tag 为准）；已合并上游 tag `v0.1.151`，自定义差异优先看 `v0.1.151..HEAD`。 |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| 命名约定 | 对外使用 `sub2apiplus` / `Sub2API Plus`；Go module 和 import 保留 `github.com/Wei-Shaw/sub2api`，降低上游合并成本。 |
| Go 版本 | 主服务 `go 1.26.5`（`backend/go.mod`）；keeper `go 1.24`（`keeper/go.mod`） |
| Docker 命名 | Compose service 保留 `sub2api`；默认容器名为 `sub2apiplus`、`sub2apiplus-postgres`、`sub2apiplus-redis`。 |

维护原则：

1. 长期跟随上游主线，发布版本按上游 release/tag 对齐。
2. Plus 账号级开关优先放入 `account.Extra`；mimic / 保活走 Plus 设置页，Antigravity 走账号编辑页。
3. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、反向代理配置、`.env` 和数据目录。

## 1. Plus 增强功能

管理后台侧边栏“Plus 增强功能”进入 `/admin/settings/plus-enhancements`，包含“API Key 官方客户端兼容”和“账号保活”两个 Tab；Antigravity 增强在账号创建/编辑页配置。

| 功能 | 定位 | 配置归属 / UI 入口 |
|---|---|---|
| API Key 官方客户端兼容 | 模拟 Claude / Codex 官方客户端的请求形态。 | Plus 设置页；账号级开关在 `account.Extra`。 |
| Antigravity 增强 | 统一默认模型、模型映射、官方伪装和计费口径。 | 账号创建/编辑页。 |
| 账号保活 | 通过官方客户端对空闲账号发起低频真实项目请求。 | Plus 设置页；执行器为 keeper sidecar。 |

### 1.1 API Key 官方客户端兼容

API Key 官方客户端兼容用于让 Kilo / Cline / Cursor / Roo Code 等非官方客户端经 sub2apiplus 转发到第三方中转站时，尽量接近 Claude / Codex 官方客户端的 header、body、TLS 和路由形态。

统一适用条件：mimic 只作用于启用对应开关的 Anthropic / OpenAI API Key 账号和非官方客户端；检测到 Claude / Codex 官方客户端（桌面版或 CLI）时统一跳过 mimic，按普通 API Key 逻辑处理。

核心规则：

1. 仅 Anthropic / OpenAI 的 API Key 账号生效，不改变 OAuth 账号逻辑。
2. mimic 与 passthrough 运行时互斥；同时开启时，非官方客户端优先走 mimic。
3. 测试连接仍使用官方客户端请求形态：OpenAI 复用 Codex mimic profile，Anthropic 构造 Claude Code 风格请求。
4. 非官方客户端命中 mimic 时，关键身份 header 不允许被账号级 header override 覆盖；官方客户端跳过 mimic 后不应用该保护。
5. OpenAI Codex mimic 的 `/v1/messages` 固定进入 Responses mimic 链路，不受 `force_chat_completions`、普通 Responses probe false 或 `openai_responses_supported=false` 影响。
6. OpenAI Codex mimic 当前只支持 HTTP/SSE 上游，命中时不进入 Responses WebSocket；跳过 mimic 的官方 Codex 客户端按普通 API Key 账号的全局/账号 WS 开关、force HTTP 和 WSv2 mode 选择路由。该判断同时作用于账号调度、粘性账号复核和最终转发。

账号 `account.Extra` 开关：

```json
{
  "anthropic_apikey_mimic_claude_code": true,
  "openai_apikey_mimic_codex_cli": true,
  "openai_apikey_mimic_codex_profile": "desktop_0_142",
  "enable_tls_fingerprint": true
}
```

| 平台 | mimic 目标 | 当前行为 |
|---|---|---|
| Anthropic API Key | Claude Code | `/v1/messages` 与 `/v1/messages/count_tokens` 使用独立构造链，补 Claude Code header、beta、TLS 和 body 形态；UA 基线为 `claude-cli/2.1.207 (external, sdk-cli)`，并执行已知工具名归一和响应侧结构化回写。 |
| OpenAI API Key | Codex | 默认 profile 为 `desktop_0_142`，基线 `codex_exec/0.144.1`、Linux/aarch64 形态；补 `x-openai-internal-codex-responses-lite`、`x-codex-*` 和 turn metadata。Responses 路由、body 默认值（`store=false`、`stream`、`include=reasoning.encrypted_content`、`reasoning.context=all_turns`、`text.verbosity=low`）、`prompt_cache_key`、`client_metadata`、TLS profile 和 capability probe 均按 profile 处理；`cli_rs_0_125` 可回滚旧 Codex CLI。 |

#### Anthropic 1M 上下文 beta

非官方客户端实际命中 Anthropic API Key mimic 时，以下模型前缀会自动补充 `context-1m-2025-08-07`：

- `claude-fable-5`
- `claude-opus-4-6`
- `claude-opus-4-7`
- `claude-opus-4-8`

模型按前缀匹配，因此覆盖对应的版本化名称。`context-1m-2025-08-07` 在完整 `anthropic-beta` 列表中固定处于第 7 项，即 `mid-conversation-system-2026-04-07` 之后、`advisor-tool-2026-03-01` 之前。

| 场景 | beta 尾部 | 1M beta |
|---|---|---|
| 普通 `/v1/messages` | `effort-2025-11-24,fallback-credit-2026-06-01` | 匹配上述模型时保留在第 7 项。 |
| `output_config.format.type == "json_schema"` 的 `/v1/messages` | `effort-2025-11-24,structured-outputs-2025-12-15` | 同上。 |
| `/v1/messages/count_tokens` | 普通尾部并追加 `token-counting-2024-11-01`；无官方样本证明前不套用 structured outputs 条件切换。 | 同上。 |

自动补齐只属于实际命中 mimic 的请求；官方 Claude 客户端需自行启用并发送 1M beta。

Anthropic API Key mimic 的请求构造边界：

| 项目 | `/v1/messages` | `/v1/messages/count_tokens` |
|---|---|---|
| TLS | 启用 TLS fingerprint 且命中 mimic 时使用 Claude CLI 2.1.207 / Node.js 26 profile，ALPN 固定 HTTP/1.1。 | 使用相同 profile。 |
| 压缩 | 始终显式发送 `Accept-Encoding: gzip, deflate, br, zstd`，由受控转发链解压，账号 header override 不得覆盖。 | 不照搬显式压缩头。 |
| 身份与会话 | 同时发送 `x-api-key` 和 Bearer token；移除 `x-client-request-id`、`x-stainless-helper-method`；从最终 `metadata.user_id` 提取同源 session ID。 | 只应用已确认的 Claude mimic header，不添加 SDK CLI 身份块或 session header。 |
| Body | 使用 `cc_entrypoint=sdk-cli` 和 Claude Agent SDK 身份提示；不自动补 `temperature=1`；仅删除无附加语义的 `tool_choice: {"type":"auto"}`，带 `disable_parallel_tool_use` 等附加字段时保留。 | 只应用已确认的 body 清理和工具名处理，不照搬 structured outputs 规则。 |

OpenAI `/v1/responses/compact` 是特例：上游保持官方 unary JSON 形态，移除 `stream`、`store`、`prompt_cache_key`、`client_metadata`，不补 Codex mimic body 默认值，并强制 `Accept: application/json`。通过普通 `/v1/responses` body-signal 触发且原请求为 `stream:true` 时，下游桥接为最小 Responses SSE，确保包含 `response.output_item.done` 和 `response.completed`。

能力边界：

- mimic 只对齐 header、body、TLS 和路由，不复制服务端隐藏 prompt、账号状态、产品 memory 或 UI 上下文，也不替换响应文本或清洗客户端正文身份。
- Codex mimic 已使用真正的 HTTP/2 wire，但 Go `x/net/http2` 生成的 SETTINGS、伪 header 顺序和 window 更新等帧级指纹尚未逐字节对齐官方 Rust h2 栈。
- 效果应以第三方中转站实际收到的上游请求为准，不能只看 usage 页面中的客户端入口 `USER-AGENT`。

### 1.2 Antigravity 增强

Antigravity 增强用于让 Antigravity 账号新增后默认可用；新建账号默认白名单、默认映射和 `/models` 收敛到官方抓包确认的 8 个模型，同时保留账号编辑页“自定义模型名称”入口，允许管理员手动把后续新增的 Google / Antigravity 模型加入该账号白名单。

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

规则：

| 主题 | 当前行为 |
|---|---|
| 默认集合 | `model_mapping` 缺失或为空时，旧账号默认允许并展示官方 8 模型；新账号默认写入其自映射。管理员可通过“自定义模型名称”追加模型，手动“同步上游支持的模型”保存真实结果。 |
| 显式白名单 | 非空 `model_mapping` 中规范化后的 `model -> model` 自映射构成唯一允许集合，官方模型不会隐式补回；显式配置但无法解析出有效字符串映射时按空白名单处理。 |
| 映射与别名 | 模型映射保存“外部显示模型 -> 实际发包 model”；普通映射、通配符和 `gemini-3.1-pro-high` 等历史别名只负责解析，最终目标必须命中允许集合，历史别名不进入默认白名单或 `/models`。 |
| 模型广告 | `/antigravity/v1/models` 只展示账号实际允许的界面模型和手动加入的模型，不展示兼容别名。 |
| `web_search` | 固定使用 `gemini-3.5-flash-low`；存在显式白名单时必须保留该模型的自映射，不能绕过白名单。 |
| 官方伪装 | UA 为 `antigravity/hub/2.2.1 darwin/arm64`；默认 8 模型忽略客户端 `thinking` / `output_config.effort`，使用表中固定预算；补 `labels/model_enum/trajectory_id` 和同源 `requestId`，过滤无关 stop / sampling 参数。手动追加模型按其名称发包，无需进入全局官方模型表。 |
| 计费 | 按最终实际发包模型 `UpstreamModel` 查价，日志保留外部模型，且优先于渠道 `requested` / `channel_mapped`；`gpt-oss-120b-medium` 为 `$0.05 / $0.01 / $0.20` 每 1M tokens。 |

### 1.3 账号保活

账号保活用于在 OpenAI / Anthropic API Key 账号空闲超过配置间隔后，通过官方 `codex` / `claude` 客户端在真实项目目录发起低频请求，维持上游账号活跃。

功能边界：

1. 主服务管理配置、账号引用、成本、最近使用时间和历史；`keeper/` 以独立 `sub2apiplus-keeper` sidecar 运行，负责调度官方客户端、worker 目录、会话、日志和本地 Web/API。
2. OpenAI 使用 `codex`，Anthropic 使用 `claude`；仅支持相应平台的 API Key 账号，OAuth、setup-token、upstream 等账号不进入候选。
3. 不同账号可并行执行，同一账号通过 `Running` 状态防止重复执行。
4. keeper 获取按账号、按平台签发的短期 scoped proxy token；官方客户端进程不能获得全局 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN`。
5. 只有当前可调度且具备有效平台 API Key 的账号进入候选；停用、过期、过载、限流、临时不可调度或配额耗尽的账号不返回、不签发 token，恢复后自动重新进入。账号代理入口在实际执行前再次校验状态。
6. 官方客户端单次执行超时默认 2700 秒；`sub2apiplus.timeout_seconds` 仅控制 keeper 调主服务内部接口的超时，默认 180 秒。

保活配置放在“Plus 增强功能 / 账号保活”分组中。账号级设置保存在 `account.Extra`，全局约束和题库保存在 keeper state：

| 配置 | 说明 |
|---|---|
| 保活账号 | 从当前可调度且具备有效凭据的 OpenAI / Anthropic API Key 账号中选择。 |
| 启用保活 | `keeper_keepalive_enabled`，控制是否参与自动保活。 |
| 保活间隔 | `keeper_keepalive_interval_minutes`，默认 8 分钟，最小 1 分钟。 |
| 工作时间 | 默认 `04:00` - `24:00`。 |
| 执行模式 | OpenAI 默认全新会话 `fresh`，Anthropic 默认接续上次会话 `resume_last`。 |
| 模型 | 从账号可用模型列表选择。 |
| 项目 | 从 `SUB2APIPLUS_KEEPER_PROJECTS` 暴露的项目列表选择，keeper 内部映射到 `/workspace/projects/<项目名>`。 |
| 最大输出 token | `keeper_keepalive_max_output_tokens`，默认 512；主服务按该值钳制请求，硬上限为 1024。 |
| prompt 约束和题库 | 全局只读约束、通用问题、项目问题和账号自定义 prompt。 |
| 模型成本 | 复用 sub2apiplus 现有成本和 usage 统计口径。 |

保活触发规则：

1. keeper 按 `scan_interval_seconds` 周期扫描账号；本仓库示例配置为 30 秒，未配置时程序默认 120 秒。
2. 下次触发时间取“最近真实请求”和“最近成功保活”分别加保活间隔后的较晚值；账号持续使用时会自动顺延，不产生额外保活请求。
3. 手动“立即执行”忽略空闲判断，但同账号运行中时不会重复启动。
4. 失败会记录错误和失败次数，并按账号间隔重新排队；客户端断开、服务退出等非业务失败不会一律累计为连续失败。
5. 收到 `SIGTERM` / `Interrupt` 后，keeper 会取消运行上下文、等待任务收尾并关闭持久连接。

页面包含三个视图：

| 视图 | 内容 |
|---|---|
| 概览 | keeper 版本、账号数、成功/失败、运行中账号、24 小时用量/费用、最近结果、下次时间和立即执行；账号列表只展示已启用目标。 |
| 配置 | 管理账号、模型、项目、间隔、工作时间、执行模式、账号 prompt、全局约束和题库；支持全部/已启用/已禁用筛选，已启用优先，同状态按账号 ID 倒序。 |
| 会话历史 | 展示时间、账号、状态、模型、token、费用、摘要、错误、提示词和 assistant 回复；从全部 target 汇总，停用账号的既有记录仍可查看，完整上游/客户端错误保留在错误详情中。 |

## 2. 全新服务器部署

推荐使用 Docker Compose。以下流程统一把 `deploy/docker-compose.local.yml` 保存为运行目录中的 `docker-compose.yml`；后续命令统一使用 `docker compose ...`，不再加 `-f docker-compose.local.yml`。

### 2.1 部署目录

```text
/root/docker/sub2apiplus/
├── app/                 # 主服务 compose、环境变量和数据
└── keeper/
    ├── app/             # keeper compose、配置、数据和 projects/<项目名>
    └── repo/            # keeper 构建源码
```

keeper 的 `projects/<项目名>` 挂载到容器内 `/workspace/projects/<项目名>`。

### 2.2 准备主服务

宿主机先安装 Docker、Docker Compose plugin、`git`、`curl` 和 `openssl`。推荐用一键脚本下载模板、生成 `.env`，并自动填入 `JWT_SECRET`、`TOTP_ENCRYPTION_KEY` 和 `POSTGRES_PASSWORD`：

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -sSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-deploy.sh | bash
```

需要手动审查和准备文件时执行：

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -fsSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-compose.local.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/.env.example -o .env
mkdir -p data postgres_data redis_data
```

主服务 `.env` 至少要设置：

```env
COMPOSE_PROJECT_NAME=sub2apiplus
POSTGRES_PASSWORD=<随机强密码>
JWT_SECRET=<openssl rand -hex 32>
TOTP_ENCRYPTION_KEY=<openssl rand -hex 32>
ADMIN_PASSWORD=<自定义后台密码，可选>
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<openssl rand -hex 32>
SUB2APIPLUS_KEEPER_PROJECTS=homeproxy
# 可选；主服务代理 keeper 时使用。留空默认 http://sub2apiplus-keeper:38090
# SUB2APIPLUS_KEEPER_BASE_URL=http://sub2apiplus-keeper:38090
```

启动主服务：

```sh
docker compose up -d
curl -fsS http://127.0.0.1:8080/health
```

如果没有设置 `ADMIN_PASSWORD`，首次后台密码从主服务日志获取：

```sh
docker compose logs sub2api | grep "admin password"
```

后台地址为 `http://<服务器IP>:8080`。登录后先添加 API 账号，并确认平台、可用模型和模型成本配置正常。

### 2.3 准备 keeper

keeper 镜像构建时会在容器内安装官方 `codex` / `claude` 客户端；它们不进入 sub2apiplus 主镜像，也不要求宿主机单独安装。

准备 keeper 源码和配置：

```sh
mkdir -p /root/docker/sub2apiplus/keeper/app/{data,projects} /root/docker/sub2apiplus/keeper/repo
git clone --depth 1 https://github.com/itv3/sub2apiplus.git /tmp/sub2apiplus-src
cp -a /tmp/sub2apiplus-src/keeper/. /root/docker/sub2apiplus/keeper/repo/
cp /tmp/sub2apiplus-src/keeper/keeper.example.yaml /root/docker/sub2apiplus/keeper/app/keeper.yaml
rm -rf /tmp/sub2apiplus-src
```

下载保活项目。`SUB2APIPLUS_KEEPER_PROJECTS` 使用单级项目名，不接受绝对路径、`..` 或多级路径；同名目录必须放在 keeper 的 `projects` 下：

```sh
cd /root/docker/sub2apiplus/keeper/app/projects
git clone --depth 1 https://github.com/itv3/homeproxy.git homeproxy
```

多个项目用英文逗号分隔，例如 `SUB2APIPLUS_KEEPER_PROJECTS=homeproxy,sub2apiplus` 对应：

```sh
ls /root/docker/sub2apiplus/keeper/app/projects/homeproxy
ls /root/docker/sub2apiplus/keeper/app/projects/sub2apiplus
```

准备 keeper `.env`，其中 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 必须与主服务 `.env` 完全一致：

```env
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<与主服务相同>
KEEPER_BIND_HOST=127.0.0.1
KEEPER_HOST_PORT=38091
KEEPER_WEB_USERNAME=
KEEPER_WEB_PASSWORD=
```

检查 keeper `keeper.yaml`。完整示例为 `keeper/keeper.example.yaml`；提示词题库可先使用示例值，后台保存后持久化到 `/app/data/state.json`：

```yaml
timezone: Asia/Shanghai
scan_interval_seconds: 30
state_path: /app/data/state.json
projects_root: /workspace/projects
runtime_root: /app/data/runtime

sub2apiplus:
  base_url: http://sub2apiplus:8080
  internal_token: ${SUB2APIPLUS_KEEPER_INTERNAL_TOKEN}
  timeout_seconds: 180

web:
  enabled: true
  listen: 0.0.0.0:38090
  username: ${KEEPER_WEB_USERNAME}
  password: ${KEEPER_WEB_PASSWORD}
```

`sub2apiplus.base_url` 必须使用 keeper 容器内可访问的地址，默认主服务容器名对应 `http://sub2apiplus:8080`。

生成 keeper compose：

```sh
cd /root/docker/sub2apiplus/keeper/app
cat > docker-compose.yml <<'YAML'
name: sub2apiplus-keeper

services:
  keeper:
    build:
      context: ../repo
      dockerfile: Dockerfile
    image: sub2apiplus-keeper:latest
    container_name: sub2apiplus-keeper
    restart: unless-stopped
    cap_add:
      - CAP_SYS_ADMIN
    security_opt:
      - seccomp=unconfined
      - apparmor=unconfined
    env_file:
      - ./.env
    environment:
      - KEEPER_CONFIG=/app/keeper.yaml
    volumes:
      - ./keeper.yaml:/app/keeper.yaml:ro
      - ./data:/app/data
      - ./projects:/workspace/projects:ro
    ports:
      - "${KEEPER_BIND_HOST:-127.0.0.1}:${KEEPER_HOST_PORT:-38091}:38090"
    networks:
      - sub2api-network

networks:
  sub2api-network:
    external: true
    name: sub2apiplus_sub2api-network
YAML
```

`sub2api-network.name` 必须与主服务网络一致；设置 `COMPOSE_PROJECT_NAME=sub2apiplus` 后固定为 `sub2apiplus_sub2api-network`，可用 `docker network ls | grep sub2apiplus` 确认。

构建并启动 keeper：

```sh
cd /root/docker/sub2apiplus/keeper/app
docker compose build keeper
docker compose up -d keeper
docker exec sub2apiplus-keeper codex --version
docker exec sub2apiplus-keeper claude --version
```

keeper 需要 `CAP_SYS_ADMIN`、`seccomp=unconfined`、`apparmor=unconfined` 以运行官方客户端和 `bubblewrap` 沙箱；`data`、`runtime`、`projects` 必须持久化。

### 2.4 后台激活和验证

1. 在后台添加 API 账号，确认模型和成本配置正常。
2. 进入“Plus 增强功能 / 账号保活 / 配置”，添加 OpenAI / Anthropic API Key 账号；平台自动选择 `codex` / `claude`。
3. 配置模型、项目、间隔、工作时间、执行模式、全局约束和题库后保存。
4. 点击“立即执行”，到“会话历史”确认回复、token 和费用。

验证命令：

```sh
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:38091/
cd /root/docker/sub2apiplus/app && docker compose ps
cd /root/docker/sub2apiplus/keeper/app && docker compose ps
```

## 3. 运维与升级

### 3.1 主服务常用命令

```sh
cd /root/docker/sub2apiplus/app
docker compose ps
docker compose logs --tail=100 sub2api
docker compose restart sub2api
docker compose pull sub2api
docker compose up -d --no-deps sub2api
```

Docker 镜像发布到 `ghcr.io/itv3/sub2apiplus`，支持 `linux/amd64` 和 `linux/arm64`。常用 tag：

| Tag | 含义 |
|---|---|
| `latest` | 最新稳定镜像。 |
| `<Plus 版本>` | 指定版本，例如 `0.1.151-1`。 |
| `<上游主版本>.<上游次版本>` | 对应 minor 线的最新补丁。 |

### 3.2 关键环境变量

| 变量 | 用途 |
|---|---|
| `COMPOSE_PROJECT_NAME=sub2apiplus` | 固定 Docker 网络名，供 keeper sidecar 接入。 |
| `POSTGRES_PASSWORD` | PostgreSQL 密码，必须固定保存。 |
| `JWT_SECRET` | 登录会话签名密钥，生产环境必须固定。 |
| `TOTP_ENCRYPTION_KEY` | TOTP 加密密钥，生产环境必须固定。 |
| `ADMIN_PASSWORD` | 管理员初始密码；留空时从 `docker compose logs sub2api` 查看自动生成值。 |
| `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` | 主服务与 keeper 的内部鉴权 token，双方必须一致。 |
| `SUB2APIPLUS_KEEPER_BASE_URL` | 主服务代理 keeper Web/API 时使用的地址；写入主服务 `.env` 并由 compose 注入。留空时默认 `http://sub2apiplus-keeper:38090`。 |
| `SUB2APIPLUS_KEEPER_PROJECTS` | 账号保活项目下拉框项目名，多个项目用英文逗号分隔。 |
| `KEEPER_BIND_HOST` | keeper Web 端口绑定地址，默认建议 `127.0.0.1`，由 sub2apiplus 后台代理访问。 |
| `KEEPER_HOST_PORT` | keeper Web 映射到宿主机的端口，默认示例为 `38091`。 |
| `KEEPER_WEB_USERNAME` / `KEEPER_WEB_PASSWORD` | keeper 独立 Web 入口的 Basic Auth；留空或只配置一项时不会放行，只能通过内部 token 或完整 Basic Auth 访问。 |

### 3.3 数据迁移

迁移时停止主服务和 keeper，并打包整个 `/root/docker/sub2apiplus`：

```sh
cd /root/docker/sub2apiplus/app
docker compose down

cd /root/docker/sub2apiplus/keeper/app
docker compose down

cd /root/docker
tar czf sub2apiplus.tar.gz sub2apiplus/
```

在新服务器解压后分别启动：

```sh
cd /root/docker/sub2apiplus/app
docker compose up -d

cd /root/docker/sub2apiplus/keeper/app
docker compose up -d
```

### 3.4 发布和应用更新

发布前先跑定向测试；大范围合并或共享逻辑改动时扩大到 `go test ./...` 和完整前端测试：

```sh
cd /path/to/sub2apiplus/backend
go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service

cd /path/to/sub2apiplus/keeper
go test ./...

cd /path/to/sub2apiplus
make test-frontend
```

Plus 版本在上游版本后追加自定义序号，例如 `0.1.151-1`；Git tag 为 `v0.1.151-1`，镜像 tag 为 `ghcr.io/itv3/sub2apiplus:0.1.151-1`。Release workflow 使用 annotated tag 的 message 生成 Release notes，因此必须使用 `git tag -a -m`；轻量 tag 不包含说明正文。

```sh
cd /path/to/sub2apiplus
VERSION=0.1.151-1
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

AMD64 / ARM64 都只替换应用容器，不动 PostgreSQL、Redis、volume 和 `.env`：

```sh
cd /root/docker/sub2apiplus/app
docker compose pull sub2api
docker compose up -d --no-deps sub2api
docker compose ps
docker compose logs --tail=100 sub2api
curl -fsS http://127.0.0.1:8080/health
```

不要执行 `docker compose down -v`，不要删除 volume，不要覆盖 `.env`。

### 3.5 其它运行能力

Gemini 账号支持三种接入方式：

1. Code Assist OAuth：默认使用内置 Gemini CLI OAuth Client，不需要额外配置。
2. AI Studio OAuth：在 `.env` 设置 `GEMINI_OAUTH_CLIENT_ID` 和 `GEMINI_OAUTH_CLIENT_SECRET`。
3. API Key：直接在后台添加 Gemini API Key 账号。

后台“数据管理”入口当前仅保留兼容诊断；服务端固定返回 `DATA_MANAGEMENT_DEPRECATED`，不建议新部署 `datamanagementd`，也不要按旧流程挂载 `/tmp/sub2api-datamanagement.sock`。数据迁移优先使用第 3.3 节的本地目录迁移流程；数据库备份请在 PostgreSQL / Redis 层独立执行。

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径，不安装 keeper sidecar；需要账号保活时使用 Docker Compose 部署。

TLS fingerprint 内置两类 profile：Anthropic mimic 使用 Claude CLI / Node.js 风格 profile（ALPN `http/1.1`）；OpenAI Codex mimic 默认使用 `codex_exec/0.144.1` 抓包 profile（ALPN `h2, http/1.1`，含 `X25519MLKEM768` 后量子混合组，无 GREASE 的 Rustls 指纹），并在 ALPN 协商为 `h2` 时经 `x/net/http2` 走真正的 HTTP/2 wire；`cli_rs_0_125` 可回滚到 Node.js 风格 profile（h1）。API Key 官方客户端兼容需要在账号 `extra` 中启用对应 mimic 开关和 `enable_tls_fingerprint`，具体行为以第 1 节和代码常量为准。

## 4. 维护参考

### 4.1 keeper 内部接口

sub2apiplus 提供内部接口给 keeper 和 Plus 增强功能页面使用。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活、当前可调度且具备有效平台 API Key 凭据的 OpenAI / Anthropic API Key 账号，以及模型、prompt、项目、最大输出 token、最近使用时间、下一次时间和 due 判断。 |
| `GET /api/v1/internal/keeper/projects` | 返回可在保活配置页选择的项目列表，来源为 `SUB2APIPLUS_KEEPER_PROJECTS`。 |
| `GET /api/v1/internal/keeper/state` | 代理 keeper 状态，用于概览、会话历史和运行状态展示。 |
| `GET /api/v1/internal/keeper/settings` | 读取 keeper 版本、全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/settings` | 保存全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/run?target=<target>` | 立即执行指定保活目标；`target` 可匹配目标 ID、账号 ID 或账号名称。 |
| `GET /api/v1/internal/keeper/accounts/:id/models` | 返回该账号可用于保活的模型列表。 |
| `POST /api/v1/internal/keeper/accounts/:id/keepalive` | keeper sidecar 回写状态、token、费用和错误信息；带 `prompt` 的主服务直连执行请求会被拒绝。 |
| `GET/POST /api/v1/internal/keeper/openai/accounts/:id/*` | Codex 代理；POST 仅允许 `/v1/responses`、`/responses`、`/v1/chat/completions`、`/chat/completions`，GET 仅允许 `/v1/models`、`/models`、`/v1/responses/*`、`/responses/*`。 |
| `GET/POST /api/v1/internal/keeper/anthropic/accounts/:id/*` | Claude 代理；POST 仅允许 `/v1/messages`、`/v1/messages/count_tokens`，GET 仅允许 `/v1/models`。 |

| 接口类型 | 允许的鉴权 |
|---|---|
| 账号列表、项目、state、settings、立即执行和模型列表 | 全局内部 token 或后台 admin auth。 |
| `keepalive` 回写 | 仅全局内部 token。 |
| OpenAI / Anthropic 账号代理 | 仅主服务按账号和平台签发的 scoped proxy token，不接受 admin auth。 |

代理路径拒绝 query、fragment、`.`、`..`、`%2e` 等不安全片段；请求和响应 header 使用显式 allowlist，避免 `Cookie`、`Set-Cookie`、账号密钥、上游鉴权信息或未脱敏日志越界。

keeper 通过 `max_output_tokens` 把账号级最大输出 token 传回主服务。主服务内部代理会按该值钳制 OpenAI Responses 的 `max_output_tokens`、OpenAI Chat Completions 的 `max_completion_tokens` / `max_tokens`，以及 Anthropic Messages 的 `max_tokens`；请求未带这些字段时会补默认值，超过上限时会降到账号配置值。

### 4.2 Plus UI 入口

Plus UI 细节以代码为准。上游合并后确认：

1. 侧边栏和路由仍能进入“Plus 增强功能”，页面保留 API Key 官方客户端兼容和账号保活分组。
2. mimic 支持全部/已开启/未开启筛选并优先显示已开启账号；保活配置支持全部/已启用/已禁用筛选。
3. 保活概览只展示启用目标，会话历史仍可读取停用目标的既有记录。
4. Antigravity 账号编辑保留模型白名单和模型映射入口。

### 4.3 v0.1.151 差异文件清单

本节只记录差异口径和审核入口，不维护一份容易过期的全量文件清单。

基线说明：

- 发布基线是官方上游 tag `v0.1.151`。
- 当前分支已通过合并提交同步官方 `v0.1.151`；审核 Plus 自定义实现时，应优先看 `v0.1.151..HEAD` 中 API Key mimic、Antigravity、keeper 和 Plus UI 相关文件。
- `batch_image` 已包含在上游发布内容中，不属于 Plus 三项增强的自研范围；不要把上游批量生图代码误算进 Plus 自定义差异。
- `keeper/` 是当前新增源码目录；`.codex-captures/` 是本地抓包样本，`.kilo/` 是本地工具配置，不计入源码清单。

需要完整差异时直接生成：

```sh
git diff --name-only v0.1.151..HEAD
git diff --stat v0.1.151..HEAD
```

Plus 审核重点文件组：

| 范围 | 入口文件 |
|---|---|
| API Key mimic | `backend/internal/service/*apikey_mimic*`、OpenAI gateway/scheduler/WS 相关 service、`backend/internal/pkg/claude/constants.go`、`backend/internal/pkg/tlsfingerprint/`、`backend/internal/repository/http_upstream.go`、`backend/internal/handler/openai_gateway_handler.go`。 |
| Antigravity | `backend/internal/pkg/antigravity/`、`backend/internal/service/antigravity_*`、`upstream_models.go`、`model_rate_limit.go`、`backend/resources/model-pricing/model_prices_and_context_window.json`。 |
| 账号保活 | `keeper/`、`backend/internal/handler/admin/account_handler_keeper.go`、`backend/internal/service/*keeper*`、`backend/internal/server/routes/admin.go`。 |
| Plus UI | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、账号 API、路由、侧边栏、i18n、账号创建/编辑和模型白名单相关组件。 |
| 发布部署 | `README.md`、`deploy/.env.example`、`deploy/docker-compose.local.yml`、`.github/workflows/release.yml`、`backend/cmd/server/VERSION`。 |

### 4.4 上游合并检查

合并上游后按第 1 节功能规则重点确认：

**API Key mimic**

- 只对非官方客户端触发，官方 Claude / Codex 客户端回到 passthrough 或普通 API Key 逻辑；命中 mimic 时关键身份头不被账号 header override 覆盖。
- Anthropic 使用 mimic 专用完整 beta 列表，不影响普通 API Key；`/v1/messages` 与 `/v1/messages/count_tokens` 保持第 1.1 节的独立构造边界，工具名归一和 per-request reverseMap 只修改结构化工具字段。
- Anthropic 仅在启用 TLS fingerprint 且命中 mimic 时使用 Claude CLI 2.1.207 / Node.js 26 profile；平台、账号类型或客户端不匹配时不得套用。
- OpenAI mimic 强制 HTTP，跳过 mimic 后账号调度、previous response 粘连复核和最终转发都恢复普通 WS/HTTP 路由；`/v1/messages` 固定走 Responses mimic，compact 保持上游 JSON 并按需桥接下游 SSE。
- `desktop_0_142` 仍为默认 profile；`codex_exec/0.144.1` 保持 ALPN `h2, http/1.1`、`X25519MLKEM768`、无 GREASE 的 11 extension 顺序，并通过 `x/net/http2` 走真实 HTTP/2；`cli_rs_0_125` 回滚 Node.js 风格 h1。
- HTTP/1.1 与 HTTP/2 Transport 均保持 `DisableCompression=true`，避免自动注入 gzip，同时不影响显式压缩响应的受控解压；Responses capability probe 继续按 mimic 状态分键。
- `CodexBaseInstructionsForModel()` 保持 `gpt-5.5` / `gpt-5.2` 策略，未单独维护 prompt 的后续版本回退到最新版本（当前 GPT-5.5）。

**Antigravity**

- 新账号默认白名单、映射和 `/models` 统一为官方 8 模型，不产生重复模型；自定义模型和上游同步结果仍按真实配置保存。
- 显式白名单只由自映射构成，请求校验与 `/models` 使用同一允许集合；默认表、别名、通配符和 `web_search` 都不能重新放开已移除模型。
- 官方 UA、固定 `thinkingBudget`、labels、同源 `requestId` 和最终 `UpstreamModel` 计费保持有效，包括 `gpt-oss-120b-medium` 的既定价格。

**Keeper 与 UI**

- 账号列表、项目、settings/state、立即执行、最大输出 token 钳制和会话回写保持对齐；候选和实际代理都校验可调度状态，恢复后自动重新进入。
- Plus 路由、侧边栏、i18n、账号 API、统一设置页及 Antigravity 编辑入口与后端保持一致。
- Release / Docker 继续发布 `ghcr.io/itv3/sub2apiplus`，应用更新只替换 app 容器。

重点文件以第 4.3 节清单为准；合并时优先看 API Key mimic、Antigravity、keeper 和 Plus UI 四组。

### 4.5 Mimic 对齐原则

继续提升官方客户端一致性时，不直接盲改源码，按下面顺序处理：

1. 采集真实官方客户端请求。
2. 采集经 sub2apiplus 的伪装请求。
3. 建差异表。
4. 用失败请求做 A/B 消融重放，每次只改一个变量或一个可解释变量组。
5. 只对高置信差异改源码。
6. 每改一项都做官方客户端和第三方客户端双向回归。

后续维护方向：

- 继续对齐官方客户端 body identity、system prompt、metadata 和工具调用形态。
- 为 Anthropic / OpenAI 提供独立 profile，包括 `anthropic_apikey_mimic_official_identity` 和 `anthropic_apikey_mimic_profile`，避免客户端形态互相污染。
- 展示账号实际出站 profile、TLS fingerprint 状态和最近一次 mimic 的脱敏诊断摘要，并提供仅脱敏导出的抓包入口。
- 增加脱敏差异采集和 A/B 消融辅助能力，以真实失败样本决定源码调整。
- UI 不得展示密钥、token、authorization 或 x-api-key；高风险 body cloaking 开关默认关闭或仅灰度启用。

## 5. 其它文档索引

以下文档位于 `docs/`，不属于 Plus 三项增强的主线说明，但部署或二次开发时可能用到：

| 文档 | 说明 |
|---|---|
| [`docs/PAYMENT_CN.md`](docs/PAYMENT_CN.md) | 支付能力中文说明。 |
| [`docs/PAYMENT.md`](docs/PAYMENT.md) | 支付能力英文说明。 |
| [`docs/ADMIN_PAYMENT_INTEGRATION_API.md`](docs/ADMIN_PAYMENT_INTEGRATION_API.md) | 管理端支付集成 API。 |
| [`docs/BATCH_IMAGE_MVP.md`](docs/BATCH_IMAGE_MVP.md) | 批量生图 MVP（上游能力，非 Plus 自研三项增强）。 |
| [`docs/legal/admin-compliance.zh.md`](docs/legal/admin-compliance.zh.md) | 管理端合规说明（中文）。 |
| [`docs/legal/admin-compliance.en.md`](docs/legal/admin-compliance.en.md) | 管理端合规说明（英文）。 |

## 6. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
