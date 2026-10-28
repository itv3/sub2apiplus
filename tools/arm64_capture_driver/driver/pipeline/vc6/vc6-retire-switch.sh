#!/bin/bash
# VC-6 §4.6.7 退休部署切换：把生产网关切到退休版本（env 的 RETIRE_VERSION）后的最终镜像，复核 Active／Previous 坐标不变、回滚镜像仍在，
# 并在退休构建树上导出运行闭集，证明 profiles/<退休版本> 不在运行投影内（画像字节按冻结合同保留在源码树）。
# R15 下容器替换一律经 egress-transition 严格模式；阶段证据已存在时拒绝覆盖。
set -Eeuo pipefail; umask 077
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
# 业务验证模型：缺省取 env 的 LITE_MODEL（见 vc6-switch.sh 同处注释），可用环境变量 BUSINESS_MODEL 覆盖。
APP=$COMPOSE_DIR
BACKUP=$COMPOSE_BACKUP
test -s "$BACKUP" || { echo "缺 COMPOSE_BACKUP：$BACKUP"; exit 2; }
D=${D:-/root/docker/capture-cli/data}; TOOLS=$D/tools/official_client_capture
B=$D/candidates/$CAND
W=$V6; R=$(cat "$W/vc6-retire-root.txt")
A=$B/retire-artifacts
PROD_IMAGE_ID=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['docker_build']['image_id'])" "$A/retire-build-parameters.json")
TREE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['source_tree_sha256'])" "$A/retire-build-parameters.json")
PC=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['authoritative_commit'])" "$A/retire-build-parameters.json")
PROD_REF=${PROD_IMAGE_REPO}@$PROD_IMAGE_ID
BUILD_ID="${CAMPAIGN_PREFIX}-production-retire-${PC:0:9}-$(date -u -d "$(cat "$A/built-at-utc.txt")" +%Y%m%dt%H%M%Sz)"; echo "$BUILD_ID" > "$W/vc6-retire-build-id.txt"
# ROLLBACK_REF 由 vc6-env.sh 现取；退休后 Active／Previous 坐标不变，目标版本与摘要仍是本轮目标
EXPECT_VERSION=$TARGET_VERSION
EXPECT_DIGEST=$TARGET_DIGEST
E=$R/stages/retire_switch; test ! -e "$E/stage.json" || { echo "退休切换阶段已完成，拒绝覆盖"; exit 2; }
mkdir -p "$E"; chmod 700 "$R" "$R/stages" "$E"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "STAGE=retire_switch START=$START image=$PROD_IMAGE_ID build_id=$BUILD_ID"
echo "=== 回滚镜像仍可用（只读核对，不切换）"
docker image inspect "$ROLLBACK_REF" --format 'rollback_present={{.Id}}' | head -1
echo "=== 退休后运行投影（退休构建树 cmd/egressruntimedump）"
DUMP=$(mktemp -d)
( cd "$B/retire-build-tree/backend" && PATH=/usr/local/go/bin:$PATH GOFLAGS=-mod=vendor go run ./cmd/egressruntimedump -output "$DUMP/out" ) | tail -1
( cd "$DUMP/out" && find . -type f | sort | xargs sha256sum ) > "$E/runtime-projection-files.txt"
echo "运行闭集文件数 $(wc -l < "$E/runtime-projection-files.txt")；profiles：$(grep -o 'profiles/[0-9.]*/' "$E/runtime-projection-files.txt" | sort -u | tr '\n' ' ')"
if grep -q "profiles/${RETIRE_VERSION}/" "$E/runtime-projection-files.txt"; then echo "FATAL: 运行投影仍含 ${RETIRE_VERSION} 画像"; exit 1; fi
rm -rf "$DUMP"
echo "=== 退休前运行投影（晋升同步构建树，对照用）"
DUMPB=$(mktemp -d)
( cd "$B/production-build-tree/backend" && PATH=/usr/local/go/bin:$PATH GOFLAGS=-mod=vendor go run ./cmd/egressruntimedump -output "$DUMPB/out" ) | tail -1
( cd "$DUMPB/out" && find . -type f | sort | xargs sha256sum ) > "$E/runtime-projection-before-files.txt"
cp "$B/production-build-tree/backend/internal/officialegress/catalogdata/runtime/release-catalog.json" "$E/pre-retire-selector.json"
rm -rf "$DUMPB"
cd "$APP"
cp docker-compose.yml "$E/docker-compose.before.yml"
cp "$BACKUP" docker-compose.yml
python3 - "$PROD_REF" "$PROD_IMAGE_ID" "$TREE" "$BUILD_ID" <<'PY'
import os, sys, pathlib
ref, image_id, tree, build_id = sys.argv[1:]
E = os.environ
# 正式镜像行取 env 的 PRODUCTION_IMAGE（含 @sha256 摘要的整行），活动事实各字段取 env，不再写字面量
old = "    image: " + E["PRODUCTION_IMAGE"] + "\n    container_name: sub2apiplus\n"
act_path, prof_id, prof_digest, target_ver = E["ACT_FACT_NAME"], E["TARGET_PROFILE_ID"], E["TARGET_DIGEST"], E["TARGET_VERSION"]
p = pathlib.Path("docker-compose.yml"); s = p.read_text()
assert s.count(old) == 1, "compose 备份中正式镜像行不唯一或不存在"
s = s.replace(old, f"    image: {ref}\n    container_name: sub2apiplus\n")
anchor = "      - AUTO_SETUP=true\n"; assert s.count(anchor) == 1
s = s.replace(anchor, anchor + "".join([
  f"      # VC-6 §4.6.7：退休 {E['RETIRE_VERSION']} 运行画像后的最终生产镜像（默认 active，无强制 mode override）\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_FACT_PATH=/app/data/{act_path}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_PROFILE_ID={prof_id}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST={prof_digest}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_IMAGE_ID={image_id}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_IMAGE_REFERENCE={ref}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_SOURCE_TREE_SHA256={tree}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_BUILD_ID={build_id}\n",
  f"      - GATEWAY_EGRESS_ACTIVATION_DEPLOYED_VERSION={target_ver}\n",
]))
p.write_text(s); print("compose ->", ref)
PY
cp docker-compose.yml "$E/docker-compose.used.yml"
docker compose config --quiet
python3 "$TOOLS/codex_upgrade_supervisor.py" egress-transition --container sub2apiplus --compose-service sub2api \
  -- docker compose -f "$APP/docker-compose.yml" up -d sub2api 2>&1 | tail -4
