#!/bin/bash
# VC-2 五件套已准备且草案完成后，顺序执行预览、离线门、批准与 VC-3。
# 每个失败均停止后续动作；官方适用判据必须先通过，候选内部验收仍由 VC-5 执行。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
JOINT="${1:?必须提供待批准五件套联合摘要}"
echo "=== 批次 4：批准预览"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 4 VC-1 action-plan-vc2-preview.json | grep -v "^$"
echo "=== VC-2 离线判据门：通过后才允许批准"
PREFLIGHT=$(python3 -B "$DRV/vc2_assertion_preflight.py" record --joint "$JOINT")
python3 -B "$DRV/vc2_assertion_preflight.py" verify --joint "$JOINT" --report "$PREFLIGHT"
echo "=== 批次 5：批准"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-2 5 VC-1 action-plan-vc2-approve.json | grep -v "^$"
test -f "$NEWDIR/control/vc/vc-2-checkpoint.json"
echo "=== VC-3 前重放离线报告，拒绝批准后的输入漂移"
python3 -B "$DRV/vc2_assertion_preflight.py" verify --joint "$JOINT" --report "$PREFLIGHT"
echo "=== 批次 6：VC-3 stage-profile"; bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-3 6 VC-2 action-plan-vc3-stage-profile.json | grep -v "^$"
test -f "$NEWDIR/control/vc/vc-3-checkpoint.json"
