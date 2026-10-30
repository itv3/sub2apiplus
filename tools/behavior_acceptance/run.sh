#!/usr/bin/env bash
# 升级前行为验收套件：一条命令起栈、跑完、拆栈、删派生镜像。
#
# 用法：
#   bash run.sh <网关镜像> [--expect pass|accident] [--only S1,S3] [--out 目录] [--keep] [--skip-log-checks] [--no-prod-guard]
#   bash run.sh --cleanup            只清理上次异常中断留下的套件容器 / 网络 / 派生镜像
#
#   <网关镜像>   被测镜像，标签或 标签@摘要 均可，例如 ghcr.io/itv3/sub2apiplus:0.2.18-5
#   --expect     pass（默认）：要求全部场景通过；accident：对事故版本（v0.2.18-4）要求“必现”场景复现事故特征
#   --only       只跑指定场景（逗号分隔，如 S1,S3）
#   --out        结果目录（场景输出、结果 JSON、前后对比）；默认 ./bhv-out-<时间戳>
#   --keep       跑完不拆栈（调试用；之后用 --cleanup 清）
#   --skip-log-checks  不核对依赖网关日志文案的断言
#   --no-prod-guard    不做“现网未动”前后对比（非 ARM64 主机）
#
# 环境变量（都有默认值）：BHV_PROJECT（名字前缀，须以 bhv 开头，默认 bhvacc）、BHV_PORT（网关端口，默认 28190）、
#   BHV_NET_PREFIX（独立网络前三段，默认 172.31.78）、BHV_MAX_USE_PCT（根盘已用上限，默认 69）、
#   BHV_MIN_AVAIL_GIB（根盘可用下限，默认 40；设 0 关闭磁盘门槛）、BHV_PROD_CONTAINERS（现网容器名，空格分隔，
#   默认 "sub2apiplus sub2apiplus-postgres sub2apiplus-redis"）、BHV_PROD_PG（现网库容器，默认 sub2apiplus-postgres）。
#
# 最后一行固定是 EXIT=<退出码>（后台运行时用它判断结束）。退出码：0 达到预期；1 有场景未达预期；2 参数 / 前置不满足；3 栈不健康。
set -u
export PYTHONDONTWRITEBYTECODE=1

SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC=2
IMAGE=""
EXPECT="pass"
ONLY=""
OUT=""
KEEP=0
SKIP_LOG=0
PROD_GUARD=1
CLEANUP_ONLY=0


export BHV_PROJECT="${BHV_PROJECT:-bhvacc}"
export BHV_PORT="${BHV_PORT:-28190}"
export BHV_NET_PREFIX="${BHV_NET_PREFIX:-172.31.78}"
export BHV_STACKCTL="${SUITE_DIR}/stackctl.sh"
LOCK_DIR="/tmp/${BHV_PROJECT}.lock"
WORKDIR=""
HAVE_LOCK=0
STACK_STARTED=0
FINISHED=0

say() { echo "[$(date '+%H:%M:%S')] $*"; }

disk_line() {
  # 根盘使用率与可用空间（GiB）
  df -P / | awk 'NR==2 {gsub("%","",$5); printf "已用 %s%%，可用 %.1f GiB", $5, $4/1048576}'
}

disk_ok() {
  local min_avail="${BHV_MIN_AVAIL_GIB:-40}" max_use="${BHV_MAX_USE_PCT:-69}"
  if [ "${min_avail}" = 0 ]; then
    return 0
  fi
  df -P / | awk -v mu="${max_use}" -v ma="${min_avail}" 'NR==2 {gsub("%","",$5); if ($5+0 > mu+0 || $4/1048576 < ma+0) exit 1}'
}

# 现网快照：容器 ID / 启动时间 / 重启次数 + 现网库若干稳定表的行数与账号状态校验和（只读事务）。
# 不存在的容器跳过；不读写现网容器里的任何东西，只用 docker inspect 和一次只读 SQL。
prod_snapshot() {
  local c pg="${BHV_PROD_PG:-sub2apiplus-postgres}"
  for c in ${BHV_PROD_CONTAINERS:-sub2apiplus sub2apiplus-postgres sub2apiplus-redis}; do
    docker inspect -f '容器 {{.Name}} id={{.Id}} 启动={{.State.StartedAt}} 重启={{.RestartCount}} 镜像={{.Image}}' "${c}" 2> /dev/null || echo "容器 ${c} 不存在"
  done
  if docker inspect "${pg}" > /dev/null 2>&1; then
    docker exec -e PGOPTIONS='-c default_transaction_read_only=on' "${pg}" psql -U sub2api -d sub2api -At -F ' ' -c \
      "select 'rows_users', count(*)::text from users union all select 'rows_groups', count(*)::text from groups union all select 'rows_api_keys', count(*)::text from api_keys union all select 'rows_accounts', count(*)::text from accounts union all select 'rows_account_groups', count(*)::text from account_groups union all select 'rows_settings', count(*)::text from settings union all select 'md5_accounts_state', md5(coalesce(string_agg(id::text||':'||status||':'||schedulable::text, ',' order by id), '')) from accounts" 2>&1 | sed 's/^/现网库 /'
  else
    echo "现网库容器 ${pg} 不存在"
  fi
}

