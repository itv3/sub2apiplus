#!/bin/bash
# VC-4（采集主机端，Claude，构建完成后；Claude 升级 B3-04a）：实现测试收据 → 批次 plan-candidate-gates（计划 + 批次外
#   生成模式预演 + 派发）→ 批次 record-candidate-build（计划 + --preview 零写入预览 + 派发）→ 收尾摘要。
# 由 vc4-all.sh 按客户端表 vc4_dispatch_script 调用（Codex 是 vc4.sh，流程不变）。两个批次的动作计划经 claude_vc4.py 用
#   claude_upgrade_batch 的受管动作生成（受管 Python 直接调 claude_upgrade.py、旗标精确闭合，生成后按 VC-4 阶段幂等重派
#   合同自检）；record 的预演是 claude_upgrade 自带的零写入预览，另在前后比对 Campaign 写域（候选目录、VC 控制制品、
#   受管 Store）。实现测试收据沿用客户端中立的 vc4-facts.sh；Claude 不复用实现测试（每轮全量重跑）。
# 用法：bash vc4-claude.sh <E 证据根>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
E="$1"
if [ "$CT_VC4_DISPATCH_SCRIPT" != vc4-claude.sh ]; then
  echo "本轮客户端（${CT_CLIENT}）的 VC-4 派发脚本是 ${CT_VC4_DISPATCH_SCRIPT}，拒绝"; exit 3
fi
if [ ! -f "$E/receipt.json" ]; then
  test -f "$E/logs/check-egress-spec.log"; test -f "$E/logs/implementation.log"
fi
TREE=$(python3 "$DRV/claude_vc4.py" tree); echo "TREE=$TREE"
echo "=== 实现测试收据"; if [ -f "$E/receipt.json" ]; then echo "receipt 已存在，跳过 facts/finalize"; else bash "$DRV/vc4-facts.sh" "$E" "$TREE" 2>&1 | tail -n 2; fi; test -f "$E/receipt.json"
python3 -m tools.official_client_capture.codex_upgrade_vc_receipt replay --evidence-root "$E" --receipt receipt.json > "$RUNROOT/vc4-replay.json"; python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("replay:", d.get("kind"), d.get("status"))' "$RUNROOT/vc4-replay.json"
python3 "$DRV/vc4_resume.py" check --evidence-root "$E" --mode full || exit 3
echo "=== 批次 plan-candidate-gates：计划 + 预演 + 派发"
python3 "$DRV/claude_vc4.py" plan-gates --output "$W/action-plan-vc4-plan-gates.json"
chmod 600 "$W"/*.json
rm -f "$B/plan-source/$LIFECYCLE_DIR/gate-plan-rehearsal.json" "$B/plan-source/$LIFECYCLE_DIR/gate-plan-dispatch.json"
python3 "$DRV/claude_vc4.py" plan-rehearsal > "$RUNROOT/vc4-plan-rehearsal.out" 2>&1 || { tail -c 800 "$RUNROOT/vc4-plan-rehearsal.out"; exit 3; }
cat "$RUNROOT/vc4-plan-rehearsal.out"; test ! -e "$B/plan-source/$LIFECYCLE_DIR/gate-plan-rehearsal.json"
SEQ=$(next_seq); echo "批次序号=$SEQ"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-4 "$SEQ" VC-3 action-plan-vc4-plan-gates.json | grep -v "^$"
cmp "$B/plan-source/$LIFECYCLE_DIR/gate-plan-dispatch.json" "$B/source/$LIFECYCLE_DIR/gate-plan.json" && echo "派发产物 plan 与提交内 gate-plan.json 逐字一致"
echo "=== 批次 record-candidate-build：计划 + 零写入预览 + 派发"
python3 "$DRV/claude_vc4.py" record-plan --evidence-root "$E" --output "$W/action-plan-vc4-record-build.json"; chmod 600 "$W"/*.json
python3 "$DRV/claude_vc4.py" record-preview --plan "$W/action-plan-vc4-record-build.json" > "$RUNROOT/vc4-record-rehearsal.out" 2>&1 || { tail -c 800 "$RUNROOT/vc4-record-rehearsal.out"; exit 3; }
tail -n 2 "$RUNROOT/vc4-record-rehearsal.out" | cut -c1-400
SEQ=$(next_seq); echo "批次序号=$SEQ"
bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-4 "$SEQ" VC-3 action-plan-vc4-record-build.json | grep -v "^$"
python3 "$DRV/claude_vc4.py" summary
echo "TREE=$TREE E=$E"; echo "VC4_DONE"
