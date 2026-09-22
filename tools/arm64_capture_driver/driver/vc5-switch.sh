#!/bin/bash
# 把 ARM64 网关切换为候选镜像（previous 模式，声明目标画像）或恢复生产镜像。
# 用法：bash vc5-switch.sh candidate <image_tag> <image_id> <source_tree_sha256> <build_id> | bash vc5-switch.sh restore
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$COMPOSE_DIR"
MODE="$1"
if [ "$MODE" = restore ]; then
  cp "$COMPOSE_BACKUP" docker-compose.yml
else
  TAG="$2"; IMAGE_ID="$3"; TREE="$4"; BUILD_ID="$5"
  cp "$COMPOSE_BACKUP" docker-compose.yml
  python3 - "$TAG" "$CAND" "$IMAGE_ID" "$TREE" "$BUILD_ID" "$PRODUCTION_IMAGE" "$PROFILE_ID" "$PROFILE_DIGEST" <<'PY'
import sys, pathlib
tag, cand, image_id, tree, build_id, production_image, profile_id, profile_digest = sys.argv[1:]
p = pathlib.Path("docker-compose.yml"); s = p.read_text()
old_img = f"    image: {production_image}\n    container_name: sub2apiplus\n"
assert s.count(old_img) == 1, "compose 备份中生产镜像行不唯一或不存在"
s = s.replace(old_img, f"    image: {tag}\n    container_name: sub2apiplus\n")
anchor = "      - AUTO_SETUP=true\n"; assert s.count(anchor) == 1
s = s.replace(anchor, anchor + f"      # Codex 0.154 候选（VC-5）：以 previous 模式选择候选 Release，并落盘画像激活事实（声明身份必须与 VC-4 构建收据一致）\n      - GATEWAY_OFFICIAL_CLIENT_PROFILES_MODE=previous\n      - GATEWAY_EGRESS_ACTIVATION_FACT_PATH=/app/data/{cand}-candidate-activation-fact.json\n      - GATEWAY_EGRESS_ACTIVATION_PROFILE_ID={profile_id}\n      - GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST={profile_digest}\n      - GATEWAY_EGRESS_ACTIVATION_IMAGE_ID={image_id}\n      - GATEWAY_EGRESS_ACTIVATION_IMAGE_REFERENCE=sub2apiplus-c0154-candidate@{image_id}\n      - GATEWAY_EGRESS_ACTIVATION_SOURCE_TREE_SHA256={tree}\n      - GATEWAY_EGRESS_ACTIVATION_BUILD_ID={build_id}\n      - GATEWAY_EGRESS_ACTIVATION_DEPLOYED_VERSION=0.154.0\n")
p.write_text(s); print("compose 已切换到", tag)
PY
fi
docker compose config --quiet; docker compose up -d sub2api 2>&1 | tail -n 1
st=unknown; for i in $(seq 1 36); do st=$(docker inspect sub2apiplus --format "{{.State.Health.Status}}"); [ "$st" = healthy ] && break; sleep 5; done
echo "health=$st image=$(docker inspect sub2apiplus --format '{{.Config.Image}}') image_id=$(docker inspect sub2apiplus --format '{{.Image}}')"
[ "$st" = healthy ]
if [ "$MODE" != restore ]; then f="data/$CAND-candidate-activation-fact.json"; test -f "$f"; python3 -c "
import json,sys; d=json.load(open(sys.argv[1])); print({k:d.get(k) for k in ('profile_mode','codex_version','profile_id','profile_digest','image_id','image_reference','source_tree_sha256','build_id','deployed_version','release_digest')}); assert all(d.get(k) for k in ('profile_id','image_id','image_reference','source_tree_sha256','build_id','deployed_version')), '声明身份缺失'" "$f"; fi
