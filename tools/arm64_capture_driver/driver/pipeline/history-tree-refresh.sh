#!/bin/bash
# 把“历史测试树”跟进到集成分支提交（采集主机侧脚本，由本机 local/sync-history-tree.sh 上传后调用）。
# 历史测试树 = 本轮参数文件的 HISTORY_TEST_TREE：后台验证、入口门禁都在它上面检出被测提交跑单元。
# 用法：bash history-tree-refresh.sh <40 位提交> <bundle 绝对路径，或 - 表示树里已有该提交> <bundle 内分支引用，如 refs/heads/<集成分支>>
#   FORCE_INSTALL=1 强制重装前端依赖（缺省只在锁文件与已装依赖不一致时才装）
# 步骤：bundle verify → fetch 对象（不建分支）→ 校验取到的就是目标提交 → checkout --detach →
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
[[ ${C} =~ ^[0-9a-f]{40}$ ]] || { echo "提交必须是 40 位小写十六进制"; exit 2; }
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
git -C "${HT}" checkout -q --detach "${C}"
[ "$(git -C "${HT}" rev-parse HEAD)" = "${C}" ]
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
