#!/bin/bash
# VC-4 前置小步骤（门禁需求来源为 arm64-preview 的客户端，即 Claude；Claude 升级 B3-04a）：
#   基础准入检查 → 登记当前候选 revision（revision-open，同一参数重跑幂等）→ 门禁需求预览（plan-candidate-gates 只给
#   Campaign 与候选，零写入）写 $RUNROOT/vc4-gate-requirements.json（0600），交本机候选链 local-claude-candidate-chain.sh
#   只读取。需求预览的结果与按 VC-2／VC-3 阶段结果只读推出的需求逐字核对（vc4_client.py requirements）。
# 次序：VC-3 完成后、本机候选链之前、VC-4 总控 vc4-all.sh 之前执行。本步不读 C／DC／RECEIPT／BUNDLE（候选提交还没有，
#   参数文件里可先沿用上一轮的取值；vc4-all.sh 之前改成本轮本机候选链给出的提交）。
# Codex 的门禁需求由本机候选链在采集主机现算（客户端表 vc4_requirements_source=local-chain），这里拒绝，Codex 流程不变。
# 用法：ARM64_VC_ENV=… bash vc4-requirements.sh
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
if [ "$CT_VC4_REQUIREMENTS_SOURCE" != arm64-preview ]; then
  echo "本轮客户端（${CT_CLIENT}）的门禁需求不经采集主机预览（vc4_requirements_source=${CT_VC4_REQUIREMENTS_SOURCE}），用本机候选链"
  exit 3
fi
echo "=== 基础准入检查与 revision 登记"; bash "$DRV/guard.sh" pre-plan
python3 "$DRV/vc4_resume.py" open-revision --evidence-root "$RUNROOT" || exit 3
echo "=== 门禁需求预览（零写入）"
python3 "$DRV/vc4_client.py" requirements --output "$RUNROOT/vc4-gate-requirements.json" || exit 3
echo "VC4_REQUIREMENTS_DONE $(utc_now)"
