#!/bin/bash
# VC-5 run 之后到 compare：Kilo 双入口 → seal checkpoint → observed-profile/Kilo 收据（含权限收口）→
#   预演 → assertion bundle → 预演 → seal 预览 → 预演 → 批准 + compare（批次号自动取 max+1）。
# 用法：bash vc5-seal.sh <attempt_id>
# 幂等与硬边界（2026-09-22 审核 P1）：evidence-manifest.json（seal 预览产物）存在后证据根绝对只读——
#   Kilo／seal checkpoint／assertion bundle／seal 预览这四个写动作一律不再派发；任何前置产物缺失即失败关闭（退出 3），
#   只允许读侧复核（seal-receipts 只核对模式）与批准 + compare。manifest 不存在时按产物存在与否逐步续跑。
set -Eeuo pipefail; umask 077
PHASE_CONTEXT_ARGS=(--mode attempt --attempt-id "$1")
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
vc5_require_admission
echo "ATT=$ATT status=$ATT_STATUS"
# 已封存采集可来自前序基线；仅对当前 local 比较目标补跑，禁止重写承接证据。
if [ "$CAPTURE_READY" = 1 ]; then
  if [ "$COMPARE_READY" != 1 ]; then
    test -n "$COMPARE_WRITE" || { echo "比较来源只读，不能派发"; exit 3; }
    python3 -B "$DRV/gen_vc5_plans.py" "$W" "$NEW" "$CAND" "$IMAGE_ID" "$BUILD_ID" "$ATT"
    bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$(next_seq)" VC-4 action-plan-vc5-compare.json
  fi
  phase_require --mode attempt --attempt-id "$ATT"
  test "$COMPARE_READY" = 1
  echo "SEAL_DONE ATT=$ATT SEALED=1"; exit 0
fi
test -n "$CAPTURE_WRITE" || { echo "采集来源只读，不能派发封存"; exit 3; }
MANIFEST="$A/evidence-manifest.json"; SEALED=0
if [ -e "$MANIFEST" ] || [ -L "$MANIFEST" ]; then
  SEALED=1; echo "=== evidence-manifest.json 已存在：证据根只读，只做读侧复核与批准 + compare"
  for f in "$EV/client/raw/kilo-facts.json" "$EV/environment/client-after/probe-manifest.json" "$EV/assertion-bundle/capture-manifest.json" "$A/seal-preview.json"; do
    test -f "$f" || { echo "SEAL_ABORT: manifest 已存在但前置产物缺失：${f}（不可补写，需人工裁定）"; exit 3; }
  done
