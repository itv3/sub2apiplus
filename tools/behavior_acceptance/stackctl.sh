#!/usr/bin/env bash
# 升级前行为验收套件：栈管理（起栈 / 换预算重建网关 / 拆栈）。由 run.sh 调用，场景脚本重建网关时也会调它。
#
# 用法：stackctl.sh up | restart-gw <default|tight> | down | status
# 环境变量（run.sh 导出）：
#   BHV_WORKDIR     工作目录（必填；测试 CA、假上游数据、gw.env、state.json 都在这里，拆栈后由 run.sh 删除）
#   BHV_IMAGE       被测网关镜像（up 时必填）
#   BHV_PROJECT     项目名 / 名字前缀，必须以 bhv 开头（默认 bhvacc）；容器、网络、派生镜像都用它命名
#   BHV_PORT        网关映射到宿主机 127.0.0.1 的端口（默认 28190）
#   BHV_NET_PREFIX  独立网络的前三段（默认 172.31.78），网络是 <前缀>.0/24
#
# 隔离：只创建、只删除名字以 "<项目名>-" 开头的容器 / 网络 / 派生镜像；不碰任何其它容器、卷、镜像（被测镜像本身也不删）。
set -u
export PYTHONDONTWRITEBYTECODE=1

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P="${BHV_PROJECT:-bhvacc}"
if ! [[ "${P}" =~ ^bhv[a-z0-9]*$ ]]; then
  echo "项目名 ${P} 不合法：必须以 bhv 开头且只含小写字母数字（防止误删现网容器）" >&2
  exit 2
fi
W="${BHV_WORKDIR:-}"
export BHV_PROJECT="${P}"
export BHV_PORT="${BHV_PORT:-28190}"
export BHV_NET_PREFIX="${BHV_NET_PREFIX:-172.31.78}"

# 预算两档（字节）。default＝现网同款 256MiB；tight＝64MiB，单请求上限 5MiB，
# 只够一个 3～4MiB 大历史连接（权重＝正文×6.11＋25MiB 固定开销，一个约 44～49MiB，两个放不下）
budget_env() {
  case "$1" in
    default) export BHV_BUDGET=268435456 BHV_MAXREQ=38797312 ;;
    tight)   export BHV_BUDGET=67108864 BHV_MAXREQ=5242880 ;;
    *) echo "预算档应为 default 或 tight，收到：$1" >&2; return 2 ;;
  esac
}

compose() {
  docker compose -p "${P}" -f "${SUITE_DIR}/compose.yml" "$@"
}

need_workdir() {
  if [ -z "${W}" ] || [ ! -d "${W}" ]; then
    echo "BHV_WORKDIR 未设置或目录不存在：${W}" >&2
    exit 2
  fi
  if [ -f "${W}/pg.pw" ]; then
    BHV_PG_PASSWORD="$(cat "${W}/pg.pw")"
    export BHV_PG_PASSWORD
  fi
  export BHV_WORKDIR="${W}"
}

wait_health() {
  local code="" i
  for i in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${BHV_PORT}/health" || true)"
    [ "${code}" = 200 ] && break
    sleep 2
  done
  if [ "${code}" != 200 ]; then
    echo "网关 /health=${code} 等待超时（约 $((i * 2)) 秒）；最近日志：" >&2
    docker logs --tail 30 "${P}-gw" >&2 || true
    return 1
  fi
  echo "网关 /health=200（约 $((i * 2)) 秒）"
}

# 子网冲突检查：同一个 /24 已被别的 docker 网络占用就停（我们自己的网络不算）
check_subnet_free() {
  local n sn
  for n in $(docker network ls -q); do
    if [ "$(docker network inspect -f '{{.Name}}' "${n}")" = "${P}-net" ]; then
      continue
    fi
    sn="$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "${n}")"
    case " ${sn} " in
      *" ${BHV_NET_PREFIX}.0/24 "*)
        echo "子网 ${BHV_NET_PREFIX}.0/24 已被网络 $(docker network inspect -f '{{.Name}}' "${n}") 占用，请换 BHV_NET_PREFIX" >&2
        return 1 ;;
    esac
  done
}

