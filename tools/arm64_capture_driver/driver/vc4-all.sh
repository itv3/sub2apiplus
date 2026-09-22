#!/bin/bash
# VC-4 一条龙：四树 → 前端构建 + 五门禁并行 → 构建 → 等待并注入本机 check-egress-spec 日志 → 派发前检查（pre-vc4）
#   → revision-open --initial → vc4.sh（实现测试收据 + plan-candidate-gates + record-candidate-build）。
# 用法：ARM64_VC_ENV=… setsid -f bash vc4-all.sh > $RUNROOT/vc4-all.out 2>&1 < /dev/null
#   本机随后把实现测试日志上传到 $RUNROOT/impl-logs/（check-egress-spec.log、cross-check/、READY）。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
IMPL=$RUNROOT/impl-logs
bash "$DRV/trees.sh" 2>&1 | tail -4
rm -f "$RUNROOT/frontend.out" "$RUNROOT/gates.out"
ESTAMP=$(date -u +%Y%m%dt%H%M%Sz); E=$D/control/c0154-vc4-implementation-tests-$ESTAMP; echo "$E" > "$RUNROOT/E.txt"
(nohup bash "$DRV/frontend.sh" > "$RUNROOT/frontend.out" 2>&1 &)
(nohup bash "$DRV/vc4-gates.sh" "$E" > "$RUNROOT/gates.out" 2>&1 &)
until grep -q "GATES_DONE" "$RUNROOT/gates.out" 2>/dev/null && grep -q "FRONTEND_DONE\|build exit=[1-9]" "$RUNROOT/frontend.out" 2>/dev/null; do sleep 10; done
echo "gates exit_code=0 count: $(grep -c '^exit_code=0' $E/logs/implementation.log)"; grep FRONTEND_DONE "$RUNROOT/frontend.out"
grep -E "^exit_code=|^## gate" $E/logs/implementation.log | paste - - | cut -c1-120
test "$(grep -c '^exit_code=0' $E/logs/implementation.log)" = 5
bash "$DRV/build.sh" "$C" 2>&1 | tail -3
# 等待本机上传的本地实现测试日志（DC 提交工作树上 make check-egress-spec，及候选 commit 自身的交叉核对）
until [ -f "$IMPL/check-egress-spec.log" ] && [ -f "$IMPL/cross-check/check-egress-spec.C-only.local.log" ] && [ -f "$IMPL/READY" ]; do sleep 15; done
grep -q "^exit_code=0" "$IMPL/check-egress-spec.log"
mkdir -p "$E/logs/cross-check"
cp "$IMPL/check-egress-spec.log" "$E/logs/check-egress-spec.log"
cp "$IMPL/cross-check/check-egress-spec.C-only.local.log" "$E/logs/cross-check/check-egress-spec.C-only.local.log"
chown -R root:root "$E"; find "$E" -type f -exec chmod 600 {} +; find "$E" -type d -exec chmod 700 {} +
echo "=== 派发前检查（驱动/磁盘/总账/账本）"; bash "$DRV/guard.sh" pre-vc4 "$NEWDIR"
echo "=== revision-open --initial（改造 2：VC-4 首批前登记 r1，幂等）"
( cd "$D" && /usr/bin/python3 tools/official_client_capture/codex_upgrade.py revision-open --campaign-dir "$NEWDIR" --candidate-id "$CAND" --initial | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('revision-open:', {k:d.get(k) for k in ('revision','status','candidate_id')}, 'ledger:', (d.get('ledger_event') or {}).get('head_sequence'))" )
bash "$DRV/vc4.sh" "$E" 2>&1 | grep -E "逐字一致|预演结果|rc=|actions:|TREE=|VC-4 checkpoint|build-receipt|账本|gates:|replay:|VC4_DONE|Error|Traceback|拒绝|失败|BATCH" | cut -c1-260
echo "VC4_ALL_DONE E=$E $(utc_now)"
