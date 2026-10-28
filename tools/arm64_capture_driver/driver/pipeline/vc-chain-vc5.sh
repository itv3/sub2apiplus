#!/bin/bash
# 调度者串联（采集主机，<驱动目录>/pipeline/vc-chain-vc5.sh）：VC-5 恢复链派出的候选采集批次跑完（vc5-run-batch.out 出现
# RUN_BATCH_DONE）后，立即从 VC5 起流水线到 VC6。流水线 VC5 段会重新准入（批次在跑时准入判 supervisor_run_active，只能等它结束）、
# 开采集账号（已开则幂等）、起 vc5-all；vc5-all 看到 vc5-run-batch.out 已存在就不再派批次，直接接着做 seal → accept → canonical。
# 批次失败（进程消失且无 RUN_BATCH_DONE）则只记录、不起流水线，等调度者处置。
# 用法（ARM64 root）：
#   ARM64_VC_ENV=<本轮参数文件> setsid -f bash <驱动目录>/pipeline/vc-chain-vc5.sh '<批准文本>' > <PREP>/pipeline/vc-chain-vc5.out 2>&1 < /dev/null
set -uo pipefail
APPROVER=${1:?批准文本}
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
P=${PREP}
OUT=${RR}/vc5-run-batch.out
say() { echo "$(date -u +%FT%TZ) $*"; }
say "等候选采集批次结束（${OUT}）"
miss=0
while true; do
  if grep -q '^RUN_BATCH_DONE ' "${OUT}" 2>/dev/null; then break; fi
  if pgrep -f '[v]c5-run-batch.sh' >/dev/null 2>&1; then miss=0; else miss=$((miss + 1)); fi
  if [ "${miss}" -ge 3 ]; then
    grep -q '^RUN_BATCH_DONE ' "${OUT}" 2>/dev/null && break
    say "候选采集批次进程已消失且无 RUN_BATCH_DONE，不起流水线：$(tail -3 "${OUT}" | tr '\n' ' ' | cut -c1-300)"
    exit 1
  fi
  sleep 30
done
say "批次完成：$(grep '^RUN_BATCH_DONE ' "${OUT}" | tail -1)；从 VC5 起流水线到 VC6"
bash "${PIPE_HOME}/vc-pipeline.sh" start --from VC5 --to VC6 --approver "${APPROVER}" --wait-handoff 60 || { say "流水线起跑自检不通过"; exit 1; }
say "流水线 VC5→VC6 已起"
