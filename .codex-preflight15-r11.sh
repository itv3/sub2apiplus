#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# R11 candidate aux 全场景预检包装。
#
# 只执行 aux：A09/A11/A12/A13/A14 已覆盖本轮需要复核的 compact、图片、
# turn-state、Live 与 Files 三段式；core 工具只做 live/source 双重摘要门禁，
# 本轮不重复 core 抓包，也不再同步已经冻结到 Vircs 的工具。

batch_id=${BATCH_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
if [[ ! $batch_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "BATCH_ID 格式非法。" >&2
  exit 2
fi

ssh Vircs 'bash -s' -- "$batch_id" <<'REMOTE'
set -Eeuo pipefail
set +x
umask 077

batch_id=$1
[[ $batch_id =~ ^[A-Za-z0-9._-]+$ ]]

service_container=sub2apiplus
keeper_container=sub2apiplus-keeper
postgres_container=sub2apiplus-postgres
redis_container=sub2apiplus-redis
capture_container=capture-cli

app_dir=/root/Docker/sub2apiplus/app
base_compose="$app_dir/docker-compose.yml"
normal_override=/root/Docker/sub2apiplus/deployments/codex0145-20260730T195700Z-r11/image.override.yml
normal_image=sub2apiplus:codex0145-20260730T195700Z-f9bd704c2e75-r11
normal_image_id=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
capture_image=sub2apiplus:codex0145-20260730T195700Z-f9bd704c2e75-r11-capture
capture_image_id=sha256:54aee6e64177d2db210fd183f829aa90cfdb4ec7ed9cf3fdbfecb50c82473b64

capture_root=/root/oauth-capture
tool_root="$capture_root/tools/official_client_capture"
r11_source_root=/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source
run_id="candidate-aux-isolated-preflight15-$batch_id-r11"
work_dir="$capture_root/runs/$run_id"
aux_script="$tool_root/run_candidate_aux_capture.sh"
jwt_source="$capture_root/private-tools/codex0145-20260730T190054Z-r9/jwtgen"
jwt_source_sha256=051ed6ded09d81e9e40aeb70dc599fb3d445e66ebe9e371ef6ae96962d097562

tool_names=(
  run_candidate_core_capture.sh
  run_candidate_aux_capture.sh
  upstream_byte_relay.py
  drive_candidate_gateway_ws.py
  scrub_raw_bytes.py
)
tool_hashes=(
  3c7439376a3168052e2dbbc750704675f43043e5e827ede95a70855d5f7410cf
  b9fa106ed65c66de8b95595e1333691cf36021124a029414f326a050248675ba
  a5f911f1f28d679cc2b6eef32a9fa750c4aa893da4292cb5521ce6947e8ad511
  7f3dbf4ea7a0fb06a56d404bc754512c38ca268a46abd9c943f9ecf5f9ac78df
  92154026b091d6ef84af4708c277dc3a6669fc9171fd144198e537aac9515f62
)

# 已由上层同步完成；这里只接受 live tools 与冻结 R11 source 摘要完全一致的集合。
for index in "${!tool_names[@]}"; do
  name=${tool_names[$index]}
  [[ -f $tool_root/$name && ! -L $tool_root/$name ]]
  [[ -f $r11_source_root/tools/official_client_capture/$name &&
     ! -L $r11_source_root/tools/official_client_capture/$name ]]
  [[ $(sha256sum "$tool_root/$name" | awk '{print $1}') == "${tool_hashes[$index]}" ]]
  [[ $(sha256sum "$r11_source_root/tools/official_client_capture/$name" | awk '{print $1}') == \
    "${tool_hashes[$index]}" ]]
done

runtime_dir=$(mktemp -d -p "$capture_root/runtime" preflight15-r11.XXXXXX)
capture_override="$runtime_dir/capture.override.yml"
admin_token_file="$runtime_dir/admin.token"
jwt_runtime="$runtime_dir/jwtgen"
jwt_container=/tmp/candidate-preflight15-r11-jwtgen
proxy_name="candidate-aux-${run_id:0:72}"
expires_at=$(( $(date +%s) + 1100 ))

baseline_ready=0
group_restore_armed=0
account_restore_armed=0
normal_restore_armed=0
restore_failed=0
auth_digest=""
cache_key=""
baseline_group=""
baseline_proxy=""
baseline_mapping=""
baseline_keeper_running=false
postgres_id=""
redis_id=""
keeper_id=""

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
[[ -n $db_user && -n $db_name ]]

db_query() {
  docker exec "$postgres_container" \
    psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -qAtc "$1"
}

wait_healthy() {
  local health
  for _ in $(seq 1 120); do
    health=$(docker inspect -f \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$service_container" 2>/dev/null || true)
    [[ $health == healthy ]] && return 0
    sleep 1
  done
  return 1
}

has_capture_env() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/ {found=1} END {exit !found}'
}

has_exact_env() {
  local wanted=$1
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk -v wanted="$wanted" '$0 == wanted {found=1} END {exit !found}'
}

# Redis L2 使用完整键；Pub/Sub 消息按服务实现只发送 SHA-256 摘要，
# 因为进程内 L1 的索引不带 `apikey:auth:` 前缀。
invalidate_auth_cache() {
  local deleted subscribers
  [[ $cache_key == apikey:auth:* ]]
  deleted=$(
    printf '%s' "$cache_key" |
      docker exec -i "$redis_container" redis-cli --raw -x DEL 2>/dev/null
  ) || return 1
  [[ $deleted =~ ^[01]$ ]] || return 1
  subscribers=$(
    printf '%s' "$auth_digest" |
      docker exec -i "$redis_container" redis-cli --raw -x \
        PUBLISH auth:cache:invalidate 2>/dev/null
  ) || return 1
  [[ $subscribers =~ ^[0-9]+$ && $subscribers -ge 1 ]]
}

auth_cache_absent() {
  local exists
  exists=$(
    printf '%s' "$cache_key" |
      docker exec -i "$redis_container" redis-cli --raw -x EXISTS 2>/dev/null
  ) || return 1
  [[ $exists == 0 ]]
}

unlink_container_jwt() {
  docker exec -u 0 "$service_container" sh -c \
    'test ! -e "$1" || unlink "$1"' sh "$jwt_container"
}

restore_environment() {
  local original_rc=$?
  local proxy_value fallback_value mapping_hex
  local group_platform group_oauth group_live group_images
  local current temp_proxy_count
  trap - EXIT INT TERM
  set +e

  unlink_container_jwt >/dev/null 2>&1 || true

  if [[ $account_restore_armed == 1 ]]; then
    IFS='|' read -r proxy_value fallback_value <<<"$baseline_proxy"
    [[ $proxy_value == NULL || $proxy_value =~ ^[0-9]+$ ]] || restore_failed=1
    [[ $fallback_value == NULL || $fallback_value =~ ^[0-9]+$ ]] || restore_failed=1
    db_query "
update accounts
set proxy_id=$proxy_value, proxy_fallback_origin_id=$fallback_value
where id=99" >/dev/null || restore_failed=1

    case "$baseline_mapping" in
      present:*)
        mapping_hex=${baseline_mapping#present:}
        db_query "
update accounts
set credentials=jsonb_set(
  coalesce(credentials,'{}'::jsonb), '{model_mapping}',
  convert_from(decode('$mapping_hex','hex'),'UTF8')::jsonb, true)
where id=99" >/dev/null || restore_failed=1
        ;;
      missing:)
        db_query "
update accounts
set credentials=coalesce(credentials,'{}'::jsonb)-'model_mapping'
where id=99" >/dev/null || restore_failed=1
        ;;
      *) restore_failed=1 ;;
    esac

    db_query "
delete from proxies
where name='$proxy_name'
  and not exists (
    select 1 from accounts
    where proxy_id=proxies.id or proxy_fallback_origin_id=proxies.id
  )" >/dev/null || restore_failed=1
  fi

  if [[ $group_restore_armed == 1 ]]; then
    IFS='|' read -r group_platform group_oauth group_live group_images \
      <<<"$baseline_group"
    if [[ $group_platform =~ ^[a-z0-9_-]+$ &&
          $group_oauth =~ ^(true|false)$ &&
          $group_live =~ ^(true|false)$ &&
          $group_images =~ ^(true|false)$ ]]; then
      db_query "
update groups
set platform='$group_platform',
    require_oauth_only=$group_oauth,
    allow_live=$group_live,
    allow_image_generation=$group_images
where id=9" >/dev/null || restore_failed=1
      invalidate_auth_cache || restore_failed=1
    else
      restore_failed=1
    fi
  fi

  if [[ $baseline_keeper_running == true ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
  fi

  if [[ $normal_restore_armed == 1 ]]; then
    if [[ $(docker image inspect -f '{{.Id}}' "$normal_image" 2>/dev/null) != \
      "$normal_image_id" ]]; then
      restore_failed=1
    else
      (
        cd "$app_dir"
        docker compose -p sub2apiplus \
          -f "$base_compose" -f "$normal_override" \
          up -d --no-deps --force-recreate sub2api
      ) >"$runtime_dir/normal-restore.log" 2>&1 || restore_failed=1
      wait_healthy || restore_failed=1
    fi
  fi

  if [[ $baseline_ready == 1 ]]; then
    current=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id=9" 2>/dev/null)
    [[ $current == "$baseline_group" ]] || restore_failed=1

    current=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=99" 2>/dev/null)
    [[ $current == "$baseline_proxy" ]] || restore_failed=1

    current=$(db_query "
select case
  when credentials ? 'model_mapping' then 'present:' ||
    encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
  else 'missing:' end
from accounts where id=99" 2>/dev/null)
    [[ $current == "$baseline_mapping" ]] || restore_failed=1

    temp_proxy_count=$(db_query \
      "select count(*) from proxies where name='$proxy_name'" 2>/dev/null)
    [[ $temp_proxy_count == 0 ]] || restore_failed=1
    auth_cache_absent || restore_failed=1
  fi

  [[ $(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null) == \
    "$normal_image_id" ]] || restore_failed=1
  has_capture_env && restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null) == \
    "$postgres_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null) == \
    "$redis_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null) == \
    "$keeper_id" ]] || restore_failed=1
  [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container" 2>/dev/null) == \
    "$baseline_keeper_running" ]] || restore_failed=1
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-aux-capture.crt \
    >/dev/null 2>&1 || restore_failed=1

  [[ ! -e $admin_token_file ]] || shred -u -- "$admin_token_file" || restore_failed=1
  [[ ! -e $jwt_runtime ]] || shred -u -- "$jwt_runtime" || restore_failed=1
  [[ ! -e $capture_override ]] || unlink -- "$capture_override" || restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    echo "preflight15 R11 外层恢复或状态核验失败；保留 $runtime_dir。" >&2
    exit 97
  fi
  [[ ! -e $runtime_dir/normal-restore.log ]] ||
    unlink -- "$runtime_dir/normal-restore.log"
  rmdir "$runtime_dir" >/dev/null 2>&1 || true
  echo "preflight15 R11 已恢复 normal；PG/Redis/Keeper ID 与基线一致。"
  exit "$original_rc"
}

trap restore_environment EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 冻结镜像与运行态前置门禁。
[[ ! -e $work_dir ]]
[[ -s $base_compose && -s $normal_override ]]
[[ $(docker image inspect -f '{{.Id}}' "$normal_image") == "$normal_image_id" ]]
[[ $(docker image inspect -f '{{.Id}}' "$capture_image") == "$capture_image_id" ]]
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$normal_image_id" ]]
wait_healthy
! has_capture_env
[[ $(sha256sum "$jwt_source" | awk '{print $1}') == "$jwt_source_sha256" ]]

baseline_group=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id=9")
[[ $baseline_group == 'composite|false|true|false' ]]

baseline_proxy=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=99")
[[ $baseline_proxy == 'NULL|NULL' ]]

baseline_mapping=$(db_query "
select case
  when credentials ? 'model_mapping' then 'present:' ||
    encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
  else 'missing:' end
from accounts where id=99")
[[ $baseline_mapping =~ ^(present:[0-9a-f]+|missing:)$ ]]

[[ $(db_query "select group_id from api_keys where id=15") == 9 ]]
[[ $(db_query "
select coalesce(string_agg(a.id::text,',' order by a.id),'')
from account_groups ag
join accounts a on a.id=ag.account_id
where ag.group_id=9 and a.platform='openai' and a.type='oauth'
  and a.status='active' and a.schedulable=true
") == 99 ]]

# PostgreSQL 仅向 shell 返回摘要，不读取 API Key 原文。
auth_digest=$(db_query "
select encode(sha256(convert_to(key,'UTF8')),'hex')
from api_keys
where id=15 and group_id=9 and status='active'")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]]
cache_key="apikey:auth:$auth_digest"

postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
baseline_keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
[[ $baseline_keeper_running == true ]]
baseline_ready=1

# JWT 仅通过 0600 文件传入 aux；不进入 argv、环境或日志。
install -m 0700 "$jwt_source" "$jwt_runtime"
docker cp "$jwt_runtime" "$service_container:$jwt_container" >/dev/null
docker exec -u 0 -w /app "$service_container" "$jwt_container" |
  sed -n 's/^JWT=//p' >"$admin_token_file"
unlink_container_jwt
chmod 0600 "$admin_token_file"
[[ $(wc -l <"$admin_token_file") -eq 1 ]]
awk 'length($0) >= 8 && $0 ~ /^[A-Za-z0-9._~-]+$/ {ok=1} END {exit !ok}' \
  "$admin_token_file"

# 临时开放 OpenAI + Images；DEL L2、发布完整 key 清 L1，随后 capture 重建再次清空 L1。
group_restore_armed=1
changed=$(db_query "
with changed as (
  update groups
  set platform='openai', allow_image_generation=true
  where id=9 and platform='composite'
    and require_oauth_only=false and allow_live=true
    and allow_image_generation=false
  returning id
)
select count(*) from changed")
[[ $changed == 1 ]]
invalidate_auth_cache

{
  printf '%s\n' \
    'services:' \
    '  sub2api:' \
    "    image: $capture_image" \
    '    environment:' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_MODE=synthetic-only' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_ACK=YES_I_ACCEPT_SYNTHETIC_ONLY' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID=15' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID=9' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID=99' \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME=$proxy_name" \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST=capture-cli' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT=18443' \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX=$expires_at"
} >"$capture_override"
chmod 0600 "$capture_override"

(
  cd "$app_dir"
  docker compose -p sub2apiplus \
    -f "$base_compose" -f "$capture_override" config -q
)

account_restore_armed=1
normal_restore_armed=1
(
  cd "$app_dir"
  docker compose -p sub2apiplus \
    -f "$base_compose" -f "$capture_override" \
    up -d --no-deps --force-recreate sub2api
) >"$runtime_dir/capture-up.log" 2>&1
wait_healthy
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$capture_image_id" ]]
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_MODE=synthetic-only'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_ACK=YES_I_ACCEPT_SYNTHETIC_ONLY'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID=15'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID=9'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID=99'
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME=$proxy_name"
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST=capture-cli'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT=18443'
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX=$expires_at"

ENABLE_CANDIDATE_AUX_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY \
RUN_ID="$run_id" ACCOUNT_ID=99 API_KEY_ID=15 \
ADMIN_BEARER_TOKEN_FILE="$admin_token_file" \
CAPTURE_ROOT="$capture_root" CAPTURE_CONTAINER="$capture_container" \
SERVICE_CONTAINER="$service_container" KEEPER_CONTAINER="$keeper_container" \
POSTGRES_CONTAINER="$postgres_container" REDIS_CONTAINER="$redis_container" \
SERVICE_PORT=3001 SERVICE_BASE_URL=http://127.0.0.1:3001 \
RELAY_PORT=18443 MODEL=gpt-5.6-sol IMAGE_MODEL=gpt-image-2 \
"$aux_script"

# 对脱敏后的真实上行字节做额外定向验收，不只依赖动作计数。
python3 - "$work_dir" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))
if summary.get("status") != "complete" or summary.get("exit_code") != 0:
    raise SystemExit("aux run-summary 未完成")
if not all(summary.get("restoration", {}).values()):
    raise SystemExit("aux 内层恢复核验失败")


def requests(scenario: str) -> list[dict]:
    result = []
    relay = root / "scenarios" / scenario / "relay"
    for path in sorted(relay.glob("conn*.client_to_upstream.bin")):
        raw = path.read_bytes()
        head, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            continue
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin-1")
        parts = request_line.split(" ")
        if len(parts) != 3:
            continue
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers.setdefault(name.decode("ascii").lower(), []).append(
                value.decode("latin-1").strip()
            )
        length = int(headers.get("content-length", ["0"])[0])
        result.append({
            "file": path.name,
            "method": parts[0],
            "target": parts[1],
            "headers": headers,
            "body": body[:length] if length else body,
        })
    return result


a09 = requests("A09")
compact = [item for item in a09 if item["target"] == "/backend-api/codex/responses/compact"]
if len(compact) != 4:
    raise SystemExit(f"A09 compact 数量错误：{len(compact)}")

by_variant = {}
for item in compact:
    metadata_values = item["headers"].get("x-codex-turn-metadata", [])
    if len(metadata_values) != 1:
        raise SystemExit("A09 compact 缺少唯一 turn metadata")
    variant = json.loads(metadata_values[0]).get("capture_variant")
    body = json.loads(item["body"])
    if body.get("text") != {"verbosity": "low"}:
        raise SystemExit(f"A09 {variant} compact text 不匹配")
    by_variant[variant] = item

if set(by_variant) != {"prime", "default", "beta", "turn_state"}:
    raise SystemExit(f"A09 compact variant 不完整：{sorted(by_variant)}")
expected_turn_state = "turn-state-candidate-aux-0145"
for variant in ("prime", "default", "beta"):
    if "x-codex-turn-state" in by_variant[variant]["headers"]:
        raise SystemExit(f"A09 {variant} 不应提前携带 turn-state")
if by_variant["turn_state"]["headers"].get("x-codex-turn-state") != [expected_turn_state]:
    raise SystemExit("A09 turn-state 未由 beta 响应自然回放")

generation = next(
    (item for item in a09 if item["target"] == "/backend-api/codex/images/generations"),
    None,
)
edit = next(
    (item for item in a09 if item["target"] == "/backend-api/codex/images/edits"),
    None,
)
if generation is None or edit is None:
    raise SystemExit("A09 images 两端点不完整")
generation_body = json.loads(generation["body"])
edit_body = json.loads(edit["body"])
if list(generation_body) != ["prompt", "background", "model", "quality", "size"]:
    raise SystemExit(f"images generation 字段顺序错误：{list(generation_body)}")
if list(edit_body) != ["images", "prompt", "background", "model", "quality", "size"]:
    raise SystemExit(f"images edit 字段顺序错误：{list(edit_body)}")
for label, body in (("generation", generation_body), ("edit", edit_body)):
    if body.get("background") != "auto" or body.get("quality") != "high":
        raise SystemExit(f"images {label} background/quality 不匹配")

a14 = requests("A14")
files = [
    item for item in a14
    if item["target"] == "/backend-api/files"
    or item["target"].startswith("/candidate-aux/file_candidate_aux_0145?")
    or item["target"] == "/backend-api/files/file_candidate_aux_0145/uploaded"
]
if len(files) != 3:
    raise SystemExit(f"A14 Files 三段式数量错误：{len(files)}")
for item in files:
    if "user-agent" in item["headers"]:
        raise SystemExit(f"A14 不应出现 User-Agent：{item['target']}")

receipt = {
    "schema_version": "candidate-preflight15-r11/v1",
    "accepted": True,
    "checks": {
        "a09_compact_text": True,
        "a09_images_background_quality": True,
        "a09_turn_state_chain": True,
        "a14_files_no_user_agent": True,
    },
}
receipt_path = root / "preflight15-validation.json"
receipt_path.write_text(
    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(receipt_path, 0o600)
print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
PY

# capture-up.log 不含秘密，成功后精确删除；证据目录完整保留。
[[ ! -e $runtime_dir/capture-up.log ]] || unlink -- "$runtime_dir/capture-up.log"
REMOTE
