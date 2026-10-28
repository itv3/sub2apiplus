#!/bin/bash
# VC-6 本机两步（在开发机上执行；取值一律从采集主机上的本轮参数文件现取，见 pipeline-lib.sh 的 pl_local_init）。
# 操作的是本机“候选链工作树”：local-candidate-chain.sh 用的 REPO（分支 = env 的 BUNDLE_BRANCH，头 = env 的 DC）。
#
#   bash vc6-local-commit.sh check   只读自检：解析 env、找候选链工作树、核对分支头与工作树状态，什么都不改（建议 VC-6 前先跑一次）
#   bash vc6-local-commit.sh sync    流水线 ORDER 的 handoff local-sync：vc6-gates.sh 之后，取 ARM64 production tree 的晋升差异
#                                    （只限 Catalog／contract），逐文件核摘要后提交“同步 production tree”，打增量 bundle 上传为
#                                    $RUNROOT/vc6/production-tree.bundle，写 $RUNROOT/vc6/vc6-pc.txt，最后创建交接标记
#                                    $RUNROOT/handoff/local-sync.done（流水线据此继续）
#   bash vc6-local-commit.sh retire  流水线 ORDER 的 handoff local-retire：激活登记（vc6-canonical-activation.sh）之后，在同步提交上
#                                    跑 retire-apply.py 裁剪退休版本（env 的 RETIRE_VERSION）并提交，打增量 bundle 上传为
#                                    $RUNROOT/vc6/retire-tree.bundle，写 $RUNROOT/vc6/vc6-retire-pc.txt，创建 $RUNROOT/handoff/local-retire.done
#
# 环境变量（可选）：
#   VC6_REPO          候选链工作树路径；缺省自动找（主仓库的 worktree 里检出了 BUNDLE_BRANCH 的那个）
#   VC6_LOCAL_TMP     本机放 bundle 的目录，缺省 ${TMPDIR:-/tmp}/vc6-run
#   VC6_COAUTHOR      提交说明末尾的 Co-Authored-By 行，缺省为空（不加）
# 冻结承接收据按先例留到收尾合并后统一登记，这里不生成（源码与收据必须分开提交）。
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
MODE=${1:?check|sync|retire}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "${HERE}/../pipeline-lib.sh"
pl_local_init
# ---- 本轮参数：在采集主机上用驱动自带的 parse_env.py 解析（只接受精确键集合，不执行命令），输出 export 语句，本机 eval
eval "${REMOTE_VARS}"
ARM64=${PIPE_SSH_HOST}
RR=${RUNROOT}; V6=${RR}/vc6
P=${D}/candidates/${CAND}/production-tree          # 采集主机上的 production tree（vc6-promote.sh 建）
BRANCH=${BUNDLE_BRANCH}
COAUTHOR=${VC6_COAUTHOR:-}
TMP=${VC6_LOCAL_TMP:-${TMPDIR:-/tmp}/vc6-run}
mkdir -p "${TMP}"
# ---- 找候选链工作树
find_wt() {
  if [ -n "${VC6_REPO:-}" ]; then echo "${VC6_REPO}"; return; fi
  local main=${PIPE_LOCAL_REPO} wt=""
  wt=$(git -C "${main}" worktree list --porcelain | awk -v b="refs/heads/${BRANCH}" '/^worktree /{p=substr($0,10)} $1=="branch" && $2==b{print p}' | head -1)
  [ -n "${wt}" ] && echo "${wt}"
}
WT=$(find_wt || true)
if [ "${MODE}" = check ]; then
  # check 模式只报告，不因前置未满足而退出（VC-4 之前这些前置本来就不满足）
  echo "env：C=${C:0:9} DC=${DC:0:9} ENTRY_COMMIT=${ENTRY_COMMIT:0:9} 目标=${TARGET_VERSION} 基线=${BASELINE_VERSION} 退休=${RETIRE_VERSION} 标记前缀=${TARGET_TAG} 分支=${BRANCH}"
  [ "${DC}" != "${ENTRY_COMMIT}" ] && echo "DC 已是候选链值" || echo "（提示）env 的 DC 仍等于入口提交，VC-4 之后才会更新"
  if [ -n "${WT}" ] && [ -d "${WT}" ]; then
    echo "候选链工作树：${WT}  当前分支：$(git -C "${WT}" rev-parse --abbrev-ref HEAD)  头：$(git -C "${WT}" rev-parse HEAD)"
    [ "$(git -C "${WT}" rev-parse HEAD)" = "${DC}" ] && echo "头 = DC：可以做 sync" || echo "头 ≠ DC：sync 已做过，或工作树不对"
    [ -z "$(git -C "${WT}" status --porcelain --untracked-files=all)" ] && echo "工作树干净" || echo "（警告）工作树不干净"
  else
    echo "（提示）还没有检出 ${BRANCH} 的候选链工作树（VC-3 之后本机建链才有；也可用 VC6_REPO 指定）"
  fi
  ssh -n "${ARM64}" "test -d ${P} && echo 'ARM64 production tree 已存在' || echo 'ARM64 production tree 尚不存在（vc6-promote.sh 之后才有）'"
  ssh -n "${ARM64}" "test -f ${V6}/vc6-pc.txt && echo vc6-pc.txt=\$(cat ${V6}/vc6-pc.txt) || echo 'vc6-pc.txt 尚不存在（sync 之后才有）'"
  ssh -n "${ARM64}" "test -f ${RR}/vc6/ORDER.txt -o -f ${REMOTE_PREP}/vc6/ORDER.txt && echo 'ORDER.txt 在位' || echo '缺 ORDER.txt'"
  echo "VC6_LOCAL_CHECK_DONE"; exit 0
