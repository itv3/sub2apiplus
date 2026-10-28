#!/bin/bash
# VC-6 canonical：批次登记 production-activation → rollback-verification（均以生产激活收据为步骤收据）。
set -Eeuo pipefail; umask 077
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV（本轮参数文件）}"
export ARM64_VC_ENV
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# 不写死 VC_STATE_DIR（曾经写死的值恰好等于 vc-batch.sh 的缺省 control/<Campaign>-supervisor，不设置效果相同；
# 流水线或调度者若导出了 VC_STATE_DIR 则原样继承）
source ${PIPE_DRIVER_DIR}/lib.sh; cd "$D"
V6=$RUNROOT/vc6; R=$(cat "$V6/vc6-root.txt")
ATT=$(cat "$V6/vc5-att.txt")
test -s "$R/activation-receipt.json"
python3 - "$D" "$NEW" "$CAND" "$ATT" "$R" "$W/action-plan-vc6-activation.json" <<'PY'
import json, sys
D, CID, CAND, ATT, R, OUT = sys.argv[1:]
import glob
_cp = json.load(open(sorted(glob.glob(f"{D}/evidence/campaigns/{CID}/canonical/checkpoints/*.json"))[-1]))
reuse = sorted(i["item_id"] for i in _cp["items"] if i["item_id"].startswith("candidate-"))
def act(seq, step):
    return {"action_id": f"canonical-{seq}-{step}", "operation": f"VC-6:canonical-advance-{step}", "timeout_seconds": 1800,
            "command": ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "canonical-advance",
                        "--campaign-dir", f"{D}/evidence/campaigns/{CID}", "--candidate-id", CAND, "--attempt-id", ATT,
                        "--canonical-step", step, "--step-receipt", f"{R}/activation-receipt.json"], "item_ids": [step]}
plan = {"schema_version": "codex-upgrade-vc-action-plan/v1",
        "execute_item_ids": ["production-activation", "rollback-verification"], "reuse_item_ids": reuse,
        "actions": [act(5, "production-activation"), act(6, "rollback-verification")]}
open(OUT, "w").write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print("动作计划：", [a["action_id"] for a in plan["actions"]])
PY
chmod 600 "$W/action-plan-vc6-activation.json"
SEQ=$(next_seq); echo "=== 批次 ${SEQ}：VC-6 production-activation → rollback-verification $(utc_now)"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-6 "$SEQ" VC-5 action-plan-vc6-activation.json
python3 -c "
import json, glob, sys
p = sorted(glob.glob(sys.argv[1] + '/canonical/checkpoints/*.json'))[-1]
d = json.load(open(p)); print('latest checkpoint:', p.split('/')[-1], d['phase'], 'execute 剩余:', d['plan']['execute_item_ids'])" "$NEWDIR"
echo "VC6_ACTIVATION_DONE $(utc_now)"
