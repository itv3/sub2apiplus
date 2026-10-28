#!/bin/bash
# 把采集账号设为 status=active、schedulable=true（幂等）。账号号取 pipeline.env 的 CAPTURE_ACCOUNT_ID，缺省取本轮参数文件的采集账号。
# 采集账号平时关闭、需要发真实请求时才打开，升级结束后由 capture-account-off.sh 关回去。只改这一个账号的这两个字段，
# 动手前打印并写日志 ${PREP}/logs/capture-account.log（当前状态、改什么、时间），动完再写结果。细节与安全约束见 capture_admin.py 顶部。
# 退出码：0 已是/已成为目标状态；2 账号身份对不上（拒绝）；3 另一个实例在跑；4 访问令牌快过期（拒绝）；5 管理接口失败；6 改完核对不符。
set -u
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
export CAPTURE_ACCOUNT_ID=${CAPTURE_ACCOUNT_ID:-${CLIENT_ACCOUNT_ID:-${CODEX_ACCOUNT_ID:-}}}
export PIPE_COMPOSE_ENV=${PIPE_COMPOSE_ENV:-${COMPOSE_DIR}/.env}
mkdir -p "${PREP}/logs"
exec python3 -B "${PIPE_HOME}/capture_admin.py" account on
