#!/bin/bash
# 把采集账号恢复成 status=inactive、schedulable=false（幂等）。账号号取 pipeline.env 的 CAPTURE_ACCOUNT_ID，缺省取本轮参数文件的采集账号。
# 升级结束（或验证完）后必须关回去：账号一旦 active，刷新服务会在访问令牌过期前 30 分钟刷新它并轮换刷新令牌，让别处同一登录的令牌失效。
# 只改这一个账号的这两个字段，动手前打印并写日志 ${PREP}/logs/capture-account.log。退出码同 capture-account-on.sh。
# 注意：VC-5 里必须等 vc5-all 打出 VC5_ALL_DONE 之后才能关（seal 要对比 Kilo 后与候选采集后的账号状态，提前关会被判环境污染）；
# vc-pipeline.sh 已按此顺序调用，手工执行前先确认 vc5-all 已结束。
set -u
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
export CAPTURE_ACCOUNT_ID=${CAPTURE_ACCOUNT_ID:-${CLIENT_ACCOUNT_ID:-${CODEX_ACCOUNT_ID:-}}}
export PIPE_COMPOSE_ENV=${PIPE_COMPOSE_ENV:-${COMPOSE_DIR}/.env}
mkdir -p "${PREP}/logs"
exec python3 -B "${PIPE_HOME}/capture_admin.py" account off
