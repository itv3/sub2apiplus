#!/bin/bash
# VC-5 compare 之后到 accept：断言配置 → 批次 assert-rules（改造 5：builder 必须由父监督器派发并冻结当前基线/evaluator 摘要）
#   → 目标平台门禁（gate_before/make test/gate_after；失败自动归档后重跑，不进入 seal 链）→ 门禁 facts/收据 → 批次 accept。
# 用法：bash vc5-accept.sh <attempt_id>（要求本机门禁六件套已注入 $G/local；当前部署的后台验证已通过）
set -Eeuo pipefail; umask 077
PHASE_CONTEXT_ARGS=(--mode attempt --attempt-id "$1")
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
vc5_require_admission
G=$GATE_ROOT; AS=$ASSERTION_CONFIG_DIR
# 修复轮规则（E4-01，指南「修好接着跑」）：验收前当前部署的后台验证必须已有通过的结论（全集通过）。
if ! python3 -B "$DRV/background_validation.py" require-passed --runroot "$RUNROOT" --data-root "$D"; then
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
# 控制目录（非 attempt 证据根、不在 EvidenceManifest 内）的权限收口：只改不合规条目，幂等
closeout_control() { local root="$1"; find "$root" ! -user root -exec chown root:root {} +; find "$root" -type d ! -perm 700 -exec chmod 700 {} +; find "$root" -type f ! -perm 600 -exec chmod 600 {} +; }
echo "=== 当前基线 b$EVALUATION_BASELINE 断言配置"
python3 -B "$DRV/gen_vc5_plans.py" "$W" "$NEW" "$CAND" "$IMAGE_ID" "$BUILD_ID" "$ATT"
if [ "$ASSERTIONS_READY" != 1 ]; then
  test -n "$ASSERTIONS_WRITE" || { echo "断言来源只读，不能重写"; exit 3; }
  mkdir -p "$AS" "$ASSERTIONS_WRITE"; chmod 700 "$AS" "$ASSERTIONS_WRITE"
[ -f "$AS/config.json" ] || python3 "$DRV/build_assertion_config.py" "$NEWDIR" "$CAND" "$OFFICIAL_CAMPAIGN" "$AS/config.json" | tail -n 3 | cut -c1-200
closeout_control "$AS"
test -f "$W/action-plan-vc5-assert.json"

  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：assert-rules（builder 由父监督器派发，b$EVALUATION_BASELINE，reuse_authority=none）"; batch "$SEQ" action-plan-vc5-assert.json
fi
phase_require --mode attempt --attempt-id "$ATT"
python3 -c "
import json
from collections import Counter
d=json.load(open('$ASSERTIONS_RESULT')); rs=d.get('results') or d.get('rules') or []
print('assertions:', {k:(str(v)[:60]) for k,v in d.items() if not isinstance(v,(list,dict))}, '| rule count:', len(rs), '| status 计数:', Counter((r.get('status') or r.get('result')) for r in rs) if isinstance(rs,list) else '')
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
python3 -c "import json; d=json.load(open('$G/logs/target-platform.gate.json')); print('target-platform:', d.get('exit_code'), d.get('started_at_utc'), d.get('completed_at_utc'))"
test "$(python3 -c "import json; print(json.load(open('$G/logs/target-platform.gate.json'))['exit_code'])")" = 0
echo "=== 门禁 facts 与收据"; [ -f "$G/candidate-gates.facts.json" ] || python3 "$DRV/build_gate_facts.py" "$G" "$ATT" "$NEWDIR/campaign.json" "$CAPTURE_RESULT" "$BUILD_RECEIPT" "$G/local" "$G/logs" | tail -n 2 | cut -c1-200
[ -f "$G/candidate-gates.receipt.json" ] || python3 -m tools.official_client_capture.codex_upgrade_gate_receipt finalize --evidence-root "$G" --facts candidate-gates.facts.json --output candidate-gates.receipt.json | cut -c1-200
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt replay --evidence-root "$G" --receipt candidate-gates.receipt.json | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('replay:', d.get('status'), d.get('receipt_digest') or d.get('receipt_sha256'))"
closeout_control "$G"; closeout_control "$AS"
if [ "$ACCEPT_READY" != 1 ]; then
  # 承接判断不能被 facts／收据文件已存在绕过；派发 accept 之前再次核对。
  python3 -B "$DRV/target_platform_gate.py" check-cached "${TARGET_CHECK_ARGS[@]}"
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：accept"; batch "$SEQ" action-plan-vc5-accept.json
fi
phase_require --mode attempt --attempt-id "$ATT"
python3 -c "import json; d=json.load(open('$ACCEPT_RESULT')); print('acceptance:', {k:(str(d.get(k))[:80]) for k in ('status','accepted','decision','summary','package_digest') if k in d})"
ls "$NEWDIR/control/vc/" | tr "\n" " "; echo
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence')})"
echo "ACCEPT_DONE"
