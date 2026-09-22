#!/bin/bash
# 管理 token → prepare-profile（Active + EP-019 补丁）→ 五清单 → VC-2 三批（草案／预览／批准）→ VC-3 stage-profile。
# 用法：ARM64_VC_ENV=… bash vc23.sh
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
echo "=== 管理 token"; ST=$D/state/$UP; mkdir -p "$ST"; chmod 700 "$ST"
( cd "$COMPOSE_DIR" && set -a && . ./.env && set +a; DATABASE_HOST="$(docker inspect sub2apiplus-postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")" DATABASE_PORT=5432 DATABASE_USER="$POSTGRES_USER" DATABASE_PASSWORD="$POSTGRES_PASSWORD" DATABASE_DBNAME="$POSTGRES_DB" DATABASE_SSLMODE=disable JWT_SECRET="$JWT_SECRET" JWT_EXPIRE_HOUR="${JWT_EXPIRE_HOUR:-24}" timeout 30 "$D/private-tools/arm64-20260825T130707Z/jwtgen" -email "$ADMIN_EMAIL" 2>/dev/null | sed -n "s/^JWT=//p" | head -1 ) > "$ST/admin-token.tmp"
test -s "$ST/admin-token.tmp"; printf "%s" "$(cat "$ST/admin-token.tmp")" > "$ST/admin-token"; rm -f "$ST/admin-token.tmp"; chmod 400 "$ST/admin-token"
python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split(\".\")[1]; p+=\"=\"*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print(\"token exp 剩余分钟:\", (d[\"exp\"]-int(time.time()))//60)" "$ST/admin-token"
echo "=== 五清单与计划"; mkdir -p "$W"; chmod 700 "$W"
cp "$TOOLS/codex_upgrade_rules_0_154_0.json" "$W/target-rules.json"
cp "$INPUT_RULE_MIGRATION" "$W/rule-migration.json"
cp "$INPUT_TARGET_SNAPSHOT" "$W/target-snapshot-input.json"; chmod 600 "$W/target-snapshot-input.json"
python3 - "$W/action-plan-vc2-prepare-profile.json" "$NEW" "$W" "$PROFILE_ID" "$D" <<"PY"
import json, sys
out, new, W, pid, D = sys.argv[1:]
json.dump({"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["prepare-profile"], "reuse_item_ids": [], "actions": [{"action_id": "prepare-profile", "operation": "VC-2:prepare-profile", "timeout_seconds": 1800, "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "prepare-profile", "--campaign-dir", f"{D}/evidence/campaigns/{new}", "--snapshot", f"{W}/target-snapshot-input.json", "--profile-id", pid, "--output", f"{W}/profile.json"], "item_ids": ["prepare-profile"]}]}, open(out, "w"), ensure_ascii=False, indent=2)
print("VC-2 prepare-profile 计划 ->", out)
PY
chmod 600 "$W"/*.json
echo "=== 批次 2：prepare-profile（Active 0.151 + EP-019 补丁 → 目标 Snapshot）"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 2 VC-1 action-plan-vc2-prepare-profile.json | grep -v "^$"
test -f "$W/profile.json"
python3 - "$W/profile.json" <<"PY"
import json,sys
p=sys.argv[1]; d=json.load(open(p))
assert d["status"]=="draft" and d["codex_version"]=="0.154.0", d.get("status")
d["status"]="approved"
open(p,"w").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
print("profile.json ->", d["profile_id"], d["profile_digest"][:16], "wham_usage:", [h["Name"] for h in [e for e in d["profile_payload"]["Endpoints"] if e["ID"]=="wham_usage"][0]["Headers"]])
PY
python3 - "$TOOLS/codex_upgrade_scenarios_0_154_0.json" "$W/profile.json" "$W/scenarios.json" <<"PY"
import json, sys
scenario = json.load(open(sys.argv[1])); profile = json.load(open(sys.argv[2]))
scenario["profile_id"] = profile["profile_id"]
open(sys.argv[3], "w").write(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n")
print("scenarios.json profile_id ->", scenario["profile_id"])
PY
cp "$TOOLS/candidate_rule_expectations_0_154_0.json" "$W/assertion-profile.json"
chmod 600 "$W"/*.json
JOINT=$(python3 - "$W" <<"PY"
import sys, json
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
W = Path(sys.argv[1])
files = {"target_rule_manifest": "target-rules.json", "migration_manifest": "rule-migration.json", "scenario_manifest": "scenarios.json", "profile_manifest": "profile.json", "assertion_profile_manifest": "assertion-profile.json"}
print(cu._fingerprint({k: cu._normalized_json_sha256(json.loads((W / v).read_text())) for k, v in files.items()}))
PY
)
echo "JOINT=$JOINT"
python3 "$DRV/gen_vc2_plans.py" "$W" "$NEW" "$IN" "$JOINT" | cut -c1-120
CATALOG="$D/control/c0154-vc3-candidate-catalog-$ROUND-$STAMP"
python3 - "$W/action-plan-vc3-stage-profile.json" "$NEW" "$CATALOG" "$D" <<"PY"
import json, sys
out, new, catalog, D = sys.argv[1:]
json.dump({"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["stage-profile"], "reuse_item_ids": [], "actions": [{"action_id": "stage-profile", "operation": "VC-3:stage-profile", "timeout_seconds": 1800, "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "stage-profile", "--campaign-dir", f"{D}/evidence/campaigns/{new}", "--output", catalog], "item_ids": ["stage-profile"]}]}, open(out, "w"), ensure_ascii=False, indent=2)
print("VC-3 计划 ->", out)
PY
chmod 600 "$W"/*.json
echo "=== 批次 3：classify 草案"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 3 VC-1 action-plan-vc2-draft.json | grep -v "^$"
echo "=== 批次 4：批准预览"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 4 VC-1 action-plan-vc2-preview.json | grep -v "^$"
echo "=== 批次 5：批准"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 5 VC-1 action-plan-vc2-approve.json | grep -v "^$"
test -f "$NEWDIR/control/vc/vc-2-checkpoint.json"
echo "=== 批次 6：VC-3 stage-profile"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-3 6 VC-2 action-plan-vc3-stage-profile.json | grep -v "^$"
test -f "$NEWDIR/control/vc/vc-3-checkpoint.json"
python3 -c "
import json; r=json.load(open(\"$CATALOG/catalog-stage-receipt.json\")); print({k:(r.get(k)[:16] if isinstance(r.get(k),str) else r.get(k)) for k in (\"campaign_id\",\"classification_sha256\",\"target_profile_digest\",\"candidate_release_digest\",\"inventory_sha256\",\"post_promotion_gate_requirements_sha256\")})"
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(\"账本:\", {k:d.get(k) for k in (\"status\",\"active_phase\",\"head_sequence\")})"
echo "CATALOG=$CATALOG"; echo "VC23_DONE"
