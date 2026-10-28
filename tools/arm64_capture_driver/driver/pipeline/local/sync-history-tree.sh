#!/bin/bash
# 本机侧一条命令跟进采集主机的历史测试树（建增量 bundle → 上传 → 采集主机上检出并按需装前端依赖）。
# 用法：bash sync-history-tree.sh <本机分支名>
# 增量基点 = 采集主机测试树当前 HEAD（本机必须有该提交）；bundle 只含基点之后的对象（通常不到 1 MB）。
# 采集主机上跑的是随驱动安装的 history-tree-refresh.sh；一轮的第一次同步若驱动还是上一版（没有该脚本），先把脚本传到 bootstrap 目录再跑。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
REPO=${PIPE_LOCAL_REPO}
BR=${1:?"用法：sync-history-tree.sh <本机分支名>"}
C=$(git -C "${REPO}" rev-parse --verify "refs/heads/${BR}^{commit}")
HT=$(pl_remote_var HISTORY_TEST_TREE)
[ -n "${HT}" ] || { echo "取不到参数文件的 HISTORY_TEST_TREE"; exit 2; }
BASE=$(ssh -n "${PIPE_SSH_HOST}" "git -C ${HT} rev-parse HEAD")
[[ ${BASE} =~ ^[0-9a-f]{40}$ ]]
echo "分支 ${BR} 尖=${C}；采集主机测试树当前=${BASE}"
if ssh -n "${PIPE_SSH_HOST}" "test -f ${REMOTE_PIPE}/history-tree-refresh.sh"; then
  CMD="ARM64_VC_ENV=${ARM64_VC_ENV} bash ${REMOTE_PIPE}/history-tree-refresh.sh"
else
  pl_push_bootstrap "${PIPE_HOME}/history-tree-refresh.sh"
  CMD="ARM64_VC_ENV=${ARM64_VC_ENV} PIPE_DRIVER_DIR=${REMOTE_DRIVER} bash ${REMOTE_PREP}/bootstrap/history-tree-refresh.sh"
fi
if [ "${BASE}" = "${C}" ]; then
  echo "测试树已在该提交，只核对前端依赖"
  pl_ssh "${CMD} ${C} - refs/heads/${BR}"
  exit 0
fi
git -C "${REPO}" cat-file -e "${BASE}^{commit}" || { echo "本机没有采集主机测试树当前提交 ${BASE}，无法建增量 bundle（改用完整 bundle：git bundle create … ${BR}）"; exit 2; }
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ap-hist-${C:0:12}.XXXXXX")
git -C "${REPO}" bundle create "${WORK}/h.bundle" "${BASE}..refs/heads/${BR}" 2>&1 | tail -1
ssh -n "${PIPE_SSH_HOST}" "install -d -m 700 ${REMOTE_PREP}/stage"
scp -q "${WORK}/h.bundle" "${PIPE_SSH_HOST}:${REMOTE_PREP}/stage/h-${C:0:12}.bundle"
pl_ssh "${CMD} ${C} ${REMOTE_PREP}/stage/h-${C:0:12}.bundle refs/heads/${BR}"
