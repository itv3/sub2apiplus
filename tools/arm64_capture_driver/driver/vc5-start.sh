#!/bin/bash
# VC-5 启动：派发前检查（pre-vc5）→ run 计划 → 切换候选网关 → 只读预检 → 后台派发 run 批次（10 Job 全量，含 candidate-trace-test）。
# 用法：ARM64_VC_ENV=… bash vc5-start.sh（批次日志 $RUNROOT/vc5-run-batch.out）
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
if [ -f "$RUNROOT/vc5-run-batch.out" ]; then echo "VC5_START_SKIP: $RUNROOT/vc5-run-batch.out 已存在（run 批次已派发过，不重复）"; exit 0; fi
echo "=== 派发前检查（驱动/磁盘/总账/账本/VC-4 checkpoint）"; bash "$DRV/guard.sh" pre-vc5 "$NEWDIR" 2>&1 | tee -a "$RUNROOT/guard-pre-vc5.out"
IMAGE_ID=$(python3 -c "import json; print(json.load(open('$B/artifacts/build-parameters.json'))['docker_build']['image_id'])")
BUILD_ID=$(python3 -c "import json; print(json.load(open('$NEWDIR/candidates/$CAND/build-receipt.json'))['build']['build_id'])")
test "$(git -C "$B/source" rev-parse HEAD)" = "$C"; TAG=$CANDIDATE_IMAGE_REPOSITORY:$ROUND-${C:0:9}
TREE=$(python3 -c "
from pathlib import Path
from tools.official_client_capture import codex_upgrade as cu
print(cu._directory_tree_digest(Path('$B/source')))")
echo "IMAGE_ID=$IMAGE_ID BUILD_ID=$BUILD_ID TAG=$TAG TREE=$TREE"
mkdir -p "$W"; chmod 700 "$W"
CANDIDATE_DIR=$B PROFILE_ID=$PROFILE_ID PROFILE_DIGEST=$PROFILE_DIGEST python3 "$DRV/gen_vc5_plans.py" "$W" "$NEW" "$CAND" "$IMAGE_ID" "$BUILD_ID" | cut -c1-160; chmod 600 "$W"/*.json
echo "=== 切换候选网关"; bash "$DRV/vc5-switch.sh" candidate "$TAG" "$IMAGE_ID" "$TREE" "$BUILD_ID" 2>&1 | tail -n 3
echo "=== 预检"; bash "$DRV/vc5-precheck.sh" "$IMAGE_ID" "$BUILD_ID" 2>&1 | tail -n 6 | cut -c1-240
cat > "$RUNROOT/vc5-run-batch.sh" <<RUN
#!/bin/bash
echo \$\$ > "$RUNROOT/vc5-run-batch.pid"
set -Eeuo pipefail
export ARM64_VC_ENV=$ARM64_VC_ENV ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
SEQ=\$(python3 -c "import glob,os; print(max(int(os.path.basename(p).split('-')[0]) for p in glob.glob('$NEWDIR/control/vc/batches/*.json'))+1)"); echo "VC-5 run 批次序号=\$SEQ"
bash "$DRV/vc-batch.sh" $NEW $IN VC-5 \$SEQ VC-4 action-plan-vc5-run.json
echo "RUN_BATCH_DONE \$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN
chmod 700 "$RUNROOT/vc5-run-batch.sh"
rm -f "$RUNROOT/vc5-run-batch.pid"
echo "--- 残留检查"; pgrep -af "mitmdum[p]|tcpdum[p]" | cut -c1-100 || echo "无 mitmdump/tcpdump"; docker exec capture-cli sh -c "ls /run/oauth-capture/ 2>/dev/null | grep -v stopped || true"
setsid -f bash "$RUNROOT/vc5-run-batch.sh" > "$RUNROOT/vc5-run-batch.out" 2>&1 < /dev/null; echo "run 批次后台启动于 $(utc_now)"
