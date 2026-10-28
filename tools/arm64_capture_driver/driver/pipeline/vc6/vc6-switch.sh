#!/bin/bash
# VC-6 阶段切换驱动（ARM64 正式切换测试服务器）：
#   bash vc6-switch.sh production → 切到切换镜像（按晋升后 Catalog 的默认 active 运行，无强制 mode）
#   bash vc6-switch.sh rollback   → 切回 VC-5 前冻结的正式生产 compose 与镜像（env 的 PRODUCTION_IMAGE，Active＝本轮基线版本）
#   bash vc6-switch.sh restore    → 恢复切换镜像（目标恢复）
# R15 下容器替换一律经 egress-transition 严格模式（前置确认指定出口、事后等待准入）。每次切换后复核容器镜像、health、固定 IP、
# 出口守护状态、activation fact、日志错误数，并用采集专用 API key（env 的 API_KEY_ID）跑一次真实业务；阶段证据已存在时拒绝覆盖。
set -Eeuo pipefail; umask 077
# 本轮取值一律从参数文件现取（见同目录 vc6-env.sh），不写死 Campaign／候选／提交号／摘要／镜像号
source "$(dirname "${BASH_SOURCE[0]}")/vc6-env.sh"
# 业务验证模型：vc6-env.sh 缺省取 env 的 LITE_MODEL（gpt-6-astra，经 /v1/chat/completions 跨镜像实证最多，回滚镜像必然认识）；
# 主线 gpt-6.1-sol 从未经 chat completions 验证过，其出站已在 VC-5 用真实 /v1/responses 请求验证；可用环境变量 BUSINESS_MODEL 覆盖。
# 注意：本轮采集账号与 Key 是 env 的 CODEX_ACCOUNT_ID／API_KEY_ID），换轮时可能不同；业务验证前先核对该 Key 经 chat completions 能调业务模型。
MODE="${1:?production|rollback|restore}"
APP=$COMPOSE_DIR
BACKUP=$COMPOSE_BACKUP
test -s "$BACKUP" || { echo "缺 COMPOSE_BACKUP：${BACKUP}（VC-5 起跑前由流水线建）"; exit 2; }
D=${D:-/root/docker/capture-cli/data}; TOOLS=$D/tools/official_client_capture
W=$V6; R=$(cat "$W/vc6-root.txt")
A=$D/candidates/$CAND/production-artifacts
PROD_IMAGE_ID=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['docker_build']['image_id'])" "$A/production-build-parameters.json")
PROD_REF=${PROD_IMAGE_REPO}@$PROD_IMAGE_ID
BUILD_ID=$(cat "$W/vc6-build-id.txt")
TREE=$(cat "$W/vc6-tree.txt")
# ROLLBACK_IMAGE_ID、TARGET_DIGEST、ROLLBACK_DIGEST 由 vc6-env.sh 现取
case "$MODE" in
  production) STAGE=production_switch; EXPECT_IMAGE=$PROD_IMAGE_ID; EXPECT_VERSION=$TARGET_VERSION; EXPECT_DIGEST=$TARGET_DIGEST ;;
  restore)    STAGE=target_restore;    EXPECT_IMAGE=$PROD_IMAGE_ID; EXPECT_VERSION=$TARGET_VERSION; EXPECT_DIGEST=$TARGET_DIGEST ;;
  rollback)   STAGE=rollback;          EXPECT_IMAGE=$ROLLBACK_IMAGE_ID; EXPECT_VERSION=$ROLLBACK_VERSION; EXPECT_DIGEST=$ROLLBACK_DIGEST ;;
  *) echo "MODE 只能是 production|rollback|restore"; exit 2;;
esac
E=$R/stages/$STAGE; test ! -e "$E/stage.json" || { echo "阶段 $STAGE 已完成，拒绝覆盖"; exit 2; }
mkdir -p "$E"; chmod 700 "$R/stages" "$E"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo "STAGE=$STAGE START=$START"
cd "$APP"
cp docker-compose.yml "$E/docker-compose.before.yml"
cp "$BACKUP" docker-compose.yml
if [ "$MODE" != rollback ]; then
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
  "      # VC-6 §4.6.6：切换镜像，按晋升后 Catalog 的默认 active 运行（无强制 mode override）\n",
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
else
  echo "compose -> VC-5 前冻结的正式生产 compose（${PRODUCTION_IMAGE}）"