teardown() {
  if [ "${STACK_STARTED}" = 1 ] && [ "${KEEP}" = 0 ]; then
    say "拆栈、删派生镜像"
    BHV_WORKDIR="${WORKDIR}" bash "${SUITE_DIR}/stackctl.sh" down || true
    STACK_STARTED=0
  fi
  if [ -n "${WORKDIR}" ] && [ "${KEEP}" = 0 ]; then
    # 工作目录里有测试 CA 私钥、gw.env、假上游数据：只删本脚本用 mktemp 建的、名字带项目前缀的目录
    case "${WORKDIR}" in
      /tmp/${BHV_PROJECT}.work.*)
        rm -rf "${WORKDIR}" 2> /dev/null || docker run --rm -v "${WORKDIR}:/w" alpine:3.21 sh -c 'rm -rf /w/* /w/.[!.]*' > /dev/null 2>&1
        rm -rf "${WORKDIR}" 2> /dev/null
        ;;
    esac
    WORKDIR=""
  fi
  if [ "${HAVE_LOCK}" = 1 ]; then
    rmdir "${LOCK_DIR}" 2> /dev/null
    HAVE_LOCK=0
  fi
}

on_exit() {
  if [ "${FINISHED}" = 0 ]; then
    FINISHED=1
    teardown
  fi
  echo "EXIT=${RC}"
}
trap on_exit EXIT
trap 'RC=130; exit 130' INT TERM
trap 'RC=129; exit 129' HUP

while [ $# -gt 0 ]; do
  case "$1" in
    --expect) EXPECT="${2:-}"; shift 2 ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --skip-log-checks) SKIP_LOG=1; shift ;;
    --no-prod-guard) PROD_GUARD=0; shift ;;
    --cleanup) CLEANUP_ONLY=1; shift ;;
    -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}"; RC=0; exit 0 ;;
    -*) echo "未知参数：$1" >&2; exit 2 ;;
    *) if [ -z "${IMAGE}" ]; then IMAGE="$1"; shift; else echo "多余参数：$1" >&2; exit 2; fi ;;
  esac
done

# 项目名既是容器 / 网络 / 镜像的前缀，也决定拆栈时删谁：必须以 bhv 开头，避免误指向现网（如 sub2apiplus）
if ! [[ "${BHV_PROJECT}" =~ ^bhv[a-z0-9]*$ ]]; then
  echo "BHV_PROJECT=${BHV_PROJECT} 不合法：必须以 bhv 开头且只含小写字母数字" >&2
  exit 2
fi

if [ "${CLEANUP_ONLY}" = 1 ]; then
  BHV_WORKDIR="/nonexistent" bash "${SUITE_DIR}/stackctl.sh" down
  RC=$?
  if [ -z "$(docker ps -aq --filter "name=^${BHV_PROJECT}-")" ]; then
    rmdir "${LOCK_DIR}" 2> /dev/null
  fi
  exit "${RC}"
fi

# ---------------------------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------------------------
if [ -z "${IMAGE}" ]; then
  echo "缺少网关镜像参数。用法：bash run.sh <镜像> [选项]（--help 看说明）" >&2
  exit 2
fi
if [ "${EXPECT}" != pass ] && [ "${EXPECT}" != accident ]; then
  echo "--expect 只能是 pass 或 accident" >&2
  exit 2
fi
for tool in docker python3 openssl curl; do
  if ! command -v "${tool}" > /dev/null 2>&1; then
    echo "缺少依赖命令：${tool}" >&2
    exit 2
  fi
done
if ! docker compose version > /dev/null 2>&1; then
  echo "缺少 docker compose 插件" >&2
  exit 2
fi
if ! docker image inspect "${IMAGE}" > /dev/null 2>&1; then
  say "本机没有镜像 ${IMAGE}，尝试拉取"
  docker pull "${IMAGE}" || { echo "镜像 ${IMAGE} 本机没有也拉不到，停止（不自己构建镜像）" >&2; exit 2; }
fi
if ! mkdir "${LOCK_DIR}" 2> /dev/null; then
  echo "已有一次运行在进行或上次异常中断（锁 ${LOCK_DIR}）。确认没有在跑后执行：bash run.sh --cleanup（会清残留并释放锁）" >&2
  exit 2
fi
HAVE_LOCK=1
if [ -n "$(docker ps -aq --filter "name=^${BHV_PROJECT}-")" ]; then
  echo "发现上次残留的 ${BHV_PROJECT}-* 容器，先执行：bash run.sh --cleanup" >&2
  exit 2
fi
if python3 - "${BHV_PORT}" << 'PYEOF'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
PYEOF
then
  :
else
  echo "端口 127.0.0.1:${BHV_PORT} 已被占用，请换 BHV_PORT" >&2
  exit 2
