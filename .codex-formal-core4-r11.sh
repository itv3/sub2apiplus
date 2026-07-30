#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# R11 正式 core 四轮抓包外层保险包装。
#
# 本脚本应在 Vircs 的 root shell 中执行。core 场景不调用 Live，因此全程坚持
# R11 normal 镜像；candidatecapture 镜像只用于另行执行的 Live 辅助场景。
# 内层脚本负责单轮账号 proxy/extra、CA、hosts 与 keeper 恢复；本包装再做一次
# 独立快照和强制恢复，任何不一致均以 97 退出并保留现场。

compose_file=/root/Docker/sub2apiplus/app/docker-compose.yml
normal_override=/root/Docker/sub2apiplus/deployments/codex0145-20260730T195700Z-r11/image.override.yml
source_root=/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source
tool_root=/root/oauth-capture/tools/official_client_capture
capture_root=/root/oauth-capture
core_script="$tool_root/run_candidate_core_capture.sh"

normal_image_ref=sub2apiplus:codex0145-20260730T195700Z-39e579acb066-r11
normal_image_id=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
forbidden_capture_image_id=sha256:54aee6e64177d2db210fd183f829aa90cfdb4ec7ed9cf3fdbfecb50c82473b64

service_container=sub2apiplus
capture_container=capture-cli
postgres_container=sub2apiplus-postgres
redis_container=sub2apiplus-redis
keeper_container=sub2apiplus-keeper
group_id=9
account_id=99
api_key_id=15
service_port=3001
relay_port=18443

