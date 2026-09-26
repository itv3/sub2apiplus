#!/bin/bash
# 前端 dist：在 node:20-slim 容器内按发版流水线命令（pnpm install --frozen-lockfile && pnpm run build）构建，
# 产物复制到独立的 frontend-dist 根，再注入 build-tree/backend/internal/web/dist。
set -Eeuo pipefail; umask 022
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 前端源码副本：frontend/ 与其 ?raw 引用的 docs/legal（LegalDocumentView.vue 引用 ../../../../docs/legal/*.md）
rm -rf "$B/frontend-build"; mkdir -p "$B/frontend-build"
git -C "$B/source" archive "$C" frontend docs/legal | tar -x -C "$B/frontend-build"
E=$(cat "$RUNROOT/E.txt")
NODE_IMAGE_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inputs"]["base_images"]["NODE_IMAGE"]["image_id"])' "$E/pre-build.json")
docker run --rm -v "$B/frontend-build:/work" -w /work/frontend -e CI=true "$NODE_IMAGE_ID" bash -c '
  set -e; umask 022
  corepack enable && corepack prepare pnpm@9.15.9 --activate
  node --version > /work/node-version.txt; pnpm --version > /work/pnpm-version.txt
  echo "node=$(cat /work/node-version.txt) pnpm=$(cat /work/pnpm-version.txt)"
  pnpm install --frozen-lockfile
  pnpm run build
' > "$B/frontend-build.log" 2>&1
echo "build exit=$?"
ls "$B/frontend-build/backend/internal/web/dist" | head
rm -rf "$B/frontend-dist"; cp -a "$B/frontend-build/backend/internal/web/dist" "$B/frontend-dist"
rm -rf "$B/build-tree/backend/internal/web/dist"; cp -a "$B/frontend-dist" "$B/build-tree/backend/internal/web/dist"
echo "dist files=$(find $B/frontend-dist -type f | wc -l) build-tree status=[$(git -C $B/build-tree status --porcelain --untracked-files=all)]"
echo "FRONTEND_DONE $(utc_now)"
