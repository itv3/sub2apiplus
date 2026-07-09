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
4. 账号级 header override 可继续用于普通 API Key / passthrough 出站；mimic 链路会保护官方身份头，不允许覆写 `user-agent`、Codex 会话头、Claude Code / Stainless 相关头等关键字段。
5. OpenAI Codex mimic 开启后，`/v1/messages` 也必须走 Responses mimic 链路，不受 `force_chat_completions`、普通 Responses probe false 或 `openai_responses_supported=false` 影响。

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

OpenAI `/v1/responses/compact` 是特例：compact 请求保持官方 JSON 形态，移除 `stream`、`store`、`prompt_cache_key`、`client_metadata` 等请求级字段，不执行 Codex mimic body 默认值补写，并强制上游 `Accept: application/json`。当 Codex remote compact 通过普通 `/v1/responses` body-signal 触发且客户端原始请求带 `stream:true` 时，上游仍走 unary JSON，主服务写回客户端时会桥接为最小 Responses SSE，确保客户端收到 `response.output_item.done` 和 `response.completed`。

能力边界：

- 不承诺 100% 复制官方客户端；服务端隐藏 prompt、账号侧状态、产品侧 memory、UI 上下文和 HTTP/2 wire 指纹都可能超出 sub2apiplus 能力范围。
- mimic 主要伪装 header、body、TLS 和路由形态；不做响应文本替换，也不清洗原客户端在正文、工具描述、路径或能力提示里的身份信息。
- 判断 mimic 效果应比较第三方中转站看到的上游入站请求形态，不能只看 ARM64 usage 页面里的客户端入口 `USER-AGENT`。

维护入口见第 4.3 节。

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

1. 模型白名单保存该账号允许发包的模型；默认值来自官方 8 模型，管理员可在账号编辑页通过“自定义模型名称”手动追加新模型。
2. 模型映射保存“外部显示模型 -> 实际发包 model”；映射目标必须命中该账号允许的模型集合。
3. 外部 `/antigravity/v1/models` 默认只展示界面模型，不展示兼容别名；账号手动加入白名单的新模型可以作为该账号的可用模型。
4. `gemini-3.1-pro-high` 这类历史兼容别名可继续接收请求，但不进入默认白名单和 `/models`。
5. 账号自定义 `model_mapping` 时，历史兼容别名只有在目标模型也被该账号白名单 / 映射允许时才放行。
6. Antigravity `web_search` 请求固定使用默认官方模型表内的 `gemini-3.5-flash-low` 作为兜底发包模型。
7. 计费按最终实际发包模型 `UpstreamModel` 查价；日志仍保留外部显示模型，且该计费口径优先于渠道的 `requested` / `channel_mapped` 配置。

已实现能力：

| 功能 | 当前行为 |
|---|---|
| 账号编辑 | Antigravity 与 OpenAI / Anthropic 一样支持“模型白名单 / 模型映射”，并可通过“自定义模型名称”手动追加后续新增模型。 |
| 模型收敛 | 新建账号默认使用官方 8 模型，`/models` 面向客户端默认收敛到官方 8 模型；手动“同步上游支持的模型”保存真实同步结果，不再把未知上游结果改写成官方默认值。 |
| 官方伪装 | UA 使用 `antigravity/hub/2.2.1 darwin/arm64`；默认官方 8 模型忽略客户端 `thinking` / `output_config.effort` 对上游预算的覆盖，始终按抓包 fixed `thinkingBudget` 发送；补 `labels/model_enum/trajectory_id` 和同源 `requestId`，并过滤无关 stop / sampling 参数。账号手动追加的新模型按其模型名发包，不要求先维护全局官方模型注册表。 |
| 成本 | 按最终实际发包模型计费；`gpt-oss-120b-medium` 按 `$0.05 / $0.01 / $0.20` 每 1M tokens 计价。 |

维护入口见第 4.3 节。

### 1.3 账号保活

账号保活用于在 OpenAI / Anthropic API Key 账号空闲超过配置间隔后，通过官方 `codex` / `claude` 客户端在真实项目目录发起低频请求，维持上游账号活跃。

功能边界：

