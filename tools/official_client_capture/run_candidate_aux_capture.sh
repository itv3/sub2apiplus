#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 候选侧 A09/A11/A12/A13/A14 受控辅助端点抓包。
#
# 安全边界：
#   1. 只有显式双开关才能启动 relay 的 candidate-aux-v1 合成画像；
#   2. 合成画像没有生产 upstream 配置，未知 host/path 一律本地 421；
#   3. WHAM consume、OAuth refresh、区域文件 PUT 都只到 capture 容器；
#   4. OAuth dummy refresh_token 在 relay 写盘前等长遮蔽；
#   5. 账号 proxy、hosts、CA、keeper 均按运行前值精确恢复，恢复失败固定退出 97。

required_gate=YES_I_ACCEPT_SYNTHETIC_ONLY
if [[ ${ENABLE_CANDIDATE_AUX_SYNTHETIC:-} != "$required_gate" ]]; then
  echo "拒绝启动：必须显式设置 ENABLE_CANDIDATE_AUX_SYNTHETIC=$required_gate。" >&2
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
redis_container=${REDIS_CONTAINER:-sub2apiplus-redis}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
capture_mount=${CAPTURE_MOUNT:-/capture}
account_id=${ACCOUNT_ID:?必须提供专用 OpenAI OAuth ACCOUNT_ID}
api_key_id=${API_KEY_ID:-1}
run_id=${RUN_ID:?必须提供 RUN_ID}
relay_port=${RELAY_PORT:-18443}
# 默认取 Lite 轨的权威模型（capturelib.model.LITE_TRACK_MODELS[0]），
# tests/test_main_track_models.py 锁定一致；原默认 gpt-5.6-sol 在 free 账号上 404。
model=${MODEL:-gpt-5.6-luna}
image_model=${IMAGE_MODEL:-gpt-image-2}

for numeric in "$account_id" "$api_key_id" "$relay_port"; do
  if [[ ! $numeric =~ ^[0-9]+$ ]]; then
    echo "ACCOUNT_ID、API_KEY_ID 与 RELAY_PORT 必须是正整数。" >&2
    exit 2
  fi
