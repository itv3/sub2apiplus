#!/bin/bash
# Claude 的 VC-5 启动（Claude 升级 B5b-01b，客户端表 vc5_start_script）：派发前检查（pre-vc5）→ run 计划（claude_vc5.py 用受管
#   capture-candidate run 生成）→ 切换候选网关（vc5-claude-switch.sh：Claude 候选开关、隔离实例 ARM64 与激活事实变量）→ 只读
#   预检（候选 run 的正式入口门）→ 后台派发 run 批次。与 Codex 的 vc5-start.sh 同一套锁、准入、守卫与后台批次写法。
# 用法：ARM64_VC_ENV=… bash vc5-claude-start.sh（批次日志 $RUNROOT/vc5-run-batch.out）
set -Eeuo pipefail; umask 077
PHASE_CONTEXT_ARGS=(--mode build)
CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-claude-start.sh)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
vc5_dispatch_lock
vc5_require_admission
if [ -f "$RUNROOT/vc5-run-batch.out" ]; then echo "VC5_START_SKIP: $RUNROOT/vc5-run-batch.out 已存在（run 批次已派发过，不重复）"; exit 0; fi
echo "=== 派发前检查（驱动/磁盘/总账/账本/VC-4 checkpoint）"; bash "$DRV/guard.sh" pre-vc5 "$NEWDIR" 2>&1 | tee -a "$RUNROOT/guard-pre-vc5.out"
test "$(git -C "$B/source" rev-parse HEAD)" = "$C"; TAG=$CANDIDATE_IMAGE_REPOSITORY:$ROUND-${C:0:9}
echo "IMAGE_ID=$IMAGE_ID BUILD_ID=$BUILD_ID TAG=$TAG TREE=$TREE"
mkdir -p "$W"; chmod 700 "$W"
python3 "$DRV/claude_vc5.py" run-plan --output "$W/action-plan-vc5-run.json" | cut -c1-200
echo "=== 切换候选网关"; bash "$DRV/vc5-claude-switch.sh" candidate "$TAG" 2>&1 | tail -n 4
export ADMIN_BEARER_TOKEN_FILE=$D/state/$UP/admin-token
echo "=== 预检"; python3 "$DRV/claude_vc5.py" precheck 2>&1 | tail -n 6 | cut -c1-240
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
