#!/bin/bash
# Claude 的 VC-5 seal 链（Claude 升级 B5b-01b，客户端表 vc5_seal_script）：按客户端收据就位情况分步派发，每一步之前由
#   claude_vc5.py seal-state 判定下一步——
#   1. 两项 attempt 级边界核对（capture-candidate boundary-check，阶段项 candidate-seal，每项一个批次，零请求的第三方入口拒绝
#      在前；codex-final-wire-isolation 带真实请求确认与参数文件的四个轮次参数）。某项批次失败：reconcile-supervisor-run 入账、
#      修复后重起 vc5-all.sh，seal 链生成同一份计划，即 N+1 逐字重派这一项（不作废候选 attempt）；已在的收据不合法即中止，
#      人工审计（不自动删除或重跑）；
#   2. Desktop／VS Code 客户端收据（Mac 侧产出，放进 attempt 的 client-evidence 目录）没就位：暂停；
#   3. 当前输入（attempt、客户端收据、门禁执行主机覆盖、工具身份）下没有 seal 预览：派发预览批次（批次内合法暂停点）；
#   4. 闭合门禁 blocked：拒绝（SEAL_ABORT，处置阻断原因后重来）；
#   5. 等审核人批准：没给 --approve-seal-sha256／--approved-by 即暂停（驱动不替审核人批准）；给了就派发「批准 + compare」批次。
#   已封存（候选阶段结果已写）而当前基线没有比较收据时只派发 compare。暂停写 $RUNROOT/vc5-pause.json（vc5-all.sh 读它打印
#   VC5_ALL_PAUSED 并退出 0）。
# 用法：bash vc5-claude-seal.sh <attempt_id> [--approve-seal-sha256 <review_sha256> --approved-by <审核人>]
set -Eeuo pipefail; umask 077
PHASE_CONTEXT_ARGS=(--mode attempt --attempt-id "$1")
CLIENT_STEP_REQUIRED=(CT_VC5_SEAL_SCRIPT vc5-claude-seal.sh)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
shift
APPROVE=""; APPROVED_BY=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --approve-seal-sha256) APPROVE="${2:-}"; shift 2 ;;
    --approved-by) APPROVED_BY="${2:-}"; shift 2 ;;
    *) echo "用法：bash vc5-claude-seal.sh <attempt_id> [--approve-seal-sha256 <摘要> --approved-by <审核人>]"; exit 2 ;;
  esac
done
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
vc5_require_admission
echo "ATT=$ATT status=$ATT_STATUS"
batch() { local seq="$1" plan="$2"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$seq" VC-4 "$plan" | grep -v "^$"; python3 - "$W/batch-$seq.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); run = d.get("campaign_run") or {}
acts = run.get("actions", [])
ok = d.get("status") == "stopped" and all(a.get("status") == "passed" for a in acts) and acts
print("batch ok" if ok else "BATCH FAILED")
sys.exit(0 if ok else 1)
PY
}
state_field() { python3 -c "import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))" "$STATE" "$1"; }
mkdir -p "$W"; chmod 700 "$W"
if [ "$CAPTURE_READY" = 1 ]; then
  if [ "$COMPARE_READY" != 1 ]; then
    test -n "$COMPARE_WRITE" || { echo "比较来源只读，不能派发"; exit 3; }
    python3 "$DRV/claude_vc5.py" compare-plan --output "$W/action-plan-vc5-compare.json"
    SEQ=$(next_seq); echo "=== 批次 ${SEQ}：compare"; batch "$SEQ" action-plan-vc5-compare.json
  fi
  phase_require --mode attempt --attempt-id "$ATT"
  test "$COMPARE_READY" = 1
  echo "SEAL_DONE ATT=$ATT SEALED=1"; exit 0
fi
test -n "$CAPTURE_WRITE" || { echo "采集来源只读，不能派发封存"; exit 3; }
for round in 1 2 3 4 5 6; do
  STATE=$(python3 "$DRV/claude_vc5.py" seal-state)
  NEXT=$(state_field next)
  echo "=== seal 链下一步：$NEXT"
  case "$NEXT" in
    boundary_invalid)
      echo "SEAL_ABORT 边界核对收据不合法（人工审计，不自动删除或重跑）：$(state_field boundary_invalid)"; exit 3 ;;
    boundary)
      for CHECK in $(python3 -c "import json,sys; print(' '.join(json.loads(sys.argv[1])['boundary_missing']))" "$STATE"); do
        python3 "$DRV/claude_vc5.py" boundary-plan --check "$CHECK" --output "$W/action-plan-vc5-boundary-$CHECK.json"
        SEQ=$(next_seq); echo "=== 批次 ${SEQ}：边界核对 $CHECK"
        if ! batch "$SEQ" "action-plan-vc5-boundary-$CHECK.json"; then
          echo "BOUNDARY_FAILED check=$CHECK 批次 ${SEQ}：看 $W/batch-$SEQ.out 与 attempt 的 client-evidence/$CHECK/runs/ 下的 verdict.json；"
          echo "  reconcile-supervisor-run 入账、按失败码修复后重起 vc5-all.sh（N+1 逐字重派这一项，不作废候选 attempt）"
          exit 3
        fi
      done ;;
    clients)
      python3 "$DRV/claude_vc5.py" pause --reason client_receipts_missing \
        --next "把 $(state_field clients_missing) 的客户端收据放进 $A/client-evidence/ 后重起 vc5-all.sh"
      exit 0 ;;
    preview)
      python3 "$DRV/claude_vc5.py" seal-plan --step preview --output "$W/action-plan-vc5-seal-preview.json"
      SEQ=$(next_seq); echo "=== 批次 ${SEQ}：seal 预览"; batch "$SEQ" action-plan-vc5-seal-preview.json ;;
    blocked)
      echo "SEAL_ABORT 闭合门禁 blocked：$(state_field preview)"; exit 3 ;;
    approve)
      REVIEW=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['preview']['review_sha256'])" "$STATE"); echo "SEAL_SHA=$REVIEW"
      if [ -z "$APPROVE" ]; then
        python3 "$DRV/claude_vc5.py" pause --reason seal_approval_required \
          --next "审核 seal 预览后执行 bash $DRV/vc5-claude-seal.sh $ATT --approve-seal-sha256 $REVIEW --approved-by <审核人>，再重起 vc5-all.sh"
        exit 0
      fi
      test "$APPROVE" = "$REVIEW" || { echo "批准摘要与当前 seal 预览的 review_sha256 不一致，拒绝"; exit 3; }
      python3 "$DRV/claude_vc5.py" seal-plan --step approve --approve-seal-sha256 "$APPROVE" --approved-by "$APPROVED_BY" \
        --output "$W/action-plan-vc5-seal-approve-compare.json"
      SEQ=$(next_seq); echo "=== 批次 ${SEQ}：seal 批准 + compare"; batch "$SEQ" action-plan-vc5-seal-approve-compare.json
      phase_require --mode attempt --attempt-id "$ATT"
      test "$CAPTURE_READY" = 1; test "$COMPARE_READY" = 1
      python3 "$DRV/claude_vc5.py" summary | cut -c1-200
      echo "SEAL_DONE ATT=$ATT SEAL_SHA=$REVIEW SEALED=1"; exit 0 ;;
    compare)
      phase_require --mode attempt --attempt-id "$ATT"; exec bash "$DRV/vc5-claude-seal.sh" "$ATT" ;;
    *) echo "SEAL_ABORT 未知的 seal 链状态：$NEXT"; exit 3 ;;
  esac
done
echo "SEAL_ABORT seal 链在 6 步内没有收敛"; exit 3