fi
[ -n "${WT}" ] && [ -d "${WT}" ] || { echo "找不到检出了 ${BRANCH} 的候选链工作树（可用 VC6_REPO 指定）"; exit 3; }
cd "${WT}"
test "$(git rev-parse --abbrev-ref HEAD)" = "${BRANCH}" || { echo "工作树 ${WT} 不在 ${BRANCH}"; exit 3; }
test -z "$(git status --porcelain --untracked-files=all)" || { echo "工作树 ${WT} 不干净"; exit 3; }
[ "${DC}" != "${ENTRY_COMMIT}" ] || { echo "env 的 DC 仍等于入口提交（VC-4 候选链尚未写入 env）"; exit 3; }

mark_done() { ssh -n "${ARM64}" "install -d -m 700 ${RR}/handoff && : > ${RR}/handoff/$1.done && ls -l ${RR}/handoff/$1.done"; }

case "${MODE}" in
sync)
  test "$(git rev-parse HEAD)" = "${DC}" || { echo "候选分支头不是 ${DC:0:9}，sync 已做过？"; exit 3; }
  LIST=$(ssh -n "${ARM64}" "git -C ${P} status --porcelain --untracked-files=all" | sed 's/^...//')
  test -n "${LIST}" || { echo "ARM64 production tree 没有晋升差异"; exit 3; }
  echo "晋升差异："; echo "${LIST}"
  echo "${LIST}" | while read -r f; do
    case "${f}" in backend/internal/officialegress/catalogdata/runtime/*|backend/internal/officialegress/releasecontract/testdata/*) ;; *) echo "清单外路径：${f}"; exit 3;; esac
    mkdir -p "$(dirname "${f}")"
    ssh -n "${ARM64}" "cat ${P}/${f}" > "${f}"
    LSHA=$(shasum -a 256 "${f}" | cut -c1-64); RSHA=$(ssh -n "${ARM64}" "sha256sum ${P}/${f} | cut -c1-64")
    test "${LSHA}" = "${RSHA}" || { echo "摘要不一致：${f}"; exit 3; }
    echo "  ${LSHA:0:12} ${f}"
  done
  git add -A backend/internal/officialegress
  git commit -q -m "feat(${TARGET_TAG}): 同步 production tree（Catalog 晋升为 Active=${TARGET_VERSION} / Previous=${BASELINE_VERSION}）" ${COAUTHOR:+-m "${COAUTHOR}"}
  PC=$(git rev-parse HEAD); git show --stat --format='%H %s' HEAD | head -8
  BUNDLE_FILE=${TMP}/production-tree.bundle; rm -f "${BUNDLE_FILE}"
  git bundle create "${BUNDLE_FILE}" "${DC}..${BRANCH}" > /dev/null 2>&1; git bundle verify "${BUNDLE_FILE}" > /dev/null 2>&1 || true
  scp -q "${BUNDLE_FILE}" "${ARM64}:${V6}/production-tree.bundle"
  test "$(shasum -a 256 "${BUNDLE_FILE}" | cut -c1-64)" = "$(ssh -n "${ARM64}" "sha256sum ${V6}/production-tree.bundle | cut -c1-64")"
  ssh -n "${ARM64}" "echo ${PC} > ${V6}/vc6-pc.txt; chmod 600 ${V6}/vc6-pc.txt ${V6}/production-tree.bundle; cat ${V6}/vc6-pc.txt"
  # 远端复核：同步提交的树 = production tree 工作区（差异文件逐字相同）。临时目录用固定字面路径，用完按字面路径删
  CHK=${V6}/pc-check
  ssh -n "${ARM64}" "set -e; rm -rf ${CHK}; mkdir ${CHK}; git clone -q --no-checkout ${P} ${CHK}/r; git -C ${CHK}/r fetch -q ${V6}/production-tree.bundle refs/heads/${BRANCH}:refs/heads/pc-check; git -C ${CHK}/r checkout -q --detach ${PC}; for f in ${LIST//$'\n'/ }; do cmp -s ${CHK}/r/\$f ${P}/\$f || { echo 不一致 \$f; exit 3; }; done; rm -rf ${CHK}; echo 远端复核一致"
  mark_done local-sync
  echo "VC6_SYNC_DONE PC=${PC}" ;;
retire)
  PC=$(ssh -n "${ARM64}" "cat ${V6}/vc6-pc.txt")
  test "$(git rev-parse HEAD)" = "${PC}" || { echo "候选分支头不是同步提交 ${PC:0:9}"; exit 3; }
  python3 -B "${HERE}/retire-apply.py" backend/internal/officialegress "${NEW}" "${RETIRE_VERSION}" --dry-run
  python3 -B "${HERE}/retire-apply.py" backend/internal/officialegress "${NEW}" "${RETIRE_VERSION}" | tail -1
  git status --porcelain --untracked-files=all
  git add -A backend/internal/officialegress
  git commit -q -m "feat(${TARGET_TAG}): 退休 ${RETIRE_VERSION} 运行画像（Catalog 裁剪 + selector 切换，画像字节按冻结合同原地保留）" ${COAUTHOR:+-m "${COAUTHOR}"}
  RPC=$(git rev-parse HEAD); git show --stat --format='%H %s' HEAD | head -8
  BUNDLE_FILE=${TMP}/retire-tree.bundle; rm -f "${BUNDLE_FILE}"
  git bundle create "${BUNDLE_FILE}" "${PC}..${BRANCH}" > /dev/null 2>&1
  scp -q "${BUNDLE_FILE}" "${ARM64}:${V6}/retire-tree.bundle"
  test "$(shasum -a 256 "${BUNDLE_FILE}" | cut -c1-64)" = "$(ssh -n "${ARM64}" "sha256sum ${V6}/retire-tree.bundle | cut -c1-64")"
  ssh -n "${ARM64}" "echo ${RPC} > ${V6}/vc6-retire-pc.txt; chmod 600 ${V6}/vc6-retire-pc.txt ${V6}/retire-tree.bundle; cat ${V6}/vc6-retire-pc.txt"
  mark_done local-retire
  echo "VC6_RETIRE_COMMIT_DONE RPC=${RPC}" ;;
*) echo "MODE 只能是 check|sync|retire"; exit 2 ;;
esac
