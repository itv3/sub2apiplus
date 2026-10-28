#!/bin/bash
# Codex VC-1 官方证据封存（驱动里没有 Codex 的封存脚本，历次升级都是派发两个批次：封存预览 → 封存批准）。
# 随驱动安装在 <驱动目录>/pipeline/vc2/vc1-seal.sh。零新增真实请求（封存只处理已采到的证据）。
#   ARM64_VC_ENV=… bash vc1-seal.sh preview          首批（capture-official run）完成、attempt 处于 awaiting_receipts 后：派发封存预览批次
#   ARM64_VC_ENV=… bash vc1-seal.sh approve --yes    读出最新预览的 review_sha256，作为 --approve-seal-sha256 派发封存批准批次，
#                                                    再做账本登记（account-sealed-official），核对 VC-1 checkpoint
# 批准是治理动作：--yes 必须由调度者（或调度者起跑流水线时传的 --approver）显式给出；预览输出的要点会先打印供核对。
# 批次序号取下一个可用序号（next_seq）；封存批次的前序阶段是 VC-0（与历次升级一致）。
set -Eeuo pipefail; umask 077
export PYTHONDONTWRITEBYTECODE=1
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV}"
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
PIPE_HOME=${PIPE_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
source "${PIPE_DRIVER_DIR}/lib.sh"; cd "$D"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODE=${1:?preview|approve}
ATT_ROOT=$NEWDIR/official/attempts
awaiting() { # 恰好一个 awaiting_receipts 的 attempt 目录
  python3 -B - "$ATT_ROOT" <<'PY'
import json, sys
from pathlib import Path
ready = [p.parent for p in sorted(Path(sys.argv[1]).glob("*/attempt.json")) if json.load(open(p)).get("status") == "awaiting_receipts"]
assert len(ready) == 1, f"需要恰好一个 awaiting_receipts 的 attempt，实际 {[r.name for r in ready]}"
print(ready[0])
PY
}
case "$MODE" in
preview)
  A=$(awaiting); echo "attempt：$A"
  python3 -B "$HERE/gen_vc1_seal_plans.py" preview
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：VC-1 封存预览 $(utc_now)"
  bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-1 "$SEQ" VC-0 action-plan-vc1-seal-preview.json
  python3 -B - "$A" <<'PY'
import glob, json, os, sys
previews = sorted(glob.glob(sys.argv[1] + "/seal-preview*.json"), key=os.path.getmtime)
assert previews, "没有生成 seal-preview*.json"
d = json.load(open(previews[-1]))
print("最新封存预览：", previews[-1])
print("review_sha256 =", d["review_sha256"])
print("scan_summary  =", json.dumps(d.get("scan_summary"), ensure_ascii=False)[:600])
PY
  echo "VC1_SEAL_PREVIEW_DONE $(utc_now)" ;;
approve)
  [ "${2:-}" = "--yes" ] || { echo "批准是治理动作：确认预览要点后加 --yes"; exit 2; }
  A=$(awaiting)
  SHA=$(python3 -B - "$A" <<'PY'
import glob, json, os, sys
previews = sorted(glob.glob(sys.argv[1] + "/seal-preview*.json"), key=os.path.getmtime)
assert previews, "还没有封存预览：先 vc1-seal.sh preview"
print(json.load(open(previews[-1]))["review_sha256"])
PY
)
  echo "批准摘要（最新预览的 review_sha256）：$SHA"
  python3 -B "$HERE/gen_vc1_seal_plans.py" approve "$SHA"
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：VC-1 封存批准 $(utc_now)"
  bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-1 "$SEQ" VC-0 action-plan-vc1-seal-approve.json
  echo "=== 账本登记（封存批准批次之后单独跑 account-sealed-official；已登记则幂等）"
  python3 -B tools/official_client_capture/codex_upgrade.py account-sealed-official --campaign-dir "$NEWDIR" || echo "（提示）account-sealed-official 返回非零，先看输出再判断是否已自动登记"
  test -f "$NEWDIR/control/vc/vc-1-checkpoint.json" && echo "vc-1-checkpoint.json 已写出" || { echo "封存批准后仍没有 vc-1-checkpoint.json"; exit 3; }
  echo "VC1_SEAL_APPROVE_DONE $(utc_now)" ;;
*) echo "用法：preview | approve --yes"; exit 2 ;;
esac
