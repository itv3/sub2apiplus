#!/bin/bash
# VC-4 同源构建（严格合同 sub2apiplus-candidate-build-parameters/v2）：
#   go build（build-tree，含 vcs 信息）→ Docker context（mode 逐字沿用 build-tree）→ docker build →
#   build-parameters.json / 前端 builder 收据 / 工具链偏差批准收据。
# 用法：bash build.sh <C 完整 sha>（必须等于参数文件中的 C）
set -Eeuo pipefail; umask 022
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"; cd "$D"
test "$1" = "$C"; C9=${C:0:9}
TAG=sub2apiplus-c0154-candidate:$ROUND-$C9; VERSION_LABEL="$(cat $B/source/backend/cmd/server/VERSION)-$CAND"
mkdir -p "$B/artifacts"; chmod 700 "$B/artifacts"
test "$(git -C $B/build-tree rev-parse HEAD)" = "$C"
test -z "$(git -C $B/build-tree status --porcelain --untracked-files=all)"
test -f "$B/frontend-dist/index.html"; test -f "$B/build-tree/backend/vendor/modules.txt"
# ---------- 同源 go build ----------
DATE=$(utc_now)
LDFLAGS="-s -w -X main.Commit=$C -X main.Date=$DATE -X main.BuildType=release"
echo "$DATE" > "$B/artifacts/built-at-utc.txt"
( cd "$B/build-tree/backend" && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOFLAGS=-mod=vendor go build -tags=embed,candidatecapture -ldflags "$LDFLAGS" -o "$B/artifacts/sub2api" ./cmd/server )
chmod 755 "$B/artifacts/sub2api"
go version -m "$B/artifacts/sub2api" | grep -E "vcs\.|-tags|GOARCH|GOOS|CGO_ENABLED" | tr -d '\t' | paste -sd' '
# ---------- Docker context：mode 逐字沿用 build-tree（cp -a），不做统一 chmod ----------
CTX="$B/artifacts/ctx"; rm -rf "$CTX"; mkdir -p "$CTX/backend" "$CTX/deploy"
cp -a "$B/artifacts/sub2api" "$CTX/sub2api"
cp -a "$B/build-tree/backend/resources" "$CTX/backend/resources"
cp -a "$B/build-tree/deploy/docker-entrypoint.sh" "$B/build-tree/deploy/container-healthcheck.sh" "$CTX/deploy/"
cp -a "$B/build-tree/Dockerfile.goreleaser" "$CTX/Dockerfile"
find "$CTX" -type d -empty -print -delete
E=$(cat "$RUNROOT/E.txt")
BASE_ARGS=()
for name in ALPINE_IMAGE POSTGRES_IMAGE; do
  digest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inputs"]["base_images"][sys.argv[2]]["repo_digests"][0])' "$E/pre-build.json" "$name")
  BASE_ARGS+=(--build-arg "$name=$digest")
done
docker build --platform linux/arm64 "${BASE_ARGS[@]}" --label "org.opencontainers.image.revision=$C" --label "org.opencontainers.image.version=$VERSION_LABEL" -t "$TAG" "$CTX" > "$B/artifacts/docker-build.log" 2>&1
IMAGE_ID=$(docker inspect --format '{{.Id}}' "$TAG")
docker image inspect --format '{{json .RepoDigests}}' "$IMAGE_ID" | grep -q "sub2apiplus-c0154-candidate@$IMAGE_ID"
NODE_VERSION=$(tr -d '[:space:]' < "$B/frontend-build/node-version.txt"); PNPM_VERSION=$(tr -d '[:space:]' < "$B/frontend-build/pnpm-version.txt")
NODE_IMAGE_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["inputs"]["base_images"]["NODE_IMAGE"]["image_id"])' "$E/pre-build.json")
# ---------- 三份收据 ----------
python3 - "$B" "$C" "$DATE" "$LDFLAGS" "$IMAGE_ID" "$VERSION_LABEL" "$NODE_VERSION" "$PNPM_VERSION" "$NODE_IMAGE_ID" "$CAND" "$FRONTEND_DEVIATION_APPROVED_BY" <<'PY'
import json, sys, hashlib, platform, datetime
from pathlib import Path
from tools.official_client_capture import codex_upgrade_candidate_build as cb
B, C, DATE, LDFLAGS, IMAGE_ID, VERSION_LABEL, NODE_VERSION, PNPM_VERSION, NODE_IMAGE_ID, CAND, APPROVED_BY = sys.argv[1:]
B = Path(B); art = B / "artifacts"
def binding(path: Path) -> dict:
    return {"path": str(path), "sha256": cb.file_sha256(path), "bytes": path.stat().st_size}
def write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"); path.chmod(0o600)
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
dist_inventory = cb.scan_tree_inventory(B / "frontend-dist")
builder_identity = (f"approved_local: docker image node:20-slim ({NODE_IMAGE_ID}) on {platform.machine()} {platform.release()}, "
                    f"corepack pnpm@{PNPM_VERSION}; command per .github/workflows/release.yml (pnpm install --frozen-lockfile && pnpm run build)")