make_pki() {
  # 一次性测试 CA（3 天有效）+ 叶子证书（SAN 含假上游冒充的域名）；私钥只在工作目录，拆栈后随目录删除
  mkdir -p "${W}/pki" "${W}/mock-data" "${W}/gwdata"
  cp "${SUITE_DIR}/wsmock.py" "${W}/mock-data/wsmock.py"
  openssl ecparam -name prime256v1 -genkey -noout -out "${W}/pki/ca.key" || return 1
  openssl req -x509 -new -key "${W}/pki/ca.key" -sha256 -days 3 -subj "/CN=bhvacc-test-ca" -out "${W}/pki/ca.pem" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" || return 1
  openssl ecparam -name prime256v1 -genkey -noout -out "${W}/pki/leaf.key" || return 1
  openssl req -new -key "${W}/pki/leaf.key" -subj "/CN=chatgpt.com" -out "${W}/pki/leaf.csr" || return 1
  printf 'subjectAltName=DNS:chatgpt.com,DNS:*.chatgpt.com,DNS:auth.openai.com,DNS:api.openai.com,DNS:api.wsrec.test\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth\n' > "${W}/pki/leaf.ext"
  openssl x509 -req -in "${W}/pki/leaf.csr" -CA "${W}/pki/ca.pem" -CAkey "${W}/pki/ca.key" -CAcreateserial -days 2 -sha256 \
    -extfile "${W}/pki/leaf.ext" -out "${W}/pki/leaf.crt" 2> /dev/null || return 1
  cp "${W}/pki/leaf.crt" "${W}/pki/leaf.key" "${W}/mock-data/"
  chmod 600 "${W}/pki/ca.key" "${W}/pki/leaf.key" "${W}/mock-data/leaf.key"
  chmod 644 "${W}/mock-data/leaf.crt" "${W}/mock-data/wsmock.py"
}

make_env() {
  # 网关环境变量：对照现网 compose 的公开项（准入、WS、数据库 / Redis 指向套件自己的容器），
  # 全部密码与密钥都是本次随机值；不读取、不复制现网容器的任何环境变量。
  local pw jwt totp admin
  pw="$(openssl rand -hex 12)"
  jwt="$(openssl rand -hex 32)"
  totp="$(openssl rand -hex 32)"
  admin="$(openssl rand -hex 12)"
  printf '%s' "${pw}" > "${W}/pg.pw"
  chmod 600 "${W}/pg.pw"
  (
    umask 077
    cat > "${W}/gw.env" <<EOF
AUTO_SETUP=true
SERVER_HOST=0.0.0.0
SERVER_PORT=8080
SERVER_MODE=release
RUN_MODE=standard
GATEWAY_REQUEST_MEMORY_AMPLIFICATION=6.11
GATEWAY_REQUEST_MEMORY_FIXED_BYTES=26214400
DATABASE_HOST=pg
DATABASE_PORT=5432
DATABASE_USER=sub2api
DATABASE_PASSWORD=${pw}
DATABASE_DBNAME=sub2api
DATABASE_SSLMODE=disable
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0
ADMIN_EMAIL=bhvacc-admin@acc-pool.invalid
ADMIN_PASSWORD=${admin}
JWT_SECRET=${jwt}
TOTP_ENCRYPTION_KEY=${totp}
TZ=Asia/Shanghai
GATEWAY_OPENAI_WS_MIN_IDLE_PER_ACCOUNT=0
SECURITY_URL_ALLOWLIST_ENABLED=false
EOF
  )
  BHV_PG_PASSWORD="${pw}"
  export BHV_PG_PASSWORD
}