fi
batch() { local seq="$1" plan="$2"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$seq" VC-4 "$plan" | grep -v "^$"; python3 - "$W/batch-$seq.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); run = d.get("campaign_run") or {}
acts = run.get("actions", [])
ok = d.get("status") == "stopped" and all(a.get("status") == "passed" for a in acts) and acts
print("batch ok" if ok else "BATCH FAILED")
sys.exit(0 if ok else 1)
PY
}
rehearse() { local plan="$1"; local upper="$RUNROOT/seal-rehearsal-upper"; rm -rf "$upper"; mkdir -p "$upper"; chmod 700 "$upper"
  python3 -m tools.official_client_capture.codex_upgrade rehearse-candidate-seal --campaign-dir "$NEWDIR" --candidate-id "$CAND" --attempt-id "$ATT" --action-plan "$W/$plan" --data-root "$D" --alias-root /root/oauth-capture --upper-root "$upper" > "$W/rehearsal-$plan.out" 2> "$W/rehearsal-$plan.err" || { echo "REHEARSAL FAILED"; tail -c 1500 "$W/rehearsal-$plan.err"; return 1; }
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('预演:', {k:(str(d.get(k))[:70]) for k in ('status','receipt','lower_unchanged','live_request_count','actions_sha256')})" "$W/rehearsal-$plan.out"; rm -rf "$upper"; }
if [ "$SEALED" = 0 ] && [ ! -f "$EV/client/raw/kilo-facts.json" ]; then
  echo "=== 等待账号 ${CODEX_ACCOUNT_ID} 调度投影不再含 candidate-frozen-aux 临时写入的 model_mapping"
  for i in $(seq 1 60); do
    if docker exec sub2apiplus-redis sh -c "unset REDISCLI_AUTH; redis-cli --no-auth-warning get sched:acc:${CODEX_ACCOUNT_ID}" | python3 -c "import sys,json; d=json.loads(sys.stdin.read() or '{}'); sys.exit(0 if 'model_mapping' not in (d.get('Credentials') or {}) else 1)"; then echo "投影已刷新（第 $i 次检查）"; break; fi
    sleep 10
  done
  echo "=== Kilo 双入口"; bash "$DRV/vc5-kilo.sh" "$ATT" 2>&1 | tail -n 6 | cut -c1-300; test -f "$EV/client/raw/kilo-facts.json"
fi
CANDIDATE_DIR=$B PROFILE_ID=$PROFILE_ID PROFILE_DIGEST=$PROFILE_DIGEST python3 "$DRV/gen_vc5_plans.py" "$W" "$NEW" "$CAND" "$IMAGE_ID" "$BUILD_ID" "$ATT" | cut -c1-200; chmod 600 "$W"/*.json
SEQ=$(next_seq); echo "下一批次序号=$SEQ"
if [ "$SEALED" = 0 ] && [ ! -f "$EV/environment/client-after/probe-manifest.json" ]; then echo "=== 批次 ${SEQ}：seal checkpoint"; batch "$SEQ" action-plan-vc5-seal-checkpoint.json; SEQ=$((SEQ+1)); fi
CKPT=$(python3 -c "import json; print(json.load(open('$EV/environment/client-after/probe-manifest.json'))['observed_at_utc'])"); echo "CKPT=$CKPT"
echo "=== observed-profile / Kilo 收据 + 权限收口（源码树外；manifest 存在时只核对）"; bash "$DRV/vc5-seal-receipts.sh" "$ATT" "$CKPT" 2>&1 | tail -n 4 | cut -c1-200
if [ "$SEALED" = 0 ] && [ ! -f "$EV/assertion-bundle/capture-manifest.json" ]; then
  echo "=== 预演批次 ${SEQ}（assertion bundle）"; rehearse action-plan-vc5-seal-assertion.json
  echo "=== 批次 ${SEQ}：assertion bundle"; batch "$SEQ" action-plan-vc5-seal-assertion.json; SEQ=$((SEQ+1))
fi
if [ "$SEALED" = 0 ] && [ ! -f "$A/seal-preview.json" ]; then
  echo "=== 预演批次 ${SEQ}（seal 预览）"; rehearse action-plan-vc5-seal-preview.json
  echo "=== 批次 ${SEQ}：seal 预览"; batch "$SEQ" action-plan-vc5-seal-preview.json; SEQ=$((SEQ+1))
fi
SEAL_SHA=$(python3 -c "import json; print(json.load(open('$A/seal-preview.json'))['review_sha256'])"); echo "SEAL_SHA=$SEAL_SHA"
CANDIDATE_DIR=$B PROFILE_ID=$PROFILE_ID PROFILE_DIGEST=$PROFILE_DIGEST python3 "$DRV/gen_vc5_plans.py" "$W" "$NEW" "$CAND" "$IMAGE_ID" "$BUILD_ID" "$ATT" "$SEAL_SHA" | cut -c1-120; chmod 600 "$W"/*.json
if [ ! -f "$CAPTURE_RESULT" ]; then
  echo "=== 预演批次 ${SEQ}（seal 批准 + compare）"; rehearse action-plan-vc5-seal-approve-compare.json
  echo "=== 批次 ${SEQ}：seal 批准 + compare"; batch "$SEQ" action-plan-vc5-seal-approve-compare.json; SEQ=$((SEQ+1))
fi
phase_require --mode attempt --attempt-id "$ATT"
python3 -c "import json; d=json.load(open('$CAPTURE_RESULT')); print('candidate result:', {k:(str(d.get(k))[:40]) for k in ('status','package_digest','attempt_id','candidate_id')})"
python3 -c "import json; d=json.load(open('$COMPARE_RESULT')); print('compare:', {k:(str(d.get(k))[:60]) for k in ('status','package_digest','official_package_digest','candidate_package_digest','summary')})"
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" 2>/dev/null | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence')})" || echo "账本状态读取失败（不影响 seal 产物）"
echo "SEAL_DONE ATT=$ATT SEAL_SHA=$SEAL_SHA NEXT_SEQ=$SEQ SEALED=$SEALED"
