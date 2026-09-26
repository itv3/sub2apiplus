#!/bin/bash
# VC-4：登记当前 revision → 四树与构建 → 依据真实输入决定实现测试重跑范围 → 收据与正式派发。
# 用法：ARM64_VC_ENV=… setsid -f bash vc4-all.sh > $RUNROOT/vc4-all.out 2>&1 < /dev/null
#   本机随后把实现测试日志上传到 $RUNROOT/impl-logs/（check-egress-spec.log、cross-check/、READY）。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$D"
IMPL=$RUNROOT/impl-logs
echo "=== 基础准入检查与 revision 登记"; bash "$DRV/guard.sh" pre-plan
python3 "$DRV/vc4_resume.py" open-revision --evidence-root "$RUNROOT" || exit 3
RESUME_FROM="${1:-}"
if [ "$#" -gt 0 ]; then
  [ "$#" = 2 ] && [ "$1" = --resume-from ] && [ "$2" = upload-wait ] || { echo '用法：vc4-all.sh [--resume-from upload-wait]'; exit 3; }
  RESUME_FROM=upload-wait
fi
REUSE=0
if [ -f "$RUNROOT/E.txt" ]; then
  E=$(cat "$RUNROOT/E.txt")
  if [ "$RESUME_FROM" = upload-wait ]; then
    python3 "$DRV/vc4_resume.py" check --evidence-root "$E" --mode upload-wait || exit 3
    REUSE=1
  elif python3 "$DRV/vc4_resume.py" check --evidence-root "$E" --mode full; then
    REUSE=1
  fi
elif [ "$RESUME_FROM" = upload-wait ]; then
  echo '上传续跑拒绝：缺少等待前证据根'; exit 3
fi
if [ "$REUSE" = 0 ]; then
  bash "$DRV/trees.sh" 2>&1 | tail -4
  rm -f "$RUNROOT/frontend.out" "$RUNROOT/gates.out"
  E=$(mktemp -d "$D/control/${CAMPAIGN_PREFIX}-vc4-implementation-tests-$(date -u +%Y%m%dt%H%M%Sz)-XXXXXX")
  echo "$E" > "$RUNROOT/E.txt"
  python3 "$DRV/vc4_resume.py" prepare --evidence-root "$E" || exit 3
  EARLY=$(python3 "$DRV/vc4_resume.py" early-test-mode --evidence-root "$E")
  # 独立进程组保证失败时 Go／Docker 等后代也收到终止信号，不遗留仍在改写构建树的工作。
  set -m
  nohup bash "$DRV/frontend.sh" > "$RUNROOT/frontend.out" 2>&1 & FRONTEND_PID=$!
  GATES_PID=""
  if [ "$EARLY" = full ]; then
    nohup bash "$DRV/vc4-gates.sh" "$E" > "$RUNROOT/gates.out" 2>&1 & GATES_PID=$!
  fi
  set +m
  cleanup_children() {
    kill -TERM -- "-$FRONTEND_PID" 2>/dev/null || true
    if [ -n "$GATES_PID" ]; then kill -TERM -- "-$GATES_PID" 2>/dev/null || true; wait "$GATES_PID" 2>/dev/null || true; fi
    wait "$FRONTEND_PID" 2>/dev/null || true
  }
  trap cleanup_children EXIT
  if [ "$EARLY" = full ]; then
    wait_for_marker "$RUNROOT/gates.out" '^GATES_DONE ' "$(wait_budget VC-4)" "$GATES_PID" \
      --peer-log "$RUNROOT/frontend.out" --peer-regex '^FRONTEND_DONE ' --peer-pid "$FRONTEND_PID" --log "$E/logs/implementation.log"
    wait "$GATES_PID" || exit 3
  else
    wait_for_marker "$RUNROOT/frontend.out" '^FRONTEND_DONE ' "$(wait_budget VC-4)" "$FRONTEND_PID"
  fi
  wait "$FRONTEND_PID" || { tail -n 200 "$RUNROOT/frontend.out"; exit 3; }
  trap - EXIT
  bash "$DRV/build.sh" "$C" 2>&1 | tail -3
  python3 "$DRV/implementation_gates.py" plan-retest "$E" || exit 3
  MODE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["decision"]["mode"])' "$E/retest-plan.json")
  if [ "$EARLY" = full ] && [ "$MODE" != full ]; then echo '提前执行的门禁与最终输入计划不一致，拒绝声明复用'; exit 3; fi
  if [ "$MODE" != all ] && [ "$EARLY" != full ]; then
    set -m
    nohup bash "$DRV/vc4-gates.sh" "$E" > "$RUNROOT/gates.out" 2>&1 & GATES_PID=$!
    set +m
    cleanup_gates() { kill -TERM -- "-$GATES_PID" 2>/dev/null || true; wait "$GATES_PID" 2>/dev/null || true; }
    trap cleanup_gates EXIT
    wait_for_marker "$RUNROOT/gates.out" '^GATES_DONE ' "$(wait_budget VC-4)" "$GATES_PID" --log "$E/logs/implementation.log"
    wait "$GATES_PID" || { tail -n 200 "$RUNROOT/gates.out"; exit 3; }
    trap - EXIT
  fi
  if [ "$MODE" != full ]; then
    TREE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inputs"]["source_tree_sha256"])' "$E/retest-plan.json")
    bash "$DRV/vc4-facts.sh" "$E" "$TREE"
  fi
  python3 "$DRV/vc4_resume.py" record-upload-wait --evidence-root "$E" || exit 3
else
  echo 'VC4_REUSED：构建输入和成功产物一致，跳过四树、门禁与构建'
fi
# 等待本机上传的本地实现测试日志（DC 提交工作树上 make check-egress-spec，及候选 commit 自身的交叉核对）
if [ ! -f "$E/receipt.json" ]; then
wait_for_heartbeat "$IMPL/HEARTBEAT" 300 "$(wait_budget VC-4)" --log "$RUNROOT/vc4-all.out"
python3 "$DRV/upload_manifest.py" verify "$RUNROOT" || exit 3
grep -q "^exit_code=0" "$IMPL/check-egress-spec.log"
mkdir -p "$E/logs/cross-check"
cp "$IMPL/check-egress-spec.log" "$E/logs/check-egress-spec.log"
cp "$IMPL/cross-check/check-egress-spec.C-only.local.log" "$E/logs/cross-check/check-egress-spec.C-only.local.log"
chown -R root:root "$E"; find "$E" -type f -exec chmod 600 {} +; find "$E" -type d -exec chmod 700 {} +
fi
echo "=== 派发前检查（驱动/磁盘/总账/账本）"; bash "$DRV/guard.sh" pre-vc4 "$NEWDIR"
bash "$DRV/vc4.sh" "$E" 2>&1 | grep -E "逐字一致|预演结果|rc=|actions:|TREE=|VC-4 checkpoint|build-receipt|账本|gates:|replay:|VC4_DONE|Error|Traceback|拒绝|失败|BATCH" | cut -c1-260
echo "VC4_ALL_DONE E=$E $(utc_now)"
