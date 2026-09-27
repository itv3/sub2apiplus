#!/bin/bash
# 前阶段 0：pre-A3 路径认证（修好接着跑第 18、19 项）。
# 必须在 stage1 建账本之前完成：stage1 一建账本 VC-0 即开始计时，约 55 分钟的 pre-A3 放在其后必然超时
# （stage1 建账本前会核验本轮认证，缺失即拒绝）。同一部署（部署收据逐字相同）、同一激活认证、工具五摘要
# 未变时复用最近一次通过的认证（逐字复制到本轮坐标），不重跑。
# 产物：$POLICY_COMPAT_RECEIPT、$POLICY_ACTIVATION、$PRE_A3_CERTIFICATION；stage2 发现已存在即跳过。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" <<'PYDEPLOY'
import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('installed_driver', sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_deploy_receipt(Path(sys.argv[2])/'control')[0])
PYDEPLOY
)
[ -f "$POLICY_COMPAT_RECEIPT" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification compatibility --previous-policy "${PREVIOUS_POLICY:?缺少前序策略文件}" --output "$POLICY_COMPAT_RECEIPT" | cut -c1-160
[ -f "$POLICY_ACTIVATION" ] || python3 -m tools.official_client_capture.codex_upgrade_policy_certification activation --deployment-receipt "$DEPLOY" --compatibility-receipt "$POLICY_COMPAT_RECEIPT" --output "$POLICY_ACTIVATION" | cut -c1-160
if [ -f "$PRE_A3_CERTIFICATION" ]; then
  echo "PRE_A3_PRESENT $PRE_A3_CERTIFICATION"
elif REUSE=$(python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --search-root "$(dirname "$PRE_A3_CERTIFICATION")" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION"); then
  cp -p "$REUSE" "$PRE_A3_CERTIFICATION"; chmod 600 "$PRE_A3_CERTIFICATION"
  echo "PRE_A3_REUSED $REUSE"
else
  python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification run --staging-root "$D/staging/pre-a3-certification-$STAMP" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --output "$PRE_A3_CERTIFICATION" | cut -c1-300
fi
# 与 stage1 建账本前同一口径复核本轮坐标（复用与新跑都要通过）。
python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" >/dev/null
echo "PRE_A3_DONE $PRE_A3_CERTIFICATION"
