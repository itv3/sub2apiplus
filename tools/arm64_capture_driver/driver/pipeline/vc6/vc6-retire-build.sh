#!/bin/bash
# VC-6 §4.6.7 退休部署：用退休后的权威提交重建最终生产镜像（不含 candidatecapture），写退休构建参数收据。
# R15 出口策略只放行登记容器，BuildKit 的 RUN apk add 必须走 --network=host。
set -Eeuo pipefail; umask 022
export PATH=/usr/local/go/bin:$PATH PYTHONDONTWRITEBYTECODE=1
# 必须在 cd 之前把脚本所在目录解析成绝对路径：用相对路径调用本脚本时（例如 bash ./vc6-retire-build.sh），
# 下一行 cd 到数据根之后，相对的 dirname 会指向数据根下并不存在的位置，source vc6-env.sh 就找不到文件。
VC6_SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
D=${D:-/root/docker/capture-cli/data}; cd "$D"; export PYTHONPATH=.
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "${VC6_SELF_DIR}/vc6-env.sh"
B=$D/candidates/$CAND
W=$V6
R=$NEWDIR/control/vc6-retire-deploy-$STAMP
echo "$R" > "$W/vc6-retire-root.txt"
PC=$(cat "$W/vc6-retire-pc.txt")     # 退休后的权威提交
PREV=$(cat "$W/vc6-pc.txt")   # 退休前部署所用的权威提交（production tree 同步）
mkdir -p "$R/build"; chmod 700 "$R" "$R/build"
A=$B/retire-artifacts; rm -rf "$A"; mkdir -p "$A"; chmod 700 "$A"
PB=$B/retire-build-tree; rm -rf "$PB"; cp -a "$B/production-tree" "$PB"
git -C "$PB" fetch -q "$W/production-tree.bundle" "refs/heads/${BUNDLE_BRANCH}:refs/heads/vc6-production-tree"
git -C "$PB" fetch -q "$W/retire-tree.bundle" "refs/heads/${BUNDLE_BRANCH}:refs/heads/vc6-retire-tree"
git -C "$PB" checkout -q -f --detach "$PC"
git -C "$PB" clean -qfd
test -z "$(git -C "$PB" status --porcelain --untracked-files=all)"
git config --global --add safe.directory "$PB" 2>/dev/null || true
# 退休后的源码树摘要：git 跟踪文件的确定性清单摘要（确定性算法，可在构建树上复算）
TREE=$(git -C "$PB" ls-files -s | sha256sum | cut -d' ' -f1); echo "SOURCE_TREE_SHA256=$TREE"
cp -a "$B/build-tree/backend/vendor" "$PB/backend/vendor"
rm -rf "$PB/backend/internal/web/dist"; cp -a "$B/frontend-dist" "$PB/backend/internal/web/dist"
VERSION_LABEL="$(cat $PB/backend/cmd/server/VERSION)-${CAMPAIGN_PREFIX}-production-retire"
DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "$DATE" > "$A/built-at-utc.txt"
LDFLAGS="-s -w -X main.Commit=$PC -X main.Date=$DATE -X main.BuildType=release"
echo "=== go build（tags=embed，不含 candidatecapture）$(date -u +%H:%M:%SZ)"
( cd "$PB/backend" && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 GOFLAGS=-mod=vendor go build -tags=embed -ldflags "$LDFLAGS" -o "$A/sub2api" ./cmd/server )
chmod 755 "$A/sub2api"
go version -m "$A/sub2api" | grep -E "\-tags|GOARCH|GOOS|CGO_ENABLED|vcs" | tr -d '\t' | paste -sd' '
go version -m "$A/sub2api" | grep -q candidatecapture && { echo "FATAL: 生产二进制含 candidatecapture"; exit 1; } || echo "候选抓包标签缺席（符合 §4.6.4）"
echo "=== 退休事实记录：画像字节按冻结合同保留、随 go:embed 进入二进制，运行投影由 selector／ReleaseGraph／SnapshotCatalog 决定"
echo "binary_profiles_retired_refs=$(strings -a "$A/sub2api" | grep -c "profiles/${RETIRE_VERSION}/" || true)"
echo "=== docker context 与构建 $(date -u +%H:%M:%SZ)"
CTX="$A/ctx"; rm -rf "$CTX"; mkdir -p "$CTX/backend" "$CTX/deploy"
cp -a "$A/sub2api" "$CTX/sub2api"
cp -a "$PB/backend/resources" "$CTX/backend/resources"
cp -a "$PB/deploy/docker-entrypoint.sh" "$PB/deploy/container-healthcheck.sh" "$CTX/deploy/"
cp -a "$PB/Dockerfile.goreleaser" "$CTX/Dockerfile"
find "$CTX" -type d -empty -print -delete
TAG=${PROD_IMAGE_REPO}:${TARGET_VERSION}-retire-${PC:0:9}
docker build --network=host --platform linux/arm64 --label "org.opencontainers.image.revision=$PC" --label "org.opencontainers.image.version=$VERSION_LABEL" -t "$TAG" "$CTX" > "$A/docker-build.log" 2>&1
IMAGE_ID=$(docker inspect --format '{{.Id}}' "$TAG"); echo "IMAGE_ID=$IMAGE_ID"
python3 - "$A" "$PB" "$PC" "$PREV" "$TREE" "$DATE" "$LDFLAGS" "$IMAGE_ID" "$TAG" "$VERSION_LABEL" "$R" <<'PY'
import json, hashlib, os, sys, platform
from pathlib import Path
A,PB,PC,PREV,TREE,DATE,LDFLAGS,IMAGE_ID,TAG,VERSION_LABEL,R = sys.argv[1:]
A=Path(A); R=Path(R)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
binary=A/"sub2api"
payload={
 "schema_version":"vc6-retire-build-parameters/v1",
 "campaign_id":os.environ["NEW"],
 "candidate_id":os.environ["CAND"],
 "purpose":"VC-6 §4.6.7 运行画像退休后的生产镜像重建（Active／Previous 坐标不变，退休 "+os.environ["RETIRE_VERSION"]+"）",
 "authoritative_commit":PC,
 "previous_deployed_commit":PREV,
 "source_tree_sha256":TREE,
 "source_tree_sha256_algorithm":"sha256(git ls-files -s 的逐字输出)，在退休后构建树上复算即可比对",
 "build_tree":{"root":PB,"umask":"0022","vendor":"复用候选 build-tree vendor","frontend_dist":"复用候选 frontend-dist","git_commit":PC,"git_status":"clean"},
 "go_build":{"command":["go","build","-tags=embed","-ldflags",LDFLAGS,"-o",str(binary),"./cmd/server"],
   "working_directory":"backend","environment":{"CGO_ENABLED":"0","GOOS":"linux","GOARCH":"arm64","GOFLAGS":"-mod=vendor"},
   "required_tags":["embed"],"forbidden_tags":["candidatecapture"]},
 "docker_build":{"context_root":str(A/"ctx"),"dockerfile":"Dockerfile","platform":"linux/arm64","network":"host","image_id":IMAGE_ID,
   "tag":TAG,"labels":{"org.opencontainers.image.revision":PC,"org.opencontainers.image.version":VERSION_LABEL}},
 "binary":{"path":str(binary),"sha256":sha(binary),"bytes":binary.stat().st_size},
 "built_at_utc":DATE,
 "builder":{"kind":"approved_local","identity":"ARM64 正式切换测试服务器本地构建（与候选同一流水线，去掉 candidatecapture）","host":platform.node(),"architecture":"linux/"+platform.machine()},
}
payload["build_parameters_sha256"]=hashlib.sha256((json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()).hexdigest()
out=A/"retire-build-parameters.json"; out.write_text(json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"); out.chmod(0o600)
copy=R/"build"/"retire-build-parameters.json"; copy.write_bytes(out.read_bytes()); copy.chmod(0o600)
print(json.dumps({"image_id":IMAGE_ID,"binary_sha256":payload["binary"]["sha256"],"bytes":payload["binary"]["bytes"],"source_tree_sha256":TREE},ensure_ascii=False))
PY
echo "RETIRE_BUILD_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
