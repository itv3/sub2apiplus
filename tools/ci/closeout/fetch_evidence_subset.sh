#!/bin/bash
# 收尾 b7：把终态事实生成器需要的 ARM64 证据小文件（共 9 个）只读取回本机，保持证据根里的相对布局，供 gen_terminal_facts.py 在本机运行。
#
# 做什么：只读 ARM64（tar 打包后在本机解包），不改任何远端文件。
# 输入：
#   bash fetch_evidence_subset.sh <本机目标目录> <Campaign 全名> <候选 id> <ARM64 轮次目录>
#   四个参数都必填，没有缺省值。环境变量：ARM64_HOST（ssh 主机别名，缺省 ARM64）、DATA_ROOT（ARM64 证据根，缺省 /root/docker/capture-cli/data）。
# 输出：目标目录下得到 data/（证据根子集）与 run/（轮次目录里 vc6 的 5 个小文件）；末行 FETCH_DONE <文件数> 个文件，最后检查点 <编号>。
#   gen_terminal_facts.py 的参数：--evidence-root <目标目录>/data --evidence-root-label <DATA_ROOT> --run-vc6 <目标目录>/run/vc6
# 何时用：closeout.sh 的 b7-terminal-facts 步骤之内。
set -Eeuo pipefail
DEST=${1:?本机目标目录}
CAMPAIGN=${2:?Campaign 全名}
CAND=${3:?候选 id}
RUNROUND=${4:?ARM64 轮次目录}
HOST=${ARM64_HOST:-ARM64}
D=${DATA_ROOT:-/root/docker/capture-cli/data}
C=evidence/campaigns/${CAMPAIGN}
mkdir -p "${DEST}/data" "${DEST}/run"
VC6_ROOT=$(ssh -n "${HOST}" "cat ${RUNROUND}/vc6/vc6-root.txt"); RETIRE_ROOT=$(ssh -n "${HOST}" "cat ${RUNROUND}/vc6/vc6-retire-root.txt")
VC6_REL=${VC6_ROOT#${D}/}; RETIRE_REL=${RETIRE_ROOT#${D}/}
LAST=$(ssh -n "${HOST}" "ls ${D}/${C}/canonical/checkpoints | sort | tail -1")
ssh -n "${HOST}" "cd ${D} && tar -cf - ${C}/campaign.json ${C}/campaign.sha256 ${C}/canonical/checkpoints/${LAST} \
  ${C}/control/vc/receipts/${CAND}/vc6-completion.json ${C}/control/vc/receipts/${CAND}/cleanup-decision.json \
  ${C}/delivery/${CAND}/receipt.json ${VC6_REL}/activation-receipt.json ${RETIRE_REL}/stages/retire_switch/stage.json" | tar -xf - -C "${DEST}/data"
ssh -n "${HOST}" "cd ${RUNROUND} && tar -cf - vc6/vc6-root.txt vc6/vc6-retire-root.txt vc6/vc6-pc.txt vc6/vc6-retire-pc.txt vc6/vc6-tree.txt" | tar -xf - -C "${DEST}/run"
echo "FETCH_DONE $(find "${DEST}" -type f | wc -l | tr -d ' ') 个文件，最后检查点 ${LAST}"