builder = cb._self_bound({
    "schema_version": cb.FRONTEND_BUILDER_SCHEMA, "status": "complete", "builder_identity": builder_identity,
    "source_git_commit": C, "build_command": ["pnpm", "run", "build"], "node_version": NODE_VERSION, "pnpm_version": PNPM_VERSION,
    "package_manifest_sha256": cb.file_sha256(B / "source/frontend/package.json"),
    "lockfile_sha256": cb.file_sha256(B / "source/frontend/pnpm-lock.yaml"),
    "dist_inventory_sha256": dist_inventory["inventory_sha256"], "live_request_count": 0, "built_at_utc": now,
})
write(art / "frontend-builder-receipt.json", builder)
approval = cb._self_bound({
    "schema_version": cb.TOOLCHAIN_APPROVAL_SCHEMA, "status": "approved", "candidate_id": CAND,
    "expected": {"node_major": 20, "builder_kind": "release_pipeline"},
    "actual": {"node_version": NODE_VERSION, "builder_kind": "approved_local"},
    "reason": ("候选镜像只用于 ARM64 隔离抓包（携带 candidatecapture 构建标签，不得发布），无法由 GitHub Actions 发版流水线产出；"
               "前端 dist 改在 ARM64 上以 docker node:20-slim（Node 20 与流水线一致）按流水线同一命令构建，仅 builder 身份偏离 release_pipeline。"),
    "approved_by": APPROVED_BY,
    "approved_at_utc": now,
})
write(art / "frontend-toolchain-deviation-approval.json", approval)
binary = binding(art / "sub2api")
params = {
    "schema_version": cb.BUILD_PARAMETERS_SCHEMA, "candidate_id": CAND,
    "source": {"root": str(B / "source"), "git_commit": C},
    "build_tree": {"root": str(B / "build-tree"), "git_commit": C, "umask": "0022"},
    "frontend": {
        "source_root": "frontend", "package_manifest": "package.json", "lockfile": "pnpm-lock.yaml",
        "build_command": ["pnpm", "run", "build"], "node_version": NODE_VERSION, "pnpm_version": PNPM_VERSION,
        "builder": {"kind": "approved_local", "identity": builder_identity, "receipt": binding(art / "frontend-builder-receipt.json")},
        "dist_source_root": str(B / "frontend-dist"), "dist_build_tree_path": "backend/internal/web/dist",
        "toolchain_policy": {"required_node_major": 20, "deviation_approval": binding(art / "frontend-toolchain-deviation-approval.json")},
    },
    "go_build": {
        "command": ["go", "build", "-tags=embed,candidatecapture", "-ldflags", LDFLAGS, "-o", str(art / "sub2api"), "./cmd/server"],
        "working_directory": "backend",
        "environment": {"CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64", "GOFLAGS": "-mod=vendor"},
        "required_tags": ["candidatecapture", "embed"],
    },
    "docker_build": {
        "context_root": str(art / "ctx"), "dockerfile": "Dockerfile", "platform": "linux/arm64", "image_id": IMAGE_ID,
        "labels": {"org.opencontainers.image.revision": C, "org.opencontainers.image.version": VERSION_LABEL},
        "entrypoint": ["/app/docker-entrypoint.sh"],
        "assembly": [
            {"context_path": "Dockerfile", "source_kind": "build_tree", "source_path": "Dockerfile.goreleaser"},
            {"context_path": "backend/resources", "source_kind": "build_tree", "source_path": "backend/resources"},
            {"context_path": "deploy/container-healthcheck.sh", "source_kind": "build_tree", "source_path": "deploy/container-healthcheck.sh"},
            {"context_path": "deploy/docker-entrypoint.sh", "source_kind": "build_tree", "source_path": "deploy/docker-entrypoint.sh"},
            {"context_path": "sub2api", "source_kind": "binary", "source_path": str(art / "sub2api")},
        ],
    },
    "binary": binary,
}
write(art / "build-parameters.json", params)
# 本地先按严格合同校验参数（不写 Campaign）
cb.validate_build_parameters(params, candidate_id=CAND, source_root=B / "source", git_commit=C, binary_path=art / "sub2api",
    binary_sha256=binary["sha256"], binary_bytes=binary["bytes"], build_tree=B / "build-tree", docker_context=art / "ctx",
    frontend_dist_source=B / "frontend-dist", target_architecture="linux/arm64", image_id=IMAGE_ID)
print(json.dumps({"binary_sha256": binary["sha256"], "binary_bytes": binary["bytes"], "image_id": IMAGE_ID, "node": NODE_VERSION, "pnpm": PNPM_VERSION, "dist_files": dist_inventory["file_count"], "built_at_utc": DATE}, ensure_ascii=False))
PY
# source transition = 描述 A→C 的冻结承接收据（入库于 D 提交，不在候选 commit 树内），从 source 仓库对象取原文放到源码树外
git -C "$B/source" show "$DC:$RECEIPT" > "$B/artifacts/source-transition.json"; chmod 600 "$B/artifacts/source-transition.json"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['current_commit']==sys.argv[2], d['current_commit']; print('source-transition:', d['base_commit'][:9], '->', d['current_commit'][:9], d['result'], d['unregistered_path_count'])" "$B/artifacts/source-transition.json" "$C"
echo "BUILD_DONE $(utc_now)"
