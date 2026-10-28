#!/bin/bash
# 为入口（entry.sh／入口空跑／后台验证）建完整 bundle 并上传，打印要写进参数文件的 ENTRY_* 三行与填写命令（本机运行）。
# 用法：bash upload-entry-bundle.sh <本机分支名> [--dry-run]
# 为什么是完整 bundle（约 120 MB）：入口门禁用它在隔离目录里重建源码，需要含全部历史（每换一次提交就留一个，累计会占 GB 级——换提交前先确认旧的没有被引用再清）。
# 先 bundle verify（本机与采集主机各一次），再上传到本轮 RUNROOT（0600）。不改参数文件，由调度者按打印的命令填。
# 文件名里的轮次标签取 RUNROOT 目录名，提交前 9 位区分不同提交。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../pipeline-lib.sh"
pl_local_init
REPO=${PIPE_LOCAL_REPO}
BR=${1:?"用法：upload-entry-bundle.sh <本机分支名> [--dry-run]"}
DRY=${2:-}
C=$(git -C "${REPO}" rev-parse --verify "refs/heads/${BR}^{commit}")
NAME=$(basename "${REMOTE_RUNROOT}")-deploy-${C:0:9}.bundle
HT=$(pl_remote_var HISTORY_TEST_TREE)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ap-entry-${C:0:9}.XXXXXX")
git -C "${REPO}" bundle create "${WORK}/${NAME}" "refs/heads/${BR}" 2>&1 | tail -1
git -C "${REPO}" bundle verify "${WORK}/${NAME}" | tail -1
echo "bundle $(du -h "${WORK}/${NAME}" | cut -f1)  分支=${BR}  提交=${C}"
if [ "${DRY}" != "--dry-run" ]; then
  scp -q "${WORK}/${NAME}" "${PIPE_SSH_HOST}:${REMOTE_RUNROOT}/${NAME}"
  ssh -n "${PIPE_SSH_HOST}" "chmod 600 ${REMOTE_RUNROOT}/${NAME} && git -C ${HT} bundle verify ${REMOTE_RUNROOT}/${NAME} | tail -1; sha256sum ${REMOTE_RUNROOT}/${NAME}"
  shasum -a 256 "${WORK}/${NAME}"
fi
cat <<MSG

# 写进参数文件（在采集主机上执行）：
bash ${REMOTE_PIPE}/set-env-keys.sh ${ARM64_VC_ENV} \\
  ENTRY_BUNDLE=${REMOTE_RUNROOT}/${NAME} ENTRY_BRANCH=${BR} ENTRY_COMMIT=${C}
MSG
