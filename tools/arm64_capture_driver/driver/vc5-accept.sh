#!/bin/bash
# VC-5 compare 之后到 accept：断言配置 → 批次 assert-rules（改造 5：builder 必须由父监督器派发并冻结 b0/evaluator 摘要）
#   → 目标平台门禁（gate_before/make test/gate_after；失败自动归档后重跑，不进入 seal 链）→ 门禁 facts/收据 → 批次 accept。
# 用法：bash vc5-accept.sh <attempt_id>（要求本机门禁六件套已注入 $G/local）
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
ATT="$1"
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
echo "=== 断言配置"; mkdir -p "$AS" "$NEWDIR/assertions/$CAND"; chmod 700 "$AS" "$NEWDIR/assertions" "$NEWDIR/assertions/$CAND"
[ -f "$AS/config.json" ] || python3 "$DRV/build_assertion_config.py" "$NEWDIR" "$CAND" "$OFFICIAL_CAMPAIGN" "$AS/config.json" | tail -n 3 | cut -c1-200
closeout_control "$AS"
test -f "$W/action-plan-vc5-assert.json"
if [ ! -f "$NEWDIR/assertions/$CAND/evaluation-run.json" ]; then
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：assert-rules（builder 由父监督器派发，b0，reuse_authority=none）"; batch "$SEQ" action-plan-vc5-assert.json
fi
python3 -c "
import json
from collections import Counter
d=json.load(open('$NEWDIR/assertions/$CAND/results.json')); rs=d.get('results') or d.get('rules') or []
print('assertions:', {k:(str(v)[:60]) for k,v in d.items() if not isinstance(v,(list,dict))}, '| rule count:', len(rs), '| status 计数:', Counter((r.get('status') or r.get('result')) for r in rs) if isinstance(rs,list) else '')
e=json.load(open('$NEWDIR/assertions/$CAND/evaluation-run.json')); rows=e.get('rules') or []
print('evaluation-run:', {k:(str(v)[:60]) for k,v in e.items() if not isinstance(v,(list,dict))}, '| rules:', len(rows), '| failed:', [r['rule'] for r in rows if r.get('status')!='pass'])"
echo "=== 目标平台门禁"; mkdir -p "$G"; chmod 700 "$G"; test -d "$G/local" || { echo "缺少 $G/local（本机门禁目录）"; exit 1; }
if [ -f "$G/logs/target-platform.gate.json" ] && [ "$(python3 -c "import json; print(json.load(open('$G/logs/target-platform.gate.json'))['exit_code'])")" != 0 ]; then
  # 失败的目标平台门禁产物整体归档（保留审计），然后重跑；只重跑门禁，不回到 seal 链
  SUP="$G/superseded/target-platform-$(date -u +%Y%m%dt%H%M%Sz)"; mkdir -p "$SUP"; chmod 700 "$G/superseded" "$SUP"
  mv "$G"/logs/target-platform.* "$SUP/"; mv "$G"/environment/"$ATT"-* "$SUP/" 2>/dev/null || true
  echo "上一次目标平台门禁 exit_code≠0，已归档到 ${SUP}，重跑"
fi
[ -f "$G/logs/target-platform.gate.json" ] || bash "$DRV/vc5-gate-target.sh" "$ATT" "$G" "$B/test-tree" 2>&1 | tail -n 4 | cut -c1-200
python3 -c "import json; d=json.load(open('$G/logs/target-platform.gate.json')); print('target-platform:', d.get('exit_code'), d.get('started_at_utc'), d.get('completed_at_utc'))"
test "$(python3 -c "import json; print(json.load(open('$G/logs/target-platform.gate.json'))['exit_code'])")" = 0
echo "=== 门禁 facts 与收据"; [ -f "$G/candidate-gates.facts.json" ] || python3 "$DRV/build_gate_facts.py" "$G" "$ATT" "$NEWDIR/campaign.json" "$NEWDIR/candidates/$CAND/result.json" "$NEWDIR/candidates/$CAND/build-receipt.json" "$G/local" "$G/logs" | tail -n 2 | cut -c1-200
[ -f "$G/candidate-gates.receipt.json" ] || python3 -m tools.official_client_capture.codex_upgrade_gate_receipt finalize --evidence-root "$G" --facts candidate-gates.facts.json --output candidate-gates.receipt.json | cut -c1-200
python3 -m tools.official_client_capture.codex_upgrade_gate_receipt replay --evidence-root "$G" --receipt candidate-gates.receipt.json | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('replay:', d.get('status'), d.get('receipt_digest') or d.get('receipt_sha256'))"
closeout_control "$G"; closeout_control "$AS"
if [ ! -f "$NEWDIR/acceptance/$CAND/result.json" ]; then
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：accept"; batch "$SEQ" action-plan-vc5-accept.json
fi
python3 -c "import json; d=json.load(open('$NEWDIR/acceptance/$CAND/result.json')); print('acceptance:', {k:(str(d.get(k))[:80]) for k in ('status','accepted','decision','summary','package_digest') if k in d})"
ls "$NEWDIR/control/vc/" | tr "\n" " "; echo
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence')})"
echo "ACCEPT_DONE"