done
if [[ ! $run_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "RUN_ID 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi

admin_token=${ADMIN_BEARER_TOKEN:-}
if [[ -z $admin_token && -n ${ADMIN_BEARER_TOKEN_FILE:-} ]]; then
  if [[ ! -f $ADMIN_BEARER_TOKEN_FILE ]]; then
    echo "ADMIN_BEARER_TOKEN_FILE 不存在。" >&2
    exit 2
  fi
  admin_token=$(<"$ADMIN_BEARER_TOKEN_FILE")
fi
if [[ -z $admin_token || ! $admin_token =~ ^[A-Za-z0-9._~-]+$ ]]; then
  echo "必须通过 ADMIN_BEARER_TOKEN 或只读 token 文件提供管理凭据。" >&2
  exit 2
fi

# 校验 token 剩余有效期。管理 token 只签 24 小时，过期时的失败极具迷惑性：走普通 API
# 的 A09／A11 流量与 pcap 全部正常，只有依赖管理接口的 A12／A13／A14 三个场景 actions
# 全空、pcap 0～24 字节（k71 因此半失败）。格式合法不等于还能用，必须看 exp。
admin_token_min_ttl=${ADMIN_TOKEN_MIN_TTL_SECONDS:-1800}
if [[ ! $admin_token_min_ttl =~ ^[0-9]+$ ]]; then
  echo "ADMIN_TOKEN_MIN_TTL_SECONDS 必须是非负整数。" >&2
  exit 2
fi
admin_token_ttl_report=$(
  ADMIN_TOKEN_VALUE="$admin_token" MIN_TTL="$admin_token_min_ttl" python3 - <<'PY'
import base64, json, os, sys, time

token = os.environ["ADMIN_TOKEN_VALUE"]
min_ttl = int(os.environ["MIN_TTL"])
parts = token.split(".")
if len(parts) != 3:
    sys.exit("管理 token 不是三段式 JWT，无法解析有效期。")
payload_raw = parts[1]
try:
    padded = payload_raw + "=" * (-len(payload_raw) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
except Exception as error:  # noqa: BLE001 —— 任何解析失败都必须拒绝启动
    sys.exit(f"管理 token 载荷无法解码：{error}")
exp = payload.get("exp")
if not isinstance(exp, int):
    sys.exit("管理 token 载荷缺少整数 exp，拒绝启动。")
remaining = exp - int(time.time())
expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
if remaining < min_ttl:
    sys.exit(
        f"管理 token 剩余有效期 {remaining}s 少于要求的 {min_ttl}s"
        f"（过期时刻 {expires_at}）。请在服务容器内用 state/jwtgen-bin 重签，"
        "输出按 ^JWT= 提取后重试。"
    )
print(f"管理 token 剩余 {remaining}s，过期时刻 {expires_at}")
PY
) || {
  echo "$admin_token_ttl_report" >&2
  exit 2
}
echo "$admin_token_ttl_report"

work_dir="$capture_root/runs/$run_id"
container_work_dir="$capture_mount/runs/$run_id"
tls_dir="$work_dir/tls-private"
container_tls_dir="$container_work_dir/tls-private"
runtime_dir="$capture_root/runtime/candidate-aux-$run_id"
ca_full="$capture_root/state/mitm/mitmproxy-ca.pem"
ca_cert="$capture_root/state/mitm/mitmproxy-ca-cert.pem"
custom_ca_path=/usr/local/share/ca-certificates/candidate-aux-capture.crt
relay_tool="$capture_mount/tools/official_client_capture/upstream_byte_relay.py"
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
scrub_tool="$script_dir/scrub_raw_bytes.py"

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

# A11 的第一跳要求 Live attestation。Linux 生不出 DeviceCheck 值，Sub2API 为隔离抓包
# 提供了 candidatecapture 构建：只补这一个值，Live 调度、首跳、Sideband、TLS 与版本画像
# 都不改动，且必须逐项匹配本轮的 api_key／group／account／临时代理四元组才生效。
# 该 provider 只读进程环境，因此要重建容器而不是 docker restart；恢复时按原 compose 拉回。
live_attestation_armed=0

deploy_with_live_attestation() {
  local expires_at
  [[ -n ${LIVE_ATTESTATION_COMPOSE_DIR:-} && -n ${LIVE_ATTESTATION_COMPOSE_FILES:-} ]] || return 0
  group_id=$(db_query "select group_id from api_keys where id = $api_key_id")
  [[ $group_id =~ ^[0-9]+$ ]] || { echo "无法读取 API Key 分组，跳过 Live attestation 注入。" >&2; return 1; }
  expires_at=$(( $(date -u +%s) + 900 ))
  install -d -m 0700 "$capture_root/runtime/live-attestation"
  cat > "$capture_root/runtime/live-attestation/$run_id.override.yml" <<YML
services:
  sub2api:
    environment:
      SUB2API_LIVE_ATTESTATION_CAPTURE_MODE: synthetic-only
      SUB2API_LIVE_ATTESTATION_CAPTURE_ACK: YES_I_ACCEPT_SYNTHETIC_ONLY
      SUB2API_LIVE_ATTESTATION_CAPTURE_API_KEY_ID: "$api_key_id"
      SUB2API_LIVE_ATTESTATION_CAPTURE_GROUP_ID: "$group_id"
      SUB2API_LIVE_ATTESTATION_CAPTURE_ACCOUNT_ID: "$account_id"
      SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_NAME: "$proxy_name"
      SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_HOST: "$capture_container"
      SUB2API_LIVE_ATTESTATION_CAPTURE_PROXY_PORT: "$relay_port"
      SUB2API_LIVE_ATTESTATION_CAPTURE_EXPIRES_AT_UNIX: "$expires_at"
YML
  chmod 600 "$capture_root/runtime/live-attestation/$run_id.override.yml"
  live_attestation_armed=1
  (cd "$LIVE_ATTESTATION_COMPOSE_DIR" && eval docker compose $LIVE_ATTESTATION_COMPOSE_FILES \
    -f "$capture_root/runtime/live-attestation/$run_id.override.yml" up -d sub2api) >/dev/null || return 1
  wait_healthy || return 1
  # compose 重建的是全新容器，之前 docker cp 进去的抓包 CA 随旧容器一起消失；
  # 不补装，第一跳会在 TLS 阶段被自己的 relay 证书挡下（bad certificate）。
  docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null || return 1
  docker exec "$service_container" update-ca-certificates >/dev/null 2>&1 || return 1
  restart_service
}

restore_deploy_without_live_attestation() {
  [[ $live_attestation_armed == 1 ]] || return 0
  live_attestation_armed=0
  (cd "$LIVE_ATTESTATION_COMPOSE_DIR" && eval docker compose $LIVE_ATTESTATION_COMPOSE_FILES up -d sub2api) >/dev/null || return 1
  wait_healthy || return 1
  # 恢复部署同样是新容器：CA 由 EXIT 钩子按基线清理，这里只需保证服务已就绪。
  rm -f "$capture_root/runtime/live-attestation/$run_id.override.yml"
}

auth_config() {
  # curl 从匿名 fd 读取 header，凭据不进入命令行参数或抓包目录。
  printf 'header = "Authorization: Bearer %s"\n' "$1"
}

request_with_token() {
  local token=$1
  shift
  # relay 一上线，Sub2API 的后台流量就会打到只接受特定形态的 relay 上并被拒，账号随即
  # 进入临时熔断；清一次不够，必须紧贴每次触发请求，否则请求本身会拿到 503。
  clear_account_gate
  curl --silent --show-error --max-time 120 --config <(auth_config "$token") "$@"
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
original_model_mapping_state=""
restored_model_mapping_state=""
model_mapping_restore_armed=0
model_mapping_restored=1
original_hosts_hash=""
original_ca_hash=""
restored_hosts_hash=""
restored_ca_hash=""
service_container_id_before=""
dummy_refresh=""

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
    if ! stop_container_process "$pid" TERM "$scenario_id relay" 50; then
      return 1
    fi
    relay_started=0
  fi
  # 合成响应很快，内核过滤器可能已经收到流量，但 tcpdump 用户态还没来得及
  # 把数据块落盘。固定留出排空窗口，避免生成只有 24 字节全局头的空 pcap。
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
  # 到这里两个后台进程均已停止。先清空全局标记，后续即使产物校验失败，EXIT
  # 恢复钩子也不会把同一目录当成仍在运行而二次处理。
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
    "$original_proxy_state" "$restored_hosts_hash" "$original_hosts_hash" \
    "$restored_ca_hash" "$original_ca_hash" "$model_mapping_restored" \
    "$codex_version" <<'PY'
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
run_id = sys.argv[2]
status = sys.argv[3]
exit_code = int(sys.argv[4])
original_proxy = sys.argv[5]
restored_hosts_hash, original_hosts_hash = sys.argv[6:8]
restored_ca_hash, original_ca_hash = sys.argv[8:10]
model_mapping_restored = sys.argv[10] == "1"
codex_version = sys.argv[11]

scenarios = []
for scenario_id in ("A09", "A11", "A12", "A13", "A14"):
    scenario_root = root / "scenarios" / scenario_id
    interventions = scenario_root / "relay" / "intervention.jsonl"
    actions = Counter()
    production_forwarded = False
    if interventions.is_file():
        for line in interventions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            action = event.get("action")
            if action:
                actions[action] += 1
            production_forwarded = production_forwarded or bool(
                event.get("production_forwarded")
            )
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
    "schema_version": "candidate-aux-capture/v1",
    "codex_version": codex_version,
    "run_id": run_id,
    "status": status,
    "exit_code": exit_code,
    "synthetic_profile": "candidate-aux-v1",
    "explicit_gate": True,
    "production_forwarding_enabled": False,
    "scenarios": scenarios,
    "restoration": {
        "account_proxy_original": original_proxy,
        "account_model_mapping_equal": model_mapping_restored,
        "hosts_sha256_equal": bool(original_hosts_hash)
        and restored_hosts_hash == original_hosts_hash,
        "ca_bundle_sha256_equal": bool(original_ca_hash)
        and restored_ca_hash == original_ca_hash,
    },
}
path = root / "run-summary.json"
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
PY
}

# 从 stdin 读 /etc/hosts 内容，剔除 Docker 为容器自身写的
# 「<地址> <实例 ID 前 12 位>」行后按行排序取摘要。
# 算法必须与 codex_upgrade_environment_probe.py 的 _container_hosts_digest 逐字一致，
# 否则本脚本自查通过、环境探针却判定漂移。
hosts_digest_excluding_self() {
  CANDIDATE_AUX_SELF_ID="$1" python3 -c '
import hashlib
import os
import sys

identifier = os.environ["CANDIDATE_AUX_SELF_ID"].strip()
self_names = {identifier, identifier[:12]}
retained = []
for line in sys.stdin.read().splitlines():
    fields = line.split()
    if len(fields) == 2 and fields[1] in self_names:
        continue
    retained.append(line)
payload = "".join(f"{line}\n" for line in sorted(retained))
print(hashlib.sha256(payload.encode("utf-8")).hexdigest())
'
}

restore_environment() {
  local original_exit_code=$?
  local proxy_value fallback_value current_proxy_state
  local model_mapping_hex
  local service_restart_needed=0
  trap - EXIT ERR INT TERM
  set +e

  # 抓包产物不完整属于本轮验收失败，不等同于环境恢复失败；后台 PID 的停止动作
  # 已在 stop_capture 的产物校验之前完成。
  stop_capture || capture_status=failed

  if [[ $proxy_bound == 1 && $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ ]]; then
    read -r proxy_value fallback_value <<<"${original_proxy_state/|/ }"
    db_query "update accounts set proxy_id = $proxy_value, proxy_fallback_origin_id = $fallback_value where id = $account_id" \
      >/dev/null || restore_failed=1
    proxy_bound=0
  fi
  if [[ $proxy_created == 1 && $proxy_id =~ ^[0-9]+$ ]]; then
    db_query "delete from proxies where id = $proxy_id and name = 'candidate-aux-${run_id:0:72}'" \
      >/dev/null || restore_failed=1
    proxy_created=0
  fi

  # 图片端点需要图片模型通过账号的显式 model_mapping 白名单。只恢复该字段，
  # 不重写 credentials 中的 OAuth 凭据，并在最后对原始 JSON 做逐字节状态核验。
  if [[ $model_mapping_restore_armed == 1 ]]; then
    service_restart_needed=1
    case "$original_model_mapping_state" in
      present:*)
        model_mapping_hex=${original_model_mapping_state#present:}
        db_query "update accounts set credentials = jsonb_set(
          coalesce(credentials,'{}'::jsonb),
          '{model_mapping}',
          convert_from(decode('$model_mapping_hex','hex'),'UTF8')::jsonb,
          true
        ) where id = $account_id" >/dev/null || restore_failed=1
        ;;
      missing:)
        db_query "update accounts set credentials = coalesce(credentials,'{}'::jsonb) - 'model_mapping'
          where id = $account_id" >/dev/null || restore_failed=1
        ;;
      *)
        restore_failed=1
        ;;
    esac
    model_mapping_restore_armed=0
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

  # Docker restart 会重建 /etc/hosts；最后按字节恢复运行前快照，随后不再重启。
  restore_deploy_without_live_attestation || restore_failed=1

  # 只有容器实例没被换掉时才回灌快照。
  #
  # 恢复部署会 compose 重建服务容器，Docker 为新实例重新生成 /etc/hosts，其中的
  # 自引用行是「<容器 IP> <新实例 ID 前 12 位>」。环境探针
  # （codex_upgrade_environment_probe.py 的 _container_hosts_digest）正是靠
  # 「主机名等于当前容器 ID」来识别并剔除这两行，从而吸收 A11 必然发生的重建。
  # 此时若把运行前快照按字节灌回去，自引用行会带上旧实例 ID，探针认不出、只能
  # 按「人为写入的劫持行」保留，after 摘要必然偏离 before —— 恢复动作本身反而
  # 制造出 environment_contaminated。重建后的 hosts 由 Docker 全新生成，不含任何
  # 采集期写入的劫持行，不回灌才是与 before 等价的状态。
  service_container_id_after=$(docker inspect -f '{{.Id}}' "$service_container" 2>/dev/null || true)
  if [[ -s $runtime_dir/hosts.before
        && -n $service_container_id_before
        && $service_container_id_after == "$service_container_id_before" ]]; then
    docker cp "$runtime_dir/hosts.before" "$service_container:/tmp/candidate-aux-hosts.restore" \
      >/dev/null 2>&1 || restore_failed=1
    docker exec "$service_container" sh -c \
      'cat /tmp/candidate-aux-hosts.restore > /etc/hosts && rm -f /tmp/candidate-aux-hosts.restore' \
      >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ -s $runtime_dir/ca-certificates.before ]]; then
    restored_ca_hash=$(docker exec "$service_container" sha256sum \
      /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
    if [[ $restored_ca_hash != "$original_ca_hash" ]]; then
      docker cp "$runtime_dir/ca-certificates.before" \
        "$service_container:/tmp/candidate-aux-ca.restore" >/dev/null 2>&1 || restore_failed=1
      docker exec "$service_container" sh -c \
        'cat /tmp/candidate-aux-ca.restore > /etc/ssl/certs/ca-certificates.crt && rm -f /tmp/candidate-aux-ca.restore' \
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
  [[ $current_proxy_state == "$original_proxy_state" ]] || restore_failed=1
  if [[ -n $original_model_mapping_state ]]; then
    restored_model_mapping_state=$(db_query "
      select case
        when credentials ? 'model_mapping' then
          'present:' || encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
        else 'missing:'
      end
      from accounts where id = $account_id" 2>/dev/null)
    if [[ $restored_model_mapping_state == "$original_model_mapping_state" ]]; then
      model_mapping_restored=1
    else
      model_mapping_restored=0
      restore_failed=1
    fi
  fi
  if [[ -n $service_container_id_before
        && $service_container_id_after == "$service_container_id_before" ]]; then
    # 容器实例未变：hosts 已按字节回灌，用最严格的字节比对。
    restored_hosts_hash=$(docker exec "$service_container" sha256sum /etc/hosts 2>/dev/null | awk '{print $1}')
  else
    # 容器被 compose 重建：自引用行必然改写，字节比对恒不成立。改用环境探针的
    # 同一语义（剔除自引用行后按行排序）比对，两端摘要都按该语义重算才可比。
    original_hosts_hash=$(hosts_digest_excluding_self "$service_container_id_before" \
      <"$runtime_dir/hosts.before" 2>/dev/null || true)
    restored_hosts_hash=$(docker exec "$service_container" cat /etc/hosts 2>/dev/null |
      hosts_digest_excluding_self "$service_container_id_after" 2>/dev/null || true)
  fi
  restored_ca_hash=$(docker exec "$service_container" sha256sum \
    /etc/ssl/certs/ca-certificates.crt 2>/dev/null | awk '{print $1}')
  [[ $restored_hosts_hash == "$original_hosts_hash" ]] || restore_failed=1
  [[ $restored_ca_hash == "$original_ca_hash" ]] || restore_failed=1
  if [[ $custom_ca_baseline_absent == 1 ]]; then
    docker exec "$service_container" test ! -e "$custom_ca_path" >/dev/null 2>&1 ||
      restore_failed=1
  fi

  if [[ -n $dummy_refresh ]]; then
    CANDIDATE_A13_DUMMY_TOKEN="$dummy_refresh" python3 - "$work_dir" <<'PY' || restore_failed=1
import os
import sys
from pathlib import Path

needle = os.environ["CANDIDATE_A13_DUMMY_TOKEN"].encode("ascii")
for path in Path(sys.argv[1]).rglob("*"):
    if path.is_file() and needle in path.read_bytes():
        raise SystemExit(1)
PY
  fi
  dummy_refresh=""

  if [[ $restore_failed != 0 ]]; then
    write_summary restoration_failed 97 || true
    echo "候选辅助抓包环境恢复失败；备份保留在 $runtime_dir。" >&2
    exit 97
  fi

  final_status=$capture_status
  final_exit_code=$original_exit_code
  if [[ $original_exit_code != 0 ]]; then
    final_status=failed
  fi
  write_summary "$final_status" "$final_exit_code" || true
  rm -f -- "$tls_dir/relay.key" "$tls_dir/relay.csr" "$tls_dir/relay.ext"
  echo "环境已精确恢复：账号 proxy/fallback、hosts、CA bundle 与 keeper 状态均已核验。"
  exit "$original_exit_code"
}

trap restore_environment EXIT ERR INT TERM

original_proxy_state=$(db_query \
  "select coalesce(proxy_id::text,'NULL') || '|' || coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id = $account_id")
if [[ ! $original_proxy_state =~ ^(NULL|[0-9]+)\|(NULL|[0-9]+)$ ]]; then
  echo "无法读取账号 proxy/fallback 初始状态。" >&2
  exit 1
fi
original_gate_state=$(account_gate_state)
if [[ ! $original_gate_state =~ ^[0-9a-f]*\|[0-9a-f]*$ ]]; then
  echo "无法读取账号 #$account_id 的调度门初始状态。" >&2
  exit 1
fi
account_shape=$(db_query \
  "select platform || '|' || type || '|' || coalesce(parent_account_id::text,'NULL') from accounts where id = $account_id")
if [[ ! $account_shape =~ ^openai\|oauth\|NULL$ ]]; then
  echo "ACCOUNT_ID 必须是非影子的 OpenAI OAuth 专用账号。" >&2
  exit 1
fi
if [[ ! $image_model =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "IMAGE_MODEL 只能包含字母、数字、点、下划线和连字符。" >&2
  exit 2
fi
model_mapping_type=$(db_query "
  select case
    when not (credentials ? 'model_mapping') then 'missing'
    else coalesce(jsonb_typeof(credentials->'model_mapping'),'null')
  end
  from accounts where id = $account_id")
if [[ $model_mapping_type != object && $model_mapping_type != missing ]]; then
  echo "账号 model_mapping 必须缺失或为 JSON 对象，拒绝覆盖未知结构。" >&2
  exit 1
fi
original_model_mapping_state=$(db_query "
  select case
    when credentials ? 'model_mapping' then
      'present:' || encode(convert_to((credentials->'model_mapping')::text,'UTF8'),'hex')
    else 'missing:'
  end
  from accounts where id = $account_id")
if [[ ! $original_model_mapping_state =~ ^(present:[0-9a-f]+|missing:)$ ]]; then
  echo "无法保存账号 model_mapping 初始状态。" >&2
  exit 1
fi

api_key=$(db_query "select key from api_keys where id = $api_key_id")
group_id=$(db_query "select group_id from api_keys where id = $api_key_id")
if [[ -z $api_key || ! $group_id =~ ^[0-9]+$ ]]; then
  echo "API_KEY_ID 不存在或未绑定分组。" >&2
  exit 1
fi

# 网关场景必须可证明只会选中目标账号；不临时禁用其他生产账号。
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
allow_live=$(db_query "select allow_live::text from groups where id = $group_id")
if [[ $allow_live != true ]]; then
  echo "API Key 分组未启用 Live，无法执行 A11。" >&2
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
admin_base_url="$service_base_url/api/v1/admin"

if ! docker exec "$service_container" getent hosts "$capture_container" >/dev/null; then
  echo "候选服务容器无法解析受控 capture 容器。" >&2
  exit 1
fi
if ! docker exec "$capture_container" sh -c 'command -v tcpdump' >/dev/null; then
  echo "capture 容器缺少 tcpdump，无法形成 A11/A13/A14 的 SNI pcap。" >&2
  exit 1
fi
relay_help=$(docker exec "$capture_container" python3 "$relay_tool" --help 2>&1 || true)
if ! grep -q 'candidate-aux-v1' <<<"$relay_help" ||
  ! grep -q -- '--codex-version' <<<"$relay_help"; then
  echo "capture 容器中的 relay 尚未同步目标版本参数或候选辅助合成画像。" >&2
  exit 1
fi

docker cp "$service_container:/etc/hosts" "$runtime_dir/hosts.before" >/dev/null
service_container_id_before=$(docker inspect -f '{{.Id}}' "$service_container")
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

# 一个证书覆盖本轮四个 SNI。区域文件 host 固定写在 relay 合成响应里，第二跳只能
# 来自第一跳返回值，入口脚本没有独立硬编码 PUT URL。
openssl req -newkey rsa:2048 -nodes -keyout "$tls_dir/relay.key" \
  -out "$tls_dir/relay.csr" -subj "/CN=chatgpt.com" >/dev/null 2>&1
printf '%s\n' \
  'subjectAltName=DNS:chatgpt.com,DNS:api.openai.com,DNS:auth.openai.com,DNS:region-candidate-0145.oaiusercontent.com' \
  'extendedKeyUsage=serverAuth' >"$tls_dir/relay.ext"
serial=$(openssl rand -hex 16)
openssl x509 -req -in "$tls_dir/relay.csr" -CA "$ca_full" -CAkey "$ca_full" \
  -set_serial "0x$serial" -out "$tls_dir/relay.crt" -days 1 -sha256 \
  -extfile "$tls_dir/relay.ext" >/dev/null 2>&1
chmod 0600 "$tls_dir"/*

proxy_name="candidate-aux-${run_id:0:72}"
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

# 账号 99 的长期配置使用显式文本模型白名单；图片抓包只在本轮临时加入图片
# 模型，并由 EXIT 钩子恢复原始字段。先武装恢复标记，再执行更新，覆盖中断窗口。
model_mapping_restore_armed=1
model_mapping_restored=0
db_query "update accounts set credentials = jsonb_set(
  coalesce(credentials,'{}'::jsonb),
  '{model_mapping}',
  coalesce(credentials->'model_mapping','{}'::jsonb) ||
    jsonb_build_object('$image_model','$image_model'),
  true
) where id = $account_id" >/dev/null

if ! docker cp "$ca_cert" "$service_container:$custom_ca_path" >/dev/null; then
  # 基线已确认该路径不存在；即使复制只完成了一部分，恢复钩子也必须尝试清理。
  ca_installed=1
  echo "安装候选辅助抓包 CA 失败。" >&2
  exit 1
fi
ca_installed=1
docker exec "$service_container" update-ca-certificates >/dev/null 2>&1
restart_service

start_capture() {
  local scenario=$1
  local scenario_root="$work_dir/scenarios/$scenario"
  local container_scenario_root="$container_work_dir/scenarios/$scenario"
  install -d -m 0700 "$scenario_root/relay-private" "$scenario_root/trigger"
  docker exec "$capture_container" mkdir -p \
    "$container_scenario_root/relay-private" "$container_scenario_root/trigger"
  current_scenario=$scenario
  clear_account_gate

  docker exec "$capture_container" sh -c '
    umask 077
    python3 "$1" --cert "$2" --key "$3" --mode connect --port "$4" \
      --upstream-host chatgpt.com --output "$5" --timeout 300 \
      --codex-version "$6" \
      --synthetic-profile candidate-aux-v1 --allow-synthetic-responses \
      >"$7" 2>&1 &
    echo $! >"$8"
  ' sh "$relay_tool" "$container_tls_dir/relay.crt" "$container_tls_dir/relay.key" \
    "$relay_port" "$container_scenario_root/relay-private" "$codex_version" \
    "$container_scenario_root/relay.log" "$container_scenario_root/relay.pid"
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
  for _ in $(seq 1 100); do
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

wait_live_cleanup() {
  local call_id=$1
  local output_path=$2
  local call_hash call_key controller lease_id user_id
  local account_score user_score api_key_score
  if [[ ! $call_id =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "A11 返回的 call_id 格式无效。" >&2
    return 1
  fi
  call_hash=$(printf '%s' "$call_id" | sha256sum | awk '{print $1}')
  call_key="live:call:$call_hash"
  for _ in $(seq 1 100); do
    controller=$(docker exec "$redis_container" redis-cli --raw HGET "$call_key" controller | tr -d '\r')
    lease_id=$(docker exec "$redis_container" redis-cli --raw HGET "$call_key" lease_id | tr -d '\r')
    user_id=$(docker exec "$redis_container" redis-cli --raw HGET "$call_key" user_id | tr -d '\r')
    if [[ $controller == closed && -n $lease_id && $user_id =~ ^[0-9]+$ ]]; then
      account_score=$(docker exec "$redis_container" redis-cli --raw \
        ZSCORE "concurrency:live:account:$account_id" "$lease_id" | tr -d '\r')
      user_score=$(docker exec "$redis_container" redis-cli --raw \
        ZSCORE "concurrency:live:user:$user_id" "$lease_id" | tr -d '\r')
      api_key_score=$(docker exec "$redis_container" redis-cli --raw \
        ZSCORE "concurrency:live:api_key:$api_key_id" "$lease_id" | tr -d '\r')
      if [[ -z $account_score && -z $user_score && -z $api_key_score ]]; then
        printf '%s\n' \
          'controller_closed=true' \
          'account_lease_released=true' \
          'user_lease_released=true' \
          'api_key_lease_released=true' >"$output_path"
        chmod 0600 "$output_path"
        return 0
      fi
    fi
    sleep 0.1
  done
  echo "A11 session.ended 后未证明 Live 记录关闭且三类租约全部释放。" >&2
  return 1
}

official_ua="codex_exec/$codex_version (Ubuntu 24.4.0; x86_64) unknown (codex_exec; $codex_version)"
common_gateway_headers=(
  -H "User-Agent: $official_ua"
  -H 'Originator: codex_exec'
  -H "Version: $codex_version"
  -H 'X-Codex-Terminal: unknown'
)

# A09：models、三种 legacy compact header 插槽、两阶段 alpha-search、images 两端点。
start_capture A09
trigger_root="$work_dir/scenarios/A09/trigger"
code=$(request_with_token "$api_key" --output "$trigger_root/models.json" --write-out '%{http_code}' \
  "${common_gateway_headers[@]}" \
  "$service_base_url/backend-api/codex/models?client_version=$codex_version")
assert_2xx A09-models "$code"

compact_installation_id=33333333-3333-4333-8333-333333333333
compact_session_id=11111111-1111-4111-8111-111111111111
compact_window_id="$compact_session_id:0"
compact_body=$(printf \
  '{"model":"%s","input":[],"parallel_tool_calls":false,"reasoning":{"effort":"medium"},"prompt_cache_key":"%s","text":{"verbosity":"low"}}' \
  "$model" "$compact_session_id")
for variant in prime default beta turn_state; do
  extra_headers=()
  case "$variant" in
    prime) compact_turn_id=22222222-2222-4222-8222-222222222219 ;;
    default) compact_turn_id=22222222-2222-4222-8222-222222222220 ;;
    # beta 响应与随后回送必须属于同一个 turn；状态仓库故意按 session+turn
    # 隔离，若这里换 turn_id，就只能证明响应下发，不能证明真实闭环。
    beta | turn_state) compact_turn_id=22222222-2222-4222-8222-222222222221 ;;
  esac
  compact_turn_metadata=$(printf \
    '{"installation_id":"%s","session_id":"%s","thread_id":"%s","turn_id":"%s","window_id":"%s","request_kind":"compaction","thread_source":"user","turn_started_at_unix_ms":1785432600000,"capture_variant":"%s"}' \
    "$compact_installation_id" "$compact_session_id" "$compact_session_id" \
    "$compact_turn_id" "$compact_window_id" "$variant")
  case "$variant" in
    beta) extra_headers=(-H 'X-Codex-Beta-Features: candidate_aux_beta') ;;
  esac
  code=$(request_with_token "$api_key" --output "$trigger_root/compact-$variant.json" \
    --write-out '%{http_code}' -X POST "${common_gateway_headers[@]}" \
    "${extra_headers[@]}" -H 'Content-Type: application/json' \
    -H "X-Codex-Installation-ID: $compact_installation_id" \
    -H "Session-Id: $compact_session_id" \
    -H "Thread-Id: $compact_session_id" \
    -H "X-Codex-Window-Id: $compact_window_id" \
    -H "X-Codex-Turn-Metadata: $compact_turn_metadata" \
    --data-binary "$compact_body" "$service_base_url/v1/responses/compact")
  assert_2xx "A09-compact-$variant" "$code"
done

for phase in 1 2; do
  search_body=$(printf \
    '{"id":"candidate-search","model":"%s","input":[],"commands":{"search_query":[{"q":"candidate phase %s"}]},"settings":{"allowed_callers":["direct"],"external_web_access":false},"max_output_tokens":2000}' \
    "$model" "$phase")
  code=$(request_with_token "$api_key" --output "$trigger_root/search-$phase.json" \
    --write-out '%{http_code}' -X POST "${common_gateway_headers[@]}" \
    -H 'Content-Type: application/json' \
    -H "X-Codex-Turn-Metadata: {\"phase\":$phase}" \
    --data-binary "$search_body" "$service_base_url/v1/alpha/search")
  assert_2xx "A09-search-$phase" "$code"
done

code=$(request_with_token "$api_key" --output "$trigger_root/image-generation.json" \
  --write-out '%{http_code}' -X POST "${common_gateway_headers[@]}" \
  -H 'Content-Type: application/json' \
  --data-binary "{\"model\":\"$image_model\",\"prompt\":\"candidate auxiliary probe\",\"background\":\"auto\",\"quality\":\"high\",\"n\":1,\"size\":\"1024x1024\",\"response_format\":\"b64_json\"}" \
  "$service_base_url/v1/images/generations")
assert_2xx A09-image-generation "$code"

printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' |
  base64 -d >"$trigger_root/one-pixel.png"
code=$(request_with_token "$api_key" --output "$trigger_root/image-edit.json" \
  --write-out '%{http_code}' -X POST "${common_gateway_headers[@]}" \
  -F "model=$image_model" -F 'prompt=candidate auxiliary edit probe' \
  -F 'background=auto' -F 'quality=high' -F 'size=1024x1024' \
  -F "image=@$trigger_root/one-pixel.png;type=image/png" \
  "$service_base_url/v1/images/edits")
assert_2xx A09-image-edit "$code"
wait_action A09 alpha_search 2
stop_capture

# A11：第一跳返回 call_id，生产 observer 自动建立 api.openai.com sideband；relay
# 发送 session.ended，候选走真实终止清理路径。第一跳前先按本轮四元组重建服务，
# 使 candidatecapture provider 生效；未提供 compose 坐标时保持原样并由断言暴露。
deploy_with_live_attestation || echo "Live attestation 注入未生效，A11 将按原样执行。" >&2
start_capture A11
trigger_root="$work_dir/scenarios/A11/trigger"
live_body='{"sdp":"v=0\\r\\n","session":{"model":"gpt-realtime","modalities":["audio","text"]}}'
code=$(request_with_token "$api_key" --output "$trigger_root/live.sdp" --dump-header "$trigger_root/live.headers" \
  --write-out '%{http_code}' -X POST "${common_gateway_headers[@]}" \
  -H 'Content-Type: application/json' --data-binary "$live_body" \
  "$service_base_url/backend-api/codex/realtime/calls")
assert_2xx A11-live-first-hop "$code"
wait_action A11 realtime_first_hop
wait_action A11 realtime_sideband
live_location=$(tr -d '\r' <"$trigger_root/live.headers" | \
  awk 'BEGIN{IGNORECASE=1} /^Location:[[:space:]]*/ {sub(/^[^:]+:[[:space:]]*/, ""); print; exit}')
live_call_id=${live_location%%\?*}
live_call_id=${live_call_id##*/}
wait_live_cleanup "$live_call_id" "$trigger_root/live-cleanup.txt"
stop_capture

# A12：目标画像下 QueryUsage 自然发出 settings/user + usage + details；
# ResetQuota 的 consume 仍由同一生产 service 路径生成 redeem_request_id，
# 但 TLS 隧道只到纯合成 relay。
start_capture A12
trigger_root="$work_dir/scenarios/A12/trigger"
code=$(request_with_token "$admin_token" --output "$trigger_root/quota.json" --write-out '%{http_code}' \
  "$admin_base_url/openai/accounts/$account_id/quota")
assert_2xx A12-quota "$code"
code=$(request_with_token "$admin_token" --output "$trigger_root/consume.json" --write-out '%{http_code}' \
  -X POST "$admin_base_url/openai/accounts/$account_id/reset-quota")
assert_2xx A12-consume "$code"
wait_action A12 wham_usage
wait_action A12 wham_credit_details
wait_action A12 wham_safe_consume
stop_capture

# A13：raw 管理入口不关联/更新账号。dummy 只经匿名管道进入请求；relay 在任何
# ByteRecorder.write 之前等长遮蔽 form/json 值，并返回 invalid_grant。
start_capture A13
trigger_root="$work_dir/scenarios/A13/trigger"
dummy_refresh="dmy_$(openssl rand -hex 24)"
code=$(
  printf '{"refresh_token":"%s","proxy_id":%s}' "$dummy_refresh" "$proxy_id" |
    curl --silent --show-error --max-time 120 --config <(auth_config "$admin_token") \
      --output "$trigger_root/invalid-grant.json" --write-out '%{http_code}' \
      -X POST -H 'Content-Type: application/json' --data-binary @- \
      "$admin_base_url/openai/refresh-token"
)
if [[ ! $code =~ ^(400|401|422|502)$ ]]; then
  echo "A13 预期受控 invalid_grant，实际 HTTP $code。" >&2
  exit 1
fi
wait_action A13 oauth_dummy_invalid_grant
stop_capture

# A14：固定内存 payload 的生产 AccountTestService 路径完成 create→响应 URL→PUT→
# uploaded。入口没有区域 URL 参数，因此 PUT host 只能来自 create 响应。
start_capture A14
trigger_root="$work_dir/scenarios/A14/trigger"
code=$(request_with_token "$admin_token" --output "$trigger_root/files-probe.sse" \
  --write-out '%{http_code}' -X POST -H 'Content-Type: application/json' \
  --data-binary '{"mode":"official_files_probe"}' \
  "$admin_base_url/accounts/$account_id/test")
assert_2xx A14-files "$code"
wait_action A14 files_create
wait_action A14 files_blob_put
wait_action A14 files_uploaded
stop_capture

# 冻结动作计数与“绝不生产转发”最终门禁。任何多余/缺失动作都让本轮失败；摘要仍由
# EXIT 恢复钩子生成，并带出恢复核验状态。
python3 - "$work_dir" "$codex_version" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
codex_version = sys.argv[2]
expected = {
    "A09": {
        "models_manifest": 1,
        "legacy_compact": 4,
        "alpha_search": 2,
        "images_generation": 1,
        "images_edit": 1,
    },
    "A11": {"realtime_first_hop": 1, "realtime_sideband": 1},
    # ResetQuota 消费额度后必然再查一次用量刷新显示缓存（openai_oauth_handler.go
    # 的 Step 2，用 WithoutCancel + 独立超时，不受入口 context 取消影响），因此
    # A12 的两次入口调用共产生两轮 settings/user、usage 与 credit_details。
    # settings/user 只在当前画像登记该端点时出现；本 job 绑定的目标画像已登记，
    # 所以它与其余两条 GET 一样必须计入受控出站事实。
    "A12": {
        "wham_settings_user": 2,
        "wham_usage": 2,
        "wham_credit_details": 2,
        "wham_safe_consume": 1,
    },
    "A13": {"oauth_dummy_invalid_grant": 1},
    "A14": {"files_create": 1, "files_blob_put": 1, "files_uploaded": 1},
}
for scenario, wanted in expected.items():
    path = root / "scenarios" / scenario / "relay" / "intervention.jsonl"
    manifest_path = root / "scenarios" / scenario / "relay" / "relay.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("synthetic_profile") != "candidate-aux-v1":
        raise SystemExit(f"{scenario} relay 未绑定冻结合成画像")
    if manifest.get("codex_version") != codex_version:
        raise SystemExit(f"{scenario} relay Codex 版本与 Campaign 目标不一致")
    if manifest.get("production_forwarding_enabled") is not False:
        raise SystemExit(f"{scenario} relay 仍允许生产转发")
    for connection in manifest.get("connections", []):
        if connection.get("valid") is not True:
            raise SystemExit(f"{scenario} 存在无效 relay 连接")
        if connection.get("production_forwarded") is not False:
            raise SystemExit(f"{scenario} 连接未证明 production_forwarded=false")
    counts = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("production_forwarded") is not False:
            raise SystemExit(f"{scenario} 存在未证明为本地合成的事件")
        if event.get("type") != "synthetic_aux_response":
            raise SystemExit(f"{scenario} 存在白名单外受控事件: {event.get('type')}")
        counts[event["action"]] += 1
    if counts != Counter(wanted):
        raise SystemExit(f"{scenario} 动作计数不匹配: {dict(counts)} != {wanted}")
PY

capture_status=complete
printf 'run_id=%s\n' "$run_id"
