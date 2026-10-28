#!/bin/bash
# 管理 token → prepare-profile（本轮批准输入）→ 五清单 → VC-2 三批（草案／预览／批准）→ VC-3 stage-profile。
# 用法：ARM64_VC_ENV=… SEQ0=<下一个可用批次序号> bash vc23-seq.sh
# （由驱动 vc23.sh 机械改写：批次序号 2～6 写死 → SEQ0 起的五个连续序号；其余字节不变。生成器 gen_vc2_seq_scripts.py）
set -Eeuo pipefail; umask 077
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
PIPE_HOME=${PIPE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
source "${PIPE_DRIVER_DIR}/lib.sh"; cd "$D"
: "${SEQ0:?必须设置 SEQ0（本 Campaign 下一个可用的批次序号；流水线用 control/vc/batches 下已提交的最大序号 +1 算出）}"
# 管理 token 补丁：管理 token 签发（见 gen_vc2_seq_scripts.py 的说明）。其余逻辑与 lib.sh 的 ensure_admin_token force 相同。
ensure_admin_token_dbadmin() {
  local st="$D/state/$UP"
  echo "管理 token：force 重签（先用 .env 的 ADMIN_EMAIL，对不上 DB 管理员时回落到 DB 里唯一的 active admin）"
  mkdir -p "$st"; chmod 700 "$st"
  if [ -e "$st/admin-token" ]; then mv "$st/admin-token" "$st/admin-token.superseded-$(date -u +%Y%m%dt%H%M%Sz)"; fi
  ( cd "$COMPOSE_DIR" && set -a && . ./.env && set +a
    DB_IP="$(docker inspect sub2apiplus-postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")"
    sign_with() { DATABASE_HOST="$DB_IP" DATABASE_PORT=5432 DATABASE_USER="$POSTGRES_USER" DATABASE_PASSWORD="$POSTGRES_PASSWORD" DATABASE_DBNAME="$POSTGRES_DB" DATABASE_SSLMODE=disable JWT_SECRET="$JWT_SECRET" JWT_EXPIRE_HOUR="${JWT_EXPIRE_HOUR:-24}" timeout 30 "$JWTGEN_BIN" -email "$1" 2>/dev/null | sed -n "s/^JWT=//p" | head -1; }
    # 脚本带 pipefail／set -e：jwtgen 非零退出时必须吞掉，才能走到下面的回落分支
    tok="$(sign_with "$ADMIN_EMAIL" || true)"
    if [ -z "$tok" ]; then
      mapfile -t emails < <(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select email from users where role='admin' and status='active' order by id")
      [ "${#emails[@]}" = 1 ] || { echo "管理 token 回落失败：DB 里 active admin 不是恰好一个（${#emails[@]} 个）" >&2; exit 3; }
      echo "NOTE: .env 的 ADMIN_EMAIL 在 DB 里不存在，回落到 DB 的唯一 active admin（邮箱长度 ${#emails[0]}）" >&2
      tok="$(sign_with "${emails[0]}" || true)"
    fi
    printf "%s" "$tok"
  ) > "$st/admin-token.tmp"
  test -s "$st/admin-token.tmp" || { echo "管理 token 签发失败（jwtgen 无输出）" >&2; return 3; }
  printf "%s" "$(cat "$st/admin-token.tmp")" > "$st/admin-token"; rm -f "$st/admin-token.tmp"; chmod 400 "$st/admin-token"
  python3 -c "
import base64,json,sys,time
t=open(sys.argv[1]).read().strip(); p=t.split('.')[1]; p+='='*(-len(p)%4); d=json.loads(base64.urlsafe_b64decode(p)); print('token exp 剩余分钟:', (int(d['exp'])-int(time.time()))//60)" "$st/admin-token"
}
# 派发脚本按客户端表（B5b-01）：Claude 的 VC-2／VC-3 走 vc23-claude.sh，这里只给 Codex。
[ "$CT_VC23_DISPATCH_SCRIPT" = vc23.sh ] || { echo "本轮客户端（${CT_CLIENT}）的 VC-2／VC-3 派发脚本是 ${CT_VC23_DISPATCH_SCRIPT}，拒绝"; exit 3; }
echo "=== 管理 token"; ensure_admin_token_dbadmin
echo "=== 五清单与计划"; mkdir -p "$W"; chmod 700 "$W"
cp "$RULES_JSON" "$W/target-rules.json"
cp "$INPUT_RULE_MIGRATION" "$W/rule-migration.json"
cp "$INPUT_TARGET_SNAPSHOT" "$W/target-snapshot-input.json"; chmod 600 "$W/target-snapshot-input.json"
python3 - "$W/action-plan-vc2-prepare-profile.json" "$NEW" "$W" "$PROFILE_ID" "$D" <<"PY"
import json, sys
out, new, W, pid, D = sys.argv[1:]
json.dump({"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["prepare-profile"], "reuse_item_ids": [], "actions": [{"action_id": "prepare-profile", "operation": "VC-2:prepare-profile", "timeout_seconds": 1800, "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "prepare-profile", "--campaign-dir", f"{D}/evidence/campaigns/{new}", "--snapshot", f"{W}/target-snapshot-input.json", "--profile-id", pid, "--output", f"{W}/profile.json"], "item_ids": ["prepare-profile"]}]}, open(out, "w"), ensure_ascii=False, indent=2)
print("VC-2 prepare-profile 计划 ->", out)
PY
chmod 600 "$W"/*.json
echo "=== 批次 $SEQ0：prepare-profile（本轮批准输入 → 目标 Snapshot）"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$SEQ0" VC-1 action-plan-vc2-prepare-profile.json | grep -v "^$"
test -f "$W/profile.json"
python3 - "$W/profile.json" <<"PY"
import json,sys,os
p=sys.argv[1]; d=json.load(open(p))
assert d["status"]=="draft" and d["codex_version"]==os.environ["TARGET_VERSION"], d.get("status")
d["status"]="approved"
open(p,"w").write(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
print("profile.json ->", d["profile_id"], d["profile_digest"][:16], "wham_usage:", [h["Name"] for h in [e for e in d["profile_payload"]["Endpoints"] if e["ID"]=="wham_usage"][0]["Headers"]])
PY
python3 - "$SCENARIOS_JSON" "$W/profile.json" "$W/scenarios.json" <<"PY"
import json, sys
scenario = json.load(open(sys.argv[1])); profile = json.load(open(sys.argv[2]))
scenario["profile_id"] = profile["profile_id"]
open(sys.argv[3], "w").write(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n")
print("scenarios.json profile_id ->", scenario["profile_id"])
PY
cp "$EXPECTATIONS_JSON" "$W/assertion-profile.json"
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
CATALOG="$D/control/${CAMPAIGN_PREFIX}-vc3-candidate-catalog-$ROUND-$STAMP"
python3 - "$W/action-plan-vc3-stage-profile.json" "$NEW" "$CATALOG" "$D" <<"PY"
import json, sys
out, new, catalog, D = sys.argv[1:]
json.dump({"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": ["stage-profile"], "reuse_item_ids": [], "actions": [{"action_id": "stage-profile", "operation": "VC-3:stage-profile", "timeout_seconds": 1800, "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "stage-profile", "--campaign-dir", f"{D}/evidence/campaigns/{new}", "--output", catalog], "item_ids": ["stage-profile"]}]}, open(out, "w"), ensure_ascii=False, indent=2)
print("VC-3 计划 ->", out)
PY
chmod 600 "$W"/*.json
echo "=== 批次 $((SEQ0+1))：classify 草案"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 "$((SEQ0+1))" VC-1 action-plan-vc2-draft.json | grep -v "^$"
SEQ0="$SEQ0" bash ${PIPE_HOME}/vc2/vc2-approve-and-stage-seq.sh "$JOINT"
python3 -c "
import json; r=json.load(open(\"$CATALOG/catalog-stage-receipt.json\")); print({k:(r.get(k)[:16] if isinstance(r.get(k),str) else r.get(k)) for k in (\"campaign_id\",\"classification_sha256\",\"target_profile_digest\",\"candidate_release_digest\",\"inventory_sha256\",\"post_promotion_gate_requirements_sha256\")})"
python3 -m tools.official_client_capture.codex_upgrade_timing_ledger status --ledger-dir "$L" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(\"账本:\", {k:d.get(k) for k in (\"status\",\"active_phase\",\"head_sequence\")})"
echo "CATALOG=$CATALOG"; echo "VC23_DONE"
