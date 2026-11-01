#!/bin/bash
# 把“历史测试树”跟进到集成分支提交（采集主机侧脚本，由本机 local/sync-history-tree.sh 上传后调用）。
# 历史测试树 = 本轮参数文件的 HISTORY_TEST_TREE：后台验证、入口门禁都在它上面检出被测提交跑单元。
# 用法：bash history-tree-refresh.sh <40 位提交> <bundle 绝对路径，或 - 表示树里已有该提交> <bundle 内分支引用，如 refs/heads/<集成分支>>
#         [<发版 tag 名> <发版 tag 指向的 40 位提交>]
#   FORCE_INSTALL=1 强制重装前端依赖（缺省只在锁文件与已装依赖不一致时才装）
#   第 4、5 个参数（可选，必须成对；本机 sync-history-tree.sh 会自动带上）：集成分支上最近的发版 tag（形如 v0.2.18-6）及其提交。
#   为什么：下面 fetch 带 --no-tags，历史树从来不带 tag（ARM64 的树最高只到 v0.2.13-3），入口门禁与后台验证的“最近发版 tag”因此解析成差 5 个版本的旧 tag，
#   改动文件上千个，Codex 按客户端筛选必回退全集（Codex 升级工具优化第 2 项 S1）。给了这对参数，检出之后补一个本地 tag（git tag -f，只补这一个 tag，
#   不拉远端 tag）；不给时行为与改造前逐字相同，只多一行警告。
#   补 tag 之前先校验：tag 名形如 v0.2.18 或 v0.2.18-6、提交是 40 位、提交在树里存在、而且是目标提交的祖先（非祖先的 tag 会让“最近发版 tag”解析错，一律拒绝，
#   树不动）。
# 步骤：bundle verify → fetch 对象（不建分支）→ 校验取到的就是目标提交 → （给了 tag：校验 tag 提交存在且是祖先）→ checkout --detach → （给了 tag：补 tag）→
#       比对 frontend/pnpm-lock.yaml 与 node_modules/.pnpm/lock.yaml → 不一致时 docker node:20-slim 里
#       corepack pnpm + pnpm install --frozen-lockfile（pnpm store 已缓存时约 2 秒）。
# 不删除任何文件。树里本来就有的未跟踪目录 frontend/.pnpm-store/ 是 pnpm 缓存，属正常。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"

pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
pl_defaults
umask 022
C=${1:?"用法：history-tree-refresh.sh <40位提交> <bundle|-> <分支引用>"}
BUNDLE=${2:?"缺 bundle"}
REF=${3:?"缺 bundle 内分支引用"}
TAG=${4:-}; TAG_COMMIT=${5:-}
[[ ${C} =~ ^[0-9a-f]{40}$ ]] || { echo "提交必须是 40 位小写十六进制"; exit 2; }
# 发版 tag 参数先校验格式（退出 2，树还没动）：成对出现，名字只认发版格式，提交 40 位小写十六进制。
if [ -n "${TAG}${TAG_COMMIT}" ]; then
  [[ ${TAG} =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?$ ]] || { echo "发版 tag 名必须形如 v0.2.18 或 v0.2.18-6：${TAG:-（空）}（第 4、5 个参数必须成对给）"; exit 2; }
  [[ ${TAG_COMMIT} =~ ^[0-9a-f]{40}$ ]] || { echo "发版 tag 指向的提交必须是 40 位小写十六进制：${TAG_COMMIT:-（空）}（第 4、5 个参数必须成对给）"; exit 2; }
