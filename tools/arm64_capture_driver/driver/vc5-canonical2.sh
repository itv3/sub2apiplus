#!/bin/bash
# VC-5 步骤 7：production_replacement 的 canonical 交接（指南 §4.5.7）。
#   离线只读预览 canonical-import（无父 run、无 --supervisor-run-dir，deadline 取总计划冻结值）得到 review_sha256
#   → 批次 N：纯 canonical 批次只含 canonical-import（带批准摘要；时间锚从父 run 上下文解析）
#   → 批次 N+1：纯 canonical 批次 seal → compare → accept（带序号 action_id 保证次序；从中间 step 续跑、Candidate Job 零执行）
#   → 校验 vc5-completion.json 与 vc-5-checkpoint.json。
# 用法：bash vc5-canonical2.sh <attempt_id>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
ATT="$1"; EV="$NEWDIR/candidates/$CAND/attempts/$ATT/evidence"
test -f "$NEWDIR/acceptance/$CAND/result.json"
if [ -f "$NEWDIR/control/vc/receipts/$CAND/vc5-completion.json" ]; then echo "CANONICAL2_SKIP: vc5-completion.json 已存在"; echo "CANONICAL2_DONE"; exit 0; fi
ACTIVE=$D/promotions/c0151-formal-rule-correction-20260905t0033z-c0151-v10-c1-production/catalogdata/runtime/profiles/0.151.0/dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json
PATCH=$TOOLS/profile_rule_patches_0_154_0.json
echo "=== 离线预览 A（内部函数，零副作用，无父 run）$(utc_now)"
SHA=$(python3 - "$NEWDIR" "$CAND" "$ATT" "$EV" "$ACTIVE" "$PATCH" <<'PY'
import argparse, sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
NEW, CAND, ATT, EV, ACTIVE, PATCH = sys.argv[1:]
a = argparse.Namespace(campaign_dir=Path(NEW), candidate_id=CAND, attempt_id=ATT, kilo_facts=Path(EV)/"client/raw/kilo-facts.json",
    active_profile=Path(ACTIVE), profile_patch_manifest=Path(PATCH), profile_activation_fact=Path(EV)/"client/raw/profile-activation-fact.json",
    supervisor_run_dir=None, phase="VC-5", retire_version="0.149.1", approve_import_sha256=None)
anchor = cu._canonical_time_anchor(a, Path(NEW)); assert anchor["source"] == "campaign-plan", anchor
d = cu.import_canonical_checkpoint(a)
assert d["status"] == "approval_required", d
assert d["execute_item_ids"][-3:] == ["production-activation", "rollback-verification", "retire-0.149.1"], d["execute_item_ids"]
assert d["scanned_bytes"] == 0 and d["live_request_count"] == 0, d
print(d["review_sha256"])
PY
); echo "review_sha256=$SHA"
echo "=== 离线预览 B（CLI，无父 run；核对与 A 同一摘要）$(utc_now)"
/usr/bin/python3 $TOOLS/codex_upgrade.py canonical-import --campaign-dir "$NEWDIR" --candidate-id "$CAND" --attempt-id "$ATT" \
  --kilo-facts "$EV/client/raw/kilo-facts.json" --active-profile "$ACTIVE" --profile-patch-manifest "$PATCH" \
  --profile-activation-fact "$EV/client/raw/profile-activation-fact.json" --phase VC-5 --retire-version 0.149.1 > "$W/canonical-preview.out" 2> "$W/canonical-preview.err" || true
python3 - "$W/canonical-preview.out" "$SHA" <<'PY'
import json, sys
t = open(sys.argv[1]).read()
try:
    d = json.loads(t)
except Exception:
    print("CLI 预览输出非 JSON：", t[-400:]); sys.exit(1)
