# Apple container 部署

Sub2API Plus 可以通过 Apple 的 `container` CLI 运行原生三服务栈。该流程直接运行已发布的 Sub2API Plus、PostgreSQL 和 Redis OCI 镜像，不需要 Docker Desktop 或兼容 Docker 的守护进程。

## 支持范围

Apple `container` 主要用于 Mac 本地开发，以及由管理员主动维护的部署。生产环境仍推荐使用 Docker Compose。

Apple `container` 1.1 不提供重启策略、开机自动启动、持续健康调度、Docker API Socket 或完整的 Compose 编排。`apple-container.sh` 会在每次调用时按顺序启动服务并检查就绪状态，但它不是持续运行的进程监督器。

## 环境要求

- Apple silicon Mac
- macOS 26 或更高版本
- Apple `container` 1.1.0 或更高版本
- 用于生成初始密钥的 `openssl`
- 首次启动已发布容器时，根据 macOS 提示允许 `container-runtime-linux` 访问本地网络

从 Apple `container` 的[官方发布页](https://github.com/apple/container/releases)安装，然后确认版本：

```bash
container --version
```

## 快速开始

```bash
git clone https://github.com/itv3/sub2apiplus.git
cd sub2apiplus/deploy

# 创建 .env，并随机生成 PostgreSQL、JWT 和 TOTP 密钥。
./apple-container.sh init

# 启动前检查并按需修改配置。
nano .env

# 创建卷、网络和容器，等待依赖就绪后启动 Sub2API Plus。
./apple-container.sh up

# 检查 PostgreSQL、Redis 和应用接口。
./apple-container.sh status
```

启动后访问 `http://localhost:8080`。如果 `ADMIN_PASSWORD` 为空，可从应用日志中获取自动生成的管理员密码：

```bash
./apple-container.sh logs app
```

环境文件使用字面量 `KEY=value` 格式。不要使用 `${VALUE:-default}` 一类 Compose 表达式；除非引号本身就是值的一部分，否则不要给值加引号。`BIND_HOST` 必须是 IPv4 地址，`SERVER_PORT` 必须位于 1025 至 65535 之间。

## 命令说明

```bash
# 启动依赖，并使用当前依赖 IP 重建轻量应用容器。
./apple-container.sh up

# 同时重建 PostgreSQL 和 Redis 容器，但保留其持久卷。
./apple-container.sh up --recreate

# 停止所有容器，保留资源和数据。
./apple-container.sh down

# 按依赖顺序重启 PostgreSQL、Redis 和 Sub2API Plus。
./apple-container.sh restart

# 显示资源状态并执行实时健康探测。
./apple-container.sh status

# 查看或持续跟踪指定服务日志。
./apple-container.sh logs app -f
./apple-container.sh logs postgres -f
./apple-container.sh logs redis -f

# 拉取全部 linux/arm64 镜像，然后重建容器。
./apple-container.sh pull
./apple-container.sh up --recreate

# 删除容器和网络，保留命名卷。
./apple-container.sh destroy --yes

# 永久删除服务栈及应用、数据库和缓存数据。
./apple-container.sh destroy --volumes --yes
```

`destroy --volumes` 不会删除 `.env`、备份文件或已拉取的镜像。停用部署时需要单独删除凭据和备份。只有确认没有其他 Apple 容器使用某个镜像后，才能执行 `container image delete <image>`。

主机重启或执行 `container system stop` 后，需要再次运行 `./apple-container.sh up`。Apple `container` 不会自动重启持久化容器。

## 配置

脚本默认读取 `deploy/.env`，与 Docker Compose 共用同一份源文件。可以通过 `SUB2API_ENV_FILE` 为当前 Shell 中的所有命令指定另一份文件：

```bash
export SUB2API_ENV_FILE=/absolute/path/to/sub2api.env
./apple-container.sh init
./apple-container.sh up
```

Apple 容器专用镜像覆盖项如下：

```dotenv
APPLE_CONTAINER_SUB2API_IMAGE=ghcr.io/itv3/sub2apiplus:latest
APPLE_CONTAINER_POSTGRES_IMAGE=postgres:18-alpine
APPLE_CONTAINER_REDIS_IMAGE=redis:8-alpine
```

普通 `up` 命令会重建应用容器，因此应用环境变量的修改会立即生效。修改 PostgreSQL、Redis 镜像或 Redis 运行参数后，应使用 `up --recreate`。持久数据始终保留在命名卷中。

`POSTGRES_USER`、`POSTGRES_PASSWORD` 和 `POSTGRES_DB` 只在 PostgreSQL 初始化空数据卷时生效。修改 `.env` 并重建容器不会改变已有数据库。密码轮换应使用 `ALTER ROLE`，用户或数据库变更应设计明确的迁移。确实需要初始化全新空数据库时，应先备份旧数据，再使用 `destroy --volumes`。

共用配置在 Apple 流程中的处理方式：

| 配置 | Apple 流程行为 |
|---|---|
| 应用和网关变量 | 从 `.env` 传入 Sub2API Plus |
| `BIND_HOST`、`SERVER_PORT` | 用于 macOS 对外发布端口 |
| `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` | 仅用于 PostgreSQL 首次初始化 |
| `REDIS_PASSWORD` | 同时传给 Redis 和 Sub2API Plus |
| `DATABASE_PORT`、`REDIS_PORT` | 内部端口固定为 5432 和 6379 |
| `POSTGRES_MAX_*`、`REDIS_MAXCLIENTS` | 当前不会应用到数据库或缓存服务端 |

## 管理的资源

脚本只管理带有 `org.sub2api.stack=apple-container` 标签的资源：

| 类型 | 名称 |
|---|---|
| 容器 | `sub2api-apple`、`sub2api-apple-postgres`、`sub2api-apple-redis` |
| 网络 | `sub2api-apple` |
| 命名卷 | `sub2api-apple-data`、`sub2api-apple-postgres-data`、`sub2api-apple-redis-data` |

PostgreSQL 卷挂载到 `/var/lib/postgresql`，保留 PostgreSQL 18 默认的子数据目录。Sub2API Plus 和 Redis 也将数据写入 Apple 命名卷挂载点下的子目录。这样处理是因为 Apple 命名卷没有 Docker 的 copy-up 和挂载点所有权行为。

## 网络

Apple `container` 1.1 不提供 Compose 风格的网络内服务别名。PostgreSQL 和 Redis 启动后，脚本通过 `container inspect` 读取它们在私有网络中的当前 IPv4 地址，将地址注入新创建的应用容器，然后启动 Sub2API Plus。脚本不会修改 `~/.config/container/config.toml` 或 macOS 主机解析器。

三个服务只加入私有 `sub2api-apple` 网络。只有应用发布宿主机端口，PostgreSQL 和 Redis 端口不会对外开放。

每次执行 `up` 或 `restart` 都会重建应用容器，因为依赖服务的轻量虚拟机停止后，内部地址可能变化。应用数据仍保留在 `sub2api-apple-data` 中。

脚本在报告成功前，会从 macOS 检查已发布的 `/health` 接口。首次启动时需要允许本地网络访问。如果内部探测成功，但宿主机端口探测因连接重置失败，请为 `container-runtime-linux` 开启本地网络权限，依次执行 `container system stop`、`container system start`，然后再次执行 `up`。升级运行时后可能再次出现权限提示。

## 备份与升级

持久化使用前，应在 `.env` 中固定镜像版本标签或摘要。升级应用或数据库镜像前，应在服务栈健康时创建备份：

```bash
umask 077
mkdir -p backups

# 逻辑备份 PostgreSQL。
container exec sub2api-apple sh -c \
  'PGPASSWORD="$DATABASE_PASSWORD" pg_dump -h "$DATABASE_HOST" -U "$DATABASE_USER" "$DATABASE_DBNAME"' \
  > backups/sub2api.sql

# 备份应用配置和本地文件。
container exec sub2api-apple sh -c 'tar -C "$DATA_DIR" -czf - .' \
  > backups/sub2api-data.tar.gz

./apple-container.sh pull
./apple-container.sh up --recreate
./apple-container.sh status
```

数据库迁移只向前执行。在确认升级后的服务栈正常前，应保留旧镜像引用和两份备份；只回退镜像无法撤销已经执行的数据库迁移。重要数据投入使用前，必须实际演练恢复流程。

将备份恢复到现有服务栈前，先确认镜像版本与备份兼容，再停止写入并替换应用数据和数据库：

```bash
# 确保空资源或现有资源已经创建，然后停止服务栈。
./apple-container.sh up
./apple-container.sh down

# 只删除应用容器，以便临时容器挂载其命名卷。
container delete sub2api-apple
SUB2API_IMAGE=ghcr.io/itv3/sub2apiplus:latest # 与 .env 中的 APPLE_CONTAINER_SUB2API_IMAGE 保持一致。
container run --rm --name sub2api-apple-data-restore \
  --entrypoint /bin/sh \
  --volume sub2api-apple-data:/restore \
  --volume "$PWD/backups:/backup:ro" \
  "$SUB2API_IMAGE" \
  -c 'rm -rf /restore/data && mkdir -p /restore/data && tar -xzf /backup/sub2api-data.tar.gz -C /restore/data'

# 在应用容器不存在时恢复逻辑数据库备份。
container start sub2api-apple-postgres
until container exec sub2api-apple-postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; do sleep 1; done
container copy backups/sub2api.sql sub2api-apple-postgres:/tmp/sub2api.sql
container exec sub2api-apple-postgres sh -c '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  dropdb -h 127.0.0.1 -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB"
  createdb -h 127.0.0.1 -U "$POSTGRES_USER" "$POSTGRES_DB"
  psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f /tmp/sub2api.sql
  rm /tmp/sub2api.sql
'

./apple-container.sh up
./apple-container.sh status
```

如果灾难恢复前已经删除命名卷，应先运行一次 `up` 创建全新服务栈，再执行上述恢复步骤。首次恢复演练必须使用非生产数据。

升级 Apple 运行时本身时执行：

```bash
./apple-container.sh down
container system stop
# 安装或升级到 Apple container 1.1.0 或更高版本。
container system start
./apple-container.sh up
```

## 运维限制

- 没有等价于 `restart: unless-stopped` 的功能；主机重启后需要运行 `up`，或自行配置 `launchd` 监督器。
- 健康探测只在 `up`、`restart` 和 `status` 期间执行；Apple `container` 不会持续调度探测。
- Docker Compose、Testcontainers、Buildx，以及依赖 `/var/run/docker.sock` 的工具无法直接使用该运行时。
- 重要数据投入使用前，必须验证命名卷的备份和恢复流程。
- 脚本面向原生 `linux/arm64` 镜像，Sub2API Plus 正常发布流程包含 arm64 版本。
- 包括凭据在内的运行时环境变量会保留在 Apple 容器配置中，能够检查本地运行时的用户可以看到这些值。
