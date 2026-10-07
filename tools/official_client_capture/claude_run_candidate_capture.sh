#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# Claude 候选侧受控取证（Claude 升级控制面设计 B4-01）：一个 FW-F v5 probe 的候选网关隔离包装。
#
# 参数形态对齐 Codex 的 run_candidate_core_capture.sh：全部参数经环境变量给出（场景清单候选步骤的 environment，逐字核对
# 见 claude_upgrade_plan._validate_candidate_step），位置参数是要在隔离环境里执行的中继候选模式命令：
#   docker exec <抓包容器> python3 <工具根>/claude_fw_e_relay.py capture --execute ... \
#     --candidate-runtime-dir <运行目录> --candidate-relay-port <端口>
#
# 拓扑：官方客户端在抓包容器里经 base URL 加专用 API Key 打候选网关；网关专用 Anthropic OAuth 账号临时绑定的代理就是
# 抓包容器里的 connect 中继，中继记录网关的最终出站字节（真实上游场景按冻结上游 IP 转发，合成场景本地应答、绝不转发）。
#
# 安全边界：
#   1. 只有显式开关 CLAUDE_CANDIDATE_CAPTURE=YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE 才启动；
#   2. API Key 分组必须是该账号的单账号隔离分组，不临时禁用、也不影响其他生产账号；
#   3. 账号 proxy／fallback、调度门、服务容器 hosts 与 CA bundle、keeper 状态按运行前值精确恢复并逐项核验，
#      恢复失败固定退出 97；
#   4. 网关 API Key、账号令牌与管理凭据只写进本次私有运行目录（0700／0600），中继只在内存里使用并做逐字节秘密扫描，
#      本脚本退出前无论成败都删除这些文件；
#   5. 账号令牌只读取用于秘密扫描，不改写 credentials；一次只允许一个候选取证持有锁。

required_gate=YES_I_ACCEPT_CANDIDATE_GATEWAY_CAPTURE
if [[ ${CLAUDE_CANDIDATE_CAPTURE:-} != "$required_gate" ]]; then
  echo "拒绝启动：必须显式设置 CLAUDE_CANDIDATE_CAPTURE=${required_gate}。" >&2
  exit 2
fi

campaign_dir=${CAMPAIGN_DIR:-}
capture_root=${CAPTURE_ROOT:-}
capture_container=${CAPTURE_CONTAINER:-}
service_container=${SERVICE_CONTAINER:-}
keeper_container=${KEEPER_CONTAINER:-}
postgres_container=${POSTGRES_CONTAINER:-}
account_id=${ACCOUNT_ID:-}
api_key_id=${API_KEY_ID:-}
run_id=${RUN_ID:-}
probe_id=${PROBE_ID:-}
output_root=${OUTPUT_ROOT:-}
runtime_dir=${CANDIDATE_RUNTIME_DIR:-}
relay_port=${RELAY_PORT:-}
gateway_trigger=${GATEWAY_TRIGGER:-}
ca_cert=${CA_CERT:-}

