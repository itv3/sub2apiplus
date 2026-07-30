#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# R11 正式 aux 全场景抓包外层包装。
#
# 使用方式（在 Vircs root shell 中）：
#   BATCH_ID=20260731T000000Z \
#   RUN_ID=formal-r11-aux-20260731T000000Z \
#   NORMAL_IMAGE_REF=sub2apiplus:...-r11 \
#   CAPTURE_IMAGE_REF=sub2apiplus:...-r11-capture \
#   bash .codex-formal-aux-r11.sh
#
# 本脚本不复制、安装或覆盖工具；live tools 与冻结 R11 source 必须先满足摘要门禁。

batch_id=${BATCH_ID:?必须提供正式 BATCH_ID}
run_id=${RUN_ID:?必须提供正式 RUN_ID}
normal_image_ref=${NORMAL_IMAGE_REF:?必须提供 NORMAL_IMAGE_REF}
capture_image_ref=${CAPTURE_IMAGE_REF:?必须提供 CAPTURE_IMAGE_REF}

normal_image_id=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
capture_image_id=sha256:54aee6e64177d2db210fd183f829aa90cfdb4ec7ed9cf3fdbfecb50c82473b64

if [[ ! $batch_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "BATCH_ID 格式非法。" >&2
  exit 2
fi
if [[ ! $run_id =~ ^formal-r11-aux-[A-Za-z0-9._-]+$ ||
      $run_id != *"$batch_id"* ]]; then
  echo "RUN_ID 必须是包含 BATCH_ID 的 formal-r11-aux-* 正式名称。" >&2
  exit 2
fi
if [[ $normal_image_ref == *[[:space:]]* || $normal_image_ref != *-r11 ]]; then
  echo "NORMAL_IMAGE_REF 必须是无空白、以 -r11 结尾的冻结引用。" >&2
  exit 2
fi
if [[ $capture_image_ref == *[[:space:]]* ||
      $capture_image_ref != *-r11-capture ]]; then
  echo "CAPTURE_IMAGE_REF 必须是无空白、以 -r11-capture 结尾的冻结引用。" >&2
  exit 2
fi

service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
redis_container=${REDIS_CONTAINER:-sub2apiplus-redis}
capture_container=${CAPTURE_CONTAINER:-capture-cli}

group_id=9
account_id=99
api_key_id=15
relay_port=18443
service_port=3001

app_dir=/root/Docker/sub2apiplus/app
base_compose="$app_dir/docker-compose.yml"
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
tool_root="$capture_root/tools/official_client_capture"
r11_source_root=${R11_SOURCE_ROOT:-/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source}
aux_script="$tool_root/run_candidate_aux_capture.sh"
work_dir="$capture_root/runs/$run_id"
runtime_dir="$capture_root/runtime/formal-r11-aux-$batch_id"
state_file="$runtime_dir/outer-baseline.json"
normal_override="$runtime_dir/normal.override.yml"
capture_override="$runtime_dir/capture.override.yml"
admin_token_file="$runtime_dir/admin.token"
jwt_runtime="$runtime_dir/jwtgen"
jwt_container=/tmp/formal-r11-aux-jwtgen
jwt_source="$capture_root/private-tools/codex0145-20260730T190054Z-r9/jwtgen"
jwt_source_sha256=051ed6ded09d81e9e40aeb70dc599fb3d445e66ebe9e371ef6ae96962d097562
proxy_name="candidate-aux-${run_id:0:72}"
expires_at=$(( $(date +%s) + 1100 ))

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

if [[ -e $runtime_dir || -e $work_dir ]]; then
  echo "正式 runtime 或 run 目录已存在，拒绝覆盖。" >&2
  exit 2
fi
install -d -m 0700 "$runtime_dir"

# 在完整恢复钩子完成定义前，前置门禁若失败只可能留下本脚本新建的空 runtime。
cleanup_unarmed_runtime() {
  local original_rc=$?
  trap - EXIT ERR INT TERM
  set +e
  [[ ! -e $admin_token_file ]] || shred -u -- "$admin_token_file"
  [[ ! -e $jwt_runtime ]] || shred -u -- "$jwt_runtime"
  [[ ! -e $normal_override ]] || unlink -- "$normal_override"
  [[ ! -e $capture_override ]] || unlink -- "$capture_override"
  [[ ! -e $state_file ]] || unlink -- "$state_file"
  rmdir "$runtime_dir" >/dev/null 2>&1 || true
  exit "$original_rc"
}
trap cleanup_unarmed_runtime EXIT ERR INT TERM

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
if [[ -z $db_user || -z $db_name ]]; then
  echo "无法读取 PostgreSQL 非敏感连接元数据。" >&2
  exit 1
fi

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
  echo "服务未在 120 秒内恢复 healthy。" >&2
  return 1
}

attestation_env_count() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/ {count++} END {print count+0}'
}

