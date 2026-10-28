#!/bin/bash
# 本机：VC-6 两次本机交接自动执行。流水线 VC6 段跑到 handoff 行时会等交接标记（--wait-handoff 60 分钟），
# 这里每 60 秒查一次 ARM64：前一 run 步骤已成功、交接标记还没有，就在本机跑对应的 vc6-local-commit.sh 子命令（它自己会建标记）。
#   handoff local-sync   ← vc6-done/gates 已有   → vc6-local-commit.sh sync
#   handoff local-retire ← vc6-done/canonical-activation 已有 → vc6-local-commit.sh retire
# 任一子命令失败即退出、不重试（流水线会因等不到标记而 STOPPED，调度者查日志后手工处理）；retire 完成后退出；
# ARM64 状态文件在本脚本启动后出现 EXIT 行且不是 VC6 段内的交接等待，也退出。
# 用法（这是开发机上的监视脚本，不在采集主机上跑，也不是门禁；可脱离会话）：
#   VC6_REPO=<候选链工作树> setsid -f bash <仓库>/tools/arm64_capture_driver/driver/pipeline/local/vc6-handoff-watch.sh > /dev/null 2>&1 < /dev/null
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init || exit 2
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${VC6_LOCAL_TMP:-${TMPDIR:-/tmp}/vc6-run}
LOG=${LOGDIR}/handoff-watch.log
mkdir -p "${LOGDIR}"
: "${VC6_REPO:?必须用 VC6_REPO 指定检出了候选分支的本机工作树}"
export VC6_REPO
ARM64=${PIPE_SSH_HOST}
DONE=${REMOTE_PREP}/pipeline/vc6-done
HO=${REMOTE_RUNROOT}/handoff
STATUS=${REMOTE_PREP}/pipeline/vc-status.txt
say() { echo "$(date -u +%FT%TZ) $*" >> "${LOG}"; }
ready() { # $1 前一 run 步骤 $2 交接名：前者已成功且交接标记不存在
  ssh -n -o ConnectTimeout=20 "${ARM64}" "test -f ${DONE}/$1 && test ! -f ${HO}/$2.done" 2>/dev/null
}
run_mode() { # $1 子命令
  say "开始 vc6-local-commit.sh $1（VC6_REPO=${VC6_REPO}）"
  if bash "${HERE}/vc6-local-commit.sh" "$1" >> "${LOGDIR}/handoff-$1.log" 2>&1 < /dev/null; then
    say "完成 $1 rc=0"
  else
    say "失败 $1 rc=$?，见 ${LOGDIR}/handoff-$1.log；本监视退出，不重试"
    exit 1
  fi
}
n0=$(ssh -n -o ConnectTimeout=20 "${ARM64}" "wc -l < ${STATUS}" 2>/dev/null || echo 0)
say "启动：状态文件起点行 ${n0}"
deadline=$(( $(date +%s) + 20 * 3600 ))
while [ "$(date +%s)" -lt "${deadline}" ]; do
  if ready gates local-sync; then run_mode sync; fi
  if ready canonical-activation local-retire; then run_mode retire; say "两次交接都已完成，退出"; exit 0; fi
  # 本脚本启动后流水线写了 STOPPED 或 EXIT rc≠0：停止监视（等调度者处理后重启本脚本）
  if ssh -n -o ConnectTimeout=20 "${ARM64}" "tail -n +$((n0 + 1)) ${STATUS} | grep -Eq ' STOPPED | EXIT rc=[1-9]'" 2>/dev/null; then
    say "流水线已停（STOPPED／EXIT rc≠0），本监视退出"; exit 2
  fi
  sleep 60
done
say "20 小时到期，退出"; exit 3
