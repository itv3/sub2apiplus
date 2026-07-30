#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# 真实 Kilo 入口的候选出站字节抓包。
#
# 本脚本只把 OpenAI OAuth #90 的出站临时导向不具备生产转发能力的冻结 relay，
# 并等待人工在 Kilo 中分别发出一条 Compatible 与一条 Responses 请求。所有账号、
# CA、认证缓存和 keeper 状态均由同一个 EXIT trap 精确恢复；PostgreSQL/Redis 容器
# 从不重建，避免把抓包动作扩大成部署动作。

required_gate=YES_I_ACCEPT_KILO_SYNTHETIC_ONLY
if [[ ${ENABLE_KILO_SYNTHETIC_CAPTURE:-} != "$required_gate" ]]; then
  echo "拒绝启动：必须显式设置 ENABLE_KILO_SYNTHETIC_CAPTURE=$required_gate。" >&2
  exit 2
fi

run_id=${RUN_ID:?必须提供 RUN_ID}
if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

service_container=${SERVICE_CONTAINER:-sub2apiplus}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
redis_container=${REDIS_CONTAINER:-sub2apiplus-redis}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
capture_container=${CAPTURE_CONTAINER:-capture-cli}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_mount=${CAPTURE_MOUNT:-/capture}
account_id=${ACCOUNT_ID:-90}
competing_account_id=${COMPETING_ACCOUNT_ID:-95}
api_key_id=${API_KEY_ID:-1}
group_id=${GROUP_ID:-8}
relay_port=${RELAY_PORT:-18443}

for numeric in "$account_id" "$competing_account_id" "$api_key_id" "$group_id" "$relay_port"; do
  if [[ ! $numeric =~ ^[0-9]+$ ]]; then
    echo "账号、Key、分组和端口参数必须为正整数。" >&2
    exit 2
  fi
done

work_dir="$capture_root/runs/$run_id"
byte_root="$work_dir/byte"
container_byte_root="$capture_mount/runs/$run_id/byte"
tls_dir="$byte_root/tls-private"
container_tls_dir="$container_byte_root/tls-private"
runtime_dir="$capture_root/runtime/kilo-byte-$run_id"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/kilo-r11-capture.crt
relay_tool="$capture_mount/tools/official_client_capture/upstream_byte_relay.py"
scrub_tool="$capture_root/tools/official_client_capture/scrub_raw_bytes.py"

if [[ -e $byte_root || -e $runtime_dir ]]; then
  echo "抓包或恢复目录已存在，拒绝覆盖：$byte_root / $runtime_dir" >&2
  exit 2
fi
install -d -m 0700 "$work_dir" "$byte_root" "$tls_dir" "$runtime_dir"

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
if [[ -z $db_user || -z $db_name ]]; then
  echo "无法读取 PostgreSQL 连接元数据。" >&2
  exit 1
fi

db_query() {
  docker exec "$postgres_container" psql -U "$db_user" -d "$db_name" -qAtc "$1"
}

