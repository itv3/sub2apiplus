#!/bin/bash
# 本机侧一条命令跟进采集主机的历史测试树（建增量 bundle → 上传 → 采集主机上检出并按需装前端依赖）。
# 用法：bash sync-history-tree.sh <本机分支名>
# 增量基点 = 采集主机测试树当前 HEAD（本机必须有该提交）；bundle 只含基点之后的对象（通常不到 1 MB）。
# 发版 tag（Codex 升级工具优化第 2 项 S1）：采集主机侧取 bundle 用 --no-tags，历史树不带 tag（ARM64 的树最高只到 v0.2.13-3），入口门禁与后台验证的
#   “最近发版 tag”因此解析错，Codex 按客户端筛选必回退全集。所以本脚本在本机用 git describe --tags --abbrev=0 --match 'v[0-9]*' 取分支上最近的发版 tag
#   （形如 v0.2.18-6）及其提交，作为第 4、5 个参数交给 history-tree-refresh.sh，让它在检出之后补这一个本地 tag。本机分支上找不到发版 tag 时不传，
#   只打一行警告（采集主机侧也会警告），同步照旧。
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
# 本机分支上最近的发版 tag：名字必须是发版格式（v0.2.18、v0.2.18-6），提交取 tag 指向的提交（^{commit}，带注解的 tag 也能解到提交）。
TAG_ARGS=""
TAG=$(git -C "${REPO}" describe --tags --abbrev=0 --match 'v[0-9]*' "${C}" 2>/dev/null || true)
if [[ ${TAG} =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?$ ]] && TAG_COMMIT=$(git -C "${REPO}" rev-parse --verify "refs/tags/${TAG}^{commit}" 2>/dev/null) \
    && [[ ${TAG_COMMIT} =~ ^[0-9a-f]{40}$ ]]; then
  TAG_ARGS=" ${TAG} ${TAG_COMMIT}"
  echo "发版 tag ${TAG} → ${TAG_COMMIT}（随同步补进采集主机的历史测试树）"
else
  echo "警告：本机分支 ${BR} 上找不到发版 tag（最近的 tag：${TAG:-无}），历史测试树不会补 tag，Codex 按客户端筛选会回退全集；先在本机补齐发版 tag（git fetch --tags）再同步"
fi
if ssh -n "${PIPE_SSH_HOST}" "test -f ${REMOTE_PIPE}/history-tree-refresh.sh"; then
  CMD="ARM64_VC_ENV=${ARM64_VC_ENV} bash ${REMOTE_PIPE}/history-tree-refresh.sh"
else
  pl_push_bootstrap "${PIPE_HOME}/history-tree-refresh.sh"
  CMD="ARM64_VC_ENV=${ARM64_VC_ENV} PIPE_DRIVER_DIR=${REMOTE_DRIVER} bash ${REMOTE_PREP}/bootstrap/history-tree-refresh.sh"
fi
if [ "${BASE}" = "${C}" ]; then
  echo "测试树已在该提交，只核对前端依赖"
  pl_ssh "${CMD} ${C} - refs/heads/${BR}${TAG_ARGS}"
  exit 0
fi
git -C "${REPO}" cat-file -e "${BASE}^{commit}" || { echo "本机没有采集主机测试树当前提交 ${BASE}，无法建增量 bundle（改用完整 bundle：git bundle create … ${BR}）"; exit 2; }
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ap-hist-${C:0:12}.XXXXXX")
git -C "${REPO}" bundle create "${WORK}/h.bundle" "${BASE}..refs/heads/${BR}" 2>&1 | tail -1
ssh -n "${PIPE_SSH_HOST}" "install -d -m 700 ${REMOTE_PREP}/stage"
scp -q "${WORK}/h.bundle" "${PIPE_SSH_HOST}:${REMOTE_PREP}/stage/h-${C:0:12}.bundle"
pl_ssh "${CMD} ${C} ${REMOTE_PREP}/stage/h-${C:0:12}.bundle refs/heads/${BR}${TAG_ARGS}"
