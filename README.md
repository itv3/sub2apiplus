# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的自用增强版 fork。维护目标是长期跟随上游升级，同时保留自建镜像、私有部署和 Plus 增强功能。

## 0. 当前状态

| 项 | 结论 |
|---|---|
| 仓库 | `https://github.com/itv3/sub2apiplus` |
| 上游 | `https://github.com/Wei-Shaw/sub2api` |
| Docker 镜像 | `ghcr.io/itv3/sub2apiplus` |
| Go module | 继续保留 `github.com/Wei-Shaw/sub2api` |
| Compose service | 继续保留 `sub2api`，用于 `docker compose pull/up/logs sub2api` 等命令。 |
| 默认容器名 | `sub2apiplus`、`sub2apiplus-postgres`、`sub2apiplus-redis`。 |

维护原则：

1. 长期跟随上游主线，发布版本按上游 release/tag 对齐。
2. 对外名称使用 `sub2apiplus` / `Sub2API Plus`，内部 Go module 和 import 尽量保留上游命名，降低合并成本。
3. Plus 增强功能通过统一入口管理，账号级开关优先放入 `account.Extra`。
4. Docker 更新默认只替换应用容器，不动 PostgreSQL、Redis、Nginx、`.env` 和数据目录。

## 1. Plus 增强功能

管理后台侧边栏通过“Plus 增强功能”统一进入，路由为 `/admin/settings/plus-enhancements`。

| 功能 | 定位 | 配置归属 |
|---|---|---|
| API Key 账号伪装 | 让第三方客户端经 sub2apiplus 转发时尽量接近官方 Claude / Codex 客户端请求形态。 | `sub2apiplus` 管理后台和账号 `account.Extra`。 |
| Antigravity 增强 | 统一 Antigravity 账号的默认模型、模型映射、官方伪装和成本口径。 | `sub2apiplus` 管理后台、模型配置和成本表。 |
| 账号保活 | 通过官方 `codex` / `claude` 客户端对空闲账号做低频真实项目提问，保持上游账号活跃。 | 业务配置在 `sub2apiplus`，执行器运行在 keeper sidecar。 |

### 1.1 API Key 账号伪装

API Key 账号伪装用于让 Kilo / Cline / Cursor / Roo Code 等非官方客户端经 sub2apiplus 转发到第三方中转站 API 时，尽量接近“Claude / Codex 官方客户端直连该中转站 API”的 header、body、TLS 和路由形态。

核心规则：

1. 仅 Anthropic / OpenAI 的 API Key 账号生效，不改变 OAuth 账号逻辑。
2. mimic 与 passthrough 运行时互斥；同时开启时优先走 mimic。
3. 测试连接也走官方客户端请求形态：OpenAI 复用 Codex mimic profile，Anthropic 构造 Claude Code 风格请求。

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
| Anthropic API Key | Claude Code | 独立处理 `/v1/messages` 和 `count_tokens`，补 Claude Code header / beta / TLS / body 形态，做已知工具名归一和响应侧结构化回写。 |
| OpenAI API Key | Codex | `desktop_0_142` 默认模拟 Codex Desktop；`cli_rs_0_125` 可回滚旧 Codex CLI。Responses 路由、body 默认值、`prompt_cache_key`、`client_metadata`、TLS profile 和 capability probe 都按 mimic profile 处理。 |

能力边界：

- 不承诺 100% 复制官方客户端；服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 上下文和 HTTP/2 wire 指纹都可能超出 sub2apiplus 能力范围。
- mimic 主要伪装 header、body、TLS 和路由形态；不做响应文本替换，也不清洗原客户端在正文、工具描述、路径或能力提示里的身份信息。
- 判断 mimic 效果应比较第三方中转站看到的上游入站请求形态，不能只看 ARM64 usage 页面里的客户端入口 `USER-AGENT`。

关键文件：

`backend/internal/service/account_apikey_mimicry.go`、`gateway_apikey_mimicry.go`、`gateway_tool_rewrite.go`、`openai_apikey_mimicry.go`、`openai_apikey_mimic_profile.go`、`openai_gateway_chat_completions.go`、`openai_gateway_service.go`、`openai_codex_transform.go`、`openai_upstream_http.go`、`account_test_service.go`、`account.go`、`backend/internal/pkg/openai/constants.go`。

### 1.2 Antigravity 增强

Antigravity 增强用于让 Antigravity 账号新增后默认可用；白名单、映射、`/models` 和实际发包都统一到官方抓包确认的 8 个模型。

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