fi
DISK_BEFORE="$(disk_line)"
say "根盘（起跑前）：${DISK_BEFORE}"
if ! disk_ok; then
  echo "根盘超过门槛（已用须 ≤ ${BHV_MAX_USE_PCT:-69}%，可用须 ≥ ${BHV_MIN_AVAIL_GIB:-40} GiB），先腾盘；不想要门槛可设 BHV_MIN_AVAIL_GIB=0" >&2
  exit 2
fi

OUT="${OUT:-$(pwd)/bhv-out-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "${OUT}"
OUT="$(cd "${OUT}" && pwd)"
WORKDIR="$(mktemp -d "/tmp/${BHV_PROJECT}.work.XXXXXX")"
export BHV_WORKDIR="${WORKDIR}"
export BHV_IMAGE="${IMAGE}"
export BHV_LOGDIR="${OUT}/gateway-logs"
chmod 700 "${WORKDIR}"

IMG_ID="$(docker image inspect -f '{{.Id}}' "${IMAGE}")"
IMG_VER="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}' "${IMAGE}" 2> /dev/null)"
say "被测镜像：${IMAGE}"
say "镜像 ID：${IMG_ID}；标签：${IMG_VER}"
say "预期：$([ "${EXPECT}" = pass ] && echo 全部场景通过 || echo 复现事故特征)；结果目录：${OUT}"

if [ "${PROD_GUARD}" = 1 ]; then
  prod_snapshot > "${OUT}/prod-before.txt" 2>&1
  say "现网快照（前）已记录：$(grep -c . "${OUT}/prod-before.txt") 行"
fi

# ---------------------------------------------------------------------------------------------
# 起栈 → 跑场景 → 拆栈
# ---------------------------------------------------------------------------------------------
STACK_STARTED=1
say "起栈"
bash "${SUITE_DIR}/stackctl.sh" up 2>&1 | tee "${OUT}/stack-up.log"
UP_RC="${PIPESTATUS[0]}"
if [ "${UP_RC}" != 0 ]; then
  echo "起栈失败（rc=${UP_RC}），见 ${OUT}/stack-up.log" >&2
  RC=3
  exit 3
fi

SCEN_ARGS=(--expect "${EXPECT}" --out "${OUT}" --image-label "${IMAGE}")
[ -n "${ONLY}" ] && SCEN_ARGS+=(--only "${ONLY}")
[ "${SKIP_LOG}" = 1 ] && SCEN_ARGS+=(--skip-log-checks)
say "跑场景"
python3 -B "${SUITE_DIR}/scenarios.py" "${SCEN_ARGS[@]}" 2>&1 | tee "${OUT}/scenarios-${EXPECT}.log"
RC="${PIPESTATUS[0]}"

# 网关侧证据：整个运行的网关日志（拆栈前取），便于事后核对 websocket_request_memory_rejected 等
mkdir -p "${BHV_LOGDIR}"
docker logs "${BHV_PROJECT}-gw" > "${BHV_LOGDIR}/gateway-last.log" 2>&1 || true
say "网关日志已存 ${BHV_LOGDIR}（全部重建前后的日志合计 $(cat "${BHV_LOGDIR}"/gateway-*.log | grep -c websocket_request_memory_rejected) 条 websocket_request_memory_rejected）"

FINISHED=1
teardown

# ---------------------------------------------------------------------------------------------
# 拆栈后的对比：现网未动、根盘
# ---------------------------------------------------------------------------------------------
if [ "${PROD_GUARD}" = 1 ]; then
  prod_snapshot > "${OUT}/prod-after.txt" 2>&1
  if grep -q -E '^现网库 (ERROR|Error|psql|FATAL)' "${OUT}/prod-before.txt" "${OUT}/prod-after.txt"; then
    say "现网对比：现网库快照取数失败（见 ${OUT}/prod-before.txt），无法证明库未动"
    [ "${RC}" = 0 ] && RC=1
  elif diff "${OUT}/prod-before.txt" "${OUT}/prod-after.txt" > "${OUT}/prod-diff.txt"; then
    say "现网对比：容器 ID／启动时间／重启次数与现网库行数／校验和前后完全一致"
  else
    say "现网对比：前后不一致！见 ${OUT}/prod-diff.txt（现网容器重启或库行数变化，需人工判断是否与本套件无关）"
    cat "${OUT}/prod-diff.txt"
    if [ "${RC}" = 0 ]; then
      RC=1
    fi
  fi
fi
say "根盘（跑完后）：$(disk_line)（起跑前：${DISK_BEFORE}）"
LEFT="$(docker ps -a --format '{{.Names}}' --filter "name=^${BHV_PROJECT}-"; docker network ls --format '{{.Name}}' --filter "name=^${BHV_PROJECT}-"; docker images --format '{{.Repository}}' | grep "^${BHV_PROJECT}-" || true)"
if [ -n "${LEFT}" ]; then
  say "拆栈后仍有残留：${LEFT}"
  [ "${RC}" = 0 ] && RC=1
else
  say "拆栈核对：无 ${BHV_PROJECT}-* 容器、网络、派生镜像残留"
fi
exit "${RC}"