has_exact_env() {
  local wanted=$1
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk -v wanted="$wanted" '$0 == wanted {found=1} END {exit !found}'
}

mount_snapshot() {
  local container=$1
  docker inspect -f '{{json .Mounts}}' "$container" |
    python3 -c '
import hashlib, json, sys
value = json.load(sys.stdin)
raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
print(f"{len(value)}|{hashlib.sha256(raw).hexdigest()}")
'
}

semantic_hosts_sha256() {
  local hostname
  hostname=$(docker exec "$service_container" hostname)
  docker exec "$service_container" cat /etc/hosts |
    python3 -c '
import hashlib, re, sys
hostname = sys.argv[1]
lines = []
for raw in sys.stdin:
    value = " ".join(raw.split())
    if not value or value.startswith("#") or hostname in value:
        continue
    lines.append(value)
payload = ("\n".join(sorted(set(lines))) + "\n").encode()
print(hashlib.sha256(payload).hexdigest())
' "$hostname"
}

ca_sha256() {
  docker exec "$service_container" sha256sum \
    /etc/ssl/certs/ca-certificates.crt | awk '{print $1}'
}

no_capture_hosts() {
  ! docker exec "$service_container" grep -Eqi \
    '(^|[[:space:]])(chatgpt\.com|api\.openai\.com|auth\.openai\.com|region-candidate-0145\.oaiusercontent\.com)([[:space:]]|$)' \
    /etc/hosts
}