1. 模型白名单保存官方发包 model。
2. 模型映射保存“外部显示模型 -> 官方发包 model”。
3. 外部 `/antigravity/v1/models` 默认只展示界面模型，不展示兼容别名。
4. `gemini-3.1-pro-high` 这类历史兼容别名可继续接收请求，但不进入默认白名单和 `/models`。
5. 账号自定义 `model_mapping` 时，历史兼容别名只有在目标官方模型也被该账号白名单 / 映射允许时才放行。

已实现能力：

| 功能 | 当前行为 |
|---|---|
| 账号编辑 | Antigravity 与 OpenAI / Anthropic 一样支持“模型白名单 / 模型映射”。 |
| 模型收敛 | 新建账号、同步按钮、上游同步和 `/models` 都统一到官方 8 模型。 |
| 官方伪装 | UA 使用 `antigravity/hub/2.2.1 darwin/arm64`；官方 8 模型忽略客户端 `thinking` / `output_config.effort` 对上游预算的覆盖，始终按抓包 fixed `thinkingBudget` 发送；补 `labels/model_enum/trajectory_id` 和同源 `requestId`，并过滤无关 stop / sampling 参数。 |
| 成本 | `gpt-oss-120b-medium` 按 `$0.05 / $0.01 / $0.20` 每 1M tokens 计价。 |

关键文件：

`backend/internal/pkg/antigravity/claude_types.go`、`backend/internal/pkg/antigravity/oauth.go`、`backend/internal/pkg/antigravity/gemini_types.go`、`backend/internal/pkg/antigravity/request_transformer.go`、`backend/internal/service/upstream_models.go`、`backend/internal/service/gateway_service.go`、`backend/internal/handler/gateway_handler.go`、`frontend/src/composables/useModelWhitelist.ts`、`frontend/src/components/account/CreateAccountModal.vue`、`frontend/src/components/account/EditAccountModal.vue`、`frontend/src/components/account/ModelWhitelistSelector.vue`、`backend/resources/model-pricing/model_prices_and_context_window.json`。完整差异文件见第 4.3 节。

### 1.3 账号保活

账号保活用于在 OpenAI / Anthropic 账号空闲超过配置间隔后，通过官方 `codex` / `claude` 客户端在真实项目目录发起低频请求，维持上游账号活跃。

功能边界：

1. 保活配置、账号引用、成本口径、最近使用时间和历史展示都归 sub2apiplus 管理。
2. keeper 源码位于主仓库 `keeper/`，随 sub2apiplus 一起维护；运行时是独立 sidecar 容器 `sub2apiplus-keeper`，不嵌入主服务进程。
3. keeper 负责调度、运行 `codex` / `claude` 官方客户端、维护 worker 目录、会话文件和执行日志，同时提供本地 Web/API 供主服务代理状态、设置和立即执行。
4. 官方客户端由账号平台自动选择：OpenAI 使用 `codex`，Anthropic 使用 `claude`。
5. 不同账号到期后可并行执行，同一个账号通过 `Running` 状态防止重复执行。
6. 官方客户端单次执行超时默认 2700 秒；`sub2apiplus.timeout_seconds` 只是 keeper 调主服务内部接口的超时，默认 180 秒。

保活配置放在“Plus 增强功能 / 账号保活”分组中：

| 配置 | 说明 |
|---|---|
| 保活账号 | 从现有 OpenAI / Anthropic 账号中选择；实际执行能力取决于账号上游凭据和测试路径是否可用。 |
| 启用保活 | 写入 `account.Extra.keeper_keepalive_enabled`。 |
| 保活间隔 | 写入 `account.Extra.keeper_keepalive_interval_minutes`，默认 8 分钟，最小 1 分钟。 |
| 工作时间 | 写入账号 `extra`，默认 `04:00` - `24:00`。 |
| 执行模式 | OpenAI 默认全新会话 `fresh`，Anthropic 默认接续上次会话 `resume_last`。 |
| 模型 | 从账号可用模型列表选择。 |
| 工作目录 | 从 `SUB2APIPLUS_KEEPER_PROJECTS` 暴露的项目列表选择。 |
| prompt 约束和题库 | 包括全局只读约束、通用问题、项目问题和账号自定义 prompt；全局约束和题库保存在 keeper state。 |
| 模型成本 | 复用 sub2apiplus 现有成本和 usage 统计口径。 |

保活触发规则：

