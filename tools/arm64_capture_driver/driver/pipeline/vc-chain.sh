#!/bin/bash
# 调度者串联（采集主机，<驱动目录>/pipeline/vc-chain.sh）：VC4 段跑完且通过后，自动从 VC5 起跑到 VC6。
# 原因：入口提交若在 BGV 之前已有 passed 的后台验证结论，流水线跑到 BGV 段会因“同提交已有结论不会重起”而停，需要人再从 VC5 续起；
# 这里把 VC4 单独跑（--to VC4），通过后直接起 VC5→VC6，省掉中间等人的空档。VC4 未通过则什么都不续起。
# 用法（ARM64 root）：
#   ARM64_VC_ENV=<本轮参数文件> setsid -f bash <驱动目录>/pipeline/vc-chain.sh '<批准文本>' > <PREP>/pipeline/vc-chain.out 2>&1 < /dev/null
set -uo pipefail
APPROVER=${1:?批准文本}
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
P=${PREP}
S=${P}/pipeline/vc-status.txt
n0=$(wc -l < "${S}")
echo "$(date -u +%FT%TZ) 起 VC4（状态文件起点行 ${n0}）"
bash "${PIPE_HOME}/vc-pipeline.sh" start --from VC4 --to VC4 --approver "${APPROVER}" --wait-handoff 60 || { echo "$(date -u +%FT%TZ) VC4 起跑自检不通过，不续起"; exit 1; }
# 只看本次起跑之后追加的行；流水线最后一行必为 EXIT rc=N
until tail -n +"$((n0 + 1))" "${S}" | grep -q ' EXIT rc='; do sleep 30; done
if tail -n +"$((n0 + 1))" "${S}" | grep -q ' PASS VC4$'; then
  echo "$(date -u +%FT%TZ) VC4 通过，从 VC5 起跑到 VC6"
  bash "${PIPE_HOME}/vc-pipeline.sh" start --from VC5 --to VC6 --approver "${APPROVER}" --wait-handoff 60 || { echo "$(date -u +%FT%TZ) VC5 起跑自检不通过"; exit 1; }
  echo "$(date -u +%FT%TZ) VC5→VC6 已起"
else
  echo "$(date -u +%FT%TZ) VC4 未通过，不续起"
  exit 1
fi
