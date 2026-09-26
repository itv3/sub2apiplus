#!/bin/bash
# ARM64 抓包驱动链公共前导（被各脚本 source，不可直接执行）：
#   1. DRV = 脚本所在目录（安装目标 /root/arm64-capture-driver/driver 或仓库内 tools/arm64_capture_driver/driver）
#   2. 以 parse_env.py 安全解析本轮参数文件 ${ARM64_VC_ENV}（绝不 source：只接受精确键集合的 KEY=VALUE，
#      值里只允许引用已定义键，任何 $(…)／反引号／; 等一律拒绝；2026-09-22 审核 P1）
#   3. 建立 ${RUNROOT}，导出 D／PYTHONPATH／PATH
# 用法：source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
if [ -z "${BASH_SOURCE[1]:-}" ]; then echo "lib.sh 只能被 source"; exit 2; fi
export DRV=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${ARM64_VC_ENV:?ARM64_VC_ENV 未设置（指向本轮 env.sh，见 env.example.sh）}"
ARM64_VC_ENV_EXPORTS=$(python3 "$DRV/parse_env.py" "$ARM64_VC_ENV") || { echo "参数文件拒绝加载：$ARM64_VC_ENV"; exit 2; }
eval "$ARM64_VC_ENV_EXPORTS"; unset ARM64_VC_ENV_EXPORTS
[ -d "$RUNROOT" ] || mkdir -p "$RUNROOT"; [ "$(python3 -c "import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))" "$RUNROOT")" = 0o700 ] || chmod 700 "$RUNROOT"
export D PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 PATH=/usr/local/go/bin:/opt/node-v20/bin:$PATH
NEWDIR=$D/evidence/campaigns/$NEW
W=$D/control/$IN
TOOLS=$D/tools/official_client_capture
L=$D/control/$UP-timing-ledger
G=$D/control/$NEW-candidate-gates
AS=$D/control/$NEW-assertions
# 下一批次序号：按 control/vc/batches 中已 COMMIT 的最大序号 +1
next_seq() { python3 -c "import glob,os,sys; xs=[int(os.path.basename(p).split('-')[0]) for p in glob.glob(sys.argv[1]+'/control/vc/batches/*.json')]; print(max(xs)+1 if xs else 1)" "$NEWDIR"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# 等待失败统一退出 3；只退出当前驱动，不改 Campaign／账本或猜测恢复分支。
# 用法：wait_for_marker <文件> <正则> <总秒数> [PID] [wait_state.py 选项…]
# PID 可选：第 4 个参数不以 -- 开头时才按 PID 取走（空串表示不绑定 PID）；省略 PID 直接跟 --log 等选项时，
# 选项原样交给 wait_state.py，不会被误当成 PID。非空 PID 必须是正整数，否则直接按等待失败退出 3。
wait_for_marker() {
  local file="$1" regex="$2" max_seconds="$3" pid=""
  shift 3
  if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then pid="$1"; shift; fi
  if [ -n "$pid" ] && ! [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
    echo "等待失败：PID 必须是正整数：$pid" >&2
    exit 3
  fi
  local args=(marker "$file" --regex "$regex" --max-seconds "$max_seconds")
  if [ -n "$pid" ]; then args+=(--pid "$pid"); fi
  python3 "$DRV/wait_state.py" "${args[@]}" "$@" || exit 3
}
wait_for_heartbeat() {
  local file="$1" stale_seconds="$2" max_seconds="$3"
  shift 3
  python3 "$DRV/wait_state.py" heartbeat "$file" --stale-seconds "$stale_seconds" --max-seconds "$max_seconds" "$@" || exit 3
}
# 沿用参数文件中的阶段预算，并在每次重起时仍受项目绝对截止约束。
wait_budget() {
  python3 - "$1" "$STAGE_BUDGETS" "$PROJECT_DEADLINE_UTC" <<'PY'
import datetime, sys, time
phase, budgets, deadline = sys.argv[1:]
minutes = dict(item.split('=', 1) for item in budgets.split())
remaining = datetime.datetime.fromisoformat(deadline.replace('Z', '+00:00')).timestamp() - time.time()
print(max(1, int(min(float(minutes.get(phase, '90'))*60, remaining))))
PY
}