1. keeper 按 `scan_interval_seconds` 周期扫描账号；本仓库示例配置为 30 秒，未配置时程序默认 120 秒。
2. 触发时间取“最近真实请求时间 + 保活间隔”和“最近成功保活完成时间 + 保活间隔”中较晚者。
3. 只要账号一直正常使用，最近真实请求时间会不断向后更新，保活时间随之顺延，不会额外产生保活请求。
4. 手动“立即执行”会忽略空闲判断，但同账号运行中时不会重复启动。
5. 保活失败会记录错误和失败次数，下一次仍按账号配置的保活间隔重新排队。

页面包含三个视图：

| 视图 | 内容 |
|---|---|
| 概览 | keeper 版本、账号数、今日成功 / 失败、运行中账号、24 小时用量 / 费用、最近结果、下次保活时间和立即执行入口。 |
| 配置 | 添加 / 编辑保活账号，选择模型、工作目录、间隔、工作时间、执行模式、账号 prompt，并维护全局约束提示词和提示词题库。 |
| 会话历史 | 展示每次保活的时间、账号、状态、模型、token、费用、结果摘要、错误、提示词和模型回复。 |

## 2. 全新服务器部署

推荐使用 Docker Compose。以下流程统一把 `deploy/docker-compose.local.yml` 保存为运行目录中的 `docker-compose.yml`；后续命令统一使用 `docker compose ...`，不再加 `-f docker-compose.local.yml`。

### 2.1 部署目录

| 项 | 路径 / 说明 |
|---|---|
| 主服务 app | `/root/docker/sub2apiplus/app`，保存 `docker-compose.yml`、`.env`、`data`、`postgres_data`、`redis_data`。 |
| keeper 目录 | `/root/docker/sub2apiplus/keeper`。 |
| keeper app | `/root/docker/sub2apiplus/keeper/app`，保存 `docker-compose.yml`、`.env`、`keeper.yaml` 和 `data`。 |
| keeper 源码 | `/root/docker/sub2apiplus/keeper/repo`。 |
| keeper 项目 | `/root/docker/sub2apiplus/keeper/app/projects/<项目名>`，挂载到容器内 `/workspace/projects/<项目名>`。 |

### 2.2 准备主服务

宿主机先安装 Docker、Docker Compose plugin、`git`、`curl` 和 `openssl`。

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -fsSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-compose.local.yml -o docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/.env.example -o .env
mkdir -p data postgres_data redis_data
```

也可以用一键准备脚本生成同样的运行目录；脚本会下载 compose 模板、生成 `.env`，并自动填入 `JWT_SECRET`、`TOTP_ENCRYPTION_KEY` 和 `POSTGRES_PASSWORD`：

```sh
mkdir -p /root/docker/sub2apiplus/app
cd /root/docker/sub2apiplus/app
curl -sSL https://raw.githubusercontent.com/itv3/sub2apiplus/main/deploy/docker-deploy.sh | bash
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

keeper 镜像构建时会在容器内安装官方客户端；它们不进入 sub2apiplus 主镜像，也不要求宿主机单独安装。`keeper/Dockerfile` 中执行的安装逻辑如下：

```sh
curl -fsSL https://chatgpt.com/codex/install.sh | sh
curl -fsSL https://claude.ai/install.sh | bash
ln -sf /root/.local/bin/claude /usr/local/bin/claude
codex --version
claude --version
```

准备 keeper 源码和配置：

```sh
mkdir -p /root/docker/sub2apiplus/keeper/app/{data,projects} /root/docker/sub2apiplus/keeper/repo
git clone --depth 1 https://github.com/itv3/sub2apiplus.git /tmp/sub2apiplus-src
cp -a /tmp/sub2apiplus-src/keeper/. /root/docker/sub2apiplus/keeper/repo/
cp /tmp/sub2apiplus-src/keeper/keeper.example.yaml /root/docker/sub2apiplus/keeper/app/keeper.yaml
rm -rf /tmp/sub2apiplus-src
```

下载保活项目。`SUB2APIPLUS_KEEPER_PROJECTS` 写的是项目名，目录必须放在 keeper 的 `projects` 下；项目名只允许单级目录名，不接受绝对路径、`..` 或多级路径：

```sh
cd /root/docker/sub2apiplus/keeper/app/projects
git clone --depth 1 https://github.com/itv3/homeproxy.git homeproxy
```

多个项目用英文逗号分隔，例如：

```env
SUB2APIPLUS_KEEPER_PROJECTS=homeproxy,sub2apiplus
```

对应目录必须同时存在：

```text
/root/docker/sub2apiplus/keeper/app/projects/homeproxy
/root/docker/sub2apiplus/keeper/app/projects/sub2apiplus
```

准备 keeper `.env`，其中 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 必须与主服务 `.env` 完全一致：

