#!/bin/bash
# VC-4 第一步：从 bundle 建 source/gate-tree/build-tree/plan-source 四棵树（umask 022：build tree 合同要求 0022），
# build-tree 注入前序候选的 vendor（go.mod/go.sum 逐字相同才复用），前端 dist 由 docker node:20 单独构建后注入。
set -Eeuo pipefail; umask 022
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
rm -rf "$B"; mkdir -p "$B"; chmod 700 "$B"
git clone -q --no-checkout "$PREV_CANDIDATE/source" "$B/source"
git -C "$B/source" fetch -q "$BUNDLE" "$BUNDLE_BRANCH"
git -C "$B/source" checkout -q --detach "$C"
git -C "$B/source" remote set-url origin file:///Users/czs/Developer/sub2apiplus
for t in gate-tree build-tree plan-source; do
  git clone -q --no-checkout "$B/source" "$B/$t"; git -C "$B/$t" checkout -q --detach "$C"
  git -C "$B/$t" remote set-url origin file:///Users/czs/Developer/sub2apiplus
done
for t in source gate-tree build-tree plan-source; do
  echo "$t HEAD=$(git -C $B/$t rev-parse HEAD) status=[$(git -C $B/$t status --porcelain --untracked-files=all)]"
done
# 承接收据所在提交也必须在 source 仓库对象里（build 阶段从对象取 source-transition 原文）
git -C "$B/source" cat-file -e "$DC:$RECEIPT" && echo "receipt object present in $DC"
# 与前序候选的 go.mod/go.sum 逐字一致才复用 vendor
cmp "$B/source/backend/go.mod" "$PREV_CANDIDATE/source/backend/go.mod"; cmp "$B/source/backend/go.sum" "$PREV_CANDIDATE/source/backend/go.sum"
cp -a "$PREV_CANDIDATE/build-tree/backend/vendor" "$B/build-tree/backend/vendor"
echo "vendor modules.txt sha256=$(sha256sum $B/build-tree/backend/vendor/modules.txt | cut -d' ' -f1)"
stat -c "%a %n" "$B/source/deploy/docker-entrypoint.sh" "$B/source/deploy/container-healthcheck.sh" "$B/build-tree/deploy/docker-entrypoint.sh" "$B/build-tree/Dockerfile.goreleaser"
echo "TREES_DONE $(utc_now)"