print("CLI preview:", {k: d.get(k) for k in ("status", "review_sha256", "approval_projection_excluded", "scanned_bytes", "live_request_count")})
assert d["status"] == "approval_required" and d["review_sha256"] == sys.argv[2], d
print("review_sha256 一致（内部函数 = CLI）")
PY
tail -c 300 "$W/canonical-preview.err"; echo
python3 - "$W" "$NEWDIR" "$CAND" "$ATT" "$EV" "$ACTIVE" "$PATCH" "$SHA" "$D" <<'PY'
import json, sys
W, NEW, CAND, ATT, EV, ACTIVE, PATCH, SHA, D = sys.argv[1:]
TOOLS = f"{D}/tools/official_client_capture"
JOBS = ["candidate-compact-direct","candidate-compact-mitm","candidate-core-direct","candidate-core-mitm","candidate-frozen-aux","candidate-frozen-core","candidate-h1-wire","candidate-images-wire","candidate-trace-test","candidate-ws-handshake-repeat"]
py = ["/usr/bin/python3", f"{TOOLS}/codex_upgrade.py"]; ref = ["--campaign-dir", NEW, "--candidate-id", CAND, "--attempt-id", ATT]
imp = py + ["canonical-import"] + ref + ["--kilo-facts", f"{EV}/client/raw/kilo-facts.json", "--active-profile", ACTIVE, "--profile-patch-manifest", PATCH,
    "--profile-activation-fact", f"{EV}/client/raw/profile-activation-fact.json", "--phase", "VC-5", "--retire-version", "0.149.1", "--approve-import-sha256", SHA]
plan_a = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["canonical-import"], "reuse_item_ids": JOBS,
          "actions": [{"action_id": "canonical-1-import", "operation": "VC-5:canonical-import", "timeout_seconds": 1800, "command": imp, "item_ids": ["canonical-import"]}]}
actions_b = []
for index, step in ((2, "seal"), (3, "compare"), (4, "accept")):
    actions_b.append({"action_id": f"canonical-{index}-{step}", "operation": f"VC-5:canonical-advance-{step}", "timeout_seconds": 1800,
                      "command": py + ["canonical-advance"] + ref + ["--canonical-step", step], "item_ids": [f"canonical-{step}"]})
plan_b = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["canonical-accept", "canonical-compare", "canonical-seal"], "reuse_item_ids": JOBS, "actions": actions_b}
from tools.official_client_capture import codex_upgrade_vc_artifacts as va
for name, plan in (("action-plan-vc5-canonical-a.json", plan_a), ("action-plan-vc5-canonical-b.json", plan_b)):
    validated = va.validate_action_plan(plan)
    binding = va.canonical_batch_binding(validated["actions"], execute_item_ids=validated["execute_item_ids"])
    open(f"{W}/{name}", "w").write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    print("plan ->", name, "binding:", {k: binding[k] for k in ("candidate_id", "attempt_id", "item_ids")})
PY
chmod 600 "$W"/*.json
if [ ! -f "$NEWDIR/canonical/import-receipt.json" ]; then
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：canonical-import（批准，时间锚来自父 run）$(utc_now)"
  bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$SEQ" VC-4 action-plan-vc5-canonical-a.json | grep -v "^$" | cut -c1-400
fi
ls "$NEWDIR/canonical/checkpoints"; python3 -c "
import json; r=json.load(open('$NEWDIR/canonical/import-receipt.json')); print('import-receipt:', r['status'], r['review_sha256'][:16], 'approval_run:', {k:(str(v)[-28:]) for k,v in r['approval_run'].items()})"
SEQ=$(next_seq)
echo "=== 批次 ${SEQ}：canonical seal → compare → accept（从 import 之后的 step 续跑，Candidate Job 零执行）$(utc_now)"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$SEQ" VC-4 action-plan-vc5-canonical-b.json | grep -v "^$" | cut -c1-400
ls "$NEWDIR/canonical/checkpoints"; ls "$NEWDIR/control/vc/" | tr "\n" " "; echo
test -f "$NEWDIR/control/vc/vc-5-checkpoint.json" && python3 -c "
import json; c=json.load(open('$NEWDIR/control/vc/vc-5-checkpoint.json')); print('VC-5 checkpoint:', {k:c.get(k) for k in ('phase','status','completed_at_utc')})
r=json.load(open('$NEWDIR/control/vc/receipts/$CAND/vc5-completion.json')); print('vc5-completion:', {k:(str(r.get(k))[:80]) for k in ('status','receipt_digest')})
cp=json.load(open(sorted(__import__('glob').glob('$NEWDIR/canonical/checkpoints/*.json'))[-1])); print('canonical latest:', cp['phase'], 'execute:', cp['plan']['execute_item_ids'], 'metrics:', cp['metrics'])"
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('账本:', {k:d.get(k) for k in ('status','active_phase','head_sequence','next_action')})" | cut -c1-300
echo "CANONICAL2_DONE"