```env
SUB2APIPLUS_KEEPER_INTERNAL_TOKEN=<与主服务相同>
KEEPER_BIND_HOST=127.0.0.1
KEEPER_HOST_PORT=38091
KEEPER_WEB_USERNAME=
KEEPER_WEB_PASSWORD=
```

检查 keeper `keeper.yaml`。提示词种子可以直接写在这里；后台保存后会持久化到 `/app/data/state.json`，后续以 state 中的值为准：

```yaml
timezone: Asia/Shanghai
scan_interval_seconds: 30
state_path: /app/data/state.json
projects_root: /workspace/projects
runtime_root: /app/data/runtime

prompt_guard: 注意：仅进行代码分析，在没有得到我的明确同意前禁止修改任何代码。

prompt_bank:
  - id: global-001
    scope: global
    enabled: true
    text: 先看一下这个仓库的 README 和目录结构，帮我判断它主要解决什么问题，核心入口大概在哪里。
  - id: global-002
    scope: global
    enabled: true
    text: 帮我从项目结构入手，找出最值得先理解的三个模块，并说明它们之间可能怎么协作。
  - id: homeproxy-001
    scope: project
    project_path: /workspace/projects/homeproxy
    enabled: true
    text: 先看一下 homeproxy 的 README 和目录结构，帮我概括它在 OpenWrt 上提供哪些代理相关能力。

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

`sub2apiplus.base_url` 使用 keeper 容器内可访问的主服务地址。本仓库默认主服务容器名为 `sub2apiplus`，因此默认写 `http://sub2apiplus:8080`。

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

`sub2api-network.name` 必须与主服务 compose 创建的网络一致。主服务 `.env` 已设置 `COMPOSE_PROJECT_NAME=sub2apiplus` 时，网络名固定为 `sub2apiplus_sub2api-network`；可以用 `docker network ls | grep sub2apiplus` 确认。

构建并启动 keeper：

```sh
cd /root/docker/sub2apiplus/keeper/app
docker compose build keeper
docker compose up -d keeper
docker exec sub2apiplus-keeper codex --version
docker exec sub2apiplus-keeper claude --version
```

keeper 容器需要 `CAP_SYS_ADMIN`、`seccomp=unconfined`、`apparmor=unconfined`，用于满足官方客户端和 `bubblewrap` 沙箱运行要求。keeper 的 `data`、`runtime`、`projects` 必须持久化，容器重建不能丢失会话、日志和项目目录。

### 2.4 后台激活和验证

1. 进入 sub2apiplus 后台，添加 API 账号，确认模型和成本配置正常。
2. 进入“Plus 增强功能 / 账号保活 / 配置”，点击“添加账号”。
3. 从现有 OpenAI / Anthropic 账号中选择账号；平台自动决定 OpenAI 使用 `codex`、Anthropic 使用 `claude`。
4. 选择模型、工作目录、保活间隔、工作时间和执行模式，保存。
5. 在配置页维护全局约束提示词和提示词题库；保存后写入 keeper state。
6. 点击“立即执行”，再到“会话历史”确认有回复、token 和费用。

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
| `<Plus 版本>` | 指定版本，例如 `0.1.146-1`。 |
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
| `SUB2APIPLUS_KEEPER_BASE_URL` | 主服务代理 keeper Web/API 时使用的地址；留空时默认 `http://sub2apiplus-keeper:38090`。 |
| `SUB2APIPLUS_KEEPER_PROJECTS` | 账号保活工作目录下拉框项目名，多个项目用英文逗号分隔。 |
| `KEEPER_BIND_HOST` | keeper Web 端口绑定地址，默认建议 `127.0.0.1`，由 sub2apiplus 后台代理访问。 |
| `KEEPER_HOST_PORT` | keeper Web 映射到宿主机的端口，默认示例为 `38091`。 |
| `KEEPER_WEB_USERNAME` / `KEEPER_WEB_PASSWORD` | keeper 独立 Web 入口的 Basic Auth；留空或只配置一项时不会放行，只能通过内部 token 或完整 Basic Auth 访问。 |

### 3.3 数据迁移

使用本地目录版 compose 时，迁移整套服务需要停止主服务和 keeper，然后打包 `/root/docker/sub2apiplus` 运行目录：

```sh
cd /root/docker/sub2apiplus/app
docker compose down

cd /root/docker/sub2apiplus/keeper/app
docker compose down

cd /root/docker
tar czf sub2apiplus.tar.gz sub2apiplus/
```

恢复到新服务器后解压并执行：