wait_healthy() {
  local health
  for _ in $(seq 1 90); do
    health=$(docker inspect -f \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$service_container" 2>/dev/null || true)
    if [[ $health == healthy || $health == running ]]; then
      return 0
    fi
    sleep 1
  done
  echo "主服务未在 90 秒内恢复健康。" >&2
  return 1
}

restart_service() {
  docker restart "$service_container" >/dev/null
  wait_healthy
}

invalidate_auth_cache() {
  printf 'apikey:auth:%s' "$auth_digest" |
    docker exec -i "$redis_container" redis-cli -x DEL >/dev/null
  printf '%s' "$auth_digest" |
    docker exec -i "$redis_container" redis-cli -x PUBLISH auth:cache:invalidate >/dev/null
}

hash_group_mapping() {
  db_query "
select ag.account_id || '|' || ag.priority
from account_groups ag
where ag.group_id = $group_id
order by ag.account_id" | sha256sum | awk '{print $1}'
}

hash_container_mounts() {
  docker inspect -f '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Source}}|{{.Destination}}|{{.RW}}{{println}}{{end}}' "$1" |
    sort | sha256sum | awk '{print $1}'
}

stop_container_process() {
  local pid=$1
  local first_signal=$2
  local label=$3
  local attempts=${4:-60}
  if [[ ! $pid =~ ^[0-9]+$ ]]; then
    echo "$label 的 PID 非法。" >&2
    restore_failed=1
    return 1
  fi
  docker exec "$capture_container" kill "-$first_signal" "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 "$attempts"); do
    if ! docker exec "$capture_container" kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  docker exec "$capture_container" kill -KILL "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if ! docker exec "$capture_container" kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "$label 在强制终止后仍存活。" >&2
  restore_failed=1
  return 1
}

current_scenario=""
relay_started=0
pcap_started=0

start_capture() {
  local scenario_id=$1
  local relay_scenario=$2
  local scenario_root="$byte_root/scenarios/$scenario_id"
  local container_scenario_root="$container_byte_root/scenarios/$scenario_id"

  if docker exec "$capture_container" sh -c \
    "ss -lnt | grep -q ':$relay_port '"; then
    echo "relay 端口 $relay_port 已被占用。" >&2
    return 1
  fi
  install -d -m 0700 \
    "$scenario_root/relay-private" "$scenario_root/relay" "$scenario_root/trigger"
  docker exec "$capture_container" mkdir -p \
    "$container_scenario_root/relay-private" "$container_scenario_root/trigger"
  current_scenario=$scenario_id

  docker exec "$capture_container" sh -c '
    umask 077
    python3 "$1" --cert "$2" --key "$3" --mode connect --port "$4" \
      --upstream-host chatgpt.com --output "$5" --timeout 600 \
      --synthetic-profile candidate-core-v1 --allow-synthetic-responses \
      --candidate-core-scenario "$6" \
      >"$7" 2>&1 &
    echo $! >"$8"
  ' sh "$relay_tool" "$container_tls_dir/relay.crt" "$container_tls_dir/relay.key" \
    "$relay_port" "$container_scenario_root/relay-private" "$relay_scenario" \
    "$container_scenario_root/relay.log" "$container_scenario_root/relay.pid"
  relay_started=1

  docker exec "$capture_container" sh -c '
    umask 077
    tcpdump -U -n -s 0 -i any port "$1" -w "$2" >"$3" 2>&1 &
    echo $! >"$4"
  ' sh "$relay_port" "$container_scenario_root/egress.pcap" \
    "$container_scenario_root/tcpdump.log" "$container_scenario_root/pcap.pid"
  pcap_started=1

  for _ in $(seq 1 50); do
    if docker exec "$capture_container" sh -c \
      "ss -lnt | grep -q ':$relay_port '"; then
      return 0
    fi
    sleep 0.1
  done
  echo "$scenario_id relay 未就绪。" >&2
  return 1
}

wait_action() {
  local scenario_id=$1
  local action=$2
  local minimum=${3:-1}
  local path="$byte_root/scenarios/$scenario_id/relay-private/intervention.jsonl"
  local count
  for _ in $(seq 1 6000); do
    count=0
    if [[ -f $path ]]; then
      count=$(grep -c "\"action\": \"$action\"" "$path" 2>/dev/null || true)
    fi
    if (( count >= minimum )); then
      return 0
    fi
    sleep 0.1
  done
  echo "$scenario_id 未在 10 分钟内观察到 $action。" >&2
  return 1
}

stop_capture() {
  local scenario_id scenario_root container_scenario_root pid
  if [[ -z $current_scenario ]]; then
    return 0
  fi
  scenario_id=$current_scenario
  scenario_root="$byte_root/scenarios/$scenario_id"
  container_scenario_root="$container_byte_root/scenarios/$scenario_id"

  if [[ $relay_started == 1 ]]; then
    pid=$(docker exec "$capture_container" sh -c 'cat "$1" 2>/dev/null || true' sh \
      "$container_scenario_root/relay.pid")
    stop_container_process "$pid" TERM "$scenario_id relay" 80
    relay_started=0
  fi
  if [[ $pcap_started == 1 ]]; then
    sleep 1
    pid=$(docker exec "$capture_container" sh -c 'cat "$1" 2>/dev/null || true' sh \
      "$container_scenario_root/pcap.pid")
    stop_container_process "$pid" INT "$scenario_id tcpdump" 60
    pcap_started=0
  fi
  current_scenario=""

  for _ in $(seq 1 50); do
    [[ -s $scenario_root/relay-private/relay.json ]] && break
    sleep 0.1
  done
  if [[ ! -s $scenario_root/relay-private/relay.json ]]; then
    echo "$scenario_id 缺少 relay.json。" >&2
    return 1
  fi
  if [[ ! -s $scenario_root/egress.pcap ]] ||
    (( $(stat -c '%s' "$scenario_root/egress.pcap") <= 24 )); then
    echo "$scenario_id 缺少有效 pcap。" >&2
    return 1
  fi
  python3 "$scrub_tool" \
    --src "$scenario_root/relay-private" \
    --dst "$scenario_root/relay" \
    --verify >"$scenario_root/scrub.log"
  for name in intervention.jsonl sni.log; do
    if [[ -f $scenario_root/relay-private/$name ]]; then
      install -m 0600 "$scenario_root/relay-private/$name" "$scenario_root/relay/$name"
    fi
  done
  case "$scenario_root/relay-private" in
    "$byte_root"/scenarios/*/relay-private)
      rm -rf -- "$scenario_root/relay-private"
      ;;
    *)
      echo "拒绝删除未验证的 relay 私有目录。" >&2
      return 1
      ;;
  esac
}

write_summary() {
  local final_status=$1
  local exit_code=$2
  python3 - "$byte_root" "$run_id" "$final_status" "$exit_code" \
    "$original_account_state" "$restored_account_equal" \
    "$original_competing_state" "$restored_competing_equal" \
    "$original_group_mapping_hash" "$restored_group_mapping_hash" \
    "$postgres_id" "$redis_id" "$keeper_id" \
    "$postgres_mount_hash" "$redis_mount_hash" "$keeper_mount_hash" \
    "$service_image_ref" "$service_image_id" <<'PY'
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
scenarios = []
for scenario_id, relay_scenario in (("KILO_COMPAT", "A03"), ("KILO_RESPONSES", "A05")):
    scenario_root = root / "scenarios" / scenario_id
    actions = Counter()
    production_forwarded = False
    interventions = scenario_root / "relay" / "intervention.jsonl"
    if interventions.is_file():
        for line in interventions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("action"):
                actions[event["action"]] += 1
            production_forwarded |= bool(event.get("production_forwarded"))
    pcap = scenario_root / "egress.pcap"
    scenarios.append({
        "scenario_id": scenario_id,
        "relay_scenario": relay_scenario,
        "actions": dict(sorted(actions.items())),
        "production_forwarded": production_forwarded,
        "pcap_bytes": pcap.stat().st_size if pcap.is_file() else 0,
        "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest()
        if pcap.is_file() else "",
    })

payload = {
    "schema_version": "kilo-r11-byte-capture/v1",
    "run_id": sys.argv[2],
    "status": sys.argv[3],
    "exit_code": int(sys.argv[4]),
    "client": "ZLF Code/Kilo 7.4.1701",
    "configured_model": "gpt-5.6-luna",
    "synthetic_profile": "candidate-core-v1",
    "production_forwarding_enabled": False,
    "scenarios": scenarios,
    "restoration": {
        "account_90_original": sys.argv[5],
        "account_90_equal": sys.argv[6] == "true",
        "account_95_original": sys.argv[7],
        "account_95_equal": sys.argv[8] == "true",
        "group_mapping_sha256_before": sys.argv[9],
        "group_mapping_sha256_after": sys.argv[10],
        "group_mapping_equal": bool(sys.argv[9]) and sys.argv[9] == sys.argv[10],
        "data_container_ids": {
            "postgres": sys.argv[11],
            "redis": sys.argv[12],
            "keeper": sys.argv[13],
        },
        "data_mount_sha256": {
            "postgres": sys.argv[14],
            "redis": sys.argv[15],
            "keeper": sys.argv[16],
        },
        "service_image_ref": sys.argv[17],
        "service_image_id": sys.argv[18],
    },
}
path = root / "run-summary.json"
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
PY
}

restore_failed=0
capture_status=failed
baseline_ready=0
proxy_id=""
proxy_created=0
proxy_bound=0
ca_installed=0
original_keeper_running=false
original_account_state=""
original_account_extra_hex=""
original_competing_state=""
original_group_mapping_hash=""
restored_group_mapping_hash=""
restored_account_equal=false
restored_competing_equal=false
auth_digest=""
service_image_ref=""
service_image_id=""
postgres_id=""
redis_id=""
keeper_id=""
postgres_mount_hash=""
redis_mount_hash=""
keeper_mount_hash=""
original_ca_hash=""

restore_environment() {
  local original_exit_code=$?
  local account_status account_sched account_proxy account_fallback
  local competing_status competing_sched
  local current_account_state current_account_extra current_competing_state
  local current_service_image current_postgres_id current_redis_id current_keeper_id
  trap - EXIT ERR INT TERM
  set +e

  stop_capture || restore_failed=1

  if [[ $baseline_ready == 1 ]]; then
    IFS='|' read -r account_status account_sched account_proxy account_fallback \
      <<<"$original_account_state"
    IFS='|' read -r competing_status competing_sched <<<"$original_competing_state"
    db_query "
update accounts
set status = '$account_status',
    schedulable = $account_sched,
    proxy_id = $account_proxy,
    proxy_fallback_origin_id = $account_fallback,
    extra = convert_from(decode('$original_account_extra_hex','hex'),'UTF8')::jsonb
where id = $account_id" >/dev/null || restore_failed=1
    db_query "
update accounts
set status = '$competing_status', schedulable = $competing_sched
where id = $competing_account_id" >/dev/null || restore_failed=1
    if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
      db_query "delete from proxies where id = $proxy_id and name = 'kilo-r11-${run_id:0:72}'" \
        >/dev/null || restore_failed=1
      proxy_created=0
    fi
    invalidate_auth_cache || restore_failed=1
  fi

  if [[ $baseline_ready == 1 ]]; then
    if [[ $ca_installed == 1 ]]; then
      docker exec "$service_container" rm -f "$custom_ca_path" >/dev/null 2>&1 || restore_failed=1
      docker exec "$service_container" update-ca-certificates --fresh >/dev/null 2>&1 || restore_failed=1
      ca_installed=0
    fi
    if [[ -s $runtime_dir/ca-certificates.before ]]; then
      current_ca_hash=$(docker exec "$service_container" sha256sum \
        /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
      if [[ $current_ca_hash != "$original_ca_hash" ]]; then
        docker cp "$runtime_dir/ca-certificates.before" \
          "$service_container:/tmp/kilo-r11-ca.restore" >/dev/null 2>&1 || restore_failed=1
        docker exec "$service_container" sh -c \
          'cat /tmp/kilo-r11-ca.restore > /etc/ssl/certs/ca-certificates.crt && rm -f /tmp/kilo-r11-ca.restore' \
          >/dev/null 2>&1 || restore_failed=1
      fi
    fi
    restart_service || restore_failed=1
  fi

  if [[ $original_keeper_running == true ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
  fi

  if [[ $baseline_ready == 1 ]]; then
    current_account_state=$(db_query "
select status || '|' || schedulable::text || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = $account_id" 2>/dev/null)
    current_account_extra=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex')
from accounts where id = $account_id" 2>/dev/null)
    current_competing_state=$(db_query "
select status || '|' || schedulable::text
from accounts where id = $competing_account_id" 2>/dev/null)
    restored_group_mapping_hash=$(hash_group_mapping 2>/dev/null)
    if [[ $current_account_state == "$original_account_state" &&
      $current_account_extra == "$original_account_extra_hex" ]]; then
      restored_account_equal=true
    else
      restore_failed=1
    fi
    if [[ $current_competing_state == "$original_competing_state" ]]; then
      restored_competing_equal=true
    else
      restore_failed=1
    fi
    [[ $restored_group_mapping_hash == "$original_group_mapping_hash" ]] || restore_failed=1
    [[ $(db_query "select count(*) from proxies where name = 'kilo-r11-${run_id:0:72}'" 2>/dev/null) == 0 ]] || restore_failed=1
  fi

  if [[ $baseline_ready == 1 ]]; then
    current_service_image=$(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null)
    current_postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null)
    current_redis_id=$(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null)
    current_keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null)
    [[ $current_service_image == "$service_image_id" ]] || restore_failed=1
    [[ $current_postgres_id == "$postgres_id" ]] || restore_failed=1
    [[ $current_redis_id == "$redis_id" ]] || restore_failed=1
    [[ $current_keeper_id == "$keeper_id" ]] || restore_failed=1
    [[ $(hash_container_mounts "$postgres_container" 2>/dev/null) == "$postgres_mount_hash" ]] || restore_failed=1
    [[ $(hash_container_mounts "$redis_container" 2>/dev/null) == "$redis_mount_hash" ]] || restore_failed=1
    [[ $(hash_container_mounts "$keeper_container" 2>/dev/null) == "$keeper_mount_hash" ]] || restore_failed=1
    [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container" 2>/dev/null) == "$original_keeper_running" ]] || restore_failed=1
    docker exec "$service_container" test ! -e "$custom_ca_path" >/dev/null 2>&1 || restore_failed=1
    current_ca_hash=$(docker exec "$service_container" sha256sum \
      /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
    [[ $current_ca_hash == "$original_ca_hash" ]] || restore_failed=1
    wait_healthy || restore_failed=1
  fi

  local final_status=$capture_status
  if [[ $original_exit_code != 0 ]]; then
    final_status=failed
  fi
  if [[ $restore_failed != 0 ]]; then
    final_status=restoration_failed
  fi
  write_summary "$final_status" "$original_exit_code" || true
  rm -f -- "$tls_dir/relay.key" "$tls_dir/relay.csr" "$tls_dir/relay.ext"

  if [[ $restore_failed != 0 ]]; then
    echo "环境恢复门禁失败；恢复备份保留在 $runtime_dir。" >&2
    exit 97
  fi
  echo "环境已精确恢复：账号、调度、代理、CA、认证缓存、数据容器和 keeper 均通过门禁。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

original_account_state=$(db_query "
select status || '|' || schedulable::text || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = $account_id")
original_account_extra_hex=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex')
from accounts where id = $account_id")
original_competing_state=$(db_query "
select status || '|' || schedulable::text
from accounts where id = $competing_account_id")
original_group_mapping_hash=$(hash_group_mapping)
account_shape=$(db_query "select platform || '|' || type from accounts where id = $account_id")
competing_shape=$(db_query "select platform || '|' || type from accounts where id = $competing_account_id")
api_key_state=$(db_query "select group_id || '|' || status from api_keys where id = $api_key_id and deleted_at is null")
token_present=$(db_query "select (length(coalesce(credentials->>'access_token','')) > 0)::text from accounts where id = $account_id")
if [[ $original_account_state != 'active|true|NULL|NULL' ||
  ! $original_account_extra_hex =~ ^[0-9a-f]+$ ||
  $original_competing_state != 'active|true' ||
  $account_shape != 'openai|oauth' ||
  $competing_shape != 'openai|apikey' ||
  $api_key_state != "$group_id|active" ||
  $token_present != true ]]; then
  echo "Kilo key1/group8/account90/account95 前置状态不符合冻结假设。" >&2
  exit 1
fi

auth_digest=$(db_query "
select encode(sha256(convert_to(key,'UTF8')),'hex')
from api_keys where id = $api_key_id and group_id = $group_id")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]]

service_image_ref=$(docker inspect -f '{{.Config.Image}}' "$service_container")
service_image_id=$(docker inspect -f '{{.Image}}' "$service_container")
postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
postgres_mount_hash=$(hash_container_mounts "$postgres_container")
redis_mount_hash=$(hash_container_mounts "$redis_container")
keeper_mount_hash=$(hash_container_mounts "$keeper_container")
original_keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
original_ca_hash=$(docker exec "$service_container" sha256sum \
  /etc/ssl/certs/ca-certificates.crt | awk '{print $1}')
if [[ $original_keeper_running != true ]]; then
  echo "keeper 必须在抓包前处于运行态。" >&2
  exit 1
fi
if docker exec "$service_container" test -e "$custom_ca_path"; then
  echo "临时 CA 路径已存在，拒绝覆盖。" >&2
  exit 1
fi
if ! test -s "$ca_full" || ! test -s "$ca_cert" ||
  ! test -s "$scrub_tool" ||
  ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null ||
  ! docker exec "$capture_container" sh -c 'command -v tcpdump >/dev/null' ||
  ! docker exec "$capture_container" python3 "$relay_tool" --help 2>&1 | grep -q candidate-core-v1; then
  echo "抓包网络、CA 或工具前置条件缺失。" >&2
  exit 1
fi
if docker exec "$capture_container" sh -c \
  "ps -ef | grep -E 'candidate-core-v1|tcpdump.*$relay_port' | grep -v grep | grep -q ."; then
  echo "capture-cli 中存在同类残留进程。" >&2
  exit 1
fi

docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" \
  "$runtime_dir/ca-certificates.before" >/dev/null
chmod 0600 "$runtime_dir/ca-certificates.before"
baseline_ready=1

docker stop "$keeper_container" >/dev/null

openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj '/CN=chatgpt.com' >/dev/null 2>&1
printf '%s\n' \
  'subjectAltName=DNS:chatgpt.com' \
  'extendedKeyUsage=serverAuth' >"$tls_dir/relay.ext"
serial=$(openssl rand -hex 16)
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -set_serial "0x$serial" -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 0600 "$tls_dir"/*

proxy_id=$(db_query "
insert into proxies (name,protocol,host,port,status,fallback_mode)
values ('kilo-r11-${run_id:0:72}','http','$capture_container',$relay_port,'active','none')
returning id")
[[ $proxy_id =~ ^[0-9]+$ ]]
proxy_created=1
db_query "update accounts set proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $account_id" >/dev/null
proxy_bound=1
db_query "update accounts set schedulable = false where id = $competing_account_id" >/dev/null
invalidate_auth_cache

docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null
ca_installed=1
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1

start_capture KILO_COMPAT A03
restart_service
printf 'READY_KILO_COMPAT run_id=%s\n' "$run_id"
wait_action KILO_COMPAT responses_http_success 1
printf 'CAPTURED_KILO_COMPAT run_id=%s\n' "$run_id"
stop_capture

start_capture KILO_RESPONSES A05
restart_service
printf 'READY_KILO_RESPONSES run_id=%s\n' "$run_id"
wait_action KILO_RESPONSES responses_ws_response_create 1
printf 'CAPTURED_KILO_RESPONSES run_id=%s\n' "$run_id"
stop_capture

capture_status=complete
