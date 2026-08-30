#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# 候选侧 A03/A04/A05/A06/A07/A08/A10/A15 核心场景受控抓包。
#
# relay 只有在脚本环境开关与自身命令行开关同时满足时才会启动；合成画像没有
# upstream/DNS 出口，未知 host/path/state 一律 421。入口始终调用生产 sub2api。

required_gate=YES_I_ACCEPT_SYNTHETIC_ONLY
if [[ ${ENABLE_CANDIDATE_CORE_SYNTHETIC:-} != "$required_gate" ]]; then
  echo "拒绝启动：必须显式设置 ENABLE_CANDIDATE_CORE_SYNTHETIC=$required_gate。" >&2
  exit 2
fi

codex_version=${CODEX_VERSION:?必须由 Campaign 提供 CODEX_VERSION}
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是完整的 x.y.z 版本。" >&2
  exit 2
fi

capture_container=${CAPTURE_CONTAINER:-capture-cli}
service_container=${SERVICE_CONTAINER:-sub2apiplus}
keeper_container=${KEEPER_CONTAINER:-sub2apiplus-keeper}
postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_mount=${CAPTURE_MOUNT:-/capture}
account_id=${ACCOUNT_ID:?必须提供专用 OpenAI OAuth ACCOUNT_ID}
api_key_id=${API_KEY_ID:-1}
run_id=${RUN_ID:?必须提供 RUN_ID}
relay_port=${RELAY_PORT:-18443}
ws_failure_count=${A07_WS_FAILURE_COUNT:-6}
# 主线与 Lite 轨模型必须由 Campaign 场景清单显式注入，禁止脚本按当前版本猜测。
main_model=${MAIN_MODEL:?必须由 Campaign 提供 MAIN_MODEL}
lite_model=${LITE_MODEL:?必须由 Campaign 提供 LITE_MODEL}

for numeric in "$account_id" "$api_key_id" "$relay_port" "$ws_failure_count"; do
  if [[ ! $numeric =~ ^[0-9]+$ ]]; then
    echo "ACCOUNT_ID、API_KEY_ID、RELAY_PORT 与 A07_WS_FAILURE_COUNT 必须是正整数。" >&2
    exit 2
  fi
done
if (( ws_failure_count < 1 || ws_failure_count > 20 )); then
  echo "A07_WS_FAILURE_COUNT 必须在 1..20。" >&2
  exit 2
fi
if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

work_dir="$capture_root/runs/$run_id"
container_work_dir="$capture_mount/runs/$run_id"
tls_dir="$work_dir/tls-private"
container_tls_dir="$container_work_dir/tls-private"
runtime_dir="$capture_root/runtime/candidate-core-$run_id"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/candidate-core-capture.crt
relay_tool="$capture_mount/tools/official_client_capture/upstream_byte_relay.py"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
scrub_tool="$script_dir/scrub_raw_bytes.py"
gateway_ws_driver="$script_dir/drive_candidate_gateway_ws.py"

if [[ -e $work_dir ]]; then
  echo "抓包目录已存在，拒绝覆盖：$work_dir" >&2
  exit 2
fi
test -s "$ca_full"
test -s "$ca_cert"
install -d -m 0700 "$work_dir" "$tls_dir" "$runtime_dir"

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
  echo "候选服务未在 90 秒内恢复健康。" >&2
  return 1
}

restart_service() {
  docker restart "$service_container" >/dev/null
  wait_healthy
}

auth_config() {
  # 凭据从匿名 fd 读取，不进入命令行或抓包目录。
  printf 'header = "Authorization: Bearer %s"\n' "$1"
}

request_with_token() {
  local token=$1
  shift
  # relay 一上线，Sub2API 的后台流量就会打到只接受特定形态的 relay 上并被拒，账号随即
  # 进入临时熔断；清一次不够，必须紧贴每次触发请求，否则请求本身会拿到 503。
  clear_account_gate
  curl --silent --show-error --max-time 180 --config <(auth_config "$token") "$@"
}

assert_2xx() {
  local label=$1
  local code=$2
  if [[ ! $code =~ ^2[0-9][0-9]$ ]]; then
    echo "$label 入口调用失败，HTTP $code。" >&2
    return 1
  fi
}

current_scenario=""
relay_started=0
pcap_started=0
proxy_id=""
proxy_created=0
proxy_bound=0
ca_installed=0
custom_ca_baseline_absent=0
keeper_was_running=false
restore_failed=0
capture_status=failed
original_proxy_state=""
original_gate_state=""
restored_gate_equal=false
original_extra_hex=""
original_hosts_hash=""
original_ca_hash=""
restored_hosts_hash=""
restored_ca_hash=""
restored_proxy_equal=false
restored_extra_equal=false
api_key=""

# 合成 relay 把 chatgpt.com 劫持到容器内端口；relay 停止后仍在途的真实出站会拿到
# connection refused，Sub2API 据此把账号临时熔断。熔断是本脚本自身的副作用，不恢复
# 就会让同一 attempt 的后续任务全部 503，因此按运行前值精确回写并复核。
# 合成 relay 上线后，Sub2API 的任何出站（含后台任务）都会打到只接受特定形态的 relay 上；
# 被拒的连接会让账号进入临时熔断，紧接着要采集的场景就拿到 503。每个场景开始时主动清一次，
# 退出时仍按 original_gate_state 的原值恢复，真实故障照旧以场景失败的形式暴露。
clear_account_gate() {
  db_query "update accounts set temp_unschedulable_until = null, temp_unschedulable_reason = null where id = $account_id" >/dev/null
}

account_gate_state() {
  db_query "select coalesce(encode(convert_to(coalesce(temp_unschedulable_until::text,''),'UTF8'),'hex'),'') || '|' || coalesce(encode(convert_to(coalesce(temp_unschedulable_reason,''),'UTF8'),'hex'),'') from accounts where id = $account_id"
}

