#!/bin/bash
# VC-4 第一步：从 bundle 建 source/gate-tree/build-tree/plan-source 四棵树（umask 022：build tree 合同要求 0022），
# 四树使用同一完整历史来源；仅 build-tree 注入 vendor。先完成暂存和收据校验再替换原树，原树保留回退。
set -Eeuo pipefail; umask 022
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
python3 "$DRV/vc4_contract.py" network-check > /dev/null
verify_history_tree "$HISTORY_TEST_TREE"
STAGING=$(mktemp -d "${B}.staging-XXXXXX")
PREVIOUS=""
cleanup_staging() {
  if [ -d "$STAGING" ]; then rm -rf "$STAGING"; fi
  if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ] && [ ! -e "$B" ]; then mv "$PREVIOUS" "$B"; fi
}
trap cleanup_staging EXIT
SOURCE_ORIGIN=$(git -C "$HISTORY_TEST_TREE" remote get-url origin)
clone_test_tree "$STAGING/source" "$BUNDLE" "$BUNDLE_BRANCH" "$C"
git -C "$STAGING/source" remote set-url origin "$SOURCE_ORIGIN"
for t in gate-tree build-tree plan-source; do
  git clone -q --no-checkout "$STAGING/source" "$STAGING/$t"
  git -C "$STAGING/$t" checkout -q --detach "$C"
  git -C "$STAGING/$t" remote set-url origin "$SOURCE_ORIGIN"
  verify_history_tree "$STAGING/$t" "$C"
done
for t in source gate-tree build-tree plan-source; do
  echo "$t HEAD=$(git -C "$STAGING/$t" rev-parse HEAD) status=[$(git -C "$STAGING/$t" status --porcelain --untracked-files=all)]"
done
# 承接收据所在提交也必须在 source 仓库对象里（build 阶段从对象取 source-transition 原文）
git -C "$STAGING/source" cat-file -e "$DC:$RECEIPT"
echo "receipt object present in $DC"
# 依赖不变才复用 vendor；依赖变化时重新物化，新输入摘要使实现门禁全部重跑。
VENDOR_MODE=regenerated_changed
if cmp -s "$STAGING/source/backend/go.mod" "$PREV_CANDIDATE/source/backend/go.mod" && \
   cmp -s "$STAGING/source/backend/go.sum" "$PREV_CANDIDATE/source/backend/go.sum"; then
  VENDOR_MODE=regenerated_missing
fi
if [ "$VENDOR_MODE" = regenerated_missing ] && [ -d "$PREV_CANDIDATE/build-tree/backend/vendor" ] && \
   [ ! -L "$PREV_CANDIDATE/build-tree/backend/vendor" ] && [ -f "$PREV_CANDIDATE/build-tree/backend/vendor/modules.txt" ]; then
  cp -a "$PREV_CANDIDATE/build-tree/backend/vendor" "$STAGING/build-tree/backend/vendor"
  VENDOR_MODE=copied_previous
else
  ( cd "$STAGING/build-tree/backend" && go mod vendor )
  test -z "$(git -C "$STAGING/build-tree" status --porcelain --untracked-files=all)"
  echo "vendor 已按本轮模块合同重新生成：$VENDOR_MODE"
fi
python3 "$DRV/vc4_contract.py" record-trees --base "$STAGING" --vendor-mode "$VENDOR_MODE" > /dev/null
if [ -e "$B" ]; then
  PREVIOUS="${B}.previous-$(date -u +%Y%m%dt%H%M%Sz)-${STAGING##*-}"
  test ! -e "$PREVIOUS"
  mv "$B" "$PREVIOUS"
fi
mv "$STAGING" "$B"
trap - EXIT
echo "建树收据：$B/tree-preparation.json；回退目录：${PREVIOUS:-无前序目录}"
echo "TREES_DONE $(utc_now)"
