#!/bin/bash
# Claude 的 VC-5 accept（Claude 升级 B5b-01b，客户端表 vc5_accept_script）：assert 批次（真实 Claude builder，配置、输出、结果
#   目录与评估基线取当前基线比较收据登记的那一份）→ 目标平台门禁（与 Codex 同一套：gate_before/make test/gate_after，失败自动归档
#   后重跑）→ 门禁 facts（subject 取 claude_vc5.py gate-subject）与收据 → accept 批次（Store 验收链、accept 阶段结果、验收证据封印与
#   VC-5 checkpoint；两种用途都在这里完成 VC-5，没有 canonical 交接）。外部门禁收据有失败门禁时 accept 只把这一轮定稿进 Store、
#   批次失败；下一步是 capture-candidate seal --revalidate（门禁重试链）。
# 用法：bash vc5-claude-accept.sh <attempt_id>（要求本机门禁六件套已注入 $GATE_ROOT/local；当前部署的后台验证已通过）
set -Eeuo pipefail; umask 077
PHASE_CONTEXT_ARGS=(--mode attempt --attempt-id "$1")
CLIENT_STEP_REQUIRED=(CT_VC5_ACCEPT_SCRIPT vc5-claude-accept.sh)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
vc5_require_admission
G=$GATE_ROOT
if ! python3 -B "$DRV/background_validation.py" require-passed --runroot "$RUNROOT" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}; then
  echo "VC-5 验收前要求当前部署的后台验证已通过：等它跑完，或修好、部署后重新起（background-validate.sh start）"; exit 3
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
closeout_control() { local root="$1"; find "$root" ! -user root -exec chown root:root {} +; find "$root" -type d ! -perm 700 -exec chmod 700 {} +; find "$root" -type f ! -perm 600 -exec chmod 600 {} +; }
mkdir -p "$W"; chmod 700 "$W"
test "$COMPARE_READY" = 1 || { echo "当前基线还没有比较收据：先走 seal 链（vc5-claude-seal.sh）"; exit 3; }
if [ "$ASSERTIONS_READY" != 1 ]; then
  test -n "$ASSERTIONS_WRITE" || { echo "断言来源只读，不能重写"; exit 3; }
  python3 "$DRV/claude_vc5.py" assert-plan --output "$W/action-plan-vc5-assert.json"
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：assert-rules（真实 Claude builder，b$EVALUATION_BASELINE，reuse_authority=none）"; batch "$SEQ" action-plan-vc5-assert.json
fi
phase_require --mode attempt --attempt-id "$ATT"
python3 -c "
import json
d=json.load(open('$ASSERTIONS_RESULT')); rs=d.get('rules') or []
print('assertions:', {k:(str(v)[:60]) for k,v in d.items() if not isinstance(v,(list,dict))}, '| rule count:', len(rs))
e=json.load(open('$EVALUATION_RUN')); rows=e.get('rules') or []
print('evaluation-run:', {k:(str(v)[:60]) for k,v in e.items() if not isinstance(v,(list,dict))}, '| rules:', len(rows), '| failed:', [r['rule'] for r in rows if r.get('status')!='pass'])"
echo "=== 目标平台门禁"; mkdir -p "$G"; chmod 700 "$G"; test -d "$G/local" || { echo "缺少 $G/local（本机门禁目录）"; exit 1; }
TARGET_CHECK_ARGS=(--gate-root "$G" --data-root "$D")
if [ -n "${VC5_TARGET_REQUEST:-}" ]; then TARGET_CHECK_ARGS+=(--request "$VC5_TARGET_REQUEST"); fi
if [ "$ACCEPT_READY" != 1 ] && { [ -f "$G/logs/target-platform.gate.json" ] || [ -d "$G/target-units" ]; }; then
  if ! python3 -B "$DRV/target_platform_gate.py" check-cached "${TARGET_CHECK_ARGS[@]}"; then
    python3 -B "$DRV/target_platform_gate.py" archive --gate-root "$G" --attempt "$ATT"
  fi
fi
[ -f "$G/logs/target-platform.gate.json" ] || bash "$DRV/vc5-gate-target.sh" "$ATT" "$G" "$B/test-tree" 2>&1 | tail -n 4 | cut -c1-200
python3 -c "import json; d=json.load(open('$G/logs/target-platform.gate.json')); print('target-platform:', d.get('exit_code'), d.get('host'), d.get('architecture'), d.get('started_at_utc'), d.get('completed_at_utc'))"
test "$(python3 -c "import json; print(json.load(open('$G/logs/target-platform.gate.json'))['exit_code'])")" = 0
echo "=== 门禁 facts 与收据（subject 取 claude_upgrade_compare.candidate_gate_identity）"
if [ ! -f "$G/candidate-gates.facts.json" ]; then
  python3 "$DRV/claude_vc5.py" gate-subject --output "$G/candidate-gates.subject.json"
  python3 "$DRV/build_gate_facts.py" "$G" "$ATT" "$NEWDIR/campaign.json" "$CAPTURE_RESULT" "$BUILD_RECEIPT" "$G/local" "$G/logs" \
    --subject "$G/candidate-gates.subject.json" | tail -n 2 | cut -c1-200
fi
[ -f "$G/candidate-gates.receipt.json" ] || python3 -m "$CT_GATE_RECEIPT_MODULE" finalize --evidence-root "$G" --facts candidate-gates.facts.json --output candidate-gates.receipt.json | cut -c1-200
python3 -m "$CT_GATE_RECEIPT_MODULE" replay --evidence-root "$G" --receipt candidate-gates.receipt.json | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('replay:', d.get('status'), d.get('receipt_digest') or d.get('receipt_sha256'))"
closeout_control "$G"
if [ "$ACCEPT_READY" != 1 ]; then
  python3 -B "$DRV/target_platform_gate.py" check-cached "${TARGET_CHECK_ARGS[@]}"
  python3 "$DRV/claude_vc5.py" accept-plan --output "$W/action-plan-vc5-accept.json"
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：accept"; batch "$SEQ" action-plan-vc5-accept.json
fi
phase_require --mode attempt --attempt-id "$ATT" --require-completion VC-5
python3 -c "import json; d=json.load(open('$ACCEPT_RESULT')); print('acceptance:', {k:(str(d.get(k))[:80]) for k in ('status','accepted','campaign_purpose','production_state','evaluation_baseline')})"
echo "VC-5 checkpoint: $VC5_CHECKPOINT"
python3 -m "$CT_TIMING_LEDGER_MODULE" status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence')})"
echo "ACCEPT_DONE"
