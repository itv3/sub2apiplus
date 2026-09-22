#!/bin/bash
# 前阶段 2：策略 v6→v7 兼容/激活认证 → pre-A3 路径认证 → 发布认证 → reuse-official-evidence 建 Formal Campaign → 账本对齐。
# 读取 $RUNROOT/stage1.env（stage1 产物坐标）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
# shellcheck disable=SC1091
source "$RUNROOT/stage1.env"
PC="$D/control/policy-certification"
PREV_POLICY="$PC/tool_identity_policy_v6_previous.json"
[ -f "$PREV_POLICY" ] || { cp "$D/control/managed-tools-backup-before-de2e16889794-20260917t222001z-488bba78/tool_identity_policy_v2.json" "$PREV_POLICY"; chmod 600 "$PREV_POLICY"; }
python3 -c "import json; print(\"previous policy_version:\", json.load(open(\"$PREV_POLICY\"))[\"policy_version\"])"
[ -f "$PC/policy-compatibility-v6-to-v7-20260918t104926z.json" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification compatibility --previous-policy "$PREV_POLICY" --output "$PC/policy-compatibility-v6-to-v7-20260918t104926z.json" | cut -c1-160
[ -f "$PC/policy-activation-v7-$STAMP.json" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification activation --deployment-receipt "$DEPLOY" --compatibility-receipt "$PC/policy-compatibility-v6-to-v7-20260918t104926z.json" --output "$PC/policy-activation-v7-$STAMP.json" | cut -c1-160
[ -f "$PC/pre-a3-path-certification-$STAMP.json" ] || python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification run --staging-root "$D/staging/pre-a3-certification-$STAMP" --deployment-receipt "$DEPLOY" --policy-activation "$PC/policy-activation-v7-$STAMP.json" --output "$PC/pre-a3-path-certification-$STAMP.json" | cut -c1-300
[ -f "$PC/tool-release-certification-v7-$ROUND-$STAMP.json" ] || python3 -m tools.official_client_capture.certify_release issue --deployment-receipt "$DEPLOY" --pre-a3-certification "$PC/pre-a3-path-certification-$STAMP.json" --policy-activation "$PC/policy-activation-v7-$STAMP.json" --job-rehearsal-root "$JR" --job-rehearsal-receipt receipt.json --atomic-rehearsal-root "$D/staging/$AT" --atomic-rehearsal-receipt receipt.json --atomic-container capture-cli --data-root "$D" --output "$PC/tool-release-certification-v7-$ROUND-$STAMP.json" | cut -c1-200
python3 -m tools.official_client_capture.certify_release verify --certification "$PC/tool-release-certification-v7-$ROUND-$STAMP.json" | cut -c1-160
PL="$D/evidence/campaigns/upgrade-project-ledger"
python3 -m tools.official_client_capture.codex_upgrade reuse-official-evidence --predecessor-campaign-dir "$OFFICIAL_CAMPAIGN" --campaign-dir "$NEWDIR" --campaign-id "$NEW" --codex-account-id 22 --job-rehearsal-root "$JR" --job-rehearsal-receipt receipt.json --recovery-timing-ledger-dir "$L" --recovery-timing-receipt "receipts/vc0-input-$ROUND-preflight-$STAMP.json" --recovery-arm64-environment-root "$ENV" --recovery-arm64-environment-receipt receipt.json --predecessor-stop-ledger-dir "$OFFICIAL_STOP_LEDGER" --predecessor-stop-receipt "$OFFICIAL_STOP_RECEIPT" 2>&1 | python3 -c "
import sys,json
t=sys.stdin.read()
try:
    d=json.loads(t); print({k:d.get(k) for k in (\"status\",\"campaign_id\",\"next_command\")})
except Exception: print(t[-1200:])"
test -d "$NEWDIR" || { echo "REUSE_FAILED"; exit 1; }
bash "$DRV/align-ledger.sh" "$L"
python3 -m tools.official_client_capture.codex_upgrade status --campaign-dir "$NEWDIR" 2>&1 | python3 -c "
import sys,json
t=sys.stdin.read()
try:
    d=json.loads(t); print({k:d.get(k) for k in (\"status\",\"next_command\")}, \"registered=\", (d.get(\"project_ledger\") or {}).get(\"campaign_registered\"), \"head=\", (d.get(\"project_ledger\") or {}).get(\"head_sequence\"))
except Exception: print(t[-600:])"
python3 -m tools.official_client_capture.codex_upgrade_project_ledger status --ledger-dir "$PL" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); h=d.get(\"head\",d); print(\"project:\", {k:h.get(k) for k in (\"sequence\",\"precise_total\",\"estimated_total\",\"blocked\")})"
echo "STAGE2_DONE"
