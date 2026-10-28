#!/bin/bash
# 本机：VC-4 候选链交接（local-candidate-chain.sh 产出 bundle 之后）。把 bundle 上传到采集主机本轮参数文件的 $BUNDLE，并写流水线读的交接文件。
# 用法：bash vc4-handoff.sh <本机 bundle 路径> [<DC 完整提交，可选，写进交接文件供流水线交叉核对>]
# 流水线（vc-pipeline.sh 的 VC4 阶段）会自己从 bundle 推出 C／DC／RECEIPT 写进参数文件，并核对“DC = C + 恰好一个 docs/egress/maintenance/*.json”，
# 交接文件里的分支必须等于参数文件的 BUNDLE_BRANCH，所以这里从采集主机的参数文件现取分支名，不写死。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
LOCAL=${1:?本机 bundle 路径}; DC=${2:-}
[ -z "${DC}" ] || [[ ${DC} =~ ^[0-9a-f]{40}$ ]] || { echo "DC 必须是 40 位小写十六进制（git rev-parse 现取）"; exit 2; }
REMOTE=$(pl_remote_var BUNDLE)
BRANCH=$(pl_remote_var BUNDLE_BRANCH)
[ -n "${REMOTE}" ] && [ -n "${BRANCH}" ] || { echo "取不到参数文件的 BUNDLE／BUNDLE_BRANCH"; exit 2; }
SHA=$(shasum -a 256 "${LOCAL}" | cut -d' ' -f1)
scp -q "${LOCAL}" "${PIPE_SSH_HOST}:${REMOTE}.part"
if [ -n "${DC}" ]; then DCJ="\"${DC}\""; else DCJ=null; fi
ssh -n "${PIPE_SSH_HOST}" "set -e; test \"\$(sha256sum ${REMOTE}.part | cut -c1-64)\" = ${SHA}; chmod 600 ${REMOTE}.part; mv ${REMOTE}.part ${REMOTE}; install -d -m 700 ${REMOTE_RUNROOT}/handoff; printf '{\"bundle_sha256\":\"%s\",\"branch\":\"%s\",\"dc\":%s}\n' ${SHA} '${BRANCH}' '${DCJ}' > ${REMOTE_RUNROOT}/handoff/vc4-chain.json; chmod 600 ${REMOTE_RUNROOT}/handoff/vc4-chain.json; cat ${REMOTE_RUNROOT}/handoff/vc4-chain.json"
echo VC4_HANDOFF_DONE