safe_name='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
safe_path='^/[A-Za-z0-9._/+:-]+$'
for pair in "CAPTURE_CONTAINER=$capture_container" "SERVICE_CONTAINER=$service_container" \
  "KEEPER_CONTAINER=$keeper_container" "POSTGRES_CONTAINER=$postgres_container" "RUN_ID=$run_id" \
  "PROBE_ID=$probe_id"; do
  if [[ ! ${pair#*=} =~ $safe_name ]]; then
    echo "${pair%%=*} 缺失或含非法字符。" >&2
    exit 2
  fi
done
for pair in "ACCOUNT_ID=$account_id" "API_KEY_ID=$api_key_id" "RELAY_PORT=$relay_port"; do
  if [[ ! ${pair#*=} =~ ^[1-9][0-9]{0,9}$ ]]; then
    echo "${pair%%=*} 必须是正整数（候选 Campaign 必须登记候选账号与 ingress API Key）。" >&2
    exit 2
  fi
done
if (( relay_port < 1024 || relay_port > 65535 )); then
  echo "RELAY_PORT 必须在 1024..65535。" >&2
  exit 2
fi
case "$gateway_trigger" in
  client | gateway-account-refresh) ;;
  *)
    echo "GATEWAY_TRIGGER 只能是 client 或 gateway-account-refresh。" >&2
    exit 2
    ;;
esac
for pair in "CAMPAIGN_DIR=$campaign_dir" "CAPTURE_ROOT=$capture_root" "OUTPUT_ROOT=$output_root" \
  "CANDIDATE_RUNTIME_DIR=$runtime_dir" "CA_CERT=$ca_cert"; do
  value=${pair#*=}
  if [[ ! $value =~ $safe_path || $value == *"/../"* || $value == *"/.." || $value == *"/./"* ]]; then
    echo "${pair%%=*} 必须是规范安全绝对路径。" >&2
    exit 2
  fi
done

# 位置参数：在隔离环境里执行的中继候选模式命令，必须与环境变量逐项对应（场景清单在 plan 时已核对同一合同）。
if (( $# < 8 )) || [[ $1 != docker || $2 != exec || $3 != "$capture_container" || $4 != python3 ]]; then
  echo "位置参数必须是 docker exec \$CAPTURE_CONTAINER python3 <中继> capture ... 命令。" >&2
  exit 2
fi
if [[ $5 != */claude_fw_e_relay.py || $6 != capture ]]; then
  echo "位置参数必须调用 claude_fw_e_relay.py capture。" >&2
  exit 2
fi
argument_value() {
  local flag=$1
  shift
  local found="" count=0
  while (( $# > 0 )); do
    if [[ $1 == "$flag" && $# -ge 2 ]]; then
      found=$2
      count=$((count + 1))
    fi
    shift
  done
  [[ $count == 1 ]] || return 1
  printf '%s' "$found"
}
for pair in "--run-id=$run_id" "--probe-id=$probe_id" "--fw-f-v5-probe=$probe_id" "--output-root=$output_root" \
  "--candidate-runtime-dir=$runtime_dir" "--candidate-relay-port=$relay_port" "--ca-cert=$ca_cert"; do
  flag=${pair%%=*}
  expected=${pair#*=}
  actual=$(argument_value "$flag" "$@") || {
    echo "中继命令缺少唯一的 ${flag}。" >&2
    exit 2
  }
  if [[ $actual != "$expected" ]]; then
    echo "中继命令的 $flag 与环境变量不一致。" >&2
    exit 2
  fi
done
for forbidden in --claude-credentials-file --hosts-file; do
  for item in "$@"; do
    if [[ $item == "$forbidden" ]]; then
      echo "候选取证不得携带 ${forbidden}。" >&2
      exit 2
    fi
  done
done

# 宿主数据根：Campaign 目录规范坐标 <数据根>/evidence/campaigns/<id>（与编排器 _host_data_root 同一规则）。
campaign_parent=$(dirname "$campaign_dir")
if [[ $(basename "$campaign_parent") != campaigns || $(basename "$(dirname "$campaign_parent")") != evidence ]]; then
  echo "CAMPAIGN_DIR 不在规范的 <数据根>/evidence/campaigns/<id> 下。" >&2
  exit 2
fi
host_data_root=$(dirname "$(dirname "$campaign_parent")")
if [[ ! -d $host_data_root || -L $host_data_root ]]; then
  echo "宿主数据根不存在或是符号链接：$host_data_root" >&2
  exit 2
fi
# 容器坐标 → 宿主坐标：只接受落在 CAPTURE_ROOT 之下的路径（抓包容器把宿主数据根挂在 CAPTURE_ROOT）。
host_path() {
  local value=$1
  if [[ $value != "$capture_root"/* ]]; then
    return 1
  fi
  printf '%s/%s' "$host_data_root" "${value#"$capture_root"/}"
}
runtime_host=$(host_path "$runtime_dir") || {
  echo "CANDIDATE_RUNTIME_DIR 不在 CAPTURE_ROOT 之下。" >&2
  exit 2
}
output_host=$(host_path "$output_root") || {
  echo "OUTPUT_ROOT 不在 CAPTURE_ROOT 之下。" >&2
  exit 2
}
report_dir="$(dirname "$output_host")/candidate-gateway"
report_path="$report_dir/$probe_id.json"
custom_ca_path=/usr/local/share/ca-certificates/claude-candidate-capture.crt
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

admin_token=""
if [[ $gateway_trigger == gateway-account-refresh ]]; then
  admin_token=${ADMIN_BEARER_TOKEN:-}
  if [[ -z $admin_token && -n ${ADMIN_BEARER_TOKEN_FILE:-} ]]; then
    if [[ ! -f $ADMIN_BEARER_TOKEN_FILE || -L $ADMIN_BEARER_TOKEN_FILE ]]; then
      echo "ADMIN_BEARER_TOKEN_FILE 不存在或是符号链接。" >&2
      exit 2
    fi
    admin_token=$(<"$ADMIN_BEARER_TOKEN_FILE")
  fi
  if [[ -z $admin_token || ! $admin_token =~ ^[A-Za-z0-9._~-]+$ ]]; then
    echo "账号刷新触发必须通过 ADMIN_BEARER_TOKEN 或只读 token 文件提供管理凭据。" >&2
    exit 2
  fi
  # 管理 token 只签 24 小时；过期时网关拒绝触发，表面上像网关没发刷新请求，必须看 exp。
  ADMIN_TOKEN_VALUE="$admin_token" MIN_TTL="${ADMIN_TOKEN_MIN_TTL_SECONDS:-900}" python3 - <<'PY' || exit 2
import base64, json, os, sys, time

token = os.environ["ADMIN_TOKEN_VALUE"]
parts = token.split(".")
if len(parts) != 3:
    sys.exit("管理 token 不是三段式 JWT，无法解析有效期。")
try:
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
except Exception as error:  # noqa: BLE001 —— 任何解析失败都拒绝启动
    sys.exit(f"管理 token 载荷无法解码：{type(error).__name__}")
exp = payload.get("exp")
if not isinstance(exp, int) or exp - int(time.time()) < int(os.environ["MIN_TTL"]):
    sys.exit("管理 token 缺少 exp 或剩余有效期不足；请重签后重试。")
PY
fi

lock_dir="$host_data_root/runtime/claude-candidate"
install -d -m 0700 "$lock_dir"
exec 8>"$lock_dir/.capture.lock"
if ! flock -n 8; then
  echo "另一个 Claude 候选取证正持有锁，拒绝并发执行。" >&2
  exit 2
fi
if [[ -e $runtime_host || -L $runtime_host ]]; then
  echo "候选运行目录已存在（上一轮未清理或并发残留），拒绝覆盖：$runtime_host" >&2
  exit 2
fi
if [[ -e $output_host || -L $output_host ]]; then
  echo "证据输出目录已存在，拒绝覆盖：$output_host" >&2
  exit 2
fi

db_user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_USER=//p')
db_name=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" | sed -n 's/^POSTGRES_DB=//p')
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

# 服务容器重启经共享监督器的出口维护声明执行（与 Codex 候选采集脚本同一入口）。
restart_service() {
  local -a maintenance_args=()
  [[ ${1:-} != cleanup ]] || maintenance_args+=(--cleanup)
  python3 "$script_dir/codex_upgrade_supervisor.py" egress-transition --container "$service_container" \
    "${maintenance_args[@]}" -- docker restart "$service_container" >/dev/null
  wait_healthy
}

# 账号调度门：connect 中继在场期间网关的后台出站也会打到中继上，被拒会让账号临时熔断；开跑前清一次，退出时按原值恢复。
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

proxy_state() {
  db_query "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id"
}

# ---- 只读前检：任何修改之前完成，前检失败不留下任何改动 -------------------------------------------------------
account_shape=$(db_query \
  "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') || '|' || case when deleted_at is null then 'live' else 'deleted' end from accounts where id = $account_id")
if [[ $account_shape != 'anthropic|oauth|NULL|live' ]]; then
  echo "ACCOUNT_ID 必须是未删除、非影子的 Anthropic OAuth 专用账号。" >&2
  exit 1
fi
token_present=$(db_query \
  "select case when length(coalesce(credentials->>'access_token','')) > 0 then 'true' else 'false' end from accounts where id = $account_id")
if [[ $token_present != true ]]; then
  echo "专用账号缺少当前 access token（需老板在后台完成 OAuth 授权）。" >&2
  exit 1
fi
api_key_shape=$(db_query \
  "select status || '|' || coalesce(group_id::text,'NULL') || '|' || case when deleted_at is null then 'live' else 'deleted' end from api_keys where id = $api_key_id")
if [[ ! $api_key_shape =~ ^active\|([0-9]+)\|live$ ]]; then
  echo "API_KEY_ID 不存在、未启用、已删除或未绑定分组。" >&2
  exit 1
fi
group_id=${BASH_REMATCH[1]}
eligible_accounts=$(db_query "
select a.id
from account_groups ag
join accounts a on a.id = ag.account_id
where ag.group_id = $group_id
  and a.platform = 'anthropic'
  and a.status = 'active'
  and a.schedulable = true
  and a.deleted_at is null
order by a.id")
if [[ $eligible_accounts != "$account_id" ]]; then
  echo "API Key 分组不是 ACCOUNT_ID 的单账号隔离分组，拒绝影响生产调度。" >&2
  exit 1
fi
original_proxy_state=$(proxy_state)
if [[ ! $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ ]]; then
  echo "无法读取账号 proxy/fallback 初始状态。" >&2
  exit 1
fi
original_gate_state=$(account_gate_state)
if [[ ! $original_gate_state =~ ^[0-9a-f]*\|[0-9a-f]*$ ]]; then
  echo "无法读取账号 #$account_id 的调度门初始状态。" >&2
  exit 1
fi
if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null; then
  echo "候选服务容器无法解析受控抓包容器（connect 中继不可达）。" >&2
  exit 1
fi
if ! docker exec "$capture_container" getent hosts "$service_container" >/dev/null; then
  echo "抓包容器无法解析候选服务容器（客户端打不到网关）。" >&2
  exit 1
fi
service_port=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" | sed -n 's/^SERVER_PORT=//p' | head -1)
service_port=${service_port:-8080}
if [[ ! $service_port =~ ^[1-9][0-9]{0,4}$ ]]; then
  echo "无法解析候选服务容器内端口。" >&2
  exit 1
fi
if docker exec "$service_container" test -e "$custom_ca_path"; then
  echo "临时 CA 路径已存在，拒绝覆盖：$custom_ca_path" >&2
  exit 1
fi

install -d -m 0700 "$runtime_host" "$runtime_host/restore"
# 快照阶段还没有任何修改：失败时只删掉本轮运行目录，不能留下让下一轮拒绝启动的残留。
trap 'rm -rf -- "$runtime_host"' EXIT ERR INT TERM
docker cp "$service_container:/etc/hosts" "$runtime_host/restore/hosts.before" >/dev/null
docker cp "$service_container:/etc/ssl/certs/ca-certificates.crt" "$runtime_host/restore/ca-certificates.before" >/dev/null
docker cp "$capture_container:$ca_cert" "$runtime_host/restore/capture-ca.crt" >/dev/null
chmod 0600 "$runtime_host"/restore/*
service_container_id_before=$(docker inspect -f '{{.Id}}' "$service_container")
original_hosts_hash=$(sha256sum "$runtime_host/restore/hosts.before" | awk '{print $1}')
original_ca_hash=$(sha256sum "$runtime_host/restore/ca-certificates.before" | awk '{print $1}')
keeper_was_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

proxy_id=""
proxy_created=0
proxy_bound=0
ca_installed=0
keeper_stopped=0
restore_failed=0
capture_exit_code=""
restored_hosts_hash=""
restored_ca_hash=""
restored_gate_equal=false
restored_proxy_equal=false

remove_runtime_secrets() {
  rm -f -- "$runtime_host/api-key" "$runtime_host/admin-token" "$runtime_host/known-secrets.json" \
    "$runtime_host/gateway.json"
}

write_report() {
  local status=$1
  install -d -m 0700 "$report_dir"
  python3 - "$report_path" "$status" "$run_id" "$probe_id" "$account_id" "$api_key_id" "$gateway_trigger" \
    "$original_proxy_state" "$restored_proxy_equal" "$restored_gate_equal" "$original_hosts_hash" \
    "$restored_hosts_hash" "$original_ca_hash" "$restored_ca_hash" "$keeper_was_running" "$capture_exit_code" \
    "$started_at" "$relay_port" <<'PY'
import json, os, sys, time

(path, status, run_id, probe_id, account_id, api_key_id, trigger, original_proxy, proxy_equal, gate_equal,
 hosts_before, hosts_after, ca_before, ca_after, keeper_was_running, capture_exit_code, started_at, relay_port) = sys.argv[1:]
report = {
    "schema_version": "claude-candidate-gateway-restoration/v1",
    "status": status,
    "run_id": run_id,
    "probe_id": probe_id,
    "account_id": int(account_id),
    "api_key_id": int(api_key_id),
    "trigger": trigger,
    "relay_port": int(relay_port),
    "capture_exit_code": int(capture_exit_code) if capture_exit_code.lstrip("-").isdigit() else None,
    "account_proxy_original": original_proxy,
    "account_proxy_restored_equal": proxy_equal == "true",
    "account_gate_restored_equal": gate_equal == "true",
    "hosts_sha256_equal": bool(hosts_before) and hosts_after == hosts_before,
    "ca_bundle_sha256_equal": bool(ca_before) and ca_after == ca_before,
    "keeper_was_running": keeper_was_running == "true",
    "runtime_secrets_removed": True,
    "started_at_utc": started_at,
    "ended_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
PY
}

restore_environment() {
  local original_exit_code=$?
  local proxy_value fallback_value service_container_id_after
  trap - EXIT ERR INT TERM
  set +e
  [[ -n $capture_exit_code ]] || capture_exit_code=$original_exit_code
  remove_runtime_secrets

  if [[ $proxy_bound == 1 && $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ ]]; then
    read -r proxy_value fallback_value <<<"${original_proxy_state/|/ }"
    db_query "update accounts set proxy_id = $proxy_value, proxy_fallback_origin_id = $fallback_value where id = $account_id" \
      >/dev/null || restore_failed=1
    proxy_bound=0
  fi
  if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
    db_query "delete from proxies where id = $proxy_id and name = 'claude-candidate-${run_id:0:64}'" >/dev/null ||
      restore_failed=1
    proxy_created=0
  fi
  if [[ $ca_installed == 1 ]]; then
    docker exec "$service_container" rm -f "$custom_ca_path" >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" update-ca-certificates --fresh >/dev/null 2>&1 || restore_failed=1
    restart_service cleanup || restore_failed=1
    ca_installed=0
  fi
  # docker restart 会重建 /etc/hosts：容器实例没换时按字节回灌运行前快照，随后不再重启。
  service_container_id_after=$(docker inspect -f '{{.Id}}' "$service_container" 2>/dev/null || true)
  if [[ -s $runtime_host/restore/hosts.before && $service_container_id_after == "$service_container_id_before" ]]; then
    docker cp "$runtime_host/restore/hosts.before" "$service_container:/tmp/claude-candidate-hosts.restore" \
      >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" sh -c \
      'cat /tmp/claude-candidate-hosts.restore > /etc/hosts && rm -f /tmp/claude-candidate-hosts.restore' \
      >/dev/null 2>&1 || restore_failed=1
  else
    restore_failed=1
  fi
  restored_ca_hash=$(docker exec "$service_container" sha256sum /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
  if [[ $restored_ca_hash != "$original_ca_hash" && -s $runtime_host/restore/ca-certificates.before ]]; then
    docker cp "$runtime_host/restore/ca-certificates.before" "$service_container:/tmp/claude-candidate-ca.restore" \
      >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" sh -c \
      'cat /tmp/claude-candidate-ca.restore > /etc/ssl/certs/ca-certificates.crt && rm -f /tmp/claude-candidate-ca.restore' \
      >/dev/null 2>&1 || restore_failed=1
    restored_ca_hash=$(docker exec "$service_container" sha256sum /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
  fi
  if [[ $keeper_stopped == 1 ]]; then
    docker start "$keeper_container" >/dev/null 2>&1 || restore_failed=1
  fi
  if restore_account_gate && [[ $(account_gate_state) == "$original_gate_state" ]]; then
    restored_gate_equal=true
  else
    restore_failed=1
  fi
  if [[ $(proxy_state 2>/dev/null) == "$original_proxy_state" ]]; then
    restored_proxy_equal=true
  else
    restore_failed=1
  fi
  restored_hosts_hash=$(docker exec "$service_container" sha256sum /etc/hosts 2>/dev/null | awk '{print $1}')
  [[ $restored_hosts_hash == "$original_hosts_hash" ]] || restore_failed=1
  [[ $restored_ca_hash == "$original_ca_hash" ]] || restore_failed=1
  docker exec "$service_container" test ! -e "$custom_ca_path" >/dev/null 2>&1 || restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    write_report restoration_failed || true
    echo "Claude 候选取证环境恢复失败；运行前快照保留在 $runtime_host/restore。" >&2
    exit 97
  fi
  if [[ $capture_exit_code == 0 ]]; then
    write_report complete || exit 97
  else
    write_report capture_failed || exit 97
  fi
  rm -rf -- "$runtime_host"
  echo "候选网关隔离已精确恢复：账号 proxy/fallback、调度门、hosts、CA bundle 与 keeper 状态均已核验。"
  exit "$capture_exit_code"
}

# ---- 从这里开始才修改 keeper、账号、代理与 CA；所有只读前检与恢复快照已经完成 -----------------------------------
trap restore_environment EXIT ERR INT TERM
if [[ $keeper_was_running == true ]]; then
  docker stop "$keeper_container" >/dev/null
  keeper_stopped=1
fi

proxy_id=$(db_query "
insert into proxies (name,protocol,host,port,status,fallback_mode)
values ('claude-candidate-${run_id:0:64}','http','$capture_container',$relay_port,'active','none')
returning id")
if [[ ! $proxy_id =~ ^[0-9]+$ ]]; then
  echo "创建临时受控代理失败。" >&2
  exit 1
fi
proxy_created=1
db_query "update accounts set proxy_id = $proxy_id, proxy_fallback_origin_id = null where id = $account_id" >/dev/null
proxy_bound=1
if [[ $(proxy_state) != "$proxy_id|NULL" ]]; then
  echo "账号临时代理绑定后复核不一致，拒绝触发任何出站。" >&2
  exit 1
fi

ca_installed=1
docker cp "$runtime_host/restore/capture-ca.crt" "$service_container:$custom_ca_path" >/dev/null
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1
restart_service
clear_account_gate

# 私有运行目录：网关坐标、API Key、账号令牌（只用于秘密扫描）与按需的管理凭据。值不进命令行参数与日志。
api_key=$(db_query "select key from api_keys where id = $api_key_id")
if [[ -z $api_key ]]; then
  echo "无法读取 API_KEY_ID 的 key 值。" >&2
  exit 1
fi
access_token=$(db_query "select coalesce(credentials->>'access_token','') from accounts where id = $account_id")
refresh_token=$(db_query "select coalesce(credentials->>'refresh_token','') from accounts where id = $account_id")
CANDIDATE_API_KEY="$api_key" CANDIDATE_ACCESS_TOKEN="$access_token" CANDIDATE_REFRESH_TOKEN="$refresh_token" \
  CANDIDATE_ADMIN_TOKEN="$admin_token" python3 - "$runtime_host" "$run_id" "$probe_id" "$service_container" \
  "$service_port" "$account_id" "$api_key_id" "$relay_port" "$gateway_trigger" <<'PY'
import json, os, sys
from pathlib import Path

root, run_id, probe_id, service, port, account_id, api_key_id, relay_port, trigger = sys.argv[1:]
root = Path(root)


def write(name, data):
    descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


write("gateway.json", (json.dumps({
    "schema_version": "claude-candidate-gateway-runtime/v1",
    "run_id": run_id,
    "probe_id": probe_id,
    "base_url": f"http://{service}:{int(port)}",
    "account_id": int(account_id),
    "api_key_id": int(api_key_id),
    "relay_port": int(relay_port),
    "trigger": trigger,
}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
write("api-key", os.environ["CANDIDATE_API_KEY"].encode("utf-8") + b"\n")
secrets = {
    key: value
    for key, value in (
        ("gateway_account_access_token", os.environ.get("CANDIDATE_ACCESS_TOKEN", "")),
        ("gateway_account_refresh_token", os.environ.get("CANDIDATE_REFRESH_TOKEN", "")),
    )
    if value
}
write("known-secrets.json", (json.dumps(secrets, sort_keys=True) + "\n").encode("utf-8"))
if trigger == "gateway-account-refresh":
    write("admin-token", os.environ["CANDIDATE_ADMIN_TOKEN"].encode("utf-8") + b"\n")
PY
api_key=""
access_token=""
refresh_token=""
admin_token=""

set +e
"$@"
capture_exit_code=$?
set -e
exit "$capture_exit_code"
