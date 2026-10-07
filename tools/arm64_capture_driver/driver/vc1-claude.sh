#!/bin/bash
# VC-1（采集主机端，Claude，VC-0 收口派发的首批 capture-official 完成之后；Claude 升级 B2-11 第二部分）：
#   不带参数：预览批次——静态分析 vc1-01-analyze-target（seal 的目标 SinkInventory 与 sink containment）在前，seal 预览
#     vc1-02-seal-preview 在后（停在等批准；只读复用前序的 Campaign 在这一批里直接封存完成）；
#   带审核过的 review_sha256：批准批次——seal 批准 vc1-03-seal-approve。
# 动作计划经 claude_vc1.py 用受管 claude_upgrade_static.vc1_seal_actions 生成（受管 Python 直接调 claude_upgrade.py），经
#   vc-batch.sh 派发（阶段 VC-1、前序 VC-0；监督器状态目录与 VC-0 收口同一个 control/<NEW>-supervisor）。由客户端表
#   vc1_dispatch_script 登记（Codex 没有这一项）。
# 用法：bash vc1-claude.sh [<批准的 review_sha256>]；环境变量 DISCOVERY_DISPOSITIONS 给发现项的人工处置文件（Campaign 内，
#   预览与批准给同一份）。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
if [ "$CT_VC1_DISPATCH_SCRIPT" != vc1-claude.sh ]; then
  echo "本轮客户端（${CT_CLIENT}）没有登记 vc1-claude.sh 为 VC-1 派发脚本，拒绝"; exit 3
fi
APPROVE="${1:-}"
ARGS=()
if [ -n "$APPROVE" ]; then PLAN=action-plan-vc1-seal-approve.json; ARGS+=(--approve-seal-sha256 "$APPROVE"); else PLAN=action-plan-vc1-seal-preview.json; fi
if [ -n "${DISCOVERY_DISPOSITIONS:-}" ]; then ARGS+=(--discovery-dispositions "$DISCOVERY_DISPOSITIONS"); fi
python3 "$DRV/claude_vc1.py" plan --output "$W/$PLAN" ${ARGS[@]+"${ARGS[@]}"}
SEQ=$(next_seq); echo "批次序号=$SEQ"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-1 "$SEQ" VC-0 "$PLAN" | grep -v "^$"
python3 "$DRV/claude_vc1.py" summary
echo "VC1_DONE"
