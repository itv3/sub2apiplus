#!/bin/bash
# 前阶段 0：pre-A3 路径认证（修好接着跑第 18、19 项）。
# 必须在 stage1 建账本之前完成：stage1 一建账本 VC-0 即开始计时，pre-A3（44 个场景按整机额度并行，ARM64 4 核
# 约 8 分钟）放在其后会挤占 VC-0 预算
# （stage1 建账本前会核验本轮认证，缺失即拒绝）。工具五摘要与策略未变时复用最近一次通过的认证（逐字复制到
# 本轮坐标），重新部署（部署收据、激活认证换新）也不重跑；跨部署复用登记复用收据 pre-a3-reuse-*.json
# （write-once、按绑定内容幂等），stage2 的发布认证据此把复用来的认证绑定到本次部署收据。
# 产物：$POLICY_COMPAT_RECEIPT、$POLICY_ACTIVATION、$PRE_A3_CERTIFICATION（及复用收据）；stage2 发现已存在即跳过。
# E1-02：第一步先跑入口便宜检查（entry-preflight.sh）——参数、守卫、部署绑定、目标客户端、官方包、plan 审计、
# 环境探针试采，几分钟内把这些错误一次报全；未通过即停，不进入 pre-A3。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
bash "$DRV/entry-preflight.sh"
use_managed_bytecode   # 便宜检查刚按受管树内容准备好字节码共享层，本脚本后续的子命令沿用（E2-02）
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
  issue_pre_a3_certification   # E2-03：按场景并行跑完再核对签发（lib.sh），没通过即停、修好后同一 STAMP 直接重跑
fi
# 与 stage1 建账本前同一口径复核本轮坐标（复用与新跑都要通过）。
python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification find-reusable --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" >/dev/null
# 跨部署复用登记复用收据（同一组绑定不重复写；认证就是本次部署下签发的则不需要）。
python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification record-reuse --certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation "$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" | cut -c1-300
echo "PRE_A3_DONE $PRE_A3_CERTIFICATION"
