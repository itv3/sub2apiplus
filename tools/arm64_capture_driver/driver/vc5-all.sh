#!/bin/bash
# VC-5 一条龙（按阶段状态续跑；重跑时已完成的阶段一律跳过）：
#   外部门禁六件套注入（本机 check-egress-spec + full-regression）+ 复核 test-tree（DC 提交）
#   → start（pre-vc5 检查 + 切候选网关 + 预检 + 后台 run 批次）→ 等待 run → seal 链（仅当 compare 结果不存在）
#   → accept（仅当 acceptance 结果不存在；目标平台门禁失败在 accept 内归档重跑，不回到 seal 链）
#   → canonical2（离线预览 + 两个纯 canonical 批次）→ VC-5 completion。
# 用法：ARM64_VC_ENV=… setsid -f bash vc5-all.sh > $RUNROOT/vc5-all.out 2>&1 < /dev/null
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
LOCAL_GATES=$RUNROOT/local-gates
if [ ! -f "$G/local/full-regression.gate.json" ]; then
  echo "=== 外部门禁：本机 check-egress-spec 与 full-regression 六件套注入 + 复核 test-tree（DC 提交 ${DC}）$(utc_now)"
  mkdir -p "$G/local"; chmod 700 "$G" "$G/local"
  for f in check-egress-spec.gate.json check-egress-spec.stdout.log check-egress-spec.stderr.log full-regression.gate.json full-regression.stdout.log full-regression.stderr.log; do test -f "$LOCAL_GATES/$f"; cp -f "$LOCAL_GATES/$f" "$G/local/"; done
  chown root:root "$G/local"/*; chmod 600 "$G/local"/*
  python3 -c "import json; d=json.load(open('$G/local/full-regression.gate.json')); assert d['exit_code']==0 and d['tree_head']=='$DC', d; print('full-regression(本机):', d['exit_code'], d['host'], d['architecture'], d['started_at_utc'], d['completed_at_utc'])"
  test "$(git -C $B/test-tree rev-parse HEAD)" = "$DC" || bash "$DRV/gates.sh" prepare "$DC" 2>&1 | tail -n 3
fi
if [ ! -f "$RUNROOT/vc5-run-batch.out" ]; then
  echo "=== VC-5 start $(utc_now)"; bash "$DRV/vc5-start.sh" 2>&1 | tail -n 12 | cut -c1-240; test -f "$RUNROOT/vc5-run-batch.out"
fi
echo "=== 等待 run 批次（candidate run）$(utc_now)"
wait_for_marker "$RUNROOT/vc5-run-batch.out" '^RUN_BATCH_DONE ' "$(wait_budget VC-5)" '' \
  --pid-file "$RUNROOT/vc5-run-batch.pid" --supervisor-root "$D/control/$NEW-supervisor" \
  --log "$RUNROOT/vc5-all.out"
grep -E "rc=|actions:|timing events|admission" "$RUNROOT/vc5-run-batch.out" | cut -c1-300
ATT=$(ls -1t "$NEWDIR/candidates/$CAND/attempts/" | head -1); echo "ATT=$ATT"
python3 -c "import json; d=json.load(open('$NEWDIR/candidates/$CAND/attempts/$ATT/attempt.json')); rs=d.get('results') or []; print('attempt:', {k:d.get(k) for k in ('status','attempt_id','started_at_utc')}, 'complete:', sum(1 for r in rs if r.get('status')=='complete'), '/', len(rs))"
if [ ! -f "$NEWDIR/comparisons/$CAND/result.json" ]; then
  echo "=== seal 链 $(utc_now)"; bash "$DRV/vc5-seal.sh" "$ATT" 2>&1 | { grep -E "===|ATT=|batch ok|BATCH FAILED|REHEARSAL|预演:|CKPT=|SEAL_SHA=|candidate result|compare:|账本|SEAL_DONE|SEAL_ABORT|PERMISSION_CLOSEOUT|Error|Traceback|拒绝|失败|assert" || true; } | cut -c1-300
else
  echo "=== seal 链已完成（comparisons/$CAND/result.json 存在），跳过 $(utc_now)"
fi
test -f "$NEWDIR/comparisons/$CAND/result.json"
if [ ! -f "$NEWDIR/acceptance/$CAND/result.json" ]; then
  echo "=== accept $(utc_now)"; bash "$DRV/vc5-accept.sh" "$ATT" 2>&1 | { grep -E "===|assertions:|evaluation-run:|target-platform|归档|replay:|batch ok|BATCH FAILED|acceptance:|账本|ACCEPT_DONE|Error|Traceback|拒绝|失败|缺少" || true; } | cut -c1-300
else
  echo "=== accept 已完成（acceptance/$CAND/result.json 存在），跳过 $(utc_now)"
fi
test -f "$NEWDIR/acceptance/$CAND/result.json"
ls "$NEWDIR/control/vc/" | tr "\n" " "; echo
echo "=== canonical 交接 $(utc_now)"; bash "$DRV/vc5-canonical2.sh" "$ATT" 2>&1 | { grep -E "review_sha256|CLI preview|一致|plan ->|===|rc=|actions:|checkpoint|import-receipt|canonical latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败" || true; } | cut -c1-300
test -f "$NEWDIR/control/vc/receipts/$CAND/vc5-completion.json"
python3 -c "
import json,glob
for p in sorted(glob.glob('$NEWDIR/control/vc/vc-5-checkpoint.json')):
    c=json.load(open(p)); print('VC-5 checkpoint:', {k:c.get(k) for k in ('phase','status','completed_at_utc')})
"
echo "VC5_ALL_DONE ATT=$ATT $(utc_now)"