st=none; for i in $(seq 1 48); do st=$(docker inspect sub2apiplus --format '{{.State.Health.Status}}' 2>/dev/null || echo none); [ "$st" = healthy ] && break; sleep 5; done
IMAGE=$(docker inspect sub2apiplus --format '{{.Image}}')
echo "health=$st image=$IMAGE expect=$PROD_IMAGE_ID"
[ "$st" = healthy ] && [ "$IMAGE" = "$PROD_IMAGE_ID" ] || { echo "STAGE_FAILED"; docker logs --tail 30 sub2apiplus 2>&1 | cut -c1-200; exit 1; }
echo "=== 网络、挂载与出口守护复核"
docker inspect sub2apiplus --format '{{json .NetworkSettings.Networks}}' > "$E/networks.json"
python3 -c "import json;d=json.load(open('$E/networks.json'));ips={k:v['IPAddress'] for k,v in d.items()};print('ips:',ips);assert ips.get('sub2apiplus_sub2api-network')=='172.20.0.2' and ips.get('proxy-network')=='172.25.0.3', ips"
docker inspect sub2apiplus --format '{{json .Mounts}}' > "$E/mounts.json"
cp /run/sub2api-egress/status.json "$E/egress-status.json" 2>/dev/null || echo "（出口守护状态文件不可读）"
echo "=== activation fact"
cp "$APP/data/${ACT_FACT_NAME}" "$E/activation-fact.json"
python3 - "$E/activation-fact.json" "$EXPECT_VERSION" "$EXPECT_DIGEST" "$PROD_IMAGE_ID" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({k: d.get(k) for k in ("profile_mode","codex_version","profile_id","profile_digest","image_id","image_reference","source_tree_sha256","build_id","deployed_version","release_digest")}, ensure_ascii=False))
assert d["profile_mode"] == "active" and d["codex_version"] == sys.argv[2] and d["profile_digest"] == sys.argv[3] and d["image_id"] == sys.argv[4], d
PY
curl -s --max-time 20 http://172.20.0.2:8080/health > "$E/health.json"; echo "health: $(head -c 200 "$E/health.json")"
echo "=== 真实业务验证（API key ${API_KEY_ID}，账号 ${CODEX_ACCOUNT_ID} 一次请求）"
set -a; . "$APP/.env"; set +a
KEY=$(docker exec sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAtc "select key from api_keys where id=${API_KEY_ID} and status='active' and deleted_at is null")
PRE=$(docker exec sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAtc "select coalesce(max(id),0) from usage_logs")
curl -s --max-time 180 -o "$E/business-response.json" -w "http_status=%{http_code} time=%{time_total}s\n" http://172.20.0.2:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d "{\"model\":\"${BUSINESS_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly one word: pong\"}]}" | tee "$E/business-curl.txt"
unset KEY
head -c 200 "$E/business-response.json"; echo
sleep 3
docker exec sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAtc "select id, model, account_id, upstream_endpoint, duration_ms from usage_logs where id > $PRE order by id" | tee "$E/usage-rows.txt"
grep -q "http_status=200" "$E/business-curl.txt" && test -s "$E/usage-rows.txt" || { echo "业务验证未通过"; exit 1; }
docker logs --since "$START" sub2apiplus > "$E/container.log" 2>&1 || true
ERRS=$(grep -c '"level":"ERROR"\|"level":"FATAL"' "$E/container.log" || true)
GUARD=$(grep -ci 'guard.*fail' "$E/container.log" || true)
echo "日志 ERROR/FATAL=$ERRS guard 失败=$GUARD"; grep '"level":"ERROR"\|"level":"FATAL"' "$E/container.log" | head -5 | cut -c1-300 || true
chmod 600 "$E"/*
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$E" "$START" "$END" "$IMAGE" "$EXPECT_VERSION" "$EXPECT_DIGEST" "$ERRS" "$GUARD" "$ROLLBACK_REF" "$TREE" "$BUILD_ID" <<'PY'
import json, os, sys, socket
from pathlib import Path
E, START, END, IMAGE, VER, DIG, ERRS, GUARD, ROLLBACK, TREE, BUILD_ID = sys.argv[1:]
E = Path(E)
ok = int(ERRS) == 0 and int(GUARD) == 0
stage = {"name": "retire_switch", "started_at_utc": START, "completed_at_utc": END, "host": socket.gethostname(),
         "architecture": "linux/arm64", "status": "pass" if ok else "fail", "image_id": IMAGE,
         "purpose": "VC-6 §4.6.7 退休 " + os.environ["RETIRE_VERSION"] + " 运行画像后的生产镜像切换",
         "checks": {"container_status": "running", "health": "pass", "active_version": VER,
                    "profile_digest": DIG, "rollback_image_reference_unchanged": ROLLBACK,
                    "source_tree_sha256": TREE, "build_id": BUILD_ID,
                    "fatal_log_count": int(ERRS), "guard_failure_count": int(GUARD)},
         "evidence": sorted(p.name for p in E.iterdir() if p.is_file())}
(E / "stage.json").write_text(json.dumps(stage, ensure_ascii=False, indent=2) + "\n"); (E / "stage.json").chmod(0o600)
print(json.dumps({k: stage[k] for k in ("name", "status", "started_at_utc", "completed_at_utc", "image_id", "checks")}, ensure_ascii=False))
PY
echo "RETIRE_SWITCH_DONE $(date -u +%H:%M:%SZ)"
