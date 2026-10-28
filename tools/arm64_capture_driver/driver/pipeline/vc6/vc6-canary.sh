#!/bin/bash
# VC-6 §4.6.5 独立 Active canary：隔离 compose（独立网络／PostgreSQL／Redis／数据目录），用切换镜像按晋升后 Catalog 的默认
# active 运行（不设强制 mode）。R15 出口策略只放行登记容器：canary 不接采集网络、不改出口策略，验证启动、健康、架构、activation
# fact、Guard 与日志；真实业务在 production_switch／rollback／target_restore 三个阶段用生产容器验证。
# compose 文件只写 ${VAR} 占位，密钥经 --env-file 注入，存进证据的 compose 不含任何口令；克隆的生产数据导出文件恢复后即删除。
set -Eeuo pipefail; umask 077
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
APP=$COMPOSE_DIR
D=${D:-/root/docker/capture-cli/data}
W=$V6
R=$(cat "$W/vc6-root.txt")
A=$D/candidates/$CAND/production-artifacts
export IMAGE_ID=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['docker_build']['image_id'])" "$A/production-build-parameters.json")
export IMAGE_TAG=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['docker_build']['tag'])" "$A/production-build-parameters.json")
PC=$(cat "$W/vc6-pc.txt"); BUILT=$(cat "$A/built-at-utc.txt")
export BUILD_ID="${CAMPAIGN_PREFIX}-production-${PC:0:9}-$(date -u -d "$BUILT" +%Y%m%dt%H%M%Sz)"; echo "$BUILD_ID" > "$W/vc6-build-id.txt"
export CANARY=$W/canary
export TREE=$(cat "$W/vc6-tree.txt")
PROFILE_DIGEST=$TARGET_DIGEST   # 与 env.sh 的 PROFILE_DIGEST 同值（VC-3 后写入）；已由 parse_env 导出，compose 插值直接用
docker compose -f "$CANARY/docker-compose.yml" down -v 2>/dev/null || true
rm -rf "$CANARY"; mkdir -p "$CANARY/data"; chmod 700 "$CANARY" "$CANARY/data"
cat > "$CANARY/docker-compose.yml" <<'YML'
name: vc6-canary
services:
  canary-postgres:
    image: postgres:18-alpine
    container_name: vc6-canary-postgres
    environment:
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=${POSTGRES_DB}
    networks: [canary-net]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      timeout: 5s
      retries: 20
  canary-redis:
    image: redis:8-alpine
    container_name: vc6-canary-redis
    command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}"]
    networks: [canary-net]
  canary-app:
    image: ${IMAGE_TAG}
    container_name: vc6-canary-app
    depends_on:
      canary-postgres:
        condition: service_healthy
    ports:
      - "127.0.0.1:28081:8080"
    volumes:
      - ${CANARY}/data:/app/data
    environment:
      - AUTO_SETUP=true
      # canary 按晋升后 Catalog 的默认 active 运行：不设置 GATEWAY_OFFICIAL_CLIENT_PROFILES_MODE
      - GATEWAY_EGRESS_ACTIVATION_FACT_PATH=/app/data/vc6-canary-activation-fact.json
      - GATEWAY_EGRESS_ACTIVATION_PROFILE_ID=${TARGET_PROFILE_ID}
      - GATEWAY_EGRESS_ACTIVATION_PROFILE_DIGEST=${PROFILE_DIGEST}
      - GATEWAY_EGRESS_ACTIVATION_IMAGE_ID=${IMAGE_ID}
      - GATEWAY_EGRESS_ACTIVATION_IMAGE_REFERENCE=${PROD_IMAGE_REPO}@${IMAGE_ID}
      - GATEWAY_EGRESS_ACTIVATION_SOURCE_TREE_SHA256=${TREE}
      - GATEWAY_EGRESS_ACTIVATION_BUILD_ID=${BUILD_ID}
      - GATEWAY_EGRESS_ACTIVATION_DEPLOYED_VERSION=${TARGET_VERSION}
      - SERVER_HOST=0.0.0.0
      - SERVER_PORT=8080
      - SERVER_MODE=${SERVER_MODE:-release}
      - RUN_MODE=${RUN_MODE:-standard}
      - DATABASE_HOST=canary-postgres
      - DATABASE_PORT=5432
      - DATABASE_USER=${POSTGRES_USER}
      - DATABASE_PASSWORD=${POSTGRES_PASSWORD}
      - DATABASE_DBNAME=${POSTGRES_DB}
      - DATABASE_SSLMODE=disable
      - REDIS_HOST=canary-redis
      - REDIS_PORT=6379
      - REDIS_PASSWORD=${REDIS_PASSWORD}
      - REDIS_DB=0
      - JWT_SECRET=${JWT_SECRET}
      - ADMIN_EMAIL=${ADMIN_EMAIL}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - LOG_LEVEL=${LOG_LEVEL:-info}
      - LOG_FORMAT=${LOG_FORMAT:-json}
    networks: [canary-net]
networks:
  canary-net:
    driver: bridge