1. 保活配置、账号引用、成本口径、最近使用时间和历史展示都归 sub2apiplus 管理。
2. keeper 源码位于主仓库 `keeper/`，随 sub2apiplus 一起维护；运行时是独立 sidecar 容器 `sub2apiplus-keeper`，不嵌入主服务进程。
3. keeper 负责调度、运行 `codex` / `claude` 官方客户端、维护 worker 目录、会话文件和执行日志，同时提供本地 Web/API 供主服务代理状态、设置和立即执行。
4. 官方客户端由账号平台自动选择：OpenAI 使用 `codex`，Anthropic 使用 `claude`。
5. 当前仅支持 OpenAI / Anthropic 的 API Key 账号；OAuth、setup-token、upstream 等账号不会进入保活候选，也不会被 keeper sidecar 执行。
6. 不同账号到期后可并行执行，同一个账号通过 `Running` 状态防止重复执行。
7. keeper 从主服务获取按账号、按平台签发的短期代理 token；官方客户端进程只拿该 scoped token，不能拿全局 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN`。
8. 官方客户端单次执行超时默认 2700 秒；`sub2apiplus.timeout_seconds` 只是 keeper 调主服务内部接口的超时，默认 180 秒。

保活配置放在“Plus 增强功能 / 账号保活”分组中：

| 配置 | 说明 |
|---|---|
| 保活账号 | 从现有 OpenAI / Anthropic API Key 账号中选择；实际执行能力取决于账号上游凭据和测试路径是否可用。 |
| 启用保活 | 写入 `account.Extra.keeper_keepalive_enabled`。 |
| 保活间隔 | 写入 `account.Extra.keeper_keepalive_interval_minutes`，默认 8 分钟，最小 1 分钟。 |
| 工作时间 | 写入账号 `extra`，默认 `04:00` - `24:00`。 |
| 执行模式 | OpenAI 默认全新会话 `fresh`，Anthropic 默认接续上次会话 `resume_last`。 |
| 模型 | 从账号可用模型列表选择。 |
| 项目 | 从 `SUB2APIPLUS_KEEPER_PROJECTS` 暴露的项目列表选择，keeper 内部映射到 `/workspace/projects/<项目名>`。 |
| 最大输出 token | 通过配置页写入 `account.Extra.keeper_keepalive_max_output_tokens`，默认 512；主服务内部代理会按该值钳制官方客户端请求，服务端硬上限为 1024。 |
| prompt 约束和题库 | 包括全局只读约束、通用问题、项目问题和账号自定义 prompt；全局约束和题库保存在 keeper state。 |
| 模型成本 | 复用 sub2apiplus 现有成本和 usage 统计口径。 |

保活触发规则：

1. keeper 按 `scan_interval_seconds` 周期扫描账号；本仓库示例配置为 30 秒，未配置时程序默认 120 秒。
2. 触发时间取“最近真实请求时间 + 保活间隔”和“最近成功保活完成时间 + 保活间隔”中较晚者。
3. 只要账号一直正常使用，最近真实请求时间会不断向后更新，保活时间随之顺延，不会额外产生保活请求。
4. 手动“立即执行”会忽略空闲判断，但同账号运行中时不会重复启动。
5. 保活失败会记录错误和失败次数，下一次仍按账号配置的保活间隔重新排队；客户端断开、服务退出等非业务失败不会一律累计为连续失败。
6. keeper 收到 `SIGTERM` / `Interrupt` 后会取消运行上下文，等待已启动任务收尾，并关闭持久客户端连接。

页面包含三个视图：

| 视图 | 内容 |
|---|---|
| 概览 | keeper 版本、账号数、今日成功 / 失败、运行中账号、24 小时用量 / 费用、最近结果、下次保活时间和立即执行入口。 |
| 配置 | 添加 / 编辑保活账号，选择模型、项目、间隔、工作时间、执行模式、账号 prompt，并维护全局约束提示词和提示词题库。 |
| 会话历史 | 展示每次保活的时间、账号、状态、模型、token、费用、结果摘要、错误详情、提示词和模型回复；模型回复只展示客户端解析出的 assistant 内容，完整上游 / 客户端错误保留在错误详情中。 |

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

keeper 镜像构建时会在容器内安装官方 `codex` / `claude` 客户端；它们不进入 sub2apiplus 主镜像，也不要求宿主机单独安装。

准备 keeper 源码和配置：

```sh
mkdir -p /root/docker/sub2apiplus/keeper/app/{data,projects} /root/docker/sub2apiplus/keeper/repo
git clone --depth 1 https://github.com/itv3/sub2apiplus.git /tmp/sub2apiplus-src
cp -a /tmp/sub2apiplus-src/keeper/. /root/docker/sub2apiplus/keeper/repo/
cp /tmp/sub2apiplus-src/keeper/keeper.example.yaml /root/docker/sub2apiplus/keeper/app/keeper.yaml
rm -rf /tmp/sub2apiplus-src
```

下载保活项目。`SUB2APIPLUS_KEEPER_PROJECTS` 写项目名，目录必须放在 keeper 的 `projects` 下；项目名只允许单级目录名，不接受绝对路径、`..` 或多级路径：

```sh
cd /root/docker/sub2apiplus/keeper/app/projects
git clone --depth 1 https://github.com/itv3/homeproxy.git homeproxy
```

多个项目用英文逗号分隔，并确保同名目录存在，例如 `SUB2APIPLUS_KEEPER_PROJECTS=homeproxy,sub2apiplus` 对应：

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

检查 keeper `keeper.yaml`。完整示例来自 `keeper/keeper.example.yaml`，全新部署至少确认下面几项；提示词题库可先使用示例值，后台保存后会持久化到 `/app/data/state.json`：

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

keeper 容器需要 `CAP_SYS_ADMIN`、`seccomp=unconfined`、`apparmor=unconfined`，用于满足官方客户端和 `bubblewrap` 沙箱运行要求。`data`、`runtime`、`projects` 必须持久化，容器重建不能丢失会话、日志和项目目录。

### 2.4 后台激活和验证

1. 进入 sub2apiplus 后台，添加 API 账号，确认模型和成本配置正常。
2. 进入“Plus 增强功能 / 账号保活 / 配置”，点击“添加账号”。
3. 从现有 OpenAI / Anthropic API Key 账号中选择账号；平台自动决定 OpenAI 使用 `codex`、Anthropic 使用 `claude`。
4. 选择模型、项目、保活间隔、工作时间和执行模式，保存。
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
| `<Plus 版本>` | 指定版本，例如 `0.1.149-1`。 |
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
| `SUB2APIPLUS_KEEPER_PROJECTS` | 账号保活项目下拉框项目名，多个项目用英文逗号分隔。 |
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

发布前先跑定向测试；大范围合并或共享逻辑改动时扩大到 `go test ./...` 和完整前端测试：

```sh
cd /path/to/sub2apiplus/backend
go test ./internal/pkg/apicompat ./internal/pkg/openai ./internal/service