batch_id=${BATCH_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
if [[ ! $batch_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "BATCH_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

run_ids=(
  "formal-r11-core01-$batch_id"
  "formal-r11-core02-$batch_id"
  "formal-r11-core03-$batch_id"
  "formal-r11-core04-$batch_id"
)
runtime_dir="$capture_root/runtime/formal-r11-core4-$batch_id"
state_file="$runtime_dir/outer-baseline.json"
if [[ -e $runtime_dir ]]; then
  echo "外层运行目录已存在，拒绝覆盖：$runtime_dir" >&2
  exit 2
fi
for run_id in "${run_ids[@]}"; do
  if [[ -e $capture_root/runs/$run_id ]]; then
    echo "正式抓包目录已存在，拒绝覆盖：$capture_root/runs/$run_id" >&2
    exit 2
  fi
done
install -d -m 0700 "$runtime_dir"

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
  return 1
}

attestation_env_count() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    sed -n '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/p' |
    wc -l |
    tr -d ' '
}

normal_compose_shape() {
  docker compose -f "$compose_file" -f "$normal_override" config --format json |
    python3 -c '
import json
import sys

service = json.load(sys.stdin)["services"]["sub2api"]
environment = service.get("environment") or {}
if isinstance(environment, list):
    names = {item.split("=", 1)[0] for item in environment}
else:
    names = set(environment)
count = sum(name.startswith("SUB2API_LIVE_ATTESTATION_CAPTURE_") for name in names)
print("{}|{}".format(service.get("image", ""), count))
'
}

invalidate_auth_cache() {
  printf 'apikey:auth:%s' "$auth_digest" |
    docker exec -i "$redis_container" redis-cli -x DEL >/dev/null
  printf '%s' "$auth_digest" |
    docker exec -i "$redis_container" redis-cli -x PUBLISH auth:cache:invalidate >/dev/null
}

stop_owned_processes() {
  local pid_file pid signal
  for run_id in "${run_ids[@]}"; do
    while IFS= read -r pid_file; do
      [[ -f $pid_file ]] || continue
      pid=$(tr -d '[:space:]' <"$pid_file")
      [[ $pid =~ ^[0-9]+$ ]] || continue
      signal=TERM
      [[ $pid_file == */pcap.pid ]] && signal=INT
      docker exec "$capture_container" kill "-$signal" "$pid" >/dev/null 2>&1 || true
    done < <(
      find "$capture_root/runs/$run_id" -type f \
        \( -path '*/relay-private/relay.pid' -o -name pcap.pid \) \
        -print 2>/dev/null || true
    )
  done
}

baseline_ready=0
restore_failed=0
capture_complete=0
original_group_state=""
original_proxy_state=""
original_extra_hex=""
original_keeper_running=false
auth_digest=""
postgres_id=""
redis_id=""
keeper_id=""

restore_outer_environment() {
  local original_exit_code=$?
  local proxy_value fallback_value current_group current_proxy current_extra
  local temp_proxy_count current_cache_exists
  trap - EXIT INT TERM
  set +e

  stop_owned_processes

  if [[ $baseline_ready == 1 ]]; then
    read -r proxy_value fallback_value <<<"${original_proxy_state/|/ }"
    db_query "
update accounts
set proxy_id = $proxy_value,
    proxy_fallback_origin_id = $fallback_value,
    extra = convert_from(decode('$original_extra_hex','hex'),'UTF8')::jsonb
where id = $account_id" >/dev/null || restore_failed=1

    for run_id in "${run_ids[@]}"; do
      db_query "delete from proxies where name = 'candidate-core-${run_id:0:72}'" \
        >/dev/null || restore_failed=1
    done

    db_query "update groups set platform = 'composite' where id = $group_id" \
      >/dev/null || restore_failed=1
    invalidate_auth_cache || restore_failed=1
  fi

  if [[ $original_keeper_running == true ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
  fi

  if [[ $(docker image inspect -f '{{.Id}}' "$normal_image_ref" 2>/dev/null) != "$normal_image_id" ||
    $(normal_compose_shape 2>/dev/null) != "$normal_image_ref|0" ]]; then
    echo "R11 normal 镜像或 Compose 画像未绑定冻结目标，拒绝恢复。" >&2
    restore_failed=1
  else
    docker compose -f "$compose_file" -f "$normal_override" \
      up -d --no-deps --force-recreate sub2api \
      >"$runtime_dir/normal-restore.log" 2>&1 || restore_failed=1
    wait_healthy || restore_failed=1
  fi

  if [[ $baseline_ready == 1 ]]; then
    current_group=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = $group_id" 2>/dev/null)
    current_proxy=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = $account_id" 2>/dev/null)
    current_extra=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex')
from accounts where id = $account_id" 2>/dev/null)
    temp_proxy_count=$(db_query "
select count(*) from proxies
where name in (
  'candidate-core-${run_ids[0]:0:72}',
  'candidate-core-${run_ids[1]:0:72}',
  'candidate-core-${run_ids[2]:0:72}',
  'candidate-core-${run_ids[3]:0:72}'
)" 2>/dev/null)
    current_cache_exists=$(
      printf 'apikey:auth:%s' "$auth_digest" |
        docker exec -i "$redis_container" redis-cli -x EXISTS 2>/dev/null |
        tr -d '\r'
    )

    [[ $current_group == "$original_group_state" ]] || restore_failed=1
    [[ $current_proxy == "$original_proxy_state" ]] || restore_failed=1
    [[ $current_extra == "$original_extra_hex" ]] || restore_failed=1
    [[ $temp_proxy_count == 0 ]] || restore_failed=1
    [[ $current_cache_exists == 0 ]] || restore_failed=1
  fi

  [[ $(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null) == "$normal_image_id" ]] ||
    restore_failed=1
  [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container" 2>/dev/null) == "$original_keeper_running" ]] ||
    restore_failed=1
  [[ $(attestation_env_count 2>/dev/null) == 0 ]] || restore_failed=1
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-core-capture.crt \
    >/dev/null 2>&1 || restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null) == "$postgres_id" ]] ||
    restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null) == "$redis_id" ]] ||
    restore_failed=1
  [[ $(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null) == "$keeper_id" ]] ||
    restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    echo "正式 core 外层恢复失败；受保护基线保留在 $state_file。" >&2
    exit 97
  fi

  rm -f -- "$state_file"
  if [[ $capture_complete == 1 && $original_exit_code == 0 ]]; then
    echo "四次正式 core 抓包完成，R11 normal、分组、账号、缓存与数据容器均已恢复。"
  else
    echo "正式 core 抓包未完成，但外层环境已精确恢复。" >&2
  fi
  exit "$original_exit_code"
}

normal_tag_id=$(docker image inspect -f '{{.Id}}' "$normal_image_ref" 2>/dev/null || true)
current_service_id=$(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null || true)
compose_shape=$(normal_compose_shape 2>/dev/null || true)
if [[ $normal_tag_id != "$normal_image_id" || $current_service_id != "$normal_image_id" ||
  $compose_shape != "$normal_image_ref|0" ||
  $current_service_id == "$forbidden_capture_image_id" ]]; then
  echo "core 正式抓包必须从冻结 R11 normal 镜像启动。" >&2
  exit 1
fi
if ! wait_healthy; then
  echo "R11 normal 服务当前不健康。" >&2
  exit 1
fi
if [[ $(attestation_env_count) != 0 ]]; then
  echo "core 不允许携带任何 Live candidatecapture attestation 环境变量。" >&2
  exit 1
fi