```sh
cd /root/docker/sub2apiplus/app
docker compose up -d

cd /root/docker/sub2apiplus/keeper/app
docker compose up -d
```

### 3.4 发布和 ARM64 更新

发布前测试：

```sh
cd /path/to/sub2apiplus/backend
go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service

cd /path/to/sub2apiplus
make test-frontend
```

大范围合并或共享逻辑改动时扩大为：

```sh
cd /path/to/sub2apiplus/backend
go test ./...

cd /path/to/sub2apiplus
make test-frontend
```

版本规则：

| 项 | 规则 | 示例 |
|---|---|---|
| Plus 版本 | 上游版本后追加自定义序号 | `0.1.146-1` |
| Git tag | `v<Plus 版本>` | `v0.1.146-1` |
| GHCR 镜像 | `ghcr.io/itv3/sub2apiplus:<Plus 版本>` | `ghcr.io/itv3/sub2apiplus:0.1.146-1` |

发布：

```sh
cd /path/to/sub2apiplus
VERSION=0.1.146-1
echo "$VERSION" > backend/cmd/server/VERSION
git add backend/cmd/server/VERSION
git commit -m "chore: release v${VERSION}"
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

### 3.5 其它运行能力

Gemini 账号支持三种接入方式：

1. Code Assist OAuth：默认使用内置 Gemini CLI OAuth Client，不需要额外配置。
2. AI Studio OAuth：在 `.env` 设置 `GEMINI_OAUTH_CLIENT_ID` 和 `GEMINI_OAUTH_CLIENT_SECRET`。
3. API Key：直接在后台添加 Gemini API Key 账号。

如需启用后台“数据管理”功能，需要额外安装宿主机 `datamanagementd`，并把宿主机 `/tmp/sub2api-datamanagement.sock` 挂载到主服务容器同路径。

```sh
sudo ./deploy/install-datamanagementd.sh --binary /path/to/datamanagementd
# 或从源码构建安装
sudo ./deploy/install-datamanagementd.sh --source /path/to/sub2api
```

Docker 场景建议在 `docker-compose.override.yml` 中挂载 Socket：

```yaml
services:
  sub2api:
    volumes:
      - /tmp/sub2api-datamanagement.sock:/tmp/sub2api-datamanagement.sock
