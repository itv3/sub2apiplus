#!/usr/bin/env bash
# 从开发机把本套件同步到远端 docker 主机（如 ARM64 测试机）并跑一次。
#
# 用法：
#   bash remote_run.sh <ssh 主机别名> <网关镜像> [run.sh 的其它参数...]
#   BHV_DETACH=1 bash remote_run.sh <主机> <镜像> ...   后台跑（远端 setsid -f，日志以 EXIT= 结尾），立即返回
#
# 环境变量：BHV_REMOTE_DIR（远端放置目录，默认 /root/behavior-acceptance）、BHV_REMOTE_OUT（远端结果目录，默认 <放置目录>/out）。
# 只同步本目录里的套件文件到远端 BHV_REMOTE_DIR；远端只会创建 bhvacc-* 容器 / 网络 / 派生镜像，不碰现网。
set -u
HOST="${1:-}"
IMAGE="${2:-}"
if [ -z "${HOST}" ] || [ -z "${IMAGE}" ]; then
  echo "用法：bash remote_run.sh <ssh 主机别名> <网关镜像> [run.sh 的其它参数...]" >&2
  exit 2
fi
shift 2
SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RDIR="${BHV_REMOTE_DIR:-/root/behavior-acceptance}"
ROUT="${BHV_REMOTE_OUT:-${RDIR}/out}"
STAMP="$(date +%Y%m%d-%H%M%S)"

# 同步套件文件（不带 __pycache__ 与结果目录）
tar -C "${SUITE_DIR}" --exclude='__pycache__' --exclude='bhv-out-*' -cf - . | ssh "${HOST}" "mkdir -p '${RDIR}' && tar -xf - -C '${RDIR}'" || exit 2

ARGS=""
for a in "$@"; do
  ARGS="${ARGS} $(printf '%q' "${a}")"
done
if [ "${BHV_DETACH:-0}" = 1 ]; then
  LOG="${RDIR}/run-${STAMP}.log"
  ssh -n "${HOST}" "cd '${RDIR}' && PYTHONDONTWRITEBYTECODE=1 setsid -f bash -c \"bash run.sh '${IMAGE}' --out '${ROUT}-${STAMP}'${ARGS} > '${LOG}' 2>&1\""
  echo "已在 ${HOST} 后台启动，日志 ${LOG}（最后一行 EXIT=<码> 表示结束），结果目录 ${ROUT}-${STAMP}"
  exit 0
fi
ssh -n "${HOST}" "cd '${RDIR}' && PYTHONDONTWRITEBYTECODE=1 bash run.sh '${IMAGE}' --out '${ROUT}-${STAMP}'${ARGS}"