fi
cp docker-compose.yml "$E/docker-compose.used.yml"
docker compose config --quiet
python3 "$TOOLS/codex_upgrade_supervisor.py" egress-transition --container sub2apiplus --compose-service sub2api \
  -- docker compose -f "$APP/docker-compose.yml" up -d sub2api 2>&1 | tail -4
st=none; for i in $(seq 1 48); do st=$(docker inspect sub2apiplus --format '{{.State.Health.Status}}' 2>/dev/null || echo none); [ "$st" = healthy ] && break; sleep 5; done
IMAGE=$(docker inspect sub2apiplus --format '{{.Image}}')
echo "health=$st image=$IMAGE expect=$EXPECT_IMAGE"
[ "$st" = healthy ] && [ "$IMAGE" = "$EXPECT_IMAGE" ] || { echo "STAGE_FAILED"; docker logs --tail 30 sub2apiplus 2>&1 | cut -c1-200; exit 1; }
echo "=== 网络、挂载与出口守护复核"
docker inspect sub2apiplus --format '{{json .NetworkSettings.Networks}}' > "$E/networks.json"
python3 -c "import json;d=json.load(open('$E/networks.json'));ips={k:v['IPAddress'] for k,v in d.items()};print('ips:',ips);assert ips.get('sub2apiplus_sub2api-network')=='172.20.0.2' and ips.get('proxy-network')=='172.25.0.3', ips"
docker inspect sub2apiplus --format '{{json .Mounts}}' > "$E/mounts.json"
cp /run/sub2api-egress/status.json "$E/egress-status.json" 2>/dev/null && python3 -c "import json;d=json.load(open('$E/egress-status.json'));print('出口守护：',{k:d.get(k) for k in ('state','status','mode','updated_at_utc') if k in d})" || echo "（出口守护状态文件不可读）"
echo "=== activation fact"
if [ "$MODE" = rollback ]; then echo "(回滚镜像不落盘 VC-6 activation fact；以镜像 digest、Active Release 与画像摘要核对)"; else
  cp "$APP/data/${ACT_FACT_NAME}" "$E/activation-fact.json"
  python3 - "$E/activation-fact.json" "$EXPECT_VERSION" "$EXPECT_DIGEST" "$EXPECT_IMAGE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(json.dumps({k: d.get(k) for k in ("profile_mode","codex_version","profile_id","profile_digest","image_id","image_reference","source_tree_sha256","build_id","deployed_version","release_digest")}, ensure_ascii=False))
assert d["profile_mode"] == "active" and d["codex_version"] == sys.argv[2] and d["profile_digest"] == sys.argv[3] and d["image_id"] == sys.argv[4], d
PY
fi
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
python3 - "$E" "$STAGE" "$START" "$END" "$IMAGE" "$EXPECT_VERSION" "$EXPECT_DIGEST" "$ERRS" "$GUARD" <<'PY'
import json, sys, socket
from pathlib import Path
E, STAGE, START, END, IMAGE, VER, DIG, ERRS, GUARD = sys.argv[1:]
E = Path(E)
ok = int(ERRS) == 0 and int(GUARD) == 0
stage = {"name": STAGE, "started_at_utc": START, "completed_at_utc": END, "host": socket.gethostname(),
         "architecture": "linux/arm64", "status": "pass" if ok else "fail", "image_id": IMAGE,
         "checks": {"container_status": "running", "health": "pass", "active_version": VER,
                    "profile_digest": DIG, "fatal_log_count": int(ERRS), "guard_failure_count": int(GUARD)},
         "evidence": sorted(p.name for p in E.iterdir() if p.is_file())}
(E / "stage.json").write_text(json.dumps(stage, ensure_ascii=False, indent=2) + "\n"); (E / "stage.json").chmod(0o600)
print(json.dumps({k: stage[k] for k in ("name", "status", "started_at_utc", "completed_at_utc", "image_id", "checks")}, ensure_ascii=False))
PY
echo "STAGE_DONE $STAGE"
