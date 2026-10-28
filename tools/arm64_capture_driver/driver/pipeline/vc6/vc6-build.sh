#!/bin/bash
# VC-6 §4.6.4：用 production tree 的权威同步提交构建切换镜像（不含 candidatecapture），并写构建参数收据。
# R15 出口策略只放行登记容器，BuildKit 的 RUN apk add 必须走 --network=host。
set -Eeuo pipefail; umask 022
export PATH=/usr/local/go/bin:$PATH PYTHONDONTWRITEBYTECODE=1
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=.
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
B=$D/candidates/$CAND
P=$B/production-tree
W=$V6
R=$(cat $W/vc6-root.txt)
PC=$(cat $W/vc6-pc.txt)                                   # 权威仓库 production tree 同步提交
# C＝已验收候选 commit（production tree 的基线），由 vc6-env.sh 从 env.sh 现取
TREE=$(cat "$W/vc6-tree.txt")
A=$B/production-artifacts; rm -rf "$A"; mkdir -p "$A" "$R/build"; chmod 700 "$A" "$R/build"
PB=$B/production-build-tree; rm -rf "$PB"; cp -a "$P" "$PB"
git -C "$PB" fetch -q "$W/production-tree.bundle" "refs/heads/${BUNDLE_BRANCH}:refs/heads/vc6-production-tree"
git -C "$PB" checkout -q -f --detach "$PC"
git -C "$PB" clean -qfd
test -z "$(git -C "$PB" status --porcelain --untracked-files=all)"
git config --global --add safe.directory "$PB" 2>/dev/null || true
cp -a "$B/build-tree/backend/vendor" "$PB/backend/vendor"
rm -rf "$PB/backend/internal/web/dist"; cp -a "$B/frontend-dist" "$PB/backend/internal/web/dist"
VERSION_LABEL="$(cat $PB/backend/cmd/server/VERSION)-${CAMPAIGN_PREFIX}-production"
DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$DATE" > "$A/built-at-utc.txt"
LDFLAGS="-s -w -X main.Commit=$PC -X main.Date=$DATE -X main.BuildType=release"
echo "=== go build（tags=embed，不含 candidatecapture）$(date -u +%H:%M:%SZ)"
( cd "$PB/backend" && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOFLAGS=-mod=vendor go build -tags=embed -ldflags "$LDFLAGS" -o "$A/sub2api" ./cmd/server )
chmod 755 "$A/sub2api"
go version -m "$A/sub2api" | grep -E "\-tags|GOARCH|GOOS|CGO_ENABLED|vcs" | tr -d '\t' | paste -sd' '
go version -m "$A/sub2api" | grep -q candidatecapture && { echo "FATAL: 生产二进制含 candidatecapture"; exit 1; } || echo "候选抓包标签缺席（符合 §4.6.4）"
echo "=== docker context 与构建 $(date -u +%H:%M:%SZ)"
CTX="$A/ctx"; rm -rf "$CTX"; mkdir -p "$CTX/backend" "$CTX/deploy"
cp -a "$A/sub2api" "$CTX/sub2api"
cp -a "$PB/backend/resources" "$CTX/backend/resources"
cp -a "$PB/deploy/docker-entrypoint.sh" "$PB/deploy/container-healthcheck.sh" "$CTX/deploy/"
cp -a "$PB/Dockerfile.goreleaser" "$CTX/Dockerfile"
find "$CTX" -type d -empty -print -delete
TAG=${PROD_IMAGE_REPO}:${TARGET_VERSION}-${PC:0:9}
docker build --network=host --platform linux/arm64 --label "org.opencontainers.image.revision=$PC" --label "org.opencontainers.image.version=$VERSION_LABEL" -t "$TAG" "$CTX" > "$A/docker-build.log" 2>&1
IMAGE_ID=$(docker inspect --format '{{.Id}}' "$TAG")
echo "IMAGE_ID=$IMAGE_ID"; echo "REPO_DIGESTS=$(docker image inspect --format '{{json .RepoDigests}}' "$IMAGE_ID")"
python3 - "$A" "$PB" "$P" "$PC" "$C" "$TREE" "$DATE" "$LDFLAGS" "$IMAGE_ID" "$TAG" "$VERSION_LABEL" "$R" <<'PY'
import json, hashlib, os, subprocess, sys, platform
from pathlib import Path
A,PB,P,PC,C,TREE,DATE,LDFLAGS,IMAGE_ID,TAG,VERSION_LABEL,R = [x for x in sys.argv[1:]]
A=Path(A); R=Path(R)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
binary=A/"sub2api"
payload={
 "schema_version":"vc6-production-build-parameters/v1",
 "campaign_id":os.environ["NEW"],
 "candidate_id":os.environ["CAND"],
 "authoritative_commit":PC,
 "candidate_commit":C,
 "production_tree":{"root":P,"source_tree_sha256":TREE,
   "note":"production tree = 已验收候选 commit + promotion inventory（清单项数与实际变化项数见 promote.out）；权威仓库同步提交 %s 另含 VC-4 承接收据文档，不参与镜像内容"%PC},
 "build_tree":{"root":PB,"umask":"0022","vendor":"复用候选 build-tree vendor","frontend_dist":"复用候选 frontend-dist","git_commit":PC,"git_status":"clean"},
 "go_build":{"command":["go","build","-tags=embed","-ldflags",LDFLAGS,"-o",str(binary),"./cmd/server"],
   "working_directory":"backend","environment":{"CGO_ENABLED":"0","GOOS":"linux","GOARCH":"arm64","GOFLAGS":"-mod=vendor"},
   "required_tags":["embed"],"forbidden_tags":["candidatecapture"]},
 "docker_build":{"context_root":str(A/"ctx"),"dockerfile":"Dockerfile","platform":"linux/arm64","network":"host","image_id":IMAGE_ID,
   "tag":TAG,"labels":{"org.opencontainers.image.revision":PC,"org.opencontainers.image.version":VERSION_LABEL},
   "repo_digests":json.loads(subprocess.run(["docker","image","inspect","--format","{{json .RepoDigests}}",IMAGE_ID],capture_output=True,text=True).stdout)},
 "binary":{"path":str(binary),"sha256":sha(binary),"bytes":binary.stat().st_size},
 "built_at_utc":DATE,
 "builder":{"kind":"approved_local","identity":"ARM64 正式切换测试服务器本地构建（与候选同一流水线，去掉 candidatecapture）","host":platform.node(),"architecture":"linux/"+platform.machine()},
 "release_pipeline":{"status":"pending","note":"ARM64 是正式切换测试服务器（2026-09-22 定）：切换演练不要求与之后的发版镜像同 digest；VC-6 收口后经老板批准推送 GitHub、CI 全绿后打注释 tag 正式发版，生产服务器按标准部署流程更新"},
}
payload["build_parameters_sha256"]=hashlib.sha256((json.dumps({k:v for k,v in payload.items()},ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()).hexdigest()
out=A/"production-build-parameters.json"; out.write_text(json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"); out.chmod(0o600)
copy=R/"build"/"production-build-parameters.json"; copy.write_bytes(out.read_bytes()); copy.chmod(0o600)
print(json.dumps({"image_id":IMAGE_ID,"binary_sha256":payload["binary"]["sha256"],"bytes":payload["binary"]["bytes"],"params_sha256":sha(out)},ensure_ascii=False))
PY
echo "BUILD_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