cmd_up() {
  need_workdir
  if [ -z "${BHV_IMAGE:-}" ]; then
    echo "BHV_IMAGE 未设置" >&2
    exit 2
  fi
  check_subnet_free || exit 1
  make_pki || { echo "生成测试 CA 失败" >&2; exit 1; }
  make_env
  # 镜像：假上游（alpine + python3，已存在则复用）+ 带测试 CA 的网关派生镜像（只加一个信任根，二进制不变）
  if ! docker image inspect "${P}-mock:1" > /dev/null 2>&1; then
    docker build --network=host -q -t "${P}-mock:1" - < "${SUITE_DIR}/Dockerfile.mock" > /dev/null || { echo "构建假上游镜像失败（需要能访问 alpine 软件源）" >&2; exit 1; }
  fi
  printf 'FROM %s\nCOPY ca.pem /usr/local/share/ca-certificates/bhvacc-test-ca.crt\nRUN update-ca-certificates\n' "${BHV_IMAGE}" \
    | docker build --network=host -q -t "${P}-gw:run" --label "bhvacc.base_image=${BHV_IMAGE}" -f - "${W}/pki" > /dev/null || { echo "构建网关派生镜像失败" >&2; exit 1; }
  echo "派生镜像 ${P}-gw:run 基于 ${BHV_IMAGE}"
  budget_env default
  compose up -d --no-build pg redis mock || exit 1
  local i
  for i in $(seq 1 30); do
    docker exec "${P}-pg" pg_isready -U sub2api -d sub2api > /dev/null 2>&1 && break
    sleep 1
  done
  # 首次启动网关：auto setup 建表；再造测试数据；再重建网关让调度缓存读到新数据
  compose up -d --no-build gw || exit 1
  wait_health || exit 1
  BHV_STACKCTL="${SUITE_DIR}/stackctl.sh" python3 -B "${SUITE_DIR}/setup_data.py" || { echo "造测试数据失败" >&2; exit 1; }
  compose up -d --no-build --force-recreate --no-deps gw || exit 1
  wait_health || exit 1
  echo "网关二进制 sha256=$(docker exec "${P}-gw" sha256sum /app/sub2api | cut -c1-16)  镜像版本标签=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "${BHV_IMAGE}" 2> /dev/null)"
}

cmd_restart_gw() {
  need_workdir
  budget_env "${1:-default}" || exit 2
  # 重建前存下旧网关容器的完整日志（BHV_LOGDIR 由 run.sh 设置），重建会让它消失
  if [ -n "${BHV_LOGDIR:-}" ]; then
    mkdir -p "${BHV_LOGDIR}"
    docker logs "${P}-gw" > "${BHV_LOGDIR}/gateway-before-restart-$(date +%H%M%S).log" 2>&1 || true
  fi
  compose up -d --no-build --force-recreate --no-deps gw > /dev/null 2>&1 || { echo "重建网关失败" >&2; exit 1; }
  wait_health > /dev/null || exit 1
}

cmd_down() {
  # 只按名字前缀删：容器 → 网络 → 派生镜像。不依赖 compose 文件和环境变量，残留时也能清
  local c
  for c in $(docker ps -aq --filter "name=^${P}-"); do
    docker rm -f -v "${c}" > /dev/null 2>&1
  done
  docker network rm "${P}-net" > /dev/null 2>&1
  docker rmi -f "${P}-gw:run" "${P}-mock:1" > /dev/null 2>&1
  local left
  left="$(docker ps -a --format '{{.Names}}' --filter "name=^${P}-"; docker network ls --format '{{.Name}}' --filter "name=^${P}-"; docker images --format '{{.Repository}}:{{.Tag}}' | grep "^${P}-" || true)"
  if [ -n "${left}" ]; then
    echo "拆栈后仍有残留：${left}" >&2
    return 1
  fi
  echo "拆栈完成：容器、网络、派生镜像均已删除（项目 ${P}）"
}

cmd_status() {
  docker ps -a --filter "name=^${P}-" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
}

case "${1:-}" in
  up) cmd_up ;;
  restart-gw) cmd_restart_gw "${2:-default}" ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) echo "用法：stackctl.sh up | restart-gw <default|tight> | down | status" >&2; exit 2 ;;
esac
