#!/bin/bash
# 本机 VC-4／VC-5 门禁＋上传包装。
# ARM64 上 vc4-all.sh 等待本机上传的心跳最多 300 秒（wait_state.py heartbeat：心跳文件缺失或过期即失败），
# 而本机 check-egress-spec 门禁与 make test 全量回归要跑几十分钟；此前心跳只在 local-upload.sh 开始后才发，
# 总控必然因"上传心跳一直缺失"退出，只能 --resume-from upload-wait 续跑。本包装在门禁一开始就向 ARM64 发心跳：
#   1. 建远端目录、清 READY、touch 心跳，后台每 30 秒一次（单次 ssh 失败不中断，远端 300 秒窗口能容忍）；
#   2. 启动 local-gate.sh（它先清空并重建输出目录），目录建好后再启动 local-full-regression.sh（两者本就并行，
#      后启动避免被 local-gate 的清空删掉产物），两者都结束才继续；
#   3. 停本包装的心跳，交给 local-upload.sh（它自带心跳，完成时写 READY）。
# 用法：bash local-vc4.sh <ROUND> <C> <DC> <RECEIPT 相对路径> <输出根> <ARM64 RUNROOT>
#   环境同 local-gate.sh（REPO、GATES_ROOT、HISTORICAL_SOURCE_ROOT）。
set -Eeuo pipefail
[ "$#" = 6 ] || { echo '用法：bash local-vc4.sh <ROUND> <C> <DC> <RECEIPT> <输出根> <ARM64 RUNROOT>'; exit 3; }
ROUND="$1"; C="$2"; DC="$3"; RECEIPT="$4"; OUTROOT="$5"; RUNROOT="$6"
# 远端命令只接受独立子目录；限制字符同时防止参数插入远端 shell（与 local-upload.sh 同一约束）。
[[ "$RUNROOT" =~ ^/[a-zA-Z0-9/_.-]+$ ]] && [[ "$RUNROOT" != / && "$RUNROOT" != /root && "$RUNROOT" != */../* ]] || exit 3
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH=(ssh -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=2 ARM64)
"${SSH[@]}" "mkdir -p '$RUNROOT/impl-logs' && chmod 700 '$RUNROOT' '$RUNROOT/impl-logs' && rm -f '$RUNROOT/impl-logs/READY' && touch '$RUNROOT/impl-logs/HEARTBEAT'"
# 间隔可由 LOCAL_VC4_HEARTBEAT_SECONDS 覆盖（只用于测试；远端过期窗口 300 秒）。
HEARTBEAT_SECONDS=${LOCAL_VC4_HEARTBEAT_SECONDS:-30}
( while sleep "$HEARTBEAT_SECONDS"; do "${SSH[@]}" "touch '$RUNROOT/impl-logs/HEARTBEAT'" >/dev/null 2>&1 || true; done ) &
HEARTBEAT_PID=$!
GATE_PID=""; REGRESSION_PID=""
stop_all() {
  kill "$HEARTBEAT_PID" 2>/dev/null || true; wait "$HEARTBEAT_PID" 2>/dev/null || true
  for pid in $GATE_PID $REGRESSION_PID; do kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; done
}
trap stop_all EXIT
trap 'exit 3' INT TERM HUP
bash "$HERE/local-gate.sh" "$ROUND" "$C" "$DC" "$RECEIPT" "$OUTROOT" > "$OUTROOT.local-gate.out" 2>&1 &
GATE_PID=$!
# local-gate.sh 先 rm -rf 再 mkdir 输出目录；等目录建好（最多 120 秒）再启动全量回归。
for _ in $(seq 1 120); do
  [ -d "$OUTROOT/impl-logs/cross-check" ] && break
  kill -0 "$GATE_PID" 2>/dev/null || break
  sleep 1
done
[ -d "$OUTROOT/impl-logs/cross-check" ] || { echo "local-gate.sh 未建好输出目录"; cat "$OUTROOT.local-gate.out"; exit 3; }
bash "$HERE/local-full-regression.sh" "$DC" "$OUTROOT" > "$OUTROOT.local-full-regression.out" 2>&1 &
REGRESSION_PID=$!
GATE_RC=0; wait "$GATE_PID" || GATE_RC=$?; GATE_PID=""
REGRESSION_RC=0; wait "$REGRESSION_PID" || REGRESSION_RC=$?; REGRESSION_PID=""
tail -n 3 "$OUTROOT.local-gate.out"; tail -n 5 "$OUTROOT.local-full-regression.out"
# 门禁脚本自身的失败（非门禁结论）不上传；门禁结论（rc 非零）照常上传，由 ARM64 端按日志与 gate.json 判定。
[ "$GATE_RC" = 0 ] && [ "$REGRESSION_RC" = 0 ] || { echo "本机门禁脚本失败：local-gate rc=$GATE_RC full-regression rc=$REGRESSION_RC"; exit 3; }
# 交接：local-upload.sh 一开始就 touch 心跳并自带心跳循环，先停本包装的循环，避免两路 ssh 并发。
kill "$HEARTBEAT_PID" 2>/dev/null || true; wait "$HEARTBEAT_PID" 2>/dev/null || true
trap - EXIT
bash "$HERE/local-upload.sh" "$OUTROOT" "$RUNROOT"
echo "LOCAL_VC4_DONE"
