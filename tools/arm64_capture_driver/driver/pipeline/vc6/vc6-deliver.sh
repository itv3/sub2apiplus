#!/bin/bash
# VC-6 候选交付登记：
#   bash vc6-deliver.sh 1 → 首次登记（不带归档／清理收据，只证明 production_active_restored，不生成 VC-6 checkpoint）
#   bash vc6-deliver.sh 2 → 二次登记（带 private_archive 与 cleanup_decision 统一收据，生成 vc6-completion 与 VC-6 checkpoint）
set -Eeuo pipefail; umask 077
MODE="${1:?1|2}"
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV（本轮参数文件）}"
export ARM64_VC_ENV
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
# 不写死 VC_STATE_DIR，理由同 vc6-canonical-activation.sh
source ${PIPE_DRIVER_DIR}/lib.sh; cd "$D"
ATT=$(cat "$RUNROOT/vc6/vc5-att.txt")
python3 - "$D" "$NEW" "$CAND" "$ATT" "$MODE" "$W/action-plan-vc6-deliver-$MODE.json" <<'PY'
import json, sys
D, CID, CAND, ATT, MODE, OUT = sys.argv[1:]
N = f"{D}/evidence/campaigns/{CID}"
import glob
_cp = json.load(open(sorted(glob.glob(f"{D}/evidence/campaigns/{CID}/canonical/checkpoints/*.json"))[-1]))
reuse = sorted(i["item_id"] for i in _cp["items"] if i["item_id"].startswith("candidate-"))
cmd = ["/usr/bin/python3", f"{D}/tools/official_client_capture/codex_upgrade.py", "deliver-candidate",
       "--campaign-dir", N, "--candidate-id", CAND, "--attempt-id", ATT,
       "--build-receipt", f"{N}/candidates/{CAND}/build-receipt.json"]
if MODE == "1":
    item, action_id, operation = "vc6-delivery-archive-pending", "vc6-1-deliver-precheck", "VC-6:deliver-candidate-archive-pending"
else:
    cmd += ["--private-archive-receipt", f"{N}/control/vc/receipts/{CAND}/private-archive.json",
            "--cleanup-decision-receipt", f"{N}/control/vc/receipts/{CAND}/cleanup-decision.json"]
    item, action_id, operation = "vc6-delivery", "vc6-2-deliver-final", "VC-6:deliver-candidate"
plan = {"schema_version": "codex-upgrade-vc-action-plan/v1", "execute_item_ids": [item], "reuse_item_ids": reuse,
        "actions": [{"action_id": action_id, "operation": operation, "timeout_seconds": 1800, "command": cmd, "item_ids": [item]}]}
open(OUT, "w").write(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print("动作计划：", action_id, item)
PY
chmod 600 "$W/action-plan-vc6-deliver-$MODE.json"
SEQ=$(next_seq); echo "=== 批次 ${SEQ}：VC-6 deliver-candidate（第 $MODE 次）$(utc_now)"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-6 "$SEQ" VC-5 "action-plan-vc6-deliver-$MODE.json"
python3 - "$NEWDIR" "$CAND" <<'PY'
import json, sys
from pathlib import Path
N, CAND = Path(sys.argv[1]), sys.argv[2]
for p in [N / "delivery" / CAND / "receipt.json", N / "control/vc/receipts" / CAND / "vc6-completion.json", N / "control/vc/vc-6-checkpoint.json"]:
    if p.exists():
        d = json.load(open(p))
        print(p.name, "->", json.dumps({k: d.get(k) for k in ("status", "release_state", "vc6_status", "purpose", "phase", "receipt_digest") if k in d}, ensure_ascii=False))
    else:
        print(p.name, "-> 尚不存在")
PY
echo "VC6_DELIVER_${MODE}_DONE $(utc_now)"