```

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径，不安装 keeper sidecar；需要账号保活时使用 Docker Compose 部署。

TLS fingerprint 默认内置 Claude CLI / Node.js 风格 profile。API Key 账号伪装需要在账号 `extra` 中启用对应 mimic 开关和 `enable_tls_fingerprint`，具体行为以第 1 节和代码常量为准。

## 4. 维护参考

### 4.1 keeper 内部接口

sub2apiplus 提供内部接口给 keeper 和 Plus 增强功能页面使用。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活的账号、模型、prompt、项目目录、最近使用时间、下一次时间和 due 判断。 |
| `GET /api/v1/internal/keeper/projects` | 返回可在保活配置页选择的项目目录列表，来源为 `SUB2APIPLUS_KEEPER_PROJECTS`。 |
| `GET /api/v1/internal/keeper/state` | 代理 keeper 状态，用于概览、会话历史和运行状态展示。 |
| `GET /api/v1/internal/keeper/settings` | 读取 keeper 版本、全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/settings` | 保存全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/run?target=<target>` | 立即执行指定保活目标；`target` 可匹配目标 ID、账号 ID 或账号名称。 |
| `GET /api/v1/internal/keeper/accounts/:id/models` | 返回该账号可用于保活的模型列表。 |
| `POST /api/v1/internal/keeper/accounts/:id/keepalive` | 带 `prompt` 时由 sub2apiplus 直接执行保活；不带 `prompt` 时用于 keeper 回写状态、token、费用和错误信息。 |
| `ANY /api/v1/internal/keeper/openai/accounts/:id/*` | Codex 官方客户端的 OpenAI 代理入口，仅允许 `/v1/responses`、`/responses`、`/v1/chat/completions`、`/chat/completions`、`/v1/models`、`/models`。 |
| `ANY /api/v1/internal/keeper/anthropic/accounts/:id/*` | Claude 官方客户端的 Anthropic 代理入口，仅允许 `/v1/messages`、`/v1/messages/count_tokens`、`/v1/models`。 |

内部接口优先使用 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 鉴权，keeper 通过 `x-api-key` 或 `Authorization: Bearer` 调用；后台管理员请求仍可走现有 admin auth。代理路径会拒绝 query、fragment、`.`、`..`、`%2e` 等不安全片段，并剥离上游认证头和 hop-by-hop header。接口不能暴露账号密钥、上游 `Authorization`、上游 `x-api-key` 或未脱敏日志。

### 4.2 Plus UI 入口

Plus 增强功能的 UI 细节以代码为准，README 只记录入口和维护文件，避免把页面按钮说明写成另一份容易过期的手册。

| 范围 | 关键文件 |
|---|---|
| 路由入口 | `frontend/src/router/index.ts` |
| 侧边栏入口 | `frontend/src/components/layout/AppSidebar.vue` |
| Plus 增强页面 | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue` |
| 账号 API 封装 | `frontend/src/api/admin/accounts.ts` |
| 文案 | `frontend/src/i18n/locales/zh.ts`、`frontend/src/i18n/locales/en.ts` |
| Antigravity 模型配置 UI | `frontend/src/composables/useModelWhitelist.ts`、`frontend/src/components/account/CreateAccountModal.vue`、`frontend/src/components/account/EditAccountModal.vue`、`frontend/src/components/account/ModelWhitelistSelector.vue` |

上游合并后需要确认：后台侧边栏仍能进入“Plus 增强功能”；页面仍包含 API Key 账号伪装和账号保活分组；账号保活的概览、配置、会话历史仍能正常加载；Antigravity 账号编辑仍保留模型白名单和模型映射入口。

### 4.3 v0.1.146 差异文件清单

本清单以官方上游 tag `v0.1.146` 为基线，覆盖当前工作树中 Plus / fork 自定义触及的文件。`keeper/` 是当前新增源码目录；`.codex-captures/` 是本地抓包样本，`.kilo/` 是本地工具配置，不计入源码清单。

品牌、发布、镜像、部署和文档收敛：

```text
.github/workflows/cla.yml
.github/workflows/release.yml
.goreleaser.simple.yaml
.goreleaser.yaml
DEV_GUIDE.md
Dockerfile
Dockerfile.goreleaser
README.md
README_CN.md
README_JA.md
backend/cmd/server/VERSION
backend/internal/payment/provider/wxpay_test.go
backend/internal/service/auth_email_binding.go
backend/internal/service/auth_oauth_email_flow.go
backend/internal/service/auth_service.go
backend/internal/service/balance_notify_service.go
backend/internal/service/balance_notify_service_test.go
backend/internal/service/notification_email_service_test.go
backend/internal/service/totp_service.go
backend/internal/service/user_service.go
backend/internal/web/embed_test.go
deploy/.env.example
deploy/DATAMANAGEMENTD_CN.md
deploy/DOCKER.md
deploy/Dockerfile
deploy/README.md
deploy/build_image.sh
deploy/config.example.yaml
deploy/docker-compose.dev.yml
deploy/docker-compose.local.yml
deploy/docker-compose.standalone.yml
deploy/docker-compose.yml
deploy/docker-deploy.sh
deploy/install.sh
deploy/sub2api.service
docs/ADMIN_PAYMENT_INTEGRATION_API.md
frontend/index.html
frontend/package.json
frontend/pnpm-lock.yaml
frontend/src/api/index.ts
frontend/src/api/__tests__/settings.authSourceDefaults.spec.ts
frontend/src/components/admin/AdminComplianceDialog.vue
frontend/src/components/auth/__tests__/WechatOAuthSection.spec.ts
frontend/src/components/layout/AppHeader.vue
frontend/src/components/layout/AuthLayout.vue
frontend/src/components/layout/README.md
frontend/src/components/user/profile/__tests__/ProfileIdentityBindingsSection.spec.ts
frontend/src/components/user/profile/__tests__/totp-timer-cleanup.spec.ts
frontend/src/main.ts
frontend/src/router/README.md
frontend/src/router/title.ts
frontend/src/router/__tests__/title.spec.ts
frontend/src/router/__tests__/wechat-route.spec.ts
frontend/src/stores/README.md
frontend/src/stores/adminCompliance.ts
frontend/src/stores/app.ts
frontend/src/styles/onboarding.css
frontend/src/types/index.ts
frontend/src/utils/__tests__/ccswitchImport.spec.ts
frontend/src/views/HomeView.vue
frontend/src/views/KeyUsageView.vue
frontend/src/views/__tests__/KeyUsageView.spec.ts
frontend/src/views/admin/SettingsView.vue
frontend/src/views/admin/__tests__/SettingsView.spec.ts
frontend/src/views/auth/EmailVerifyView.vue
frontend/src/views/auth/README.md
frontend/src/views/auth/RegisterView.vue
frontend/src/views/auth/USAGE_EXAMPLES.md
frontend/src/views/auth/VISUAL_GUIDE.md
frontend/src/views/auth/__tests__/EmailVerifyView.spec.ts
frontend/src/views/public/LegalDocumentView.vue
```

API Key mimic、Codex / Claude 官方客户端形态和采样诊断：

```text
backend/internal/domain/constants.go
backend/internal/domain/constants_test.go
backend/internal/handler/gateway_handler.go
backend/internal/handler/openai_gateway_handler_test.go
backend/internal/handler/openai_images_failover_test.go
backend/internal/pkg/openai_compat/upstream_capability.go
backend/internal/pkg/openai_compat/upstream_capability_test.go
backend/internal/pkg/tlsfingerprint/dialer.go
backend/internal/pkg/tlsfingerprint/dialer_test.go
backend/internal/repository/http_upstream.go
backend/internal/service/account.go
backend/internal/service/account_anthropic_passthrough_test.go
backend/internal/service/account_apikey_mimicry.go
backend/internal/service/account_openai_passthrough_test.go
backend/internal/service/apikey_mimic_identity.go
backend/internal/service/apikey_mimic_identity_test.go
backend/internal/service/content_moderation.go
backend/internal/service/gateway_anthropic_apikey_passthrough_test.go
backend/internal/service/gateway_apikey_mimicry.go
backend/internal/service/gateway_debug_logging.go
backend/internal/service/gateway_debug_logging_test.go
backend/internal/service/gateway_hotpath_optimization_test.go
backend/internal/service/gateway_service.go
backend/internal/service/gateway_tool_rewrite.go
backend/internal/service/gateway_tool_rewrite_test.go
backend/internal/service/openai_apikey_mimic_profile.go
backend/internal/service/openai_apikey_mimic_profile_test.go
backend/internal/service/openai_apikey_mimicry.go
backend/internal/service/openai_apikey_responses_probe.go
backend/internal/service/openai_apikey_responses_probe_test.go
backend/internal/service/openai_codex_transform.go
backend/internal/service/openai_codex_transform_test.go
backend/internal/service/openai_gateway_chat_completions.go
backend/internal/service/openai_gateway_chat_completions_test.go
backend/internal/service/openai_gateway_messages.go
backend/internal/service/openai_gateway_record_usage_test.go
backend/internal/service/openai_gateway_responses_chat_fallback_test.go
backend/internal/service/openai_gateway_service.go
backend/internal/service/openai_gateway_service_test.go
backend/internal/service/openai_images_responses.go
backend/internal/service/openai_oauth_passthrough_test.go
backend/internal/service/openai_upstream_http.go
backend/internal/service/openai_upstream_http_test.go
backend/internal/service/openai_ws_protocol_forward_test.go
tools/sub2api_capture_proxy.py
tools/sub2api_capture_summary.py
```

Antigravity 默认模型、模型映射、官方请求形态和成本口径：

```text
backend/internal/domain/constants.go
backend/internal/domain/constants_test.go
backend/internal/handler/admin/account_handler_available_models_test.go
backend/internal/pkg/antigravity/claude_types.go
backend/internal/pkg/antigravity/claude_types_test.go
backend/internal/pkg/antigravity/gemini_types.go
backend/internal/pkg/antigravity/oauth.go
backend/internal/pkg/antigravity/oauth_test.go
backend/internal/pkg/antigravity/request_transformer.go
backend/internal/pkg/antigravity/request_transformer_test.go
backend/internal/service/antigravity_gateway_service.go
backend/internal/service/antigravity_gateway_service_test.go
backend/internal/service/antigravity_model_mapping_test.go
backend/internal/service/antigravity_rate_limit_test.go
backend/internal/service/gateway_service_antigravity_whitelist_test.go
backend/internal/service/model_rate_limit.go
backend/internal/service/upstream_models.go
backend/internal/service/upstream_models_test.go
backend/resources/model-pricing/model_prices_and_context_window.json
frontend/src/components/account/AccountUsageCell.vue
frontend/src/components/account/CreateAccountModal.vue
frontend/src/components/account/EditAccountModal.vue
frontend/src/components/account/ModelWhitelistSelector.vue
frontend/src/components/account/__tests__/BulkEditAccountModal.spec.ts
frontend/src/components/account/__tests__/ModelWhitelistSelector.spec.ts
frontend/src/composables/useModelWhitelist.ts
frontend/src/composables/__tests__/useModelWhitelist.spec.ts
```

账号保活、keeper sidecar、内部代理接口和 Plus 增强功能 UI：

```text
backend/cmd/server/wire_gen.go
backend/internal/handler/admin/account_codex_import_test.go
backend/internal/handler/admin/account_data_handler_test.go
backend/internal/handler/admin/account_handler.go
backend/internal/handler/admin/account_handler_list_test.go
backend/internal/handler/admin/account_handler_mixed_channel_test.go
backend/internal/handler/admin/account_handler_passthrough_test.go
backend/internal/handler/admin/batch_update_credentials_test.go
backend/internal/server/api_contract_test.go
backend/internal/server/routes/admin.go
backend/internal/service/account_test_service.go
backend/internal/service/account_test_service_keeper_proxy_test.go
backend/internal/service/account_test_service_openai_test.go
backend/internal/service/account_usage_service.go
backend/internal/service/billing_service.go
backend/internal/service/billing_service_test.go
backend/internal/service/setting_service.go
frontend/src/api/admin/accounts.ts
frontend/src/components/layout/AppSidebar.vue
frontend/src/i18n/locales/en.ts
frontend/src/i18n/locales/zh.ts
frontend/src/router/index.ts
frontend/src/views/admin/ApiKeyMimicSettingsView.vue
keeper/.gitignore
keeper/Dockerfile
keeper/go.mod
keeper/go.sum
keeper/keeper.example.yaml
keeper/main.go
keeper/main_test.go
keeper/persistent.go
```

### 4.4 上游合并检查

合并上游后重点确认：

- Anthropic mimic 与 passthrough 仍互斥，`/v1/messages` 和 `/v1/messages/count_tokens` 仍走独立 mimic builder。
- Anthropic 工具名归一和 per-request reverseMap 仍只改结构化工具字段。
- OpenAI API Key mimic 仍参与 `isCodexCLI` 和 Responses 路由判定。
- `desktop_0_142` 仍是默认 Codex profile，TLS fingerprint 未显式绑定时不回退到 Node.js profile。
- Responses capability probe 仍按 mimic / 非 mimic 分键保存。
- `CodexBaseInstructionsForModel()` 的 `gpt-5.5` / `gpt-5.2` / 其余 `gpt-5*` 映射仍符合当前策略。
- Antigravity 账号编辑、默认白名单/映射、同步上游和 `/models` 仍统一到官方 8 模型，不出现 16 个重复模型。
- Antigravity 官方伪装仍保留官方 UA、fixed `thinkingBudget`、`labels.model_enum`、`labels.trajectory_id`、同源 `requestId`，并确保客户端 `thinking` / `output_config.effort` 不覆盖官方 8 模型的上游预算。
- Antigravity 成本仍保留 `gpt-oss-120b-medium` 的 `$0.05 / $0.01 / $0.20` 每 1M tokens。
- Plus 增强功能入口仍包含 API Key 账号伪装、Antigravity 增强和账号保活。
- Plus 前端路由、侧边栏、i18n、账号 API 封装和统一设置页仍与后端接口对齐。
- Release / Docker 仍发布并部署 `ghcr.io/itv3/sub2apiplus`，ARM64 更新只替换 app 容器。

重点文件以第 4.3 节清单为准；合并时优先看 API Key mimic、Antigravity、keeper 和 Plus UI 四组。

### 4.5 Mimic 对齐原则

继续提升官方客户端一致性时，不直接盲改源码，按下面顺序处理：

1. 采集真实官方客户端请求。
2. 采集经 sub2apiplus 的伪装请求。
3. 建差异表。
4. 用失败请求做 A/B 消融重放，每次只改一个变量或一个可解释变量组。
5. 只对高置信差异改源码。
6. 每改一项都做官方客户端和第三方客户端双向回归。

诊断与灰度能力可围绕这些方向扩展：

- `anthropic_apikey_mimic_official_identity`
- `anthropic_apikey_mimic_profile`
- 显示当前账号实际出站 profile。
- 显示是否启用 TLS fingerprint。
- 显示最近一次 mimic 请求的脱敏诊断摘要。
- 提供“仅脱敏导出”的抓包辅助入口。

UI 中不得展示密钥、token、authorization、x-api-key。高风险 body cloaking 开关默认关闭或灰度。

后续增强方向：

- 继续对齐官方客户端 body identity、system prompt、metadata 和工具调用形态。
- 为 Anthropic / OpenAI mimic 增加独立 profile，避免不同官方客户端形态互相污染。
- 增加脱敏差异采集和 A/B 消融辅助能力，用真实失败样本决定是否改源码。

## 5. 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。接入第三方 AI 服务可能违反服务商条款，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