fi
if [ "${BUNDLE}" != "-" ]; then
  [[ ${BUNDLE} == /* ]] && [ -f "${BUNDLE}" ] && [ ! -L "${BUNDLE}" ] || { echo "bundle 必须是存在的普通文件（绝对路径）"; exit 2; }
fi
HT=${HISTORY_TEST_TREE}
LOG=${PREP}/logs/history-tree-${C:0:12}-$(date -u +%Y%m%dt%H%M%Sz).log
mkdir -p "${PREP}/logs"
exec > >(tee -a "${LOG}") 2>&1
echo "== 历史测试树跟进到 ${C}（日志 ${LOG}）"
if pgrep -af 'unit_executor|entry-gates|background_validation|entry_orchestrator|entry-dryrun' | grep -v pgrep >/dev/null; then
  echo "有在跑的验证，停止（检出会改动它正在用的测试树）"; exit 3
fi
echo "树当前 HEAD=$(git -C "${HT}" rev-parse HEAD)"
if [ "${BUNDLE}" != "-" ]; then
  git -C "${HT}" bundle verify "${BUNDLE}" | tail -2
  git -C "${HT}" fetch -q --no-tags "${BUNDLE}" "${REF}"
  [ "$(git -C "${HT}" rev-parse FETCH_HEAD)" = "${C}" ] || { echo "bundle 里的分支尖不是目标提交，停止"; exit 3; }
fi
git -C "${HT}" cat-file -e "${C}^{commit}" || { echo "树里没有目标提交，停止"; exit 3; }
if [ -n "${TAG}" ]; then
  # 在 checkout 之前校验：不合格就拒绝，树停在原处。非祖先的 tag 会让 git describe 解析出错的“最近发版 tag”，所以必须是目标提交的祖先。
  git -C "${HT}" cat-file -e "${TAG_COMMIT}^{commit}" 2>/dev/null || { echo "树里没有发版 tag ${TAG} 指向的提交 ${TAG_COMMIT}（本机 bundle 没带上？），停止"; exit 3; }
  git -C "${HT}" merge-base --is-ancestor "${TAG_COMMIT}" "${C}" \
    || { echo "发版 tag ${TAG} 指向的提交 ${TAG_COMMIT} 不是目标提交 ${C} 的祖先，拒绝补 tag（历史树不变）"; exit 3; }
fi
git -C "${HT}" checkout -q --detach "${C}"
[ "$(git -C "${HT}" rev-parse HEAD)" = "${C}" ]
if [ -n "${TAG}" ]; then
  OLD_TAG_COMMIT=$(git -C "${HT}" rev-parse -q --verify "refs/tags/${TAG}^{commit}" || true)
  git -C "${HT}" tag -f "${TAG}" "${TAG_COMMIT}" > /dev/null
  if [ -z "${OLD_TAG_COMMIT}" ]; then echo "已补发版 tag ${TAG} → ${TAG_COMMIT}"
  elif [ "${OLD_TAG_COMMIT}" = "${TAG_COMMIT}" ]; then echo "发版 tag ${TAG} 已在树里且指向同一提交 ${TAG_COMMIT}"
  else echo "发版 tag ${TAG} 原来指向 ${OLD_TAG_COMMIT}，已改为 ${TAG_COMMIT}"; fi
  echo "最近发版 tag（git describe --tags --abbrev=0 --match 'v[0-9]*' HEAD）：$(git -C "${HT}" describe --tags --abbrev=0 --match 'v[0-9]*' HEAD 2>&1)"
else
  echo "警告：历史测试树没有最新发版 tag（没给第 4、5 个参数）：入口门禁与后台验证会把“最近发版 tag”解析成树里已有的旧 tag，Codex 按客户端筛选会回退全集；用本机 local/sync-history-tree.sh 同步会自动带上"
fi
echo "已检出 ${C}；工作区改动（只应有 frontend/.pnpm-store/）："
git -C "${HT}" status --porcelain | head -5
if git -C "${HT}" show "${C}:frontend/pnpm-lock.yaml" | cmp -s - "${HT}/frontend/node_modules/.pnpm/lock.yaml" && [ "${FORCE_INSTALL:-0}" != 1 ]; then
  echo "锁文件与已装依赖一致，不重装前端依赖"
else
  echo "== 锁文件变了（或强制）：docker node:20-slim 内按锁文件装依赖"
  git -C "${HT}" show "${C}:frontend/pnpm-lock.yaml" | cmp - "${HT}/frontend/pnpm-lock.yaml"
  docker run --rm -v "${HT}/frontend:/work/frontend" -w /work/frontend -e CI=true node:20-slim \
    bash -c 'set -e; umask 022; corepack enable && corepack prepare pnpm@9.15.9 --activate; node --version; pnpm --version; pnpm install --frozen-lockfile 2>&1 | tail -8'
  git -C "${HT}" show "${C}:frontend/pnpm-lock.yaml" | cmp - "${HT}/frontend/node_modules/.pnpm/lock.yaml"
  echo "装完后锁文件与已装依赖一致"
fi
echo "node_modules=$(du -sh "${HT}/frontend/node_modules" | cut -f1)  __pycache__(树内，不含 node_modules)=$(find "${HT}" -name __pycache__ -not -path '*/node_modules/*' | wc -l | tr -d ' ')"
echo HISTORY_TREE_OK
