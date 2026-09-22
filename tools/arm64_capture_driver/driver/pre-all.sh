#!/bin/bash
# stage1 之后的前置一条龙：stage2（认证 + reuse 建 Campaign + 账本对齐）→ vc23（管理 token、五清单、VC-2 批次 2～5、VC-3 批次 6）。
# 用法：ARM64_VC_ENV=… bash pre-all.sh（日志落 $RUNROOT/stage2.log、$RUNROOT/vc23.log）
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
test -f "$RUNROOT/stage1.env" || { echo "缺少 $RUNROOT/stage1.env（先跑 stage1.sh）"; exit 1; }
bash "$DRV/stage2.sh" > "$RUNROOT/stage2.log" 2>&1
grep -q "STAGE2_DONE" "$RUNROOT/stage2.log"
bash "$DRV/vc23.sh" > "$RUNROOT/vc23.log" 2>&1
grep -q "VC23_DONE" "$RUNROOT/vc23.log"
echo "PRE_ALL_DONE $(utc_now)"
