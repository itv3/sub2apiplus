#!/bin/bash
# 工具演进续跑：修复新头交回之后，本机一条命令走完“部署 → 历史测试树 → 入口 bundle → 参数文件五键 → 判定”，可选再接采集主机上的演进续跑。
# 本机运行（zsh/bash 均可）。配套采集主机侧的 evolve-and-resume.sh（适用场景见该脚本头注释）。
#
# 用法：
#   bash evo-local.sh <新头 40 位提交> --base <部署前基线提交>                                  # 部署＋同步＋参数文件＋采集主机 --check，到此为止
#   bash evo-local.sh <新头> --base <基线提交> --apply --approver '<批准人及依据>' [--reason '<登记原因>']
#                                                                                             # 上面全做完且 --check 无“不通过”，再起演进续跑
# 分支取 pipeline.env 的 PIPE_LOCAL_BRANCH，集成工作树取 PIPE_LOCAL_WORKTREE（缺省 PIPE_LOCAL_REPO）。
#
# 做什么（任一步失败即停，不回滚已完成的部署——部署有 rollback_backup，回滚走受监督部署）：
#   0. 前置：新头必须是该分支的尖、是 --base 的后代、集成工作树干净；采集主机上没有在跑的批次／后台验证／流水线；
#   1. upload-and-deploy.sh <新头> --verify-only  → 再真部署（受监督部署：受管工具＋驱动重装，约 2～5 分钟）；
#   2. sync-history-tree.sh <分支>   （历史测试树跟进到新头，BGV／VC-4 门禁用）；
#   3. upload-entry-bundle.sh <分支> （完整 bundle ≈125 MB，BGV 的 ENTRY_BUNDLE）；
#   4. 采集主机 set-env-keys.sh：ENTRY_BUNDLE／ENTRY_BRANCH／ENTRY_COMMIT 三键写新值，C／DC 仍用占位（取新头，VC-4 前换真值）；
#   5. 采集主机 evolve-and-resume.sh --check --head <新头>（只读判定）；
#   6. 带 --apply 时：evolve-and-resume.sh --apply（E0～E7，后台 setsid -f，状态写 vc-status.txt）。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=${PIPE_LOCAL_REPO}
WT=${PIPE_LOCAL_WORKTREE:-${REPO}}
BR=${PIPE_LOCAL_BRANCH:?"pipeline.env 里要给 PIPE_LOCAL_BRANCH（本机集成分支名）"}
HEAD_ARG=${1:?"用法：evo-local.sh <新头 40 位提交> --base <基线提交> [--apply --approver '…' --reason '…']"}; shift
APPLY=0; APPROVER=""; BASE=""; REASON=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --approver) APPROVER=${2:-}; shift ;;
    --base) BASE=${2:-}; shift ;;
    --reason) REASON=${2:-}; shift ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done
[[ ${HEAD_ARG} =~ ^[0-9a-f]{40}$ ]] || { echo "新头必须是 40 位小写十六进制（用 git rev-parse 现取，不手写）"; exit 2; }
[[ ${BASE} =~ ^[0-9a-f]{40}$ ]] || { echo "必须用 --base 给出部署前基线提交（40 位小写十六进制，git rev-parse 现取）"; exit 2; }
if [ "${APPLY}" = 1 ] && [ -z "${APPROVER}" ]; then echo "--apply 必须同时给 --approver"; exit 2; fi
H=$(git -C "${REPO}" rev-parse --verify "${HEAD_ARG}^{commit}")
TIP=$(git -C "${REPO}" rev-parse --verify "refs/heads/${BR}^{commit}")
[ "${H}" = "${TIP}" ] || { echo "新头 ${H:0:9} 不是 ${BR} 的尖 ${TIP:0:9}"; exit 2; }
git -C "${REPO}" merge-base --is-ancestor "${BASE}" "${H}" || { echo "新头不是基线 ${BASE:0:9} 的后代（部署基线不符）"; exit 2; }
[ -z "$(git -C "${WT}" status --porcelain)" ] || { echo "集成工作树不干净（修复还在改？）：${WT}"; exit 2; }
echo "== 0. 新头 ${H}：$(git -C "${REPO}" log -1 --format=%s "${H}")"
BUSY=$(ssh -n "${PIPE_SSH_HOST}" "pgrep -af '[c]ompile-and-run-vc-batch|[b]ackground_validation.py (start|run|reuse)|[v]c-pipeline.sh (start|--run)|[e]ntry_orchestrator.py|[a]rm64-vc4-gates.sh|[v]c[45]-all.sh|[e]volve-and-resume.sh --run' || true")
[ -z "${BUSY}" ] || { echo "采集主机上有批次／验证／流水线在跑，不部署："; echo "${BUSY}" | cut -c1-160; exit 3; }

echo "== 1. 受监督部署（先 --verify-only）"
bash "${HERE}/upload-and-deploy.sh" "${H}" --verify-only
bash "${HERE}/upload-and-deploy.sh" "${H}"
echo "== 2. 历史测试树跟进"
bash "${HERE}/sync-history-tree.sh" "${BR}"
echo "== 3. 入口 bundle 上传"
bash "${HERE}/upload-entry-bundle.sh" "${BR}"
B=${REMOTE_RUNROOT}/$(basename "${REMOTE_RUNROOT}")-deploy-${H:0:9}.bundle
echo "== 4. 采集主机参数文件写 ENTRY_* 三键与 C／DC 占位"
pl_ssh "bash ${REMOTE_PIPE}/set-env-keys.sh ${ARM64_VC_ENV} ENTRY_BUNDLE=${B} ENTRY_BRANCH=${BR} ENTRY_COMMIT=${H} C=${H} DC=${H}"
echo "== 5. 采集主机 evolve-and-resume.sh --check"
EVO_ENV="ARM64_VC_ENV=${ARM64_VC_ENV} EVO_BASE_COMMIT=${BASE}"
pl_ssh "${EVO_ENV} bash ${REMOTE_PIPE}/evolve-and-resume.sh --check --head ${H}" | tee /dev/stderr | grep -q '不通过 0' \
  || { echo "--check 有“不通过”项，不继续 --apply"; exit 4; }
if [ "${APPLY}" = 1 ]; then
  echo "== 6. 起演进续跑（后台）"
  REASON_ENV=""; [ -z "${REASON}" ] || REASON_ENV="EVO_REASON='${REASON}'"
  pl_ssh "${EVO_ENV} ${REASON_ENV} bash ${REMOTE_PIPE}/evolve-and-resume.sh --apply --head ${H} --approver '${APPROVER}'"
  echo "盯状态：ssh ${PIPE_SSH_HOST} 'tail -f ${REMOTE_PREP}/pipeline/vc-status.txt'（最后一行 EXIT rc=N 即结束）"
fi
