#!/bin/bash
# 本机：VC-6 两次本机交接自动执行。流水线 VC6 段跑到 handoff 行时会等交接标记（--wait-handoff 60 分钟），
# 这里每 60 秒查一次 ARM64：前一 run 步骤已成功、交接标记还没有，就在本机跑对应的 vc6-local-commit.sh 子命令（它自己会建标记）。
#   handoff local-sync   ← vc6-done/gates 已有   → vc6-local-commit.sh sync
#   handoff local-retire ← vc6-done/canonical-activation 已有 → vc6-local-commit.sh retire
# 任一子命令失败即退出、不重试（流水线会因等不到标记而 STOPPED，调度者查日志后手工处理）；例外：失败的同时交接标记已经存在（别处——例如调度者手工——
# 已经做完了这次交接），按已完成处理，不算失败；retire 完成后退出；ARM64 状态文件在本脚本启动后出现 EXIT 行且不是 VC6 段内的交接等待，也退出。
# 启动前核对（Codex 升级工具优化第 7 项）：VC6_REPO 必须已经检出了候选别名分支（参数文件的 BUNDLE_BRANCH）且工作树干净，否则立刻报错退出 2——
#   0.166 的 VC6_REPO 停在 cand166 分支上，监视照常启动，到 local-retire 才在 vc6-local-commit.sh 的前置核对里失败退出（日志没人看，表现成“没触发”）。
#
# 用法（这是开发机上的监视脚本，不在采集主机上跑，也不是门禁）：
#   VC6_REPO=<候选链工作树> bash <仓库>/tools/arm64_capture_driver/driver/pipeline/local/vc6-handoff-watch.sh --detach
#   --detach：核对通过后用 Python start_new_session 脱离当前会话在后台跑，输出写进 ${VC6_LOCAL_TMP:-${TMPDIR:-/tmp}/vc6-run}/handoff-watch.out，
#     并等到监视日志里出现“启动”行才报成功（没出现就非零退出）。**不要用 setsid -f：** 这台 Mac 上没有 setsid，命令静默失败、没有进程也没有日志——0.166 第一次起监视
#     就是这样没起来，两次交接只好手工做。不带 --detach 时在前台跑（可自己放进 tmux／脱离会话的 Python）。
#   监视在跑时不要同时手工跑同一个交接（两边抢同一个工作树的提交）；要手工处理先结束监视（kill 日志里“启动”行对应的进程）。
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init || exit 2
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${VC6_LOCAL_TMP:-${TMPDIR:-/tmp}/vc6-run}
LOG=${LOGDIR}/handoff-watch.log
mkdir -p "${LOGDIR}"
DETACH=0
if [ "${1:-}" = --detach ]; then DETACH=1; shift; fi
[ "$#" -eq 0 ] || { echo "用法：VC6_REPO=<候选链工作树> bash vc6-handoff-watch.sh [--detach]"; exit 2; }
: "${VC6_REPO:?必须用 VC6_REPO 指定检出了候选分支的本机工作树}"
export VC6_REPO
ARM64=${PIPE_SSH_HOST}
DONE=${REMOTE_PREP}/pipeline/vc6-done
HO=${REMOTE_RUNROOT}/handoff
STATUS=${REMOTE_PREP}/pipeline/vc-status.txt
say() { echo "$(date -u +%FT%TZ) $*" >> "${LOG}"; }
# ---- 启动前核对：VC6_REPO 已在候选别名分支上、工作树干净（vc6-local-commit.sh 的 sync／retire 都要这两条，晚到交接时才发现就来不及了）
BRANCH=$(pl_remote_var BUNDLE_BRANCH)
[ -n "${BRANCH}" ] || { echo "取不到采集主机参数文件的 BUNDLE_BRANCH（候选别名分支）"; exit 2; }
[ -d "${VC6_REPO}" ] || { echo "VC6_REPO 不是目录：${VC6_REPO}"; exit 2; }
CURRENT=$(git -C "${VC6_REPO}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
if [ "${CURRENT}" != "${BRANCH}" ]; then
  msg="VC6_REPO（${VC6_REPO}）当前在 ${CURRENT:-（读不出分支）}，不在候选别名分支 ${BRANCH}：vc6-local-commit.sh 的 sync／retire 都会在前置核对里失败。先在该工作树里 git checkout ${BRANCH}，再起监视"
  echo "${msg}"; say "拒绝启动：${msg}"; exit 2
fi
if [ -n "$(git -C "${VC6_REPO}" status --porcelain --untracked-files=all 2>/dev/null)" ]; then
  msg="VC6_REPO（${VC6_REPO}）工作树不干净：vc6-local-commit.sh 要求干净的候选链工作树。先处理未提交的改动，再起监视"
  echo "${msg}"; say "拒绝启动：${msg}"; exit 2
fi
if [ "${DETACH}" = 1 ] && [ -z "${VC6_WATCH_CHILD:-}" ]; then
  # 核对已经在前台过了；子进程用 Python start_new_session 脱离会话（本机没有 setsid），输出进 handoff-watch.out，等它写出“启动”行才算起来了。
  mark=$(wc -l < "${LOG}" 2>/dev/null | tr -d ' ' || true); mark=${mark:-0}
  # Python 代码放进变量、用 -c 传入（不在 $( ) 里嵌 heredoc：macOS 自带的 bash 3.2 解析这种写法有已知缺陷）
  detach_py='import os, subprocess, sys
script, out = sys.argv[1:3]
with open(out, "ab") as sink:
    child = subprocess.Popen(["bash", script], stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True, env=dict(os.environ))
print(child.pid)'
  pid=$(VC6_WATCH_CHILD=1 python3 -B -c "${detach_py}" "${HERE}/vc6-handoff-watch.sh" "${LOGDIR}/handoff-watch.out"
  ) || { echo "脱离会话启动失败（python3 start_new_session）"; exit 1; }
  for _ in $(seq 1 30); do
    if tail -n +$((mark + 1)) "${LOG}" 2>/dev/null | grep -q ' 启动：'; then echo "监视已在后台跑（pid ${pid}）：日志 ${LOG}，标准输出 ${LOGDIR}/handoff-watch.out"; exit 0; fi
    kill -0 "${pid}" 2>/dev/null || break
    sleep 1
  done
  echo "监视没有在 30 秒内写出“启动”行（pid ${pid} $(kill -0 "${pid}" 2>/dev/null && echo 仍在跑 || echo 已退出)）：看 ${LOGDIR}/handoff-watch.out 与 ${LOG}"; exit 1
fi
ready() { # $1 前一 run 步骤 $2 交接名：前者已成功且交接标记不存在
  ssh -n -o ConnectTimeout=20 "${ARM64}" "test -f ${DONE}/$1 && test ! -f ${HO}/$2.done" 2>/dev/null
}
marked() { # $1 交接名：交接标记已存在
  ssh -n -o ConnectTimeout=20 "${ARM64}" "test -f ${HO}/$1.done" 2>/dev/null
}
run_mode() { # $1 子命令 $2 交接名
  say "开始 vc6-local-commit.sh $1（VC6_REPO=${VC6_REPO}）"
  if bash "${HERE}/vc6-local-commit.sh" "$1" >> "${LOGDIR}/handoff-$1.log" 2>&1 < /dev/null; then
    say "完成 $1 rc=0"
  else
    rc=$?
    # 失败时交接标记已经在了：别处（例如调度者几乎同时手工做）已经完成这次交接，不算失败。0.166 的 local-retire 就是这样：监视触发时调度者手工 retire 刚提交，
    # 监视的前置核对（候选分支头 = 同步提交）因此失败退出。给手工那一边最多 90 秒把标记建出来。
    for _ in $(seq 1 18); do
      if marked "$2"; then say "子命令 $1 失败 rc=${rc}，但交接标记 $2.done 已存在（别处已完成这次交接），按已完成处理；失败详情见 ${LOGDIR}/handoff-$1.log"; return 0; fi
      sleep 5
    done
    say "失败 $1 rc=${rc}，见 ${LOGDIR}/handoff-$1.log；本监视退出，不重试"
    exit 1
  fi
}
n0=$(ssh -n -o ConnectTimeout=20 "${ARM64}" "wc -l < ${STATUS}" 2>/dev/null || echo 0)
say "启动：状态文件起点行 ${n0}（VC6_REPO=${VC6_REPO} 在 ${BRANCH}，pid $$）"
deadline=$(( $(date +%s) + 20 * 3600 ))
while [ "$(date +%s)" -lt "${deadline}" ]; do
  if ready gates local-sync; then run_mode sync local-sync; fi
  if ready canonical-activation local-retire; then run_mode retire local-retire; say "两次交接都已完成，退出"; exit 0; fi
  # 本脚本启动后流水线写了 STOPPED 或 EXIT rc≠0：停止监视（等调度者处理后重启本脚本）
  if ssh -n -o ConnectTimeout=20 "${ARM64}" "tail -n +$((n0 + 1)) ${STATUS} | grep -Eq ' STOPPED | EXIT rc=[1-9]'" 2>/dev/null; then
    say "流水线已停（STOPPED／EXIT rc≠0），本监视退出"; exit 2
  fi
  sleep 60
done
say "20 小时到期，退出"; exit 3