expected_tool_hashes=(
  '3c7439376a3168052e2dbbc750704675f43043e5e827ede95a70855d5f7410cf run_candidate_core_capture.sh'
  'a5f911f1f28d679cc2b6eef32a9fa750c4aa893da4292cb5521ce6947e8ad511 upstream_byte_relay.py'
  '7f3dbf4ea7a0fb06a56d404bc754512c38ca268a46abd9c943f9ecf5f9ac78df drive_candidate_gateway_ws.py'
  '92154026b091d6ef84af4708c277dc3a6669fc9171fd144198e537aac9515f62 scrub_raw_bytes.py'
)
for expected in "${expected_tool_hashes[@]}"; do
  expected_hash=${expected%% *}
  file_name=${expected#* }
  [[ $(sha256sum "$tool_root/$file_name" | awk '{print $1}') == "$expected_hash" ]] || {
    echo "正式工具哈希不匹配：$file_name" >&2
    exit 1
  }
  [[ $(sha256sum "$source_root/tools/official_client_capture/$file_name" | awk '{print $1}') == "$expected_hash" ]] || {
    echo "工具未与冻结 R11 源码快照绑定：$file_name" >&2
    exit 1
  }
done

original_group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = $group_id")
original_proxy_state=$(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = $account_id")
original_extra_hex=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex')
from accounts where id = $account_id")
eligible_accounts=$(db_query "
select string_agg(a.id::text,',' order by a.id)
from account_groups ag
join accounts a on a.id = ag.account_id
where ag.group_id = $group_id
  and a.platform = 'openai'
  and a.type = 'oauth'
  and a.status = 'active'
  and a.schedulable = true")
token_present=$(db_query "
select (length(coalesce(credentials->>'access_token','')) > 0)::text
from accounts where id = $account_id")

if [[ $original_group_state != 'composite|false|true|false' ||
  $original_proxy_state != 'NULL|NULL' ||
  ! $original_extra_hex =~ ^[0-9a-f]+$ ||
  $eligible_accounts != "$account_id" ||
  $token_present != true ]]; then
  echo "group9/account99/API key15 的正式隔离前置条件不成立。" >&2
  exit 1
fi

# 只从数据库取得摘要；API Key 明文不进入外层 shell、进程参数或抓包证据。
auth_digest=$(db_query "
select encode(sha256(convert_to(key, 'UTF8')), 'hex')
from api_keys
where id = $api_key_id and group_id = $group_id
  and status = 'active' and deleted_at is null")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]] || exit 1

postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
original_keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
if [[ $original_keeper_running != true ]]; then
  echo "keeper 必须在正式抓包前处于运行态。" >&2
  exit 1
fi

if docker exec "$service_container" test -e \
  /usr/local/share/ca-certificates/candidate-core-capture.crt; then
  echo "检测到残留 core CA，拒绝开始。" >&2
  exit 1
fi
if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null ||
  ! docker exec "$capture_container" sh -c 'command -v tcpdump >/dev/null' ||
  ! docker exec "$capture_container" python3 -c 'import zstandard' >/dev/null 2>&1 ||
  ! test -s "$capture_root/state/mitm/mitmproxy-ca.pem" ||
  ! test -s "$capture_root/state/mitm/mitmproxy-ca-cert.pem"; then
  echo "capture 网络、tcpdump、zstandard 或验收 CA 前置条件缺失。" >&2
  exit 1
fi
if docker exec "$capture_container" sh -c \
  "ps -ef | grep -E 'candidate-core-v1|tcpdump.*$relay_port' | grep -v grep | grep -q ."; then
  echo "capture-cli 中仍有 core 抓包进程。" >&2
  exit 1
fi

python3 - "$state_file" "$batch_id" "$original_group_state" \
  "$original_proxy_state" "$original_extra_hex" "$postgres_id" "$redis_id" \
  "$keeper_id" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "batch_id": sys.argv[2],
    "group_state": sys.argv[3],
    "account_proxy_state": sys.argv[4],
    "account_extra_hex": sys.argv[5],
    "data_container_ids": {
        "postgres": sys.argv[6],
        "redis": sys.argv[7],
        "keeper": sys.argv[8],
    },
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

baseline_ready=1
trap restore_outer_environment EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

updated_platform=$(db_query "
update groups set platform = 'openai'
where id = $group_id and platform = 'composite'
returning platform")
if [[ $updated_platform != openai ]]; then
  echo "无法把 group9 临时切换为 openai。" >&2
  exit 1
fi
invalidate_auth_cache
docker restart "$service_container" >/dev/null
wait_healthy
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$normal_image_id" ]]

validate_run() {
  local run_id=$1
  python3 - "$capture_root/runs/$run_id/run-summary.json" "$run_id" \
    "$original_proxy_state" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
expected = {"A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"}
if payload.get("run_id") != sys.argv[2]:
    raise SystemExit("run_id 不一致")
if payload.get("status") != "complete" or payload.get("exit_code") != 0:
    raise SystemExit("单轮 core 未完成")
if payload.get("synthetic_profile") != "candidate-core-v1":
    raise SystemExit("合成画像不一致")
if payload.get("production_forwarding_enabled") is not False:
    raise SystemExit("单轮未证明 production forwarding 已关闭")
restoration = payload.get("restoration", {})
if restoration.get("account_proxy_original") != sys.argv[3] or not all(
    restoration.get(key) is True
    for key in (
        "account_proxy_equal",
        "account_extra_equal",
        "hosts_sha256_equal",
        "ca_bundle_sha256_equal",
    )
):
    raise SystemExit("单轮内层恢复证明不完整")
scenarios = payload.get("scenarios", [])
if {item.get("scenario_id") for item in scenarios} != expected:
    raise SystemExit("单轮场景集合不完整")
for item in scenarios:
    if item.get("production_forwarded") is not False or item.get("pcap_bytes", 0) <= 24:
        raise SystemExit(f"{item.get('scenario_id')} 的 pcap/转发门禁不合格")
PY
}

for run_id in "${run_ids[@]}"; do
  printf '开始正式 core：%s\n' "$run_id"
  ENABLE_CANDIDATE_CORE_SYNTHETIC=YES_I_ACCEPT_SYNTHETIC_ONLY \
  RUN_ID="$run_id" \
  ACCOUNT_ID="$account_id" \
  API_KEY_ID="$api_key_id" \
  CAPTURE_CONTAINER="$capture_container" \
  SERVICE_CONTAINER="$service_container" \
  KEEPER_CONTAINER="$keeper_container" \
  POSTGRES_CONTAINER="$postgres_container" \
  CAPTURE_ROOT="$capture_root" \
  CAPTURE_MOUNT=/capture \
  SERVICE_PORT="$service_port" \
  RELAY_PORT="$relay_port" \
  A07_WS_FAILURE_COUNT=6 \
    "$core_script"
  validate_run "$run_id"

  [[ $(docker inspect -f '{{.Image}}' "$service_container") == "$normal_image_id" ]]
  [[ $(attestation_env_count) == 0 ]]
  [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container") == true ]]
  [[ $(db_query "select platform from groups where id = $group_id") == openai ]]
  [[ $(db_query "
select coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = $account_id") == "$original_proxy_state" ]]
  [[ $(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex')
from accounts where id = $account_id") == "$original_extra_hex" ]]
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-core-capture.crt
done

python3 - "$source_root" "$capture_root" "$runtime_dir/a02-selection.json" \
  "${run_ids[@]}" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

source_root = Path(sys.argv[1])
capture_root = Path(sys.argv[2])
selection_path = Path(sys.argv[3])
run_ids = sys.argv[4:]
sys.path.insert(0, str(source_root))

from tools.official_client_capture.pcap_clienthello import (  # noqa: E402
    iter_packets,
    parse_client_hello,
    tcp_payload,
)

orders = []
pcap_hashes = set()
selections = []
for run_id in run_ids:
    path = capture_root / "runs" / run_id / "scenarios" / "A06" / "egress.pcap"
    hellos = []
    for linktype, packet in iter_packets(path):
        parsed = tcp_payload(linktype, packet)
        if parsed is None:
            continue
        hello = parse_client_hello(parsed[2])
        if hello is not None:
            hellos.append(tuple(hello[1]))
    if len(hellos) != 1:
        raise SystemExit(f"{run_id} 的 A06 ClientHello 数量 {len(hellos)} != 1")
    orders.append(hellos[0])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pcap_hashes.add(digest)
    selections.append({
        "run_id": run_id,
        "source_path": str(path),
        "sha256": digest,
        "scenario_ids": ["A02"],
        # core 通过系统证书池信任临时根证书，没有配置画像意义上的自定义 CA。
        "labels": {"transport": "websocket", "ca_mode": "system"},
    })

if len(orders) != 4 or len(pcap_hashes) != 4:
    raise SystemExit("A02 未形成四份独立 pcap")
if any(set(order) != set(orders[0]) for order in orders[1:]):
    raise SystemExit("A02 四次 ClientHello 的扩展集合不一致")
if len(set(orders)) < 2:
    raise SystemExit("A02 四次 ClientHello 未形成至少两种扩展顺序")

selection_path.write_text(
    json.dumps({"a02_pcaps": selections}, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.chmod(selection_path, 0o600)
print("A02 四份 pcap 校验通过：同扩展集合，且至少两种排列。")
PY

printf '%s\n' "${run_ids[@]}" >"$runtime_dir/core4-runs.txt"
chmod 0600 "$runtime_dir/core4-runs.txt"
capture_complete=1
