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
# 管理 token 自动续签（修好接着跑第 33 项）：候选采集用的 admin JWT 由 JWT_EXPIRE_HOUR（默认 24 小时）控制，VC-5 预检要求
# 剩余 ≥1800 秒，而 run／seal／accept／canonical 全程可能跨越十几个小时。剩余不足 ${ADMIN_TOKEN_MIN_SECONDS:-43200} 秒（12 小时）
# 或传入 force 时，按 vc23.sh 同一方式在服务容器内重签（旧 token 改名留档、只输出剩余分钟、绝不输出 token 本身）。
# 用法：ensure_admin_token [force]
ensure_admin_token() {
  local st="$D/state/$UP" min="${ADMIN_TOKEN_MIN_SECONDS:-43200}" remaining=0
  if [ "${1:-}" = "force" ]; then
    echo "管理 token：force 重签"
  elif [ ! -s "$st/admin-token" ]; then
    # 首次签发一直是 vc23.sh 的事；续签入口遇到缺失只提示，交给后续预检失败关闭（stub／只读场景不触碰签发环境）。
    echo "管理 token 缺失：本入口不签发（由 vc23.sh 首次签发，后续预检失败关闭）"; return 0
  else
    remaining=$(python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print(max(0, int(d['exp'])-int(time.time())))" "$st/admin-token") || { echo "管理 token 无法解码，失败关闭"; return 3; }
    if [ "$remaining" -ge "$min" ]; then echo "管理 token 剩余 $((remaining/60)) 分钟（≥ $((min/60)) 分钟，不重签）"; return 0; fi
    echo "管理 token 剩余 $((remaining/60)) 分钟 < $((min/60)) 分钟，重签"
  fi
  mkdir -p "$st"; chmod 700 "$st"
  if [ -e "$st/admin-token" ]; then mv "$st/admin-token" "$st/admin-token.superseded-$(date -u +%Y%m%dt%H%M%Sz)"; fi
  ( cd "$COMPOSE_DIR" && set -a && . ./.env && set +a; DATABASE_HOST="$(docker inspect sub2apiplus-postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")" DATABASE_PORT=5432 DATABASE_USER="$POSTGRES_USER" DATABASE_PASSWORD="$POSTGRES_PASSWORD" DATABASE_DBNAME="$POSTGRES_DB" DATABASE_SSLMODE=disable JWT_SECRET="$JWT_SECRET" JWT_EXPIRE_HOUR="${JWT_EXPIRE_HOUR:-24}" timeout 30 "$JWTGEN_BIN" -email "$ADMIN_EMAIL" 2>/dev/null | sed -n "s/^JWT=//p" | head -1 ) > "$st/admin-token.tmp"
  test -s "$st/admin-token.tmp"; printf "%s" "$(cat "$st/admin-token.tmp")" > "$st/admin-token"; rm -f "$st/admin-token.tmp"; chmod 400 "$st/admin-token"
  python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print('token exp 剩余分钟:', (int(d['exp'])-int(time.time()))//60)" "$st/admin-token"
}
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