invalidate_auth_cache() {
  local deleted subscribers
  [[ $cache_key == apikey:auth:* ]] || return 1
  deleted=$(
    printf '%s' "$cache_key" |
      docker exec -i "$redis_container" redis-cli --raw -x DEL 2>/dev/null
  ) || return 1
  [[ $deleted =~ ^[01]$ ]] || return 1
  subscribers=$(
    printf '%s' "$cache_key" |
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

stop_owned_processes() {
  local pid_file pid signal
  [[ -d $work_dir ]] || return 0
  while IFS= read -r pid_file; do
    [[ -f $pid_file ]] || continue
    pid=$(tr -d '[:space:]' <"$pid_file")
    [[ $pid =~ ^[0-9]+$ ]] || continue
    signal=TERM
    [[ $pid_file == */pcap.pid ]] && signal=INT
    docker exec "$capture_container" kill "-$signal" "$pid" >/dev/null 2>&1 || true
  done < <(
    find "$work_dir" -type f \
      \( -path '*/relay-private/relay.pid' -o -name pcap.pid \) \
      -print 2>/dev/null || true
  )
}

baseline_ready=0
restore_armed=0
capture_complete=0
restore_failed=0
baseline_group=""
baseline_proxy=""
baseline_mapping=""
baseline_credentials_sha=""
baseline_counts=""
baseline_hosts_sha=""
baseline_ca_sha=""
baseline_keeper_running=false
postgres_id=""
redis_id=""
keeper_id=""
postgres_mount=""
redis_mount=""
keeper_mount=""
auth_digest=""
cache_key=""

write_formal_receipt() {
  local status=$1
  local final_group final_proxy final_counts
  final_group=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id=$group_id")
  final_proxy=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=$account_id")
  final_counts=$(db_query "
select (select count(*) from users)::text || '|' ||
       (select count(*) from groups)::text || '|' ||
       (select count(*) from accounts)::text || '|' ||
       (select count(*) from api_keys)::text || '|' ||
       (select count(*) from account_groups)::text || '|' ||
       (select count(*) from proxies)::text")
  python3 - "$work_dir/formal-wrapper-receipt.json" \
    "$batch_id" "$run_id" "$status" \
    "$normal_image_ref" "$normal_image_id" \
    "$capture_image_ref" "$capture_image_id" \
    "$baseline_group" "$final_group" \
    "$baseline_proxy" "$final_proxy" \
    "$baseline_counts" "$final_counts" \
    "$postgres_id" "$redis_id" "$keeper_id" \
    "$postgres_mount" "$redis_mount" "$keeper_mount" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    path, batch_id, run_id, status,
    normal_ref, normal_id, capture_ref, capture_id,
    before_group, after_group, before_proxy, after_proxy,
    before_counts, after_counts, postgres_id, redis_id, keeper_id,
    postgres_mount, redis_mount, keeper_mount,
) = sys.argv[1:]
payload = {
    "schema_version": "formal-r11-aux-wrapper/v1",
    "batch_id": batch_id,
    "run_id": run_id,
    "status": status,
    "images": {
        "normal": {"ref": normal_ref, "id": normal_id},
        "capture": {"ref": capture_ref, "id": capture_id},
    },
    "restoration": {
        "group_equal": before_group == after_group,
        "account_proxy_equal": before_proxy == after_proxy,
        "protected_table_counts_equal": before_counts == after_counts,
        "postgres_container_id": postgres_id,
        "redis_container_id": redis_id,
        "keeper_container_id": keeper_id,
        "postgres_mount_snapshot": postgres_mount,
        "redis_mount_snapshot": redis_mount,
        "keeper_mount_snapshot": keeper_mount,
    },
}
out = Path(path)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(out, 0o600)
PY
}

restore_environment() {
  local original_rc=$?
  local proxy_value fallback_value mapping_hex
  local group_platform group_oauth group_live group_images
  local current current_mapping current_credentials_sha current_counts
  local temp_proxy_count
  trap - EXIT ERR INT TERM
  set +e

  stop_owned_processes
  unlink_container_jwt >/dev/null 2>&1 || true

  if [[ $restore_armed == 1 ]]; then
    IFS='|' read -r proxy_value fallback_value <<<"$baseline_proxy"
    [[ $proxy_value == NULL || $proxy_value =~ ^[0-9]+$ ]] || restore_failed=1
    [[ $fallback_value == NULL || $fallback_value =~ ^[0-9]+$ ]] || restore_failed=1
    db_query "
update accounts
set proxy_id=$proxy_value, proxy_fallback_origin_id=$fallback_value
where id=$account_id" >/dev/null || restore_failed=1

    case "$baseline_mapping" in
      present:*)
        mapping_hex=${baseline_mapping#present:}
        db_query "
update accounts
set credentials=jsonb_set(
  coalesce(credentials,'{}'::jsonb), '{model_mapping}',
  convert_from(decode('$mapping_hex','hex'),'UTF8')::jsonb, true)
where id=$account_id" >/dev/null || restore_failed=1
        ;;
      missing:)
        db_query "
update accounts
set credentials=coalesce(credentials,'{}'::jsonb)-'model_mapping'
where id=$account_id" >/dev/null || restore_failed=1
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
where id=$group_id" >/dev/null || restore_failed=1
      invalidate_auth_cache || restore_failed=1
    else
      restore_failed=1
    fi

    if [[ $baseline_keeper_running == true ]]; then
      docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
    fi

    if [[ $(docker image inspect -f '{{.Id}}' "$normal_image_ref" 2>/dev/null) != \
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
from groups where id=$group_id" 2>/dev/null)
    [[ $current == "$baseline_group" ]] || restore_failed=1

    current=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=$account_id" 2>/dev/null)
    [[ $current == "$baseline_proxy" ]] || restore_failed=1

    current_mapping=$(db_query "
select case
  when credentials ? 'model_mapping' then 'present:' ||
    encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
  else 'missing:' end
from accounts where id=$account_id" 2>/dev/null)
    [[ $current_mapping == "$baseline_mapping" ]] || restore_failed=1

    current_credentials_sha=$(db_query "
select encode(sha256(convert_to(credentials::text,'UTF8')),'hex')
from accounts where id=$account_id" 2>/dev/null)
    [[ $current_credentials_sha == "$baseline_credentials_sha" ]] || restore_failed=1

    current_counts=$(db_query "
select (select count(*) from users)::text || '|' ||
       (select count(*) from groups)::text || '|' ||
       (select count(*) from accounts)::text || '|' ||
       (select count(*) from api_keys)::text || '|' ||
       (select count(*) from account_groups)::text || '|' ||
       (select count(*) from proxies)::text" 2>/dev/null)
    [[ $current_counts == "$baseline_counts" ]] || restore_failed=1

    temp_proxy_count=$(db_query \
      "select count(*) from proxies where name='$proxy_name'" 2>/dev/null)
    [[ $temp_proxy_count == 0 ]] || restore_failed=1
    auth_cache_absent || restore_failed=1

    [[ $(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null) == \
      "$postgres_id" ]] || restore_failed=1
    [[ $(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null) == \
      "$redis_id" ]] || restore_failed=1
    [[ $(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null) == \
      "$keeper_id" ]] || restore_failed=1
    [[ $(mount_snapshot "$postgres_container" 2>/dev/null) == \
      "$postgres_mount" ]] || restore_failed=1
    [[ $(mount_snapshot "$redis_container" 2>/dev/null) == \
      "$redis_mount" ]] || restore_failed=1
    [[ $(mount_snapshot "$keeper_container" 2>/dev/null) == \
      "$keeper_mount" ]] || restore_failed=1
    [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container" 2>/dev/null) == \
      "$baseline_keeper_running" ]] || restore_failed=1

    [[ $(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null) == \
      "$normal_image_id" ]] || restore_failed=1
    [[ $(docker inspect -f '{{.Config.Image}}' "$service_container" 2>/dev/null) == \
      "$normal_image_ref" ]] || restore_failed=1
    [[ $(attestation_env_count 2>/dev/null) == 0 ]] || restore_failed=1
    [[ $(ca_sha256 2>/dev/null) == "$baseline_ca_sha" ]] || restore_failed=1
    [[ $(semantic_hosts_sha256 2>/dev/null) == "$baseline_hosts_sha" ]] || restore_failed=1
    no_capture_hosts || restore_failed=1
    docker exec "$service_container" test ! -e \
      /usr/local/share/ca-certificates/candidate-aux-capture.crt \
      >/dev/null 2>&1 || restore_failed=1
  fi

  [[ ! -e $admin_token_file ]] || shred -u -- "$admin_token_file" || restore_failed=1
  [[ ! -e $jwt_runtime ]] || shred -u -- "$jwt_runtime" || restore_failed=1
  [[ ! -e $capture_override ]] || unlink -- "$capture_override" || restore_failed=1
  [[ ! -e $normal_override ]] || unlink -- "$normal_override" || restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    [[ -d $work_dir ]] && write_formal_receipt restoration_failed || true
    echo "正式 aux 外层恢复失败；基线保留在 $state_file，退出码 97。" >&2
    exit 97
  fi

  if [[ -d $work_dir ]]; then
    if [[ $capture_complete == 1 && $original_rc == 0 ]]; then
      write_formal_receipt accepted || restore_failed=1
    else
      write_formal_receipt capture_failed || restore_failed=1
    fi
  fi
  if [[ $restore_failed != 0 ]]; then
    echo "正式 aux receipt 写入失败；退出码 97。" >&2
    exit 97
  fi

  [[ ! -e $state_file ]] || unlink -- "$state_file"
  [[ ! -e $runtime_dir/capture-up.log ]] || unlink -- "$runtime_dir/capture-up.log"
  [[ ! -e $runtime_dir/normal-restore.log ]] || unlink -- "$runtime_dir/normal-restore.log"
  rmdir "$runtime_dir" >/dev/null 2>&1 || true

  if [[ $capture_complete == 1 && $original_rc == 0 ]]; then
    echo "正式 aux 抓包完成，R11 normal 与全部受保护状态已恢复。"
  else
    echo "正式 aux 抓包未完成，但外层状态已精确恢复。" >&2
  fi
  exit "$original_rc"
}

trap restore_environment EXIT ERR INT TERM

# 镜像、工具与 normal 运行态冻结门禁。
[[ -s $base_compose ]]
[[ $(docker image inspect -f '{{.Id}}' "$normal_image_ref") == "$normal_image_id" ]]
[[ $(docker image inspect -f '{{.Id}}' "$capture_image_ref") == "$capture_image_id" ]]
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$normal_image_id" ]]
[[ $(docker inspect -f '{{.Config.Image}}' "$service_container") == "$normal_image_ref" ]]
wait_healthy
[[ $(attestation_env_count) == 0 ]]
no_capture_hosts
docker exec "$service_container" test ! -e \
  /usr/local/share/ca-certificates/candidate-aux-capture.crt

for index in "${!tool_names[@]}"; do
  name=${tool_names[$index]}
  expected=${tool_hashes[$index]}
  live="$tool_root/$name"
  frozen="$r11_source_root/tools/official_client_capture/$name"
  [[ -f $live && ! -L $live && -f $frozen && ! -L $frozen ]]
  [[ $(sha256sum "$live" | awk '{print $1}') == "$expected" ]]
  [[ $(sha256sum "$frozen" | awk '{print $1}') == "$expected" ]]
done
[[ $(sha256sum "$jwt_source" | awk '{print $1}') == "$jwt_source_sha256" ]]

# 完整基线：业务状态、账号凭据摘要、数据容器 ID、挂载与保护表计数。
baseline_group=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id=$group_id")
[[ $baseline_group == 'composite|false|true|false' ]]

baseline_proxy=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=$account_id")
[[ $baseline_proxy == 'NULL|NULL' ]]

baseline_mapping=$(db_query "
select case
  when credentials ? 'model_mapping' then 'present:' ||
    encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
  else 'missing:' end
from accounts where id=$account_id")
[[ $baseline_mapping =~ ^(present:[0-9a-f]+|missing:)$ ]]

baseline_credentials_sha=$(db_query "
select encode(sha256(convert_to(credentials::text,'UTF8')),'hex')
from accounts where id=$account_id")
[[ $baseline_credentials_sha =~ ^[0-9a-f]{64}$ ]]

baseline_counts=$(db_query "
select (select count(*) from users)::text || '|' ||
       (select count(*) from groups)::text || '|' ||
       (select count(*) from accounts)::text || '|' ||
       (select count(*) from api_keys)::text || '|' ||
       (select count(*) from account_groups)::text || '|' ||
       (select count(*) from proxies)::text")
[[ $baseline_counts =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]

[[ $(db_query "select group_id from api_keys where id=$api_key_id") == "$group_id" ]]
[[ $(db_query "
select coalesce(string_agg(a.id::text,',' order by a.id),'')
from account_groups ag
join accounts a on a.id=ag.account_id
where ag.group_id=$group_id and a.platform='openai' and a.type='oauth'
  and a.status='active' and a.schedulable=true") == "$account_id" ]]

auth_digest=$(db_query "
select encode(sha256(convert_to(key,'UTF8')),'hex')
from api_keys
where id=$api_key_id and group_id=$group_id and status='active'")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]]
cache_key="apikey:auth:$auth_digest"

postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
postgres_mount=$(mount_snapshot "$postgres_container")
redis_mount=$(mount_snapshot "$redis_container")
keeper_mount=$(mount_snapshot "$keeper_container")
baseline_keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
[[ $baseline_keeper_running == true ]]
baseline_hosts_sha=$(semantic_hosts_sha256)
baseline_ca_sha=$(ca_sha256)
[[ $baseline_hosts_sha =~ ^[0-9a-f]{64}$ && $baseline_ca_sha =~ ^[0-9a-f]{64}$ ]]

python3 - "$state_file" \
  "$batch_id" "$run_id" "$baseline_group" "$baseline_proxy" \
  "$baseline_mapping" "$baseline_credentials_sha" "$baseline_counts" \
  "$postgres_id" "$redis_id" "$keeper_id" \
  "$postgres_mount" "$redis_mount" "$keeper_mount" \
  "$baseline_hosts_sha" "$baseline_ca_sha" <<'PY'
import json
import os
import sys
from pathlib import Path

keys = (
    "batch_id", "run_id", "group", "proxy", "model_mapping",
    "credentials_sha256", "protected_table_counts", "postgres_id",
    "redis_id", "keeper_id", "postgres_mount", "redis_mount",
    "keeper_mount", "hosts_semantic_sha256", "ca_sha256",
)
path = Path(sys.argv[1])
path.write_text(
    json.dumps(dict(zip(keys, sys.argv[2:])), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
baseline_ready=1
restore_armed=1

# 正常与 capture Compose 都由环境传入的冻结引用生成，不复用可漂移标签。
{
  printf '%s\n' \
    'services:' \
    '  sub2api:' \
    "    image: $normal_image_ref"
} >"$normal_override"
chmod 0600 "$normal_override"

{
  printf '%s\n' \
    'services:' \
    '  sub2api:' \
    "    image: $capture_image_ref" \
    '    environment:' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_MODE=synthetic-only' \
    '      - SUB2API_LIVE_ATTESTATION_CAPTURE_ACK=YES_I_ACCEPT_SYNTHETIC_ONLY' \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID=$api_key_id" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID=$group_id" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID=$account_id" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME=$proxy_name" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST=$capture_container" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT=$relay_port" \
    "      - SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX=$expires_at"
} >"$capture_override"
chmod 0600 "$capture_override"

(
  cd "$app_dir"
  docker compose -p sub2apiplus -f "$base_compose" -f "$normal_override" config -q
  docker compose -p sub2apiplus -f "$base_compose" -f "$capture_override" config -q
)

# JWT 仅进入 0600 文件；生成器在 normal 容器 /app 工作目录执行。
install -m 0700 "$jwt_source" "$jwt_runtime"
docker cp "$jwt_runtime" "$service_container:$jwt_container" >/dev/null
docker exec -u 0 -w /app "$service_container" "$jwt_container" |
  sed -n 's/^JWT=//p' >"$admin_token_file"
unlink_container_jwt
chmod 0600 "$admin_token_file"
[[ $(wc -l <"$admin_token_file") -eq 1 ]]
awk 'length($0) >= 8 && $0 ~ /^[A-Za-z0-9._~-]+$/ {ok=1} END {exit !ok}' \
  "$admin_token_file"

# 精确切换 group9；完整 cache key 同时 DEL L2 与 PUBLISH 清 L1。
changed=$(db_query "
with changed as (
  update groups
  set platform='openai', allow_image_generation=true
  where id=$group_id and platform='composite'
    and require_oauth_only=false and allow_live=true
    and allow_image_generation=false
  returning id
)
select count(*) from changed")
[[ $changed == 1 ]]
invalidate_auth_cache

(
  cd "$app_dir"
  docker compose -p sub2apiplus \
    -f "$base_compose" -f "$capture_override" \
    up -d --no-deps --force-recreate sub2api
) >"$runtime_dir/capture-up.log" 2>&1
wait_healthy
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$capture_image_id" ]]
[[ $(docker inspect -f '{{.Config.Image}}' "$service_container") == "$capture_image_ref" ]]
[[ $(attestation_env_count) == 9 ]]
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_MODE=synthetic-only'
has_exact_env 'SUB2API_LIVE_ATTESTATION_CAPTURE_ACK=YES_I_ACCEPT_SYNTHETIC_ONLY'
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID=$api_key_id"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID=$group_id"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID=$account_id"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME=$proxy_name"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST=$capture_container"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT=$relay_port"
has_exact_env "SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX=$expires_at"

ENABLE_CANDIDATE_AUX_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY \
RUN_ID="$run_id" ACCOUNT_ID="$account_id" API_KEY_ID="$api_key_id" \
ADMIN_BEARER_TOKEN_FILE="$admin_token_file" \
CAPTURE_ROOT="$capture_root" CAPTURE_CONTAINER="$capture_container" \
SERVICE_CONTAINER="$service_container" KEEPER_CONTAINER="$keeper_container" \
POSTGRES_CONTAINER="$postgres_container" REDIS_CONTAINER="$redis_container" \
SERVICE_PORT="$service_port" SERVICE_BASE_URL="http://127.0.0.1:$service_port" \
RELAY_PORT="$relay_port" MODEL=gpt-5.6-sol IMAGE_MODEL=gpt-image-2 \
"$aux_script"

# 冻结五场景动作计数，并对 A09/A14 原始脱敏上行字节做正式定向复核。
python3 - "$work_dir" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
summary = json.loads((root / "run-summary.json").read_text(encoding="utf-8"))
if summary.get("status") != "complete" or summary.get("exit_code") != 0:
    raise SystemExit("正式 aux run-summary 未完成")
if not all(summary.get("restoration", {}).values()):
    raise SystemExit("正式 aux 内层恢复核验失败")
expected_actions = {
    "A09": {"models_manifest": 1, "legacy_compact": 4, "alpha_search": 2,
             "images_generation": 1, "images_edit": 1},
    "A11": {"realtime_first_hop": 1, "realtime_sideband": 1},
    "A12": {"wham_usage": 1, "wham_credit_details": 1,
             "wham_safe_consume": 1},
    "A13": {"oauth_dummy_invalid_grant": 1},
    "A14": {"files_create": 1, "files_blob_put": 1, "files_uploaded": 1},
}
actual_actions = {
    item["scenario_id"]: item.get("actions", {})
    for item in summary.get("scenarios", [])
}
if actual_actions != expected_actions:
    raise SystemExit(f"正式 aux 动作计数错误：{actual_actions!r}")


def requests(scenario: str) -> list[dict]:
    result = []
    relay = root / "scenarios" / scenario / "relay"
    for path in sorted(relay.glob("conn*.client_to_upstream.bin")):
        raw = path.read_bytes()
        head, separator, body = raw.partition(b"\r\n\r\n")
        if not separator:
            continue
        lines = head.split(b"\r\n")
        parts = lines[0].decode("latin-1").split(" ")
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
            "method": parts[0], "target": parts[1], "headers": headers,
            "body": body[:length] if length else body,
        })
    return result


a09 = requests("A09")
compact = [r for r in a09 if r["target"] == "/backend-api/codex/responses/compact"]
if len(compact) != 4:
    raise SystemExit("A09 compact 必须恰好四次")
by_variant = {}
for request in compact:
    metadata = request["headers"].get("x-codex-turn-metadata", [])
    if len(metadata) != 1:
        raise SystemExit("A09 compact turn metadata 数量错误")
    variant = json.loads(metadata[0]).get("capture_variant")
    body = json.loads(request["body"])
    if body.get("text") != {"verbosity": "low"}:
        raise SystemExit(f"A09 {variant} compact text 不匹配")
    by_variant[variant] = request
if set(by_variant) != {"prime", "default", "beta", "turn_state"}:
    raise SystemExit("A09 compact variant 不完整")
for variant in ("prime", "default", "beta"):
    if "x-codex-turn-state" in by_variant[variant]["headers"]:
        raise SystemExit(f"A09 {variant} 提前携带 turn-state")
if by_variant["turn_state"]["headers"].get("x-codex-turn-state") != [
    "turn-state-candidate-aux-0145"
]:
    raise SystemExit("A09 turn-state 未自然闭环")

generation = next((r for r in a09
                   if r["target"] == "/backend-api/codex/images/generations"), None)
edit = next((r for r in a09
             if r["target"] == "/backend-api/codex/images/edits"), None)
if generation is None or edit is None:
    raise SystemExit("A09 images 两端点不完整")
generation_body = json.loads(generation["body"])
edit_body = json.loads(edit["body"])
if list(generation_body) != ["prompt", "background", "model", "quality", "size"]:
    raise SystemExit("A09 images generation 字段顺序错误")
if list(edit_body) != ["images", "prompt", "background", "model", "quality", "size"]:
    raise SystemExit("A09 images edit 字段顺序错误")
for body in (generation_body, edit_body):
    if body.get("background") != "auto" or body.get("quality") != "high":
        raise SystemExit("A09 images background/quality 不匹配")

a11_cleanup = root / "scenarios" / "A11" / "trigger" / "live-cleanup.txt"
if not a11_cleanup.is_file():
    raise SystemExit("A11 缺少 Live cleanup 证明")
cleanup_lines = set(a11_cleanup.read_text(encoding="utf-8").splitlines())
required_cleanup = {
    "controller_closed=true", "account_lease_released=true",
    "user_lease_released=true", "api_key_lease_released=true",
}
if not required_cleanup.issubset(cleanup_lines):
    raise SystemExit("A11 Live cleanup 证明不完整")

a14 = requests("A14")
files = [r for r in a14 if
         r["target"] == "/backend-api/files"
         or r["target"].startswith("/candidate-aux/file_candidate_aux_0145?")
         or r["target"] == "/backend-api/files/file_candidate_aux_0145/uploaded"]
if len(files) != 3:
    raise SystemExit("A14 Files 三段式不完整")
if any("user-agent" in request["headers"] for request in files):
    raise SystemExit("A14 Files 出站不得出现 User-Agent")

receipt = {
    "schema_version": "formal-r11-aux-wire-validation/v1",
    "accepted": True,
    "actions": actual_actions,
    "checks": {
        "a09_compact_text": True,
        "a09_turn_state": True,
        "a09_images_background_quality": True,
        "a11_cleanup": True,
        "a12_wham": True,
        "a13_invalid_grant": True,
        "a14_files_no_user_agent": True,
    },
}
path = root / "formal-wire-validation.json"
path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
PY

capture_complete=1
