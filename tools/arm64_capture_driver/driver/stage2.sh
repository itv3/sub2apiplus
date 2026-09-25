#!/bin/bash
# 前阶段 2：本轮兼容/激活认证 → pre-A3 路径认证 → 发布认证 → reuse-official-evidence 原子导入并自动对齐账本。
# 读取 $RUNROOT/stage1.env（stage1 产物坐标）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
# shellcheck disable=SC1091
source "$RUNROOT/stage1.env"
# R19：客户端启动探测必须已通过且属于本轮预检 Campaign，否则不进入认证与导入。
: "${PROBE:?stage1.env 缺少客户端启动探测坐标（先跑 stage1-finish.sh）}"
python3 -B "$DRV/client_launch_probe.py" verify --output-dir "$PROBE" --campaign-dir "$PRE" | cut -c1-200
[ -f "$POLICY_COMPAT_RECEIPT" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification compatibility --previous-policy "${PREVIOUS_POLICY:?缺少前序策略文件}" --output "$POLICY_COMPAT_RECEIPT" | cut -c1-160
[ -f "$POLICY_ACTIVATION" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification activation --deployment-receipt "$DEPLOY" --compatibility-receipt "$POLICY_COMPAT_RECEIPT" --output "$POLICY_ACTIVATION" | cut -c1-160
[ -f "$PRE_A3_CERTIFICATION" ] || python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification run --staging-root "$D/staging/pre-a3-certification-$STAMP" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --output "$PRE_A3_CERTIFICATION" | cut -c1-300
[ -f "$RELEASE_CERTIFICATION" ] || python3 -m tools.official_client_capture.certify_release issue --deployment-receipt "$DEPLOY" --pre-a3-certification "$PRE_A3_CERTIFICATION" --policy-activation "$POLICY_ACTIVATION" --job-rehearsal-root "$JR" --job-rehearsal-receipt receipt.json --atomic-rehearsal-root "$D/staging/$AT" --atomic-rehearsal-receipt receipt.json --atomic-container capture-cli --data-root "$D" --output "$RELEASE_CERTIFICATION" | cut -c1-200
python3 -m tools.official_client_capture.certify_release verify --certification "$RELEASE_CERTIFICATION" | cut -c1-160
# 此入口只用于同一目标版本的证据恢复；新版本首次 VC-1 必须从正式 closeout 入口重新取证。
[ "$EVIDENCE_DECISION" = reuse ] || { echo '新目标首次取证：使用 codex_upgrade_vc0_closeout 正式入口完成 VC-1 后再进入 vc23.sh'; exit 3; }
python3 - "$PREDECESSOR_CAMPAIGN" "$TARGET_VERSION" <<'PYVERSION'
import sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
manifest=cu.load_campaign_manifest(Path(sys.argv[1]))
if manifest['target_version'] != sys.argv[2]:
    raise SystemExit('前序官方证据目标版本不一致，禁止跨版本复用')
PYVERSION
PL="$D/evidence/campaigns/upgrade-project-ledger"
python3 -m tools.official_client_capture.codex_upgrade reuse-official-evidence --predecessor-campaign-dir "$PREDECESSOR_CAMPAIGN" --campaign-dir "$NEWDIR" --campaign-id "$NEW" --codex-account-id "$CODEX_ACCOUNT_ID" --job-rehearsal-root "$JR" --job-rehearsal-receipt receipt.json --recovery-timing-ledger-dir "$L" --recovery-timing-receipt "receipts/vc0-input-$ROUND-preflight-$STAMP.json" --recovery-arm64-environment-root "$ENV" --recovery-arm64-environment-receipt receipt.json --predecessor-stop-ledger-dir "$OFFICIAL_STOP_LEDGER" --predecessor-stop-receipt "$OFFICIAL_STOP_RECEIPT" 2>&1 | python3 -c "
import sys,json
t=sys.stdin.read()
try:
    d=json.loads(t); print({k:d.get(k) for k in (\"status\",\"campaign_id\",\"next_command\")})
except Exception: print(t[-1200:])"
test -d "$NEWDIR" || { echo "REUSE_FAILED"; exit 1; }
python3 -m tools.official_client_capture.codex_upgrade status --campaign-dir "$NEWDIR" 2>&1 | python3 -c "
import sys,json
t=sys.stdin.read()
try:
    d=json.loads(t); print({k:d.get(k) for k in (\"status\",\"next_command\")}, \"registered=\", (d.get(\"project_ledger\") or {}).get(\"campaign_registered\"), \"head=\", (d.get(\"project_ledger\") or {}).get(\"head_sequence\"))
except Exception: print(t[-600:])"
python3 -m tools.official_client_capture.codex_upgrade_project_ledger status --ledger-dir "$PL" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); h=d.get(\"head\",d); print(\"project:\", {k:h.get(k) for k in (\"sequence\",\"precise_total\",\"estimated_total\",\"blocked\")})"
echo "STAGE2_DONE"
