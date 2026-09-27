#!/bin/bash
# VC-5 候选采集续跑（修好接着跑）：失败的 capture-candidate run（或上一轮续跑补跑）批次对账、批准恢复预览并授权后，
#   依次派发 N+1 零请求恢复预览批次与 N+2 按已批准预览的真实补跑批次（只重跑失败与受工具演进影响的作业，其余只读复用）；
#   补跑批次成功后向 vc5-run-batch.out 写 RUN_BATCH_DONE，随后照常执行 vc5-all.sh（seal 链、accept、canonical）。
# 前置（直接控制命令，由操作员逐步执行并核对输出）：受监督部署 → tool-evolution 预览／批准（登记本次修复）→
#   reconcile-attempt（登记失败 attempt）→ reconcile-attempt --approve-recovery-sha256 → authorize-recovery-preview。
# 用法：ARM64_VC_ENV=… [VC_STATE_DIR=<监督器状态目录>] setsid -f bash vc5-recover.sh <已授权的 recovery-preview 绝对路径> \
#         > $RUNROOT/vc5-recover.out 2>&1 < /dev/null
# 原 vc5-run-batch.out 若没有 RUN_BATCH_DONE（失败批次的日志）先改名留档；已有完成标记时不重复派发。
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
PREVIEW="$1"; test -f "$PREVIEW"
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
ensure_admin_token
OUT="$RUNROOT/vc5-run-batch.out"
if [ -f "$OUT" ] && grep -q '^RUN_BATCH_DONE ' "$OUT"; then echo "VC5_RECOVER_SKIP: $OUT 已有 RUN_BATCH_DONE"; exit 0; fi
if [ -f "$OUT" ]; then mv "$OUT" "$OUT.failed-$(date -u +%Y%m%dt%H%M%Sz)"; fi
echo $$ > "$RUNROOT/vc5-run-batch.pid"
mkdir -p "$W"; chmod 700 "$W"
python3 "$DRV/gen_vc5_recovery_plans.py" "$W" "$NEWDIR" "$PREVIEW" | cut -c1-400
# 与 vc5-start 同一套派发前检查与候选网关切换：补跑复用的旧结果要求本轮 before 探针与来源 attempt 的 after 探针
# 在 service／containers／account／configuration 上连续，网关必须切回同一候选镜像与声明身份。
echo "=== 派发前检查（驱动/磁盘/总账/账本/VC-4 checkpoint）"; bash "$DRV/guard.sh" pre-vc5 "$NEWDIR" 2>&1 | tee -a "$RUNROOT/guard-pre-vc5.out"
IMAGE_ID=$(python3 -c "import json; print(json.load(open('$B/artifacts/build-parameters.json'))['docker_build']['image_id'])")
BUILD_ID=$(python3 -c "import json; print(json.load(open('$NEWDIR/candidates/$CAND/build-receipt.json'))['build']['build_id'])")
test "$(git -C "$B/source" rev-parse HEAD)" = "$C"; TAG=$CANDIDATE_IMAGE_REPOSITORY:$ROUND-${C:0:9}
TREE=$(python3 -c "
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path('$B/source')))")
echo "IMAGE_ID=$IMAGE_ID BUILD_ID=$BUILD_ID TAG=$TAG TREE=$TREE"
echo "=== 切换候选网关"; bash "$DRV/vc5-switch.sh" candidate "$TAG" "$IMAGE_ID" "$TREE" "$BUILD_ID" 2>&1 | tail -n 3
echo "=== 预检"; bash "$DRV/vc5-precheck.sh" "$IMAGE_ID" "$BUILD_ID" 2>&1 | tail -n 6 | cut -c1-240
batch() {
  local seq="$1" plan="$2"
  bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-5 "$seq" VC-4 "$plan" | grep -v "^$" || true
  python3 - "$W/batch-$seq.out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); run = d.get("campaign_run") or {}
acts = run.get("actions", [])
ok = d.get("status") == "stopped" and all(a.get("status") == "passed" for a in acts) and acts
print("batch ok" if ok else "BATCH FAILED")
sys.exit(0 if ok else 1)
PY
}
{
  SEQ=$(next_seq); echo "=== 批次 ${SEQ}：零请求恢复预览 $(utc_now)"
  batch "$SEQ" action-plan-vc5-recovery-preview.json
  SEQ=$((SEQ+1)); echo "=== 批次 ${SEQ}：按已批准预览真实补跑 $(utc_now)"
  batch "$SEQ" action-plan-vc5-recovery-run.json
  ATT=$(ls -1t "$NEWDIR/candidates/$CAND/attempts/" | head -1)
  python3 -c "import json; d=json.load(open('$NEWDIR/candidates/$CAND/attempts/$ATT/attempt.json')); rs=d.get('results') or []; print('attempt:', {k:d.get(k) for k in ('status','attempt_id')}, 'complete:', sum(1 for r in rs if r.get('status')=='complete'), '/', len(rs), 'reused:', sum(1 for r in rs if r.get('disposition')=='reused'))"
  echo "RUN_BATCH_DONE $(utc_now)"
} >> "$OUT" 2>&1
