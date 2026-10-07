#!/bin/bash
# VC-2／VC-3（采集主机端，Claude，VC-1 封存之后；Claude 升级 B5b-01）：一次派发一个批次。
#   VC-2（前序 VC-1）：classify-draft → 审核人处置 → classify-revise → classify-approve；
#   VC-3（前序 VC-2）：prepare-draft → prepare-revise → prepare-approve → stage-preview → stage-approve。
# 动作计划经 claude_vc23.py 用受管 claude_upgrade_batch 的受管动作生成（受管 Python 直接调 claude_upgrade.py、旗标精确闭合，
#   生成后按阶段幂等重派合同自检），经 vc-batch.sh 派发（批次边界的后台验证与整机预约照常）。草案、修订与预览是批次内的
#   合法暂停点；审核摘要、联合摘要与批准人由审核人给出，本脚本不自动批准。由客户端表 vc23_dispatch_script 登记（Codex 是
#   vc23.sh，流程不变）。
# 用法：bash vc23-claude.sh <步骤> [--from-revision <rev> --revision-input <目录>] [--approve-revision <rev>
#   --approve-review-sha256 <摘要>] [--approve-joint-sha256 <摘要>] [--approved-by <批准人>]
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
if [ "$CT_VC23_DISPATCH_SCRIPT" != vc23-claude.sh ]; then
  echo "本轮客户端（${CT_CLIENT}）的 VC-2／VC-3 派发脚本是 ${CT_VC23_DISPATCH_SCRIPT}，拒绝"; exit 3
fi
STEP="${1:?必须给出步骤：classify-draft|classify-revise|classify-approve|prepare-draft|prepare-revise|prepare-approve|stage-preview|stage-approve}"
shift
PHASES=$(python3 "$DRV/claude_vc23.py" phase --step "$STEP")
read -r PHASE PRED <<< "$PHASES"
PLAN="action-plan-claude-$STEP.json"
mkdir -p "$W"; chmod 700 "$W"
python3 "$DRV/claude_vc23.py" plan --step "$STEP" --output "$W/$PLAN" "$@"
SEQ=$(next_seq); echo "批次序号=$SEQ（$PHASE，前序 $PRED）"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" "$PHASE" "$SEQ" "$PRED" "$PLAN" | grep -v "^$"
python3 "$DRV/claude_vc23.py" summary
echo "VC23_CLAUDE_DONE step=$STEP"