cd /path/to/sub2apiplus/keeper
go test ./...

cd /path/to/sub2apiplus
make test-frontend
```

Plus 版本在上游版本后追加自定义序号，例如 `0.1.149-1`；Git tag 使用 `v0.1.149-1`；GHCR 镜像 tag 使用 `ghcr.io/itv3/sub2apiplus:0.1.149-1`。

```sh
cd /path/to/sub2apiplus
VERSION=0.1.149-1
echo "$VERSION" > backend/cmd/server/VERSION
git add backend/cmd/server/VERSION
git commit -m "chore: release v${VERSION}"
git push origin main
git tag "v${VERSION}"
git push origin "v${VERSION}"
```

如果 tag push 没触发 Release，手动触发：

```sh
gh workflow run Release --repo itv3/sub2apiplus --ref main -f tag="v${VERSION}" -f simple_release=false
```

ARM64 更新只替换应用容器，不动 PostgreSQL、Redis、volume 和 `.env`：

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

后台“数据管理”入口当前仅保留兼容诊断；服务端固定返回 `DATA_MANAGEMENT_DEPRECATED`，不建议新部署 `datamanagementd`，也不要按旧流程挂载 `/tmp/sub2api-datamanagement.sock`。数据迁移优先使用第 3.3 节的本地目录迁移流程；数据库备份请在 PostgreSQL / Redis 层独立执行。

二进制 `install.sh` 仍是上游兼容的 systemd 安装路径，不安装 keeper sidecar；需要账号保活时使用 Docker Compose 部署。

TLS fingerprint 默认内置 Claude CLI / Node.js 风格 profile。API Key 账号伪装需要在账号 `extra` 中启用对应 mimic 开关和 `enable_tls_fingerprint`，具体行为以第 1 节和代码常量为准。

## 4. 维护参考

### 4.1 keeper 内部接口

sub2apiplus 提供内部接口给 keeper 和 Plus 增强功能页面使用。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/internal/keeper/accounts` | 返回已启用保活且满足候选条件的 OpenAI / Anthropic API Key 账号、模型、prompt、项目、最大输出 token、最近使用时间、下一次时间和 due 判断。 |
| `GET /api/v1/internal/keeper/projects` | 返回可在保活配置页选择的项目列表，来源为 `SUB2APIPLUS_KEEPER_PROJECTS`。 |
| `GET /api/v1/internal/keeper/state` | 代理 keeper 状态，用于概览、会话历史和运行状态展示。 |
| `GET /api/v1/internal/keeper/settings` | 读取 keeper 版本、全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/settings` | 保存全局约束提示词和提示词题库。 |
| `POST /api/v1/internal/keeper/run?target=<target>` | 立即执行指定保活目标；`target` 可匹配目标 ID、账号 ID 或账号名称。 |
| `GET /api/v1/internal/keeper/accounts/:id/models` | 返回该账号可用于保活的模型列表。 |
| `POST /api/v1/internal/keeper/accounts/:id/keepalive` | keeper sidecar 回写状态、token、费用和错误信息；带 `prompt` 的主服务直连执行请求会被拒绝。 |
| `POST /api/v1/internal/keeper/openai/accounts/:id/*` | Codex 官方客户端的 OpenAI 代理入口，仅允许 `/v1/responses`、`/responses`、`/v1/chat/completions`、`/chat/completions`。 |
| `GET /api/v1/internal/keeper/openai/accounts/:id/*` | Codex 官方客户端的 OpenAI 查询入口，仅允许 `/v1/models`、`/models`、`/v1/responses/*`、`/responses/*`。 |
| `POST /api/v1/internal/keeper/anthropic/accounts/:id/*` | Claude 官方客户端的 Anthropic 代理入口，仅允许 `/v1/messages`、`/v1/messages/count_tokens`。 |
| `GET /api/v1/internal/keeper/anthropic/accounts/:id/*` | Claude 官方客户端的 Anthropic 查询入口，仅允许 `/v1/models`。 |

账号列表、项目、state、settings、立即执行和模型列表接口可使用 `SUB2APIPLUS_KEEPER_INTERNAL_TOKEN` 或后台 admin auth；`keepalive` 回写接口只接受全局内部 token；账号代理接口只接受主服务为该账号和平台签发的 scoped proxy token，不接受后台 admin auth。代理路径会拒绝 query、fragment、`.`、`..`、`%2e` 等不安全片段；请求和响应 header 都使用显式 allowlist，避免浏览器 `Cookie`、上游 `Set-Cookie`、账号密钥、上游 `Authorization`、上游 `x-api-key` 或未脱敏日志越界。

keeper 通过 `max_output_tokens` 把账号级最大输出 token 传回主服务。主服务内部代理会按该值钳制 OpenAI Responses 的 `max_output_tokens`、OpenAI Chat Completions 的 `max_completion_tokens` / `max_tokens`，以及 Anthropic Messages 的 `max_tokens`；请求未带这些字段时会补默认值，超过上限时会降到账号配置值。

### 4.2 Plus UI 入口

Plus 增强功能的 UI 细节以代码为准，维护文件统一记录在第 4.3 节。上游合并后需要确认：后台侧边栏仍能进入“Plus 增强功能”；页面仍包含 API Key 账号伪装和账号保活分组；账号保活的概览、配置、会话历史仍能正常加载；Antigravity 账号编辑仍保留模型白名单和模型映射入口。

### 4.3 v0.1.149 差异文件清单

本节只记录差异口径和审核入口，不维护一份容易过期的全量文件清单。

基线说明：

- 发布基线是官方上游 tag `v0.1.149`。
- 当前分支已通过合并提交同步官方 `v0.1.149`；审核 Plus 自定义实现时，应优先看 `v0.1.149..HEAD` 中 API Key mimic、Antigravity、keeper 和 Plus UI 相关文件。
- `batch_image` 已包含在上游发布内容中，不属于 Plus 三项增强的自研范围；不要把上游批量生图代码误算进 Plus 自定义差异。
- `keeper/` 是当前新增源码目录；`.codex-captures/` 是本地抓包样本，`.kilo/` 是本地工具配置，不计入源码清单。

需要完整差异时直接生成：

```sh
git diff --name-only v0.1.149..HEAD
git diff --stat v0.1.149..HEAD
```

Plus 审核重点文件组：

| 范围 | 入口文件 |
|---|---|
| API Key mimic | `backend/internal/service/account_apikey_mimicry.go`、`gateway_apikey_mimicry.go`、`apikey_mimic_identity.go`、`openai_apikey_mimic_profile.go`、`openai_codex_transform.go`、`openai_gateway_service.go`、`gateway_tool_rewrite.go`、`account_header_override.go`。 |
| Antigravity | `backend/internal/pkg/antigravity/official_models.go`、`request_transformer.go`、`backend/internal/service/antigravity_gateway_service.go`、`upstream_models.go`、`model_rate_limit.go`、`backend/resources/model-pricing/model_prices_and_context_window.json`。 |
| 账号保活 | `keeper/main.go`、`keeper/persistent.go`、`keeper/keeper.example.yaml`、`backend/internal/handler/admin/account_handler_keeper.go`、`backend/internal/service/account_test_service_keeper.go`、`backend/internal/service/keeper_proxy_token.go`、`backend/internal/server/routes/admin.go`。 |
| Plus UI | `frontend/src/views/admin/ApiKeyMimicSettingsView.vue`、`frontend/src/api/admin/accounts.ts`、`frontend/src/router/index.ts`、`frontend/src/components/layout/AppSidebar.vue`、`frontend/src/i18n/locales/zh/common.ts`、`frontend/src/i18n/locales/zh/admin/accounts.ts`、`frontend/src/i18n/locales/en/common.ts`、`frontend/src/i18n/locales/en/admin/accounts.ts`、`frontend/src/components/account/CreateAccountModal.vue`、`frontend/src/components/account/EditAccountModal.vue`、`frontend/src/components/account/ModelWhitelistSelector.vue`、`frontend/src/composables/useModelWhitelist.ts`。 |
| 发布部署 | `README.md`、`deploy/.env.example`、`deploy/docker-compose.local.yml`、`.github/workflows/release.yml`、`backend/cmd/server/VERSION`。 |

### 4.4 上游合并检查

合并上游后重点确认：

- Anthropic mimic 与 passthrough 仍互斥，`/v1/messages` 和 `/v1/messages/count_tokens` 仍走独立 mimic builder。
- Anthropic 工具名归一和 per-request reverseMap 仍只改结构化工具字段。
- API Key mimic 链路仍保护官方身份头，账号级 header override 不能覆盖 Codex / Claude Code 关键头。
- OpenAI API Key mimic 仍参与 `isCodexCLI` 和 Responses 路由判定。
- OpenAI `/v1/messages` 的 Codex mimic 仍固定走 Responses mimic 链路，不能被普通 Chat Completions fallback 绕过。
- OpenAI `/v1/responses/compact` 在 Codex mimic 下仍保持上游 compact JSON 形态；body-signal 且客户端原始 `stream:true` 的 compact 请求仍会桥接回 Responses SSE。
- `desktop_0_142` 仍是默认 Codex profile，TLS fingerprint 未显式绑定时不回退到 Node.js profile。
- Responses capability probe 仍按 mimic / 非 mimic 分键保存。
- `CodexBaseInstructionsForModel()` 的 `gpt-5.5` / `gpt-5.2` / 其余 `gpt-5*` 映射仍符合当前策略。
- Antigravity 新建账号默认白名单/映射和 `/models` 仍统一到官方 8 模型，不出现 16 个重复模型；账号编辑页“自定义模型名称”手动加入的新模型仍可作为该账号允许模型；手动“同步上游支持的模型”仍保存真实同步结果，不回退伪造成官方默认列表。
- Antigravity 官方伪装仍保留官方 UA、fixed `thinkingBudget`、`labels.model_enum`、`labels.trajectory_id`、同源 `requestId`，并确保客户端 `thinking` / `output_config.effort` 不覆盖默认官方 8 模型的上游预算；`web_search` 兜底模型仍在默认官方 8 模型表内。
- Antigravity 计费仍优先使用最终实际发包模型 `UpstreamModel`，并保留 `gpt-oss-120b-medium` 的 `$0.05 / $0.01 / $0.20` 每 1M tokens。
- Plus 增强功能入口仍包含 API Key 账号伪装、Antigravity 增强和账号保活。
- Plus 前端路由、侧边栏、i18n、账号 API 封装和统一设置页仍与后端接口对齐。
- keeper 内部 handler/service 拆分后，账号列表、项目列表、settings/state 代理、立即执行、账号级最大输出 token 钳制和会话回写仍互相对齐。
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
