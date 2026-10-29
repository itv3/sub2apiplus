#!/bin/bash
# VC-5 一条龙（按阶段状态续跑；重跑时已完成的阶段一律跳过）：
#   外部门禁六件套注入（本机 check-egress-spec + full-regression）+ 复核 test-tree（DC 提交）
#   → start（pre-vc5 检查 + 切候选网关 + 预检 + 后台 run 批次）→ 等待 run → seal 链（仅当 compare 结果不存在）
#   → accept（仅当 acceptance 结果不存在；目标平台门禁失败在 accept 内归档重跑，不回到 seal 链）
#   → canonical2（离线预览 + 两个纯 canonical 批次）→ VC-5 completion。
# 各步的派发脚本取客户端表（B5b-01b）：Codex 即 vc5-start.sh／vc5-seal.sh／vc5-accept.sh／vc5-canonical2.sh（代入后与改造前
#   逐字相同）；Claude 是 vc5-claude-start.sh／vc5-claude-seal.sh／vc5-claude-accept.sh，没有 canonical 交接（accept 写 VC-5
#   checkpoint）。Claude 的 seal 链在客户端收据未就位、等待 seal 批准两处暂停：它写 $RUNROOT/vc5-pause.json，本脚本打印
#   VC5_ALL_PAUSED reason=… next=… 后退出 0，审核人处置后重起本脚本续跑（Codex 的 seal 链不写这个文件）。
# 阶段失败不再静默（Codex 升级工具优化第 9 项）：seal 链、accept、canonical 三个阶段的输出经过过滤，上一轮 canonical 前准入消费失败时原因被过滤掉、
#   脚本带着非零退出码静默结束，流水线约 20 分钟后才报“进程已消失”。现在每个阶段失败都会先打印 VC5_ALL_STAGE_FAILED（阶段名、退出码、未过滤的完整输出落盘路径与末 8 行），
#   再以同一退出码退出；目标平台门禁结束后、canonical 前先做准入守卫（vc5_admission_guard，lib.sh）：准入消费不通过时，启动前设了 VC5_RENEW_APPROVER
#   就自动续签管理令牌（只续令牌），没设就打印 VC5_ADMISSION_GUARD_FAILED 与处理办法后退出 3。
# 用法：[VC5_RENEW_APPROVER='批准人及依据'] ARM64_VC_ENV=… setsid -f bash vc5-all.sh > $RUNROOT/vc5-all.out 2>&1 < /dev/null
set -Eeuo pipefail
PHASE_CONTEXT_ARGS=(--mode paths)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
vc5_require_admission
# 运行一个过滤输出的阶段：<阶段名> <保留行的 grep -E 模式> <命令…>。输出边跑边过滤（进度仍实时进 vc5-all.out），同时原样落盘到
# $RUNROOT/vc5-all-<阶段名>.raw；命令失败时打印 VC5_ALL_STAGE_FAILED 与末 8 行，并以命令自己的退出码返回。
vc5_stage() {
  local stage=$1 pattern=$2 rc=0 raw
  shift 2
  raw="$RUNROOT/vc5-all-${stage}.raw"
  : > "$raw"; chmod 600 "$raw"
  set +e
  "$@" 2>&1 | tee "$raw" | { grep -E "${pattern}|VC5_ADMISSION|VC5_ALL_" || true; } | cut -c1-300
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "VC5_ALL_STAGE_FAILED stage=${stage} rc=${rc} raw=${raw}（未过滤的完整输出）；末 8 行："
    tail -n 8 "$raw" | cut -c1-300
    return "$rc"
  fi
}
G=$GATE_ROOT
LOCAL_GATES=$RUNROOT/local-gates
if [ ! -f "$G/local/full-regression.gate.json" ]; then
  echo "=== 外部门禁：本机 check-egress-spec 与 full-regression 六件套注入 + 复核 test-tree（DC 提交 ${DC}）$(utc_now)"
  mkdir -p "$G/local"; chmod 700 "$G" "$G/local"
  for f in check-egress-spec.gate.json check-egress-spec.stdout.log check-egress-spec.stderr.log full-regression.gate.json full-regression.stdout.log full-regression.stderr.log; do test -f "$LOCAL_GATES/$f"; cp -f "$LOCAL_GATES/$f" "$G/local/"; done
  chown root:root "$G/local"/*; chmod 600 "$G/local"/*
  python3 -c "import json; d=json.load(open('$G/local/full-regression.gate.json')); assert d['exit_code']==0 and d['tree_head']=='$DC', d; assert d.get('client_selection') is None or '${VC5_ALLOW_SUBSET_LOCAL_GATES:-}'=='1', '本地 full-regression 是按客户端筛选的子集（client_selection），不是 make test 全集；确认接受请设 VC5_ALLOW_SUBSET_LOCAL_GATES=1'; print('full-regression(本机):', d['exit_code'], d['host'], d['architecture'], d['started_at_utc'], d['completed_at_utc'])"
  test "$(git -C $B/test-tree rev-parse HEAD)" = "$DC" || bash "$DRV/gates.sh" prepare "$DC" 2>&1 | tail -n 3
fi
if [ ! -f "$RUNROOT/vc5-run-batch.out" ]; then
  echo "=== VC-5 start $(utc_now)"; bash "$DRV/$CT_VC5_START_SCRIPT" 2>&1 | tail -n 12 | cut -c1-240; test -f "$RUNROOT/vc5-run-batch.out"
fi
echo "=== 等待 run 批次（candidate run）$(utc_now)"
wait_for_marker "$RUNROOT/vc5-run-batch.out" '^RUN_BATCH_DONE ' "$(wait_budget VC-5)" '' \
  --pid-file "$RUNROOT/vc5-run-batch.pid" --supervisor-root "${VC_STATE_DIR:-$D/control/$NEW-supervisor}" \
  --log "$RUNROOT/vc5-all.out"
grep -E "rc=|actions:|timing events|admission" "$RUNROOT/vc5-run-batch.out" | cut -c1-300
phase_require --mode attempt
echo "ATT=$ATT status=$ATT_STATUS baseline=b$EVALUATION_BASELINE"
if [ "$COMPARE_READY" != 1 ]; then
  rm -f "$RUNROOT/vc5-pause.json"
  echo "=== seal 链 $(utc_now)"; vc5_stage seal "===|ATT=|batch ok|BATCH FAILED|REHEARSAL|预演:|CKPT=|SEAL_SHA=|candidate result|compare:|账本|SEAL_DONE|SEAL_ABORT|PERMISSION_CLOSEOUT|Error|Traceback|拒绝|失败|assert" bash "$DRV/$CT_VC5_SEAL_SCRIPT" "$ATT"
  if [ -f "$RUNROOT/vc5-pause.json" ]; then
    python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print('VC5_ALL_PAUSED reason=' + d['reason'] + ' next=' + d['next'])" "$RUNROOT/vc5-pause.json"; exit 0
  fi
else
  echo "=== seal 链已完成（当前基线比较收据重放通过），跳过 $(utc_now)"
fi
phase_require --mode attempt --attempt-id "$ATT"
test "$COMPARE_READY" = 1
if [ "$ACCEPT_READY" != 1 ]; then
  echo "=== accept $(utc_now)"; vc5_stage accept "===|assertions:|evaluation-run:|target-platform|归档|replay:|batch ok|BATCH FAILED|acceptance:|账本|ACCEPT_DONE|Error|Traceback|拒绝|失败|缺少" bash "$DRV/$CT_VC5_ACCEPT_SCRIPT" "$ATT"
else
  echo "=== accept 已完成（当前基线验收收据重放通过），跳过 $(utc_now)"
fi
phase_require --mode attempt --attempt-id "$ATT"
test "$ACCEPT_READY" = 1
ls "$NEWDIR/control/vc/" | tr "\n" " "; echo
if [ -n "$CT_VC5_CANONICAL_SCRIPT" ]; then
  # 目标平台门禁（accept 内）刚结束：准入批准件与管理令牌可能已在这两个小时里过期，canonical 前先守一次（不通过就续签或明确报错，不再静默退出）。
  vc5_admission_guard canonical
  echo "=== canonical 交接 $(utc_now)"; vc5_stage canonical "review_sha256|CLI preview|一致|plan ->|===|rc=|actions:|checkpoint|import-receipt|canonical latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败" bash "$DRV/$CT_VC5_CANONICAL_SCRIPT" "$ATT"
else
  echo "=== 本轮客户端（${CT_CLIENT}）没有 canonical 交接：VC-5 已在 accept 时完成 $(utc_now)"
fi
phase_require --mode attempt --attempt-id "$ATT" --require-completion VC-5
echo "VC-5 checkpoint: $VC5_CHECKPOINT receipt: $VC5_RECEIPT"
echo "VC5_ALL_DONE ATT=$ATT $(utc_now)"