restore_account_gate() {
  local until_hex reason_hex
  [[ -n $original_gate_state ]] || return 0
  until_hex=${original_gate_state%%|*}
  reason_hex=${original_gate_state##*|}
  [[ $until_hex =~ ^[0-9a-f]*$ && $reason_hex =~ ^[0-9a-f]*$ ]] || return 1
  db_query "update accounts set temp_unschedulable_until = nullif(convert_from(decode('$until_hex','hex'),'UTF8'),'')::timestamptz, temp_unschedulable_reason = nullif(convert_from(decode('$reason_hex','hex'),'UTF8'),'') where id = $account_id" >/dev/null
}

stop_container_process() {
  local pid=$1
  local first_signal=$2
  local label=$3
  local attempts=${4:-50}
  if [[ ! $pid =~ ^[0-9]+$ ]]; then
    echo "$label 的 PID 无效，无法证明后台进程已经停止。" >&2
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
  echo "$label 在强制终止后仍存活，环境恢复不能视为成功。" >&2
  restore_failed=1
  return 1
}

stop_capture() {
  local scenario_id scenario_root container_scenario_root pid
  if [[ -z $current_scenario ]]; then
    return 0
  fi
  scenario_id=$current_scenario
  scenario_root="$work_dir/scenarios/$scenario_id"
  container_scenario_root="$container_work_dir/scenarios/$scenario_id"

  if [[ $relay_started == 1 ]]; then
    pid=$(docker exec "$capture_container" sh -c 'cat "$1" 2>/dev/null || true' sh \
      "$container_scenario_root/relay.pid")
    if ! stop_container_process "$pid" TERM "$scenario_id relay" 80; then
      return 1
    fi
    relay_started=0
  fi
  # 合成响应很快，内核过滤器可能已收到包但 tcpdump 用户态尚未来得及落盘。
  # 先给它一个固定排空窗口，再发 SIGINT，避免得到只有全局头的 24 字节 pcap。
  if [[ $pcap_started == 1 ]]; then
    sleep 1
  fi
  if [[ $pcap_started == 1 ]]; then
    pid=$(docker exec "$capture_container" sh -c 'cat "$1" 2>/dev/null || true' sh \
      "$container_scenario_root/pcap.pid")
    if ! stop_container_process "$pid" INT "$scenario_id tcpdump" 50; then
      return 1
    fi
    pcap_started=0
  fi
  current_scenario=""

  for _ in $(seq 1 30); do
    [[ -s $scenario_root/relay-private/relay.json ]] && break
    sleep 0.1
  done
  if [[ ! -s $scenario_root/relay-private/relay.json ]]; then
    echo "$scenario_id 缺少 relay.json。" >&2
    return 1
  fi
  for path in "$scenario_root/egress.pcap" "$scenario_root/tcpdump.log" \
    "$scenario_root/pcap.pid"; do
    if [[ -e $path ]]; then
      chown "$(id -u):$(id -g)" "$path"
      chmod 0600 "$path"
    fi
  done
  if [[ ! -s $scenario_root/egress.pcap ]] ||
    (( $(stat -c '%s' "$scenario_root/egress.pcap" 2>/dev/null || printf '0') <= 24 )); then
    echo "$scenario_id 缺少包含数据包的有效 pcap。" >&2
    return 1
  fi
  if ! docker exec "$capture_container" tcpdump -nn -r \
    "$container_scenario_root/egress.pcap" -c 1 >/dev/null 2>&1; then
    echo "$scenario_id 的 pcap 无法解析出首个数据包。" >&2
    return 1
  fi

  python3 "$scrub_tool" \
    --src "$scenario_root/relay-private" \
    --dst "$scenario_root/relay" \
    --verify >/dev/null
  for name in intervention.jsonl sni.log; do
    if [[ -f $scenario_root/relay-private/$name ]]; then
      install -m 0600 "$scenario_root/relay-private/$name" "$scenario_root/relay/$name"
    fi
  done
  case "$scenario_root/relay-private" in
    "$work_dir"/scenarios/*/relay-private)
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
  python3 - \
    "$work_dir" "$run_id" "$final_status" "$exit_code" \
    "$original_proxy_state" "$restored_proxy_equal" "$restored_extra_equal" \
    "$restored_hosts_hash" "$original_hosts_hash" \
    "$restored_ca_hash" "$original_ca_hash" "$codex_version" <<'PY'
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
scenarios = []
for scenario_id in ("A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"):
    scenario_root = root / "scenarios" / scenario_id
    interventions = scenario_root / "relay" / "intervention.jsonl"
    actions = Counter()
    production_forwarded = False
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
        "actions": dict(sorted(actions.items())),
        "production_forwarded": production_forwarded,
        "pcap_bytes": pcap.stat().st_size if pcap.is_file() else 0,
        "pcap_sha256": hashlib.sha256(pcap.read_bytes()).hexdigest()
        if pcap.is_file() else "",
    })

payload = {
    "schema_version": "candidate-core-capture/v1",
    "codex_version": sys.argv[12],
    "run_id": sys.argv[2],
    "status": sys.argv[3],
    "exit_code": int(sys.argv[4]),
    "synthetic_profile": "candidate-core-v1",
    "explicit_gate": True,
    "production_forwarding_enabled": False,
    "scenarios": scenarios,
    "limitations": {
        "A08": "relay 只声明真实跨调用连接；keepalive/断连重试关系由受源码哈希约束的结构化测试补证",
        "A10_token_budget": "TokenBudget 零出站只由结构化测试证明，本脚本不伪造不存在的网络请求",
        "A15_surface": "relay 证明身份 header 的真实出站；exec/TUI 进程来源由结构化测试证明",
    },
    "restoration": {
        "account_proxy_original": sys.argv[5],
        "account_proxy_equal": sys.argv[6] == "true",
        "account_extra_equal": sys.argv[7] == "true",
        "hosts_sha256_equal": bool(sys.argv[9]) and sys.argv[8] == sys.argv[9],
        "ca_bundle_sha256_equal": bool(sys.argv[11]) and sys.argv[10] == sys.argv[11],
    },
}
path = root / "run-summary.json"
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
PY
}

restore_environment() {
  local original_exit_code=$?
  local proxy_value fallback_value current_proxy_state current_extra_hex
  local service_restart_needed=0
  trap - EXIT ERR INT TERM
  set +e

  stop_capture || capture_status=failed

  if [[ -n $original_extra_hex && $original_extra_hex =~ ^[0-9a-f]+$ ]]; then
    db_query "update accounts set extra = convert_from(decode('$original_extra_hex','hex'),'UTF8')::jsonb where id = $account_id" \
      >/dev/null || restore_failed=1
  fi
  if [[ $proxy_bound == 1 && $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ ]]; then
    read -r proxy_value fallback_value <<<"${original_proxy_state/|/ }"
    db_query "update accounts set proxy_id = $proxy_value, proxy_fallback_origin_id = $fallback_value where id = $account_id" \
      >/dev/null || restore_failed=1
    proxy_bound=0
  fi
  if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
    db_query "delete from proxies where id = $proxy_id and name = 'candidate-core-${run_id:0:72}'" \
      >/dev/null || restore_failed=1
    proxy_created=0
  fi

  if [[ $ca_installed == 1 ]]; then
    service_restart_needed=1
    docker exec "$service_container" rm -f "$custom_ca_path" >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" update-ca-certificates --fresh >/dev/null 2>&1 || restore_failed=1
    ca_installed=0
  fi
  if [[ $service_restart_needed == 1 ]]; then
    restart_service || restore_failed=1
  fi

  if [[ -s $runtime_dir/hosts.before ]]; then
    docker cp "$runtime_dir/hosts.before" "$service_container:/tmp/candidate-core-hosts.restore" \
      >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" sh -c \
      'cat /tmp/candidate-core-hosts.restore > /etc/hosts && rm -f /tmp/candidate-core-hosts.restore' \
      >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ -s $runtime_dir/ca-certificates.before ]]; then
    restored_ca_hash=$(docker exec "$service_container" sha256sum \
      /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
    if [[ $restored_ca_hash != "$original_ca_hash" ]]; then
      docker cp "$runtime_dir/ca-certificates.before" \
        "$service_container:/tmp/candidate-core-ca.restore" >/dev/null 2>&1 || restore_failed=1
      docker exec "$service_container" sh -c \
        'cat /tmp/candidate-core-ca.restore > /etc/ssl/certs/ca-certificates.crt && rm -f /tmp/candidate-core-ca.restore' \
        >/dev/null 2>&1 || restore_failed=1
    fi
  fi
  if [[ $keeper_was_running == true ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
  fi

  if restore_account_gate && [[ $(account_gate_state) == "$original_gate_state" ]]; then
    restored_gate_equal=true
  else
    restore_failed=1
  fi

  current_proxy_state=$(db_query \
    "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id" \
    2>/dev/null)
  current_extra_hex=$(db_query \
    "select encode(convert_to(extra::text,'UTF8'),'hex') from accounts where id = $account_id" \
    2>/dev/null)
  if [[ $current_proxy_state == "$original_proxy_state" ]]; then
    restored_proxy_equal=true
  else
    restore_failed=1
  fi
  if [[ $current_extra_hex == "$original_extra_hex" ]]; then
    restored_extra_equal=true
  else
    restore_failed=1
  fi
  restored_hosts_hash=$(docker exec "$service_container" sha256sum /etc/hosts 2>/dev/null | awk '{print $1}')
  restored_ca_hash=$(docker exec "$service_container" sha256sum \
    /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
  [[ $restored_hosts_hash == "$original_hosts_hash" ]] || restore_failed=1
  [[ $restored_ca_hash == "$original_ca_hash" ]] || restore_failed=1
  if [[ $custom_ca_baseline_absent == 1 ]]; then
    docker exec "$service_container" test ! -e "$custom_ca_path" >/dev/null 2>&1 ||
      restore_failed=1
  fi

  if [[ -n $api_key ]]; then
    python3 - "$work_dir" 3< <(printf '%s' "$api_key") <<'PY' || restore_failed=1
import os
import sys
from pathlib import Path

needle = os.fdopen(3, "rb").read()
if not needle:
    raise SystemExit(1)
for path in Path(sys.argv[1]).rglob("*"):
    if path.is_file() and needle in path.read_bytes():
        raise SystemExit(1)
PY
  fi
  api_key=""

  if [[ $restore_failed != 0 ]]; then
    write_summary restoration_failed 97 || true
    echo "候选核心抓包环境恢复失败；备份保留在 $runtime_dir。" >&2
    exit 97
  fi

  local final_status=$capture_status
  if [[ $original_exit_code != 0 ]]; then
    final_status=failed
  fi
  write_summary "$final_status" "$original_exit_code" || true
  rm -f -- "$tls_dir/relay.key" "$tls_dir/relay.csr" "$tls_dir/relay.ext"
  echo "环境已精确恢复：账号 proxy/fallback/extra、hosts、CA bundle 与 keeper 状态均已核验。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

original_proxy_state=$(db_query \
  "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id")
original_extra_hex=$(db_query \
  "select encode(convert_to(extra::text,'UTF8'),'hex') from accounts where id = $account_id")
account_shape=$(db_query \
  "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') from accounts where id = $account_id")
if [[ ! $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ || ! $original_extra_hex =~ ^[0-9a-f]+$ ]]; then
  echo "无法读取账号 proxy/fallback/extra 初始状态。" >&2
  exit 1
fi
original_gate_state=$(account_gate_state)
if [[ ! $original_gate_state =~ ^[0-9a-f]*\|[0-9a-f]*$ ]]; then
  echo "无法读取账号 #$account_id 的调度门初始状态。" >&2
  exit 1
fi
if [[ ! $account_shape =~ ^openai\|oauth\|NULL$ ]]; then
  echo "ACCOUNT_ID 必须是非影子的 OpenAI OAuth 专用账号。" >&2
  exit 1
fi

api_key=$(db_query "select key from api_keys where id = $api_key_id")
group_id=$(db_query "select group_id from api_keys where id = $api_key_id")
token_present=$(db_query \
  "select case when length(coalesce(credentials->>'access_token','')) > 0 then 'true' else 'false' end from accounts where id = $account_id")
if [[ -z $api_key || ! $group_id =~ ^[0-9]+$ || $token_present != true ]]; then
  echo "API Key/分组不存在，或专用账号缺少当前 access token。" >&2
  exit 1
fi
eligible_accounts=$(db_query "
select a.id
from account_groups ag
join accounts a on a.id = ag.account_id
where ag.group_id = $group_id
  and a.platform = 'openai'
  and a.type = 'oauth'
  and a.status = 'active'
  and a.schedulable = true
order by a.id")
if [[ $eligible_accounts != "$account_id" ]]; then
  echo "API Key 分组不是 ACCOUNT_ID 的单账号隔离分组，拒绝影响生产调度。" >&2
  exit 1
fi

service_port=${SERVICE_PORT:-}
if [[ -z $service_port ]]; then
  service_port=$(docker port "$service_container" 2>/dev/null | sed -n 's/.*://p' | head -1)
fi
if [[ ! $service_port =~ ^[0-9]+$ ]]; then
  echo "无法解析候选服务宿主机端口；可显式设置 SERVICE_PORT。" >&2
  exit 1
fi
service_base_url=${SERVICE_BASE_URL:-"http://127.0.0.1:$service_port"}

if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null; then
  echo "候选服务容器无法解析受控 capture 容器。" >&2
  exit 1
fi
if ! docker exec "$capture_container" sh -c 'command -v tcpdump' >/dev/null; then
  echo "capture 容器缺少 tcpdump。" >&2
  exit 1
fi
relay_help=$(docker exec "$capture_container" python3 "$relay_tool" --help 2>&1 || true)
if ! grep -q 'candidate-core-v1' <<<"$relay_help" ||
  ! grep -q -- '--codex-version' <<<"$relay_help"; then
  echo "capture 容器中的 relay 尚未同步目标版本参数或 candidate-core-v1。" >&2
  exit 1
fi
if [[ ! -r $gateway_ws_driver ]]; then
  echo "候选网关 WebSocket 双轮驱动器缺失。" >&2
  exit 1
fi
gateway_help=$(python3 "$gateway_ws_driver" --help 2>&1 || true)
if ! grep -q -- '--api-key-fd' <<<"$gateway_help" ||
  ! grep -q -- '--codex-version' <<<"$gateway_help"; then
  echo "候选网关 WebSocket 双轮驱动器缺失或没有目标版本参数。" >&2
  exit 1
fi

docker cp "$service_container:/etc/hosts" "$runtime_dir/hosts.before" >/dev/null
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" \
  "$runtime_dir/ca-certificates.before" >/dev/null
chmod 0600 "$runtime_dir/hosts.before" "$runtime_dir/ca-certificates.before"
original_hosts_hash=$(sha256sum "$runtime_dir/hosts.before" | awk '{print $1}')
original_ca_hash=$(sha256sum "$runtime_dir/ca-certificates.before" | awk '{print $1}')

if docker exec "$service_container" test -e "$custom_ca_path"; then
  echo "临时 CA 路径已存在，拒绝覆盖：$custom_ca_path" >&2
  exit 1
fi
custom_ca_baseline_absent=1
keeper_was_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
if [[ $keeper_was_running == true ]]; then
  docker stop "$keeper_container" >/dev/null
fi

openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf '%s\n' \
  'subjectAltName=DNS:chatgpt.com' \
  'extendedKeyUsage=serverAuth' >"$tls_dir/relay.ext"
serial=$(openssl rand -hex 16)
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -set_serial "0x$serial" -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 0600 "$tls_dir"/*

proxy_name="candidate-core-${run_id:0:72}"
proxy_id=$(db_query "
insert into proxies (name,protocol,host,port,status,fallback_mode)
values ('$proxy_name','http','$capture_container',$relay_port,'active','none')
returning id")
if [[ ! $proxy_id =~ ^[0-9]+$ ]]; then
  echo "创建临时受控代理失败。" >&2
  exit 1
fi
proxy_created=1
db_query "update accounts set proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $account_id" \
  >/dev/null
proxy_bound=1

if ! docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null; then
  # 基线已确认该路径不存在；即使复制只完成了一部分，恢复钩子也必须尝试清理。
  ca_installed=1
  echo "安装候选核心抓包 CA 失败。" >&2
  exit 1
fi
ca_installed=1
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1
restart_service

start_capture() {
  local scenario=$1
  local scenario_root="$work_dir/scenarios/$scenario"
  local container_scenario_root="$container_work_dir/scenarios/$scenario"
  install -d -m 0700 \
    "$scenario_root/relay-private" "$scenario_root/relay" "$scenario_root/trigger"
  docker exec "$capture_container" mkdir -p \
    "$container_scenario_root/relay-private" "$container_scenario_root/trigger"
  current_scenario=$scenario
  clear_account_gate

  docker exec "$capture_container" sh -c '
    umask 077
    python3 "$1" --cert "$2" --key "$3" --mode connect --port "$4" \
      --upstream-host chatgpt.com --output "$5" --timeout 420 \
      --codex-version "$8" \
      --synthetic-profile candidate-core-v1 --allow-synthetic-responses \
      --candidate-core-scenario "$6" --candidate-core-ws-failures "$7" \
      >"$9" 2>&1 &
    echo $! >"${10}"
  ' sh "$relay_tool" "$container_tls_dir/relay.crt" "$container_tls_dir/relay.key" \
    "$relay_port" "$container_scenario_root/relay-private" "$scenario" \
    "$ws_failure_count" "$codex_version" "$container_scenario_root/relay.log" \
    "$container_scenario_root/relay.pid"
  relay_started=1

  docker exec "$capture_container" sh -c '
    umask 077
    tcpdump -U -n -s 0 -i any port "$1" -w "$2" >"$3" 2>&1 &
    echo $! >"$4"
  ' sh "$relay_port" "$container_scenario_root/egress.pcap" \
    "$container_scenario_root/tcpdump.log" "$container_scenario_root/pcap.pid"
  pcap_started=1
  sleep 1
}

wait_action() {
  local scenario=$1
  local action=$2
  local minimum=${3:-1}
  local path="$work_dir/scenarios/$scenario/relay-private/intervention.jsonl"
  local count
  for _ in $(seq 1 300); do
    count=0
    if [[ -f $path ]]; then
      count=$(grep -c "\"action\": \"$action\"" "$path" 2>/dev/null || true)
    fi
    if (( count >= minimum )); then
      return 0
    fi
    sleep 0.1
  done
  echo "$scenario 未观察到动作 $action（至少 $minimum 次）。" >&2
  return 1
}

set_account_features() {
  local residency=$1
  local metrics=$2
  local patch='{}'
  if [[ $residency == us ]]; then
    patch='{"official_codex_enforce_residency":"us"}'
  fi
  if [[ $metrics == true ]]; then
    if [[ $patch == '{}' ]]; then
      patch='{"official_codex_runtime_metrics":true}'
    else
      patch='{"official_codex_enforce_residency":"us","official_codex_runtime_metrics":true}'
    fi
  fi
  db_query "update accounts set extra = (extra - 'official_codex_enforce_residency' - 'official_codex_runtime_metrics') || '$patch'::jsonb where id = $account_id" \
    >/dev/null
  restart_service
  # 条件造完必须自证。此前这里设完就跑，update 影响了几行、字段是否真的落库全不查——
  # 条件没造出来时脚本一无所知，照样往下走，最后只在 accept 阶段以「头缺失」的形式暴露，
  # 中间隔着十几个步骤。k71 的 HDR-002/residency-positive 就卡在这：链路每一环（采集顺序、
  # 生产代码、画像槽位、账号 ID）事后逐层复查都对，唯独采集当时的账号状态没留证据，
  # 无法还原。加这道断言后，下一轮同样失败时可立即分清是哪一侧：
  # 断言过了头还缺 → 生产代码或画像；断言没过 → 条件根本没造出来。
  local expected_residency="null"
  local expected_metrics="null"
  [[ $residency == us ]] && expected_residency='"us"'
  [[ $metrics == true ]] && expected_metrics="true"
  local observed
  observed=$(db_query "select coalesce(extra->'official_codex_enforce_residency','null'::jsonb)::text || '|' || coalesce(extra->'official_codex_runtime_metrics','null'::jsonb)::text from accounts where id = $account_id")
  if [[ $observed != "$expected_residency|$expected_metrics" ]]; then
    echo "账号 $account_id 的受管字段未按预期落库：期望 $expected_residency|$expected_metrics，实际 ${observed:-<空>}。" >&2
    return 1
  fi
}

restore_account_features() {
  db_query "update accounts set extra = convert_from(decode('$original_extra_hex','hex'),'UTF8')::jsonb where id = $account_id" \
    >/dev/null
  restart_service
}

write_request_body() {
  local output=$1
  local model=$2
  local mode=$3
  local variant=$4
  local previous_response_id=${5:-}
  local compaction=${6:-false}
  local thread_source=${7:-user}
  local subagent_kind=${8:-}
  local parent_thread_id=${9:-}
  local compaction_reason=${10:-}
  local subagent_header=${11:-}
  python3 - "$output" "$model" "$mode" "$variant" "$previous_response_id" \
    "$compaction" "$thread_source" "$subagent_kind" "$parent_thread_id" \
    "$compaction_reason" "$subagent_header" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    output,
    model,
    mode,
    variant,
    previous_response_id,
    compaction,
    thread_source,
    subagent_kind,
    parent_thread_id,
    compaction_reason,
    subagent_header,
) = sys.argv[1:]
session_id = "11111111-1111-4111-8111-111111111111"
thread_id = session_id
if thread_source in {"memory_consolidation", "subagent"}:
    thread_id = "55555555-5555-4555-8555-555555555555"
turn_id = "22222222-2222-4222-8222-222222222222"
metadata = {
    "installation_id": "33333333-3333-4333-8333-333333333333",
    "session_id": session_id,
    "thread_id": thread_id,
    "turn_id": turn_id,
    "window_id": thread_id + ":0",
    "request_kind": "turn",
    "thread_source": thread_source,
    "capture_variant": variant,
}
if subagent_kind:
    metadata["subagent_kind"] = subagent_kind
if parent_thread_id:
    metadata["parent_thread_id"] = parent_thread_id
if compaction_reason:
    metadata["compaction_reason"] = compaction_reason
turn_metadata = json.dumps(metadata, separators=(",", ":"))
payload = {
    "model": model,
    "instructions": "候选核心抓包固定指令",
    "input": [{"type": "message", "role": "user", "content": "candidate core probe"}],
    "tools": [{
        "type": "function",
        "name": "read_file",
        "description": "读取文件",
        "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }],
    "tool_choice": "auto",
    "parallel_tool_calls": True,
    "reasoning": {"effort": "high", "context": "none", "summary": "auto"},
    "store": False,
    "stream": True,
    "include": ["reasoning.encrypted_content"],
    "prompt_cache_key": session_id,
    "client_metadata": {
        "x-codex-installation-id": metadata["installation_id"],
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "x-codex-window-id": metadata["window_id"],
        "x-codex-turn-metadata": turn_metadata,
    },
}
if subagent_header:
    payload["client_metadata"]["x-openai-subagent"] = subagent_header
if parent_thread_id:
    payload["client_metadata"]["x-codex-parent-thread-id"] = parent_thread_id
if mode == "lite":
    # 目标 Codex 已根据模型 manifest 完成 Lite 定型后才进入严格入口：
    # 顶层 instructions/tools 不存在，开发者指令与工具目录分别成为 input
    # 前缀，且 Lite 固定关闭并行工具调用。严格入口只校验该形态，不代替
    # 官方客户端执行结构变换。
    developer_message = {
        "type": "message",
        "role": "developer",
        "content": [{
            "type": "input_text",
            "text": payload.pop("instructions"),
        }],
    }
    additional_tools = {
        "type": "additional_tools",
        "role": "developer",
        "tools": payload.pop("tools"),
    }
    payload["input"] = [additional_tools, developer_message, *payload["input"]]
    payload["parallel_tool_calls"] = False
    payload["reasoning"]["context"] = "all_turns"
elif mode == "non_lite":
    payload["reasoning"]["context"] = "all_turns"
else:
    raise SystemExit(f"未知请求模式：{mode}")
if previous_response_id:
    payload["previous_response_id"] = previous_response_id
    payload["input"].append({
        "type": "message",
        "role": "user",
        "content": "candidate continuation",
    })
if compaction == "true":
    payload["input"].append({"type": "compaction_trigger"})
data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
Path(output).write_bytes(data)
os.chmod(output, 0o600)
metadata_path = Path(output + ".turn-metadata")
metadata_path.write_text(turn_metadata + "\n", encoding="utf-8")
os.chmod(metadata_path, 0o600)
thread_path = Path(output + ".thread-id")
thread_path.write_text(thread_id + "\n", encoding="utf-8")
os.chmod(thread_path, 0o600)
window_path = Path(output + ".window-id")
window_path.write_text(metadata["window_id"] + "\n", encoding="utf-8")
os.chmod(window_path, 0o600)
PY
}

prepare_a06_bodies() {
  local first_body=$1
  local continuation_body=$2
  python3 - "$first_body" "$continuation_body" <<'PY'
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

first_path, continuation_path = map(Path, sys.argv[1:])
first = json.loads(first_path.read_text(encoding="utf-8"))
continuation = json.loads(continuation_path.read_text(encoding="utf-8"))
carrier = {
    "type": "additional_tools",
    "role": "developer",
    "tools": [{
        "type": "custom",
        "name": "exec",
        "description": "执行候选核心验证命令",
    }],
}
first_input = first.get("input")
if not isinstance(first_input, list) or not first_input:
    raise SystemExit("A06 首轮 input 缺失")
first["input"] = [carrier, *first_input]

# 续轮模板不预置响应 ID；驱动器只在内存中注入首轮真实业务 response.id。
# 输入保留首轮业务前缀并追加一条用户消息，明确形成可复用前缀的增量帧。
continuation["input"] = [
    deepcopy(item)
    for item in first["input"]
    if not (isinstance(item, dict) and item.get("type") == "additional_tools")
]
continuation["input"].append({
    "type": "message",
    "role": "user",
    "content": "candidate continuation",
})
for path, payload in ((first_path, first), (continuation_path, continuation)):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
PY
}

compress_zstd() {
  local source=$1
  local output=$2
  local container_source=${source/#$capture_root/$capture_mount}
  local container_output=${output/#$capture_root/$capture_mount}
  docker exec -i "$capture_container" python3 - "$container_source" "$container_output" <<'PY'
import os
import sys
from pathlib import Path

try:
    import zstandard
except ModuleNotFoundError as error:
    raise SystemExit("capture 容器缺少 zstandard，不能生成真实 zstd 入站体") from error
source, output = map(Path, sys.argv[1:])
output.write_bytes(zstandard.ZstdCompressor(level=3).compress(source.read_bytes()))
os.chmod(output, 0o600)
PY
  install -m 0600 "$source.turn-metadata" "$output.turn-metadata"
  install -m 0600 "$source.thread-id" "$output.thread-id"
  install -m 0600 "$source.window-id" "$output.window-id"
}

extract_response_id() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line.startswith("data: ") or line == "data: [DONE]":
        continue
    try:
        event = json.loads(line[6:])
    except json.JSONDecodeError:
        continue
    response = event.get("response") if isinstance(event, dict) else None
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        print(response["id"])
        raise SystemExit(0)
raise SystemExit(1)
PY
}

exec_ua="codex_exec/$codex_version (Ubuntu 24.4.0; x86_64) unknown (codex_exec; $codex_version)"
tui_ua="codex-tui/$codex_version (Ubuntu 24.4.0; x86_64) xterm-256color (codex-tui; $codex_version)"
gateway_driver_ua='sub2apiplus-candidate-capture/1.0'
gateway_driver_originator='sub2apiplus_candidate_capture'
session_id=11111111-1111-4111-8111-111111111111
parent_id=44444444-4444-4444-8444-444444444444

official_headers() {
  local ua=$1
  local originator=$2
  local thread_id=${3:-$session_id}
  local window_id=${4:-$session_id:0}
  printf '%s\n' \
    "User-Agent: $ua" \
    "Originator: $originator" \
    "Version: $codex_version" \
    'X-Codex-Terminal: unknown' \
    "Session-Id: $session_id" \
    "Thread-Id: $thread_id" \
    "X-Client-Request-Id: $thread_id" \
    "X-Codex-Window-Id: $window_id"
}

run_response_request() {
  local scenario=$1
  local label=$2
  local body=$3
  local ua=$4
  local originator=$5
  shift 5
  local trigger_root="$work_dir/scenarios/$scenario/trigger"
  local output="$trigger_root/$label.sse"
  local headers=()
  local turn_metadata thread_id window_id
  turn_metadata=$(<"$body.turn-metadata")
  thread_id=$(<"$body.thread-id")
  window_id=$(<"$body.window-id")
  while IFS= read -r line; do
    headers+=(-H "$line")
  done < <(official_headers "$ua" "$originator" "$thread_id" "$window_id")
  headers+=(-H "X-Codex-Turn-Metadata: $turn_metadata")
  local code
  code=$(request_with_token "$api_key" --output "$output" --write-out '%{http_code}' \
    -X POST "${headers[@]}" "$@" -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' --data-binary "@$body" \
    "$service_base_url/v1/responses")
  assert_2xx "$scenario-$label" "$code"
}

run_response_ws_session() {
  local first_body=$1
  local continuation_body=$2
  local trigger_root=$3
  # 与 request_with_token 同一条理由：relay 在场的整个窗口里，Sub2API 的后台出站会持续
  # 触发临时熔断，只在 start_capture 清一次不够。WS 驱动原先漏了这一步，A06 因此在
  # 握手阶段被服务端以 1013 关闭，pcap 里一个包都没有。
  clear_account_gate
  python3 "$gateway_ws_driver" \
    --host 127.0.0.1 \
    --port "$service_port" \
    --path /v1/responses \
    --first-body "$first_body" \
    --second-body "$continuation_body" \
    --first-output "$trigger_root/first.sse" \
    --second-output "$trigger_root/continuation.sse" \
    --summary "$trigger_root/ws-session.json" \
    --codex-version "$codex_version" \
    --session-affinity candidate-core-a06 \
    --timeout 180 \
    --api-key-fd 3 \
    3< <(printf '%s' "$api_key")
}

# A03：先用 Lite zstd 冷请求建立 Cookie jar；该请求与官方 Lite 专项的
# 冷启动前提一致。随后抓默认非 Lite 样本，并用两轮 Lite zstd 建立
# turn-state 闭环。
start_capture A03
trigger_root="$work_dir/scenarios/A03/trigger"
write_request_body "$trigger_root/prime.json" "$lite_model" lite a03-cookie-prime
compress_zstd "$trigger_root/prime.json" "$trigger_root/prime.zst"
run_response_request A03 prime "$trigger_root/prime.zst" \
  "$exec_ua" codex_exec -H 'Content-Encoding: zstd'
write_request_body "$trigger_root/default.json" "$main_model" non_lite a03-default
compress_zstd "$trigger_root/default.json" "$trigger_root/default.zst"
run_response_request A03 default "$trigger_root/default.zst" \
  "$exec_ua" codex_exec -H 'Content-Encoding: zstd'
write_request_body "$trigger_root/lite.json" "$lite_model" lite a03-turn
compress_zstd "$trigger_root/lite.json" "$trigger_root/lite.zst"
for turn in 1 2; do
  run_response_request A03 "lite-turn-$turn" "$trigger_root/lite.zst" \
    "$exec_ua" codex_exec -H 'Content-Encoding: zstd'
done
wait_action A03 responses_http_success 4
stop_capture

# A04：非 Lite 明文；账号管理态与经过 metadata 交叉验证的条件头分别形成正负样本。
start_capture A04
trigger_root="$work_dir/scenarios/A04/trigger"
write_request_body "$trigger_root/non-lite.json" "$main_model" non_lite a04-baseline
set_account_features unset false
run_response_request A04 baseline "$trigger_root/non-lite.json" "$exec_ua" codex_exec
set_account_features us false
run_response_request A04 residency-us "$trigger_root/non-lite.json" "$exec_ua" codex_exec
set_account_features unset true
write_request_body "$trigger_root/memgen.json" "$main_model" non_lite a04-memgen \
  '' false memory_consolidation '' '' '' memory_consolidation
run_response_request A04 memgen "$trigger_root/memgen.json" "$exec_ua" codex_exec \
  -H 'X-OpenAI-Subagent: memory_consolidation' \
  -H 'X-OpenAI-Memgen-Request: true'
write_request_body "$trigger_root/parent-thread.json" "$main_model" non_lite a04-parent \
  '' false subagent thread_spawn "$parent_id" '' collab_spawn
run_response_request A04 parent-thread "$trigger_root/parent-thread.json" "$exec_ua" codex_exec \
  -H 'X-OpenAI-Subagent: collab_spawn' \
  -H "X-Codex-Parent-Thread-Id: $parent_id"
restore_account_features
wait_action A04 responses_http_success 4
stop_capture

# A05：普通 HTTP 入口让网关扮演 Campaign 目标 Codex 客户端并默认选择 WS；
# 官方 HTTP 入口代表 CLI 已耗尽自身 WS 预算，生产代码会按画像强制 HTTP fallback。
start_capture A05
trigger_root="$work_dir/scenarios/A05/trigger"
write_request_body "$trigger_root/lite.json" "$lite_model" lite a05-turn
for turn in 1 2; do
  run_response_request A05 "turn-$turn" "$trigger_root/lite.json" \
    "$gateway_driver_ua" "$gateway_driver_originator" \
    -H 'X-Session-Affinity: candidate-core-a05'
done
wait_action A05 responses_ws_handshake_success
wait_action A05 responses_ws_response_create 2
stop_capture

# A06：非 Lite WS；同一入站连接串行发送两轮，第二轮只在内存中注入上一轮
# 真实业务 response.id。上游依次出现预热、首轮业务和前缀复用续轮三帧。
start_capture A06
trigger_root="$work_dir/scenarios/A06/trigger"
write_request_body "$trigger_root/first.json" "$main_model" non_lite a06-first
write_request_body "$trigger_root/continuation.json" "$main_model" non_lite a06-continuation
prepare_a06_bodies "$trigger_root/first.json" "$trigger_root/continuation.json"
run_response_ws_session \
  "$trigger_root/first.json" \
  "$trigger_root/continuation.json" \
  "$trigger_root"
first_response_id=$(extract_response_id "$trigger_root/first.sse")
continuation_response_id=$(extract_response_id "$trigger_root/continuation.sse")
if [[ $first_response_id != resp_candidate_core_a06_0002 ]]; then
  echo "A06 首轮业务 response.id 未证明 0001 预热已被网关消费。" >&2
  exit 1
fi
if [[ $continuation_response_id != resp_candidate_core_a06_0003 ||
  $continuation_response_id == "$first_response_id" ]]; then
  echo "A06 续轮未在同一连接取得独立的第三个受控 response.id。" >&2
  exit 1
fi
wait_action A06 responses_ws_handshake_success
wait_action A06 responses_ws_response_create 3
stop_capture

# A07：单次生产入口调用；冻结次数的 502 WS 握手失败后才允许 HTTP SSE 成功。
start_capture A07
trigger_root="$work_dir/scenarios/A07/trigger"
write_request_body "$trigger_root/fallback.json" "$main_model" non_lite a07-fallback
run_response_request A07 fallback "$trigger_root/fallback.json" \
  "$gateway_driver_ua" "$gateway_driver_originator" \
  -H 'X-Session-Affinity: candidate-core-a07'
wait_action A07 responses_ws_retryable_failure "$ws_failure_count"
wait_action A07 responses_http_fallback_success
stop_capture
# A07 的受控 502 可能写入候选进程的短期模型阻断缓存；重启只清理进程态，账号、
# proxy 与证据均不变，避免它污染后续独立场景。
restart_service

# A08：只采真实跨上层调用连接。retry 内部关系由结构化测试补证，不在这里伪造。
start_capture A08
trigger_root="$work_dir/scenarios/A08/trigger"
write_request_body "$trigger_root/http.json" "$main_model" non_lite a08-cross-call
for call in 1 2 3; do
  run_response_request A08 "call-$call" "$trigger_root/http.json" "$exec_ua" codex_exec
done
wait_action A08 responses_http_success 3
stop_capture

# A10：四个真实远端 V2 请求都携带 compaction_trigger；TokenBudget 零出站不造包。
start_capture A10
trigger_root="$work_dir/scenarios/A10/trigger"
for reason in user_requested context_limit model_downshift comp_hash_changed; do
  write_request_body "$trigger_root/$reason.json" "$main_model" non_lite "a10-$reason" \
    '' true user '' '' "$reason"
  run_response_request A10 "$reason" "$trigger_root/$reason.json" "$tui_ua" codex-tui
done
wait_action A10 responses_http_success 4
stop_capture

# A15：三种冻结身份经生产入口生成真实出站。进程来源本身只由 test trace 证明。
start_capture A15
trigger_root="$work_dir/scenarios/A15/trigger"
for variant in exec-suffix tui-suffix initial-no-suffix; do
  case "$variant" in
    exec-suffix) ua=$exec_ua; originator=codex_exec ;;
    tui-suffix) ua=$tui_ua; originator=codex-tui ;;
    initial-no-suffix)
      ua="codex_exec/$codex_version (Ubuntu 24.4.0; x86_64) unknown"
      originator=codex_cli_rs
      ;;
  esac
  code=$(request_with_token "$api_key" --output "$trigger_root/models-$variant.json" \
    --write-out '%{http_code}' -H "User-Agent: $ua" -H "Originator: $originator" \
    -H "Version: $codex_version" \
    "$service_base_url/backend-api/codex/models?client_version=$codex_version")
  assert_2xx "A15-models-$variant" "$code"
done
wait_action A15 models_manifest 3
stop_capture

# 冻结动作和无生产转发门禁。A03 的 zstd 与 A10 的 trigger 直接在 scrubbed raw 中复核。
python3 - "$work_dir" "$ws_failure_count" "$codex_version" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
ws_failures = int(sys.argv[2])
codex_version = sys.argv[3]
minimums = {
    "A03": {"responses_http_success": 4},
    "A04": {"responses_http_success": 4},
    "A05": {"responses_ws_handshake_success": 1, "responses_ws_response_create": 2},
    "A06": {"responses_ws_handshake_success": 1, "responses_ws_response_create": 3},
    "A07": {"responses_ws_retryable_failure": ws_failures, "responses_http_fallback_success": 1},
    "A08": {"responses_http_success": 3},
    "A10": {"responses_http_success": 4},
    "A15": {"models_manifest": 3},
}
for scenario, wanted in minimums.items():
    scenario_root = root / "scenarios" / scenario / "relay"
    manifest = json.loads((scenario_root / "relay.json").read_text(encoding="utf-8"))
    if manifest.get("synthetic_profile") != "candidate-core-v1":
        raise SystemExit(f"{scenario} relay 未绑定 candidate-core-v1")
    if manifest.get("codex_version") != codex_version:
        raise SystemExit(f"{scenario} relay Codex 版本与 Campaign 目标不一致")
    if manifest.get("candidate_core_scenario") != scenario:
        raise SystemExit(f"{scenario} relay 场景绑定错误")
    if manifest.get("production_forwarding_enabled") is not False:
        raise SystemExit(f"{scenario} relay 仍允许生产转发")
    for connection in manifest.get("connections", []):
        if connection.get("valid") is not True:
            raise SystemExit(f"{scenario} 存在无效 relay 连接: {connection.get('error')}")
        if connection.get("production_forwarded") is not False:
            raise SystemExit(f"{scenario} 连接未证明 production_forwarded=false")
    counts = Counter()
    events = scenario_root / "intervention.jsonl"
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") != "synthetic_core_response":
            raise SystemExit(f"{scenario} 出现非核心白名单事件: {event.get('type')}")
        if event.get("production_forwarded") is not False:
            raise SystemExit(f"{scenario} 出现未证明为本地合成的事件")
        counts[event["action"]] += 1
    for action, minimum in wanted.items():
        actual = counts[action]
        if scenario in {"A03", "A06", "A07"} and actual != minimum:
            raise SystemExit(f"{scenario} {action} 次数 {actual} != {minimum}")
        if actual < minimum:
            raise SystemExit(f"{scenario} {action} 次数 {actual} < {minimum}")

a03_root = root / "scenarios/A03/relay"
a03_pairs = []
for request_path in sorted(a03_root.glob("*.client_to_upstream.bin")):
    request = request_path.read_bytes()
    if not request.startswith(b"POST /backend-api/codex/responses HTTP/1.1\r\n"):
        continue
    response_path = Path(str(request_path).replace(
        ".client_to_upstream.bin", ".upstream_to_client.bin"
    ))
    if not response_path.is_file():
        raise SystemExit(f"A03 请求缺少同连接响应: {request_path.name}")
    a03_pairs.append((request.lower(), response_path.read_bytes().lower()))
if len(a03_pairs) != 4:
    raise SystemExit(f"A03 Responses 请求数 {len(a03_pairs)} != 4")
if any(b"\r\ncontent-encoding: zstd\r\n" not in request for request, _ in a03_pairs):
    raise SystemExit("A03 四次 Responses 未全部使用真实 content-encoding:zstd")
if b"\r\ncookie:" in a03_pairs[0][0]:
    raise SystemExit("A03 prime 请求的冷 Cookie jar 意外非空")
if any(b"\r\ncookie: <secret>" not in request for request, _ in a03_pairs[1:]):
    raise SystemExit("A03 default 及两轮 Lite 请求未持续回放已脱敏 Cookie")
if b"\r\nset-cookie: <secret>" not in a03_pairs[0][1]:
    raise SystemExit("A03 prime 响应未保留已脱敏 Set-Cookie 证据")
if any(b"_cfuvid" in data for pair in a03_pairs for data in pair):
    raise SystemExit("A03 公开 relay 产物泄漏 _cfuvid Cookie 名或值")
turn_state = b"\r\nx-codex-turn-state: turn-state-candidate-core-0145\r\n"
if any(turn_state in request for request, _ in a03_pairs[:3]):
    raise SystemExit("A03 turn-state 在 Lite 续轮之前意外出现")
if any(turn_state in response for _, response in a03_pairs[:2]):
    raise SystemExit("A03 turn-state 在 Lite 首轮响应之前意外下发")
if turn_state not in a03_pairs[2][1]:
    raise SystemExit("A03 首轮 Lite 响应未下发 turn-state")
if turn_state not in a03_pairs[3][0]:
    raise SystemExit("A03 第二轮 Lite 请求未回放 turn-state")
a03_events = [
    json.loads(line)
    for line in (a03_root / "intervention.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
cookie_events = [
    event for event in a03_events
    if event.get("set_cookie_names") == ["_cfuvid"]
]
if len(cookie_events) != 1:
    raise SystemExit(f"A03 allowlist Set-Cookie _cfuvid 事件数 {len(cookie_events)} != 1")
a04_bytes = b"".join(
    path.read_bytes()
    for path in (root / "scenarios/A04/relay").glob("*.client_to_upstream.bin")
)
if b"\r\ncontent-encoding: zstd\r\n" in a04_bytes.lower():
    raise SystemExit("A04 非 Lite 明文场景意外出现 zstd")
a10_bytes = b"".join(
    path.read_bytes()
    for path in (root / "scenarios/A10/relay").glob("*.client_to_upstream.bin")
)
if a10_bytes.count(b'"type":"compaction_trigger"') < 4:
    raise SystemExit("A10 未抓到四个真实 compaction_trigger")
PY

capture_status=complete
printf 'run_id=%s\n' "$run_id"