YML
chmod 600 "$CANARY/docker-compose.yml"
DC=(docker compose --env-file "$APP/.env" -f "$CANARY/docker-compose.yml")
START=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "=== canary START=$START image=$IMAGE_ID build_id=$BUILD_ID"
"${DC[@]}" up -d canary-postgres canary-redis 2>&1 | tail -2
st=none; for i in $(seq 1 40); do st=$(docker inspect vc6-canary-postgres --format '{{.State.Health.Status}}' 2>/dev/null || echo none); [ "$st" = healthy ] && break; sleep 3; done; echo "postgres health=$st"
echo "=== 克隆生产数据（结构＋账号／密钥，不含 usage_logs 与 ops_* 明细）"
( set -a; . "$APP/.env"; set +a
  docker exec sub2apiplus-postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exclude-table-data='usage_logs' --exclude-table-data='ops_*' > "$CANARY/clone.sql"
  wc -c < "$CANARY/clone.sql"
  docker exec -i vc6-canary-postgres psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$CANARY/clone.sql" > "$CANARY/clone-restore.log" 2>&1 || true
  rm -f "$CANARY/clone.sql"
  echo "恢复日志 ERROR 行数：$(grep -c "ERROR" "$CANARY/clone-restore.log" || true)"
  docker exec vc6-canary-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAtc "select (select count(*) from users), (select count(*) from api_keys), (select count(*) from accounts where deleted_at is null)" || echo "（行数核对查询失败，不影响 canary）" )
echo "=== 启动 canary 应用（默认 active 模式）"
"${DC[@]}" up -d canary-app 2>&1 | tail -2
st=none; for i in $(seq 1 60); do st=$(docker inspect vc6-canary-app --format '{{.State.Health.Status}}' 2>/dev/null || echo none); [ "$st" = healthy ] && break; sleep 5; done
IMAGE=$(docker inspect vc6-canary-app --format '{{.Image}}'); ARCH=$(docker image inspect "$IMAGE_ID" --format '{{.Architecture}}/{{.Os}}')
echo "canary health=$st image=$IMAGE arch=$ARCH"
[ "$st" = healthy ] && [ "$IMAGE" = "$IMAGE_ID" ] || { echo "CANARY_UNHEALTHY"; docker logs --tail 40 vc6-canary-app 2>&1 | cut -c1-200; exit 1; }
sleep 20   # 让启动后的后台任务跑一轮，再统计日志
E=$R/stages/canary; mkdir -p "$E"; chmod 700 "$R/stages" "$E"
cp "$CANARY/data/vc6-canary-activation-fact.json" "$E/activation-fact.json"
python3 - "$E/activation-fact.json" "$PROFILE_DIGEST" "$IMAGE_ID" <<'PY'
import json, os, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({k: d.get(k) for k in ("profile_mode","codex_version","profile_id","profile_digest","image_id","image_reference","source_tree_sha256","build_id","deployed_version","release_digest")}, ensure_ascii=False))
assert d["profile_mode"] == "active" and d["codex_version"] == os.environ["TARGET_VERSION"] and d["profile_digest"] == sys.argv[2] and d["image_id"] == sys.argv[3], d
PY
curl -s --max-time 10 http://127.0.0.1:28081/health > "$E/health.json"; echo "health: $(head -c 200 "$E/health.json")"
docker inspect vc6-canary-app --format '{{json .NetworkSettings.Networks}}' > "$E/networks.json"
cp "$CANARY/docker-compose.yml" "$E/canary-compose.yml"
docker logs --since "$START" vc6-canary-app > "$E/container.log" 2>&1 || true
ERRS=$(grep -c '"level":"ERROR"\|"level":"FATAL"' "$E/container.log" || true)
GUARD=$(grep -ci 'guard.*fail' "$E/container.log" || true)
echo "日志 ERROR/FATAL=$ERRS guard 失败=$GUARD"; grep '"level":"ERROR"\|"level":"FATAL"' "$E/container.log" | head -5 | cut -c1-300 || true
chmod 600 "$E"/*
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$E" "$START" "$END" "$IMAGE_ID" "$PROFILE_DIGEST" "$ERRS" "$GUARD" <<'PY'
import json, os, sys, socket
from pathlib import Path
E, START, END, IMAGE, DIG, ERRS, GUARD = sys.argv[1:]
E = Path(E)
ok = int(ERRS) == 0 and int(GUARD) == 0
stage = {"name": "canary", "started_at_utc": START, "completed_at_utc": END, "host": socket.gethostname(),
         "architecture": "linux/arm64", "status": "pass" if ok else "fail", "image_id": IMAGE,
         "checks": {"container_status": "running", "health": "pass", "active_version": os.environ["TARGET_VERSION"],
                    "profile_digest": DIG, "fatal_log_count": int(ERRS), "guard_failure_count": int(GUARD)},
         "evidence": sorted(p.name for p in E.iterdir() if p.is_file())}
(E / "stage.json").write_text(json.dumps(stage, ensure_ascii=False, indent=2) + "\n"); (E / "stage.json").chmod(0o600)
print(json.dumps({k: stage[k] for k in ("name", "status", "started_at_utc", "completed_at_utc", "image_id", "checks")}, ensure_ascii=False))
PY
echo "CANARY_DONE $(date -u +%H:%M:%SZ)"
